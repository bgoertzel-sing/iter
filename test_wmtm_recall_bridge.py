"""Tests for wmtm/recall_bridge.py — LTM parsing, recall, and spreading activation."""
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


# ---- LTMCluster ----

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
        assert "alpha" in c.text
        assert "beta" in c.text
        assert "hello world" in c.text

    def test_text_property_empty(self):
        c = LTMCluster(id="ep-1")
        assert c.text.strip() == ""


# ---- parse_journal ----

class TestParseJournal:
    JOURNAL_LINES = [
        ";;; BEGIN MemoryCluster ep-note-001",
        "(ClusterType ep-note-001 episode)",
        "(About ep-note-001 topicA)",
        '(EventNote ep-note-001 "cats chase mice")',
        "(EvidenceFor ep-note-001 ep-note-002)",
        "(EvidenceSupportCount ep-note-001 5)",
        ";;; END MemoryCluster ep-note-001",
        "",
        ";;; BEGIN MemoryCluster ep-note-002",
        "(ClusterType ep-note-002 episode)",
        "(About ep-note-002 topicB)",
        '(EventNote ep-note-002 "dogs bark loudly")',
        ";;; END MemoryCluster ep-note-002",
        "",
        ";;; BEGIN MemoryCluster ep-note-003",
        "(ClusterType ep-note-003 episode)",
        "(Supersedes ep-note-003 ep-note-002)",
        ";;; END MemoryCluster ep-note-003",
    ]

    def test_parses_all_clusters(self):
        clusters = parse_journal(self.JOURNAL_LINES)
        # ep-note-002 is superseded by ep-note-003, so only 2 remain
        assert len(clusters) == 2
        ids = {c.id for c in clusters}
        assert "ep-note-001" in ids
        assert "ep-note-003" in ids
        assert "ep-note-002" not in ids

    def test_populates_fields(self):
        clusters = parse_journal(self.JOURNAL_LINES)
        c = next(c for c in clusters if c.id == "ep-note-001")
        assert c.cluster_type == "episode"
        assert "topicA" in c.about_tags
        assert c.event_note == "cats chase mice"
        assert "ep-note-002" in c.evidence_for
        assert c.evidence_support_count == 5

    def test_empty_journal(self):
        assert parse_journal([]) == []

    def test_unclosed_cluster_is_dropped(self):
        lines = [
            ";;; BEGIN MemoryCluster ep-note-010",
            '(EventNote ep-note-010 "incomplete")',
        ]
        assert parse_journal(lines) == []


# ---- _collect_superseded ----

class TestCollectSuperseded:
    def test_finds_superseded_ids(self):
        lines = [
            "(Supersedes ep-note-003 ep-note-002)",
            "(Supersedes ep-note-005 ep-note-004)",
        ]
        result = _collect_superseded(lines)
        assert "ep-note-002" in result
        assert "ep-note-004" in result

    def test_ignores_placeholder(self):
        lines = ["(Supersedes ep-note-003 old-id)"]
        result = _collect_superseded(lines)
        assert "old-id" not in result

    def test_empty(self):
        assert _collect_superseded([]) == set()


# ---- RecallBridge.recall ----

class TestRecall:
    def _make_clusters(self):
        return [
            LTMCluster(id="c1", about_tags=["python"], event_note="python programming",
                       evidence_support_count=10),
            LTMCluster(id="c2", about_tags=["rust"], event_note="rust programming",
                       evidence_support_count=3),
            LTMCluster(id="c3", about_tags=["cooking"], event_note="how to bake bread",
                       evidence_support_count=0),
        ]

    def test_recall_returns_matching(self):
        clusters = self._make_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=200)
        results = bridge.recall("python programming", store, top_k=5)
        assert len(results) > 0
        assert results[0].cluster_id == "c1"

    def test_recall_excludes_active_items(self):
        clusters = self._make_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=200)
        store.admit(item_id="c1", content="python programming")
        results = bridge.recall("python programming", store, top_k=5)
        ids = [r.cluster_id for r in results]
        assert "c1" not in ids

    def test_recall_top_k_limit(self):
        clusters = [
            LTMCluster(id=f"c{i}", about_tags=["test"], event_note=f"test item {i}")
            for i in range(10)
        ]
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=200)
        results = bridge.recall("test", store, top_k=3)
        assert len(results) == 3

    def test_recall_no_match_returns_empty(self):
        clusters = self._make_clusters()
        bridge = RecallBridge(clusters)
        store = WMTMStore(capacity=200)
        results = bridge.recall("quantum physics", store, top_k=5)
        assert len(results) == 0


# ---- RecallBridge.spreading_activation ----

class TestSpreadingActivation:
    def test_single_seed_returns_score(self):
        clusters = [
            LTMCluster(id="a", evidence_for=["b"]),
            LTMCluster(id="b", evidence_for=["c"]),
            LTMCluster(id="c"),
        ]
        bridge = RecallBridge(clusters)
        scores = bridge.spreading_activation(["a"], depth=2, decay=0.5)
        assert "a" in scores
        assert scores["a"] == 1.0
        assert "b" in scores
        assert scores["b"] == pytest.approx(0.5)
        assert "c" in scores

    def test_unknown_seed_ignored(self):
        clusters = [LTMCluster(id="a")]
        bridge = RecallBridge(clusters)
        scores = bridge.spreading_activation(["nonexistent"], depth=1)
        assert "nonexistent" not in scores

    def test_empty_seeds(self):
        bridge = RecallBridge([LTMCluster(id="a")])
        assert bridge.spreading_activation([]) == {}

    def test_visited_not_revisited(self):
        """A cluster visited once should not get re-activated in later hops."""
        clusters = [
            LTMCluster(id="a", evidence_for=["b"]),
            LTMCluster(id="b", evidence_for=["a"]),  # cycle
        ]
        bridge = RecallBridge(clusters)
        scores = bridge.spreading_activation(["a"], depth=5, decay=0.5)
        # a should be 1.0 (visited once), b should be 0.5
        assert scores["a"] == 1.0
        assert scores["b"] == pytest.approx(0.5)

    def test_decay_reduces_activation(self):
        clusters = [
            LTMCluster(id="a", evidence_for=["b"]),
            LTMCluster(id="b", evidence_for=["c"]),
            LTMCluster(id="c"),
        ]
        bridge = RecallBridge(clusters)
        scores = bridge.spreading_activation(["a"], depth=2, decay=0.5)
        assert scores["a"] > scores["b"] > scores["c"]


# ---- RecallBridge._tokenize ----

class TestTokenize:
    def test_basic(self):
        tokens = RecallBridge._tokenize("hello world")
        assert "hello" in tokens
        assert "world" in tokens

    def test_strips_punctuation(self):
        tokens = RecallBridge._tokenize("hello, world! (test)")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_short_words_filtered(self):
        tokens = RecallBridge._tokenize("a be cat")
        assert "a" not in tokens
        assert "a" not in tokens
        assert "cat" in tokens

    def test_case_insensitive(self):
        tokens = RecallBridge._tokenize("Hello WORLD")
        assert "hello" in tokens
        assert "world" in tokens
