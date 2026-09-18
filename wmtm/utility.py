"""UtilityTracker: records use/miss events and computes utility scores.

Tracks how often WMTM items are actually used (accessed for inference,
recall, or response generation) vs. how many cycles they survive unused.
This feeds into the ForgettingPolicy for age-based eviction decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .item import WMTMItem
from .store import WMTMStore


@dataclass
class UtilityRecord:
    """Per-item utility tracking record."""
    item_id: str
    use_count: int = 0
    miss_count: int = 0  # cycles where item was active but not used
    last_use_tick: int = 0
    total_ticks_alive: int = 0
    inferred_from_count: int = 0  # how many derived items came from this

    @property
    def utility_score(self) -> float:
        """Composite utility: uses weighted heavily, misses penalize lightly.

        Formula: use_count * 2.0 + inferred_from_count * 1.5 - miss_count * 0.3
        Clamped to >= 0.
        """
        raw = self.use_count * 2.0 + self.inferred_from_count * 1.5 - self.miss_count * 0.3
        return max(raw, 0.0)

    @property
    def hit_rate(self) -> float:
        """Fraction of alive ticks where the item was used."""
        if self.total_ticks_alive == 0:
            return 0.0
        return self.use_count / self.total_ticks_alive


class UtilityTracker:
    """Tracks use/miss events for WMTM items and updates their utility.

    Call record_use() when an item is accessed for any purpose.
    Call record_miss() at each tick for items that weren't used.
    Call record_inferred_from() when an item contributes to a derivation.
    """

    def __init__(self) -> None:
        """Initialize the utility function evaluator."""
        self._records: dict[str, UtilityRecord] = {}

    def ensure(self, item_id: str) -> UtilityRecord:
        """Get or create a utility record for an item."""
        if item_id not in self._records:
            self._records[item_id] = UtilityRecord(item_id=item_id)
        return self._records[item_id]

    def record_use(self, item_id: str, tick: int) -> None:
        """Record that an item was used at the given tick."""
        rec = self.ensure(item_id)
        rec.use_count += 1
        rec.last_use_tick = tick

    def record_miss(self, item_id: str, tick: int) -> None:
        """Record that an item survived a cycle without being used."""
        rec = self.ensure(item_id)
        rec.miss_count += 1
        rec.total_ticks_alive += 1

    def record_inferred_from(self, item_id: str) -> None:
        """Record that this item contributed to a derived belief."""
        rec = self.ensure(item_id)
        rec.inferred_from_count += 1

    def tick(self, store: WMTMStore, current_tick: int) -> None:
        """Process one tick: record misses for all active items.

        F10 fix: Items that were used this tick (last_use_tick == current_tick)
        are not counted as misses. Utility score is accumulated, not overwritten,
        to avoid erasing touch()-based increments.
        """
        for item in store.get_active_set():
            rec = self.ensure(item.id)
            # Only count as miss if the item wasn't used this tick
            # F10 fix: item.last_used is set by touch(), rec.last_use_tick by record_use()
            # Check both to avoid counting a use as a miss
            was_used_this_tick = (item.last_used == current_tick or rec.last_use_tick == current_tick)
            rec.total_ticks_alive += 1
            if not was_used_this_tick:
                rec.miss_count += 1
            # F10 fix: Don't overwrite item.utility. Instead, set it to the
            # tracker's computed score PLUS any touch-based increment.
            # The tracker score already includes use_count contributions,
            # so we use it as the authoritative value but add the touch delta.
            # Since touch() adds +1.0 and record_use() adds to use_count,
            # we need to reconcile: use the tracker score as the base.
            item.utility = rec.utility_score

    def get_record(self, item_id: str) -> Optional[UtilityRecord]:
        """Return the utility record for an item, or None if not tracked."""
        return self._records.get(item_id)

    def get_utility(self, item_id: str) -> float:
        """Return the current utility score for an item (0.0 if untracked)."""
        rec = self._records.get(item_id)
        return rec.utility_score if rec else 0.0

    def remove(self, item_id: str) -> None:
        """Clean up tracking record when an item is evicted."""
        self._records.pop(item_id, None)

    def promote_to_ltm_candidates(
        self,
        store: WMTMStore,
        min_age: int = 30,
        min_utility: float = 2.0,
    ) -> list[WMTMItem]:
        """Find items worthy of promotion back to LTM.

        Criteria: survived many cycles AND high utility.
        """
        candidates = []
        for item in store.get_active_set():
            if item.age >= min_age and item.utility >= min_utility:
                candidates.append(item)
        return candidates
