"""
FeedManager v5

New: AUTO watchlist discovery.
If settings.watchlist == ['AUTO'], fetches all actively-trading
USDT perpetual futures from Binance real API at startup and uses
those as the watchlist. Falls back to a curated 40-coin default
if the fetch fails.

Binance WS combined stream supports up to 1024 streams per connection.
Each coin uses 2 streams (kline_1m + bookTicker), so 512 coins max.
Typical Binance Futures has ~300 USDT pairs — well within limits.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosedError

from core.config import settings
from core.models import Candle, CandleBuffer, OrderBookSnapshot

log = logging.getLogger("apex.feed")

# Fallback watchlist if AUTO discovery fails
_DEFAULT_WATCHLIST = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","MATICUSDT",
    "DOTUSDT","UNIUSDT","ATOMUSDT","LTCUSDT","NEARUSDT",
    "APTUSDT","ARBUSDT","OPUSDT","INJUSDT","SUIUSDT",
    "FILUSDT","AAVEUSDT","FTMUSDT","SANDUSDT","MANAUSDT",
    "AXSUSDT","GALAUSDT","APEUSDT","GMTUSDT","CRVUSDT",
    "LDOUSDT","STXUSDT","RNDRUSDT","IMXUSDT","RUNEUSDT",
    "BLURUSDT","SEIUSDT","TIAUSDT","WLDUSDT","JUPUSDT",
]

# Minimum 24h USDT volume to include a coin (filter out illiquid pairs)
_MIN_VOLUME_USDT = 50_000_000  # 50M USDT


async def _discover_watchlist() -> list[str]:
    """
    Fetch all USDT perpetual futures from Binance real API.
    Filters by: status=TRADING, quoteAsset=USDT, 24h volume > 50M.
    Returns symbols sorted by 24h volume descending.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Exchange info — get all trading USDT perp symbols
            ei = await client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
            ei.raise_for_status()
            symbols = {
                s["symbol"]
                for s in ei.json().get("symbols", [])
                if s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL"
            }

            # 24h ticker — filter by volume
            tk = await client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
            tk.raise_for_status()
            tickers = [
                t for t in tk.json()
                if t["symbol"] in symbols
                and float(t.get("quoteVolume", 0)) >= _MIN_VOLUME_USDT
            ]
            tickers.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
            result = [t["symbol"] for t in tickers]
            log.info(
                "AUTO watchlist: %d USDT perp futures (volume ≥ $%dM)",
                len(result), _MIN_VOLUME_USDT // 1_000_000,
            )
            return result
    except Exception as e:
        log.warning("AUTO watchlist discovery failed: %s — using default list", e)
        return _DEFAULT_WATCHLIST


class FeedManager:
    def __init__(self):
        # watchlist will be populated in run() if AUTO
        self._raw_watchlist = settings.watchlist
        self.watchlist: list[str] = []
        self.buffers:    dict[str, CandleBuffer]       = {}
        self.orderbooks: dict[str, OrderBookSnapshot]  = {}
        self._callbacks: list[Callable[[str], None]]   = []
        self._running = False

    def on_candle_close(self, fn: Callable[[str], None]):
        self._callbacks.append(fn)

    async def _resolve_watchlist(self):
        if self._raw_watchlist == ["AUTO"]:
            self.watchlist = await _discover_watchlist()
        else:
            self.watchlist = [s for s in self._raw_watchlist if s]

        # Also update settings so orchestrator / risk can see the full list
        settings.watchlist = self.watchlist

        # Init buffers for all symbols
        self.buffers = {sym: CandleBuffer(maxlen=100) for sym in self.watchlist}
        log.info("Watchlist ready: %d coins", len(self.watchlist))

    def _build_stream_url(self) -> str:
        streams = []
        for sym in self.watchlist:
            s = sym.lower()
            streams.append(f"{s}@kline_1m")
            streams.append(f"{s}@bookTicker")
        combined = "/".join(streams)
        return f"{settings.ws_base}/stream?streams={combined}"

    async def run(self):
        self._running = True
        await self._resolve_watchlist()

        url = self._build_stream_url()
        log.info("Connecting to WS feed (%d coins, %d streams)...",
                 len(self.watchlist), len(self.watchlist) * 2)

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**21,   # 2MB — larger for many coins
                ) as ws:
                    log.info("WS connected: %d coins", len(self.watchlist))
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            self._dispatch(json.loads(raw))
                        except Exception as e:
                            log.warning("Dispatch error: %s", e)

            except ConnectionClosedError as e:
                log.warning("WS closed (%s), reconnecting in 3s...", e)
            except Exception as e:
                log.error("WS error (%s), reconnecting in 5s...", e)
                await asyncio.sleep(2)

            if self._running:
                await asyncio.sleep(3)

    def stop(self):
        self._running = False

    def _dispatch(self, msg: dict):
        data = msg.get("data", msg)
        event = data.get("e")
        if event == "kline":
            self._handle_kline(data)
        elif event == "bookTicker":
            self._handle_book(data)

    def _handle_kline(self, data: dict):
        k = data["k"]
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
            # New symbol arrived (rare) — create buffer on the fly
            self.buffers[sym] = CandleBuffer(maxlen=100)
            buf = self.buffers[sym]
        closed = buf.update(candle)
        if closed:
            for cb in self._callbacks:
                try:
                    cb(sym)
                except Exception as e:
                    log.warning("Callback error for %s: %s", sym, e)

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
