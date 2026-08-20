#!/usr/bin/env python3
"""Refresh cached Battlegrounds meta stats (at most weekly unless --force)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hsbg.stats import BGStats  # noqa: E402

force = "--force" in sys.argv
s = BGStats()
got = s.refresh(force=force)
print(f"fetched: {got or 'nothing (cache still fresh)'}")
print(f"heroes: {len(s.heroes)}  cards: {len(s.cards)}  updated: {s.updated}")
