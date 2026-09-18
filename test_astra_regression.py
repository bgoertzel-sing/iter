"""Regression tests for Astra WMTM review findings (F01-F15)."""
import math
import os
import tempfile

import pytest

from wmtm.store import WMTMStore
from wmtm.inference import WMTMInferenceEngine
from wmtm.orchestrator import WMTMOrchestrator
from wmtm.writeback import WritebackManager
from wmtm.utility import UtilityTracker
from wmtm.forgetting_log import ForgettingLog


def test_f05_has_relation_not_contradiction():
    """F05: Non-functional 'has' relations should not trigger contradictions."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "rabbit has eyes", initial_sti=5.0)
    store.admit("i2", "rabbit has ears", initial_sti=5.0)
    store.admit("i3", "rabbit has fur", initial_sti=5.0)
    store.admit("i4", "rabbit has legs", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) == 0


def test_f05_implies_still_contradicts():
    """F05: Functional 'implies' relations should still trigger contradictions."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies danger", initial_sti=3.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) >= 1


def test_f05_winner_not_in_losers():
    """F05: Winner should not appear in loser list."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=10.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    resolutions = engine.resolve_contradictions(store, reports)
    for res in resolutions:
        assert res.winner_id not in res.loser_ids


def test_f04_implication_atom_name():
    """F04: Arrow/causes pattern should produce Implication() names."""
    from wmtm.pln import extract_pln_atoms
    atoms = extract_pln_atoms("rain causes flooding")
    impl_atoms = [a for a in atoms if a.atom_type == "Implication"]
    assert len(impl_atoms) >= 1
    assert "Implication" in impl_atoms[0].name


def test_f04_implication_chain_stays_implication():
    """F04: Implication chain A->B->C should produce Implication(A,C)."""
    from wmtm.pln import extract_pln_atoms, pln_inference_over_atoms, PLNInferenceEngine
    atoms = []
    atoms.extend(extract_pln_atoms("rain implies flooding", "s1"))
    atoms.extend(extract_pln_atoms("flooding implies damage", "s2"))
    engine = PLNInferenceEngine()
    derived = pln_inference_over_atoms(atoms, engine)
    impl_names = [a.name for a in derived if a.atom_type == "Implication"]
    assert "Implication(rain,damage)" in impl_names


def test_f08_nan_capacity_rejected():
    """F08: NaN capacity should raise ValueError."""
    with pytest.raises(ValueError):
        WMTMStore(capacity=float('nan'))


def test_f