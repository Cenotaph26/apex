"""
FeedManager v6

Ön filtre — üç koşulun tümünü sağlayan coinler:
  1. Hacim ≥ 50M USDT/gün          → likidite
  2. ATR/fiyat: %0.08 – %0.50      → hareket eden ama çılgın olmayan
  3. Fiyat ≥ 0.001 USDT            → dust coin değil

Filtre her FILTER_REFRESH_HOURS saatte bir yenilenir.
WS bağlantısı kesilse bile filtre bağımsız çalışır.
"""
from __future__ import annotations
import asyncio
import json
import logging
import math
import time
from typing import Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError

from core.config import settings
from core.models import Candle, CandleBuffer, OrderBookSnapshot

log = logging.getLogger("apex.feed")

_DEFAULT_WATCHLIST = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "NEARUSDT","APTUSDT","ARBUSDT","OPUSDT","INJUSDT",
    "SUIUSDT","TIAUSDT","WLDUSDT","SEIUSDT","RNDRUSDT",
    "LDOUSDT","STXUSDT","IMXUSDT","RUNEUSDT","BLURUSDT",
    "AAVEUSDT","CRVUSDT","GMTUSDT","APEUSDT","AXSUSDT",
    "SANDUSDT","MANAUSDT","GALAUSDT","FTMUSDT","FILUSDT",
    "ATOMUSDT","LTCUSDT","UNIUSDT","MATICUSDT","BNBUSDT",
]


async def _fetch_filtered_watchlist() -> list[str]:
    """
    Binance Futures'taki tüm aktif USDT perpetual coinleri çekip
    Volatilite-Hacim Composite filtresini uygular.

    Filtre kriterleri (üçü birden):
      1. 24h quoteVolume >= min_volume_usdt
      2. ATR proxy (priceChangePercent/24) normalize edilmiş değer
         → yaklaşık 1m ATR = dailyChange / sqrt(1440)
         → bu değer min_atr_pct ile max_atr_pct arasında olmalı
      3. lastPrice >= 0.001 (dust coin filtresi)

    Sonuç: hacme göre azalan sırada coin listesi.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Tüm USDT perp sembolleri
            ei_r = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
            ei_r.raise_for_status()
            trading_symbols = {
                s["symbol"]
                for s in ei_r.json().get("symbols", [])
                if s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL"
            }
            log.info("Binance Futures: %d aktif USDT perp sembol", len(trading_symbols))

            # 24h ticker verisi
            tk_r = await client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
            tk_r.raise_for_status()
            tickers = [t for t in tk_r.json() if t["symbol"] in trading_symbols]

        passed = []
        rejected_vol = rejected_atr = rejected_price = 0

        for t in tickers:
            sym     = t["symbol"]
            volume  = float(t.get("quoteVolume", 0))
            price   = float(t.get("lastPrice", 0))
            chg_pct = abs(float(t.get("priceChangePercent", 0)))

            # 1. Hacim filtresi
            if volume < settings.min_volume_usdt:
                rejected_vol += 1
                continue

            # 2. Fiyat filtresi (dust coin)
            if price < 0.001:
                rejected_price += 1
                continue

            # 3. ATR proxy filtresi
            # Günlük değişim %'si → yaklaşık 1m volatilite
            # daily_chg / sqrt(1440) = 1m ATR proxy
            atr_proxy = chg_pct / math.sqrt(1440)
            if atr_proxy < settings.min_atr_pct or atr_proxy > settings.max_atr_pct:
                rejected_atr += 1
                continue

            passed.append((sym, volume, atr_proxy))

        # Hacme göre sırala
        passed.sort(key=lambda x: x[1], reverse=True)
        result = [p[0] for p in passed]

        log.info(
            "Ön filtre sonucu: %d / %d coin geçti "
            "(hacim reddedilen=%d  ATR reddedilen=%d  fiyat reddedilen=%d)",
            len(result), len(tickers),
            rejected_vol, rejected_atr, rejected_price,
        )
        if result:
            top5 = [(p[0], f"${p[1]/1e6:.0f}M", f"atr≈{p[2]:.3f}%") for p in passed[:5]]
            log.info("İlk 5 coin: %s", top5)
        else:
            log.warning("Filtre sonucu 0 coin — tüm filtreler atlanıyor, varsayılan liste kullanılıyor")
            return _DEFAULT_WATCHLIST

        return result

    except Exception as e:
        log.warning("Watchlist keşfi başarısız: %s — varsayılan liste kullanılıyor", e)
        return _DEFAULT_WATCHLIST


class FeedManager:
    def __init__(self):
        self._raw_watchlist = settings.watchlist
        self.watchlist: list[str] = []
        self.buffers:    dict[str, CandleBuffer]      = {}
        self.orderbooks: dict[str, OrderBookSnapshot] = {}
        self._callbacks: list[Callable[[str], None]]  = []
        self._running   = False
        self._last_filter_time: float = 0.0

    def on_candle_close(self, fn: Callable[[str], None]):
        self._callbacks.append(fn)

    async def _resolve_watchlist(self):
        if self._raw_watchlist == ["AUTO"]:
            self.watchlist = await _fetch_filtered_watchlist()
        else:
            self.watchlist = [s for s in self._raw_watchlist if s]

        settings.watchlist = self.watchlist
        self._last_filter_time = time.time()

        # Buffer'ları başlat / güncelle (yeni coinler için ekle, çıkanları koru)
        for sym in self.watchlist:
            if sym not in self.buffers:
                self.buffers[sym] = CandleBuffer(maxlen=100)

        log.info("Watchlist hazır: %d coin", len(self.watchlist))
        # Warm funding cache now that watchlist is resolved
        try:
            from agents.funding import FundingAgent as _FA
            _fa = _FA()
            import asyncio as _asyncio
            _asyncio.create_task(_fa.prefetch_all(self.watchlist))
            log.info("Funding cache ön yükleme başlatıldı: %d coin", len(self.watchlist))
        except Exception as _e:
            log.warning("Funding prefetch başlatılamadı: %s", _e)

    async def _refresh_filter_loop(self):
        """Her FILTER_REFRESH_HOURS saatte bir filtreyi yeniler."""
        while self._running:
            await asyncio.sleep(settings.filter_refresh_hours * 3600)
            if not self._running:
                break
            log.info("Ön filtre yenileniyor...")
            old_count = len(self.watchlist)
            new_list = await _fetch_filtered_watchlist()
            if new_list:
                self.watchlist = new_list
                settings.watchlist = new_list
                for sym in self.watchlist:
                    if sym not in self.buffers:
                        self.buffers[sym] = CandleBuffer(maxlen=100)
                log.info(
                    "Filtre yenilendi: %d → %d coin",
                    old_count, len(self.watchlist),
                )

    def _build_stream_url(self) -> str:
        streams = []
        for sym in self.watchlist:
            s = sym.lower()
            streams.append(f"{s}@kline_15m")
            streams.append(f"{s}@bookTicker")
        return f"{settings.ws_base}/stream?streams={'/'.join(streams)}"

    async def _preload_historical(self):
        """
        Startup'ta her coin için Binance Futures REST API'den
        son 100 adet 15m mumu çekip buffer'ı anında doldurur.
        Böylece WS bağlanır bağlanmaz sinyal üretilebilir.
        """
        log.info("Geçmiş veri yükleniyor (%d coin)...", len(self.watchlist))
        ok = 0
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for sym in self.watchlist:
                    if not self._running:
                        break
                    try:
                        r = await client.get(
                            "https://fapi.binance.com/fapi/v1/klines",
                            params={
                                "symbol":   sym,
                                "interval": "15m",
                                "limit":    100,
                            },
                        )
                        if not r.is_success:
                            continue
                        klines = r.json()
                        buf = self.buffers.setdefault(sym, CandleBuffer(maxlen=100))
                        for k in klines:
                            candle = Candle(
                                symbol=sym,
                                open_time=int(k[0]),
                                open=float(k[1]),
                                high=float(k[2]),
                                low=float(k[3]),
                                close=float(k[4]),
                                volume=float(k[5]),
                                close_time=int(k[6]),
                                is_closed=True,
                            )
                            buf._buf.append(candle)
                        ok += 1
                    except Exception as e:
                        log.warning("Geçmiş veri hatası %s: %s", sym, e)
                    await asyncio.sleep(0.05)  # rate limit koruması
        except Exception as e:
            log.warning("Geçmiş veri yükleme başarısız: %s", e)
        log.info("Geçmiş veri yüklendi: %d/%d coin, her biri 100 bar hazır",
                 ok, len(self.watchlist))

    async def run(self):
        self._running = True
        await self._resolve_watchlist()

        # Geçmiş mumları yükle — WS açılmadan önce buffer'ları doldur
        await self._preload_historical()

        # Filtre yenileme döngüsünü arka planda başlat
        asyncio.create_task(self._refresh_filter_loop())

        url = self._build_stream_url()
        log.info(
            "WS feed bağlanıyor (%d coin, %d stream)...",
            len(self.watchlist), len(self.watchlist) * 2,
        )

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**22,   # 4MB — çok sayıda coin için
                ) as ws:
                    log.info("WS bağlandı: %d coin", len(self.watchlist))
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            self._dispatch(json.loads(raw))
                        except Exception as e:
                            log.warning("Dispatch hatası: %s", e)

            except ConnectionClosedError as e:
                log.warning("WS kapandı (%s), 3s sonra yeniden bağlanılıyor...", e)
            except Exception as e:
                log.error("WS hatası (%s), yeniden bağlanılıyor...", e)
                await asyncio.sleep(2)

            if self._running:
                await asyncio.sleep(3)

    def stop(self):
        self._running = False

    def _dispatch(self, msg: dict):
        data  = msg.get("data", msg)
        event = data.get("e")
        if event == "kline":
            self._handle_kline(data)
        elif event == "bookTicker":
            self._handle_book(data)

    def _handle_kline(self, data: dict):
        k   = data["k"]
        sym = k["s"]
        candle = Candle(
            symbol=sym,
            open_time=k["t"],
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            close_time=k["T"],
            is_closed=k["x"],
        )
        buf = self.buffers.get(sym)
        if buf is None:
            self.buffers[sym] = CandleBuffer(maxlen=100)
            buf = self.buffers[sym]
        closed = buf.update(candle)
        if closed:
            log.debug("Candle closed: %s buf_len=%d", sym, len(buf))
            for cb in self._callbacks:
                try:
                    cb(sym)
                except Exception as e:
                    log.warning("Callback hatası %s: %s", sym, e)

    def _handle_book(self, data: dict):
        sym = data.get("s", "")
        try:
            snap = OrderBookSnapshot(
                symbol=sym,
                bids=[(float(data["b"]), float(data["B"]))],
                asks=[(float(data["a"]), float(data["A"]))],
                timestamp=time.time(),
            )
            self.orderbooks[sym] = snap
        except (KeyError, ValueError):
            pass
