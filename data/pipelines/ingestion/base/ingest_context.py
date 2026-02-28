"""
/data/pipelines/ingestion/base/ingest_context.py

Immutable Ingest Execution Context

This module defines the unforgeable, immutable execution envelope for every ingestion operation.

It answers:

> "Under exactly what conditions was this data admitted into the system?"

If the answer is unclear, the data is illegal.

Design Principle: If ingestion can't prove its context, the fact does not exist.

Every ingested fact must be traceable to a single, immutable context object.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class IngestMode(Enum):
    """
    Defines why ingestion is happening.
    
    LIVE        → real-time platform ingestion
    BATCH       → scheduled backfill
    REPLAY      → deterministic re-execution
    RECOVERY    → post-failure repair ingestion
    AUDIT       → forensic reconstruction
    
    Rules:
    - Mode is explicit
    - Mode never inferred
    - Mode never changes mid-run
    """
    LIVE = "live"
    BATCH = "batch"
    REPLAY = "replay"
    RECOVERY = "recovery"
    AUDIT = "audit"


class IngestAuthority(Enum):
    """
    Defines who authorized the ingestion.
    
    PLATFORM        → external source of truth
    RECOVERY_ENGINE → repair pipeline
    WATCHDOG        → emergency override
    SYSTEM          → internal canonical correction
    
    Rules:
    - Authority must be declared
    - No anonymous ingestion
    - Authority is audited downstream
    """
    PLATFORM = "platform"
    RECOVERY_ENGINE = "recovery_engine"
    WATCHDOG = "watchdog"
    SYSTEM = "system"


# ============================================================================
# EXCEPTIONS
# ============================================================================

class ContextInvariantViolation(Exception):
    """
    Raised when context invariants are violated at construction time.
    
    Violations → construction fails hard.
    """
    pass


# ============================================================================
# IMMUTABLE CONTEXT
# ============================================================================

@dataclass(frozen=True)
class IngestContext:
    """
    Frozen, immutable execution context for ingestion operations.
    
    This is the unforgeable passport stamp for data.
    No stamp → no entry → no analytics → no revenue.
    
    Rules:
    - frozen=True (immutable)
    - hashable=True
    - comparable=True
    - no defaults except explicit None
    """
    
    # Canonical execution identifier (links to infra runtime_context)
    run_id: str
    
    # Exact pipeline code version
    pipeline_version: str
    
    # Why ingestion exists
    mode: IngestMode
    
    # Who allowed it
    authority: IngestAuthority
    
    # Source platform (if external)
    platform: Optional[str]
    
    # Whether watchdog emergency constraints apply
    emergency_mode: bool
    
    # Bound replay session (if applicable)
    replay_id: Optional[str]
    
    # Canonical ingestion time (normalized upstream, epoch milliseconds)
    timestamp_ms: int
    
    # Deterministic fingerprint of the entire context
    context_hash: str
    
    def __post_init__(self):
        """
        Enforce context invariants at construction time.
        
        Violations → construction fails hard. No partial contexts allowed.
        """
        ContextInvariants.enforce(self)
    
    def __hash__(self) -> int:
        """
        Make context hashable for use in sets and dicts.
        
        Uses context_hash as the basis for hash.
        """
        return hash(self.context_hash)
    
    def __eq__(self, other: object) -> bool:
        """
        Context equality based on all semantic fields.
        
        Two contexts are equal if all fields are identical.
        This ensures semantic equivalence, not just hash equality.
        """
        if not isinstance(other, IngestContext):
            return False
        return (
            self.run_id == other.run_id
            and self.pipeline_version == other.pipeline_version
            and self.mode == other.mode
            and self.authority == other.authority
            and self.platform == other.platform
            and self.emergency_mode == other.emergency_mode
            and self.replay_id == other.replay_id
            and self.timestamp_ms == other.timestamp_ms
            and self.context_hash == other.context_hash
        )


# ============================================================================
# CONTEXT INVARIANTS (ABSOLUTE)
# ============================================================================

class ContextInvariants:
    """
    Absolute invariants enforced at IngestContext construction.
    
    Violations → construction fails hard.
    
    No partial contexts allowed.
    """
    
    @staticmethod
    def enforce(context: IngestContext) -> None:
        """
        Enforce all context invariants.
        
        Raises ContextInvariantViolation on any violation.
        """
        ContextInvariants._replay_mode_requires_replay_id(context)
        ContextInvariants._recovery_mode_requires_recovery_authority(context)
        ContextInvariants._emergency_mode_requires_watchdog_authority(context)
        ContextInvariants._platform_authority_requires_platform(context)
        ContextInvariants._pipeline_version_cannot_be_empty(context)
        ContextInvariants._timestamp_must_be_valid(context)
        ContextInvariants._context_hash_must_be_valid(context)
        ContextInvariants._context_hash_must_match_recomputed(context)
    
    @staticmethod
    def _replay_mode_requires_replay_id(context: IngestContext) -> None:
        """Invariant: replay mode REQUIRES replay_id."""
        if context.mode == IngestMode.REPLAY:
            if not context.replay_id or not context.replay_id.strip():
                raise ContextInvariantViolation(
                    "INVARIANT VIOLATION: REPLAY mode requires replay_id"
                )
    
    @staticmethod
    def _recovery_mode_requires_recovery_authority(context: IngestContext) -> None:
        """Invariant: recovery mode REQUIRES recovery authority."""
        if context.mode == IngestMode.RECOVERY:
            if context.authority != IngestAuthority.RECOVERY_ENGINE:
                raise ContextInvariantViolation(
                    f"INVARIANT VIOLATION: RECOVERY mode requires RECOVERY_ENGINE authority, "
                    f"got {context.authority.value}"
                )
    
    @staticmethod
    def _emergency_mode_requires_watchdog_authority(context: IngestContext) -> None:
        """Invariant: emergency_mode cannot be true without watchdog authority."""
        if context.emergency_mode:
            if context.authority != IngestAuthority.WATCHDOG:
                raise ContextInvariantViolation(
                    f"INVARIANT VIOLATION: emergency_mode requires WATCHDOG authority, "
                    f"got {context.authority.value}"
                )
    
    @staticmethod
    def _platform_authority_requires_platform(context: IngestContext) -> None:
        """Invariant: platform must be present for PLATFORM authority."""
        if context.authority == IngestAuthority.PLATFORM:
            if not context.platform or not context.platform.strip():
                raise ContextInvariantViolation(
                    "INVARIANT VIOLATION: PLATFORM authority requires platform to be set"
                )
    
    @staticmethod
    def _pipeline_version_cannot_be_empty(context: IngestContext) -> None:
        """Invariant: pipeline_version cannot be empty."""
        if not context.pipeline_version or not context.pipeline_version.strip():
            raise ContextInvariantViolation(
                "INVARIANT VIOLATION: pipeline_version cannot be empty"
            )
    
    @staticmethod
    def _timestamp_must_be_valid(context: IngestContext) -> None:
        """
        Invariant: timestamp_ms must be valid (positive).
        
        Note: Monotonic timestamp enforcement per run_id requires state tracking
        and must be enforced at the orchestration layer (runtime context or
        ingestion orchestrator). This file is a pure immutable truth container
        and cannot maintain mutable runtime state.
        
        Orchestration layers MUST enforce: timestamp_ms must be strictly
        monotonic per run_id. Violations indicate ordering integrity breach.
        """
        if context.timestamp_ms <= 0:
            raise ContextInvariantViolation(
                f"INVARIANT VIOLATION: timestamp_ms must be positive, got {context.timestamp_ms}"
            )
    
    @staticmethod
    def _context_hash_must_be_valid(context: IngestContext) -> None:
        """Invariant: context_hash must be present and valid."""
        if not context.context_hash or not context.context_hash.strip():
            raise ContextInvariantViolation(
                "INVARIANT VIOLATION: context_hash must be present"
            )
        
        # Verify hash length (SHA-256 produces 64 hex characters)
        if len(context.context_hash) != 64:
            raise ContextInvariantViolation(
                f"INVARIANT VIOLATION: context_hash must be 64 characters (SHA-256), "
                f"got {len(context.context_hash)}"
            )
    
    @staticmethod
    def _context_hash_must_match_recomputed(context: IngestContext) -> None:
        """
        Invariant: context_hash must match recomputed canonical hash.
        
        This prevents hash forgery by ensuring the provided hash matches
        the deterministic recomputation of all context fields.
        """
        recomputed_hash = ContextInvariants._compute_context_hash(context)
        if context.context_hash != recomputed_hash:
            raise ContextInvariantViolation(
                f"INVARIANT VIOLATION: context_hash does not match recomputed hash. "
                f"Provided: {context.context_hash[:16]}..., "
                f"Expected: {recomputed_hash[:16]}... "
                f"(Hash forgery detected)"
            )
    
    @staticmethod
    def _compute_context_hash(context: IngestContext) -> str:
        """
        Compute deterministic context hash from context fields.
        
        This is a private method used only for validation.
        The hash MUST:
        - be deterministic
        - include all fields
        - exclude runtime-only memory identity
        - be stable across replays
        
        Returns:
            64-character hex string (SHA-256)
        """
        # Build canonical representation
        canonical = {
            'run_id': context.run_id,
            'pipeline_version': context.pipeline_version,
            'mode': context.mode.value,
            'authority': context.authority.value,
            'platform': context.platform,
            'emergency_mode': context.emergency_mode,
            'replay_id': context.replay_id,
            'timestamp_ms': context.timestamp_ms,
        }
        
        # Sort keys for determinism
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        
        # Compute SHA-256 hash
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'IngestMode',
    'IngestAuthority',
    'IngestContext',
    'ContextInvariantViolation',
    'ContextInvariants',
]
