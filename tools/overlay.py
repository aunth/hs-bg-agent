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
import re
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
from hsbg.combat import simulate  # noqa: E402
from hsbg.state import BGState, EntityStore, OpponentTracker  # noqa: E402
from hsbg.stats import BGStats  # noqa: E402
from hsbg.advisor import Advisor  # noqa: E402
from hsbg.art import ArtCache  # noqa: E402
from hsbg.i18n import (  # noqa: E402
    explain_comp_fit,
    format_ago,
    get_lang,
    set_lang,
    t,
    toggle_lang,
    tr_fit_describe,
    tr_reason,
    tr_role,
    tr_tribe,
    tr_verdict,
)
from hsbg.planner import TurnPlanner  # noqa: E402
from hsbg.tail import PowerLogFollower  # noqa: E402

PANEL_W, MARGIN = 330.0, 18.0
CARD_W, CARD_H, CARD_GAP = 72.0, 109.0, 6.0     # 256x388 rendered card, scaled
CARD_ROW_H = CARD_H + 22.0                      # card height + role caption + row gap
BOARDS, TITLE_H = 3, 20.0                       # builds listed, row height
ROWS_TOP = 58.0                                 # header is three lines
BOARD_COLS = 8                                  # cards per row in the builds panel
BOARD_MAX = BOARD_COLS                          # alias for preview_board.py
INSPECTOR_H = 58.0                              # bottom card-hover inspector box
STALE_AFTER = 45.0                              # seconds of silence before data reads as history
_HTML_TAG = re.compile(r"<[^>]+>")


def _clean_card_text(raw: str) -> str:
    if not raw:
        return "No card text."
    txt = raw.replace("[x]", "").replace("\n", " ").replace("\r", " ")
    txt = _HTML_TAG.sub("", txt)
    return " ".join(txt.split()) or "No card text."
BUTTONS = {
    "TB_BaconShop_DragBuy": "BUY", "TB_BaconShop_DragBuy_Spell": "BUY SPELL",
    "TB_BaconShop_DragSell": "SELL", "TB_BaconShop_8p_Reroll_Button": "REROLL",
    "TB_BaconShopLockAll_Button": "FREEZE",
}


def button_label(card_id: str) -> str | None:
    if card_id.startswith("TB_BaconShopTechUp"):
        return "UPGRADE"
    return BUTTONS.get(card_id)


def _cover_source(image_size, frame_w: float, frame_h: float):
    """Centered crop of ``image_size`` matching the frame's aspect ratio.

    Used as the source rect for drawInRect_fromRect_..., so a differently-
    shaped source image (square bare art vs. tall framed render) fills the
    frame without being stretched out of proportion.
    """
    iw, ih = image_size.width, image_size.height
    if not iw or not ih:
        return AppKit.NSZeroRect
    img_aspect, frame_aspect = iw / ih, frame_w / frame_h
    if img_aspect > frame_aspect:
        w = ih * frame_aspect
        return ((iw - w) / 2.0, 0.0), (w, ih)
    h = iw / frame_aspect
    return (0.0, (ih - h) / 2.0), (iw, h)


_CACHED_HS_BOUNDS = None
_CACHED_HS_BOUNDS_AT = 0.0


def hearthstone_bounds():
    """Screen rect of the Hearthstone window, in Quartz (top-left origin) coords (cached 1.5s)."""
    global _CACHED_HS_BOUNDS, _CACHED_HS_BOUNDS_AT
    now = time.monotonic()
    if (now - _CACHED_HS_BOUNDS_AT) < 1.5:
        return _CACHED_HS_BOUNDS
    _CACHED_HS_BOUNDS_AT = now
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
    _CACHED_HS_BOUNDS = best[1] if best else None
    return _CACHED_HS_BOUNDS


class Model:
    """Shared state between the log-reading thread and the UI thread."""

    def __init__(self):
        self.lock = threading.RLock()
        self.store = EntityStore()
        self.bg = BGState(self.store, CardDB())
        self.stats = BGStats()
        self.advisor = Advisor(self.bg.cards, self.stats)
        self.opponents = OpponentTracker()
        self.planner = TurnPlanner(self.advisor)
        self.art = ArtCache()
        self.choice = None
        self.action = None
        self.action_at = 0.0
        self.session = None
        self.last_data = 0.0
        self.combat_sim = None           # (raw_turn, SimResult) for current/last fight
        self._sim_raw_turn = -1
        self._cached_plan = None
        self._plan_dirty = True
        self._cached_board_snap = None
        self._board_snap_dirty = True
        self.left_click_comps: list[tuple[int, str]] = []  # (line_index, comp_id) in left panel

    def apply_event(self, ev, catching_up: bool = False):
        """Apply a single parsed event under lock. Returns pending combat sim args if triggered."""
        self.store.apply(ev)
        self._plan_dirty = True
        self._board_snap_dirty = True
        if isinstance(ev, P.CreateGame):
            self.choice, self.action = None, None
            self.combat_sim, self._sim_raw_turn = None, -1
            self._cached_plan = None
            # Each lobby has its own tribe slate and mode.
            self.advisor.tracker.reset()
            self.advisor.combat_history.reset()
            self.opponents.reset()
            return None
        if isinstance(ev, P.ChoiceOffered):
            self.choice = ev
        elif isinstance(ev, P.ChoiceMade):
            self.choice = None
        elif isinstance(ev, P.BlockStart):
            label = button_label(ev.ref.card_id or "")
            if label:
                self.action, self.action_at = label, time.time()
                if label == "REROLL":
                    self.advisor.observe(self.bg)
            if not catching_up and ev.block_type == "ATTACK":
                raw_turn = self.bg.raw_turn
                if self.bg.in_combat and raw_turn != self._sim_raw_turn:
                    me, bob = self.store.local_player, self.store.bob_player
                    if me and bob:
                        mine = self.bg.combat_minions(me.player_id)
                        opp = self.bg.combat_minions(bob.player_id)
                        if mine and opp:
                            self.opponents.observe_combat(self.bg)
                            tier = max(1, self.bg.tier)
                            self._sim_raw_turn = raw_turn
                            return (raw_turn, mine, opp, tier)
        elif isinstance(ev, P.TagChange) and ev.ref.is_game and ev.tag in ("TURN", "VISUAL_TURN"):
            self.advisor.observe(self.bg)
            if not self.bg.visual_in_combat:
                self.combat_sim = None
        return None

    def run(self):
        from hsbg.tavern import snapshot_from_bg

        follower = PowerLogFollower()
        while True:
            try:
                events = follower.poll()
            except Exception as exc:            # never let the reader die silently
                print(f"reader error: {exc!r}", flush=True)
                time.sleep(1.0)
                continue

            pending_sim = None
            plan_inputs = None
            if events:
                catching_up = len(events) > 200
                # Process in chunks of 2000 and yield GIL so Cocoa UI thread never stalls
                chunk_size = 2000
                for start in range(0, len(events), chunk_size):
                    chunk = events[start : start + chunk_size]
                    is_tail_chunk = (start + chunk_size) >= len(events)
                    with self.lock:
                        for i, ev in enumerate(chunk):
                            skip_sim = catching_up and not (is_tail_chunk and i >= len(chunk) - 40)
                            sim_req = self.apply_event(ev, catching_up=skip_sim)
                            if sim_req is not None:
                                pending_sim = sim_req
                        if is_tail_chunk:
                            self.advisor.observe(self.bg)
                            self.session = follower.session
                            if follower.log_mtime:
                                self.last_data = follower.log_mtime
                    if not is_tail_chunk:
                        time.sleep(0.002)
            else:
                with self.lock:
                    if follower.log_mtime:
                        self.last_data = follower.log_mtime

            # Run Monte Carlo combat sim OUTSIDE self.lock
            if pending_sim is not None:
                raw_turn, mine, opp, tier = pending_sim
                sim_res = simulate(mine, opp, runs=250, tavern_tiers=(tier, tier))
                with self.lock:
                    if self._sim_raw_turn == raw_turn:
                        self.combat_sim = (raw_turn, sim_res)

            # Snapshot inputs for TurnPlanner under lock, then run planner OUTSIDE self.lock!
            with self.lock:
                if self._plan_dirty:
                    bg = self.bg
                    if (
                        not bg.in_combat
                        and self.choice is None
                        and (bg.shop or bg.hand or bg.gold > 0)
                    ):
                        plan_inputs = (
                            snapshot_from_bg(bg),
                            self.opponents.expected_opponent_board(bg) or None,
                        )
                    else:
                        self._cached_plan = None
                    self._plan_dirty = False

            if plan_inputs is not None:
                snap_in, enemy_in = plan_inputs
                new_plan = self.planner.plan(snap_in, enemy_board=enemy_in)
                with self.lock:
                    self._cached_plan = new_plan

            if self._board_snap_dirty:
                self.board_snapshot()

            time.sleep(0.10)

    def has_live_data(self) -> bool:
        """True once a match session has been seen and has builds/tribes to show.

        Do not hide the builds panel after 45s of log silence: if Hearthstone
        hits its 10MB log cap or a turn runs long, hiding the builds panel makes
        it impossible to inspect or lock builds.
        """
        with self.lock:
            if not self.session:
                return False
            return bool(self.bg.turn > 0 or self.advisor.tracker.proven or self.bg.shop or self.bg.board)

    def all_comp_entries_unlocked(self):
        """[(fit, [(card_id, role, owned)])] across all comps, for locking any build by comp_id."""
        bg = self.bg
        if not self.stats.comps:
            return []
        board_cids = [m.card_id for m in bg.board]
        owned = set(board_cids) | {m.card_id for m in bg.hand}
        tribes: dict[str, int] = {}
        for m in bg.board:
            card = bg.cards.by_id(m.card_id) if bg.cards else None
            for r in (card.races if card else ()):
                tribes[r] = tribes.get(r, 0) + 1
        fits = self.advisor.comp_fits(
            list(owned), tribes, len(bg.board), board_card_ids=board_cids
        )
        order = {"CORE": 0, "ADDON": 1, "CYCLE": 2}
        out = []
        for fit in fits:
            def _sort_key(kv):
                cid, role = kv
                c = bg.cards.by_id(cid) if bg.cards else None
                return (order.get(role, 3), c.tier if c else 9, c.name if c else cid)

            cards = sorted(fit.comp.cards.items(), key=_sort_key)
            out.append((fit, [(cid, role, cid in owned) for cid, role in cards]))
        return out

    def _targets_unlocked(self, limit: int = BOARDS):
        bg = self.bg
        if not self.stats.comps or not self.session:
            return []
        board_cids = [m.card_id for m in bg.board]
        owned = set(board_cids) | {m.card_id for m in bg.hand}
        tribes: dict[str, int] = {}
        for m in bg.board:
            card = bg.cards.by_id(m.card_id) if bg.cards else None
            for r in (card.races if card else ()):
                tribes[r] = tribes.get(r, 0) + 1
        all_fits = self.advisor.comp_fits(
            list(owned), tribes, len(bg.board), board_card_ids=board_cids
        )
        fits = [
            f
            for f in all_fits
            if f.availability == "active" or (f.availability != "absent" and f.affinity > 0)
        ]
        if not fits:
            fits = [f for f in all_fits if f.availability != "absent"]
        order = {"CORE": 0, "ADDON": 1, "CYCLE": 2}
        out = []
        for fit in fits[:limit]:
            def _sort_key(kv):
                cid, role = kv
                c = bg.cards.by_id(cid) if bg.cards else None
                return (order.get(role, 3), c.tier if c else 9, c.name if c else cid)

            cards = sorted(fit.comp.cards.items(), key=_sort_key)
            out.append((fit, [(cid, role, cid in owned)
                              for cid, role in cards]))
        return out

    def targets(self, limit: int = BOARDS):
        """[(fit, [(card_id, role, owned)])] for the most plausible builds.

        Only builds whose tribe is *confirmed* present (or need no tribe at
        all) are shown -- a merely "unconfirmed" tribe is not yet evidence for
        anything, so surfacing it is a guess dressed up as advice.
        """
        with self.lock:
            return self._targets_unlocked(limit)

    def board_snapshot(self, limit: int = BOARDS) -> dict:
        """Thread-safe cached snapshot for BoardView so drawRect_ never touches shared state unlocked."""
        with self.lock:
            if not self._board_snap_dirty and self._cached_board_snap is not None:
                return self._cached_board_snap
            t = self.advisor.tracker
            self._cached_board_snap = {
                "targets": self._targets_unlocked(limit),
                "all_entries": {fit.comp.comp_id: (fit, cards) for fit, cards in self.all_comp_entries_unlocked()},
                "proven": sorted(t.proven),
                "witness": dict(t.witness),
                "complete": t.complete,
                "duos": t.duos,
            }
            self._board_snap_dirty = False
            return self._cached_board_snap

    def _current_plan_unlocked(self):
        bg = self.bg
        if bg.visual_in_combat or self.choice is not None:
            return None
        if not bg.shop and not bg.hand and bg.gold <= 0:
            return None
        return self._cached_plan

    def shop_badges(self) -> dict:
        """Per-card and tavern-action (Upgrade / Reroll) floating badge data for Bob's tavern."""
        with self.lock:
            bg = self.bg
            if not self.session or bg.visual_in_combat or self.choice is not None or bg.turn <= 0:
                return {"cards": [], "actions": {}}
            sugg, _ = self.advisor.evaluate_shop(bg, observe=False)
            by_id = {s.card_id: s for s in sugg}
            spell_suggs = {s.card_id: s for s in self.advisor.evaluate_spells(bg)} if bg.shop_spells else {}

            affordable_card_scores = []
            buy_cost = bg.buy_cost or 3
            if bg.gold >= buy_cost:
                affordable_card_scores.extend(s.score for s in sugg)
            affordable_spell_scores = [
                spell_suggs[sp.card_id].score
                for sp in bg.shop_spells
                if sp.card_id in spell_suggs and bg.gold >= (sp.cost if sp.cost is not None else 1)
            ]
            all_card_scores = [s.score for s in sugg] + [s.score for s in spell_suggs.values()]
            best_card = max(all_card_scores, default=0.0)
            best_aff_spell = max(affordable_spell_scores, default=0.0)

            plan = self._current_plan_unlocked()
            actions = self.advisor.score_tavern_actions(
                bg,
                best_card_score=best_card,
                best_affordable_spell_score=best_aff_spell,
                plan=plan,
            )

            candidate_tops = list(affordable_card_scores) + list(affordable_spell_scores)
            for act in actions.values():
                if act.get("affordable"):
                    candidate_tops.append(act["score"])
            no_affordable = not candidate_tops
            if no_affordable:
                candidate_tops = list(all_card_scores)
            global_best = max(candidate_tops, default=0.0)

            for act in actions.values():
                act["top"] = bool(act.get("affordable") and act["score"] >= global_best - 0.01 and act["score"] >= 60.0)

            cards: list[dict] = []
            for m in bg.shop:
                sg = by_id.get(m.card_id)
                score = sg.score if sg else 50.0
                can_buy = bg.gold >= buy_cost
                top = bool(sg and (can_buy or no_affordable) and score >= global_best - 0.01 and score >= 60.0)
                cards.append({
                    "name": m.name,
                    "score": score,
                    "top": top,
                    "frozen": m.frozen,
                    "is_spell": False,
                })
            for sp in bg.shop_spells:
                sg = spell_suggs.get(sp.card_id)
                score = sg.score if sg else 50.0
                can_buy = bg.gold >= (sp.cost if sp.cost is not None else 1)
                top = bool(sg and (can_buy or no_affordable) and score >= global_best - 0.01 and score >= 60.0)
                cards.append({
                    "name": sp.name,
                    "score": score,
                    "top": top,
                    "frozen": sp.frozen,
                    "is_spell": True,
                })
            return {"cards": cards, "actions": actions}

    def lines(self):
        """(text, style) pairs for the panel. Pure read under lock."""
        with self.lock:
            bg, choice = self.bg, self.choice
            age = time.time() - self.last_data if self.last_data else None
            live = age is not None and age < 3.0
            stale = age is None or age > STALE_AFTER
            action = self.action if (time.time() - self.action_at) < 6.0 else None

            lang_badge = "[UA]" if get_lang() == "uk" else "[EN]"
            if live:
                flag = t("live")
            elif age:
                flag = f"○ {format_ago(age)}"
            else:
                flag = t("idle")
            out = [(f"{t('app_title')}  {flag}  {lang_badge}", "head")]
            if not self.session:
                out.append((t("waiting_match"), "dim"))
                return out
            if stale:
                out.append(("", "dim"))
                out.append((t("no_live_data"), "warn"))
                out.append((t("last_log_write", ago=format_ago(age)) if age else t("no_log_yet"), "dim"))
                out.append((t("stale_line_1"), "dim"))
                out.append((t("stale_line_2"), "dim"))
                out.append((t("stale_line_3"), "dim"))
                out.append((t("stale_line_4"), "dim"))
                out.append((t("stale_line_5"), "dim"))
            up = t("up_cost", cost=bg.upgrade_cost) if bg.upgrade_cost is not None else ""
            out.append((t("turn_stat", turn=bg.turn, tier=bg.tier, gold=bg.gold, up=up), "stat"))
            if action:
                out.append((f"↳ {action}", "dim"))
            if self.combat_sim is not None:
                _, sim = self.combat_sim
                style = "good" if sim.win_pct >= 55 else ("bad" if sim.loss_pct >= 55 else "stat")
                out.append((t("combat_odds", win=sim.win_pct, tie=sim.tie_pct, loss=sim.loss_pct), style))
            if choice is not None:
                is_hero = choice.choice_type == "MULLIGAN"
                kind = t("hero_pick") if is_hero else t("discover")
                out.append(("", "dim"))
                out.append((kind, "sub"))
                if is_hero:
                    ranked = []
                    for r in choice.entities:
                        nm = (bg.cards.name(r.card_id) if bg.cards else "") or r.name or r.card_id
                        hs = self.stats.hero(r.card_id)
                        ranked.append((hs.avg_position if hs else 99.0, nm, hs))
                    best = min((p for p, _, _ in ranked), default=99.0)
                    for pos, nm, hs in sorted(ranked):
                        tick = "★ " if hs and pos == best else "  "
                        out.append((f"{tick}{nm}", "good" if tick.strip() else "body"))
                        if hs:
                            out.append((t("hero_stats", avg=hs.avg_position, pick=hs.pick_rate, games=hs.games), "dim"))
                        else:
                            out.append((t("no_stats"), "dim"))
                else:
                    cids = [r.card_id or "" for r in choice.entities]
                    scored = self.advisor.score_choices(bg, cids)
                    pairs = list(zip(choice.entities, scored))
                    pairs.sort(key=lambda pair: -pair[1].score)
                    best_score = pairs[0][1].score if pairs else 0.0
                    for r, sg in pairs:
                        nm = sg.name or r.name or r.card_id or "?"
                        top = sg.score == best_score and sg.score >= 55
                        tick = "★ " if top else "  "
                        style = "good" if top else ("body" if sg.score >= 50 else "bad")
                        out.append((f"{tick}{nm:<22}{sg.score:5.0f}", style))
                        for reason in sg.reasons[:2]:
                            out.append((f"    · {tr_reason(reason)}", "dim"))
            plan = self._current_plan_unlocked()
            if plan is not None:
                out.append(("", "dim"))
                out.append((t("turn_plan"), "sub"))
                if plan.target_comp:
                    tc = plan.target_comp.comp
                    out.append((f"  -> {tc.name} ({tc.power_level})", "head"))
                for step in plan.format_steps():
                    txt = step if len(step) <= 36 else step[:35] + "…"
                    is_hold = step.startswith("Hold") or step.startswith("Утриматись")
                    style = "dim" if is_hold else "good"
                    out.append((f"  {txt}", style))
                if plan.alternatives:
                    alt = plan.alternatives[0]
                    alt_txt = " -> ".join(a.describe() for a in alt.actions[:2]) or t("hold_turn")
                    alt_line = t("alt_prefix", text=alt_txt)
                    out.append((alt_line if len(alt_line) <= 42 else alt_line[:41] + "…", "dim"))
            if bg.in_combat and bg.shop:
                out.append(("", "dim"))
                out.append((t("enemy_board_header", n=len(bg.shop)), "sub"))
                for i, m in enumerate(bg.shop, 1):
                    out.append((f"{i}. {m.name}  {m.atk}/{m.health}", "body"))
            _, fits = self.advisor.evaluate_shop(bg, observe=False)
            out.append(("", "dim"))
            out.append((t("board_header", n=len(bg.board)), "sub"))
            for i, m in enumerate(bg.board, 1):
                out.append((f"{i}. {m.name}  {m.atk}/{m.health}", "body"))
            if not bg.board:
                out.append((t("empty"), "dim"))
            click_comps: list[tuple[int, str]] = []
            locked_cid = getattr(self, "locked_comp_id", None)
            board_hidden = getattr(self, "board_hidden_flag", False)
            live_fits = ([f for f in fits if f.affinity > 0 and f.available][:3]
                         if (bg.shop and not bg.in_combat) else [])
            if live_fits:
                out.append(("", "dim"))
                hdr_idx = len(out)
                hdr_key = "building_toward_show" if board_hidden else "building_toward"
                out.append((t(hdr_key), "sub"))
                click_comps.append((hdr_idx, "__TOGGLE_BOARD__"))
                for f in live_fits:
                    is_lck = (locked_cid == f.comp.comp_id)
                    mark = "🔒 " if is_lck else "▸ "
                    line_idx = len(out)
                    out.append((f"  {mark}{f.comp.name}  ({f.comp.power_level})", "stat" if is_lck else "body"))
                    click_comps.append((line_idx, f.comp.comp_id))
                    line_idx2 = len(out)
                    out.append((f"    {tr_fit_describe(f.describe())}", "dim"))
                    click_comps.append((line_idx2, f.comp.comp_id))
            elif self.has_live_data():
                out.append(("", "dim"))
                hdr_idx = len(out)
                hdr_key = "building_toward_show" if board_hidden else "building_toward"
                out.append((t(hdr_key), "sub"))
                click_comps.append((hdr_idx, "__TOGGLE_BOARD__"))
            advice = self.advisor.pivot_check(bg, fits) if (bg.shop and not bg.in_combat) else None
            if advice:
                is_lck = (locked_cid == advice.comp.comp_id)
                mark = "🔒 " if is_lck else "▸ "
                out.append(("", "dim"))
                out.append((t("pivot_check"), "sub"))
                line_idx = len(out)
                out.append((f"  {mark}{advice.comp.name}  ({advice.comp.power_level})", "stat" if is_lck else "body"))
                click_comps.append((line_idx, advice.comp.comp_id))
                line_idx2 = len(out)
                out.append((f"    {tr_fit_describe(advice.describe())}", "dim"))
                click_comps.append((line_idx2, advice.comp.comp_id))
                out.append((f"    {tr_verdict(advice.verdict)}", "dim"))
            self.left_click_comps = click_comps
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
    "cardhover": (9.0, AppKit.NSFontWeightBold,
                  C.colorWithRed_green_blue_alpha_(0.55, 0.88, 1.0, 1.0)),
}


class ShopOverlayView(AppKit.NSView):
    """Draws floating score badges directly above Bob's tavern cards, Upgrade button, and Reroll button."""

    def initWithModel_(self, model):
        self = objc.super(ShopOverlayView, self).init()
        self.model = model
        self.cached_badges = {"cards": [], "actions": {}}
        self.slot_dx = 184.0
        return self

    def isFlipped(self):
        return True

    def _draw_pill(self, cx: float, py: float, text: str, score: float, top: bool, affordable: bool = True):
        pill_h = 28.0
        if not affordable:
            border = C.colorWithRed_green_blue_alpha_(0.45, 0.48, 0.54, 0.45)
            score_style = "dim"
            bw_line = 1.0
        elif top:
            border = C.colorWithRed_green_blue_alpha_(0.45, 0.95, 0.55, 0.95)
            score_style = "good"
            bw_line = 2.2
        elif score >= 55:
            border = C.colorWithRed_green_blue_alpha_(1.0, 0.85, 0.35, 0.70)
            score_style = "stat"
            bw_line = 1.4
        else:
            border = C.colorWithRed_green_blue_alpha_(0.95, 0.50, 0.45, 0.65)
            score_style = "bad"
            bw_line = 1.2

        _, wt1, col1 = STYLES[score_style]
        f1 = AppKit.NSFont.monospacedSystemFontOfSize_weight_(13.5, wt1)
        attrs = {AppKit.NSFontAttributeName: f1, AppKit.NSForegroundColorAttributeName: col1}
        ns_str = AppKit.NSString.stringWithString_(text)
        str_sz = ns_str.sizeWithAttributes_(attrs)
        pill_w = max(62.0, str_sz.width + 18.0)
        px = cx - pill_w / 2.0
        frame = ((px, py), (pill_w, pill_h))

        C.colorWithRed_green_blue_alpha_(0.05, 0.06, 0.09, 0.90).set()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(frame, 8, 8)
        path.fill()
        border.set()
        path.setLineWidth_(bw_line)
        path.stroke()

        tx = px + (pill_w - str_sz.width) / 2.0
        ty = py + (pill_h - str_sz.height) / 2.0
        ns_str.drawAtPoint_withAttributes_((tx, ty), attrs)

    def drawRect_(self, _rect):
        data = self.cached_badges
        if not data:
            return
        cards = data.get("cards", []) if isinstance(data, dict) else data
        actions = data.get("actions", {}) if isinstance(data, dict) else {}
        if not cards and not actions:
            return
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        cx_center = w * 0.503
        dx = self.slot_dx

        # 1. Tavern Upgrade badge (left of Bob's hero portrait)
        up = actions.get("upgrade")
        if up:
            prefix = "★ " if up.get("top") else ""
            txt = f"{prefix}{t('badge_upgrade')} {up['score']:.0f}"
            self._draw_pill(cx_center - 1.45 * dx, 6.0, txt, up["score"], up.get("top", False), up.get("affordable", True))

        # 2. Reroll badge (right of Bob's hero portrait)
        rr = actions.get("reroll")
        if rr:
            prefix = "★ " if rr.get("top") else ""
            txt = f"{prefix}{t('badge_reroll')} {rr['score']:.0f}"
            self._draw_pill(cx_center + 1.25 * dx, 6.0, txt, rr["score"], rr.get("top", False), rr.get("affordable", True))

        # 3. Per-card score badges above Bob's tavern row (clear of card art)
        n = len(cards)
        card_py = max(42.0, h - 32.0)
        for i, b in enumerate(cards):
            cx = cx_center + (i - (n - 1) / 2.0) * dx
            score = b["score"]
            top = b["top"]
            prefix = "★ " if top else ""
            self._draw_pill(cx, card_py, f"{prefix}{score:.0f}", score, top, True)


class PanelView(AppKit.NSView):
    def initWithModel_(self, model):
        self = objc.super(PanelView, self).init()
        self.model = model
        self.cached_lines = None
        self.line_y_ranges: dict[int, tuple[float, float]] = {}
        return self

    def isFlipped(self):
        return True

    def comp_at_y(self, y: float) -> str | None:
        for line_idx, comp_id in getattr(self.model, "left_click_comps", ()):
            rng = self.line_y_ranges.get(line_idx)
            if rng and (rng[0] - 2.0) <= y <= (rng[1] + 2.0):
                return comp_id
        return None

    def drawRect_(self, rect):
        bounds = self.bounds()
        C.colorWithRed_green_blue_alpha_(0.05, 0.06, 0.09, 0.82).set()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 10, 10)
        path.fill()
        C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.10).set()
        path.setLineWidth_(1.0)
        path.stroke()

        y = 12.0
        lines = self.cached_lines if self.cached_lines is not None else self.model.lines()
        y_ranges: dict[int, tuple[float, float]] = {}
        for idx, (text, style) in enumerate(lines):
            size, weight, color = STYLES[style]
            if not text:
                y += 6
                continue
            y_ranges[idx] = (y, y + size + 5)
            font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight)
            attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color}
            AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_((14.0, y), attrs)
            y += size + 5
        self.line_y_ranges = y_ranges


class BoardView(AppKit.NSView):
    def initWithModel_(self, model):
        self = objc.super(BoardView, self).init()
        self.model = model
        self._images = {}
        self.hover_row = -1        # set by the timer from the cursor position
        self.hover_card = -1       # index of hovered card within active build
        self.locked_comp_id = None # comp_id when user clicks a build to lock it
        self.locked_entry = None   # cached (fit, cards) so locked build never disappears
        self.last_toggle_at = 0.0
        self.cached_snapshot = None
        return self

    def acceptsFirstMouse_(self, _event):
        return True

    def _active_targets(self, snap: dict) -> list:
        targets = list(snap.get("targets") or [])
        all_entries = snap.get("all_entries") or {}
        if self.locked_comp_id:
            if self.locked_comp_id in all_entries:
                self.locked_entry = all_entries[self.locked_comp_id]
            found = any(e[0].comp.comp_id == self.locked_comp_id for e in targets)
            if not found and self.locked_entry is not None:
                targets = [self.locked_entry] + targets[: max(0, BOARDS - 1)]
        return targets

    def toggle_lock_comp_id(self, comp_id: str, snap: dict) -> bool:
        now = time.monotonic()
        if (now - self.last_toggle_at) < 0.20:
            return False
        self.last_toggle_at = now
        all_entries = snap.get("all_entries") or {}
        if self.locked_comp_id == comp_id:
            self.locked_comp_id = None
            self.locked_entry = None
        else:
            self.locked_comp_id = comp_id
            self.locked_entry = all_entries.get(comp_id)
        self.model.locked_comp_id = self.locked_comp_id
        self.hover_card = -1
        self.setNeedsDisplay_(True)
        return True

    def active_row(self, targets: list) -> int:
        """Row index currently displayed (locked build takes precedence over hover)."""
        if self.locked_comp_id:
            for i, (fit, _) in enumerate(targets):
                if fit.comp.comp_id == self.locked_comp_id:
                    return i
        if 0 <= self.hover_row < len(targets):
            return self.hover_row
        return -1

    def cards_top_y(self, num_targets: int) -> float:
        rows = max(1, min(BOARDS, num_targets))
        return ROWS_TOP + rows * TITLE_H + 10.0

    def needed_size(self, snap: dict, collapsed: bool = False) -> tuple[float, float]:
        full_w = 14.0 * 2 + BOARD_COLS * (CARD_W + CARD_GAP) - CARD_GAP
        if collapsed:
            return (110.0, 30.0)
        return (full_w, self.needed_height(snap))

    def needed_height(self, snap: dict) -> float:
        targets = self._active_targets(snap)
        if not targets:
            return ROWS_TOP + 28.0
        act = self.active_row(targets)
        top = self.cards_top_y(len(targets))
        if act < 0 or act >= len(targets):
            # Compact when no build is hovered/locked: hide cards & explanation completely
            return top
        _, cards = targets[act]
        num_rows = max(1, (len(cards) + BOARD_COLS - 1) // BOARD_COLS)
        return top + num_rows * CARD_ROW_H + INSPECTOR_H + 14.0

    def row_at(self, point, num_targets: int = BOARDS) -> int:
        """Which build row a point in view coordinates falls on, or -1."""
        x, y = point
        if x < 8 or x > self.bounds().size.width - 8:
            return -1
        top = ROWS_TOP - 3.0
        if y < top:
            return -1
        idx = int((y - top) // TITLE_H)
        limit = min(BOARDS, num_targets)
        return idx if 0 <= idx < limit else -1

    def card_at(self, point, num_targets: int, num_cards: int) -> int:
        """Which card index a point in view coordinates falls on, or -1."""
        if num_cards <= 0:
            return -1
        x, y = point
        top = self.cards_top_y(num_targets)
        if y < top or x < 14.0:
            return -1
        row = int((y - top) // CARD_ROW_H)
        rel_y = (y - top) - row * CARD_ROW_H
        if rel_y > CARD_H + 16.0:
            return -1
        col = int((x - 14.0) // (CARD_W + CARD_GAP))
        rel_x = (x - 14.0) - col * (CARD_W + CARD_GAP)
        if col < 0 or col >= BOARD_COLS or rel_x > CARD_W:
            return -1
        idx = row * BOARD_COLS + col
        return idx if 0 <= idx < num_cards else -1

    def toggle_lock_at_row(self, row: int, targets: list) -> bool:
        """Click handler for build row: lock it, or unlock if already locked."""
        if row < 0 or row >= len(targets):
            return False
        fit, cards = targets[row]
        snap = self.cached_snapshot if self.cached_snapshot is not None else self.model.board_snapshot()
        self.locked_entry = (fit, cards)
        return self.toggle_lock_comp_id(fit.comp.comp_id, snap)

    def mouseDown_(self, event):
        snap = self.cached_snapshot if self.cached_snapshot is not None else self.model.board_snapshot()
        targets = self._active_targets(snap)
        pt = self.convertPoint_fromView_(event.locationInWindow(), None)
        row = self.row_at((pt.x, pt.y), len(targets))
        if row >= 0:
            self.toggle_lock_at_row(row, targets)
        elif self.locked_comp_id and pt.y < ROWS_TOP:
            # Clicking the header while locked also unlocks.
            self.locked_comp_id = None
            self.locked_entry = None
            self.hover_card = -1
            self.setNeedsDisplay_(True)

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
        C.colorWithRed_green_blue_alpha_(0.05, 0.06, 0.09, 0.88).set()
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 10, 10)
        path.fill()
        border_alpha = 0.35 if self.locked_comp_id else 0.12
        if self.locked_comp_id:
            C.colorWithRed_green_blue_alpha_(1.0, 0.85, 0.35, border_alpha).set()
        else:
            C.colorWithRed_green_blue_alpha_(1, 1, 1, border_alpha).set()
        path.setLineWidth_(1.5 if self.locked_comp_id else 1.0)
        path.stroke()

        def label(text, x, y, style):
            size, weight, color = STYLES[style]
            font = AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight)
            AppKit.NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                (x, y), {AppKit.NSFontAttributeName: font,
                         AppKit.NSForegroundColorAttributeName: color})

        snap = self.cached_snapshot if self.cached_snapshot is not None else self.model.board_snapshot()
        if getattr(self.model, "board_hidden_flag", False):
            label(t("builds_collapsed_pill"), 12.0, 7.0, "head")
            return
        targets = self._active_targets(snap)
        proven = snap["proven"]
        witness = snap["witness"]
        title = t("builds_lobby_title")
        label(title, 12.0, 8.0, "head")
        if self.locked_comp_id:
            label(t("locked_badge"), bounds.size.width - 28.0, 9.0, "stat")
        elif snap["duos"]:
            label(t("duos_warn"), 24.0 + len(title) * 8.4, 9.0, "warn")
        if proven:
            parts = [f"{tr_tribe(tr)} ({witness.get(tr, '?')})" for tr in proven]
            if not snap["complete"]:
                parts.append(t("learning_tag"))
            half = (len(parts) + 1) // 2
            pref = t("tribes_prefix")
            pad = " " * len(pref)
            label(pref + " · ".join(parts[:half]), 14.0, 23.0, "dim")
            if parts[half:]:
                label(pad + " · ".join(parts[half:]), 14.0, 37.0, "dim")
        if not targets:
            label(t("no_viable_build"), 14.0, 42.0, "dim")
            return

        act_row = self.active_row(targets)
        y = ROWS_TOP
        for i, (fit, cards) in enumerate(targets):
            is_locked = (self.locked_comp_id == fit.comp.comp_id)
            hot = (i == act_row)
            if is_locked:
                C.colorWithRed_green_blue_alpha_(1.0, 0.85, 0.35, 0.16).set()
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    ((8.0, y - 3), (bounds.size.width - 16.0, TITLE_H)), 4, 4).fill()
            elif hot:
                C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.09).set()
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    ((8.0, y - 3), (bounds.size.width - 16.0, TITLE_H)), 4, 4).fill()
            prefix = "🔒" if is_locked else ("▸" if hot else " ")
            row_style = "stat" if is_locked else ("good" if hot else "sub")
            cnt_lbl = t("cards_count", n=len(cards))
            label(f"{prefix} {fit.comp.name} ({fit.comp.power_level}) {cnt_lbl}",
                  12.0, y, row_style)
            label(tr_fit_describe(fit.describe()), 320.0, y, "dim")
            y += TITLE_H

        if act_row < 0 or act_row >= len(targets):
            return

        def draw_card(card_id, frame, owned, name, hovered):
            img = self.image_for(card_id)
            path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(frame, 6, 6)
            if img:
                ctx = AppKit.NSGraphicsContext.currentContext()
                ctx.saveGraphicsState()
                path.addClip()
                source = _cover_source(img.size(), CARD_W, CARD_H)
                alpha = 1.0 if (owned or hovered) else 0.50
                img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                    frame, source, AppKit.NSCompositingOperationSourceOver,
                    alpha, True,
                    {AppKit.NSImageHintInterpolation: AppKit.NSImageInterpolationHigh})
                ctx.restoreGraphicsState()
            else:
                C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.08 if hovered else 0.05).set()
                path.fill()

            (fx, fy), (fw, fh) = frame
            plate_h = 16.0
            C.colorWithRed_green_blue_alpha_(0.03, 0.03, 0.05, 0.88).set()
            AppKit.NSBezierPath.bezierPathWithRect_(
                ((fx, fy + fh - plate_h), (fw, plate_h))).fill()
            disp = name if len(name) <= 13 else name[:12] + "…"
            name_style = "cardhover" if hovered else ("cardhave" if owned else "cardname")
            label(disp, fx + 3, fy + fh - plate_h + 3, name_style)

            if hovered:
                border = C.colorWithRed_green_blue_alpha_(0.55, 0.88, 1.0, 1.0)
                width = 2.4
            elif owned:
                border = C.colorWithRed_green_blue_alpha_(0.45, 0.90, 0.55, 0.95)
                width = 2.0
            else:
                border = C.colorWithRed_green_blue_alpha_(0.55, 0.42, 0.22, 0.9)
                width = 1.2
            border.set()
            path.setLineWidth_(width)
            path.stroke()

        fit, cards = targets[act_row]
        cards_top = self.cards_top_y(len(targets))
        for idx, (card_id, role, owned) in enumerate(cards):
            row_idx = idx // BOARD_COLS
            col_idx = idx % BOARD_COLS
            x = 14.0 + col_idx * (CARD_W + CARD_GAP)
            top = cards_top + row_idx * CARD_ROW_H
            hovered = (idx == self.hover_card)
            frame = ((x, top), (CARD_W, CARD_H))
            card_obj = self.model.bg.cards.by_id(card_id) if self.model.bg.cards else None
            name = (card_obj.name if card_obj else "") or card_id
            draw_card(card_id, frame, owned, name, hovered)
            tier_prefix = f"T{card_obj.tier} " if (card_obj and card_obj.tier) else ""
            cap = f"{tier_prefix}{tr_role('HAVE' if owned else role)}"
            cap_style = "cardhover" if hovered else ("cardhave" if owned else "cardrole")
            label(cap, x + 1, top + CARD_H + 3, cap_style)

        # Bottom card inspector box
        num_rows = max(1, (len(cards) + BOARD_COLS - 1) // BOARD_COLS)
        insp_y = cards_top + num_rows * CARD_ROW_H + 2.0
        insp_rect = ((12.0, insp_y), (bounds.size.width - 24.0, INSPECTOR_H - 4.0))
        C.colorWithRed_green_blue_alpha_(0.08, 0.10, 0.15, 0.92).set()
        insp_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(insp_rect, 6, 6)
        insp_path.fill()
        C.colorWithRed_green_blue_alpha_(1, 1, 1, 0.12).set()
        insp_path.setLineWidth_(1.0)
        insp_path.stroke()

        if 0 <= self.hover_card < len(cards):
            h_cid, h_role, h_owned = cards[self.hover_card]
            c_obj = self.model.bg.cards.by_id(h_cid) if self.model.bg.cards else None
            c_name = (c_obj.name if c_obj else "") or h_cid
            c_tier = f"T{c_obj.tier}" if (c_obj and c_obj.tier) else t("spell_or_token")
            c_stats = f"{c_obj.attack}/{c_obj.health}" if (c_obj and c_obj.type == "MINION") else ""
            c_tribe = tr_tribe(c_obj.tribe) if (c_obj and c_obj.tribe) else tr_tribe("NEUTRAL")
            status_lbl = tr_role("HAVE" if h_owned else h_role)
            stat_obj = self.model.stats.card(h_cid)
            meta_lbl = "мета" if get_lang() == "uk" else "meta"
            avg_lbl = "сер" if get_lang() == "uk" else "avg"
            meta_str = f"  ·  {meta_lbl} {stat_obj.impact:+.2f} ({avg_lbl} #{stat_obj.avg_placement:.2f})" if stat_obj else ""
            header_bits = "  ·  ".join(b for b in (c_name, c_tier, c_stats, c_tribe, status_lbl) if b)
            label(header_bits + meta_str, 20.0, insp_y + 6.0, "good" if h_owned else "stat")
            clean_txt = _clean_card_text(c_obj.text if c_obj else "")
            if len(clean_txt) > 86:
                label(clean_txt[:86], 20.0, insp_y + 24.0, "body")
                label(clean_txt[86:172], 20.0, insp_y + 39.0, "dim")
            else:
                label(clean_txt, 20.0, insp_y + 25.0, "body")
        else:
            exp_full = explain_comp_fit(fit, compact=False)
            if exp_full:
                label(f"▸ {fit.comp.name} ({fit.comp.power_level}) — {exp_full[0]}", 20.0, insp_y + 7.0, "good")
                if len(exp_full) > 1:
                    label(f"  {exp_full[1]}", 20.0, insp_y + 24.0, "stat")
            hint = t("inspector_locked_hint") if self.locked_comp_id else t("inspector_ unlocked_hint")
            label(hint, 20.0, insp_y + 40.0, "dim")


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

        board_w = 14.0 * 2 + BOARD_COLS * (CARD_W + CARD_GAP) - CARD_GAP
        board_h = ROWS_TOP + BOARDS * TITLE_H + 34.0
        self.board_window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (board_w, board_h)), style, AppKit.NSBackingStoreBuffered, False
        )
        for w in (self.board_window,):
            w.setOpaque_(False)
            w.setBackgroundColor_(C.clearColor())
            # Must be True so macOS WindowServer never routes cursor ownership to Python
            # (which causes the macOS spinning beachball cursor); global mouse monitor still catches clicks.
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

        self.shop_window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (1600, 56)), style, AppKit.NSBackingStoreBuffered, False
        )
        self.shop_window.setOpaque_(False)
        self.shop_window.setBackgroundColor_(C.clearColor())
        self.shop_window.setIgnoresMouseEvents_(True)
        self.shop_window.setLevel_(AppKit.NSScreenSaverWindowLevel)
        self.shop_window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorStationary
        )
        self.shop_view = ShopOverlayView.alloc().initWithModel_(model)
        self.shop_window.setContentView_(self.shop_view)

        self._left_frame = ((0.0, 0.0), (PANEL_W, 400.0))
        self._board_frame = ((0.0, 0.0), (board_w, board_h))
        self._was_btn_down = False
        self._install_mouse_monitors()
        return self

    def _handle_global_click(self) -> None:
        if getattr(self, "hidden", False):
            return
        loc = AppKit.NSEvent.mouseLocation()
        # 1. Check click in the left panel (header language toggle [UA]/[EN] or build rows)
        (lx, ly), (lw, lh) = self._left_frame
        if lx <= loc.x <= lx + lw and ly <= loc.y <= ly + lh:
            flipped_y = (ly + lh) - loc.y
            if flipped_y <= 30.0:
                now = time.monotonic()
                if (now - getattr(self, "_last_lang_toggle", 0.0)) >= 0.25:
                    self._last_lang_toggle = now
                    toggle_lang()
                    self.model._plan_dirty = True
                return
            cid = self.view.comp_at_y(flipped_y)
            if cid == "__TOGGLE_BOARD__":
                self.toggle_board_hidden()
                return
            if cid:
                snap = self.board_view.cached_snapshot or self.model.board_snapshot()
                self.board_view.toggle_lock_comp_id(cid, snap)
                self.board_hidden = False
                self.model.board_hidden_flag = False
                return
        # 2. Check click on BUILDS FOR THIS LOBBY in the bottom panel
        (bx, by), (bw, bh) = self._board_frame
        if bx <= loc.x <= bx + bw and by <= loc.y <= by + bh:
            if getattr(self, "board_hidden", False):
                # Clicking the collapsed [▶ БІЛДИ] icon badge expands it back
                self.toggle_board_hidden()
                return
            local_pt = (loc.x - bx, (by + bh) - loc.y)
            snap = self.board_view.cached_snapshot or self.model.board_snapshot()
            targets = self.board_view._active_targets(snap)
            row = self.board_view.row_at(local_pt, len(targets))
            if row >= 0:
                self.board_view.toggle_lock_at_row(row, targets)
            elif local_pt[1] < ROWS_TOP:
                # Clicking the ▼ icon / title on the left collapses the builds panel
                self.toggle_board_hidden()

    def _install_mouse_monitors(self) -> None:
        def _on_global(_event):
            self._handle_global_click()

        def _on_local(event):
            self._handle_global_click()
            return event

        self._global_mon = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskLeftMouseDown, _on_global
        )
        self._local_mon = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskLeftMouseDown, _on_local
        )

    def toggle_hidden(self) -> None:
        self.hidden = not getattr(self, "hidden", False)

    def toggle_board_hidden(self) -> None:
        """Collapse/expand the builds panel into a small icon badge on the left."""
        now = time.monotonic()
        if (now - getattr(self, "_last_hide_toggle", 0.0)) < 0.25:
            return
        self._last_hide_toggle = now
        self.board_hidden = not getattr(self, "board_hidden", False)
        if self.board_hidden:
            self.board_view.locked_comp_id = None
            self.board_view.locked_entry = None
            self.board_view.hover_row = -1
            self.board_view.hover_card = -1
            self.model.locked_comp_id = None
        self.model.board_hidden_flag = self.board_hidden
        self.board_view.setNeedsDisplay_(True)

    def tick_(self, _timer):
        if getattr(self, "hidden", False):
            for w in (self.window, self.board_window, self.shop_window):
                w.orderOut_(None)
            return
        btn_down = bool(
            Quartz.CGEventSourceButtonState(
                Quartz.kCGEventSourceStateCombinedSessionState,
                Quartz.kCGMouseButtonLeft,
            )
        )
        if btn_down and not self._was_btn_down:
            self._handle_global_click()
        self._was_btn_down = btn_down
        lines = self.model.lines()
        self.view.cached_lines = lines
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
        self._left_frame = ((x, y), (PANEL_W, height))
        self.window.setFrame_display_(self._left_frame, True)
        self.view.setNeedsDisplay_(True)
        self.window.orderFrontRegardless()

        # Floating per-card & tavern action (Upgrade / Reroll) score badges
        badges = self.model.shop_badges()
        has_badges = bool(badges and (badges.get("cards") or badges.get("actions")))
        if has_badges and b:
            win_w, win_h = b["Width"], b["Height"]
            self.shop_view.cached_badges = badges
            self.shop_view.slot_dx = win_h * 0.1275
            sh_h = 84.0
            sx = b["X"]
            # Positioned above the top rim of tavern minion frames (~22.8% down from window top) so cards are never covered
            sy = primary_h - (b["Y"] + win_h * 0.228) - sh_h
            self.shop_window.setFrame_display_(((sx, sy), (win_w, sh_h)), True)
            self.shop_view.setNeedsDisplay_(True)
            self.shop_window.orderFrontRegardless()
        else:
            self.shop_window.orderOut_(None)

        show_board = self.model.has_live_data() or bool(self.board_view.locked_comp_id)
        if not show_board:
            self.board_window.orderOut_(None)
            return
        snap = self.model.board_snapshot()
        self.board_view.cached_snapshot = snap
        collapsed = getattr(self, "board_hidden", False)
        targets = self.board_view._active_targets(snap)
        bw, bh = self.board_view.needed_size(snap, collapsed=collapsed)
        if b:
            bx = b["X"] + MARGIN
            by = primary_h - (b["Y"] + b["Height"]) + MARGIN
        else:
            bx, by = MARGIN, MARGIN

        if not collapsed:
            loc = AppKit.NSEvent.mouseLocation()
            (lx, ly), (lw, lh) = self._left_frame
            in_left = (lx <= loc.x <= lx + lw and ly <= loc.y <= ly + lh)
            left_hovered_cid = None
            if in_left:
                cid = self.view.comp_at_y((ly + lh) - loc.y)
                if cid and cid != "__TOGGLE_BOARD__":
                    left_hovered_cid = cid

            inside = (bx <= loc.x <= bx + bw and by <= loc.y <= by + bh)
            if inside:
                local_pt = (loc.x - bx, by + bh - loc.y)
                hovered_title = self.board_view.row_at(local_pt, len(targets))
                if hovered_title >= 0 and not self.board_view.locked_comp_id:
                    self.board_view.hover_row = hovered_title
                    self.board_view.hover_card = -1
                act_row = self.board_view.active_row(targets)
                if 0 <= act_row < len(targets):
                    _, active_cards = targets[act_row]
                    self.board_view.hover_card = self.board_view.card_at(
                        local_pt, len(targets), len(active_cards)
                    )
                else:
                    self.board_view.hover_card = -1
            elif left_hovered_cid and not self.board_view.locked_comp_id:
                self.board_view.hover_card = -1
                matched_row = next(
                    (i for i, (f, _) in enumerate(targets) if f.comp.comp_id == left_hovered_cid),
                    -1,
                )
                self.board_view.hover_row = matched_row
            else:
                self.board_view.hover_card = -1
                if not self.board_view.locked_comp_id:
                    self.board_view.hover_row = -1
            bw, bh = self.board_view.needed_size(snap, collapsed=False)

        self.board_h = bh
        self._board_frame = ((bx, by), (bw, bh))
        self.board_window.setFrame_display_(self._board_frame, True)
        self.board_window.orderFrontRegardless()
        self.board_view.setNeedsDisplay_(True)


def main():
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in ("--lang", "-l") and i < len(sys.argv) - 1:
            set_lang(sys.argv[i + 1])
        elif arg.startswith("--lang="):
            set_lang(arg.split("=", 1)[1])
    model = Model()
    threading.Thread(target=model.run, daemon=True).start()

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no dock icon
    overlay = Overlay.alloc().initWithModel_(model)
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.08, overlay, "tick:", None, True
    )
    # kill -USR1/-USR2 <pid> toggle panels; no global hotkey, which would need
    # Input Monitoring permission.
    signal.signal(signal.SIGUSR1, lambda *_: overlay.toggle_hidden())
    signal.signal(signal.SIGUSR2, lambda *_: overlay.toggle_board_hidden())
    print("overlay running — Ctrl-C to quit")
    print("hide/show everything:   pkill -USR1 -f tools/overlay.py")
    print("hide/show builds panel: pkill -USR2 -f tools/overlay.py")
    try:
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
