# hs-bg-agent

Battlegrounds state reader and advisor, built on Hearthstone's local log output.

## Scope

**In scope** — the read + reason half of the pipeline:

```
Hearthstone -> Power.log -> parser -> normalized BG state -> evaluation -> recommendation
```

Reading logs, reconstructing shop/board/hand/gold/tier, and evaluating lines is the
same category of tool as HDT and the Bacon trackers: it observes and advises, and the
human plays.

**Out of scope** — the OS-level input executor. Battlegrounds lobbies are eight real
people; an agent that clicks for you is cheating them, not just an account risk.
The pipeline stops at "here is the recommended line."

## Layout

```
tools/     shell utilities for capturing and probing logs
src/       parser + state model + evaluator
data/      captured log sessions (gitignored)
docs/      notes, reference copies of config
```

## Setup

Hearthstone only writes Power.log when `log.config` enables it. A reference copy is
in `docs/log.config`. To install it:

    cp docs/log.config ~/Library/Preferences/Blizzard/Hearthstone/log.config

Hearthstone must be **fully quit** first — the file is read once at startup.
To disable logging again, delete that file and restart the game.

Verified against client 36.2.0.0 / data build 36201 on macOS.

## Workflow

    # after playing a Battlegrounds game
    ./tools/capture.sh              # snapshot the session's logs into data/captures/
    ./tools/probe_power.sh          # report what the log actually exposes

`capture.sh` matters because Hearthstone opens a new `Logs/Hearthstone_<timestamp>/`
directory on every launch, so an un-snapshotted Power.log is easy to lose.

## Status

Open question: whether current Battlegrounds logs reliably expose shop, board, hand,
gold, tavern tier, Discover options, hero powers, tavern spells, and trinkets.
`probe_power.sh` is the experiment that answers it. Nothing is built on top until
that comes back.

Known limitation regardless of outcome: opponents' boards are hidden between combats,
so tempo-vs-scaling evaluation has to work from lobby-level signals (tier, placement,
damage taken) rather than exact enemy boards.
