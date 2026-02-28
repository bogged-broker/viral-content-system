"""
Backend Configuration Schemas

Immutable configuration dataclasses for backend instantiation.
All configs are frozen and fully resolved before reaching factory.
"""

from __future__ import annotations

from typing import Optional, Dict, Any
from dataclasses import dataclass


@dataclass(frozen=True)
class BackendConfig:
    """
    Base configuration for backend instantiation.
    Immutable and fully resolved before reaching factory.
    """
    backend_type: str
    environment: str
    version: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            "backend_type": self.backend_type,
            "environment": self.environment,
            "version": self.version,
        }


@dataclass(frozen=True)
class MemoryBackendConfig(BackendConfig):
    """Configuration for in-memory backend."""
    max_size_mb: Optional[int] = None
    eviction_policy: Optional[str] = None


@dataclass(frozen=True)
class KVBackendConfig(BackendConfig):
    """Configuration for key-value store backend."""
    storage_path: str
    create_if_missing: bool = True
    sync_writes: bool = True
    compression_enabled: bool = False


@dataclass(frozen=True)
class RedisBackendConfig(BackendConfig):
    """Configuration for Redis backend."""
    host: str
    port: int
    db: int = 0
    password: Optional[str] = None
    ssl_enabled: bool = False
    connection_timeout_ms: int = 5000
    max_retries: int = 3


@dataclass(frozen=True)
class S3BackendConfig(BackendConfig):
    """Configuration for S3-compatible backend."""
    bucket_name: str
    region: str
    endpoint_url: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    prefix: str = ""
