#!/usr/bin/env python3
"""Replay a captured Power.log through the parser and print Battlegrounds state.

    tools/replay.py [path/to/Power.log] [--all]

Prints a snapshot whenever the shop composition changes, plus every action
button press. This is the test harness -- replaying a capture is the only way
to iterate on the parser without playing a 20-minute game each time.

Known rough edge: player-level tags (gold, tier) arrive keyed by battletag,
which never appears in a bracketed reference, so names are bound to players in
arrival order. Good enough to validate the pipeline, worth replacing with a
proper binding later.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hsbg import parser as P  # noqa: E402
from hsbg.carddb import CardDB  # noqa: E402
from hsbg.state import BGState, EntityStore  # noqa: E402

BUTTONS = {
    "TB_BaconShop_DragBuy": "BUY",
    "TB_BaconShop_DragBuy_Spell": "BUY-SPELL",
    "TB_BaconShop_DragSell": "SELL",
    "TB_BaconShop_8p_Reroll_Button": "REROLL",
    "TB_BaconShopLockAll_Button": "FREEZE",
}


def button_label(card_id: str) -> str | None:
    if card_id.startswith("TB_BaconShopTechUp"):
        return "UPGRADE"
    return BUTTONS.get(card_id)


def snapshot(bg: BGState) -> str:
    shop = ", ".join(str(m) for m in bg.shop) or "-"
    board = ", ".join(str(m) for m in bg.board) or "-"
    return (
        f"  turn {bg.turn:>2} | tier {bg.tier} | gold {bg.gold}\n"
        f"    shop  [{len(bg.shop)}]: {shop}\n"
        f"    board [{len(bg.board)}]: {board}"
    )


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else None
    if not path:
        caps = sorted((Path(__file__).resolve().parent.parent / "data/captures").glob("*/Power.log"))
        if not caps:
            print("no captures found; run tools/capture.sh first", file=sys.stderr)
            return 1
        path = caps[-1]

    store = EntityStore()
    bg = BGState(store, CardDB())
    last_shop = None
    heroes_shown = False
    n_events = 0

    for ev in P.parse_file(path):
        n_events += 1
        store.apply(ev)

        if isinstance(ev, P.BlockStart):
            label = button_label(ev.ref.card_id or "")
            if label:
                print(f"\n>> {label}")
                print(snapshot(bg))
            continue

        if not heroes_shown and len(bg.hero_offers) >= 2:
            offers = bg.hero_offers
            print("HERO OFFERS:")
            for h in offers:
                print(f"    {h.tag('_name') or h.card_id}  ({h.card_id})")
            heroes_shown = True

        key = tuple(m.id for m in bg.shop)
        if key and key != last_shop:
            last_shop = key
            print("\n-- shop changed --")
            print(snapshot(bg))

    print(f"\n[{n_events} events parsed from {path}]")
    me, bob = store.local_player, store.bob_player
    print(f"[local player_id={me.player_id if me else '?'}  "
          f"bob player_id={bob.player_id if bob else '?'}  "
          f"entities={len(store.entities)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
