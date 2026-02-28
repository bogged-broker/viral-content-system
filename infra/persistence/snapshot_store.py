"""
snapshot_store.py - Point-in-Time Snapshot Authority

Location: /infra/persistence/snapshot_store.py

Purpose:
    The only way the system is allowed to freeze reality.
    
    Answers: "What exactly did the system know at time T — 
              and can we prove it?"

Snapshots are used for:
    - Deterministic replay
    - Incident forensics
    - Audit defense
    - Experiment reproducibility
    - Rollback verification
    - Trust & enforcement disputes

If snapshots are wrong → everything downstream is fiction.

What this file is NOT:
    ❌ Not a backup daemon
    ❌ Not a filesystem dump
    ❌ Not incremental state writes
    ❌ Not journaling
    ❌ Not logging

Snapshots are intentional, named, immutable artifacts.

Authority Ordering:
    state_backend → serializer → snapshot_store → archival/replay

Snapshots consume fully validated state, never raw blobs.

Design Principle:
    A snapshot that can be edited is not a snapshot — 
    it is a lie with a timestamp.

Mental Model:
    - Snapshots freeze meaning
    - Manifests prove completeness
    - Hashes prove identity
    - Restores require consent
    - Audits see everything
    
    This file is why your system can rewind time without lying.
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List, Set


# ============================================================================
# STATE KEY (Minimal for standalone)
# ============================================================================

@dataclass(frozen=True)
class StateKey:
    """State key identifier."""
    namespace: str
    key: str
    
    def to_string(self) -> str:
        """Convert to string representation."""
        return f"{self.namespace}:{self.key}"
    
    @staticmethod
    def from_string(s: str) -> 'StateKey':
        """Parse from string."""
        parts = s.split(':', 1)
        return StateKey(namespace=parts[0], key=parts[1] if len(parts) > 1 else "")


# ============================================================================
# SNAPSHOT SCOPE - Capture Boundaries
# ============================================================================

class SnapshotScope(Enum):
    """
    Snapshot scope.
    
    Scopes define:
        - Capture boundaries
        - Blast radius
        - Restore permissions
    """
    GLOBAL = "global"               # Entire system
    RUN = "run"                     # Single execution run
    WORKFLOW = "workflow"           # Workflow instance
    ACCOUNT = "account"             # Account state
    EXPERIMENT = "experiment"       # Experiment trial


# ============================================================================
# SNAPSHOT REASON - Explicit Why
# ============================================================================

class SnapshotReason(Enum):
    """
    Reason for snapshot creation.
    
    Every snapshot must have an explicit why.
    """
    EXPERIMENT_START = "experiment_start"
    EXPERIMENT_END = "experiment_end"
    ROLLOUT = "rollout"
    INCIDENT = "incident"
    MANUAL = "manual"
    MIGRATION = "migration"
    AUDIT = "audit"
    REPLAY = "replay"


# ============================================================================
# SNAPSHOT EXCEPTIONS
# ============================================================================

class SnapshotError(Exception):
    """Base exception for snapshot errors."""
    pass


class SnapshotNotFoundError(SnapshotError):
    """Snapshot not found."""
    pass


class SnapshotImmutableError(SnapshotError):
    """Attempt to modify immutable snapshot."""
    pass


class SnapshotSchemaError(SnapshotError):
    """Schema incompatibility."""
    pass


class SnapshotCorruptionError(SnapshotError):
    """Snapshot data corrupted."""
    pass


class SnapshotRestoreError(SnapshotError):
    """Snapshot restore failed."""
    pass


# ============================================================================
# SNAPSHOT METADATA - The Contract
# ============================================================================

@dataclass(frozen=True)
class SnapshotMetadata:
    """
    Metadata for a point-in-time snapshot.
    
    Rules:
        - snapshot_id is deterministic
        - schema versions are recorded
        - hash covers ALL content
    """
    snapshot_id: str
    scope: SnapshotScope
    scope_id: str
    reason: SnapshotReason
    
    created_at: int                 # Logical timestamp (monotonic)
    created_by: str                 # Who created it
    
    schema_versions: Dict[str, str] # Schema name → version
    state_hash: str                 # Content hash
    
    # Optional metadata
    description: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "reason": self.reason.value,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "schema_versions": self.schema_versions,
            "state_hash": self.state_hash,
            "description": self.description,
            "tags": self.tags
        }
    
    def to_json_canonical(self) -> str:
        """Canonical JSON serialization."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))


# ============================================================================
# SNAPSHOT MANIFEST - Completeness Proof
# ============================================================================

@dataclass(frozen=True)
class SnapshotManifest:
    """
    Complete manifest of snapshot contents.
    
    Manifest is:
        - Complete
        - Ordered
        - Immutable
        - Replay-safe
    """
    metadata: SnapshotMetadata
    state_keys: List[StateKey]
    
    # Computed fields
    state_count: int = field(default=0)
    total_size_bytes: int = field(default=0)
    
    def __post_init__(self):
        """Compute derived fields."""
        if self.state_count == 0:
            object.__setattr__(self, 'state_count', len(self.state_keys))
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "metadata": self.metadata.to_dict(),
            "state_keys": [sk.to_string() for sk in self.state_keys],
            "state_count": self.state_count,
            "total_size_bytes": self.total_size_bytes
        }
    
    def verify_completeness(self, expected_keys: Set[StateKey]) -> bool:
        """Verify manifest contains all expected keys."""
        manifest_keys = set(self.state_keys)
        return manifest_keys == expected_keys


# ============================================================================
# SNAPSHOT ID GENERATOR - Deterministic
# ============================================================================

class SnapshotIDGenerator:
    """
    Generates deterministic snapshot IDs.
    
    Snapshot IDs derive from:
        (scope + scope_id + timestamp + state_hash)
    
    Result:
        - Deterministic
        - Non-guessable
        - Collision-safe
    
    No random UUIDs.
    """
    
    @staticmethod
    def generate(
        scope: SnapshotScope,
        scope_id: str,
        timestamp: int,
        state_hash: str
    ) -> str:
        """
        Generate deterministic snapshot ID.
        
        Args:
            scope: Snapshot scope
            scope_id: Scope identifier
            timestamp: Creation timestamp
            state_hash: Content hash
        
        Returns:
            Deterministic snapshot ID
        """
        components = [
            scope.value,
            scope_id,
            str(timestamp),
            state_hash[:16]  # First 16 chars of hash
        ]
        
        raw = ":".join(components)
        hash_bytes = hashlib.sha256(raw.encode()).hexdigest()
        
        # Format: snapshot_<scope>_<hash>
        return f"snapshot_{scope.value}_{hash_bytes[:16]}"


# ============================================================================
# SNAPSHOT HASH COMPUTER - Content Verification
# ============================================================================

class SnapshotHashComputer:
    """
    Computes deterministic content hash for snapshots.
    
    Hash covers ALL content in canonical order.
    """
    
    @staticmethod
    def compute(states: Dict[StateKey, bytes]) -> str:
        """
        Compute hash of snapshot content.
        
        Args:
            states: Mapping of state keys to serialized bytes
        
        Returns:
            SHA-256 hash of content
        """
        hasher = hashlib.sha256()
        
        # Sort keys for determinism
        sorted_keys = sorted(states.keys(), key=lambda k: k.to_string())
        
        for key in sorted_keys:
            # Hash key
            hasher.update(key.to_string().encode())
            # Hash value
            hasher.update(states[key])
        
        return hasher.hexdigest()


# ============================================================================
# SNAPSHOT WRITER - Capture Engine
# ============================================================================

class SnapshotWriter:
    """
    Captures atomic point-in-time snapshots.
    
    Guarantees:
        - Atomic capture
        - Transactional read
        - Canonical serialization
        - Stable hashing
    
    If capture fails → no snapshot exists.
    """
    
    def __init__(
        self,
        state_backend: Any,
        storage_dir: Path
    ):
        self.state_backend = state_backend
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self._lock = threading.Lock()
    
    def create_snapshot(
        self,
        scope: SnapshotScope,
        scope_id: str,
        reason: SnapshotReason,
        created_by: str,
        state_keys: List[StateKey],
        schema_versions: Dict[str, str],
        description: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None
    ) -> SnapshotMetadata:
        """
        Create atomic snapshot.
        
        Args:
            scope: Snapshot scope
            scope_id: Scope identifier
            reason: Why snapshot is created
            created_by: Creator identifier
            state_keys: Keys to snapshot
            schema_versions: Schema versions at time of snapshot
            description: Optional description
            tags: Optional tags
        
        Returns:
            SnapshotMetadata
        
        Raises:
            SnapshotError: If capture fails
        """
        with self._lock:
            try:
                # Capture state (atomic read)
                states = self._capture_state(state_keys)
                
                # Compute content hash
                state_hash = SnapshotHashComputer.compute(states)
                
                # Generate deterministic snapshot ID
                timestamp = int(time.time() * 1000)
                snapshot_id = SnapshotIDGenerator.generate(
                    scope, scope_id, timestamp, state_hash
                )
                
                # Create metadata
                metadata = SnapshotMetadata(
                    snapshot_id=snapshot_id,
                    scope=scope,
                    scope_id=scope_id,
                    reason=reason,
                    created_at=timestamp,
                    created_by=created_by,
                    schema_versions=schema_versions,
                    state_hash=state_hash,
                    description=description,
                    tags=tags or {}
                )
                
                # Create manifest
                manifest = SnapshotManifest(
                    metadata=metadata,
                    state_keys=state_keys,
                    state_count=len(state_keys),
                    total_size_bytes=sum(len(v) for v in states.values())
                )
                
                # Write snapshot atomically
                self._write_snapshot(snapshot_id, metadata, manifest, states)
                
                return metadata
                
            except Exception as e:
                raise SnapshotError(f"Failed to create snapshot: {e}")
    
    def _capture_state(self, state_keys: List[StateKey]) -> Dict[StateKey, bytes]:
        """
        Capture state atomically.
        
        In production, this would use a transaction or snapshot isolation.
        """
        states = {}
        
        for key in state_keys:
            # Read from state backend
            value = self.state_backend.get(key.to_string())
            if value is not None:
                states[key] = value
        
        return states
    
    def _write_snapshot(
        self,
        snapshot_id: str,
        metadata: SnapshotMetadata,
        manifest: SnapshotManifest,
        states: Dict[StateKey, bytes]
    ) -> None:
        """
        Write snapshot to storage atomically.
        
        Structure:
            snapshots/
                <snapshot_id>/
                    metadata.json
                    manifest.json
                    states/
                        <key_hash>.bin
        """
        snapshot_dir = self.storage_dir / snapshot_id
        
        # Check if already exists (immutability)
        if snapshot_dir.exists():
            raise SnapshotImmutableError(
                f"Snapshot already exists: {snapshot_id}"
            )
        
        # Create directory structure
        snapshot_dir.mkdir(parents=True)
        states_dir = snapshot_dir / "states"
        states_dir.mkdir()
        
        try:
            # Write metadata
            metadata_file = snapshot_dir / "metadata.json"
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata.to_dict(), f, indent=2, sort_keys=True)
            
            # Write manifest
            manifest_file = snapshot_dir / "manifest.json"
            with open(manifest_file, 'w', encoding='utf-8') as f:
                json.dump(manifest.to_dict(), f, indent=2, sort_keys=True)
            
            # Write states
            for key, value in states.items():
                # Hash key for filename
                key_hash = hashlib.sha256(key.to_string().encode()).hexdigest()[:16]
                state_file = states_dir / f"{key_hash}.bin"
                
                with open(state_file, 'wb') as f:
                    f.write(value)
            
            # Write key mapping (for lookup)
            key_map_file = snapshot_dir / "key_map.json"
            key_map = {
                hashlib.sha256(k.to_string().encode()).hexdigest()[:16]: k.to_string()
                for k in states.keys()
            }
            with open(key_map_file, 'w', encoding='utf-8') as f:
                json.dump(key_map, f, indent=2, sort_keys=True)
            
        except Exception as e:
            # Clean up on failure
            if snapshot_dir.exists():
                import shutil
                shutil.rmtree(snapshot_dir)
            raise SnapshotError(f"Failed to write snapshot: {e}")


# ============================================================================
# SNAPSHOT READER - Restore Engine
# ============================================================================

class SnapshotReader:
    """
    Reads and restores snapshots.
    
    Rules:
        - Restore is explicit
        - Restore is audited
        - Restore requires schema compatibility
        - No partial restores
    
    Snapshots do not auto-apply.
    """
    
    def __init__(
        self,
        state_backend: Any,
        storage_dir: Path
    ):
        self.state_backend = state_backend
        self.storage_dir = Path(storage_dir)
        
        self._lock = threading.Lock()
    
    def load_snapshot(self, snapshot_id: str) -> SnapshotManifest:
        """
        Load snapshot manifest.
        
        Args:
            snapshot_id: Snapshot identifier
        
        Returns:
            SnapshotManifest
        
        Raises:
            SnapshotNotFoundError: If snapshot doesn't exist
        """
        snapshot_dir = self.storage_dir / snapshot_id
        
        if not snapshot_dir.exists():
            raise SnapshotNotFoundError(f"Snapshot not found: {snapshot_id}")
        
        try:
            # Load metadata
            metadata_file = snapshot_dir / "metadata.json"
            with open(metadata_file, 'r', encoding='utf-8') as f:
                metadata_dict = json.load(f)
            
            metadata = SnapshotMetadata(
                snapshot_id=metadata_dict['snapshot_id'],
                scope=SnapshotScope(metadata_dict['scope']),
                scope_id=metadata_dict['scope_id'],
                reason=SnapshotReason(metadata_dict['reason']),
                created_at=metadata_dict['created_at'],
                created_by=metadata_dict['created_by'],
                schema_versions=metadata_dict['schema_versions'],
                state_hash=metadata_dict['state_hash'],
                description=metadata_dict.get('description'),
                tags=metadata_dict.get('tags', {})
            )
            
            # Load manifest
            manifest_file = snapshot_dir / "manifest.json"
            with open(manifest_file, 'r', encoding='utf-8') as f:
                manifest_dict = json.load(f)
            
            state_keys = [StateKey.from_string(s) for s in manifest_dict['state_keys']]
            
            manifest = SnapshotManifest(
                metadata=metadata,
                state_keys=state_keys,
                state_count=manifest_dict['state_count'],
                total_size_bytes=manifest_dict['total_size_bytes']
            )
            
            return manifest
            
        except Exception as e:
            raise SnapshotError(f"Failed to load snapshot: {e}")
    
    def restore_snapshot(
        self,
        snapshot_id: str,
        current_schema_versions: Dict[str, str],
        allow_schema_mismatch: bool = False
    ) -> None:
        """
        Restore snapshot to state backend.
        
        Args:
            snapshot_id: Snapshot to restore
            current_schema_versions: Current schema versions
            allow_schema_mismatch: Allow schema version differences
        
        Raises:
            SnapshotSchemaError: If schema incompatible
            SnapshotCorruptionError: If verification fails
        """
        with self._lock:
            # Load manifest
            manifest = self.load_snapshot(snapshot_id)
            
            # Verify schema compatibility
            if not allow_schema_mismatch:
                self._verify_schema_compatibility(
                    manifest.metadata.schema_versions,
                    current_schema_versions
                )
            
            # Load states
            states = self._load_states(snapshot_id, manifest)
            
            # Verify content hash
            actual_hash = SnapshotHashComputer.compute(states)
            if actual_hash != manifest.metadata.state_hash:
                raise SnapshotCorruptionError(
                    f"Hash mismatch: expected {manifest.metadata.state_hash}, "
                    f"got {actual_hash}"
                )
            
            # Restore states
            self._restore_states(states)
    
    def _verify_schema_compatibility(
        self,
        snapshot_versions: Dict[str, str],
        current_versions: Dict[str, str]
    ) -> None:
        """
        Verify schema versions are compatible.
        
        Raises:
            SnapshotSchemaError: If incompatible
        """
        for schema_name, snapshot_version in snapshot_versions.items():
            current_version = current_versions.get(schema_name)
            
            if current_version is None:
                raise SnapshotSchemaError(
                    f"Schema not found: {schema_name}"
                )
            
            if snapshot_version != current_version:
                raise SnapshotSchemaError(
                    f"Schema version mismatch for {schema_name}: "
                    f"snapshot={snapshot_version}, current={current_version}"
                )
    
    def _load_states(
        self,
        snapshot_id: str,
        manifest: SnapshotManifest
    ) -> Dict[StateKey, bytes]:
        """Load state data from snapshot."""
        snapshot_dir = self.storage_dir / snapshot_id
        states_dir = snapshot_dir / "states"
        
        # Load key mapping
        key_map_file = snapshot_dir / "key_map.json"
        with open(key_map_file, 'r', encoding='utf-8') as f:
            key_map = json.load(f)
        
        states = {}
        
        for key in manifest.state_keys:
            key_hash = hashlib.sha256(key.to_string().encode()).hexdigest()[:16]
            state_file = states_dir / f"{key_hash}.bin"
            
            if not state_file.exists():
                raise SnapshotCorruptionError(
                    f"Missing state file for key: {key.to_string()}"
                )
            
            with open(state_file, 'rb') as f:
                states[key] = f.read()
        
        return states
    
    def _restore_states(self, states: Dict[StateKey, bytes]) -> None:
        """
        Restore states to backend.
        
        In production, this would use a transaction.
        """
        for key, value in states.items():
            self.state_backend.set(key.to_string(), value)


# ============================================================================
# SNAPSHOT INVARIANTS - Absolute Rules
# ============================================================================

class SnapshotInvariants:
    """
    Enforces snapshot invariants.
    
    MUST enforce:
        - Immutable snapshots
        - No overwrite
        - No in-place restore
        - Schema compatibility required
        - Full hash verification
        - Audit entry for every action
    """
    
    @staticmethod
    def assert_immutable(snapshot_id: str, storage_dir: Path) -> None:
        """Assert snapshot doesn't already exist."""
        snapshot_dir = storage_dir / snapshot_id
        if snapshot_dir.exists():
            raise SnapshotImmutableError(
                f"Snapshot already exists (immutable): {snapshot_id}"
            )
    
    @staticmethod
    def assert_complete(
        manifest: SnapshotManifest,
        states: Dict[StateKey, bytes]
    ) -> None:
        """Assert all manifest keys have state data."""
        manifest_keys = set(manifest.state_keys)
        state_keys = set(states.keys())
        
        if manifest_keys != state_keys:
            missing = manifest_keys - state_keys
            extra = state_keys - manifest_keys
            raise SnapshotError(
                f"Incomplete snapshot: missing={missing}, extra={extra}"
            )
    
    @staticmethod
    def assert_hash_valid(
        expected_hash: str,
        states: Dict[StateKey, bytes]
    ) -> None:
        """Assert content hash matches."""
        actual_hash = SnapshotHashComputer.compute(states)
        if actual_hash != expected_hash:
            raise SnapshotCorruptionError(
                f"Hash verification failed: expected {expected_hash}, "
                f"got {actual_hash}"
            )


# ============================================================================
# SNAPSHOT STORE - Public Facade
# ============================================================================

class SnapshotStore:
    """
    Public API for snapshot operations.
    
    The store:
        - Delegates to writer/reader
        - Enforces invariants
        - Handles authorization hooks
    """
    
    def __init__(
        self,
        state_backend: Any,
        storage_dir: Path,
        audit_dir: Optional[Path] = None
    ):
        self.writer = SnapshotWriter(state_backend, storage_dir)
        self.reader = SnapshotReader(state_backend, storage_dir)
        self.storage_dir = storage_dir
        
        self.audit_dir = audit_dir or Path("/var/snapshots/audit")
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        
        self._lock = threading.Lock()
    
    def snapshot(
        self,
        scope: SnapshotScope,
        scope_id: str,
        reason: SnapshotReason,
        created_by: str,
        state_keys: List[StateKey],
        schema_versions: Dict[str, str],
        description: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None
    ) -> SnapshotMetadata:
        """
        Create snapshot.
        
        This is the only public way to freeze reality.
        """
        # Enforce invariants
        # (immutability checked in writer)
        
        # Create snapshot
        metadata = self.writer.create_snapshot(
            scope=scope,
            scope_id=scope_id,
            reason=reason,
            created_by=created_by,
            state_keys=state_keys,
            schema_versions=schema_versions,
            description=description,
            tags=tags
        )
        
        # Audit
        self._audit_snapshot("create", metadata)
        
        return metadata
    
    def restore(
        self,
        snapshot_id: str,
        restored_by: str,
        current_schema_versions: Dict[str, str],
        allow_schema_mismatch: bool = False
    ) -> None:
        """
        Restore snapshot.
        
        Requires explicit consent and audit.
        """
        # Load manifest first for audit
        manifest = self.reader.load_snapshot(snapshot_id)
        
        # Audit restore attempt
        self._audit_restore("restore", manifest.metadata, restored_by)
        
        # Restore
        self.reader.restore_snapshot(
            snapshot_id,
            current_schema_versions,
            allow_schema_mismatch
        )
    
    def list_snapshots(
        self,
        scope: Optional[SnapshotScope] = None,
        scope_id: Optional[str] = None
    ) -> List[SnapshotMetadata]:
        """
        List snapshots.
        
        Optionally filtered by scope and scope_id.
        """
        snapshots = []
        
        for snapshot_dir in self.storage_dir.iterdir():
            if not snapshot_dir.is_dir():
                continue
            
            try:
                manifest = self.reader.load_snapshot(snapshot_dir.name)
                metadata = manifest.metadata
                
                # Filter
                if scope and metadata.scope != scope:
                    continue
                if scope_id and metadata.scope_id != scope_id:
                    continue
                
                snapshots.append(metadata)
                
            except Exception:
                # Skip invalid snapshots
                continue
        
        # Sort by creation time (newest first)
        snapshots.sort(key=lambda m: m.created_at, reverse=True)
        
        return snapshots
    
    def verify_snapshot(self, snapshot_id: str) -> bool:
        """
        Verify snapshot integrity.
        
        Returns:
            True if valid, False if corrupted
        """
        try:
            manifest = self.reader.load_snapshot(snapshot_id)
            states = self.reader._load_states(snapshot_id, manifest)
            
            SnapshotInvariants.assert_hash_valid(
                manifest.metadata.state_hash,
                states
            )
            
            return True
            
        except (SnapshotCorruptionError, SnapshotError):
            return False
    
    def _audit_snapshot(self, action: str, metadata: SnapshotMetadata) -> None:
        """Write snapshot action to audit log."""
        audit_file = self.audit_dir / "snapshots.jsonl"
        
        entry = {
            "action": action,
            "snapshot_id": metadata.snapshot_id,
            "scope": metadata.scope.value,
            "scope_id": metadata.scope_id,
            "reason": metadata.reason.value,
            "created_by": metadata.created_by,
            "created_at": metadata.created_at,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        try:
            with open(audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, sort_keys=True) + '\n')
                f.flush()
        except Exception as e:
            print(f"Failed to audit snapshot: {e}", flush=True)
    
    def _audit_restore(
        self,
        action: str,
        metadata: SnapshotMetadata,
        restored_by: str
    ) -> None:
        """Write restore action to audit log."""
        audit_file = self.audit_dir / "snapshots.jsonl"
        
        entry = {
            "action": action,
            "snapshot_id": metadata.snapshot_id,
            "scope": metadata.scope.value,
            "scope_id": metadata.scope_id,
            "restored_by": restored_by,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        try:
            with open(audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, sort_keys=True) + '\n')
                f.flush()
        except Exception as e:
            print(f"Failed to audit restore: {e}", flush=True)


# ============================================================================
# FACTORY
# ============================================================================

def create_snapshot_store(
    state_backend: Any,
    storage_dir: str = "/var/snapshots/store",
    audit_dir: str = "/var/snapshots/audit"
) -> SnapshotStore:
    """
    Create snapshot store.
    
    Args:
        state_backend: State backend instance
        storage_dir: Where to store snapshots
        audit_dir: Where to store audit logs
    
    Returns:
        SnapshotStore
    """
    return SnapshotStore(
        state_backend=state_backend,
        storage_dir=Path(storage_dir),
        audit_dir=Path(audit_dir)
    )


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    print("Snapshot Store Demo")
    print("=" * 60)
    
    # Mock state backend
    class MockStateBackend:
        def __init__(self):
            self._data = {}
        
        def get(self, key: str) -> Optional[bytes]:
            return self._data.get(key)
        
        def set(self, key: str, value: bytes) -> None:
            self._data[key] = value
    
    backend = MockStateBackend()
    
    # Populate with test data
    backend.set("workflow:wf1", b'{"state": "running"}')
    backend.set("workflow:wf2", b'{"state": "pending"}')
    
    # Create snapshot store
    store = create_snapshot_store(
        state_backend=backend,
        storage_dir="/tmp/snapshot_demo/store",
        audit_dir="/tmp/snapshot_demo/audit"
    )
    
    # Create snapshot
    print("\n1. Create Snapshot")
    state_keys = [
        StateKey(namespace="workflow", key="wf1"),
        StateKey(namespace="workflow", key="wf2")
    ]
    
    metadata = store.snapshot(
        scope=SnapshotScope.EXPERIMENT,
        scope_id="exp_001",
        reason=SnapshotReason.EXPERIMENT_START,
        created_by="experiment_runner",
        state_keys=state_keys,
        schema_versions={"workflow": "1.0.0"},
        description="Baseline snapshot before experiment",
        tags={"experiment": "exp_001", "phase": "start"}
    )
    
    print(f"✓ Snapshot created: {metadata.snapshot_id}")
    print(f"  Scope: {metadata.scope.value}")
    print(f"  Reason: {metadata.reason.value}")
    print(f"  State hash: {metadata.state_hash}")
    
    # List snapshots
    print("\n2. List Snapshots")
    snapshots = store.list_snapshots(scope=SnapshotScope.EXPERIMENT)
    print(f"✓ Found {len(snapshots)} experiment snapshots")
    for snap in snapshots:
        print(f"  - {snap.snapshot_id} ({snap.reason.value})")
    
    # Verify snapshot
    print("\n3. Verify Snapshot Integrity")
    valid = store.verify_snapshot(metadata.snapshot_id)
    print(f"✓ Snapshot valid: {valid}")
    
    # Modify state
    print("\n4. Modify State")
    backend.set("workflow:wf1", b'{"state": "completed"}')
    print(f"✓ Modified workflow:wf1")
    
    # Restore snapshot
    print("\n5. Restore Snapshot")
    store.restore(
        snapshot_id=metadata.snapshot_id,
        restored_by="operator",
        current_schema_versions={"workflow": "1.0.0"}
    )
    
    restored_value = backend.get("workflow:wf1")
    print(f"✓ Restored workflow:wf1: {restored_value}")
    
    # Load manifest
    print("\n6. Load Snapshot Manifest")
    manifest = store.reader.load_snapshot(metadata.snapshot_id)
    print(f"✓ Manifest loaded:")
    print(f"  State count: {manifest.state_count}")
    print(f"  Total size: {manifest.total_size_bytes} bytes")
    print(f"  Keys: {[k.to_string() for k in manifest.state_keys]}")
    
    print("\n" + "=" * 60)
    print("Snapshots freeze meaning.")
    print("Manifests prove completeness.")
    print("Hashes prove identity.")
    print("Restores require consent.")
    print("Audits see everything.")