"""Headless Battlegrounds simulation environment for closed-loop agent evaluation.

Implements a Gym-style `reset() / step(action)` loop over `tavern.py` and
`combat.py` so `TurnPlanner` (or future RL policies) can run full autonomous
multi-turn games (`perceive -> plan -> act -> combat -> next turn`) offline.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, replace

from .carddb import CardDB
from .combat import simulate
from .tavern import (
    Action,
    EndTurn,
    GameSnapshot,
    MinionPool,
    RollShop,
    apply_action,
    legal_actions,
)
from .value import synthetic_enemy_board, tminion_to_cminion

BASE_UPGRADE_COST = {1: 5, 2: 7, 3: 8, 4: 9, 5: 10}


@dataclass
class StepResult:
    snapshot: GameSnapshot
    reward: float
    done: bool
    info: dict


class BGGymEnv:
    """Single-player headless Battlegrounds environment against benchmark lobby boards."""

    def __init__(
        self,
        cards: CardDB | None = None,
        seed: int = 42,
        max_turns: int = 12,
    ) -> None:
        self.cards = cards or CardDB()
        self.rng = random.Random(seed)
        self.max_turns = max_turns
        self.pool = MinionPool(self.cards)
        self.state: GameSnapshot = self._initial_snapshot()
        self._uid_counter = 500_000

    def _next_shop(self, tier: int) -> tuple:
        shop = self.pool.sample_shop(tier, rng=self.rng, start_uid=self._uid_counter)
        self._uid_counter += len(shop) + 1
        return shop

    def _initial_snapshot(self) -> GameSnapshot:
        self._uid_counter = 500_000
        return GameSnapshot(
            turn=1,
            tier=1,
            gold=3,
            upgrade_cost=BASE_UPGRADE_COST[1],
            shop=self._next_shop(1),
            hero_health=30,
            hero_armor=10,
        )

    def reset(self, seed: int | None = None) -> GameSnapshot:
        if seed is not None:
            self.rng = random.Random(seed)
        self.state = self._initial_snapshot()
        return self.state

    def legal_actions(self) -> list[Action]:
        return legal_actions(self.state) + [EndTurn()]

    def _resolve_combat_and_advance_turn(self, snap: GameSnapshot) -> StepResult:
        my_board = [tminion_to_cminion(m, self.cards) for m in snap.board]
        enemy_board = synthetic_enemy_board(snap.turn, snap.tier)
        sim = simulate(
            my_board,
            enemy_board,
            runs=25,
            seed=self.rng.randrange(1_000_000),
            tavern_tiers=(snap.tier, snap.tier),
        )
        # Expected net combat outcome from simulation
        if sim.win_pct >= sim.loss_pct and sim.win_pct >= sim.tie_pct:
            dmg_taken = 0
            reward = 2.0 + 0.1 * max(0.0, sim.avg_damage)
        elif sim.tie_pct >= sim.win_pct and sim.tie_pct >= sim.loss_pct:
            dmg_taken = 0
            reward = 0.2
        else:
            dmg_taken = max(1, int(round(abs(sim.avg_damage))))
            reward = -1.5 - 0.25 * dmg_taken

        total_hp = max(0, snap.hero_health + snap.hero_armor - dmg_taken)
        new_armor = max(0, snap.hero_armor - dmg_taken)
        rem_dmg = max(0, dmg_taken - snap.hero_armor)
        new_hp = max(0, snap.hero_health - rem_dmg)

        done = (new_hp <= 0) or (snap.turn >= self.max_turns)
        if done:
            final_snap = replace(snap, hero_health=new_hp, hero_armor=new_armor)
            return StepResult(
                snapshot=final_snap,
                reward=round(reward, 2),
                done=True,
                info={"win_pct": sim.win_pct, "damage_taken": dmg_taken, "total_hp": total_hp},
            )

        next_turn = snap.turn + 1
        next_gold = min(10, next_turn + 2) + snap.future_gold_bonus
        if snap.upgraded_this_turn:
            next_up_cost = BASE_UPGRADE_COST.get(snap.tier)
        elif snap.upgrade_cost is not None:
            next_up_cost = max(0, snap.upgrade_cost - 1)
        else:
            next_up_cost = BASE_UPGRADE_COST.get(snap.tier)

        if snap.frozen_shop and snap.shop:
            # Keep frozen cards and top up to full shop size for current tier
            kept = tuple(replace(m, frozen=False) for m in snap.shop)
            fresh = self._next_shop(snap.tier)
            target_sz = len(fresh)
            next_shop = (kept + fresh)[: max(len(kept), target_sz)]
        else:
            next_shop = self._next_shop(snap.tier)

        next_snap = replace(
            snap,
            turn=next_turn,
            gold=next_gold,
            upgrade_cost=next_up_cost,
            board=tuple(replace(m, newly_bought=False) for m in snap.board),
            hand=tuple(replace(m, newly_bought=False) for m in snap.hand),
            shop=next_shop,
            shop_spells=(),
            hero_health=new_hp,
            hero_armor=new_armor,
            upgraded_this_turn=False,
            triples_formed=0,
            future_gold_bonus=0,
            spells_cast=(),
            frozen_shop=False,
            rolled=False,
        )
        self.state = next_snap
        return StepResult(
            snapshot=next_snap,
            reward=round(reward, 2),
            done=False,
            info={"win_pct": sim.win_pct, "damage_taken": dmg_taken, "total_hp": total_hp},
        )

    def step(self, action: Action) -> StepResult:
        """Execute one action in the environment; RollShop refreshes shop from MinionPool."""
        if isinstance(action, EndTurn):
            return self._resolve_combat_and_advance_turn(self.state)

        nxt = apply_action(self.state, action)
        if isinstance(action, RollShop):
            # In closed-loop mode, rolling immediately populates a fresh shop!
            nxt = replace(
                nxt,
                shop=self._next_shop(nxt.tier),
                rolled=False,
            )
        self.state = nxt
        return StepResult(snapshot=nxt, reward=0.0, done=False, info={})

    def play_episode(self, planner, max_actions_per_turn: int = 12) -> dict:
        """Run a full closed-loop autonomous episode driven by `planner`."""
        snap = self.reset()
        total_reward = 0.0
        turns_survived = 0
        actions_taken = 0

        while True:
            steps_this_turn = 0
            while steps_this_turn < max_actions_per_turn:
                plan = planner.plan(self.state)
                if not plan.actions:
                    break
                first_act = plan.actions[0]
                res = self.step(first_act)
                actions_taken += 1
                steps_this_turn += 1
                # If the plan didn't end in RollShop, execute the remaining planned steps directly
                if not isinstance(first_act, RollShop):
                    for follow_act in plan.actions[1:]:
                        if isinstance(follow_act, RollShop):
                            self.step(follow_act)
                            actions_taken += 1
                            steps_this_turn += 1
                            break
                        self.step(follow_act)
                        actions_taken += 1
                        steps_this_turn += 1
                    else:
                        break
            end_res = self.step(EndTurn())
            total_reward += end_res.reward
            turns_survived = snap.turn
            snap = end_res.snapshot
            if end_res.done:
                break

        return {
            "turns_survived": turns_survived,
            "final_tier": self.state.tier,
            "final_board_size": len(self.state.board),
            "final_hp": self.state.hero_health + self.state.hero_armor,
            "total_reward": round(total_reward, 2),
            "actions_taken": actions_taken,
        }
