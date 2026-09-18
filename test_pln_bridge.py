"""Dedicated tests for pln_bridge.py."""
import pytest
from wmtm.store import WMTMStore
from wmtm.item import WMTMItem
from wmtm.attention import AttentionValue
from wmtm.pln import TruthValue, PLNAtom
from wmtm.pln_bridge import (
    wmtm_item_to_pln_atoms,
    wmtm_to_pln_atoms,
    pln_atom_to_wmtm_candidate,
    pln_atoms_to_wmtm_candidates,
    atom_name_to_text,
    run_pln_inference_over_wmtm,
)


def _make_item(item_id="i1", content="dog is a animal", sti=5.0, utility=0.0, source_type="recalled"):
    return WMTMItem(
        id=item_id,
        content=content,
        source_type=source_type,
        attention=AttentionValue(sti=sti),
        utility=utility,
    )


class TestWmtmItemToPlnAtoms:
    def test_extracts_inheritance_atom(self):
        item = _make_item(content="dog is a animal")
        atoms = wmtm_item_to_pln_atoms(item)
        assert len(atoms) >= 1
        assert any(a.atom_type == "Inheritance" for a in atoms)

    def test_fallback_concept_atom(self):
        item = _make_item(content="just some text with no pattern")
        atoms = wmtm_item_to_pln_atoms(item)
        assert len(atoms) == 1
        assert atoms[0].atom_type == "Concept"

    def test_truth_value_from_attention(self):
        item = _make_item(sti=100.0, utility=3.0)
        atoms = wmtm_item_to_pln_atoms(item)
        assert atoms[0].truth.confidence == pytest.approx(0.5)
        assert atoms[0].truth.strength == 0.8  # F04: default TV strength




class TestWmtmToPlnAtoms:
    def test_multiple_items(self):
        store = WMTMStore(capacity=10)
        store.admit("i1", "cat is a animal", initial_sti=5.0)
        store.admit("i2", "dog is a mammal", initial_sti=3.0)
        atoms = wmtm_to_pln_atoms(store.get_active_set())
        assert len(atoms) >= 2


class TestPlnAtomToWmtmCandidate:
    def test_concept_returns_none(self):
        atom = PLNAtom(atom_type="Concept", name="Concept(dog)", truth=TruthValue(0.8, 0.9))
        cand = pln_atom_to_wmtm_candidate(atom)
        assert cand is None

    def test_inheritance_atom_converts(self):
        atom = PLNAtom(
            atom_type="Inheritance",
            name="Inheritance(dog,animal)",
            truth=TruthValue(strength=0.9, confidence=0.8),
            source_ids=["i1"],
        )
        cand = pln_atom_to_wmtm_candidate(atom)
        assert cand is not None
        assert "dog is a animal" in cand.content
        assert cand.confidence == 0.8
        assert cand.inference_type == "pln_inheritance"
        assert cand.initial_sti == pytest.approx(0.9 * 0.8)


class TestAtomNameToText:
    def test_inheritance(self):
        atom = PLNAtom(atom_type="Inheritance", name="Inheritance(dog,animal)", truth=TruthValue())
        assert atom_name_to_text(atom) == "dog is a animal"

    def test_implication(self):
        atom = PLNAtom(atom_type="Implication", name="Implication(rain,wet)", truth=TruthValue())
        assert atom_name_to_text(atom) == "rain implies wet"

    def test_similarity(self):
        atom = PLNAtom(atom_type="Similarity", name="Similarity(cat,dog)", truth=TruthValue())
        assert atom_name_to_text(atom) == "cat is similar to dog"

    def test_no_match_returns_name(self):
        atom = PLNAtom(atom_type="Unknown", name="SomeWeirdName", truth=TruthValue())
        assert atom_name_to_text(atom) == "SomeWeirdName"


class TestPlnAtomsToWmtmCandidates:
    def test_filters_concepts(self):
        atoms = [
            PLNAtom(atom_type="Concept", name="Concept(x)", truth=TruthValue(0.5, 0.5)),
            PLNAtom(atom_type="Inheritance", name="Inheritance(a,b)", truth=TruthValue(0.7, 0.6), source_ids=["s1"]),
        ]
        cands = pln_atoms_to_wmtm_candidates(atoms)
        assert len(cands) == 1
        assert cands[0].inference_type == "pln_inheritance"

    def test_empty_list(self):
        assert pln_atoms_to_wmtm_candidates([]) == []
