"""
Core data structures: Candle, Tick, and a rolling CandleBuffer
that each agent reads from.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from collections import deque
from typing import Deque
import time


@dataclass
class Candle:
    symbol: str
    open_time: int       # ms epoch
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    is_closed: bool = False

    @property
    def body_ratio(self) -> float:
        """Candle body size relative to total wick range (0-1)."""
        rng = self.high - self.low
        if rng == 0:
            return 0.0
        return abs(self.close - self.open) / rng

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass
class Tick:
    symbol: str
    price: float
    qty: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class FundingRate:
    symbol: str
    rate: float          # e.g. 0.0001 = 0.01%
    next_funding_ms: int


@dataclass
class OrderBookSnapshot:
    symbol: str
    bids: list[tuple[float, float]]   # (price, qty) top-5
    asks: list[tuple[float, float]]
    timestamp: float = field(default_factory=time.time)

    @property
    def bid_ask_imbalance(self) -> float:
        """Positive = more bid pressure, negative = more ask pressure."""
        total_bid = sum(q for _, q in self.bids)
        total_ask = sum(q for _, q in self.asks)
        total = total_bid + total_ask
        if total == 0:
            return 0.0
        return (total_bid - total_ask) / total


class CandleBuffer:
    """
    Thread-safe rolling window of closed 1m candles per symbol.
    Agents read this buffer directly — no locking needed because
    Python's deque append/popleft are atomic at the GIL level.
    """

    def __init__(self, maxlen: int = 100):
        self._buf: Deque[Candle] = deque(maxlen=maxlen)
        self._live: Candle | None = None  # current unclosed candle

    def update(self, candle: Candle) -> bool:
        """Returns True when a new candle closes."""
        if candle.is_closed:
            self._buf.append(candle)
            self._live = None
            return True
        self._live = candle
        return False

    @property
    def closed(self) -> list[Candle]:
        return list(self._buf)

    @property
    def live(self) -> Candle | None:
        return self._live

    def __len__(self) -> int:
        return len(self._buf)
