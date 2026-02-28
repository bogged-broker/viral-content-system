"""
/utils/guards.py

Assertion Helpers (Fail Fast, Fail Loud)

Deterministic. Zero softness. Production-grade invariant enforcement.

This module is the single authority for runtime invariant enforcement across
the system. It eliminates silent contract violations, weak assertions, and
inconsistent precondition enforcement.

Core Law:
    All invariants must fail explicitly, deterministically, and permanently.

Philosophy:
    - No soft checks
    - No warnings
    - No best effort
    - No implicit defaults
    - Failure stops execution immediately

Guard failures indicate:
    - Programmer error
    - Contract violation
    - System invariant breach
    - Corrupt upstream input
    - Unauthorized mutation

These are NEVER recoverable states.

Critical:
    Python's built-in assert is BANNED.
    Reason: Stripped under -O, inconsistent errors, vague messages.
    All invariant enforcement must go through this file.

Performance:
    - O(1) checks only
    - No introspection abuse
    - No stack walking
    - Zero recursion
    - Always executes (never disabled)
"""

from __future__ import annotations

from typing import Any, NoReturn, Sized, Tuple, List, Dict
import sys


# =============================================================================
# ERROR MODEL
# =============================================================================

class GuardViolation(RuntimeError):
    """
    Invariant violation detected.
    
    Guard failures indicate programmer error, contract violation, or system
    invariant breach. These are never recoverable states.
    
    Attributes:
        message: Violation description
        context: Optional structured context
    
    Guarantees:
        - Deterministic formatting
        - Stable across runs
        - No memory addresses
        - Structured and concise
    """
    
    def __init__(self, message: str, *, context: dict[str, str] | None = None) -> None:
        self.guard_message = message
        self.guard_context = context or {}
        
        # Build deterministic error message
        parts = [f"Guard violation: {message}"]
        
        if context:
            # Sort context keys for determinism
            sorted_items = sorted(context.items())
            context_strs = [f"{k}={v}" for k, v in sorted_items]
            parts.append(f"Context: {', '.join(context_strs)}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# CONDITION ENFORCEMENT
# =============================================================================

def require(condition: bool, *, message: str) -> None:
    """
    Enforce precondition. Fail if False.
    
    Args:
        condition: Must be True
        message: Violation description (required)
    
    Raises:
        GuardViolation: If condition is False
    
    Use For:
        - Precondition enforcement
        - Constructor validation
        - Function argument enforcement
    
    Rules:
        - Message required
        - Deterministic formatting
        - Never silently passes
    
    Example:
        >>> require(len(events) > 0, message="events must not be empty")
        >>> require(window_size > 0, message="window_size must be positive")
        >>> require(version in VALID_VERSIONS, message="invalid version")
    """
    if not condition:
        raise GuardViolation(message)


def forbid(condition: bool, *, message: str) -> None:
    """
    Enforce negative precondition. Fail if True.
    
    Args:
        condition: Must be False
        message: Violation description (required)
    
    Raises:
        GuardViolation: If condition is True
    
    Inverse of require(). Use when checking for prohibited states.
    
    Example:
        >>> forbid(isinstance(obj, float), message="floats forbidden in canonical path")
        >>> forbid(has_duplicates(ids), message="duplicate IDs detected")
        >>> forbid(is_mutated, message="unauthorized mutation detected")
    """
    if condition:
        raise GuardViolation(message)


# =============================================================================
# TYPE ENFORCEMENT
# =============================================================================

def require_type(
    obj: Any,
    expected_type: type | tuple[type, ...],
    *,
    name: str
) -> None:
    """
    Enforce strict type requirement.
    
    Args:
        obj: Object to check
        expected_type: Required type or tuple of types
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If obj is not instance of expected_type
    
    Guarantees:
        - Strict isinstance match
        - No implicit coercion
        - Exact type mismatch detail
    
    Error Includes:
        - Variable name
        - Expected type
        - Actual type
    
    Example:
        >>> require_type(count, int, name="count")
        >>> require_type(config, (dict, ConfigObject), name="config")
    """
    if not isinstance(obj, expected_type):
        if isinstance(expected_type, tuple):
            type_names = ", ".join(t.__name__ for t in expected_type)
            expected_str = f"one of ({type_names})"
        else:
            expected_str = expected_type.__name__
        
        actual_str = type(obj).__name__
        
        raise GuardViolation(
            f"Type mismatch for '{name}'",
            context={
                "expected": expected_str,
                "actual": actual_str
            }
        )


def require_not_none(obj: Any, *, name: str) -> None:
    """
    Enforce non-None requirement. No silent None propagation.
    
    Args:
        obj: Object to check
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If obj is None
    
    Critical:
        None should never silently propagate through the system.
        If None is unexpected, fail immediately.
    
    Example:
        >>> require_not_none(user_id, name="user_id")
        >>> require_not_none(config.get("timeout"), name="config.timeout")
    """
    if obj is None:
        raise GuardViolation(
            f"'{name}' must not be None",
            context={"name": name}
        )


# =============================================================================
# COLLECTION ENFORCEMENT
# =============================================================================

def require_non_empty(collection: Sized, *, name: str) -> None:
    """
    Enforce non-empty collection requirement.
    
    Args:
        collection: Collection to check (must have __len__)
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If collection is empty
    
    Use For:
        - Pipelines
        - Ingestion batches
        - Window definitions
        - Required lists
    
    Critical:
        Empty collections must be intentional, not accidental.
        If empty is illegal, fail immediately.
    
    Example:
        >>> require_non_empty(events, name="events")
        >>> require_non_empty(pipeline_steps, name="pipeline_steps")
    """
    # Type check first
    if not isinstance(collection, Sized):
        raise GuardViolation(
            f"'{name}' must be a collection",
            context={
                "name": name,
                "actual_type": type(collection).__name__
            }
        )
    
    if len(collection) == 0:
        raise GuardViolation(
            f"'{name}' must not be empty",
            context={"name": name}
        )


def require_length(
    collection: Sized,
    *,
    expected_length: int,
    name: str
) -> None:
    """
    Enforce exact length requirement.
    
    Args:
        collection: Collection to check
        expected_length: Required length
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If length does not match
    
    Example:
        >>> require_length(coordinates, expected_length=2, name="coordinates")
        >>> require_length(version_tuple, expected_length=3, name="version")
    """
    # Type check first
    if not isinstance(collection, Sized):
        raise GuardViolation(
            f"'{name}' must be a collection",
            context={
                "name": name,
                "actual_type": type(collection).__name__
            }
        )
    
    actual_length = len(collection)
    if actual_length != expected_length:
        raise GuardViolation(
            f"'{name}' has wrong length",
            context={
                "name": name,
                "expected": str(expected_length),
                "actual": str(actual_length)
            }
        )


# =============================================================================
# UNREACHABLE CODE ENFORCEMENT
# =============================================================================

def unreachable(message: str) -> NoReturn:
    """
    Mark code path as logically unreachable.
    
    Args:
        message: Description of why this is unreachable
    
    Raises:
        GuardViolation: Always raises, never returns
    
    Use For:
        - Exhaustive Enum handling
        - Logic branches declared impossible
        - Switch statement defaults that should never execute
    
    Critical:
        This function MUST always raise.
        Never passes silently.
        Never returns normally.
    
    Example:
        >>> match event_type:
        ...     case EventType.CREATED:
        ...         handle_created()
        ...     case EventType.UPDATED:
        ...         handle_updated()
        ...     case _:
        ...         unreachable(f"unknown event type: {event_type}")
    """
    raise GuardViolation(f"Unreachable code executed: {message}")


# =============================================================================
# NUMERIC ENFORCEMENT
# =============================================================================

def require_positive(value: int | float, *, name: str) -> None:
    """
    Enforce positive number requirement (> 0).
    
    Args:
        value: Number to check
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If value <= 0
    
    Example:
        >>> require_positive(timeout_ms, name="timeout_ms")
        >>> require_positive(batch_size, name="batch_size")
    """
    # Type check first
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GuardViolation(
            f"'{name}' must be numeric",
            context={
                "name": name,
                "actual_type": type(value).__name__
            }
        )
    
    if value <= 0:
        raise GuardViolation(
            f"'{name}' must be positive",
            context={
                "name": name,
                "actual": str(value)
            }
        )


def require_non_negative(value: int | float, *, name: str) -> None:
    """
    Enforce non-negative number requirement (>= 0).
    
    Args:
        value: Number to check
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If value < 0
    
    Example:
        >>> require_non_negative(offset, name="offset")
        >>> require_non_negative(count, name="count")
    """
    # Type check first
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise GuardViolation(
            f"'{name}' must be numeric",
            context={
                "name": name,
                "actual_type": type(value).__name__
            }
        )
    
    if value < 0:
        raise GuardViolation(
            f"'{name}' must be non-negative",
            context={
                "name": name,
                "actual": str(value)
            }
        )


# =============================================================================
# STRING ENFORCEMENT
# =============================================================================

def require_non_blank(value: str, *, name: str) -> None:
    """
    Enforce non-blank string requirement.
    
    Args:
        value: String to check
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If string is empty or whitespace-only
    
    Critical:
        Checks for empty strings AND whitespace-only strings.
        "   " is considered blank.
    
    Example:
        >>> require_non_blank(event_id, name="event_id")
        >>> require_non_blank(username, name="username")
    """
    # Type check first
    if not isinstance(value, str):
        raise GuardViolation(
            f"'{name}' must be string",
            context={
                "name": name,
                "actual_type": type(value).__name__
            }
        )
    
    if not value or not value.strip():
        raise GuardViolation(
            f"'{name}' must not be blank",
            context={"name": name}
        )


# =============================================================================
# COMPARISON ENFORCEMENT
# =============================================================================

def require_equal(
    actual: Any,
    expected: Any,
    *,
    name: str
) -> None:
    """
    Enforce equality requirement.
    
    Args:
        actual: Actual value
        expected: Expected value
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If actual != expected
    
    Example:
        >>> require_equal(version, 2, name="schema_version")
        >>> require_equal(len(batch), batch_size, name="batch_length")
    """
    if actual != expected:
        raise GuardViolation(
            f"'{name}' has wrong value",
            context={
                "name": name,
                "expected": repr(expected),
                "actual": repr(actual)
            }
        )


def require_in(
    value: Any,
    allowed: set[Any] | frozenset[Any] | list[Any] | tuple[Any, ...],
    *,
    name: str
) -> None:
    """
    Enforce membership requirement.
    
    Args:
        value: Value to check
        allowed: Allowed values
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If value not in allowed
    
    Example:
        >>> require_in(status, {"active", "pending"}, name="status")
        >>> require_in(event_type, VALID_EVENT_TYPES, name="event_type")
    """
    if value not in allowed:
        # Sort for deterministic error (if possible)
        try:
            sorted_allowed = sorted(str(v) for v in allowed)
            allowed_str = f"[{', '.join(sorted_allowed)}]"
        except (TypeError, AttributeError):
            allowed_str = repr(allowed)
        
        raise GuardViolation(
            f"'{name}' has invalid value",
            context={
                "name": name,
                "allowed": allowed_str,
                "actual": repr(value)
            }
        )


# =============================================================================
# MUTABILITY ENFORCEMENT
# =============================================================================

def forbid_mutation(
    original: Any,
    current: Any,
    *,
    name: str
) -> None:
    """
    Detect unauthorized mutation.
    
    Args:
        original: Original value
        current: Current value
        name: Variable name for error reporting
    
    Raises:
        GuardViolation: If original != current
    
    Use For:
        - Immutability enforcement
        - Frozen object validation
        - Replay verification
    
    Example:
        >>> original_hash = hash_state(obj)
        >>> # ... operations ...
        >>> forbid_mutation(original_hash, hash_state(obj), name="state_hash")
    """
    if original != current:
        raise GuardViolation(
            f"Unauthorized mutation detected in '{name}'",
            context={
                "name": name,
                "original": repr(original),
                "current": repr(current)
            }
        )


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Error model
    "GuardViolation",
    
    # Condition enforcement
    "require",
    "forbid",
    
    # Type enforcement
    "require_type",
    "require_not_none",
    
    # Collection enforcement
    "require_non_empty",
    "require_length",
    
    # Unreachable code
    "unreachable",
    
    # Numeric enforcement
    "require_positive",
    "require_non_negative",
    
    # String enforcement
    "require_non_blank",
    
    # Comparison enforcement
    "require_equal",
    "require_in",
    
    # Mutability enforcement
    "forbid_mutation",
]

