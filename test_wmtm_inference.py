"""Tests for WMTM Inference Engine."""
import pytest
from wmtm.inference import (
    WMTMInferenceEngine,
    InferenceCandidate,
    BeliefTriple,
    extract_triples,
)
from wmtm.store import WMTMStore
from wmtm.item import WMTMItem
from wmtm.attention import AttentionValue


# --- extract_triples tests ---

def test_extract_arrow_implication():
    triples = extract_triples("fire implies smoke")
    assert len(triples) == 1
    assert triples[0].subject == "fire"
    assert triples[0].relation == "implies"
    assert triples[0].object == "smoke"


def test_extract_arrow_symbol():
    triples = extract_triples("rain -> wet")
    assert len(triples) >= 1
    assert any(t.subject == "rain" and t.object == "wet" for t in triples)


def test_extract_isa():
    triples = extract_triples("socrates is a man")
    assert len(triples) == 1
    assert triples[0].relation == "is-a"
    assert triples[0].object == "man"


def test_extract_has():
    triples = extract_triples("dog has tail")
    assert len(triples) == 1
    assert triples[0].relation == "has"
    assert triples[0].object == "tail"


def test_extract_multiple():
    triples = extract_triples("fire implies smoke and water is a liquid")
    assert len(triples) >= 2


def test_extract_empty():
    triples = extract_triples("hello world nothing here")
    assert len(triples) == 0


# --- deduction tests ---

def test_deduction_basic():
    """A->B, B->C => A->C"""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "smoke implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    deduction_candidates = [c for c in candidates if c.inference_type == 'deduction']
    assert len(deduction_candidates) >= 1
    dc = deduction_candidates[0]
    assert dc.triple.subject == "fire"
    assert dc.triple.object == "danger"
    assert dc.confidence > 0
    assert len(dc.derived_from) == 2


def test_deduction_no_chain():
    """If no chain exists, no deduction candidates."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "water implies wet", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    deduction_candidates = [c for c in candidates if c.inference_type == 'deduction']
    assert len(deduction_candidates) == 0


# --- induction tests ---

def test_induction_basic():
    """Multiple A->B instances => generalize."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    induction_candidates = [c for c in candidates if c.inference_type == 'induction']
    assert len(induction_candidates) >= 1
    ic = induction_candidates[0]
    assert ic.confidence > 0.5  # 2/3 = 0.667
    assert len(ic.derived_from) >= 2


def test_induction_no_duplicate():
    """Single instance should not trigger induction."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    induction_candidates = [c for c in candidates if c.inference_type == 'induction']
    assert len(induction_candidates) == 0


# --- abduction tests ---

def test_abduction_basic():
    """B observed, A->B => maybe A."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "I see smoke everywhere", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    abduction_candidates = [c for c in candidates if c.inference_type == 'abduction']
    assert len(abduction_candidates) >= 1
    ac = abduction_candidates[0]
    assert "fire" in ac.content.lower()
    assert ac.confidence < 0.5  # abduction is weak


def test_abduction_no_match():
    """If consequent not observed, no abduction."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "the sky is blue", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    abduction_candidates = [c for c in candidates if c.inference_type == 'abduction']
    assert len(abduction_candidates) == 0


# --- is_novel and admit_derived tests ---

def test_is_novel_true():
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    cand = InferenceCandidate(
        content="water implies ice",
        confidence=0.5,
        derived_from=["i1"],
        inference_type="deduction",
        initial_sti=3.0,
    )
    assert engine.is_novel(cand, store) is True


def test_is_novel_false_duplicate():
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    cand = InferenceCandidate(
        content="fire implies smoke",
        confidence=0.5,
        derived_from=["i1"],
        inference_type="deduction",
        initial_sti=3.0,
    )
    assert engine.is_novel(cand, store) is False


def test_admit_derived_adds_to_store():
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "smoke implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    admitted = engine.admit_derived(candidates, store)
    # At least one should be admitted (deduction of fire->danger)
    assert len(admitted) >= 1
    # Check it's in the store
    active = store.get_active_set()
    assert any(a.id == item.id for item in active for a in admitted)


def test_admit_derived_skips_duplicates():
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "smoke implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    # First admission
    admitted1 = engine.admit_derived(candidates, store)
    # Second admission should not re-add duplicates
    admitted2 = engine.admit_derived(candidates, store)
    assert len(admitted2) == 0


def test_min_confidence_filter():
    """Candidates below min_confidence are filtered out."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "I see smoke", initial_sti=5.0)
    # Set high min_confidence to filter out abduction (0.3)
    engine = WMTMInferenceEngine(min_confidence=0.5)
    candidates = engine.infer(store.get_active_set())
    abduction_candidates = [c for c in candidates if c.inference_type == 'abduction']
    assert len(abduction_candidates) == 0


def test_full_pipeline_deduction_and_admit():
    """End-to-end: infer + admit + verify store grew."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "rain implies wet", initial_sti=8.0)
    store.admit("i2", "wet implies slippery", initial_sti=8.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    initial_count = len(store.get_active_set())
    admitted = engine.admit_derived(candidates, store)
    final_count = len(store.get_active_set())
    assert final_count > initial_count
    # Verify the deduced item exists
    active = store.get_active_set()
    contents = [item.content.lower() for item in active]
    assert any("rain" in c and "slippery" in c for c in contents)
