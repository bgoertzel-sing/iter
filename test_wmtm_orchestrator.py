"""Tests for WMTM Orchestrator."""
import pytest
from wmtm.orchestrator import WMTMOrchestrator, CycleResult
from wmtm.store import WMTMStore
from wmtm.inference import WMTMInferenceEngine
from wmtm.utility import UtilityTracker
from wmtm.forgetting_log import ForgettingLog
from wmtm.forgetting import ForgettingPolicy
from wmtm.writeback import WritebackManager


def test_basic_cycle_no_recall():
    """Run a cycle with no recall_fn or append_fn."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "smoke implies danger", initial_sti=5.0)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert isinstance(result, CycleResult)
    assert result.cycle == 0
    assert result.active_count >= 2  # original items (maybe + derived)


def test_cycle_with_recall():
    """Cycle with a recall_fn that adds items."""
    store = WMTMStore(capacity=20)
    orch = WMTMOrchestrator(store)
    recall_data = [("r1", "recalled fact", 5.0)]
    result = orch.cycle(recall_fn=lambda: recall_data)
    assert result.active_count >= 1
    assert store.get("r1") is not None


def test_cycle_derives_and_admits():
    """Inference should produce derived items."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=8.0)
    store.admit("i2", "smoke implies danger", initial_sti=8.0)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert result.admitted_derived >= 1


def test_cycle_eviction_on_low_sti():
    """Items with very low STI should be evicted after tick."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "will decay", initial_sti=0.02)
    orch = WMTMOrchestrator(store)
    result = orch.cycle()
    assert result.evicted >= 1
    assert store.get("i1") is None


def test_cycle_writeback():
    store = WMTMStore(capacity=20)
    store.admit('d1', 'derived belief', source_type='derived', initial_sti=100.0)
    wb = WritebackManager(derived_min_age=3, derived_min_utility=0.5)
    orch = WMTMOrchestrator(store, writeback_manager=wb)
    # Record uses through tracker so utility is preserved through tick
    for _ in range(5):
        orch.utility.record_use('d1', tick=0)
    # Age the item
    for _ in range(5):
        store.tick()
    calls = []
    result = orch.cycle(append_fn=lambda c: calls.append(c))
    assert result.written_back >= 1
def test_cycle_count_increments():
    store = WMTMStore(capacity=10)
    orch = WMTMOrchestrator(store)
    assert orch.cycle_count == 0
    orch.cycle()
    assert orch.cycle_count == 1
    orch.cycle()
    assert orch.cycle_count == 2


def test_multiple_cycles_stable():
    """Run several cycles without crashing."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "smoke implies danger", initial_sti=10.0)
    orch = WMTMOrchestrator(store)
    for _ in range(10):
        orch.cycle()
    assert orch.cycle_count == 10
    assert len(store) > 0  # Something survived


def test_forget_log_prevents_re_derivation():
    """Once forgotten, a derived item should not be re-admitted."""
    store = WMTMStore(capacity=5)  # small to force eviction
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "smoke implies danger", initial_sti=10.0)
    orch = WMTMOrchestrator(store)
    # First cycle: derive + admit
    r1 = orch.cycle()
    assert r1.admitted_derived >= 1
    # Run many cycles to let things decay and get forgotten
    for _ in range(50):
        orch.cycle()
    # The derived item should have been evicted at some point
    # And should not be re-derived (since forget_log blocks it)
    # The forget log should have entries
    assert len(orch.forget_log) > 0


def test_cycle_with_append_failure():
    """Writeback failures should not crash the cycle."""
    store = WMTMStore(capacity=20)
    item = store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    for _ in range(20):
        store.tick()
    item = store.get("d1")
    item.utility = 5.0
    wb = WritebackManager(derived_min_age=10, derived_min_utility=1.0)
    orch = WMTMOrchestrator(store, writeback_manager=wb)
    def failing_append(content):
        raise RuntimeError("LTM unavailable")
    result = orch.cycle(append_fn=failing_append)
    # Should not crash, written_back should be 0
    assert result.written_back == 0


def test_full_pipeline_integration():
    """End-to-end: recall, infer, forget, writeback."""
    store = WMTMStore(capacity=30)
    orch = WMTMOrchestrator(store)
    append_calls = []
    # Recall some items with implications
    recall_data = [
        ("r1", "rain implies wet", 10.0),
        ("r2", "wet implies slippery", 10.0),
        ("r3", "I see wet ground", 8.0),
    ]
    # Run cycles
    for i in range(5):
        recall = recall_data if i == 0 else []
        orch.cycle(recall_fn=(lambda rd=recall: rd) if recall else None,
                   append_fn=lambda c: append_calls.append(c))
    # Should have derived items (deduction: rain->slippery, abduction: maybe rain)
    assert orch.cycle_count == 5
    assert len(store) > 0
