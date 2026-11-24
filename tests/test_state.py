"""Unit tests for EntityStore name resolution and BGState projections."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hsbg import parser as P  # noqa: E402
from hsbg.state import BGState, EntityStore, HeroPower, OpponentBoard, OpponentTracker  # noqa: E402


class StateTests(unittest.TestCase):
    def _base_store(self) -> EntityStore:
        s = EntityStore()
        s.apply(P.CreateGame())
        s.apply(P.GameEntityDecl(1, {"TURN": "3"}))
        s.apply(P.PlayerDecl(2, 4, 144115193835963207, 12345, {}))
        s.apply(P.PlayerDecl(3, 12, 0, 0, {}))
        return s

    def test_bob_first_does_not_steal_local_tags(self) -> None:
        s = self._base_store()
        # Bartender Bob tag arrives BEFORE local player battletag.
        s.apply(P.TagChange(P.EntityRef(raw="Bartender Bob", name="Bartender Bob"), "RESOURCES", "99"))
        s.apply(P.TagChange(P.EntityRef(raw="Player#1234", name="Player#1234"), "RESOURCES", "5"))
        s.apply(P.TagChange(P.EntityRef(raw="Player#1234", name="Player#1234"), "RESOURCES_USED", "2"))
        s.apply(P.TagChange(P.EntityRef(raw="Player#1234", name="Player#1234"), "PLAYER_TECH_LEVEL", "2"))

        bg = BGState(s)
        self.assertEqual(bg.gold, 3)
        self.assertEqual(bg.tier, 2)

    def test_shop_spells_and_upgrade_cost(self) -> None:
        s = self._base_store()
        s.apply(
            P.FullEntity(
                10,
                "BG28_512",
                {
                    "CARDTYPE": "BATTLEGROUND_SPELL",
                    "ZONE": "PLAY",
                    "CONTROLLER": "12",
                    "COST": "2",
                    "TECH_LEVEL": "1",
                    "ZONE_POSITION": "4",
                    "FROZEN": "1",
                },
            )
        )
        s.apply(
            P.FullEntity(
                11,
                "TB_BaconShopTechUp04_Button",
                {
                    "CARDTYPE": "GAME_MODE_BUTTON",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                    "COST": "8",
                },
            )
        )
        bg = BGState(s)
        self.assertEqual(len(bg.shop_spells), 1)
        self.assertEqual(bg.shop_spells[0].cost, 2)
        self.assertTrue(bg.shop_spells[0].frozen)
        self.assertEqual(bg.upgrade_cost, 8)

    def test_combat_minions_respect_zone_position_and_damage(self) -> None:
        s = self._base_store()
        s.apply(
            P.FullEntity(
                20,
                "M_RIGHT",
                {
                    "CARDTYPE": "MINION",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                    "ATK": "4",
                    "HEALTH": "6",
                    "DAMAGE": "2",
                    "TECH_LEVEL": "3",
                    "ZONE_POSITION": "2",
                    "VENOMOUS": "1",
                },
            )
        )
        s.apply(
            P.FullEntity(
                21,
                "M_LEFT",
                {
                    "CARDTYPE": "MINION",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                    "ATK": "2",
                    "HEALTH": "2",
                    "TECH_LEVEL": "1",
                    "ZONE_POSITION": "1",
                },
            )
        )
        bg = BGState(s)
        cm = bg.combat_minions(4)
        self.assertEqual([m.card_id for m in cm], ["M_LEFT", "M_RIGHT"])
        self.assertEqual(cm[1].health, 4)
        self.assertTrue(cm[1].venomous)
        self.assertEqual(cm[1].tier, 3)

    def test_hero_power_states(self) -> None:
        s = self._base_store()
        bg = BGState(s)
        self.assertIsNone(bg.hero_power)

        # Active, playable hero power
        s.apply(
            P.FullEntity(
                30,
                "TB_BaconShop_HP_001",
                {
                    "CARDTYPE": "HERO_POWER",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                    "COST": "2",
                    "_name": "Test Power",
                },
            )
        )
        hp = bg.hero_power
        self.assertIsNotNone(hp)
        assert hp is not None
        self.assertEqual(
            hp,
            HeroPower(
                id=30,
                card_id="TB_BaconShop_HP_001",
                name="Test Power",
                cost=2,
                used=False,
                passive=False,
                playable=True,
            ),
        )

        # Used via EXHAUSTED
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "EXHAUSTED", "1"))
        hp = bg.hero_power
        assert hp is not None
        self.assertTrue(hp.used)
        self.assertFalse(hp.playable)

        # Reset EXHAUSTED, used via BACON_HERO_POWER_ACTIVATED
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "EXHAUSTED", "0"))
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "BACON_HERO_POWER_ACTIVATED", "1"))
        hp = bg.hero_power
        assert hp is not None
        self.assertTrue(hp.used)
        self.assertFalse(hp.playable)

        # Reset used, test disabled via HERO_POWER_DISABLED
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "BACON_HERO_POWER_ACTIVATED", "0"))
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "HERO_POWER_DISABLED", "1"))
        hp = bg.hero_power
        assert hp is not None
        self.assertFalse(hp.used)
        self.assertFalse(hp.playable)

        # Passive hero power via HIDE_COST
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "HERO_POWER_DISABLED", "0"))
        s.apply(P.TagChange(P.EntityRef(raw="30", id=30), "HIDE_COST", "1"))
        hp = bg.hero_power
        assert hp is not None
        self.assertTrue(hp.passive)
        self.assertFalse(hp.playable)

    def test_reroll_and_buy_costs(self) -> None:
        s = self._base_store()
        bg = BGState(s)
        # Defaults when entities are absent
        self.assertEqual(bg.reroll_cost, 1)
        self.assertEqual(bg.buy_cost, 3)

        s.apply(
            P.FullEntity(
                40,
                "TB_BaconShop_8p_Reroll_Button",
                {"ZONE": "PLAY", "CONTROLLER": "4", "COST": "0"},
            )
        )
        s.apply(
            P.FullEntity(
                41,
                "TB_BaconShop_DragBuy",
                {"ZONE": "PLAY", "CONTROLLER": "4"},
            )
        )
        self.assertEqual(bg.reroll_cost, 0)
        # No COST tag on DragBuy -> defaults to 3
        self.assertEqual(bg.buy_cost, 3)

        s.apply(P.TagChange(P.EntityRef(raw="41", id=41), "COST", "2"))
        self.assertEqual(bg.buy_cost, 2)

        # Negative cost clamped to 0
        s.apply(P.TagChange(P.EntityRef(raw="41", id=41), "COST", "-1"))
        self.assertEqual(bg.buy_cost, 0)

    def test_hand_spells_and_trinkets(self) -> None:
        s = self._base_store()
        # Hand minion
        s.apply(
            P.FullEntity(
                50,
                "BG_Minion_1",
                {
                    "CARDTYPE": "MINION",
                    "ZONE": "HAND",
                    "CONTROLLER": "4",
                    "ATK": "3",
                    "HEALTH": "4",
                    "ZONE_POSITION": "1",
                },
            )
        )
        # Playable hand spells
        s.apply(
            P.FullEntity(
                51,
                "BG28_Spell_B",
                {
                    "CARDTYPE": "BATTLEGROUND_SPELL",
                    "ZONE": "HAND",
                    "CONTROLLER": "4",
                    "COST": "0",
                    "ZONE_POSITION": "3",
                },
            )
        )
        s.apply(
            P.FullEntity(
                52,
                "BG_Coin",
                {
                    "CARDTYPE": "SPELL",
                    "ZONE": "HAND",
                    "CONTROLLER": "4",
                    "COST": "0",
                    "ZONE_POSITION": "2",
                },
            )
        )
        # Unplayable hand spell (e.g. locked quest/triple placeholder)
        s.apply(
            P.FullEntity(
                53,
                "BG_Unplayable_Spell",
                {
                    "CARDTYPE": "SPELL",
                    "ZONE": "HAND",
                    "CONTROLLER": "4",
                    "ZONE_POSITION": "4",
                    "LITERALLY_UNPLAYABLE": "1",
                },
            )
        )
        # Trinkets: placeholder slot vs actual active trinket
        s.apply(
            P.FullEntity(
                60,
                "BG30_Trinket_1st",
                {
                    "CARDTYPE": "BATTLEGROUND_TRINKET",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                },
            )
        )
        s.apply(
            P.FullEntity(
                61,
                "BG30_Magic_Trinket",
                {
                    "CARDTYPE": "BATTLEGROUND_TRINKET",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                },
            )
        )

        bg = BGState(s)
        # hand remains backward-compatible
        self.assertEqual(len(bg.hand), 4)
        self.assertEqual([sp.card_id for sp in bg.hand_spells], ["BG_Coin", "BG28_Spell_B"])
        self.assertEqual([t.card_id for t in bg.trinkets], ["BG30_Magic_Trinket"])

    def test_next_opponent_and_opponent_tracker(self) -> None:
        s = self._base_store()
        # Local hero entity
        s.apply(
            P.FullEntity(
                70,
                "HERO_LOCAL",
                {
                    "CARDTYPE": "HERO",
                    "ZONE": "PLAY",
                    "CONTROLLER": "4",
                    "HEALTH": "30",
                    "NEXT_OPPONENT_PLAYER_ID": "7",
                },
            )
        )
        s.apply(
            P.TagChange(
                P.EntityRef(raw="Player#1234", name="Player#1234"),
                "HERO_ENTITY",
                "70",
            )
        )
        s.apply(
            P.TagChange(
                P.EntityRef(raw="Player#1234", name="Player#1234"),
                "PLAYER_TECH_LEVEL",
                "3",
            )
        )

        bg = BGState(s)
        self.assertEqual(bg.next_opponent_id, 7)

        # Player tag override for NEXT_OPPONENT_PLAYER_ID
        s.apply(
            P.TagChange(
                P.EntityRef(raw="Player#1234", name="Player#1234"),
                "NEXT_OPPONENT_PLAYER_ID",
                "5",
            )
        )
        self.assertEqual(bg.next_opponent_id, 5)

        tracker = OpponentTracker()
        # Not in combat yet (TURN=3 -> shop phase)
        self.assertFalse(bg.in_combat)
        self.assertIsNone(tracker.observe_combat(bg))

        # Enter combat (TURN=4 -> turn 2 combat)
        s.apply(P.TagChange(P.EntityRef(raw="GameEntity", name="GameEntity"), "TURN", "4"))
        self.assertTrue(bg.in_combat)

        # Enemy hero and enemy minion controlled by Bob (player_id=12)
        s.apply(
            P.FullEntity(
                80,
                "TB_BaconShop_HERO_Enemy5",
                {
                    "CARDTYPE": "HERO",
                    "ZONE": "PLAY",
                    "CONTROLLER": "12",
                    "PLAYER_ID": "5",
                    "PLAYER_TECH_LEVEL": "4",
                },
            )
        )
        s.apply(
            P.FullEntity(
                81,
                "ENEMY_MINION_1",
                {
                    "CARDTYPE": "MINION",
                    "ZONE": "PLAY",
                    "CONTROLLER": "12",
                    "ATK": "5",
                    "HEALTH": "6",
                    "TECH_LEVEL": "3",
                    "ZONE_POSITION": "1",
                },
            )
        )

        self.assertIsNotNone(bg.combat_enemy_hero)
        observed = tracker.observe_combat(bg)
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(observed.hero_card_id, "TB_BaconShop_HERO_Enemy5")
        self.assertEqual(observed.player_id, 5)
        self.assertEqual(observed.tier, 4)
        self.assertEqual(observed.turn_seen, 2)
        self.assertEqual(len(observed.minions), 1)
        self.assertEqual(observed.minions[0].card_id, "ENEMY_MINION_1")

        # Expected opponent board when next_opponent_id == 5
        self.assertEqual(
            [m.card_id for m in tracker.expected_opponent_board(bg)],
            ["ENEMY_MINION_1"],
        )

        # When next opponent is unknown (id 8), fall back to last_enemy_board
        s.apply(
            P.TagChange(
                P.EntityRef(raw="Player#1234", name="Player#1234"),
                "NEXT_OPPONENT_PLAYER_ID",
                "8",
            )
        )
        self.assertEqual(
            [m.card_id for m in tracker.expected_opponent_board(bg)],
            ["ENEMY_MINION_1"],
        )

        tracker.reset()
        self.assertEqual(tracker.boards, {})
        self.assertEqual(tracker.by_player_id, {})
        self.assertEqual(tracker.last_enemy_board, [])


if __name__ == "__main__":
    unittest.main()
