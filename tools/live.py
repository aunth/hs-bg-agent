#!/usr/bin/env python3
"""Live Battlegrounds state view.

    tools/live.py                 follow the running game
    tools/live.py --logs DIR      follow a different Logs/ directory (for testing)

Redraws whenever the state changes. Start it before or during a game; it waits
for a session to appear and re-attaches by itself when Hearthstone restarts.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg import parser as P  # noqa: E402
from hsbg.advisor import Advisor  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.i18n import explain_comp_fit, get_lang, set_lang, t  # noqa: E402
from hsbg.planner import TurnPlanner  # noqa: E402
from hsbg.state import BGState, EntityStore, OpponentTracker  # noqa: E402
from hsbg.stats import BGStats  # noqa: E402
from hsbg.tail import LOGS_DIR, PowerLogFollower  # noqa: E402

BUTTONS = {
    "TB_BaconShop_DragBuy": "BUY",
    "TB_BaconShop_DragBuy_Spell": "BUY SPELL",
    "TB_BaconShop_DragSell": "SELL",
    "TB_BaconShop_8p_Reroll_Button": "REROLL",
    "TB_BaconShopLockAll_Button": "FREEZE",
}


def button_label(card_id: str) -> str | None:
    if card_id.startswith("TB_BaconShopTechUp"):
        return "UPGRADE"
    return BUTTONS.get(card_id)

CLEAR, DIM, BOLD, CYAN, YELLOW, GREEN, RESET = (
    "\033[2J\033[H", "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[32m", "\033[0m"
)


def render(bg: BGState, session, last_action, connected, choice=None, flowing=False, plan=None) -> str:
    mark = f"{GREEN}{t('live')}{RESET}" if flowing else f"{DIM}{t('idle')}{RESET}"
    L = [f"{BOLD}{CYAN}  {t('app_title')} [{get_lang().upper()}]{RESET}   {mark}",
         f"{DIM}  {session.name if session else t('waiting_session')}{RESET}", ""]
    if not connected:
        L += [f"  {t('no_power_log')}", "",
              f"{DIM}  {t('start_hs_hint')}{RESET}"]
        return "\n".join(L)

    up = t("up_cost", cost=bg.upgrade_cost) if bg.upgrade_cost is not None else ""
    L.append(f"  {t('turn_stat', turn=f'{BOLD}{bg.turn}{RESET}', tier=f'{BOLD}{bg.tier}{RESET}', gold=f'{BOLD}{YELLOW}{bg.gold}{RESET}', up=up)}")
    if last_action:
        L.append(f"  {DIM}{t('last_action')}{RESET} {GREEN}{last_action}{RESET}")
    L.append("")

    if choice is not None:
        kind = t("hero_pick") if choice.choice_type == "MULLIGAN" else t("discover")
        L.append(f"{BOLD}{YELLOW}  {t('choose_n', kind=kind, n=choice.count_min)}{RESET}")
        for i, r in enumerate(choice.entities, 1):
            L.append(f"    {i}. {r.name or r.card_id}  {DIM}{r.card_id}{RESET}")
        L.append("")

    if plan is not None:
        L.append(f"{BOLD}{GREEN}  {t('turn_plan')}{RESET}")
        if plan.target_comp:
            tc = plan.target_comp.comp
            L.append(f"    {CYAN}{t('target_label', name=tc.name, power=tc.power_level)}{RESET}")
            for exp_line in explain_comp_fit(plan.target_comp, compact=False):
                L.append(f"      {DIM}{exp_line}{RESET}")
        for step in plan.format_steps():
            L.append(f"    {GREEN}{step}{RESET}")
        if plan.alternatives:
            alt = plan.alternatives[0]
            alt_txt = " -> ".join(a.describe() for a in alt.actions[:2]) or t("hold_turn")
            L.append(f"    {DIM}{t('alt_prefix', text=alt_txt).strip()}{RESET}")
        L.append("")

    L.append(f"{BOLD}  {t('shop_header', n=len(bg.shop))}{RESET}")
    for i, m in enumerate(bg.shop, 1):
        L.append(f"    {i}. {m}")
    if not bg.shop:
        L.append(f"  {DIM}{t('empty')}{RESET}")
    if bg.shop_spells:
        L.append("")
        L.append(f"{BOLD}  {t('spells_header', n=len(bg.shop_spells))}{RESET}")
        for sp in bg.shop_spells:
            L.append(f"    · {sp}")
    L.append("")

    L.append(f"{BOLD}  {t('board_header', n=len(bg.board))}{RESET}")
    for i, m in enumerate(bg.board, 1):
        L.append(f"    {i}. {m}")
    if not bg.board:
        L.append(f"  {DIM}{t('empty')}{RESET}")

    if bg.hand:
        L += ["", f"{BOLD}  {t('hand_header', n=len(bg.hand))}{RESET}"]
        L += [f"    · {m}" for m in bg.hand]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", type=Path, default=LOGS_DIR)
    ap.add_argument("--once", action="store_true", help="exit when the log stops growing")
    ap.add_argument("--lang", choices=("uk", "en"), default=None, help="UI language (uk or en)")
    args = ap.parse_args()
    if args.lang:
        set_lang(args.lang)

    store = EntityStore()
    cards = CardDB()
    stats = BGStats()
    bg = BGState(store, cards)
    advisor = Advisor(cards, stats)
    opponents = OpponentTracker()
    planner = TurnPlanner(advisor)
    follower = PowerLogFollower(args.logs)
    last_action, last_action_at, dirty, connected = None, 0.0, True, False
    choice = None
    idle_since = time.monotonic()
    last_data = 0.0

    print(CLEAR + render(bg, None, None, False, None, False), flush=True)
    try:
        while True:
            events = follower.poll()
            now = time.monotonic()
            if events:
                connected, idle_since = True, now
                last_data = now
            elif args.once and now - idle_since > 2.0:
                break

            flowing = (now - last_data) < 3.0
            if flowing != getattr(main, "_was_flowing", None):
                main._was_flowing, dirty = flowing, True

            for ev in events:
                store.apply(ev)
                dirty = True
                if isinstance(ev, P.CreateGame):
                    choice, last_action = None, None
                    advisor.tracker.reset()
                    advisor.combat_history.reset()
                    opponents.reset()
                elif isinstance(ev, P.ChoiceOffered):
                    choice = ev
                elif isinstance(ev, P.ChoiceMade):
                    choice = None
                if isinstance(ev, P.BlockStart):
                    label = button_label(ev.ref.card_id or "")
                    now = time.monotonic()
                    # The same button block repeats in the log; collapse it.
                    if label and not (label == last_action and now - last_action_at < 1.0):
                        last_action, last_action_at = label, now
            if dirty:
                advisor.observe(bg)
                if bg.in_combat:
                    opponents.observe_combat(bg)
                plan = (
                    planner.plan_bg(bg, enemy_board=opponents.expected_opponent_board(bg) or None)
                    if (connected and not bg.in_combat and choice is None and (bg.shop or bg.hand or bg.gold > 0))
                    else None
                )
                print(CLEAR + render(bg, follower.session, last_action, connected, choice, flowing, plan),
                      flush=True)
                dirty = False
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
