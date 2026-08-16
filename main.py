import asyncio
import html
import logging
import weakref

from aiohttp import web

from config import Settings
from database import Database
from keyboards import inline
from payment_safety import checked_decimal, supplier_cost_toman
from payments import Zarinpal
from router import Router
from rubika_api import RubikaAPI, normalize_event
from supplier import G2Bulk, g2_idempotency_key, usd_toman_rate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("atomic-rubika")


class Application:
    def __init__(self, config: Settings):
        self.config = config
        self.db = Database(config.database_url)
        self.api = RubikaAPI(config.token)
        self.router = Router(self.db, self.api, config)
        self.zarinpal = Zarinpal(settings_getter=self.db.setting)
        self.g2 = G2Bulk()
        self.tasks: list[asyncio.Task] = []
        self._update_slots = asyncio.Semaphore(20)
        self._chat_locks = weakref.WeakValueDictionary()

    async def start(self):
        await self.db.start()
        await self.api.start()
        await self.zarinpal.start()
        await self.g2.start()
        me = await self.api.get_me()
        log.info("Rubika API connected: %s", me)
        if self.config.mode == "polling":
            log.info("Starting Rubika long polling (RUBIKA_MODE=polling)")
            self.tasks.append(asyncio.create_task(self.polling_loop()))
        else:
            base, secret = self.config.callback_base, self.config.webhook_secret
            await self.api.update_endpoint(f"{base}/rubika/update/{secret}", "ReceiveUpdate")
            await self.api.update_endpoint(f"{base}/rubika/inline/{secret}", "ReceiveInlineMessage")
        self.tasks.extend(
            [
                asyncio.create_task(self.fulfillment_loop()),
                asyncio.create_task(self.cleanup_loop()),
                asyncio.create_task(self.price_sync_loop()),
            ]
        )
        # بروزرسانی فوری قیمت جم در اولین استارت، تا قیمت‌ها همیشه لحظه‌ای باشند
        self.tasks.append(asyncio.create_task(self.run_initial_price_sync()))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.g2.close()
        await self.zarinpal.close()
        await self.api.close()
        await self.db.close()

    async def process_payload_ordered(self, payload):
        """Allow parallel chats while preserving event order inside each chat."""
        event = normalize_event(payload)
        raw_update = (
            payload.get("update")
            if isinstance(payload, dict) and isinstance(payload.get("update"), dict)
            else payload
        )
        chat_id = (
            event.get("chat_id") if event
            else str(raw_update.get("chat_id") or "")
            if isinstance(raw_update, dict)
            else ""
        )
        key = chat_id or f"payload:{id(payload)}"
        lock = self._chat_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[key] = lock
        async with self._update_slots:
            async with lock:
                await self.process_payload(payload)

    async def process_payload(self, payload):
        event = normalize_event(payload)
        raw_update = (
            payload.get("update")
            if isinstance(payload, dict) and isinstance(payload.get("update"), dict)
            else payload
        )
        if (
            not event
            and isinstance(raw_update, dict)
            and raw_update.get("type") == "StartedBot"
            and raw_update.get("chat_id")
        ):
            chat_id = str(raw_update["chat_id"])
            chat_result = await self.api.get_chat(chat_id)
            data = (
                chat_result.get("data")
                if isinstance(chat_result.get("data"), dict)
                else chat_result
            )
            chat = data.get("chat") if isinstance(data.get("chat"), dict) else data
            sender_id = str(chat.get("user_id") or "")
            if sender_id:
                first = str(chat.get("first_name") or "")
                last = str(chat.get("last_name") or "")
                display_name = " ".join(part for part in (first, last) if part).strip()
                event = {
                    "event_id": f"started:{chat_id}:{sender_id}",
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "message_id": "",
                    "text": "/start",
                    "button_id": "",
                    "file": None,
                    "display_name": display_name,
                }
        if not event:
            return
        try:
            await self.router.handle(event)
        except Exception:
            log.exception("Unhandled event error")
            try:
                await self.api.send_message(
                    event["chat_id"], "⚠️ خطای موقتی رخ داد؛ دوباره تلاش کن."
                )
            except Exception:
                log.exception("Could not send error response")

    async def polling_loop(self):
        offset = None
        while True:
            try:
                response = await self.api.get_updates(offset)
                data = response.get("data") if isinstance(response.get("data"), dict) else response
                updates = data.get("updates") or []
                if updates:
                    log.info("Polling received %s update(s)", len(updates))
                    await asyncio.gather(
                        *(self.process_payload_ordered(update) for update in updates)
                    )
                offset = data.get("next_offset_id") or offset
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling failed")
                await asyncio.sleep(5)

    async def deliver_fulfillment_notifications(self):
        """Retry final supplier notifications until each recipient receives one."""
        row = await self.db.pool.fetchrow(
            """SELECT f.order_id,f.provider_order_id,f.status,
                      f.user_notified_at,f.admin_notified_at,
                      u.chat_id,u.rubika_id,p.title
               FROM fulfillments f
               JOIN orders o ON o.id=f.order_id
               JOIN users u ON u.id=o.user_id
               JOIN order_items i ON i.order_id=o.id
               JOIN products p ON p.id=i.product_id
               WHERE f.provider='g2bulk'
                 AND f.status IN ('COMPLETED','FAILED')
                 AND (
                   f.user_notified_at IS NULL
                   OR f.admin_notified_at IS NULL
                 )
               ORDER BY f.updated_at,f.order_id
               LIMIT 1"""
        )
        if not row:
            return False

        completed = row["status"] == "COMPLETED"
        if row["user_notified_at"] is None:
            if completed:
                user_text = (
                    f"✅ سفارش #{row['order_id']} با موفقیت تکمیل شد و محصول "
                    "برای اکانت شما واریز شد."
                )
            else:
                support = await self.db.get_support_contact()
                user_text = (
                    f"⚠️ تحویل خودکار سفارش #{row['order_id']} با مشکل روبه‌رو شد. "
                    "پرداخت شما ثبت است و سفارش برای بررسی ایمن به پشتیبانی ارجاع شد.\n"
                    f"آیدی پشتیبانی: {support['handle']}"
                )
            try:
                if row["chat_id"]:
                    await self.api.send_message(row["chat_id"], user_text)
                await self.db.pool.execute(
                    """UPDATE fulfillments
                       SET user_notified_at=COALESCE(user_notified_at,now())
                       WHERE order_id=$1 AND status=$2""",
                    row["order_id"],
                    row["status"],
                )
            except Exception:
                log.exception(
                    "Could not notify Rubika user for supplier order %s",
                    row["order_id"],
                )
                await self.db.pool.execute(
                    "UPDATE fulfillments SET updated_at=now() WHERE order_id=$1",
                    row["order_id"],
                )

        if row["admin_notified_at"] is None:
            if completed:
                admin_text = (
                    f"✅ سفارش #{row['order_id']} در G2Bulk تکمیل شد.\n"
                    f"محصول: {row['title']}\n"
                    f"کاربر: {row['rubika_id']}\n"
                    f"شناسه G2Bulk: {row['provider_order_id'] or '-'}"
                )
            else:
                admin_text = (
                    f"⚠️ تحویل سفارش #{row['order_id']} در G2Bulk ناموفق اعلام شد.\n"
                    f"محصول: {row['title']}\n"
                    f"کاربر: {row['rubika_id']}\n"
                    f"شناسه G2Bulk: {row['provider_order_id'] or '-'}\n"
                    "قبل از هر تلاش دستی، وضعیت سفارش تأمین‌کننده را بررسی کن."
                )
            try:
                await self.api.send_message(
                    self.config.admin_chat_id,
                    admin_text,
                )
                await self.db.pool.execute(
                    """UPDATE fulfillments
                       SET admin_notified_at=COALESCE(admin_notified_at,now())
                       WHERE order_id=$1 AND status=$2""",
                    row["order_id"],
                    row["status"],
                )
            except Exception:
                log.exception(
                    "Could not notify Rubika admin for supplier order %s",
                    row["order_id"],
                )
                await self.db.pool.execute(
                    "UPDATE fulfillments SET updated_at=now() WHERE order_id=$1",
                    row["order_id"],
                )
        return True

    async def fulfillment_loop(self):
        while True:
            try:
                if await self.deliver_fulfillment_notifications():
                    await asyncio.sleep(1)
                    continue
                manual = await self.db.pool.fetchrow(
                    """SELECT o.id,u.chat_id,u.rubika_id,p.title,p.kind,
                              f.id fulfillment_id
                       FROM orders o
                       JOIN order_items i ON i.order_id=o.id
                       JOIN products p ON p.id=i.product_id
                       JOIN users u ON u.id=o.user_id
                       LEFT JOIN fulfillments f ON f.order_id=o.id
                       WHERE o.status='paid' AND p.kind<>'gem'
                         AND p.kind<>'gem_credentials'
                         AND (f.id IS NULL OR f.status='WAITING_NOTIFY')
                       ORDER BY o.id LIMIT 1"""
                )
                if manual:
                    claimed = await self.db.pool.fetchval(
                        """UPDATE fulfillments SET status='NOTIFYING', updated_at=now()
                           WHERE order_id=$1 AND status='WAITING_NOTIFY'
                           RETURNING id""",
                        manual["id"],
                    )
                    if not claimed:
                        claimed = await self.db.pool.fetchval(
                            """INSERT INTO fulfillments(
                                 order_id,provider,idempotency_key,status,attempts
                               ) VALUES($1,'manual',$2,'NOTIFYING',1)
                               ON CONFLICT(order_id) DO NOTHING RETURNING id""",
                            manual["id"],
                            f"manual:{manual['id']}",
                        )
                    if claimed:
                        await self.api.send_message(
                            self.config.admin_chat_id,
                            f"📦 سفارش آماده تحویل #{manual['id']}\n"
                            f"محصول: {manual['title']}\n"
                            f"نوع: {manual['kind']}\n"
                            f"کاربر: {manual['rubika_id']}\n\n"
                            "پس از ارسال پک/محصول برای کاربر، تکمیل را بزن.",
                            inline_keypad=inline(
                                [
                                    [
                                        (
                                            f"order_complete:{manual['id']}",
                                            "✅ تحویل شد",
                                        )
                                    ]
                                ]
                            ),
                        )
                        await self.api.send_message(
                            manual["chat_id"],
                            f"⏳ پرداخت سفارش #{manual['id']} تأیید شد و "
                            "برای تحویل به پشتیبانی ارسال شد.",
                        )
                        await self.db.pool.execute(
                            """UPDATE fulfillments SET status='WAITING_ADMIN',
                               updated_at=now() WHERE id=$1 AND status='NOTIFYING'""",
                            claimed,
                        )
                        await self.db.pool.execute(
                            """UPDATE orders SET status='processing'
                               WHERE id=$1 AND status='paid'""",
                            manual["id"],
                        )
                    continue
                credential = await self.db.pool.fetchrow(
                    """SELECT o.id,u.chat_id,u.rubika_id,p.title,
                              f.id fulfillment_id
                       FROM orders o
                       JOIN order_items i ON i.order_id=o.id
                       JOIN products p ON p.id=i.product_id
                       JOIN users u ON u.id=o.user_id
                       JOIN credential_orders c ON c.order_id=o.id
                       LEFT JOIN fulfillments f ON f.order_id=o.id
                       WHERE o.status='paid' AND p.kind='gem_credentials'
                         AND (f.id IS NULL OR f.status='WAITING_NOTIFY')
                       ORDER BY o.id LIMIT 1"""
                )
                if credential:
                    claimed = await self.db.pool.fetchval(
                        """UPDATE fulfillments SET status='NOTIFYING', updated_at=now()
                           WHERE order_id=$1 AND status='WAITING_NOTIFY'
                           RETURNING id""",
                        credential["id"],
                    )
                    if not claimed:
                        claimed = await self.db.pool.fetchval(
                            """INSERT INTO fulfillments(
                                 order_id,provider,idempotency_key,status,attempts
                               ) VALUES($1,'credential',$2,'NOTIFYING',1)
                               ON CONFLICT(order_id) DO NOTHING RETURNING id""",
                            credential["id"],
                            f"credential:{credential['id']}",
                        )
                    if claimed:
                        await self.router.notify_credential_paid(credential["id"])
                        await self.api.send_message(
                            credential["chat_id"],
                            f"⏳ پرداخت سفارش #{credential['id']} تأیید شد.\n"
                            "پشتیبان جم با اطلاعات به‌زودی اکانت را بررسی می‌کند.",
                        )
                        try:
                            await self.router.send_user_post_pay_credential_help(
                                credential["chat_id"], credential["id"]
                            )
                        except Exception:
                            log.exception(
                                "credential post-pay help failed for %s",
                                credential["id"],
                            )
                        await self.db.pool.execute(
                            """UPDATE fulfillments SET status='WAITING_ADMIN',
                               updated_at=now() WHERE id=$1 AND status='NOTIFYING'""",
                            claimed,
                        )
                        await self.db.pool.execute(
                            """UPDATE orders SET status='processing'
                               WHERE id=$1 AND status='paid'""",
                            credential["id"],
                        )
                    continue
                pending = await self.db.pool.fetchrow(
                    """SELECT f.order_id,f.provider_order_id,u.chat_id
                       FROM fulfillments f
                       JOIN orders o ON o.id=f.order_id
                       JOIN users u ON u.id=o.user_id
                       WHERE f.status IN ('PENDING','PROCESSING')
                         AND f.provider_order_id IS NOT NULL
                       ORDER BY f.updated_at LIMIT 1"""
                )
                if pending:
                    result = await self.g2.status(pending["provider_order_id"])
                    if result.get("ok"):
                        status = result["status"]
                        await self.db.pool.execute(
                            """UPDATE fulfillments SET status=$1,updated_at=now()
                               WHERE order_id=$2""",
                            status,
                            pending["order_id"],
                        )
                        if status == "COMPLETED":
                            await self.db.pool.execute(
                                "UPDATE orders SET status='completed' WHERE id=$1",
                                pending["order_id"],
                            )
                        elif status == "FAILED":
                            await self.db.pool.execute(
                                "UPDATE orders SET status='delivery_failed' WHERE id=$1",
                                pending["order_id"],
                            )
                            await self._refund_failed_order(pending["order_id"])
                    await asyncio.sleep(3)
                    continue
                unknown = await self.db.pool.fetchrow(
                    """SELECT f.order_id,u.chat_id
                       FROM fulfillments f
                       JOIN orders o ON o.id=f.order_id
                       JOIN users u ON u.id=o.user_id
                       WHERE f.status IN ('SUBMITTING','SUBMIT_UNKNOWN')
                         AND f.provider_order_id IS NULL
                         AND f.updated_at<=now()-interval '30 seconds'
                       ORDER BY f.updated_at LIMIT 1"""
                )
                if unknown:
                    recovered = await self.g2.find_order_by_remark(
                        f"rubika-{unknown['order_id']}"
                    )
                    if recovered.get("found"):
                        provider_status = recovered["status"]
                        if provider_status == "COMPLETED":
                            order_status = "completed"
                        elif provider_status == "FAILED":
                            order_status = "delivery_failed"
                        else:
                            order_status = "processing"
                        async with self.db.pool.acquire() as conn:
                            async with conn.transaction():
                                await conn.execute(
                                    """UPDATE fulfillments
                                       SET provider_order_id=$1,status=$2,
                                           error=NULL,updated_at=now()
                                       WHERE order_id=$3
                                         AND status IN ('SUBMITTING','SUBMIT_UNKNOWN')""",
                                    recovered["provider_order_id"],
                                    provider_status,
                                    unknown["order_id"],
                                )
                                await conn.execute(
                                    """UPDATE orders SET status=$1,player_name=$2
                                       WHERE id=$3""",
                                    order_status,
                                    recovered.get("player_name") or "",
                                    unknown["order_id"],
                                )
                        if provider_status == "FAILED":
                            await self._refund_failed_order(unknown["order_id"])
                    else:
                        await self.db.pool.execute(
                            """UPDATE fulfillments
                               SET status='SUBMIT_UNKNOWN',updated_at=now()
                               WHERE order_id=$1
                                 AND status IN ('SUBMITTING','SUBMIT_UNKNOWN')""",
                            unknown["order_id"],
                        )
                    await asyncio.sleep(3)
                    continue
                row = await self.db.pool.fetchrow(
                    """SELECT o.id,o.player_id,
                              o.total_amount-o.discount_amount sale_toman,
                              p.supplier_sku,p.supplier_cost_usd,u.chat_id,
                              f.id fulfillment_id
                       FROM orders o
                       JOIN order_items i ON i.order_id=o.id
                       JOIN products p ON p.id=i.product_id
                       JOIN users u ON u.id=o.user_id
                       LEFT JOIN fulfillments f ON f.order_id=o.id
                       WHERE o.status='paid' AND p.kind='gem'
                         AND f.id IS NULL
                       ORDER BY o.id LIMIT 1"""
                )
                if not row:
                    await asyncio.sleep(5)
                    continue
                idem = g2_idempotency_key(row["id"])
                claimed = await self.db.pool.fetchval(
                    """INSERT INTO fulfillments(
                         order_id,provider,idempotency_key,status,attempts
                       ) VALUES($1,'g2bulk',$2,'SUBMITTING',1)
                       ON CONFLICT(order_id) DO NOTHING RETURNING id""",
                    row["id"],
                    idem,
                )
                if not claimed:
                    continue
                result = await self.g2.order(row["supplier_sku"], row["player_id"], row["id"])
                if result.get("ok"):
                    provider_status = result["status"]
                    order_status = "completed" if provider_status == "COMPLETED" else "processing"
                    async with self.db.pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute(
                                """UPDATE fulfillments SET provider_order_id=$1,
                                   status=$2,updated_at=now() WHERE order_id=$3""",
                                result["provider_order_id"],
                                provider_status,
                                row["id"],
                            )
                            await conn.execute(
                                """UPDATE orders SET status=$1,player_name=$2
                                   WHERE id=$3""",
                                order_status,
                                result["player_name"],
                                row["id"],
                            )
                    rate = await usd_toman_rate(
                        await self.db.setting("usd_toman_rate", "")
                    )
                    cost_usd = result.get("cost_usd") or row["supplier_cost_usd"]
                    if rate.get("ok") and cost_usd:
                        safe_cost_usd = checked_decimal(
                            cost_usd, label="هزینه دلاری تأمین‌کننده"
                        )
                        cost_toman = supplier_cost_toman(
                            safe_cost_usd, rate["rate"]
                        )
                        await self.db.pool.execute(
                            """INSERT INTO profit_snapshots(
                                 order_id,sale_toman,supplier_cost_usd,usd_toman_rate,
                                 supplier_cost_toman,gross_profit_toman,fx_source
                               ) VALUES($1,$2,$3,$4,$5,$6,$7)
                               ON CONFLICT(order_id) DO NOTHING""",
                            row["id"],
                            row["sale_toman"],
                            safe_cost_usd,
                            rate["rate"],
                            cost_toman,
                            row["sale_toman"] - cost_toman,
                            rate["source"],
                        )
                    if provider_status != "COMPLETED":
                        await self.api.send_message(
                            row["chat_id"],
                            f"⏳ سفارش #{row['id']} برای تحویل ارسال شد.",
                        )
                else:
                    uncertain = bool(result.get("uncertain"))
                    fulfillment_status = (
                        "SUBMIT_UNKNOWN" if uncertain else "REJECTED"
                    )
                    await self.db.pool.execute(
                        """UPDATE fulfillments SET status=$1,error=$2,
                           next_retry_at=NULL,
                           updated_at=now() WHERE order_id=$3""",
                        fulfillment_status,
                        result.get("error", "")[:1000],
                        row["id"],
                    )
                    if uncertain:
                        await self.db.pool.execute(
                            """UPDATE orders SET status='processing'
                               WHERE id=$1 AND status='paid'""",
                            row["id"],
                        )
                        await self.api.send_message(
                            self.config.admin_chat_id,
                            f"🚨 وضعیت ثبت سفارش #{row['id']} در G2Bulk نامشخص است.\n"
                            "ارسال مجدد خودکار متوقف شد؛ ابتدا سفارش‌های G2Bulk را تطبیق بده.\n"
                            f"خطا: {result.get('error')}",
                        )
                    else:
                        await self.db.pool.execute(
                            """UPDATE orders SET status='delivery_failed'
                               WHERE id=$1 AND status='paid'""",
                            row["id"],
                        )
                        # برگرداندن پول به کیف پول کاربر برای سفارش‌های failed
                        await self._refund_failed_order(row["id"])
                        await self.api.send_message(
                            self.config.admin_chat_id,
                            f"⚠️ تأمین‌کننده سفارش #{row['id']} را قطعی رد کرد:\n"
                            f"{result.get('error')}\n"
                            f"مبلغ به کیف پول کاربر برگردانده شد.",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Fulfillment worker failed")
                await asyncio.sleep(5)

    async def cleanup_loop(self):
        while True:
            try:
                await self.reconcile_gateway_payments()
                await self.db.pool.execute(
                    "DELETE FROM processed_events WHERE created_at<now()-interval '7 days'"
                )
                await self.db.pool.execute(
                    """UPDATE payments SET status='expired'
                       WHERE status='pending' AND expires_at<now()
                         AND NOT EXISTS (
                           SELECT 1 FROM receipts r
                           WHERE r.payment_id=payments.id
                             AND r.status='pending'
                         )"""
                )
                await self.db.pool.execute(
                    """UPDATE payments SET purpose='wallet',order_id=NULL
                       WHERE provider='gateway' AND authority IS NOT NULL
                         AND status IN ('expired','cancelled','rejected')
                         AND purpose='order' AND order_id IS NOT NULL"""
                )
                await self.db.pool.execute(
                    """UPDATE receipts r SET status='rejected',reviewed_at=now()
                       FROM payments p WHERE p.id=r.payment_id
                       AND p.status='expired' AND r.status='pending'"""
                )
                expired_orders = await self.db.expire_stale_orders()
                for order in expired_orders:
                    text = f"⏳ مهلت سفارش #{order['id']} تمام شد و سفارش لغو شد."
                    if order["refunded"]:
                        text += (
                            f"\n💰 {order['refunded']:,} تومان به کیف پولت برگشت."
                        )
                    await self.api.send_message(order["chat_id"], text)
                await self.db.pool.execute(
                    """UPDATE sessions SET state='',data='{}'::jsonb,updated_at=now()
                       WHERE state<>'' AND updated_at<now()-interval '2 hours'"""
                )
                await asyncio.sleep(5 * 60)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Cleanup failed")
                await asyncio.sleep(60)

    async def price_sync_loop(self):
        """به‌روزرسانی خودکار قیمت بسته‌های جم و جم با اطلاعات هر ۲۴ ساعت.

        جم با آیدی از کاتالوگ G2Bulk (سود gem_profit_percent) و جم با اطلاعات
        از بهای دلاری پنل با سود مستقل ۴۰٪ (هفتگی/ماهانه) محاسبه می‌شوند.
        """
        while True:
            try:
                last = await self.db.setting_timestamp("gem_price_last_sync")
                now = await self.db.pool.fetchval("SELECT now()")
                if last is not None:
                    elapsed_hours = (now - last).total_seconds() / 3600
                    if elapsed_hours < 24:
                        await asyncio.sleep(max(60, int((24 - elapsed_hours) * 3600)))
                        continue
                await self.run_gem_price_sync()
                await asyncio.sleep(24 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Price sync failed")
                await asyncio.sleep(5 * 60)

    async def run_initial_price_sync(self):
        """اجرای فوری بروزرسانی قیمت در اولین استارت ربات (بدون انتظار ۲۴ ساعت)."""
        try:
            await self.run_gem_price_sync()
        except Exception:
            log.exception("Initial price sync failed")

    async def run_credential_price_sync(self):
        """فقط جم با اطلاعات — بدون G2Bulk."""
        manual = await self.db.setting("usd_toman_rate", "")
        rate = await usd_toman_rate(manual, force=True)
        if not rate.get("ok"):
            log.warning("Credential price sync skipped: %s", rate.get("error"))
            return {"ok": False, "error": rate.get("error")}
        if rate.get("source") == "nobitex_usdtirt_best_ask":
            await self.db.set_setting("usd_toman_rate", str(rate["rate"]))
        cred_cfg = await self.db.get_credential_pricing_config()
        cred_updated = await self.db.sync_credential_prices(rate["rate"], force=False)
        await self.db.touch_credential_price_last_sync()
        log.info(
            "Credential price sync: updated=%d rate=%s weekly=$%s monthly=$%s",
            cred_updated,
            rate["rate"],
            cred_cfg["weekly_cost"],
            cred_cfg["monthly_cost"],
        )
        return {"ok": True, "updated": cred_updated, "rate": rate["rate"]}

    async def run_gem_price_sync(self):
        """جم با آیدی از G2Bulk + جم با اطلاعات از $ ثابت (دو مسیر جدا)."""
        cred_result = await self.run_credential_price_sync()
        manual = await self.db.setting("usd_toman_rate", "")
        rate = await usd_toman_rate(manual, force=True)
        if not rate.get("ok"):
            if cred_result.get("ok"):
                return {
                    "ok": True,
                    "updated": cred_result.get("updated", 0),
                    "gem_updated": 0,
                    "cred_updated": cred_result.get("updated", 0),
                    "rate": cred_result.get("rate"),
                    "source": "credential_only",
                }
            log.warning("Price sync skipped: %s", rate.get("error"))
            return {"ok": False, "error": rate.get("error")}
        if rate.get("source") == "nobitex_usdtirt_best_ask":
            await self.db.set_setting("usd_toman_rate", str(rate["rate"]))
        gem_updated = 0
        catalogue = await self.g2.catalogue()
        if catalogue.get("ok"):
            profit_percent = await self.gem_profit_percent()
            gem_updated = await self.db.sync_gem_prices_from_catalogue(
                items=catalogue["items"],
                rate_value=rate["rate"],
                profit_percent=profit_percent,
            )
        else:
            log.warning(
                "Gem catalogue sync skipped: %s",
                catalogue.get("error"),
            )
        cred_updated = cred_result.get("updated", 0) if cred_result.get("ok") else 0
        cred_cfg = await self.db.get_credential_pricing_config()
        await self.db.touch_price_last_sync()
        total_updated = gem_updated + cred_updated
        log.info(
            "Price sync done: gem=%d cred=%d total=%d rate=%s "
            "id_profit=%d%% weekly_profit=%d%% monthly_profit=%d%% source=%s",
            gem_updated,
            cred_updated,
            total_updated,
            rate["rate"],
            await self.gem_profit_percent(),
            cred_cfg["weekly_profit"],
            cred_cfg["monthly_profit"],
            rate["source"],
        )
        return {
            "ok": True,
            "updated": total_updated,
            "gem_updated": gem_updated,
            "cred_updated": cred_updated,
            "rate": rate["rate"],
            "source": rate["source"],
            "profit_percent": await self.gem_profit_percent(),
            "weekly_profit": cred_cfg["weekly_profit"],
            "monthly_profit": cred_cfg["monthly_profit"],
            "weekly_cost": str(cred_cfg["weekly_cost"]),
            "monthly_cost": str(cred_cfg["monthly_cost"]),
        }

    async def gem_profit_percent(self) -> int:
        """درصد سود بسته‌های جم؛ از تنظیم دیتابیس خوانده می‌شود (پیش‌فرض ۷)."""
        raw = await self.db.setting("gem_profit_percent", "10")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 10
        return max(1, min(200, value))

    async def reconcile_gateway_payments(self):
        rows = await self.db.pool.fetch(
            """SELECT id,authority,amount,verify_attempts,status,purpose,order_id
               FROM payments
               WHERE provider='gateway' AND authority IS NOT NULL
                 AND status IN ('pending','expired','cancelled','rejected')
                 AND expires_at<now()
                 AND created_at>now()-interval '24 hours'
                 AND verify_attempts<6
                 AND (
                   last_checked_at IS NULL
                   OR last_checked_at<now()-interval '30 minutes'
                 )
               ORDER BY id LIMIT 100"""
        )
        for row in rows:
            verify_status, ref_id = await self.zarinpal.verify(
                row["amount"],
                row["authority"],
            )
            if verify_status == "not_paid":
                attempts = int(row["verify_attempts"] or 0) + 1
                await self.db.pool.execute(
                    """UPDATE payments SET verify_attempts=verify_attempts+1,
                       last_checked_at=now() WHERE id=$1""",
                    row["id"],
                )
                # لینک درگاهِ منقضی که در درگاه پرداخت نشده، نباید سفارش را
                # قفل کند؛ پس از چند بار اطمینان، detach کن یا لغو کن.
                if attempts >= 3 or (
                    attempts >= 2 and row["status"] == "expired"
                ):
                    if row["purpose"] == "order" and row["order_id"]:
                        await self.db.pool.execute(
                            """UPDATE payments SET purpose='wallet',order_id=NULL,
                               status='cancelled'
                               WHERE id=$1 AND status IN
                               ('pending','expired','cancelled','rejected')""",
                            row["id"],
                        )
                    else:
                        await self.db.pool.execute(
                            """UPDATE payments SET status='cancelled'
                               WHERE id=$1 AND status IN
                               ('pending','expired','cancelled','rejected')""",
                            row["id"],
                        )
                continue
            if verify_status != "verified":
                await self.db.pool.execute(
                    "UPDATE payments SET last_checked_at=now() WHERE id=$1",
                    row["id"],
                )
                continue
            try:
                payment, changed = await self.db.finalize_gateway(
                    row["authority"],
                    ref_id,
                )
            except ValueError:
                log.exception("Verified payment could not be finalized: %s", row["id"])
                await self.api.send_message(
                    self.config.admin_chat_id,
                    f"⚠️ پرداخت زرین‌پال #{row['id']} در درگاه تأیید شده "
                    "ولی سفارش محلی قابل نهایی‌سازی نیست؛ فوری بررسی شود.",
                )
                await self.db.pool.execute(
                    "UPDATE payments SET last_checked_at=now() WHERE id=$1",
                    row["id"],
                )
                continue
            await self.db.pool.execute(
                "UPDATE payments SET last_checked_at=now() WHERE id=$1",
                row["id"],
            )
            if changed:
                user = await self.db.pool.fetchrow(
                    "SELECT chat_id FROM users WHERE id=$1",
                    payment["user_id"],
                )
                await self.api.send_message(
                    user["chat_id"],
                    f"✅ پرداخت {payment['amount']:,} تومان پس از بررسی مجدد ثبت شد.\n"
                    f"کد پیگیری: {ref_id}\n"
                    f"سفارش: #{payment.get('order_id') or '—'} | "
                    f"مبلغ: {payment.get('amount') or payment.get('amount', 0):,} تومان",
                )


    async def _refund_failed_order(self, order_id: int):
        """برگرداندن مبلغ سفارش رد شده به کیف پول کاربر."""
        try:
            async with self.db.pool.acquire() as conn:
                async with conn.transaction():
                    # وضعیت سفارش را بگیر
                    order = await conn.fetchrow(
                        """SELECT o.id, o.user_id, o.total_amount, o.discount_amount,
                              o.wallet_paid, o.payable_amount, o.status,
                              o.inventory_reserved
                           FROM orders o WHERE o.id=$1 FOR UPDATE""",
                        order_id,
                    )
                    if not order or order["status"] != "delivery_failed":
                        return
                    # کل پرداخت شده = سهم کیف پول + هر پرداخت تأییدشده (درگاه/کارت)
                    verified_total = await conn.fetchval(
                        """SELECT COALESCE(SUM(amount),0) FROM payments
                           WHERE order_id=$1 AND status='verified'
                             AND provider IN ('gateway','card')""",
                        order_id,
                    )
                    total_paid = int(order["wallet_paid"] or 0) + int(
                        verified_total or 0
                    )
                    if total_paid <= 0:
                        return
                    if order["inventory_reserved"]:
                        await conn.execute(
                            """UPDATE products p SET stock=stock+i.quantity
                               FROM order_items i
                               WHERE i.order_id=$1 AND i.product_id=p.id""",
                            order_id,
                        )
                        await conn.execute(
                            """UPDATE orders SET inventory_reserved=false
                               WHERE id=$1""",
                            order_id,
                        )
                    # مبلغ را به کیف پول برگردان
                    reference = f"delivery_failed_refund:{order_id}"
                    inserted = await conn.fetchval(
                        """INSERT INTO wallet_ledger(user_id,amount,entry_type,reference)
                           VALUES($1,$2,'delivery_refund',$3)
                           ON CONFLICT(reference) DO NOTHING RETURNING id""",
                        order["user_id"],
                        total_paid,
                        reference,
                    )
                    if inserted:
                        await conn.execute(
                            "UPDATE users SET balance=balance+$1 WHERE id=$2",
                            total_paid,
                            order["user_id"],
                        )
                        await self.api.send_message(
                            await conn.fetchval(
                                "SELECT chat_id FROM users WHERE id=$1", order["user_id"]
                            ),
                            f"⚠️ سفارش #{order_id} به دلیل عدم موجودی در تأمین‌کننده رد شد.\n"
                            f"💰 مبلغ {total_paid:,} تومان به کیف پولت برگشت.",
                        )
        except Exception as e:
            log.exception("Failed to refund failed order %s: %s", order_id, e)


async def webhook(request):
    app_core: Application = request.app["core"]
    if request.match_info["secret"] != app_core.config.webhook_secret:
        raise web.HTTPNotFound()
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        raise web.HTTPBadRequest() from None
    asyncio.create_task(app_core.process_payload_ordered(payload))
    return web.json_response({"status": "ok"})


async def payment_callback(request):
    core: Application = request.app["core"]
    query = dict(request.query)
    try:
        body = await request.json()
        if isinstance(body, dict):
            query.update(body)
    except (TypeError, ValueError):
        pass
    authority = (query.get("Authority") or query.get("authority") or "").strip()
    callback_status = (
        (query.get("Status") or query.get("status") or "").strip().upper()
    )
    title, message = "پرداخت ناموفق", "پرداخت انجام نشد."
    payment = await core.db.payment_by_authority(authority) if authority else None
    if payment and payment["status"] == "verified":
        title, message = "پرداخت موفق", "این پرداخت قبلاً ثبت شده است."
    elif (
        payment
        and payment["status"] in {"pending", "expired", "cancelled", "rejected"}
        and callback_status == "OK"
    ):
        verify_status, ref_id = await core.zarinpal.verify(payment["amount"], authority)
        if verify_status == "verified":
            try:
                finalized, changed = await core.db.finalize_gateway(authority, ref_id)
            except ValueError as exc:
                title, message = "پرداخت قابل ثبت نیست", str(exc)
                await core.api.send_message(
                    core.config.admin_chat_id,
                    f"⚠️ زرین‌پال پرداخت #{payment['id']} به مبلغ "
                    f"{payment['amount']:,} تومان را تأیید کرد، اما ثبت محلی "
                    f"انجام نشد: {exc}\nکد پیگیری: {ref_id}",
                )
            else:
                if changed:
                    user = await core.db.pool.fetchrow(
                        "SELECT chat_id FROM users WHERE id=$1",
                        finalized["user_id"],
                    )
                    await core.api.send_message(
                        user["chat_id"],
                        f"✅ پرداخت {finalized['amount']:,} تومان ثبت شد.\n"
                        f"کد پیگیری: {ref_id}",
                    )
                title, message = "پرداخت موفق", f"کد پیگیری: {ref_id}"
        elif verify_status == "unavailable":
            title = "در حال بررسی"
            message = "ارتباط با درگاه برقرار نشد؛ کمی بعد دوباره صفحه را باز کن."
    body = f"""<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{html.escape(title)}</title><style>
    body{{font-family:sans-serif;background:#0f172a;color:#fff;display:grid;
    place-items:center;min-height:100vh;margin:0}}main{{background:#1e293b;padding:32px;
    border-radius:20px;max-width:520px;text-align:center}}</style>
    <main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>
    <p>حالا می‌توانی به ربات روبیکا برگردی.</p></main></html>"""
    return web.Response(text=body, content_type="text/html")


async def health(_):
    return web.json_response({"status": "ok"})


async def ready(request):
    core: Application = request.app["core"]
    await core.db.pool.fetchval("SELECT 1")
    return web.json_response({"status": "ready", "mode": core.config.mode})


def build_web_app(config=None):
    config = config or Settings.load()
    core = Application(config)
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app["core"] = core
    app.router.add_post("/rubika/update/{secret}", webhook)
    app.router.add_post("/rubika/inline/{secret}", webhook)
    app.router.add_get("/payment/callback", payment_callback)
    app.router.add_post("/payment/callback", payment_callback)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.on_startup.append(lambda _: core.start())
    app.on_cleanup.append(lambda _: core.close())
    return app


if __name__ == "__main__":
    settings = Settings.load()
    web.run_app(build_web_app(settings), host="0.0.0.0", port=settings.web_port)
