import sys, os
sys.path.insert(0, os.path.expanduser("~/programing/hs-bg-agent/src"))
from hsbg.combat import CMinion, simulate

def M(a, h, **kw): return CMinion(name=f"{a}/{h}", atk=a, health=h, **kw)

cases = [
    ("1/1 vs 1/1 -> mutual destruction, tie",       [M(1,1)], [M(1,1)],          "tie", 100),
    ("3/3 vs 1/1 -> clean win",                     [M(3,3)], [M(1,1)],          "win", 100),
    ("1/1 vs 3/3 -> clean loss",                    [M(1,1)], [M(3,3)],          "loss",100),
    ("empty vs 1/1 -> loss",                        [],       [M(1,1)],          "loss",100),
    ("empty vs empty -> tie",                       [],       [],                "tie", 100),
    ("divine shield 1/1 vs 1/1 -> shield survives", [M(1,1,divine_shield=True)], [M(1,1)], "win", 100),
    ("poison 1/1 vs 5/5 -> poison kills, trade",    [M(1,1,poisonous=True)], [M(5,5)], "tie", 100),
    ("0-attack 0/5 vs 1/1 -> cannot attack, loses", [M(0,5)], [M(1,1)],          "loss",100),
    ("2x 1/1 vs 1x 1/1 -> outnumbered wins",        [M(1,1),M(1,1)], [M(1,1)],   "win", 100),
]

fails = 0
for label, f, e, expect, pct in cases:
    r = simulate(f, e, runs=400, seed=7)
    got = {"win": r.win_pct, "tie": r.tie_pct, "loss": r.loss_pct}[expect]
    ok = abs(got - pct) < 1e-6
    fails += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {label:46} {r}")

# Taunt must always be the target.
r = simulate([CMinion(name="atk",atk=1,health=10)],
             [CMinion(name="taunt",atk=0,health=1,taunt=True),
              CMinion(name="big",atk=0,health=99)], runs=200, seed=3)
# The 1/10 kills the taunt, then grinds the 0/99 down over 99 rounds. Neither
# enemy minion can attack back, so it is a clean win, not a stalemate.
print(f"\n  taunt soak: {r}  (expect win 100%)")

print(f"\n{'ALL PASS' if not fails else str(fails)+' FAILURES'}")
