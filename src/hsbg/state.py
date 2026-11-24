"""Entity store and Battlegrounds state projection.

Two layers, deliberately separate:

* ``EntityStore`` is generic Hearthstone -- ids, cardIds and tags. It knows
  nothing about Battlegrounds.
* ``BGState`` projects that store into shop / board / hand / gold / tier.

The projection turns on one fact: the shop and your board both live in
``ZONE=PLAY`` and are told apart only by which player controls them. Bartender
Bob is identified by his null game account id, not by a hardcoded player id,
because the ids vary between games.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import parser as P
from .carddb import CardDB
from .combat import CMinion

BOB_NAME = "Bartender Bob"
COMBAT_KEYWORDS = {
    "TAUNT": "taunt",
    "DIVINE_SHIELD": "divine_shield",
    "POISONOUS": "poisonous",
    "VENOMOUS": "venomous",
    "WINDFURY": "windfury",
    "MEGA_WINDFURY": "mega_windfury",
    "REBORN": "reborn",
}


@dataclass
class Entity:
    id: int
    card_id: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    def tag(self, name: str, default=None):
        return self.tags.get(name, default)

    def int_tag(self, name: str, default: int = 0) -> int:
        v = self.tags.get(name)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @property
    def name(self) -> str:
        return self.tags.get("_name", "") or self.card_id


class EntityStore:
    def __init__(self) -> None:
        self.entities: dict[int, Entity] = {}
        self.in_play: dict[int, Entity] = {}
        self.in_hand: dict[int, Entity] = {}
        self.game_entity_id: int | None = None
        self.players: list[P.PlayerDecl] = []
        # Player-level tags arrive keyed by battletag, which never appears in a
        # bracketed reference, so those cannot be resolved to an entity id.
        # Keep them by name and bind the names to players in arrival order.
        self.by_name: dict[str, dict[str, str]] = {}
        self._name_order: list[str] = []
        self._local_name: str | None = None
        self._bob_name: str | None = None

    def _sync_zone(self, e: Entity) -> None:
        z = e.tags.get("ZONE")
        if z == "PLAY":
            self.in_play[e.id] = e
            self.in_hand.pop(e.id, None)
        elif z == "HAND":
            self.in_hand[e.id] = e
            self.in_play.pop(e.id, None)
        else:
            self.in_play.pop(e.id, None)
            self.in_hand.pop(e.id, None)

    # -- lookup ---------------------------------------------------------
    def get(self, eid: int) -> Entity:
        e = self.entities.get(eid)
        if e is None:
            e = self.entities[eid] = Entity(eid)
        return e

    def resolve(self, ref: P.EntityRef) -> Entity | None:
        if ref.is_game:
            return self.get(self.game_entity_id) if self.game_entity_id else None
        if ref.id is not None:
            e = self.get(ref.id)
            if ref.card_id and not e.card_id:
                e.card_id = ref.card_id
            if ref.name:
                e.tags["_name"] = ref.name
            return e
        return None

    @property
    def local_player(self) -> P.PlayerDecl | None:
        """The human. Bob is the slot with a null game account."""
        real = [p for p in self.players if not p.is_bot_slot]
        return real[0] if real else None

    @property
    def bob_player(self) -> P.PlayerDecl | None:
        bots = [p for p in self.players if p.is_bot_slot]
        return bots[0] if bots else None

    def _resolve_name_key(self, is_local: bool) -> str | None:
        if is_local:
            if self._local_name and self._local_name in self.by_name:
                return self._local_name
            for n in self._name_order:
                if n != BOB_NAME and "#" in n:
                    return n
            for n in self._name_order:
                if n != BOB_NAME:
                    return n
            return None
        if self._bob_name and self._bob_name in self.by_name:
            return self._bob_name
        if BOB_NAME in self.by_name:
            return BOB_NAME
        local_key = self._resolve_name_key(is_local=True)
        for n in self._name_order:
            if n != local_key:
                return n
        return None

    def player_tags(self, player_id: int) -> dict[str, str]:
        """Tags for a player, merged from id-keyed and name-keyed sources."""
        merged: dict[str, str] = {}
        for p in self.players:
            if p.player_id == player_id:
                merged.update(self.entities.get(p.entity_id, Entity(0)).tags)
        local = self.local_player
        is_local = bool(local and local.player_id == player_id)
        name_key = self._resolve_name_key(is_local=is_local)
        if name_key is not None:
            merged.update(self.by_name.get(name_key, {}))
        return merged

    # -- ingest ---------------------------------------------------------
    def apply(self, ev) -> None:
        match ev:
            case P.CreateGame():
                self.__init__()  # a new game resets everything
            case P.GameEntityDecl():
                self.game_entity_id = ev.entity_id
                self.get(ev.entity_id).tags.update(ev.tags)
            case P.PlayerDecl():
                self.players.append(ev)
                self.get(ev.entity_id).tags.update(ev.tags)
            case P.FullEntity():
                e = self.get(ev.id)
                e.card_id = ev.card_id or e.card_id
                e.tags.update(ev.tags)
                self._sync_zone(e)
            case P.ShowEntity():
                e = self.resolve(ev.ref)
                if e:
                    e.card_id = ev.card_id or e.card_id
                    e.tags.update(ev.tags)
                    self._sync_zone(e)
            case P.HideEntity():
                e = self.resolve(ev.ref)
                if e:
                    e.tags[ev.tag] = ev.value
                    if ev.tag == "ZONE":
                        self._sync_zone(e)
            case P.ChoiceOffered() | P.ChoiceMade():
                if ev.player and ev.player != BOB_NAME:
                    self._local_name = ev.player
            case P.TagChange():
                e = self.resolve(ev.ref)
                if e is not None:
                    e.tags[ev.tag] = ev.value
                    if ev.tag == "ZONE":
                        self._sync_zone(e)
                elif ev.ref.name:
                    n = ev.ref.name
                    if n not in self.by_name:
                        self.by_name[n] = {}
                        self._name_order.append(n)
                    if n == BOB_NAME:
                        self._bob_name = n
                    elif "#" in n and self._local_name is None:
                        self._local_name = n
                    self.by_name[n][ev.tag] = ev.value


@dataclass
class Minion:
    id: int
    card_id: str
    name: str
    atk: int
    health: int
    tier: int
    pos: int
    frozen: bool = False
    golden: bool = False
    tribe: str = ""

    def __str__(self) -> str:
        label = self.name or self.card_id
        flags = "".join(f for f, on in (("*", self.golden), ("~", self.frozen)) if on)
        tribe = f" [{self.tribe}]" if self.tribe else ""
        return f"{label}{flags} {self.atk}/{self.health} (t{self.tier}){tribe}"


@dataclass
class ShopSpell:
    id: int
    card_id: str
    name: str
    cost: int
    tier: int
    pos: int
    frozen: bool = False

    def __str__(self) -> str:
        mark = "~" if self.frozen else ""
        return f"{self.name or self.card_id}{mark} ({self.cost}g, t{self.tier})"


@dataclass
class HeroPower:
    id: int
    card_id: str
    name: str
    cost: int
    used: bool
    passive: bool
    playable: bool


@dataclass
class OpponentBoard:
    hero_card_id: str
    player_id: int
    turn_seen: int
    tier: int
    minions: list[CMinion]


class OpponentTracker:
    def __init__(self) -> None:
        self.boards: dict[str, OpponentBoard] = {}
        self.by_player_id: dict[int, OpponentBoard] = {}
        self.last_enemy_board: list[CMinion] = []

    def reset(self) -> None:
        self.boards.clear()
        self.by_player_id.clear()
        self.last_enemy_board = []

    def observe_combat(self, bg: BGState) -> OpponentBoard | None:
        if not bg.in_combat:
            return None
        bob = bg.s.bob_player
        if not bob:
            return None
        opp_minions = bg.combat_minions(bob.player_id)
        if not opp_minions:
            return None
        enemy_hero = bg.combat_enemy_hero
        hero_card_id = enemy_hero.card_id if enemy_hero else ""
        player_id = bg.next_opponent_id or (enemy_hero.int_tag("PLAYER_ID") if enemy_hero else 0)
        tier = (enemy_hero.int_tag("PLAYER_TECH_LEVEL") if enemy_hero else 0) or bg.tier
        board = OpponentBoard(
            hero_card_id=hero_card_id,
            player_id=player_id,
            turn_seen=bg.turn,
            tier=tier,
            minions=opp_minions,
        )
        key = hero_card_id or str(player_id)
        self.boards[key] = board
        if player_id:
            self.by_player_id[player_id] = board
        self.last_enemy_board = opp_minions
        return board

    def expected_opponent_board(self, bg: BGState) -> list[CMinion]:
        opp_id = bg.next_opponent_id
        if opp_id in self.by_player_id:
            return self.by_player_id[opp_id].minions
        return self.last_enemy_board


class BGState:
    """A read-only view over an EntityStore, projected for Battlegrounds."""

    def __init__(self, store: EntityStore, cards: CardDB | None = None) -> None:
        self.s = store
        self.cards = cards

    def _card_name(self, entity) -> tuple[str, int, str]:
        """Resolve display name, tier and tribe, preferring the card database.

        The log only prints an entity's name when it happens to emit a bracketed
        reference for it, so the database is the reliable source.
        """
        card = self.cards.by_id(entity.card_id) if self.cards else None
        name = (card.name if card else "") or entity.tag("_name", "") or entity.card_id
        tier = entity.int_tag("TECH_LEVEL") or (card.tier if card else 0)
        return name, tier, (card.tribe if card else "")

    def _play_entities(self):
        return self.s.in_play.values() if (self.s.in_play or self.s.in_hand) else self.s.entities.values()

    def _hand_entities(self):
        return self.s.in_hand.values() if (self.s.in_play or self.s.in_hand) else self.s.entities.values()

    def _minions(self, controller: int | None) -> list[Minion]:
        if controller is None:
            return []
        out = []
        for e in self._play_entities():
            if e.tag("CARDTYPE") != "MINION" or e.tag("ZONE") != "PLAY":
                continue
            if e.int_tag("CONTROLLER", -1) != controller:
                continue
            name, tier, tribe = self._card_name(e)
            out.append(
                Minion(
                    id=e.id,
                    card_id=e.card_id,
                    name=name,
                    atk=e.int_tag("ATK"),
                    health=e.int_tag("HEALTH"),
                    tier=tier,
                    pos=e.int_tag("ZONE_POSITION"),
                    frozen=e.int_tag("FROZEN") == 1,
                    golden=e.int_tag("PREMIUM") == 1,
                    tribe=tribe,
                )
            )
        return sorted(out, key=lambda m: m.pos)

    def combat_minions(self, controller: int | None) -> list[CMinion]:
        """Board minions in ZONE_POSITION order, ready for combat simulation."""
        if controller is None:
            return []
        ordered: list[tuple[int, int, CMinion]] = []
        for e in self._play_entities():
            if e.tag("CARDTYPE") != "MINION" or e.tag("ZONE") != "PLAY":
                continue
            if e.int_tag("CONTROLLER", -1) != controller:
                continue
            card = self.cards.by_id(e.card_id) if self.cards else None
            name, tier, _ = self._card_name(e)
            kw = {py: e.int_tag(tag) == 1 for tag, py in COMBAT_KEYWORDS.items()}
            golden = e.int_tag("PREMIUM") == 1
            base_atk = (card.attack * (2 if golden else 1)) if card else None
            base_health = (card.health * (2 if golden else 1)) if card else None
            current_health = max(0, e.int_tag("HEALTH") - e.int_tag("DAMAGE"))
            ordered.append((
                e.int_tag("ZONE_POSITION"),
                e.id,
                CMinion(
                    card_id=e.card_id,
                    name=name,
                    atk=e.int_tag("ATK"),
                    health=current_health,
                    tier=tier or 1,
                    golden=golden,
                    base_atk=base_atk,
                    base_health=base_health,
                    **kw,
                ),
            ))
        ordered.sort(key=lambda item: (item[0], item[1]))
        return [m for _, _, m in ordered]

    @property
    def shop(self) -> list[Minion]:
        bob = self.s.bob_player
        return self._minions(bob.player_id if bob else None)

    @property
    def shop_spells(self) -> list[ShopSpell]:
        bob = self.s.bob_player
        if not bob:
            return []
        out = []
        for e in self._play_entities():
            if e.tag("CARDTYPE") != "BATTLEGROUND_SPELL" or e.tag("ZONE") != "PLAY":
                continue
            if e.int_tag("CONTROLLER", -1) != bob.player_id:
                continue
            name, tier, _ = self._card_name(e)
            out.append(
                ShopSpell(
                    id=e.id,
                    card_id=e.card_id,
                    name=name,
                    cost=e.int_tag("COST"),
                    tier=tier,
                    pos=e.int_tag("ZONE_POSITION"),
                    frozen=e.int_tag("FROZEN") == 1,
                )
            )
        return sorted(out, key=lambda s: s.pos)

    @property
    def board(self) -> list[Minion]:
        me = self.s.local_player
        return self._minions(me.player_id if me else None)

    @property
    def hand(self) -> list[Minion]:
        me = self.s.local_player
        if not me:
            return []
        out = []
        for e in self._hand_entities():
            if e.tag("ZONE") != "HAND" or e.int_tag("CONTROLLER", -1) != me.player_id:
                continue
            name, tier, tribe = self._card_name(e)
            out.append(
                Minion(
                    e.id,
                    e.card_id,
                    name,
                    e.int_tag("ATK"),
                    e.int_tag("HEALTH"),
                    tier,
                    e.int_tag("ZONE_POSITION"),
                    golden=e.int_tag("PREMIUM") == 1,
                    tribe=tribe,
                )
            )
        return sorted(out, key=lambda m: m.pos)

    @property
    def hand_spells(self) -> list[ShopSpell]:
        me = self.s.local_player
        if not me:
            return []
        out = []
        for e in self._hand_entities():
            if e.tag("ZONE") != "HAND" or e.int_tag("CONTROLLER", -1) != me.player_id:
                continue
            if e.int_tag("LITERALLY_UNPLAYABLE") == 1 or e.int_tag("CANT_PLAY") == 1:
                continue
            card = self.cards.by_id(e.card_id) if self.cards else None
            ctype = e.tag("CARDTYPE") or (card.type if card else "")
            if ctype not in ("SPELL", "BATTLEGROUND_SPELL"):
                continue
            name, tier, _ = self._card_name(e)
            out.append(
                ShopSpell(
                    id=e.id,
                    card_id=e.card_id,
                    name=name,
                    cost=e.int_tag("COST"),
                    tier=tier,
                    pos=e.int_tag("ZONE_POSITION"),
                    frozen=e.int_tag("FROZEN") == 1,
                )
            )
        return sorted(out, key=lambda s: s.pos)

    @property
    def trinkets(self) -> list[Entity]:
        me = self.s.local_player
        if not me:
            return []
        return [
            e
            for e in self._play_entities()
            if e.tag("ZONE") == "PLAY"
            and e.int_tag("CONTROLLER", -1) == me.player_id
            and e.tag("CARDTYPE") == "BATTLEGROUND_TRINKET"
            and e.card_id not in ("BG30_Trinket_1st", "BG30_Trinket_2nd")
        ]

    @property
    def hero_power(self) -> HeroPower | None:
        me = self.s.local_player
        if not me:
            return None
        for e in self._play_entities():
            if (
                e.tag("CARDTYPE") == "HERO_POWER"
                and e.tag("ZONE") == "PLAY"
                and e.int_tag("CONTROLLER", -1) == me.player_id
            ):
                name, _, _ = self._card_name(e)
                cost = e.int_tag("COST", 0)
                passive = e.int_tag("HIDE_COST") == 1
                used = (
                    e.int_tag("EXHAUSTED") == 1
                    or e.int_tag("BACON_HERO_POWER_ACTIVATED") == 1
                )
                disabled = (
                    e.int_tag("HERO_POWER_DISABLED") == 1
                    or e.int_tag("CANT_PLAY") == 1
                    or e.int_tag("LITERALLY_UNPLAYABLE") == 1
                )
                playable = (not passive) and (not used) and (not disabled)
                return HeroPower(
                    id=e.id,
                    card_id=e.card_id,
                    name=name,
                    cost=cost,
                    used=used,
                    passive=passive,
                    playable=playable,
                )
        return None

    @property
    def reroll_cost(self) -> int:
        me = self.s.local_player
        if not me:
            return 1
        for e in self._play_entities():
            if (
                e.tag("ZONE") == "PLAY"
                and e.int_tag("CONTROLLER", -1) == me.player_id
                and e.card_id == "TB_BaconShop_8p_Reroll_Button"
            ):
                return e.int_tag("COST", 1)
        return 1

    @property
    def buy_cost(self) -> int:
        me = self.s.local_player
        if not me:
            return 3
        for e in self._play_entities():
            if (
                e.tag("ZONE") == "PLAY"
                and e.int_tag("CONTROLLER", -1) == me.player_id
                and e.card_id == "TB_BaconShop_DragBuy"
            ):
                if "COST" in e.tags:
                    return max(0, e.int_tag("COST", 3))
                return 3
        return 3

    @property
    def upgrade_cost(self) -> int | None:
        """Current tavern tier upgrade cost in gold, or None if unavailable."""
        me = self.s.local_player
        if not me:
            return None
        for e in self._play_entities():
            if (
                e.tag("ZONE") == "PLAY"
                and e.int_tag("CONTROLLER", -1) == me.player_id
                and e.card_id.startswith("TB_BaconShopTechUp")
            ):
                return e.int_tag("COST")
        return None

    @property
    def in_combat(self) -> bool:
        raw = self.raw_turn
        return raw > 0 and raw % 2 == 0

    @property
    def visual_raw_turn(self) -> int:
        """The animation queue's (PowerTaskList) TURN tag on GameEntity, or raw_turn if unset.

        GameState receives the next shop turn from the server ~0.8s after combat
        starts, whereas PowerTaskList advances GameEntity TURN only when the
        combat animation actually finishes on screen (~20-35s later).
        """
        g = self.s.entities.get(self.s.game_entity_id or -1)
        if not g:
            return 0
        return g.int_tag("VISUAL_TURN") or g.int_tag("TURN")

    @property
    def visual_in_combat(self) -> bool:
        """True while the client is visually in the combat phase on screen."""
        if self.in_combat:
            return True
        vraw = self.visual_raw_turn
        return vraw > 0 and vraw % 2 == 0

    def _my_tags(self) -> dict[str, str]:
        me = self.s.local_player
        return self.s.player_tags(me.player_id) if me else {}

    def _hero_entity(self) -> Entity | None:
        """The local player's actual equipped hero.

        HERO_ENTITY names its id but, like RESOURCES/PLAYER_TECH_LEVEL,
        arrives keyed by battletag rather than a resolvable entity ref -- see
        the module docstring and player_tags() -- so it comes from the same
        merged tag source as gold/tier, not a direct id lookup.
        """
        hid = self._my_tags().get("HERO_ENTITY")
        if not hid:
            return None
        try:
            return self.s.entities.get(int(hid))
        except (TypeError, ValueError):
            return None

    @property
    def gold(self) -> int:
        """Gold flickers 0->N as the client resyncs; this is the settled value."""
        t = self._my_tags()
        def n(k):
            try:
                return int(t.get(k, 0))
            except ValueError:
                return 0
        return max(0, n("RESOURCES") - n("RESOURCES_USED"))

    @property
    def tier(self) -> int:
        try:
            return int(self._my_tags().get("PLAYER_TECH_LEVEL", 0))
        except ValueError:
            return 0

    @property
    def raw_turn(self) -> int:
        """The game's TURN tag, which counts shop *and* combat phases."""
        g = self.s.entities.get(self.s.game_entity_id or -1)
        return g.int_tag("TURN") if g else 0

    @property
    def turn(self) -> int:
        """The Battlegrounds turn number as the player sees it.

        Each Battlegrounds turn spans two logged turns -- one shop, one combat --
        so the raw tag runs at roughly double. Verified against gold, which is
        min(10, turn + 2): raw 11 -> turn 6 -> 8 gold.
        """
        raw = self.raw_turn
        return (raw + 1) // 2 if raw else 0

    @property
    def max_gold(self) -> int:
        return min(10, self.turn + 2) if self.turn else 0

    @property
    def hero_card_id(self) -> str:
        hero = self._hero_entity()
        return hero.card_id if hero else ""

    @property
    def hero_health(self) -> int:
        """Remaining hero health, before armor. 0 before a hero is equipped."""
        hero = self._hero_entity()
        return max(0, hero.int_tag("HEALTH") - hero.int_tag("DAMAGE")) if hero else 0

    @property
    def hero_armor(self) -> int:
        hero = self._hero_entity()
        return hero.int_tag("ARMOR") if hero else 0

    @property
    def leaderboard_place(self) -> int:
        """1-8, Blizzard's own live rank -- already accounts for every real
        player's true health, which is why it beats anything derived locally.
        0 if unknown (e.g. before turn 1)."""
        hero = self._hero_entity()
        return hero.int_tag("PLAYER_LEADERBOARD_PLACE") if hero else 0

    @property
    def damage_last_turn(self) -> int:
        """Damage taken in the most recently resolved combat."""
        try:
            return int(self._my_tags().get("DAMAGE_DEALT_TO_HERO_LAST_TURN", 0))
        except ValueError:
            return 0

    @property
    def hero_offers(self) -> list[Entity]:
        """During the opening, the four candidate heroes sit in your hand."""
        me = self.s.local_player
        if not me:
            return []
        return [
            e for e in self.s.entities.values()
            if e.tag("CARDTYPE") == "HERO" and e.tag("ZONE") == "HAND"
            and e.int_tag("CONTROLLER", -1) == me.player_id
        ]

    @property
    def next_opponent_id(self) -> int:
        raw = self._my_tags().get("NEXT_OPPONENT_PLAYER_ID")
        if raw is not None:
            try:
                val = int(raw)
                if val:
                    return val
            except (TypeError, ValueError):
                pass
        hero = self._hero_entity()
        if hero:
            return hero.int_tag("NEXT_OPPONENT_PLAYER_ID", 0)
        return 0

    @property
    def combat_enemy_hero(self) -> Entity | None:
        bob = self.s.bob_player
        if not bob:
            return None
        for e in self.s.entities.values():
            if (
                e.tag("CARDTYPE") == "HERO"
                and e.tag("ZONE") == "PLAY"
                and e.int_tag("CONTROLLER", -1) == bob.player_id
            ):
                return e
        return None
