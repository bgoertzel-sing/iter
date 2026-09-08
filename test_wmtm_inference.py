"""Tests for WMTM Inference Engine."""
from wmtm.inference import (
    WMTMInferenceEngine,
    InferenceCandidate,
    BeliefTriple,
    extract_triples,
)
from wmtm.store import WMTMStore


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


# --- analogy tests ---

def test_analogy_basic():
    """Two pairs with same relation, different subject/object => analogical transfer."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "rain implies wet", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    analogy_candidates = [c for c in candidates if c.inference_type == 'analogy']
    assert len(analogy_candidates) >= 1
    # Should derive fire implies wet or rain implies smoke
    contents = [c.content for c in analogy_candidates]
    assert any("wet" in c for c in contents) or any("smoke" in c for c in contents)


def test_analogy_no_transfer_same_pair():
    """Identical subject+object should not trigger analogy."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    candidates = engine.infer(store.get_active_set())
    analogy_candidates = [c for c in candidates if c.inference_type == 'analogy']
    assert len(analogy_candidates) == 0


def test_analogy_low_confidence():
    """Analogy candidates should have low (speculative) confidence."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "rain implies wet", initial_sti=5.0)
    engine = WMTMInferenceEngine(min_confidence=0.0)
    candidates = engine.infer(store.get_active_set())
    analogy_candidates = [c for c in candidates if c.inference_type == 'analogy']
    for ac in analogy_candidates:
        assert ac.confidence <= 0.3  # analogy is speculative


# --- evidence aggregation tests ---

def test_evidence_aggregation_basic():
    """Multiple items with same (subject, relation, object) => aggregated evidence."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies smoke", initial_sti=5.0)
    store.admit("i3", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine(min_confidence=0.0, sti_focus_ratio=1.0)
    candidates = engine.infer(store.get_active_set())
    agg_candidates = [c for c in candidates if c.inference_type == 'evidence_aggregation']
    assert len(agg_candidates) >= 1
    ac = agg_candidates[0]
    assert "aggregated" in ac.content
    assert "3 sources" in ac.content
    assert ac.confidence > 0.7  # 3 sources should boost confidence


def test_evidence_aggregation_no_single():
    """Single item should not trigger evidence aggregation."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine(min_confidence=0.0)
    candidates = engine.infer(store.get_active_set())
    agg_candidates = [c for c in candidates if c.inference_type == 'evidence_aggregation']
    assert len(agg_candidates) == 0


def test_evidence_aggregation_capped():
    """Evidence aggregation confidence should be capped at 0.95."""
    store = WMTMStore(capacity=30)
    for i in range(10):
        store.admit(f"i{i}", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine(min_confidence=0.0, inference_budget=50, sti_focus_ratio=1.0)
    candidates = engine.infer(store.get_active_set())
    agg_candidates = [c for c in candidates if c.inference_type == 'evidence_aggregation']
    assert len(agg_candidates) >= 1
    assert agg_candidates[0].confidence <= 0.95


# --- contradiction detection tests ---

def test_detect_contradictions_basic():
    """Same subject+relation, different objects => contradiction."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) >= 1
    r = reports[0]
    assert r.subject == "fire"
    assert r.relation == "implies"
    assert "smoke" in r.conflicting_objects
    assert "danger" in r.conflicting_objects
    assert r.severity > 0


def test_detect_contradictions_none():
    """No contradictions when all items agree."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies smoke", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) == 0


def test_detect_contradictions_multiple():
    """Multiple conflicting objects => higher severity."""
    store = WMTMStore(capacity=20)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "fire implies danger", initial_sti=5.0)
    store.admit("i3", "fire implies heat", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    reports = engine.detect_contradictions(store)
    assert len(reports) >= 1
    assert len(reports[0].conflicting_objects) >= 3
    assert reports[0].severity > 0.6  # 3 conflicts * 0.3


# --- inference budget tests ---

def test_inference_budget_limits_candidates():
    """Inference budget should limit number of candidates returned."""
    store = WMTMStore(capacity=30)
    # Create many deduction chains
    for i in range(10):
        store.admit(f"chain_a{i}", f"a{i} implies b{i}", initial_sti=5.0)
        store.admit(f"chain_b{i}", f"b{i} implies c{i}", initial_sti=5.0)
    engine = WMTMInferenceEngine(inference_budget=3, min_confidence=0.0, sti_focus_ratio=1.0)
    candidates = engine.infer(store.get_active_set())
    assert len(candidates) <= 3


def test_focus_set_filters_by_sti():
    """_focus_set should return only top-N% STI items."""
    store = WMTMStore(capacity=20)
    store.admit("low1", "x implies y", initial_sti=1.0)
    store.admit("low2", "y implies z", initial_sti=1.0)
    store.admit("high1", "a implies b", initial_sti=10.0)
    store.admit("high2", "b implies c", initial_sti=10.0)
    engine = WMTMInferenceEngine(sti_focus_ratio=0.5)
    focus = engine._focus_set(store.get_active_set())
    assert len(focus) == 2
    # Should be the high-STI items
    stis = [it.attention.sti for it in focus]
    assert all(s >= 10.0 for s in stis)


def test_focus_set_small_set_returns_all():
    """_focus_set should return all items when set is small (<=2)."""
    store = WMTMStore(capacity=10)
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "smoke implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    focus = engine._focus_set(store.get_active_set())
    assert len(focus) == 2


# --- generate_candidates (alias for infer) ---


def test_generate_candidates_alias():
    """generate_candidates should produce same results as infer."""
    store = WMTMStore()
    store.admit("i1", "fire implies smoke", initial_sti=5.0)
    store.admit("i2", "smoke implies danger", initial_sti=5.0)
    engine = WMTMInferenceEngine()
    active = store.get_active_set()
    via_infer = engine.infer(active)
    via_alias = engine.generate_candidates(active)
    assert len(via_alias) == len(via_infer)
    # Same content, just different candidate IDs
    infer_contents = {c.content for c in via_infer}
    alias_contents = {c.content for c in via_alias}
    assert infer_contents == alias_contents


def test_generate_candidates_empty():
    """generate_candidates on empty active set returns []."""
    engine = WMTMInferenceEngine()
    result = engine.generate_candidates([])
    assert result == []


# --- BeliefTriple.to_text ---


def test_belief_triple_to_text():
    """to_text should return 'subject relation object' format."""
    t = BeliefTriple(subject="fire", relation="implies", object="smoke")
    assert t.to_text() == "fire implies smoke"


def test_belief_triple_to_text_isa():
    """to_text with is-a relation."""
    t = BeliefTriple(subject="socrates", relation="is-a", object="man")
    assert t.to_text() == "socrates is-a man"


def test_belief_triple_to_text_has():
    """to_text with has relation."""
    t = BeliefTriple(subject="car", relation="has", object="wheels")
    assert t.to_text() == "car has wheels"
