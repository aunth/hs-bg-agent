"""macOS Quartz UI actuator for autonomous execution of TurnPlanner actions in Hearthstone.

Activated ONLY when `--autoplay` is passed (or toggled via `[AUTO: ON]` in the overlay header).
Operates in a strict closed loop:
1. Executes at most ONE atomic action (Buy, Play, Sell, Upgrade, Roll, Freeze, Hero Power, or Discover pick).
2. Waits for `Power.log` state confirmation (or cooldown timeout) before executing the next step.
3. Yields immediately for 3 seconds if manual human mouse movement is detected.
"""
from __future__ import annotations

import time
import Quartz

from .tavern import (
    BuyMinion,
    BuySpell,
    FreezeShop,
    PlayMinion,
    PlaySpell,
    ReorderBoard,
    RollShop,
    SellMinion,
    UpgradeTavern,
    UseHeroPower,
    choose_one_labels,
    select_card_target,
    snapshot_from_bg,
)


def _post_mouse(event_type: int, x: float, y: float, button: int = Quartz.kCGMouseButtonLeft) -> None:
    ev = Quartz.CGEventCreateMouseEvent(None, event_type, (float(x), float(y)), button)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def mouse_click(x: float, y: float, hold_s: float = 0.045) -> None:
    _post_mouse(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.03)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(hold_s)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x, y)


def mouse_drag(x0: float, y0: float, x1: float, y1: float, steps: int = 18, duration_s: float = 0.18) -> None:
    _post_mouse(Quartz.kCGEventMouseMoved, x0, y0)
    time.sleep(0.025)
    _post_mouse(Quartz.kCGEventLeftMouseDown, x0, y0)
    time.sleep(0.035)
    dt = duration_s / max(1, steps)
    for i in range(1, steps + 1):
        t = i / steps
        # Smooth ease-in-out curve (3t^2 - 2t^3) so cursor motion is fluid, not jerky
        s = t * t * (3.0 - 2.0 * t)
        cx = x0 + (x1 - x0) * s
        cy = y0 + (y1 - y0) * s
        _post_mouse(Quartz.kCGEventLeftMouseDragged, cx, cy)
        time.sleep(dt)
    time.sleep(0.035)
    _post_mouse(Quartz.kCGEventLeftMouseUp, x1, y1)


class AutoActuator:
    """Closed-loop execution driver translating TurnPlanner actions into Quartz mouse events."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled: bool = enabled
        self.last_action_at: float = 0.0
        self.last_state_sig: tuple | None = None
        self.last_state_change_at: float = 0.0
        self.last_cursor_pos: tuple[float, float] | None = None
        self.human_pause_until: float = 0.0
        self.status_text: str = ""
        self._play_attempts: dict[tuple[int, int], int] = {}

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        self.status_text = ""
        self._play_attempts.clear()
        return self.enabled

    def _slot_x(self, cx_center: float, slot_dx: float, idx: int, count: int) -> float:
        if count <= 1:
            return cx_center
        return cx_center + (idx - (count - 1) / 2.0) * slot_dx

    def _hand_xy(
        self,
        win_x: float,
        win_y: float,
        win_w: float,
        win_h: float,
        idx: int,
        count: int,
        expanded: bool = False,
    ) -> tuple[float, float]:
        hand_cx = win_x + win_w * 0.485
        if expanded:
            hand_y = win_y + win_h * 0.885
            if count <= 1:
                return (hand_cx, hand_y)
            dx = win_h * min(0.095, 0.38 / max(3, count - 1))
            return (hand_cx + (idx - (count - 1) / 2.0) * dx, hand_y)
        hand_y = win_y + win_h * 0.935
        if count <= 1:
            return (hand_cx, hand_y)
        if count == 2:
            dx = win_h * 0.068
        elif count == 3:
            dx = win_h * 0.056
        elif count == 4:
            dx = win_h * 0.046
        else:
            dx = win_h * (0.21 / max(4, count - 1))
        return (hand_cx + (idx - (count - 1) / 2.0) * dx, hand_y)

    def _find_board_target_idx(self, bg, target_name: str) -> int | None:
        if not target_name or not bg.board:
            return None
        clean = target_name.rstrip("*")
        for i, m in enumerate(bg.board):
            if m.name == target_name or m.name.rstrip("*") == clean:
                return i
        return None

    def step(self, model, bounds: dict) -> str | None:
        """Execute one step if autoplay is enabled and the game is ready. Returns description of executed action."""
        if not self.enabled or not bounds:
            return None
        now = time.time()
        if now < self.human_pause_until:
            return None
        # Cooldown between actions so animations and Power.log settle
        if (now - self.last_action_at) < 1.15:
            return None

        with model.lock:
            bg = model.bg
            choice = model.choice
            # Note: during Hero Selection (MULLIGAN) and start-of-game Discovers (e.g. Faelin),
            # bg.turn is 0, so allow `choice is not None` even when bg.turn == 0!
            if not model.session or bg.visual_in_combat or (bg.turn <= 0 and choice is None):
                return None
            plan = model._current_plan_unlocked()
            state_sig = (
                bg.turn,
                bg.tier,
                bg.gold,
                len(bg.board),
                len(bg.hand),
                len(bg.shop),
                len(bg.shop_spells),
                choice.choice_id if choice else -1,
            )

        if state_sig != self.last_state_sig:
            self.last_state_sig = state_sig
            self.last_state_change_at = now
        elif (now - self.last_action_at) < 2.0:
            # Wait up to 2.0s for the previous action to reflect in Power.log before retrying
            return None

        win_x = float(bounds["X"])
        win_y = float(bounds["Y"])
        win_w = float(bounds["Width"])
        win_h = float(bounds["Height"])
        cx_center = win_x + win_w * 0.503
        slot_dx = win_h * 0.1275

        shop_y = win_y + win_h * 0.325
        board_y = win_y + win_h * 0.545
        hero_y = win_y + win_h * 0.805
        bob_y = win_y + win_h * 0.155

        # 1. Handle active Discover or Hero Mulligan prompt
        if choice is not None and choice.entities:
            k = len(choice.entities)
            is_hero = choice.choice_type == "MULLIGAN"
            if is_hero:
                # Hero entities in DebugPrintEntityChoices arrive in reverse zonePos order (4, 3, 2, 1);
                # sort by zone_pos so screen index 0..k-1 matches left-to-right on screen!
                ordered_heroes = sorted(
                    enumerate(choice.entities),
                    key=lambda pair: (pair[1].zone_pos if pair[1].zone_pos else (pair[0] + 1)),
                )
                best_screen_idx = 0
                best_pos = 99.0
                best_name = ""
                for screen_idx, (_orig_i, r) in enumerate(ordered_heroes):
                    hs = model.stats.hero(r.card_id)
                    pos = hs.avg_position if hs else 99.0
                    if pos < best_pos:
                        best_pos = pos
                        best_screen_idx = screen_idx
                        best_name = r.name or r.card_id
                disc_dx = win_h * (0.23 if k <= 3 else 0.185)
                px = self._slot_x(cx_center, disc_dx, best_screen_idx, k)
                py = win_y + win_h * 0.455
                mouse_click(px, py)
                time.sleep(0.30)
                # Click Confirm button below the hero options
                mouse_click(cx_center, win_y + win_h * 0.785)
                self.last_action_at = time.time()
                self.status_text = f"Auto Hero: {best_name}"
                return self.status_text
            else:
                cids = [r.card_id or "" for r in choice.entities]
                scored = model.advisor.score_choices(bg, cids)
                best_idx = max(range(len(scored)), key=lambda i: scored[i].score)
                disc_dx = win_h * (0.24 if k <= 3 else 0.19)
                px = self._slot_x(cx_center, disc_dx, best_idx, k)
                py = win_y + win_h * 0.48
                mouse_click(px, py)
                self.last_action_at = time.time()
                self.status_text = f"Auto Discover: {scored[best_idx].name}"
                return self.status_text

        # 2. Execute the first action in the current TurnPlan
        if plan is None or not plan.actions:
            return None

        act = plan.actions[0]
        total_shop = len(bg.shop) + len(bg.shop_spells)

        if isinstance(act, BuyMinion):
            idx = next((i for i, m in enumerate(bg.shop) if m.id == act.uid or m.card_id == act.card_id), None)
            if idx is None:
                return None
            sx = self._slot_x(cx_center, slot_dx, idx, total_shop)
            mouse_drag(sx, shop_y, cx_center, hero_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, BuySpell):
            sp_idx = next((i for i, s in enumerate(bg.shop_spells) if s.id == act.uid or s.card_id == act.card_id), None)
            if sp_idx is None:
                return None
            slot_i = len(bg.shop) + sp_idx
            sx = self._slot_x(cx_center, slot_dx, slot_i, total_shop)
            mouse_drag(sx, shop_y, cx_center, hero_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, PlayMinion):
            h_idx = next((i for i, m in enumerate(bg.hand) if m.id == act.uid), None)
            if h_idx is None:
                h_idx = next(
                    (
                        i
                        for i, m in enumerate(bg.hand)
                        if m.card_id == act.card_id
                        and (bg.s.entities.get(m.id) is None or bg.s.entities[m.id].int_tag("LITERALLY_UNPLAYABLE") != 1)
                    ),
                    None,
                )
            if h_idx is None:
                return None
            target_hand_m = bg.hand[h_idx]
            att_key = (bg.turn, target_hand_m.id)
            tries = self._play_attempts.get(att_key, 0) + 1
            self._play_attempts[att_key] = tries
            if tries >= 3:
                # Card is locked/unplayable on the client (e.g. Faelin/dormant) -> mark unplayable in store and re-plan!
                with model.lock:
                    ent = bg.s.entities.get(target_hand_m.id)
                    if ent is not None:
                        ent.tags["LITERALLY_UNPLAYABLE"] = "1"
                    model._plan_dirty = True
                self.status_text = f"Skip locked: {target_hand_m.name}"
                return self.status_text

            # If multiple cards in hand (e.g. Faelin locked cards next to playable card),
            # hover hand first so the fan expands cleanly before grabbing the card
            if len(bg.hand) > 1 or tries == 2:
                _post_mouse(Quartz.kCGEventMouseMoved, win_x + win_w * 0.485, win_y + win_h * 0.94)
                time.sleep(0.24)
                hx, hy = self._hand_xy(win_x, win_y, win_w, win_h, h_idx, len(bg.hand), expanded=True)
            else:
                hx, hy = self._hand_xy(win_x, win_y, win_w, win_h, h_idx, len(bg.hand), expanded=False)

            drop_x = cx_center + max(0.0, (len(bg.board) - 1) * 0.35) * slot_dx
            mouse_drag(hx, hy, drop_x, board_y)

            # Handle Choose One option if applicable
            if act.option in (1, 2) and choose_one_labels(act.card_id):
                time.sleep(0.38)
                opt_x = cx_center + (-1.15 if act.option == 1 else 1.15) * slot_dx
                mouse_click(opt_x, win_y + win_h * 0.48)

            # Handle targeted Battlecry / Choose One target on board
            t_idx = self._find_board_target_idx(bg, act.target_name)
            if t_idx is not None:
                time.sleep(0.30)
                tx = self._slot_x(cx_center, slot_dx, t_idx, max(1, len(bg.board)))
                mouse_click(tx, board_y)

            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, PlaySpell):
            h_idx = next((i for i, m in enumerate(bg.hand) if m.id == act.uid or m.card_id == act.card_id), None)
            if h_idx is None:
                return None
            hx, hy = self._hand_xy(win_x, win_y, win_w, win_h, h_idx, len(bg.hand))
            t_idx = self._find_board_target_idx(bg, act.target_name)
            if t_idx is not None and bg.board:
                tx = self._slot_x(cx_center, slot_dx, t_idx, len(bg.board))
                mouse_drag(hx, hy, tx, board_y)
            else:
                mouse_drag(hx, hy, cx_center, board_y)

            if act.option in (1, 2) and choose_one_labels(act.card_id):
                time.sleep(0.35)
                opt_x = cx_center + (-1.15 if act.option == 1 else 1.15) * slot_dx
                mouse_click(opt_x, win_y + win_h * 0.48)
                if t_idx is not None and bg.board:
                    time.sleep(0.28)
                    tx = self._slot_x(cx_center, slot_dx, t_idx, len(bg.board))
                    mouse_click(tx, board_y)

            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, SellMinion):
            b_idx = next((i for i, m in enumerate(bg.board) if m.id == act.uid or m.card_id == act.card_id), None)
            if b_idx is None:
                return None
            bx = self._slot_x(cx_center, slot_dx, b_idx, len(bg.board))
            mouse_drag(bx, board_y, cx_center, bob_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, UpgradeTavern):
            up_x = cx_center - 1.24 * slot_dx
            up_y = win_y + win_h * 0.145
            mouse_click(up_x, up_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, RollShop):
            roll_x = cx_center + 1.08 * slot_dx
            roll_y = win_y + win_h * 0.145
            mouse_click(roll_x, roll_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, FreezeShop):
            freeze_x = cx_center + 1.95 * slot_dx
            freeze_y = win_y + win_h * 0.125
            mouse_click(freeze_x, freeze_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, UseHeroPower):
            hp_x = cx_center + 0.92 * slot_dx
            hp_y = win_y + win_h * 0.815
            t_idx = self._find_board_target_idx(bg, act.target_name)
            if t_idx is not None and bg.board:
                tx = self._slot_x(cx_center, slot_dx, t_idx, len(bg.board))
                mouse_drag(hp_x, hp_y, tx, board_y)
            else:
                mouse_click(hp_x, hp_y)
            self.last_action_at = time.time()
            self.status_text = f"Auto: {act.describe()}"
            return self.status_text

        if isinstance(act, ReorderBoard):
            # Skip ReorderBoard if already last step or execute first swap
            return None

        return None
