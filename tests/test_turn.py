"""Verify raw TURN -> Battlegrounds turn mapping against gold progression."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hsbg import parser as P  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402

CAP = ROOT / "data/captures/Hearthstone_2026_08_20_03_08_56_live-game2/Power.log"


class TurnMappingTests(unittest.TestCase):
    def test_mapping(self) -> None:
        self.assertEqual(
            [(r, (r + 1) // 2) for r in (1, 3, 5, 7, 9, 11)],
            [(1, 1), (3, 2), (5, 3), (7, 4), (9, 5), (11, 6)],
        )

    def test_gold_matches_turn(self) -> None:
        if not CAP.exists():
            self.skipTest("capture missing")
        store = EntityStore()
        bg = BGState(store, CardDB())
        peak: dict[int, int] = {}
        for ev in P.parse_file(CAP):
            store.apply(ev)
            if bg.turn and bg.gold:
                peak[bg.turn] = max(peak.get(bg.turn, 0), bg.gold)
        bad = [(t, g, min(10, t + 2)) for t, g in peak.items() if g != min(10, t + 2)]
        self.assertEqual(bad, [], f"turn/gold mismatch: {bad}")


if __name__ == "__main__":
    unittest.main()
