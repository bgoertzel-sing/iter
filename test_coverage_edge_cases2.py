"""
Additional tests targeting remaining uncovered lines:
- event_store.py 168-170: ROLLBACK path in supersede()
- plan_validator.py 140: _estimate_utility empty-steps early return
"""
import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch
from control.event_store import EventStore, EventRecord
from control.plan_validator import PlanValidator
from control.types import Plan
from datetime import datetime, timezone


class TestEventStoreSupersedeRollback:
    """Test that supersede() rolls back on failure (lines 168-170)."""

    def test_supersede_rollback_on_duplicate_id(self):
        """supersede should ROLLBACK and re-raise when INSERT fails (duplicate event_id)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            es = EventStore(path)
            # Insert two events
            ev1 = EventRecord(
                event_id="e1", event_type="test",
                timestamp=datetime.now(timezone.utc).isoformat(),
                revision=1, payload='{"v": 1}',
            )
            ev2 = EventRecord(
                event_id="e2", event_type="test",
                timestamp=datetime.now(timezone.utc).isoformat(),
                revision=2, payload='{"v": 2}',
            )
            es.append(ev1)
            es.append(ev2)

            # Try to supersede e1 with an event that has same ID as e2 (constraint violation)
            ev_dup = EventRecord(
                event_id="e2", event_type="test",
                timestamp=datetime.now(timezone.utc).isoformat(),
                revision=3, payload='{"v": 3}',
            )
            with pytest.raises(Exception):
                es.supersede("e1", ev_dup)

            # Verify rollback: e1 should NOT be superseded
            rows = es._conn.execute(
                "SELECT superseded FROM events WHERE event_id = ?", ("e1",)
            ).fetchone()
            assert rows[0] == 0

            # Verify no third event was inserted
            rows = es._conn.execute(
                "SELECT COUNT(*) FROM events"
            ).fetchone()
            assert rows[0] == 2  # Only e1 and e2

            es.close()
        finally:
            os.unlink(path)


class TestPlanValidatorEstimateUtilityEmpty:
    """Test _estimate_utility with empty steps (line 140)."""

    def test_estimate_utility_empty_steps_directly(self):
        """_estimate_utility returns 0.0 for empty plan when called directly."""
        validator = PlanValidator()
        plan = Plan(
            plan_id="p1", goal_id="g1", goal_revision=1,
            state_revision=0, steps=(),
            terminal_predicate="done",
        )
        result = validator._estimate_utility(plan, [])
        assert result == 0.0
