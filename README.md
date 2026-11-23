# hs-bg-agent

A Battlegrounds advisor: reads Hearthstone's local logs, works out what is in
your shop and what you could build, and shows it as an overlay on the game.

## Scope

**In scope** — read and reason:

```
Hearthstone -> Power.log -> parser -> BG state -> scoring -> overlay
```

Observing and advising, same category of tool as HDT or the Bacon trackers. The
human plays.

**Out of scope** — the OS-level input executor. Battlegrounds lobbies are seven
other real people, and there is no offline mode, so an agent that clicks for you
is taking something from them. The pipeline stops at "here is the recommended
line."

## Run it

```bash
tools/capture.sh                          # snapshot a session's logs
tools/replay.py [capture/Power.log]       # replay one, print state transitions
tools/live.py                             # live state in the terminal
.venv/bin/python tools/overlay.py         # live state as an in-game overlay
pkill -USR1 -f tools/overlay.py           # hide/show everything
pkill -USR2 -f tools/overlay.py           # hide/show just the builds panel
pkill -f tools/overlay.py                 # quit it
```

Data refresh (cached, safe to re-run):

```bash
tools/fetch_carddb.sh      # card metadata, ~10 MB, after each patch
tools/fetch_stats.py       # meta stats, weekly
tools/fetch_art.py         # card images for the comps
```

## Setup

Hearthstone only writes `Power.log` when `log.config` enables it:

```bash
cp docs/log.config ~/Library/Preferences/Blizzard/Hearthstone/log.config
```

Hearthstone must be **fully quit** first -- the file is read once at startup.
Delete it and restart to turn logging off again.

To check it is working, look at which files exist in the newest session under
`/Applications/Hearthstone/Logs/`. `Asset.log`, `LoadingScreen.log` and `Net.log`
present means the config was read. `Power.log` and `Zone.log` appear only once a
match starts. Do **not** rely on the `Failed to load config file:` line in
`Hearthstone.log` -- it appears even when logging works correctly.

The overlay needs a virtualenv with pyobjc:

```bash
python3 -m venv .venv && ./.venv/bin/pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz
```

## Layout

```
src/hsbg/parser.py    Power.log lines -> typed events
src/hsbg/state.py     entity store, and the Battlegrounds projection over it
src/hsbg/tail.py      follows the live log across game restarts
src/hsbg/carddb.py    cardId -> name, tribe, tier, text
src/hsbg/stats.py     meta statistics and composition data
src/hsbg/advisor.py   contextual scoring, build detection, tribe detection
src/hsbg/combat.py    combat simulator
src/hsbg/art.py       card images, cached
tools/                capture, replay, live view, overlay, data fetchers
docs/                 findings, log.config reference
```

## What works

Shop, board, hand, gold, tavern tier, hero offers, Discover prompts, freeze,
positioning, trinkets, triples, turn number, leaderboard placement. Contextual
scoring of shop minions. Build detection with per-lobby tribe filtering. An
in-game overlay with card images. A combat engine that passes its unit tests.

Verified against real captured games -- see `docs/findings-01-power-log.md` and
`docs/findings-02-combat-rules.md` for the log format and the combat rules.

## What does not

* **The combat simulator is not wired into the advisor.** Scores reason about
  card quality and build direction, never about whether you would win the fight.
* **Deathrattles are unimplemented** -- the registry exists and is empty (48
  cards in the pool have one). Reborn is handled.
* **`damage_dealt` is a stub**, returning a minion count rather than tier-based
  damage.
* **Duos is detected but unsupported.** All statistics are solo Battlegrounds;
  in a Duos game the numbers are quietly wrong, so the overlay says so.
* **No validation of the simulator against real combats.** The log records both
  boards and the actual result, so this is possible and is the next thing worth
  doing.
* Start-of-combat effects, auras and attack triggers are not modelled.

## Notes worth keeping

* Parse only `GameState.DebugPrintPower()`. Every power is logged twice; the
  `PowerTaskList` copy is the animation queue.
* Shop and board both live in `ZONE=PLAY`, told apart by the entity's `player`
  id. Bartender Bob is the player slot with a null `GameAccountId`; ids vary per
  game, so never hardcode them.
* The `TURN` tag counts combat phases too, so it runs at double the
  Battlegrounds turn. Gold is the check: `min(10, turn + 2)`.
* Discover arrives on `DebugPrintEntityChoices`, resolutions on
  `DebugPrintEntitiesChosen` -- different channels from `DebugPrintPower`.
* Only a *single*-tribe minion proves its tribe is in the lobby; dual-tribe
  cards are ambiguous evidence.
* State that lives only in a running process (the tribe slate, the Duos flag)
  needs resetting on `CREATE_GAME`. Replaying a capture always starts clean, so
  bugs of this kind are invisible to replay testing.
