"""WP8: Real-component adapters for the control Orchestrator.

Bridges wmtm/ components (WMTMStore, PLN, GoalChainer, GoalStore)
into the control/ Orchestrator's hook-based architecture.

This replaces mock hooks with real component implementations, enabling
shadow-mode integration of the full goal-driven control pipeline.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from control.types import (
    ActionGrant,
    Appraisal,
    Belief,
    DeonticStatus,
    EffectClass,
    EffectRecord,
    GoalSpec,
    GoalStatus,
    Observation,
    Plan,
    PlanStep,
    now_iso,
)
from control.dispatcher import DispatchResult
from control.plan_validator import PlanValidator
from control.reference_monitor import ReferenceMonitor
from control.dispatcher import Dispatcher
from control.types import compute_mac
from control.event_store import EventStore

logger = logging.getLogger(__name__)


# -- Goal Bridge --------------------------------------------------------

def wmtm_goal_to_spec(goal) -> GoalSpec:
    """Convert a wmtm.goal.Goal into a control.types.GoalSpec."""
    status_map = {
        "pending": GoalStatus.DRAFT,
        "active": GoalStatus.ACTIVE,
        "achieved": GoalStatus.SATISFIED,
        "abandoned": GoalStatus.CANCELLED,
        "blocked": GoalStatus.PAUSED,
    }
    return GoalSpec(
        goal_id=goal.id,
        revision=1,
        owner="wmtm",
        description=goal.description,
        priority=int(goal.priority * 10),
        status=status_map.get(goal.status, GoalStatus.DRAFT),
    )


def spec_to_wmtm_goal(spec: GoalSpec) -> Any:
    """Convert a control.types.GoalSpec into a wmtm.goal.Goal."""
    from wmtm.goal import Goal as WMTMGoal
    status_map = {
        GoalStatus.DRAFT: "pending",
        GoalStatus.ACTIVE: "active",
        GoalStatus.SATISFIED: "achieved",
        GoalStatus.CANCELLED: "abandoned",
        GoalStatus.PAUSED: "blocked",
        GoalStatus.FAILED: "abandoned",
    }
    return WMTMGoal(
        id=spec.goal_id,
        description=spec.description,
        priority=spec.priority / 10.0 if spec.priority else 0.5,
        status=status_map.get(spec.status, "pending"),
    )


# -- Observer Adapter --------------------------------------------------

class WMTMObserverAdapter:
    """Adapts WMTMStore active items into Observation records."""

    def __init__(self, wmtm_store, query: str = ""):
        self._store = wmtm_store
        self._query = query

    def __call__(self, state_revision: int) -> list[Observation]:
        """Recall items from WMTM and convert to Observations."""
        items = self._store.get_active_set()
        observations = []
        for item in items:
            obs = Observation(
                observation_id=item.id,
                source="wmtm",
                collection_method="active_set",
                timestamp=now_iso(),
                state_revision=state_revision,
                schema_version="v1",
                content_digest=item.id,
                raw_evidence_ref=f"wmtm://{item.id}",
                content={
                    "content": item.content,
                    "source_type": item.source_type,
                    "sti": item.attention.sti,
                },
            )
            observations.append(obs)
        return observations


# -- Belief Adapter ----------------------------------------------------

class PLNBeliefAdapter:
    """Adapts PLN inference over WMTM into Belief records."""

    def __init__(self, wmtm_store, use_pln: bool = True):
        self._store = wmtm_store
        self._use_pln = use_pln

    def __call__(self, observations: list[Observation]) -> list[Belief]:
        """Run PLN inference and convert to Beliefs."""
        if not self._use_pln:
            return []
        try:
            from wmtm.pln_bridge import run_pln_inference_over_wmtm
            candidates = run_pln_inference_over_wmtm(self._store)
        except Exception as e:
            logger.warning(f"PLN inference failed: {e}")
            return []

        beliefs = []
        for cand in candidates:
            obs_ids = [
                obs.observation_id for obs in observations
                if obs.observation_id in (cand.derived_from or [])
            ]
            belief = Belief(
                belief_id=cand.candidate_id,
                claim=cand.content,
                truth_value=cand.confidence,
                confidence=cand.confidence,
                observation_ids=obs_ids,
                rule_ids=[cand.inference_type] if cand.inference_type else [],
                proof_trace=[cand.content],
            )
            beliefs.append(belief)
        return beliefs


# -- Plan Adapter ------------------------------------------------------

class GoalChainerPlanAdapter:
    """Adapts GoalChainer decisions into Plan records."""

    def __init__(self, wmtm_store, request: str = ""):
        self._store = wmtm_store
        self._request = request

    def __call__(self, observations: list[Observation], beliefs: list[Belief],
                 goal: GoalSpec) -> list[Plan]:
        """Generate plans from GoalChainer decisions."""
        request = self._request or goal.description
        try:
            from wmtm.goalchainer_bridge import run_goalchainer_over_wmtm
            candidates = run_goalchainer_over_wmtm(self._store, request)
        except Exception as e:
            logger.warning(f"GoalChainer failed: {e}")
            return []

        plans = []
        state_rev = observations[0].state_revision if observations else 0
        for cand in candidates:
            step = PlanStep(
                step_id=f"step_{cand.candidate_id}",
                operator_id="goalchainer_decision",
                operator_revision=1,
                arguments={"content": cand.content, "confidence": cand.confidence},
                target=goal.goal_id,
            )
            plan = Plan(
                plan_id=f"plan_{cand.candidate_id}",
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                state_revision=state_rev,
                steps=[step],
                terminal_predicate=goal.success_predicate or "done",
            )
            plans.append(plan)
        return plans


# -- Validate Adapter --------------------------------------------------

class RealValidateAdapter:
    """Wraps PlanValidator to match the Orchestrator's validate_fn hook."""

    def __init__(self, validator: PlanValidator):
        self._validator = validator

    def __call__(self, plan: Plan, observations: list[Observation]) -> Appraisal:
        return self._validator.validate(plan, observations)


# -- Authorize Adapter -------------------------------------------------

class RealAuthorizeAdapter:
    """Wraps ReferenceMonitor to produce authorized ActionGrants."""

    def __init__(self, ref_monitor: ReferenceMonitor, secret: bytes,
                 event_store: Optional[EventStore] = None):
        self._ref_monitor = ref_monitor
        self._secret = secret
        self._event_store = event_store

    def __call__(self, plan: Plan, goal: GoalSpec,
                 state_revision: int) -> list[ActionGrant]:
        grants = []
        current_rev = state_revision
        if self._event_store:
            events = self._event_store.get_all()
            if events:
                current_rev = events[-1].revision

        for step in plan.steps:
            grant = ActionGrant(
                grant_id=f"grant_{step.step_id}",
                operator_id=step.operator_id,
                operator_revision=step.operator_revision,
                tool_name=step.operator_id,
                canonical_args=repr(step.arguments),
                target=step.target,
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                state_revision=current_rev,
                idempotency_key=f"ik_{step.step_id}",
                nonce=f"n_{step.step_id}",
                issued_at=now_iso(),
                expires_at=now_iso(),
            )
            grant = ActionGrant(
                grant_id=grant.grant_id,
                operator_id=grant.operator_id,
                operator_revision=grant.operator_revision,
                tool_name=grant.tool_name,
                canonical_args=grant.canonical_args,
                target=grant.target,
                goal_id=grant.goal_id,
                goal_revision=grant.goal_revision,
                state_revision=grant.state_revision,
                idempotency_key=grant.idempotency_key,
                nonce=grant.nonce,
                issued_at=grant.issued_at,
                expires_at=grant.expires_at,
                mac=compute_mac(grant, self._secret),
            )
            authorized, reason = self._ref_monitor.authorize(grant, current_rev)
            if authorized:
                grants.append(grant)
            else:
                logger.warning(f"Authorization denied for {grant.grant_id}: {reason}")
        return grants


# -- Dispatch Adapter --------------------------------------------------

class RealDispatchAdapter:
    """Wraps Dispatcher to match the Orchestrator's dispatch_fn hook."""

    def __init__(self, dispatcher: Dispatcher):
        self._dispatcher = dispatcher

    def __call__(self, grant: ActionGrant) -> DispatchResult:
        return self._dispatcher.dispatch(grant, authorized=True)


# -- Evaluate Adapter --------------------------------------------------

class RealEvaluateAdapter:
    """Evaluates goal satisfaction based on effect records.

    In shadow mode, goals are never marked SATISFIED from tool returns.
    This adapter checks for explicit success predicates in observations.
    """

    def __init__(self, success_check: Optional[Callable] = None):
        self._success_check = success_check

    def __call__(self, effects: list[EffectRecord], goal: GoalSpec) -> GoalStatus:
        if self._success_check:
            return self._success_check(effects, goal)
        return GoalStatus.ACTIVE


# -- Integrated Orchestrator Factory -----------------------------------

def create_integrated_orchestrator(
    wmtm_store,
    goal: GoalSpec,
    secret: bytes,
    event_store: Optional[EventStore] = None,
    use_pln: bool = True,
    goalchainer_request: str = "",
    success_check: Optional[Callable] = None,
    plan_validator: Optional[PlanValidator] = None,
    ref_monitor: Optional[ReferenceMonitor] = None,
    dispatcher: Optional[Dispatcher] = None,
):
    """Create an Orchestrator wired with real WMTM/PLN/GoalChainer components.

    Args:
        wmtm_store: A WMTMStore instance with active items.
        goal: A GoalSpec describing the active goal.
        secret: HMAC secret for grant signing.
        event_store: Optional EventStore for revision tracking.
        use_pln: Whether to run PLN inference in the belief phase.
        goalchainer_request: Custom request string for GoalChainer.
        success_check: Optional callable(effects, goal) -> GoalStatus.
        plan_validator: Optional pre-configured PlanValidator.
        ref_monitor: Optional pre-configured ReferenceMonitor.
        dispatcher: Optional pre-configured Dispatcher.

    Returns:
        A configured Orchestrator with real-component hooks.
    """
    from control.orchestrator import Orchestrator

    if plan_validator is None:
        plan_validator = PlanValidator()
    if ref_monitor is None:
        ref_monitor = ReferenceMonitor(secret)
    if dispatcher is None:
        dispatcher = Dispatcher()

    observe = WMTMObserverAdapter(wmtm_store)
    believe = PLNBeliefAdapter(wmtm_store, use_pln=use_pln)
    plan_fn = GoalChainerPlanAdapter(wmtm_store, goalchainer_request)
    validate = RealValidateAdapter(plan_validator)
    authorize = RealAuthorizeAdapter(ref_monitor, secret, event_store)
    dispatch = RealDispatchAdapter(dispatcher)
    evaluate = RealEvaluateAdapter(success_check)

    orch = Orchestrator(
        observe_fn=observe,
        believe_fn=believe,
        plan_fn=plan_fn,
        validate_fn=validate,
        authorize_fn=authorize,
        dispatch_fn=dispatch,
        evaluate_fn=evaluate,
    )
    orch.set_goal(goal)
    return orch
