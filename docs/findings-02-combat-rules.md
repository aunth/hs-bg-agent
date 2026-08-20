# Finding 02 — Battlegrounds combat resolution

Rules implemented in `src/hsbg/combat.py`. Resolution order follows
[twanvl/hearthstone-battlegrounds-simulator](https://github.com/twanvl/hearthstone-battlegrounds-simulator)
(**MIT licence**), which reverse-engineered them by testing against the live
game. Reading it saved rediscovering these the hard way.

## The rules that matter

1. **First attacker** — the side with more minions goes first; a tie is random.
2. **Attack order** — each board keeps a rotating `next_attacker` cursor.
   Minions attack left-to-right and wrap around. A minion with 0 attack is
   *skipped*, not merely passed over for its turn.
3. **Deaths resolve in two phases** — collect every dead minion and remove it
   from the board *first*, then run the deathrattles, looping while new deaths
   appear. Running each deathrattle as its minion dies produces different, wrong
   results whenever minions die simultaneously. This is the classic source of
   simulator disagreement.
4. **Counter-damage uses a snapshot** of the defender's attack taken before the
   exchange, since triggers can change it mid-attack.
5. **Divine Shield** absorbs a hit entirely and is consumed; poison does *not*
   apply through a shield.
6. **Battle ends** when both sides in succession have no minion able to attack.

## Why this was cheaper than expected

The current shop pool is only **275 minions**, of which 48 carry a deathrattle
needing hand-scripting and 27 are pure vanilla stats.

More importantly, the board state comes from the log with **live tags** —
`ATK`, `HEALTH`, `TAUNT`, `DIVINE_SHIELD`, `REBORN`, `PREMIUM` all arrive with
tavern buffs already applied. So keywords never have to be derived from card
text for the board being simulated; they are read directly.

## Validation

`tests/test_combat.py` covers nine cases with certain answers — mutual
destruction, clean wins and losses, empty boards, divine shield absorption,
poison trades, 0-attack minions being unable to attack, and outnumbering. All
pass.

The real validation is still to come: replaying combats from captured games and
checking our predicted result against what actually happened. The log records
both boards during combat, so it can serve as the test oracle.

## Known gaps

* `Battle.damage_dealt` is a stub. Battlegrounds damage is tavern tier plus the
  tiers of surviving minions; it currently returns a minion count.
* Deathrattle effects beyond Reborn are unimplemented — the registry
  (`DEATHRATTLES`) exists and is empty. This is the grind: 48 cards.
* Start-of-combat effects, auras, and attack triggers are not modelled.
