"""
/infra/recovery/recovery_models.py

Core data models for recovery orchestration.

All models are immutable (frozen dataclasses) to ensure deterministic behavior.
Uses logical timestamps, never system time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple, List
from datetime import datetime  # Only for logical timestamp representation


# ============================================================================
# LOGICAL TIMESTAMP (No System Time)
# ============================================================================


@dataclass(frozen=True)
class LogicalTimestamp:
    """
    Logical timestamp for deterministic recovery.
    
    NEVER uses system time. Must be sourced from:
    - Snapshot timestamps
    - Reference timestamps from context
    - Checkpoint boundaries
    
    This ensures replay determinism.
    """
    iso_string: str  # ISO format string for deterministic serialization
    
    @classmethod
    def from_datetime(cls, dt: datetime) -> LogicalTimestamp:
        """Create from datetime (must be from snapshot, not system time)."""
        return cls(iso_string=dt.isoformat())
    
    def to_datetime(self) -> datetime:
        """Convert to datetime for compatibility (read-only)."""
        return datetime.fromisoformat(self.iso_string)
    
    def __lt__(self, other: LogicalTimestamp) -> bool:
        """Compare timestamps deterministically."""
        return self.iso_string < other.iso_string
    
    def __le__(self, other: LogicalTimestamp) -> bool:
        return self.iso_string <= other.iso_string
    
    def __gt__(self, other: LogicalTimestamp) -> bool:
        return self.iso_string > other.iso_string
    
    def __ge__(self, other: LogicalTimestamp) -> bool:
        return self.iso_string >= other.iso_string


# ============================================================================
# RECOVERY MODES & TYPES
# ============================================================================


class RecoveryMode(Enum):
    """
    Explicit recovery modes - no silent escalation between modes.
    
    Ordered by increasing intervention level.
    """
    NO_RECOVERY_NEEDED = 0      # System consistent, resume normally
    SAFE_RESUME = 1             # Resume from last checkpoint
    PARTIAL_REWIND = 2          # Rewind to safe boundary, replay forward
    FULL_REBUILD_REQUIRED = 3   # Complete rebuild from source
    BLOCKED_CORRUPTION_DETECTED = 4  # Corruption detected, manual intervention required

    def __lt__(self, other: RecoveryMode) -> bool:
        return self.value < other.value


class RecoveryStage(Enum):
    """Pipeline stages that can be recovered."""
    INGESTION = "INGESTION"
    VALIDATION = "VALIDATION"
    PERSISTENCE = "PERSISTENCE"
    AGGREGATION = "AGGREGATION"
    METRICS_EVALUATION = "METRICS_EVALUATION"
    DOWNSTREAM_PROPAGATION = "DOWNSTREAM_PROPAGATION"


class CorruptionReason(Enum):
    """Explicit corruption detection reasons."""
    CHECKPOINT_BEYOND_PERSISTENCE = "CHECKPOINT_BEYOND_PERSISTENCE"
    AGGREGATION_STATE_INCONSISTENT = "AGGREGATION_STATE_INCONSISTENT"
    SNAPSHOT_VERSION_MISMATCH = "SNAPSHOT_VERSION_MISMATCH"
    ORPHANED_TRANSACTION_FRAGMENTS = "ORPHANED_TRANSACTION_FRAGMENTS"
    CHECKPOINT_MOVED_BACKWARDS = "CHECKPOINT_MOVED_BACKWARDS"
    PARTIAL_TRANSACTION_DETECTED = "PARTIAL_TRANSACTION_DETECTED"
    WINDOW_STATE_CORRUPTED = "WINDOW_STATE_CORRUPTED"
    INTEGRITY_GUARD_FAILURE = "INTEGRITY_GUARD_FAILURE"
    KEYSPACE_NAMESPACE_DRIFT = "KEYSPACE_NAMESPACE_DRIFT"
    WRITE_AHEAD_LOG_MISMATCH = "WRITE_AHEAD_LOG_MISMATCH"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class CheckpointState:
    """
    Immutable checkpoint state.
    
    Represents the highest safe committed boundary.
    Must guarantee all irreversible side effects behind it are complete.
    """
    checkpoint_id: str
    stage: RecoveryStage
    boundary_timestamp: LogicalTimestamp  # Logical timestamp, not system time
    sequence_number: int
    persistence_version: str
    transaction_log_position: Optional[int]
    window_id: Optional[str]  # For windowed aggregation
    idempotency_key: str
    checkpoint_hash: str

    def __post_init__(self):
        """Validate checkpoint construction."""
        if self.sequence_number < 0:
            raise ValueError("Checkpoint sequence number cannot be negative")
        if not self.checkpoint_hash:
            raise ValueError("Checkpoint hash required for verification")


@dataclass(frozen=True)
class CheckpointSnapshot:
    """Snapshot of all checkpoints across pipeline stages."""
    checkpoints: Dict[RecoveryStage, CheckpointState]
    snapshot_timestamp: LogicalTimestamp
    snapshot_version: str
    snapshot_hash: str

    def get_checkpoint(self, stage: RecoveryStage) -> Optional[CheckpointState]:
        """Get checkpoint for specific stage."""
        return self.checkpoints.get(stage)

    def max_sequence_number(self) -> int:
        """Get maximum checkpoint sequence across all stages."""
        if not self.checkpoints:
            return 0
        return max(cp.sequence_number for cp in self.checkpoints.values())


@dataclass(frozen=True)
class StageState:
    """State of a single pipeline stage."""
    stage: RecoveryStage
    status: str  # "COMPLETE", "IN_PROGRESS", "FAILED", "PENDING"
    last_processed_timestamp: Optional[LogicalTimestamp]
    batch_id: Optional[str]
    records_processed: int
    completion_marker: Optional[str]
    is_atomic_boundary: bool
    idempotency_keys: Tuple[str, ...]


@dataclass(frozen=True)
class PipelineStateSnapshot:
    """Immutable snapshot of pipeline stage states."""
    stages: Dict[RecoveryStage, StageState]
    pipeline_version: str
    snapshot_timestamp: LogicalTimestamp
    snapshot_hash: str

    def get_stage(self, stage: RecoveryStage) -> Optional[StageState]:
        """Get state for specific stage."""
        return self.stages.get(stage)

    def stages_in_progress(self) -> list[RecoveryStage]:
        """Get all stages currently in progress."""
        return [
            stage
            for stage, state in self.stages.items()
            if state.status == "IN_PROGRESS"
        ]


@dataclass(frozen=True)
class TransactionState:
    """State of a single transaction."""
    transaction_id: str
    transaction_version: str
    status: str  # "COMMITTED", "PENDING", "ABORTED"
    log_position: int
    write_timestamp: LogicalTimestamp
    affected_keys: Tuple[str, ...]


@dataclass(frozen=True)
class PersistenceSnapshot:
    """Snapshot of persistence backend state."""
    last_committed_version: str
    last_committed_timestamp: LogicalTimestamp
    transaction_log_head: int
    pending_transactions: Tuple[TransactionState, ...]
    committed_transactions: Tuple[TransactionState, ...]
    backend_integrity_valid: bool
    keyspace_namespace: str
    write_ahead_log_position: Optional[int]
    snapshot_version: str
    snapshot_hash: str

    def has_pending_transactions(self) -> bool:
        """Check if any transactions are pending."""
        return len(self.pending_transactions) > 0

    def has_orphaned_fragments(self) -> bool:
        """Check for orphaned transaction fragments."""
        # Orphaned if pending transactions exist but log position doesn't match
        if not self.pending_transactions:
            return False
        expected_positions = {t.log_position for t in self.pending_transactions}
        return self.transaction_log_head not in expected_positions


@dataclass(frozen=True)
class WindowState:
    """State of an aggregation window."""
    window_id: str
    window_start: LogicalTimestamp
    window_end: LogicalTimestamp
    is_closed: bool
    is_committed: bool
    records_aggregated: int
    partial_state_exists: bool
    aggregation_hash: Optional[str]


@dataclass(frozen=True)
class AggregationSnapshot:
    """Snapshot of aggregation state."""
    open_windows: Tuple[WindowState, ...]
    closed_windows: Tuple[WindowState, ...]
    last_committed_window_id: Optional[str]
    snapshot_timestamp: LogicalTimestamp
    snapshot_hash: str

    def has_partial_windows(self) -> bool:
        """Check if any windows are in partial state."""
        return any(w.partial_state_exists for w in self.open_windows)


@dataclass(frozen=True)
class RecoveryContext:
    """
    Complete context for recovery decision.
    
    Immutable snapshot of all system state needed for recovery planning.
    Uses logical timestamps, never system time.
    
    All time references must come from snapshot/reference context.
    This ensures replay determinism.
    """
    context_id: str
    """Unique recovery context identifier"""
    
    reference_timestamp: LogicalTimestamp
    """Logical reference time (NOT wall clock, NOT system time)"""
    
    crash_detected_at: Optional[LogicalTimestamp]
    """Last known good time before crash (logical timestamp)"""
    
    recovery_reason: str
    """Reason for recovery: "CRASH", "RESTART", "REPLAY", "DEPLOYMENT", etc."""
    
    schema_version: str
    """Schema version for compatibility checking"""
    
    config_version: str
    """Configuration version"""
    
    context_hash: str
    """Deterministic hash of context for verification"""
    
    def __post_init__(self):
        """Validate recovery context."""
        if not self.context_id:
            raise ValueError("context_id cannot be empty")
        if not self.reference_timestamp:
            raise ValueError("reference_timestamp cannot be None")
        if not self.context_hash:
            raise ValueError("context_hash cannot be empty")


@dataclass(frozen=True)
class RecoveryConfig:
    """
    Recovery policy configuration.
    
    Immutable configuration for recovery behavior.
    No runtime mutation allowed.
    """
    allow_partial_rewind: bool
    """Whether partial rewind is allowed"""
    
    allow_full_rebuild: bool
    """Whether full rebuild is allowed"""
    
    max_rewind_windows: int
    """Maximum number of windows to rewind (safety limit)"""
    
    require_idempotency_keys: bool
    """Whether idempotency keys are required for all operations"""
    
    strict_transaction_validation: bool
    """Whether to strictly validate transactions"""
    
    corruption_tolerance: str
    """Corruption tolerance level: "STRICT", "MODERATE", "PERMISSIVE" """
    
    stage_dependencies: Dict[RecoveryStage, Tuple[RecoveryStage, ...]]
    """Stage dependency graph (which stages depend on which)"""
    
    config_version: str
    """Configuration version"""
    
    def __post_init__(self):
        """Validate recovery configuration."""
        if self.max_rewind_windows < 0:
            raise ValueError("max_rewind_windows cannot be negative")
        if self.corruption_tolerance not in ("STRICT", "MODERATE", "PERMISSIVE"):
            raise ValueError(
                f"corruption_tolerance must be STRICT, MODERATE, or PERMISSIVE, "
                f"got {self.corruption_tolerance}"
            )


@dataclass(frozen=True)
class ResumeBoundary:
    """
    Safe resume point after recovery.
    
    Must correspond to an atomic commit point.
    """
    stage: RecoveryStage
    boundary_timestamp: LogicalTimestamp
    checkpoint_sequence: int
    idempotency_key: str
    window_id: Optional[str]
    transaction_log_position: Optional[int]
    is_atomic: bool
    atomic_commit_id: str
    window_safe: bool
    transaction_safe: bool

    def __post_init__(self):
        """Validate resume boundary is atomic."""
        if not self.is_atomic:
            raise ValueError("Resume boundary must be atomic")
        if not self.window_safe:
            raise ValueError("Resume boundary must be window-safe")
        if not self.transaction_safe:
            raise ValueError("Resume boundary must be transaction-safe")


@dataclass(frozen=True)
class RewindBoundary:
    """Target boundary for partial rewind."""
    target_stage: RecoveryStage
    target_timestamp: LogicalTimestamp
    target_checkpoint_sequence: int
    reprocessing_range_start: LogicalTimestamp
    reprocessing_range_end: LogicalTimestamp
    affected_stages: Tuple[RecoveryStage, ...]
    requires_side_effect_compensation: bool
    compensation_actions: Tuple[str, ...]


@dataclass(frozen=True)
class RecoveryStep:
    """Single step in recovery plan."""
    step_id: str
    step_type: str  # "VALIDATE", "REWIND", "REPLAY", "REBUILD", "VERIFY"
    target_stage: RecoveryStage
    description: str
    prerequisites: Tuple[str, ...]  # Step IDs that must complete first
    idempotency_key: str
    is_reversible: bool


@dataclass(frozen=True)
class ConsistencyCheck:
    """Consistency validation required during recovery."""
    check_id: str
    check_type: str
    target_stage: RecoveryStage
    validation_rule: str
    expected_state: Dict[str, Any]


@dataclass(frozen=True)
class IdempotencyGuarantee:
    """
    Explicit idempotency guarantee contract.
    
    Required for replay safety.
    """
    effect_scopes: Tuple[str, ...]
    replay_keys: Tuple[str, ...]
    deduplication_authority: str


@dataclass(frozen=True)
class WindowCompletenessGuarantee:
    """
    Window completeness guarantee for aggregation recovery.
    
    Ensures closed windows are immutable and open windows are safe to rebuild.
    """
    closed_windows_immutable: bool
    open_windows_safe: bool
    last_committed_window_id: Optional[str]
    requires_full_rewind: bool
    requires_full_rebuild: bool


@dataclass(frozen=True)
class TransactionReconciliationReport:
    """
    Transaction reconciliation report.
    
    Validates all transactions are fully committed before resume.
    """
    all_transactions_committed: bool
    pending_transaction_count: int
    transaction_id_monotonic: bool
    last_committed_transaction_id: Optional[str]
    orphaned_fragments_detected: bool


@dataclass(frozen=True)
class RecoveryPlan:
    """
    Immutable, deterministic recovery plan.
    
    Declarative specification of recovery - does NOT execute logic.
    """
    plan_id: str
    recovery_mode: RecoveryMode
    ordered_steps: Tuple[RecoveryStep, ...]
    resume_boundary: Optional[ResumeBoundary]
    rewind_boundary: Optional[RewindBoundary]
    affected_stages: Tuple[RecoveryStage, ...]
    consistency_checks: Tuple[ConsistencyCheck, ...]
    idempotency_requirements: Dict[str, str]
    idempotency_guarantee: Optional[IdempotencyGuarantee]
    window_completeness_guarantee: Optional[WindowCompletenessGuarantee]
    transaction_reconciliation: Optional[TransactionReconciliationReport]
    reference_timestamp: LogicalTimestamp  # Logical, not wall clock
    plan_version: str
    plan_hash: str
    
    # Corruption details (if blocked)
    corruption_detected: bool = False
    corruption_reason: Optional[CorruptionReason] = None
    failing_stage: Optional[RecoveryStage] = None
    inconsistency_detail: Optional[str] = None

    def is_blocked(self) -> bool:
        """Check if recovery is blocked."""
        return self.recovery_mode == RecoveryMode.BLOCKED_CORRUPTION_DETECTED

    def requires_manual_intervention(self) -> bool:
        """Check if manual intervention required."""
        return self.is_blocked() or self.recovery_mode == RecoveryMode.FULL_REBUILD_REQUIRED

    def to_dict(self) -> Dict[str, Any]:
        """Serialize plan to dictionary."""
        return {
            "plan_id": self.plan_id,
            "recovery_mode": self.recovery_mode.name,
            "steps": [
                {
                    "step_id": s.step_id,
                    "type": s.step_type,
                    "stage": s.target_stage.value,
                    "description": s.description,
                    "prerequisites": list(s.prerequisites),
                }
                for s in self.ordered_steps
            ],
            "resume_boundary": {
                "stage": self.resume_boundary.stage.value,
                "timestamp": self.resume_boundary.boundary_timestamp.iso_string,
                "sequence": self.resume_boundary.checkpoint_sequence,
            }
            if self.resume_boundary
            else None,
            "affected_stages": [s.value for s in self.affected_stages],
            "corruption_detected": self.corruption_detected,
            "corruption_reason": self.corruption_reason.value
            if self.corruption_reason
            else None,
            "failing_stage": self.failing_stage.value if self.failing_stage else None,
            "plan_hash": self.plan_hash,
        }
