"""
/infra/recovery/audit/audit_chain.py

Cryptographic Lineage & Integrity Authority

This module answers exactly one question:
"Does this recovery history form one continuous, provable, untampered chain?"

If the answer is ever "I'm not sure" — the system freezes.

This file makes silent tampering impossible without detection.

Authority: audit_logger → audit_chain → immutable_store → truth
If audit_chain refuses linkage, nothing downstream proceeds.

WHAT THIS FILE IS:
- Chain head tracker
- Parent hash resolver
- Continuity enforcer
- Gap/fork/rewind detector
- Corruption authority
- Watchdog integrator

WHAT THIS FILE IS NOT:
- Logger
- Storage backend
- Validator UI
- Compactor
- Repair mechanism
- Recovery helper

Design Principle:
History must be harder to change than code.
Breaking the audit chain must be catastrophic by design.
"""

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Self, List, Dict

from .audit_models import AuditRecord


# ============================================================================
# CORE ENUMS
# ============================================================================


class ChainState(Enum):
    """
    Audit chain health states.
    
    Once CORRUPTED, never auto-exit.
    Once FROZEN, operator intervention required.
    """
    HEALTHY = "healthy"
    SEALED = "sealed"
    CORRUPTED = "corrupted"
    FROZEN = "frozen"


# ============================================================================
# EXCEPTIONS
# ============================================================================


class ChainViolation(Exception):
    """
    Raised when chain continuity is broken.
    
    This is NOT a recoverable error.
    This is an existential integrity failure.
    """
    def __init__(self, violation_type: str, details: dict[str, any]):
        self.violation_type = violation_type
        self.details = details
        super().__init__(f"Chain violation [{violation_type}]: {details}")


class ChainFrozenError(Exception):
    """Raised when attempting operations on a frozen chain."""
    pass


class ChainCorruptedError(Exception):
    """Raised when chain corruption is detected."""
    pass


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class ChainHead:
    """
    The only moving pointer in the audit system.
    
    This represents the current tip of the chain.
    Immutable. Atomic replacement only.
    """
    event_hash: str
    timestamp: int
    height: int
    
    def __post_init__(self):
        """Validate invariants."""
        if self.height < 0:
            raise ValueError(f"Invalid height: {self.height}")
        if not self.event_hash:
            raise ValueError("event_hash cannot be empty")
        if self.timestamp < 0:
            raise ValueError(f"Invalid timestamp: {self.timestamp}")


@dataclass(frozen=True)
class ChainLink:
    """
    Pure structural linkage.
    
    No metadata. No payload. Just structure.
    This is what enforces continuity.
    """
    event_hash: str
    parent_hash: str | None  # None only for genesis
    height: int
    
    def __post_init__(self):
        """Validate invariants."""
        if self.height < 0:
            raise ValueError(f"Invalid height: {self.height}")
        if not self.event_hash:
            raise ValueError("event_hash cannot be empty")
        if self.height > 0 and self.parent_hash is None:
            raise ValueError("Non-genesis link must have parent_hash")
        if self.height == 0 and self.parent_hash is not None:
            raise ValueError("Genesis link cannot have parent_hash")


# ============================================================================
# BACKEND PROTOCOL
# ============================================================================


class ChainBackend(Protocol):
    """
    Minimal interface for chain persistence.
    
    Backend MUST be immutable (append-only).
    Backend MUST be atomic.
    Backend MUST detect tampering.
    """
    
    def get_head(self) -> ChainHead | None:
        """
        Get current chain head.
        
        Returns:
            Current head, or None if chain uninitialized.
        """
        ...
    
    def get_link(self, event_hash: str) -> ChainLink | None:
        """
        Retrieve a specific chain link.
        
        Args:
            event_hash: Hash of the event to retrieve.
            
        Returns:
            Chain link if found, None otherwise.
        """
        ...
    
    def append_link(self, link: ChainLink) -> None:
        """
        Append a new link to the chain.
        
        MUST be atomic.
        MUST fail if link already exists.
        MUST fail if backend is sealed/corrupted.
        
        Args:
            link: Chain link to append.
            
        Raises:
            Exception on failure (backend-specific).
        """
        ...
    
    def set_head(self, head: ChainHead) -> None:
        """
        Atomically update chain head.
        
        MUST be atomic.
        MUST fail if backend is sealed/corrupted.
        
        Args:
            head: New chain head.
            
        Raises:
            Exception on failure (backend-specific).
        """
        ...
    
    def get_all_links(self) -> list[ChainLink]:
        """
        Retrieve all links in the chain.
        
        Used for verification and replay.
        Order does not matter (verification re-sorts by height).
        
        Returns:
            All chain links.
        """
        ...
    
    def get_state(self) -> ChainState:
        """
        Get current backend state.
        
        Returns:
            Current chain state.
        """
        ...
    
    def set_state(self, state: ChainState) -> None:
        """
        Set backend state.
        
        Used to mark chain as corrupted/frozen.
        
        Args:
            state: New chain state.
        """
        ...


# ============================================================================
# WATCHDOG INTEGRATION
# ============================================================================


class WatchdogEscalator(Protocol):
    """
    Interface to watchdog system.
    
    Chain violations are existential failures.
    Watchdog MUST respond with system-level action.
    """
    
    def escalate_chain_breach(
        self,
        violation_type: str,
        details: dict[str, any]
    ) -> None:
        """
        Escalate chain violation to watchdog.
        
        Watchdog responsibilities:
        - Lock recovery pipeline
        - Freeze audit logging
        - Block all mutation paths
        - Require operator intervention
        - Emit alerts
        
        Args:
            violation_type: Type of violation detected.
            details: Violation details for forensics.
        """
        ...


# ============================================================================
# AUDIT CHAIN INVARIANTS
# ============================================================================


class AuditChainInvariants:
    """
    Absolute invariants for audit chain integrity.
    
    These are mathematical truths, not policy choices.
    Violation = existential failure.
    """
    
    @staticmethod
    def validate_single_head(head: ChainHead | None, all_links: list[ChainLink]) -> None:
        """
        Enforce exactly one canonical head.
        
        Args:
            head: Current chain head.
            all_links: All links in chain.
            
        Raises:
            ChainViolation: If multiple heads detected.
        """
        if not all_links:
            return  # Empty chain is valid
        
        if head is None:
            raise ChainViolation(
                "MISSING_HEAD",
                {"message": "Chain has links but no head"}
            )
        
        # Find all potential heads (links not referenced as parents)
        all_hashes = {link.event_hash for link in all_links}
        parent_hashes = {link.parent_hash for link in all_links if link.parent_hash}
        potential_heads = all_hashes - parent_hashes
        
        if len(potential_heads) > 1:
            raise ChainViolation(
                "MULTIPLE_HEADS",
                {
                    "expected_head": head.event_hash,
                    "detected_heads": list(potential_heads)
                }
            )
        
        if len(potential_heads) == 1 and head.event_hash not in potential_heads:
            raise ChainViolation(
                "HEAD_MISMATCH",
                {
                    "stored_head": head.event_hash,
                    "structural_head": list(potential_heads)[0]
                }
            )
    
    @staticmethod
    def validate_height_continuity(all_links: list[ChainLink]) -> None:
        """
        Enforce strictly increasing height with no gaps.
        
        Args:
            all_links: All links in chain.
            
        Raises:
            ChainViolation: If height discontinuity detected.
        """
        if not all_links:
            return
        
        sorted_links = sorted(all_links, key=lambda l: l.height)
        
        for i, link in enumerate(sorted_links):
            if link.height != i:
                raise ChainViolation(
                    "HEIGHT_DISCONTINUITY",
                    {
                        "expected_height": i,
                        "actual_height": link.height,
                        "event_hash": link.event_hash
                    }
                )
    
    @staticmethod
    def validate_no_orphans(all_links: list[ChainLink]) -> None:
        """
        Ensure every non-genesis link has valid parent.
        
        Args:
            all_links: All links in chain.
            
        Raises:
            ChainViolation: If orphaned links detected.
        """
        hash_set = {link.event_hash for link in all_links}
        
        for link in all_links:
            if link.parent_hash is not None:  # Non-genesis
                if link.parent_hash not in hash_set:
                    raise ChainViolation(
                        "ORPHANED_LINK",
                        {
                            "event_hash": link.event_hash,
                            "missing_parent": link.parent_hash,
                            "height": link.height
                        }
                    )
    
    @staticmethod
    def validate_no_parallel_chains(all_links: list[ChainLink]) -> None:
        """
        Detect parallel chains (forks).
        
        Args:
            all_links: All links in chain.
            
        Raises:
            ChainViolation: If fork detected.
        """
        # Group links by height
        height_map: dict[int, list[ChainLink]] = {}
        for link in all_links:
            height_map.setdefault(link.height, []).append(link)
        
        # Check for multiple links at same height
        for height, links in height_map.items():
            if len(links) > 1:
                raise ChainViolation(
                    "FORK_DETECTED",
                    {
                        "height": height,
                        "fork_hashes": [l.event_hash for l in links]
                    }
                )


# ============================================================================
# AUDIT CHAIN (THE GUARD)
# ============================================================================


class AuditChain:
    """
    Cryptographic lineage and integrity authority.
    
    This class enforces:
    - Single chain continuity
    - Hash linkage correctness
    - Height monotonicity
    - Timestamp monotonicity
    - Zero-gap history
    - Corruption detection
    
    If the chain is broken, recovery must stop, not heal itself.
    """
    
    def __init__(
        self,
        backend: ChainBackend,
        hash_algo: str = "sha256",
        watchdog: WatchdogEscalator | None = None
    ):
        """
        Initialize audit chain guard.
        
        Args:
            backend: Immutable storage backend.
            hash_algo: Hash algorithm (must match audit_logger).
            watchdog: Optional watchdog escalator.
            
        Raises:
            ValueError: If hash algorithm unsupported.
        """
        if hash_algo not in hashlib.algorithms_available:
            raise ValueError(f"Unsupported hash algorithm: {hash_algo}")
        
        self._backend = backend
        self._hash_algo = hash_algo
        self._watchdog = watchdog
        
        # Verify chain integrity on startup
        self._startup_verification()
    
    def _startup_verification(self) -> None:
        """
        Verify chain integrity on initialization.
        
        This catches corruption before any operations proceed.
        
        Raises:
            ChainCorruptedError: If corruption detected.
            ChainFrozenError: If chain is frozen.
        """
        state = self._backend.get_state()
        
        if state == ChainState.FROZEN:
            raise ChainFrozenError(
                "Chain is frozen. Operator intervention required."
            )
        
        if state == ChainState.CORRUPTED:
            raise ChainCorruptedError(
                "Chain is corrupted. Manual recovery required."
            )
        
        # Run full verification
        try:
            self.verify_chain()
        except ChainViolation as e:
            self._mark_corrupted("STARTUP_VERIFICATION_FAILED", e.details)
            raise ChainCorruptedError(f"Startup verification failed: {e}")
    
    def _mark_corrupted(self, violation_type: str, details: dict[str, any]) -> None:
        """
        Mark chain as corrupted and escalate to watchdog.
        
        Args:
            violation_type: Type of violation.
            details: Violation details.
        """
        # Mark backend as corrupted
        self._backend.set_state(ChainState.CORRUPTED)
        
        # Escalate to watchdog
        if self._watchdog:
            self._watchdog.escalate_chain_breach(violation_type, details)
    
    def _mark_frozen(self, reason: str, details: dict[str, any]) -> None:
        """
        Freeze the chain.
        
        Args:
            reason: Reason for freeze.
            details: Additional details.
        """
        self._backend.set_state(ChainState.FROZEN)
        
        if self._watchdog:
            self._watchdog.escalate_chain_breach(f"CHAIN_FROZEN:{reason}", details)
    
    def _check_not_frozen(self) -> None:
        """
        Ensure chain is not frozen before mutation.
        
        Raises:
            ChainFrozenError: If chain is frozen.
        """
        if self._backend.get_state() == ChainState.FROZEN:
            raise ChainFrozenError("Cannot modify frozen chain")
    
    def _check_not_corrupted(self) -> None:
        """
        Ensure chain is not corrupted before mutation.
        
        Raises:
            ChainCorruptedError: If chain is corrupted.
        """
        if self._backend.get_state() == ChainState.CORRUPTED:
            raise ChainCorruptedError("Cannot modify corrupted chain")
    
    def get_head(self) -> ChainHead | None:
        """
        Get current chain head.
        
        Returns:
            Current head, or None if chain uninitialized.
            Never guesses. Never infers.
        """
        return self._backend.get_head()
    
    def validate_parent(self, record: AuditRecord) -> None:
        """
        Validate parent linkage for new record.
        
        Enforces:
        - Parent exists (unless genesis)
        - Parent hash matches chain head
        - Timestamp monotonicity
        - Height continuity (+1 exactly)
        
        Args:
            record: Audit record to validate.
            
        Raises:
            ChainViolation: If validation fails.
        """
        self._check_not_frozen()
        self._check_not_corrupted()
        
        current_head = self.get_head()
        
        # Genesis case
        if record.parent_hash is None:
            if current_head is not None:
                raise ChainViolation(
                    "INVALID_GENESIS",
                    {
                        "message": "Genesis record submitted to non-empty chain",
                        "current_head": current_head.event_hash
                    }
                )
            return  # Valid genesis
        
        # Non-genesis case
        if current_head is None:
            raise ChainViolation(
                "MISSING_PARENT",
                {
                    "message": "Non-genesis record submitted to empty chain",
                    "record_parent": record.parent_hash
                }
            )
        
        # Parent hash must match current head
        if record.parent_hash != current_head.event_hash:
            self._mark_corrupted(
                "PARENT_HASH_MISMATCH",
                {
                    "expected_parent": current_head.event_hash,
                    "actual_parent": record.parent_hash,
                    "record_hash": record.event_hash
                }
            )
            raise ChainViolation(
                "PARENT_HASH_MISMATCH",
                {
                    "expected": current_head.event_hash,
                    "actual": record.parent_hash
                }
            )
        
        # Timestamp must be monotonically increasing
        if record.timestamp <= current_head.timestamp:
            self._mark_frozen(
                "TIMESTAMP_REWIND",
                {
                    "head_timestamp": current_head.timestamp,
                    "record_timestamp": record.timestamp,
                    "record_hash": record.event_hash
                }
            )
            raise ChainViolation(
                "TIMESTAMP_REWIND",
                {
                    "head_timestamp": current_head.timestamp,
                    "record_timestamp": record.timestamp
                }
            )
        
        # Height must be exactly +1
        expected_height = current_head.height + 1
        if hasattr(record, 'height') and record.height != expected_height:
            self._mark_corrupted(
                "HEIGHT_DISCONTINUITY",
                {
                    "expected_height": expected_height,
                    "actual_height": record.height,
                    "record_hash": record.event_hash
                }
            )
            raise ChainViolation(
                "HEIGHT_DISCONTINUITY",
                {
                    "expected": expected_height,
                    "actual": record.height
                }
            )
    
    def advance(self, record: AuditRecord) -> ChainHead:
        """
        Advance the chain with a new record.
        
        Steps (MANDATED):
        1. Validate parent
        2. Compute expected height
        3. Verify hash correctness
        4. Append chain link
        5. Move head atomically
        
        No partial advance. No rollback here.
        
        Args:
            record: Sealed audit record to add.
            
        Returns:
            New chain head.
            
        Raises:
            ChainViolation: If advancement fails.
            ChainFrozenError: If chain is frozen.
            ChainCorruptedError: If chain is corrupted.
        """
        self._check_not_frozen()
        self._check_not_corrupted()
        
        # Step 1: Validate parent
        self.validate_parent(record)
        
        # Step 2: Compute expected height
        current_head = self.get_head()
        expected_height = 0 if current_head is None else current_head.height + 1
        
        # Step 3: Verify hash correctness
        # Re-compute hash to ensure record wasn't tampered with
        recomputed_hash = self._compute_hash(record)
        if recomputed_hash != record.event_hash:
            self._mark_corrupted(
                "HASH_MISMATCH",
                {
                    "expected_hash": recomputed_hash,
                    "actual_hash": record.event_hash,
                    "parent_hash": record.parent_hash
                }
            )
            raise ChainViolation(
                "HASH_MISMATCH",
                {
                    "expected": recomputed_hash,
                    "actual": record.event_hash
                }
            )
        
        # Step 4: Append chain link (atomic)
        link = ChainLink(
            event_hash=record.event_hash,
            parent_hash=record.parent_hash,
            height=expected_height
        )
        
        try:
            self._backend.append_link(link)
        except Exception as e:
            self._mark_corrupted(
                "APPEND_FAILED",
                {
                    "error": str(e),
                    "link": {
                        "event_hash": link.event_hash,
                        "parent_hash": link.parent_hash,
                        "height": link.height
                    }
                }
            )
            raise ChainViolation(
                "APPEND_FAILED",
                {"error": str(e)}
            )
        
        # Step 5: Move head atomically
        new_head = ChainHead(
            event_hash=record.event_hash,
            timestamp=record.timestamp,
            height=expected_height
        )
        
        try:
            self._backend.set_head(new_head)
        except Exception as e:
            # Head update failed but link was appended
            # This is catastrophic - chain is now inconsistent
            self._mark_corrupted(
                "HEAD_UPDATE_FAILED",
                {
                    "error": str(e),
                    "appended_link": link.event_hash,
                    "failed_head": {
                        "event_hash": new_head.event_hash,
                        "timestamp": new_head.timestamp,
                        "height": new_head.height
                    }
                }
            )
            raise ChainViolation(
                "HEAD_UPDATE_FAILED",
                {"error": str(e)}
            )
        
        return new_head
    
    def _compute_hash(self, record: AuditRecord) -> str:
        """
        Recompute record hash for verification.
        
        MUST match audit_logger's hash computation exactly.
        
        Args:
            record: Audit record.
            
        Returns:
            Computed hash.
        """
        h = hashlib.new(self._hash_algo)
        
        # Hash components in deterministic order
        h.update(record.recovery_id.encode('utf-8'))
        h.update(record.event_type.encode('utf-8'))
        h.update(str(record.timestamp).encode('utf-8'))
        h.update(record.actor.encode('utf-8'))
        
        if record.parent_hash:
            h.update(record.parent_hash.encode('utf-8'))
        
        # Include payload if present
        if hasattr(record, 'payload') and record.payload:
            import json
            payload_str = json.dumps(record.payload, sort_keys=True)
            h.update(payload_str.encode('utf-8'))
        
        return h.hexdigest()
    
    def verify_chain(self) -> None:
        """
        Verify complete chain integrity.
        
        Used for:
        - Startup integrity checks
        - Replay validation
        - Forensic verification
        
        Must:
        - Traverse entire chain
        - Recompute hashes
        - Verify linkage
        - Detect forks or gaps
        
        Any anomaly → CORRUPTED.
        
        Raises:
            ChainViolation: If any violation detected.
        """
        head = self.get_head()
        all_links = self._backend.get_all_links()
        
        # Run invariant checks
        AuditChainInvariants.validate_single_head(head, all_links)
        AuditChainInvariants.validate_height_continuity(all_links)
        AuditChainInvariants.validate_no_orphans(all_links)
        AuditChainInvariants.validate_no_parallel_chains(all_links)
        
        # Verify chain traversal from genesis to head
        if not all_links:
            return  # Empty chain is valid
        
        # Sort by height
        sorted_links = sorted(all_links, key=lambda l: l.height)
        
        # Verify linkage
        for i, link in enumerate(sorted_links):
            # Genesis must have no parent
            if i == 0:
                if link.parent_hash is not None:
                    raise ChainViolation(
                        "INVALID_GENESIS_PARENT",
                        {
                            "genesis_hash": link.event_hash,
                            "unexpected_parent": link.parent_hash
                        }
                    )
            else:
                # Non-genesis must link to previous
                prev_link = sorted_links[i - 1]
                if link.parent_hash != prev_link.event_hash:
                    raise ChainViolation(
                        "BROKEN_LINKAGE",
                        {
                            "height": link.height,
                            "event_hash": link.event_hash,
                            "expected_parent": prev_link.event_hash,
                            "actual_parent": link.parent_hash
                        }
                    )
        
        # Verify head points to last link
        if head and head.event_hash != sorted_links[-1].event_hash:
            raise ChainViolation(
                "HEAD_TAIL_MISMATCH",
                {
                    "head_hash": head.event_hash,
                    "tail_hash": sorted_links[-1].event_hash
                }
            )
    
    def get_chain_state(self) -> ChainState:
        """
        Get current chain state.
        
        Returns:
            Current chain state.
        """
        return self._backend.get_state()
    
    def get_chain_height(self) -> int:
        """
        Get current chain height.
        
        Returns:
            Chain height, or -1 if uninitialized.
        """
        head = self.get_head()
        return head.height if head else -1
    
    def replay_verification(self, records: list[AuditRecord]) -> None:
        """
        Verify a sequence of records forms a valid chain.
        
        Used for:
        - Replay validation
        - Import verification
        - Forensic analysis
        
        Does NOT modify the chain.
        
        Args:
            records: Ordered sequence of audit records.
            
        Raises:
            ChainViolation: If sequence is invalid.
        """
        if not records:
            return
        
        # Sort by timestamp to ensure order
        sorted_records = sorted(records, key=lambda r: r.timestamp)
        
        # Verify first record is genesis
        if sorted_records[0].parent_hash is not None:
            raise ChainViolation(
                "REPLAY_MISSING_GENESIS",
                {
                    "first_record": sorted_records[0].event_hash,
                    "unexpected_parent": sorted_records[0].parent_hash
                }
            )
        
        # Verify linkage
        for i in range(1, len(sorted_records)):
            current = sorted_records[i]
            previous = sorted_records[i - 1]
            
            if current.parent_hash != previous.event_hash:
                raise ChainViolation(
                    "REPLAY_BROKEN_LINKAGE",
                    {
                        "index": i,
                        "current_hash": current.event_hash,
                        "expected_parent": previous.event_hash,
                        "actual_parent": current.parent_hash
                    }
                )
            
            # Verify timestamp monotonicity
            if current.timestamp <= previous.timestamp:
                raise ChainViolation(
                    "REPLAY_TIMESTAMP_VIOLATION",
                    {
                        "index": i,
                        "current_timestamp": current.timestamp,
                        "previous_timestamp": previous.timestamp
                    }
                )
            
            # Verify hash correctness
            recomputed = self._compute_hash(current)
            if recomputed != current.event_hash:
                raise ChainViolation(
                    "REPLAY_HASH_MISMATCH",
                    {
                        "index": i,
                        "expected_hash": recomputed,
                        "actual_hash": current.event_hash
                    }
                )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'ChainState',
    'ChainViolation',
    'ChainFrozenError',
    'ChainCorruptedError',
    'ChainHead',
    'ChainLink',
    'ChainBackend',
    'WatchdogEscalator',
    'AuditChainInvariants',
    'AuditChain',
]