"""ForgettingPolicy: decides what to evict and when in the WMTM."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
        """
        evicted = []

        # 1. Evict items below STI threshold
        for item in list(store._items.values()):
            threshold = (
                self.derived_sti_threshold
                if item.source_type == "derived"
                else self.sti_threshold
            )
            if item.attention.sti < threshold:
                evicted.append(store.evict(item.id))

        # 2. Age-based review: items exceeding max_age with low utility
        for item in list(store._items.values()):
            age_limit = (
                self.derived_max_age
                if item.source_type == "derived"
                else self.max_age
            )
            if item.age >= age_limit and item.utility < self.min_utility:
                evicted.append(store.evict(item.id))

        # 3. If still over capacity, evict lowest-utility items
        while len(store) > store.capacity:
            lowest = min(store._items.values(), key=lambda it: it.utility)
            evicted.append(store.evict(lowest.id))

        # Filter out None (items already gone)
        return [e for e in evicted if e is not None]

    def should_writeback(self, item, min_survival_cycles: int = 30,
                         min_utility: float = 2.0) -> bool:
        """A recalled item that survived many cycles with high utility
        may deserve enrichment back to LTM."""
        if item.source_type != "recalled":
            return False
        return item.age >= min_survival_cycles and item.utility >= min_utility
