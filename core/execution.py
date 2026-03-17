"""
ExecutionEngine — Binance Futures Demo Trading REST client.

Futures Demo: https://testnet.binancefuture.com
- GET  /fapi/v2/balance       → wallet balance
- POST /fapi/v1/order         → place order
- DELETE /fapi/v1/order       → cancel order
- GET  /fapi/v1/time          → server time

API key from: binance.com → Testnet → Futures Testnet
Or from demo trading: your existing demo key works with testnet.binancefuture.com
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

_OFFSET_TTL = 300  # seconds between time syncs


def _sign(query_string: str, secret: str) -> str:
    return _hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class ExecutionEngine:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.rest_base,
            headers={"X-MBX-APIKEY": settings.api_key},
            timeout=15.0,
        )
        self._dry_run = not bool(settings.api_key)
        self._offset_ms: int = 0
        self._offset_fetched_at: float = 0.0

        mode = "Futures Demo" if settings.is_futures_demo else "Spot Testnet"
        if self._dry_run:
            log.warning("No API key — DRY-RUN mode")
        else:
            log.info("ExecutionEngine ready [%s] base=%s key=...%s",
                     mode, settings.rest_base, settings.api_key[-6:])

    # ── Time sync ─────────────────────────────────────────────

    async def _sync_time(self):
        time_path = "/fapi/v1/time" if settings.is_futures_demo else "/api/v3/time"
        try:
            r = await self._client.get(time_path, timeout=5.0)
            server_ms = r.json()["serverTime"]
            local_ms  = int(time.time() * 1000)
            self._offset_ms = server_ms - local_ms
            self._offset_fetched_at = time.time()
            log.info("Clock offset synced: %+d ms", self._offset_ms)
        except Exception as e:
            log.warning("Time sync failed (offset=0): %s", e)
            self._offset_ms = 0
            self._offset_fetched_at = time.time()

    async def _get_offset(self) -> int:
        if time.time() - self._offset_fetched_at > _OFFSET_TTL:
            await self._sync_time()
        return self._offset_ms

    # ── Signed request helpers ────────────────────────────────

    async def _signed_get(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
        p["recvWindow"] = 60000
        qs = urllib.parse.urlencode(p)
        p["signature"]  = _sign(qs, settings.api_secret)
        r = await self._client.get(path, params=p)
        if not r.is_success:
            log.error("GET %s → %d: %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json()

    async def _signed_post(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
        p["recvWindow"] = 60000
        qs = urllib.parse.urlencode(p)
        p["signature"]  = _sign(qs, settings.api_secret)
        r = await self._client.post(
            path,
            content=urllib.parse.urlencode(p).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not r.is_success:
            log.error("POST %s → %d: %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json()

    # ── Balance ───────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return USDT balance from Futures Demo account."""
        if self._dry_run:
            return 1000.0
        try:
            if settings.is_futures_demo:
                # Futures: GET /fapi/v2/balance → list of assets
                data = await self._signed_get("/fapi/v2/balance")
                non_zero = [
                    f"{a['asset']}={float(a.get('availableBalance', a.get('balance', 0))):.2f}"
                    for a in data
                    if float(a.get('availableBalance', a.get('balance', 0))) > 0
                ]
                log.info("Futures Demo balances: %s",
                         ", ".join(non_zero) if non_zero else "(empty)")
                for asset in data:
                    if asset.get("asset") == "USDT":
                        bal = float(asset.get("availableBalance",
                                              asset.get("balance", 0)))
                        log.info("Futures Demo USDT balance: %.2f", bal)
                        return bal
                log.warning("USDT not found in futures balance")
                return 1000.0
            else:
                # Spot testnet: GET /api/v3/account
                data = await self._signed_get("/api/v3/account")
                non_zero = [
                    f"{a['asset']}={float(a['free']):.4f}"
                    for a in data.get("balances", [])
                    if float(a.get("free", 0)) > 0
                ]
                log.info("Spot balances: %s",
                         ", ".join(non_zero) if non_zero else "(empty)")
                for asset in data.get("balances", []):
                    if asset["asset"] in ("USDT", "BUSD"):
                        bal = float(asset["free"])
                        if bal > 0:
                            return bal
                return 1000.0

        except httpx.HTTPStatusError as e:
            log.error("Balance HTTP %d: %s",
                      e.response.status_code, e.response.text[:300])
            return 1000.0
        except Exception as e:
            log.error("Balance fetch error: %s", e)
            return 1000.0

    # ── Orders ────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,      # 'BUY' | 'SELL'
        qty: float,
    ) -> dict:
        if self._dry_run:
            log.info("[DRY-RUN] MARKET %s %s qty=%.4f", side, symbol, qty)
            return {"orderId": f"dry_{int(time.time())}", "status": "FILLED"}

        if settings.is_futures_demo:
            # Futures order
            params = {
                "symbol":   symbol,
                "side":     side,
                "type":     "MARKET",
                "quantity": f"{qty:.3f}",
            }
            path = "/fapi/v1/order"
        else:
            params = {
                "symbol":   symbol,
                "side":     side,
                "type":     "MARKET",
                "quantity": f"{qty:.4f}",
            }
            path = "/api/v3/order"

        try:
            result = await self._signed_post(path, params)
            log.info("ORDER: %s %s qty=%.4f → id=%s status=%s",
                     side, symbol, qty,
                     result.get("orderId"), result.get("status"))
            return result
        except Exception as e:
            log.error("Order failed %s %s: %s", side, symbol, e)
            return {}

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
    ) -> dict:
        if self._dry_run:
            log.info("[DRY-RUN] LIMIT %s %s qty=%.4f @ %.4f",
                     side, symbol, qty, price)
            return {"orderId": f"dry_{int(time.time())}", "status": "NEW"}

        if settings.is_futures_demo:
            params = {
                "symbol":      symbol,
                "side":        side,
                "type":        "LIMIT",
                "timeInForce": "GTC",
                "quantity":    f"{qty:.3f}",
                "price":       f"{price:.2f}",
            }
            path = "/fapi/v1/order"
        else:
            params = {
                "symbol":      symbol,
                "side":        side,
                "type":        "LIMIT",
                "timeInForce": "GTC",
                "quantity":    f"{qty:.4f}",
                "price":       f"{price:.4f}",
            }
            path = "/api/v3/order"

        try:
            result = await self._signed_post(path, params)
            log.info("LIMIT: %s %s @ %.4f → id=%s",
                     side, symbol, price, result.get("orderId"))
            return result
        except Exception as e:
            log.error("Limit order failed: %s", e)
            return {}

    async def cancel_order(self, symbol: str, order_id: str):
        if self._dry_run:
            return
        path = "/fapi/v1/order" if settings.is_futures_demo else "/api/v3/order"
        p = {
            "symbol":  symbol,
            "orderId": order_id,
        }
        try:
            p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
            p["recvWindow"] = 60000
            qs = urllib.parse.urlencode(p)
            p["signature"]  = _sign(qs, settings.api_secret)
            r = await self._client.delete(path, params=p)
            r.raise_for_status()
        except Exception as e:
            log.warning("Cancel order failed: %s", e)

    async def close(self):
        await self._client.aclose()
