"""
/data/schemas/engagement.py

Canonical Engagement Measurement Schemas (Facts Only, No Meaning)

This module defines raw, structural measurements of interaction with content.
It answers: "What events objectively occurred between an actor and a piece of content?"

It does NOT answer:
- Whether engagement was "good"
- Whether it was legitimate
- Whether it counts
- Whether it should be rewarded
- Whether it was suppressed or boosted

Those are system judgments, not data truth.

Design Principle:
    Engagement is an event, not a verdict.

Authority Level: CANONICAL MEASUREMENT
The system may later accept, discount, or ignore engagement,
but it must always be able to prove what was observed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union


# ============================================================================
# BASE IMPORTS (from other schema files)
# ============================================================================


class CanonicalSchema:
    """Base class for all canonical schemas.
    
    All schemas must:
    - Be immutable (frozen dataclass)
    - Have deterministic hashing
    - Support validation
    - Have canonical serialization
    """
    
    def validate(self) -> None:
        """Validate schema integrity.
        
        Raises:
            SchemaValidationError: If validation fails
        """
        raise NotImplementedError("Subclasses must implement validate()")
    
    def to_canonical_dict(self) -> Dict[str, Any]:
        """Convert to canonical dictionary with stable ordering.
        
        Returns:
            Ordered dictionary representation
        """
        raise NotImplementedError("Subclasses must implement to_canonical_dict()")
    
    def get_canonical_hash(self) -> str:
        """Get deterministic hash of canonical representation.
        
        Returns:
            SHA256 hash of canonical form
        """
        canonical_json = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


# ============================================================================
# EXCEPTIONS
# ============================================================================


class EngagementSchemaError(Exception):
    """Base exception for engagement schema errors."""
    pass


class SchemaValidationError(EngagementSchemaError):
    """Raised when schema validation fails."""
    pass


class InvalidEngagementKindError(EngagementSchemaError):
    """Raised when engagement kind is invalid."""
    pass


class InvalidActorError(EngagementSchemaError):
    """Raised when actor configuration is invalid."""
    pass


class InvalidTargetError(EngagementSchemaError):
    """Raised when target configuration is invalid."""
    pass


class DeterminismViolation(EngagementSchemaError):
    """Raised when determinism guarantees are violated."""
    pass


class MetadataBoundsViolation(EngagementSchemaError):
    """Raised when metadata exceeds bounds."""
    pass


# ============================================================================
# ENUMS
# ============================================================================


class EngagementKind(Enum):
    """Enumeration of engagement types.
    
    Kinds are labels, not weights.
    No implicit ranking or scoring.
    """
    
    VIEW = "view"
    LIKE = "like"
    SHARE = "share"
    COMMENT = "comment"
    FOLLOW = "follow"
    SAVE = "save"
    BOOKMARK = "bookmark"
    REPLY = "reply"
    REPOST = "repost"
    QUOTE = "quote"
    MENTION = "mention"
    SUBSCRIBE = "subscribe"
    
    def allows_duration(self) -> bool:
        """Check if this engagement kind allows duration measurement.
        
        Returns:
            True only for VIEW
        """
        return self == EngagementKind.VIEW
    
    def requires_duration(self) -> bool:
        """Check if this engagement kind requires duration.
        
        Returns:
            True only for VIEW
        """
        return self == EngagementKind.VIEW
    
    def is_temporal(self) -> bool:
        """Check if this engagement has temporal extent.
        
        Returns:
            True for kinds that measure time
        """
        return self in {EngagementKind.VIEW}
    
    def is_instantaneous(self) -> bool:
        """Check if this engagement is instantaneous.
        
        Returns:
            True for point-in-time events
        """
        return not self.is_temporal()


class AccountKind(Enum):
    """Enumeration of account types.
    
    Explicit classification of actor types.
    Bots, systems, and services are represented, not hidden.
    """
    
    HUMAN = "human"
    BOT = "bot"
    SERVICE = "service"
    SYSTEM = "system"
    ANONYMOUS = "anonymous"
    ORGANIZATION = "organization"
    
    def is_automated(self) -> bool:
        """Check if this account kind is automated.
        
        Returns:
            True for bots, services, systems
        """
        return self in {
            AccountKind.BOT,
            AccountKind.SERVICE,
            AccountKind.SYSTEM,
        }
    
    def is_identifiable(self) -> bool:
        """Check if this account kind is identifiable.
        
        Returns:
            False only for anonymous
        """
        return self != AccountKind.ANONYMOUS


class ContentKind(Enum):
    """Enumeration of content types.
    
    Must match definitions in /data/schemas/content.py
    """
    
    POST = "post"
    VIDEO = "video"
    IMAGE = "image"
    ARTICLE = "article"
    AUDIO = "audio"
    STREAM = "stream"
    STORY = "story"
    POLL = "poll"
    EVENT = "event"
    PRODUCT = "product"
    
    def is_temporal_content(self) -> bool:
        """Check if content has temporal duration.
        
        Returns:
            True for video, audio, stream
        """
        return self in {
            ContentKind.VIDEO,
            ContentKind.AUDIO,
            ContentKind.STREAM,
        }


# ============================================================================
# ENGAGEMENT ACTOR MODEL
# ============================================================================


@dataclass(frozen=True)
class EngagementActor:
    """Immutable representation of an engagement actor.
    
    Rules:
    - Bots, systems, services are explicit
    - Anonymity is represented, not inferred
    - No trust judgment here
    """
    
    account_id: Optional[str]
    actor_kind: AccountKind
    
    def __post_init__(self):
        """Validate actor configuration."""
        # Validate account_id presence matches actor_kind
        if self.actor_kind == AccountKind.ANONYMOUS:
            if self.account_id is not None:
                raise InvalidActorError(
                    f"Anonymous actors cannot have account_id: {self.account_id}"
                )
        else:
            if self.account_id is None:
                raise InvalidActorError(
                    f"Non-anonymous actor kind {self.actor_kind} requires account_id"
                )
            
            # Validate account_id format
            if not self._is_valid_account_id(self.account_id):
                raise InvalidActorError(
                    f"Invalid account_id format: {self.account_id}"
                )
    
    def _is_valid_account_id(self, account_id: str) -> bool:
        """Validate account ID format.
        
        Args:
            account_id: Account ID to validate
            
        Returns:
            True if valid
        """
        if not account_id:
            return False
        
        # Must be alphanumeric with underscores/hyphens, 1-128 chars
        pattern = r'^[a-zA-Z0-9_-]{1,128}$'
        return bool(re.match(pattern, account_id))
    
    def is_anonymous(self) -> bool:
        """Check if actor is anonymous."""
        return self.actor_kind == AccountKind.ANONYMOUS
    
    def is_automated(self) -> bool:
        """Check if actor is automated."""
        return self.actor_kind.is_automated()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "account_id": self.account_id,
            "actor_kind": self.actor_kind.value,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> EngagementActor:
        """Create from dictionary."""
        return cls(
            account_id=d.get("account_id"),
            actor_kind=AccountKind(d["actor_kind"]),
        )
    
    @classmethod
    def anonymous(cls) -> EngagementActor:
        """Create anonymous actor."""
        return cls(account_id=None, actor_kind=AccountKind.ANONYMOUS)
    
    @classmethod
    def human(cls, account_id: str) -> EngagementActor:
        """Create human actor."""
        return cls(account_id=account_id, actor_kind=AccountKind.HUMAN)
    
    @classmethod
    def bot(cls, account_id: str) -> EngagementActor:
        """Create bot actor."""
        return cls(account_id=account_id, actor_kind=AccountKind.BOT)
    
    @classmethod
    def service(cls, account_id: str) -> EngagementActor:
        """Create service actor."""
        return cls(account_id=account_id, actor_kind=AccountKind.SERVICE)
    
    def __str__(self) -> str:
        if self.is_anonymous():
            return f"Actor(anonymous)"
        return f"Actor({self.actor_kind.value}:{self.account_id})"


# ============================================================================
# ENGAGEMENT TARGET MODEL
# ============================================================================


@dataclass(frozen=True)
class EngagementTarget:
    """Immutable representation of engagement target.
    
    Rules:
    - Target must exist in /data/schemas/content.py
    - No derived or proxy IDs
    """
    
    content_id: str
    content_kind: ContentKind
    
    def __post_init__(self):
        """Validate target configuration."""
        if not self.content_id:
            raise InvalidTargetError("content_id cannot be empty")
        
        if not self._is_valid_content_id(self.content_id):
            raise InvalidTargetError(
                f"Invalid content_id format: {self.content_id}"
            )
    
    def _is_valid_content_id(self, content_id: str) -> bool:
        """Validate content ID format.
        
        Args:
            content_id: Content ID to validate
            
        Returns:
            True if valid
        """
        if not content_id:
            return False
        
        # Must be alphanumeric with underscores/hyphens, 1-256 chars
        pattern = r'^[a-zA-Z0-9_-]{1,256}$'
        return bool(re.match(pattern, content_id))
    
    def is_temporal_content(self) -> bool:
        """Check if target is temporal content."""
        return self.content_kind.is_temporal_content()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "content_id": self.content_id,
            "content_kind": self.content_kind.value,
        }
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> EngagementTarget:
        """Create from dictionary."""
        return cls(
            content_id=d["content_id"],
            content_kind=ContentKind(d["content_kind"]),
        )
    
    def __str__(self) -> str:
        return f"Target({self.content_kind.value}:{self.content_id})"


# ============================================================================
# ENGAGEMENT ID GENERATOR
# ============================================================================


class EngagementIdGenerator:
    """Deterministic engagement ID generator.
    
    Rules:
    - No random UUIDs
    - No sequence numbers
    - Same observation → same ID
    - Prevents silent duplication
    """
    
    @classmethod
    def generate(
        cls,
        actor: EngagementActor,
        target: EngagementTarget,
        kind: EngagementKind,
        occurred_at: int,
    ) -> str:
        """Generate deterministic engagement ID.
        
        Args:
            actor: Engagement actor
            target: Engagement target
            kind: Engagement kind
            occurred_at: Timestamp when engagement occurred
            
        Returns:
            Deterministic engagement ID
        """
        # Build canonical components
        # CRITICAL: Include actor_kind and content_kind to prevent semantic collisions
        # Two actors with same account_id but different kinds must produce different IDs
        actor_str = actor.account_id if actor.account_id else "anonymous"
        components = [
            f"actor={actor_str}",
            f"actor_kind={actor.actor_kind.value}",
            f"target={target.content_id}",
            f"content_kind={target.content_kind.value}",
            f"kind={kind.value}",
            f"ts={occurred_at}",
        ]
        
        # Hash components
        canonical = "::".join(components)
        hash_digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        
        # Format: eng_{kind}_{timestamp}_{hash}
        engagement_id = f"eng_{kind.value}_{occurred_at}_{hash_digest[:16]}"
        
        return engagement_id
    
    @classmethod
    def verify(
        cls,
        engagement_id: str,
        actor: EngagementActor,
        target: EngagementTarget,
        kind: EngagementKind,
        occurred_at: int,
    ) -> bool:
        """Verify engagement ID matches parameters.
        
        Args:
            engagement_id: ID to verify
            actor: Expected actor
            target: Expected target
            kind: Expected kind
            occurred_at: Expected timestamp
            
        Returns:
            True if ID is valid for parameters
        """
        expected = cls.generate(actor, target, kind, occurred_at)
        return engagement_id == expected


# ============================================================================
# METADATA VALIDATOR
# ============================================================================


class MetadataValidator:
    """Validator for engagement metadata.
    
    Metadata is bounded key-value, not freeform blobs.
    """
    
    MAX_METADATA_PAIRS = 50
    MAX_KEY_LENGTH = 128
    MAX_VALUE_LENGTH = 1024
    
    KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_.-]{1,128}$')
    
    @classmethod
    def validate(cls, metadata: Tuple[Tuple[str, str], ...]) -> None:
        """Validate metadata bounds and format.
        
        Args:
            metadata: Metadata tuples
            
        Raises:
            MetadataBoundsViolation: If metadata violates bounds
        """
        if not isinstance(metadata, tuple):
            raise MetadataBoundsViolation(
                f"Metadata must be tuple, got {type(metadata)}"
            )
        
        if len(metadata) > cls.MAX_METADATA_PAIRS:
            raise MetadataBoundsViolation(
                f"Metadata exceeds maximum pairs: {len(metadata)} > {cls.MAX_METADATA_PAIRS}"
            )
        
        seen_keys = set()
        
        for i, item in enumerate(metadata):
            if not isinstance(item, tuple) or len(item) != 2:
                raise MetadataBoundsViolation(
                    f"Metadata[{i}] must be 2-tuple, got {item}"
                )
            
            key, value = item
            
            if not isinstance(key, str) or not isinstance(value, str):
                raise MetadataBoundsViolation(
                    f"Metadata[{i}] must have string key and value"
                )
            
            # Validate key format
            if not cls.KEY_PATTERN.match(key):
                raise MetadataBoundsViolation(
                    f"Invalid metadata key format: {key}"
                )
            
            # Validate key length
            if len(key) > cls.MAX_KEY_LENGTH:
                raise MetadataBoundsViolation(
                    f"Metadata key too long: {len(key)} > {cls.MAX_KEY_LENGTH}"
                )
            
            # Validate value length
            if len(value) > cls.MAX_VALUE_LENGTH:
                raise MetadataBoundsViolation(
                    f"Metadata value too long: {len(value)} > {cls.MAX_VALUE_LENGTH}"
                )
            
            # Check for duplicate keys
            if key in seen_keys:
                raise MetadataBoundsViolation(
                    f"Duplicate metadata key: {key}"
                )
            seen_keys.add(key)
    
    @classmethod
    def normalize(cls, metadata: Tuple[Tuple[str, str], ...]) -> Tuple[Tuple[str, str], ...]:
        """Normalize metadata for canonical ordering.
        
        Args:
            metadata: Metadata tuples
            
        Returns:
            Sorted metadata tuples
        """
        return tuple(sorted(metadata, key=lambda x: x[0]))


# ============================================================================
# CORE SCHEMA: ENGAGEMENT EVENT
# ============================================================================


@dataclass(frozen=True)
class EngagementEvent(CanonicalSchema):
    """Canonical engagement event schema.
    
    Records raw, structural measurements of interaction with content.
    
    Stable field order (for canonical serialization):
    1. Identity (engagement_id, schema_name, schema_version)
    2. Classification (kind)
    3. Relationships (actor, target)
    4. Observation (occurred_at, duration_ms)
    5. Metadata
    """
    
    # Identity
    engagement_id: str
    schema_name: str
    schema_version: int
    
    # Classification
    kind: EngagementKind
    
    # Relationships
    actor: EngagementActor
    target: EngagementTarget
    
    # Raw observation
    occurred_at: int  # Monotonic timestamp in milliseconds since epoch
    duration_ms: Optional[int]  # Only for VIEW
    
    # Bounded metadata
    metadata: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    
    def __post_init__(self):
        """Validate engagement event after initialization."""
        self.validate()
    
    def validate(self) -> None:
        """Validate engagement event.
        
        Raises:
            SchemaValidationError: If validation fails
        """
        # Validate schema identity
        if self.schema_name != "engagement":
            raise SchemaValidationError(
                f"schema_name must be 'engagement', got '{self.schema_name}'"
            )
        
        if self.schema_version not in {1, 2}:
            raise SchemaValidationError(
                f"Unsupported schema_version: {self.schema_version}"
            )
        
        # Validate engagement_id format
        if not self.engagement_id.startswith("eng_"):
            raise SchemaValidationError(
                f"engagement_id must start with 'eng_': {self.engagement_id}"
            )
        
        # Validate kind
        if not isinstance(self.kind, EngagementKind):
            raise SchemaValidationError(
                f"kind must be EngagementKind: {type(self.kind)}"
            )
        
        # Validate duration rules
        if self.kind.allows_duration():
            # VIEW can have duration
            if self.duration_ms is not None:
                if not isinstance(self.duration_ms, int):
                    raise SchemaValidationError(
                        f"duration_ms must be int: {type(self.duration_ms)}"
                    )
                if self.duration_ms < 0:
                    raise SchemaValidationError(
                        f"duration_ms must be non-negative: {self.duration_ms}"
                    )
        else:
            # Non-VIEW must not have duration
            if self.duration_ms is not None:
                raise SchemaValidationError(
                    f"duration_ms only allowed for VIEW, got {self.kind.value}"
                )
        
        # Validate timestamp
        if not isinstance(self.occurred_at, int):
            raise SchemaValidationError(
                f"occurred_at must be int: {type(self.occurred_at)}"
            )
        
        if self.occurred_at <= 0:
            raise SchemaValidationError(
                f"occurred_at must be positive: {self.occurred_at}"
            )
        
        # Validate metadata
        MetadataValidator.validate(self.metadata)
        
        # Verify engagement_id is deterministic
        expected_id = EngagementIdGenerator.generate(
            self.actor,
            self.target,
            self.kind,
            self.occurred_at,
        )
        
        if self.engagement_id != expected_id:
            raise DeterminismViolation(
                f"engagement_id is not deterministic.\n"
                f"Expected: {expected_id}\n"
                f"Got: {self.engagement_id}"
            )
    
    def to_canonical_dict(self) -> Dict[str, Any]:
        """Convert to canonical dictionary with stable ordering.
        
        Returns:
            Ordered dictionary representation
        """
        # Stable field order
        result = {
            # 1. Identity
            "engagement_id": self.engagement_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            
            # 2. Classification
            "kind": self.kind.value,
            
            # 3. Relationships
            "actor": self.actor.to_dict(),
            "target": self.target.to_dict(),
            
            # 4. Observation
            "occurred_at": self.occurred_at,
        }
        
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        
        # 5. Metadata (sorted)
        if self.metadata:
            result["metadata"] = dict(MetadataValidator.normalize(self.metadata))
        
        return result
    
    def to_json(self) -> str:
        """Convert to JSON string.
        
        Returns:
            JSON representation
        """
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        )
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> EngagementEvent:
        """Create from dictionary.
        
        Args:
            d: Dictionary representation
            
        Returns:
            EngagementEvent instance
        """
        # Parse metadata
        metadata_dict = d.get("metadata", {})
        metadata = tuple(sorted(metadata_dict.items(), key=lambda x: x[0]))
        
        return cls(
            engagement_id=d["engagement_id"],
            schema_name=d["schema_name"],
            schema_version=d["schema_version"],
            kind=EngagementKind(d["kind"]),
            actor=EngagementActor.from_dict(d["actor"]),
            target=EngagementTarget.from_dict(d["target"]),
            occurred_at=d["occurred_at"],
            duration_ms=d.get("duration_ms"),
            metadata=metadata,
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> EngagementEvent:
        """Create from JSON string.
        
        Args:
            json_str: JSON representation
            
        Returns:
            EngagementEvent instance
        """
        d = json.loads(json_str)
        return cls.from_dict(d)
    
    def is_view(self) -> bool:
        """Check if this is a VIEW engagement."""
        return self.kind == EngagementKind.VIEW
    
    def is_anonymous(self) -> bool:
        """Check if actor is anonymous."""
        return self.actor.is_anonymous()
    
    def is_automated(self) -> bool:
        """Check if actor is automated."""
        return self.actor.is_automated()
    
    def get_metadata_value(self, key: str) -> Optional[str]:
        """Get metadata value by key.
        
        Args:
            key: Metadata key
            
        Returns:
            Metadata value or None
        """
        for k, v in self.metadata:
            if k == key:
                return v
        return None
    
    def __str__(self) -> str:
        return (
            f"EngagementEvent({self.kind.value}: "
            f"{self.actor} -> {self.target} "
            f"@ {self.occurred_at})"
        )


# ============================================================================
# RETENTION OBSERVATION
# ============================================================================


@dataclass(frozen=True)
class RetentionObservation(CanonicalSchema):
    """Retention observation schema.
    
    Retention is not engagement — it is time-based observation.
    
    Rules:
    - No percentages stored
    - No bucketing
    - No truncation
    - Ratios are computed elsewhere
    """
    
    # Identity
    observation_id: str
    schema_name: str
    schema_version: int
    
    # Target
    content_id: str
    content_kind: ContentKind
    
    # Actor
    actor: EngagementActor
    
    # Observation values (raw milliseconds, no percentages)
    watched_ms: int
    content_duration_ms: int
    
    # Timing
    occurred_at: int  # When observation was recorded
    
    # Bounded metadata
    metadata: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    
    def __post_init__(self):
        """Validate retention observation after initialization."""
        self.validate()
    
    def validate(self) -> None:
        """Validate retention observation.
        
        Raises:
            SchemaValidationError: If validation fails
        """
        # Validate schema identity
        if self.schema_name != "retention":
            raise SchemaValidationError(
                f"schema_name must be 'retention', got '{self.schema_name}'"
            )
        
        if self.schema_version not in {1, 2}:
            raise SchemaValidationError(
                f"Unsupported schema_version: {self.schema_version}"
            )
        
        # Validate observation_id format
        if not self.observation_id.startswith("ret_"):
            raise SchemaValidationError(
                f"observation_id must start with 'ret_': {self.observation_id}"
            )
        
        # Validate content_id
        if not self.content_id:
            raise SchemaValidationError("content_id cannot be empty")
        
        # Validate watched_ms
        if not isinstance(self.watched_ms, int):
            raise SchemaValidationError(
                f"watched_ms must be int: {type(self.watched_ms)}"
            )
        
        if self.watched_ms < 0:
            raise SchemaValidationError(
                f"watched_ms must be non-negative: {self.watched_ms}"
            )
        
        # Validate content_duration_ms
        if not isinstance(self.content_duration_ms, int):
            raise SchemaValidationError(
                f"content_duration_ms must be int: {type(self.content_duration_ms)}"
            )
        
        if self.content_duration_ms <= 0:
            raise SchemaValidationError(
                f"content_duration_ms must be positive: {self.content_duration_ms}"
            )
        
        # Validate watched_ms <= content_duration_ms (can watch beyond for loops, but flag suspicious)
        # Note: We don't enforce this strictly as content can loop
        
        # Validate timestamp
        if not isinstance(self.occurred_at, int):
            raise SchemaValidationError(
                f"occurred_at must be int: {type(self.occurred_at)}"
            )
        
        if self.occurred_at <= 0:
            raise SchemaValidationError(
                f"occurred_at must be positive: {self.occurred_at}"
            )
        
        # Validate metadata
        MetadataValidator.validate(self.metadata)
        
        # Verify observation_id is deterministic
        expected_id = self._generate_observation_id()
        if self.observation_id != expected_id:
            raise DeterminismViolation(
                f"observation_id is not deterministic.\n"
                f"Expected: {expected_id}\n"
                f"Got: {self.observation_id}"
            )
    
    def _generate_observation_id(self) -> str:
        """Generate deterministic observation ID.
        
        Returns:
            Deterministic observation ID
        """
        # CRITICAL: Include actor_kind and content_kind to prevent semantic collisions
        actor_str = self.actor.account_id if self.actor.account_id else "anonymous"
        components = [
            f"actor={actor_str}",
            f"actor_kind={self.actor.actor_kind.value}",
            f"content={self.content_id}",
            f"content_kind={self.content_kind.value}",
            f"watched={self.watched_ms}",
            f"duration={self.content_duration_ms}",
            f"ts={self.occurred_at}",
        ]
        
        canonical = "::".join(components)
        hash_digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        
        return f"ret_{self.occurred_at}_{hash_digest[:16]}"
    
    def to_canonical_dict(self) -> Dict[str, Any]:
        """Convert to canonical dictionary with stable ordering.
        
        Returns:
            Ordered dictionary representation
        """
        result = {
            # Identity
            "observation_id": self.observation_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            
            # Target
            "content_id": self.content_id,
            "content_kind": self.content_kind.value,
            
            # Actor
            "actor": self.actor.to_dict(),
            
            # Observation
            "watched_ms": self.watched_ms,
            "content_duration_ms": self.content_duration_ms,
            
            # Timing
            "occurred_at": self.occurred_at,
        }
        
        # Metadata (sorted)
        if self.metadata:
            result["metadata"] = dict(MetadataValidator.normalize(self.metadata))
        
        return result
    
    def to_json(self) -> str:
        """Convert to JSON string.
        
        Returns:
            JSON representation
        """
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True,
        )
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> RetentionObservation:
        """Create from dictionary.
        
        Args:
            d: Dictionary representation
            
        Returns:
            RetentionObservation instance
        """
        # Parse metadata
        metadata_dict = d.get("metadata", {})
        metadata = tuple(sorted(metadata_dict.items(), key=lambda x: x[0]))
        
        return cls(
            observation_id=d["observation_id"],
            schema_name=d["schema_name"],
            schema_version=d["schema_version"],
            content_id=d["content_id"],
            content_kind=ContentKind(d["content_kind"]),
            actor=EngagementActor.from_dict(d["actor"]),
            watched_ms=d["watched_ms"],
            content_duration_ms=d["content_duration_ms"],
            occurred_at=d["occurred_at"],
            metadata=metadata,
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> RetentionObservation:
        """Create from JSON string.
        
        Args:
            json_str: JSON representation
            
        Returns:
            RetentionObservation instance
        """
        d = json.loads(json_str)
        return cls.from_dict(d)
    
    def get_metadata_value(self, key: str) -> Optional[str]:
        """Get metadata value by key.
        
        Args:
            key: Metadata key
            
        Returns:
            Metadata value or None
        """
        for k, v in self.metadata:
            if k == key:
                return v
        return None
    
    def __str__(self) -> str:
        return (
            f"RetentionObservation({self.actor} watched "
            f"{self.watched_ms}ms/{self.content_duration_ms}ms "
            f"of {self.content_id})"
        )


# ============================================================================
# ENGAGEMENT EVENT BUILDER
# ============================================================================


class EngagementEventBuilder:
    """Builder for creating EngagementEvent instances.
    
    Provides fluent interface for constructing events with validation.
    """
    
    CURRENT_SCHEMA_VERSION = 1
    
    def __init__(self):
        self._kind: Optional[EngagementKind] = None
        self._actor: Optional[EngagementActor] = None
        self._target: Optional[EngagementTarget] = None
        self._occurred_at: Optional[int] = None
        self._duration_ms: Optional[int] = None
        self._metadata: Dict[str, str] = {}
    
    def kind(self, kind: EngagementKind) -> EngagementEventBuilder:
        """Set engagement kind."""
        self._kind = kind
        return self
    
    def actor(self, actor: EngagementActor) -> EngagementEventBuilder:
        """Set actor."""
        self._actor = actor
        return self
    
    def human_actor(self, account_id: str) -> EngagementEventBuilder:
        """Set human actor."""
        self._actor = EngagementActor.human(account_id)
        return self
    
    def anonymous_actor(self) -> EngagementEventBuilder:
        """Set anonymous actor."""
        self._actor = EngagementActor.anonymous()
        return self
    
    def target(self, target: EngagementTarget) -> EngagementEventBuilder:
        """Set target."""
        self._target = target
        return self
    
    def target_content(self, content_id: str, content_kind: ContentKind) -> EngagementEventBuilder:
        """Set target content."""
        self._target = EngagementTarget(content_id, content_kind)
        return self
    
    def occurred_at(self, timestamp: int) -> EngagementEventBuilder:
        """Set occurrence timestamp."""
        self._occurred_at = timestamp
        return self
    
    def duration(self, duration_ms: int) -> EngagementEventBuilder:
        """Set duration (for VIEW only)."""
        self._duration_ms = duration_ms
        return self
    
    def add_metadata(self, key: str, value: str) -> EngagementEventBuilder:
        """Add metadata."""
        self._metadata[key] = value
        return self
    
    def build(self) -> EngagementEvent:
        """Build and validate the engagement event.
        
        Returns:
            Validated EngagementEvent
            
        Raises:
            SchemaValidationError: If required fields are missing
        """
        # Validate required fields
        if self._kind is None:
            raise SchemaValidationError("kind is required")
        if self._actor is None:
            raise SchemaValidationError("actor is required")
        if self._target is None:
            raise SchemaValidationError("target is required")
        if self._occurred_at is None:
            raise SchemaValidationError("occurred_at is required")
        
        # Generate engagement ID
        engagement_id = EngagementIdGenerator.generate(
            self._actor,
            self._target,
            self._kind,
            self._occurred_at,
        )
        
        # Convert metadata to sorted tuple
        metadata = tuple(sorted(self._metadata.items(), key=lambda x: x[0]))
        
        # Build event
        return EngagementEvent(
            engagement_id=engagement_id,
            schema_name="engagement",
            schema_version=self.CURRENT_SCHEMA_VERSION,
            kind=self._kind,
            actor=self._actor,
            target=self._target,
            occurred_at=self._occurred_at,
            duration_ms=self._duration_ms,
            metadata=metadata,
        )


# ============================================================================
# RETENTION OBSERVATION BUILDER
# ============================================================================


class RetentionObservationBuilder:
    """Builder for creating RetentionObservation instances.
    
    Provides fluent interface for constructing observations with validation.
    """
    
    CURRENT_SCHEMA_VERSION = 1
    
    def __init__(self):
        self._content_id: Optional[str] = None
        self._content_kind: Optional[ContentKind] = None
        self._actor: Optional[EngagementActor] = None
        self._watched_ms: Optional[int] = None
        self._content_duration_ms: Optional[int] = None
        self._occurred_at: Optional[int] = None
        self._metadata: Dict[str, str] = {}
    
    def content(self, content_id: str, content_kind: ContentKind) -> RetentionObservationBuilder:
        """Set content."""
        self._content_id = content_id
        self._content_kind = content_kind
        return self
    
    def actor(self, actor: EngagementActor) -> RetentionObservationBuilder:
        """Set actor."""
        self._actor = actor
        return self
    
    def human_actor(self, account_id: str) -> RetentionObservationBuilder:
        """Set human actor."""
        self._actor = EngagementActor.human(account_id)
        return self
    
    def anonymous_actor(self) -> RetentionObservationBuilder:
        """Set anonymous actor."""
        self._actor = EngagementActor.anonymous()
        return self
    
    def watched(self, watched_ms: int) -> RetentionObservationBuilder:
        """Set watched duration."""
        self._watched_ms = watched_ms
        return self
    
    def content_duration(self, duration_ms: int) -> RetentionObservationBuilder:
        """Set content duration."""
        self._content_duration_ms = duration_ms
        return self
    
    def occurred_at(self, timestamp: int) -> RetentionObservationBuilder:
        """Set occurrence timestamp."""
        self._occurred_at = timestamp
        return self
    
    def add_metadata(self, key: str, value: str) -> RetentionObservationBuilder:
        """Add metadata."""
        self._metadata[key] = value
        return self
    
    def build(self) -> RetentionObservation:
        """Build and validate the retention observation.
        
        Returns:
            Validated RetentionObservation
            
        Raises:
            SchemaValidationError: If required fields are missing
        """
        # Validate required fields
        if self._content_id is None:
            raise SchemaValidationError("content_id is required")
        if self._content_kind is None:
            raise SchemaValidationError("content_kind is required")
        if self._actor is None:
            raise SchemaValidationError("actor is required")
        if self._watched_ms is None:
            raise SchemaValidationError("watched_ms is required")
        if self._content_duration_ms is None:
            raise SchemaValidationError("content_duration_ms is required")
        if self._occurred_at is None:
            raise SchemaValidationError("occurred_at is required")
        
        # Generate observation ID deterministically without creating invalid object
        # This avoids the infra-hostile pattern of constructing temporarily invalid schemas
        actor_str = self._actor.account_id if self._actor.account_id else "anonymous"
        components = [
            f"actor={actor_str}",
            f"actor_kind={self._actor.actor_kind.value}",
            f"content={self._content_id}",
            f"content_kind={self._content_kind.value}",
            f"watched={self._watched_ms}",
            f"duration={self._content_duration_ms}",
            f"ts={self._occurred_at}",
        ]
        
        canonical = "::".join(components)
        hash_digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        observation_id = f"ret_{self._occurred_at}_{hash_digest[:16]}"
        
        # Convert metadata to sorted tuple
        metadata = tuple(sorted(self._metadata.items(), key=lambda x: x[0]))
        
        # Build observation
        return RetentionObservation(
            observation_id=observation_id,
            schema_name="retention",
            schema_version=self.CURRENT_SCHEMA_VERSION,
            content_id=self._content_id,
            content_kind=self._content_kind,
            actor=self._actor,
            watched_ms=self._watched_ms,
            content_duration_ms=self._content_duration_ms,
            occurred_at=self._occurred_at,
            metadata=metadata,
        )


# ============================================================================
# EXPORTS
# ============================================================================


__all__ = [
    # Base
    "CanonicalSchema",
    # Exceptions
    "EngagementSchemaError",
    "SchemaValidationError",
    "InvalidEngagementKindError",
    "InvalidActorError",
    "InvalidTargetError",
    "DeterminismViolation",
    "MetadataBoundsViolation",
    # Enums
    "EngagementKind",
    "AccountKind",
    "ContentKind",
    # Models
    "EngagementActor",
    "EngagementTarget",
    # Core schemas
    "EngagementEvent",
    "RetentionObservation",
    # Builders
    "EngagementEventBuilder",
    "RetentionObservationBuilder",
    # Utilities
    "EngagementIdGenerator",
    "MetadataValidator",
]