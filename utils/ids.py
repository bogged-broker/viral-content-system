"""
/utils/ids.py

Stable Identity Generation (Non-Random)

Research-grade. Deterministic. No shortcuts.

This module is the single authority for generating stable, deterministic
identifiers in the system. It eliminates random UUIDs, time-based IDs,
and environment-dependent identity generation.

Core Law:
    Identity must be derived from meaning — not from time, randomness,
    or environment.

Philosophy:
    IDs are content-derived or namespace-derived. Nothing else is allowed.
    If two things mean the same thing, they get the same ID — forever.

Critical:
    If IDs are unstable, replay diverges.
    If IDs depend on time, replay becomes impossible.
    If IDs are random, correctness becomes probabilistic.

Identity Sources (ONLY THREE ALLOWED):
    1. Content-derived identity (hash of canonical structure)
    2. Namespace-derived identity (stable hierarchical derivation)
    3. Composite identity (explicit multi-field encoding)

Forbidden:
    - uuid4()
    - random module
    - time-based UUIDs
    - process IDs
    - hostnames
    - MAC addresses
    - auto-increment counters
    - hashing without canonicalization
    - object memory addresses

Determinism Guarantee:
    Given identical input, namespace, and version:
        - Same ID across machines
        - Same ID across executions
        - Same ID across Python versions
        - Same ID across replay
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict


# =============================================================================
# CONSTANTS
# =============================================================================

# Hash algorithm version - increment when changing algorithm
HASH_VERSION: str = "v1"

# Hash output format
HASH_ENCODING: str = "hex"  # lowercase hex

# Expected hash length (SHA-256 in hex)
EXPECTED_HASH_LENGTH: int = 64


# =============================================================================
# ERROR MODEL
# =============================================================================

class IdentityDerivationError(RuntimeError):
    """
    Identity derivation failure.
    
    Raised when:
        - Invalid namespace
        - Invalid version
        - Canonicalization failure
        - Hash computation failure
        - Invalid ID component
    
    Attributes:
        message: Error description
        namespace: Namespace that caused failure
        version: Version that caused failure
        input_type: Type of input that failed
    """
    
    def __init__(
        self,
        message: str,
        *,
        namespace: str | None = None,
        version: str | None = None,
        input_type: str | None = None
    ) -> None:
        self.namespace = namespace
        self.version = version
        self.input_type = input_type
        
        # Build deterministic error message
        parts = [f"Identity derivation error: {message}"]
        
        if namespace is not None:
            parts.append(f"Namespace: {namespace}")
        if version is not None:
            parts.append(f"Version: {version}")
        if input_type is not None:
            parts.append(f"Input type: {input_type}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# VALIDATION
# =============================================================================

def _validate_namespace(namespace: str) -> None:
    """
    Validate namespace string.
    
    Args:
        namespace: Namespace to validate
    
    Raises:
        IdentityDerivationError: If namespace is invalid
    
    Rules:
        - Must be non-empty string
        - Must be lowercase
        - Must be ASCII-safe
        - No whitespace
        - No special characters except underscore/hyphen
    """
    if not isinstance(namespace, str):
        raise IdentityDerivationError(
            "Namespace must be string",
            namespace=str(namespace),
            input_type=type(namespace).__name__
        )
    
    if not namespace:
        raise IdentityDerivationError(
            "Namespace must not be empty",
            namespace=namespace
        )
    
    if namespace != namespace.lower():
        raise IdentityDerivationError(
            "Namespace must be lowercase",
            namespace=namespace
        )
    
    # Check for valid characters (lowercase letters, digits, underscore, hyphen)
    if not all(c.isalnum() or c in ('_', '-') for c in namespace):
        raise IdentityDerivationError(
            "Namespace contains invalid characters (only a-z, 0-9, _, - allowed)",
            namespace=namespace
        )
    
    # No whitespace
    if ' ' in namespace or '\t' in namespace or '\n' in namespace:
        raise IdentityDerivationError(
            "Namespace must not contain whitespace",
            namespace=namespace
        )


def _validate_version(version: str) -> None:
    """
    Validate version string.
    
    Args:
        version: Version to validate
    
    Raises:
        IdentityDerivationError: If version is invalid
    
    Rules:
        - Must be non-empty string
        - Must be ASCII-safe
        - No whitespace
    """
    if not isinstance(version, str):
        raise IdentityDerivationError(
            "Version must be string",
            version=str(version),
            input_type=type(version).__name__
        )
    
    if not version:
        raise IdentityDerivationError(
            "Version must not be empty",
            version=version
        )
    
    # No whitespace
    if ' ' in version or '\t' in version or '\n' in version:
        raise IdentityDerivationError(
            "Version must not contain whitespace",
            version=version
        )


def _validate_id_component(component: str, *, name: str) -> None:
    """
    Validate individual ID component.
    
    Args:
        component: Component to validate
        name: Component name for error reporting
    
    Raises:
        IdentityDerivationError: If component is invalid
    
    Rules:
        - Must be non-empty string
        - No whitespace
        - No delimiter characters (:, |, /)
    """
    if not isinstance(component, str):
        raise IdentityDerivationError(
            f"ID component '{name}' must be string",
            input_type=type(component).__name__
        )
    
    if not component:
        raise IdentityDerivationError(
            f"ID component '{name}' must not be empty"
        )
    
    # No whitespace
    if any(c in component for c in (' ', '\t', '\n')):
        raise IdentityDerivationError(
            f"ID component '{name}' must not contain whitespace"
        )
    
    # No delimiter characters (these would break composite IDs)
    if any(c in component for c in (':', '|', '/')):
        raise IdentityDerivationError(
            f"ID component '{name}' must not contain delimiter characters (:, |, /)"
        )


# =============================================================================
# HASHING PRIMITIVES
# =============================================================================

def _compute_hash(data: bytes) -> str:
    """
    Compute SHA-256 hash of bytes.
    
    Args:
        data: Bytes to hash
    
    Returns:
        Lowercase hex digest (64 characters)
    
    Guarantees:
        - Deterministic output
        - Collision-resistant
        - Stable across platforms
        - Fixed length (64 chars)
    
    Critical:
        Uses SHA-256 for cryptographic collision resistance.
        Do not truncate unless absolutely necessary and risk is documented.
    """
    hasher = hashlib.sha256()
    hasher.update(data)
    return hasher.hexdigest().lower()


def _build_hash_input(
    *,
    namespace: str,
    version: str,
    content_bytes: bytes
) -> bytes:
    """
    Build deterministic hash input from components.
    
    Args:
        namespace: Namespace string
        version: Version string
        content_bytes: Canonical content bytes
    
    Returns:
        Deterministic bytes for hashing
    
    Format:
        namespace:{namespace}||version:{version}||{content_bytes}
    
    Critical:
        Format is part of identity contract.
        Changing format requires version bump.
    """
    # Build prefix with explicit delimiters
    prefix = f"namespace:{namespace}||version:{version}||".encode("utf-8")
    
    # Concatenate
    return prefix + content_bytes


# =============================================================================
# CONTENT-DERIVED IDENTITY
# =============================================================================

def derive_id(
    obj: Any,
    *,
    namespace: str,
    version: str
) -> str:
    """
    Derive deterministic ID from canonical content.
    
    Args:
        obj: Object to derive ID from (must be serializable)
        namespace: Identity namespace
        version: Identity version
    
    Returns:
        Deterministic ID string (format: {namespace}:{version}:{hash})
    
    Raises:
        IdentityDerivationError: If derivation fails
    
    Guarantees:
        - Deterministic output
        - Collision-resistant
        - Stable across machines/executions/versions
        - Same input → same ID always
    
    Use Cases:
        - Canonical content IDs
        - Window IDs
        - Computation IDs
        - Aggregation identities
        - Replay scope identifiers
    
    Mechanism:
        1. Validate namespace and version
        2. Serialize object to canonical bytes
        3. Build hash input with namespace/version prefix
        4. Compute SHA-256 hash
        5. Format as {namespace}:{version}:{hash}
    
    Example:
        >>> derive_id({"a": 1, "b": 2}, namespace="content", version="v1")
        'content:v1:85c9f7e4...'  # 64-char hash
    """
    # Validate inputs
    _validate_namespace(namespace)
    _validate_version(version)
    
    # Import here to avoid circular dependency
    from utils.serialization import to_canonical_bytes
    
    # Serialize to canonical bytes
    try:
        content_bytes = to_canonical_bytes(obj)
    except Exception as e:
        raise IdentityDerivationError(
            f"Failed to canonicalize object for ID derivation: {e}",
            namespace=namespace,
            version=version,
            input_type=type(obj).__name__
        )
    
    # Build hash input
    hash_input = _build_hash_input(
        namespace=namespace,
        version=version,
        content_bytes=content_bytes
    )
    
    # Compute hash
    hash_digest = _compute_hash(hash_input)
    
    # Format ID
    return f"{namespace}:{version}:{hash_digest}"


# =============================================================================
# NAMESPACE-DERIVED CHILD IDENTITY
# =============================================================================

def derive_child_id(
    parent_id: str,
    *,
    child_namespace: str,
    child_key: str,
    version: str
) -> str:
    """
    Derive deterministic child ID from parent ID.
    
    Args:
        parent_id: Parent identity
        child_namespace: Child namespace
        child_key: Child-specific key
        version: Identity version
    
    Returns:
        Deterministic child ID string
    
    Raises:
        IdentityDerivationError: If derivation fails
    
    Guarantees:
        - Deterministic output
        - Namespace isolation
        - Parent-child relationship preserved
        - Same inputs → same child ID
    
    Use Cases:
        - Window → sub-window relationships
        - Computation → output artifact
        - Hierarchical resource derivation
    
    Mechanism:
        Hash of: parent_id || child_namespace || child_key || version
    
    Example:
        >>> derive_child_id(
        ...     "window:v1:abc123...",
        ...     child_namespace="subwindow",
        ...     child_key="0-100",
        ...     version="v1"
        ... )
        'subwindow:v1:def456...'
    """
    # Validate inputs
    _validate_id_component(parent_id, name="parent_id")
    _validate_namespace(child_namespace)
    _validate_id_component(child_key, name="child_key")
    _validate_version(version)
    
    # Build hash input
    hash_input_str = (
        f"parent:{parent_id}||"
        f"child_namespace:{child_namespace}||"
        f"child_key:{child_key}||"
        f"version:{version}"
    )
    hash_input_bytes = hash_input_str.encode("utf-8")
    
    # Compute hash
    hash_digest = _compute_hash(hash_input_bytes)
    
    # Format ID
    return f"{child_namespace}:{version}:{hash_digest}"


# =============================================================================
# COMPOSITE IDENTITY
# =============================================================================

def compose_id(
    *parts: str,
    namespace: str
) -> str:
    """
    Compose explicit multi-part ID with delimiter safety.
    
    Args:
        *parts: ID components (must be non-empty, no delimiters)
        namespace: Identity namespace
    
    Returns:
        Composite ID string (format: {namespace}:{part1}:{part2}:...)
    
    Raises:
        IdentityDerivationError: If any part is invalid
    
    Guarantees:
        - Delimiter safety
        - No empty components
        - Namespace prefix
        - Deterministic format
    
    Use Cases:
        - Structural encoding explicitly defined
        - All parts already canonical
        - No structural hashing needed
    
    Critical:
        Parts must not contain delimiter characters (:, |, /).
        Parts must be validated before composition.
    
    Example:
        >>> compose_id("user_123", "session_456", namespace="auth")
        'auth:user_123:session_456'
    """
    # Validate namespace
    _validate_namespace(namespace)
    
    # Validate all parts
    if not parts:
        raise IdentityDerivationError(
            "compose_id requires at least one part",
            namespace=namespace
        )
    
    for idx, part in enumerate(parts):
        _validate_id_component(part, name=f"part[{idx}]")
    
    # Compose ID
    return f"{namespace}:{':'.join(parts)}"


# =============================================================================
# ID PARSING
# =============================================================================

def parse_id(id_string: str) -> dict[str, str]:
    """
    Parse ID string into components.
    
    Args:
        id_string: ID string to parse
    
    Returns:
        Dictionary with 'namespace', 'version', and 'hash' (or 'parts')
    
    Raises:
        IdentityDerivationError: If ID format is invalid
    
    Example:
        >>> parse_id("content:v1:abc123...")
        {'namespace': 'content', 'version': 'v1', 'hash': 'abc123...'}
        >>> parse_id("auth:user_123:session_456")
        {'namespace': 'auth', 'parts': ['user_123', 'session_456']}
    """
    if not isinstance(id_string, str):
        raise IdentityDerivationError(
            "ID must be string",
            input_type=type(id_string).__name__
        )
    
    if not id_string:
        raise IdentityDerivationError("ID must not be empty")
    
    # Split on delimiter
    components = id_string.split(':')
    
    if len(components) < 2:
        raise IdentityDerivationError(
            f"Invalid ID format (expected namespace:... format): {id_string}"
        )
    
    namespace = components[0]
    _validate_namespace(namespace)
    
    # Check if this is a hashed ID (namespace:version:hash)
    if len(components) == 3 and len(components[2]) == EXPECTED_HASH_LENGTH:
        version = components[1]
        hash_value = components[2]
        _validate_version(version)
        
        return {
            'namespace': namespace,
            'version': version,
            'hash': hash_value
        }
    
    # Otherwise, treat as composite ID
    parts = components[1:]
    for idx, part in enumerate(parts):
        _validate_id_component(part, name=f"part[{idx}]")
    
    return {
        'namespace': namespace,
        'parts': parts
    }


# =============================================================================
# COLLISION DETECTION
# =============================================================================

def detect_collision(id1: str, id2: str, obj1: Any, obj2: Any) -> bool:
    """
    Detect hash collision (extremely unlikely but checked for correctness).
    
    Args:
        id1: First ID
        id2: Second ID
        obj1: First object
        obj2: Second object
    
    Returns:
        True if collision detected, False otherwise
    
    Critical:
        If collision detected, system must raise fatal invariant violation.
        No runtime collision resolution allowed.
    
    Use Case:
        Testing and validation only.
        Production should never encounter collisions.
    """
    # Import here to avoid circular dependency
    from utils.comparators import strict_equal
    
    # If IDs are equal but objects are not, collision detected
    if id1 == id2:
        return not strict_equal(obj1, obj2)
    
    return False


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Constants
    "HASH_VERSION",
    
    # Error model
    "IdentityDerivationError",
    
    # Content-derived identity
    "derive_id",
    
    # Namespace-derived identity
    "derive_child_id",
    
    # Composite identity
    "compose_id",
    
    # Utilities
    "parse_id",
    "detect_collision",
]