"""Beam-search turn planner and combat positioning optimizer for Battlegrounds.

Searches legal action sequences under the current gold budget, deduplicates
transpositions, refines top candidates with Monte Carlo combat simulation and
MinionPool rollout expectations, optimizes final board positioning, and surfaces
a distinct alternative line.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace

from .advisor import Advisor, CompFit
from .combat import CMinion, simulate
from .i18n import get_lang, t
from .tavern import (
    Action,
    BuyMinion,
    BuySpell,
    FreezeShop,
    GameSnapshot,
    PlayMinion,
    PlaySpell,
    ReorderBoard,
    RollShop,
    SellMinion,
    TMinion,
    UpgradeTavern,
    UseHeroPower,
    apply_action,
    legal_actions,
    snapshot_from_bg,
    state_key,
)
from .value import (
    ValueBreakdown,
    ValueFunction,
    synthetic_enemy_board,
    tminion_to_cminion,
)


@dataclass(frozen=True)
class TurnPlan:
    actions: tuple[Action, ...]
    end_state: GameSnapshot
    value: ValueBreakdown
    baseline_value: float
    target_comp: CompFit | None = None
    alternatives: tuple[TurnPlan, ...] = ()
    elapsed_ms: float = 0.0
    nodes_expanded: int = 0

    @property
    def gain(self) -> float:
        return round(self.value.total - self.baseline_value, 2)

    def format_steps(self) -> list[str]:
        """Format action sequence into concise human steps (merging adjacent Buy + Play of the same card)."""
        if not self.actions:
            return [t("hold_turn")]
        uk = get_lang() == "uk"
        unit = "з" if uk else "g"
        merged: list[str] = []
        i = 0
        n = len(self.actions)
        while i < n:
            act = self.actions[i]
            # Merge BuyMinion(X) immediately followed by PlayMinion(X) into one clear step
            if (
                isinstance(act, BuyMinion)
                and (i + 1) < n
                and isinstance(self.actions[i + 1], PlayMinion)
                and self.actions[i + 1].card_id == act.card_id
            ):
                play_act = self.actions[i + 1]
                opt_str = f" [{play_act.option_label}]" if play_act.option_label else ""
                tgt_str = f" -> {play_act.target_name}" if play_act.target_name else ""
                if uk:
                    merged.append(f"Купити й виставити {act.name}{opt_str}{tgt_str} (-{act.cost}{unit})")
                else:
                    merged.append(f"Buy & play {act.name}{opt_str}{tgt_str} (-{act.cost}{unit})")
                i += 2
                continue
            merged.append(act.describe())
            i += 1
        return [f"{idx}. {step_txt}" for idx, step_txt in enumerate(merged, 1)]

    def explain_strategy(self) -> str:
        """Plain-language summary of WHY this line is recommended (no cryptic internal math)."""
        uk = get_lang() == "uk"
        has_up = any(isinstance(a, UpgradeTavern) for a in self.actions)
        buys = [a for a in self.actions if isinstance(a, BuyMinion)]
        sells = [a for a in self.actions if isinstance(a, SellMinion)]
        spells = [a for a in self.actions if isinstance(a, (BuySpell, PlaySpell))]
        rolls = [a for a in self.actions if isinstance(a, RollShop)]
        hp_acts = [a for a in self.actions if isinstance(a, UseHeroPower)]
        triples = self.end_state.triples_formed
        tier = self.end_state.tier

        if triples > 0:
            disc_t = min(6, tier + 1)
            return (
                f"Збираємо золотий триплет під розкопку T{disc_t}"
                if uk
                else f"Complete Golden triple to Discover a T{disc_t} card"
            )
        if has_up and buys:
            return (
                f"Апаємо T{tier} і одразу добираємо карту в темп"
                if uk
                else f"Upgrade to T{tier} and buy a minion for tempo"
            )
        if has_up:
            return (
                f"Апаємо таверну до T{tier} по кривій (кращі карти далі)"
                if uk
                else f"Standard curve upgrade to T{tier} for higher-tier cards"
            )
        if hp_acts and not buys:
            tgt = hp_acts[0].target_name
            if tgt:
                return f"Використовуємо абілку героя в {tgt}" if uk else f"Use Hero Power on {tgt}"
            return "Граємо від абілки героя цього ходу" if uk else "Play around Hero Power tempo this turn"
        if buys:
            names = ", ".join(b.name for b in buys[:2])
            if self.target_comp and self.value.synergy >= 4.0:
                comp_nm = self.target_comp.comp.name
                return (
                    f"Посилюємо білд {comp_nm} (беремо {names})"
                    if uk
                    else f"Build toward {comp_nm} (pick up {names})"
                )
            if sells:
                return (
                    f"Міняємо слабкий слот на сильніші карти ({names})"
                    if uk
                    else f"Replace weak board slot with stronger stats ({names})"
                )
            return (
                f"Набираємо темп і стати на стіл ({names})"
                if uk
                else f"Maximize board tempo and stats ({names})"
            )
        if spells and rolls:
            return (
                "Магазин слабкий — беремо дешевий спел і робимо рол"
                if uk
                else "Weak shop — grab cheap spell and roll for better cards"
            )
        if spells:
            return "Посилюємо стіл/економіку заклинанням" if uk else "Boost board/economy with Tavern Spell"
        if rolls:
            return (
                "У таверні немає хороших карт — робимо рол і шукаємо далі"
                if uk
                else "Current shop is weak — reroll to find playable cards"
            )
        return "Зберігаємо поточний стіл" if uk else "Hold current board"


@dataclass
class _Node:
    snap: GameSnapshot
    actions: tuple[Action, ...]
    value: ValueBreakdown
    fits: list[CompFit]


def _tactical_slot_key(m: TMinion, cards) -> tuple[int, int]:
    """Heuristic left-to-right tactical ordering seed for Battlegrounds combat.

    Bucket 0 (leftmost): Cleave / high-ATK Divine Shield / Deathrattle buffers.
    Bucket 1 (middle): Standard attackers descending by ATK.
    Bucket 2 (backline): Low-ATK scaling/support units.
    Bucket 3 (rightmost): Taunts (isolated at the far edge to minimize cleave splash).
    """
    card = cards.by_id(m.card_id) if cards else None
    mechs = set(card.mechanics if card else ())
    text = (card.text or "").lower() if card else ""
    if "TAUNT" in mechs:
        return (3, -m.atk)
    is_cleave = "also damages the minions next to" in text
    is_ds = "DIVINE_SHIELD" in mechs and m.atk >= 4
    is_dr_lead = "DEATHRATTLE" in mechs and (
        "give" in text or "summon" in text or "beetle" in text
    )
    if is_cleave or is_ds or is_dr_lead:
        return (0, -m.atk)
    if m.atk <= 2 and ("whenever" in text or "other" in text or "avenge" in text):
        return (2, -m.atk)
    return (1, -m.atk)


def optimize_positioning(
    board: tuple[TMinion, ...],
    enemy_board: list[CMinion] | None,
    cards,
    runs: int = 32,
) -> tuple[TMinion, ...] | None:
    """Find a higher-equity left-to-right board order via tactical seeds + local swaps.

    Returns the improved `tuple[TMinion, ...]` if strictly better than `board`,
    or `None` if current ordering is already optimal.
    """
    n = len(board)
    if n < 2:
        return None
    # If all minions are identical card_id and stats, order is invariant.
    if len({(m.card_id, m.atk, m.health, m.golden) for m in board}) <= 1:
        return None

    opp = enemy_board or synthetic_enemy_board(
        turn=max(1, n), tier=max((m.tier for m in board), default=1)
    )
    tier = max((max(1, m.tier) for m in board), default=1)

    tactical = tuple(sorted(board, key=lambda m: _tactical_slot_key(m, cards)))
    candidates: list[tuple[TMinion, ...]] = [board]
    seen_orders = {tuple(m.uid for m in board)}

    def _add_cand(order: tuple[TMinion, ...]) -> None:
        sig = tuple(m.uid for m in order)
        if sig not in seen_orders:
            seen_orders.add(sig)
            candidates.append(order)

    _add_cand(tactical)
    # Generate adjacent pairwise swaps around tactical and current orderings.
    for seed_order in (tactical, board):
        lst = list(seed_order)
        for i in range(n - 1):
            swapped = lst[:]
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            _add_cand(tuple(swapped))
            if len(candidates) >= 10:
                break
        if len(candidates) >= 10:
            break

    def _combat_utility(order: tuple[TMinion, ...]) -> float:
        c_board = [tminion_to_cminion(m, cards) for m in order]
        sim = simulate(c_board, opp, runs=runs, seed=17, tavern_tiers=(tier, tier))
        return (sim.win_pct - sim.loss_pct) + 0.4 * sim.avg_damage

    base_util = _combat_utility(board)
    best_order = board
    best_util = base_util

    for cand in candidates[1:]:
        u = _combat_utility(cand)
        # Prefer tactical seed on ties if tactical arrangement differs meaningfully,
        # or any ordering that improves simulated combat utility by >= 2.5 points.
        if u > best_util + 2.5 or (
            cand == tactical
            and u >= best_util
            and [_tactical_slot_key(m, cards)[0] for m in cand]
            != [_tactical_slot_key(m, cards)[0] for m in board]
        ):
            best_util = u
            best_order = cand

    if tuple(m.uid for m in best_order) == tuple(m.uid for m in board):
        return None
    return best_order


class TurnPlanner:
    def __init__(
        self,
        advisor: Advisor,
        beam_width: int = 24,
        max_depth: int = 8,
        time_budget_ms: float = 80.0,
    ) -> None:
        self.advisor = advisor
        self.value_fn = ValueFunction(advisor)
        self.beam_width = beam_width
        self.max_depth = max_depth
        self.time_budget_ms = time_budget_ms

    def _maybe_freeze(self, node: _Node) -> _Node:
        """Append FreezeShop if high-value unaffordable cards remain in shop."""
        snap = node.snap
        if snap.rolled or snap.frozen_shop or not snap.shop:
            return node

        owned_counts: dict[str, int] = {}
        for m in (*snap.board, *snap.hand):
            if not m.golden and m.card_type == "MINION" and m.card_id:
                owned_counts[m.card_id] = owned_counts.get(m.card_id, 0) + 1

        board_tribes: dict[str, int] = {}
        for m in snap.board:
            card = self.advisor.cards.by_id(m.card_id)
            for r in (card.races if card else ()):
                board_tribes[r] = board_tribes.get(r, 0) + 1

        top_comp = next((f for f in node.fits if f.available and f.affinity > 0), None)
        freeze_targets: list[str] = []
        for m in snap.shop:
            if owned_counts.get(m.card_id, 0) >= 2:
                freeze_targets.append(m.name)
                continue
            if top_comp and top_comp.comp.cards.get(m.card_id) == "CORE":
                freeze_targets.append(m.name)
                continue
            sg = self.advisor.score_card(
                m.card_id, board_tribes, node.fits, snap.turn, owned_counts
            )
            if sg.score >= 74.0:
                freeze_targets.append(m.name)

        if not freeze_targets:
            return node

        act = FreezeShop(tuple(freeze_targets[:2]))
        frozen_snap = apply_action(snap, act)
        return _Node(
            snap=frozen_snap,
            actions=node.actions + (act,),
            value=node.value,
            fits=node.fits,
        )

    def _expectimax_roll_adjustment(self, node: _Node, samples: int = 3) -> float:
        """Refine RollShop valuation by sampling K shops from MinionPool."""
        snap = node.snap
        pool = self.value_fn.pool
        if not snap.rolled or snap.gold < snap.buy_cost or pool is None:
            return 0.0
        if not pool.available_up_to_tier(snap.tier):
            return 0.0

        rng = random.Random(42 + snap.turn * 7 + snap.gold)
        best_gains: list[float] = []
        base_unrolled = replace(snap, rolled=False)
        base_val, _ = self.value_fn.evaluate(base_unrolled)

        for s_idx in range(samples):
            sampled_shop = pool.sample_shop(
                snap.tier, rng=rng, start_uid=300_000 + s_idx * 10
            )
            hyp = replace(snap, rolled=False, shop=sampled_shop)
            best_sample_val = base_val.total
            # 1-step greedy buy+play lookahead on the sampled shop
            for act in legal_actions(hyp):
                if isinstance(act, RollShop):
                    continue
                nxt = apply_action(hyp, act)
                for sub_act in legal_actions(nxt):
                    if sub_act.__class__.__name__ == "PlayMinion":
                        nxt = apply_action(nxt, sub_act)
                        break
                v, _ = self.value_fn.evaluate(nxt)
                if v.total > best_sample_val:
                    best_sample_val = v.total
            best_gains.append(max(0.0, best_sample_val - base_val.total))

        avg_sample_gain = sum(best_gains) / len(best_gains)
        # Blend analytical roll_ev (75%) with sampled pool expectation (25%).
        return round(0.25 * (avg_sample_gain - node.value.roll_ev), 2)

    def _plan_signature(self, actions: tuple[Action, ...]) -> tuple:
        """Coarse behavioral signature to select genuinely distinct alternatives."""
        has_upgrade = any(isinstance(a, UpgradeTavern) for a in actions)
        ends_roll = bool(actions and isinstance(actions[-1], RollShop))
        first_kind = type(actions[0]).__name__ if actions else "Hold"
        return (has_upgrade, ends_roll, first_kind)

    def plan(
        self,
        snap: GameSnapshot,
        enemy_board: list[CMinion] | None = None,
    ) -> TurnPlan:
        """Search for the highest-value action sequence from `snap`."""
        t0 = time.perf_counter()
        deadline = t0 + (self.time_budget_ms / 1000.0)
        self.value_fn.prepare()

        root_val, root_fits = self.value_fn.evaluate(snap)
        root = _Node(snap=snap, actions=(), value=root_val, fits=root_fits)

        visited: dict[tuple, tuple[float, int, _Node]] = {
            state_key(snap): (root_val.total, 0, root)
        }
        frontier: list[_Node] = [root]
        all_candidates: list[_Node] = [root]
        expanded = 0

        for _depth in range(self.max_depth):
            if not frontier or time.perf_counter() >= deadline:
                break
            next_layer: list[_Node] = []
            for node in frontier:
                if time.perf_counter() >= deadline:
                    break
                actions = legal_actions(node.snap)
                if not actions:
                    continue
                expanded += 1
                for act in actions:
                    child_snap = apply_action(node.snap, act)
                    key = state_key(child_snap)
                    new_actions = node.actions + (act,)
                    prev = visited.get(key)
                    if prev is not None and prev[1] <= len(new_actions):
                        continue
                    child_val, child_fits = self.value_fn.evaluate(child_snap)
                    child_node = _Node(
                        snap=child_snap,
                        actions=new_actions,
                        value=child_val,
                        fits=child_fits,
                    )
                    if (
                        prev is None
                        or child_val.total > prev[0]
                        or (child_val.total == prev[0] and len(new_actions) < prev[1])
                    ):
                        visited[key] = (child_val.total, len(new_actions), child_node)
                        all_candidates.append(child_node)
                        if not child_snap.rolled:
                            next_layer.append(child_node)

            next_layer.sort(key=lambda n: (-n.value.total, len(n.actions)))
            frontier = next_layer[: self.beam_width]

        all_candidates.sort(key=lambda n: (-n.value.total, len(n.actions)))

        # Refine top candidates with Expectimax pool sampling & combat equity if time allows.
        if (time.perf_counter() + 0.010) < deadline:
            for cand in all_candidates[:4]:
                adj = self._expectimax_roll_adjustment(cand)
                if adj != 0.0:
                    cand.value = replace(
                        cand.value,
                        total=round(cand.value.total + adj, 2),
                        roll_ev=round(cand.value.roll_ev + adj, 2),
                    )
            all_candidates.sort(key=lambda n: (-n.value.total, len(n.actions)))

        best = self._maybe_freeze(all_candidates[0])

        # Optimize board positioning on the winning plan if time budget permits.
        if len(best.snap.board) >= 2 and (time.perf_counter() + 0.008) < deadline:
            reordered = optimize_positioning(
                best.snap.board, enemy_board, self.advisor.cards, runs=24
            )
            if reordered is not None:
                reorder_act = ReorderBoard(tuple(m.name for m in reordered))
                best = _Node(
                    snap=replace(best.snap, board=reordered),
                    actions=best.actions + (reorder_act,),
                    value=best.value,
                    fits=best.fits,
                )

        # Select up to 1 distinct alternative line (e.g. Upgrade vs Buy/Roll).
        best_sig = self._plan_signature(best.actions)
        alternatives: list[TurnPlan] = []
        seen_sigs = {best_sig}
        for cand in all_candidates[1:]:
            sig = self._plan_signature(cand.actions)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            alt_node = self._maybe_freeze(cand)
            alt_target = next(
                (f for f in alt_node.fits if f.available and f.affinity > 0), None
            )
            alternatives.append(
                TurnPlan(
                    actions=alt_node.actions,
                    end_state=alt_node.snap,
                    value=alt_node.value,
                    baseline_value=root_val.total,
                    target_comp=alt_target,
                )
            )
            if len(alternatives) >= 1:
                break

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # Compute full detailed CompFit (with matched/missing card names) once for the final state
        end_board_cids = [m.card_id for m in best.snap.board]
        end_owned = end_board_cids + [
            m.card_id for m in best.snap.hand if m.card_type == "MINION"
        ]
        end_tribes: dict[str, int] = {}
        for m in best.snap.board:
            for r in self.value_fn._races(m.card_id):
                end_tribes[r] = end_tribes.get(r, 0) + 1
        rich_fits = self.advisor.comp_fits(
            end_owned, end_tribes, len(best.snap.board), board_card_ids=end_board_cids
        )
        target = next((f for f in rich_fits if f.available and f.affinity > 0), None)
        return TurnPlan(
            actions=best.actions,
            end_state=best.snap,
            value=best.value,
            baseline_value=root_val.total,
            target_comp=target,
            alternatives=tuple(alternatives),
            elapsed_ms=round(elapsed_ms, 2),
            nodes_expanded=expanded,
        )

    def plan_bg(self, bg, enemy_board: list[CMinion] | None = None) -> TurnPlan:
        """Convenience wrapper accepting a live BGState."""
        return self.plan(snapshot_from_bg(bg), enemy_board=enemy_board)
