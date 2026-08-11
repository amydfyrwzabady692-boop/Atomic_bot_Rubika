"""فلو جم با اطلاعات برای ربات روبیکا (مشابه تلگرام)."""
from __future__ import annotations

import logging

from credential_vault import (
    CredentialVaultError,
    decrypt_credentials,
    encrypt_credentials,
    mask_identifier,
)
from keyboards import admin_menu, credential_staff_menu, inline, main_menu

log = logging.getLogger(__name__)

METHOD_META = {
    "google": {
        "label": "Gmail / Google",
        "id_prompt": (
            "📧 مرحله ۱ از ۳ — ایمیل Gmail\n"
            "ایمیل اکانت گوگل متصل به فری‌فایر را بفرست.\n"
            "مثال: name@gmail.com"
        ),
        "pass_prompt": (
            "🔑 مرحله ۲ از ۳ — رمز عبور\n"
            "رمز فعلی اکانت Gmail را بفرست.\n"
            "بعد از تحویل حتماً رمز را عوض کن."
        ),
        "backup_prompt": (
            "🛡 مرحله ۳ از ۳ — کد بک‌آپ Gmail\n"
            "━━━━━━━━━━━━━━━\n"
            "این کد برای ورود ادمین به اکانت لازم است.\n"
            "اول طبق راهنما کد را پیدا کن و اینجا بفرست.\n\n"
            "راهنمای گرفتن کد بک‌آپ گوگل:\n"
            "۱) گوشی یا کامپیوتر → برو به\n"
            "https://myaccount.google.com\n"
            "۲) با همان ایمیلی که به فری‌فایر وصل است وارد شو\n"
            "۳) از سمت چپ بزن: Security یا امنیت\n"
            "۴) پیدا کن: 2-Step Verification / تأیید دو مرحله‌ای\n"
            "   (اگر خاموش بود اول روشن کن)\n"
            "۵) پایین صفحه بزن: Backup codes / کدهای پشتیبان\n"
            "۶) بزن: Get backup codes / دریافت کدها\n"
            "۷) چند کد چندرقمی می‌بینی — همه‌شان یا چند تا را اینجا بفرست\n"
            "   (هر خط یک کد)\n\n"
            "⚠️ کدهای استفاده‌شده را نفرست؛ کد تازه بفرست."
        ),
    },
    "facebook": {
        "label": "Facebook",
        "id_prompt": (
            "📘 مرحله ۱ از ۳ — شناسه Facebook\n"
            "ایمیل، شماره موبایل یا نام‌کاربری فیسبوک متصل به فری‌فایر را بفرست."
        ),
        "pass_prompt": (
            "🔑 مرحله ۲ از ۳ — رمز عبور Facebook\n"
            "رمز ورود فیسبوک را بفرست.\n"
            "بعد از تحویل رمز را عوض کن."
        ),
        "backup_prompt": (
            "🛡 مرحله ۳ از ۳ — کد بک‌آپ Facebook\n"
            "━━━━━━━━━━━━━━━\n"
            "این کد برای ورود ادمین به اکانت لازم است.\n"
            "اول طبق راهنما کد را پیدا کن و اینجا بفرست.\n\n"
            "راهنمای گرفتن کد بک‌آپ فیسبوک:\n"
            "۱) اپ یا سایت فیسبوک را باز کن و وارد همان حساب شو\n"
            "۲) برو به Settings / تنظیمات\n"
            "   یا Accounts Center / مرکز حساب‌ها\n"
            "۳) بزن: Password and security / رمز عبور و امنیت\n"
            "۴) بزن: Two-factor authentication / تأیید دو مرحله‌ای\n"
            "   (اگر خاموش بود اول روشن کن)\n"
            "۵) باز کن: Recovery codes یا Backup codes / کدهای بازیابی\n"
            "۶) کدها را کپی کن و همین‌جا بفرست (هر خط یک کد)"
        ),
    },
    "vk": {
        "label": "VK",
        "id_prompt": (
            "🟣 مرحله ۱ از ۳ — شناسه VK\n"
            "ایمیل، شماره موبایل یا نام‌کاربری VK متصل به فری‌فایر را بفرست."
        ),
        "pass_prompt": (
            "🔑 مرحله ۲ از ۳ — رمز عبور VK\n"
            "رمز ورود VK را بفرست.\n"
            "بعد از تحویل رمز را عوض کن."
        ),
        "backup_prompt": (
            "🛡 مرحله ۳ از ۳ — کد بک‌آپ VK\n"
            "━━━━━━━━━━━━━━━\n"
            "این کد برای ورود ادمین به اکانت لازم است.\n"
            "اول طبق راهنما کد را پیدا کن و اینجا بفرست.\n\n"
            "راهنمای گرفتن کد بک‌آپ VK:\n"
            "۱) اپ یا سایت VK را باز کن و وارد همان حساب شو\n"
            "۲) برو به Settings / Настройки / تنظیمات\n"
            "۳) باز کن: Security / Безопасность / امنیت\n"
            "۴) بزن: Two-step verification / تأیید دو مرحله‌ای\n"
            "   (اگر خاموش بود اول روشن کن)\n"
            "۵) باز کن: Backup codes / کدهای پشتیبان\n"
            "۶) کدها را کپی کن و همین‌جا بفرست (هر خط یک کد)"
        ),
    },
}

BACKUP_FOOTER = (
    "\n\n━━━━━━━━━━━━━━━\n"
    "✅ اگر کد را پیدا کردی → همین‌جا بفرست\n"
    "🆘 اگر بلد نیستی / پیدا نکردی → دکمه زیر را بزن:\n"
    "«نیاز به راهنمایی — بک‌آپ بلد نیستم»\n"
    "بعد پرداخت کن؛ پس از پرداخت موفق پشتیبانی با شماره سفارش کمکت می‌کند."
)


class CredentialHandlers:
    """Mixin-style helpers; Router methods call these with self as router."""

    async def freefire_menu(self, event):
        await self.send(
            event["chat_id"],
            "🎮 محصولات فری‌فایر\n"
            "━━━━━━━━━━━━━━━\n"
            "روش خرید را انتخاب کن:\n\n"
            "🆔 جم با آیدی\n"
            "⚡ تحویل لحظه‌ای · قیمت پایین\n"
            "فقط آیدی بازی را می‌فرستی و جم خودکار واریز می‌شود.\n\n"
            "🔐 جم با اطلاعات\n"
            "📅 عضویت هفتگی و ماهانه\n"
            "با اطلاعات ورود اکانت، توسط پشتیبانی انجام می‌شود.",
            buttons=inline(
                [
                    [("gems_by_id", "🆔 جم با آیدی · تحویل لحظه‌ای")],
                    [("gems_credentials", "🔐 جم با اطلاعات · هفتگی / ماهانه")],
                    [("home", "🔙 منوی اصلی")],
                ]
            ),
        )

    async def credential_products_menu(self, event):
        products = await self.db.products("gem_credentials")
        support = await self.db.get_credential_support_contact()
        text = (
            "🔐 جم با اطلاعات اکانت\n"
            "━━━━━━━━━━━━━━━\n"
            "عضویت هفتگی یا ماهانه را انتخاب کن.\n"
            "بعد از انتخاب: روش ورود ← شناسه ← رمز ← راهنمای بک‌آپ "
            "(اگر بلد نیستی دکمه راهنمایی را بزن).\n\n"
            "راهنمای گرفتن بک‌آپ برای Gmail / Facebook / VK داخل چت می‌آید.\n"
        )
        if support["handle"]:
            text += f"اگر بعد از پرداخت هنوز مشکل داشتی، به {support['handle']} پیام بده."
        rows = []
        for product in products:
            rows.append(
                [
                    (
                        f"cred_product:{product['id']}",
                        f"{product['title']} • {int(product['price']):,} تومان",
                    )
                ]
            )
        if not rows:
            text += "\n\n❌ فعلاً محصول فعالی وجود ندارد."
        rows.append([("gems", "🔙 روش‌های خرید")])
        await self.send(event["chat_id"], text, buttons=inline(rows))

    async def show_credential_product(self, event, product_id: int):
        product = await self.db.pool.fetchrow(
            "SELECT * FROM products WHERE id=$1 AND kind='gem_credentials' AND active",
            product_id,
        )
        if not product:
            await self.send(event["chat_id"], "❌ محصول پیدا نشد یا غیرفعال شده است.")
            return
        sku = str(product.get("supplier_sku") or "")
        plan = "هفتگی" if "weekly" in sku else "ماهانه"
        support = await self.db.get_credential_support_contact()
        text = (
            f"🔐 {product['title']}\n"
            "━━━━━━━━━━━━━━━\n"
            f"📅 دوره: {plan}\n"
            f"💰 قیمت: {int(product['price']):,} تومان\n"
            "⏳ تحویل: دستی پس از بررسی اطلاعات توسط ادمین\n\n"
            "مراحل بعدی:\n"
            "۱) انتخاب روش ورود (Gmail / Facebook / VK)\n"
            "۲) شناسه ورود\n"
            "۳) رمز عبور\n"
            "۴) راهنمای بک‌آپ (اگر بلد نیستی: نیاز به راهنمایی)\n"
            "۵) پرداخت\n"
        )
        if support["handle"]:
            text += f"\nاگر بعد از پرداخت مشکل داشتی: {support['handle']}"
        await self.send(
            event["chat_id"],
            text,
            buttons=inline(
                [
                    [(f"cred_buy:{product['id']}", "✅ ادامه و ثبت اطلاعات")],
                    [("gems_credentials", "🔙 بازگشت")],
                ]
            ),
        )

    async def credential_buy_start(self, event, user, product_id: int):
        if await self.db.setting("sales_enabled", "1") != "1":
            await self.send(event["chat_id"], "⛔ فروش موقتاً متوقف شده است.")
            return
        product = await self.db.pool.fetchrow(
            "SELECT * FROM products WHERE id=$1 AND kind='gem_credentials' "
            "AND active AND stock>0",
            product_id,
        )
        if not product:
            await self.send(event["chat_id"], "❌ محصول موجود نیست.")
            return
        await self.db.set_session(
            event["sender_id"],
            "cred_method",
            {
                "product_id": int(product["id"]),
                "title": product["title"],
                "price": int(product["price"]),
            },
        )
        await self.send(
            event["chat_id"],
            "روش ورود اکانت فری‌فایر را انتخاب کن:",
            buttons=inline(
                [
                    [("cred_method:google", "📧 Gmail / Google")],
                    [("cred_method:facebook", "📘 Facebook")],
                    [("cred_method:vk", "🟣 VK")],
                    [("cred_cancel", "❌ انصراف")],
                ]
            ),
        )

    async def credential_method_selected(self, event, method: str):
        state, data = await self.db.session(event["sender_id"])
        if state != "cred_method" or method not in METHOD_META:
            await self.send(event["chat_id"], "جلسه منقضی شده؛ دوباره شروع کن.")
            return
        data["method"] = method
        await self.db.set_session(event["sender_id"], "cred_identifier", data)
        await self.send(
            event["chat_id"],
            METHOD_META[method]["id_prompt"],
            buttons=inline([[("cred_cancel", "❌ انصراف")]]),
        )

    async def credential_prompt_current_step(self, event, state, data):
        """اگر کاربر در مرحله انتخاب دکمه متن فرستاد، همان مرحله را یادآوری کن."""
        if state == "cred_method":
            await self.send(
                event["chat_id"],
                "لطفاً یکی از دکمه‌های روش ورود را بزن:",
                buttons=inline(
                    [
                        [("cred_method:google", "📧 Gmail / Google")],
                        [("cred_method:facebook", "📘 Facebook")],
                        [("cred_method:vk", "🟣 VK")],
                        [("cred_cancel", "❌ انصراف")],
                    ]
                ),
            )
            return True
        if state == "cred_confirm":
            await self._credential_show_confirm(event, data)
            return True
        return False

    async def credential_handle_state(self, event, user, state, data):
        text = (event.get("text") or "").strip()
        if state == "cred_identifier":
            method = data.get("method")
            if not method or method not in METHOD_META or not text or len(text) > 200:
                await self.send(event["chat_id"], "شناسه نامعتبر است؛ دوباره بفرست.")
                return True
            data["identifier"] = text
            await self.db.set_session(event["sender_id"], "cred_password", data)
            await self.send(
                event["chat_id"],
                METHOD_META[method]["pass_prompt"],
                buttons=inline([[("cred_cancel", "❌ انصراف")]]),
            )
            return True
        if state == "cred_password":
            method = data.get("method")
            if method not in METHOD_META:
                await self.send(event["chat_id"], "جلسه منقضی شده؛ دوباره شروع کن.")
                await self.db.set_session(event["sender_id"])
                return True
            if not (6 <= len(text) <= 200) or "\n" in text:
                await self.send(
                    event["chat_id"], "رمز باید بین ۶ تا ۲۰۰ کاراکتر و تک‌خطی باشد."
                )
                return True
            data["password"] = text
            await self.db.set_session(event["sender_id"], "cred_backup", data)
            await self.send(
                event["chat_id"],
                METHOD_META[method]["backup_prompt"] + BACKUP_FOOTER,
                buttons=inline(
                    [
                        [("cred_backup_skip", "🆘 نیاز به راهنمایی — بک‌آپ بلد نیستم")],
                        [("cred_cancel", "❌ انصراف")],
                    ]
                ),
            )
            return True
        if state == "cred_backup":
            if len(text) < 4 or len(text) > 800:
                await self.send(
                    event["chat_id"],
                    "اگر کد داری بین ۴ تا ۸۰۰ کاراکتر بفرست.\n"
                    "اگر بلد نیستی، دکمه نیاز به راهنمایی را بزن.",
                    buttons=inline(
                        [
                            [("cred_backup_skip", "🆘 نیاز به راهنمایی — بک‌آپ بلد نیستم")],
                            [("cred_cancel", "❌ انصراف")],
                        ]
                    ),
                )
                return True
            data["backup_code"] = text
            data["backup_skipped"] = False
            await self.db.set_session(event["sender_id"], "cred_confirm", data)
            await self._credential_show_confirm(event, data)
            return True
        return False

    async def credential_backup_skip(self, event):
        state, data = await self.db.session(event["sender_id"])
        if state != "cred_backup":
            await self.send(event["chat_id"], "جلسه منقضی شده.")
            return
        data["backup_code"] = ""
        data["backup_skipped"] = True
        await self.db.set_session(event["sender_id"], "cred_confirm", data)
        await self._credential_show_confirm(event, data)

    async def _credential_show_confirm(self, event, data):
        method = METHOD_META.get(data.get("method"), {}).get("label", data.get("method"))
        has_backup = bool(str(data.get("backup_code") or "").strip())
        backup_line = "ثبت شد ✅" if has_backup else "نیاز به راهنمایی / ارسال نشده 🆘"
        text = (
            "✅ بازبینی اطلاعات\n"
            "━━━━━━━━━━━━━━━\n"
            f"محصول: {data.get('title')}\n"
            f"روش ورود: {method}\n"
            f"شناسه: {mask_identifier(data.get('identifier'))}\n"
            f"رمز: ثبت شد ✅\n"
            f"کد بک‌آپ: {backup_line}\n"
            f"مبلغ: {int(data.get('price') or 0):,} تومان\n\n"
            "با تأیید، سفارش ساخته می‌شود و صفحه پرداخت باز می‌شود.\n"
        )
        if not has_backup:
            text += (
                "🆘 بک‌آپ نفرستادی؛ بعد از پرداخت موفق دکمه/تیکت پشتیبانی باز می‌شود.\n"
            )
        await self.send(
            event["chat_id"],
            text,
            buttons=inline(
                [
                    [("cred_confirm", "✅ تأیید و ساخت سفارش")],
                    [("cred_cancel", "❌ انصراف و حذف اطلاعات")],
                ]
            ),
        )

    async def credential_confirm(self, event, user):
        if await self.db.setting("sales_enabled", "1") != "1":
            await self.send(event["chat_id"], "⛔ فروش موقتاً متوقف شده است.")
            await self.db.set_session(event["sender_id"])
            return
        state, data = await self.db.session(event["sender_id"])
        if state != "cred_confirm" or not data.get("password") or not data.get("identifier"):
            await self.send(event["chat_id"], "❌ اطلاعات ناقص است؛ دوباره شروع کن.")
            await self.db.set_session(event["sender_id"])
            return
        try:
            ciphertext = encrypt_credentials(
                data["identifier"],
                data["password"],
                backup_code=data.get("backup_code") or "",
            )
            order, product = await self.db.create_credential_order(
                user["id"],
                int(data["product_id"]),
                login_method=data["method"],
                ciphertext=ciphertext,
                two_factor=bool(data.get("backup_code")),
            )
        except (ValueError, CredentialVaultError) as exc:
            await self.send(event["chat_id"], f"❌ {exc}", menu=main_menu())
            await self.db.set_session(event["sender_id"])
            return
        await self.db.set_session(event["sender_id"])
        balance = int(user["balance"] or 0)
        fresh = await self.db.pool.fetchrow(
            "SELECT balance FROM users WHERE id=$1", user["id"]
        )
        if fresh:
            balance = int(fresh["balance"] or 0)
        await self.send(
            event["chat_id"],
            f"✦ انتخاب روش پرداخت\n"
            f"سفارش #{order['id']}\n"
            f"محصول: {product['title']}\n"
            f"مبلغ: {int(order['payable_amount']):,} تومان\n"
            f"موجودی کیف پول: {balance:,} تومان\n\n"
            "اگر بک‌آپ بلد نبودی، بعد از پرداخت تیکت راهنمایی برایت باز می‌شود.",
            buttons=inline(
                [
                    [(f"pay:gateway:{order['id']}", "💳 زرین‌پال")],
                    [(f"pay:card:{order['id']}", "🏦 کارت‌به‌کارت")],
                    [(f"pay:wallet:{order['id']}", "💰 کیف پول")],
                    [(f"pay_cancel:{order['id']}", "✖️ لغو سفارش")],
                    [("home", "🏠 منوی اصلی")],
                ]
            ),
        )

    async def credential_cancel(self, event):
        await self.db.set_session(event["sender_id"])
        await self.send(
            event["chat_id"],
            "✖️ ثبت اطلاعات لغو شد.",
            menu=await self.user_menu(event["sender_id"]),
        )

    async def notify_credential_paid(self, order_id: int):
        await self.db.mark_credential_paid(order_id)
        row = await self.db.get_credential_order(order_id)
        if not row:
            return
        method = METHOD_META.get(row["login_method"], {}).get(
            "label", row["login_method"] or "—"
        )
        has_backup = bool(row.get("two_factor"))
        text = (
            f"💰 پرداخت شد — جم با اطلاعات #{order_id}\n"
            f"محصول: {row['product_title']}\n"
            f"مبلغ: {int(row['total_amount']):,} تومان\n"
            f"روش ورود: {method}\n"
            f"کد بک‌آپ: {'ثبت شده ✅' if has_backup else 'ثبت نشده 🆘'}\n"
            f"کاربر: {row['rubika_id']}\n\n"
            "اطلاعات را باز کن، وارد اکانت شو و «انجام شد» بزن."
        )
        buttons = inline(
            [[(f"cred_admin_open:{order_id}", "🔐 باز کردن سفارش")]]
        )
        recipients = {self.config.admin_chat_id, self.config.admin_id}
        for admin in await self.db.list_credential_admins():
            recipients.add(str(admin["rubika_id"]))
        for rid in recipients:
            if not rid:
                continue
            try:
                await self.api.send_message(rid, text, inline_keypad=buttons)
            except Exception:
                log.exception("credential paid notify failed for %s", rid)

    async def send_user_post_pay_credential_help(self, chat_id: str, order_id: int):
        support = await self.db.get_credential_support_contact()
        text = (
            f"🆘 نیاز به راهنمایی بک‌آپ کد؟\n"
            f"پرداخت سفارش #{order_id} ثبت شد.\n"
            f"اگر بک‌آپ بلد نیستی یا کار نمی‌کند، تیکت بزن و شماره سفارش را بنویس.\n"
        )
        if support["handle"]:
            text += f"\nپشتیبانی: {support['handle']}"
        await self.api.send_message(
            chat_id,
            text,
            inline_keypad=inline(
                [[(f"cred_ticket:{order_id}", f"🆘 تیکت راهنمایی بک‌آپ (سفارش #{order_id})")]]
            ),
        )

    async def credential_ticket_start(self, event, user, order_id: int):
        row = await self.db.get_credential_order(order_id)
        if not row or str(row["rubika_id"]) != str(user["rubika_id"]):
            await self.send(event["chat_id"], "❌ این سفارش مال شما نیست.")
            return
        if row["status"] not in ("paid", "processing", "delivered", "completed"):
            await self.send(event["chat_id"], "اول باید پرداخت موفق باشد.")
            return
        await self.db.set_session(
            event["sender_id"],
            "cred_ticket_message",
            {"order_id": int(order_id)},
        )
        await self.send(
            event["chat_id"],
            f"🆘 راهنمایی بک‌آپ — سفارش #{order_id}\n"
            "پیامت را بنویس؛ پشتیبان جم با اطلاعات می‌بیند.",
            buttons=inline([[("cred_cancel", "❌ انصراف")]]),
        )

    async def credential_ticket_receive(self, event, user, data):
        text = (event.get("text") or "").strip()
        if not text:
            await self.send(event["chat_id"], "فقط متن بفرست.")
            return
        order_id = int(data.get("order_id") or 0)
        ticket_id = await self.db.pool.fetchval(
            """INSERT INTO tickets(user_id,department,status,category,related_order_id)
               VALUES($1,'جم با اطلاعات','open','credential',$2) RETURNING id""",
            user["id"],
            order_id or None,
        )
        await self.db.pool.execute(
            """INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,text)
               VALUES($1,'user',$2,$3)""",
            ticket_id,
            event["sender_id"],
            f"سفارش #{order_id}\n\n{text}" if order_id else text,
        )
        await self.db.set_session(event["sender_id"])
        notify = (
            f"🔐 تیکت جم با اطلاعات #{ticket_id}\n"
            f"سفارش: #{order_id}\n"
            f"کاربر: {user['rubika_id']}\n\n{text}"
        )
        button_rows = []
        if order_id:
            button_rows.append([(f"cred_admin_open:{order_id}", "🔐 سفارش")])
        button_rows.append([(f"ticket_reply:{ticket_id}", "💬 پاسخ")])
        button_rows.append([(f"ticket:{ticket_id}", "🎫 باز کردن تیکت")])
        buttons = inline(button_rows)
        recipients = {self.config.admin_chat_id, self.config.admin_id}
        for admin in await self.db.list_credential_admins():
            recipients.add(str(admin["rubika_id"]))
        for rid in recipients:
            if not rid:
                continue
            try:
                await self.api.send_message(rid, notify, inline_keypad=buttons)
            except Exception:
                log.exception("cred ticket notify failed")
        await self.send(
            event["chat_id"],
            f"✅ تیکت #{ticket_id} ثبت شد.",
            menu=await self.user_menu(event["sender_id"]),
        )

    async def credadmin_home(self, event):
        ready = await self.db.count_ready_credential_orders()
        tickets = int(
            await self.db.pool.fetchval(
                """SELECT count(*) FROM tickets
                   WHERE status='open' AND COALESCE(category,'bot')='credential'"""
            )
            or 0
        )
        await self.send(
            event["chat_id"],
            "🔐 پنل جم با اطلاعات\n"
            "فقط سفارش‌های پرداخت‌شده و تیکت‌های همین بخش.\n"
            "همه‌چیز با دکمه انجام می‌شود.",
            menu=credential_staff_menu(),
            buttons=inline(
                [
                    [("cred_admin_list", f"🔐 سفارش‌های جم با اطلاعات ({ready})")],
                    [("cred_admin_tickets", f"🎫 تیکت‌های این بخش ({tickets})")],
                    [("cred_admin_home", "🔄 بروزرسانی")],
                    [("home", "🏠 منوی کاربر")],
                ]
            ),
        )

    async def cred_admin_tickets(self, event):
        rows = await self.db.pool.fetch(
            """SELECT t.id,t.status,t.related_order_id,t.updated_at,u.rubika_id
               FROM tickets t
               JOIN users u ON u.id=t.user_id
               WHERE t.status='open'
                 AND COALESCE(t.category,'bot')='credential'
               ORDER BY t.updated_at DESC
               LIMIT 30"""
        )
        lines = ["🎫 تیکت‌های جم با اطلاعات", "━━━━━━━━━━━━━━━"]
        buttons = []
        for row in rows:
            order_part = (
                f" · سفارش #{row['related_order_id']}" if row["related_order_id"] else ""
            )
            lines.append(f"#{row['id']} · {row['rubika_id']}{order_part}")
            buttons.append([(f"ticket:{row['id']}", f"تیکت #{row['id']}")])
            buttons.append([(f"ticket_reply:{row['id']}", f"💬 پاسخ #{row['id']}")])
        if not rows:
            lines.append("تیکت بازی نیست.")
        buttons.append([("cred_admin_home", "🔙 پنل")])
        await self.send(event["chat_id"], "\n".join(lines), buttons=inline(buttons))

    async def cred_admin_list(self, event):
        rows = await self.db.list_ready_credential_orders(30, paid_only=True)
        lines = ["🔐 سفارش‌های جم با اطلاعات", "━━━━━━━━━━━━━━━"]
        buttons = []
        labels = {
            "ready": "🟢 آماده",
            "needs_info": "🟠 ناقص",
            "completed": "✅ تکمیل",
            "awaiting_payment": "⏳ پرداخت",
        }
        for row in rows:
            st = labels.get(row["cred_status"], row["cred_status"])
            lines.append(
                f"#{row['id']} · {row['title']} · {int(row['total_amount']):,} ت · {st}"
            )
            buttons.append(
                [(f"cred_admin_open:{row['id']}", f"{st} · #{row['id']}")]
            )
        if not rows:
            lines.append("سفارشی نیست.")
        buttons.append([("cred_admin_list", "🔄 بروزرسانی")])
        buttons.append([("cred_admin_home", "🔙 پنل")])
        await self.send(event["chat_id"], "\n".join(lines), buttons=inline(buttons))

    async def cred_admin_open(self, event, order_id: int):
        row = await self.db.get_credential_order(order_id)
        if not row:
            await self.send(event["chat_id"], "سفارش پیدا نشد.")
            return
        method = METHOD_META.get(row["login_method"], {}).get(
            "label", row["login_method"] or "—"
        )
        text = (
            f"🔐 سفارش #{order_id}\n"
            "━━━━━━━━━━━━━━━\n"
            f"محصول: {row['product_title']}\n"
            f"مبلغ: {int(row['total_amount']):,} ت\n"
            f"وضعیت سفارش: {row['status']}\n"
            f"وضعیت اطلاعات: {row['cred_status']}\n"
            f"روش: {method}\n"
            f"کد بک‌آپ: {'ثبت شده ✅' if row['two_factor'] else 'ثبت نشده 🆘'}\n"
            f"کاربر: {row['display_name']} · {row['rubika_id']}\n"
        )
        if row.get("admin_note"):
            text += f"یادداشت: {row['admin_note']}\n"
        buttons = []
        if (
            row["ciphertext"]
            and row["status"] in ("paid", "processing")
            and not row["deleted_at"]
        ):
            buttons.append([(f"cred_admin_reveal:{order_id}", "👁 نمایش اطلاعات ورود")])
        if row["status"] in ("paid", "processing") and row["cred_status"] in (
            "ready",
            "needs_info",
        ):
            buttons.append(
                [
                    (f"cred_admin_done:{order_id}", "✅ انجام شد — خبر به مشتری"),
                    (f"cred_admin_bad:{order_id}", "⚠️ اطلاعات ناقص"),
                ]
            )
            buttons.append(
                [(f"cred_admin_refund_ask:{order_id}", "💰 لغو و برگشت به کیف پول")]
            )
        buttons.append([("cred_admin_list", "🔙 لیست")])
        await self.send(event["chat_id"], text, buttons=inline(buttons))

    async def cred_admin_reveal(self, event, order_id: int):
        row = await self.db.get_credential_order(order_id)
        if not row or not row["ciphertext"] or row["deleted_at"]:
            await self.send(event["chat_id"], "اطلاعات قابل نمایش نیست.")
            return
        if row["status"] not in ("paid", "processing"):
            await self.send(event["chat_id"], "سفارش هنوز پرداخت نشده.")
            return
        try:
            secret = decrypt_credentials(row["ciphertext"])
        except CredentialVaultError as exc:
            await self.send(event["chat_id"], str(exc))
            return
        await self.db.mark_credential_viewed(order_id)
        method = METHOD_META.get(row["login_method"], {}).get(
            "label", row["login_method"] or "—"
        )
        backup = str(secret.get("backup_code") or "").strip() or "— ثبت نشده —"
        text = (
            f"🔐 ورود سریع — سفارش #{order_id}\n"
            f"محصول: {row['product_title']}\n"
            f"روش: {method}\n\n"
            f"📧 شناسه:\n{secret['identifier']}\n\n"
            f"🔑 رمز:\n{secret['password']}\n\n"
            f"🛡 بک‌آپ:\n{backup}\n\n"
            "این اطلاعات تا «انجام شد» نگه داشته می‌شود."
        )
        await self.send(
            event["chat_id"],
            text,
            buttons=inline(
                [
                    [
                        (f"cred_admin_done:{order_id}", "✅ انجام شد — خبر به مشتری"),
                        (f"cred_admin_bad:{order_id}", "⚠️ اطلاعات ناقص"),
                    ],
                    [(f"cred_admin_refund_ask:{order_id}", "💰 لغو و برگشت به کیف پول")],
                    [(f"cred_admin_open:{order_id}", "🔒 مخفی کردن (هنوز در سرور هست)")],
                ]
            ),
        )

    async def cred_admin_done(self, event, order_id: int):
        try:
            await self.db.complete_credential_order(order_id)
        except ValueError as exc:
            await self.send(event["chat_id"], f"❌ {exc}")
            return
        row = await self.db.get_credential_order(order_id)
        if row and row["chat_id"]:
            try:
                await self.api.send_message(
                    row["chat_id"],
                    f"✅ سفارش #{order_id} با موفقیت انجام شد.\n"
                    "برای امنیت، رمز اکانت را عوض کن.",
                    chat_keypad=main_menu(),
                )
            except Exception:
                log.exception("notify user cred done failed")
        await self.send(event["chat_id"], f"✅ سفارش #{order_id} تکمیل شد.")
        await self.cred_admin_list(event)

    async def cred_admin_bad(self, event, order_id: int):
        await self.db.reject_credential_info(
            order_id, "اطلاعات ورود صحیح یا کامل نیست."
        )
        support = await self.db.get_credential_support_contact()
        row = await self.db.get_credential_order(order_id)
        if row and row["chat_id"]:
            try:
                await self.api.send_message(
                    row["chat_id"],
                    f"⚠️ اطلاعات سفارش #{order_id} ناقص است.\n"
                    f"با پشتیبانی ({support['handle'] or 'پشتیبانی'}) "
                    f"تماس بگیر و شماره سفارش را بفرست.",
                    inline_keypad=inline(
                        [[(f"cred_ticket:{order_id}", f"🆘 تیکت سفارش #{order_id}")]]
                    ),
                )
            except Exception:
                log.exception("notify user cred bad failed")
        await self.send(event["chat_id"], f"⚠️ سفارش #{order_id} به‌عنوان ناقص ثبت شد.")
        await self.cred_admin_open(event, order_id)

    async def cred_admin_refund_ask(self, event, order_id: int):
        row = await self.db.get_credential_order(order_id)
        if not row or row["status"] not in ("paid", "processing"):
            await self.send(event["chat_id"], "این سفارش قابل برگشت نیست.")
            return
        amount = int(row["total_amount"] or 0)
        await self.send(
            event["chat_id"],
            f"⚠️ لغو سفارش #{order_id}\n"
            f"مبلغ قابل برگشت به کیف پول مشتری پس از تأیید محاسبه می‌شود.\n"
            f"مبلغ سفارش: {amount:,} تومان\n\n"
            "اطلاعات ورود حذف می‌شود و مشتری می‌تواند دوباره خرید کند.",
            buttons=inline(
                [
                    [
                        (
                            f"cred_admin_refund_ok:{order_id}",
                            "✅ تأیید لغو و برگشت پول",
                        )
                    ],
                    [(f"cred_admin_open:{order_id}", "❌ انصراف")],
                ]
            ),
        )

    async def cred_admin_refund_ok(self, event, order_id: int):
        try:
            refunded = await self.db.refund_credential_order(order_id)
        except ValueError as exc:
            await self.send(event["chat_id"], f"❌ {exc}")
            return
        row = await self.db.get_credential_order(order_id)
        if row and row["chat_id"]:
            try:
                user_text = f"⚠️ سفارش #{order_id} لغو شد."
                if refunded > 0:
                    user_text += (
                        f"\n💰 مبلغ {refunded:,} تومان به کیف پولت برگشت.\n"
                        "می‌توانی دوباره خرید کنی یا موجودی را نگه داری."
                    )
                await self.api.send_message(
                    row["chat_id"], user_text, chat_keypad=main_menu()
                )
            except Exception:
                log.exception("notify user cred refund failed")
        refund_line = (
            f"\n💰 مبلغ {refunded:,} تومان به کیف پول کاربر برگشت."
            if refunded > 0
            else "\n(مبلغی برای برگشت ثبت نشد / قبلاً برگشته بود.)"
        )
        await self.send(event["chat_id"], f"🗑 سفارش #{order_id} لغو شد.{refund_line}")
        await self.cred_admin_list(event)

    async def owner_add_credential_admin(self, event, rubika_id: str, title: str = ""):
        await self.db.add_admin(
            rubika_id, title=title or "پشتیبان جم با اطلاعات", role="credential"
        )
        await self.db.set_setting("credential_support_id", rubika_id)
        await self.send(
            event["chat_id"],
            f"✅ پشتیبان جم با اطلاعات ثبت شد.\n"
            f"شناسه: {rubika_id}\n"
            f"آیدی پشتیبانی این بخش هم همین شد.\n"
            "به او بگو از دکمه «پنل جم با اطلاعات» استفاده کند.",
            menu=admin_menu(),
        )

    async def credential_pricing_hub(self, event):
        from supplier import compute_gem_sale_price, usd_toman_rate

        await self.db.set_session(event["sender_id"])
        cfg = await self.db.get_credential_pricing_config()
        manual_rate = await self.db.setting("usd_toman_rate", "")
        live = await usd_toman_rate(manual_rate)
        last_sync = await self.db.setting_timestamp("gem_price_last_sync")
        products = await self.db.products("gem_credentials")
        rate_value = live["rate"] if live.get("ok") else None
        weekly_preview = monthly_preview = None
        if rate_value:
            weekly_preview = await compute_gem_sale_price(
                cfg["weekly_cost"], rate_value, cfg["weekly_profit"]
            )
            monthly_preview = await compute_gem_sale_price(
                cfg["monthly_cost"], rate_value, cfg["monthly_profit"]
            )
        lines = [
            "💱 قیمت‌گذاری جم با اطلاعات",
            "━━━━━━━━━━━━━━━",
            "فرمول: بهای دلاری × نرخ دلار × (1 + سود٪) → گرد به هزار تومان",
            "",
            f"💵 بهای خالص هفتگی: {cfg['weekly_cost']} USD · سود {cfg['weekly_profit']}٪",
            f"💵 بهای خالص ماهانه: {cfg['monthly_cost']} USD · سود {cfg['monthly_profit']}٪",
        ]
        if live.get("ok"):
            lines.append(
                f"💱 نرخ لحظه‌ای: {live['rate']:,} تومان ({live['source']})"
            )
            if weekly_preview is not None:
                lines.append(f"📅 قیمت فروش هفتگی (محاسبه): {weekly_preview:,} ت")
            if monthly_preview is not None:
                lines.append(f"📆 قیمت فروش ماهانه (محاسبه): {monthly_preview:,} ت")
        else:
            lines.append(
                f"⚠️ نرخ لحظه‌ای در دسترس نیست؛ fallback: {manual_rate or '—'}"
            )
        if last_sync:
            lines.extend(["", f"🕐 آخرین sync: {last_sync.strftime('%Y-%m-%d %H:%M')}"])
        lines.extend(["", "⏱ هر ۲۴ ساعت خودکار به‌روز می‌شود.", "", "محصولات فعال:"])
        for p in products:
            lines.append(f"• #{p['id']} {p['title']} — {int(p['price']):,} ت")
        if not products:
            lines.append("• محصولی نیست.")
        await self.send(
            event["chat_id"],
            "\n".join(lines),
            buttons=inline(
                [
                    [("admin_pricing_sync", "🔄 sync جم با اطلاعات")],
                    [
                        ("admin_cred_set_weekly_cost", "💵 $ هفتگی"),
                        ("admin_cred_set_monthly_cost", "💵 $ ماهانه"),
                    ],
                    [("admin_pricing_home", "🔙 قیمت و سود"), ("admin_shop", "🔙 فروشگاه")],
                ]
            ),
        )

    async def credential_price_sync(self, event):
        await self.admin_pricing_sync(event)

    async def dispatch_credential_action(self, event, user, action: str) -> bool:
        """Handle user/admin credential button/command ids. Return True if handled."""
        if action in {"/credadmin", "cred_admin_home"}:
            await self.credadmin_home(event)
            return True
        if action == "cred_admin_list":
            await self.cred_admin_list(event)
            return True
        if action == "cred_admin_tickets":
            await self.cred_admin_tickets(event)
            return True
        if action == "cred_pricing" or action == "admin_cred_pricing":
            await self.credential_pricing_hub(event)
            return True
        if action == "cred_price_sync":
            await self.credential_price_sync(event)
            return True
        if action == "cred_cancel":
            await self.credential_cancel(event)
            return True
        if action == "cred_backup_skip":
            await self.credential_backup_skip(event)
            return True
        if action == "cred_confirm":
            await self.credential_confirm(event, user)
            return True
        if action == "gems_credentials":
            await self.credential_products_menu(event)
            return True

        def _id_after(prefix: str):
            raw = action.removeprefix(prefix)
            return int(raw) if raw.isdigit() else None

        if action.startswith("cred_product:"):
            pid = _id_after("cred_product:")
            if pid is None:
                await self.send(event["chat_id"], "❌ شناسه محصول نامعتبر است.")
            else:
                await self.show_credential_product(event, pid)
            return True
        if action.startswith("cred_buy:"):
            pid = _id_after("cred_buy:")
            if pid is None:
                await self.send(event["chat_id"], "❌ شناسه محصول نامعتبر است.")
            else:
                await self.credential_buy_start(event, user, pid)
            return True
        if action.startswith("cred_method:"):
            await self.credential_method_selected(
                event, action.removeprefix("cred_method:")
            )
            return True
        if action.startswith("cred_ticket:"):
            oid = _id_after("cred_ticket:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.credential_ticket_start(event, user, oid)
            return True
        if action.startswith("cred_admin_open:"):
            oid = _id_after("cred_admin_open:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.cred_admin_open(event, oid)
            return True
        if action.startswith("cred_admin_reveal:"):
            oid = _id_after("cred_admin_reveal:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.cred_admin_reveal(event, oid)
            return True
        if action.startswith("cred_admin_done:"):
            oid = _id_after("cred_admin_done:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.cred_admin_done(event, oid)
            return True
        if action.startswith("cred_admin_bad:"):
            oid = _id_after("cred_admin_bad:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.cred_admin_bad(event, oid)
            return True
        if action.startswith("cred_admin_refund_ask:"):
            oid = _id_after("cred_admin_refund_ask:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.cred_admin_refund_ask(event, oid)
            return True
        if action.startswith("cred_admin_refund_ok:"):
            oid = _id_after("cred_admin_refund_ok:")
            if oid is None:
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
            else:
                await self.cred_admin_refund_ok(event, oid)
            return True
        return False

    async def ensure_credential_staff(self, rubika_id: str) -> bool:
        return await self.db.is_credential_admin(rubika_id, self.config.admin_id) or (
            await self.db.is_admin(rubika_id, self.config.admin_id)
        )
