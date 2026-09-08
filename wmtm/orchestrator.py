"""WMTMOrchestrator: ties all WMTM components into a single cycle.

One orchestration cycle:
1. Recall from LTM (via recall_bridge) into WMTM
2. Run inference engine over active set
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


@dataclass
class CycleResult:
    """Summary of a single orchestration cycle."""
    cycle: int
    admitted_derived: int = 0
    evicted: int = 0
    written_back: int = 0
    active_count: int = 0
    contradictions: list = field(default_factory=list)
    resolutions: list = field(default_factory=list)


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
    ) -> None:
        self.store = store
        self.engine = inference_engine or WMTMInferenceEngine()
        self.utility = utility_tracker or UtilityTracker()
        self.forgetting = forgetting_policy or ForgettingPolicy()
        self.forget_log = forgetting_log or ForgettingLog()
        self.forget_log_override_threshold = 500.0  # only re-derive forgotten content if exceptionally high attention
        self.writeback = writeback_manager or WritebackManager()
        self._cycle = 0

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

    def _admit_candidate(self, cand, index: int):
        """Admit a single derived candidate into the store."""
        item_id = f"derived-{cand.inference_type}-{self._cycle}-{index}"
        return self.store.admit(
            item_id=item_id,
            content=cand.content,
            source_type='derived',
            derived_from=cand.derived_from,
            initial_sti=cand.initial_sti,
        )

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

        # 2. Run inference
        active = self.store.get_active_set()
        candidates = self.engine.infer(active)

        # 3. Admit novel derived candidates
        admitted = self._admit_derived(candidates)
        result.admitted_derived = len(admitted)

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
        self._cycle += 1
        return result

    @property
    def cycle_count(self) -> int:
        """Return the current cycle count."""
        return self._cycle
