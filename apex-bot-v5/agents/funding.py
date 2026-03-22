"""
FundingAgent — scores based on futures funding rate anomalies.

Fixes:
- Parallel warmup: on first run (empty cache), all symbols are fetched
  concurrently via prefetch_all(). Previously 20 sequential HTTP calls
  at candle close could cause ~100s delay at 5s timeout each.
- Module-level cache replaced with instance-level dict to avoid
  state leak between instances in tests.
"""
from __future__ import annotations
import asyncio
import logging
import time

import httpx

from core.config import settings

log = logging.getLogger("apex.funding")

_CACHE_TTL = 60  # seconds


async def _fetch_rate(client: httpx.AsyncClient, symbol: str) -> float | None:
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
        # Instance-level cache — no global state leak between instances
        self._cache: dict[str, tuple[float, float]] = {}  # sym -> (rate, fetched_at)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient()
        return self._client

    async def get_rate(self, symbol: str) -> float:
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and (now - cached[1]) < _CACHE_TTL:
            return cached[0]

        client = await self._get_client()
        rate = await _fetch_rate(client, symbol)
        if rate is None:
            # Return last known rate if available rather than defaulting to 0
            rate = cached[0] if cached else 0.0
        self._cache[symbol] = (rate, now)
        return rate

    async def prefetch_all(self, symbols: list[str]) -> None:
        """
        Warm the cache for all symbols in parallel.
        Call this once at startup so the first candle close doesn't
        trigger 20 sequential HTTP requests.
        """
        client = await self._get_client()
        results = await asyncio.gather(
            *[_fetch_rate(client, sym) for sym in symbols],
            return_exceptions=True,
        )
        now = time.time()
        for sym, rate in zip(symbols, results):
            if isinstance(rate, float):
                self._cache[sym] = (rate, now)
        cached_count = sum(1 for r in results if isinstance(r, float))
        log.info("Funding cache warmed: %d/%d symbols", cached_count, len(symbols))

    async def score(self, symbol: str, direction: str) -> float:
        """
        Returns 0-100 score.
        For LONG:  very negative funding → high score (longs get paid)
        For SHORT: very positive funding → high score (shorts get paid)
        Neutral funding (near 0) → ~50.
        """
        rate = await self.get_rate(symbol)

        # Typical range: -0.003 to +0.003 (extreme = ±0.001)
        normalised = max(-1.0, min(1.0, rate / 0.001))

        if direction == "long":
            raw = 50 + (-normalised) * 50
        elif direction == "short":
            raw = 50 + normalised * 50
        else:
            return 0.0

        score = round(float(raw), 1)
        log.debug("Funding %s dir=%s rate=%.5f score=%.1f",
                  symbol, direction, rate, score)
        return score
