#!/usr/bin/env python3
"""Prefetch card art for every meta composition, so nothing downloads mid-match."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from hsbg.art import ArtCache      # noqa: E402
from hsbg.stats import BGStats     # noqa: E402

stats = BGStats()
ids = [cid for comp in stats.comps for cid in comp.cards]
art = ArtCache()
uniq = list(dict.fromkeys(ids))
r_ok, r_bad = art.prefetch(uniq, "render")
a_ok, _ = art.prefetch([c for c in uniq if not art.get(c, "render", fetch=False)], "art")
have = sum(1 for c in uniq if art.best(c, fetch=False))
print(f"{len(stats.comps)} comps, {len(uniq)} distinct cards")
print(f"  full renders : {r_ok}")
print(f"  artwork only : {a_ok}   (render unavailable for these)")
print(f"  total covered: {have}/{len(uniq)}")
