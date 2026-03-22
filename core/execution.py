"""
ExecutionEngine v3 — Binance Futures Demo.

Key improvements:
- cancel_all_and_close(): emergency close via market order + cancel all open orders
- sync_positions(): fetch real positions from exchange, reconcile with bot state
- Proper reduceOnly on close orders
- Detailed logging of every order response
"""
from __future__ import annotations
import hashlib
import hmac as _hmac
import logging
import time
import urllib.parse

import httpx

from core.config import settings

log = logging.getLogger("apex.execution")
_OFFSET_TTL = 300


def _sign(qs: str, secret: str) -> str:
    return _hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


class ExecutionEngine:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.rest_base,
            headers={"X-MBX-APIKEY": settings.api_key},
            timeout=15.0,
        )
        self._dry_run = not bool(settings.api_key)
        self._offset_ms: int = 0
        self._offset_at:  float = 0.0
        mode = "Futures Demo" if settings.is_futures_demo else "Spot Testnet"
        if self._dry_run:
            log.warning("DRY-RUN mode — no real orders")
        else:
            log.info("ExecutionEngine [%s] %s key=...%s",
                     mode, settings.rest_base, settings.api_key[-6:])

    # ── Time ──────────────────────────────────────────────────
    async def _sync_time(self):
        path = "/fapi/v1/time" if settings.is_futures_demo else "/api/v3/time"
        try:
            r = await self._client.get(path, timeout=5.0)
            self._offset_ms = r.json()["serverTime"] - int(time.time() * 1000)
            self._offset_at = time.time()
            log.info("Clock offset: %+d ms", self._offset_ms)
        except Exception as e:
            log.warning("Time sync failed: %s", e)
            self._offset_at = time.time()

    async def _offset(self) -> int:
        if time.time() - self._offset_at > _OFFSET_TTL:
            await self._sync_time()
        return self._offset_ms

    # ── Signed requests ───────────────────────────────────────
    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + await self._offset()
        p["recvWindow"] = 60000
        qs = urllib.parse.urlencode(p)
        p["signature"] = _sign(qs, settings.api_secret)
        r = await self._client.get(path, params=p)
        if not r.is_success:
            log.error("GET %s → %d: %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + await self._offset()
        p["recvWindow"] = 60000
        qs = urllib.parse.urlencode(p)
        p["signature"] = _sign(qs, settings.api_secret)
        body = urllib.parse.urlencode(p).encode()
        r = await self._client.post(path, content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if not r.is_success:
            log.error("POST %s → %d: %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json()

    async def _delete(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000) + await self._offset()
        p["recvWindow"] = 60000
        qs = urllib.parse.urlencode(p)
        p["signature"] = _sign(qs, settings.api_secret)
        r = await self._client.delete(path, params=p)
        if not r.is_success:
            log.error("DELETE %s → %d: %s", path, r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json()

    # ── Balance ───────────────────────────────────────────────
    async def get_balance(self) -> float:
        if self._dry_run:
            return 1000.0
        try:
            if settings.is_futures_demo:
                data = await self._get("/fapi/v2/balance")
                non_zero = [f"{a['asset']}={float(a.get('availableBalance',0)):.2f}"
                            for a in data if float(a.get('availableBalance',0)) > 0]
                log.info("Futures Demo balances: %s", ", ".join(non_zero))
                for a in data:
                    if a.get("asset") == "USDT":
                        bal = float(a.get("availableBalance", 0))
                        log.info("Futures Demo USDT balance: %.2f", bal)
                        return bal
            else:
                data = await self._get("/api/v3/account")
                for a in data.get("balances", []):
                    if a["asset"] in ("USDT", "BUSD") and float(a["free"]) > 0:
                        return float(a["free"])
            return 1000.0
        except Exception as e:
            log.error("Balance error: %s", e)
            return 1000.0

    # ── Leverage ──────────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        if self._dry_run or not settings.is_futures_demo:
            return True
        try:
            r = await self._post("/fapi/v1/leverage",
                                 {"symbol": symbol, "leverage": leverage})
            log.info("Leverage: %s x%d (maxNotional=%s)",
                     symbol, r.get("leverage"), r.get("maxNotionalValue", "?"))
            return True
        except Exception as e:
            log.warning("Leverage failed %s x%d: %s", symbol, leverage, e)
            return False

    # ── Open positions from exchange ──────────────────────────
    async def get_open_positions(self) -> list[dict]:
        """Fetch real open positions from Futures exchange."""
        if self._dry_run or not settings.is_futures_demo:
            return []
        try:
            data = await self._get("/fapi/v2/positionRisk")
            return [p for p in data if abs(float(p.get("positionAmt", 0))) > 0]
        except Exception as e:
            log.error("Get positions failed: %s", e)
            return []

    # ── Market order ──────────────────────────────────────────
    async def place_market_order(self, symbol: str, side: str, qty: float,
                                  reduce_only: bool = False) -> dict:
        if self._dry_run:
            log.info("[DRY] MARKET %s %s %.4f reduceOnly=%s", side, symbol, qty, reduce_only)
            return {"orderId": f"dry_{int(time.time())}", "status": "FILLED", "avgPrice": "0"}

        if settings.is_futures_demo:
            params: dict = {"symbol": symbol, "side": side,
                            "type": "MARKET", "quantity": str(qty)}
            if reduce_only:
                params["reduceOnly"] = "true"
            path = "/fapi/v1/order"
        else:
            params = {"symbol": symbol, "side": side,
                      "type": "MARKET", "quantity": f"{qty:.4f}"}
            path = "/api/v3/order"

        try:
            r = await self._post(path, params)
            log.info("MARKET %s %s %.4f → id=%s status=%s avgPrice=%s",
                     side, symbol, qty, r.get("orderId"), r.get("status"), r.get("avgPrice","?"))
            return r
        except Exception as e:
            log.error("Market order FAILED %s %s %.4f: %s", side, symbol, qty, e)
            return {}

    # ── Stop-loss (STOP_MARKET on exchange) ───────────────────
    async def place_stop_loss(self, symbol: str, side: str, qty: float,
                               stop_price: float) -> dict:
        if self._dry_run:
            log.info("[DRY] STOP_MARKET %s %s %.4f @ %.5f", side, symbol, qty, stop_price)
            return {"orderId": f"dry_sl_{int(time.time())}"}
        if not settings.is_futures_demo:
            return {}

        from core.risk import SYMBOL_INFO
        _, _, pp = SYMBOL_INFO.get(symbol, (1.0, 1.0, 4))
        sp = round(stop_price, pp)

        try:
            r = await self._post("/fapi/v1/order", {
                "symbol":      symbol,
                "side":        side,
                "type":        "STOP_MARKET",
                "quantity":    str(qty),
                "stopPrice":   f"{sp:.{pp}f}",
                "reduceOnly":  "true",
                "workingType": "MARK_PRICE",
                "timeInForce": "GTE_GTC",
            })
            log.info("STOP_MARKET %s %s @ %.5f id=%s", side, symbol, sp, r.get("orderId"))
            return r
        except Exception as e:
            log.error("Stop loss FAILED %s %s: %s", side, symbol, e)
            return {}

    # ── Take-profit (TAKE_PROFIT_MARKET on exchange) ──────────
    async def place_take_profit(self, symbol: str, side: str, qty: float,
                                 stop_price: float) -> dict:
        if self._dry_run:
            log.info("[DRY] TP_MARKET %s %s %.4f @ %.5f", side, symbol, qty, stop_price)
            return {"orderId": f"dry_tp_{int(time.time())}"}
        if not settings.is_futures_demo:
            return {}

        from core.risk import SYMBOL_INFO
        _, _, pp = SYMBOL_INFO.get(symbol, (1.0, 1.0, 4))
        sp = round(stop_price, pp)

        try:
            r = await self._post("/fapi/v1/order", {
                "symbol":      symbol,
                "side":        side,
                "type":        "TAKE_PROFIT_MARKET",
                "quantity":    str(qty),
                "stopPrice":   f"{sp:.{pp}f}",
                "reduceOnly":  "true",
                "workingType": "MARK_PRICE",
                "timeInForce": "GTE_GTC",
            })
            log.info("TP_MARKET %s %s @ %.5f id=%s", side, symbol, sp, r.get("orderId"))
            return r
        except Exception as e:
            log.error("Take profit FAILED %s %s: %s", side, symbol, e)
            return {}

    # ── Cancel ────────────────────────────────────────────────
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if self._dry_run or not order_id:
            return True
        path = "/fapi/v1/order" if settings.is_futures_demo else "/api/v3/order"
        try:
            await self._delete(path, {"symbol": symbol, "orderId": order_id})
            log.info("Cancelled order %s id=%s", symbol, order_id)
            return True
        except Exception as e:
            log.warning("Cancel failed %s %s: %s", symbol, order_id, e)
            return False

    async def cancel_all_orders(self, symbol: str):
        if self._dry_run or not settings.is_futures_demo:
            return
        try:
            await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            log.info("All orders cancelled: %s", symbol)
        except Exception as e:
            log.warning("Cancel all failed %s: %s", symbol, e)

    # ── Emergency close: cancel all + market close ────────────
    async def emergency_close(self, symbol: str, side: str, qty: float):
        """Cancel all open orders then close position with market order."""
        log.warning("EMERGENCY CLOSE: %s %s qty=%.4f", symbol, side, qty)
        await self.cancel_all_orders(symbol)
        await asyncio.sleep(0.2)
        await self.place_market_order(symbol, side, qty, reduce_only=True)

    async def close(self):
        await self._client.aclose()


import asyncio  # needed for emergency_close
