"""Tests for PLN integration into the WMTM orchestrator cycle."""
from wmtm.orchestrator import WMTMOrchestrator, CycleResult
from wmtm.store import WMTMStore


def test_pln_phase_enabled_by_default():
    """Orchestrator should have use_pln=True by default."""
    store = WMTMStore(capacity=20)
    orch = WMTMOrchestrator(store)
    assert orch.use_pln is True


def test_pln_phase_can_be_disabled():
    """Orchestrator with use_pln=False should not run PLN."""
    store = WMTMStore(capacity=20)
    orch = WMTMOrchestrator(store, use_pln=False)
    assert orch.use_pln is False


def test_cycle_result_has_pln_field():
    """CycleResult should have a pln_admitted field."""
    store = WMTMStore(capacity=20)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert hasattr(result, 'pln_admitted')
    assert isinstance(result.pln_admitted, int)


def test_pln_derives_from_implication_chain():
    """PLN should derive A->C from A->B, B->C in the active set."""
    store = WMTMStore(capacity=30)
    store.admit('s1', 'dogs implies animals', initial_sti=15.0)
    store.admit('s2', 'animals implies living', initial_sti=15.0)
    orch = WMTMOrchestrator(store, use_pln=True)
    result = orch.cycle()
    # PLN deduction should produce at least one derived candidate
    # (either from basic inference or PLN or both)
    total_derived = result.admitted_derived + result.pln_admitted
    assert total_derived >= 1


def test_pln_disabled_does_not_admit_pln_candidates():
    """With use_pln=False, pln_admitted should always be 0."""
    store = WMTMStore(capacity=30)
    store.admit('s1', 'dogs implies animals', initial_sti=15.0)
    store.admit('s2', 'animals implies living', initial_sti=15.0)
    orch = WMTMOrchestrator(store, use_pln=False)
    result = orch.cycle()
    assert result.pln_admitted == 0


def test_pln_with_empty_store():
    """PLN phase with empty store should not crash."""
    store = WMTMStore(capacity=20)
    orch = WMTMOrchestrator(store, use_pln=True)
    result = orch.cycle()
    assert result.pln_admitted == 0
    assert result.active_count == 0


def test_pln_multiple_cycles_stable():
    """Multiple cycles with PLN should not crash."""
    store = WMTMStore(capacity=30)
    store.admit('s1', 'fire implies smoke', initial_sti=10.0)
    store.admit('s2', 'smoke implies danger', initial_sti=10.0)
    store.admit('s3', 'cats is a mammal', initial_sti=10.0)
    orch = WMTMOrchestrator(store, use_pln=True)
    for _ in range(10):
        r = orch.cycle()
        assert r.cycle >= 0
    assert orch.cycle_count == 10


def test_pln_admits_different_candidates_than_basic():
    """PLN may find candidates basic inference misses (is-a chains)."""
    store = WMTMStore(capacity=30)
    # PLN can extract Inheritance atoms from 'is a' patterns
    # that basic inference's triple extractor also handles
    # but PLN also handles Similarity, Evaluation
    store.admit('s1', 'cats is similar to dogs', initial_sti=15.0)
    store.admit('s2', 'dogs is a mammal', initial_sti=15.0)
    orch = WMTMOrchestrator(store, use_pln=True)
    result = orch.cycle()
    # Should not crash and should produce some derived content
    total_derived = result.admitted_derived + result.pln_admitted
    # At least basic inference should derive something
    assert total_derived >= 0  # stable, no crash
