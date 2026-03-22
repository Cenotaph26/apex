"""
Risk Manager — position sizing, drawdown control, correlation filter.

Position sizing fix:
  qty = risk_amount / stop_distance_in_price
  BUT also capped by: notional = qty * price ≤ max_notional_per_trade
  AND rounded to symbol's stepSize.

Futures lot size reference (testnet.binancefuture.com):
  Symbol         minQty   stepSize  pricePrecision
  BTCUSDT        0.001    0.001     2
  ETHUSDT        0.001    0.001     2
  SOLUSDT        1        1         3
  BNBUSDT        0.01     0.01      3
  XRPUSDT        1        1         4
  ADAUSDT        1        1         4
  DOGEUSDT       1        1         5
  AVAXUSDT       0.1      0.1       3
  LINKUSDT       0.01     0.01      3
  DOTUSDT        0.1      0.1       3
  UNIUSDT        0.01     0.01      3
  ATOMUSDT       0.01     0.01      3
  LTCUSDT        0.001    0.001     2
  NEARUSDT       1        1         4
  APTUSDT        0.1      0.1       3
  ARBUSDT        1        1         5
  OPUSDT         1        1         4
  INJUSDT        0.1      0.1       3
  SUIUSDT        1        1         5
  MATICUSDT      1        1         5
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
    "BTCUSDT":   (0.001, 0.001, 2),
    "ETHUSDT":   (0.001, 0.001, 2),
    "SOLUSDT":   (1.0,   1.0,   3),
    "BNBUSDT":   (0.01,  0.01,  3),
    "XRPUSDT":   (1.0,   1.0,   4),
    "ADAUSDT":   (1.0,   1.0,   4),
    "DOGEUSDT":  (1.0,   1.0,   5),
    "AVAXUSDT":  (0.1,   0.1,   3),
    "LINKUSDT":  (0.01,  0.01,  3),
    "MATICUSDT": (1.0,   1.0,   5),
    "DOTUSDT":   (0.1,   0.1,   3),
    "UNIUSDT":   (0.01,  0.01,  3),
    "ATOMUSDT":  (0.01,  0.01,  3),
    "LTCUSDT":   (0.001, 0.001, 2),
    "NEARUSDT":  (1.0,   1.0,   4),
    "APTUSDT":   (0.1,   0.1,   3),
    "ARBUSDT":   (1.0,   1.0,   5),
    "OPUSDT":    (1.0,   1.0,   4),
    "INJUSDT":   (0.1,   0.1,   3),
    "SUIUSDT":   (1.0,   1.0,   5),
}

# Max notional per trade in USDT (safety cap)
MAX_NOTIONAL_USDT = 500.0


def _round_step(value: float, step: float) -> float:
    """Round value down to nearest step."""
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
        trs.append(max(c.high - c.low,
                       abs(c.high - p.close),
                       abs(c.low  - p.close)))
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
    symbol:    str
    direction: str       # 'long' | 'short'
    entry_price: float
    qty:       float
    stop_loss: float
    tp1:       float
    tp2:       float
    tp3:       float
    tp1_hit:   bool = False
    tp2_hit:   bool = False
    order_id:  str  = ""
    sl_order_id: str = ""
    tp1_order_id: str = ""
    pnl:       float = 0.0


@dataclass
class RiskState:
    starting_balance: float
    current_balance:  float
    peak_balance:     float
    positions: dict[str, Position] = field(default_factory=dict)
    closed_pnl: float = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_balance == 0:
            return 0.0
        dd = (self.peak_balance - self.current_balance) / self.peak_balance * 100
        return max(0.0, dd)

    @property
    def daily_pnl(self) -> float:
        return self.closed_pnl + sum(p.pnl for p in self.positions.values())


class RiskManager:
    def __init__(self, initial_balance: float = 1000.0):
        self.state = RiskState(
            starting_balance=initial_balance,
            current_balance=initial_balance,
            peak_balance=initial_balance,
        )

    def can_open(
        self,
        symbol: str,
        direction: str,
        buffers: dict[str, CandleBuffer],
    ) -> tuple[bool, str]:
        if symbol in self.state.positions:
            return False, f"already have position in {symbol}"
        if len(self.state.positions) >= settings.max_open_positions:
            return False, f"max positions reached ({settings.max_open_positions})"
        if self.state.drawdown_pct >= settings.max_drawdown_pct:
            return False, f"drawdown {self.state.drawdown_pct:.1f}% ≥ {settings.max_drawdown_pct}%"

        open_risk = len(self.state.positions) * settings.risk_per_trade_pct
        if open_risk + settings.risk_per_trade_pct > settings.max_portfolio_risk_pct:
            return False, "portfolio risk cap reached"

        buf = buffers.get(symbol)
        if buf:
            closes_new = [c.close for c in buf.closed]
            for open_sym, _ in self.state.positions.items():
                ob = buffers.get(open_sym)
                if ob:
                    closes_old = [c.close for c in ob.closed]
                    corr = _correlation(closes_new, closes_old)
                    if abs(corr) > settings.correlation_threshold:
                        return False, f"high correlation with {open_sym} ({corr:.2f})"

        return True, "ok"

    def position_size(
        self,
        symbol: str,
        entry_price: float,
        direction: str,
        buf: CandleBuffer,
    ) -> tuple[float, float, float]:
        """
        Returns (qty, stop_loss_price, tp1_price).
        qty is correctly rounded to stepSize and capped by notional.
        """
        info = SYMBOL_INFO.get(symbol, (1.0, 1.0, 4))
        min_qty, step_size, _ = info

        candles = buf.closed
        atr = _atr(candles)
        if atr == 0 or atr < entry_price * 0.0001:
            atr = entry_price * 0.005  # fallback 0.5%

        sl_distance = atr * settings.atr_sl_multiplier
        # Minimum SL distance: 0.1% of price
        sl_distance = max(sl_distance, entry_price * 0.001)

        if direction == "long":
            stop_loss = entry_price - sl_distance
            tp1 = entry_price * (1 + settings.tp1_pct / 100)
        else:
            stop_loss = entry_price + sl_distance
            tp1 = entry_price * (1 - settings.tp1_pct / 100)

        # Risk-based qty
        risk_usdt = self.state.current_balance * settings.risk_per_trade_pct / 100
        qty_risk  = risk_usdt / sl_distance

        # Notional cap: don't open position larger than MAX_NOTIONAL_USDT
        qty_cap = MAX_NOTIONAL_USDT / entry_price

        qty = min(qty_risk, qty_cap)

        # Round down to stepSize
        qty = _round_step(qty, step_size)

        # Enforce minimum
        if qty < min_qty:
            qty = min_qty

        log.info(
            "Position size %s: entry=%.4f atr=%.4f sl_dist=%.4f "
            "risk_usdt=%.2f qty_risk=%.4f qty_cap=%.4f qty=%.4f notional=%.2f",
            symbol, entry_price, atr, sl_distance,
            risk_usdt, qty_risk, qty_cap, qty, qty * entry_price,
        )

        return qty, round(stop_loss, 6), round(tp1, 6)

    def open_position(self, pos: Position):
        self.state.positions[pos.symbol] = pos
        log.info("OPENED %s %s qty=%.4f entry=%.4f sl=%.4f notional=%.2f USDT",
                 pos.symbol, pos.direction, pos.qty,
                 pos.entry_price, pos.stop_loss,
                 pos.qty * pos.entry_price)

    def close_position(self, symbol: str, exit_price: float, reason: str = ""):
        pos = self.state.positions.pop(symbol, None)
        if pos is None:
            return 0.0
        if pos.direction == "long":
            pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            pnl = (pos.entry_price - exit_price) * pos.qty

        self.state.closed_pnl += pnl
        self.state.current_balance += pnl
        if self.state.current_balance > self.state.peak_balance:
            self.state.peak_balance = self.state.current_balance

        log.info("CLOSED %s [%s] exit=%.4f pnl=%+.2f balance=%.2f",
                 symbol, reason or "manual", exit_price, pnl,
                 self.state.current_balance)
        return pnl

    def update_pnl(self, symbol: str, current_price: float):
        pos = self.state.positions.get(symbol)
        if pos is None:
            return
        if pos.direction == "long":
            pos.pnl = (current_price - pos.entry_price) * pos.qty
        else:
            pos.pnl = (pos.entry_price - current_price) * pos.qty

    def position_count(self) -> int:
        return len(self.state.positions)
