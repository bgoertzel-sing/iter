"""Tests for WMTM Phase 2: RecallBridge (LTM -> WMTM)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from wmtm.store import WMTMStore
from wmtm.recall_bridge import RecallBridge, parse_journal, LTMCluster, RecallCandidate


class TestParseJournal:
    def test_parse_simple_cluster(self):
        lines = [
            ';;; BEGIN MemoryCluster test-1',
            '(MemoryCluster test-1)',
            '(ClusterType test-1 Episode)',
            '(About test-1 petta)',
            '(About test-1 validation)',
            '(EventNote ev-1 "Testing the parser works correctly")',
            '(EvidenceFor ev-1 other-cluster)',
            '(EvidenceSupportCount test-1 5)',
            ';;; END MemoryCluster test-1',
        ]
        clusters = parse_journal(lines)
        assert len(clusters) == 1
        c = clusters[0]
        assert c.id == 'test-1'
        assert c.cluster_type == 'Episode'
        assert 'petta' in c.about_tags
        assert 'validation' in c.about_tags
        assert 'Testing the parser works correctly' in c.event_note
        assert 'other-cluster' in c.evidence_for
        assert c.evidence_support_count == 5

    def test_parse_multiple_clusters(self):
        lines = []
        for i in range(3):
            lines.extend([
                f';;; BEGIN MemoryCluster c-{i}',
                f'(MemoryCluster c-{i})',
                f'(About c-{i} topic-{i})',
                f'(EventNote ev-{i} "Note number {i}")',
                f';;; END MemoryCluster c-{i}',
            ])
        clusters = parse_journal(lines)
        assert len(clusters) == 3
        assert clusters[0].id == 'c-0'
        assert clusters[2].id == 'c-2'

    def test_skip_superseded_clusters(self):
        lines = [
            ';;; BEGIN MemoryCluster old-one',
            '(MemoryCluster old-one)',
            '(EventNote ev-old "Old data")',
            ';;; END MemoryCluster old-one',
            ';;; BEGIN MemoryCluster new-one',
            '(MemoryCluster new-one)',
            '(Supersedes new-one old-one)',
            '(EventNote ev-new "New data")',
            ';;; END MemoryCluster new-one',
        ]
        clusters = parse_journal(lines)
        ids = [c.id for c in clusters]
        assert 'new-one' in ids
        assert 'old-one' not in ids

    def test_empty_journal(self):
        assert parse_journal([]) == []

    def test_cluster_without_about(self):
        lines = [
            ';;; BEGIN MemoryCluster bare',
            '(MemoryCluster bare)',
            '(EventNote ev-b "Just a note")',
            ';;; END MemoryCluster bare',
        ]
        clusters = parse_journal(lines)
        assert len(clusters) == 1
        assert clusters[0].about_tags == []
        assert clusters[0].event_note == 'Just a note'


class TestRecallBridge:
    def _make_test_clusters(self):
        return [
            LTMCluster(
                id='petta-facts',
                cluster_type='Fact',
                about_tags=['petta', 'memory', 'api'],
                event_note='append_cluster validates each append atomically',
                evidence_for=['related-fact'],
                evidence_support_count=30,
            ),
            LTMCluster(
                id='iter-runtime',
                cluster_type='Fact',
                about_tags=['iter', 'runtime', 'loop'],
                event_note='memory txt cap is 3000 chars per turn',
                evidence_for=[],
                evidence_support_count=58,
            ),
            LTMCluster(
                id='related-fact',
                cluster_type='Fact',
                about_tags=['petta', 'validation'],
                event_note='query_about is exact whole-token match',
                evidence_for=['petta-facts'],
                evidence_support_count=10,
            ),
            LTMCluster(
                id='unrelated',
                cluster_type='Episode',
                about_tags=['cooking', 'recipes'],
                event_note='How to make pasta from scratch',
                evidence_for=[],
                evidence_support_count=2,
            ),
        ]

    def test_keyword_match_returns_relevant(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        results = bridge.recall('petta memory api', store, top_k=10)
        ids = [r.cluster_id for r in results]
        assert 'petta-facts' in ids
        assert 'related-fact' in ids
        assert 'unrelated' not in ids

    def test_top_k_limits_results(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        results = bridge.recall('petta', store, top_k=1)
        assert len(results) <= 1

    def test_higher_evidence_support_boosts_score(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        results = bridge.recall('petta memory', store, top_k=10)
        # petta-facts has evidence_support_count=30, related-fact has 10
        # Both match 'petta', but petta-facts should rank higher
        petta_score = next(r.score for r in results if r.cluster_id == 'petta-facts')
        related_score = next(r.score for r in results if r.cluster_id == 'related-fact')
        assert petta_score > related_score

    def test_spreading_activation_finds_connected(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        # Admit petta-facts into WMTM first
        store.admit('petta-facts', 'append_cluster validates each append atomically',
                     initial_sti=10.0)
        # Now recall with a query that matches related-fact
        results = bridge.recall('validation query', store, top_k=10)
        ids = [r.cluster_id for r in results]
        # related-fact should get a boost from EvidenceFor edge to petta-facts
        assert 'related-fact' in ids

    def test_already_active_not_returned(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        store.admit('petta-facts', 'active item', initial_sti=10.0)
        results = bridge.recall('petta memory api', store, top_k=10)
        ids = [r.cluster_id for r in results]
        assert 'petta-facts' not in ids  # already in WMTM

    def test_no_match_returns_empty(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        results = bridge.recall('quantum physics astrophysics', store, top_k=10)
        assert len(results) == 0

    def test_candidates_have_content_and_score(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        results = bridge.recall('petta', store, top_k=10)
        assert all(r.content for r in results)
        assert all(r.score > 0 for r in results)

    def test_spreading_activation_returns_scores(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        scores = bridge.spreading_activation(['petta-facts'], depth=2, decay=0.5)
        assert 'petta-facts' in scores
        # related-fact is linked via EvidenceFor
        assert 'related-fact' in scores
        assert scores['petta-facts'] > scores['related-fact']

    def test_spreading_activation_respects_depth(self):
        clusters = self._make_test_clusters()
        bridge = RecallBridge(clusters)
        # depth=0 means no traversal, only seeds
        scores = bridge.spreading_activation(['petta-facts'], depth=0)
        assert 'petta-facts' in scores
        assert 'related-fact' not in scores  # 1 hop away, not reached at depth 0

    def test_recall_against_real_journal(self):
        """Integration test: parse the actual petta-memory journal."""
        journal_path = Path(__file__).parent / 'memory' / '_journal' / 'journal.metta'
        if not journal_path.exists():
            return  # skip if no journal
        lines = journal_path.read_text(encoding='utf-8').splitlines()
        clusters = parse_journal(lines)
        assert len(clusters) > 0
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=100)
        results = bridge.recall('petta memory validation', store, top_k=5)
        assert len(results) > 0
        assert all(r.score > 0 for r in results)
