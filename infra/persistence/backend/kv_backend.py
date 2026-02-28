"""
Deterministic Key-Value Storage Backend Contract
(Replay-Critical Infrastructure Authority)

================================================================================
WHAT THIS FILE EXISTS FOR (NON-NEGOTIABLE)
================================================================================

kv_backend.py defines the canonical interface and guarantees for all key-value
storage used in the system.

It does NOT implement storage.

It defines:
- Deterministic read semantics
- Atomic write guarantees
- Idempotent mutation behavior
- Namespacing rules
- Versioning and CAS contracts
- Failure handling semantics

This file is the single authority between:
    System logic ↔ Physical storage engine

If this contract is ambiguous, replay breaks.
If this contract is weak, corruption spreads silently.

================================================================================
SEVERITY OF FAILURE IF WRONG
================================================================================

If this file is wrong:
- Snapshot rollback corrupts silently
- Replay diverges undetectably
- Counters drift across replicas
- Deterministic audits become impossible

This file is infrastructure-critical for 500k+ LOC production systems.

================================================================================
ARCHITECTURAL POSITION
================================================================================

Layer stack:
    Application Logic
          ↓
    Persistence API
          ↓
    kv_backend.py  ← THIS FILE (contract only)
          ↓
    Concrete backend (Redis / RocksDB / DynamoDB / Postgres / InMemory)
          ↓
    Physical storage

This file contains:
- Abstract base classes only
- Strict behavioral contracts
- NO environment-specific logic
- NO serialization logic
- NO business logic

================================================================================
KEY ADDRESSING REQUIREMENTS
================================================================================

Keys must be:
- Fully qualified
- Explicitly namespaced
- Byte-safe
- Stable across replays

Required format validation:
- Type: str
- No whitespace
- Maximum length: MAX_KEY_LENGTH (2048)
- ASCII-safe characters only

Example canonical format:
    {namespace}:{domain}:{entity_id}:{version?}

Key validation is MANDATORY before any storage operation.

================================================================================
DETERMINISM REQUIREMENTS
================================================================================

READ CONSISTENCY:
- get() must return last committed value
- No eventual-read ambiguity during replay
- Backend MUST provide read-after-write consistency
- If underlying engine cannot: force blocking semantics or declare UNSUPPORTED

WRITE SEMANTICS:
- All writes MUST be atomic
- Fully durable (depending on durability mode)
- Isolation-safe

Writes MUST NOT silently:
- Partial-write
- Truncate
- Downgrade versions

VERSIONING:
- If version supplied to set(): backend MUST store as metadata
- Future CAS operations MUST validate version
- Version numbers MUST be strictly monotonic per key
- If version omitted: versionless mode (CAS fails deterministically)

================================================================================
IDEMPOTENCY RULES
================================================================================

Backend MUST support:
- Repeated identical set(key, value, version=X) calls without altering state
- No hidden version increments
- Idempotent replays MUST produce identical storage state hashes

================================================================================
TTL BEHAVIOR
================================================================================

TTL is advisory and MUST NOT break replay correctness.

If backend supports TTL:
- Expiration must be metadata-visible
- Expired keys must appear nonexistent

During replay:
- TTL must either be frozen OR disabled
- Backend must declare TTL handling mode

================================================================================
REPLAY SAFETY REQUIREMENTS
================================================================================

During deterministic replay mode:
- All non-deterministic storage backends are PROHIBITED
- In-memory backend MUST:
  * Preserve insertion order
  * Use deterministic hashing
  * Disable random eviction

Backend implementations MUST declare supported replay safety level.

================================================================================
STORAGE INTEGRITY GUARANTEES
================================================================================

Backends MUST guarantee:
- No silent key overwrites unless explicitly allowed
- No mutation of stored byte payload
- No automatic transformation (compression allowed if reversible)

================================================================================
THREADING & CONCURRENCY CONTRACT
================================================================================

Backends MUST document:
- Thread-safe: Yes/No
- Process-safe: Yes/No
- Transaction-safe: Yes/No

If not safe: MUST explicitly state limitation.
No implicit locking in abstract layer.

================================================================================
FAILURE PHILOSOPHY
================================================================================

Storage failures are:
- Immediate (fail fast, no silent retries)
- Explicit (typed exceptions, no boolean error returns)
- Fatal unless explicitly handled upstream

No retry logic here. Retries belong in orchestration layer.

CAS operations return False only for version mismatch.
All other failures raise exceptions.

================================================================================
PROHIBITED BEHAVIORS
================================================================================

This file must NEVER:
- Import Redis
- Import DynamoDB
- Import database drivers
- Contain serialization logic
- Contain caching logic
- Reference application domains

It is pure contract.

================================================================================
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Mapping, Any, List, Tuple
from dataclasses import dataclass


# ============================================================================
# Constants
# ============================================================================

# Maximum key length in characters
MAX_KEY_LENGTH = 2048

# Maximum value size in bytes (100 MB)
MAX_VALUE_SIZE = 100 * 1024 * 1024


# ============================================================================
# Replay Safety Mode
# ============================================================================

class ReplaySafetyMode(Enum):
    """
    Replay safety level supported by backend.
    
    Backends must declare which level they provide.
    """
    STRICT = "strict"              # Full deterministic replay guarantees
    BEST_EFFORT = "best_effort"    # Attempts determinism, may have edge cases
    NONE = "none"                  # No replay safety (prohibited in replay mode)


# ============================================================================
# Durability Mode
# ============================================================================

class DurabilityMode(Enum):
    """
    Durability guarantee level for writes.
    
    Controls fsync/commit behavior.
    """
    STRONG = "strong"      # Synchronous disk/commit durability
    EVENTUAL = "eventual"  # Buffered with eventual commit
    EPHEMERAL = "ephemeral"  # No crash guarantees (testing only)


# ============================================================================
# TTL Handling Mode
# ============================================================================

class TTLHandlingMode(Enum):
    """
    How backend handles TTL during replay.
    """
    FROZEN = "frozen"      # TTL frozen during replay
    DISABLED = "disabled"  # TTL disabled during replay
    ACTIVE = "active"      # TTL active (may break replay determinism)
    UNSUPPORTED = "unsupported"  # Backend doesn't support TTL


# ============================================================================
# Error Taxonomy
# ============================================================================

class KVBackendError(Exception):
    """
    Base exception for all KV backend errors.
    
    All backend implementations MUST raise only exceptions derived from
    this base class. No silent failures allowed.
    """
    pass


class KeyValidationError(KVBackendError):
    """
    Key format or content is invalid.
    
    Examples:
    - Empty key
    - Whitespace in key
    - Key exceeds MAX_KEY_LENGTH
    - Non-ASCII characters
    """
    def __init__(self, key: str, reason: str):
        super().__init__(f"Invalid key '{key}': {reason}")
        self.key = key
        self.reason = reason


class ValueValidationError(KVBackendError):
    """
    Value format or content is invalid.
    
    Examples:
    - Value exceeds MAX_VALUE_SIZE
    - Value not bytes type
    """
    def __init__(self, reason: str):
        super().__init__(f"Invalid value: {reason}")
        self.reason = reason


class VersionConflictError(KVBackendError):
    """
    Version mismatch in compare-and-swap operation.
    
    This is NOT a fatal error - indicates concurrent modification.
    CAS operations should catch this and retry if appropriate.
    """
    def __init__(self, key: str, expected: int, actual: Optional[int]):
        actual_str = str(actual) if actual is not None else "none"
        super().__init__(
            f"Version conflict on key '{key}': expected {expected}, actual {actual_str}"
        )
        self.key = key
        self.expected = expected
        self.actual = actual


class AtomicityViolationError(KVBackendError):
    """
    Backend detected atomicity violation.
    
    This is a CRITICAL error indicating:
    - Partial write detected
    - Corrupt state detected
    - Transaction integrity failure
    
    Recovery may require manual intervention.
    """
    def __init__(self, key: str, reason: str):
        super().__init__(f"Atomicity violation for key '{key}': {reason}")
        self.key = key
        self.reason = reason


class UnsupportedOperationError(KVBackendError):
    """
    Operation not supported by this backend.
    
    Examples:
    - CAS on backend without versioning support
    - TTL on backend without expiration support
    - Transactions on non-transactional backend
    """
    def __init__(self, operation: str, backend_type: str):
        super().__init__(
            f"Operation '{operation}' not supported by backend type '{backend_type}'"
        )
        self.operation = operation
        self.backend_type = backend_type


class StorageExhaustedError(KVBackendError):
    """
    Storage capacity exceeded.
    
    Backend cannot accept more data.
    """
    def __init__(self, reason: str):
        super().__init__(f"Storage exhausted: {reason}")
        self.reason = reason


# ============================================================================
# Backend Capabilities
# ============================================================================

@dataclass(frozen=True)
class KVBackendCapabilities:
    """
    Explicit declaration of backend capabilities.
    
    Every backend implementation MUST provide this via get_capabilities().
    """
    # Replay safety level
    replay_safety: ReplaySafetyMode
    
    # Durability level
    durability: DurabilityMode
    
    # Feature support
    supports_versioning: bool
    supports_cas: bool
    supports_ttl: bool
    supports_transactions: bool
    supports_batching: bool
    
    # TTL handling during replay
    ttl_handling: TTLHandlingMode
    
    # Concurrency safety
    thread_safe: bool
    process_safe: bool
    distributed_safe: bool
    
    # Performance characteristics
    supports_range_queries: bool
    supports_prefix_scan: bool
    
    def validate(self) -> None:
        """
        Validate capability consistency.
        
        Raises:
            ValueError: If capabilities are inconsistent
        """
        if self.supports_cas and not self.supports_versioning:
            raise ValueError("CAS requires versioning support")
        
        if self.supports_ttl and self.ttl_handling == TTLHandlingMode.UNSUPPORTED:
            raise ValueError("Backend claims TTL support but ttl_handling is UNSUPPORTED")
        
        if not self.supports_ttl and self.ttl_handling != TTLHandlingMode.UNSUPPORTED:
            raise ValueError("Backend doesn't support TTL but declares handling mode")


# ============================================================================
# KV Backend Contract
# ============================================================================

class KVBackend(ABC):
    """
    Authoritative abstract base class for all key-value backends.
    
    This contract defines the formal interface for deterministic,
    replay-safe key-value storage.
    
    CONTRACT VIOLATIONS:
    
    Any implementation that:
    - Silently fails operations
    - Mutates stored values
    - Provides non-deterministic reads
    - Violates atomicity guarantees
    - Raises non-KVBackendError exceptions
    
    ...is in violation of this contract and MUST be rejected.
    """
    
    # ========================================================================
    # Core Operations
    # ========================================================================
    
    @abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        """
        Retrieve value for key.
        
        This is the primary read operation. All reads must go through this method.
        
        GUARANTEES:
        - Returns last committed value for this key
        - Returns None if key does not exist (NEVER raises exception for missing key)
        - Returns None if key expired (TTL)
        - No mutation of stored bytes (returns copy, not reference)
        - Read-after-write consistent (after successful set(), get() returns value)
        
        READ CONSISTENCY:
        - get() MUST return last committed value
        - No eventual-read ambiguity during replay
        - Backend MUST provide read-after-write consistency
        - If underlying engine cannot: force blocking semantics or declare UNSUPPORTED
        
        DETERMINISM:
        - Multiple calls with same key return same value (unless modified)
        - No time-based variation in result
        - No random variation in result
        - Replay correctness depends on this
        
        Args:
            key: Storage key (validated format, must pass validate_key())
            
        Returns:
            Raw bytes value if key exists and not expired, None otherwise
            
        Raises:
            KeyValidationError: If key format is invalid
            KVBackendError: On storage failure (not for missing keys)
        """
        pass
    
    @abstractmethod
    def set(
        self,
        key: str,
        value: bytes,
        *,
        version: Optional[int] = None,
        ttl_seconds: Optional[int] = None
    ) -> None:
        """
        Write value to key with optional version and TTL.
        
        This is the primary write operation. All writes must go through this method.
        
        GUARANTEES:
        - Atomic write (all-or-nothing at key level)
        - Durable according to backend's durability mode
        - Idempotent: repeated identical calls produce identical state
        - No silent partial writes
        - No silent truncation
        - No silent version downgrades
        
        WRITE SEMANTICS:
        - All writes MUST be atomic
        - Fully durable (depending on durability mode)
        - Isolation-safe (no interference from concurrent operations)
        - Writes MUST NOT silently partial-write, truncate, or downgrade versions
        
        VERSIONING:
        - If version provided: stored as metadata for CAS operations
        - Version must be strictly monotonic per key (each version > previous)
        - If version omitted: versionless mode (CAS will fail deterministically)
        - Backend MUST store version as metadata if provided
        - Future CAS operations MUST validate version
        
        TTL BEHAVIOR:
        - TTL is advisory and MUST NOT break replay correctness
        - If ttl_seconds provided: key expires after duration
        - TTL handling during replay determined by backend's ttl_handling mode:
          * FROZEN: TTL frozen during replay (no expiration)
          * DISABLED: TTL disabled during replay (no expiration)
          * ACTIVE: TTL active (may break replay determinism - not recommended)
        - Expired keys appear as non-existent (get() returns None, exists() returns False)
        - Expiration must be metadata-visible
        
        IDEMPOTENCY RULES:
        - set(k, v, version=X) called N times → same final state
        - No hidden version increments
        - Storage state hash must be identical after repeated identical calls
        - Idempotent replays MUST produce identical storage state hashes
        
        Args:
            key: Storage key (validated format, must pass validate_key())
            value: Raw bytes to store (must pass validate_value())
            version: Optional version number for CAS support (must be > previous version)
            ttl_seconds: Optional expiration time in seconds (advisory, must not break replay)
            
        Raises:
            KeyValidationError: If key format is invalid
            ValueValidationError: If value is invalid
            UnsupportedOperationError: If versioning/TTL not supported
            StorageExhaustedError: If storage capacity exceeded
            KVBackendError: On storage failure
        """
        pass
    
    @abstractmethod
    def delete(self, key: str) -> None:
        """
        Delete key from storage.
        
        GUARANTEES:
        - Idempotent (safe to call multiple times)
        - Atomic operation
        - Silent if key does not exist
        - After success, get(key) returns None
        
        Args:
            key: Storage key to delete
            
        Raises:
            KeyValidationError: If key format is invalid
            KVBackendError: On storage failure
        """
        pass
    
    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check if key exists in storage.
        
        GUARANTEES:
        - Pure existence check (no side effects)
        - Returns False for expired keys (TTL)
        - Consistent with get() semantics
        - Does NOT create entries
        - Does NOT mutate state
        
        CONSISTENCY:
        After set(K, V): exists(K) returns True
        After delete(K): exists(K) returns False
        If get(K) returns None: exists(K) returns False
        If get(K) returns value: exists(K) returns True
        
        Args:
            key: Storage key to check
            
        Returns:
            True if key exists and not expired, False otherwise
            
        Raises:
            KeyValidationError: If key format is invalid
            KVBackendError: On storage failure
        """
        pass
    
    # ========================================================================
    # Versioning & CAS Operations
    # ========================================================================
    
    @abstractmethod
    def compare_and_swap(
        self,
        key: str,
        expected_version: int,
        new_value: bytes,
        new_version: int
    ) -> bool:
        """
        Atomic compare-and-swap with version checking.
        
        This is the primary optimistic concurrency control primitive.
        All backends that cannot support CAS must emulate it safely.
        
        GUARANTEES:
        - Atomic read-compare-write operation (single atomic operation)
        - Returns True only if version matches and swap succeeds
        - Returns False if version mismatch (no exception - this is expected behavior)
        - All other failures raise exceptions (not boolean returns)
        - No silent failures
        
        ALGORITHM (MUST be atomic):
        1. Read current (value, version) atomically
        2. If current version == expected_version:
             Write (new_value, new_version) atomically
             Return True
        3. Else:
             No modification (key unchanged)
             Return False
        
        VERSION RULES:
        - new_version MUST be > expected_version (strictly monotonic)
        - Backend MAY enforce new_version == expected_version + 1
        - Version numbers must be strictly monotonic per key
        - If key does not exist: returns False (version mismatch)
        
        DETERMINISM:
        - Same inputs → same result
        - No time-based variation
        - Replay correctness depends on this
        
        IDEMPOTENCY:
        - If called with same args after success: returns False (version changed)
        - Deterministic: same inputs → same result
        
        ERROR MODEL:
        - Returns False only for version mismatch (expected behavior)
        - Raises VersionConflictError for critical version issues (if backend chooses)
        - Raises exceptions for all other failures (not boolean returns)
        
        Args:
            key: Storage key (validated format)
            expected_version: Expected current version (must match exactly)
            new_value: New value to write if version matches (must pass validate_value())
            new_version: New version to assign (must be > expected_version)
            
        Returns:
            True if version matched and swap succeeded, False if version mismatch
            
        Raises:
            KeyValidationError: If key format is invalid
            ValueValidationError: If value is invalid
            UnsupportedOperationError: If backend doesn't support CAS
            ValueError: If new_version <= expected_version
            AtomicityViolationError: If atomicity cannot be guaranteed
            KVBackendError: On storage failure
        """
        pass
    
    def get_version(self, key: str) -> Optional[int]:
        """
        Get current version of key.
        
        Optional operation - backends may return None if versioning not tracked.
        
        Args:
            key: Storage key
            
        Returns:
            Current version number, or None if key absent or no version
            
        Raises:
            KeyValidationError: If key format is invalid
            UnsupportedOperationError: If backend doesn't support versioning
        """
        raise UnsupportedOperationError("get_version", self.__class__.__name__)
    
    # ========================================================================
    # Batch Operations
    # ========================================================================
    
    def batch_set(
        self,
        entries: Mapping[str, bytes],
        *,
        versions: Optional[Mapping[str, int]] = None,
        ttl_seconds: Optional[int] = None
    ) -> None:
        """
        Write multiple key-value pairs.
        
        ATOMICITY:
        - If backend supports transactions: all-or-nothing
        - If backend does NOT support transactions: applied in deterministic order
        
        Default implementation: Sequential set() calls.
        Backends SHOULD override with optimized batch implementation.
        
        Args:
            entries: Mapping of keys to values
            versions: Optional mapping of keys to versions
            ttl_seconds: Optional TTL applied to all entries
            
        Raises:
            Same exceptions as set()
        """
        for key, value in entries.items():
            version = versions.get(key) if versions else None
            self.set(key, value, version=version, ttl_seconds=ttl_seconds)
    
    def batch_delete(self, keys: List[str]) -> None:
        """
        Delete multiple keys.
        
        ATOMICITY:
        Same atomicity guarantees as batch_set().
        
        Default implementation: Sequential delete() calls.
        
        Args:
            keys: List of keys to delete
            
        Raises:
            Same exceptions as delete()
        """
        for key in keys:
            self.delete(key)
    
    # ========================================================================
    # Advanced Queries (Optional)
    # ========================================================================
    
    def list_keys(
        self,
        prefix: Optional[str] = None,
        limit: Optional[int] = None
    ) -> List[str]:
        """
        List keys, optionally filtered by prefix.
        
        DETERMINISM:
        - Results must be sorted (deterministic ordering)
        - Same prefix+limit → same results
        
        Optional operation - backends may raise UnsupportedOperationError.
        
        Args:
            prefix: Optional key prefix to filter by
            limit: Optional maximum number of keys to return
            
        Returns:
            Sorted list of keys
            
        Raises:
            UnsupportedOperationError: If backend doesn't support listing
        """
        raise UnsupportedOperationError("list_keys", self.__class__.__name__)
    
    def scan_prefix(
        self,
        prefix: str,
        limit: Optional[int] = None
    ) -> List[Tuple[str, bytes]]:
        """
        Scan all key-value pairs with given prefix.
        
        DETERMINISM:
        - Results must be sorted by key
        - Same prefix+limit → same results
        
        Optional operation - backends may raise UnsupportedOperationError.
        
        Args:
            prefix: Key prefix to scan
            limit: Optional maximum number of entries to return
            
        Returns:
            Sorted list of (key, value) tuples
            
        Raises:
            UnsupportedOperationError: If backend doesn't support prefix scan
        """
        raise UnsupportedOperationError("scan_prefix", self.__class__.__name__)
    
    # ========================================================================
    # Metadata & Introspection
    # ========================================================================
    
    @abstractmethod
    def get_capabilities(self) -> KVBackendCapabilities:
        """
        Get backend capabilities and guarantees.
        
        REQUIRED:
        Every backend MUST implement this to explicitly declare:
        - Replay safety level
        - Durability level
        - Feature support
        - Concurrency guarantees
        
        Returns:
            KVBackendCapabilities instance
        """
        pass
    
    def stats(self) -> Mapping[str, Any]:
        """
        Get backend statistics and metrics.
        
        This is REQUIRED for observability in production systems.
        Interface must support optional hooks for metrics collection.
        
        Optional operation - backends may return empty dict.
        However, backends SHOULD implement this for production use.
        
        REQUIRED METRICS (if implemented):
        - read_count: int (total get() calls)
        - write_count: int (total set() calls)
        - delete_count: int (total delete() calls)
        - cas_count: int (total compare_and_swap() calls)
        - cas_success_count: int (successful CAS operations)
        - storage_bytes: int (total bytes stored)
        - key_count: int (total number of keys)
        - error_count: int (total errors encountered)
        
        OPTIONAL METRICS:
        - cas_failure_count: int (failed CAS due to version mismatch)
        - ttl_expired_count: int (keys expired due to TTL)
        - batch_operation_count: int (batch operations performed)
        
        OBSERVABILITY:
        - No logging in abstract class (metrics only)
        - Metrics must be deterministic (same state → same metrics)
        - Metrics should be thread-safe if backend is thread-safe
        
        Returns:
            Mapping of metric names to values (immutable recommended)
        """
        return {}
    
    # ========================================================================
    # State Fingerprinting (Replay Validation)
    # ========================================================================
    
    def storage_fingerprint(self) -> str:
        """
        Compute deterministic hash of complete storage state.
        
        This is REQUIRED for replay validation and integrity checking at 500k+ LOC scale.
        
        Used for:
        - Replay validation (verify state matches expected)
        - Snapshot verification (verify snapshot integrity)
        - Cross-region integrity checking (verify consistency across replicas)
        - Deterministic audits (prove system state correctness)
        
        DETERMINISM REQUIREMENTS (CRITICAL):
        - Same storage state → same fingerprint (byte-for-byte identical)
        - Keys MUST be sorted alphabetically (deterministic ordering)
        - Hash MUST include (key + version + value) for each entry
        - Hash algorithm MUST be deterministic (SHA-256 recommended)
        - Fingerprint MUST be stable across runs, machines, and time
        
        ALGORITHM:
        1. Collect all (key, version, value) tuples
        2. Sort by key (alphabetically, case-sensitive)
        3. For each entry: hash(key + str(version) + value)
        4. Combine all hashes deterministically
        5. Return hex-encoded final hash
        
        Optional operation - backends may raise UnsupportedOperationError.
        However, backends SHOULD implement this for production use.
        
        Returns:
            Hex-encoded SHA-256 hash of storage state (64 characters)
            
        Raises:
            UnsupportedOperationError: If backend doesn't support fingerprinting
            KVBackendError: On storage failure during fingerprint computation
        """
        raise UnsupportedOperationError("storage_fingerprint", self.__class__.__name__)
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def validate_key(self, key: str) -> None:
        """
        Validate key format.
        
        RULES:
        - Must be non-empty string
        - No whitespace
        - Maximum length: MAX_KEY_LENGTH
        - ASCII-safe characters only
        
        Args:
            key: Key to validate
            
        Raises:
            KeyValidationError: If key is invalid
        """
        if not isinstance(key, str):
            raise KeyValidationError(str(key), "Key must be string")
        
        if not key:
            raise KeyValidationError(key, "Key cannot be empty")
        
        if len(key) > MAX_KEY_LENGTH:
            raise KeyValidationError(
                key,
                f"Key exceeds maximum length {MAX_KEY_LENGTH}"
            )
        
        if any(c.isspace() for c in key):
            raise KeyValidationError(key, "Key cannot contain whitespace")
        
        # ASCII-safe check
        try:
            key.encode('ascii')
        except UnicodeEncodeError:
            raise KeyValidationError(key, "Key must be ASCII-safe")
    
    def validate_value(self, value: bytes) -> None:
        """
        Validate value format.
        
        RULES:
        - Must be bytes type
        - Maximum size: MAX_VALUE_SIZE
        
        Args:
            value: Value to validate
            
        Raises:
            ValueValidationError: If value is invalid
        """
        if not isinstance(value, bytes):
            raise ValueValidationError(f"Value must be bytes, got {type(value)}")
        
        if len(value) > MAX_VALUE_SIZE:
            raise ValueValidationError(
                f"Value exceeds maximum size {MAX_VALUE_SIZE} bytes"
            )


# ============================================================================
# Helper Functions
# ============================================================================

def is_valid_key(key: str) -> bool:
    """
    Check if key is valid without raising exception.
    
    Args:
        key: Key to check
        
    Returns:
        True if valid, False otherwise
    """
    try:
        # Create temporary backend instance for validation
        # In practice, use static validation logic
        if not isinstance(key, str) or not key:
            return False
        if len(key) > MAX_KEY_LENGTH:
            return False
        if any(c.isspace() for c in key):
            return False
        key.encode('ascii')
        return True
    except (UnicodeEncodeError, Exception):
        return False