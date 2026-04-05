"""
Risk Manager v5

Fixes vs v4:
1. PnL formula corrected — USDT-margined futures:
      pnl = (exit - entry) * qty          ← correct
   was: pnl = (exit - entry) * qty / lev  ← wrong (5x understated at x5 lev)
   Leverage only affects margin requirement, not PnL.

2. Drawdown now includes unrealized PnL from open positions.
   Previously drawdown was based solely on walletBalance (realized),
   meaning a position down 7% unrealized showed 0% drawdown and the
   circuit breaker never fired. Now:
      drawdown = (peak - (wallet + unrealized_pnl)) / peak

3. Dynamic MAX_NOTIONAL_PCT (10% of balance) — replaces hardcoded 500 USDT.

4. qty_risk formula corrected to match PnL formula:
   For USDT-margined futures, risk at SL = sl_distance * qty (no leverage).
   qty = risk_usdt / sl_distance  (leverage only determines how much margin
   is required, not how many contracts fit the risk budget).
"""
from __future__ import annotations
import logging
import math
import statistics
from dataclasses import dataclass, field

from core.config import settings
from core.models import CandleBuffer, Candle

log = logging.getLogger("apex.risk")

# Futures lot precision: (minQty, stepSize, pricePrecision)
SYMBOL_INFO: dict[str, tuple[float, float, int]] = {
    "BTCUSDT":   (0.001, 0.001, 2), "ETHUSDT":   (0.001, 0.001, 2),
    "SOLUSDT":   (1.0,   1.0,   3), "BNBUSDT":   (0.01,  0.01,  3),
    "XRPUSDT":   (1.0,   1.0,   4), "ADAUSDT":   (1.0,   1.0,   4),
    "DOGEUSDT":  (1.0,   1.0,   5), "AVAXUSDT":  (0.1,   0.1,   3),
    "LINKUSDT":  (0.01,  0.01,  3), "MATICUSDT": (1.0,   1.0,   5),
    "DOTUSDT":   (0.1,   0.1,   3), "UNIUSDT":   (0.01,  0.01,  3),
    "ATOMUSDT":  (0.01,  0.01,  3), "LTCUSDT":   (0.001, 0.001, 2),
    "NEARUSDT":  (1.0,   1.0,   4), "APTUSDT":   (0.1,   0.1,   3),
    "ARBUSDT":   (1.0,   1.0,   5), "OPUSDT":    (1.0,   1.0,   4),
    "INJUSDT":   (0.1,   0.1,   3), "SUIUSDT":   (1.0,   1.0,   5),
}

# Max position notional as fraction of account balance per trade.
MAX_NOTIONAL_PCT = 0.10


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, min(len(candles), period + 1)):
        c, p = candles[-i], candles[-i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return statistics.mean(trs) if trs else 0.0


def _correlation(a: list[float], b: list[float], n: int = 20) -> float:
    if len(a) < n + 1 or len(b) < n + 1:
        return 0.0
    ra = [a[-i] / a[-i - 1] - 1 for i in range(1, n + 1)]
    rb = [b[-i] / b[-i - 1] - 1 for i in range(1, n + 1)]
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return 0.0


@dataclass
class Position:
    symbol:       str
    direction:    str
    entry_price:  float
    qty:          float
    stop_loss:    float
    tp1:          float
    tp2:          float
    tp3:          float
    leverage:     int   = 1
    tp1_hit:      bool  = False
    tp2_hit:      bool  = False
    tp3_hit:      bool  = False
    order_id:     str   = ""
    sl_order_id:  str   = ""
    tp1_order_id: str   = ""
    pnl:          float = 0.0
    entry_ts:     int   = 0    # ms timestamp when position opened
    entry_score:  float = 0.0  # sinyal skoru — pozisyon açılırken sabitlenir   # unrealized, updated every tick


@dataclass
class RiskState:
    starting_balance: float
    current_balance:  float   # walletBalance from exchange (realized only)
    peak_balance:     float   # peak of (wallet + unrealized) equity
    positions: dict[str, Position] = field(default_factory=dict)
    closed_pnl: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.pnl for p in self.positions.values())

    @property
    def total_equity(self) -> float:
        """Wallet balance + open unrealized PnL — true account value."""
        return self.current_balance + self.unrealized_pnl

    @property
    def drawdown_pct(self) -> float:
        """
        Drawdown from peak equity, including unrealized losses.
        Uses total_equity so an open position bleeding 7% doesn't hide
        behind walletBalance staying flat.
        """
        if self.peak_balance == 0:
            return 0.0
        return max(0.0, (self.peak_balance - self.total_equity) / self.peak_balance * 100)

    @property
    def daily_pnl(self) -> float:
        return self.closed_pnl + self.unrealized_pnl


class RiskManager:
    def __init__(self, initial_balance: float = 1000.0):
        self.state = RiskState(
            starting_balance=initial_balance,
            current_balance=initial_balance,
            peak_balance=initial_balance,
        )
        self.leverage_map: dict[str, int] = {}

    def get_leverage(self, symbol: str) -> int:
        return self.leverage_map.get(symbol, settings.default_leverage)

    def set_leverage(self, symbol: str, leverage: int):
        leverage = max(1, min(leverage, 20))
        self.leverage_map[symbol] = leverage
        log.info("Leverage set: %s x%d", symbol, leverage)

    def set_global_leverage(self, leverage: int):
        leverage = max(1, min(leverage, 20))
        for sym in SYMBOL_INFO:
            self.leverage_map[sym] = leverage
        settings.default_leverage = leverage
        log.info("Global leverage set: x%d", leverage)

    def can_open(self, symbol: str, direction: str,
                 buffers: dict[str, CandleBuffer],
                 pending_count: int = 0) -> tuple[bool, str]:
        if symbol in self.state.positions:
            return False, f"already have position in {symbol}"
        # Count pending (being opened) + confirmed positions
        total_active = len(self.state.positions) + pending_count
        if total_active >= settings.max_open_positions:
            return False, f"max positions reached ({settings.max_open_positions})"
        if self.state.drawdown_pct >= settings.max_drawdown_pct:
            return False, (
                f"drawdown {self.state.drawdown_pct:.1f}% "
                f">= {settings.max_drawdown_pct}% "
                f"(equity={self.state.total_equity:.2f} peak={self.state.peak_balance:.2f})"
            )
        open_risk = len(self.state.positions) * settings.risk_per_trade_pct
        if open_risk + settings.risk_per_trade_pct > settings.max_portfolio_risk_pct:
            return False, "portfolio risk cap reached"
        buf = buffers.get(symbol)
        if buf:
            closes_new = [c.close for c in buf.closed]
            for open_sym in self.state.positions:
                ob = buffers.get(open_sym)
                if ob:
                    corr = _correlation(closes_new, [c.close for c in ob.closed])
                    if abs(corr) > settings.correlation_threshold:
                        return False, f"high correlation with {open_sym} ({corr:.2f})"
        return True, "ok"

    def position_size(self, symbol: str, entry_price: float, direction: str,
                      buf: CandleBuffer) -> tuple[float, float, float]:
        """
        Returns (qty, stop_loss, tp1).

        For USDT-margined futures:
          actual_loss_at_SL = sl_distance * qty   (leverage doesn't affect this)
          qty = risk_usdt / sl_distance

        The leverage multiplier only determines how much margin is reserved,
        not how many contracts we can buy per dollar of risk.
        """
        info = SYMBOL_INFO.get(symbol, (1.0, 1.0, 4))
        min_qty, step_size, _ = info
        leverage = self.get_leverage(symbol)

        candles = buf.closed
        atr = _atr(candles)
        if atr == 0 or atr < entry_price * 0.0001:
            atr = entry_price * 0.005

        sl_distance = max(atr * settings.atr_sl_multiplier, entry_price * 0.001)

        if direction == "long":
            stop_loss = entry_price - sl_distance
            tp1 = entry_price * (1 + settings.tp1_pct / 100)
        else:
            stop_loss = entry_price + sl_distance
            tp1 = entry_price * (1 - settings.tp1_pct / 100)

        # Risk budget in USDT
        risk_usdt = self.state.current_balance * settings.risk_per_trade_pct / 100

        # Correct formula: qty = risk / sl_distance (no leverage factor)
        qty_risk = risk_usdt / sl_distance

        # Notional cap: don't put more than MAX_NOTIONAL_PCT of balance in one trade
        max_notional = self.state.current_balance * MAX_NOTIONAL_PCT
        qty_cap = max_notional / entry_price   # no leverage factor here either

        qty = _round_step(min(qty_risk, qty_cap), step_size)
        qty = max(qty, min_qty)

        margin_required = qty * entry_price / leverage
        log.info(
            "Position size %s: entry=%.4f sl_dist=%.5f lev=x%d "
            "qty=%.4f risk=%.2f USDT margin=%.2f USDT (cap_notional=%.2f)",
            symbol, entry_price, sl_distance, leverage,
            qty, sl_distance * qty, margin_required, max_notional,
        )

        return qty, round(stop_loss, 8), round(tp1, 8)

    def open_position(self, pos: Position):
        self.state.positions[pos.symbol] = pos
        margin = pos.qty * pos.entry_price / pos.leverage
        log.info("OPENED %s %s lev=x%d qty=%.4f entry=%.4f sl=%.4f margin=%.2f USDT",
                 pos.symbol, pos.direction, pos.leverage,
                 pos.qty, pos.entry_price, pos.stop_loss, margin)

    def close_position(self, symbol: str, exit_price: float, reason: str = "") -> float:
        pos = self.state.positions.pop(symbol, None)
        if pos is None:
            return 0.0

        # Correct USDT-margined futures PnL: no leverage divisor
        if pos.direction == "long":
            pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            pnl = (pos.entry_price - exit_price) * pos.qty

        self.state.closed_pnl += pnl
        # Update wallet locally for immediate dashboard feedback.
        # _balance_loop overwrites with real walletBalance within 30s.
        self.state.current_balance += pnl
        # Update peak with realized equity
        if self.state.current_balance > self.state.peak_balance:
            self.state.peak_balance = self.state.current_balance
        log.info("CLOSED %s [%s] exit=%.4f pnl=%+.4f USDT balance=%.2f",
                 symbol, reason or "manual", exit_price, pnl,
                 self.state.current_balance)
        return pnl

    def update_pnl(self, symbol: str, current_price: float):
        """Update unrealized PnL and peak equity for drawdown calculation."""
        pos = self.state.positions.get(symbol)
        if pos is None:
            return

        # Correct USDT-margined futures unrealized PnL
        if pos.direction == "long":
            pos.pnl = (current_price - pos.entry_price) * pos.qty
        else:
            pos.pnl = (pos.entry_price - current_price) * pos.qty

        # Keep peak updated with unrealized gains so drawdown reflects true high
        equity = self.state.total_equity
        if equity > self.state.peak_balance:
            self.state.peak_balance = equity

    def position_count(self) -> int:
        return len(self.state.positions)
