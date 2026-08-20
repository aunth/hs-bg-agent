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


@dataclass
class CompFit:
    comp: Comp
    owned: int              # exact card matches you already hold
    total: int
    tribe_hits: int = 0     # board minions whose tribe this comp is built on
    board_size: int = 0

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
        return ", ".join(bits) or "no pieces yet"


@dataclass
class Suggestion:
    card_id: str
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)


class Advisor:
    def __init__(self, cards: CardDB, stats: BGStats):
        self.cards = cards
        self.stats = stats
        self._tribes: dict[str, set[str]] = {}

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
            fits.append(CompFit(comp, hits, len(comp.cards), tribe_hits, board_size))
        return sorted(fits, key=lambda f: (-f.affinity, -f.comp.quality))

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
            if not role:
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
        owned = [m.card_id for m in bg.board] + [m.card_id for m in bg.hand]
        tribes: dict[str, int] = {}
        for m in bg.board:
            card = self.cards.by_id(m.card_id)
            for r in (card.races if card else ()):
                tribes[r] = tribes.get(r, 0) + 1
        fits = self.comp_fits(owned, tribes, len(bg.board))
        sugg = [self.score_card(m.card_id, tribes, fits, bg.turn) for m in bg.shop]
        return sugg, fits
