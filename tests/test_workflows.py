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
        message = api.messages[-1]
        self.assertNotIn("VPN", message[1])
        button = message[2]["inline_keypad"]["rows"][0]["buttons"][0]
        self.assertEqual(button["type"], "Link")
        self.assertEqual(
            button["button_link"]["link_url"],
            "https://payment.example/A0001",
        )

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
