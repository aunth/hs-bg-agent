"""Battlegrounds combat simulator.

Resolution order follows twanvl's simulator (MIT licence,
github.com/twanvl/hearthstone-battlegrounds-simulator), which reverse-engineered
these rules by testing against the live game. The subtle parts, all of which
produce wrong results if done naively:

* The side with **more minions** attacks first; a tie is decided randomly.
* Each board keeps a rotating ``next_attacker`` cursor. Minions attack
  left-to-right, wrapping around, and any minion with 0 attack is skipped
  entirely rather than losing its turn.
* Deaths are resolved in **two phases**: every dead minion is collected and
  removed from the board first, and only then do their deathrattles run. The
  loop repeats while new deaths appear. Running each deathrattle as its minion
  dies gives different -- wrong -- results when minions die simultaneously.
* The defender's counter-damage uses a **snapshot** of its attack taken before
  the exchange, since triggers may change it mid-attack.
* Divine Shield absorbs a hit entirely and is consumed. Poison zeroes health
  only if the hit landed (a shielded hit does not poison).
* Battle ends when both sides in succession have no minion able to attack.

Board state comes from the log with live tags, so buffs and shields granted in
the tavern are already reflected -- no need to derive keywords from card text.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

BOARD_SIZE = 7


@dataclass
class CMinion:
    card_id: str = ""
    name: str = ""
    atk: int = 0
    health: int = 0
    taunt: bool = False
    divine_shield: bool = False
    poisonous: bool = False
    windfury: bool = False
    mega_windfury: bool = False
    reborn: bool = False
    golden: bool = False

    @property
    def dead(self) -> bool:
        return self.health <= 0

    def num_attacks(self) -> int:
        return 4 if self.mega_windfury else (2 if self.windfury else 1)

    def __str__(self) -> str:
        f = "".join(c for c, on in (("T", self.taunt), ("D", self.divine_shield),
                                    ("P", self.poisonous), ("W", self.windfury)) if on)
        return f"{self.name or self.card_id} {self.atk}/{self.health}{f and '[' + f + ']'}"


# card_id -> callable(battle, side, position, minion) run when the minion dies.
DEATHRATTLES: dict[str, callable] = {}


def deathrattle(card_id: str):
    def wrap(fn):
        DEATHRATTLES[card_id] = fn
        return fn
    return wrap


class Side:
    def __init__(self, minions: list[CMinion], hero_health: int = 30):
        self.minions = [replace(m) for m in minions]   # never mutate the caller's board
        self.next_attacker = 0
        self.hero_health = hero_health

    def alive(self) -> bool:
        return bool(self.minions)

    def find_attacker(self) -> int:
        """Next minion able to attack, skipping any with 0 attack."""
        n = len(self.minions)
        if n == 0:
            return -1
        for _ in range(n):
            if self.next_attacker >= n:
                self.next_attacker = 0
            idx = self.next_attacker
            if self.minions[idx].atk > 0:
                return idx
            self.next_attacker += 1
        return -1

    def targets(self) -> list[int]:
        taunts = [i for i, m in enumerate(self.minions) if m.taunt]
        return taunts or list(range(len(self.minions)))

    def tier_total(self) -> int:
        return sum(m.atk + m.health for m in self.minions)


class Battle:
    def __init__(self, friendly: list[CMinion], enemy: list[CMinion],
                 rng: random.Random | None = None):
        self.sides = [Side(friendly), Side(enemy)]
        self.rng = rng or random.Random()
        self.turn = -1

    # -- setup ----------------------------------------------------------
    def start(self) -> None:
        a, b = len(self.sides[0].minions), len(self.sides[1].minions)
        self.turn = 0 if a > b else (1 if b > a else self.rng.randrange(2))

    def done(self) -> bool:
        return not (self.sides[0].alive() and self.sides[1].alive())

    # -- combat ---------------------------------------------------------
    def run(self) -> tuple[int, int]:
        """Returns (winner, damage). winner is 0, 1, or -1 for a tie."""
        self.start()
        missed_prev = False
        for _ in range(2000):                      # guard against non-terminating boards
            if self.done():
                break
            ok = self.attack_round()
            if missed_prev and not ok:
                break                              # neither side can attack: stalemate
            missed_prev = not ok
        return self.outcome()

    def attack_round(self) -> bool:
        side = self.sides[self.turn]
        idx = side.find_attacker()
        if idx == -1:
            self.turn = 1 - self.turn
            return False
        attacker = side.minions[idx]
        for _ in range(attacker.num_attacks()):
            if idx >= len(side.minions) or side.minions[idx] is not attacker or attacker.dead:
                break
            self.single_attack(self.turn, idx)
        # Advance the cursor past this minion, whether or not it survived.
        side.next_attacker = (idx + 1) if (idx < len(side.minions)
                                           and side.minions[idx] is attacker) else idx
        self.turn = 1 - self.turn
        return True

    def single_attack(self, player: int, idx: int) -> None:
        me, foe = self.sides[player], self.sides[1 - player]
        if not foe.minions:
            return
        attacker = me.minions[idx]
        target = self.rng.choice(foe.targets())
        defender = foe.minions[target]
        # Snapshot: triggers during the exchange must not change the riposte.
        riposte_atk, riposte_poison = defender.atk, defender.poisonous
        self.damage(defender, attacker.atk, attacker.poisonous)
        self.damage(attacker, riposte_atk, riposte_poison)
        self.check_for_deaths()

    def damage(self, m: CMinion, amount: int, poison: bool) -> bool:
        if amount <= 0:
            return False
        if m.divine_shield:
            m.divine_shield = False        # absorbed entirely; poison does not apply
            return False
        m.health -= amount
        if m.health > 0 and poison:
            m.health = 0
        return True

    def check_for_deaths(self) -> None:
        """Collect every dead minion first, then run deathrattles. Repeat."""
        while True:
            corpses = []
            for player in (0, 1):
                side = self.sides[player]
                survivors = []
                for pos, m in enumerate(side.minions):
                    if m.dead:
                        corpses.append((player, pos, m))
                    else:
                        survivors.append(m)
                if len(survivors) != len(side.minions):
                    side.minions = survivors
            if not corpses:
                return
            for player, pos, m in corpses:
                if m.reborn:
                    revived = replace(m, health=1, reborn=False, divine_shield=False)
                    self.summon(player, pos, revived)
                fn = DEATHRATTLES.get(m.card_id)
                if fn:
                    fn(self, player, pos, m)

    def summon(self, player: int, pos: int, minion: CMinion) -> None:
        side = self.sides[player]
        if len(side.minions) >= BOARD_SIZE:
            return
        side.minions.insert(min(pos, len(side.minions)), minion)

    # -- result ---------------------------------------------------------
    def outcome(self) -> tuple[int, int]:
        a, b = self.sides[0].alive(), self.sides[1].alive()
        if a and not b:
            return 0, self.damage_dealt(0)
        if b and not a:
            return 1, self.damage_dealt(1)
        return -1, 0

    def damage_dealt(self, winner: int) -> int:
        """Battlegrounds damage: tavern tier plus the tiers of surviving minions."""
        return sum(1 + 0 for _ in self.sides[winner].minions)  # refined once tiers are wired


@dataclass
class SimResult:
    runs: int
    wins: int
    ties: int
    losses: int
    avg_damage: float = 0.0

    @property
    def win_pct(self) -> float:
        return self.wins / self.runs * 100 if self.runs else 0.0

    @property
    def tie_pct(self) -> float:
        return self.ties / self.runs * 100 if self.runs else 0.0

    @property
    def loss_pct(self) -> float:
        return self.losses / self.runs * 100 if self.runs else 0.0

    def __str__(self) -> str:
        return (f"win {self.win_pct:.0f}%  tie {self.tie_pct:.0f}%  "
                f"loss {self.loss_pct:.0f}%  (n={self.runs})")


def simulate(friendly: list[CMinion], enemy: list[CMinion], runs: int = 500,
             seed: int | None = None) -> SimResult:
    """Monte Carlo over the combat RNG (first attacker, target selection)."""
    rng = random.Random(seed)
    wins = ties = losses = dmg = 0
    for _ in range(runs):
        winner, d = Battle(friendly, enemy, rng).run()
        if winner == 0:
            wins += 1
            dmg += d
        elif winner == 1:
            losses += 1
        else:
            ties += 1
    return SimResult(runs, wins, ties, losses, dmg / wins if wins else 0.0)
