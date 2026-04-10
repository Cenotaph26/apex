"""
Configuration v5 — all values from environment variables.

Changes vs v4:
- atr_sl_multiplier: 1.5 → 2.5  (v6: daha geniş SL, erken tetiklenme azalır)
- watchlist: defaults to AUTO — discovered from Binance Futures at startup
- max_open_positions: now runtime-settable via API (no restart needed)
- score_threshold: now runtime-settable via API (no restart needed)
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # ── Exchange ──────────────────────────────────────────────
    exchange: str = os.getenv("EXCHANGE", "binance")
    mode: str     = os.getenv("MODE", "futures_demo")

    api_key: str = (
        os.getenv("API_KEY") or
        os.getenv("BINANCE_API_KEY") or
        ""
    )
    api_secret: str = (
        os.getenv("API_SECRET") or
        os.getenv("BINANCE_API_SECRET") or
        ""
    )

    # ── Watchlist ─────────────────────────────────────────────
    # Set WATCHLIST=AUTO to discover all USDT perpetual futures from Binance.
    # Set WATCHLIST=BTCUSDT,ETHUSDT,... to use a fixed list.
    # Default is AUTO.
    watchlist: list[str] = field(default_factory=lambda: [
        s.strip() for s in
        os.getenv("WATCHLIST", "AUTO").split(",")
        if s.strip()
    ])

    # ── Orchestrator (runtime-mutable) ────────────────────────
    score_threshold:    float = float(os.getenv("SCORE_THRESHOLD",    "72"))
    default_leverage:   int   = int(os.getenv("DEFAULT_LEVERAGE",     "1"))

    # ── Gerçek trade verisi bulgularına dayanan filtreler ──────────────
    # -341 USDT kaybın %73'ü bu 7 sembolden geldi (24-31 Mart gerçek data)
    symbol_blacklist: str = os.getenv(
        "SYMBOL_BLACKLIST",
        "WIFUSDT,HUMAUSDT,SKYUSDT,PIXELUSDT,PAXGUSDT,SIGNUSDT,ZROUSDT"
    )

    # Saat filtresi: 08-18 UTC iyi saatler WR=%77 +121$, kötü saatler WR=%22 -390$
    trading_hours_enabled: bool = os.getenv("TRADING_HOURS_ENABLED","true").lower() == "true"
    trading_hours_start:   int  = int(os.getenv("TRADING_HOURS_START", "8"))
    trading_hours_end:     int  = int(os.getenv("TRADING_HOURS_END",  "18"))

    def load_from_redis(self):
        """Restart sonrası Redis'ten kaydedilmiş runtime ayarları yükle."""
        try:
            from core.storage import storage
            saved = storage.get_all_config()
            if not saved:
                return
            for key, attr, typ in [
                ("score_threshold",    "score_threshold",    float),
                ("max_open_positions", "max_open_positions", int),
                ("atr_sl_multiplier",  "atr_sl_multiplier",  float),
                ("trail_mode",         "trail_mode",         str),
                ("default_leverage",   "default_leverage",   int),
                ("tp1_pct",            "tp1_pct",            float),
                ("tp2_pct",            "tp2_pct",            float),
                ("tp3_pct",            "tp3_pct",            float),
                ("symbol_blacklist",        "symbol_blacklist",        str),
                ("risk_per_trade_pct",      "risk_per_trade_pct",      float),
                ("use_fixed_notional",      "use_fixed_notional",      bool),
                ("fixed_notional_usdt",     "fixed_notional_usdt",     float),
                ("max_notional_usdt",       "max_notional_usdt",       float),
                ("trading_hours_enabled",   "trading_hours_enabled",   bool),
                ("trading_hours_start",     "trading_hours_start",     int),
                ("trading_hours_end",       "trading_hours_end",       int),
            ]:
                if key in saved and hasattr(self, attr):
                    setattr(self, attr, typ(saved[key]))
            import logging
            logging.getLogger("apex.config").info(
                "Redis config yüklendi: %d ayar", len(saved))
        except Exception as e:
            import logging
            logging.getLogger("apex.config").warning("Redis config: %s", e)
    loop_interval_sec:  float = float(os.getenv("LOOP_INTERVAL_SEC",  "60"))
    max_open_positions: int   = int(os.getenv("MAX_OPEN_POSITIONS",   "2"))

    # ── Risk ──────────────────────────────────────────────────
    risk_per_trade_pct:     float = float(os.getenv("RISK_PER_TRADE_PCT",    "1.5"))
    # Sabit notional modu — risk_pct yerine her trade sabit USDT kullanılır
    use_fixed_notional:     bool  = os.getenv("USE_FIXED_NOTIONAL", "true").lower() == "true"
    fixed_notional_usdt:    float = float(os.getenv("FIXED_NOTIONAL_USDT", "100"))
    # Max notional sınırı (0 = devre dışı, risk.py'deki MAX_NOTIONAL_PCT geçerli)
    max_notional_usdt:      float = float(os.getenv("MAX_NOTIONAL_USDT", "0"))
    max_drawdown_pct:       float = float(os.getenv("MAX_DRAWDOWN_PCT",      "8.0"))
    max_portfolio_risk_pct: float = float(os.getenv("MAX_PORTFOLIO_RISK_PCT","6.0"))
    correlation_threshold:  float = float(os.getenv("CORRELATION_THRESHOLD", "0.75"))
    trail_mode:             str   = os.getenv("TRAIL_MODE", "none")   # none|breakeven|tp1|atr_trail (TP3 için atr_trail önerilir)

    # ── Agent weights ─────────────────────────────────────────
    weight_momentum:    float = float(os.getenv("WEIGHT_MOMENTUM",    "0.35"))
    weight_orderflow:   float = float(os.getenv("WEIGHT_ORDERFLOW",   "0.30"))
    weight_funding:     float = float(os.getenv("WEIGHT_FUNDING",     "0.20"))
    weight_liquidation: float = float(os.getenv("WEIGHT_LIQUIDATION", "0.15"))

    # ── MomentumSniper ────────────────────────────────────────
    momentum_consolidation_bars:  int   = int(os.getenv("MOMENTUM_CONSOLIDATION_BARS",   "10"))
    momentum_breakout_margin_pct: float = float(os.getenv("MOMENTUM_BREAKOUT_MARGIN_PCT","0.20"))

    # ── TP / SL ───────────────────────────────────────────────
    tp1_pct:           float = float(os.getenv("TP1_PCT",           "1.5"))   # Gerçek: SL ort %2.15, TP1=%1 → R:R=0.46. TP1=1.5% → R:R=0.70
    tp2_pct:           float = float(os.getenv("TP2_PCT",           "1.8"))   # Öneri: TP1=1.5%→TP2=1.8% (0.3% gap, daha erişilebilir)
    tp3_pct:           float = float(os.getenv("TP3_PCT",           "4.0"))   # İstenen: %4.0 hedef, trailing ile yakalanır
    # Increased from 1.5 → 2.0: gives SL more room so normal volatility
    # doesn't trigger it before the trade has a chance to develop.
    atr_sl_multiplier: float = float(os.getenv("ATR_SL_MULTIPLIER", "2.0"))   # Dar SL → büyük notional → daha fazla kâr (aynı risk)

    # ── Watchlist ön filtresi ────────────────────────────────
    # Binance'tan çekilen tüm coinlere uygulanır.
    # Sadece ÜÇ koşulu birden sağlayanlar listeye girer.
    min_volume_usdt:    float = float(os.getenv("MIN_VOLUME_USDT",    "10000000"))   # 10M USDT/gün (50M çok sıkıydı)
    min_atr_pct:        float = float(os.getenv("MIN_ATR_PCT",        "0.02"))       # min %0.02/bar (0.08 çok sıkıydı)
    max_atr_pct:        float = float(os.getenv("MAX_ATR_PCT",        "1.50"))       # max %1.50/bar (0.50 çılgın piyasada reddediyordu)
    filter_refresh_hours: int = int(os.getenv("FILTER_REFRESH_HOURS", "6"))          # kaç saatte bir filtre yenilenir

    # ── URLs ──────────────────────────────────────────────────
    @property
    def blacklisted_symbols(self) -> set:
        """Kara listedeki sembolleri set olarak döndür."""
        return {s.strip().upper() for s in self.symbol_blacklist.split(",") if s.strip()}

    def is_trading_hour(self) -> bool:
        """08-18 UTC arası mı kontrol et."""
        if not self.trading_hours_enabled:
            return True
        from datetime import datetime, timezone
        h = datetime.now(timezone.utc).hour
        return self.trading_hours_start <= h < self.trading_hours_end

    @property
    def is_futures_demo(self) -> bool:
        return self.mode == "futures_demo"

    @property
    def testnet(self) -> bool:
        return True

    @property
    def rest_base(self) -> str:
        if self.is_futures_demo:
            return "https://testnet.binancefuture.com"
        return "https://testnet.binance.vision"

    @property
    def ws_base(self) -> str:
        return "wss://stream.binance.com:9443"


settings = Settings()
