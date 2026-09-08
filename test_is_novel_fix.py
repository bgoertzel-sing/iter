"""Test that is_novel uses exact matching, not substring matching.

Regression test for bug where abduction/evidence candidates were filtered
out because their content contained parent item content as explanation.
"""
import pytest
from wmtm import WMTMStore, WMTMOrchestrator, WMTMInferenceEngine, InferenceCandidate


class TestIsNovelFix:
    def test_abduction_candidate_is_novel(self):
        """Abduction candidate contains parent content in explanation but is novel."""
        store = WMTMStore(capacity=50)
        store.admit(item_id='r1', content='rain implies wet',
                    source_type='recalled', initial_sti=80)
        store.admit(item_id='r2', content='wet is observed',
                    source_type='recalled', initial_sti=90)

        engine = WMTMInferenceEngine()
        cand = InferenceCandidate(
            content='maybe rain (abduced from wet being true and rain implies wet)',
            confidence=0.3,
            derived_from=['r1', 'r2'],
            inference_type='abduction',
        )
        assert engine.is_novel(cand, store) is True

    def test_evidence_aggregation_candidate_is_novel(self):
        """Evidence aggregation candidate contains parent content but is novel."""
        store = WMTMStore(capacity=50)
        store.admit(item_id='r1', content='alpha implies beta',
                    source_type='recalled', initial_sti=60)
        store.admit(item_id='r2', content='alpha implies beta',
                    source_type='recalled', initial_sti=60)

        engine = WMTMInferenceEngine()
        cand = InferenceCandidate(
            content='alpha implies beta (aggregated from 2 sources)',
            confidence=0.91,
            derived_from=['r1', 'r2'],
            inference_type='evidence_aggregation',
        )
        assert engine.is_novel(cand, store) is True

    def test_exact_duplicate_is_not_novel(self):
        """Exact content match should still be filtered."""
        store = WMTMStore(capacity=50)
        store.admit(item_id='r1', content='alpha implies beta',
                    source_type='recalled', initial_sti=60)

        engine = WMTMInferenceEngine()
        cand = InferenceCandidate(
            content='alpha implies beta',
            confidence=0.8,
            derived_from=['r1'],
            inference_type='deduction',
        )
        assert engine.is_novel(cand, store) is False

    def test_case_insensitive_exact_match(self):
        """Exact match should be case-insensitive."""
        store = WMTMStore(capacity=50)
        store.admit(item_id='r1', content='Alpha Implies Beta',
                    source_type='recalled', initial_sti=60)

        engine = WMTMInferenceEngine()
        cand = InferenceCandidate(
            content='alpha implies beta',
            confidence=0.8,
            derived_from=['r1'],
            inference_type='deduction',
        )
        assert engine.is_novel(cand, store) is False

    def test_abduction_via_orchestrator(self):
        """Abduction should produce derived items via orchestrator cycle."""
        store = WMTMStore(capacity=50)
        orch = WMTMOrchestrator(store)
        r = orch.cycle(recall_fn=lambda: [
            ('r1', 'rain implies wet', 80.0),
            ('r2', 'wet is observed', 90.0),
        ])
        assert r.admitted_derived >= 1
        derived = [i for i in store.get_active_set() if i.source_type == 'derived']
        assert any('maybe rain' in i.content for i in derived)

    def test_evidence_aggregation_via_orchestrator(self):
        """Evidence aggregation should produce derived items via orchestrator."""
        store = WMTMStore(capacity=50)
        orch = WMTMOrchestrator(store)
        r = orch.cycle(recall_fn=lambda: [
            ('r1', 'alpha implies beta', 60.0),
            ('r2', 'alpha implies beta', 60.0),
        ])
        assert r.admitted_derived >= 1
        derived = [i for i in store.get_active_set() if i.source_type == 'derived']
        assert any('aggregated' in i.content for i in derived)
