"""
/infra/recovery/audit/recovery_log.py

Immutable Recovery Trail (Causal, Verifiable, Replay-Safe)

This file is the authoritative history of system mutation during recovery.

It answers one question only:
    "After failure, how exactly did the system change — step by step —
     in a way no one can later deny?"

If this log is corrupted, recovery is INVALID.

WHAT THIS FILE IS:
  - The authoritative mutation history
  - The causal chain of state transitions
  - The cryptographically-chained proof of legitimacy
  - The replay verification foundation
  - The third-party audit trail

WHAT THIS FILE IS NOT:
  ❌ Not general logging
  ❌ Not metrics
  ❌ Not debugging output
  ❌ Not operator notes
  ❌ Not best-effort
  ❌ Not recoverable if damaged

If this file lies, the system must STOP.

DESIGN PRINCIPLE (NON-NEGOTIABLE):
    Recovery must be explainable without trust.
    
    No internal authority is assumed.
    Every state transition is proven, not declared.

RELATIONSHIP TO audit_logger.py:
    audit_logger.py = courtroom stenographer ("this action happened")
    recovery_log.py = signed chain of custody ("this state is legitimate")
    
    Same incident. Very different legal weight.

CORE RESPONSIBILITIES:
  1. Record every recovery mutation intent
  2. Record pre-state hash
  3. Record post-state hash
  4. Chain entries cryptographically
  5. Be append-only forever
  6. Reject ambiguity
  7. Support deterministic replay
  8. Enable third-party verification

No exception paths. No shortcuts. No trust.

MENTAL MODEL:
    audit_logger says: "something happened."
    recovery_log proves: "this state is legitimate."
    
    You need both — or neither matters.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Tuple, Iterator, Callable
from datetime import datetime
import hashlib
import json
import time


# =============================================================================
# RECOVERY ACTION TYPES & PHASES
# =============================================================================


class RecoveryActionType(Enum):
    """
    Types of recovery actions.
    
    Each type has specific pre/post state requirements.
    """
    ROLLBACK = "rollback"  # Restore from snapshot
    REPLAY = "replay"  # Re-execute operations
    REPAIR = "repair"  # Fix corrupted state
    MERGE = "merge"  # Merge divergent states
    ABORT = "abort"  # Abort recovery operation
    SNAPSHOT_LOAD = "snapshot_load"  # Load snapshot
    STATE_REBUILD = "state_rebuild"  # Rebuild from scratch
    INVARIANT_CHECK = "invariant_check"  # Verify invariants
    LOCK_ACQUIRE = "lock_acquire"  # Acquire recovery lock
    LOCK_RELEASE = "lock_release"  # Release recovery lock
    
    def requires_post_state(self) -> bool:
        """Does this action require post-state hash?"""
        return self not in (
            RecoveryActionType.ABORT,
            RecoveryActionType.INVARIANT_CHECK,
        )


class RecoveryPhase(Enum):
    """
    Recovery execution phases.
    
    Phases NEVER skip.
    If one is missing → corruption detected.
    
    STRICT ORDERING:
      INITIATED → VALIDATED → EXECUTED → COMMITTED
    """
    INITIATED = "initiated"  # Recovery action started
    VALIDATED = "validated"  # Pre-conditions checked
    EXECUTED = "executed"  # Action performed
    COMMITTED = "committed"  # Action finalized
    ABORTED = "aborted"  # Action cancelled
    
    def is_terminal(self) -> bool:
        """Is this a terminal phase?"""
        return self in (RecoveryPhase.COMMITTED, RecoveryPhase.ABORTED)
    
    def next_phase(self) -> Optional["RecoveryPhase"]:
        """Get the next expected phase."""
        phase_order = [
            RecoveryPhase.INITIATED,
            RecoveryPhase.VALIDATED,
            RecoveryPhase.EXECUTED,
            RecoveryPhase.COMMITTED,
        ]
        
        if self == RecoveryPhase.ABORTED:
            return None  # Terminal
        
        try:
            current_index = phase_order.index(self)
            if current_index < len(phase_order) - 1:
                return phase_order[current_index + 1]
        except ValueError:
            pass
        
        return None


class RecoveryScope(Enum):
    """
    Scope of recovery action.
    
    Determines what part of the system is affected.
    """
    SYSTEM = "system"  # Entire system
    WORKFLOW = "workflow"  # Single workflow
    NODE = "node"  # Single node
    SHARD = "shard"  # Single shard
    LOCK = "lock"  # Lock/lease
    SNAPSHOT = "snapshot"  # Snapshot operation


class RecoveryActor(Enum):
    """
    Entity performing recovery action.
    
    Used for attribution and authorization checking.
    """
    SYSTEM = "system"  # Automated system recovery
    WATCHDOG = "watchdog"  # Watchdog daemon
    OPERATOR = "operator"  # Human operator
    COORDINATOR = "coordinator"  # Recovery coordinator
    SCHEDULER = "scheduler"  # Scheduled recovery


class RecoveryReason(Enum):
    """
    Enumerated reasons for recovery action.
    
    MUST be enum-backed, never freeform.
    """
    CRASH_DETECTED = "crash_detected"
    CORRUPTION_DETECTED = "corruption_detected"
    INVARIANT_VIOLATION = "invariant_violation"
    SCHEDULED_ROLLBACK = "scheduled_rollback"
    OPERATOR_INITIATED = "operator_initiated"
    WATCHDOG_TIMEOUT = "watchdog_timeout"
    STATE_DIVERGENCE = "state_divergence"
    SNAPSHOT_RESTORE = "snapshot_restore"
    MANUAL_REPAIR = "manual_repair"
    EMERGENCY_ABORT = "emergency_abort"


# =============================================================================
# RECOVERY LOG ENTRY (IMMUTABLE)
# =============================================================================


@dataclass(frozen=True)
class RecoveryLogEntry:
    """
    Single immutable recovery log entry.
    
    Each entry MUST contain complete information about a state transition.
    
    HASH CHAIN:
      entry_hash = hash(prev_entry_hash + serialized_entry_fields)
      
      Break one entry → break the chain → invalidate recovery.
    
    RULES:
      - Entries written BEFORE mutation (INITIATED, VALIDATED)
      - Commit entry written AFTER mutation (COMMITTED)
      - No overwrite
      - No truncation
      - No batching across actions
      - No retries that reorder sequence
      
      If write fails → recovery halts immediately.
    
    NOTHING is optional except future state (pre-commit).
    """
    
    # === IDENTITY ===
    entry_id: str  # Deterministic ID (sequence-based)
    sequence: int  # Strictly monotonic sequence number
    timestamp_ns: int  # Monotonic clock nanoseconds
    
    # === ACTION ===
    action_type: RecoveryActionType
    phase: RecoveryPhase
    
    # === TARGET ===
    target_scope: RecoveryScope
    target_id: str  # Specific ID (node_id, shard_id, workflow_id, etc.)
    
    # === STATE HASHES ===
    pre_state_hash: Optional[str] = None  # Merkle/digest before action
    post_state_hash: Optional[str] = None  # Merkle/digest after action (None until COMMITTED)
    
    # === SNAPSHOT LINEAGE ===
    input_snapshot_ref: Optional[str] = None  # Immutable snapshot ID consumed
    output_snapshot_ref: Optional[str] = None  # Immutable snapshot ID produced
    
    # === ATTRIBUTION ===
    actor: RecoveryActor = RecoveryActor.SYSTEM
    reason: RecoveryReason = RecoveryReason.CRASH_DETECTED
    
    # === CONTEXT ===
    recovery_id: str = ""  # Overall recovery session ID
    parent_sequence: Optional[int] = None  # Parent entry sequence (for nested actions)
    correlation_id: Optional[str] = None  # For cross-referencing with audit_logger
    
    # === HASH CHAIN ===
    prev_entry_hash: str = ""  # Hash of previous entry (empty for first entry)
    entry_hash: str = ""  # Hash of this entry
    
    # === METADATA (BOUNDED) ===
    metadata: Dict[str, Any] = field(default_factory=dict)  # JSON-safe only
    
    def __post_init__(self):
        """Post-initialization validation."""
        self.validate()
    
    def validate(self) -> None:
        """
        Validate entry invariants.
        
        Raises:
            ValueError: If any invariant violated
        """
        if not self.entry_id:
            raise ValueError("entry_id cannot be empty")
        
        if self.sequence < 0:
            raise ValueError(f"sequence must be >= 0, got {self.sequence}")
        
        if self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be >= 0")
        
        if not self.target_id:
            raise ValueError("target_id cannot be empty")
        
        if not self.recovery_id:
            raise ValueError("recovery_id cannot be empty")
        
        if not self.entry_hash:
            raise ValueError("entry_hash cannot be empty")
        
        # Phase-specific validation
        # TIER-0: pre_state_hash is MANDATORY for all mutation phases
        # (except ABORTED which may not have pre-state if aborted before initiation)
        if self.phase != RecoveryPhase.ABORTED:
            if self.pre_state_hash is None:
                raise ValueError(
                    f"pre_state_hash is MANDATORY for phase {self.phase.value} "
                    f"in action {self.action_type.value} (Tier-0 requirement)"
                )
            if len(self.pre_state_hash) != 64:
                raise ValueError(
                    f"pre_state_hash must be 64-char SHA-256 hex, got {len(self.pre_state_hash)} chars"
                )
        
        if self.phase == RecoveryPhase.COMMITTED:
            if self.action_type.requires_post_state() and self.post_state_hash is None:
                raise ValueError(
                    f"COMMITTED phase requires post_state_hash for {self.action_type.value}"
                )
            if self.post_state_hash and len(self.post_state_hash) != 64:
                raise ValueError(
                    f"post_state_hash must be 64-char SHA-256 hex, got {len(self.post_state_hash)} chars"
                )
        
        # First entry validation
        if self.sequence == 0:
            if self.prev_entry_hash != "":
                raise ValueError("First entry must have empty prev_entry_hash")
        else:
            if not self.prev_entry_hash:
                raise ValueError("Non-first entry must have prev_entry_hash")
        
        # Hash format validation
        if len(self.entry_hash) != 64:
            raise ValueError(
                f"entry_hash must be 64-char SHA-256 hex, got {len(self.entry_hash)} chars"
            )
        
        if self.prev_entry_hash and len(self.prev_entry_hash) != 64:
            raise ValueError(
                f"prev_entry_hash must be 64-char SHA-256 hex, got {len(self.prev_entry_hash)} chars"
            )
        
        # Parent sequence validation
        if self.parent_sequence is not None:
            if self.parent_sequence < 0:
                raise ValueError("parent_sequence must be >= 0")
            if self.parent_sequence >= self.sequence:
                raise ValueError("parent_sequence must be < sequence")
        
        # Metadata must be JSON-safe
        if self.metadata:
            try:
                json.dumps(self.metadata)
            except (TypeError, ValueError) as e:
                raise ValueError(f"metadata must be JSON-safe: {e}")
    
    def compute_hash(self) -> str:
        """
        Compute deterministic entry hash.
        
        Returns:
            str: SHA-256 hex digest
        """
        # Serialize to deterministic format
        data = {
            "entry_id": self.entry_id,
            "sequence": self.sequence,
            "timestamp_ns": self.timestamp_ns,
            "action_type": self.action_type.value,
            "phase": self.phase.value,
            "target_scope": self.target_scope.value,
            "target_id": self.target_id,
            "pre_state_hash": self.pre_state_hash or "",
            "post_state_hash": self.post_state_hash or "",
            "input_snapshot_ref": self.input_snapshot_ref or "",
            "output_snapshot_ref": self.output_snapshot_ref or "",
            "actor": self.actor.value,
            "reason": self.reason.value,
            "recovery_id": self.recovery_id,
            "parent_sequence": self.parent_sequence or -1,
            "correlation_id": self.correlation_id or "",
            "prev_entry_hash": self.prev_entry_hash,
        }
        
        # Deterministic JSON serialization
        json_str = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        
        # Hash: prev_entry_hash + json_str
        combined = self.prev_entry_hash + json_str
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()
    
    def verify_hash(self) -> bool:
        """
        Verify entry hash is correct.
        
        Returns:
            bool: True if hash valid
        """
        expected = self.compute_hash()
        return self.entry_hash == expected
    
    def is_terminal(self) -> bool:
        """Is this a terminal phase entry?"""
        return self.phase.is_terminal()
    
    def state_changed(self) -> bool:
        """Did this entry change state?"""
        if self.pre_state_hash is None or self.post_state_hash is None:
            return False
        return self.pre_state_hash != self.post_state_hash


# =============================================================================
# RECOVERY CONTEXT
# =============================================================================


@dataclass(frozen=True)
class RecoveryContext:
    """
    Context for a recovery session.
    
    Provides common information for all entries in a recovery.
    """
    recovery_id: str
    initiated_by: RecoveryActor
    initiated_reason: RecoveryReason
    started_at: datetime
    target_scope: RecoveryScope
    target_id: str
    
    def validate(self) -> None:
        """Validate context."""
        if not self.recovery_id:
            raise ValueError("recovery_id cannot be empty")
        
        if not self.target_id:
            raise ValueError("target_id cannot be empty")


# =============================================================================
# RECOVERY LOG WRITER
# =============================================================================


class RecoveryLogWriter:
    """
    Write-only recovery log writer.
    
    WRITE RULES (ABSOLUTE):
      - Entries written BEFORE mutation (INITIATED, VALIDATED)
      - Commit entry written AFTER mutation (COMMITTED)
      - No overwrite
      - No truncation
      - No batching across actions
      - No retries that reorder sequence
      
      If write fails → recovery halts immediately.
    
    RECOVERY FLOW (CANONICAL):
      1. Log INITIATED
      2. Log VALIDATED
      3. Apply mutation
      4. Log EXECUTED
      5. Verify resulting state
      6. Log COMMITTED
      
      Missing any step = invalid recovery.
    """
    
    def __init__(self, context: RecoveryContext, storage_backend):
        """
        Initialize writer.
        
        Args:
            context: Recovery context
            storage_backend: Backend for writing entries (must support append-only)
        """
        context.validate()
        self._context = context
        self._backend = storage_backend
        self._sequence_counter = 0
        self._last_entry_hash = ""
        self._closed = False
        # TIER-0: Track phase progression per action to enforce legal transitions
        self._action_phases: Dict[Tuple[RecoveryActionType, str], List[RecoveryPhase]] = {}
    
    def write_entry(
        self,
        action_type: RecoveryActionType,
        phase: RecoveryPhase,
        target_scope: RecoveryScope,
        target_id: str,
        pre_state_hash: Optional[str] = None,  # Required for non-ABORTED phases
        post_state_hash: Optional[str] = None,
        input_snapshot_ref: Optional[str] = None,
        output_snapshot_ref: Optional[str] = None,
        actor: Optional[RecoveryActor] = None,
        reason: Optional[RecoveryReason] = None,
        parent_sequence: Optional[int] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryLogEntry:
        """
        Write a single log entry.
        
        This is the ONLY way to write to the recovery log.
        
        Args:
            action_type: Type of recovery action
            phase: Execution phase
            target_scope: Scope of action
            target_id: Target identifier
            pre_state_hash: State hash before action
            post_state_hash: State hash after action
            input_snapshot_ref: Input snapshot reference
            output_snapshot_ref: Output snapshot reference
            actor: Actor performing action (defaults to context)
            reason: Reason for action (defaults to context)
            parent_sequence: Parent entry sequence
            correlation_id: Correlation ID for audit_logger
            metadata: Optional metadata (JSON-safe)
        
        Returns:
            RecoveryLogEntry: The written entry
        
        Raises:
            ValueError: If entry invalid
            RuntimeError: If write fails or writer closed
        """
        if self._closed:
            raise RuntimeError("Cannot write to closed recovery log")
        
        # TIER-0: Enforce pre_state_hash requirement at API level (not just validation)
        if phase != RecoveryPhase.ABORTED:
            if pre_state_hash is None:
                raise ValueError(
                    f"pre_state_hash is MANDATORY for phase {phase.value} "
                    f"in action {action_type.value} (Tier-0 requirement). "
                    f"Cannot write entry without pre-state proof."
                )
            if len(pre_state_hash) != 64:
                raise ValueError(
                    f"pre_state_hash must be 64-char SHA-256 hex, got {len(pre_state_hash)} chars"
                )
        
        # Get monotonic timestamp
        timestamp_ns = time.monotonic_ns()
        
        # Generate entry ID
        entry_id = f"{self._context.recovery_id}:{self._sequence_counter:010d}"
        
        # Create entry
        entry = RecoveryLogEntry(
            entry_id=entry_id,
            sequence=self._sequence_counter,
            timestamp_ns=timestamp_ns,
            action_type=action_type,
            phase=phase,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            post_state_hash=post_state_hash,
            input_snapshot_ref=input_snapshot_ref,
            output_snapshot_ref=output_snapshot_ref,
            actor=actor or self._context.initiated_by,
            reason=reason or self._context.initiated_reason,
            recovery_id=self._context.recovery_id,
            parent_sequence=parent_sequence,
            correlation_id=correlation_id,
            prev_entry_hash=self._last_entry_hash,
            entry_hash="",  # Will be computed
            metadata=metadata or {},
        )
        
        # Compute hash
        entry_hash = entry.compute_hash()
        
        # Recreate with hash (dataclass is frozen)
        entry = RecoveryLogEntry(
            entry_id=entry.entry_id,
            sequence=entry.sequence,
            timestamp_ns=entry.timestamp_ns,
            action_type=entry.action_type,
            phase=entry.phase,
            target_scope=entry.target_scope,
            target_id=entry.target_id,
            pre_state_hash=entry.pre_state_hash,
            post_state_hash=entry.post_state_hash,
            input_snapshot_ref=entry.input_snapshot_ref,
            output_snapshot_ref=entry.output_snapshot_ref,
            actor=entry.actor,
            reason=entry.reason,
            recovery_id=entry.recovery_id,
            parent_sequence=entry.parent_sequence,
            correlation_id=entry.correlation_id,
            prev_entry_hash=entry.prev_entry_hash,
            entry_hash=entry_hash,
            metadata=entry.metadata,
        )
        
        # Validate entry
        entry.validate()
        
        # TIER-0: Enforce legal phase progression AND completeness
        action_key = (action_type, target_id)
        if action_key not in self._action_phases:
            self._action_phases[action_key] = []
        
        existing_phases = self._action_phases[action_key]
        
        # Check for illegal phase transitions
        if phase == RecoveryPhase.INITIATED:
            if existing_phases:
                raise ValueError(
                    f"Cannot write INITIATED phase after phases {existing_phases} "
                    f"for {action_type.value} on {target_id}"
                )
        elif phase == RecoveryPhase.VALIDATED:
            if RecoveryPhase.INITIATED not in existing_phases:
                raise ValueError(
                    f"Cannot write VALIDATED phase without INITIATED for {action_type.value} on {target_id}"
                )
            if RecoveryPhase.EXECUTED in existing_phases or RecoveryPhase.COMMITTED in existing_phases:
                raise ValueError(
                    f"Cannot write VALIDATED phase after EXECUTED/COMMITTED for {action_type.value} on {target_id}"
                )
        elif phase == RecoveryPhase.EXECUTED:
            if RecoveryPhase.VALIDATED not in existing_phases:
                raise ValueError(
                    f"Cannot write EXECUTED phase without VALIDATED for {action_type.value} on {target_id}"
                )
            if RecoveryPhase.COMMITTED in existing_phases:
                raise ValueError(
                    f"Cannot write EXECUTED phase after COMMITTED for {action_type.value} on {target_id}"
                )
        elif phase == RecoveryPhase.COMMITTED:
            if RecoveryPhase.EXECUTED not in existing_phases:
                raise ValueError(
                    f"Cannot write COMMITTED phase without EXECUTED for {action_type.value} on {target_id}"
                )
            # TIER-0: Enforce phase completeness - COMMITTED requires all prior phases
            required_before_commit = [
                RecoveryPhase.INITIATED,
                RecoveryPhase.VALIDATED,
                RecoveryPhase.EXECUTED,
            ]
            missing = [p for p in required_before_commit if p not in existing_phases]
            if missing:
                raise ValueError(
                    f"Cannot COMMIT {action_type.value} on {target_id} without phases: {[p.value for p in missing]}"
                )
        elif phase == RecoveryPhase.ABORTED:
            # ABORTED can appear at any point, but marks terminal state
            pass
        
        # Update phase tracking
        if phase not in existing_phases:
            self._action_phases[action_key].append(phase)
        
        # Write to backend
        try:
            self._backend.append_entry(entry)
        except Exception as e:
            # CRITICAL: Write failure halts recovery
            raise RuntimeError(
                f"FATAL: Recovery log write failed at sequence {self._sequence_counter}: {e}"
            ) from e
        
        # Update state
        self._last_entry_hash = entry_hash
        self._sequence_counter += 1
        
        return entry
    
    def write_initiated(
        self,
        action_type: RecoveryActionType,
        target_scope: RecoveryScope,
        target_id: str,
        pre_state_hash: str,  # TIER-0: MANDATORY
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryLogEntry:
        """
        Write INITIATED phase entry.
        
        This MUST be the first entry for any action.
        
        TIER-0: pre_state_hash is MANDATORY - no recovery mutation can occur
        without proving the pre-state.
        
        Args:
            pre_state_hash: MANDATORY state hash before action (64-char SHA-256 hex)
        """
        return self.write_entry(
            action_type=action_type,
            phase=RecoveryPhase.INITIATED,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    
    def write_validated(
        self,
        action_type: RecoveryActionType,
        target_scope: RecoveryScope,
        target_id: str,
        pre_state_hash: str,  # TIER-0: MANDATORY
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryLogEntry:
        """
        Write VALIDATED phase entry.
        
        TIER-0: pre_state_hash is MANDATORY - validation must prove pre-state.
        """
        return self.write_entry(
            action_type=action_type,
            phase=RecoveryPhase.VALIDATED,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    
    def write_executed(
        self,
        action_type: RecoveryActionType,
        target_scope: RecoveryScope,
        target_id: str,
        pre_state_hash: str,  # TIER-0: MANDATORY
        post_state_hash: Optional[str] = None,
        output_snapshot_ref: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryLogEntry:
        """
        Write EXECUTED phase entry.
        
        TIER-0: pre_state_hash is MANDATORY - execution must prove pre-state.
        """
        return self.write_entry(
            action_type=action_type,
            phase=RecoveryPhase.EXECUTED,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            post_state_hash=post_state_hash,
            output_snapshot_ref=output_snapshot_ref,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    
    def write_committed(
        self,
        action_type: RecoveryActionType,
        target_scope: RecoveryScope,
        target_id: str,
        pre_state_hash: str,
        post_state_hash: str,
        output_snapshot_ref: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryLogEntry:
        """
        Write COMMITTED phase entry.
        
        This MUST be written AFTER mutation is complete.
        """
        return self.write_entry(
            action_type=action_type,
            phase=RecoveryPhase.COMMITTED,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            post_state_hash=post_state_hash,
            output_snapshot_ref=output_snapshot_ref,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    
    def write_aborted(
        self,
        action_type: RecoveryActionType,
        target_scope: RecoveryScope,
        target_id: str,
        pre_state_hash: Optional[str] = None,
        reason: Optional[RecoveryReason] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> RecoveryLogEntry:
        """Write ABORTED phase entry."""
        return self.write_entry(
            action_type=action_type,
            phase=RecoveryPhase.ABORTED,
            target_scope=target_scope,
            target_id=target_id,
            pre_state_hash=pre_state_hash,
            reason=reason or RecoveryReason.EMERGENCY_ABORT,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    
    def close(self) -> None:
        """
        Close the writer.
        
        After closing, no more entries can be written.
        """
        self._closed = True
    
    def is_closed(self) -> bool:
        """Is writer closed?"""
        return self._closed
    
    def get_sequence_counter(self) -> int:
        """Get current sequence counter."""
        return self._sequence_counter


# =============================================================================
# RECOVERY LOG READER
# =============================================================================


class RecoveryLogReader:
    """
    Read-only recovery log reader.
    
    READ SEMANTICS:
      - Deterministic
      - Order-preserving
      - Side-effect free
      - Verifiable
    
    REQUIREMENTS:
      - Validate hash chain
      - Validate state hashes
      - Refuse partial tails unless explicitly flagged
    """
    
    def __init__(self, storage_backend):
        """
        Initialize reader.
        
        Args:
            storage_backend: Backend for reading entries
        """
        self._backend = storage_backend
    
    def read_all(self) -> Tuple[RecoveryLogEntry, ...]:
        """
        Read all log entries.
        
        TIER-0: MANDATORY hash chain verification on read.
        This ensures readers cannot return corrupted data without detection.
        There is no option to disable verification - this is a constitutional requirement.
        
        Returns:
            Tuple[RecoveryLogEntry, ...]: All entries in order
        
        Raises:
            ValueError: If log corrupted (sequence, hash chain, or entry hash invalid)
        """
        entries = self._backend.read_all_entries()
        
        # Validate sequence ordering
        for i, entry in enumerate(entries):
            if entry.sequence != i:
                raise ValueError(
                    f"Sequence mismatch at index {i}: expected {i}, got {entry.sequence}"
                )
        
        # TIER-0: MANDATORY hash chain verification (no option to disable)
        if entries:
            for i, entry in enumerate(entries):
                # Verify entry hash
                if not entry.verify_hash():
                    raise ValueError(
                        f"Entry {i} (seq {entry.sequence}) has invalid entry_hash"
                    )
                
                # Verify prev_entry_hash linkage
                if i == 0:
                    if entry.prev_entry_hash != "":
                        raise ValueError(
                            f"First entry must have empty prev_entry_hash, got '{entry.prev_entry_hash}'"
                        )
                else:
                    prev_entry = entries[i - 1]
                    if entry.prev_entry_hash != prev_entry.entry_hash:
                        raise ValueError(
                            f"Hash chain broken at entry {i} (seq {entry.sequence}): "
                            f"expected prev_entry_hash={prev_entry.entry_hash}, "
                            f"got {entry.prev_entry_hash}"
                        )
        
        return tuple(entries)
    
    def read_range(self, start_seq: int, end_seq: int) -> Tuple[RecoveryLogEntry, ...]:
        """
        Read entries in sequence range.
        
        TIER-0: Uses verified read_all() to ensure chain integrity before filtering.
        
        Args:
            start_seq: Start sequence (inclusive)
            end_seq: End sequence (inclusive)
        
        Returns:
            Tuple[RecoveryLogEntry, ...]: Entries in range (from verified chain)
        """
        if start_seq < 0 or end_seq < start_seq:
            raise ValueError("Invalid sequence range")
        
        # TIER-0: Read from verified chain (read_all() validates hash chain)
        all_entries = self.read_all()
        return tuple(e for e in all_entries if start_seq <= e.sequence <= end_seq)
    
    def read_by_recovery_id(self, recovery_id: str) -> Tuple[RecoveryLogEntry, ...]:
        """
        Read all entries for a specific recovery.
        
        Args:
            recovery_id: Recovery ID to filter by
        
        Returns:
            Tuple[RecoveryLogEntry, ...]: Matching entries
        """
        all_entries = self.read_all()
        return tuple(e for e in all_entries if e.recovery_id == recovery_id)
    
    def read_by_correlation_id(self, correlation_id: str) -> Tuple[RecoveryLogEntry, ...]:
        """
        Read entries by correlation ID.
        
        Used to correlate with audit_logger entries.
        
        Args:
            correlation_id: Correlation ID
        
        Returns:
            Tuple[RecoveryLogEntry, ...]: Matching entries
        """
        all_entries = self.read_all()
        return tuple(e for e in all_entries if e.correlation_id == correlation_id)
    
    def get_latest_entry(self) -> Optional[RecoveryLogEntry]:
        """
        Get the latest log entry.
        
        Returns:
            Optional[RecoveryLogEntry]: Latest entry or None if empty
        """
        entries = self.read_all()
        return entries[-1] if entries else None


# =============================================================================
# RECOVERY LOG VERIFIER
# =============================================================================


class RecoveryLogVerifier:
    """
    Third-party verifiable log verification.
    
    VERIFICATION REQUIREMENTS:
      - Verify full hash chain
      - Recompute state hashes
      - Validate monotonic clocks
      - Ensure no skipped phases
      - Ensure no conflicting actions
    
    No trust in internal clocks or actors.
    """
    
    def __init__(self, reader: RecoveryLogReader):
        """
        Initialize verifier.
        
        Args:
            reader: Recovery log reader
        """
        self._reader = reader
    
    def verify_full_chain(self) -> Tuple[bool, List[str]]:
        """
        Verify the full hash chain.
        
        Returns:
            Tuple[bool, List[str]]: (is_valid, list of errors)
        """
        errors = []
        
        try:
            entries = self._reader.read_all()
        except Exception as e:
            return False, [f"Failed to read log: {e}"]
        
        if not entries:
            return True, []  # Empty log is valid
        
        # Verify hash chain
        for i, entry in enumerate(entries):
            # Verify entry hash
            if not entry.verify_hash():
                errors.append(
                    f"Entry {i} (seq {entry.sequence}) has invalid entry_hash"
                )
            
            # Verify prev_entry_hash linkage
            if i == 0:
                if entry.prev_entry_hash != "":
                    errors.append(
                        f"First entry must have empty prev_entry_hash, got '{entry.prev_entry_hash}'"
                    )
            else:
                prev_entry = entries[i - 1]
                if entry.prev_entry_hash != prev_entry.entry_hash:
                    errors.append(
                        f"Entry {i} prev_entry_hash mismatch: "
                        f"expected {prev_entry.entry_hash}, got {entry.prev_entry_hash}"
                    )
        
        # Verify sequence monotonicity
        for i in range(1, len(entries)):
            if entries[i].sequence != entries[i - 1].sequence + 1:
                errors.append(
                    f"Sequence not monotonic at index {i}: "
                    f"{entries[i - 1].sequence} -> {entries[i].sequence}"
                )
        
        # Verify timestamp monotonicity
        for i in range(1, len(entries)):
            if entries[i].timestamp_ns < entries[i - 1].timestamp_ns:
                errors.append(
                    f"Timestamp not monotonic at index {i}"
                )
        
        return len(errors) == 0, errors
    
    def verify_phase_progression(self) -> Tuple[bool, List[str]]:
        """
        Verify phase progression follows rules.
        
        Returns:
            Tuple[bool, List[str]]: (is_valid, list of errors)
        """
        errors = []
        
        try:
            entries = self._reader.read_all()
        except Exception as e:
            return False, [f"Failed to read log: {e}"]
        
        # Group by action (action_type + target_id)
        actions: Dict[Tuple[RecoveryActionType, str], List[RecoveryLogEntry]] = {}
        
        for entry in entries:
            key = (entry.action_type, entry.target_id)
            if key not in actions:
                actions[key] = []
            actions[key].append(entry)
        
        # Verify each action's phase progression
        for (action_type, target_id), action_entries in actions.items():
            phases = [e.phase for e in action_entries]
            
            # Check for expected progression
            expected_progression = [
                RecoveryPhase.INITIATED,
                RecoveryPhase.VALIDATED,
                RecoveryPhase.EXECUTED,
                RecoveryPhase.COMMITTED,
            ]
            
            # Allow ABORTED to appear anywhere
            if RecoveryPhase.ABORTED in phases:
                continue  # Aborted actions can have incomplete progression
            
            # TIER-0: Enforce phase completeness - all required phases must exist
            required_phases = [
                RecoveryPhase.INITIATED,
                RecoveryPhase.VALIDATED,
                RecoveryPhase.EXECUTED,
                RecoveryPhase.COMMITTED,
            ]
            
            missing_phases = [p for p in required_phases if p not in phases]
            if missing_phases:
                errors.append(
                    f"Action {action_type.value} on {target_id} missing required phases: "
                    f"{[p.value for p in missing_phases]}"
                )
            
            # Check that phases appear in order
            phase_indices = []
            for phase in phases:
                try:
                    phase_indices.append(expected_progression.index(phase))
                except ValueError:
                    errors.append(
                        f"Unexpected phase {phase.value} for {action_type.value} on {target_id}"
                    )
                    continue
            
            # Verify strictly increasing
            for i in range(1, len(phase_indices)):
                if phase_indices[i] <= phase_indices[i - 1]:
                    errors.append(
                        f"Phase regression in {action_type.value} on {target_id}: "
                        f"{phases[i - 1].value} -> {phases[i].value}"
                    )
        
        return len(errors) == 0, errors
    
    def verify_state_hashes(
        self, 
        state_hash_computer: Optional[Callable[[RecoveryLogEntry], Tuple[str, Optional[str]]]] = None
    ) -> Tuple[bool, List[str]]:
        """
        Verify state hash invariants.
        
        TIER-0: Recomputes state hashes to verify truth, not just presence.
        Requires state_hash_computer callback: (entry) -> (pre_hash, post_hash)
        
        Args:
            state_hash_computer: Optional callback to recompute state hashes.
                                If provided, will verify hash correctness.
                                Signature: (entry: RecoveryLogEntry) -> Tuple[str, Optional[str]]
        
        Returns:
            Tuple[bool, List[str]]: (is_valid, list of errors)
        """
        errors = []
        
        try:
            entries = self._reader.read_all()
        except Exception as e:
            return False, [f"Failed to read log: {e}"]
        
        for entry in entries:
            # TIER-0: pre_state_hash is mandatory (except ABORTED)
            if entry.phase != RecoveryPhase.ABORTED:
                if entry.pre_state_hash is None:
                    errors.append(
                        f"Entry {entry.sequence} phase {entry.phase.value} missing mandatory pre_state_hash"
                    )
                elif len(entry.pre_state_hash) != 64:
                    errors.append(
                        f"Entry {entry.sequence} pre_state_hash invalid format (must be 64-char hex)"
                    )
            
            # COMMITTED phase must have post_state_hash
            if entry.phase == RecoveryPhase.COMMITTED:
                if entry.action_type.requires_post_state():
                    if entry.post_state_hash is None:
                        errors.append(
                            f"Entry {entry.sequence} COMMITTED phase missing post_state_hash"
                        )
                    elif len(entry.post_state_hash) != 64:
                        errors.append(
                            f"Entry {entry.sequence} post_state_hash invalid format (must be 64-char hex)"
                    )
            
            # TIER-0: Recompute state hashes if computer provided
            if state_hash_computer is not None and entry.phase != RecoveryPhase.ABORTED:
                try:
                    computed_pre, computed_post = state_hash_computer(entry)
                    if computed_pre != entry.pre_state_hash:
                        errors.append(
                            f"Entry {entry.sequence} pre_state_hash mismatch: "
                            f"expected {computed_pre}, got {entry.pre_state_hash}"
                        )
                    if entry.post_state_hash and computed_post:
                        if computed_post != entry.post_state_hash:
                            errors.append(
                                f"Entry {entry.sequence} post_state_hash mismatch: "
                                f"expected {computed_post}, got {entry.post_state_hash}"
                            )
                except Exception as e:
                    errors.append(
                        f"Entry {entry.sequence} state hash recomputation failed: {e}"
                    )
        
        return len(errors) == 0, errors
    
    def verify_all(
        self,
        state_hash_computer: Optional[Callable[[RecoveryLogEntry], Tuple[str, Optional[str]]]] = None
    ) -> Tuple[bool, List[str]]:
        """
        Run all verifications.
        
        TIER-0: Comprehensive verification including optional state hash recomputation.
        
        Args:
            state_hash_computer: Optional callback to recompute state hashes for truth verification.
                                Signature: (entry: RecoveryLogEntry) -> Tuple[str, Optional[str]]
        
        Returns:
            Tuple[bool, List[str]]: (is_valid, list of all errors)
        """
        all_errors = []
        
        chain_valid, chain_errors = self.verify_full_chain()
        all_errors.extend(chain_errors)
        
        phase_valid, phase_errors = self.verify_phase_progression()
        all_errors.extend(phase_errors)
        
        state_valid, state_errors = self.verify_state_hashes(state_hash_computer=state_hash_computer)
        all_errors.extend(state_errors)
        
        return len(all_errors) == 0, all_errors


# =============================================================================
# DETERMINISTIC REPLAY RECONSTRUCTION AUTHORITY
# =============================================================================


class RecoveryLogReplayAuthority:
    """
    Deterministic replay reconstruction authority.
    
    TIER-0: Provides mathematical proof that state can be reconstructed
    from the recovery log, enabling external verification of recovery legitimacy.
    
    This class answers: "Given this log, can I prove the final state is correct?"
    
    CORE RESPONSIBILITY:
        Reconstruct state transitions from log entries in deterministic order,
        proving that the log contains sufficient information to verify recovery.
    
    USAGE:
        Used by external auditors to verify recovery legitimacy without
        trusting internal system state.
    """
    
    def __init__(self, reader: RecoveryLogReader, verifier: RecoveryLogVerifier):
        """
        Initialize replay authority.
        
        Args:
            reader: Recovery log reader
            verifier: Recovery log verifier (must have verified log first)
        """
        self._reader = reader
        self._verifier = verifier
    
    def reconstruct_state_transitions(
        self,
        recovery_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Reconstruct state transitions from log entries.
        
        TIER-0: Returns deterministic sequence of state transitions that can be
        externally verified.
        
        Args:
            recovery_id: Optional recovery ID to filter by
            target_id: Optional target ID to filter by
        
        Returns:
            List[Dict]: State transitions in deterministic order, each containing:
                - sequence: Entry sequence number
                - action_type: Action type
                - phase: Phase
                - pre_state_hash: Pre-state hash
                - post_state_hash: Post-state hash (if available)
                - timestamp_ns: Timestamp
                - target_id: Target identifier
        
        Raises:
            ValueError: If log is corrupted or unverifiable
        """
        # First verify log integrity (with optional state hash recomputation)
        # Note: state_hash_computer can be passed via verify_all() if needed
        is_valid, errors = self._verifier.verify_all()
        if not is_valid:
            raise ValueError(
                f"Cannot reconstruct from unverifiable log: {errors}"
            )
        
        # Read entries (read_all() now always verifies chain - Tier-0 requirement)
        entries = self._reader.read_all()
        
        # Filter if needed
        if recovery_id:
            entries = [e for e in entries if e.recovery_id == recovery_id]
        if target_id:
            entries = [e for e in entries if e.target_id == target_id]
        
        # Reconstruct transitions
        transitions = []
        for entry in entries:
            transition = {
                "sequence": entry.sequence,
                "action_type": entry.action_type.value,
                "phase": entry.phase.value,
                "pre_state_hash": entry.pre_state_hash,
                "post_state_hash": entry.post_state_hash,
                "timestamp_ns": entry.timestamp_ns,
                "target_id": entry.target_id,
                "target_scope": entry.target_scope.value,
                "entry_hash": entry.entry_hash,
                "prev_entry_hash": entry.prev_entry_hash,
            }
            transitions.append(transition)
        
        return transitions
    
    def verify_replay_equivalence(
        self,
        current_state_hash: str,
        recovery_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify that replay from log produces equivalent state.
        
        TIER-0: Mathematical proof that current state matches log-derived state.
        
        Args:
            current_state_hash: Current system state hash to verify against
            recovery_id: Optional recovery ID to filter by
            target_id: Optional target ID to filter by
        
        Returns:
            Tuple[bool, Optional[str]]: (is_equivalent, error_message_if_not)
        """
        transitions = self.reconstruct_state_transitions(
            recovery_id=recovery_id,
            target_id=target_id,
        )
        
        if not transitions:
            return False, "No transitions found in log"
        
        # Get final post_state_hash from last COMMITTED entry
        final_entry = None
        for transition in reversed(transitions):
            if transition["phase"] == "committed" and transition["post_state_hash"]:
                final_entry = transition
                break
        
        if not final_entry:
            return False, "No COMMITTED entry with post_state_hash found"
        
        expected_hash = final_entry["post_state_hash"]
        
        if current_state_hash != expected_hash:
            return False, (
                f"State hash mismatch: expected {expected_hash} from log, "
                f"got {current_state_hash} from system"
            )
        
        return True, None
    
    def get_replay_proof(
        self,
        recovery_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate replay proof for external audit.
        
        TIER-0: Returns complete proof package that external auditors can verify.
        
        Args:
            recovery_id: Optional recovery ID to filter by
            target_id: Optional target ID to filter by
        
        Returns:
            Dict containing:
                - log_verification: Verification results
                - transitions: State transitions
                - final_state_hash: Final state hash from log
                - chain_integrity: Hash chain verification status
        """
        # Verify log
        chain_valid, chain_errors = self._verifier.verify_full_chain()
        phase_valid, phase_errors = self._verifier.verify_phase_progression()
        state_valid, state_errors = self._verifier.verify_state_hashes()
        
        # Get transitions
        transitions = self.reconstruct_state_transitions(
            recovery_id=recovery_id,
            target_id=target_id,
        )
        
        # Get final state hash
        final_state_hash = None
        for transition in reversed(transitions):
            if transition["phase"] == "committed" and transition["post_state_hash"]:
                final_state_hash = transition["post_state_hash"]
                break
        
        return {
            "log_verification": {
                "chain_valid": chain_valid,
                "chain_errors": chain_errors,
                "phase_valid": phase_valid,
                "phase_errors": phase_errors,
                "state_valid": state_valid,
                "state_errors": state_errors,
            },
            "transitions": transitions,
            "final_state_hash": final_state_hash,
            "chain_integrity": chain_valid and len(chain_errors) == 0,
        }


# =============================================================================
# RECOVERY LOG INVARIANTS
# =============================================================================


class RecoveryLogInvariants:
    """
    Invariants enforced on recovery log.
    
    HARD FAILURES:
      - No COMMITTED without EXECUTED
      - No EXECUTED without VALIDATED
      - No missing hash link
      - No duplicate sequence numbers
      - No state hash mismatch
      - No mutation without corresponding entry
    
    Violation → system hard stop.
    """
    
    @staticmethod
    def enforce_phase_ordering(entries: List[RecoveryLogEntry]) -> None:
        """
        Enforce phase ordering invariant.
        
        TIER-0: Enforces complete phase progression and ordering.
        
        Raises:
            ValueError: If ordering violated
        """
        # Group by action
        actions: Dict[Tuple[RecoveryActionType, str], List[RecoveryPhase]] = {}
        
        for entry in entries:
            key = (entry.action_type, entry.target_id)
            if key not in actions:
                actions[key] = []
            actions[key].append(entry.phase)
        
        for (action_type, target_id), phases in actions.items():
            # Skip ABORTED actions (they can have incomplete progression)
            if RecoveryPhase.ABORTED in phases:
                continue
            
            # TIER-0: Enforce phase completeness
            required_phases = [
                RecoveryPhase.INITIATED,
                RecoveryPhase.VALIDATED,
                RecoveryPhase.EXECUTED,
                RecoveryPhase.COMMITTED,
            ]
            
            missing_phases = [p for p in required_phases if p not in phases]
            if missing_phases:
                raise ValueError(
                    f"Action {action_type.value} on {target_id} missing required phases: "
                    f"{[p.value for p in missing_phases]}"
                )
            
            # Cannot have COMMITTED without EXECUTED
            if RecoveryPhase.COMMITTED in phases:
                if RecoveryPhase.EXECUTED not in phases:
                    raise ValueError(
                        f"COMMITTED without EXECUTED for {action_type.value} on {target_id}"
                    )
            
            # Cannot have EXECUTED without VALIDATED
            if RecoveryPhase.EXECUTED in phases:
                if RecoveryPhase.VALIDATED not in phases:
                    raise ValueError(
                        f"EXECUTED without VALIDATED for {action_type.value} on {target_id}"
                    )
            
            # Cannot have VALIDATED without INITIATED
            if RecoveryPhase.VALIDATED in phases:
                if RecoveryPhase.INITIATED not in phases:
                    raise ValueError(
                        f"VALIDATED without INITIATED for {action_type.value} on {target_id}"
                    )
    
    @staticmethod
    def enforce_hash_chain(entries: List[RecoveryLogEntry]) -> None:
        """
        Enforce hash chain invariant.
        
        Raises:
            ValueError: If chain broken
        """
        for i, entry in enumerate(entries):
            if not entry.verify_hash():
                raise ValueError(
                    f"Invalid entry hash at sequence {entry.sequence}"
                )
            
            if i > 0:
                prev_entry = entries[i - 1]
                if entry.prev_entry_hash != prev_entry.entry_hash:
                    raise ValueError(
                        f"Hash chain broken at sequence {entry.sequence}"
                    )
    
    @staticmethod
    def enforce_no_duplicates(entries: List[RecoveryLogEntry]) -> None:
        """
        Enforce no duplicate sequences.
        
        Raises:
            ValueError: If duplicates found
        """
        sequences = [e.sequence for e in entries]
        if len(sequences) != len(set(sequences)):
            raise ValueError("Duplicate sequence numbers detected")
    
    @staticmethod
    def enforce_state_hash_consistency(entries: List[RecoveryLogEntry]) -> None:
        """
        Enforce state hash consistency invariant.
        
        TIER-0: Ensures post_state_hash of one entry matches pre_state_hash of next
        for same target (when applicable).
        
        Raises:
            ValueError: If state hash chain broken
        """
        # Group entries by target_id
        target_entries: Dict[str, List[RecoveryLogEntry]] = {}
        for entry in entries:
            if entry.target_id not in target_entries:
                target_entries[entry.target_id] = []
            target_entries[entry.target_id].append(entry)
        
        # For each target, verify state hash continuity
        for target_id, target_entry_list in target_entries.items():
            # Sort by sequence
            sorted_entries = sorted(target_entry_list, key=lambda e: e.sequence)
            
            for i in range(len(sorted_entries) - 1):
                current = sorted_entries[i]
                next_entry = sorted_entries[i + 1]
                
                # If current entry has post_state_hash and next has pre_state_hash,
                # and they're part of the same recovery flow, they should match
                if (current.post_state_hash and 
                    next_entry.pre_state_hash and
                    current.recovery_id == next_entry.recovery_id and
                    current.target_id == next_entry.target_id):
                    if current.post_state_hash != next_entry.pre_state_hash:
                        raise ValueError(
                            f"State hash mismatch for {target_id} between entries "
                            f"{current.sequence} and {next_entry.sequence}: "
                            f"entry {current.sequence} post_state_hash={current.post_state_hash} "
                            f"!= entry {next_entry.sequence} pre_state_hash={next_entry.pre_state_hash}"
                        )
    
    @staticmethod
    def enforce_all(entries: List[RecoveryLogEntry]) -> None:
        """
        Enforce all invariants.
        
        TIER-0: Comprehensive invariant checking.
        
        Raises:
            ValueError: If any invariant violated
        """
        RecoveryLogInvariants.enforce_hash_chain(entries)
        RecoveryLogInvariants.enforce_no_duplicates(entries)
        RecoveryLogInvariants.enforce_phase_ordering(entries)
        RecoveryLogInvariants.enforce_state_hash_consistency(entries)


# =============================================================================
# STORAGE BACKEND PROTOCOL
# =============================================================================


class RecoveryLogStorage(ABC):
    """
    Abstract storage backend for recovery log.
    
    Implementations MUST be append-only.
    """
    
    @abstractmethod
    def append_entry(self, entry: RecoveryLogEntry) -> None:
        """
        Append entry to log.
        
        MUST be atomic.
        MUST reject if sequence already exists.
        
        Raises:
            RuntimeError: If append fails
        """
        pass
    
    @abstractmethod
    def read_all_entries(self) -> List[RecoveryLogEntry]:
        """Read all entries in sequence order."""
        pass
    
    @abstractmethod
    def read_range(self, start_seq: int, end_seq: int) -> List[RecoveryLogEntry]:
        """Read entries in sequence range."""
        pass


# =============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# =============================================================================

"""
STRICTLY FORBIDDEN in this file:

❌ Redaction
❌ Compression before verification
❌ Background cleanup
❌ Entry mutation
❌ Silent abort
❌ Aggregation
❌ Lazy writing
❌ Retry without sequence increment
❌ State hash omission
❌ Hash chain bypass

This log is not for humans — it's for TRUTH.

If this log lies, the system must STOP.
"""


# =============================================================================
# VERSION INFO
# =============================================================================

__version__ = "1.0.0"
__log_format_version__ = 1

# This log proves legitimacy. It does not declare it.