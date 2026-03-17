"""
WebSocket feed: subscribes to kline (1m) + bookTicker streams
for all coins in the watchlist and populates CandleBuffers.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Callable

import websockets
from websockets.exceptions import ConnectionClosedError

from core.config import settings
from core.models import Candle, CandleBuffer, OrderBookSnapshot

log = logging.getLogger("apex.feed")

# Binance combined stream max = 1024 streams per connection.
# We use 2 streams per coin (kline_1m + bookTicker) = 40 for 20 coins.
# Well within limits.


class FeedManager:
    def __init__(self):
        self.buffers: dict[str, CandleBuffer] = {
            sym: CandleBuffer(maxlen=100) for sym in settings.watchlist
        }
        self.orderbooks: dict[str, OrderBookSnapshot] = {}
        self._callbacks: list[Callable[[str], None]] = []
        self._running = False

    def on_candle_close(self, fn: Callable[[str], None]):
        """Register a callback triggered when any candle closes."""
        self._callbacks.append(fn)

    def _build_stream_url(self) -> str:
        # Binance combined stream format:
        # wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/btcusdt@bookTicker/...
        streams = []
        for sym in settings.watchlist:
            s = sym.lower()
            streams.append(f"{s}@kline_1m")
            streams.append(f"{s}@bookTicker")
        combined = "/".join(streams)
        return f"{settings.ws_base}/stream?streams={combined}"

    async def run(self):
        self._running = True
        url = self._build_stream_url()
        log.info("Connecting to WS feed (%d coins)...", len(settings.watchlist))

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**20,
                ) as ws:
                    log.info("WS connected: %s", url[:80] + "...")
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
        # Combined stream wraps payload in {"stream": "...", "data": {...}}
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
            return
        closed = buf.update(candle)
        if closed:
            for cb in self._callbacks:
                try:
                    cb(sym)
                except Exception as e:
                    log.warning("Callback error for %s: %s", sym, e)

    def _handle_book(self, data: dict):
        sym = data.get("s", "")
        # bookTicker only has best bid/ask — store as single-level snapshot
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
