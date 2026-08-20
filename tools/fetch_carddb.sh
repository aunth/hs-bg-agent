#!/bin/bash
# Download card metadata from HearthstoneJSON into data/cards.json (~10 MB).
# Re-run after a Hearthstone patch to pick up new cards.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
URL="https://api.hearthstonejson.com/v1/latest/enUS/cards.json"
echo "fetching $URL"
curl -sSL --max-time 180 -o "$ROOT/data/cards.json" "$URL"
echo "cards.json: $(wc -c < "$ROOT/data/cards.json") bytes"
