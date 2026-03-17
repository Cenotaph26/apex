"""
ExecutionEngine — places orders on Binance Spot Testnet.

Uses HMAC-signed REST requests (ccxt-free, raw httpx).
Testnet base: https://testnet.binance.vision
Get testnet API keys: https://testnet.binance.vision/
"""
from __future__ import annotations
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from core.config import settings
from core.risk import Position

log = logging.getLogger("apex.execution")


def _sign(params: dict, secret: str) -> str:
    query = urllib.parse.urlencode(params)
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class ExecutionEngine:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=settings.rest_base,
            headers={
                "X-MBX-APIKEY": settings.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10.0,
        )
        self._dry_run = not settings.api_key  # no key = dry run

    async def _signed_post(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = _sign(params, settings.api_secret)
        r = await self._client.post(path, data=params)
        r.raise_for_status()
        return r.json()

    async def _signed_get(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        params["signature"] = _sign(params, settings.api_secret)
        r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def get_balance(self) -> float:
        """Returns USDT or BUSD balance from testnet."""
        if self._dry_run:
            return 1000.0
        try:
            data = await self._signed_get("/api/v3/account", {})
            non_zero = [
                f"{a['asset']}={a['free']}"
                for a in data.get("balances", [])
                if float(a.get("free", 0)) > 0
            ]
            if non_zero:
                log.info("Testnet balances: %s", ", ".join(non_zero))
            else:
                log.warning("No non-zero balances found on testnet account")
            for target in ("USDT", "BUSD"):
                for asset in data.get("balances", []):
                    if asset["asset"] == target:
                        bal = float(asset["free"])
                        if bal > 0:
                            log.info("Using %s balance: %.2f", target, bal)
                            return bal
            log.warning("No stablecoin balance found, returning 1000.0")
            return 1000.0
        except Exception as e:
            log.error("Balance fetch failed: %s", e)
            return 1000.0

    async def place_market_order(
        self,
        symbol: str,
        side: str,   # 'BUY' | 'SELL'
        qty: float,
    ) -> dict:
        if self._dry_run:
            log.info("[DRY-RUN] %s %s qty=%.4f", side, symbol, qty)
            return {"orderId": f"dry_{int(time.time())}", "status": "FILLED"}

        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{qty:.4f}",
        }
        try:
            result = await self._signed_post("/api/v3/order", params)
            log.info("ORDER: %s %s qty=%.4f → id=%s status=%s",
                     side, symbol, qty, result.get("orderId"), result.get("status"))
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
            log.info("[DRY-RUN] LIMIT %s %s qty=%.4f @ %.4f", side, symbol, qty, price)
            return {"orderId": f"dry_{int(time.time())}", "status": "NEW"}

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.4f}",
            "price": f"{price:.4f}",
        }
        try:
            result = await self._signed_post("/api/v3/order", params)
            log.info("LIMIT ORDER: %s %s qty=%.4f @ %.4f → id=%s",
                     side, symbol, qty, price, result.get("orderId"))
            return result
        except Exception as e:
            log.error("Limit order failed: %s", e)
            return {}

    async def cancel_order(self, symbol: str, order_id: str):
        if self._dry_run:
            return
        params = {
            "symbol": symbol,
            "orderId": order_id,
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        import urllib.parse, hashlib, hmac
        query = urllib.parse.urlencode(params)
        params["signature"] = hmac.new(
            settings.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        try:
            r = await self._client.delete("/api/v3/order", params=params)
            r.raise_for_status()
        except Exception as e:
            log.warning("Cancel order failed: %s", e)

    async def close(self):
        await self._client.aclose()
