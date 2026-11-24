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
        if turn <= 0:
            return
        if self.turns and self.turns[-1][0] == turn:
            _, _, prev_place = self.turns[-1]
            self.turns[-1] = (turn, damage_taken, place or prev_place)
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
    matched_core: list[str] = field(default_factory=list)
    matched_support: list[str] = field(default_factory=list)
    tribe_minions: list[str] = field(default_factory=list)
    missing_core: list[str] = field(default_factory=list)
    comp_tribes: tuple[str, ...] = ()

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
    def comp_fits(
        self,
        owned: list[str],
        board_tribes: dict[str, int],
        board_size: int,
        board_card_ids: list[str] | None = None,
    ) -> list[CompFit]:
        """How plausible each meta composition is, given what you hold."""
        owned_set = set(owned)
        b_cids = board_card_ids if board_card_ids is not None else owned
        fits = []
        for comp in self.stats.comps:
            hits = 0
            matched_core: list[str] = []
            matched_support: list[str] = []
            missing_core: list[str] = []
            for cid, role in comp.cards.items():
                card = self.cards.by_id(cid)
                cname = (card.name if card else "") or cid
                ctier = f" (T{card.tier})" if (card and card.tier) else ""
                if cid in owned_set:
                    hits += 1
                    if role == "CORE":
                        matched_core.append(cname)
                    else:
                        matched_support.append(cname)
                elif role == "CORE":
                    missing_core.append(f"{cname}{ctier}")

            tribes = self.comp_tribes(comp)
            tribe_hits = sum(n for r, n in board_tribes.items() if r in tribes)
            tribe_minions: list[str] = []
            if tribes:
                seen_names: set[str] = set()
                for bcid in b_cids:
                    bcard = self.cards.by_id(bcid)
                    if not bcard:
                        continue
                    if any(r in tribes or r == "ALL" for r in bcard.races):
                        if bcard.name and bcard.name not in seen_names:
                            seen_names.add(bcard.name)
                            tribe_minions.append(bcard.name)

            fits.append(
                CompFit(
                    comp=comp,
                    owned=hits,
                    total=len(comp.cards),
                    tribe_hits=tribe_hits,
                    board_size=board_size,
                    availability=self.tracker.status(tribes),
                    matched_core=matched_core,
                    matched_support=matched_support,
                    tribe_minions=tribe_minions,
                    missing_core=missing_core,
                    comp_tribes=tuple(sorted(tribes)),
                )
            )
        # Builds whose tribe is not in this lobby sort last, whatever their power.
        rank = {"active": 0, "unknown": 1, "absent": 2}
        return sorted(fits, key=lambda f: (rank[f.availability], -f.affinity,
                                           -f.comp.quality))

    # -- scoring --------------------------------------------------------
    def _board_context(self, bg) -> tuple[list[str], dict[str, int], dict[str, int], list[CompFit]]:
        board_cids = [m.card_id for m in bg.board]
        owned = board_cids + [m.card_id for m in bg.hand]
        owned_counts: dict[str, int] = {}
        for m in list(bg.board) + list(bg.hand):
            if not m.golden and m.card_id:
                owned_counts[m.card_id] = owned_counts.get(m.card_id, 0) + 1
        tribes: dict[str, int] = {}
        for m in bg.board:
            card = self.cards.by_id(m.card_id)
            for r in (card.races if card else ()):
                tribes[r] = tribes.get(r, 0) + 1
        fits = self.comp_fits(owned, tribes, len(bg.board), board_card_ids=board_cids)
        return owned, owned_counts, tribes, fits

    def observe(self, bg) -> None:
        """Update stateful lobby trackers (tribes, mode, combat history) from bg."""
        self.tracker.observe(self.cards, list(bg.shop) + list(bg.board), bg.turn)
        self.combat_history.observe(bg.turn, bg.damage_last_turn, bg.leaderboard_place)

    def score_card(
        self,
        card_id: str,
        board_tribes: dict[str, int],
        fits: list[CompFit],
        turn: int,
        owned_counts: dict[str, int] | None = None,
    ) -> Suggestion:
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

        if owned_counts:
            copies = owned_counts.get(card_id, 0)
            if copies >= 2:
                score += 14.0
                reasons.insert(0, "completes triple!")
            elif copies == 1:
                score += 5.0
                reasons.append("makes a pair")

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

    def evaluate_shop(self, bg, observe: bool = True) -> tuple[list[Suggestion], list[CompFit]]:
        if observe:
            self.observe(bg)
        _, owned_counts, tribes, fits = self._board_context(bg)
        sugg = [self.score_card(m.card_id, tribes, fits, bg.turn, owned_counts) for m in bg.shop]
        return sugg, fits

    def score_choices(self, bg, card_ids: list[str]) -> list[Suggestion]:
        """Score non-hero Discover options (minions, spells, trinkets, buddies)."""
        _, owned_counts, tribes, fits = self._board_context(bg)
        return [self.score_card(cid, tribes, fits, bg.turn, owned_counts) for cid in card_ids]

    def evaluate_spells(self, bg) -> list[Suggestion]:
        """Score Tavern Spells currently offered in the shop."""
        _, owned_counts, tribes, fits = self._board_context(bg)
        return [
            self.score_card(sp.card_id, tribes, fits, bg.turn, owned_counts)
            for sp in bg.shop_spells
        ]

    def score_tavern_actions(
        self,
        bg,
        best_card_score: float,
        best_affordable_spell_score: float = 0.0,
        plan=None,
    ) -> dict[str, dict]:
        """Score Upgrade Tavern and Reroll on the same 0..100 scale as shop cards."""
        from .tavern import BuyMinion, BuySpell, RollShop, UpgradeTavern

        out: dict[str, dict] = {}
        plan_actions = list(plan.actions) if (plan is not None and getattr(plan, "actions", None)) else []
        has_upgrade_in_plan = any(isinstance(a, UpgradeTavern) for a in plan_actions)
        first_major = next(
            (a for a in plan_actions if isinstance(a, (UpgradeTavern, BuyMinion, BuySpell, RollShop))),
            None,
        )

        # 1. Tavern Upgrade score (0..100)
        if bg.tier < 6:
            up_cost = bg.upgrade_cost if bg.upgrade_cost is not None else max(0, 5 + bg.tier)
            affordable_up = bg.gold >= up_cost
            up_score = 50.0
            if bg.tier == 1:
                if bg.turn == 2 and up_cost <= 4:
                    up_score += 35.0
                elif bg.turn >= 3 and up_cost <= 3:
                    up_score += 36.0
                elif up_cost <= 4:
                    up_score += 22.0
                else:
                    up_score -= 10.0
            elif bg.tier == 2:
                if up_cost <= 4:
                    up_score += 28.0
                elif up_cost == 5:
                    up_score += 20.0
                elif up_cost == 6 and bg.turn >= 4:
                    up_score += 18.0
                else:
                    up_score -= 8.0
            elif bg.tier == 3:
                if up_cost <= 5:
                    up_score += 26.0
                elif up_cost == 6:
                    up_score += 18.0
                elif up_cost == 7:
                    up_score += 10.0
                else:
                    up_score -= 10.0
            elif bg.tier == 4:
                if up_cost <= 6:
                    up_score += 25.0
                elif up_cost == 7:
                    up_score += 17.0
                elif up_cost == 8:
                    up_score += 9.0
                else:
                    up_score -= 12.0
            elif bg.tier == 5:
                if up_cost <= 6:
                    up_score += 24.0
                elif up_cost == 7:
                    up_score += 15.0
                elif up_cost == 8:
                    up_score += 7.0
                else:
                    up_score -= 14.0

            if best_card_score < 56.0:
                up_score += 6.0
            elif best_card_score >= 72.0 and bg.gold < up_cost + 3:
                up_score -= 8.0

            eff_hp = (getattr(bg, "hero_health", 0) or 30) + (getattr(bg, "hero_armor", 0) or 0)
            if eff_hp <= 15 and bg.tier >= 4:
                up_score -= 12.0

            if not affordable_up and not has_upgrade_in_plan:
                if bg.gold + len(bg.board) < up_cost:
                    up_score = min(up_score, 26.0)
                else:
                    up_score = min(up_score - 16.0, 46.0)

            if isinstance(first_major, UpgradeTavern):
                up_score = max(up_score, best_card_score + 5.0, 78.0)
            elif has_upgrade_in_plan:
                up_score = max(up_score, 72.0)
            elif plan is not None and affordable_up and not has_upgrade_in_plan and best_card_score > 0:
                up_score = min(up_score, max(48.0, best_card_score - 3.0))

            out["upgrade"] = {
                "score": max(0.0, min(100.0, up_score)),
                "affordable": affordable_up or has_upgrade_in_plan,
                "cost": up_cost,
            }

        # 2. Reroll score (0..100)
        roll_cost = bg.reroll_cost if bg.reroll_cost is not None else 1
        affordable_roll = bg.gold >= roll_cost
        if not affordable_roll:
            roll_score = 18.0
        elif roll_cost == 0:
            roll_score = 80.0 if (best_card_score < 62.0 or bg.gold < 3) else 65.0
        else:
            up_cost_now = bg.upgrade_cost if (bg.tier < 6 and bg.upgrade_cost is not None) else 99
            can_buy_minion_after = bg.gold >= roll_cost + (bg.buy_cost or 3)
            if bg.turn <= 2:
                if bg.gold == 1 and best_affordable_spell_score < 55.0 and best_card_score < 66.0:
                    roll_score = 60.0
                else:
                    roll_score = 22.0
            elif can_buy_minion_after:
                if best_card_score < 54.0:
                    roll_score = 68.0 + min(8.0, (54.0 - best_card_score) * 0.8)
                elif best_card_score < 60.0:
                    roll_score = 58.0
                elif best_card_score < 70.0:
                    roll_score = 45.0
                else:
                    roll_score = 33.0
            elif bg.gold < (bg.buy_cost or 3) and bg.gold < up_cost_now:
                if best_affordable_spell_score >= 58.0:
                    roll_score = 38.0
                elif best_card_score >= 66.0:
                    roll_score = 30.0
                else:
                    roll_score = 64.0
            else:
                roll_score = 38.0

        if isinstance(first_major, RollShop):
            roll_score = max(roll_score, best_card_score + 4.0, 68.0)
        elif isinstance(first_major, (BuyMinion, BuySpell, UpgradeTavern)):
            top_target = max(best_card_score, out.get("upgrade", {}).get("score", 0.0))
            roll_score = min(roll_score, max(45.0, top_target - 4.0))

        out["reroll"] = {
            "score": max(0.0, min(100.0, roll_score)),
            "affordable": affordable_roll,
            "cost": roll_cost,
        }
        return out

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
