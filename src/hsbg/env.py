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
from .learner import WeightLearner
from .tavern import (
    Action,
    BuyMinion,
    BuySpell,
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
        learner: WeightLearner | None = None,
    ) -> None:
        self.cards = cards or CardDB()
        self.rng = random.Random(seed)
        self.max_turns = max_turns
        self.learner = learner or WeightLearner()
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
        enemy_board, opp_profile = self.learner.sample_opponent_board(
            snap.turn,
            snap.tier,
            self.cards,
            self.rng,
        )
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

        info_dict = {
            "win_pct": sim.win_pct,
            "tie_pct": sim.tie_pct,
            "loss_pct": sim.loss_pct,
            "damage_taken": dmg_taken,
            "total_hp": total_hp,
            "opp_profile": opp_profile,
        }

        done = (new_hp <= 0) or (snap.turn >= self.max_turns)
        if done:
            final_snap = replace(snap, hero_health=new_hp, hero_armor=new_armor)
            return StepResult(
                snapshot=final_snap,
                reward=round(reward, 2),
                done=True,
                info=info_dict,
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
            info=info_dict,
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
                roll_cost=1,
                rolled=False,
            )
        self.state = nxt
        return StepResult(snapshot=nxt, reward=0.0, done=False, info={})

    def play_episode(
        self,
        planner,
        max_actions_per_turn: int = 12,
        verbose: bool = False,
        learn: bool = True,
    ) -> dict:
        """Run a full closed-loop autonomous episode driven by `planner`."""
        snap = self.reset()
        total_reward = 0.0
        turns_survived = 0
        actions_taken = 0
        trajectory: list[dict] = []

        while True:
            steps_this_turn = 0
            turn_actions: list[str] = []
            bought_cards: list[str] = []
            while steps_this_turn < max_actions_per_turn:
                plan = planner.plan(self.state)
                if not plan.actions:
                    break
                first_act = plan.actions[0]
                if isinstance(first_act, (BuyMinion, BuySpell)):
                    bought_cards.append(first_act.card_id)
                turn_actions.append(first_act.describe())
                self.step(first_act)
                actions_taken += 1
                steps_this_turn += 1
                # If the plan didn't end in RollShop, execute the remaining planned steps directly
                if not isinstance(first_act, RollShop):
                    for follow_act in plan.actions[1:]:
                        if isinstance(follow_act, (BuyMinion, BuySpell)):
                            bought_cards.append(follow_act.card_id)
                        turn_actions.append(follow_act.describe())
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
            pre_combat = self.state
            my_cboard = [tminion_to_cminion(m, self.cards) for m in pre_combat.board]
            end_res = self.step(EndTurn())
            if learn and my_cboard:
                self.learner.record_ghost_board(pre_combat.turn, my_cboard)
            trajectory.append({
                "turn": pre_combat.turn,
                "bought_cards": bought_cards,
                "board_cards": [m.card_id for m in pre_combat.board],
                "reward": end_res.reward,
            })
            total_reward += end_res.reward
            turns_survived = snap.turn
            if verbose:
                board_str = ", ".join(
                    f"{m.name}{'*' if m.golden else ''} {m.atk}/{m.health}"
                    for m in pre_combat.board
                ) or "(порожньо)"
                acts_str = " -> ".join(turn_actions) if turn_actions else "Утриматись"
                win_pct = end_res.info.get("win_pct", 0.0)
                tie_pct = end_res.info.get("tie_pct", 0.0)
                loss_pct = end_res.info.get("loss_pct", 0.0)
                dmg = end_res.info.get("damage_taken", 0)
                hp = end_res.info.get("total_hp", 0)
                prof = end_res.info.get("opp_profile", "average")
                print(
                    f"  Хід {pre_combat.turn:2d} | T{pre_combat.tier} | дії: {acts_str}\n"
                    f"         | стіл ({len(pre_combat.board)}): {board_str}\n"
                    f"         | бій [{prof}]: win={win_pct:5.1f}%  tie={tie_pct:4.1f}%  loss={loss_pct:5.1f}%  dmg=-{dmg}  HP={hp}"
                )
            snap = end_res.snapshot
            if end_res.done:
                break

        updated_cards = {}
        if learn:
            updated_cards = self.learner.update_from_trajectory(trajectory)
            if getattr(planner, "value_fn", None) is not None:
                planner.value_fn._card_quality_cache.clear()

        return {
            "turns_survived": turns_survived,
            "final_tier": self.state.tier,
            "final_board_size": len(self.state.board),
            "final_hp": self.state.hero_health + self.state.hero_armor,
            "total_reward": round(total_reward, 2),
            "actions_taken": actions_taken,
            "updated_cards": len(updated_cards),
        }
