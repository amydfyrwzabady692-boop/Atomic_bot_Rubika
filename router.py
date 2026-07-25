import logging
import os
import re

from database import Database
from keyboards import admin_menu, inline, main_menu
from payment_safety import MIN_WALLET_CHARGE, checked_amount
from payments import Zarinpal
from rubika_api import RubikaAPI, RubikaAPIError
from supplier import G2Bulk, usd_toman_rate

log = logging.getLogger(__name__)

WELCOME = """✨ به اتومیک شاپ روبیکا خوش اومدی! ✨

اینجا جاییه که سرعت، امنیت و قیمت مناسب کنار هم جمع شدن تا خرید راحت‌تری داشته باشی 🚀

💎 خرید جم و محصولات بازی
🎯 پک‌های حرفه‌ای سنسیویتی موبایل و PC
💳 پرداخت امن با درگاه یا کارت‌به‌کارت
🎁 کدهای هدیه و تخفیف‌های ویژه
🧑‍💻 پشتیبانی مستقیم و پیگیری سفارش

تمام سفارش‌ها و پرداخت‌های تو از داخل ربات قابل مشاهده و پیگیری هستند.

👇 برای شروع، یکی از گزینه‌های منوی پایین را انتخاب کن.

⚛️ Atomic Shop"""


class Router:
    def __init__(self, db: Database, api: RubikaAPI, config):
        self.db, self.api, self.config = db, api, config
        self.zarinpal = Zarinpal()
        self.g2 = G2Bulk()

    async def send(self, chat_id, text, *, menu=None, buttons=None):
        return await self.api.send_message(chat_id, text, chat_keypad=menu, inline_keypad=buttons)

    async def handle(self, event: dict):
        if not event["chat_id"] or not event["sender_id"]:
            return
        if not await self.db.claim_event(event["event_id"]):
            return
        user = await self.db.user(event["sender_id"], event["chat_id"])
        if user["blocked"] and not await self.db.is_admin(event["sender_id"], self.config.admin_id):
            await self.send(event["chat_id"], "🚫 حساب شما مسدود است.")
            return
        action = event["button_id"] or event["text"].strip()
        if action in {"/start", "شروع", "home", "🏠 منوی کاربر"}:
            await self.start(event, user)
            return
        if action == "/admin" or action.startswith("admin_"):
            if await self.db.is_admin(event["sender_id"], self.config.admin_id):
                await self.admin(event, action)
            else:
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
            return
        if action.startswith("/") and await self.db.is_admin(
            event["sender_id"], self.config.admin_id
        ):
            await self.admin_command(event, action)
            return
        if action.startswith("product:"):
            await self.product_selected(event, user, int(action.split(":")[1]))
            return
        if action.startswith("gem_buy:"):
            await self.ask_gem_player_id(event, int(action.split(":")[1]))
            return
        if action == "gem_confirm":
            state, data = await self.db.session(event["sender_id"])
            if state != "gem_confirm":
                await self.send(event["chat_id"], "جلسه خرید منقضی شده؛ دوباره بسته را انتخاب کن.")
                return
            await self.db.set_session(event["sender_id"])
            await self.create_order_prompt(
                event,
                user,
                int(data["product_id"]),
                str(data["player_id"]),
            )
            return
        if action == "gem_reedit":
            state, data = await self.db.session(event["sender_id"])
            if state != "gem_confirm":
                await self.send(event["chat_id"], "جلسه خرید منقضی شده؛ دوباره بسته را انتخاب کن.")
                return
            await self.ask_gem_player_id(event, int(data["product_id"]))
            return
        if action == "gem_cancel":
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], "✖️ ثبت سفارش لغو شد.", menu=main_menu())
            return
        if action.startswith("pay:"):
            _, method, order_id = action.split(":")
            await self.pay_order(event, user, int(order_id), method)
            return
        routes = {
            "gems": lambda: self.show_products(event, "gem", "💎 بسته‌های جم"),
            "💎 خرید جم": lambda: self.show_products(event, "gem", "💎 بسته‌های جم"),
            "💎 جم فری‌فایر": lambda: self.show_products(event, "gem", "💎 بسته‌های جم"),
            "sense": lambda: self.sense_menu(event),
            "🎯 پک سنسیویتی": lambda: self.sense_menu(event),
            "🎯 پک سنس": lambda: self.sense_menu(event),
            "sense_mobile": lambda: self.show_products(event, "sense_mobile", "📱 سنسیویتی موبایل"),
            "sense_pc": lambda: self.show_products(event, "sense_pc", "🖥 سنسیویتی PC"),
            "store": lambda: self.show_products(event, "store", "🛍 محصولات فروشگاه"),
            "🛍 فروشگاه": lambda: self.show_products(event, "store", "🛍 محصولات فروشگاه"),
            "🛍 فروشگاه اکانت": lambda: self.show_products(
                event, "store", "🛍 محصولات فروشگاه"
            ),
            "wallet": lambda: self.wallet(event, user),
            "💰 کیف پول": lambda: self.wallet(event, user),
            "wallet_charge": lambda: self.ask_wallet_charge(event),
            "orders": lambda: self.orders(event, user),
            "📦 سفارش‌های من": lambda: self.orders(event, user),
            "account": lambda: self.account(event, user),
            "👤 حساب من": lambda: self.account(event, user),
            "support": lambda: self.ask_support(event),
            "🧑‍💻 پشتیبانی": lambda: self.ask_support(event),
            "🎧 پشتیبانی": lambda: self.ask_support(event),
            "promo": lambda: self.ask_promo(event),
            "🎁 ثبت کد": lambda: self.ask_promo(event),
            "help": lambda: self.help(event),
            "📚 راهنما": lambda: self.help(event),
            "join_request": lambda: self.join_request(event, user),
        }
        handler = routes.get(action)
        if handler:
            await handler()
            return
        state, data = await self.db.session(event["sender_id"])
        if state:
            await self.handle_state(event, user, state, data)
            return
        await self.send(event["chat_id"], "از منوی پایین انتخاب کن 👇", menu=main_menu())

    async def start(self, event, user):
        channels = await self.db.pool.fetch(
            "SELECT * FROM forced_channels WHERE active ORDER BY id"
        )
        if channels:
            approved = await self.db.pool.fetchval(
                """SELECT 1 FROM join_requests WHERE user_id=$1 AND status='approved'
                   ORDER BY id DESC LIMIT 1""",
                user["id"],
            )
            if not approved:
                lines = ["📢 ابتدا در کانال‌های زیر عضو شو:"]
                lines += [f"• {r['title']}\n{r['invite_url']}" for r in channels]
                lines.append("\nسپس «بررسی عضویت» را بزن.")
                await self.send(
                    event["chat_id"],
                    "\n".join(lines),
                    buttons=inline([[("join_request", "✅ بررسی عضویت")]]),
                )
                return
        text = await self.db.setting("welcome_text", WELCOME)
        if text.strip() == "✨ به اتومیک شاپ روبیکا خوش اومدی! ✨":
            text = WELCOME
        await self.send(event["chat_id"], text, menu=main_menu())

    async def join_request(self, event, user):
        await self.db.pool.execute(
            """INSERT INTO join_requests(user_id,status) VALUES($1,'pending')
               ON CONFLICT DO NOTHING""",
            user["id"],
        )
        await self.send(
            event["chat_id"],
            "✅ درخواست بررسی ثبت شد. پس از تأیید مدیر، منوی ربات باز می‌شود.",
        )
        await self.api.send_message(
            self.config.admin_chat_id,
            f"📢 درخواست عضویت\nکاربر: {user['rubika_id']}\n"
            f"تأیید: /join_ok {user['id']}\nرد: /join_no {user['id']}",
        )

    async def sense_menu(self, event):
        await self.send(
            event["chat_id"],
            "نوع پک سنسیویتی را انتخاب کن:",
            buttons=inline(
                [
                    [("sense_mobile", "📱 موبایل"), ("sense_pc", "🖥 PC")],
                    [("home", "🏠 بازگشت")],
                ]
            ),
        )

    async def show_products(self, event, kind, title):
        rows = await self.db.products(kind)
        if not rows:
            await self.send(event["chat_id"], "فعلاً محصول فعالی موجود نیست.")
            return
        buttons = [[(f"product:{r['id']}", f"{r['title']} — {r['price']:,} تومان")] for r in rows]
        buttons.append([("home", "🏠 بازگشت")])
        await self.send(event["chat_id"], title, buttons=inline(buttons))

    async def product_selected(self, event, user, product_id):
        product = await self.db.pool.fetchrow(
            "SELECT * FROM products WHERE id=$1 AND active AND stock>0", product_id
        )
        if not product:
            await self.send(event["chat_id"], "این محصول دیگر موجود نیست.")
            return
        if product["kind"] == "gem":
            await self.send(
                event["chat_id"],
                f"💎 {product['title']}\n"
                f"تعداد جم: {product['amount']:,}\n"
                f"💰 قیمت: {product['price']:,} تومان\n\n"
                "برای ادامه خرید، بسته را تأیید کن.",
                buttons=inline(
                    [
                        [(f"gem_buy:{product_id}", "✅ خرید این بسته")],
                        [("gems", "🔙 بازگشت به فهرست")],
                    ]
                ),
            )
            return
        await self.create_order_prompt(event, user, product_id)

    async def ask_gem_player_id(self, event, product_id):
        product = await self.db.pool.fetchrow(
            "SELECT id FROM products WHERE id=$1 AND kind='gem' AND active AND stock>0",
            product_id,
        )
        if not product:
            await self.send(event["chat_id"], "این بسته دیگر موجود یا فعال نیست.")
            return
        await self.db.set_session(
            event["sender_id"], "gem_player_id", {"product_id": product_id}
        )
        await self.send(
            event["chat_id"],
            "🎮 آیدی عددی فری‌فایر را ارسال کن:",
            buttons=inline([[("gem_cancel", "✖️ انصراف")]]),
        )

    async def create_order_prompt(self, event, user, product_id, player_id=""):
        order, product = await self.db.create_order(user["id"], product_id, player_id)
        await self.send(
            event["chat_id"],
            f"🧾 سفارش #{order['id']}\n{product['title']}\n"
            f"مبلغ: {order['payable_amount']:,} تومان\nروش پرداخت:",
            buttons=inline(
                [
                    [(f"pay:gateway:{order['id']}", "🌐 درگاه زرین‌پال")],
                    [(f"pay:card:{order['id']}", "💳 کارت‌به‌کارت")],
                    [(f"pay:wallet:{order['id']}", "💰 کیف پول")],
                ]
            ),
        )

    async def pay_order(self, event, user, order_id, method):
        order = await self.db.pool.fetchrow(
            "SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user["id"]
        )
        if not order or order["status"] != "pending":
            await self.send(event["chat_id"], "این سفارش قابل پرداخت نیست.")
            return
        if method == "wallet":
            try:
                amount = await self.db.wallet_pay(user["id"], order_id)
                await self.send(event["chat_id"], f"✅ پرداخت {amount:,} تومان انجام شد.")
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
            return
        if method == "gateway" and await self.db.setting("zarinpal_enabled", "1") != "1":
            await self.send(event["chat_id"], "درگاه زرین‌پال فعلاً غیرفعال است.")
            return
        if method == "card" and await self.db.setting("card_enabled", "1") != "1":
            await self.send(event["chat_id"], "کارت‌به‌کارت فعلاً غیرفعال است.")
            return
        payment = await self.db.create_payment(
            user["id"],
            order_id,
            "order",
            method,
            order["payable_amount"],
            self.config.payment_ttl_minutes,
        )
        if method == "gateway":
            callback = f"{self.config.callback_base}/payment/callback"
            authority, url, error = await self.zarinpal.request(
                payment["amount"], f"Atomic Rubika order #{order_id}", callback
            )
            if error:
                await self.send(event["chat_id"], f"❌ {error}")
                return
            await self.db.attach_authority(payment["id"], authority)
            await self.send(
                event["chat_id"],
                "✅ لینک پرداخت ساخته شد.\nابتدا VPN را خاموش کن و سپس لینک را باز کن:\n\n"
                f"{url}\n\nاین لینک قابل کپی است.",
            )
            return
        number = (await self.db.setting("card_number", "")).strip() or os.getenv(
            "CARD_TRANSFER_NUMBER", ""
        ).strip()
        holder = (await self.db.setting("card_holder", "")).strip() or os.getenv(
            "CARD_TRANSFER_HOLDER", ""
        ).strip()
        bank = (await self.db.setting("card_bank", "")).strip() or os.getenv(
            "CARD_TRANSFER_BANK", ""
        ).strip()
        if not number:
            await self.send(event["chat_id"], "کارت‌به‌کارت فعلاً فعال نیست.")
            return
        await self.db.set_session(event["sender_id"], "card_receipt", {"payment_id": payment["id"]})
        await self.send(
            event["chat_id"],
            f"💳 مبلغ دقیق: {payment['amount']:,} تومان\nکارت: {number}\n"
            f"به نام: {holder}\nبانک: {bank}\n\nتصویر رسید را همین‌جا ارسال کن.",
        )

    async def wallet(self, event, user):
        fresh = await self.db.pool.fetchrow("SELECT * FROM users WHERE id=$1", user["id"])
        await self.send(
            event["chat_id"],
            f"💰 موجودی: {fresh['balance']:,} تومان",
            buttons=inline([[("wallet_charge", "➕ شارژ کیف پول")]]),
        )

    async def ask_wallet_charge(self, event):
        await self.db.set_session(event["sender_id"], "wallet_amount")
        await self.send(event["chat_id"], "مبلغ شارژ را به تومان بفرست (حداقل ۱۰٬۰۰۰):")

    async def orders(self, event, user):
        rows = await self.db.pool.fetch(
            """SELECT id,status,total_amount,created_at FROM orders
               WHERE user_id=$1 ORDER BY id DESC LIMIT 10""",
            user["id"],
        )
        text = "📦 سفارش‌های اخیر:\n" + (
            "\n".join(f"#{r['id']} | {r['status']} | {r['total_amount']:,} تومان" for r in rows)
            if rows
            else "هنوز سفارشی نداری."
        )
        await self.send(event["chat_id"], text)

    async def account(self, event, user):
        await self.send(
            event["chat_id"],
            f"👤 شناسه روبیکا: {user['rubika_id']}\n"
            f"💰 موجودی: {user['balance']:,} تومان\n"
            f"📅 عضویت: {user['created_at']:%Y-%m-%d}",
        )

    async def ask_support(self, event):
        await self.db.set_session(event["sender_id"], "support_message")
        await self.send(event["chat_id"], "پیام خودت را برای پشتیبانی بنویس:")

    async def ask_promo(self, event):
        await self.db.set_session(event["sender_id"], "promo_code")
        await self.send(event["chat_id"], "کد هدیه یا تخفیف را ارسال کن:")

    async def help(self, event):
        await self.send(
            event["chat_id"],
            "📚 از منوی پایین بخش موردنظر را انتخاب کن.\n"
            "پرداخت درگاه فقط پس از تأیید مستقیم زرین‌پال ثبت می‌شود.\n"
            "برای کارت‌به‌کارت، خرید فقط بعد از تأیید مدیر انجام خواهد شد.",
        )

    async def handle_state(self, event, user, state, data):
        if state == "gem_player_id":
            player_id = re.sub(r"\s+", "", event["text"]).translate(
                str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
            )
            if not player_id.isdigit() or not 5 <= len(player_id) <= 20:
                await self.send(event["chat_id"], "آیدی معتبر نیست؛ فقط عدد بفرست.")
                return
            await self.send(event["chat_id"], "⏳ در حال بررسی آیدی بازی…")
            result = await self.g2.check_player(player_id)
            if not result["ok"]:
                await self.send(
                    event["chat_id"],
                    f"❌ {result['error']}\nآیدی را اصلاح کن و دوباره بفرست.",
                    buttons=inline([[("gem_cancel", "✖️ انصراف")]]),
                )
                return
            product = await self.db.pool.fetchrow(
                "SELECT * FROM products WHERE id=$1 AND kind='gem' AND active AND stock>0",
                int(data["product_id"]),
            )
            if not product:
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "این بسته دیگر موجود یا فعال نیست.")
                return
            await self.db.set_session(
                event["sender_id"],
                "gem_confirm",
                {
                    "product_id": product["id"],
                    "player_id": player_id,
                    "player_name": result["name"],
                },
            )
            await self.send(
                event["chat_id"],
                "✅ اکانت تأیید شد\n"
                f"👤 نام اکانت: {result['name']}\n"
                f"🆔 UID: {player_id}\n"
                f"💎 بسته: {product['title']}\n"
                f"💰 مبلغ: {product['price']:,} تومان\n\n"
                "اگر اطلاعات درست است، تأیید کن.",
                buttons=inline(
                    [
                        [("gem_confirm", "✅ تأیید و ادامه پرداخت")],
                        [("gem_reedit", "✏️ اصلاح آیدی")],
                        [("gem_cancel", "✖️ انصراف")],
                    ]
                ),
            )
        elif state == "wallet_amount":
            try:
                amount = checked_amount(
                    event["text"].replace(",", ""),
                    minimum=MIN_WALLET_CHARGE,
                    label="مبلغ شارژ",
                )
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            payment = await self.db.create_payment(
                user["id"],
                None,
                "wallet",
                "gateway",
                amount,
                self.config.payment_ttl_minutes,
            )
            callback = f"{self.config.callback_base}/payment/callback"
            authority, url, error = await self.zarinpal.request(
                amount, f"Atomic Rubika wallet #{payment['id']}", callback
            )
            if error:
                await self.send(event["chat_id"], f"❌ {error}")
                return
            await self.db.attach_authority(payment["id"], authority)
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"VPN را خاموش کن و لینک را باز کن:\n\n{url}")
        elif state == "support_message":
            text = event["text"].strip()
            if not text:
                await self.send(event["chat_id"], "پیام متنی بفرست.")
                return
            ticket = await self.db.pool.fetchrow(
                "INSERT INTO tickets(user_id) VALUES($1) RETURNING id", user["id"]
            )
            await self.db.pool.execute(
                """INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,text)
                   VALUES($1,'user',$2,$3)""",
                ticket["id"],
                event["sender_id"],
                text[:4000],
            )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ تیکت #{ticket['id']} ثبت شد.")
            await self.api.send_message(
                self.config.admin_chat_id,
                f"🎧 تیکت #{ticket['id']} از {event['sender_id']}\n{text}\n\n"
                f"پاسخ: /reply {ticket['id']} متن",
            )
        elif state == "promo_code":
            try:
                kind, value = await self.db.redeem_code(user["id"], event["text"])
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            await self.db.set_session(event["sender_id"])
            if kind == "gift":
                await self.send(
                    event["chat_id"],
                    f"✅ مبلغ {value:,} تومان به کیف پولت اضافه شد.",
                )
            else:
                await self.send(
                    event["chat_id"],
                    f"✅ تخفیف {value}٪ برای سفارش بعدی فعال شد.",
                )
        elif state == "card_receipt":
            file_info = event.get("file")
            if not file_info:
                await self.send(event["chat_id"], "لطفاً تصویر یا فایل رسید را ارسال کن.")
                return
            file_id = str(file_info.get("file_id") or file_info.get("id") or "")
            receipt = await self.db.pool.fetchrow(
                """INSERT INTO receipts(
                     payment_id,user_id,source_chat_id,source_message_id,file_id
                   ) VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT(payment_id) DO NOTHING RETURNING id""",
                int(data["payment_id"]),
                user["id"],
                event["chat_id"],
                event["message_id"],
                file_id,
            )
            await self.db.set_session(event["sender_id"])
            if not receipt:
                await self.send(event["chat_id"], "این رسید قبلاً ثبت شده است.")
                return
            await self.send(event["chat_id"], f"✅ رسید #{receipt['id']} ثبت و منتظر تأیید است.")
            await self.api.send_message(
                self.config.admin_chat_id,
                f"🧾 رسید جدید #{receipt['id']}\n"
                f"تأیید: /receipt_ok {receipt['id']}\nرد: /receipt_no {receipt['id']}",
            )
            try:
                await self.api.forward_message(
                    event["chat_id"], event["message_id"], self.config.admin_chat_id
                )
            except RubikaAPIError:
                log.exception("Could not forward receipt to admin")

    async def admin(self, event, action):
        chat = event["chat_id"]
        if action == "/admin":
            await self.send(chat, "🛠 پنل مدیریت اتومیک روبیکا", menu=admin_menu())
        elif action == "admin_stats":
            s = await self.db.stats()
            await self.send(
                chat,
                f"📊 آمار کلی\nکاربران: {s['users']:,}\nخریداران: {s['buyers']:,}\n"
                f"موجودی کل: {s['balances']:,} تومان\nفروش‌ها: {s['sales']:,}\n"
                f"جمع فروش: {s['revenue']:,} تومان",
            )
        elif action == "admin_receipts":
            rows = await self.db.pool.fetch(
                "SELECT id,payment_id,user_id FROM receipts WHERE status='pending' ORDER BY id LIMIT 30"
            )
            await self.send(
                chat,
                "🧾 رسیدهای تأییدنشده\n"
                + (
                    "\n".join(
                        f"#{r['id']} پرداخت {r['payment_id']} کاربر {r['user_id']}" for r in rows
                    )
                    if rows
                    else "موردی نیست."
                ),
            )
        elif action == "admin_users":
            rows = await self.db.pool.fetch(
                "SELECT id,rubika_id,balance FROM users WHERE balance>0 ORDER BY balance DESC LIMIT 30"
            )
            await self.send(
                chat,
                "👥 کاربران دارای موجودی\n"
                + (
                    "\n".join(f"{r['id']} | {r['rubika_id']} | {r['balance']:,}" for r in rows)
                    if rows
                    else "موردی نیست."
                )
                + "\n\n/users_balance\n/users_referral\n/users_card\n"
                "/user ID\n/block DB_ID\n/unblock DB_ID",
            )
        elif action == "admin_support":
            rows = await self.db.pool.fetch(
                "SELECT id,user_id,department FROM tickets WHERE status='open' ORDER BY updated_at DESC LIMIT 30"
            )
            await self.send(
                chat,
                "🎧 تیکت‌های باز\n"
                + (
                    "\n".join(f"#{r['id']} کاربر {r['user_id']} | {r['department']}" for r in rows)
                    if rows
                    else "موردی نیست."
                ),
            )
        elif action == "admin_fx":
            rate = await usd_toman_rate()
            profit = await self.db.pool.fetchrow(
                """SELECT count(*) orders,
                          coalesce(sum(gross_profit_toman),0) profit
                   FROM profit_snapshots"""
            )
            if rate.get("ok"):
                rate_text = f"{rate['rate']:,} تومان ({rate['source']})"
            else:
                rate_text = "دریافت نشد؛ نرخ دستی را تنظیم کن."
            await self.send(
                chat,
                f"💵 نرخ دلار: {rate_text}\n"
                f"سفارش‌های محاسبه‌شده: {profit['orders']:,}\n"
                f"سود ناخالص ثبت‌شده: {profit['profit']:,} تومان",
            )
        else:
            await self.send(chat, self.admin_help(action))

    def admin_help(self, section):
        docs = {
            "admin_products": "/product_add kind|title|price|stock|amount|sku|cost_usd\n/product_edit id|field|value\n/product_delete id",
            "admin_categories": "/category_add title\n/category_delete id",
            "admin_finance": "/setting zarinpal_enabled 1\n/setting card_number NUMBER\n/setting card_holder NAME\n/setting card_bank BANK",
            "admin_search": "/user شناسه\n/order شماره",
            "admin_broadcast": "/broadcast متن پیام",
            "admin_codes": "/code_add gift|CODE|VALUE|MAX\n/code_add discount|CODE|PERCENT|MAX\n/code_delete ID",
            "admin_settings": "/setting welcome_text TEXT\n/admin_add ID TITLE\n/admin_delete ID\n/channel_add CHAT|TITLE|URL\n/channel_delete ID\n/department_add TITLE\n/department_delete ID",
        }
        return "راهنمای این بخش:\n" + docs.get(section, "دستور این بخش تعریف نشده است.")

    async def admin_command(self, event, command):
        admin_id, chat = event["sender_id"], event["chat_id"]
        name, _, args = command.partition(" ")
        try:
            if name == "/reply":
                ticket_id, text = args.split(" ", 1)
                row = await self.db.pool.fetchrow(
                    """SELECT t.id,u.chat_id FROM tickets t JOIN users u ON u.id=t.user_id
                       WHERE t.id=$1""",
                    int(ticket_id),
                )
                if not row:
                    raise ValueError("تیکت پیدا نشد.")
                await self.db.pool.execute(
                    """INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,text)
                       VALUES($1,'admin',$2,$3)""",
                    int(ticket_id),
                    admin_id,
                    text[:4000],
                )
                await self.db.pool.execute(
                    "UPDATE tickets SET updated_at=now() WHERE id=$1", int(ticket_id)
                )
                await self.api.send_message(
                    row["chat_id"], f"🎧 پاسخ پشتیبانی #{ticket_id}\n{text}"
                )
            elif name in {"/receipt_ok", "/receipt_no"}:
                await self.review_receipt(admin_id, int(args), name.endswith("_ok"))
            elif name in {"/join_ok", "/join_no"}:
                await self.review_join(admin_id, int(args), name.endswith("_ok"))
            elif name == "/setting":
                key, value = args.split(" ", 1)
                allowed = {
                    "welcome_text",
                    "support_id",
                    "zarinpal_enabled",
                    "card_enabled",
                    "card_number",
                    "card_holder",
                    "card_bank",
                    "usd_toman_rate",
                }
                if key not in allowed:
                    raise ValueError("کلید تنظیمات مجاز نیست.")
                await self.db.set_setting(key, value)
            elif name == "/admin_add":
                rubika_id, _, title = args.partition(" ")
                await self.db.pool.execute(
                    """INSERT INTO admins(rubika_id,title) VALUES($1,$2)
                       ON CONFLICT(rubika_id) DO UPDATE SET title=$2,active=true""",
                    rubika_id,
                    title,
                )
            elif name == "/admin_delete":
                if args.strip() == self.config.admin_id:
                    raise ValueError("مدیر اصلی قابل حذف نیست.")
                await self.db.pool.execute(
                    "UPDATE admins SET active=false WHERE rubika_id=$1", args.strip()
                )
            elif name == "/product_add":
                kind, title, price, stock, amount, sku, cost = args.split("|", 6)
                await self.db.pool.execute(
                    """INSERT INTO products(kind,title,price,stock,amount,supplier_sku,supplier_cost_usd)
                       VALUES($1,$2,$3,$4,$5,$6,$7)""",
                    kind.strip(),
                    title.strip(),
                    int(price),
                    int(stock),
                    int(amount) if amount.strip() else None,
                    sku.strip(),
                    float(cost) if cost.strip() else None,
                )
            elif name == "/product_edit":
                product_id, field, value = args.split("|", 2)
                allowed = {
                    "title",
                    "price",
                    "stock",
                    "description",
                    "active",
                    "supplier_sku",
                    "supplier_cost_usd",
                }
                if field not in allowed:
                    raise ValueError("فیلد غیرمجاز است.")
                if field in {"price", "stock"}:
                    value = int(value)
                elif field == "supplier_cost_usd":
                    value = float(value)
                elif field == "active":
                    value = value.strip().lower() in {"1", "true", "yes", "on"}
                await self.db.pool.execute(
                    f'UPDATE products SET "{field}"=$1 WHERE id=$2',
                    value,
                    int(product_id),
                )
            elif name == "/product_delete":
                await self.db.pool.execute(
                    "UPDATE products SET active=false WHERE id=$1", int(args)
                )
            elif name == "/category_add":
                await self.db.pool.execute("INSERT INTO categories(title) VALUES($1)", args.strip())
            elif name == "/category_delete":
                await self.db.pool.execute(
                    "UPDATE categories SET active=false WHERE id=$1", int(args)
                )
            elif name == "/code_add":
                kind, code, value, max_uses = args.split("|", 3)
                await self.db.pool.execute(
                    """INSERT INTO promo_codes(code_type,code,value,max_uses)
                       VALUES($1,upper($2),$3,$4)""",
                    kind,
                    code,
                    int(value),
                    int(max_uses),
                )
            elif name == "/code_delete":
                await self.db.pool.execute(
                    "UPDATE promo_codes SET active=false WHERE id=$1", int(args)
                )
            elif name == "/charge":
                user_id, amount = args.split()
                amount = checked_amount(amount, label="شارژ کاربر")
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            amount,
                            int(user_id),
                        )
                        await conn.execute(
                            """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                               VALUES($1,$2,'admin_charge',$3)""",
                            int(user_id),
                            amount,
                            f"admin:{admin_id}:{user_id}:{os.urandom(8).hex()}",
                        )
            elif name == "/charge_all":
                amount = checked_amount(args, label="شارژ همگانی")
                batch = f"admin-all:{admin_id}:{os.urandom(8).hex()}"
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """INSERT INTO wallet_ledger(
                                 user_id,amount,entry_type,reference
                               )
                               SELECT id,$1,'admin_charge_all',$2||':'||id::text
                               FROM users WHERE NOT blocked""",
                            amount,
                            batch,
                        )
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE NOT blocked",
                            amount,
                        )
            elif name == "/broadcast":
                users = await self.db.pool.fetch("SELECT chat_id FROM users WHERE NOT blocked")
                sent = 0
                for row in users:
                    try:
                        await self.api.send_message(row["chat_id"], args[:4000])
                        sent += 1
                    except Exception:
                        log.exception("broadcast failed")
                args = f"{args[:500]} | sent={sent}"
            elif name == "/user":
                row = await self.db.pool.fetchrow(
                    "SELECT * FROM users WHERE rubika_id=$1 OR id::text=$1",
                    args.strip(),
                )
                if not row:
                    raise ValueError("کاربر پیدا نشد.")
                await self.send(
                    chat,
                    f"کاربر {row['id']}\n{row['rubika_id']}\nموجودی {row['balance']:,}",
                )
                return
            elif name in {"/users_balance", "/users_referral", "/users_card"}:
                clauses = {
                    "/users_balance": "balance>0",
                    "/users_referral": "referred_by IS NOT NULL",
                    "/users_card": "card_verified",
                }
                rows = await self.db.pool.fetch(
                    f"""SELECT id,rubika_id,balance FROM users
                        WHERE {clauses[name]} ORDER BY id DESC LIMIT 100"""
                )
                await self.send(
                    chat,
                    "\n".join(
                        f"{row['id']} | {row['rubika_id']} | {row['balance']:,}" for row in rows
                    )
                    or "موردی نیست.",
                )
                return
            elif name in {"/block", "/unblock"}:
                await self.db.pool.execute(
                    "UPDATE users SET blocked=$1 WHERE id=$2",
                    name == "/block",
                    int(args),
                )
            elif name == "/order":
                row = await self.db.pool.fetchrow("SELECT * FROM orders WHERE id=$1", int(args))
                if not row:
                    raise ValueError("سفارش پیدا نشد.")
                await self.send(
                    chat,
                    f"سفارش #{row['id']}\nوضعیت: {row['status']}\nمبلغ: {row['total_amount']:,}",
                )
                return
            elif name == "/department_add":
                await self.db.pool.execute(
                    "INSERT INTO departments(title) VALUES($1)", args.strip()
                )
            elif name == "/department_delete":
                await self.db.pool.execute(
                    "UPDATE departments SET active=false WHERE id=$1", int(args)
                )
            elif name == "/channel_add":
                channel_id, title, url = args.split("|", 2)
                await self.db.pool.execute(
                    "INSERT INTO forced_channels(chat_id,title,invite_url) VALUES($1,$2,$3)",
                    channel_id.strip(),
                    title.strip(),
                    url.strip(),
                )
            elif name == "/channel_delete":
                await self.db.pool.execute(
                    "UPDATE forced_channels SET active=false WHERE id=$1", int(args)
                )
            else:
                raise ValueError("دستور مدیریت ناشناخته است.")
            await self.db.audit(admin_id, name, details=args)
            await self.send(chat, "✅ عملیات با موفقیت انجام شد.")
        except (ValueError, TypeError, IndexError) as exc:
            await self.send(chat, f"❌ {exc}")

    async def review_join(self, admin_id, user_id, approved):
        status = "approved" if approved else "rejected"
        row = await self.db.pool.fetchrow(
            """UPDATE join_requests SET status=$1,reviewed_by=$2
               WHERE id=(SELECT id FROM join_requests WHERE user_id=$3 AND status='pending'
                         ORDER BY id DESC LIMIT 1)
               RETURNING user_id""",
            status,
            admin_id,
            user_id,
        )
        if not row:
            raise ValueError("درخواست معتبری وجود ندارد.")
        user = await self.db.pool.fetchrow("SELECT chat_id FROM users WHERE id=$1", user_id)
        await self.api.send_message(
            user["chat_id"],
            "✅ عضویت تأیید شد؛ /start را بزن." if approved else "❌ عضویت تأیید نشد.",
        )

    async def review_receipt(self, admin_id, receipt_id, approved):
        async with self.db.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                receipt = await conn.fetchrow(
                    """SELECT r.*,p.amount,p.order_id,p.purpose,p.status payment_status,
                              o.status order_status
                       FROM receipts r JOIN payments p ON p.id=r.payment_id
                       LEFT JOIN orders o ON o.id=p.order_id
                       WHERE r.id=$1 FOR UPDATE OF r,p""",
                    receipt_id,
                )
                if not receipt or receipt["status"] != "pending":
                    raise ValueError("رسید قبلاً بررسی شده یا وجود ندارد.")
                status = "approved" if approved else "rejected"
                await conn.execute(
                    "UPDATE receipts SET status=$1,reviewed_by=$2,reviewed_at=now() WHERE id=$3",
                    status,
                    admin_id,
                    receipt_id,
                )
                if approved:
                    if receipt["payment_status"] != "pending":
                        raise ValueError("پرداخت دیگر در وضعیت انتظار نیست.")
                    if receipt["purpose"] == "order" and receipt["order_status"] != "pending":
                        raise ValueError("سفارش قبلاً پرداخت یا بسته شده است.")
                    await conn.execute(
                        "UPDATE payments SET status='verified',verified_at=now() WHERE id=$1",
                        receipt["payment_id"],
                    )
                    if receipt["purpose"] == "wallet":
                        reference = f"receipt:{receipt_id}"
                        await conn.execute(
                            """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                               VALUES($1,$2,'card_charge',$3)""",
                            receipt["user_id"],
                            receipt["amount"],
                            reference,
                        )
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            receipt["amount"],
                            receipt["user_id"],
                        )
                    else:
                        await conn.execute(
                            """UPDATE orders SET status='paid',payment_method='card',paid_at=now()
                               WHERE id=$1 AND status='pending'""",
                            receipt["order_id"],
                        )
        user = await self.db.pool.fetchrow(
            "SELECT chat_id FROM users WHERE id=$1", receipt["user_id"]
        )
        await self.api.send_message(
            user["chat_id"],
            "✅ رسید تأیید و پرداخت ثبت شد."
            if approved
            else "❌ رسید رد شد؛ با پشتیبانی تماس بگیر.",
        )
