"""WMTMItem: a single item in the working medium-term memory."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .attention import AttentionValue


@dataclass
class WMTMItem:
    """A mutable item in the WMTM active set.

    source_type: 'recalled' (pulled from LTM) or 'derived' (from PLN inference)
    origin_cluster: LTM MemoryCluster ID (if recalled)
    derived_from: parent item IDs (if derived)
    age: number of ticks since admission
    utility: composite usefulness score (higher = more useful)
    last_used: tick number of last access
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

    def touch(self, tick: int) -> None:
        """Record access at the given tick."""
        self.last_used = tick
        self.utility += 1.0
        self.attention.boost(0.5)

    def __repr__(self) -> str:
        return (
            f"WMTMItem(id={self.id!r}, type={self.source_type}, "
            f"sti={self.attention.sti:.2f}, age={self.age})"
        )
