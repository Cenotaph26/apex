"""
ExecutionEngine — Binance Futures Demo (testnet.binancefuture.com).

Key fixes vs previous version:
- Correct Futures endpoints: /fapi/v1/order
- STOP_MARKET orders for stop-loss (exchange-side, not bot-side polling)
- TAKE_PROFIT_MARKET orders for TP
- positionSide always BOTH (one-way mode)
- reduceOnly=true for SL/TP orders
- Detailed error logging
- exchange_info cache for lot sizes
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
        self._offset_ms: int = 0
        self._offset_fetched_at: float = 0.0

        mode = "Futures Demo" if settings.is_futures_demo else "Spot Testnet"
        if self._dry_run:
            log.warning("No API key — DRY-RUN mode (no real orders)")
        else:
            log.info("ExecutionEngine [%s] base=%s key=...%s",
                     mode, settings.rest_base, settings.api_key[-6:])

    # ── Time sync ─────────────────────────────────────────────

    async def _sync_time(self):
        path = "/fapi/v1/time" if settings.is_futures_demo else "/api/v3/time"
        try:
            r = await self._client.get(path, timeout=5.0)
            server_ms = r.json()["serverTime"]
            self._offset_ms = server_ms - int(time.time() * 1000)
            self._offset_fetched_at = time.time()
            log.info("Clock offset: %+d ms", self._offset_ms)
        except Exception as e:
            log.warning("Time sync failed: %s", e)
            self._offset_ms = 0
            self._offset_fetched_at = time.time()

    async def _get_offset(self) -> int:
        if time.time() - self._offset_fetched_at > _OFFSET_TTL:
            await self._sync_time()
        return self._offset_ms

    # ── Signed requests ───────────────────────────────────────

    def _build_qs(self, params: dict) -> str:
        return urllib.parse.urlencode(params)

    async def _signed_get(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
        p["recvWindow"] = 60000
        qs = self._build_qs(p)
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
        qs = self._build_qs(p)
        p["signature"]  = _sign(qs, settings.api_secret)
        body = urllib.parse.urlencode(p).encode()
        r = await self._client.post(
            path, content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not r.is_success:
            log.error("POST %s → %d: %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
        return r.json()

    async def _signed_delete(self, path: str, params: dict | None = None) -> dict:
        p = dict(params or {})
        p["timestamp"]  = int(time.time() * 1000) + await self._get_offset()
        p["recvWindow"] = 60000
        qs = self._build_qs(p)
        p["signature"]  = _sign(qs, settings.api_secret)
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
                data = await self._signed_get("/fapi/v2/balance")
                non_zero = [
                    f"{a['asset']}={float(a.get('availableBalance', 0)):.2f}"
                    for a in data
                    if float(a.get("availableBalance", 0)) > 0
                ]
                log.info("Futures Demo balances: %s", ", ".join(non_zero))
                for a in data:
                    if a.get("asset") == "USDT":
                        bal = float(a.get("availableBalance", 0))
                        log.info("Futures Demo USDT balance: %.2f", bal)
                        return bal
            else:
                data = await self._signed_get("/api/v3/account")
                for a in data.get("balances", []):
                    if a["asset"] in ("USDT", "BUSD"):
                        bal = float(a["free"])
                        if bal > 0:
                            return bal
            return 1000.0
        except Exception as e:
            log.error("Balance fetch error: %s", e)
            return 1000.0

    # ── Set leverage (call once per symbol before trading) ────

    async def set_leverage(self, symbol: str, leverage: int = 1) -> bool:
        """Set leverage for symbol. Returns True on success."""
        if self._dry_run or not settings.is_futures_demo:
            return True
        try:
            await self._signed_post("/fapi/v1/leverage", {
                "symbol":   symbol,
                "leverage": leverage,
            })
            log.info("Leverage set: %s x%d", symbol, leverage)
            return True
        except Exception as e:
            log.warning("Leverage set failed %s: %s", symbol, e)
            return False

    # ── Market order ──────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,       # 'BUY' | 'SELL'
        qty: float,
        reduce_only: bool = False,
    ) -> dict:
        if self._dry_run:
            log.info("[DRY-RUN] MARKET %s %s qty=%.4f reduceOnly=%s",
                     side, symbol, qty, reduce_only)
            return {"orderId": f"dry_{int(time.time())}", "status": "FILLED",
                    "avgPrice": "0"}

        if settings.is_futures_demo:
            params: dict = {
                "symbol":   symbol,
                "side":     side,
                "type":     "MARKET",
                "quantity": str(qty),
            }
            if reduce_only:
                params["reduceOnly"] = "true"
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
            log.info("MARKET ORDER: %s %s qty=%.4f id=%s status=%s avgPrice=%s",
                     side, symbol, qty,
                     result.get("orderId"),
                     result.get("status"),
                     result.get("avgPrice", "?"))
            return result
        except Exception as e:
            log.error("Market order FAILED %s %s qty=%.4f: %s", side, symbol, qty, e)
            return {}

    # ── Stop-loss order (exchange-side) ───────────────────────

    async def place_stop_loss(
        self,
        symbol: str,
        side: str,         # opposite of position: SELL for long, BUY for short
        qty: float,
        stop_price: float,
    ) -> dict:
        """Places a STOP_MARKET order on Futures exchange. Exchange triggers it."""
        if self._dry_run:
            log.info("[DRY-RUN] STOP_MARKET %s %s qty=%.4f @ %.4f",
                     side, symbol, qty, stop_price)
            return {"orderId": f"dry_sl_{int(time.time())}"}

        if not settings.is_futures_demo:
            return {}

        # Price precision
        from core.risk import SYMBOL_INFO
        _, _, price_prec = SYMBOL_INFO.get(symbol, (1.0, 1.0, 4))
        sp = round(stop_price, price_prec)

        params = {
            "symbol":           symbol,
            "side":             side,
            "type":             "STOP_MARKET",
            "quantity":         str(qty),
            "stopPrice":        f"{sp:.{price_prec}f}",
            "reduceOnly":       "true",
            "workingType":      "MARK_PRICE",
            "timeInForce":      "GTE_GTC",
        }
        try:
            result = await self._signed_post("/fapi/v1/order", params)
            log.info("STOP_MARKET placed: %s %s stopPrice=%.4f id=%s",
                     side, symbol, sp, result.get("orderId"))
            return result
        except Exception as e:
            log.error("Stop loss FAILED %s %s: %s", side, symbol, e)
            return {}

    # ── Take-profit order (exchange-side) ─────────────────────

    async def place_take_profit(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_price: float,
    ) -> dict:
        """Places a TAKE_PROFIT_MARKET order on Futures exchange."""
        if self._dry_run:
            log.info("[DRY-RUN] TAKE_PROFIT_MARKET %s %s qty=%.4f @ %.4f",
                     side, symbol, qty, stop_price)
            return {"orderId": f"dry_tp_{int(time.time())}"}

        if not settings.is_futures_demo:
            return {}

        from core.risk import SYMBOL_INFO
        _, _, price_prec = SYMBOL_INFO.get(symbol, (1.0, 1.0, 4))
        sp = round(stop_price, price_prec)

        params = {
            "symbol":      symbol,
            "side":        side,
            "type":        "TAKE_PROFIT_MARKET",
            "quantity":    str(qty),
            "stopPrice":   f"{sp:.{price_prec}f}",
            "reduceOnly":  "true",
            "workingType": "MARK_PRICE",
            "timeInForce": "GTE_GTC",
        }
        try:
            result = await self._signed_post("/fapi/v1/order", params)
            log.info("TAKE_PROFIT placed: %s %s stopPrice=%.4f id=%s",
                     side, symbol, sp, result.get("orderId"))
            return result
        except Exception as e:
            log.error("Take profit FAILED %s %s: %s", side, symbol, e)
            return {}

    # ── Cancel order ──────────────────────────────────────────

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if self._dry_run or not order_id:
            return True
        path = "/fapi/v1/order" if settings.is_futures_demo else "/api/v3/order"
        try:
            await self._signed_delete(path, {
                "symbol":  symbol,
                "orderId": order_id,
            })
            log.info("Order cancelled: %s id=%s", symbol, order_id)
            return True
        except Exception as e:
            log.warning("Cancel failed %s id=%s: %s", symbol, order_id, e)
            return False

    # ── Cancel all open orders for symbol ─────────────────────

    async def cancel_all_orders(self, symbol: str):
        if self._dry_run or not settings.is_futures_demo:
            return
        try:
            await self._signed_delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
            log.info("All orders cancelled: %s", symbol)
        except Exception as e:
            log.warning("Cancel all orders failed %s: %s", symbol, e)

    # ── Get open positions from exchange ──────────────────────

    async def get_open_positions(self) -> list[dict]:
        """Fetch actual open positions from Futures exchange."""
        if self._dry_run or not settings.is_futures_demo:
            return []
        try:
            data = await self._signed_get("/fapi/v2/positionRisk")
            open_pos = [
                p for p in data
                if abs(float(p.get("positionAmt", 0))) > 0
            ]
            return open_pos
        except Exception as e:
            log.error("Get positions failed: %s", e)
            return []

    async def close(self):
        await self._client.aclose()
