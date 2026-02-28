"""
/config/defaults.py

Canonical Baseline Configuration Authority
(Explicit, Versioned, Immutable, Deterministic)

This module defines the single source of truth for all default configuration values.
Every configuration key in the system must be declared here first.

CRITICAL INVARIANTS:
- Single canonical baseline exists (CANONICAL_BASE_CONFIG)
- Every config key declared here before use anywhere
- No dynamic behavior, no environment branching
- Deep immutability enforced
- Version alignment required with config_versioning.py
- Unknown keys in overrides must fail
- Structure changes require version bump

This file must remain:
- Static and deterministic
- Environment-agnostic
- Free of external dependencies
- Deeply immutable
- Explicitly versioned
"""

from typing import Any, Dict, Mapping
from types import MappingProxyType

# Import configuration errors for proper error handling
# Tier-0 requirement: Hard import failure if module missing (no fallbacks)
from .config_errors import (
    UnknownConfigurationKeyError,
    ConfigurationVersionMismatchError,
)


# ============================================================================
# VERSION ALIGNMENT (CRITICAL)
# ============================================================================
# Tier-0 requirement: Hard import failure if module missing (no fallbacks)
# Version drift must fail startup, not silently default
from .config_versioning import CURRENT_CONFIG_SCHEMA_VERSION


# ============================================================================
# CONFIGURATION METADATA (GOVERNANCE)
# ============================================================================

CONFIG_METADATA: Mapping[str, Mapping[str, Any]] = MappingProxyType({
    "schema_version": {
        "description": "Configuration schema version for migration and validation",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "system": {
        "description": "Core system-level configuration and operational parameters",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "deployment": {
        "description": "Deployment environment and profile configuration",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "persistence": {
        "description": "Data persistence, immutability, and integrity guarantees",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "recovery": {
        "description": "Fault recovery, repair authorization, and safety protocols",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "account": {
        "description": "Account management, lifecycle, and authorization policies",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "experiments": {
        "description": "Experimental feature flags and A/B testing framework",
        "introduced_in": 1,
        "strictness_class": "RELAXED",
        "mutable_at_runtime": True,
    },
    "rate_limiting": {
        "description": "Request rate limiting, throttling, and quota management",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
    "observability": {
        "description": "Logging, metrics, tracing, and monitoring configuration",
        "introduced_in": 1,
        "strictness_class": "OPTIONAL",
        "mutable_at_runtime": True,
    },
    "security": {
        "description": "Security policies, encryption, and authentication requirements",
        "introduced_in": 1,
        "strictness_class": "STRICT",
        "mutable_at_runtime": False,
    },
})


# ============================================================================
# CANONICAL BASE CONFIGURATION (SINGLE SOURCE OF TRUTH)
# ============================================================================

_CANONICAL_BASE_CONFIG: Dict[str, Any] = {
    # ========================================================================
    # SCHEMA VERSION (CRITICAL - MUST ALIGN)
    # ========================================================================
    "schema_version": CURRENT_CONFIG_SCHEMA_VERSION,
    
    # ========================================================================
    # SYSTEM - Core system-level configuration
    # ========================================================================
    "system": {
        "service_name": "canonical-service",
        "instance_id_required": True,
        "startup_validation_strict": True,
        "graceful_shutdown_timeout_seconds": 30,
        "health_check_enabled": True,
        "health_check_interval_seconds": 60,
        "panic_on_invariant_violation": True,
        "allow_unsafe_mode": False,
        "debug_mode": False,
        "verbose_logging": False,
    },
    
    # ========================================================================
    # DEPLOYMENT - Environment and profile configuration
    # ========================================================================
    "deployment": {
        "default_environment": "STAGING",
        "allowed_environments": ["DEVELOPMENT", "STAGING", "PRODUCTION"],
        "profile_enforcement_enabled": True,
        "require_explicit_environment": True,
        "allow_profile_override": False,
        "deployment_id_required": True,
        "region": "us-east-1",
        "availability_zone": "us-east-1a",
        "multi_region_enabled": False,
    },
    
    # ========================================================================
    # PERSISTENCE - Data integrity and immutability
    # ========================================================================
    "persistence": {
        "enforce_immutability": True,
        "hash_verification": True,
        "hash_algorithm": "sha256",
        "enable_write_ahead_log": True,
        "fsync_on_write": True,
        "enable_compression": False,
        "compression_algorithm": "zstd",
        "enable_encryption_at_rest": True,
        "backup_enabled": True,
        "backup_interval_hours": 24,
        "retention_days": 90,
        "enable_point_in_time_recovery": True,
        "checkpoint_interval_seconds": 300,
        "max_transaction_size_mb": 100,
    },
    
    # ========================================================================
    # RECOVERY - Fault recovery and repair authorization
    # ========================================================================
    "recovery": {
        "allow_partial_repair": False,
        "require_operator_authorization": True,
        "auto_recovery_enabled": False,
        "max_auto_recovery_attempts": 3,
        "recovery_timeout_seconds": 600,
        "enable_repair_audit_log": True,
        "rollback_enabled": True,
        "rollback_requires_approval": True,
        "corruption_detection_enabled": True,
        "fail_fast_on_corruption": True,
        "quarantine_corrupted_data": True,
    },
    
    # ========================================================================
    # ACCOUNT - Account lifecycle and authorization
    # ========================================================================
    "account": {
        "require_email_verification": True,
        "require_phone_verification": False,
        "allow_account_deletion": True,
        "deletion_requires_confirmation": True,
        "soft_delete_retention_days": 30,
        "max_login_attempts": 5,
        "lockout_duration_minutes": 15,
        "session_timeout_minutes": 60,
        "require_mfa": False,
        "password_min_length": 12,
        "password_require_uppercase": True,
        "password_require_lowercase": True,
        "password_require_digits": True,
        "password_require_special": True,
        "password_expiry_days": 90,
        "allow_password_reuse": False,
        "password_history_count": 5,
    },
    
    # ========================================================================
    # EXPERIMENTS - Feature flags and A/B testing
    # ========================================================================
    "experiments": {
        "enabled": True,
        "allow_unregistered_variants": False,
        "require_variant_registration": True,
        "default_variant_allocation": "control",
        "enable_dynamic_allocation": False,
        "allocation_cache_ttl_seconds": 300,
        "enable_experiment_override": False,
        "require_override_authorization": True,
        "track_experiment_exposure": True,
        "exposure_logging_enabled": True,
        "enable_automatic_rollout": False,
    },
    
    # ========================================================================
    # RATE_LIMITING - Request throttling and quota management
    # ========================================================================
    "rate_limiting": {
        "enabled": True,
        "default_strategy": "token_bucket",
        "requests_per_minute": 100,
        "burst_size": 20,
        "enable_per_user_limits": True,
        "enable_per_ip_limits": True,
        "enable_global_limits": True,
        "rejection_strategy": "http_429",
        "enable_rate_limit_headers": True,
        "track_limit_violations": True,
        "auto_block_on_violation": False,
        "violation_threshold": 10,
        "block_duration_minutes": 60,
        "enable_adaptive_limits": False,
    },
    
    # ========================================================================
    # OBSERVABILITY - Logging, metrics, and monitoring
    # ========================================================================
    "observability": {
        "logging_enabled": True,
        "log_level": "INFO",
        "log_format": "json",
        "enable_structured_logging": True,
        "enable_request_logging": True,
        "enable_performance_logging": True,
        "log_retention_days": 30,
        "metrics_enabled": True,
        "metrics_port": 9090,
        "metrics_path": "/metrics",
        "enable_custom_metrics": True,
        "tracing_enabled": True,
        "tracing_sample_rate": 0.1,
        "enable_distributed_tracing": True,
        "trace_retention_days": 7,
        "alerting_enabled": True,
        "enable_error_tracking": True,
        "enable_profiling": False,
    },
    
    # ========================================================================
    # SECURITY - Security policies and encryption
    # ========================================================================
    "security": {
        "enable_tls": True,
        "tls_min_version": "1.3",
        "enable_certificate_validation": True,
        "enable_hsts": True,
        "hsts_max_age_seconds": 31536000,
        "enable_csrf_protection": True,
        "enable_cors": False,
        "cors_allowed_origins": [],
        "enable_content_security_policy": True,
        "enable_xss_protection": True,
        "enable_clickjacking_protection": True,
        "enable_api_key_rotation": True,
        "api_key_rotation_days": 90,
        "enable_secret_scanning": True,
        "enable_vulnerability_scanning": True,
        "require_signed_requests": False,
        "enable_request_validation": True,
        "enable_response_sanitization": True,
    },
}


# ============================================================================
# DEEP IMMUTABILITY ENFORCEMENT
# ============================================================================

def _deep_freeze(obj: Any) -> Any:
    """
    Recursively freeze nested dictionaries and lists into immutable structures.
    
    Converts:
    - dict -> MappingProxyType (immutable mapping)
    - list -> tuple (immutable sequence)
    - Leaves primitives unchanged (str, int, float, bool, None)
    
    For production-grade systems, this function must be strict:
    - Only known types are allowed
    - Unknown types raise errors to prevent silent failures
    - Ensures complete immutability guarantee
    
    Args:
        obj: Object to freeze
        
    Returns:
        Deeply immutable version of the object
        
    Raises:
        TypeError: If object type is not supported for freezing
    """
    if isinstance(obj, dict):
        return MappingProxyType({
            key: _deep_freeze(value) 
            for key, value in obj.items()
        })
    elif isinstance(obj, list):
        return tuple(_deep_freeze(item) for item in obj)
    elif isinstance(obj, tuple):
        # Recursively freeze tuple contents
        return tuple(_deep_freeze(item) for item in obj)
    elif isinstance(obj, (str, int, float, bool, type(None))):
        # Primitives are already immutable
        return obj
    elif isinstance(obj, MappingProxyType):
        # Already frozen, recursively freeze values
        return MappingProxyType({
            key: _deep_freeze(value)
            for key, value in obj.items()
        })
    else:
        # Production-grade strictness: reject unknown types
        # This prevents silent failures and ensures complete immutability
        raise TypeError(
            f"Cannot freeze object of type {type(obj).__name__}: {obj}. "
            f"Only dict, list, tuple, and primitives (str, int, float, bool, None) are supported. "
            f"Unknown types in configuration violate immutability guarantees."
        )


# Create the deeply immutable canonical configuration
CANONICAL_BASE_CONFIG: Mapping[str, Any] = _deep_freeze(_CANONICAL_BASE_CONFIG)


# ============================================================================
# VALIDATION UTILITIES
# ============================================================================

def validate_version_alignment() -> None:
    """
    Validate that the configuration version matches the expected schema version.
    
    This MUST be called during system startup to prevent version drift.
    
    Version misalignment is non-negotiable because:
    Configuration schema drift can cause silent data corruption, incorrect policy
    enforcement, and unpredictable system behavior.
    
    Raises:
        ConfigurationVersionMismatchError: If version alignment check fails
    """
    declared_version = CANONICAL_BASE_CONFIG["schema_version"]
    expected_version = CURRENT_CONFIG_SCHEMA_VERSION
    
    if declared_version != expected_version:
        raise ConfigurationVersionMismatchError(
            f"FATAL: Configuration version mismatch. "
            f"CANONICAL_BASE_CONFIG declares version {declared_version}, "
            f"but CURRENT_CONFIG_SCHEMA_VERSION is {expected_version}. "
            f"Update defaults.py to match schema version. "
            f"Version drift is forbidden and must be resolved before system startup.",
            config_version=declared_version,
            compatibility_level="INCOMPATIBLE",
        )


def get_all_config_keys(config: Mapping[str, Any], prefix: str = "") -> set:
    """
    Recursively extract all configuration keys for schema validation.
    
    Args:
        config: Configuration mapping to extract keys from
        prefix: Key prefix for nested keys (used in recursion)
        
    Returns:
        Set of all dotted key paths in the configuration
    """
    keys = set()
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key
        keys.add(full_key)
        
        if isinstance(value, Mapping):
            keys.update(get_all_config_keys(value, full_key))
    
    return keys


def validate_override_keys(override: Mapping[str, Any]) -> None:
    """
    Validate that override contains only keys defined in baseline schema.
    
    Unknown config is unsafe config because:
    Undeclared keys indicate schema drift, typos, or deprecated fields that may
    cause silent failures or incorrect behavior.
    
    This function enforces the critical invariant:
    > All configuration keys must be declared in CANONICAL_BASE_CONFIG first.
    
    Args:
        override: Override configuration to validate
        
    Raises:
        UnknownConfigurationKeyError: If override contains unknown keys not in baseline
        
    Examples:
        >>> from config.defaults import CANONICAL_BASE_CONFIG, validate_override_keys
        >>> override = {"system": {"new_field": "value"}}  # Not in baseline
        >>> validate_override_keys(override)  # Raises UnknownConfigurationKeyError
    """
    baseline_keys = get_all_config_keys(CANONICAL_BASE_CONFIG)
    override_keys = get_all_config_keys(override)
    
    unknown_keys = override_keys - baseline_keys
    
    if unknown_keys:
        # Sort for deterministic error messages
        sorted_unknown = sorted(unknown_keys)
        
        raise UnknownConfigurationKeyError(
            f"Override contains keys not defined in CANONICAL_BASE_CONFIG: {sorted_unknown}. "
            f"All configuration keys must be declared in defaults.py first. "
            f"This prevents schema drift and ensures configuration determinism.",
            failing_field=sorted_unknown[0] if sorted_unknown else None,
            unknown_keys=sorted_unknown,
        )


# ============================================================================
# STARTUP VALIDATION (EXECUTE ON IMPORT)
# ============================================================================

# Validate version alignment immediately on import
validate_version_alignment()


# ============================================================================
# PUBLIC API
# ============================================================================

__all__ = [
    "CANONICAL_BASE_CONFIG",
    "CONFIG_METADATA",
    "CURRENT_CONFIG_SCHEMA_VERSION",
    "validate_version_alignment",
    "validate_override_keys",
    "get_all_config_keys",
]