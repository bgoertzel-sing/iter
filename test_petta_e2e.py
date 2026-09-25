"""Lightweight end-to-end tests for WMTM + PeTTa integration.

Uses direct SWI-Prolog subprocess calls with short-running MeTTa expressions.
No heavy GoalChainer imports — verifies the data pipeline, not GC internals.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wmtm.store import WMTMStore
from wmtm.item import WMTMItem, AttentionValue
from wmtm.goal import Goal
from wmtm.goal_store import GoalStore
from wmtm.gov_bridge import GovernanceBridge, DecisionHistory
from wmtm.goalchainer_bridge import (
    wmtm_to_goalchainer_evidence,
    goalchainer_result_to_candidates,
    parse_decisions,
)
from wmtm.gc_adapter import (
    WMTMEvidenceReasoner,
    wmtm_goal_to_gc_goal,
    wmtm_items_to_actions,
)

SWIPL = Path("/home/openclaw/research-agent/projects/omegaclaw/local/swipl-9.3.36/lib/swipl/bin/x86_64-linux/swipl")
PETTA_MAIN = Path("/home/openclaw/research-agent/projects/omegaclaw/repos/PeTTa/src/main.pl")


def run_metta_expr(expr: str, timeout_s: int = 10) -> str:
    """Run a MeTTa expression via PeTTa/SWI-Prolog and return stdout."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".metta", delete=False) as f:
        f.write(expr + "\n")
        f.flush()
        try:
            result = subprocess.run(
                [str(SWIPL), "-q", "-s", str(PETTA_MAIN), "--", f.name],
                capture_output=True, text=True, timeout=timeout_s,
            )
            return result.stdout
        finally:
            os.unlink(f.name)


def petta_available() -> bool:
    return SWIPL.exists() and PETTA_MAIN.exists()


class TestWMTMToPeTTaPipeline(unittest.TestCase):
    """Test WMTM data flows correctly into PeTTa-compatible formats."""

    def setUp(self):
        self.store = WMTMStore()
        self._tmpdir = tempfile.mkdtemp()
        self.goal_store = GoalStore(os.path.join(self._tmpdir, "goals.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_wmtm_items_produce_valid_evidence(self):
        self.store.admit("chemical_bond", "covalent bond energy 3.5 eV",
                         initial_sti=80.0)
        evidence = wmtm_to_goalchainer_evidence(self.store)
        self.assertIsInstance(evidence, list)
        self.assertTrue(len(evidence) > 0)
        ev_str = json.dumps(evidence)
        self.assertIn("chemical_bond", ev_str)

    def test_wmtm_goal_converts_to_gc_format(self):
        goal = Goal("synthesize_compound", {"target": "H2O"})
        goal.activate(cycle=1)
        gc_goal = wmtm_goal_to_gc_goal(goal)
        self.assertIsNotNone(gc_goal)
        self.assertIn("synthesize_compound", str(gc_goal))

    def test_wmtm_items_convert_to_actions(self):
        self.store.admit("action_heat", "heat temperature 100 degrees",
                         initial_sti=90.0)
        self.store.admit("action_mix", "mix speed fast reaction",
                         initial_sti=30.0)
        actions = wmtm_items_to_actions(self.store)
        self.assertIsInstance(actions, list)
        # CandidateAction is a dataclass, not JSON-serializable directly
        action_ids = [a.id for a in actions]
        self.assertTrue(any("action_heat" in aid for aid in action_ids))

    def test_evidence_reasoner_produces_rankings(self):
        store = WMTMStore()
        store.admit("obs_A", "observation alpha value 1",
                     initial_sti=90.0)
        store.admit("obs_B", "observation beta value 2",
                     initial_sti=40.0)
        reasoner = WMTMEvidenceReasoner(store)
        # WMTMEvidenceReasoner indexes active items; verify index has entries
        self.assertTrue(len(reasoner._active_index) > 0)
        # Active set should be sorted by STI (highest first)
        active = store.get_active_set()
        self.assertEqual(len(active), 2)
        self.assertEqual(active[0].id, "obs_A")

    def test_governance_bridge_roundtrip(self):
        store = WMTMStore()
        _tmpdir = tempfile.mkdtemp()
        goal_store = GoalStore(os.path.join(_tmpdir, "goals.json"))
        goal = Goal("test_goal", {"param": "value"})
        goal.activate(cycle=1)
        goal_store.add(goal)
        store.admit("evidence_1", "evidence supports test_goal",
                     initial_sti=70.0)
        history = DecisionHistory(os.path.join(_tmpdir, "history.json"))
        bridge = GovernanceBridge(goal_store, decision_history=history)
        # With empty GC result, governance should still process cleanly
        result = bridge.process_goalchainer_result(
            {"decisions": []}, store, cycle=1
        )
        self.assertIsNotNone(result)
        import shutil
        shutil.rmtree(_tmpdir, ignore_errors=True)

    def test_parse_decisions_handles_gc_output(self):
        gc_output = {
            "decisions": [
                {"action_id": "proceed_g1", "label": "proceed",
                 "status": "obligated", "score": 0.85},
                {"action_id": "halt_g2", "label": "halt",
                 "status": "forbidden", "score": 0.92},
            ]
        }
        decisions = parse_decisions(gc_output)
        self.assertEqual(len(decisions), 2)
        # parse_decisions returns GoalChainerDecision objects, not dicts
        self.assertEqual(decisions[0].status, "obligated")
        self.assertEqual(decisions[1].status, "forbidden")

    def test_candidate_generation_from_gc_result(self):
        gc_result = {
            "decisions": [
                {"action_id": "react_A", "label": "react_A",
                 "status": "recommended", "score": 0.9},
                {"action_id": "react_B", "label": "react_B",
                 "status": "permitted", "score": 0.3},
            ]
        }
        candidates = goalchainer_result_to_candidates(gc_result)
        self.assertIsInstance(candidates, list)
        self.assertEqual(len(candidates), 2)


@unittest.skipUnless(petta_available(), "PeTTa runtime not available")
class TestPeTTaRuntimeDirect(unittest.TestCase):
    """Test PeTTa runtime with short-running MeTTa expressions."""

    def test_arithmetic(self):
        out = run_metta_expr("!(+ 2 3)")
        self.assertIn("5", out)

    def test_boolean_logic(self):
        out = run_metta_expr("!(if True ok fail)")
        self.assertIn("ok", out)

    def test_type_definition(self):
        program = "(: myval Int)\n(= (myval) 42)\n!(myval)"
        out = run_metta_expr(program)
        self.assertIn("42", out)

    def test_equality(self):
        out = run_metta_expr("!(== 3 3)")
        # PeTTa outputs lowercase "true"
        self.assertIn("true", out.lower())

    def test_list_construction(self):
        out = run_metta_expr("!(cons-atom A (B C))")
        self.assertTrue("A" in out)


@unittest.skipUnless(petta_available(), "PeTTa runtime not available")
class TestWMTMPeTTaIntegration(unittest.TestCase):
    """Test that WMTM-generated evidence can be expressed as MeTTa and evaluated."""

    def test_wmtm_evidence_as_metta_atoms(self):
        """Convert WMTM items to MeTTa atom declarations and evaluate."""
        store = WMTMStore()
        store.admit("bond_energy", "bond energy 3.5 eV covalent",
                     initial_sti=80.0)
        store.admit("reaction_temp", "reaction temperature 100 kelvin",
                     initial_sti=60.0)
        # Generate MeTTa program from WMTM evidence
        evidence = wmtm_to_goalchainer_evidence(store)
        lines = []
        for ev in evidence:
            name = ev.get("id", "unknown")
            sti = ev.get("sti", 0)
            lines.append(f"(= (sti-of {name}) {int(sti)})")
        lines.append("!(sti-of bond_energy)")
        program = "\n".join(lines)
        out = run_metta_expr(program)
        self.assertIn("80", out)

    def test_governance_constraint_as_metta(self):
        """Express a deontic constraint in MeTTa and verify evaluation."""
        program = """
(= (deontic forbidden) 0.0)
(= (deontic obligated) 1.0)
(= (deontic recommended) 0.7)
(= (deontic permitted) 0.5)
!(deontic obligated)
!(deontic forbidden)
"""
        out = run_metta_expr(program)
        self.assertIn("1.0", out)
        self.assertIn("0.0", out)


if __name__ == "__main__":
    unittest.main()
