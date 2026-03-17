"""
OrderFlowAgent — order book imbalance scorer.

Uses the best bid/ask snapshot from bookTicker.
Positive imbalance (more bids) → bullish signal.
"""
from __future__ import annotations
import logging

from core.models import OrderBookSnapshot

log = logging.getLogger("apex.orderflow")


class OrderFlowAgent:
    name = "OrderFlowAgent"

    def score(
        self,
        book: OrderBookSnapshot | None,
        direction: str,
    ) -> float:
        """
        Returns 0-100 score for given direction.
        direction: 'long' | 'short'
        """
        if book is None:
            return 50.0  # neutral when no data

        imb = book.bid_ask_imbalance  # -1 to +1

        if direction == "long":
            # +1 imbalance → 100, -1 → 0
            raw = (imb + 1) / 2 * 100
        elif direction == "short":
            raw = (1 - imb) / 2 * 100
        else:
            return 0.0

        score = min(100.0, max(0.0, raw))
        log.debug("OrderFlow %s dir=%s imb=%.3f score=%.1f",
                  book.symbol, direction, imb, score)
        return round(score, 1)
