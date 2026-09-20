"""
WP9: End-to-End Integration Test

Exercises the full OODA pipeline through the Orchestrator with real adapters:
  WMTM (observe) -> PLN (infer beliefs) -> GoalChainer (plan) -> Validate -> Authorize -> Dispatch -> Evaluate
"""
import pytest
import sys
sys.path.insert(0, ".")

from control.orchestrator import Orchestrator, CycleResult
from control.types import (
    ActionGrant, ActionState, Appraisal, Belief, DeonticStatus,
    Effect, EffectClass, EffectRecord, GoalSpec, GoalStatus,
    Observation, Plan, PlanStep, now_iso,
)
from control.dispatcher import DispatchResult
from control.wmtm_adapters import (
    WMTMObserverAdapter, PLNBeliefAdapter, GoalChainerPlanAdapter,
    create_integrated_orchestrator,
)
from wmtm.store import WMTMStore


class TestWP9E2EPipeline:
    """Full pipeline integration tests."""

    def test_full_cycle_with_real_adapters(self):
        """Run a complete cycle with real WMTM+PLN+GC adapters."""
        store = WMTMStore(capacity=20)
        store.admit("f1", "CPU is a resource", source_type="recalled")
        store.admit("f2", "resource is a system", source_type="recalled")
        store.admit("f3", "Disk is a device", source_type="recalled")

        goal = GoalSpec(
            goal_id="g_reduce_load",
            revision=1,
            owner="ops",
            description="Reduce CPU load",
            priority=1,
            status=GoalStatus.ACTIVE,
            success_predicate="cpu < 60",
        )

        orch = create_integrated_orchestrator(store, goal, b"test_secret", use_pln=True)
        result = orch.run_cycle()

        assert isinstance(result, CycleResult)
        assert len(result.observations) >= 1
        assert len(result.beliefs) >= 1
        assert result.elapsed_ms > 0

    def test_multi_cycle_progression(self):
        """Run multiple cycles to test state progression."""
        store = WMTMStore(capacity=20)
        store.admit("f1", "Service response time 500ms", source_type="recalled")
        store.admit("f2", "Error rate 5%", source_type="recalled")

        goal = GoalSpec(
            goal_id="g_fix_latency",
            revision=1,
            owner="ops",
            description="Fix high latency",
            priority=1,
            status=GoalStatus.ACTIVE,
        )

        orch = create_integrated_orchestrator(store, goal, b"secret", use_pln=True)

        results = []
        for _ in range(3):
            r = orch.run_cycle()
            results.append(r)

        assert len(results) == 3
        assert orch.cycle_count == 3
        assert orch.state_revision >= 3

    def test_goal_satisfaction_stops_cycle(self):
        """When evaluate returns SATISFIED, orchestrator clears goal."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "CPU at 40%", source_type="recalled")

        goal = GoalSpec(
            goal_id="g_done",
            revision=1,
            owner="ops",
            description="CPU under control",
            priority=1,
            status=GoalStatus.ACTIVE,
        )

        orch = create_integrated_orchestrator(store, goal, b"secret", use_pln=False)
        orch._evaluate_fn = lambda effects, g: GoalStatus.SATISFIED
        orch._plan_fn = lambda obs, beliefs, goal: [
            Plan(
                plan_id="p1",
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                state_revision=0,
                steps=[
                    PlanStep(
                        step_id="s1",
                        operator_id="noop",
                        operator_revision=1,
                        arguments={},
                        target=goal.goal_id,
                    )
                ],
                terminal_predicate="done",
            )
        ]
        orch._validate_fn = lambda plan, obs: Appraisal(
            appraisal_id="",
            plan_id=plan.plan_id,
            deontic_status=DeonticStatus.PERMITTED,
            utility_estimate=1.0, uncertainty=0.0,
            ranking_score=1.0,
            notes="test",
        )
        orch._authorize_fn = lambda plan, goal, sr: [
            ActionGrant(
                grant_id="grant1",
                operator_id=plan.steps[0].operator_id,
                operator_revision=plan.steps[0].operator_revision,
                tool_name=plan.steps[0].operator_id,
                canonical_args="{}",
                target=plan.steps[0].target,
                goal_id=plan.goal_id,
                goal_revision=plan.goal_revision,
                state_revision=0,
                idempotency_key="",
                nonce="",
                issued_at=now_iso(),
                expires_at=now_iso(),
            )
        ]
        orch._dispatch_fn = lambda grant: DispatchResult(
            grant_id=grant.grant_id,
            success=True,
            effect_record=EffectRecord(
                record_id="eff1",
                grant_id=grant.grant_id,
                operator_id=grant.operator_id,
                target=grant.target,
                observed_at=now_iso(),
                effect_class=EffectClass.IRREVERSIBLE,
                details={"description": "noop executed"},
            ),
        )

        result = orch.run_cycle()
        assert result.goal_status == GoalStatus.SATISFIED
        assert orch.goal is None

    def test_human_stop_aborts_cycle(self):
        """I9: Human stop dominates."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "test", source_type="recalled")
        goal = GoalSpec(
            goal_id="g_stop", revision=1, owner="t",
            description="test", priority=1, status=GoalStatus.ACTIVE,
        )
        orch = create_integrated_orchestrator(store, goal, b"s", use_pln=False)
        orch.stop()
        result = orch.run_cycle()
        assert result.stopped is True
        assert "Stopped" in result.error

    def test_error_handling_in_observe(self):
        """I10: Fail closed -- observe error aborts cycle gracefully."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "test", source_type="recalled")
        goal = GoalSpec(
            goal_id="g_err", revision=1, owner="t",
            description="test", priority=1, status=GoalStatus.ACTIVE,
        )
        orch = create_integrated_orchestrator(store, goal, b"s", use_pln=False)

        def broken_observe(sr):
            raise RuntimeError("sensor failure")
        orch._observe_fn = broken_observe

        result = orch.run_cycle()
        assert result.error != ""
        assert "sensor failure" in result.error
        assert result.cycle == 0

    def test_writeback_to_wmtm(self):
        """After dispatch, effects should be recorded via record_fn."""
        store = WMTMStore(capacity=20)
        store.admit("f1", "CPU at 95%", source_type="recalled")

        goal = GoalSpec(
            goal_id="g_wb", revision=1, owner="ops",
            description="Reduce load", priority=1, status=GoalStatus.ACTIVE,
        )

        recorded_events = []

        def recording_fn(event):
            recorded_events.append(event)
            return str(id(event))

        orch = create_integrated_orchestrator(store, goal, b"s", use_pln=False)
        orch._record_fn = recording_fn

        orch._plan_fn = lambda obs, beliefs, g: [
            Plan(
                plan_id="p_wb", goal_id=g.goal_id, goal_revision=g.revision,
                state_revision=0,
                steps=[PlanStep(step_id="s1", operator_id="restart",
                               operator_revision=1, arguments={"svc": "api"},
                               target=g.goal_id)],
                terminal_predicate="done",
            )
        ]
        orch._validate_fn = lambda plan, obs: Appraisal(
            appraisal_id="",
            plan_id=plan.plan_id, deontic_status=DeonticStatus.OBLIGATED,
            utility_estimate=1.0, uncertainty=0.0,
            ranking_score=1.0, notes="valid",
        )
        orch._authorize_fn = lambda plan, g, sr: [
            ActionGrant(
                grant_id="gr1", operator_id="restart", operator_revision=1,
                tool_name="restart", canonical_args='{"svc": "api"}',
                target=plan.steps[0].target, goal_id=plan.goal_id,
                goal_revision=plan.goal_revision, state_revision=0,
                idempotency_key="", nonce="", issued_at=now_iso(),
                expires_at=now_iso(),
            )
        ]
        orch._dispatch_fn = lambda grant: DispatchResult(
            grant_id=grant.grant_id, success=True,
            effect_record=EffectRecord(
                record_id="eff_wb", grant_id=grant.grant_id,
                operator_id=grant.operator_id, target=grant.target,
                observed_at=now_iso(),
                effect_class=EffectClass.IRREVERSIBLE,
                details={"description": "Service restarted"},
            ),
        )
        orch._evaluate_fn = lambda effects, g: GoalStatus.ACTIVE

        result = orch.run_cycle()

        assert len(result.dispatches) == 1
        assert result.dispatches[0].success is True
        assert len(result.effects) == 1

        effect_events = [e for e in recorded_events if isinstance(e, EffectRecord)]
        assert len(effect_events) == 1
        assert effect_events[0].details.get("description") == "Service restarted"

    def test_run_multiple_cycles_auto(self):
        """Orchestrator.run() should stop when goal is satisfied."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "test", source_type="recalled")
        goal = GoalSpec(
            goal_id="g_run", revision=1, owner="t",
            description="test", priority=1, status=GoalStatus.ACTIVE,
        )
        orch = create_integrated_orchestrator(store, goal, b"s", use_pln=False)

        call_count = [0]
        def counting_evaluate(effects, g):
            call_count[0] += 1
            if call_count[0] >= 2:
                return GoalStatus.SATISFIED
            return GoalStatus.ACT
        # Need plan+validate+authorize+dispatch for evaluate to fire
        orch._plan_fn = lambda obs, beliefs, g: [
            Plan(
                plan_id="p_run", goal_id=g.goal_id, goal_revision=g.revision,
                state_revision=0,
                steps=[PlanStep(step_id="s1", operator_id="noop",
                               operator_revision=1, arguments={},
                               target=g.goal_id)],
                terminal_predicate="done",
            )
        ]
        orch._validate_fn = lambda plan, obs: Appraisal(
            appraisal_id="",
            plan_id=plan.plan_id, deontic_status=DeonticStatus.PERMITTED,
            utility_estimate=1.0, uncertainty=0.0,
            ranking_score=1.0, notes="ok",
        )
        orch._authorize_fn = lambda plan, g, sr: [
            ActionGrant(
                grant_id="gr", operator_id="noop", operator_revision=1,
                tool_name="noop", canonical_args="{}",
                target=plan.steps[0].target, goal_id=plan.goal_id,
                goal_revision=plan.goal_revision, state_revision=0,
                idempotency_key="", nonce="", issued_at=now_iso(),
                expires_at=now_iso(),
            )
        ]
        orch._dispatch_fn = lambda grant: DispatchResult(
            grant_id=grant.grant_id, success=True,
            effect_record=EffectRecord(
                record_id="e", grant_id=grant.grant_id,
                operator_id=grant.operator_id, target=grant.target,
                observed_at=now_iso(),
                effect_class=EffectClass.IRREVERSIBLE,
                details={"description": "noop"},
            ),
        )
        orch._evaluate_fn = counting_evaluate

        results = orch.run(max_cycles=10)
        # Should stop after 2nd cycle returns SATISFIED
        assert len(results) <= 3
        assert orch.goal is None

    def test_no_goal_returns_error(self):
        """Cycle with no goal returns 'No active goal' error."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "test", source_type="recalled")
        orch = create_integrated_orchestrator(store, None, b"s", use_pln=False)
        result = orch.run_cycle()
        assert result.error == "No active goal"
        assert result.stopped is False

    def test_forbidden_plan_not_executed(self):
        """Plan with FORBIDDEN deontic status should not be dispatched."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "test", source_type="recalled")
        goal = GoalSpec(
            goal_id="g_forbid", revision=1, owner="t",
            description="test", priority=1, status=GoalStatus.ACTIVE,
        )
        orch = create_integrated_orchestrator(store, goal, b"s", use_pln=False)
        orch._plan_fn = lambda obs, beliefs, g: [
            Plan(
                plan_id="p1", goal_id=g.goal_id, goal_revision=g.revision,
                state_revision=0,
                steps=[PlanStep(step_id="s1", operator_id="dangerous",
                               operator_revision=1, arguments={},
                               target=g.goal_id)],
                terminal_predicate="done",
            )
        ]
        orch._validate_fn = lambda plan, obs: Appraisal(
            appraisal_id="",
            plan_id=plan.plan_id, deontic_status=DeonticStatus.FORBIDDEN,
            utility_estimate=1.0, uncertainty=0.0,
            ranking_score=0.0, rejection_reason="too risky",
        )

        result = orch.run_cycle()
        assert len(result.plans) == 1
        assert len(result.appraisals) == 1
        assert result.appraisals[0].deontic_status == DeonticStatus.FORBIDDEN
        # No grants or dispatches for forbidden plans
        assert len(result.grants) == 0
        assert len(result.dispatches) == 0
