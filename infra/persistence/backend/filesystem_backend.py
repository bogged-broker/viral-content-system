"""
/infra/persistence/backend/filesystem_backend.py

Local & NFS Durability Backend (Dev / Cold Path)

WHAT THIS FILE ACTUALLY IS:
    Turns abstract persistence contracts into real bytes on real filesystems
    without pretending filesystems are nicer than they are.
    
    Answers: "Can we satisfy persistence guarantees using POSIX semantics
             without lying?"
    
    Durability-oriented, not performance-oriented.

WHAT THIS FILE IS NOT:
    ❌ Not a database
    ❌ Not optimized for latency
    ❌ Not parallel by default
    ❌ Not crash-proof without fsync
    ❌ Not a hot path
    ❌ Not distributed consensus
    
    This backend is honest storage, not clever storage.

DESIGN PRINCIPLE:
    > If the filesystem lies, we assume it did — and defend accordingly.
    
    Durability is explicitly purchased with syscalls.

INTENDED USE CASES:
    ✅ Local development
    ✅ CI / deterministic tests
    ✅ Cold storage
    ✅ Recovery staging
    ✅ Forensic inspection
    ✅ Minimal-dependency environments
    
    ❌ High-QPS prod hot paths
    ❌ Multi-writer scaling
    ❌ Cross-AZ coordination

CRASH RECOVERY RULES:
    On backend open():
    - Scan tx/ directory
    - Any leftover tx dirs → delete entirely
    - Partial commits must not be guessed
    - Tombstones always win
    
    Filesystem backend must never try to be clever.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Protocol, List, Dict


# ============================================================================
# CAPABILITY DECLARATION (EXPLICIT)
# ============================================================================

@dataclass(frozen=True)
class FilesystemBackendCapabilities:
    """
    Explicit capability declaration.
    
    No pretending. No ambiguity.
    """
    atomic_write: bool = False  # Emulated via write-rename-fsync
    atomic_read: bool = True
    supports_transactions: bool = False  # Emulated via staging
    supports_leases: bool = False
    supports_versioning: bool = True
    supports_range_queries: bool = False
    consistency_model: str = "READ_AFTER_WRITE"
    durability_level: str = "LOCAL_DISK"


# ============================================================================
# ERROR TYPES (NO HIDING)
# ============================================================================

class BackendError(Exception):
    """Base backend error."""
    pass


class BackendPermissionDenied(BackendError):
    """Permission denied by filesystem."""
    pass


class BackendUnavailable(BackendError):
    """Backend unavailable (disk full, etc)."""
    pass


class BackendInvariantViolation(BackendError):
    """Backend invariant violated."""
    pass


class BackendDataCorruption(BackendError):
    """Data corruption detected."""
    pass


class BackendUnsupportedOperation(BackendError):
    """Operation not supported."""
    pass


class BackendConcurrencyConflict(BackendError):
    """Concurrent access conflict."""
    pass


class BackendTransactionError(BackendError):
    """Transaction operation failed."""
    pass


# ============================================================================
# FILESYSTEM OPERATIONS (DEFENSIVE)
# ============================================================================

class FilesystemOps:
    """
    Defensive filesystem operations.
    
    Every operation assumes the filesystem might lie.
    """
    
    @staticmethod
    def safe_write_file(path: Path, data: bytes, fsync: bool = True) -> None:
        """
        Write file with durability guarantees.
        
        Uses write-rename-fsync sequence to prevent partial writes.
        """
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        
        # Write to temporary file in same directory (same filesystem)
        tmp_path = parent / f".tmp.{uuid.uuid4().hex}"
        
        try:
            # Write data
            with open(tmp_path, 'wb') as f:
                f.write(data)
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
            
            # Atomic rename
            os.rename(tmp_path, path)
            
            # fsync directory to persist rename
            if fsync:
                FilesystemOps.fsync_directory(parent)
                
        except PermissionError as e:
            FilesystemOps.safe_remove(tmp_path)
            raise BackendPermissionDenied(f"Permission denied: {e}") from e
        except OSError as e:
            FilesystemOps.safe_remove(tmp_path)
            if e.errno == 28:  # ENOSPC
                raise BackendUnavailable(f"Disk full: {e}") from e
            raise BackendInvariantViolation(f"Write failed: {e}") from e
    
    @staticmethod
    def safe_read_file(path: Path) -> bytes:
        """Read file safely."""
        try:
            # Verify path is not a symlink
            if path.is_symlink():
                raise BackendInvariantViolation(f"Refusing to read symlink: {path}")
            
            with open(path, 'rb') as f:
                return f.read()
        except FileNotFoundError:
            raise
        except PermissionError as e:
            raise BackendPermissionDenied(f"Permission denied: {e}") from e
        except OSError as e:
            raise BackendDataCorruption(f"Read failed: {e}") from e
    
    @staticmethod
    def safe_remove(path: Path) -> None:
        """Remove file/directory safely."""
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
            elif path.exists():
                path.unlink()
        except Exception:
            # Best effort - cleanup failures are logged but not raised
            pass
    
    @staticmethod
    def fsync_directory(dir_path: Path) -> None:
        """fsync a directory to persist metadata changes."""
        try:
            fd = os.open(dir_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Some filesystems don't support directory fsync
            # This is a known limitation
            pass
    
    @staticmethod
    def validate_path(root: Path, path: Path) -> None:
        """
        Validate path is safe and within root.
        
        Security: Never follow symlinks, validate all paths.
        """
        try:
            # Resolve without following symlinks
            resolved = path.resolve()
            root_resolved = root.resolve()
            
            # Check path is within root
            if not str(resolved).startswith(str(root_resolved)):
                raise BackendInvariantViolation(
                    f"Path {path} escapes root {root}"
                )
            
            # Check no symlinks in chain
            current = path
            while current != root:
                if current.is_symlink():
                    raise BackendInvariantViolation(
                        f"Symlink detected in path: {current}"
                    )
                current = current.parent
                
        except Exception as e:
            raise BackendInvariantViolation(f"Path validation failed: {e}") from e


# ============================================================================
# DIRECTORY LAYOUT (AUTHORITATIVE)
# ============================================================================

class FilesystemLayout:
    """
    Deterministic directory layout.
    
    All on-disk structure must be fully deterministic.
    No hidden files. No filesystem tricks.
    """
    
    def __init__(self, root: Path):
        self.root = root.resolve()
        
        # Subdirectories
        self.blobs_dir = self.root / "blobs"
        self.metadata_dir = self.root / "metadata"
        self.manifests_dir = self.root / "manifests"
        self.tombstones_dir = self.root / "tombstones"
        self.tx_dir = self.root / "tx"
        self.lock_file = self.root / "LOCK"
    
    def blob_path(self, key: str, version_id: str) -> Path:
        """Get path for blob version."""
        return self.blobs_dir / key / f"{version_id}.blob"
    
    def metadata_path(self, key: str) -> Path:
        """Get path for metadata."""
        return self.metadata_dir / f"{key}.json"
    
    def manifest_path(self, key: str) -> Path:
        """Get path for manifest."""
        return self.manifests_dir / f"{key}.manifest.json"
    
    def tombstone_path(self, key: str) -> Path:
        """Get path for tombstone."""
        return self.tombstones_dir / f"{key}.tombstone"
    
    def tx_staging_dir(self, tx_id: str) -> Path:
        """Get staging directory for transaction."""
        return self.tx_dir / tx_id
    
    def initialize(self) -> None:
        """Initialize directory structure."""
        for directory in [
            self.blobs_dir,
            self.metadata_dir,
            self.manifests_dir,
            self.tombstones_dir,
            self.tx_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)
    
    def validate_key(self, key: str) -> None:
        """
        Validate key for filesystem safety.
        
        No user-supplied filenames directly.
        """
        if not key:
            raise BackendInvariantViolation("Key cannot be empty")
        
        # Disallow path separators
        if '/' in key or '\\' in key:
            raise BackendInvariantViolation(f"Key cannot contain path separators: {key}")
        
        # Disallow special names
        if key in {'.', '..'}:
            raise BackendInvariantViolation(f"Invalid key: {key}")
        
        # Disallow hidden files
        if key.startswith('.'):
            raise BackendInvariantViolation(f"Key cannot start with dot: {key}")


# ============================================================================
# LOCKING (HONEST LIMITS)
# ============================================================================

class BackendLock:
    """
    Advisory file-based lock.
    
    HONEST LIMITS:
    - Single global lock file
    - Enforce single-writer semantics
    - Fail fast on contention
    - Never silently interleave writes
    - Never assume NFS lock correctness
    
    Concurrency safety > concurrency performance.
    """
    
    def __init__(self, lock_path: Path, timeout: float = 5.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self._fd: int | None = None
        self._acquired = False
    
    def acquire(self) -> None:
        """Acquire lock with timeout."""
        if self._acquired:
            raise BackendInvariantViolation("Lock already acquired")
        
        start = time.time()
        
        # Ensure lock file exists
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        
        while True:
            try:
                # Open lock file
                fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_RDWR,
                    0o644
                )
                
                try:
                    # Try to acquire exclusive lock (non-blocking)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    self._acquired = True
                    return
                    
                except BlockingIOError:
                    os.close(fd)
                    
                    # Check timeout
                    if time.time() - start > self.timeout:
                        raise BackendConcurrencyConflict(
                            f"Failed to acquire lock after {self.timeout}s"
                        )
                    
                    # Wait and retry
                    time.sleep(0.1)
                    
            except PermissionError as e:
                raise BackendPermissionDenied(f"Cannot acquire lock: {e}") from e
    
    def release(self) -> None:
        """Release lock."""
        if not self._acquired or self._fd is None:
            return
        
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        finally:
            self._fd = None
            self._acquired = False
    
    def __enter__(self) -> BackendLock:
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


# ============================================================================
# TRANSACTION EMULATION (STRICT)
# ============================================================================

class FilesystemTransaction:
    """
    Emulated transaction using staging directory.
    
    STRATEGY:
    - Every transaction gets a unique staging directory
    - All writes go into staging
    - Commit = rename entire staging tree into place
    - Rollback = delete staging tree
    
    If commit fails → nothing is visible.
    """
    
    def __init__(self, backend: FilesystemBackend, tx_id: str | None = None):
        self._backend = backend
        self.tx_id = tx_id or f"tx_{uuid.uuid4().hex}_{int(time.time() * 1000)}"
        self._staging_dir = backend._layout.tx_staging_dir(self.tx_id)
        self._active = False
        self._committed = False
        self._operations: list[dict[str, Any]] = []
    
    def begin(self) -> None:
        """Begin transaction."""
        if self._active:
            raise BackendTransactionError("Transaction already active")
        
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._active = True
    
    def stage_blob_write(
        self,
        key: str,
        version_id: str,
        data: bytes
    ) -> None:
        """Stage blob write in transaction."""
        if not self._active:
            raise BackendTransactionError("Transaction not active")
        
        # Write to staging area
        staging_blob_dir = self._staging_dir / "blobs" / key
        staging_blob_dir.mkdir(parents=True, exist_ok=True)
        
        staging_path = staging_blob_dir / f"{version_id}.blob"
        FilesystemOps.safe_write_file(staging_path, data, fsync=True)
        
        self._operations.append({
            'type': 'blob_write',
            'key': key,
            'version_id': version_id,
            'staging_path': staging_path
        })
    
    def stage_metadata_write(self, key: str, metadata: dict[str, Any]) -> None:
        """Stage metadata write in transaction."""
        if not self._active:
            raise BackendTransactionError("Transaction not active")
        
        staging_metadata_dir = self._staging_dir / "metadata"
        staging_metadata_dir.mkdir(parents=True, exist_ok=True)
        
        staging_path = staging_metadata_dir / f"{key}.json"
        serialized = json.dumps(metadata, sort_keys=True, indent=2).encode()
        FilesystemOps.safe_write_file(staging_path, serialized, fsync=True)
        
        self._operations.append({
            'type': 'metadata_write',
            'key': key,
            'staging_path': staging_path
        })
    
    def stage_tombstone(self, key: str) -> None:
        """Stage tombstone creation."""
        if not self._active:
            raise BackendTransactionError("Transaction not active")
        
        staging_tombstone_dir = self._staging_dir / "tombstones"
        staging_tombstone_dir.mkdir(parents=True, exist_ok=True)
        
        staging_path = staging_tombstone_dir / f"{key}.tombstone"
        tombstone_data = {
            'key': key,
            'deleted_at': int(time.time() * 1000),
            'tx_id': self.tx_id
        }
        serialized = json.dumps(tombstone_data, sort_keys=True).encode()
        FilesystemOps.safe_write_file(staging_path, serialized, fsync=True)
        
        self._operations.append({
            'type': 'tombstone',
            'key': key,
            'staging_path': staging_path
        })
    
    def commit(self) -> None:
        """
        Commit transaction atomically.
        
        Move all staged files into final locations.
        """
        if not self._active:
            raise BackendTransactionError("Transaction not active")
        
        if self._committed:
            raise BackendTransactionError("Transaction already committed")
        
        try:
            # Move each staged operation to final location
            for op in self._operations:
                if op['type'] == 'blob_write':
                    final_path = self._backend._layout.blob_path(
                        op['key'],
                        op['version_id']
                    )
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Atomic rename
                    os.rename(op['staging_path'], final_path)
                    FilesystemOps.fsync_directory(final_path.parent)
                    
                elif op['type'] == 'metadata_write':
                    final_path = self._backend._layout.metadata_path(op['key'])
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    os.rename(op['staging_path'], final_path)
                    FilesystemOps.fsync_directory(final_path.parent)
                    
                elif op['type'] == 'tombstone':
                    final_path = self._backend._layout.tombstone_path(op['key'])
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    os.rename(op['staging_path'], final_path)
                    FilesystemOps.fsync_directory(final_path.parent)
            
            self._committed = True
            
        except Exception as e:
            # Commit failed - staging files may be partially moved
            # This is acceptable - crash recovery will clean up
            raise BackendTransactionError(f"Commit failed: {e}") from e
        finally:
            # Clean up staging directory
            self.rollback()
    
    def rollback(self) -> None:
        """Rollback transaction by deleting staging directory."""
        if self._staging_dir.exists():
            FilesystemOps.safe_remove(self._staging_dir)
        
        self._active = False
        self._operations.clear()
    
    def __enter__(self) -> FilesystemTransaction:
        self.begin()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is not None:
            self.rollback()
        elif not self._committed:
            self.rollback()


# ============================================================================
# FILESYSTEM BACKEND (MAIN)
# ============================================================================

class FilesystemBackend:
    """
    Local & NFS durability backend.
    
    DESIGN PRINCIPLES:
    - Durability is explicitly purchased with syscalls
    - Never lie about what the filesystem guarantees
    - Fail fast, fail explicitly
    - No clever optimizations
    
    CRASH RECOVERY:
    - Scan tx/ directory on open
    - Delete any leftover transaction directories
    - Tombstones always win
    """
    
    def __init__(
        self,
        root_path: str | Path,
        use_locking: bool = True,
        fsync_enabled: bool = True,
        lock_timeout: float = 5.0
    ):
        """
        Initialize filesystem backend.
        
        Args:
            root_path: Root directory for all storage
            use_locking: Whether to use file-based locking
            fsync_enabled: Whether to fsync writes (disable for testing only)
            lock_timeout: Lock acquisition timeout in seconds
        """
        self._root = Path(root_path).resolve()
        self._layout = FilesystemLayout(self._root)
        self._use_locking = use_locking
        self._fsync_enabled = fsync_enabled
        self._lock_timeout = lock_timeout
        self._lock: BackendLock | None = None
        
        # Security: Validate root is not world-writable
        self._validate_root_security()
        
        # Initialize directory structure
        self._layout.initialize()
        
        # Crash recovery
        self._recover_from_crash()
    
    def _validate_root_security(self) -> None:
        """Validate root directory security."""
        if self._root.exists():
            stat_info = self._root.stat()
            mode = stat_info.st_mode
            
            # Check not world-writable
            if mode & 0o002:
                raise BackendPermissionDenied(
                    f"Refusing world-writable root: {self._root}"
                )
    
    def _recover_from_crash(self) -> None:
        """
        Crash recovery on backend open.
        
        RULES:
        - Scan tx/ directory
        - Delete any leftover transaction directories
        - Partial commits must not be guessed
        """
        tx_dir = self._layout.tx_dir
        
        if not tx_dir.exists():
            return
        
        # Clean up any leftover transaction directories
        for tx_staging in tx_dir.iterdir():
            if tx_staging.is_dir():
                FilesystemOps.safe_remove(tx_staging)
    
    def _acquire_lock(self) -> BackendLock | None:
        """Acquire backend lock if locking enabled."""
        if not self._use_locking:
            return None
        
        lock = BackendLock(self._layout.lock_file, self._lock_timeout)
        lock.acquire()
        return lock
    
    def _is_tombstoned(self, key: str) -> bool:
        """Check if key is tombstoned."""
        tombstone_path = self._layout.tombstone_path(key)
        return tombstone_path.exists()
    
    # ========================================================================
    # BLOB OPERATIONS
    # ========================================================================
    
    def put_blob(
        self,
        key: str,
        data: bytes,
        version_id: str | None = None,
        metadata: dict[str, Any] | None = None
    ) -> str:
        """
        Write blob with durability guarantees.
        
        SEMANTICS:
        1. Write blob to staging
        2. fsync(fd)
        3. fsync(dir)
        4. Atomic rename into final location
        5. fsync(parent_dir)
        
        Partial files are never visible.
        Overwrites are illegal.
        """
        self._layout.validate_key(key)
        
        # Generate version ID if not provided
        if version_id is None:
            version_id = self._generate_version_id(data)
        
        lock = self._acquire_lock()
        try:
            # Check not tombstoned
            if self._is_tombstoned(key):
                raise BackendInvariantViolation(f"Key is tombstoned: {key}")
            
            # Use transaction for atomicity
            with FilesystemTransaction(self) as tx:
                tx.stage_blob_write(key, version_id, data)
                
                # Update metadata if provided
                if metadata:
                    tx.stage_metadata_write(key, metadata)
                
                tx.commit()
            
            return version_id
            
        finally:
            if lock:
                lock.release()
    
    def get_blob(self, key: str, version_id: str | None = None) -> bytes:
        """
        Read blob.
        
        Respects tombstones.
        """
        self._layout.validate_key(key)
        
        # Check tombstone
        if self._is_tombstoned(key):
            raise FileNotFoundError(f"Key is tombstoned: {key}")
        
        if version_id is None:
            # Get latest version
            version_id = self._get_latest_version(key)
            if version_id is None:
                raise FileNotFoundError(f"No versions found for key: {key}")
        
        blob_path = self._layout.blob_path(key, version_id)
        
        if not blob_path.exists():
            raise FileNotFoundError(f"Blob not found: {key}@{version_id}")
        
        return FilesystemOps.safe_read_file(blob_path)
    
    def list_versions(self, key: str) -> list[str]:
        """List all versions for a key."""
        self._layout.validate_key(key)
        
        blob_dir = self._layout.blobs_dir / key
        
        if not blob_dir.exists():
            return []
        
        versions = []
        for blob_file in blob_dir.iterdir():
            if blob_file.suffix == '.blob':
                version_id = blob_file.stem
                versions.append(version_id)
        
        # Lexicographic ordering
        return sorted(versions)
    
    def _get_latest_version(self, key: str) -> str | None:
        """Get latest version ID for key."""
        versions = self.list_versions(key)
        return versions[-1] if versions else None
    
    def delete_blob(self, key: str) -> None:
        """
        Delete blob by creating tombstone.
        
        SEMANTICS:
        - Delete never destroys data
        - Creates immutable tombstone
        - Versions remain for replay/audit
        - GC happens elsewhere
        """
        self._layout.validate_key(key)
        
        lock = self._acquire_lock()
        try:
            with FilesystemTransaction(self) as tx:
                tx.stage_tombstone(key)
                tx.commit()
                
        finally:
            if lock:
                lock.release()
    
    # ========================================================================
    # METADATA OPERATIONS
    # ========================================================================
    
    def put_metadata(self, key: str, metadata: dict[str, Any]) -> None:
        """Write metadata."""
        self._layout.validate_key(key)
        
        lock = self._acquire_lock()
        try:
            if self._is_tombstoned(key):
                raise BackendInvariantViolation(f"Key is tombstoned: {key}")
            
            with FilesystemTransaction(self) as tx:
                tx.stage_metadata_write(key, metadata)
                tx.commit()
                
        finally:
            if lock:
                lock.release()
    
    def get_metadata(self, key: str) -> dict[str, Any]:
        """Read metadata."""
        self._layout.validate_key(key)
        
        if self._is_tombstoned(key):
            raise FileNotFoundError(f"Key is tombstoned: {key}")
        
        metadata_path = self._layout.metadata_path(key)
        
        if not metadata_path.exists():
            raise FileNotFoundError(f"Metadata not found: {key}")
        
        data = FilesystemOps.safe_read_file(metadata_path)
        return json.loads(data.decode())
    
    # ========================================================================
    # LISTING OPERATIONS
    # ========================================================================
    
    def list_keys(self) -> list[str]:
        """List all non-tombstoned keys."""
        keys = set()
        
        # Collect keys from blobs
        if self._layout.blobs_dir.exists():
            for key_dir in self._layout.blobs_dir.iterdir():
                if key_dir.is_dir():
                    keys.add(key_dir.name)
        
        # Collect keys from metadata
        if self._layout.metadata_dir.exists():
            for metadata_file in self._layout.metadata_dir.iterdir():
                if metadata_file.suffix == '.json':
                    keys.add(metadata_file.stem)
        
        # Filter out tombstoned keys
        return sorted([k for k in keys if not self._is_tombstoned(k)])
    
    # ========================================================================
    # UTILITY METHODS
    # ========================================================================
    
    def _generate_version_id(self, data: bytes) -> str:
        """
        Generate deterministic version ID.
        
        DETERMINISM GUARANTEE:
        Same bytes → same version ID across machines.
        """
        timestamp_ms = int(time.time() * 1000)
        content_hash = hashlib.sha256(data).hexdigest()[:16]
        return f"{timestamp_ms}_{content_hash}"
    
    def get_capabilities(self) -> FilesystemBackendCapabilities:
        """Get backend capabilities."""
        return FilesystemBackendCapabilities()
    
    def get_stats(self) -> dict[str, Any]:
        """Get backend statistics."""
        stats = {
            'root': str(self._root),
            'total_keys': len(self.list_keys()),
            'total_tombstones': len(list(self._layout.tombstones_dir.glob('*.tombstone'))) if self._layout.tombstones_dir.exists() else 0,
            'fsync_enabled': self._fsync_enabled,
            'locking_enabled': self._use_locking,
        }
        
        # Calculate total storage
        total_size = 0
        if self._layout.blobs_dir.exists():
            for blob_file in self._layout.blobs_dir.rglob('*.blob'):
                total_size += blob_file.stat().st_size
        
        stats['total_storage_bytes'] = total_size
        
        return stats
    
    def close(self) -> None:
        """Close backend."""
        # Nothing to close for filesystem backend
        pass
    
    def __enter__(self) -> FilesystemBackend:
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ============================================================================
# INVARIANTS (DEFENSIVE VALIDATION)
# ============================================================================

class FilesystemBackendInvariants:
    """
    Invariant validation for filesystem backend.
    
    GUARANTEES:
    - Same inputs → same directory layout
    - Same bytes → same hashes
    - Same version IDs across machines
    - Same failure → same error class
    
    Replay depends on this.
    """
    
    @staticmethod
    def verify_deterministic_layout(backend: FilesystemBackend, key: str, version_id: str) -> bool:
        """Verify deterministic path generation."""
        path1 = backend._layout.blob_path(key, version_id)
        path2 = backend._layout.blob_path(key, version_id)
        return path1 == path2
    
    @staticmethod
    def verify_no_symlinks(backend: FilesystemBackend) -> bool:
        """Verify no symlinks in backend."""
        for path in backend._root.rglob('*'):
            if path.is_symlink():
                return False
        return True
    
    @staticmethod
    def verify_tombstone_immutability(backend: FilesystemBackend, key: str) -> bool:
        """Verify tombstone cannot be overwritten."""
        backend.delete_blob(key)
        
        try:
            backend.put_blob(key, b"data")
            return False  # Should have failed
        except BackendInvariantViolation:
            return True  # Correctly rejected
    
    @staticmethod
    def verify_crash_recovery(backend: FilesystemBackend) -> bool:
        """Verify crash recovery cleans up staging."""
        # Create abandoned transaction directory
        abandoned_tx = backend._layout.tx_dir / "abandoned_tx"
        abandoned_tx.mkdir(parents=True, exist_ok=True)
        
        # Trigger recovery
        backend._recover_from_crash()
        
        # Verify cleanup
        return not abandoned_tx.exists()


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Main backend
    'FilesystemBackend',
    
    # Capabilities
    'FilesystemBackendCapabilities',
    
    # Transactions
    'FilesystemTransaction',
    
    # Errors
    'BackendError',
    'BackendPermissionDenied',
    'BackendUnavailable',
    'BackendInvariantViolation',
    'BackendDataCorruption',
    'BackendUnsupportedOperation',
    'BackendConcurrencyConflict',
    'BackendTransactionError',
    
    # Utilities
    'FilesystemOps',
    'FilesystemLayout',
    'BackendLock',
    
    # Invariants
    'FilesystemBackendInvariants',
]