"""Governance Bridge (F04): deontic enforcement + goal-decision tracking.

The Gov Bridge sits between GoalChainer decisions and WMTM, enforcing
governance constraints:

1. **GoalDecisionRecord** — tracks which goals produced which decisions,
   with execution outcomes for accountability.
2. **DeonticEnforcer** — modifies WMTM attention based on deontic status:
   forbidden → suppress STI, obligated → boost STI.
3. **GovernanceBridge** — coordinates the full governance loop:
   GoalChainer result → deontic enforcement → goal lifecycle update →
   decision history tracking.

Flow (called from orchestrator after _run_goalchainer_phase):
  1. process_result: Parse GoalChainer result, create decision records
  2. enforce_deontic: Apply STI penalties/boosts to WMTM items
  3. update_goal_lifecycle: Achieve/block/abandon goals based on outcomes
  4. Decision history is persisted for audit
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .goal import Goal
from .goal_store import GoalStore
from .store import WMTMStore


# ── Data structures ────────────────────────────────────────────────


@dataclass
class GoalDecisionRecord:
    """Links a goal to a GoalChainer decision and its execution outcome.

    This is the accountability trail: which goal triggered which decision,
    what deontic status it carried, and whether execution succeeded.
    """
    id: str = field(default_factory=lambda: f"gdr-{uuid.uuid4().hex[:8]}")
    goal_id: str = ""
    decision_action_id: str = ""
    decision_label: str = ""
    deontic_status: str = "unregulated"   # recommended/permitted/forbidden/obligated
    score: float = 0.0
    execution_result: Optional[str] = None  # success/failure/pending/skipped
    execution_detail: str = ""
    cycle: int = 0
    timestamp: float = field(default_factory=time.time)
    satisfied_goals: list = field(default_factory=list)
    missing_required_goals: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal_id": self.goal_id,
            "decision_action_id": self.decision_action_id,
            "decision_label": self.decision_label,
            "deontic_status": self.deontic_status,
            "score": self.score,
            "execution_result": self.execution_result,
            "execution_detail": self.execution_detail,
            "cycle": self.cycle,
            "timestamp": self.timestamp,
            "satisfied_goals": list(self.satisfied_goals),
            "missing_required_goals": list(self.missing_required_goals),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GoalDecisionRecord":
        return cls(
            id=data["id"],
            goal_id=data.get("goal_id", ""),
            decision_action_id=data.get("decision_action_id", ""),
            decision_label=data.get("decision_label", ""),
            deontic_status=data.get("deontic_status", "unregulated"),
            score=data.get("score", 0.0),
            execution_result=data.get("execution_result"),
            execution_detail=data.get("execution_detail", ""),
            cycle=data.get("cycle", 0),
            timestamp=data.get("timestamp", 0.0),
            satisfied_goals=data.get("satisfied_goals", []),
            missing_required_goals=data.get("missing_required_goals", []),
        )


# ── Decision History Store ─────────────────────────────────────────


class DecisionHistory:
    """Persistent store for GoalDecisionRecords.

    Provides query by goal, by cycle range, and by outcome for audit
    and governance analysis.
    """

    def __init__(self, path: str, auto_save: bool = True) -> None:
        self.path = path
        self.auto_save = auto_save
        self._records: list[GoalDecisionRecord] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                data = json.load(f)
            for rdict in data.get("records", []):
                self._records.append(GoalDecisionRecord.from_dict(rdict))

    def save(self) -> None:
        data = {
            "records": [r.to_dict() for r in self._records],
            "count": len(self._records),
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)

    def add(self, record: GoalDecisionRecord) -> GoalDecisionRecord:
        self._records.append(record)
        if self.auto_save:
            self.save()
        return record

    def by_goal(self, goal_id: str) -> list[GoalDecisionRecord]:
        return [r for r in self._records if r.goal_id == goal_id]

    def by_cycle_range(self, start: int, end: int) -> list[GoalDecisionRecord]:
        return [r for r in self._records if start <= r.cycle <= end]

    def by_outcome(self, outcome: str) -> list[GoalDecisionRecord]:
        return [r for r in self._records if r.execution_result == outcome]

    def recent(self, n: int = 10) -> list[GoalDecisionRecord]:
        return self._records[-n:]

    def all(self) -> list[GoalDecisionRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)


# ── Deontic Enforcer ───────────────────────────────────────────────


class DeonticEnforcer:
    """Applies deontic constraints from GoalChainer decisions to WMTM attention.

    Deontic statuses and their effects on WMTM items whose content
    matches the decision context:

    - **forbidden**: Suppress STI by penalty_factor (default 0.2x).
      Items related to forbidden actions lose attention rapidly.
    - **obligated**: Boost STI by boost_factor (default 3.0x).
      Items related to obligated actions gain attention priority.
    - **recommended**: Moderate boost (default 1.5x).
    - **permitted**: No modification (neutral).
    - **unregulated**: No modification.

    Matching is keyword-based: decision label/action_id words are compared
    against WMTM item content words.
    """

    def __init__(
        self,
        forbidden_penalty: float = 0.2,
        obligated_boost: float = 3.0,
        recommended_boost: float = 1.5,
        min_keyword_overlap: int = 1,
    ) -> None:
        self.forbidden_penalty = forbidden_penalty
        self.obligated_boost = obligated_boost
        self.recommended_boost = recommended_boost
        self.min_keyword_overlap = min_keyword_overlap

    def enforce(
        self,
        store: WMTMStore,
        decisions: list[dict],
    ) -> dict:
        """Apply deontic adjustments to WMTM items based on decisions.

        Args:
            store: The WMTM store whose items may be adjusted.
            decisions: List of decision dicts from GoalChainer
                       (each has action_id, label, status, score).

        Returns:
            Summary dict with counts of items boosted/suppressed.
        """
        summary = {"boosted": 0, "suppressed": 0, "unchanged": 0}

        for dec in decisions:
            status = dec.get("status", "unregulated")
            if status in ("permitted", "unregulated"):
                continue

            keywords = self._extract_keywords(dec)
            if not keywords:
                continue

            factor = self._status_factor(status)

            for item in store.get_active_set():
                item_words = set(w.lower() for w in item.content.split() if len(w) > 3)
                overlap = len(keywords & item_words)
                if overlap >= self.min_keyword_overlap:
                    old_sti = item.attention.sti
                    if factor > 1.0:
                        item.attention.boost(factor * overlap)
                        summary["boosted"] += 1
                    elif factor < 1.0:
                        # Suppress: reduce STI proportionally
                        penalty = old_sti * (1.0 - factor)
                        item.attention.sti = max(0.0, old_sti - penalty)
                        summary["suppressed"] += 1

        return summary

    def _extract_keywords(self, decision: dict) -> set:
        """Extract meaningful keywords from a decision for matching."""
        text = f"{decision.get('label', '')} {decision.get('action_id', '')}"
        return set(w.lower() for w in text.split() if len(w) > 3)

    def _status_factor(self, status: str) -> float:
        """Map deontic status to an STI modification factor."""
        return {
            "forbidden": self.forbidden_penalty,
            "obligated": self.obligated_boost,
            "recommended": self.recommended_boost,
            "permitted": 1.0,
            "unregulated": 1.0,
        }.get(status, 1.0)


# ── Governance Bridge ──────────────────────────────────────────────


class GovernanceBridge:
    """The main F04 Gov Bridge: coordinates deontic enforcement,
    goal-decision tracking, and goal lifecycle updates.

    Usage:
        gov = GovernanceBridge(goal_store, decision_history)
        result = gov.process_goalchainer_result(gc_result, store, cycle)
    """

    def __init__(
        self,
        goal_store: GoalStore,
        decision_history: Optional[DecisionHistory] = None,
        enforcer: Optional[DeonticEnforcer] = None,
        history_path: str = "",
    ) -> None:
        self.goal_store = goal_store
        self.history = decision_history or DecisionHistory(
            path=history_path or os.path.join(
                os.path.dirname(goal_store.path), "decision_history.json"
            )
        )
        self.enforcer = enforcer or DeonticEnforcer()

    def process_goalchainer_result(
        self,
        gc_result: dict,
        store: WMTMStore,
        cycle: int,
        goal_id: str = "",
    ) -> dict:
        """Process a GoalChainer result through the full governance pipeline.

        Steps:
          1. Parse decisions and create GoalDecisionRecords
          2. Apply deontic enforcement to WMTM
          3. Process execution results
          4. Update goal lifecycle based on outcomes

        Args:
            gc_result: Raw result dict from solve_incident.
            store: WMTM store for deontic enforcement.
            cycle: Current orchestration cycle number.
            goal_id: ID of the goal that triggered this GoalChainer run.

        Returns:
            Summary dict with records created, enforcement results, and
            goal lifecycle changes.
        """
        if not gc_result:
            return {"records": 0, "enforcement": {}, "lifecycle_changes": []}

        decisions = gc_result.get("decisions", [])
        executed = gc_result.get("executed", {})
        decided_action = gc_result.get("decided", "")

        # 1. Create decision records
        records = self._create_decision_records(
            decisions, executed, decided_action, goal_id, cycle
        )

        # 2. Apply deontic enforcement to WMTM
        enforcement = self.enforcer.enforce(store, decisions)

        # 3. Update goal lifecycle based on execution outcome
        lifecycle_changes = self._update_goal_lifecycle(
            records, goal_id, cycle
        )

        return {
            "records": len(records),
            "enforcement": enforcement,
            "lifecycle_changes": lifecycle_changes,
            "decided_action": decided_action,
        }

    def _create_decision_records(
        self,
        decisions: list[dict],
        executed: dict,
        decided_action: str,
        goal_id: str,
        cycle: int,
    ) -> list[GoalDecisionRecord]:
        """Create GoalDecisionRecords for each decision in the result."""
        records = []
        for dec in decisions:
            action_id = dec.get("action_id", "")
            is_executed = (action_id == decided_action)

            # Determine execution result for the decided action
            exec_result = "skipped"
            exec_detail = ""
            if is_executed and executed:
                if isinstance(executed, dict):
                    exec_result = "success" if executed.get("success", False) else "failure"
                    exec_detail = str(executed.get("detail", executed.get("error", "")))
                elif isinstance(executed, str):
                    exec_result = "success" if executed else "failure"
                    exec_detail = executed

            record = GoalDecisionRecord(
                goal_id=goal_id,
                decision_action_id=action_id,
                decision_label=dec.get("label", ""),
                deontic_status=dec.get("status", "unregulated"),
                score=dec.get("score", 0.0),
                execution_result=exec_result if is_executed else "skipped",
                execution_detail=exec_detail if is_executed else "",
                cycle=cycle,
                satisfied_goals=dec.get("satisfied_goals", []),
                missing_required_goals=dec.get("missing_required_goals", []),
            )
            self.history.add(record)
            records.append(record)

        return records

    def _update_goal_lifecycle(
        self,
        records: list[GoalDecisionRecord],
        goal_id: str,
        cycle: int,
    ) -> list[dict]:
        """Update goal status based on decision outcomes.

        Rules:
        - If the executed decision succeeded and satisfied the goal → achieve
        - If the executed decision failed and had missing_required_goals → block
        - If consecutive failures exceed threshold → abandon
        - Expiry is handled separately by the orchestrator
        """
        changes = []
        if not goal_id:
            return changes

        goal = self.goal_store.get(goal_id)
        if goal is None or not goal.is_actionable():
            return changes

        # Find the executed decision record
        executed_record = None
        for r in records:
            if r.execution_result not in ("skipped", None):
                executed_record = r
                break

        if executed_record is None:
            return changes

        # Check for goal achievement
        if (executed_record.execution_result == "success"
                and goal_id in executed_record.satisfied_goals):
            goal.achieve(cycle)
            self.goal_store.update(goal)
            changes.append({
                "goal_id": goal_id,
                "old_status": "active",
                "new_status": "achieved",
                "reason": f"Decision {executed_record.decision_action_id} succeeded "
                          f"and satisfied goal",
                "cycle": cycle,
            })
            return changes

        # Check for blocking (missing required goals)
        if (executed_record.execution_result == "failure"
                and executed_record.missing_required_goals):
            goal.block()
            self.goal_store.update(goal)
            changes.append({
                "goal_id": goal_id,
                "old_status": goal.status,
                "new_status": "blocked",
                "reason": f"Missing required goals: "
                          f"{', '.join(executed_record.missing_required_goals)}",
                "cycle": cycle,
            })
            return changes

        # Check for consecutive failures → abandon
        # Only count executed (non-skipped) records for consecutive failures
        recent = self.history.by_goal(goal_id)
        consecutive_failures = 0
        for r in reversed(recent):
            if r.execution_result == "skipped":
                continue  # skip non-executed decisions
            if r.execution_result == "failure":
                consecutive_failures += 1
            else:
                break
        if consecutive_failures >= 3:
            goal.abandon()
            self.goal_store.update(goal)
            changes.append({
                "goal_id": goal_id,
                "old_status": "active",
                "new_status": "abandoned",
                "reason": f"{consecutive_failures} consecutive failures",
                "cycle": cycle,
            })

        return changes

    def goal_decision_summary(self, goal_id: str) -> dict:
        """Return a summary of all decisions made for a goal.

        Useful for governance audit and reporting.
        """
        records = self.history.by_goal(goal_id)
        if not records:
            return {"goal_id": goal_id, "total": 0}

        outcomes = {}
        for r in records:
            key = r.execution_result or "pending"
            outcomes[key] = outcomes.get(key, 0) + 1

        return {
            "goal_id": goal_id,
            "total": len(records),
            "outcomes": outcomes,
            "last_decision": records[-1].to_dict(),
            "deontic_statuses": list(set(r.deontic_status for r in records)),
        }
