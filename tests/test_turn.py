"""The log's TURN tag counts shop and combat phases, so it runs at double the
Battlegrounds turn. Gold is the independent check: min(10, turn + 2)."""
import sys, os
sys.path.insert(0, os.path.expanduser("~/programing/hs-bg-agent/src"))
from hsbg import parser as P
from hsbg.carddb import CardDB
from hsbg.state import BGState, EntityStore

CAP = os.path.expanduser("~/programing/hs-bg-agent/data/captures/"
                         "Hearthstone_2026_08_20_03_08_56_live-game2/Power.log")

def test_mapping():
    assert [(r, (r + 1) // 2) for r in (1, 3, 5, 7, 9, 11)] == \
           [(1, 1), (3, 2), (5, 3), (7, 4), (9, 5), (11, 6)]

def test_gold_matches_turn():
    """Peak gold on each turn must equal min(10, turn + 2)."""
    if not os.path.exists(CAP):
        print("  SKIP (capture missing)"); return
    store = EntityStore(); bg = BGState(store, CardDB())
    peak = {}
    for ev in P.parse_file(CAP):
        store.apply(ev)
        if bg.turn and bg.gold:
            peak[bg.turn] = max(peak.get(bg.turn, 0), bg.gold)
    bad = [(t, g, min(10, t + 2)) for t, g in peak.items() if g != min(10, t + 2)]
    assert not bad, f"turn/gold mismatch: {bad}"
    print(f"  checked turns {sorted(peak)} -> gold {[peak[t] for t in sorted(peak)]}")

for fn in (test_mapping, test_gold_matches_turn):
    fn(); print(f"  PASS {fn.__name__}")
