"""
APEX — entry point v5

New endpoints:
  POST /control/max_positions?value=N  — set max open positions (1-10)
  POST /control/score?value=N          — set min signal score (50-90)
  GET  /state                          — includes watchlist_count
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.orchestrator import Orchestrator
from core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
# Show DEBUG logs for feed so candle closes are visible
logging.getLogger("apex.feed").setLevel(logging.DEBUG)
log = logging.getLogger("apex.main")

orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator
    log.info("APEX starting — exchange=%s testnet=%s",
             settings.exchange.upper(), settings.testnet)
    orchestrator = Orchestrator()
    await orchestrator._funding.prefetch_all(settings.watchlist)
    task = asyncio.create_task(orchestrator.run())
    yield
    log.info("Shutting down...")
    orchestrator.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="APEX Bot", lifespan=lifespan)

_dash_dir = os.path.join(os.path.dirname(__file__), "dashboard")
if os.path.isdir(_dash_dir):
    app.mount("/static", StaticFiles(directory=_dash_dir), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse("dashboard/index.html")


@app.get("/health")
async def health():
    if orchestrator is None:
        return {"status": "starting"}
    return {
        "status":           "ok",
        "exchange":         settings.exchange,
        "testnet":          settings.testnet,
        "active_coins":     len(settings.watchlist),
        "open_positions":   orchestrator.position_count(),
        "daily_pnl":        orchestrator.daily_pnl(),
        "max_positions":    settings.max_open_positions,
        "score_threshold":  settings.score_threshold,
        "min_volume_m":    settings.min_volume_usdt / 1_000_000,
        "atr_filter":      f"{settings.min_atr_pct}%-{settings.max_atr_pct}%",
    }


@app.get("/state")
async def state():
    if orchestrator is None:
        return {}
    return orchestrator.snapshot()


@app.get("/trades")
async def get_trades(n: int = 100):
    """Return last N trades as JSON (from Redis or file)."""
    from core.storage import storage
    return storage.get_trades(n)


@app.get("/trades/csv")
async def trades_csv():
    """Return trades as CSV (from Redis or file)."""
    from fastapi.responses import Response
    from core.storage import storage
    data = storage.get_csv_bytes()
    if data:
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=apex_trades.csv"}
        )
    return {"error": "No trade log yet"}


@app.get("/trades/stats")
async def trades_stats_storage():
    """Live trade stats from Redis/CSV."""
    from core.storage import storage
    return storage.get_stats()


# ── Control endpoints ─────────────────────────────────────────────────────────

@app.post("/control/leverage")
async def set_leverage(symbol: str | None = None, leverage: int = 1):
    if orchestrator is None:
        return {"error": "not ready"}
    msg = await orchestrator.set_leverage(symbol, leverage)
    # Redis'e kaydet (restart sonrası korunur)
    from core.storage import storage
    storage.save_config("default_leverage", leverage)
    return {"ok": True, "msg": msg}


@app.post("/control/close/{symbol}")
async def force_close(symbol: str):
    if orchestrator is None:
        return {"error": "not ready"}
    msg = await orchestrator.force_close(symbol.upper())
    return {"ok": True, "msg": msg}


@app.post("/control/tp")
async def set_tp(tp1: float = 2.0, tp2: float = 4.0, tp3: float = 7.0):
    """TP seviyelerini değiştir (%)"""
    settings.tp1_pct = tp1; settings.tp2_pct = tp2; settings.tp3_pct = tp3
    from core.storage import storage
    storage.save_config("tp1_pct", tp1)
    storage.save_config("tp2_pct", tp2)
    storage.save_config("tp3_pct", tp3)
    return {"ok": True, "msg": f"TP güncellendi: TP1={tp1}% TP2={tp2}% TP3={tp3}%"}


@app.post("/control/atr")
async def set_atr(multiplier: float = 3.0):
    """ATR SL çarpanını değiştir"""
    settings.atr_sl_multiplier = multiplier
    from core.storage import storage
    storage.save_config("atr_sl_multiplier", multiplier)
    return {"ok": True, "msg": f"ATR SL multiplier: ×{multiplier}"}


@app.post("/control/hours")
async def set_hours(enabled: bool = True, start: int = 9, end: int = 18):
    """Saat filtresi (UTC). enabled=false ile kapat."""
    settings.trading_hours_enabled = enabled
    settings.trading_hours_start   = start
    settings.trading_hours_end     = end
    from core.storage import storage
    storage.save_config("trading_hours_enabled", enabled)
    storage.save_config("trading_hours_start",   start)
    storage.save_config("trading_hours_end",     end)
    status = f"{start:02d}:00-{end:02d}:00 UTC" if enabled else "Devre dışı"
    return {"ok": True, "msg": f"Saat filtresi: {status}"}


@app.post("/control/blacklist")
async def set_blacklist(symbols: str = ""):
    """Kara liste güncelle. Virgülle ayır: WIFUSDT,HUMAUSDT"""
    bl = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    settings.symbol_blacklist = bl
    from core.storage import storage
    storage.save_config("symbol_blacklist", bl)
    return {"ok": True, "msg": f"Kara liste: {bl}"}


@app.post("/control/hours")
async def set_trading_hours(hours: str = ""):
    """
    Saat filtresi ayarla. Örn: hours=9-12,14,17
    Boş string = 24 saat aktif (filtre yok).
    """
    settings.trading_hours = hours.strip()
    from core.storage import storage
    storage.save_config("trading_hours", hours.strip())
    if hours.strip():
        msg = f"Saat filtresi: {hours.strip()} UTC aktif"
    else:
        msg = "Saat filtresi kaldırıldı — 24 saat aktif"
    if orchestrator:
        orchestrator._log("ok", msg)
    return {"ok": True, "msg": msg}


@app.post("/control/blacklist")
async def set_blacklist(symbols: str = ""):
    """
    Kara liste güncelle. Örn: symbols=WIFUSDT,HUMAUSDT
    Boş string = kara liste temizle.
    """
    settings.symbol_blacklist = symbols.strip()
    from core.storage import storage
    storage.save_config("symbol_blacklist", symbols.strip())
    count = len([s for s in symbols.split(",") if s.strip()])
    msg = f"Kara liste: {count} sembol eklendi" if symbols.strip() else "Kara liste temizlendi"
    if orchestrator:
        orchestrator._log("ok", msg)
    return {"ok": True, "msg": msg}


@app.post("/control/trail")
async def set_trail(mode: str = "none"):
    """Set trailing stop mode: none | breakeven | tp1 | atr_trail"""
    valid = {"none", "breakeven", "tp1", "atr_trail"}
    if mode not in valid:
        return {"error": f"Invalid mode. Valid: {valid}"}
    settings.trail_mode = mode
    from core.storage import storage
    storage.save_config("trail_mode", mode)
    return {"ok": True, "msg": f"Trail mode set: {mode}"}


@app.post("/control/pause")
async def pause():
    if orchestrator is None:
        return {"error": "not ready"}
    orchestrator._running = False
    return {"ok": True, "msg": "Bot duraklatıldı — yeni pozisyon açılmaz"}


@app.post("/control/resume")
async def resume():
    if orchestrator is None:
        return {"error": "not ready"}
    orchestrator._running = True
    return {"ok": True, "msg": "Bot devam ediyor"}


@app.post("/control/max_positions")
async def set_max_positions(value: int):
    """Set maximum concurrent open positions (1-10). No restart needed."""
    if orchestrator is None:
        return {"error": "not ready"}
    value = max(1, min(10, value))
    settings.max_open_positions = value
    log.info("Max open positions set to %d", value)
    return {"ok": True, "msg": f"Maksimum pozisyon sayısı: {value}",
            "max_positions": value}


@app.post("/control/score")
async def set_score_threshold(value: float):
    """Set minimum signal score threshold (50-90). No restart needed."""
    if orchestrator is None:
        return {"error": "not ready"}
    value = max(60.0, min(85.0, float(value)))
    settings.score_threshold = value
    log.info("Score threshold set to %.1f", value)
    return {"ok": True, "msg": f"Min skor eşiği: {value:.0f}",
            "score_threshold": value}
    from core.storage import storage
    storage.save_config("score_threshold", threshold)
    log.info("Score threshold set: %.1f (saved to Redis)", threshold)

@app.get("/stream")
async def stream(request: Request):
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    if orchestrator is not None:
                        data = json.dumps(orchestrator.snapshot())
                        yield f"data: {data}\n\n"
                    else:
                        yield "data: {}\n\n"
                except Exception as e:
                    log.warning("SSE snapshot error: %s", e)
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, GeneratorExit):
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, loop="asyncio")
