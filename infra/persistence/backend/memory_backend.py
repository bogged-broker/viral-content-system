"""
In-Memory Deterministic KV Backend (Test & Ephemeral Authority)

This module provides a pure in-memory implementation of the persistence backend contract.
It exists for deterministic unit/integration testing, local development, ephemeral execution,
replay validation, and performance benchmarking without I/O.

This backend must:
- Fully conform to the backend interface
- Preserve deterministic semantics
- Support atomic-like behavior within process scope
- NEVER introduce behavior differences from durable backends

If behavior diverges from persistent backends, tests lie.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from copy import deepcopy
import time

# Import BackendBase contract and exceptions
from infra.persistence.backends.backend_base import (
    BackendBase,
    BackendCapabilities,
    DurabilityLevel,
    IsolationLevel,
    WriteMode,
    PersistenceError,
    NonRetryablePersistenceError,
    RetryablePersistenceError,
    KeyNotFoundError,
    KeyExistsError,
    VersionMismatchError,
)


# ============================================================================
# Stored Value Model
# ============================================================================

@dataclass
class StoredValue:
    """
    Internal representation of stored data.
    
    Tracks value, version, and metadata for versioning support.
    """
    value: bytes
    version: int
    timestamp: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def clone(self) -> StoredValue:
        """Create deep copy to prevent mutation leaks."""
        return StoredValue(
            value=bytes(self.value),  # Copy bytes
            version=self.version,
            timestamp=self.timestamp,
            metadata=deepcopy(self.metadata)
        )


# ============================================================================
# Clock Abstraction (For Determinism)
# ============================================================================

class Clock:
    """
    Clock abstraction for deterministic timestamp generation.
    Allows injection of custom time source for testing.
    
    CRITICAL: Default behavior is deterministic (monotonic counter starting at 0.0)
    to ensure replay determinism. Wall-clock time must be explicitly injected
    via time_fn parameter if needed for non-replay scenarios.
    """
    
    def __init__(self, time_fn: Optional[Callable[[], float]] = None):
        """
        Initialize clock.
        
        Args:
            time_fn: Optional time function (defaults to deterministic monotonic counter)
                     For replay determinism, default uses monotonic counter starting at 0.0.
                     Wall-clock time (time.time) must be explicitly provided if needed.
        """
        # Default to deterministic monotonic counter for replay correctness
        # This ensures identical sequences produce identical timestamps
        self._monotonic_counter = 0.0
        self._lock = threading.Lock()
        self._use_deterministic = time_fn is None
        
        if time_fn is not None:
            self._time_fn = time_fn
        else:
            # Deterministic default: will use _monotonic_counter directly
            self._time_fn = None
    
    def now(self) -> float:
        """
        Get current timestamp.
        
        Returns monotonically increasing value for determinism.
        With default deterministic clock, returns incrementing counter.
        With injected time_fn, ensures monotonicity via correction.
        """
        with self._lock:
            if self._use_deterministic:
                # Deterministic mode: return counter and increment for next call
                timestamp = self._monotonic_counter
                self._monotonic_counter += 0.000001
                return timestamp
            else:
                # Injected time_fn mode: use provided function with monotonic correction
                timestamp = self._time_fn()
                # Ensure monotonic increasing
                if timestamp <= self._monotonic_counter:
                    self._monotonic_counter += 0.000001
                    timestamp = self._monotonic_counter
                else:
                    self._monotonic_counter = timestamp
                return timestamp


# ============================================================================
# Memory Backend
# ============================================================================

class MemoryBackend(BackendBase):
    """
    Deterministic in-memory key-value backend.
    
    Provides:
    - Full contract compliance with backend_base.py
    - Atomic batch operations
    - Optimistic concurrency control
    - Deterministic behavior for testing
    - Snapshot/restore capabilities
    
    This backend is behaviorally identical to durable backends,
    differing only in durability guarantees.
    """
    
    def __init__(
        self,
        thread_safe: bool = False,
        clock: Optional[Clock] = None
    ):
        """
        Initialize memory backend.
        
        Args:
            thread_safe: Whether to enable thread-safety with locks
            clock: Optional clock instance for deterministic timestamps.
                   Defaults to deterministic monotonic counter for replay correctness.
        """
        # Primary storage
        self._store: Dict[str, StoredValue] = {}
        
        # Configuration
        self._thread_safe = thread_safe
        self._clock = clock or Clock()
        
        # Thread safety
        self._lock = threading.RLock() if thread_safe else None
        
        # Metrics
        self._metrics = {
            "read_count": 0,
            "write_count": 0,
            "delete_count": 0,
            "batch_count": 0,
        }
    
    # ========================================================================
    # Core Operations (BackendBase Contract)
    # ========================================================================
    
    def get(self, key: str) -> Optional[bytes]:
        """
        Retrieve value for key.
        
        GUARANTEES:
        - Returns exactly the last committed value for this key
        - Returns None if key does not exist (NEVER raises KeyNotFoundError)
        - No implicit decoding or deserialization
        - No metadata injection into returned bytes
        - No mutation of stored bytes
        
        Args:
            key: Storage key (non-empty string)
            
        Returns:
            Raw bytes value if key exists, None otherwise
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
        """
        self._validate_key(key)
        
        with self._maybe_lock():
            if key not in self._store:
                self._metrics["read_count"] += 1
                return None
            
            # Clone to prevent mutation leaks
            stored = self._store[key].clone()
            self._metrics["read_count"] += 1
            return stored.value
    
    def put(
        self,
        key: str,
        value: bytes,
        mode: WriteMode = WriteMode.UPSERT,
        expected_version: Optional[int] = None
    ) -> None:
        """
        Write value to key with specified write mode.
        
        GUARANTEES:
        - Atomic at the key level (all-or-nothing)
        - On success, value is durable according to backend's durability level
        - On failure, no partial state persists
        - Idempotent for UPSERT mode (same key+value → same final state)
        
        WRITE MODE SEMANTICS:
        - INSERT_ONLY: Succeeds only if key does NOT exist
        - UPSERT: Always succeeds (creates if absent, overwrites if present)
        - REPLACE_ONLY: Succeeds only if key EXISTS
        - CAS: Requires expected_version parameter, succeeds only if version matches
        
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
        """
        self._validate_key(key)
        self._validate_value(value)
        
        with self._maybe_lock():
            key_exists = key in self._store
            current_version = self._store[key].version if key_exists else 0
            
            # Handle CAS mode
            if mode == WriteMode.CAS:
                if expected_version is None:
                    raise NonRetryablePersistenceError(
                        "expected_version required for CAS mode"
                    )
                if not key_exists:
                    raise KeyNotFoundError(key)
                if current_version != expected_version:
                    raise VersionMismatchError(key, expected_version, current_version)
                # Version matches - proceed with update
                new_version = expected_version + 1
            
            # Handle INSERT_ONLY mode
            elif mode == WriteMode.INSERT_ONLY:
                if key_exists:
                    raise KeyExistsError(key)
                new_version = 1
            
            # Handle REPLACE_ONLY mode
            elif mode == WriteMode.REPLACE_ONLY:
                if not key_exists:
                    raise KeyNotFoundError(key)
                new_version = current_version + 1
            
            # Handle UPSERT mode (default)
            else:  # WriteMode.UPSERT
                if key_exists:
                    new_version = current_version + 1
                else:
                    new_version = 1
            
            # Store with new version
            self._store[key] = StoredValue(
                value=bytes(value),  # Copy bytes
                version=new_version,
                timestamp=self._clock.now()
            )
            self._metrics["write_count"] += 1
    
    def delete(self, key: str) -> None:
        """
        Delete key from storage.
        
        GUARANTEES:
        - Idempotent (safe to call multiple times)
        - Atomic (key either exists or doesn't)
        - After success, get(key) MUST return None
        - Silent if key does not exist (no error raised)
        
        Args:
            key: Storage key to delete
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
        """
        self._validate_key(key)
        
        with self._maybe_lock():
            if key in self._store:
                del self._store[key]
                self._metrics["delete_count"] += 1
    
    def exists(self, key: str) -> bool:
        """
        Check if key exists in storage.
        
        GUARANTEES:
        - Pure existence check (no side effects)
        - Does NOT mutate state
        - Does NOT create entries
        - Consistent with get() semantics
        
        Args:
            key: Storage key to check
            
        Returns:
            True if key exists, False otherwise
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
        """
        self._validate_key(key)
        
        with self._maybe_lock():
            return key in self._store
    
    # ========================================================================
    # Batch Operations (BackendBase Contract)
    # ========================================================================
    
    def batch_put(
        self,
        entries: Dict[str, bytes],
        mode: WriteMode = WriteMode.UPSERT
    ) -> None:
        """
        Write multiple key-value pairs atomically.
        
        ATOMICITY:
        All-or-nothing semantics (atomic across all keys).
        Either all writes succeed or none succeed.
        
        Args:
            entries: Dictionary mapping keys to values
            mode: Write mode (applied to all entries)
            
        Raises:
            Same exceptions as put()
        """
        with self._maybe_lock():
            # Stage changes on a copy
            staged_store = deepcopy(self._store)
            
            try:
                # Apply all writes to staged store
                for key, value in entries.items():
                    self._validate_key(key)
                    self._validate_value(value)
                    
                    key_exists = key in staged_store
                    current_version = staged_store[key].version if key_exists else 0
                    
                    # Handle CAS mode (not supported in batch_put)
                    # CAS requires per-key expected_version, but batch_put interface
                    # only accepts entries dict without version metadata.
                    # This is a base contract limitation, not implementation limitation.
                    # Use individual put() calls with WriteMode.CAS for CAS operations.
                    if mode == WriteMode.CAS:
                        raise NonRetryablePersistenceError(
                            "CAS mode not supported in batch_put: base interface does not "
                            "support per-key expected_version. Use put() with WriteMode.CAS "
                            "and expected_version parameter for CAS operations."
                        )
                    
                    # Handle INSERT_ONLY mode
                    elif mode == WriteMode.INSERT_ONLY:
                        if key_exists:
                            raise KeyExistsError(key)
                        new_version = 1
                    
                    # Handle REPLACE_ONLY mode
                    elif mode == WriteMode.REPLACE_ONLY:
                        if not key_exists:
                            raise KeyNotFoundError(key)
                        new_version = current_version + 1
                    
                    # Handle UPSERT mode (default)
                    else:  # WriteMode.UPSERT
                        if key_exists:
                            new_version = current_version + 1
                        else:
                            new_version = 1
                    
                    # Store with new version
                    staged_store[key] = StoredValue(
                        value=bytes(value),  # Copy bytes
                        version=new_version,
                        timestamp=self._clock.now()
                    )
                
                # All operations succeeded - commit changes
                self._store = staged_store
                self._metrics["batch_count"] += 1
                self._metrics["write_count"] += len(entries)
                
            except (KeyExistsError, KeyNotFoundError, VersionMismatchError):
                # Re-raise contract exceptions as-is
                raise
            except Exception as e:
                # Wrap other exceptions
                raise RetryablePersistenceError(f"Batch put failed: {e}") from e
    
    def batch_delete(self, keys: List[str]) -> None:
        """
        Delete multiple keys atomically.
        
        ATOMICITY:
        All-or-nothing semantics (atomic across all keys).
        Either all deletes succeed or none succeed.
        
        Args:
            keys: List of keys to delete
            
        Raises:
            Same exceptions as delete()
        """
        with self._maybe_lock():
            # Stage changes on a copy
            staged_store = deepcopy(self._store)
            
            try:
                # Validate all keys first
                for key in keys:
                    self._validate_key(key)
                
                # Apply all deletes to staged store and count deletions
                deleted_count = 0
                for key in keys:
                    if key in staged_store:
                        del staged_store[key]
                        deleted_count += 1
                
                # All operations succeeded - commit changes
                self._store = staged_store
                self._metrics["batch_count"] += 1
                self._metrics["delete_count"] += deleted_count
                
            except (NonRetryablePersistenceError, KeyNotFoundError, KeyExistsError, VersionMismatchError):
                # Re-raise contract exceptions as-is (preserve error taxonomy)
                raise
            except Exception as e:
                # Wrap unexpected exceptions as retryable
                raise RetryablePersistenceError(f"Batch delete failed: {e}") from e
    
    # ========================================================================
    # Helper Methods (Not part of BackendBase, but useful for testing)
    # ========================================================================
    
    def get_version(self, key: str) -> Optional[int]:
        """
        Get current version of key (helper method for testing).
        
        Args:
            key: Storage key
            
        Returns:
            Current version number if key exists, None otherwise
        """
        self._validate_key(key)
        
        with self._maybe_lock():
            if key not in self._store:
                return None
            return self._store[key].version
    
    # ========================================================================
    # Snapshot Operations
    # ========================================================================
    
    def dump_state(self) -> Dict[str, StoredValue]:
        """
        Export complete backend state.
        
        Used for replay validation and checkpointing.
        
        Returns:
            Deep copy of internal store
        """
        with self._maybe_lock():
            return deepcopy(self._store)
    
    def restore_state(self, snapshot: Dict[str, StoredValue]) -> None:
        """
        Restore backend state from snapshot.
        
        Replaces entire store with snapshot contents.
        
        Args:
            snapshot: State snapshot to restore
        """
        with self._maybe_lock():
            # Validate snapshot structure
            for key, stored_value in snapshot.items():
                self._validate_key(key)
                if not isinstance(stored_value, StoredValue):
                    raise NonRetryablePersistenceError(
                        f"Invalid snapshot: key '{key}' has wrong value type"
                    )
            
            # Replace store
            self._store = deepcopy(snapshot)
    
    def clear(self) -> None:
        """
        Clear all data from store.
        
        WARNING: Destructive operation.
        """
        with self._maybe_lock():
            self._store.clear()
    
    # ========================================================================
    # Metadata & Introspection
    # ========================================================================
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """
        Get backend-specific metadata for key.
        
        GUARANTEES:
        - Does NOT mutate state
        - Returns empty dict if key does not exist (no error)
        - Metadata MUST NOT be injected into value bytes
        
        Args:
            key: Storage key
            
        Returns:
            Dictionary of metadata (empty if key absent)
            
        Raises:
            RetryablePersistenceError: If transient failure occurs
        """
        self._validate_key(key)
        
        with self._maybe_lock():
            if key not in self._store:
                return {}
            
            stored = self._store[key]
            return {
                "version": stored.version,
                "timestamp": stored.timestamp,
                "size": len(stored.value),
                **stored.metadata
            }
    
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
        return BackendCapabilities(
            durability_level=DurabilityLevel.EPHEMERAL,
            isolation_level=IsolationLevel.SERIALIZABLE if self._thread_safe else IsolationLevel.NONE,
            supports_transactions=True,  # Supports atomic batch operations
            supports_batching=True,
            supports_cas=True,  # Supports CAS via put() with WriteMode.CAS
            thread_safe=self._thread_safe,
            process_safe=False,  # In-memory only
            distributed_safe=False  # In-memory only
        )
    
    def get_metrics(self) -> Dict[str, Any]:
        """
        Get backend metrics.
        
        Returns:
            Dictionary of metric values
        """
        with self._maybe_lock():
            return {
                **self._metrics,
                "total_keys": len(self._store),
                "total_bytes": sum(len(sv.value) for sv in self._store.values()),
            }
    
    def list_keys(self, prefix: Optional[str] = None) -> List[str]:
        """
        List all keys, optionally filtered by prefix.
        
        Args:
            prefix: Optional key prefix to filter by
            
        Returns:
            Sorted list of keys (deterministic ordering)
        """
        with self._maybe_lock():
            if prefix is None:
                keys = list(self._store.keys())
            else:
                keys = [k for k in self._store.keys() if k.startswith(prefix)]
            
            # Return sorted for determinism
            return sorted(keys)
    
    # ========================================================================
    # Validation
    # ========================================================================
    
    def _validate_key(self, key: str) -> None:
        """
        Validate key format.
        
        Args:
            key: Key to validate
            
        Raises:
            NonRetryablePersistenceError: If key is invalid
        """
        if not isinstance(key, str):
            raise NonRetryablePersistenceError(
                f"Key must be string, got {type(key).__name__}"
            )
        
        if not key:
            raise NonRetryablePersistenceError("Key cannot be empty")
        
        if len(key) > 2048:
            raise NonRetryablePersistenceError(
                f"Key exceeds maximum length (2048): {len(key)}"
            )
    
    def _validate_value(self, value: bytes) -> None:
        """
        Validate value format.
        
        Args:
            value: Value to validate
            
        Raises:
            NonRetryablePersistenceError: If value is invalid
        """
        if not isinstance(value, bytes):
            raise NonRetryablePersistenceError(
                f"Value must be bytes, got {type(value).__name__}"
            )
    
    # ========================================================================
    # Thread Safety
    # ========================================================================
    
    def _maybe_lock(self):
        """
        Context manager for optional thread-safety.
        
        Returns lock context if thread_safe=True, otherwise no-op.
        """
        if self._lock is not None:
            return self._lock
        else:
            # No-op context manager
            class NoOpContextManager:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
            return NoOpContextManager()
    
    # ========================================================================
    # String Representation
    # ========================================================================
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        metrics = self.get_metrics()
        return (
            f"MemoryBackend("
            f"keys={metrics['total_keys']}, "
            f"bytes={metrics['total_bytes']}, "
            f"thread_safe={self._thread_safe})"
        )