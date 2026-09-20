"""Tests for WP8: real-component integration adapters."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wmtm.store import WMTMStore
from wmtm.goal import Goal as WMTMGoal

from control.types import (
    GoalSpec, GoalStatus, Observation, Belief, Plan,
    ActionOperator, Effect, EffectClass, PlanStep,
    now_iso,
)
from control.orchestrator import Orchestrator, CycleResult
from control.dispatcher import DispatchResult, Dispatcher
from control.plan_validator import PlanValidator
from control.reference_monitor import ReferenceMonitor
from control.event_store import EventStore
from control.wmtm_adapters import (
    WMTMObserverAdapter,
    PLNBeliefAdapter,
    GoalChainerPlanAdapter,
    RealValidateAdapter,
    RealAuthorizeAdapter,
    RealDispatchAdapter,
    RealEvaluateAdapter,
    wmtm_goal_to_spec,
    spec_to_wmtm_goal,
    create_integrated_orchestrator,
)


class TestGoalBridge:
    def test_wmtm_goal_to_spec(self):
        g = WMTMGoal(id="g1", description="test goal", priority=0.8, status="active")
        spec = wmtm_goal_to_spec(g)
        assert spec.goal_id == "g1"
        assert spec.description == "test goal"
        assert spec.priority == 8
        assert spec.status == GoalStatus.ACTIVE

    def test_wmtm_goal_to_spec_pending(self):
        g = WMTMGoal(id="g2", description="pending", status="pending")
        spec = wmtm_goal_to_spec(g)
        assert spec.status == GoalStatus.DRAFT

    def test_wmtm_goal_to_spec_achieved(self):
        g = WMTMGoal(id="g3", description="done", status="achieved")
        spec = wmtm_goal_to_spec(g)
        assert spec.status == GoalStatus.SATISFIED

    def test_spec_to_wmtm_goal(self):
        spec = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=5, status=GoalStatus.ACTIVE)
        g = spec_to_wmtm_goal(spec)
        assert g.id == "g1"
        assert g.description == "test"
        assert g.priority == 0.5
        assert g.status == "active"

    def test_spec_to_wmtm_goal_satisfied(self):
        spec = GoalSpec(goal_id="g2", revision=1, owner="t",
                       description="done", priority=3, status=GoalStatus.SATISFIED)
        g = spec_to_wmtm_goal(spec)
        assert g.status == "achieved"

    def test_roundtrip(self):
        g = WMTMGoal(id="g1", description="rt", priority=0.6, status="active")
        spec = wmtm_goal_to_spec(g)
        g2 = spec_to_wmtm_goal(spec)
        assert g2.id == g.id
        assert g2.description == g.description
        assert g2.status == g.status


class TestWMTMObserverAdapter:
    def test_empty_store(self):
        store = WMTMStore(capacity=10)
        adapter = WMTMObserverAdapter(store)
        assert adapter(state_revision=0) == []

    def test_returns_observations(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "CPU 95%", source_type="recalled")
        store.admit("i2", "prod-web-01", source_type="recalled")
        adapter = WMTMObserverAdapter(store)
        obs = adapter(state_revision=0)
        assert len(obs) == 2
        assert all(isinstance(o, Observation) for o in obs)
        assert obs[0].source == "wmtm"
        assert obs[0].schema_version == "v1"

    def test_correct_revision(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "content", source_type="recalled")
        adapter = WMTMObserverAdapter(store)
        obs = adapter(state_revision=42)
        assert obs[0].state_revision == 42

    def test_content_metadata(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "content here", source_type="recalled")
        adapter = WMTMObserverAdapter(store)
        obs = adapter(state_revision=0)
        assert obs[0].content["content"] == "content here"
        assert "source_type" in obs[0].content
        assert "sti" in obs[0].content


class TestPLNBeliefAdapter:
    def test_no_pln(self):
        store = WMTMStore(capacity=10)
        adapter = PLNBeliefAdapter(store, use_pln=False)
        assert adapter([]) == []

    def test_empty_store(self):
        store = WMTMStore(capacity=10)
        adapter = PLNBeliefAdapter(store, use_pln=True)
        assert adapter([]) == []

    def test_failure_graceful(self):
        store = WMTMStore(capacity=10)
        adapter = PLNBeliefAdapter(store, use_pln=True)
        adapter._store = None
        assert adapter([]) == []


class TestGoalChainerPlanAdapter:
    def test_empty_store(self):
        store = WMTMStore(capacity=10)
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1)
        adapter = GoalChainerPlanAdapter(store, "request")
        assert adapter([], [], goal) == []


class TestRealEvaluateAdapter:
    def test_default_active(self):
        adapter = RealEvaluateAdapter()
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1)
        assert adapter([], goal) == GoalStatus.ACTIVE

    def test_custom_check(self):
        adapter = RealEvaluateAdapter(
            success_check=lambda e, g: GoalStatus.SATISFIED
        )
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1)
        assert adapter([], goal) == GoalStatus.SATISFIED


class TestRealValidateAdapter:
    def test_wraps_validator(self):
        validator = PlanValidator()
        adapter = RealValidateAdapter(validator)
        assert adapter._validator is validator


class TestCreateIntegratedOrchestrator:
    def test_creates_with_goal(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "test", source_type="recalled")
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1, status=GoalStatus.ACTIVE)
        orch = create_integrated_orchestrator(store, goal, b"secret")
        assert orch.goal is goal
        assert not orch.stopped

    def test_cycle_empty_plans(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "test", source_type="recalled")
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1, status=GoalStatus.ACTIVE)
        orch = create_integrated_orchestrator(store, goal, b"secret", use_pln=False)
        result = orch.run_cycle()
        assert isinstance(result, CycleResult)
        assert len(result.plans) == 0


    def test_cycle_with_mocked_plan(self):
        store = WMTMStore(capacity=20)
        store.admit("f1", "CPU at 95%", source_type="recalled")
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="Investigate CPU", priority=1,
                       status=GoalStatus.ACTIVE)
        orch = create_integrated_orchestrator(store, goal, b"secret", use_pln=False)
        def mock_plan(observations, beliefs, goal):
            step = PlanStep(step_id="s1", operator_id="noop",
                           operator_revision=1, arguments={"q": "cpu"},
                           target=goal.goal_id)
            state_rev = observations[0].state_revision if observations else 0
            return [Plan(plan_id="p1", goal_id=goal.goal_id,
                        goal_revision=goal.revision, state_revision=state_rev,
                        steps=[step], terminal_predicate="done")]
        orch._plan_fn = mock_plan
        orch._validate_fn._validator.register_operator(
            ActionOperator(operator_id="noop", revision=1,
                          parameter_schema="v1", precondition="")
        )
        result = orch.run_cycle()
        assert isinstance(result, CycleResult)
        assert len(result.plans) == 1
        assert result.goal_status == GoalStatus.ACTIVE


class TestFullIntegration:
    def test_shadow_mode_no_dispatch(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "test fact", source_type="recalled")
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1, status=GoalStatus.ACTIVE)
        orch = create_integrated_orchestrator(store, goal, b"secret", use_pln=False)
        result = orch.run_cycle()
        assert len(result.dispatches) == 0

    def test_stop_flag(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "test", source_type="recalled")
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1, status=GoalStatus.ACTIVE)
        orch = create_integrated_orchestrator(store, goal, b"secret")
        orch.stop()
        result = orch.run_cycle()
        assert result.stopped

    def test_multiple_cycles(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "test", source_type="recalled")
        goal = GoalSpec(goal_id="g1", revision=1, owner="t",
                       description="test", priority=1, status=GoalStatus.ACTIVE)
        orch = create_integrated_orchestrator(store, goal, b"secret", use_pln=False)
        for i in range(3):
            result = orch.run_cycle()
            assert isinstance(result, CycleResult)
        assert orch.cycle_count == 3

    def test_no_goal_no_op(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "test", source_type="recalled")
        orch = create_integrated_orchestrator(store, None, b"secret", use_pln=False)
        result = orch.run_cycle()
        assert result.error == "No active goal"
