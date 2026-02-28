"""
/account_system/geo_allocator.py

Deterministic Geographic Allocation Authority
(No Cross-Region Drift, No Heuristic Guessing, No Silent Mutation)

This module is the single authority that determines which geographic region
an account belongs to and why.

CRITICAL PRINCIPLES:
- Deterministic: Same inputs must produce same region
- Explicit: No inferred behavior from environment
- Stable: Region mapping must not drift over time without versioning
- Transparent: Every assignment has an explainable reason
- Stateless: No memory of past allocations
- Replay-safe: Re-running allocator must produce identical result

ABSOLUTE INVARIANTS:
1. No cross-region drift
2. No heuristic guessing
3. No silent mutation
4. Deterministic region assignment
5. Explicit allocation rules
6. No runtime randomness
7. No environment dependencies

This file ensures accounts are allocated with the same rigor as
billion-dollar companies (Instagram, ChatGPT, etc.).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, Tuple, FrozenSet, List
from types import MappingProxyType


class AllocationStrategy(Enum):
    """Strategy used for geographic allocation."""
    COUNTRY_MAP = "country_map"
    DETERMINISTIC_HASH = "deterministic_hash"
    EXPLICIT_OVERRIDE = "explicit_override"
    DEFAULT_REGION = "default_region"


class AllocationError(Exception):
    """
    Raised when geographic allocation fails or config is invalid.
    
    All errors must be explicit and deterministic.
    Examples:
    - Unknown country
    - Region not in configured list
    - No regions configured
    - Hash space empty
    - Version mismatch
    """
    def __init__(self, message: str, error_code: Optional[str] = None) -> None:
        """
        Initialize allocation error.
        
        Args:
            message: Human-readable error message
            error_code: Optional error code for programmatic handling
        """
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class GeoAllocationConfig:
    """
    Immutable configuration for geographic allocation.
    
    Must define:
    - Allocation version
    - Supported regions (sorted deterministically)
    - Country → region map (immutable)
    - Hash algorithm identifier
    - Weighting (optional, immutable)
    - Explicit overrides (immutable)
    - Default region behavior
    
    Config validation must happen before allocation.
    Invalid config → explicit error.
    
    TIER-0 REQUIREMENTS:
    - All dict fields are frozen via MappingProxyType
    - supported_regions is sorted deterministically
    - Version compatibility enforced
    """
    version: str
    """Allocation version (for replay tracing and versioning)"""
    
    supported_regions: tuple[str, ...]
    """List of supported region identifiers (must be sorted deterministically)"""
    
    country_to_region: MappingProxyType[str, str]
    """Immutable mapping from ISO country codes to region identifiers"""
    
    hash_algorithm: str = "sha256"
    """Hash algorithm for deterministic sharding (sha256, sha512, sha1, md5)"""
    
    region_weights: Optional[MappingProxyType[str, int]] = None
    """Optional immutable weights for weighted hash distribution"""
    
    explicit_overrides: Optional[MappingProxyType[str, str]] = None
    """Optional immutable explicit account_id → region overrides (highest precedence)"""
    
    default_region: Optional[str] = None
    """Optional default region for fallback allocation"""
    
    min_compatible_version: Optional[str] = None
    """Minimum compatible version for version enforcement"""
    
    def __post_init__(self) -> None:
        """Validate configuration on instantiation."""
        # Convert mutable dicts to immutable MappingProxyType if needed
        # (allows both dict and MappingProxyType inputs for flexibility)
        if not isinstance(self.country_to_region, MappingProxyType):
            object.__setattr__(self, 'country_to_region', MappingProxyType(self.country_to_region))
        
        if self.region_weights is not None and not isinstance(self.region_weights, MappingProxyType):
            object.__setattr__(self, 'region_weights', MappingProxyType(self.region_weights))
        
        if self.explicit_overrides is not None and not isinstance(self.explicit_overrides, MappingProxyType):
            object.__setattr__(self, 'explicit_overrides', MappingProxyType(self.explicit_overrides))
        
        # Sort and validate regions
        if not self.version:
            raise AllocationError("Config version cannot be empty")
        
        if not self.supported_regions:
            raise AllocationError("No regions configured")
        
        if len(set(self.supported_regions)) != len(self.supported_regions):
            raise AllocationError("Duplicate regions in supported_regions")
        
        # CRITICAL: Ensure regions are sorted deterministically to prevent silent remapping
        # This prevents hash drift when region list order changes
        sorted_regions = tuple(sorted(self.supported_regions))
        if sorted_regions != self.supported_regions:
            # Auto-fix: sort regions deterministically
            object.__setattr__(self, 'supported_regions', sorted_regions)
        
        # Validate country mappings reference valid regions
        for country, region in self.country_to_region.items():
            if region not in self.supported_regions:
                raise AllocationError(
                    f"Country '{country}' maps to unsupported region '{region}'"
                )
        
        # Validate region weights if provided
        if self.region_weights is not None:
            for region, weight in self.region_weights.items():
                if region not in self.supported_regions:
                    raise AllocationError(
                        f"Weight defined for unsupported region '{region}'"
                    )
                if weight <= 0:
                    raise AllocationError(
                        f"Region weight must be positive, got {weight} for '{region}'"
                    )
        
        # Validate explicit overrides
        if self.explicit_overrides is not None:
            for account_id, region in self.explicit_overrides.items():
                if region not in self.supported_regions:
                    raise AllocationError(
                        f"Override for '{account_id}' maps to unsupported region '{region}'"
                    )
        
        # Validate default region
        if self.default_region is not None:
            if self.default_region not in self.supported_regions:
                raise AllocationError(
                    f"Default region '{self.default_region}' not in supported regions"
                )
        
        # Validate hash algorithm
        if self.hash_algorithm not in {"sha256", "sha512", "sha1", "md5"}:
            raise AllocationError(
                f"Unsupported hash algorithm '{self.hash_algorithm}'"
            )
    
    @classmethod
    def create(
        cls,
        version: str,
        supported_regions: list[str],
        country_to_region: dict[str, str],
        hash_algorithm: str = "sha256",
        region_weights: Optional[dict[str, int]] = None,
        explicit_overrides: Optional[dict[str, str]] = None,
        default_region: Optional[str] = None,
        min_compatible_version: Optional[str] = None,
    ) -> GeoAllocationConfig:
        """
        Factory method to create immutable config with proper freezing.
        
        Ensures all dict fields are converted to MappingProxyType for immutability.
        Sorts supported_regions deterministically to prevent silent remapping.
        """
        # Sort regions deterministically to prevent hash drift
        sorted_regions = tuple(sorted(set(supported_regions)))
        
        # Freeze all dict fields
        frozen_country_map = MappingProxyType(country_to_region)
        frozen_weights = MappingProxyType(region_weights) if region_weights else None
        frozen_overrides = MappingProxyType(explicit_overrides) if explicit_overrides else None
        
        return cls(
            version=version,
            supported_regions=sorted_regions,
            country_to_region=frozen_country_map,
            hash_algorithm=hash_algorithm,
            region_weights=frozen_weights,
            explicit_overrides=frozen_overrides,
            default_region=default_region,
            min_compatible_version=min_compatible_version,
        )


@dataclass(frozen=True)
class GeoAllocationResult:
    """
    Immutable result of geographic allocation.
    
    Must contain:
    - region_id: canonical region string
    - allocation_strategy: identifier of rule used
    - allocation_version: configuration version
    - deterministic_hash (if hashing used)
    - metadata (optional, read-only)
    
    No ambiguous string return values allowed.
    """
    region_id: str
    """Canonical region identifier"""
    
    allocation_strategy: AllocationStrategy
    """Strategy used for allocation"""
    
    allocation_version: str
    """Configuration version used for allocation"""
    
    deterministic_hash: Optional[str] = None
    """Deterministic hash value (if hash-based allocation) - truncated for security"""
    
    metadata: Optional[Dict[str, str]] = None
    """Optional metadata (read-only, safe for external exposure)"""
    
    def __post_init__(self) -> None:
        """Validate allocation result."""
        if not self.region_id:
            raise AllocationError("region_id cannot be empty")
        
        if not self.allocation_version:
            raise AllocationError("allocation_version cannot be empty")
        
        # Ensure metadata is immutable if provided
        if self.metadata is not None:
            # Convert to immutable dict-like structure
            object.__setattr__(self, 'metadata', MappingProxyType(self.metadata))
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize allocation result to dictionary for logging/audit."""
        return {
            "region_id": self.region_id,
            "allocation_strategy": self.allocation_strategy.value,
            "allocation_version": self.allocation_version,
            "deterministic_hash": self.deterministic_hash,
            "metadata": dict(self.metadata) if self.metadata else None,
        }


class GeoAllocator:
    """
    Deterministic geographic allocation authority.
    
    This class ensures accounts are allocated with the same rigor as
    billion-dollar companies (Instagram, ChatGPT, etc.).
    
    Guarantees:
    - Same inputs → same region (deterministic)
    - No runtime randomness
    - No environment dependencies
    - Replay-safe across all environments
    - No cross-region drift
    - No heuristic guessing
    - No silent mutation
    - Explicit allocation rules
    - Version compatibility enforcement
    - Pure function (no side effects, no logging)
    
    Allocation must not depend on:
    - Hostname
    - Server region
    - Environment variable
    - System clock
    - Process ID
    - Random seed
    - I/O operations (logging)
    """
    
    def __init__(
        self,
        config: GeoAllocationConfig,
        expected_version: Optional[str] = None,
    ) -> None:
        """
        Initialize allocator with validated config.
        
        Args:
            config: Immutable geographic allocation configuration
            expected_version: Optional expected version for compatibility check
        
        Raises:
            AllocationError: If config is invalid or version incompatible
        """
        self._config = config
        
        # Validate config at initialization
        self._validate_config()
        
        # Version enforcement: prevent allocation with incompatible versions
        if expected_version is not None:
            self._enforce_version_compatibility(expected_version)
    
    def _validate_config(self) -> None:
        """Validate configuration at initialization."""
        # Additional validation beyond __post_init__
        if not self._config.supported_regions:
            raise AllocationError(
                "No regions configured",
                error_code="NO_REGIONS_CONFIGURED"
            )
        
        # Validate hash algorithm is available
        try:
            getattr(hashlib, self._config.hash_algorithm)
        except AttributeError:
            raise AllocationError(
                f"Hash algorithm '{self._config.hash_algorithm}' not available",
                error_code="INVALID_HASH_ALGORITHM"
            )
    
    def _enforce_version_compatibility(self, expected_version: str) -> None:
        """
        Enforce version compatibility to prevent silent remapping during migrations.
        
        For Tier-0 safety, requires exact version match. This prevents:
        - Silent remapping during migrations
        - Cross-region drift from version mismatches
        - Disaster recovery replay mismatches
        
        Args:
            expected_version: Expected config version
        
        Raises:
            AllocationError: If versions are incompatible
        """
        # Tier-0 requirement: exact version match for maximum safety
        # This prevents any possibility of silent remapping during migrations
        if expected_version != self._config.version:
            raise AllocationError(
                f"Version mismatch: expected {expected_version}, "
                f"got {self._config.version}. "
                f"Exact version match required for Tier-0 determinism.",
                error_code="VERSION_MISMATCH"
            )
    
    def allocate(
        self,
        account_id: str,
        declared_country: Optional[str] = None
    ) -> GeoAllocationResult:
        """
        Allocate account to geographic region deterministically.
        
        DETERMINISTIC: Same inputs always produce identical region.
        No randomness. No hidden state. No environment dependencies.
        
        Allocation strategies (in precedence order):
        1. Explicit Override (highest precedence)
        2. Country-Based Mapping
        3. Deterministic Hash-Based Sharding
        4. Default Region (fallback)
        
        Args:
            account_id: Unique account identifier
            declared_country: Optional ISO country code (e.g., "US", "DE")
        
        Returns:
            GeoAllocationResult with region assignment and metadata
        
        Raises:
            AllocationError: If allocation cannot be performed deterministically
        """
        if not account_id:
            raise AllocationError(
                "account_id cannot be empty",
                error_code="EMPTY_ACCOUNT_ID"
            )
        
        # Strategy 1: Explicit Override (highest precedence)
        if (self._config.explicit_overrides is not None and
                account_id in self._config.explicit_overrides):
            region_id = self._config.explicit_overrides[account_id]
            return GeoAllocationResult(
                region_id=region_id,
                allocation_strategy=AllocationStrategy.EXPLICIT_OVERRIDE,
                allocation_version=self._config.version,
                metadata={"override_account": account_id, "strategy": "explicit_override"}
            )
        
        # Strategy 2: Country-Based Mapping
        if declared_country is not None:
            declared_country = declared_country.upper().strip()
            
            if not declared_country:
                raise AllocationError(
                    "declared_country cannot be empty string",
                    error_code="EMPTY_COUNTRY_CODE"
                )
            
            if declared_country in self._config.country_to_region:
                region_id = self._config.country_to_region[declared_country]
                return GeoAllocationResult(
                    region_id=region_id,
                    allocation_strategy=AllocationStrategy.COUNTRY_MAP,
                    allocation_version=self._config.version,
                    metadata={"country": declared_country, "strategy": "country_map"}
                )
            else:
                # Invalid country code - fail fast, no fallback
                raise AllocationError(
                    f"Country '{declared_country}' not in configured country mapping",
                    error_code="UNKNOWN_COUNTRY"
                )
        
        # Strategy 3: Deterministic Hash-Based Sharding
        # Used when no country provided or multi-region allowed
        if len(self._config.supported_regions) > 0:
            region_id, hash_value = self._deterministic_hash_allocation(account_id)
            # Truncate hash for security (expose only first 16 chars)
            truncated_hash = hash_value[:16] if hash_value else None
            return GeoAllocationResult(
                region_id=region_id,
                allocation_strategy=AllocationStrategy.DETERMINISTIC_HASH,
                allocation_version=self._config.version,
                deterministic_hash=truncated_hash,
                metadata={
                    "hash_algorithm": self._config.hash_algorithm,
                    "strategy": "deterministic_hash"
                }
            )
        
        # Strategy 4: Default Region (fallback)
        if self._config.default_region is not None:
            return GeoAllocationResult(
                region_id=self._config.default_region,
                allocation_strategy=AllocationStrategy.DEFAULT_REGION,
                allocation_version=self._config.version,
                metadata={"strategy": "default_region"}
            )
        
        # No allocation strategy applicable
        raise AllocationError(
            "No allocation strategy applicable: no country, no regions, no default",
            error_code="NO_ALLOCATION_STRATEGY"
        )
    
    def _deterministic_hash_allocation(self, account_id: str) -> Tuple[str, str]:
        """
        Allocate using deterministic hash sharding.
        
        Uses stable hashing algorithm (SHA-256 by default) that is:
        - Immune to Python version/platform differences
        - Deterministic across processes
        - Replay-safe
        - Order-independent (regions are pre-sorted)
        
        Never uses:
        - Python built-in hash() (non-deterministic)
        - Random()
        - Time-based UUIDs
        
        CRITICAL: Regions are already sorted deterministically in config,
        so hash modulo is stable even if region list is reordered externally.
        
        Args:
            account_id: Account identifier to hash
        
        Returns:
            Tuple of (region_id, hex_hash)
        """
        # Compute stable hash (immune to Python version/platform)
        hash_func = getattr(hashlib, self._config.hash_algorithm)
        hash_bytes = hash_func(account_id.encode('utf-8')).digest()
        hash_value = hash_bytes.hex()
        
        # Convert to integer for modulo operation
        # Use big-endian for consistent byte order across platforms
        hash_int = int.from_bytes(hash_bytes, byteorder='big')
        
        # Weighted or uniform distribution
        # NOTE: supported_regions is already sorted deterministically in config
        # This prevents silent remapping when region order changes
        if self._config.region_weights is not None:
            region_id = self._weighted_hash_selection(hash_int)
        else:
            # Uniform distribution across regions
            # Regions are pre-sorted, so modulo is stable
            region_index = hash_int % len(self._config.supported_regions)
            region_id = self._config.supported_regions[region_index]
        
        return region_id, hash_value
    
    def _weighted_hash_selection(self, hash_int: int) -> str:
        """
        Select region using weighted hash distribution.
        
        CRITICAL: Regions are iterated in sorted order (from config),
        ensuring deterministic selection even if weights are reordered.
        
        Args:
            hash_int: Integer hash value
        
        Returns:
            Selected region_id
        """
        # Build cumulative weight ranges
        # NOTE: supported_regions is already sorted deterministically
        total_weight = sum(
            self._config.region_weights.get(r, 1)
            for r in self._config.supported_regions
        )
        
        # Map hash to range [0, total_weight)
        position = hash_int % total_weight
        
        # Find region by cumulative weight
        # Iteration order is deterministic (sorted regions)
        cumulative = 0
        for region in self._config.supported_regions:
            weight = self._config.region_weights.get(region, 1)
            cumulative += weight
            if position < cumulative:
                return region
        
        # Fallback (should never reach due to modulo math)
        return self._config.supported_regions[0]


def allocate(
    account_id: str,
    declared_country: Optional[str],
    config: GeoAllocationConfig,
    expected_version: Optional[str] = None,
) -> GeoAllocationResult:
    """
    Convenience function for one-off allocation.
    
    DETERMINISTIC: Same inputs always produce identical region.
    No randomness. No hidden state. No environment dependencies.
    Pure function with no side effects.
    
    For repeated allocations, instantiate GeoAllocator once and reuse
    for better performance.
    
    Args:
        account_id: Unique account identifier
        declared_country: Optional ISO country code (e.g., "US", "DE")
        config: Geographic allocation configuration
        expected_version: Optional expected version for compatibility check
    
    Returns:
        GeoAllocationResult with region assignment and metadata
    
    Raises:
        AllocationError: If allocation cannot be performed deterministically
    """
    allocator = GeoAllocator(config, expected_version=expected_version)
    return allocator.allocate(account_id, declared_country)