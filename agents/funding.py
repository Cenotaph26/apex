"""
FundingAgent — scores based on futures funding rate anomalies.

Fetches funding rate via Binance Futures REST (testnet or live).
Negative funding → longs are paid → bullish bias.
Extreme positive funding → shorts are squeezed → bearish.
"""
from __future__ import annotations
import asyncio
import logging
import time

import httpx

from core.config import settings

log = logging.getLogger("apex.funding")

# Cache funding rates to avoid hammering the API
_cache: dict[str, tuple[float, float]] = {}  # sym -> (rate, fetched_at)
_CACHE_TTL = 60  # seconds


async def _fetch_rate(client: httpx.AsyncClient, symbol: str) -> float | None:
    # Always use real futures API for funding rate data.
    # Testnet futures endpoint is unreliable / missing many symbols.
    base = "https://fapi.binance.com"
    try:
        r = await client.get(
            f"{base}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=5.0,
        )
        r.raise_for_status()
        data = r.json()
        return float(data.get("lastFundingRate", 0))
    except Exception as e:
        log.debug("Funding fetch error %s: %s", symbol, e)
        return None


class FundingAgent:
    name = "FundingAgent"

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def get_rate(self, symbol: str) -> float:
        now = time.time()
        cached = _cache.get(symbol)
        if cached and (now - cached[1]) < _CACHE_TTL:
            return cached[0]

        client = await self._get_client()
        rate = await _fetch_rate(client, symbol)
        if rate is None:
            rate = 0.0
        _cache[symbol] = (rate, now)
        return rate

    async def score(self, symbol: str, direction: str) -> float:
        """
        Returns 0-100 score.
        For LONG:  very negative funding → high score (longs get paid)
        For SHORT: very positive funding → high score (shorts get paid)
        Neutral funding (near 0) → ~50.
        """
        rate = await self.get_rate(symbol)

        # Typical range: -0.003 to +0.003 (extreme = ±0.001)
        # Normalise to -1..+1 by dividing by 0.001
        normalised = max(-1.0, min(1.0, rate / 0.001))

        if direction == "long":
            # negative funding is good for longs
            raw = 50 + (-normalised) * 50
        elif direction == "short":
            raw = 50 + normalised * 50
        else:
            return 0.0

        score = round(float(raw), 1)
        log.debug("Funding %s dir=%s rate=%.5f score=%.1f",
                  symbol, direction, rate, score)
        return score
