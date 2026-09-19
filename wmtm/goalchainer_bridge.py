"""GoalChainer Bridge: feeds WMTM active set to GoalChainer and admits decisions back.

This closes the feedback loop: GoalChainer reads WMTM evidence, makes a
decision, and the recommended action (with deontic status and motivation)
is converted into WMTM InferenceCandidates that re-enter working memory.

Flow:
  1. wmtm_to_goalchainer_evidence: Convert WMTM active set -> memory_items
  2. run_goalchainer: Call GoalChainer pipeline with evidence
  3. goalchainer_result_to_candidates: Convert decisions -> WMTM candidates
  4. WMTM orchestrator admits novel candidates (step 2c)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

from .store import WMTMStore
from .inference import InferenceCandidate


@dataclass
class GoalChainerDecision:
    """A single decision from GoalChainer, normalized for WMTM consumption."""
    action_id: str
    label: str
    status: str           # recommended, permitted, forbidden, obligated
    score: float
    motivation: str = ""
    evidence_ids: list = None

    def __post_init__(self):
        """Initialize evidence_ids list if not provided."""
        if self.evidence_ids is None:
            self.evidence_ids = []


def wmtm_to_goalchainer_evidence(store: WMTMStore, max_items: int = 20) -> list:
    """Convert WMTM active set into GoalChainer memory_items format.

    GoalChainer expects dicts with id, content, sti keys.
    """
    items = []
    for item in store.get_active_set()[:max_items]:
        items.append({
            'id': item.id,
            'content': item.content,
            'sti': float(item.attention.sti),
        })
    return items


def run_goalchainer(
    request: str,
    store: WMTMStore,
    max_evidence: int = 20,
) -> Optional[dict]:
    """Run the GoalChainer pipeline with WMTM evidence.

    Returns the raw GoalChainer result dict, or None on failure.
    """
    gc_dir = '/home/openclaw/research-agent/projects/omegaclaw/repos/OmegaClaw-GoalChainer/src'
    if gc_dir not in sys.path:
        sys.path.insert(0, gc_dir)

    os.environ.setdefault('GOALCHAINER_USE_HEURISTIC_PLN', '1')

    try:
        from goal_chainer.pipeline import solve_incident
    except ImportError:
        return None

    memory_items = wmtm_to_goalchainer_evidence(store, max_evidence)
    try:
        result = solve_incident(request, memory_items=memory_items if memory_items else None)
        return result
    except Exception:
        return None


def goalchainer_result_to_candidates(
    result: dict,
    cycle: int = 0,
    source_ids: Optional[list] = None,
) -> list:
    """Convert GoalChainer decisions into WMTM InferenceCandidates.

    Each ranked action becomes a candidate with:
    - content: human-readable decision summary
    - confidence: from score
    - inference_type: goalchainer_decision
    - initial_sti: proportional to score
    - derived_from: evidence IDs from WMTM
    """
    if source_ids is None:
        source_ids = []

    candidates = []
    decisions = result.get('decisions', [])

    for i, dec in enumerate(decisions):
        action_id = dec.get('action_id', f'action-{i}')
        label = dec.get('label', action_id)
        status = dec.get('status', 'unregulated')
        score = float(dec.get('score', 0.5))

        # Build content string
        content = f"GoalChainer decision: {action_id} ({label}) -- status={status}, score={score:.3f}"

        # Confidence from score (clamped to [0, 1])
        confidence = max(0.0, min(1.0, score))

        # Initial STI: recommended/obligated get boost
        sti_boost = 1.0
        if status in ('recommended', 'obligated'):
            sti_boost = 2.0
        elif status == 'forbidden':
            sti_boost = 0.3

        initial_sti = score * sti_boost * 10.0  # scale to WMTM STI range

        candidates.append(InferenceCandidate(
            content=content,
            confidence=confidence,
            derived_from=list(source_ids),
            inference_type='goalchainer_decision',
            triple=None,
            initial_sti=initial_sti,
        ))

    return candidates


def run_goalchainer_over_wmtm(
    store: WMTMStore,
    request: str,
    max_evidence: int = 20,
) -> list:
    """Full GoalChainer feedback loop over WMTM.

    1. Extract evidence from WMTM active set
    2. Run GoalChainer pipeline
    3. Convert decisions to WMTM candidates

    Returns candidates that the orchestrator can admit.
    """
    if not store.get_active_set():
        return []

    result = run_goalchainer(request, store, max_evidence)
    if result is None:
        return []

    # Collect source IDs from active set for provenance
    source_ids = [item.id for item in store.get_active_set()[:max_evidence]]

    return goalchainer_result_to_candidates(result, source_ids=source_ids)
