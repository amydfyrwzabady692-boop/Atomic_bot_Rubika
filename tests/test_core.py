import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from database import Database
from keyboards import main_menu
from payment_safety import checked_amount, order_amounts
from rubika_api import normalize_event
from supplier import g2_idempotency_key


class EventTests(unittest.TestCase):
    def test_normalizes_update(self):
        event = normalize_event(
            {
                "update": {
                    "type": "NewMessage",
                    "chat_id": "chat",
                    "new_message": {
                        "message_id": "m1",
                        "sender_id": "u1",
                        "text": "hello",
                        "aux_data": {"button_id": "gems"},
                    },
                }
            }
        )
        self.assertEqual(event["sender_id"], "u1")
        self.assertEqual(event["button_id"], "gems")
        self.assertEqual(event["event_id"], "update:chat:m1:NewMessage")

    def test_normalizes_inline(self):
        event = normalize_event(
            {
                "inline_message": {
                    "chat_id": "chat",
                    "sender_id": "u1",
                    "message_id": "m2",
                    "aux_data": {"button_id": "pay:gateway:7"},
                }
            }
        )
        self.assertEqual(event["button_id"], "pay:gateway:7")

    def test_ignores_non_message_updates(self):
        self.assertIsNone(normalize_event({"update": {"type": "RemovedMessage"}}))


class FinancialTests(unittest.TestCase):
    def test_amount_rejects_bool_and_bounds(self):
        with self.assertRaises(ValueError):
            checked_amount(True)
        with self.assertRaises(ValueError):
            checked_amount(-1)
        with self.assertRaises(ValueError):
            checked_amount(100_000_001)

    def test_order_amounts(self):
        self.assertEqual(order_amounts(100_000, 10_000, 20_000), (90_000, 70_000))
        with self.assertRaises(ValueError):
            order_amounts(100_000, 100_000, 0)
        with self.assertRaises(ValueError):
            order_amounts(100_000, 0, 100_001)

    def test_g2bulk_idempotency_key_is_stable_uuid(self):
        first = g2_idempotency_key(42)
        self.assertEqual(first, g2_idempotency_key(42))
        self.assertEqual(len(first), 36)


class DatabaseTests(unittest.TestCase):
    def test_session_decodes_json_text(self):
        class Pool:
            async def fetchrow(self, *_args):
                return {"state": "gem_player_id", "data": '{"product_id": 7}'}

        database = Database("postgresql://unused")
        database.pool = Pool()
        state, data = asyncio.run(database.session("u1"))
        self.assertEqual(state, "gem_player_id")
        self.assertEqual(data, {"product_id": 7})

    def test_session_rejects_malformed_json_text(self):
        class Pool:
            async def fetchrow(self, *_args):
                return {"state": "gem_player_id", "data": "not-json"}

        database = Database("postgresql://unused")
        database.pool = Pool()
        state, data = asyncio.run(database.session("u1"))
        self.assertEqual(state, "gem_player_id")
        self.assertEqual(data, {})


class KeyboardTests(unittest.TestCase):
    def test_main_keyboard_uses_official_shape(self):
        value = main_menu()
        self.assertTrue(value["resize_keyboard"])
        self.assertGreaterEqual(len(value["rows"]), 4)
        for row in value["rows"]:
            for button in row["buttons"]:
                self.assertEqual(button["type"], "Simple")
                self.assertTrue(button["id"])
        labels = [
            button["button_text"]
            for row in value["rows"]
            for button in row["buttons"]
        ]
        self.assertIn("💎 جم فری‌فایر", labels)
        self.assertIn("🛍 فروشگاه اکانت", labels)


class SchemaTests(unittest.TestCase):
    def test_financial_uniqueness_and_checks_present(self):
        schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("authority TEXT UNIQUE", schema)
        self.assertIn("ref_id TEXT UNIQUE", schema)
        self.assertIn("reference TEXT UNIQUE", schema)
        self.assertIn("CHECK (balance >= 0)", schema)
        self.assertIn("order_id BIGINT UNIQUE", schema)
        self.assertIn("UNIQUE(code_id,user_id)", schema)
        self.assertIn("pending_discounts", schema)
        self.assertIn("telegram_catalog_20260726", schema)
        self.assertIn("('gem','بسته ۱۱۰ جمی',110,'110',200000,9999)", schema)


if __name__ == "__main__":
    unittest.main()
