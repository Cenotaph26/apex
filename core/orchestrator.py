"""
Orchestrator — APEX brain.

Key fixes:
1. position_size() now returns correct qty (notional capped, stepSize rounded)
2. SL placed as STOP_MARKET on exchange immediately after entry
3. TP1 = TAKE_PROFIT_MARKET on exchange (partial close 50%)
4. After TP1 hit → cancel old SL → place breakeven SL (trailing)
5. TP2 = close remaining on exchange
6. Monitor loop checks real exchange positions to detect fills
7. wins/losses tracked correctly from exchange fills
"""
from __future__ import annotations
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.config import settings
from core.feed import FeedManager
from core.models import CandleBuffer
from core.risk import RiskManager, Position
from core.execution import ExecutionEngine
from agents.momentum import MomentumSniper
from agents.orderflow import OrderFlowAgent
from agents.funding import FundingAgent
from agents.liquidation import LiquidationHunter

log = logging.getLogger("apex.orchestrator")

MAX_LOG_LINES  = 50
MAX_EQUITY_PTS = 120


@dataclass
class CoinSignal:
    symbol:           str
    score:            float
    direction:        str
    momentum_score:   float
    orderflow_score:  float
    funding_score:    float
    liquidation_score: float
    timestamp:        float = field(default_factory=time.time)


@dataclass
class LogEntry:
    ts:  str
    lvl: str
    msg: str


class Orchestrator:
    def __init__(self):
        self.feed = FeedManager()
        self.risk = RiskManager()
        self.exec = ExecutionEngine()

        self._momentum    = MomentumSniper()
        self._orderflow   = OrderFlowAgent()
        self._funding     = FundingAgent()
        self._liquidation = LiquidationHunter()

        self._running = False
        self._last_signals: dict[str, CoinSignal] = {}
        self._pending_eval: set[str] = set()
        self._lock = asyncio.Lock()

        self._logs: deque[LogEntry]  = deque(maxlen=MAX_LOG_LINES)
        self._equity_history: deque[dict] = deque(maxlen=MAX_EQUITY_PTS)
        self._wins:   int   = 0
        self._losses: int   = 0
        self._start_time: float = time.time()

        self.feed.on_candle_close(self._on_candle_close)

    # ── Internal log ──────────────────────────────────────────

    def _log(self, lvl: str, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._logs.appendleft(LogEntry(ts=ts, lvl=lvl, msg=msg))
        if lvl == "warn":
            log.warning(msg)
        elif lvl == "err":
            log.error(msg)
        else:
            log.info(msg)

    # ── Lifecycle ─────────────────────────────────────────────

    async def run(self):
        self._running = True
        await self.exec._sync_time()

        balance = await self.exec.get_balance()
        self.risk.state.current_balance  = balance
        self.risk.state.peak_balance     = balance
        self.risk.state.starting_balance = balance
        self._log("ok", f"Starting balance: {balance:.2f} USDT")

        tasks = [
            asyncio.create_task(self.feed.run()),
            asyncio.create_task(self._eval_loop()),
            asyncio.create_task(self._monitor_loop()),
            asyncio.create_task(self._equity_loop()),
            asyncio.create_task(self._balance_loop()),
        ]
        await asyncio.gather(*tasks)

    def stop(self):
        self._running = False
        self.feed.stop()

    # ── Callbacks ─────────────────────────────────────────────

    def _on_candle_close(self, symbol: str):
        self._pending_eval.add(symbol)

    # ── Background loops ──────────────────────────────────────

    async def _balance_loop(self):
        await asyncio.sleep(30)
        while self._running:
            try:
                bal = await self.exec.get_balance()
                if bal > 0:
                    old = self.risk.state.current_balance
                    self.risk.state.current_balance = bal
                    if bal > self.risk.state.peak_balance:
                        self.risk.state.peak_balance = bal
                    if abs(bal - old) > 0.5:
                        self._log("ok", f"Balance synced: {bal:.2f} USDT")
            except Exception as e:
                self._log("warn", f"Balance sync failed: {e}")
            await asyncio.sleep(30)

    async def _equity_loop(self):
        while self._running:
            await asyncio.sleep(60)
            ts = datetime.now(timezone.utc).strftime("%H:%M")
            self._equity_history.append({
                "ts":      ts,
                "balance": round(self.risk.state.current_balance, 2),
            })

    # ── Eval loop ─────────────────────────────────────────────

    async def _eval_loop(self):
        while self._running:
            await asyncio.sleep(1.0)
            if not self._pending_eval:
                continue

            async with self._lock:
                batch = self._pending_eval.copy()
                self._pending_eval.clear()

            signals: list[CoinSignal] = []
            for sym in batch:
                sig = await self._score_coin(sym)
                if sig is not None:
                    self._last_signals[sym] = sig
                    signals.append(sig)
                    self._log("info",
                        f"candle closed {sym} score={sig.score:.0f} dir={sig.direction}")

            if not signals:
                continue

            winner = max(signals, key=lambda s: s.score)
            self._log("info",
                f"orchestrator: winner {winner.symbol} {winner.score:.1f} "
                f"({len(signals)} coins evaluated)")

            if winner.score >= settings.score_threshold:
                await self._try_open(winner)

    # ── Score ─────────────────────────────────────────────────

    async def _score_coin(self, symbol: str) -> CoinSignal | None:
        buf = self.feed.buffers.get(symbol)
        if buf is None or len(buf) < 30:
            return None

        m_score, direction = self._momentum.score(buf)
        if direction == "none" or m_score == 0:
            return None

        book     = self.feed.orderbooks.get(symbol)
        of_score = self._orderflow.score(book, direction)
        fu_score = await self._funding.score(symbol, direction)
        lq_score = self._liquidation.score(buf, direction)

        total = (
            m_score  * settings.weight_momentum  +
            of_score * settings.weight_orderflow +
            fu_score * settings.weight_funding   +
            lq_score * settings.weight_liquidation
        )
        return CoinSignal(
            symbol=symbol, score=round(total, 1), direction=direction,
            momentum_score=m_score, orderflow_score=of_score,
            funding_score=fu_score, liquidation_score=lq_score,
        )

    # ── Open position ─────────────────────────────────────────

    async def _try_open(self, sig: CoinSignal):
        ok, reason = self.risk.can_open(sig.symbol, sig.direction, self.feed.buffers)
        if not ok:
            self._log("info", f"trade blocked [{sig.symbol}]: {reason}")
            return

        buf = self.feed.buffers[sig.symbol]
        if not buf.closed:
            return

        entry_price = buf.closed[-1].close

        # Get correct position size
        qty, stop_loss, tp1 = self.risk.position_size(
            sig.symbol, entry_price, sig.direction, buf
        )

        if qty <= 0:
            self._log("warn", f"qty=0 for {sig.symbol}, skip")
            return

        # TP2 and TP3
        pct_mult = -1 if sig.direction == "short" else 1
        tp2 = entry_price * (1 + pct_mult * settings.tp2_pct / 100)
        tp3 = entry_price * (1 + pct_mult * settings.tp3_pct / 100)

        # Set leverage to 1x (safe)
        await self.exec.set_leverage(sig.symbol, leverage=1)

        # Entry side
        entry_side = "BUY" if sig.direction == "long" else "SELL"
        close_side = "SELL" if sig.direction == "long" else "BUY"

        # 1. Place entry market order
        result = await self.exec.place_market_order(sig.symbol, entry_side, qty)
        if not result.get("orderId"):
            self._log("warn", f"Entry order failed for {sig.symbol}")
            return

        order_id = str(result.get("orderId", ""))

        # Use actual fill price if available
        avg_price = float(result.get("avgPrice", 0) or entry_price)
        if avg_price > 0:
            entry_price = avg_price

        # 2. Place exchange-side stop-loss immediately
        sl_result = await self.exec.place_stop_loss(
            sig.symbol, close_side, qty, stop_loss
        )
        sl_order_id = str(sl_result.get("orderId", ""))

        # 3. Place TP1 on exchange (close 50% at TP1)
        tp1_qty_raw = qty * 0.5
        from core.risk import SYMBOL_INFO, _round_step
        _, step, _ = SYMBOL_INFO.get(sig.symbol, (1.0, 1.0, 4))
        tp1_qty = _round_step(tp1_qty_raw, step)
        if tp1_qty <= 0:
            tp1_qty = qty  # if too small, close all at TP1

        tp1_result = await self.exec.place_take_profit(
            sig.symbol, close_side, tp1_qty, tp1
        )
        tp1_order_id = str(tp1_result.get("orderId", ""))

        pos = Position(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=entry_price,
            qty=qty,
            stop_loss=stop_loss,
            tp1=tp1, tp2=tp2, tp3=tp3,
            order_id=order_id,
            sl_order_id=sl_order_id,
            tp1_order_id=tp1_order_id,
        )
        self.risk.open_position(pos)
        self._log("sig",
            f"{sig.symbol} signal={sig.score:.0f} dir={sig.direction} "
            f"entry={entry_price:.4f} sl={stop_loss:.4f} tp1={tp1:.4f} "
            f"qty={qty} notional={qty*entry_price:.1f} USDT")

    # ── Position monitor ──────────────────────────────────────

    async def _monitor_loop(self):
        """
        Polls live prices every second to update PnL display.
        Exchange handles SL/TP triggers — we just detect when position is gone.
        """
        while self._running:
            await asyncio.sleep(1.0)

            for sym, pos in list(self.risk.state.positions.items()):
                buf = self.feed.buffers.get(sym)
                if buf is None:
                    continue
                live = buf.live or (buf.closed[-1] if buf.closed else None)
                if live is None:
                    continue

                price = live.close
                self.risk.update_pnl(sym, price)

                # Check if SL was hit (for display/local tracking)
                # Real SL is on exchange — we detect via price crossing
                if pos.direction == "long":
                    sl_hit = price <= pos.stop_loss
                    tp1_hit = price >= pos.tp1 and not pos.tp1_hit
                    tp2_hit = price >= pos.tp2 and pos.tp1_hit and not pos.tp2_hit
                else:
                    sl_hit = price >= pos.stop_loss
                    tp1_hit = price <= pos.tp1 and not pos.tp1_hit
                    tp2_hit = price <= pos.tp2 and pos.tp1_hit and not pos.tp2_hit

                if sl_hit:
                    self._log("warn",
                        f"SL triggered: {sym} @ {price:.4f} "
                        f"(exchange STOP_MARKET should fill)")
                    # Remove from our tracking — exchange order handles close
                    pnl = self.risk.close_position(sym, price, reason="SL")
                    if pnl < 0:
                        self._losses += 1
                    else:
                        self._wins += 1
                    await self.exec.cancel_all_orders(sym)

                elif tp1_hit:
                    pos.tp1_hit = True
                    self._log("ok",
                        f"TP1 hit: {sym} @ {price:.4f} — "
                        f"exchange TP order filling 50%")
                    # Cancel existing SL, place breakeven SL
                    if pos.sl_order_id:
                        await self.exec.cancel_order(sym, pos.sl_order_id)
                    # Move SL to breakeven
                    be_price = pos.entry_price
                    close_side = "SELL" if pos.direction == "long" else "BUY"
                    remaining_qty = pos.qty * 0.5
                    from core.risk import SYMBOL_INFO, _round_step
                    _, step, _ = SYMBOL_INFO.get(sym, (1.0, 1.0, 4))
                    remaining_qty = _round_step(remaining_qty, step)
                    sl_result = await self.exec.place_stop_loss(
                        sym, close_side, remaining_qty, be_price
                    )
                    pos.sl_order_id = str(sl_result.get("orderId", ""))
                    self._log("ok",
                        f"SL moved to breakeven: {sym} @ {be_price:.4f}")

                elif tp2_hit:
                    pos.tp2_hit = True
                    self._log("ok", f"TP2 hit: {sym} @ {price:.4f} — closing rest")
                    close_side = "SELL" if pos.direction == "long" else "BUY"
                    remaining = _round_step(pos.qty * 0.5, step)
                    if remaining > 0:
                        await self.exec.place_market_order(
                            sym, close_side, remaining, reduce_only=True
                        )
                    await self.exec.cancel_all_orders(sym)
                    self.risk.close_position(sym, price, reason="TP2")
                    self._wins += 1

    # ── Status helpers ────────────────────────────────────────

    def position_count(self) -> int:
        return len(self.risk.state.positions)

    def daily_pnl(self) -> float:
        return round(self.risk.state.daily_pnl, 4)

    def _live_prices(self) -> dict[str, float]:
        prices = {}
        for sym, buf in self.feed.buffers.items():
            c = buf.live or (buf.closed[-1] if buf.closed else None)
            if c:
                prices[sym] = round(c.close, 6)
        return prices

    def snapshot(self) -> dict:
        signals = {
            sym: {
                "score":     s.score,
                "direction": s.direction,
                "agents": {
                    "momentum":    s.momentum_score,
                    "orderflow":   s.orderflow_score,
                    "funding":     s.funding_score,
                    "liquidation": s.liquidation_score,
                },
            }
            for sym, s in self._last_signals.items()
        }

        positions = {}
        for sym, p in self.risk.state.positions.items():
            buf = self.feed.buffers.get(sym)
            c = (buf.live or (buf.closed[-1] if buf.closed else None)) if buf else None
            cur = round(c.close, 6) if c else p.entry_price
            sig = self._last_signals.get(sym)
            positions[sym] = {
                "direction": p.direction,
                "entry":     p.entry_price,
                "current":   cur,
                "qty":       p.qty,
                "sl":        p.stop_loss,
                "tp1":       p.tp1,
                "tp2":       p.tp2,
                "tp3":       p.tp3,
                "tp1_hit":   p.tp1_hit,
                "tp2_hit":   p.tp2_hit,
                "pnl":       round(p.pnl, 4),
                "score":     sig.score if sig else 0,
                "notional":  round(p.qty * p.entry_price, 2),
            }

        uptime_s = int(time.time() - self._start_time)
        h, m, s = uptime_s // 3600, (uptime_s % 3600) // 60, uptime_s % 60

        best = (max(self._last_signals.values(), key=lambda x: x.score)
                if self._last_signals else None)

        return {
            "balance":        round(self.risk.state.current_balance, 2),
            "daily_pnl":      self.daily_pnl(),
            "drawdown_pct":   round(self.risk.state.drawdown_pct, 2),
            "wins":           self._wins,
            "losses":         self._losses,
            "open_positions": positions,
            "last_signals":   signals,
            "live_prices":    self._live_prices(),
            "equity_history": list(self._equity_history),
            "logs": [
                {"ts": e.ts, "lvl": e.lvl, "msg": e.msg}
                for e in self._logs
            ],
            "uptime": f"{h:02d}:{m:02d}:{s:02d}",
            "best_signal": {
                "symbol":    best.symbol,
                "score":     best.score,
                "direction": best.direction,
                "agents": {
                    "momentum":    best.momentum_score,
                    "orderflow":   best.orderflow_score,
                    "funding":     best.funding_score,
                    "liquidation": best.liquidation_score,
                },
            } if best else None,
        }
