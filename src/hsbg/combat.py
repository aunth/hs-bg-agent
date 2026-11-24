"""Battlegrounds combat simulator.

Resolution order follows twanvl's simulator (MIT licence,
github.com/twanvl/hearthstone-battlegrounds-simulator), which reverse-engineered
these rules by testing against the live game. The subtle parts, all of which
produce wrong results if done naively:

* The side with **more minions** attacks first; a tie is decided randomly.
* Each board keeps a rotating ``next_attacker`` cursor. Minions attack
  left-to-right, wrapping around, and any minion with 0 attack is skipped
  entirely rather than losing its turn.
* Deaths are resolved in **two phases**: every dead minion is collected and
  removed from the board first, and only then do their deathrattles run. The
  loop repeats while new deaths appear. Running each deathrattle as its minion
  dies gives different -- wrong -- results when minions die simultaneously.
* The defender's counter-damage uses a **snapshot** of its attack taken before
  the exchange, since triggers may change it mid-attack.
* Divine Shield absorbs a hit entirely and is consumed. Poison zeroes health
  only if the hit landed (a shielded hit does not poison).
* Battle ends when both sides in succession have no minion able to attack.

Board state comes from the log with live tags, so buffs and shields granted in
the tavern are already reflected -- no need to derive keywords from card text.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BOARD_SIZE = 7

# Known cleave card IDs (Cave Hydra, Foe Reaper 4000, Blade Collector, etc.)
CLEAVE_CARDS: set[str] = {
    "BG_LOE_089",
    "BG_LOOT_078",
    "TB_BaconUps_151",
    "BG_GVG_113",
    "TB_BaconUps_153",
    "BG26_817",
    "BG26_817_G",
}

# Fallback tribe map for cards & tokens when CMinion.tribe is not explicitly populated.
_CARD_TRIBES: dict[str, str] = {
    "BG26_805": "Beast",
    "BG26_805_G": "Beast",
    "BG31_803": "Beast",
    "BG31_803_G": "Beast",
    "BG31_801": "Beast",
    "BG31_801_G": "Beast",
    "BG28_603t": "Beast",
    "BG28_603t_G": "Beast",
    "BG36_200": "Beast",
    "BG36_200_G": "Beast",
    "BG36_200t": "Beast",
    "BG26_800": "Beast",
    "BG26_800_G": "Beast",
    "BG26_800t": "Beast",
    "BG26_800_Gt": "Beast",
    "BG_KAR_005": "Beast",
    "KAR_005": "Beast",
    "TB_BaconUps_004": "Beast",
    "BG_KAR_005a": "Beast",
    "TB_BaconUps_004t": "Beast",
    "BG_CFM_316": "Beast",
    "CFM_316": "Beast",
    "TB_BaconUps_027": "Beast",
    "BG_CFM_316t": "Beast",
    "TB_BaconUps_027t": "Beast",
    "OG_216": "Beast",
    "BG_OG_216": "Beast",
    "TB_BaconUps_026": "Beast",
    "OG_216a": "Beast",
    "BG26_803": "Beast",
    "BG26_803_G": "Beast",
    "BG26_804": "Beast",
    "BG26_804_G": "Beast",
    "BG26_803t": "Beast",
    "BG26_803_Gt": "Beast",
    "BGS_018": "Beast",
    "TB_BaconUps_085": "Beast",
    "BG21_000": "Beast",
    "BG21_000_G": "Beast",
    "BG34_Giant_031": "Beast",
    "BG34_Giant_031_G": "Beast",
    "BG_LOOT_078": "Beast",
    "BG_LOE_089": "Beast",
    "TB_BaconUps_151": "Beast",
    "BG31_807": "Beast",
    "BG31_807_G": "Beast",
    "BG31_808": "Beast",
    "BG31_808_G": "Beast",
    "BG31_809": "Beast",
    "BG31_809_G": "Beast",
    "BG32_204": "Beast",
    "BG32_204_G": "Beast",
    "BG36_202": "Beast",
    "BG36_202_G": "Beast",
    "BG36_207": "Beast",
    "BG36_207_G": "Beast",
    "BG36_209": "Beast",
    "BG36_209_G": "Beast",
    "BG35_604": "Beast",
    "BG35_604_G": "Beast",
    "BG19_010": "Beast",
    "TB_BaconUps_250": "Beast",
    "BG19_010t": "Beast",
    "TB_BaconUps_250t": "Beast",
    "BG28_300": "Undead",
    "BG28_300_G": "Undead",
    "BG_ICC_026t": "Undead",
    "BG_ICC_026t_G": "Undead",
    "BG25_001": "Undead",
    "BG25_001_G": "Undead",
    "BG25_010": "Undead",
    "BG25_010_G": "Undead",
    "BG25_010t": "Undead",
    "BG25_010_Gt": "Undead",
    "BG25_008": "Undead",
    "BG25_008_G": "Undead",
    "BG25_009": "Undead",
    "BG25_009_G": "Undead",
    "BG25_011": "Undead",
    "BG25_022": "Undead",
    "BG25_022_G": "Undead",
    "BG28_309": "Undead",
    "BG28_309_G": "Undead",
    "BG30_125": "Undead",
    "BG30_125_G": "Undead",
    "BG29_611": "Mechanical",
    "BG29_611_G": "Mechanical",
    "BG_BOT_312": "Mechanical",
    "BOT_312": "Mechanical",
    "TB_BaconUps_032": "Mechanical",
    "BG_BOT_312t": "Mechanical",
    "TB_BaconUps_032t": "Mechanical",
    "BG_EX1_556": "Mechanical",
    "EX1_556": "Mechanical",
    "TB_BaconUps_006": "Mechanical",
    "BG_EX1_556t": "Mechanical",
    "TB_BaconUps_006t": "Mechanical",
    "BOT_445": "Mechanical/Beast",
    "BG_BOT_445": "Mechanical/Beast",
    "TB_BaconUps_002": "Mechanical/Beast",
    "BOT_445t": "Mechanical/Beast",
    "TB_BaconUps_002t": "Mechanical",
    "BOT_537": "Mechanical",
    "BG_BOT_537": "Mechanical",
    "TB_BaconUps_039": "Mechanical",
    "BOT_537t": "Mechanical/Beast",
    "TB_BaconUps_039t": "Mechanical/Beast",
    "BGS_006": "Mechanical",
    "TB_BaconUps_080": "Mechanical",
    "BG_BOT_606": "Mechanical",
    "BOT_606": "Mechanical",
    "TB_BaconUps_028": "Mechanical",
    "BG_GVG_113": "Mechanical",
    "TB_BaconUps_153": "Mechanical",
    "BGS_014": "Demon",
    "TB_BaconUps_113": "Demon",
    "BG21_029": "Demon",
    "BG21_029_G": "Demon",
    "BG_BRM_006t": "Demon",
    "TB_BaconUps_030t": "Demon",
    "BG_LOOT_368": "Demon",
    "LOOT_368": "Demon",
    "TB_BaconUps_059": "Demon",
    "BG_CS2_065": "Demon",
    "TB_BaconUps_059t": "Demon",
    "YOD_026": "Demon",
    "CORE_YOD_026": "Demon",
    "TB_BaconUps_112": "Demon",
    "BG29_877": "Elemental/Demon",
    "BG29_877_G": "Elemental/Demon",
    "BGS_061": "Pirate",
    "TB_BaconUps_141": "Pirate",
    "BGS_061t": "Pirate",
    "TB_BaconUps_141t": "Pirate",
    "BG_NEW1_027": "Pirate",
    "NEW1_027": "Pirate",
    "TB_BaconUps_136": "Pirate",
    "BG26_817": "Pirate",
    "BG26_817_G": "Pirate",
    "BG30_119": "Pirate",
    "BG30_119_G": "Pirate",
    "BG32_236": "Pirate",
    "BG33_828": "Pirate",
    "BG33_828_G": "Pirate",
    "BG_EX1_507": "Murloc",
    "EX1_507": "Murloc",
    "TB_BaconUps_008": "Murloc",
    "BGS_030": "Murloc",
    "TB_BaconUps_100": "Murloc",
    "BG33_318": "Murloc",
    "BG33_318_G": "Murloc",
    "BG29_888": "Dragon",
    "BG29_888_G": "Dragon",
    "BG29_815": "Undead/Dragon",
    "BG29_815_G": "Undead/Dragon",
    "BG24_004": "Dragon",
    "BG24_004_G": "Dragon",
    "BG24_500": "Dragon",
    "BG24_500_G": "Dragon",
    "BG26_963": "Dragon",
    "BG26_963_G": "Dragon",
    "BG36_245": "Dragon",
    "BG36_245_G": "Dragon",
    "BG36_364": "Dragon",
    "BG36_364_G": "Dragon",
    "BG36_849": "Dragon",
    "BG36_849_G": "Dragon",
    "BGS_045": "Dragon",
    "TB_BaconUps_115": "Dragon",
    "BG31_810": "Elemental",
    "BG31_810_G": "Elemental",
    "BG31_925": "Naga",
    "BG31_925_G": "Naga",
    "BGFYM_002t": "Aberration",
    "BG36_116": "Aberration",
    "BG36_116_G": "Aberration",
}


def _load_extra_tribes() -> None:
    path = Path(__file__).resolve().parent.parent.parent / "data" / "cards.json"
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for c in raw:
            cid = c.get("id")
            if not cid or cid in _CARD_TRIBES:
                continue
            races = c.get("races") or ([c["race"]] if c.get("race") else [])
            if races:
                _CARD_TRIBES[cid] = "All" if "ALL" in races else "/".join(r.title() for r in races)
    except Exception:
        pass


_load_extra_tribes()


@dataclass
class CMinion:
    card_id: str = ""
    name: str = ""
    atk: int = 0
    health: int = 0
    tier: int = 1
    taunt: bool = False
    divine_shield: bool = False
    poisonous: bool = False
    venomous: bool = False
    windfury: bool = False
    mega_windfury: bool = False
    reborn: bool = False
    golden: bool = False
    base_atk: int | None = None
    base_health: int | None = None
    tribe: str = ""
    cleave: bool = False
    extra_deathrattles: tuple[Callable, ...] = ()
    _aura_atk: int = 0
    _aura_health: int = 0

    def clone(self) -> CMinion:
        """Fast direct copy without dataclasses.replace reflection overhead."""
        return CMinion(
            self.card_id,
            self.name,
            self.atk,
            self.health,
            self.tier,
            self.taunt,
            self.divine_shield,
            self.poisonous,
            self.venomous,
            self.windfury,
            self.mega_windfury,
            self.reborn,
            self.golden,
            self.base_atk,
            self.base_health,
            self.tribe,
            self.cleave,
            self.extra_deathrattles,
            self._aura_atk,
            self._aura_health,
        )

    @property
    def dead(self) -> bool:
        return self.health <= 0

    def num_attacks(self) -> int:
        return 4 if self.mega_windfury else (2 if self.windfury else 1)

    def __str__(self) -> str:
        f = "".join(
            c
            for c, on in (
                ("T", self.taunt),
                ("D", self.divine_shield),
                ("P", self.poisonous),
                ("V", self.venomous),
                ("W", self.windfury or self.mega_windfury),
                ("R", self.reborn),
            )
            if on
        )
        return f"{self.name or self.card_id} {self.atk}/{self.health}{f and '[' + f + ']'}"


def has_tribe(minion: CMinion, target_tribe: str) -> bool:
    """Return True if minion belongs to target_tribe (or is 'All')."""
    if not target_tribe:
        return True
    t = minion.tribe or _CARD_TRIBES.get(minion.card_id, "")
    if not t:
        return False
    tl = t.lower()
    if tl == "all":
        return True
    target_l = target_tribe.lower()
    return target_l == tl or target_l in tl.split("/")


def _is_golden(minion: CMinion) -> bool:
    return (
        minion.golden
        or minion.card_id.endswith("_G")
        or minion.card_id.startswith("TB_BaconUps_")
    )


# card_id -> (target_tribe, atk_bonus, health_bonus) or callable(source, target) -> (atk, hp)
# Grants aura stats to other friendly minions of target_tribe while source is alive.
AURAS: dict[str, tuple[str, int, int] | Callable] = {
    "BG26_805": ("Beast", 1, 0),          # Humming Bird (+1 Attack to other Beasts; +2 if golden)
    "BG26_805_G": ("Beast", 2, 0),
    "BG_EX1_507": ("Murloc", 2, 0),       # Murloc Warleader (+2 Attack to other Murlocs)
    "EX1_507": ("Murloc", 2, 0),
    "TB_BaconUps_008": ("Murloc", 4, 0),
    "BG_EX1_507_G": ("Murloc", 4, 0),
    "BG_NEW1_027": ("Pirate", 1, 1),      # Southsea Captain (+1/+1 to other Pirates)
    "NEW1_027": ("Pirate", 1, 1),
    "TB_BaconUps_136": ("Pirate", 2, 2),
    "BG_NEW1_027_G": ("Pirate", 2, 2),
    "BG_GVG_021": ("Demon", 2, 2),        # Mal'Ganis (+2/+2 to other Demons)
    "GVG_021": ("Demon", 2, 2),
    "TB_BaconUps_060": ("Demon", 4, 4),
    "BG_CS2_122": ("", 1, 0),             # Raid Leader (+1 Attack to other minions)
    "CS2_122": ("", 1, 0),
}

# card_id -> callable(battle, side_idx, pos, minion) run at start of combat.
START_OF_COMBAT: dict[str, Callable] = {}

# card_id -> callable(battle, side_idx, attacker_idx, attacker) run right before an attack.
ON_ATTACK: dict[str, Callable] = {}

# card_id -> callable(battle, side, position, minion) run when the minion dies.
DEATHRATTLES: dict[str, Callable] = {}


def start_of_combat(*card_ids: str):
    def wrap(fn: Callable) -> Callable:
        for cid in card_ids:
            START_OF_COMBAT[cid] = fn
        return fn
    return wrap


def on_attack(*card_ids: str):
    def wrap(fn: Callable) -> Callable:
        for cid in card_ids:
            ON_ATTACK[cid] = fn
        return fn
    return wrap


def deathrattle(*card_ids: str):
    def wrap(fn: Callable) -> Callable:
        for cid in card_ids:
            DEATHRATTLES[cid] = fn
        return fn
    return wrap


class Side:
    def __init__(self, minions: list[CMinion], hero_health: int = 30, tavern_tier: int = 1):
        self.minions = [m.clone() for m in minions]   # never mutate the caller's board
        self.next_attacker = 0
        self.hero_health = hero_health
        self.tavern_tier = tavern_tier
        self.has_auras = any(m.card_id in AURAS or m._aura_atk or m._aura_health for m in self.minions)
        self.beetle_atk_bonus = 0
        self.beetle_hp_bonus = 0
        self.beast_atk_bonus = 0
        self.beast_hp_bonus = 0

    def alive(self) -> bool:
        return bool(self.minions)

    def find_attacker(self) -> int:
        """Next minion able to attack, skipping any with 0 attack."""
        n = len(self.minions)
        if n == 0:
            return -1
        for _ in range(n):
            if self.next_attacker >= n:
                self.next_attacker = 0
            idx = self.next_attacker
            if self.minions[idx].atk > 0:
                return idx
            self.next_attacker += 1
        return -1

    def targets(self) -> list[int]:
        taunts = [i for i, m in enumerate(self.minions) if m.taunt and not m.dead]
        if taunts:
            return taunts
        return [i for i, m in enumerate(self.minions) if not m.dead]

    def tier_total(self) -> int:
        return sum(max(1, m.tier) for m in self.minions)


class Battle:
    def __init__(
        self,
        friendly: list[CMinion],
        enemy: list[CMinion],
        rng: random.Random | None = None,
        tavern_tiers: tuple[int, int] = (1, 1),
    ):
        self.sides = [
            Side(friendly, tavern_tier=tavern_tiers[0]),
            Side(enemy, tavern_tier=tavern_tiers[1]),
        ]
        self.rng = rng or random.Random()
        self.turn = -1

    # -- setup ----------------------------------------------------------
    def start(self) -> None:
        a, b = len(self.sides[0].minions), len(self.sides[1].minions)
        self.turn = 0 if a > b else (1 if b > a else self.rng.randrange(2))

        for s in (0, 1):
            if self.sides[s].has_auras:
                self.recalc_auras(s)

        if START_OF_COMBAT:
            has_soc = any(
                m.card_id in START_OF_COMBAT
                for s in (0, 1)
                for m in self.sides[s].minions
            )
            if has_soc:
                for s in (self.turn, 1 - self.turn):
                    snapshot = list(self.sides[s].minions)
                    for m in snapshot:
                        if m.dead:
                            continue
                        fn = START_OF_COMBAT.get(m.card_id)
                        if fn is not None:
                            pos = next((i for i, cur in enumerate(self.sides[s].minions) if cur is m), -1)
                            if pos != -1:
                                fn(self, s, pos, m)
                                self.check_for_deaths()

    def recalc_auras(self, player: int) -> None:
        side = self.sides[player]
        minions = side.minions
        if not minions:
            side.has_auras = False
            return
        sources = [(m, AURAS[m.card_id]) for m in minions if not m.dead and m.card_id in AURAS]
        if not sources and not side.has_auras:
            return
        side.has_auras = bool(sources)
        for m in minions:
            if m.dead:
                continue
            want_atk = 0
            want_hp = 0
            for src, spec in sources:
                if src is m:
                    continue
                if callable(spec):
                    da, dh = spec(src, m)
                    want_atk += da
                    want_hp += dh
                else:
                    tribe, da, dh = spec
                    if has_tribe(m, tribe):
                        mult = 2 if (src.golden and not src.card_id.endswith("_G") and not src.card_id.startswith("TB_BaconUps_")) else 1
                        want_atk += da * mult
                        want_hp += dh * mult
            if want_atk != m._aura_atk:
                m.atk = max(0, m.atk + (want_atk - m._aura_atk))
                m._aura_atk = want_atk
            if want_hp != m._aura_health:
                diff_hp = want_hp - m._aura_health
                if diff_hp > 0:
                    m.health += diff_hp
                elif m.health > 0:
                    m.health = max(1, m.health + diff_hp)
                m._aura_health = want_hp

    def done(self) -> bool:
        return not (self.sides[0].alive() and self.sides[1].alive())

    # -- combat ---------------------------------------------------------
    def run(self) -> tuple[int, int]:
        """Returns (winner, damage). winner is 0, 1, or -1 for a tie."""
        self.start()
        missed_prev = False
        for _ in range(2000):                      # guard against non-terminating boards
            if self.done():
                break
            ok = self.attack_round()
            if missed_prev and not ok:
                break                              # neither side can attack: stalemate
            missed_prev = not ok
        return self.outcome()

    def attack_round(self) -> bool:
        side = self.sides[self.turn]
        idx = side.find_attacker()
        if idx == -1:
            self.turn = 1 - self.turn
            return False
        attacker = side.minions[idx]
        # Advance cursor past this minion up front; deaths and summons adjust it
        # in place so index shifts to the left never skip or repeat a minion.
        side.next_attacker = idx + 1
        for _ in range(attacker.num_attacks()):
            if attacker.dead or self.done():
                break
            cur_idx = next((i for i, m in enumerate(side.minions) if m is attacker), -1)
            if cur_idx == -1:
                break
            self.single_attack(self.turn, cur_idx)
        self.turn = 1 - self.turn
        return True

    def single_attack(self, player: int, idx: int, check_deaths: bool = True) -> None:
        me, foe = self.sides[player], self.sides[1 - player]
        if not foe.minions:
            return
        attacker = me.minions[idx]
        on_atk = ON_ATTACK.get(attacker.card_id)
        if on_atk is not None:
            on_atk(self, player, idx, attacker)
            if attacker.dead or not foe.minions:
                if check_deaths:
                    self.check_for_deaths()
                return
        valid_targets = foe.targets()
        if not valid_targets:
            return
        target = self.rng.choice(valid_targets)
        defender = foe.minions[target]
        # Snapshot: triggers during the exchange must not change the riposte.
        riposte_atk = defender.atk
        riposte_poison = defender.poisonous
        riposte_venom = defender.venomous

        is_cleave = attacker.cleave or (attacker.card_id in CLEAVE_CARDS)
        adj_targets: list[CMinion] = []
        if is_cleave:
            if target - 1 >= 0:
                adj_targets.append(foe.minions[target - 1])
            if target + 1 < len(foe.minions):
                adj_targets.append(foe.minions[target + 1])

        if self.damage(defender, attacker.atk, attacker.poisonous or attacker.venomous):
            if attacker.venomous:
                attacker.venomous = False

        if adj_targets:
            for adj in adj_targets:
                if self.damage(adj, attacker.atk, attacker.poisonous or attacker.venomous):
                    if attacker.venomous:
                        attacker.venomous = False

        if self.damage(attacker, riposte_atk, riposte_poison or riposte_venom):
            if riposte_venom:
                defender.venomous = False
        if check_deaths:
            self.check_for_deaths()

    def damage(self, m: CMinion, amount: int, poison: bool) -> bool:
        if amount <= 0:
            return False
        if m.divine_shield:
            m.divine_shield = False        # absorbed entirely; poison/venom do not apply
            return False
        m.health -= amount
        if m.health > 0 and poison:
            m.health = 0
        return True

    def check_for_deaths(self) -> None:
        """Collect every dead minion first, then run deathrattles. Repeat."""
        while True:
            corpses = []
            for player in (0, 1):
                side = self.sides[player]
                survivors = []
                removed_before_cursor = 0
                for pos, m in enumerate(side.minions):
                    if m.dead:
                        corpses.append((player, pos, m))
                        if pos < side.next_attacker:
                            removed_before_cursor += 1
                    else:
                        survivors.append(m)
                if removed_before_cursor:
                    side.next_attacker = max(0, side.next_attacker - removed_before_cursor)
                if len(survivors) != len(side.minions):
                    side.minions = survivors
                    if side.has_auras:
                        self.recalc_auras(player)
            if not corpses:
                return
            for player, pos, m in corpses:
                fn = DEATHRATTLES.get(m.card_id)
                if fn is not None or m.extra_deathrattles:
                    dr_mult = 1
                    for fm in self.sides[player].minions:
                        if not fm.dead and fm.card_id in ("BG25_354", "BG25_354_G", "FP1_031", "TB_BaconUps_055"):
                            dr_mult += 2 if _is_golden(fm) else 1
                    for _ in range(dr_mult):
                        if fn is not None:
                            fn(self, player, pos, m)
                        for extra_fn in m.extra_deathrattles:
                            extra_fn(self, player, pos, m)
                if m.reborn:
                    # Reborn summons a fresh copy with 1 HP and unbuffed base attack
                    # when base_atk is known.
                    revived_atk = m.base_atk if m.base_atk is not None else (m.atk - m._aura_atk)
                    revived = m.clone()
                    revived.atk = revived_atk
                    revived.health = 1
                    revived.reborn = False
                    revived.divine_shield = False
                    revived.extra_deathrattles = ()
                    revived._aura_atk = 0
                    revived._aura_health = 0
                    self.summon(player, pos, revived)

    def summon(self, player: int, pos: int, minion: CMinion) -> bool:
        side = self.sides[player]
        if len(side.minions) >= BOARD_SIZE:
            return False
        if (side.beast_atk_bonus or side.beast_hp_bonus) and has_tribe(minion, "Beast"):
            minion.atk += side.beast_atk_bonus
            minion.health += side.beast_hp_bonus
        insert_at = min(pos, len(side.minions))
        side.minions.insert(insert_at, minion)
        if insert_at <= side.next_attacker:
            side.next_attacker += 1
        if minion.card_id in AURAS or side.has_auras:
            side.has_auras = True
            self.recalc_auras(player)
        return True

    # -- result ---------------------------------------------------------
    def outcome(self) -> tuple[int, int]:
        a, b = self.sides[0].alive(), self.sides[1].alive()
        if a and not b:
            return 0, self.damage_dealt(0)
        if b and not a:
            return 1, self.damage_dealt(1)
        return -1, 0

    def damage_dealt(self, winner: int) -> int:
        """Battlegrounds damage: tavern tier plus the tiers of surviving minions."""
        side = self.sides[winner]
        return side.tavern_tier + side.tier_total()


@dataclass
class SimResult:
    runs: int
    wins: int
    ties: int
    losses: int
    avg_damage: float = 0.0

    @property
    def win_pct(self) -> float:
        return self.wins / self.runs * 100 if self.runs else 0.0

    @property
    def tie_pct(self) -> float:
        return self.ties / self.runs * 100 if self.runs else 0.0

    @property
    def loss_pct(self) -> float:
        return self.losses / self.runs * 100 if self.runs else 0.0

    def __str__(self) -> str:
        return (f"win {self.win_pct:.0f}%  tie {self.tie_pct:.0f}%  "
                f"loss {self.loss_pct:.0f}%  (n={self.runs})")


def simulate(
    friendly: list[CMinion],
    enemy: list[CMinion],
    runs: int = 500,
    seed: int | None = None,
    tavern_tiers: tuple[int, int] = (1, 1),
) -> SimResult:
    """Monte Carlo over the combat RNG (first attacker, target selection)."""
    rng = random.Random(seed)
    wins = ties = losses = dmg = 0
    for _ in range(runs):
        winner, d = Battle(friendly, enemy, rng, tavern_tiers=tavern_tiers).run()
        if winner == 0:
            wins += 1
            dmg += d
        elif winner == 1:
            losses += 1
        else:
            ties += 1
    return SimResult(runs, wins, ties, losses, dmg / wins if wins else 0.0)


# ---------------------------------------------------------------------------
# Combat Trigger Implementations (START_OF_COMBAT, ON_ATTACK, DEATHRATTLES)
# ---------------------------------------------------------------------------


def _summon_tokens(
    battle: Battle,
    player: int,
    pos: int,
    count: int,
    card_id: str,
    name: str,
    atk: int,
    health: int,
    tribe: str = "",
    taunt: bool = False,
    reborn: bool = False,
    divine_shield: bool = False,
) -> list[CMinion]:
    summoned: list[CMinion] = []
    for i in range(count):
        tok = CMinion(
            card_id=card_id,
            name=name,
            atk=atk,
            health=health,
            tier=1,
            taunt=taunt,
            reborn=reborn,
            divine_shield=divine_shield,
            base_atk=atk,
            base_health=health,
            tribe=tribe,
        )
        if battle.summon(player, pos + i, tok):
            summoned.append(tok)
    return summoned


def _summon_beetles(battle: Battle, player: int, pos: int, count: int) -> list[CMinion]:
    side = battle.sides[player]
    atk = 2 + side.beetle_atk_bonus
    hp = 2 + side.beetle_hp_bonus
    return _summon_tokens(battle, player, pos, count, "BG28_603t", "Beetle", atk, hp, tribe="Beast")


# -- Start of Combat --------------------------------------------------------


@start_of_combat("BG26_963", "BG26_963_G")
def _soc_electric_synthesizer(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Electric Synthesizer: Give your other Dragons +1/+1 (+2/+2 if golden)."""
    b = 2 if _is_golden(minion) else 1
    for fm in battle.sides[player].minions:
        if fm is not minion and not fm.dead and has_tribe(fm, "Dragon"):
            fm.atk += b
            fm.health += b


@start_of_combat("BG24_500", "BG24_500_G")
def _soc_amber_guardian(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Amber Guardian: Give another friendly Dragon +2/+2 and Divine Shield."""
    g = _is_golden(minion)
    b = 4 if g else 2
    count = 2 if g else 1
    candidates = [
        fm for fm in battle.sides[player].minions
        if fm is not minion and not fm.dead and has_tribe(fm, "Dragon")
    ]
    if not candidates:
        return
    unshielded = [fm for fm in candidates if not fm.divine_shield]
    pool = unshielded if len(unshielded) >= count else candidates
    chosen = battle.rng.sample(pool, min(count, len(pool)))
    for t in chosen:
        t.atk += b
        t.health += b
        t.divine_shield = True


@start_of_combat("BG36_620", "BG36_620_G")
def _soc_boom_in_a_box(battle: Battle, _player: int, _pos: int, minion: CMinion) -> None:
    """Boom-in-a-Box: Deal 3 damage to all other minions (6 if golden)."""
    dmg = 6 if _is_golden(minion) else 3
    for s in (0, 1):
        for m in battle.sides[s].minions:
            if m is not minion and not m.dead:
                battle.damage(m, dmg, False)


@start_of_combat("BG36_364", "BG36_364_G")
def _soc_hopebringer(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Hopebringer: Give your minions +4/+3 (+8/+6 if golden)."""
    m = 2 if _is_golden(minion) else 1
    for fm in battle.sides[player].minions:
        if not fm.dead:
            fm.atk += 4 * m
            fm.health += 3 * m


@start_of_combat("BG31_810", "BG31_810_G")
def _soc_ultraviolet_ascendant(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Ultraviolet Ascendant: Give your other Elementals +4/+4 (+8/+8 if golden)."""
    b = 8 if _is_golden(minion) else 4
    for fm in battle.sides[player].minions:
        if fm is not minion and not fm.dead and has_tribe(fm, "Elemental"):
            fm.atk += b
            fm.health += b


@start_of_combat("BG36_245", "BG36_245_G")
def _soc_runic_arcanist(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Runic Arcanist: Cast Shiny Ring twice (+2/+2 to friendly minions; +4/+4 if golden)."""
    b = 4 if _is_golden(minion) else 2
    for fm in battle.sides[player].minions:
        if not fm.dead:
            fm.atk += b
            fm.health += b


@start_of_combat("BG30_119", "BG30_119_G")
def _soc_sky_pirate_flagbearer(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Sky Pirate Flagbearer: Give your other Pirates 'Deathrattle: Summon a Scallywag.'"""
    g = _is_golden(minion)

    def _spawn_scally(b: Battle, p: int, pos: int, _m: CMinion) -> None:
        cid = "TB_BaconUps_141" if g else "BGS_061"
        stat = 2 if g else 1
        _summon_tokens(b, p, pos, 1, cid, "Scallywag", 3 * stat, stat, tribe="Pirate")

    for fm in battle.sides[player].minions:
        if fm is not minion and not fm.dead and has_tribe(fm, "Pirate"):
            fm.extra_deathrattles = fm.extra_deathrattles + (_spawn_scally,)


# -- On Attack (Rally / Whenever this attacks) ------------------------------


@on_attack("BG29_888", "BG29_888_G")
def _oa_glim_guardian(_battle: Battle, _player: int, _idx: int, attacker: CMinion) -> None:
    """Glim Guardian: Rally: Gain +2 Attack (+4 if golden)."""
    attacker.atk += 4 if _is_golden(attacker) else 2


@on_attack("BG33_886", "BG33_886_G")
def _oa_tusked_camper(_battle: Battle, _player: int, _idx: int, attacker: CMinion) -> None:
    """Tusked Camper: Rally: Plays a Blood Gem (+1/+1) on itself (+2/+2 if golden)."""
    b = 2 if _is_golden(attacker) else 1
    attacker.atk += b
    attacker.health += b


@on_attack("BG36_200", "BG36_200_G")
def _oa_flittering_bat(battle: Battle, player: int, idx: int, attacker: CMinion) -> None:
    """Flittering Bat: Rally: Summon a 1/1 Beast (2/2 if golden)."""
    s = 2 if _is_golden(attacker) else 1
    _summon_tokens(battle, player, idx + 1, 1, "BG36_200t", "Foraging Bat", s, s, tribe="Beast")


@on_attack("BG36_207", "BG36_207_G")
def _oa_wolf_pup(battle: Battle, player: int, _idx: int, attacker: CMinion) -> None:
    """Wolf Pup: Rally: Give your other minions +4/+1 (+8/+2 if golden)."""
    m = 2 if _is_golden(attacker) else 1
    for fm in battle.sides[player].minions:
        if fm is not attacker and not fm.dead:
            fm.atk += 4 * m
            fm.health += 1 * m


@on_attack("BG20_104", "BG20_104_G")
def _oa_bonker(battle: Battle, player: int, _idx: int, attacker: CMinion) -> None:
    """Bonker: Rally: Plays a Blood Gem (+1/+1) on all your other minions."""
    b = 2 if _is_golden(attacker) else 1
    for fm in battle.sides[player].minions:
        if fm is not attacker and not fm.dead:
            fm.atk += b
            fm.health += b


@on_attack("BG33_883", "BG33_883_G")
def _oa_razorfen_vineweaver(_battle: Battle, _player: int, _idx: int, attacker: CMinion) -> None:
    """Razorfen Vineweaver: Rally: Plays 4 Blood Gems on itself."""
    b = 8 if _is_golden(attacker) else 4
    attacker.atk += b
    attacker.health += b


@on_attack("BG36_849", "BG36_849_G")
def _oa_heroic_broodmother(_battle: Battle, _player: int, _idx: int, attacker: CMinion) -> None:
    """Heroic Broodmother: Rally: Gain Divine Shield."""
    attacker.divine_shield = True


@on_attack("BG33_318", "BG33_318_G")
def _oa_bile_spitter(battle: Battle, player: int, _idx: int, attacker: CMinion) -> None:
    """Bile Spitter: Rally: Give another friendly Murloc Venomous."""
    candidates = [
        fm for fm in battle.sides[player].minions
        if fm is not attacker and not fm.dead and has_tribe(fm, "Murloc") and not fm.venomous
    ]
    if candidates:
        count = 2 if _is_golden(attacker) else 1
        for t in battle.rng.sample(candidates, min(count, len(candidates))):
            t.venomous = True


@on_attack("BGS_045", "TB_BaconUps_115")
def _oa_glyph_guardian(_battle: Battle, _player: int, _idx: int, attacker: CMinion) -> None:
    """Glyph Guardian: Whenever this attacks, double its Attack (triple if golden)."""
    attacker.atk *= 3 if _is_golden(attacker) else 2


# -- Deathrattles: Token Summoners ------------------------------------------


@deathrattle("BG31_803", "BG31_803_G", "BG31_808", "BG31_808_G", "BG32_204", "BG32_204_G", "BG36_209", "BG36_209_G")
def _dr_buzzing_vermin(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Buzzing Vermin (and single-Beetle summoners): Summon a 2/2 Beetle (two if golden)."""
    _summon_beetles(battle, player, pos, 2 if _is_golden(minion) else 1)


@deathrattle("BG31_801", "BG31_801_G")
def _dr_forest_rover(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Forest Rover: Buffs beetles (+2/+1) and summons a 2/2 Beetle (two if golden)."""
    g = _is_golden(minion)
    m = 2 if g else 1
    side = battle.sides[player]
    side.beetle_atk_bonus += 2 * m
    side.beetle_hp_bonus += 1 * m
    for fm in side.minions:
        if not fm.dead and (fm.card_id in ("BG28_603t", "BG28_603t_G") or fm.name == "Beetle"):
            fm.atk += 2 * m
            fm.health += 1 * m
    _summon_beetles(battle, player, pos, 2 if g else 1)


@deathrattle("BG31_809", "BG31_809_G")
def _dr_turquoise_skitterer(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Turquoise Skitterer: Your Beetles have +5/+5. Summon a 2/2 Beetle."""
    g = _is_golden(minion)
    m = 2 if g else 1
    side = battle.sides[player]
    side.beetle_atk_bonus += 5 * m
    side.beetle_hp_bonus += 5 * m
    for fm in side.minions:
        if not fm.dead and (fm.card_id in ("BG28_603t", "BG28_603t_G") or fm.name == "Beetle"):
            fm.atk += 5 * m
            fm.health += 5 * m
    _summon_beetles(battle, player, pos, 2 if g else 1)


@deathrattle("BG31_807", "BG31_807_G")
def _dr_nest_swarmer(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Nest Swarmer: Summon three 2/2 Beetles (six if golden)."""
    _summon_beetles(battle, player, pos, 6 if _is_golden(minion) else 3)


@deathrattle("BG28_300", "BG28_300_G")
def _dr_harmless_bonehead(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Harmless Bonehead: Summon two 1/1 Skeletons (four if golden)."""
    count = 4 if _is_golden(minion) else 2
    _summon_tokens(battle, player, pos, count, "BG_ICC_026t", "Skeleton", 1, 1, tribe="Undead")


@deathrattle("BG30_125", "BG30_125_G")
def _dr_cadaver_caretaker(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Cadaver Caretaker: Summon three 1/1 Skeletons (six if golden)."""
    count = 6 if _is_golden(minion) else 3
    _summon_tokens(battle, player, pos, count, "BG_ICC_026t", "Skeleton", 1, 1, tribe="Undead")


@deathrattle("BG29_611", "BG29_611_G")
def _dr_cord_puller(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Cord Puller: Summon a 1/1 Microbot (2/2 if golden)."""
    s = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_032t" if s == 2 else "BG_BOT_312t"
    _summon_tokens(battle, player, pos, 1, cid, "Microbot", s, s, tribe="Mechanical")


@deathrattle("BG26_800", "BG26_800_G")
def _dr_manasaber(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Manasaber: Summon two 0/1 Cublings with Taunt (0/2 if golden)."""
    hp = 2 if _is_golden(minion) else 1
    cid = "BG26_800_Gt" if hp == 2 else "BG26_800t"
    _summon_tokens(battle, player, pos, 2, cid, "Cubling", 0, hp, tribe="Beast", taunt=True)


@deathrattle("BG25_010", "BG25_010_G")
def _dr_handless_forsaken(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Handless Forsaken: Summon a 2/1 Hand with Reborn (two 2/1 Hands if golden)."""
    count = 2 if _is_golden(minion) else 1
    _summon_tokens(battle, player, pos, count, "BG25_010t", "Helping Hand", 2, 1, tribe="Undead", reborn=True)


@deathrattle("BGS_061", "TB_BaconUps_141", "BGS_061_G")
def _dr_scallywag(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Scallywag: Summon a 1/1 Pirate (2/2 if golden). It attacks immediately."""
    s = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_141t" if s == 2 else "BGS_061t"
    toks = _summon_tokens(battle, player, pos, 1, cid, "Sky Pirate", s, s, tribe="Pirate")
    if toks and battle.sides[1 - player].targets():
        tok = toks[0]
        cur_idx = next((i for i, m in enumerate(battle.sides[player].minions) if m is tok), -1)
        if cur_idx != -1:
            battle.single_attack(player, cur_idx, check_deaths=False)


@deathrattle("BG_KAR_005", "KAR_005", "TB_BaconUps_004", "BG_KAR_005_G")
def _dr_kindly_grandmother(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Kindly Grandmother: Summon a 3/2 Big Bad Wolf (6/4 if golden)."""
    m = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_004t" if m == 2 else "BG_KAR_005a"
    _summon_tokens(battle, player, pos, 1, cid, "Big Bad Wolf", 3 * m, 2 * m, tribe="Beast")


@deathrattle("BG_CFM_316", "CFM_316", "TB_BaconUps_027", "BG_CFM_316_G")
def _dr_rat_pack(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Rat Pack: Summon a number of 1/1 Rats (2/2 if golden) equal to this minion's Attack."""
    count = max(0, min(BOARD_SIZE, minion.atk))
    s = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_027t" if s == 2 else "BG_CFM_316t"
    _summon_tokens(battle, player, pos, count, cid, "Rat", s, s, tribe="Beast")


@deathrattle("OG_216", "BG_OG_216", "TB_BaconUps_026", "BG_OG_216_G")
def _dr_infested_wolf(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Infested Wolf: Summon two 1/1 Spiders (2/2 if golden)."""
    s = 2 if _is_golden(minion) else 1
    _summon_tokens(battle, player, pos, 2, "OG_216a", "Spider", s, s, tribe="Beast")


@deathrattle("BG_EX1_556", "EX1_556", "TB_BaconUps_006", "BG_EX1_556_G")
def _dr_harvest_golem(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Harvest Golem: Summon a 2/1 Damaged Golem (4/2 if golden)."""
    m = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_006t" if m == 2 else "BG_EX1_556t"
    _summon_tokens(battle, player, pos, 1, cid, "Damaged Golem", 2 * m, 1 * m, tribe="Mechanical")


@deathrattle("BOT_445", "BG_BOT_445", "TB_BaconUps_002", "BG_BOT_445_G")
def _dr_mecharoo(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Mecharoo: Summon a 1/1 Jo-E Bot (2/2 if golden)."""
    s = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_002t" if s == 2 else "BOT_445t"
    _summon_tokens(battle, player, pos, 1, cid, "Jo-E Bot", s, s, tribe="Mechanical/Beast")


@deathrattle("BG_BOT_312", "BOT_312", "TB_BaconUps_032", "BG_BOT_312_G")
def _dr_replicating_menace(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Replicating Menace: Summon three 1/1 Microbots (2/2 if golden)."""
    s = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_032t" if s == 2 else "BG_BOT_312t"
    _summon_tokens(battle, player, pos, 3, cid, "Microbot", s, s, tribe="Mechanical")


@deathrattle("BGS_014", "TB_BaconUps_113", "BGS_014_G")
def _dr_imprisoner(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Imprisoner: Summon a 1/1 Imp (2/2 if golden)."""
    s = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_030t" if s == 2 else "BG_BRM_006t"
    _summon_tokens(battle, player, pos, 1, cid, "Imp", s, s, tribe="Demon")


@deathrattle("BG21_029", "BG21_029_G")
def _dr_icky_imp(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Icky Imp: Summon two 1/1 Imps (four 1/1 Imps if golden)."""
    count = 4 if _is_golden(minion) else 2
    _summon_tokens(battle, player, pos, count, "BG_BRM_006t", "Imp", 1, 1, tribe="Demon")


@deathrattle("BOT_537", "BG_BOT_537", "TB_BaconUps_039", "BG_BOT_537_G")
def _dr_mechano_egg(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Mechano-Egg: Summon an 8/8 Robosaur (16/16 if golden)."""
    s = 16 if _is_golden(minion) else 8
    cid = "TB_BaconUps_039t" if s == 16 else "BOT_537t"
    _summon_tokens(battle, player, pos, 1, cid, "Robosaur", s, s, tribe="Mechanical/Beast")


@deathrattle("BGS_006", "TB_BaconUps_080", "BGS_006_G")
def _dr_sneeds_old_shredder(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Sneed's Old Shredder: Summon a random Legendary minion (2 if golden)."""
    count = 2 if _is_golden(minion) else 1
    _summon_tokens(battle, player, pos, count, "BGS_006t", "Legendary Minion", 5, 5)


@deathrattle("BG26_804", "BG26_804_G", "BG26_803", "BG26_803_G")
def _dr_octosari(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Octosari, Wrap God: Summon an 8/8 Tentacle (16/16 if golden; 2/2 or 4/4 for BG26_803)."""
    g = _is_golden(minion)
    base = 2 if minion.card_id.startswith("BG26_803") else 8
    s = base * (2 if g else 1)
    cid = "BG26_803_Gt" if g else "BG26_803t"
    _summon_tokens(battle, player, pos, 1, cid, "Tentacle of Octosari", s, s, tribe="Beast")


@deathrattle("BG_LOOT_368", "LOOT_368", "TB_BaconUps_059", "BG_LOOT_368_G")
def _dr_voidlord(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Voidlord: Summon three 1/3 Demons with Taunt (2/6 if golden)."""
    m = 2 if _is_golden(minion) else 1
    cid = "TB_BaconUps_059t" if m == 2 else "BG_CS2_065"
    _summon_tokens(battle, player, pos, 3, cid, "Voidwalker", 1 * m, 3 * m, tribe="Demon", taunt=True)


@deathrattle("BG36_116", "BG36_116_G")
def _dr_underrot_spawn(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Underrot Spawn: Summon an 0/2 Tentacle with Taunt. Give your minions +1 Attack."""
    g = _is_golden(minion)
    count = 2 if g else 1
    b = 2 if g else 1
    _summon_tokens(battle, player, pos, count, "BGFYM_002t", "Aberrant Tentacle", 0, 2, tribe="Aberration", taunt=True)
    for fm in battle.sides[player].minions:
        if not fm.dead:
            fm.atk += b


@deathrattle("BG25_009", "BG25_009_G")
def _dr_eternal_summoner(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Eternal Summoner: Summon 1 Eternal Knight (4/2 Undead; 8/4 if golden)."""
    m = 2 if _is_golden(minion) else 1
    _summon_tokens(battle, player, pos, 1, "BG25_008", "Eternal Knight", 4 * m, 2 * m, tribe="Undead")


@deathrattle("BG35_604", "BG35_604_G")
def _dr_sewer_lord(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Sewer Lord: Summon two Sewer Rats that summon 2/3 Turtles with Taunt."""
    count = 4 if _is_golden(minion) else 2
    _summon_tokens(battle, player, pos, count, "BG19_010", "Sewer Rat", 3, 2, tribe="Beast")


@deathrattle("BG19_010", "TB_BaconUps_250")
def _dr_sewer_rat(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Sewer Rat: Summon a 2/3 Turtle with Taunt (4/6 if golden)."""
    m = 2 if _is_golden(minion) else 1
    _summon_tokens(battle, player, pos, 1, "BG19_010t", "Half-Shell", 2 * m, 3 * m, tribe="Beast", taunt=True)


# -- Deathrattles: Stat Buffers / Shielders / Damage ------------------------


@deathrattle("BG_OG_256", "OG_256", "TB_BaconUps_025", "BG_OG_256_G")
def _dr_spawn_of_nzoth(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Spawn of N'Zoth: Give your minions +1/+1 (+2/+2 if golden)."""
    b = 2 if _is_golden(minion) else 1
    for fm in battle.sides[player].minions:
        if not fm.dead:
            fm.atk += b
            fm.health += b


@deathrattle("BG_OG_221", "OG_221", "TB_BaconUps_014", "BG_OG_221_G")
def _dr_selfless_hero(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Selfless Hero: Give a random friendly minion Divine Shield (2 if golden)."""
    count = 2 if _is_golden(minion) else 1
    candidates = [fm for fm in battle.sides[player].minions if not fm.dead and not fm.divine_shield]
    if candidates:
        for t in battle.rng.sample(candidates, min(count, len(candidates))):
            t.divine_shield = True


@deathrattle("BGS_018", "TB_BaconUps_085", "BGS_018_G")
def _dr_goldrinn(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Goldrinn, the Great Wolf: Your Beasts have +7/+7 until next turn (+14/+14 if golden)."""
    b = 14 if _is_golden(minion) else 7
    side = battle.sides[player]
    side.beast_atk_bonus += b
    side.beast_hp_bonus += b
    for fm in side.minions:
        if not fm.dead and has_tribe(fm, "Beast"):
            fm.atk += b
            fm.health += b


@deathrattle("BGS_040", "TB_BaconUps_154", "BGS_040_G")
def _dr_nadina_the_red(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Nadina the Red: Give 3 friendly Dragons Divine Shield (6 if golden)."""
    count = 6 if _is_golden(minion) else 3
    candidates = [
        fm for fm in battle.sides[player].minions
        if not fm.dead and not fm.divine_shield and has_tribe(fm, "Dragon")
    ]
    if candidates:
        for t in battle.rng.sample(candidates, min(count, len(candidates))):
            t.divine_shield = True


@deathrattle("BG_BOT_606", "BOT_606", "TB_BaconUps_028", "BG_BOT_606_G")
def _dr_kaboom_bot(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Kaboom Bot: Deal 4 damage to a random enemy minion (twice if golden)."""
    foe = battle.sides[1 - player]
    shots = 2 if _is_golden(minion) else 1
    for _ in range(shots):
        alive_enemies = [em for em in foe.minions if not em.dead]
        if not alive_enemies:
            break
        target = battle.rng.choice(alive_enemies)
        battle.damage(target, 4, False)


@deathrattle("BG29_877", "BG29_877_G")
def _dr_fiery_felblood(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Fiery Felblood: Give friendly minions / Tavern minions +2 Attack (+4 if golden)."""
    b = 4 if _is_golden(minion) else 2
    for fm in battle.sides[player].minions:
        if not fm.dead:
            fm.atk += b


@deathrattle("YOD_026", "CORE_YOD_026", "TB_BaconUps_112", "BG_YOD_026")
def _dr_fiendish_servant(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Fiendish Servant: Give this minion's Attack to a random friendly minion (twice if golden)."""
    times = 2 if _is_golden(minion) else 1
    for _ in range(times):
        alive = [fm for fm in battle.sides[player].minions if not fm.dead]
        if alive:
            battle.rng.choice(alive).atk += minion.atk


@deathrattle("BGS_030", "TB_BaconUps_100", "BGS_030_G")
def _dr_king_bagurgle(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """King Bagurgle: Give your other Murlocs +2/+2 (+4/+4 if golden)."""
    b = 4 if _is_golden(minion) else 2
    for fm in battle.sides[player].minions:
        if not fm.dead and has_tribe(fm, "Murloc"):
            fm.atk += b
            fm.health += b


@deathrattle("BG29_815", "BG29_815_G")
def _dr_nightbane_ignited(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Nightbane, Ignited: Give 2 different friendly minions this minion's Attack (twice if golden)."""
    times = 2 if _is_golden(minion) else 1
    for _ in range(times):
        candidates = [fm for fm in battle.sides[player].minions if not fm.dead]
        if not candidates:
            break
        chosen = battle.rng.sample(candidates, min(2, len(candidates)))
        for t in chosen:
            t.atk += minion.atk


def _make_leapfrogger_fn(golden: bool) -> Callable:
    b = 2 if golden else 1

    def _leap(battle: Battle, player: int, _pos: int, _minion: CMinion) -> None:
        beasts = [fm for fm in battle.sides[player].minions if not fm.dead and has_tribe(fm, "Beast")]
        if beasts:
            target = battle.rng.choice(beasts)
            target.atk += b
            target.health += b
            target.extra_deathrattles = target.extra_deathrattles + (_leap,)

    return _leap


_LEAP_NORMAL = _make_leapfrogger_fn(False)
_LEAP_GOLDEN = _make_leapfrogger_fn(True)


@deathrattle("BG21_000", "BG21_000_G", "BG34_Giant_031", "BG34_Giant_031_G")
def _dr_leapfrogger(battle: Battle, player: int, pos: int, minion: CMinion) -> None:
    """Leapfrogger: Give a friendly Beast +1/+1 (+2/+2 if golden) and this Deathrattle."""
    fn = _LEAP_GOLDEN if _is_golden(minion) else _LEAP_NORMAL
    fn(battle, player, pos, minion)


@deathrattle("BG25_022", "BG25_022_G")
def _dr_scarlet_skull(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Scarlet Skull: Give a friendly Undead +1/+2 (+2/+4 if golden)."""
    m = 2 if _is_golden(minion) else 1
    undead = [fm for fm in battle.sides[player].minions if not fm.dead and has_tribe(fm, "Undead")]
    if undead:
        t = battle.rng.choice(undead)
        t.atk += 1 * m
        t.health += 2 * m


@deathrattle("BG28_309", "BG28_309_G")
def _dr_mummifier(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Mummifier: Give a different friendly Undead Reborn (2 if golden)."""
    count = 2 if _is_golden(minion) else 1
    candidates = [
        fm for fm in battle.sides[player].minions
        if not fm.dead and not fm.reborn and has_tribe(fm, "Undead")
    ]
    if candidates:
        for t in battle.rng.sample(candidates, min(count, len(candidates))):
            t.reborn = True


@deathrattle("BG36_202", "BG36_202_G")
def _dr_tasty_lobster(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Tasty Lobster: Give a random friendly Beast +2/+1 (+4/+2 if golden)."""
    m = 2 if _is_golden(minion) else 1
    beasts = [fm for fm in battle.sides[player].minions if not fm.dead and has_tribe(fm, "Beast")]
    if beasts:
        t = battle.rng.choice(beasts)
        t.atk += 2 * m
        t.health += 1 * m


@deathrattle("BG33_828", "BG33_828_G")
def _dr_ship_master_eudora(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Ship Master Eudora: Give your minions +8/+8 (+16/+16 if golden)."""
    b = 16 if _is_golden(minion) else 8
    for fm in battle.sides[player].minions:
        if not fm.dead:
            fm.atk += b
            fm.health += b


@deathrattle("BG31_925", "BG31_925_G")
def _dr_showy_cyclist(battle: Battle, player: int, _pos: int, minion: CMinion) -> None:
    """Showy Cyclist: Give all your Naga +2/+1 (+4/+2 if golden)."""
    m = 2 if _is_golden(minion) else 1
    for fm in battle.sides[player].minions:
        if not fm.dead and has_tribe(fm, "Naga"):
            fm.atk += 2 * m
            fm.health += 1 * m
