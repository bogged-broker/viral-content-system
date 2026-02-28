"""
/infra/persistence/backend/base.py

Persistence Backend Contracts (Zero Storage Assumptions)

This file is the ONLY legal interface between persistence authorities
(SnapshotStore, LockManager, etc.) and any concrete storage substrate.

This is a pure contract layer. If a backend violates this file, it is not
a backend — it's a bug.

WHAT THIS FILE IS:
  - The semantic contract for all storage operations
  - The source of truth for backend capabilities
  - The enforcer of deterministic behavior
  - The audit trail foundation

WHAT THIS FILE IS NOT:
  ❌ No storage logic
  ❌ No imports from concrete backends
  ❌ No environment awareness
  ❌ No serialization policy
  ❌ No business semantics
  ❌ No retry logic
  ❌ No metrics, tracing, or logging

CORE PRINCIPLE:
  Persistence backends are interchangeable ONLY if semantics are identical.
  This file enforces semantic equivalence, not API compatibility.

PHILOSOPHY:
  All backends must support:
    - Atomicity or explicit failure
    - Idempotent writes
    - Read-after-write consistency OR declared degradation
    - Monotonic versioning
    - Explicit durability boundaries
  
  If a backend cannot guarantee something, it must DECLARE it, not FAKE it.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Iterable, Dict, Any, List, Protocol
from datetime import datetime


# =============================================================================
# CONSISTENCY & DURABILITY MODELS
# =============================================================================


class ConsistencyModel(Enum):
    """
    Explicit consistency guarantees.
    
    Backends MUST declare their model accurately.
    Higher layers MUST NOT assume stronger guarantees.
    """
    STRONG = "strong"  # Linearizable, immediate visibility
    READ_AFTER_WRITE = "read_after_write"  # Session consistency
    EVENTUAL = "eventual"  # No immediate guarantees
    MONOTONIC_READ = "monotonic_read"  # Never go backwards
    CAUSAL = "causal"  # Preserves happens-before


class DurabilityLevel(Enum):
    """
    Physical durability guarantees.
    
    This affects RPO (Recovery Point Objective) and data loss exposure.
    """
    MEMORY = "memory"  # Lost on process death
    LOCAL_DISK = "local_disk"  # Lost on disk failure
    REPLICATED = "replicated"  # Survives single-node failure
    EXTERNAL = "external"  # Cloud-managed, multi-region
    PERSISTENT_MEMORY = "persistent_memory"  # nvDIMM, Intel Optane


class IsolationLevel(Enum):
    """
    Transaction isolation semantics.
    
    Only relevant if backend supports transactions.
    """
    READ_UNCOMMITTED = "read_uncommitted"
    READ_COMMITTED = "read_committed"
    REPEATABLE_READ = "repeatable_read"
    SERIALIZABLE = "serializable"
    SNAPSHOT = "snapshot"


# =============================================================================
# BACKEND HEALTH & STATUS
# =============================================================================


class BackendStatus(Enum):
    """Current operational status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Functional but impaired
    UNAVAILABLE = "unavailable"  # Cannot serve requests
    INITIALIZING = "initializing"  # Not yet ready
    SHUTTING_DOWN = "shutting_down"


@dataclass(frozen=True)
class BackendHealth:
    """
    Structured health check result.
    
    MUST be deterministic for same backend state.
    MUST NOT include non-deterministic timing jitter.
    """
    status: BackendStatus
    message: str
    latency_ms: Optional[float] = None
    available_capacity_bytes: Optional[int] = None
    degradation_reason: Optional[str] = None
    last_successful_write: Optional[datetime] = None
    error: Optional[str] = None
    
    def is_usable(self) -> bool:
        """Can the backend serve requests?"""
        return self.status in (BackendStatus.HEALTHY, BackendStatus.DEGRADED)


# =============================================================================
# BACKEND CAPABILITIES (IMMUTABLE DECLARATION)
# =============================================================================


@dataclass(frozen=True)
class BackendCapabilities:
    """
    Immutable declaration of backend guarantees.
    
    PURPOSE:
      Lets higher layers adapt safely without probing behavior.
    
    RULES:
      - MUST be static after initialization
      - MUST NOT change at runtime
      - MUST accurately reflect backend semantics
      - Higher layers MUST respect these declarations
    
    If a backend lies about capabilities, it violates the contract.
    """
    
    # Atomicity guarantees
    atomic_write: bool  # Single-key writes are atomic
    atomic_read: bool  # Single-key reads are atomic
    atomic_delete: bool  # Single-key deletes are atomic
    
    # Transactional support
    supports_transactions: bool  # Multi-key ACID transactions
    supports_optimistic_locking: bool  # CAS, version-based updates
    supports_pessimistic_locking: bool  # Explicit locks/leases
    isolation_level: Optional[IsolationLevel] = None  # If transactions supported
    
    # Versioning & history
    supports_versioning: bool  # Immutable version history
    supports_version_listing: bool  # Can enumerate versions
    supports_version_deletion: bool  # Can delete old versions
    version_ordering_monotonic: bool  # Versions never go backwards
    
    # Query capabilities
    supports_prefix_listing: bool  # List keys by prefix
    supports_range_queries: bool  # Range scans
    supports_metadata_indexing: bool  # Query on metadata fields
    
    # Size & limits
    max_object_size_bytes: Optional[int] = None
    max_key_length_bytes: Optional[int] = None
    max_metadata_size_bytes: Optional[int] = None
    max_transaction_size_ops: Optional[int] = None  # Max ops per transaction
    
    # Consistency & durability
    consistency_model: ConsistencyModel = ConsistencyModel.EVENTUAL
    durability_level: DurabilityLevel = DurabilityLevel.MEMORY
    
    # Operational characteristics
    supports_flush: bool = False  # Explicit durability barrier
    supports_bulk_delete: bool = False  # Efficient multi-key delete
    supports_streaming: bool = False  # Streaming reads/writes
    
    # Advanced features
    supports_conditional_writes: bool = False  # If-match, if-none-match
    supports_multipart_upload: bool = False  # Large object chunking
    supports_server_side_copy: bool = False  # Copy without download
    
    def validate(self) -> None:
        """
        Validate capability consistency.
        
        Raises:
            ValueError: If capabilities are internally inconsistent
        """
        if self.supports_transactions and self.isolation_level is None:
            raise ValueError(
                "Backend declares transaction support but no isolation level"
            )
        
        if not self.supports_transactions and self.isolation_level is not None:
            raise ValueError(
                "Backend declares isolation level but no transaction support"
            )
        
        if self.supports_versioning and not self.atomic_write:
            raise ValueError(
                "Versioning requires atomic writes"
            )
        
        if self.max_transaction_size_ops is not None and not self.supports_transactions:
            raise ValueError(
                "Transaction size limit declared without transaction support"
            )


# =============================================================================
# BLOB & METADATA REFERENCES
# =============================================================================


@dataclass(frozen=True)
class BlobRef:
    """
    Reference to stored blob data.
    
    IMMUTABLE after creation.
    Used for content addressing and version tracking.
    """
    key: str
    version_id: Optional[str] = None  # If versioning supported
    etag: Optional[str] = None  # Content hash or server-assigned tag
    size_bytes: Optional[int] = None
    created_at: Optional[datetime] = None
    
    def __str__(self) -> str:
        if self.version_id:
            return f"{self.key}@{self.version_id}"
        return self.key


@dataclass(frozen=True)
class MetadataEntry:
    """
    Structured metadata record.
    
    MUST be JSON-safe.
    NO arbitrary Python objects.
    """
    key: str
    value: Dict[str, Any]
    version: Optional[str] = None
    updated_at: Optional[datetime] = None


# =============================================================================
# TRANSACTION ABSTRACTION
# =============================================================================


class BackendTransaction(ABC):
    """
    Explicit transactional scope.
    
    RULES:
      - May be real (DB transaction)
      - May be emulated (staging + commit)
      - May be rejected if unsupported
      - MUST be explicit, never implicit
      - NO auto-commit
      - NO silent fallbacks
    
    USAGE:
        with backend.begin_transaction() as tx:
            backend.put_blob("key1", b"data1", tx=tx)
            backend.put_blob("key2", b"data2", tx=tx)
            # Commit on __exit__ if no exception
    """
    
    @abstractmethod
    def commit(self) -> None:
        """
        Commit all operations in this transaction.
        
        MUST be idempotent (safe to call multiple times).
        
        Raises:
            BackendConflict: If transaction conflicts with concurrent writes
            BackendInvariantViolation: If transaction violates constraints
            BackendUnavailable: If backend is unavailable
        """
        pass
    
    @abstractmethod
    def rollback(self) -> None:
        """
        Roll back all operations in this transaction.
        
        MUST be idempotent.
        MUST leave no partial state.
        """
        pass
    
    @abstractmethod
    def is_active(self) -> bool:
        """Is this transaction still open?"""
        pass
    
    def __enter__(self) -> "BackendTransaction":
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit with automatic commit/rollback."""
        if exc_type is None and self.is_active():
            self.commit()
        elif self.is_active():
            self.rollback()


# =============================================================================
# BACKEND EXCEPTIONS (TYPED, NEVER GENERIC)
# =============================================================================


class BackendError(Exception):
    """
    Base exception for all backend errors.
    
    CRITICAL:
      - Backends MUST raise typed errors, never generic exceptions
      - No silent retries
      - No swallowing
      - Deterministic exception types for deterministic failures
    """
    pass


class BackendUnavailable(BackendError):
    """Backend is temporarily or permanently unavailable."""
    pass


class BackendPermissionDenied(BackendError):
    """Insufficient permissions for requested operation."""
    pass


class BackendConflict(BackendError):
    """
    Operation conflicts with existing state.
    
    Examples:
      - Optimistic lock failure
      - Version mismatch
      - Concurrent modification
    """
    pass


class BackendInvariantViolation(BackendError):
    """
    Operation would violate backend invariants.
    
    Examples:
      - Transaction too large
      - Invalid key format
      - Constraint violation
    """
    pass


class BackendDataCorruption(BackendError):
    """
    Data integrity check failed.
    
    This is CRITICAL and may indicate:
      - Bit rot
      - Incomplete writes
      - Checksum mismatch
    """
    pass


class BackendUnsupportedOperation(BackendError):
    """
    Operation is not supported by this backend.
    
    Should be preventable by checking capabilities.
    """
    pass


class BackendKeyNotFound(BackendError):
    """Requested key does not exist."""
    pass


class BackendTimeout(BackendError):
    """Operation exceeded time limit."""
    pass


class BackendQuotaExceeded(BackendError):
    """Storage quota or rate limit exceeded."""
    pass


# =============================================================================
# CORE PERSISTENCE BACKEND (ABSTRACT BASE CLASS)
# =============================================================================


class PersistenceBackend(ABC):
    """
    The absolute contract for all persistence backends.
    
    THIS IS THE SPINE.
    
    Every backend MUST implement this EXACTLY.
    
    FORBIDDEN BEHAVIORS:
      ❌ Lazy consistency promotion
      ❌ Silent overwrite
      ❌ Hidden retries
      ❌ Implicit transactions
      ❌ Partial visibility
      ❌ Background mutation
      ❌ Auto schema evolution
    
    If it mutates state invisibly, it's ILLEGAL.
    
    DETERMINISM REQUIREMENTS:
      - Same input → same result
      - Same failure → same exception type
      - No nondeterministic timing dependencies
      - No reliance on wall-clock unless explicit
      - Replay depends on this
    
    AUDIT & RECOVERY IMPLICATIONS:
      This contract enables:
        - Snapshot immutability verification
        - Replay determinism
        - Post-mortem reconstruction
        - Third-party audit
        - Cross-backend migration
      
      Every method must be externally explainable.
    """
    
    # =========================================================================
    # LIFECYCLE MANAGEMENT
    # =========================================================================
    
    @abstractmethod
    def open(self) -> None:
        """
        Initialize backend connection and resources.
        
        MUST be explicitly called before use.
        MUST be idempotent (safe to call multiple times).
        MUST NOT perform operations on unopened backend.
        
        Raises:
            BackendUnavailable: If backend cannot be initialized
            BackendPermissionDenied: If credentials are invalid
        """
        pass
    
    @abstractmethod
    def close(self) -> None:
        """
        Close backend connection and release resources.
        
        MUST be idempotent.
        MUST ensure all pending writes are visible (if possible).
        MUST NOT leave partial state.
        
        After close(), backend is unusable until open() called again.
        """
        pass
    
    @abstractmethod
    def healthcheck(self) -> BackendHealth:
        """
        Check backend operational status.
        
        MUST be safe to call at any time.
        MUST return structured result, not raise.
        SHOULD complete quickly (<1s typical).
        
        Returns:
            BackendHealth: Current health status
        """
        pass
    
    # =========================================================================
    # CAPABILITY DECLARATION
    # =========================================================================
    
    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """
        Immutable capability declaration.
        
        MUST be static after initialization.
        MUST NOT change at runtime.
        Higher layers MUST respect these declarations.
        
        Returns:
            BackendCapabilities: What this backend guarantees
        """
        pass
    
    # =========================================================================
    # TRANSACTION CONTROL
    # =========================================================================
    
    @abstractmethod
    def begin_transaction(self) -> BackendTransaction:
        """
        Begin an explicit transaction.
        
        OPTIONAL SUPPORT - check capabilities.supports_transactions first.
        
        Returns:
            BackendTransaction: Transaction handle
        
        Raises:
            BackendUnsupportedOperation: If transactions not supported
            BackendUnavailable: If backend unavailable
        """
        pass
    
    # =========================================================================
    # BLOB OPERATIONS (IMMUTABLE)
    # =========================================================================
    
    @abstractmethod
    def put_blob(
        self,
        key: str,
        data: bytes,
        *,
        tx: Optional[BackendTransaction] = None,
        metadata: Optional[Dict[str, Any]] = None,
        if_not_exists: bool = False,
    ) -> BlobRef:
        """
        Store immutable blob data.
        
        RULES:
          - MUST be idempotent or fail deterministically
          - Overwrites MUST be forbidden OR explicitly versioned
          - Partial writes are ILLEGAL
          - Empty data is legal (zero-byte blob)
        
        Args:
            key: Unique blob identifier
            data: Opaque byte content
            tx: Optional transaction scope
            metadata: Optional structured metadata (JSON-safe)
            if_not_exists: Fail if key already exists
        
        Returns:
            BlobRef: Reference to stored blob
        
        Raises:
            BackendConflict: If if_not_exists=True and key exists
            BackendInvariantViolation: If key format invalid or data too large
            BackendQuotaExceeded: If storage quota exceeded
            BackendUnavailable: If backend unavailable
        """
        pass
    
    @abstractmethod
    def get_blob(
        self,
        key: str,
        *,
        version_id: Optional[str] = None,
    ) -> bytes:
        """
        Retrieve blob data.
        
        RULES:
          - MUST return exact data that was written
          - MUST NOT modify or decompress implicitly
          - MUST fail fast if key not found
        
        Args:
            key: Blob identifier
            version_id: Specific version (if versioning supported)
        
        Returns:
            bytes: Exact blob content
        
        Raises:
            BackendKeyNotFound: If key does not exist
            BackendDataCorruption: If data integrity check fails
            BackendUnavailable: If backend unavailable
        """
        pass
    
    @abstractmethod
    def exists_blob(self, key: str) -> bool:
        """
        Check if blob exists.
        
        MUST be cheaper than get_blob when possible.
        MUST be consistent with get_blob visibility.
        
        Args:
            key: Blob identifier
        
        Returns:
            bool: True if exists, False otherwise
        """
        pass
    
    @abstractmethod
    def delete_blob(
        self,
        key: str,
        *,
        tx: Optional[BackendTransaction] = None,
        version_id: Optional[str] = None,
    ) -> None:
        """
        Delete blob.
        
        RULES:
          - MUST be idempotent (deleting non-existent key is OK)
          - If versioning supported, may delete specific version
          - MUST NOT leave partial state
        
        Args:
            key: Blob identifier
            tx: Optional transaction scope
            version_id: Specific version to delete (if versioning supported)
        
        Raises:
            BackendInvariantViolation: If version_id required but not provided
            BackendUnavailable: If backend unavailable
        """
        pass
    
    @abstractmethod
    def list_blobs(
        self,
        prefix: str = "",
        *,
        limit: Optional[int] = None,
    ) -> Iterable[str]:
        """
        List blob keys by prefix.
        
        OPTIONAL SUPPORT - check capabilities.supports_prefix_listing.
        
        RULES:
          - Results SHOULD be deterministically ordered
          - Empty prefix = list all keys
          - MUST NOT include deleted keys
        
        Args:
            prefix: Key prefix filter
            limit: Maximum results to return
        
        Returns:
            Iterable[str]: Matching keys
        
        Raises:
            BackendUnsupportedOperation: If listing not supported
        """
        pass
    
    # =========================================================================
    # METADATA OPERATIONS (SMALL, STRUCTURED)
    # =========================================================================
    
    @abstractmethod
    def put_metadata(
        self,
        key: str,
        value: Dict[str, Any],
        *,
        tx: Optional[BackendTransaction] = None,
    ) -> None:
        """
        Store structured metadata.
        
        RULES:
          - Value MUST be JSON-safe (no arbitrary Python objects)
          - MUST be small (<1MB typical)
          - Schema enforcement happens elsewhere
        
        Args:
            key: Metadata key
            value: JSON-safe dictionary
            tx: Optional transaction scope
        
        Raises:
            BackendInvariantViolation: If value not JSON-safe or too large
            BackendUnavailable: If backend unavailable
        """
        pass
    
    @abstractmethod
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """
        Retrieve structured metadata.
        
        Args:
            key: Metadata key
        
        Returns:
            Dict[str, Any]: Metadata value
        
        Raises:
            BackendKeyNotFound: If key not found
            BackendDataCorruption: If metadata corrupted
        """
        pass
    
    @abstractmethod
    def query_metadata(
        self,
        prefix: str = "",
        *,
        filters: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> Iterable[MetadataEntry]:
        """
        Query metadata by prefix and optional filters.
        
        OPTIONAL SUPPORT - check capabilities.supports_metadata_indexing.
        
        Args:
            prefix: Key prefix filter
            filters: Optional field filters (backend-specific)
            limit: Maximum results
        
        Returns:
            Iterable[MetadataEntry]: Matching entries
        
        Raises:
            BackendUnsupportedOperation: If queries not supported
        """
        pass
    
    # =========================================================================
    # VERSIONING (MANDATORY IF DECLARED)
    # =========================================================================
    
    @abstractmethod
    def list_versions(self, key: str) -> Iterable[str]:
        """
        List all versions of a key.
        
        OPTIONAL SUPPORT - check capabilities.supports_versioning.
        
        RULES:
          - Versions MUST be immutable
          - Ordering MUST be stable (ideally chronological)
          - MUST include current version
        
        Args:
            key: Blob key
        
        Returns:
            Iterable[str]: Version IDs
        
        Raises:
            BackendUnsupportedOperation: If versioning not supported
            BackendKeyNotFound: If key never existed
        """
        pass
    
    @abstractmethod
    def get_version_metadata(self, key: str, version_id: str) -> BlobRef:
        """
        Get metadata for specific version.
        
        Args:
            key: Blob key
            version_id: Version identifier
        
        Returns:
            BlobRef: Version reference with metadata
        
        Raises:
            BackendUnsupportedOperation: If versioning not supported
            BackendKeyNotFound: If version not found
        """
        pass
    
    # =========================================================================
    # CONSISTENCY BARRIERS
    # =========================================================================
    
    @abstractmethod
    def flush(self) -> None:
        """
        Ensure all prior writes are visible.
        
        OPTIONAL SUPPORT - check capabilities.supports_flush.
        
        This is an explicit durability barrier.
        After flush() returns, all prior writes MUST be:
          - Visible to subsequent reads
          - Durable according to durability_level
        
        No-op allowed ONLY if declared in capabilities.
        
        Raises:
            BackendUnsupportedOperation: If flush not supported
            BackendUnavailable: If backend unavailable
        """
        pass
    
    # =========================================================================
    # BULK OPERATIONS (OPTIONAL)
    # =========================================================================
    
    def bulk_delete(
        self,
        keys: List[str],
        *,
        tx: Optional[BackendTransaction] = None,
    ) -> None:
        """
        Delete multiple blobs efficiently.
        
        OPTIONAL SUPPORT - check capabilities.supports_bulk_delete.
        
        Default implementation calls delete_blob() in loop.
        Backends MAY override for efficiency.
        
        MUST be atomic if in transaction.
        MUST be idempotent.
        
        Args:
            keys: List of keys to delete
            tx: Optional transaction scope
        
        Raises:
            BackendUnsupportedOperation: If bulk delete not supported
        """
        if not self.capabilities.supports_bulk_delete:
            raise BackendUnsupportedOperation(
                f"{self.__class__.__name__} does not support bulk delete"
            )
        
        # Default implementation
        for key in keys:
            self.delete_blob(key, tx=tx)
    
    # =========================================================================
    # CONTEXT MANAGER PROTOCOL
    # =========================================================================
    
    def __enter__(self) -> "PersistenceBackend":
        """Context manager entry - opens backend."""
        self.open()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - closes backend."""
        self.close()


# =============================================================================
# NULL TRANSACTION (FOR BACKENDS WITHOUT TRANSACTION SUPPORT)
# =============================================================================


class NullTransaction(BackendTransaction):
    """
    No-op transaction for backends that don't support transactions.
    
    Operations outside transaction scope are immediately visible.
    """
    
    def __init__(self):
        self._active = True
    
    def commit(self) -> None:
        """No-op commit."""
        self._active = False
    
    def rollback(self) -> None:
        """No-op rollback."""
        self._active = False
    
    def is_active(self) -> bool:
        """Check if active."""
        return self._active


# =============================================================================
# TYPE PROTOCOLS (FOR DUCK TYPING COMPATIBILITY)
# =============================================================================


class BlobStorage(Protocol):
    """Minimal blob storage protocol (duck typing)."""
    
    def put_blob(self, key: str, data: bytes, **kwargs) -> BlobRef:
        ...
    
    def get_blob(self, key: str, **kwargs) -> bytes:
        ...
    
    def exists_blob(self, key: str) -> bool:
        ...
    
    def delete_blob(self, key: str, **kwargs) -> None:
        ...


class MetadataStorage(Protocol):
    """Minimal metadata storage protocol (duck typing)."""
    
    def put_metadata(self, key: str, value: Dict[str, Any], **kwargs) -> None:
        ...
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        ...


# =============================================================================
# VERSION INFO
# =============================================================================

__version__ = "1.0.0"
__contract_version__ = "1.0.0"  # Increment on breaking changes

# This contract is the law. All backends must obey.