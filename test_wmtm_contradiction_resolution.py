"""Tests for contradiction resolution (Phase 5)."""
import pytest
from wmtm.inference import (
    WMTMInferenceEngine,
    ContradictionReport,
    ContradictionResolution,
)
from wmtm.store import WMTMStore
from wmtm.item import WMTMItem
from wmtm.attention import AttentionValue
from wmtm.orchestrator import WMTMOrchestrator, CycleResult
from wmtm.utility import UtilityTracker


# --- resolve_contradictions unit tests ---

def test_resolve_contradictions_basic():
    """Two contradicting items: higher composite score wins, loser penalized."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=3.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) >= 1

    resolutions = engine.resolve_contradictions(store, reports)
    assert len(resolutions) >= 1
    res = resolutions[0]
    assert res.winner_id == "i1"
    assert "i2" in res.loser_ids
    assert res.action == "penalize"
    assert res.strategy == "composite"

    # Loser should still be in store (penalized, not evicted)
    assert store.get("i2") is not None
    # Winner should have boosted STI
    winner = store.get("i1")
    assert winner.attention.sti > 10.0
    # Loser should have reduced STI
    loser = store.get("i2")
    assert loser.attention.sti < 3.0


def test_resolve_contradictions_high_severity_evicts():
    """When severity >= threshold, losers are evicted."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=8.0)
    store.admit("i3", "fire implies heat", initial_sti=6.0)
    store.admit("i4", "fire implies light", initial_sti=4.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert reports[0].severity >= 0.9

    resolutions = engine.resolve_contradictions(store, reports)
    res = resolutions[0]
    assert res.action == "evict"
    assert len(res.evicted_ids) >= 3
    for eid in res.evicted_ids:
        assert store.get(eid) is None
    assert store.get(res.winner_id) is not None


def test_resolve_contradictions_utility_tiebreak():
    """When STI is equal, higher utility breaks the tie."""
    store = WMTMStore(capacity=20)
    item1 = store.admit("i1", "fire implies smoke", initial_sti=5.0)
    item2 = store.admit("i2", "fire implies danger", initial_sti=5.0)
    item2.utility = 10.0
    item1.utility = 0.0
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    resolutions = engine.resolve_contradictions(store, reports)
    res = resolutions[0]
    assert res.winner_id == "i2"
    assert res.winner_score > res.loser_scores[0]


def test_resolve_contradictions_no_contradictions():
    """No contradictions => no resolutions."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "water implies wet", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) == 0
    resolutions = engine.resolve_contradictions(store, reports)
    assert len(resolutions) == 0


def test_resolve_contradictions_custom_threshold():
    """Custom eviction severity threshold."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=3.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    resolutions = engine.resolve_contradictions(store, reports, eviction_severity_threshold=0.3)
    res = resolutions[0]
    assert res.action == "evict"
    assert len(res.evicted_ids) >= 1


def test_resolve_contradictions_returns_resolution_objects():
    """Return type should be list of ContradictionResolution."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    resolutions = engine.resolve_contradictions(store, reports)
    for res in resolutions:
        assert isinstance(res, ContradictionResolution)
        assert isinstance(res.report, ContradictionReport)


def test_resolve_contradictions_winner_boosted():
    """Winner should receive STI boost proportional to severity."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    initial_winner_sti = store.get("i1").attention.sti
    severity = reports[0].severity
    engine.resolve_contradictions(store, reports)
    final_winner_sti = store.get("i1").attention.sti
    assert final_winner_sti >= initial_winner_sti + severity * 2.0 * 0.95 - 0.01  # 5% consolidates to ATI


def test_resolve_contradictions_empty_store():
    """resolve_contradictions with empty store should not crash."""
    store = WMTMStore(capacity=20)
    engine = WMTMInferenceEngine()
    resolutions = engine.resolve_contradictions(store, [])
    assert len(resolutions) == 0


# --- Orchestrator integration tests ---

def test_orchestrator_resolves_contradictions():
    """Orchestrator cycle should populate result.resolutions when contradictions exist."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=3.0)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert len(result.contradictions) >= 1
    assert len(result.resolutions) >= 1
    res = result.resolutions[0]
    assert isinstance(res, ContradictionResolution)


def test_orchestrator_no_contradictions_no_resolutions():
    """No contradictions => no resolutions in cycle result."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "the sky is blue", initial_sti=10.0)
    store.admit("i2", "the grass is green", initial_sti=10.0)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert len(result.contradictions) == 0
    assert len(result.resolutions) == 0


def test_orchestrator_resolution_evicts_logged():
    """Items evicted by contradiction resolution should be logged in forget_log."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=100.0)
    store.admit("i2", "fire implies danger", initial_sti=80.0)
    store.admit("i3", "fire implies heat", initial_sti=60.0)
    store.admit("i4", "fire implies light", initial_sti=40.0)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert len(result.resolutions) >= 1
    evict_res = [r for r in result.resolutions if r.action == "evict"]
    if evict_res:
        assert len(orch.forget_log) > 0


def test_orchestrator_resolution_winner_survives():
    """After resolution, the winning item should still be in the store."""
    store = WMTMStore(capacity=20)
    store.admit("strong", "fire implies smoke", initial_sti=100.0)
    store.admit("weak", "fire implies danger", initial_sti=1.0)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    if result.resolutions:
        winner_id = result.resolutions[0].winner_id
        assert store.get(winner_id) is not None


def test_orchestrator_cycle_result_has_resolutions_field():
    """CycleResult should have a resolutions field."""
    cr = CycleResult(cycle=0)
    assert hasattr(cr, "resolutions")
    assert cr.resolutions == []


def test_orchestrator_multi_cycle_stability_with_contradictions():
    """Running multiple cycles with contradictions should not crash."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    store.admit("i3", "water implies wet", initial_sti=8.0)
    store.admit("i4", "water implies dry", initial_sti=4.0)
    orch = WMTMOrchestrator(store)
    for _ in range(10):
        result = orch.cycle()
        assert isinstance(result, CycleResult)
    assert len(store) <= 20


def test_resolution_does_not_evict_below_severity():
    """Low severity contradictions should penalize, not evict."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    # severity = 0.6 < 0.9 default threshold
    assert reports[0].severity < 0.9
    resolutions = engine.resolve_contradictions(store, reports)
    res = resolutions[0]
    assert res.action == "penalize"
    assert len(res.evicted_ids) == 0
    assert store.get("i2") is not None
    orch = WMTMOrchestrator(store)
    for i in range(3):
        result = orch.cycle()
        assert isinstance(result, CycleResult)
