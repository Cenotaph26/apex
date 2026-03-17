"""
Configuration — all values come from environment variables.
Copy .env.example to .env for local dev.
Railway injects these automatically from the Variables tab.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # ── Exchange ──────────────────────────────────────────────
    exchange: str = os.getenv("EXCHANGE", "binance")
    testnet: bool = os.getenv("TESTNET", "true").lower() == "true"
    api_key: str = os.getenv("API_KEY", "")
    api_secret: str = os.getenv("API_SECRET", "")

    # ── Watchlist ─────────────────────────────────────────────
    # Comma-separated, e.g. "BTCUSDT,ETHUSDT,SOLUSDT"
    watchlist: list[str] = field(default_factory=lambda: [
        s.strip() for s in
        os.getenv(
            "WATCHLIST",
            "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,"
            "ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,MATICUSDT,"
            "DOTUSDT,UNIUSDT,ATOMUSDT,LTCUSDT,NEARUSDT,"
            "APTUSDT,ARBUSDT,OPUSDT,INJUSDT,SUIUSDT"
        ).split(",")
        if s.strip()
    ])

    # ── Orchestrator ──────────────────────────────────────────
    score_threshold: float = float(os.getenv("SCORE_THRESHOLD", "75"))
    loop_interval_sec: float = float(os.getenv("LOOP_INTERVAL_SEC", "60"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))

    # ── Risk ──────────────────────────────────────────────────
    risk_per_trade_pct: float = float(os.getenv("RISK_PER_TRADE_PCT", "1.5"))  # % of balance
    max_drawdown_pct: float = float(os.getenv("MAX_DRAWDOWN_PCT", "8.0"))
    max_portfolio_risk_pct: float = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "6.0"))
    correlation_threshold: float = float(os.getenv("CORRELATION_THRESHOLD", "0.85"))

    # ── Agent weights ─────────────────────────────────────────
    weight_momentum: float = float(os.getenv("WEIGHT_MOMENTUM", "0.35"))
    weight_orderflow: float = float(os.getenv("WEIGHT_ORDERFLOW", "0.30"))
    weight_funding: float = float(os.getenv("WEIGHT_FUNDING", "0.20"))
    weight_liquidation: float = float(os.getenv("WEIGHT_LIQUIDATION", "0.15"))

    # ── MomentumSniper params ─────────────────────────────────
    momentum_consolidation_bars: int = int(os.getenv("MOMENTUM_CONSOLIDATION_BARS", "10"))
    momentum_breakout_margin_pct: float = float(os.getenv("MOMENTUM_BREAKOUT_MARGIN_PCT", "0.15"))
    momentum_volume_multiplier: float = float(os.getenv("MOMENTUM_VOLUME_MULTIPLIER", "1.8"))

    # ── TP / SL ───────────────────────────────────────────────
    tp1_pct: float = float(os.getenv("TP1_PCT", "0.8"))   # close 40% here
    tp2_pct: float = float(os.getenv("TP2_PCT", "1.6"))   # close 35% here
    tp3_pct: float = float(os.getenv("TP3_PCT", "2.8"))   # close 25% here
    atr_sl_multiplier: float = float(os.getenv("ATR_SL_MULTIPLIER", "1.5"))

    # ── WebSocket / REST URLs ─────────────────────────────────
    @property
    def ws_base(self) -> str:
        # Binance testnet does NOT support combined streams (/stream?streams=...).
        # Market data (kline, bookTicker) always comes from the real Binance WS.
        # Only order execution uses testnet REST endpoints.
        return "wss://stream.binance.com:9443"

    @property
    def ws_base_single(self) -> str:
        """Single-stream base (no /stream path)."""
        return "wss://stream.binance.com:9443/ws"

    @property
    def rest_base(self) -> str:
        if self.testnet:
            return "https://testnet.binance.vision"
        return "https://api.binance.com"


settings = Settings()
