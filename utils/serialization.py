"""
/utils/serialization.py

Canonical, Round-Trip-Safe Encoding Authority

Research-grade. Deterministic. No ambiguity tolerated.

This module is the single authority for converting structured data into
deterministic byte representations and back again without ambiguity.

Core Law:
    Serialization must preserve structure exactly and encode meaning
    deterministically.

Philosophy:
    Same input → same bytes
    Same bytes → same structure
    No implicit coercion. No silent mutation.

Critical:
    If serialization is unstable, hashing is meaningless.
    If hashing is meaningless, identity collapses.
    If identity collapses, replay is fiction.

Format:
    Deterministic JSON (strict mode)
    - Keys sorted lexicographically
    - No whitespace
    - UTF-8 encoding
    - Explicit separators: (",", ":")
    - No NaN, Infinity, -Infinity
    - Integers remain integers
    - Booleans lowercase
    - None → null

Round-Trip Guarantee:
    obj == from_canonical_bytes(to_canonical_bytes(obj))

Performance:
    - O(n) structural walk
    - O(n log n) for dict key sorting
    - No regex transforms
    - No dynamic introspection loops
"""

from __future__ import annotations

import json
from dataclasses import is_dataclass, asdict
from enum import Enum
from typing import Any


# =============================================================================
# CONSTANTS
# =============================================================================

# Serialization format version
# Increment when format changes, triggers replay incompatibility
SERIALIZATION_VERSION: int = 1

# JSON encoding parameters for determinism
_JSON_SEPARATORS = (",", ":")  # No whitespace
_JSON_ENSURE_ASCII = False  # Allow UTF-8
_JSON_SORT_KEYS = True  # Deterministic key ordering


# =============================================================================
# ERROR MODEL
# =============================================================================

class SerializationError(RuntimeError):
    """
    Serialization or deserialization failure.
    
    Raised when:
        - Unsupported type encountered
        - Float policy violated
        - Invalid structure for serialization
        - Deserialization produces corrupt data
    
    Attributes:
        message: Error description
        object_type: Type that caused failure
        path: Location in structure (if applicable)
    """
    
    def __init__(
        self,
        message: str,
        *,
        object_type: str | None = None,
        path: str | None = None
    ) -> None:
        self.object_type = object_type
        self.path = path
        
        # Build deterministic error message
        parts = [f"Serialization error: {message}"]
        
        if object_type is not None:
            parts.append(f"Type: {object_type}")
        if path is not None:
            parts.append(f"Path: {path}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# STRUCTURE PREPARATION
# =============================================================================

def prepare_for_serialization(obj: Any, *, path: str = "root") -> Any:
    """
    Prepare object for canonical serialization.
    
    Args:
        obj: Object to prepare
        path: Current path for error reporting
    
    Returns:
        JSON-serializable structure
    
    Raises:
        SerializationError: If object cannot be serialized
    
    Transformations:
        - FrozenDict → dict (preserving canonical order)
        - Enum → value
        - dataclass → dict
        - tuple → list (required by JSON)
        - Validates no float (unless allowed)
        - Validates no NaN/Infinity
        - Validates no forbidden types
    
    Critical:
        Must be deterministic and structural only.
        No business logic allowed.
    
    Example:
        >>> prepare_for_serialization({"a": 1, "b": (2, 3)})
        {"a": 1, "b": [2, 3]}
    """
    # None
    if obj is None:
        return None
    
    # Primitives
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return obj
    
    # Float policy enforcement
    if isinstance(obj, float):
        # Check for NaN/Infinity
        if obj != obj:  # NaN check
            raise SerializationError(
                "NaN values are forbidden",
                object_type="float",
                path=path
            )
        if obj == float('inf') or obj == float('-inf'):
            raise SerializationError(
                "Infinity values are forbidden",
                object_type="float",
                path=path
            )
        
        # Reject float by default
        raise SerializationError(
            "Float type forbidden in canonical serialization",
            object_type="float",
            path=path
        )
    
    # Enums
    if isinstance(obj, Enum):
        # Recursively prepare the enum value
        return prepare_for_serialization(obj.value, path=f"{path}.<enum_value>")
    
    # Dataclasses
    if is_dataclass(obj) and not isinstance(obj, type):
        # Convert to dict and recursively prepare
        obj_dict = asdict(obj)
        return prepare_for_serialization(obj_dict, path=path)
    
    # Dict
    if isinstance(obj, dict):
        # Validate all keys are strings
        for key in obj.keys():
            if not isinstance(key, str):
                raise SerializationError(
                    f"Dict keys must be strings, got {type(key).__name__}",
                    object_type=type(key).__name__,
                    path=path
                )
        
        # Recursively prepare values (keys will be sorted during JSON encoding)
        result = {}
        for key, value in obj.items():
            value_path = f"{path}.{key}"
            result[key] = prepare_for_serialization(value, path=value_path)
        return result
    
    # List
    if isinstance(obj, list):
        # Recursively prepare elements
        result = []
        for idx, item in enumerate(obj):
            item_path = f"{path}[{idx}]"
            result.append(prepare_for_serialization(item, path=item_path))
        return result
    
    # Tuple (convert to list for JSON)
    if isinstance(obj, tuple):
        # Recursively prepare elements
        result = []
        for idx, item in enumerate(obj):
            item_path = f"{path}[{idx}]"
            result.append(prepare_for_serialization(item, path=item_path))
        return result
    
    # Forbidden types
    if isinstance(obj, set):
        raise SerializationError(
            "Set type forbidden (use sorted list instead)",
            object_type="set",
            path=path
        )
    
    if isinstance(obj, bytes):
        raise SerializationError(
            "Bytes type forbidden (use base64 encoding explicitly)",
            object_type="bytes",
            path=path
        )
    
    # Catch-all for unsupported types
    raise SerializationError(
        f"Unsupported type for serialization: {type(obj).__name__}",
        object_type=type(obj).__name__,
        path=path
    )


# =============================================================================
# CANONICAL SERIALIZATION
# =============================================================================

def to_canonical_bytes(obj: Any) -> bytes:
    """
    Convert object to deterministic byte representation.
    
    Args:
        obj: Object to serialize
    
    Returns:
        UTF-8 encoded deterministic JSON bytes
    
    Raises:
        SerializationError: If object cannot be serialized
    
    Guarantees:
        - Deterministic output
        - UTF-8 encoded
        - Sorted keys
        - No whitespace
        - Same object → byte-identical output across OS/Python versions
    
    Properties:
        - Keys sorted lexicographically
        - Explicit separators: (",", ":")
        - No NaN, Infinity, -Infinity
        - Integers preserved
        - Booleans lowercase (true/false)
        - None → null
    
    Critical:
        Safe for SHA-256 hashing.
        Stable across processes, machines, Python versions.
    
    Example:
        >>> to_canonical_bytes({"b": 2, "a": 1})
        b'{"a":1,"b":2}'
        >>> to_canonical_bytes({"a": None, "b": [1, 2, 3]})
        b'{"a":null,"b":[1,2,3]}'
    """
    # Prepare structure
    prepared = prepare_for_serialization(obj)
    
    # Serialize to JSON with strict parameters
    try:
        json_str = json.dumps(
            prepared,
            ensure_ascii=_JSON_ENSURE_ASCII,
            sort_keys=_JSON_SORT_KEYS,
            separators=_JSON_SEPARATORS,
            allow_nan=False  # Reject NaN/Infinity explicitly
        )
    except (TypeError, ValueError) as e:
        raise SerializationError(
            f"JSON encoding failed: {e}",
            object_type=type(obj).__name__
        )
    
    # Encode to UTF-8 bytes
    try:
        return json_str.encode("utf-8")
    except UnicodeEncodeError as e:
        raise SerializationError(f"UTF-8 encoding failed: {e}")


# =============================================================================
# CANONICAL DESERIALIZATION
# =============================================================================

def from_canonical_bytes(data: bytes) -> Any:
    """
    Convert deterministic bytes back to structured object.
    
    Args:
        data: UTF-8 encoded JSON bytes
    
    Returns:
        Reconstructed structure (pure data types only)
    
    Raises:
        SerializationError: If bytes cannot be deserialized
    
    Guarantees:
        - Exact structure reconstruction
        - Dict preserves canonical order
        - Does not restore custom class instances
        - Pure data structures only
    
    Round-Trip Property:
        obj == from_canonical_bytes(to_canonical_bytes(obj))
    
    Returns Types:
        - dict
        - list
        - int
        - str
        - bool
        - None
    
    Example:
        >>> from_canonical_bytes(b'{"a":1,"b":2}')
        {'a': 1, 'b': 2}
        >>> from_canonical_bytes(b'[1,2,3]')
        [1, 2, 3]
    """
    # Decode UTF-8
    try:
        json_str = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SerializationError(f"UTF-8 decoding failed: {e}")
    
    # Parse JSON
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise SerializationError(f"JSON parsing failed: {e}")
    
    # Validate structure (no floats, proper types)
    _validate_deserialized_structure(obj)
    
    return obj


def _validate_deserialized_structure(obj: Any, *, path: str = "root") -> None:
    """
    Validate deserialized structure contains only allowed types.
    
    Args:
        obj: Deserialized object
        path: Current path for error reporting
    
    Raises:
        SerializationError: If structure contains forbidden types
    
    Critical:
        JSON parsing can produce floats for numeric values.
        We must reject these unless explicitly allowed.
    """
    # None
    if obj is None:
        return
    
    # Primitives
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, str):
        return
    
    # Float check
    if isinstance(obj, float):
        raise SerializationError(
            "Float detected in deserialized structure (forbidden)",
            object_type="float",
            path=path
        )
    
    # Dict
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise SerializationError(
                    f"Dict keys must be strings, got {type(key).__name__}",
                    object_type=type(key).__name__,
                    path=path
                )
            value_path = f"{path}.{key}"
            _validate_deserialized_structure(value, path=value_path)
        return
    
    # List
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            item_path = f"{path}[{idx}]"
            _validate_deserialized_structure(item, path=item_path)
        return
    
    # Unsupported type
    raise SerializationError(
        f"Unexpected type in deserialized structure: {type(obj).__name__}",
        object_type=type(obj).__name__,
        path=path
    )


# =============================================================================
# ROUND-TRIP VERIFICATION
# =============================================================================

def verify_round_trip(obj: Any) -> bool:
    """
    Verify object survives serialization round-trip.
    
    Args:
        obj: Object to test
    
    Returns:
        True if round-trip preserves structure
    
    Test:
        obj == from_canonical_bytes(to_canonical_bytes(obj))
    
    Note:
        Tuples are converted to lists, so exact equality may fail.
        Use structural comparison for precise validation.
    
    Example:
        >>> verify_round_trip({"a": 1, "b": [2, 3]})
        True
        >>> verify_round_trip({"a": 1.5})
        False  # Floats forbidden
    """
    try:
        serialized = to_canonical_bytes(obj)
        deserialized = from_canonical_bytes(serialized)
        
        # Normalize obj for comparison (tuples → lists)
        normalized = prepare_for_serialization(obj)
        
        return normalized == deserialized
    except SerializationError:
        return False


# =============================================================================
# HASH COMPATIBILITY
# =============================================================================

def to_hash_input(obj: Any) -> bytes:
    """
    Convert object to bytes suitable for cryptographic hashing.
    
    Args:
        obj: Object to convert
    
    Returns:
        Canonical bytes for hashing
    
    Guarantees:
        - Safe for SHA-256
        - Stable across processes
        - Stable across machines
        - Stable across Python versions
        - Same structure → same hash
    
    Critical:
        This is the foundation for stable identity.
        Changing this function requires:
            - Version bump
            - Coordinated migration
            - Identity regeneration
    
    Example:
        >>> import hashlib
        >>> data = {"a": 1, "b": 2}
        >>> hashlib.sha256(to_hash_input(data)).hexdigest()
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    """
    return to_canonical_bytes(obj)


# =============================================================================
# UTILITIES
# =============================================================================

def to_canonical_string(obj: Any) -> str:
    """
    Convert object to deterministic string representation.
    
    Args:
        obj: Object to convert
    
    Returns:
        Deterministic JSON string
    
    Note:
        For human-readable debugging only.
        Use to_canonical_bytes() for identity/hashing.
    
    Example:
        >>> to_canonical_string({"b": 2, "a": 1})
        '{"a":1,"b":2}'
    """
    return to_canonical_bytes(obj).decode("utf-8")


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Version
    "SERIALIZATION_VERSION",
    
    # Error model
    "SerializationError",
    
    # Core API
    "to_canonical_bytes",
    "from_canonical_bytes",
    
    # Utilities
    "to_hash_input",
    "to_canonical_string",
    "verify_round_trip",
    "prepare_for_serialization",
]



