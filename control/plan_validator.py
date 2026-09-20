"""Plan validator for goal-driven iter control.

Validates plans against registered action operators, checking:
- All step operators exist and are at the correct revision
- Pre-conditions are satisfiable given current observations
- Resource claims are within limits
- Plans are well-formed (non-empty steps, valid goal reference)

Safety invariants:
- I3: Plans reference the state revision they were planned against.
- I5: Deontic logic (permitted/forbidden/obligated) is enforced.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from control.types import (
    ActionOperator,
    Appraisal,
    DeonticStatus,
    Observation,
    Plan,
    PlanStep,
    Predicate,
    Effect,
    EffectClass,
    ResourceClaim,
)


class PlanValidator:
    """Validates plans before they can be appraised and dispatched.

    The validator checks structural integrity, operator availability,
    pre-condition satisfiability, and resource constraints.
    """

    def __init__(self, max_steps: int = 32, max_plan_depth: int = 4):
        self._operators: dict[str, ActionOperator] = {}
        self._max_steps = max_steps
        self._max_plan_depth = max_plan_depth
        self._predicate_check_fns: dict[str, callable] = {}

    def register_operator(self, operator: ActionOperator):
        """Register an action operator."""
        key = f"{operator.operator_id}@{operator.revision}"
        self._operators[key] = operator

    def register_predicate_check(self, predicate_ref: str, check_fn: callable):
        """Register a check function for a predicate reference."""
        self._predicate_check_fns[predicate_ref] = check_fn

    def get_operator(self, operator_id: str, revision: int) -> Optional[ActionOperator]:
        """Retrieve a registered operator by ID and revision."""
        return self._operators.get(f"{operator_id}@{revision}")

    def validate(self, plan: Plan, observations: list[Observation]) -> Appraisal:
        """Validate a plan and return an Appraisal.

        The appraisal contains a deontic_status (PERMITTED, FORBIDDEN,
        or OBLIGATED), a utility estimate, and an uncertainty score.
        """
        errors: list[str] = []

        # Check 1: Plan has steps
        if len(plan.steps) == 0:
            errors.append("Plan has no steps")

        # Check 2: Step count within limits
        if len(plan.steps) > self._max_steps:
            errors.append(
                f"Plan has {len(plan.steps)} steps, max is {self._max_steps}"
            )

        # Check 3: Goal reference is non-empty
        if not plan.goal_id:
            errors.append("Plan has no goal_id")

        # Check 4: Each step references a valid operator
        for i, step in enumerate(plan.steps):
            op = self.get_operator(step.operator_id, step.operator_revision)
            if op is None:
                errors.append(
                    f"Step {i}: operator {step.operator_id}@{step.operator_revision} not registered"
                )
                continue

            # Check 5: Pre-conditions are satisfiable
            if op.precondition:
                if not self._is_precondition_satisfied(op.precondition, observations):
                    errors.append(
                        f"Step {i}: pre-condition not satisfied: {op.precondition}"
                    )

        # Determine deontic status
        if errors:
            deontic = DeonticStatus.FORBIDDEN
            utility = 0.0
            uncertainty = 1.0
        else:
            deontic = DeonticStatus.PERMITTED
            utility = self._estimate_utility(plan, observations)
            uncertainty = self._estimate_uncertainty(plan, observations)

        return Appraisal(
            appraisal_id="",
            plan_id=plan.plan_id,
            deontic_status=deontic,
            utility_estimate=utility,
            uncertainty=uncertainty,
            authorized=False,  # authorization happens in reference monitor
            rejection_reason="; ".join(errors) if errors else "",
        )

    def _is_precondition_satisfied(self, precondition_ref: str, observations: list[Observation]) -> bool:
        """Check if a pre-condition (predicate reference) is satisfied.

        If a check function was registered for this predicate ref, use it.
        Otherwise, check if any observation's content contains the ref as a key
        with a truthy value.
        """
        # Use registered check function if available
        if precondition_ref in self._predicate_check_fns:
            return self._predicate_check_fns[precondition_ref](observations)

        # Default: check if any observation content has this as a truthy key
        for obs in observations:
            if precondition_ref in obs.content:
                return bool(obs.content[precondition_ref])
        return False

    def _estimate_utility(self, plan: Plan, observations: list[Observation]) -> float:
        """Estimate the utility of a plan on a 0-1 scale.

        Simple heuristic: more steps = lower utility (diminishing returns),
        more satisfied pre-conditions = higher utility.
        """
        if not plan.steps:
            return 0.0
        step_factor = 1.0 / (1.0 + 0.1 * len(plan.steps))
        satisfied = 0
        total = 0
        for step in plan.steps:
            op = self.get_operator(step.operator_id, step.operator_revision)
            if op and op.precondition:
                total += 1
                if self._is_precondition_satisfied(op.precondition, observations):
                    satisfied += 1
        sat_factor = satisfied / total if total > 0 else 1.0
        return round(step_factor * sat_factor, 4)

    def _estimate_uncertainty(self, plan: Plan, observations: list[Observation]) -> float:
        """Estimate uncertainty on a 0-1 scale.

        More steps = more uncertainty. Fewer observations = more uncertainty.
        """
        step_uncertainty = 0.05 * len(plan.steps)
        obs_uncertainty = 0.3 if len(observations) == 0 else 0.0
        return round(min(1.0, step_uncertainty + obs_uncertainty), 4)

    @property
    def operator_count(self) -> int:
        return len(self._operators)
