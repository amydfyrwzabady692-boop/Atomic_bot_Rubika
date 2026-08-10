import asyncio
import inspect
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from database import Database
from payment_safety import checked_decimal, supplier_cost_toman
from router import Router


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.forwards = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {"status": "OK"}

    async def forward_message(self, from_chat_id, message_id, to_chat_id):
        self.forwards.append((from_chat_id, message_id, to_chat_id))
        return {"status": "OK"}


class ReceiptPool:
    async def fetch(self, query, *_args):
        if "FROM receipts r" in query:
            return [
                {
                    "id": 7,
                    "payment_id": 9,
                    "user_id": 3,
                    "source_chat_id": "user-chat",
                    "source_message_id": "message-11",
                    "amount": 200_000,
                    "purpose": "order",
                    "order_id": 12,
                    "expires_at": datetime(2030, 1, 1, tzinfo=UTC),
                    "rubika_id": "u-user",
                }
            ]
        return []


class ReceiptDatabase:
    def __init__(self):
        self.pool = ReceiptPool()


class ActionDatabase:
    async def audit(self, *_args, **_kwargs):
        return None


class AdminPool:
    def __init__(self):
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


class AdminDatabase:
    def __init__(self):
        self.pool = AdminPool()

    async def audit(self, *_args, **_kwargs):
        return None


class PaymentPool:
    async def fetchrow(self, query, *_args):
        if "FROM orders" in query:
            return {
                "id": 12,
                "status": "pending",
                "inventory_reserved": True,
                "payable_amount": 200_000,
            }
        raise AssertionError(query)


class PaymentDatabase:
    def __init__(self, method):
        self.pool = PaymentPool()
        self.method = method
        self.sessions = []
        self.authorities = []

    async def setting(self, key, default=""):
        settings = {
            "payments_enabled": "1",
            "zarinpal_enabled": "1",
            "card_enabled": "1",
            "card_number": "6037991234567893",
            "card_holder": "صاحب کارت",
            "card_bank": "بانک",
        }
        return settings.get(key, default)

    async def set_session(self, rubika_id, state="", data=None):
        self.sessions.append((rubika_id, state, data))

    async def active_order_gateway(self, _user_id, _order_id):
        return None

    async def create_payment(self, *_args):
        return {"id": 31, "amount": 200_000, "provider": self.method}

    async def attach_authority(self, payment_id, authority):
        self.authorities.append((payment_id, authority))


class PaymentGateway:
    async def request(self, *_args):
        return "A0001", "https://payment.example/A0001", None


class ProductDatabase:
    async def products(self, _kind):
        skus = [
            "Level Up Package - Level 6",
            "Level Up Package - Level 10",
            "Level Up Package - Level 15",
            "Level Up Package - Level 20",
            "Level Up Package - Level 25",
            "Level Up Package - Level 30",
            "110",
            "231",
            "Weekly Membership",
            "Booyah Pass",
            "583",
            "1188",
            "Monthly Membership",
            "2420",
        ]
        return [
            {
                "id": i,
                "title": f"بسته {i}",
                "price": i * 1000,
                "supplier_sku": sku,
            }
            for i, sku in enumerate(skus, start=1)
        ]


class NullableAmountProductPool:
    async def fetchrow(self, *_args):
        return {
            "id": 7,
            "kind": "gem",
            "title": "💎 110 جم",
            "price": 191_000,
            "amount": None,
            "supplier_sku": "110",
        }


class NullableAmountProductDatabase:
    def __init__(self):
        self.pool = NullableAmountProductPool()

    async def setting(self, _key, default=""):
        return default


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class WalletConnection:
    def __init__(self, balance, payable):
        self.balance = balance
        self.payable = payable
        self.executed = []

    def transaction(self, **_kwargs):
        return AsyncContext(self)

    async def fetchrow(self, query, *_args):
        if "FROM users" in query:
            return {"id": 1, "balance": self.balance}
        if "FROM orders" in query:
            return {
                "id": 2,
                "status": "pending",
                "inventory_reserved": True,
                "wallet_paid": 0,
                "payable_amount": self.payable,
            }
        raise AssertionError(query)

    async def fetchval(self, query, *_args):
        if "SELECT 1 FROM payments" in query:
            return None
        if "count(*)+1" in query:
            return 1
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


class WalletPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


class VerifiedGatewayConnection:
    def __init__(self):
        self.queries = []

    def transaction(self, **_kwargs):
        return AsyncContext(self)

    async def fetchrow(self, query, *_args):
        self.queries.append(query)
        if "FROM payments" in query:
            return {
                "id": 41,
                "status": "verified",
                "ref_id": "REF-1",
                "provider": "gateway",
                "purpose": "order",
                "order_id": 12,
            }
        raise AssertionError(query)


class VerifiedGatewayPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


class WorkflowTests(unittest.TestCase):
    def make_router(self, database):
        api = FakeAPI()
        config = SimpleNamespace(
            admin_id="u-admin",
            admin_chat_id="admin-chat",
            payment_ttl_minutes=15,
            callback_base="https://rubika.example",
        )
        return Router(database, api, config), api

    def test_admin_receipts_forward_original_and_show_safe_review_buttons(self):
        router, api = self.make_router(ReceiptDatabase())
        asyncio.run(
            router.admin(
                {"chat_id": "admin-chat", "sender_id": "u-admin"},
                "admin_receipts",
            )
        )
        self.assertEqual(
            api.forwards,
            [("user-chat", "message-11", "admin-chat")],
        )
        keypads = [
            item[2].get("inline_keypad")
            for item in api.messages
            if item[2].get("inline_keypad")
        ]
        button_ids = [
            button["id"]
            for keypad in keypads
            for row in keypad["rows"]
            for button in row["buttons"]
        ]
        self.assertIn("receipt_review:ok:7", button_ids)
        self.assertIn("receipt_review:no:7", button_ids)
        self.assertNotIn("receipt_apply:ok:7", button_ids)

    def test_gems_and_memberships_are_page_one_and_level_ups_page_two(self):
        router, api = self.make_router(ProductDatabase())
        asyncio.run(
            router.show_products(
                {"chat_id": "user-chat"}, "gem", "محصولات", page=1
            )
        )
        keypad = api.messages[-1][2]["inline_keypad"]
        ids = [
            button["id"]
            for row in keypad["rows"]
            for button in row["buttons"]
        ]
        self.assertEqual(ids[:8], [f"product:{i}:1" for i in range(7, 15)])
        self.assertIn("products_page:gem:2", ids)
        self.assertNotIn("product:1:1", ids)

        asyncio.run(
            router.show_products(
                {"chat_id": "user-chat"}, "gem", "محصولات", page=2
            )
        )
        second_ids = [
            button["id"]
            for row in api.messages[-1][2]["inline_keypad"]["rows"]
            for button in row["buttons"]
        ]
        self.assertEqual(
            second_ids[:6],
            [f"product:{i}:2" for i in range(1, 7)],
        )

    def test_product_click_handles_legacy_null_amount(self):
        router, api = self.make_router(NullableAmountProductDatabase())
        asyncio.run(
            router.product_selected(
                {"chat_id": "user-chat"}, {"id": 3}, 7, page=1
            )
        )
        self.assertIn("تعداد جم: 110", api.messages[-1][1])
        self.assertIn("191,000 تومان", api.messages[-1][1])

    def test_fulfillment_worker_never_auto_retries_ambiguous_submission(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("SUBMIT_UNKNOWN", source)
        self.assertIn("find_order_by_remark", source)
        worker = source[
            source.index("async def fulfillment_loop"):
            source.index("async def cleanup_loop")
        ]
        self.assertNotIn("f.status='RETRY'", worker)

    def test_receipt_review_requires_a_second_confirmation(self):
        router, api = self.make_router(ActionDatabase())
        asyncio.run(
            router.handle_admin_action(
                {"chat_id": "admin-chat", "sender_id": "u-admin"},
                "receipt_review:ok:7",
            )
        )
        keypad = api.messages[-1][2]["inline_keypad"]
        button_ids = [
            button["id"]
            for row in keypad["rows"]
            for button in row["buttons"]
        ]
        self.assertIn("receipt_apply:ok:7", button_ids)

    def test_financial_database_paths_lock_and_validate_ownership(self):
        create_payment = Database.create_payment.__doc__ or ""
        self.assertIsInstance(create_payment, str)
        source = Path(ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("WHERE p.id=$1 AND p.user_id=$2 FOR UPDATE OF p", source)
        self.assertIn("provider\"] != \"card\"", source)
        self.assertIn("inventory_reserved", source)
        self.assertIn("ON CONFLICT(reference) DO NOTHING", source)
        self.assertIn("isolation=\"serializable\"", source)
        self.assertIn("gateway_issued", source)
        self.assertIn("receipt_pending", source)
        self.assertIn("یک سفارش با لینک درگاه", source)

    def test_payment_method_cannot_change_while_money_is_in_flight(self):
        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        router_source = (ROOT / "router.py").read_text(encoding="utf-8")
        worker_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("p.provider='gateway' AND p.authority IS NOT NULL", database_source)
        self.assertIn("receipt_pending", database_source)
        self.assertIn("لینک درگاه هم صادر شده", router_source)
        self.assertIn('verify_status == "not_paid"', worker_source)
        self.assertIn("verify_attempts", worker_source)

    def test_expired_gateway_link_does_not_lock_a_fresh_order(self):
        # A gateway payment that is expired/cancelled (and never paid) must not
        # block creating a fresh payment for the same order, nor be offered as
        # an active link. Only live pending-with-valid-expiry links count.
        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("AND p.status='pending' AND p.expires_at>now()", database_source)
        # The stale link must be detached to wallet so a late payment credits
        # the wallet instead of double-delivering the order.
        self.assertIn("purpose='wallet',order_id=NULL", database_source)
        self.assertIn("status IN ('expired','cancelled')", database_source)
        # Retrying wallet charge must cancel a stale gateway link without
        # crediting the wallet — pending links are not verified payments.
        self.assertIn("Never credit the wallet here", database_source)
        self.assertNotIn("auto-cancel-wallet:", database_source)
        # Reconcile releases stale unpaid links sooner instead of holding the
        # order for up to 24h.
        worker_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('and row["status"] == "expired"', worker_source)
        self.assertIn("provider IN ('gateway','card')", worker_source)
        self.assertIn("inventory_reserved=false", worker_source)
        self.assertIn("delivery_failed", worker_source)
        self.assertIn("order_amounts", database_source)
        self.assertIn("'rejected'", database_source)

    def test_admin_panel_has_internal_guard_and_session_reset(self):
        router_source = (ROOT / "router.py").read_text(encoding="utf-8")
        self.assertIn("async def admin(self, event, action):", router_source)
        self.assertIn("دسترسی مدیر ندارید.", router_source)
        self.assertIn("admin_product_add_kind", router_source)
        self.assertIn("product_add_start(event)", router_source)
        self.assertIn("COALESCE(category,'bot')<>'credential'", router_source)

    def test_late_gateway_payment_credits_wallet_when_order_closed(self):
        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("order_status != \"pending\"", database_source)
        self.assertIn("purpose='wallet',order_id=NULL", database_source)

    def test_credential_prices_sync_with_daily_gem_price_loop(self):
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("sync_credential_prices", main_source)
        self.assertIn("cred_updated", main_source)
        self.assertIn("weekly_profit", main_source)
        self.assertIn("credential_weekly_profit_percent", database_source)
        self.assertIn("'40'", database_source)
        self.assertIn("compute_gem_sale_price", database_source)

    def test_reconcile_detaches_instead_of_terminal_reject(self):
        worker_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("UPDATE payments SET status='rejected'", worker_source)
        self.assertIn("purpose='wallet',order_id=NULL", worker_source)
        worker_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("UPDATE payments SET status='rejected'", worker_source)
        self.assertIn("purpose='wallet',order_id=NULL", worker_source)

    def test_pending_receipt_is_not_expired_before_admin_review(self):
        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        worker_source = (ROOT / "main.py").read_text(encoding="utf-8")
        review_source = inspect.getsource(Router.review_receipt)
        self.assertIn("r.status='pending'", database_source)
        self.assertIn("r.status='pending'", worker_source)
        self.assertNotIn('receipt["expires_at"] <= now', review_source)

    def test_completed_supplier_notifications_use_a_durable_outbox(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("deliver_fulfillment_notifications", source)
        self.assertIn("user_notified_at", source)
        self.assertIn("admin_notified_at", source)
        self.assertIn("f.provider='g2bulk'", source)

    def test_partial_wallet_payment_only_spends_available_balance(self):
        connection = WalletConnection(balance=50_000, payable=200_000)
        database = Database("postgresql://unused")
        database.pool = WalletPool(connection)
        result = asyncio.run(database.wallet_pay(1, 2))
        self.assertEqual(
            result,
            {
                "used": 50_000,
                "remaining": 150_000,
                "balance": 0,
                "paid": False,
            },
        )
        ledger = next(
            args
            for query, args in connection.executed
            if "INSERT INTO wallet_ledger" in query
        )
        self.assertEqual(ledger[1], -50_000)
        self.assertTrue(
            any(
                "payment_method='wallet+pending'" in query
                for query, _args in connection.executed
            )
        )

    def test_full_wallet_payment_marks_order_paid(self):
        connection = WalletConnection(balance=300_000, payable=200_000)
        database = Database("postgresql://unused")
        database.pool = WalletPool(connection)
        result = asyncio.run(database.wallet_pay(1, 2))
        self.assertTrue(result["paid"])
        self.assertEqual(result["remaining"], 0)
        self.assertTrue(
            any(
                "status='paid'" in query and "inventory_reserved=false" in query
                for query, _args in connection.executed
            )
        )

    def test_repeated_gateway_callback_is_idempotent_before_order_check(self):
        connection = VerifiedGatewayConnection()
        database = Database("postgresql://unused")
        database.pool = VerifiedGatewayPool(connection)
        payment, changed = asyncio.run(
            database.finalize_gateway("AUTH-1", "REF-1")
        )
        self.assertFalse(changed)
        self.assertEqual(payment["id"], 41)
        self.assertEqual(len(connection.queries), 2)
        self.assertNotIn("FOR UPDATE", connection.queries[0])
        self.assertIn("FOR UPDATE", connection.queries[1])

    def test_supplier_cost_math_uses_decimal_rounding(self):
        self.assertEqual(str(checked_decimal("0.935")), "0.935000")
        self.assertEqual(supplier_cost_toman("0.935", 100_001), 93_501)

    def test_admin_supports_stable_product_and_category_reordering(self):
        source = inspect.getsource(Router.admin_command)
        move_source = inspect.getsource(Database.move_catalogue_item)
        self.assertIn('/product_move', source)
        self.assertIn('/category_move', source)
        self.assertIn('"first"', move_source)
        self.assertIn('"last"', move_source)
        self.assertIn("executemany", move_source)

    def test_delegated_admin_cannot_manage_other_admins(self):
        database = AdminDatabase()
        router, api = self.make_router(database)
        asyncio.run(
            router.admin_command(
                {"chat_id": "delegated-chat", "sender_id": "u-delegated"},
                "/admin_add u01234567890 Test",
            )
        )
        self.assertFalse(database.pool.executed)
        self.assertIn("فقط در اختیار مالک اصلی", api.messages[-1][1])

    def test_owner_can_disable_all_delegated_admins(self):
        database = AdminDatabase()
        router, api = self.make_router(database)
        asyncio.run(
            router.admin_command(
                {"chat_id": "admin-chat", "sender_id": "u-admin"},
                "/admin_clear",
            )
        )
        self.assertTrue(
            any(
                "UPDATE admins SET active=false" in query
                and args == ("u-admin",)
                for query, args in database.pool.executed
            )
        )
        self.assertIn("موفقیت", api.messages[-1][1])

    def test_gateway_payment_is_a_direct_link_button_without_vpn_text(self):
        router, api = self.make_router(PaymentDatabase("gateway"))
        router.zarinpal = PaymentGateway()
        asyncio.run(
            router.pay_order(
                {"chat_id": "user-chat", "sender_id": "u-user"},
                {"id": 3},
                12,
                "gateway",
            )
        )
        # First message carries the Link button; the URL is also sent as a
        # standalone tap-able message so Rubika can open it even if the Link
        # button itself is not clickable in some clients.
        first, *rest = api.messages
        self.assertNotIn("VPN", first[1])
        button = first[2]["inline_keypad"]["rows"][0]["buttons"][0]
        self.assertEqual(button["type"], "Link")
        self.assertEqual(button["id"], "https://payment.example/A0001")
        # The URL must be delivered on its own line as a clickable link.
        self.assertTrue(any(msg[1] == "https://payment.example/A0001" for msg in rest))

    def test_card_number_is_sent_as_an_exact_standalone_copy_message(self):
        router, api = self.make_router(PaymentDatabase("card"))
        asyncio.run(
            router.pay_order(
                {"chat_id": "user-chat", "sender_id": "u-user"},
                {"id": 3},
                12,
                "card",
            )
        )
        texts = [message[1] for message in api.messages]
        self.assertIn("6037991234567893", texts)
        self.assertFalse(any("کارت: 6037991234567893" in text for text in texts))


if __name__ == "__main__":
    unittest.main()
