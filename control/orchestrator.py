"""Goal-driven control-cycle orchestrator.

Implements the bounded OODA-style control loop:
  Observe -> Belief -> Plan -> Appraise -> Authorize -> Execute -> Evaluate

The orchestrator sequences phases but never directly executes effects
(dispatcher's job) or authorizes actions (reference monitor's job).

Safety invariants:
  I2: Obligation != authorization (planning produces plans, not grants)
  I5: Independent completion (each step completes before next begins)
  I9: Human stop dominates (stop flag checked at top of each cycle)
  I10: Fail closed (errors abort the cycle, not silently swallowed)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from control.types import (
    ActionGrant,
    ActionState,
    Appraisal,
    Belief,
    DeonticStatus,
    Effect,
    EffectClass,
    EffectRecord,
    EventRecord,
    GoalSpec,
    GoalStatus,
    Observation,
    Plan,
    PlanStep,
    now_iso,
)
from control.event_store import EventStore
from control.dispatcher import Dispatcher, DispatchResult

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    """Result of a single control-cycle iteration."""
    cycle: int
    observations: list[Observation] = field(default_factory=list)
    beliefs: list[Belief] = field(default_factory=list)
    plans: list[Plan] = field(default_factory=list)
    appraisals: list[Appraisal] = field(default_factory=list)
    grants: list[ActionGrant] = field(default_factory=list)
    dispatches: list[DispatchResult] = field(default_factory=list)
    effects: list[EffectRecord] = field(default_factory=list)
    goal_status: Optional[GoalStatus] = None
    stopped: bool = False
    error: str = ""
    elapsed_ms: float = 0.0


class Orchestrator:
    """Sequences the goal-driven control cycle.

    Components are injected via callable hooks so that tests can
    substitute mocks. The orchestrator itself has no domain knowledge;
    it merely sequences the phases and enforces invariants.

    Hooks (all callable, injected at construction):
        observe_fn(state_revision) -> list[Observation]
        believe_fn(observations) -> list[Belief]
        plan_fn(observations, beliefs, goal) -> list[Plan]
        validate_fn(plan, observations) -> Appraisal
        authorize_fn(plan, goal, state_revision) -> list[ActionGrant]
        dispatch_fn(grant) -> DispatchResult
        evaluate_fn(effects, goal) -> GoalStatus
        record_fn(event) -> str  (store events)
    """

    def __init__(
        self,
        observe_fn: Callable[[int], list[Observation]],
        believe_fn: Callable[[list[Observation]], list[Belief]],
        plan_fn: Callable[[list[Observation], list[Belief], GoalSpec], list[Plan]],
        validate_fn: Callable[[Plan, list[Observation]], Appraisal],
        authorize_fn: Callable[[Plan, GoalSpec, int], list[ActionGrant]],
        dispatch_fn: Callable[[ActionGrant], DispatchResult],
        evaluate_fn: Callable[[list[EffectRecord], GoalSpec], GoalStatus],
        record_fn: Optional[Callable[[Any], str]] = None,
    ):
        self._observe_fn = observe_fn
        self._believe_fn = believe_fn
        self._plan_fn = plan_fn
        self._validate_fn = validate_fn
        self._authorize_fn = authorize_fn
        self._dispatch_fn = dispatch_fn
        self._evaluate_fn = evaluate_fn
        self._record_fn = record_fn or (lambda e: "")
        self._cycle = 0
        self._stopped = False
        self._goal: Optional[GoalSpec] = None
        self._state_revision = 0
        self._cycle_results: list[CycleResult] = []

    def set_goal(self, goal: GoalSpec):
        """Set the active goal for subsequent cycles."""
        self._goal = goal

    def stop(self):
        """I9: Human stop dominates."""
        self._stopped = True

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def cycle_count(self) -> int:
        return self._cycle

    @property
    def goal(self) -> Optional[GoalSpec]:
        return self._goal

    @property
    def state_revision(self) -> int:
        return self._state_revision

    @property
    def cycle_history(self) -> list[CycleResult]:
        return list(self._cycle_results)

    def run_cycle(self) -> CycleResult:
        """Execute one full control-cycle iteration."""
        start = time.monotonic()
        result = CycleResult(cycle=self._cycle)

        try:
            # I9: Check human stop at top of cycle
            if self._stopped:
                result.stopped = True
                result.error = "Stopped by human"
                result.elapsed_ms = (time.monotonic() - start) * 1000
                self._cycle_results.append(result)
                return result

            # No goal -> nothing to do
            if self._goal is None:
                result.error = "No active goal"
                result.elapsed_ms = (time.monotonic() - start) * 1000
                self._cycle_results.append(result)
                return result

            # Phase 1: Observe
            observations = self._observe_fn(self._state_revision)
            result.observations = observations
            for obs in observations:
                self._record_fn(obs)

            # Phase 2: Belief formation
            beliefs = self._believe_fn(observations)
            result.beliefs = beliefs
            for belief in beliefs:
                self._record_fn(belief)

            # Phase 3: Planning (I2: planning produces plans, not grants)
            plans = self._plan_fn(observations, beliefs, self._goal)
            result.plans = plans
            for plan in plans:
                self._record_fn(plan)

            if not plans:
                result.elapsed_ms = (time.monotonic() - start) * 1000
                self._cycle_results.append(result)
                self._cycle += 1
                self._state_revision += 1
                return result

            # Phase 4: Appraise (validate plans)
            appraisals = []
            for plan in plans:
                appraisal = self._validate_fn(plan, observations)
                appraisals.append(appraisal)
            result.appraisals = appraisals

            # Select best plan: only OBLIGATED or PERMITTED may proceed
            eligible = [
                (p, a) for p, a in zip(plans, appraisals)
                if a.deontic_status in (DeonticStatus.OBLIGATED, DeonticStatus.PERMITTED)
            ]
            if not eligible:
                result.elapsed_ms = (time.monotonic() - start) * 1000
                self._cycle_results.append(result)
                self._cycle += 1
                self._state_revision += 1
                return result

            # Pick highest ranking_score
            best_plan, best_appraisal = max(
                eligible, key=lambda pa: pa[1].ranking_score
            )

            # Phase 5: Authorize (reference monitor issues grants)
            grants = self._authorize_fn(best_plan, self._goal, self._state_revision)
            result.grants = grants

            # Phase 6: Execute (dispatcher) - I5: independent completion
            dispatches = []
            for grant in grants:
                dispatch_result = self._dispatch_fn(grant)
                dispatches.append(dispatch_result)
                if dispatch_result.effect_record:
                    self._record_fn(dispatch_result.effect_record)
            result.dispatches = dispatches
            result.effects = [
                d.effect_record for d in dispatches if d.effect_record
            ]

            # Phase 7: Evaluate goal status
            goal_status = self._evaluate_fn(result.effects, self._goal)
            result.goal_status = goal_status

            if goal_status in (GoalStatus.SATISFIED, GoalStatus.FAILED):
                self._goal = None  # Goal complete

        except Exception as e:
            # I10: Fail closed
            result.error = f"{type(e).__name__}: {e}"
            logger.error("Control cycle error: %s", result.error, exc_info=True)

        result.elapsed_ms = (time.monotonic() - start) * 1000
        self._cycle_results.append(result)
        self._cycle += 1
        self._state_revision += 1
        return result

    def run(self, max_cycles: int = 100) -> list[CycleResult]:
        """Run cycles until goal achieved/failed, stopped, or max reached."""
        results = []
        for _ in range(max_cycles):
            if self._stopped or self._goal is None:
                break
            result = self.run_cycle()
            results.append(result)
            if result.goal_status in (GoalStatus.SATISFIED, GoalStatus.FAILED):
                break
        return results
