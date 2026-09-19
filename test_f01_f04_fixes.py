"""F01/F02/F03/F04 fix verification tests."""
import json
from wmtm.attention import AttentionValue
from wmtm.item import WMTMItem
from wmtm.store import WMTMStore
from wmtm.orchestrator import WMTMOrchestrator
from wmtm.pln import PLNAtom, PLNInferenceEngine, pln_inference_over_atoms, TruthValue


def test_f01_attention_round_trip():
    av = AttentionValue(sti=5.0, ati=3.0, lti=1.0)
    d = av.to_dict()
    av2 = AttentionValue.from_dict(d)
    assert av2.sti == 5.0
    assert av2.ati == 3.0
    assert av2.lti == 1.0


def test_f01_item_round_trip():
    item = WMTMItem(id="i1", content="test", source_type="derived",
                    derived_from=["p1", "p2"], attention=AttentionValue(sti=10.0, ati=2.0, lti=0.5),
                    age=3, utility=4.5, last_used=7)
    d = item.to_dict()
    item2 = WMTMItem.from_dict(d)
    assert item2.id == "i1"
    assert item2.content == "test"
    assert item2.source_type == "derived"
    assert item2.derived_from == ["p1", "p2"]
    assert item2.attention.sti == 10.0
    assert item2.age == 3
    assert item2.utility == 4.5


def test_f01_store_round_trip():
    store = WMTMStore(capacity=60, tick=5)
    store.admit("a", "alpha", initial_sti=3.0)
    store.admit("b", "beta", initial_sti=2.0)
    d = store.to_dict()
    assert d["capacity"] == 60
    assert d["tick"] == 5
    assert len(d["items"]) == 2
    store2 = WMTMStore.from_dict(d)
    assert store2.capacity == 60
    assert store2._tick == 5
    assert len(store2) == 2
    assert store2.get("a").content == "alpha"
    assert store2.get("b").content == "beta"


def test_f01_orchestrator_snapshot_restore():
    store = WMTMStore(capacity=60)
    orch = WMTMOrchestrator(store)
    orch.store.admit("x", "data", initial_sti=5.0)
    orch._cycle = 7
    snap = orch.snapshot_state()
    assert snap["cycle"] == 7
    assert "x" in snap["store"]["items"]
    snap_json = json.dumps(snap)  # must be JSON serializable
    snap2 = json.loads(snap_json)
    orch2 = WMTMOrchestrator(WMTMStore(capacity=60))
    orch2.restore_state(snap2)
    assert orch2._cycle == 7
    assert orch2.store.get("x").content == "data"


def test_f04_implication_not_inheritance():
    """Arrow atoms should be Implication, not Inheritance."""
    atoms = [
        PLNAtom(atom_type="Implication", name="Implication(A,B)", truth=TruthValue(strength=1.0, confidence=0.9)),
        PLNAtom(atom_type="Implication", name="Implication(B,C)", truth=TruthValue(strength=1.0, confidence=0.9)),
    ]
    derived = pln_inference_over_atoms(atoms)
    names = [d.name for d in derived]
    # Deduction should produce Implication(A,C), not Inheritance(A,C)
    assert "Implication(A,C)" in names
    assert "Inheritance(A,C)" not in names


def test_f04_no_cross_type_deduction():
    """Inheritance and Implication should not mix in deduction."""
    atoms = [
        PLNAtom(atom_type="Inheritance", name="Inheritance(A,B)", truth=TruthValue(strength=1.0, confidence=0.9)),
        PLNAtom(atom_type="Implication", name="Implication(B,C)", truth=TruthValue(strength=1.0, confidence=0.9)),
    ]
    derived = pln_inference_over_atoms(atoms)
    names = [d.name for d in derived]
    # A is-a B and B -> C should NOT produce A is-a C or A -> C
    assert "Inheritance(A,C)" not in names
    assert "Implication(A,C)" not in names


def test_f03_writeback_checks_return():
    """Writeback should not mark items written if append_fn returns ok=False."""
    from wmtm.writeback import WritebackManager, WritebackCandidate
    from wmtm.item import WMTMItem
    from wmtm.attention import AttentionValue
    
    item = WMTMItem(id="w1", content="writeback test", attention=AttentionValue(sti=10.0))
    cand = WritebackCandidate(item=item, reason="high_utility", metta_content="(test content)")
    mgr = WritebackManager()
    
    # Simulate append_fn returning failure
    def fail_fn(content):
        return {"ok": False, "error": "journal full"}
    
    written = mgr.writeback([cand], fail_fn)
    assert len(written) == 0  # should not be marked as written


def test_f03_writeback_success():
    """Writeback should mark items written when append_fn succeeds."""
    from wmtm.writeback import WritebackManager, WritebackCandidate
    from wmtm.item import WMTMItem
    from wmtm.attention import AttentionValue
    
    item = WMTMItem(id="w2", content="writeback test", attention=AttentionValue(sti=10.0))
    cand = WritebackCandidate(item=item, reason="high_utility", metta_content="(test content)")
    mgr = WritebackManager()
    
    def ok_fn(content):
        return {"ok": True}
    
    written = mgr.writeback([cand], ok_fn)
    assert len(written) == 1
    assert written[0] == "w2"
