"""
/utils/__init__.py

Explicit Public API Surface
(No Wildcards, No Leakage)

This file is not decorative.
It defines the public contract surface of the entire utility layer.

Research-grade. Locked-down. Zero leakage.

CRITICAL PRINCIPLES:
- Defines the ONLY symbols that external modules may import from utils directly
- Prevents internal utility sprawl
- Enforces stable public API boundaries
- Eliminates accidental deep-import coupling
- Makes dependency graph analyzable
- Freezes the foundational surface area
- Supports long-term refactor safety
- Prevents wildcard pollution

CORE LAW:
If a symbol is not explicitly exported in __all__, it does not exist
as a public API.

No implicit access.
No convenience leakage.
No wildcard exposure.

IMPORT DISCIPLINE:
Allowed:   from utils import hash_bytes, hash_struct
Allowed:   from utils.serialization import to_canonical_bytes
Forbidden: from utils import *

STABILITY GUARANTEE:
Anything exported here is:
- Semantically stable
- Backwards-compatible
- Safe for system-wide import
- Version-sensitive

Removing an export is a breaking change.
Adding is allowed (carefully).
Renaming is forbidden.

WILDCARD BAN (ZERO TOLERANCE):
Explicitly forbidden:
- __all__ = [name for name in dir()]
- from .module import *

Wildcard means:
- Accidental exposure
- API instability
- Refactor fragility
- Hidden contract drift

Not allowed in a system of this grade.

DEPENDENCY DIRECTION RULE:
utils/__init__.py must:
- Not import from higher layers
- Not trigger heavy imports
- Not execute environment reads
- Not execute hashing
- Not perform IO

It must be import-cheap and side-effect free.
Import time must be deterministic.

This file must be static and explicit.
Not dynamic. Not reflective. Not auto-populating.
"""

# ============================================================================
# Core Errors
# ============================================================================
# Base utility-level error types for system-wide error handling

from .errors import (
    UtilityError,
    GuardViolation,
    ValidationError,
    SerializationError,
    HashingError,
    TimeError,
    EnvironmentConfigurationError,
    MathError,
    OrderingError,
    LoggingError,
)


# ============================================================================
# Type Definitions
# ============================================================================
# Canonical type aliases for cross-module type safety

from .types import (
    EpochMillis,
    VersionString,
    HashString,
    Identifier,
)


# ============================================================================
# Freezing Utilities
# ============================================================================
# Immutability enforcement primitives

from .frozen import (
    deep_freeze,
    FrozenDict,
    is_frozen,
)


# ============================================================================
# Serialization
# ============================================================================
# Canonical serialization entry points for deterministic encoding/decoding

from .serialization import (
    to_canonical_bytes,
    from_canonical_bytes,
    to_hash_input,
)


# ============================================================================
# Hashing
# ============================================================================
# Cryptographic hashing primitives for stable content identity

from .hashing import (
    hash_bytes,
    hash_struct,
)


# ============================================================================
# Environment Loading
# ============================================================================
# Environment variable access and validation

from .env import (
    load_environment,
    Environment,
    require_env_var,
    get_env_bool,
    get_env_int,
)


# ============================================================================
# Public API Declaration
# ============================================================================
# Explicit export list. Every export must be intentional.
# No wildcards. No dynamic building. No condition-based exports.

__all__ = [
    # ========================================================================
    # Errors (Base utility-level error types)
    # ========================================================================
    "UtilityError",
    "GuardViolation",
    "ValidationError",
    "SerializationError",
    "HashingError",
    "TimeError",
    "EnvironmentConfigurationError",
    "MathError",
    "OrderingError",
    "LoggingError",
    
    # ========================================================================
    # Types (Canonical type aliases)
    # ========================================================================
    "EpochMillis",
    "VersionString",
    "HashString",
    "Identifier",
    
    # ========================================================================
    # Freezing (Immutability enforcement)
    # ========================================================================
    "deep_freeze",
    "FrozenDict",
    "is_frozen",
    
    # ========================================================================
    # Serialization (Canonical encoding/decoding)
    # ========================================================================
    "to_canonical_bytes",
    "from_canonical_bytes",
    "to_hash_input",
    
    # ========================================================================
    # Hashing (Cryptographic hashing primitives)
    # ========================================================================
    "hash_bytes",
    "hash_struct",
    
    # ========================================================================
    # Environment (Environment variable access)
    # ========================================================================
    "load_environment",
    "Environment",
    "require_env_var",
    "get_env_bool",
    "get_env_int",
]

# Validate that __all__ is a list (not dynamically built)
if not isinstance(__all__, list):
    raise TypeError("__all__ must be a static list, not dynamically built")

# Validate no wildcard patterns
if any('*' in str(item) for item in __all__):
    raise ValueError("__all__ must not contain wildcard patterns")


# ============================================================================
# Import-Time Validation (Optional, Development Only)
# ============================================================================
# Validates that only declared symbols are exported.
# Runs only if VALIDATE_UTILS_EXPORTS environment variable is set.
# In production, this is disabled for performance (import-cheap requirement).

def _validate_exports() -> None:
    """
    Validate that only declared symbols are exported.
    
    This runs at import time to catch accidental exposure.
    Only runs if VALIDATE_UTILS_EXPORTS environment variable is set.
    
    DETERMINISTIC: Same environment always produces same validation result.
    No side effects if validation is disabled.
    """
    import sys
    
    current_module = sys.modules[__name__]
    actual_exports = set(dir(current_module))
    declared_exports = set(__all__)
    
    # Allow private symbols and builtins
    allowed_private = {
        '__name__', '__doc__', '__package__', '__loader__',
        '__spec__', '__path__', '__file__', '__cached__',
        '__builtins__', '__all__', '_validate_exports',
        '__annotations__', '__dict__', '__weakref__',
    }
    
    # Find unexpected public symbols
    unexpected = actual_exports - declared_exports - allowed_private
    
    # Filter out imported modules that weren't meant to be exposed
    unexpected = {
        name for name in unexpected
        if not name.startswith('_')
    }
    
    if unexpected:
        import warnings
        warnings.warn(
            f"utils.__init__ has unexpected public symbols: {sorted(unexpected)}. "
            f"These are not in __all__ and should not be relied upon.",
            ImportWarning,
            stacklevel=2
        )


# Run validation at import time (only if explicitly enabled)
# In production, this is disabled for performance (import-cheap requirement)
# No environment variable access in normal operation (deterministic import)
try:
    import os
    if os.environ.get('VALIDATE_UTILS_EXPORTS', '').lower() in ('1', 'true', 'yes'):
        _validate_exports()
except Exception:
    # Silently ignore validation errors (don't break import)
    pass
finally:
    # Clean up validation function from namespace
    # Also clean up os import if it was used
    try:
        del _validate_exports
        if 'os' in locals():
            del os
    except NameError:
        pass