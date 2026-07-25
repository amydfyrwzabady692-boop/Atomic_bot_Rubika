import os
import uuid
from decimal import ROUND_HALF_UP, Decimal

import aiohttp


def g2_idempotency_key(order_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atomic-rubika:order:{order_id}"))


async def usd_toman_rate():
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
            manual = int(os.getenv("USD_TOMAN_RATE", "").replace(",", ""))
        except ValueError:
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
                    return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            return {"success": False, "message": str(exc)}

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
            return {
                "ok": True,
                "idempotency_key": idem,
                "provider_order_id": str(order.get("order_id") or ""),
                "status": str(order.get("status") or "PENDING").upper(),
                "cost_usd": order.get("price"),
                "player_name": order.get("player_name") or "",
            }
        return {
            "ok": False,
            "idempotency_key": idem,
            "error": data.get("message") or "خطای تامین‌کننده",
        }

    async def status(self, provider_order_id):
        data = await self._call(
            "POST",
            "/games/order/status",
            {"order_id": str(provider_order_id)},
        )
        order = data.get("order") if isinstance(data.get("order"), dict) else {}
        status = str(order.get("status") or data.get("status") or "").upper()
        if status == "CANCELED":
            status = "FAILED"
        if status in {"PENDING", "PROCESSING", "COMPLETED", "FAILED"}:
            return {"ok": True, "status": status}
        return {"ok": False, "error": data.get("message") or "وضعیت نامعتبر"}
