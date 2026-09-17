"""Tests for GoalChainer bridge and orchestrator feedback loop."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wmtm.store import WMTMStore
from wmtm.orchestrator import WMTMOrchestrator, CycleResult
from wmtm.goalchainer_bridge import (
    GoalChainerDecision,
    wmtm_to_goalchainer_evidence,
    goalchainer_result_to_candidates,
    run_goalchainer_over_wmtm,
)


class TestGoalChainerBridgeConversion:
    """Test conversion functions without needing real GoalChainer."""

    def test_result_to_candidates_basic(self):
        mock_result = {
            "decisions": [
                {"action_id": "act-1", "label": "Investigate", "status": "recommended", "score": 0.9},
                {"action_id": "act-2", "label": "Ignore", "status": "forbidden", "score": 0.1},
            ],
        }
        candidates = goalchainer_result_to_candidates(mock_result, source_ids=["s1", "s2"])
        assert len(candidates) == 2
        assert candidates[0].inference_type == "goalchainer_decision"
        assert candidates[0].confidence == 0.9
        assert candidates[0].derived_from == ["s1", "s2"]
        assert candidates[1].confidence == 0.1

    def test_result_to_candidates_empty(self):
        result = goalchainer_result_to_candidates({"decisions": []})
        assert result == []

    def test_result_to_candidates_missing_fields(self):
        mock = {"decisions": [{}]}
        candidates = goalchainer_result_to_candidates(mock)
        assert len(candidates) == 1
        assert candidates[0].confidence == 0.5
        assert "action-0" in candidates[0].content

    def test_sti_boost_for_recommended(self):
        mock = {
            "decisions": [
                {"action_id": "a", "label": "x", "status": "recommended", "score": 0.8},
                {"action_id": "b", "label": "y", "status": "forbidden", "score": 0.8},
            ],
        }
        candidates = goalchainer_result_to_candidates(mock)
        assert candidates[0].initial_sti > candidates[1].initial_sti

    def test_confidence_clamped(self):
        mock = {
            "decisions": [
                {"action_id": "a", "label": "x", "status": "ok", "score": 5.0},
                {"action_id": "b", "label": "y", "status": "ok", "score": -2.0},
            ],
        }
        candidates = goalchainer_result_to_candidates(mock)
        assert candidates[0].confidence == 1.0
        assert candidates[1].confidence == 0.0


class TestWMTMToGoalChainerEvidence:
    """Test evidence extraction from WMTM."""

    def test_empty_store_produces_empty_evidence(self):
        store = WMTMStore(capacity=10)
        evidence = wmtm_to_goalchainer_evidence(store)
        assert evidence == []

    def test_non_empty_store_produces_evidence(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "The sky is blue")
        store.admit("i2", "Grass is green")
        evidence = wmtm_to_goalchainer_evidence(store)
        assert len(evidence) == 2
        assert "content" in evidence[0]
        assert "id" in evidence[0]
        assert "sti" in evidence[0]

    def test_max_items_limits_evidence(self):
        store = WMTMStore(capacity=20)
        for i in range(10):
            store.admit(f"i{i}", f"Fact {i}")
        evidence = wmtm_to_goalchainer_evidence(store, max_items=3)
        assert len(evidence) == 3


class TestRunGoalChainerOverWMTM:
    """Test the full bridge function (returns [] if GoalChainer not available)."""

    def test_empty_store_returns_empty(self):
        store = WMTMStore(capacity=10)
        result = run_goalchainer_over_wmtm(store, "test request")
        assert result == []

    def test_non_empty_store_no_crash(self, monkeypatch):
        """Should not crash even if GoalChainer import fails.
        Mocks the internal run_goalchainer to avoid slow real calls."""
        store = WMTMStore(capacity=10)
        store.admit("i1", "Some fact")

        import wmtm.goalchainer_bridge as gb
        monkeypatch.setattr(gb, "run_goalchainer", lambda *a, **kw: None)

        result = run_goalchainer_over_wmtm(store, "test request")
        assert isinstance(result, list)
        assert result == []  # None result means no candidates


class TestOrchestratorGoalChainerIntegration:
    """Test orchestrator step 2c integration."""

    def test_cycle_result_has_gc_admitted(self):
        r = CycleResult(cycle=0)
        assert hasattr(r, "gc_admitted")
        assert r.gc_admitted == 0

    def test_orchestrator_default_goalchainer_disabled(self):
        store = WMTMStore(capacity=10)
        orch = WMTMOrchestrator(store)
        assert orch.use_goalchainer is False
        assert orch.goalchainer_request == ""

    def test_orchestrator_can_enable_goalchainer(self):
        store = WMTMStore(capacity=10)
        orch = WMTMOrchestrator(store, use_goalchainer=True, goalchainer_request="resolve incident")
        assert orch.use_goalchainer is True
        assert orch.goalchainer_request == "resolve incident"

    def test_disabled_goalchainer_produces_zero_gc_admitted(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "A fact")
        orch = WMTMOrchestrator(store)
        result = orch.cycle()
        assert result.gc_admitted == 0

    def test_enabled_goalchainer_empty_store_zero_admitted(self):
        store = WMTMStore(capacity=10)
        orch = WMTMOrchestrator(store, use_goalchainer=True, goalchainer_request="test")
        result = orch.cycle()
        assert result.gc_admitted == 0

    def test_enabled_goalchainer_nonempty_store_no_crash(self, monkeypatch):
        """GoalChainer may or may not be importable, but cycle should not crash.
        Mocks the actual call to avoid slow real pipeline invocation."""
        store = WMTMStore(capacity=10)
        store.admit("i1", "A fact")
        orch = WMTMOrchestrator(store, use_goalchainer=True, goalchainer_request="test")

        # Mock to return empty list (simulates GoalChainer unavailable)
        import wmtm.goalchainer_bridge
        monkeypatch.setattr(
            wmtm.goalchainer_bridge, "run_goalchainer_over_wmtm",
            lambda store, request, max_evidence=20: []
        )

        result = orch.cycle()
        assert isinstance(result, CycleResult)
        assert result.gc_admitted >= 0

    def test_enabled_goalchainer_admits_mocked_candidates(self, monkeypatch):
        """When GoalChainer returns candidates, orchestrator admits them."""
        store = WMTMStore(capacity=10)
        store.admit("i1", "A fact")
        orch = WMTMOrchestrator(store, use_goalchainer=True, goalchainer_request="test")

        from wmtm.inference import InferenceCandidate
        def mock_run(store, request, max_evidence=20):
            return [InferenceCandidate(
                content="Mocked GC decision: investigate",
                confidence=0.9,
                derived_from=["i1"],
                inference_type="goalchainer_decision",
                triple=None,
                initial_sti=18.0,
            )]

        import wmtm.goalchainer_bridge
        monkeypatch.setattr(wmtm.goalchainer_bridge, "run_goalchainer_over_wmtm", mock_run)

        result = orch.cycle()
        assert isinstance(result, CycleResult)
        assert result.gc_admitted >= 1  # at least one candidate admitted

    def test_multi_cycle_stability_with_goalchainer(self, monkeypatch):
        """Multiple cycles with GoalChainer enabled should not crash.
        Mocks the actual GoalChainer call to avoid slow imports."""
        store = WMTMStore(capacity=10)
        store.admit("i1", "Fact A")
        store.admit("i2", "Fact B")
        orch = WMTMOrchestrator(store, use_goalchainer=True, goalchainer_request="test")

        # Mock run_goalchainer_over_wmtm to return a fake candidate
        def mock_run(store, request, max_evidence=20):
            from wmtm.inference import InferenceCandidate
            return [InferenceCandidate(
                content="Mocked GC decision",
                confidence=0.8,
                derived_from=["i1", "i2"],
                inference_type="goalchainer_decision",
                triple=None,
                initial_sti=16.0,
            )]

        import wmtm.goalchainer_bridge
        monkeypatch.setattr(wmtm.goalchainer_bridge, "run_goalchainer_over_wmtm", mock_run)

        for i in range(3):
            result = orch.cycle()
            assert isinstance(result, CycleResult)

    def test_pln_and_goalchainer_coexist(self, monkeypatch):
        store = WMTMStore(capacity=10)
        store.admit("i1", "Fact A")
        orch = WMTMOrchestrator(
            store, use_pln=True, use_goalchainer=True, goalchainer_request="test"
        )

        from wmtm.inference import InferenceCandidate
        def mock_run(store, request, max_evidence=20):
            return [InferenceCandidate(
                content="Mocked GC decision",
                confidence=0.8,
                derived_from=["i1"],
                inference_type="goalchainer_decision",
                triple=None,
                initial_sti=16.0,
            )]

        import wmtm.goalchainer_bridge
        monkeypatch.setattr(wmtm.goalchainer_bridge, "run_goalchainer_over_wmtm", mock_run)

        result = orch.cycle()
        assert isinstance(result, CycleResult)
        assert hasattr(result, "pln_admitted")
        assert hasattr(result, "gc_admitted")
