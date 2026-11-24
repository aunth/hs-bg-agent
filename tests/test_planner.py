"""Unit tests for tavern transition model, MinionPool, ValueFunction, TurnPlanner, and positioning."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hsbg.advisor import Advisor  # noqa: E402
from hsbg.carddb import Card  # noqa: E402
from hsbg.env import BGGymEnv  # noqa: E402
from hsbg.planner import TurnPlanner, optimize_positioning  # noqa: E402
from hsbg.stats import CardStat, Comp  # noqa: E402
from hsbg.tavern import (  # noqa: E402
    BuyMinion,
    BuySpell,
    FreezeShop,
    GameSnapshot,
    MinionPool,
    PlayMinion,
    PlaySpell,
    RollShop,
    SellMinion,
    TMinion,
    TSpell,
    UpgradeTavern,
    UseHeroPower,
    apply_action,
    legal_actions,
)


class FakeCards:
    def __init__(self, cards: dict[str, Card]):
        self._cards = cards

    def by_id(self, cid: str) -> Card | None:
        return self._cards.get(cid)

    def name(self, cid: str, default: str = "") -> str:
        c = self._cards.get(cid)
        return c.name if c else (default or cid)

    @property
    def pool(self) -> list[Card]:
        return [c for c in self._cards.values() if c.is_pool_minion]


class FakeStats:
    def __init__(self, cards: dict[str, CardStat], comps: list[Comp]):
        self._cards = cards
        self.comps = comps
        self.heroes = {}

    def card(self, cid: str) -> CardStat | None:
        return self._cards.get(cid)

    def hero(self, cid: str):
        return self.heroes.get(cid)


def _make_advisor() -> Advisor:
    cards = FakeCards({
        "CORE_1": Card("CORE_1", 1, "Beast Core", attack=5, health=5, tier=4, races=("BEAST",), is_pool_minion=True),
        "CORE_2": Card("CORE_2", 2, "Beast Enabler", attack=4, health=4, tier=3, races=("BEAST",), is_pool_minion=True),
        "T1_GOOD": Card("T1_GOOD", 3, "Good T1", attack=3, health=3, tier=1, races=("BEAST",), is_pool_minion=True),
        "T1_WEAK": Card("T1_WEAK", 4, "Weak Filler", attack=1, health=1, tier=1, races=("MURLOC",), is_pool_minion=True),
        "T1_TRASH": Card("T1_TRASH", 5, "Bad Card", attack=1, health=2, tier=1, races=("DEMON",), is_pool_minion=True),
        "SP_GOOD": Card("SP_GOOD", 6, "Tavern Coin", type="BATTLEGROUND_SPELL", tier=1),
        "TAUNT_1": Card("TAUNT_1", 7, "Shield Guard", attack=2, health=6, tier=2, mechanics=("TAUNT",)),
        "CLEAVE_1": Card("CLEAVE_1", 8, "Cave Hydra", attack=6, health=4, tier=4, text="Also damages the minions next to whomever this attacks."),
    })
    stats = FakeStats(
        cards={
            "CORE_1": CardStat("CORE_1", 1000, 3.6, 4.6),
            "CORE_2": CardStat("CORE_2", 1000, 3.9, 4.6),
            "T1_GOOD": CardStat("T1_GOOD", 1000, 4.0, 4.5),
            "T1_WEAK": CardStat("T1_WEAK", 1000, 4.9, 4.4),
            "T1_TRASH": CardStat("T1_TRASH", 1000, 5.1, 4.3),
            "SP_GOOD": CardStat("SP_GOOD", 1000, 4.1, 4.5),
        },
        comps=[
            Comp(
                comp_id="beast_comp",
                name="Beast Deathrattle",
                power_level="S",
                difficulty="Medium",
                cards={"CORE_1": "CORE", "CORE_2": "CORE", "T1_GOOD": "ADDON"},
            )
        ],
    )
    adv = Advisor(cards, stats)
    adv.tracker.proven.add("BEAST")
    return adv


class TavernTransitionTests(unittest.TestCase):
    def test_buy_forms_triple_and_merges_board_and_hand(self) -> None:
        snap = GameSnapshot(
            turn=4,
            tier=2,
            gold=3,
            upgrade_cost=6,
            board=(TMinion(1, "T1_GOOD", "Good T1", 4, 4, 1),),
            hand=(TMinion(2, "T1_GOOD", "Good T1", 3, 3, 1),),
            shop=(TMinion(3, "T1_GOOD", "Good T1", 3, 3, 1),),
        )
        nxt = apply_action(snap, BuyMinion(3, "T1_GOOD", "Good T1", 3))
        self.assertEqual(nxt.gold, 0)
        self.assertEqual(nxt.triples_formed, 1)
        self.assertEqual(len(nxt.board), 0)
        self.assertEqual(len(nxt.hand), 1)
        self.assertTrue(nxt.hand[0].golden)
        self.assertEqual((nxt.hand[0].atk, nxt.hand[0].health), (10, 10))

    def test_legal_actions_respect_board_and_gold_limits(self) -> None:
        full_board = tuple(
            TMinion(i, "T1_WEAK", f"W{i}", 1, 1, 1) for i in range(1, 8)
        )
        snap = GameSnapshot(
            turn=6,
            tier=3,
            gold=2,
            upgrade_cost=5,
            board=full_board,
            hand=(TMinion(20, "T1_GOOD", "Good T1", 3, 3, 1),),
            shop=(TMinion(30, "CORE_1", "Beast Core", 5, 5, 4),),
            shop_spells=(TSpell(40, "SP_GOOD", "Tavern Coin", cost=1, tier=1),),
        )
        acts = legal_actions(snap)
        self.assertFalse(any(isinstance(a, PlayMinion) for a in acts))
        self.assertFalse(any(isinstance(a, BuyMinion) for a in acts))
        self.assertFalse(any(isinstance(a, UpgradeTavern) for a in acts))
        self.assertTrue(any(isinstance(a, BuySpell) for a in acts))
        self.assertTrue(any(isinstance(a, SellMinion) for a in acts))
        self.assertTrue(any(isinstance(a, RollShop) for a in acts))

    def test_unplayable_hand_minion_is_not_offered(self) -> None:
        snap = GameSnapshot(
            turn=2,
            tier=1,
            gold=4,
            upgrade_cost=4,
            hand=(TMinion(99, "BG31_HERO_802pt4", "Immortal", 8, 8, 1, playable=False),),
        )
        acts = legal_actions(snap)
        self.assertFalse(any(isinstance(a, PlayMinion) for a in acts))

    def test_deterministic_spells_battlecries_and_sell_effects(self) -> None:
        # Them Apples buffs shop minions by +1/+2
        snap = GameSnapshot(
            turn=2,
            tier=1,
            gold=4,
            upgrade_cost=4,
            shop=(TMinion(1, "T1_GOOD", "Good T1", 3, 3, 1),),
            shop_spells=(TSpell(10, "BG28_966", "Them Apples", cost=1, tier=1),),
        )
        after_apple = apply_action(snap, BuySpell(10, "BG28_966", "Them Apples", 1))
        self.assertEqual((after_apple.shop[0].atk, after_apple.shop[0].health), (4, 5))

        # Shell Collector battlecry grants +1 gold when played
        snap_shell = GameSnapshot(
            turn=3,
            tier=2,
            gold=0,
            upgrade_cost=6,
            hand=(TMinion(2, "BG23_002", "Shell Collector", 4, 3, 2),),
        )
        after_shell = apply_action(snap_shell, PlayMinion(2, "BG23_002", "Shell Collector"))
        self.assertEqual(after_shell.gold, 1)

        # Sellemental sell adds a 3/3 Water Droplet to hand (+1g from sale)
        snap_sell = GameSnapshot(
            turn=3,
            tier=2,
            gold=0,
            upgrade_cost=6,
            board=(TMinion(5, "BGS_115", "Sellemental", 3, 3, 2),),
        )
        after_sell = apply_action(snap_sell, SellMinion(5, "BGS_115", "Sellemental", 1))
        self.assertEqual(after_sell.gold, 1)
        self.assertEqual(len(after_sell.hand), 1)
        self.assertEqual(after_sell.hand[0].card_id, "BGS_115t")

    def test_hand_spell_and_hero_power_actions(self) -> None:
        snap = GameSnapshot(
            turn=4,
            tier=2,
            gold=2,
            upgrade_cost=5,
            board=(TMinion(1, "T1_GOOD", "Good T1", 3, 3, 1),),
            hand=(TMinion(2, "BG20_GEM", "Blood Gem", 0, 0, 1, card_type="SPELL", cost=0),),
            hero_power_id="HP_1",
            hero_power_name="Activate",
            hero_power_cost=2,
            hero_power_usable=True,
        )
        acts = legal_actions(snap)
        self.assertTrue(any(isinstance(a, PlaySpell) for a in acts))
        self.assertTrue(any(isinstance(a, UseHeroPower) for a in acts))
        after_gem = apply_action(snap, PlaySpell(2, "BG20_GEM", "Blood Gem", 0))
        self.assertEqual((after_gem.board[0].atk, after_gem.board[0].health), (4, 4))
        after_hp = apply_action(after_gem, UseHeroPower("HP_1", "Activate", 2))
        self.assertEqual(after_hp.gold, 0)
        self.assertFalse(after_hp.hero_power_usable)

    def test_minion_pool_filters_absent_tribes(self) -> None:
        adv = _make_advisor()
        adv.tracker.turn = 10
        adv.tracker.last_new_turn = 2  # tracker.complete == True -> MURLOC/DEMON absent
        pool = MinionPool(adv.cards, adv.tracker)
        avail_ids = {c.id for c in pool.available_up_to_tier(4)}
        self.assertIn("CORE_1", avail_ids)
        self.assertNotIn("T1_WEAK", avail_ids)


class TurnPlannerTests(unittest.TestCase):
    def test_turn_1_buys_best_minion_and_plays_it(self) -> None:
        planner = TurnPlanner(_make_advisor())
        snap = GameSnapshot(
            turn=1,
            tier=1,
            gold=3,
            upgrade_cost=5,
            shop=(
                TMinion(1, "T1_TRASH", "Bad Card", 1, 2, 1),
                TMinion(2, "T1_GOOD", "Good T1", 3, 3, 1),
                TMinion(3, "T1_WEAK", "Weak Filler", 1, 1, 1),
            ),
        )
        plan = planner.plan(snap)
        self.assertGreaterEqual(len(plan.actions), 2)
        self.assertIsInstance(plan.actions[0], BuyMinion)
        self.assertEqual(plan.actions[0].card_id, "T1_GOOD")
        self.assertIsInstance(plan.actions[1], PlayMinion)
        self.assertEqual(plan.end_state.gold, 0)
        self.assertEqual(len(plan.end_state.board), 1)

    def test_turn_2_prefers_curve_upgrade_over_weak_buy(self) -> None:
        planner = TurnPlanner(_make_advisor())
        snap = GameSnapshot(
            turn=2,
            tier=1,
            gold=4,
            upgrade_cost=4,
            board=(TMinion(10, "T1_GOOD", "Good T1", 3, 3, 1),),
            shop=(
                TMinion(1, "T1_TRASH", "Bad Card", 1, 2, 1),
                TMinion(2, "T1_WEAK", "Weak Filler", 1, 1, 1),
                TMinion(3, "T1_TRASH", "Bad Card", 1, 2, 1),
            ),
        )
        plan = planner.plan(snap)
        self.assertTrue(any(isinstance(a, UpgradeTavern) for a in plan.actions))
        self.assertEqual(plan.end_state.tier, 2)

    def test_turn_2_upgrades_to_tier_2_even_when_shop_offers_t1_pair(self) -> None:
        planner = TurnPlanner(_make_advisor())
        snap = GameSnapshot(
            turn=2,
            tier=1,
            gold=4,
            upgrade_cost=4,
            board=(TMinion(10, "T1_GOOD", "Good T1", 3, 3, 1),),
            shop=(
                TMinion(1, "T1_GOOD", "Good T1", 3, 3, 1),
                TMinion(2, "T1_WEAK", "Weak Filler", 1, 1, 1),
                TMinion(3, "T1_TRASH", "Bad Card", 1, 2, 1),
            ),
        )
        plan = planner.plan(snap)
        self.assertIsInstance(plan.actions[0], UpgradeTavern)
        self.assertEqual(plan.end_state.tier, 2)

    def test_casts_them_apples_before_buying_minion(self) -> None:
        planner = TurnPlanner(_make_advisor())
        snap = GameSnapshot(
            turn=2,
            tier=1,
            gold=4,
            upgrade_cost=None,
            shop=(TMinion(1, "T1_GOOD", "Good T1", 3, 3, 1),),
            shop_spells=(TSpell(10, "BG28_966", "Them Apples", cost=1, tier=1),),
        )
        plan = planner.plan(snap)
        # Casting Them Apples first buffs Good T1 in shop to 4/5 before buying it
        self.assertIsInstance(plan.actions[0], BuySpell)
        self.assertIsInstance(plan.actions[1], BuyMinion)
        self.assertEqual((plan.end_state.board[0].atk, plan.end_state.board[0].health), (4, 5))

    def test_full_board_sells_weakest_to_buy_and_play_core_piece(self) -> None:
        planner = TurnPlanner(_make_advisor())
        board = (
            TMinion(1, "CORE_2", "Beast Enabler", 12, 12, 3),
            TMinion(2, "CORE_2", "Beast Enabler", 10, 10, 3),
            TMinion(3, "T1_GOOD", "Good T1", 8, 8, 1),
            TMinion(4, "T1_GOOD", "Good T1", 8, 8, 1),
            TMinion(5, "T1_GOOD", "Good T1", 7, 7, 1),
            TMinion(6, "T1_GOOD", "Good T1", 7, 7, 1),
            TMinion(7, "T1_WEAK", "Weak Filler", 1, 1, 1),
        )
        snap = GameSnapshot(
            turn=7,
            tier=4,
            gold=2,
            upgrade_cost=8,
            board=board,
            shop=(TMinion(50, "CORE_1", "Beast Core", 5, 5, 4),),
        )
        plan = planner.plan(snap)
        sells = [a for a in plan.actions if isinstance(a, SellMinion)]
        buys = [a for a in plan.actions if isinstance(a, BuyMinion)]
        plays = [a for a in plan.actions if isinstance(a, PlayMinion)]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].uid, 7)
        self.assertEqual([b.card_id for b in buys], ["CORE_1"])
        self.assertEqual([p.card_id for p in plays], ["CORE_1"])
        self.assertIsNotNone(plan.target_comp)
        self.assertEqual(plan.target_comp.comp.name, "Beast Deathrattle")

    def test_recommends_roll_instead_of_buying_trash_with_4g(self) -> None:
        planner = TurnPlanner(_make_advisor())
        snap = GameSnapshot(
            turn=5,
            tier=3,
            gold=4,
            upgrade_cost=8,
            board=(
                TMinion(1, "CORE_2", "Beast Enabler", 8, 8, 3),
                TMinion(2, "T1_GOOD", "Good T1", 6, 6, 1),
            ),
            shop=(
                TMinion(10, "T1_TRASH", "Bad Card", 1, 2, 1),
                TMinion(11, "T1_TRASH", "Bad Card", 1, 2, 1),
                TMinion(12, "T1_WEAK", "Weak Filler", 1, 1, 1),
            ),
        )
        plan = planner.plan(snap)
        self.assertEqual(len(plan.actions), 1)
        self.assertIsInstance(plan.actions[0], RollShop)

    def test_freezes_shop_for_unaffordable_triple(self) -> None:
        planner = TurnPlanner(_make_advisor())
        snap = GameSnapshot(
            turn=5,
            tier=3,
            gold=0,
            upgrade_cost=7,
            board=(
                TMinion(1, "CORE_1", "Beast Core", 5, 5, 4),
                TMinion(2, "CORE_1", "Beast Core", 5, 5, 4),
            ),
            shop=(TMinion(3, "CORE_1", "Beast Core", 5, 5, 4),),
        )
        plan = planner.plan(snap)
        self.assertTrue(any(isinstance(a, FreezeShop) for a in plan.actions))

    def test_positioning_optimizer_places_cleave_first_and_taunt_last(self) -> None:
        adv = _make_advisor()
        board = (
            TMinion(1, "TAUNT_1", "Shield Guard", 2, 6, 2),
            TMinion(2, "T1_GOOD", "Good T1", 4, 4, 1),
            TMinion(3, "CLEAVE_1", "Cave Hydra", 6, 4, 4),
        )
        reordered = optimize_positioning(board, None, adv.cards, runs=24)
        self.assertIsNotNone(reordered)
        self.assertEqual(reordered[0].card_id, "CLEAVE_1")
        self.assertEqual(reordered[-1].card_id, "TAUNT_1")

    def test_closed_loop_headless_env_episode(self) -> None:
        adv = _make_advisor()
        planner = TurnPlanner(adv)
        env = BGGymEnv(cards=adv.cards, seed=7, max_turns=5)
        summary = env.play_episode(planner)
        self.assertEqual(summary["turns_survived"], 5)
        self.assertGreaterEqual(summary["final_tier"], 2)
        self.assertGreaterEqual(summary["final_board_size"], 2)
        self.assertGreater(summary["actions_taken"], 5)


if __name__ == "__main__":
    unittest.main()
