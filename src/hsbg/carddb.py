"""Card metadata from HearthstoneJSON.

The log identifies cards only by ``cardId`` (and occasionally by ``dbfId``, e.g.
``BACON_TRIPLE_UPGRADE_MINION_ID``), so everything human-readable -- names,
tribes, card text -- has to come from here.

Fetch the data with ``tools/fetch_carddb.sh``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cards.json"


@dataclass(frozen=True)
class Card:
    id: str
    dbf_id: int
    name: str
    type: str = ""
    attack: int = 0
    health: int = 0
    tier: int = 0
    text: str = ""
    races: tuple[str, ...] = ()
    mechanics: tuple[str, ...] = ()
    is_pool_minion: bool = False
    is_buddy: bool = False

    @property
    def tribe(self) -> str:
        if not self.races:
            return ""
        if "ALL" in self.races:
            return "All"
        return "/".join(r.title() for r in self.races)


class CardDB:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._by_id: dict[str, Card] = {}
        self._by_dbf: dict[int, Card] = {}
        if self.path.exists():
            self._load()

    @property
    def loaded(self) -> bool:
        return bool(self._by_id)

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for c in raw:
            cid = c.get("id")
            if not cid:
                continue
            races = c.get("races") or ([c["race"]] if c.get("race") else [])
            card = Card(
                id=cid,
                dbf_id=c.get("dbfId", 0),
                name=c.get("name", ""),
                type=c.get("type", ""),
                attack=c.get("attack", 0) or 0,
                health=c.get("health", 0) or 0,
                tier=c.get("techLevel", 0) or 0,
                text=c.get("text", "") or "",
                races=tuple(races),
                mechanics=tuple(c.get("mechanics") or ()),
                is_pool_minion=bool(c.get("isBattlegroundsPoolMinion")),
                is_buddy=bool(c.get("isBattlegroundsBuddy")),
            )
            self._by_id[cid] = card
            if card.dbf_id:
                self._by_dbf[card.dbf_id] = card

    def by_id(self, card_id: str) -> Card | None:
        return self._by_id.get(card_id)

    def by_dbf(self, dbf_id: int) -> Card | None:
        return self._by_dbf.get(dbf_id)

    def name(self, card_id: str, default: str = "") -> str:
        c = self._by_id.get(card_id)
        return c.name if c else (default or card_id)

    @property
    def pool(self) -> list[Card]:
        """Every minion that can appear in a Battlegrounds shop."""
        return [c for c in self._by_id.values() if c.is_pool_minion]
