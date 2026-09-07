"""Tests for WMTM Phase 1: Core Store + Forgetting + Attention."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from wmtm.attention import AttentionValue
from wmtm.item import WMTMItem
from wmtm.store import WMTMStore
from wmtm.forgetting import ForgettingPolicy


class TestAttentionValue:
    def test_decay_reduces_all_components(self):
        av = AttentionValue(sti=10.0, ati=10.0, lti=10.0)
        av.tick()
        assert av.sti < 10.0
        assert av.ati < 10.0
        assert av.lti < 10.0

    def test_sti_decays_faster_than_lti(self):
        av = AttentionValue(sti=100.0, ati=100.0, lti=100.0)
        av.tick()
        assert av.sti < av.ati < av.lti

    def test_boost_increases_sti(self):
        av = AttentionValue(sti=1.0)
        av.boost(5.0)
        assert av.sti == 6.0

    def test_total_is_weighted_composite(self):
        av = AttentionValue(sti=10.0, ati=4.0, lti=5.0)
        assert av.total == 13.0


class TestWMTMItem:
    def test_touch_updates_last_used_and_utility(self):
        item = WMTMItem(id="x", content="test")
        item.touch(tick=5)
        assert item.last_used == 5
        assert item.utility == 1.0

    def test_touch_boosts_attention(self):
        item = WMTMItem(id="x", content="test")
        sti_before = item.attention.sti
        item.touch(tick=1)
        assert item.attention.sti > sti_before


class TestWMTMStore:
    def test_admit_adds_item(self):
        store = WMTMStore(capacity=10)
        item = store.admit("a", "hello")
        assert len(store) == 1
        assert item.source_type == "recalled"

    def test_admit_existing_refreshes_attention(self):
        store = WMTMStore(capacity=10)
        store.admit("a", "hello", initial_sti=1.0)
        original_sti = store.get("a").attention.sti
        store.admit("a", "hello", initial_sti=2.0)
        assert store.get("a").attention.sti > original_sti
        assert len(store) == 1

    def test_evict_removes_item(self):
        store = WMTMStore(capacity=10)
        store.admit("a", "hello")
        store.admit("b", "world")
        evicted = store.evict("a")
        assert evicted is not None
        assert evicted.id == "a"
        assert len(store) == 1

    def test_evict_nonexistent_returns_none(self):
        store = WMTMStore(capacity=10)
        assert store.evict("ghost") is None

    def test_capacity_eviction_removes_lowest_sti(self):
        store = WMTMStore(capacity=3)
        store.admit("a", "one", initial_sti=5.0)
        store.admit("b", "two", initial_sti=10.0)
        store.admit("c", "three", initial_sti=1.0)
        store.admit("d", "four", initial_sti=8.0)
        assert "d" in store
        assert "c" not in store
        assert len(store) == 3

    def test_tick_decays_attention_and_ages(self):
        store = WMTMStore(capacity=10)
        store.admit("a", "hello", initial_sti=5.0)
        item = store.get("a")
        sti_before = item.attention.sti
        store.tick()
        assert item.attention.sti < sti_before
        assert item.age == 1

    def test_tick_evicts_below_floor(self):
        store = WMTMStore(capacity=10)
        store.admit("a", "hello", initial_sti=0.005)
        evicted = store.tick()
        assert len(evicted) == 1
        assert evicted[0].id == "a"

    def test_get_active_set_sorted_by_attention(self):
        store = WMTMStore(capacity=10)
        store.admit("low", "x", initial_sti=1.0)
        store.admit("high", "y", initial_sti=10.0)
        store.admit("mid", "z", initial_sti=5.0)
        active = store.get_active_set()
        assert active[0].id == "high"
        assert active[1].id == "mid"
        assert active[2].id == "low"

    def test_touch_records_access(self):
        store = WMTMStore(capacity=10)
        store.admit("a", "hello")
        store.touch("a")
        assert store.get("a").utility == 1.0

    def test_invalid_capacity_raises(self):
        try:
            WMTMStore(capacity=0)
            assert False
        except ValueError:
            pass


class TestForgettingPolicy:
    def test_evicts_below_sti_threshold(self):
        store = WMTMStore(capacity=10)
        store.admit("strong", "x", initial_sti=10.0)
        store.admit("weak", "y", initial_sti=0.01)
        policy = ForgettingPolicy(sti_threshold=0.05)
        evicted = policy.evaluate(store)
        assert len(evicted) == 1
        assert evicted[0].id == "weak"

    def test_age_based_eviction_for_low_utility(self):
        store = WMTMStore(capacity=10)
        store.admit("old_unused", "x", initial_sti=10.0)
        store.get("old_unused").age = 100
        store.get("old_unused").utility = 0.1
        store.admit("old_used", "y", initial_sti=10.0)
        store.get("old_used").age = 100
        store.get("old_used").utility = 5.0
        policy = ForgettingPolicy(max_age=50, min_utility=0.5)
        evicted = policy.evaluate(store)
        evicted_ids = [e.id for e in evicted]
        assert "old_unused" in evicted_ids
        assert "old_used" not in evicted_ids

    def test_derived_items_have_stricter_thresholds(self):
        store = WMTMStore(capacity=10)
        store.admit("recalled", "x", source_type="recalled", initial_sti=0.08)
        store.admit("derived", "y", source_type="derived", initial_sti=0.08)
        policy = ForgettingPolicy(sti_threshold=0.05, derived_sti_threshold=0.10)
        evicted = policy.evaluate(store)
        evicted_ids = [e.id for e in evicted]
        assert "derived" in evicted_ids
        assert "recalled" not in evicted_ids

    def test_should_writeback_for_durable_recalled_item(self):
        store = WMTMStore(capacity=10)
        store.admit("durable", "x", source_type="recalled")
        item = store.get("durable")
        item.age = 40
        item.utility = 3.0
        policy = ForgettingPolicy()
        assert policy.should_writeback(item) is True

    def test_should_not_writeback_for_derived_item(self):
        item = WMTMItem(id="d", content="x", source_type="derived", age=100, utility=10.0)
        policy = ForgettingPolicy()
        assert policy.should_writeback(item) is False

    def test_should_not_writeback_for_young_item(self):
        item = WMTMItem(id="y", content="x", source_type="recalled", age=5, utility=10.0)
        policy = ForgettingPolicy()
        assert policy.should_writeback(item) is False
