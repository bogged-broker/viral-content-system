"""
/config/config_errors.py

Fatal Configuration Violation Authority
(Non-Recoverable, System-Blocking Errors Only)

This module defines the complete and exclusive set of fatal configuration errors
that prevent the system from starting or continuing safely.

CRITICAL PRINCIPLES:
- Configuration errors ≠ application errors
- Configuration errors mean: "The system cannot trust its own operating assumptions"
- All errors here are FATAL and must halt startup
- No auto-recovery, no suppression, no downgrading to warnings
- Only caught at top-level boot boundary, never in business logic

IMMUTABILITY CONSTRAINT:
- Pure error type definitions only
- No runtime logic
- No global state
- No side effects on import
- No environment variable access
- Deterministic behavior guaranteed

ERROR HIERARCHY:
All configuration fatal errors inherit from ConfigurationError(RuntimeError).
Only explicitly defined subclasses are permitted.
"""

from types import MappingProxyType
from typing import Any, Optional


# ============================================================================
# BASE CONFIGURATION ERROR
# ============================================================================

class ConfigurationError(RuntimeError):
    """
    Base class for all fatal configuration errors.
    
    Signals non-recoverable misconfiguration that must abort system boot.
    
    Properties:
    - Intended to abort boot process
    - Must be caught only at top-level boot boundary
    - Must never be handled inside business logic
    - Must never be auto-recovered or suppressed
    - Must never be downgraded to warnings
    
    All configuration fatal errors must inherit from this class.
    """
    
    def __init__(
        self,
        message: str,
        *,
        error_type: Optional[str] = None,
        environment: Optional[str] = None,
        config_version: Optional[int] = None,
        failing_field: Optional[str] = None,
        compatibility_level: Optional[str] = None,
        strictness_profile: Optional[str] = None,
        **additional_context: Any
    ) -> None:
        """
        Initialize configuration error with structured context.
        
        Args:
            message: Human-readable error description
            error_type: Error classification (auto-set from class name if None)
            environment: Deployment environment where error occurred
            config_version: Configuration schema version involved
            failing_field: Specific configuration field that failed
            compatibility_level: Compatibility assessment (if applicable)
            strictness_profile: Active strictness profile
            **additional_context: Additional structured context for audit logging
        """
        super().__init__(message)
        
        # Structured payload for fatal audit events
        self.error_type = error_type or self.__class__.__name__
        self.environment = environment
        self.config_version = config_version
        self.failing_field = failing_field
        self.compatibility_level = compatibility_level
        self.strictness_profile = strictness_profile
        # Immutable context container for Tier-0 audit payload guarantees
        self.additional_context = MappingProxyType(additional_context) if additional_context else MappingProxyType({})


# ============================================================================
# FATAL ERROR TYPES (EXHAUSTIVE LIST)
# ============================================================================

class InvalidDeploymentEnvironmentError(ConfigurationError):
    """
    FATAL: Unknown or unsupported deployment environment detected.
    
    Raised when:
    - Unknown environment provided
    - Unsupported deployment profile detected
    - Environment not explicitly declared in allowed list
    - Environment string mismatch or malformed
    
    This is fatal because:
    Environment determines safety constraints. Operating with unknown environment
    violates fundamental trust boundaries and may apply incorrect strictness rules.
    
    Examples:
        >>> raise InvalidDeploymentEnvironmentError(
        ...     "Environment 'PROD' not recognized. Must be one of: PRODUCTION, STAGING",
        ...     environment="PROD",
        ...     failing_field="deployment.environment"
        ... )
    """
    pass


class ConfigurationVersionMismatchError(ConfigurationError):
    """
    FATAL: Configuration version incompatibility detected.
    
    Raised when:
    - Persisted config version != expected schema version
    - Compatibility level is INCOMPATIBLE
    - Missing migration plan for version upgrade
    - Downgrade attempted in production environment
    - Forward-only version constraint violated in strict environment
    
    Version misalignment is non-negotiable because:
    Configuration schema drift can cause silent data corruption, incorrect policy
    enforcement, and unpredictable system behavior.
    
    Examples:
        >>> raise ConfigurationVersionMismatchError(
        ...     "Configuration version 3 incompatible with schema version 5",
        ...     config_version=3,
        ...     compatibility_level="INCOMPATIBLE",
        ...     environment="PRODUCTION"
        ... )
    """
    pass


class MissingConfigurationFieldError(ConfigurationError):
    """
    FATAL: Required configuration field is absent.
    
    Raised when:
    - Required top-level configuration field missing
    - Required constraint not defined
    - Required policy flag absent
    - Deployment profile missing mandatory enforcement flag
    
    This is fatal because:
    Silent defaults are forbidden for critical fields. All required configuration
    must be explicitly declared to prevent implicit assumptions.
    
    Examples:
        >>> raise MissingConfigurationFieldError(
        ...     "Required field 'persistence.enforce_immutability' not found",
        ...     failing_field="persistence.enforce_immutability",
        ...     environment="PRODUCTION"
        ... )
    """
    pass


class InvariantViolationError(ConfigurationError):
    """
    FATAL: System invariant or strictness constraint violated.
    
    Raised when:
    - Strictness monotonicity violated (production less strict than staging)
    - Production environment has forbidden policy combination
    - Mutually exclusive configuration flags both enabled
    - Trust gating disabled in production
    - Safety constraints weakened
    
    System invariants must not be weakened because:
    Invariants represent fundamental safety guarantees. Violating them creates
    unpredictable system states and potential security vulnerabilities.
    
    Examples:
        >>> raise InvariantViolationError(
        ...     "PRODUCTION cannot be less strict than STAGING",
        ...     environment="PRODUCTION",
        ...     strictness_profile="RELAXED",
        ...     invariant="strictness_monotonicity"
        ... )
    """
    pass


class UnsupportedConfigurationUpgradeError(ConfigurationError):
    """
    FATAL: Unsafe or unsupported configuration upgrade attempted.
    
    Raised when:
    - Attempted upgrade skipping required intermediate versions
    - Migration path not defined for version transition
    - Irreversible upgrade attempted without explicit approval
    - Upgrade metadata corrupted or missing
    - Breaking change without migration plan
    
    This prevents unsafe evolution because:
    Skipping versions or missing migrations can leave system in inconsistent state
    with partially migrated data structures.
    
    Examples:
        >>> raise UnsupportedConfigurationUpgradeError(
        ...     "Cannot upgrade from version 2 to 5 (skips required version 3)",
        ...     config_version=2,
        ...     target_version=5,
        ...     missing_migrations=[3, 4]
        ... )
    """
    pass


class ConfigurationImmutabilityError(ConfigurationError):
    """
    FATAL: Attempted mutation of immutable configuration.
    
    Raised when:
    - Attempt to mutate deployment profile at runtime
    - Attempt to change configuration after lock
    - Attempt to override strict production rule
    - Any mutation attempt within config boundary after freeze
    
    Immutability must be enforced because:
    Runtime configuration changes violate determinism guarantees and can cause
    inconsistent behavior across request boundaries.
    
    Examples:
        >>> raise ConfigurationImmutabilityError(
        ...     "Cannot modify 'deployment.environment' after configuration lock",
        ...     failing_field="deployment.environment",
        ...     operation="set_value"
        ... )
    """
    pass


class UnknownConfigurationKeyError(ConfigurationError):
    """
    FATAL: Unknown or undeclared configuration key detected.
    
    Raised when:
    - Unexpected top-level configuration key detected
    - Deprecated configuration key used
    - Configuration key exists but not declared in canonical schema
    - Typo in configuration key name
    
    Unknown config is unsafe config because:
    Undeclared keys indicate schema drift, typos, or deprecated fields that may
    cause silent failures or incorrect behavior.
    
    Examples:
        >>> raise UnknownConfigurationKeyError(
        ...     "Configuration key 'persistance.enabled' not in schema (typo?)",
        ...     failing_field="persistance.enabled",
        ...     did_you_mean="persistence.enabled"
        ... )
    """
    pass


# ============================================================================
# EXPORTED API
# ============================================================================

__all__ = [
    # Base error
    "ConfigurationError",
    
    # Fatal error types (exhaustive)
    "InvalidDeploymentEnvironmentError",
    "ConfigurationVersionMismatchError",
    "MissingConfigurationFieldError",
    "InvariantViolationError",
    "UnsupportedConfigurationUpgradeError",
    "ConfigurationImmutabilityError",
    "UnknownConfigurationKeyError",
]

