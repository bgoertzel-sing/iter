"""Tests for GoalChainer Adapter (gc_adapter.py).

Tests the direct WMTM ↔ GoalChainer integration layer:
- WMTMEvidenceReasoner: projects evidence from WMTM attention
- wmtm_goal_to_gc_goal: WMTM Goal → GoalChainer Goal conversion
- wmtm_items_to_actions: WMTM items → CandidateActions
- run_goalchainer_direct: full adapter pipeline
- AdapterResult → GovernanceBridge integration
"""
import json
import os
import sys
import tempfile
import pytest

# Ensure GoalChainer is importable
GC_SRC = '/home/openclaw/research-agent/projects/omegaclaw/repos/OmegaClaw-GoalChainer/src'
if os.path.isdir(GC_SRC) and GC_SRC not in sys.path:
    sys.path.insert(0, GC_SRC)

from wmtm.store import WMTMStore
from wmtm.goal import Goal as WMTMGoal
from wmtm.goal_store import GoalStore
from wmtm.gc_adapter import (
    WMTMEvidenceReasoner,
    wmtm_goal_to_gc_goal,
    wmtm_items_to_actions,
    run_goalchainer_direct,
    AdapterResult,
    _link_actions_to_goals,
)

# Check if GoalChainer is available for integration tests
try:
    from goal_chainer.models import (
        CandidateAction, Decision, EvidenceProjection, Goal as GCGoal,
        GoalScenario, Norm, NormMode,
    )
    from goal_chainer.scoring import DecisionEngine
    GC_AVAILABLE = True
except ImportError:
    GC_AVAILABLE = False

needs_gc = pytest.mark.skipif(not GC_AVAILABLE, reason="GoalChainer not available")


def _make_store_with_items(items):
    """Create a WMTMStore populated with items."""
    store = WMTMStore(capacity=50)
    for item_id, content, sti in items:
        store.admit(item_id=item_id, content=content, source_type='test', initial_sti=sti)
    return store


def _make_goal_store(goals):
    """Create a GoalStore with given goals."""
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        json.dump({"goals": [], "count": 0}, f)
        path = f.name
    gs = GoalStore(path=path)
    for g in goals:
        gs.add(g)
    return gs


# ── WMTMEvidenceReasoner Tests ─────────────────────────────────────


@needs_gc
class TestWMTMEvidenceReasoner:

    def test_project_returns_evidence_projection(self):
        store = _make_store_with_items([
            ("item1", "chemical reaction bonding molecules", 80.0),
            ("item2", "enzyme catalysis protein folding", 60.0),
        ])
        reasoner = WMTMEvidenceReasoner(store, max_sti=100.0)

        action = CandidateAction(
            id="act-1", label="chemical bonding analysis",
            description="Analyze chemical bonding patterns in molecules",
            satisfies=(), evidence_query="chemical bonding",
            evidence_atoms=("chemical", "bonding", "molecules"),
            default_strength=0.5, default_confidence=0.8,
        )
        ev = reasoner.project(action)
        assert isinstance(ev, EvidenceProjection)
        assert 0.0 <= ev.strength <= 1.0
        assert 0.0 <= ev.confidence <= 1.0
        assert ev.source == "wmtm-attention"

    def test_strength_reflects_sti(self):
        store = _make_store_with_items([
            ("high", "quantum computing entanglement", 90.0),
            ("low", "quantum computing decoherence", 10.0),
        ])
        reasoner = WMTMEvidenceReasoner(store, max_sti=100.0)

        action = CandidateAction(
            id="q1", label="quantum computing research",
            description="quantum computing entanglement analysis",
            satisfies=(), evidence_query="quantum",
            evidence_atoms=("quantum", "computing"),
            default_strength=0.5, default_confidence=0.8,
        )
        ev = reasoner.project(action)
        # Matching items have STI 90 and 10, avg=50 → strength=0.5
        assert ev.strength > 0.3  # should reflect real STI

    def test_confidence_from_atom_coverage(self):
        store = _make_store_with_items([
            ("a", "protein folding simulation", 50.0),
        ])
        reasoner = WMTMEvidenceReasoner(store)

        # All atoms present
        action_full = CandidateAction(
            id="f1", label="protein analysis",
            description="protein folding",
            satisfies=(), evidence_query="protein",
            evidence_atoms=("protein", "folding"),
            default_strength=0.5, default_confidence=0.8,
        )
        ev_full = reasoner.project(action_full)

        # No atoms present
        action_none = CandidateAction(
            id="f2", label="galaxy formation",
            description="galaxy formation cosmic evolution",
            satisfies=(), evidence_query="galaxy",
            evidence_atoms=("galaxy", "cosmic", "evolution"),
            default_strength=0.5, default_confidence=0.8,
        )
        ev_none = reasoner.project(action_none)

        assert ev_full.confidence >= ev_none.confidence

    def test_deontic_from_norms(self):
        store = _make_store_with_items([("x", "test content here", 50.0)])
        norm = Norm(id="n1", mode="forbid", target_action="act-x", reason="banned")
        reasoner = WMTMEvidenceReasoner(store, norms=[norm])

        action = CandidateAction(
            id="act-x", label="test action",
            description="some test content here",
            satisfies=(), evidence_query="test",
            evidence_atoms=("test",),
            default_strength=0.5, default_confidence=0.8,
        )
        ev = reasoner.project(action)
        assert ev.deontic == "forbidden"

    def test_no_matching_items_uses_defaults(self):
        store = _make_store_with_items([("a", "unrelated topic here", 50.0)])
        reasoner = WMTMEvidenceReasoner(store)

        action = CandidateAction(
            id="q1", label="xyz",
            description="completely different subject matter",
            satisfies=(), evidence_query="xyz",
            evidence_atoms=("aaaa", "bbbb", "cccc"),
            default_strength=0.7, default_confidence=0.9,
        )
        ev = reasoner.project(action)
        assert ev.strength == 0.7  # falls back to default

    def test_empty_store(self):
        store = WMTMStore(capacity=50)
        reasoner = WMTMEvidenceReasoner(store)

        action = CandidateAction(
            id="a1", label="test", description="test action",
            satisfies=(), evidence_query="test",
            evidence_atoms=("test",),
            default_strength=0.5, default_confidence=0.8,
        )
        ev = reasoner.project(action)
        assert ev.strength == 0.5  # default


# ── Goal Conversion Tests ──────────────────────────────────────────


@needs_gc
class TestGoalConversion:

    def test_wmtm_goal_to_gc_goal_basic(self):
        wg = WMTMGoal(id="g1", description="Optimize reaction yields", priority=0.5)
        gc = wmtm_goal_to_gc_goal(wg)
        assert gc is not None
        assert gc.id == "g1"
        assert gc.owner == "wmtm"
        assert gc.statement == "Optimize reaction yields"
        assert gc.weight == 0.5
        assert gc.kind == "individual"
        assert gc.required is False

    def test_high_priority_is_required(self):
        wg = WMTMGoal(id="g2", description="Critical safety goal", priority=0.9)
        gc = wmtm_goal_to_gc_goal(wg)
        assert gc.required is True

    def test_collective_kind(self):
        wg = WMTMGoal(id="g3", description="Group objective", priority=0.3)
        gc = wmtm_goal_to_gc_goal(wg, kind="collective")
        assert gc.kind == "collective"


# ── Items to Actions Tests ─────────────────────────────────────────


@needs_gc
class TestItemsToActions:

    def test_converts_items_to_actions(self):
        store = _make_store_with_items([
            ("i1", "molecular dynamics simulation results", 80.0),
            ("i2", "protein structure prediction model", 60.0),
        ])
        actions = wmtm_items_to_actions(store, max_items=5)
        assert len(actions) == 2
        assert all(isinstance(a, CandidateAction) for a in actions)
        assert actions[0].id == "wmtm-i1"
        assert "molecular" in actions[0].label.lower()

    def test_strength_normalized(self):
        store = _make_store_with_items([
            ("top", "highest attention item content", 100.0),
            ("low", "lowest attention item content", 10.0),
        ])
        actions = wmtm_items_to_actions(store)
        strengths = {a.id: a.default_strength for a in actions}
        assert strengths["wmtm-top"] > strengths["wmtm-low"]

    def test_max_items_limit(self):
        store = _make_store_with_items([
            (f"i{n}", f"item number {n} content here", float(100 - n))
            for n in range(20)
        ])
        actions = wmtm_items_to_actions(store, max_items=5)
        assert len(actions) == 5

    def test_empty_store_returns_empty(self):
        store = WMTMStore(capacity=50)
        actions = wmtm_items_to_actions(store)
        assert actions == []


# ── Link Actions to Goals Tests ────────────────────────────────────


@needs_gc
class TestLinkActionsToGoals:

    def test_links_by_keyword_overlap(self):
        store = WMTMStore(capacity=50)  # not used by _link_actions_to_goals
        goals = [
            GCGoal(id="g1", owner="test", statement="chemical reaction optimization",
                   weight=5.0, kind="individual"),
            GCGoal(id="g2", owner="test", statement="protein structure prediction",
                   weight=3.0, kind="individual"),
        ]
        actions = [
            CandidateAction(
                id="a1", label="chemical analysis",
                description="analyze chemical reaction pathways",
                satisfies=(), evidence_query="chemical",
                evidence_atoms=("chemical",),
                default_strength=0.5, default_confidence=0.8,
            ),
            CandidateAction(
                id="a2", label="protein modeling",
                description="model protein structure folding",
                satisfies=(), evidence_query="protein",
                evidence_atoms=("protein",),
                default_strength=0.5, default_confidence=0.8,
            ),
        ]
        linked = _link_actions_to_goals(actions, goals, store)
        assert len(linked) == 2
        # a1 should satisfy g1 (chemical overlap)
        a1 = next(a for a in linked if a.id == "a1")
        assert "g1" in a1.satisfies
        # a2 should satisfy g2 (protein overlap)
        a2 = next(a for a in linked if a.id == "a2")
        assert "g2" in a2.satisfies

    def test_no_overlap_no_link(self):
        store = WMTMStore(capacity=50)
        goals = [GCGoal(id="g1", owner="t", statement="xyz xyz xyz",
                        weight=1.0, kind="individual")]
        actions = [CandidateAction(
            id="a1", label="abc", description="completely different topic",
            satisfies=(), evidence_query="abc",
            evidence_atoms=("abc",),
            default_strength=0.5, default_confidence=0.8,
        )]
        linked = _link_actions_to_goals(actions, goals, store)
        assert linked[0].satisfies == ()


# ── Full Adapter Pipeline Tests ────────────────────────────────────


@needs_gc
class TestRunGoalchainerDirect:

    def test_basic_run_returns_decisions(self):
        store = _make_store_with_items([
            ("i1", "chemical reaction bonding analysis", 80.0),
            ("i2", "enzyme catalysis protein folding", 60.0),
        ])
        result = run_goalchainer_direct(store)
        assert result is not None
        assert isinstance(result, AdapterResult)
        assert len(result.decisions) > 0
        assert all(isinstance(d, Decision) for d in result.decisions)

    def test_with_goals_and_norms(self):
        store = _make_store_with_items([
            ("i1", "chemical reaction optimization study", 80.0),
        ])
        gs = _make_goal_store([
            WMTMGoal(id="g1", description="optimize chemical reaction yields", priority=0.7),
        ])
        norms = [{"id": "n1", "mode": "permit", "target_action": "wmtm-i1", "reason": "ok"}]
        result = run_goalchainer_direct(store, goal_store=gs, norms=norms)
        assert result is not None
        assert len(result.goal_ids) == 1

    def test_forbidden_norm_blocks(self):
        store = _make_store_with_items([
            ("i1", "dangerous experiment hazardous materials", 90.0),
        ])
        norms = [{
            "id": "n1", "mode": "forbid",
            "target_action": "wmtm-i1",
            "reason": "safety violation",
        }]
        result = run_goalchainer_direct(store, norms=norms)
        assert result is not None
        # The forbidden action should be scored negatively
        forbidden = [d for d in result.decisions if d.action_id == "wmtm-i1"]
        if forbidden:
            assert forbidden[0].score < 0 or forbidden[0].status == "blocked"

    def test_empty_store_returns_empty_result(self):
        store = WMTMStore(capacity=50)
        result = run_goalchainer_direct(store)
        assert result is not None
        assert len(result.decisions) == 0
        assert result.action_count == 0

    def test_decisions_sorted_by_score(self):
        store = _make_store_with_items([
            ("i1", "high priority chemical bonding", 90.0),
            ("i2", "medium priority enzyme work", 50.0),
            ("i3", "lower priority background task", 20.0),
        ])
        result = run_goalchainer_direct(store)
        assert result is not None
        scores = [d.score for d in result.decisions]
        assert scores == sorted(scores, reverse=True), "Decisions should be sorted by score descending"


# ── AdapterResult Integration Tests ────────────────────────────────


@needs_gc
class TestAdapterResultIntegration:

    def test_to_gc_result_dict_format(self):
        store = _make_store_with_items([
            ("i1", "chemical reaction analysis study", 80.0),
        ])
        result = run_goalchainer_direct(store)
        assert result is not None
        assert len(result.decisions) > 0

        gc_dict = result.to_gc_result_dict()
        assert "decisions" in gc_dict
        assert "decided" in gc_dict
        assert "status" in gc_dict
        assert "executed" in gc_dict
        assert isinstance(gc_dict["decisions"], list)
        assert len(gc_dict["decisions"]) > 0

    def test_gc_result_dict_feeds_gov_bridge(self):
        """End-to-end: adapter result → GovernanceBridge.process_goalchainer_result"""
        from wmtm.gov_bridge import GovernanceBridge, DecisionHistory

        store = _make_store_with_items([
            ("i1", "chemical reaction analysis study", 80.0),
            ("i2", "enzyme catalysis protein study", 60.0),
        ])
        gs = _make_goal_store([
            WMTMGoal(id="g1", description="analyze chemical reactions", priority=0.5),
        ])

        # Run adapter
        adapter_result = run_goalchainer_direct(store, goal_store=gs)
        assert adapter_result is not None
        gc_dict = adapter_result.to_gc_result_dict()

        # Feed into GovernanceBridge
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            json.dump({"records": []}, f)
            hist_path = f.name
        gov = GovernanceBridge(
            goal_store=gs,
            decision_history=DecisionHistory(path=hist_path, auto_save=False),
        )
        gov_result = gov.process_goalchainer_result(gc_dict, store, cycle=1, goal_id="g1")

        # Should have created records and applied enforcement
        assert gov_result["records"] > 0
        assert "enforcement" in gov_result
        assert "lifecycle_changes" in gov_result
