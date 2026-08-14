import logging
import os
import re

import asyncpg

from admin_flows import AdminFlowHandlers
from credentials import CredentialHandlers
from database import Database
from keyboards import admin_menu, credential_staff_menu, inline, link_button, main_menu
from payment_safety import (
    MIN_WALLET_CHARGE,
    checked_amount,
    checked_decimal,
    order_amounts,
    supplier_cost_toman,
    valid_card_number,
)
from payments import Zarinpal
from rubika_api import RubikaAPI, RubikaAPIError
from supplier import G2Bulk, can_fulfill, usd_toman_rate

log = logging.getLogger(__name__)

GEM_PRODUCTS_PER_PAGE = 8


def _ordered_gem_catalogue(rows):
    """Show diamonds/memberships first and keep every Level Up pack on page 2."""
    return sorted(
        rows,
        key=lambda row: str(row.get("supplier_sku") or "")
        .strip()
        .casefold()
        .startswith("level up package"),
    )

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


class Router(CredentialHandlers, AdminFlowHandlers):
    def __init__(self, db: Database, api: RubikaAPI, config):
        self.db, self.api, self.config = db, api, config
        settings_getter = getattr(db, "setting", None)
        self.zarinpal = Zarinpal(
            settings_getter=settings_getter if callable(settings_getter) else None
        )
        self.g2 = G2Bulk()

    async def send(self, chat_id, text, *, menu=None, buttons=None):
        return await self.api.send_message(chat_id, text, chat_keypad=menu, inline_keypad=buttons)

    async def user_menu(self, rubika_id: str):
        # پنل مدیریت فقط برای مالک (RUBIKA_ADMIN_ID)، نه مدیران فرعی.
        is_owner = self.db.is_owner(rubika_id, self.config.admin_id)
        is_cred = (not is_owner) and await self.db.is_credential_admin(
            rubika_id, self.config.admin_id
        )
        return main_menu(is_admin=is_owner, is_cred_staff=is_cred)

    async def handle(self, event: dict):
        if not event["chat_id"] or not event["sender_id"]:
            return
        if (
            not event["event_id"].startswith("inline:")
            and not await self.db.claim_event(event["event_id"])
        ):
            return
        user = await self.db.user(
            event["sender_id"],
            event["chat_id"],
            event.get("display_name", ""),
        )
        if user["blocked"] and not await self.db.is_admin(event["sender_id"], self.config.admin_id):
            await self.send(event["chat_id"], "🚫 حساب شما مسدود است.")
            return
        action = event["button_id"] or event["text"].strip()
        _user_label_map = {
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
            "🔐 جم با اطلاعات · هفتگی / ماهانه": "gems_credentials",
            "🏠 بازگشت": "home",
            "🔙 منوی اصلی": "home",
            "🔙 روش‌های خرید": "gems",
            "🔙 پک سنس": "sense",
            "🔙 بازگشت به دسته‌ها": "store",
            "🔙 بازگشت به کیف پول": "wallet",
            "✅ بررسی عضویت": "join_request",
            "📱 موبایل": "sense_mobile",
            "🖥 PC": "sense_pc",
            "✏️ مبلغ دلخواه": "wallet_charge",
        }
        if not event["button_id"]:
            action = _user_label_map.get(action, action)
        # برخی کلاینت‌های روبیکا برای دکمه‌های chat_keypad فقط متن دکمه را
        # می‌فرستند نه button_id را؛ متن دکمه‌های منوی ادمین را به action درست تبدیل کن.
        _admin_label_map = {
            "📊 آمار کلی": "admin_stats",
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
            "📣 ارسال پیام": "admin_broadcast",
            "📣 پیام همگانی": "admin_broadcast",
            "🎁 کدها": "admin_codes",
            "⚙️ تنظیمات": "admin_settings",
            "👮 مدیریت مدیران": "admin_admins",
            "🚨 مرکز عملیات": "admin_ops",
            "🛍 مدیریت فروشگاه": "admin_shop",
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
            "🔐 جم با اطلاعات": "cred_admin_home",
            "🛠 پنل مدیریت": "admin_panel",
        }
        _cred_staff_label_map = {
            "🔐 پنل جم با اطلاعات": "cred_admin_home",
            "📦 سفارش‌های آماده": "cred_admin_list",
            "🎫 تیکت‌ها": "cred_admin_tickets",
        }
        is_owner = self.db.is_owner(event["sender_id"], self.config.admin_id)
        is_admin = await self.db.is_admin(event["sender_id"], self.config.admin_id)
        is_cred_staff = await self.ensure_credential_staff(event["sender_id"])
        if not event["button_id"]:
            if is_owner:
                action = _admin_label_map.get(action, action)
            elif is_cred_staff:
                action = _cred_staff_label_map.get(action, action)
        if action in {"/start", "شروع", "home", "🏠 منوی کاربر"}:
            await self.db.set_session(event["sender_id"])
            await self.start(event, user)
            return
        if action == "admin_panel":
            if is_owner:
                await self.open_admin_panel(event)
            else:
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
            return
        if action in {"admin_cancel_broadcast", "flow_cancel", "support_cancel"}:
            state, _ = await self.db.session(event["sender_id"])
            if action == "support_cancel" and state != "support_message":
                await self.send(event["chat_id"], "ثبت تیکت فعالی نیست.")
                return
            if action in {"admin_cancel_broadcast", "flow_cancel"} and not state:
                await self.send(event["chat_id"], "عملیات فعالی برای لغو نیست.")
                return
            await self.db.set_session(event["sender_id"])
            if is_owner:
                menu = admin_menu()
            elif is_cred_staff:
                menu = credential_staff_menu()
            else:
                menu = await self.user_menu(event["sender_id"])
            label = (
                "✖️ ثبت تیکت لغو شد."
                if action == "support_cancel"
                else "✖️ عملیات لغو شد."
            )
            await self.send(event["chat_id"], label, menu=menu)
            return
        if action in {"/myid", "myid", "🆔 شناسه من"}:
            await self.send(
                event["chat_id"],
                f"🆔 شناسه روبیکای شما:\n{event['sender_id']}",
            )
            return
        receipt_in_progress = False
        if event.get("file"):
            current_state, _ = await self.db.session(event["sender_id"])
            receipt_in_progress = current_state == "card_receipt"
        if (
            action != "join_request"
            and not is_admin
            and not is_cred_staff
            and not receipt_in_progress
            and not await self.can_use_bot(user["id"])
        ):
            await self.start(event, user)
            return
        # قیمت‌گذاری فقط برای ادمین کامل (قبل از مسیر عمومی cred_*)
        if action in {
            "admin_cred_pricing",
            "admin_pricing_home",
            "admin_pricing_sync",
            "admin_set_profit",
            "cred_pricing",
            "cred_price_sync",
            "admin_profit_sync",
            "admin_sync_prices",
        } or action.startswith(
            ("admin_cred_set_", "admin_set_gem_id_profit")
        ):
            if not is_owner:
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
            if action in {
                "admin_pricing_home",
                "admin_set_profit",
                "admin_cred_pricing",
            }:
                await self.admin_pricing_home(event, with_menu=True)
                return
            if action in {
                "admin_pricing_sync",
                "cred_price_sync",
                "admin_profit_sync",
                "admin_sync_prices",
            }:
                await self.admin_pricing_sync(event)
                return
            if action.startswith(("admin_cred_set_", "admin_set_gem_id_profit")):
                await self._start_credential_setting(event, action)
                return
            await self.dispatch_credential_action(event, user, action)
            return
        if action in {"/credadmin", "cred_admin_home"} or action.startswith(
            ("cred_admin_", "cred_")
        ):
            # Staff-only admin credential actions
            if action.startswith("cred_admin_") or action in {
                "/credadmin",
                "cred_admin_home",
            }:
                if not is_cred_staff:
                    await self.send(
                        event["chat_id"], "⛔️ دسترسی پنل جم با اطلاعات ندارید."
                    )
                    return
            if await self.dispatch_credential_action(event, user, action):
                return
        if action == "/admin" or action.startswith("admin_"):
            if is_owner:
                if action == "/admin":
                    await self.open_admin_panel(event)
                else:
                    await self.admin(event, action)
            elif action == "/admin":
                await self.send(
                    event["chat_id"],
                    "⛔️ دسترسی مدیر ندارید.\n"
                    f"🆔 شناسه شما: `{event['sender_id']}`\n\n"
                    "اگر مالک ربات هستید، این شناسه را در `RUBIKA_ADMIN_ID` بگذارید.\n"
                    "اگر پشتیبان جم با اطلاعات هستید، از دکمه پنل جم با اطلاعات استفاده کنید.",
                )
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
                "ticket_reply:",
                "order_complete:",
            )
        ):
            if is_admin:
                await self.handle_admin_action(event, action)
                return
            if is_cred_staff and (
                action.startswith("ticket:")
                or action.startswith("ticket_close:")
                or action.startswith("ticket_reply:")
            ):
                await self.handle_admin_action(event, action, credential_only=True)
                return
            await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
            return
        if action.startswith("/") and is_owner:
            # دستورات اسلش فقط به‌عنوان سازگاری قدیمی؛ پنل کاملاً دکمه‌ای است.
            await self.admin_command(event, action)
            return
        if action.startswith("/") and is_cred_staff and action.startswith(
            ("/reply", "/credadmin")
        ):
            await self.admin_command(event, action, credential_only=True)
            return
        if action == "noop":
            return
        if action.startswith("products_page:"):
            match = re.fullmatch(
                r"products_page:(gem|sense_mobile|sense_pc):([1-9]\d*)",
                action,
            )
            if not match:
                await self.send(event["chat_id"], "❌ صفحه محصول نامعتبر است.")
                return
            titles = {
                "gem": "🎮 محصولات فری‌فایر",
                "sense_mobile": "📱 سنسیویتی موبایل",
                "sense_pc": "🖥 سنسیویتی PC",
            }
            await self.show_products(
                event, match.group(1), titles[match.group(1)], int(match.group(2))
            )
            return
        if action.startswith("product:"):
            match = re.fullmatch(r"product:([1-9]\d*)(?::([1-9]\d*))?", action)
            if not match:
                await self.send(event["chat_id"], "❌ شناسه محصول نامعتبر است.")
                return
            await self.product_selected(
                event,
                user,
                int(match.group(1)),
                int(match.group(2) or 1),
            )
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
            await self.send(
                event["chat_id"],
                "✖️ ثبت سفارش لغو شد.",
                menu=await self.user_menu(event["sender_id"]),
            )
            return
        if action.startswith("pay_check:"):
            order_arg = action.removeprefix("pay_check:")
            if not order_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
                return
            await self.check_gateway_payment(event, user, order_id=int(order_arg))
            return
        if action.startswith("wallet_check:"):
            payment_arg = action.removeprefix("wallet_check:")
            if not payment_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه پرداخت نامعتبر است.")
                return
            await self.check_gateway_payment(event, user, payment_id=int(payment_arg))
            return
        if action.startswith("order_pay:"):
            order_arg = action.removeprefix("order_pay:")
            if not order_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
                return
            await self.resume_order_payment(event, user, int(order_arg))
            return
        if action == "card_receipt_cancel":
            state, data = await self.db.session(event["sender_id"])
            if state != "card_receipt":
                await self.send(event["chat_id"], "فرآیند پرداخت فعالی نیست.")
                return
            order_id = data.get("order_id")
            payment_id = data.get("payment_id")
            await self.db.set_session(event["sender_id"])
            if payment_id and str(payment_id).isdigit():
                await self.db.pool.execute(
                    """UPDATE payments SET status='cancelled'
                       WHERE id=$1 AND user_id=$2 AND status='pending'""",
                    int(payment_id),
                    user["id"],
                )
            if order_id and str(order_id).isdigit():
                order = await self.db.pool.fetchrow(
                    """SELECT id,status,payable_amount,wallet_paid,inventory_reserved
                       FROM orders WHERE id=$1 AND user_id=$2""",
                    int(order_id),
                    user["id"],
                )
                if (
                    order
                    and order["status"] == "pending"
                    and order["inventory_reserved"]
                    and int(order["payable_amount"] or 0) > 0
                ):
                    wallet_paid = int(order["wallet_paid"] or 0)
                    text = "✖️ ارسال رسید لغو شد."
                    if wallet_paid:
                        text += (
                            f"\n💰 {wallet_paid:,} تومان قبلاً از کیف پول کسر شده؛ "
                            "برای پرداخت باقی‌مانده روش دیگری انتخاب کن."
                        )
                    else:
                        text += "\nروش پرداخت را دوباره انتخاب کن:"
                    await self.send(
                        event["chat_id"],
                        text,
                        buttons=inline(
                            [
                                [(f"pay:gateway:{order_id}", "🌐 درگاه زرین‌پال")],
                                [(f"pay:card:{order_id}", "💳 کارت‌به‌کارت")],
                                [(f"pay_cancel:{order_id}", "✖️ لغو سفارش")],
                            ]
                        ),
                    )
                    return
            await self.send(
                event["chat_id"],
                "✖️ ارسال رسید لغو شد.",
                menu=await self.user_menu(event["sender_id"]),
            )
            return
        if action.startswith("pay:"):
            match = re.fullmatch(r"pay:(gateway|card|wallet):([1-9]\d*)", action)
            if not match:
                await self.send(event["chat_id"], "❌ اطلاعات پرداخت نامعتبر است.")
                return
            await self.pay_order(event, user, int(match.group(2)), match.group(1))
            return
        if action.startswith("pay_reopen:"):
            order_arg = action.removeprefix("pay_reopen:")
            if not order_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
                return
            payment = await self.db.active_order_gateway(user["id"], int(order_arg))
            if not payment:
                await self.send(event["chat_id"], "لینک پرداخت فعالی برای این سفارش پیدا نشد.")
                return
            await self.send_gateway_link(event, payment, int(order_arg), existing=True)
            return
        if action.startswith("pay_change:"):
            order_arg = action.removeprefix("pay_change:")
            if not order_arg.isdigit():
                await self.send(event["chat_id"], "❌ شناسه سفارش نامعتبر است.")
                return
            order_id = int(order_arg)
            try:
                await self.db.detach_order_gateway_to_wallet(user["id"], order_id)
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            await self.send(
                event["chat_id"],
                "✅ روش پرداخت قابل تغییر شد. لینک قبلی را دیگر استفاده نکن؛ "
                "اگر بعداً از همان لینک پرداخت شود، مبلغ فقط به کیف پولت اضافه می‌شود "
                "و سفارش دوباره تحویل نمی‌شود.\n\nروش جدید را انتخاب کن:",
                buttons=self.payment_method_buttons(order_id),
            )
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
                await self.send(
                    event["chat_id"],
                    text,
                    menu=await self.user_menu(event["sender_id"]),
                )
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
                buttons=inline([[("support_cancel", "✖️ انصراف")]]),
            )
            return
        routes = {
            "gems": lambda: self.freefire_menu(event),
            "💎 خرید جم": lambda: self.freefire_menu(event),
            "💎 جم فری‌فایر": lambda: self.freefire_menu(event),
            "🎮 محصولات فری‌فایر": lambda: self.freefire_menu(event),
            "gems_by_id": lambda: self.show_products(event, "gem", "🆔 جم با آیدی"),
            "gems_credentials": lambda: self.credential_products_menu(event),
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
            "myid": lambda: self.send(
                event["chat_id"],
                f"🆔 شناسه روبیکای شما:\n{event['sender_id']}",
            ),
            "🆔 شناسه من": lambda: self.send(
                event["chat_id"],
                f"🆔 شناسه روبیکای شما:\n{event['sender_id']}",
            ),
            "join_request": lambda: self.join_request(event, user),
        }
        _menu_reset_actions = {
            "home", "🏠 منوی کاربر", "/start", "شروع",
            "help", "📚 راهنما", "myid", "🆔 شناسه من",
        }
        active_state, active_data = await self.db.session(event["sender_id"])
        handler = routes.get(action)
        if handler:
            if (
                active_state
                and action not in _menu_reset_actions
                and action not in {"wallet_charge", "join_request"}
            ):
                await self.flow_in_progress_prompt(
                    event, active_state, active_data
                )
                return
            if action not in {"wallet_charge", "join_request"}:
                await self.db.set_session(event["sender_id"])
            await handler()
            return
        state, data = await self.db.session(event["sender_id"])
        if state:
            await self.handle_state(event, user, state, data)
            return
        await self.send(
            event["chat_id"],
            "از منوی پایین انتخاب کن 👇",
            menu=await self.user_menu(event["sender_id"]),
        )

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

    async def _invalidate_join_approvals(self):
        await self.db.pool.execute(
            """UPDATE join_requests SET status='stale'
               WHERE status='approved'"""
        )

    async def _delivery_preflight(self, order_id):
        """پیش از دریافت پول، موجودی سرویس تحویل (G2Bulk) را برای قلم‌های سفارش بررسی می‌کند.

        اگر موجودی کافی نباشد، کاربر نباید به مرحله پرداخت برود تا پولش کسر نشود.
        خروجی: (available, error_message, cost_usd, balance_usd)
        """
        rows = await self.db.pool.fetch(
            """SELECT p.supplier_sku,p.supplier_cost_usd,p.amount,p.kind
               FROM order_items i
               JOIN products p ON p.id=i.product_id
               WHERE i.order_id=$1""",
            order_id,
        )
        if not rows:
            return False, "قلمی برای سفارش پیدا نشد.", None, None
        force = True
        checked = False
        for row in rows:
            # فقط جم خودکار از G2Bulk چک می‌شود؛ بقیه (سنس/فروشگاه/جم با اطلاعات) دستی‌اند.
            if row["kind"] != "gem":
                continue
            if str(row["supplier_sku"] or "").strip().isdigit():
                amount = int(row["amount"] or row["supplier_sku"])
                catalogue = str(row["supplier_sku"] or amount)
            elif str(row["supplier_sku"] or "").strip():
                # نام کاتالوگ مانند "Level Up Package - Level 6" یا "Weekly Membership"
                amount = int(row["amount"] or 0)
                catalogue = str(row["supplier_sku"])
            else:
                continue
            available, cost_usd, balance, error = await can_fulfill(
                amount, catalogue, force=force
            )
            checked = True
            if not available:
                return False, error or "موجودی سرویس تأمین کافی نیست.", cost_usd, balance
            force = False
        if not checked and any(row["kind"] == "gem" for row in rows):
            # جم بدون sku قابل تحویل خودکار نیست — اجازه پرداخت بده و دستی هندل شود.
            return True, None, None, None
        return True, None, None, None

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
        is_owner = self.db.is_owner(event["sender_id"], self.config.admin_id)
        is_cred = (not is_owner) and await self.db.is_credential_admin(
            event["sender_id"], self.config.admin_id
        )
        if is_owner:
            text += "\n\n🛠 برای مدیریت، از دکمه «پنل مدیریت» در منو استفاده کن."
        elif is_cred:
            text += "\n\n🔐 برای سفارش‌های جم با اطلاعات، دکمه «پنل جم با اطلاعات» را بزن."
            await self.send(
                event["chat_id"],
                text,
                menu=credential_staff_menu(),
            )
            return
        await self.send(
            event["chat_id"],
            text,
            menu=await self.user_menu(event["sender_id"]),
        )

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

    async def show_products(self, event, kind, title, page=1):
        rows = await self.db.products(kind)
        if not rows:
            await self.send(event["chat_id"], "فعلاً محصول فعالی موجود نیست.")
            return
        if kind == "gem":
            rows = _ordered_gem_catalogue(rows)
            per_page = GEM_PRODUCTS_PER_PAGE
        else:
            per_page = 7
        total_pages = max(1, (len(rows) + per_page - 1) // per_page)
        page = max(1, min(int(page), total_pages))
        start = (page - 1) * per_page
        buttons = [
            [
                (
                    f"product:{r['id']}:{page}",
                    f"{r['title']} — {r['price']:,} تومان",
                )
            ]
            for r in rows[start:start + per_page]
        ]
        if total_pages > 1:
            navigation = [
                (
                    "noop"
                    if number == page
                    else f"products_page:{kind}:{number}",
                    f"• {number} •" if number == page else str(number),
                )
                for number in range(1, total_pages + 1)
            ]
            buttons.append(navigation)
        if kind == "gem":
            buttons.append([("gems", "🔙 روش‌های خرید")])
        elif kind.startswith("sense"):
            buttons.append([("sense", "🔙 پک سنس")])
        else:
            buttons.append([("home", "🏠 بازگشت")])
        await self.send(
            event["chat_id"],
            f"{title}\nبسته موردنظرت را انتخاب کن — صفحه {page} از {total_pages} 👇",
            buttons=inline(buttons),
        )

    async def product_selected(self, event, user, product_id, page=1):
        if await self.db.setting("sales_enabled", "1") != "1":
            await self.send(event["chat_id"], "⛔ فروش موقتاً متوقف شده است.")
            return
        product = await self.db.pool.fetchrow(
            "SELECT * FROM products WHERE id=$1 AND active AND stock>0", product_id
        )
        if not product:
            await self.send(event["chat_id"], "این محصول دیگر موجود نیست.")
            return
        kind = str(product["kind"] or "")
        title = str(product["title"] or "محصول")
        price = int(product["price"] or 0)
        if price <= 0:
            await self.send(
                event["chat_id"],
                "قیمت این محصول معتبر نیست؛ با پشتیبانی تماس بگیر.\n"
                f"آیدی پشتیبانی: {(await self.db.get_support_contact())['handle']}",
            )
            return
        if kind == "gem":
            supplier_sku = str(product["supplier_sku"] or "").strip()
            if supplier_sku.isdigit():
                amount = int(product["amount"] or supplier_sku)
                product_line = f"تعداد جم: {amount:,}"
            elif supplier_sku.startswith("Level Up Package"):
                product_line = "🎯 نوع بسته: ارتقای سطح"
            elif supplier_sku == "Weekly Membership":
                product_line = "📅 نوع بسته: هفتگی"
            elif supplier_sku == "Monthly Membership":
                product_line = "📆 نوع بسته: ماهانه"
            elif supplier_sku == "Booyah Pass":
                product_line = "🏆 نوع محصول: بویاه پس"
            else:
                product_line = "📦 محصول ویژه فری‌فایر"
            await self.send(
                event["chat_id"],
                f"🎮 {title}\n"
                f"{product_line}\n"
                f"💰 قیمت: {price:,} تومان\n\n"
                "برای ادامه خرید، بسته را تأیید کن.",
                buttons=inline(
                    [
                        [(f"gem_buy:{product_id}", "✅ خرید این بسته")],
                        [(f"products_page:gem:{max(1, int(page))}", "🔙 بازگشت به فهرست")],
                    ]
                ),
            )
            return
        if kind == "gem_credentials":
            await self.show_credential_product(event, product_id)
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
            await self.send(
                event["chat_id"], f"❌ {exc}", menu=await self.user_menu(event["sender_id"])
            )
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
            buttons=self.payment_method_buttons(order["id"]),
        )

    @staticmethod
    def payment_method_buttons(order_id):
        return inline(
            [
                [(f"pay:gateway:{order_id}", "🌐 درگاه زرین‌پال")],
                [(f"pay:card:{order_id}", "💳 کارت‌به‌کارت")],
                [(f"pay:wallet:{order_id}", "💰 کیف پول")],
                [(f"pay_cancel:{order_id}", "✖️ لغو سفارش")],
            ]
        )

    @staticmethod
    def gateway_followup_buttons(order_id=None, *, payment_id=None):
        rows = []
        if order_id is not None:
            rows.extend(
                [
                    [(f"pay_check:{order_id}", "✅ پرداخت کردم")],
                    [(f"pay_reopen:{order_id}", "🔁 نمایش دوباره لینک")],
                    [(f"pay_change:{order_id}", "🔄 تغییر روش پرداخت")],
                ]
            )
        elif payment_id is not None:
            rows.extend(
                [
                    [(f"wallet_check:{payment_id}", "✅ پرداخت کردم")],
                    [("wallet", "🔙 بازگشت به کیف پول")],
                ]
            )
        return inline(rows) if rows else None

    async def flow_in_progress_prompt(self, event, state, data):
        buttons = [[("home", "🏠 منوی کاربر")]]
        if state == "card_receipt":
            buttons = [[("card_receipt_cancel", "✖️ لغو ارسال رسید")]]
            if data.get("order_id"):
                buttons.append(
                    [(f"pay_cancel:{data['order_id']}", "✖️ لغو سفارش")]
                )
        elif state in {"gem_player_id", "gem_confirm"}:
            buttons = [[("gem_cancel", "✖️ انصراف از خرید")]]
        elif state == "support_message":
            buttons = [[("support_cancel", "✖️ انصراف از تیکت")]]
        elif state == "wallet_amount":
            buttons = [[("wallet", "🔙 بازگشت به کیف پول")]]
        elif state in {"promo_code", "account_card", "account_referral"}:
            buttons = [[("home", "🏠 منوی کاربر")]]
        elif state.startswith("admin_"):
            buttons = [[("admin_cancel_broadcast", "✖️ انصراف")]]
        elif state.startswith("cred_") or state == "admin_ticket_reply":
            buttons = [[("cred_cancel", "❌ انصراف")]]
            if state == "admin_ticket_reply":
                buttons = [[("flow_cancel", "✖️ انصراف")]]
        await self.send(
            event["chat_id"],
            "⚠️ یک فرآیند نیمه‌کاره داری.\n"
            "اول آن را تمام کن یا با دکمه زیر لغو کن.",
            buttons=inline(buttons),
        )

    async def check_gateway_payment(
        self, event, user, *, order_id=None, payment_id=None
    ):
        payment = None
        if order_id is not None:
            payment = await self.db.active_order_gateway(user["id"], order_id)
            if not payment:
                payment = await self.db.pool.fetchrow(
                    """SELECT p.* FROM payments p
                       JOIN orders o ON o.id=p.order_id
                       WHERE p.order_id=$1 AND o.user_id=$2
                         AND p.provider='gateway' AND p.authority IS NOT NULL
                         AND p.status IN ('pending','expired','cancelled','rejected')
                       ORDER BY p.id DESC LIMIT 1""",
                    order_id,
                    user["id"],
                )
        elif payment_id is not None:
            payment = await self.db.pool.fetchrow(
                """SELECT * FROM payments
                   WHERE id=$1 AND user_id=$2 AND provider='gateway'""",
                payment_id,
                user["id"],
            )
        if not payment or not payment.get("authority"):
            await self.send(
                event["chat_id"],
                "❌ لینک درگاه فعالی پیدا نشد. دوباره روش پرداخت را انتخاب کن.",
            )
            return
        if payment.get("status") == "verified":
            ref = payment.get("ref_id") or "—"
            await self.send(
                event["chat_id"],
                f"✅ این پرداخت قبلاً ثبت شده.\nکد پیگیری: {ref}",
                menu=await self.user_menu(event["sender_id"]),
            )
            return
        if payment.get("order_id"):
            order = await self.db.pool.fetchrow(
                "SELECT status FROM orders WHERE id=$1 AND user_id=$2",
                payment["order_id"],
                user["id"],
            )
            if order and order["status"] not in {"pending"}:
                await self.send(
                    event["chat_id"],
                    f"✅ سفارش #{payment['order_id']} دیگر در انتظار پرداخت نیست.",
                    menu=await self.user_menu(event["sender_id"]),
                )
                return
        await self.send(event["chat_id"], "⏳ در حال بررسی پرداخت در درگاه…")
        verify_status, ref_id = await self.zarinpal.verify(
            payment["amount"],
            payment["authority"],
        )
        if verify_status == "verified":
            try:
                finalized, changed = await self.db.finalize_gateway(
                    payment["authority"],
                    ref_id,
                )
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            user_menu = await self.user_menu(event["sender_id"])
            if changed:
                await self.send(
                    event["chat_id"],
                    f"✅ پرداخت {finalized['amount']:,} تومان ثبت شد.\n"
                    f"کد پیگیری: {ref_id}",
                    menu=user_menu,
                )
            else:
                await self.send(
                    event["chat_id"],
                    f"✅ این پرداخت قبلاً ثبت شده.\nکد پیگیری: {ref_id}",
                    menu=user_menu,
                )
            return
        if verify_status == "not_paid":
            followup = self.gateway_followup_buttons(
                order_id=payment.get("order_id"),
                payment_id=payment["id"] if not payment.get("order_id") else None,
            )
            await self.send(
                event["chat_id"],
                "❌ هنوز پرداخت در درگاه تأیید نشده.\n"
                "اگر واریز کرده‌ای، چند لحظه صبر کن و دوباره «پرداخت کردم» را بزن.",
                buttons=followup,
            )
            return
        await self.send(
            event["chat_id"],
            "⚠️ ارتباط با درگاه برقرار نشد؛ کمی بعد دوباره تلاش کن.",
            buttons=self.gateway_followup_buttons(
                order_id=payment.get("order_id"),
                payment_id=payment["id"] if not payment.get("order_id") else None,
            ),
        )

    async def resume_order_payment(self, event, user, order_id: int):
        order = await self.db.pool.fetchrow(
            """SELECT o.*,
                      (SELECT p.title FROM order_items oi
                       JOIN products p ON p.id=oi.product_id
                       WHERE oi.order_id=o.id LIMIT 1) AS title
               FROM orders o
               WHERE o.id=$1 AND o.user_id=$2""",
            order_id,
            user["id"],
        )
        if (
            not order
            or order["status"] != "pending"
            or not order["inventory_reserved"]
            or order["payable_amount"] <= 0
        ):
            await self.send(event["chat_id"], "این سفارش دیگر قابل پرداخت نیست.")
            return
        existing_gateway = await self.db.active_order_gateway(user["id"], order_id)
        if existing_gateway:
            await self.send_gateway_link(
                event, existing_gateway, order_id, existing=True
            )
            return
        balance = await self.db.pool.fetchval(
            "SELECT balance FROM users WHERE id=$1",
            user["id"],
        )
        await self.send(
            event["chat_id"],
            f"🧾 سفارش #{order_id}\n"
            f"{order.get('title') or 'محصول'}\n"
            f"مبلغ: {order['payable_amount']:,} تومان\n"
            f"موجودی کیف پول: {balance:,} تومان\nروش پرداخت:",
            buttons=self.payment_method_buttons(order_id),
        )

    async def admin_pricing_home(self, event, *, with_menu=False):
        """صفحه اول پنل: سود جم/هفتگی/ماهانه + قیمت‌گذاری جم با اطلاعات."""
        from supplier import compute_gem_sale_price, usd_toman_rate

        await self.db.set_session(event["sender_id"])
        cfg = await self.db.get_credential_pricing_config()
        gem_profit = await self.gem_profit_percent()
        manual = await self.db.setting("usd_toman_rate", "")
        live = await usd_toman_rate(manual, force=False)
        products = await self.db.credential_products_admin()
        last_sync = await self.db.setting_timestamp("gem_price_last_sync")
        lines = [
            "🛠 صفحه اول پنل مدیریت",
            "━━━━━━━━━━━━━━━",
            "📈 درصد سود",
            f"💎 جم با آیدی: {gem_profit}٪",
            f"📅 هفتگی: {cfg['weekly_profit']}٪",
            f"📆 ماهانه: {cfg['monthly_profit']}٪",
            "",
            "💱 قیمت‌گذاری جم با اطلاعات",
            "(مستقل از G2Bulk — فقط $ × نرخ لحظه‌ای × سود)",
            f"💵 بهای خالص هفتگی: {cfg['weekly_cost']} USD",
            f"💵 بهای خالص ماهانه: {cfg['monthly_cost']} USD",
        ]
        if live.get("ok"):
            weekly_sale = await compute_gem_sale_price(
                cfg["weekly_cost"], live["rate"], cfg["weekly_profit"]
            )
            monthly_sale = await compute_gem_sale_price(
                cfg["monthly_cost"], live["rate"], cfg["monthly_profit"]
            )
            lines.extend(
                [
                    f"💱 نرخ لحظه‌ای: {live['rate']:,} ت ({live['source']})",
                    f"📅 قیمت فروش هفتگی: {weekly_sale:,} ت",
                    f"📆 قیمت فروش ماهانه: {monthly_sale:,} ت",
                ]
            )
        else:
            lines.append(
                f"⚠️ نرخ لحظه‌ای نیامد؛ fallback: {manual or 'تنظیم نشده'}"
            )
        if last_sync:
            lines.append(f"🕐 آخرین sync: {last_sync.strftime('%Y-%m-%d %H:%M')}")
        lines.append("\nمحصولات فعال:")
        if products:
            for p in products:
                lines.append(f"• {p['title']} — {int(p['price']):,} ت")
        else:
            lines.append("• محصولی نیست — دکمه همگام‌سازی را بزن.")
        await self.send(
            event["chat_id"],
            "\n".join(lines),
            menu=admin_menu() if with_menu else None,
            buttons=inline(
                [
                    [
                        ("admin_set_gem_id_profit", "✏️ سود جم"),
                        ("admin_cred_set_weekly_profit", "✏️ سود هفتگی"),
                    ],
                    [("admin_cred_set_monthly_profit", "✏️ سود ماهانه")],
                    [
                        ("admin_cred_set_weekly_cost", "✏️ $ هفتگی"),
                        ("admin_cred_set_monthly_cost", "✏️ $ ماهانه"),
                    ],
                    [("admin_pricing_sync", "🔄 همگام‌سازی قیمت‌ها")],
                ]
            ),
        )

    async def admin_pricing_sync(self, event):
        """همگام‌سازی جم با اطلاعات — فقط $1.328/$6.64 × نرخ لحظه‌ای (بدون G2Bulk)."""
        chat = event["chat_id"]
        await self.send(chat, "⏳ همگام‌سازی قیمت جم با اطلاعات…")
        try:
            cred = await self.sync_credential_prices_now(force=True)
        except Exception as exc:
            log.exception("Credential price sync crashed")
            await self.send(chat, f"❌ خطا:\n{exc}")
            return
        if not cred.get("ok"):
            await self.send(
                chat,
                f"❌ نشد:\n{cred.get('error')}\n"
                "از 💳 امور مالی → ✏️ نرخ دلار یک عدد بگذار (fallback).",
            )
            return
        await self.send(chat, self.format_credential_sync_report(cred))
        await self.admin_pricing_home(event, with_menu=False)

    async def profit_settings_hub(self, event):
        await self.admin_pricing_home(event, with_menu=False)

    async def _start_credential_setting(self, event, action: str):
        mapping = {
            "admin_set_gem_id_profit": (
                "gem_profit_percent",
                "profit",
                "💎 سود جم با آیدی (۱ تا ۲۰۰٪)",
            ),
            "admin_cred_set_weekly_profit": (
                "credential_weekly_profit_percent",
                "profit",
                "📅 سود هفتگی جم با اطلاعات (۱ تا ۲۰۰٪)",
            ),
            "admin_cred_set_monthly_profit": (
                "credential_monthly_profit_percent",
                "profit",
                "📆 سود ماهانه جم با اطلاعات (۱ تا ۲۰۰٪)",
            ),
            "admin_cred_set_weekly_cost": (
                "credential_weekly_cost_usd",
                "cost",
                "💵 بهای دلاری هفتگی (مثلاً 1.328)",
            ),
            "admin_cred_set_monthly_cost": (
                "credential_monthly_cost_usd",
                "cost",
                "💵 بهای دلاری ماهانه (مثلاً 6.64)",
            ),
        }
        item = mapping.get(action)
        if not item:
            await self.send(event["chat_id"], "❌ تنظیم نامعتبر است.")
            return
        key, kind, title = item
        current = await self.db.setting(key, "")
        await self.db.set_session(
            event["sender_id"],
            "admin_cred_setting",
            {"key": key, "kind": kind},
        )
        await self.send(
            event["chat_id"],
            f"{title}\nمقدار فعلی: {current or '—'}\n\nمقدار جدید را بفرست:",
            buttons=inline(
                [[("admin_pricing_home", "🔙 قیمت و سود"), ("admin_cancel_broadcast", "✖️ انصراف")]]
            ),
        )

    async def send_card_transfer_messages(
        self,
        chat_id,
        *,
        amount: int,
        holder: str,
        bank: str,
        number: str,
        title: str,
        buttons=None,
    ):
        """پیام‌های کارت‌به‌کارت را جدا می‌فرستد تا کپی و خواندن راحت‌تر باشد."""
        await self.send(
            chat_id,
            f"💳 {title}\n\nمبلغ دقیق واریز:\n{int(amount):,} تومان",
            buttons=buttons,
        )
        await self.send(
            chat_id,
            f"👤 به نام: {holder or '—'}\n🏦 بانک: {bank or '—'}",
        )
        await self.send(chat_id, self.pretty_card(number))
        await self.send(
            chat_id,
            "پس از واریز، تصویر رسید را همین‌جا ارسال کن.",
        )

    async def send_gateway_link(
        self, event, payment, order_id=None, *, existing=False, url=None
    ):
        url = url or (self.zarinpal.start_base + str(payment["authority"]))
        label = "لینک پرداخت قبلی" if existing else "لینک پرداخت امن"
        rows = [[link_button("unused", "🔗 باز کردن درگاه پرداخت", url)]]
        if order_id is not None:
            rows.append([(f"pay_change:{order_id}", "🔄 تغییر امن روش پرداخت")])
        text = (
            f"✅ {label}\n"
            f"مبلغ: {int(payment['amount']):,} تومان\n\n"
            "لینک پرداخت (روی آن بزن تا باز شود):"
        )
        try:
            await self.send(event["chat_id"], text, buttons=inline(rows))
            # لینک را به‌صورت یک پیام جدا می‌فرستیم تا روبیکا آن را به‌عنوان
            # لینکِ قابل‌کلیک (تپ‌شدنی) تشخیص دهد؛ دکمه Link در برخی نسخه‌های
            # روبیکا باز نمی‌شود.
            await self.send(event["chat_id"], url)
            if order_id is not None:
                await self.send(
                    event["chat_id"],
                    "اگر لینک باز نشد، روی لینک بالا بزن یا کپی کن.",
                    buttons=self.gateway_followup_buttons(order_id),
                )
        except RubikaAPIError:
            log.exception("Rubika rejected gateway Link keypad; sending plain URL")
            fallback = []
            if order_id is not None:
                fallback = [[(f"pay_change:{order_id}", "🔄 تغییر امن روش پرداخت")]]
            await self.send(
                event["chat_id"],
                text + f"\n{url}",
                buttons=inline(fallback) if fallback else None,
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
        existing_gateway = await self.db.active_order_gateway(user["id"], order_id)
        if existing_gateway:
            if method == "gateway":
                await self.send_gateway_link(event, existing_gateway, order_id, existing=True)
            else:
                await self.send(
                    event["chat_id"],
                    "برای این سفارش یک لینک درگاه صادر شده است. می‌توانی همان لینک را "
                    "باز کنی یا با دکمه زیر روش پرداخت را به‌صورت امن تغییر بدهی.",
                    buttons=inline(
                        [
                            [(f"pay_reopen:{order_id}", "🔗 نمایش لینک قبلی")],
                            [(f"pay_check:{order_id}", "✅ پرداخت کردم")],
                            [(f"pay_change:{order_id}", "🔄 تغییر امن روش پرداخت")],
                        ]
                    ),
                )
            return
        if method == "wallet":
            available, preflight_error, _cost, _balance = await self._delivery_preflight(
                order_id
            )
            if not available:
                await self.send(
                    event["chat_id"],
                    "❌ موجودی سرویس تحویل برای این بسته کافی نیست؛ "
                    "برای جلوگیری از کسر پول، پرداخت باز نشد.",
                )
                return
            try:
                result = await self.db.wallet_pay(user["id"], order_id)
                user_menu = await self.user_menu(event["sender_id"])
                if result["paid"]:
                    await self.send(
                        event["chat_id"],
                        f"✅ پرداخت {result['used']:,} تومان از کیف پول انجام شد.\n"
                        f"موجودی جدید: {result['balance']:,} تومان",
                        menu=user_menu,
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
        # بررسی موجودی سرویس تحویل (G2Bulk) پیش از دریافت پول — تا کاربر وقتی
        # موجودی تأمین‌کننده کافی نیست اصلاً به پرداخت نرود و پولش کسر نشود.
        available, preflight_error, _cost, _balance = await self._delivery_preflight(
            order_id
        )
        if not available:
            await self.send(
                event["chat_id"],
                "❌ موجودی سرویس تحویل برای این بسته کافی نیست؛ "
                "برای جلوگیری از کسر پول، پرداخت باز نشد.",
            )
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
                await self.db.pool.execute(
                    """UPDATE payments SET status='cancelled'
                       WHERE id=$1 AND status='pending'""",
                    payment["id"],
                )
                await self.send(event["chat_id"], f"❌ {error}")
                return
            await self.db.attach_authority(payment["id"], authority)
            payment = dict(payment)
            payment["authority"] = authority
            await self.send_gateway_link(event, payment, order_id, url=url)
            await self.send(
                event["chat_id"],
                "پس از پرداخت در درگاه، «پرداخت کردم» را بزن تا سریع‌تر ثبت شود.",
                buttons=self.gateway_followup_buttons(order_id),
            )
            return
        await self.db.set_session(
            event["sender_id"],
            "card_receipt",
            {"payment_id": payment["id"], "order_id": order_id},
        )
        await self.send_card_transfer_messages(
            event["chat_id"],
            amount=payment["amount"],
            holder=holder,
            bank=bank,
            number=number,
            title="کارت‌به‌کارت سفارش",
            buttons=inline(
                [
                    [("card_receipt_cancel", "✖️ انصراف")],
                    [(f"pay_cancel:{order_id}", "✖️ لغو سفارش")],
                ]
            ),
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
                await self.db.pool.execute(
                    """UPDATE payments SET status='cancelled'
                       WHERE id=$1 AND status='pending'""",
                    payment["id"],
                )
                await self.send(event["chat_id"], f"❌ {error}")
                return
            await self.db.attach_authority(payment["id"], authority)
            payment = dict(payment)
            payment["authority"] = authority
            await self.send_gateway_link(event, payment, url=url)
            await self.send(
                event["chat_id"],
                "پس از پرداخت در درگاه، «پرداخت کردم» را بزن.",
                buttons=self.gateway_followup_buttons(payment_id=payment["id"]),
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
        await self.send_card_transfer_messages(
            event["chat_id"],
            amount=amount,
            holder=holder,
            bank=bank,
            number=number,
            title="شارژ کیف پول",
            buttons=inline([[("card_receipt_cancel", "✖️ انصراف")]]),
        )

    @staticmethod
    def pretty_card(number):
        digits = "".join(character for character in str(number) if character.isdigit())
        if len(digits) == 16:
            # A standalone ASCII-only message is both LTR-safe and copies without
            # invisible bidi control characters that banking apps may reject.
            return digits
        return str(number)

    @staticmethod
    def _format_order_line(row, labels):
        amount = int(row["payable_amount"] or 0)
        line = (
            f"#{row['id']} | {labels.get(row['status'], row['status'])} | "
            f"{amount:,} تومان"
        )
        wallet_paid = int(row.get("wallet_paid") or 0)
        if row["status"] == "pending" and wallet_paid > 0:
            line += f" (پرداخت‌شده از کیف: {wallet_paid:,})"
        return line

    async def orders(self, event, user):
        rows = await self.db.pool.fetch(
            """SELECT id,status,total_amount,discount_amount,payable_amount,
                      wallet_paid,inventory_reserved,created_at
               FROM orders
               WHERE user_id=$1 ORDER BY id DESC LIMIT 10""",
            user["id"],
        )
        labels = {
            "pending": "در انتظار پرداخت",
            "paid": "پرداخت‌شده",
            "processing": "در حال انجام",
            "completed": "تکمیل‌شده",
            "delivered": "تکمیل‌شده",
            "cancelled": "لغوشده",
            "expired": "منقضی",
            "delivery_failed": "نیازمند پیگیری",
        }
        text = "📦 سفارش‌های اخیر:\n" + (
            "\n".join(
                self._format_order_line(row, labels)
                for row in rows
            )
            if rows
            else "هنوز سفارشی نداری."
        )
        buttons = []
        for row in rows:
            if (
                row["status"] == "pending"
                and row["payable_amount"] > 0
                and row["inventory_reserved"]
            ):
                buttons.append(
                    [(f"order_pay:{row['id']}", f"💳 پرداخت #{row['id']}")]
                )
        await self.send(
            event["chat_id"],
            text,
            buttons=inline(buttons) if buttons else None,
        )

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
            "شناسه روبیکای کاربر معرف را بفرست (مثلاً u0...):\n"
            "همان شناسه‌ای که در «🆔 شناسه من» نمایش داده می‌شود.",
        )

    async def ask_support(self, event):
        support = await self.db.get_support_contact()
        departments = await self.db.pool.fetch(
            "SELECT id,title FROM departments WHERE active ORDER BY id"
        )
        header = "🎧 پشتیبانی Atomic\n━━━━━━━━━━━━━━━\n"
        handle = support["handle"] or "@omid_1797"
        header += (
            f"آیدی پشتیبانی (پیام مستقیم):\n{handle}\n\n"
            "یا از طریق تیکت داخل ربات پیام بفرست.\n"
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
                header + "\n" + (prompt or "پیام خودت را برای پشتیبانی بنویس:"),
                buttons=inline([[("support_cancel", "✖️ انصراف")]]),
            )
            return
        await self.send(
            event["chat_id"],
            header + "\nدپارتمان پشتیبانی را انتخاب کن:",
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
        support = await self.db.get_support_contact()
        body = text or default
        if support["handle"] and support["handle"] not in body:
            body += f"\n\n🎧 آیدی پشتیبانی:\n{support['handle']}"
        await self.send(
            event["chat_id"],
            body,
        )

    async def handle_state(self, event, user, state, data):
        if state.startswith("admin_") and state != "admin_ticket_reply":
            if not self.db.is_owner(event["sender_id"], self.config.admin_id):
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
        if state in {"cred_method", "cred_confirm"}:
            await self.credential_prompt_current_step(event, state, data)
            return
        if state in {
            "cred_qty",
            "cred_identifier",
            "cred_password",
            "cred_backup",
        }:
            await self.credential_handle_state(event, user, state, data)
            return
        if state == "cred_ticket_message":
            await self.credential_ticket_receive(event, user, data)
            return
        if state == "admin_ticket_reply":
            await self.ticket_reply_receive(event, data)
            return
        if state.startswith("admin_product_add_"):
            if await self.product_add_handle_state(event, state, data):
                return
            if state == "admin_product_add_kind":
                await self.product_add_start(event)
                return
            if state == "admin_product_add_cred_plan":
                await self.product_kind_selected(
                    event, str(data.get("kind") or "gem_credentials")
                )
                return
        if state == "admin_product_edit_price":
            try:
                price = checked_amount(event["text"], label="قیمت")
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            pid = int(data.get("product_id") or 0)
            await self.db.pool.execute(
                "UPDATE products SET price=$1 WHERE id=$2", price, pid
            )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ قیمت محصول #{pid} به‌روز شد.")
            await self.product_open(event, pid)
            return
        if state == "admin_product_edit_stock":
            try:
                stock = int(
                    event["text"]
                    .strip()
                    .translate(
                        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
                    )
                )
            except ValueError:
                await self.send(event["chat_id"], "❌ فقط عدد بفرست.")
                return
            if stock < 0:
                await self.send(event["chat_id"], "موجودی منفی مجاز نیست.")
                return
            pid = int(data.get("product_id") or 0)
            await self.db.pool.execute(
                "UPDATE products SET stock=$1 WHERE id=$2", stock, pid
            )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ موجودی محصول #{pid} به‌روز شد.")
            await self.product_open(event, pid)
            return
        if state == "admin_category_add":
            title = (event.get("text") or "").strip()
            if not title:
                await self.send(event["chat_id"], "عنوان خالی است.")
                return
            await self.db.pool.execute(
                """INSERT INTO categories(title,sort_order)
                   VALUES($1,(SELECT COALESCE(MAX(sort_order),0)+10 FROM categories))
                   ON CONFLICT(title) DO UPDATE SET active=true""",
                title,
            )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ دسته «{title}» اضافه شد.")
            await self.categories_manage_home(event)
            return
        if state == "admin_setting_edit":
            await self.setting_edit_receive(event, data)
            return
        if state == "admin_search_query":
            await self.search_receive(event)
            return
        if state == "admin_channel_add":
            parts = [p.strip() for p in (event.get("text") or "").split("|")]
            if len(parts) < 3:
                await self.send(
                    event["chat_id"],
                    "فرمت: chat_id|عنوان|لینک دعوت",
                )
                return
            chat_id, title, url = parts[0], parts[1], parts[2]
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """INSERT INTO forced_channels(chat_id,title,invite_url,active)
                           VALUES($1,$2,$3,true)
                           ON CONFLICT(chat_id) DO UPDATE
                           SET title=EXCLUDED.title,invite_url=EXCLUDED.invite_url,
                               active=true""",
                        chat_id,
                        title,
                        url,
                    )
                    await conn.execute(
                        """UPDATE join_requests SET status='stale'
                           WHERE status='approved'"""
                    )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ کانال «{title}» اضافه شد.")
            await self.admin(event, "admin_settings")
            return
        if state == "admin_department_add":
            title = (event.get("text") or "").strip()
            if not title:
                await self.send(event["chat_id"], "عنوان خالی است.")
                return
            await self.db.pool.execute(
                """INSERT INTO departments(title,active)
                   VALUES($1,true)
                   ON CONFLICT(title) DO UPDATE SET active=true""",
                title,
            )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ دپارتمان «{title}» اضافه شد.")
            await self.admin(event, "admin_settings")
            return
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
            return
        if state == "gem_confirm":
            await self.send(
                event["chat_id"],
                "از دکمه‌های «تأیید»، «اصلاح آیدی» یا «انصراف» استفاده کن.",
                buttons=inline(
                    [
                        [("gem_confirm", "✅ تأیید و ادامه پرداخت")],
                        [("gem_reedit", "✏️ اصلاح آیدی")],
                        [("gem_cancel", "✖️ انصراف")],
                    ]
                ),
            )
            return
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
                    f"دپارتمان: {data.get('department') or 'عمومی'}\n{text}",
                    inline_keypad=inline(
                        [
                            [(f"ticket_reply:{ticket['id']}", "💬 پاسخ")],
                            [(f"ticket:{ticket['id']}", "🎫 باز کردن")],
                        ]
                    ),
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
                menu=await self.user_menu(event["sender_id"]),
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
            referral_arg = (event.get("text") or "").strip()
            if not referral_arg:
                await self.send(event["chat_id"], "❌ کد معرف خالی است.")
                return
            referrer = await self.db.pool.fetchrow(
                "SELECT id FROM users WHERE rubika_id=$1",
                referral_arg,
            )
            if not referrer and referral_arg.isdigit():
                referrer = await self.db.pool.fetchrow(
                    "SELECT id FROM users WHERE id=$1",
                    int(referral_arg),
                )
            if not referrer or referrer["id"] == user["id"]:
                await self.send(event["chat_id"], "❌ کد معرف معتبر نیست.")
                return
            changed = await self.db.pool.execute(
                """UPDATE users SET referred_by=$1
                   WHERE id=$2 AND referred_by IS NULL""",
                referrer["id"],
                user["id"],
            )
            if not changed.endswith("1"):
                await self.send(
                    event["chat_id"],
                    "کد معرف قبلاً ثبت شده یا کاربر معرف پیدا نشد.",
                )
                return
            await self.db.set_session(event["sender_id"])
            await self.send(
                event["chat_id"],
                "✅ کد معرف ثبت شد.",
                menu=await self.user_menu(event["sender_id"]),
            )
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
                await self.send(
                    event["chat_id"],
                    f"❌ {exc}",
                    menu=await self.user_menu(event["sender_id"]),
                )
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
        elif state == "admin_add_admin":
            if event["sender_id"] != self.config.admin_id:
                await self.db.set_session(event["sender_id"])
                await self.send(
                    event["chat_id"], "⛔️ فقط مالک اصلی می‌تواند مدیر اضافه کند."
                )
                return
            parts = event["text"].strip().split(maxsplit=1)
            if len(parts) < 2:
                await self.send(
                    event["chat_id"],
                    "فرمت درست: `u0...` عنوان\nمثال: `u0xxxx مدیر مالی`",
                )
                return
            rubika_id, title = parts[0].strip(), parts[1].strip()
            if not re.fullmatch(r"u0[A-Za-z0-9]{10,80}", rubika_id):
                await self.send(event["chat_id"], "❌ شناسه روبیکا معتبر نیست (فرمت u0...).")
                return
            await self.db.pool.execute(
                """INSERT INTO admins(rubika_id,title,role,active)
                   VALUES($1,$2,'admin',true)
                   ON CONFLICT(rubika_id) DO UPDATE
                   SET title=$2,role='admin',active=true""",
                rubika_id,
                title,
            )
            await self.db.set_session(event["sender_id"])
            await self.send(
                event["chat_id"],
                f"✅ مدیر {title} با شناسه {rubika_id} اضافه شد.",
                menu=admin_menu(),
            )
        elif state == "admin_add_cred_admin":
            if event["sender_id"] != self.config.admin_id:
                await self.db.set_session(event["sender_id"])
                await self.send(
                    event["chat_id"], "⛔️ فقط مالک اصلی می‌تواند پشتیبان اضافه کند."
                )
                return
            parts = event["text"].strip().split(maxsplit=1)
            if len(parts) < 2:
                await self.send(
                    event["chat_id"],
                    "فرمت درست: `u0...` عنوان\nمثال: `u0xxxx پشتیبانی جم`",
                )
                return
            rubika_id, title = parts[0].strip(), parts[1].strip()
            if not re.fullmatch(r"u0[A-Za-z0-9]{10,80}", rubika_id):
                await self.send(event["chat_id"], "❌ شناسه روبیکا معتبر نیست (فرمت u0...).")
                return
            await self.db.set_session(event["sender_id"])
            await self.owner_add_credential_admin(event, rubika_id, title)
        elif state == "admin_code_add":
            if not await self.db.is_admin(event["sender_id"], self.config.admin_id):
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
            raw = event["text"].strip()
            try:
                kind, code, value, max_uses = raw.split("|", 3)
            except ValueError:
                await self.send(
                    event["chat_id"],
                    "❌ فرمت درست نیست. مثال: `gift|SALE10|100000|5` یا `discount|OFF20|20|10`",
                )
                return
            kind = kind.strip()
            code = code.strip().upper()
            if kind not in {"gift", "discount"}:
                await self.send(event["chat_id"], "❌ نوع کد فقط gift یا discount است.")
                return
            if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
                await self.send(event["chat_id"], "❌ ساختار کد معتبر نیست.")
                return
            try:
                safe_value = int(value)
                safe_max_uses = int(max_uses)
            except ValueError:
                await self.send(event["chat_id"], "❌ مقدار و تعداد باید عدد باشند.")
                return
            if safe_value <= 0 or safe_max_uses <= 0:
                await self.send(event["chat_id"], "❌ مقدار و تعداد استفاده باید مثبت باشند.")
                return
            if kind == "discount" and safe_value > 99:
                await self.send(event["chat_id"], "❌ درصد تخفیف باید بین ۱ تا ۹۹ باشد.")
                return
            if kind == "gift":
                safe_value = checked_amount(safe_value, label="مبلغ کد هدیه")
            try:
                await self.db.pool.execute(
                    """INSERT INTO promo_codes(code_type,code,value,max_uses)
                       VALUES($1,upper($2),$3,$4)""",
                    kind,
                    code,
                    safe_value,
                    safe_max_uses,
                )
            except asyncpg.UniqueViolationError:
                await self.send(event["chat_id"], "❌ این کد قبلاً ثبت شده است.")
                return
            await self.db.set_session(event["sender_id"])
            await self.send(
                event["chat_id"],
                f"✅ کد `{code}` با نوع {kind} و مقدار {safe_value} اضافه شد.",
                menu=admin_menu(),
            )
        elif state == "admin_charge_one":
            if not await self.db.is_admin(event["sender_id"], self.config.admin_id):
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
            raw = event["text"].strip()
            try:
                user_id, amount = raw.split("|", 1)
            except ValueError:
                await self.send(
                    event["chat_id"],
                    "❌ فرمت درست نیست. مثال: `303|50000`",
                )
                return
            try:
                target_id = int(user_id.strip())
                amount = checked_amount(amount, label="شارژ کاربر")
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            exists = await self.db.pool.fetchval(
                "SELECT 1 FROM users WHERE id=$1", target_id
            )
            if not exists:
                await self.send(
                    event["chat_id"],
                    "❌ کاربر پیدا نشد؛ شناسه داخلی را از «👥 کاربران» بگیر.",
                )
                return
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE users SET balance=balance+$1 WHERE id=$2",
                        amount,
                        target_id,
                    )
                    await conn.execute(
                        """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                           VALUES($1,$2,'admin_charge',$3)""",
                        target_id,
                        amount,
                        f"admin:{event['sender_id']}:{target_id}:{os.urandom(8).hex()}",
                    )
            await self.db.set_session(event["sender_id"])
            await self.send(
                event["chat_id"],
                f"✅ مبلغ {amount:,} تومان به کیف پول کاربر {target_id} اضافه شد.",
                menu=admin_menu(),
            )
        elif state == "admin_charge_all":
            if not await self.db.is_admin(event["sender_id"], self.config.admin_id):
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
            raw = event["text"].strip().replace(",", "").replace(" ", "")
            try:
                amount = checked_amount(raw, label="شارژ همگانی")
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return
            batch = f"admin-all:{event['sender_id']}:{os.urandom(8).hex()}"
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
            await self.db.set_session(event["sender_id"])
            await self.send(
                event["chat_id"],
                f"✅ مبلغ {amount:,} تومان به کیف پول همه کاربران فعال اضافه شد.",
                menu=admin_menu(),
            )
        elif state == "admin_cred_setting":
            if not await self.db.is_admin(event["sender_id"], self.config.admin_id):
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "⛔️ دسترسی مدیر ندارید.")
                return
            key = str(data.get("key") or "")
            raw = (
                event["text"]
                .strip()
                .replace("٪", "")
                .replace("%", "")
                .replace(",", "")
            )
            if key.endswith("_profit_percent"):
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    await self.send(event["chat_id"], "❌ فقط عدد بفرست (مثلاً 40).")
                    return
                if not 1 <= value <= 200:
                    await self.send(event["chat_id"], "❌ درصد باید بین ۱ تا ۲۰۰ باشد.")
                    return
                await self.db.set_setting(key, str(value))
                labels = {
                    "gem_profit_percent": "جم با آیدی",
                    "credential_weekly_profit_percent": "هفتگی",
                    "credential_monthly_profit_percent": "ماهانه",
                }
                msg = f"✅ سود {labels.get(key, key)} = {value}٪"
            elif key.endswith("_cost_usd"):
                try:
                    from decimal import Decimal, InvalidOperation

                    value = Decimal(raw)
                except (InvalidOperation, TypeError, ValueError):
                    await self.send(event["chat_id"], "❌ عدد اعشاری معتبر بفرست (مثلاً 1.328).")
                    return
                if value < Decimal("0.01") or value > Decimal("1000"):
                    await self.send(event["chat_id"], "❌ هزینه باید بین 0.01 تا 1000 باشد.")
                    return
                await self.db.set_setting(key, str(value))
                msg = f"✅ {key} = {value} USD"
            else:
                await self.db.set_session(event["sender_id"])
                await self.send(event["chat_id"], "❌ تنظیم نامعتبر.")
                return
            await self.db.set_session(event["sender_id"])
            sync_note = ""
            if key.endswith("_profit_percent"):
                if key == "gem_profit_percent":
                    try:
                        result = await self.run_gem_price_sync_router()
                        if result.get("ok"):
                            sync_note = (
                                f"\n🔄 قیمت جم با آیدی: {result.get('gem_updated', 0)} · "
                                f"جم با اطلاعات: {result.get('cred_updated', 0)}"
                            )
                        else:
                            sync_note = f"\n⚠️ sync قیمت: {result.get('error')}"
                    except Exception:
                        log.exception("Price sync after gem profit change failed")
                        sync_note = "\n⚠️ sync قیمت انجام نشد."
                else:
                    sync = await self.sync_credential_prices_now()
                    if sync and sync.get("ok"):
                        sync_note = (
                            f"\n🔄 قیمت فروش ({sync['updated']} محصول) با نرخ "
                            f"{sync['rate']:,} تومان اعمال شد."
                        )
                    elif sync and sync.get("error"):
                        sync_note = f"\n⚠️ قیمت فوری اعمال نشد: {sync['error']}"
            elif key.endswith("_cost_usd"):
                sync = await self.sync_credential_prices_now()
                if sync and sync.get("ok"):
                    sync_note = (
                        f"\n🔄 قیمت فروش ({sync['updated']} محصول) اعمال شد."
                    )
                elif sync and sync.get("error"):
                    sync_note = f"\n⚠️ قیمت فوری اعمال نشد: {sync['error']}"
            await self.send(event["chat_id"], msg + sync_note)
            if key.endswith("_profit_percent"):
                await self.admin_pricing_home(event, with_menu=False)
            else:
                await self.admin_pricing_home(event, with_menu=False)
        elif state == "admin_broadcast":
            if not await self.db.is_admin(
                event["sender_id"], self.config.admin_id
            ):
                await self.db.set_session(event["sender_id"])
                await self.send(
                    event["chat_id"], "⛔️ دسترسی مدیر ندارید."
                )
                return
            text = event["text"].strip()
            if not text:
                await self.send(
                    event["chat_id"],
                    "پیام خالی است. متن پیام همگانی را بنویس:",
                )
                return
            await self.db.set_session(event["sender_id"])
            users = await self.db.pool.fetch(
                "SELECT chat_id FROM users WHERE NOT blocked"
            )
            sent = 0
            for row in users:
                try:
                    await self.api.send_message(row["chat_id"], text[:4000])
                    sent += 1
                except Exception:
                    log.exception("broadcast failed")
            await self.send(
                event["chat_id"],
                f"📣 پیام همگانی ارسال شد.\n"
                f"کاربران فعال: {len(users):,}\nارسال شد: {sent:,}",
                menu=admin_menu(),
            )

    async def admin_users_list(self, event, *, filter_key: str = "all"):
        chat = event["chat_id"]
        clauses = {
            "all": "TRUE",
            "balance": "balance>0",
            "referral": "EXISTS(SELECT 1 FROM users child WHERE child.referred_by=users.id)",
            "card": "card_verified",
        }
        titles = {
            "all": "👥 همه کاربران",
            "balance": "💰 کاربران با موجودی",
            "referral": "👥 کاربران دارای زیرمجموعه",
            "card": "💳 کاربران با کارت تأییدشده",
        }
        where = clauses.get(filter_key, "TRUE")
        rows = await self.db.pool.fetch(
            f"""SELECT id,display_name,rubika_id,balance,blocked,card_number
                FROM users WHERE {where} ORDER BY id DESC LIMIT 20"""
        )
        if not rows:
            await self.send(chat, "کاربری در این فیلتر پیدا نشد.")
            return

        def _user_label(r):
            name = (r["display_name"] or "").strip()
            line = (
                f"{r['id']} | {name or '—'} | {r['rubika_id']} | {r['balance']:,} ت"
                + (" 🚫" if r["blocked"] else "")
            )
            if filter_key == "card" and r.get("card_number"):
                line += f" | ****{r['card_number'][-4:]}"
            return line

        total = await self.db.pool.fetchval(
            f"SELECT count(*) FROM users WHERE {where}"
        )
        await self.send(
            chat,
            f"{titles.get(filter_key, '👥 کاربران')}\n"
            f"نمایش {len(rows)} از {total:,}\n\n"
            + "\n".join(_user_label(r) for r in rows)
            + "\n\nبرای بن/آنبن روی دکمه‌ها بزن:",
            buttons=inline(
                [
                    [
                        ("admin_users", "👥 همه"),
                        ("admin_users_balance", "💰 موجودی"),
                    ],
                    [
                        ("admin_users_referral", "👥 زیرمجموعه"),
                        ("admin_users_card", "💳 کارت"),
                    ],
                    *[
                        [
                            (
                                f"admin_block:{r['id']}",
                                ("🚫 بن " if not r["blocked"] else "✅ آنبن ")
                                + str(r["id"]),
                            )
                        ]
                        for r in rows
                    ],
                    [("admin_search", "🔎 جستجوی کاربر")],
                ]
            ),
        )

    async def admin(self, event, action):
        chat = event["chat_id"]
        if not self.db.is_owner(event["sender_id"], self.config.admin_id):
            await self.send(chat, "⛔️ دسترسی مدیر ندارید.")
            return
        state, _ = await self.db.session(event["sender_id"])
        if (
            state
            and state.startswith("admin_")
            and action not in {"admin_cancel_broadcast", "flow_cancel", "/admin"}
            and not action.startswith(
                ("admin_product_kind:", "admin_cred_plan:", "admin_product_")
            )
        ):
            await self.db.set_session(event["sender_id"])
        if action == "/admin":
            await self.open_admin_panel(event)
            return
        elif action == "admin_stats":
            s = await self.db.stats()
            await self.send(
                chat,
                f"📊 آمار کلی\nکاربران: {s['users']:,}\nخریداران: {s['buyers']:,}\n"
                f"موجودی کل: {s['balances']:,} تومان\nفروش‌ها: {s['sales']:,}\n"
                f"جمع فروش: {s['revenue']:,} تومان\n"
                f"مغایرت دفتر کیف پول: {s['wallet_mismatches']:,}",
            )
        elif action == "admin_ops":
            # مرکز عملیات و هشدارها — مشابه admx_ops تلگرام
            ops = await self.db.pool.fetchrow(
                """SELECT
                  (SELECT count(*) FROM orders WHERE status IN ('pending','paid','processing')) open_orders,
                  (SELECT count(*) FROM orders WHERE status='delivery_failed') failed,
                  (SELECT count(*) FROM receipts WHERE status='pending') pending_receipts,
                  (SELECT count(*) FROM tickets WHERE status='open') open_tickets,
                  (SELECT count(*) FROM payments WHERE status='pending' AND expires_at>now()) active_payments,
                  (SELECT count(*) FROM orders WHERE status='pending' AND created_at>now()-interval '1 day') pending_24h,
                  (SELECT count(*) FROM orders WHERE paid_at IS NOT NULL AND paid_at>now()-interval '1 day') sales_24h,
                  (SELECT count(*) FROM credential_orders c
                     JOIN orders o ON o.id=c.order_id
                    WHERE c.cred_status IN ('ready','needs_info')
                      AND o.status IN ('paid','processing')) ready_credentials,
                  (SELECT count(*) FROM tickets
                    WHERE status='open' AND COALESCE(category,'bot')='credential') cred_tickets
                """
            )
            alerts = (
                int(ops["pending_receipts"]) + int(ops["failed"])
                + int(ops["open_tickets"]) + int(ops["open_orders"])
                + int(ops["ready_credentials"])
            )
            await self.send(
                chat,
                f"🚨 مرکز عملیات و هشدارها\n"
                f"━━━━━━━━━━━━━━━\n"
                f"سفارش‌های باز: {ops['open_orders']:,}\n"
                f"تحویل ناموفق: {ops['failed']:,}\n"
                f"رسید در انتظار: {ops['pending_receipts']:,}\n"
                f"تیکت باز: {ops['open_tickets']:,}\n"
                f"🔐 جم با اطلاعات آماده: {ops['ready_credentials']:,}\n"
                f"🔐 تیکت جم با اطلاعات: {ops['cred_tickets']:,}\n"
                f"پرداخت فعال: {ops['active_payments']:,}\n"
                f"فروش ۲۴ ساعت اخیر: {ops['sales_24h']:,}\n"
                f"سفارش‌های pending دیروز: {ops['pending_24h']:,}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{'🚨' if alerts else '✅'} هشدارهای قابل اقدام: {alerts:,}",
                buttons=inline(
                    [
                        [
                            (
                                "admin_receipts",
                                f"🧾 رسیدها ({ops['pending_receipts']:,})",
                            )
                        ],
                        [
                            (
                                "admin_orders",
                                f"📦 سفارش‌های باز ({ops['open_orders']:,})",
                            )
                        ],
                        [
                            (
                                "admin_support",
                                f"🎧 تیکت‌ها ({ops['open_tickets']:,})",
                            )
                        ],
                        [
                            (
                                "cred_admin_home",
                                f"🔐 جم با اطلاعات ({ops['ready_credentials']:,})",
                            )
                        ],
                    ]
                ),
            )
        elif action == "admin_shop":
            ready_cred = await self.db.count_ready_credential_orders()
            await self.send(
                chat,
                "🛍 مدیریت فروشگاه\nیک بخش را انتخاب کن:",
                buttons=inline(
                    [
                        [("admin_products", "📦 محصولات")],
                        [("admin_categories", "🗂 دسته‌بندی‌ها")],
                        [("admin_codes", "🎁 کدها")],
                        [("admin_finance", "💳 امور مالی")],
                        [
                            (
                                "cred_admin_home",
                                f"🔐 جم با اطلاعات ({ready_cred})",
                            )
                        ],
                        [("admin_pricing_home", "📈 قیمت و سود")],
                        [("home", "🏠 بازگشت")],
                    ]
                ),
            )
        elif action == "admin_orders":
            open_rows = await self.db.pool.fetch(
                """SELECT o.id,o.status,o.total_amount-o.discount_amount total,
                          u.rubika_id
                   FROM orders o JOIN users u ON u.id=o.user_id
                   WHERE o.status IN ('pending','paid','processing')
                   ORDER BY o.id DESC LIMIT 20"""
            )
            failed_rows = await self.db.pool.fetch(
                """SELECT o.id,o.status,o.total_amount-o.discount_amount total,
                          u.rubika_id
                   FROM orders o JOIN users u ON u.id=o.user_id
                   WHERE o.status='delivery_failed'
                   ORDER BY o.id DESC LIMIT 20"""
            )
            text = "📦 سفارش‌های باز:\n"
            buttons = [[("admin_search", "🔎 جستجوی سفارش/کاربر")]]
            if open_rows:
                text += "\n".join(
                    f"#{r['id']} | {r['status']} | {r['total']:,} ت | {r['rubika_id']}"
                    for r in open_rows
                )
                for r in open_rows:
                    if r["status"] in ("paid", "processing"):
                        buttons.append(
                            [
                                (
                                    f"order_complete:{r['id']}",
                                    f"✅ تکمیل #{r['id']}",
                                )
                            ]
                        )
            else:
                text += "موردی نیست."
            text += "\n\n⚠️ تحویل ناموفق:\n"
            if failed_rows:
                text += "\n".join(
                    f"#{r['id']} | {r['total']:,} ت | {r['rubika_id']}"
                    for r in failed_rows
                )
            else:
                text += "موردی نیست."
            buttons.append([("admin_ops", "🔙 مرکز عملیات")])
            await self.send(chat, text, buttons=inline(buttons))
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
            await self.admin_users_list(event, filter_key="all")
        elif action == "admin_users_balance":
            await self.admin_users_list(event, filter_key="balance")
        elif action == "admin_users_referral":
            await self.admin_users_list(event, filter_key="referral")
        elif action == "admin_users_card":
            await self.admin_users_list(event, filter_key="card")
        elif action == "admin_support":
            rows = await self.db.pool.fetch(
                """SELECT id,user_id,department FROM tickets
                   WHERE status='open'
                     AND COALESCE(category,'bot')<>'credential'
                   ORDER BY updated_at DESC LIMIT 30"""
            )
            buttons = []
            for row in rows[:12]:
                buttons.append(
                    [
                        (f"ticket:{row['id']}", f"🎫 #{row['id']}"),
                        (f"ticket_reply:{row['id']}", "💬 پاسخ"),
                    ]
                )
            buttons.append([("cred_admin_tickets", "🔐 تیکت جم با اطلاعات")])
            await self.send(
                chat,
                "🎧 تیکت‌های باز\n"
                + (
                    "\n".join(
                        f"#{r['id']} کاربر {r['user_id']} | {r['department']}"
                        for r in rows
                    )
                    if rows
                    else "موردی نیست."
                ),
                buttons=inline(buttons) if buttons else None,
            )
        elif action == "admin_admins":
            if event["sender_id"] != self.config.admin_id:
                await self.send(chat, "⛔️ مدیریت مدیران فقط در اختیار مالک اصلی ربات است.")
                return
            rows = await self.db.pool.fetch(
                """SELECT rubika_id,title,role,active,created_at
                   FROM admins ORDER BY active DESC,created_at"""
            )
            active_rows = [row for row in rows if row["active"]]
            lines = [
                "👮 مدیریت مدیران",
                f"مالک اصلی: {self.config.admin_id}",
                "",
                "مدیران فعال:",
            ]
            lines.extend(
                f"• {row['rubika_id']} | {row['title'] or 'بدون عنوان'} | "
                f"نقش: {'🔐 جم با اطلاعات' if row['role']=='credential' else '🛠 ادمین'}"
                for row in active_rows
                if row["rubika_id"] != self.config.admin_id
            )
            if len(lines) == 4:
                lines.append("• مدیر دیگری فعال نیست.")
            # دکمه‌های تعاملی برای هر مدیر + افزودن/پاک‌سازی
            buttons = []
            for row in active_rows:
                if row["rubika_id"] == self.config.admin_id:
                    continue
                buttons.append(
                    [
                        (
                            f"admin_remove:{row['rubika_id']}",
                            f"❌ حذف {row['title'] or row['rubika_id'][:8]}",
                        )
                    ]
                )
            buttons.append([("admin_add_admin", "➕ افزودن مدیر با شناسه")])
            buttons.append(
                [("admin_add_cred_admin", "🔐 افزودن/تعیین پشتیبان جم با اطلاعات")]
            )
            buttons.append([("admin_clear_all", "🗑 حذف همه مدیران فرعی")])
            buttons.append([("home", "🏠 بازگشت")])
            await self.send(chat, "\n".join(lines), buttons=inline(buttons))
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
                    cost = supplier_cost_toman(
                        product["supplier_cost_usd"], rate["rate"]
                    )
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
                )
                + "\n\nبروزرسانی خودکار قیمت جم و جم با اطلاعات هر ۲۴ ساعت انجام می‌شود "
                "(جم با اطلاعات: سود ۴۰٪ هفتگی/ماهانه).",
                buttons=inline(
                    [
                        [
                            (
                                "admin_pricing_sync",
                                "🔄 همگام‌سازی قیمت‌ها",
                            )
                        ],
                        [("admin_pricing_home", "📈 قیمت و سود")],
                        [("admin_finance", "💳 تنظیمات مالی")],
                    ]
                ),
            )
        elif action == "admin_pricing_home":
            await self.admin_pricing_home(event, with_menu=True)
        elif action in {"admin_sync_prices", "admin_profit_sync", "admin_pricing_sync"}:
            await self.admin_pricing_sync(event)
        elif action == "admin_set_profit":
            await self.admin_pricing_home(event, with_menu=True)
        elif action.startswith("admin_cred_plan:"):
            plan = action.removeprefix("admin_cred_plan:")
            if plan not in {"weekly", "monthly"}:
                await self.send(chat, "❌ دوره نامعتبر است.")
                return
            await self.db.set_session(
                event["sender_id"],
                "admin_product_add_title",
                {"kind": "gem_credentials", "cred_plan": plan},
            )
            await self.send(
                chat,
                f"عنوان محصول {'هفتگی' if plan == 'weekly' else 'ماهانه'} را بفرست:",
                buttons=inline([[("flow_cancel", "✖️ انصراف")]]),
            )
        elif action == "admin_products":
            await self.products_manage_home(event)
        elif action == "admin_product_add":
            await self.product_add_start(event)
        elif action.startswith("admin_product_kind:"):
            await self.product_kind_selected(
                event, action.removeprefix("admin_product_kind:")
            )
        elif action.startswith("admin_product_open:"):
            pid = action.removeprefix("admin_product_open:")
            if pid.isdigit():
                await self.product_open(event, int(pid))
        elif action.startswith("admin_product_toggle:"):
            pid = action.removeprefix("admin_product_toggle:")
            if pid.isdigit():
                await self.db.pool.execute(
                    "UPDATE products SET active=NOT active WHERE id=$1", int(pid)
                )
                await self.product_open(event, int(pid))
        elif action.startswith("admin_product_edit_price:"):
            pid = action.removeprefix("admin_product_edit_price:")
            if pid.isdigit():
                await self.db.set_session(
                    event["sender_id"],
                    "admin_product_edit_price",
                    {"product_id": int(pid)},
                )
                await self.send(
                    chat,
                    f"💰 قیمت جدید محصول #{pid} را به تومان بفرست:",
                    buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
                )
        elif action.startswith("admin_product_edit_stock:"):
            pid = action.removeprefix("admin_product_edit_stock:")
            if pid.isdigit():
                await self.db.set_session(
                    event["sender_id"],
                    "admin_product_edit_stock",
                    {"product_id": int(pid)},
                )
                await self.send(
                    chat,
                    f"📦 موجودی جدید محصول #{pid} را بفرست:",
                    buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
                )
        elif action.startswith("admin_product_move:"):
            parts = action.removeprefix("admin_product_move:").split(":")
            if len(parts) == 2 and parts[0].isdigit():
                await self.db.move_catalogue_item(
                    "products", int(parts[0]), parts[1]
                )
                await self.product_open(event, int(parts[0]))
        elif action.startswith("admin_product_delete:"):
            pid = action.removeprefix("admin_product_delete:")
            if pid.isdigit():
                await self.db.pool.execute(
                    "UPDATE products SET active=false WHERE id=$1", int(pid)
                )
                await self.send(chat, f"✅ محصول #{pid} غیرفعال شد.")
                await self.products_manage_home(event)
        elif action == "admin_categories":
            await self.categories_manage_home(event)
        elif action == "admin_category_add":
            await self.category_add_start(event)
        elif action.startswith("admin_category_toggle:"):
            cid = action.removeprefix("admin_category_toggle:")
            if cid.isdigit():
                await self.db.pool.execute(
                    "UPDATE categories SET active=NOT active WHERE id=$1", int(cid)
                )
                await self.categories_manage_home(event)
        elif action.startswith("admin_category_del:"):
            cid = action.removeprefix("admin_category_del:")
            if cid.isdigit():
                await self.db.pool.execute(
                    "UPDATE categories SET active=false WHERE id=$1", int(cid)
                )
                await self.categories_manage_home(event)
        elif action.startswith("admin_category_move:"):
            parts = action.removeprefix("admin_category_move:").split(":")
            if len(parts) == 2 and parts[0].isdigit():
                await self.db.move_catalogue_item(
                    "categories", int(parts[0]), parts[1]
                )
                await self.categories_manage_home(event)
        elif action == "admin_finance":
            values = {
                key: await self.db.setting(key, "")
                for key in (
                    "payments_enabled",
                    "zarinpal_enabled",
                    "zarinpal_merchant_id",
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
            merchant = values["zarinpal_merchant_id"] or os.getenv(
                "ZARINPAL_MERCHANT_ID", ""
            ).strip()
            merchant_text = (
                f"{merchant[:6]}…{merchant[-4:]}"
                if len(merchant) > 12
                else merchant or "تنظیم‌نشده"
            )
            masked_card = (
                f"**** **** **** {values['card_number'][-4:]}"
                if len(values["card_number"]) >= 4
                else "تنظیم‌نشده"
            )
            await self.send(
                chat,
                "💳 تنظیمات مالی\n"
                f"پرداخت‌ها: {'فعال ✅' if values['payments_enabled']=='1' else 'غیرفعال ❌'}\n"
                f"زرین‌پال: {'فعال ✅' if values['zarinpal_enabled']=='1' else 'غیرفعال ❌'}\n"
                f"کارت‌به‌کارت: {'فعال ✅' if values['card_enabled']=='1' else 'غیرفعال ❌'}\n"
                f"مرچنت: {merchant_text}\n"
                f"کارت: {masked_card}\n"
                f"دارنده: {values['card_holder'] or '—'}\n"
                f"بانک: {values['card_bank'] or '—'}\n\n"
                "همه تنظیمات با دکمه قابل تغییرند:",
                buttons=inline(
                    [
                        [
                            (
                                "admin_toggle:payments_enabled",
                                (
                                    "🔴 پرداخت غیرفعال"
                                    if values["payments_enabled"] == "1"
                                    else "🟢 پرداخت فعال"
                                ),
                            )
                        ],
                        [
                            (
                                "admin_toggle:zarinpal_enabled",
                                (
                                    "🔴 زرین‌پال غیرفعال"
                                    if values["zarinpal_enabled"] == "1"
                                    else "🟢 زرین‌پال فعال"
                                ),
                            )
                        ],
                        [
                            (
                                "admin_toggle:card_enabled",
                                (
                                    "🔴 کارت غیرفعال"
                                    if values["card_enabled"] == "1"
                                    else "🟢 کارت فعال"
                                ),
                            )
                        ],
                        [("admin_edit:zarinpal_merchant_id", "✏️ مرچنت زرین‌پال")],
                        [("admin_edit:card_number", "✏️ شماره کارت")],
                        [("admin_edit:card_holder", "✏️ نام دارنده")],
                        [("admin_edit:card_bank", "✏️ بانک")],
                        [("admin_edit:usd_toman_rate", "✏️ نرخ دلار")],
                        [("admin_pricing_home", "📈 قیمت و سود")],
                        [("admin_pricing_sync", "🔄 sync جم با اطلاعات")],
                    ]
                ),
            )
        elif action.startswith("admin_edit:"):
            await self.finance_edit_start(event, action.removeprefix("admin_edit:"))
        elif action.startswith("admin_toggle:"):
            key = action.removeprefix("admin_toggle:")
            allowed_toggle = {
                "payments_enabled",
                "zarinpal_enabled",
                "card_enabled",
                "sales_enabled",
            }
            if key not in allowed_toggle:
                await self.send(chat, "❌ کلید تنظیم نامعتبر است.")
                return
            current = await self.db.setting(key, "1")
            new_value = "0" if current == "1" else "1"
            await self.db.set_setting(key, new_value)
            await self.db.audit(event["sender_id"], "setting", details=f"{key}={new_value}")
            await self.send(chat, f"✅ تنظیم {key} به {new_value} تغییر کرد.")
            await self.admin(
                event, "admin_settings" if key == "sales_enabled" else "admin_finance"
            )
        elif action == "admin_codes":
            rows = await self.db.pool.fetch(
                """SELECT id,code,code_type,value,used_count,max_uses,active
                   FROM promo_codes ORDER BY id DESC LIMIT 50"""
            )
            lines = [
                "🎁 کدهای تخفیف/هدیه\n",
            ]
            if rows:
                lines.append(
                    "\n".join(
                        f"#{row['id']} `{row['code']}` | {row['code_type']} "
                        f"{row['value']} | {row['used_count']}/{row['max_uses']} | "
                        f"{'فعال' if row['active'] else 'حذف‌شده'}"
                        for row in rows
                    )
                )
            else:
                lines.append("کدی نیست.")
            lines.append("\nبرای افزودن، دکمه زیر را بزن:")
            buttons = []
            for row in rows[:20]:
                if row["active"]:
                    buttons.append(
                        [
                            (
                                f"admin_code_del:{row['id']}",
                                f"🗑 حذف {row['code']}",
                            )
                        ]
                    )
            buttons.append([("admin_code_add", "➕ افزودن کد")])
            buttons.append([("home", "🏠 بازگشت")])
            await self.send(chat, "\n".join(lines), buttons=inline(buttons))
        elif action == "admin_settings":
            sales = await self.db.setting("sales_enabled", "1")
            departments = await self.db.pool.fetch(
                "SELECT id,title FROM departments WHERE active ORDER BY id"
            )
            channels = await self.db.pool.fetch(
                "SELECT id,title,chat_id FROM forced_channels WHERE active ORDER BY id"
            )
            await self.send(
                chat,
                "⚙️ تنظیمات فروشگاه\n"
                f"فروش: {'فعال ✅' if sales == '1' else 'غیرفعال ❌'}\n\n"
                "دپارتمان‌ها:\n"
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
                ),
                buttons=inline(
                    [
                        [
                            (
                                "admin_toggle:sales_enabled",
                                ("🔴 فروش غیرفعال" if sales == "1"
                                 else "🟢 فروش فعال"),
                            )
                        ],
                        [("admin_settings_text", "✏️ ویرایش متن پیام‌ها")],
                        [("admin_support_ids", "🎧 آیدی‌های پشتیبانی")],
                        [("admin_channel_add", "📢 افزودن کانال اجباری")],
                        [("admin_department_add", "🏷 افزودن دپارتمان")],
                        *[
                            [(f"admin_channel_del:{row['id']}", f"🗑 کانال #{row['id']}")]
                            for row in channels[:10]
                        ],
                        *[
                            [(f"admin_department_del:{row['id']}", f"🗑 دپارتمان #{row['id']}")]
                            for row in departments[:10]
                        ],
                        [("admin_admins", "👮 مدیریت مدیران")],
                        [("admin_search", "🔎 جستجو")],
                    ]
                ),
            )
        elif action == "admin_settings_text":
            welcome = await self.db.setting("welcome_text", "")
            help_text = await self.db.setting("help_text", "")
            support_prompt = await self.db.setting("support_prompt", "")
            await self.send(
                chat,
                "✏️ ویرایش متن پیام‌ها\n"
                f"خوش‌آمد: {(welcome or '—')[:80]}\n"
                f"راهنما: {(help_text or '—')[:80]}\n"
                f"پشتیبانی: {(support_prompt or '—')[:80]}\n\n"
                "روی دکمه موردنظر بزن و متن جدید را بفرست:",
                buttons=inline(
                    [
                        [("admin_edit:welcome_text", "✏️ متن خوش‌آمد")],
                        [("admin_edit:help_text", "✏️ متن راهنما")],
                        [("admin_edit:support_prompt", "✏️ پیام پشتیبانی")],
                        [("admin_settings", "🔙 بازگشت")],
                    ]
                ),
            )
        elif action == "admin_support_ids":
            general = await self.db.get_support_contact()
            cred = await self.db.get_credential_support_contact()
            await self.send(
                chat,
                "🎧 آیدی‌های پشتیبانی\n"
                "━━━━━━━━━━━━━━━\n"
                "این آیدی‌ها را خودت انتخاب کن تا مشتری بتواند پیام بدهد.\n\n"
                f"💎 پشتیبانی عمومی / جم با آیدی:\n"
                f"{general['handle'] or 'تنظیم نشده'}\n\n"
                f"🔐 پشتیبانی جم با اطلاعات:\n"
                f"{cred['handle'] or 'تنظیم نشده'}\n\n"
                "⚠️ آیدی جم با اطلاعات فقط بعد از پرداخت به مشتری نشان داده می‌شود.",
                buttons=inline(
                    [
                        [("admin_edit:support_id", "✏️ آیدی پشتیبانی عمومی")],
                        [
                            (
                                "admin_edit:credential_support_id",
                                "✏️ آیدی پشتیبانی جم با اطلاعات",
                            )
                        ],
                        [("admin_settings", "🔙 بازگشت")],
                    ]
                ),
            )
        elif action == "admin_search":
            await self.search_start(event)
        elif action == "admin_channel_add":
            await self.channel_add_start(event)
        elif action == "admin_department_add":
            await self.department_add_start(event)
        elif action.startswith("admin_channel_del:"):
            cid = action.removeprefix("admin_channel_del:")
            if cid.isdigit():
                await self.db.pool.execute(
                    "UPDATE forced_channels SET active=false WHERE id=$1", int(cid)
                )
                await self.admin(event, "admin_settings")
        elif action.startswith("admin_department_del:"):
            did = action.removeprefix("admin_department_del:")
            if did.isdigit():
                await self.db.pool.execute(
                    "UPDATE departments SET active=false WHERE id=$1", int(did)
                )
                await self.admin(event, "admin_settings")
        elif action == "admin_broadcast":
            await self.db.set_session(
                event["sender_id"],
                "admin_broadcast",
                {},
            )
            await self.send(
                chat,
                "📣 *پیام همگانی*\n"
                "پیام موردنظر را بنویس. برای همه کاربران فعال ارسال می‌شود.\n"
                "برای انصراف، دکمه زیر را بزن:",
                buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
            )
        elif action == "admin_charge":
            await self.send(
                chat,
                "💰 شارژ کیف پول کاربر\n"
                "یک گزینه را انتخاب کن:\n\n"
                "• **شارژ یک کاربر خاص** — شناسه داخلی و مبلغ را بفرست\n"
                "• **شارژ همگانی** — به همه کاربران فعال مبلغی اضافه کن",
                buttons=inline(
                    [
                        [
                            (
                                "admin_charge_one",
                                "👤 شارژ یک کاربر",
                            ),
                            ("admin_charge_all_btn", "📤 شارژ همگانی"),
                        ]
                    ]
                ),
            )
        elif action == "admin_charge_one":
            await self.db.set_session(
                event["sender_id"],
                "admin_charge_one",
                {},
            )
            await self.send(
                chat,
                "💰 شارژ یک کاربر\n"
                "فرمت: `شناسه داخلی|مبلغ`\n"
                "مثال: `303|50000`\n"
                "برای دیدن شناسه‌ها: «👥 کاربران» را ببین.",
                buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
            )
        elif action == "admin_charge_all_btn":
            await self.db.set_session(
                event["sender_id"],
                "admin_charge_all",
                {},
            )
            await self.send(
                chat,
                "📤 شارژ همگانی\n"
                "مبلغی را که به همه کاربران فعال اضافه شود بفرست:\n"
                "مثال: `10000`",
                buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
            )
        elif action == "admin_code_add":
            await self.db.set_session(
                event["sender_id"],
                "admin_code_add",
                {},
            )
            await self.send(
                chat,
                "🎁 افزودن کد تخفیف/هدیه\n"
                "فرمت: `نوع|کد|مقدار|تعداد استفاده`\n"
                "- نوع: `gift` (مبلغ هدیه) یا `discount` (درصد تخفیف)\n"
                "- مثال هدیه: `gift|SALE10|100000|5`\n"
                "- مثال تخفیف: `discount|OFF20|20|10`\n\n"
                "برای انصراف دکمه زیر را بزن:",
                buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
            )
        elif action.startswith("admin_code_del:"):
            code_arg = action.removeprefix("admin_code_del:")
            if not code_arg.isdigit():
                await self.send(chat, "❌ شناسه کد نامعتبر است.")
                return
            await self.db.pool.execute(
                "UPDATE promo_codes SET active=false WHERE id=$1", int(code_arg)
            )
            await self.db.audit(event["sender_id"], "code_delete", details=code_arg)
            await self.send(chat, f"🗑 کد {code_arg} حذف شد.")
            await self.admin(event, "admin_codes")
        elif action == "admin_cancel_broadcast":
            await self.db.set_session(event["sender_id"])
            await self.send(chat, "✖️ عملیات لغو شد.", menu=admin_menu())
        elif action.startswith("admin_block:"):
            user_arg = action.removeprefix("admin_block:")
            if not user_arg.isdigit():
                await self.send(chat, "❌ شناسه کاربر نامعتبر است.")
                return
            user_id = int(user_arg)
            current = await self.db.pool.fetchrow(
                "SELECT blocked FROM users WHERE id=$1", user_id
            )
            if not current:
                await self.send(chat, "کاربر پیدا نشد.")
                return
            new_state = not current["blocked"]
            await self.db.pool.execute(
                "UPDATE users SET blocked=$1 WHERE id=$2", new_state, user_id
            )
            await self.db.audit(
                event["sender_id"],
                "block" if new_state else "unblock",
                details=str(user_id),
            )
            await self.send(
                chat,
                f"✅ کاربر {user_id} {'بن شد 🚫' if new_state else 'آنبن شد ✅'}",
            )
            # بازگشت به لیست کاربران
            await self.admin(event, "admin_users")
        elif action == "admin_add_admin":
            if event["sender_id"] != self.config.admin_id:
                await self.send(chat, "⛔️ فقط مالک اصلی می‌تواند مدیر اضافه کند.")
                return
            await self.db.set_session(
                event["sender_id"],
                "admin_add_admin",
                {},
            )
            await self.send(
                chat,
                "➕ شناسه روبیکای مدیر جدید را بفرست (فرمت `u0...`):\n"
                "سپس یک عنوان برای او بنویس.",
                buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
            )
        elif action == "admin_add_cred_admin":
            if event["sender_id"] != self.config.admin_id:
                await self.send(chat, "⛔️ فقط مالک اصلی می‌تواند پشتیبان اضافه کند.")
                return
            await self.db.set_session(
                event["sender_id"],
                "admin_add_cred_admin",
                {},
            )
            await self.send(
                chat,
                "🔐 شناسه روبیکای پشتیبان جم با اطلاعات را بفرست (فرمت `u0...`):\n"
                "سپس یک عنوان بنویس.\n"
                "مثال: `u0xxxx پشتیبانی جم`",
                buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
            )
        elif action.startswith("admin_remove:"):
            if event["sender_id"] != self.config.admin_id:
                await self.send(chat, "⛔️ فقط مالک اصلی می‌تواند مدیر حذف کند.")
                return
            target = action.removeprefix("admin_remove:")
            if target == self.config.admin_id:
                await self.send(chat, "❌ مدیر اصلی قابل حذف نیست.")
                return
            await self.db.pool.execute(
                "UPDATE admins SET active=false WHERE rubika_id=$1", target
            )
            await self.db.audit(event["sender_id"], "admin_delete", details=target)
            await self.send(chat, f"✅ مدیر {target} حذف شد.")
            await self.admin(event, "admin_admins")
        elif action == "admin_clear_all":
            if event["sender_id"] != self.config.admin_id:
                await self.send(chat, "⛔️ فقط مالک اصلی می‌تواند مدیران را حذف کند.")
                return
            await self.db.pool.execute(
                "UPDATE admins SET active=false WHERE rubika_id<>$1",
                self.config.admin_id,
            )
            await self.db.audit(event["sender_id"], "admin_clear", details="all")
            await self.send(chat, "🗑 همه مدیران فرعی حذف شدند.")
            await self.admin(event, "admin_admins")
        else:
            await self.send(chat, self.admin_help(action))

    async def handle_admin_action(self, event, action, *, credential_only: bool = False):
        chat = event["chat_id"]
        admin_id = event["sender_id"]
        if credential_only and not (
            action.startswith("ticket:")
            or action.startswith("ticket_close:")
            or action.startswith("ticket_reply:")
        ):
            await self.send(chat, "⛔️ این عملیات برای نقش پشتیبان جم با اطلاعات مجاز نیست.")
            return
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
            if credential_only and str(ticket.get("category") or "bot") != "credential":
                await self.send(chat, "⛔️ فقط تیکت‌های جم با اطلاعات برای شما باز است.")
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
                f"{transcript or 'پیامی ثبت نشده.'}",
                buttons=inline(
                    [
                        [(f"ticket_reply:{ticket_id}", "💬 پاسخ")],
                        [(f"ticket_close:{ticket_id}", "✅ بستن تیکت")],
                    ]
                ),
            )
            return
        reply_match = re.fullmatch(r"ticket_reply:([1-9]\d*)", action)
        if reply_match:
            await self.ticket_reply_start(
                event, int(reply_match.group(1)), credential_only=credential_only
            )
            return
        close_match = re.fullmatch(r"ticket_close:([1-9]\d*)", action)
        if close_match:
            ticket_id = int(close_match.group(1))
            if credential_only:
                category = await self.db.pool.fetchval(
                    "SELECT category FROM tickets WHERE id=$1", ticket_id
                )
                if str(category or "bot") != "credential":
                    await self.send(chat, "⛔️ فقط تیکت‌های جم با اطلاعات برای شما باز است.")
                    return
            changed = await self.db.pool.execute(
                """UPDATE tickets SET status='closed',updated_at=now()
                   WHERE id=$1 AND status='open'""",
                ticket_id,
            )
            if changed.endswith("1"):
                ticket_row = await self.db.pool.fetchrow(
                    "SELECT user_id FROM tickets WHERE id=$1", ticket_id
                )
                if ticket_row:
                    user_row = await self.db.pool.fetchrow(
                        "SELECT chat_id FROM users WHERE id=$1",
                        ticket_row["user_id"],
                    )
                    if user_row and user_row["chat_id"]:
                        await self.api.send_message(
                            user_row["chat_id"],
                            f"✅ تیکت #{ticket_id} بسته شد.\n"
                            "اگر سوال دیگری داری، از منوی پشتیبانی تیکت جدید بساز.",
                        )
            await self.send(
                chat,
                "✅ تیکت بسته شد." if changed.endswith("1") else "این تیکت قبلاً بسته شده است.",
            )
            return
        if credential_only:
            await self.send(chat, "⛔️ این عملیات برای نقش پشتیبان جم با اطلاعات مجاز نیست.")
            return
        complete_match = re.fullmatch(r"order_complete:([1-9]\d*)", action)
        if complete_match:
            order_id = int(complete_match.group(1))
            cred = await self.db.get_credential_order(order_id)
            if cred:
                try:
                    await self.db.complete_credential_order(order_id)
                except ValueError as exc:
                    await self.send(chat, f"❌ {exc}")
                    return
                if cred["chat_id"]:
                    await self.api.send_message(
                        cred["chat_id"],
                        f"✅ سفارش #{order_id} تکمیل و تحویل شد.\n"
                        "برای امنیت، رمز اکانت را عوض کن.",
                        chat_keypad=main_menu(),
                    )
                await self.db.audit(admin_id, "order_complete", details=str(order_id))
                await self.send(chat, f"✅ سفارش #{order_id} تکمیل شد.")
                return
            row = await self.db.pool.fetchrow(
                """SELECT o.user_id, p.kind, f.status AS fulfillment_status
                   FROM orders o
                   JOIN order_items i ON i.order_id=o.id
                   JOIN products p ON p.id=i.product_id
                   LEFT JOIN fulfillments f ON f.order_id=o.id
                   WHERE o.id=$1 AND o.status IN ('paid','processing')
                   LIMIT 1""",
                order_id,
            )
            if not row:
                await self.send(chat, "سفارش قابل تکمیل نیست.")
                return
            if row["kind"] == "gem" and row["fulfillment_status"] in {
                "PENDING",
                "PROCESSING",
                "SUBMITTING",
                "SUBMIT_UNKNOWN",
            }:
                await self.send(
                    chat,
                    "⛔️ این سفارش جم خودکار است و هنوز در G2Bulk در جریان است؛ "
                    "تکمیل دستی مجاز نیست.",
                )
                return
            updated = await self.db.pool.fetchrow(
                """UPDATE orders SET status='completed'
                   WHERE id=$1 AND status IN ('paid','processing')
                   RETURNING user_id""",
                order_id,
            )
            if not updated:
                await self.send(chat, "سفارش قابل تکمیل نیست.")
                return
            await self.db.pool.execute(
                """UPDATE fulfillments SET status='COMPLETED',updated_at=now()
                   WHERE order_id=$1""",
                order_id,
            )
            user = await self.db.pool.fetchrow(
                "SELECT chat_id FROM users WHERE id=$1",
                updated["user_id"],
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
            "admin_products": "از دکمه «افزودن محصول» و باز کردن هر محصول برای ویرایش استفاده کن.",
            "admin_categories": "از دکمه «افزودن دسته» و دکمه‌های فعال/حذف استفاده کن.",
            "admin_finance": "همه تنظیمات مالی از دکمه‌های همین بخش قابل تغییرند.",
            "admin_search": "شناسه روبیکا یا شماره سفارش را بفرست.",
            "admin_broadcast": "متن پیام همگانی را بفرست.",
            "admin_codes": "از دکمه افزودن/حذف کد استفاده کن.",
            "admin_settings": "متن‌ها، کانال و دپارتمان را با دکمه مدیریت کن.",
            "admin_admins": "با دکمه، مدیر یا پشتیبان جم با اطلاعات اضافه/حذف کن.",
        }
        return "راهنما:\n" + docs.get(section, "از دکمه‌های پنل استفاده کن.")

    async def admin_command(self, event, command, *, credential_only: bool = False):
        admin_id, chat = event["sender_id"], event["chat_id"]
        name, _, args = command.partition(" ")
        if credential_only and name not in {"/reply", "/credadmin"}:
            await self.send(
                chat,
                "⛔️ از دکمه‌های پنل جم با اطلاعات استفاده کن.",
            )
            return
        try:
            if name == "/credadmin":
                await self.credadmin_home(event)
                return
            if name == "/reply":
                # سازگاری قدیمی؛ مسیر اصلی دکمه «💬 پاسخ» است.
                ticket_id, text = args.split(" ", 1)
                if not text.strip():
                    raise ValueError("متن پاسخ خالی است.")
                fake = dict(event)
                fake["text"] = text
                await self.ticket_reply_receive(
                    fake,
                    {
                        "ticket_id": int(ticket_id),
                        "credential_only": credential_only,
                    },
                )
                return
            elif name in {"/receipt_ok", "/receipt_no"}:
                receipt_arg = args.strip()
                if not receipt_arg.isdigit() or int(receipt_arg) <= 0:
                    raise ValueError(
                        f"شماره رسید را وارد کن؛ مثال: {name} 1"
                    )
                await self.review_receipt(admin_id, int(receipt_arg), name.endswith("_ok"))
            elif name in {"/join_ok", "/join_no"}:
                await self.review_join(admin_id, int(args), name.endswith("_ok"))
            elif name == "/sync_prices":
                await self.send(chat, "⏳ در حال بروزرسانی قیمت جم با نرخ لحظه‌ای…")
                result = await self.run_gem_price_sync_router()
                if not result.get("ok"):
                    raise ValueError(result.get("error") or "بروزرسانی ناموفق بود.")
                await self.send(
                    chat,
                    f"✅ بروزرسانی قیمت جم انجام شد.\n"
                    f"تعداد به‌روزرسانی‌شده: {result['updated']}\n"
                    f"نرخ دلار لحظه‌ای: {result['rate']:,} تومان ({result['source']})",
                )
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
                    "zarinpal_merchant_id",
                    "card_enabled",
                    "card_number",
                    "card_holder",
                    "card_bank",
                    "usd_toman_rate",
                    "credential_support_id",
                    "credential_weekly_profit_percent",
                    "credential_monthly_profit_percent",
                    "credential_weekly_cost_usd",
                    "credential_monthly_cost_usd",
                    "gem_profit_percent",
                }
                if key not in allowed:
                    raise ValueError("کلید تنظیمات مجاز نیست.")
                if key.endswith("_enabled") and value.strip() not in {"0", "1"}:
                    raise ValueError("مقدار این تنظیم فقط 0 یا 1 است.")
                if key in {"welcome_text", "help_text", "support_prompt"} and not value.strip():
                    raise ValueError("متن تنظیم نمی‌تواند خالی باشد.")
                if key == "zarinpal_merchant_id":
                    merchant = value.strip()
                    if not merchant:
                        raise ValueError("مرچنت زرین‌پال نمی‌تواند خالی باشد.")
                    try:
                        import uuid

                        uuid.UUID(merchant)
                    except (ValueError, AttributeError):
                        raise ValueError(
                            "مرچنت زرین‌پال باید UUID معتبر باشد."
                        ) from None
                    value = merchant
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
                if admin_id != self.config.admin_id:
                    raise ValueError("مدیریت مدیران فقط در اختیار مالک اصلی ربات است.")
                rubika_id, _, title = args.partition(" ")
                rubika_id, title = rubika_id.strip(), title.strip()
                if not re.fullmatch(r"u0[A-Za-z0-9]{10,80}", rubika_id):
                    raise ValueError("شناسه داخلی روبیکا معتبر نیست.")
                if not title:
                    raise ValueError("برای مدیر یک نام یا عنوان وارد کن.")
                await self.db.pool.execute(
                    """INSERT INTO admins(rubika_id,title,role,active)
                       VALUES($1,$2,'admin',true)
                       ON CONFLICT(rubika_id) DO UPDATE
                       SET title=$2,role='admin',active=true""",
                    rubika_id,
                    title,
                )
            elif name == "/credadmin_add":
                if admin_id != self.config.admin_id:
                    raise ValueError("افزودن پشتیبان جم با اطلاعات فقط برای مالک است.")
                rubika_id, _, title = args.partition(" ")
                rubika_id, title = rubika_id.strip(), title.strip()
                if not re.fullmatch(r"u0[A-Za-z0-9]{10,80}", rubika_id):
                    raise ValueError("شناسه داخلی روبیکا معتبر نیست.")
                await self.owner_add_credential_admin(event, rubika_id, title)
                return
            elif name == "/admin_delete":
                if admin_id != self.config.admin_id:
                    raise ValueError("مدیریت مدیران فقط در اختیار مالک اصلی ربات است.")
                if args.strip() == self.config.admin_id:
                    raise ValueError("مدیر اصلی قابل حذف نیست.")
                await self.db.pool.execute(
                    "UPDATE admins SET active=false WHERE rubika_id=$1", args.strip()
                )
            elif name == "/admin_clear":
                if admin_id != self.config.admin_id:
                    raise ValueError("مدیریت مدیران فقط در اختیار مالک اصلی ربات است.")
                await self.db.pool.execute(
                    "UPDATE admins SET active=false WHERE rubika_id<>$1",
                    self.config.admin_id,
                )
            elif name == "/product_add":
                kind, title, price, stock, amount, sku, cost = args.split("|", 6)
                kind = kind.strip()
                if kind not in {"gem", "sense_mobile", "sense_pc", "store", "gem_credentials"}:
                    raise ValueError("نوع محصول مجاز نیست.")
                if not title.strip():
                    raise ValueError("عنوان محصول خالی است.")
                safe_price = checked_amount(price, label="قیمت محصول")
                safe_stock = int(stock)
                if safe_stock < 0:
                    raise ValueError("موجودی محصول منفی نمی‌تواند باشد.")
                safe_amount = int(amount) if amount.strip() else None
                safe_cost = (
                    checked_decimal(cost, label="هزینه دلاری")
                    if cost.strip() else None
                )
                if safe_amount is not None and safe_amount <= 0:
                    raise ValueError("تعداد محصول باید مثبت باشد.")
                if safe_cost is not None and safe_cost <= 0:
                    raise ValueError("هزینه دلاری باید مثبت باشد.")
                await self.db.pool.execute(
                    """INSERT INTO products(
                         kind,title,price,stock,amount,supplier_sku,
                         supplier_cost_usd,sort_order
                       ) VALUES(
                         $1,$2,$3,$4,$5,$6,$7,
                         (SELECT COALESCE(MAX(sort_order),0)+10 FROM products)
                       )""",
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
                    "sort_order",
                }
                if field not in allowed:
                    raise ValueError("فیلد غیرمجاز است.")
                if field == "price":
                    value = checked_amount(value, label="قیمت محصول")
                elif field in {"stock", "amount", "category_id", "sort_order"}:
                    value = int(value)
                    if field in {"stock", "sort_order"} and value < 0:
                        raise ValueError("موجودی یا ترتیب نمایش نمی‌تواند منفی باشد.")
                elif field == "supplier_cost_usd":
                    value = checked_decimal(value, label="هزینه دلاری")
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
            elif name == "/product_move":
                item_id, direction = args.split("|", 1)
                await self.db.move_catalogue_item("products", int(item_id), direction)
            elif name == "/category_add":
                if not args.strip():
                    raise ValueError("عنوان دسته‌بندی خالی است.")
                await self.db.pool.execute(
                    """INSERT INTO categories(title,sort_order)
                       VALUES($1,(SELECT COALESCE(MAX(sort_order),0)+10 FROM categories))
                       ON CONFLICT(title) DO UPDATE SET active=true""",
                    args.strip(),
                )
            elif name == "/category_delete":
                await self.db.pool.execute(
                    "UPDATE categories SET active=false WHERE id=$1", int(args)
                )
            elif name == "/category_move":
                item_id, direction = args.split("|", 1)
                await self.db.move_catalogue_item("categories", int(item_id), direction)
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
                try:
                    user_id, amount = args.split()
                except ValueError:
                    raise ValueError("فرمت درست: /charge USER_ID AMOUNT") from None
                amount = checked_amount(amount, label="شارژ کاربر")
                target_id = int(user_id)
                exists = await self.db.pool.fetchval(
                    "SELECT 1 FROM users WHERE id=$1", target_id
                )
                if not exists:
                    raise ValueError("کاربر پیدا نشد؛ شناسه داخلی را از بخش «کاربران» بگیر.")
                async with self.db.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            amount,
                            target_id,
                        )
                        await conn.execute(
                            """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                               VALUES($1,$2,'admin_charge',$3)""",
                            target_id,
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
                name = (row.get("display_name") or "").strip() or "—"
                await self.send(
                    chat,
                    f"کاربر {row['id']}\n"
                    f"👤 نام: {name}\n"
                    f"🆔 روبیکا: {row['rubika_id']}\n"
                    f"💰 موجودی: {row['balance']:,}\n"
                    f"🛒 خریدها: {row['purchases']:,}\n"
                    f"👥 زیرمجموعه‌ها: {row['referral_count']:,}\n"
                    f"💳 کارت: {'تأییدشده' if row['card_verified'] else 'تأییدنشده'}\n"
                    f"وضعیت: {'مسدود 🚫' if row['blocked'] else 'فعال ✅'}",
                )
                return
            elif name in {"/users_balance", "/users_referral", "/users_card"}:
                clauses = {
                    "/users_balance": "balance>0",
                    "/users_referral": "EXISTS(SELECT 1 FROM users child WHERE child.referred_by=users.id)",
                    "/users_card": "card_verified",
                }
                rows = await self.db.pool.fetch(
                    f"""SELECT id,display_name,rubika_id,balance,card_number FROM users
                        WHERE {clauses[name]} ORDER BY id DESC LIMIT 100"""
                )
                await self.send(
                    chat,
                    "\n".join(
                        f"{row['id']} | {(row['display_name'] or '—').strip()} | "
                        f"{row['rubika_id']} | {row['balance']:,}"
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

    async def sync_credential_prices_now(self, *, force=True):
        """فقط جم با اطلاعات — $1.328/$6.64 × نرخ Nobitex/fallback."""
        manual = await self.db.setting("usd_toman_rate", "")
        rate = await usd_toman_rate(manual, force=True)
        if not rate.get("ok"):
            return {"ok": False, "error": rate.get("error")}
        if rate.get("source") == "nobitex_usdtirt_best_ask":
            await self.db.set_setting("usd_toman_rate", str(rate["rate"]))
        updated = await self.db.sync_credential_prices(rate["rate"], force=force)
        await self.db.touch_credential_price_last_sync()
        cfg = await self.db.get_credential_pricing_config()
        from supplier import compute_gem_sale_price

        weekly_price = await compute_gem_sale_price(
            cfg["weekly_cost"], rate["rate"], cfg["weekly_profit"]
        )
        monthly_price = await compute_gem_sale_price(
            cfg["monthly_cost"], rate["rate"], cfg["monthly_profit"]
        )
        return {
            "ok": True,
            "updated": updated,
            "rate": rate["rate"],
            "source": rate["source"],
            "fallback": bool(rate.get("fallback")),
            "weekly_profit": cfg["weekly_profit"],
            "monthly_profit": cfg["monthly_profit"],
            "weekly_cost": str(cfg["weekly_cost"]),
            "monthly_cost": str(cfg["monthly_cost"]),
            "weekly_price": weekly_price,
            "monthly_price": monthly_price,
        }

    def format_credential_sync_report(self, sync: dict) -> str:
        lines = [
            "✅ جم با اطلاعات — قیمت‌ها اعمال شد.",
            "(مستقل از G2Bulk · فقط $ × نرخ لحظه‌ای × سود)",
            f"بسته‌های به‌روز: {sync.get('updated', 0)}",
            f"نرخ دلار: {sync.get('rate', 0):,} ت ({sync.get('source', '—')})",
            (
                f"سود: هفتگی {sync.get('weekly_profit', 40)}٪ · "
                f"ماهانه {sync.get('monthly_profit', 40)}٪"
            ),
            (
                f"بهای خالص: {sync.get('weekly_cost', '1.328')} / "
                f"{sync.get('monthly_cost', '6.64')} USD"
            ),
        ]
        if sync.get("weekly_price") is not None:
            lines.append(
                f"📅 فروش هفتگی: {int(sync['weekly_price']):,} ت · "
                f"📆 فروش ماهانه: {int(sync['monthly_price']):,} ت"
            )
        if sync.get("fallback"):
            lines.append("⚠️ نرخ از fallback دستی بود.")
        return "\n".join(lines)

    async def gem_profit_percent(self) -> int:
        """درصد سود بسته‌های جم؛ از تنظیم دیتابیس (پیش‌فرض ۱۰)."""
        raw = await self.db.setting("gem_profit_percent", "10")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 10
        return max(1, min(200, value))

    async def _format_price_sync_report(self, result: dict) -> str:
        lines = [
            "✅ بروزرسانی قیمت انجام شد.",
            f"جم با آیدی: {result.get('gem_updated', 0)} مورد",
            f"جم با اطلاعات: {result.get('cred_updated', 0)} مورد",
            f"سود جم با آیدی: {result.get('profit_percent', '—')}٪",
            (
                f"سود هفتگی/ماهانه: {result.get('weekly_profit', 40)}٪ / "
                f"{result.get('monthly_profit', 40)}٪"
            ),
            f"نرخ دلار: {result.get('rate', 0):,} تومان ({result.get('source', '—')})",
        ]
        if result.get("fallback"):
            lines.append("⚠️ نرخ از fallback دستی استفاده شد.")
        cat_err = str(result.get("catalogue_error") or "").strip()
        if cat_err:
            lines.append(f"⚠️ کاتالوگ G2Bulk: {cat_err}")
        total = int(result.get("updated") or 0)
        if total == 0:
            lines.append(
                "ℹ️ هیچ ردیفی تغییر نکرد (قیمت‌ها از قبل همین بودند یا SKU جم با آیدی "
                "با کاتالوگ G2Bulk مطابقت ندارد)."
            )
        return "\n".join(lines)

    async def _run_price_sync(self, *, force_rate=False):
        manual = await self.db.setting("usd_toman_rate", "")
        rate = await usd_toman_rate(manual, force=force_rate)
        if not rate.get("ok"):
            return {"ok": False, "error": rate.get("error")}
        if rate.get("source") == "nobitex_usdtirt_best_ask":
            await self.db.set_setting("usd_toman_rate", str(rate["rate"]))
        gem_updated = 0
        catalogue = await self.g2.catalogue()
        catalogue_error = ""
        if catalogue.get("ok"):
            profit_percent = await self.gem_profit_percent()
            gem_updated = await self.db.sync_gem_prices_from_catalogue(
                items=catalogue["items"],
                rate_value=rate["rate"],
                profit_percent=profit_percent,
            )
        else:
            catalogue_error = catalogue.get("error") or "دریافت کاتالوگ G2Bulk ناموفق بود."
        cred_cfg = await self.db.get_credential_pricing_config()
        cred_updated = await self.db.sync_credential_prices(
            rate["rate"], force=force_rate
        )
        await self.db.touch_price_last_sync()
        return {
            "ok": True,
            "updated": gem_updated + cred_updated,
            "gem_updated": gem_updated,
            "cred_updated": cred_updated,
            "rate": rate["rate"],
            "source": rate["source"],
            "fallback": bool(rate.get("fallback")),
            "profit_percent": await self.gem_profit_percent(),
            "weekly_profit": cred_cfg["weekly_profit"],
            "monthly_profit": cred_cfg["monthly_profit"],
            "weekly_cost": str(cred_cfg["weekly_cost"]),
            "monthly_cost": str(cred_cfg["monthly_cost"]),
            "catalogue_error": catalogue_error,
        }

    async def run_gem_price_sync_router(self):
        """بروزرسانی دستی قیمت جم با آیدی و جم با اطلاعات (مثل تلگرام)."""
        return await self._run_price_sync(force_rate=True)

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
            "✅ عضویت تأیید شد؛ از منوی پایین شروع کن."
            if approved
            else "❌ عضویت تأیید نشد.",
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
                    if (
                        receipt["provider"] != "card"
                        or receipt["payment_status"] != "pending"
                    ):
                        raise ValueError("پرداخت دیگر در وضعیت انتظار نیست.")
                    if receipt["purpose"] == "order" and (
                        not order
                        or order["status"] != "pending"
                        or not order["inventory_reserved"]
                    ):
                        raise ValueError("سفارش قبلاً پرداخت یا بسته شده است.")
                    if receipt["purpose"] == "order":
                        issued_gateway = await conn.fetchval(
                            """SELECT 1 FROM payments
                               WHERE order_id=$1 AND id<>$2
                                 AND provider='gateway' AND authority IS NOT NULL
                                 AND status='pending' AND expires_at>now()
                               LIMIT 1""",
                            receipt["order_id"],
                            receipt["payment_id"],
                        )
                        if issued_gateway:
                            raise ValueError(
                                "برای این سفارش لینک درگاه هم صادر شده است؛ "
                                "ابتدا وضعیت درگاه باید تطبیق شود."
                            )
                    await conn.execute(
                        "UPDATE payments SET status='verified',verified_at=now() WHERE id=$1",
                        receipt["payment_id"],
                    )
                    if receipt["purpose"] == "wallet":
                        reference = f"receipt:{receipt_id}"
                        inserted = await conn.fetchval(
                            """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                               VALUES($1,$2,'card_charge',$3)
                               ON CONFLICT(reference) DO NOTHING RETURNING id""",
                            receipt["user_id"],
                            receipt["amount"],
                            reference,
                        )
                        if inserted:
                            await conn.execute(
                                "UPDATE users SET balance=balance+$1 WHERE id=$2",
                                receipt["amount"],
                                receipt["user_id"],
                            )
                    else:
                        _, due = order_amounts(
                            order["total_amount"],
                            order["discount_amount"],
                            order["wallet_paid"],
                        )
                        if int(receipt["amount"]) != due:
                            raise ValueError(
                                "مبلغ رسید با مانده سفارش مطابقت ندارد."
                            )
                        await conn.execute(
                            """UPDATE receipts SET status='rejected',reviewed_at=now()
                               WHERE payment_id IN (
                                 SELECT id FROM payments
                                 WHERE order_id=$1 AND id<>$2
                               ) AND status='pending'""",
                            receipt["order_id"],
                            receipt["payment_id"],
                        )
                        await conn.execute(
                            """UPDATE payments SET status='cancelled'
                               WHERE order_id=$1 AND id<>$2 AND status='pending'""",
                            receipt["order_id"],
                            receipt["payment_id"],
                        )
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
        if approved:
            if receipt["purpose"] == "wallet":
                msg = (
                    f"✅ شارژ کیف پول به مبلغ {receipt['amount']:,} تومان تأیید شد."
                )
            else:
                msg = (
                    f"✅ رسید تأیید و پرداخت سفارش #{receipt['order_id']} ثبت شد.\n"
                    f"مبلغ: {receipt['amount']:,} تومان"
                )
        else:
            msg = (
                "❌ رسید رد شد؛ با پشتیبانی تماس بگیر.\n"
                f"آیدی پشتیبانی: {(await self.db.get_support_contact())['handle']}"
            )
        await self.api.send_message(user["chat_id"], msg)
