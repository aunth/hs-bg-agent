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
import signal
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
from hsbg.stats import BGStats  # noqa: E402
from hsbg.advisor import Advisor  # noqa: E402
from hsbg.art import ArtCache  # noqa: E402
from hsbg.tail import PowerLogFollower  # noqa: E402

PANEL_W, MARGIN = 330.0, 18.0
CARD_W, CARD_H, CARD_GAP = 72.0, 109.0, 6.0     # 256x388 rendered card, scaled
BOARDS, TITLE_H = 3, 19.0                       # builds listed, row height
ROWS_TOP = 58.0                                 # header is three lines
BOARD_MAX = 7
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
        self.stats = BGStats()
        self.advisor = Advisor(self.bg.cards, self.stats)
        self.art = ArtCache()
        self.choice = None
        self.action = None
        self.action_at = 0.0
        self.session = None
        self.last_data = 0.0

    def run(self):
        follower = PowerLogFollower()
        while True:
            try:
                events = follower.poll()
            except Exception as exc:            # never let the reader die silently
                print(f"reader error: {exc!r}", flush=True)
                time.sleep(1.0)
                continue
            if events:
                with self.lock:
                    for ev in events:
                        self.store.apply(ev)
                        if isinstance(ev, P.CreateGame):
                            self.choice, self.action = None, None
                            # Each lobby has its own tribe slate and mode.
                            self.advisor.tracker.reset()
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

    def targets(self, limit: int = BOARDS):
        """[(fit, [(card_id, role, owned)])] for the most plausible builds.

        Builds whose tribe is not in this lobby are dropped entirely -- they are
        not options, so showing them would be noise.
        """
        with self.lock:
            bg = self.bg
            if not self.stats.comps:
                return []
            owned = {m.card_id for m in bg.board} | {m.card_id for m in bg.hand}
            tribes: dict[str, int] = {}
            for m in bg.board:
                card = bg.cards.by_id(m.card_id) if bg.cards else None
                for r in (card.races if card else ()):
                    tribes[r] = tribes.get(r, 0) + 1
            fits = [f for f in self.advisor.comp_fits(list(owned), tribes, len(bg.board))
                    if f.available]
            order = {"CORE": 0, "ADDON": 1, "CYCLE": 2}
            out = []
            for fit in fits[:limit]:
                cards = sorted(fit.comp.cards.items(), key=lambda kv: order.get(kv[1], 3))
                out.append((fit, [(cid, role, cid in owned)
                                  for cid, role in cards[:BOARD_MAX]]))
            return out

    def lines(self):
        """(text, style) pairs for the panel."""
        with self.lock:
            bg, choice = self.bg, self.choice
            age = time.time() - self.last_data if self.last_data else None
            live = age is not None and age < 3.0
            # Beyond this the board on screen is history, not the live game.
            stale = age is None or age > 45
            action = self.action if (time.time() - self.action_at) < 6.0 else None
            def ago(sec):
                return f"{int(sec)}s" if sec < 90 else f"{int(sec // 60)}m"

            flag = "● live" if live else (f"○ {ago(age)} old" if age else "○ waiting")
            out = [(f"BATTLEGROUNDS   {flag}", "head")]
            if not self.session:
                out.append(("waiting for a match…", "dim"))
                return out
            if stale:
                # Say plainly that this is not current, and why we think so.
                out.append(("", "dim"))
                out.append(("NO LIVE DATA", "warn"))
                out.append((f"  last log write {ago(age)} ago" if age else "  no log yet", "dim"))
                out.append(("  board below is the last game,", "dim"))
                out.append(("  not the current one.", "dim"))
                out.append(("  if you are mid-game, Hearthstone", "dim"))
                out.append(("  has stopped writing logs —", "dim"))
                out.append(("  restart the client.", "dim"))
            out.append((f"turn {bg.turn}    tier {bg.tier}    gold {bg.gold}", "stat"))
            if action:
                out.append((f"↳ {action}", "dim"))
            if choice is not None:
                kind = "HERO PICK" if choice.choice_type == "MULLIGAN" else "DISCOVER"
                out.append(("", "dim"))
                out.append((kind, "sub"))
                ranked = []
                for r in choice.entities:
                    nm = (bg.cards.name(r.card_id) if bg.cards else "") or r.name or r.card_id
                    hs = self.stats.hero(r.card_id)
                    ranked.append((hs.avg_position if hs else 99.0, nm, hs))
                best = min((p for p, _, _ in ranked), default=99.0)
                for i, (pos, nm, hs) in enumerate(sorted(ranked), 1):
                    tick = "★ " if hs and pos == best else "  "
                    out.append((f"{tick}{nm}", "good" if tick.strip() else "body"))
                    if hs:
                        out.append((f"     avg {hs.avg_position:.2f}   "
                                    f"picked {hs.pick_rate:.0f}%   {hs.games} games", "dim"))
                    else:
                        out.append(("     no stats", "dim"))
            out.append(("", "dim"))
            sugg, fits = self.advisor.evaluate_shop(bg)
            by_id = {s.card_id: s for s in sugg}
            best = max((s.score for s in sugg), default=0.0)
            out.append((f"SHOP ({len(bg.shop)})", "sub"))
            for i, m in enumerate(bg.shop, 1):
                sg = by_id.get(m.card_id)
                mark = "~" if m.frozen else " "
                score = sg.score if sg else 0.0
                top = sg and score == best and score >= 60
                style = "good" if top else ("body" if score >= 55 else "bad")
                label = f"{i}.{mark}{m.name}"
                out.append((f"{label:<24}{score:5.0f}" if sg else label, style))
                out.append((f"    {m.atk}/{m.health} t{m.tier} {m.tribe or '—'}", "dim"))
                if sg and sg.reasons:
                    for r in sg.reasons[:2]:
                        out.append((f"    · {r}", "dim"))
            if not bg.shop:
                out.append(("  (empty)", "dim"))
            out.append(("", "dim"))
            out.append((f"BOARD ({len(bg.board)})", "sub"))
            for i, m in enumerate(bg.board, 1):
                out.append((f"{i}. {m.name}  {m.atk}/{m.health}", "body"))
            if not bg.board:
                out.append(("  (empty)", "dim"))
            live_fits = [f for f in fits if f.affinity > 0][:3] if bg.shop else []
            if live_fits:
                out.append(("", "dim"))
                out.append(("BUILDING TOWARD", "sub"))
                for f in live_fits:
                    out.append((f"  {f.comp.name}  ({f.comp.power_level})", "body"))
                    out.append((f"    {f.describe()}", "dim"))
            return out


C = AppKit.NSColor
STYLES = {
    "head": (13.0, AppKit.NSFontWeightBold, C.colorWithRed_green_blue_alpha_(0.55, 0.85, 1.0, 1.0)),
    "stat": (14.0, AppKit.NSFontWeightSemibold, C.colorWithRed_green_blue_alpha_(1.0, 0.85, 0.35, 1.0)),
    "sub":  (11.0, AppKit.NSFontWeightBold, C.colorWithRed_green_blue_alpha_(0.6, 0.65, 0.72, 1.0)),
    "body": (13.0, AppKit.NSFontWeightRegular, C.colorWithRed_green_blue_alpha_(0.96, 0.96, 0.97, 1.0)),
    "dim":  (11.0, AppKit.NSFontWeightRegular, C.colorWithRed_green_blue_alpha_(0.58, 0.60, 0.66, 1.0)),
    "good": (13.0, AppKit.NSFontWeightSemibold, C.colorWithRed_green_blue_alpha_(0.45, 0.90, 0.55, 1.0)),
    "bad":  (13.0, AppKit.NSFontWeightRegular, C.colorWithRed_green_blue_alpha_(0.95, 0.50, 0.45, 1.0)),
    "warn": (12.0, AppKit.NSFontWeightBold, C.colorWithRed_green_blue_alpha_(1.0, 0.62, 0.25, 1.0)),
    "cardname": (9.0, AppKit.NSFontWeightMedium,
                 C.colorWithRed_green_blue_alpha_(0.90, 0.91, 0.94, 1.0)),
    "cardhave": (9.0, AppKit.NSFontWeightBold,
                 C.colorWithRed_green_blue_alpha_(0.45, 0.90, 0.55, 1.0)),
    "cardrole": (8.5, AppKit.NSFontWeightRegular,
                 C.colorWithRed_green_blue_alpha_(0.55, 0.58, 0.64, 1.0)),
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


class BoardView(AppKit.NSView):
    def initWithModel_(self, model):
        self = objc.super(BoardView, self).init()
        self.model = model
        self._images = {}
        self.hover_row = -1        # set by the timer from the cursor position
        return self

    def row_at(self, point) -> int:
        """Which build row a point in view coordinates falls on, or -1."""
        x, y = point
        if x < 8 or x > self.bounds().size.width - 8:
            return -1
        idx = int((y - ROWS_TOP) // TITLE_H)
        return idx if 0 <= idx < BOARDS else -1

    def isFlipped(self):
        return True

    def image_for(self, card_id):
        if card_id not in self._images:
            path = self.model.art.best(card_id, fetch=False)
            img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path)) if path else None
            self._images[card_id] = img
        return self._images[card_id]

    def drawRect_(self, rect):
        bounds = self.bounds()
        C.colorWithRed_green_blue_alpha_(0.05, 0.06, 0.09, 0.82).set()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 10, 10)
        path.fill()
        C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.10).set()
        path.setLineWidth_(1.0)
        path.stroke()

        def label(text, x, y, style):
            size, weight, color = STYLES[style]
            font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight)
            AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                (x, y), {AppKit.NSFontAttributeName: font,
                         AppKit.NSForegroundColorAttributeName: color})

        targets = self.model.targets()
        tracker = self.model.advisor.tracker
        # Two lines: the tribe list overflowed a single header at panel width.
        title = "BUILDS FOR THIS LOBBY"
        label(title, 14.0, 8.0, "head")
        if tracker.duos:
            label("DUOS — scores are solo-BG data, treat as rough",
                  20.0 + len(title) * 7.6, 9.0, "warn")
        if tracker.proven:
            # Name the minion that proved each tribe, so the call is checkable.
            parts = [f"{t.title()} ({tracker.witness.get(t, '?')})"
                     for t in sorted(tracker.proven)]
            if not tracker.complete:
                parts.append("+learning")
            half = (len(parts) + 1) // 2      # two lines: one line overflowed
            label("tribes: " + " · ".join(parts[:half]), 14.0, 23.0, "dim")
            if parts[half:]:
                label("        " + " · ".join(parts[half:]), 14.0, 37.0, "dim")
        else:
            label("tribes: learning from the shop…", 14.0, 23.0, "dim")
        if not targets:
            label("no viable build yet", 14.0, 42.0, "dim")
            return

        # One line per build; the cards appear for whichever one you point at.
        y = ROWS_TOP
        for i, (fit, cards) in enumerate(targets):
            hot = (i == self.hover_row)
            if hot:
                C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.09).set()
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    ((8.0, y - 3), (self.bounds().size.width - 16.0, TITLE_H)), 4, 4).fill()
            label(f"{'▸' if hot else ' '} {fit.comp.name} ({fit.comp.power_level})",
                  12.0, y, "good" if hot else "sub")
            label(fit.describe(), 210.0, y, "dim")
            y += TITLE_H

        if self.hover_row < 0 or self.hover_row >= len(targets):
            label("point at a build to see its cards", 14.0, y + 8.0, "dim")
            return

        fit, cards = targets[self.hover_row]
        top = y + 12.0
        x = 14.0
        if True:
            for card_id, role, owned in cards:
                img = self.image_for(card_id)
                frame = ((x, top), (CARD_W, CARD_H))
                if img:
                    # Cards you already hold stay bright; the rest are dimmed.
                    img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                        frame, AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver,
                        1.0 if owned else 0.40, True, None)
                else:
                    C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.06).set()
                    AppKit.NSBezierPath.bezierPathWithRect_(frame).fill()
                if owned:
                    C.colorWithRed_green_blue_alpha_(0.45, 0.90, 0.55, 0.95).set()
                    ring = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        frame, 4, 4)
                    ring.setLineWidth_(2.0)
                    ring.stroke()
                name = (self.model.bg.cards.name(card_id) if self.model.bg.cards else card_id)
                if len(name) > 12:
                    name = name[:11] + "…"
                label(name, x + 1, top + CARD_H + 3, "cardhave" if owned else "cardname")
                label("HAVE" if owned else role.title(), x + 1, top + CARD_H + 15,
                      "cardhave" if owned else "cardrole")
                x += CARD_W + CARD_GAP


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

        board_w = 14.0 * 2 + BOARD_MAX * (CARD_W + CARD_GAP)
        board_h = ROWS_TOP + BOARDS * TITLE_H + CARD_H + 46.0
        self.board_window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (board_w, board_h)), style, AppKit.NSBackingStoreBuffered, False
        )
        for w in (self.board_window,):
            w.setOpaque_(False)
            w.setBackgroundColor_(C.clearColor())
            w.setIgnoresMouseEvents_(True)
            w.setLevel_(AppKit.NSScreenSaverWindowLevel)
            w.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorStationary
            )
        self.board_view = BoardView.alloc().initWithModel_(model)
        self.board_window.setContentView_(self.board_view)
        self.board_window.orderFrontRegardless()
        self.board_w, self.board_h = board_w, board_h
        return self

    def toggle_hidden(self) -> None:
        self.hidden = not getattr(self, "hidden", False)

    def tick_(self, _timer):
        if getattr(self, "hidden", False):
            for w in (self.window, self.board_window):
                w.orderOut_(None)
            return
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
        # Target board sits along the bottom of the game window.
        bh = self.board_h
        if b:
            bx = b["X"] + MARGIN
            by = primary_h - (b["Y"] + b["Height"]) + MARGIN
        else:
            bx, by = MARGIN, MARGIN
        self.board_window.setFrame_display_(((bx, by), (self.board_w, bh)), True)
        # The panels are click-through, so mouse events never reach them --
        # poll the cursor instead. Reading the location needs no permission.
        loc = AppKit.NSEvent.mouseLocation()
        inside = (bx <= loc.x <= bx + self.board_w and by <= loc.y <= by + bh)
        self.board_view.hover_row = (
            self.board_view.row_at((loc.x - bx, by + bh - loc.y)) if inside else -1)
        self.board_window.orderFrontRegardless()
        self.window.orderFrontRegardless()
        self.board_view.setNeedsDisplay_(True)


def main():
    model = Model()
    threading.Thread(target=model.run, daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no dock icon
    overlay = Overlay.alloc().initWithModel_(model)
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, overlay, "tick:", None, True
    )
    # kill -USR1 <pid> toggles both panels; no global hotkey, which would need
    # Input Monitoring permission.
    signal.signal(signal.SIGUSR1, lambda *_: overlay.toggle_hidden())
    print("overlay running — Ctrl-C to quit")
    print("hide/show:  pkill -USR1 -f tools/overlay.py")
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
