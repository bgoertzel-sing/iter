"""GoalChainer Bridge: feeds WMTM active set to GoalChainer and admits decisions back.

This closes the feedback loop: GoalChainer reads WMTM evidence, makes a
decision, and the recommended action (with deontic status and motivation)
is converted into WMTM InferenceCandidates that re-enter working memory.

F04 integration: results now flow through GovernanceBridge for deontic
enforcement and goal lifecycle tracking before being admitted to WMTM.

Flow:
  1. wmtm_to_goalchainer_evidence: Convert WMTM active set -> memory_items
  2. run_goalchainer: Call GoalChainer pipeline with evidence
  3. GovernanceBridge.process_result: enforce deontic constraints, update goals
  4. goalchainer_result_to_candidates: Convert decisions -> WMTM candidates
  5. WMTM orchestrator admits novel candidates (step 2c)
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

from .store import WMTMStore
from .inference import InferenceCandidate

logger = logging.getLogger(__name__)

# Path to GoalChainer source — resolved at import time
GC_SRC_DIR = '/home/openclaw/research-agent/projects/omegaclaw/repos/OmegaClaw-GoalChainer/src'


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

    @classmethod
    def from_pipeline_dict(cls, dec: dict, index: int = 0) -> "GoalChainerDecision":
        """Create from a GoalChainer pipeline decision dict."""
        return cls(
            action_id=dec.get('action_id', f'action-{index}'),
            label=dec.get('label', dec.get('action_id', f'action-{index}')),
            status=dec.get('status', 'unregulated'),
            score=float(dec.get('score', 0.5)),
            motivation=dec.get('motivation', ''),
            evidence_ids=dec.get('evidence_ids', []),
        )


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


def _ensure_gc_path() -> bool:
    """Add GoalChainer src to sys.path if it exists. Returns True if available."""
    if os.path.isdir(GC_SRC_DIR):
        if GC_SRC_DIR not in sys.path:
            sys.path.insert(0, GC_SRC_DIR)
        return True
    logger.warning("GoalChainer source not found at %s", GC_SRC_DIR)
    return False


def run_goalchainer(
    request: str,
    store: WMTMStore,
    max_evidence: int = 20,
) -> Optional[dict]:
    """Run the GoalChainer pipeline with WMTM evidence.

    Returns the raw GoalChainer result dict, or None on failure.
    """
    if not _ensure_gc_path():
        return None

    os.environ.setdefault('GOALCHAINER_USE_HEURISTIC_PLN', '1')

    try:
        from goal_chainer.pipeline import solve_incident
    except ImportError:
        logger.warning("Could not import goal_chainer.pipeline — GoalChainer not available")
        return None

    memory_items = wmtm_to_goalchainer_evidence(store, max_evidence)
    try:
        result = solve_incident(request, memory_items=memory_items if memory_items else None)
        logger.info("GoalChainer returned %d decisions for request: %s",
                     len(result.get('decisions', [])), request[:80])
        return result
    except Exception as exc:
        logger.error("GoalChainer pipeline failed: %s", exc)
        return None


def parse_decisions(result: dict) -> list:
    """Parse GoalChainer result into GoalChainerDecision objects."""
    decisions = []
    for i, dec in enumerate(result.get('decisions', [])):
        decisions.append(GoalChainerDecision.from_pipeline_dict(dec, i))
    return decisions


def goalchainer_result_to_candidates(
    result: dict,
    cycle: int = 0,
    source_ids: Optional[list] = None,
    deontic_adjustments: Optional[dict] = None,
) -> list:
    """Convert GoalChainer decisions into WMTM InferenceCandidates.

    Each ranked action becomes a candidate with:
    - content: human-readable decision summary
    - confidence: from score
    - inference_type: goalchainer_decision
    - initial_sti: proportional to score, adjusted by deontic enforcement
    - derived_from: evidence IDs from WMTM

    Args:
        deontic_adjustments: optional dict of {action_id: sti_multiplier}
            from GovernanceBridge enforcement. If provided, overrides
            the default status-based STI boost.
    """
    if source_ids is None:
        source_ids = []
    if deontic_adjustments is None:
        deontic_adjustments = {}

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

        # STI calculation: use deontic adjustment if available, else default
        if action_id in deontic_adjustments:
            sti_multiplier = deontic_adjustments[action_id]
        else:
            # Default status-based boost
            sti_multiplier = 1.0
            if status in ('recommended', 'obligated'):
                sti_multiplier = 2.0
            elif status == 'forbidden':
                sti_multiplier = 0.3

        initial_sti = score * sti_multiplier * 10.0  # scale to WMTM STI range

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
    gov_bridge=None,
    goal_id: str = "",
    cycle: int = 0,
) -> list:
    """Full GoalChainer feedback loop over WMTM.

    1. Extract evidence from WMTM active set
    2. Run GoalChainer pipeline
    3. (F04) Process through GovernanceBridge if available
    4. Convert decisions to WMTM candidates

    Returns candidates that the orchestrator can admit.
    """
    if not store.get_active_set():
        return []

    result = run_goalchainer(request, store, max_evidence)
    if result is None:
        return []

    # Collect source IDs from active set for provenance
    source_ids = [item.id for item in store.get_active_set()[:max_evidence]]

    # F04: Process through governance bridge if available
    deontic_adjustments = {}
    if gov_bridge is not None:
        gov_result = gov_bridge.process_result(
            result, store, goal_id=goal_id, cycle=cycle,
        )
        # Convert enforcement actions to STI multipliers for candidates
        for record in gov_result.get('records', []):
            aid = record.decision_action_id
            status = record.deontic_status
            if status == 'forbidden':
                deontic_adjustments[aid] = 0.1  # heavy suppression
            elif status == 'obligated':
                deontic_adjustments[aid] = 3.0  # strong boost
            elif status == 'recommended':
                deontic_adjustments[aid] = 2.0
            # permitted/unregulated: no override, use default

    return goalchainer_result_to_candidates(
        result, cycle=cycle, source_ids=source_ids,
        deontic_adjustments=deontic_adjustments,
    )
