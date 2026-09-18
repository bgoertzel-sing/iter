"""Writeback: promotes high-utility WMTM items back to LTM.

When a recalled or derived item survives many cycles with high utility,
it deserves to be written back to the LTM journal (via petta_append)
so it persists beyond the WMTM's bounded lifetime.

For derived items, this is how inferred beliefs become permanent knowledge.
For recalled items, this allows enrichment (e.g., adding new evidence).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .item import WMTMItem
from .store import WMTMStore


@dataclass
class WritebackCandidate:
    """An item selected for writeback to LTM."""
    item: WMTMItem
    reason: str  # 'high_utility', 'derived_promotion', 'enrichment'
    metta_content: str  # formatted MeTTa for petta_append


def format_derived_metta(item: WMTMItem) -> str:
    """Format a derived item as a MeTTa MemoryCluster for LTM."""
    cluster_id = f"wmtm-derived-{item.id}"
    lines = [
        f"(MemoryCluster {cluster_id})",
        f"(SchemaVersion {cluster_id} medium-memory-v1)",
        f"(ClusterType {cluster_id} DerivedBelief)",
        f"(ClusterSource {cluster_id} wmtm-inference)",
        f"(Contains {cluster_id} ev-{cluster_id})",
        f"(ObservedEvent ev-{cluster_id})",
        f"(About {cluster_id} {cluster_id})",
        f"(EventNote ev-{cluster_id} \"{item.content}\")",
    ]
    # Record derivation lineage
    for parent_id in item.derived_from:
        lines.append(f"(DerivedFrom {cluster_id} {parent_id})")
    lines.append(f"(WMTMUtility {cluster_id} {item.utility:.2f})")
    return "\n".join(lines)


def format_enrichment_metta(item: WMTMItem) -> str:
    """Format an enrichment (recalled item with new utility) for LTM."""
    cluster_id = f"wmtm-enrich-{item.id}"
    lines = [
        f"(MemoryCluster {cluster_id})",
        f"(SchemaVersion {cluster_id} medium-memory-v1)",
        f"(ClusterType {cluster_id} EnrichmentNote)",
        f"(ClusterSource {cluster_id} wmtm-writeback)",
        f"(Contains {cluster_id} ev-{cluster_id})",
        f"(ObservedEvent ev-{cluster_id})",
        f"(About {cluster_id} {item.origin_cluster or cluster_id})",
        f"(EventNote ev-{cluster_id} \"{item.content}\")",
        f"(WMTMUtility {cluster_id} {item.utility:.2f})",
        f"(WMTMAge {cluster_id} {item.age})",
    ]
    return "\n".join(lines)


class WritebackManager:
    """Manages promotion of WMTM items to LTM.

    Selects candidates based on utility and age, formats them as
    MeTTa MemoryClusters, and writes them via a callback (typically
    petta_append).
    """

    def __init__(
        self,
        min_age: int = 30,
        min_utility: float = 2.0,
        derived_min_utility: float = 1.5,
        derived_min_age: int = 15,
    ) -> None:
        """Initialize the writeback manager with a store and journal path."""
        self.min_age = min_age
        self.min_utility = min_utility
        self.derived_min_utility = derived_min_utility
        self.derived_min_age = derived_min_age
        self._written_back: set[str] = set()  # track already-written IDs

    def select_candidates(self, store: WMTMStore) -> list[WritebackCandidate]:
        """Select items from the store that should be written back to LTM."""
        candidates = []
        for item in store.get_active_set():
            if item.id in self._written_back:
                continue
            cand = self._select_for_item(item)
            if cand is not None:
                candidates.append(cand)
        return candidates

    def _select_for_item(self, item: WMTMItem) -> Optional[WritebackCandidate]:
        """Evaluate a single item for writeback eligibility."""
        if item.source_type == "derived":
            return self._select_derived(item)
        if item.source_type == "recalled":
            return self._select_recalled(item)
        return None

    def _select_derived(self, item: WMTMItem) -> Optional[WritebackCandidate]:
        """Check if a derived item qualifies for promotion to LTM."""
        if item.age >= self.derived_min_age and item.utility >= self.derived_min_utility:
            return WritebackCandidate(
                item=item,
                reason="derived_promotion",
                metta_content=format_derived_metta(item),
            )
        return None

    def _select_recalled(self, item: WMTMItem) -> Optional[WritebackCandidate]:
        """Check if a recalled item qualifies for enrichment writeback."""
        if item.age >= self.min_age and item.utility >= self.min_utility:
            return WritebackCandidate(
                item=item,
                reason="enrichment",
                metta_content=format_enrichment_metta(item),
            )
        return None

    def writeback(
        self,
        candidates: list[WritebackCandidate],
        append_fn: Callable[[str], object],
    ) -> list[str]:
        """Execute writeback for selected candidates.

        Args:
            candidates: from select_candidates()
            append_fn: function that takes a string and appends to LTM
                       (typically petta_append or append_cluster)

        Returns list of item IDs that were written back.

        Checks the return value of append_fn: if it returns a dict with
        ok=False (or raises), the item is NOT marked as written, allowing
        retry on the next cycle.
        """
        written = []
        for cand in candidates:
            try:
                result = append_fn(cand.metta_content)
                # Check for failure indication from append_fn
                if isinstance(result, dict) and result.get("ok") is False:
                    # Writeback failed — do NOT mark as written, allow retry
                    continue
                self._written_back.add(cand.item.id)
                written.append(cand.item.id)
            except Exception:
                # Don't let one failure block others; don't mark written
                continue
        return written

    def has_been_written(self, item_id: str) -> bool:
        """Check if an item has already been written back."""
        return item_id in self._written_back

    def reset(self) -> None:
        """Clear tracking (for testing)."""
        self._written_back.clear()
