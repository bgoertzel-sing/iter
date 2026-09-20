"""Tests for control/reference_monitor.py -- authorization service."""
import pytest
from datetime import datetime, timezone, timedelta
from control.types import ActionGrant, compute_mac, verify_mac, now_iso, expires_iso
from control.reference_monitor import ReferenceMonitor, parse_iso


@pytest.fixture
def secret():
    return b'test-secret-key-1234567890123456'


@pytest.fixture
def monitor(secret):
    return ReferenceMonitor(secret)


def make_grant(secret, state_revision=1, idempotency_key="key1", grant_id="g1", expires_in=300):
    g = ActionGrant(
        grant_id=grant_id, operator_id='op1', operator_revision=1,
        tool_name='shell', canonical_args='{}', target='t1',
        goal_id='goal1', goal_revision=1, state_revision=state_revision,
        idempotency_key=idempotency_key, nonce='n1',
        issued_at=now_iso(), expires_at=expires_iso(expires_in),
    )
    mac = compute_mac(g, secret)
    g_dict = {
        'grant_id': g.grant_id, 'operator_id': g.operator_id,
        'operator_revision': g.operator_revision, 'tool_name': g.tool_name,
        'canonical_args': g.canonical_args, 'target': g.target,
        'goal_id': g.goal_id, 'goal_revision': g.goal_revision,
        'state_revision': g.state_revision, 'idempotency_key': g.idempotency_key,
        'nonce': g.nonce, 'issued_at': g.issued_at, 'expires_at': g.expires_at,
        'mac': mac,
    }
    return ActionGrant(**g_dict)


class TestAuthorize:
    def test_valid_grant(self, monitor, secret):
        g = make_grant(secret, state_revision=1)
        authorized, reason = monitor.authorize(g, current_revision=1)
        assert authorized is True
        assert reason == 'Authorized'

    def test_wrong_mac(self, monitor, secret):
        g = make_grant(secret, state_revision=1)
        # Tamper: create grant with wrong secret
        bad_g = make_grant(b'wrong-secret', state_revision=1)
        authorized, reason = monitor.authorize(bad_g, current_revision=1)
        assert authorized is False
        assert 'HMAC' in reason

    def test_expired_grant(self, monitor, secret):
        g = make_grant(secret, state_revision=1, expires_in=-10)
        authorized, reason = monitor.authorize(g, current_revision=1)
        assert authorized is False
        assert 'expired' in reason.lower()

    def test_idempotency_reuse(self, monitor, secret):
        g = make_grant(secret, state_revision=1, idempotency_key='k1')
        auth1, _ = monitor.authorize(g, current_revision=1)
        assert auth1 is True
        g2 = make_grant(secret, state_revision=1, idempotency_key='k1', grant_id='g2')
        auth2, reason2 = monitor.authorize(g2, current_revision=1)
        assert auth2 is False
        assert 'Idempotency' in reason2

    def test_revision_mismatch(self, monitor, secret):
        g = make_grant(secret, state_revision=3)
        authorized, reason = monitor.authorize(g, current_revision=5)
        assert authorized is False
        assert 'Revision mismatch' in reason

    def test_double_dispatch(self, monitor, secret):
        g = make_grant(secret, state_revision=1)
        auth1, _ = monitor.authorize(g, current_revision=1)
        assert auth1 is True
        auth2, reason2 = monitor.authorize(g, current_revision=1)
        assert auth2 is False
        assert 'already' in reason2.lower() or 'idempotency' in reason2.lower()

    def test_custom_clock(self, monitor, secret):
        g = make_grant(secret, state_revision=1, expires_in=300)
        future = datetime.now(timezone.utc) + timedelta(seconds=400)
        authorized, reason = monitor.authorize(g, current_revision=1, clock=lambda: future)
        assert authorized is False
        assert 'expired' in reason.lower()


class TestRevoke:
    def test_revoke_grant(self, monitor, secret):
        g = make_grant(secret, state_revision=1)
        monitor.authorize(g, current_revision=1)
        assert monitor.is_dispatched(g.grant_id) is True
        monitor.revoke(g.grant_id)
        assert monitor.is_dispatched(g.grant_id) is False


class TestState:
    def test_used_keys_after_authorize(self, monitor, secret):
        g = make_grant(secret, state_revision=1, idempotency_key='mykey')
        monitor.authorize(g, current_revision=1)
        keys = monitor.used_idempotency_keys()
        assert 'mykey' in keys

    def test_reset(self, monitor, secret):
        g = make_grant(secret, state_revision=1)
        monitor.authorize(g, current_revision=1)
        monitor.reset()
        assert len(monitor.used_idempotency_keys()) == 0
        assert monitor.is_dispatched(g.grant_id) is False


class TestParseIso:
    def test_parse_z(self):
        dt = parse_iso('2026-01-01T00:00:00Z')
        assert dt.year == 2026

    def test_parse_offset(self):
        dt = parse_iso('2026-01-01T00:00:00+00:00')
        assert dt.year == 2026
