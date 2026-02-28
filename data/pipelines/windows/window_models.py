"""
/data/pipelines/windows/window_models.py

Window Declarations & Contracts — Research-Grade Authority Spec

What This File Exists For (NON-NEGOTIABLE):
  window_models.py is the single declarative authority for defining what a window is in the system.

It declares:
  - Window types
  - Window definitions (parameters + version)
  - Policy constraints
  - The immutable value objects emitted by resolution

It does not classify events. It does not interpret time. It does not compute boundaries.

If this file drifts, history drifts.

AUTHORITY: A window definition is an immutable contract, not a suggestion.

Design Principle (CRITICAL):
  > A window definition is an immutable contract, not a suggestion.

Once emitted, a window definition may only evolve via explicit versioning and replay/migration.

Conceptual Model:
  window_models.py defines pure data.
  It answers only: "What window exists, with what parameters, under what policy, and under what version?"
  Classification is deferred to window_engine.py.

Determinism Requirements (ABSOLUTE):
  All models in this file MUST satisfy:
  1. Structural determinism
  2. Serialization determinism
  3. Hash stability across platforms
  4. Version-explicit evolution

Any change to a field:
  - Changes window identity
  - Requires replay or migration

No silent compatibility.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, FrozenSet, Dict, Any
import json
import hashlib


class WindowType(str, Enum):
    """
    Explicit, versioned window kinds supported by the system.
    
    RULES:
    - Closed set per version
    - Additive changes only (no semantic reuse)
    - Removal requires replay migration
    """
    TUMBLING_TIME = "tumbling_time"
    SLIDING_TIME = "sliding_time"
    SESSION = "session"
    FIXED_EVENT = "fixed_event"
    LIFETIME = "lifetime"
    HOPPING_TIME = "hopping_time"
    GLOBAL = "global"


class TimestampExtractionStrategy(str, Enum):
    """Strategy for extracting event timestamp."""
    EVENT_TIME = "event_time"
    INGESTION_TIME = "ingestion_time"
    EXPLICIT_FIELD = "explicit_field"
    EARLIEST_OF_FIELDS = "earliest_of_fields"
    LATEST_OF_FIELDS = "latest_of_fields"


class WindowAssignmentStatus(str, Enum):
    """Status of window assignment result."""
    ASSIGNED = "assigned"
    TOO_LATE = "too_late"
    OUT_OF_BOUNDS = "out_of_bounds"
    INVALID_TIMESTAMP = "invalid_timestamp"
    REJECTED_BY_POLICY = "rejected_by_policy"


@dataclass(frozen=True)
class WindowPolicy:
    """
    Defines global, static constraints applied to all windows.
    
    RULES:
    - One policy per deployment version
    - No runtime overrides
    - Evaluated before resolution
    
    Conflict resolution (MANDATORY):
        effective_allowed_lateness_ms = min(
            window_definition.allowed_lateness_ms,
            policy.max_allowed_lateness_ms,
        )
    
    Any conflict → hard failure.
    """
    allowed_window_types: FrozenSet[WindowType]
    max_window_span_ms: int
    max_allowed_lateness_ms: int
    min_window_span_ms: int = 1
    max_session_gap_ms: int = 86400000
    min_session_gap_ms: int = 1
    max_slide_ratio: float = 1.0
    min_slide_ratio: float = 0.0
    replay_compatibility_required: bool = True
    require_explicit_versions: bool = True
    allow_unaligned_windows: bool = False
    policy_version: str = "v1"
    
    def __post_init__(self):
        if self.max_window_span_ms <= 0:
            raise ValueError(f"max_window_span_ms must be positive: {self.max_window_span_ms}")
        if self.min_window_span_ms <= 0:
            raise ValueError(f"min_window_span_ms must be positive: {self.min_window_span_ms}")
        if self.min_window_span_ms > self.max_window_span_ms:
            raise ValueError(
                f"min_window_span_ms ({self.min_window_span_ms}) > "
                f"max_window_span_ms ({self.max_window_span_ms})"
            )
        if self.max_allowed_lateness_ms < 0:
            raise ValueError(f"max_allowed_lateness_ms must be non-negative: {self.max_allowed_lateness_ms}")
        if self.max_session_gap_ms <= 0:
            raise ValueError(f"max_session_gap_ms must be positive: {self.max_session_gap_ms}")
        if self.min_session_gap_ms <= 0:
            raise ValueError(f"min_session_gap_ms must be positive: {self.min_session_gap_ms}")
        if not self.allowed_window_types:
            raise ValueError("allowed_window_types cannot be empty")
    
    def effective_allowed_lateness_ms(self, window_definition: 'WindowDefinition') -> int:
        """
        Compute effective allowed lateness after policy enforcement.
        
        Conflict resolution (MANDATORY):
            effective_allowed_lateness_ms = min(
                window_definition.allowed_lateness_ms,
                policy.max_allowed_lateness_ms,
            )
        
        Any conflict → hard failure.
        
        Args:
            window_definition: Window definition to compute effective lateness for
        
        Returns:
            Minimum of window and policy lateness
        
        Raises:
            ValueError: If conflict cannot be resolved
        """
        window_lateness = window_definition.allowed_lateness_ms
        policy_lateness = self.max_allowed_lateness_ms
        
        effective = min(window_lateness, policy_lateness)
        
        # Hard failure if window exceeds policy (unless policy allows it)
        if window_lateness > policy_lateness:
            raise ValueError(
                f"Window allowed_lateness_ms ({window_lateness}) exceeds "
                f"policy max_allowed_lateness_ms ({policy_lateness}). "
                f"Effective: {effective}"
            )
        
        return effective
    
    def validate_window_span(self, window_span_ms: int) -> None:
        """
        Validate window span against policy constraints.
        
        Args:
            window_span_ms: Window span to validate
        
        Raises:
            ValueError: Window span violates policy
        """
        if window_span_ms < self.min_window_span_ms:
            raise ValueError(
                f"Window span {window_span_ms}ms < minimum {self.min_window_span_ms}ms"
            )
        if window_span_ms > self.max_window_span_ms:
            raise ValueError(
                f"Window span {window_span_ms}ms > maximum {self.max_window_span_ms}ms"
            )
    
    def validate_window_type(self, window_type: WindowType) -> None:
        """
        Validate window type is allowed by policy.
        
        Args:
            window_type: Window type to validate
        
        Raises:
            ValueError: Window type not allowed
        """
        if window_type not in self.allowed_window_types:
            raise ValueError(
                f"Window type {window_type.value} not in allowed types: "
                f"{[wt.value for wt in self.allowed_window_types]}"
            )
    
    def canonical_serialization(self) -> bytes:
        """Produce canonical byte representation for hashing."""
        policy_dict = {
            "allowed_window_types": sorted([wt.value for wt in self.allowed_window_types]),
            "max_window_span_ms": self.max_window_span_ms,
            "max_allowed_lateness_ms": self.max_allowed_lateness_ms,
            "min_window_span_ms": self.min_window_span_ms,
            "max_session_gap_ms": self.max_session_gap_ms,
            "min_session_gap_ms": self.min_session_gap_ms,
            "replay_compatibility_required": self.replay_compatibility_required,
            "require_explicit_versions": self.require_explicit_versions,
            "allow_unaligned_windows": self.allow_unaligned_windows,
            "policy_version": self.policy_version,
        }
        
        canonical = json.dumps(
            policy_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        return canonical.encode('utf-8')
    
    def policy_fingerprint(self) -> str:
        """Compute stable fingerprint for this policy."""
        canonical = self.canonical_serialization()
        return hashlib.sha256(canonical).hexdigest()[:16]


@dataclass(frozen=True)
class WindowDefinition:
    """
    Defines what a window is, never how it is resolved.
    
    This is a declarative contract, not behavior.
    
    RULES:
    - Fully immutable (frozen=True)
    - Fully serializable (canonical JSON)
    - Version required
    - Comparable and hashable
    
    Invalid field combinations MUST be rejected by invariants:
    - SESSION + window_size_ms
    - SLIDING_TIME without slide_ms
    - Negative or zero durations
    
    Examples of invalid combinations:
    - SESSION + window_size_ms
    - SLIDING_TIME without slide_ms
    - Negative or zero durations
    - Overlapping disallowed windows
    - Unaligned boundaries (if policy disallows)
    """
    window_type: WindowType
    
    window_size_ms: Optional[int] = None
    slide_ms: Optional[int] = None
    session_gap_ms: Optional[int] = None
    hop_size_ms: Optional[int] = None
    event_count: Optional[int] = None
    
    alignment_epoch_ms: int = 0
    allowed_lateness_ms: int = 0
    
    timestamp_field: str = "event_time_ms"
    timestamp_extraction_strategy: TimestampExtractionStrategy = TimestampExtractionStrategy.EVENT_TIME
    
    definition_version: str = "v1"
    identity_format_version: str = "v1"
    
    metadata: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.alignment_epoch_ms < 0:
            raise ValueError(f"alignment_epoch_ms must be non-negative: {self.alignment_epoch_ms}")
        if self.allowed_lateness_ms < 0:
            raise ValueError(f"allowed_lateness_ms must be non-negative: {self.allowed_lateness_ms}")
        
        if self.window_size_ms is not None and self.window_size_ms <= 0:
            raise ValueError(f"window_size_ms must be positive: {self.window_size_ms}")
        if self.slide_ms is not None and self.slide_ms <= 0:
            raise ValueError(f"slide_ms must be positive: {self.slide_ms}")
        if self.session_gap_ms is not None and self.session_gap_ms <= 0:
            raise ValueError(f"session_gap_ms must be positive: {self.session_gap_ms}")
        if self.hop_size_ms is not None and self.hop_size_ms <= 0:
            raise ValueError(f"hop_size_ms must be positive: {self.hop_size_ms}")
        if self.event_count is not None and self.event_count <= 0:
            raise ValueError(f"event_count must be positive: {self.event_count}")
        
        if not self.definition_version:
            raise ValueError("definition_version must be explicitly set")
        if not self.identity_format_version:
            raise ValueError("identity_format_version must be explicitly set")
    
    def canonical_serialization(self) -> bytes:
        """
        Produce canonical byte representation for hashing.
        
        Canonical Serialization (REQUIRED):
        - Lexicographically ordered fields (sort_keys=True)
        - Explicit null handling (None fields omitted, not null)
        - Explicit version (definition_version, identity_format_version)
        - No whitespace (separators=(',', ':'))
        - UTF-8 encoding only (ensure_ascii=True)
        
        This is consumed by window_identity.py.
        
        The serialization is identity, not transport.
        Same definition must produce identical bytes forever.
        """
        definition_dict = {
            "window_type": self.window_type.value,
            "alignment_epoch_ms": self.alignment_epoch_ms,
            "allowed_lateness_ms": self.allowed_lateness_ms,
            "timestamp_field": self.timestamp_field,
            "timestamp_extraction_strategy": self.timestamp_extraction_strategy.value,
            "definition_version": self.definition_version,
            "identity_format_version": self.identity_format_version,
        }
        
        if self.window_size_ms is not None:
            definition_dict["window_size_ms"] = self.window_size_ms
        if self.slide_ms is not None:
            definition_dict["slide_ms"] = self.slide_ms
        if self.session_gap_ms is not None:
            definition_dict["session_gap_ms"] = self.session_gap_ms
        if self.hop_size_ms is not None:
            definition_dict["hop_size_ms"] = self.hop_size_ms
        if self.event_count is not None:
            definition_dict["event_count"] = self.event_count
        if self.metadata is not None:
            definition_dict["metadata"] = self._canonicalize_metadata(self.metadata)
        
        canonical = json.dumps(
            definition_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        return canonical.encode('utf-8')
    
    @staticmethod
    def _canonicalize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively canonicalize metadata for deterministic serialization."""
        if not isinstance(metadata, dict):
            return metadata
        return {
            k: WindowDefinition._canonicalize_metadata(v) if isinstance(v, dict) else v
            for k, v in sorted(metadata.items())
        }
    
    def definition_fingerprint(self) -> str:
        """
        Compute stable fingerprint for this definition.
        
        Returns:
            16-character hex fingerprint
        """
        canonical = self.canonical_serialization()
        return hashlib.sha256(canonical).hexdigest()[:16]
    
    def definition_hash(self) -> str:
        """
        Compute full stable hash for this definition.
        
        Returns:
            64-character hex hash
        """
        canonical = self.canonical_serialization()
        return hashlib.sha256(canonical).hexdigest()
    
    # Query methods (pure property checks, not behavior)
    # These answer questions about the definition structure, not classification or interpretation.
    
    def is_time_based(self) -> bool:
        """
        Check if window is time-based.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type in {
            WindowType.TUMBLING_TIME,
            WindowType.SLIDING_TIME,
            WindowType.SESSION,
            WindowType.HOPPING_TIME,
        }
    
    def is_count_based(self) -> bool:
        """
        Check if window is count-based.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type == WindowType.FIXED_EVENT
    
    def is_unbounded(self) -> bool:
        """
        Check if window is unbounded.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type in {WindowType.LIFETIME, WindowType.GLOBAL}
    
    def requires_window_size(self) -> bool:
        """
        Check if window type requires window_size_ms.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type in {
            WindowType.TUMBLING_TIME,
            WindowType.SLIDING_TIME,
            WindowType.HOPPING_TIME,
        }
    
    def requires_session_gap(self) -> bool:
        """
        Check if window type requires session_gap_ms.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type == WindowType.SESSION
    
    def requires_slide(self) -> bool:
        """
        Check if window type requires slide_ms.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type == WindowType.SLIDING_TIME
    
    def requires_hop_size(self) -> bool:
        """
        Check if window type requires hop_size_ms.
        
        This is a pure query about the definition structure, not behavior.
        """
        return self.window_type == WindowType.HOPPING_TIME
    
    def validate_required_fields(self) -> None:
        """
        Validate that required fields are present for window type.
        
        Raises:
            ValueError: Required field missing for window type
        """
        if self.requires_window_size() and self.window_size_ms is None:
            raise ValueError(f"{self.window_type.value} requires window_size_ms")
        
        if self.requires_session_gap() and self.session_gap_ms is None:
            raise ValueError(f"{self.window_type.value} requires session_gap_ms")
        
        if self.requires_slide() and self.slide_ms is None:
            raise ValueError(f"{self.window_type.value} requires slide_ms")
        
        if self.requires_hop_size() and self.hop_size_ms is None:
            raise ValueError(f"{self.window_type.value} requires hop_size_ms")
        
        if self.is_count_based() and self.event_count is None:
            raise ValueError(f"{self.window_type.value} requires event_count")
    
    def validate_forbidden_fields(self) -> None:
        """
        Validate that forbidden fields are not present for window type.
        
        Raises:
            ValueError: Forbidden field present for window type
        """
        if self.window_type == WindowType.SESSION:
            if self.window_size_ms is not None:
                raise ValueError("SESSION windows cannot have window_size_ms")
            if self.slide_ms is not None:
                raise ValueError("SESSION windows cannot have slide_ms")
            if self.hop_size_ms is not None:
                raise ValueError("SESSION windows cannot have hop_size_ms")
        
        if self.window_type == WindowType.TUMBLING_TIME:
            if self.slide_ms is not None:
                raise ValueError("TUMBLING_TIME windows cannot have slide_ms")
            if self.session_gap_ms is not None:
                raise ValueError("TUMBLING_TIME windows cannot have session_gap_ms")
            if self.hop_size_ms is not None:
                raise ValueError("TUMBLING_TIME windows cannot have hop_size_ms")
        
        if self.window_type == WindowType.FIXED_EVENT:
            if self.window_size_ms is not None:
                raise ValueError("FIXED_EVENT windows cannot have window_size_ms (time-based)")
            if self.slide_ms is not None:
                raise ValueError("FIXED_EVENT windows cannot have slide_ms")
            if self.session_gap_ms is not None:
                raise ValueError("FIXED_EVENT windows cannot have session_gap_ms")
        
        if self.is_unbounded():
            if self.window_size_ms is not None:
                raise ValueError(f"{self.window_type.value} windows cannot have bounded window_size_ms")
            if self.slide_ms is not None:
                raise ValueError(f"{self.window_type.value} windows cannot have slide_ms")
            if self.session_gap_ms is not None:
                raise ValueError(f"{self.window_type.value} windows cannot have session_gap_ms")


@dataclass(frozen=True)
class WindowAssignment:
    """
    Represents the result of classification, not the logic.
    
    This is a pure value object emitted by window resolution.
    
    RULES:
    - No behavior (only data access)
    - No mutation (frozen=True)
    - Serializable (canonical JSON)
    - Byte-identical on replay
    
    This object may be persisted and audited.
    
    Note: Helper methods (is_valid, window_duration_ms, etc.) are pure queries,
    not behavior. They compute derived values from immutable data.
    """
    window_id: str
    window_type: WindowType
    window_start_ms: int
    window_end_ms: int
    
    event_time_ms: int
    alignment_epoch_ms: int
    
    window_version: str
    identity_format_version: str
    
    status: WindowAssignmentStatus = WindowAssignmentStatus.ASSIGNED
    definition_fingerprint: str = ""
    assignment_metadata: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError(
                f"window_end_ms ({self.window_end_ms}) must be > "
                f"window_start_ms ({self.window_start_ms})"
            )
        if self.event_time_ms < 0:
            raise ValueError(f"event_time_ms must be non-negative: {self.event_time_ms}")
        if self.alignment_epoch_ms < 0:
            raise ValueError(f"alignment_epoch_ms must be non-negative: {self.alignment_epoch_ms}")
        if not self.window_id:
            raise ValueError("window_id must be non-empty")
        if not self.window_version:
            raise ValueError("window_version must be explicitly set")
        if not self.identity_format_version:
            raise ValueError("identity_format_version must be explicitly set")
    
    def is_valid(self) -> bool:
        """Check if assignment is valid."""
        return self.status == WindowAssignmentStatus.ASSIGNED
    
    def is_late(self) -> bool:
        """Check if event was too late for window."""
        return self.status == WindowAssignmentStatus.TOO_LATE
    
    def is_out_of_bounds(self) -> bool:
        """Check if event was out of window bounds."""
        return self.status == WindowAssignmentStatus.OUT_OF_BOUNDS
    
    def window_duration_ms(self) -> int:
        """Compute window duration in milliseconds."""
        return self.window_end_ms - self.window_start_ms
    
    def event_offset_from_start_ms(self) -> int:
        """Compute event offset from window start."""
        return self.event_time_ms - self.window_start_ms
    
    def event_offset_from_end_ms(self) -> int:
        """Compute event offset from window end (negative if before end)."""
        return self.event_time_ms - self.window_end_ms
    
    def canonical_serialization(self) -> bytes:
        """Produce canonical byte representation for verification."""
        assignment_dict = {
            "window_id": self.window_id,
            "window_type": self.window_type.value,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "event_time_ms": self.event_time_ms,
            "alignment_epoch_ms": self.alignment_epoch_ms,
            "window_version": self.window_version,
            "identity_format_version": self.identity_format_version,
            "status": self.status.value,
        }
        
        if self.definition_fingerprint:
            assignment_dict["definition_fingerprint"] = self.definition_fingerprint
        
        if self.assignment_metadata is not None:
            assignment_dict["assignment_metadata"] = self._canonicalize_metadata(
                self.assignment_metadata
            )
        
        canonical = json.dumps(
            assignment_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        return canonical.encode('utf-8')
    
    @staticmethod
    def _canonicalize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively canonicalize metadata."""
        if not isinstance(metadata, dict):
            return metadata
        return {
            k: WindowAssignment._canonicalize_metadata(v) if isinstance(v, dict) else v
            for k, v in sorted(metadata.items())
        }
    
    def assignment_hash(self) -> str:
        """Compute stable hash of assignment."""
        canonical = self.canonical_serialization()
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class CanonicalEvent:
    """
    Canonical event for window resolution.
    
    NOTE: This is a helper type for window resolution inputs.
    It is not a core window model type, but is included here for convenience.
    Event classification logic belongs in window_engine.py.
    
    RULES:
    - Immutable (frozen=True)
    - Contains all necessary timestamp information
    - Serializable
    """
    event_time_ms: int
    event_id: str
    payload: Dict[str, Any]
    
    ingestion_time_ms: Optional[int] = None
    processing_time_ms: Optional[int] = None
    
    def __post_init__(self):
        if self.event_time_ms < 0:
            raise ValueError(f"event_time_ms must be non-negative: {self.event_time_ms}")
        if not self.event_id:
            raise ValueError("event_id must be non-empty")
    
    def extract_timestamp(
        self,
        strategy: TimestampExtractionStrategy,
        field: str = "event_time_ms",
    ) -> int:
        """
        Extract timestamp based on strategy.
        
        Args:
            strategy: Timestamp extraction strategy
            field: Field name for explicit field strategy
        
        Returns:
            Timestamp in milliseconds
        
        Raises:
            ValueError: Timestamp extraction failed
        """
        if strategy == TimestampExtractionStrategy.EVENT_TIME:
            return self.event_time_ms
        
        elif strategy == TimestampExtractionStrategy.INGESTION_TIME:
            if self.ingestion_time_ms is None:
                raise ValueError("ingestion_time_ms not available")
            return self.ingestion_time_ms
        
        elif strategy == TimestampExtractionStrategy.EXPLICIT_FIELD:
            if field == "event_time_ms":
                return self.event_time_ms
            if field in self.payload:
                value = self.payload[field]
                if isinstance(value, (int, float)):
                    return int(value)
                raise ValueError(f"Field {field} is not numeric: {type(value)}")
            raise ValueError(f"Field {field} not found in payload")
        
        else:
            raise ValueError(f"Unsupported timestamp extraction strategy: {strategy}")


def create_default_policy() -> WindowPolicy:
    """Create default window policy."""
    return WindowPolicy(
        allowed_window_types=frozenset([
            WindowType.TUMBLING_TIME,
            WindowType.SLIDING_TIME,
            WindowType.SESSION,
            WindowType.HOPPING_TIME,
            WindowType.GLOBAL,
            WindowType.LIFETIME,
        ]),
        max_window_span_ms=365 * 24 * 3600 * 1000,
        max_allowed_lateness_ms=7 * 24 * 3600 * 1000,
        min_window_span_ms=1,
    )


def create_strict_policy() -> WindowPolicy:
    """Create strict window policy with tighter constraints."""
    return WindowPolicy(
        allowed_window_types=frozenset([
            WindowType.TUMBLING_TIME,
            WindowType.SLIDING_TIME,
        ]),
        max_window_span_ms=24 * 3600 * 1000,
        max_allowed_lateness_ms=3600 * 1000,
        min_window_span_ms=1000,
        replay_compatibility_required=True,
        require_explicit_versions=True,
        allow_unaligned_windows=False,
    )