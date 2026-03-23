"""
TradeLogger — Her işlemi Redis (veya CSV) olarak kaydeder.
Storage modülü üzerinden Redis öncelikli çalışır.

CSV kolonları:
  timestamp, symbol, direction, entry_price, exit_price, qty,
  leverage, notional_usdt, stop_loss, tp1, tp2,
  exit_reason, tp1_hit, pnl_usdt, pnl_pct,
  hold_time_min, score, agents_momentum, agents_orderflow,
  agents_funding, agents_liquidation, session_balance, session_drawdown_pct

Dosyalar:
  /data/trades.csv   — tüm işlemler (append)
  /data/trades.json  — son 500 işlem (overwrite)
  /data/session.json — aktif oturum özeti (her kapanışta güncelle)
"""
from __future__ import annotations
import csv, json, os, logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("apex.trade_logger")
from core.storage import storage

DATA_DIR = os.getenv("DATA_DIR", "/data")
CSV_PATH  = os.path.join(DATA_DIR, "trades.csv")
JSON_PATH = os.path.join(DATA_DIR, "trades.json")
SESSION_PATH = os.path.join(DATA_DIR, "session.json")
MAX_JSON_TRADES = 500

CSV_HEADERS = [
    "timestamp", "symbol", "direction",
    "entry_price", "exit_price", "qty", "leverage",
    "notional_usdt", "stop_loss", "tp1", "tp2",
    "exit_reason", "tp1_hit",
    "pnl_usdt", "pnl_pct",
    "hold_time_min", "entry_bar_ts", "exit_bar_ts",
    "score", "mom_score", "of_score", "fu_score", "lq_score",
    "session_balance", "session_drawdown_pct",
]


@dataclass
class TradeRecord:
    timestamp:           str
    symbol:              str
    direction:           str        # long | short
    entry_price:         float
    exit_price:          float
    qty:                 float
    leverage:            int
    notional_usdt:       float      # qty * entry / leverage
    stop_loss:           float
    tp1:                 float
    tp2:                 float
    exit_reason:         str        # SL | TP1 | TP2 | TP3 | force_close | EOD | exchange_sync
    tp1_hit:             bool
    pnl_usdt:            float
    pnl_pct:             float
    hold_time_min:       float
    entry_bar_ts:        int        # ms
    exit_bar_ts:         int        # ms
    score:               float
    mom_score:           float
    of_score:            float
    fu_score:            float
    lq_score:            float
    session_balance:     float
    session_drawdown_pct: float


class TradeLogger:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._trades: list[dict] = []
        self._session_start = datetime.now(timezone.utc).isoformat()
        self._session_wins = 0
        self._session_losses = 0
        self._session_gross_profit = 0.0
        self._session_gross_loss = 0.0

        # Write CSV header if file doesn't exist
        if not os.path.exists(CSV_PATH):
            with open(CSV_PATH, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
            log.info("Trade log created: %s", CSV_PATH)
        else:
            log.info("Trade log appending to: %s", CSV_PATH)

    def log_trade(self, record: TradeRecord):
        """Log a completed trade to CSV and memory."""
        row = {
            "timestamp":            record.timestamp,
            "symbol":               record.symbol,
            "direction":            record.direction,
            "entry_price":          f"{record.entry_price:.6f}",
            "exit_price":           f"{record.exit_price:.6f}",
            "qty":                  f"{record.qty:.4f}",
            "leverage":             record.leverage,
            "notional_usdt":        f"{record.notional_usdt:.2f}",
            "stop_loss":            f"{record.stop_loss:.6f}",
            "tp1":                  f"{record.tp1:.6f}",
            "tp2":                  f"{record.tp2:.6f}",
            "exit_reason":          record.exit_reason,
            "tp1_hit":              record.tp1_hit,
            "pnl_usdt":             f"{record.pnl_usdt:+.4f}",
            "pnl_pct":              f"{record.pnl_pct:+.2f}",
            "hold_time_min":        f"{record.hold_time_min:.1f}",
            "entry_bar_ts":         record.entry_bar_ts,
            "exit_bar_ts":          record.exit_bar_ts,
            "score":                f"{record.score:.1f}",
            "mom_score":            f"{record.mom_score:.1f}",
            "of_score":             f"{record.of_score:.1f}",
            "fu_score":             f"{record.fu_score:.1f}",
            "lq_score":             f"{record.lq_score:.1f}",
            "session_balance":      f"{record.session_balance:.2f}",
            "session_drawdown_pct": f"{record.session_drawdown_pct:.2f}",
        }

        # Persist to Redis or CSV via storage
        storage.append_trade(row)

        # Update in-memory list
        self._trades.append(row)
        if len(self._trades) > MAX_JSON_TRADES:
            self._trades = self._trades[-MAX_JSON_TRADES:]

        # Update session stats
        pnl = record.pnl_usdt
        if pnl > 0:
            self._session_wins += 1
            self._session_gross_profit += pnl
        else:
            self._session_losses += 1
            self._session_gross_loss += abs(pnl)

        # Write JSON
        try:
            with open(JSON_PATH, "w") as f:
                json.dump(self._trades, f, indent=2)
        except Exception as e:
            log.error("JSON write failed: %s", e)

        log.info("Trade logged: %s %s %s pnl=%+.2f reason=%s",
                 record.symbol, record.direction, record.exit_reason,
                 record.pnl_usdt, record.exit_reason)

    def update_session(self, balance: float, drawdown_pct: float):
        """Session özetini Redis/dosyaya yaz."""
        stats = storage.get_stats()
        session = {
            "session_start":  self._session_start,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
            "balance":        round(balance, 2),
            "drawdown_pct":   round(drawdown_pct, 2),
            **stats,
        }
        storage.save_session(session)

    def get_recent(self, n: int = 50) -> list[dict]:
        return self._trades[-n:]

    def get_stats(self) -> dict:
        """Redis/CSV'den gerçek zamanlı stats döner."""
        return storage.get_stats()

    def get_recent(self, n: int = 50) -> list[dict]:
        """Redis/CSV'den son N trade döner (override)."""
        return storage.get_trades(n)
