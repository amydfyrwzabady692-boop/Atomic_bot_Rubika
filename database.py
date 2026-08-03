import json
from pathlib import Path

import asyncpg


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            await conn.execute(schema)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def user(self, rubika_id: str, chat_id: str, display_name: str = ""):
        return await self.pool.fetchrow(
            """INSERT INTO users(rubika_id,chat_id,display_name) VALUES($1,$2,$3)
               ON CONFLICT(rubika_id) DO UPDATE SET
                 chat_id=EXCLUDED.chat_id,
                 display_name=CASE
                   WHEN EXCLUDED.display_name<>'' THEN EXCLUDED.display_name
                   ELSE users.display_name
                 END
               RETURNING *""",
            rubika_id,
            chat_id,
            (display_name or "")[:200],
        )

    async def is_admin(self, rubika_id: str, root_id: str) -> bool:
        if rubika_id == root_id:
            return True
        return bool(
            await self.pool.fetchval(
                "SELECT 1 FROM admins WHERE rubika_id=$1 AND active", rubika_id
            )
        )

    async def setting(self, key: str, default=""):
        value = await self.pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
        return default if value is None else value

    async def set_setting(self, key: str, value: str):
        await self.pool.execute(
            """INSERT INTO settings(key,value,updated_at) VALUES($1,$2,now())
               ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()""",
            key,
            value,
        )

    async def claim_event(self, event_id: str) -> bool:
        result = await self.pool.execute(
            "INSERT INTO processed_events(event_id) VALUES($1) ON CONFLICT DO NOTHING",
            event_id,
        )
        return result.endswith("1")

    async def session(self, rubika_id: str):
        row = await self.pool.fetchrow(
            "SELECT state,data FROM sessions WHERE rubika_id=$1", rubika_id
        )
        if not row:
            return "", {}
        data = row["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = {}
        return row["state"], data if isinstance(data, dict) else {}

    async def set_session(self, rubika_id: str, state="", data=None):
        await self.pool.execute(
            """INSERT INTO sessions(rubika_id,state,data,updated_at)
               VALUES($1,$2,$3::jsonb,now()) ON CONFLICT(rubika_id) DO UPDATE
               SET state=EXCLUDED.state,data=EXCLUDED.data,updated_at=now()""",
            rubika_id,
            state,
            json.dumps(data or {}),
        )

    async def products(self, kind: str):
        return await self.pool.fetch(
            "SELECT * FROM products WHERE kind=$1 AND active AND stock>0 "
            "ORDER BY sort_order,id",
            kind,
        )

    async def move_catalogue_item(self, table: str, item_id: int, direction: str):
        """Move a product/category deterministically and compact its sort ranks."""
        if table not in {"products", "categories"}:
            raise ValueError("جدول مرتب‌سازی نامعتبر است.")
        direction = str(direction or "").strip().lower()
        if direction not in {"up", "down", "first", "last"}:
            raise ValueError("جهت باید up، down، first یا last باشد.")
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                rows = await conn.fetch(
                    f"SELECT id FROM {table} ORDER BY sort_order,id FOR UPDATE"
                )
                identifiers = [int(row["id"]) for row in rows]
                item_id = int(item_id)
                if item_id not in identifiers:
                    raise ValueError("آیتم برای مرتب‌سازی پیدا نشد.")
                old_index = identifiers.index(item_id)
                new_index = {
                    "up": max(0, old_index - 1),
                    "down": min(len(identifiers) - 1, old_index + 1),
                    "first": 0,
                    "last": len(identifiers) - 1,
                }[direction]
                identifiers.pop(old_index)
                identifiers.insert(new_index, item_id)
                await conn.executemany(
                    f"UPDATE {table} SET sort_order=$1 WHERE id=$2",
                    [(rank * 10, identifier) for rank, identifier in enumerate(identifiers, 1)],
                )
                return old_index != new_index

    async def create_order(self, user_id: int, product_id: int, player_id=""):
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                await conn.fetchval(
                    "SELECT id FROM users WHERE id=$1 FOR UPDATE",
                    user_id,
                )
                old_orders = await conn.fetch(
                    """SELECT o.id,o.wallet_paid,
                              EXISTS(
                                SELECT 1 FROM payments p
                                WHERE p.order_id=o.id
                                  AND p.provider='gateway'
                                  AND p.authority IS NOT NULL
                                  AND p.status='pending' AND p.expires_at>now()
                              ) gateway_issued,
                              EXISTS(
                                SELECT 1 FROM payments p
                                JOIN receipts r ON r.payment_id=p.id
                                WHERE p.order_id=o.id
                                  AND r.status='pending'
                              ) receipt_pending
                       FROM orders o
                       WHERE o.user_id=$1 AND o.status='pending' FOR UPDATE""",
                    user_id,
                )
                if any(
                    old["gateway_issued"] or old["receipt_pending"]
                    for old in old_orders
                ):
                    raise ValueError(
                        "یک سفارش با لینک درگاه یا رسیدِ در انتظار داری؛ "
                        "ابتدا نتیجه همان پرداخت باید مشخص شود."
                    )
                for old in old_orders:
                    if old["wallet_paid"]:
                        reference = f"cancel-order:{old['id']}:wallet-refund"
                        inserted = await conn.fetchval(
                            """INSERT INTO wallet_ledger(
                                 user_id,amount,entry_type,reference
                               ) VALUES($1,$2,'order_refund',$3)
                               ON CONFLICT(reference) DO NOTHING RETURNING id""",
                            user_id,
                            old["wallet_paid"],
                            reference,
                        )
                        if inserted:
                            await conn.execute(
                                "UPDATE users SET balance=balance+$1 WHERE id=$2",
                                old["wallet_paid"],
                                user_id,
                            )
                    await conn.execute(
                        """UPDATE products p SET stock=stock+i.quantity
                           FROM order_items i
                           WHERE i.order_id=$1 AND i.product_id=p.id
                             AND EXISTS (
                               SELECT 1 FROM orders o WHERE o.id=$1
                               AND o.inventory_reserved
                             )""",
                        old["id"],
                    )
                if old_orders:
                    old_ids = [row["id"] for row in old_orders]
                    await conn.execute(
                        """UPDATE receipts SET status='rejected',reviewed_at=now()
                           WHERE payment_id IN (
                             SELECT id FROM payments WHERE order_id=ANY($1::bigint[])
                           ) AND status='pending'""",
                        old_ids,
                    )
                    await conn.execute(
                        """UPDATE payments SET status='cancelled'
                           WHERE order_id=ANY($1::bigint[]) AND status='pending'""",
                        old_ids,
                    )
                    await conn.execute(
                        """UPDATE orders SET status='cancelled',
                           inventory_reserved=false,wallet_paid=0,
                           payable_amount=total_amount-discount_amount
                           WHERE id=ANY($1::bigint[])""",
                        old_ids,
                    )
                product = await conn.fetchrow(
                    "SELECT * FROM products WHERE id=$1 AND active AND stock>0 FOR UPDATE",
                    product_id,
                )
                if not product:
                    raise ValueError("محصول موجود نیست.")
                discount = 0
                pending_code = await conn.fetchrow(
                    """SELECT c.* FROM pending_discounts d
                       JOIN promo_codes c ON c.id=d.code_id
                       WHERE d.user_id=$1 FOR UPDATE OF d,c""",
                    user_id,
                )
                if pending_code:
                    if (
                        pending_code["active"]
                        and pending_code["used_count"] < pending_code["max_uses"]
                        and (
                            pending_code["expires_at"] is None
                            or pending_code["expires_at"] > await conn.fetchval("SELECT now()")
                        )
                    ):
                        percent = min(99, int(pending_code["value"]))
                        discount = int(product["price"]) * percent // 100
                order = await conn.fetchrow(
                    """INSERT INTO orders(
                         user_id,total_amount,discount_amount,payable_amount,
                         player_id,promo_code,inventory_reserved
                       ) VALUES(
                         $1,$2::bigint,$3::bigint,$2::bigint-$3::bigint,$4,$5,true
                       ) RETURNING *""",
                    user_id,
                    product["price"],
                    discount,
                    player_id,
                    pending_code["code"] if pending_code and discount else "",
                )
                await conn.execute(
                    """INSERT INTO order_items(order_id,product_id,title,quantity,unit_price)
                       VALUES($1,$2,$3,1,$4)""",
                    order["id"],
                    product_id,
                    product["title"],
                    product["price"],
                )
                changed = await conn.execute(
                    """UPDATE products SET stock=stock-1
                       WHERE id=$1 AND active AND stock>0""",
                    product_id,
                )
                if not changed.endswith("1"):
                    raise ValueError("موجودی محصول تمام شده است.")
                if pending_code:
                    if discount:
                        await conn.execute(
                            """UPDATE promo_codes SET used_count=used_count+1
                               WHERE id=$1""",
                            pending_code["id"],
                        )
                        await conn.execute(
                            """INSERT INTO promo_redemptions(
                                 code_id,user_id,applied_order_id
                               ) VALUES($1,$2,$3)""",
                            pending_code["id"],
                            user_id,
                            order["id"],
                        )
                    await conn.execute("DELETE FROM pending_discounts WHERE user_id=$1", user_id)
                return order, product

    async def redeem_code(self, user_id: int, raw_code: str):
        code_value = raw_code.strip().upper()
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                await conn.fetchval(
                    "SELECT id FROM users WHERE id=$1 FOR UPDATE",
                    user_id,
                )
                code = await conn.fetchrow(
                    """SELECT * FROM promo_codes WHERE code=$1 FOR UPDATE""",
                    code_value,
                )
                now = await conn.fetchval("SELECT now()")
                if (
                    not code
                    or not code["active"]
                    or code["used_count"] >= code["max_uses"]
                    or (code["expires_at"] and code["expires_at"] <= now)
                ):
                    raise ValueError("کد نامعتبر یا منقضی است.")
                used = await conn.fetchval(
                    "SELECT 1 FROM promo_redemptions WHERE code_id=$1 AND user_id=$2",
                    code["id"],
                    user_id,
                )
                if used:
                    raise ValueError("این کد قبلاً استفاده شده است.")
                if code["code_type"] == "gift":
                    reference = f"gift:{code['id']}:{user_id}"
                    await conn.execute(
                        """INSERT INTO wallet_ledger(
                             user_id,amount,entry_type,reference
                           ) VALUES($1,$2,'gift_code',$3)""",
                        user_id,
                        code["value"],
                        reference,
                    )
                    await conn.execute(
                        "UPDATE users SET balance=balance+$1 WHERE id=$2",
                        code["value"],
                        user_id,
                    )
                    await conn.execute(
                        """INSERT INTO promo_redemptions(code_id,user_id)
                           VALUES($1,$2)""",
                        code["id"],
                        user_id,
                    )
                    await conn.execute(
                        "UPDATE promo_codes SET used_count=used_count+1 WHERE id=$1",
                        code["id"],
                    )
                    return "gift", int(code["value"])
                await conn.execute(
                    """INSERT INTO pending_discounts(user_id,code_id)
                       VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE
                       SET code_id=EXCLUDED.code_id,created_at=now()""",
                    user_id,
                    code["id"],
                )
                return "discount", min(99, int(code["value"]))

    async def create_payment(self, user_id, order_id, purpose, provider, amount, minutes):
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                if order_id is not None:
                    order = await conn.fetchrow(
                        """SELECT status,payable_amount,inventory_reserved
                           FROM orders WHERE id=$1 AND user_id=$2 FOR UPDATE""",
                        order_id,
                        user_id,
                    )
                    if (
                        not order
                        or order["status"] != "pending"
                        or not order["inventory_reserved"]
                        or order["payable_amount"] <= 0
                    ):
                        raise ValueError("سفارش قابل پرداخت نیست.")
                    protected_payment = await conn.fetchrow(
                        """SELECT p.id,p.provider,p.authority,
                                  EXISTS(
                                    SELECT 1 FROM receipts r
                                    WHERE r.payment_id=p.id AND r.status='pending'
                                  ) receipt_pending
                           FROM payments p
                           WHERE p.order_id=$1
                             AND (
                               (p.provider='gateway' AND p.authority IS NOT NULL
                                AND p.status='pending' AND p.expires_at>now())
                               OR EXISTS(
                                 SELECT 1 FROM receipts r
                                 WHERE r.payment_id=p.id AND r.status='pending'
                               )
                             )
                           ORDER BY p.id DESC LIMIT 1 FOR UPDATE OF p""",
                        order_id,
                    )
                    if protected_payment:
                        if protected_payment["receipt_pending"]:
                            raise ValueError(
                                "رسید این سفارش در انتظار بررسی است؛ "
                                "تا اعلام نتیجه روش پرداخت را عوض نکن."
                            )
                        raise ValueError(
                            "برای این سفارش لینک درگاه فعالی وجود دارد؛ "
                            "همان لینک را بررسی کن یا از «تغییر امن روش پرداخت» استفاده کن."
                        )
                    # لینک درگاهِ منقضی/لغوشده نباید سفارش را قفل کند. آن را به
                    # کیف پول جدا می‌کنیم تا اگر بعداً پرداخت شد، مبلغ به کیف پول
                    # برگردد و تحویلِ دوم برای سفارش رخ ندهد.
                    await conn.execute(
                        """UPDATE payments SET purpose='wallet',order_id=NULL
                           WHERE order_id=$1 AND provider='gateway'
                             AND authority IS NOT NULL
                             AND status IN ('expired','cancelled')""",
                        order_id,
                    )
                    amount = int(order["payable_amount"])
                    await conn.execute(
                        """UPDATE receipts SET status='rejected',reviewed_at=now()
                           WHERE payment_id IN (
                             SELECT id FROM payments
                             WHERE order_id=$1 AND status='pending'
                           ) AND status='pending'""",
                        order_id,
                    )
                    await conn.execute(
                        """UPDATE payments SET status='cancelled'
                           WHERE order_id=$1 AND status='pending'""",
                        order_id,
                    )
                else:
                    await conn.fetchval(
                        "SELECT id FROM users WHERE id=$1 FOR UPDATE",
                        user_id,
                    )
                    # If there's a live gateway payment that hasn't
                    # expired yet, let the user know — but if they're
                    # retrying, auto-cancel it so they can proceed.
                    protected_wallet_payment = await conn.fetchrow(
                        """SELECT p.id,p.provider,p.authority,
                                  EXISTS(
                                    SELECT 1 FROM receipts r
                                    WHERE r.payment_id=p.id AND r.status='pending'
                                  ) receipt_pending
                           FROM payments p
                           WHERE p.user_id=$1 AND p.purpose='wallet'
                             AND (
                               (p.provider='gateway' AND p.authority IS NOT NULL
                                AND p.status='pending' AND p.expires_at>now())
                               OR EXISTS(
                                 SELECT 1 FROM receipts r
                                 WHERE r.payment_id=p.id AND r.status='pending'
                               )
                             )
                           ORDER BY p.id DESC LIMIT 1 FOR UPDATE OF p""",
                        user_id,
                    )
                    if protected_wallet_payment:
                        if protected_wallet_payment["receipt_pending"]:
                            raise ValueError(
                                "رسید این سفارش در انتظار بررسی است و "
                                "روش پرداخت قابل تغییر نیست."
                            )
                        # Auto-cancel the stale gateway so the user can
                        # retry with a fresh link.  Refund the amount
                        # only when it is a real positive value.
                        refund_amount = int(
                            protected_wallet_payment.get("amount") or 0
                        )
                        await conn.execute(
                            """UPDATE payments SET status='cancelled'
                               WHERE id=$1""",
                            protected_wallet_payment["id"],
                        )
                        if refund_amount > 0:
                            await conn.execute(
                                """INSERT INTO wallet_ledger(
                                     user_id,amount,entry_type,reference
                                   ) VALUES($1,$2,'gateway_cancel',$3)
                                   ON CONFLICT(reference) DO NOTHING""",
                                user_id,
                                refund_amount,
                                f"auto-cancel-wallet:{protected_wallet_payment['id']}",
                            )
                            await conn.execute(
                                "UPDATE users SET balance=balance+$1 WHERE id=$2",
                                refund_amount,
                                user_id,
                            )
                    await conn.execute(
                        """UPDATE payments SET status='cancelled'
                           WHERE user_id=$1 AND purpose='wallet'
                             AND status='pending' AND authority IS NULL
                             AND NOT EXISTS (
                               SELECT 1 FROM receipts r
                               WHERE r.payment_id=payments.id AND r.status='pending'
                             )""",
                        user_id,
                    )
                return await conn.fetchrow(
                    """INSERT INTO payments(
                         order_id,user_id,purpose,provider,amount,expires_at
                       ) VALUES(
                         $1::bigint,$2::bigint,$3::text,$4::text,$5::bigint,
                         now()+make_interval(mins => $6::int)
                       )
                       RETURNING *""",
                    order_id,
                    user_id,
                    purpose,
                    provider,
                    amount,
                    minutes,
                )

    async def attach_authority(self, payment_id: int, authority: str):
        await self.pool.execute(
            "UPDATE payments SET authority=$1 WHERE id=$2 AND status='pending'",
            authority,
            payment_id,
        )

    async def active_order_gateway(self, user_id: int, order_id: int):
        return await self.pool.fetchrow(
            """SELECT p.* FROM payments p
               JOIN orders o ON o.id=p.order_id
               WHERE p.order_id=$1 AND o.user_id=$2
                 AND o.status='pending' AND o.inventory_reserved
                 AND p.provider='gateway' AND p.authority IS NOT NULL
                 AND p.status='pending' AND p.expires_at>now()
               ORDER BY p.id DESC LIMIT 1""",
            order_id,
            user_id,
        )

    async def detach_order_gateway_to_wallet(self, user_id: int, order_id: int):
        """Detach an issued link without losing a possible late payment.

        A later successful callback credits the user's wallet, so changing the
        order's payment method can never trigger a second product delivery.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                order = await conn.fetchrow(
                    """SELECT id,status,inventory_reserved FROM orders
                       WHERE id=$1 AND user_id=$2 FOR UPDATE""",
                    order_id,
                    user_id,
                )
                if (
                    not order
                    or order["status"] != "pending"
                    or not order["inventory_reserved"]
                ):
                    raise ValueError("سفارش قابل تغییر نیست.")
                pending_receipt = await conn.fetchval(
                    """SELECT 1 FROM payments p
                       JOIN receipts r ON r.payment_id=p.id
                       WHERE p.order_id=$1 AND r.status='pending' LIMIT 1""",
                    order_id,
                )
                if pending_receipt:
                    raise ValueError("رسید این سفارش در انتظار بررسی است و روش پرداخت قابل تغییر نیست.")
                payment = await conn.fetchrow(
                    """SELECT * FROM payments
                       WHERE order_id=$1 AND provider='gateway'
                         AND authority IS NOT NULL
                         AND status IN ('pending','cancelled','expired')
                       ORDER BY id DESC LIMIT 1 FOR UPDATE""",
                    order_id,
                )
                if not payment:
                    raise ValueError("لینک درگاه فعالی برای تغییر پیدا نشد.")
                await conn.execute(
                    """UPDATE payments SET purpose='wallet',order_id=NULL
                       WHERE id=$1 AND order_id=$2""",
                    payment["id"],
                    order_id,
                )
                await conn.execute(
                    "UPDATE orders SET payment_method='pending' WHERE id=$1",
                    order_id,
                )
                return payment

    async def payment_by_authority(self, authority: str):
        return await self.pool.fetchrow("SELECT * FROM payments WHERE authority=$1", authority)

    async def finalize_gateway(self, authority: str, ref_id: str):
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                # Read once without a lock so a terminal callback can be
                # handled without touching its order.  Pending order payments
                # always lock order -> payment, which is the same order used
                # by create_payment, receipt review and cancellation.
                payment = await conn.fetchrow(
                    "SELECT * FROM payments WHERE authority=$1",
                    authority,
                )
                if not payment:
                    raise ValueError("پرداخت پیدا نشد.")
                if payment["status"] == "verified":
                    payment = await conn.fetchrow(
                        "SELECT * FROM payments WHERE id=$1 FOR UPDATE",
                        payment["id"],
                    )
                    if payment["ref_id"] and str(payment["ref_id"]) != str(ref_id):
                        raise ValueError("شناسه مرجع با پرداخت تأییدشده مطابقت ندارد.")
                    return payment, False
                if (
                    payment["provider"] != "gateway"
                    or payment["status"] not in {"pending", "expired", "cancelled"}
                ):
                    raise ValueError("پرداخت نامعتبر است.")
                if payment["purpose"] == "order":
                    order_status = await conn.fetchval(
                        "SELECT status FROM orders WHERE id=$1 FOR UPDATE",
                        payment["order_id"],
                    )
                payment = await conn.fetchrow(
                    "SELECT * FROM payments WHERE id=$1 FOR UPDATE",
                    payment["id"],
                )
                # Another callback may have completed while this transaction
                # waited for the order/payment locks.  It is still a success.
                if payment["status"] == "verified":
                    if payment["ref_id"] and str(payment["ref_id"]) != str(ref_id):
                        raise ValueError("شناسه مرجع با پرداخت تأییدشده مطابقت ندارد.")
                    return payment, False
                if payment["purpose"] == "order" and order_status != "pending":
                    raise ValueError("سفارش قبلاً پرداخت یا بسته شده است.")
                await conn.execute(
                    """UPDATE payments SET status='verified',ref_id=$1,verified_at=now()
                       WHERE id=$2""",
                    ref_id,
                    payment["id"],
                )
                if payment["purpose"] == "wallet":
                    reference = f"gateway:{authority}"
                    inserted = await conn.fetchval(
                        """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                           VALUES($1,$2,'gateway_charge',$3)
                           ON CONFLICT(reference) DO NOTHING RETURNING id""",
                        payment["user_id"],
                        payment["amount"],
                        reference,
                    )
                    if inserted:
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            payment["amount"],
                            payment["user_id"],
                        )
                else:
                    await conn.execute(
                        """UPDATE receipts SET status='rejected',reviewed_at=now()
                           WHERE payment_id IN (
                             SELECT id FROM payments
                             WHERE order_id=$1 AND id<>$2
                           ) AND status='pending'""",
                        payment["order_id"],
                        payment["id"],
                    )
                    await conn.execute(
                        """UPDATE payments SET status='cancelled'
                           WHERE order_id=$1 AND id<>$2 AND status='pending'""",
                        payment["order_id"],
                        payment["id"],
                    )
                    changed = await conn.execute(
                        """UPDATE orders SET status='paid',payable_amount=0,
                           inventory_reserved=false,
                           payment_method=CASE WHEN wallet_paid>0
                             THEN 'wallet+gateway' ELSE 'gateway' END,
                           paid_at=now()
                           WHERE id=$1 AND status='pending'
                             AND inventory_reserved""",
                        payment["order_id"],
                    )
                    if not changed.endswith("1"):
                        raise ValueError("سفارش دیگر قابل پرداخت نیست.")
                return payment, True

    async def wallet_pay(self, user_id: int, order_id: int):
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                user = await conn.fetchrow("SELECT * FROM users WHERE id=$1 FOR UPDATE", user_id)
                order = await conn.fetchrow(
                    "SELECT * FROM orders WHERE id=$1 AND user_id=$2 FOR UPDATE",
                    order_id,
                    user_id,
                )
                if (
                    not order
                    or order["status"] != "pending"
                    or not order["inventory_reserved"]
                ):
                    raise ValueError("سفارش قابل پرداخت نیست.")
                if order["wallet_paid"] > 0:
                    raise ValueError("سهم کیف پول قبلاً برای این سفارش اعمال شده است.")
                protected_payment = await conn.fetchval(
                    """SELECT 1 FROM payments p
                       WHERE p.order_id=$1 AND (
                         (p.provider='gateway' AND p.authority IS NOT NULL
                          AND p.status IN ('pending','cancelled','expired'))
                         OR EXISTS (
                           SELECT 1 FROM receipts r
                           WHERE r.payment_id=p.id AND r.status='pending'
                         )
                       ) LIMIT 1""",
                    order_id,
                )
                if protected_payment:
                    raise ValueError(
                        "برای این سفارش پرداخت باز وجود دارد؛ "
                        "تا مشخص شدن نتیجه، کیف پول اعمال نمی‌شود."
                    )
                amount = min(int(user["balance"]), int(order["payable_amount"]))
                if amount <= 0:
                    raise ValueError("موجودی کیف پول صفر است.")
                attempt = await conn.fetchval(
                    """SELECT count(*)+1 FROM wallet_ledger
                       WHERE user_id=$1 AND entry_type='order_payment'
                         AND reference LIKE $2""",
                    user_id,
                    f"order:{order_id}:wallet:%",
                )
                reference = f"order:{order_id}:wallet:{attempt}"
                await conn.execute(
                    """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                       VALUES($1,$2,'order_payment',$3)""",
                    user_id,
                    -amount,
                    reference,
                )
                await conn.execute(
                    "UPDATE users SET balance=balance-$1 WHERE id=$2", amount, user_id
                )
                remaining = int(order["payable_amount"]) - amount
                if remaining == 0:
                    await conn.execute(
                        """UPDATE orders SET status='paid',wallet_paid=$1,
                           payable_amount=0,payment_method='wallet',paid_at=now(),
                           inventory_reserved=false WHERE id=$2""",
                        amount,
                        order_id,
                    )
                else:
                    await conn.execute(
                        """UPDATE orders SET wallet_paid=$1,payable_amount=$2,
                           payment_method='wallet+pending' WHERE id=$3""",
                        amount,
                        remaining,
                        order_id,
                    )
                await conn.execute(
                    """UPDATE payments SET status='cancelled'
                       WHERE order_id=$1 AND status='pending'""",
                    order_id,
                )
                await conn.execute(
                    """UPDATE receipts SET status='rejected',reviewed_at=now()
                       WHERE payment_id IN (
                         SELECT id FROM payments
                         WHERE order_id=$1 AND status='cancelled'
                       ) AND status='pending'""",
                    order_id,
                )
                return {
                    "used": amount,
                    "remaining": remaining,
                    "balance": int(user["balance"]) - amount,
                    "paid": remaining == 0,
                }

    async def cancel_order(self, user_id: int, order_id: int):
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                order = await conn.fetchrow(
                    """SELECT * FROM orders
                       WHERE id=$1 AND user_id=$2 FOR UPDATE""",
                    order_id,
                    user_id,
                )
                if not order or order["status"] != "pending":
                    raise ValueError("سفارش قابل لغو نیست.")
                external_payment = await conn.fetchval(
                    """SELECT 1 FROM payments
                       WHERE order_id=$1 AND provider='gateway'
                         AND authority IS NOT NULL
                         AND status IN ('pending','cancelled','expired')
                       LIMIT 1""",
                    order_id,
                )
                pending_receipt = await conn.fetchval(
                    """SELECT 1 FROM payments p
                       JOIN receipts r ON r.payment_id=p.id
                       WHERE p.order_id=$1 AND r.status='pending'
                       LIMIT 1""",
                    order_id,
                )
                if external_payment or pending_receipt:
                    raise ValueError(
                        "این سفارش لینک درگاه یا رسیدِ در انتظار دارد و تا تعیین "
                        "نتیجه پرداخت قابل لغو نیست."
                    )
                refunded = int(order["wallet_paid"])
                if refunded:
                    reference = f"cancel-order:{order_id}:wallet-refund"
                    inserted = await conn.fetchval(
                        """INSERT INTO wallet_ledger(
                             user_id,amount,entry_type,reference
                           ) VALUES($1,$2,'order_refund',$3)
                           ON CONFLICT(reference) DO NOTHING RETURNING id""",
                        user_id,
                        refunded,
                        reference,
                    )
                    if inserted:
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            refunded,
                            user_id,
                        )
                if order["inventory_reserved"]:
                    await conn.execute(
                        """UPDATE products p SET stock=stock+i.quantity
                           FROM order_items i
                           WHERE i.order_id=$1 AND i.product_id=p.id""",
                        order_id,
                    )
                await conn.execute(
                    """UPDATE receipts SET status='rejected',reviewed_at=now()
                       WHERE payment_id IN (
                         SELECT id FROM payments WHERE order_id=$1
                       ) AND status='pending'""",
                    order_id,
                )
                await conn.execute(
                    """UPDATE payments SET status='cancelled'
                       WHERE order_id=$1 AND status='pending'""",
                    order_id,
                )
                await conn.execute(
                    """UPDATE orders SET status='cancelled',wallet_paid=0,
                       payable_amount=total_amount-discount_amount,
                       inventory_reserved=false WHERE id=$1""",
                    order_id,
                )
                return refunded

    async def submit_receipt(
        self,
        *,
        payment_id: int,
        user_id: int,
        source_chat_id: str,
        source_message_id: str,
        file_id: str,
    ):
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                payment = await conn.fetchrow(
                    """SELECT p.*,o.status order_status
                       FROM payments p
                       LEFT JOIN orders o ON o.id=p.order_id
                       WHERE p.id=$1 AND p.user_id=$2 FOR UPDATE OF p""",
                    payment_id,
                    user_id,
                )
                now = await conn.fetchval("SELECT now()")
                if (
                    not payment
                    or payment["provider"] != "card"
                    or payment["status"] != "pending"
                    or payment["expires_at"] <= now
                    or (
                        payment["purpose"] == "order"
                        and payment["order_status"] != "pending"
                    )
                ):
                    raise ValueError("مهلت پرداخت تمام شده یا تراکنش معتبر نیست.")
                return await conn.fetchrow(
                    """INSERT INTO receipts(
                         payment_id,user_id,source_chat_id,source_message_id,file_id
                       ) VALUES($1,$2,$3,$4,$5)
                       ON CONFLICT(payment_id) DO NOTHING RETURNING id""",
                    payment_id,
                    user_id,
                    source_chat_id,
                    source_message_id,
                    file_id,
                )

    async def expire_stale_orders(self):
        expired = []
        async with self.pool.acquire() as conn:
            async with conn.transaction(isolation="serializable"):
                rows = await conn.fetch(
                    """SELECT o.*,u.chat_id FROM orders o
                       JOIN users u ON u.id=o.user_id
                       WHERE o.status='pending'
                         AND o.created_at<now()-interval '1 hour'
                         AND NOT EXISTS (
                           SELECT 1 FROM payments p
                           WHERE p.order_id=o.id AND p.provider='gateway'
                             AND p.authority IS NOT NULL
                             AND p.status IN ('pending','cancelled','expired')
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM payments p
                           JOIN receipts r ON r.payment_id=p.id
                           WHERE p.order_id=o.id AND r.status='pending'
                         )
                       FOR UPDATE OF o"""
                )
                for order in rows:
                    refunded = int(order["wallet_paid"])
                    if refunded:
                        reference = f"expire-order:{order['id']}:wallet-refund"
                        inserted = await conn.fetchval(
                            """INSERT INTO wallet_ledger(
                                 user_id,amount,entry_type,reference
                               ) VALUES($1,$2,'order_refund',$3)
                               ON CONFLICT(reference) DO NOTHING RETURNING id""",
                            order["user_id"],
                            refunded,
                            reference,
                        )
                        if inserted:
                            await conn.execute(
                                "UPDATE users SET balance=balance+$1 WHERE id=$2",
                                refunded,
                                order["user_id"],
                            )
                    if order["inventory_reserved"]:
                        await conn.execute(
                            """UPDATE products p SET stock=stock+i.quantity
                               FROM order_items i
                               WHERE i.order_id=$1 AND i.product_id=p.id""",
                            order["id"],
                        )
                    await conn.execute(
                        """UPDATE receipts SET status='rejected',reviewed_at=now()
                           WHERE payment_id IN (
                             SELECT id FROM payments WHERE order_id=$1
                           ) AND status='pending'""",
                        order["id"],
                    )
                    await conn.execute(
                        """UPDATE payments SET status='expired'
                           WHERE order_id=$1 AND status='pending'""",
                        order["id"],
                    )
                    await conn.execute(
                        """UPDATE orders SET status='expired',wallet_paid=0,
                           payable_amount=total_amount-discount_amount,
                           inventory_reserved=false WHERE id=$1""",
                        order["id"],
                    )
                    expired.append(
                        {
                            "id": order["id"],
                            "chat_id": order["chat_id"],
                            "refunded": refunded,
                        }
                    )
        return expired

    async def stats(self):
        return await self.pool.fetchrow(
            """SELECT
              (SELECT count(*) FROM users) users,
              (SELECT count(DISTINCT user_id) FROM orders WHERE paid_at IS NOT NULL) buyers,
              (SELECT coalesce(sum(balance),0) FROM users) balances,
              (SELECT count(*) FROM orders WHERE paid_at IS NOT NULL) sales,
              (SELECT coalesce(sum(total_amount-discount_amount),0)
                 FROM orders WHERE paid_at IS NOT NULL) revenue,
              (SELECT count(*) FROM users u
                 WHERE u.balance<>coalesce((
                   SELECT sum(l.amount) FROM wallet_ledger l
                   WHERE l.user_id=u.id
                 ),0)) wallet_mismatches"""
        )

    async def audit(self, admin_id, action, target="", details=""):
        await self.pool.execute(
            "INSERT INTO audit_logs(admin_id,action,target,details) VALUES($1,$2,$3,$4)",
            admin_id,
            action,
            target,
            details[:1000],
        )
