"""
/experiments/variant_generator.py

Deterministic Variant Definition & Allocation Authority
(No Implicit Buckets, No Allocation Drift, No Structural Ambiguity)

This module is the single authority responsible for constructing valid experiment
variants and their allocation structure.

CRITICAL PRINCIPLES:
- Explicit variant identity (collision-resistant IDs)
- Valid traffic allocation (sum = 100%)
- Deterministic bucket mapping (integer-only math)
- Structural correctness (no overlaps, full coverage)
- Allocation immutability once finalized
- No floating-point boundary logic
- Same input → identical output (deterministic)

ABSOLUTE INVARIANTS:
1. Allocation sum must equal exactly 100%
2. Bucket ranges must fully cover domain
3. No overlapping ranges
4. Deterministic ordering only
5. Version bump required for any allocation change
6. No floating-point boundary logic
7. No hidden default control
8. Same input → identical output
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List, Tuple, FrozenSet
from enum import Enum
from types import MappingProxyType


class SchemaVersion(Enum):
    """
    Schema versioning for compatibility tracking.
    
    Any schema change must invalidate old runtime snapshots.
    """
    EXPERIMENT_V1 = "experiment_schema_v1"
    ALLOCATION_V1 = "allocation_schema_v1"
    
    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class VariantSpec:
    """
    Immutable specification for a single variant.
    
    All inputs must be immutable.
    No runtime mutation allowed after generation.
    """
    name: str
    """Human-readable variant name (must be unique within experiment)"""
    
    allocation_weight: int
    """Integer percentage or weight (must be >= 0, sum must = 100)"""
    
    parameter_payload: Dict[str, Any]
    """Configuration blob for this variant (immutable, will be wrapped in MappingProxyType)"""
    
    is_control: bool = False
    """Whether this is a control variant (explicit marking required)"""
    
    order_index: Optional[int] = None
    """Explicit ordering index for deterministic sorting (if None, uses variant_id after generation)"""
    
    def __post_init__(self):
        """Validate variant specification and make parameter_payload immutable."""
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Variant name cannot be empty and must be string")
        
        if self.allocation_weight < 0:
            raise ValueError(
                f"Negative allocation not allowed: {self.allocation_weight}. "
                f"Variant: {self.name}"
            )
        
        if not isinstance(self.allocation_weight, int):
            # Check if safely convertible to int
            if isinstance(self.allocation_weight, float):
                if self.allocation_weight.is_integer():
                    # This will be caught by type system, but defensive check
                    pass
                else:
                    raise ValueError(
                        f"Non-integer allocation weight not allowed: {self.allocation_weight}. "
                        f"Variant: {self.name}. Use integer percentages only."
                    )
            else:
                raise ValueError(
                    f"Allocation weight must be integer, got {type(self.allocation_weight)}. "
                    f"Variant: {self.name}"
                )
        
        # Make parameter_payload immutable using MappingProxyType
        # This prevents runtime mutation while preserving dict-like access
        if isinstance(self.parameter_payload, dict) and not isinstance(self.parameter_payload, MappingProxyType):
            # Use object.__setattr__ to modify frozen dataclass
            object.__setattr__(self, 'parameter_payload', MappingProxyType(self.parameter_payload))


@dataclass(frozen=True)
class AllocationSpec:
    """
    Immutable allocation specification.
    
    Defines bucket domain and adaptive mode (if applicable).
    """
    adaptive_mode: bool = False
    """Whether experiment uses adaptive allocation (requires version bump for changes)"""
    
    bucket_domain_size: int = 10_000
    """Size of bucket domain (default: 0-9999, size=10,000)"""
    
    multi_control_allowed: bool = False
    """Whether multiple control variants are explicitly allowed"""
    
    def __post_init__(self):
        """Validate allocation specification."""
        if self.bucket_domain_size < 100:
            raise ValueError(
                f"bucket_domain_size must be >= 100, got {self.bucket_domain_size}. "
                f"Minimum size required for accurate allocation."
            )
        
        if not isinstance(self.bucket_domain_size, int):
            raise ValueError(
                f"bucket_domain_size must be integer, got {type(self.bucket_domain_size)}"
            )


@dataclass(frozen=True)
class Variant:
    """
    Immutable variant definition.
    
    Each Variant includes all required fields:
    - variant_id (stable, collision-resistant)
    - experiment_id
    - version
    - human_readable_name
    - allocation_weight
    - parameter_payload
    - is_control
    - created_at_logical_timestamp
    - schema_version
    """
    variant_id: str
    """Stable, collision-resistant variant identifier"""
    
    experiment_id: str
    """Experiment identifier"""
    
    version: int
    """Experiment version"""
    
    human_readable_name: str
    """Human-readable variant name"""
    
    allocation_weight: int
    """Allocation weight (percentage)"""
    
    parameter_payload: Dict[str, Any]
    """Configuration blob for this variant (immutable MappingProxyType)"""
    
    is_control: bool
    """Whether this is a control variant"""
    
    created_at_logical_timestamp: int
    """Logical timestamp when variant was created"""
    
    schema_version: str = SchemaVersion.EXPERIMENT_V1.value
    """Schema version for compatibility"""


@dataclass(frozen=True)
class AllocationRange:
    """
    Immutable bucket range allocation (inclusive bounds).
    
    No implicit boundaries allowed.
    Must be contiguous, non-overlapping, and fully cover domain.
    """
    start_bucket: int
    """Start bucket (inclusive, 0-based)"""
    
    end_bucket: int
    """End bucket (inclusive)"""
    
    variant_id: str
    """Variant ID assigned to this range"""
    
    def __post_init__(self):
        """Validate allocation range."""
        if self.start_bucket < 0:
            raise ValueError(
                f"start_bucket cannot be negative: {self.start_bucket}. "
                f"Variant: {self.variant_id}"
            )
        
        if self.end_bucket < self.start_bucket:
            raise ValueError(
                f"end_bucket ({self.end_bucket}) must be >= start_bucket ({self.start_bucket}). "
                f"Variant: {self.variant_id}"
            )
        
        if not isinstance(self.start_bucket, int) or not isinstance(self.end_bucket, int):
            raise ValueError(
                f"Bucket boundaries must be integers. "
                f"Got start_bucket={type(self.start_bucket)}, end_bucket={type(self.end_bucket)}. "
                f"Variant: {self.variant_id}"
            )
    
    def contains(self, bucket: int) -> bool:
        """
        Check if bucket falls within this range (inclusive).
        
        Args:
            bucket: Bucket number to check
            
        Returns:
            True if bucket is in range [start_bucket, end_bucket]
        """
        return self.start_bucket <= bucket <= self.end_bucket
    
    def size(self) -> int:
        """
        Number of buckets in this range (inclusive).
        
        Returns:
            Number of buckets: end_bucket - start_bucket + 1
        """
        return self.end_bucket - self.start_bucket + 1
    
    def overlaps(self, other: "AllocationRange") -> bool:
        """
        Check if this range overlaps with another range.
        
        Args:
            other: Other allocation range to check
            
        Returns:
            True if ranges overlap
        """
        return not (self.end_bucket < other.start_bucket or other.end_bucket < self.start_bucket)


@dataclass(frozen=True)
class VariantAllocationSnapshot:
    """
    Immutable snapshot of complete variant allocation.
    
    VariantAllocationSnapshot must include:
    - experiment_id
    - experiment_version
    - bucket_domain_size
    - allocation_ranges (list of AllocationRange)
    - variant_metadata
    - allocation_hash
    - schema_version
    - experiment_schema_version
    - compatibility metadata (optional)
    
    Runtime must use allocation_ranges exactly as defined.
    Never recompute boundaries.
    Never infer allocation order.
    """
    experiment_id: str
    """Experiment identifier"""
    
    experiment_version: int
    """Experiment version"""
    
    bucket_domain_size: int
    """Size of bucket domain"""
    
    allocation_ranges: Tuple[AllocationRange, ...]
    """Ordered list of allocation ranges (immutable tuple)"""
    
    variant_metadata: Tuple[Variant, ...]
    """Ordered list of variants (immutable tuple)"""
    
    allocation_hash: str
    """Deterministic hash of allocation configuration"""
    
    schema_version: str = SchemaVersion.ALLOCATION_V1.value
    """Allocation schema version"""
    
    experiment_schema_version: str = SchemaVersion.EXPERIMENT_V1.value
    """Experiment schema version"""
    
    compatibility_metadata: Optional[Dict[str, Any]] = None
    """Compatibility metadata for version migration"""
    
    def get_variant_by_bucket(self, bucket: int) -> Optional[Variant]:
        """Retrieve variant for a given bucket"""
        if bucket < 0 or bucket >= self.bucket_domain_size:
            return None
        
        for alloc_range in self.allocation_ranges:
            if alloc_range.contains(bucket):
                # Find matching variant
                for variant in self.variant_metadata:
                    if variant.variant_id == alloc_range.variant_id:
                        return variant
        return None
    
    def get_variant_by_id(self, variant_id: str) -> Optional[Variant]:
        """Retrieve variant by ID"""
        for variant in self.variant_metadata:
            if variant.variant_id == variant_id:
                return variant
        return None


class VariantGenerationError(Exception):
    """Raised when variant generation fails validation"""
    pass


def _generate_variant_id(experiment_id: str, version: int, variant_name: str) -> str:
    """
    Generate deterministic, collision-resistant variant ID.
    
    Variant ID must be:
    - Deterministic (same inputs → same ID)
    - Collision-resistant
    - Stable across re-generation of same config
    
    Format: {experiment_id}:{version}:{variant_name_hash}
    
    Recommended format from spec:
    {experiment_id}:{version}:{variant_name_hash}
    
    Args:
        experiment_id: Experiment identifier
        version: Experiment version
        variant_name: Variant name
        
    Returns:
        Collision-resistant variant identifier
    """
    # Normalize variant name for deterministic hashing
    normalized_name = variant_name.strip().lower()
    
    # Use first 12 chars of SHA256 for collision resistance while keeping readable
    # SHA256 provides 2^128 collision resistance, 12 hex chars = 48 bits
    name_hash = hashlib.sha256(normalized_name.encode('utf-8')).hexdigest()[:12]
    
    # Format: experiment_id:version:hash
    variant_id = f"{experiment_id}:v{version}:{name_hash}"
    
    return variant_id


def _validate_allocation_sum(variant_specs: list[VariantSpec]) -> None:
    """Enforce allocation sum equals exactly 100%"""
    total = sum(spec.allocation_weight for spec in variant_specs)
    if total != 100:
        raise VariantGenerationError(
            f"Allocation sum must equal exactly 100%, got {total}. "
            f"Allocations: {[(s.name, s.allocation_weight) for s in variant_specs]}"
        )


def _validate_control_variants(
    variant_specs: List[VariantSpec],
    multi_control_allowed: bool = False,
) -> None:
    """
    Enforce control variant rules.
    
    Rules:
    - Exactly one control unless multi-control explicitly declared
    - Control must be explicitly marked
    - Control allocation must be declared (no implicit remainder allocation)
    
    Prohibited:
    - "Remainder allocation goes to control"
    - Dynamic control resizing
    
    Args:
        variant_specs: List of variant specifications
        multi_control_allowed: Whether multiple controls are explicitly allowed
        
    Raises:
        VariantGenerationError: If control rules violated
    """
    control_count = sum(1 for spec in variant_specs if spec.is_control)
    
    if control_count == 0:
        raise VariantGenerationError(
            "At least one control variant required. "
            "Mark one variant with is_control=True. "
            "No implicit default control allowed."
        )
    
    if control_count > 1 and not multi_control_allowed:
        control_names = [spec.name for spec in variant_specs if spec.is_control]
        raise VariantGenerationError(
            f"Multiple control variants found ({control_count}): {control_names}. "
            f"Set multi_control_allowed=True in AllocationSpec to allow multiple controls."
        )
    
    # Verify control allocation is explicit (not remainder)
    control_specs = [spec for spec in variant_specs if spec.is_control]
    for control_spec in control_specs:
        if control_spec.allocation_weight == 0:
            raise VariantGenerationError(
                f"Control variant '{control_spec.name}' has zero allocation. "
                f"Control allocation must be explicitly declared (no implicit remainder)."
            )


def _validate_no_duplicates(variant_specs: list[VariantSpec]) -> None:
    """Ensure no duplicate variant names (O(n) implementation)"""
    seen_names: set[str] = set()
    duplicates: set[str] = set()
    
    for spec in variant_specs:
        if spec.name in seen_names:
            duplicates.add(spec.name)
        else:
            seen_names.add(spec.name)
    
    if duplicates:
        raise VariantGenerationError(f"Duplicate variant names found: {duplicates}")


def _validate_variant_specs(
    variant_specs: List[VariantSpec],
    allocation_spec: AllocationSpec,
) -> None:
    """
    Run all validation checks on variant specifications.
    
    Validates:
    - Non-empty variant list
    - Allocation sum = 100%
    - No negative allocation
    - Control variant rules
    - No duplicate names
    - Integer-safe weights
    
    Args:
        variant_specs: List of variant specifications
        allocation_spec: Allocation specification
        
    Raises:
        VariantGenerationError: If validation fails
    """
    if not variant_specs:
        raise VariantGenerationError(
            "Empty variant list not allowed. "
            "At least one variant must be specified."
        )
    
    # Validate allocation sum
    _validate_allocation_sum(variant_specs)
    
    # Validate control variants
    _validate_control_variants(variant_specs, allocation_spec.multi_control_allowed)
    
    # Validate no duplicates
    _validate_no_duplicates(variant_specs)
    
    # Validate integer-safe weights
    for spec in variant_specs:
        if not isinstance(spec.allocation_weight, int):
            raise VariantGenerationError(
                f"Non-integer allocation weight for variant '{spec.name}': "
                f"{spec.allocation_weight}. Use integer percentages only."
            )


def _create_variants(
    experiment_id: str,
    experiment_version: int,
    variant_specs: List[VariantSpec],
    logical_timestamp: int,
) -> List[Variant]:
    """
    Create immutable Variant objects from specifications.
    
    Deterministically ordered by explicit order_index (if provided) or variant_id.
    Order must be stable across re-generation and config evolution.
    
    Args:
        experiment_id: Experiment identifier
        experiment_version: Experiment version
        variant_specs: List of variant specifications
        logical_timestamp: Logical timestamp for creation
        
    Returns:
        List of Variant objects (deterministically ordered)
    """
    # First, generate variant IDs for all specs to enable stable sorting
    # This ensures ordering is based on stable attributes, not mutable names
    spec_with_ids = []
    for spec in variant_specs:
        variant_id = _generate_variant_id(experiment_id, experiment_version, spec.name)
        spec_with_ids.append((variant_id, spec))
    
    # Sort by explicit order_index if provided, otherwise by variant_id
    # This ensures deterministic ordering that doesn't change with name mutations
    sorted_specs = sorted(
        spec_with_ids,
        key=lambda x: (
            x[1].order_index if x[1].order_index is not None else float('inf'),  # order_index first
            x[0]  # variant_id as stable tiebreaker
        )
    )
    
    # Check for duplicate variant IDs (shouldn't happen with hash, but defensive)
    generated_ids: set[str] = set()
    variants: List[Variant] = []
    
    for variant_id, spec in sorted_specs:
        # Defensive check for collisions (extremely unlikely with SHA256)
        if variant_id in generated_ids:
            raise VariantGenerationError(
                f"Variant ID collision detected: {variant_id}. "
                f"This should not happen with SHA256 hashing. "
                f"Variant name: {spec.name}"
            )
        generated_ids.add(variant_id)
        
        variant = Variant(
            variant_id=variant_id,
            experiment_id=experiment_id,
            version=experiment_version,
            human_readable_name=spec.name,
            allocation_weight=spec.allocation_weight,
            parameter_payload=spec.parameter_payload,  # Already immutable MappingProxyType
            is_control=spec.is_control,
            created_at_logical_timestamp=logical_timestamp,
            schema_version=SchemaVersion.EXPERIMENT_V1.value
        )
        variants.append(variant)
    
    return variants


def _compute_bucket_ranges(
    variants: List[Variant],
    bucket_domain_size: int,
) -> List[AllocationRange]:
    """
    Convert allocation weights to deterministic bucket ranges.
    
    Uses integer-only math to avoid floating-point drift.
    Ranges are contiguous, non-overlapping, and fully cover domain.
    
    Algorithm:
    1. Calculate buckets per variant using integer division
    2. Distribute remainder buckets deterministically across variants (by variant_id order)
    3. Validate no gaps, no overlaps, full coverage
    
    Args:
        variants: Ordered list of variants (already sorted deterministically)
        bucket_domain_size: Size of bucket domain
        
    Returns:
        List of allocation ranges (contiguous, non-overlapping, full coverage)
        
    Raises:
        VariantGenerationError: If bucket calculation fails
    """
    if not variants:
        raise VariantGenerationError("Cannot compute ranges for empty variant list")
    
    # Variants already sorted deterministically from _create_variants
    allocation_ranges: List[AllocationRange] = []
    current_bucket = 0
    
    # Calculate base buckets for each variant (integer division)
    base_buckets = []
    total_allocated = 0
    
    for variant in variants:
        # Calculate number of buckets for this variant
        # Use integer division to avoid floating point: (weight * domain) // 100
        buckets_for_variant = (variant.allocation_weight * bucket_domain_size) // 100
        
        # Validate bucket calculation
        if buckets_for_variant < 0:
            raise VariantGenerationError(
                f"Negative bucket count calculated for variant '{variant.human_readable_name}': "
                f"{buckets_for_variant}. Weight: {variant.allocation_weight}, "
                f"Domain: {bucket_domain_size}"
            )
        
        base_buckets.append(buckets_for_variant)
        total_allocated += buckets_for_variant
    
    # Calculate remainder buckets to distribute
    remainder = bucket_domain_size - total_allocated
    
    if remainder < 0:
        raise VariantGenerationError(
            f"Bucket calculation overflow: allocated {total_allocated} buckets "
            f"for domain size {bucket_domain_size}"
        )
    
    # Distribute remainder deterministically across variants (one bucket per variant in order)
    # This ensures symmetric distribution rather than always assigning to last variant
    for i, variant in enumerate(variants):
        # Base allocation
        buckets_for_variant = base_buckets[i]
        
        # Add one remainder bucket if available (distributed deterministically)
        if i < remainder:
            buckets_for_variant += 1
        
        # Validate bounds
        if end_bucket >= bucket_domain_size:
            raise VariantGenerationError(
                f"Bucket calculation overflow: end_bucket={end_bucket}, "
                f"domain_size={bucket_domain_size}. "
                f"Variant: {variant.human_readable_name}"
            )
        
        if end_bucket < current_bucket:
            raise VariantGenerationError(
                f"Invalid bucket range: start={current_bucket}, end={end_bucket}. "
                f"Variant: {variant.human_readable_name}"
            )
        
        allocation_range = AllocationRange(
            start_bucket=current_bucket,
            end_bucket=end_bucket,
            variant_id=variant.variant_id
        )
        allocation_ranges.append(allocation_range)
        
        current_bucket = end_bucket + 1
    
    # Validate full domain coverage
    if not allocation_ranges:
        raise VariantGenerationError("No allocation ranges generated")
    
    if allocation_ranges[-1].end_bucket != bucket_domain_size - 1:
        raise VariantGenerationError(
            f"Bucket ranges do not fully cover domain. "
            f"Last bucket: {allocation_ranges[-1].end_bucket}, "
            f"expected: {bucket_domain_size - 1}. "
            f"Domain size: {bucket_domain_size}"
        )
    
    # Validate no gaps (contiguity)
    for i in range(len(allocation_ranges) - 1):
        current_end = allocation_ranges[i].end_bucket
        next_start = allocation_ranges[i + 1].start_bucket
        
        if current_end + 1 != next_start:
            raise VariantGenerationError(
                f"Gap detected between ranges {i} and {i+1}: "
                f"Range {i} ends at {current_end}, Range {i+1} starts at {next_start}. "
                f"Expected contiguous ranges."
            )
    
    # Validate no overlaps
    for i in range(len(allocation_ranges)):
        for j in range(i + 1, len(allocation_ranges)):
            if allocation_ranges[i].overlaps(allocation_ranges[j]):
                raise VariantGenerationError(
                    f"Overlapping ranges detected: Range {i} ({allocation_ranges[i].start_bucket}-"
                    f"{allocation_ranges[i].end_bucket}) overlaps with Range {j} "
                    f"({allocation_ranges[j].start_bucket}-{allocation_ranges[j].end_bucket})"
                )
    
    # Validate starts at 0
    if allocation_ranges[0].start_bucket != 0:
        raise VariantGenerationError(
            f"First allocation range must start at 0, got {allocation_ranges[0].start_bucket}"
        )
    
    return allocation_ranges


def _compute_allocation_hash(
    experiment_id: str,
    experiment_version: int,
    variants: list[Variant],
    bucket_domain_size: int
) -> str:
    """
    Generate deterministic hash of allocation configuration
    
    Used to detect config drift and prevent mid-flight mutation.
    Any change to allocation (including parameter_payload) produces different hash.
    """
    # Build deterministic representation
    # CRITICAL: Include parameter_payload to detect payload drift
    hash_components = {
        'experiment_id': experiment_id,
        'version': experiment_version,
        'bucket_domain_size': bucket_domain_size,
        'variants': [
            {
                'variant_id': v.variant_id,
                'name': v.human_readable_name,
                'weight': v.allocation_weight,
                'is_control': v.is_control,
                'parameter_payload': dict(v.parameter_payload)  # Convert MappingProxyType to dict for JSON
            }
            for v in variants  # Already sorted
        ]
    }
    
    # Use JSON with sorted keys for determinism
    # Sort parameter_payload keys for deterministic hashing
    canonical_repr = json.dumps(hash_components, sort_keys=True, separators=(',', ':'))
    
    # SHA256 for collision resistance
    return hashlib.sha256(canonical_repr.encode('utf-8')).hexdigest()


def generate_variants(
    experiment_id: str,
    experiment_version: int,
    allocation_spec: AllocationSpec,
    variant_specs: List[VariantSpec],
    logical_timestamp: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
    previous_snapshot: Optional[VariantAllocationSnapshot] = None,
) -> VariantAllocationSnapshot:
    """
    Generate deterministic variant allocation snapshot
    
    This is the single authority for constructing experiment variants and their
    allocation structure. Given identical inputs, produces identical outputs.
    
    Args:
        experiment_id: Unique experiment identifier
        experiment_version: Version number (must increment for allocation changes)
        allocation_spec: Allocation configuration
        variant_specs: List of variant specifications
        logical_timestamp: Optional timestamp for created_at (defaults to 0 for determinism)
        logger: Optional logger instance
        previous_snapshot: Optional previous snapshot for version mutation enforcement
    
    Returns:
        VariantAllocationSnapshot: Immutable allocation snapshot
    
    Raises:
        VariantGenerationError: If validation fails
    
    Guarantees:
        - Allocation sum equals exactly 100%
        - Bucket ranges fully cover domain with no overlaps
        - Deterministic ordering and hashing
        - No floating-point boundary logic
        - Idempotent: same input → identical output
    """
    if logger is None:
        logger = logging.getLogger(__name__)
    
    # Default to 0 for determinism if not provided
    if logical_timestamp is None:
        logical_timestamp = 0
    
    # Validate inputs
    if not experiment_id or not isinstance(experiment_id, str):
        raise VariantGenerationError(
            f"experiment_id cannot be empty and must be string, got: {type(experiment_id)}"
        )
    
    if not isinstance(experiment_version, int) or experiment_version < 1:
        raise VariantGenerationError(
            f"experiment_version must be integer >= 1, got: {experiment_version} "
            f"(type: {type(experiment_version)})"
        )
    
    if not isinstance(allocation_spec, AllocationSpec):
        raise VariantGenerationError(
            f"allocation_spec must be AllocationSpec, got: {type(allocation_spec)}"
        )
    
    if not isinstance(variant_specs, list) or len(variant_specs) == 0:
        raise VariantGenerationError(
            f"variant_specs must be non-empty list, got: {type(variant_specs)}"
        )
    
    # Validate variant specifications
    _validate_variant_specs(variant_specs, allocation_spec)
    
    # Enforce version mutation discipline: if previous snapshot provided, verify version increment
    if previous_snapshot is not None:
        if previous_snapshot.experiment_id != experiment_id:
            raise VariantGenerationError(
                f"Previous snapshot experiment_id mismatch: "
                f"expected {experiment_id}, got {previous_snapshot.experiment_id}"
            )
        
        if experiment_version <= previous_snapshot.experiment_version:
            raise VariantGenerationError(
                f"Version must increment for allocation changes. "
                f"Previous version: {previous_snapshot.experiment_version}, "
                f"new version: {experiment_version}. "
                f"Allocation changes require version bump."
            )
    
    logger.debug(
        f"Generating variants for experiment {experiment_id} v{experiment_version}: "
        f"{len(variant_specs)} variants, domain_size={allocation_spec.bucket_domain_size}"
    )
    
    # Create variants (deterministically ordered)
    variants = _create_variants(
        experiment_id=experiment_id,
        experiment_version=experiment_version,
        variant_specs=variant_specs,
        logical_timestamp=logical_timestamp
    )
    
    # Compute bucket ranges (integer-only math)
    allocation_ranges = _compute_bucket_ranges(
        variants=variants,
        bucket_domain_size=allocation_spec.bucket_domain_size
    )
    
    # Compute allocation hash
    allocation_hash = _compute_allocation_hash(
        experiment_id=experiment_id,
        experiment_version=experiment_version,
        variants=variants,
        bucket_domain_size=allocation_spec.bucket_domain_size
    )
    
    # Create compatibility metadata
    compatibility_metadata = {
        "generated_at_logical_timestamp": logical_timestamp,
        "variant_count": len(variants),
        "control_count": sum(1 for v in variants if v.is_control),
        "adaptive_mode": allocation_spec.adaptive_mode,
        "multi_control_allowed": allocation_spec.multi_control_allowed,
    }
    
    # Create immutable snapshot
    snapshot = VariantAllocationSnapshot(
        experiment_id=experiment_id,
        experiment_version=experiment_version,
        bucket_domain_size=allocation_spec.bucket_domain_size,
        allocation_ranges=tuple(allocation_ranges),  # Immutable tuple
        variant_metadata=tuple(variants),  # Immutable tuple
        allocation_hash=allocation_hash,
        schema_version=SchemaVersion.ALLOCATION_V1.value,
        experiment_schema_version=SchemaVersion.EXPERIMENT_V1.value,
        compatibility_metadata=compatibility_metadata,
    )
    
    # Log successful generation
    logger.info(
        f"Generated variant allocation snapshot for {experiment_id} v{experiment_version}: "
        f"{len(variants)} variants, {len(allocation_ranges)} ranges, "
        f"hash={allocation_hash[:16]}..."
    )
    
    return snapshot


def verify_snapshot_integrity(snapshot: VariantAllocationSnapshot) -> Tuple[bool, List[str]]:
    """
    Verify snapshot integrity comprehensively.
    
    Checks:
    - Allocation hash matches
    - Bucket ranges fully cover domain
    - No gaps or overlaps
    - Variant IDs match between ranges and metadata
    - Domain boundaries correct (starts at 0, ends at domain_size-1)
    
    Used to detect corruption or tampering.
    
    Args:
        snapshot: Snapshot to verify
        
    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors: List[str] = []
    
    # Recompute and verify allocation hash
    recomputed_hash = _compute_allocation_hash(
        experiment_id=snapshot.experiment_id,
        experiment_version=snapshot.experiment_version,
        variants=list(snapshot.variant_metadata),
        bucket_domain_size=snapshot.bucket_domain_size
    )
    
    if recomputed_hash != snapshot.allocation_hash:
        errors.append(
            f"Allocation hash mismatch: expected {snapshot.allocation_hash}, "
            f"got {recomputed_hash}"
        )
    
    # Verify bucket ranges fully cover domain
    if not snapshot.allocation_ranges:
        errors.append("No allocation ranges in snapshot")
    else:
        # Check starts at 0
        if snapshot.allocation_ranges[0].start_bucket != 0:
            errors.append(
                f"First range must start at 0, got {snapshot.allocation_ranges[0].start_bucket}"
            )
        
        # Check ends at domain_size - 1
        if snapshot.allocation_ranges[-1].end_bucket != snapshot.bucket_domain_size - 1:
            errors.append(
                f"Last range must end at {snapshot.bucket_domain_size - 1}, "
                f"got {snapshot.allocation_ranges[-1].end_bucket}"
            )
        
        # Check no gaps
        for i in range(len(snapshot.allocation_ranges) - 1):
            current_end = snapshot.allocation_ranges[i].end_bucket
            next_start = snapshot.allocation_ranges[i + 1].start_bucket
            if current_end + 1 != next_start:
                errors.append(
                    f"Gap between ranges {i} and {i+1}: {current_end} -> {next_start}"
                )
        
        # Check no overlaps
        for i in range(len(snapshot.allocation_ranges)):
            for j in range(i + 1, len(snapshot.allocation_ranges)):
                if snapshot.allocation_ranges[i].overlaps(snapshot.allocation_ranges[j]):
                    errors.append(
                        f"Overlap between ranges {i} and {j}"
                    )
    
    # Verify variant IDs match
    range_variant_ids = {r.variant_id for r in snapshot.allocation_ranges}
    metadata_variant_ids = {v.variant_id for v in snapshot.variant_metadata}
    
    if range_variant_ids != metadata_variant_ids:
        missing_in_metadata = range_variant_ids - metadata_variant_ids
        missing_in_ranges = metadata_variant_ids - range_variant_ids
        
        if missing_in_metadata:
            errors.append(
                f"Variant IDs in ranges but not in metadata: {missing_in_metadata}"
            )
        if missing_in_ranges:
            errors.append(
                f"Variant IDs in metadata but not in ranges: {missing_in_ranges}"
            )
    
    return len(errors) == 0, errors


# Export public API
__all__ = [
    # Data structures
    'VariantSpec',
    'AllocationSpec',
    'Variant',
    'AllocationRange',
    'VariantAllocationSnapshot',
    
    # Errors
    'VariantGenerationError',
    
    # Functions
    'generate_variants',
    'verify_snapshot_integrity',
    
    # Enums
    'SchemaVersion',
]