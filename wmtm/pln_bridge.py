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

    Preserves epistemic truth values stored on the item (tv_strength,
    tv_confidence) when available. Falls back to the default TV from
    extract_pln_atoms when no stored TV exists. Does NOT reconstruct
    truth values from attention/utility signals, which are separate
    attention-allocation metrics, not epistemic evidence.
    """
    atoms = extract_pln_atoms(item.content, source_id=item.id)

    if not atoms:
        concept_name = re.sub(r'[^a-zA-Z0-9_]', '_', item.content[:50])
        default_tv = TruthValue(strength=0.8, confidence=0.5)
        atoms = [PLNAtom(
            atom_type='Concept',
            name=f'Concept({concept_name})',
            truth=default_tv,
            source_ids=[item.id],
        )]

    # If the item has stored epistemic TV, use it (preserves PLN-derived TVs
    # through round-trips). Otherwise, the default TV from extract_pln_atoms
    # is preserved (strength=0.8, confidence=0.5).
    if item.tv_strength > 0 or item.tv_confidence > 0:
        stored_tv = TruthValue(strength=item.tv_strength, confidence=item.tv_confidence)
        for atom in atoms:
            atom.truth = stored_tv

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
