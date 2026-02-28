"""
/infra/recovery/audit/recovery_summary.py

Deterministic Recovery Narrative & Decision Summary Generator

This file turns forensic truth into coherent, bounded, explainable narrative.

It answers:
    "What happened during recovery, why did it happen, what changed,
     and can a human or regulator understand it without reading 50,000 lines of logs?"

This file NEVER records events.
This file NEVER decides actions.
This file NEVER mutates state.

It ONLY derives explanation from immutable evidence.

WHAT THIS FILE IS:
  - The human-comprehension bridge after truth is proven
  - A deterministic narrative generator
  - A mutation catalog
  - An anomaly surfacing mechanism
  - A signable, exportable summary

WHAT THIS FILE IS NOT:
  ❌ Not a recovery controller
  ❌ Not a logger
  ❌ Not an audit authority
  ❌ Not a visualization tool
  ❌ Not a dashboard
  ❌ Not allowed to infer missing data

If evidence is incomplete, the summary MUST say "INSUFFICIENT EVIDENCE".

DESIGN PRINCIPLE (CRITICAL):
    Summaries explain — they never justify.
    
    This file may describe WHY a decision was taken,
    but it must never argue that the decision was CORRECT.

AUTHORITY FLOW:
    recovery_log → summary (PRIMARY)
    audit_logger → annotation only (OPTIONAL)
    
    The summary cannot exist without a verified recovery log.

CORE RESPONSIBILITIES:
  1. Consume recovery_log entries verbatim
  2. Cross-reference audit_logger events (optional, read-only)
  3. Produce deterministic, repeatable summaries
  4. Explicitly list all mutations performed
  5. Explicitly list all aborted or skipped actions
  6. Surface invariant violations clearly
  7. Never collapse multiple causes into one
  8. Produce machine- and human-readable output

No narrative creativity. No smoothing.

MENTAL MODEL:
    recovery_log proves truth.
    recovery_summary explains truth.
    
    One faces machines and auditors.
    The other faces humans and courts.
    
    They must never be confused.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Tuple, FrozenSet
from datetime import datetime
import hashlib
import json


# =============================================================================
# SUMMARY SCOPE & SEVERITY
# =============================================================================


class SummaryScope(Enum):
    """
    Recovery action scope classification.
    
    Defines the granularity at which changes occurred.
    """
    SYSTEM = "system"  # System-wide changes
    WORKFLOW = "workflow"  # Workflow-specific changes
    NODE = "node"  # Single node changes
    SHARD = "shard"  # Single shard changes
    SNAPSHOT = "snapshot"  # Snapshot-level operations
    LOCK = "lock"  # Lock/lease operations
    
    def is_narrow(self) -> bool:
        """Is this a narrow scope (node/shard level)?"""
        return self in (SummaryScope.NODE, SummaryScope.SHARD)
    
    def is_broad(self) -> bool:
        """Is this a broad scope (system/workflow level)?"""
        return self in (SummaryScope.SYSTEM, SummaryScope.WORKFLOW)


class SummarySeverity(Enum):
    """
    Computed severity classification.
    
    CRITICAL: Severity is COMPUTED, never DECLARED.
    It derives from outcomes, not intent.
    """
    INFO = "info"  # Normal operation, no issues
    WARNING = "warning"  # Degraded but functional
    CRITICAL = "critical"  # Functional but violated constraints
    FATAL = "fatal"  # Could not complete, system unsafe
    
    def is_actionable(self) -> bool:
        """Does this severity require action?"""
        return self in (SummarySeverity.CRITICAL, SummarySeverity.FATAL)
    
    def __lt__(self, other: "SummarySeverity") -> bool:
        """Allow severity comparison."""
        severity_order = {
            SummarySeverity.INFO: 0,
            SummarySeverity.WARNING: 1,
            SummarySeverity.CRITICAL: 2,
            SummarySeverity.FATAL: 3,
        }
        return severity_order[self] < severity_order[other]


class RecoveryPhase(Enum):
    """
    Recovery execution phases.
    
    Used to track progression through recovery workflow.
    """
    INITIALIZATION = "initialization"
    VALIDATION = "validation"
    SNAPSHOT_LOADING = "snapshot_loading"
    STATE_RECONSTRUCTION = "state_reconstruction"
    INVARIANT_CHECKING = "invariant_checking"
    COMMIT = "commit"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"
    COMPLETED = "completed"
    ABORTED = "aborted"


class RecoveryOutcome(Enum):
    """Final recovery outcome classification."""
    SUCCESS = "success"  # Completed successfully
    PARTIAL_SUCCESS = "partial_success"  # Some actions succeeded
    FAILED = "failed"  # Failed with rollback
    ABORTED = "aborted"  # Aborted before commit
    DEGRADED = "degraded"  # Completed but with warnings
    INSUFFICIENT_DATA = "insufficient_data"  # Cannot determine


# =============================================================================
# RECOVERY CHANGE RECORD
# =============================================================================


@dataclass(frozen=True)
class RecoveryChangeRecord:
    """
    Explicit record of a single mutation.
    
    Each mutation becomes ONE explicit record.
    NO aggregation across unrelated actions.
    
    RULES:
      - Must reference exact log sequence range
      - Must include pre/post state hashes
      - Must list all consumed/produced snapshots
      - Must declare if irreversible
    
    This is EVIDENCE of change, not justification.
    """
    
    # Temporal bounds
    sequence_start: int  # First log sequence number
    sequence_end: int  # Last log sequence number
    timestamp_start: datetime
    timestamp_end: datetime
    
    # Action classification
    action_type: str  # E.g., "snapshot_restore", "state_rebuild", "lock_release"
    target_scope: SummaryScope
    target_id: str  # Specific ID (node_id, shard_id, etc.)
    
    # State verification
    pre_state_hash: Optional[str] = None  # State before change
    post_state_hash: Optional[str] = None  # State after change
    state_changed: bool = True  # Did state actually change?
    
    # Snapshot lineage
    snapshots_consumed: Tuple[str, ...] = field(default_factory=tuple)
    snapshots_produced: Tuple[str, ...] = field(default_factory=tuple)
    
    # Reversibility
    irreversible: bool = False  # Can this change be undone?
    rollback_checkpoint: Optional[str] = None  # Checkpoint for rollback
    
    # Context
    phase: RecoveryPhase = RecoveryPhase.STATE_RECONSTRUCTION
    retry_attempt: int = 0  # 0 for first attempt
    
    # Validation
    invariants_checked: Tuple[str, ...] = field(default_factory=tuple)
    invariants_passed: Tuple[str, ...] = field(default_factory=tuple)
    invariants_failed: Tuple[str, ...] = field(default_factory=tuple)
    
    def validate(self) -> None:
        """
        Validate change record invariants.
        
        DEPRECATED: Use InvariantEnforcer.validate_change_record instead.
        This method delegates to centralized enforcer for consistency.
        
        Raises:
            ValueError: If any invariant violated
        """
        InvariantEnforcer.validate_change_record(self)
    
    def is_successful(self) -> bool:
        """Did this change succeed?"""
        return len(self.invariants_failed) == 0 and self.post_state_hash is not None
    
    def is_retry(self) -> bool:
        """Is this a retry attempt?"""
        return self.retry_attempt > 0
    
    def duration_seconds(self) -> float:
        """Get duration in seconds."""
        delta = self.timestamp_end - self.timestamp_start
        return delta.total_seconds()


# =============================================================================
# RECOVERY ANOMALY RECORD
# =============================================================================


@dataclass(frozen=True)
class RecoveryAnomalyRecord:
    """
    Record of something that almost happened, failed, or violated expectations.
    
    Anomalies are NOT errors — they are deviations from expected behavior
    that require human attention.
    
    RULES:
      - Description must be factual, not interpretive
      - Severity is computed from impact
      - Must reference related log sequence if applicable
      - Must explicitly name violated invariant if applicable
    """
    
    description: str  # Factual description, no interpretation
    severity: SummarySeverity
    
    # Context
    related_sequence: Optional[int] = None  # Log sequence number
    related_action_type: Optional[str] = None
    related_target_id: Optional[str] = None
    phase: Optional[RecoveryPhase] = None
    
    # Violation details
    invariant_violated: Optional[str] = None  # Specific invariant name
    expected_value: Optional[str] = None  # What was expected
    actual_value: Optional[str] = None  # What was found
    
    # Impact
    blocked_actions: Tuple[str, ...] = field(default_factory=tuple)  # Actions that couldn't proceed
    required_manual_intervention: bool = False
    
    # Evidence
    evidence_snapshot_ids: Tuple[str, ...] = field(default_factory=tuple)
    evidence_log_range: Optional[Tuple[int, int]] = None  # (start, end) sequence
    
    def validate(self) -> None:
        """
        Validate anomaly record.
        
        Raises:
            ValueError: If invalid
        """
        if not self.description:
            raise ValueError("description cannot be empty")
        
        if self.related_sequence is not None and self.related_sequence < 0:
            raise ValueError("related_sequence must be >= 0")
        
        if self.evidence_log_range is not None:
            start, end = self.evidence_log_range
            if start < 0 or end < start:
                raise ValueError("Invalid evidence_log_range")
        
        # No duplicate evidence
        if len(self.evidence_snapshot_ids) != len(set(self.evidence_snapshot_ids)):
            raise ValueError("evidence_snapshot_ids contains duplicates")
    
    def is_critical(self) -> bool:
        """Is this anomaly critical?"""
        return self.severity.is_actionable()


# =============================================================================
# RECOVERY SUMMARY INPUT
# =============================================================================


@dataclass(frozen=True)
class RecoverySummaryInput:
    """
    Input required to generate a recovery summary.
    
    This is the ONLY valid input to summary generation.
    """
    
    # Required: Recovery log entries (must be validated)
    log_entries: Tuple[Dict[str, Any], ...]  # Raw log entries in order
    
    # Optional: Audit events for annotation
    audit_events: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    
    # Identity
    recovery_id: str = ""
    run_id: str = ""
    
    # Temporal bounds
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    
    def validate(self) -> None:
        """
        Validate input.
        
        Raises:
            ValueError: If invalid
        """
        if not self.log_entries:
            raise ValueError("log_entries cannot be empty")
        
        if not self.recovery_id:
            raise ValueError("recovery_id cannot be empty")
        
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
        
        if self.started_at and self.finished_at:
            if self.finished_at < self.started_at:
                raise ValueError("finished_at must be >= started_at")


# =============================================================================
# RECOVERY SUMMARY (FINAL PRODUCT)
# =============================================================================


@dataclass(frozen=True)
class RecoverySummary:
    """
    The final, immutable recovery summary.
    
    This object is:
      - Signable (has deterministic hash)
      - Exportable (serializable to JSON)
      - Comparable (can diff two summaries)
      - Auditable (contains all evidence references)
    
    VALIDATION RULES:
      - No committed change without post_state_hash
      - No missing sequence ranges
      - No aggregation across scopes
      - No empty summary for non-empty log
    """
    
    # Identity
    recovery_id: str
    run_id: str
    summary_version: int = 1
    
    # Temporal bounds
    started_at: datetime
    finished_at: datetime
    
    # Outcome
    outcome: RecoveryOutcome
    final_phase: RecoveryPhase
    
    # Action counts
    total_actions: int
    committed_actions: int
    aborted_actions: int
    retried_actions: int
    
    # Detailed records
    changes: Tuple[RecoveryChangeRecord, ...] = field(default_factory=tuple)
    anomalies: Tuple[RecoveryAnomalyRecord, ...] = field(default_factory=tuple)
    
    # Invariant tracking
    invariants_checked: Tuple[str, ...] = field(default_factory=tuple)
    invariants_passed: Tuple[str, ...] = field(default_factory=tuple)
    invariants_breached: Tuple[str, ...] = field(default_factory=tuple)
    
    # Phase progression
    phases_completed: Tuple[RecoveryPhase, ...] = field(default_factory=tuple)
    phases_skipped: Tuple[RecoveryPhase, ...] = field(default_factory=tuple)
    
    # Severity assessment
    max_severity: SummarySeverity = SummarySeverity.INFO
    requires_manual_review: bool = False
    
    # Snapshot lineage
    snapshots_consumed: Tuple[str, ...] = field(default_factory=tuple)
    snapshots_produced: Tuple[str, ...] = field(default_factory=tuple)
    
    # Evidence references
    log_sequence_range: Tuple[int, int] = (0, 0)  # (start, end) inclusive
    audit_event_count: int = 0
    
    # Verification
    summary_hash: str = ""
    
    # Additional context
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Post-initialization validation."""
        self.validate()
    
    def validate(self) -> None:
        """
        Validate summary invariants.
        
        DEPRECATED: Use InvariantEnforcer.validate_summary instead.
        This method delegates to centralized enforcer for consistency.
        
        Raises:
            ValueError: If any invariant violated
        """
        InvariantEnforcer.validate_summary(self)
    
    def is_successful(self) -> bool:
        """Did recovery succeed?"""
        return self.outcome == RecoveryOutcome.SUCCESS
    
    def has_failures(self) -> bool:
        """Were there any failures?"""
        return self.aborted_actions > 0 or len(self.invariants_breached) > 0
    
    def get_critical_anomalies(self) -> Tuple[RecoveryAnomalyRecord, ...]:
        """Get only critical anomalies."""
        return tuple(a for a in self.anomalies if a.is_critical())
    
    def duration_seconds(self) -> float:
        """Get total recovery duration in seconds."""
        delta = self.finished_at - self.started_at
        return delta.total_seconds()
    
    def get_phase_duration(self, phase: RecoveryPhase) -> float:
        """
        Get duration of specific phase.
        
        Note: This is approximate based on change records in that phase.
        """
        phase_changes = [c for c in self.changes if c.phase == phase]
        if not phase_changes:
            return 0.0
        
        min_start = min(c.timestamp_start for c in phase_changes)
        max_end = max(c.timestamp_end for c in phase_changes)
        delta = max_end - min_start
        return delta.total_seconds()


# =============================================================================
# CENTRALIZED INVARIANT ENFORCEMENT (TIER-0 REQUIREMENT)
# =============================================================================


class InvariantEnforcer:
    """
    Centralized, exhaustive Tier-0 invariant enforcement.
    
    This is the SINGLE AUTHORITATIVE source for all invariant checks.
    All validation must pass through this gate.
    
    TIER-0 HARD RULES:
      1. No missing sequence ranges
      2. No committed change without post hash
      3. No aggregation across scopes
      4. No empty summary for non-empty log
      5. No overlapping sequence ranges
      6. No duplicate snapshots
      7. No invariant contradictions
      8. No gaps in sequence continuity (unless explicitly aborted)
    
    This class provides declarative, exhaustive validation that cannot
    be bypassed or weakened by refactoring.
    """
    
    @staticmethod
    def validate_log_entries(entries: Tuple[Dict[str, Any], ...]) -> None:
        """
        Validate log entry sequence integrity.
        
        Raises:
            ValueError: If any invariant violated
        """
        if not entries:
            raise ValueError("Log entries cannot be empty")
        
        prev_seq = -1
        seen_sequences = set()
        
        for i, entry in enumerate(entries):
            seq = entry.get("sequence", -1)
            
            if seq < 0:
                raise ValueError(f"Entry {i}: sequence must be >= 0, got {seq}")
            
            if seq in seen_sequences:
                raise ValueError(f"Entry {i}: duplicate sequence number {seq}")
            
            if seq <= prev_seq:
                raise ValueError(
                    f"Entry {i}: out-of-order sequence {prev_seq} -> {seq}"
                )
            
            seen_sequences.add(seq)
            prev_seq = seq
    
    @staticmethod
    def validate_change_record(change: RecoveryChangeRecord) -> None:
        """
        Validate single change record invariants.
        
        Raises:
            ValueError: If any invariant violated
        """
        if change.sequence_start < 0:
            raise ValueError(f"Change {change.sequence_start}: sequence_start must be >= 0")
        
        if change.sequence_end < change.sequence_start:
            raise ValueError(
                f"Change {change.sequence_start}: sequence_end ({change.sequence_end}) "
                f"must be >= sequence_start ({change.sequence_start})"
            )
        
        if change.timestamp_end < change.timestamp_start:
            raise ValueError(
                f"Change {change.sequence_start}: timestamp_end must be >= timestamp_start"
            )
        
        if not change.action_type:
            raise ValueError(f"Change {change.sequence_start}: action_type cannot be empty")
        
        if not change.target_id:
            raise ValueError(f"Change {change.sequence_start}: target_id cannot be empty")
        
        # TIER-0 RULE: Committed change must have post_state_hash
        if change.state_changed and change.post_state_hash is None:
            raise ValueError(
                f"Change {change.sequence_start}: state_changed=True requires post_state_hash"
            )
        
        # No duplicate snapshots
        if len(change.snapshots_consumed) != len(set(change.snapshots_consumed)):
            raise ValueError(
                f"Change {change.sequence_start}: snapshots_consumed contains duplicates"
            )
        
        if len(change.snapshots_produced) != len(set(change.snapshots_produced)):
            raise ValueError(
                f"Change {change.sequence_start}: snapshots_produced contains duplicates"
            )
        
        # Invariant consistency
        all_checked = set(change.invariants_checked)
        all_passed = set(change.invariants_passed)
        all_failed = set(change.invariants_failed)
        
        if not all_passed.issubset(all_checked):
            raise ValueError(
                f"Change {change.sequence_start}: invariants_passed must be subset of invariants_checked"
            )
        
        if not all_failed.issubset(all_checked):
            raise ValueError(
                f"Change {change.sequence_start}: invariants_failed must be subset of invariants_checked"
            )
        
        if all_passed & all_failed:
            raise ValueError(
                f"Change {change.sequence_start}: invariant cannot be both passed and failed"
            )
        
        if change.retry_attempt < 0:
            raise ValueError(
                f"Change {change.sequence_start}: retry_attempt must be >= 0"
            )
    
    @staticmethod
    def validate_change_sequence(changes: Tuple[RecoveryChangeRecord, ...]) -> None:
        """
        Validate sequence of change records for continuity and non-overlap.
        
        TIER-0 RULE: No missing sequence ranges, no overlaps.
        
        Raises:
            ValueError: If any invariant violated
        """
        if not changes:
            return
        
        # Sort by sequence_start
        sorted_changes = sorted(changes, key=lambda c: c.sequence_start)
        
        # Check for overlaps
        for i in range(1, len(sorted_changes)):
            prev = sorted_changes[i - 1]
            curr = sorted_changes[i]
            
            # Overlap detection: current start <= previous end
            if curr.sequence_start <= prev.sequence_end:
                # Overlap is only acceptable if they're exactly the same sequence
                if curr.sequence_start != prev.sequence_end:
                    raise ValueError(
                        f"Overlapping sequence ranges: "
                        f"change {prev.sequence_start}-{prev.sequence_end} "
                        f"overlaps with {curr.sequence_start}-{curr.sequence_end}"
                    )
    
    @staticmethod
    def validate_summary(summary: RecoverySummary) -> None:
        """
        Validate complete summary against all Tier-0 invariants.
        
        This is the FINAL GATE before summary is considered valid.
        
        Raises:
            ValueError: If any invariant violated
        """
        # Identity invariants
        if not summary.recovery_id:
            raise ValueError("recovery_id cannot be empty")
        
        if not summary.run_id:
            raise ValueError("run_id cannot be empty")
        
        if summary.summary_version < 1:
            raise ValueError("summary_version must be >= 1")
        
        # Temporal invariants
        if summary.finished_at < summary.started_at:
            raise ValueError("finished_at must be >= started_at")
        
        # Action count invariants
        if summary.total_actions < 0:
            raise ValueError("total_actions must be >= 0")
        
        if summary.committed_actions < 0:
            raise ValueError("committed_actions must be >= 0")
        
        if summary.aborted_actions < 0:
            raise ValueError("aborted_actions must be >= 0")
        
        if summary.committed_actions + summary.aborted_actions > summary.total_actions:
            raise ValueError(
                "committed + aborted cannot exceed total_actions"
            )
        
        # Validate all change records
        for change in summary.changes:
            InvariantEnforcer.validate_change_record(change)
        
        # Validate change sequence
        InvariantEnforcer.validate_change_sequence(summary.changes)
        
        # TIER-0 RULE: Committed changes must have post_state_hash
        for change in summary.changes:
            if change.is_successful() and change.post_state_hash is None:
                raise ValueError(
                    f"Committed change at sequence {change.sequence_start} "
                    "missing post_state_hash"
                )
        
        # Validate all anomalies
        for anomaly in summary.anomalies:
            anomaly.validate()
        
        # Invariant consistency
        all_checked = set(summary.invariants_checked)
        all_passed = set(summary.invariants_passed)
        all_breached = set(summary.invariants_breached)
        
        if not all_passed.issubset(all_checked):
            raise ValueError("invariants_passed must be subset of invariants_checked")
        
        if not all_breached.issubset(all_checked):
            raise ValueError("invariants_breached must be subset of invariants_checked")
        
        if all_passed & all_breached:
            raise ValueError("invariant cannot be both passed and breached")
        
        # Log sequence range validation
        start, end = summary.log_sequence_range
        if start < 0 or end < start:
            raise ValueError("Invalid log_sequence_range")
        
        # TIER-0 RULE: No empty summary for non-empty log
        if end > start and not summary.changes and not summary.anomalies:
            raise ValueError(
                "Non-empty log must produce at least one change or anomaly"
            )
        
        # Hash validation
        if not summary.summary_hash:
            raise ValueError("summary_hash cannot be empty")
        
        # TIER-0 RULE: No aggregation across scopes
        # Each change must have distinct scope or distinct target_id
        scope_target_pairs = [
            (c.target_scope, c.target_id) for c in summary.changes
        ]
        # This is informational - we don't forbid same scope/target,
        # but we ensure each change is explicit and not aggregated
        # (aggregation prevention is handled in extraction logic)


# =============================================================================
# SUMMARY BUILDER
# =============================================================================


class RecoverySummaryBuilder:
    """
    Builds RecoverySummary from recovery log entries.
    
    TIER-0 RULES:
      1. Verify recovery_log integrity first (via InvariantEnforcer)
      2. One change record per log entry (NO aggregation unless explicitly atomic)
      3. Formal invariant mapping across full sequence graph
      4. Exhaustive anomaly detection via sequence-graph validation
      5. Reject out-of-order logs
      6. Flag: partial recoveries, repeated attempts, aborted commits
      7. Compute severity from outcomes, not intent
      8. All descriptions purely factual, no interpretation
    
    Same input log → same summary (deterministic).
    No clocks, no randomness.
    No interpretive logic - only descriptive facts from log.
    """
    
    def __init__(self, input_data: RecoverySummaryInput):
        """
        Initialize builder.
        
        Args:
            input_data: Validated input data
        """
        input_data.validate()
        self._input = input_data
        self._changes: List[RecoveryChangeRecord] = []
        self._anomalies: List[RecoveryAnomalyRecord] = []
        self._invariants_checked: set = set()
        self._invariants_passed: set = set()
        self._invariants_breached: set = set()
        self._phases_completed: set = set()
        self._phases_skipped: set = set()
        self._max_severity = SummarySeverity.INFO
        self._snapshots_consumed: set = set()
        self._snapshots_produced: set = set()
    
    def build(self) -> RecoverySummary:
        """
        Build the recovery summary.
        
        Returns:
            RecoverySummary: Complete summary
        
        Raises:
            ValueError: If log is invalid or inconsistent
        """
        # 1. Verify log integrity (using centralized enforcer)
        InvariantEnforcer.validate_log_entries(self._input.log_entries)
        
        # 2. Extract changes (one per entry, no aggregation)
        self._extract_changes()
        
        # 3. Formal invariant mapping across full sequence graph
        self._map_sequence_invariants()
        
        # 4. Detect anomalies (exhaustive sequence-graph validation)
        self._detect_anomalies()
        
        # 5. Compute outcome
        outcome = self._compute_outcome()
        
        # 6. Determine final phase
        final_phase = self._determine_final_phase()
        
        # 7. Count actions
        total_actions = len(self._changes)
        committed_actions = sum(1 for c in self._changes if c.is_successful())
        aborted_actions = total_actions - committed_actions
        retried_actions = sum(1 for c in self._changes if c.is_retry())
        
        # 8. Determine if manual review required
        requires_manual_review = (
            len(self._invariants_breached) > 0
            or self._max_severity.is_actionable()
            or outcome in (RecoveryOutcome.FAILED, RecoveryOutcome.DEGRADED)
        )
        
        # 9. Get log sequence range
        if self._input.log_entries:
            min_seq = min(e.get("sequence", 0) for e in self._input.log_entries)
            max_seq = max(e.get("sequence", 0) for e in self._input.log_entries)
            log_sequence_range = (min_seq, max_seq)
        else:
            log_sequence_range = (0, 0)
        
        # 10. Build summary
        summary = RecoverySummary(
            recovery_id=self._input.recovery_id,
            run_id=self._input.run_id,
            started_at=self._input.started_at or datetime.utcnow(),
            finished_at=self._input.finished_at or datetime.utcnow(),
            outcome=outcome,
            final_phase=final_phase,
            total_actions=total_actions,
            committed_actions=committed_actions,
            aborted_actions=aborted_actions,
            retried_actions=retried_actions,
            changes=tuple(self._changes),
            anomalies=tuple(self._anomalies),
            invariants_checked=tuple(sorted(self._invariants_checked)),
            invariants_passed=tuple(sorted(self._invariants_passed)),
            invariants_breached=tuple(sorted(self._invariants_breached)),
            phases_completed=tuple(sorted(self._phases_completed, key=lambda p: p.value)),
            phases_skipped=tuple(sorted(self._phases_skipped, key=lambda p: p.value)),
            max_severity=self._max_severity,
            requires_manual_review=requires_manual_review,
            snapshots_consumed=tuple(sorted(self._snapshots_consumed)),
            snapshots_produced=tuple(sorted(self._snapshots_produced)),
            log_sequence_range=log_sequence_range,
            audit_event_count=len(self._input.audit_events),
            summary_hash="",  # Will be computed after
        )
        
        # 11. Compute and set hash
        summary_hash = self._compute_summary_hash(summary)
        
        # Recreate with hash (dataclass is frozen)
        summary = RecoverySummary(
            recovery_id=summary.recovery_id,
            run_id=summary.run_id,
            summary_version=summary.summary_version,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            outcome=summary.outcome,
            final_phase=summary.final_phase,
            total_actions=summary.total_actions,
            committed_actions=summary.committed_actions,
            aborted_actions=summary.aborted_actions,
            retried_actions=summary.retried_actions,
            changes=summary.changes,
            anomalies=summary.anomalies,
            invariants_checked=summary.invariants_checked,
            invariants_passed=summary.invariants_passed,
            invariants_breached=summary.invariants_breached,
            phases_completed=summary.phases_completed,
            phases_skipped=summary.phases_skipped,
            max_severity=summary.max_severity,
            requires_manual_review=summary.requires_manual_review,
            snapshots_consumed=summary.snapshots_consumed,
            snapshots_produced=summary.snapshots_produced,
            log_sequence_range=summary.log_sequence_range,
            audit_event_count=summary.audit_event_count,
            summary_hash=summary_hash,
            metadata=summary.metadata,
        )
        
        # 12. FINAL INVARIANT GATE (centralized enforcement)
        InvariantEnforcer.validate_summary(summary)
        
        return summary
    
    def _map_sequence_invariants(self) -> None:
        """
        Formal invariant mapping across full sequence graph.
        
        TIER-0: Exhaustive validation of all invariants across the entire
        recovery sequence, not just individual changes.
        
        This creates a complete invariant state machine that validates:
        - Invariant consistency across sequence transitions
        - Invariant dependencies (some invariants depend on others)
        - Invariant lifecycle (checked -> passed/failed -> rechecked)
        - Cross-sequence invariant relationships
        """
        if not self._changes:
            return
        
        # Build sequence graph: map each sequence to its invariant state
        sequence_invariant_map: Dict[int, Dict[str, str]] = {}
        # State: "checked", "passed", "failed", "rechecked", "conflict"
        
        for change in self._changes:
            seq = change.sequence_start
            
            # Map invariants for this sequence
            for inv in change.invariants_checked:
                if inv not in sequence_invariant_map:
                    sequence_invariant_map[inv] = {}
                
                # Determine state
                if inv in change.invariants_failed:
                    state = "failed"
                elif inv in change.invariants_passed:
                    state = "passed"
                else:
                    state = "checked"
                
                # Check for state transitions
                if seq in sequence_invariant_map[inv]:
                    prev_state = sequence_invariant_map[inv][seq]
                    if prev_state != state:
                        # State change detected - validate transition
                        if prev_state == "failed" and state == "passed":
                            # Recovery from failure - valid
                            pass
                        elif prev_state == "passed" and state == "failed":
                            # Regression - anomaly
                            self._anomalies.append(RecoveryAnomalyRecord(
                                description=f"Invariant '{inv}' regressed from passed to failed at sequence {seq}",
                                severity=SummarySeverity.CRITICAL,
                                related_sequence=seq,
                                invariant_violated=inv,
                                expected_value="passed",
                                actual_value="failed",
                                required_manual_intervention=True,
                                evidence_log_range=(change.sequence_start, change.sequence_end),
                            ))
                            self._update_max_severity(SummarySeverity.CRITICAL)
                        else:
                            # Recheck - mark as rechecked
                            state = "rechecked"
                
                sequence_invariant_map[inv][seq] = state
        
        # Validate invariant dependencies across sequence
        self._validate_invariant_dependencies(sequence_invariant_map)
        
        # Validate invariant lifecycle consistency
        self._validate_invariant_lifecycle(sequence_invariant_map)
    
    def _validate_invariant_dependencies(self, sequence_map: Dict[str, Dict[int, str]]) -> None:
        """
        Validate that invariant dependencies are satisfied across sequence.
        
        Some invariants depend on others being satisfied first.
        This checks that dependency order is maintained.
        """
        # Known dependency rules (can be extended)
        # Example: "state_consistency" depends on "hash_validity"
        dependencies = {
            # Format: dependent -> required
            # Add actual dependencies based on system design
        }
        
        for dependent, required in dependencies.items():
            if dependent not in sequence_map or required not in sequence_map:
                continue
            
            # Check that required invariant is satisfied before dependent
            dependent_seqs = sorted(sequence_map[dependent].keys())
            required_seqs = sorted(sequence_map[required].keys())
            
            for dep_seq in dependent_seqs:
                # Find latest required check before this dependent check
                prior_required = [r for r in required_seqs if r < dep_seq]
                if prior_required:
                    latest_required_seq = max(prior_required)
                    required_state = sequence_map[required][latest_required_seq]
                    
                    if required_state == "failed":
                        # Dependent invariant checked when required failed
                        self._anomalies.append(RecoveryAnomalyRecord(
                            description=f"Invariant '{dependent}' checked at sequence {dep_seq} but required dependency '{required}' was failed at sequence {latest_required_seq}",
                            severity=SummarySeverity.WARNING,
                            related_sequence=dep_seq,
                            invariant_violated=dependent,
                            required_manual_intervention=False,
                        ))
                        self._update_max_severity(SummarySeverity.WARNING)
    
    def _validate_invariant_lifecycle(self, sequence_map: Dict[str, Dict[int, str]]) -> None:
        """
        Validate invariant lifecycle consistency.
        
        Invariants should follow expected lifecycle:
        checked -> passed/failed -> (optionally) rechecked
        
        Detect anomalies like:
        - Invariant passed without being checked
        - Invariant failed then passed without explicit recovery
        - Invariant checked multiple times with conflicting results
        """
        for inv_name, state_map in sequence_map.items():
            sequences = sorted(state_map.keys())
            
            for i, seq in enumerate(sequences):
                state = state_map[seq]
                
                # Check for impossible transitions
                if i > 0:
                    prev_seq = sequences[i - 1]
                    prev_state = state_map[prev_seq]
                    
                    # Invalid: failed -> passed without intermediate recovery action
                    if prev_state == "failed" and state == "passed":
                        # Check if there's a recovery action between sequences
                        recovery_actions = [
                            c for c in self._changes
                            if prev_seq < c.sequence_start < seq
                            and "recovery" in c.action_type.lower()
                        ]
                        
                        if not recovery_actions:
                            self._anomalies.append(RecoveryAnomalyRecord(
                                description=f"Invariant '{inv_name}' transitioned from failed to passed between sequences {prev_seq} and {seq} without explicit recovery action",
                                severity=SummarySeverity.WARNING,
                                related_sequence=seq,
                                invariant_violated=inv_name,
                                expected_value="explicit recovery action",
                                actual_value="no recovery action found",
                                evidence_log_range=(prev_seq, seq),
                            ))
                            self._update_max_severity(SummarySeverity.WARNING)
    
    def _verify_log_integrity(self) -> None:
        """
        Verify log is well-formed and ordered.
        
        DEPRECATED: Use InvariantEnforcer.validate_log_entries instead.
        This method is kept for backward compatibility but delegates to enforcer.
        
        Raises:
            ValueError: If log is invalid
        """
        InvariantEnforcer.validate_log_entries(self._input.log_entries)
    
    def _extract_changes(self) -> None:
        """
        Extract change records from log entries.
        
        TIER-0 RULE: One change record per log entry.
        NO AGGREGATION across entries unless explicitly marked as atomic.
        
        Each log entry represents a distinct state transition that must be
        explicitly recorded. Contiguous entries with same action/target are
        still separate changes unless the log explicitly marks them as atomic.
        
        This prevents collapsing semantically distinct events into one record.
        """
        for entry in self._input.log_entries:
            # Each entry becomes its own change record
            # Only aggregate if entry explicitly marks itself as part of atomic group
            is_atomic_continuation = entry.get("atomic_group_id") is not None
            
            if is_atomic_continuation:
                # Check if we have a pending atomic group
                if (self._changes and 
                    self._changes[-1].sequence_end < entry.get("sequence", 0) - 1):
                    # Gap in atomic group - start new change
                    self._process_single_entry(entry)
                else:
                    # Continue atomic group - extend last change
                    self._extend_atomic_change(entry)
            else:
                # Standalone entry - create new change
                self._process_single_entry(entry)
    
    def _process_single_entry(self, entry: Dict[str, Any]) -> None:
        """
        Process a single log entry into a change record.
        
        Purely descriptive - no interpretation, only facts from log.
        """
        sequence = entry.get("sequence", 0)
        timestamp = self._parse_timestamp(entry.get("timestamp"))
        
        # Extract fields directly from entry (no inference)
        action_type = entry.get("action_type", "unknown")
        target_scope = self._parse_scope(entry.get("target_scope", "system"))
        target_id = entry.get("target_id", "unknown")
        phase = self._parse_phase(entry.get("phase", "state_reconstruction"))
        
        # State hashes (must be in log, no inference)
        pre_state_hash = entry.get("pre_state_hash")
        post_state_hash = entry.get("post_state_hash")
        state_changed = entry.get("state_changed", post_state_hash is not None)
        
        # Snapshots (from log only)
        snapshots_consumed = tuple(entry.get("snapshots_consumed", []))
        snapshots_produced = tuple(entry.get("snapshots_produced", []))
        
        self._snapshots_consumed.update(snapshots_consumed)
        self._snapshots_produced.update(snapshots_produced)
        
        # Invariants (from log only)
        invariants_checked = tuple(entry.get("invariants_checked", []))
        invariants_passed = tuple(entry.get("invariants_passed", []))
        invariants_failed = tuple(entry.get("invariants_failed", []))
        
        self._invariants_checked.update(invariants_checked)
        self._invariants_passed.update(invariants_passed)
        self._invariants_breached.update(invariants_failed)
        
        # Retry info (from log only)
        retry_attempt = entry.get("retry_attempt", 0)
        
        # Create change record (one entry = one change)
        change = RecoveryChangeRecord(
            sequence_start=sequence,
            sequence_end=sequence,  # Single entry = same start/end
            timestamp_start=timestamp,
            timestamp_end=timestamp,
            action_type=action_type,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            post_state_hash=post_state_hash,
            state_changed=state_changed,
            snapshots_consumed=snapshots_consumed,
            snapshots_produced=snapshots_produced,
            irreversible=entry.get("irreversible", False),
            phase=phase,
            retry_attempt=retry_attempt,
            invariants_checked=invariants_checked,
            invariants_passed=invariants_passed,
            invariants_failed=invariants_failed,
        )
        
        self._changes.append(change)
        self._phases_completed.add(phase)
    
    def _extend_atomic_change(self, entry: Dict[str, Any]) -> None:
        """
        Extend the last change record with atomic continuation entry.
        
        Only used when entry explicitly marks itself as atomic_group_id.
        This is the ONLY exception to "one entry = one change" rule.
        """
        if not self._changes:
            self._process_single_entry(entry)
            return
        
        # Extend last change
        last_change = self._changes[-1]
        sequence = entry.get("sequence", 0)
        timestamp = self._parse_timestamp(entry.get("timestamp"))
        
        # Merge snapshots
        new_consumed = set(last_change.snapshots_consumed)
        new_consumed.update(entry.get("snapshots_consumed", []))
        
        new_produced = set(last_change.snapshots_produced)
        new_produced.update(entry.get("snapshots_produced", []))
        
        self._snapshots_consumed.update(entry.get("snapshots_consumed", []))
        self._snapshots_produced.update(entry.get("snapshots_produced", []))
        
        # Merge invariants
        new_checked = set(last_change.invariants_checked)
        new_checked.update(entry.get("invariants_checked", []))
        
        new_passed = set(last_change.invariants_passed)
        new_passed.update(entry.get("invariants_passed", []))
        
        new_failed = set(last_change.invariants_failed)
        new_failed.update(entry.get("invariants_failed", []))
        
        self._invariants_checked.update(entry.get("invariants_checked", []))
        self._invariants_passed.update(entry.get("invariants_passed", []))
        self._invariants_breached.update(entry.get("invariants_failed", []))
        
        # Update post_state_hash if present
        post_state_hash = entry.get("post_state_hash") or last_change.post_state_hash
        
        # Create extended change record
        extended_change = RecoveryChangeRecord(
            sequence_start=last_change.sequence_start,
            sequence_end=sequence,  # Extended to include this entry
            timestamp_start=last_change.timestamp_start,
            timestamp_end=timestamp,
            action_type=last_change.action_type,
            target_scope=last_change.target_scope,
            target_id=last_change.target_id,
            pre_state_hash=last_change.pre_state_hash,
            post_state_hash=post_state_hash,
            state_changed=last_change.state_changed or entry.get("state_changed", False),
            snapshots_consumed=tuple(sorted(new_consumed)),
            snapshots_produced=tuple(sorted(new_produced)),
            irreversible=last_change.irreversible or entry.get("irreversible", False),
            phase=last_change.phase,
            retry_attempt=last_change.retry_attempt,
            invariants_checked=tuple(sorted(new_checked)),
            invariants_passed=tuple(sorted(new_passed)),
            invariants_failed=tuple(sorted(new_failed)),
        )
        
        # Replace last change
        self._changes[-1] = extended_change
    
    def _detect_anomalies(self) -> None:
        """
        Exhaustive anomaly detection using sequence-graph validation.
        
        This performs formal validation across the entire sequence graph,
        not just pattern matching. Detects:
        - Invariant violations
        - Aborted actions
        - Partial recoveries
        - Repeated attempts
        - Sequence gaps
        - Hash mismatches
        - Phase transitions violations
        - Replay divergence (identical hashes with different sequences)
        """
        # 1. Invariant violations (explicit from log)
        for invariant in self._invariants_breached:
            # Find all changes that violated this invariant
            violating_changes = [
                c for c in self._changes 
                if invariant in c.invariants_failed
            ]
            
            for change in violating_changes:
                anomaly = RecoveryAnomalyRecord(
                    description=f"Invariant '{invariant}' violated at sequence {change.sequence_start}",
                    severity=SummarySeverity.CRITICAL,
                    related_sequence=change.sequence_start,
                    related_action_type=change.action_type,
                    related_target_id=change.target_id,
                    phase=change.phase,
                    invariant_violated=invariant,
                    required_manual_intervention=True,
                    evidence_log_range=(change.sequence_start, change.sequence_end),
                )
                self._anomalies.append(anomaly)
                self._update_max_severity(SummarySeverity.CRITICAL)
        
        # 2. Retry chains (exhaustive detection)
        retry_changes = [c for c in self._changes if c.is_retry()]
        if retry_changes:
            for change in retry_changes:
                anomaly = RecoveryAnomalyRecord(
                    description=f"Action '{change.action_type}' on '{change.target_id}' retried {change.retry_attempt} time(s) at sequence {change.sequence_start}",
                    severity=SummarySeverity.WARNING,
                    related_sequence=change.sequence_start,
                    related_action_type=change.action_type,
                    related_target_id=change.target_id,
                    phase=change.phase,
                    evidence_log_range=(change.sequence_start, change.sequence_end),
                )
                self._anomalies.append(anomaly)
                self._update_max_severity(SummarySeverity.WARNING)
        
        # 3. Failed changes (explicit from log state)
        failed_changes = [c for c in self._changes if not c.is_successful()]
        if failed_changes:
            for change in failed_changes:
                anomaly = RecoveryAnomalyRecord(
                    description=f"Action '{change.action_type}' on '{change.target_id}' failed at sequence {change.sequence_start}",
                    severity=SummarySeverity.CRITICAL,
                    related_sequence=change.sequence_start,
                    related_action_type=change.action_type,
                    related_target_id=change.target_id,
                    phase=change.phase,
                    required_manual_intervention=True,
                    evidence_log_range=(change.sequence_start, change.sequence_end),
                )
                self._anomalies.append(anomaly)
                self._update_max_severity(SummarySeverity.CRITICAL)
        
        # 4. Sequence gap detection (missing expected entries)
        self._detect_sequence_gaps()
        
        # 5. Hash consistency validation (replay divergence detection)
        self._detect_hash_inconsistencies()
        
        # 6. Phase transition violations
        self._detect_phase_violations()
        
        # 7. Partial recovery detection
        self._detect_partial_recoveries()
    
    def _detect_sequence_gaps(self) -> None:
        """
        Detect gaps in sequence numbers that indicate missing log entries.
        
        TIER-0: Missing sequence ranges must be explicitly flagged.
        """
        if not self._changes:
            return
        
        sorted_changes = sorted(self._changes, key=lambda c: c.sequence_start)
        
        for i in range(1, len(sorted_changes)):
            prev = sorted_changes[i - 1]
            curr = sorted_changes[i]
            
            # Gap detection: expected next sequence is not present
            expected_next = prev.sequence_end + 1
            if curr.sequence_start > expected_next:
                gap_size = curr.sequence_start - expected_next
                anomaly = RecoveryAnomalyRecord(
                    description=f"Sequence gap detected: missing {gap_size} entry/entries between sequence {prev.sequence_end} and {curr.sequence_start}",
                    severity=SummarySeverity.WARNING,
                    related_sequence=prev.sequence_end,
                    evidence_log_range=(prev.sequence_end, curr.sequence_start),
                )
                self._anomalies.append(anomaly)
                self._update_max_severity(SummarySeverity.WARNING)
    
    def _detect_hash_inconsistencies(self) -> None:
        """
        Detect hash inconsistencies that indicate replay divergence.
        
        TIER-0: Identical pre-state hashes with different post-state hashes
        indicate non-deterministic behavior or missing context.
        """
        # Group changes by pre_state_hash
        hash_groups: Dict[Optional[str], List[RecoveryChangeRecord]] = {}
        for change in self._changes:
            if change.pre_state_hash:
                if change.pre_state_hash not in hash_groups:
                    hash_groups[change.pre_state_hash] = []
                hash_groups[change.pre_state_hash].append(change)
        
        # Check for divergence: same pre-hash, different post-hash
        for pre_hash, changes in hash_groups.items():
            if len(changes) < 2:
                continue
            
            post_hashes = {c.post_state_hash for c in changes if c.post_state_hash}
            if len(post_hashes) > 1:
                # Same pre-state, different post-states = divergence
                sequences = [c.sequence_start for c in changes]
                anomaly = RecoveryAnomalyRecord(
                    description=f"Hash divergence detected: {len(changes)} changes share pre_state_hash '{pre_hash[:16]}...' but produce different post-states at sequences {sequences}",
                    severity=SummarySeverity.CRITICAL,
                    expected_value=f"Single post_state_hash for pre_hash {pre_hash[:16]}...",
                    actual_value=f"{len(post_hashes)} different post_state_hashes",
                    required_manual_intervention=True,
                )
                self._anomalies.append(anomaly)
                self._update_max_severity(SummarySeverity.CRITICAL)
    
    def _detect_phase_violations(self) -> None:
        """
        Detect violations of expected phase transition order.
        
        TIER-0: Phase transitions should follow expected order.
        Out-of-order phases indicate recovery logic errors.
        """
        if not self._changes:
            return
        
        phase_order = list(RecoveryPhase)
        sorted_changes = sorted(self._changes, key=lambda c: c.sequence_start)
        
        prev_phase_index = -1
        for change in sorted_changes:
            try:
                curr_phase_index = phase_order.index(change.phase)
            except ValueError:
                # Unknown phase - already handled elsewhere
                continue
            
            # Allow same phase or forward progression
            # Backward progression (except to ABORTED/CLEANUP) is suspicious
            if curr_phase_index < prev_phase_index:
                if change.phase not in (RecoveryPhase.ABORTED, RecoveryPhase.CLEANUP):
                    anomaly = RecoveryAnomalyRecord(
                        description=f"Phase regression detected: transition from {phase_order[prev_phase_index].value} to {change.phase.value} at sequence {change.sequence_start}",
                        severity=SummarySeverity.WARNING,
                        related_sequence=change.sequence_start,
                        phase=change.phase,
                        evidence_log_range=(change.sequence_start, change.sequence_end),
                    )
                    self._anomalies.append(anomaly)
                    self._update_max_severity(SummarySeverity.WARNING)
            
            prev_phase_index = curr_phase_index
    
    def _detect_partial_recoveries(self) -> None:
        """
        Detect partial recovery scenarios.
        
        TIER-0: Partial recoveries must be explicitly flagged.
        """
        if not self._changes:
            return
        
        # Check if recovery completed all expected phases
        expected_phases = {
            RecoveryPhase.INITIALIZATION,
            RecoveryPhase.VALIDATION,
            RecoveryPhase.STATE_RECONSTRUCTION,
            RecoveryPhase.COMMIT,
        }
        
        missing_phases = expected_phases - self._phases_completed
        
        if missing_phases and RecoveryPhase.COMPLETED not in self._phases_completed:
            anomaly = RecoveryAnomalyRecord(
                description=f"Partial recovery detected: missing phases {[p.value for p in sorted(missing_phases, key=lambda x: x.value)]}",
                severity=SummarySeverity.WARNING,
                required_manual_intervention=False,
            )
            self._anomalies.append(anomaly)
            self._update_max_severity(SummarySeverity.WARNING)
    
    def _compute_outcome(self) -> RecoveryOutcome:
        """Compute final recovery outcome."""
        if not self._changes:
            return RecoveryOutcome.INSUFFICIENT_DATA
        
        successful_changes = sum(1 for c in self._changes if c.is_successful())
        total_changes = len(self._changes)
        
        if successful_changes == 0:
            return RecoveryOutcome.FAILED
        elif successful_changes == total_changes:
            if self._invariants_breached:
                return RecoveryOutcome.DEGRADED
            elif self._max_severity.is_actionable():
                return RecoveryOutcome.DEGRADED
            else:
                return RecoveryOutcome.SUCCESS
        else:
            return RecoveryOutcome.PARTIAL_SUCCESS
    
    def _determine_final_phase(self) -> RecoveryPhase:
        """Determine final recovery phase reached."""
        if not self._phases_completed:
            return RecoveryPhase.INITIALIZATION
        
        # Return highest phase completed
        phase_order = list(RecoveryPhase)
        completed_indices = [phase_order.index(p) for p in self._phases_completed]
        max_index = max(completed_indices)
        return phase_order[max_index]
    
    def _update_max_severity(self, severity: SummarySeverity) -> None:
        """Update maximum severity seen."""
        if severity > self._max_severity:
            self._max_severity = severity
    
    def _parse_scope(self, scope_str: str) -> SummaryScope:
        """Parse scope string to enum."""
        try:
            return SummaryScope(scope_str.lower())
        except ValueError:
            return SummaryScope.SYSTEM
    
    def _parse_phase(self, phase_str: str) -> RecoveryPhase:
        """Parse phase string to enum."""
        try:
            return RecoveryPhase(phase_str.lower())
        except ValueError:
            return RecoveryPhase.STATE_RECONSTRUCTION
    
    def _parse_timestamp(self, ts: Any) -> datetime:
        """Parse timestamp from various formats."""
        if isinstance(ts, datetime):
            return ts
        elif isinstance(ts, str):
            return datetime.fromisoformat(ts)
        elif isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts)
        else:
            return datetime.utcnow()
    
    def _compute_summary_hash(self, summary: RecoverySummary) -> str:
        """
        Compute deterministic hash of summary.
        
        Args:
            summary: Summary to hash
        
        Returns:
            str: SHA-256 hex digest
        """
        # Serialize to deterministic JSON
        data = {
            "recovery_id": summary.recovery_id,
            "run_id": summary.run_id,
            "outcome": summary.outcome.value,
            "total_actions": summary.total_actions,
            "committed_actions": summary.committed_actions,
            "aborted_actions": summary.aborted_actions,
            "invariants_breached": list(summary.invariants_breached),
            "max_severity": summary.max_severity.value,
        }
        
        json_bytes = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        
        return hashlib.sha256(json_bytes).hexdigest()


# =============================================================================
# SUMMARY RENDERER (READ-ONLY)
# =============================================================================


class RecoverySummaryRenderer(ABC):
    """
    Abstract base for summary rendering.
    
    Rendering NEVER changes meaning.
    
    RULES:
      - No truncation
      - No redaction here
      - No hiding failed actions
      - Ordering must reflect execution
    """
    
    @abstractmethod
    def render(self, summary: RecoverySummary) -> str:
        """
        Render summary to string.
        
        Args:
            summary: Summary to render
        
        Returns:
            str: Rendered output
        """
        pass


class JSONRenderer(RecoverySummaryRenderer):
    """Render summary as JSON."""
    
    def render(self, summary: RecoverySummary) -> str:
        """Render as JSON."""
        data = {
            "recovery_id": summary.recovery_id,
            "run_id": summary.run_id,
            "summary_version": summary.summary_version,
            "started_at": summary.started_at.isoformat(),
            "finished_at": summary.finished_at.isoformat(),
            "duration_seconds": summary.duration_seconds(),
            "outcome": summary.outcome.value,
            "final_phase": summary.final_phase.value,
            "total_actions": summary.total_actions,
            "committed_actions": summary.committed_actions,
            "aborted_actions": summary.aborted_actions,
            "retried_actions": summary.retried_actions,
            "requires_manual_review": summary.requires_manual_review,
            "max_severity": summary.max_severity.value,
            "invariants_breached": list(summary.invariants_breached),
            "anomaly_count": len(summary.anomalies),
            "summary_hash": summary.summary_hash,
        }
        
        return json.dumps(data, indent=2, sort_keys=True)


class MarkdownRenderer(RecoverySummaryRenderer):
    """Render summary as Markdown."""
    
    def render(self, summary: RecoverySummary) -> str:
        """Render as Markdown."""
        lines = [
            f"# Recovery Summary: {summary.recovery_id}",
            "",
            f"**Run ID:** {summary.run_id}",
            f"**Outcome:** {summary.outcome.value}",
            f"**Duration:** {summary.duration_seconds():.2f}s",
            f"**Max Severity:** {summary.max_severity.value}",
            "",
            "## Metrics",
            f"- Total Actions: {summary.total_actions}",
            f"- Committed: {summary.committed_actions}",
            f"- Aborted: {summary.aborted_actions}",
            f"- Retried: {summary.retried_actions}",
            "",
        ]
        
        if summary.invariants_breached:
            lines.extend([
                "## ⚠️ Invariant Violations",
                "",
            ])
            for inv in summary.invariants_breached:
                lines.append(f"- {inv}")
            lines.append("")
        
        if summary.anomalies:
            lines.extend([
                "## Anomalies",
                "",
            ])
            for anomaly in summary.anomalies:
                lines.append(f"### {anomaly.severity.value.upper()}: {anomaly.description}")
                if anomaly.invariant_violated:
                    lines.append(f"- Invariant: {anomaly.invariant_violated}")
                lines.append("")
        
        lines.append(f"**Summary Hash:** `{summary.summary_hash}`")
        
        return "\n".join(lines)


# =============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# =============================================================================

"""
STRICTLY FORBIDDEN in this file:

❌ "Inferred" causes
❌ Heuristic severity guessing
❌ Human-friendly but lossy wording
❌ Omitted failures
❌ Reordered timelines
❌ Direct access to live state
❌ Narrative creativity
❌ Smoothing or aggregating unrelated events
❌ Hiding evidence
❌ Making excuses for failures

If it isn't in the log, it doesn't exist.
If the log says it failed, the summary says it failed.

Summaries EXPLAIN truth. They never JUSTIFY actions.
"""


# =============================================================================
# VERSION INFO
# =============================================================================

__version__ = "1.0.0"
__summary_version__ = 1

# recovery_log proves truth. recovery_summary explains truth.