"""ECAN attention values for WMTM items.

Short-Term Importance (STI): current relevance, decays fast.
Activational-Type Importance (ATI): medium-term, decays slower.
Long-Term Importance (LTI): durable importance, decays slowest.

Attention flows: STI -> ATI -> LTI via consolidation during boost()
(active access). When an item is repeatedly accessed, a fraction of
the injected STI converts to ATI, and a fraction of ATI converts to LTI.
This makes the three-tier decay model functional: items that are
frequently accessed build up durable importance that persists longer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AttentionValue:
    """ECAN attention triple with exponential decay and consolidation flow."""

    sti: float = 0.0
    ati: float = 0.0
    lti: float = 0.0

    # Decay rates per tick (fraction retained: 0.90 = lose 10%/tick)
    sti_decay: float = 0.90
    ati_decay: float = 0.97
    lti_decay: float = 0.995

    # Consolidation rates: fraction of boost that flows to next tier
    sti_to_ati_rate: float = 0.05  # 5% of boost consolidates to ATI
    ati_to_lti_rate: float = 0.02  # 2% of ATI consolidates to LTI per boost

    def tick(self) -> None:
        """Apply one decay step (passive, no consolidation)."""
        self.sti *= self.sti_decay
        self.ati *= self.ati_decay
        self.lti *= self.lti_decay

    def boost(self, amount: float) -> None:
        """Inject attention into STI, with consolidation to ATI/LTI.

        For positive amounts: a fraction consolidates to ATI, and a
        fraction of current ATI consolidates to LTI. This builds durable
        importance for frequently-accessed items.

        For negative amounts: directly reduces STI (no consolidation).
        """
        if amount <= 0:
            self.sti = max(0.0, self.sti + amount)
            return

        # Consolidate a fraction of current ATI to LTI before adding
        ati_flow = self.ati * self.ati_to_lti_rate
        self.lti += ati_flow
        self.ati -= ati_flow

        # Inject into STI, with a fraction consolidating to ATI
        sti_flow = amount * self.sti_to_ati_rate
        self.sti += amount - sti_flow
        self.ati += sti_flow

    def penalty(self, amount: float) -> None:
        """Reduce STI by a penalty amount (clamped to >= 0)."""
        self.sti = max(0.0, self.sti - amount)

    @property
    def total(self) -> float:
        """Weighted composite attention for ranking."""
        return self.sti * 1.0 + self.ati * 0.5 + self.lti * 0.2

    def to_dict(self) -> dict:
        """Serialize attention value for persistence."""
        return {
            "sti": self.sti,
            "ati": self.ati,
            "lti": self.lti,
            "sti_decay": self.sti_decay,
            "ati_decay": self.ati_decay,
            "lti_decay": self.lti_decay,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AttentionValue":
        """Restore attention value from serialized dict."""
        return cls(
            sti=d["sti"],
            ati=d["ati"],
            lti=d["lti"],
            sti_decay=d.get("sti_decay", 0.90),
            ati_decay=d.get("ati_decay", 0.97),
            lti_decay=d.get("lti_decay", 0.995),
        )
