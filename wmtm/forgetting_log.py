"""ForgettingLog: records evicted items to prevent re-derivation.

When an item is evicted from WMTM, its signature is recorded so that
the inference engine and recall bridge can skip re-admitting it
(unless it receives significant new attention).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .item import WMTMItem


@dataclass
class ForgetRecord:
    """Record of a forgotten/evicted item."""
    item_id: str
    content_hash: str
    source_type: str
    evicted_at_tick: int
    age_at_eviction: int
    final_utility: float
    final_sti: float
    derived_from: list[str] = field(default_factory=list)


def content_hash(content: str) -> str:
    """Compute a short hash of content for dedup."""
    return hashlib.md5(content.lower().strip().encode()).hexdigest()[:12]


class ForgettingLog:
    """Append-only log of evicted items.

    Used to prevent re-derivation of forgotten beliefs and to
    track what has been forgotten for debugging/analysis.
    """

    def __init__(self) -> None:
        self._records: list[ForgetRecord] = []
        self._hashes: set[str] = set()
        self._ids: set[str] = set()

    def record(self, item: WMTMItem, tick: int) -> ForgetRecord:
        """Record that an item was evicted at the given tick."""
        ch = content_hash(item.content)
        rec = ForgetRecord(
            item_id=item.id,
            content_hash=ch,
            source_type=item.source_type,
            evicted_at_tick=tick,
            age_at_eviction=item.age,
            final_utility=item.utility,
            final_sti=item.attention.sti,
            derived_from=item.derived_from,
        )
        self._records.append(rec)
        self._hashes.add(ch)
        self._ids.add(item.id)
        return rec

    def is_forgotten(self, content: str) -> bool:
        """Check if content matching this has been forgotten."""
        return content_hash(content) in self._hashes

    def is_id_forgotten(self, item_id: str) -> bool:
        """Check if an item ID has been forgotten."""
        return item_id in self._ids

    def should_re_admit(
        self,
        content: str,
        sti_threshold: float = 5.0,
        current_sti: float = 0.0,
    ) -> bool:
        """Decide whether to re-admit a previously forgotten item.

        Returns True if the item should be re-admitted (e.g., because
        current attention is high enough to override the forgetting).
        """
        if not self.is_forgotten(content):
            return True  # Never forgotten, allow
        return current_sti >= sti_threshold

    def get_records(self) -> list[ForgetRecord]:
        """Return all forgetting records."""
        return list(self._records)

    def clear(self) -> None:
        """Clear the log (for testing)."""
        self._records.clear()
        self._hashes.clear()
        self._ids.clear()

    def __len__(self) -> int:
        return len(self._records)
