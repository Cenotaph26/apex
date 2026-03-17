"""
MomentumSniper — 1m breakout agent.

Logic:
1. Consolidation detection  (last N bars tight range)
2. Resistance / support from rolling window
3. Price breakout confirmed on candle close
4. Volume spike filter  (volume > avg * multiplier)
5. Momentum velocity     (acceleration of closes)
→ Returns confidence score 0-100
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
            30,  # need 30 bars for avg_range
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

        range_ratio = recent_range / avg_range  # smaller = tighter
        # Score: 0 if ratio>=1, 100 if ratio<=0.2
        consolidation_score = max(0.0, min(100.0, (1.0 - range_ratio) * 125))

        # ── 2. Resistance / Support ───────────────────────────
        # Exclude the current (last) candle so it can break above/below
        last = candles[-1]
        lookback = candles[-21:-1]  # 20 candles before last
        if not lookback:
            return 0.0, "none"
        resistance = max(c.close for c in lookback)
        support = min(c.close for c in lookback)

        # ── 3. Price breakout ─────────────────────────────────
        margin = self.cfg.momentum_breakout_margin_pct / 100

        long_break = last.close > resistance * (1 + margin)
        short_break = last.close < support * (1 - margin)

        if not long_break and not short_break:
            return 0.0, "none"

        direction = "long" if long_break else "short"

        # Breakout margin score: bigger break = higher score (cap at 100)
        if long_break:
            break_pct = (last.close - resistance) / resistance * 100
        else:
            break_pct = (support - last.close) / support * 100

        breakout_score = min(100.0, break_pct / margin * 30 + 40)

        # ── 4. Volume spike ───────────────────────────────────
        avg_vol = statistics.mean(c.volume for c in candles[-21:-1])
        if avg_vol == 0:
            return 0.0, "none"

        vol_ratio = last.volume / avg_vol
        if vol_ratio < self.cfg.momentum_volume_multiplier:
            # Volume not confirmed — hard fail
            log.debug("%s volume too low: %.2fx (need %.1fx)",
                      last.symbol, vol_ratio, self.cfg.momentum_volume_multiplier)
            return 0.0, "none"

        volume_score = min(100.0, (vol_ratio / 3.0) * 100)

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
        body_score = last.body_ratio * 100  # strong body = clean close

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
            "[cons=%.0f vol=%.0f brk=%.0f vel=%.0f body=%.0f]",
            last.symbol, direction, total,
            consolidation_score, volume_score, breakout_score,
            velocity_score, body_score,
        )
        return round(total, 1), direction
