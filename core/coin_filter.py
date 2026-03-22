"""
CoinFilter — Dinamik ön filtre sistemi

Binance Futures'daki tüm coinleri tarar ama skor hesaplamadan ÖNCE
coin kalitesini kontrol eder. Böylece:

  - Düşük hacimli coinler (slippage riski)
  - Aşırı düşük/yüksek volatilite coinler
  
...skor hesaplamaya bile girmez. Sinyal eşiği (65-70) korunur,
işlem sayısı yeterli kalır ama kalite artar.

Filtreler (her 60 dakikada bir güncellenir):
  1. Hacim filtresi  : 24h USDT hacmi >= MIN_VOLUME_USDT
  2. Volatilite alt  : Son 4 saatlik ATR >= MIN_ATR_PCT (fırsat var mı?)
  3. Volatilite üst  : Son 4 saatlik ATR <= MAX_ATR_PCT (SL çok sık vurulur mu?)

Neden bu üç?
  - Hacim düşükse: spread genişler → slippage %0.03 değil %0.10-0.20 olur
  - ATR çok düşükse: TP1 (%0.6) asla vurmaz → sadece komisyon ödersin
  - ATR çok yüksekse: SL (ATR×2.5) bir mumda tetiklenir → %95 SL isabeti
"""
from __future__ import annotations
import logging
import time
import math
import statistics

from core.models import CandleBuffer

log = logging.getLogger("apex.coin_filter")

# ── Filtre parametreleri ──────────────────────────────────────────────────────
MIN_VOLUME_USDT  = 50_000_000    # 24h USDT hacmi ≥ 50M (ATR filtresi kaliteyi sağlar)
MIN_ATR_PCT      = 0.08          # Son 4 saatlik ATR ≥ %0.08 (BTC~%0.10 geçebilsin)
MAX_ATR_PCT      = 2.00          # Son 4 saatlik ATR ≤ %2.00 (aşırı volatilite)
FILTER_TTL_SEC   = 3600          # Filtre sonuçlarını 1 saat cache'le
ATR_BARS         = 240           # 4 saat = 240 × 1m bar


def _atr_pct(candles: list, n: int = ATR_BARS) -> float:
    """Son n barın ATR'sini fiyatın yüzdesi olarak döndürür."""
    if len(candles) < 2:
        return 0.0
    trs = []
    recent = candles[-min(n, len(candles)):]
    for i in range(1, len(recent)):
        c = recent[i]; p = recent[i-1]
        if hasattr(c, 'high'):   # Candle dataclass
            tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            mid = (c.high + c.low) / 2
        else:                    # dict (backtest)
            tr = max(c['h']-c['l'], abs(c['h']-p['c']), abs(c['l']-p['c']))
            mid = (c['h'] + c['l']) / 2
        trs.append(tr / mid * 100 if mid > 0 else 0)
    return statistics.mean(trs) if trs else 0.0


def _volume_24h_usdt(candles: list) -> float:
    """Son 1440 bardan (24 saat) USDT hacmini tahmin eder."""
    n = min(len(candles), 1440)
    recent = candles[-n:]
    total = 0.0
    for c in recent:
        if hasattr(c, 'volume'):
            total += c.volume * c.close
        else:
            total += c.get('qv', c.get('v', 0) * c.get('c', 0))
    # Eğer 1440 bardan azsa, orantılı tahmin yap
    if n < 1440:
        total = total * (1440 / n)
    return total


class CoinFilter:
    """
    Her coin için ön filtre kararını verir.
    Kararlar TTL süresi boyunca cache'lenir.
    """

    def __init__(
        self,
        min_volume: float = MIN_VOLUME_USDT,  # 50M
        min_atr_pct: float = MIN_ATR_PCT,  # 0.08
        max_atr_pct: float = MAX_ATR_PCT,
        ttl_sec: float = FILTER_TTL_SEC,
    ):
        self.min_volume  = min_volume
        self.min_atr_pct = min_atr_pct
        self.max_atr_pct = max_atr_pct
        self.ttl_sec     = ttl_sec

        # Cache: symbol → (pass: bool, reason: str, expires_at: float)
        self._cache: dict[str, tuple[bool, str, float]] = {}
        self._stats = {"pass": 0, "fail_volume": 0, "fail_atr_low": 0, "fail_atr_high": 0}

    def is_tradeable(self, symbol: str, buf: CandleBuffer) -> tuple[bool, str]:
        """
        Coinin işlem yapılabilir olup olmadığını kontrol eder.
        Returns: (tradeable: bool, reason: str)
        """
        now = time.time()

        # Cache kontrol
        cached = self._cache.get(symbol)
        if cached and cached[2] > now:
            return cached[0], cached[1]

        candles = buf.closed if hasattr(buf, 'closed') else buf
        if len(candles) < 30:
            # Yeterli veri yok — geç (filtreleme için erken)
            return True, "warmup"

        # ── Filtre 1: Hacim ───────────────────────────────────────────────────
        vol_24h = _volume_24h_usdt(candles)
        if vol_24h < self.min_volume:
            result = (False, f"hacim düşük: ${vol_24h/1e6:.0f}M < ${self.min_volume/1e6:.0f}M")
            self._cache[symbol] = (*result, now + self.ttl_sec)
            self._stats["fail_volume"] += 1
            return result

        # ── Filtre 2: Volatilite alt sınır ───────────────────────────────────
        atr_pct = _atr_pct(candles)
        if atr_pct < self.min_atr_pct:
            result = (False, f"ATR çok düşük: %{atr_pct:.3f} < %{self.min_atr_pct}")
            self._cache[symbol] = (*result, now + self.ttl_sec)
            self._stats["fail_atr_low"] += 1
            return result

        # ── Filtre 3: Volatilite üst sınır ───────────────────────────────────
        if atr_pct > self.max_atr_pct:
            result = (False, f"ATR çok yüksek: %{atr_pct:.3f} > %{self.max_atr_pct}")
            self._cache[symbol] = (*result, now + self.ttl_sec)
            self._stats["fail_atr_high"] += 1
            return result

        # ── Geçti ─────────────────────────────────────────────────────────────
        result = (True, f"ok: vol=${vol_24h/1e6:.0f}M atr=%{atr_pct:.3f}")
        self._cache[symbol] = (*result, now + self.ttl_sec)
        self._stats["pass"] += 1
        return result

    def stats(self) -> dict:
        total = sum(self._stats.values())
        return {
            "total_checked": total,
            "passed":        self._stats["pass"],
            "failed_volume": self._stats["fail_volume"],
            "failed_atr_low":  self._stats["fail_atr_low"],
            "failed_atr_high": self._stats["fail_atr_high"],
            "pass_rate": f"{self._stats['pass']/total*100:.0f}%" if total > 0 else "—",
        }

    def clear_cache(self):
        self._cache.clear()
        log.info("CoinFilter cache cleared")
