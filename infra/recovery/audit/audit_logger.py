"""
/infra/recovery/audit/audit_logger.py

Append-Only Recovery Audit Recorder

This module is the ONLY mechanism allowed to persist recovery actions.
It answers: "Did this recovery step truly happen — and can we prove it later?"

If this logger refuses to write, the recovery step must not proceed.

This is not observability. This is truth preservation.

WHAT THIS FILE IS:
- Schema validator
- Cryptographic sealer
- Immutable appender
- Hash chain linker
- Failure enforcer
- Watchdog integrator

WHAT THIS FILE IS NOT:
- Metrics logger
- Debugging aid
- Best-effort logging
- Mutable
- Async-fire-and-forget
- Analytics

Design Principle:
Recovery without auditability is corruption.
This logger exists to make corruption obvious and expensive.

Authority chain: recovery_action → audit_logger → audit_chain → immutable_store
Audit logging sits in the execution path, not beside it.
"""

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Any, Dict

from .audit_models import AuditRecord, AuditEventType
from .audit_chain import AuditChain, ChainViolation, ChainFrozenError, ChainCorruptedError


# ============================================================================
# CORE ENUMS
# ============================================================================


class AuditWriteResult(Enum):
    """
    Write operation outcomes.
    
    No "retry later". No "maybe". Only binary results.
    """
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FATAL = "fatal"


# ============================================================================
# EXCEPTIONS
# ============================================================================


class AuditWriteError(Exception):
    """Base exception for audit write failures."""
    def __init__(self, result: AuditWriteResult, reason: str, details: dict[str, Any] | None = None):
        self.result = result
        self.reason = reason
        self.details = details or {}
        super().__init__(f"Audit write {result.value}: {reason}")


class AuditValidationError(AuditWriteError):
    """Raised when record fails validation."""
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(AuditWriteResult.REJECTED, reason, details)


class AuditSealingError(AuditWriteError):
    """Raised when record sealing fails."""
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(AuditWriteResult.FATAL, reason, details)


class AuditAppendError(AuditWriteError):
    """Raised when append operation fails."""
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(AuditWriteResult.FATAL, reason, details)


class AuditFrozenError(AuditWriteError):
    """Raised when writes blocked by watchdog freeze."""
    def __init__(self, reason: str = "Audit system frozen by watchdog"):
        super().__init__(AuditWriteResult.REJECTED, reason)


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class AuditLoggerConfig:
    """
    Immutable configuration for audit logger.
    
    Loaded at process start only. No runtime modification.
    """
    storage_backend: str
    hash_algorithm: str
    require_parent_hash: bool
    fail_closed: bool  # If true, halt on any failure
    schema_version: str
    enable_fsync: bool = True  # Force durability guarantee
    max_record_size_bytes: int = 1_048_576  # 1MB default limit
    
    def __post_init__(self):
        """Validate configuration."""
        if self.hash_algorithm not in hashlib.algorithms_available:
            raise ValueError(f"Unsupported hash algorithm: {self.hash_algorithm}")
        
        if not self.schema_version:
            raise ValueError("schema_version is required")
        
        if self.max_record_size_bytes <= 0:
            raise ValueError("max_record_size_bytes must be positive")


@dataclass(frozen=True)
class AuditWriteReceipt:
    """
    Proof of successful audit write.
    
    Receipts are proof — not acknowledgments.
    Immutable. Cryptographically verifiable.
    """
    event_hash: str
    parent_event_hash: str | None
    timestamp: int
    storage_offset: str  # Backend-specific location identifier
    height: int  # Position in chain
    
    def __post_init__(self):
        """Validate receipt."""
        if not self.event_hash:
            raise ValueError("event_hash cannot be empty")
        if self.timestamp <= 0:
            raise ValueError("Invalid timestamp")
        if self.height < 0:
            raise ValueError("Invalid height")


# ============================================================================
# BACKEND PROTOCOL
# ============================================================================


class AuditStorageBackend(Protocol):
    """
    Minimal interface for audit storage.
    
    Backend MUST provide:
    - Append-only semantics
    - Durability guarantees
    - Atomic writes
    - No mutation capability
    """
    
    def append(self, record: AuditRecord, require_fsync: bool = True) -> str:
        """
        Append sealed record to immutable storage.
        
        Args:
            record: Sealed audit record.
            require_fsync: Force durability guarantee.
            
        Returns:
            Storage offset/identifier.
            
        Raises:
            Exception on failure (backend-specific).
        """
        ...
    
    def get(self, event_hash: str) -> AuditRecord | None:
        """
        Retrieve record by event hash.
        
        Args:
            event_hash: Hash of event to retrieve.
            
        Returns:
            Record if found, None otherwise.
        """
        ...
    
    def exists(self, event_hash: str) -> bool:
        """
        Check if record exists.
        
        Args:
            event_hash: Hash to check.
            
        Returns:
            True if record exists.
        """
        ...
    
    def verify_integrity(self) -> bool:
        """
        Verify backend integrity.
        
        Returns:
            True if backend is intact and uncorrupted.
        """
        ...


# ============================================================================
# WATCHDOG INTEGRATION
# ============================================================================


class WatchdogAuthority(Protocol):
    """
    Interface to watchdog enforcement system.
    
    Watchdog has ultimate authority over audit operations.
    """
    
    def is_frozen(self) -> bool:
        """
        Check if system is frozen.
        
        Returns:
            True if writes should be blocked.
        """
        ...
    
    def authorize_write(self, record: AuditRecord) -> bool:
        """
        Check if write is authorized.
        
        Args:
            record: Record to authorize.
            
        Returns:
            True if authorized.
        """
        ...
    
    def report_write_failure(
        self,
        record: AuditRecord,
        error: AuditWriteError
    ) -> None:
        """
        Report write failure to watchdog.
        
        Args:
            record: Failed record.
            error: Failure details.
        """
        ...
    
    def report_write_success(
        self,
        receipt: AuditWriteReceipt
    ) -> None:
        """
        Report successful write.
        
        Args:
            receipt: Write receipt.
        """
        ...


# ============================================================================
# AUDIT LOGGER INVARIANTS
# ============================================================================


class AuditLoggerInvariants:
    """
    Absolute invariants for audit logging.
    
    These are mathematical truths enforced at runtime.
    Violation → system-wide hard stop.
    """
    
    @staticmethod
    def validate_append_only(record: AuditRecord, backend: AuditStorageBackend) -> None:
        """
        Enforce append-only semantics.
        
        Args:
            record: Record to validate.
            backend: Storage backend.
            
        Raises:
            AuditValidationError: If record already exists.
        """
        if backend.exists(record.event_hash):
            raise AuditValidationError(
                "DUPLICATE_EVENT",
                {
                    "event_hash": record.event_hash,
                    "message": "Record already exists - append-only violation"
                }
            )
    
    @staticmethod
    def validate_timestamp_monotonicity(
        record: AuditRecord,
        chain: AuditChain
    ) -> None:
        """
        Enforce strictly increasing timestamps.
        
        Args:
            record: Record to validate.
            chain: Audit chain.
            
        Raises:
            AuditValidationError: If timestamp violates monotonicity.
        """
        head = chain.get_head()
        if head and record.timestamp <= head.timestamp:
            raise AuditValidationError(
                "TIMESTAMP_NOT_MONOTONIC",
                {
                    "record_timestamp": record.timestamp,
                    "head_timestamp": head.timestamp,
                    "message": "Timestamps must be strictly increasing"
                }
            )
    
    @staticmethod
    def validate_parent_chain(
        record: AuditRecord,
        chain: AuditChain,
        require_parent: bool
    ) -> None:
        """
        Validate parent hash chain linkage.
        
        Args:
            record: Record to validate.
            chain: Audit chain.
            require_parent: Whether parent is required.
            
        Raises:
            AuditValidationError: If parent validation fails.
        """
        head = chain.get_head()
        
        # Genesis case
        if head is None:
            if require_parent and record.parent_hash is not None:
                raise AuditValidationError(
                    "INVALID_GENESIS",
                    {
                        "message": "First record must not have parent_hash",
                        "parent_hash": record.parent_hash
                    }
                )
            return
        
        # Non-genesis case
        if require_parent and record.parent_hash is None:
            raise AuditValidationError(
                "MISSING_PARENT_HASH",
                {
                    "message": "Non-genesis record requires parent_hash",
                    "head_hash": head.event_hash
                }
            )
        
        if record.parent_hash and record.parent_hash != head.event_hash:
            raise AuditValidationError(
                "PARENT_HASH_MISMATCH",
                {
                    "expected": head.event_hash,
                    "actual": record.parent_hash,
                    "message": "Parent hash must match current chain head"
                }
            )
    
    @staticmethod
    def validate_immutability(record: AuditRecord) -> None:
        """
        Ensure record is fully sealed and immutable.
        
        Args:
            record: Record to validate.
            
        Raises:
            AuditValidationError: If record is mutable.
        """
        # Check required fields are populated
        if not record.event_hash:
            raise AuditValidationError(
                "UNSEALED_RECORD",
                {"message": "Record missing event_hash - not sealed"}
            )
        
        if not record.recovery_id:
            raise AuditValidationError(
                "MISSING_RECOVERY_ID",
                {"message": "Record missing recovery_id"}
            )
        
        if not record.actor:
            raise AuditValidationError(
                "MISSING_ACTOR",
                {"message": "Record missing actor"}
            )
        
        # Verify record is frozen (if using frozen dataclass)
        if hasattr(record, '__dataclass_fields__'):
            if not record.__dataclass_fields__.get('__frozen__', False):
                # Check if actually frozen by attempting mutation
                try:
                    object.__setattr__(record, '_test', None)
                    raise AuditValidationError(
                        "MUTABLE_RECORD",
                        {"message": "Record is not frozen - immutability violated"}
                    )
                except (AttributeError, TypeError):
                    # Good - record is frozen
                    pass
    
    @staticmethod
    def validate_no_unaudited_calls() -> None:
        """
        Placeholder for runtime enforcement.
        
        In production, this would verify that recovery operations
        cannot proceed without passing through audit_logger.
        
        Implementation depends on execution framework.
        """
        pass
    
    @staticmethod
    def validate_schema_version(
        record: AuditRecord,
        expected_version: str
    ) -> None:
        """
        Validate schema version compatibility.
        
        Args:
            record: Record to validate.
            expected_version: Expected schema version.
            
        Raises:
            AuditValidationError: If schema version mismatch.
        """
        if not hasattr(record, 'schema_version'):
            raise AuditValidationError(
                "MISSING_SCHEMA_VERSION",
                {"message": "Record missing schema_version"}
            )
        
        if record.schema_version != expected_version:
            raise AuditValidationError(
                "SCHEMA_VERSION_MISMATCH",
                {
                    "expected": expected_version,
                    "actual": record.schema_version,
                    "message": "Schema version incompatible"
                }
            )
    
    @staticmethod
    def validate_size_limit(
        record: AuditRecord,
        max_size_bytes: int
    ) -> None:
        """
        Enforce maximum record size.
        
        Args:
            record: Record to validate.
            max_size_bytes: Maximum allowed size.
            
        Raises:
            AuditValidationError: If record exceeds size limit.
        """
        # Serialize to measure size
        serialized = json.dumps(record.__dict__, sort_keys=True)
        size_bytes = len(serialized.encode('utf-8'))
        
        if size_bytes > max_size_bytes:
            raise AuditValidationError(
                "RECORD_TOO_LARGE",
                {
                    "size_bytes": size_bytes,
                    "max_size_bytes": max_size_bytes,
                    "message": f"Record exceeds size limit"
                }
            )


# ============================================================================
# AUDIT LOGGER (THE AUTHORITY)
# ============================================================================


class AuditLogger:
    """
    The ONLY mechanism allowed to persist recovery actions.
    
    This is a court stenographer:
    - Writes exactly what happened
    - Never summarizes
    - Never edits
    - Never forgives
    - Never forgets
    
    If audit logging succeeds but recovery fails → acceptable.
    If recovery succeeds but audit logging fails → FORBIDDEN.
    """
    
    def __init__(
        self,
        config: AuditLoggerConfig,
        backend: AuditStorageBackend,
        chain: AuditChain,
        watchdog: WatchdogAuthority | None = None
    ):
        """
        Initialize audit logger.
        
        Args:
            config: Immutable configuration.
            backend: Append-only storage backend.
            chain: Audit chain for linkage.
            watchdog: Optional watchdog authority.
        """
        self._config = config
        self._backend = backend
        self._chain = chain
        self._watchdog = watchdog
        
        # Verify backend integrity on startup
        if not self._backend.verify_integrity():
            raise RuntimeError("Backend integrity check failed - cannot initialize audit logger")
        
        # Pre-compute hash function for performance
        self._hasher_factory = lambda: hashlib.new(config.hash_algorithm)
    
    def record(self, record: AuditRecord) -> AuditWriteReceipt:
        """
        Record an audit event.
        
        This is the ONLY public method.
        
        Execution phases (MANDATED ORDER):
        1. Validation (NO REPAIR)
        2. Sealing (CRYPTOGRAPHIC)
        3. Append (IMMUTABLE)
        
        Args:
            record: Audit record to persist.
            
        Returns:
            Write receipt as proof of persistence.
            
        Raises:
            AuditWriteError: On any failure.
            AuditFrozenError: If system is frozen.
        """
        try:
            # PHASE 1: VALIDATION
            self._validate(record)
            
            # PHASE 2: SEALING
            sealed_record = self._seal(record)
            
            # PHASE 3: APPEND
            receipt = self._append(sealed_record)
            
            # Report success to watchdog
            if self._watchdog:
                self._watchdog.report_write_success(receipt)
            
            return receipt
            
        except AuditWriteError as e:
            # Report failure to watchdog
            if self._watchdog:
                self._watchdog.report_write_failure(record, e)
            
            # Fail closed if configured
            if self._config.fail_closed and e.result == AuditWriteResult.FATAL:
                # In production, this would trigger system-wide halt
                raise RuntimeError(f"FATAL audit error in fail-closed mode: {e}")
            
            raise
    
    def _validate(self, record: AuditRecord) -> None:
        """
        Phase 1: Validation (NO REPAIR).
        
        Must verify:
        - Schema version
        - Immutability
        - Invariant compliance
        - Watchdog permissions
        - Size limits
        - Chain consistency
        
        Failure → hard reject.
        
        Args:
            record: Record to validate.
            
        Raises:
            AuditValidationError: If validation fails.
            AuditFrozenError: If system is frozen.
        """
        # Check watchdog freeze
        if self._watchdog and self._watchdog.is_frozen():
            raise AuditFrozenError()
        
        # Check watchdog authorization
        if self._watchdog and not self._watchdog.authorize_write(record):
            raise AuditValidationError(
                "WRITE_NOT_AUTHORIZED",
                {
                    "recovery_id": record.recovery_id,
                    "actor": record.actor,
                    "message": "Watchdog denied write authorization"
                }
            )
        
        # Schema version
        AuditLoggerInvariants.validate_schema_version(
            record,
            self._config.schema_version
        )
        
        # Size limit
        AuditLoggerInvariants.validate_size_limit(
            record,
            self._config.max_record_size_bytes
        )
        
        # Immutability (for already-sealed records)
        if record.event_hash:
            AuditLoggerInvariants.validate_immutability(record)
        
        # Append-only (no duplicates)
        if record.event_hash:
            AuditLoggerInvariants.validate_append_only(record, self._backend)
        
        # Timestamp monotonicity
        AuditLoggerInvariants.validate_timestamp_monotonicity(record, self._chain)
        
        # Parent chain linkage
        AuditLoggerInvariants.validate_parent_chain(
            record,
            self._chain,
            self._config.require_parent_hash
        )
    
    def _seal(self, record: AuditRecord) -> AuditRecord:
        """
        Phase 2: Sealing (CRYPTOGRAPHIC).
        
        Must:
        - Compute canonical serialization
        - Hash content
        - Link parent hash
        - Populate integrity fields
        - Refuse overwrite
        
        Logger does not invent values — only seals.
        
        Args:
            record: Record to seal.
            
        Returns:
            Sealed record with integrity fields populated.
            
        Raises:
            AuditSealingError: If sealing fails.
        """
        # If already sealed, verify integrity
        if record.event_hash:
            recomputed_hash = self._compute_hash(record)
            if recomputed_hash != record.event_hash:
                raise AuditSealingError(
                    "HASH_MISMATCH",
                    {
                        "expected": recomputed_hash,
                        "actual": record.event_hash,
                        "message": "Pre-sealed record hash does not match"
                    }
                )
            return record  # Already sealed correctly
        
        # Populate parent hash if not set
        parent_hash = record.parent_hash
        if parent_hash is None:
            head = self._chain.get_head()
            if head is not None:
                parent_hash = head.event_hash
        
        # Populate timestamp if not set
        timestamp = record.timestamp
        if timestamp == 0:
            timestamp = self._get_monotonic_timestamp()
        
        # Create sealed copy with integrity fields
        sealed_dict = {
            **record.__dict__,
            'parent_hash': parent_hash,
            'timestamp': timestamp
        }
        
        # Compute hash
        event_hash = self._compute_hash_from_dict(sealed_dict)
        sealed_dict['event_hash'] = event_hash
        
        # Create new sealed record
        sealed_record = type(record)(**sealed_dict)
        
        return sealed_record
    
    def _append(self, sealed_record: AuditRecord) -> AuditWriteReceipt:
        """
        Phase 3: Append (IMMUTABLE).
        
        Rules:
        - Append-only backend
        - No updates
        - No deletes
        - No compaction here
        - fsync / durability guarantee required
        
        If append fails → recovery must not continue.
        
        Args:
            sealed_record: Sealed record to append.
            
        Returns:
            Write receipt.
            
        Raises:
            AuditAppendError: If append fails.
        """
        try:
            # Advance chain (validates parent, updates head)
            new_head = self._chain.advance(sealed_record)
            
        except (ChainViolation, ChainFrozenError, ChainCorruptedError) as e:
            raise AuditAppendError(
                "CHAIN_ADVANCEMENT_FAILED",
                {
                    "error": str(e),
                    "event_hash": sealed_record.event_hash,
                    "message": "Failed to advance audit chain"
                }
            )
        
        try:
            # Append to backend storage
            storage_offset = self._backend.append(
                sealed_record,
                require_fsync=self._config.enable_fsync
            )
            
        except Exception as e:
            # Backend append failed but chain was advanced
            # This is catastrophic - chain and storage are now inconsistent
            raise AuditAppendError(
                "BACKEND_APPEND_FAILED",
                {
                    "error": str(e),
                    "event_hash": sealed_record.event_hash,
                    "chain_height": new_head.height,
                    "message": "CRITICAL: Chain advanced but backend append failed"
                }
            )
        
        # Create receipt
        receipt = AuditWriteReceipt(
            event_hash=sealed_record.event_hash,
            parent_event_hash=sealed_record.parent_hash,
            timestamp=sealed_record.timestamp,
            storage_offset=storage_offset,
            height=new_head.height
        )
        
        return receipt
    
    def _compute_hash(self, record: AuditRecord) -> str:
        """
        Compute cryptographic hash of record.
        
        MUST be deterministic and canonical.
        MUST match chain's hash computation.
        
        Args:
            record: Record to hash.
            
        Returns:
            Hex-encoded hash.
        """
        return self._compute_hash_from_dict(record.__dict__)
    
    def _compute_hash_from_dict(self, record_dict: dict[str, Any]) -> str:
        """
        Compute hash from record dictionary.
        
        Uses canonical JSON serialization for determinism.
        
        Args:
            record_dict: Record data.
            
        Returns:
            Hex-encoded hash.
        """
        hasher = self._hasher_factory()
        
        # Create canonical representation
        # Sort keys for determinism
        canonical = {
            'recovery_id': record_dict.get('recovery_id', ''),
            'event_type': record_dict.get('event_type', ''),
            'timestamp': record_dict.get('timestamp', 0),
            'actor': record_dict.get('actor', ''),
            'parent_hash': record_dict.get('parent_hash'),
        }
        
        # Add payload if present
        if 'payload' in record_dict and record_dict['payload']:
            canonical['payload'] = record_dict['payload']
        
        # Serialize canonically
        serialized = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        
        # Hash
        hasher.update(serialized.encode('utf-8'))
        
        return hasher.hexdigest()
    
    def _get_monotonic_timestamp(self) -> int:
        """
        Get monotonically increasing timestamp.
        
        Ensures timestamp is always > previous head timestamp.
        
        Returns:
            Monotonic timestamp (nanoseconds since epoch).
        """
        current_ns = time.time_ns()
        
        head = self._chain.get_head()
        if head:
            # Ensure strictly greater than head
            if current_ns <= head.timestamp:
                current_ns = head.timestamp + 1
        
        return current_ns
    
    def get_config(self) -> AuditLoggerConfig:
        """
        Get logger configuration.
        
        Returns:
            Immutable config.
        """
        return self._config
    
    def verify_record(self, record: AuditRecord) -> bool:
        """
        Verify record integrity without writing.
        
        Useful for forensic verification.
        
        Args:
            record: Record to verify.
            
        Returns:
            True if record is valid and sealed correctly.
        """
        if not record.event_hash:
            return False
        
        try:
            recomputed = self._compute_hash(record)
            return recomputed == record.event_hash
        except Exception:
            return False


# ============================================================================
# CONVENIENCE FACTORY
# ============================================================================


def create_audit_logger(
    config: AuditLoggerConfig,
    backend: AuditStorageBackend,
    chain: AuditChain,
    watchdog: WatchdogAuthority | None = None
) -> AuditLogger:
    """
    Factory function for creating audit logger.
    
    Args:
        config: Logger configuration.
        backend: Storage backend.
        chain: Audit chain.
        watchdog: Optional watchdog authority.
        
    Returns:
        Configured audit logger.
    """
    return AuditLogger(
        config=config,
        backend=backend,
        chain=chain,
        watchdog=watchdog
    )


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'AuditWriteResult',
    'AuditWriteError',
    'AuditValidationError',
    'AuditSealingError',
    'AuditAppendError',
    'AuditFrozenError',
    'AuditLoggerConfig',
    'AuditWriteReceipt',
    'AuditStorageBackend',
    'WatchdogAuthority',
    'AuditLoggerInvariants',
    'AuditLogger',
    'create_audit_logger',
]