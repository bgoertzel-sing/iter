"""Tests for control/plan_validator.py -- plan validation."""
import pytest
from control.types import (
    ActionOperator, Appraisal, DeonticStatus, Observation,
    Plan, PlanStep, Predicate, Effect, EffectClass, ResourceClaim,
    now_iso, content_digest,
)
from control.plan_validator import PlanValidator


def make_operator(op_id='op1', rev=1, precondition='', effects=None, resource_claims=None):
    return ActionOperator(
        operator_id=op_id, revision=rev,
        parameter_schema='schema_v1',
        precondition=precondition,
        expected_effects=tuple(effects or []),
        possible_adverse_effects=(),
        effect_class=EffectClass.OBSERVE,
        resource_claims=tuple(resource_claims or []),
    )


def make_step(step_id='s1', op_id='op1', op_rev=1):
    return PlanStep(
        step_id=step_id, operator_id=op_id, operator_revision=op_rev,
        arguments={}, target='t1',
    )


def make_plan(steps=None, plan_id='p1', goal_id='g1'):
    return Plan(
        plan_id=plan_id, goal_id=goal_id, goal_revision=1,
        state_revision=1, steps=tuple(steps) if steps is not None else tuple([make_step()]),
        terminal_predicate='done',
    )


def make_obs(content=None, rev=1):
    return Observation(
        observation_id='o1', source='src', collection_method='m',
        timestamp=now_iso(), state_revision=rev, schema_version='v1',
        content_digest=content_digest(content or {}), raw_evidence_ref='',
        content=content or {},
    )


class TestRegisterOperator:
    def test_register(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        assert pv.operator_count == 1

    def test_get_operator(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        retrieved = pv.get_operator('op1', 1)
        assert retrieved is not None
        assert retrieved.operator_id == 'op1'

    def test_get_nonexistent(self):
        pv = PlanValidator()
        assert pv.get_operator('unknown', 1) is None


class TestValidate:
    def test_valid_plan(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        plan = make_plan()
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.PERMITTED
        assert appraisal.utility_estimate > 0
        assert appraisal.uncertainty < 1.0
        assert not appraisal.authorized

    def test_empty_steps_forbidden(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        plan = make_plan(steps=[])
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN
        assert 'no steps' in appraisal.rejection_reason.lower()

    def test_too_many_steps(self):
        pv = PlanValidator(max_steps=3)
        pv.register_operator(make_operator())
        steps = [make_step(step_id=f's{i}') for i in range(5)]
        plan = make_plan(steps=steps)
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN
        assert 'max is 3' in appraisal.rejection_reason

    def test_no_goal_id(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        plan = make_plan(goal_id='')
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN
        assert 'goal_id' in appraisal.rejection_reason

    def test_operator_not_registered(self):
        pv = PlanValidator()
        step = make_step(op_id='unknown_op')
        plan = make_plan(steps=[step])
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN
        assert 'not registered' in appraisal.rejection_reason

    def test_precondition_not_satisfied(self):
        pv = PlanValidator()
        op = make_operator(precondition='system_ready')
        pv.register_operator(op)
        plan = make_plan()
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN
        assert 'pre-condition' in appraisal.rejection_reason.lower()

    def test_precondition_satisfied(self):
        pv = PlanValidator()
        op = make_operator(precondition='system_ready')
        pv.register_operator(op)
        obs = make_obs(content={'system_ready': True})
        plan = make_plan()
        appraisal = pv.validate(plan, [obs])
        assert appraisal.deontic_status == DeonticStatus.PERMITTED

    def test_no_precondition_always_satisfied(self):
        pv = PlanValidator()
        op = make_operator(precondition='')
        pv.register_operator(op)
        plan = make_plan()
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.PERMITTED


class TestRegisteredPredicateCheck:
    def test_custom_check_fn(self):
        pv = PlanValidator()
        op = make_operator(precondition='custom_pred')
        pv.register_operator(op)
        pv.register_predicate_check('custom_pred', lambda obss: True)
        plan = make_plan()
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.PERMITTED

    def test_custom_check_fn_returns_false(self):
        pv = PlanValidator()
        op = make_operator(precondition='custom_pred')
        pv.register_operator(op)
        pv.register_predicate_check('custom_pred', lambda obss: False)
        plan = make_plan()
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN


class TestUtilityEstimate:
    def test_single_step_high_utility(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        plan = make_plan(steps=[make_step()])
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.utility_estimate >= 0.9

    def test_many_steps_lower_utility(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        steps = [make_step(step_id=f's{i}') for i in range(10)]
        plan = make_plan(steps=steps)
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.utility_estimate < 0.9


class TestUncertainty:
    def test_no_observations_higher_uncertainty(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        plan = make_plan()
        appraisal = pv.validate(plan, [])
        assert appraisal.uncertainty >= 0.3

    def test_with_observations_lower_uncertainty(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        plan = make_plan()
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.uncertainty < 0.3

    def test_more_steps_more_uncertainty(self):
        pv = PlanValidator()
        pv.register_operator(make_operator())
        steps = [make_step(step_id=f's{i}') for i in range(10)]
        plan = make_plan(steps=steps)
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.uncertainty >= 0.5


class TestMultipleOperators:
    def test_different_revisions(self):
        pv = PlanValidator()
        pv.register_operator(make_operator(op_id='op1', rev=1))
        pv.register_operator(make_operator(op_id='op1', rev=2))
        assert pv.operator_count == 2
        step = make_step(op_id='op1', op_rev=2)
        plan = make_plan(steps=[step])
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.PERMITTED

    def test_wrong_revision_fails(self):
        pv = PlanValidator()
        pv.register_operator(make_operator(op_id='op1', rev=1))
        step = make_step(op_id='op1', op_rev=2)
        plan = make_plan(steps=[step])
        appraisal = pv.validate(plan, [make_obs()])
        assert appraisal.deontic_status == DeonticStatus.FORBIDDEN
