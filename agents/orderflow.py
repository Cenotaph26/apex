"""
OrderFlowAgent — order book imbalance scorer.

Uses the best bid/ask snapshot from bookTicker.
Positive imbalance (more bids) → bullish signal.

Fix: staleness check added — if the snapshot is older than MAX_AGE_SEC
(e.g. WS was reconnecting), return neutral 50 instead of stale data.
"""
from __future__ import annotations
import logging
import time

from core.models import OrderBookSnapshot

log = logging.getLogger("apex.orderflow")

# If bookTicker hasn't updated in this many seconds, treat as no data
MAX_AGE_SEC = 10.0


class OrderFlowAgent:
    name = "OrderFlowAgent"

    def score(
        self,
        book: OrderBookSnapshot | None,
        direction: str,
    ) -> float:
        """Returns 0-100 score for given direction."""
        if book is None:
            return 50.0  # neutral — no data

        # Staleness check: WS reconnect gap can leave stale snapshot
        age = time.time() - book.timestamp
        if age > MAX_AGE_SEC:
            log.debug("OrderFlow %s snapshot stale (%.1fs) — returning neutral",
                      book.symbol, age)
            return 50.0

        imb = book.bid_ask_imbalance  # -1 to +1

        if direction == "long":
            raw = (imb + 1) / 2 * 100
        elif direction == "short":
            raw = (1 - imb) / 2 * 100
        else:
            return 0.0

        score = min(100.0, max(0.0, raw))
        log.debug("OrderFlow %s dir=%s imb=%.3f age=%.1fs score=%.1f",
                  book.symbol, direction, imb, age, score)
        return round(score, 1)
