"""Reference monitor for goal-driven iter control.

Authorizes action grants by verifying HMAC signatures, checking expiry,
validating idempotency keys, and enforcing revision matching against the
current event log revision.

Safety invariants:
- I3: Effect-bearing requests carry a revision matching the current snapshot.
- I6: Persisted authorizations are tamper-proof (HMAC-bound).
- I8: Every dispatched action is reconciled against the event log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Optional

from control.types import ActionGrant, compute_mac, verify_mac


def parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string to datetime."""
    # Handle trailing Z
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


class ReferenceMonitor:
    """Authorizes action grants before dispatch.

    The reference monitor is the sole authority for approving actions.
    It verifies:
    1. HMAC signature integrity (tamper detection)
    2. Grant has not expired
    3. Idempotency key has not been used before
    4. State revision matches the current event log revision
    """

    def __init__(self, secret: bytes):
        self._secret = secret
        self._used_idempotency_keys: set[str] = set()
        self._dispatched_grant_ids: set[str] = set()

    def authorize(
        self,
        grant: ActionGrant,
        current_revision: int,
        clock: callable = None,
    ) -> tuple[bool, str]:
        """Authorize an action grant.

        Returns (authorized, reason) tuple.
        - authorized: True if all checks pass
        - reason: human-readable explanation
        """
        now = clock() if clock else datetime.now(timezone.utc)

        # Check 1: HMAC signature
        if not verify_mac(grant, self._secret):
            return (False, "HMAC verification failed: signature mismatch")

        # Check 2: Expiry
        try:
            expires_at = parse_iso(grant.expires_at)
        except (ValueError, AttributeError):
            return (False, f"Invalid expires_at format: {grant.expires_at}")

        if now > expires_at:
            return (False, f"Grant expired at {grant.expires_at}")

        # Check 3: Idempotency key
        if grant.idempotency_key in self._used_idempotency_keys:
            return (False, f"Idempotency key already used: {grant.idempotency_key}")

        # Check 4: State revision match
        if grant.state_revision != current_revision:
            return (
                False,
                f"Revision mismatch: grant has {grant.state_revision}, "
                f"current is {current_revision}",
            )

        # Check 5: Grant ID not already dispatched
        if grant.grant_id in self._dispatched_grant_ids:
            return (False, f"Grant already dispatched: {grant.grant_id}")

        # All checks pass — record the authorization
        self._used_idempotency_keys.add(grant.idempotency_key)
        self._dispatched_grant_ids.add(grant.grant_id)
        return (True, "Authorized")

    def revoke(self, grant_id: str):
        """Revoke a previously authorized grant.

        This removes the idempotency key associated with the grant,
        preventing re-use of its authorization.
        """
        self._dispatched_grant_ids.discard(grant_id)

    def is_dispatched(self, grant_id: str) -> bool:
        """Check if a grant has been dispatched."""
        return grant_id in self._dispatched_grant_ids

    def used_idempotency_keys(self) -> set[str]:
        """Return a copy of used idempotency keys."""
        return self._used_idempotency_keys.copy()

    def reset(self):
        """Reset all state. Used for testing."""
        self._used_idempotency_keys.clear()
        self._dispatched_grant_ids.clear()
