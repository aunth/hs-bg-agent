#!/bin/bash
# Probe the newest Hearthstone session's Power.log for Battlegrounds observability.
S="${1:-$(ls -1dt /Applications/Hearthstone/Logs/*/ | head -1)}"
P="$S/Power.log"
echo "SESSION: $S"
if [ ! -f "$P" ]; then echo "!! NO Power.log — logging not active. Was HS fully quit before log.config was written?"; exit 1; fi
echo "SIZE:   $(wc -c < "$P") bytes / $(wc -l < "$P") lines"

hdr(){ printf '\n===== %s =====\n' "$1"; }

hdr "1. STRUCTURAL EVENTS (proves verbose Power logging works)"
for k in FULL_ENTITY SHOW_ENTITY HIDE_ENTITY TAG_CHANGE BLOCK_START BLOCK_END CREATE_GAME META_DATA CHANGE_ENTITY; do
  printf '%-14s %s\n' "$k" "$(grep -c "$k" "$P")"
done

hdr "2. BATTLEGROUNDS MODE MARKERS"
grep -oE 'BACON[A-Z_]*|GAME_TYPE=[A-Z_]+|BG[0-9]+_[A-Za-z0-9_]+' "$P" | sort | uniq -c | sort -rn | head -25

hdr "3. GOLD / RESOURCES  (gold available + spent)"
grep -oE 'tag=(RESOURCES|RESOURCES_USED|TEMP_RESOURCES) value=[0-9]+' "$P" | sort | uniq -c | sort -rn | head -12

hdr "4. TAVERN TIER / UPGRADE / REROLL"
grep -oE 'tag=(PLAYER_TECH_LEVEL|TECH_LEVEL|BACON_TIER[A-Z_]*|[A-Z_]*REROLL[A-Z_]*|[A-Z_]*UPGRADE[A-Z_]*|[A-Z_]*TAVERN[A-Z_]*) value=[0-9]+' "$P" | sort | uniq -c | sort -rn | head -20

hdr "5. FREEZE"
grep -oE 'tag=[A-Z_]*FROZEN[A-Z_]* value=[0-9]+' "$P" | sort | uniq -c | sort -rn | head

hdr "6. ZONES SEEN (shop vs board vs hand separation)"
grep -oE 'zone=[A-Z]+' "$P" | sort | uniq -c | sort -rn

hdr "7. ZONE CHANGES (buy = SETASIDE/PLAY -> HAND, play = HAND -> PLAY, sell = PLAY -> GRAVEYARD)"
grep -oE 'tag=ZONE value=[A-Z]+' "$P" | sort | uniq -c | sort -rn

hdr "8. CARDTYPES (minion / hero / hero power / spell / battleground_* )"
grep -oE 'tag=CARDTYPE value=[A-Z_]+' "$P" | sort | uniq -c | sort -rn

hdr "9. DISCOVER / CHOICES  (excludes the COIN_MANA_GEM_FOR_CHOICE_CARDS false positive)"
grep -oE '(^|[^_])CHOICES?[A-Z_]*' "$P" | grep -vE 'COIN_MANA_GEM' | sort | uniq -c | sort -rn | head
echo "-- real choice blocks --"
grep -nE 'BLOCK_START.*CHOICE|SHOW_ENTITY.*Updating.*SETASIDE' "$P" | head -10
echo "-- discover-flagged entities --"
grep -oE 'BACON_IS_[A-Z_]*DISCOVER[A-Z_]*|BACON_TURNS_LEFT_TO_DISCOVER[A-Z_]*' "$P" | sort | uniq -c

hdr "9b. SHOP vs BOARD SPLIT (player id: you vs Bob)"
echo "-- entities in PLAY by owning player id --"
grep -oE 'zone=PLAY zonePos=[0-9]+ cardId=[A-Za-z0-9_]* player=[0-9]+' "$P" | grep -oE 'player=[0-9]+' | sort | uniq -c | sort -rn | head
echo "-- shop-pool minions --"
echo "IS_BACON_POOL_MINION: $(grep -c 'IS_BACON_POOL_MINION' "$P")"

hdr "9c. ACTION BUTTONS (each action emits a BLOCK_START on these)"
grep -oE 'cardId=TB_BaconShop[A-Za-z0-9_]*(Button|DragBuy|DragSell|DragBuy_Spell|CheckTriples)[A-Za-z0-9_]*' "$P" | sort | uniq -c | sort -rn | head

hdr "10. TRINKETS / QUESTS / ANOMALIES / BUDDIES"
grep -oiE '(trinket|quest|anomal|buddy)[A-Za-z_]*' "$P" | sort | uniq -c | sort -rn | head -20

hdr "11. TAVERN SPELLS"
grep -oE 'cardId=BG[0-9]+_[A-Za-z0-9_]*' "$P" | sort -u | head -40

hdr "12. BOARD POSITION (repositioning)"
grep -oE 'tag=ZONE_POSITION value=[0-9]+' "$P" | sort | uniq -c | sort -rn | head

hdr "13. COMBAT / OPPONENT VISIBILITY"
grep -oE 'tag=(NEXT_OPPONENT_PLAYER_ID|PLAYER_LEADERBOARD_PLACE|HEALTH|DAMAGE|ATK) value=[0-9]+' "$P" | sort | uniq -c | sort -rn | head -12

hdr "14. TURN STRUCTURE"
grep -oE 'tag=(TURN|STEP|NEXT_STEP|MULLIGAN_STATE|PLAYSTATE) value=[A-Z0-9_]+' "$P" | sort | uniq -c | sort -rn | head -20

hdr "15. RAW SAMPLE — first 40 lines"
head -40 "$P"
