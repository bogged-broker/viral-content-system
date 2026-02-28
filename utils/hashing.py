"""
/utils/hashing.py

Canonical, deterministic hashing primitives for system-wide content identity.

This module provides cryptographic hashing with guaranteed byte-stability across:
- Machines
- Operating systems
- Python versions
- Time
- Replay executions

NO randomness. NO environment dependencies. NO silent fallbacks.
Equal meaning → equal hash. Always.
"""

import hashlib
import json
from typing import Any, Dict, List, Set, Tuple, Union

from utils.errors import HashingError, InvalidInputError
from utils.serialization import to_canonical_bytes


# Supported hash algorithm
_HASH_ALGORITHM = "sha256"

# Supported types for structural hashing
_CANONICAL_TYPES = (dict, list, tuple, set, str, int, bool, type(None))


def hash_bytes(data: bytes) -> str:
    """
    Lowest-level hashing primitive.
    
    Computes cryptographic hash of raw bytes with zero interpretation.
    
    Args:
        data: Raw bytes to hash
        
    Returns:
        Hex-encoded digest string
        
    Raises:
        InvalidInputError: If data is not bytes
    """
    if not isinstance(data, bytes):
        raise InvalidInputError(
            f"hash_bytes requires bytes input, got {type(data).__name__}"
        )
    
    if len(data) == 0:
        raise InvalidInputError("Cannot hash empty bytes")
    
    hasher = hashlib.new(_HASH_ALGORITHM)
    hasher.update(data)
    return hasher.hexdigest()


def hash_struct(
    obj: Any,
    *,
    version: str,
    order_sensitive: bool = True,
) -> str:
    """
    Canonical structural hash for deterministic content identity.
    
    Produces stable hashes for structured data with explicit versioning
    and ordering semantics. Used for:
    - Computation identity
    - Aggregation keys
    - Replay equivalence
    - Audit fingerprints
    
    Args:
        obj: Structure to hash (dict, list, primitives)
        version: Hash version identifier (required, non-empty)
        order_sensitive: If False, forces canonical ordering on lists/tuples
        
    Returns:
        Hex-encoded digest including version envelope
        
    Raises:
        HashingError: On unsupported types or non-deterministic inputs
        InvalidInputError: On invalid version or obj
    """
    if not version or not isinstance(version, str):
        raise InvalidInputError(
            f"version must be non-empty string, got {version!r}"
        )
    
    if not version.strip():
        raise InvalidInputError("version cannot be whitespace-only")
    
    # Validate and canonicalize structure
    canonical = _canonicalize(obj, order_sensitive=order_sensitive)
    
    # Serialize to deterministic bytes
    try:
        canonical_bytes = to_canonical_bytes(canonical)
    except Exception as e:
        raise HashingError(
            f"Failed to serialize structure to bytes: {e}"
        ) from e
    
    # Build versioned envelope
    version_prefix = f"hash_version:{version}||".encode("utf-8")
    envelope = version_prefix + canonical_bytes
    
    # Hash the envelope
    hasher = hashlib.new(_HASH_ALGORITHM)
    hasher.update(envelope)
    return hasher.hexdigest()


def assert_hash_stable(
    obj: Any,
    expected_hash: str,
    *,
    version: str,
) -> None:
    """
    Assert that obj produces expected hash.
    
    Used in tests, invariants, and replay verification to detect
    breaking changes in hashing semantics.
    
    Args:
        obj: Structure to verify
        expected_hash: Expected hex digest
        version: Hash version to use
        
    Raises:
        HashingError: If computed hash does not match expected
        InvalidInputError: On invalid inputs
    """
    if not isinstance(expected_hash, str):
        raise InvalidInputError(
            f"expected_hash must be string, got {type(expected_hash).__name__}"
        )
    
    if not expected_hash.strip():
        raise InvalidInputError("expected_hash cannot be empty")
    
    computed = hash_struct(obj, version=version)
    
    if computed != expected_hash:
        raise HashingError(
            f"Hash mismatch for version '{version}':\n"
            f"  Expected: {expected_hash}\n"
            f"  Computed: {computed}\n"
            f"  Object: {obj!r}"
        )


def _canonicalize(obj: Any, *, order_sensitive: bool) -> Any:
    """
    Convert obj to canonical form for deterministic hashing.
    
    Applies strict normalization rules:
    - dict → sorted by key (UTF-8)
    - set → sorted list
    - list/tuple → preserved order (or sorted if order_sensitive=False)
    - primitives → validated and preserved
    
    Args:
        obj: Object to canonicalize
        order_sensitive: Whether to preserve order in sequences
        
    Returns:
        Canonicalized structure (nested dicts/lists/primitives)
        
    Raises:
        HashingError: On unsupported or non-deterministic types
    """
    # Handle None
    if obj is None:
        return None
    
    # Handle bool (must check before int, as bool is subclass of int)
    if isinstance(obj, bool):
        return obj
    
    # Handle int
    if isinstance(obj, int):
        return obj
    
    # Handle str
    if isinstance(obj, str):
        return obj
    
    # Handle float - REJECTED
    if isinstance(obj, float):
        raise HashingError(
            f"Float hashing is forbidden (non-deterministic): {obj!r}. "
            "Convert to int, Decimal, or string explicitly."
        )
    
    # Handle dict
    if isinstance(obj, dict):
        if not obj:
            raise HashingError("Cannot hash empty dict")
        
        try:
            sorted_items = sorted(obj.items(), key=lambda x: x[0])
        except TypeError as e:
            raise HashingError(
                f"Dict keys must be sortable (comparable): {e}"
            ) from e
        
        return {
            k: _canonicalize(v, order_sensitive=order_sensitive)
            for k, v in sorted_items
        }
    
    # Handle set
    if isinstance(obj, set):
        if not obj:
            raise HashingError("Cannot hash empty set")
        
        try:
            sorted_list = sorted(obj)
        except TypeError as e:
            raise HashingError(
                f"Set elements must be sortable: {e}"
            ) from e
        
        return [
            _canonicalize(item, order_sensitive=order_sensitive)
            for item in sorted_list
        ]
    
    # Handle list
    if isinstance(obj, list):
        if not obj:
            raise HashingError("Cannot hash empty list")
        
        items = obj if order_sensitive else sorted(obj)
        
        try:
            return [
                _canonicalize(item, order_sensitive=order_sensitive)
                for item in items
            ]
        except TypeError as e:
            if not order_sensitive:
                raise HashingError(
                    f"List elements must be sortable when order_sensitive=False: {e}"
                ) from e
            raise
    
    # Handle tuple
    if isinstance(obj, tuple):
        if not obj:
            raise HashingError("Cannot hash empty tuple")
        
        items = obj if order_sensitive else sorted(obj)
        
        try:
            # Convert to list for canonical form (tuples hash same as lists)
            return [
                _canonicalize(item, order_sensitive=order_sensitive)
                for item in items
            ]
        except TypeError as e:
            if not order_sensitive:
                raise HashingError(
                    f"Tuple elements must be sortable when order_sensitive=False: {e}"
                ) from e
            raise
    
    # Unsupported type
    raise HashingError(
        f"Unsupported type for hashing: {type(obj).__name__}. "
        f"Allowed types: {', '.join(t.__name__ for t in _CANONICAL_TYPES)}"
    )


def _validate_finite_number(value: Union[int, float]) -> None:
    """
    Validate that numeric value is finite.
    
    Args:
        value: Number to validate
        
    Raises:
        HashingError: If value is inf or nan
    """
    if isinstance(value, float):
        if not (-float('inf') < value < float('inf')):
            raise HashingError(
                f"Non-finite numbers cannot be hashed: {value}"
            )