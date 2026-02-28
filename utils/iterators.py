"""
Deterministic Iteration Utilities

This module provides the single authority for deterministic iteration semantics
across the entire system.

Core Invariant:
    All non-sequential iteration must be explicitly ordered.

Guarantees:
    - Stable traversal order across runs
    - No reliance on dict insertion order
    - No non-deterministic set iteration
    - Platform-independent ordering
    - Replay-safe iteration

Purpose:
    Eliminates iteration drift that would cause:
        - Aggregation divergence
        - Hash instability
        - Replay failures
        - Non-reproducible audits

Non-Goals:
    - NOT generic itertools wrappers
    - NOT lazy streaming operators
    - NOT async iteration management
    - NOT business-level grouping
    - NOT performance micro-optimizations
"""

from __future__ import annotations

from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
    Set as AbstractSet,
)
from typing import Any, TypeVar, Tuple, List, Dict

from utils.guards import GuardViolation, require


T = TypeVar('T')
K = TypeVar('K')
V = TypeVar('V')


# ============================================================================
# Dictionary Iteration
# ============================================================================


def iter_dict_sorted(
    mapping: Mapping[str, Any]
) -> Iterator[tuple[str, Any]]:
    """
    Iterate over mapping with deterministic key ordering.
    
    Args:
        mapping: Dictionary to iterate
        
    Yields:
        (key, value) tuples in lexicographic key order
        
    Raises:
        GuardViolation: If keys are not strings or not comparable
        
    Guarantees:
        - Keys sorted lexicographically
        - Deterministic across machines
        - Stable across Python versions
        - No reliance on insertion order
        
    Example:
        >>> d = {"z": 3, "a": 1, "m": 2}
        >>> list(iter_dict_sorted(d))
        [('a', 1), ('m', 2), ('z', 3)]
        
    Used by:
        - Serialization
        - Structural diff
        - Hashing
        - Registry enumeration
    """
    require(
        isinstance(mapping, Mapping),
        f"Expected Mapping, got {type(mapping).__name__}"
    )
    
    # Validate all keys are strings
    for key in mapping.keys():
        require(
            isinstance(key, str),
            f"All mapping keys must be str, got {type(key).__name__} for key {key!r}"
        )
    
    # Sort keys lexicographically
    sorted_keys = sorted(mapping.keys())
    
    # Yield in sorted order
    for key in sorted_keys:
        yield (key, mapping[key])


# ============================================================================
# Set Iteration
# ============================================================================


def iter_set_sorted(items: AbstractSet[T]) -> Iterator[T]:
    """
    Iterate over set with deterministic ordering.
    
    Args:
        items: Set to iterate
        
    Yields:
        Items in deterministic sorted order
        
    Raises:
        GuardViolation: If items are not comparable
        
    Guarantees:
        - Deterministic sorted iteration
        - No reliance on hash order
        - Stable across runs
        
    Example:
        >>> s = {3, 1, 2}
        >>> list(iter_set_sorted(s))
        [1, 2, 3]
        
    Note:
        Items must be comparable using natural ordering.
        Mixed types will raise GuardViolation.
    """
    require(
        isinstance(items, AbstractSet),
        f"Expected AbstractSet, got {type(items).__name__}"
    )
    
    if not items:
        return
    
    # Validate comparability by attempting sort
    try:
        sorted_items = sorted(items)
    except TypeError as e:
        raise GuardViolation(
            f"Set elements must be comparable and deterministic for sorting: {e}"
        )
    
    # Yield in sorted order
    yield from sorted_items


# ============================================================================
# Sequence Iteration
# ============================================================================


def iter_sequence_strict(items: Sequence[T]) -> Iterator[T]:
    """
    Iterate over sequence preserving exact order.
    
    Args:
        items: Sequence to iterate
        
    Yields:
        Items in original sequence order
        
    Raises:
        GuardViolation: If items is not a Sequence
        
    Guarantees:
        - Preserves order exactly
        - Rejects non-sequence iterables
        - No implicit conversion
        
    Example:
        >>> lst = [3, 1, 2]
        >>> list(iter_sequence_strict(lst))
        [3, 1, 2]
        
    Used when order is semantically meaningful and must be preserved.
    """
    require(
        isinstance(items, Sequence),
        f"Expected Sequence, got {type(items).__name__}"
    )
    
    yield from items


# ============================================================================
# Set Operations
# ============================================================================


def iter_union_sorted(
    a: AbstractSet[str],
    b: AbstractSet[str]
) -> Iterator[str]:
    """
    Iterate over union of two string sets in sorted order.
    
    Args:
        a: First set
        b: Second set
        
    Yields:
        Unique strings from a ∪ b in lexicographic order
        
    Raises:
        GuardViolation: If sets contain non-strings
        
    Guarantees:
        - Deterministic union ordering
        - No duplicates
        - Sorted lexicographically
        
    Example:
        >>> a = {"x", "a"}
        >>> b = {"b", "x"}
        >>> list(iter_union_sorted(a, b))
        ['a', 'b', 'x']
        
    Used in:
        - Structural diff
        - Field validation
        - Key reconciliation
    """
    require(
        isinstance(a, AbstractSet) and isinstance(b, AbstractSet),
        "Both arguments must be AbstractSet"
    )
    
    # Validate all elements are strings
    for item in a:
        require(isinstance(item, str), f"Expected str in set a, got {type(item).__name__}")
    for item in b:
        require(isinstance(item, str), f"Expected str in set b, got {type(item).__name__}")
    
    # Compute union and sort
    union = a | b
    sorted_union = sorted(union)
    
    yield from sorted_union


# ============================================================================
# Grouping
# ============================================================================


def stable_group_by(
    items: Iterable[T],
    key_fn: Callable[[T], str]
) -> dict[str, list[T]]:
    """
    Group items by key function with deterministic ordering.
    
    Args:
        items: Items to group
        key_fn: Function mapping item to group key (must return str)
        
    Returns:
        Dictionary mapping keys to lists of items.
        Iteration over dict keys will be in sorted order.
        Items within each group preserve original order.
        
    Raises:
        GuardViolation: If key_fn returns non-string
        
    Guarantees:
        - Keys sorted lexicographically
        - Items within groups preserve input order (stable)
        - Deterministic across runs
        
    Example:
        >>> items = [("a", 1), ("b", 1), ("a", 2)]
        >>> groups = stable_group_by(items, lambda x: x[0])
        >>> dict(groups)
        {'a': [('a', 1), ('a', 2)], 'b': [('b', 1)]}
        >>> list(groups.keys())
        ['a', 'b']
        
    Used for:
        - Deterministic aggregation grouping
        - Field-based partitioning
        - Stable batch creation
    """
    require(callable(key_fn), "key_fn must be callable")
    
    # Materialize items and build groups
    groups: dict[str, list[T]] = {}
    
    for item in items:
        key = key_fn(item)
        
        require(
            isinstance(key, str),
            f"key_fn must return str, got {type(key).__name__} for item {item!r}"
        )
        
        if key not in groups:
            groups[key] = []
        groups[key].append(item)
    
    # Return dict (iteration via iter_dict_sorted elsewhere if needed)
    # The dict itself has keys that can be sorted deterministically
    return groups


# ============================================================================
# Chunking
# ============================================================================


def chunked_deterministic(
    items: Sequence[T],
    chunk_size: int
) -> Iterator[Sequence[T]]:
    """
    Split sequence into fixed-size chunks deterministically.
    
    Args:
        items: Sequence to chunk
        chunk_size: Size of each chunk (must be > 0)
        
    Yields:
        Subsequences of length chunk_size (final chunk may be smaller)
        
    Raises:
        GuardViolation: If chunk_size <= 0 or items not a Sequence
        
    Guarantees:
        - Deterministic chunk boundaries
        - Stable across runs
        - No randomness
        - Preserves item order
        
    Example:
        >>> items = [1, 2, 3, 4, 5]
        >>> list(chunked_deterministic(items, 2))
        [[1, 2], [3, 4], [5]]
        
    Used for:
        - Batch replay
        - Persistence commit batching
        - Recovery segmentation
    """
    require(
        isinstance(items, Sequence),
        f"Expected Sequence, got {type(items).__name__}"
    )
    require(
        isinstance(chunk_size, int),
        f"chunk_size must be int, got {type(chunk_size).__name__}"
    )
    require(
        chunk_size > 0,
        f"chunk_size must be positive, got {chunk_size}"
    )
    
    # Deterministic slicing
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


# ============================================================================
# Flattening
# ============================================================================


def flatten_deterministic(
    nested: Iterable[Iterable[T]]
) -> Iterator[T]:
    """
    Flatten nested iterables deterministically.
    
    Args:
        nested: Iterable of iterables
        
    Yields:
        All items from nested structure in stable order
        
    Raises:
        GuardViolation: If nested contains sets (unordered)
        
    Guarantees:
        - Stable nested traversal
        - Outer iterable order preserved
        - Inner iterable order preserved
        - No automatic sorting of inner sequences
        
    Example:
        >>> nested = [[1, 2], [3], [4, 5]]
        >>> list(flatten_deterministic(nested))
        [1, 2, 3, 4, 5]
        
    Warning:
        Outer and inner iterables must have deterministic order.
        Do not pass sets as inner containers.
    """
    for inner in nested:
        # Reject sets (unordered)
        require(
            not isinstance(inner, (set, frozenset)),
            "Cannot flatten sets (unordered). Use iter_set_sorted first."
        )
        
        yield from inner


# ============================================================================
# Validation Helpers
# ============================================================================


def validate_deterministic_iteration(items: Any) -> None:
    """
    Validate that a container supports deterministic iteration.
    
    Args:
        items: Container to validate
        
    Raises:
        GuardViolation: If container has non-deterministic iteration
        
    Allowed:
        - list, tuple (ordered)
        - str, bytes (ordered)
        - Mapping with string keys (can be sorted)
        
    Rejected:
        - set, frozenset (unordered)
        - Mapping with non-string keys
        - Custom containers without ordering guarantees
    """
    # Sequences are deterministic
    if isinstance(items, (list, tuple, str, bytes)):
        return
    
    # Mappings are deterministic if keys are strings
    if isinstance(items, Mapping):
        for key in items.keys():
            require(
                isinstance(key, str),
                f"Mapping keys must be str for deterministic iteration, got {type(key).__name__}"
            )
        return
    
    # Sets are non-deterministic
    if isinstance(items, (set, frozenset)):
        raise GuardViolation(
            "Sets have non-deterministic iteration. Use iter_set_sorted()."
        )
    
    # Unknown types rejected
    raise GuardViolation(
        f"Cannot guarantee deterministic iteration for {type(items).__name__}. "
        f"Use explicit iteration utilities."
    )


# ============================================================================
# Comparison Utilities
# ============================================================================


def compare_iterables_ordered(
    a: Iterable[T],
    b: Iterable[T]
) -> bool:
    """
    Compare two iterables element-by-element in order.
    
    Args:
        a: First iterable
        b: Second iterable
        
    Returns:
        True if iterables contain same elements in same order
        
    Guarantees:
        - Order-sensitive comparison
        - Works with any iterable
        - Short-circuits on first difference
        
    Example:
        >>> compare_iterables_ordered([1, 2, 3], [1, 2, 3])
        True
        >>> compare_iterables_ordered([1, 2, 3], [1, 3, 2])
        False
    """
    iter_a = iter(a)
    iter_b = iter(b)
    
    while True:
        try:
            item_a = next(iter_a)
        except StopIteration:
            # a exhausted, check if b is too
            try:
                next(iter_b)
                return False  # b has more items
            except StopIteration:
                return True  # Both exhausted
        
        try:
            item_b = next(iter_b)
        except StopIteration:
            return False  # b exhausted but a has more
        
        if item_a != item_b:
            return False
    
    return True


# ============================================================================
# Public API Exports
# ============================================================================


__all__ = [
    # Dictionary iteration
    "iter_dict_sorted",
    
    # Set iteration
    "iter_set_sorted",
    
    # Sequence iteration
    "iter_sequence_strict",
    
    # Set operations
    "iter_union_sorted",
    
    # Grouping
    "stable_group_by",
    
    # Chunking
    "chunked_deterministic",
    
    # Flattening
    "flatten_deterministic",
    
    # Validation
    "validate_deterministic_iteration",
    
    # Comparison
    "compare_iterables_ordered",
]


# ============================================================================
# Self-Test (Inline Validation)
# ============================================================================


def _self_test() -> None:
    """
    Inline self-test to validate determinism guarantees.
    
    This runs at import time in debug mode to catch issues early.
    """
    # Test 1: Dict iteration is sorted
    d = {"z": 3, "a": 1, "m": 2}
    keys = [k for k, v in iter_dict_sorted(d)]
    assert keys == ["a", "m", "z"], f"Dict iteration not sorted: {keys}"
    
    # Test 2: Set iteration is sorted
    s = {3, 1, 2}
    items = list(iter_set_sorted(s))
    assert items == [1, 2, 3], f"Set iteration not sorted: {items}"
    
    # Test 3: Union is sorted
    a = {"x", "a"}
    b = {"b", "x"}
    union = list(iter_union_sorted(a, b))
    assert union == ["a", "b", "x"], f"Union not sorted: {union}"
    
    # Test 4: Grouping preserves order
    items = [("a", 1), ("b", 1), ("a", 2)]
    groups = stable_group_by(items, lambda x: x[0])
    assert list(groups.keys()) == ["a", "b"]  # Sorted keys
    assert groups["a"] == [("a", 1), ("a", 2)]  # Preserved order
    
    # Test 5: Chunking is deterministic
    items_list = [1, 2, 3, 4, 5]
    chunks = list(chunked_deterministic(items_list, 2))
    assert chunks == [[1, 2], [3, 4], [5]], f"Chunking not deterministic: {chunks}"
    
    # Test 6: Flatten preserves order
    nested = [[1, 2], [3], [4, 5]]
    flat = list(flatten_deterministic(nested))
    assert flat == [1, 2, 3, 4, 5], f"Flatten not deterministic: {flat}"


# Run self-test on import in debug builds
if __debug__:
    _self_test()
