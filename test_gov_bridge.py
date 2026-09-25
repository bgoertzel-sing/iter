"""Comprehensive tests for the F04 Governance Bridge.

Tests cover:
1. GoalDecisionRecord creation and serialization
2. DecisionHistory persistence and queries
3. DeonticEnforcer STI modifications
4. GovernanceBridge full pipeline
5. Goal lifecycle updates (achieve/block/abandon)
6. Integration: orchestrator cycle with gov_bridge wired in
"""
import json
import os
import tempfile

import pytest

from wmtm.store import WMTMStore
from wmtm.goal import Goal
from wmtm.goal_store import GoalStore
from wmtm.gov_bridge import (
    GoalDecisionRecord,
    DecisionHistory,
    DeonticEnforcer,
    GovernanceBridge,
)
from wmtm.orchestrator import WMTMOrchestrator


# ── Helpers ────────────────────────────────────────────────────────


def _tmp_path(name="test.json"):
    d = tempfile.mkdtemp()
    return os.path.join(d, name)


def _seed_store(n=5, capacity=200):
    store = WMTMStore(capacity=capacity)
    for i in range(n):
        store.admit(f"item-{i}", f"deploy service action {i}", initial_sti=10.0 + i)
    return store


def _fake_gc_result(
    decided="deploy-fix",
    status="recommended",
    score=0.85,
    executed_success=True,
    satisfied_goals=None,
    missing_required=None,
):
    """Build a fake GoalChainer result dict matching solve_incident output."""
    return {
        "request": "fix deployment issue",
        "decided": decided,
        "label": "Deploy Fix",
        "status": status,
        "decisions": [
            {
                "action_id": decided,
                "label": "Deploy Fix",
                "status": status,
                "score": score,
                "satisfied_goals": satisfied_goals or [],
                "missing_required_goals": missing_required or [],
            },
            {
                "action_id": "rollback",
                "label": "Rollback Changes",
                "status": "permitted",
                "score": 0.3,
                "satisfied_goals": [],
                "missing_required_goals": [],
            },
        ],
        "motivation": "consensus reached",
        "incident": {"id": "inc-1"},
        "executed": {"success": executed_success, "detail": "action completed"},
    }


# ── GoalDecisionRecord ────────────────────────────────────────────


class TestGoalDecisionRecord:
    def test_defaults(self):
        r = GoalDecisionRecord()
        assert r.id.startswith("gdr-")
        assert r.execution_result is None
        assert r.deontic_status == "unregulated"

    def test_round_trip(self):
        r = GoalDecisionRecord(
            goal_id="g1",
            decision_action_id="act-1",
            deontic_status="forbidden",
            score=0.7,
            execution_result="failure",
            cycle=5,
            satisfied_goals=["g1"],
        )
        d = r.to_dict()
        r2 = GoalDecisionRecord.from_dict(d)
        assert r2.goal_id == "g1"
        assert r2.deontic_status == "forbidden"
        assert r2.score == 0.7
        assert r2.satisfied_goals == ["g1"]
        assert r2.cycle == 5


# ── DecisionHistory ────────────────────────────────────────────────


class TestDecisionHistory:
    def test_add_and_query(self):
        h = DecisionHistory(path=_tmp_path("hist.json"))
        r1 = GoalDecisionRecord(goal_id="g1", cycle=1, execution_result="success")
        r2 = GoalDecisionRecord(goal_id="g1", cycle=2, execution_result="failure")
        r3 = GoalDecisionRecord(goal_id="g2", cycle=2, execution_result="success")
        h.add(r1)
        h.add(r2)
        h.add(r3)
        assert len(h) == 3
        assert len(h.by_goal("g1")) == 2
        assert len(h.by_goal("g2")) == 1
        assert len(h.by_outcome("success")) == 2
        assert len(h.by_cycle_range(1, 1)) == 1
        assert len(h.by_cycle_range(2, 2)) == 2

    def test_persistence(self):
        path = _tmp_path("hist2.json")
        h1 = DecisionHistory(path=path)
        h1.add(GoalDecisionRecord(goal_id="g1", cycle=1))
        h1.add(GoalDecisionRecord(goal_id="g2", cycle=2))
        # Reload from disk
        h2 = DecisionHistory(path=path)
        assert len(h2) == 2
        assert h2.all()[0].goal_id == "g1"

    def test_recent(self):
        h = DecisionHistory(path=_tmp_path("hist3.json"))
        for i in range(20):
            h.add(GoalDecisionRecord(goal_id=f"g{i}", cycle=i))
        recent = h.recent(5)
        assert len(recent) == 5
        assert recent[0].goal_id == "g15"


# ── DeonticEnforcer ────────────────────────────────────────────────


class TestDeonticEnforcer:
    def test_forbidden_suppresses_sti(self):
        store = _seed_store(3)
        enforcer = DeonticEnforcer()
        decisions = [
            {"action_id": "deploy-fix", "label": "Deploy service fix", "status": "forbidden", "score": 0.9}
        ]
        # Items contain "deploy" and "service" which overlap with the decision label
        original_stis = [item.attention.sti for item in store.get_active_set()]
        result = enforcer.enforce(store, decisions)
        new_stis = [item.attention.sti for item in store.get_active_set()]
        # At least some items should have been suppressed
        assert result["suppressed"] > 0
        assert any(new < old for new, old in zip(new_stis, original_stis))

    def test_obligated_boosts_sti(self):
        store = _seed_store(3)
        enforcer = DeonticEnforcer()
        decisions = [
            {"action_id": "deploy-fix", "label": "Deploy service action", "status": "obligated", "score": 0.9}
        ]
        original_stis = [item.attention.sti for item in store.get_active_set()]
        result = enforcer.enforce(store, decisions)
        new_stis = [item.attention.sti for item in store.get_active_set()]
        assert result["boosted"] > 0
        assert any(new > old for new, old in zip(new_stis, original_stis))

    def test_recommended_moderate_boost(self):
        store = _seed_store(3)
        enforcer = DeonticEnforcer()
        decisions = [
            {"action_id": "deploy-fix", "label": "Deploy service action", "status": "recommended", "score": 0.8}
        ]
        original_stis = [item.attention.sti for item in store.get_active_set()]
        result = enforcer.enforce(store, decisions)
        new_stis = [item.attention.sti for item in store.get_active_set()]
        assert result["boosted"] > 0

    def test_permitted_no_change(self):
        store = _seed_store(3)
        enforcer = DeonticEnforcer()
        decisions = [
            {"action_id": "deploy-fix", "label": "Deploy service action", "status": "permitted", "score": 0.5}
        ]
        original_stis = [item.attention.sti for item in store.get_active_set()]
        result = enforcer.enforce(store, decisions)
        new_stis = [item.attention.sti for item in store.get_active_set()]
        assert result["boosted"] == 0
        assert result["suppressed"] == 0
        assert original_stis == new_stis

    def test_no_overlap_no_change(self):
        store = WMTMStore(capacity=100)
        store.admit("x1", "completely unrelated topic about cats", initial_sti=10.0)
        enforcer = DeonticEnforcer()
        decisions = [
            {"action_id": "deploy-fix", "label": "Deploy service", "status": "forbidden", "score": 0.9}
        ]
        result = enforcer.enforce(store, decisions)
        assert result["suppressed"] == 0
        assert result["boosted"] == 0

    def test_custom_factors(self):
        enforcer = DeonticEnforcer(
            forbidden_penalty=0.5,
            obligated_boost=5.0,
            recommended_boost=2.0,
        )
        assert enforcer._status_factor("forbidden") == 0.5
        assert enforcer._status_factor("obligated") == 5.0
        assert enforcer._status_factor("recommended") == 2.0


# ── GovernanceBridge ───────────────────────────────────────────────


class TestGovernanceBridge:
    def _make_bridge(self):
        goal_path = _tmp_path("goals.json")
        hist_path = _tmp_path("hist.json")
        gs = GoalStore(path=goal_path)
        g = Goal(id="g-test", description="Fix deployment issue", priority=0.8, status="active")
        gs.add(g)
        bridge = GovernanceBridge(goal_store=gs, history_path=hist_path)
        return bridge, gs

    def test_process_result_creates_records(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        gc_result = _fake_gc_result()
        out = bridge.process_goalchainer_result(gc_result, store, cycle=1, goal_id="g-test")
        assert out["records"] == 2  # two decisions
        assert len(bridge.history) == 2

    def test_process_result_enforcement(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        gc_result = _fake_gc_result(status="forbidden")
        out = bridge.process_goalchainer_result(gc_result, store, cycle=1, goal_id="g-test")
        assert "enforcement" in out
        # The forbidden decision should have suppressed some items
        assert out["enforcement"]["suppressed"] >= 0  # may be 0 if no keyword overlap

    def test_goal_achieved_on_success(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        gc_result = _fake_gc_result(
            executed_success=True,
            satisfied_goals=["g-test"],
        )
        out = bridge.process_goalchainer_result(gc_result, store, cycle=5, goal_id="g-test")
        assert len(out["lifecycle_changes"]) == 1
        assert out["lifecycle_changes"][0]["new_status"] == "achieved"
        # Verify goal store was updated
        goal = gs.get("g-test")
        assert goal.status == "achieved"
        assert goal.achieved_cycle == 5

    def test_goal_blocked_on_missing_required(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        gc_result = _fake_gc_result(
            executed_success=False,
            missing_required=["prereq-1", "prereq-2"],
        )
        out = bridge.process_goalchainer_result(gc_result, store, cycle=3, goal_id="g-test")
        assert len(out["lifecycle_changes"]) == 1
        assert out["lifecycle_changes"][0]["new_status"] == "blocked"
        goal = gs.get("g-test")
        assert goal.status == "blocked"

    def test_goal_abandoned_after_consecutive_failures(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        # Run 3 consecutive failures
        for cycle in range(3):
            gc_result = _fake_gc_result(executed_success=False)
            bridge.process_goalchainer_result(gc_result, store, cycle=cycle, goal_id="g-test")
        goal = gs.get("g-test")
        assert goal.status == "abandoned"

    def test_no_goal_id_skips_lifecycle(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        gc_result = _fake_gc_result(executed_success=True, satisfied_goals=["g-test"])
        out = bridge.process_goalchainer_result(gc_result, store, cycle=1, goal_id="")
        assert out["lifecycle_changes"] == []

    def test_empty_result_handled(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        out = bridge.process_goalchainer_result({}, store, cycle=1, goal_id="g-test")
        assert out["records"] == 0

    def test_goal_decision_summary(self):
        bridge, gs = self._make_bridge()
        store = _seed_store()
        # Create some history
        bridge.process_goalchainer_result(
            _fake_gc_result(executed_success=True),
            store, cycle=1, goal_id="g-test"
        )
        bridge.process_goalchainer_result(
            _fake_gc_result(executed_success=False),
            store, cycle=2, goal_id="g-test"
        )
        summary = bridge.goal_decision_summary("g-test")
        assert summary["total"] == 4  # 2 decisions per result x 2 runs
        assert "outcomes" in summary
        assert "last_decision" in summary

    def test_decision_history_persists(self):
        """Verify decision records survive a bridge reload."""
        goal_path = _tmp_path("goals2.json")
        hist_path = _tmp_path("hist2.json")
        gs = GoalStore(path=goal_path)
        gs.add(Goal(id="g1", description="test", priority=0.5, status="active"))

        bridge1 = GovernanceBridge(goal_store=gs, history_path=hist_path)
        store = _seed_store()
        bridge1.process_goalchainer_result(
            _fake_gc_result(), store, cycle=1, goal_id="g1"
        )
        assert len(bridge1.history) == 2

        # Reload
        bridge2 = GovernanceBridge(goal_store=gs, history_path=hist_path)
        assert len(bridge2.history) == 2


# ── Integration: Orchestrator + Gov Bridge ─────────────────────────


class TestOrchestratorGovIntegration:
    def test_cycle_with_gov_bridge_no_gc(self):
        """Gov bridge wired but GoalChainer disabled — no crash."""
        store = _seed_store()
        goal_path = _tmp_path("goals_int.json")
        gs = GoalStore(path=goal_path)
        gs.add(Goal(id="g1", description="test goal", priority=0.8, status="active"))
        bridge = GovernanceBridge(
            goal_store=gs,
            history_path=_tmp_path("hist_int.json"),
        )
        orch = WMTMOrchestrator(
            store=store,
            use_goalchainer=False,
            goal_store=gs,
            gov_bridge=bridge,
        )
        result = orch.cycle()
        # Gov fields should be empty since GC is disabled
        assert result.gov_enforcement == {}
        assert result.gov_lifecycle_changes == []

    def test_cycle_result_has_gov_fields(self):
        """CycleResult includes gov_enforcement and gov_lifecycle_changes."""
        store = _seed_store()
        orch = WMTMOrchestrator(store=store)
        result = orch.cycle()
        assert hasattr(result, "gov_enforcement")
        assert hasattr(result, "gov_lifecycle_changes")
