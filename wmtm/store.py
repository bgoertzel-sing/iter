"""WMTMStore: bounded-capacity mutable active-set for working memory."""
from __future__ import annotations

from typing import Optional

from .attention import AttentionValue
from .item import WMTMItem


class WMTMStore:
    """A bounded-capacity mutable store for the WMTM layer.

    Unlike the append-only LTM journal, items can be added, updated,
    and REMOVED. When at capacity, lowest-STI items are evicted first.
    """

    def __init__(self, capacity: int = 200, tick: int = 0) -> None:
        """Initialize the WMTM store with a given capacity."""
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items: dict[str, WMTMItem] = {}
        self._pending_evicted: list[WMTMItem] = []
        self._tick = tick

    # -- core operations ------------------------------------------------

    def admit(
        self,
        item_id: str,
        content: str,
        source_type: str = "recalled",
        origin_cluster: Optional[str] = None,
        derived_from: Optional[list[str]] = None,
        initial_sti: float = 1.0,
    ) -> WMTMItem:
        """Add an item. If at/over capacity, evict lowest-STI first.

        If the item already exists, refresh its attention and return it.
        """
        if item_id in self._items:
            existing = self._items[item_id]
            existing.attention.boost(initial_sti)
            existing.last_used = self._tick
            return existing

        item = WMTMItem(
            id=item_id,
            content=content,
            source_type=source_type,
            origin_cluster=origin_cluster,
            derived_from=derived_from or [],
            attention=AttentionValue(sti=initial_sti),
            age=0,
            utility=0.0,
            last_used=self._tick,
        )

        while len(self._items) >= self.capacity:
            self._evict_lowest_sti()

        self._items[item_id] = item
        return item

    def evict(self, item_id: str) -> Optional[WMTMItem]:
        """Explicitly remove an item by ID. Returns the evicted item or None."""
        return self._items.pop(item_id, None)

    def get(self, item_id: str) -> Optional[WMTMItem]:
        """Retrieve an item by ID, returning None if not found."""
        return self._items.get(item_id)

    def touch(self, item_id: str) -> None:
        """Record an access/use of an item."""
        item = self._items.get(item_id)
        if item:
            item.touch(self._tick)

    def get_active_set(self) -> list["WMTMItem"]:
        """Return all active items sorted by total attention descending."""
        items = list(self._items.values())
        items.sort(key=lambda it: it.attention.total, reverse=True)
        return items

    def drain_pending_evicted(self) -> list["WMTMItem"]:
        """Return and clear items evicted by capacity overflow."""
        evicted = self._pending_evicted
        self._pending_evicted = []
        return evicted

    def tick(self) -> list[WMTMItem]:
        """Advance one cycle: decay attention, age items, return evicted list."""
        self._tick += 1
        evicted: list[WMTMItem] = []
        for item in self._items.values():
            item.age += 1
            item.attention.tick()

        # Evict items whose STI has decayed below a floor
        floor = 0.01
        for item in list(self._items.values()):
            if item.attention.sti < floor:
                evicted.append(self._items.pop(item.id))
        return evicted

    # -- internal helpers -----------------------------------------------

    def _evict_lowest_sti(self) -> Optional[WMTMItem]:
        """Remove and return the item with the lowest STI."""
        if not self._items:
            return None
        lowest = min(self._items.values(), key=lambda it: it.attention.sti)
        evicted = self._items.pop(lowest.id)
        self._pending_evicted.append(evicted)
        return evicted

    # -- dunder helpers -------------------------------------------------

    def __len__(self) -> int:
        """Return the number of items currently in the active set."""
        return len(self._items)

    def __contains__(self, item_id: str) -> bool:
        """Check whether an item with the given id exists in the store."""
        return item_id in self._items
