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
    ):
        self.store = store
        self.engine = inference_engine or WMTMInferenceEngine()
        self.utility = utility_tracker or UtilityTracker()
        self.forgetting = forgetting_policy or ForgettingPolicy()
        self.forget_log = forgetting_log or ForgettingLog()
        self.forget_log_override_threshold = 500.0  # only re-derive forgotten content if exceptionally high attention
        self.writeback = writeback_manager or WritebackManager()
        self._cycle = 0

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

        # 1. Recall from LTM (if provided)
        if recall_fn is not None:
            recalled = recall_fn()
            for item_id, content, sti in recalled:
                self.store.admit(
                    item_id=item_id,
                    content=content,
                    source_type='recalled',
                    initial_sti=sti,
                )

        # 1b. Log any items evicted by capacity during recall
        for ev in self.store.drain_pending_evicted():
            self.forget_log.record(ev, self._cycle)

        # 2. Run inference
        active = self.store.get_active_set()
        candidates = self.engine.infer(active)

        # 3. Admit novel derived candidates
        admitted = []
        for i, cand in enumerate(candidates):
            # Skip if previously forgotten (unless high attention)
            if self.forget_log.is_forgotten(cand.content):
                if cand.initial_sti < self.forget_log_override_threshold:
                    continue
            # Skip if content duplicates existing
            if not self.engine.is_novel(cand, self.store):
                continue
            item_id = f"derived-{cand.inference_type}-{self._cycle}-{i}"
            item = self.store.admit(
                item_id=item_id,
                content=cand.content,
                source_type='derived',
                derived_from=cand.derived_from,
                initial_sti=cand.initial_sti,
            )
            if item is not None:
                admitted.append(item)
                # Record that parents contributed to a derivation
                for pid in cand.derived_from:
                    self.utility.record_inferred_from(pid)

        result.admitted_derived = len(admitted)

        # 3b. Log any items evicted by capacity during derived admission
        for ev in self.store.drain_pending_evicted():
            self.forget_log.record(ev, self._cycle)

        # 3c. Detect and resolve contradictions in the active set
        contradictions = self.engine.detect_contradictions(self.store)
        result.contradictions = contradictions

        # 3d. Resolve contradictions (Phase 5: attention+utility-weighted resolution)
        if contradictions:
            resolutions = self.engine.resolve_contradictions(self.store, contradictions)
            result.resolutions = resolutions
            # Log items evicted by contradiction resolution
            for res in resolutions:
                for ev_item in res.evicted_items:
                    self.forget_log.record(ev_item, self._cycle)
                    self.utility.remove(ev_item.id)

        # 4. Tick: decay + age
        evicted_by_tick = self.store.tick()
        for ev in evicted_by_tick:
            self.forget_log.record(ev, self._cycle)

        # 5. Utility tracking
        self.utility.tick(self.store, self._cycle)

        # 5b. Reinforce: boost STI for items actually used this cycle
        for item in self.store.get_active_set():
            rec = self.utility.get_record(item.id)
            if rec is not None and rec.use_count > 0 and rec.last_use_tick == self._cycle:
                item.attention.boost(2.0)  # small STI boost for being useful

        # 6. Forgetting policy
        evicted_by_policy = self.forgetting.evaluate(self.store)
        for ev in evicted_by_policy:
            self.forget_log.record(ev, self._cycle)
            self.utility.remove(ev.id)

        result.evicted = len(evicted_by_tick) + len(evicted_by_policy)

        # 7. Writeback
        if append_fn is not None:
            wb_candidates = self.writeback.select_candidates(self.store)
            written = self.writeback.writeback(wb_candidates, append_fn)
            result.written_back = len(written)

        result.active_count = len(self.store)
        self._cycle += 1
        return result

    @property
    def cycle_count(self) -> int:
        return self._cycle
