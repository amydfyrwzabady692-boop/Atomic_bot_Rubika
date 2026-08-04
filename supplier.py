import asyncio
import os
import re
import time
import threading
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import aiohttp

_inventory_cache = {"at": 0.0, "value": None}
_inventory_refresh_lock = threading.Lock()
_INVENTORY_CACHE_SECONDS = 5 * 60
_FORCED_REFRESH_COALESCE_SECONDS = 30


def g2_idempotency_key(order_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atomic-rubika:order:{order_id}"))


def get_inventory_snapshot(force=False):
    """موجودی دلاری و قیمت زنده کاتالوگ بازی را با کش کوتاه برمی‌گرداند.

    این تابع در `supplier.py` برای هماهنگی با بات تلگرام استفاده می‌شود
    تا قبل از دریافت پول، موجودی G2Bulk بررسی شود.
    خروجی: {'ok': bool, 'balance': Decimal, 'prices': {amount: cost}, ...}
    """
    import asyncio as _asyncio

    loop = _asyncio.new_event_loop()
    try:
        return _asyncio.run(_fetch_inventory_snapshot(force))
    finally:
        loop.close()


async def _fetch_inventory_snapshot(force=False):
    """واقعی‌ترین پیاده‌سازی: از خود کلاینت G2Bulk برای بررسی موجودی استفاده می‌کند."""
    from supplier import G2Bulk

    g2 = G2Bulk()
    await g2.start()
    try:
        data = await g2._call("GET", "/getMe")
        if not data.get("success") or data.get("balance") is None:
            return {
                "ok": False,
                "error": data.get("message") or "دریافت موجودی G2Bulk ناموفق بود.",
            }
        catalogue = await g2._call("GET", f"/games/{g2.game}/catalogue")
        if not catalogue.get("success"):
            return {
                "ok": False,
                "error": catalogue.get("message") or "دریافت کاتالوگ G2Bulk ناموفق بود.",
            }
        try:
            balance = Decimal(str(data["balance"]))
        except (InvalidOperation, TypeError, ValueError):
            return {"ok": False, "error": "موجودی برگشتی G2Bulk معتبر نیست."}

        prices = {}
        names = {}
        prices_by_name = {}
        for item in catalogue.get("catalogues") or []:
            name = str(item.get("name") or "").strip()
            match = re.search(r"\d+", name)
            try:
                package_amount = int(match.group()) if match else None
                cost = Decimal(str(item.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not name or cost <= 0:
                continue
            prices_by_name[_normalise_catalogue_name(name)] = cost
            if package_amount:
                prices[package_amount] = cost
                names[package_amount] = name
        return {
            "ok": True,
            "balance": balance,
            "currency": str(data.get("currency") or "USD"),
            "prices": prices,
            "prices_by_name": prices_by_name,
            "names": names,
            "username": data.get("username") or "",
        }
    finally:
        await g2.close()


def _normalise_catalogue_name(name):
    return " ".join(str(name or "").strip().casefold().split())


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
        self.session: aiohttp.ClientSession | None = None
        self._inventory_cache: dict = {"at": 0.0, "value": None}
        self._inventory_lock = threading.Lock()

    async def inventory_snapshot(self, force=False):
        """موجودی دلاری و قیمت زنده کاتالوگ بازی را با کش کوتاه برمی‌گرداند."""
        if not self.key:
            return {"ok": False, "error": "G2BULK_API_KEY تنظیم نشده است."}
        import time as _time

        now = _time.monotonic()
        cached = self._inventory_cache.get("value")
        if cached and now - self._inventory_cache.get("at", 0) < _INVENTORY_CACHE_SECONDS:
            return cached
        with self._inventory_lock:
            now = _time.monotonic()
            cached = self._inventory_cache.get("value")
            if cached and (
                (not force and now - self._inventory_cache.get("at", 0) < _INVENTORY_CACHE_SECONDS)
                or (force and cached.get("ok")
                    and now - self._inventory_cache.get("at", 0) < _FORCED_REFRESH_COALESCE_SECONDS)
            ):
                return cached
            data = await self._call("GET", "/getMe")
            if not data.get("success") or data.get("balance") is None:
                result = {
                    "ok": False,
                    "error": data.get("message") or "دریافت موجودی G2Bulk ناموفق بود.",
                }
                self._inventory_cache.update(at=now, value=result)
                return result
            catalogue = await self._call("GET", f"/games/{self.game}/catalogue")
            if not catalogue.get("success"):
                result = {
                    "ok": False,
                    "error": catalogue.get("message") or "دریافت کاتالوگ G2Bulk ناموفق بود.",
                }
                self._inventory_cache.update(at=now, value=result)
                return result
            try:
                balance = Decimal(str(data["balance"]))
            except (InvalidOperation, TypeError, ValueError):
                result = {"ok": False, "error": "موجودی برگشتی G2Bulk معتبر نیست."}
                self._inventory_cache.update(at=now, value=result)
                return result
            prices = {}
            names = {}
            prices_by_name = {}
            for item in catalogue.get("catalogues") or []:
                name = str(item.get("name") or "").strip()
                match = re.search(r"\d+", name)
                try:
                    package_amount = int(match.group()) if match else None
                    cost = Decimal(str(item.get("amount")))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if not name or cost <= 0:
                    continue
                prices_by_name[_normalise_catalogue_name(name)] = cost
                if package_amount:
                    prices[package_amount] = cost
                    names[package_amount] = name
            result = {
                "ok": True,
                "balance": balance,
                "currency": str(data.get("currency") or "USD"),
                "prices": prices,
                "prices_by_name": prices_by_name,
                "names": names,
                "username": data.get("username") or "",
            }
            self._inventory_cache.update(at=now, value=result)
            return result

    async def can_fulfill(self, amount, catalogue_name="", force=False):
        """بررسی می‌کند یک بسته با موجودی فعلی حساب API قابل سفارش است."""
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return False, None, None, "مقدار بسته نامعتبر است."
        snapshot = await self.inventory_snapshot(force=force)
        if not snapshot.get("ok"):
            return False, None, None, snapshot.get("error")
        cost = None
        if catalogue_name:
            cost = snapshot.get("prices_by_name", {}).get(
                _normalise_catalogue_name(catalogue_name)
            )
        if cost is None:
            cost = snapshot.get("prices", {}).get(amount)
        if cost is None and catalogue_name:
            match = re.search(r"\d+", str(catalogue_name))
            if match:
                cost = snapshot.get("prices", {}).get(int(match.group()))
        if cost is None:
            return False, None, snapshot["balance"], (
                "بسته در کاتالوگ زنده API پیدا نشد."
            )
        available = Decimal(str(snapshot["balance"])) >= Decimal(str(cost))
        if not available:
            return False, cost, snapshot["balance"], (
                "موجودی سرویس تأمین برای این بسته کافی نیست."
            )
        return True, cost, snapshot["balance"], None

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def _call(self, method, path, body=None, idem=None):
        headers = {"Accept": "application/json", "X-API-Key": self.key}
        if idem:
            headers["X-Idempotency-Key"] = idem
        if self.session is None or self.session.closed:
            await self.start()
        try:
            async with self.session.request(
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

    async def catalogue(self):
        """برگشت کاتالوگ زنده G2Bulk با هزینه دلاری هر بسته.

        خروجی: {'ok': bool, 'items': [{name, cost_usd, amount}], 'error'?}
        """
        if not self.key:
            return {"ok": False, "error": "G2BULK_API_KEY تنظیم نشده است."}
        data = await self._call("GET", f"/games/{self.game}/catalogue")
        if not data.get("success"):
            return {
                "ok": False,
                "error": data.get("message") or "دریافت کاتالوگ G2Bulk ناموفق بود.",
            }
        items = []
        for item in data.get("catalogues") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                cost = Decimal(str(item.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if cost <= 0:
                continue
            match = __import__("re").search(r"\d+", name)
            amount = int(match.group()) if match else None
            items.append({"name": name, "cost_usd": cost, "amount": amount})
        return {"ok": True, "items": items}


# A module-level G2Bulk client used by the inventory convenience functions.
# It shares a single HTTP session so concurrent preflight checks coalesce.
_module_g2: G2Bulk | None = None


async def async_get_inventory_snapshot(force=False):
    """Async wrapper around a module-level G2Bulk instance for inventory."""
    global _module_g2
    if _module_g2 is None:
        _module_g2 = G2Bulk()
    if _module_g2.session is None or _module_g2.session.closed:
        await _module_g2.start()
    return await _module_g2.inventory_snapshot(force=force)


async def can_fulfill(amount, catalogue_name='', force=False):
    """Async version of G2Bulk.can_fulfill (module-level convenience).

    خروجی: (available, cost_usd, balance_usd, error)
    - available: True اگر موجودی کافی باشد
    - cost_usd: هزینه دلاری بسته
    - balance_usd: موجودی فعلی حساب G2Bulk
    - error: پیام خطا در صورت مشکل
    """
    global _module_g2
    if _module_g2 is None:
        _module_g2 = G2Bulk()
    if _module_g2.session is None or _module_g2.session.closed:
        await _module_g2.start()
    return await _module_g2.can_fulfill(amount, catalogue_name, force=force)


async def compute_gem_sale_price(cost_usd, usd_toman_rate_value, profit_percent=7):
    """قیمت فروش هر بسته جم با سود مشخص.

    price_toman = ceil(cost_usd * rate * (1 + profit_percent/100) / 1000) * 1000
    یعنی همیشه به نزدیک‌ترین هزار تومان بالاتر گرد می‌شود.
    """
    cost_usd = Decimal(str(cost_usd))
    rate = Decimal(str(usd_toman_rate_value))
    profit = Decimal(1) + (Decimal(profit_percent) / Decimal(100))
    raw = (cost_usd * rate * profit).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return (int(raw) // 1000 + (1 if int(raw) % 1000 else 0)) * 1000
