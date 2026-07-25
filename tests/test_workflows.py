import asyncio
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


if __name__ == "__main__":
    unittest.main()
