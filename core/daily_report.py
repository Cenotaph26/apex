"""
Daily Report — Günlük/Haftalık Otomatik Performans Özeti

Her gün UTC 18:00'de (trading_hours_end) veya bot başladığında
önceki günün özetini log'a yazar ve Redis'e kaydeder.

Rapor içeriği:
  - Günlük net PnL
  - Win/loss sayısı ve win rate
  - Profit factor
  - En iyi / en kötü trade
  - Saat dilimine göre performans breakdown
  - Haftalık özet (Pazartesi sabahı)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

log = logging.getLogger("apex.daily_report")

if TYPE_CHECKING:
    from core.storage import Storage


def _fmt_pnl(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def _build_daily_stats(trades: list[dict], date_str: str) -> dict | None:
    """Belirli bir güne ait trade'leri filtrele ve istatistik hesapla."""
    day_trades = [
        t for t in trades
        if t.get("timestamp", "").startswith(date_str)
    ]
    if not day_trades:
        return None

    wins   = [t for t in day_trades if float(t.get("pnl_usdt", 0)) > 0]
    losses = [t for t in day_trades if float(t.get("pnl_usdt", 0)) <= 0]
    net    = sum(float(t.get("pnl_usdt", 0)) for t in day_trades)
    gp     = sum(float(t.get("pnl_usdt", 0)) for t in wins)
    gl     = abs(sum(float(t.get("pnl_usdt", 0)) for t in losses))
    pf     = round(gp / gl, 2) if gl > 0 else 0.0
    wr     = round(len(wins) / len(day_trades) * 100, 1) if day_trades else 0

    best  = max(day_trades, key=lambda t: float(t.get("pnl_usdt", 0)), default=None)
    worst = min(day_trades, key=lambda t: float(t.get("pnl_usdt", 0)), default=None)

    # Saat dilimine göre breakdown (UTC)
    hour_buckets: dict[str, list[float]] = {}
    for t in day_trades:
        ts = t.get("timestamp", "")
        try:
            h = int(ts[11:13])
            bucket = f"{h:02d}:00"
        except Exception:
            bucket = "??"
        hour_buckets.setdefault(bucket, []).append(float(t.get("pnl_usdt", 0)))

    best_hour  = max(hour_buckets, key=lambda h: sum(hour_buckets[h]), default=None)
    worst_hour = min(hour_buckets, key=lambda h: sum(hour_buckets[h]), default=None)

    # Exit reason breakdown
    reasons: dict[str, int] = {}
    for t in day_trades:
        r = t.get("exit_reason", "?")
        reasons[r] = reasons.get(r, 0) + 1

    return {
        "date":       date_str,
        "total":      len(day_trades),
        "wins":       len(wins),
        "losses":     len(losses),
        "wr_pct":     wr,
        "net_pnl":    round(net, 2),
        "gross_profit": round(gp, 2),
        "gross_loss":   round(gl, 2),
        "profit_factor": pf,
        "best_trade":  {
            "symbol": best.get("symbol", "?"),
            "pnl":    float(best.get("pnl_usdt", 0)),
        } if best else None,
        "worst_trade": {
            "symbol": worst.get("symbol", "?"),
            "pnl":    float(worst.get("pnl_usdt", 0)),
        } if worst else None,
        "best_hour":  best_hour,
        "worst_hour": worst_hour,
        "exit_reasons": reasons,
        "symbols": list({t.get("symbol", "") for t in day_trades}),
    }


def format_daily_report(stats: dict) -> str:
    """İnsan okunabilir günlük rapor metni."""
    lines = [
        f"━━━ APEX GÜNLÜK RAPOR [{stats['date']}] ━━━",
        f"📊 Toplam işlem : {stats['total']}",
        f"✅ Kazanç       : {stats['wins']}  ❌ Kayıp: {stats['losses']}",
        f"🎯 Win Rate     : %{stats['wr_pct']}",
        f"💰 Net PnL      : {_fmt_pnl(stats['net_pnl'])}",
        f"📈 Profit Factor: {stats['profit_factor']}",
    ]
    if stats.get("best_trade"):
        b = stats["best_trade"]
        lines.append(f"🏆 En iyi trade : {b['symbol']} {_fmt_pnl(b['pnl'])}")
    if stats.get("worst_trade"):
        w = stats["worst_trade"]
        lines.append(f"💔 En kötü trade: {w['symbol']} {_fmt_pnl(w['pnl'])}")
    if stats.get("best_hour"):
        lines.append(f"⏰ En iyi saat  : {stats['best_hour']} UTC")
    if stats.get("worst_hour"):
        lines.append(f"⏰ En kötü saat : {stats['worst_hour']} UTC")
    if stats.get("exit_reasons"):
        r = stats["exit_reasons"]
        parts = [f"{k}:{v}" for k, v in sorted(r.items(), key=lambda x: -x[1])]
        lines.append(f"🚪 Çıkış nedeni : {', '.join(parts)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


class DailyReporter:
    """
    Her gün trading_hours_end saatinde günlük rapor üretir.
    Bot restart olsa da Redis'te son rapor saklanır.
    """

    def __init__(self, storage: "Storage"):
        self._storage = storage
        self._last_reported: str = ""   # son rapor tarihi YYYY-MM-DD

    async def run_loop(self, log_fn):
        """
        Arka plan döngüsü — her dakika saat kontrolü yapar.
        UTC 18:00'de (veya DAILY_REPORT_HOUR env) rapor üretir.
        """
        report_hour = int(os.getenv("DAILY_REPORT_HOUR", "18"))
        await asyncio.sleep(60)   # startup grace
        while True:
            try:
                now = datetime.now(timezone.utc)
                today = now.strftime("%Y-%m-%d")

                if now.hour == report_hour and self._last_reported != today:
                    await self._generate_and_log(today, log_fn)
                    self._last_reported = today

                # Haftalık rapor — Pazartesi 08:00 UTC
                if now.weekday() == 0 and now.hour == 8:
                    weekly_key = f"weekly_{today}"
                    if self._last_reported != weekly_key:
                        await self._generate_weekly(log_fn)
                        self._last_reported = weekly_key

            except Exception as e:
                log.warning("DailyReporter loop error: %s", e)
            await asyncio.sleep(60)

    async def _generate_and_log(self, date_str: str, log_fn):
        """Günlük raporu oluştur, log'a yaz, Redis'e kaydet."""
        try:
            trades = self._storage.get_trades(2000)
            stats  = _build_daily_stats(trades, date_str)
            if not stats:
                log_fn("info", f"Günlük rapor [{date_str}]: işlem yok")
                return

            report_text = format_daily_report(stats)
            for line in report_text.split("\n"):
                log_fn("ok" if stats["net_pnl"] >= 0 else "warn", line)

            # Redis'e kaydet
            try:
                import json
                self._storage._r and self._storage._r.set(
                    f"apex:report:{date_str}", json.dumps(stats), ex=60*60*24*30
                )
            except Exception:
                pass

        except Exception as e:
            log.warning("Daily report generation error: %s", e)

    async def _generate_weekly(self, log_fn):
        """Son 7 günün toplam özeti."""
        try:
            trades = self._storage.get_trades(2000)
            now    = datetime.now(timezone.utc)
            weekly_trades = []
            for i in range(7):
                d = (now - timedelta(days=i+1)).strftime("%Y-%m-%d")
                weekly_trades += [t for t in trades if t.get("timestamp", "").startswith(d)]

            if not weekly_trades:
                return

            wins   = [t for t in weekly_trades if float(t.get("pnl_usdt", 0)) > 0]
            losses = [t for t in weekly_trades if float(t.get("pnl_usdt", 0)) <= 0]
            net    = sum(float(t.get("pnl_usdt", 0)) for t in weekly_trades)
            gp     = sum(float(t.get("pnl_usdt", 0)) for t in wins)
            gl     = abs(sum(float(t.get("pnl_usdt", 0)) for t in losses))
            pf     = round(gp / gl, 2) if gl > 0 else 0.0
            wr     = round(len(wins) / len(weekly_trades) * 100, 1)

            log_fn("ok" if net >= 0 else "warn",
                   f"━━━ HAFTALIK ÖZET ━━━ "
                   f"işlem={len(weekly_trades)} WR=%{wr} "
                   f"net={_fmt_pnl(net)} PF={pf}")
        except Exception as e:
            log.warning("Weekly report error: %s", e)
