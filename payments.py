import os
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv

from payment_safety import MIN_GATEWAY_AMOUNT, checked_amount

load_dotenv(Path(__file__).with_name(".env"))


class Zarinpal:
    """درگاه زرین‌پال. مرچنت ابتدا از تنظیم دیتابیس (zarinpal_merchant_id)
    خوانده می‌شود و در نبود آن از متغیر محیطی ZARINPAL_MERCHANT_ID استفاده
    می‌شود — هم‌اهنگ با ربات تلگرام اتومیک که مرچنت را بدون استقرار مجدد
    تغییر می‌دهد."""

    def __init__(self, settings_getter=None):
        self._settings_getter = settings_getter
        self._env_merchant = os.getenv("ZARINPAL_MERCHANT_ID", "").strip()
        self.sandbox = os.getenv("ZARINPAL_SANDBOX", "0") == "1"
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None

    async def merchant(self) -> str:
        value = ""
        if self._settings_getter is not None:
            try:
                value = await self._settings_getter("zarinpal_merchant_id", "")
            except (KeyError, TypeError, ValueError):
                value = ""
        if not str(value or "").strip():
            value = self._env_merchant
        return str(value or "").strip()

    @property
    def api_base(self):
        host = "sandbox.zarinpal.com" if self.sandbox else "payment.zarinpal.com"
        return f"https://{host}/pg/v4/payment"

    @property
    def start_base(self):
        host = "sandbox.zarinpal.com" if self.sandbox else "payment.zarinpal.com"
        return f"https://{host}/pg/StartPay/"

    async def _post(self, path: str, payload: dict) -> dict:
        if self.session is None or self.session.closed:
            await self.start()
        try:
            async with self.session.post(
                f"{self.api_base}/{path}", json=payload
            ) as response:
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    return {"_transport_error": True}
                if response.status >= 500:
                    result["_transport_error"] = True
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return {"_transport_error": True}

    async def request(self, amount, description, callback_url):
        merchant = await self.merchant()
        if not merchant:
            return None, None, "مرچنت زرین‌پال تنظیم نشده است."
        amount = checked_amount(amount, minimum=MIN_GATEWAY_AMOUNT)
        parsed = urlparse(callback_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None, None, "آدرس بازگشت معتبر نیست."
        result = await self._post(
            "request.json",
            {
                "merchant_id": merchant,
                "amount": amount,
                "currency": "IRT",
                "description": description[:255],
                "callback_url": callback_url,
            },
        )
        if result.get("_transport_error"):
            return None, None, "ارتباط با زرین‌پال برقرار نشد؛ کمی بعد دوباره تلاش کن."
        data = result.get("data") or {}
        if data.get("code") == 100 and data.get("authority"):
            authority = str(data["authority"])
            return authority, self.start_base + authority, None
        errors = result.get("errors")
        if isinstance(errors, dict):
            message = errors.get("message") or str(errors)
        elif isinstance(errors, list) and errors:
            first = errors[0]
            message = (
                first.get("message") if isinstance(first, dict) else str(first)
            )
        else:
            message = ""
        if not message:
            message = f"ساخت لینک پرداخت ناموفق بود (کد {data.get('code') or '-'})."
        return None, None, message[:300]

    async def verify(self, amount, authority):
        merchant = await self.merchant()
        if not merchant or not authority:
            return "invalid", None
        amount = checked_amount(amount, minimum=MIN_GATEWAY_AMOUNT)
        result = await self._post(
            "verify.json",
            {
                "merchant_id": merchant,
                "amount": amount,
                "authority": str(authority),
            },
        )
        if result.get("_transport_error"):
            return "unavailable", None
        data = result.get("data") or {}
        if data.get("code") in (100, 101) and data.get("ref_id"):
            return "verified", str(data["ref_id"])
        if data.get("code") in {-2, 21} or result.get("errors"):
            # -2/21 = تراکنش یافت نشد / مرچنت یا authority نادرست.
            return "not_paid", None
        return "not_paid", None
