"""
MomentumSniper v2 — 1m breakout agent.

Logic:
1. Consolidation detection  (last N bars tight range)
2. Resistance / support from rolling window
3. Price breakout confirmed on candle close
4. Volume filter  (soft penalty instead of hard-fail)
5. Momentum velocity     (acceleration of closes)
6. Candle body quality

Fix vs v1:
- Volume was a hard-fail gate: if volume < 1.8x avg the entire signal was
  discarded, ignoring consolidation=100, funding=100, liquidation=100, etc.
  Now volume is a soft score — low volume reduces the total score but
  doesn't zero out a strong multi-factor signal.
  A breakout with 1.5x volume on perfect consolidation can still score ~60
  and clear the 65-threshold, whereas fake breakouts with no other
  confirmation still won't.
"""
from __future__ import annotations
import logging
import statistics

from core.models import Candle, CandleBuffer
from core.config import settings

log = logging.getLogger("apex.momentum")


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, min(len(candles), period + 1)):
        c = candles[-i]
        p = candles[-i - 1]
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
    return statistics.mean(trs) if trs else 0.0


class MomentumSniper:
    name = "MomentumSniper"

    def __init__(self):
        self.cfg = settings

    def score(self, buf: CandleBuffer) -> tuple[float, str]:
        """
        Returns (score 0-100, direction 'long'|'short'|'none').
        """
        candles = buf.closed
        needed = max(
            self.cfg.momentum_consolidation_bars + 2,
            30,
        )
        if len(candles) < needed:
            return 0.0, "none"

        # ── 1. Consolidation ──────────────────────────────────
        recent = candles[-self.cfg.momentum_consolidation_bars:]
        recent_range = max(c.high for c in recent) - min(c.low for c in recent)

        avg_range = statistics.mean(
            c.high - c.low for c in candles[-30:]
        ) * self.cfg.momentum_consolidation_bars

        if avg_range == 0:
            return 0.0, "none"

        range_ratio = recent_range / avg_range
        consolidation_score = max(0.0, min(100.0, (1.0 - range_ratio) * 125))

        # ── 2. Resistance / Support ───────────────────────────
        last = candles[-1]
        lookback = candles[-21:-1]
        if not lookback:
            return 0.0, "none"
        resistance = max(c.close for c in lookback)
        support    = min(c.close for c in lookback)

        # ── 3. Price breakout ─────────────────────────────────
        margin = self.cfg.momentum_breakout_margin_pct / 100

        long_break  = last.close > resistance * (1 + margin)
        short_break = last.close < support   * (1 - margin)

        if not long_break and not short_break:
            return 0.0, "none"

        direction = "long" if long_break else "short"

        if long_break:
            break_pct = (last.close - resistance) / resistance * 100
        else:
            break_pct = (support - last.close) / support * 100

        breakout_score = min(100.0, break_pct / margin * 30 + 40)

        # ── 4. Volume — SOFT score (was hard-fail) ────────────
        avg_vol = statistics.mean(c.volume for c in candles[-21:-1])
        if avg_vol == 0:
            return 0.0, "none"

        vol_ratio = last.volume / avg_vol
        # Score ramps from 0 at 0.5x to 100 at 3x+
        # A 1.8x spike (old threshold) now gives ~43 pts — still meaningful
        # but no longer a binary pass/fail gate.
        volume_score = min(100.0, max(0.0, (vol_ratio - 0.5) / 2.5 * 100))

        if vol_ratio < 0.8:
            # True zero-volume candle / data gap — still discard
            log.debug("%s volume suspiciously low: %.2fx — skipping",
                      last.symbol, vol_ratio)
            return 0.0, "none"

        # ── 5. Momentum velocity ──────────────────────────────
        if len(candles) >= 4:
            d1 = candles[-2].close - candles[-3].close
            d2 = candles[-1].close - candles[-2].close
            accel = d2 - d1
            if direction == "long":
                velocity_score = min(100.0, max(0.0, 50 + accel / last.close * 5000))
            else:
                velocity_score = min(100.0, max(0.0, 50 - accel / last.close * 5000))
        else:
            velocity_score = 50.0

        # ── 6. Candle body quality ────────────────────────────
        body_score = last.body_ratio * 100

        # ── Weighted total ────────────────────────────────────
        total = (
            consolidation_score * 0.25 +
            volume_score        * 0.30 +
            breakout_score      * 0.20 +
            velocity_score      * 0.15 +
            body_score          * 0.10
        )

        log.info(
            "%s MomentumSniper | dir=%s score=%.1f "
            "[cons=%.0f vol=%.0f(%.1fx) brk=%.0f vel=%.0f body=%.0f]",
            last.symbol, direction, total,
            consolidation_score, volume_score, vol_ratio,
            breakout_score, velocity_score, body_score,
        )
        return round(total, 1), direction
