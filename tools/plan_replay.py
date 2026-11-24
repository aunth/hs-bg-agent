#!/usr/bin/env python3
"""Offline replay harness for TurnPlanner on captured Power.log sessions.

    tools/plan_replay.py [path/to/Power.log]

Runs the turn planner once per shop phase (when the fresh shop settles) and
prints the recommended action line, target comp, alternative line, and latency.
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg import parser as P  # noqa: E402
from hsbg.advisor import Advisor  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.planner import TurnPlanner  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402
from hsbg.stats import BGStats  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        path = Path(args[0])
    else:
        caps = sorted(
            (Path(__file__).resolve().parent.parent / "data/captures").glob("*/Power.log")
        )
        if not caps:
            print("no captures found; run tools/capture.sh first", file=sys.stderr)
            return 1
        path = caps[-1]

    store = EntityStore()
    cards = CardDB()
    stats = BGStats()
    bg = BGState(store, cards)
    advisor = Advisor(cards, stats)
    planner = TurnPlanner(advisor)

    planned_turns: set[int] = set()
    latencies: list[float] = []

    print(f"Replaying: {path}\n")
    for ev in P.parse_file(path):
        store.apply(ev)
        if isinstance(ev, P.CreateGame):
            advisor.tracker.reset()
            advisor.combat_history.reset()
            planned_turns.clear()
            continue

        # Plan once per shop turn as soon as the shop has settled with gold > 0.
        if (
            not bg.in_combat
            and bg.turn > 0
            and bg.turn not in planned_turns
            and len(bg.shop) >= 3
            and bg.gold >= 3
        ):
            advisor.observe(bg)
            plan = planner.plan_bg(bg)
            planned_turns.add(bg.turn)
            latencies.append(plan.elapsed_ms)

            up = f"up={bg.upgrade_cost}g" if bg.upgrade_cost is not None else "up=-"
            target = (
                f"{plan.target_comp.comp.name} ({plan.target_comp.comp.power_level})"
                if plan.target_comp
                else "open / no comp yet"
            )
            print(
                f"=== Turn {bg.turn:>2} | T{bg.tier} | {bg.gold}g | {up} | "
                f"HP {bg.hero_health}+{bg.hero_armor} | {plan.elapsed_ms:.1f}ms ({plan.nodes_expanded} nodes) ==="
            )
            print("  shop :", ", ".join(str(m) for m in bg.shop))
            if bg.shop_spells:
                print("  spell:", ", ".join(str(s) for s in bg.shop_spells))
            print("  board:", ", ".join(str(m) for m in bg.board) or "(empty)")
            print(f"  target: {target}")
            print(f"  PLAN (val={plan.value.total:.1f}, gain={plan.gain:+.1f}):")
            for step in plan.format_steps():
                print(f"    {step}")
            if plan.alternatives:
                alt = plan.alternatives[0]
                alt_summary = " -> ".join(a.describe() for a in alt.actions) or "Hold"
                print(f"  ALT  (val={alt.value.total:.1f}): {alt_summary}")
            print()

    if latencies:
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(
            f"Summary: {len(latencies)} turns planned | "
            f"p50={p50:.1f}ms | p95={p95:.1f}ms | max={max(latencies):.1f}ms"
        )
    else:
        print("No shop turns found in capture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
