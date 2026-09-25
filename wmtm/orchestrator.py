"""WMTMOrchestrator: ties all WMTM components into a single cycle.

One orchestration cycle:
1. Recall from LTM (via recall_bridge) into WMTM
2. Run inference engine over active set
2b. Run PLN inference over active set (optional, default on)
3. Admit derived candidates (if novel)
4. Tick: decay attention, age items
5. Track utility (use/miss)
6. Apply forgetting policy (evict low-attention/low-utility)
7. Writeback high-utility items to LTM

The orchestrator is designed to be called once per agent step.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .store import WMTMStore
from .inference import WMTMInferenceEngine
from .utility import UtilityTracker
from .forgetting_log import ForgettingLog
from .forgetting import ForgettingPolicy
from .writeback import WritebackManager
from .goal_store import GoalStore
from .gov_bridge import GovernanceBridge, DecisionHistory


@dataclass
class CycleResult:
    """Summary of a single orchestration cycle."""
    cycle: int
    admitted_derived: int = 0
    pln_admitted: int = 0
    gc_admitted: int = 0
    evicted: int = 0
    written_back: int = 0
    contradiction_evictions: int = 0
    active_count: int = 0
    contradictions: list = field(default_factory=list)
    resolutions: list = field(default_factory=list)
    forgetting_log_size: int = 0
    gov_enforcement: dict = field(default_factory=dict)
    gov_lifecycle_changes: list = field(default_factory=list)


class WMTMOrchestrator:
    """Coordinates the full WMTM cycle: recall -> infer -> forget -> writeback.

    Usage:
        orch = WMTMOrchestrator(store)
        result = orch.cycle(recall_fn=None, append_fn=petta_append)
    """

    def __init__(
        self,
        store: WMTMStore,
        inference_engine: Optional[WMTMInferenceEngine] = None,
        utility_tracker: Optional[UtilityTracker] = None,
        forgetting_policy: Optional[ForgettingPolicy] = None,
        forgetting_log: Optional[ForgettingLog] = None,
        writeback_manager: Optional[WritebackManager] = None,
        use_pln: bool = True,
        use_goalchainer: bool = False,
        goalchainer_request: str = '',
        goal_store: Optional[GoalStore] = None,
        gov_bridge: Optional[GovernanceBridge] = None,
    ) -> None:
        """Initialize the WMTM orchestrator with a store and default forgetting policy.

        Args:
            use_pln: if True, run PLN inference (via pln_bridge) in each cycle.
            gov_bridge: if provided, enables F04 governance enforcement.
        """
        self.store = store
        self.engine = inference_engine or WMTMInferenceEngine()
        self.utility = utility_tracker or UtilityTracker()
        self.forgetting = forgetting_policy or ForgettingPolicy()
        self.forget_log = forgetting_log or ForgettingLog()
        self.forget_log_override_threshold = 500.0  # only re-derive forgotten content if exceptionally high attention
        self.writeback = writeback_manager or WritebackManager()
        self.use_pln = use_pln
        self.use_goalchainer = use_goalchainer
        self.goalchainer_request = goalchainer_request
        self.goal_store = goal_store
        self.gov_bridge = gov_bridge
        self._cycle = 0
        self._last_deontic_decisions: list = []  # F04: cached for PLN deontic propagation


    def active_goal(self):
        """Return the highest-priority active goal, or None."""
        if self.goal_store is None:
            return None
        active = self.goal_store.active()
        return active[0] if active else None

    # ── F01: State persistence for subprocess boundaries ───────────

    def snapshot_state(self) -> dict:
        """Serialize orchestrator state for persistence across subprocess restarts.

        F01: WMTM state (store items, attention values, cycle count) is lost when
        the agent process restarts. This method captures a JSON-serializable
        snapshot that can be restored via restore_state().
        """
        return {
            "cycle": self._cycle,
            "store": self.store.to_dict(),
            "goal_store_path": self.goal_store.path if self.goal_store is not None else None,
        }

    def restore_state(self, snapshot: dict) -> None:
        """Restore orchestrator state from a snapshot (F01).

        Args:
            snapshot: dict from snapshot_state(), or empty dict for fresh start.
        """
        if not snapshot:
            return
        self._cycle = snapshot.get("cycle", 0)
        if "store" in snapshot:
            from .store import WMTMStore
            self.store = WMTMStore.from_dict(snapshot["store"])

    # ── Phase helpers ──────────────────────────────────────────────

    def _recall_from_ltm(
        self,
        recall_fn: Optional[Callable[[], list[tuple[str, str, float]]]],
    ) -> None:
        """Pull items from LTM into the store via recall_fn."""
        if recall_fn is None:
            return
        for item_id, content, sti in recall_fn():
            self.store.admit(
                item_id=item_id,
                content=content,
                source_type='recalled',
                initial_sti=sti,
            )
        self._log_capacity_evictions()

    def _log_capacity_evictions(self) -> None:
        """Record any items evicted by capacity pressure since last drain."""
        for ev in self.store.drain_pending_evicted():
            self.forget_log.record(ev, self._cycle)


    def _goal_context_boost(self) -> None:
        """Boost STI for items whose content matches the active goal description."""
        goal = self.active_goal()
        if goal is None:
            return
        keywords = set(w.lower() for w in goal.description.split() if len(w) > 3)
        for item in self.store.get_active_set():
            item_words = set(w.lower() for w in item.content.split() if len(w) > 3)
            overlap = len(keywords & item_words)
            if overlap > 0:
                boost = 2.0 * overlap / max(len(keywords), 1)
                item.attention.boost(boost)

    def _admit_derived(self, candidates: list) -> list:
        """Admit novel, non-forgotten derived candidates; return admitted items."""
        admitted = []
        for i, cand in enumerate(candidates):
            if self._is_skipworthy(cand):
                continue
            item = self._admit_candidate(cand, i)
            if item is not None:
                admitted.append(item)
                for pid in cand.derived_from:
                    self.utility.record_inferred_from(pid)
        self._log_capacity_evictions()
        return admitted

    def _is_skipworthy(self, cand) -> bool:
        """Check if a candidate should be skipped (forgotten or duplicate)."""
        if self.forget_log.is_forgotten(cand.content):
            if cand.initial_sti < self.forget_log_override_threshold:
                return True
        if not self.engine.is_novel(cand, self.store):
            return True
        return False

    def _admit_candidate(self, cand, index: int) -> None:
        """Admit a single derived candidate into the store."""
        item_id = f"derived-{cand.inference_type}-{self._cycle}-{index}"
        return self.store.admit(
            item_id=item_id,
            content=cand.content,
            source_type='derived',
            derived_from=cand.derived_from,
            initial_sti=cand.initial_sti,
        )

    def _run_pln_phase(self, deontic_decisions: list = None) -> list:
        """Run PLN inference over the WMTM active set and admit candidates.

        When a GovernanceBridge is active (F04), deontic constraints from
        GoalChainer decisions are propagated into PLN atom truth values
        before inference, so PLN reasoning reflects governance.

        Returns the list of admitted PLN-derived items.
        """
        if not self.use_pln:
            return []
        try:
            if deontic_decisions and self.gov_bridge is not None:
                from .pln_bridge import run_pln_inference_over_wmtm_with_deontic
                pln_candidates = run_pln_inference_over_wmtm_with_deontic(
                    self.store, deontic_decisions=deontic_decisions
                )
            else:
                from .pln_bridge import run_pln_inference_over_wmtm
                pln_candidates = run_pln_inference_over_wmtm(self.store)
            if not pln_candidates:
                return []
            return self._admit_derived(pln_candidates)
        except Exception:
            return []

    def _run_goalchainer_phase(self, result: CycleResult) -> list:
        """Run GoalChainer over the WMTM active set and admit decisions.

        This is the feedback loop: GoalChainer reads WMTM evidence,
        makes a decision, and the results are admitted back into WMTM.
        When a GovernanceBridge is configured (F04), decisions also go
        through deontic enforcement and goal-lifecycle updates.

        Returns the list of admitted GoalChainer-derived items.
        """
        if not self.use_goalchainer or not self.goalchainer_request:
            return []
        try:
            from .goalchainer_bridge import run_goalchainer, goalchainer_result_to_candidates

            gc_result = run_goalchainer(self.goalchainer_request, self.store)
            if gc_result is None:
                return []

            # F04: Run governance bridge if configured
            if self.gov_bridge is not None:
                goal = self.active_goal()
                goal_id = goal.id if goal else ""
                gov_out = self.gov_bridge.process_goalchainer_result(
                    gc_result, self.store, self._cycle, goal_id=goal_id
                )
                result.gov_enforcement = gov_out.get("enforcement", {})
                result.gov_lifecycle_changes = gov_out.get("lifecycle_changes", [])

            # F04: Cache deontic decisions for PLN propagation
            self._last_deontic_decisions = gc_result.get("decisions", [])

            # Convert decisions to WMTM candidates and admit
            source_ids = [item.id for item in self.store.get_active_set()[:20]]
            gc_candidates = goalchainer_result_to_candidates(
                gc_result, cycle=self._cycle, source_ids=source_ids
            )
            if not gc_candidates:
                return []
            return self._admit_derived(gc_candidates)
        except Exception:
            return []

    def _resolve_contradictions_phase(self, result: CycleResult) -> None:
        """Detect and resolve contradictions in the active set."""
        contradictions = self.engine.detect_contradictions(self.store)
        result.contradictions = contradictions
        if not contradictions:
            return
        resolutions = self.engine.resolve_contradictions(self.store, contradictions)
        result.resolutions = resolutions
        for res in resolutions:
            for ev_item in res.evicted_items:
                self.forget_log.record(ev_item, self._cycle)
                self.utility.remove(ev_item.id)

    def _reinforce(self) -> None:
        """Boost STI for items that were actually used this cycle."""
        for item in self.store.get_active_set():
            rec = self.utility.get_record(item.id)
            if rec is not None and rec.use_count > 0 and rec.last_use_tick == self._cycle:
                item.attention.boost(2.0)

    def _do_writeback(
        self,
        append_fn: Optional[Callable[[str], None]],
        result: CycleResult,
    ) -> None:
        """Write high-utility items back to LTM via append_fn."""
        if append_fn is None:
            return
        wb_candidates = self.writeback.select_candidates(self.store)
        written = self.writeback.writeback(wb_candidates, append_fn)
        result.written_back = len(written)

    # ── Main cycle ─────────────────────────────────────────────────

    def cycle(
        self,
        recall_fn: Optional[Callable[[], list[tuple[str, str, float]]]] = None,
        append_fn: Optional[Callable[[str], None]] = None,
    ) -> CycleResult:
        """Run one full WMTM orchestration cycle.

        Args:
            recall_fn: optional callable returning [(item_id, content, sti)] from LTM
            append_fn: optional callable for writeback to LTM (petta_append)

        Returns CycleResult summary.
        """
        result = CycleResult(cycle=self._cycle)

        # 1. Recall from LTM
        self._recall_from_ltm(recall_fn)

        # 1b. Goal-driven context boost
        self._goal_context_boost()

        # 2. Run basic inference
        active = self.store.get_active_set()
        candidates = self.engine.infer(active)

        # 3. Admit novel derived candidates
        admitted = self._admit_derived(candidates)
        result.admitted_derived = len(admitted)

        # 2b. Run PLN inference and admit its candidates (F04: with deontic constraints)
        pln_admitted = self._run_pln_phase(deontic_decisions=self._last_deontic_decisions)
        result.pln_admitted = len(pln_admitted)

        # 2c. Run GoalChainer feedback loop and admit its decisions
        gc_admitted = self._run_goalchainer_phase(result)
        result.gc_admitted = len(gc_admitted)

        # 3c/3d. Detect and resolve contradictions
        self._resolve_contradictions_phase(result)

        # 4. Tick: decay + age
        evicted_by_tick = self.store.tick()
        for ev in evicted_by_tick:
            self.forget_log.record(ev, self._cycle)

        # 5. Utility tracking
        self.utility.tick(self.store, self._cycle)

        # 5b. Reinforce used items
        self._reinforce()

        # 6. Forgetting policy
        evicted_by_policy = self.forgetting.evaluate(self.store)
        for ev in evicted_by_policy:
            self.forget_log.record(ev, self._cycle)
            self.utility.remove(ev.id)

        result.evicted = len(evicted_by_tick) + len(evicted_by_policy)

        # 7. Writeback
        self._do_writeback(append_fn, result)

        result.active_count = len(self.store)
        result.forgetting_log_size = len(self.forget_log)

        # 8. Goal lifecycle: check achievement/expiry
        self._goal_lifecycle_check()

        self._cycle += 1
        return result


    def _goal_lifecycle_check(self) -> None:
        """Check active goals for expiry and update goal store."""
        if self.goal_store is None:
            return
        for goal in self.goal_store.active():
            if goal.is_expired(self._cycle) and goal.status in ('pending', 'active'):
                goal.abandon()
                self.goal_store.update(goal)

    @property
    def cycle_count(self) -> int:
        """Return the current cycle count."""
        return self._cycle
