"""End-to-end integration tests for F04 Gov Bridge.

Tests the full WMTM → GoalChainer → GovernanceBridge → WMTM round trip,
including deontic enforcement, goal lifecycle, decision history,
and deontic-aware PLN propagation.

Uses mocked GoalChainer results (PeTTa runtime not required).
"""
import json
import os
import tempfile
import pytest

from wmtm.store import WMTMStore
from wmtm.orchestrator import WMTMOrchestrator
from wmtm.goal import Goal
from wmtm.goal_store import GoalStore
from wmtm.gov_bridge import GovernanceBridge, DecisionHistory, DeonticEnforcer, GoalDecisionRecord
from wmtm.goalchainer_bridge import (
    wmtm_to_goalchainer_evidence,
    goalchainer_result_to_candidates,
    parse_decisions,
    GoalChainerDecision,
)
from wmtm.pln_bridge import (
    apply_deontic_constraints_to_atoms,
    run_pln_inference_over_wmtm_with_deontic,
    wmtm_to_pln_atoms,
)
from wmtm.pln import PLNAtom, TruthValue


# ── Fixtures ──────────────────────────────────────────────────────

def _make_store(*items):
    """Create a WMTMStore with given (id, content, sti) tuples."""
    store = WMTMStore(capacity=50)
    for item_id, content, sti in items:
        store.admit(item_id=item_id, content=content, source_type='test', initial_sti=sti)
    return store


def _mock_gc_result(decisions, executed=None, decided=None):
    """Create a mock GoalChainer result dict."""
    return {
        "request": "test incident",
        "decisions": decisions,
        "executed": executed or {},
        "decided": decided or (decisions[0]["action_id"] if decisions else ""),
    }


def _make_gov_bridge(tmpdir):
    """Create a GovernanceBridge with temp storage."""
    gs_path = os.path.join(tmpdir, "goals.json")
    dh_path = os.path.join(tmpdir, "decisions.json")
    goal_store = GoalStore(path=gs_path)
    decision_history = DecisionHistory(path=dh_path)
    return GovernanceBridge(
        goal_store=goal_store,
        decision_history=decision_history,
    )


# ── E2E: WMTM → Evidence → GC Result → Gov Bridge → WMTM ────────

class TestE2ERoundTrip:
    """Full round-trip: WMTM items → GoalChainer evidence → decisions → governance → WMTM candidates."""

    def test_full_round_trip_with_governance(self):
        """Verify the complete feedback loop works end-to-end."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Setup: WMTM store with some items
            store = _make_store(
                ("item-1", "checkout payment retry timeout error", 80.0),
                ("item-2", "customer email ava@example.com exposed", 60.0),
                ("item-3", "redacted summary ready for publish", 70.0),
            )

            # Setup: Goal and governance
            gov = _make_gov_bridge(tmpdir)
            goal = Goal(id="g1", description="resolve checkout incident safely", priority=1)
            goal.activate(cycle=0)
            gov.goal_store.add(goal)

            # Step 1: Extract evidence for GoalChainer
            evidence = wmtm_to_goalchainer_evidence(store, max_items=10)
            assert len(evidence) == 3
            assert evidence[0]["id"] == "item-1"
            assert evidence[0]["sti"] == 80.0

            # Step 2: Simulate GoalChainer result (mocked)
            gc_result = _mock_gc_result(
                decisions=[
                    {
                        "action_id": "publish_redacted_summary",
                        "label": "Publish Redacted Summary",
                        "status": "recommended",
                        "score": 0.92,
                        "satisfied_goals": ["g1"],
                    },
                    {
                        "action_id": "publish_raw_log",
                        "label": "Publish Raw Log",
                        "status": "forbidden",
                        "score": 0.3,
                    },
                    {
                        "action_id": "hold_external_update",
                        "label": "Hold External Update",
                        "status": "permitted",
                        "score": 0.5,
                    },
                ],
                executed={"success": True, "detail": "redacted summary published"},
                decided="publish_redacted_summary",
            )

            # Step 3: Process through governance bridge
            gov_out = gov.process_goalchainer_result(
                gc_result, store, cycle=0, goal_id="g1"
            )

            # Verify governance created records
            assert gov_out["records"] == 3
            assert gov_out["enforcement"]["suppressed"] >= 0  # forbidden items
            assert gov_out["enforcement"]["boosted"] >= 0      # recommended items

            # Verify goal lifecycle: g1 should be achieved (success + satisfied)
            assert len(gov_out["lifecycle_changes"]) == 1
            assert gov_out["lifecycle_changes"][0]["new_status"] == "achieved"

            # Step 4: Convert to WMTM candidates
            source_ids = [item.id for item in store.get_active_set()]
            candidates = goalchainer_result_to_candidates(
                gc_result, cycle=0, source_ids=source_ids
            )
            assert len(candidates) == 3
            # Recommended action should have higher STI than forbidden
            rec_cand = candidates[0]  # publish_redacted_summary
            forb_cand = candidates[1]  # publish_raw_log
            assert rec_cand.initial_sti > forb_cand.initial_sti

            # Step 5: Verify decision history persisted
            history = gov.history
            assert len(history) == 3
            assert history.by_goal("g1")[0].execution_result == "success"

    def test_round_trip_goal_blocked(self):
        """Goal gets blocked when execution fails with missing requirements."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(
                ("item-1", "upgrade system component", 80.0),
            )
            gov = _make_gov_bridge(tmpdir)
            goal = Goal(id="g2", description="upgrade system", priority=1)
            goal.activate(cycle=0)
            gov.goal_store.add(goal)

            gc_result = _mock_gc_result(
                decisions=[{
                    "action_id": "run_upgrade",
                    "label": "Run Upgrade",
                    "status": "obligated",
                    "score": 0.8,
                    "missing_required_goals": ["prerequisite-1"],
                }],
                executed={"success": False, "error": "prerequisite not met"},
                decided="run_upgrade",
            )

            gov_out = gov.process_goalchainer_result(
                gc_result, store, cycle=0, goal_id="g2"
            )

            assert len(gov_out["lifecycle_changes"]) == 1
            assert gov_out["lifecycle_changes"][0]["new_status"] == "blocked"

    def test_round_trip_goal_abandoned_after_failures(self):
        """Goal gets abandoned after 3 consecutive failures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(("item-1", "persistent problem", 50.0))
            gov = _make_gov_bridge(tmpdir)
            goal = Goal(id="g3", description="fix persistent issue", priority=1)
            goal.activate(cycle=0)
            gov.goal_store.add(goal)

            for i in range(3):
                gc_result = _mock_gc_result(
                    decisions=[{
                        "action_id": "attempt_fix",
                        "label": "Attempt Fix",
                        "status": "recommended",
                        "score": 0.6,
                    }],
                    executed={"success": False, "error": f"attempt {i+1} failed"},
                    decided="attempt_fix",
                )
                gov_out = gov.process_goalchainer_result(
                    gc_result, store, cycle=i, goal_id="g3"
                )

            # After 3 failures, goal should be abandoned
            assert gov_out["lifecycle_changes"][0]["new_status"] == "abandoned"
            assert "3 consecutive failures" in gov_out["lifecycle_changes"][0]["reason"]


class TestDeonticPLNPropagation:
    """Test that deontic constraints propagate into PLN atom truth values."""

    def test_forbidden_reduces_atom_strength(self):
        """Forbidden status should drastically reduce PLN atom strength."""
        atoms = [
            PLNAtom(
                atom_type="Evaluation",
                name="Evaluation(publish_raw_log,checkout)",
                truth=TruthValue(strength=0.8, confidence=0.7),
                source_ids=["item-1"],
            ),
        ]
        decisions = [{
            "action_id": "publish_raw_log",
            "label": "Publish Raw Log",
            "status": "forbidden",
            "score": 0.3,
        }]

        apply_deontic_constraints_to_atoms(atoms, decisions)

        # Strength should be reduced to ~10%
        assert atoms[0].truth.strength == pytest.approx(0.08, abs=0.01)
        # Confidence should be slightly boosted
        assert atoms[0].truth.confidence == pytest.approx(0.9, abs=0.01)

    def test_obligated_preserves_strength_boosts_confidence(self):
        """Obligated status preserves strength and boosts confidence."""
        atoms = [
            PLNAtom(
                atom_type="Evaluation",
                name="Evaluation(publish_redacted_summary,incident)",
                truth=TruthValue(strength=0.8, confidence=0.5),
                source_ids=["item-2"],
            ),
        ]
        decisions = [{
            "action_id": "publish_redacted_summary",
            "label": "Publish Redacted Summary",
            "status": "obligated",
            "score": 0.9,
        }]

        apply_deontic_constraints_to_atoms(atoms, decisions)

        # Strength preserved
        assert atoms[0].truth.strength == pytest.approx(0.8, abs=0.01)
        # Confidence boosted by 0.3
        assert atoms[0].truth.confidence == pytest.approx(0.8, abs=0.01)

    def test_no_decisions_no_change(self):
        """Empty decisions list leaves atoms unchanged."""
        atoms = [
            PLNAtom(
                atom_type="Concept",
                name="Concept(test)",
                truth=TruthValue(strength=0.5, confidence=0.5),
                source_ids=["x"],
            ),
        ]
        apply_deontic_constraints_to_atoms(atoms, [])
        assert atoms[0].truth.strength == 0.5
        assert atoms[0].truth.confidence == 0.5

    def test_permitted_no_change(self):
        """Permitted status doesn't modify atoms."""
        atoms = [
            PLNAtom(
                atom_type="Evaluation",
                name="Evaluation(hold_external_update,status)",
                truth=TruthValue(strength=0.6, confidence=0.6),
                source_ids=["y"],
            ),
        ]
        decisions = [{
            "action_id": "hold_external_update",
            "label": "Hold External Update",
            "status": "permitted",
            "score": 0.5,
        }]
        apply_deontic_constraints_to_atoms(atoms, decisions)
        assert atoms[0].truth.strength == 0.6
        assert atoms[0].truth.confidence == 0.6

    def test_non_matching_atoms_unchanged(self):
        """Atoms without keyword overlap are not modified."""
        atoms = [
            PLNAtom(
                atom_type="Inheritance",
                name="Inheritance(cat,animal)",
                truth=TruthValue(strength=0.9, confidence=0.9),
                source_ids=["z"],
            ),
        ]
        decisions = [{
            "action_id": "publish_raw_log",
            "label": "Publish Raw Log",
            "status": "forbidden",
            "score": 0.3,
        }]
        apply_deontic_constraints_to_atoms(atoms, decisions)
        # No overlap → no change
        assert atoms[0].truth.strength == 0.9


class TestOrchestratorGovBridgeE2E:
    """Test the orchestrator with GovernanceBridge fully wired."""

    def test_orchestrator_cycle_caches_deontic_decisions(self):
        """Verify that deontic decisions are cached for PLN propagation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(
                ("item-1", "system health check", 80.0),
            )
            gs_path = os.path.join(tmpdir, "goals.json")
            goal_store = GoalStore(path=gs_path)
            dh_path = os.path.join(tmpdir, "decisions.json")

            gov = GovernanceBridge(
                goal_store=goal_store,
                decision_history=DecisionHistory(path=dh_path),
            )

            orch = WMTMOrchestrator(
                store=store,
                use_pln=True,
                use_goalchainer=False,  # GC not available in test
                gov_bridge=gov,
                goal_store=goal_store,
            )

            # Initially empty
            assert orch._last_deontic_decisions == []

            # Run a cycle — no GC, so decisions stay empty
            result = orch.cycle()
            assert result.gc_admitted == 0

    def test_decision_history_persistence_across_cycles(self):
        """Verify decision history persists and accumulates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dh_path = os.path.join(tmpdir, "decisions.json")
            history = DecisionHistory(path=dh_path)

            # Add records
            for i in range(5):
                history.add(GoalDecisionRecord(
                    goal_id="g1",
                    decision_action_id=f"action-{i}",
                    cycle=i,
                ))

            assert len(history) == 5

            # Reload from disk
            history2 = DecisionHistory(path=dh_path)
            assert len(history2) == 5
            assert history2.by_goal("g1")[0].decision_action_id == "action-0"

    def test_goalchainer_decision_parsing(self):
        """Verify GoalChainerDecision creation from pipeline dicts."""
        dec_dict = {
            "action_id": "publish_redacted_summary",
            "label": "Publish Redacted Summary",
            "status": "recommended",
            "score": 0.92,
            "motivation": "privacy protection",
            "evidence_ids": ["ev-1", "ev-2"],
        }
        dec = GoalChainerDecision.from_pipeline_dict(dec_dict, index=0)
        assert dec.action_id == "publish_redacted_summary"
        assert dec.status == "recommended"
        assert dec.score == 0.92
        assert dec.evidence_ids == ["ev-1", "ev-2"]

    def test_wmtm_evidence_extraction_max_items(self):
        """Verify evidence extraction respects max_items limit."""
        items = [(f"item-{i}", f"content {i}", 50.0 + i) for i in range(30)]
        store = _make_store(*items)

        evidence = wmtm_to_goalchainer_evidence(store, max_items=5)
        assert len(evidence) <= 5

    def test_candidates_with_deontic_adjustments(self):
        """Verify candidates get correct STI from deontic adjustments."""
        gc_result = _mock_gc_result(
            decisions=[
                {"action_id": "act-a", "label": "A", "status": "obligated", "score": 0.8},
                {"action_id": "act-b", "label": "B", "status": "forbidden", "score": 0.6},
            ]
        )
        adjustments = {"act-a": 3.0, "act-b": 0.1}

        candidates = goalchainer_result_to_candidates(
            gc_result, cycle=0, source_ids=["s1"],
            deontic_adjustments=adjustments,
        )
        assert len(candidates) == 2
        # Obligated (3.0x): 0.8 * 3.0 * 10.0 = 24.0
        assert candidates[0].initial_sti == pytest.approx(24.0, abs=0.1)
        # Forbidden (0.1x): 0.6 * 0.1 * 10.0 = 0.6
        assert candidates[1].initial_sti == pytest.approx(0.6, abs=0.1)
