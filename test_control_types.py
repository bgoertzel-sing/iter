"""Tests for control/types.py — typed domain model for goal-driven iter control."""
import datetime as dt
import pytest
from control.types import (
    EffectClass, GoalStatus, ActionState, DeonticStatus,
    Predicate, Effect, ResourceClaim, GoalSpec, Observation, Belief,
    ActionOperator, ActionGrant, PlanStep, Plan, Appraisal, EventRecord,
    is_valid_transition, to_dict, canonical_args, content_digest,
    compute_mac, verify_mac, now_iso, expires_iso,
)


class TestEnums:
    def test_effect_class_values(self):
        assert EffectClass.OBSERVE.value == 'observe'
        assert EffectClass.REVERSIBLE.value == 'reversible'
        assert EffectClass.IRREVERSIBLE.value == 'irreversible'
        assert EffectClass.EXTERNAL.value == 'external'

    def test_goal_status_values(self):
        assert GoalStatus.DRAFT.value == 'draft'
        assert GoalStatus.ACTIVE.value == 'active'
        assert GoalStatus.SATISFIED.value == 'satisfied'

    def test_action_state_values(self):
        assert ActionState.PROPOSED.value == 'proposed'
        assert ActionState.REJECTED.value == 'rejected'
        assert ActionState.UNKNOWN.value == 'unknown'

    def test_deontic_status_values(self):
        assert DeonticStatus.OBLIGATED.value == 'obligated'
        assert DeonticStatus.FORBIDDEN.value == 'forbidden'


class TestPredicate:
    def test_auto_id(self):
        p = Predicate(name='all_receipts_match', version=1)
        assert p.predicate_id.startswith('pred_')

    def test_deterministic_id(self):
        p1 = Predicate(name='all_receipts_match', version=1)
        p2 = Predicate(name='all_receipts_match', version=1)
        assert p1.predicate_id == p2.predicate_id

    def test_version_matters(self):
        p1 = Predicate(name='all_receipts_match', version=1)
        p2 = Predicate(name='all_receipts_match', version=2)
        assert p1.predicate_id != p2.predicate_id


class TestGoalSpec:
    def test_auto_id(self):
        g = GoalSpec(goal_id='', revision=1, owner='test', description='test', priority=1)
        assert g.goal_id.startswith('goal_')

    def test_custom_id_preserved(self):
        g = GoalSpec(goal_id='custom', revision=2, owner='ben', description='test', priority=5)
        assert g.goal_id == 'custom'

    def test_defaults(self):
        g = GoalSpec(goal_id='g1', revision=1, owner='o', description='d', priority=1)
        assert g.status == GoalStatus.DRAFT
        assert g.effect_class_ceiling == EffectClass.OBSERVE
        assert g.allowed_operator_ids == ()

    def test_frozen(self):
        g = GoalSpec(goal_id='g1', revision=1, owner='o', description='d', priority=1)
        with pytest.raises(AttributeError):
            g.priority = 10


class TestObservation:
    def test_auto_id_and_digest(self):
        o = Observation(
            observation_id='', source='shard_status', collection_method='file_read',
            timestamp='2026-09-19T18:00:00Z', state_revision=1, schema_version='v1',
            content_digest='', raw_evidence_ref='/tmp/status.json',
            content={'shard': 'g1014', 'status': 'running'}
        )
        assert o.observation_id.startswith('obs_')
        assert len(o.content_digest) == 64

    def test_custom_id_preserved(self):
        o = Observation(
            observation_id='custom', source='s', collection_method='m',
            timestamp='t', state_revision=1, schema_version='v',
            content_digest='d', raw_evidence_ref='r', content={}
        )
        assert o.observation_id == 'custom'
        assert o.content_digest == 'd'


class TestBelief:
    def test_auto_id(self):
        b = Belief(belief_id='', claim='shard_running', truth_value=0.9, confidence=0.8)
        assert b.belief_id.startswith('belief_')
        assert b.observation_ids == ()


class TestActionOperator:
    def test_auto_id(self):
        op = ActionOperator(
            operator_id='', revision=1, parameter_schema='read_shard',
            precondition='shard_exists', effect_class=EffectClass.OBSERVE
        )
        assert op.operator_id.startswith('op_')
        assert op.timeout_s == 30
        assert op.compensation_operator_id is None

    def test_with_effects_and_claims(self):
        eff = Effect(name='status_read', effect_type='observation')
        rc = ResourceClaim(resource_type='file_lock', amount=1, exclusive=True)
        op = ActionOperator(
            operator_id='read_shard_status', revision=2, parameter_schema='schema',
            precondition='shard_exists', expected_effects=(eff,),
            resource_claims=(rc,), effect_class=EffectClass.OBSERVE,
            verification_predicate='verify_status'
        )
        assert op.expected_effects == (eff,)
        assert op.resource_claims == (rc,)


class TestActionGrant:
    def test_auto_id_nonce_key(self):
        gr = ActionGrant(
            grant_id='', operator_id='op', operator_revision=1, tool_name='tool',
            canonical_args='{}', target='t', goal_id='g', goal_revision=1,
            state_revision=1, idempotency_key='', nonce='',
            issued_at=now_iso(), expires_at=expires_iso(30)
        )
        assert gr.grant_id.startswith('grant_')
        assert gr.nonce != ''
        assert gr.idempotency_key != ''

    def test_not_expired(self):
        gr = ActionGrant(
            grant_id='g', operator_id='op', operator_revision=1, tool_name='tool',
            canonical_args='{}', target='t', goal_id='g', goal_revision=1,
            state_revision=1, idempotency_key='k', nonce='n',
            issued_at=now_iso(), expires_at=expires_iso(60)
        )
        assert not gr.is_expired()

    def test_expired(self):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)).isoformat()
        gr = ActionGrant(
            grant_id='g', operator_id='op', operator_revision=1, tool_name='tool',
            canonical_args='{}', target='t', goal_id='g', goal_revision=1,
            state_revision=1, idempotency_key='k', nonce='n',
            issued_at=past, expires_at=past
        )
        assert gr.is_expired()

    def test_mac_compute_and_verify(self):
        gr = ActionGrant(
            grant_id='g', operator_id='op', operator_revision=1, tool_name='tool',
            canonical_args='{}', target='t', goal_id='g', goal_revision=1,
            state_revision=1, idempotency_key='k', nonce='n',
            issued_at=now_iso(), expires_at=expires_iso(30)
        )
        secret = b'test_secret'
        mac = compute_mac(gr, secret)
        assert mac != ''
        gr_dict = to_dict(gr)
        gr_dict['mac'] = mac
        gr2 = ActionGrant(**gr_dict)
        assert verify_mac(gr2, secret) is True
        assert verify_mac(gr2, b'wrong') is False
        assert verify_mac(gr, secret) is False  # empty MAC


class TestPlanStepAndPlan:
    def test_plan_step_auto_id(self):
        s = PlanStep(
            step_id='', operator_id='op', operator_revision=1,
            arguments={'path': '/tmp/x'}, target='shard-g1014'
        )
        assert s.step_id.startswith('step_')

    def test_plan_with_dependency(self):
        s1 = PlanStep(
            step_id='', operator_id='read_shard_status', operator_revision=1,
            arguments={'path': '/tmp/status.json'}, target='shard-g1014'
        )
        s2 = PlanStep(
            step_id='', operator_id='compare_receipts', operator_revision=1,
            arguments={'expected': 'abc', 'observed': 'def'}, target='shard-g1014',
            depends_on=(s1.step_id,)
        )
        assert s2.depends_on == (s1.step_id,)
        plan = Plan(
            plan_id='', goal_id='g1', goal_revision=1, state_revision=1,
            steps=(s1, s2), terminal_predicate='all_receipts_match'
        )
        assert plan.plan_id.startswith('plan_')
        assert len(plan.steps) == 2
        assert plan.valid is False


class TestAppraisal:
    def test_auto_id(self):
        a = Appraisal(
            appraisal_id='', plan_id='plan_1',
            deontic_status=DeonticStatus.RECOMMENDED,
            utility_estimate=0.7, uncertainty=0.2
        )
        assert a.appraisal_id.startswith('appr_')
        assert a.authorized is False  # I2: obligation is not authorization
        assert a.conflicts == ()


class TestEventRecord:
    def test_auto_id(self):
        ev = EventRecord(
            event_id='', event_type='observation',
            timestamp=now_iso(), revision=1, payload='{}'
        )
        assert ev.event_id.startswith('evt_')
        assert ev.prev_event_id is None


class TestStateTransitions:
    @pytest.mark.parametrize('from_state,to_state', [
        (ActionState.PROPOSED, ActionState.VALIDATED),
        (ActionState.PROPOSED, ActionState.REJECTED),
        (ActionState.VALIDATED, ActionState.AUTHORIZED),
        (ActionState.AUTHORIZED, ActionState.DISPATCHING),
        (ActionState.DISPATCHING, ActionState.DISPATCHED),
        (ActionState.DISPATCHING, ActionState.FAILED),
        (ActionState.DISPATCHING, ActionState.UNKNOWN),
        (ActionState.DISPATCHED, ActionState.SUCCEEDED),
        (ActionState.DISPATCHED, ActionState.FAILED),
        (ActionState.SUCCEEDED, ActionState.RECONCILED),        (ActionState.FAILED, ActionState.RECONCILED),
        (ActionState.UNKNOWN, ActionState.RECONCILED),
        (ActionState.RECONCILED, ActionState.VERIFIED),
        (ActionState.RECONCILED, ActionState.CONTRADICTED),
    ])
    def test_valid_transitions(self, from_state, to_state):
        assert is_valid_transition(from_state, to_state)

    @pytest.mark.parametrize('from_state,to_state', [
        (ActionState.PROPOSED, ActionState.SUCCEEDED),
        (ActionState.PROPOSED, ActionState.AUTHORIZED),
        (ActionState.VALIDATED, ActionState.DISPATCHED),
        (ActionState.AUTHORIZED, ActionState.SUCCEEDED),
        (ActionState.DISPATCHED, ActionState.AUTHORIZED),
        (ActionState.VERIFIED, ActionState.PROPOSED),
        (ActionState.REJECTED, ActionState.PROPOSED),
        (ActionState.CONTRADICTED, ActionState.RECONCILED),
    ])
    def test_invalid_transitions(self, from_state, to_state):
        assert not is_valid_transition(from_state, to_state)


class TestHelpers:
    def test_canonical_args(self):
        args = {'b': 2, 'a': 1}
        result = canonical_args(args)
        assert result == '{"a":1,"b":2}'

    def test_content_digest(self):
        d1 = content_digest({'a': 1, 'b': 2})
        d2 = content_digest({'b': 2, 'a': 1})
        assert d1 == d2  # order-independent
        assert len(d1) == 64

    def test_now_iso(self):
        ts = now_iso()
        assert 'T' in ts
        assert '+' in ts or 'Z' in ts

    def test_expires_iso(self):
        exp = expires_iso(60)
        assert 'T' in exp

    def test_to_dict_handles_enums(self):
        g = GoalSpec(goal_id='g1', revision=1, owner='o', description='d', priority=1)
        d = to_dict(g)
        assert d['status'] == 'draft'
        assert d['effect_class_ceiling'] == 'observe'
