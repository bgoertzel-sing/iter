"""PLN Bridge: connects WMTM items to PLN atoms and back.

This module provides bidirectional conversion between WMTM working memory
items and PLN atoms, enabling PLN inference to operate over the WMTM
active set and derived PLN conclusions to be admitted back into WMTM.

Flow:
  1. wmtm_to_pln_atoms: Convert WMTM active set → PLN atom set
  2. pln_inference_over_atoms: Run PLN inference (from pln.py)
  3. pln_atoms_to_wmtm_candidates: Convert derived atoms → WMTM candidates
  4. WMTM orchestrator admits novel candidates
"""
from __future__ import annotations

import re
from typing import Optional

from .item import WMTMItem
from .store import WMTMStore
from .pln import (
    TruthValue,
    PLNAtom,
    PLNInferenceEngine,
    extract_pln_atoms,
    pln_inference_over_atoms,
)
from .inference import InferenceCandidate


# ─── WMTM → PLN conversion ───────────────────────────────────────────

def wmtm_item_to_pln_atoms(item: WMTMItem) -> list[PLNAtom]:
    """Convert a WMTM item into one or more PLN atoms.

    F04: Uses stored epistemic TV (tv_strength, tv_confidence) when available.
    Falls back to default TV (strength=0.8, confidence=0.5) otherwise.
    Does NOT derive TV from attention/utility signals.
    """
    # Determine truth value: stored TV or default
    if getattr(item, "tv_strength", 0.0) > 0 or getattr(item, "tv_confidence", 0.0) > 0:
        tv = TruthValue(strength=getattr(item, "tv_strength", 0.0), confidence=getattr(item, "tv_confidence", 0.0))
    else:
        tv = TruthValue(strength=0.8, confidence=0.5)

    # Extract structured atoms from the item content
    atoms = extract_pln_atoms(item.content, source_id=item.id)

    # If no structured atoms were extracted, create a Concept atom
    if not atoms:
        concept_name = re.sub(r'[^a-zA-Z0-9_]', '_', item.content[:50])
        atoms = [PLNAtom(
            atom_type='Concept',
            name=f'Concept({concept_name})',
            truth=tv,
            source_ids=[item.id],
        )]
    else:
        # Update truth values with the determined TV
        for atom in atoms:
            atom.truth = tv

    return atoms


def wmtm_to_pln_atoms(active_set: list[WMTMItem]) -> list[PLNAtom]:
    """Convert the entire WMTM active set into PLN atoms."""
    all_atoms = []
    for item in active_set:
        all_atoms.extend(wmtm_item_to_pln_atoms(item))
    return all_atoms


# ─── PLN → WMTM conversion ───────────────────────────────────────────

def pln_atom_to_wmtm_candidate(
    atom: PLNAtom,
    cycle: int = 0,
    index: int = 0,
) -> Optional[InferenceCandidate]:
    """Convert a derived PLN atom into a WMTM InferenceCandidate.

    Maps PLN truth values back to WMTM attention:
      initial_sti = strength * confidence (combined certainty)
      confidence field = PLN confidence
    """
    if atom.atom_type == 'Concept':
        # Don't re-admit concept atoms; they're just representations
        return None

    # Convert atom name to natural language content
    content = atom_name_to_text(atom)

    # Initial STI from combined truth value certainty
    initial_sti = atom.truth.strength * atom.truth.confidence

    return InferenceCandidate(
        content=content,
        confidence=atom.truth.confidence,
        derived_from=atom.source_ids,
        inference_type=f'pln_{atom.atom_type.lower()}',
        triple=None,
        initial_sti=initial_sti,
    )


def atom_name_to_text(atom: PLNAtom) -> str:
    """Convert a PLN atom name to human-readable text."""
    match = re.match(r'(\w+)\((\w+),(\w+)\)', atom.name)
    if not match:
        return atom.name

    rel, a, b = match.group(1), match.group(2), match.group(3)

    if rel == 'Inheritance':
        return f'{a} is a {b}'
    elif rel == 'Implication':
        return f'{a} implies {b}'
    elif rel == 'Similarity':
        return f'{a} is similar to {b}'
    elif rel == 'Evaluation':
        return f'{a} has {b}'
    else:
        return f'{a} {rel} {b}'


def pln_atoms_to_wmtm_candidates(
    atoms: list[PLNAtom],
    cycle: int = 0,
) -> list[InferenceCandidate]:
    """Convert a list of derived PLN atoms into WMTM candidates."""
    candidates = []
    for i, atom in enumerate(atoms):
        cand = pln_atom_to_wmtm_candidate(atom, cycle=cycle, index=i)
        if cand is not None:
            candidates.append(cand)
    return candidates


# ─── Full PLN inference cycle over WMTM ───────────────────────────────

def run_pln_inference_over_wmtm(
    store: WMTMStore,
    engine: Optional[PLNInferenceEngine] = None,
) -> list[InferenceCandidate]:
    """Run PLN inference over the WMTM active set.

    This is the main entry point for PLN reasoning over working memory:
    1. Extract PLN atoms from all WMTM items
    2. Run PLN inference rules (deduction, induction, etc.)
    3. Convert derived atoms back to WMTM candidates

    Returns candidates that the WMTM orchestrator can admit.
    """
    if engine is None:
        engine = PLNInferenceEngine()

    active_set = store.get_active_set()
    if not active_set:
        return []

    # Step 1: Convert WMTM items to PLN atoms
    atoms = wmtm_to_pln_atoms(active_set)

    # Step 2: Run PLN inference
    derived_atoms = pln_inference_over_atoms(atoms, engine=engine)

    # Step 3: Convert back to WMTM candidates
    candidates = pln_atoms_to_wmtm_candidates(derived_atoms)

    return candidates



# ─── F04: Deontic constraint propagation into PLN ─────────────────────

# Deontic status → truth value modifiers for PLN inference.
# When a GoalChainer decision labels an action with a deontic status,
# PLN atoms whose content relates to that action have their truth
# values adjusted before inference, so PLN reasoning reflects
# governance constraints.
DEONTIC_TV_MODIFIERS = {
    "forbidden": {"strength_factor": 0.1, "confidence_boost": 0.2},
    "obligated": {"strength_factor": 1.0, "confidence_boost": 0.3},
    "recommended": {"strength_factor": 0.9, "confidence_boost": 0.1},
    "permitted": {"strength_factor": 1.0, "confidence_boost": 0.0},
    "unregulated": {"strength_factor": 1.0, "confidence_boost": 0.0},
}


def apply_deontic_constraints_to_atoms(
    atoms: list[PLNAtom],
    deontic_decisions: list[dict],
    min_keyword_overlap: int = 1,
) -> list[PLNAtom]:
    """Adjust PLN atom truth values based on deontic constraints.

    For each decision with a non-neutral deontic status, find atoms whose
    names/source content overlap with the decision's action keywords, and
    adjust their truth values:

    - **forbidden**: Strength reduced to 10% (strong suppression in PLN).
      Confidence slightly boosted (we're confident it's bad).
    - **obligated**: Strength stays at 100%, confidence boosted (we're more
      certain about this action's relevance).
    - **recommended**: Slight strength preservation, minor confidence boost.

    This ensures PLN inference treats forbidden actions as unlikely/low-truth
    and obligated actions as high-confidence, propagating governance constraints
    through the reasoning chain.

    Args:
        atoms: PLN atoms to modify (modified in place and returned).
        deontic_decisions: List of decision dicts from GoalChainer,
            each with action_id, label, status fields.
        min_keyword_overlap: Minimum keyword overlap to consider a match.

    Returns:
        The same atom list (modified in place) for chaining.
    """
    if not deontic_decisions:
        return atoms

    for dec in deontic_decisions:
        status = dec.get("status", "unregulated")
        if status in ("permitted", "unregulated"):
            continue

        modifiers = DEONTIC_TV_MODIFIERS.get(status, DEONTIC_TV_MODIFIERS["unregulated"])
        keywords = set(
            w.lower() for w in
            f"{dec.get('label', '')} {dec.get('action_id', '')}".split()
            if len(w) > 3
        )
        if not keywords:
            continue

        for atom in atoms:
            atom_words = set(
                w.lower() for w in
                re.sub(r'[^a-zA-Z0-9_ ]', ' ', atom.name).split()
                if len(w) > 3
            )
            overlap = len(keywords & atom_words)
            if overlap >= min_keyword_overlap:
                old_s = atom.truth.strength
                old_c = atom.truth.confidence
                atom.truth = TruthValue(
                    strength=max(0.0, min(1.0, old_s * modifiers["strength_factor"])),
                    confidence=max(0.0, min(1.0, old_c + modifiers["confidence_boost"])),
                )

    return atoms


def run_pln_inference_over_wmtm_with_deontic(
    store: WMTMStore,
    deontic_decisions: list[dict] | None = None,
    engine: Optional[PLNInferenceEngine] = None,
) -> list[InferenceCandidate]:
    """Run PLN inference with deontic constraints applied to input atoms.

    Like run_pln_inference_over_wmtm but applies deontic truth-value
    adjustments before inference, so PLN reasoning reflects governance.

    Args:
        store: WMTM store with active set.
        deontic_decisions: Decisions from GoalChainer with deontic status.
        engine: Optional PLN engine instance.

    Returns:
        Candidates with governance-aware truth values.
    """
    if engine is None:
        engine = PLNInferenceEngine()

    active_set = store.get_active_set()
    if not active_set:
        return []

    # Step 1: Convert WMTM items to PLN atoms
    atoms = wmtm_to_pln_atoms(active_set)

    # Step 1b (F04): Apply deontic constraints to atom truth values
    if deontic_decisions:
        apply_deontic_constraints_to_atoms(atoms, deontic_decisions)

    # Step 2: Run PLN inference
    derived_atoms = pln_inference_over_atoms(atoms, engine=engine)

    # Step 3: Convert back to WMTM candidates
    candidates = pln_atoms_to_wmtm_candidates(derived_atoms)

    return candidates
