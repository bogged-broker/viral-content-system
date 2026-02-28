"""
Filesystem Backend Adapter

Adapter wrapper for FilesystemBackend to match BackendBase interface.
FilesystemBackend uses put_blob()/get_blob() instead of put()/get(),
so this adapter provides the standard interface.
"""

from typing import Optional, Dict, Any
from infra.persistence.backends.backend_base import BackendCapabilities, DurabilityLevel, IsolationLevel


class FilesystemBackendAdapter:
    """
    Adapter wrapper for FilesystemBackend to match BackendBase interface.
    
    FilesystemBackend uses put_blob()/get_blob() instead of put()/get(),
    so this adapter provides the standard interface.
    """
    
    def __init__(self, backend: Any):
        """Initialize adapter with actual FilesystemBackend instance."""
        self._backend = backend
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        try:
            # Try to get latest version - if it exists, key exists
            self._backend._get_latest_version(key)
            return True
        except Exception:
            return False
    
    def get(self, key: str) -> Optional[bytes]:
        """Get raw bytes for key. Returns None if key does not exist."""
        try:
            return self._backend.get_blob(key)
        except Exception:
            return None
    
    def put(
        self,
        key: str,
        value: bytes,
        mode: Any = None,
        expected_version: Optional[int] = None
    ) -> None:
        """Write raw bytes to key."""
        # FilesystemBackend uses put_blob() instead of put()
        # Ignore mode and expected_version for now
        self._backend.put_blob(key, value)
    
    def delete(self, key: str) -> None:
        """Delete key."""
        self._backend.delete_blob(key)
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """Get backend-specific metadata for key."""
        try:
            return self._backend.get_metadata(key)
        except Exception:
            return {}
    
    def get_capabilities(self) -> BackendCapabilities:
        """Get backend capabilities."""
        fs_caps = self._backend.get_capabilities()
        # Convert FilesystemBackendCapabilities to BackendCapabilities
        # FilesystemBackendCapabilities has different structure, so map appropriately
        return BackendCapabilities(
            durability_level=DurabilityLevel.STRONG,  # Filesystem is durable
            isolation_level=IsolationLevel.READ_COMMITTED,  # Filesystem provides basic isolation
            supports_transactions=getattr(fs_caps, 'supports_transactions', False),
            supports_batching=True,  # Filesystem can batch via staging
            supports_cas=False,
            thread_safe=True,  # Filesystem handles concurrent access
            process_safe=True,  # Filesystem is process-safe
            distributed_safe=False  # Not distributed
        )
