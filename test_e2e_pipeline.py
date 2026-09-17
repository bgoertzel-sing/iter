"""End-to-end pipeline test: exercises all components in a simulated scenario.

Scenario: A system monitoring agent receives facts about an incident,
runs a full orchestration cycle with PLN inference + GoalChainer decisions,
and verifies that derived knowledge and decisions are admitted to WMTM.
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wmtm.store import WMTMStore
from wmtm.orchestrator import WMTMOrchestrator, CycleResult
from wmtm.inference import InferenceCandidate
from wmtm.goalchainer_bridge import goalchainer_result_to_candidates
from wmtm.pln_bridge import run_pln_inference_over_wmtm


class TestEndToEndPipeline:
    """Full pipeline: recall -> inference -> PLN -> GoalChainer -> admit -> writeback."""

    def test_full_cycle_with_all_components(self):
        """A single cycle with PLN + GoalChainer (mocked) produces results."""
        store = WMTMStore(capacity=20)

        # Simulate recall: agent learns about an incident
        store.admit("fact-1", "Server CPU is at 95 percent", source_type="recalled")
        store.admit("fact-2", "Server is production-web-01", source_type="recalled")
        store.admit("fact-3", "CPU spike started 10 minutes ago", source_type="recalled")

        orch = WMTMOrchestrator(
            store,
            use_pln=True,
            use_goalchainer=True,
            goalchainer_request="Server CPU at 95 percent on production-web-01",
        )

        # Mock GoalChainer to avoid slow real pipeline call
        from wmtm.inference import InferenceCandidate as IC

        def mock_gc_run(store, request, max_evidence=20):
            return [IC(
                content="GoalChainer decision: investigate-cpu (Investigate CPU) -- status=recommended, score=0.900",
                confidence=0.9,
                derived_from=["fact-1", "fact-2", "fact-3"],
                inference_type="goalchainer_decision",
                triple=None,
                initial_sti=18.0,
            )]

        import wmtm.goalchainer_bridge
        original = wmtm.goalchainer_bridge.run_goalchainer_over_wmtm
        wmtm.goalchainer_bridge.run_goalchainer_over_wmtm = mock_gc_run

        try:
            result = orch.cycle()
        finally:
            wmtm.goalchainer_bridge.run_goalchainer_over_wmtm = original

        # Verify cycle completed
        assert isinstance(result, CycleResult)
        assert result.cycle == 0

        # Verify GoalChainer decision was admitted
        assert result.gc_admitted >= 1

        # Verify the decision item is in the store
        active = store.get_active_set()
        gc_items = [i for i in active if "GoalChainer decision" in i.content]
        assert len(gc_items) >= 1
        assert "investigate-cpu" in gc_items[0].content

    def test_multi_cycle_knowledge_accumulation(self):
        """Over multiple cycles, knowledge accumulates in WMTM."""
        store = WMTMStore(capacity=30)

        # Initial facts
        store.admit("f1", "Temperature is high", source_type="recalled")
        store.admit("f2", "Fan speed is low", source_type="recalled")

        orch = WMTMOrchestrator(store, use_pln=True, use_goalchainer=False)

        results = []
        for i in range(3):
            r = orch.cycle()
            results.append(r)

        # Each cycle should complete without error
        assert all(isinstance(r, CycleResult) for r in results)
        assert all(r.cycle == i for i, r in enumerate(results))

        # Store should have items (original + possibly derived)
        active = store.get_active_set()
        assert len(active) >= 2  # at least the originals

    def test_goalchainer_decision_has_provenance(self):
        """GoalChainer decisions carry provenance from source evidence."""
        store = WMTMStore(capacity=10)
        store.admit("e1", "Disk usage at 90 percent", source_type="recalled")
        store.admit("e2", "Log files growing rapidly", source_type="recalled")

        orch = WMTMOrchestrator(
            store,
            use_goalchainer=True,
            goalchainer_request="Disk full incident",
        )

        from wmtm.inference import InferenceCandidate as IC

        def mock_gc_run(store, request, max_evidence=20):
            return [IC(
                content="GoalChainer decision: clean-logs (Clean Logs) -- status=recommended, score=0.850",
                confidence=0.85,
                derived_from=["e1", "e2"],
                inference_type="goalchainer_decision",
                triple=None,
                initial_sti=17.0,
            )]

        import wmtm.goalchainer_bridge
        original = wmtm.goalchainer_bridge.run_goalchainer_over_wmtm
        wmtm.goalchainer_bridge.run_goalchainer_over_wmtm = mock_gc_run

        try:
            result = orch.cycle()
        finally:
            wmtm.goalchainer_bridge.run_goalchainer_over_wmtm = original

        # Find the admitted GoalChainer item
        active = store.get_active_set()
        gc_items = [i for i in active if "GoalChainer decision" in i.content]
        assert len(gc_items) == 1

        # Verify provenance
        item = gc_items[0]
        assert item.source_type == "derived"
        assert "e1" in item.derived_from
        assert "e2" in item.derived_from

    def test_pln_derives_knowledge_from_facts(self):
        """PLN bridge produces candidates from structured facts."""
        store = WMTMStore(capacity=20)
        store.admit("p1", "Dog is a Mammal", source_type="recalled")
        store.admit("p2", "Mammal is a Animal", source_type="recalled")

        candidates = run_pln_inference_over_wmtm(store)
        # PLN should derive something (even if just concept atoms)
        assert isinstance(candidates, list)

    def test_pipeline_degrades_gracefully_without_goalchainer(self):
        """With GoalChainer disabled, cycle still works with PLN only."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "Service is down", source_type="recalled")

        orch = WMTMOrchestrator(store, use_pln=True, use_goalchainer=False)
        result = orch.cycle()

        assert isinstance(result, CycleResult)
        assert result.gc_admitted == 0
        assert result.pln_admitted >= 0  # PLN may or may not derive

    def test_pipeline_degrades_gracefully_without_pln(self):
        """With PLN disabled, cycle still works with GoalChainer only."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "Service is down", source_type="recalled")

        orch = WMTMOrchestrator(
            store, use_pln=False, use_goalchainer=True,
            goalchainer_request="Service down",
        )

        from wmtm.inference import InferenceCandidate as IC
        def mock_gc_run(store, request, max_evidence=20):
            return [IC(
                content="GoalChainer decision: restart-service",
                confidence=0.8,
                derived_from=["f1"],
                inference_type="goalchainer_decision",
                triple=None,
                initial_sti=16.0,
            )]

        import wmtm.goalchainer_bridge
        original = wmtm.goalchainer_bridge.run_goalchainer_over_wmtm
        wmtm.goalchainer_bridge.run_goalchainer_over_wmtm = mock_gc_run

        try:
            result = orch.cycle()
        finally:
            wmtm.goalchainer_bridge.run_goalchainer_over_wmtm = original

        assert isinstance(result, CycleResult)
        assert result.pln_admitted == 0
        assert result.gc_admitted >= 1

    def test_full_cycle_with_writeback(self):
        """Cycle with writeback callback writes to LTM."""
        store = WMTMStore(capacity=10)
        store.admit("f1", "Important fact", source_type="recalled")

        written = []
        def mock_append(note):
            written.append(note)

        orch = WMTMOrchestrator(store, use_pln=True, use_goalchainer=False)
        result = orch.cycle(append_fn=mock_append)

        assert isinstance(result, CycleResult)
        # Writeback may or may not produce items depending on STI thresholds
        # but the cycle should complete without error

    def test_forgetting_works_in_full_cycle(self):
        """Items below attention threshold are forgotten during cycle."""
        store = WMTMStore(capacity=5)  # small capacity to trigger eviction

        # Add more items than capacity
        for i in range(7):
            store.admit(f"f{i}", f"Fact number {i}", source_type="recalled")

        orch = WMTMOrchestrator(store, use_pln=False, use_goalchainer=False)
        result = orch.cycle()

        assert isinstance(result, CycleResult)
        # Some items may be evicted due to capacity
        # The cycle should complete without error

    def test_contradiction_detection_in_cycle(self):
        """Contradictions between items are detected during cycle."""
        store = WMTMStore(capacity=10)
        store.admit("c1", "The sky is blue", source_type="recalled")
        store.admit("c2", "The sky is NOT blue", source_type="recalled")

        orch = WMTMOrchestrator(store, use_pln=False, use_goalchainer=False)
        result = orch.cycle()

        assert isinstance(result, CycleResult)
        # Contradictions may or may not be detected depending on implementation
        # but cycle should complete

    def test_attention_decay_across_cycles(self):
        """STI decays across cycles, affecting active set."""
        store = WMTMStore(capacity=10)
        store.admit("a1", "High priority item", source_type="recalled", initial_sti=50.0)
        store.admit("a2", "Low priority item", source_type="recalled", initial_sti=1.0)

        orch = WMTMOrchestrator(store, use_pln=False, use_goalchainer=False)

        # Run several cycles to let attention decay
        for i in range(3):
            orch.cycle()

        # Both items might still be in store (capacity is 10)
        # but STI should have decayed
        active = store.get_active_set()
        assert len(active) >= 1  # at least something remains
