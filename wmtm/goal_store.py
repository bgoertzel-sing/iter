"""GoalStore: persistence and management for Goal objects.

Goals are stored in a JSON file and can be added, retrieved, updated,
and queried by status or priority. The store supports hierarchical
goal decomposition through parent_id / sub_goal_ids links.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .goal import Goal


class GoalStore:
    """A file-backed store for goals with query and lifecycle helpers.

    Args:
        path: Path to the JSON file for persistence. Created on first save.
        auto_save: If True, save after every mutation (default True).
    """

    def __init__(self, path: str, auto_save: bool = True) -> None:
        self.path = path
        self.auto_save = auto_save
        self._goals: dict[str, Goal] = {}
        self._load()

    def _load(self) -> None:
        """Load goals from JSON file if it exists."""
        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                data = json.load(f)
            for gdict in data.get("goals", []):
                g = Goal.from_dict(gdict)
                self._goals[g.id] = g

    def save(self) -> None:
        """Persist all goals to JSON file."""
        data = {
            "goals": [g.to_dict() for g in self._goals.values()],
            "count": len(self._goals),
        }
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)

    def add(self, goal: Goal) -> Goal:
        """Add a goal to the store. Raises if duplicate ID."""
        if goal.id in self._goals:
            raise ValueError(f"Goal {goal.id} already exists in store")
        self._goals[goal.id] = goal
        if self.auto_save:
            self.save()
        return goal

    def get(self, goal_id: str) -> Optional[Goal]:
        """Retrieve a goal by ID, or None if not found."""
        return self._goals.get(goal_id)

    def update(self, goal: Goal) -> None:
        """Update an existing goal (must already be in store)."""
        if goal.id not in self._goals:
            raise KeyError(f"Goal {goal.id} not in store; use add() first")
        self._goals[goal.id] = goal
        if self.auto_save:
            self.save()

    def remove(self, goal_id: str) -> None:
        """Remove a goal by ID."""
        if goal_id in self._goals:
            del self._goals[goal_id]
            if self.auto_save:
                self.save()

    def all(self) -> list[Goal]:
        """Return all goals as a list."""
        return list(self._goals.values())

    def active(self) -> list[Goal]:
        """Return goals with status 'active' or 'pending', sorted by priority desc."""
        result = [g for g in self._goals.values() if g.is_actionable()]
        result.sort(key=lambda g: g.priority, reverse=True)
        return result

    def by_status(self, status: str) -> list[Goal]:
        """Return all goals with the given status."""
        return [g for g in self._goals.values() if g.status == status]

    def children_of(self, parent_id: str) -> list[Goal]:
        """Return direct child goals of the given parent."""
        return [g for g in self._goals.values() if g.parent_id == parent_id]

    def expired(self, current_cycle: int) -> list[Goal]:
        """Return goals that have passed their deadline and are still actionable."""
        return [
            g for g in self._goals.values()
            if g.is_expired(current_cycle) and g.is_actionable()
        ]

    def link_subgoal(self, parent_id: str, child_id: str) -> None:
        """Link a child goal to a parent, updating both sides."""
        parent = self.get(parent_id)
        child = self.get(child_id)
        if parent is None:
            raise KeyError(f"Parent goal {parent_id} not found")
        if child is None:
            raise KeyError(f"Child goal {child_id} not found")
        if child_id not in parent.sub_goal_ids:
            parent.sub_goal_ids.append(child_id)
        child.parent_id = parent_id
        if self.auto_save:
            self.save()

    def __len__(self) -> int:
        return len(self._goals)

    def __contains__(self, goal_id: str) -> bool:
        return goal_id in self._goals

    def __repr__(self) -> str:
        n_active = len(self.active())
        return f"GoalStore(path={self.path!r}, total={len(self)}, active={n_active})"
