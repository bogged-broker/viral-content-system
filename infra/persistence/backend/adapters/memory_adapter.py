"""
Memory Backend Adapter

Adapter wrapper for MemoryBackend to match BackendBase interface.
MemoryBackend uses set() instead of put() and doesn't have get_capabilities(),
so this adapter provides the missing methods.
"""

from typing import Optional, Dict, Any
from infra.persistence.backends.backend_base import BackendCapabilities, DurabilityLevel, IsolationLevel


class MemoryBackendAdapter:
    """
    Adapter wrapper for MemoryBackend to match BackendBase interface.
    
    MemoryBackend uses set() instead of put() and doesn't have get_capabilities(),
    so this adapter provides the missing methods.
    """
    
    def __init__(self, backend: Any):
        """Initialize adapter with actual MemoryBackend instance."""
        self._backend = backend
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        return self._backend.exists(key)
    
    def get(self, key: str) -> Optional[bytes]:
        """Get raw bytes for key. Returns None if key does not exist."""
        # Check existence first to avoid exception
        if not self._backend.exists(key):
            return None
        
        try:
            return self._backend.get(key)
        except Exception:
            # If get() still raises (shouldn't happen if exists() is correct), return None
            return None
    
    def put(
        self,
        key: str,
        value: bytes,
        mode: Any = None,
        expected_version: Optional[int] = None
    ) -> None:
        """Write raw bytes to key."""
        # MemoryBackend uses set() instead of put()
        # Ignore mode and expected_version for now (MemoryBackend doesn't support them)
        self._backend.set(key, value)
    
    def delete(self, key: str) -> None:
        """Delete key."""
        self._backend.delete(key)
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """Get backend-specific metadata for key."""
        try:
            return self._backend.get_metadata(key)
        except Exception:
            # Return empty dict if key doesn't exist (BackendBase contract)
            return {}
    
    def get_capabilities(self) -> BackendCapabilities:
        """Get backend capabilities."""
        # Create BackendCapabilities matching MemoryBackend's actual capabilities
        return BackendCapabilities(
            durability_level=DurabilityLevel.EPHEMERAL,
            isolation_level=IsolationLevel.NONE,
            supports_transactions=self._backend.supports_transactions(),
            supports_batching=True,
            supports_cas=False,
            thread_safe=False,  # Based on constructor parameter
            process_safe=False,
            distributed_safe=False
        )
