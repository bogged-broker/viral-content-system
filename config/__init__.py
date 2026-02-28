"""
/config/__init__.py

Configuration Boundary Export Authority
(Explicit Surface, Zero Leakage, Deterministic Imports)

This module is the single public export surface of the entire configuration subsystem.
It defines the official configuration API boundary that all external modules must use.

CRITICAL PRINCIPLES:
- All exports are explicit (no wildcards)
- Minimal surface exposure (only stable public contracts)
- Deterministic and side-effect free
- No accidental symbol leakage
- Static analyzable dependency graph
- Refactor-resilient design

IMPORT CONTRACT:
External modules MUST use:
    from config import DeploymentEnvironment, get_deployment_profile
    
External modules MUST NOT use:
    from config.deployment_profile import get_deployment_profile
    from config.internal.* import ...
    
STABILITY GUARANTEE:
Changing this file is a breaking API change requiring:
- Version bump
- Dependency audit
- Migration plan

IMMUTABILITY:
__all__ is a tuple (not list) to prevent runtime mutation.
"""

# ============================================================================
# FATAL CONFIGURATION ERRORS
# ============================================================================
# All fatal configuration exceptions that halt system startup

from .config_errors import (
    # Base error class
    ConfigurationError,
    
    # Fatal error types (exhaustive)
    InvalidDeploymentEnvironmentError,
    ConfigurationVersionMismatchError,
    MissingConfigurationFieldError,
    InvariantViolationError,
    UnsupportedConfigurationUpgradeError,
    ConfigurationImmutabilityError,
    UnknownConfigurationKeyError,
)


# ============================================================================
# CANONICAL CONFIGURATION BASELINE
# ============================================================================
# Default configuration schema and validation utilities

from .defaults import (
    # Primary configuration baseline
    CANONICAL_BASE_CONFIG,
    
    # Configuration metadata for governance
    CONFIG_METADATA,
    
    # Schema version alignment
    CURRENT_CONFIG_SCHEMA_VERSION,
)


# ============================================================================
# DEPLOYMENT PROFILE AND ENVIRONMENT
# ============================================================================
# Deployment environment types and profile management

from .deployment_profile import (
    # Environment enumeration
    DeploymentEnvironment,
    
    # Profile management
    DeploymentProfile,
    create_deployment_profile,
    get_deployment_profile,
    initialize_deployment_profile,
    set_deployment_profile_context,
    validate_deployment_profile,
    
    # Profile constraints
    enforce_strictness_monotonicity,
)


# ============================================================================
# CONFIGURATION VERSIONING
# ============================================================================
# Version management and migration support
# NOTE: These imports would be activated when config_versioning.py exists

# from .config_versioning import (
#     # Version constants
#     CURRENT_CONFIG_SCHEMA_VERSION,  # May already be imported from defaults
#     
#     # Compatibility checking
#     CompatibilityLevel,
#     check_compatibility,
#     
#     # Migration support
#     get_migration_path,
#     validate_migration_chain,
# )


# ============================================================================
# POLICY TYPES AND CONSTRAINTS
# ============================================================================
# High-level policy interfaces and constraint types
# NOTE: These imports would be activated when policy_types.py exists

# from .policy_types import (
#     # Strictness levels
#     StrictnessLevel,
#     
#     # Constraint enforcement
#     EnvironmentConstraintError,
#     PolicyViolationError,
#     
#     # Policy interfaces
#     PolicyContract,
#     StrictnessConstraint,
# )


# ============================================================================
# CONFIGURATION LOADING AND RUNTIME
# ============================================================================
# Runtime configuration management and accessors
# NOTE: These imports would be activated when config_loader.py exists

# from .config_loader import (
#     # Configuration loading
#     load_configuration,
#     reload_configuration,
#     
#     # Configuration access
#     get_config,
#     get_config_value,
#     
#     # Configuration state
#     is_config_locked,
#     lock_configuration,
# )


# ============================================================================
# EXPLICIT EXPORT SURFACE (IMMUTABLE)
# ============================================================================
# This tuple defines the complete public API of the configuration subsystem.
# It is a tuple (not list) to prevent runtime mutation.
# All external dependencies must import only from this surface.

__all__ = (
    # ========================================================================
    # FATAL ERRORS (from config_errors.py)
    # ========================================================================
    "ConfigurationError",
    "InvalidDeploymentEnvironmentError",
    "ConfigurationVersionMismatchError",
    "MissingConfigurationFieldError",
    "InvariantViolationError",
    "UnsupportedConfigurationUpgradeError",
    "ConfigurationImmutabilityError",
    "UnknownConfigurationKeyError",
    
    # ========================================================================
    # CANONICAL BASELINE (from defaults.py)
    # ========================================================================
    "CANONICAL_BASE_CONFIG",
    "CONFIG_METADATA",
    "CURRENT_CONFIG_SCHEMA_VERSION",
    
    # ========================================================================
    # DEPLOYMENT PROFILE (from deployment_profile.py)
    # ========================================================================
    "DeploymentEnvironment",
    "DeploymentProfile",
    "create_deployment_profile",
    "get_deployment_profile",
    "initialize_deployment_profile",
    "set_deployment_profile_context",
    "validate_deployment_profile",
    "enforce_strictness_monotonicity",
    
    # ========================================================================
    # CONFIGURATION VERSIONING (from config_versioning.py)
    # ========================================================================
    # Uncomment when config_versioning.py is implemented:
    # "CompatibilityLevel",
    # "check_compatibility",
    # "get_migration_path",
    # "validate_migration_chain",
    
    # ========================================================================
    # POLICY TYPES (from policy_types.py)
    # ========================================================================
    # Uncomment when policy_types.py is implemented:
    # "StrictnessLevel",
    # "EnvironmentConstraintError",
    # "PolicyViolationError",
    # "PolicyContract",
    # "StrictnessConstraint",
    
    # ========================================================================
    # CONFIGURATION LOADING (from config_loader.py)
    # ========================================================================
    # Uncomment when config_loader.py is implemented:
    # "load_configuration",
    # "reload_configuration",
    # "get_config",
    # "get_config_value",
    # "is_config_locked",
    # "lock_configuration",
)


# ============================================================================
# EXPORT SURFACE VALIDATION
# ============================================================================
# Note: Export surface validation is intentionally NOT performed at import time
# to maintain strict side-effect-free boundary purity. Validation should be
# performed during explicit bootstrap/initialization phases if needed.


# ============================================================================
# PUBLIC API DOCUMENTATION
# ============================================================================

__version__ = "1.0.0"
__author__ = "Configuration System"
__description__ = "Configuration subsystem public API boundary"

# Prevent modification of __all__ at runtime
# Note: While tuple provides immutability, this serves as documentation
_ORIGINAL_ALL = __all__


def __dir__():
    """
    Customize dir() output to show only public API.
    
    Returns:
        List of public symbols from __all__
    """
    return list(__all__)


# ============================================================================
# IMPORT-TIME SIDE EFFECT PROHIBITION
# ============================================================================
# This module maintains strict side-effect-free boundary purity.
# All validation, assertions, and runtime checks must be performed during
# explicit bootstrap/initialization phases, not at import time.
#
# This ensures:
# - Import safety (no failures during import)
# - Determinism guarantees (no dependency on runtime config state)
# - Pure boundary contract (passive API definition layer)