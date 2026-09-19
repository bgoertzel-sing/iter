"""Goal representation for goal-driven WMTM orchestration.

Goals are structured targets that guide inference and decision-making.
Each goal has a description, priority, status lifecycle, and optional
deadline. Goals feed into GoalChainer to produce action recommendations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


@dataclass
class Goal:
    """A single goal with lifecycle tracking.

    Attributes:
        id: Unique identifier (auto-generated if not provided).
        description: Human-readable goal description.
        priority: 0.0-1.0, higher = more urgent.
        status: One of 'pending', 'active', 'achieved', 'abandoned', 'blocked'.
        created_cycle: Cycle number when goal was created.
        achieved_cycle: Cycle when goal was achieved (None if not yet).
        deadline_cycle: Optional soft deadline (cycle number).
        parent_id: Optional parent goal for hierarchical decomposition.
        sub_goal_ids: List of child goal IDs.
        metadata: Additional key-value pairs.
    """
    id: str = field(default_factory=lambda: f"goal-{uuid.uuid4().hex[:8]}")
    description: str = ""
    priority: float = 0.5
    status: str = "pending"
    created_cycle: int = 0
    achieved_cycle: Optional[int] = None
    deadline_cycle: Optional[int] = None
    parent_id: Optional[str] = None
    sub_goal_ids: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        """Validate goal fields after initialization."""
        if not 0.0 <= self.priority <= 1.0:
            raise ValueError(f"priority must be in [0, 1], got {self.priority}")
        valid_statuses = {"pending", "active", "achieved", "abandoned", "blocked"}
        if self.status not in valid_statuses:
            raise ValueError(
                f"status must be one of {valid_statuses}, got '{self.status}'"
            )

    def activate(self, cycle: int) -> None:
        """Transition goal from 'pending' to 'active'."""
        if self.status != "pending":
            raise ValueError(
                f"Cannot activate goal in '{self.status}' status (must be 'pending')"
            )
        self.status = "active"

    def achieve(self, cycle: int) -> None:
        """Mark goal as achieved at the given cycle."""
        if self.status not in ("active", "pending"):
            raise ValueError(
                f"Cannot achieve goal in '{self.status}' status"
            )
        self.status = "achieved"
        self.achieved_cycle = cycle

    def abandon(self) -> None:
        """Mark goal as abandoned."""
        if self.status == "achieved":
            raise ValueError("Cannot abandon an achieved goal")
        self.status = "abandoned"

    def block(self) -> None:
        """Mark goal as blocked (cannot proceed without external input)."""
        if self.status == "achieved":
            raise ValueError("Cannot block an achieved goal")
        self.status = "blocked"

    def unblock(self) -> None:
        """Transition from 'blocked' back to 'active'."""
        if self.status != "blocked":
            raise ValueError(
                f"Cannot unblock goal in '{self.status}' status (must be 'blocked')"
            )
        self.status = "active"

    def is_expired(self, current_cycle: int) -> bool:
        """Check if goal has passed its deadline."""
        if self.deadline_cycle is None:
            return False
        return current_cycle > self.deadline_cycle and self.status not in (
            "achieved",
            "abandoned",
        )

    def is_actionable(self) -> bool:
        """Check if goal can be acted upon (active or pending)."""
        return self.status in ("active", "pending")

    def to_dict(self) -> dict:
        """Serialize goal to dict for persistence."""
        return {
            "id": self.id,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "created_cycle": self.created_cycle,
            "achieved_cycle": self.achieved_cycle,
            "deadline_cycle": self.deadline_cycle,
            "parent_id": self.parent_id,
            "sub_goal_ids": list(self.sub_goal_ids),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Goal":
        """Deserialize goal from dict."""
        return cls(
            id=data["id"],
            description=data["description"],
            priority=data["priority"],
            status=data["status"],
            created_cycle=data["created_cycle"],
            achieved_cycle=data.get("achieved_cycle"),
            deadline_cycle=data.get("deadline_cycle"),
            parent_id=data.get("parent_id"),
            sub_goal_ids=data.get("sub_goal_ids", []),
            metadata=data.get("metadata", {}),
        )

    def __repr__(self) -> str:
        return f"Goal(id={self.id!r}, status={self.status!r}, priority={self.priority:.2f}, desc={self.description!r})"
