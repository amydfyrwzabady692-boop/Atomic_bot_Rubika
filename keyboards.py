def button(button_id: str, text: str) -> dict:
    return {"id": button_id, "type": "Simple", "button_text": text}


def keypad(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "rows": [{"buttons": [button(button_id, text) for button_id, text in row]} for row in rows],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def main_menu() -> dict:
    return keypad(
        [
            [("gems", "💎 خرید جم"), ("sense", "🎯 پک سنسیویتی")],
            [("store", "🛍 فروشگاه"), ("wallet", "💰 کیف پول")],
            [("orders", "📦 سفارش‌های من"), ("account", "👤 حساب من")],
            [("promo", "🎁 ثبت کد"), ("support", "🧑‍💻 پشتیبانی")],
            [("help", "📚 راهنما")],
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
            [("home", "🏠 منوی کاربر")],
        ]
    )


def inline(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "rows": [{"buttons": [button(button_id, text) for button_id, text in row]} for row in rows]
    }
