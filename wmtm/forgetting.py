"""ForgettingPolicy: decides what to evict and when in the WMTM."""
from __future__ import annotations

from dataclasses import dataclass

from .item import WMTMItem
from .store import WMTMStore


@dataclass
class ForgettingPolicy:
    """Policy for evicting WMTM items based on attention decay and utility.

    Thresholds:
      sti_threshold: below this STI, an item is evicted immediately.
      max_age: after this many cycles, an item is reviewed for utility.
      min_utility: at max_age review, if utility < this, evict.
      derived_max_age: stricter age limit for derived items (they decay faster).
      derived_sti_threshold: stricter STI floor for derived items.
    """
    sti_threshold: float = 0.05
    max_age: int = 50
    min_utility: float = 0.5
    derived_max_age: int = 20
    derived_sti_threshold: float = 0.10

    def evaluate(self, store: WMTMStore) -> list:
        """Run the forgetting policy over the store.

        Returns list of evicted items.
        
        F15: Also applies wall-clock-based eviction since cycle-based age
        never exceeds 1 (iter process restarts every step).
        """
        evicted = []
        evicted.extend(self._evict_below_sti(store))
        evicted.extend(self._evict_aged_low_utility(store))
        evicted.extend(self._evict_wallclock_stale(store))
        evicted.extend(self._evict_over_capacity(store))
        return [e for e in evicted if e is not None]

    def _evict_below_sti(self, store: WMTMStore) -> list:
        """Evict items whose STI falls below their type-specific threshold."""
        evicted = []
        for item in list(store._items.values()):
            threshold = self._sti_threshold_for(item)
            if item.attention.sti < threshold:
                evicted.append(store.evict(item.id))
        return evicted

    def _evict_aged_low_utility(self, store: WMTMStore) -> list:
        """Evict items exceeding their max age with utility below minimum."""
        evicted = []
        for item in list(store._items.values()):
            age_limit = self._max_age_for(item)
            if item.age >= age_limit and item.utility < self.min_utility:
                evicted.append(store.evict(item.id))
        return evicted

    @staticmethod
    def _evict_over_capacity(store: WMTMStore) -> list:
        """Evict lowest-utility items until store is at or below capacity."""
        evicted = []
        while len(store) > store.capacity:
            lowest = min(store._items.values(), key=lambda it: it.utility)
            evicted.append(store.evict(lowest.id))
        return evicted

    @staticmethod
    def _evict_wallclock_stale(store: WMTMStore) -> list:
        """Evict items that are stale by wall-clock time (F15).
        
        Since iter process restarts every step, item.age never exceeds 1.
        Wall-clock timestamps are the only reliable staleness measure.
        Items older than 6 hours with zero utility are evicted.
        """
        import time as _t
        now = _t.time()
        evicted = []
        for item in list(store._items.values()):
            if item.created_at <= 0:
                # Legacy item without timestamp — skip here, handled by
                # _cleanup_stale_items in wmtm_context.py
                continue
            wall_age_hours = (now - item.created_at) / 3600.0
            # Zero-utility items older than 6 hours
            if item.utility <= 0.0 and wall_age_hours >= 6.0:
                evicted.append(store.evict(item.id))
        return evicted

    def _sti_threshold_for(self, item: WMTMItem) -> float:
        """Return the STI threshold for the item's source type."""
        if item.source_type == "derived":
            return self.derived_sti_threshold
        return self.sti_threshold

    def _max_age_for(self, item: WMTMItem) -> int:
        """Return the max age for the item's source type."""
        if item.source_type == "derived":
            return self.derived_max_age
        return self.max_age

    def should_writeback(self, item: WMTMItem, min_survival_cycles: int = 30,
                         min_utility: float = 2.0) -> bool:
        """A recalled item that survived many cycles with high utility
        may deserve enrichment back to LTM."""
        if item.source_type != "recalled":
            return False
        return item.age >= min_survival_cycles and item.utility >= min_utility
