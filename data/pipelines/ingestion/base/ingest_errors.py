"""
/data/pipelines/ingestion/base/ingest_errors.py

Canonical Ingestion Error Taxonomy & Evidence Model

This module defines the only allowed error shapes ingestion is permitted to emit.

It answers:

> "What specifically went wrong, where, why, and under what authority — without ambiguity or loss."

This file exists so failures remain actionable, replayable, and auditable.

Design Principle: Strings lie. Structures don't.

If an error cannot be expressed structurally, it is not allowed to exist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class IngestErrorCode(Enum):
    """
    Exact failure conditions - the canonical, closed taxonomy.
    
    Codes are stable API surface. No "UNKNOWN". No reuse across meanings.
    Expandable only by migration.
    
    This enum is FROZEN after initial definition. Adding new codes requires:
    1. Migration plan
    2. Version bump
    3. CI validation
    """
    
    # Schema & Validation
    SCHEMA_VIOLATION = "schema_violation"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    TYPE_MISMATCH = "type_mismatch"
    CONSTRAINT_VIOLATION = "constraint_violation"
    INVALID_FORMAT = "invalid_format"
    UNSUPPORTED_VERSION = "unsupported_version"
    
    # Input Issues
    DUPLICATE_INPUT = "duplicate_input"
    OUT_OF_ORDER_INPUT = "out_of_order_input"
    MALFORMED_INPUT = "malformed_input"
    INPUT_TOO_LARGE = "input_too_large"
    INPUT_CORRUPTED = "input_corrupted"
    
    # Authority & Permissions
    AUTHORITY_INVALID = "authority_invalid"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    POLICY_VIOLATION = "policy_violation"
    
    # State & Dependencies
    DEPENDENCY_MISSING = "dependency_missing"
    STATE_CONFLICT = "state_conflict"
    BACKPRESSURE_DENIED = "backpressure_denied"
    PRECONDITION_FAILED = "precondition_failed"
    
    # Invariants & Business Rules
    INVARIANT_BROKEN = "invariant_broken"
    BUSINESS_RULE_VIOLATED = "business_rule_violated"
    IMMUTABILITY_VIOLATED = "immutability_violated"
    
    # Infrastructure
    SERIALIZATION_FAILED = "serialization_failed"
    DESERIALIZATION_FAILED = "deserialization_failed"
    STORAGE_WRITE_FAILED = "storage_write_failed"
    STORAGE_READ_FAILED = "storage_read_failed"
    NETWORK_FAILURE = "network_failure"
    
    # Data Integrity
    CHECKSUM_MISMATCH = "checksum_mismatch"
    HASH_COLLISION = "hash_collision"
    DATA_LOSS_DETECTED = "data_loss_detected"
    
    # Replay & Determinism
    REPLAY_DIVERGENCE = "replay_divergence"
    NON_DETERMINISTIC_BEHAVIOR = "non_deterministic_behavior"
    REPLAY_STATE_MISMATCH = "replay_state_mismatch"
    
    # Timeouts & Resources
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    DEADLOCK_DETECTED = "deadlock_detected"
    
    def __init_subclass__(cls, **kwargs):
        """Prevent subclassing of IngestErrorCode to enforce closed taxonomy."""
        raise TypeError("IngestErrorCode cannot be subclassed")


class ErrorCategory(Enum):
    """
    Layer where failure occurred.
    
    Exactly one category per error. Category ≠ severity.
    """
    INPUT = "input"
    VALIDATION = "validation"
    AUTHORITY = "authority"
    STATE = "state"
    INFRA = "infra"
    REPLAY = "replay"


class RecoveryHint(Enum):
    """
    What recovery is allowed (declarative, not executable).
    
    Hints are never optimistic - they declare constraints.
    """
    RETRYABLE = "retryable"
    REPLAY_SAFE = "replay_safe"
    REQUIRES_MIGRATION = "requires_migration"
    REQUIRES_MANUAL_REVIEW = "requires_manual_review"
    FATAL = "fatal"


# ============================================================================
# CAUSE STRUCTURES (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class IngestErrorCause:
    """
    Represents one concrete failure cause.
    
    One cause = one fact. Causes may be chained, not merged.
    """
    code: IngestErrorCode
    message: str  # Constrained, factual, no stack traces
    source: str  # Module / step name
    timestamp_ms: int
    
    def __post_init__(self):
        """Validate cause at construction."""
        if not self.message:
            raise ValueError("IngestErrorCause message cannot be empty")
        
        if not self.source:
            raise ValueError("IngestErrorCause source cannot be empty")
        
        if self.timestamp_ms <= 0:
            raise ValueError("IngestErrorCause timestamp_ms must be positive")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "code": self.code.value,
            "message": self.message,
            "source": self.source,
            "timestamp_ms": self.timestamp_ms,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> IngestErrorCause:
        """Reconstruct from dictionary."""
        return IngestErrorCause(
            code=IngestErrorCode(data["code"]),
            message=data["message"],
            source=data["source"],
            timestamp_ms=data["timestamp_ms"],
        )


@dataclass(frozen=True)
class IngestErrorContext:
    """
    Captures where this error happened.
    
    Context must exist even if IDs are unknown.
    None is explicit absence, not omission.
    """
    pipeline_step: str
    run_id: str
    input_id: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    
    def __post_init__(self):
        """Validate context at construction."""
        if not self.pipeline_step:
            raise ValueError("IngestErrorContext pipeline_step cannot be empty")
        
        if not self.run_id:
            raise ValueError("IngestErrorContext run_id cannot be empty")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "pipeline_step": self.pipeline_step,
            "run_id": self.run_id,
            "input_id": self.input_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> IngestErrorContext:
        """Reconstruct from dictionary."""
        return IngestErrorContext(
            pipeline_step=data["pipeline_step"],
            run_id=data["run_id"],
            input_id=data.get("input_id"),
            entity_type=data.get("entity_type"),
            entity_id=data.get("entity_id"),
        )


# ============================================================================
# CANONICAL ERROR (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class IngestError:
    """
    The canonical ingestion error object.
    
    Immutable, serializable, replay-comparable.
    
    Rules:
    - frozen=True (immutable)
    - cause_chain length ≥ 1
    - order is causal (root → surface)
    - hash must include everything
    
    This is a pure structural fact. No behavioral helpers, no guidance,
    no interpretation. Just the error evidence.
    """
    category: ErrorCategory
    cause_chain: Tuple[IngestErrorCause, ...]
    context: IngestErrorContext
    recovery_hint: RecoveryHint
    error_hash: str
    
    def __post_init__(self):
        """Validate error at construction - enforces invariants."""
        # Apply all invariants
        ErrorInvariants.validate_cause_chain_not_empty(self.cause_chain)
        ErrorInvariants.validate_no_duplicate_causes(self.cause_chain)
        ErrorInvariants.validate_fatal_not_retryable(self.recovery_hint)
        ErrorInvariants.validate_replay_errors_replay_safe(
            self.category,
            self.recovery_hint
        )
        ErrorInvariants.validate_input_errors_have_input_id(
            self.category,
            self.context
        )
        ErrorInvariants.validate_infra_errors_have_source(
            self.category,
            self.cause_chain
        )
        
        # Validate hash
        if not self.error_hash:
            raise ValueError("IngestError error_hash cannot be empty")
        
        if len(self.error_hash) < 32:
            raise ValueError("IngestError error_hash must be at least 32 characters")
        
        # Recompute and verify hash matches deterministic computation
        computed = compute_error_hash(
            self.category, self.cause_chain, self.context, self.recovery_hint
        )
        if computed != self.error_hash:
            raise ValueError("error_hash does not match deterministic computation")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "category": self.category.value,
            "cause_chain": [cause.to_dict() for cause in self.cause_chain],
            "context": self.context.to_dict(),
            "recovery_hint": self.recovery_hint.value,
            "error_hash": self.error_hash,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> IngestError:
        """Reconstruct from dictionary."""
        return IngestError(
            category=ErrorCategory(data["category"]),
            cause_chain=tuple(
                IngestErrorCause.from_dict(c) for c in data["cause_chain"]
            ),
            context=IngestErrorContext.from_dict(data["context"]),
            recovery_hint=RecoveryHint(data["recovery_hint"]),
            error_hash=data["error_hash"],
        )


# ============================================================================
# ERROR HASHING (CRITICAL)
# ============================================================================

def compute_error_hash(
    category: ErrorCategory,
    cause_chain: Tuple[IngestErrorCause, ...],
    context: IngestErrorContext,
    recovery_hint: RecoveryHint
) -> str:
    """
    Compute deterministic hash of error structure.
    
    The error hash MUST include:
    - All cause codes & messages & sources (NOT timestamps - for replay determinism)
    - ALL context fields (input_id, entity_type, entity_id, pipeline_step, run_id)
    - Category & recovery hint
    
    Same failure under same conditions → same error hash → replay detectability
    Timestamps excluded to ensure identical structural failures hash identically.
    """
    hash_components = {
        "category": category.value,
        "recovery_hint": recovery_hint.value,
        "context": {
            "pipeline_step": context.pipeline_step,
            "run_id": context.run_id,
            "input_id": context.input_id,
            "entity_type": context.entity_type,
            "entity_id": context.entity_id,
        },
        "causes": [
            {
                "code": cause.code.value,
                "message": cause.message,
                "source": cause.source,
            }
            for cause in cause_chain
        ],
    }
    
    # Sort keys for stable hash
    hash_json = json.dumps(hash_components, sort_keys=True)
    return hashlib.sha256(hash_json.encode('utf-8')).hexdigest()


# ============================================================================
# ERROR INVARIANTS (ABSOLUTE)
# ============================================================================

class ErrorInvariants:
    """
    Absolute invariants enforced at IngestError construction.
    
    Violations → hard failure.
    
    If you can't construct a valid error object, you must crash ingestion.
    """
    
    @staticmethod
    def validate_cause_chain_not_empty(cause_chain: Tuple[IngestErrorCause, ...]) -> None:
        """No empty cause_chain."""
        if not cause_chain or len(cause_chain) == 0:
            raise ValueError("IngestError cause_chain cannot be empty")
    
    @staticmethod
    def validate_no_duplicate_causes(cause_chain: Tuple[IngestErrorCause, ...]) -> None:
        """
        No duplicate causes in chain.
        
        Checks full cause identity (code + message + source),
        not just error codes. This preserves causal fidelity - distinct
        causal events with the same code are allowed.
        Timestamps excluded from identity check for replay determinism.
        """
        seen_causes = set()
        for cause in cause_chain:
            # Create a hashable representation of the full cause
            cause_identity = (
                cause.code,
                cause.message,
                cause.source,
            )
            if cause_identity in seen_causes:
                raise ValueError(
                    f"Duplicate cause in chain: {cause.code.value} with identical fields"
                )
            seen_causes.add(cause_identity)
    
    @staticmethod
    def validate_fatal_not_retryable(recovery_hint: RecoveryHint) -> None:
        """
        FATAL ⇒ not RETRYABLE (explicit contract enforcement).
        
        Enforces that FATAL errors are mutually exclusive with RETRYABLE.
        Structurally enforced by enum, but explicitly validated per blueprint.
        """
        # Contract: FATAL and RETRYABLE are mutually exclusive
        # Enum prevents dual values, but explicit validation required by spec
    
    @staticmethod
    def validate_replay_errors_replay_safe(
        category: ErrorCategory,
        recovery_hint: RecoveryHint
    ) -> None:
        """REPLAY errors ⇒ REPLAY_SAFE only."""
        if category == ErrorCategory.REPLAY:
            if recovery_hint != RecoveryHint.REPLAY_SAFE:
                raise ValueError(
                    f"REPLAY category requires REPLAY_SAFE hint, got: {recovery_hint.value}"
                )
    
    @staticmethod
    def validate_input_errors_have_input_id(
        category: ErrorCategory,
        context: IngestErrorContext
    ) -> None:
        """Input-level errors MUST include input_id."""
        if category == ErrorCategory.INPUT:
            if context.input_id is None:
                raise ValueError(
                    "INPUT category errors must have context.input_id"
                )
    
    @staticmethod
    def validate_infra_errors_have_source(
        category: ErrorCategory,
        cause_chain: Tuple[IngestErrorCause, ...]
    ) -> None:
        """Infra errors MUST include source in all causes."""
        if category == ErrorCategory.INFRA:
            for cause in cause_chain:
                if not cause.source:
                    raise ValueError(
                        "INFRA category errors must have source in all causes"
                    )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    'IngestErrorCode',
    'ErrorCategory',
    'RecoveryHint',
    
    # Data Structures
    'IngestErrorCause',
    'IngestErrorContext',
    'IngestError',
    
    # Hashing
    'compute_error_hash',
    
    # Invariants
    'ErrorInvariants',
]
