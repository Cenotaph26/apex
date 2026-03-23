"""
Storage — Redis (öncelikli) veya dosya sistemi (fallback).
REDIS_URL env var varsa Redis kullanır, yoksa DATA_DIR/trades.csv yazar.
"""
from __future__ import annotations
import json, os, logging, io, csv
from datetime import datetime, timezone

log = logging.getLogger("apex.storage")

REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_PRIVATE_URL")
DATA_DIR  = os.getenv("DATA_DIR", "/data")
MAX_TRADES = 2000

CSV_HEADERS = [
    "timestamp","symbol","direction","entry_price","exit_price",
    "qty","leverage","notional_usdt","stop_loss","tp1","tp2",
    "exit_reason","tp1_hit","pnl_usdt","pnl_pct","hold_time_min",
    "score","session_balance","session_drawdown_pct",
]

class Storage:
    def __init__(self):
        self._r = None
        if REDIS_URL:
            try:
                import redis
                self._r = redis.from_url(REDIS_URL, decode_responses=True,
                                         socket_connect_timeout=5, socket_timeout=5)
                self._r.ping()
                log.info("Storage: Redis ✅ (%s...)", REDIS_URL[:25])
            except Exception as e:
                self._r = None
                log.warning("Storage: Redis bağlanamadı (%s) → dosya sistemi", e)
        if not self._r:
            os.makedirs(DATA_DIR, exist_ok=True)
            log.info("Storage: Dosya sistemi (%s)", DATA_DIR)

    # ── Trade ─────────────────────────────────────────────
    def append_trade(self, record: dict):
        record.setdefault("timestamp",
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
        if self._r:
            try:
                self._r.lpush("apex:trades", json.dumps(record))
                self._r.ltrim("apex:trades", 0, MAX_TRADES - 1)
                return
            except Exception as e:
                log.warning("Redis write: %s", e)
        self._csv_append(record)

    def get_trades(self, n: int = 100) -> list[dict]:
        if self._r:
            try:
                return [json.loads(x) for x in self._r.lrange("apex:trades", 0, n-1)]
            except Exception as e:
                log.warning("Redis read: %s", e)
        return self._csv_read(n)

    def get_stats(self) -> dict:
        trades = self.get_trades(MAX_TRADES)
        wins   = [t for t in trades if float(t.get("pnl_usdt", 0)) > 0]
        losses = [t for t in trades if float(t.get("pnl_usdt", 0)) <= 0]
        gp = sum(float(t["pnl_usdt"]) for t in wins)
        gl = abs(sum(float(t["pnl_usdt"]) for t in losses))
        total = len(trades)
        return {
            "total":         total,
            "wins":          len(wins),
            "losses":        len(losses),
            "wr_pct":        round(len(wins)/total*100, 1) if total else 0,
            "gross_profit":  round(gp, 2),
            "gross_loss":    round(gl, 2),
            "net_pnl":       round(gp - gl, 2),
            "profit_factor": round(gp/gl, 2) if gl > 0 else 0,
        }

    def get_csv_bytes(self) -> bytes | None:
        """CSV olarak export (download için)."""
        if self._r:
            try:
                trades = self.get_trades(MAX_TRADES)
                if not trades: return None
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=CSV_HEADERS, extrasaction="ignore")
                w.writeheader()
                for t in reversed(trades):
                    w.writerow(t)
                return buf.getvalue().encode("utf-8")
            except Exception as e:
                log.error("CSV export: %s", e)
                return None
        path = os.path.join(DATA_DIR, "trades.csv")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        return None

    # ── Session ───────────────────────────────────────────
    def save_session(self, data: dict):
        payload = json.dumps(data)
        if self._r:
            try:
                self._r.set("apex:session", payload, ex=86400)
                return
            except Exception as e:
                log.warning("Session save: %s", e)
        try:
            with open(os.path.join(DATA_DIR, "session.json"), "w") as f:
                f.write(payload)
        except Exception: pass

    def get_session(self) -> dict:
        if self._r:
            try:
                raw = self._r.get("apex:session")
                if raw: return json.loads(raw)
            except Exception: pass
        try:
            with open(os.path.join(DATA_DIR, "session.json")) as f:
                return json.load(f)
        except Exception:
            return {}

    # ── Runtime config (restart'ta korunur) ──────────────
    def save_config(self, key: str, value) -> bool:
        if self._r:
            try:
                self._r.hset("apex:config", key, json.dumps(value))
                return True
            except Exception: pass
        return False

    def get_config(self, key: str, default=None):
        if self._r:
            try:
                raw = self._r.hget("apex:config", key)
                if raw is not None: return json.loads(raw)
            except Exception: pass
        return default

    def get_all_config(self) -> dict:
        if self._r:
            try:
                return {k: json.loads(v) for k,v in self._r.hgetall("apex:config").items()}
            except Exception: pass
        return {}

    # ── CSV fallback ──────────────────────────────────────
    def _csv_append(self, record: dict):
        path = os.path.join(DATA_DIR, "trades.csv")
        new  = not os.path.exists(path)
        try:
            with open(path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
                if new: w.writeheader()
                w.writerow(record)
        except Exception as e:
            log.error("CSV append: %s", e)

    def _csv_read(self, n: int) -> list[dict]:
        path = os.path.join(DATA_DIR, "trades.csv")
        if not os.path.exists(path): return []
        try:
            with open(path) as f:
                rows = list(csv.DictReader(f))
            return list(reversed(rows[-n:]))
        except Exception:
            return []

# Singleton
storage = Storage()
