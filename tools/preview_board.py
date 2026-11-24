#!/usr/bin/env python3
"""Render the target-board panel to a PNG, without needing it on screen.

    .venv/bin/python tools/preview_board.py <capture/Power.log> [out.png]

Replays a captured game to build real state, then draws the same view the
overlay uses. Lets us check the rendering independently of whether the overlay
is actually visible over the game.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import AppKit  # noqa: E402

# Reuse Model / BoardView from the overlay verbatim rather than duplicating them.
_overlay = ROOT / "tools" / "overlay.py"
ns = {"__file__": str(_overlay), "__name__": "overlay_preview"}
exec(compile(_overlay.read_text(), str(_overlay), "exec"), ns)

from hsbg import parser as P  # noqa: E402

cap = sys.argv[1] if len(sys.argv) > 1 else None
out = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "target_board.png")

AppKit.NSApplication.sharedApplication()
model = ns["Model"]()
if cap:
    # Mirror the live path: the tribe tracker learns from each shop as it appears,
    # so replaying state alone is not enough.
    for ev in P.parse_file(cap):
        model.apply_event(ev)
        if model.bg.shop:
            model.advisor.observe(model.bg)
    model.session = Path(cap).parent
    model.last_data = 9e9    # suppress the stale banner for the preview

view = ns["BoardView"].alloc().initWithModel_(model)
view.hover_row = 0          # preview the first build expanded
view.hover_card = 0         # preview hovering the first card
snap = model.board_snapshot()
view.cached_snapshot = snap
w = 14.0 * 2 + ns["BOARD_COLS"] * (ns["CARD_W"] + ns["CARD_GAP"]) - ns["CARD_GAP"]
h = view.needed_height(snap)
view.setFrame_(((0, 0), (w, h)))

rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
Path(out).write_bytes(bytes(data))
print(f"wrote {out}  ({w:.0f}x{h:.0f})")
print("active tribes:", ", ".join(sorted(model.advisor.tracker.proven)) or "none yet")
for fit, cards in model.targets():
    print(f"  {fit.comp.name} ({fit.comp.power_level}) — {fit.describe()}")
