"""
object_store_backend.py - Immutable Blob & Snapshot Storage

Location: /infra/persistence/backends/object_store_backend.py

Purpose:
    Write-once, immutable, content-addressed blob storage.
    
    Used for:
        - Snapshots
        - Archival payloads
        - Replay blobs
        - Audit artifacts

What this backend IS:
    ✓ Write-once
    ✓ Immutable
    ✓ Content-addressed
    ✓ Cheap at scale

What this backend is NOT:
    ❌ Transactional
    ❌ Mutable
    ❌ Queryable
    ❌ Fast

Reads are slower. Writes are permanent.

Core Responsibilities:
    1. Store immutable blobs
    2. Enforce write-once semantics
    3. Support hash-based addressing
    4. Detect corruption
    5. Refuse overwrite
    6. Be cheap & scalable

Determinism Guarantees:
    - Same payload → same object_id
    - Hash verified on read
    - Immutable forever

This backend underpins replay trust.

Mental Model:
    Object storage is the system's long-term memory.
"""

import hashlib
import json
import os
import shutil
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, BinaryIO
from enum import Enum


# ============================================================================
# STORAGE EXCEPTIONS
# ============================================================================

class ObjectStoreError(Exception):
    """Base exception for object store errors."""
    pass


class ObjectNotFoundError(ObjectStoreError):
    """Object does not exist."""
    pass


class ObjectAlreadyExistsError(ObjectStoreError):
    """Object already exists (write-once violation)."""
    pass


class ObjectCorruptionError(ObjectStoreError):
    """Object data is corrupted."""
    pass


class ObjectPermissionError(ObjectStoreError):
    """Permission denied."""
    pass


class ObjectPartialWriteError(ObjectStoreError):
    """Partial write detected."""
    pass


# ============================================================================
# OBJECT METADATA
# ============================================================================

@dataclass
class ObjectMetadata:
    """
    Metadata for stored objects.
    """
    object_id: str
    content_hash: str
    size_bytes: int
    created_at: str
    content_type: Optional[str] = None
    compression: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)
    
    def to_json(self) -> str:
        """Convert to JSON."""
        return json.dumps(self.to_dict(), sort_keys=True)
    
    @staticmethod
    def from_dict(data: dict) -> 'ObjectMetadata':
        """Create from dictionary."""
        return ObjectMetadata(**data)


# ============================================================================
# STATE BACKEND INTERFACE (Minimal for standalone)
# ============================================================================

class StateBackend(ABC):
    """
    Abstract base for state backends.
    
    Cross-Backend Invariants (ABSOLUTE):
        - No schema awareness
        - No serialization logic
        - No inference
        - No mutation beyond contract
        - No cross-backend coupling
    
    They store bytes + metadata, nothing more.
    """
    
    @abstractmethod
    def put(self, key: str, value: bytes, metadata: Optional[dict] = None) -> None:
        """Store value."""
        pass
    
    @abstractmethod
    def get(self, key: str) -> bytes:
        """Retrieve value."""
        pass
    
    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        pass
    
    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete value (if supported)."""
        pass


# ============================================================================
# OBJECT STORE BACKEND - Core Implementation
# ============================================================================

class ObjectStoreBackend(StateBackend):
    """
    Immutable object store backend.
    
    Enforces:
        - Write-once semantics
        - Content-addressed storage
        - Hash verification
        - Corruption detection
        - No overwrites
    
    Failure Semantics:
        - Missing object → Hard fail
        - Hash mismatch → Corruption alert
        - Partial write → Abort
        - Permission error → Abort
    """
    
    # Hash algorithm for content addressing
    HASH_ALGORITHM = "sha256"
    
    # Metadata file suffix
    METADATA_SUFFIX = ".meta"
    
    def __init__(
        self,
        storage_dir: Path,
        verify_on_read: bool = True,
        allow_overwrites: bool = False,  # DANGEROUS - almost always False
        use_sharding: bool = True
    ):
        """
        Initialize object store backend.
        
        Args:
            storage_dir: Root directory for object storage
            verify_on_read: Verify hash on every read
            allow_overwrites: Allow overwriting objects (DANGEROUS)
            use_sharding: Use directory sharding for scalability
        """
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.verify_on_read = verify_on_read
        self.allow_overwrites = allow_overwrites
        self.use_sharding = use_sharding
        
        self._lock = threading.Lock()
        
        # Statistics
        self._stats = {
            "puts": 0,
            "gets": 0,
            "corruption_detected": 0,
            "overwrite_attempts": 0
        }
    
    def put(
        self,
        object_id: str,
        payload: bytes,
        metadata: Optional[dict] = None
    ) -> None:
        """
        Store immutable object.
        
        Args:
            object_id: Unique object identifier
            payload: Binary payload to store
            metadata: Optional metadata
        
        Raises:
            ObjectAlreadyExistsError: If object exists and overwrites disabled
            ObjectPartialWriteError: If write fails partway through
            ObjectPermissionError: If permission denied
        """
        with self._lock:
            # Check for existing object
            if self.exists(object_id) and not self.allow_overwrites:
                self._stats["overwrite_attempts"] += 1
                raise ObjectAlreadyExistsError(
                    f"Object already exists (write-once): {object_id}"
                )
            
            # Compute content hash
            content_hash = self._compute_hash(payload)
            
            # Create metadata
            object_metadata = ObjectMetadata(
                object_id=object_id,
                content_hash=content_hash,
                size_bytes=len(payload),
                created_at=datetime.utcnow().isoformat(),
                content_type=metadata.get("content_type") if metadata else None,
                compression=metadata.get("compression") if metadata else None
            )
            
            # Get file paths
            object_path = self._get_object_path(object_id)
            metadata_path = self._get_metadata_path(object_id)
            
            # Ensure parent directory exists
            object_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write atomically
            try:
                self._write_atomic(object_path, payload)
                self._write_atomic(metadata_path, object_metadata.to_json().encode())
            except PermissionError as e:
                raise ObjectPermissionError(f"Permission denied: {e}")
            except Exception as e:
                # Clean up partial write
                if object_path.exists():
                    object_path.unlink()
                if metadata_path.exists():
                    metadata_path.unlink()
                raise ObjectPartialWriteError(f"Partial write aborted: {e}")
            
            self._stats["puts"] += 1
    
    def get(self, object_id: str) -> bytes:
        """
        Retrieve immutable object.
        
        Args:
            object_id: Object identifier
        
        Returns:
            Binary payload
        
        Raises:
            ObjectNotFoundError: If object doesn't exist
            ObjectCorruptionError: If hash verification fails
        """
        # Check existence
        if not self.exists(object_id):
            raise ObjectNotFoundError(f"Object not found: {object_id}")
        
        # Get file paths
        object_path = self._get_object_path(object_id)
        metadata_path = self._get_metadata_path(object_id)
        
        # Read payload
        try:
            with open(object_path, 'rb') as f:
                payload = f.read()
        except Exception as e:
            raise ObjectStoreError(f"Failed to read object: {e}")
        
        # Verify hash if enabled
        if self.verify_on_read:
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    metadata = ObjectMetadata.from_dict(json.load(f))
            except Exception as e:
                raise ObjectStoreError(f"Failed to read metadata: {e}")
            
            actual_hash = self._compute_hash(payload)
            if actual_hash != metadata.content_hash:
                self._stats["corruption_detected"] += 1
                raise ObjectCorruptionError(
                    f"Hash mismatch for {object_id}: "
                    f"expected {metadata.content_hash}, got {actual_hash}"
                )
        
        self._stats["gets"] += 1
        return payload
    
    def exists(self, object_id: str) -> bool:
        """
        Check if object exists.
        
        Args:
            object_id: Object identifier
        
        Returns:
            True if object exists
        """
        object_path = self._get_object_path(object_id)
        return object_path.exists()
    
    def delete(self, object_id: str) -> None:
        """
        Delete object.
        
        RESTRICTED: Deletes should be extremely rare in object stores.
        
        Args:
            object_id: Object identifier
        
        Raises:
            ObjectNotFoundError: If object doesn't exist
        """
        if not self.exists(object_id):
            raise ObjectNotFoundError(f"Object not found: {object_id}")
        
        # Get file paths
        object_path = self._get_object_path(object_id)
        metadata_path = self._get_metadata_path(object_id)
        
        # Delete files
        object_path.unlink()
        if metadata_path.exists():
            metadata_path.unlink()
    
    def get_metadata(self, object_id: str) -> ObjectMetadata:
        """
        Get object metadata without reading payload.
        
        Args:
            object_id: Object identifier
        
        Returns:
            ObjectMetadata
        """
        if not self.exists(object_id):
            raise ObjectNotFoundError(f"Object not found: {object_id}")
        
        metadata_path = self._get_metadata_path(object_id)
        
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                return ObjectMetadata.from_dict(json.load(f))
        except Exception as e:
            raise ObjectStoreError(f"Failed to read metadata: {e}")
    
    def list_objects(self, prefix: Optional[str] = None) -> List[str]:
        """
        List all objects (or with prefix).
        
        WARNING: This can be slow for large stores.
        
        Args:
            prefix: Optional object ID prefix filter
        
        Returns:
            List of object IDs
        """
        objects = []
        
        for path in self.storage_dir.rglob("*"):
            if path.is_file() and not path.name.endswith(self.METADATA_SUFFIX):
                object_id = self._path_to_object_id(path)
                if prefix is None or object_id.startswith(prefix):
                    objects.append(object_id)
        
        return sorted(objects)
    
    def verify_integrity(self, object_id: str) -> bool:
        """
        Verify object integrity.
        
        Args:
            object_id: Object identifier
        
        Returns:
            True if integrity check passes
        """
        try:
            # This will verify hash
            self.get(object_id)
            return True
        except ObjectCorruptionError:
            return False
    
    def get_stats(self) -> dict:
        """Get backend statistics."""
        return dict(self._stats)
    
    def _get_object_path(self, object_id: str) -> Path:
        """
        Get filesystem path for object.
        
        Uses sharding for scalability if enabled.
        """
        if self.use_sharding:
            # Use first 2 chars for sharding
            shard = object_id[:2] if len(object_id) >= 2 else "00"
            return self.storage_dir / shard / object_id
        else:
            return self.storage_dir / object_id
    
    def _get_metadata_path(self, object_id: str) -> Path:
        """Get metadata file path."""
        object_path = self._get_object_path(object_id)
        return object_path.with_suffix(self.METADATA_SUFFIX)
    
    def _path_to_object_id(self, path: Path) -> str:
        """Convert filesystem path back to object ID."""
        if self.use_sharding:
            # Remove shard directory
            return path.name
        else:
            return path.relative_to(self.storage_dir).as_posix()
    
    def _compute_hash(self, payload: bytes) -> str:
        """
        Compute content hash.
        
        Same payload → same hash (deterministic).
        """
        hasher = hashlib.new(self.HASH_ALGORITHM)
        hasher.update(payload)
        return hasher.hexdigest()
    
    def _write_atomic(self, path: Path, data: bytes) -> None:
        """
        Write file atomically using temp file + rename.
        
        Prevents partial writes from being visible.
        """
        temp_path = path.with_suffix('.tmp')
        
        try:
            # Write to temp file
            with open(temp_path, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            
            # Atomic rename
            temp_path.replace(path)
            
        except Exception:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise


# ============================================================================
# CONTENT-ADDRESSED OBJECT STORE
# ============================================================================

class ContentAddressedObjectStore(ObjectStoreBackend):
    """
    Content-addressed object store.
    
    Object IDs are automatically derived from content hash.
    This ensures:
        - Same content → same object_id
        - Deduplication
        - Immutability guarantees
    """
    
    def put(
        self,
        payload: bytes,
        metadata: Optional[dict] = None
    ) -> str:
        """
        Store object with content-derived ID.
        
        Args:
            payload: Binary payload
            metadata: Optional metadata
        
        Returns:
            Content-derived object ID (hash)
        """
        # Derive object ID from content
        object_id = self._compute_hash(payload)
        
        # Store using parent implementation
        try:
            super().put(object_id, payload, metadata)
        except ObjectAlreadyExistsError:
            # Already exists - this is fine for content-addressed storage
            pass
        
        return object_id
    
    def put_with_id(
        self,
        object_id: str,
        payload: bytes,
        metadata: Optional[dict] = None
    ) -> None:
        """
        Store with explicit ID (must match content hash).
        
        Args:
            object_id: Explicit object ID
            payload: Binary payload
            metadata: Optional metadata
        
        Raises:
            ValueError: If object_id doesn't match content hash
        """
        expected_id = self._compute_hash(payload)
        if object_id != expected_id:
            raise ValueError(
                f"Object ID mismatch: provided {object_id}, "
                f"expected {expected_id} (content hash)"
            )
        
        super().put(object_id, payload, metadata)


# ============================================================================
# SNAPSHOT STORE - Specialized for Snapshots
# ============================================================================

class SnapshotObjectStore:
    """
    Specialized object store for state snapshots.
    
    Adds snapshot-specific operations on top of object store.
    """
    
    def __init__(self, backend: ObjectStoreBackend):
        self.backend = backend
    
    def store_snapshot(
        self,
        snapshot_id: str,
        snapshot_data: bytes,
        metadata: Optional[dict] = None
    ) -> None:
        """
        Store state snapshot.
        
        Args:
            snapshot_id: Snapshot identifier
            snapshot_data: Serialized snapshot
            metadata: Snapshot metadata
        """
        full_metadata = metadata or {}
        full_metadata["snapshot"] = True
        full_metadata["content_type"] = "application/octet-stream"
        
        self.backend.put(
            object_id=f"snapshot:{snapshot_id}",
            payload=snapshot_data,
            metadata=full_metadata
        )
    
    def load_snapshot(self, snapshot_id: str) -> bytes:
        """
        Load state snapshot.
        
        Args:
            snapshot_id: Snapshot identifier
        
        Returns:
            Snapshot data
        """
        return self.backend.get(f"snapshot:{snapshot_id}")
    
    def snapshot_exists(self, snapshot_id: str) -> bool:
        """Check if snapshot exists."""
        return self.backend.exists(f"snapshot:{snapshot_id}")
    
    def list_snapshots(self) -> List[str]:
        """List all snapshot IDs."""
        snapshots = []
        for obj_id in self.backend.list_objects(prefix="snapshot:"):
            snapshot_id = obj_id.replace("snapshot:", "")
            snapshots.append(snapshot_id)
        return sorted(snapshots)
    
    def verify_snapshot(self, snapshot_id: str) -> bool:
        """Verify snapshot integrity."""
        return self.backend.verify_integrity(f"snapshot:{snapshot_id}")


# ============================================================================
# ARCHIVE STORE - For Audit Artifacts
# ============================================================================

class ArchiveObjectStore:
    """
    Specialized object store for archival and audit artifacts.
    """
    
    def __init__(self, backend: ObjectStoreBackend):
        self.backend = backend
    
    def archive(
        self,
        archive_id: str,
        payload: bytes,
        archive_type: str,
        metadata: Optional[dict] = None
    ) -> None:
        """
        Archive payload.
        
        Args:
            archive_id: Archive identifier
            payload: Data to archive
            archive_type: Type of archive (audit, replay, etc.)
            metadata: Additional metadata
        """
        full_metadata = metadata or {}
        full_metadata["archive_type"] = archive_type
        full_metadata["archived_at"] = datetime.utcnow().isoformat()
        
        self.backend.put(
            object_id=f"archive:{archive_type}:{archive_id}",
            payload=payload,
            metadata=full_metadata
        )
    
    def retrieve(self, archive_id: str, archive_type: str) -> bytes:
        """Retrieve archived payload."""
        return self.backend.get(f"archive:{archive_type}:{archive_id}")
    
    def list_archives(self, archive_type: Optional[str] = None) -> List[str]:
        """List archives by type."""
        prefix = f"archive:{archive_type}:" if archive_type else "archive:"
        archives = []
        
        for obj_id in self.backend.list_objects(prefix=prefix):
            parts = obj_id.split(":")
            if len(parts) >= 3:
                archives.append(parts[2])
        
        return sorted(archives)


# ============================================================================
# FACTORY
# ============================================================================

def create_object_store(
    storage_dir: str,
    verify_on_read: bool = True,
    allow_overwrites: bool = False,
    use_sharding: bool = True,
    content_addressed: bool = False
) -> ObjectStoreBackend:
    """
    Create object store backend.
    
    Args:
        storage_dir: Storage directory
        verify_on_read: Verify hash on every read
        allow_overwrites: Allow overwriting (DANGEROUS)
        use_sharding: Use directory sharding
        content_addressed: Use content-addressed store
    
    Returns:
        ObjectStoreBackend instance
    """
    if content_addressed:
        return ContentAddressedObjectStore(
            storage_dir=Path(storage_dir),
            verify_on_read=verify_on_read,
            allow_overwrites=allow_overwrites,
            use_sharding=use_sharding
        )
    else:
        return ObjectStoreBackend(
            storage_dir=Path(storage_dir),
            verify_on_read=verify_on_read,
            allow_overwrites=allow_overwrites,
            use_sharding=use_sharding
        )


def create_snapshot_store(storage_dir: str) -> SnapshotObjectStore:
    """Create snapshot store."""
    backend = create_object_store(
        storage_dir=storage_dir,
        verify_on_read=True,
        allow_overwrites=False,
        use_sharding=True,
        content_addressed=False
    )
    return SnapshotObjectStore(backend)


def create_archive_store(storage_dir: str) -> ArchiveObjectStore:
    """Create archive store."""
    backend = create_object_store(
        storage_dir=storage_dir,
        verify_on_read=True,
        allow_overwrites=False,
        use_sharding=True,
        content_addressed=False
    )
    return ArchiveObjectStore(backend)


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    print("Object Store Backend Demo")
    print("=" * 60)
    
    # Create standard object store
    print("\n1. Standard Object Store")
    store = create_object_store(
        storage_dir="/tmp/object_store_demo",
        verify_on_read=True,
        allow_overwrites=False
    )
    
    # Store object
    payload = b"This is immutable data for replay"
    store.put("replay_001", payload)
    print(f"✓ Stored object: replay_001")
    
    # Retrieve object
    retrieved = store.get("replay_001")
    assert retrieved == payload
    print(f"✓ Retrieved and verified: {len(retrieved)} bytes")
    
    # Get metadata
    metadata = store.get_metadata("replay_001")
    print(f"✓ Metadata:")
    print(f"    Hash: {metadata.content_hash}")
    print(f"    Size: {metadata.size_bytes} bytes")
    print(f"    Created: {metadata.created_at}")
    
    # Try to overwrite (should fail)
    print("\n2. Write-Once Enforcement")
    try:
        store.put("replay_001", b"different data")
        print("✗ Overwrite succeeded (SHOULD NOT HAPPEN)")
    except ObjectAlreadyExistsError:
        print("✓ Overwrite blocked (write-once enforced)")
    
    # Content-addressed store
    print("\n3. Content-Addressed Store")
    ca_store = create_object_store(
        storage_dir="/tmp/ca_store_demo",
        content_addressed=True
    )
    
    payload1 = b"Hello, World!"
    object_id = ca_store.put(payload1)
    print(f"✓ Stored with content-derived ID: {object_id}")
    
    # Same content → same ID
    payload2 = b"Hello, World!"
    object_id2 = ca_store.put(payload2)
    assert object_id == object_id2
    print(f"✓ Same content → same ID (deduplication)")
    
    # Snapshot store
    print("\n4. Snapshot Store")
    snapshot_store = create_snapshot_store("/tmp/snapshot_demo")
    
    snapshot_data = b"Snapshot at timestamp 1000"
    snapshot_store.store_snapshot("snap_001", snapshot_data)
    print("✓ Stored snapshot: snap_001")
    
    loaded = snapshot_store.load_snapshot("snap_001")
    assert loaded == snapshot_data
    print("✓ Loaded and verified snapshot")
    
    # List snapshots
    snapshots = snapshot_store.list_snapshots()
    print(f"✓ Snapshots: {snapshots}")
    
    # Archive store
    print("\n5. Archive Store")
    archive_store = create_archive_store("/tmp/archive_demo")
    
    audit_data = b"Audit log entry for run_123"
    archive_store.archive("run_123", audit_data, "audit")
    print("✓ Archived audit data")
    
    retrieved_audit = archive_store.retrieve("run_123", "audit")
    assert retrieved_audit == audit_data
    print("✓ Retrieved audit archive")
    
    # Statistics
    print("\n6. Backend Statistics")
    stats = store.get_stats()
    print(f"✓ Stats: {stats}")
    
    # Corruption detection
    print("\n7. Corruption Detection")
    print("   (Simulating corruption by manual file modification)")
    object_path = store._get_object_path("replay_001")
    with open(object_path, 'wb') as f:
        f.write(b"corrupted data")
    
    try:
        store.get("replay_001")
        print("✗ Corruption not detected")
    except ObjectCorruptionError as e:
        print(f"✓ Corruption detected: {e}")
    
    print("\n" + "=" * 60)
    print("Object storage is the system's long-term memory.")
    print("Write-once. Immutable. Content-addressed. Corruption-proof.")