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
    score_threshold:    float = float(os.getenv("SCORE_THRESHOLD",    "65"))
    default_leverage:   int   = int(os.getenv("DEFAULT_LEVERAGE",     "1"))

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
                ("trading_hours",      "trading_hours",      str),
                ("symbol_blacklist",   "symbol_blacklist",   str),
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
    max_drawdown_pct:       float = float(os.getenv("MAX_DRAWDOWN_PCT",      "8.0"))
    max_portfolio_risk_pct: float = float(os.getenv("MAX_PORTFOLIO_RISK_PCT","6.0"))
    correlation_threshold:  float = float(os.getenv("CORRELATION_THRESHOLD", "0.75"))
    # Saat filtresi: boş string = tüm saatler aktif
    # Örnek: "9-12,14,17" = sadece 09:00-12:00, 14:00-15:00, 17:00-18:00 UTC
    # Gerçek veriden iyi saatler: WR>50% olan 09-12, 14, 17 UTC
    trading_hours:      str   = os.getenv("TRADING_HOURS", "")  # boş = 24 saat
    trail_mode:             str   = os.getenv("TRAIL_MODE", "none")   # none|breakeven|tp1|atr_trail — backtest: none best PnL
    # Kara liste: virgülle ayrılmış semboller — gerçek veriden kötü performanslılar
    # WIF(-54$), HUMA(-49$), SKY(-40$), PIXEL(-39$), PAXG(-32$) toplamın %63'ü
    symbol_blacklist:   str   = os.getenv("SYMBOL_BLACKLIST", "WIFUSDT,HUMAUSDT,SKYUSDT,PIXELUSDT,PAXGUSDT,SIGNUSDT")  # gerçek veriden kara liste
    # Saat filtresi: sadece bu saatlerde işlem aç (UTC, boş=hep aktif)
    # Gerçek veri: 09-12 ve 14-18 UTC → WR %51, diğerleri %33
    trading_hours_start:    int   = int(os.getenv("TRADING_HOURS_START", "9"))
    trading_hours_end:      int   = int(os.getenv("TRADING_HOURS_END",   "18"))
    trading_hours_enabled:  bool  = os.getenv("TRADING_HOURS_ENABLED", "false").lower() == "true"  # varsayılan kapalı, panelden açılır
    # Kara liste: bu semboller hiç işleme alınmaz
    symbol_blacklist: list[str] = [s.strip() for s in
        os.getenv("SYMBOL_BLACKLIST", "WIFUSDT,HUMAUSDT,SKYUSDT,PIXELUSDT,PAXGUSDT,SIGNUSDT").split(",")
        if s.strip()]

    # ── Agent weights ─────────────────────────────────────────
    weight_momentum:    float = float(os.getenv("WEIGHT_MOMENTUM",    "0.35"))
    weight_orderflow:   float = float(os.getenv("WEIGHT_ORDERFLOW",   "0.30"))
    weight_funding:     float = float(os.getenv("WEIGHT_FUNDING",     "0.20"))
    weight_liquidation: float = float(os.getenv("WEIGHT_LIQUIDATION", "0.15"))

    # ── MomentumSniper ────────────────────────────────────────
    momentum_consolidation_bars:  int   = int(os.getenv("MOMENTUM_CONSOLIDATION_BARS",   "10"))
    momentum_breakout_margin_pct: float = float(os.getenv("MOMENTUM_BREAKOUT_MARGIN_PCT","0.20"))

    # ── TP / SL ───────────────────────────────────────────────
    # TP seviyeleri — gerçek veriye göre optimize edildi (24-31 Mart analizi)
    # TP1=1.5%: R:R=0.70 (mevcut 0.46'dan iyi), TP1 hit oranı artar
    # TP2=2.5%: Ulaşılabilir, sim. net +177 USDT
    # TP3=4.0%: Gerçekçi hedef (önceki 7.0% hiç dolmuyordu)
    tp1_pct:           float = float(os.getenv("TP1_PCT",           "1.5"))  # %1.5 — TP3 erişilebilir
    tp2_pct:           float = float(os.getenv("TP2_PCT",           "3.0"))  # %3.0 — SL ortalama %2.1den uzak
    tp3_pct:           float = float(os.getenv("TP3_PCT",           "5.0"))  # %5.0 x10 kaldıraçla %50 ROI
    # Increased from 1.5 → 2.0: gives SL more room so normal volatility
    # doesn't trigger it before the trade has a chance to develop.
    atr_sl_multiplier: float = float(os.getenv("ATR_SL_MULTIPLIER", "3.0"))  # geniş SL — gürültüden kaçınır
    tp1_close_pct:      float = float(os.getenv("TP1_CLOSE_PCT",      "0.25"))  # TP1'de %25 kapat
    tp2_close_pct:      float = float(os.getenv("TP2_CLOSE_PCT",      "0.50"))  # TP2'de kalan %50 kapat

    # ── Watchlist ön filtresi ────────────────────────────────
    # Binance'tan çekilen tüm coinlere uygulanır.
    # Sadece ÜÇ koşulu birden sağlayanlar listeye girer.
    min_volume_usdt:    float = float(os.getenv("MIN_VOLUME_USDT",    "10000000"))   # 10M USDT/gün (50M çok sıkıydı)
    min_atr_pct:        float = float(os.getenv("MIN_ATR_PCT",        "0.02"))       # min %0.02/bar (0.08 çok sıkıydı)
    max_atr_pct:        float = float(os.getenv("MAX_ATR_PCT",        "1.50"))       # max %1.50/bar (0.50 çılgın piyasada reddediyordu)
    filter_refresh_hours: int = int(os.getenv("FILTER_REFRESH_HOURS", "6"))          # kaç saatte bir filtre yenilenir

    # ── URLs ──────────────────────────────────────────────────
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
