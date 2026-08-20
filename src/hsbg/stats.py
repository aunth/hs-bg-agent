"""Battlegrounds meta statistics.

Data is published by Firestone as static, unauthenticated files on their CDN:

    .../api/bgs/hero-stats/<mmr>/<period>/overview-from-hourly.gz.json
    .../api/bgs/card-stats/<mmr>/<period>/overview-from-hourly.gz.json

Their terms grant personal, non-commercial use only, and restrict copying
portions of their service -- so this fetches at most once a week, caches
locally, and identifies itself. Do not redistribute the cached files, and if
this ever becomes more than a personal tool, ask them first.

The interesting metric is not raw average placement but *impact*: the gap
between the average placement of games where a card was played and of those
where it was not. A card can have a good raw average simply by appearing in
games that were already going well.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BASE = "https://static.zerotoheroes.com/api/bgs"
COMPS_URL = ("https://static.zerotoheroes.com/hearthstone/data/"
             "battlegrounds-strategies/bgs-comps-strategies.gz.json")
UA = "hs-bg-agent/0.1 (personal Battlegrounds advisor)"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "stats"
MAX_AGE = 7 * 24 * 3600  # a week; the meta does not move faster than this


@dataclass
class HeroStat:
    card_id: str
    games: int
    avg_position: float
    conservative: float
    pick_rate: float          # of the times offered, how often taken
    tribe_stats: list = field(default_factory=list)


@dataclass
class Comp:
    """A meta composition: which cards it wants, and how central each one is."""
    comp_id: str
    name: str
    power_level: str          # S / A / B
    difficulty: str
    cards: dict[str, str] = field(default_factory=dict)   # cardId -> CORE/ADDON/CYCLE
    avg_placement: float = 0.0

    @property
    def quality(self) -> float:
        return {"S": 1.0, "A": 0.85, "B": 0.7, "C": 0.55}.get(self.power_level, 0.7)


@dataclass
class CardStat:
    card_id: str
    games: int
    avg_placement: float
    avg_placement_other: float
    # turn -> impact at that turn (baseline for that turn minus this card's)
    by_turn: dict[int, float] = field(default_factory=dict)

    @property
    def impact(self) -> float:
        """Placement gain from playing this card. Positive is better.

        Lower placement is better in Battlegrounds, so this is baseline minus
        the card's own average.
        """
        return self.avg_placement_other - self.avg_placement


def _fetch(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)


class BGStats:
    def __init__(self, mmr: str = "mmr-100", period: str = "last-patch",
                 cache_dir: Path | None = None):
        self.mmr, self.period = mmr, period
        self.dir = cache_dir or CACHE_DIR
        self.heroes: dict[str, HeroStat] = {}
        self.cards: dict[str, CardStat] = {}
        self.comps: list[Comp] = []
        self.archetypes: dict[str, float] = {}
        self.updated: str = ""
        self.load()

    def _path(self, kind: str) -> Path:
        return self.dir / f"{kind}_{self.mmr}_{self.period}.json"

    def stale(self, kind: str) -> bool:
        p = self._path(kind)
        return not p.exists() or (time.time() - p.stat().st_mtime) > MAX_AGE

    def refresh(self, force: bool = False) -> list[str]:
        """Download whatever is stale. Returns the kinds actually fetched."""
        done = []
        for kind in ("hero-stats", "card-stats", "comp-stats"):
            if force or self.stale(kind):
                url = f"{BASE}/{kind}/{self.mmr}/{self.period}/overview-from-hourly.gz.json"
                _fetch(url, self._path(kind))
                done.append(kind)
        if force or self.stale("comps"):
            _fetch(COMPS_URL, self._path("comps"))
            done.append("comps")
        if done:
            self.load()
        return done

    def load(self) -> None:
        hp = self._path("hero-stats")
        if hp.exists():
            d = json.loads(hp.read_text(encoding="utf-8"))
            self.updated = d.get("lastUpdateDate", "")
            for h in d.get("heroStats", []):
                offered = h.get("totalOffered") or 0
                self.heroes[h["heroCardId"]] = HeroStat(
                    card_id=h["heroCardId"],
                    games=h.get("dataPoints", 0),
                    avg_position=h.get("averagePosition", 0.0),
                    conservative=h.get("conservativePositionEstimate", 0.0),
                    pick_rate=(h.get("totalPicked", 0) / offered * 100) if offered else 0.0,
                    tribe_stats=h.get("tribeStats") or [],
                )
        cp = self._path("card-stats")
        if cp.exists():
            d = json.loads(cp.read_text(encoding="utf-8"))
            for c in d.get("cardStats", []):
                self.cards[c["cardId"]] = CardStat(
                    card_id=c["cardId"],
                    games=c.get("totalPlayed", 0),
                    avg_placement=c.get("averagePlacement") or 0.0,
                    avg_placement_other=c.get("averagePlacementOther") or 0.0,
                    # Each turn carries its own baseline, so compare within the
                    # turn rather than against the card's all-turns average.
                    by_turn={
                        t["turn"]: ((t.get("averagePlacementOther") or 0.0)
                                    - (t.get("averagePlacement") or 0.0))
                        for t in (c.get("turnStats") or [])
                        if (t.get("totalPlayed") or 0) >= 50
                        and t.get("averagePlacementOther") and t.get("averagePlacement")
                    },
                )
        self._load_comps()

    def _load_comps(self) -> None:
        cs = self._path("comp-stats")
        if cs.exists():
            d = json.loads(cs.read_text(encoding="utf-8"))
            for a in d.get("compStats", []):
                self.archetypes[a["archetype"]] = a.get("averagePlacement", 0.0)
        cp = self._path("comps")
        if not cp.exists():
            return
        d = json.loads(cp.read_text(encoding="utf-8"))
        raw = d.get("comps") if isinstance(d, dict) else d
        for c in raw or []:
            if not c.get("name"):
                continue                      # the file carries blank placeholder rows
            comp = Comp(
                comp_id=c.get("compId", ""),
                name=c["name"],
                power_level=c.get("powerLevel", ""),
                difficulty=c.get("difficulty", ""),
                cards={x["cardId"]: x.get("status", "ADDON") for x in (c.get("cards") or [])},
            )
            # The two feeds use different taxonomies, so this only sometimes hits.
            comp.avg_placement = self.archetypes.get(comp.comp_id, 0.0)
            self.comps.append(comp)

    @property
    def loaded(self) -> bool:
        return bool(self.heroes or self.cards)

    def hero(self, card_id: str) -> HeroStat | None:
        return self.heroes.get(card_id)

    def card(self, card_id: str) -> CardStat | None:
        return self.cards.get(card_id)
