"""State-level value function V(GameSnapshot) -> ValueBreakdown.

Evaluates a complete post-action tavern state rather than isolated shop cards,
balancing immediate board strength, combat simulation equity, target-comp
synergy, tavern tier curve, economy, and pool-aware roll option value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .advisor import Advisor, CompFit, ROLE_WEIGHT
from .combat import CMinion, simulate
from .tavern import BOARD_LIMIT, GameSnapshot, MinionPool, TMinion

# Expected turn at which reaching each tavern tier is considered "on curve".
CURVE_TURN = {2: 2, 3: 4, 4: 6, 5: 8, 6: 9}
TIER_BASE_VALUE = {2: 25.0, 3: 19.5, 4: 18.5, 5: 17.5, 6: 15.5}


@dataclass(frozen=True)
class ValueBreakdown:
    total: float
    board: float
    synergy: float
    economy: float
    roll_ev: float
    target_comp: str = ""
    combat_win_pct: float | None = None


def tminion_to_cminion(m: TMinion, cards=None) -> CMinion:
    """Project a tavern TMinion into a combat CMinion using CardDB mechanics."""
    card = cards.by_id(m.card_id) if cards else None
    mechs = set(card.mechanics if card else ())
    base_atk = (card.attack * (2 if m.golden else 1)) if card else m.atk
    base_hp = (card.health * (2 if m.golden else 1)) if card else m.health
    tribe = m.tribe or (card.tribe if card else "")
    text = (card.text or "").lower() if card else ""
    is_cleave = "also damages the minions next to" in text
    return CMinion(
        card_id=m.card_id,
        name=m.name,
        atk=m.atk,
        health=m.health,
        tier=max(1, m.tier),
        taunt="TAUNT" in mechs,
        divine_shield="DIVINE_SHIELD" in mechs,
        poisonous="POISONOUS" in mechs,
        venomous="VENOMOUS" in mechs,
        windfury="WINDFURY" in mechs,
        reborn="REBORN" in mechs,
        golden=m.golden,
        base_atk=base_atk,
        base_health=base_hp,
        tribe=tribe,
        cleave=is_cleave,
    )


def synthetic_enemy_board(turn: int, tier: int) -> list[CMinion]:
    """Build a turn-calibrated benchmark warband when the next opponent's board is unseen."""
    t = max(1, turn)
    count = min(7, max(1, t if t <= 4 else (t - 1 if t <= 6 else 7)))
    # Population average total stats (atk+hp) grows roughly as ~4.5 * t^1.42
    total_stats = max(4.0, 4.5 * (t ** 1.42))
    per_unit = max(2, int(round(total_stats / count)))
    atk = max(1, per_unit // 2)
    hp = max(1, per_unit - atk)
    unit_tier = max(1, min(6, tier))
    return [
        CMinion(
            card_id=f"BENCH_{i}",
            name=f"Benchmark {i + 1}",
            atk=atk + (i % 2),
            health=hp + ((i + 1) % 2),
            tier=unit_tier,
            taunt=(i == count - 1 and t >= 4),
        )
        for i in range(count)
    ]


class ValueFunction:
    """Evaluates GameSnapshot states using CardDB, BGStats, Advisor, and combat sim."""

    def __init__(self, advisor: Advisor) -> None:
        self.advisor = advisor
        self._card_quality_cache: dict[tuple[str, int], float] = {}
        self._card_races_cache: dict[str, tuple[str, ...]] = {}
        self._comp_meta: list[tuple] | None = None
        self.pool: MinionPool | None = None

    def prepare(self) -> None:
        """Snapshot comp availability and active minion pool once per search."""
        rank = {"active": 0, "unknown": 1, "absent": 2}
        meta = []
        for comp in self.advisor.stats.comps:
            tribes = self.advisor.comp_tribes(comp)
            avail = self.advisor.tracker.status(tribes)
            meta.append((comp, tribes, avail, rank[avail], len(comp.cards)))
        self._comp_meta = meta
        self.pool = MinionPool(self.advisor.cards, self.advisor.tracker)

    def _fast_comp_fits(
        self,
        owned_set: set[str],
        board_tribes: dict[str, int],
        board_size: int,
    ) -> list[CompFit]:
        if self._comp_meta is None:
            self.prepare()
        fits: list[CompFit] = []
        for comp, tribes, avail, _avail_rank, total in self._comp_meta or ():
            hits = sum(1 for cid in comp.cards if cid in owned_set)
            tribe_hits = sum(n for r, n in board_tribes.items() if r in tribes)
            fits.append(CompFit(comp, hits, total, tribe_hits, board_size, avail))
        rank = {"active": 0, "unknown": 1, "absent": 2}
        fits.sort(key=lambda f: (rank[f.availability], -f.affinity, -f.comp.quality))
        return fits

    def _races(self, card_id: str) -> tuple[str, ...]:
        races = self._card_races_cache.get(card_id)
        if races is None:
            card = self.advisor.cards.by_id(card_id)
            races = card.races if card else ()
            self._card_races_cache[card_id] = races
        return races

    def _card_quality(self, card_id: str, turn: int) -> float:
        """Intrinsic meta quality of a card at a given turn, roughly in -5..+8."""
        key = (card_id, turn)
        cached = self._card_quality_cache.get(key)
        if cached is not None:
            return cached
        stat = self.advisor.stats.card(card_id)
        q = 0.0
        if stat is not None:
            impact = max(-1.0, min(1.5, stat.impact))
            q += impact * 4.5
            here = stat.by_turn.get(turn)
            if here is not None:
                delta = max(-0.5, min(0.5, here - stat.impact))
                q += delta * 3.0
        self._card_quality_cache[key] = q
        return q

    def _minion_board_value(self, m: TMinion, turn: int) -> float:
        turn_scale = max(6.0, 2.0 * turn + 2.0)
        raw_stats = max(1, m.atk + m.health)
        stat_val = 7.5 * math.sqrt(raw_stats / turn_scale)
        golden_bonus = 4.0 if m.golden else 0.0
        tier_bonus = 0.9 * max(0, m.tier - 1)
        quality = self._card_quality(m.card_id, turn)
        slot_bonus = 3.0
        return slot_bonus + stat_val + golden_bonus + tier_bonus + quality

    def _tier_curve_value(self, tier: int, turn: int, hp_total: int) -> float:
        """Cumulative value of being at `tier` on `turn`, scaled by HP safety."""
        if tier <= 1:
            return 0.0
        hp_factor = 0.65 if hp_total < 15 else (0.85 if hp_total < 22 else 1.0)
        horizon = min(1.0, max(0.35, (13.0 - turn) / 5.0))
        total = 0.0
        for level in range(2, tier + 1):
            base = TIER_BASE_VALUE.get(level, 12.0)
            target_turn = CURVE_TURN.get(level, 9)
            curve_factor = 1.0 if turn >= target_turn else 0.72
            total += base * curve_factor * hp_factor * horizon
        return total

    def _roll_option_value(self, snap: GameSnapshot, fits: list[CompFit]) -> float:
        """Expected value of stopping deterministic search at a RollShop action."""
        if not snap.rolled:
            return 0.0
        rem = snap.gold
        if rem <= 0:
            return 1.4
        if rem < snap.buy_cost:
            return 2.2
        buys = rem // snap.buy_cost
        open_slots = max(0, BOARD_LIMIT - len(snap.board))
        per_buy_ev = (11.0 if open_slots > 0 else 7.2) + 0.8 * snap.tier
        if fits and fits[0].available and fits[0].affinity > 0.2:
            # Check how many needed comp cards are actually reachable at current tier.
            top_comp = fits[0].comp
            reachable = sum(
                1
                for cid in top_comp.cards
                if (c := self.advisor.cards.by_id(cid)) and c.tier <= snap.tier
            )
            if reachable >= 2:
                per_buy_ev += 1.2
        leftover_bonus = 0.6 if (rem % snap.buy_cost) >= snap.roll_cost else 0.0
        return buys * per_buy_ev + leftover_bonus

    def combat_bonus(
        self,
        snap: GameSnapshot,
        enemy_board: list[CMinion] | None = None,
        runs: int = 24,
    ) -> tuple[float, float]:
        """Return (value_bonus, win_pct) from Monte Carlo combat vs opponent/benchmark."""
        if not snap.board:
            return -6.0, 0.0
        my_cminions = [tminion_to_cminion(m, self.advisor.cards) for m in snap.board]
        opp = enemy_board or synthetic_enemy_board(max(1, snap.turn), max(1, snap.tier))
        tier = max(1, snap.tier)
        sim = simulate(my_cminions, opp, runs=runs, seed=13, tavern_tiers=(tier, tier))
        hp_total = snap.hero_health + snap.hero_armor
        urgency = 1.45 if hp_total < 16 else (1.15 if hp_total < 24 else 1.0)
        # Map win/tie/loss + net damage into a centered bonus around [-5..+6].
        equity = ((sim.win_pct - sim.loss_pct) / 100.0) * 3.8 + (sim.avg_damage * 0.15)
        return round(equity * urgency, 2), round(sim.win_pct, 1)

    def evaluate(
        self,
        snap: GameSnapshot,
    ) -> tuple[ValueBreakdown, list[CompFit]]:
        turn = max(1, snap.turn)
        hp_total = snap.hero_health + snap.hero_armor

        # 1. Board strength
        board_val = sum(self._minion_board_value(m, turn) for m in snap.board)

        # 2. Comp & tribe synergy
        board_cids = [m.card_id for m in snap.board]
        owned_ids = board_cids + [
            m.card_id for m in snap.hand if m.card_type == "MINION"
        ]
        board_tribes: dict[str, int] = {}
        for m in snap.board:
            for r in self._races(m.card_id):
                board_tribes[r] = board_tribes.get(r, 0) + 1

        fits = self._fast_comp_fits(set(owned_ids), board_tribes, len(snap.board))
        active_fits = [f for f in fits if f.available]
        top_fit = next(
            (f for f in active_fits if f.affinity > 0),
            active_fits[0] if active_fits else None,
        )

        synergy_val = 0.0
        # On turns 1-2, Tier 1 minions are tempo/economy, not build commitment.
        early_comp_scale = 0.25 if turn <= 2 else (0.60 if turn == 3 else 1.0)
        candidate_fits = active_fits[:2]
        for cid in owned_ids:
            best_bonus = 0.0
            for fit in candidate_fits:
                role = fit.comp.cards.get(cid)
                if not role:
                    continue
                weight = ROLE_WEIGHT.get(role, 0.5)
                bonus = weight * fit.comp.quality * (4.0 + 8.5 * fit.affinity) * early_comp_scale
                if bonus > best_bonus:
                    best_bonus = bonus
            synergy_val += best_bonus

        real_tribe_counts = [n for r, n in board_tribes.items() if r != "ALL"]
        if real_tribe_counts:
            max_tribe = max(real_tribe_counts)
            if max_tribe >= 2:
                synergy_val += min(10.0, 1.8 * (max_tribe - 1)) * early_comp_scale

        copy_counts: dict[str, int] = {}
        for m in (*snap.board, *snap.hand):
            if not m.golden and m.card_type == "MINION" and m.card_id:
                copy_counts[m.card_id] = copy_counts.get(m.card_id, 0) + 1
        pairs = sum(1 for c in copy_counts.values() if c == 2)
        pair_val = 1.5 if turn <= 2 else 3.8
        synergy_val += pairs * pair_val
        if snap.triples_formed:
            discover_tier = min(6, snap.tier + 1)
            synergy_val += snap.triples_formed * (16.0 + 1.5 * discover_tier)

        # 3. Economy: tier curve, unplayed hand minions, cast spells, future gold, hero power
        economy_val = self._tier_curve_value(snap.tier, turn, hp_total)
        for m in snap.hand:
            if m.card_type != "MINION":
                continue
            q = self._card_quality(m.card_id, turn)
            factor = 0.65 if m.golden else 0.35
            economy_val += factor * (3.5 + max(-2.0, q) + 0.5 * m.tier)

        for sp_cid in snap.spells_cast:
            q = self._card_quality(sp_cid, turn)
            economy_val += max(1.5, 4.2 + q)

        if snap.future_gold_bonus:
            economy_val += snap.future_gold_bonus * 2.2

        if snap.hero_power_id and not snap.hero_power_usable:
            economy_val += 3.2

        economy_val += 0.15 * snap.gold

        # 4. Stochastic roll option value (only if branch ended in RollShop)
        roll_ev = self._roll_option_value(snap, active_fits)

        total = board_val + synergy_val + economy_val + roll_ev
        breakdown = ValueBreakdown(
            total=round(total, 2),
            board=round(board_val, 2),
            synergy=round(synergy_val, 2),
            economy=round(economy_val, 2),
            roll_ev=round(roll_ev, 2),
            target_comp=top_fit.comp.name if (top_fit and top_fit.affinity > 0) else "",
        )
        return breakdown, fits
