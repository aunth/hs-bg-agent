#!/usr/bin/env python3
"""Floating in-game overlay for Battlegrounds state.

    .venv/bin/python tools/overlay.py

Draws a click-through panel on top of Hearthstone. Requires the game in
borderless-window mode (``Screenmanager Fullscreen mode = 1``, the default);
exclusive fullscreen puts the game in its own Space where nothing can draw over
it.

The log tells us *what* is in the shop and in what left-to-right order, never
where it sits on screen, so the panel lists the shop in order and you read it
against the cards. Quitting: Ctrl-C in the terminal that launched it.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import AppKit  # noqa: E402
import objc  # noqa: E402
import Quartz  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402

from hsbg import parser as P  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402
from hsbg.tail import PowerLogFollower  # noqa: E402

PANEL_W, MARGIN = 330.0, 18.0
BUTTONS = {
    "TB_BaconShop_DragBuy": "BUY", "TB_BaconShop_DragBuy_Spell": "BUY SPELL",
    "TB_BaconShop_DragSell": "SELL", "TB_BaconShop_8p_Reroll_Button": "REROLL",
    "TB_BaconShopLockAll_Button": "FREEZE",
    "TB_BaconShopTechUp02_Button": "UPGRADE", "TB_BaconShopTechUp03_Button": "UPGRADE",
}


def hearthstone_bounds():
    """Screen rect of the Hearthstone window, in Quartz (top-left origin) coords."""
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    best = None
    for w in info or []:
        if "Hearthstone" not in str(w.get("kCGWindowOwnerName", "")):
            continue
        b = w.get("kCGWindowBounds") or {}
        area = b.get("Width", 0) * b.get("Height", 0)
        if area > 100_000 and (best is None or area > best[0]):
            best = (area, b)
    return best[1] if best else None


class Model:
    """Shared state between the log-reading thread and the UI thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.store = EntityStore()
        self.bg = BGState(self.store, CardDB())
        self.choice = None
        self.action = None
        self.action_at = 0.0
        self.session = None
        self.last_data = 0.0

    def run(self):
        follower = PowerLogFollower()
        while True:
            events = follower.poll()
            if events:
                with self.lock:
                    for ev in events:
                        self.store.apply(ev)
                        if isinstance(ev, P.ChoiceOffered):
                            self.choice = ev
                        elif isinstance(ev, P.ChoiceMade):
                            self.choice = None
                        elif isinstance(ev, P.BlockStart):
                            label = BUTTONS.get(ev.ref.card_id or "")
                            if label:
                                self.action, self.action_at = label, time.time()
                    self.session = follower.session
                    self.last_data = time.time()
            time.sleep(0.12)

    def lines(self):
        """(text, style) pairs for the panel."""
        with self.lock:
            bg, choice = self.bg, self.choice
            live = (time.time() - self.last_data) < 3.0
            action = self.action if (time.time() - self.action_at) < 6.0 else None
            out = [(f"BATTLEGROUNDS   {'● live' if live else '○ idle'}", "head")]
            if not self.session:
                out.append(("waiting for a match…", "dim"))
                return out
            out.append((f"turn {bg.turn}    tier {bg.tier}    gold {bg.gold}", "stat"))
            if action:
                out.append((f"↳ {action}", "dim"))
            if choice is not None:
                kind = "HERO PICK" if choice.choice_type == "MULLIGAN" else "DISCOVER"
                out.append(("", "dim"))
                out.append((kind, "sub"))
                for i, r in enumerate(choice.entities, 1):
                    nm = (bg.cards.name(r.card_id) if bg.cards else "") or r.name or r.card_id
                    out.append((f"{i}. {nm}", "body"))
            out.append(("", "dim"))
            out.append((f"SHOP ({len(bg.shop)})", "sub"))
            for i, m in enumerate(bg.shop, 1):
                mark = "~" if m.frozen else " "
                out.append((f"{i}.{mark}{m.name}", "body"))
                out.append((f"    {m.atk}/{m.health}  t{m.tier}  {m.tribe or '—'}", "dim"))
            if not bg.shop:
                out.append(("  (empty)", "dim"))
            out.append(("", "dim"))
            out.append((f"BOARD ({len(bg.board)})", "sub"))
            for i, m in enumerate(bg.board, 1):
                out.append((f"{i}. {m.name}  {m.atk}/{m.health}", "body"))
            if not bg.board:
                out.append(("  (empty)", "dim"))
            return out


C = AppKit.NSColor
STYLES = {
    "head": (13.0, AppKit.NSFontWeightBold, C.colorWithRed_green_blue_alpha_(0.55, 0.85, 1.0, 1.0)),
    "stat": (14.0, AppKit.NSFontWeightSemibold, C.colorWithRed_green_blue_alpha_(1.0, 0.85, 0.35, 1.0)),
    "sub":  (11.0, AppKit.NSFontWeightBold, C.colorWithRed_green_blue_alpha_(0.6, 0.65, 0.72, 1.0)),
    "body": (13.0, AppKit.NSFontWeightRegular, C.colorWithRed_green_blue_alpha_(0.96, 0.96, 0.97, 1.0)),
    "dim":  (11.0, AppKit.NSFontWeightRegular, C.colorWithRed_green_blue_alpha_(0.58, 0.60, 0.66, 1.0)),
}


class PanelView(AppKit.NSView):
    def initWithModel_(self, model):
        self = objc.super(PanelView, self).init()
        self.model = model
        return self

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        bounds = self.bounds()
        C.colorWithRed_green_blue_alpha_(0.05, 0.06, 0.09, 0.82).set()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 10, 10)
        path.fill()
        C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.10).set()
        path.setLineWidth_(1.0)
        path.stroke()

        y = 12.0
        for text, style in self.model.lines():
            size, weight, color = STYLES[style]
            if not text:
                y += 6
                continue
            font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight)
            attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color}
            AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_((14.0, y), attrs)
            y += size + 5


class Overlay(AppKit.NSObject):
    def initWithModel_(self, model):
        self = objc.super(Overlay, self).init()
        self.model = model
        style = AppKit.NSWindowStyleMaskBorderless
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (PANEL_W, 400)), style, AppKit.NSBackingStoreBuffered, False
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(C.clearColor())
        self.window.setIgnoresMouseEvents_(True)          # clicks pass through to the game
        self.window.setLevel_(AppKit.NSScreenSaverWindowLevel)
        self.window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        self.view = PanelView.alloc().initWithModel_(model)
        self.window.setContentView_(self.view)
        self.window.orderFrontRegardless()
        return self

    def tick_(self, _timer):
        lines = self.model.lines()
        height = 24.0 + sum(6 if not t else STYLES[s][0] + 5 for t, s in lines)
        b = hearthstone_bounds()
        # Quartz measures y downward from the top-left of the PRIMARY display;
        # AppKit measures upward from its bottom-left. Converting via mainScreen
        # is wrong on a multi-monitor setup -- mainScreen is merely the focused
        # one, which may not be the primary.
        primary_h = AppKit.NSScreen.screens()[0].frame().size.height
        if b:
            x = b["X"] + MARGIN
            top_y = primary_h - b["Y"]          # AppKit y of the window's top edge
            y = top_y - MARGIN - height          # panel hangs below that edge
        else:
            x, y = MARGIN, MARGIN
        self.window.setFrame_display_(((x, y), (PANEL_W, height)), True)
        self.view.setNeedsDisplay_(True)


def main():
    model = Model()
    threading.Thread(target=model.run, daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no dock icon
    overlay = Overlay.alloc().initWithModel_(model)
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, overlay, "tick:", None, True
    )
    print("overlay running — Ctrl-C to quit")
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
