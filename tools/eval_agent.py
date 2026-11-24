#!/usr/bin/env python3
"""Offline benchmark and evaluation harness for hs-bg-agent (Phase 6).

    tools/eval_agent.py [path/to/Power.log ...]

Evaluates across captured Power.log sessions:
1. TurnPlanner latency (p50, p95, max ms) and search throughput.
2. Action-category alignment between planner's recommended line and human plays.
3. Simulated combat winrate delta (post-plan board + positioning vs pre-turn board).
4. Combat simulator accuracy on actual logged combats.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from hsbg import parser as P  # noqa: E402
from hsbg.advisor import Advisor  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.combat import simulate  # noqa: E402
from hsbg.planner import TurnPlanner  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402
from hsbg.stats import BGStats  # noqa: E402
from hsbg.tavern import (  # noqa: E402
    BuyMinion,
    BuySpell,
    FreezeShop,
    RollShop,
    SellMinion,
    UpgradeTavern,
    snapshot_from_bg,
)
from hsbg.value import synthetic_enemy_board, tminion_to_cminion  # noqa: E402
import validate_combat as vc  # noqa: E402

BUTTON_TO_KIND = {
    "TB_BaconShop_DragBuy": "BUY",
    "TB_BaconShop_DragBuy_Spell": "BUY_SPELL",
    "TB_BaconShop_DragSell": "SELL",
    "TB_BaconShop_8p_Reroll_Button": "ROLL",
    "TB_BaconShopLockAll_Button": "FREEZE",
}


def _human_action_kind(card_id: str) -> str | None:
    if card_id.startswith("TB_BaconShopTechUp"):
        return "UPGRADE"
    return BUTTON_TO_KIND.get(card_id)


def _plan_action_kinds(plan) -> set[str]:
    kinds: set[str] = set()
    for a in plan.actions:
        if isinstance(a, BuyMinion):
            kinds.add("BUY")
        elif isinstance(a, BuySpell):
            kinds.add("BUY_SPELL")
        elif isinstance(a, SellMinion):
            kinds.add("SELL")
        elif isinstance(a, UpgradeTavern):
            kinds.add("UPGRADE")
        elif isinstance(a, RollShop):
            kinds.add("ROLL")
        elif isinstance(a, FreezeShop):
            kinds.add("FREEZE")
    return kinds


def evaluate_capture(path: Path, cards: CardDB, stats: BGStats) -> dict:
    store = EntityStore()
    bg = BGState(store, cards)
    advisor = Advisor(cards, stats)
    planner = TurnPlanner(advisor)

    planned_turns: dict[int, object] = {}
    human_actions_by_turn: dict[int, set[str]] = {}
    latencies: list[float] = []
    winrate_gains: list[float] = []

    for ev in P.parse_file(path):
        store.apply(ev)
        if isinstance(ev, P.CreateGame):
            advisor.tracker.reset()
            advisor.combat_history.reset()
            planned_turns.clear()
            human_actions_by_turn.clear()
            continue

        if isinstance(ev, P.BlockStart) and bg.turn > 0 and not bg.in_combat:
            kind = _human_action_kind(ev.ref.card_id or "")
            if kind:
                human_actions_by_turn.setdefault(bg.turn, set()).add(kind)

        if (
            not bg.in_combat
            and bg.turn > 0
            and bg.turn not in planned_turns
            and len(bg.shop) >= 3
            and bg.gold >= 3
        ):
            advisor.observe(bg)
            pre_snap = snapshot_from_bg(bg)
            plan = planner.plan(pre_snap)
            planned_turns[bg.turn] = plan
            latencies.append(plan.elapsed_ms)

            enemy = synthetic_enemy_board(bg.turn, max(1, bg.tier))
            pre_c = [tminion_to_cminion(m, cards) for m in pre_snap.board]
            post_c = [tminion_to_cminion(m, cards) for m in plan.end_state.board]
            pre_win = (
                simulate(pre_c, enemy, runs=50, seed=7).win_pct if pre_c else 0.0
            )
            post_win = (
                simulate(post_c, enemy, runs=50, seed=7).win_pct if post_c else 0.0
            )
            winrate_gains.append(post_win - pre_win)

    matched_turns = 0
    compared_turns = 0
    for turn, plan in planned_turns.items():
        human_kinds = human_actions_by_turn.get(turn)
        if not human_kinds:
            continue
        compared_turns += 1
        plan_kinds = _plan_action_kinds(plan)
        if plan_kinds & human_kinds:
            matched_turns += 1

    combats = list(vc.find_combats(path, cards))
    combat_exact = 0
    combat_plausible = 0
    for _turn, mine, opp, actual, tiers in combats:
        sim = simulate(mine, opp, runs=250, seed=7, tavern_tiers=tiers)
        pred = vc.predict_outcome(sim)
        actual_pct = {0: sim.win_pct, 1: sim.loss_pct, -1: sim.tie_pct}[actual]
        combat_exact += int(pred == actual)
        combat_plausible += int(actual_pct >= 5.0)

    return {
        "path": str(path),
        "turns": len(planned_turns),
        "latencies": latencies,
        "compared_turns": compared_turns,
        "matched_turns": matched_turns,
        "winrate_gains": winrate_gains,
        "combats": len(combats),
        "combat_exact": combat_exact,
        "combat_plausible": combat_plausible,
    }


def main() -> int:
    args = [Path(a) for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        args = sorted((ROOT / "data/captures").glob("*/Power.log"))
        live_logs = sorted(Path("/Applications/Hearthstone/Logs").glob("Hearthstone_*/Power.log"))
        args.extend(live_logs)
    if not args:
        print("no Power.log captures found", file=sys.stderr)
        return 1

    cards = CardDB()
    stats = BGStats()

    all_latencies: list[float] = []
    all_gains: list[float] = []
    total_turns = total_compared = total_matched = 0
    total_combats = total_exact = total_plausible = 0

    print("=== hs-bg-agent Offline Evaluation Benchmark ===\n")
    for path in args:
        if not path.exists():
            continue
        res = evaluate_capture(path, cards, stats)
        all_latencies.extend(res["latencies"])
        all_gains.extend(res["winrate_gains"])
        total_turns += res["turns"]
        total_compared += res["compared_turns"]
        total_matched += res["matched_turns"]
        total_combats += res["combats"]
        total_exact += res["combat_exact"]
        total_plausible += res["combat_plausible"]

        avg_gain = statistics.mean(res["winrate_gains"]) if res["winrate_gains"] else 0.0
        p50 = statistics.median(res["latencies"]) if res["latencies"] else 0.0
        print(
            f"• {path.parent.name}: {res['turns']} turns | p50={p50:.1f}ms | "
            f"action overlap={res['matched_turns']}/{res['compared_turns']} | "
            f"avg combat winrate delta={avg_gain:+.1f}% | "
            f"combat sim exact={res['combat_exact']}/{res['combats']}"
        )

    print("\n--- Aggregate Metrics ---")
    if all_latencies:
        p50 = statistics.median(all_latencies)
        s_lat = sorted(all_latencies)
        p95 = s_lat[min(len(s_lat) - 1, int(len(s_lat) * 0.95))]
        print(f"Planner latency      : n={total_turns} turns | p50={p50:.1f}ms | p95={p95:.1f}ms | max={max(s_lat):.1f}ms")
    if total_compared:
        pct = total_matched / total_compared * 100.0
        print(f"Human action overlap : {total_matched}/{total_compared} ({pct:.0f}%)")
    if all_gains:
        print(f"Sim board winrate Δ  : {statistics.mean(all_gains):+.1f}% per turn vs pre-shop board")
    if total_combats:
        print(
            f"Combat sim accuracy  : exact={total_exact}/{total_combats} ({total_exact / total_combats * 100:.0f}%) | "
            f"plausible={total_plausible}/{total_combats} ({total_plausible / total_combats * 100:.0f}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
