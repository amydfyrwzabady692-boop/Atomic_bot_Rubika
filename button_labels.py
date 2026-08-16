import re

ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?::[A-Za-z0-9._-]+)*$")

USER_BUTTON_LABELS = {
    "🎮 محصولات فری‌فایر": "gems",
    "💎 خرید جم": "gems",
    "💎 جم فری‌فایر": "gems",
    "💰 کیف پول": "wallet",
    "📦 سفارش‌های من": "orders",
    "👤 حساب من": "account",
    "🛍 فروشگاه": "store",
    "🛍 فروشگاه اکانت": "store",
    "🎯 پک سنسیویتی": "sense",
    "🎯 پک سنس": "sense",
    "🎁 ثبت کد": "promo",
    "🎧 پشتیبانی": "support",
    "🧑‍💻 پشتیبانی": "support",
    "📚 راهنما": "help",
    "🆔 شناسه من": "myid",
    "🆔 جم با آیدی · تحویل لحظه‌ای": "gems_by_id",
    "🆔 جم با آیدی": "gems_by_id",
    "جم با آیدی": "gems_by_id",
    "جم با ایدی": "gems_by_id",
    "🔐 جم با اطلاعات · هفتگی / ماهانه": "gems_credentials",
    "🔐 جم با اطلاعات": "gems_credentials",
    "🏠 بازگشت": "home",
    "🏠 منوی کاربر": "home",
    "🔙 منوی اصلی": "home",
    "🔙 روش‌های خرید": "gems",
    "🔙 پک سنس": "sense",
    "🔙 بازگشت به دسته‌ها": "store",
    "🔙 بازگشت به کیف پول": "wallet",
    "✅ بررسی عضویت": "join_request",
    "📱 موبایل": "sense_mobile",
    "🖥 PC": "sense_pc",
    "✏️ مبلغ دلخواه": "wallet_charge",
    "✖️ انصراف از تیکت": "support_cancel",
    "✖️ انصراف از خرید": "gem_cancel",
    "✖️ لغو ارسال رسید": "card_receipt_cancel",
    "❌ انصراف": "cred_cancel",
    "❌ انصراف و حذف اطلاعات": "cred_cancel",
}

ADMIN_BUTTON_LABELS = {
    "📊 آمار کلی": "admin_stats",
    "📊 آمار": "admin_stats",
    "💵 نرخ و سود": "admin_fx",
    "💵 نرخ دلار": "admin_fx",
    "📦 مدیریت محصولات": "admin_products",
    "🗂 دسته‌بندی": "admin_categories",
    "🗂 دسته‌بندی‌ها": "admin_categories",
    "💳 بخش مالی": "admin_finance",
    "💳 امور مالی": "admin_finance",
    "🧾 رسیدها": "admin_receipts",
    "👥 کاربران": "admin_users",
    "💰 شارژ کاربر": "admin_charge",
    "🔎 جستجو": "admin_search",
    "🎧 تیکت‌های پشتیبانی": "admin_support",
    "🎧 پشتیبانی": "admin_support",
    "📣 ارسال پیام": "admin_broadcast",
    "📣 پیام همگانی": "admin_broadcast",
    "🎁 کدها": "admin_codes",
    "⚙️ تنظیمات": "admin_settings",
    "👮 مدیریت مدیران": "admin_admins",
    "👮 مدیران": "admin_admins",
    "🚨 مرکز عملیات": "admin_ops",
    "🛍 مدیریت فروشگاه": "admin_shop",
    "🛍 فروشگاه": "admin_shop",
    "📦 سفارش‌ها": "admin_orders",
    "🔄 بروزرسانی قیمت جم": "admin_pricing_sync",
    "🔄 بروزرسانی قیمت": "admin_pricing_sync",
    "🔄 همگام‌سازی قیمت‌ها": "admin_pricing_sync",
    "🔄 sync جم با اطلاعات": "admin_pricing_sync",
    "📈 درصد سود": "admin_pricing_home",
    "📈 درصد سود جم": "admin_set_gem_id_profit",
    "📅 سود هفتگی": "admin_cred_set_weekly_profit",
    "📆 سود ماهانه": "admin_cred_set_monthly_profit",
    "💱 قیمت‌گذاری جم با اطلاعات": "admin_pricing_home",
    "💱 قیمت‌گذاری هفتگی/ماهانه": "admin_pricing_home",
    "📈 قیمت و سود": "admin_pricing_home",
    "🔙 قیمت و سود": "admin_pricing_home",
    "✏️ سود جم": "admin_set_gem_id_profit",
    "✏️ سود هفتگی": "admin_cred_set_weekly_profit",
    "✏️ سود ماهانه": "admin_cred_set_monthly_profit",
    "✏️ $ هفتگی": "admin_cred_set_weekly_cost",
    "✏️ $ ماهانه": "admin_cred_set_monthly_cost",
    "🔐 جم با اطلاعات": "cred_admin_home",
    "🛠 پنل مدیریت": "admin_panel",
}

CRED_STAFF_BUTTON_LABELS = {
    "🔐 پنل جم با اطلاعات": "cred_admin_home",
    "📦 سفارش‌های آماده": "cred_admin_list",
    "🎫 تیکت‌ها": "cred_admin_tickets",
}


def apply_button_label_map(mapping: dict, button_id: str, text: str, current: str) -> str:
    """Map visible keypad text to an action, without overriding a real button id."""
    button_id = (button_id or "").strip()
    text = (text or "").strip()
    if button_id and ACTION_ID_RE.fullmatch(button_id) and button_id not in mapping:
        return current
    for candidate in (button_id, text, current):
        mapped = mapping.get(candidate)
        if mapped:
            return mapped
    return current
