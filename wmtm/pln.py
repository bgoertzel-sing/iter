"""PLN (Probabilistic Logic Networks) Truth Values and Inference Rules.

Implements proper PLN truth value algebra and inference rules
that operate over WMTM items. This is the bridge between WMTM's
working memory and PLN-style probabilistic reasoning.

TruthValue conventions (PLN standard):
  strength ∈ [0,1]      -- how true the proposition is
  confidence ∈ [0,1]    -- how much evidence backs it (count-based)

Inference rules implemented:
  - Deduction: A->B, B->C => A->C (strength=s_A*s_B, conf=conf_A*conf_B)
  - Induction: A->B, A->C => B~C (generalization)
  - Abduction: B, A->B => A (with reduced confidence)
  - Inheritance: MemberLink + ConceptLink => InheritanceLink
  - Similarity: shared properties => similarity assessment
  - AND/OR/NOT: logical combination with proper PLN semantics
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# ─── TruthValue ───────────────────────────────────────────────────────

@dataclass
class TruthValue:
    """PLN SimpleTruthValue: (strength, confidence).

    strength: P(proposition is true) ∈ [0, 1]
    confidence: based on evidence count, ∫ of evidence / (evidence + K)
      where K is the lookahead parameter (default 1, scaled to [0,1]).
    """
    strength: float = 0.5
    confidence: float = 0.0

    def __repr__(self) -> str:
        """Return concise truth-value representation."""
        return f"TV(s={self.strength:.3f}, c={self.confidence:.3f})"

    def __eq__(self, other: object) -> bool:
        """Check truth-value equality within floating-point tolerance."""
        if not isinstance(other, TruthValue):
            return NotImplemented
        return (abs(self.strength - other.strength) < 1e-6 and
                abs(self.confidence - other.confidence) < 1e-6)

    # ── PLN Logical Operators ──

    def conjunct(self, other: TruthValue) -> TruthValue:
        """AND: min strength, product confidence."""
        return TruthValue(
            strength=min(self.strength, other.strength),
            confidence=self.confidence * other.confidence,
        )

    def disjunct(self, other: TruthValue) -> TruthValue:
        """OR: max strength, probabilistic sum confidence."""
        return TruthValue(
            strength=max(self.strength, other.strength),
            confidence=self.confidence + other.confidence
                       - self.confidence * other.confidence,
        )

    def negate(self) -> TruthValue:
        """NOT: invert strength, keep confidence."""
        return TruthValue(
            strength=1.0 - self.strength,
            confidence=self.confidence,
        )

    def weighted_avg(self, other: TruthValue, w: float) -> TruthValue:
        """Weighted average of two truth values."""
        w = max(0.0, min(1.0, w))
        return TruthValue(
            strength=w * self.strength + (1 - w) * other.strength,
            confidence=w * self.confidence + (1 - w) * other.confidence,
        )

    def implicates(self, other: TruthValue) -> TruthValue:
        """PLN Implication: A => B.

        strength = P(B|A) = P(A∧B) / P(A)
        Simplified: s = min(1, s_B / max(s_A, epsilon))
        confidence = c_A * c_B
        """
        eps = 1e-8
        s_imp = min(1.0, other.strength / max(self.strength, eps))
        c_imp = self.confidence * other.confidence
        return TruthValue(strength=s_imp, confidence=c_imp)

    # ── Evidence count conversion ──

    @staticmethod
    def from_evidence(
        positive: float,
        negative: float,
        lookahead: float = 1.0,
    ) -> TruthValue:
        """Create TV from positive/negative evidence counts.

        strength = pos / (pos + neg)
        confidence = (pos + neg) / (pos + neg + lookahead)
        """
        total = positive + negative
        if total == 0:
            return TruthValue(strength=0.5, confidence=0.0)
        s = positive / total
        c = total / (total + lookahead)
        return TruthValue(strength=s, confidence=c)

    def add_evidence(self, positive: float, negative: float, lookahead: float = 1.0) -> TruthValue:
        """Revise this TV with new evidence."""
        if self.confidence > 0:
            cur_total = self.confidence * lookahead / (1 - self.confidence + 1e-8)
        else:
            cur_total = 0.0
        cur_pos = cur_total * self.strength
        cur_neg = cur_total * (1 - self.strength)
        return TruthValue.from_evidence(
            cur_pos + positive, cur_neg + negative, lookahead
        )

    def to_dict(self) -> dict:
        """Serialize TruthValue to a dict with rounded values."""
        return {"strength": round(self.strength, 4), "confidence": round(self.confidence, 4)}

    @classmethod
    def from_dict(cls, d: dict) -> TruthValue:
        """Reconstruct a TruthValue from a serialized dict."""
        return cls(strength=d["strength"], confidence=d["confidence"])


# ─── PLN Atoms ────────────────────────────────────────────────────────

@dataclass
class PLNAtom:
    """A PLN atom: a proposition with a truth value.

    atom_type: 'Concept', 'Inheritance', 'Member', 'Evaluation', 'Implication'
    name: human-readable identifier
    truth: TruthValue
    source_ids: IDs of WMTM items this atom was derived from
    """
    atom_type: str
    name: str
    truth: TruthValue
    source_ids: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Return concise PLN-atom representation."""
        return f"PLNAtom({self.atom_type}:{self.name} {self.truth})"


# ─── PLN Inference Rules ──────────────────────────────────────────────

class PLNInferenceEngine:
    """Runs PLN inference rules over a set of atoms.

    Supports:
    - Deduction: Inheritance(A,B) ∧ Inheritance(B,C) => Inheritance(A,C)
    - Abduction: Inheritance(A,B) ∧ B.observed => A (weakened)
    - Induction: Inheritance(A,B) ∧ Inheritance(A,C) => Similarity(B,C)
    - ANDIntroduction: combine multiple atoms into a conjunction
    - ORIntroduction: combine into disjunction
    - ContradictionDetection: A ∧ ¬A
    """

    def __init__(
        self,
        min_strength: float = 0.1,
        min_confidence: float = 0.05,
    ) -> None:
        """Initialize PLN inference engine with strength and confidence thresholds."""
        self.min_strength = min_strength
        self.min_confidence = min_confidence

    def deduction(
        self,
        ab: TruthValue,
        bc: TruthValue,
    ) -> TruthValue:
        """Deduction: A->B, B->C => A->C.

        strength = s_AB * s_BC
        confidence = c_AB * c_BC * (1 - |s_AB - s_BC|)  # discount for disagreement
        """
        s = ab.strength * bc.strength
        c = ab.confidence * bc.confidence * (1 - abs(ab.strength - bc.strength))
        return TruthValue(strength=s, confidence=c)

    def abduction(
        self,
        ab: TruthValue,
        b_observed: TruthValue,
    ) -> TruthValue:
        """Abduction: B observed, A->B known => maybe A.

        strength = s_AB * s_B
        confidence = c_AB * c_B * s_AB  # extra discount for abduction
        """
        s = ab.strength * b_observed.strength
        c = ab.confidence * b_observed.confidence * ab.strength
        return TruthValue(strength=s, confidence=c)

    def induction(
        self,
        ab: TruthValue,
        ac: TruthValue,
    ) -> TruthValue:
        """Induction: A->B, A->C => B~C (similarity).

        strength = s_AB * s_AC + (1 - s_AB) * (1 - s_AC)
        confidence = min(c_AB, c_AC) * 0.5  # induction is weaker
        """
        s = ab.strength * ac.strength + (1 - ab.strength) * (1 - ac.strength)
        c = min(ab.confidence, ac.confidence) * 0.5
        return TruthValue(strength=s, confidence=c)

    def analogy_transfer(
        self,
        ab: TruthValue,
        cd: TruthValue,
        ac: TruthValue,
    ) -> TruthValue:
        """Analogy: A~B, C~D, A~C => B~D.

        Transfer similarity from known pairs to unknown pair.
        """
        s = ac.strength * cd.strength * ab.strength
        c = min(ab.confidence, cd.confidence, ac.confidence) * 0.3
        return TruthValue(strength=s, confidence=c)

    def evidence_aggregation(
        self,
        evidence: list[TruthValue],
    ) -> TruthValue:
        """Aggregate multiple evidence sources for the same conclusion.

        Uses weighted average with confidence as weight.
        """
        if not evidence:
            return TruthValue(strength=0.5, confidence=0.0)
        total_w = sum(tv.confidence for tv in evidence)
        if total_w == 0:
            return TruthValue(strength=0.5, confidence=0.0)
        s = sum(tv.strength * tv.confidence for tv in evidence) / total_w
        
        c = 1.0
        for tv in evidence:
            c = c * (1 - tv.confidence)
        c = 1 - c
        return TruthValue(strength=s, confidence=c)

    def contradiction_strength(
        self,
        a: TruthValue,
        neg_a: TruthValue,
    ) -> float:
        """Detect contradiction between A and ¬A.

        Returns severity: 0 if no contradiction, 1 if perfect contradiction.
        Both must have sufficient confidence.
        """
        if a.confidence < self.min_confidence or neg_a.confidence < self.min_confidence:
            return 0.0
        severity = a.strength * neg_a.strength * a.confidence * neg_a.confidence
        return severity

    def is_above_threshold(self, tv: TruthValue) -> bool:
        """Check if a truth value passes the minimum threshold."""
        return (tv.strength >= self.min_strength and
                tv.confidence >= self.min_confidence)


# ─── Triple extraction ───────────────────────────────────────────────

_RE_ARROW = re.compile(r'(\w+)\s*(?:->|implies|causes|leads to)\s*(\w+)')
_RE_ISA = re.compile(r'(\w+)\s+is\s+(?:a|an)\s+(\w+)')
_RE_HAS = re.compile(r'(\w+)\s+ha(?:s|ve)\s+(\w+)')
_RE_SIMILAR = re.compile(r'(\w+)\s+(?:is similar to|resembles|~)\s+(\w+)')


def extract_pln_atoms(text: str, source_id: str = "") -> list[PLNAtom]:
    """Extract PLN atoms from natural language text."""
    atoms = []
    default_tv = TruthValue(strength=0.8, confidence=0.5)

    for m in _RE_ARROW.finditer(text.lower()):
        name = f"Implication({m.group(1)},{m.group(2)})"
        atoms.append(PLNAtom(
            atom_type="Implication",
            name=name,
            truth=default_tv,
            source_ids=[source_id] if source_id else [],
        ))

    for m in _RE_ISA.finditer(text.lower()):
        name = f"Inheritance({m.group(1)},{m.group(2)})"
        atoms.append(PLNAtom(
            atom_type="Inheritance",
            name=name,
            truth=default_tv,
            source_ids=[source_id] if source_id else [],
        ))

    for m in _RE_HAS.finditer(text.lower()):
        name = f"Evaluation(has,{m.group(1)},{m.group(2)})"
        atoms.append(PLNAtom(
            atom_type="Evaluation",
            name=name,
            truth=default_tv,
            source_ids=[source_id] if source_id else [],
        ))

    for m in _RE_SIMILAR.finditer(text.lower()):
        name = f"Similarity({m.group(1)},{m.group(2)})"
        atoms.append(PLNAtom(
            atom_type="Similarity",
            name=name,
            truth=default_tv,
            source_ids=[source_id] if source_id else [],
        ))

    return atoms


def pln_inference_over_atoms(atoms: list[PLNAtom], engine: PLNInferenceEngine = None) -> list[PLNAtom]:
    """Run PLN inference over a set of atoms, return derived atoms."""
    if engine is None:
        engine = PLNInferenceEngine()
    derived = []

    # F04: Separate Inheritance and Implication to prevent cross-type deduction.
    inheritance_map = {}   # for Inheritance deduction (is-a)
    implication_map = {}   # for Implication deduction (causal/logical)
    for atom in atoms:
        match = re.match(r'\w+\((\w+),(\w+)\)', atom.name)
        if not match:
            continue
        subj, obj = match.group(1), match.group(2)
        if atom.atom_type == "Inheritance":
            if subj not in inheritance_map:
                inheritance_map[subj] = []
            inheritance_map[subj].append((obj, atom))
        elif atom.atom_type == "Implication":
            if subj not in implication_map:
                implication_map[subj] = []
            implication_map[subj].append((obj, atom))

    seen = {a.name for a in atoms}

    # F04: Deduction preserves relation type.
    # Inheritance deduction: A is-a B, B is-a C => A is-a C
    for subj_a, targets_a in inheritance_map.items():
        for obj_b, atom_ab in targets_a:
            if obj_b in inheritance_map:
                for obj_c, atom_bc in inheritance_map[obj_b]:
                    name = f"Inheritance({subj_a},{obj_c})"
                    if name not in seen:
                        tv = engine.deduction(atom_ab.truth, atom_bc.truth)
                        if engine.is_above_threshold(tv):
                            derived_atom = PLNAtom(
                                atom_type="Inheritance",
                                name=name,
                                truth=tv,
                                source_ids=atom_ab.source_ids + atom_bc.source_ids,
                            )
                            derived.append(derived_atom)
                            seen.add(name)

    # Implication deduction: A -> B, B -> C => A -> C
    for subj_a, targets_a in implication_map.items():
        for obj_b, atom_ab in targets_a:
            if obj_b in implication_map:
                for obj_c, atom_bc in implication_map[obj_b]:
                    name = f"Implication({subj_a},{obj_c})"
                    if name not in seen:
                        tv = engine.deduction(atom_ab.truth, atom_bc.truth)
                        if engine.is_above_threshold(tv):
                            derived_atom = PLNAtom(
                                atom_type="Implication",
                                name=name,
                                truth=tv,
                                source_ids=atom_ab.source_ids + atom_bc.source_ids,
                            )
                            derived.append(derived_atom)
                            seen.add(name)

    # Induction: A->B, A->C => Similarity(B,C)
    # F04: Apply to both Inheritance and Implication maps.
    for rel_map in (inheritance_map, implication_map):
        for subj_a, targets_a in rel_map.items():
            for i, (obj_b, atom_ab) in enumerate(targets_a):
                for j, (obj_c, atom_ac) in enumerate(targets_a):
                    if i < j:
                        name = f"Similarity({obj_b},{obj_c})"
                        if name not in seen:
                            tv = engine.induction(atom_ab.truth, atom_ac.truth)
                            if engine.is_above_threshold(tv):
                                derived_atom = PLNAtom(
                                    atom_type="Similarity",
                                    name=name,
                                    truth=tv,
                                    source_ids=atom_ab.source_ids + atom_ac.source_ids,
                                )
                                derived.append(derived_atom)
                                seen.add(name)

    return derived
