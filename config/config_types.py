"""
/config/config_types.py

Canonical Configuration Models
(Immutable, Versioned, Serializable)

This is your system's constitution.

Everything else:
- Loads it
- Validates it
- Hashes it
- Enforces it
- Rejects invalid variation

But nothing redefines it.

CRITICAL PRINCIPLES:
- Defines the complete shape of system configuration
- Answers: "What is the full configuration structure the system understands?"
- Does NOT: Load config, Validate config, Merge config, Access environment variables, Apply runtime overrides
- Defines immutable config data models only

CORE DESIGN LAWS:
1. Entire config must be frozen (deep immutable)
2. Entire config must be serializable
3. Entire config must be hashable
4. Entire config must be versioned
5. Entire config must be comparable
6. No optional ambiguity without explicit intent
7. No hidden defaults inside model
8. No mutation after instantiation

VERSION LAW:
SystemConfig.version must:
- Exist
- Be explicit
- Be validated against expected config schema version
- Be part of config hash
- Version drift = hard failure

NO DEFAULTS RULE:
Defaults must live in /config/defaults.py
This file defines schema only.
Because defaults are policy, not structure.

SERIALIZATION REQUIREMENTS:
SystemConfig must be:
- Canonically serializable
- Stable ordering
- Compatible with /utils/serialization.py
- Deterministic across all runtimes and languages

HASHING REQUIREMENTS:
SystemConfig must be hashable for identity purposes.
Hashing implementation and policy belong to /config/config_hashing.py.

STRICT PROHIBITIONS:
This file must not:
- Read environment variables
- Access filesystem
- Merge configs
- Inject defaults
- Normalize strings
- Validate logic
- Import from infra/
- Import from data/
- Import from pipelines/

This file defines shape only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

# Import from utils.types (no fallback allowed)
from utils.types import VersionString, EpochMillis


# ============================================================================
# Limits Configuration
# ============================================================================


@dataclass(frozen=True)
class LimitsConfig:
    """
    Processing and resource limits.
    
    Defines hard constraints on system resource usage and processing capacity.
    No runtime expansion.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        max_events_per_run: Maximum events processed in single run
        max_window_span_ms: Maximum window span in milliseconds
        max_replay_depth: Maximum replay depth allowed
        max_batch_size: Maximum batch size for processing
        max_memory_mb: Maximum memory allocation in MB
        retry_count: Number of retry attempts
        timeout_seconds: Operation timeout in seconds
    """
    max_events_per_run: int
    """Maximum events processed in single run"""
    
    max_window_span_ms: EpochMillis
    """Maximum window span in milliseconds"""
    
    max_replay_depth: int
    """Maximum replay depth allowed"""
    
    max_batch_size: int
    """Maximum batch size for processing"""
    
    max_memory_mb: int
    """Maximum memory allocation in MB"""
    
    retry_count: int
    """Number of retry attempts"""
    
    timeout_seconds: int
    """Operation timeout in seconds"""


# ============================================================================
# Persistence Configuration
# ============================================================================


@dataclass(frozen=True)
class PersistenceConfig:
    """
    Persistence and storage configuration.
    
    Defines how system state is persisted and recovered.
    Backend string resolved elsewhere.
    No IO logic here.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        backend: Storage backend identifier (e.g., 'postgres', 's3')
        snapshot_interval: Snapshot interval in seconds
        strong_consistency_required: Whether strong consistency is required
        checkpoint_enabled: Whether checkpointing is enabled
        compression_enabled: Whether compression is enabled
    """
    backend: str
    """Storage backend identifier (e.g., 'postgres', 's3')"""
    
    snapshot_interval: int
    """Snapshot interval in seconds"""
    
    strong_consistency_required: bool
    """Whether strong consistency is required"""
    
    checkpoint_enabled: bool
    """Whether checkpointing is enabled"""
    
    compression_enabled: bool
    """Whether compression is enabled"""


# ============================================================================
# Replay Configuration
# ============================================================================


@dataclass(frozen=True)
class ReplayConfig:
    """
    Replay policy and requirements.
    
    Defines replay behavior and validation strictness.
    Strict replay policy.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        replay_allowed: Whether replay is permitted
        require_hash_match: Whether config hash must match
        strict_identity: Whether strict identity validation required
        allow_partial_replay: Whether partial replay is allowed
        validate_watermarks: Whether watermark validation is enforced
    """
    replay_allowed: bool
    """Whether replay is permitted"""
    
    require_hash_match: bool
    """Whether config hash must match"""
    
    strict_identity: bool
    """Whether strict identity validation required"""
    
    allow_partial_replay: bool
    """Whether partial replay is allowed"""
    
    validate_watermarks: bool
    """Whether watermark validation is enforced"""


# ============================================================================
# Windows Configuration
# ============================================================================


@dataclass(frozen=True)
class WindowsConfig:
    """
    Window policy configuration.
    
    Defines window behavior constraints and policies.
    Does NOT contain window definitions (those belong in pipeline layer).
    This config only defines policy limits.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        allowed_window_types: Set of permitted window types
        max_allowed_lateness_ms: Maximum allowed lateness in milliseconds
    """
    allowed_window_types: FrozenSet[str]
    """Set of permitted window types"""
    
    max_allowed_lateness_ms: EpochMillis
    """Maximum allowed lateness in milliseconds"""


# ============================================================================
# Computation Configuration
# ============================================================================


@dataclass(frozen=True)
class ComputationConfig:
    """
    Computation semantics and versioning.
    
    Defines computation identity and enforcement policies.
    Identity-level enforcement only.
    
    NOTE: Execution mechanics (aggregation functions, deduplication windows)
    belong to pipeline layer, not constitution schema. This config defines
    only identity-level computation semantics.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        allowed_computation_versions: Set of permitted computation versions
        strict_hash_enforcement: Whether strict hash enforcement is enabled
        version: Computation version
    """
    allowed_computation_versions: FrozenSet[VersionString]
    """Set of permitted computation versions"""
    
    strict_hash_enforcement: bool
    """Whether strict hash enforcement is enabled"""
    
    version: VersionString
    """Computation version"""


# ============================================================================
# Schema Configuration
# ============================================================================


@dataclass(frozen=True)
class SchemaConfig:
    """
    Data schema versioning.
    
    Defines schema versions for data compatibility.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        event_version: Event schema version
        state_version: State schema version
    """
    event_version: VersionString
    """Event schema version"""
    
    state_version: VersionString
    """State schema version"""


# ============================================================================
# Logging Configuration
# ============================================================================


@dataclass(frozen=True)
class LoggingConfig:
    """
    Logging behavior configuration.
    
    Defines logging policy and format.
    Does NOT instantiate loggers.
    
    All fields are required. No defaults.
    Frozen (immutable after creation).
    
    Attributes:
        level: Log level (e.g., 'INFO', 'DEBUG', 'ERROR')
        structured: Whether structured logging is enabled
        format: Log format identifier
    """
    level: str
    """Log level (e.g., 'INFO', 'DEBUG', 'ERROR')"""
    
    structured: bool
    """Whether structured logging is enabled"""
    
    format: str
    """Log format identifier"""


# ============================================================================
# Root System Configuration
# ============================================================================


@dataclass(frozen=True)
class SystemConfig:
    """
    Root system configuration.
    
    This is the canonical configuration structure.
    This is your system's constitution.
    
    All fields are required. No defaults.
    Must be frozen.
    No default values.
    All fields required.
    Version must match declared baseline.
    
    The configuration is:
        - Frozen (immutable after creation)
        - Serializable (canonical representation)
        - Hashable (deterministic identity)
        - Versioned (explicit compatibility)
        - Comparable (equality deterministic)
    
    Attributes:
        version: Configuration schema version (semantic versioning)
        limits: Processing and resource limits
        persistence: Persistence and storage config
        replay: Replay policy and requirements
        windows: Window policy configuration
        computation: Computation semantics and versioning
        schema: Data schema versioning
        logging: Logging behavior
    """
    version: VersionString
    """Configuration schema version (semantic versioning)"""
    
    limits: LimitsConfig
    """Processing and resource limits"""
    
    persistence: PersistenceConfig
    """Persistence and storage config"""
    
    replay: ReplayConfig
    """Replay policy and requirements"""
    
    windows: WindowsConfig
    """Window policy configuration"""
    
    computation: ComputationConfig
    """Computation semantics and versioning"""
    
    schema: SchemaConfig
    """Data schema versioning"""
    
    logging: LoggingConfig
    """Logging behavior"""


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    # Type Aliases
    "VersionString",
    "EpochMillis",
    
    # Configuration Models
    "LimitsConfig",
    "PersistenceConfig",
    "ReplayConfig",
    "WindowsConfig",
    "ComputationConfig",
    "SchemaConfig",
    "LoggingConfig",
    
    # Root Config
    "SystemConfig",
]