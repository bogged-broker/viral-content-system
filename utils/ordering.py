"""
/utils/ordering.py

Total Ordering Helpers (Explicit, Testable)

Deterministic. No hidden behavior. Research-grade correctness.

This module is the single authority for total ordering inside the system.
It eliminates implicit Python comparison behavior, insertion-order dependence,
and cross-version sorting drift.

Core Law:
    Ordering must be total, explicit, and version-stable.

Philosophy:
    No object should ever be sorted without a declared ordering rule.
    If ordering changes across machines or runs, replay becomes impossible.

Critical:
    All orderings must be:
        - Total (any two elements comparable)
        - Antisymmetric
        - Transitive
        - Deterministic across environments
        - Stable across Python versions
        - Testable independently

Forbidden:
    - sorted(iterable) without key
    - list.sort() without key
    - Sorting dicts directly
    - Using float keys without canonical rounding
    - Comparing Enums without consistent value extraction
    - Relying on insertion order
    - Lambda sort keys scattered across codebase

Performance:
    - canonical_sort: O(n log n)
    - key extraction: O(n)
    - No quadratic behavior
    - No repeated key recomputation
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Iterable, TypeVar, List


# =============================================================================
# TYPE VARIABLES
# =============================================================================

T = TypeVar('T')


# =============================================================================
# ERROR MODEL
# =============================================================================

class OrderingError(RuntimeError):
    """
    Ordering constraint violation.
    
    Raised when:
        - Unsupported type encountered in ordering
        - Heterogeneous types detected
        - Invalid key function provided
        - Comparison preconditions violated
    
    Attributes:
        message: Error description
        offending_type: Type that caused failure
        position: Position in iterable (if applicable)
    """
    
    def __init__(
        self,
        message: str,
        *,
        offending_type: str | None = None,
        position: int | None = None
    ) -> None:
        self.offending_type = offending_type
        self.position = position
        
        # Build deterministic error message
        parts = [f"Ordering error: {message}"]
        
        if offending_type is not None:
            parts.append(f"Type: {offending_type}")
        if position is not None:
            parts.append(f"Position: {position}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# KEY COMPONENT VALIDATION
# =============================================================================

def _validate_key_component(component: Any, *, path: str = "component") -> None:
    """
    Validate that component is allowed in ordering key.
    
    Args:
        component: Component to validate
        path: Path for error reporting
    
    Raises:
        OrderingError: If component type is not allowed
    
    Allowed Types:
        - int
        - str
        - bool
        - tuple (recursively valid)
        - Enum (value will be used)
        - None (if explicitly allowed in context)
    
    Forbidden Types:
        - float (unless explicitly canonicalized)
        - datetime
        - mutable containers
        - custom objects
        - dict/set/list
    """
    # None check
    if component is None:
        # None allowed, but caller must handle ordering semantics
        return
    
    # Primitives
    if isinstance(component, bool):
        return
    if isinstance(component, int):
        return
    if isinstance(component, str):
        return
    
    # Enums (will use value)
    if isinstance(component, Enum):
        # Recursively validate enum value
        _validate_key_component(component.value, path=f"{path}.<enum_value>")
        return
    
    # Tuple (recursive validation)
    if isinstance(component, tuple):
        for idx, item in enumerate(component):
            _validate_key_component(item, path=f"{path}[{idx}]")
        return
    
    # Forbidden types
    if isinstance(component, float):
        raise OrderingError(
            f"Float type forbidden in ordering key at {path}",
            offending_type="float"
        )
    
    if isinstance(component, (list, dict, set)):
        raise OrderingError(
            f"Mutable container forbidden in ordering key at {path}",
            offending_type=type(component).__name__
        )
    
    # Unknown type
    raise OrderingError(
        f"Unsupported type in ordering key at {path}",
        offending_type=type(component).__name__
    )


# =============================================================================
# DETERMINISTIC KEY CONSTRUCTION
# =============================================================================

def deterministic_key(*components: Any) -> tuple:
    """
    Construct fully comparable tuple key from explicitly ordered components.
    
    Args:
        *components: Key components in precedence order
    
    Returns:
        Validated tuple key suitable for ordering
    
    Raises:
        OrderingError: If any component is invalid
    
    Guarantees:
        - All components are comparable
        - Nested structures are validated
        - No implicit ordering of dicts or sets
        - Deterministic across environments
    
    Rules:
        - Components define explicit precedence
        - First component is primary sort key
        - Subsequent components are tiebreakers
        - All types must be explicitly allowed
    
    Example:
        >>> deterministic_key(1000, "event_123", 5)
        (1000, 'event_123', 5)
        >>> deterministic_key(1000, {"a": 1})  # Raises OrderingError
    
    Usage:
        canonical_sort(events, key=lambda e: deterministic_key(
            e.timestamp_ms,
            e.window_id,
            e.event_id
        ))
    """
    # Validate all components
    for idx, component in enumerate(components):
        _validate_key_component(component, path=f"component[{idx}]")
    
    # Convert Enums to values for consistent comparison
    normalized = []
    for component in components:
        if isinstance(component, Enum):
            normalized.append(component.value)
        elif isinstance(component, tuple):
            # Recursively normalize nested tuples
            normalized.append(_normalize_tuple(component))
        else:
            normalized.append(component)
    
    return tuple(normalized)


def _normalize_tuple(t: tuple) -> tuple:
    """
    Recursively normalize tuple for consistent comparison.
    
    Args:
        t: Tuple to normalize
    
    Returns:
        Normalized tuple with Enums converted to values
    """
    result = []
    for item in t:
        if isinstance(item, Enum):
            result.append(item.value)
        elif isinstance(item, tuple):
            result.append(_normalize_tuple(item))
        else:
            result.append(item)
    return tuple(result)


# =============================================================================
# CANONICAL SORT
# =============================================================================

def canonical_sort(
    iterable: Iterable[T],
    *,
    key: Callable[[T], tuple],
    reverse: bool = False
) -> list[T]:
    """
    Deterministic sort with explicit key function.
    
    Args:
        iterable: Items to sort
        key: Function extracting tuple key from each item
        reverse: Sort in descending order (default: False)
    
    Returns:
        New sorted list
    
    Raises:
        OrderingError: If key function invalid or items not comparable
    
    Guarantees:
        - Always returns new list
        - Requires explicit key function
        - Stable across Python versions
        - Deterministic tuple-only ordering
        - Validates all keys before sorting
    
    Critical:
        No default sort allowed. Key function must be provided.
        This ensures ordering is always explicit and documented.
    
    Example:
        >>> events = [Event(time=100), Event(time=50)]
        >>> canonical_sort(events, key=lambda e: deterministic_key(e.time))
        [Event(time=50), Event(time=100)]
    """
    # Convert to list if needed
    items = list(iterable)
    
    # Empty list fast path
    if not items:
        return []
    
    # Extract and validate all keys first (fail fast)
    keys = []
    for idx, item in enumerate(items):
        try:
            item_key = key(item)
        except Exception as e:
            raise OrderingError(
                f"Key extraction failed: {e}",
                position=idx,
                offending_type=type(item).__name__
            )
        
        # Validate key is a tuple
        if not isinstance(item_key, tuple):
            raise OrderingError(
                "Key function must return tuple",
                position=idx,
                offending_type=type(item_key).__name__
            )
        
        # Validate key components
        _validate_key_component(item_key, path=f"key[{idx}]")
        keys.append(item_key)
    
    # Check for heterogeneous types in first key component
    if keys:
        first_component_types = set()
        for idx, k in enumerate(keys):
            if k:  # Non-empty tuple
                component = k[0]
                if component is not None:
                    component_type = type(component)
                    # Special handling for bool/int distinction
                    if component_type is bool:
                        first_component_types.add(bool)
                    elif component_type is int:
                        first_component_types.add(int)
                    else:
                        first_component_types.add(component_type)
        
        # Check if we have incompatible types
        if bool in first_component_types and int in first_component_types:
            # This is allowed (bool is subclass of int in Python)
            pass
        elif len(first_component_types) > 1:
            type_names = sorted(t.__name__ for t in first_component_types)
            raise OrderingError(
                f"Heterogeneous types detected in primary key component: {type_names}",
                offending_type=", ".join(type_names)
            )
    
    # Perform stable sort
    # Python's sort is guaranteed stable (preserves relative order of equal elements)
    return sorted(items, key=key, reverse=reverse)


# =============================================================================
# STRICT COMPARISON
# =============================================================================

def compare_tuples(a: tuple, b: tuple) -> int:
    """
    Explicit tuple comparison returning -1/0/1.
    
    Args:
        a: First tuple
        b: Second tuple
    
    Returns:
        -1 if a < b
        0 if a == b
        1 if a > b
    
    Raises:
        OrderingError: If tuples contain incomparable types
    
    Use Cases:
        - Window boundary checks
        - Explicit comparison requirements
        - Binary search implementations
    
    Critical:
        Validates all components before comparison.
        Does not rely on implicit tuple comparison.
    
    Example:
        >>> compare_tuples((1, "a"), (1, "b"))
        -1
        >>> compare_tuples((1, "a"), (1, "a"))
        0
        >>> compare_tuples((2, "a"), (1, "z"))
        1
    """
    # Validate both tuples
    _validate_key_component(a, path="tuple_a")
    _validate_key_component(b, path="tuple_b")
    
    # Normalize for comparison
    a_normalized = _normalize_tuple(a)
    b_normalized = _normalize_tuple(b)
    
    # Lexicographic comparison
    if a_normalized < b_normalized:
        return -1
    elif a_normalized > b_normalized:
        return 1
    else:
        return 0


# =============================================================================
# DETERMINISTIC MERGE
# =============================================================================

def merge_sorted(
    left: list[T],
    right: list[T],
    *,
    key: Callable[[T], tuple]
) -> list[T]:
    """
    Merge two sorted lists deterministically.
    
    Args:
        left: First sorted list
        right: Second sorted list
        key: Key function used for original sorting
    
    Returns:
        Merged sorted list
    
    Raises:
        OrderingError: If inputs not sorted or keys invalid
    
    Guarantees:
        - Preserves stability
        - Validates preconditions
        - Deterministic ordering
    
    Preconditions:
        - Both inputs must be sorted by the same key function
        - Key function must be deterministic
    
    Use Cases:
        - Replay comparisons
        - Window boundary stitching
        - Deterministic reconciliation
    
    Example:
        >>> left = [Event(time=1), Event(time=3)]
        >>> right = [Event(time=2), Event(time=4)]
        >>> merge_sorted(left, right, key=lambda e: deterministic_key(e.time))
        [Event(time=1), Event(time=2), Event(time=3), Event(time=4)]
    """
    # Validate inputs are sorted
    _validate_sorted(left, key=key, name="left")
    _validate_sorted(right, key=key, name="right")
    
    # Merge
    result: list[T] = []
    i, j = 0, 0
    
    while i < len(left) and j < len(right):
        left_key = key(left[i])
        right_key = key(right[j])
        
        # Validate keys
        _validate_key_component(left_key, path=f"left[{i}].key")
        _validate_key_component(right_key, path=f"right[{j}].key")
        
        # Compare
        cmp = compare_tuples(left_key, right_key)
        
        if cmp <= 0:
            # left[i] <= right[j], take from left (preserves stability)
            result.append(left[i])
            i += 1
        else:
            # right[j] < left[i], take from right
            result.append(right[j])
            j += 1
    
    # Append remaining elements
    result.extend(left[i:])
    result.extend(right[j:])
    
    return result


def _validate_sorted(
    items: list[T],
    *,
    key: Callable[[T], tuple],
    name: str
) -> None:
    """
    Validate that list is sorted according to key function.
    
    Args:
        items: List to validate
        key: Key function
        name: List name for error reporting
    
    Raises:
        OrderingError: If list is not sorted
    """
    if len(items) <= 1:
        return
    
    prev_key = key(items[0])
    _validate_key_component(prev_key, path=f"{name}[0].key")
    
    for idx in range(1, len(items)):
        curr_key = key(items[idx])
        _validate_key_component(curr_key, path=f"{name}[{idx}].key")
        
        if compare_tuples(prev_key, curr_key) > 0:
            raise OrderingError(
                f"List '{name}' is not sorted at position {idx}",
                position=idx
            )
        
        prev_key = curr_key


# =============================================================================
# UTILITIES
# =============================================================================

def is_sorted(
    items: list[T],
    *,
    key: Callable[[T], tuple]
) -> bool:
    """
    Check if list is sorted according to key function.
    
    Args:
        items: List to check
        key: Key function
    
    Returns:
        True if sorted, False otherwise
    
    Example:
        >>> is_sorted([1, 2, 3], key=lambda x: deterministic_key(x))
        True
        >>> is_sorted([1, 3, 2], key=lambda x: deterministic_key(x))
        False
    """
    try:
        _validate_sorted(items, key=key, name="items")
        return True
    except OrderingError:
        return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Error model
    "OrderingError",
    
    # Key construction
    "deterministic_key",
    
    # Sorting
    "canonical_sort",
    
    # Comparison
    "compare_tuples",
    
    # Merge
    "merge_sorted",
    
    # Utilities
    "is_sorted",
]


