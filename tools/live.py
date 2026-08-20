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
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402
from hsbg.tail import LOGS_DIR, PowerLogFollower  # noqa: E402

BUTTONS = {
    "TB_BaconShop_DragBuy": "BUY",
    "TB_BaconShop_DragBuy_Spell": "BUY SPELL",
    "TB_BaconShop_DragSell": "SELL",
    "TB_BaconShop_8p_Reroll_Button": "REROLL",
    "TB_BaconShopLockAll_Button": "FREEZE",
    "TB_BaconShopTechUp02_Button": "UPGRADE",
    "TB_BaconShopTechUp03_Button": "UPGRADE",
}

CLEAR, DIM, BOLD, CYAN, YELLOW, GREEN, RESET = (
    "\033[2J\033[H", "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[32m", "\033[0m"
)


def render(bg: BGState, session, last_action, connected, choice=None, flowing=False) -> str:
    mark = f"{GREEN}● live{RESET}" if flowing else f"{DIM}○ idle{RESET}"
    L = [f"{BOLD}{CYAN}  Battlegrounds — live state{RESET}   {mark}",
         f"{DIM}  {session.name if session else 'waiting for a game session...'}{RESET}", ""]
    if not connected:
        L += ["  No Power.log yet.", "",
              f"{DIM}  Start Hearthstone and enter a Battlegrounds match.{RESET}"]
        return "\n".join(L)

    L.append(f"  turn {BOLD}{bg.turn}{RESET}   tier {BOLD}{bg.tier}{RESET}   "
             f"gold {BOLD}{YELLOW}{bg.gold}{RESET}")
    if last_action:
        L.append(f"  {DIM}last action:{RESET} {GREEN}{last_action}{RESET}")
    L.append("")

    if choice is not None:
        kind = "hero pick" if choice.choice_type == "MULLIGAN" else "discover"
        L.append(f"{BOLD}{YELLOW}  {kind} — choose {choice.count_min}{RESET}")
        for i, r in enumerate(choice.entities, 1):
            L.append(f"    {i}. {r.name or r.card_id}  {DIM}{r.card_id}{RESET}")
        L.append("")

    L.append(f"{BOLD}  shop{RESET} ({len(bg.shop)})")
    for i, m in enumerate(bg.shop, 1):
        L.append(f"    {i}. {m}")
    if not bg.shop:
        L.append(f"    {DIM}(empty){RESET}")
    L.append("")

    L.append(f"{BOLD}  board{RESET} ({len(bg.board)})")
    for i, m in enumerate(bg.board, 1):
        L.append(f"    {i}. {m}")
    if not bg.board:
        L.append(f"    {DIM}(empty){RESET}")

    if bg.hand:
        L += ["", f"{BOLD}  hand{RESET} ({len(bg.hand)})"]
        L += [f"    · {m}" for m in bg.hand]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", type=Path, default=LOGS_DIR)
    ap.add_argument("--once", action="store_true", help="exit when the log stops growing")
    args = ap.parse_args()

    store = EntityStore()
    bg = BGState(store, CardDB())
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
                if isinstance(ev, P.ChoiceOffered):
                    choice = ev
                elif isinstance(ev, P.ChoiceMade):
                    choice = None
                if isinstance(ev, P.BlockStart):
                    label = BUTTONS.get(ev.ref.card_id or "")
                    now = time.monotonic()
                    # The same button block repeats in the log; collapse it.
                    if label and not (label == last_action and now - last_action_at < 1.0):
                        last_action, last_action_at = label, now
            if dirty:
                print(CLEAR + render(bg, follower.session, last_action, connected, choice, flowing),
                      flush=True)
                dirty = False
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
