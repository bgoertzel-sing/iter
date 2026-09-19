import pytest
import tempfile
import os
from wmtm.goal import Goal
from wmtm.goal_store import GoalStore
from wmtm.orchestrator import WMTMOrchestrator
from wmtm.store import WMTMStore
from wmtm.attention import AttentionValue


class TestGoalOrchestratorIntegration:
    """M4: End-to-end tests for goal-driven WMTM orchestration."""

    @pytest.fixture
    def goal_store(self):
        p = tempfile.mktemp(suffix=".json")
        yield GoalStore(p)
        if os.path.exists(p):
            os.unlink(p)

    @pytest.fixture
    def orch(self, goal_store):
        return WMTMOrchestrator(
            store=WMTMStore(capacity=50),
            goal_store=goal_store,
            use_goalchainer=False,
        )

    def test_orchestrator_has_goal_store(self, orch, goal_store):
        assert orch.goal_store is goal_store

    def test_active_goal_none_when_empty(self, orch):
        assert orch.active_goal() is None

    def test_active_goal_returns_highest_priority(self, orch, goal_store):
        g1 = Goal(description="low priority task", priority=0.1)
        g2 = Goal(description="high priority task", priority=0.9)
        goal_store.add(g1)
        goal_store.add(g2)
        g1.activate(cycle=0)
        g2.activate(cycle=0)
        goal_store.update(g1)
        goal_store.update(g2)
        active = orch.active_goal()
        assert active.id == g2.id

    def test_goal_context_boost_increases_sti(self, orch, goal_store):
        item = orch.store.admit(item_id="i1", content="calibrate chemical reactions priority")
        initial_sti = item.attention.sti
        goal = Goal(description="calibrate chemical priority", priority=0.9)
        goal.activate(cycle=0)
        goal_store.add(goal)
        orch._goal_context_boost()
        assert item.attention.sti > initial_sti

    def test_goal_context_boost_no_goal_noop(self, orch):
        item = orch.store.admit(item_id="i1", content="some content")
        initial_sti = item.attention.sti
        orch._goal_context_boost()
        assert item.attention.sti == initial_sti

    def test_goal_lifecycle_abandons_expired(self, orch, goal_store):
        g = Goal(description="expired task", deadline_cycle=5, priority=0.5)
        goal_store.add(g)
        g.activate(cycle=0)
        goal_store.update(g)
        # Simulate cycles past deadline
        orch._cycle = 10
        orch._goal_lifecycle_check()
        assert goal_store.get(g.id).status == "abandoned"

    def test_goal_lifecycle_noop_without_store(self):
        orch = WMTMOrchestrator(store=WMTMStore(capacity=10), goal_store=None)
        orch._cycle = 100
        orch._goal_lifecycle_check()  # should not raise

    def test_goal_lifecycle_skips_achieved(self, orch, goal_store):
        g = Goal(description="done task", deadline_cycle=5, priority=0.5)
        goal_store.add(g)
        g.activate(cycle=0)
        g.achieve(cycle=3)
        goal_store.update(g)
        orch._cycle = 10
        orch._goal_lifecycle_check()
        assert goal_store.get(g.id).status == "achieved"

    def test_snapshot_includes_goal_store_path(self, orch, goal_store):
        snap = orch.snapshot_state()
        assert "goal_store_path" in snap
        assert snap["goal_store_path"] == goal_store.path

    def test_snapshot_goal_store_none(self):
        orch = WMTMOrchestrator(store=WMTMStore(capacity=10), goal_store=None)
        snap = orch.snapshot_state()
        assert snap["goal_store_path"] is None

    def test_cycle_runs_with_goal_store(self, orch, goal_store):
        item = orch.store.admit(item_id="i1", content="test content for processing")
        g = Goal(description="test content processing", priority=0.7)
        goal_store.add(g)
        g.activate(cycle=0)
        goal_store.update(g)
        result = orch.cycle()
        assert result is not None
        assert orch.cycle_count == 1
        # Item should have been boosted
        assert item.attention.sti > 0
