import asyncio
import html
import logging

from aiohttp import web

from config import Settings
from database import Database
from keyboards import inline
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
        self.zarinpal = Zarinpal()
        self.g2 = G2Bulk()
        self.tasks: list[asyncio.Task] = []

    async def start(self):
        await self.db.start()
        await self.api.start()
        me = await self.api.get_me()
        log.info("Rubika API connected: %s", me)
        if self.config.mode == "polling":
            self.tasks.append(asyncio.create_task(self.polling_loop()))
        else:
            base, secret = self.config.callback_base, self.config.webhook_secret
            await self.api.update_endpoint(f"{base}/rubika/update/{secret}", "ReceiveUpdate")
            await self.api.update_endpoint(f"{base}/rubika/inline/{secret}", "ReceiveInlineMessage")
        self.tasks.extend(
            [
                asyncio.create_task(self.fulfillment_loop()),
                asyncio.create_task(self.cleanup_loop()),
            ]
        )

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.api.close()
        await self.db.close()

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
                event = {
                    "event_id": f"started:{chat_id}:{sender_id}",
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "message_id": "",
                    "text": "/start",
                    "button_id": "",
                    "file": None,
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
                for update in data.get("updates") or []:
                    await self.process_payload(update)
                offset = data.get("next_offset_id") or offset
                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling failed")
                await asyncio.sleep(5)

    async def fulfillment_loop(self):
        while True:
            try:
                manual = await self.db.pool.fetchrow(
                    """SELECT o.id,u.chat_id,u.rubika_id,p.title,p.kind,
                              f.id fulfillment_id
                       FROM orders o
                       JOIN order_items i ON i.order_id=o.id
                       JOIN products p ON p.id=i.product_id
                       JOIN users u ON u.id=o.user_id
                       LEFT JOIN fulfillments f ON f.order_id=o.id
                       WHERE o.status='paid' AND p.kind<>'gem'
                         AND (f.id IS NULL OR f.status='WAITING_NOTIFY')
                       ORDER BY o.id LIMIT 1"""
                )
                if manual:
                    if manual["fulfillment_id"]:
                        claimed = manual["fulfillment_id"]
                    else:
                        claimed = await self.db.pool.fetchval(
                            """INSERT INTO fulfillments(
                                 order_id,provider,idempotency_key,status,attempts
                               ) VALUES($1,'manual',$2,'WAITING_NOTIFY',1)
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
                               updated_at=now() WHERE order_id=$1""",
                            manual["id"],
                        )
                        await self.db.pool.execute(
                            """UPDATE orders SET status='processing'
                               WHERE id=$1 AND status='paid'""",
                            manual["id"],
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
                            await self.api.send_message(
                                pending["chat_id"],
                                f"✅ سفارش #{pending['order_id']} با موفقیت انجام شد.",
                            )
                        elif status == "FAILED":
                            await self.db.pool.execute(
                                "UPDATE orders SET status='delivery_failed' WHERE id=$1",
                                pending["order_id"],
                            )
                            await self.api.send_message(
                                self.config.admin_chat_id,
                                f"⚠️ تحویل سفارش #{pending['order_id']} ناموفق شد.",
                            )
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
                        if provider_status == "COMPLETED":
                            await self.api.send_message(
                                unknown["chat_id"],
                                f"✅ سفارش #{unknown['order_id']} با موفقیت انجام شد.",
                            )
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
                        cost_toman = round(float(cost_usd) * rate["rate"])
                        await self.db.pool.execute(
                            """INSERT INTO profit_snapshots(
                                 order_id,sale_toman,supplier_cost_usd,usd_toman_rate,
                                 supplier_cost_toman,gross_profit_toman,fx_source
                               ) VALUES($1,$2,$3,$4,$5,$6,$7)
                               ON CONFLICT(order_id) DO NOTHING""",
                            row["id"],
                            row["sale_toman"],
                            float(cost_usd),
                            rate["rate"],
                            cost_toman,
                            row["sale_toman"] - cost_toman,
                            rate["source"],
                        )
                    if provider_status == "COMPLETED":
                        await self.api.send_message(
                            row["chat_id"],
                            f"✅ سفارش #{row['id']} با موفقیت انجام شد.",
                        )
                    else:
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
                        await self.api.send_message(
                            self.config.admin_chat_id,
                            f"⚠️ تأمین‌کننده سفارش #{row['id']} را قطعی رد کرد:\n"
                            f"{result.get('error')}",
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
                       WHERE status='pending' AND expires_at<now()"""
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

    async def reconcile_gateway_payments(self):
        rows = await self.db.pool.fetch(
            """SELECT id,authority,amount FROM payments
               WHERE provider='gateway' AND authority IS NOT NULL
                 AND status IN ('pending','expired','cancelled')
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
            await self.db.pool.execute(
                """UPDATE payments SET verify_attempts=verify_attempts+1,
                   last_checked_at=now() WHERE id=$1""",
                row["id"],
            )
            verify_status, ref_id = await self.zarinpal.verify(
                row["amount"],
                row["authority"],
            )
            if verify_status != "verified":
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
                continue
            if changed:
                user = await self.db.pool.fetchrow(
                    "SELECT chat_id FROM users WHERE id=$1",
                    payment["user_id"],
                )
                await self.api.send_message(
                    user["chat_id"],
                    f"✅ پرداخت {payment['amount']:,} تومان پس از بررسی مجدد ثبت شد.\n"
                    f"کد پیگیری: {ref_id}",
                )


async def webhook(request):
    app_core: Application = request.app["core"]
    if request.match_info["secret"] != app_core.config.webhook_secret:
        raise web.HTTPNotFound()
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        raise web.HTTPBadRequest() from None
    asyncio.create_task(app_core.process_payload(payload))
    return web.json_response({"status": "ok"})


async def payment_callback(request):
    core: Application = request.app["core"]
    authority = (request.query.get("Authority") or request.query.get("authority") or "").strip()
    callback_status = (
        (request.query.get("Status") or request.query.get("status") or "").strip().upper()
    )
    title, message = "پرداخت ناموفق", "پرداخت انجام نشد."
    payment = await core.db.payment_by_authority(authority) if authority else None
    if payment and payment["status"] == "verified":
        title, message = "پرداخت موفق", "این پرداخت قبلاً ثبت شده است."
    elif (
        payment
        and payment["status"] in {"pending", "expired", "cancelled"}
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
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.on_startup.append(lambda _: core.start())
    app.on_cleanup.append(lambda _: core.close())
    return app


if __name__ == "__main__":
    settings = Settings.load()
    web.run_app(build_web_app(settings), host="0.0.0.0", port=settings.web_port)
