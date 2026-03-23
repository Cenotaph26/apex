"""
Orchestrator v6 — Software-managed SL/TP

Root cause fix:
  Binance Futures testnet rejects STOP_MARKET and TAKE_PROFIT_MARKET
  orders with -4120 ("use Algo Order API"). So exchange-side SL/TP
  orders were never actually placed — Binance showed nothing, and
  TP1 partial close never executed on the exchange.

New approach — everything managed in monitor_loop:
  1. Entry:  MARKET order only (always works)
  2. SL:     monitor_loop watches price every second, fires MARKET close
  3. TP1:    monitor_loop detects price ≥ tp1 → MARKET close 50% qty
  4. TP2:    monitor_loop detects price ≥ tp2 → MARKET close remaining 50%
  5. TP3:    monitor_loop detects price ≥ tp3 → MARKET close any remainder

All three TP levels are tracked independently. This gives the dashboard
accurate PnL and ensures every partial close actually hits the exchange.
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
import statistics as _statistics

def atr(bars, n=14):
    """Simple ATR for trailing stop calculation (Candle objects)."""
    if len(bars) < 2: return 0.0
    trs = []
    for i in range(1, min(len(bars), n+1)):
        c, p = bars[-i], bars[-i-1]
        trs.append(max(c.high - c.low,
                       abs(c.high - p.close),
                       abs(c.low  - p.close)))
    return _statistics.mean(trs) if trs else 0.0
from core.execution import ExecutionEngine
from agents.momentum import MomentumSniper
from agents.orderflow import OrderFlowAgent
from agents.funding import FundingAgent
from agents.liquidation import LiquidationHunter
from core.coin_filter import CoinFilter
from core.trade_logger import TradeLogger, TradeRecord

log = logging.getLogger("apex.orchestrator")

MAX_LOG_LINES  = 60
MAX_EQUITY_PTS = 120

# What fraction of the original position to close at each TP level
TP1_CLOSE_PCT = 0.25   # close 25% at TP1 (backtest: higher PF with 25%)
TP2_CLOSE_PCT = 0.50   # close 50% of remaining (= 25% of original) at TP2
# TP3 closes everything left

# 15m bar duration in ms
BAR_DURATION_MS = 15 * 60 * 1000


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
        self._coin_filter  = CoinFilter()
        self._trade_logger = TradeLogger()

        self._running = False
        self._last_signals: dict[str, CoinSignal] = {}
        self._pending_eval: set[str] = set()

        self._eval_lock  = asyncio.Lock()
        self._open_lock  = asyncio.Lock()
        self._closing:  set[str] = set()
        self._opening:  set[str] = set()
        self._warned_unknown: set[str] = set()

        self._logs: deque[LogEntry] = deque(maxlen=MAX_LOG_LINES)
        self._equity_history: deque[dict] = deque(maxlen=MAX_EQUITY_PTS)
        self._wins   = 0
        self._losses = 0
        self._start_time = time.time()

        self.feed.on_candle_close(self._on_candle_close)

    # ── Logging ───────────────────────────────────────────────
    def _log(self, lvl: str, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._logs.appendleft(LogEntry(ts=ts, lvl=lvl, msg=msg))
        {"warn": log.warning, "err": log.error}.get(lvl, log.info)(msg)

    # ── Lifecycle ─────────────────────────────────────────────
    async def run(self):
        self._running = True
        # Redis'ten önceki session'da kaydedilmiş ayarları yükle
        settings.load_from_redis()
        await self.exec._sync_time()

        balance = await self.exec.get_balance()
        self.risk.state.current_balance  = balance
        self.risk.state.peak_balance     = balance
        self.risk.state.starting_balance = balance
        self._log("ok", f"Starting balance: {balance:.2f} USDT (walletBalance)")

        # NOTE: prefetch_all is now called INSIDE feed.run() after _resolve_watchlist
        # so watchlist is guaranteed to be populated before funding cache is warmed.
        # Do NOT call it here — settings.watchlist may still be ["AUTO"] at this point.

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
    def _make_trade_record(self, pos, exit_price: float,
                           exit_reason: str, pnl: float) -> "TradeRecord":
        """Build a TradeRecord from a closed position."""
        from datetime import datetime, timezone
        now_ms = int(time.time() * 1000)
        entry_ms = getattr(pos, 'entry_ts', 0) or 0
        hold_min = (now_ms - entry_ms) / 60000.0 if entry_ms else 0.0
        notional = pos.qty * pos.entry_price / pos.leverage
        if pos.direction == "long":
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100
        sig = self._last_signals.get(pos.symbol)
        return TradeRecord(
            timestamp            = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            symbol               = pos.symbol,
            direction            = pos.direction,
            entry_price          = pos.entry_price,
            exit_price           = exit_price,
            qty                  = pos.qty,
            leverage             = pos.leverage,
            notional_usdt        = round(notional, 2),
            stop_loss            = pos.stop_loss,
            tp1                  = pos.tp1,
            tp2                  = pos.tp2,
            exit_reason          = exit_reason,
            tp1_hit              = pos.tp1_hit,
            pnl_usdt             = round(pnl, 4),
            pnl_pct              = round(pnl_pct, 2),
            hold_time_min        = round(hold_min, 1),
            entry_bar_ts         = entry_ms,
            exit_bar_ts          = now_ms,
            score                = sig.score if sig else 0.0,
            mom_score            = sig.momentum_score if sig else 0.0,
            of_score             = sig.orderflow_score if sig else 0.0,
            fu_score             = sig.funding_score if sig else 0.0,
            lq_score             = sig.liquidation_score if sig else 0.0,
            session_balance      = round(self.risk.state.current_balance, 2),
            session_drawdown_pct = round(self.risk.state.drawdown_pct, 2),
        )

    async def _balance_loop(self):
        await asyncio.sleep(30)
        while self._running:
            try:
                bal = await self.exec.get_balance()
                if bal > 0:
                    old = self.risk.state.current_balance
                    self.risk.state.current_balance = bal
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
        Every 30s reconcile bot state with exchange.
        If exchange closed a position (SL filled externally, manual close),
        clean up bot state so we don't hold a ghost position.
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
                            self._log("ok" if pnl >= 0 else "warn",
                                f"Position synced closed: {sym} pnl={pnl:+.4f} USDT")
                        finally:
                            self._closing.discard(sym)

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

            async with self._eval_lock:
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
                else:
                    # Log why score returned None (helps debug)
                    buf = self.feed.buffers.get(sym)
                    buf_len = len(buf) if buf else 0
                    if buf_len < 30:
                        self._log("info",
                            f"candle closed {sym} — warming up ({buf_len}/30 bars)")

            if not signals:
                continue

            winner = max(signals, key=lambda s: s.score)
            self._log("info",
                f"orchestrator: winner {winner.symbol} {winner.score:.1f} "
                f"({len(signals)} coins evaluated)")

            if winner.score >= settings.score_threshold:
                await self._try_open(winner)

    # ── Scoring ───────────────────────────────────────────────
    async def _score_coin(self, symbol: str) -> CoinSignal | None:
        buf = self.feed.buffers.get(symbol)
        if buf is None or len(buf) < 30:
            return None

        # ── Ön filtre: hacim + volatilite ────────────────────────────────────
        tradeable, filter_reason = self._coin_filter.is_tradeable(symbol, buf)
        if not tradeable:
            log.debug("coin_filter rejected %s: %s", symbol, filter_reason)
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
        if sig.symbol in self._opening or sig.symbol in self.risk.state.positions:
            return

        async with self._open_lock:
            if sig.symbol in self._opening or sig.symbol in self.risk.state.positions:
                return

            # Skip non-ASCII symbols (e.g. Chinese characters in testnet)
            if not sig.symbol.isascii():
                self._log("warn", f"Skipping non-ASCII symbol: {sig.symbol}")
                return

            ok, reason = self.risk.can_open(sig.symbol, sig.direction, self.feed.buffers, pending_count=len(self._opening))
            if not ok:
                self._log("info", f"trade blocked [{sig.symbol}]: {reason}")
                return

            self._opening.add(sig.symbol)

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

            # 1. Set leverage (returns actual leverage, may be lower if symbol has max)
            actual_lev = await self.exec.set_leverage(sig.symbol, leverage)
            if actual_lev != leverage:
                leverage = actual_lev
                self._log("info",
                    f"Leverage adjusted: {sig.symbol} x{leverage} (requested x{leverage})")

            entry_side = "BUY" if sig.direction == "long" else "SELL"

            # 2. Entry MARKET order — only order type sent to exchange
            result = await self.exec.place_market_order(sig.symbol, entry_side, qty)
            if not result.get("orderId"):
                self._log("warn", f"Entry order failed for {sig.symbol}")
                return

            avg_price = float(result.get("avgPrice") or entry_price)
            if avg_price > 0:
                entry_price = avg_price
                sl_dist = abs(stop_loss - entry_price)
                stop_loss = (entry_price - sl_dist) if sig.direction == "long" else (entry_price + sl_dist)
                tp1 = entry_price * (1 + pct * settings.tp1_pct / 100)
                tp2 = entry_price * (1 + pct * settings.tp2_pct / 100)
                tp3 = entry_price * (1 + pct * settings.tp3_pct / 100)

            pos = Position(
                symbol=sig.symbol, direction=sig.direction,
                entry_price=entry_price, qty=qty,
                stop_loss=stop_loss, tp1=tp1, tp2=tp2, tp3=tp3,
                leverage=leverage,
                order_id=str(result.get("orderId", "")),
                entry_ts=int(time.time() * 1000),
            )
            self.risk.open_position(pos)

            margin = qty * entry_price / leverage
            _, step, _ = SYMBOL_INFO.get(sig.symbol, (1.0, 1.0, 4))
            tp1_qty = max(_round_step(qty * TP1_CLOSE_PCT, step), step)

            self._log("sig",
                f"{sig.symbol} score={sig.score:.0f} {sig.direction} "
                f"entry={entry_price:.4f} "
                f"sl={stop_loss:.4f} "
                f"tp1={tp1:.4f} ({tp1_qty:.4f} lots / {TP1_CLOSE_PCT*100:.0f}%) "
                f"tp2={tp2:.4f}  tp3={tp3:.4f} "
                f"qty={qty} lev=x{leverage} margin={margin:.1f} USDT")
        finally:
            self._opening.discard(sig.symbol)

    # ── Monitor loop — SOFTWARE SL/TP ─────────────────────────
    async def _monitor_loop(self):
        """
        Runs every second. Manages SL and all TP levels entirely in software.

        SL  → close full remaining qty at market
        TP1 → close TP1_CLOSE_PCT (50%) of original qty at market,
               then move SL to breakeven
        TP2 → close TP2_CLOSE_PCT (50%) of remaining qty at market
        TP3 → close whatever is left at market

        All partial closes send a MARKET reduceOnly order to the exchange,
        so Binance always reflects the correct position size.
        """
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
                _, step, pp = SYMBOL_INFO.get(sym, (1.0, 1.0, 4))

                lng = pos.direction == "long"

                # ── SL ────────────────────────────────────────
                sl_hit = price <= pos.stop_loss if lng else price >= pos.stop_loss

                # ── TP1 ───────────────────────────────────────
                tp1_hit = (not pos.tp1_hit and
                           (price >= pos.tp1 if lng else price <= pos.tp1))

                # ── TP2 (only after TP1 hit) ──────────────────
                tp2_hit = (pos.tp1_hit and not pos.tp2_hit and
                           (price >= pos.tp2 if lng else price <= pos.tp2))

                # ── TP3 (only after TP2 hit) ──────────────────
                tp3_hit = (pos.tp2_hit and not pos.tp3_hit and
                           (price >= pos.tp3 if lng else price <= pos.tp3))

                # ─────────────────────────────────────────────
                if sl_hit:
                    self._closing.add(sym)
                    try:
                        self._log("warn",
                            f"SL hit: {sym} @ {price:.{pp}f} "
                            f"(sl={pos.stop_loss:.{pp}f}) — closing {pos.qty} lots")
                        r = await self.exec.place_market_order(
                            sym, close_side, pos.qty, reduce_only=True)
                        if r.get("orderId"):
                            _rec = self._make_trade_record(pos, price, "SL", 0)
                            pnl = self.risk.close_position(sym, price, "SL")
                            _rec.pnl_usdt = round(pnl, 4)
                            self._trade_logger.log_trade(_rec)
                            self._trade_logger.update_session(
                                self.risk.state.current_balance,
                                self.risk.state.drawdown_pct)
                            self._losses += 1
                            self._log("warn",
                                f"Closed {sym} SL pnl={pnl:+.4f} USDT")
                        else:
                            self._log("err",
                                f"SL market order FAILED for {sym} — retrying next tick")
                            # Don't close position state — will retry next second
                    finally:
                        self._closing.discard(sym)

                elif tp1_hit:
                    self._closing.add(sym)
                    try:
                        close_qty = max(_round_step(pos.qty * TP1_CLOSE_PCT, step), step)
                        self._log("ok",
                            f"TP1 hit: {sym} @ {price:.{pp}f} "
                            f"— closing {close_qty} lots ({TP1_CLOSE_PCT*100:.0f}%)")
                        r = await self.exec.place_market_order(
                            sym, close_side, close_qty, reduce_only=True)
                        if r.get("orderId"):
                            pos.tp1_hit = True
                            remaining = max(_round_step(pos.qty - close_qty, step), step)
                            pos.qty = remaining

                            # Trailing stop mode (backtest: 'none' outperforms 'breakeven')
                            trail = getattr(settings, 'trail_mode', 'none')
                            if trail == "breakeven":
                                pos.stop_loss = pos.entry_price
                                self._log("ok",
                                    f"TP1 hit {sym} @ {price:.{pp}f} | SL→breakeven"
                                    f" | remaining={remaining}")
                            elif trail == "tp1":
                                pos.stop_loss = pos.tp1
                                self._log("ok",
                                    f"TP1 hit {sym} @ {price:.{pp}f} | SL→TP1={pos.tp1:.{pp}f}"
                                    f" | remaining={remaining}")
                            elif trail == "atr_trail":
                                _a = atr(list(self.feed.buffers[sym].closed)) if sym in self.feed.buffers else 0
                                if pos.direction == "long":
                                    pos.stop_loss = max(pos.entry_price, price - _a * 1.5)
                                else:
                                    pos.stop_loss = min(pos.entry_price, price + _a * 1.5)
                                self._log("ok",
                                    f"TP1 hit {sym} @ {price:.{pp}f} | SL→ATR trail"
                                    f" | remaining={remaining}")
                            else:  # "none" — keep original SL (backtest winner)
                                self._log("ok",
                                    f"TP1 hit {sym} @ {price:.{pp}f} | SL unchanged"
                                    f" | remaining={remaining}")
                        else:
                            self._log("err",
                                f"TP1 market order FAILED for {sym} — retrying next tick")
                    finally:
                        self._closing.discard(sym)

                elif tp2_hit:
                    self._closing.add(sym)
                    try:
                        # TP2: close ALL remaining qty (position fully closed here)
                        close_qty = pos.qty
                        self._log("ok",
                            f"TP2 hit: {sym} @ {price:.{pp}f} "
                            f"— closing ALL remaining {close_qty} lots")
                        r = await self.exec.place_market_order(
                            sym, close_side, close_qty, reduce_only=True)
                        if r.get("orderId"):
                            pos.tp2_hit = True
                            _rec2 = self._make_trade_record(pos, price, "TP2", 0)
                            pnl = self.risk.close_position(sym, price, "TP2")
                            _rec2.pnl_usdt = round(pnl, 4)
                            self._trade_logger.log_trade(_rec2)
                            self._trade_logger.update_session(
                                self.risk.state.current_balance,
                                self.risk.state.drawdown_pct)
                            self._wins += 1
                            self._log("ok",
                                f"Closed {sym} TP2 pnl={pnl:+.4f} USDT — full position closed")
                        else:
                            self._log("err",
                                f"TP2 market order FAILED for {sym}")
                    finally:
                        self._closing.discard(sym)

                elif tp3_hit:
                    self._closing.add(sym)
                    try:
                        self._log("ok",
                            f"TP3 hit: {sym} @ {price:.{pp}f} "
                            f"— closing remaining {pos.qty} lots")
                        r = await self.exec.place_market_order(
                            sym, close_side, pos.qty, reduce_only=True)
                        if r.get("orderId"):
                            pos.tp3_hit = True
                            pnl = self.risk.close_position(sym, price, "TP3")
                            self._wins += 1
                            self._log("ok",
                                f"Closed {sym} TP3 pnl={pnl:+.4f} USDT")
                        else:
                            self._log("err",
                                f"TP3 market order FAILED for {sym}")
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
            r = await self.exec.place_market_order(
                symbol, close_side, pos.qty, reduce_only=True)
            if not r.get("orderId"):
                return f"Market order failed for {symbol}"
            _fc_rec = self._make_trade_record(pos, cur, "force_close", 0)
            pnl = self.risk.close_position(symbol, cur, "force_close")
            _fc_rec.pnl_usdt = round(pnl, 4)
            self._trade_logger.log_trade(_fc_rec)
            self._trade_logger.update_session(
                self.risk.state.current_balance, self.risk.state.drawdown_pct)
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
                  "agents": {"momentum": s.momentum_score,
                             "orderflow": s.orderflow_score,
                             "funding":   s.funding_score,
                             "liquidation": s.liquidation_score}}
            for sym, s in self._last_signals.items()
        }
        positions = {}
        for sym, p in self.risk.state.positions.items():
            buf = self.feed.buffers.get(sym)
            c = (buf.live or (buf.closed[-1] if buf.closed else None)) if buf else None
            cur = round(c.close, 6) if c else p.entry_price
            sig = self._last_signals.get(sym)
            sl_dist = abs(p.stop_loss - p.entry_price)
            tp1_dist = abs(p.tp1 - p.entry_price)
            rr = tp1_dist / sl_dist if sl_dist > 0 else 0
            _, _, pp = SYMBOL_INFO.get(sym, (1.0, 1.0, 4))
            positions[sym] = {
                "direction": p.direction, "entry": p.entry_price, "current": cur,
                "qty": p.qty, "sl": p.stop_loss,
                "tp1": p.tp1, "tp2": p.tp2, "tp3": p.tp3,
                "tp1_hit": p.tp1_hit, "tp2_hit": p.tp2_hit, "tp3_hit": p.tp3_hit,
                "pnl": round(p.pnl, 4), "score": sig.score if sig else 0,
                "leverage": p.leverage,
                "margin": round(p.qty * p.entry_price / p.leverage, 2),
                "rr_ratio": round(rr, 2),
            }

        uptime_s = int(time.time() - self._start_time)
        h, m, s = uptime_s // 3600, (uptime_s % 3600) // 60, uptime_s % 60
        best = (max(self._last_signals.values(), key=lambda x: x.score)
                if self._last_signals else None)

        return {
            "balance":        round(self.risk.state.current_balance, 2),
            "total_equity":   round(self.risk.state.total_equity, 2),
            "unrealized_pnl": round(self.risk.state.unrealized_pnl, 4),
            "daily_pnl":      self.daily_pnl(),
            "drawdown_pct":   round(self.risk.state.drawdown_pct, 2),
            "wins":           self._wins,
            "losses":         self._losses,
            "open_positions": positions,
            "last_signals":   signals,
            "live_prices":    self._live_prices(),
            "equity_history": list(self._equity_history),
            "leverage_map":   {k: v for k, v in self.risk.leverage_map.items()},
            "global_leverage":   settings.default_leverage,
            "score_threshold":   settings.score_threshold,
            "max_open_positions": settings.max_open_positions,
            "watchlist_count":   len(settings.watchlist),
            "filter_info":       (
                f"vol≥{settings.min_volume_usdt/1e6:.0f}M "
                f"atr:{settings.min_atr_pct:.2f}%-{settings.max_atr_pct:.2f}%"
            ),
            "coin_filter_stats": self._coin_filter.stats(),
            "trade_stats":        self._trade_logger.get_stats(),
            "recent_trades":      self._trade_logger.get_recent(20),
            "logs": [{"ts": e.ts, "lvl": e.lvl, "msg": e.msg} for e in self._logs],
            "uptime": f"{h:02d}:{m:02d}:{s:02d}",
            "best_signal": {
                "symbol": best.symbol, "score": best.score, "direction": best.direction,
                "agents": {"momentum": best.momentum_score,
                           "orderflow": best.orderflow_score,
                           "funding":   best.funding_score,
                           "liquidation": best.liquidation_score},
            } if best else None,
        }
