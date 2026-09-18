"""RecallBridge: LTM -> WMTM pull mechanism.

Selects which items to pull from the LTM archive (petta-memory journal)
into the WMTM based on current task context and ECAN attention signals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .store import WMTMStore


_RE_CLUSTER_BEGIN = re.compile(r";;; BEGIN MemoryCluster (\S+)")
_RE_CLUSTER_END = re.compile(r";;; END MemoryCluster (\S+)")
_RE_ABOUT = re.compile(r"\(About (\S+) (\S+)\)")
_RE_EVENT_NOTE = re.compile(r'\(EventNote (\S+) "(.*)"\)')
_RE_EVIDENCE_FOR = re.compile(r"\(EvidenceFor (\S+) (\S+)\)")
_RE_CLUSTER_TYPE = re.compile(r"\(ClusterType (\S+) (\S+)\)")
_RE_EVIDENCE_SUPPORT = re.compile(r"\(EvidenceSupportCount (\S+) (\d+)\)")
_RE_PROMOTES_FROM = re.compile(r"\(PromotesFrom (\S+) (\S+)\)")
_RE_SUPERSEDES = re.compile(r"\(Supersedes (\S+) (\S+)\)")


@dataclass
class LTMCluster:
    """Parsed representation of a single LTM journal cluster for WMTM admission."""
    id: str
    cluster_type: str = "Unknown"
    about_tags: list[str] = field(default_factory=list)
    event_note: str = ""
    evidence_for: list[str] = field(default_factory=list)
    promotes_from: list[str] = field(default_factory=list)
    evidence_support_count: int = 0
    raw_lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Return concatenated about_tags and event_note as searchable text."""
        return " ".join(self.about_tags) + " " + self.event_note


def _collect_superseded(lines: list[str]) -> set[str]:
    """Scan all lines for Supersedes markers and return superseded cluster IDs."""
    superseded: set[str] = set()
    for line in lines:
        for m in _RE_SUPERSEDES.finditer(line):
            old_id: str = m.group(2)
            if old_id != "old-id":
                superseded.add(old_id)
    return superseded


def _populate_cluster_fields(cluster: LTMCluster, stripped: str) -> None:
    """Extract metadata from a single line into the current cluster."""
    for m in _RE_ABOUT.finditer(stripped):
        if m.group(1) == cluster.id:
            cluster.about_tags.append(m.group(2))
    _extract_simple(cluster, stripped, _RE_EVENT_NOTE, "event_note")
    for m in _RE_EVIDENCE_FOR.finditer(stripped):
        cluster.evidence_for.append(m.group(2))
    _extract_cluster_scoped(cluster, stripped, _RE_CLUSTER_TYPE, "cluster_type")
    _extract_cluster_scoped_int(cluster, stripped, _RE_EVIDENCE_SUPPORT, "evidence_support_count")
    for m in _RE_PROMOTES_FROM.finditer(stripped):
        cluster.promotes_from.append(m.group(2))


def _extract_simple(cluster: LTMCluster, stripped: str, regex, attr: str) -> None:
    """Set a simple string attribute from the first regex match."""
    m = regex.search(stripped)
    if m:
        setattr(cluster, attr, m.group(2))


def _extract_cluster_scoped(cluster: LTMCluster, stripped: str, regex, attr: str) -> None:
    """Set an attribute only if the regex match's group(1) equals the cluster ID."""
    for m in regex.finditer(stripped):
        if m.group(1) == cluster.id:
            setattr(cluster, attr, m.group(2))


def _extract_cluster_scoped_int(cluster: LTMCluster, stripped: str, regex, attr: str) -> None:
    """Set an int attribute only if the regex match's group(1) equals the cluster ID."""
    for m in regex.finditer(stripped):
        if m.group(1) == cluster.id:
            setattr(cluster, attr, int(m.group(2)))


def parse_journal(lines: list[str]) -> list[LTMCluster]:
    """Parse journal lines into LTMCluster objects, skipping superseded.

    Two-pass parser:
    1. Collect all Supersedes markers to identify superseded cluster IDs.
    2. Walk lines, opening clusters on BEGIN markers, populating fields
       from metadata lines, and closing on END markers (skipping superseded).
    """
    superseded: set[str] = _collect_superseded(lines)

    clusters: list[LTMCluster] = []
    current: Optional[LTMCluster] = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        begin_m = _RE_CLUSTER_BEGIN.match(stripped)
        if begin_m:
            cid: str = begin_m.group(1)
            current = LTMCluster(id=cid, raw_lines=[])
            current_lines = [line]
            continue

        end_m = _RE_CLUSTER_END.match(stripped)
        if end_m and current is not None:
            cid = end_m.group(1)
            if cid not in superseded:
                current.raw_lines = current_lines
                clusters.append(current)
            current = None
            current_lines = []
            continue

        if current is not None:
            current_lines.append(line)
            _populate_cluster_fields(current, stripped)

    return clusters


@dataclass
class RecallCandidate:
    """A cluster candidate for admission into WMTM, with computed priority score."""
    cluster_id: str
    content: str
    score: float
    source_cluster: str
    about_tags: list[str] = field(default_factory=list)


class RecallBridge:
    """Bridges LTM archive -> WMTM via keyword match + spreading activation."""

    def __init__(self, ltm_clusters: list[LTMCluster]) -> None:
        """Initialize the recall bridge with store, forgetting policy, and inference engine."""
        self._clusters: list[LTMCluster] = ltm_clusters
        self._by_id: dict[str, LTMCluster] = {c.id: c for c in ltm_clusters}
        self._evidence_for_targets: dict[str, list[str]] = {}
        for c in ltm_clusters:
            for target in c.evidence_for:
                self._evidence_for_targets.setdefault(target, []).append(c.id)

    def recall(
        self,
        query_context: str,
        store: WMTMStore,
        top_k: int = 20,
    ) -> list[RecallCandidate]:
        """Recall items from LTM matching query_context."""
        query_terms: set[str] = self._tokenize(query_context)
        active_ids: set[str] = {item.id for item in store.get_active_set()}
        candidates: list[RecallCandidate] = []

        for cluster in self._clusters:
            if cluster.id in active_ids:
                continue
            score = self._score_cluster(cluster, query_terms, active_ids, store)
            if score <= 0:
                continue
            candidates.append(RecallCandidate(
                cluster_id=cluster.id,
                content=cluster.event_note or cluster.text,
                score=score,
                source_cluster=cluster.id,
                about_tags=cluster.about_tags,
            ))

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:top_k]

    def _score_cluster(
        self,
        cluster: LTMCluster,
        query_terms: set[str],
        active_ids: set[str],
        store: WMTMStore,
    ) -> float:
        """Compute relevance score for a cluster against query terms."""
        score: float = 0.0
        cluster_text: set[str] = self._tokenize(cluster.text)
        matching_terms: set[str] = query_terms & cluster_text
        score += len(matching_terms) * 1.0

        cluster_text_raw: str = cluster.text.lower()
        for term in query_terms:
            if term in cluster_text_raw:
                score += 0.5

        if score == 0:
            return 0.0

        score += min(cluster.evidence_support_count * 0.01, 2.0)
        spread_score: float = self._spreading_activation_for(cluster, active_ids, store)
        score += spread_score
        return score

    def spreading_activation(
        self,
        seed_ids: list[str],
        depth: int = 2,
        decay: float = 0.5,
    ) -> dict[str, float]:
        """Follow EvidenceFor/PromotesFrom edges from seed items."""
        visited: set[str] = set()
        scores: dict[str, float] = {}
        frontier: list[tuple[str, float]] = [
            (sid, 1.0) for sid in seed_ids if sid in self._by_id
        ]

        for _hop in range(depth + 1):
            frontier = self._activation_hop(
                frontier, visited, scores, decay
            )

        return scores

    def _activation_hop(
        self,
        frontier: list[tuple[str, float]],
        visited: set[str],
        scores: dict[str, float],
        decay: float,
    ) -> list[tuple[str, float]]:
        """Process one hop of spreading activation, updating visited/scores.

        Returns the next frontier to explore.
        """
        next_frontier: list[tuple[str, float]] = []
        for cid, activation in frontier:
            if cid in visited:
                continue
            visited.add(cid)
            scores[cid] = scores.get(cid, 0.0) + activation
            cluster: Optional[LTMCluster] = self._by_id.get(cid)
            if not cluster:
                continue
            self._collect_activation_neighbors(
                cluster, activation, decay, visited, next_frontier
            )
        return next_frontier

    def _collect_activation_neighbors(
        self,
        cluster: LTMCluster,
        activation: float,
        decay: float,
        visited: set[str],
        next_frontier: list[tuple[str, float]],
    ) -> None:
        """Add unvisited EvidenceFor targets and reverse-evidence sources."""
        for target in cluster.evidence_for:
            if target not in visited:
                next_frontier.append((target, activation * decay))
        for source_id in self._evidence_for_targets.get(cluster.id, []):
            if source_id not in visited:
                next_frontier.append((source_id, activation * decay))

    def _spreading_activation_for(
        self,
        cluster: LTMCluster,
        active_ids: set[str],
        store: WMTMStore,
    ) -> float:
        """Compute spreading activation for a single token across the active set."""
        boost: float = 0.0
        for target in cluster.evidence_for:
            if target in active_ids:
                boost += 0.5
        for source_id in self._evidence_for_targets.get(cluster.id, []):
            if source_id in active_ids:
                boost += 0.5
        spread: dict[str, float] = self.spreading_activation(
            list(active_ids), depth=2, decay=0.3
        )
        if cluster.id in spread:
            boost += spread[cluster.id] * 0.2
        return boost

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Tokenize a text string into normalized lowercase tokens."""
        tokens: set[str] = set()
        for word in text.lower().split():
            word = word.strip(".,;:!?()[]{}\"'")
            if len(word) >= 2:
                tokens.add(word)
        return tokens
