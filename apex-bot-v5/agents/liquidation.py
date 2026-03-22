"""
LiquidationHunter — detects when a large cluster of stop-losses
or liquidations are about to be triggered, amplifying the move.

Heuristic (no raw liquidation feed on testnet):
- If price is within 0.5% of a recent swing high/low AND
  volume just spiked AND momentum is aligned → stops are being hunted.
"""
from __future__ import annotations
import logging
import statistics

from core.models import CandleBuffer

log = logging.getLogger("apex.liquidation")

_PROXIMITY_PCT = 0.005   # within 0.5% of swing extreme
_VOL_SPIKE     = 1.5     # volume spike threshold


class LiquidationHunter:
    name = "LiquidationHunter"

    def score(self, buf: CandleBuffer, direction: str) -> float:
        candles = buf.closed
        if len(candles) < 20:
            return 0.0

        last = candles[-1]
        lookback = candles[-20:]

        swing_high = max(c.high for c in lookback)
        swing_low  = min(c.low  for c in lookback)

        avg_vol = statistics.mean(c.volume for c in lookback)
        if avg_vol == 0:
            return 0.0

        vol_ratio = last.volume / avg_vol

        if direction == "long":
            # Price breaking above swing high = stops triggered above
            proximity = abs(last.close - swing_high) / swing_high
            price_ok  = last.close > swing_high * (1 - _PROXIMITY_PCT)
        elif direction == "short":
            proximity = abs(last.close - swing_low) / swing_low
            price_ok  = last.close < swing_low * (1 + _PROXIMITY_PCT)
        else:
            return 0.0

        if not price_ok:
            return 0.0

        proximity_score = max(0.0, (1 - proximity / _PROXIMITY_PCT)) * 100
        volume_score    = min(100.0, (vol_ratio / _VOL_SPIKE) * 70)

        total = (proximity_score * 0.5 + volume_score * 0.5)
        log.debug("Liquidation %s dir=%s prox=%.1f vol=%.1f score=%.1f",
                  last.symbol, direction, proximity_score, volume_score, total)
        return round(total, 1)
