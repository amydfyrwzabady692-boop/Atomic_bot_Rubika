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


def main_menu(*, is_admin: bool = False, is_cred_staff: bool = False) -> dict:
    rows = [
        [("gems", "🎮 محصولات فری‌فایر"), ("wallet", "💰 کیف پول")],
        [("orders", "📦 سفارش‌های من"), ("account", "👤 حساب من")],
        [("store", "🛍 فروشگاه اکانت"), ("sense", "🎯 پک سنس")],
        [("promo", "🎁 ثبت کد"), ("support", "🎧 پشتیبانی")],
        [("help", "📚 راهنما"), ("myid", "🆔 شناسه من")],
    ]
    if is_admin:
        rows.append([("admin_panel", "🛠 پنل مدیریت")])
    elif is_cred_staff:
        rows.append([("cred_admin_home", "🔐 پنل جم با اطلاعات")])
    return keypad(rows)


def credential_staff_menu() -> dict:
    return keypad(
        [
            [("cred_admin_home", "🔐 پنل جم با اطلاعات")],
            [("cred_admin_list", "📦 سفارش‌های آماده"), ("cred_admin_tickets", "🎫 تیکت‌ها")],
            [("home", "🏠 منوی کاربر")],
        ]
    )


def admin_menu() -> dict:
    """صفحه اول پنل: سود جم + هفتگی/ماهانه + قیمت‌گذاری جم با اطلاعات."""
    return keypad(
        [
            [("admin_set_gem_id_profit", "📈 درصد سود جم")],
            [
                ("admin_cred_set_weekly_profit", "📅 سود هفتگی"),
                ("admin_cred_set_monthly_profit", "📆 سود ماهانه"),
            ],
            [("admin_pricing_home", "💱 قیمت‌گذاری جم با اطلاعات")],
            [("admin_pricing_sync", "🔄 همگام‌سازی قیمت‌ها")],
            [("admin_ops", "🚨 مرکز عملیات"), ("admin_stats", "📊 آمار")],
            [("admin_users", "👥 کاربران"), ("admin_shop", "🛍 فروشگاه")],
            [("admin_finance", "💳 امور مالی"), ("admin_orders", "📦 سفارش‌ها")],
            [("admin_receipts", "🧾 رسیدها"), ("admin_support", "🎧 پشتیبانی")],
            [("admin_broadcast", "📣 پیام همگانی"), ("admin_settings", "⚙️ تنظیمات")],
            [("admin_search", "🔎 جستجو"), ("admin_admins", "👮 مدیران")],
            [("cred_admin_home", "🔐 جم با اطلاعات")],
            [("home", "🏠 منوی کاربر")],
        ]
    )


def inline(rows: list[list[tuple[str, str] | dict]]) -> dict:
    return {
        "rows": [{"buttons": [_build_button(item) for item in row]} for row in rows]
    }


def inline_as_chat_keypad(inline_keypad: dict | None) -> dict | None:
    """Copy glass buttons onto the bottom keypad so polling can receive taps.

    Rubika delivers InlineKeypad clicks only to the ReceiveInlineMessage webhook.
    ChatKeypad taps arrive as ordinary NewMessage updates via getUpdates.
    """
    if not isinstance(inline_keypad, dict):
        return None
    rows = []
    for row in inline_keypad.get("rows") or []:
        if not isinstance(row, dict):
            continue
        buttons = []
        for item in row.get("buttons") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "Simple") == "Link":
                continue
            button_id = str(item.get("id") or "").strip()
            label = str(item.get("button_text") or "").strip()
            if not button_id or not label:
                continue
            buttons.append(button(button_id, label))
        if buttons:
            rows.append({"buttons": buttons})
    if not rows:
        return None
    return {
        "rows": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }
