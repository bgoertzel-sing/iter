"""WMTMItem: a single item in the working medium-term memory."""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Optional

from .attention import AttentionValue


def _now() -> float:
    return _time.time()


@dataclass
class WMTMItem:
    """A mutable item in the WMTM active set.

    source_type: 'recalled' (pulled from LTM) or 'derived' (from PLN inference)
    origin_cluster: LTM MemoryCluster ID (if recalled)
    derived_from: parent item IDs (if derived)
    age: number of ticks since admission
    utility: composite usefulness score (higher = more useful)
    last_used: tick number of last access
    created_at: wall-clock unix timestamp when this item was first admitted
    """
    id: str
    content: str
    source_type: str = "recalled"  # or "derived"
    origin_cluster: Optional[str] = None
    derived_from: list[str] = field(default_factory=list)
    attention: AttentionValue = field(default_factory=AttentionValue)
    age: int = 0
    utility: float = 0.0
    last_used: int = 0
    created_at: float = field(default_factory=_now)
    origin_timestamp: float = 0.0  # F25: original journal entry timestamp (0 = unknown)

    @property
    def effective_age_seconds(self) -> float:
        """Wall-clock age using the OLDEST known timestamp.

        F25: When a recalled item is re-admitted, created_at resets to now,
        making a 23-day-old entry appear 41 minutes old. origin_timestamp
        preserves the journal entry's actual date, so staleness checks use
        the real age, not the re-admission age.

        Returns seconds since the oldest available timestamp, or since
        created_at if no origin_timestamp is set.
        """
        import time as _t
        now = _t.time()
        if self.origin_timestamp > 0:
            return now - min(self.origin_timestamp, self.created_at)
        return now - self.created_at

    def touch(self, tick: int) -> None:
        """Record access at the given tick."""
        self.last_used = tick
        self.utility += 1.0
        self.attention.boost(0.5)

    def __repr__(self) -> str:
        """Return a concise string representation of the WMTM item."""
        return (
            f"WMTMItem(id={self.id!r}, type={self.source_type}, "
            f"sti={self.attention.sti:.2f}, age={self.age})"
        )

    def to_dict(self) -> dict:
        """Serialize item for persistence (F01)."""
        return {
            "id": self.id,
            "content": self.content,
            "source_type": self.source_type,
            "origin_cluster": self.origin_cluster,
            "derived_from": self.derived_from,
            "attention": self.attention.to_dict(),
            "age": self.age,
            "utility": self.utility,
            "last_used": self.last_used,
            "created_at": self.created_at,
            "origin_timestamp": self.origin_timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WMTMItem":
        """Restore item from serialized dict (F01)."""
        from .attention import AttentionValue
        return cls(
            id=d["id"],
            content=d["content"],
            source_type=d["source_type"],
            origin_cluster=d["origin_cluster"],
            derived_from=d.get("derived_from", []),
            attention=AttentionValue.from_dict(d["attention"]),
            age=d.get("age", 0),
            utility=d.get("utility", 0.0),
            last_used=d.get("last_used", 0),
            created_at=d.get("created_at", 0.0),
            origin_timestamp=d.get("origin_timestamp", 0.0),
        )
