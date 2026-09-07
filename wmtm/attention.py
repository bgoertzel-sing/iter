"""ECAN attention values for WMTM items.

Short-Term Importance (STI): current relevance, decays fast.
Activational-Type Importance (ATI): medium-term, decays slower.
Long-Term Importance (LTI): durable importance, decays slowest.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AttentionValue:
    """ECAN attention triple with exponential decay per tick."""

    sti: float = 0.0
    ati: float = 0.0
    lti: float = 0.0

    # Decay rates per tick (fraction retained: 0.95 = lose 5%/tick)
    sti_decay: float = 0.90
    ati_decay: float = 0.97
    lti_decay: float = 0.995

    def tick(self) -> None:
        """Apply one decay step to all three attention components."""
        self.sti *= self.sti_decay
        self.ati *= self.ati_decay
        self.lti *= self.lti_decay

    def boost(self, amount: float) -> None:
        """Inject attention (typically into STI)."""
        self.sti += amount

    @property
    def total(self) -> float:
        """Weighted composite attention for ranking."""
        return self.sti * 1.0 + self.ati * 0.5 + self.lti * 0.2
