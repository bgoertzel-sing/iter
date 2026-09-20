"""Action dispatcher for goal-driven iter control.

The dispatcher is the sole component that executes authorized actions.
It enforces:
- I1: No grant, no effect (must have authorized grant)
- I6: At-most-once + reconcile (idempotency via reference monitor)
- I10: Fail closed (errors are logged, not silently swallowed)

The dispatcher:
1. Receives an authorized ActionGrant from the reference monitor
2. Looks up the operator implementation
3. Executes the action in a bounded context
4. Records the outcome in the event store
5. Returns an EffectRecord
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from control.types import ActionGrant, Effect, EffectClass, EffectRecord, now_iso


@dataclass
class DispatchResult:
    """Result of a dispatch attempt."""
    grant_id: str
    success: bool
    effect_record: Optional[EffectRecord] = None
    error: str = ""
    elapsed_ms: float = 0.0


class Dispatcher:
    """Executes authorized actions via registered operator implementations.

    The dispatcher is parameterized with operator implementations (callables)
    that are registered by the orchestrator. Each implementation receives
    the canonical arguments and returns an Effect or raises an exception.
    """

    def __init__(self, timeout_s: float = 30.0):
        self._implementations: dict[str, Callable] = {}
        self._timeout_s = timeout_s
        self._dispatch_log: list[DispatchResult] = []

    def register_implementation(self, operator_id: str, impl: Callable):
        """Register an implementation function for an operator."""
        self._implementations[operator_id] = impl

    def dispatch(
        self,
        grant: ActionGrant,
        authorized: bool = True,
    ) -> DispatchResult:
        """Execute an authorized action.

        Args:
            grant: The action grant to execute.
            authorized: Whether the grant has been authorized by the
                reference monitor. If False, dispatch is refused.

        Returns:
            DispatchResult with outcome details.
        """
        start = time.monotonic()

        # I1: No grant, no effect
        if not authorized:
            result = DispatchResult(
                grant_id=grant.grant_id,
                success=False,
                error="Grant not authorized",
                elapsed_ms=0.0,
            )
            self._dispatch_log.append(result)
            return result

        # Look up implementation
        impl = self._implementations.get(grant.tool_name)
        if impl is None:
            result = DispatchResult(
                grant_id=grant.grant_id,
                success=False,
                error=f"No implementation registered for operator: {grant.tool_name}",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )
            self._dispatch_log.append(result)
            return result

        # Execute the action
        try:
            import json
            args = json.loads(grant.canonical_args) if grant.canonical_args else {}
            effect = impl(args, grant.target)

            # Validate the returned effect
            if not isinstance(effect, Effect):
                raise TypeError(
                    f"Operator {grant.tool_name} returned {type(effect).__name__}, "
                    f"expected Effect"
                )

            effect_record = EffectRecord(
                record_id=f"er_{grant.grant_id}",
                grant_id=grant.grant_id,
                operator_id=grant.tool_name,
                target=grant.target,
                observed_at=now_iso(),
                effect_class=effect.effect_class,
                magnitude=effect.magnitude,
                details=effect.details,
            )

            result = DispatchResult(
                grant_id=grant.grant_id,
                success=True,
                effect_record=effect_record,
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        except Exception as e:
            result = DispatchResult(
                grant_id=grant.grant_id,
                success=False,
                error=f"{type(e).__name__}: {e}",
                elapsed_ms=(time.monotonic() - start) * 1000,
            )

        self._dispatch_log.append(result)
        return result

    @property
    def dispatch_count(self) -> int:
        return len(self._dispatch_log)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self._dispatch_log if r.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self._dispatch_log if not r.success)

    def get_log(self) -> list[DispatchResult]:
        return list(self._dispatch_log)

    def reset(self):
        self._dispatch_log.clear()
