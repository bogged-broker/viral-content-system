"""
/data/pipelines/ingestion/base/ingest_result.py

Deterministic Ingestion Outcome Contract

This module defines the complete, immutable outcome record for a single ingestion step.

It answers:

> "Did ingestion succeed — and if so, what exactly changed?"

No vibes. No assumptions. No "probably fine".

Design Principle: If ingestion cannot prove its result, it failed.

Silence is not success.
Partial success is explicit.
Failure is never hidden.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .ingest_context import IngestContext
else:
    from .ingest_context import IngestContext


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class IngestStatus(Enum):
    """
    Defines the final truth of the ingest step.
    
    SUCCESS     → all inputs processed & committed
    PARTIAL     → some inputs admitted, others rejected
    FAILED      → no mutations committed
    SKIPPED     → explicitly bypassed (policy / backpressure)
    
    Rules:
    - Exactly one status
    - Status never inferred
    - Status never escalated downstream
    """
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class RejectionReason(Enum):
    """
    Defines why a record was not ingested.
    
    Examples (non-exhaustive but canonical):
    - SCHEMA_INVALID
    - DUPLICATE
    - OUT_OF_ORDER
    - BACKPRESSURE_SHED
    - QUOTA_EXCEEDED
    - INVARIANT_VIOLATION
    - AUTHORITY_MISMATCH
    - STALE_REPLAY
    
    Rules:
    - Every rejection has exactly one reason
    - "UNKNOWN" is forbidden
    - Reasons are stable API surface
    """
    # Schema & Validation
    SCHEMA_INVALID = "schema_invalid"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_FORMAT = "invalid_format"
    
    # Input Issues
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    
    # Authority & Permissions
    AUTHORITY_MISMATCH = "authority_mismatch"
    QUOTA_EXCEEDED = "quota_exceeded"
    POLICY_VIOLATION = "policy_violation"
    BACKPRESSURE_SHED = "backpressure_shed"
    
    # Context & Canonicalization
    CONTEXT_MISMATCH = "context_mismatch"
    CANONICALIZATION_FAILED = "canonicalization_failed"
    
    # State & Dependencies
    INVARIANT_VIOLATION = "invariant_violation"
    DEPENDENCY_MISSING = "dependency_missing"
    STATE_CONFLICT = "state_conflict"
    
    # Version & Compatibility
    UNSUPPORTED_VERSION = "unsupported_version"
    
    # Determinism & Replay
    NON_DETERMINISTIC = "non_deterministic"
    STALE_REPLAY = "stale_replay"
    
    # Resources & Timeouts
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"


# ============================================================================
# CORE DATA STRUCTURES (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class MutationRecord:
    """
    Represents a successful, committed change.
    
    Rules:
    - Only for committed mutations
    - No speculative entries
    - Order is deterministic
    """
    entity_type: str
    entity_id: str
    operation: str  # create / update / delete
    version: Optional[str] = None
    hash: str = ""  # post-mutation fingerprint
    
    def __post_init__(self):
        """Validate mutation record at construction."""
        if not self.entity_type or not self.entity_type.strip():
            raise ValueError("MutationRecord entity_type cannot be empty")
        
        if not self.entity_id or not self.entity_id.strip():
            raise ValueError("MutationRecord entity_id cannot be empty")
        
        if self.operation not in ("create", "update", "delete"):
            raise ValueError(
                f"MutationRecord operation must be create/update/delete, got: {self.operation}"
            )
        
        if not self.hash or not self.hash.strip():
            raise ValueError("MutationRecord hash cannot be empty")
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "operation": self.operation,
            "version": self.version,
            "hash": self.hash,
        }
    
    @staticmethod
    def from_dict(data: dict) -> MutationRecord:
        """Reconstruct from dictionary."""
        return MutationRecord(
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            operation=data["operation"],
            version=data.get("version"),
            hash=data.get("hash", ""),
        )


@dataclass(frozen=True)
class RejectionRecord:
    """
    Represents a rejected or ignored input.
    
    Rules:
    - Every rejected input must appear exactly once
    - No collapsing multiple inputs into one record
    """
    input_id: str
    reason: RejectionReason
    details: Optional[str] = None
    
    def __post_init__(self):
        """Validate rejection record at construction."""
        if not self.input_id or not self.input_id.strip():
            raise ValueError("RejectionRecord input_id cannot be empty")
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "input_id": self.input_id,
            "reason": self.reason.value,
            "details": self.details,
        }
    
    @staticmethod
    def from_dict(data: dict) -> RejectionRecord:
        """Reconstruct from dictionary."""
        return RejectionRecord(
            input_id=data["input_id"],
            reason=RejectionReason(data["reason"]),
            details=data.get("details"),
        )


# ============================================================================
# CANONICAL RESULT (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class IngestResult:
    """
    The canonical ingestion outcome object.
    
    Immutable, serializable, replay-comparable.
    
    Rules:
    - frozen=True (immutable)
    - totals must reconcile
    - hashes are mandatory
    - timestamps are monotonic
    """
    context_hash: str  # Must match IngestContext.context_hash
    status: IngestStatus
    inputs_received: int
    inputs_committed: int
    inputs_rejected: int
    mutations: Tuple[MutationRecord, ...]
    rejections: Tuple[RejectionRecord, ...]
    started_at_ms: int
    completed_at_ms: int
    result_hash: str  # Deterministic fingerprint of the full result
    
    def __post_init__(self):
        """Validate result at construction - enforces invariants."""
        # Apply all invariants
        ResultInvariants.validate_totals_reconcile(
            self.inputs_received,
            self.inputs_committed,
            self.inputs_rejected
        )
        ResultInvariants.validate_failed_no_commits(
            self.status,
            self.inputs_committed
        )
        ResultInvariants.validate_success_no_rejections(
            self.status,
            self.inputs_rejected
        )
        ResultInvariants.validate_skipped_no_mutations(
            self.status,
            self.mutations
        )
        ResultInvariants.validate_partial_has_both(
            self.status,
            self.mutations,
            self.rejections
        )
        ResultInvariants.validate_timestamps_monotonic(
            self.started_at_ms,
            self.completed_at_ms
        )
        ResultInvariants.validate_success_has_mutations(
            self.status,
            self.mutations
        )
        
        # Validate unique rejection input_ids (bijection guarantee)
        ResultInvariants.validate_unique_rejection_ids(self.rejections)
        
        # Validate hashes
        if not self.context_hash or not self.context_hash.strip():
            raise ValueError("IngestResult context_hash cannot be empty")
        
        if not self.result_hash or not self.result_hash.strip():
            raise ValueError("IngestResult result_hash cannot be empty")
        
        if len(self.result_hash) < 32:
            raise ValueError("IngestResult result_hash must be at least 32 characters")
        
        # CRITICAL: Verify hash integrity - if ingestion cannot prove its result, it failed
        computed_hash = compute_result_hash(
            context_hash=self.context_hash,
            status=self.status,
            inputs_received=self.inputs_received,
            inputs_committed=self.inputs_committed,
            inputs_rejected=self.inputs_rejected,
            mutations=self.mutations,
            rejections=self.rejections
        )
        if computed_hash != self.result_hash:
            raise ValueError(
                f"Result hash integrity violation: "
                f"supplied={self.result_hash[:16]}..., "
                f"computed={computed_hash[:16]}... "
                f"(If ingestion cannot prove its result, it failed)"
            )
    
    @property
    def is_success(self) -> bool:
        """Check if ingestion was successful."""
        return self.status == IngestStatus.SUCCESS
    
    @property
    def is_partial(self) -> bool:
        """Check if ingestion was partial."""
        return self.status == IngestStatus.PARTIAL
    
    @property
    def is_failed(self) -> bool:
        """Check if ingestion failed."""
        return self.status == IngestStatus.FAILED
    
    @property
    def is_skipped(self) -> bool:
        """Check if ingestion was skipped."""
        return self.status == IngestStatus.SKIPPED
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "context_hash": self.context_hash,
            "status": self.status.value,
            "inputs_received": self.inputs_received,
            "inputs_committed": self.inputs_committed,
            "inputs_rejected": self.inputs_rejected,
            "mutations": [m.to_dict() for m in self.mutations],
            "rejections": [r.to_dict() for r in self.rejections],
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "result_hash": self.result_hash,
        }
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True)
    
    @staticmethod
    def from_dict(data: dict) -> IngestResult:
        """Reconstruct from dictionary."""
        return IngestResult(
            context_hash=data["context_hash"],
            status=IngestStatus(data["status"]),
            inputs_received=data["inputs_received"],
            inputs_committed=data["inputs_committed"],
            inputs_rejected=data["inputs_rejected"],
            mutations=tuple(
                MutationRecord.from_dict(m) for m in data["mutations"]
            ),
            rejections=tuple(
                RejectionRecord.from_dict(r) for r in data["rejections"]
            ),
            started_at_ms=data["started_at_ms"],
            completed_at_ms=data["completed_at_ms"],
            result_hash=data["result_hash"],
        )
    
    @staticmethod
    def from_json(json_str: str) -> IngestResult:
        """Reconstruct from JSON string."""
        return IngestResult.from_dict(json.loads(json_str))
    
    def __str__(self) -> str:
        """Human-readable representation."""
        lines = [
            f"IngestResult [{self.status.value.upper()}]",
            f"Context hash: {self.context_hash[:16]}...",
            f"Result hash: {self.result_hash[:16]}...",
            f"Inputs: {self.inputs_received} received, "
            f"{self.inputs_committed} committed, "
            f"{self.inputs_rejected} rejected",
            f"Duration: {self.completed_at_ms - self.started_at_ms}ms",
        ]
        
        if self.mutations:
            lines.append(f"Mutations: {len(self.mutations)}")
        
        if self.rejections:
            lines.append(f"Rejections: {len(self.rejections)}")
        
        return "\n".join(lines)


# ============================================================================
# RESULT HASHING (CRITICAL)
# ============================================================================

def compute_result_hash(
    context_hash: str,
    status: IngestStatus,
    inputs_received: int,
    inputs_committed: int,
    inputs_rejected: int,
    mutations: Tuple[MutationRecord, ...],
    rejections: Tuple[RejectionRecord, ...]
) -> str:
    """
    Compute deterministic hash of result structure.
    
    The result hash MUST:
    - include context_hash
    - include all mutations & rejections
    - include status & counts
    - be deterministic across replays
    - NOT include timestamps (they are metadata, not part of outcome)
    
    Two identical inputs under identical context
    → identical result hash
    → provable determinism.
    
    Note: Timestamps (started_at_ms, completed_at_ms) are excluded from hash
    to ensure replay determinism. They are metadata about execution timing,
    not part of the outcome contract itself.
    """
    hash_components = {
        "context_hash": context_hash,
        "status": status.value,
        "inputs_received": inputs_received,
        "inputs_committed": inputs_committed,
        "inputs_rejected": inputs_rejected,
        "mutations": [
            {
                "entity_type": m.entity_type,
                "entity_id": m.entity_id,
                "operation": m.operation,
                "version": m.version,  # Include version for semantic completeness
                "hash": m.hash,
            }
            for m in mutations
        ],
        "rejections": [
            {
                "input_id": r.input_id,
                "reason": r.reason.value,
                "details": r.details,  # Include details for forensic auditability
            }
            for r in rejections
        ],
    }
    
    # Sort keys for stable hash
    hash_json = json.dumps(hash_components, sort_keys=True)
    return hashlib.sha256(hash_json.encode('utf-8')).hexdigest()


def verify_result_hash(result: IngestResult) -> bool:
    """Verify that result hash matches computed hash."""
    computed = compute_result_hash(
        context_hash=result.context_hash,
        status=result.status,
        inputs_received=result.inputs_received,
        inputs_committed=result.inputs_committed,
        inputs_rejected=result.inputs_rejected,
        mutations=result.mutations,
        rejections=result.rejections
    )
    return computed == result.result_hash


# ============================================================================
# RESULT INVARIANTS (ABSOLUTE)
# ============================================================================

class ResultInvariants:
    """
    Absolute invariants enforced at IngestResult construction.
    
    Violations → hard failure.
    
    No "best effort" objects allowed.
    """
    
    @staticmethod
    def validate_totals_reconcile(
        inputs_received: int,
        inputs_committed: int,
        inputs_rejected: int
    ) -> None:
        """Invariant: inputs_received = committed + rejected."""
        if inputs_received != inputs_committed + inputs_rejected:
            raise ValueError(
                f"Totals do not reconcile: "
                f"received={inputs_received}, "
                f"committed={inputs_committed}, "
                f"rejected={inputs_rejected}"
            )
    
    @staticmethod
    def validate_failed_no_commits(
        status: IngestStatus,
        inputs_committed: int
    ) -> None:
        """Invariant: FAILED ⇒ inputs_committed == 0."""
        if status == IngestStatus.FAILED:
            if inputs_committed != 0:
                raise ValueError(
                    f"FAILED status requires inputs_committed=0, got {inputs_committed}"
                )
    
    @staticmethod
    def validate_success_no_rejections(
        status: IngestStatus,
        inputs_rejected: int
    ) -> None:
        """Invariant: SUCCESS ⇒ inputs_rejected == 0."""
        if status == IngestStatus.SUCCESS:
            if inputs_rejected != 0:
                raise ValueError(
                    f"SUCCESS status requires inputs_rejected=0, got {inputs_rejected}"
                )
    
    @staticmethod
    def validate_skipped_no_mutations(
        status: IngestStatus,
        mutations: Tuple[MutationRecord, ...]
    ) -> None:
        """Invariant: SKIPPED ⇒ no mutations."""
        if status == IngestStatus.SKIPPED:
            if mutations:
                raise ValueError(
                    f"SKIPPED status requires no mutations, got {len(mutations)}"
                )
    
    @staticmethod
    def validate_partial_has_both(
        status: IngestStatus,
        mutations: Tuple[MutationRecord, ...],
        rejections: Tuple[RejectionRecord, ...]
    ) -> None:
        """Invariant: PARTIAL ⇒ both mutations AND rejections present."""
        if status == IngestStatus.PARTIAL:
            if not mutations:
                raise ValueError(
                    "PARTIAL status requires mutations to be present"
                )
            if not rejections:
                raise ValueError(
                    "PARTIAL status requires rejections to be present"
                )
    
    @staticmethod
    def validate_timestamps_monotonic(
        started_at_ms: int,
        completed_at_ms: int
    ) -> None:
        """Invariant: completed_at_ms ≥ started_at_ms."""
        if completed_at_ms < started_at_ms:
            raise ValueError(
                f"Timestamps not monotonic: "
                f"started={started_at_ms}, completed={completed_at_ms}"
            )
    
    @staticmethod
    def validate_success_has_mutations(
        status: IngestStatus,
        mutations: Tuple[MutationRecord, ...]
    ) -> None:
        """Invariant: SUCCESS ⇒ mutations present (empty mutation list forbidden)."""
        if status == IngestStatus.SUCCESS:
            if not mutations:
                raise ValueError(
                    "SUCCESS status requires mutations to be present"
                )
    
    @staticmethod
    def validate_unique_rejection_ids(
        rejections: Tuple[RejectionRecord, ...]
    ) -> None:
        """
        Invariant: Each rejected input_id must be unique.
        
        Enforces strict bijection: one rejection per input_id.
        Prevents silent collapsing of duplicate rejections.
        """
        if not rejections:
            return
        
        input_ids = [r.input_id for r in rejections]
        unique_ids = set(input_ids)
        
        if len(input_ids) != len(unique_ids):
            duplicates = [
                input_id for input_id in unique_ids
                if input_ids.count(input_id) > 1
            ]
            raise ValueError(
                f"Duplicate rejection input_ids detected: {duplicates}. "
                f"Each rejected input must appear exactly once (bijection guarantee)."
            )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    'IngestStatus',
    'RejectionReason',
    
    # Data Structures
    'MutationRecord',
    'RejectionRecord',
    'IngestResult',
    
    # Hashing
    'compute_result_hash',
    'verify_result_hash',
    
    # Invariants
    'ResultInvariants',
]
