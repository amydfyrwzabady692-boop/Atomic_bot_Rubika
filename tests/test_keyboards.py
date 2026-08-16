import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from keyboards import inline, inline_as_chat_keypad, link_button


class ChatKeypadMirrorTests(unittest.TestCase):
    def test_copies_simple_buttons_for_polling(self):
        glass = inline(
            [
                [("gems_by_id", "🆔 جم با آیدی · تحویل لحظه‌ای")],
                [("home", "🔙 منوی اصلی")],
            ]
        )
        bottom = inline_as_chat_keypad(glass)
        self.assertEqual(bottom["rows"][0]["buttons"][0]["id"], "gems_by_id")
        self.assertTrue(bottom["resize_keyboard"])
        self.assertFalse(bottom["one_time_keyboard"])

    def test_skips_link_buttons(self):
        glass = inline(
            [
                [link_button("unused", "باز کردن درگاه", "https://example.com/pay")],
                [("pay_change:7", "تغییر روش")],
            ]
        )
        bottom = inline_as_chat_keypad(glass)
        self.assertEqual(len(bottom["rows"]), 1)
        self.assertEqual(bottom["rows"][0]["buttons"][0]["id"], "pay_change:7")

    def test_empty_or_link_only_returns_none(self):
        self.assertIsNone(inline_as_chat_keypad(None))
        self.assertIsNone(
            inline_as_chat_keypad(
                inline([[link_button("x", "لینک", "https://example.com")]])
            )
        )


if __name__ == "__main__":
    unittest.main()
