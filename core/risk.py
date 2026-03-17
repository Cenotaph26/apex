"""
RiskManager — veto authority over all trade decisions.

Responsibilities:
- Position sizing (ATR-based)
- Max drawdown circuit breaker
- Max open positions cap
- Correlation filter (don't open correlated coins simultaneously)
"""
from __future__ import annotations
import logging
import statistics
from dataclasses import dataclass, field

from core.config import settings
from core.models import CandleBuffer, Candle

log = logging.getLogger("apex.risk")


@dataclass
class Position:
    symbol: str
    direction: str       # 'long' | 'short'
    entry_price: float
    qty: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    order_id: str = ""
    pnl: float = 0.0


@dataclass
class RiskState:
    starting_balance: float
    current_balance: float
    peak_balance: float
    positions: dict[str, Position] = field(default_factory=dict)
    closed_pnl: float = 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_balance == 0:
            return 0.0
        return (self.peak_balance - self.current_balance) / self.peak_balance * 100

    @property
    def daily_pnl(self) -> float:
        open_pnl = sum(p.pnl for p in self.positions.values())
        return self.closed_pnl + open_pnl


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, min(len(candles), period + 1)):
        c, p = candles[-i], candles[-i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return statistics.mean(trs) if trs else 0.0


def _correlation(a: list[float], b: list[float], n: int = 20) -> float:
    """Pearson correlation of last N close returns."""
    if len(a) < n + 1 or len(b) < n + 1:
        return 0.0
    ra = [a[-i] / a[-i - 1] - 1 for i in range(1, n + 1)]
    rb = [b[-i] / b[-i - 1] - 1 for i in range(1, n + 1)]
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return 0.0


class RiskManager:
    def __init__(self, initial_balance: float = 1000.0):
        self.state = RiskState(
            starting_balance=initial_balance,
            current_balance=initial_balance,
            peak_balance=initial_balance,
        )

    # ── Public API ────────────────────────────────────────────

    def can_open(
        self,
        symbol: str,
        direction: str,
        buffers: dict[str, CandleBuffer],
    ) -> tuple[bool, str]:
        """Returns (ok, reason)."""

        # 1. Already in this coin
        if symbol in self.state.positions:
            return False, f"already have position in {symbol}"

        # 2. Max positions
        if len(self.state.positions) >= settings.max_open_positions:
            return False, f"max positions reached ({settings.max_open_positions})"

        # 3. Circuit breaker
        if self.state.drawdown_pct >= settings.max_drawdown_pct:
            return False, f"drawdown {self.state.drawdown_pct:.1f}% ≥ {settings.max_drawdown_pct}%"

        # 4. Portfolio risk cap
        open_risk_pct = len(self.state.positions) * settings.risk_per_trade_pct
        if open_risk_pct + settings.risk_per_trade_pct > settings.max_portfolio_risk_pct:
            return False, "portfolio risk cap reached"

        # 5. Correlation filter
        buf = buffers.get(symbol)
        if buf:
            closes_new = [c.close for c in buf.closed]
            for open_sym, pos in self.state.positions.items():
                ob = buffers.get(open_sym)
                if ob:
                    closes_old = [c.close for c in ob.closed]
                    corr = _correlation(closes_new, closes_old)
                    if abs(corr) > settings.correlation_threshold:
                        return False, (
                            f"high correlation with {open_sym} ({corr:.2f})"
                        )

        return True, "ok"

    def position_size(
        self,
        symbol: str,
        entry_price: float,
        buf: CandleBuffer,
    ) -> tuple[float, float]:
        """
        Returns (qty, stop_loss_price) based on ATR.
        Risk per trade = risk_per_trade_pct % of current balance.
        """
        candles = buf.closed
        atr = _atr(candles)
        if atr == 0:
            atr = entry_price * 0.005  # fallback: 0.5%

        stop_distance = atr * settings.atr_sl_multiplier
        stop_loss = entry_price - stop_distance  # for long; caller flips for short

        risk_amount = self.state.current_balance * settings.risk_per_trade_pct / 100
        qty = risk_amount / stop_distance if stop_distance > 0 else 0.0

        return round(qty, 6), round(stop_loss, 4)

    def open_position(self, pos: Position):
        self.state.positions[pos.symbol] = pos
        log.info("Position OPENED: %s %s qty=%.4f entry=%.4f sl=%.4f",
                 pos.symbol, pos.direction, pos.qty, pos.entry_price, pos.stop_loss)

    def close_position(self, symbol: str, exit_price: float):
        pos = self.state.positions.pop(symbol, None)
        if pos is None:
            return
        if pos.direction == "long":
            pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            pnl = (pos.entry_price - exit_price) * pos.qty

        self.state.closed_pnl += pnl
        self.state.current_balance += pnl
        if self.state.current_balance > self.state.peak_balance:
            self.state.peak_balance = self.state.current_balance

        log.info("Position CLOSED: %s exit=%.4f pnl=%.4f balance=%.2f",
                 symbol, exit_price, pnl, self.state.current_balance)

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
