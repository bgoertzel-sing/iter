"""Unit tests for WMTMStore: bounded-capacity mutable active-set."""
import pytest
from wmtm.store import WMTMStore
from wmtm.item import WMTMItem


class TestWMTMStoreInit:
    def test_default_capacity(self):
        s = WMTMStore()
        assert s.capacity == 200
        assert len(s) == 0

    def test_custom_capacity(self):
        s = WMTMStore(capacity=5)
        assert s.capacity == 5

    def test_zero_capacity_raises(self):
        with pytest.raises(ValueError):
            WMTMStore(capacity=0)

    def test_negative_capacity_raises(self):
        with pytest.raises(ValueError):
            WMTMStore(capacity=-10)


class TestWMTMStoreAdmit:
    def test_admit_single_item(self):
        s = WMTMStore(capacity=10)
        item = s.admit("i1", "hello")
        assert item.id == "i1"
        assert item.content == "hello"
        assert len(s) == 1
        assert "i1" in s

    def test_admit_multiple_items(self):
        s = WMTMStore(capacity=10)
        for i in range(5):
            s.admit(f"i{i}", f"content_{i}")
        assert len(s) == 5

    def test_admit_duplicate_refreshes(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "hello", initial_sti=2.0)
        item = s.admit("i1", "hello", initial_sti=3.0)
        # Should boost, not replace
        assert len(s) == 1
        assert item.attention.sti == pytest.approx(4.85)  # 2.0 + 3.0 - 5% consolidation

    def test_admit_with_source_type(self):
        s = WMTMStore(capacity=10)
        item = s.admit("d1", "derived", source_type="derived")
        assert item.source_type == "derived"

    def test_admit_with_origin_cluster(self):
        s = WMTMStore(capacity=10)
        item = s.admit("i1", "content", origin_cluster="cl_A")
        assert item.origin_cluster == "cl_A"

    def test_admit_with_derived_from(self):
        s = WMTMStore(capacity=10)
        item = s.admit("d1", "content", derived_from=["p1", "p2"])
        assert item.derived_from == ["p1", "p2"]

    def test_admit_default_sti(self):
        s = WMTMStore(capacity=10)
        item = s.admit("i1", "content")
        assert item.attention.sti == pytest.approx(1.0)


class TestWMTMStoreEviction:
    def test_capacity_eviction_removes_lowest_sti(self):
        s = WMTMStore(capacity=3)
        s.admit("a", "a", initial_sti=5.0)
        s.admit("b", "b", initial_sti=1.0)
        s.admit("c", "c", initial_sti=3.0)
        # Adding 4th should evict "b" (lowest STI)
        s.admit("d", "d", initial_sti=4.0)
        assert len(s) == 3
        assert "b" not in s
        assert "a" in s
        assert "c" in s
        assert "d" in s

    def test_drain_pending_evicted(self):
        s = WMTMStore(capacity=2)
        s.admit("a", "a", initial_sti=5.0)
        s.admit("b", "b", initial_sti=1.0)
        s.admit("c", "c", initial_sti=3.0)
        evicted = s.drain_pending_evicted()
        assert len(evicted) == 1
        assert evicted[0].id == "b"
        # Second drain should be empty
        assert s.drain_pending_evicted() == []

    def test_explicit_evict(self):
        s = WMTMStore(capacity=10)
        s.admit("x", "content")
        evicted = s.evict("x")
        assert evicted is not None
        assert evicted.id == "x"
        assert len(s) == 0

    def test_evict_nonexistent_returns_none(self):
        s = WMTMStore(capacity=10)
        assert s.evict("nope") is None

    def test_multiple_evictions_at_once(self):
        s = WMTMStore(capacity=2)
        s.admit("a", "a", initial_sti=5.0)
        s.admit("b", "b", initial_sti=3.0)
        s.admit("c", "c", initial_sti=1.0)
        # capacity=2, so one evicted. Now add another
        s.admit("d", "d", initial_sti=0.5)
        evicted = s.drain_pending_evicted()
        # First admission of c evicts b (sti=3 > sti=1? no, b has sti=3, c has sti=1
        # Wait: a=5, b=3, c=1. Adding c at capacity → evict lowest = c? No, c is being added.
        # Actually: a=5, b=3 are in store. Adding c: store is at capacity=2, so evict lowest = b(sti=3)
        # Then c is added. Then adding d: at capacity again, evict lowest = c(sti=1)
        # So evicted should be b and c
        assert len(evicted) == 2
        evicted_ids = {e.id for e in evicted}
        assert "b" in evicted_ids
        assert "c" in evicted_ids


class TestWMTMStoreGet:
    def test_get_existing(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "hello")
        item = s.get("i1")
        assert item is not None
        assert item.content == "hello"

    def test_get_nonexistent(self):
        s = WMTMStore(capacity=10)
        assert s.get("nope") is None


class TestWMTMStoreTouch:
    def test_touch_updates_item(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "hello")
        s.touch("i1")
        item = s.get("i1")
        assert item.utility == pytest.approx(1.0)
        assert item.attention.sti == pytest.approx(1.475)  # 1.0 + 0.5 - 5% consolidation

    def test_touch_nonexistent_silent(self):
        s = WMTMStore(capacity=10)
        # Should not raise
        s.touch("nope")


class TestWMTMStoreGetActiveSet:
    def test_sorted_by_attention_desc(self):
        s = WMTMStore(capacity=10)
        s.admit("low", "l", initial_sti=1.0)
        s.admit("high", "h", initial_sti=10.0)
        s.admit("mid", "m", initial_sti=5.0)
        active = s.get_active_set()
        assert active[0].id == "high"
        assert active[1].id == "mid"
        assert active[2].id == "low"

    def test_empty_active_set(self):
        s = WMTMStore(capacity=10)
        assert s.get_active_set() == []


class TestWMTMStoreTick:
    def test_tick_ages_items(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "content")
        assert s.get("i1").age == 0
        s.tick()
        assert s.get("i1").age == 1
        s.tick()
        assert s.get("i1").age == 2

    def test_tick_decays_attention(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "content", initial_sti=10.0)
        s.tick()
        assert s.get("i1").attention.sti == pytest.approx(9.0)

    def test_tick_evicts_decayed_items(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "content", initial_sti=0.005)
        # STI is below floor (0.01), should be evicted on tick
        evicted = s.tick()
        assert len(evicted) == 1
        assert evicted[0].id == "i1"
        assert "i1" not in s

    def test_tick_keeps_healthy_items(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "content", initial_sti=5.0)
        evicted = s.tick()
        assert evicted == []
        assert "i1" in s

    def test_tick_multiple_cycles(self):
        s = WMTMStore(capacity=10)
        s.admit("i1", "content", initial_sti=100.0)
        for _ in range(100):
            evicted = s.tick()
            if evicted:
                break
        # After many ticks, STI should decay: 100 * 0.9^50 ≈ 0.0052
        # Which is below 0.01 floor, so eventually evicted
        assert "i1" not in s
