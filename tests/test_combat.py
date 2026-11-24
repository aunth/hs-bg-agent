"""Unit tests for the Battlegrounds combat simulator."""
from __future__ import annotations

import random
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hsbg.combat import (  # noqa: E402
    AURAS,
    Battle,
    CLEAVE_CARDS,
    CMinion,
    DEATHRATTLES,
    ON_ATTACK,
    START_OF_COMBAT,
    simulate,
)


def M(a: int, h: int, **kw) -> CMinion:
    kw.setdefault("name", f"{a}/{h}")
    return CMinion(atk=a, health=h, **kw)


class CombatTests(unittest.TestCase):
    def test_deterministic_outcomes(self) -> None:
        cases = [
            ("1/1 vs 1/1 -> tie", [M(1, 1)], [M(1, 1)], "tie", 100.0),
            ("3/3 vs 1/1 -> win", [M(3, 3)], [M(1, 1)], "win", 100.0),
            ("1/1 vs 3/3 -> loss", [M(1, 1)], [M(3, 3)], "loss", 100.0),
            ("empty vs 1/1 -> loss", [], [M(1, 1)], "loss", 100.0),
            ("empty vs empty -> tie", [], [], "tie", 100.0),
            ("divine shield 1/1 vs 1/1 -> win", [M(1, 1, divine_shield=True)], [M(1, 1)], "win", 100.0),
            ("poison 1/1 vs 5/5 -> tie", [M(1, 1, poisonous=True)], [M(5, 5)], "tie", 100.0),
            ("0-attack 0/5 vs 1/1 -> loss", [M(0, 5)], [M(1, 1)], "loss", 100.0),
            ("2x 1/1 vs 1x 1/1 -> win", [M(1, 1), M(1, 1)], [M(1, 1)], "win", 100.0),
        ]
        for label, f, e, expect, pct in cases:
            with self.subTest(label=label):
                r = simulate(f, e, runs=200, seed=7)
                got = {"win": r.win_pct, "tie": r.tie_pct, "loss": r.loss_pct}[expect]
                self.assertAlmostEqual(got, pct, places=6)

    def test_taunt_soak(self) -> None:
        r = simulate(
            [CMinion(name="atk", atk=1, health=10)],
            [
                CMinion(name="taunt", atk=0, health=1, taunt=True),
                CMinion(name="big", atk=0, health=99),
            ],
            runs=100,
            seed=3,
        )
        self.assertEqual(r.win_pct, 100.0)

    def test_venomous_consumed_on_first_hit(self) -> None:
        # 1/10 venomous kills first 1/20, loses venomous, then loses to second 2/20.
        r = simulate(
            [M(1, 10, venomous=True), M(0, 1)],
            [M(1, 20, taunt=True), M(2, 20)],
            runs=100,
            seed=1,
        )
        # First minion has 2 minions vs 2, deterministic after taunt dies:
        # 1/10 kills taunt (takes 1 -> 1/9, venomous spent), then trades 1 dmg vs 2 dmg -> loses.
        self.assertEqual(r.loss_pct, 100.0)

    def test_venomous_not_consumed_by_divine_shield(self) -> None:
        # First hit pops divine shield without consuming venomous; second hit kills 1/20.
        r = simulate(
            [M(1, 10, venomous=True)],
            [M(1, 20, divine_shield=True)],
            runs=100,
            seed=1,
        )
        self.assertEqual(r.win_pct, 100.0)

    def test_reborn_restores_base_attack(self) -> None:
        # Buffed 10/1 reborn minion with base_atk=2 revives as 2/1, not 10/1.
        # Against a 1/5 enemy: first hit doesn't happen if enemy attacks first? Force friendly first (2 vs 1).
        r = simulate(
            [M(10, 1, reborn=True, base_atk=2), M(0, 1)],
            [M(1, 15)],
            runs=100,
            seed=2,
        )
        # Turn 1 (friendly): 10/1 hits 1/15 -> 1/5; 10/1 dies and reborns as 2/1.
        # Turn 2 (enemy): 1/5 hits either 2/1 or 0/1; either way 2/1 cannot deal 5 dmg, so enemy wins.
        self.assertEqual(r.loss_pct, 100.0)

    def test_tier_based_damage(self) -> None:
        b = Battle(
            [M(5, 5, tier=4), M(5, 5, tier=2)],
            [M(1, 1, tier=1)],
            rng=random.Random(1),
            tavern_tiers=(5, 3),
        )
        winner, dmg = b.run()
        self.assertEqual(winner, 0)
        # Tavern tier 5 + surviving tiers (4 + 2) = 11
        self.assertEqual(dmg, 11)

    def test_cursor_tracks_summon_on_death(self) -> None:
        # Side 0: [A(1/1, spawns token), B(5/5)]. Side 1: [E(1/2, taunt), F(0/10)].
        # A attacks E and dies, summoning token at pos 0. Next attacker for side 0 must be B, not token.
        cid = "__TEST_TOKEN_DR__"

        def spawn_token(battle, player, pos, _minion):
            battle.summon(player, pos, M(1, 1, name="token"))

        DEATHRATTLES[cid] = spawn_token
        try:
            b = Battle(
                [M(1, 1, card_id=cid, name="A"), M(5, 5, name="B")],
                [M(1, 2, taunt=True, name="E")],
                rng=random.Random(0),
            )
            b.start()
            self.assertEqual(b.turn, 0)
            b.attack_round()  # A attacks E, dies, token spawned at 0, B is at 1
            self.assertEqual(b.sides[0].next_attacker, 1)
            self.assertEqual(b.sides[0].minions[1].name, "B")
        finally:
            DEATHRATTLES.pop(cid, None)

    def test_clone_preserves_fields(self) -> None:
        m = CMinion(
            card_id="BG26_817",
            name="Blade Collector",
            atk=3,
            health=2,
            tier=4,
            taunt=True,
            divine_shield=True,
            tribe="Pirate",
            cleave=True,
        )
        c = m.clone()
        self.assertIsNot(c, m)
        self.assertEqual(c.card_id, "BG26_817")
        self.assertEqual(c.tribe, "Pirate")
        self.assertTrue(c.cleave)
        self.assertTrue(c.taunt)
        self.assertTrue(c.divine_shield)

    def test_cleave_damages_adjacent_minions(self) -> None:
        # 5/5 cleave attacking middle taunt (0/5) also kills adjacent 4/5 minions.
        f = [CMinion(card_id="BG_LOE_089", name="Cave Hydra", atk=5, health=5, cleave=True)]
        e = [
            M(4, 5, name="Left"),
            M(0, 5, name="Center", taunt=True),
            M(4, 5, name="Right"),
        ]
        r = simulate(f, e, runs=50, seed=1)
        self.assertEqual(r.win_pct, 100.0)
        self.assertIn("BG_GVG_113", CLEAVE_CARDS)
        self.assertIn("BG26_817", CLEAVE_CARDS)

    def test_humming_bird_aura_and_death_removal(self) -> None:
        # Humming Bird (BG26_805) gives other friendly Beasts +1 Attack (+2 when golden) while alive.
        self.assertIn("BG26_805", AURAS)
        bird = CMinion(card_id="BG26_805", name="Humming Bird", atk=1, health=4, tribe="Beast")
        beast = CMinion(card_id="BG31_803", name="Alley Beast", atk=2, health=3, tribe="Beast")
        b = Battle([bird, beast], [M(10, 10)], rng=random.Random(1))
        b.start()
        # Bird does not buff itself (1 atk), other beast gets +1 atk -> 3 atk.
        self.assertEqual(b.sides[0].minions[0].atk, 1)
        self.assertEqual(b.sides[0].minions[1].atk, 3)
        # Kill Humming Bird; aura bonus must be removed from surviving beast.
        b.sides[0].minions[0].health = 0
        b.check_for_deaths()
        self.assertEqual(len(b.sides[0].minions), 1)
        self.assertEqual(b.sides[0].minions[0].atk, 2)

    def test_murloc_warleader_and_southsea_captain_auras(self) -> None:
        warleader = CMinion(card_id="BG_EX1_507", name="Murloc Warleader", atk=3, health=3, tribe="Murloc")
        murloc = CMinion(name="Murloc", atk=1, health=1, tribe="Murloc")
        captain = CMinion(card_id="BG_NEW1_027", name="Southsea Captain", atk=3, health=3, tribe="Pirate", golden=True)
        pirate = CMinion(name="Pirate", atk=2, health=2, tribe="Pirate")
        b = Battle([warleader, murloc, captain, pirate], [M(1, 1)], rng=random.Random(1))
        b.start()
        self.assertEqual(b.sides[0].minions[1].atk, 3)      # +2 Attack from Warleader
        self.assertEqual(b.sides[0].minions[3].atk, 4)      # +2/+2 from Golden Southsea Captain
        self.assertEqual(b.sides[0].minions[3].health, 4)

    def test_start_of_combat_triggers(self) -> None:
        self.assertIn("BG26_963", START_OF_COMBAT)
        synth = CMinion(card_id="BG26_963", name="Electric Synthesizer", atk=3, health=4, tribe="Dragon")
        whelp = CMinion(card_id="BG29_888", name="Glim Guardian", atk=1, health=4, tribe="Dragon")
        b = Battle([synth, whelp], [M(1, 1)], rng=random.Random(1))
        b.start()
        # Electric Synthesizer buffs other friendly Dragons by +1/+1 at start of combat.
        self.assertEqual(b.sides[0].minions[1].atk, 2)
        self.assertEqual(b.sides[0].minions[1].health, 5)

    def test_on_attack_glim_guardian_log_turn4(self) -> None:
        # Exact reproduction of bg_t 2 (raw 4) from Hearthstone_2026_11_23_22_25_23/Power.log:
        # Glim Guardian (1/4) + Aberrant Tentacle (0/2 Taunt) vs Joyous (2/3).
        # Glim Guardian attacks first, Rally gives +2 Attack -> 3/4, one-shots 2/3 Joyous -> 100% WIN.
        self.assertIn("BG29_888", ON_ATTACK)
        mine = [
            CMinion(card_id="BG29_888", name="Glim Guardian", atk=1, health=4),
            CMinion(card_id="BGFYM_002t", name="Aberrant Tentacle", atk=0, health=2, taunt=True),
        ]
        opp = [CMinion(card_id="BG36_110", name="Joyous", atk=2, health=3)]
        r = simulate(mine, opp, runs=100, seed=7)
        self.assertEqual(r.win_pct, 100.0)

    def test_deathrattle_token_summoners_and_golden(self) -> None:
        # Buzzing Vermin (BG31_803): 1/1 -> summons 2/2 Beetle (two if golden)
        b = Battle(
            [CMinion(card_id="BG31_803", name="Buzzing Vermin", atk=1, health=1, golden=True)],
            [M(1, 3)],
            rng=random.Random(1),
        )
        winner, _ = b.run()
        self.assertEqual(winner, 0)

        # Forest Rover (BG31_801) buffs beetles (+2/+1) and summons a 3/3 (or 4/3) Beetle
        b2 = Battle(
            [CMinion(card_id="BG31_801", name="Forest Rover", atk=1, health=1)],
            [M(2, 2)],
            rng=random.Random(1),
        )
        winner2, _ = b2.run()
        self.assertEqual(winner2, 0)

        # Harmless Bonehead (BG28_300), Cord Puller (BG29_611), Handless Forsaken (BG25_010), Voidlord (BG_LOOT_368)
        for cid in (
            "BG31_803", "BG31_801", "BG28_300", "BG29_611", "BG26_800",
            "BG25_010", "BGS_061", "BG_KAR_005", "BG_CFM_316", "OG_216",
            "BG_EX1_556", "BOT_445", "BG_BOT_312", "BGS_014", "BG21_029",
            "BOT_537", "BGS_006", "BG26_804", "BG_LOOT_368",
        ):
            self.assertIn(cid, DEATHRATTLES, f"{cid} missing from DEATHRATTLES")

    def test_scallywag_immediate_attack(self) -> None:
        # Scallywag (3/1) dies against 3/2, summons 1/1 Sky Pirate that attacks immediately and kills the 3/2 (now 3/0 -> wait, 3/1 hits 1/2 -> 1/2 dies, so test 1/1 Scallywag vs 1/2):
        # 1/1 Scallywag hits 1/2 -> 1/1 left; Scallywag dies, 1/1 Pirate attacks immediately and finishes 1/1 -> Tie!
        r = simulate(
            [CMinion(card_id="BGS_061", name="Scallywag", atk=1, health=1)],
            [M(1, 2)],
            runs=50,
            seed=1,
        )
        self.assertEqual(r.tie_pct, 100.0)

    def test_stat_buffer_shield_damage_deathrattles(self) -> None:
        for cid in (
            "BG_OG_256", "BG_OG_221", "BGS_018", "BGS_040",
            "BG_BOT_606", "BG29_877", "BGS_030", "BG29_815", "BG21_000",
        ):
            self.assertIn(cid, DEATHRATTLES, f"{cid} missing from DEATHRATTLES")

        # Selfless Hero gives Divine Shield to friendly minion
        b = Battle(
            [
                CMinion(card_id="BG_OG_221", name="Selfless Hero", atk=2, health=1, taunt=True),
                M(5, 1, name="Glass Cannon"),
            ],
            [M(5, 5)],
            rng=random.Random(1),
        )
        # Enemy attacks Taunt Selfless Hero -> Selfless dies and shields 5/1 Glass Cannon -> 5/1 kills 5/5 and survives!
        b.turn = 1
        b.attack_round()
        self.assertTrue(b.sides[0].minions[0].divine_shield)

        # Leapfrogger propagates deathrattle to friendly Beast
        b_leap = Battle(
            [
                CMinion(card_id="BG21_000", name="Leapfrogger", atk=1, health=1, taunt=True, tribe="Beast"),
                CMinion(name="Beast1", atk=1, health=1, taunt=True, tribe="Beast"),
                CMinion(name="Beast2", atk=1, health=5, tribe="Beast"),
            ],
            [M(1, 10)],
            rng=random.Random(1),
        )
        b_leap.start()
        b_leap.single_attack(1, 0)  # enemy hits Leapfrogger -> dies -> hops onto Beast1 or Beast2
        self.assertTrue(any(m.extra_deathrattles for m in b_leap.sides[0].minions))

    def test_simulation_throughput_over_5000_per_sec(self) -> None:
        friendly = [
            CMinion(card_id="BG29_888", name="Glim Guardian", atk=2, health=4, tier=1),
            CMinion(card_id="BG29_611", name="Cord Puller", atk=1, health=1, tier=1, divine_shield=True),
            CMinion(card_id="BG31_803", name="Buzzing Vermin", atk=1, health=1, tier=1, taunt=True),
            CMinion(card_id="BG_TTN_401", name="Ancestral Automaton", atk=5, health=6, tier=2),
        ]
        enemy = [
            CMinion(card_id="BG25_008", name="Eternal Knight", atk=4, health=2, tier=2, reborn=True),
            CMinion(card_id="BG29_611", name="Cord Puller", atk=1, health=1, tier=1, divine_shield=True),
            CMinion(card_id="BGS_004", name="Wrath Weaver", atk=3, health=5, tier=1),
            CMinion(card_id="BG32_237", name="Intrepid Botanist", atk=3, health=4, tier=2),
        ]
        runs = 2500
        t0 = time.perf_counter()
        simulate(friendly, enemy, runs=runs, seed=42)
        elapsed = time.perf_counter() - t0
        sims_per_sec = runs / max(elapsed, 1e-9)
        self.assertGreaterEqual(
            sims_per_sec,
            5000.0,
            f"Expected >= 5,000 sims/sec on 4v4 board, got {sims_per_sec:.0f} sims/sec",
        )


if __name__ == "__main__":
    unittest.main()
