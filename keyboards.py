def button(button_id: str, text: str) -> dict:
    return {"id": button_id, "type": "Simple", "button_text": text}


def link_button(button_id: str, text: str, url: str) -> dict:
    if not url.startswith(("https://", "http://")):
        raise ValueError("Link button URL must be absolute")
    # Rubika's Bot API expects the URL itself in ``id`` for Link buttons.
    # ``button_link`` is a Telegram-style shape and makes send_message fail.
    return {"id": url, "type": "Link", "button_text": text}


def _build_button(item: tuple[str, str] | dict) -> dict:
    return item if isinstance(item, dict) else button(*item)


def keypad(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "rows": [{"buttons": [button(button_id, text) for button_id, text in row]} for row in rows],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def main_menu() -> dict:
    return keypad(
        [
            [("gems", "🎮 محصولات فری‌فایر"), ("wallet", "💰 کیف پول")],
            [("orders", "📦 سفارش‌های من"), ("account", "👤 حساب من")],
            [("store", "🛍 فروشگاه اکانت"), ("sense", "🎯 پک سنس")],
            [("promo", "🎁 ثبت کد"), ("support", "🎧 پشتیبانی")],
        ]
    )


def admin_menu() -> dict:
    return keypad(
        [
            [("admin_ops", "🚨 مرکز عملیات"), ("admin_stats", "📊 آمار کلی")],
            [("admin_users", "👥 کاربران"), ("admin_charge", "💰 شارژ کاربر")],
            [("admin_shop", "🛍 مدیریت فروشگاه"), ("admin_finance", "💳 امور مالی")],
            [("admin_receipts", "🧾 رسیدها"), ("admin_orders", "📦 سفارش‌ها")],
            [("admin_support", "🎧 پشتیبانی"), ("admin_broadcast", "📣 پیام همگانی")],
            [("admin_sync_prices", "🔄 بروزرسانی قیمت جم"), ("admin_fx", "💵 نرخ و سود")],
            [("admin_settings", "⚙️ تنظیمات"), ("admin_admins", "👮 مدیریت مدیران")],
            [("home", "🏠 منوی کاربر")],
        ]
    )


def inline(rows: list[list[tuple[str, str] | dict]]) -> dict:
    return {
        "rows": [{"buttons": [_build_button(item) for item in row]} for row in rows]
    }
