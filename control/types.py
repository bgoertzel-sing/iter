"""Typed domain model for goal-driven iter control.

Defines core dataclasses: GoalSpec, Observation, Belief, ActionOperator,
ActionGrant, Plan, Appraisal, and supporting enums.

All types are frozen dataclasses for immutability.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional


# ── Enums ──────────────────────────────────────────────────────────────────

class EffectClass(str, Enum):
    OBSERVE = "observe"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"
    EXTERNAL = "external"


class GoalStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    SATISFIED = "satisfied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionState(str, Enum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    AUTHORIZED = "authorized"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    REJECTED = "rejected"


class DeonticStatus(str, Enum):
    OBLIGATED = "obligated"
    RECOMMENDED = "recommended"
    PERMITTED = "permitted"
    FORBIDDEN = "forbidden"


# ── Type aliases (as strings for frozen dataclasses) ───────────────────────

UUID = str
PrincipalId = str
SchemaRef = str
PredicateRef = str


# ── Predicate ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Predicate:
    """An executable, versioned success/failure predicate.

    name: human-readable identifier
    version: schema revision
    predicate_id: stable reference for inclusion in GoalSpec/ActionOperator
    """
    name: str
    version: int = 1
    predicate_id: str = ""
    description: str = ""

    def __post_init__(self):
        if not self.predicate_id:
            digest = hashlib.sha256(
                f"{self.name}:{self.version}".encode()
            ).hexdigest()[:16]
            object.__setattr__(self, "predicate_id", f"pred_{digest}")


# ── Effect & ResourceClaim ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Effect:
    """A declared effect of an action operator."""
    name: str
    effect_type: str  # "state_change", "observation", "artifact"
    description: str = ""
    effect_class: EffectClass = EffectClass.OBSERVE
    magnitude: float = 0.0
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EffectRecord:
    """A record of an actually-observed effect after dispatch."""
    record_id: str
    grant_id: str
    operator_id: str
    target: str
    observed_at: str
    effect_class: EffectClass = EffectClass.OBSERVE
    magnitude: float = 0.0
    details: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ResourceClaim:
    """A resource requirement for an action operator."""
    resource_type: str  # e.g. "cpu", "memory", "file_lock"
    amount: int = 1
    exclusive: bool = True


# ── OperatorId ──────────────────────────────────────────────────────────────

OperatorId = str


# ── GoalSpec ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GoalSpec:
    """A versioned goal specification with executable success predicates.

    A goal is satisfied only when its success_predicate is true on an
    admitted observation snapshot — never from tool return status.
    """
    goal_id: str
    revision: int
    owner: str
    description: str
    priority: int
    deadline: Optional[str] = None  # ISO-8601 or None
    success_predicate: str = ""  # PredicateRef
    failure_predicate: Optional[str] = None  # PredicateRef or None
    observation_schema: str = ""  # SchemaRef
    allowed_operator_ids: tuple[str, ...] = ()
    effect_class_ceiling: EffectClass = EffectClass.OBSERVE
    status: GoalStatus = GoalStatus.DRAFT

    def __post_init__(self):
        if not self.goal_id:
            object.__setattr__(self, "goal_id", f"goal_{uuid.uuid4().hex[:16]}")


# ── Observation ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Observation:
    """An externally obtained fact with full provenance.

    Beliefs are not observations and cannot directly satisfy success
    predicates unless the predicate explicitly accepts a reviewed derivation.
    """
    observation_id: str
    source: str
    collection_method: str
    timestamp: str  # ISO-8601 UTC
    state_revision: int
    schema_version: str
    content_digest: str
    raw_evidence_ref: str  # path or URI to raw evidence
    content: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.observation_id:
            object.__setattr__(
                self, "observation_id", f"obs_{uuid.uuid4().hex[:16]}"
            )
        if not self.content_digest:
            digest = hashlib.sha256(
                json.dumps(self.content, sort_keys=True).encode()
            ).hexdigest()
            object.__setattr__(self, "content_digest", digest)


# ── Belief ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Belief:
    """A derived PLN claim citing observation and rule provenance."""
    belief_id: str
    claim: str
    truth_value: float  # PLN truth strength [0,1]
    confidence: float   # PLN confidence [0,1]
    observation_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    proof_trace: str = ""

    def __post_init__(self):
        if not self.belief_id:
            object.__setattr__(
                self, "belief_id", f"belief_{uuid.uuid4().hex[:16]}"
            )


# ── ActionOperator ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActionOperator:
    """A typed action operator with preconditions, effects, and compensation.

    PLN may supply beliefs used by preconditions or estimates, but
    implication traversal is not accepted as a plan.
    """
    operator_id: str
    revision: int
    parameter_schema: str  # SchemaRef
    precondition: str      # PredicateRef
    expected_effects: tuple[Effect, ...] = ()
    possible_adverse_effects: tuple[Effect, ...] = ()
    effect_class: EffectClass = EffectClass.OBSERVE
    resource_claims: tuple[ResourceClaim, ...] = ()
    compensation_operator_id: Optional[str] = None
    timeout_s: int = 30
    verification_predicate: str = ""  # PredicateRef

    def __post_init__(self):
        if not self.operator_id:
            object.__setattr__(
                self, "operator_id", f"op_{uuid.uuid4().hex[:16]}"
            )


# ── ActionGrant ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ActionGrant:
    """A single-use, expiring authorization to execute one exact action.

    The dispatcher repeats all reference-monitor checks before invocation;
    it never trusts fields supplied by the planner or LLM.
    """
    grant_id: str
    operator_id: str
    operator_revision: int
    tool_name: str
    canonical_args: str  # JSON-serialized canonical arguments
    target: str
    goal_id: str
    goal_revision: int
    state_revision: int
    idempotency_key: str
    nonce: str
    issued_at: str  # ISO-8601
    expires_at: str  # ISO-8601
    mac: str = ""    # HMAC or signature binding all above fields

    def __post_init__(self):
        if not self.grant_id:
            object.__setattr__(
                self, "grant_id", f"grant_{uuid.uuid4().hex[:16]}"
            )
        if not self.nonce:
            object.__setattr__(self, "nonce", uuid.uuid4().hex)
        if not self.idempotency_key:
            object.__setattr__(
                self, "idempotency_key", f"idem_{uuid.uuid4().hex[:16]}"
            )

    def is_expired(self, now: str | None = None) -> bool:
        """Check if this grant has expired."""
        ts = now or datetime.now(timezone.utc).isoformat()
        return ts > self.expires_at


# ── Plan ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlanStep:
    """A single step in a plan — an instantiated operator."""
    step_id: str
    operator_id: str
    operator_revision: int
    arguments: dict[str, Any]
    target: str
    depends_on: tuple[str, ...] = ()  # step_ids this step depends on

    def __post_init__(self):
        if not self.step_id:
            object.__setattr__(self, "step_id", f"step_{uuid.uuid4().hex[:8]}")


@dataclass(frozen=True)
class Plan:
    """An ordered graph of instantiated operators with causal links.

    Plan validation rejects missing preconditions, unbound parameters,
    cycles not declared as bounded loops, incompatible resources, and
    terminal predicates not entailed by expected effects plus observations.
    """
    plan_id: str
    goal_id: str
    goal_revision: int
    state_revision: int
    steps: tuple[PlanStep, ...]
    terminal_predicate: str  # PredicateRef
    resource_constraints: dict[str, int] = field(default_factory=dict)
    valid: bool = False

    def __post_init__(self):
        if not self.plan_id:
            object.__setattr__(self, "plan_id", f"plan_{uuid.uuid4().hex[:16]}")
# ── Appraisal ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Appraisal:
    """GoalChainer's appraisal of a candidate plan.

    GoalChainer returns an Appraisal, not a binding action.
    It contains deontic status, utility, conflicts, uncertainty, and ranking.
    """
    appraisal_id: str
    plan_id: str
    deontic_status: DeonticStatus
    utility_estimate: float  # [0, 1]
    uncertainty: float      # [0, 1]
    conflicts: tuple[str, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()
    ranking_score: float = 0.0
    notes: str = ""
    authorized: bool = False  # Only the authorization service sets this
    rejection_reason: str = ""  # Populated by plan validator when FORBIDDEN

    def __post_init__(self):
        if not self.appraisal_id:
            object.__setattr__(
                self, "appraisal_id", f"appr_{uuid.uuid4().hex[:16]}"
            )


# ── EventRecord ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EventRecord:
    """An immutable event in the transactional event store.

    Event records are immutable. Corrections append superseding events.
    """
    event_id: str
    event_type: str  # "observation", "action", "belief", "goal", "grant"
    timestamp: str  # ISO-8601 UTC
    revision: int
    payload: str  # JSON-serialized
    prev_event_id: Optional[str] = None  # for superseding events

    def __post_init__(self):
        if not self.event_id:
            object.__setattr__(
                self, "event_id", f"evt_{uuid.uuid4().hex[:16]}"
            )


# ── Helpers ────────────────────────────────────────────────────────────────────

VALID_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PROPOSED: frozenset({ActionState.VALIDATED, ActionState.REJECTED}),
    ActionState.VALIDATED: frozenset({ActionState.AUTHORIZED, ActionState.REJECTED}),
    ActionState.AUTHORIZED: frozenset({ActionState.DISPATCHING, ActionState.REJECTED}),
    ActionState.DISPATCHING: frozenset({ActionState.DISPATCHED, ActionState.FAILED, ActionState.UNKNOWN}),
    ActionState.DISPATCHED: frozenset({ActionState.SUCCEEDED, ActionState.FAILED, ActionState.UNKNOWN}),
    ActionState.SUCCEEDED: frozenset({ActionState.RECONCILED}),
    ActionState.FAILED: frozenset({ActionState.RECONCILED}),
    ActionState.UNKNOWN: frozenset({ActionState.RECONCILED}),
    ActionState.RECONCILED: frozenset({ActionState.VERIFIED, ActionState.CONTRADICTED}),
    ActionState.VERIFIED: frozenset(),  # terminal
    ActionState.CONTRADICTED: frozenset(),  # terminal
    ActionState.REJECTED: frozenset(),  # terminal
}


def is_valid_transition(from_state: ActionState, to_state: ActionState) -> bool:
    """Check if a state transition is allowed by the action lifecycle."""
    return to_state in VALID_TRANSITIONS.get(from_state, frozenset())


def to_dict(obj) -> dict:
    """Serialize a frozen dataclass to a JSON-safe dict, handling enums."""
    d = asdict(obj)
    for k, v in d.items():
        if isinstance(v, Enum):
            d[k] = v.value
    return d


def canonical_args(args: dict) -> str:
    """Serialize arguments in a canonical (sorted) JSON form."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"))


def content_digest(data: Any) -> str:
    """Compute a SHA-256 digest of canonical JSON."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def compute_mac(grant: ActionGrant, secret: bytes) -> str:
    """Compute an HMAC-SHA256 binding all grant fields."""
    msg_parts = "|".join([
        grant.grant_id, grant.operator_id, str(grant.operator_revision),
        grant.tool_name, grant.canonical_args, grant.target,
        grant.goal_id, str(grant.goal_revision), str(grant.state_revision),
        grant.idempotency_key, grant.nonce, grant.issued_at, grant.expires_at,
    ])
    return hmac.new(secret, msg_parts.encode(), hashlib.sha256).hexdigest()


def verify_mac(grant: ActionGrant, secret: bytes) -> bool:
    """Verify a grant's MAC. Returns True if valid."""
    if not grant.mac:
        return False
    expected = compute_mac(
        ActionGrant(
            grant_id=grant.grant_id,
            operator_id=grant.operator_id,
            operator_revision=grant.operator_revision,
            tool_name=grant.tool_name,
            canonical_args=grant.canonical_args,
            target=grant.target,
            goal_id=grant.goal_id,
            goal_revision=grant.goal_revision,
            state_revision=grant.state_revision,
            idempotency_key=grant.idempotency_key,
            nonce=grant.nonce,
            issued_at=grant.issued_at,
            expires_at=grant.expires_at,
            mac="",
        ),
        secret,
    )
    return hmac.compare_digest(grant.mac, expected)


def now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def expires_iso(seconds: int = 30) -> str:
    """Return an ISO-8601 timestamp N seconds from now."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
