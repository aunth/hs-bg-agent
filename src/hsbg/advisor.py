"""Contextual scoring for shop minions.

A global average placement says how a card performs across every game ever
played. It cannot say whether the card is good *for you, now*. This combines
four signals into one number:

* **meta**   -- the card's impact (placement with it, minus placement without)
* **build**  -- does it advance a composition you are already partway into
* **tribe**  -- does it share a tribe with what you already have
* **timing** -- is it good specifically at the turn you are on

The output is a 0-100 score plus the reasons behind it, because a bare number
you cannot interrogate is not advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .carddb import CardDB
from .stats import BGStats, Comp

ROLE_WEIGHT = {"CORE": 1.0, "ADDON": 0.7, "CYCLE": 0.4}

# Battlegrounds runs a subset of tribes per lobby, and how many is not fixed --
# it has changed across patches and is not worth hardcoding. So instead of
# waiting to prove a specific count, we watch for the slate to go quiet: once
# several turns pass with no newly-proven tribe, whatever hasn't shown up is
# treated as absent. Recommending a build around a tribe that is not in the
# game is worse than no advice at all.
MIN_TURN = 5           # too little shop evidence before this to conclude anything
STAGNATION_TURNS = 4    # turns of no new tribe evidence before we call the slate settled


class TribeTracker:
    """Works out which tribes are in this lobby by watching the shop.

    Only a *single*-tribe minion proves its tribe is active: Ominous Seer is
    Demon/Naga, so seeing it means Demon or Naga, not both. Dual-tribe minions
    and Amalgams are therefore ignored as evidence.
    """

    def __init__(self) -> None:
        self.proven: set[str] = set()
        self.ambiguous: set[str] = set()
        # Which minion proved each tribe, so the conclusion can be audited.
        self.witness: dict[str, str] = {}
        # Duos runs its own card pool and meta; our stats are solo Battlegrounds.
        self.duos = False
        self.turn = 0
        self.last_new_turn = 0

    def reset(self) -> None:
        self.proven.clear()
        self.ambiguous.clear()
        self.witness.clear()
        self.duos = False
        self.turn = 0
        self.last_new_turn = 0

    def observe(self, cards: CardDB, minions, turn: int = 0) -> None:
        self.turn = turn
        for m in minions:
            if m.card_id.startswith("BGDUO"):
                # Duos-exclusive card: positive proof of the mode.
                self.duos = True
            card = cards.by_id(m.card_id)
            races = [r for r in (card.races if card else ()) if r != "ALL"]
            if len(races) == 1:
                if races[0] not in self.proven:
                    self.last_new_turn = turn
                self.proven.add(races[0])
                self.witness.setdefault(races[0], card.name if card else m.card_id)
            elif races:
                self.ambiguous.update(races)

    @property
    def complete(self) -> bool:
        """Once the slate has gone quiet for a while, anything unseen is absent."""
        if self.turn < MIN_TURN:
            return False
        return (self.turn - self.last_new_turn) >= STAGNATION_TURNS

    def status(self, tribes: set[str]) -> str:
        """'active', 'absent', or 'unknown' for a set of tribes."""
        if not tribes:
            return "active"                      # neutral builds always work
        if tribes & self.proven:
            return "active"
        if self.complete:
            return "absent"
        return "unknown"


class CombatHistory:
    """Per-turn damage taken and leaderboard place, straight from the log.

    Deliberately nothing more clever than that: DAMAGE_DEALT_TO_HERO_LAST_TURN
    and PLAYER_LEADERBOARD_PLACE are exact, already-computed facts (the second
    is Blizzard's own live rank, accounting for all eight real players' true
    health) -- there is no need to infer or simulate anything here.
    """

    def __init__(self) -> None:
        self.turns: list[tuple[int, int, int]] = []  # (turn, damage_taken, place)

    def reset(self) -> None:
        self.turns.clear()

    def observe(self, turn: int, damage_taken: int, place: int) -> None:
        if turn <= 0 or (self.turns and self.turns[-1][0] == turn):
            return
        self.turns.append((turn, damage_taken, place))

    def recent_damage(self, n: int = 3) -> list[int]:
        return [d for _, d, _ in self.turns[-n:]]


@dataclass
class CompFit:
    comp: Comp
    owned: int              # exact card matches you already hold
    total: int
    tribe_hits: int = 0     # board minions whose tribe this comp is built on
    board_size: int = 0
    availability: str = "unknown"   # active / absent / unknown

    @property
    def available(self) -> bool:
        return self.availability != "absent"

    @property
    def progress(self) -> float:
        return self.owned / self.total if self.total else 0.0

    @property
    def affinity(self) -> float:
        """How plausible this build is for you, in 0..1.

        Exact card matches are the strongest signal, but early on you hold none
        of a comp's specific pieces while already committed to its tribe -- so
        tribe overlap carries the rest.
        """
        tribe = (self.tribe_hits / self.board_size) if self.board_size else 0.0
        return min(1.0, 0.65 * self.progress + 0.35 * tribe)

    def describe(self) -> str:
        bits = []
        if self.owned:
            bits.append(f"{self.owned}/{self.total} cards")
        if self.tribe_hits:
            bits.append(f"{self.tribe_hits} on tribe")
        if self.availability == "absent":
            bits.append("tribe not in lobby")
        elif self.availability == "unknown":
            bits.append("tribe unconfirmed")
        return ", ".join(bits) or "no pieces yet"


@dataclass
class Suggestion:
    card_id: str
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class PivotAdvice:
    """Whether switching to a different build is worth considering right now.

    Every field is either observed directly from your own game (board,
    leaderboard place, damage taken) or Firestone's real per-hero, per-turn
    population average -- never a guess about the other seven boards. The
    numbers are always shown alongside the verdict, not hidden behind it.
    """
    comp: Comp
    board_ratio: float | None    # your board's stats / population average this turn
    leaderboard_place: int       # 1-8 from Blizzard's own live rank, 0 if unknown
    recent_damage: list[int]     # damage taken in the last few turns, oldest first
    missing_core: list[str]      # this comp's CORE cards you don't own yet
    tier_gap: int                # tavern tiers above your current one they need
    verdict: str

    def describe(self) -> str:
        bits = []
        if self.board_ratio is not None:
            bits.append(f"board {self.board_ratio * 100:.0f}% of average")
        if self.leaderboard_place:
            bits.append(f"place {self.leaderboard_place}")
        if self.recent_damage:
            bits.append(f"took {sum(self.recent_damage)} dmg last "
                        f"{len(self.recent_damage)}t")
        bits.append(f"{len(self.missing_core)} core piece(s) missing")
        if self.tier_gap > 0:
            bits.append(f"needs tier +{self.tier_gap}")
        return ", ".join(bits)


class Advisor:
    def __init__(self, cards: CardDB, stats: BGStats):
        self.cards = cards
        self.stats = stats
        self._tribes: dict[str, set[str]] = {}
        self.tracker = TribeTracker()
        self.combat_history = CombatHistory()

    def comp_tribes(self, comp: Comp) -> set[str]:
        """The tribes a comp is actually built around, inferred from its cards."""
        if comp.comp_id not in self._tribes:
            counts: dict[str, int] = {}
            for cid in comp.cards:
                card = self.cards.by_id(cid)
                for r in (card.races if card else ()):
                    if r != "ALL":
                        counts[r] = counts.get(r, 0) + 1
            top = max(counts.values(), default=0)
            # Keep tribes that are clearly central, not incidental one-offs.
            self._tribes[comp.comp_id] = {r for r, n in counts.items()
                                          if top and n >= max(2, top * 0.5)}
        return self._tribes[comp.comp_id]

    # -- build detection ------------------------------------------------
    def comp_fits(self, owned: list[str], board_tribes: dict[str, int],
                  board_size: int) -> list[CompFit]:
        """How plausible each meta composition is, given what you hold."""
        owned_set = set(owned)
        fits = []
        for comp in self.stats.comps:
            hits = sum(1 for cid in comp.cards if cid in owned_set)
            tribes = self.comp_tribes(comp)
            tribe_hits = sum(n for r, n in board_tribes.items() if r in tribes)
            fits.append(CompFit(comp, hits, len(comp.cards), tribe_hits, board_size,
                                self.tracker.status(tribes)))
        # Builds whose tribe is not in this lobby sort last, whatever their power.
        rank = {"active": 0, "unknown": 1, "absent": 2}
        return sorted(fits, key=lambda f: (rank[f.availability], -f.affinity,
                                           -f.comp.quality))

    # -- scoring --------------------------------------------------------
    def score_card(self, card_id: str, board_tribes: dict[str, int],
                   fits: list[CompFit], turn: int) -> Suggestion:
        card = self.cards.by_id(card_id)
        name = card.name if card else card_id
        score, reasons = 50.0, []

        stat = self.stats.card(card_id)
        if stat:
            # impact runs roughly -1.0 .. +1.5 placement positions
            score += max(-1.0, min(1.5, stat.impact)) * 16.0
            reasons.append(f"meta {stat.impact:+.2f}")
            here = stat.by_turn.get(turn)
            if here is not None:
                # How much better this card is at this turn than it is generally.
                delta = here - stat.impact
                if abs(delta) > 0.10:
                    score += max(-6.0, min(6.0, delta * 8))
                    reasons.append(f"{'strong' if delta > 0 else 'weak'} at turn {turn}")
        else:
            reasons.append("no meta data")

        # Does it advance something you are already building?
        best = None
        for fit in fits:
            role = fit.comp.cards.get(card_id)
            if not role or not fit.available:
                continue
            weight = ROLE_WEIGHT.get(role, 0.5)
            # A comp you have pieces of is worth far more than one you don't.
            bonus = 24.0 * fit.comp.quality * weight * (0.25 + 0.75 * fit.affinity)
            if best is None or bonus > best[0]:
                best = (bonus, fit, role)
        if best:
            bonus, fit, role = best
            score += bonus
            reasons.append(f"{role.lower()} of {fit.comp.name} "
                           f"({fit.describe()}, {fit.comp.power_level})")

        if card and card.races:
            shared = max((board_tribes.get(r, 0) for r in card.races), default=0)
            if shared:
                score += min(10.0, shared * 2.5)
                reasons.append(f"{shared} on board share its tribe")

        return Suggestion(card_id, name, max(0.0, min(100.0, score)), reasons)

    def evaluate_shop(self, bg) -> tuple[list[Suggestion], list[CompFit]]:
        # Everything the shop offers is evidence about which tribes are in play.
        self.tracker.observe(self.cards, list(bg.shop) + list(bg.board), bg.turn)
        self.combat_history.observe(bg.turn, bg.damage_last_turn, bg.leaderboard_place)
        owned = [m.card_id for m in bg.board] + [m.card_id for m in bg.hand]
        tribes: dict[str, int] = {}
        for m in bg.board:
            card = self.cards.by_id(m.card_id)
            for r in (card.races if card else ()):
                tribes[r] = tribes.get(r, 0) + 1
        fits = self.comp_fits(owned, tribes, len(bg.board))
        sugg = [self.score_card(m.card_id, tribes, fits, bg.turn) for m in bg.shop]
        return sugg, fits

    # -- pivot analysis ---------------------------------------------------
    def board_power_ratio(self, bg) -> float | None:
        """Your board's total stats against the real population average for
        your hero at this turn. >1 is ahead of pace, <1 behind. None with no
        benchmark to compare against -- never a fabricated number."""
        hero = self.stats.hero(bg.hero_card_id)
        if not hero or not bg.turn:
            return None
        benchmark = hero.warband_stats.get(bg.turn)
        if not benchmark:
            return None
        mine = sum(m.atk + m.health for m in bg.board)
        return mine / benchmark

    def pivot_cost(self, comp: Comp, owned: set[str],
                    current_tier: int) -> tuple[list[str], int]:
        """This comp's missing CORE pieces, and how many tavern tiers above
        your current one the furthest of them needs."""
        missing = [cid for cid, role in comp.cards.items()
                   if role == "CORE" and cid not in owned]
        if not missing:
            return missing, 0
        tiers = [c.tier for c in (self.cards.by_id(cid) for cid in missing) if c]
        return missing, max(0, max(tiers, default=current_tier) - current_tier)

    def pivot_check(self, bg, fits: list[CompFit]) -> PivotAdvice | None:
        """Is switching build worth considering right now?

        Picks the strongest *actual pivot candidate* -- an available comp
        whose tribe your board doesn't already share -- and grounds the call
        in what's real: your board against the population average for your
        hero at this turn, Blizzard's own live leaderboard rank, your recent
        damage taken, and how many core pieces the switch actually needs.
        Never models the other seven boards.
        """
        board_tribes: set[str] = set()
        for m in bg.board:
            card = self.cards.by_id(m.card_id)
            board_tribes.update(r for r in (card.races if card else ()) if r != "ALL")
        if not board_tribes:
            return None    # nothing committed yet -- there is no "pivot" to weigh

        candidates = [f for f in fits if f.available
                      and not (self.comp_tribes(f.comp) & board_tribes)]
        if not candidates:
            return None
        fit = max(candidates, key=lambda f: (f.comp.quality, f.affinity))

        owned = {m.card_id for m in bg.board} | {m.card_id for m in bg.hand}
        missing_core, tier_gap = self.pivot_cost(fit.comp, owned, bg.tier)
        ratio = self.board_power_ratio(bg)
        recent = self.combat_history.recent_damage()

        if ratio is None:
            pace = "no benchmark yet for your hero"
        elif ratio >= 1.1:
            pace = "ahead of pace"
        elif ratio >= 0.9:
            pace = "on pace"
        else:
            pace = "behind pace"
        cost_bit = f"needs {len(missing_core)} core piece(s)"
        if tier_gap:
            cost_bit += f", up to tier {bg.tier + tier_gap}"
        verdict = f"{pace} -- {fit.comp.name} {cost_bit}"

        return PivotAdvice(fit.comp, ratio, bg.leaderboard_place, recent,
                            missing_core, tier_gap, verdict)
