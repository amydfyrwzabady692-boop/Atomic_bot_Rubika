import os
from urllib.parse import urlparse

import aiohttp

from payment_safety import MIN_GATEWAY_AMOUNT, checked_amount


class Zarinpal:
    def __init__(self):
        self.merchant = os.getenv("ZARINPAL_MERCHANT_ID", "").strip()
        self.sandbox = os.getenv("ZARINPAL_SANDBOX", "0") == "1"

    @property
    def api_base(self):
        host = "sandbox.zarinpal.com" if self.sandbox else "payment.zarinpal.com"
        return f"https://{host}/pg/v4/payment"

    @property
    def start_base(self):
        host = "sandbox.zarinpal.com" if self.sandbox else "payment.zarinpal.com"
        return f"https://{host}/pg/StartPay/"

    async def _post(self, path: str, payload: dict) -> dict:
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.api_base}/{path}", json=payload) as response:
                    result = await response.json(content_type=None)
                    return result if isinstance(result, dict) else {}
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return {"_transport_error": True}

    async def request(self, amount, description, callback_url):
        if not self.merchant:
            return None, None, "مرچنت زرین‌پال تنظیم نشده است."
        amount = checked_amount(amount, minimum=MIN_GATEWAY_AMOUNT)
        parsed = urlparse(callback_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None, None, "آدرس بازگشت معتبر نیست."
        result = await self._post(
            "request.json",
            {
                "merchant_id": self.merchant,
                "amount": amount,
                "currency": "IRT",
                "description": description[:255],
                "callback_url": callback_url,
            },
        )
        data = result.get("data") or {}
        if data.get("code") == 100 and data.get("authority"):
            authority = str(data["authority"])
            return authority, self.start_base + authority, None
        return None, None, "ساخت لینک پرداخت ناموفق بود."

    async def verify(self, amount, authority):
        if not self.merchant or not authority:
            return "invalid", None
        amount = checked_amount(amount, minimum=MIN_GATEWAY_AMOUNT)
        result = await self._post(
            "verify.json",
            {
                "merchant_id": self.merchant,
                "amount": amount,
                "authority": str(authority),
            },
        )
        if result.get("_transport_error"):
            return "unavailable", None
        data = result.get("data") or {}
        if data.get("code") in (100, 101) and data.get("ref_id"):
            return "verified", str(data["ref_id"])
        return "not_paid", None
