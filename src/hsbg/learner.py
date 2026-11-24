"""Online & Self-Play Reinforcement Learning weight updater and Opponent Pool.

Maintains `data/learned_weights.json` with:
1. Per-card learned score adjustments (`card_weights[card_id]`) split by game phase
   (`global`, `early` turns 1-3, `mid` turns 4-7, `late` turns 8+).
2. Per-turn moving return baselines (`turn_baselines[turn]`) for zero-centered
   Advantage updates: `delta = lr * (G_t - baseline_t)`.
3. A rolling pool of opponent boards (`ghost_boards[turn]`) combining real human
   opponent boards from `Power.log` and self-play agent boards, sampled across
   Lowroll (20%), Median (60%), and Highroll/"Goldfish" (20%) brackets.
4. Automatic hot-reloading on file mtime change so the live overlay picks up new
   weights immediately without restarting.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .combat import CMinion

if TYPE_CHECKING:
    from .carddb import CardDB
    from .tavern import MinionPool, TMinion

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_WEIGHTS_PATH = ROOT / "data" / "learned_weights.json"

MAX_DELTA = 12.0
MAX_GHOSTS_PER_TURN = 36


def turn_bucket(turn: int) -> str:
    if turn <= 3:
        return "early"
    if turn <= 7:
        return "mid"
    return "late"


def _board_power(minions: list[dict] | list[CMinion]) -> float:
    total = 0.0
    for m in minions:
        if isinstance(m, dict):
            atk = m.get("atk", 1)
            hp = m.get("health", 1)
            tier = m.get("tier", 1)
            ds = 4.0 if m.get("divine_shield") else 0.0
            reb = 3.0 if m.get("reborn") else 0.0
            ven = 8.0 if (m.get("venomous") or m.get("poisonous")) else 0.0
        else:
            atk, hp, tier = m.atk, m.health, m.tier
            ds = 4.0 if m.divine_shield else 0.0
            reb = 3.0 if m.reborn else 0.0
            ven = 8.0 if (m.venomous or m.poisonous) else 0.0
        total += atk + hp + tier * 1.2 + ds + reb + ven
    return total


def serialize_cminion(m: CMinion) -> dict:
    return {
        "card_id": m.card_id,
        "name": m.name,
        "atk": m.atk,
        "health": m.health,
        "tier": m.tier,
        "taunt": m.taunt,
        "divine_shield": m.divine_shield,
        "poisonous": m.poisonous,
        "venomous": m.venomous,
        "windfury": m.windfury,
        "reborn": m.reborn,
        "golden": m.golden,
        "cleave": m.cleave,
    }


def deserialize_cminion(d: dict) -> CMinion:
    return CMinion(
        card_id=d.get("card_id", ""),
        name=d.get("name", "Minion"),
        atk=int(d.get("atk", 1)),
        health=int(d.get("health", 1)),
        tier=int(d.get("tier", 1)),
        taunt=bool(d.get("taunt", False)),
        divine_shield=bool(d.get("divine_shield", False)),
        poisonous=bool(d.get("poisonous", False)),
        venomous=bool(d.get("venomous", False)),
        windfury=bool(d.get("windfury", False)),
        reborn=bool(d.get("reborn", False)),
        golden=bool(d.get("golden", False)),
        cleave=bool(d.get("cleave", False)),
    )


class WeightLearner:
    """Persistent RL weight table and opponent ghost pool with automatic hot-reload."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_WEIGHTS_PATH
        self.card_weights: dict[str, dict[str, float]] = {}
        self.turn_baselines: dict[str, float] = {}
        self.ghost_boards: dict[str, list[list[dict]]] = {}
        self.episodes_trained: int = 0
        self.live_combats_learned: int = 0
        self._last_mtime: float = 0.0
        self._last_check: float = 0.0
        self.load(force=True)

    def load(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and (now - self._last_check) < 1.0:
            return False
        self._last_check = now
        try:
            if not self.path.exists():
                return False
            mtime = os.stat(self.path).st_mtime
            if not force and mtime <= self._last_mtime:
                return False
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.card_weights = raw.get("card_weights", {})
            self.turn_baselines = raw.get("turn_baselines", {})
            self.ghost_boards = raw.get("ghost_boards", {})
            self.episodes_trained = int(raw.get("episodes_trained", 0))
            self.live_combats_learned = int(raw.get("live_combats_learned", 0))
            self._last_mtime = mtime
            return True
        except Exception:
            return False

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "episodes_trained": self.episodes_trained,
                "live_combats_learned": self.live_combats_learned,
                "turn_baselines": self.turn_baselines,
                "card_weights": self.card_weights,
                "ghost_boards": self.ghost_boards,
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._last_mtime = os.stat(self.path).st_mtime
        except Exception:
            pass

    def card_delta(self, card_id: str, turn: int) -> float:
        """Return learned score adjustment in [-12.0, +12.0] for `card_id` at `turn`."""
        self.load(force=False)
        entry = self.card_weights.get(card_id)
        if not entry:
            return 0.0
        g_val = float(entry.get("global", 0.0))
        b_val = float(entry.get(turn_bucket(max(1, turn)), 0.0))
        return max(-MAX_DELTA, min(MAX_DELTA, 0.55 * g_val + 0.45 * b_val))

    def record_ghost_board(self, turn: int, board: list[CMinion]) -> None:
        """Store a real or self-play warband at `turn` for future opponent sampling."""
        if turn <= 0 or not board:
            return
        t_key = str(turn)
        serialized = [serialize_cminion(m) for m in board]
        bucket = self.ghost_boards.setdefault(t_key, [])
        bucket.append(serialized)
        if len(bucket) > MAX_GHOSTS_PER_TURN:
            del bucket[0 : len(bucket) - MAX_GHOSTS_PER_TURN]

    def sample_opponent_board(
        self,
        turn: int,
        tier: int,
        cards: CardDB,
        rng: random.Random,
    ) -> tuple[list[CMinion], str]:
        """Sample an opponent board for `turn` across Lowroll (20%), Median (60%), or Goldfish (20%)."""
        roll = rng.random()
        if roll < 0.20:
            profile = "lowroll"
            mult = rng.uniform(0.72, 0.88)
        elif roll < 0.80:
            profile = "average"
            mult = rng.uniform(0.92, 1.08)
        else:
            profile = "goldfish"
            mult = rng.uniform(1.22, 1.48)

        ghosts = self.ghost_boards.get(str(turn), [])
        if len(ghosts) >= 3 and rng.random() < 0.72:
            ranked = sorted(ghosts, key=_board_power)
            n = len(ranked)
            if profile == "lowroll":
                chosen = ranked[rng.randrange(0, max(1, n // 3))]
            elif profile == "goldfish":
                chosen = ranked[rng.randrange(max(0, (2 * n) // 3), n)]
            else:
                lo = max(0, n // 4)
                hi = max(lo + 1, (3 * n) // 4)
                chosen = ranked[rng.randrange(lo, hi)]
            return [deserialize_cminion(d) for d in chosen], f"ghost-{profile}"

        # Fallback / diversity: procedural board calibrated to turn & profile
        from .value import synthetic_enemy_board

        base = synthetic_enemy_board(turn, tier, power_mult=mult)
        return base, profile

    def _apply_card_delta(self, card_id: str, turn: int, advantage: float, lr: float) -> None:
        if not card_id or card_id.startswith("BENCH_"):
            return
        entry = self.card_weights.setdefault(
            card_id,
            {"global": 0.0, "early": 0.0, "mid": 0.0, "late": 0.0, "n": 0},
        )
        b_key = turn_bucket(max(1, turn))
        step = max(-2.5, min(2.5, lr * advantage))
        entry["global"] = round(max(-MAX_DELTA, min(MAX_DELTA, entry.get("global", 0.0) * 0.996 + step)), 4)
        entry[b_key] = round(max(-MAX_DELTA, min(MAX_DELTA, entry.get(b_key, 0.0) * 0.996 + step * 1.15)), 4)
        entry["n"] = int(entry.get("n", 0)) + 1

    def update_from_trajectory(
        self,
        trajectory: list[dict],
        lr: float = 0.28,
        gamma: float = 0.72,
        save: bool = True,
    ) -> dict[str, float]:
        """Update card weights from an episode trajectory using discounted Monte Carlo Advantage.

        Each item in `trajectory` has:
          - `turn`: int
          - `bought_cards`: list[str]
          - `board_cards`: list[str]
          - `reward`: float
        """
        if not trajectory:
            return {}
        n = len(trajectory)
        returns = [0.0] * n
        running_g = 0.0
        for i in range(n - 1, -1, -1):
            running_g = float(trajectory[i]["reward"]) + gamma * running_g
            returns[i] = running_g

        deltas_applied: dict[str, float] = {}
        for step_info, g_t in zip(trajectory, returns):
            turn = int(step_info["turn"])
            t_key = str(turn)
            base = float(self.turn_baselines.get(t_key, g_t))
            advantage = g_t - base
            self.turn_baselines[t_key] = round(0.90 * base + 0.10 * g_t, 4)

            for cid in step_info.get("bought_cards", ()):
                self._apply_card_delta(cid, turn, advantage, lr=lr)
                deltas_applied[cid] = self.card_delta(cid, turn)
            for cid in step_info.get("board_cards", ()):
                self._apply_card_delta(cid, turn, advantage, lr=lr * 0.45)
                deltas_applied[cid] = self.card_delta(cid, turn)

        self.episodes_trained += 1
        if save:
            self.save()
        return deltas_applied

    def update_from_live_combat(
        self,
        turn: int,
        bought_cards: list[str],
        board_cards: list[str],
        combat_reward: float,
        enemy_board: list[CMinion] | None = None,
    ) -> None:
        """Online learning update after a single live combat in Hearthstone."""
        if turn <= 0:
            return
        if enemy_board:
            self.record_ghost_board(turn, enemy_board)
        t_key = str(turn)
        base = float(self.turn_baselines.get(t_key, 0.0))
        advantage = combat_reward - base
        self.turn_baselines[t_key] = round(0.92 * base + 0.08 * combat_reward, 4)

        for cid in bought_cards:
            self._apply_card_delta(cid, turn, advantage, lr=0.22)
        for cid in board_cards:
            self._apply_card_delta(cid, turn, advantage, lr=0.10)

        self.live_combats_learned += 1
        self.save()
