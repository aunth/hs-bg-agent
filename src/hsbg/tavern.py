"""Immutable Battlegrounds tavern snapshot, MinionPool, and pure state transitions.

Decoupled from EntityStore so a search planner can branch over hypothetical
action sequences (buy, sell, play, spell, hero power, upgrade, roll, freeze,
reorder) without mutating live game state.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
from typing import Union

from .carddb import Card, CardDB
from .i18n import get_lang, t

BOARD_LIMIT = 7
HAND_LIMIT = 10
MAX_TIER = 6

# Standard Battlegrounds shop minion count per tavern tier.
SHOP_SIZE_BY_TIER = {1: 3, 2: 4, 3: 4, 4: 5, 5: 5, 6: 6}
# Standard copy count per pool minion by tier.
COPIES_BY_TIER = {1: 15, 2: 15, 3: 13, 4: 11, 5: 9, 6: 7, 7: 5}


@dataclass(frozen=True)
class TMinion:
    uid: int
    card_id: str
    name: str
    atk: int
    health: int
    tier: int
    golden: bool = False
    frozen: bool = False
    tribe: str = ""
    card_type: str = "MINION"
    newly_bought: bool = False
    playable: bool = True
    cost: int = 0


@dataclass(frozen=True)
class TSpell:
    uid: int
    card_id: str
    name: str
    cost: int
    tier: int
    frozen: bool = False


@dataclass(frozen=True)
class GameSnapshot:
    turn: int
    tier: int
    gold: int
    upgrade_cost: int | None
    board: tuple[TMinion, ...] = ()
    hand: tuple[TMinion, ...] = ()
    shop: tuple[TMinion, ...] = ()
    shop_spells: tuple[TSpell, ...] = ()
    roll_cost: int = 1
    buy_cost: int = 3
    sell_value: int = 1
    hero_card_id: str = ""
    hero_health: int = 30
    hero_armor: int = 0
    leaderboard_place: int = 0
    hero_power_id: str = ""
    hero_power_name: str = ""
    hero_power_cost: int = 0
    hero_power_usable: bool = False
    upgraded_this_turn: bool = False
    triples_formed: int = 0
    future_gold_bonus: int = 0
    spells_cast: tuple[str, ...] = ()
    frozen_shop: bool = False
    rolled: bool = False
    next_uid: int = 100_000


# -- Typed Action Union -----------------------------------------------------


@dataclass(frozen=True)
class BuyMinion:
    uid: int
    card_id: str
    name: str
    cost: int = 3

    def describe(self) -> str:
        return t("act_buy_minion", name=self.name, cost=self.cost)


@dataclass(frozen=True)
class BuySpell:
    uid: int
    card_id: str
    name: str
    cost: int = 1
    option: int = 0
    option_label: str = ""
    target_name: str = ""

    def describe(self) -> str:
        tgt = f" -> {self.target_name}" if self.target_name else ""
        if self.option_label:
            return t("act_buy_spell_opt", name=self.name, opt=f"{self.option_label}{tgt}", cost=self.cost)
        return t("act_buy_spell", name=f"{self.name}{tgt}", cost=self.cost)


@dataclass(frozen=True)
class SellMinion:
    uid: int
    card_id: str
    name: str
    gain: int = 1

    def describe(self) -> str:
        return t("act_sell_minion", name=self.name, gain=self.gain)


@dataclass(frozen=True)
class PlayMinion:
    uid: int
    card_id: str
    name: str
    option: int = 0
    option_label: str = ""
    target_name: str = ""

    def describe(self) -> str:
        tgt = f" -> {self.target_name}" if self.target_name else ""
        if self.option_label:
            return t("act_play_minion_opt", name=self.name, opt=self.option_label) + tgt
        return t("act_play_minion", name=f"{self.name}{tgt}")


@dataclass(frozen=True)
class PlaySpell:
    uid: int
    card_id: str
    name: str
    cost: int = 0
    option: int = 0
    option_label: str = ""
    target_name: str = ""

    def describe(self) -> str:
        unit = "з" if get_lang() == "uk" else "g"
        cost_str = f" (-{self.cost}{unit})" if self.cost > 0 else ""
        tgt = f" -> {self.target_name}" if self.target_name else ""
        if self.option_label:
            return t("act_play_spell_opt", name=self.name, opt=f"{self.option_label}{tgt}", cost_str=cost_str)
        return t("act_play_spell", name=f"{self.name}{tgt}", cost_str=cost_str)


@dataclass(frozen=True)
class UseHeroPower:
    card_id: str
    name: str
    cost: int = 0
    target_uid: int = 0
    target_name: str = ""

    def describe(self) -> str:
        if self.target_name:
            return t("act_hero_power_target", name=self.name, target=self.target_name, cost=self.cost)
        return t("act_hero_power", name=self.name, cost=self.cost)


@dataclass(frozen=True)
class UpgradeTavern:
    cost: int
    new_tier: int

    def describe(self) -> str:
        return t("act_upgrade", tier=self.new_tier, cost=self.cost)


@dataclass(frozen=True)
class ReorderBoard:
    order: tuple[str, ...]

    def describe(self) -> str:
        shown = " | ".join(self.order[:4])
        if len(self.order) > 4:
            shown += f" (+{len(self.order) - 4})"
        return t("act_reorder", order=shown)


@dataclass(frozen=True)
class FreezeShop:
    targets: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.targets:
            shown = ", ".join(self.targets[:2])
            return t("act_freeze_targets", targets=shown)
        return t("act_freeze")


@dataclass(frozen=True)
class RollShop:
    cost: int = 1

    def describe(self) -> str:
        return t("act_roll", cost=self.cost)


@dataclass(frozen=True)
class EndTurn:
    def describe(self) -> str:
        return t("act_end_turn")


Action = Union[
    BuyMinion,
    BuySpell,
    SellMinion,
    PlayMinion,
    PlaySpell,
    UseHeroPower,
    UpgradeTavern,
    ReorderBoard,
    FreezeShop,
    RollShop,
    EndTurn,
]


# -- Choose One & Hero Power Registries (Phases 8 & 9) ----------------------

ONCE_PER_GAME_HPS = {
    "TB_BaconShop_HP_046",   # Gonna Be Rich! (Reno Jackson: Make a friendly minion Golden)
    "BG31_HERO_005p",        # Cloning Gallery (Zerek: Summon an exact copy of a friendly minion)
    "TB_BaconShop_HP_076",   # Piggy Bank (Gain Gold, increases each turn, once per game)
    "BGDUO_Anomaly_003t",    # Golden Friendship (Goldenize and Pass a minion, once per game)
}

CORE_ENGINE_NAMES = {
    "Banana Slamma",
    "Titus Rivendare",
    "Brann Bronzebeard",
    "Drakkari Enchanter",
    "Persistent Poet",
    "Trigore the Lasher",
    "Tarecgosa",
    "Turquoise Skitterer",
    "Ravaging Scorpid",
    "Living Azerite",
    "Rock Rock",
    "Deep Blue Crooner",
    "Zesty Shaker",
    "Scrap Scraper",
    "Utility Drone",
    "Polarizing Beatboxer",
    "Tichondrius",
    "Famished Felbat",
    "Underhanded Dealer",
    "Bream Counter",
    "Choral Mrrrglr",
    "Young Murk-Eye",
    "Kalecgos, Arcane Aspect",
    "Nightbane, Ignited",
    "Catacomb Crasher",
    "Pokey Thornmantle",
    "Charlga",
    "Silithid Burrower",
    "Fandral Staghelm",
}

# Maps Choose One card_id (base, without _G) -> ((uk_opt1, en_opt1), (uk_opt2, en_opt2))
CHOOSE_ONE_CARDS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "BG31_880": (("+3/+1", "+3/+1"), ("+1/+3", "+1/+3")),  # Alliance Flag
    "BG31_320": (("2 Самоцвіти (+2/+2)", "2 Blood Gems (+2/+2)"), ("+1з наст. хід", "+1g next turn")),  # Crater Miner
    "BG32_237": (("+1 Атаки спелам", "+1 Spell Atk"), ("+1 Здоров'я спелам", "+1 Spell HP")),  # Intrepid Botanist
    "BG31_144": (("+3 Атк і Бож. щит", "+3 Atk & Divine Shield"), ("+3 ХП і Отрута", "+3 HP & Venomous")),  # Adaptive Ancestor
    "BG31_325": (("3 Самоцвіти зараз", "3 Blood Gems now"), ("Дезратл: 2 Самоцвіти", "Deathrattle: 2 Gems")),  # Crystal Infuser
    "BG30_123": (("Самоцвіти +1/+1 на гру", "Gems +1/+1 this game"), ("4 Самоцвіти зараз (+4/+4)", "4 Blood Gems now (+4/+4)")),  # Fearless Foodie
    "BG31_825": (("+2з наст. хід", "+2g next turn"), ("+4з через 2 ходи", "+4g in 2 turns")),  # Grace Farsail
    "BG30_128": (("Подвоїти Атаку", "Double Attack"), ("Подвоїти Здоров'я", "Double Health")),  # Sleeping Sea Glass
    "BG36_330": (("2 безкошт. ріроли", "2 free Refreshes"), ("3 Самоцвіти (+3/+3)", "3 Blood Gems (+3/+3)")),  # Sly Infiltrator
    "BG27_084": (("+1/+1 і Переродження", "+1/+1 & Reborn"), ("+4 Атк і Несамовитість", "+4 Atk & Windfury")),  # Sprightly Scarab
    "BG31_881": (("+2/+2 всім зараз", "+2/+2 all now"), ("+4/+4 всім наст. хід", "+4/+4 all next turn")),  # Time Management
    "BG36_332": (("Випадковий Свинобраз", "Random Quilboar"), ("+1 макс. золота", "+1 max Gold")),  # Snare Trapper
    "BG30_117": (("+4 Атаки всім", "+4 Attack all"), ("+4 Здоров'я всім", "+4 Health all")),  # Volcanic Visitor
    "BG31_886": (("+12/+12 одному", "+12/+12 to one"), ("+2/+2 всім", "+2/+2 to all")),  # Forest's Bounty
    "BG30_110": (("2x Атака в таверні", "2x Tavern Attack"), ("2x ХП в таверні", "2x Tavern Health")),  # Mirage Conjurer
    "BG31_842": (("+4/+4 таверні на гру", "+4/+4 Tavern this game"), ("З'їсти таверну", "Consume Tavern")),  # Nether Construct
    "BG36_341": (("3 Самоцвіти всім", "3 Gems to all"), ("Шквал самоцвітів x3", "Gem Barrage x3")),  # Veteran Brigand
}


def _base_cid(card_id: str) -> str:
    return card_id[:-2] if card_id.endswith("_G") else card_id


def _has_choose_one_combiner(snap: GameSnapshot) -> bool:
    return any(_base_cid(m.card_id) in ("BG_OG_044", "BG31_327") for m in snap.board)


def choose_one_labels(card_id: str) -> tuple[str, str] | None:
    base = _base_cid(card_id)
    pair = CHOOSE_ONE_CARDS.get(base)
    if not pair:
        return None
    idx = 0 if get_lang() == "uk" else 1
    return (pair[0][idx], pair[1][idx])


# Backline engines that should NEVER receive Taunt from Tricky Trousers / Fortify / Defender's Rites
BACKLINE_Fragile_ENGINES = {
    "Banana Slamma",
    "Titus Rivendare",
    "Brann Bronzebeard",
    "Drakkari Enchanter",
    "Scrap Scraper",
    "Utility Drone",
    "Deep Blue Crooner",
    "Zesty Shaker",
    "Young Murk-Eye",
    "Pokey Thornmantle",
}

# Deathrattle / frontliner minions that WANT Taunt first
TAUNT_PRIORITY_NAMES = {
    "Buzzing Vermin",
    "Manasaber",
    "Harmless Bonehead",
    "Handless Forsaken",
    "Leapfrogger",
    "Turquoise Skitterer",
    "Ravaging Scorpid",
    "Recurring Nightmare",
    "Octosari, Wrap God",
    "Goldrinn, the Great Wolf",
    "Gentle Djinni",
    "Cord Puller",
}

# Spells & minions that target a single friendly minion (or tavern minion)
TAUNT_BUFF_SPELLS = {
    "BG28_520",             # Tricky Trousers (+1/+2 and Taunt)
    "BG28_503",             # Fortify (+3 Health and Taunt)
    "BG28_825",             # Defender's Rites (+7/+7 and Taunt)
    "BG23_004t", "BG23_004_Gt",  # Angler's Lure (+2/+6 and Taunt)
    "BG24_Reward_715t",     # Mega Horn (+5/+5 and Taunt)
    "BG27_002t",            # Slimy Shield (+1/+1 and Taunt)
    "BG34_Treasure_906",    # The Bouncer (+6/+6 and Taunt)
    "BGS_Treasures_026",    # The Bouncer
}

SINGLE_CARRY_BUFF_SPELLS = {
    "BG28_897",             # Tavern Dish Banana (+2/+2)
    "BG31_880", "BG31_880t", "BG31_880t2",  # Alliance Flag (+3/+1 or +1/+3)
    "EBG_Spell_014",        # Pointy Arrow (+4 Attack)
    "BG28_814",             # Echoing Roar (End of turn +2/+2)
    "BG28_885",             # Lantern Light (+Tier/+Tier)
    "BG32_337",             # Seafood Stew
    "BG35_149",             # Deepwater Clan
    "BG35_910",             # Conflagration (+4/+4)
    "BG35_952",             # Back to Back (+4/+4)
    "BG36_624",             # Repair Job (+4/+8)
    "BG36_883",             # Winner's Bread (+2/+3)
    "BG23_000t", "BG23_000_Gt",  # Mini-Trident
    "BG23_008t", "BG23_008_Gt",  # Glowing Crown (Divine Shield)
    "BG26_171t", "BG26_171_Gt",  # Just Keep Swimming
    "BG26_501t", "BG26_501_Gt",  # Sick Riffs
    "BG26_502t", "BG26_502_Gt",  # Deep Blues
    "BG27_004t", "BG27_004_Gt",  # Crab Mount
    "BG31_924t", "BG31_924_Gt",  # Thaumaturgy
    "BG_EX1_014t", "BG_TB_006", "BGS_Treasures_000",  # Bananas
    "BG28_507",             # Sacred Gift (Divine Shield)
    "BG30_MagicItem_714t",  # Harvested Pearl (+30/+30)
}


def select_card_target(snap: GameSnapshot, card_id: str, option: int = 0) -> TMinion | None:
    """Select the optimal target minion on `snap.board` (or `snap.shop`) for a targeted spell or battlecry."""
    base = _base_cid(card_id)
    board = snap.board
    if not board:
        return None

    # 1. Taunt-granting spells (Tricky Trousers, Fortify, Defender's Rites, Angler's Lure):
    # Prioritize Deathrattle openers or highest-HP frontliners; avoid fragile backline engines.
    if base in TAUNT_BUFF_SPELLS:
        return max(
            board,
            key=lambda m: (
                m.name.rstrip("*") not in BACKLINE_Fragile_ENGINES,
                m.name.rstrip("*") in TAUNT_PRIORITY_NAMES,
                m.health,
                m.tier,
            ),
        )

    # 2. Blood Gems & single-target stat buff spells (Banana, Alliance Flag, Pointy Arrow, Repair Job, etc.)
    if base.startswith("BG20_GEM") or base in SINGLE_CARRY_BUFF_SPELLS:
        if base == "EBG_Spell_014" or (base == "BG31_880" and option == 1):
            # Attack-heavy buff: prefer Cleave / Divine Shield / high-HP unit that survives to hit twice
            return max(
                board,
                key=lambda m: (
                    m.name.rstrip("*") in ("Persistent Poet", "Tarecgosa", "Bronze Warden", "Cave Hydra", "Foe Reaper 4000"),
                    m.health >= 4,
                    m.golden,
                    m.tier,
                    m.health + m.atk,
                ),
            )
        return max(
            board,
            key=lambda m: (
                m.name.rstrip("*") in ("Persistent Poet", "Tarecgosa", "Trigore the Lasher", "Bronze Warden"),
                m.golden,
                m.tier,
                m.atk + m.health,
            ),
        )

    # 3. Forest's Bounty (BG31_886): Option 1 targets a single minion (+12/+12); Option 2 buffs all
    if base == "BG31_886":
        if option == 2:
            return None
        return max(board, key=lambda m: (m.golden, m.tier, m.atk + m.health))

    # 4. Perfect Vision (BG28_838: Set a minion's stats to 20/20) -> smallest minion on board
    if base == "BG28_838":
        return min(board, key=lambda m: (m.atk + m.health, -m.tier))

    # 5. Eyes of the Earth Mother (EBG_Spell_017: Make a friendly minion from Tier 4 or below Golden)
    if base == "EBG_Spell_017":
        cands = [m for m in board if not m.golden and m.tier <= 4]
        if cands:
            return max(cands, key=lambda m: (m.name.rstrip("*") in CORE_ENGINE_NAMES, m.tier, m.atk + m.health))
        return None

    # 6. Dreamer's Embrace / Boogie Down / Surging Scales (Trigger a friendly minion's Battlecry)
    if base in ("BG28_883", "BG26_355t", "BG29_870t", "BG26_355"):
        if base == "BG26_355" and option == 2:
            return None
        bc_ids = {"BG23_002", "BG26_135", "BG20_100", "BG31_320", "BG30_123", "BG27_084", "BG31_825", "BG36_330", "BG36_332"}
        bc_cands = [m for m in board if _base_cid(m.card_id) in bc_ids]
        if bc_cands:
            return max(bc_cands, key=lambda m: (m.golden, m.tier, m.atk + m.health))
        return max(board, key=lambda m: (m.tier, m.atk + m.health))

    # 7. Robust Evolution (BG30_804: Transform a minion into a higher Tier one, keeping stats) -> lowest-tier minion
    if base == "BG30_804":
        return min(board, key=lambda m: (m.name.rstrip("*") in CORE_ENGINE_NAMES, m.tier, -(m.atk + m.health)))

    # 8. Chef's Choice / Natural Blessing / Lost Staff of Hamuul / Eonar's Favor / Sea Summoning -> main tribe minion
    if base in ("BG28_518", "BG28_845", "EBG_Spell_038", "BG35_912", "BG27_012t", "BG27_012_Gt"):
        with_tribe = [m for m in board if m.tribe]
        if with_tribe:
            return max(with_tribe, key=lambda m: (m.tier, m.atk + m.health))
        return board[0]

    # 9. Tribe-restricted targeted spells & battlecries
    if base in ("BG27_084", "BG27_084t", "BG27_084t2", "BG35_MagicItem_872t"):  # Sprightly Scarab / Ophidian Staff -> Beast
        beasts = [m for m in board if "Beast" in (m.tribe or "") or "All" in (m.tribe or "")]
        if not beasts:
            return None
        if option == 2:
            return max(beasts, key=lambda m: (m.atk, m.tier))
        return max(beasts, key=lambda m: (m.name.rstrip("*") in TAUNT_PRIORITY_NAMES, m.tier, m.atk + m.health))

    if base in ("BG32_815", "BG23_007t", "BG23_007_Gt", "BG31_830t", "BG31_830_Gt"):  # Shifting Tide / Undersea Mount / Reinvigoration -> Naga
        nagas = [m for m in board if "Naga" in (m.tribe or "") or "All" in (m.tribe or "")]
        pool_n = nagas if nagas else list(board)
        return max(pool_n, key=lambda m: (m.golden, m.tier, m.atk + m.health))

    if base == "BG33_248":  # Nimble Wingbeat -> Dragon
        dragons = [m for m in board if "Dragon" in (m.tribe or "") or "All" in (m.tribe or "")]
        return max(dragons, key=lambda m: (m.name.rstrip("*") in ("Persistent Poet", "Tarecgosa"), m.atk, m.tier)) if dragons else None

    if base == "BG33_898":  # Murky's Fish -> Murloc
        murlocs = [m for m in board if "Murloc" in (m.tribe or "") or "All" in (m.tribe or "")]
        return max(murlocs, key=lambda m: (m.golden, m.tier, m.atk + m.health)) if murlocs else None

    if base in ("BG28_607", "BG36_880"):  # Corrupted Cupcakes / Methodical Madness -> Demon
        demons = [m for m in board if "Demon" in (m.tribe or "") or "All" in (m.tribe or "")]
        return max(demons, key=lambda m: (m.golden, m.tier, m.atk + m.health)) if demons else None

    if base == "BG35_911":  # Arcane Absorption -> Elemental
        elems = [m for m in board if "Elemental" in (m.tribe or "") or "All" in (m.tribe or "")]
        return max(elems, key=lambda m: (m.golden, m.tier, m.atk + m.health)) if elems else None

    return None


def evaluate_hero_power_timing(snap: GameSnapshot) -> dict:
    """Evaluate whether to trigger `snap.hero_power_id` now, which target to pick, and score (0..100).

    Returns dict with keys:
      - `should_use`: bool
      - `target_uid`: int
      - `target_name`: str
      - `hold_hint`: str
      - `score`: float (0..100)
    """
    hp_id = snap.hero_power_id
    eff_hp = (snap.hero_health or 30) + (snap.hero_armor or 0)
    emergency = eff_hp <= 15 and snap.turn >= 6

    if not hp_id or not snap.hero_power_usable:
        return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 0.0}

    # 1. Reno Jackson (Gonna Be Rich!) / Golden Friendship: Make a friendly minion Golden (Once per game)
    if hp_id in ("TB_BaconShop_HP_046", "BGDUO_Anomaly_003t"):
        candidates = [m for m in snap.board if not m.golden]
        if not candidates:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": t("hp_hold_core"), "score": 15.0}
        best = max(
            candidates,
            key=lambda m: (
                m.name.rstrip("*") in CORE_ENGINE_NAMES and m.tier >= 4,
                m.tier >= 5,
                m.tier,
                m.atk + m.health,
            ),
        )
        is_core_engine = (best.name.rstrip("*") in CORE_ENGINE_NAMES and best.tier >= 4) or best.tier >= 5
        is_emergency_carry = emergency and (best.tier >= 4 or (best.atk + best.health) >= 16)
        if is_core_engine or is_emergency_carry:
            sc = 92.0 if is_core_engine else 80.0
            return {
                "should_use": True,
                "target_uid": best.uid,
                "target_name": best.name,
                "hold_hint": "",
                "score": sc,
            }
        return {
            "should_use": False,
            "target_uid": 0,
            "target_name": "",
            "hold_hint": t("hp_hold_core"),
            "score": 22.0,
        }

    # 2. Zerek, Master Cloner (Cloning Gallery): Once per game, summon an exact copy of a friendly minion
    if hp_id == "BG31_HERO_005p":
        if not snap.board or len(snap.board) >= BOARD_LIMIT:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": t("hp_hold_core"), "score": 15.0}
        best = max(
            snap.board,
            key=lambda m: (
                m.name.rstrip("*") in CORE_ENGINE_NAMES and m.tier >= 4,
                m.golden and m.tier >= 4,
                m.tier >= 5,
                m.tier,
                m.atk + m.health,
            ),
        )
        in_hand, in_board = _non_golden_copies(snap, best.card_id)
        completes_high_triple = (not best.golden and (len(in_hand) + len(in_board)) == 2 and best.tier >= 4)
        is_core_or_giant = (
            (best.name.rstrip("*") in CORE_ENGINE_NAMES and best.tier >= 4)
            or best.tier >= 5
            or (best.atk + best.health) >= 26
            or completes_high_triple
        )
        is_emergency_carry = emergency and (best.tier >= 4 or (best.atk + best.health) >= 16)
        if is_core_or_giant or is_emergency_carry:
            sc = 90.0 if is_core_or_giant else 79.0
            return {
                "should_use": True,
                "target_uid": best.uid,
                "target_name": best.name,
                "hold_hint": "",
                "score": sc,
            }
        return {
            "should_use": False,
            "target_uid": 0,
            "target_name": "",
            "hold_hint": t("hp_hold_core"),
            "score": 24.0,
        }

    # 3. Piggy Bank (TB_BaconShop_HP_076): Gain Gold (increases by 1 each turn, once per game)
    if hp_id == "TB_BaconShop_HP_076":
        enables_level = (
            snap.upgrade_cost is not None
            and snap.tier >= 3
            and snap.gold < snap.upgrade_cost <= snap.gold + max(2, snap.turn)
        )
        if (snap.turn >= 6 and enables_level) or snap.turn >= 8 or emergency:
            return {"should_use": True, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 84.0}
        return {
            "should_use": False,
            "target_uid": 0,
            "target_name": "",
            "hold_hint": t("hp_hold_turn", turn=6),
            "score": 28.0,
        }

    # 4. Teron Gorefiend (BG25_HERO_103p, Rapid Reanimation): 0g destroy & resummon in combat
    if hp_id == "BG25_HERO_103p":
        if not snap.board:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 20.0}
        dr_priority = {"Buzzing Vermin", "Manasaber", "Harmless Bonehead", "Handless Forsaken", "Turquoise Skitterer", "Ravaging Scorpid", "Octosari, Wrap God", "Leapfrogger"}
        best = max(
            snap.board,
            key=lambda m: (m.name.rstrip("*") in dr_priority, m.tier, m.atk + m.health),
        )
        return {"should_use": True, "target_uid": best.uid, "target_name": best.name, "hold_hint": "", "score": 78.0}

    # 5. Shudderwock (TB_BaconShop_HP_022, Snicker-snack): Trigger a friendly minion's Battlecry
    if hp_id == "TB_BaconShop_HP_022":
        bc_ids = {"BG23_002", "BG26_135", "BG20_100", "BG31_320", "BG30_123", "BG27_084", "BG31_825", "BG36_330", "BG36_332"}
        bc_minions = [m for m in snap.board if _base_cid(m.card_id) in bc_ids]
        if not bc_minions:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 30.0}
        best = max(bc_minions, key=lambda m: (m.golden, m.tier, m.atk + m.health))
        return {"should_use": True, "target_uid": best.uid, "target_name": best.name, "hold_hint": "", "score": 82.0}

    # 6. Galakrond's Greed (TB_BaconShop_HP_011): Replace a Tavern minion with a higher Tier one
    if hp_id == "TB_BaconShop_HP_011":
        if not snap.shop:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 25.0}
        best_shop = max(snap.shop, key=lambda m: (m.tier, -(m.atk + m.health)))
        sc = 74.0 if best_shop.tier >= max(2, snap.tier) else 58.0
        return {"should_use": True, "target_uid": best_shop.uid, "target_name": best_shop.name, "hold_hint": "", "score": sc}

    # 7. Major Hymn / Minor Hymn (BG26_HERO_102p / BG26_HERO_102p2): Give a minion Attack/Health equal to Tier
    if hp_id in ("BG26_HERO_102p", "BG26_HERO_102p2"):
        if not snap.board:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 25.0}
        best = max(snap.board, key=lambda m: (m.golden, m.tier, m.atk + m.health))
        sc = 72.0 if snap.hero_power_cost == 0 else 62.0
        return {"should_use": True, "target_uid": best.uid, "target_name": best.name, "hold_hint": "", "score": sc}

    # 8. Jandice Barov (TB_BaconShop_HP_084, Swap, Lock, & Shop It): Swap a non-Golden friendly minion with Tavern
    if hp_id == "TB_BaconShop_HP_084":
        non_g = [m for m in snap.board if not m.golden]
        if not non_g or not snap.shop:
            return {"should_use": False, "target_uid": 0, "target_name": "", "hold_hint": "", "score": 25.0}
        re_buy_ids = {"BG23_002", "BGS_115", "BG26_135", "BG30_123"}
        best = max(non_g, key=lambda m: (m.card_id in re_buy_ids, -m.tier, -(m.atk + m.health)))
        good = best.card_id in re_buy_ids or best.tier < snap.tier
        return {
            "should_use": good,
            "target_uid": best.uid,
            "target_name": best.name,
            "hold_hint": "",
            "score": 74.0 if good else 38.0,
        }

    # 9. Generic active Hero Power (0g always use; 1-2g use when affordable and not blocking curve)
    target_uid = 0
    target_name = ""
    if snap.board and snap.hero_power_cost <= 1:
        best_b = max(snap.board, key=lambda m: (m.golden, m.tier, m.atk + m.health))
        target_uid = best_b.uid
        target_name = best_b.name
    sc = 75.0 if snap.hero_power_cost == 0 else (62.0 if snap.gold >= snap.hero_power_cost else 30.0)
    return {
        "should_use": snap.gold >= snap.hero_power_cost,
        "target_uid": target_uid,
        "target_name": target_name,
        "hold_hint": "",
        "score": sc,
    }


def _buff_best_board_minion(
    board: tuple[TMinion, ...], atk_delta: int, hp_delta: int
) -> tuple[TMinion, ...]:
    if not board:
        return board
    # Pick highest-tier / highest-stat minion as default buff target.
    best_idx = max(
        range(len(board)),
        key=lambda i: (board[i].golden, board[i].tier, board[i].atk + board[i].health),
    )
    target = board[best_idx]
    buffed = replace(target, atk=target.atk + atk_delta, health=target.health + hp_delta)
    return board[:best_idx] + (buffed,) + board[best_idx + 1 :]


def _buff_all_minions(
    minions: tuple[TMinion, ...], atk_delta: int, hp_delta: int
) -> tuple[TMinion, ...]:
    return tuple(
        replace(m, atk=m.atk + atk_delta, health=m.health + hp_delta) for m in minions
    )


def _apply_spell_effect(snap: GameSnapshot, card_id: str, option: int = 0) -> GameSnapshot:
    """Apply deterministic economy/stat effects for common Tavern Spells & coins."""
    both = _has_choose_one_combiner(snap)
    # Tavern Coin / The Coin / Hasty Excavation (+1 Gold)
    if card_id in ("BG28_810", "GAME_005", "BG28_571"):
        return replace(snap, gold=snap.gold + 1)
    # Careful Investment (+2 Gold next turn)
    if card_id == "BG28_800":
        return replace(snap, future_gold_bonus=snap.future_gold_bonus + 2)
    # Blood Gem (+1/+1)
    if card_id.startswith("BG20_GEM"):
        return replace(snap, board=_buff_best_board_minion(snap.board, 1, 1))
    # Tavern Dish Banana (+2/+2)
    if card_id == "BG28_897":
        return replace(snap, board=_buff_best_board_minion(snap.board, 2, 2))
    # Alliance Flag (Choose One: +3/+1 or +1/+3)
    if card_id == "BG31_880":
        if both:
            return replace(snap, board=_buff_best_board_minion(snap.board, 4, 4))
        if option == 2:
            return replace(snap, board=_buff_best_board_minion(snap.board, 1, 3))
        return replace(snap, board=_buff_best_board_minion(snap.board, 3, 1))
    # Time Management (BG31_881: Choose One - Give your minions +2/+2 now; or +4/+4 next turn)
    if card_id == "BG31_881":
        if both:
            return replace(snap, board=_buff_all_minions(snap.board, 5, 5))
        if option == 2:
            # Future start-of-turn +4/+4 to all minions (discounted slightly for 1-turn delay)
            return replace(snap, board=_buff_all_minions(snap.board, 3, 3))
        return replace(snap, board=_buff_all_minions(snap.board, 2, 2))
    # Forest's Bounty (BG31_886: Choose One - Give a minion +6/+6 twice; or Give your minions +2/+2)
    if card_id == "BG31_886":
        if both:
            b1 = _buff_best_board_minion(snap.board, 12, 12)
            return replace(snap, board=_buff_all_minions(b1, 2, 2))
        if option == 2:
            return replace(snap, board=_buff_all_minions(snap.board, 2, 2))
        return replace(snap, board=_buff_best_board_minion(snap.board, 12, 12))
    # Pointy Arrow (+4 Attack)
    if card_id == "EBG_Spell_014":
        return replace(snap, board=_buff_best_board_minion(snap.board, 4, 0))
    # Fortify (+3 Health and Taunt)
    if card_id == "BG28_503":
        return replace(snap, board=_buff_best_board_minion(snap.board, 0, 3))
    # Tricky Trousers (+1/+2 and Taunt)
    if card_id == "BG28_520":
        return replace(snap, board=_buff_best_board_minion(snap.board, 1, 2))
    # Defender's Rites (+7/+7 and Taunt)
    if card_id == "BG28_825":
        return replace(snap, board=_buff_best_board_minion(snap.board, 7, 7))
    # Repair Job (+4/+8)
    if card_id == "BG36_624":
        return replace(snap, board=_buff_best_board_minion(snap.board, 4, 8))
    # Lantern Light (+Tier/+Tier)
    if card_id == "BG28_885":
        return replace(snap, board=_buff_best_board_minion(snap.board, snap.tier, snap.tier))
    # Conflagration / Back to Back (+4/+4)
    if card_id in ("BG35_910", "BG35_952"):
        return replace(snap, board=_buff_best_board_minion(snap.board, 4, 4))
    # Winner's Bread (+2/+3)
    if card_id == "BG36_883":
        return replace(snap, board=_buff_best_board_minion(snap.board, 2, 3))
    # Them Apples: Give minions in the Tavern +1/+2
    if card_id == "BG28_966":
        return replace(snap, shop=_buff_all_minions(snap.shop, 1, 2))
    return snap


def _apply_battlecry_effect(snap: GameSnapshot, minion: TMinion, option: int = 0) -> GameSnapshot:
    """Apply deterministic battlecry/Choose One/economy effects when a minion is played."""
    mult = 2 if minion.golden else 1
    cid = _base_cid(minion.card_id)
    both = _has_choose_one_combiner(snap)

    # Shell Collector: Get a Tavern Coin (+1g immediately usable)
    if cid == "BG23_002":
        return replace(snap, gold=snap.gold + 1 * mult)
    # Southsea Busker: Gain 1 Gold next turn
    if cid == "BG26_135":
        return replace(snap, future_gold_bonus=snap.future_gold_bonus + 1 * mult)
    # Razorfen Geomancer: +2 Blood Gems (+2/+2)
    if cid == "BG20_100":
        return replace(snap, board=_buff_best_board_minion(snap.board, 2 * mult, 2 * mult))
    # Crater Miner (Choose One: 2 Blood Gems or Gem Day +1g next turn)
    if cid == "BG31_320":
        if both:
            return replace(
                snap,
                board=_buff_best_board_minion(snap.board, 2 * mult, 2 * mult),
                future_gold_bonus=snap.future_gold_bonus + 1 * mult,
            )
        if option == 2:
            return replace(snap, future_gold_bonus=snap.future_gold_bonus + 1 * mult)
        return replace(snap, board=_buff_best_board_minion(snap.board, 2 * mult, 2 * mult))
    # Fearless Foodie (Choose One: Blood Gems +1/+1 this game vs 4 Blood Gems now)
    if cid == "BG30_123":
        if both or option == 2:
            return replace(snap, board=_buff_best_board_minion(snap.board, 4 * mult, 4 * mult))
        # Option 1: permanent Blood Gem scaling (modeled as +2/+2 immediate + future scaling credit)
        has_quilboar = any("Quilboar" in (m.tribe or "") for m in snap.board)
        bonus = 5 * mult if (has_quilboar and snap.turn <= 7) else 2 * mult
        return replace(snap, board=_buff_best_board_minion(snap.board, bonus, bonus))
    # Sprightly Scarab (BG27_084: Choose One - Give a Beast +1/+1 and Reborn; or +4 Attack and Windfury)
    if cid == "BG27_084":
        beasts = [i for i, m in enumerate(snap.board) if "Beast" in (m.tribe or "") or "All" in (m.tribe or "")]
        if not beasts:
            return snap
        if option == 2 and not both:
            best_i = max(beasts, key=lambda i: (snap.board[i].atk, snap.board[i].tier))
            t_min = snap.board[best_i]
            buffed = replace(t_min, atk=t_min.atk + 4 * mult, health=t_min.health + 1 * mult)
        else:
            best_i = max(beasts, key=lambda i: (snap.board[i].tier, snap.board[i].atk + snap.board[i].health))
            t_min = snap.board[best_i]
            # Reborn grants an extra life in combat (valued at ~ +3/+4 equivalent stats + 1/1)
            atk_add = (5 if both else 2) * mult
            hp_add = (4 if both else 3) * mult
            buffed = replace(t_min, atk=t_min.atk + atk_add, health=t_min.health + hp_add)
        new_b = snap.board[:best_i] + (buffed,) + snap.board[best_i + 1 :]
        return replace(snap, board=new_b)
    # Adaptive Ancestor (BG31_144: Choose One - +3 Attack & Divine Shield; or +3 Health & Venomous)
    if cid == "BG31_144" and snap.board:
        last = snap.board[-1]
        if both:
            buffed = replace(last, atk=last.atk + 6 * mult, health=last.health + 6 * mult)
        elif option == 2:
            # Venomous scales strongly against high-HP late-game boards
            v_bonus = 7 if snap.turn >= 6 else 4
            buffed = replace(last, atk=last.atk + v_bonus, health=last.health + 3 * mult)
        else:
            buffed = replace(last, atk=last.atk + 3 * mult, health=last.health + 3 * mult)
        return replace(snap, board=snap.board[:-1] + (buffed,))
    # Grace Farsail (BG31_825: Choose One - +2 Gold next turn; or +4 Gold in 2 turns)
    if cid == "BG31_825":
        if both:
            return replace(snap, future_gold_bonus=snap.future_gold_bonus + 5 * mult)
        if option == 2:
            eff_hp = (snap.hero_health or 30) + (snap.hero_armor or 0)
            g_val = 3 * mult if eff_hp >= 22 else 2 * mult
            return replace(snap, future_gold_bonus=snap.future_gold_bonus + g_val)
        return replace(snap, future_gold_bonus=snap.future_gold_bonus + 2 * mult)
    # Sleeping Sea Glass (BG30_128: Choose One - Double Attack or Double Health)
    if cid == "BG30_128" and snap.board:
        last = snap.board[-1]
        factor = 3 if minion.golden else 2
        if both:
            buffed = replace(last, atk=last.atk * factor, health=last.health * factor)
        elif option == 2:
            buffed = replace(last, health=last.health * factor)
        else:
            buffed = replace(last, atk=last.atk * factor)
        return replace(snap, board=snap.board[:-1] + (buffed,))
    # Sly Infiltrator (BG36_330: Choose One - 2 free Refreshes; or 3 Blood Gems)
    if cid == "BG36_330":
        if both:
            return replace(
                snap,
                board=_buff_best_board_minion(snap.board, 3 * mult, 3 * mult),
                future_gold_bonus=snap.future_gold_bonus + 2 * mult,
            )
        if option == 1:
            return replace(snap, roll_cost=0, future_gold_bonus=snap.future_gold_bonus + 1 * mult)
        return replace(snap, board=_buff_best_board_minion(snap.board, 3 * mult, 3 * mult))
    # Volcanic Visitor (BG30_117: Choose One - +4 Attack to all; or +4 Health to all)
    if cid == "BG30_117":
        if both:
            return replace(snap, board=_buff_all_minions(snap.board, 4 * mult, 4 * mult))
        if option == 2:
            return replace(snap, board=_buff_all_minions(snap.board, 0, 4 * mult))
        return replace(snap, board=_buff_all_minions(snap.board, 4 * mult, 0))
    # Mirage Conjurer (BG30_110: Choose One - Double Attack or Double Health of Tavern minions)
    if cid == "BG30_110":
        factor = 3 if minion.golden else 2
        if both:
            new_shop = tuple(replace(m, atk=m.atk * factor, health=m.health * factor) for m in snap.shop)
        elif option == 2:
            new_shop = tuple(replace(m, health=m.health * factor) for m in snap.shop)
        else:
            new_shop = tuple(replace(m, atk=m.atk * factor) for m in snap.shop)
        return replace(snap, shop=new_shop)
    # Crystal Infuser / Veteran Brigand
    if cid in ("BG31_325", "BG36_341"):
        amt = 3 * mult
        if option == 2 and cid == "BG36_341":
            return replace(snap, board=_buff_all_minions(snap.board, 2 * mult, 2 * mult))
        return replace(snap, board=_buff_best_board_minion(snap.board, amt, amt))
    return snap


def _apply_hero_power_effect(snap: GameSnapshot, action: UseHeroPower) -> GameSnapshot:
    """Apply deterministic state transition for targeted & economy Hero Powers."""
    hp_id = action.card_id
    t_uid = action.target_uid

    # 1. Reno Jackson (Gonna Be Rich!) / Golden Friendship: Make target board minion Golden
    if hp_id in ("TB_BaconShop_HP_046", "BGDUO_Anomaly_003t") and snap.board:
        idx = next((i for i, m in enumerate(snap.board) if m.uid == t_uid and not m.golden), None)
        if idx is None:
            idx = next((i for i, m in enumerate(snap.board) if not m.golden), None)
        if idx is not None:
            m = snap.board[idx]
            g_min = replace(
                m,
                name=f"{m.name.rstrip('*')}*",
                atk=max(m.atk + 2, m.atk * 2),
                health=max(m.health + 2, m.health * 2),
                golden=True,
            )
            return replace(snap, board=snap.board[:idx] + (g_min,) + snap.board[idx + 1 :])

    # 2. Zerek, Master Cloner (Cloning Gallery): Summon an exact copy of target friendly minion
    if hp_id == "BG31_HERO_005p" and snap.board and len(snap.board) < BOARD_LIMIT:
        idx = next((i for i, m in enumerate(snap.board) if m.uid == t_uid), 0)
        m = snap.board[idx]
        clone = replace(m, uid=snap.next_uid, newly_bought=True)
        return replace(snap, board=snap.board + (clone,), next_uid=snap.next_uid + 1)

    # 3. Piggy Bank (TB_BaconShop_HP_076): Gain Gold
    if hp_id == "TB_BaconShop_HP_076":
        gain = max(2, min(8, snap.turn))
        return replace(snap, gold=snap.gold + gain)

    # 4. Shudderwock (TB_BaconShop_HP_022): Re-trigger target minion's Battlecry
    if hp_id == "TB_BaconShop_HP_022" and snap.board:
        idx = next((i for i, m in enumerate(snap.board) if m.uid == t_uid), 0)
        return _apply_battlecry_effect(snap, snap.board[idx])

    # 5. Major Hymn / Minor Hymn (BG26_HERO_102p / BG26_HERO_102p2)
    if hp_id in ("BG26_HERO_102p", "BG26_HERO_102p2") and snap.board:
        idx = next((i for i, m in enumerate(snap.board) if m.uid == t_uid), 0)
        m = snap.board[idx]
        add_a = snap.tier if hp_id == "BG26_HERO_102p" else 0
        add_h = snap.tier if hp_id == "BG26_HERO_102p2" else 0
        buffed = replace(m, atk=m.atk + add_a, health=m.health + add_h)
        return replace(snap, board=snap.board[:idx] + (buffed,) + snap.board[idx + 1 :])

    # 6. Teron Gorefiend (BG25_HERO_103p): Combat resummon value modeled as immediate combat stat equivalent
    if hp_id == "BG25_HERO_103p" and snap.board:
        idx = next((i for i, m in enumerate(snap.board) if m.uid == t_uid), 0)
        m = snap.board[idx]
        buffed = replace(m, atk=m.atk + max(2, m.tier), health=m.health + max(2, m.tier))
        return replace(snap, board=snap.board[:idx] + (buffed,) + snap.board[idx + 1 :])

    # 7. Galakrond's Greed (TB_BaconShop_HP_011): Evolve target shop minion to tier + 1
    if hp_id == "TB_BaconShop_HP_011" and snap.shop:
        idx = next((i for i, m in enumerate(snap.shop) if m.uid == t_uid), 0)
        m = snap.shop[idx]
        new_t = min(MAX_TIER, m.tier + 1)
        evolved = replace(m, tier=new_t, atk=m.atk + 2, health=m.health + 2)
        return replace(snap, shop=snap.shop[:idx] + (evolved,) + snap.shop[idx + 1 :])

    return snap


def _apply_sell_effect(snap: GameSnapshot, minion: TMinion) -> GameSnapshot:
    """Apply deterministic on-sell effects (tokens, team buffs)."""
    mult = 2 if minion.golden else 1
    cid = minion.card_id
    # Sellemental: When you sell this, get a 3/3 Elemental in hand
    if cid == "BGS_115" and len(snap.hand) < HAND_LIMIT:
        droplet = TMinion(
            uid=snap.next_uid,
            card_id="BGS_115t",
            name="Water Droplet*" if minion.golden else "Water Droplet",
            atk=3 * mult,
            health=3 * mult,
            tier=1,
            golden=minion.golden,
            tribe="Elemental",
            card_type="MINION",
            newly_bought=False,
        )
        return replace(snap, hand=snap.hand + (droplet,), next_uid=snap.next_uid + 1)
    # Fire Baller: When you sell this, give your minions +1 Attack
    if cid == "BG31_816":
        return replace(snap, board=_buff_all_minions(snap.board, 1 * mult, 0))
    # Snow Baller: When you sell this, give your minions +1 Health
    if cid == "BG31_818":
        return replace(snap, board=_buff_all_minions(snap.board, 0, 1 * mult))
    return snap


# -- MinionPool for Expectimax Roll & Discover ------------------------------


class MinionPool:
    """Lobby-filtered minion pool indexed by tavern tier."""

    def __init__(self, cards: CardDB, tracker=None) -> None:
        self.cards = cards
        self.tracker = tracker
        self.by_tier: dict[int, list[Card]] = {t: [] for t in range(1, 8)}
        self._rebuild()

    def _rebuild(self) -> None:
        for t in self.by_tier:
            self.by_tier[t].clear()
        for card in getattr(self.cards, "pool", ()):
            if not (1 <= card.tier <= 7):
                continue
            if self.tracker is not None:
                races = {r for r in card.races if r != "ALL"}
                if races and self.tracker.status(races) == "absent":
                    continue
            self.by_tier[card.tier].append(card)

    def available_up_to_tier(self, tier: int) -> list[Card]:
        out: list[Card] = []
        for t in range(1, min(6, max(1, tier)) + 1):
            out.extend(self.by_tier.get(t, ()))
        return out

    def sample_shop(
        self,
        tier: int,
        rng: random.Random | None = None,
        start_uid: int = 200_000,
    ) -> tuple[TMinion, ...]:
        """Sample a random shop offering for `tier` weighted by copy counts."""
        r = rng or random.Random()
        candidates = self.available_up_to_tier(tier)
        count = SHOP_SIZE_BY_TIER.get(max(1, min(6, tier)), 4)
        if not candidates:
            return ()
        weights = [COPIES_BY_TIER.get(c.tier, 10) for c in candidates]
        chosen = r.choices(candidates, weights=weights, k=count)
        return tuple(
            TMinion(
                uid=start_uid + i,
                card_id=c.id,
                name=c.name,
                atk=c.attack,
                health=c.health,
                tier=c.tier,
                tribe=c.tribe,
            )
            for i, c in enumerate(chosen)
        )


# -- Snapshot Builder -------------------------------------------------------


def _resolve_card_type(bg, entity_id: int, card_id: str) -> str:
    store = getattr(bg, "s", None)
    if store is not None:
        ent = store.entities.get(entity_id)
        if ent is not None:
            ctype = ent.tag("CARDTYPE")
            if ctype:
                return ctype
    cards = getattr(bg, "cards", None)
    if cards is not None and card_id:
        card = cards.by_id(card_id)
        if card and card.type:
            return card.type
    return "MINION"


def _is_playable_hand_entity(bg, entity_id: int) -> bool:
    store = getattr(bg, "s", None)
    if store is None:
        return True
    ent = store.entities.get(entity_id)
    if ent is None:
        return True
    if (
        ent.int_tag("LITERALLY_UNPLAYABLE") == 1
        or ent.int_tag("CANT_PLAY") == 1
        or ent.int_tag("UNPLAYABLE_VISUALS") == 1
        or ent.int_tag("2471") == 1
    ):
        return False
    # Ambassador Faelin's Expedition Plans locks Tier 2/4/6 discovered cards until you reach that tavern tier
    curr_tier = getattr(bg, "tier", 1) or 1
    if ent.int_tag("CANT_DISCARD") == 1 and ent.int_tag("TECH_LEVEL", 0) > curr_tier:
        return False
    return True


def _hand_entity_cost(bg, entity_id: int) -> int:
    store = getattr(bg, "s", None)
    if store is None:
        return 0
    ent = store.entities.get(entity_id)
    if ent is None:
        return 0
    if ent.int_tag("HIDE_COST") == 1:
        return 0
    return max(0, ent.int_tag("COST", 0))


def _to_tminion(m, card_type: str = "MINION", playable: bool = True, cost: int = 0) -> TMinion:
    return TMinion(
        uid=m.id,
        card_id=m.card_id,
        name=m.name or m.card_id,
        atk=m.atk,
        health=m.health,
        tier=m.tier,
        golden=bool(getattr(m, "golden", False)),
        frozen=bool(getattr(m, "frozen", False)),
        tribe=getattr(m, "tribe", ""),
        card_type=card_type,
        playable=playable,
        cost=cost,
    )


def snapshot_from_bg(bg) -> GameSnapshot:
    """Build an immutable GameSnapshot from a live BGState (or test double)."""
    board = tuple(_to_tminion(m, "MINION") for m in getattr(bg, "board", ()))
    shop = tuple(_to_tminion(m, "MINION") for m in getattr(bg, "shop", ()))
    hand_items: list[TMinion] = []
    for m in getattr(bg, "hand", ()):
        ctype = _resolve_card_type(bg, m.id, m.card_id)
        if ctype == "HERO":
            continue
        hand_items.append(
            _to_tminion(
                m,
                ctype,
                playable=_is_playable_hand_entity(bg, m.id),
                cost=_hand_entity_cost(bg, m.id),
            )
        )
    spells = tuple(
        TSpell(
            uid=sp.id,
            card_id=sp.card_id,
            name=sp.name or sp.card_id,
            cost=sp.cost,
            tier=sp.tier,
            frozen=bool(getattr(sp, "frozen", False)),
        )
        for sp in getattr(bg, "shop_spells", ())
    )
    frozen_shop = any(m.frozen for m in shop) or any(s.frozen for s in spells)
    max_uid = max(
        (x.uid for x in (*board, *hand_items, *shop, *spells)),
        default=0,
    )
    roll_cost = getattr(bg, "reroll_cost", None)
    if roll_cost is None:
        roll_cost = 1
    buy_cost = getattr(bg, "buy_cost", None)
    if buy_cost is None:
        buy_cost = 3

    hp = getattr(bg, "hero_power", None)
    hp_id = getattr(hp, "card_id", "") if hp else ""
    hp_name = getattr(hp, "name", "") if hp else ""
    hp_cost = getattr(hp, "cost", 0) if hp else 0
    hp_usable = bool(getattr(hp, "playable", False)) if hp else False

    return GameSnapshot(
        turn=getattr(bg, "turn", 0) or 0,
        tier=getattr(bg, "tier", 1) or 1,
        gold=getattr(bg, "gold", 0) or 0,
        upgrade_cost=getattr(bg, "upgrade_cost", None),
        board=board,
        hand=tuple(hand_items),
        shop=shop,
        shop_spells=spells,
        roll_cost=roll_cost,
        buy_cost=buy_cost,
        hero_card_id=getattr(bg, "hero_card_id", "") or "",
        hero_health=getattr(bg, "hero_health", 30) or 30,
        hero_armor=getattr(bg, "hero_armor", 0) or 0,
        leaderboard_place=getattr(bg, "leaderboard_place", 0) or 0,
        hero_power_id=hp_id,
        hero_power_name=hp_name or hp_id,
        hero_power_cost=hp_cost,
        hero_power_usable=hp_usable,
        frozen_shop=frozen_shop,
        next_uid=max(100_000, max_uid + 1),
    )


# -- Transition Model -------------------------------------------------------


def _non_golden_copies(snap: GameSnapshot, card_id: str) -> tuple[list[TMinion], list[TMinion]]:
    """Return (hand_matches, board_matches) of non-golden copies for card_id."""
    if not card_id:
        return [], []
    in_hand = [
        m
        for m in snap.hand
        if not m.golden and m.card_type == "MINION" and m.card_id == card_id
    ]
    in_board = [m for m in snap.board if not m.golden and m.card_id == card_id]
    return in_hand, in_board


def legal_actions(snap: GameSnapshot) -> list[Action]:
    """Enumerate legal deterministic actions plus the stochastic RollShop boundary."""
    if snap.rolled:
        return []

    actions: list[Action] = []

    for m in snap.hand:
        if not m.playable:
            continue
        opts = choose_one_labels(m.card_id) if not _has_choose_one_combiner(snap) else None
        if m.card_type == "MINION":
            if len(snap.board) < BOARD_LIMIT:
                if opts:
                    t1 = select_card_target(snap, m.card_id, option=1)
                    t2 = select_card_target(snap, m.card_id, option=2)
                    actions.append(PlayMinion(m.uid, m.card_id, m.name, option=1, option_label=opts[0], target_name=t1.name if t1 else ""))
                    actions.append(PlayMinion(m.uid, m.card_id, m.name, option=2, option_label=opts[1], target_name=t2.name if t2 else ""))
                else:
                    t0 = select_card_target(snap, m.card_id, option=0)
                    actions.append(PlayMinion(m.uid, m.card_id, m.name, target_name=t0.name if t0 else ""))
        elif m.card_type in ("SPELL", "BATTLEGROUND_SPELL") and snap.gold >= m.cost:
            if opts:
                t1 = select_card_target(snap, m.card_id, option=1)
                t2 = select_card_target(snap, m.card_id, option=2)
                actions.append(PlaySpell(m.uid, m.card_id, m.name, m.cost, option=1, option_label=opts[0], target_name=t1.name if t1 else ""))
                actions.append(PlaySpell(m.uid, m.card_id, m.name, m.cost, option=2, option_label=opts[1], target_name=t2.name if t2 else ""))
            else:
                t0 = select_card_target(snap, m.card_id, option=0)
                actions.append(PlaySpell(m.uid, m.card_id, m.name, m.cost, target_name=t0.name if t0 else ""))

    if snap.gold >= snap.buy_cost:
        for m in snap.shop:
            in_hand, in_board = _non_golden_copies(snap, m.card_id)
            forms_triple = (len(in_hand) + len(in_board)) >= 2
            if len(snap.hand) < HAND_LIMIT or (forms_triple and len(in_hand) >= 1):
                actions.append(BuyMinion(m.uid, m.card_id, m.name, snap.buy_cost))

    if len(snap.hand) < HAND_LIMIT:
        for sp in snap.shop_spells:
            if snap.gold >= sp.cost:
                opts = choose_one_labels(sp.card_id) if not _has_choose_one_combiner(snap) else None
                if opts:
                    t1 = select_card_target(snap, sp.card_id, option=1)
                    t2 = select_card_target(snap, sp.card_id, option=2)
                    actions.append(BuySpell(sp.uid, sp.card_id, sp.name, sp.cost, option=1, option_label=opts[0], target_name=t1.name if t1 else ""))
                    actions.append(BuySpell(sp.uid, sp.card_id, sp.name, sp.cost, option=2, option_label=opts[1], target_name=t2.name if t2 else ""))
                else:
                    t0 = select_card_target(snap, sp.card_id, option=0)
                    actions.append(BuySpell(sp.uid, sp.card_id, sp.name, sp.cost, target_name=t0.name if t0 else ""))

    if (
        not snap.upgraded_this_turn
        and snap.tier < MAX_TIER
        and snap.upgrade_cost is not None
        and snap.gold >= snap.upgrade_cost
    ):
        actions.append(UpgradeTavern(snap.upgrade_cost, snap.tier + 1))

    if snap.hero_power_usable and snap.hero_power_id and snap.gold >= snap.hero_power_cost:
        hp_eval = evaluate_hero_power_timing(snap)
        if hp_eval["should_use"]:
            actions.append(
                UseHeroPower(
                    snap.hero_power_id,
                    snap.hero_power_name,
                    snap.hero_power_cost,
                    target_uid=hp_eval["target_uid"],
                    target_name=hp_eval["target_name"],
                )
            )

    for m in snap.board:
        if not m.newly_bought:
            actions.append(SellMinion(m.uid, m.card_id, m.name, snap.sell_value))

    if snap.gold >= snap.roll_cost:
        actions.append(RollShop(snap.roll_cost))

    return actions


def apply_action(snap: GameSnapshot, action: Action) -> GameSnapshot:
    """Pure state transition: apply one action and return the next GameSnapshot."""
    if isinstance(action, (EndTurn, ReorderBoard)):
        return snap

    if isinstance(action, PlayMinion):
        if len(snap.board) >= BOARD_LIMIT:
            raise ValueError("Board is full")
        idx = next((i for i, m in enumerate(snap.hand) if m.uid == action.uid), None)
        if idx is None:
            raise ValueError(f"Minion uid={action.uid} not in hand")
        minion = snap.hand[idx]
        if minion.card_type != "MINION" or not minion.playable:
            raise ValueError(f"Hand card uid={action.uid} is not a playable minion")
        new_hand = snap.hand[:idx] + snap.hand[idx + 1 :]
        nxt = replace(snap, hand=new_hand, board=snap.board + (minion,))
        return _apply_battlecry_effect(nxt, minion, option=action.option)

    if isinstance(action, PlaySpell):
        idx = next((i for i, m in enumerate(snap.hand) if m.uid == action.uid), None)
        if idx is None:
            raise ValueError(f"Spell uid={action.uid} not in hand")
        sp = snap.hand[idx]
        if not sp.playable or snap.gold < action.cost:
            raise ValueError(f"Cannot cast hand spell uid={action.uid}")
        new_hand = snap.hand[:idx] + snap.hand[idx + 1 :]
        nxt = replace(
            snap,
            gold=snap.gold - action.cost,
            hand=new_hand,
            spells_cast=snap.spells_cast + (sp.card_id,),
        )
        return _apply_spell_effect(nxt, sp.card_id, option=action.option)

    if isinstance(action, BuyMinion):
        if snap.gold < action.cost:
            raise ValueError("Insufficient gold for BuyMinion")
        idx = next((i for i, m in enumerate(snap.shop) if m.uid == action.uid), None)
        if idx is None:
            raise ValueError(f"Minion uid={action.uid} not in shop")
        bought = snap.shop[idx]
        new_shop = snap.shop[:idx] + snap.shop[idx + 1 :]
        in_hand, in_board = _non_golden_copies(snap, bought.card_id)
        if len(in_hand) + len(in_board) >= 2:
            to_consume = (in_hand + in_board)[:2]
            consume_uids = {c.uid for c in to_consume}
            new_hand = tuple(m for m in snap.hand if m.uid not in consume_uids)
            new_board = tuple(m for m in snap.board if m.uid not in consume_uids)
            if len(new_hand) >= HAND_LIMIT:
                raise ValueError("Hand full even after triple merge")
            total_atk = sum(c.atk for c in to_consume) + bought.atk
            total_hp = sum(c.health for c in to_consume) + bought.health
            golden_minion = TMinion(
                uid=snap.next_uid,
                card_id=bought.card_id,
                name=f"{bought.name}*",
                atk=total_atk,
                health=total_hp,
                tier=bought.tier,
                golden=True,
                frozen=False,
                tribe=bought.tribe,
                card_type="MINION",
                newly_bought=True,
            )
            return replace(
                snap,
                gold=snap.gold - action.cost,
                shop=new_shop,
                board=new_board,
                hand=new_hand + (golden_minion,),
                triples_formed=snap.triples_formed + 1,
                next_uid=snap.next_uid + 1,
            )
        if len(snap.hand) >= HAND_LIMIT:
            raise ValueError("Hand is full")
        acquired = replace(bought, frozen=False, newly_bought=True)
        return replace(
            snap,
            gold=snap.gold - action.cost,
            shop=new_shop,
            hand=snap.hand + (acquired,),
        )

    if isinstance(action, BuySpell):
        if snap.gold < action.cost:
            raise ValueError("Insufficient gold for BuySpell")
        idx = next((i for i, sp in enumerate(snap.shop_spells) if sp.uid == action.uid), None)
        if idx is None:
            raise ValueError(f"Spell uid={action.uid} not in shop")
        sp = snap.shop_spells[idx]
        new_spells = snap.shop_spells[:idx] + snap.shop_spells[idx + 1 :]
        nxt = replace(
            snap,
            gold=snap.gold - action.cost,
            shop_spells=new_spells,
            spells_cast=snap.spells_cast + (sp.card_id,),
        )
        return _apply_spell_effect(nxt, sp.card_id, option=action.option)

    if isinstance(action, SellMinion):
        idx = next((i for i, m in enumerate(snap.board) if m.uid == action.uid), None)
        if idx is None:
            raise ValueError(f"Minion uid={action.uid} not on board")
        sold = snap.board[idx]
        new_board = snap.board[:idx] + snap.board[idx + 1 :]
        nxt = replace(snap, gold=snap.gold + action.gain, board=new_board)
        return _apply_sell_effect(nxt, sold)

    if isinstance(action, UpgradeTavern):
        if snap.upgraded_this_turn or snap.tier >= MAX_TIER:
            raise ValueError("Cannot upgrade tavern")
        if snap.upgrade_cost is None or snap.gold < action.cost:
            raise ValueError("Insufficient gold for UpgradeTavern")
        return replace(
            snap,
            gold=snap.gold - action.cost,
            tier=snap.tier + 1,
            upgrade_cost=None,
            upgraded_this_turn=True,
        )

    if isinstance(action, UseHeroPower):
        if not snap.hero_power_usable or snap.gold < action.cost:
            raise ValueError("Hero Power not usable")
        nxt = replace(
            snap,
            gold=snap.gold - action.cost,
            hero_power_usable=False,
        )
        return _apply_hero_power_effect(nxt, action)

    if isinstance(action, FreezeShop):
        return replace(
            snap,
            frozen_shop=True,
            shop=tuple(replace(m, frozen=True) for m in snap.shop),
            shop_spells=tuple(replace(s, frozen=True) for s in snap.shop_spells),
        )

    if isinstance(action, RollShop):
        if snap.gold < action.cost:
            raise ValueError("Insufficient gold for RollShop")
        return replace(
            snap,
            gold=snap.gold - action.cost,
            shop=(),
            shop_spells=(),
            frozen_shop=False,
            rolled=True,
        )

    raise ValueError(f"Unsupported action: {action!r}")


def state_key(snap: GameSnapshot) -> tuple:
    """Canonical hashable key for transposition table deduplication."""
    return (
        snap.gold,
        snap.tier,
        snap.upgraded_this_turn,
        snap.hero_power_usable,
        snap.triples_formed,
        snap.future_gold_bonus,
        snap.frozen_shop,
        snap.rolled,
        tuple(sorted((m.card_id, m.atk, m.health, m.golden) for m in snap.board)),
        tuple(sorted((m.card_id, m.atk, m.health, m.golden, m.card_type) for m in snap.hand)),
        tuple(sorted((m.uid, m.atk, m.health) for m in snap.shop)),
        tuple(sorted(s.uid for s in snap.shop_spells)),
        tuple(sorted(snap.spells_cast)),
    )
