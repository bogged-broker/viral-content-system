"""
Deterministic Persistence Contract Authority

================================================================================
WHAT THIS FILE EXISTS FOR (NON-NEGOTIABLE)
================================================================================

backend_base.py defines the only authoritative interface that every persistence
backend must implement. It answers:

> "What does it mean, formally and contractually, for data to be written, read,
> deleted, and durable in this system?"

This file defines:
- Atomicity guarantees
- Durability guarantees
- Isolation guarantees (if applicable)
- Error semantics
- Idempotency expectations
- Determinism requirements

This is a CONTRACT LAYER ONLY.
No implementation. No backend-specific logic. No shortcuts.

================================================================================
NON-GOALS
================================================================================

This file does NOT:
- Implement storage
- Contain serialization logic
- Contain retry logic
- Perform migrations
- Instantiate concrete backends

That belongs elsewhere.

================================================================================
CORE DESIGN PRINCIPLES
================================================================================

1. DETERMINISM FIRST

Given:
  - The same key
  - The same serialized value
  - The same preconditions

The backend MUST produce the same observable result.

No time-based mutation. No hidden metadata injection. No implicit version
stamping unless explicitly defined in contract.

2. EXPLICIT DURABILITY SEMANTICS

The contract must define:
  - Whether put() returns only after durable write
  - Whether fsync / commit is required
  - Whether writes may be buffered
  - Crash consistency guarantees

This must not be "backend-specific magic."

3. ATOMICITY GUARANTEE

Each operation must explicitly define:
  - Is it atomic?
  - Across what scope? (single key / batch)
  - What happens during partial failure?

Undefined behavior is forbidden.

4. STRICT ERROR TAXONOMY

The base interface must specify that implementations raise only:
  - RetryablePersistenceError
  - NonRetryablePersistenceError
  - ConsistencyViolation
  - DurabilityFailure

No raw Exception. Error leakage = contract violation.

================================================================================
OPERATIONAL GUARANTEES
================================================================================

DURABILITY LEVELS:

All backend implementations must explicitly declare which durability level
they provide. This determines when put() returns and what survives crashes.

1. STRONG (DurabilityLevel.STRONG)
   - Synchronous disk/commit durability
   - put() returns ONLY after data is durably written to non-volatile storage
   - REQUIRES: fsync() or equivalent commit operation before return
   - Crash-safe: data persists across process/system failures
   - After successful put(K, V) with STRONG durability:
     * Data MUST survive process crash
     * Data MUST survive system crash
     * Data MUST survive power loss (if storage hardware supports it)
   - Use for: Production, critical data, audit trails, checkpoints
   - Performance: Slower (synchronous I/O required)

2. EVENTUAL (DurabilityLevel.EVENTUAL)
   - Buffered writes with guaranteed eventual commit
   - put() MAY return before data is durably written
   - Data is buffered in memory or write-ahead log
   - Background flush to durable storage (timing not guaranteed)
   - May be lost in crash window (between put() return and background flush)
   - After successful put(K, V) with EVENTUAL durability:
     * Data MAY be lost if process crashes before background flush
     * Data SHOULD eventually be durable (timing not specified)
   - Use for: High-throughput non-critical data, metrics, logs
   - Performance: Faster (asynchronous I/O allowed)

3. EPHEMERAL (DurabilityLevel.EPHEMERAL)
   - No crash guarantees whatsoever
   - put() may store data only in memory
   - Data lost on process termination
   - No fsync or commit operations required
   - After successful put(K, V) with EPHEMERAL durability:
     * Data WILL be lost on process termination
     * Data WILL be lost on system crash
   - Use for: Testing, development, temporary computation, caches
   - Performance: Fastest (no I/O required)

ATOMICITY GUARANTEES:

Single-key operations (get, put, delete):
- ALWAYS atomic at the key level
- Either complete success or complete failure
- No partial writes visible
- If put() fails mid-write:
  * Key MUST remain in previous state (old value or absent)
  * Key MUST NOT contain partial/corrupted bytes
  * Key MUST NOT be in undefined state
- If delete() fails:
  * Key MUST remain in previous state (present or absent)
  * No partial deletion state

Batch operations (batch_put, batch_delete):
- If backend supports transactions (supports_transactions=True):
  * All-or-nothing semantics (atomic across all keys)
  * Either ALL operations succeed or NONE succeed
  * If any operation fails, entire batch is rolled back
  * No partial state visible to other operations
- If backend does NOT support transactions:
  * Implementation MUST explicitly document partial failure behavior
  * MUST specify which operations succeeded and which failed
  * MUST NOT leave system in undefined state
  * Recommended: Apply operations in deterministic key order
  * Recommended: Stop on first failure (fail-fast)
- Partial failure handling must be explicit and documented

ISOLATION EXPECTATIONS:

Non-transactional backends:
- Read-after-write consistency within same client/process
- No guarantees across concurrent writers
- Last-write-wins semantics

Transactional backends (if supported):
- Must explicitly document isolation level:
  * SERIALIZABLE (strictest)
  * REPEATABLE_READ
  * READ_COMMITTED
  * READ_UNCOMMITTED

CRASH CONSISTENCY GUARANTEES:

The contract must explicitly define what happens after a crash/restart.

After a crash/restart, for any key K:

STRONG durability:
  - Either previous committed value OR new committed value
  - If put(K, V) was in progress during crash:
    * If fsync completed: K contains V (new value)
    * If fsync did not complete: K contains previous value (or absent if new key)
    * NEVER partial bytes of V
    * NEVER corrupted state
  - If delete(K) was in progress during crash:
    * If commit completed: K is absent
    * If commit did not complete: K contains previous value
    * NEVER partial deletion state

EVENTUAL durability:
  - Either previous value OR new value OR no value (if in buffer)
  - If put(K, V) was in progress during crash:
    * If background flush completed: K contains V (new value)
    * If background flush did not complete: K contains previous value OR absent
    * If data was only in buffer: K may be absent (data lost)
  - Timing of background flush is not guaranteed

EPHEMERAL durability:
  - Undefined (data lost on process termination)
  - All data in memory is lost
  - No recovery possible

NEVER after crash (applies to all durability levels):
  - Partial bytes (half-written values)
  - Corrupted encoding (invalid byte sequences)
  - Undefined state (neither old nor new value)
  - Mixed old/new data within single value
  - Keys in inconsistent state (present but unreadable)

THREAD SAFETY & CONCURRENCY:

Backend implementations must specify:
- Thread-safe: Safe for concurrent access from multiple threads in same process
- Process-safe: Safe for concurrent access from multiple processes
- Distributed-safe: Safe for concurrent access across multiple hosts

Minimum requirement: Thread-safe within single process

REPLAY & IDEMPOTENCY REQUIREMENTS:

All backends MUST guarantee deterministic replay behavior:

Given:
  - Same key K
  - Same value V
  - Same write mode M

Replaying the operation multiple times MUST produce identical final state.

Specifically:
  - put(K, V, UPSERT) replayed N times → same final state
  - put(K, V, INSERT_ONLY) on existing key → same error every time
  - delete(K) replayed N times → same final state (key absent)

FORBIDDEN behaviors during replay:
  - Different results based on timestamp
  - Different results based on random values
  - Implicit state mutation between replays

DETERMINISM GUARD CLAUSES (CRITICAL):

The interface documentation must clearly forbid non-deterministic behaviors.

Backend implementations MUST NOT:
  - Insert timestamps into stored values without explicit contract
  - Use randomness in write path (no random IDs, no random suffixes)
  - Auto-mutate keys (no key rewriting, no normalization)
  - Run background mutation threads that alter semantics
  - Depend on wall-clock time for correctness
  - Depend on system hostname for correctness
  - Depend on process ID for correctness
  - Depend on thread ID for correctness
  - Use unordered iteration (dict iteration order must be deterministic)
  - Perform implicit schema evolution
  - Auto-repair or auto-correct data
  - Inject hidden metadata into values
  - Perform lazy consistency promotion
  - Use non-deterministic algorithms for versioning

Backends must be behaviorally pure relative to inputs.

Given identical inputs (key, value, mode, version), the backend MUST produce
identical observable results across:
  - Multiple runs on same machine
  - Runs on different machines
  - Runs at different times
  - Runs in different processes
  - Replay scenarios

This determinism is REQUIRED for replay correctness at 500k+ LOC scale.

================================================================================
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, Dict, List, Any
from dataclasses import dataclass


# ============================================================================
# Durability Level
# ============================================================================

class DurabilityLevel(Enum):
    """
    Explicit durability guarantee levels.
    
    Every backend implementation must declare which level it provides.
    """
    STRONG = "strong"        # Synchronous disk durability, crash-safe
    EVENTUAL = "eventual"    # Buffered with eventual commit
    EPHEMERAL = "ephemeral"  # No crash guarantees (testing only)


# ============================================================================
# Write Modes
# ============================================================================

class WriteMode(Enum):
    """
    Explicit write intent for put operations.
    
    Defines preconditions and failure behavior.
    """
    INSERT_ONLY = "insert_only"    # Fail if key already exists
    UPSERT = "upsert"              # Create or overwrite unconditionally
    REPLACE_ONLY = "replace_only"  # Fail if key does not exist
    CAS = "cas"                    # Compare-and-swap with version check


# ============================================================================
# Isolation Levels
# ============================================================================

class IsolationLevel(Enum):
    """
    Transaction isolation levels for backends that support transactions.
    
    Non-transactional backends should declare NONE.
    """
    SERIALIZABLE = "serializable"
    REPEATABLE_READ = "repeatable_read"
    READ_COMMITTED = "read_committed"
    READ_UNCOMMITTED = "read_uncommitted"
    NONE = "none"  # No transaction support


# ============================================================================
# Error Taxonomy
# ============================================================================

class PersistenceError(Exception):
    """
    Base exception for all persistence layer errors.
    
    All backend implementations MUST raise only exceptions derived from
    this base class. Raw Exception raises are contract violations.
    """
    pass


class RetryablePersistenceError(PersistenceError):
    """
    Error that MAY succeed if retried.
    
    Examples:
    - Temporary network failure
    - Lock timeout
    - Transient resource exhaustion
    
    Retry logic should handle these.
    """
    pass


class NonRetryablePersistenceError(PersistenceError):
    """
    Error that will NEVER succeed if retried with same inputs.
    
    Examples:
    - Invalid key format
    - Value exceeds size limit
    - Permission denied
    
    Retry logic should NOT retry these.
    """
    pass


class ConsistencyViolation(NonRetryablePersistenceError):
    """
    Consistency or invariant violation detected.
    
    Examples:
    - Version mismatch in CAS operation
    - Constraint violation
    - Corruption detected
    
    Indicates logical error in caller or data corruption.
    """
    pass


class DurabilityFailure(RetryablePersistenceError):
    """
    Durability commitment could not be guaranteed.
    
    Examples:
    - Disk flush failed
    - Replication quorum not reached
    - Commit log write failed
    
    Data may be lost if process crashes.
    """
    pass


class KeyNotFoundError(NonRetryablePersistenceError):
    """
    Key does not exist when existence was required.
    
    Used for operations that require key to exist (e.g., REPLACE_ONLY).
    """
    def __init__(self, key: str):
        super().__init__(f"Key not found: '{key}'")
        self.key = key


class KeyExistsError(NonRetryablePersistenceError):
    """
    Key already exists when non-existence was required.
    
    Used for operations that require key to not exist (e.g., INSERT_ONLY).
    """
    def __init__(self, key: str):
        super().__init__(f"Key already exists: '{key}'")
        self.key = key


class VersionMismatchError(ConsistencyViolation):
    """
    Version mismatch in compare-and-swap operation.
    
    Expected version does not match actual version.
    """
    def __init__(self, key: str, expected: int, actual: int):
        super().__init__(
            f"Version mismatch for key '{key}': expected {expected}, actual {actual}"
        )
        self.key = key
        self.expected = expected
        self.actual = actual


# ============================================================================
# Backend Capabilities
# ============================================================================

@dataclass(frozen=True)
class BackendCapabilities:
    """
    Explicit declaration of backend capabilities and guarantees.
    
    Every backend implementation must provide this via get_capabilities().
    """
    durability_level: DurabilityLevel
    isolation_level: IsolationLevel
    supports_transactions: bool
    supports_batching: bool
    supports_cas: bool
    thread_safe: bool
    process_safe: bool
    distributed_safe: bool
    
    def __post_init__(self):
        """Validate capability consistency."""
        if self.supports_transactions and self.isolation_level == IsolationLevel.NONE:
            raise ValueError(
                "Backend claims to support transactions but isolation_level is NONE"
            )
        
        if not self.supports_transactions and self.isolation_level != IsolationLevel.NONE:
            raise ValueError(
                "Backend does not support transactions but declares isolation level"
            )


# ============================================================================
# Backend Base Contract
# ============================================================================

class BackendBase(ABC):
    """
    Authoritative abstract base class for all persistence backends.
    
    This contract defines the formal interface that all backends must implement.
    Implementations must strictly adhere to all guarantees documented here
    and in the module-level OPERATIONAL GUARANTEES section.
    
    CONTRACT VIOLATIONS:
    
    Any implementation that:
    - Raises exceptions not derived from PersistenceError
    - Violates atomicity guarantees
    - Produces non-deterministic results for identical inputs
    - Fails to honor write mode semantics
    - Mutates stored values
    - Injects implicit metadata
    
    ...is in violation of this contract and MUST be rejected.
    """
    
    # ========================================================================
    # Core Operations
    # ========================================================================
    
    @abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        """
        Retrieve value for key.
        
        GUARANTEES:
        - Returns exactly the last committed value for this key
        - Returns None if key does not exist (NEVER raises KeyNotFoundError)
        - No implicit decoding or deserialization
        - No metadata injection into returned bytes
        - No mutation of stored bytes
        - Read-after-write consistent under backend's durability level
        
        CONSISTENCY:
        After successful put(K, V) with STRONG durability:
          - get(K) MUST return V
        
        After successful put(K, V) with EVENTUAL durability:
          - get(K) SHOULD return V (may have delay)
        
        After successful delete(K):
          - get(K) MUST return None
        
        DETERMINISM:
        Multiple calls with same key MUST return same value
        (unless intervening write occurred).
        
        Args:
            key: Storage key (non-empty string)
            
        Returns:
            Raw bytes value if key exists, None otherwise
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
            RetryablePersistenceError: If transient failure occurs
        """
        pass
    
    @abstractmethod
    def put(
        self,
        key: str,
        value: bytes,
        mode: WriteMode = WriteMode.UPSERT,
        expected_version: Optional[int] = None
    ) -> None:
        """
        Write value to key with specified write mode.
        
        This is the primary write operation. All writes must go through this method.
        
        GUARANTEES:
        - Atomic at the key level (all-or-nothing)
        - On success, value is durable according to backend's durability level
        - On failure, no partial state persists (strong crash consistency)
        - Idempotent for UPSERT mode (same key+value → same final state)
        
        DURABILITY SEMANTICS (CRITICAL):
        
        When put() returns successfully, durability depends on backend's declared level:
        
        STRONG durability:
          - put() returns ONLY after data is durably written
          - REQUIRES: fsync() or equivalent commit operation completed
          - Data MUST survive process crash immediately after return
          - Data MUST survive system crash immediately after return
          - If put() returns without error, data is guaranteed durable
        
        EVENTUAL durability:
          - put() MAY return before data is durably written
          - Data is buffered (in memory or write-ahead log)
          - Background flush will eventually make data durable
          - Data MAY be lost if process crashes before background flush
          - Timing of background flush is not guaranteed
        
        EPHEMERAL durability:
          - put() may store data only in memory
          - No fsync or commit operations required
          - Data WILL be lost on process termination
        
        WRITE MODE SEMANTICS:
        
        INSERT_ONLY:
          - Succeeds only if key does NOT exist
          - Raises KeyExistsError if key exists
          - Idempotent on replay: same error if key still exists
          - Use for: Ensuring key creation (no overwrite)
        
        UPSERT:
          - Always succeeds
          - Creates if absent, overwrites if present
          - Idempotent: replaying with same value → same final state
          - Use for: Default write mode (most common)
        
        REPLACE_ONLY:
          - Succeeds only if key EXISTS
          - Raises KeyNotFoundError if key absent
          - Overwrites existing value
          - Use for: Ensuring key exists before update
        
        CAS (Compare-And-Swap):
          - Requires expected_version parameter
          - Succeeds only if current version matches expected_version
          - Raises VersionMismatchError if version differs
          - Atomic read-modify-write primitive
          - Use for: Optimistic concurrency control
        
        CRASH CONSISTENCY:
        If crash occurs during put():
          - Backend MUST ensure either old value OR new value is present
          - NEVER partial bytes (half-written values)
          - NEVER corrupted state (invalid encoding)
          - NEVER undefined state (neither old nor new)
          - For STRONG durability: fsync/commit must be atomic
          - For EVENTUAL durability: buffer must be flushed atomically if flushed
          - Recovery after crash must restore to valid state (old or new, never partial)
        
        DETERMINISM:
        Given identical (key, value, mode, version), replaying operation
        MUST produce identical final state. This is REQUIRED for replay correctness.
        
        Args:
            key: Storage key (non-empty string)
            value: Raw bytes to store
            mode: Write mode specifying preconditions
            expected_version: Required for CAS mode, ignored otherwise
            
        Raises:
            KeyExistsError: If INSERT_ONLY and key exists
            KeyNotFoundError: If REPLACE_ONLY and key absent
            VersionMismatchError: If CAS and version mismatch
            NonRetryablePersistenceError: If invalid inputs
            RetryablePersistenceError: If transient failure
            DurabilityFailure: If durability commitment failed
        """
        pass
    
    @abstractmethod
    def delete(self, key: str) -> None:
        """
        Delete key from storage.
        
        GUARANTEES:
        - Idempotent (safe to call multiple times)
        - Atomic (key either exists or doesn't)
        - After success, get(key) MUST return None
        - Silent if key does not exist (no error raised)
        
        CONSISTENCY:
        After successful delete(K):
          - get(K) MUST return None
          - exists(K) MUST return False
        
        DETERMINISM:
        Replaying delete(K) multiple times MUST produce identical
        final state (key absent).
        
        Args:
            key: Storage key to delete
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
            RetryablePersistenceError: If transient failure occurs
        """
        pass
    
    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check if key exists in storage.
        
        GUARANTEES:
        - Pure existence check (no side effects)
        - Does NOT mutate state
        - Does NOT create entries
        - Does NOT perform repair logic
        - Consistent with get() semantics
        
        CONSISTENCY:
        After put(K, V): exists(K) MUST return True
        After delete(K): exists(K) MUST return False
        If get(K) returns None: exists(K) MUST return False
        If get(K) returns value: exists(K) MUST return True
        
        Args:
            key: Storage key to check
            
        Returns:
            True if key exists, False otherwise
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
            RetryablePersistenceError: If transient failure occurs
        """
        pass
    
    # ========================================================================
    # Batch Operations (Optional but Recommended)
    # ========================================================================
    
    def batch_put(
        self,
        entries: Dict[str, bytes],
        mode: WriteMode = WriteMode.UPSERT
    ) -> None:
        """
        Write multiple key-value pairs atomically (if supported).
        
        ATOMICITY:
        If backend supports transactions (supports_transactions=True):
          - All-or-nothing semantics (atomic across all keys)
          - Either ALL writes succeed or NONE succeed
          - If any write fails, entire batch is rolled back
          - No partial state visible to other operations
          - Atomicity is guaranteed even across multiple keys
        
        If backend does NOT support transactions:
          - Implementation MUST explicitly document partial failure behavior
          - MUST specify which operations succeeded and which failed
          - MUST NOT leave system in undefined state
          - Recommended: Apply writes in deterministic key order (sorted keys)
          - Recommended: Stop on first failure (fail-fast semantics)
          - Partial state may be visible (some keys updated, others not)
        
        Default implementation: Sequential put() calls (no atomicity).
        Backends SHOULD override with optimized batch implementation.
        
        DURABILITY:
        Durability semantics follow the same rules as put() for each entry.
        For STRONG durability backends, all entries must be durably written
        before batch_put() returns (if atomicity is supported).
        
        DETERMINISM:
        Given identical entries dict and mode, replaying batch_put() MUST produce
        identical final state. Key iteration order must be deterministic.
        
        Args:
            entries: Dictionary mapping keys to values
            mode: Write mode (applied to all entries)
            
        Raises:
            Same exceptions as put()
            Additional: Partial failure may occur if transactions not supported
        """
        for key, value in entries.items():
            self.put(key, value, mode)
    
    def batch_delete(self, keys: List[str]) -> None:
        """
        Delete multiple keys.
        
        ATOMICITY:
        Same atomicity guarantees as batch_put().
        
        Default implementation: Sequential delete() calls.
        Backends SHOULD override with optimized implementation.
        
        Args:
            keys: List of keys to delete
            
        Raises:
            Same exceptions as delete()
        """
        for key in keys:
            self.delete(key)
    
    # ========================================================================
    # Metadata & Introspection
    # ========================================================================
    
    @abstractmethod
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """
        Get backend-specific metadata for key.
        
        GUARANTEES:
        - Does NOT mutate state
        - Returns empty dict if key does not exist (no error)
        - Metadata MUST NOT be injected into value bytes
        
        Common metadata keys (backend-dependent):
        - version: int
        - timestamp: float
        - size: int
        - checksum: str
        
        Args:
            key: Storage key
            
        Returns:
            Dictionary of metadata (empty if key absent)
            
        Raises:
            RetryablePersistenceError: If transient failure occurs
        """
        pass
    
    @abstractmethod
    def get_capabilities(self) -> BackendCapabilities:
        """
        Get backend capabilities and guarantees.
        
        REQUIRED:
        Every backend MUST implement this to explicitly declare:
        - Durability level
        - Isolation level
        - Transaction support
        - Thread safety guarantees
        
        Returns:
            BackendCapabilities instance
        """
        pass
    
    # ========================================================================
    # Transaction Support (Optional)
    # ========================================================================
    
    def begin_transaction(self) -> 'TransactionContext':
        """
        Begin a transaction context.
        
        Only supported if get_capabilities().supports_transactions is True.
        
        Raises:
            NotImplementedError: If transactions not supported
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support transactions"
        )
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def supports_transactions(self) -> bool:
        """
        Check if backend supports atomic transactions.
        
        Returns:
            True if transactions supported
        """
        return self.get_capabilities().supports_transactions
    
    def supports_batching(self) -> bool:
        """
        Check if backend supports efficient batching.
        
        Returns:
            True if batching supported
        """
        return self.get_capabilities().supports_batching
    
    def supports_cas(self) -> bool:
        """
        Check if backend supports compare-and-swap.
        
        Returns:
            True if CAS supported
        """
        return self.get_capabilities().supports_cas


# ============================================================================
# Transaction Context (Optional)
# ============================================================================

class TransactionContext(ABC):
    """
    Transaction context for backends that support transactions.
    
    Usage:
        with backend.begin_transaction() as txn:
            txn.put(key1, value1)
            txn.put(key2, value2)
            txn.commit()
        # All changes committed atomically
    
    If commit() not called, transaction is aborted on exit.
    """
    
    @abstractmethod
    def put(
        self,
        key: str,
        value: bytes,
        mode: WriteMode = WriteMode.UPSERT
    ) -> None:
        """Put within transaction."""
        pass
    
    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete within transaction."""
        pass
    
    @abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        """Get within transaction (sees uncommitted changes)."""
        pass
    
    @abstractmethod
    def commit(self) -> None:
        """Commit transaction atomically."""
        pass
    
    @abstractmethod
    def abort(self) -> None:
        """Abort transaction (discard all changes)."""
        pass
    
    @abstractmethod
    def __enter__(self) -> 'TransactionContext':
        """Enter transaction context."""
        pass
    
    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Exit transaction context.
        
        Auto-aborts if exception occurred or commit not called.
        """
        pass


# ============================================================================
# Contract Purity Note
# ============================================================================
#
# This file defines ONLY the contract. No aliases, no convenience methods
# that violate the contract. All backends must implement the exact interface
# defined above.
#
# If code requires read()/write() aliases, they should be implemented in
# backend-specific adapters, not in the base contract.
#