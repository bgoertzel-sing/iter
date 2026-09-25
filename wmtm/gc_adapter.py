"""GoalChainer Adapter: direct WMTM ↔ GoalChainer data model conversion.

This adapter allows running GoalChainer's DecisionEngine directly over
WMTM state, without requiring the full MeTTa/Prolog pipeline. It maps:

  WMTM Goal → GoalChainer Goal
  WMTM store items → GoalChainer CandidateAction
  WMTM attention values → GoalChainer EvidenceProjection
  GoalChainer Decision → GovernanceBridge processing

This is useful for:
- Lightweight governance decisions using WMTM attention as evidence
- Testing the full Gov Bridge flow without MeTTa runtime
- Scenarios where the full pipeline (solve_incident) is unavailable

When the full pipeline IS available, use goalchainer_bridge.run_goalchainer()
which calls solve_incident() and gets richer evidence from MeTTa reasoning.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from .goal import Goal as WMTMGoal
from .goal_store import GoalStore
from .store import WMTMStore

logger = logging.getLogger(__name__)

# Path to GoalChainer source
GC_SRC_DIR = '/home/openclaw/research-agent/projects/omegaclaw/repos/OmegaClaw-GoalChainer/src'


def _ensure_gc() -> bool:
    """Add GoalChainer src to sys.path if available."""
    if os.path.isdir(GC_SRC_DIR):
        if GC_SRC_DIR not in sys.path:
            sys.path.insert(0, GC_SRC_DIR)
        return True
    logger.warning("GoalChainer source not found at %s", GC_SRC_DIR)
    return False


class WMTMEvidenceReasoner:
    """A GoalChainer-compatible reasoner that projects evidence from WMTM state.

    GoalChainer's DecisionEngine expects a reasoner with a .project(action)
    method returning an EvidenceProjection. This reasoner derives evidence
    strength and confidence from WMTM item attention values.

    Mapping:
    - strength: normalized STI of matching items (0-1 range)
    - confidence: fraction of action's evidence_atoms found in active set
    - deontic: derived from norm matching against WMTM content
    """

    def __init__(
        self,
        store: WMTMStore,
        norms: list = None,
        max_sti: float = 100.0,
    ) -> None:
        self.store = store
        self.norms = norms or []
        self.max_sti = max(max_sti, 1.0)
        # Pre-index active items for matching
        self._active_index = self._build_index()

    def _build_index(self) -> dict:
        """Build keyword→items index from active set for fast matching."""
        index = {}
        for item in self.store.get_active_set():
            for word in item.content.lower().split():
                if len(word) > 3:
                    index.setdefault(word, []).append(item)
        return index

    def project(self, action) -> object:
        """Project evidence for a CandidateAction from WMTM state.

        Returns a GoalChainer EvidenceProjection.
        """
        if not _ensure_gc():
            return self._fallback_projection(action)

        from goal_chainer.models import EvidenceProjection

        # Calculate strength from matching item attention
        matching_items = self._find_matching_items(action)
        if matching_items:
            avg_sti = sum(it.attention.sti for it in matching_items) / len(matching_items)
            strength = min(1.0, max(0.0, avg_sti / self.max_sti))
        else:
            strength = action.default_strength

        # Confidence: what fraction of evidence atoms are present
        atoms_found = 0
        for atom in action.evidence_atoms:
            atom_lower = atom.lower()
            for word in atom_lower.split():
                if word in self._active_index:
                    atoms_found += 1
                    break
        confidence = atoms_found / max(len(action.evidence_atoms), 1)
        confidence = max(confidence, action.default_confidence * 0.5)  # floor

        # Deontic status from norms
        deontic = self._resolve_deontic(action)

        # Expectation: combined weight
        expectation = strength * confidence

        return EvidenceProjection(
            strength=strength,
            confidence=confidence,
            source="wmtm-attention",
            projection=f"wmtm-{len(matching_items)}-items",
            proofs=tuple(it.id for it in matching_items[:5]),
            deontic=deontic,
            expectation=expectation,
        )

    def _find_matching_items(self, action) -> list:
        """Find WMTM items that match a CandidateAction's context."""
        keywords = set()
        for word in action.label.lower().split():
            if len(word) > 3:
                keywords.add(word)
        for word in action.description.lower().split():
            if len(word) > 3:
                keywords.add(word)

        seen_ids = set()
        matches = []
        for kw in keywords:
            for item in self._active_index.get(kw, []):
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    matches.append(item)
        return matches

    def _resolve_deontic(self, action) -> str:
        """Resolve deontic status for an action from norms."""
        for norm in self.norms:
            target = getattr(norm, 'target_action', '')
            if target == action.id:
                mode = getattr(norm, 'mode', 'permit')
                return {
                    'oblige': 'obligated',
                    'permit': 'permitted',
                    'forbid': 'forbidden',
                }.get(mode, 'unregulated')
        return 'unregulated'

    def _fallback_projection(self, action):
        """Return a basic dict projection when GoalChainer is not importable."""
        return type('EP', (), {
            'strength': action.default_strength,
            'confidence': action.default_confidence,
            'source': 'wmtm-fallback',
            'projection': None,
            'proofs': (),
            'deontic': 'unregulated',
            'expectation': action.default_strength * action.default_confidence,
        })()


def wmtm_goal_to_gc_goal(goal: WMTMGoal, kind: str = "individual") -> object:
    """Convert a WMTM Goal to a GoalChainer Goal.

    WMTM goals have: id, description, status, priority, created_cycle, ...
    GoalChainer goals have: id, owner, statement, weight, kind, required
    """
    if not _ensure_gc():
        return None

    from goal_chainer.models import Goal as GCGoal

    return GCGoal(
        id=goal.id,
        owner="wmtm",
        statement=goal.description,
        weight=float(goal.priority),
        kind=kind,
        required=(goal.priority >= 0.8),  # high-priority (>=0.8 on 0-1 scale) = required
    )


def wmtm_items_to_actions(
    store: WMTMStore,
    max_items: int = 10,
) -> list:
    """Convert top WMTM items into GoalChainer CandidateActions.

    Each high-attention WMTM item becomes a candidate action that
    GoalChainer can evaluate and rank.
    """
    if not _ensure_gc():
        return []

    from goal_chainer.models import CandidateAction

    items = store.get_active_set()[:max_items]
    actions = []

    for item in items:
        # Normalize STI to [0, 1] for default_strength
        max_sti = max(it.attention.sti for it in items) if items else 100.0
        strength = min(1.0, item.attention.sti / max(max_sti, 1.0))

        action = CandidateAction(
            id=f"wmtm-{item.id}",
            label=item.content[:60],
            description=item.content,
            satisfies=(),  # filled in by scenario builder
            evidence_query=item.content,
            evidence_atoms=tuple(w.lower() for w in item.content.split() if len(w) > 3),
            default_strength=max(0.01, min(0.99, strength)),
            default_confidence=0.8,
        )
        actions.append(action)

    return actions


@dataclass
class AdapterResult:
    """Result from running GoalChainer via the adapter."""
    decisions: list = field(default_factory=list)
    scenario_title: str = ""
    goal_ids: list = field(default_factory=list)
    action_count: int = 0
    evidence_source: str = "wmtm-attention"

    def to_gc_result_dict(self) -> dict:
        """Convert to the dict format expected by GovernanceBridge.process_goalchainer_result."""
        return {
            "request": self.scenario_title,
            "decided": self.decisions[0].action_id if self.decisions else "",
            "label": self.decisions[0].label if self.decisions else "",
            "status": self.decisions[0].status if self.decisions else "weak",
            "decisions": [d.to_dict() for d in self.decisions],
            "motivation": {"available": False, "source": self.evidence_source},
            "incident": {},
            "executed": {},
        }


def run_goalchainer_direct(
    store: WMTMStore,
    goal_store: Optional[GoalStore] = None,
    title: str = "wmtm-governance-cycle",
    norms: list = None,
    max_actions: int = 10,
) -> Optional[AdapterResult]:
    """Run GoalChainer's DecisionEngine directly over WMTM state.

    This bypasses the full pipeline (solve_incident) and instead:
    1. Converts WMTM goals to GoalChainer goals
    2. Converts WMTM items to CandidateActions
    3. Uses WMTMEvidenceReasoner to project evidence from attention
    4. Calls DecisionEngine.rank() to get ranked decisions

    Returns an AdapterResult, or None if GoalChainer is unavailable.
    """
    if not _ensure_gc():
        return None

    from goal_chainer.models import GoalScenario, Norm
    from goal_chainer.scoring import DecisionEngine

    # 1. Convert WMTM goals
    gc_goals = []
    goal_ids = []
    if goal_store:
        for wg in goal_store.active():
            gc_goal = wmtm_goal_to_gc_goal(wg)
            if gc_goal:
                gc_goals.append(gc_goal)
                goal_ids.append(wg.id)

    # 2. Convert norms
    gc_norms = []
    if norms:
        for n in norms:
            if isinstance(n, dict):
                gc_norms.append(Norm(
                    id=n.get("id", "norm-0"),
                    mode=n.get("mode", "permit"),
                    target_action=n.get("target_action", ""),
                    reason=n.get("reason", ""),
                    priority=n.get("priority", 0),
                ))
            else:
                gc_norms.append(n)

    # 3. Convert WMTM items to actions
    gc_actions = wmtm_items_to_actions(store, max_actions)
    if not gc_actions:
        return AdapterResult(
            scenario_title=title,
            goal_ids=goal_ids,
        )

    # Link actions to goals (each action satisfies goals whose keywords match)
    if gc_goals:
        gc_actions = _link_actions_to_goals(gc_actions, gc_goals, store)

    # 4. Build scenario
    scenario = GoalScenario(
        title=title,
        goals=tuple(gc_goals),
        norms=tuple(gc_norms),
        actions=tuple(gc_actions),
    )

    # 5. Create reasoner and rank
    reasoner = WMTMEvidenceReasoner(store, norms=gc_norms)
    engine = DecisionEngine(reasoner)
    decisions = engine.rank(scenario)

    return AdapterResult(
        decisions=decisions,
        scenario_title=title,
        goal_ids=goal_ids,
        action_count=len(gc_actions),
        evidence_source="wmtm-attention",
    )


def _link_actions_to_goals(actions: list, goals: list, store: WMTMStore) -> list:
    """Link CandidateActions to Goals based on keyword overlap.

    Returns new CandidateAction instances with updated satisfies tuples.
    """
    if not _ensure_gc():
        return actions

    from goal_chainer.models import CandidateAction

    goal_keywords = {}
    for g in goals:
        goal_keywords[g.id] = set(
            w.lower() for w in g.statement.split() if len(w) > 3
        )

    linked = []
    for action in actions:
        action_words = set(
            w.lower() for w in action.description.split() if len(w) > 3
        )
        satisfies = []
        for gid, gkw in goal_keywords.items():
            if action_words & gkw:
                satisfies.append(gid)

        linked.append(CandidateAction(
            id=action.id,
            label=action.label,
            description=action.description,
            satisfies=tuple(satisfies),
            evidence_query=action.evidence_query,
            evidence_atoms=action.evidence_atoms,
            default_strength=action.default_strength,
            default_confidence=action.default_confidence,
        ))
    return linked
