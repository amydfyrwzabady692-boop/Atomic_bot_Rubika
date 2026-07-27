def button(button_id: str, text: str) -> dict:
    return {"id": button_id, "type": "Simple", "button_text": text}


def link_button(button_id: str, text: str, url: str) -> dict:
    if not url.startswith(("https://", "http://")):
        raise ValueError("Link button URL must be absolute")
    return {
        "id": button_id,
        "type": "Link",
        "button_text": text,
        "button_link": {"type": "url", "link_url": url},
    }


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
            [("admin_stats", "📊 آمار کلی"), ("admin_fx", "💵 نرخ و سود")],
            [
                ("admin_products", "📦 مدیریت محصولات"),
                ("admin_categories", "🗂 دسته‌بندی"),
            ],
            [("admin_finance", "💳 بخش مالی"), ("admin_receipts", "🧾 رسیدها")],
            [("admin_users", "👥 کاربران"), ("admin_search", "🔎 جستجو")],
            [("admin_support", "🎧 پشتیبانی"), ("admin_broadcast", "📣 ارسال پیام")],
            [("admin_codes", "🎁 کدها"), ("admin_settings", "⚙️ تنظیمات")],
            [("admin_admins", "👮 مدیریت مدیران")],
            [("home", "🏠 منوی کاربر")],
        ]
    )


def inline(rows: list[list[tuple[str, str] | dict]]) -> dict:
    return {
        "rows": [{"buttons": [_build_button(item) for item in row]} for row in rows]
    }
