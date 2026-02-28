"""
/data/schemas/moderation.py

Canonical Moderation Decision Schemas (Explicit, Traceable, Non-Rewriteable)

This module records what moderation actions were taken — not whether they were
right, fair, or successful.

Design Principle:
    Moderation records are evidence of power being exercised.
    They must never be implicit, inferred, or collapsed.
    
Philosophy:
    Moderation records what power did — not whether power was right.
    Judgment comes later. History must not move.

Responsibilities:
    - Represent flags as observations
    - Represent decisions as explicit acts
    - Preserve actor vs subject separation
    - Encode declared rationale (not truth)
    - Support deterministic replay
    - Be immutable and append-only
    - Never override or supersede history

Forbidden:
    - Policy logic
    - Risk inference
    - Enforcement state
    - Appeal outcomes
    - Trust scoring
    - Retroactive correction
    - Overwriting past decisions
    - Soft-delete moderation
    - Severity weights (judgment quantification)
    - Enforcement mechanics (scopes, durations)

Metadata Governance:
    Metadata fields are minimal canonical atoms for historical context only.
    Strict rules:
        - No hidden enrichment after creation
        - No context smuggling via metadata
        - No post-hoc reinterpretation via added keys
        - Metadata must be set at event creation time
        - All metadata keys must be declared and validated
        - No dynamic metadata expansion
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, final, Tuple, Dict

from base import AccountKind, CanonicalSchema, SchemaValidationError


# ============================================================================
# CONSTANTS
# ============================================================================

MODERATION_SCHEMA_NAME: Final[str] = "moderation"
"""Canonical schema name for all moderation event records."""

SUPPORTED_SCHEMA_VERSIONS: Final[tuple[int, ...]] = (1,)
"""Supported schema versions for moderation event records."""

SYSTEM_ACTOR_MARKER: Final[str] = "system"
"""Marker used in ID computation when actor is system (None account_id)."""


# ============================================================================
# MODERATION TARGET TAXONOMY
# ============================================================================


class ModerationTargetType(Enum):
    """
    Enumeration of moderation target types.
    
    Targets are explicit entities that moderation acts upon.
    No inferred scopes or wildcard targets.
    
    CONTENT: User-generated content (posts, media)
    ACCOUNT: User accounts
    COMMENT: Comments on content
    """

    CONTENT = "content"
    ACCOUNT = "account"
    COMMENT = "comment"

    def get_id_prefix(self) -> str:
        """Get the canonical ID prefix for this target type."""
        return f"{self.value}_"


# ============================================================================
# MODERATION ACTION TAXONOMY
# ============================================================================


class ModerationActionType(Enum):
    """
    Enumeration of moderation action types.
    
    Actions are labels for what was done, not enforcement guarantees.
    Actions record intent, not success.
    
    FLAG: Content/account flagged for review
    STRIKE: Penalty applied to account
    WARNING: Formal warning issued
    RESTRICTION: Access or capability restricted
    REMOVAL: Content or account removed
    RESTORATION: Previously removed content/account restored
    """

    FLAG = "flag"
    STRIKE = "strike"
    WARNING = "warning"
    RESTRICTION = "restriction"
    REMOVAL = "removal"
    RESTORATION = "restoration"

    def is_punitive(self) -> bool:
        """Return True if action is punitive."""
        return self in {
            ModerationActionType.STRIKE,
            ModerationActionType.RESTRICTION,
            ModerationActionType.REMOVAL,
        }

    def is_restorative(self) -> bool:
        """Return True if action is restorative."""
        return self == ModerationActionType.RESTORATION

    def is_observational(self) -> bool:
        """Return True if action is observational only."""
        return self in {
            ModerationActionType.FLAG,
            ModerationActionType.WARNING,
        }


# ============================================================================
# MODERATION DECISION SOURCE
# ============================================================================


class ModerationDecisionSource(Enum):
    """
    Enumeration of moderation decision sources.
    
    Source is always explicit - never inferred.
    
    HUMAN: Decision made by human moderator
    AUTOMATED: Decision made by automated system
    HYBRID: Decision made by human with automated assistance
    EXTERNAL: Decision made by external entity (e.g., legal, platform)
    """

    HUMAN = "human"
    AUTOMATED = "automated"
    HYBRID = "hybrid"
    EXTERNAL = "external"

    def requires_human_actor(self) -> bool:
        """Return True if source requires a human actor account ID."""
        return self in {
            ModerationDecisionSource.HUMAN,
            ModerationDecisionSource.HYBRID,
            ModerationDecisionSource.EXTERNAL,
        }

    def allows_system_actor(self) -> bool:
        """Return True if source allows system actor (no account ID)."""
        return self == ModerationDecisionSource.AUTOMATED


# ============================================================================
# MODERATION ACTOR
# ============================================================================


@dataclass(frozen=True)
class ModerationActor:
    """
    Immutable representation of a moderation actor.
    
    An actor is the entity that made a moderation decision.
    Actors must be explicit - no anonymous humans.
    
    Rules:
        - No anonymous humans (HUMAN source requires account_id)
        - Systems may act, but must admit it (AUTOMATED allows None)
        - No trust inference from actor type
    
    Attributes:
        actor_account_id: Account ID of actor, or None for system
        actor_kind: Kind of account (human, system, etc.)
        decision_source: How the decision was made
        metadata: Optional actor context (strictly governed - set at creation, no post-hoc enrichment)
    """

    actor_account_id: str | None
    actor_kind: AccountKind
    decision_source: ModerationDecisionSource
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate actor invariants at construction time."""
        self._validate_actor_consistency()
        self._validate_metadata()

    def _validate_actor_consistency(self) -> None:
        """
        Validate actor_account_id is consistent with decision_source.
        
        Rules:
            - HUMAN, HYBRID, EXTERNAL sources require actor_account_id
            - AUTOMATED source allows None actor_account_id
        """
        if self.decision_source.requires_human_actor():
            if self.actor_account_id is None:
                raise ValueError(
                    f"Decision source {self.decision_source.value} requires "
                    f"actor_account_id (no anonymous humans)"
                )
            if not isinstance(self.actor_account_id, str):
                raise ValueError(
                    f"actor_account_id must be string, got {type(self.actor_account_id)}"
                )
            if not self.actor_account_id:
                raise ValueError("actor_account_id cannot be empty string")

        # Validate actor_kind
        if not isinstance(self.actor_kind, AccountKind):
            raise ValueError(
                f"actor_kind must be AccountKind, got {type(self.actor_kind)}"
            )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise ValueError(f"metadata must be dict, got {type(self.metadata)}")

    def is_system_actor(self) -> bool:
        """Return True if actor is a system (no account ID)."""
        return self.actor_account_id is None

    def is_human_actor(self) -> bool:
        """Return True if actor is human."""
        return self.decision_source in {
            ModerationDecisionSource.HUMAN,
            ModerationDecisionSource.HYBRID,
            ModerationDecisionSource.EXTERNAL,
        }

    def get_actor_id_for_hash(self) -> str:
        """
        Get actor identifier for hash computation.
        
        Returns:
            actor_account_id or SYSTEM_ACTOR_MARKER
        """
        return self.actor_account_id or SYSTEM_ACTOR_MARKER

    def to_dict(self) -> dict[str, Any]:
        """
        Convert actor to dictionary representation.
        
        Returns:
            Dictionary with deterministic key ordering
        """
        result: dict[str, Any] = {
            "actor_account_id": self.actor_account_id,
            "actor_kind": self.actor_kind.value,
            "decision_source": self.decision_source.value,
        }
        if self.metadata:
            result["metadata"] = dict(sorted(self.metadata.items()))
        return result


# ============================================================================
# MODERATION TARGET
# ============================================================================


@dataclass(frozen=True)
class ModerationTarget:
    """
    Immutable representation of a moderation target.
    
    A target is the entity being moderated (content, account, comment).
    
    Rules:
        - Target must exist elsewhere canonically
        - No wildcard targets
        - No implied scope widening
    
    Attributes:
        target_type: Type of target being moderated
        target_id: Canonical identifier of the target
        metadata: Optional target context (strictly governed - set at creation, no post-hoc enrichment)
    """

    target_type: ModerationTargetType
    target_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate target invariants at construction time."""
        self._validate_target_type()
        self._validate_target_id()
        self._validate_metadata()

    def _validate_target_type(self) -> None:
        """Validate target type."""
        if not isinstance(self.target_type, ModerationTargetType):
            raise ValueError(
                f"target_type must be ModerationTargetType, got {type(self.target_type)}"
            )

    def _validate_target_id(self) -> None:
        """Validate target ID is non-empty string."""
        if not self.target_id:
            raise ValueError("target_id cannot be empty")
        if not isinstance(self.target_id, str):
            raise ValueError(
                f"target_id must be string, got {type(self.target_id)}"
            )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise ValueError(f"metadata must be dict, got {type(self.metadata)}")

    def to_dict(self) -> dict[str, Any]:
        """
        Convert target to dictionary representation.
        
        Returns:
            Dictionary with deterministic key ordering
        """
        result: dict[str, Any] = {
            "target_type": self.target_type.value,
            "target_id": self.target_id,
        }
        if self.metadata:
            result["metadata"] = dict(sorted(self.metadata.items()))
        return result


# ============================================================================
# MODERATION RATIONALE
# ============================================================================


@dataclass(frozen=True)
class ModerationRationale:
    """
    Immutable representation of declared moderation rationale.
    
    Rationale records what was said at the time of the decision,
    not what was true or verified.
    
    Rules:
        - Reason is what was said, not what was true
        - Never normalized or cleaned
        - Never edited
        - Policy code is opaque external reference
        - No severity weights (pure historical recording, not judgment quantification)
    
    Attributes:
        policy_code: Opaque external policy reference
        reason_text: Verbatim explanation as provided
        metadata: Optional rationale context (strictly governed - no post-hoc enrichment)
    """

    policy_code: str
    reason_text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate rationale invariants at construction time."""
        self._validate_policy_code()
        self._validate_reason_text()
        self._validate_metadata()

    def _validate_policy_code(self) -> None:
        """Validate policy code is non-empty string."""
        if not self.policy_code:
            raise ValueError("policy_code cannot be empty")
        if not isinstance(self.policy_code, str):
            raise ValueError(
                f"policy_code must be string, got {type(self.policy_code)}"
            )
        if len(self.policy_code) > 255:
            raise ValueError(
                f"policy_code too long: {len(self.policy_code)} chars (max 255)"
            )

    def _validate_reason_text(self) -> None:
        """Validate reason text is non-empty string."""
        if not self.reason_text:
            raise ValueError("reason_text cannot be empty")
        if not isinstance(self.reason_text, str):
            raise ValueError(
                f"reason_text must be string, got {type(self.reason_text)}"
            )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise ValueError(f"metadata must be dict, got {type(self.metadata)}")

    def to_dict(self) -> dict[str, Any]:
        """
        Convert rationale to dictionary representation.
        
        Returns:
            Dictionary with deterministic key ordering
        """
        result: dict[str, Any] = {
            "policy_code": self.policy_code,
            "reason_text": self.reason_text,
        }
        if self.metadata:
            result["metadata"] = dict(sorted(self.metadata.items()))
        return result


# ============================================================================
# CORE SCHEMA: MODERATION EVENT
# ============================================================================


@final
@dataclass(frozen=True)
class ModerationEvent(CanonicalSchema):
    """
    Atomic moderation event record.
    
    This is the canonical representation of a moderation decision and action.
    It records what power did — not whether power was right.
    
    The record is:
        - Immutable (frozen dataclass)
        - Append-only (never mutated)
        - Deterministic (same inputs = same ID)
        - Explicit (all actors, targets, rationale visible)
        - Historical (captures what was said at the time)
    
    Appeals create new events — not edits.
    Restorations never delete removals.
    Time never rewinds.
    
    Attributes:
        moderation_id: Deterministic event identifier
        schema_name: Always "moderation"
        schema_version: Schema version (currently 1)
        action: What moderation action was taken
        actor: Who made the decision
        target: What was moderated
        rationale: Why the decision was made (declared)
        occurred_at: When the decision occurred (milliseconds)
        metadata: Optional additional context (strictly governed - no post-hoc enrichment)
    
    Note:
        Actions are labels, not enforcement guarantees.
        This schema records decisions, NOT enforcement mechanics.
        Enforcement semantics belong in separate enforcement systems.
    """

    # Identity
    moderation_id: str
    schema_name: str
    schema_version: int

    # Classification
    action: ModerationActionType

    # Participants
    actor: ModerationActor
    target: ModerationTarget

    # Declared rationale
    rationale: ModerationRationale

    # Timing
    occurred_at: int

    # Optional context
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate all moderation event invariants."""
        self._validate_schema_identity()
        self._validate_moderation_id()
        self._validate_action()
        self._validate_actor()
        self._validate_target()
        self._validate_rationale()
        self._validate_occurred_at()
        self._validate_metadata()

    # ========================================================================
    # VALIDATION METHODS
    # ========================================================================

    def _validate_schema_identity(self) -> None:
        """Validate schema name and version."""
        if self.schema_name != MODERATION_SCHEMA_NAME:
            raise SchemaValidationError(
                f"schema_name must be '{MODERATION_SCHEMA_NAME}', "
                f"got '{self.schema_name}'"
            )
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise SchemaValidationError(
                f"schema_version {self.schema_version} not in "
                f"supported versions {SUPPORTED_SCHEMA_VERSIONS}"
            )

    def _validate_moderation_id(self) -> None:
        """Validate moderation ID."""
        if not self.moderation_id:
            raise SchemaValidationError("moderation_id cannot be empty")
        if not isinstance(self.moderation_id, str):
            raise SchemaValidationError(
                f"moderation_id must be string, got {type(self.moderation_id)}"
            )
        # Verify deterministic ID format
        expected_id = self.compute_moderation_id(
            target_id=self.target.target_id,
            action=self.action,
            actor_id=self.actor.get_actor_id_for_hash(),
            occurred_at=self.occurred_at,
        )
        if self.moderation_id != expected_id:
            raise SchemaValidationError(
                f"moderation_id mismatch: expected {expected_id}, "
                f"got {self.moderation_id}"
            )

    def _validate_action(self) -> None:
        """Validate action type."""
        if not isinstance(self.action, ModerationActionType):
            raise SchemaValidationError(
                f"action must be ModerationActionType, got {type(self.action)}"
            )

    def _validate_actor(self) -> None:
        """Validate actor."""
        if not isinstance(self.actor, ModerationActor):
            raise SchemaValidationError(
                f"actor must be ModerationActor, got {type(self.actor)}"
            )

    def _validate_target(self) -> None:
        """Validate target."""
        if not isinstance(self.target, ModerationTarget):
            raise SchemaValidationError(
                f"target must be ModerationTarget, got {type(self.target)}"
            )

    def _validate_rationale(self) -> None:
        """Validate rationale."""
        if not isinstance(self.rationale, ModerationRationale):
            raise SchemaValidationError(
                f"rationale must be ModerationRationale, got {type(self.rationale)}"
            )

    def _validate_occurred_at(self) -> None:
        """Validate occurred_at timestamp."""
        if not isinstance(self.occurred_at, int):
            raise SchemaValidationError(
                f"occurred_at must be int, got {type(self.occurred_at)}"
            )
        if self.occurred_at < 0:
            raise SchemaValidationError(
                f"occurred_at must be >= 0, got {self.occurred_at}"
            )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise SchemaValidationError(
                f"metadata must be dict, got {type(self.metadata)}"
            )

    # ========================================================================
    # DETERMINISTIC ID COMPUTATION
    # ========================================================================

    @staticmethod
    def compute_moderation_id(
        target_id: str,
        action: ModerationActionType,
        actor_id: str,
        occurred_at: int,
    ) -> str:
        """
        Compute deterministic moderation ID.
        
        ID is derived from:
            - target_id
            - action
            - actor_id (or SYSTEM_ACTOR_MARKER)
            - occurred_at
        
        Same inputs always produce same ID (replay-safe).
        
        Args:
            target_id: Target identifier
            action: Moderation action type
            actor_id: Actor identifier or SYSTEM_ACTOR_MARKER
            occurred_at: Event timestamp
            
        Returns:
            Deterministic SHA-256 hash as moderation_id
        """
        hasher = hashlib.sha256()

        # Target
        hasher.update(target_id.encode("utf-8"))

        # Action
        hasher.update(action.value.encode("utf-8"))

        # Actor
        hasher.update(actor_id.encode("utf-8"))

        # Timestamp
        hasher.update(str(occurred_at).encode("utf-8"))

        return f"moderation_{hasher.hexdigest()}"

    # ========================================================================
    # CANONICAL SCHEMA IMPLEMENTATION
    # ========================================================================

    def validate(self) -> None:
        """
        Validate the moderation event.
        
        This method is called by the CanonicalSchema protocol.
        All validation is performed in __post_init__, so this
        is a no-op for frozen dataclasses.
        
        Raises:
            SchemaValidationError: If validation fails
        """
        # Validation already performed in __post_init__
        pass

    def to_dict(self) -> dict[str, Any]:
        """
        Convert moderation event to dictionary with canonical ordering.
        
        Ordering:
            1. Identity (moderation_id, schema_name, schema_version)
            2. Action (action)
            3. Actor (actor)
            4. Target (target)
            5. Rationale (rationale)
            6. Timing (occurred_at)
            7. Metadata (metadata)
        
        Returns:
            Dictionary with deterministic key ordering and bit-stable values
        """
        result: dict[str, Any] = {
            # Identity
            "moderation_id": self.moderation_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            # Action
            "action": self.action.value,
            # Actor
            "actor": self.actor.to_dict(),
            # Target
            "target": self.target.to_dict(),
            # Rationale
            "rationale": self.rationale.to_dict(),
            # Timing
            "occurred_at": self.occurred_at,
        }

        # Metadata
        if self.metadata:
            result["metadata"] = dict(sorted(self.metadata.items()))

        return result

    def to_json(self) -> str:
        """
        Serialize to deterministic JSON string.
        
        Returns:
            Canonical JSON representation (bit-stable)
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        """
        Compute deterministic hash of the moderation event.
        
        Returns:
            SHA-256 hash of canonical JSON representation
        """
        canonical_json = self.to_json()
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    # ========================================================================
    # QUERY METHODS
    # ========================================================================

    def is_punitive(self) -> bool:
        """Return True if action is punitive."""
        return self.action.is_punitive()

    def is_restorative(self) -> bool:
        """Return True if action is restorative."""
        return self.action.is_restorative()

    def is_automated(self) -> bool:
        """Return True if decision was fully automated."""
        return self.actor.decision_source == ModerationDecisionSource.AUTOMATED

    def is_human_decision(self) -> bool:
        """Return True if human was involved in decision."""
        return self.actor.is_human_actor()

    def get_target_type(self) -> ModerationTargetType:
        """Get target type."""
        return self.target.target_type

    def get_target_id(self) -> str:
        """Get target identifier."""
        return self.target.target_id

    def get_policy_code(self) -> str:
        """Get policy code from rationale."""
        return self.rationale.policy_code

    def get_actor_id(self) -> str | None:
        """Get actor account ID (None for system)."""
        return self.actor.actor_account_id


# ============================================================================
# MODERATION EVENT BUILDER
# ============================================================================


class ModerationEventBuilder:
    """
    Builder for constructing ModerationEvent instances.
    
    Provides a fluent interface for building moderation events
    with validation at each step.
    """

    def __init__(
        self,
        action: ModerationActionType,
        target_type: ModerationTargetType,
        target_id: str,
        occurred_at: int,
    ) -> None:
        """
        Initialize builder with required fields.
        
        Args:
            action: Moderation action type
            target_type: Target type
            target_id: Target identifier
            occurred_at: Event timestamp
        """
        self._action = action
        self._target_type = target_type
        self._target_id = target_id
        self._occurred_at = occurred_at
        self._actor: ModerationActor | None = None
        self._rationale: ModerationRationale | None = None
        self._target_metadata: dict[str, Any] = {}
        self._event_metadata: dict[str, Any] = {}

    def set_actor(
        self,
        actor_account_id: str | None,
        actor_kind: AccountKind,
        decision_source: ModerationDecisionSource,
        metadata: dict[str, Any] | None = None,
    ) -> ModerationEventBuilder:
        """Set moderation actor."""
        self._actor = ModerationActor(
            actor_account_id=actor_account_id,
            actor_kind=actor_kind,
            decision_source=decision_source,
            metadata=metadata or {},
        )
        return self

    def set_rationale(
        self,
        policy_code: str,
        reason_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> ModerationEventBuilder:
        """Set moderation rationale."""
        self._rationale = ModerationRationale(
            policy_code=policy_code,
            reason_text=reason_text,
            metadata=metadata or {},
        )
        return self

    def add_target_metadata(
        self, key: str, value: Any
    ) -> ModerationEventBuilder:
        """Add target metadata entry."""
        self._target_metadata[key] = value
        return self

    def add_event_metadata(
        self, key: str, value: Any
    ) -> ModerationEventBuilder:
        """Add event metadata entry."""
        self._event_metadata[key] = value
        return self

    def build(self) -> ModerationEvent:
        """
        Build the ModerationEvent.
        
        Returns:
            Validated ModerationEvent instance
            
        Raises:
            ValueError: If required fields are missing
        """
        if self._actor is None:
            raise ValueError("Actor is required")
        if self._rationale is None:
            raise ValueError("Rationale is required")

        # Create target
        target = ModerationTarget(
            target_type=self._target_type,
            target_id=self._target_id,
            metadata=self._target_metadata,
        )

        # Compute deterministic ID
        moderation_id = ModerationEvent.compute_moderation_id(
            target_id=self._target_id,
            action=self._action,
            actor_id=self._actor.get_actor_id_for_hash(),
            occurred_at=self._occurred_at,
        )

        return ModerationEvent(
            moderation_id=moderation_id,
            schema_name=MODERATION_SCHEMA_NAME,
            schema_version=1,
            action=self._action,
            actor=self._actor,
            target=target,
            rationale=self._rationale,
            occurred_at=self._occurred_at,
            metadata=self._event_metadata,
        )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Constants
    "MODERATION_SCHEMA_NAME",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SYSTEM_ACTOR_MARKER",
    # Enums
    "ModerationTargetType",
    "ModerationActionType",
    "ModerationDecisionSource",
    # Classes
    "ModerationActor",
    "ModerationTarget",
    "ModerationRationale",
    "ModerationEvent",
    "ModerationEventBuilder",
]