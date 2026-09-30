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

    def __init__(self, max_records: int = 500) -> None:
        """Initialize the forgetting log.

        F08 fix: Add max_records cap to prevent unbounded growth.
        When the cap is reached, oldest records are pruned (FIFO).
        """
        self._records: list[ForgetRecord] = []
        self._hashes: set[str] = set()
        self._ids: set[str] = set()
        self._max_records = max_records

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
        # F08b: Enforce max_records cap (FIFO pruning).
        # Without this, the forgetting log grows unboundedly despite
        # having a max_records parameter set in __init__.
        while len(self._records) > self._max_records:
            pruned = self._records.pop(0)
            # Only remove hash/id if no other record shares them
            if not any(r.content_hash == pruned.content_hash for r in self._records):
                self._hashes.discard(pruned.content_hash)
            if not any(r.item_id == pruned.item_id for r in self._records):
                self._ids.discard(pruned.item_id)
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
        """Return the number of logged eviction events."""
        return len(self._records)

    # -- F01b: serialization for persistence across subprocess boundaries --

    def to_dict(self) -> dict:
        """Serialize forgetting log state for persistence."""
        return {
            "max_records": self._max_records,
            "records": [
                {
                    "item_id": rec.item_id,
                    "content_hash": rec.content_hash,
                    "source_type": rec.source_type,
                    "evicted_at_tick": rec.evicted_at_tick,
                    "age_at_eviction": rec.age_at_eviction,
                    "final_utility": rec.final_utility,
                    "final_sti": rec.final_sti,
                    "derived_from": rec.derived_from,
                }
                for rec in self._records
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ForgettingLog":
        """Restore forgetting log from serialized dict."""
        log = cls(max_records=d.get("max_records", 500))
        for rec_d in d.get("records", []):
            rec = ForgetRecord(
                item_id=rec_d["item_id"],
                content_hash=rec_d["content_hash"],
                source_type=rec_d.get("source_type", "recalled"),
                evicted_at_tick=rec_d.get("evicted_at_tick", 0),
                age_at_eviction=rec_d.get("age_at_eviction", 0),
                final_utility=rec_d.get("final_utility", 0.0),
                final_sti=rec_d.get("final_sti", 0.0),
                derived_from=rec_d.get("derived_from", []),
            )
            log._records.append(rec)
            log._hashes.add(rec.content_hash)
            log._ids.add(rec.item_id)
        return log
