"""Unit tests for Advisor scoring, pairs/triples, Discover, and CombatHistory."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hsbg.advisor import Advisor, CombatHistory  # noqa: E402
from hsbg.carddb import Card  # noqa: E402
from hsbg.state import Minion, ShopSpell  # noqa: E402
from hsbg.stats import CardStat, Comp  # noqa: E402


class FakeCards:
    def __init__(self, cards: dict[str, Card]):
        self._cards = cards

    def by_id(self, cid: str) -> Card | None:
        return self._cards.get(cid)

    def name(self, cid: str, default: str = "") -> str:
        c = self._cards.get(cid)
        return c.name if c else (default or cid)


class FakeStats:
    def __init__(self, cards: dict[str, CardStat], comps: list[Comp]):
        self._cards = cards
        self.comps = comps
        self.heroes = {}

    def card(self, cid: str) -> CardStat | None:
        return self._cards.get(cid)

    def hero(self, cid: str):
        return self.heroes.get(cid)


class AdvisorTests(unittest.TestCase):
    def test_combat_history_updates_same_turn(self) -> None:
        h = CombatHistory()
        h.observe(turn=3, damage_taken=0, place=0)
        h.observe(turn=3, damage_taken=7, place=5)
        self.assertEqual(h.turns, [(3, 7, 5)])
        h.observe(turn=4, damage_taken=0, place=4)
        self.assertEqual(h.recent_damage(), [7, 0])

    def test_pair_and_triple_bonuses(self) -> None:
        cards = FakeCards({
            "C1": Card("C1", 1, "Alpha", races=("BEAST",)),
            "C2": Card("C2", 2, "Beta", races=("BEAST",)),
        })
        stats = FakeStats(
            {
                "C1": CardStat("C1", 500, 4.3, 4.5),
                "C2": CardStat("C2", 500, 4.3, 4.5),
            },
            [],
        )
        adv = Advisor(cards, stats)

        bg_pair = SimpleNamespace(
            turn=4,
            shop=[Minion(1, "C1", "Alpha", 2, 2, 1, 1), Minion(2, "C2", "Beta", 2, 2, 1, 2)],
            shop_spells=[],
            board=[Minion(3, "C1", "Alpha", 2, 2, 1, 1, golden=False)],
            hand=[],
            damage_last_turn=0,
            leaderboard_place=4,
        )
        suggs, _ = adv.evaluate_shop(bg_pair, observe=False)
        by_id = {s.card_id: s for s in suggs}
        self.assertGreater(by_id["C1"].score, by_id["C2"].score)
        self.assertIn("makes a pair", by_id["C1"].reasons)

        # Two copies -> triple completion bonus is larger than pair bonus
        bg_triple = SimpleNamespace(
            turn=4,
            shop=[Minion(1, "C1", "Alpha", 2, 2, 1, 1)],
            shop_spells=[],
            board=[Minion(3, "C1", "Alpha", 2, 2, 1, 1, golden=False)],
            hand=[Minion(4, "C1", "Alpha", 2, 2, 1, 1, golden=False)],
            damage_last_turn=0,
            leaderboard_place=4,
        )
        suggs3, _ = adv.evaluate_shop(bg_triple, observe=False)
        self.assertGreater(suggs3[0].score, by_id["C1"].score)
        self.assertIn("completes triple!", suggs3[0].reasons)

    def test_score_choices_and_spells(self) -> None:
        cards = FakeCards({
            "GOOD": Card("GOOD", 1, "Strong Pick", races=("DEMON",)),
            "BAD": Card("BAD", 2, "Weak Pick", races=("MURLOC",)),
            "SP1": Card("SP1", 3, "Tavern Coin"),
        })
        stats = FakeStats(
            {
                "GOOD": CardStat("GOOD", 1000, 3.8, 4.6),
                "BAD": CardStat("BAD", 1000, 4.9, 4.4),
                "SP1": CardStat("SP1", 1000, 4.1, 4.5),
            },
            [],
        )
        adv = Advisor(cards, stats)
        bg = SimpleNamespace(
            turn=5,
            shop=[],
            shop_spells=[ShopSpell(10, "SP1", "Tavern Coin", cost=1, tier=1, pos=1)],
            board=[Minion(1, "GOOD", "Strong Pick", 4, 4, 3, 1)],
            hand=[],
            damage_last_turn=0,
            leaderboard_place=3,
        )
        choices = adv.score_choices(bg, ["BAD", "GOOD"])
        self.assertEqual([c.card_id for c in choices], ["BAD", "GOOD"])
        self.assertGreater(choices[1].score, choices[0].score)

        sp = adv.evaluate_spells(bg)
        self.assertEqual(len(sp), 1)
        self.assertGreater(sp[0].score, 50.0)


if __name__ == "__main__":
    unittest.main()
