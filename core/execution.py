"""
ExecutionEngine — Binance Spot Testnet REST client.

Timestamp sync: fetches server time once at startup and caches
the offset. Refreshes every 5 minutes. This handles Railway
container clock drift which causes 401 errors.
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

# How long to cache the server time offset (ms)
_OFFSET_TTL = 300  # seconds


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
        self._offset_ms: int = 0          # server_time - local_time
        self._offset_fetched_at: float = 0.0

        if self._dry_run:
            log.warning("No API_KEY set — running in DRY-RUN mode (no real orders)")
        else:
            log.info("ExecutionEngine ready — testnet=%s key=...%s",
                     settings.testnet, settings.api_key[-6:])

    # ── Server time sync ──────────────────────────────────────

    async def _sync_time(self):
        """Fetch Binance server time and cache the offset."""
        try:
            r = await self._client.get("/api/v3/time", timeout=5.0)
            server_ms = r.json()["serverTime"]
            local_ms  = int(time.time() * 1000)
            self._offset_ms = server_ms - local_ms
            self._offset_fetched_at = time.time()
            log.info("Clock offset synced: %+d ms", self._offset_ms)
        except Exception as e:
            log.warning("Time sync failed (using offset=0): %s", e)
            self._offset_ms = 0

    async def _get_offset(self) -> int:
        """Return cached offset, refresh if stale."""
        if time.time() - self._offset_fetched_at > _OFFSET_TTL:
            await self._sync_time()
        return self._offset_ms

    # ── Signing helpers ───────────────────────────────────────

    async def _signed_get(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
        p["recvWindow"] = 60000          # generous window for Railway
        qs = urllib.parse.urlencode(p)
        p["signature"]  = _sign(qs, settings.api_secret)
        r = await self._client.get(path, params=p)
        if not r.is_success:
            log.error("GET %s → %d: %s", path, r.status_code, r.text[:300])
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
            log.error("POST %s → %d: %s", path, r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json()

    # ── Public API ────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Return USDT free balance from testnet account."""
        if self._dry_run:
            return 1000.0

        try:
            data = await self._signed_get("/api/v3/account")

            # Log all non-zero balances so we can debug
            non_zero = [
                f"{a['asset']}={float(a['free']):.4f}"
                for a in data.get("balances", [])
                if float(a.get("free", 0)) > 0
            ]
            log.info("Testnet balances: %s",
                     ", ".join(non_zero) if non_zero else "(none)")

            # Prefer USDT, fall back to BUSD
            for target in ("USDT", "BUSD"):
                for asset in data.get("balances", []):
                    if asset["asset"] == target:
                        bal = float(asset["free"])
                        if bal > 0:
                            log.info("Balance: %.2f %s", bal, target)
                            return bal

            log.warning("No USDT/BUSD balance on testnet — using 1000.0 fallback")
            return 1000.0

        except httpx.HTTPStatusError as e:
            log.error("Balance fetch HTTP error %d: %s",
                      e.response.status_code, e.response.text[:200])
            return 1000.0
        except Exception as e:
            log.error("Balance fetch error: %s", e)
            return 1000.0

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
    ) -> dict:
        if self._dry_run:
            log.info("[DRY-RUN] MARKET %s %s qty=%.4f", side, symbol, qty)
            return {"orderId": f"dry_{int(time.time())}", "status": "FILLED"}

        params = {
            "symbol":   symbol,
            "side":     side,
            "type":     "MARKET",
            "quantity": f"{qty:.4f}",
        }
        try:
            result = await self._signed_post("/api/v3/order", params)
            log.info("ORDER PLACED: %s %s qty=%.4f id=%s status=%s",
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

        params = {
            "symbol":      symbol,
            "side":        side,
            "type":        "LIMIT",
            "timeInForce": "GTC",
            "quantity":    f"{qty:.4f}",
            "price":       f"{price:.4f}",
        }
        try:
            result = await self._signed_post("/api/v3/order", params)
            log.info("LIMIT ORDER: %s %s @ %.4f id=%s",
                     side, symbol, price, result.get("orderId"))
            return result
        except Exception as e:
            log.error("Limit order failed: %s", e)
            return {}

    async def cancel_order(self, symbol: str, order_id: str):
        if self._dry_run:
            return
        params = {
            "symbol":    symbol,
            "orderId":   order_id,
        }
        try:
            p = dict(params)
            p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
            p["recvWindow"] = 60000
            qs = urllib.parse.urlencode(p)
            p["signature"]  = _sign(qs, settings.api_secret)
            r = await self._client.delete("/api/v3/order", params=p)
            r.raise_for_status()
        except Exception as e:
            log.warning("Cancel order failed: %s", e)

    async def close(self):
        await self._client.aclose()
