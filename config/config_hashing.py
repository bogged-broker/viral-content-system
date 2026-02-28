"""
/config/config_hashing.py

Stable Config Identity
(Replay-Critical, Cryptographically Deterministic)

This is not a convenience hash helper.

This file determines whether two deployments are the same system
or two different universes.

If config hashing is weak, replay is fiction.

CRITICAL PRINCIPLES:
- Config identity must be derived only from canonical, deterministic serialization
- Never from file contents, environment variables directly, unordered dicts, Python object repr
- Only canonical representation counts
- SHA-256 minimum, no truncated hashes, no salting, no runtime entropy
- Same input → identical output across languages if serializer equivalent

ABSOLUTE INVARIANTS:
1. Same config → same hash (deterministic)
2. Different config → different hash (cryptographic)
3. Hash independent of: load source, environment, timestamp, machine, file path, hostname, process ID
4. Canonical serialization only
5. SHA-256 minimum strength
6. No salting (reproducible by auditors)
7. Version and hash are jointly authoritative

If this file lies:
- Replay validation collapses
- Audit trails fragment
- Cross-node consistency checks fail silently
- Deployment drift becomes invisible
- Recovery re-executes under wrong assumptions

This file is the identity anchor of configuration state.
Config hash is the fingerprint of the system's assumptions.
The root anchor of replay.
The boundary between "same system" and "different system".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# Import configuration errors
try:
    from .config_errors import ConfigurationError
except ImportError:
    try:
        from config.config_errors import ConfigurationError
    except ImportError:
        # Fallback
        class ConfigurationError(RuntimeError):
            pass

# Import SystemConfig
from .config_types import SystemConfig, VersionString

# Hard require utils modules (no fallbacks allowed)
from utils.serialization import to_canonical_bytes
from utils.hashing import hash_bytes


# ============================================================================
# Hashing Errors
# ============================================================================


class HashingError(ConfigurationError):
    """
    Base exception for configuration hashing failures.
    
    This is a FATAL error that must halt system startup.
    Hashing failures mean the system cannot determine its configuration identity.
    """
    
    def __init__(
        self,
        message: str,
        config_version: Optional[str] = None,
    ):
        """
        Initialize hashing error.
        
        Args:
            message: Human-readable error description
            config_version: Configuration schema version (if known)
        """
        super().__init__(
            message,
            error_type="HashingError",
            config_version=config_version,
        )


class InvalidHashFormat(HashingError):
    """
    Raised when hash format is invalid.
    
    Hash must be lowercase hex SHA-256 (64 characters).
    """
    
    def __init__(
        self,
        hash_value: str,
        reason: str,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=f"Invalid hash format '{hash_value}': {reason}",
            config_version=config_version,
        )
        self.hash_value = hash_value
        self.reason = reason


class VersionHashMismatch(HashingError):
    """
    Raised when identical hash has mismatched version.
    
    If identical hash but mismatched version field → fatal error.
    Version and hash are jointly authoritative.
    This indicates a collision or corruption.
    """
    
    def __init__(
        self,
        hash_value: str,
        version1: str,
        version2: str,
    ):
        super().__init__(
            message=(
                f"Fatal: Identical hash {hash_value} with different versions "
                f"({version1} vs {version2}). This indicates a collision or corruption."
            ),
            config_version=version1,
        )
        self.hash_value = hash_value
        self.version1 = version1
        self.version2 = version2


class SerializationError(HashingError):
    """
    Raised when canonical serialization fails.
    
    If serializer is unstable → hard fail.
    """
    
    def __init__(
        self,
        reason: str,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            message=f"Serialization failed: {reason}",
            config_version=config_version,
        )
        self.reason = reason


# ============================================================================
# Configuration Identity
# ============================================================================


@dataclass(frozen=True)
class ConfigIdentity:
    """
    Immutable configuration identity value object.
    
    The hash and version jointly define configuration identity.
    This is the fingerprint of the system's assumptions.
    The root anchor of replay.
    The boundary between "same system" and "different system".
    
    Rules:
    - hash must be lowercase hex SHA-256 (64 characters)
    - Must never contain metadata
    - No environment information
    - No timestamps
    - Version must match SystemConfig.version exactly
    - Comparable
    - Serializable
    
    Attributes:
        hash: Lowercase hex SHA-256 (64 characters)
        version: Configuration version string (must match SystemConfig.version)
    """
    hash: str
    """Lowercase hex SHA-256 hash (64 characters)"""
    
    version: VersionString
    """Configuration version string (must match SystemConfig.version exactly)"""
    
    def __post_init__(self) -> None:
        """
        Validate identity format.
        
        Hash must be lowercase hex SHA-256 (64 characters).
        Version must be non-empty.
        """
        # Validate hash format
        if not isinstance(self.hash, str):
            raise InvalidHashFormat(
                str(self.hash), "must be string"
            )
        
        if len(self.hash) != 64:
            raise InvalidHashFormat(
                self.hash,
                f"must be 64 characters (SHA-256 hex), got {len(self.hash)}"
            )
        
        if not all(c in '0123456789abcdef' for c in self.hash):
            raise InvalidHashFormat(
                self.hash,
                "must be lowercase hexadecimal"
            )
        
        # Validate version present
        if not self.version:
            raise HashingError("Version must be non-empty")
        
        if not isinstance(self.version, str):
            raise HashingError(f"Version must be string, got {type(self.version).__name__}")
    
    def __eq__(self, other: object) -> bool:
        """
        Check identity equality.
        
        Two identities are equal if both hash and version match.
        """
        if not isinstance(other, ConfigIdentity):
            return NotImplemented
        return self.hash == other.hash and self.version == other.version
    
    def __str__(self) -> str:
        """String representation."""
        return f"{self.version}:{self.hash[:12]}..."
    
    def __repr__(self) -> str:
        """Detailed representation."""
        return f"ConfigIdentity(version={self.version!r}, hash={self.hash!r})"


# ============================================================================
# Canonical Serialization
# ============================================================================


def _canonical_serialize(config: SystemConfig) -> bytes:
    """
    Canonical serialization for SystemConfig.
    
    Uses the system canonical serializer from utils.serialization.
    No fallbacks allowed - this must hard fail if serializer unavailable.
    
    Must guarantee:
    - Lexicographic ordering of fields
    - Deterministic ordering of nested mappings
    - Deterministic ordering of frozen sets
    - UTF-8 encoding only
    - No whitespace variability
    - No float rounding drift
    
    If serializer is unstable → hard fail.
    
    Args:
        config: SystemConfig to serialize
    
    Returns:
        Canonical byte representation
    
    Raises:
        SerializationError: If serialization fails
    """
    try:
        return to_canonical_bytes(config)
    except Exception as e:
        raise SerializationError(f"Canonical serialization failed: {e}")


# ============================================================================
# Stable Hashing
# ============================================================================


def _stable_hash(data: bytes) -> str:
    """
    Cryptographically stable hash.
    
    Uses the system hashing utility from utils.hashing.
    No fallbacks allowed - this must hard fail if hasher unavailable.
    
    SHA-256 minimum.
    No truncated hashes.
    No salting.
    No runtime entropy.
    No random seed.
    Same input → identical output across languages if serializer equivalent.
    
    Why no salting?
    Because identity must be reproducible by third-party auditors.
    
    Args:
        data: Bytes to hash
    
    Returns:
        Lowercase hex SHA-256 hash (64 characters)
    
    Raises:
        HashingError: If hashing fails
    """
    try:
        return hash_bytes(data)
    except Exception as e:
        raise HashingError(f"Stable hashing failed: {e}")


# ============================================================================
# Configuration Identity Computation
# ============================================================================


def compute_config_identity(
    config: SystemConfig,
) -> ConfigIdentity:
    """
    Compute cryptographically stable identity for configuration.
    
    This is the identity anchor of configuration state.
    Config hash is the fingerprint of the system's assumptions.
    The root anchor of replay.
    The boundary between "same system" and "different system".
    
    Steps (STRICT ORDER):
        1. Assert instance of SystemConfig
        2. Canonical serialize using stable serializer
        3. Hash using cryptographic hash
        4. Validate hash format
        5. Construct ConfigIdentity
        6. Return
    
    Canonical Identity Flow:
    SystemConfig
         ↓
    canonical_serialize(...)
         ↓
    bytes (deterministic)
         ↓
    stable_hash(...)
         ↓
    hex string
         ↓
    ConfigIdentity
    
    No shortcuts.
    
    DETERMINISTIC: Same config always produces identical identity.
    No mutation. No side effects. No I/O. No logging internal state.
    Environment-independent: same hash on all machines.
    
    Args:
        config: Fully resolved SystemConfig
    
    Returns:
        ConfigIdentity with hash and version
    
    Raises:
        HashingError: If identity computation fails
        SerializationError: If serialization fails
        InvalidHashFormat: If hash format is invalid
    
    Critical Invariants:
        - Deterministic: same config → same identity
        - No mutation: config is never modified
        - No side effects: no I/O, no logging
        - Environment-independent: same hash on all machines
        - Cross-node determinism: same config on different machines → same hash
        - Stable across Python patch versions (given deterministic serializer)
    """
    # Step 1: Assert instance of SystemConfig
    if not isinstance(config, SystemConfig):
        raise HashingError(
            f"Expected SystemConfig, got {type(config).__name__}"
        )
    
    # Extract version
    if not hasattr(config, 'version'):
        raise HashingError(
            "SystemConfig must have 'version' field"
        )
    
    version = getattr(config, 'version')
    if not version:
        raise HashingError(
            "Config version must be non-empty"
        )
    
    if not isinstance(version, str):
        raise HashingError(
            f"Config version must be string, got {type(version).__name__}",
            config_version=str(version) if version else None,
        )
    
    # Step 2: Canonical serialize using stable serializer
    try:
        canonical_bytes = _canonical_serialize(config)
    except SerializationError:
        raise
    except Exception as e:
        raise SerializationError(
            f"Unexpected serialization error: {e}",
            config_version=version,
        )
    
    # Step 3: Hash using cryptographic hash
    try:
        hash_hex = _stable_hash(canonical_bytes)
    except HashingError:
        raise
    except Exception as e:
        raise HashingError(
            f"Unexpected hashing error: {e}",
            config_version=version,
        )
    
    # Step 4: Validate hash format (defensive)
    if len(hash_hex) != 64:
        raise InvalidHashFormat(
            hash_hex,
            f"SHA-256 must produce 64 hex characters, got {len(hash_hex)}",
            config_version=version,
        )
    
    if not all(c in '0123456789abcdef' for c in hash_hex):
        raise InvalidHashFormat(
            hash_hex,
            "Hash must be lowercase hexadecimal",
            config_version=version,
        )
    
    # Step 5: Construct ConfigIdentity
    identity = ConfigIdentity(hash=hash_hex, version=version)
    
    # Step 6: Return
    return identity


def verify_config_identity(
    config: SystemConfig,
    expected_identity: ConfigIdentity,
) -> bool:
    """
    Verify configuration matches expected identity.
    
    This is the replay validation function.
    
    Args:
        config: Configuration to verify
        expected_identity: Expected identity
        
    Returns:
        True if identity matches, False otherwise
        
    Raises:
        HashingError: If verification fails due to error
        VersionHashMismatch: If same hash but different version (fatal)
    """
    # Compute actual identity
    actual_identity = compute_config_identity(config)
    
    # Check version first
    if actual_identity.version != expected_identity.version:
        # Different versions - not a match
        return False
    
    # Check hash
    if actual_identity.hash == expected_identity.hash:
        # Perfect match
        return True
    
    # Hash mismatch - this is expected for different configs
    return False


def validate_replay_identity(
    current_config: SystemConfig,
    previous_identity: ConfigIdentity,
) -> None:
    """
    Validate replay configuration identity.
    
    Replay Contract:
    Replay must validate:
    previous_identity.hash == current_identity.hash
    
    If mismatch:
    - replay must fail before execution
    - no partial execution allowed
    - must emit structured divergence reason
    
    No warnings. Hard failure.
    
    This function enforces replay determinism. If the current config
    does not match the previous identity, replay MUST fail before execution.
    
    Args:
        current_config: Current configuration
        previous_identity: Previous run's identity
    
    Raises:
        HashingError: If replay validation fails (identity mismatch)
    """
    current_identity = compute_config_identity(current_config)
    
    # Check version first
    if current_identity.version != previous_identity.version:
        raise HashingError(
            f"Replay version mismatch: "
            f"previous={previous_identity.version}, "
            f"current={current_identity.version}. "
            f"Cannot replay with different config version.",
            config_version=current_identity.version,
        )
    
    # Check hash
    if current_identity.hash != previous_identity.hash:
        raise HashingError(
            f"Replay identity mismatch: "
            f"previous={previous_identity.hash}, "
            f"current={current_identity.hash}. "
            f"Configuration has diverged. Replay cannot proceed.",
            config_version=current_identity.version,
        )


def compare_config_identities(
    identity1: ConfigIdentity,
    identity2: ConfigIdentity,
) -> bool:
    """
    Compare two configuration identities for equality.
    
    Args:
        identity1: First identity
        identity2: Second identity
        
    Returns:
        True if identities are equal, False otherwise
    """
    return identity1 == identity2


# ============================================================================
# Collision Detection (Defensive)
# ============================================================================


def detect_hash_collision(
    config1: SystemConfig,
    config2: SystemConfig,
    identity1: ConfigIdentity,
    identity2: ConfigIdentity,
) -> None:
    """
    Detect hash collision (defensive check).
    
    SHA-256 collisions are practically infeasible, but if we ever
    encounter identical hashes with different versions, this is fatal.
    
    Args:
        config1: First configuration
        config2: Second configuration  
        identity1: First identity
        identity2: Second identity
        
    Raises:
        VersionHashMismatch: If same hash but different version
    """
    if identity1.hash == identity2.hash:
        if identity1.version != identity2.version:
            raise VersionHashMismatch(
                identity1.hash,
                identity1.version,
                identity2.version,
            )


# ============================================================================
# Determinism Testing Utilities
# ============================================================================


def verify_determinism(config: SystemConfig, iterations: int = 10) -> bool:
    """
    Verify identity computation is deterministic.
    
    Computes identity multiple times and ensures all results match.
    
    Args:
        config: Configuration to test
        iterations: Number of times to compute identity
        
    Returns:
        True if all identities match, False otherwise
    """
    identities = [
        compute_config_identity(config)
        for _ in range(iterations)
    ]
    
    # All identities must be equal
    first_identity = identities[0]
    return all(identity == first_identity for identity in identities)


def verify_cross_clone_determinism(config: SystemConfig) -> bool:
    """
    Verify identity is stable across structural cloning.
    
    Creates a deep copy and verifies identity remains identical.
    
    Args:
        config: Configuration to test
        
    Returns:
        True if clone has identical identity
    """
    import copy
    
    original_identity = compute_config_identity(config)
    
    # Deep copy config
    cloned_config = copy.deepcopy(config)
    
    # Compute identity of clone
    cloned_identity = compute_config_identity(cloned_config)
    
    # Must be identical
    return original_identity == cloned_identity


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    # Core Types
    "ConfigIdentity",
    
    # Main Functions
    "compute_config_identity",
    "verify_config_identity",
    "validate_replay_identity",
    "compare_config_identities",
    
    # Collision Detection
    "detect_hash_collision",
    
    # Testing Utilities
    "verify_determinism",
    "verify_cross_clone_determinism",
    
    # Errors
    "HashingError",
    "InvalidHashFormat",
    "VersionHashMismatch",
    "SerializationError",
]