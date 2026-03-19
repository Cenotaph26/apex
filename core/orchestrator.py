"""
Orchestrator — the brain of APEX.

Every time a candle closes on any coin:
1. Run all agents for that coin → coin_score
2. Compare all coin scores
3. If winner > threshold AND risk OK → open position
4. Manage open positions (TP / SL check on every tick)
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
    symbol: str
    score: float
    direction: str
    momentum_score: float
    orderflow_score: float
    funding_score: float
    liquidation_score: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class LogEntry:
    ts: str
    lvl: str   # info | ok | warn | err | sig
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

        # Dashboard state
        self._logs: deque[LogEntry] = deque(maxlen=MAX_LOG_LINES)
        self._equity_history: deque[dict] = deque(maxlen=MAX_EQUITY_PTS)
        self._wins: int = 0
        self._losses: int = 0
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

        await self.exec._sync_time()   # sync clock before first signed request
        balance = await self.exec.get_balance()
        # Reset session: peak = current balance so drawdown starts fresh each run
        self.risk.state.current_balance  = balance
        self.risk.state.peak_balance     = balance   # session peak starts here
        self.risk.state.starting_balance = balance
        self._log("ok", f"Starting balance: {balance:.2f} USDT")

        feed_task    = asyncio.create_task(self.feed.run())
        eval_task    = asyncio.create_task(self._eval_loop())
        monitor_task = asyncio.create_task(self._monitor_loop())
        equity_task  = asyncio.create_task(self._equity_loop())
        balance_task = asyncio.create_task(self._balance_loop())

        await asyncio.gather(feed_task, eval_task, monitor_task, equity_task, balance_task)

    def stop(self):
        self._running = False
        self.feed.stop()

    # ── Callbacks ─────────────────────────────────────────────

    def _on_candle_close(self, symbol: str):
        self._pending_eval.add(symbol)

    # ── Balance refresh (every 30s from testnet REST) ────────

    async def _balance_loop(self):
        """Periodically syncs real balance from testnet REST API."""
        await asyncio.sleep(30)  # let startup settle first
        while self._running:
            try:
                bal = await self.exec.get_balance()
                if bal > 0:
                    old_bal = self.risk.state.current_balance
                    self.risk.state.current_balance = bal
                    if self.risk.state.peak_balance == 0 or bal > self.risk.state.peak_balance:
                        self.risk.state.peak_balance = bal
                    if abs(bal - old_bal) > 0.01:
                        self._log("ok", f"Balance synced: {bal:.2f} USDT")
            except Exception as e:
                self._log("warn", f"Balance sync failed: {e}")
            await asyncio.sleep(30)

    # ── Equity snapshot (every 60s) ───────────────────────────

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

    # ── Score one coin ────────────────────────────────────────

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
            symbol=symbol,
            score=round(total, 1),
            direction=direction,
            momentum_score=m_score,
            orderflow_score=of_score,
            funding_score=fu_score,
            liquidation_score=lq_score,
        )

    # ── Trade execution ───────────────────────────────────────

    async def _try_open(self, sig: CoinSignal):
        ok, reason = self.risk.can_open(sig.symbol, sig.direction, self.feed.buffers)
        if not ok:
            self._log("info", f"trade blocked [{sig.symbol}]: {reason}")
            return

        buf = self.feed.buffers[sig.symbol]
        candles = buf.closed
        if not candles:
            return

        entry_price = candles[-1].close
        qty, stop_loss = self.risk.position_size(sig.symbol, entry_price, buf)

        if qty <= 0:
            self._log("warn", f"qty=0 for {sig.symbol}, skipping")
            return

        if sig.direction == "long":
            tp1 = entry_price * (1 + settings.tp1_pct / 100)
            tp2 = entry_price * (1 + settings.tp2_pct / 100)
            tp3 = entry_price * (1 + settings.tp3_pct / 100)
        else:
            tp1 = entry_price * (1 - settings.tp1_pct / 100)
            tp2 = entry_price * (1 - settings.tp2_pct / 100)
            tp3 = entry_price * (1 - settings.tp3_pct / 100)
            stop_loss = entry_price + (entry_price - stop_loss)

        side = "BUY" if sig.direction == "long" else "SELL"
        result = await self.exec.place_market_order(sig.symbol, side, qty)
        order_id = str(result.get("orderId", ""))

        pos = Position(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=entry_price,
            qty=qty,
            stop_loss=stop_loss,
            tp1=tp1, tp2=tp2, tp3=tp3,
            order_id=order_id,
        )
        self.risk.open_position(pos)
        self._log("sig",
            f"{sig.symbol} signal score {sig.score:.0f} → position opened "
            f"entry={entry_price:.4f} sl={stop_loss:.4f}")

    # ── Position monitor ──────────────────────────────────────

    async def _monitor_loop(self):
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

                if pos.direction == "long":
                    if price <= pos.stop_loss:
                        self._log("warn", f"STOP LOSS hit: {sym} @ {price:.4f}")
                        await self.exec.place_market_order(sym, "SELL", pos.qty)
                        self.risk.close_position(sym, price)
                        self._losses += 1

                    elif price >= pos.tp1 and not pos.tp1_hit:
                        pos.tp1_hit = True
                        partial = round(pos.qty * 0.40, 4)
                        await self.exec.place_market_order(sym, "SELL", partial)
                        pos.qty -= partial
                        self._log("ok", f"{sym} TP1 hit @ {price:.4f} — sold 40%")

                    elif price >= pos.tp2 and pos.tp1_hit and not pos.tp2_hit:
                        pos.tp2_hit = True
                        partial = round(pos.qty * 0.583, 4)
                        await self.exec.place_market_order(sym, "SELL", partial)
                        pos.qty -= partial
                        self._log("ok", f"{sym} TP2 hit @ {price:.4f} — sold 35%")

                    elif price >= pos.tp3 and pos.tp2_hit:
                        self._log("ok", f"{sym} TP3 hit @ {price:.4f} — closing rest")
                        await self.exec.place_market_order(sym, "SELL", pos.qty)
                        self.risk.close_position(sym, price)
                        self._wins += 1

                else:
                    if price >= pos.stop_loss:
                        self._log("warn", f"STOP LOSS hit (short): {sym} @ {price:.4f}")
                        await self.exec.place_market_order(sym, "BUY", pos.qty)
                        self.risk.close_position(sym, price)
                        self._losses += 1

                    elif price <= pos.tp1 and not pos.tp1_hit:
                        pos.tp1_hit = True
                        partial = round(pos.qty * 0.40, 4)
                        await self.exec.place_market_order(sym, "BUY", partial)
                        pos.qty -= partial
                        self._log("ok", f"{sym} TP1 hit (short) @ {price:.4f}")

                    elif price <= pos.tp2 and pos.tp1_hit and not pos.tp2_hit:
                        pos.tp2_hit = True
                        partial = round(pos.qty * 0.583, 4)
                        await self.exec.place_market_order(sym, "BUY", partial)
                        pos.qty -= partial

                    elif price <= pos.tp3 and pos.tp2_hit:
                        await self.exec.place_market_order(sym, "BUY", pos.qty)
                        self.risk.close_position(sym, price)
                        self._wins += 1

    # ── Status helpers ────────────────────────────────────────

    def position_count(self) -> int:
        return len(self.risk.state.positions)

    def daily_pnl(self) -> float:
        return round(self.risk.state.daily_pnl, 4)

    def _live_prices(self) -> dict[str, float]:
        prices = {}
        for sym, buf in self.feed.buffers.items():
            candle = buf.live or (buf.closed[-1] if buf.closed else None)
            if candle:
                prices[sym] = round(candle.close, 6)
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
            candle = (buf.live or (buf.closed[-1] if buf.closed else None)) if buf else None
            current_price = round(candle.close, 6) if candle else p.entry_price
            sig_score = self._last_signals.get(sym)
            positions[sym] = {
                "direction": p.direction,
                "entry":     p.entry_price,
                "current":   current_price,
                "qty":       p.qty,
                "sl":        p.stop_loss,
                "tp1":       p.tp1,
                "tp2":       p.tp2,
                "tp3":       p.tp3,
                "tp1_hit":   p.tp1_hit,
                "tp2_hit":   p.tp2_hit,
                "pnl":       round(p.pnl, 4),
                "score":     sig_score.score if sig_score else 0,
            }

        uptime_s = int(time.time() - self._start_time)
        h = uptime_s // 3600
        m = (uptime_s % 3600) // 60
        s = uptime_s % 60

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
