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

    def describe(self) -> str:
        return t("act_buy_spell", name=self.name, cost=self.cost)


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

    def describe(self) -> str:
        return t("act_play_minion", name=self.name)


@dataclass(frozen=True)
class PlaySpell:
    uid: int
    card_id: str
    name: str
    cost: int = 0

    def describe(self) -> str:
        unit = "з" if get_lang() == "uk" else "g"
        cost_str = f" (-{self.cost}{unit})" if self.cost > 0 else ""
        return t("act_play_spell", name=self.name, cost_str=cost_str)


@dataclass(frozen=True)
class UseHeroPower:
    card_id: str
    name: str
    cost: int = 0

    def describe(self) -> str:
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


# -- Deterministic Tavern Effect Registries ---------------------------------


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


def _apply_spell_effect(snap: GameSnapshot, card_id: str) -> GameSnapshot:
    """Apply deterministic economy/stat effects for common Tavern Spells & coins."""
    # Tavern Coin / The Coin / Hasty Excavation (+1 Gold)
    if card_id in ("BG28_810", "GAME_005", "BG28_571"):
        return replace(snap, gold=snap.gold + 1)
    # Careful Investment (+2 Gold next turn)
    if card_id == "BG28_800":
        return replace(snap, future_gold_bonus=snap.future_gold_bonus + 2)
    # Blood Gem (+1/+1)
    if card_id.startswith("BG20_GEM"):
        return replace(snap, board=_buff_best_board_minion(snap.board, 1, 1))
    # Tavern Dish Banana / Alliance Flag (+2/+2)
    if card_id in ("BG28_897", "BG31_880"):
        return replace(snap, board=_buff_best_board_minion(snap.board, 2, 2))
    # Pointy Arrow (+4 Attack)
    if card_id == "EBG_Spell_014":
        return replace(snap, board=_buff_best_board_minion(snap.board, 4, 0))
    # Fortify (+3 Health)
    if card_id == "BG28_503":
        return replace(snap, board=_buff_best_board_minion(snap.board, 0, 3))
    # Them Apples: Give minions in the Tavern +1/+2
    if card_id == "BG28_966":
        return replace(snap, shop=_buff_all_minions(snap.shop, 1, 2))
    return snap


def _apply_battlecry_effect(snap: GameSnapshot, minion: TMinion) -> GameSnapshot:
    """Apply deterministic battlecry/economy effects when a minion is played."""
    mult = 2 if minion.golden else 1
    cid = minion.card_id
    # Shell Collector: Get a Tavern Coin (+1g immediately usable)
    if cid == "BG23_002":
        return replace(snap, gold=snap.gold + 1 * mult)
    # Southsea Busker: Gain 1 Gold next turn
    if cid == "BG26_135":
        return replace(snap, future_gold_bonus=snap.future_gold_bonus + 1 * mult)
    # Razorfen Geomancer / Crater Miner: +2 Blood Gems (+2/+2)
    if cid in ("BG20_100", "BG31_320"):
        return replace(snap, board=_buff_best_board_minion(snap.board, 2 * mult, 2 * mult))
    # Fearless Foodie: +4 Blood Gems (+4/+4)
    if cid == "BG30_123":
        return replace(snap, board=_buff_best_board_minion(snap.board, 4 * mult, 4 * mult))
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
    if ent.int_tag("LITERALLY_UNPLAYABLE") == 1 or ent.int_tag("CANT_PLAY") == 1:
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
        if m.card_type == "MINION":
            if len(snap.board) < BOARD_LIMIT:
                actions.append(PlayMinion(m.uid, m.card_id, m.name))
        elif m.card_type in ("SPELL", "BATTLEGROUND_SPELL") and snap.gold >= m.cost:
            actions.append(PlaySpell(m.uid, m.card_id, m.name, m.cost))

    if snap.gold >= snap.buy_cost:
        for m in snap.shop:
            in_hand, in_board = _non_golden_copies(snap, m.card_id)
            forms_triple = (len(in_hand) + len(in_board)) >= 2
            if len(snap.hand) < HAND_LIMIT or (forms_triple and len(in_hand) >= 1):
                actions.append(BuyMinion(m.uid, m.card_id, m.name, snap.buy_cost))

    if len(snap.hand) < HAND_LIMIT:
        for sp in snap.shop_spells:
            if snap.gold >= sp.cost:
                actions.append(BuySpell(sp.uid, sp.card_id, sp.name, sp.cost))

    if (
        not snap.upgraded_this_turn
        and snap.tier < MAX_TIER
        and snap.upgrade_cost is not None
        and snap.gold >= snap.upgrade_cost
    ):
        actions.append(UpgradeTavern(snap.upgrade_cost, snap.tier + 1))

    if snap.hero_power_usable and snap.hero_power_id and snap.gold >= snap.hero_power_cost:
        actions.append(
            UseHeroPower(snap.hero_power_id, snap.hero_power_name, snap.hero_power_cost)
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
        return _apply_battlecry_effect(nxt, minion)

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
        return _apply_spell_effect(nxt, sp.card_id)

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
        return _apply_spell_effect(nxt, sp.card_id)

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
        return replace(
            snap,
            gold=snap.gold - action.cost,
            hero_power_usable=False,
        )

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
