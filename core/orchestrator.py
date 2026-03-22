"""
Orchestrator v5

Fixes vs v4:
1. Lock deadlock risk removed — _try_open no longer called inside lock.
   Duplicate-open prevention is now handled by checking positions dict
   and using _opening set, not a blanket lock over async HTTP calls.
2. Global leverage now parallel — asyncio.gather instead of 20 serial awaits.
3. snapshot() exposes total_equity and unrealized_pnl for dashboard.
4. SSE generator properly detects client disconnect via GeneratorExit.
5. Balance loop updates peak with total_equity (wallet + unrealized).
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
from core.risk import RiskManager, Position, SYMBOL_INFO, _round_step
from core.execution import ExecutionEngine
from agents.momentum import MomentumSniper
from agents.orderflow import OrderFlowAgent
from agents.funding import FundingAgent
from agents.liquidation import LiquidationHunter

log = logging.getLogger("apex.orchestrator")

MAX_LOG_LINES  = 60
MAX_EQUITY_PTS = 120


@dataclass
class CoinSignal:
    symbol: str; score: float; direction: str
    momentum_score: float; orderflow_score: float
    funding_score: float; liquidation_score: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class LogEntry:
    ts: str; lvl: str; msg: str


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

        # Separate locks for distinct concerns — no nested locking
        self._eval_lock   = asyncio.Lock()  # serializes candle batch processing
        self._open_lock   = asyncio.Lock()  # serializes position opens

        # Guards against double-close races (monitor vs sync loop)
        self._closing: set[str] = set()

        # Symbols currently being opened (prevents duplicate opens)
        self._opening: set[str] = set()

        # Remembered unknown exchange positions — warn once only
        self._warned_unknown: set[str] = set()

        self._logs: deque[LogEntry] = deque(maxlen=MAX_LOG_LINES)
        self._equity_history: deque[dict] = deque(maxlen=MAX_EQUITY_PTS)
        self._wins:   int = 0
        self._losses: int = 0
        self._start_time: float = time.time()

        self.feed.on_candle_close(self._on_candle_close)

    # ── Log helper ────────────────────────────────────────────
    def _log(self, lvl: str, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._logs.appendleft(LogEntry(ts=ts, lvl=lvl, msg=msg))
        {"warn": log.warning, "err": log.error}.get(lvl, log.info)(msg)

    # ── Lifecycle ─────────────────────────────────────────────
    async def run(self):
        self._running = True
        await self.exec._sync_time()

        balance = await self.exec.get_balance()
        self.risk.state.current_balance  = balance
        self.risk.state.peak_balance     = balance
        self.risk.state.starting_balance = balance
        self._log("ok", f"Starting balance: {balance:.2f} USDT (walletBalance)")

        await asyncio.gather(
            asyncio.create_task(self.feed.run()),
            asyncio.create_task(self._eval_loop()),
            asyncio.create_task(self._monitor_loop()),
            asyncio.create_task(self._sync_positions_loop()),
            asyncio.create_task(self._equity_loop()),
            asyncio.create_task(self._balance_loop()),
        )

    def stop(self):
        self._running = False
        self.feed.stop()

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
                    # Peak should reflect total equity (wallet + unrealized)
                    equity = self.risk.state.total_equity
                    if equity > self.risk.state.peak_balance:
                        self.risk.state.peak_balance = equity
                    if abs(bal - old) > 0.5:
                        self._log("ok", f"Balance synced: {bal:.2f} USDT "
                                        f"(equity={equity:.2f})")
            except Exception as e:
                self._log("warn", f"Balance sync failed: {e}")
            await asyncio.sleep(30)

    async def _equity_loop(self):
        while self._running:
            await asyncio.sleep(60)
            self._equity_history.append({
                "ts":      datetime.now(timezone.utc).strftime("%H:%M"),
                "balance": round(self.risk.state.total_equity, 2),
            })

    async def _sync_positions_loop(self):
        """
        Every 30s: compare bot state vs exchange.
        - Bot has position, exchange doesn't → SL/TP filled externally, clean up.
        - Exchange has position bot doesn't know → warn ONCE per symbol.
        """
        await asyncio.sleep(35)
        while self._running:
            try:
                ex_positions = await self.exec.get_open_positions()
                ex_symbols = {p["symbol"] for p in ex_positions}

                for sym in list(self.risk.state.positions.keys()):
                    if sym in self._closing or sym in self._opening:
                        continue
                    if sym not in ex_symbols:
                        self._closing.add(sym)
                        try:
                            pos = self.risk.state.positions.get(sym)
                            if pos is None:
                                continue
                            buf = self.feed.buffers.get(sym)
                            cur = pos.entry_price
                            if buf:
                                c = buf.live or (buf.closed[-1] if buf.closed else None)
                                if c:
                                    cur = c.close
                            pnl = self.risk.close_position(sym, cur, "exchange_sync")
                            if pnl >= 0:
                                self._wins += 1
                            else:
                                self._losses += 1
                            await self.exec.cancel_all_orders(sym)
                            self._log("ok" if pnl >= 0 else "warn",
                                f"Position synced closed: {sym} pnl={pnl:+.4f} USDT")
                        finally:
                            self._closing.discard(sym)

                # Warn about unknown exchange positions — only once per symbol
                current_unknowns: set[str] = set()
                for ep in ex_positions:
                    sym = ep["symbol"]
                    if sym not in self.risk.state.positions:
                        current_unknowns.add(sym)
                        if sym not in self._warned_unknown:
                            amt = float(ep.get("positionAmt", 0))
                            self._log("warn",
                                f"Unknown position on exchange: {sym} amt={amt:.4f} "
                                f"(not tracked by bot — manually opened?)")
                            self._warned_unknown.add(sym)
                # Clear when position is gone
                self._warned_unknown &= current_unknowns

            except Exception as e:
                self._log("warn", f"Position sync failed: {e}")

            await asyncio.sleep(30)

    # ── Eval loop ─────────────────────────────────────────────
    async def _eval_loop(self):
        while self._running:
            await asyncio.sleep(1.0)
            if not self._pending_eval:
                continue

            # Drain pending set under short lock
            async with self._eval_lock:
                batch = self._pending_eval.copy()
                self._pending_eval.clear()

            # Score all coins — funding HTTP calls happen here
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
        total = (m_score  * settings.weight_momentum  +
                 of_score * settings.weight_orderflow +
                 fu_score * settings.weight_funding   +
                 lq_score * settings.weight_liquidation)
        return CoinSignal(symbol=symbol, score=round(total, 1), direction=direction,
                          momentum_score=m_score, orderflow_score=of_score,
                          funding_score=fu_score, liquidation_score=lq_score)

    # ── Open position ─────────────────────────────────────────
    async def _try_open(self, sig: CoinSignal):
        # Fast pre-check before acquiring lock
        if sig.symbol in self._opening or sig.symbol in self.risk.state.positions:
            return

        # Serialize opens with a dedicated lock (no HTTP inside eval_lock)
        async with self._open_lock:
            # Re-check inside lock — another task may have opened between pre-check and here
            if sig.symbol in self._opening or sig.symbol in self.risk.state.positions:
                return

            ok, reason = self.risk.can_open(sig.symbol, sig.direction, self.feed.buffers)
            if not ok:
                self._log("info", f"trade blocked [{sig.symbol}]: {reason}")
                return

            self._opening.add(sig.symbol)

        # HTTP calls happen OUTSIDE the lock to avoid blocking other opens
        try:
            buf = self.feed.buffers.get(sig.symbol)
            if not buf or not buf.closed:
                return

            entry_price = buf.closed[-1].close
            leverage    = self.risk.get_leverage(sig.symbol)

            qty, stop_loss, tp1 = self.risk.position_size(
                sig.symbol, entry_price, sig.direction, buf)

            if qty <= 0:
                self._log("warn", f"qty=0 for {sig.symbol}, skip")
                return

            pct = -1 if sig.direction == "short" else 1
            tp2 = entry_price * (1 + pct * settings.tp2_pct / 100)
            tp3 = entry_price * (1 + pct * settings.tp3_pct / 100)

            await self.exec.set_leverage(sig.symbol, leverage)

            entry_side = "BUY" if sig.direction == "long" else "SELL"
            close_side = "SELL" if sig.direction == "long" else "BUY"

            result = await self.exec.place_market_order(sig.symbol, entry_side, qty)
            if not result.get("orderId"):
                self._log("warn", f"Entry order failed for {sig.symbol}")
                return

            order_id  = str(result.get("orderId", ""))
            avg_price = float(result.get("avgPrice") or entry_price)
            if avg_price > 0:
                entry_price = avg_price
                sl_dist = abs(stop_loss - entry_price)
                stop_loss = (entry_price - sl_dist) if sig.direction == "long" else (entry_price + sl_dist)
                tp1 = entry_price * (1 + pct * settings.tp1_pct / 100)
                tp2 = entry_price * (1 + pct * settings.tp2_pct / 100)
                tp3 = entry_price * (1 + pct * settings.tp3_pct / 100)

            sl_result = await self.exec.place_stop_loss(
                sig.symbol, close_side, qty, stop_loss)
            sl_order_id = str(sl_result.get("orderId", ""))

            _, step, _ = SYMBOL_INFO.get(sig.symbol, (1.0, 1.0, 4))
            tp1_qty = max(_round_step(qty * 0.5, step), step)
            tp1_result = await self.exec.place_take_profit(
                sig.symbol, close_side, tp1_qty, tp1)
            tp1_order_id = str(tp1_result.get("orderId", ""))

            pos = Position(
                symbol=sig.symbol, direction=sig.direction,
                entry_price=entry_price, qty=qty,
                stop_loss=stop_loss, tp1=tp1, tp2=tp2, tp3=tp3,
                leverage=leverage,
                order_id=order_id, sl_order_id=sl_order_id,
                tp1_order_id=tp1_order_id,
            )
            self.risk.open_position(pos)
            margin = qty * entry_price / leverage
            self._log("sig",
                f"{sig.symbol} score={sig.score:.0f} {sig.direction} "
                f"entry={entry_price:.4f} sl={stop_loss:.4f} tp1={tp1:.4f} "
                f"qty={qty} lev=x{leverage} margin={margin:.1f} USDT")
        finally:
            self._opening.discard(sig.symbol)

    # ── Monitor loop ──────────────────────────────────────────
    async def _monitor_loop(self):
        while self._running:
            await asyncio.sleep(1.0)
            for sym, pos in list(self.risk.state.positions.items()):
                if sym in self._closing:
                    continue

                buf = self.feed.buffers.get(sym)
                if buf is None:
                    continue
                live = buf.live or (buf.closed[-1] if buf.closed else None)
                if live is None:
                    continue

                price = live.close
                self.risk.update_pnl(sym, price)

                close_side = "SELL" if pos.direction == "long" else "BUY"
                _, step, _ = SYMBOL_INFO.get(sym, (1.0, 1.0, 4))

                if pos.direction == "long":
                    sl_hit  = price <= pos.stop_loss
                    tp1_hit = price >= pos.tp1 and not pos.tp1_hit
                    tp2_hit = price >= pos.tp2 and pos.tp1_hit and not pos.tp2_hit
                else:
                    sl_hit  = price >= pos.stop_loss
                    tp1_hit = price <= pos.tp1 and not pos.tp1_hit
                    tp2_hit = price <= pos.tp2 and pos.tp1_hit and not pos.tp2_hit

                if sl_hit:
                    self._closing.add(sym)
                    try:
                        self._log("warn", f"SL hit: {sym} @ {price:.4f} — closing")
                        await self.exec.cancel_all_orders(sym)
                        await self.exec.place_market_order(
                            sym, close_side, pos.qty, reduce_only=True)
                        pnl = self.risk.close_position(sym, price, "SL")
                        self._losses += 1
                        self._log("warn", f"Closed {sym} SL pnl={pnl:+.4f} USDT")
                    finally:
                        self._closing.discard(sym)

                elif tp1_hit:
                    pos.tp1_hit = True
                    self._log("ok", f"TP1 hit: {sym} @ {price:.4f} — 50% closed")
                    if pos.sl_order_id:
                        await self.exec.cancel_order(sym, pos.sl_order_id)
                    remaining = max(_round_step(pos.qty * 0.5, step), step)
                    pos.qty = remaining
                    sl_r = await self.exec.place_stop_loss(
                        sym, close_side, remaining, pos.entry_price)
                    pos.sl_order_id = str(sl_r.get("orderId", ""))
                    self._log("ok", f"SL moved to breakeven: {sym} @ {pos.entry_price:.4f}")

                elif tp2_hit:
                    self._closing.add(sym)
                    try:
                        pos.tp2_hit = True
                        self._log("ok", f"TP2 hit: {sym} @ {price:.4f} — closing rest")
                        # pos.qty is already the remaining half after TP1 — close all of it
                        await self.exec.cancel_all_orders(sym)
                        await self.exec.place_market_order(
                            sym, close_side, pos.qty, reduce_only=True)
                        pnl = self.risk.close_position(sym, price, "TP2")
                        self._wins += 1
                        self._log("ok", f"Closed {sym} TP2 pnl={pnl:+.4f} USDT")
                    finally:
                        self._closing.discard(sym)

    # ── Control API ───────────────────────────────────────────
    async def set_leverage(self, symbol: str | None, leverage: int) -> str:
        if symbol:
            self.risk.set_leverage(symbol, leverage)
            await self.exec.set_leverage(symbol, leverage)
            return f"Leverage set: {symbol} x{leverage}"
        else:
            self.risk.set_global_leverage(leverage)
            # Parallel: all 20 symbols at once instead of serial awaits
            await asyncio.gather(
                *[self.exec.set_leverage(sym, leverage) for sym in SYMBOL_INFO],
                return_exceptions=True,
            )
            return f"Global leverage set: x{leverage}"

    async def force_close(self, symbol: str) -> str:
        pos = self.risk.state.positions.get(symbol)
        if not pos:
            return f"No position found for {symbol}"
        if symbol in self._closing:
            return f"{symbol} is already being closed"
        self._closing.add(symbol)
        try:
            close_side = "SELL" if pos.direction == "long" else "BUY"
            buf = self.feed.buffers.get(symbol)
            cur = pos.entry_price
            if buf:
                c = buf.live or (buf.closed[-1] if buf.closed else None)
                if c:
                    cur = c.close
            await self.exec.cancel_all_orders(symbol)
            await self.exec.place_market_order(symbol, close_side, pos.qty, reduce_only=True)
            pnl = self.risk.close_position(symbol, cur, "force_close")
            msg = f"Force closed {symbol} pnl={pnl:+.4f} USDT"
            self._log("ok" if pnl >= 0 else "warn", msg)
            return msg
        finally:
            self._closing.discard(symbol)

    # ── Status ────────────────────────────────────────────────
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
            sym: {"score": s.score, "direction": s.direction,
                  "agents": {"momentum": s.momentum_score, "orderflow": s.orderflow_score,
                              "funding": s.funding_score, "liquidation": s.liquidation_score}}
            for sym, s in self._last_signals.items()
        }
        positions = {}
        for sym, p in self.risk.state.positions.items():
            buf = self.feed.buffers.get(sym)
            c = (buf.live or (buf.closed[-1] if buf.closed else None)) if buf else None
            cur = round(c.close, 6) if c else p.entry_price
            sig = self._last_signals.get(sym)
            rr = abs(p.tp1 - p.entry_price) / max(abs(p.stop_loss - p.entry_price), 0.0001)
            positions[sym] = {
                "direction": p.direction, "entry": p.entry_price, "current": cur,
                "qty": p.qty, "sl": p.stop_loss,
                "tp1": p.tp1, "tp2": p.tp2, "tp3": p.tp3,
                "tp1_hit": p.tp1_hit, "tp2_hit": p.tp2_hit,
                "pnl": round(p.pnl, 4), "score": sig.score if sig else 0,
                "leverage": p.leverage,
                "margin": round(p.qty * p.entry_price / p.leverage, 2),
                "rr_ratio": round(rr, 2),
            }

        uptime_s = int(time.time() - self._start_time)
        h, m, s = uptime_s // 3600, (uptime_s % 3600) // 60, uptime_s % 60
        best = max(self._last_signals.values(), key=lambda x: x.score) if self._last_signals else None

        return {
            "balance":          round(self.risk.state.current_balance, 2),
            "total_equity":     round(self.risk.state.total_equity, 2),
            "unrealized_pnl":   round(self.risk.state.unrealized_pnl, 4),
            "daily_pnl":        self.daily_pnl(),
            "drawdown_pct":     round(self.risk.state.drawdown_pct, 2),
            "wins":             self._wins,
            "losses":           self._losses,
            "open_positions":   positions,
            "last_signals":     signals,
            "live_prices":      self._live_prices(),
            "equity_history":   list(self._equity_history),
            "leverage_map":     {k: v for k, v in self.risk.leverage_map.items()},
            "global_leverage":  settings.default_leverage,
            "logs": [{"ts": e.ts, "lvl": e.lvl, "msg": e.msg} for e in self._logs],
            "uptime": f"{h:02d}:{m:02d}:{s:02d}",
            "best_signal": {
                "symbol": best.symbol, "score": best.score, "direction": best.direction,
                "agents": {"momentum": best.momentum_score, "orderflow": best.orderflow_score,
                           "funding": best.funding_score, "liquidation": best.liquidation_score},
            } if best else None,
        }
