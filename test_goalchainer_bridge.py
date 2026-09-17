"""Dedicated tests for goalchainer_bridge.py."""
import pytest
from wmtm.store import WMTMStore
from wmtm.goalchainer_bridge import (
    GoalChainerDecision,
    wmtm_to_goalchainer_evidence,
    goalchainer_result_to_candidates,
    run_goalchainer_over_wmtm,
)


def _seed_store(n=3, capacity=200):
    store = WMTMStore(capacity=capacity)
    for i in range(n):
        store.admit(f"item-{i}", f"evidence item {i}", initial_sti=2.0 + i)
    return store


class TestGoalChainerDecision:
    def test_defaults(self):
        d = GoalChainerDecision(action_id="a1", label="act", status="recommended", score=0.8)
        assert d.evidence_ids == []
        assert d.motivation == ""
        assert d.score == 0.8


class TestWmtmToGoalchainerEvidence:
    def test_converts_active_set(self):
        store = _seed_store(3)
        ev = wmtm_to_goalchainer_evidence(store)
        assert len(ev) == 3
        assert all("id" in e and "content" in e and "sti" in e for e in ev)

    def test_respects_max_items(self):
        store = _seed_store(10)
        ev = wmtm_to_goalchainer_evidence(store, max_items=3)
        assert len(ev) == 3

    def test_empty_store(self):
        store = WMTMStore(capacity=10)
        ev = wmtm_to_goalchainer_evidence(store)
        assert ev == []


class TestGoalchainerResultToCandidates:
    def test_basic_conversion(self):
        result = {
            "decisions": [
                {"action_id": "a1", "label": "Act1", "status": "recommended", "score": 0.9},
                {"action_id": "a2", "label": "Act2", "status": "forbidden", "score": 0.1},
            ]
        }
        cands = goalchainer_result_to_candidates(result)
        assert len(cands) == 2
        assert cands[0].inference_type == "goalchainer_decision"
        assert cands[0].confidence == 0.9
        assert cands[1].confidence == 0.1

    def test_recommended_sti_boost(self):
        result = {"decisions": [{"action_id": "a1", "label": "Act", "status": "recommended", "score": 0.8}]}
        cands = goalchainer_result_to_candidates(result)
        # recommended => sti_boost = 2.0 => initial_sti = 0.8 * 2.0 * 10.0 = 16.0
        assert cands[0].initial_sti == pytest.approx(16.0)

    def test_forbidden_sti_penalty(self):
        result = {"decisions": [{"action_id": "a1", "label": "Act", "status": "forbidden", "score": 0.5}]}
        cands = goalchainer_result_to_candidates(result)
        # forbidden => sti_boost = 0.3 => initial_sti = 0.5 * 0.3 * 10.0 = 1.5
        assert cands[0].initial_sti == pytest.approx(1.5)

    def test_empty_decisions(self):
        cands = goalchainer_result_to_candidates({"decisions": []})
        assert cands == []

    def test_source_ids_propagated(self):
        result = {"decisions": [{"action_id": "a1", "label": "Act", "status": "permitted", "score": 0.5}]}
        cands = goalchainer_result_to_candidates(result, source_ids=["x", "y"])
        assert cands[0].derived_from == ["x", "y"]

    def test_missing_fields_use_defaults(self):
        result = {"decisions": [{}]}
        cands = goalchainer_result_to_candidates(result)
        assert len(cands) == 1
        assert cands[0].confidence == 0.5  # default score


class TestRunGoalchainerOverWmtm:
    def test_empty_store_returns_empty(self):
        store = WMTMStore(capacity=10)
        cands = run_goalchainer_over_wmtm(store, "test request")
        assert cands == []
