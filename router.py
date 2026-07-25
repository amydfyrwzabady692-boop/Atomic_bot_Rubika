import logging
import os
import re

from database import Database
from keyboards import admin_menu, inline, main_menu
from payment_safety import MIN_WALLET_CHARGE, checked_amount, valid_card_number
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
        if (
            not event["event_id"].startswith("inline:")
            and not await self.db.claim_event(event["event_id"])
        ):
            return
        user = await self.db.user(event["sender_id"], event["chat_id"])
        if user["blocked"] and not await self.db.is_admin(event["sender_id"], self.config.admin_id):
            await self.send(event["chat_id"], "🚫 حساب شما مسدود است.")
            return
        action = event["button_id"] or event["text"].strip()
        if action in {"/start", "شروع", "home", "🏠 منوی کاربر"}:
            await self.db.set_session(event["sender_id"])
            await self.start(event, user)
            return
        is_admin = await self.db.is_admin(event["sender_id"], self.config.admin_id)
        receipt_in_progress = False
        if event.get("file"):
            current_state, _ = await self.db.session(event["sender_id"])
            receipt_in_progress = current_state == "card_receipt"
        if (
            action != "join_request"
            and not is_admin
            and not receipt_in_progress
            and not await self.can_use_bot(user["id"])
        ):
            await self.start(event, user)
            return
        if action == "/admin" or action.startswith("admin_"):
            if is_admin:
                await self.admin(event, action)
            else:
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
            return
        if action.startswith(
            (
                "receipt_ok:",
                "receipt_no:",
                "receipt_review:",
                "receipt_apply:",
                "join_review:",
                "join_apply:",
                "card_verify:",
                "ticket:",
                "ticket_close:",
                "order_complete:",
            )
        ):
            if not is_admin:
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
            await self.handle_admin_action(event, action)
            return
        if action.startswith("/") and is_admin:
            await self.admin_command(event, action)
            return
        if action.startswith("product:"):
            product_arg = action.removeprefix("product:")
            if not product_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه محصول نامعتبر است.")
                return
            await self.product_selected(event, user, int(product_arg))
            return
        if action.startswith("gem_buy:"):
            product_arg = action.removeprefix("gem_buy:")
            if not product_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه بسته نامعتبر است.")
                return
            await self.ask_gem_player_id(event, int(product_arg))
            return
        if action.startswith("category:"):
            category_arg = action.removeprefix("category:")
            if not category_arg.isdigit():
                await self.send(event["chat_id"], "❌ دسته‌بندی نامعتبر است.")
                return
            await self.show_store_category(event, int(category_arg))
            return
        if action == "gem_confirm":
            state, data = await self.db.session(event["sender_id"])
            if state != "gem_confirm" or not str(data.get("product_id", "")).isdigit():
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
            if state != "gem_confirm" or not str(data.get("product_id", "")).isdigit():
                await self.send(event["chat_id"], "جلسه خرید منقضی شده؛ دوباره بسته را انتخاب کن.")
                return
            await self.ask_gem_player_id(event, int(data["product_id"]))
            return
        if action == "gem_cancel":
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], "✖️ ثبت سفارش لغو شد.", menu=main_menu())
            return
        if action.startswith("pay:"):
            match = re.fullmatch(r"pay:(gateway|card|wallet):([1-9]\d*)", action)
            if not match:
                await self.send(event["chat_id"], "❌ اطلاعات پرداخت نامعتبر است.")
                return
            await self.pay_order(event, user, int(match.group(2)), match.group(1))
            return
        if action.startswith("pay_cancel:"):
            order_arg = action.removeprefix("pay_cancel:")
            if not order_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
                return
            try:
                refunded = await self.db.cancel_order(user["id"], int(order_arg))
                text = "✖️ سفارش لغو شد."
                if refunded:
                    text += f"\n💰 {refunded:,} تومان به کیف پول برگشت."
                await self.send(event["chat_id"], text, menu=main_menu())
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
            return
        if action.startswith("wallet_pay:"):
            match = re.fullmatch(r"wallet_pay:(gateway|card):([1-9]\d*)", action)
            if not match:
                await self.send(event["chat_id"], "❌ اطلاعات شارژ نامعتبر است.")
                return
            await self.start_wallet_charge(
                event,
                user,
                int(match.group(2)),
                match.group(1),
            )
            return
        if action.startswith("wallet_preset:"):
            amount_arg = action.removeprefix("wallet_preset:")
            try:
                amount = checked_amount(
                    amount_arg,
                    minimum=MIN_WALLET_CHARGE,
                    label="مبلغ شارژ",
                )
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            await self.show_wallet_charge_methods(event, amount)
            return
        if action.startswith("support_dept:"):
            department_arg = action.removeprefix("support_dept:")
            if not department_arg.isdigit():
                await self.send(event["chat_id"], "❌ دپارتمان نامعتبر است.")
                return
            department = await self.db.pool.fetchrow(
                "SELECT title FROM departments WHERE id=$1 AND active",
                int(department_arg),
            )
            if not department:
                await self.send(event["chat_id"], "این دپارتمان فعال نیست.")
                return
            await self.db.set_session(
                event["sender_id"],
                "support_message",
                {"department": department["title"]},
            )
            prompt = await self.db.setting("support_prompt", "")
            await self.send(
                event["chat_id"],
                prompt
                or f"پیامت را برای دپارتمان «{department['title']}» بنویس:",
            )
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
            "store": lambda: self.store_menu(event),
            "🛍 فروشگاه": lambda: self.store_menu(event),
            "🛍 فروشگاه اکانت": lambda: self.store_menu(event),
            "wallet": lambda: self.wallet(event, user),
            "💰 کیف پول": lambda: self.wallet(event, user),
            "wallet_charge": lambda: self.ask_wallet_charge(event),
            "orders": lambda: self.orders(event, user),
            "📦 سفارش‌های من": lambda: self.orders(event, user),
            "account": lambda: self.account(event, user),
            "👤 حساب من": lambda: self.account(event, user),
            "account_card": lambda: self.ask_account_card(event),
            "account_referral": lambda: self.ask_referral(event),
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
            if action not in {"wallet_charge", "join_request"}:
                await self.db.set_session(event["sender_id"])
            await handler()
            return
        state, data = await self.db.session(event["sender_id"])
        if state:
            await self.handle_state(event, user, state, data)
            return
        await self.send(event["chat_id"], "از منوی پایین انتخاب کن 👇", menu=main_menu())

    async def can_use_bot(self, user_id: int) -> bool:
        channels_exist = await self.db.pool.fetchval(
            "SELECT 1 FROM forced_channels WHERE active LIMIT 1"
        )
        if not channels_exist:
            return True
        return bool(
            await self.db.pool.fetchval(
                """SELECT 1 FROM join_requests
                   WHERE user_id=$1 AND status='approved'
                   ORDER BY id DESC LIMIT 1""",
                user_id,
            )
        )

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
        request = await self.db.pool.fetchrow(
            """INSERT INTO join_requests(user_id,status) VALUES($1,'pending')
               ON CONFLICT DO NOTHING RETURNING id""",
            user["id"],
        )
        if not request:
            await self.send(
                event["chat_id"],
                "⏳ درخواست قبلی هنوز در انتظار بررسی مدیر است.",
            )
            return
        await self.send(
            event["chat_id"],
            "✅ درخواست بررسی ثبت شد. پس از تأیید مدیر، منوی ربات باز می‌شود.",
        )
        await self.send(
            self.config.admin_chat_id,
            f"📢 درخواست عضویت #{request['id']}\n"
            f"کاربر: {user['rubika_id']}\nشناسه داخلی: {user['id']}",
            buttons=inline(
                [
                    [
                        (f"join_review:ok:{user['id']}", "✅ تأیید"),
                        (f"join_review:no:{user['id']}", "❌ رد"),
                    ]
                ]
            ),
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

    async def store_menu(self, event):
        categories = await self.db.pool.fetch(
            """SELECT c.id,c.title,count(p.id) product_count
               FROM categories c
               LEFT JOIN products p ON p.category_id=c.id
                 AND p.kind='store' AND p.active AND p.stock>0
               WHERE c.active
               GROUP BY c.id,c.title HAVING count(p.id)>0
               ORDER BY c.id"""
        )
        uncategorized = await self.db.pool.fetchval(
            """SELECT count(*) FROM products
               WHERE kind='store' AND active AND stock>0 AND category_id IS NULL"""
        )
        buttons = [
            [(f"category:{row['id']}", f"{row['title']} ({row['product_count']})")]
            for row in categories
        ]
        if uncategorized:
            buttons.append([("category:0", f"سایر محصولات ({uncategorized})")])
        if not buttons:
            await self.send(event["chat_id"], "فعلاً محصول فعالی در فروشگاه نیست.")
            return
        buttons.append([("home", "🏠 بازگشت")])
        await self.send(
            event["chat_id"],
            "🛍 دسته‌بندی فروشگاه را انتخاب کن:",
            buttons=inline(buttons),
        )

    async def show_store_category(self, event, category_id):
        if category_id:
            rows = await self.db.pool.fetch(
                """SELECT * FROM products WHERE kind='store' AND category_id=$1
                   AND active AND stock>0 ORDER BY price,id""",
                category_id,
            )
        else:
            rows = await self.db.pool.fetch(
                """SELECT * FROM products WHERE kind='store' AND category_id IS NULL
                   AND active AND stock>0 ORDER BY price,id"""
            )
        if not rows:
            await self.send(event["chat_id"], "محصول فعالی در این دسته نیست.")
            return
        await self.send(
            event["chat_id"],
            "یک محصول را انتخاب کن:",
            buttons=inline(
                [
                    [(f"product:{row['id']}", f"{row['title']} — {row['price']:,} تومان")]
                    for row in rows
                ]
                + [[("store", "🔙 بازگشت به دسته‌ها")]]
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
        if await self.db.setting("sales_enabled", "1") != "1":
            await self.send(event["chat_id"], "⛔ فروش موقتاً متوقف شده است.")
            return
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
        try:
            order, product = await self.db.create_order(user["id"], product_id, player_id)
        except ValueError as exc:
            await self.send(event["chat_id"], f"❌ {exc}", menu=main_menu())
            return
        balance = await self.db.pool.fetchval(
            "SELECT balance FROM users WHERE id=$1",
            user["id"],
        )
        await self.send(
            event["chat_id"],
            f"🧾 سفارش #{order['id']}\n{product['title']}\n"
            f"مبلغ: {order['payable_amount']:,} تومان\n"
            f"موجودی کیف پول: {balance:,} تومان\nروش پرداخت:",
            buttons=inline(
                [
                    [(f"pay:gateway:{order['id']}", "🌐 درگاه زرین‌پال")],
                    [(f"pay:card:{order['id']}", "💳 کارت‌به‌کارت")],
                    [(f"pay:wallet:{order['id']}", "💰 کیف پول")],
                    [(f"pay_cancel:{order['id']}", "✖️ لغو سفارش")],
                ]
            ),
        )

    async def pay_order(self, event, user, order_id, method):
        if await self.db.setting("payments_enabled", "1") != "1":
            await self.send(event["chat_id"], "⛔ پرداخت جدید موقتاً متوقف شده است.")
            return
        order = await self.db.pool.fetchrow(
            "SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user["id"]
        )
        if (
            not order
            or order["status"] != "pending"
            or not order["inventory_reserved"]
            or order["payable_amount"] <= 0
        ):
            await self.send(event["chat_id"], "این سفارش قابل پرداخت نیست.")
            return
        await self.db.set_session(event["sender_id"])
        if method == "wallet":
            try:
                result = await self.db.wallet_pay(user["id"], order_id)
                if result["paid"]:
                    await self.send(
                        event["chat_id"],
                        f"✅ پرداخت {result['used']:,} تومان از کیف پول انجام شد.\n"
                        f"موجودی جدید: {result['balance']:,} تومان",
                        menu=main_menu(),
                    )
                else:
                    await self.send(
                        event["chat_id"],
                        f"✅ مبلغ {result['used']:,} تومان از کیف پول کسر شد.\n"
                        f"موجودی جدید: {result['balance']:,} تومان\n"
                        f"باقی‌مانده سفارش: {result['remaining']:,} تومان\n\n"
                        "روش پرداخت باقی‌مانده را انتخاب کن:",
                        buttons=inline(
                            [
                                [(f"pay:gateway:{order_id}", "🌐 درگاه زرین‌پال")],
                                [(f"pay:card:{order_id}", "💳 کارت‌به‌کارت")],
                                [(f"pay_cancel:{order_id}", "✖️ لغو و بازگشت وجه")],
                            ]
                        ),
                    )
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
            return
        if method == "gateway" and await self.db.setting("zarinpal_enabled", "1") != "1":
            await self.send(event["chat_id"], "درگاه زرین‌پال فعلاً غیرفعال است.")
            return
        if method == "card" and await self.db.setting("card_enabled", "1") != "1":
            await self.send(event["chat_id"], "کارت‌به‌کارت فعلاً غیرفعال است.")
            return
        number = ""
        holder = ""
        bank = ""
        if method == "card":
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
        try:
            payment = await self.db.create_payment(
                user["id"],
                order_id,
                "order",
                method,
                order["payable_amount"],
                self.config.payment_ttl_minutes,
            )
        except ValueError as exc:
            await self.send(event["chat_id"], f"❌ {exc}")
            return
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
        await self.db.set_session(event["sender_id"], "card_receipt", {"payment_id": payment["id"]})
        await self.send(
            event["chat_id"],
            f"💳 مبلغ دقیق: {payment['amount']:,} تومان\n"
            f"کارت: {self.pretty_card(number)}\n"
            f"به نام: {holder}\nبانک: {bank}\n\nتصویر رسید را همین‌جا ارسال کن.",
        )

    async def wallet(self, event, user):
        fresh = await self.db.pool.fetchrow("SELECT * FROM users WHERE id=$1", user["id"])
        await self.send(
            event["chat_id"],
            f"💰 کیف پول Atomic\nموجودی: {fresh['balance']:,} تومان\n\n"
            "یک مبلغ را انتخاب کن یا مبلغ دلخواه بفرست:",
            buttons=inline(
                [
                    [
                        ("wallet_preset:50000", "۵۰ هزار"),
                        ("wallet_preset:100000", "۱۰۰ هزار"),
                    ],
                    [
                        ("wallet_preset:200000", "۲۰۰ هزار"),
                        ("wallet_preset:500000", "۵۰۰ هزار"),
                    ],
                    [("wallet_charge", "✏️ مبلغ دلخواه")],
                ]
            ),
        )

    async def ask_wallet_charge(self, event):
        await self.db.set_session(event["sender_id"], "wallet_amount")
        await self.send(event["chat_id"], "مبلغ شارژ را به تومان بفرست (حداقل ۱۰٬۰۰۰):")

    async def show_wallet_charge_methods(self, event, amount):
        await self.db.set_session(event["sender_id"])
        await self.send(
            event["chat_id"],
            f"💰 مبلغ شارژ: {amount:,} تومان\nروش پرداخت را انتخاب کن:",
            buttons=inline(
                [
                    [(f"wallet_pay:gateway:{amount}", "🌐 درگاه زرین‌پال")],
                    [(f"wallet_pay:card:{amount}", "💳 کارت‌به‌کارت")],
                    [("wallet", "🔙 بازگشت")],
                ]
            ),
        )

    async def start_wallet_charge(self, event, user, amount, method):
        try:
            amount = checked_amount(
                amount,
                minimum=MIN_WALLET_CHARGE,
                label="مبلغ شارژ کیف پول",
            )
        except ValueError as exc:
            await self.send(event["chat_id"], f"❌ {exc}")
            return
        if await self.db.setting("payments_enabled", "1") != "1":
            await self.send(event["chat_id"], "⛔ پرداخت جدید موقتاً متوقف شده است.")
            return
        if method == "gateway":
            if await self.db.setting("zarinpal_enabled", "1") != "1":
                await self.send(event["chat_id"], "درگاه زرین‌پال فعلاً غیرفعال است.")
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
                amount,
                f"Atomic Rubika wallet #{payment['id']}",
                callback,
            )
            if error:
                await self.send(event["chat_id"], f"❌ {error}")
                return
            await self.db.attach_authority(payment["id"], authority)
            await self.send(
                event["chat_id"],
                "✅ لینک شارژ ساخته شد.\nVPN را خاموش کن و لینک را باز کن:\n\n"
                f"{url}\n\nاین لینک قابل کپی است.",
            )
            return
        if await self.db.setting("card_enabled", "1") != "1":
            await self.send(event["chat_id"], "کارت‌به‌کارت فعلاً غیرفعال است.")
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
            await self.send(event["chat_id"], "شماره کارت تنظیم نشده است.")
            return
        payment = await self.db.create_payment(
            user["id"],
            None,
            "wallet",
            "card",
            amount,
            self.config.payment_ttl_minutes,
        )
        await self.db.set_session(
            event["sender_id"],
            "card_receipt",
            {"payment_id": payment["id"]},
        )
        await self.send(
            event["chat_id"],
            f"💳 شارژ کیف پول: {amount:,} تومان\n"
            f"کارت: {self.pretty_card(number)}\n"
            f"به نام: {holder or '—'}\nبانک: {bank or '—'}\n\n"
            "مبلغ را دقیق واریز کن و تصویر رسید را همین‌جا بفرست.",
        )

    @staticmethod
    def pretty_card(number):
        digits = "".join(character for character in str(number) if character.isdigit())
        if len(digits) == 16:
            return " ".join(digits[index : index + 4] for index in range(0, 16, 4))
        return str(number)

    async def orders(self, event, user):
        rows = await self.db.pool.fetch(
            """SELECT id,status,total_amount,discount_amount,payable_amount,created_at
               FROM orders
               WHERE user_id=$1 ORDER BY id DESC LIMIT 10""",
            user["id"],
        )
        labels = {
            "pending": "در انتظار پرداخت",
            "paid": "پرداخت‌شده",
            "processing": "در حال انجام",
            "completed": "تکمیل‌شده",
            "cancelled": "لغوشده",
            "expired": "منقضی",
            "delivery_failed": "نیازمند پیگیری",
        }
        text = "📦 سفارش‌های اخیر:\n" + (
            "\n".join(
                f"#{row['id']} | {labels.get(row['status'], row['status'])} | "
                f"{row['total_amount']-row['discount_amount']:,} تومان"
                for row in rows
            )
            if rows
            else "هنوز سفارشی نداری."
        )
        await self.send(event["chat_id"], text)

    async def account(self, event, user):
        fresh = await self.db.pool.fetchrow(
            "SELECT * FROM users WHERE id=$1",
            user["id"],
        )
        card_status = (
            "تأییدشده"
            if fresh["card_verified"]
            else "در انتظار تأیید" if fresh["card_number"] else "ثبت‌نشده"
        )
        await self.send(
            event["chat_id"],
            f"👤 شناسه روبیکا: {fresh['rubika_id']}\n"
            f"💰 موجودی: {fresh['balance']:,} تومان\n"
            f"💳 شماره کارت: {card_status}\n"
            f"📅 عضویت: {fresh['created_at']:%Y-%m-%d}",
            buttons=inline(
                [
                    [("account_card", "💳 ثبت یا ویرایش شماره کارت")],
                    [("account_referral", "👥 ثبت کد معرف")],
                ]
            ),
        )

    async def ask_account_card(self, event):
        await self.db.set_session(event["sender_id"], "account_card")
        await self.send(
            event["chat_id"],
            "شماره کارت ۱۶ رقمی متعلق به خودت را بدون فاصله بفرست:",
        )

    async def ask_referral(self, event):
        await self.db.set_session(event["sender_id"], "account_referral")
        await self.send(
            event["chat_id"],
            "شناسه عددی کاربر معرف را بفرست:",
        )

    async def ask_support(self, event):
        departments = await self.db.pool.fetch(
            "SELECT id,title FROM departments WHERE active ORDER BY id"
        )
        if not departments:
            await self.db.set_session(
                event["sender_id"],
                "support_message",
                {"department": "عمومی"},
            )
            prompt = await self.db.setting(
                "support_prompt",
                "پیام خودت را برای پشتیبانی بنویس:",
            )
            await self.send(
                event["chat_id"],
                prompt or "پیام خودت را برای پشتیبانی بنویس:",
            )
            return
        await self.send(
            event["chat_id"],
            "دپارتمان پشتیبانی را انتخاب کن:",
            buttons=inline(
                [
                    [(f"support_dept:{row['id']}", row["title"])]
                    for row in departments
                ]
                + [[("home", "🏠 بازگشت")]]
            ),
        )

    async def ask_promo(self, event):
        await self.db.set_session(event["sender_id"], "promo_code")
        await self.send(event["chat_id"], "کد هدیه یا تخفیف را ارسال کن:")

    async def help(self, event):
        default = (
            "📚 از منوی پایین بخش موردنظر را انتخاب کن.\n"
            "پرداخت درگاه فقط پس از تأیید مستقیم زرین‌پال ثبت می‌شود.\n"
            "برای کارت‌به‌کارت، خرید فقط بعد از تأیید مدیر انجام خواهد شد."
        )
        text = await self.db.setting("help_text", default)
        await self.send(
            event["chat_id"],
            text or default,
        )

    async def handle_state(self, event, user, state, data):
        if state == "gem_player_id":
            if not str(data.get("product_id", "")).isdigit():
                await self.db.set_session(event["sender_id"])
                await self.send(
                    event["chat_id"],
                    "جلسه خرید نامعتبر شد؛ دوباره بسته را انتخاب کن.",
                )
                return
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
            raw_amount = (
                event["text"]
                .translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                .replace(",", "")
                .replace("،", "")
                .replace(" ", "")
            )
            try:
                amount = checked_amount(
                    raw_amount,
                    minimum=MIN_WALLET_CHARGE,
                    label="مبلغ شارژ",
                )
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            await self.show_wallet_charge_methods(event, amount)
        elif state == "support_message":
            text = event["text"].strip()
            if not text:
                await self.send(event["chat_id"], "پیام متنی بفرست.")
                return
            ticket = await self.db.pool.fetchrow(
                """INSERT INTO tickets(user_id,department)
                   VALUES($1,$2) RETURNING id""",
                user["id"],
                str(data.get("department") or "عمومی")[:120],
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
            try:
                await self.api.send_message(
                    self.config.admin_chat_id,
                    f"🎧 تیکت #{ticket['id']} از {event['sender_id']}\n"
                    f"دپارتمان: {data.get('department') or 'عمومی'}\n{text}\n\n"
                    f"پاسخ: /reply {ticket['id']} متن",
                )
            except RubikaAPIError:
                log.exception("Ticket %s saved but admin notification failed", ticket["id"])
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
        elif state == "account_card":
            card_number = (
                event["text"]
                .translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                .replace(" ", "")
                .replace("-", "")
            )
            if not valid_card_number(card_number):
                await self.send(event["chat_id"], "❌ شماره کارت بانکی معتبر نیست.")
                return
            await self.db.pool.execute(
                """UPDATE users SET card_number=$1,card_verified=false
                   WHERE id=$2""",
                card_number,
                user["id"],
            )
            await self.db.set_session(event["sender_id"])
            await self.send(
                event["chat_id"],
                "✅ شماره کارت ثبت شد و پس از بررسی مدیر فعال می‌شود.",
                menu=main_menu(),
            )
            await self.send(
                self.config.admin_chat_id,
                f"💳 بررسی شماره کارت کاربر\n"
                f"کاربر: {user['rubika_id']}\n"
                f"کارت: {self.pretty_card(card_number)}",
                buttons=inline(
                    [
                        [
                            (f"card_verify:ok:{user['id']}", "✅ تأیید کارت"),
                            (f"card_verify:no:{user['id']}", "❌ رد کارت"),
                        ]
                    ]
                ),
            )
        elif state == "account_referral":
            referral_arg = (
                event["text"]
                .translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                .strip()
            )
            if not referral_arg.isdigit() or int(referral_arg) == user["id"]:
                await self.send(event["chat_id"], "❌ کد معرف معتبر نیست.")
                return
            changed = await self.db.pool.execute(
                """UPDATE users SET referred_by=$1
                   WHERE id=$2 AND referred_by IS NULL
                     AND EXISTS(SELECT 1 FROM users WHERE id=$1)""",
                int(referral_arg),
                user["id"],
            )
            if not changed.endswith("1"):
                await self.send(
                    event["chat_id"],
                    "کد معرف قبلاً ثبت شده یا کاربر معرف پیدا نشد.",
                )
                return
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], "✅ کد معرف ثبت شد.", menu=main_menu())
        elif state == "card_receipt":
            if not str(data.get("payment_id", "")).isdigit():
                await self.db.set_session(event["sender_id"])
                await self.send(
                    event["chat_id"],
                    "جلسه پرداخت منقضی شده؛ دوباره روش پرداخت را انتخاب کن.",
                )
                return
            file_info = event.get("file")
            if not isinstance(file_info, dict):
                await self.send(
                    event["chat_id"],
                    "📷 فقط تصویر یا فایل رسید معتبر را ارسال کن.",
                )
                return
            file_id = str(file_info.get("file_id") or file_info.get("id") or "")
            if not file_id or not event["message_id"]:
                await self.send(event["chat_id"], "فایل رسید قابل شناسایی نیست؛ دوباره ارسال کن.")
                return
            try:
                receipt = await self.db.submit_receipt(
                    payment_id=int(data["payment_id"]),
                    user_id=user["id"],
                    source_chat_id=event["chat_id"],
                    source_message_id=event["message_id"],
                    file_id=file_id,
                )
            except (TypeError, ValueError) as exc:
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], f"❌ {exc}", menu=main_menu())
                return
            await self.db.set_session(event["sender_id"])
            if not receipt:
                await self.send(event["chat_id"], "این رسید قبلاً ثبت شده است.")
                return
            detail = await self.db.pool.fetchrow(
                """SELECT p.amount,p.purpose,p.order_id
                   FROM receipts r JOIN payments p ON p.id=r.payment_id
                   WHERE r.id=$1""",
                receipt["id"],
            )
            purpose = (
                f"سفارش #{detail['order_id']}"
                if detail["purpose"] == "order"
                else "شارژ کیف پول"
            )
            await self.send(event["chat_id"], f"✅ رسید #{receipt['id']} ثبت و منتظر تأیید است.")
            try:
                await self.send(
                    self.config.admin_chat_id,
                    f"🧾 رسید جدید #{receipt['id']}\n"
                    f"نوع: {purpose}\n"
                    f"مبلغ مورد انتظار: {detail['amount']:,} تومان\n"
                    f"کاربر: {user['rubika_id']}\n"
                    "برای بررسی یکی از گزینه‌های زیر را بزن:",
                    buttons=inline(
                        [
                            [
                                (f"receipt_review:ok:{receipt['id']}", "✅ تأیید رسید"),
                                (f"receipt_review:no:{receipt['id']}", "❌ رد رسید"),
                            ]
                        ]
                    ),
                )
                await self.api.forward_message(
                    event["chat_id"], event["message_id"], self.config.admin_chat_id
                )
            except RubikaAPIError:
                log.exception(
                    "Receipt %s saved but admin notification was incomplete",
                    receipt["id"],
                )

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
                f"جمع فروش: {s['revenue']:,} تومان\n"
                f"مغایرت دفتر کیف پول: {s['wallet_mismatches']:,}",
            )
        elif action == "admin_receipts":
            rows = await self.db.pool.fetch(
                """SELECT r.id,r.payment_id,r.user_id,r.source_chat_id,
                          r.source_message_id,p.amount,p.purpose,p.order_id,
                          p.expires_at,u.rubika_id
                   FROM receipts r
                   JOIN payments p ON p.id=r.payment_id
                   JOIN users u ON u.id=r.user_id
                   WHERE r.status='pending' AND p.status='pending'
                   ORDER BY r.id LIMIT 10"""
            )
            if not rows:
                await self.send(chat, "🧾 رسید تأییدنشده‌ای وجود ندارد.")
                return
            await self.send(chat, f"🧾 {len(rows)} رسید تأییدنشده:")
            for row in rows:
                purpose = (
                    f"سفارش #{row['order_id']}"
                    if row["purpose"] == "order"
                    else "شارژ کیف پول"
                )
                await self.send(
                    chat,
                    f"🧾 رسید #{row['id']}\n"
                    f"نوع: {purpose}\n"
                    f"مبلغ: {row['amount']:,} تومان\n"
                    f"کاربر: {row['rubika_id']}\n"
                    f"مهلت: {row['expires_at']:%Y-%m-%d %H:%M}",
                    buttons=inline(
                        [
                            [
                                (f"receipt_review:ok:{row['id']}", "✅ تأیید"),
                                (f"receipt_review:no:{row['id']}", "❌ رد"),
                            ]
                        ]
                    ),
                )
                try:
                    await self.api.forward_message(
                        row["source_chat_id"],
                        row["source_message_id"],
                        chat,
                    )
                except RubikaAPIError:
                    await self.send(
                        chat,
                        f"⚠️ تصویر رسید #{row['id']} قابل فوروارد نبود؛ "
                        "اطلاعات متنی و دکمه‌های بررسی همچنان معتبرند.",
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
                buttons=(
                    inline(
                        [
                            [(f"ticket:{row['id']}", f"مشاهده تیکت #{row['id']}")]
                            for row in rows[:10]
                        ]
                    )
                    if rows
                    else None
                ),
            )
        elif action == "admin_fx":
            rate = await usd_toman_rate(
                await self.db.setting("usd_toman_rate", "")
            )
            profit = await self.db.pool.fetchrow(
                """SELECT count(*) orders,
                          coalesce(sum(gross_profit_toman),0) profit
                   FROM profit_snapshots"""
            )
            if rate.get("ok"):
                rate_text = f"{rate['rate']:,} تومان ({rate['source']})"
                products = await self.db.pool.fetch(
                    """SELECT id,title,price,supplier_cost_usd FROM products
                       WHERE active AND supplier_cost_usd IS NOT NULL
                       ORDER BY id LIMIT 50"""
                )
                product_lines = []
                for product in products:
                    cost = round(float(product["supplier_cost_usd"]) * rate["rate"])
                    product_lines.append(
                        f"#{product['id']} {product['title']}: "
                        f"هزینه {cost:,} | فروش {product['price']:,} | "
                        f"سود تقریبی {product['price']-cost:,}"
                    )
            else:
                rate_text = "دریافت نشد؛ نرخ دستی را تنظیم کن."
                product_lines = []
            await self.send(
                chat,
                f"💵 نرخ دلار: {rate_text}\n"
                f"سفارش‌های محاسبه‌شده: {profit['orders']:,}\n"
                f"سود ناخالص ثبت‌شده: {profit['profit']:,} تومان\n\n"
                + (
                    "قیمت واقعی بسته‌ها:\n" + "\n".join(product_lines)
                    if product_lines
                    else "برای نمایش هزینه واقعی، cost_usd محصولات را تنظیم کن."
                ),
            )
        elif action == "admin_products":
            rows = await self.db.pool.fetch(
                """SELECT id,kind,title,price,stock,active
                   FROM products ORDER BY kind,id LIMIT 100"""
            )
            lines = [
                f"#{row['id']} | {row['kind']} | {row['title']} | "
                f"{row['price']:,} ت | موجودی {row['stock']} | "
                f"{'فعال' if row['active'] else 'غیرفعال'}"
                for row in rows
            ]
            await self.send(
                chat,
                "📦 محصولات\n" + ("\n".join(lines) if lines else "محصولی نیست.")
                + "\n\n"
                + self.admin_help(action),
            )
        elif action == "admin_categories":
            rows = await self.db.pool.fetch(
                "SELECT id,title,active FROM categories ORDER BY id"
            )
            await self.send(
                chat,
                "🗂 دسته‌بندی‌ها\n"
                + (
                    "\n".join(
                        f"#{row['id']} | {row['title']} | "
                        f"{'فعال' if row['active'] else 'غیرفعال'}"
                        for row in rows
                    )
                    or "دسته‌ای نیست."
                )
                + "\n\n"
                + self.admin_help(action),
            )
        elif action == "admin_finance":
            values = {
                key: await self.db.setting(key, "")
                for key in (
                    "payments_enabled",
                    "zarinpal_enabled",
                    "card_enabled",
                    "card_number",
                    "card_holder",
                    "card_bank",
                )
            }
            values["card_number"] = values["card_number"] or os.getenv(
                "CARD_TRANSFER_NUMBER", ""
            ).strip()
            values["card_holder"] = values["card_holder"] or os.getenv(
                "CARD_TRANSFER_HOLDER", ""
            ).strip()
            values["card_bank"] = values["card_bank"] or os.getenv(
                "CARD_TRANSFER_BANK", ""
            ).strip()
            masked_card = (
                f"**** **** **** {values['card_number'][-4:]}"
                if len(values["card_number"]) >= 4
                else "تنظیم‌نشده"
            )
            await self.send(
                chat,
                "💳 تنظیمات مالی\n"
                f"پرداخت‌ها: {'فعال' if values['payments_enabled']=='1' else 'غیرفعال'}\n"
                f"زرین‌پال: {'فعال' if values['zarinpal_enabled']=='1' else 'غیرفعال'}\n"
                f"کارت‌به‌کارت: {'فعال' if values['card_enabled']=='1' else 'غیرفعال'}\n"
                f"کارت: {masked_card}\n"
                f"دارنده: {values['card_holder'] or '—'}\n"
                f"بانک: {values['card_bank'] or '—'}\n\n"
                + self.admin_help(action),
            )
        elif action == "admin_codes":
            rows = await self.db.pool.fetch(
                """SELECT id,code,code_type,value,used_count,max_uses,active
                   FROM promo_codes ORDER BY id DESC LIMIT 50"""
            )
            await self.send(
                chat,
                "🎁 کدها\n"
                + (
                    "\n".join(
                        f"#{row['id']} {row['code']} | {row['code_type']} "
                        f"{row['value']} | {row['used_count']}/{row['max_uses']} | "
                        f"{'فعال' if row['active'] else 'حذف‌شده'}"
                        for row in rows
                    )
                    or "کدی نیست."
                )
                + "\n\n"
                + self.admin_help(action),
            )
        elif action == "admin_settings":
            admins = await self.db.pool.fetch(
                "SELECT rubika_id,title FROM admins WHERE active ORDER BY created_at"
            )
            departments = await self.db.pool.fetch(
                "SELECT id,title FROM departments WHERE active ORDER BY id"
            )
            channels = await self.db.pool.fetch(
                "SELECT id,title,chat_id FROM forced_channels WHERE active ORDER BY id"
            )
            await self.send(
                chat,
                "⚙️ تنظیمات فروشگاه\n"
                "مدیران:\n"
                + (
                    "\n".join(
                        f"• {row['rubika_id']} | {row['title'] or 'مدیر'}"
                        for row in admins
                    )
                    or "• فقط مدیر اصلی"
                )
                + "\n\nدپارتمان‌ها:\n"
                + (
                    "\n".join(
                        f"#{row['id']} {row['title']}" for row in departments
                    )
                    or "موردی نیست."
                )
                + "\n\nکانال‌های عضویت اجباری:\n"
                + (
                    "\n".join(
                        f"#{row['id']} {row['title']} | {row['chat_id']}"
                        for row in channels
                    )
                    or "غیرفعال"
                )
                + "\n\n"
                + self.admin_help(action),
            )
        elif action in {"admin_search", "admin_broadcast"}:
            await self.send(chat, self.admin_help(action))
        else:
            await self.send(chat, self.admin_help(action))

    async def handle_admin_action(self, event, action):
        chat = event["chat_id"]
        admin_id = event["sender_id"]
        receipt_match = re.fullmatch(
            r"receipt_(?:review|ok|no):(ok|no):?([1-9]\d*)",
            action,
        )
        if not receipt_match:
            legacy = re.fullmatch(r"receipt_(ok|no):([1-9]\d*)", action)
            if legacy:
                receipt_match = legacy
        if receipt_match:
            decision, receipt_arg = receipt_match.groups()
            receipt_id = int(receipt_arg)
            await self.send(
                chat,
                (
                    f"⚠️ تأیید نهایی رسید #{receipt_id} و ثبت قطعی وجه؟"
                    if decision == "ok"
                    else f"رد نهایی رسید #{receipt_id}؟"
                ),
                buttons=inline(
                    [
                        [
                            (
                                f"receipt_apply:{decision}:{receipt_id}",
                                "✅ بله، انجام شود",
                            ),
                            ("admin_receipts", "🔙 انصراف"),
                        ]
                    ]
                ),
            )
            return
        apply_match = re.fullmatch(r"receipt_apply:(ok|no):([1-9]\d*)", action)
        if apply_match:
            decision, receipt_arg = apply_match.groups()
            receipt_id = int(receipt_arg)
            approved = decision == "ok"
            try:
                await self.review_receipt(admin_id, receipt_id, approved)
                await self.db.audit(
                    admin_id,
                    "/receipt_ok" if approved else "/receipt_no",
                    details=str(receipt_id),
                )
                await self.send(
                    chat,
                    f"✅ رسید #{receipt_id} {'تأیید' if approved else 'رد'} شد.",
                    menu=admin_menu(),
                )
            except ValueError as exc:
                await self.send(chat, f"❌ {exc}")
            return
        join_review = re.fullmatch(r"join_review:(ok|no):([1-9]\d*)", action)
        if join_review:
            decision, user_arg = join_review.groups()
            await self.send(
                chat,
                f"{'تأیید' if decision == 'ok' else 'رد'} عضویت کاربر {user_arg}؟",
                buttons=inline(
                    [
                        [
                            (
                                f"join_apply:{decision}:{user_arg}",
                                "✅ تأیید نهایی",
                            ),
                            ("admin_settings", "🔙 انصراف"),
                        ]
                    ]
                ),
            )
            return
        join_apply = re.fullmatch(r"join_apply:(ok|no):([1-9]\d*)", action)
        if join_apply:
            decision, user_arg = join_apply.groups()
            try:
                await self.review_join(admin_id, int(user_arg), decision == "ok")
                await self.db.audit(
                    admin_id,
                    "/join_ok" if decision == "ok" else "/join_no",
                    details=user_arg,
                )
                await self.send(chat, "✅ درخواست عضویت بررسی شد.")
            except ValueError as exc:
                await self.send(chat, f"❌ {exc}")
            return
        card_match = re.fullmatch(r"card_verify:(ok|no):([1-9]\d*)", action)
        if card_match:
            decision, user_arg = card_match.groups()
            user_id = int(user_arg)
            row = await self.db.pool.fetchrow(
                """UPDATE users SET card_verified=$1,
                   card_number=CASE WHEN $1 THEN card_number ELSE '' END
                   WHERE id=$2 AND card_number<>'' RETURNING chat_id""",
                decision == "ok",
                user_id,
            )
            if not row:
                await self.send(chat, "درخواست کارت معتبر نیست.")
                return
            await self.api.send_message(
                row["chat_id"],
                "✅ شماره کارت شما تأیید شد."
                if decision == "ok"
                else "❌ شماره کارت شما تأیید نشد؛ دوباره ثبت کن.",
            )
            await self.db.audit(
                admin_id,
                "card_verify",
                details=f"user={user_id};decision={decision}",
            )
            await self.send(chat, "✅ وضعیت کارت ثبت شد.")
            return
        ticket_match = re.fullmatch(r"ticket:([1-9]\d*)", action)
        if ticket_match:
            ticket_id = int(ticket_match.group(1))
            ticket = await self.db.pool.fetchrow(
                """SELECT t.*,u.rubika_id FROM tickets t
                   JOIN users u ON u.id=t.user_id WHERE t.id=$1""",
                ticket_id,
            )
            if not ticket:
                await self.send(chat, "تیکت پیدا نشد.")
                return
            messages = await self.db.pool.fetch(
                """SELECT sender_type,text,created_at FROM ticket_messages
                   WHERE ticket_id=$1 ORDER BY id LIMIT 50""",
                ticket_id,
            )
            transcript = "\n\n".join(
                f"{'کاربر' if row['sender_type']=='user' else 'مدیر'}: {row['text']}"
                for row in messages
            )
            await self.send(
                chat,
                f"🎧 تیکت #{ticket_id}\n"
                f"کاربر: {ticket['rubika_id']}\n"
                f"دپارتمان: {ticket['department']}\n"
                f"وضعیت: {ticket['status']}\n\n"
                f"{transcript or 'پیامی ثبت نشده.'}\n\n"
                f"پاسخ: /reply {ticket_id} متن",
                buttons=inline(
                    [[(f"ticket_close:{ticket_id}", "✅ بستن تیکت")]]
                ),
            )
            return
        close_match = re.fullmatch(r"ticket_close:([1-9]\d*)", action)
        if close_match:
            ticket_id = int(close_match.group(1))
            changed = await self.db.pool.execute(
                """UPDATE tickets SET status='closed',updated_at=now()
                   WHERE id=$1 AND status='open'""",
                ticket_id,
            )
            await self.send(
                chat,
                "✅ تیکت بسته شد." if changed.endswith("1") else "این تیکت قبلاً بسته شده است.",
            )
            return
        complete_match = re.fullmatch(r"order_complete:([1-9]\d*)", action)
        if complete_match:
            order_id = int(complete_match.group(1))
            row = await self.db.pool.fetchrow(
                """UPDATE orders SET status='completed'
                   WHERE id=$1 AND status IN ('paid','processing')
                   RETURNING user_id""",
                order_id,
            )
            if not row:
                await self.send(chat, "سفارش قابل تکمیل نیست.")
                return
            await self.db.pool.execute(
                """UPDATE fulfillments SET status='COMPLETED',updated_at=now()
                   WHERE order_id=$1""",
                order_id,
            )
            user = await self.db.pool.fetchrow(
                "SELECT chat_id FROM users WHERE id=$1",
                row["user_id"],
            )
            await self.api.send_message(
                user["chat_id"],
                f"✅ سفارش #{order_id} تکمیل و تحویل شد.",
            )
            await self.db.audit(admin_id, "order_complete", details=str(order_id))
            await self.send(chat, f"✅ سفارش #{order_id} تکمیل شد.")
            return
        await self.send(chat, "❌ عملیات مدیریتی نامعتبر است.")

    def admin_help(self, section):
        docs = {
            "admin_products": "/product_add kind|title|price|stock|amount|sku|cost_usd\n/product_edit id|field|value\nفیلدهای قابل ویرایش شامل category_id هم هستند.\n/product_delete id",
            "admin_categories": "/category_add title\n/category_delete id",
            "admin_finance": "/setting payments_enabled 1|0\n/setting zarinpal_enabled 1|0\n/setting card_enabled 1|0\n/setting card_number NUMBER\n/setting card_holder NAME\n/setting card_bank BANK\n/setting usd_toman_rate NUMBER",
            "admin_search": "/user شناسه\n/order شماره",
            "admin_broadcast": "/broadcast متن پیام",
            "admin_codes": "/code_add gift|CODE|VALUE|MAX\n/code_add discount|CODE|PERCENT|MAX\n/code_delete ID",
            "admin_settings": "/setting sales_enabled 1|0\n/setting welcome_text TEXT\n/setting help_text TEXT\n/setting support_prompt TEXT\n/admin_add ID TITLE\n/admin_delete ID\n/channel_add CHAT|TITLE|URL\n/channel_delete ID\n/department_add TITLE\n/department_delete ID",
        }
        return "راهنمای این بخش:\n" + docs.get(section, "دستور این بخش تعریف نشده است.")

    async def admin_command(self, event, command):
        admin_id, chat = event["sender_id"], event["chat_id"]
        name, _, args = command.partition(" ")
        try:
            if name == "/reply":
                ticket_id, text = args.split(" ", 1)
                if not text.strip():
                    raise ValueError("متن پاسخ خالی است.")
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
                receipt_arg = args.strip()
                if not receipt_arg.isdigit() or int(receipt_arg) <= 0:
                    raise ValueError(
                        f"شماره رسید را وارد کن؛ مثال: {name} 1"
                    )
                await self.review_receipt(admin_id, int(receipt_arg), name.endswith("_ok"))
            elif name in {"/join_ok", "/join_no"}:
                await self.review_join(admin_id, int(args), name.endswith("_ok"))
            elif name == "/setting":
                key, value = args.split(" ", 1)
                allowed = {
                    "welcome_text",
                    "help_text",
                    "support_prompt",
                    "support_id",
                    "sales_enabled",
                    "payments_enabled",
                    "zarinpal_enabled",
                    "card_enabled",
                    "card_number",
                    "card_holder",
                    "card_bank",
                    "usd_toman_rate",
                }
                if key not in allowed:
                    raise ValueError("کلید تنظیمات مجاز نیست.")
                if key.endswith("_enabled") and value.strip() not in {"0", "1"}:
                    raise ValueError("مقدار این تنظیم فقط 0 یا 1 است.")
                if key in {"welcome_text", "help_text", "support_prompt"} and not value.strip():
                    raise ValueError("متن تنظیم نمی‌تواند خالی باشد.")
                if key == "card_number":
                    normalized_card = re.sub(r"[\s-]+", "", value).translate(
                        str.maketrans(
                            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                            "01234567890123456789",
                        )
                    )
                    if not valid_card_number(normalized_card):
                        raise ValueError("شماره کارت بانکی معتبر نیست.")
                    value = normalized_card
                if key == "usd_toman_rate":
                    normalized_rate = int(value.replace(",", "").strip())
                    if not 10_000 <= normalized_rate <= 10_000_000:
                        raise ValueError("نرخ دلار خارج از محدوده مجاز است.")
                    value = str(normalized_rate)
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
                kind = kind.strip()
                if kind not in {"gem", "sense_mobile", "sense_pc", "store"}:
                    raise ValueError("نوع محصول مجاز نیست.")
                if not title.strip():
                    raise ValueError("عنوان محصول خالی است.")
                safe_price = checked_amount(price, label="قیمت محصول")
                safe_stock = int(stock)
                if safe_stock < 0:
                    raise ValueError("موجودی محصول منفی نمی‌تواند باشد.")
                safe_amount = int(amount) if amount.strip() else None
                safe_cost = float(cost) if cost.strip() else None
                if safe_amount is not None and safe_amount <= 0:
                    raise ValueError("تعداد محصول باید مثبت باشد.")
                if safe_cost is not None and safe_cost <= 0:
                    raise ValueError("هزینه دلاری باید مثبت باشد.")
                await self.db.pool.execute(
                    """INSERT INTO products(kind,title,price,stock,amount,supplier_sku,supplier_cost_usd)
                       VALUES($1,$2,$3,$4,$5,$6,$7)""",
                    kind,
                    title.strip(),
                    safe_price,
                    safe_stock,
                    safe_amount,
                    sku.strip(),
                    safe_cost,
                )
            elif name == "/product_edit":
                product_id, field, value = args.split("|", 2)
                product_id, field, value = (
                    product_id.strip(),
                    field.strip(),
                    value.strip(),
                )
                allowed = {
                    "title",
                    "price",
                    "stock",
                    "description",
                    "active",
                    "supplier_sku",
                    "supplier_cost_usd",
                    "amount",
                    "category_id",
                }
                if field not in allowed:
                    raise ValueError("فیلد غیرمجاز است.")
                if field == "price":
                    value = checked_amount(value, label="قیمت محصول")
                elif field in {"stock", "amount", "category_id"}:
                    value = int(value)
                    if field == "stock" and value < 0:
                        raise ValueError("موجودی محصول منفی نمی‌تواند باشد.")
                elif field == "supplier_cost_usd":
                    value = float(value)
                    if value <= 0:
                        raise ValueError("هزینه دلاری باید مثبت باشد.")
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
                if not args.strip():
                    raise ValueError("عنوان دسته‌بندی خالی است.")
                await self.db.pool.execute(
                    """INSERT INTO categories(title) VALUES($1)
                       ON CONFLICT(title) DO UPDATE SET active=true""",
                    args.strip(),
                )
            elif name == "/category_delete":
                await self.db.pool.execute(
                    "UPDATE categories SET active=false WHERE id=$1", int(args)
                )
            elif name == "/code_add":
                kind, code, value, max_uses = args.split("|", 3)
                kind = kind.strip()
                code = code.strip().upper()
                if kind not in {"gift", "discount"}:
                    raise ValueError("نوع کد فقط gift یا discount است.")
                if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
                    raise ValueError("ساختار کد معتبر نیست.")
                safe_value = int(value)
                safe_max_uses = int(max_uses)
                if safe_value <= 0 or safe_max_uses <= 0:
                    raise ValueError("مقدار و تعداد استفاده باید مثبت باشند.")
                if kind == "discount" and safe_value > 99:
                    raise ValueError("درصد تخفیف باید بین ۱ تا ۹۹ باشد.")
                if kind == "gift":
                    safe_value = checked_amount(safe_value, label="مبلغ کد هدیه")
                await self.db.pool.execute(
                    """INSERT INTO promo_codes(code_type,code,value,max_uses)
                       VALUES($1,upper($2),$3,$4)""",
                    kind,
                    code,
                    safe_value,
                    safe_max_uses,
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
                if not args.strip():
                    raise ValueError("متن پیام همگانی خالی است.")
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
                    """SELECT u.*,
                              (SELECT count(*) FROM users child
                               WHERE child.referred_by=u.id) referral_count,
                              (SELECT count(*) FROM orders o
                               WHERE o.user_id=u.id AND o.paid_at IS NOT NULL) purchases
                       FROM users u WHERE u.rubika_id=$1 OR u.id::text=$1""",
                    args.strip(),
                )
                if not row:
                    raise ValueError("کاربر پیدا نشد.")
                await self.send(
                    chat,
                    f"کاربر {row['id']}\n"
                    f"{row['rubika_id']}\n"
                    f"موجودی: {row['balance']:,}\n"
                    f"خریدها: {row['purchases']:,}\n"
                    f"زیرمجموعه‌ها: {row['referral_count']:,}\n"
                    f"کارت: {'تأییدشده' if row['card_verified'] else 'تأییدنشده'}\n"
                    f"وضعیت: {'مسدود' if row['blocked'] else 'فعال'}",
                )
                return
            elif name in {"/users_balance", "/users_referral", "/users_card"}:
                clauses = {
                    "/users_balance": "balance>0",
                    "/users_referral": "EXISTS(SELECT 1 FROM users child WHERE child.referred_by=users.id)",
                    "/users_card": "card_verified",
                }
                rows = await self.db.pool.fetch(
                    f"""SELECT id,rubika_id,balance,card_number FROM users
                        WHERE {clauses[name]} ORDER BY id DESC LIMIT 100"""
                )
                await self.send(
                    chat,
                    "\n".join(
                        f"{row['id']} | {row['rubika_id']} | {row['balance']:,}"
                        + (
                            f" | ****{row['card_number'][-4:]}"
                            if name == "/users_card" and row["card_number"]
                            else ""
                        )
                        for row in rows
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
                if not args.strip():
                    raise ValueError("عنوان دپارتمان خالی است.")
                await self.db.pool.execute(
                    """INSERT INTO departments(title) VALUES($1)
                       ON CONFLICT(title) DO UPDATE SET active=true""",
                    args.strip(),
                )
            elif name == "/department_delete":
                await self.db.pool.execute(
                    "UPDATE departments SET active=false WHERE id=$1", int(args)
                )
            elif name == "/channel_add":
                channel_id, title, url = args.split("|", 2)
                if not channel_id.strip() or not title.strip() or not url.strip():
                    raise ValueError("اطلاعات کانال کامل نیست.")
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """INSERT INTO forced_channels(chat_id,title,invite_url)
                               VALUES($1,$2,$3)
                               ON CONFLICT(chat_id) DO UPDATE SET
                               title=EXCLUDED.title,invite_url=EXCLUDED.invite_url,
                               active=true""",
                            channel_id.strip(),
                            title.strip(),
                            url.strip(),
                        )
                        await conn.execute(
                            """UPDATE join_requests SET status='stale'
                               WHERE status='approved'"""
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
            message = str(exc)
            if not re.search(r"[\u0600-\u06ff]", message):
                message = "فرمت دستور نادرست است؛ راهنمای همان بخش را دوباره باز کن."
            await self.send(chat, f"❌ {message}")
        except Exception:
            log.exception("Admin command failed: %s", name)
            await self.send(
                chat,
                "❌ عملیات انجام نشد؛ ورودی یا وضعیت فعلی را بررسی کن.",
            )

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
                metadata = await conn.fetchrow(
                    """SELECT r.payment_id,p.order_id,p.purpose
                       FROM receipts r JOIN payments p ON p.id=r.payment_id
                       WHERE r.id=$1""",
                    receipt_id,
                )
                if not metadata:
                    raise ValueError("رسید وجود ندارد.")
                order = None
                if metadata["purpose"] == "order":
                    order = await conn.fetchrow(
                        """SELECT status,wallet_paid,total_amount,
                                  discount_amount,inventory_reserved
                           FROM orders WHERE id=$1 FOR UPDATE""",
                        metadata["order_id"],
                    )
                receipt = await conn.fetchrow(
                    """SELECT r.*,p.amount,p.order_id,p.purpose,p.provider,
                              p.status payment_status,p.expires_at
                       FROM receipts r JOIN payments p ON p.id=r.payment_id
                       WHERE r.id=$1 FOR UPDATE OF r,p""",
                    receipt_id,
                )
                if not receipt or receipt["status"] != "pending":
                    raise ValueError("رسید قبلاً بررسی شده یا وجود ندارد.")
                if approved:
                    now = await conn.fetchval("SELECT now()")
                    if (
                        receipt["provider"] != "card"
                        or receipt["payment_status"] != "pending"
                        or receipt["expires_at"] <= now
                    ):
                        raise ValueError("پرداخت دیگر در وضعیت انتظار نیست.")
                    if receipt["purpose"] == "order" and (
                        not order
                        or order["status"] != "pending"
                        or not order["inventory_reserved"]
                    ):
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
                        changed = await conn.execute(
                            """UPDATE orders SET status='paid',payable_amount=0,
                               inventory_reserved=false,
                               payment_method=CASE WHEN wallet_paid>0
                                 THEN 'wallet+card' ELSE 'card' END,
                               paid_at=now()
                               WHERE id=$1 AND status='pending'
                                 AND inventory_reserved""",
                            receipt["order_id"],
                        )
                        if not changed.endswith("1"):
                            raise ValueError("سفارش دیگر قابل پرداخت نیست.")
                elif receipt["purpose"] == "order" and order and order["wallet_paid"]:
                    refund = int(order["wallet_paid"])
                    reference = f"receipt-reject:{receipt_id}:wallet-refund"
                    inserted = await conn.fetchval(
                        """INSERT INTO wallet_ledger(
                             user_id,amount,entry_type,reference
                           ) VALUES($1,$2,'order_refund',$3)
                           ON CONFLICT(reference) DO NOTHING RETURNING id""",
                        receipt["user_id"],
                        refund,
                        reference,
                    )
                    if inserted:
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            refund,
                            receipt["user_id"],
                        )
                    await conn.execute(
                        """UPDATE orders SET wallet_paid=0,
                           payable_amount=total_amount-discount_amount,
                           payment_method=NULL WHERE id=$1 AND status='pending'""",
                        receipt["order_id"],
                    )
                await conn.execute(
                    """UPDATE receipts SET status=$1,reviewed_by=$2,
                       reviewed_at=now() WHERE id=$3""",
                    "approved" if approved else "rejected",
                    admin_id,
                    receipt_id,
                )
                if not approved and receipt["payment_status"] == "pending":
                    await conn.execute(
                        "UPDATE payments SET status='rejected' WHERE id=$1",
                        receipt["payment_id"],
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
