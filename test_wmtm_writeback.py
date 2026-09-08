"""Tests for WMTM WritebackManager."""
from wmtm.writeback import WritebackManager, format_derived_metta, format_enrichment_metta
from wmtm.store import WMTMStore
from wmtm.item import WMTMItem
from wmtm.attention import AttentionValue


def test_format_derived_metta():
    item = WMTMItem(
        id="derived-deduction-0-0",
        content="fire implies danger",
        source_type="derived",
        derived_from=["i1", "i2"],
        attention=AttentionValue(sti=3.0),
        utility=5.0,
    )
    metta = format_derived_metta(item)
    assert "MemoryCluster" in metta
    assert "fire implies danger" in metta
    assert "DerivedFrom" in metta
    assert "i1" in metta and "i2" in metta
    assert "WMTMUtility" in metta


def test_format_enrichment_metta():
    item = WMTMItem(
        id="recalled-1",
        content="enriched observation",
        source_type="recalled",
        origin_cluster="cluster-abc",
        attention=AttentionValue(sti=2.0),
        utility=3.5,
        age=40,
    )
    metta = format_enrichment_metta(item)
    assert "MemoryCluster" in metta
    assert "enriched observation" in metta
    assert "WMTMUtility" in metta
    assert "WMTMAge" in metta


def test_select_derived_candidate():
    wb = WritebackManager(derived_min_age=10, derived_min_utility=1.0)
    store = WMTMStore(capacity=10)
    item = store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    # Age it
    for _ in range(15):
        store.tick()
    item.utility = 2.0
    candidates = wb.select_candidates(store)
    assert len(candidates) == 1
    assert candidates[0].item.id == "d1"
    assert candidates[0].reason == "derived_promotion"


def test_select_recalled_candidate():
    wb = WritebackManager(min_age=20, min_utility=2.0)
    store = WMTMStore(capacity=10)
    item = store.admit("r1", "recalled item", source_type="recalled", initial_sti=5.0)
    for _ in range(25):
        store.tick()
    item.utility = 3.0
    candidates = wb.select_candidates(store)
    assert len(candidates) == 1
    assert candidates[0].reason == "enrichment"


def test_select_none_too_young():
    wb = WritebackManager(derived_min_age=10, derived_min_utility=1.0)
    store = WMTMStore(capacity=10)
    store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    for _ in range(5):
        store.tick()
    candidates = wb.select_candidates(store)
    assert len(candidates) == 0


def test_select_none_low_utility():
    wb = WritebackManager(derived_min_age=10, derived_min_utility=5.0)
    store = WMTMStore(capacity=10)
    store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    for _ in range(15):
        store.tick()
    candidates = wb.select_candidates(store)
    assert len(candidates) == 0


def test_writeback_calls_append():
    wb = WritebackManager(derived_min_age=5, derived_min_utility=0.5)
    store = WMTMStore(capacity=10)
    item = store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    for _ in range(10):
        store.tick()
    item.utility = 1.0
    candidates = wb.select_candidates(store)
    calls = []
    written = wb.writeback(candidates, lambda content: calls.append(content))
    assert len(written) == 1
    assert len(calls) == 1
    assert "derived belief" in calls[0]


def test_writeback_dedup():
    wb = WritebackManager(derived_min_age=5, derived_min_utility=0.5)
    store = WMTMStore(capacity=10)
    item = store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    for _ in range(10):
        store.tick()
    item.utility = 1.0
    candidates = wb.select_candidates(store)
    wb.writeback(candidates, lambda c: None)
    # Second call should not re-write
    candidates2 = wb.select_candidates(store)
    written2 = wb.writeback(candidates2, lambda c: None)
    assert len(written2) == 0


def test_has_been_written():
    wb = WritebackManager(derived_min_age=5, derived_min_utility=0.5)
    store = WMTMStore(capacity=10)
    store.admit("d1", "derived belief", source_type="derived", initial_sti=5.0)
    for _ in range(10):
        store.tick()
    item = store.get("d1")
    item.utility = 1.0
    assert wb.has_been_written("d1") is False
    candidates = wb.select_candidates(store)
    wb.writeback(candidates, lambda c: None)
    assert wb.has_been_written("d1") is True


# --- WritebackManager.reset ---


def test_writeback_reset():
    """reset() should clear all tracking state."""
    wb = WritebackManager(derived_min_age=5, derived_min_utility=0.5)
    store = WMTMStore()
    item = store.admit("wb-reset-test", "test content", source_type="derived",
                       derived_from=["src1"], initial_sti=5.0)
    item.utility = 5.0
    # Advance tick so the item is old enough for writeback selection
    for _ in range(10):
        store.tick()
    candidates = wb.select_candidates(store)
    assert len(candidates) >= 1
    wb.writeback(candidates, lambda c: None)
    # Verify it was tracked as written back
    assert wb.has_been_written(item.id)
    # Reset clears tracking
    wb.reset()
    assert not wb.has_been_written(item.id)
