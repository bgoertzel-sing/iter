"""
Tests for edge-case coverage in control/ modules.
Targets uncovered lines identified by coverage report.
"""
import pytest
import os
import tempfile
from unittest.mock import MagicMock, patch
from control.wmtm_adapters import (
    GoalChainerPlanAdapter, RealAuthorizeAdapter, RealDispatchAdapter,
)
from control.event_store import EventStore, EventRecord
from control.plan_validator import PlanValidator
from control.reference_monitor import ReferenceMonitor
from control.types import (
    Observation, Plan, PlanStep, GoalSpec, GoalStatus,
    ActionGrant, Appraisal, EffectRecord, compute_mac,
)
from control.dispatcher import Dispatcher, DispatchResult
from datetime import datetime, timezone, timedelta


def _future_iso():
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _make_goal():
    return GoalSpec(
        goal_id="g1", revision=1, owner="tester",
        description="test", priority=1,
    )

def _make_step():
    return PlanStep(
        step_id="s1", operator_id="op1", operator_revision=1,
        arguments={}, target="g1",
    )

def _make_plan(state_rev=0):
    return Plan(
        plan_id="p1", goal_id="g1", goal_revision=1,
        state_revision=state_rev, steps=(_make_step(),),
        terminal_predicate="done",
    )


class TestGoalChainerException:
    def test_goalchainer_runtime_error_returns_empty(self):
        store = MagicMock()
        goal = _make_goal()
        adapter = GoalChainerPlanAdapter(store, "request")
        with patch("wmtm.goalchainer_bridge.run_goalchainer_over_wmtm",
                   side_effect=RuntimeError("boom")):
            result = adapter([], [], goal)
        assert result == []


class TestAuthorizeWithEventStore:
    def test_authorize_uses_event_store_revision(self):
        secret = b"test_secret"
        ref_monitor = MagicMock()
        ref_monitor.authorize.return_value = (True, "Authorized")
        event_store = MagicMock()
        event_store.get_all.return_value = [MagicMock(revision=42)]
        adapter = RealAuthorizeAdapter(ref_monitor, secret, event_store)
        grants = adapter(_make_plan(), _make_goal(), state_revision=0)
        assert len(grants) == 1
        assert grants[0].state_revision == 42
        ref_monitor.authorize.assert_called_once()
        called_grant, called_rev = ref_monitor.authorize.call_args[0]
        assert called_rev == 42

    def test_authorize_event_store_empty_falls_back(self):
        secret = b"test_secret"
        ref_monitor = MagicMock()
        ref_monitor.authorize.return_value = (True, "Authorized")
        event_store = MagicMock()
        event_store.get_all.return_value = []
        adapter = RealAuthorizeAdapter(ref_monitor, secret, event_store)
        grants = adapter(_make_plan(state_rev=7), _make_goal(), state_revision=7)
        assert len(grants) == 1
        assert grants[0].state_revision == 7


class TestAuthorizationDenied:
    def test_authorization_denied_returns_empty(self):
        secret = b"test_secret"
        ref_monitor = MagicMock()
        ref_monitor.authorize.return_value = (False, "denied")
        adapter = RealAuthorizeAdapter(ref_monitor, secret)
        grants = adapter(_make_plan(), _make_goal(), state_revision=0)
        assert grants == []


class TestDispatchAdapter:
    def test_dispatch_calls_underlying_dispatcher(self):
        dispatcher = MagicMock()
        dispatcher.dispatch.return_value = DispatchResult(
            grant_id="g1", success=True,
        )
        adapter = RealDispatchAdapter(dispatcher)
        grant = MagicMock()
        result = adapter(grant)
        assert result.success is True
        dispatcher.dispatch.assert_called_once()


class TestEventStoreNoSnapshot:
    def test_latest_snapshot_none(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            es = EventStore(path)
            assert es.latest_snapshot() is None
            es.close()
        finally:
            os.unlink(path)


class TestEventStoreOrphaned:
    def test_orphaned_prev_event_detected(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            es = EventStore(path)
            es._conn.execute(
                "INSERT INTO events (event_id, event_type, timestamp, revision, payload, prev_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("e1", "test", _now_iso(), 1, "{}", "nonexistent"),
            )
            es._conn.commit()
            report = es.reconcile()
            assert "nonexistent" in report["orphaned"]
            es.close()
        finally:
            os.unlink(path)


class TestPlanValidatorEmptySteps:
    def test_utility_empty_plan(self):
        validator = PlanValidator()
        plan = Plan(
            plan_id="p1", goal_id="g1", goal_revision=1,
            state_revision=0, steps=(),
            terminal_predicate="done",
        )
        appraisal = validator.validate(plan, [])
        assert appraisal.utility_estimate == 0.0


class TestReferenceMonitorInvalidExpiry:
    def test_invalid_expires_at_rejected(self):
        secret = b"test_secret"
        monitor = ReferenceMonitor(secret)
        grant = ActionGrant(
            grant_id="g1", operator_id="op1", operator_revision=1,
            tool_name="op1", canonical_args="{}", target="t1",
            goal_id="goal1", goal_revision=1, state_revision=0,
            idempotency_key="ik1", nonce="n1",
            issued_at=_now_iso(),
            expires_at="not-a-date",
        )
        grant_with_mac = ActionGrant(
            grant_id=grant.grant_id, operator_id=grant.operator_id,
            operator_revision=grant.operator_revision, tool_name=grant.tool_name,
            canonical_args=grant.canonical_args, target=grant.target,
            goal_id=grant.goal_id, goal_revision=grant.goal_revision,
            state_revision=grant.state_revision, idempotency_key=grant.idempotency_key,
            nonce=grant.nonce, issued_at=grant.issued_at,
            expires_at=grant.expires_at,
            mac=compute_mac(grant, secret),
        )
        authorized, reason = monitor.authorize(grant_with_mac, 0)
        assert authorized is False
        assert "Invalid expires_at" in reason


class TestReferenceMonitorAlreadyDispatched:
    def test_already_dispatched_rejected(self):
        secret = b"test_secret"
        monitor = ReferenceMonitor(secret)
        # Manually mark grant_id as already dispatched
        monitor._dispatched_grant_ids.add("g1")

        grant = ActionGrant(
            grant_id="g1", operator_id="op1", operator_revision=1,
            tool_name="op1", canonical_args="{}", target="t1",
            goal_id="goal1", goal_revision=1, state_revision=0,
            idempotency_key="ik1", nonce="n1",
            issued_at=_now_iso(),
            expires_at=_future_iso(),
        )
        grant_with_mac = ActionGrant(
            grant_id=grant.grant_id, operator_id=grant.operator_id,
            operator_revision=grant.operator_revision, tool_name=grant.tool_name,
            canonical_args=grant.canonical_args, target=grant.target,
            goal_id=grant.goal_id, goal_revision=grant.goal_revision,
            state_revision=grant.state_revision, idempotency_key=grant.idempotency_key,
            nonce=grant.nonce, issued_at=grant.issued_at,
            expires_at=grant.expires_at,
            mac=compute_mac(grant, secret),
        )
        authorized, reason = monitor.authorize(grant_with_mac, 0)
        assert authorized is False
        assert "already dispatched" in reason
