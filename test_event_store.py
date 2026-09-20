"""Tests for control/event_store.py -- transactional event store."""
import json
import os
import tempfile
import pytest
from control.types import EventRecord, now_iso
from control.event_store import EventStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = EventStore(path)
    yield s
    s.close()
    try:
        os.unlink(path)
    except OSError:
        pass


class TestInit:
    def test_empty_store(self, store):
        assert store.event_count() == 0
        assert store.current_revision() == 0


class TestAppend:
    def test_append_one(self, store):
        ev = EventRecord(event_id="e1", event_type="test", timestamp=now_iso(), revision=1, payload="{}")
        store.append(ev)
        assert store.event_count() == 1
        assert store.current_revision() == 1

    def test_append_multiple(self, store):
        for i in range(5):
            ev = EventRecord(event_id=f"e{i}", event_type="test", timestamp=now_iso(), revision=i+1, payload="{}")
            store.append(ev)
        assert store.event_count() == 5
        assert store.current_revision() == 5

    def test_duplicate_id_rejected(self, store):
        ev = EventRecord(event_id="e1", event_type="test", timestamp=now_iso(), revision=1, payload="{}")
        store.append(ev)
        with pytest.raises(ValueError, match="already exists"):
            store.append(ev)


class TestGet:
    def test_get_existing(self, store):
        ev = EventRecord(event_id="e1", event_type="obs", timestamp=now_iso(), revision=1, payload='{"k":1}')
        store.append(ev)
        result = store.get("e1")
        assert result is not None
        assert result.event_id == "e1"
        assert result.event_type == "obs"
        assert result.payload == '{"k":1}'

    def test_get_nonexistent(self, store):
        assert store.get("nonexistent") is None

    def test_get_all(self, store):
        for i in range(3):
            ev = EventRecord(event_id=f"e{i}", event_type="test", timestamp=now_iso(), revision=i+1, payload="{}")
            store.append(ev)
        all_events = store.get_all()
        assert len(all_events) == 3
        assert all_events[0].revision == 1
        assert all_events[2].revision == 3

    def test_get_by_type(self, store):
        store.append(EventRecord(event_id="e1", event_type="obs", timestamp=now_iso(), revision=1, payload="{}"))
        store.append(EventRecord(event_id="e2", event_type="action", timestamp=now_iso(), revision=2, payload="{}"))
        store.append(EventRecord(event_id="e3", event_type="obs", timestamp=now_iso(), revision=3, payload="{}"))
        obs = store.get_by_type("obs")
        assert len(obs) == 2
        actions = store.get_by_type("action")
        assert len(actions) == 1


class TestSupersede:
    def test_supersede_marks_old(self, store):
        old = EventRecord(event_id="e1", event_type="obs", timestamp=now_iso(), revision=1, payload='{"v":1}')
        store.append(old)
        new = EventRecord(event_id="e2", event_type="obs", timestamp=now_iso(), revision=2, payload='{"v":2}')
        store.supersede("e1", new)
        assert store.get("e1") is None  # old is superseded
        assert store.get("e2") is not None
        assert store.get("e2").prev_event_id == "e1"

    def test_supersede_count(self, store):
        old = EventRecord(event_id="e1", event_type="obs", timestamp=now_iso(), revision=1, payload="{}")
        store.append(old)
        new = EventRecord(event_id="e2", event_type="obs", timestamp=now_iso(), revision=2, payload="{}")
        store.supersede("e1", new)
        assert store.event_count() == 1  # only non-superseded counted


class TestSnapshot:
    def test_save_and_load(self, store):
        ev = EventRecord(event_id="e1", event_type="test", timestamp=now_iso(), revision=1, payload="{}")
        store.append(ev)
        store.save_snapshot(1, {"state": "ok"}, "e1")
        loaded = store.load_snapshot(1)
        assert loaded == {"state": "ok"}

    def test_load_nonexistent(self, store):
        assert store.load_snapshot(999) is None

    def test_latest_snapshot(self, store):
        ev1 = EventRecord(event_id="e1", event_type="test", timestamp=now_iso(), revision=1, payload="{}")
        store.append(ev1)
        store.save_snapshot(1, {"rev": 1}, "e1")
        ev2 = EventRecord(event_id="e2", event_type="test", timestamp=now_iso(), revision=2, payload="{}")
        store.append(ev2)
        store.save_snapshot(2, {"rev": 2}, "e2")
        latest = store.latest_snapshot()
        assert latest == {"rev": 2}


class TestReplay:
    def test_replay_from_zero(self, store):
        for i in range(5):
            ev = EventRecord(event_id=f"e{i}", event_type="test", timestamp=now_iso(), revision=i+1, payload="{}")
            store.append(ev)
        events = store.replay_from(0)
        assert len(events) == 5

    def test_replay_from_middle(self, store):
        for i in range(5):
            ev = EventRecord(event_id=f"e{i}", event_type="test", timestamp=now_iso(), revision=i+1, payload="{}")
            store.append(ev)
        events = store.replay_from(3)
        assert len(events) == 3
        assert events[0].revision == 3


class TestReconcile:
    def test_complete_log(self, store):
        for i in range(5):
            ev = EventRecord(event_id=f"e{i}", event_type="test", timestamp=now_iso(), revision=i+1, payload="{}")
            store.append(ev)
        report = store.reconcile()
        assert report["complete"] is True
        assert report["gaps"] == []
        assert report["duplicates"] == []
        assert report["event_count"] == 5

    def test_with_gap(self, store):
        store.append(EventRecord(event_id="e1", event_type="t", timestamp=now_iso(), revision=1, payload="{}"))
        store.append(EventRecord(event_id="e3", event_type="t", timestamp=now_iso(), revision=3, payload="{}"))
        report = store.reconcile()
        assert report["complete"] is False
        assert 2 in report["gaps"]


class TestCrashRecovery:
    def test_persistence_across_connections(self, store):
        # The fixture creates a store, we note the path
        db_path = store._db_path
        ev = EventRecord(event_id="e1", event_type="test", timestamp=now_iso(), revision=1, payload="{}")
        store.append(ev)
        store.save_snapshot(1, {"x": 1}, "e1")
        store.close()
        # Reopen
        s2 = EventStore(db_path, allow_init=False)
        assert s2.event_count() == 1
        assert s2.get("e1") is not None
        assert s2.load_snapshot(1) == {"x": 1}
        s2.close()
        os.unlink(db_path)
