"""
/config/runtime_infra.py

Runtime Infrastructure Configuration Models
(For Operational Concerns Only)

This module defines infrastructure and operational configuration schemas.
These are NOT part of canonical configuration identity.

These configs are:
- Operational knobs (cache, storage endpoints, telemetry)
- Infrastructure wiring details
- Performance tuning parameters
- Observability settings

They are separate from config_types.py because:
- Constitution defines canonical identity
- These are runtime operational concerns
- Changing these does not change computation identity
- They belong to infrastructure layer, not policy layer

All fields are required. No defaults.
Frozen (immutable after creation).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TelemetryConfig:
    """
    Telemetry and observability configuration.
    
    Defines telemetry collection and export behavior.
    This is an operational concern, not computation identity.
    
    Sampling rate uses fixed-point integer representation:
    - sampling_rate_per_million: Integer in range [0, 1000000]
    - Value represents parts per million (1.0 = 1000000, 0.5 = 500000)
    - Deterministic across all runtimes and languages
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        sampling_rate_per_million: Sampling rate in parts per million (0 to 1000000)
        export_interval_seconds: Export interval in seconds
        enabled: Whether telemetry is enabled
    """
    sampling_rate_per_million: int
    """Sampling rate in parts per million (0 to 1000000). 1000000 = 1.0, 500000 = 0.5"""
    
    export_interval_seconds: int
    """Export interval in seconds"""
    
    enabled: bool
    """Whether telemetry is enabled"""


@dataclass(frozen=True)
class StorageConfig:
    """
    Storage infrastructure configuration.
    
    Defines storage backend details.
    This is infrastructure wiring, not computation identity.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        bucket_name: Storage bucket name
        region: Storage region
        endpoint: Storage endpoint URL
    """
    bucket_name: str
    """Storage bucket name"""
    
    region: str
    """Storage region"""
    
    endpoint: str
    """Storage endpoint URL"""


@dataclass(frozen=True)
class CacheConfig:
    """
    Cache behavior configuration.
    
    Defines caching policy and limits.
    This is performance tuning, not computation identity.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        size_mb: Cache size in MB
        ttl_seconds: Time-to-live in seconds
        enabled: Whether caching is enabled
    """
    size_mb: int
    """Cache size in MB"""
    
    ttl_seconds: int
    """Time-to-live in seconds"""
    
    enabled: bool
    """Whether caching is enabled"""


__all__ = [
    "TelemetryConfig",
    "StorageConfig",
    "CacheConfig",
]
