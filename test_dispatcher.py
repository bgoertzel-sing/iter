"""Tests for control/dispatcher.py -- action dispatcher."""
import pytest
import json
from control.types import ActionGrant, Effect, EffectClass, EffectRecord, now_iso
from control.dispatcher import Dispatcher, DispatchResult


def make_grant(grant_id="g1", tool_name="op1", target="t1", canonical_args="{}"):
    return ActionGrant(
        grant_id=grant_id,
        operator_id=tool_name,
        operator_revision=1,
        tool_name=tool_name,
        canonical_args=canonical_args,
        target=target,
        goal_id="g1",
        goal_revision=1,
        state_revision=1,
        idempotency_key=f"ik_{grant_id}",
        nonce=f"n_{grant_id}",
        issued_at=now_iso(),
        expires_at=now_iso(),
    )


def make_effect(name="status_read", cls=EffectClass.OBSERVE):
    return Effect(name=name, effect_type="observation", effect_class=cls)


class TestRegisterImplementation:
    def test_register(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        # No error means success
        assert d.dispatch_count == 0


class TestDispatch:
    def test_successful_dispatch(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        grant = make_grant()
        result = d.dispatch(grant, authorized=True)
        assert result.success
        assert result.effect_record is not None
        assert result.effect_record.grant_id == "g1"
        assert result.effect_record.operator_id == "op1"

    def test_unauthorized_dispatch_refused(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        grant = make_grant()
        result = d.dispatch(grant, authorized=False)
        assert not result.success
        assert "not authorized" in result.error.lower()
        assert result.effect_record is None

    def test_no_implementation_registered(self):
        d = Dispatcher()
        grant = make_grant(tool_name="unknown_op")
        result = d.dispatch(grant, authorized=True)
        assert not result.success
        assert "no implementation" in result.error.lower()

    def test_operator_raises_exception(self):
        d = Dispatcher()
        def bad_impl(args, target):
            raise RuntimeError("boom")
        d.register_implementation("op1", bad_impl)
        grant = make_grant()
        result = d.dispatch(grant, authorized=True)
        assert not result.success
        assert "RuntimeError" in result.error
        assert "boom" in result.error

    def test_operator_returns_wrong_type(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: "not an effect")
        grant = make_grant()
        result = d.dispatch(grant, authorized=True)
        assert not result.success
        assert "TypeError" in result.error

    def test_dispatch_passes_arguments(self):
        d = Dispatcher()
        captured = {}
        def capturing_impl(args, target):
            captured["args"] = args
            captured["target"] = target
            return make_effect()
        d.register_implementation("op1", capturing_impl)
        grant = make_grant(canonical_args=json.dumps({"key": "value"}))
        result = d.dispatch(grant, authorized=True)
        assert result.success
        assert captured["args"] == {"key": "value"}
        assert captured["target"] == "t1"

    def test_dispatch_with_empty_args(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        grant = make_grant(canonical_args="")
        result = d.dispatch(grant, authorized=True)
        assert result.success


class TestDispatchLog:
    def test_log_records_dispatches(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        d.dispatch(make_grant(grant_id="g1"), authorized=True)
        d.dispatch(make_grant(grant_id="g2"), authorized=False)
        log = d.get_log()
        assert len(log) == 2

    def test_success_count(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        d.dispatch(make_grant(), authorized=True)
        d.dispatch(make_grant(grant_id="g2"), authorized=True)
        d.dispatch(make_grant(grant_id="g3"), authorized=False)
        assert d.success_count == 2
        assert d.failure_count == 1
        assert d.dispatch_count == 3

    def test_reset(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        d.dispatch(make_grant(), authorized=True)
        assert d.dispatch_count == 1
        d.reset()
        assert d.dispatch_count == 0


class TestEffectRecord:
    def test_effect_record_has_correct_fields(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect(name="file_write", cls=EffectClass.IRREVERSIBLE))
        grant = make_grant()
        result = d.dispatch(grant, authorized=True)
        assert result.success
        er = result.effect_record
        assert er.record_id == "er_g1"
        assert er.grant_id == "g1"
        assert er.operator_id == "op1"
        assert er.target == "t1"

    def test_elapsed_ms_positive(self):
        d = Dispatcher()
        d.register_implementation("op1", lambda args, target: make_effect())
        result = d.dispatch(make_grant(), authorized=True)
        assert result.elapsed_ms >= 0.0
