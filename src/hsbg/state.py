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
        self.game_entity_id: int | None = None
        self.players: list[P.PlayerDecl] = []
        # Player-level tags arrive keyed by battletag, which never appears in a
        # bracketed reference, so those cannot be resolved to an entity id.
        # Keep them by name and bind the names to players in arrival order.
        self.by_name: dict[str, dict[str, str]] = {}
        self._name_order: list[str] = []

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

    def player_tags(self, player_id: int) -> dict[str, str]:
        """Tags for a player, merged from id-keyed and name-keyed sources."""
        merged: dict[str, str] = {}
        for p in self.players:
            if p.player_id == player_id:
                merged.update(self.entities.get(p.entity_id, Entity(0)).tags)
        # First distinct name seen belongs to the local player, second to the
        # opposing slot. Heuristic -- see module docstring of tools/replay.py.
        local = self.local_player
        idx = 0 if (local and local.player_id == player_id) else 1
        if idx < len(self._name_order):
            merged.update(self.by_name.get(self._name_order[idx], {}))
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
            case P.ShowEntity():
                e = self.resolve(ev.ref)
                if e:
                    e.card_id = ev.card_id or e.card_id
                    e.tags.update(ev.tags)
            case P.HideEntity():
                e = self.resolve(ev.ref)
                if e:
                    e.tags[ev.tag] = ev.value
            case P.TagChange():
                e = self.resolve(ev.ref)
                if e is not None:
                    e.tags[ev.tag] = ev.value
                elif ev.ref.name:
                    n = ev.ref.name
                    if n not in self.by_name:
                        self.by_name[n] = {}
                        self._name_order.append(n)
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

    def _minions(self, controller: int | None) -> list[Minion]:
        if controller is None:
            return []
        out = []
        for e in self.s.entities.values():
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

    @property
    def shop(self) -> list[Minion]:
        bob = self.s.bob_player
        return self._minions(bob.player_id if bob else None)

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
        for e in self.s.entities.values():
            if e.tag("ZONE") != "HAND" or e.int_tag("CONTROLLER", -1) != me.player_id:
                continue
            name, tier, tribe = self._card_name(e)
            out.append(Minion(e.id, e.card_id, name, e.int_tag("ATK"), e.int_tag("HEALTH"),
                              tier, e.int_tag("ZONE_POSITION"), tribe=tribe))
        return out

    def _my_tags(self) -> dict[str, str]:
        me = self.s.local_player
        return self.s.player_tags(me.player_id) if me else {}

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
