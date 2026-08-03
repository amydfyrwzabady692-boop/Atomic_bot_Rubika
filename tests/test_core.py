import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from database import Database
from keyboards import admin_menu, link_button, main_menu
from payment_safety import checked_amount, order_amounts, valid_card_number
from payments import Zarinpal
from router import Router
from rubika_api import normalize_event
from supplier import G2Bulk, g2_idempotency_key


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

    def test_ambiguous_g2_submission_is_not_a_safe_failure(self):
        supplier = G2Bulk()
        supplier.key = "test-key"
        with patch.object(
            supplier,
            "_call",
            AsyncMock(
                return_value={
                    "success": False,
                    "message": "timeout",
                    "_transport_uncertain": True,
                }
            ),
        ):
            result = asyncio.run(supplier.order("110", "12345", 91))
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])

    def test_ambiguous_order_can_be_recovered_by_exact_remark(self):
        supplier = G2Bulk()
        supplier.key = "test-key"
        with patch.object(
            supplier,
            "_call",
            AsyncMock(
                return_value={
                    "success": True,
                    "orders": [
                        {
                            "order_id": 1255767,
                            "remark": "rubika-91",
                            "status": "COMPLETED",
                        }
                    ],
                }
            ),
        ):
            result = asyncio.run(supplier.find_order_by_remark("rubika-91"))
        self.assertTrue(result["found"])
        self.assertEqual(result["provider_order_id"], "1255767")

    def test_status_uses_numeric_provider_id_and_accepts_nested_payload(self):
        supplier = G2Bulk()
        supplier.key = "test-key"
        call = AsyncMock(
            return_value={
                "success": True,
                "data": {
                    "order": {
                        "status": "COMPLETED",
                        "player_name": "Player",
                    }
                },
            }
        )
        with patch.object(supplier, "_call", call):
            result = asyncio.run(supplier.status("1259759"))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["player_name"], "Player")
        self.assertEqual(
            call.await_args_list[0].args,
            ("POST", "/games/order/status", {"order_id": 1259759}),
        )

    def test_status_reconciles_against_order_history_without_resubmitting(self):
        supplier = G2Bulk()
        supplier.key = "test-key"
        call = AsyncMock(
            side_effect=[
                {"success": False, "message": "unknown response"},
                {"success": False, "message": "unknown response"},
                {
                    "success": True,
                    "orders": [
                        {
                            "order_id": 1259759,
                            "status": "COMPLETED",
                            "player_name": "Player",
                        }
                    ],
                },
            ]
        )
        with patch.object(supplier, "_call", call):
            result = asyncio.run(supplier.status("1259759"))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(call.await_args_list[-1].args[0], "GET")
        self.assertTrue(
            all(args.args[0] != "POST" or args.args[1] == "/games/order/status"
                for args in call.await_args_list)
        )

    def test_card_number_checksum(self):
        self.assertTrue(valid_card_number("6037 9912 3456 7893"))
        self.assertFalse(valid_card_number("6037991234567890"))
        self.assertFalse(valid_card_number("1111111111111111"))

    def test_zarinpal_merchant_prefers_db_setting_over_env(self):
        async def getter(key, default):
            self.assertEqual(key, "zarinpal_merchant_id")
            return "db-merchant"

        zp = Zarinpal(settings_getter=getter)
        zp._env_merchant = "env-merchant"
        self.assertEqual(asyncio.run(zp.merchant()), "db-merchant")

    def test_zarinpal_merchant_falls_back_to_env(self):
        zp = Zarinpal(settings_getter=None)
        zp._env_merchant = "env-merchant"
        self.assertEqual(asyncio.run(zp.merchant()), "env-merchant")


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

    def test_order_amount_parameters_have_explicit_sql_types(self):
        source = inspect.getsource(Database.create_order)
        self.assertIn("$2::bigint-$3::bigint", source)

    def test_payment_parameters_have_explicit_sql_types(self):
        source = inspect.getsource(Database.create_payment)
        self.assertIn("$5::bigint", source)
        self.assertIn("make_interval(mins => $6::int)", source)

    def test_card_settings_fall_back_to_environment(self):
        source = inspect.getsource(Router.pay_order)
        self.assertIn('os.getenv(\n                "CARD_TRANSFER_NUMBER"', source)

    def test_admin_panel_has_interactive_flows(self):
        handle_source = inspect.getsource(Router.handle)
        admin_source = inspect.getsource(Router.admin)
        state_source = inspect.getsource(Router.handle_state)
        # Admin chat_keypad labels map to actions for clients that send text.
        self.assertIn('"👥 کاربران": "admin_users"', handle_source)
        self.assertIn('"💳 بخش مالی": "admin_finance"', handle_source)
        # Interactive charge flows exist.
        self.assertIn("admin_charge", admin_source)
        self.assertIn("admin_charge_one", state_source)
        self.assertIn("admin_charge_all", state_source)
        # Interactive promo code flows exist.
        self.assertIn("admin_code_add", admin_source)
        self.assertIn("admin_code_del", admin_source)
        # Interactive admin add/remove exists.
        self.assertIn("admin_add_admin", state_source)
        self.assertIn("admin_remove:", admin_source)
        # Broadcast is state-driven.
        self.assertIn("admin_broadcast", state_source)
        # Users list shows display name.
        self.assertIn("display_name", admin_source)

    def test_receipt_review_has_buttons_and_safe_command_validation(self):
        handle_source = inspect.getsource(Router.handle)
        command_source = inspect.getsource(Router.admin_command)
        message_source = inspect.getsource(Router.handle_state)
        admin_source = inspect.getsource(Router.handle_admin_action)
        self.assertIn('"receipt_review:"', handle_source)
        self.assertIn("✅ تأیید رسید", message_source)
        self.assertIn("❌ رد رسید", message_source)
        self.assertIn("receipt_apply:", admin_source)
        self.assertIn("if not receipt_arg.isdigit()", command_source)
        self.assertIn("شماره رسید را وارد کن", command_source)

    def test_inline_buttons_are_not_permanently_deduplicated(self):
        source = inspect.getsource(Router.handle)
        self.assertIn('not event["event_id"].startswith("inline:")', source)


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
        self.assertIn("🎮 محصولات فری‌فایر", labels)
        self.assertIn("🛍 فروشگاه اکانت", labels)
        self.assertIn("🎧 پشتیبانی", labels)

    def test_link_button_uses_rubika_url_button_shape(self):
        value = link_button(
            "open-payment",
            "🔗 باز کردن درگاه پرداخت",
            "https://payment.example/authority",
        )
        self.assertEqual(value["type"], "Link")
        self.assertEqual(value["id"], "https://payment.example/authority")
        self.assertNotIn("button_link", value)
        with self.assertRaises(ValueError):
            link_button("bad", "bad", "javascript:alert(1)")

    def test_every_static_menu_button_has_a_router_action(self):
        source = inspect.getsource(Router)
        for menu in (main_menu(), admin_menu()):
            for row in menu["rows"]:
                for item in row["buttons"]:
                    self.assertIn(
                        f'"{item["id"]}"',
                        source,
                        f"missing route for {item['id']}",
                    )

    def test_payment_copy_and_role_safety_text(self):
        source = inspect.getsource(Router)
        handle_source = inspect.getsource(Router.handle)
        self.assertNotIn("VPN را خاموش", source)
        self.assertNotIn("وی‌پی‌ان", source)
        self.assertIn("مدیریت مدیران فقط در اختیار مالک اصلی", source)
        self.assertIn('action.startswith("admin_")', handle_source)
        self.assertIn("if is_admin:", handle_source)
        self.assertEqual(Router.pretty_card("6037 9912-3456 7893"), "6037991234567893")


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
        self.assertIn("inventory_reserved", schema)
        self.assertIn("next_retry_at", schema)
        self.assertIn("user_notified_at", schema)
        self.assertIn("admin_notified_at", schema)
        self.assertIn("payments_enabled", schema)
        self.assertIn("telegram_catalog_20260726", schema)

    def test_catalogue_order_and_order_history_are_durable(self):
        schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        self.assertIn("sort_order INTEGER NOT NULL", schema)
        self.assertIn("order_status_history", schema)
        self.assertIn("trg_order_status_transition", schema)
        self.assertIn("ORDER BY sort_order,id", database_source)
        self.assertIn("g2bulk_catalogue_14_20260727", schema)
        self.assertIn(
            "('gem','🎯 لول‌آپ سطح 6',6,"
            "'Level Up Package - Level 6',65000,9999)",
            schema,
        )
        self.assertIn(
            "('gem','💎 2420 جم',2420,'2420',3824000,9999)",
            schema,
        )
        self.assertIn("g2bulk_catalogue_titles_fa_v2_20260727", schema)
        self.assertIn("g2bulk_package_titles_fa_v3_20260728", schema)
        self.assertIn("status='SUBMIT_UNKNOWN'", schema)


if __name__ == "__main__":
    unittest.main()
