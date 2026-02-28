"""
Backend Adapters

Adapters make backend implementations compatible with BackendBase interface.
"""

from infra.persistence.backends.adapters.memory_adapter import MemoryBackendAdapter
from infra.persistence.backends.adapters.filesystem_adapter import FilesystemBackendAdapter

__all__ = [
    "MemoryBackendAdapter",
    "FilesystemBackendAdapter",
]
