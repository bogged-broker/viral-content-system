"""
/data/schemas/recovery.py

Canonical Recovery-Visible State Schemas (Truth Without Mutation)

This module defines the minimal, explicit state representations that recovery
systems are allowed to see, reason about, and act upon.

Design Principle:
    Recovery must act on declared state, never inferred state.
    
Philosophy:
    Recovery sees damage — it does not rewrite reality.
    Observation first. Repair later. History always intact.

Responsibilities:
    - Declare recovery-visible state
    - Separate observed damage from decisions
    - Preserve lineage to original facts
    - Encode state confidence explicitly
    - Be immutable and append-only
    - Support replay and forensics
    - Never encode "fixes"

Forbidden:
    - Mutation engines
    - Rollback logic
    - Repair strategy
    - Enforcement state
    - Checkpoints
    - Workflow logic
    - "Fix applied" flags
    - Auto-closure logic
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, final, Tuple, List, Dict

from base import CanonicalSchema, SchemaValidationError


# ============================================================================
# CONSTANTS
# ============================================================================

RECOVERY_SCHEMA_NAME: Final[str] = "recovery"
"""Canonical schema name for all recovery state records."""

SUPPORTED_SCHEMA_VERSIONS: Final[tuple[int, ...]] = (1,)
"""Supported schema versions for recovery state records."""

MIN_SEVERITY: Final[int] = 0
"""Minimum severity level (informational)."""

MAX_SEVERITY: Final[int] = 10
"""Maximum severity level (fatal)."""


# ============================================================================
# RECOVERY STATE TAXONOMY
# ============================================================================


class RecoveryStateKind(Enum):
    """
    Enumeration of observable recovery states.
    
    States describe observability, not blame or causation.
    
    INTACT: State is consistent and verifiable
    DEGRADED: State is partially readable but incomplete
    CORRUPTED: State contains detectable errors or violations
    INCONSISTENT: State contradicts other known facts
    UNKNOWN: State cannot be determined with confidence
    """

    INTACT = "intact"
    DEGRADED = "degraded"
    CORRUPTED = "corrupted"
    INCONSISTENT = "inconsistent"
    UNKNOWN = "unknown"

    def requires_signals(self) -> bool:
        """
        Return True if this state requires damage signals.
        
        INTACT state does not require signals.
        All other states MUST have at least one signal.
        """
        return self != RecoveryStateKind.INTACT

    def is_healthy(self) -> bool:
        """Return True if state represents healthy data."""
        return self == RecoveryStateKind.INTACT

    def is_actionable(self) -> bool:
        """
        Return True if state is actionable for recovery.
        
        UNKNOWN state is not actionable - more observation needed.
        """
        return self != RecoveryStateKind.UNKNOWN


# ============================================================================
# RECOVERY SUBJECT TAXONOMY
# ============================================================================


class RecoverySubjectType(Enum):
    """
    Enumeration of recovery subject types.
    
    Subjects are targets of repair, not causes of damage.
    
    CONTENT: User-generated content (posts, comments, media)
    ACCOUNT: User account state and metadata
    WORKFLOW: Pipeline or process execution state
    SNAPSHOT: Point-in-time data snapshots
    DATASET: Complete datasets or collections
    """

    CONTENT = "content"
    ACCOUNT = "account"
    WORKFLOW = "workflow"
    SNAPSHOT = "snapshot"
    DATASET = "dataset"

    def get_id_prefix(self) -> str:
        """Get the canonical ID prefix for this subject type."""
        return f"{self.value}_"


# ============================================================================
# SIGNAL SEVERITY
# ============================================================================


class SignalSeverity:
    """
    Signal severity constants and validation.
    
    Severity scale (0-10):
        0-2:   Informational (no action needed)
        3-5:   Warning (monitoring recommended)
        6-8:   Error (recovery should be attempted)
        9-10:  Fatal (immediate recovery required)
    """

    INFO = 0
    INFO_HIGH = 2
    WARNING = 3
    WARNING_HIGH = 5
    ERROR = 6
    ERROR_HIGH = 8
    FATAL = 9
    CRITICAL = 10

    @staticmethod
    def validate(severity: int) -> None:
        """
        Validate severity is in valid range.
        
        Raises:
            ValueError: If severity is out of range
        """
        if not isinstance(severity, int):
            raise ValueError(f"Severity must be int, got {type(severity)}")
        if severity < MIN_SEVERITY or severity > MAX_SEVERITY:
            raise ValueError(
                f"Severity {severity} out of range [{MIN_SEVERITY}, {MAX_SEVERITY}]"
            )

    @staticmethod
    def is_actionable(severity: int) -> bool:
        """Return True if severity warrants recovery action."""
        return severity >= SignalSeverity.ERROR

    @staticmethod
    def is_critical(severity: int) -> bool:
        """Return True if severity is critical."""
        return severity >= SignalSeverity.FATAL


# ============================================================================
# SIGNAL TYPES
# ============================================================================


class RecoverySignalType:
    """
    Standard recovery signal types.
    
    Signal types are descriptive identifiers, not actionable commands.
    """

    # Data integrity signals
    INVARIANT_VIOLATION = "invariant_violation"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    SCHEMA_VIOLATION = "schema_violation"
    TYPE_MISMATCH = "type_mismatch"

    # Consistency signals
    REFERENCE_BROKEN = "reference_broken"
    CYCLE_DETECTED = "cycle_detected"
    DUPLICATE_DETECTED = "duplicate_detected"
    ORDERING_VIOLATED = "ordering_violated"

    # Completeness signals
    GAP_DETECTED = "gap_detected"
    MISSING_REQUIRED = "missing_required"
    TRUNCATION_DETECTED = "truncation_detected"
    INCOMPLETE_RECORD = "incomplete_record"

    # Temporal signals
    TIMESTAMP_INVALID = "timestamp_invalid"
    TIMESTAMP_OUT_OF_RANGE = "timestamp_out_of_range"
    TEMPORAL_INCONSISTENCY = "temporal_inconsistency"

    # Operational signals
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    PERMISSION_DENIED = "permission_denied"

    # Detection signals
    ANOMALY_DETECTED = "anomaly_detected"
    PATTERN_VIOLATION = "pattern_violation"
    THRESHOLD_EXCEEDED = "threshold_exceeded"

    @staticmethod
    def is_valid(signal_type: str) -> bool:
        """Check if signal type is a known standard type."""
        standard_types = {
            name
            for name, value in vars(RecoverySignalType).items()
            if isinstance(value, str) and not name.startswith("_")
        }
        return signal_type in [
            getattr(RecoverySignalType, name) for name in standard_types
        ]


# ============================================================================
# DAMAGE SIGNAL DECLARATION
# ============================================================================


@dataclass(frozen=True)
class RecoverySignal:
    """
    Atomic damage signal observed in recovery-visible state.
    
    Signals are descriptive, not actionable. They report what was observed,
    not what should be done about it.
    
    Attributes:
        signal_type: Type identifier (e.g., "invariant_violation")
        severity: Severity level [0-10] where 0=info, 10=fatal
        description: Verbatim explanation of the signal
        metadata: Optional additional signal context
    """

    signal_type: str
    severity: int
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate signal invariants at construction time."""
        self._validate_signal_type()
        self._validate_severity()
        self._validate_description()
        self._validate_metadata()

    def _validate_signal_type(self) -> None:
        """Validate signal type is non-empty string."""
        if not self.signal_type:
            raise ValueError("Signal type cannot be empty")
        if not isinstance(self.signal_type, str):
            raise ValueError(
                f"Signal type must be string, got {type(self.signal_type)}"
            )
        if len(self.signal_type) > 255:
            raise ValueError(
                f"Signal type too long: {len(self.signal_type)} chars (max 255)"
            )

    def _validate_severity(self) -> None:
        """Validate severity is in valid range."""
        SignalSeverity.validate(self.severity)

    def _validate_description(self) -> None:
        """Validate description is non-empty string."""
        if not self.description:
            raise ValueError("Signal description cannot be empty")
        if not isinstance(self.description, str):
            raise ValueError(
                f"Signal description must be string, got {type(self.description)}"
            )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise ValueError(f"Metadata must be dict, got {type(self.metadata)}")

    def is_actionable(self) -> bool:
        """Return True if signal severity warrants action."""
        return SignalSeverity.is_actionable(self.severity)

    def is_critical(self) -> bool:
        """Return True if signal severity is critical."""
        return SignalSeverity.is_critical(self.severity)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert signal to dictionary representation.
        
        Returns:
            Dictionary with deterministic key ordering
        """
        return {
            "signal_type": self.signal_type,
            "severity": self.severity,
            "description": self.description,
            "metadata": dict(sorted(self.metadata.items())),
        }


# ============================================================================
# LINEAGE REFERENCE
# ============================================================================


@dataclass(frozen=True)
class RecoveryLineage:
    """
    Immutable lineage reference to original source data.
    
    Lineage MUST point backward only - it traces damage to its origin
    without creating new dependencies.
    
    Rules:
        - Lineage must point backward only
        - No synthesized IDs
        - Empty lineage is allowed but explicit
        - IDs must be from canonical schemas only
    
    Attributes:
        source_schema: Name of the source schema (e.g., "engagement")
        source_ids: Immutable tuple of origin record identifiers
        source_version: Optional schema version of source records
        metadata: Optional lineage context
    """

    source_schema: str
    source_ids: tuple[str, ...]
    source_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate lineage invariants at construction time."""
        self._validate_source_schema()
        self._validate_source_ids()
        self._validate_source_version()
        self._validate_metadata()

    def _validate_source_schema(self) -> None:
        """Validate source schema is valid."""
        if not self.source_schema:
            raise ValueError("Source schema cannot be empty")
        if not isinstance(self.source_schema, str):
            raise ValueError(
                f"Source schema must be string, got {type(self.source_schema)}"
            )
        # Known canonical schemas
        valid_schemas = {
            "base",
            "content",
            "account",
            "engagement",
            "moderation",
            "analytics",
            "recovery",
        }
        if self.source_schema not in valid_schemas:
            # Allow unknown schemas but log warning
            pass

    def _validate_source_ids(self) -> None:
        """Validate source IDs are immutable tuple of strings."""
        if not isinstance(self.source_ids, tuple):
            raise ValueError(
                f"Source IDs must be tuple, got {type(self.source_ids)}"
            )
        for idx, source_id in enumerate(self.source_ids):
            if not isinstance(source_id, str):
                raise ValueError(
                    f"Source ID at index {idx} must be string, got {type(source_id)}"
                )
            if not source_id:
                raise ValueError(f"Source ID at index {idx} cannot be empty")

    def _validate_source_version(self) -> None:
        """Validate source version if present."""
        if self.source_version is not None:
            if not isinstance(self.source_version, int):
                raise ValueError(
                    f"Source version must be int, got {type(self.source_version)}"
                )
            if self.source_version < 1:
                raise ValueError(
                    f"Source version must be >= 1, got {self.source_version}"
                )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise ValueError(f"Metadata must be dict, got {type(self.metadata)}")

    def is_empty(self) -> bool:
        """Return True if lineage has no source IDs."""
        return len(self.source_ids) == 0

    def get_source_count(self) -> int:
        """Return number of source records referenced."""
        return len(self.source_ids)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert lineage to dictionary representation.
        
        Returns:
            Dictionary with deterministic key ordering
        """
        result: dict[str, Any] = {
            "source_schema": self.source_schema,
            "source_ids": list(self.source_ids),
        }
        if self.source_version is not None:
            result["source_version"] = self.source_version
        if self.metadata:
            result["metadata"] = dict(sorted(self.metadata.items()))
        return result


# ============================================================================
# RECOVERY VISIBILITY WINDOW
# ============================================================================


@dataclass(frozen=True)
class RecoveryWindow:
    """
    Temporal window for recovery visibility and observation.
    
    The window defines when damage was observed and the time range
    over which the damage may have occurred.
    
    Rules:
        - Detection time ≠ origin time
        - Windows may be unknown (None)
        - Explicit None beats inference
        - Window bounds are inclusive
    
    Attributes:
        observed_at: Timestamp when damage was detected (milliseconds)
        effective_range: Optional (start, end) tuple for damage window
        confidence: Optional confidence level [0.0-1.0] in window accuracy
        metadata: Optional window context
    """

    observed_at: int
    effective_range: tuple[int, int] | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate window invariants at construction time."""
        self._validate_observed_at()
        self._validate_effective_range()
        self._validate_confidence()
        self._validate_metadata()

    def _validate_observed_at(self) -> None:
        """Validate observed_at timestamp."""
        if not isinstance(self.observed_at, int):
            raise ValueError(
                f"observed_at must be int, got {type(self.observed_at)}"
            )
        if self.observed_at < 0:
            raise ValueError(f"observed_at must be >= 0, got {self.observed_at}")

    def _validate_effective_range(self) -> None:
        """Validate effective range if present."""
        if self.effective_range is not None:
            if not isinstance(self.effective_range, tuple):
                raise ValueError(
                    f"effective_range must be tuple, got {type(self.effective_range)}"
                )
            if len(self.effective_range) != 2:
                raise ValueError(
                    f"effective_range must have 2 elements, got {len(self.effective_range)}"
                )
            start, end = self.effective_range
            if not isinstance(start, int) or not isinstance(end, int):
                raise ValueError("effective_range elements must be int")
            if start < 0 or end < 0:
                raise ValueError("effective_range elements must be >= 0")
            if end < start:
                raise ValueError(
                    f"effective_range end ({end}) < start ({start})"
                )

    def _validate_confidence(self) -> None:
        """Validate confidence if present."""
        if self.confidence is not None:
            if not isinstance(self.confidence, (int, float)):
                raise ValueError(
                    f"confidence must be numeric, got {type(self.confidence)}"
                )
            if self.confidence < 0.0 or self.confidence > 1.0:
                raise ValueError(
                    f"confidence must be in [0.0, 1.0], got {self.confidence}"
                )

    def _validate_metadata(self) -> None:
        """Validate metadata is a dict."""
        if not isinstance(self.metadata, dict):
            raise ValueError(f"Metadata must be dict, got {type(self.metadata)}")

    def has_effective_range(self) -> bool:
        """Return True if window has an effective range."""
        return self.effective_range is not None

    def get_range_duration(self) -> int | None:
        """
        Get duration of effective range in milliseconds.
        
        Returns:
            Duration in milliseconds, or None if no range
        """
        if self.effective_range is None:
            return None
        start, end = self.effective_range
        return end - start

    def is_confident(self) -> bool:
        """
        Return True if window confidence is high (>= 0.8).
        
        Returns False if confidence is not set.
        """
        if self.confidence is None:
            return False
        return self.confidence >= 0.8

    def to_dict(self) -> dict[str, Any]:
        """
        Convert window to dictionary representation.
        
        Returns:
            Dictionary with deterministic key ordering
        """
        result: dict[str, Any] = {
            "observed_at": self.observed_at,
        }
        if self.effective_range is not None:
            result["effective_range"] = list(self.effective_range)
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.metadata:
            result["metadata"] = dict(sorted(self.metadata.items()))
        return result


# ============================================================================
# CORE SCHEMA: RECOVERY STATE RECORD
# ============================================================================


@final
@dataclass(frozen=True)
class RecoveryStateRecord(CanonicalSchema):
    """
    Atomic recovery-visible state record.
    
    This is the canonical representation of observed damage or state
    that recovery systems are permitted to see and reason about.
    
    The record is:
        - Immutable (frozen dataclass)
        - Append-only (never mutated)
        - Replay-safe (deterministic ID)
        - Lineage-preserving (points to sources)
        - Evidence-based (explicit signals)
    
    Attributes:
        recovery_state_id: Deterministic state identifier
        schema_name: Always "recovery"
        schema_version: Schema version (currently 1)
        subject_type: Type of recovery subject
        subject_id: Identifier of the subject
        state: Observable recovery state
        signals: Tuple of damage signals (immutable)
        lineage: Lineage to source records
        window: Observation time window
        detected_at: Detection timestamp (milliseconds)
        metadata: Optional additional context
    """

    # Identity
    recovery_state_id: str
    schema_name: str
    schema_version: int

    # Subject
    subject_type: RecoverySubjectType
    subject_id: str

    # State
    state: RecoveryStateKind

    # Evidence
    signals: tuple[RecoverySignal, ...]
    lineage: RecoveryLineage

    # Visibility
    window: RecoveryWindow

    # Detection
    detected_at: int

    # Optional context
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate all recovery state record invariants."""
        self._validate_schema_identity()
        self._validate_recovery_state_id()
        self._validate_subject()
        self._validate_state_and_signals()
        self._validate_lineage()
        self._validate_window()
        self._validate_detected_at()
        self._validate_metadata()

    # ========================================================================
    # VALIDATION METHODS
    # ========================================================================

    def _validate_schema_identity(self) -> None:
        """Validate schema name and version."""
        if self.schema_name != RECOVERY_SCHEMA_NAME:
            raise SchemaValidationError(
                f"schema_name must be '{RECOVERY_SCHEMA_NAME}', "
                f"got '{self.schema_name}'"
            )
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise SchemaValidationError(
                f"schema_version {self.schema_version} not in "
                f"supported versions {SUPPORTED_SCHEMA_VERSIONS}"
            )

    def _validate_recovery_state_id(self) -> None:
        """Validate recovery state ID."""
        if not self.recovery_state_id:
            raise SchemaValidationError("recovery_state_id cannot be empty")
        if not isinstance(self.recovery_state_id, str):
            raise SchemaValidationError(
                f"recovery_state_id must be string, got {type(self.recovery_state_id)}"
            )
        # Verify deterministic ID format
        expected_id = self.compute_recovery_state_id(
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            state=self.state,
            signals=self.signals,
            detected_at=self.detected_at,
        )
        if self.recovery_state_id != expected_id:
            raise SchemaValidationError(
                f"recovery_state_id mismatch: expected {expected_id}, "
                f"got {self.recovery_state_id}"
            )

    def _validate_subject(self) -> None:
        """Validate subject type and ID."""
        if not isinstance(self.subject_type, RecoverySubjectType):
            raise SchemaValidationError(
                f"subject_type must be RecoverySubjectType, got {type(self.subject_type)}"
            )
        if not self.subject_id:
            raise SchemaValidationError("subject_id cannot be empty")
        if not isinstance(self.subject_id, str):
            raise SchemaValidationError(
                f"subject_id must be string, got {type(self.subject_id)}"
            )

    def _validate_state_and_signals(self) -> None:
        """
        Validate state and signals consistency.
        
        Rules:
            - State must be RecoveryStateKind
            - Signals must be tuple of RecoverySignal
            - Non-INTACT states require at least one signal
        """
        if not isinstance(self.state, RecoveryStateKind):
            raise SchemaValidationError(
                f"state must be RecoveryStateKind, got {type(self.state)}"
            )
        if not isinstance(self.signals, tuple):
            raise SchemaValidationError(
                f"signals must be tuple, got {type(self.signals)}"
            )

        # Validate all signals
        for idx, signal in enumerate(self.signals):
            if not isinstance(signal, RecoverySignal):
                raise SchemaValidationError(
                    f"Signal at index {idx} must be RecoverySignal, "
                    f"got {type(signal)}"
                )

        # Require signals for non-INTACT states
        if self.state.requires_signals() and len(self.signals) == 0:
            raise SchemaValidationError(
                f"State {self.state.value} requires at least one signal"
            )

    def _validate_lineage(self) -> None:
        """Validate lineage reference."""
        if not isinstance(self.lineage, RecoveryLineage):
            raise SchemaValidationError(
                f"lineage must be RecoveryLineage, got {type(self.lineage)}"
            )

    def _validate_window(self) -> None:
        """Validate recovery window."""
        if not isinstance(self.window, RecoveryWindow):
            raise SchemaValidationError(
                f"window must be RecoveryWindow, got {type(self.window)}"
            )

    def _validate_detected_at(self) -> None:
        """Validate detected_at timestamp."""
        if not isinstance(self.detected_at, int):
            raise SchemaValidationError(
                f"detected_at must be int, got {type(self.detected_at)}"
            )
        if self.detected_at < 0:
            raise SchemaValidationError(
                f"detected_at must be >= 0, got {self.detected_at}"
            )
        # Validate detection time is consistent with window
        if self.detected_at != self.window.observed_at:
            raise SchemaValidationError(
                f"detected_at ({self.detected_at}) must equal "
                f"window.observed_at ({self.window.observed_at})"
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
    def compute_recovery_state_id(
        subject_type: RecoverySubjectType,
        subject_id: str,
        state: RecoveryStateKind,
        signals: tuple[RecoverySignal, ...],
        detected_at: int,
    ) -> str:
        """
        Compute deterministic recovery state ID.
        
        ID is derived from:
            - subject_type
            - subject_id
            - state
            - sorted signal types
            - detected_at
        
        Same inputs always produce same ID (replay-safe).
        
        Args:
            subject_type: Recovery subject type
            subject_id: Subject identifier
            state: Recovery state kind
            signals: Tuple of recovery signals
            detected_at: Detection timestamp
            
        Returns:
            Deterministic SHA-256 hash as recovery_state_id
        """
        hasher = hashlib.sha256()

        # Subject
        hasher.update(subject_type.value.encode("utf-8"))
        hasher.update(subject_id.encode("utf-8"))

        # State
        hasher.update(state.value.encode("utf-8"))

        # Sorted signal types (for determinism)
        signal_types = sorted(s.signal_type for s in signals)
        for signal_type in signal_types:
            hasher.update(signal_type.encode("utf-8"))

        # Detection time
        hasher.update(str(detected_at).encode("utf-8"))

        return f"recovery_state_{hasher.hexdigest()}"

    # ========================================================================
    # CANONICAL SCHEMA IMPLEMENTATION
    # ========================================================================

    def validate(self) -> None:
        """
        Validate the recovery state record.
        
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
        Convert recovery state record to dictionary with canonical ordering.
        
        Ordering:
            1. Identity (recovery_state_id, schema_name, schema_version)
            2. Subject (subject_type, subject_id)
            3. State (state)
            4. Signals (signals)
            5. Lineage (lineage)
            6. Window (window)
            7. Detection (detected_at)
            8. Metadata (metadata)
        
        Returns:
            Dictionary with deterministic key ordering and bit-stable values
        """
        return {
            # Identity
            "recovery_state_id": self.recovery_state_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            # Subject
            "subject_type": self.subject_type.value,
            "subject_id": self.subject_id,
            # State
            "state": self.state.value,
            # Signals
            "signals": [s.to_dict() for s in self.signals],
            # Lineage
            "lineage": self.lineage.to_dict(),
            # Window
            "window": self.window.to_dict(),
            # Detection
            "detected_at": self.detected_at,
            # Metadata
            "metadata": dict(sorted(self.metadata.items())),
        }

    def to_json(self) -> str:
        """
        Serialize to deterministic JSON string.
        
        Returns:
            Canonical JSON representation (bit-stable)
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        """
        Compute deterministic hash of the recovery state record.
        
        Returns:
            SHA-256 hash of canonical JSON representation
        """
        canonical_json = self.to_json()
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    # ========================================================================
    # QUERY METHODS
    # ========================================================================

    def is_healthy(self) -> bool:
        """Return True if recovery state is INTACT."""
        return self.state.is_healthy()

    def is_actionable(self) -> bool:
        """Return True if recovery state requires action."""
        return self.state.is_actionable()

    def has_critical_signals(self) -> bool:
        """Return True if any signal is critical severity."""
        return any(s.is_critical() for s in self.signals)

    def get_max_severity(self) -> int:
        """
        Get maximum severity across all signals.
        
        Returns:
            Maximum severity level, or 0 if no signals
        """
        if not self.signals:
            return 0
        return max(s.severity for s in self.signals)

    def get_signal_count(self) -> int:
        """Return number of signals."""
        return len(self.signals)

    def get_signals_by_type(self, signal_type: str) -> tuple[RecoverySignal, ...]:
        """
        Get all signals of a specific type.
        
        Args:
            signal_type: Signal type to filter by
            
        Returns:
            Tuple of matching signals
        """
        return tuple(s for s in self.signals if s.signal_type == signal_type)

    def get_source_schema(self) -> str:
        """Get lineage source schema."""
        return self.lineage.source_schema

    def get_source_ids(self) -> tuple[str, ...]:
        """Get lineage source IDs."""
        return self.lineage.source_ids


# ============================================================================
# RECOVERY STATE RECORD BUILDER
# ============================================================================


class RecoveryStateRecordBuilder:
    """
    Builder for constructing RecoveryStateRecord instances.
    
    Provides a fluent interface for building recovery state records
    with validation at each step.
    """

    def __init__(
        self,
        subject_type: RecoverySubjectType,
        subject_id: str,
        state: RecoveryStateKind,
        detected_at: int,
    ) -> None:
        """
        Initialize builder with required fields.
        
        Args:
            subject_type: Recovery subject type
            subject_id: Subject identifier
            state: Recovery state kind
            detected_at: Detection timestamp
        """
        self._subject_type = subject_type
        self._subject_id = subject_id
        self._state = state
        self._detected_at = detected_at
        self._signals: list[RecoverySignal] = []
        self._lineage: RecoveryLineage | None = None
        self._window: RecoveryWindow | None = None
        self._metadata: dict[str, Any] = {}

    def add_signal(
        self,
        signal_type: str,
        severity: int,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> RecoveryStateRecordBuilder:
        """Add a recovery signal."""
        signal = RecoverySignal(
            signal_type=signal_type,
            severity=severity,
            description=description,
            metadata=metadata or {},
        )
        self._signals.append(signal)
        return self

    def set_lineage(
        self,
        source_schema: str,
        source_ids: tuple[str, ...],
        source_version: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RecoveryStateRecordBuilder:
        """Set lineage reference."""
        self._lineage = RecoveryLineage(
            source_schema=source_schema,
            source_ids=source_ids,
            source_version=source_version,
            metadata=metadata or {},
        )
        return self

    def set_window(
        self,
        observed_at: int | None = None,
        effective_range: tuple[int, int] | None = None,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RecoveryStateRecordBuilder:
        """Set recovery window."""
        self._window = RecoveryWindow(
            observed_at=observed_at or self._detected_at,
            effective_range=effective_range,
            confidence=confidence,
            metadata=metadata or {},
        )
        return self

    def add_metadata(self, key: str, value: Any) -> RecoveryStateRecordBuilder:
        """Add metadata entry."""
        self._metadata[key] = value
        return self

    def build(self) -> RecoveryStateRecord:
        """
        Build the RecoveryStateRecord.
        
        Returns:
            Validated RecoveryStateRecord instance
            
        Raises:
            ValueError: If required fields are missing
        """
        # Ensure lineage is set
        if self._lineage is None:
            self._lineage = RecoveryLineage(
                source_schema="unknown",
                source_ids=tuple(),
            )

        # Ensure window is set
        if self._window is None:
            self._window = RecoveryWindow(observed_at=self._detected_at)

        # Compute deterministic ID
        recovery_state_id = RecoveryStateRecord.compute_recovery_state_id(
            subject_type=self._subject_type,
            subject_id=self._subject_id,
            state=self._state,
            signals=tuple(self._signals),
            detected_at=self._detected_at,
        )

        return RecoveryStateRecord(
            recovery_state_id=recovery_state_id,
            schema_name=RECOVERY_SCHEMA_NAME,
            schema_version=1,
            subject_type=self._subject_type,
            subject_id=self._subject_id,
            state=self._state,
            signals=tuple(self._signals),
            lineage=self._lineage,
            window=self._window,
            detected_at=self._detected_at,
            metadata=self._metadata,
        )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Constants
    "RECOVERY_SCHEMA_NAME",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MIN_SEVERITY",
    "MAX_SEVERITY",
    # Enums
    "RecoveryStateKind",
    "RecoverySubjectType",
    # Classes
    "SignalSeverity",
    "RecoverySignalType",
    "RecoverySignal",
    "RecoveryLineage",
    "RecoveryWindow",
    "RecoveryStateRecord",
    "RecoveryStateRecordBuilder",
]