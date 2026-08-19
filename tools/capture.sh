#!/bin/bash
# Snapshot the newest Hearthstone session's logs into data/captures/.
# Hearthstone starts a fresh Logs/Hearthstone_<timestamp>/ dir every launch and
# rewrites Power.log, so grab a copy before it is lost.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="${1:-$(ls -1dt /Applications/Hearthstone/Logs/*/ | head -1)}"
S="${S%/}"
NAME="$(basename "$S")"
LABEL="${2:-}"
DEST="$ROOT/data/captures/${NAME}${LABEL:+_$LABEL}"
mkdir -p "$DEST"
cp "$S"/*.log "$DEST"/ 2>/dev/null || true
echo "captured -> $DEST"
ls -la "$DEST"
