"""Tests for wmtm/recall_bridge.py — LTM->WMTM pull mechanism."""
import pytest
from wmtm.recall_bridge import (
    LTMCluster,
    RecallCandidate,
    RecallBridge,
    parse_journal,
    _collect_superseded,
    _populate_cluster_fields,
)
from wmtm.store import WMTMStore
# -- LTMCluster tests ---------------------------------------------------
class TestLTMCluster:
    def test_default_fields(self):
        c = LTMCluster(id="ep-note-1")
        assert c.cluster_type == "Unknown"
        assert c.about_tags == []
        assert c.event_note == ""
        assert c.evidence_for == []
        assert c.promotes_from == []
        assert c.evidence_support_count == 0
        assert c.raw_lines == []
    def test_text_property_with_tags_and_note(self):
        c = LTMCluster(id="ep-1", about_tags=["alpha", "beta"], event_note="hello world")
        assert c.text == "alpha beta hello world"
    def test_text_property_empty(self):
        c = LTMCluster(id="ep-1")
        assert c.text.strip() == ""
# -- parse_journal tests ------------------------------------------------
SAMPLE_JOURNAL = [
    ';;; BEGIN MemoryCluster ep-note-001',
    '(ClusterType ep-note-001 episode)',
    '(About ep-note-001 topicA)',
    '(About ep-note-001 topicB)',
    '(EventNote ep-note-001 "The sky is blue")',
    '(EvidenceFor ep-note-001 ep-note-002)',
    '(EvidenceSupportCount ep-note-001 5)',
    ';;; END MemoryCluster ep-note-001',
    ';;; BEGIN MemoryCluster ep-note-002',
    '(ClusterType ep-note-002 episode)',
    '(About ep-note-002 topicC)',
    '(EventNote ep-note-002 "Water is wet")',
    '(Supersedes ep-note-002 ep-note-003)',
    ';;; END MemoryCluster ep-note-002',
    ';;; BEGIN MemoryCluster ep-note-003',
    '(ClusterType ep-note-003 episode)',
    '(About ep-note-003 topicD)',
    '(EventNote ep-note-003 "Old superseded note")',
    ';;; END MemoryCluster ep-note-003',
]
class TestParseJournal:
    def test_parses_all_non_superseded_clusters(self):
        clusters = parse_journal(SAMPLE_JOURNAL)
        ids = [c.id for c in clusters]
        assert "ep-note-001" in ids
        assert "ep-note-002" in ids
        # ep-note-003 is superseded by ep-note-002
        assert "ep-note-003" not in ids
    def test_extracts_about_tags(self):
        clusters = parse_journal(SAMPLE_JOURNAL)
        c = next(c for c in clusters if c.id == "ep-note-001")
        assert "topicA" in c.about_tags
        assert "topicB" in c.about_tags
    def test_extracts_event_note(self):
        clusters = parse_journal(SAMPLE_JOURNAL)
        c = next(c for c in clusters if c.id == "ep-note-001")
        assert c.event_note == "The sky is blue"
    def test_extracts_evidence_for(self):
        clusters = parse_journal(SAMPLE_JOURNAL)
        c = next(c for c in clusters if c.id == "ep-note-001")
        assert "ep-note-002" in c.evidence_for
    def test_extracts_cluster_type(self):
        clusters = parse_journal(SAMPLE_JOURNAL)
        c = next(c for c in clusters if c.id == "ep-note-001")
        assert c.cluster_type == "episode"
    def test_extracts_evidence_support_count(self):
        clusters = parse_journal(SAMPLE_JOURNAL)
        c = next(c for c in clusters if c.id == "ep-note-001")
        assert c.evidence_support_count == 5
    def test_empty_journal(self):
        assert parse_journal([]) == []
    def test_unclosed_cluster_is_dropped(self):
        lines = [
            ';;; BEGIN MemoryCluster ep-note-999',
            '(About ep-note-999 topicX)',
        ]
        clusters = parse_journal(lines)
        assert clusters == []
    def test_supersedes_with_placeholder_old_id_ignored(self):
        lines = [
            ';;; BEGIN MemoryCluster ep-note-A',
            '(Supersedes ep-note-A old-id)',
            ';;; END MemoryCluster ep-note-A',
        ]
        clusters = parse_journal(lines)
        assert len(clusters) == 1
# -- _collect_superseded tests ------------------------------------------
class TestCollectSuperseded:
    def test_finds_superseded(self):
        lines = [
            '(Supersedes ep-note-003 ep-note-001)',
            '(Supersedes ep-note-005 ep-note-002)',
        ]
        result = _collect_superseded(lines)
        assert "ep-note-001" in result
        assert "ep-note-002" in result
    def test_ignores_placeholder(self):
        lines = ['(Supersedes ep-note-x old-id)']
        result = _collect_superseded(lines)
        assert "old-id" not in result
    def test_empty(self):
        assert _collect_superseded([]) == set()
# -- RecallBridge tests -------------------------------------------------
def _make_clusters():
    return parse_journal(SAMPLE_JOURNAL)
class TestRecallBridge:
    def test_init_builds_by_id_and_evidence_map(self):
        bridge = RecallBridge(_make_clusters())
        assert "ep-note-001" in bridge._by_id
        assert "ep-note-002" in bridge._by_id
        # ep-note-001 has EvidenceFor ep-note-002
        assert "ep-note-002" in bridge._evidence_for_targets
    def test_recall_returns_candidates_matching_query(self):
        bridge = RecallBridge(_make_clusters())
        store = WMTMStore(capacity=10)
        candidates = bridge.recall("sky blue", store, top_k=10)
        ids = [c.cluster_id for c in candidates]
        assert "ep-note-001" in ids
    def test_recall_excludes_already_active_items(self):
        bridge = RecallBridge(_make_clusters())
        store = WMTMStore(capacity=10)
        store.admit(item_id="ep-note-001", content="The sky is blue")
        candidates = bridge.recall("sky blue", store, top_k=10)
        ids = [c.cluster_id for c in candidates]
        assert "ep-note-001" not in ids
    def test_recall_returns_empty_for_no_match(self):
        bridge = RecallBridge(_make_clusters())
        store = WMTMStore(capacity=10)
        candidates = bridge.recall("xyzzy nomatch", store, top_k=10)
        assert candidates == []
    def test_recall_respects_top_k(self):
        bridge = RecallBridge(_make_clusters())
        store = WMTMStore(capacity=10)
        candidates = bridge.recall("topic blue water wet", store, top_k=1)
        assert len(candidates) <= 1
    def test_recall_candidate_has_about_tags(self):
        bridge = RecallBridge(_make_clusters())
        store = WMTMStore(capacity=10)
        candidates = bridge.recall("sky blue", store, top_k=10)
        c = next(c for c in candidates if c.cluster_id == "ep-note-001")
        assert "topicA" in c.about_tags
    def test_recall_candidate_score_positive(self):
        bridge = RecallBridge(_make_clusters())
        store = WMTMStore(capacity=10)
        candidates = bridge.recall("sky blue", store, top_k=10)
        assert all(c.score > 0 for c in candidates)
# -- spreading_activation tests -----------------------------------------
class TestSpreadingActivation:
    def test_returns_scores_for_seed_ids(self):
        bridge = RecallBridge(_make_clusters())
        scores = bridge.spreading_activation(["ep-note-001"])
        assert "ep-note-001" in scores
        assert scores["ep-note-001"] == 1.0
    def test_propagates_to_evidence_for_targets(self):
        bridge = RecallBridge(_make_clusters())
        scores = bridge.spreading_activation(["ep-note-001"], depth=1, decay=0.5)
        # ep-note-001 has EvidenceFor ep-note-002
        assert "ep-note-002" in scores
        assert scores["ep-note-002"] == pytest.approx(0.5)
    def test_ignores_unknown_seed_ids(self):
        bridge = RecallBridge(_make_clusters())
        scores = bridge.spreading_activation(["nonexistent"])
        assert scores == {}
    def test_visited_prevents_cycles(self):
        bridge = RecallBridge(_make_clusters())
        scores = bridge.spreading_activation(["ep-note-001", "ep-note-002"], depth=3)
        # Each should only be visited once
        assert scores["ep-note-001"] == pytest.approx(1.0)
    def test_depth_zero_only_visits_seeds(self):
        bridge = RecallBridge(_make_clusters())
        scores = bridge.spreading_activation(["ep-note-001"], depth=0)
        assert "ep-note-001" in scores
        # ep-note-002 should not be reached at depth=0
        assert "ep-note-002" not in scores
# -- _tokenize tests ----------------------------------------------------
class TestTokenize:
    def test_basic_tokenization(self):
        tokens = RecallBridge._tokenize("hello world")
        assert "hello" in tokens
        assert "world" in tokens
    def test_strips_punctuation(self):
        tokens = RecallBridge._tokenize("hello, world!")
        assert "hello" in tokens
        assert "world" in tokens
    def test_filters_short_tokens(self):
        tokens = RecallBridge._tokenize("a hi ok")
        assert "a" not in tokens
        assert "hi" in tokens
        assert "ok" in tokens
    def test_lowercase(self):
        tokens = RecallBridge._tokenize("HELLO World")
        assert "hello" in tokens
        assert "world" in tokens
    def test_empty_string(self):
        assert RecallBridge._tokenize("") == set()
