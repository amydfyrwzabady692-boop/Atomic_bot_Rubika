"""تنظیم یک‌باره توضیحات و منوی ربات روبیکا.

اجرا:  python setup_bot.py
باید در مسیر پروژه روبیکا اجرا شود تا .env خوانده شود.
"""
import asyncio
import logging

import aiohttp

from config import Settings
from rubika_api import RubikaAPI, RubikaAPIError

logging.basicConfig(level=logging.INFO)


DESCRIPTION = """🎮 اتومیک شاپ روبیکا
⚛️ Atomic Shop

✨ خرید جم فری‌فایر با بهترین قیمت و تحویل آنی
🎯 پک‌های حرفه‌ای سنسیویتی موبایل و PC
💳 پرداخت امن با درگاه زرین‌پال یا کارت‌به‌کارت
💰 کیف پول هوشمند
🎁 کدهای هدیه و تخفیف‌های ویژه
🧑‍💻 پشتیبانی مستقیم و پیگیری سفارش

برای شروع، دکمه Start را بزنید 👇"""


async def main():
    settings = Settings.load()
    api = RubikaAPI(settings.token)
    await api.start()
    try:
        me = await api.get_me()
        bot = me.get("data", {}).get("bot", {}) if isinstance(me.get("data"), dict) else me.get("bot", {})
        print("ربات:", bot.get("bot_title") or bot.get("username") or "؟")
        try:
            result = await api.set_bot_description(DESCRIPTION)
            print("✅ توضیحات ربات تنظیم شد:", result.get("status"))
        except (RubikaAPIError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"⚠️ تنظیم توضیحات ممکن نشد (ربیکا ممکن است از این متد پشتیبانی نکند): {exc}")
        print("ℹ️ منوی پایین (chat_keypad) هنگام /start توسط ربات ارسال می‌شود.")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
