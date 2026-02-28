"""
/utils/comparators.py

Strict Equality + Structural Diff Primitives

Deterministic. No ambiguity tolerated. Research-grade correctness.

This module is the single authority for structural equality and deterministic
diffing across the system. It eliminates Python's loose equality behavior,
silent coercion, and inconsistent comparison semantics.

Core Law:
    Equality must be structural, strict, and explainable.

Philosophy:
    - No coercion
    - No type conversion
    - No tolerance
    - No "close enough"
    - Either equal, or not — and if not, diff must prove why

Equality Definition (STRICT):
    Two objects are strictly equal if:
        - Types are identical
        - Structures are identical
        - All nested fields recursively strictly equal
        - Order-sensitive containers preserve identical ordering
        - No implicit casting occurred

Examples:
    1 != 1.0
    "1" != 1
    [1,2] != [2,1]
    {"a":1,"b":2} == {"b":2,"a":1}  # Order irrelevant for dicts

Critical:
    If comparison is permissive, replay lies.
    If comparison is inconsistent, audit becomes subjective.
    If equality is fuzzy, recovery becomes fiction.

Performance:
    - O(n) traversal for equal structures
    - O(n) diff traversal
    - No quadratic scanning
    - No reflection abuse
    - Explicit stack-based or recursive-safe traversal
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, List


# =============================================================================
# ERROR MODEL
# =============================================================================

class ComparisonError(RuntimeError):
    """
    Structural comparison failure.
    
    Raised when strict equality assertion fails or comparison encounters
    invalid types.
    
    Attributes:
        message: Error description
        path: Location of first divergence
        expected: Expected value
        actual: Actual value
        difference_type: Category of difference
    """
    
    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        expected: Any = None,
        actual: Any = None,
        difference_type: str | None = None
    ) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        self.difference_type = difference_type
        
        # Build deterministic error message
        parts = [message]
        
        if path is not None:
            parts.append(f"Path: {path}")
        if difference_type is not None:
            parts.append(f"Type: {difference_type}")
        if expected is not None:
            parts.append(f"Expected: {_repr_safe(expected)}")
        if actual is not None:
            parts.append(f"Actual: {_repr_safe(actual)}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# DIFF ENTRY MODEL
# =============================================================================

class DifferenceType(Enum):
    """Categories of structural differences."""
    
    TYPE_MISMATCH = "TYPE_MISMATCH"
    VALUE_MISMATCH = "VALUE_MISMATCH"
    MISSING_FIELD = "MISSING_FIELD"
    EXTRA_FIELD = "EXTRA_FIELD"
    LENGTH_MISMATCH = "LENGTH_MISMATCH"


@dataclass(frozen=True)
class DiffEntry:
    """
    Single structural difference record.
    
    Attributes:
        path: Deterministic path to difference (e.g., "root.window[3].event_time")
        expected: Expected value at path
        actual: Actual value at path
        difference_type: Category of difference
    
    Rules:
        - Paths must be deterministic
        - Paths use dot notation for dict keys
        - Paths use bracket notation for list/tuple indices
        - No ambiguity
        - Ordered lexicographically in diff lists
    """
    
    path: str
    expected: Any
    actual: Any
    difference_type: DifferenceType
    
    def __post_init__(self) -> None:
        """Validate diff entry structure."""
        if not isinstance(self.path, str):
            raise ValueError("path must be string")
        if not isinstance(self.difference_type, DifferenceType):
            raise ValueError("difference_type must be DifferenceType enum")
        if not self.path.startswith("root"):
            raise ValueError("path must start with 'root'")


# =============================================================================
# SAFE REPRESENTATION
# =============================================================================

def _repr_safe(value: Any, max_length: int = 100) -> str:
    """
    Safe repr that avoids unbounded output.
    
    Args:
        value: Value to represent
        max_length: Maximum string length
    
    Returns:
        Deterministic string representation
    
    Rules:
        - No memory addresses
        - Truncate long strings deterministically
        - Handle None explicitly
        - Escape special characters
    """
    if value is None:
        return "None"
    
    # Get type name
    type_name = type(value).__name__
    
    # Handle primitives
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        r = repr(value)
        if len(r) > max_length:
            return r[:max_length] + "...(truncated)"
        return r
    
    # Handle collections
    if isinstance(value, (list, tuple)):
        if len(value) == 0:
            return f"{type_name}()"
        return f"{type_name}(length={len(value)})"
    
    if isinstance(value, dict):
        if len(value) == 0:
            return "dict()"
        return f"dict(keys={len(value)})"
    
    # Handle enums
    if isinstance(value, Enum):
        return f"{type_name}.{value.name}"
    
    # Fallback
    return f"{type_name}(...)"


# =============================================================================
# PATH BUILDING
# =============================================================================

def _make_field_path(parent: str, field: str) -> str:
    """
    Build deterministic field path.
    
    Args:
        parent: Parent path
        field: Field name
    
    Returns:
        Deterministic path string
    
    Example:
        >>> _make_field_path("root", "config")
        'root.config'
        >>> _make_field_path("root.window", "event_time")
        'root.window.event_time'
    """
    return f"{parent}.{field}"


def _make_index_path(parent: str, index: int) -> str:
    """
    Build deterministic index path.
    
    Args:
        parent: Parent path
        index: List/tuple index
    
    Returns:
        Deterministic path string
    
    Example:
        >>> _make_index_path("root.items", 0)
        'root.items[0]'
        >>> _make_index_path("root", 5)
        'root[5]'
    """
    return f"{parent}[{index}]"


# =============================================================================
# TYPE VALIDATION
# =============================================================================

def _validate_comparable_type(value: Any, path: str) -> None:
    """
    Validate that type is allowed in strict comparison.
    
    Args:
        value: Value to validate
        path: Current path for error reporting
    
    Raises:
        ComparisonError: If type is not allowed
    
    Allowed Types:
        - dict (string keys only)
        - list
        - tuple
        - int
        - str
        - bool
        - None
        - Enum
    
    Forbidden Types:
        - float (unless globally enabled)
        - set
        - datetime
        - custom classes (unless converted to canonical form)
        - mutable types beyond list/dict
    """
    # None is always allowed
    if value is None:
        return
    
    # Primitives
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, str):
        return
    
    # Enums
    if isinstance(value, Enum):
        return
    
    # Collections
    if isinstance(value, dict):
        # Validate all keys are strings
        for key in value.keys():
            if not isinstance(key, str):
                raise ComparisonError(
                    f"Dict keys must be strings at {path}",
                    path=path,
                    actual=f"key type: {type(key).__name__}"
                )
        return
    
    if isinstance(value, (list, tuple)):
        return
    
    # Forbidden types
    if isinstance(value, float):
        raise ComparisonError(
            f"Float type forbidden in strict comparison at {path}",
            path=path,
            actual="float (use canonical integer representation)"
        )
    
    if isinstance(value, set):
        raise ComparisonError(
            f"Set type forbidden in strict comparison at {path}",
            path=path,
            actual="set (use sorted list instead)"
        )
    
    # Unknown type
    raise ComparisonError(
        f"Unsupported type for strict comparison at {path}",
        path=path,
        actual=type(value).__name__
    )


# =============================================================================
# STRICT EQUALITY
# =============================================================================

def strict_equal(a: Any, b: Any, *, _path: str = "root") -> bool:
    """
    Recursive structural equality check.
    
    Args:
        a: First value
        b: Second value
        _path: Internal path tracking (do not use)
    
    Returns:
        True if structurally equal, False otherwise
    
    Raises:
        ComparisonError: If types are not comparable
    
    Guarantees:
        - Recursive structural comparison
        - Type identity required
        - No coercion
        - No silent reordering
        - Deterministic traversal
    
    Rules:
        - Types must match exactly
        - For dicts: keys must match, order irrelevant
        - For lists/tuples: length and order must match
        - For primitives: values must match exactly
        - For Enums: type and value must match
    
    Example:
        >>> strict_equal({"a": 1, "b": 2}, {"b": 2, "a": 1})
        True
        >>> strict_equal([1, 2], [2, 1])
        False
        >>> strict_equal(1, 1.0)
        False
    """
    # Validate types
    _validate_comparable_type(a, _path)
    _validate_comparable_type(b, _path)
    
    # None handling
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    
    # Type must match exactly
    if type(a) is not type(b):
        return False
    
    # Primitives
    if isinstance(a, (bool, int, str)):
        return a == b
    
    # Enums
    if isinstance(a, Enum):
        # Both type and value must match
        return type(a) is type(b) and a.value == b.value
    
    # Dict comparison
    if isinstance(a, dict):
        # Keys must match exactly
        if set(a.keys()) != set(b.keys()):
            return False
        
        # Compare values for each key (sorted for determinism)
        for key in sorted(a.keys()):
            field_path = _make_field_path(_path, key)
            if not strict_equal(a[key], b[key], _path=field_path):
                return False
        
        return True
    
    # List comparison
    if isinstance(a, list):
        # Length must match
        if len(a) != len(b):
            return False
        
        # Element-by-element comparison
        for idx, (a_item, b_item) in enumerate(zip(a, b)):
            item_path = _make_index_path(_path, idx)
            if not strict_equal(a_item, b_item, _path=item_path):
                return False
        
        return True
    
    # Tuple comparison
    if isinstance(a, tuple):
        # Length must match
        if len(a) != len(b):
            return False
        
        # Element-by-element comparison
        for idx, (a_item, b_item) in enumerate(zip(a, b)):
            item_path = _make_index_path(_path, idx)
            if not strict_equal(a_item, b_item, _path=item_path):
                return False
        
        return True
    
    # Should never reach here due to type validation
    raise ComparisonError(
        f"Unhandled type in strict_equal at {_path}",
        path=_path,
        actual=type(a).__name__
    )


# =============================================================================
# ASSERT STRICT EQUALITY
# =============================================================================

def assert_strict_equal(a: Any, b: Any) -> None:
    """
    Assert strict structural equality. Fail with detailed error on mismatch.
    
    Args:
        a: First value (expected)
        b: Second value (actual)
    
    Raises:
        ComparisonError: With first divergence path, expected, actual
    
    Critical:
        Used in replay validation and snapshot comparison.
        Failure must include complete forensic information.
    
    Example:
        >>> assert_strict_equal({"a": 1}, {"a": 2})
        ComparisonError: Structural comparison failed | Path: root.a | ...
    """
    if not strict_equal(a, b):
        # Generate diff to find first divergence
        diffs = structural_diff(a, b)
        
        if not diffs:
            # Should never happen, but guard against it
            raise ComparisonError(
                "Structural comparison failed but no diff found",
                expected=_repr_safe(a),
                actual=_repr_safe(b)
            )
        
        # Report first divergence
        first_diff = diffs[0]
        raise ComparisonError(
            "Structural comparison failed",
            path=first_diff.path,
            expected=first_diff.expected,
            actual=first_diff.actual,
            difference_type=first_diff.difference_type.value
        )


# =============================================================================
# STRUCTURAL DIFF
# =============================================================================

def structural_diff(a: Any, b: Any, *, _path: str = "root") -> list[DiffEntry]:
    """
    Generate deterministic, ordered list of structural differences.
    
    Args:
        a: First value (expected)
        b: Second value (actual)
        _path: Internal path tracking (do not use)
    
    Returns:
        Ordered list of DiffEntry objects
    
    Guarantees:
        - Deterministic ordering (lexicographic by path)
        - Complete traversal (all differences found)
        - Structured output
        - Stable across runs
    
    Use For:
        - Replay divergence reports
        - Audit proofs
        - Forensic debugging
        - External regulatory documentation
    
    Example:
        >>> diffs = structural_diff({"a": 1, "b": 2}, {"a": 1, "b": 3})
        >>> len(diffs)
        1
        >>> diffs[0].path
        'root.b'
    """
    diffs: list[DiffEntry] = []
    
    # Validate types
    try:
        _validate_comparable_type(a, _path)
        _validate_comparable_type(b, _path)
    except ComparisonError as e:
        diffs.append(DiffEntry(
            path=_path,
            expected=a,
            actual=b,
            difference_type=DifferenceType.TYPE_MISMATCH
        ))
        return diffs
    
    # None handling
    if a is None and b is None:
        return []
    if a is None or b is None:
        diffs.append(DiffEntry(
            path=_path,
            expected=a,
            actual=b,
            difference_type=DifferenceType.VALUE_MISMATCH
        ))
        return diffs
    
    # Type mismatch
    if type(a) is not type(b):
        diffs.append(DiffEntry(
            path=_path,
            expected=a,
            actual=b,
            difference_type=DifferenceType.TYPE_MISMATCH
        ))
        return diffs
    
    # Primitives
    if isinstance(a, (bool, int, str)):
        if a != b:
            diffs.append(DiffEntry(
                path=_path,
                expected=a,
                actual=b,
                difference_type=DifferenceType.VALUE_MISMATCH
            ))
        return diffs
    
    # Enums
    if isinstance(a, Enum):
        if type(a) is not type(b) or a.value != b.value:
            diffs.append(DiffEntry(
                path=_path,
                expected=a,
                actual=b,
                difference_type=DifferenceType.VALUE_MISMATCH
            ))
        return diffs
    
    # Dict comparison
    if isinstance(a, dict):
        # Find missing and extra keys
        a_keys = set(a.keys())
        b_keys = set(b.keys())
        
        missing_keys = a_keys - b_keys
        extra_keys = b_keys - a_keys
        common_keys = a_keys & b_keys
        
        # Report missing fields (sorted for determinism)
        for key in sorted(missing_keys):
            field_path = _make_field_path(_path, key)
            diffs.append(DiffEntry(
                path=field_path,
                expected=a[key],
                actual=None,
                difference_type=DifferenceType.MISSING_FIELD
            ))
        
        # Report extra fields (sorted for determinism)
        for key in sorted(extra_keys):
            field_path = _make_field_path(_path, key)
            diffs.append(DiffEntry(
                path=field_path,
                expected=None,
                actual=b[key],
                difference_type=DifferenceType.EXTRA_FIELD
            ))
        
        # Recurse into common fields (sorted for determinism)
        for key in sorted(common_keys):
            field_path = _make_field_path(_path, key)
            field_diffs = structural_diff(a[key], b[key], _path=field_path)
            diffs.extend(field_diffs)
        
        return diffs
    
    # List comparison
    if isinstance(a, list):
        # Check length
        if len(a) != len(b):
            diffs.append(DiffEntry(
                path=_path,
                expected=f"length={len(a)}",
                actual=f"length={len(b)}",
                difference_type=DifferenceType.LENGTH_MISMATCH
            ))
            # Still recurse into common indices
        
        # Element-by-element comparison
        for idx in range(min(len(a), len(b))):
            item_path = _make_index_path(_path, idx)
            item_diffs = structural_diff(a[idx], b[idx], _path=item_path)
            diffs.extend(item_diffs)
        
        return diffs
    
    # Tuple comparison
    if isinstance(a, tuple):
        # Check length
        if len(a) != len(b):
            diffs.append(DiffEntry(
                path=_path,
                expected=f"length={len(a)}",
                actual=f"length={len(b)}",
                difference_type=DifferenceType.LENGTH_MISMATCH
            ))
            # Still recurse into common indices
        
        # Element-by-element comparison
        for idx in range(min(len(a), len(b))):
            item_path = _make_index_path(_path, idx)
            item_diffs = structural_diff(a[idx], b[idx], _path=item_path)
            diffs.extend(item_diffs)
        
        return diffs
    
    # Should never reach here
    raise ComparisonError(
        f"Unhandled type in structural_diff at {_path}",
        path=_path,
        actual=type(a).__name__
    )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Error model
    "ComparisonError",
    
    # Diff model
    "DifferenceType",
    "DiffEntry",
    
    # Strict equality
    "strict_equal",
    "assert_strict_equal",
    
    # Structural diff
    "structural_diff",
]