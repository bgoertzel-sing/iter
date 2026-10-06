"""RecallBridge: LTM -> WMTM pull mechanism.

Selects which items to pull from the LTM archive (petta-memory journal)
into the WMTM based on current task context and ECAN attention signals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .store import WMTMStore

# F27: Function words carry no topical signal. Without this filter, query words
# like "how"/"is"/"the" matched long consolidation notes with high IDF (rare in
# the terse journal) and outranked the actually relevant cluster.
_STOPWORDS: frozenset[str] = frozenset("""
a an the is are was were be been being am do does did done doing have has had
how what when where why who whom which whose that this these those there here
it its of to in on at by for from with about as into onto over under up down
out off and or but not no nor so if then than too very can could should would
will shall may might must just also any all some each every our your my me we
you he she they them his her their us i let get got going go work working
""".split())


_RE_CLUSTER_BEGIN = re.compile(r";;; BEGIN MemoryCluster (\S+)")
_RE_CLUSTER_END = re.compile(r";;; END MemoryCluster (\S+)")
_RE_ABOUT = re.compile(r"\(About (\S+) (\S+)\)")
_RE_EVENT_NOTE = re.compile(r'\(EventNote (\S+) "(.*)"\)')
_RE_EVIDENCE_FOR = re.compile(r"\(EvidenceFor (\S+) (\S+)\)")
_RE_CLUSTER_TYPE = re.compile(r"\(ClusterType (\S+) (\S+)\)")
_RE_EVIDENCE_SUPPORT = re.compile(r"\(EvidenceSupportCount (\S+) (\d+)\)")
_RE_PROMOTES_FROM = re.compile(r"\(PromotesFrom (\S+) (\S+)\)")
_RE_SUPERSEDES = re.compile(r"\(Supersedes (\S+) (\S+)\)")
# F27: explicit cluster open timestamp, e.g. (ClusterOpenedAt ep-x 2026-09-25T00:00:00Z)
_RE_CLUSTER_OPENED = re.compile(r"\(ClusterOpenedAt (\S+) (\S+)\)")


_LOOP_NOISE_RE = re.compile(r"\bAUTOCYCLE\b|\bNop\.|NO ADDITIONAL USER INPUT|CONTINUE THE CURRENT USER TASK", re.I)


def _parse_iso_utc(value: str) -> float:
    """Parse an ISO-8601 UTC timestamp to Unix seconds; 0.0 if unparseable."""
    import calendar, datetime
    v = value.strip().rstrip(")")
    if v.endswith("Z"):
        v = v[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(v, fmt)
            return float(calendar.timegm(dt.timetuple()))
        except ValueError:
            continue
    return 0.0


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
    opened_at: float = 0.0  # F27: Unix ts from ClusterOpenedAt, 0.0 if absent

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
    if not cluster.opened_at:
        for m in _RE_CLUSTER_OPENED.finditer(stripped):
            if m.group(1) == cluster.id:
                cluster.opened_at = _parse_iso_utc(m.group(2))


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

    def get_cluster_timestamp(self, cluster_id: str) -> float:
        """Return the parsed Unix timestamp for a cluster ID, or 0.0 if unknown.

        F25: Used by the transformation to pass origin_timestamp when admitting
        recalled items, so staleness checks use the journal entry's real date
        instead of the re-admission timestamp.
        """
        c = self._by_id.get(cluster_id)
        if c is not None and c.opened_at > 0:
            return c.opened_at  # F27: explicit ClusterOpenedAt wins
        return self._parse_cluster_timestamp(cluster_id)

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
        """Compute relevance score for a cluster against query terms.
        
        Scoring layers:
        1. Term overlap with IDF-like weighting (rare terms score higher)
        2. Substring matching for multi-word phrases
        3. Bigram matching for contextual relevance
        4. Evidence support bonus
        5. Spreading activation from active WMTM items
        6. Recency bias (newer clusters get a small boost)
        """
        score: float = 0.0
        cluster_text: set[str] = self._tokenize(cluster.text)
        matching_terms: set[str] = query_terms & cluster_text

        # Layer 1: IDF-weighted term overlap
        # Terms that appear in fewer clusters are more distinctive
        for term in matching_terms:
            idf = self._idf(term)
            score += idf

        # Layer 2: Substring matching (catches multi-word terms)
        cluster_text_raw: str = cluster.text.lower()
        for term in query_terms:
            # F27: only substantive terms; short tokens substring-match noise
            if len(term) >= 4 and term in cluster_text_raw:
                score += 0.5

        if score == 0:
            return 0.0

        # Layer 3: Bigram matching (consecutive word pairs)
        query_bigrams = self._bigrams(query_terms)
        cluster_bigrams = self._bigrams(cluster_text)
        bigram_matches = query_bigrams & cluster_bigrams
        score += len(bigram_matches) * 1.5  # bigrams are stronger signals

        # Layer 4: Evidence support
        score += min(cluster.evidence_support_count * 0.01, 2.0)

        # Layer 5: Spreading activation
        spread_score: float = self._spreading_activation_for(cluster, active_ids, store)
        score += spread_score

        # F29: down-weight autonomous-loop heartbeat notes (AUTOCYCLE / Nop).
        # They match generic words like "status"/"check" without carrying
        # task information, and outrank substantive entries on short queries.
        if _LOOP_NOISE_RE.search(cluster.text):
            score *= 0.5

        # Layer 6: Recency bias (newer = slightly higher)
        # Extract timestamp from cluster ID format: ep-note-20260918T061756Z
        cluster_ts = cluster.opened_at or self._parse_cluster_timestamp(cluster.id)
        if cluster_ts > 0:
            import time as _time
            age_hours = (_time.time() - cluster_ts) / 3600
            if age_hours < 24:
                score *= 1.3  # last 24h: 30% boost
            elif age_hours < 168:
                score *= 1.1  # last week: 10% boost
            elif age_hours > 336:  # > 2 weeks
                # F01d: Decay old items so ancient operational noise doesn't
                # dominate over recent relevant entries. Items older than 2
                # weeks get progressively penalized (20% per extra week).
                extra_weeks = (age_hours - 336) / 168
                score *= max(0.3, 0.8 ** extra_weeks)  # floor at 30%

        return score

    def _idf(self, term: str) -> float:
        """Inverse document frequency: rare terms across clusters score higher."""
        if not hasattr(self, '_idf_cache'):
            self._idf_cache = {}
            total = max(len(self._clusters), 1)
            term_counts: dict[str, int] = {}
            for c in self._clusters:
                seen = set()
                for t in self._tokenize(c.text):
                    if t not in seen:
                        term_counts[t] = term_counts.get(t, 0) + 1
                        seen.add(t)
            import math
            for t, count in term_counts.items():
                self._idf_cache[t] = math.log(total / count) + 1.0
        return self._idf_cache.get(term, 1.0)

    @staticmethod
    def _bigrams(terms: set[str]) -> set[str]:
        """Generate bigrams from a set of terms (sorted for consistency)."""
        sorted_terms = sorted(terms)
        result: set[str] = set()
        for i in range(len(sorted_terms) - 1):
            result.add(f"{sorted_terms[i]}_{sorted_terms[i+1]}")
        return result

    @staticmethod
    def _parse_cluster_timestamp(cluster_id: str) -> float:
        """Extract a Unix timestamp from cluster IDs.

        F26: Handles multiple date formats found in journal cluster IDs:
        - ISO compact: ep-note-20260918T061756Z
        - Date-only: fact-evlink3-2026-09-03, ep-20260903010647219173
        - Epoch micros: ep-20260903010647219173 (fallback)
        """
        import re
        # Format 1: ISO compact (most precise)
        m = re.search(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z', cluster_id)
        if m:
            try:
                import calendar, datetime
                dt = datetime.datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                )
                return calendar.timegm(dt.timetuple())
            except (ValueError, OverflowError):
                pass

        # Format 2: YYYY-MM-DD anywhere in the ID (e.g. fact-evlink3-2026-09-03)
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})', cluster_id)
        if m:
            try:
                import calendar, datetime
                dt = datetime.datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    12, 0, 0,  # noon UTC as reasonable midpoint
                )
                ts = calendar.timegm(dt.timetuple())
                if 1577836800 < ts < 1893456000:  # 2020-2030 sanity check
                    return ts
            except (ValueError, OverflowError):
                pass

        # Format 3: Compact YYYYMMDD prefix in numeric IDs
        # e.g. ep-20260903010647219173 → 20260903
        m = re.search(r'(20\d{2})(\d{2})(\d{2})\d{6,}', cluster_id)
        if m:
            try:
                import calendar, datetime
                dt = datetime.datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    12, 0, 0,
                )
                ts = calendar.timegm(dt.timetuple())
                if 1577836800 < ts < 1893456000:
                    return ts
            except (ValueError, OverflowError):
                pass

        return 0.0

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
            # F32: strip English possessive so "H2's" matches "H2".
            for _pos in ("'s", "\u2019s"):
                if word.endswith(_pos) and len(word) > len(_pos):
                    word = word[: -len(_pos)]
                    break
            if len(word) >= 2 and word not in _STOPWORDS:
                tokens.add(word)
            # F28: also index parts of compound tokens (hive-appliance,
            # re-review, bgoertzel-sing/iter) so natural-language queries
            # ("hive appliance review") match hyphenated journal terms.
            if any(sep in word for sep in "-/_"):
                for part in re.split(r"[-/_]+", word):
                    part = part.strip(".,;:!?()[]{}\"'")
                    if len(part) >= 2 and part not in _STOPWORDS:
                        tokens.add(part)
        return tokens
