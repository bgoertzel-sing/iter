"""Test suite for WMTM+PLN integration."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from wmtm.pln import TruthValue, PLNAtom, PLNInferenceEngine, extract_pln_atoms, pln_inference_over_atoms
from wmtm.pln_bridge import run_pln_inference_over_wmtm, wmtm_to_pln_atoms, pln_atoms_to_wmtm_candidates
from wmtm.store import WMTMStore


# ─── TruthValue Tests ───────────────────────────────────────

class TestTruthValue:
    def test_conjunct(self):
        tv1 = TruthValue(0.9, 0.8)
        tv2 = TruthValue(0.7, 0.6)
        result = tv1.conjunct(tv2)
        assert result.strength == 0.7
        assert abs(result.confidence - 0.48) < 1e-6

    def test_disjunct(self):
        tv1 = TruthValue(0.9, 0.8)
        tv2 = TruthValue(0.7, 0.6)
        result = tv1.disjunct(tv2)
        assert result.strength == 0.9
        assert abs(result.confidence - 0.92) < 1e-6

    def test_negate(self):
        tv = TruthValue(0.9, 0.8)
        result = tv.negate()
        assert abs(result.strength - 0.1) < 1e-6
        assert result.confidence == 0.8

    def test_from_evidence(self):
        tv = TruthValue.from_evidence(8, 2, lookahead=1.0)
        assert abs(tv.strength - 0.8) < 1e-6
        assert abs(tv.confidence - 10/11) < 1e-6

    def test_from_evidence_zero(self):
        tv = TruthValue.from_evidence(0, 0)
        assert tv.strength == 0.5
        assert tv.confidence == 0.0

    def test_add_evidence(self):
        tv = TruthValue.from_evidence(4, 1, lookahead=1.0)
        tv2 = tv.add_evidence(4, 1, lookahead=1.0)
        assert tv2.strength > 0.7  # still mostly positive
        assert tv2.confidence > tv.confidence  # confidence increased

    def test_to_dict_from_dict(self):
        tv = TruthValue(0.75, 0.6)
        d = tv.to_dict()
        tv2 = TruthValue.from_dict(d)
        assert tv == tv2


# ─── PLN Inference Engine Tests ─────────────────────────────

class TestPLNInferenceEngine:
    def setup_method(self):
        self.engine = PLNInferenceEngine()

    def test_deduction(self):
        ab = TruthValue(0.9, 0.8)
        bc = TruthValue(0.8, 0.7)
        result = self.engine.deduction(ab, bc)
        assert 0 < result.strength < 1
        assert result.confidence > 0
        assert result.strength < ab.strength  # deduction reduces strength

    def test_abduction(self):
        ab = TruthValue(0.9, 0.8)
        b_obs = TruthValue(0.8, 0.9)
        result = self.engine.abduction(ab, b_obs)
        assert 0 < result.strength < 1
        assert result.confidence > 0

    def test_induction(self):
        ab = TruthValue(0.9, 0.8)
        ac = TruthValue(0.85, 0.75)
        result = self.engine.induction(ab, ac)
        assert 0 < result.strength < 1
        assert result.confidence < min(ab.confidence, ac.confidence)  # weaker

    def test_evidence_aggregation(self):
        evidence = [TruthValue(0.8, 0.6), TruthValue(0.9, 0.7)]
        result = self.engine.evidence_aggregation(evidence)
        assert 0.8 < result.strength < 0.9
        assert result.confidence > 0.7

    def test_evidence_aggregation_empty(self):
        result = self.engine.evidence_aggregation([])
        assert result.strength == 0.5
        assert result.confidence == 0.0

    def test_contradiction_detection(self):
        a = TruthValue(0.9, 0.8)
        neg_a = TruthValue(0.8, 0.7)
        severity = self.engine.contradiction_strength(a, neg_a)
        assert severity > 0

    def test_contradiction_low_confidence(self):
        a = TruthValue(0.9, 0.01)
        neg_a = TruthValue(0.8, 0.01)
        severity = self.engine.contradiction_strength(a, neg_a)
        assert severity == 0.0  # below threshold

    def test_threshold(self):
        tv = TruthValue(0.5, 0.1)
        assert self.engine.is_above_threshold(tv)
        tv_low = TruthValue(0.01, 0.01)
        assert not self.engine.is_above_threshold(tv_low)


# ─── Atom Extraction Tests ──────────────────────────────────

class TestExtractAtoms:
    def test_implies(self):
        atoms = extract_pln_atoms('dogs implies animals', 'src1')
        assert len(atoms) == 1
        assert atoms[0].atom_type == 'Implication'
        assert 'dogs' in atoms[0].name and 'animals' in atoms[0].name
        assert 'src1' in atoms[0].source_ids

    def test_isa(self):
        atoms = extract_pln_atoms('cats is a mammal', 'src2')
        assert len(atoms) == 1
        assert atoms[0].atom_type == 'Inheritance'

    def test_has(self):
        atoms = extract_pln_atoms('dogs have fur', 'src3')
        assert len(atoms) == 1
        assert atoms[0].atom_type == 'Evaluation'

    def test_similar(self):
        atoms = extract_pln_atoms('cats is similar to dogs', 'src4')
        assert len(atoms) == 1
        assert atoms[0].atom_type == 'Similarity'

    def test_multiple(self):
        atoms = extract_pln_atoms('dogs implies animals. animals implies living. cats is a mammal')
        assert len(atoms) == 3

    def test_no_match(self):
        atoms = extract_pln_atoms('the weather is nice today')
        assert len(atoms) == 0


# ─── PLN Inference Over Atoms Tests ─────────────────────────

class TestPLNInferenceOverAtoms:
    def test_deduction_chain(self):
        atoms = extract_pln_atoms('dogs implies animals. animals implies living')
        derived = pln_inference_over_atoms(atoms)
        names = [a.name for a in derived]
        assert any('dogs' in n and 'living' in n for n in names)

    def test_induction_similarity(self):
        atoms = extract_pln_atoms('dogs implies animals. dogs implies mammal')
        derived = pln_inference_over_atoms(atoms)
        sim_atoms = [a for a in derived if a.atom_type == 'Similarity']
        assert len(sim_atoms) >= 1

    def test_no_inference_without_chain(self):
        atoms = extract_pln_atoms('cats is a mammal')
        derived = pln_inference_over_atoms(atoms)
        assert len(derived) == 0


# ─── PLN Bridge Tests ───────────────────────────────────────

class TestPLNBridge:
    def test_wmtm_to_pln_atoms(self):
        store = WMTMStore(capacity=20)
        store.admit('i1', 'dogs implies animals', source_type='recalled', initial_sti=3.0)
        store.admit('i2', 'animals implies living', source_type='recalled', initial_sti=3.0)
        active = store.get_active_set()
        atoms = wmtm_to_pln_atoms(active)
        assert len(atoms) >= 2

    def test_run_pln_inference_over_wmtm(self):
        store = WMTMStore(capacity=20)
        store.admit('i1', 'dogs implies animals', source_type='recalled', initial_sti=3.0)
        store.admit('i2', 'animals implies living', source_type='recalled', initial_sti=3.0)
        candidates = run_pln_inference_over_wmtm(store)
        assert len(candidates) >= 1
        assert any('dogs' in c.content and 'living' in c.content for c in candidates)

    def test_empty_store(self):
        store = WMTMStore(capacity=10)
        candidates = run_pln_inference_over_wmtm(store)
        assert len(candidates) == 0

    def test_custom_engine(self):
        store = WMTMStore(capacity=20)
        store.admit('i1', 'dogs implies animals', source_type='recalled', initial_sti=3.0)
        store.admit('i2', 'animals implies living', source_type='recalled', initial_sti=3.0)
        strict_engine = PLNInferenceEngine(min_strength=0.9, min_confidence=0.9)
        candidates = run_pln_inference_over_wmtm(store, engine=strict_engine)
        # With very strict thresholds, likely no candidates pass
        # (deduction of 0.5*0.5=0.25 strength won't pass 0.9 threshold)
        assert len(candidates) == 0

    def test_candidate_has_source_ids(self):
        store = WMTMStore(capacity=20)
        store.admit('i1', 'dogs implies animals', source_type='recalled', initial_sti=3.0)
        store.admit('i2', 'animals implies living', source_type='recalled', initial_sti=3.0)
        candidates = run_pln_inference_over_wmtm(store)
        for c in candidates:
            assert len(c.derived_from) >= 2