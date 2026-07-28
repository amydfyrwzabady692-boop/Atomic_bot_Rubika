import os
import uuid
from decimal import ROUND_HALF_UP, Decimal

import aiohttp


def g2_idempotency_key(order_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atomic-rubika:order:{order_id}"))


async def usd_toman_rate(manual_rate=None):
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://apiv2.nobitex.ir/v3/orderbook/USDTIRT",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "AtomicRubika/1.0",
                },
            ) as response:
                data = await response.json(content_type=None)
        asks = data.get("asks") or []
        rate = int(
            (Decimal(str(asks[0][0])) / Decimal(10)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
        if not 10_000 <= rate <= 10_000_000:
            raise ValueError("rate out of range")
        return {"ok": True, "rate": rate, "source": "nobitex_usdtirt_best_ask"}
    except (
        aiohttp.ClientError,
        TimeoutError,
        ValueError,
        KeyError,
        IndexError,
        ArithmeticError,
    ) as exc:
        try:
            manual = int(
                str(manual_rate or os.getenv("USD_TOMAN_RATE", "")).replace(",", "")
            )
        except (TypeError, ValueError):
            manual = 0
        if 10_000 <= manual <= 10_000_000:
            return {
                "ok": True,
                "rate": manual,
                "source": "manual_fallback",
                "warning": str(exc),
            }
        return {"ok": False, "error": str(exc)}


class G2Bulk:
    base = "https://api.g2bulk.com/v1"

    def __init__(self):
        self.key = os.getenv("G2BULK_API_KEY", "").strip()
        self.game = os.getenv("G2BULK_GAME_CODE", "freefire_me").strip()

    async def _call(self, method, path, body=None, idem=None):
        headers = {"Accept": "application/json", "X-API-Key": self.key}
        if idem:
            headers["X-Idempotency-Key"] = idem
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method, self.base + path, json=body, headers=headers
                ) as response:
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict):
                        data = {"success": False, "message": str(data)}
                    if (
                        method.upper() != "GET"
                        and (response.status in {408, 429} or response.status >= 500)
                    ):
                        data["_transport_uncertain"] = True
                    return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return {
                "success": False,
                "message": str(exc),
                "_transport_uncertain": method.upper() != "GET",
            }

    async def order(self, sku, player_id, order_id):
        idem = g2_idempotency_key(order_id)
        if not self.key:
            return {
                "ok": False,
                "idempotency_key": idem,
                "error": "G2BULK_API_KEY تنظیم نشده است.",
            }
        data = await self._call(
            "POST",
            f"/games/{self.game}/order",
            {
                "catalogue_name": sku,
                "player_id": player_id,
                "remark": f"rubika-{order_id}",
            },
            idem,
        )
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        if data.get("success") and order:
            provider_order_id = str(order.get("order_id") or "").strip()
            if not provider_order_id:
                return {
                    "ok": False,
                    "uncertain": True,
                    "idempotency_key": idem,
                    "error": "تأمین‌کننده شناسه سفارش برنگرداند.",
                }
            return {
                "ok": True,
                "idempotency_key": idem,
                "provider_order_id": provider_order_id,
                "status": str(order.get("status") or "PENDING").upper(),
                "cost_usd": order.get("price"),
                "player_name": order.get("player_name") or "",
            }
        return {
            "ok": False,
            "uncertain": bool(data.get("_transport_uncertain")),
            "idempotency_key": idem,
            "error": data.get("message") or "خطای تامین‌کننده",
        }

    async def find_order_by_remark(self, remark):
        """Reconcile an ambiguous submission without placing another order."""
        remark = str(remark or "").strip()
        if not self.key or not remark:
            return {"ok": False, "found": False}
        data = await self._call("GET", "/games/orders?page=1&limit=100")
        if not data.get("success"):
            return {
                "ok": False,
                "found": False,
                "error": data.get("message") or "دریافت سفارش‌های تأمین‌کننده ناموفق بود.",
            }
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        for order in data.get("orders") or nested.get("orders") or []:
            if str(order.get("remark") or "").strip() != remark:
                continue
            provider_order_id = str(
                order.get("order_id") or order.get("id") or ""
            ).strip()
            if not provider_order_id:
                continue
            status = str(order.get("status") or "PENDING").upper()
            if status == "CANCELED":
                status = "FAILED"
            return {
                "ok": True,
                "found": True,
                "provider_order_id": provider_order_id,
                "status": status,
                "player_name": order.get("player_name") or "",
                "cost_usd": order.get("price") or order.get("total_price"),
            }
        return {"ok": True, "found": False}

    async def check_player(self, player_id: str):
        if not self.key:
            return {"ok": False, "error": "سرویس بررسی آیدی تنظیم نشده است."}
        data = await self._call(
            "POST",
            "/games/checkPlayerId",
            {"game": self.game, "user_id": str(player_id)},
        )
        valid = str(data.get("valid") or "").strip().lower()
        if valid == "valid":
            return {
                "ok": True,
                "name": str(data.get("name") or "بازیکن"),
            }
        return {
            "ok": False,
            "error": data.get("message") or data.get("error") or "آیدی معتبر نیست.",
        }

    async def status(self, provider_order_id):
        provider_id = str(provider_order_id or "").strip()
        if not self.key or not provider_id:
            return {"ok": False, "error": "شناسه سفارش یا API key موجود نیست."}

        valid_statuses = {
            "PENDING",
            "PROCESSING",
            "COMPLETED",
            "FAILED",
            "CANCELED",
            "CANCELLED",
            "REFUNDED",
        }

        def extract(response):
            nested = (
                response.get("data")
                if isinstance(response.get("data"), dict)
                else {}
            )
            order = (
                response.get("order")
                if isinstance(response.get("order"), dict)
                else {}
            )
            if not order and isinstance(nested.get("order"), dict):
                order = nested["order"]
            if not order and nested.get("status"):
                order = nested
            status_value = str(
                order.get("status")
                or nested.get("status")
                or response.get("status")
                or ""
            ).strip().upper()
            player_name = str(
                order.get("player_name")
                or nested.get("player_name")
                or response.get("player_name")
                or ""
            )
            return status_value, player_name

        request_id = int(provider_id) if provider_id.isdigit() else provider_id
        data = await self._call(
            "POST",
            "/games/order/status",
            {"order_id": request_id},
        )
        status, player_name = extract(data)

        if status not in valid_statuses and provider_id.isdigit():
            alternate = await self._call(
                "POST",
                "/games/order/status",
                {"order_id": provider_id},
            )
            alternate_status, alternate_player_name = extract(alternate)
            if alternate_status:
                data = alternate
                status = alternate_status
                player_name = alternate_player_name

        if status in valid_statuses:
            if status in {"CANCELED", "CANCELLED", "REFUNDED"}:
                status = "FAILED"
            return {
                "ok": True,
                "status": status,
                "player_name": player_name,
            }

        # The status endpoint has returned different response shapes in
        # production. Reconcile locally against the read-only order history;
        # this path can never create or duplicate a supplier order.
        history = await self._call("GET", "/games/orders?page=1&limit=100")
        nested = (
            history.get("data")
            if isinstance(history.get("data"), dict)
            else {}
        )
        orders = (
            history.get("orders")
            or nested.get("orders")
            or (history.get("data") if isinstance(history.get("data"), list) else [])
            or []
        )
        for item in orders:
            item_id = str(item.get("order_id") or item.get("id") or "").strip()
            if item_id != provider_id:
                continue
            history_status = str(item.get("status") or "").strip().upper()
            if history_status in valid_statuses:
                if history_status in {"CANCELED", "CANCELLED", "REFUNDED"}:
                    history_status = "FAILED"
                return {
                    "ok": True,
                    "status": history_status,
                    "player_name": str(item.get("player_name") or ""),
                }
        return {
            "ok": False,
            "error": data.get("message") or "وضعیت سفارش G2Bulk قابل تشخیص نیست.",
        }
