"""
APEX — entry point.
Starts the orchestrator and serves:
  GET /          → dashboard UI
  GET /health    → JSON health check
  GET /state     → full snapshot (JSON, polled)
  GET /stream    → Server-Sent Events — dashboard subscribes here
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.orchestrator import Orchestrator
from core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("apex.main")

orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator
    log.info("🚀 APEX starting — exchange=%s testnet=%s",
             settings.exchange.upper(), settings.testnet)
    orchestrator = Orchestrator()
    task = asyncio.create_task(orchestrator.run())

    yield

    log.info("🛑 Shutting down...")
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
        "status":          "ok",
        "exchange":        settings.exchange,
        "testnet":         settings.testnet,
        "active_coins":    len(settings.watchlist),
        "open_positions":  orchestrator.position_count(),
        "daily_pnl":       orchestrator.daily_pnl(),
    }


@app.get("/state")
async def state():
    if orchestrator is None:
        return {}
    return orchestrator.snapshot()


@app.get("/stream")
async def stream():
    """
    Server-Sent Events — pushes a full snapshot every second.
    Dashboard connects once and receives live updates.
    """
    async def event_generator():
        while True:
            try:
                if orchestrator is not None:
                    data = json.dumps(orchestrator.snapshot())
                    yield f"data: {data}\n\n"
                else:
                    yield "data: {}\n\n"
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("SSE error: %s", e)
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",    # disable nginx buffering
            "Access-Control-Allow-Origin": "*",
        },
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, loop="asyncio")
