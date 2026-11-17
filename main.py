"""Dip-buying mean-reversion bot for make.fun's "Race a BTC trading bot live
for 24 hours" (btc-race-24h-20261117-0603).

Protocol: agent-fun/bars-replay/v1, newline-delimited JSON on stdin/stdout.
The harness sends hello once, then one batch per 1-minute bar, then done.
Each batch carries the bar that just closed, the fills of the orders sent on
the previous batch, and the account. The bot answers every batch with exactly
one reply echoing its seq. Orders decided on the batch of bar t fill at the
open of the next bar, so every decision here is made on closes only.

Strategy (long only, at most one open position, 100% of equity per trade):
  * Sit in cash through the grind. Buy only a deep dip: the close at least
    0.70% below its trailing 60-bar simple moving average, RSI(14) below 40
    (deeply oversold), and the close at least 0.05% above the trailing
    30-bar low (a bounce confirmation, not a falling knife).
  * Exits, all measured from the entry fill price against closes: take-profit
    +2.0%, stop-loss -3.0%, a 300-bar time stop, and a trailing stop that
    arms at +1.0% and exits on a 0.5% pullback from the peak close.
    No moving-average exit: it sells rebounds too early.
  * A full-size round trip costs about 26 bps in fees, slippage and price
    impact, so entries demand a dip worth far more than that.

Determinism: no randomness, no wall clock, no network, no environment, no
files outside /bot. All thresholds are integer basis points compared with
integer arithmetic; RSI uses Wilder smoothing on the same closes every run.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- tuned parameters (grid-searched on the kit's dev window; see tuning log) ---
SMA_BARS = 60            # trailing average the dip is measured against
LOW_BARS = 30            # trailing window for the bounce-confirmation low
RSI_BARS = 14            # Wilder RSI period
DIP_BPS = 70             # buy only >= 0.70% below the 60-bar SMA
RSI_MAX = 40             # buy only while RSI(14) is below 40
BOUNCE_BPS = 5           # buy only >= 0.05% above the trailing 30-bar low
TAKE_PROFIT_BPS = 200    # exit at +2.0% from the entry fill
STOP_LOSS_BPS = 300      # exit at -3.0% from the entry fill
TIME_STOP_BARS = 300     # exit after 300 bars held, whatever the price
TRAIL_ARM_BPS = 100      # trailing stop arms once +1.0% above entry
TRAIL_PULL_BPS = 50      # ... then exits on a 0.5% pullback from the peak
MAX_SLIPPAGE_BPS = 1000  # 10%: fills move ~0.3% at most, so gaps never reject


class DipBuyer:
    """Keeps the bar history and the open position's entry, in bar time."""

    def __init__(self):
        self.closes = []        # close prices (ints, price_scale 8)
        self.lows = []          # low prices (ints)
        self.sma_sum = 0        # running sum of the last SMA_BARS closes
        self.rsi_gain = None    # Wilder-smoothed average gain
        self.rsi_loss = None    # Wilder-smoothed average loss
        self.prev_close = None
        self.entry_price = None  # fill price of the open position, or None
        self.entry_seq = None    # seq of the batch that carried the fill
        self.peak = None         # highest close seen since entry
        self.pending_buy_id = None

    # -- indicators -------------------------------------------------------
    def _rsi(self):
        if self.rsi_loss is None:
            return None
        if self.rsi_loss == 0:
            return 100.0
        rs = self.rsi_gain / self.rsi_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _sma(self):
        if len(self.closes) < SMA_BARS:
            return None
        return self.sma_sum

    def _trailing_low(self):
        if len(self.lows) < LOW_BARS:
            return None
        return min(self.lows[-LOW_BARS:])

    def _note_bar(self, bar):
        close = int(bar["c"])
        low = int(bar["l"])
        self.closes.append(close)
        self.lows.append(low)
        self.sma_sum += close
        if len(self.closes) > SMA_BARS:
            self.sma_sum -= self.closes[-SMA_BARS - 1]
        # Wilder RSI(14): seed with the mean gain/loss of the first 14
        # one-minute changes, then smooth every later change. Seeding on
        # the 15th close mirrors the standard definition (rsi[14] is the
        # seed value, rsi[i > 14] the smoothed value).
        if self.prev_close is not None:
            change = close - self.prev_close
            gain = change if change > 0 else 0
            loss = -change if change < 0 else 0
            if len(self.closes) == RSI_BARS + 1:
                gains = []
                losses = []
                for i in range(1, RSI_BARS + 1):
                    d = self.closes[i] - self.closes[i - 1]
                    gains.append(d if d > 0 else 0)
                    losses.append(-d if d < 0 else 0)
                self.rsi_gain = sum(gains) / RSI_BARS
                self.rsi_loss = sum(losses) / RSI_BARS
            elif len(self.closes) > RSI_BARS + 1:
                self.rsi_gain = (self.rsi_gain * (RSI_BARS - 1) + gain) / RSI_BARS
                self.rsi_loss = (self.rsi_loss * (RSI_BARS - 1) + loss) / RSI_BARS
        self.prev_close = close

    # -- decisions --------------------------------------------------------
    def on_batch(self, state, batch):
        for bar in batch["bars"]:
            if bar["symbol"] == SYMBOL:
                self._note_bar(bar)
        # Learn the entry fill price from this batch's fills.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
                continue
            if fill["side"] == "buy" and fill["client_id"] == self.pending_buy_id:
                self.entry_price = int(fill["price"])
                self.entry_seq = state.seq
                self.peak = self.entry_price
                self.pending_buy_id = None
            elif fill["side"] == "sell":
                self.entry_price = None
                self.entry_seq = None
                self.peak = None

        if state.warming_up:
            return []
        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding is not None else 0
        close = state.close(SYMBOL)
        if close is None:
            return []

        if qty > 0:
            return self._exit_orders(state, close, qty)
        return self._entry_orders(state, close)

    def _exit_orders(self, state, close, qty):
        if self.entry_price is None:
            # Position opened before we saw its fill (should not happen);
            # hold it and let the time stop below use the first close seen.
            self.entry_price = close
            self.entry_seq = state.seq
            self.peak = close
        if close > self.peak:
            self.peak = close
        entry = self.entry_price
        # All comparisons in integer basis points, no floats.
        take_profit = close * 10000 >= entry * (10000 + TAKE_PROFIT_BPS)
        stop_loss = close * 10000 <= entry * (10000 - STOP_LOSS_BPS)
        time_stop = (state.seq - self.entry_seq) >= TIME_STOP_BARS
        armed = close * 10000 >= entry * (10000 + TRAIL_ARM_BPS)
        pulled_back = close * 10000 <= self.peak * (10000 - TRAIL_PULL_BPS)
        if take_profit or stop_loss or time_stop or (armed and pulled_back):
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def _entry_orders(self, state, close):
        sma_sum = self._sma()
        low = self._trailing_low()
        rsi = self._rsi()
        if sma_sum is None or low is None or rsi is None:
            return []
        # Dip: close >= 0.70% below the 60-bar SMA.
        # sma_sum is the sum of the last SMA_BARS closes, so the average is
        # sma_sum / SMA_BARS; compare without dividing.
        deep_enough = close * 10000 * SMA_BARS <= sma_sum * (10000 - DIP_BPS)
        oversold = rsi < RSI_MAX
        bounced = close * 10000 >= low * (10000 + BOUNCE_BPS)
        if deep_enough and oversold and bounced:
            cid = state.client_id()
            self.pending_buy_id = cid
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": state.cash,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS,
                     "client_id": cid}]
        return []


def main():
    bot = DipBuyer()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
