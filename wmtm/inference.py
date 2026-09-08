"""WMTM-internal PLN Inference Engine.

Runs probabilistic logic inference over the active WMTM set to derive
new beliefs. Supports deduction, induction, and abduction patterns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .store import WMTMStore
from .item import WMTMItem
from .attention import AttentionValue


@dataclass
class BeliefTriple:
    """A simple (subject, relation, object) triple extracted from belief text."""
    subject: str
    relation: str
    object: str

    def __repr__(self) -> str:
        return f"({self.subject} -> {self.relation} -> {self.object})"

    def to_text(self) -> str:
        return f"{self.subject} {self.relation} {self.object}"


# Patterns for extracting triples from belief text
_RE_ARROW = re.compile(r'(\w+)\s*(?:->|implies|causes|leads to)\s*(\w+)')
_RE_ISA = re.compile(r'(\w+)\s+is\s+(?:a|an)\s+(\w+)')
_RE_HAS = re.compile(r'(\w+)\s+has\s+(\w+)')


def extract_triples(text: str) -> list[BeliefTriple]:
    """Extract (subject, relation, object) triples from text.

    Uses simple pattern matching for A->B and "A is a B" patterns.
    """
    triples = []
    for m in _RE_ARROW.finditer(text.lower()):
        triples.append(BeliefTriple(
            subject=m.group(1),
            relation='implies',
            object=m.group(2),
        ))
    for m in _RE_ISA.finditer(text.lower()):
        triples.append(BeliefTriple(
            subject=m.group(1),
            relation='is-a',
            object=m.group(2),
        ))
    for m in _RE_HAS.finditer(text.lower()):
        triples.append(BeliefTriple(
            subject=m.group(1),
            relation='has',
            object=m.group(2),
        ))
    return triples



@dataclass
class InferenceCandidate:
    """A derived belief candidate for admission to WMTM."""
    content: str
    confidence: float
    derived_from: list[str]
    inference_type: str  # 'deduction', 'induction', 'abduction', 'analogy', 'evidence_aggregation'
    triple: Optional[BeliefTriple] = None
    initial_sti: float = 0.0


@dataclass
class ContradictionReport:
    """Reports a detected contradiction between items."""
    subject: str
    relation: str
    conflicting_objects: list[str]
    item_ids: list[str]
    severity: float  # higher = more severe


@dataclass
class ContradictionResolution:
    """Records the resolution of a contradiction.

    The winner is the item with the highest composite score
    (attention + utility). Losers are penalized or evicted
    depending on severity.
    """
    report: ContradictionReport
    winner_id: str
    loser_ids: list[str]
    strategy: str  # 'composite' (STI + utility weighted)
    winner_score: float
    loser_scores: list[float]
    action: str  # 'penalize' or 'evict'
    evicted_ids: list[str] = field(default_factory=list)
    evicted_items: list = field(default_factory=list)  # WMTMItem objects that were evicted


class WMTMInferenceEngine:
    """Runs PLN-style inference over the active WMTM set.

    Patterns supported:
    - Deduction: A->B, B->C => A->C (with confidence = s1*s2)
    - Induction: multiple A->B => generalize to A implies B
    - Abduction: B true, A->B => maybe A (lower confidence)
    - Analogy: shared relation across pairs => analogical transfer
    - Evidence aggregation: multiple items support same conclusion

    Inference budget:
    - Only run on top-50% STI items (attention focus)
    - Max candidates per cycle (default 5)
    - Confidence threshold for budget filtering (default 0.3)
    """

    def __init__(
        self,
        novelty_bonus: float = 0.5,
        min_confidence: float = 0.1,
        inference_budget: int = 5,
        sti_focus_ratio: float = 0.5,
        budget_confidence_threshold: float = 0.3,
    ):
        self.novelty_bonus = novelty_bonus
        self.min_confidence = min_confidence
        self.inference_budget = inference_budget
        self.sti_focus_ratio = sti_focus_ratio
        self.budget_confidence_threshold = budget_confidence_threshold

    def _focus_set(self, active_set: list[WMTMItem]) -> list[WMTMItem]:
        """Return the top-N% STI items (attention focus).

        If fewer than 2 items, return all.
        """
        if len(active_set) <= 2:
            return active_set
        sorted_items = sorted(active_set, key=lambda it: it.attention.sti, reverse=True)
        cutoff = max(2, int(len(sorted_items) * self.sti_focus_ratio))
        return sorted_items[:cutoff]

    def infer(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Run inference over the active set, return candidates.

        Applies inference budget:
        1. Filter to top-50% STI items (attention focus)
        2. Run all inference patterns
        3. Filter by min_confidence
        4. Sort by confidence descending, truncate to budget
        """
        focus_set = self._focus_set(active_set)

        candidates = []
        candidates.extend(self._deduction(focus_set))
        candidates.extend(self._induction(focus_set))
        candidates.extend(self._abduction(focus_set))
        candidates.extend(self._analogy(focus_set))
        candidates.extend(self._evidence_aggregation(focus_set))

        candidates = [c for c in candidates if c.confidence >= self.min_confidence]

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        candidates = candidates[:self.inference_budget]

        return candidates

    def generate_candidates(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Alias for infer()."""
        return self.infer(active_set)

    def _deduction(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Deduction: A->B, B->C => A->C.

        Confidence = s1 * s2 (PLN deduction rule for strength).
        """
        candidates = []
        item_triples: list[tuple[WMTMItem, BeliefTriple]] = []
        for item in active_set:
            for t in extract_triples(item.content):
                item_triples.append((item, t))

        for item1, t1 in item_triples:
            for item2, t2 in item_triples:
                if item1.id == item2.id:
                    continue
                if t1.object == t2.subject and t1.relation == 'implies' and t2.relation == 'implies':
                    confidence = 0.8 * 0.8  # simplified PLN strength product
                    parent_sti = (item1.attention.sti + item2.attention.sti) / 2
                    initial_sti = parent_sti * confidence + self.novelty_bonus
                    derived_triple = BeliefTriple(
                        subject=t1.subject,
                        relation='implies',
                        object=t2.object,
                    )
                    candidates.append(InferenceCandidate(
                        content=derived_triple.to_text(),
                        confidence=confidence,
                        derived_from=[item1.id, item2.id],
                        inference_type='deduction',
                        triple=derived_triple,
                        initial_sti=initial_sti,
                    ))
        return candidates

    def _induction(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Induction: multiple A->B instances => generalize.

        Confidence = count / (count + 1) (Laplace smoothing).
        """
        candidates = []
        triple_counts: dict[tuple[str, str, str], list[str]] = {}
        for item in active_set:
            for t in extract_triples(item.content):
                key = (t.subject, t.relation, t.object)
                triple_counts.setdefault(key, []).append(item.id)

        for (subj, rel, obj), item_ids in triple_counts.items():
            if len(item_ids) >= 2:
                count = len(item_ids)
                confidence = count / (count + 1)
                triple = BeliefTriple(subject=subj, relation=rel, object=obj)
                candidates.append(InferenceCandidate(
                    content=f"{triple.to_text()} (induced from {count} instances)",
                    confidence=confidence,
                    derived_from=item_ids,
                    inference_type='induction',
                    triple=triple,
                    initial_sti=confidence * 0.5,
                ))
        return candidates

    def _abduction(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Abduction: B is true, A->B => maybe A.

        Lower confidence than deduction (abduction is not truth-preserving).
        """
        candidates = []
        item_triples: list[tuple[WMTMItem, BeliefTriple]] = []
        for item in active_set:
            for t in extract_triples(item.content):
                item_triples.append((item, t))

        for item1, t1 in item_triples:
            if t1.relation != 'implies':
                continue
            for item2 in active_set:
                if item2.id == item1.id:
                    continue
                if t1.object in item2.content.lower():
                    confidence = 0.3  # abductive confidence is low
                    parent_sti = (item1.attention.sti + item2.attention.sti) / 2
                    initial_sti = parent_sti * confidence
                    candidates.append(InferenceCandidate(
                        content=f"maybe {t1.subject} (abduced from {t1.object} being true and {t1.subject} implies {t1.object})",
                        confidence=confidence,
                        derived_from=[item1.id, item2.id],
                        inference_type='abduction',
                        initial_sti=initial_sti,
                    ))
        return candidates

    def _analogy(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Analogy: if two pairs share the same relation, infer analogical transfer.

        Given (s1, r, o1) and (s2, r, o2) where s1 != s2 and o1 != o2,
        derive (s1, r, o2) as an analogical transfer with discounted confidence.
        """
        candidates = []
        item_triples: list[tuple[WMTMItem, BeliefTriple]] = []
        for item in active_set:
            for t in extract_triples(item.content):
                item_triples.append((item, t))

        for i, (item1, t1) in enumerate(item_triples):
            for j, (item2, t2) in enumerate(item_triples):
                if i >= j:
                    continue
                if (t1.relation == t2.relation
                        and t1.subject != t2.subject
                        and t1.object != t2.object):
                    confidence = 0.25  # analogy is speculative
                    parent_sti = (item1.attention.sti + item2.attention.sti) / 2
                    initial_sti = parent_sti * confidence
                    derived_triple = BeliefTriple(
                        subject=t1.subject,
                        relation=t1.relation,
                        object=t2.object,
                    )
                    candidates.append(InferenceCandidate(
                        content=derived_triple.to_text(),
                        confidence=confidence,
                        derived_from=[item1.id, item2.id],
                        inference_type='analogy',
                        triple=derived_triple,
                        initial_sti=initial_sti,
                    ))
        return candidates

    def _evidence_aggregation(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Evidence aggregation: multiple items support same conclusion.

        If multiple items provide evidence for the same (subject, relation)
        pair with the same object, combine their strengths using noisy-OR:
        combined = 1 - product(1 - s_i)
        """
        candidates = []
        evidence_map: dict[tuple[str, str], list[tuple[str, WMTMItem]]] = {}
        for item in active_set:
            for t in extract_triples(item.content):
                key = (t.subject, t.relation)
                evidence_map.setdefault(key, []).append((t.object, item))

        for (subj, rel), entries in evidence_map.items():
            if len(entries) < 2:
                continue
            objects = {obj for obj, _ in entries}
            if len(objects) > 1:
                continue  # disagreement handled by contradiction detection
            obj = list(objects)[0]
            n = len(entries)
            base_strength = 0.7
            combined = 1.0 - (1.0 - base_strength) ** n
            combined = min(combined, 0.95)
            item_ids = [item.id for _, item in entries]
            parent_sti = sum(item.attention.sti for _, item in entries) / n
            initial_sti = parent_sti * combined * 0.5
            triple = BeliefTriple(subject=subj, relation=rel, object=obj)
            candidates.append(InferenceCandidate(
                content=f"{triple.to_text()} (aggregated from {n} sources)",
                confidence=combined,
                derived_from=item_ids,
                inference_type='evidence_aggregation',
                triple=triple,
                initial_sti=initial_sti,
            ))
        return candidates

    def detect_contradictions(self, store: WMTMStore) -> list[ContradictionReport]:
        """Detect contradictions between items in the store.

        Two items contradict if they share the same (subject, relation)
        but have different objects. Returns ContradictionReport for each
        conflicting pair.
        """
        reports = []
        items = store.get_active_set()
        triple_map: dict[tuple[str, str], list[tuple[str, WMTMItem]]] = {}
        for item in items:
            for t in extract_triples(item.content):
                key = (t.subject, t.relation)
                triple_map.setdefault(key, []).append((t.object, item))

        for (s, r), entries in triple_map.items():
            objects = {obj for obj, _ in entries}
            if len(objects) > 1:
                items_involved = [item for _, item in entries]
                severity = len(objects) * 0.3
                reports.append(ContradictionReport(
                    subject=s,
                    relation=r,
                    conflicting_objects=sorted(objects),
                    item_ids=[it.id for it in items_involved],
                    severity=severity,
                ))
        return reports

    def resolve_contradictions(
        self,
        store: WMTMStore,
        reports: list[ContradictionReport],
        eviction_severity_threshold: float = 0.9,
    ) -> list[ContradictionResolution]:
        """Resolve contradictions by attention+utility-weighted winner selection.

        For each contradiction:
        1. Compute composite score for each involved item:
           score = sti * 1.0 + utility * 0.5
        2. Highest-scoring item wins (gets a small STI boost).
        3. Losing items are either:
           - Penalized (STI reduction) if severity < eviction_severity_threshold
           - Evicted from the store if severity >= eviction_severity_threshold

        Returns list of ContradictionResolution records.
        """
        resolutions = []
        for report in reports:
            items = [store.get(iid) for iid in report.item_ids]
            items = [it for it in items if it is not None]
            if len(items) < 2:
                continue

            # Compute composite scores
            scored = []
            for item in items:
                score = item.attention.sti * 1.0 + item.utility * 0.5
                scored.append((score, item))

            scored.sort(key=lambda x: x[0], reverse=True)
            winner_score, winner = scored[0]
            losers = scored[1:]
            loser_ids = [item.id for _, item in losers]
            loser_scores = [score for score, _ in losers]

            # Decide action based on severity
            should_evict = report.severity >= eviction_severity_threshold
            action = 'evict' if should_evict else 'penalize'

            evicted_ids: list[str] = []
            evicted_items: list[WMTMItem] = []
            for score, loser in losers:
                if should_evict:
                    evicted = store.evict(loser.id)
                    if evicted is not None:
                        evicted_ids.append(loser.id)
                        evicted_items.append(evicted)
                else:
                    # Penalize: reduce STI proportional to severity
                    penalty_amount = report.severity * loser.attention.sti
                    loser.attention.penalty(penalty_amount)

            # Boost winner (vindicated)
            winner.attention.boost(report.severity * 2.0)

            resolutions.append(ContradictionResolution(
                report=report,
                winner_id=winner.id,
                loser_ids=loser_ids,
                strategy='composite',
                winner_score=winner_score,
                loser_scores=loser_scores,
                action=action,
                evicted_ids=evicted_ids,
                evicted_items=evicted_items,
            ))

        return resolutions

    def is_novel(
        self,
        candidate: InferenceCandidate,
        store: WMTMStore,
    ) -> bool:
        """Check if a candidate is novel (not duplicating existing WMTM item).

        Uses exact content comparison (case-insensitive) rather than
        substring matching, because derived items often contain parent
        content as part of their explanation (e.g., abduction explanations
        reference the implication they were abduced from).
        """
        cand_lower = candidate.content.lower().strip()
        for item in store.get_active_set():
            if item.content.lower().strip() == cand_lower:
                return False
        return True

    def admit_derived(
        self,
        candidates: list[InferenceCandidate],
        store: WMTMStore,
    ) -> list[WMTMItem]:
        """Admit novel inference candidates into the WMTM store."""
        admitted = []
        for i, cand in enumerate(candidates):
            if not self.is_novel(cand, store):
                continue
            item_id = f"derived-{cand.inference_type}-{i}"
            item = store.admit(
                item_id=item_id,
                content=cand.content,
                source_type='derived',
                derived_from=cand.derived_from,
                initial_sti=cand.initial_sti,
            )
            if item is not None:
                admitted.append(item)
        return admitted
