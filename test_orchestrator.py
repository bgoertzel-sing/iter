"""Tests for control/orchestrator.py -- control cycle orchestrator."""
import pytest
from control.types import (
    ActionGrant, Appraisal, Belief, DeonticStatus, Effect, EffectClass,
    EffectRecord, GoalSpec, GoalStatus, Observation, Plan, PlanStep, now_iso,
)
from control.orchestrator import Orchestrator, CycleResult
from control.dispatcher import DispatchResult


def make_obs(oid="o1"):
    return Observation(observation_id=oid, source="test", collection_method="mock",
                       timestamp=now_iso(), state_revision=0, schema_version="v1",
                       content={"value": 1}, content_digest="abc", raw_evidence_ref=None)


def make_belief(bid="b1"):
    return Belief(belief_id=bid, claim="test", truth_value=0.9, confidence=0.9,
                  observation_ids=["o1"], rule_ids=[], proof_trace=[])


def make_step(sid="s1", tool="op1"):
    return PlanStep(step_id=sid, operator_id=tool, operator_revision=1,
                   arguments={}, target="t1")


def make_plan(pid="p1", steps=None):
    return Plan(plan_id=pid, goal_id="g1", goal_revision=1, state_revision=0,
               steps=steps or [make_step()], terminal_predicate="done")


def make_grant(gid="gr1", tool="op1"):
    return ActionGrant(grant_id=gid, operator_id=tool, operator_revision=1,
                     tool_name=tool, canonical_args="{}", target="t1",
                     goal_id="g1", goal_revision=1, state_revision=0,
                     idempotency_key=f"ik_{gid}", nonce=f"n_{gid}",
                     issued_at=now_iso(), expires_at=now_iso())


def make_appraisal(deontic=DeonticStatus.OBLIGATED, score=0.8):
    return Appraisal(appraisal_id="a1", plan_id="p1", deontic_status=deontic,
                    utility_estimate=0.9, uncertainty=0.1, ranking_score=score)


def make_effect_record(rid="er1", gid="gr1"):
    return EffectRecord(record_id=rid, grant_id=gid, operator_id="op1",
                      target="t1", observed_at=now_iso(),
                      effect_class=EffectClass.OBSERVE)


def make_goal(gid="g1"):
    return GoalSpec(goal_id=gid, revision=1, owner="test", description="test", priority=1)


def make_dr(gid="gr1", success=True):
    er = make_effect_record(gid=gid) if success else None
    return DispatchResult(grant_id=gid, success=success, effect_record=er, elapsed_ms=1.0)


def make_orch(observations=None, beliefs=None, plans=None, appraisals=None,
              grants=None, dispatch_result=None, goal_status=GoalStatus.ACTIVE):
    obs_l = observations if observations is not None else [make_obs()]
    bel_l = beliefs if beliefs is not None else []
    plan_l = plans if plans is not None else [make_plan()]
    appr = appraisals[0] if appraisals else make_appraisal()
    grant_l = grants if grants is not None else [make_grant()]
    dr = dispatch_result or make_dr()
    return Orchestrator(
        observe_fn=lambda rev: obs_l,
        believe_fn=lambda obs: bel_l,
        plan_fn=lambda obs, bel, goal: plan_l,
        validate_fn=lambda plan, obs: appr,
        authorize_fn=lambda plan, goal, rev: grant_l,
        dispatch_fn=lambda grant: dr,
        evaluate_fn=lambda effects, goal: goal_status,
    )


class TestConstruction:
    def test_no_goal_returns_error(self):
        orch = make_orch()
        result = orch.run_cycle()
        assert "No active goal" in result.error
        assert result.cycle == 0

    def test_set_goal(self):
        orch = make_orch()
        g = make_goal()
        orch.set_goal(g)
        assert orch.goal is g

    def test_stop(self):
        orch = make_orch()
        orch.stop()
        assert orch.stopped

    def test_stopped_returns_stopped(self):
        orch = make_orch()
        orch.set_goal(make_goal())
        orch.stop()
        result = orch.run_cycle()
        assert result.stopped
        assert "Stopped" in result.error

    def test_cycle_count_increments(self):
        orch = make_orch(plans=[])
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert orch.cycle_count == 1
        orch.run_cycle()
        assert orch.cycle_count == 2

    def test_state_revision_increments(self):
        orch = make_orch(plans=[])
        orch.set_goal(make_goal())
        assert orch.state_revision == 0
        orch.run_cycle()
        assert orch.state_revision == 1

    def test_cycle_history_grows(self):
        orch = make_orch(plans=[])
        orch.set_goal(make_goal())
        orch.run_cycle()
        orch.run_cycle()
        assert len(orch.cycle_history) == 2


class TestObservationPhase:
    def test_observations_collected(self):
        orch = make_orch(observations=[make_obs("o1"), make_obs("o2")])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.observations) == 2
        assert result.observations[0].observation_id == "o1"

    def test_observe_receives_revision(self):
        revs = []
        orch = Orchestrator(
            observe_fn=lambda r: revs.append(r) or [],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        orch.run_cycle()
        orch.run_cycle()
        assert revs == [0, 1]

    def test_observations_recorded(self):
        recorded = []
        orch = Orchestrator(
            observe_fn=lambda rev: [make_obs("o1")],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
            record_fn=lambda e: recorded.append(e) or "",
        )
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert len(recorded) >= 1


class TestBeliefPhase:
    def test_beliefs_collected(self):
        b = make_belief("b1")
        orch = make_orch(beliefs=[b])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.beliefs) == 1
        assert result.beliefs[0].belief_id == "b1"

    def test_beliefs_passed_to_planner(self):
        captured = []
        orch = Orchestrator(
            observe_fn=lambda rev: [make_obs()],
            believe_fn=lambda obs: [make_belief()],
            plan_fn=lambda obs, bel, goal: captured.extend(bel) or [],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert len(captured) == 1


class TestPlanningPhase:
    def test_plans_generated(self):
        orch = make_orch(plans=[make_plan("p1")])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.plans) == 1
        assert result.plans[0].plan_id == "p1"

    def test_no_plans_early_return(self):
        orch = make_orch(plans=[])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.plans) == 0
        assert result.goal_status is None

    def test_plans_passed_goal(self):
        captured = []
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda obs, bel, goal: captured.append(goal) or [],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        g = make_goal("custom")
        orch.set_goal(g)
        orch.run_cycle()
        assert captured[0].goal_id == "custom"


class TestAppraisalPhase:
    def test_obligated_proceeds(self):
        orch = make_orch(appraisals=[make_appraisal(DeonticStatus.OBLIGATED, 0.9)])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.appraisals) == 1
        assert len(result.grants) == 1

    def test_permitted_proceeds(self):
        orch = make_orch(appraisals=[make_appraisal(DeonticStatus.PERMITTED, 0.7)])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.grants) == 1

    def test_forbidden_blocked(self):
        orch = make_orch(appraisals=[make_appraisal(DeonticStatus.FORBIDDEN, 0.9)])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.appraisals) == 1
        assert len(result.grants) == 0
        assert len(result.dispatches) == 0

    def test_best_ranking_selected(self):
        plan_a = make_plan("pa")
        plan_b = make_plan("pb")
        captured = []
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda obs, bel, goal: [plan_a, plan_b],
            validate_fn=lambda p, o: (
                make_appraisal(DeonticStatus.PERMITTED, 0.5)
                if p.plan_id == "pa"
                else make_appraisal(DeonticStatus.OBLIGATED, 0.9)
            ),
            authorize_fn=lambda p, g, r: captured.append(p.plan_id) or [make_grant()],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.grants) == 1
        assert captured[0] == "pb"


class TestAuthorizationPhase:
    def test_grants_issued(self):
        g1 = make_grant("g1")
        g2 = make_grant("g2")
        orch = make_orch(grants=[g1, g2])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.grants) == 2
        assert result.grants[0].grant_id == "g1"

    def test_no_grants_no_dispatch(self):
        orch = make_orch(grants=[])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.grants) == 0
        assert len(result.dispatches) == 0

    def test_authorize_receives_plan_and_goal(self):
        captured = []
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [make_plan()],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: captured.append((p.plan_id, g.goal_id)) or [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert captured[0] == ("p1", "g1")


class TestDispatchPhase:
    def test_dispatch_executed(self):
        orch = make_orch(grants=[make_grant()])
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.dispatches) == 1
        assert result.dispatches[0].success
        assert len(result.effects) == 1

    def test_dispatch_failure_recorded(self):
        dr = DispatchResult(grant_id="gr1", success=False, error="timeout", elapsed_ms=1.0)
        orch = make_orch(grants=[make_grant()], dispatch_result=dr)
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert len(result.dispatches) == 1
        assert not result.dispatches[0].success
        assert len(result.effects) == 0

    def test_effect_recorded(self):
        recorded = []
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [make_plan()],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [make_grant()],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
            record_fn=lambda e: recorded.append(e) or "",
        )
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert len(recorded) >= 1


class TestEvaluationPhase:
    def test_goal_achieved_clears_goal(self):
        orch = make_orch(goal_status=GoalStatus.SATISFIED)
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert result.goal_status == GoalStatus.SATISFIED
        assert orch.goal is None

    def test_goal_failed_clears_goal(self):
        orch = make_orch(goal_status=GoalStatus.FAILED)
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert result.goal_status == GoalStatus.FAILED
        assert orch.goal is None

    def test_goal_active_keeps_goal(self):
        orch = make_orch(goal_status=GoalStatus.ACTIVE)
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert orch.goal is not None

    def test_effects_passed_to_evaluator(self):
        captured = []
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [make_plan()],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [make_grant()],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda effs, goal: captured.append(len(effs)) or GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        orch.run_cycle()
        assert captured[0] == 1


class TestRunMultiple:
    def test_run_stops_on_achieved(self):
        orch = make_orch(goal_status=GoalStatus.SATISFIED)
        orch.set_goal(make_goal())
        results = orch.run(max_cycles=10)
        assert len(results) == 1
        assert results[0].goal_status == GoalStatus.SATISFIED

    def test_run_stops_on_failed(self):
        orch = make_orch(goal_status=GoalStatus.FAILED)
        orch.set_goal(make_goal())
        results = orch.run(max_cycles=10)
        assert len(results) == 1

    def test_run_no_goal_returns_empty(self):
        orch = make_orch()
        results = orch.run(max_cycles=10)
        assert len(results) == 0

    def test_run_respects_max_cycles(self):
        orch = make_orch(plans=[])
        orch.set_goal(make_goal())
        results = orch.run(max_cycles=3)
        assert len(results) == 3

    def test_run_stops_on_human_stop(self):
        orch = make_orch(plans=[])
        orch.set_goal(make_goal())
        orch.stop()
        results = orch.run(max_cycles=10)
        assert len(results) == 0


class TestFailClosed:
    def test_observe_error_caught(self):
        orch = Orchestrator(
            observe_fn=lambda rev: (_ for _ in ()).throw(RuntimeError("sensor dead")),
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert result.error != ""
        assert "RuntimeError" in result.error

    def test_plan_error_caught(self):
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: (_ for _ in ()).throw(ValueError("planner crashed")),
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [],
            dispatch_fn=lambda gr: make_dr(),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert "ValueError" in result.error

    def test_dispatch_error_caught(self):
        orch = Orchestrator(
            observe_fn=lambda rev: [],
            believe_fn=lambda o: [],
            plan_fn=lambda o, b, g: [make_plan()],
            validate_fn=lambda p, o: make_appraisal(),
            authorize_fn=lambda p, g, r: [make_grant()],
            dispatch_fn=lambda gr: (_ for _ in ()).throw(ConnectionError("refused")),
            evaluate_fn=lambda e, g: GoalStatus.ACTIVE,
        )
        orch.set_goal(make_goal())
        result = orch.run_cycle()
        assert "ConnectionError" in result.error
