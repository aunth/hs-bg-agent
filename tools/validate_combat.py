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
from hsbg.state import EntityStore  # noqa: E402
from hsbg.combat import CMinion, simulate  # noqa: E402

KEYWORDS = {
    "TAUNT": "taunt",
    "DIVINE_SHIELD": "divine_shield",
    "POISONOUS": "poisonous",
    "WINDFURY": "windfury",
    "MEGA_WINDFURY": "mega_windfury",
    "REBORN": "reborn",
}


def combat_minions(store: EntityStore, controller: int) -> list[CMinion]:
    """Same filter as BGState._minions, but keeping the keyword tags the
    combat simulator needs and that the display-oriented Minion drops."""
    out = []
    for e in store.entities.values():
        if e.tag("CARDTYPE") != "MINION" or e.tag("ZONE") != "PLAY":
            continue
        if e.int_tag("CONTROLLER", -1) != controller:
            continue
        kw = {py: e.int_tag(tag) == 1 for tag, py in KEYWORDS.items()}
        out.append(CMinion(
            card_id=e.card_id,
            name=e.tag("_name", "") or e.card_id,
            atk=e.int_tag("ATK"),
            health=e.int_tag("HEALTH"),
            golden=e.int_tag("PREMIUM") == 1,
            **kw,
        ))
    return sorted(out, key=lambda m: -m.health)  # order doesn't affect the sim


def find_combats(path):
    """Yield (turn, my_pre, opp_pre, actual) for every combat in a capture."""
    store = EntityStore()
    my_id = bob_id = None
    raw_turn = 0
    pre = None       # (my_pre, opp_pre) once the fight's first attack is seen
    resolved = True  # no fight in flight yet
    depth = 0        # block-nesting depth; only check for a winner at depth 0

    for ev in P.parse_file(path):
        if isinstance(ev, P.CreateGame):
            store.apply(ev)
            my_id = bob_id = None
            raw_turn = 0
            pre = None
            resolved = True
            depth = 0
            continue

        if isinstance(ev, P.TagChange) and ev.ref.is_game and ev.tag == "TURN":
            raw_turn = int(ev.value)

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

        if pre is None and isinstance(ev, P.BlockStart) and ev.block_type == "ATTACK":
            pre = (combat_minions(store, my_id), combat_minions(store, bob_id))
            resolved = False
            continue

        if pre is not None and not resolved and depth == 0:
            mine, opp = combat_minions(store, my_id), combat_minions(store, bob_id)
            if not mine or not opp:
                actual = 0 if mine and not opp else (1 if opp and not mine else -1)
                yield raw_turn, pre[0], pre[1], actual
                resolved = True
                pre = None


def label(w):
    return {0: "WIN", 1: "LOSS", -1: "TIE"}[w]


def main() -> int:
    args = sys.argv[1:]
    if not args:
        root = Path(__file__).resolve().parent.parent / "data/captures"
        args = sorted(str(p) for p in root.glob("*/Power.log"))
    if not args:
        print("no captures found; run tools/capture.sh first", file=sys.stderr)
        return 1

    total = agree = 0
    for path in args:
        combats = list(find_combats(path))
        print(f"\n{path}")
        for turn, mine, opp, actual in combats:
            sim = simulate(mine, opp, runs=300)
            if sim.win_pct == sim.loss_pct:
                predicted = -1
            else:
                predicted = 0 if sim.win_pct > sim.loss_pct else 1
            hit = predicted == actual or predicted == -1 or actual == -1
            total += 1
            agree += hit
            mark = "agree" if hit else "DISAGREE"
            print(f"  turn {turn:>3}: {len(mine)}v{len(opp)}  actual={label(actual):<4} "
                  f"sim={sim}  -> {mark}")

    if total:
        print(f"\n{agree}/{total} winner predictions agree ({agree/total*100:.0f}%)")
    else:
        print("\nno resolved combats found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
