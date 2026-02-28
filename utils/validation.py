"""
/utils/validation.py

Reusable Structural + Invariant Enforcement Primitives

Research-grade. Deterministic. No ambiguity.

This module provides composable, reusable validation primitives for structural
correctness across the entire system. It enforces declared constraints explicitly,
deterministically, and without mutation or inference.

Core Law:
    Structural validation must be declarative, explicit, and deterministic.

Validators:
    - Declare allowed structure
    - Do not mutate
    - Do not infer
    - Do not repair
    - Fail early, fail explicitly

Error Model:
    All failures raise ValidationError with:
        - message
        - path
        - expected constraint
        - actual value/type
        - deterministic formatting

Performance:
    - O(n) structural walk
    - No reflection abuse
    - No recursion stack explosions
    - Linear traversal only

Determinism:
    Given identical input:
        - Same failure path
        - Same failure message
        - Same ordering of field checks
        - Same enumeration ordering
        - Same regex behavior
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Pattern, Sequence, Tuple
import re


# =============================================================================
# ERROR MODEL
# =============================================================================

class ValidationError(RuntimeError):
    """
    Structural validation failure.
    
    All validation failures raise this exception with:
        - Clear message
        - Field path
        - Expected constraint
        - Actual value/type
        - Deterministic formatting
    
    Never raise generic ValueError or Exception.
    """
    
    def __init__(
        self,
        message: str,
        *,
        path: str,
        expected: str | None = None,
        actual: str | None = None
    ) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        
        # Build deterministic error message
        parts = [f"Validation failed at {path}: {message}"]
        if expected is not None:
            parts.append(f"Expected: {expected}")
        if actual is not None:
            parts.append(f"Actual: {actual}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# FIELD PRESENCE ENFORCEMENT
# =============================================================================

def require_fields(
    obj: Mapping[str, Any],
    required: frozenset[str],
    *,
    path: str = "root"
) -> None:
    """
    Guarantee all required keys are present.
    
    Args:
        obj: Mapping to validate
        required: Required field names
        path: Current validation path
    
    Raises:
        ValidationError: If any required field is missing
    
    Guarantees:
        - All required keys present
        - Deterministic missing key detection
        - Sorted reporting
    
    Example:
        >>> require_fields({"a": 1}, frozenset(["a", "b"]))
        ValidationError: ... Missing required fields: ["b"]
    """
    if not isinstance(obj, Mapping):
        raise ValidationError(
            "Expected mapping type",
            path=path,
            expected="Mapping[str, Any]",
            actual=type(obj).__name__
        )
    
    missing = required - obj.keys()
    if missing:
        # Sort for deterministic error message
        sorted_missing = sorted(missing)
        raise ValidationError(
            f"Missing required fields: {sorted_missing}",
            path=path,
            expected=f"fields {sorted(required)}",
            actual=f"fields {sorted(obj.keys())}"
        )


def forbid_extra_fields(
    obj: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    path: str = "root"
) -> None:
    """
    Reject silent schema drift by forbidding unknown fields.
    
    Args:
        obj: Mapping to validate
        allowed: Allowed field names
        path: Current validation path
    
    Raises:
        ValidationError: If any extra field is present
    
    Example:
        >>> forbid_extra_fields({"a": 1, "b": 2}, frozenset(["a"]))
        ValidationError: ... Unexpected fields: ["b"]
    """
    if not isinstance(obj, Mapping):
        raise ValidationError(
            "Expected mapping type",
            path=path,
            expected="Mapping[str, Any]",
            actual=type(obj).__name__
        )
    
    extra = obj.keys() - allowed
    if extra:
        # Sort for deterministic error message
        sorted_extra = sorted(extra)
        raise ValidationError(
            f"Unexpected fields: {sorted_extra}",
            path=path,
            expected=f"only fields {sorted(allowed)}",
            actual=f"fields {sorted(obj.keys())}"
        )


# =============================================================================
# TYPE ENFORCEMENT
# =============================================================================

def require_exact_type(
    value: Any,
    expected: type,
    *,
    name: str,
    path: str = "root"
) -> None:
    """
    Enforce exact type match. No subclass cheating.
    
    Args:
        value: Value to validate
        expected: Expected exact type
        name: Field name for error reporting
        path: Current validation path
    
    Raises:
        ValidationError: If type does not match exactly
    
    Critical:
        - bool is not int (even though isinstance(True, int) is True)
        - No subclass acceptance
        - Exact type() match required
    
    Example:
        >>> require_exact_type(True, int, name="count")
        ValidationError: ... Expected exact type int, got bool
    """
    actual_type = type(value)
    
    # Special case: bool is subclass of int in Python, but we want exact match
    if expected is int and actual_type is bool:
        raise ValidationError(
            f"Field '{name}' has wrong type",
            path=f"{path}.{name}",
            expected=expected.__name__,
            actual=actual_type.__name__
        )
    
    if actual_type is not expected:
        raise ValidationError(
            f"Field '{name}' has wrong type",
            path=f"{path}.{name}",
            expected=expected.__name__,
            actual=actual_type.__name__
        )


def require_one_of_types(
    value: Any,
    allowed: tuple[type, ...],
    *,
    name: str,
    path: str = "root"
) -> None:
    """
    Enforce explicit union typing.
    
    Args:
        value: Value to validate
        allowed: Tuple of allowed types
        name: Field name for error reporting
        path: Current validation path
    
    Raises:
        ValidationError: If type is not in allowed set
    
    Example:
        >>> require_one_of_types(3.14, (int, str), name="value")
        ValidationError: ... Expected one of (int, str), got float
    """
    actual_type = type(value)
    
    # Check for exact type match (handling bool/int case)
    for allowed_type in allowed:
        if allowed_type is int and actual_type is bool:
            continue  # bool should not match int requirement
        if actual_type is allowed_type:
            return
    
    allowed_names = ", ".join(t.__name__ for t in allowed)
    raise ValidationError(
        f"Field '{name}' has wrong type",
        path=f"{path}.{name}",
        expected=f"one of ({allowed_names})",
        actual=actual_type.__name__
    )


# =============================================================================
# VALUE CONSTRAINTS
# =============================================================================

def require_in_set(
    value: Any,
    allowed: frozenset[Any],
    *,
    name: str,
    path: str = "root"
) -> None:
    """
    Reject unknown enum values.
    
    Args:
        value: Value to validate
        allowed: Allowed values
        name: Field name for error reporting
        path: Current validation path
    
    Raises:
        ValidationError: If value not in allowed set
    
    Example:
        >>> require_in_set("blue", frozenset(["red", "green"]), name="color")
        ValidationError: ... Expected one of [green, red], got blue
    """
    if value not in allowed:
        # Sort for deterministic error message
        sorted_allowed = sorted(str(v) for v in allowed)
        raise ValidationError(
            f"Field '{name}' has invalid value",
            path=f"{path}.{name}",
            expected=f"one of {sorted_allowed}",
            actual=repr(value)
        )


def require_range(
    value: int | float,
    *,
    min_value: int | float | None = None,
    max_value: int | float | None = None,
    name: str,
    path: str = "root"
) -> None:
    """
    Enforce numeric range constraints.
    
    Args:
        value: Value to validate
        min_value: Minimum allowed value (inclusive), None for no minimum
        max_value: Maximum allowed value (inclusive), None for no maximum
        name: Field name for error reporting
        path: Current validation path
    
    Raises:
        ValidationError: If value outside range
    
    Note:
        Accepts both int and float, but prefer int-only validation
        for deterministic behavior across systems.
    
    Example:
        >>> require_range(150, min_value=0, max_value=100, name="percentage")
        ValidationError: ... Expected value in range [0, 100], got 150
    """
    # Type check first
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationError(
            f"Field '{name}' must be numeric",
            path=f"{path}.{name}",
            expected="int or float",
            actual=type(value).__name__
        )
    
    # Build range description
    if min_value is not None and max_value is not None:
        range_desc = f"[{min_value}, {max_value}]"
        if value < min_value or value > max_value:
            raise ValidationError(
                f"Field '{name}' out of range",
                path=f"{path}.{name}",
                expected=f"value in range {range_desc}",
                actual=str(value)
            )
    elif min_value is not None:
        range_desc = f">= {min_value}"
        if value < min_value:
            raise ValidationError(
                f"Field '{name}' out of range",
                path=f"{path}.{name}",
                expected=range_desc,
                actual=str(value)
            )
    elif max_value is not None:
        range_desc = f"<= {max_value}"
        if value > max_value:
            raise ValidationError(
                f"Field '{name}' out of range",
                path=f"{path}.{name}",
                expected=range_desc,
                actual=str(value)
            )


def require_pattern(
    value: str,
    *,
    regex: Pattern[str],
    name: str,
    path: str = "root"
) -> None:
    """
    Enforce regex pattern matching.
    
    Args:
        value: String to validate
        regex: Compiled regex pattern (must be pre-compiled)
        name: Field name for error reporting
        path: Current validation path
    
    Raises:
        ValidationError: If value does not match pattern
    
    Critical:
        - Regex must be compiled outside (for determinism)
        - Use re.compile() before calling
        - Full match semantics (anchored)
    
    Example:
        >>> pattern = re.compile(r'^[a-z]+$')
        >>> require_pattern("Hello", regex=pattern, name="username")
        ValidationError: ... Expected match for pattern ^[a-z]+$
    """
    if not isinstance(value, str):
        raise ValidationError(
            f"Field '{name}' must be string",
            path=f"{path}.{name}",
            expected="str",
            actual=type(value).__name__
        )
    
    if not regex.fullmatch(value):
        raise ValidationError(
            f"Field '{name}' does not match required pattern",
            path=f"{path}.{name}",
            expected=f"match for pattern {regex.pattern}",
            actual=repr(value)
        )


# =============================================================================
# STRUCTURAL SHAPE VALIDATION
# =============================================================================

def validate_mapping(
    obj: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    path: str = "root"
) -> None:
    """
    Enforce complete mapping structure.
    
    Args:
        obj: Mapping to validate
        required: Required field names
        optional: Optional field names (default: none)
        path: Current validation path
    
    Raises:
        ValidationError: If structure invalid
    
    Enforces:
        - All required fields present
        - No unknown fields
        - Known optional fields allowed
    
    Example:
        >>> validate_mapping(
        ...     {"name": "Alice", "extra": 1},
        ...     required=frozenset(["name"]),
        ...     optional=frozenset(["age"])
        ... )
        ValidationError: ... Unexpected fields: ["extra"]
    """
    require_fields(obj, required, path=path)
    allowed = required | optional
    forbid_extra_fields(obj, allowed, path=path)


def validate_list_of(
    items: Sequence[Any],
    *,
    item_validator: Callable[[Any, str], None],
    path: str = "root"
) -> None:
    """
    Apply validator to each list item with deterministic pathing.
    
    Args:
        items: Sequence to validate
        item_validator: Validator function(item, path) -> None
        path: Current validation path
    
    Raises:
        ValidationError: If any item fails validation
    
    Path Format:
        root.items[0]
        root.items[1]
        etc.
    
    Example:
        >>> def validate_positive(x: Any, p: str) -> None:
        ...     require_exact_type(x, int, name="item", path=p)
        ...     require_range(x, min_value=1, name="item", path=p)
        >>> validate_list_of([1, -1, 3], item_validator=validate_positive)
        ValidationError: ... at root.items[1] ...
    """
    if not isinstance(items, Sequence):
        raise ValidationError(
            "Expected sequence type",
            path=path,
            expected="Sequence",
            actual=type(items).__name__
        )
    
    for idx, item in enumerate(items):
        item_path = f"{path}[{idx}]"
        item_validator(item, item_path)


# =============================================================================
# COMPOSITE VALIDATORS
# =============================================================================

def combine_validators(
    *validators: Callable[[Any, str], None]
) -> Callable[[Any, str], None]:
    """
    Compose multiple validators into single validator.
    
    Args:
        *validators: Validator functions to combine
    
    Returns:
        Combined validator function
    
    Usage:
        Used in:
            - Ingest policies
            - Window definitions
            - Computation specs
            - Registry enforcement
    
    Example:
        >>> def validate_positive_int(value: Any, path: str) -> None:
        ...     require_exact_type(value, int, name="value", path=path)
        ...     require_range(value, min_value=0, name="value", path=path)
        >>> 
        >>> validator = combine_validators(
        ...     lambda v, p: require_exact_type(v, int, name="x", path=p),
        ...     lambda v, p: require_range(v, min_value=0, max_value=100, name="x", path=p)
        ... )
    """
    def combined(value: Any, path: str) -> None:
        for validator in validators:
            validator(value, path)
    
    return combined


# =============================================================================
# DETERMINISTIC PATH HANDLING
# =============================================================================

def make_field_path(parent_path: str, field_name: str) -> str:
    """
    Build deterministic field path.
    
    Args:
        parent_path: Parent path
        field_name: Field name to append
    
    Returns:
        Deterministic path string
    
    Examples:
        >>> make_field_path("root", "config")
        'root.config'
        >>> make_field_path("root.config", "timeout_ms")
        'root.config.timeout_ms'
    """
    return f"{parent_path}.{field_name}"


def make_index_path(parent_path: str, index: int) -> str:
    """
    Build deterministic index path.
    
    Args:
        parent_path: Parent path
        index: List/sequence index
    
    Returns:
        Deterministic path string
    
    Examples:
        >>> make_index_path("root.items", 0)
        'root.items[0]'
        >>> make_index_path("root", 5)
        'root[5]'
    """
    return f"{parent_path}[{index}]"


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Error model
    "ValidationError",
    
    # Field presence
    "require_fields",
    "forbid_extra_fields",
    
    # Type enforcement
    "require_exact_type",
    "require_one_of_types",
    
    # Value constraints
    "require_in_set",
    "require_range",
    "require_pattern",
    
    # Structural validation
    "validate_mapping",
    "validate_list_of",
    
    # Composition
    "combine_validators",
    
    # Path utilities
    "make_field_path",
    "make_index_path",
]