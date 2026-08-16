import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from button_labels import (
    ADMIN_BUTTON_LABELS,
    CRED_STAFF_BUTTON_LABELS,
    USER_BUTTON_LABELS,
    apply_button_label_map,
)
from keyboards import (
    admin_menu,
    credential_staff_menu,
    main_menu,
)


def _keypad_buttons(keypad):
    for row in keypad["rows"]:
        for item in row["buttons"]:
            yield item["id"], item["button_text"]


class StaticMenuCoverageTests(unittest.TestCase):
    def _resolve(self, button_id, text, *, owner=False, staff=False):
        current = (button_id or text or "").strip()
        current = apply_button_label_map(
            USER_BUTTON_LABELS, button_id, text, current
        )
        if owner:
            current = apply_button_label_map(
                ADMIN_BUTTON_LABELS, button_id, text, current
            )
        elif staff:
            current = apply_button_label_map(
                CRED_STAFF_BUTTON_LABELS, button_id, text, current
            )
        return current

    def test_every_static_menu_button_id_survives(self):
        cases = [
            (main_menu(), False, False),
            (main_menu(is_admin=True), True, False),
            (main_menu(is_cred_staff=True), False, True),
            (admin_menu(), True, False),
            (credential_staff_menu(), False, True),
        ]
        for keypad, owner, staff in cases:
            for button_id, text in _keypad_buttons(keypad):
                mapped = self._resolve(button_id, text, owner=owner, staff=staff)
                self.assertEqual(mapped, button_id, f"{text} -> {mapped}")

    def test_user_and_admin_text_only_labels_resolve(self):
        for button_id, text in _keypad_buttons(main_menu()):
            mapped = self._resolve("", text)
            self.assertEqual(mapped, button_id, text)
        for button_id, text in _keypad_buttons(admin_menu()):
            mapped = self._resolve("", text, owner=True)
            self.assertEqual(mapped, button_id, text)

    def test_real_button_id_is_not_overridden_by_admin_label(self):
        action = apply_button_label_map(
            ADMIN_BUTTON_LABELS,
            "support",
            "🎧 پشتیبانی",
            "support",
        )
        self.assertEqual(action, "support")

    def test_owner_text_only_admin_support_still_maps(self):
        action = apply_button_label_map(
            ADMIN_BUTTON_LABELS,
            "",
            "🎧 پشتیبانی",
            "🎧 پشتیبانی",
        )
        self.assertEqual(action, "admin_support")


if __name__ == "__main__":
    unittest.main()
