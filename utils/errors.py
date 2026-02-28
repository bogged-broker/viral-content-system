"""
/utils/errors.py

Base Utility-Level Error Types

This is not a random exception zoo.
This file defines the foundational error taxonomy for the utility layer — and nothing above it.

Research-grade. Minimal. Deterministic. Zero semantic leakage.

WHAT THIS FILE EXISTS FOR (NON-NEGOTIABLE):
errors.py defines the root exception hierarchy for the utils/ layer.

It exists to:
- Eliminate arbitrary ValueError / TypeError usage
- Prevent exception inconsistency across foundational modules
- Provide structured, deterministic failure semantics
- Support cross-layer invariant enforcement
- Enable precise catching without broad exception swallowing
- Guarantee stable, reproducible error formatting

It answers:
> "When the foundational layer fails, under what exact category did it fail?"

If utility errors are inconsistent:
- Higher layers misclassify failures
- Replay becomes ambiguous
- Audit trails fragment
- Recovery logic becomes guesswork

CORE LAW:
All utility-layer failures must descend from a single, sealed base class.

No naked built-ins in utils.
If an error originates from utils/, it must derive from UtilityError.

WHAT THIS FILE IS NOT:
- Not domain errors
- Not ingestion errors
- Not aggregation errors
- Not computation errors
- Not persistence errors
- Not replay errors
- Not business-level validation errors
- Not recovery-layer failures

This file defines only primitive infrastructure errors.

ERROR HIERARCHY:
Exception
└── UtilityError
    ├── GuardViolation
    ├── ValidationError
    ├── SerializationError
    ├── HashingError
    ├── TimeError
    ├── EnvironmentConfigurationError
    ├── MathError
    ├── OrderingError
    └── LoggingError

Flat. Explicit. No inheritance pyramids.

STABILITY:
- Error class names must remain stable once published
- Renaming breaks audit logs, replay classification, observability
- Additive changes allowed, renames forbidden
- Error messages must be deterministic and replay-safe

STRICT PROHIBITIONS:
This file MUST NOT:
- Import higher-level modules
- Define domain-specific exceptions
- Import from infra/
- Import from data/
- Import Decimal
- Import os
- Log anything
- Perform side effects

This layer must remain dependency-root.
"""

from typing import Dict, Optional
from types import MappingProxyType


# ============================================================================
# Base Utility Error
# ============================================================================


class UtilityError(RuntimeError):
    """
    Base class for all utility-level failures.
    
    All errors originating from the utils/ layer must inherit from this class.
    
    Properties:
        - Inherits from RuntimeError (not Exception)
        - Must not auto-log
        - Must preserve deterministic message
        - Must not capture large context implicitly
        - Must support stable __str__ and __repr__
    
    Attributes:
        message: Error message (deterministic, replay-safe)
        code: Optional stable error identifier
        details: Optional structured context (small dict only, immutable)
    
    Error Message Law:
        All UtilityError-derived exceptions must:
        - Have stable, deterministic messages
        - Avoid including memory addresses
        - Avoid dumping massive structures
        - Avoid implicit repr of custom objects
        - Avoid machine-dependent formatting
    
    Error messages must be replay-safe.
    
    Metadata Strategy (Minimal):
        Utility errors may optionally include:
        - code: str (stable identifier)
        - details: dict[str, str] (small structured context)
        
        But:
        - Never include raw object payloads
        - Never include huge dicts
        - Never include stack trace in message body
    
    Stack trace is implicit via exception mechanism.
    """
    
    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        details: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize utility error.
        
        Args:
            message: Error message (deterministic, replay-safe)
            code: Optional stable error code
            details: Optional small structured context (max 10 keys, string values only)
        
        Raises:
            ValueError: If details dict is too large or contains non-string values
        """
        if not isinstance(message, str):
            raise TypeError(f"message must be str, got {type(message).__name__}")
        
        if not message.strip():
            raise ValueError("message cannot be empty or whitespace-only")
        
        if code is not None and not isinstance(code, str):
            raise TypeError(f"code must be str or None, got {type(code).__name__}")
        
        if code is not None and not code.strip():
            raise ValueError("code cannot be empty or whitespace-only")
        
        # Validate details dict
        if details is not None:
            if not isinstance(details, dict):
                raise TypeError(f"details must be dict or None, got {type(details).__name__}")
            
            # Enforce size limit (prevent large context capture)
            if len(details) > 10:
                raise ValueError(f"details dict cannot exceed 10 keys, got {len(details)}")
            
            # Validate all values are strings (prevent object dumps)
            for key, value in details.items():
                if not isinstance(key, str):
                    raise TypeError(f"details keys must be str, got {type(key).__name__}")
                if not isinstance(value, str):
                    raise TypeError(
                        f"details values must be str, got {type(value).__name__} for key '{key}'"
                    )
                # Prevent large string values (prevent massive dumps)
                if len(value) > 200:
                    raise ValueError(
                        f"details value for key '{key}' exceeds 200 chars (got {len(value)})"
                    )
            
            # Make details immutable to prevent mutation
            details = MappingProxyType(details)
        
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or MappingProxyType({})
    
    def __str__(self) -> str:
        """
        Deterministic string representation.
        
        Returns stable, replay-safe error message.
        No memory addresses, no large dumps, no machine-dependent formatting.
        """
        parts = [self.message]
        
        if self.code:
            parts.append(f"[code: {self.code}]")
        
        if self.details:
            # Sort details for deterministic ordering
            detail_str = ", ".join(
                f"{k}={v}" for k, v in sorted(self.details.items())
            )
            parts.append(f"({detail_str})")
        
        return " ".join(parts)
    
    def __repr__(self) -> str:
        """
        Deterministic representation for debugging.
        
        Returns stable, replay-safe representation.
        No memory addresses, no object dumps.
        """
        class_name = self.__class__.__name__
        parts = [f"{class_name}("]
        
        # Always include message
        parts.append(f"message={self.message!r}")
        
        if self.code:
            parts.append(f", code={self.code!r}")
        
        if self.details:
            # Sort for deterministic ordering
            detail_repr = ", ".join(
                f"{k!r}: {v!r}" for k, v in sorted(self.details.items())
            )
            parts.append(f", details={{{detail_repr}}}")
        
        parts.append(")")
        return "".join(parts)


# ============================================================================
# Guard Violations
# ============================================================================


class GuardViolation(UtilityError):
    """
    Raised when invariant or contract violation detected.
    
    Used by: /utils/guards.py
    
    Used for:
        - Invariant failures (impossible states)
        - Contract violations (precondition/postcondition)
        - Internal consistency checks
    
    Must never represent:
        - User mistakes
        - External input validation
    
    Always programmer or structural fault.
    
    Examples:
        - Frozen object mutation attempt
        - Negative value where positive required
        - Non-monotonic sequence detected
    """
    pass


# ============================================================================
# Validation Errors
# ============================================================================


class ValidationError(UtilityError):
    """
    Raised when structural validation fails.
    
    Used by: /utils/validation.py
    
    Scope:
        - Structural shape mismatch
        - Type mismatch at shared primitive level
    
    Not business-level schema enforcement.
    
    Examples:
        - Expected dict, got list
        - Missing required field
        - Invalid type for field
    """
    pass


class InvalidInputError(ValidationError):
    """
    Raised when input validation fails at utility level.
    
    Used by: /utils/hashing.py and other utility modules
    
    Used for:
        - Invalid type passed to utility function
        - Invalid value (e.g., empty string where non-empty required)
        - Invalid parameter combinations
    
    Examples:
        - hash_bytes() called with non-bytes input
        - Empty string passed where non-empty required
        - Negative value where positive required
    """
    pass


# ============================================================================
# Serialization Errors
# ============================================================================


class SerializationError(UtilityError):
    """
    Raised when canonical serialization fails.
    
    Used by: /utils/serialization.py
    
    Used when:
        - Canonical encoding fails
        - Non-serializable types appear
        - Deterministic encoding contract violated
        - Round-trip inconsistency detected
    
    Must not include raw object dump in error message.
    
    Examples:
        - Cannot serialize custom class
        - Circular reference detected
        - Non-deterministic dict ordering
    """
    pass


# ============================================================================
# Hashing Errors
# ============================================================================


class HashingError(UtilityError):
    """
    Raised when stable hashing fails.
    
    Used by: /utils/hashing.py
    
    Used when:
        - Non-deterministic hash attempt
        - Non-canonical input detected
        - Unsupported type passed to hash layer
    
    Hash errors indicate identity instability.
    
    Examples:
        - Cannot hash unhashable type
        - Input not canonically ordered
        - Hash collision detected
    """
    pass


# ============================================================================
# Time Errors
# ============================================================================


class TimeError(UtilityError):
    """
    Raised when time handling fails.
    
    Used by: /utils/time.py
    
    Used for:
        - Invalid timestamp normalization
        - Negative epoch misuse
        - Non-UTC inputs
        - Monotonic violations (if detected at utility level)
    
    Not for window policy failures — those belong higher.
    
    Examples:
        - Negative timestamp where positive required
        - Timezone-aware timestamp in UTC-only context
        - Time went backwards
    """
    pass


# ============================================================================
# Environment Errors
# ============================================================================


class EnvironmentConfigurationError(UtilityError):
    """
    Raised when environment configuration fails.
    
    Note: Named to avoid collision with Python's built-in EnvironmentError
    (deprecated alias for OSError).
    
    Used by: /utils/env.py
    
    Used for:
        - Missing environment variables
        - Invalid type parsing
        - Frozen environment mutation attempts
    
    Examples:
        - Required env var ENV_NAME not set
        - Cannot parse 'DEBUG=invalid' as boolean
        - Attempt to modify frozen environment
    """
    pass


# ============================================================================
# Math Errors
# ============================================================================


class MathError(UtilityError):
    """
    Raised when mathematical operation fails.
    
    Used by: /utils/math.py
    
    Used for:
        - Float contamination (if disallowed)
        - Non-exact integer division
        - Division by zero (if not using built-in ZeroDivisionError)
        - Invalid Decimal conversion
    
    Never use ArithmeticError directly.
    
    Examples:
        - Float used where exact integer required
        - Division would lose precision
        - Overflow detected
    """
    pass


# ============================================================================
# Ordering Errors
# ============================================================================


class OrderingError(UtilityError):
    """
    Raised when ordering or comparison fails.
    
    Used by: /utils/ordering.py
    
    Used for:
        - Non-total order detection
        - Comparison ambiguity
        - Invalid comparator contract
    
    Examples:
        - Cannot compare types with no defined ordering
        - Comparator violates transitivity
        - Sort key function returns incomparable values
    """
    pass


# ============================================================================
# Logging Errors
# ============================================================================


class LoggingError(UtilityError):
    """
    Raised when structured logging fails.
    
    Used by: /utils/logging.py
    
    Used for:
        - Non-serializable log payloads
        - Mutation attempt in structured logging
        - Invalid log level definitions
        - Schema violations in structured event
    
    Logging must never silently drop invalid structures.
    
    Examples:
        - Cannot serialize log payload
        - Invalid log level 'CUSTOM'
        - Required log field missing
    """
    pass


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    # Base
    "UtilityError",
    
    # Specific Errors (alphabetical)
    "EnvironmentConfigurationError",
    "GuardViolation",
    "HashingError",
    "InvalidInputError",
    "LoggingError",
    "MathError",
    "OrderingError",
    "SerializationError",
    "TimeError",
    "ValidationError",
]

# ============================================================================
# Import-Time Validation
# ============================================================================
# Ensure no accidental shadowing of built-in exception classes

_BUILTIN_EXCEPTIONS = {
    'Exception', 'BaseException', 'RuntimeError', 'ValueError', 'TypeError',
    'KeyError', 'IndexError', 'AttributeError', 'LookupError', 'OSError',
    'IOError', 'EnvironmentError',  # EnvironmentError is deprecated but still exists
}

# Check for accidental shadowing
for name in __all__:
    if name in _BUILTIN_EXCEPTIONS:
        raise RuntimeError(
            f"Error class '{name}' shadows built-in exception. "
            f"Rename to avoid collision."
        )

# Validate all exported classes inherit from UtilityError
for name in __all__:
    if name == "UtilityError":
        continue
    
    cls = globals().get(name)
    if cls is None:
        raise RuntimeError(f"Exported class '{name}' not found in module")
    
    if not issubclass(cls, UtilityError):
        raise RuntimeError(
            f"Exported class '{name}' must inherit from UtilityError, "
            f"got {cls.__bases__}"
        )

# Clean up validation helpers
del _BUILTIN_EXCEPTIONS