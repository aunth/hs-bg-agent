#!/usr/bin/env python3
"""Validate the combat simulator against real captured combats.

    tools/validate_combat.py [path/to/Power.log ...]

With no arguments, validates every capture under data/captures/.

How a combat is found in the log: the GameEntity's TURN tag counts shop and
combat phases separately, alternating -- odd is shop, even is combat (see
docs/findings-01-power-log.md). During a combat phase, the *opponent's* board
is copied into Bartender Bob's player slot for the duration of the fight, so
the same CONTROLLER filter that normally reads Bob's shop offers reads the
actual enemy army instead. The pre-fight board is snapshotted the moment the
first BLOCK_START BlockType=ATTACK fires (setup -- copying the opponent's
board in, revealing trinkets -- is done by then, nothing has traded yet).

The result cannot be read off the state right before TURN flips back to odd:
by then the game has already torn down both boards (every borrowed enemy
minion, and apparently leftovers from the loser too, move to
ZONE=REMOVEDFROMGAME), so that read sees "both empty" for almost every fight
regardless of who actually won. Instead, the result is taken at the first moment either side's live board legitimately
empties during the fight -- exactly the same termination condition our own
simulator uses -- which happens before any of that teardown. That check has
to wait for the current top-level block to fully close first: a side that
just lost its last minion can still be mid deathrattle-chain (e.g. a token
summon), so checking mid-block sees a false, transient zero.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg import parser as P  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402
from hsbg.combat import simulate  # noqa: E402


def find_combats(path, cards: CardDB | None = None):
    """Yield (turn, my_pre, opp_pre, actual, tiers) for every combat in a capture."""
    store = EntityStore()
    bg = BGState(store, cards)
    my_id = bob_id = None
    raw_turn = 0
    pre = None              # (my_pre, opp_pre, tiers) once the fight's first attack is seen
    resolved_turn = -1      # guard: at most one combat per even raw_turn
    depth = 0               # block-nesting depth; only check for a winner at depth 0

    for ev in P.parse_file(path):
        if isinstance(ev, P.CreateGame):
            store.apply(ev)
            my_id = bob_id = None
            raw_turn = 0
            pre = None
            resolved_turn = -1
            depth = 0
            continue

        if isinstance(ev, P.TagChange) and ev.ref.is_game and ev.tag == "TURN":
            raw_turn = int(ev.value)
            if raw_turn % 2 == 1:
                pre = None

        if isinstance(ev, P.BlockStart):
            depth += 1
        elif isinstance(ev, P.BlockEnd):
            depth = max(0, depth - 1)

        store.apply(ev)

        if my_id is None:
            local, bob = store.local_player, store.bob_player
            if local and bob:
                my_id, bob_id = local.player_id, bob.player_id

        if my_id is None or raw_turn == 0 or raw_turn % 2 != 0:
            continue  # no player bound yet, or currently in the shop phase

        if (
            pre is None
            and resolved_turn != raw_turn
            and isinstance(ev, P.BlockStart)
            and ev.block_type == "ATTACK"
        ):
            mine_pre = bg.combat_minions(my_id)
            opp_pre = bg.combat_minions(bob_id)
            if mine_pre and opp_pre:
                my_tier = max(1, bg.tier)
                pre = (mine_pre, opp_pre, (my_tier, my_tier))
            continue

        if pre is not None and resolved_turn != raw_turn and depth == 0:
            mine, opp = bg.combat_minions(my_id), bg.combat_minions(bob_id)
            if not mine or not opp:
                actual = 0 if mine and not opp else (1 if opp and not mine else -1)
                yield raw_turn, pre[0], pre[1], actual, pre[2]
                resolved_turn = raw_turn
                pre = None


def label(w):
    return {0: "WIN", 1: "LOSS", -1: "TIE"}[w]


def predict_outcome(sim) -> int:
    best = max((sim.win_pct, 0), (sim.loss_pct, 1), (sim.tie_pct, -1))
    return best[1]


def main() -> int:
    args = sys.argv[1:]
    if not args:
        root = Path(__file__).resolve().parent.parent / "data/captures"
        args = sorted(str(p) for p in root.glob("*/Power.log"))
    if not args:
        print("no captures found; run tools/capture.sh first", file=sys.stderr)
        return 1

    cards = CardDB()
    total = exact = plausible = 0
    for path in args:
        combats = list(find_combats(path, cards))
        print(f"\n{path}")
        for turn, mine, opp, actual, tiers in combats:
            sim = simulate(mine, opp, runs=400, seed=7, tavern_tiers=tiers)
            predicted = predict_outcome(sim)
            actual_pct = {0: sim.win_pct, 1: sim.loss_pct, -1: sim.tie_pct}[actual]
            is_exact = predicted == actual
            is_plausible = actual_pct >= 5.0
            total += 1
            exact += is_exact
            plausible += is_plausible
            mark = "EXACT" if is_exact else ("PLAUSIBLE" if is_plausible else "DISAGREE")
            print(
                f"  bg_t{(turn + 1) // 2:>2} (raw {turn:>2}): {len(mine)}v{len(opp)}  "
                f"actual={label(actual):<4} sim={sim}  -> {mark}"
            )

    if total:
        print(f"\nexact mode match : {exact}/{total} ({exact / total * 100:.0f}%)")
        print(f"plausible (>=5%) : {plausible}/{total} ({plausible / total * 100:.0f}%)")
    else:
        print("\nno resolved combats found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
