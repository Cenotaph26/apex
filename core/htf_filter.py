"""
HTF (Higher Time Frame) Trend Filter — Multi-Timeframe Analiz

15m sinyal üretildiğinde 1h ve 4h trend yönü kontrol edilir.
Trend karşı işlemler filtrelenir → daha yüksek win rate.

Mantık:
  - 1h EMA20 ve 4h EMA20 hesaplanır
  - Long sinyal: her iki TF'de fiyat EMA üzerinde olmalı (veya biri nötr)
  - Short sinyal: her iki TF'de fiyat EMA altında olmalı
  - Strict mod: her iki TF uyumlu → geçer
  - Soft mod: en az biri uyumlu → geçer (varsayılan)

Environment Variables:
  HTF_FILTER_ENABLED=true       # aç/kapat (varsayılan: true)
  HTF_FILTER_STRICT=false       # strict: her iki TF uyumlu olmalı
  HTF_FILTER_1H_PERIOD=20       # 1h EMA periyodu
  HTF_FILTER_4H_PERIOD=20       # 4h EMA periyodu
  HTF_CANDLES_LIMIT=50          # kaç bar geçmişi çekilsin
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict

import httpx

log = logging.getLogger("apex.htf_filter")

# ── Config ────────────────────────────────────────────────────────────────────
HTF_ENABLED        = os.getenv("HTF_FILTER_ENABLED", "true").lower() == "true"
HTF_STRICT         = os.getenv("HTF_FILTER_STRICT",  "false").lower() == "true"
HTF_1H_PERIOD      = int(os.getenv("HTF_FILTER_1H_PERIOD", "20"))
HTF_4H_PERIOD      = int(os.getenv("HTF_FILTER_4H_PERIOD", "20"))
HTF_CANDLES_LIMIT  = int(os.getenv("HTF_CANDLES_LIMIT", "50"))
HTF_CACHE_TTL_SEC  = 60 * 15   # 15 dakika cache — 1h/4h bar kapanışları yavaş değişir
HTF_FETCH_TIMEOUT  = 8.0

REST_BASE = os.getenv("BINANCE_REST_BASE", "https://fapi.binance.com")


def _ema(closes: list[float], period: int) -> float:
    """Basit EMA hesabı — son değeri döner."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


class HTFCache:
    """Sembol başına 1h ve 4h EMA değerlerini cache'ler."""

    def __init__(self):
        self._cache: dict[str, dict] = defaultdict(dict)
        self._lock = asyncio.Lock()

    def _is_fresh(self, symbol: str, tf: str) -> bool:
        entry = self._cache[symbol].get(tf)
        if not entry:
            return False
        return (time.time() - entry["ts"]) < HTF_CACHE_TTL_SEC

    async def get_ema(self, symbol: str, tf: str, period: int,
                      client: httpx.AsyncClient) -> float | None:
        async with self._lock:
            if self._is_fresh(symbol, tf):
                return self._cache[symbol][tf]["ema"]

        # Cache miss — fetch
        try:
            r = await client.get(
                f"{REST_BASE}/fapi/v1/klines",
                params={"symbol": symbol, "interval": tf,
                        "limit": HTF_CANDLES_LIMIT},
                timeout=HTF_FETCH_TIMEOUT,
            )
            if not r.is_success:
                log.debug("HTF klines %s %s HTTP %d", symbol, tf, r.status_code)
                return None
            klines = r.json()
            closes = [float(k[4]) for k in klines if int(k[6]) < int(time.time() * 1000)]
            if len(closes) < period:
                return None
            ema_val = _ema(closes, period)
            current = closes[-1]
            async with self._lock:
                self._cache[symbol][tf] = {
                    "ema":     ema_val,
                    "current": current,
                    "ts":      time.time(),
                }
            log.debug("HTF %s %s current=%.4f ema%d=%.4f",
                      symbol, tf, current, period, ema_val)
            return ema_val
        except Exception as e:
            log.debug("HTF fetch %s %s: %s", symbol, tf, e)
            return None

    def get_current(self, symbol: str, tf: str) -> float | None:
        entry = self._cache[symbol].get(tf)
        return entry["current"] if entry else None


# Singleton
_htf_cache = HTFCache()


async def htf_trend_ok(symbol: str, direction: str,
                       client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """
    Verilen yön için 1h + 4h trend uyumunu kontrol eder.

    Returns:
        (ok: bool, reason: str)
        ok=True → trend destekliyor veya filtre kapalı
        ok=False → trend karşı, trade bloklansın
    """
    if not HTF_ENABLED:
        return True, "htf_disabled"

    _client = client
    _owned  = False
    if _client is None:
        _client = httpx.AsyncClient(timeout=HTF_FETCH_TIMEOUT)
        _owned  = True

    try:
        # 1h ve 4h EMA'ları paralel çek
        ema_1h, ema_4h = await asyncio.gather(
            _htf_cache.get_ema(symbol, "1h", HTF_1H_PERIOD, _client),
            _htf_cache.get_ema(symbol, "4h", HTF_4H_PERIOD, _client),
        )
        cur_1h = _htf_cache.get_current(symbol, "1h")
        cur_4h = _htf_cache.get_current(symbol, "4h")

        # Veri yoksa geç (fail-open)
        if ema_1h is None and ema_4h is None:
            return True, "htf_no_data"
        if cur_1h is None and cur_4h is None:
            return True, "htf_no_data"

        # 1h trend yönü
        bullish_1h = (cur_1h > ema_1h) if (cur_1h and ema_1h) else None
        bullish_4h = (cur_4h > ema_4h) if (cur_4h and ema_4h) else None

        if direction == "long":
            ok_1h = bullish_1h is None or bullish_1h      # True = bullish veya bilinmiyor
            ok_4h = bullish_4h is None or bullish_4h
        else:  # short
            ok_1h = bullish_1h is None or not bullish_1h
            ok_4h = bullish_4h is None or not bullish_4h

        if HTF_STRICT:
            # Her iki TF uyumlu olmalı
            ok = ok_1h and ok_4h
        else:
            # En az biri uyumlu veya bilinmiyor → geç
            ok = ok_1h or ok_4h

        if not ok:
            reason = (
                f"htf_contra: 1h={'bull' if bullish_1h else 'bear'} "
                f"4h={'bull' if bullish_4h else 'bear'} "
                f"dir={direction}"
            )
            return False, reason

        return True, "htf_ok"

    finally:
        if _owned:
            await _client.aclose()
