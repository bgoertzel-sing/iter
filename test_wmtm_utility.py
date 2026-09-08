"""Tests for WMTM UtilityTracker."""
from wmtm.utility import UtilityTracker
from wmtm.store import WMTMStore


def test_record_use():
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    tracker.record_use("i1", tick=1)
    rec = tracker.get_record("i1")
    assert rec.use_count == 2
    assert rec.last_use_tick == 1


def test_record_miss():
    tracker = UtilityTracker()
    tracker.record_miss("i1", tick=1)
    tracker.record_miss("i1", tick=2)
    rec = tracker.get_record("i1")
    assert rec.miss_count == 2
    assert rec.total_ticks_alive == 2


def test_record_inferred_from():
    tracker = UtilityTracker()
    tracker.record_inferred_from("i1")
    tracker.record_inferred_from("i1")
    rec = tracker.get_record("i1")
    assert rec.inferred_from_count == 2


def test_utility_score_uses():
    tracker = UtilityTracker()
    for _ in range(5):
        tracker.record_use("i1", tick=1)
    rec = tracker.get_record("i1")
    # use_count=5 => 5*2.0 = 10.0
    assert rec.utility_score == 10.0


def test_utility_score_inferred():
    tracker = UtilityTracker()
    for _ in range(3):
        tracker.record_inferred_from("i1")
    rec = tracker.get_record("i1")
    # inferred_from_count=3 => 3*1.5 = 4.5
    assert abs(rec.utility_score - 4.5) < 0.01


def test_utility_score_misses_penalize():
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    for i in range(10):
        tracker.record_miss("i1", tick=i)
    rec = tracker.get_record("i1")
    # 1*2.0 - 10*0.3 = 2.0 - 3.0 = -1.0, clamped to 0
    assert rec.utility_score == 0.0


def test_utility_score_mixed():
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    tracker.record_use("i1", tick=1)
    tracker.record_inferred_from("i1")
    tracker.record_miss("i1", tick=2)
    rec = tracker.get_record("i1")
    # 2*2.0 + 1*1.5 - 1*0.3 = 4.0 + 1.5 - 0.3 = 5.2
    assert abs(rec.utility_score - 5.2) < 0.01


def test_hit_rate():
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    tracker.record_miss("i1", tick=2)
    tracker.record_miss("i1", tick=3)
    rec = tracker.get_record("i1")
    # total_ticks_alive=2 (from misses), use_count=1
    assert abs(rec.hit_rate - 0.5) < 0.01


def test_hit_rate_zero():
    tracker = UtilityTracker()
    rec = tracker.ensure("i1")
    assert rec is not None
    assert rec.hit_rate == 0.0


def test_tick_updates_item_utility():
    store = WMTMStore(capacity=10)
    store.admit("i1", "test content", initial_sti=5.0)
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=0)
    # Tick 1: i1 was used at tick 0, not tick 1 => miss
    tracker.tick(store, current_tick=1)
    item = store.get("i1")
    assert item.utility > 0  # Should have been updated


def test_tick_used_this_tick_no_miss():
    store = WMTMStore(capacity=10)
    store.admit("i1", "test content", initial_sti=5.0)
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    # Tick 1: i1 was used at tick 1 => no miss
    tracker.tick(store, current_tick=1)
    rec = tracker.get_record("i1")
    assert rec.miss_count == 0


def test_remove():
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    tracker.remove("i1")
    assert tracker.get_record("i1") is None


def test_promote_to_ltm_candidates():
    store = WMTMStore(capacity=10)
    store.admit("i1", "useful item", initial_sti=5.0)
    store.admit("i2", "useless item", initial_sti=5.0)
    tracker = UtilityTracker()
    # Make i1 useful
    for _ in range(10):
        tracker.record_use("i1", tick=1)
    # Age items
    for _ in range(35):
        store.tick()
    tracker.tick(store, current_tick=35)
    candidates = tracker.promote_to_ltm_candidates(store, min_age=30, min_utility=2.0)
    assert any(c.id == "i1" for c in candidates)


# --- get_utility ---


def test_get_utility_untracked():
    """get_utility returns 0.0 for untracked items."""
    tracker = UtilityTracker()
    assert tracker.get_utility("nonexistent") == 0.0


def test_get_utility_tracked():
    """get_utility returns the utility score for tracked items."""
    tracker = UtilityTracker()
    tracker.record_use("i1", tick=1)
    tracker.record_use("i1", tick=2)
    tracker.record_miss("i1", tick=3)
    # utility_score = use_count - miss_count (simplified)
    util = tracker.get_utility("i1")
    assert isinstance(util, float)
    assert util > 0.0  # 2 uses - 1 miss = positive
