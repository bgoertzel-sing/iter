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
    inference_type: str  # 'deduction', 'induction', 'abduction'
    triple: Optional[BeliefTriple] = None
    initial_sti: float = 0.0


class WMTMInferenceEngine:
    """Runs PLN-style inference over the active WMTM set.

    Patterns supported:
    - Deduction: A->B, B->C => A->C (with confidence = s1*s2)
    - Induction: multiple A->B => generalize to A implies B
    - Abduction: B true, A->B => maybe A (lower confidence)
    """

    def __init__(self, novelty_bonus: float = 0.5, min_confidence: float = 0.1):
        self.novelty_bonus = novelty_bonus
        self.min_confidence = min_confidence

    def infer(self, active_set: list[WMTMItem]) -> list[InferenceCandidate]:
        """Run inference over the active set, return candidates."""
        candidates = []
        candidates.extend(self._deduction(active_set))
        candidates.extend(self._induction(active_set))
        candidates.extend(self._abduction(active_set))
        candidates = [c for c in candidates if c.confidence >= self.min_confidence]
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

    def is_novel(
        self,
        candidate: InferenceCandidate,
        store: WMTMStore,
    ) -> bool:
        """Check if a candidate is novel (not duplicating existing WMTM item)."""
        for item in store.get_active_set():
            if candidate.content.lower() in item.content.lower():
                return False
            if item.content.lower() in candidate.content.lower():
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
