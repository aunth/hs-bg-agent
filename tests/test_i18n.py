"""Unit tests for i18n localization (Ukrainian and English)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hsbg.i18n import (  # noqa: E402
    get_lang,
    set_lang,
    t,
    toggle_lang,
    tr_fit_describe,
    tr_reason,
    tr_role,
    tr_tribe,
    tr_verdict,
)
from hsbg.tavern import BuyMinion, RollShop, SellMinion, UpgradeTavern  # noqa: E402


class I18nTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_lang("uk")

    def test_language_switch_and_toggle(self) -> None:
        set_lang("uk")
        self.assertEqual(get_lang(), "uk")
        self.assertEqual(t("app_title"), "ПОЛЯ БИТВ")
        self.assertEqual(tr_tribe("Demon/Naga"), "Демон/Нага")
        self.assertEqual(tr_role("CORE"), "Ядро")

        toggle_lang()
        self.assertEqual(get_lang(), "en")
        self.assertEqual(t("app_title"), "BATTLEGROUNDS")
        self.assertEqual(tr_tribe("Demon/Naga"), "Demon/Naga")
        self.assertEqual(tr_role("CORE"), "Core")

    def test_action_descriptions_localized(self) -> None:
        buy = BuyMinion(1, "C1", "Buzzing Vermin", 3)
        sell = SellMinion(2, "C2", "Cord Puller", 1)
        up = UpgradeTavern(4, 2)
        roll = RollShop(1)

        set_lang("uk")
        self.assertEqual(buy.describe(), "Купити Buzzing Vermin (-3з)")
        self.assertEqual(sell.describe(), "Продати Cord Puller (+1з)")
        self.assertEqual(up.describe(), "Ап до T2 (-4з)")
        self.assertEqual(roll.describe(), "Рол (-1з) -> переплан")

        set_lang("en")
        self.assertEqual(buy.describe(), "Buy Buzzing Vermin (-3g)")
        self.assertEqual(sell.describe(), "Sell Cord Puller (+1g)")
        self.assertEqual(up.describe(), "Upgrade to T2 (-4g)")
        self.assertEqual(roll.describe(), "Roll (-1g) -> re-plan")

    def test_reason_and_fit_translations(self) -> None:
        set_lang("uk")
        self.assertEqual(tr_reason("completes triple!"), "збирає триплет!")
        self.assertEqual(tr_reason("makes a pair"), "робить пару")
        self.assertEqual(tr_reason("meta +0.75"), "мета +0.75")
        self.assertEqual(tr_reason("strong at turn 4"), "сильна на ході 4")
        self.assertEqual(
            tr_reason("core of Beast Beetle (2/11 cards, 2 on tribe, A)"),
            "ядро Beast Beetle (2/11 карт, 2 за племенем, A)",
        )
        self.assertEqual(
            tr_fit_describe("board 42% of average, place 5, took 6 dmg last 2t"),
            "стіл 42% від середнього, місце #5, -6 ХП за 2х",
        )
        self.assertEqual(
            tr_verdict("behind pace -- Beast Beetle needs 3 core piece(s), up to tier 5"),
            "позаду темпу -- Beast Beetle бракує 3 карт ядра, до T5",
        )


if __name__ == "__main__":
    unittest.main()
