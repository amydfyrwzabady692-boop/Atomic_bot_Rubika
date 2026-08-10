"""فلوهای دکمه‌محور پنل ادمین روبیکا (بدون نیاز به دستور اسلش)."""
from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from keyboards import admin_menu, inline
from payment_safety import checked_amount, valid_card_number

log = logging.getLogger(__name__)

PRODUCT_KINDS = (
    ("gem", "💎 جم با آیدی"),
    ("gem_credentials", "🔐 جم با اطلاعات"),
    ("sense_mobile", "📱 سنس موبایل"),
    ("sense_pc", "🖥 سنس PC"),
    ("store", "🛍 فروشگاه"),
)


class AdminFlowHandlers:
    """Mixin: Router methods for button-driven admin CRUD."""

    async def open_admin_panel(self, event):
        await self.send(
            event["chat_id"],
            "🛠 پنل مدیریت اتومیک روبیکا\nاز منوی پایین یا دکمه‌ها استفاده کن.",
            menu=admin_menu(),
        )

    async def ticket_reply_start(self, event, ticket_id: int, *, credential_only=False):
        ticket = await self.db.pool.fetchrow(
            "SELECT id,category,status FROM tickets WHERE id=$1", ticket_id
        )
        if not ticket:
            await self.send(event["chat_id"], "تیکت پیدا نشد.")
            return
        if credential_only and str(ticket.get("category") or "bot") != "credential":
            await self.send(event["chat_id"], "⛔️ فقط تیکت‌های جم با اطلاعات برای شما باز است.")
            return
        if ticket["status"] != "open":
            await self.send(event["chat_id"], "این تیکت بسته است.")
            return
        await self.db.set_session(
            event["sender_id"],
            "admin_ticket_reply",
            {"ticket_id": int(ticket_id), "credential_only": bool(credential_only)},
        )
        await self.send(
            event["chat_id"],
            f"💬 پاسخ به تیکت #{ticket_id}\nمتن پاسخ را همین‌جا بفرست:",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )

    async def ticket_reply_receive(self, event, data):
        text = (event.get("text") or "").strip()
        if not text:
            await self.send(event["chat_id"], "متن پاسخ خالی است؛ دوباره بفرست.")
            return
        ticket_id = int(data.get("ticket_id") or 0)
        credential_only = bool(data.get("credential_only"))
        row = await self.db.pool.fetchrow(
            """SELECT t.id,t.category,u.chat_id FROM tickets t
               JOIN users u ON u.id=t.user_id WHERE t.id=$1""",
            ticket_id,
        )
        if not row:
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], "تیکت پیدا نشد.")
            return
        if credential_only and str(row.get("category") or "bot") != "credential":
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], "⛔️ دسترسی به این تیکت ندارید.")
            return
        await self.db.pool.execute(
            """INSERT INTO ticket_messages(ticket_id,sender_type,sender_id,text)
               VALUES($1,'admin',$2,$3)""",
            ticket_id,
            event["sender_id"],
            text[:4000],
        )
        await self.db.pool.execute(
            "UPDATE tickets SET updated_at=now() WHERE id=$1", ticket_id
        )
        await self.db.set_session(event["sender_id"])
        try:
            await self.api.send_message(
                row["chat_id"], f"🎧 پاسخ پشتیبانی #{ticket_id}\n{text}"
            )
        except Exception:
            log.exception("ticket reply notify failed")
        await self.send(
            event["chat_id"],
            f"✅ پاسخ تیکت #{ticket_id} ارسال شد.",
            buttons=inline(
                [
                    [(f"ticket:{ticket_id}", "🎫 بازگشت به تیکت")],
                    [("admin_support", "🎧 لیست تیکت‌ها")],
                    [("cred_admin_tickets", "🔐 تیکت‌های جم با اطلاعات")],
                ]
            ),
        )

    async def products_manage_home(self, event):
        rows = await self.db.pool.fetch(
            """SELECT id,kind,title,price,stock,active
               FROM products ORDER BY kind,sort_order,id LIMIT 40"""
        )
        lines = ["📦 مدیریت محصولات", "━━━━━━━━━━━━━━━"]
        buttons = [[("admin_product_add", "➕ افزودن محصول")]]
        for row in rows:
            flag = "✅" if row["active"] else "❌"
            lines.append(
                f"{flag} #{row['id']} | {row['kind']} | {row['title']} | "
                f"{int(row['price']):,} ت | موجودی {row['stock']}"
            )
            buttons.append(
                [
                    (
                        f"admin_product_open:{row['id']}",
                        f"#{row['id']} {row['title'][:18]}",
                    )
                ]
            )
        if not rows:
            lines.append("محصولی نیست.")
        buttons.append([("admin_shop", "🔙 فروشگاه")])
        await self.send(event["chat_id"], "\n".join(lines), buttons=inline(buttons))

    async def product_open(self, event, product_id: int):
        row = await self.db.pool.fetchrow(
            "SELECT * FROM products WHERE id=$1", product_id
        )
        if not row:
            await self.send(event["chat_id"], "محصول پیدا نشد.")
            return
        text = (
            f"📦 محصول #{row['id']}\n"
            f"نوع: {row['kind']}\n"
            f"عنوان: {row['title']}\n"
            f"قیمت: {int(row['price']):,} تومان\n"
            f"موجودی: {row['stock']}\n"
            f"وضعیت: {'فعال ✅' if row['active'] else 'غیرفعال ❌'}\n"
            f"SKU: {row['supplier_sku'] or '—'}\n"
            f"هزینه دلاری: {row['supplier_cost_usd'] or '—'}"
        )
        await self.send(
            event["chat_id"],
            text,
            buttons=inline(
                [
                    [
                        (
                            f"admin_product_toggle:{product_id}",
                            "🔴 غیرفعال" if row["active"] else "🟢 فعال‌سازی",
                        )
                    ],
                    [(f"admin_product_edit_price:{product_id}", "💰 تغییر قیمت")],
                    [(f"admin_product_edit_stock:{product_id}", "📦 تغییر موجودی")],
                    [
                        (f"admin_product_move:{product_id}:up", "⬆️"),
                        (f"admin_product_move:{product_id}:down", "⬇️"),
                    ],
                    [(f"admin_product_delete:{product_id}", "🗑 حذف (غیرفعال)")],
                    [("admin_products", "🔙 لیست")],
                ]
            ),
        )

    async def product_add_start(self, event):
        await self.db.set_session(event["sender_id"], "admin_product_add_kind", {})
        await self.send(
            event["chat_id"],
            "➕ افزودن محصول\nنوع محصول را انتخاب کن:",
            buttons=inline(
                [[(f"admin_product_kind:{k}", label)] for k, label in PRODUCT_KINDS]
                + [[("admin_products", "✖️ انصراف")]]
            ),
        )

    async def product_kind_selected(self, event, kind: str):
        if kind not in {k for k, _ in PRODUCT_KINDS}:
            await self.send(event["chat_id"], "نوع نامعتبر است.")
            return
        await self.db.set_session(
            event["sender_id"], "admin_product_add_title", {"kind": kind}
        )
        await self.send(
            event["chat_id"],
            "عنوان محصول را بفرست:",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )

    async def product_add_handle_state(self, event, state, data) -> bool:
        text = (event.get("text") or "").strip()
        if state == "admin_product_add_title":
            if not text or len(text) > 120:
                await self.send(event["chat_id"], "عنوان معتبر بفرست (حداکثر ۱۲۰ کاراکتر).")
                return True
            data["title"] = text
            await self.db.set_session(
                event["sender_id"], "admin_product_add_price", data
            )
            await self.send(event["chat_id"], "قیمت فروش به تومان را بفرست (فقط عدد):")
            return True
        if state == "admin_product_add_price":
            try:
                price = checked_amount(text, label="قیمت")
            except ValueError as exc:
                await self.send(event["chat_id"], f"❌ {exc}")
                return True
            data["price"] = price
            await self.db.set_session(
                event["sender_id"], "admin_product_add_stock", data
            )
            await self.send(event["chat_id"], "موجودی اولیه را بفرست (عدد، مثلاً 999):")
            return True
        if state == "admin_product_add_stock":
            try:
                stock = int(
                    text.translate(
                        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
                    )
                )
            except ValueError:
                await self.send(event["chat_id"], "❌ فقط عدد بفرست.")
                return True
            if stock < 0:
                await self.send(event["chat_id"], "موجودی منفی مجاز نیست.")
                return True
            kind = data["kind"]
            title = data["title"]
            price = data["price"]
            sku = ""
            amount = None
            if kind == "gem_credentials":
                sku = "cred_weekly" if "هفته" in title else "cred_monthly"
                amount = 60 if sku == "cred_weekly" else 300
            await self.db.pool.execute(
                """INSERT INTO products(
                     kind,title,price,stock,amount,supplier_sku,sort_order,active
                   ) VALUES(
                     $1,$2,$3,$4,$5,$6,
                     (SELECT COALESCE(MAX(sort_order),0)+10 FROM products),
                     true
                   )""",
                kind,
                title,
                price,
                stock,
                amount,
                sku,
            )
            await self.db.set_session(event["sender_id"])
            await self.send(event["chat_id"], f"✅ محصول «{title}» اضافه شد.")
            await self.products_manage_home(event)
            return True
        return False

    async def categories_manage_home(self, event):
        rows = await self.db.pool.fetch(
            "SELECT id,title,active,sort_order FROM categories ORDER BY sort_order,id"
        )
        lines = ["🗂 دسته‌بندی‌ها", "━━━━━━━━━━━━━━━"]
        buttons = [[("admin_category_add", "➕ افزودن دسته")]]
        for row in rows:
            flag = "✅" if row["active"] else "❌"
            lines.append(f"{flag} #{row['id']} | {row['title']}")
            buttons.append(
                [
                    (f"admin_category_toggle:{row['id']}", f"{flag} #{row['id']}"),
                    (f"admin_category_del:{row['id']}", "🗑"),
                ]
            )
        if not rows:
            lines.append("دسته‌ای نیست.")
        buttons.append([("admin_shop", "🔙 فروشگاه")])
        await self.send(event["chat_id"], "\n".join(lines), buttons=inline(buttons))

    async def category_add_start(self, event):
        await self.db.set_session(event["sender_id"], "admin_category_add", {})
        await self.send(
            event["chat_id"],
            "➕ عنوان دسته‌بندی را بفرست:",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )

    async def finance_edit_start(self, event, key: str):
        labels = {
            "zarinpal_merchant_id": "مرچنت زرین‌پال (UUID)",
            "card_number": "شماره کارت ۱۶ رقمی",
            "card_holder": "نام دارنده کارت",
            "card_bank": "نام بانک",
            "usd_toman_rate": "نرخ دلار به تومان",
            "welcome_text": "متن خوش‌آمد",
            "help_text": "متن راهنما",
            "support_prompt": "متن شروع پشتیبانی",
            "credential_support_id": "شناسه پشتیبانی جم با اطلاعات",
        }
        if key not in labels:
            await self.send(event["chat_id"], "کلید نامعتبر است.")
            return
        current = await self.db.setting(key, "")
        await self.db.set_session(
            event["sender_id"], "admin_setting_edit", {"key": key}
        )
        preview = (current[:200] + "…") if len(str(current)) > 200 else (current or "—")
        await self.send(
            event["chat_id"],
            f"✏️ {labels[key]}\nمقدار فعلی:\n{preview}\n\nمقدار جدید را بفرست:",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )

    async def setting_edit_receive(self, event, data):
        key = str(data.get("key") or "")
        value = (event.get("text") or "").strip()
        if not key:
            await self.db.set_session(event["sender_id"])
            return
        try:
            value = await self._validate_setting_value(key, value)
        except ValueError as exc:
            await self.send(event["chat_id"], f"❌ {exc}")
            return
        await self.db.set_setting(key, value)
        await self.db.set_session(event["sender_id"])
        await self.send(event["chat_id"], f"✅ ذخیره شد: {key}")
        if key.startswith(("card_", "zarinpal_", "usd_")):
            await self.admin(event, "admin_finance")
        elif key in {"welcome_text", "help_text", "support_prompt"}:
            await self.admin(event, "admin_settings")
        elif key == "credential_support_id":
            await self.admin(event, "admin_admins")

    async def _validate_setting_value(self, key: str, value: str):
        if key.endswith("_enabled") and value not in {"0", "1"}:
            raise ValueError("فقط 0 یا 1")
        if key in {"welcome_text", "help_text", "support_prompt"} and not value:
            raise ValueError("متن نمی‌تواند خالی باشد.")
        if key == "zarinpal_merchant_id":
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError) as exc:
                raise ValueError("مرچنت باید UUID معتبر باشد.") from exc
            return value
        if key == "card_number":
            normalized = re.sub(r"[\s-]+", "", value).translate(
                str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
            )
            if not valid_card_number(normalized):
                raise ValueError("شماره کارت معتبر نیست.")
            return normalized
        if key == "usd_toman_rate":
            rate = int(value.replace(",", "").strip())
            if not 10_000 <= rate <= 10_000_000:
                raise ValueError("نرخ خارج از محدوده است.")
            return str(rate)
        if key.endswith("_cost_usd"):
            try:
                num = Decimal(value.replace(",", ""))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("عدد اعشاری معتبر بفرست.") from exc
            if num < Decimal("0.01") or num > Decimal("1000"):
                raise ValueError("خارج از محدوده")
            return str(num)
        if key.endswith("_profit_percent"):
            num = int(value.replace("%", "").replace("٪", ""))
            if not 1 <= num <= 200:
                raise ValueError("درصد بین ۱ تا ۲۰۰")
            return str(num)
        return value

    async def search_start(self, event):
        await self.db.set_session(event["sender_id"], "admin_search_query", {})
        await self.send(
            event["chat_id"],
            "🔎 جستجو\nشناسه روبیکای کاربر یا شماره سفارش را بفرست:",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )

    async def search_receive(self, event):
        raw = (event.get("text") or "").strip()
        await self.db.set_session(event["sender_id"])
        if raw.isdigit():
            order = await self.db.pool.fetchrow(
                """SELECT o.*,u.rubika_id,u.display_name FROM orders o
                   JOIN users u ON u.id=o.user_id WHERE o.id=$1""",
                int(raw),
            )
            if order:
                await self.send(
                    event["chat_id"],
                    f"📦 سفارش #{order['id']}\n"
                    f"وضعیت: {order['status']}\n"
                    f"مبلغ: {int(order['total_amount']):,} ت\n"
                    f"کاربر: {order['display_name']} · {order['rubika_id']}",
                    buttons=inline(
                        [
                            [(f"order_complete:{order['id']}", "✅ تکمیل سفارش")],
                            [("admin_orders", "🔙 سفارش‌ها")],
                        ]
                    ),
                )
                return
        user = await self.db.pool.fetchrow(
            "SELECT * FROM users WHERE rubika_id=$1", raw
        )
        if not user:
            await self.send(event["chat_id"], "❌ چیزی پیدا نشد.")
            return
        await self.send(
            event["chat_id"],
            f"👤 کاربر\n"
            f"شناسه: {user['rubika_id']}\n"
            f"نام: {user['display_name']}\n"
            f"موجودی: {int(user['balance'] or 0):,} ت\n"
            f"وضعیت: {'مسدود' if user['blocked'] else 'فعال'}",
            buttons=inline(
                [
                    [
                        (
                            f"admin_block:{user['id']}",
                            "🚫 بن" if not user["blocked"] else "✅ آنبن",
                        )
                    ],
                    [("admin_users", "🔙 کاربران")],
                ]
            ),
        )

    async def channel_add_start(self, event):
        await self.db.set_session(event["sender_id"], "admin_channel_add", {})
        await self.send(
            event["chat_id"],
            "📢 افزودن کانال اجباری\n"
            "در یک خط بفرست:\n"
            "`chat_id|عنوان|لینک دعوت`\n"
            "مثال: `b0xxx|کانال اصلی|https://rubika.ir/...`",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )

    async def department_add_start(self, event):
        await self.db.set_session(event["sender_id"], "admin_department_add", {})
        await self.send(
            event["chat_id"],
            "🏷 عنوان دپارتمان پشتیبانی را بفرست:",
            buttons=inline([[("admin_cancel_broadcast", "✖️ انصراف")]]),
        )
