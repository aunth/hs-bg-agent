"""Parse Hearthstone Power.log into a stream of typed events.

Only ``GameState.DebugPrintPower()`` lines are parsed. The client logs every
power twice -- once from GameState (authoritative) and once from PowerTaskList
(the animation queue) -- so accepting both double-counts everything.

Structure comes from indentation: an entity declaration is followed by its
``tag=`` lines at a deeper indent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Only GameState lines, plus PowerTaskList's GameEntity TURN change so the UI knows
# when the client's combat animation actually finishes on screen.
_LINE = re.compile(
    r"^D \d\d:\d\d:\d\d\.\d+ GameState\.(?P<chan>DebugPrintPower|DebugPrintEntityChoices|"
    r"DebugPrintEntitiesChosen)\(\) - (?P<body>.*)$"
)
_TASKLIST_TURN = re.compile(
    r"^D \d\d:\d\d:\d\d\.\d+ PowerTaskList\.DebugPrintPower\(\) -\s*"
    r"TAG_CHANGE Entity=GameEntity tag=TURN value=(?P<value>\d+)\b"
)

_TAG = re.compile(r"^tag=(?P<tag>[A-Za-z0-9_]+) value=(?P<value>[A-Za-z0-9_-]+)$")
_FULL_ENTITY = re.compile(r"^FULL_ENTITY - (?:Creating|Updating) ID=(?P<id>\d+) CardID=(?P<card>\S*)$")
_SHOW_ENTITY = re.compile(r"^SHOW_ENTITY - Updating (?P<ref>.+?) CardID=(?P<card>\S*)$")
_HIDE_ENTITY = re.compile(r"^HIDE_ENTITY - (?P<ref>.+?) tag=(?P<tag>\S+) value=(?P<value>\S+)$")
_TAG_CHANGE = re.compile(r"^TAG_CHANGE Entity=(?P<ref>.+?) tag=(?P<tag>[A-Za-z0-9_]+) value=(?P<value>[A-Za-z0-9_-]+)\s*(?:DEF CHANGE.*)?$")
_BLOCK_START = re.compile(r"^BLOCK_START BlockType=(?P<type>\S+) Entity=(?P<ref>.+?) EffectCardId=(?P<effect>.*?) EffectIndex=")
_PLAYER = re.compile(r"^Player EntityID=(?P<eid>\d+) PlayerID=(?P<pid>\d+) GameAccountId=\[hi=(?P<hi>\d+) lo=(?P<lo>\d+)\]")
_GAME_ENTITY = re.compile(r"^GameEntity EntityID=(?P<eid>\d+)$")

# Discover / hero select, on the DebugPrintEntityChoices channel:
#   id=1 Player=name TaskList=7 ChoiceType=MULLIGAN CountMin=1 CountMax=1
#     Entities[0]=[entityName=Artanis id=92 ... ]
_CHOICE_HEADER = re.compile(
    r"^id=(?P<id>\d+) Player=(?P<player>.+?) TaskList=(?P<tasklist>\d+) "
    r"ChoiceType=(?P<ctype>\S+) CountMin=(?P<cmin>\d+) CountMax=(?P<cmax>\d+)"
)
_CHOSEN_HEADER = re.compile(r"^id=(?P<id>\d+) Player=(?P<player>.+?) EntitiesCount=(?P<count>\d+)")
_ENTITIES_IDX = re.compile(r"^Entities\[\d+\]=(?P<ref>.+)$")

# The bracketed reference form, e.g.
#   [entityName=Flittering Bat id=377 zone=PLAY zonePos=3 cardId=BG36_200 player=12]
_BRACKET = re.compile(
    r"\[entityName=(?P<name>.*?) id=(?P<id>\d+) zone=(?P<zone>\S*) zonePos=(?P<pos>\d+) "
    r"cardId=(?P<card>\S*) player=(?P<player>\d+)\]"
)


@dataclass
class EntityRef:
    """A reference to an entity. Only one of id/name is guaranteed present."""
    raw: str
    id: int | None = None
    name: str | None = None
    card_id: str | None = None
    zone: str | None = None
    zone_pos: int | None = None
    player: int | None = None

    @property
    def is_game(self) -> bool:
        return self.raw == "GameEntity"


def parse_ref(raw: str) -> EntityRef:
    raw = raw.strip()
    m = _BRACKET.search(raw)
    if m:
        return EntityRef(
            raw=raw,
            id=int(m["id"]),
            name=m["name"] or None,
            card_id=m["card"] or None,
            zone=m["zone"] or None,
            zone_pos=int(m["pos"]),
            player=int(m["player"]),
        )
    if raw.isdigit():
        return EntityRef(raw=raw, id=int(raw))
    # Battletag or display name, e.g. "vladislove#21576", "Bartender Bob".
    return EntityRef(raw=raw, name=raw)


@dataclass
class CreateGame:
    pass


@dataclass
class GameEntityDecl:
    entity_id: int
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class PlayerDecl:
    entity_id: int
    player_id: int
    account_hi: int
    account_lo: int
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_bot_slot(self) -> bool:
        """Bartender Bob has a null game account; real players do not."""
        return self.account_hi == 0 and self.account_lo == 0


@dataclass
class FullEntity:
    id: int
    card_id: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class ShowEntity:
    ref: EntityRef
    card_id: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class HideEntity:
    ref: EntityRef
    tag: str
    value: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class TagChange:
    ref: EntityRef
    tag: str
    value: str


@dataclass
class BlockStart:
    block_type: str
    ref: EntityRef
    effect_card_id: str


@dataclass
class BlockEnd:
    pass


@dataclass
class ChoiceOffered:
    """A Discover / hero-select prompt. ChoiceType is MULLIGAN for hero picks."""
    choice_id: int
    player: str
    choice_type: str
    count_min: int
    count_max: int
    entities: list[EntityRef] = field(default_factory=list)


@dataclass
class ChoiceMade:
    choice_id: int
    player: str
    entities: list[EntityRef] = field(default_factory=list)


# Events that own the tag= lines indented beneath them.
_TAG_OWNERS = (GameEntityDecl, PlayerDecl, FullEntity, ShowEntity, HideEntity)


def parse_lines(lines):
    """Yield events from an iterable of raw Power.log lines."""
    pending = None      # event awaiting its indented tag= lines
    pending_indent = -1

    def flush():
        nonlocal pending, pending_indent
        if pending is not None:
            out, pending, pending_indent = pending, None, -1
            return out
        return None

    for raw in lines:
        if "PowerTaskList.DebugPrintPower" in raw and "Entity=GameEntity tag=TURN " in raw:
            tm_vis = _TASKLIST_TURN.match(raw.rstrip("\n"))
            if tm_vis:
                done = flush()
                if done is not None:
                    yield done
                yield TagChange(EntityRef(raw="GameEntity"), "VISUAL_TURN", tm_vis["value"])
            continue
        if "GameState.DebugPrint" not in raw:
            continue
        m = _LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        chan = m["chan"]
        body = m["body"]
        indent = len(body) - len(body.lstrip(" "))
        content = body.strip()
        if not content:
            continue

        if chan != "DebugPrintPower":
            em = _ENTITIES_IDX.match(content)
            if em and isinstance(pending, (ChoiceOffered, ChoiceMade)):
                pending.entities.append(parse_ref(em["ref"]))
                continue
            if content.startswith("Source="):
                continue
            done = flush()
            if done is not None:
                yield done
            if chan == "DebugPrintEntityChoices" and (h := _CHOICE_HEADER.match(content)):
                pending, pending_indent = ChoiceOffered(
                    int(h["id"]), h["player"], h["ctype"], int(h["cmin"]), int(h["cmax"])
                ), indent
            elif chan == "DebugPrintEntitiesChosen" and (h := _CHOSEN_HEADER.match(content)):
                pending, pending_indent = ChoiceMade(int(h["id"]), h["player"]), indent
            continue

        # A tag line belongs to the open declaration above it.
        tm = _TAG.match(content)
        if tm and pending is not None and indent > pending_indent:
            pending.tags[tm["tag"]] = tm["value"]
            continue

        done = flush()
        if done is not None:
            yield done

        if content == "CREATE_GAME":
            yield CreateGame()
        elif content == "BLOCK_END":
            yield BlockEnd()
        elif (h := _GAME_ENTITY.match(content)):
            pending, pending_indent = GameEntityDecl(int(h["eid"])), indent
        elif (h := _PLAYER.match(content)):
            pending, pending_indent = PlayerDecl(
                int(h["eid"]), int(h["pid"]), int(h["hi"]), int(h["lo"])
            ), indent
        elif (h := _FULL_ENTITY.match(content)):
            pending, pending_indent = FullEntity(int(h["id"]), h["card"]), indent
        elif (h := _SHOW_ENTITY.match(content)):
            ref = parse_ref(h["ref"].removeprefix("Entity=").strip())
            pending, pending_indent = ShowEntity(ref, h["card"]), indent
        elif (h := _HIDE_ENTITY.match(content)):
            ref = parse_ref(h["ref"].removeprefix("Entity=").strip())
            pending, pending_indent = HideEntity(ref, h["tag"], h["value"]), indent
        elif (h := _TAG_CHANGE.match(content)):
            yield TagChange(parse_ref(h["ref"]), h["tag"], h["value"])
        elif (h := _BLOCK_START.match(content)):
            yield BlockStart(h["type"], parse_ref(h["ref"]), h["effect"])
        # Everything else (META_DATA, SUB_SPELL_*, Info[n]) is ignored for now.

    done = flush()
    if done is not None:
        yield done


def parse_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        yield from parse_lines(fh)
