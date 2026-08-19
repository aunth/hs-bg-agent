# Finding 01 — Power.log exposes Battlegrounds state

Client 36.2.0.0 / data build 36201, macOS. Capture:
`data/captures/Hearthstone_2026_08_19_21_13_41` (~4 turns, 2.77 MB Power.log,
22,073 lines, 8,326 TAG_CHANGE, 519 BLOCK_START).

Verdict: **log-based state reading is viable.** Computer vision is not required.

## The key discovery: player id separates shop from board

Both the shop and your board live in `zone=PLAY`. They are told apart by the
`player` field on the entity:

- `player=4`  — you (the local player; `CONTROLLER value=4`)
- `player=12` — Bartender Bob / the shop (`CONTROLLER value=12`)

```
[entityName=Flittering Bat id=377 zone=PLAY zonePos=3 cardId=BG36_200 player=12]  <- shop offer
[entityName=Lesser Trinket id=59  zone=PLAY zonePos=0 cardId=BG30_Trinket_1st player=4]  <- yours
```

Shop minions additionally carry `tag=IS_BACON_POOL_MINION value=1`.
Note the player ids are per-game, not constants — read them from the roster that
`Zone.log` dumps at `CREATE_GAME` (it lists all 8 `playerInfos`).

## Every action has a named button entity

Each action appears as a `BLOCK_START` whose entity is a button card. This is what
makes action confirmation possible — you can verify an action landed by watching for
its block, rather than inferring from state diffs.

| Action | cardId |
|---|---|
| Buy minion | `TB_BaconShop_DragBuy` |
| Buy tavern spell | `TB_BaconShop_DragBuy_Spell` |
| Sell | `TB_BaconShop_DragSell` |
| Reroll | `TB_BaconShop_8p_Reroll_Button` |
| Freeze | `TB_BaconShopLockAll_Button` |
| Tavern upgrade | `TB_BaconShopTechUp02_Button`, `...TechUp03_Button` |
| Triple check | `TB_BaconShop_CheckTriples`, `TB_BaconShop_3ofKindChecke` |
| Bob | `TB_BaconShopBob` |

Freeze resolves cleanly — pressing `TB_BaconShopLockAll_Button` emits
`tag=FROZEN value=1` on each `player=12` shop minion inside a `SUB_SPELL_START` block.

## Checklist coverage

| Requirement | Status | Source |
|---|---|---|
| Shop | CONFIRMED | `zone=PLAY player=<bob>` + `IS_BACON_POOL_MINION` |
| Board | CONFIRMED | `zone=PLAY player=<you>` |
| Hand | CONFIRMED | `zone=HAND`, 374 refs |
| Gold | CONFIRMED | `RESOURCES` / `RESOURCES_USED` on player entity (by battletag) |
| Tavern tier | CONFIRMED | `PLAYER_TECH_LEVEL` (yours), `TECH_LEVEL` (per card), `BACON_MAX_PLAYER_TECH_LEVEL` |
| Freeze | CONFIRMED | `FROZEN` 0/1, 49 events |
| Repositioning | CONFIRMED | `ZONE_POSITION` 0-5 |
| Hero powers | CONFIRMED | `CARDTYPE=HERO_POWER` (35), e.g. `BG36_HERO_101p`, `TB_BaconShop_HP_086` |
| Tavern spells | CONFIRMED | `CARDTYPE=BATTLEGROUND_SPELL` (8), e.g. `BG28_503`, `BG28_966` |
| Trinkets | CONFIRMED | `CARDTYPE=BATTLEGROUND_TRINKET` (46); placeholders `BG30_Trinket_1st/2nd` spawn at game start carrying `BACON_TURNS_LEFT_TO_DISCOVER_TRINKET` (countdown), `BACON_IS_POTENTIAL_TRINKET`, `BACON_IS_MAGIC_ITEM_DISCOVER` |
| Triples | CONFIRMED | `BACON_TRIPLE_UPGRADE_MINION_ID` carries the dbId of the upgrade |
| Turn structure | CONFIRMED | `TURN`, `STEP`, `NEXT_STEP`, `PLAYSTATE` |
| Leaderboard | CONFIRMED | `PLAYER_LEADERBOARD_PLACE` |
| **Discover** | **UNCONFIRMED** | see below |

## The one gap: Discover

The probe's original `CHOICE_CARDS` hits were false positives — both were the
game-entity config tag `COIN_MANA_GEM_FOR_CHOICE_CARDS`, not an actual discover.
No real discover occurred in this capture: it ran only ~4 turns and the trinket
countdown still read `BACON_TURNS_LEFT_TO_DISCOVER_TRINKET value=5`.

This is unconfirmed, **not** absent. Hero selection worked without a CHOICES block
(the 4 candidate heroes were simply dealt into `zone=HAND` with `CARDTYPE=HERO`), so
Battlegrounds may represent discovers as SETASIDE entities rather than the constructed
-mode `CHOICES` power. Needs a second capture from a full-length game that includes a
trinket discover and a battlecry discover.

## Other notes

- `Zone.log` (3.79 MB) carries the same events in a friendlier form —
  `TRANSITIONING card [...] to FRIENDLY PLAY`, `zone from X -> FRIENDLY HAND` — and
  dumps the full 8-player lobby roster at game start. Useful cross-check for
  buy/sell/play detection.
- `RESOURCES` flickers 0 -> N as the client resyncs. Read gold as
  `RESOURCES - RESOURCES_USED` and debounce, do not trust a single TAG_CHANGE.
- Every power is logged twice: `GameState.DebugPrintPower()` (authoritative) and
  `PowerTaskList.DebugPrintPower()` (animation queue). **Parse GameState only**, or
  every event is double-counted.
- Numeric unnamed tags appear (`tag=937`, `tag=2955`, `tag=3639`). The client has no
  string name for these; some may carry BG data and would need reversing.

## Known limitation, unchanged

Opponents' boards are hidden between combats. Tempo-vs-scaling evaluation must work
from lobby-level signals (tier, placement, damage taken, `PLAYER_LEADERBOARD_PLACE`)
rather than exact enemy boards.
