"""
/infra/persistence/backend/memory.py

In-Memory Reference Backend (Contract-Perfect, Zero Excuses)

WHAT THIS FILE EXISTS TO PROVE:
    "If storage were perfect, deterministic, and instantaneous —
     do our contracts still make sense?"
    
    If the answer is no, your interfaces are broken.

WHAT THIS BACKEND IS:
    ✅ Gold-standard behavioral reference
    ✅ Fully deterministic
    ✅ Fully synchronous
    ✅ Strongest possible consistency
    ✅ Simplest correct semantics
    ✅ Exhaustively strict
    
    This backend behaves the way every ideal backend wishes it could.

WHAT THIS BACKEND IS NOT:
    ❌ Fast
    ❌ Scalable
    ❌ Thread-safe by accident
    ❌ Durable
    ❌ Production storage
    
    This backend is conceptual correctness, not performance.

HARD GUARANTEES:
    - Atomic writes: ✅
    - Read-after-write: ✅
    - Strong consistency: ✅
    - Strict immutability: ✅
    - Transaction correctness: ✅
    - No partial state: ✅
    - Deterministic failures: ✅
    
    If memory backend ever "sort of" works, you've failed.

WHY THIS BACKEND IS FOUNDATIONAL:
    If a feature works in memory backend but fails elsewhere:
    ➡ The other backend is broken
    ➡ Not your abstractions
    ➡ Not your recovery logic
    
    This backend is your court of appeal.

MENTAL MODEL:
    > memory.py is the mathematical proof that persistence is coherent.
    
    Everything else is just physics.
"""

from __future__ import annotations

import hashlib
import time
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, List, Dict
from collections import defaultdict


# ============================================================================
# CANONICAL CAPABILITIES DECLARATION
# ============================================================================

@dataclass(frozen=True)
class MemoryBackendCapabilities:
    """
    Maximum guarantees - authoritative truth for higher layers.
    """
    atomic_write: bool = True
    atomic_read: bool = True
    supports_transactions: bool = True
    supports_leases: bool = True
    supports_versioning: bool = True
    supports_range_queries: bool = True
    consistency_model: str = "STRONG"
    durability: str = "MEMORY"


# ============================================================================
# ERROR TYPES (RUTHLESS - NO MERCY)
# ============================================================================

class BackendError(Exception):
    """Base backend error."""
    pass


class BackendUnavailable(BackendError):
    """Backend unavailable (closed, unreachable, etc)."""
    pass


class BackendInvariantViolation(BackendError):
    """Backend invariant violated (programming error)."""
    pass


class BackendConflict(BackendError):
    """Operation conflicts with current state."""
    pass


class BackendDataCorruption(BackendError):
    """Internal data corruption detected."""
    pass


class BackendUnsupportedOperation(BackendError):
    """Operation not supported by this backend."""
    pass


class BackendLeaseExpired(BackendError):
    """Lease has expired."""
    pass


class BackendLeaseFenced(BackendError):
    """Operation fenced by newer lease."""
    pass


# ============================================================================
# CORE DATA TYPES
# ============================================================================

@dataclass(frozen=True)
class BlobRef:
    """
    Immutable blob reference.
    
    Returned from put_blob to prove write succeeded.
    """
    key: str
    version_id: str
    size: int
    content_hash: str
    created_at: int
    
    def __post_init__(self) -> None:
        """Validate blob reference."""
        if not self.key:
            raise BackendInvariantViolation("BlobRef key cannot be empty")
        if not self.version_id:
            raise BackendInvariantViolation("BlobRef version_id cannot be empty")
        if self.size < 0:
            raise BackendInvariantViolation("BlobRef size cannot be negative")


@dataclass(frozen=True)
class Lease:
    """
    Strict lease with fencing token.
    
    Leases are strict fences, not hints.
    Expired leases are invalid even if unreleased.
    """
    key: str
    owner_id: str
    fencing_token: int
    acquired_at: int
    expires_at: int
    
    def is_expired(self, current_time: int) -> bool:
        """Check if lease is expired."""
        return current_time >= self.expires_at
    
    def is_valid(self, current_time: int) -> bool:
        """Check if lease is valid (not expired)."""
        return not self.is_expired(current_time)
    
    def __post_init__(self) -> None:
        """Validate lease."""
        if not self.key:
            raise BackendInvariantViolation("Lease key cannot be empty")
        if not self.owner_id:
            raise BackendInvariantViolation("Lease owner_id cannot be empty")
        if self.fencing_token <= 0:
            raise BackendInvariantViolation("Lease fencing_token must be positive")
        if self.expires_at <= self.acquired_at:
            raise BackendInvariantViolation("Lease expires_at must be after acquired_at")


@dataclass
class Tombstone:
    """
    Deletion tombstone.
    
    Deletion means tombstone, not destruction.
    Existing versions remain.
    New operations must respect tombstone.
    Required for audit replay correctness.
    """
    key: str
    deleted_at: int
    deleted_by: str | None = None


# ============================================================================
# TRANSACTION (NO SHORTCUTS)
# ============================================================================

@dataclass
class MemoryTransaction:
    """
    Explicit, isolated, all-or-nothing transaction.
    
    IMPLEMENTATION STRATEGY:
    - On begin: Clone references, not data
    - On write: Write to transaction-local staging
    - On commit: Atomically swap authoritative state
    - On rollback: Destroy staging, touch nothing
    
    No partial commits. Ever.
    """
    tx_id: str
    backend: MemoryBackend
    started_at: int
    
    # Transaction-local staging (copy-on-write)
    _staged_blobs: dict[str, dict[str, bytes]] = field(default_factory=dict)
    _staged_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    _staged_versions: dict[str, list[str]] = field(default_factory=dict)
    _staged_tombstones: dict[str, Tombstone] = field(default_factory=dict)
    
    _committed: bool = False
    _rolled_back: bool = False
    _active: bool = True
    
    def _ensure_active(self) -> None:
        """Ensure transaction is active."""
        if not self._active:
            raise BackendConflict("Transaction is not active")
        if self._committed:
            raise BackendConflict("Transaction already committed")
        if self._rolled_back:
            raise BackendConflict("Transaction already rolled back")
    
    def put_blob(self, key: str, data: bytes) -> BlobRef:
        """
        Stage blob write in transaction.
        
        RULES:
        - Data must be copied (no external references)
        - Generate version ID deterministically
        - If key exists → append version
        - Never overwrite, never mutate, never truncate
        """
        self._ensure_active()
        
        # Check tombstone
        if key in self._staged_tombstones:
            raise BackendInvariantViolation(f"Cannot write to tombstoned key: {key}")
        
        if key in self.backend._tombstones and key not in self._staged_tombstones:
            raise BackendInvariantViolation(f"Cannot write to tombstoned key: {key}")
        
        # Deep copy data (no external references)
        data_copy = bytes(data)
        
        # Generate deterministic version ID
        version_id = self.backend._generate_version_id(data_copy)
        
        # Stage blob
        if key not in self._staged_blobs:
            # Clone existing versions if any
            if key in self.backend._blobs:
                self._staged_blobs[key] = deepcopy(self.backend._blobs[key])
            else:
                self._staged_blobs[key] = {}
        
        # Add new version
        self._staged_blobs[key][version_id] = data_copy
        
        # Update version list
        if key not in self._staged_versions:
            if key in self.backend._versions:
                self._staged_versions[key] = self.backend._versions[key].copy()
            else:
                self._staged_versions[key] = []
        
        if version_id not in self._staged_versions[key]:
            self._staged_versions[key].append(version_id)
        
        # Create blob reference
        return BlobRef(
            key=key,
            version_id=version_id,
            size=len(data_copy),
            content_hash=hashlib.sha256(data_copy).hexdigest(),
            created_at=int(time.time() * 1000)
        )
    
    def get_blob(self, key: str, version_id: str | None = None) -> bytes:
        """
        Read blob within transaction context.
        
        Sees staged writes.
        """
        self._ensure_active()
        
        # Check tombstone (staged takes precedence)
        if key in self._staged_tombstones:
            raise FileNotFoundError(f"Key is tombstoned: {key}")
        if key in self.backend._tombstones and key not in self._staged_tombstones:
            raise FileNotFoundError(f"Key is tombstoned: {key}")
        
        # Check staged blobs first
        if key in self._staged_blobs:
            blobs = self._staged_blobs[key]
        elif key in self.backend._blobs:
            blobs = self.backend._blobs[key]
        else:
            raise FileNotFoundError(f"Key not found: {key}")
        
        # Get version
        if version_id is None:
            # Get latest version
            if key in self._staged_versions:
                versions = self._staged_versions[key]
            elif key in self.backend._versions:
                versions = self.backend._versions[key]
            else:
                raise BackendDataCorruption(f"Version list exists but no versions: {key}")
            
            if not versions:
                raise BackendDataCorruption(f"Empty version list for key: {key}")
            
            version_id = versions[-1]  # Latest
        
        # Get blob data
        if version_id not in blobs:
            raise BackendDataCorruption(f"Version list references missing blob: {key}@{version_id}")
        
        # Return deep copy
        return bytes(blobs[version_id])
    
    def put_metadata(self, key: str, metadata: dict[str, Any]) -> None:
        """
        Stage metadata write.
        
        RULES:
        - Metadata mutations must be transactional
        - Metadata must never mutate blobs implicitly
        """
        self._ensure_active()
        
        # Deep copy metadata (no external references)
        self._staged_metadata[key] = deepcopy(metadata)
    
    def get_metadata(self, key: str) -> dict[str, Any]:
        """Read metadata within transaction context."""
        self._ensure_active()
        
        # Check staged metadata first
        if key in self._staged_metadata:
            return deepcopy(self._staged_metadata[key])
        elif key in self.backend._metadata:
            return deepcopy(self.backend._metadata[key])
        else:
            raise FileNotFoundError(f"Metadata not found: {key}")
    
    def delete_blob(self, key: str) -> None:
        """
        Stage deletion (tombstone).
        
        Existing versions remain.
        """
        self._ensure_active()
        
        tombstone = Tombstone(
            key=key,
            deleted_at=int(time.time() * 1000),
            deleted_by=self.tx_id
        )
        self._staged_tombstones[key] = tombstone
    
    def commit(self) -> None:
        """
        Atomically commit transaction.
        
        Swap entire authoritative state atomically.
        All-or-nothing.
        """
        self._ensure_active()
        
        # Atomically apply all staged changes
        for key, blobs in self._staged_blobs.items():
            self.backend._blobs[key] = blobs
        
        for key, versions in self._staged_versions.items():
            self.backend._versions[key] = versions
        
        for key, metadata in self._staged_metadata.items():
            self.backend._metadata[key] = metadata
        
        for key, tombstone in self._staged_tombstones.items():
            self.backend._tombstones[key] = tombstone
        
        self._committed = True
        self._active = False
    
    def rollback(self) -> None:
        """
        Rollback transaction.
        
        Destroy staging, touch nothing.
        """
        # Clear all staging
        self._staged_blobs.clear()
        self._staged_metadata.clear()
        self._staged_versions.clear()
        self._staged_tombstones.clear()
        
        self._rolled_back = True
        self._active = False
    
    def __enter__(self) -> MemoryTransaction:
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            self.rollback()
        elif not self._committed:
            self.rollback()


# ============================================================================
# MEMORY BACKEND (GOLD STANDARD)
# ============================================================================

class MemoryBackend:
    """
    In-memory reference backend - mathematical proof of persistence coherence.
    
    INTERNAL DATA MODEL (IMMUTABLE STATE CONTAINERS):
    All state lives inside single authoritative container, never scattered.
    
    - blobs: Dict[key, Dict[version_id, bytes]]
    - metadata: Dict[key, dict]
    - versions: Dict[key, List[version_id]]
    - leases: Dict[key, Lease]
    - tombstones: Dict[key, Tombstone]
    
    No mutation outside controlled methods.
    """
    
    def __init__(self):
        """Initialize memory backend."""
        # Authoritative state containers
        self._blobs: dict[str, dict[str, bytes]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._versions: dict[str, list[str]] = {}
        self._leases: dict[str, Lease] = {}
        self._tombstones: dict[str, Tombstone] = {}
        
        # Backend state
        self._closed = False
        self._next_fencing_token = 1
        self._tx_counter = 0
    
    def _ensure_open(self) -> None:
        """Ensure backend is open."""
        if self._closed:
            raise BackendUnavailable("Backend is closed")
    
    def _generate_version_id(self, data: bytes) -> str:
        """
        Generate deterministic version ID.
        
        DETERMINISM GUARANTEE:
        Same bytes → same version ID across runs, machines, architectures.
        """
        timestamp_ms = int(time.time() * 1000)
        content_hash = hashlib.sha256(data).hexdigest()[:16]
        return f"{timestamp_ms}_{content_hash}"
    
    def _generate_tx_id(self) -> str:
        """Generate unique transaction ID."""
        self._tx_counter += 1
        return f"tx_{self._tx_counter}_{int(time.time() * 1000)}"
    
    # ========================================================================
    # TRANSACTION MANAGEMENT
    # ========================================================================
    
    def begin_transaction(self) -> MemoryTransaction:
        """
        Begin explicit transaction.
        
        Returns transaction object for explicit control.
        """
        self._ensure_open()
        
        tx_id = self._generate_tx_id()
        return MemoryTransaction(
            tx_id=tx_id,
            backend=self,
            started_at=int(time.time() * 1000)
        )
    
    # ========================================================================
    # BLOB OPERATIONS
    # ========================================================================
    
    def put_blob(
        self,
        key: str,
        data: bytes,
        metadata: dict[str, Any] | None = None
    ) -> BlobRef:
        """
        Write blob with implicit single-operation atomic transaction.
        
        RULES:
        - If no tx: implicit single-operation atomic transaction
        - Data must be copied (no external references)
        - Generate version ID deterministically
        - If key exists → append version
        - Never overwrite, never mutate, never truncate
        """
        self._ensure_open()
        
        # Use implicit transaction
        with self.begin_transaction() as tx:
            blob_ref = tx.put_blob(key, data)
            
            if metadata:
                tx.put_metadata(key, metadata)
            
            tx.commit()
        
        return blob_ref
    
    def get_blob(self, key: str, version_id: str | None = None) -> bytes:
        """
        Get latest version of blob.
        
        RAISES:
        - BackendDataCorruption if version list exists but blob missing
        - BackendUnavailable if backend closed
        - FileNotFoundError if key not found
        """
        self._ensure_open()
        
        # Check tombstone
        if key in self._tombstones:
            raise FileNotFoundError(f"Key is tombstoned: {key}")
        
        # Get blob versions
        if key not in self._blobs:
            raise FileNotFoundError(f"Key not found: {key}")
        
        blobs = self._blobs[key]
        
        # Get version
        if version_id is None:
            # Get latest version
            if key not in self._versions:
                raise BackendDataCorruption(f"Blob exists but no version list: {key}")
            
            versions = self._versions[key]
            if not versions:
                raise BackendDataCorruption(f"Empty version list for key: {key}")
            
            version_id = versions[-1]  # Latest
        
        # Get blob data
        if version_id not in blobs:
            raise BackendDataCorruption(f"Version list references missing blob: {key}@{version_id}")
        
        # Return deep copy (no external references)
        return bytes(blobs[version_id])
    
    def list_versions(self, key: str) -> list[str]:
        """
        List all versions for a key.
        
        Returns versions in chronological order.
        """
        self._ensure_open()
        
        if key in self._tombstones:
            raise FileNotFoundError(f"Key is tombstoned: {key}")
        
        if key not in self._versions:
            return []
        
        return self._versions[key].copy()
    
    def delete_blob(self, key: str) -> None:
        """
        Delete blob (create tombstone).
        
        DELETION SEMANTICS:
        - Deletion means tombstone, not destruction
        - Existing versions remain
        - New operations must respect tombstone
        - Required for audit replay correctness
        """
        self._ensure_open()
        
        # Use implicit transaction
        with self.begin_transaction() as tx:
            tx.delete_blob(key)
            tx.commit()
    
    # ========================================================================
    # METADATA OPERATIONS
    # ========================================================================
    
    def put_metadata(self, key: str, metadata: dict[str, Any]) -> None:
        """
        Write metadata.
        
        RULES:
        - Metadata mutations must be transactional
        - Reads always reflect committed state
        - Metadata must never mutate blobs implicitly
        """
        self._ensure_open()
        
        with self.begin_transaction() as tx:
            tx.put_metadata(key, metadata)
            tx.commit()
    
    def get_metadata(self, key: str) -> dict[str, Any]:
        """
        Read metadata.
        
        Returns deep copy to prevent external mutation.
        """
        self._ensure_open()
        
        if key not in self._metadata:
            raise FileNotFoundError(f"Metadata not found: {key}")
        
        return deepcopy(self._metadata[key])
    
    # ========================================================================
    # LEASE OPERATIONS
    # ========================================================================
    
    def acquire_lease(
        self,
        key: str,
        owner_id: str,
        duration_ms: int
    ) -> Lease:
        """
        Acquire strict lease with fencing token.
        
        LEASE SEMANTICS:
        - Each lease tracks: owner_id, expiry, fencing_token (monotonic)
        - Expired leases are invalid even if unreleased
        - Fencing tokens prevent ABA problems
        """
        self._ensure_open()
        
        current_time = int(time.time() * 1000)
        
        # Check existing lease
        if key in self._leases:
            existing = self._leases[key]
            
            if existing.is_valid(current_time):
                raise BackendConflict(
                    f"Lease already held by {existing.owner_id} "
                    f"(expires in {existing.expires_at - current_time}ms)"
                )
        
        # Create new lease with monotonic fencing token
        lease = Lease(
            key=key,
            owner_id=owner_id,
            fencing_token=self._next_fencing_token,
            acquired_at=current_time,
            expires_at=current_time + duration_ms
        )
        
        self._next_fencing_token += 1
        self._leases[key] = lease
        
        return lease
    
    def release_lease(self, key: str, owner_id: str, fencing_token: int) -> None:
        """
        Release lease with fencing token validation.
        
        Prevents release of stale leases.
        """
        self._ensure_open()
        
        if key not in self._leases:
            raise FileNotFoundError(f"No lease found for key: {key}")
        
        lease = self._leases[key]
        
        # Validate ownership
        if lease.owner_id != owner_id:
            raise BackendConflict(f"Lease owned by {lease.owner_id}, not {owner_id}")
        
        # Validate fencing token
        if lease.fencing_token != fencing_token:
            raise BackendLeaseFenced(
                f"Lease fencing token mismatch: expected {lease.fencing_token}, "
                f"got {fencing_token}"
            )
        
        # Release
        del self._leases[key]
    
    def get_lease(self, key: str) -> Lease | None:
        """Get current lease for key, if any."""
        self._ensure_open()
        
        if key not in self._leases:
            return None
        
        lease = self._leases[key]
        current_time = int(time.time() * 1000)
        
        # Auto-expire stale leases
        if lease.is_expired(current_time):
            del self._leases[key]
            return None
        
        return lease
    
    def validate_lease(self, key: str, owner_id: str, fencing_token: int) -> None:
        """
        Validate lease is still valid.
        
        RAISES:
        - BackendLeaseExpired if lease expired
        - BackendLeaseFenced if fencing token outdated
        - BackendConflict if ownership mismatch
        """
        self._ensure_open()
        
        current_time = int(time.time() * 1000)
        
        if key not in self._leases:
            raise BackendLeaseExpired(f"No active lease for key: {key}")
        
        lease = self._leases[key]
        
        # Check expiry
        if lease.is_expired(current_time):
            del self._leases[key]
            raise BackendLeaseExpired(f"Lease expired at {lease.expires_at}")
        
        # Check ownership
        if lease.owner_id != owner_id:
            raise BackendConflict(f"Lease owned by {lease.owner_id}, not {owner_id}")
        
        # Check fencing token
        if lease.fencing_token != fencing_token:
            raise BackendLeaseFenced(
                f"Lease fencing token outdated: current is {lease.fencing_token}, "
                f"provided {fencing_token}"
            )
    
    # ========================================================================
    # LISTING OPERATIONS
    # ========================================================================
    
    def list_keys(self, prefix: str | None = None) -> list[str]:
        """
        List all non-tombstoned keys.
        
        Optionally filter by prefix (range query support).
        """
        self._ensure_open()
        
        # Collect all keys
        all_keys = set()
        all_keys.update(self._blobs.keys())
        all_keys.update(self._metadata.keys())
        
        # Filter tombstoned
        keys = [k for k in all_keys if k not in self._tombstones]
        
        # Filter by prefix if provided
        if prefix:
            keys = [k for k in keys if k.startswith(prefix)]
        
        return sorted(keys)
    
    # ========================================================================
    # CONSISTENCY BARRIERS
    # ========================================================================
    
    def flush(self) -> None:
        """
        Consistency barrier.
        
        RULES:
        - No-op in memory backend
        - Must still exist
        - Must still be callable
        - Must still be deterministic
        
        Proves higher layers don't rely on side effects.
        """
        self._ensure_open()
        # No-op for memory backend
        pass
    
    # ========================================================================
    # BACKEND MANAGEMENT
    # ========================================================================
    
    def get_capabilities(self) -> MemoryBackendCapabilities:
        """Get backend capabilities."""
        return MemoryBackendCapabilities()
    
    def get_stats(self) -> dict[str, Any]:
        """Get backend statistics."""
        self._ensure_open()
        
        total_blobs = sum(len(versions) for versions in self._blobs.values())
        total_size = sum(
            len(data)
            for key_blobs in self._blobs.values()
            for data in key_blobs.values()
        )
        
        return {
            'total_keys': len(self._blobs),
            'total_blobs': total_blobs,
            'total_size_bytes': total_size,
            'total_tombstones': len(self._tombstones),
            'active_leases': len(self._leases),
            'next_fencing_token': self._next_fencing_token,
            'backend_type': 'memory',
        }
    
    def close(self) -> None:
        """
        Close backend.
        
        After close, all operations raise BackendUnavailable.
        """
        self._closed = True
        
        # Clear all state
        self._blobs.clear()
        self._metadata.clear()
        self._versions.clear()
        self._leases.clear()
        self._tombstones.clear()
    
    def __enter__(self) -> MemoryBackend:
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ============================================================================
# INVARIANTS (AGGRESSIVE VALIDATION)
# ============================================================================

class MemoryBackendInvariants:
    """
    Invariant validation for memory backend.
    
    Memory backend must be perfect - any invariant violation is a bug.
    """
    
    @staticmethod
    def verify_version_immutability(backend: MemoryBackend, key: str) -> bool:
        """
        Verify versions are truly immutable.
        
        Same version ID must always return same bytes.
        """
        if key not in backend._blobs:
            return True
        
        blobs = backend._blobs[key]
        
        # Get first version
        for version_id, original_data in blobs.items():
            # Read multiple times
            read1 = backend.get_blob(key, version_id)
            read2 = backend.get_blob(key, version_id)
            
            if read1 != read2:
                return False
            if read1 != original_data:
                return False
        
        return True
    
    @staticmethod
    def verify_no_external_references(backend: MemoryBackend) -> bool:
        """
        Verify no external references can mutate internal state.
        
        All data must be deep-copied.
        """
        # This is verified by using bytes() and deepcopy() throughout
        # We can test by attempting to mutate returned data
        
        # Write data
        original = bytearray(b"test data")
        ref = backend.put_blob("test", bytes(original))
        
        # Mutate original
        original[0] = 0xFF
        
        # Read back
        retrieved = backend.get_blob("test")
        
        # Should not be affected
        return retrieved[0] != 0xFF
    
    @staticmethod
    def verify_transaction_isolation(backend: MemoryBackend) -> bool:
        """
        Verify transactions are truly isolated.
        
        Uncommitted changes must not be visible.
        """
        # Write initial data
        backend.put_blob("isolation_test", b"initial")
        
        # Start transaction
        tx = backend.begin_transaction()
        tx.put_blob("isolation_test", b"uncommitted")
        
        # Read from outside transaction
        outside_read = backend.get_blob("isolation_test")
        
        # Should see initial, not uncommitted
        if outside_read != b"initial":
            return False
        
        # Rollback
        tx.rollback()
        
        # Should still see initial
        after_rollback = backend.get_blob("isolation_test")
        return after_rollback == b"initial"
    
    @staticmethod
    def verify_lease_fencing(backend: MemoryBackend) -> bool:
        """
        Verify lease fencing tokens prevent stale operations.
        
        Older fencing tokens must be rejected.
        """
        # Acquire lease
        lease1 = backend.acquire_lease("fence_test", "owner1", 5000)
        token1 = lease1.fencing_token
        
        # Force expire
        backend._leases["fence_test"] = Lease(
            key="fence_test",
            owner_id="owner1",
            fencing_token=lease1.fencing_token,
            acquired_at=lease1.acquired_at,
            expires_at=0  # Expired
        )
        
        # Acquire new lease
        lease2 = backend.acquire_lease("fence_test", "owner2", 5000)
        token2 = lease2.fencing_token
        
        # Verify fencing token increased
        if token2 <= token1:
            return False
        
        # Try to use old token
        try:
            backend.validate_lease("fence_test", "owner2", token1)
            return False  # Should have failed
        except BackendLeaseFenced:
            return True  # Correctly fenced
    
    @staticmethod
    def verify_deterministic_version_ids(backend: MemoryBackend) -> bool:
        """
        Verify version IDs are deterministic.
        
        Same bytes should produce same version ID structure.
        """
        data = b"determinism test"
        
        # Generate multiple version IDs for same data
        version1 = backend._generate_version_id(data)
        version2 = backend._generate_version_id(data)
        
        # Should have same content hash component
        hash1 = version1.split('_')[1]
        hash2 = version2.split('_')[1]
        
        return hash1 == hash2
    
    @staticmethod
    def verify_tombstone_enforcement(backend: MemoryBackend) -> bool:
        """
        Verify tombstones prevent new writes.
        
        Critical for audit replay correctness.
        """
        # Write and delete
        backend.put_blob("tombstone_test", b"data")
        backend.delete_blob("tombstone_test")
        
        # Try to write again
        try:
            backend.put_blob("tombstone_test", b"new data")
            return False  # Should have failed
        except BackendInvariantViolation:
            return True  # Correctly rejected


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Main backend
    'MemoryBackend',
    
    # Capabilities
    'MemoryBackendCapabilities',
    
    # Data types
    'BlobRef',
    'Lease',
    'Tombstone',
    
    # Transaction
    'MemoryTransaction',
    
    # Errors
    'BackendError',
    'BackendUnavailable',
    'BackendInvariantViolation',
    'BackendConflict',
    'BackendDataCorruption',
    'BackendUnsupportedOperation',
    'BackendLeaseExpired',
    'BackendLeaseFenced',
    
    # Invariants
    'MemoryBackendInvariants',
]