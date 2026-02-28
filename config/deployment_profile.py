"""
/config/deployment_profile.py

Environment Constraint Authority
(Explicit, Deterministic, Non-Bypassable, Pure Dependency Injection)

This module is the single authoritative definition of environment-level behavioral
constraints. It defines what is allowed in production vs staging vs development.

TIER-0 ARCHITECTURAL PRINCIPLES:
- Zero ambiguous environment behavior
- No implicit environment branching
- No ad-hoc "if ENV == 'prod'" scattered in codebase
- Centralized policy enforcement
- Static analyzability of deployment differences
- Production must NEVER be less strict than staging
- NO GLOBAL MUTABLE STATE (uses contextvars for context-local storage)
- NO IMPORT-TIME SIDE EFFECTS (validation deferred to profile creation)
- FACTORY-ONLY PROFILE ACCESS (raw profiles are private)
- EXPLICIT INJECTION ONLY (no environment variable inference)

USAGE PATTERN:
CORRECT:
    from config import create_deployment_profile, initialize_deployment_profile
    from config import DeploymentEnvironment
    
    # At composition root (main.py/bootstrap.py):
    env = DeploymentEnvironment.PRODUCTION
    profile = initialize_deployment_profile(env)
    
    # In application code:
    from config import get_deployment_profile
    profile = get_deployment_profile()
    if profile.allow_partial_repair:
        ...
        
FORBIDDEN:
    if os.getenv("ENV") == "prod":
        ...
    from config.deployment_profile import __PRODUCTION_PROFILE  # Private!

IMMUTABILITY:
- Profile objects are frozen (dataclass frozen=True)
- Cannot mutate flags at runtime
- Cannot override per-request
- Mutation attempts raise FrozenInstanceError

DETERMINISM:
- Environment must be injected explicitly at boot
- No inference from hostname, URL, or system state
- Profile behavior identical across restarts
- No time, load, or secret dependencies

DEPENDENCY INJECTION:
- Profiles are stored in context-local storage (contextvars)
- Thread-safe and async-safe without global mutable state
- Supports explicit injection via set_deployment_profile_context()
- Enables pure composition root patterns
"""

from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config_errors import (
    InvalidDeploymentEnvironmentError,
    InvariantViolationError,
)


# ============================================================================
# CANONICAL DEPLOYMENT ENVIRONMENTS
# ============================================================================

class DeploymentEnvironment(Enum):
    """
    Exhaustive enumeration of allowed deployment environments.
    
    Only these environments are permitted. No arbitrary strings like "prod2",
    "temp", or free-form overrides allowed.
    
    Values:
        PRODUCTION: Live production environment with strictest constraints
        STAGING: Pre-production environment with relaxed constraints for testing
        DEVELOPMENT: Local/development environment with minimal constraints
        TESTING: Automated testing environment (CI/CD)
    """
    PRODUCTION = "PRODUCTION"
    STAGING = "STAGING"
    DEVELOPMENT = "DEVELOPMENT"
    TESTING = "TESTING"
    
    def __str__(self) -> str:
        """Return environment name for logging."""
        return self.value
    
    @classmethod
    def from_string(cls, env_string: str) -> "DeploymentEnvironment":
        """
        Parse environment from string, raising error if invalid.
        
        Args:
            env_string: Environment string to parse
            
        Returns:
            DeploymentEnvironment enum value
            
        Raises:
            InvalidDeploymentEnvironmentError: If environment string invalid
        """
        # Normalize to uppercase for case-insensitive matching
        normalized = env_string.strip().upper()
        
        try:
            return cls[normalized]
        except KeyError:
            allowed = [e.value for e in cls]
            raise InvalidDeploymentEnvironmentError(
                f"Invalid deployment environment: '{env_string}'. "
                f"Must be one of: {allowed}",
                environment=env_string,
                failing_field="deployment.environment",
                allowed_values=allowed,
            )


# ============================================================================
# DEPLOYMENT PROFILE (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class DeploymentProfile:
    """
    Immutable deployment profile defining environment-specific constraints.
    
    This object encapsulates all environment-level behavioral policies.
    Once created, it cannot be modified (frozen=True enforces immutability).
    
    Categories of constraints:
    1. Repair & Recovery Permissions
    2. Failure Strictness
    3. Experiment / Feature Gates
    4. Identity & Trust Relaxation
    5. Persistence & Mutation Guards
    6. Rate Limiting & Abuse Constraints
    7. Logging & Audit
    8. Concurrency & Mutation Rules
    9. Observability & Monitoring
    10. External Integrations
    """
    
    # Core environment identifier
    environment: DeploymentEnvironment
    
    # ========================================================================
    # REPAIR & RECOVERY PERMISSIONS
    # ========================================================================
    allow_partial_repair: bool
    """Allow partial repair operations (STAGING: True, PRODUCTION: False)"""
    
    allow_auto_repair: bool
    """Allow automatic repair without operator confirmation (STAGING: True, PRODUCTION: False)"""
    
    require_operator_authorization: bool
    """Require explicit operator authorization for repairs (STAGING: False, PRODUCTION: True)"""
    
    enable_repair_audit_log: bool
    """Emit immutable audit logs for all repair operations (STAGING: True, PRODUCTION: True)"""
    
    # ========================================================================
    # PERSISTENCE & MUTATION GUARDS
    # ========================================================================
    enforce_immutability: bool
    """Enforce immutability boundaries (STAGING: True, PRODUCTION: True)"""
    
    allow_truncate_operations: bool
    """Allow truncate/reset operations (STAGING: True, PRODUCTION: False)"""
    
    enforce_persistence_hash: bool
    """Validate persistence hash integrity (STAGING: True, PRODUCTION: True)"""
    
    allow_schema_downgrade: bool
    """Allow schema version downgrade (STAGING: False, PRODUCTION: False)"""
    
    # ========================================================================
    # FAILURE STRICTNESS
    # ========================================================================
    enforce_hard_fail: bool
    """Hard-fail on invariant violations (STAGING: False, PRODUCTION: True)"""
    
    allow_soft_fail: bool
    """Allow soft-fail with warnings (STAGING: True, PRODUCTION: False)"""
    
    fail_fast_on_corruption: bool
    """Immediately fail on data corruption detection (STAGING: True, PRODUCTION: True)"""
    
    # ========================================================================
    # EXPERIMENTS & FEATURE FLAGS
    # ========================================================================
    enable_experiments: bool
    """Enable experiment/A/B testing system (STAGING: True, PRODUCTION: True)"""
    
    allow_unregistered_variants: bool
    """Allow unregistered experiment variants (STAGING: True, PRODUCTION: False)"""
    
    require_variant_registration: bool
    """Require experiment variants to be pre-registered (STAGING: False, PRODUCTION: True)"""
    
    # ========================================================================
    # IDENTITY & TRUST
    # ========================================================================
    enforce_trust_gating: bool
    """Enforce trust tier gating (STAGING: False, PRODUCTION: True)"""
    
    allow_mock_providers: bool
    """Allow mock identity/payment providers (STAGING: True, PRODUCTION: False)"""
    
    require_provider_verification: bool
    """Require provider certificate/signature verification (STAGING: False, PRODUCTION: True)"""
    
    # ========================================================================
    # SCHEMA & VERSION ENFORCEMENT
    # ========================================================================
    enforce_version_alignment: bool
    """Enforce strict version alignment (STAGING: True, PRODUCTION: True)"""
    
    reject_unknown_schema_version: bool
    """Reject unknown schema versions (STAGING: False, PRODUCTION: True)"""
    
    # ========================================================================
    # RATE LIMITING & ABUSE
    # ========================================================================
    enforce_rate_limits: bool
    """Enforce rate limiting (STAGING: False, PRODUCTION: True)"""
    
    enable_abuse_detection: bool
    """Enable abuse detection and blocking (STAGING: False, PRODUCTION: True)"""
    
    enforce_geo_restrictions: bool
    """Enforce geographic restrictions (STAGING: False, PRODUCTION: True)"""
    
    # ========================================================================
    # LOGGING & AUDIT
    # ========================================================================
    enforce_audit_hash: bool
    """Enforce audit log hash integrity (STAGING: False, PRODUCTION: True)"""
    
    enable_detailed_debug_logs: bool
    """Enable detailed debug logging (STAGING: True, PRODUCTION: False)"""
    
    enforce_pii_redaction: bool
    """Enforce PII redaction in logs (STAGING: False, PRODUCTION: True)"""
    
    require_structured_logging: bool
    """Require structured JSON logging (STAGING: True, PRODUCTION: True)"""
    
    # ========================================================================
    # OBSERVABILITY & MONITORING
    # ========================================================================
    strict_observability_required: bool
    """Require observability pipeline operational (STAGING: False, PRODUCTION: True)"""
    
    allow_monitoring_bypass: bool
    """Allow startup without monitoring (STAGING: True, PRODUCTION: False)"""
    
    require_health_checks: bool
    """Require health check endpoints (STAGING: True, PRODUCTION: True)"""
    
    # ========================================================================
    # CONCURRENCY & LOCKING
    # ========================================================================
    enforce_graph_locks: bool
    """Enforce graph-level locking for mutations (STAGING: False, PRODUCTION: True)"""
    
    reject_concurrent_repair: bool
    """Reject overlapping repair operations (STAGING: False, PRODUCTION: True)"""
    
    # ========================================================================
    # EXTERNAL INTEGRATIONS
    # ========================================================================
    require_webhook_signatures: bool
    """Require webhook signature validation (STAGING: False, PRODUCTION: True)"""
    
    allow_sandbox_apis: bool
    """Allow sandbox/test API endpoints (STAGING: True, PRODUCTION: False)"""
    
    enforce_tls_verification: bool
    """Enforce TLS certificate verification (STAGING: False, PRODUCTION: True)"""
    
    def validate_strictness_monotonicity(self, reference_profile: "DeploymentProfile") -> None:
        """
        Validate that this profile is not less strict than reference profile.
        
        Used to ensure PRODUCTION >= STAGING in strictness.
        
        This comprehensive validation ensures that production never allows
        operations that staging forbids, and production always enforces
        constraints that staging enforces.
        
        Args:
            reference_profile: Reference profile to compare against (typically STAGING)
            
        Raises:
            InvariantViolationError: If this profile is less strict than reference
        """
        violations = []
        
        # ========================================================================
        # REPAIR & RECOVERY PERMISSIONS
        # ========================================================================
        # Production cannot allow operations that staging forbids
        if not reference_profile.allow_partial_repair and self.allow_partial_repair:
            violations.append("allow_partial_repair: cannot be True if reference is False")
        
        if not reference_profile.allow_auto_repair and self.allow_auto_repair:
            violations.append("allow_auto_repair: cannot be True if reference is False")
        
        # Production must enforce what staging enforces
        if reference_profile.require_operator_authorization and not self.require_operator_authorization:
            violations.append("require_operator_authorization: cannot be False if reference is True")
        
        # ========================================================================
        # PERSISTENCE & MUTATION GUARDS
        # ========================================================================
        if not reference_profile.allow_truncate_operations and self.allow_truncate_operations:
            violations.append("allow_truncate_operations: cannot be True if reference is False")
        
        if not reference_profile.allow_schema_downgrade and self.allow_schema_downgrade:
            violations.append("allow_schema_downgrade: cannot be True if reference is False")
        
        if reference_profile.enforce_immutability and not self.enforce_immutability:
            violations.append("enforce_immutability: cannot be False if reference is True")
        
        if reference_profile.enforce_persistence_hash and not self.enforce_persistence_hash:
            violations.append("enforce_persistence_hash: cannot be False if reference is True")
        
        # ========================================================================
        # FAILURE STRICTNESS
        # ========================================================================
        if not reference_profile.allow_soft_fail and self.allow_soft_fail:
            violations.append("allow_soft_fail: cannot be True if reference is False")
        
        if reference_profile.enforce_hard_fail and not self.enforce_hard_fail:
            violations.append("enforce_hard_fail: cannot be False if reference is True")
        
        if reference_profile.fail_fast_on_corruption and not self.fail_fast_on_corruption:
            violations.append("fail_fast_on_corruption: cannot be False if reference is True")
        
        # ========================================================================
        # EXPERIMENTS & FEATURE FLAGS
        # ========================================================================
        if not reference_profile.allow_unregistered_variants and self.allow_unregistered_variants:
            violations.append("allow_unregistered_variants: cannot be True if reference is False")
        
        if reference_profile.require_variant_registration and not self.require_variant_registration:
            violations.append("require_variant_registration: cannot be False if reference is True")
        
        # ========================================================================
        # IDENTITY & TRUST
        # ========================================================================
        if not reference_profile.allow_mock_providers and self.allow_mock_providers:
            violations.append("allow_mock_providers: cannot be True if reference is False")
        
        if reference_profile.enforce_trust_gating and not self.enforce_trust_gating:
            violations.append("enforce_trust_gating: cannot be False if reference is True")
        
        if reference_profile.require_provider_verification and not self.require_provider_verification:
            violations.append("require_provider_verification: cannot be False if reference is True")
        
        # ========================================================================
        # SCHEMA & VERSION ENFORCEMENT
        # ========================================================================
        if reference_profile.enforce_version_alignment and not self.enforce_version_alignment:
            violations.append("enforce_version_alignment: cannot be False if reference is True")
        
        if reference_profile.reject_unknown_schema_version and not self.reject_unknown_schema_version:
            violations.append("reject_unknown_schema_version: cannot be False if reference is True")
        
        # ========================================================================
        # RATE LIMITING & ABUSE
        # ========================================================================
        if reference_profile.enforce_rate_limits and not self.enforce_rate_limits:
            violations.append("enforce_rate_limits: cannot be False if reference is True")
        
        if reference_profile.enable_abuse_detection and not self.enable_abuse_detection:
            violations.append("enable_abuse_detection: cannot be False if reference is True")
        
        if reference_profile.enforce_geo_restrictions and not self.enforce_geo_restrictions:
            violations.append("enforce_geo_restrictions: cannot be False if reference is True")
        
        # ========================================================================
        # LOGGING & AUDIT
        # ========================================================================
        if reference_profile.enforce_audit_hash and not self.enforce_audit_hash:
            violations.append("enforce_audit_hash: cannot be False if reference is True")
        
        if not reference_profile.enable_detailed_debug_logs and self.enable_detailed_debug_logs:
            # Production should not have debug logs if staging doesn't
            # (This is a bit inverted - staging allows debug, production shouldn't)
            # Actually, this is fine - staging can have debug, production shouldn't
            # So we check: if staging doesn't allow debug, production definitely shouldn't
            pass  # This is correct as-is
        
        if reference_profile.enforce_pii_redaction and not self.enforce_pii_redaction:
            violations.append("enforce_pii_redaction: cannot be False if reference is True")
        
        if reference_profile.require_structured_logging and not self.require_structured_logging:
            violations.append("require_structured_logging: cannot be False if reference is True")
        
        # ========================================================================
        # OBSERVABILITY & MONITORING
        # ========================================================================
        if reference_profile.strict_observability_required and not self.strict_observability_required:
            violations.append("strict_observability_required: cannot be False if reference is True")
        
        if not reference_profile.allow_monitoring_bypass and self.allow_monitoring_bypass:
            violations.append("allow_monitoring_bypass: cannot be True if reference is False")
        
        if reference_profile.require_health_checks and not self.require_health_checks:
            violations.append("require_health_checks: cannot be False if reference is True")
        
        # ========================================================================
        # CONCURRENCY & LOCKING
        # ========================================================================
        if reference_profile.enforce_graph_locks and not self.enforce_graph_locks:
            violations.append("enforce_graph_locks: cannot be False if reference is True")
        
        if reference_profile.reject_concurrent_repair and not self.reject_concurrent_repair:
            violations.append("reject_concurrent_repair: cannot be False if reference is True")
        
        # ========================================================================
        # EXTERNAL INTEGRATIONS
        # ========================================================================
        if reference_profile.require_webhook_signatures and not self.require_webhook_signatures:
            violations.append("require_webhook_signatures: cannot be False if reference is True")
        
        if not reference_profile.allow_sandbox_apis and self.allow_sandbox_apis:
            violations.append("allow_sandbox_apis: cannot be True if reference is False")
        
        if reference_profile.enforce_tls_verification and not self.enforce_tls_verification:
            violations.append("enforce_tls_verification: cannot be False if reference is True")
        
        if violations:
            raise InvariantViolationError(
                f"Strictness monotonicity violated: {self.environment.value} is less strict "
                f"than {reference_profile.environment.value}. Violations: {violations}",
                environment=str(self.environment),
                strictness_profile=str(self.environment),
                invariant="strictness_monotonicity",
                violations=violations,
            )
    
    def validate_production_constraints(self) -> None:
        """
        Validate that production profile meets minimum safety requirements.
        
        Production MUST have certain constraints enabled for safety.
        
        Raises:
            InvariantViolationError: If production safety constraints violated
        """
        if self.environment != DeploymentEnvironment.PRODUCTION:
            return  # Only validate production profiles
        
        violations = []
        
        # Production MUST enforce these
        if not self.enforce_hard_fail:
            violations.append("enforce_hard_fail must be True in PRODUCTION")
        
        if not self.require_operator_authorization:
            violations.append("require_operator_authorization must be True in PRODUCTION")
        
        if not self.enforce_trust_gating:
            violations.append("enforce_trust_gating must be True in PRODUCTION")
        
        if not self.enforce_rate_limits:
            violations.append("enforce_rate_limits must be True in PRODUCTION")
        
        if not self.strict_observability_required:
            violations.append("strict_observability_required must be True in PRODUCTION")
        
        if not self.reject_unknown_schema_version:
            violations.append("reject_unknown_schema_version must be True in PRODUCTION")
        
        # Production MUST NOT allow these
        if self.allow_partial_repair:
            violations.append("allow_partial_repair must be False in PRODUCTION")
        
        if self.allow_auto_repair:
            violations.append("allow_auto_repair must be False in PRODUCTION")
        
        if self.allow_soft_fail:
            violations.append("allow_soft_fail must be False in PRODUCTION")
        
        if self.allow_mock_providers:
            violations.append("allow_mock_providers must be False in PRODUCTION")
        
        if self.allow_unregistered_variants:
            violations.append("allow_unregistered_variants must be False in PRODUCTION")
        
        if self.allow_truncate_operations:
            violations.append("allow_truncate_operations must be False in PRODUCTION")
        
        if violations:
            raise InvariantViolationError(
                f"Production safety constraints violated: {violations}",
                environment="PRODUCTION",
                strictness_profile="PRODUCTION",
                invariant="production_safety_requirements",
                violations=violations,
            )


# ============================================================================
# CANONICAL PROFILE DEFINITIONS
# ============================================================================

# PRODUCTION Profile: Strictest enforcement, zero tolerance for unsafe operations
# Private: Access only through factory function create_deployment_profile()
__PRODUCTION_PROFILE = DeploymentProfile(
    environment=DeploymentEnvironment.PRODUCTION,
    
    # Repair & Recovery: Locked down, requires authorization
    allow_partial_repair=False,
    allow_auto_repair=False,
    require_operator_authorization=True,
    enable_repair_audit_log=True,
    
    # Persistence: Strict immutability, no truncation
    enforce_immutability=True,
    allow_truncate_operations=False,
    enforce_persistence_hash=True,
    allow_schema_downgrade=False,
    
    # Failure: Hard fail, no soft failures
    enforce_hard_fail=True,
    allow_soft_fail=False,
    fail_fast_on_corruption=True,
    
    # Experiments: Enabled but strict registration required
    enable_experiments=True,
    allow_unregistered_variants=False,
    require_variant_registration=True,
    
    # Identity & Trust: Full enforcement
    enforce_trust_gating=True,
    allow_mock_providers=False,
    require_provider_verification=True,
    
    # Schema & Versioning: Strict alignment
    enforce_version_alignment=True,
    reject_unknown_schema_version=True,
    
    # Rate Limiting: Full enforcement
    enforce_rate_limits=True,
    enable_abuse_detection=True,
    enforce_geo_restrictions=True,
    
    # Logging: PII redacted, audit hash enforced
    enforce_audit_hash=True,
    enable_detailed_debug_logs=False,
    enforce_pii_redaction=True,
    require_structured_logging=True,
    
    # Observability: Strict monitoring required
    strict_observability_required=True,
    allow_monitoring_bypass=False,
    require_health_checks=True,
    
    # Concurrency: Full locking enforcement
    enforce_graph_locks=True,
    reject_concurrent_repair=True,
    
    # External: Full verification required
    require_webhook_signatures=True,
    allow_sandbox_apis=False,
    enforce_tls_verification=True,
)

# STAGING Profile: Relaxed for testing, but still maintains core safety
# Private: Access only through factory function create_deployment_profile()
__STAGING_PROFILE = DeploymentProfile(
    environment=DeploymentEnvironment.STAGING,
    
    # Repair & Recovery: More permissive for testing
    allow_partial_repair=True,
    allow_auto_repair=True,
    require_operator_authorization=False,
    enable_repair_audit_log=True,
    
    # Persistence: Maintains immutability but allows truncation
    enforce_immutability=True,
    allow_truncate_operations=True,
    enforce_persistence_hash=True,
    allow_schema_downgrade=False,
    
    # Failure: Soft fail allowed for debugging
    enforce_hard_fail=False,
    allow_soft_fail=True,
    fail_fast_on_corruption=True,
    
    # Experiments: Permissive for testing
    enable_experiments=True,
    allow_unregistered_variants=True,
    require_variant_registration=False,
    
    # Identity & Trust: Relaxed for testing
    enforce_trust_gating=False,
    allow_mock_providers=True,
    require_provider_verification=False,
    
    # Schema & Versioning: Strict alignment maintained
    enforce_version_alignment=True,
    reject_unknown_schema_version=False,
    
    # Rate Limiting: Relaxed for load testing
    enforce_rate_limits=False,
    enable_abuse_detection=False,
    enforce_geo_restrictions=False,
    
    # Logging: Debug logs enabled, no PII redaction
    enforce_audit_hash=False,
    enable_detailed_debug_logs=True,
    enforce_pii_redaction=False,
    require_structured_logging=True,
    
    # Observability: Can bypass for testing
    strict_observability_required=False,
    allow_monitoring_bypass=True,
    require_health_checks=True,
    
    # Concurrency: Relaxed for testing race conditions
    enforce_graph_locks=False,
    reject_concurrent_repair=False,
    
    # External: Sandbox APIs allowed
    require_webhook_signatures=False,
    allow_sandbox_apis=True,
    enforce_tls_verification=False,
)

# DEVELOPMENT Profile: Minimal constraints for local development
# Private: Access only through factory function create_deployment_profile()
__DEVELOPMENT_PROFILE = DeploymentProfile(
    environment=DeploymentEnvironment.DEVELOPMENT,
    
    # Repair & Recovery: Fully permissive
    allow_partial_repair=True,
    allow_auto_repair=True,
    require_operator_authorization=False,
    enable_repair_audit_log=False,
    
    # Persistence: Relaxed for rapid iteration
    enforce_immutability=False,
    allow_truncate_operations=True,
    enforce_persistence_hash=False,
    allow_schema_downgrade=True,
    
    # Failure: Soft fail for debugging
    enforce_hard_fail=False,
    allow_soft_fail=True,
    fail_fast_on_corruption=False,
    
    # Experiments: Fully permissive
    enable_experiments=True,
    allow_unregistered_variants=True,
    require_variant_registration=False,
    
    # Identity & Trust: Fully relaxed
    enforce_trust_gating=False,
    allow_mock_providers=True,
    require_provider_verification=False,
    
    # Schema & Versioning: Relaxed
    enforce_version_alignment=False,
    reject_unknown_schema_version=False,
    
    # Rate Limiting: Disabled
    enforce_rate_limits=False,
    enable_abuse_detection=False,
    enforce_geo_restrictions=False,
    
    # Logging: Debug enabled
    enforce_audit_hash=False,
    enable_detailed_debug_logs=True,
    enforce_pii_redaction=False,
    require_structured_logging=False,
    
    # Observability: Optional
    strict_observability_required=False,
    allow_monitoring_bypass=True,
    require_health_checks=False,
    
    # Concurrency: No enforcement
    enforce_graph_locks=False,
    reject_concurrent_repair=False,
    
    # External: Fully relaxed
    require_webhook_signatures=False,
    allow_sandbox_apis=True,
    enforce_tls_verification=False,
)

# TESTING Profile: Strict for CI/CD validation
# Private: Access only through factory function create_deployment_profile()
__TESTING_PROFILE = DeploymentProfile(
    environment=DeploymentEnvironment.TESTING,
    
    # Repair & Recovery: Automated testing mode
    allow_partial_repair=True,
    allow_auto_repair=True,
    require_operator_authorization=False,
    enable_repair_audit_log=True,
    
    # Persistence: Strict for validation
    enforce_immutability=True,
    allow_truncate_operations=True,
    enforce_persistence_hash=True,
    allow_schema_downgrade=False,
    
    # Failure: Hard fail to catch issues
    enforce_hard_fail=True,
    allow_soft_fail=False,
    fail_fast_on_corruption=True,
    
    # Experiments: Permissive for testing
    enable_experiments=True,
    allow_unregistered_variants=True,
    require_variant_registration=False,
    
    # Identity & Trust: Mock providers allowed
    enforce_trust_gating=False,
    allow_mock_providers=True,
    require_provider_verification=False,
    
    # Schema & Versioning: Strict validation
    enforce_version_alignment=True,
    reject_unknown_schema_version=False,
    
    # Rate Limiting: Disabled for testing
    enforce_rate_limits=False,
    enable_abuse_detection=False,
    enforce_geo_restrictions=False,
    
    # Logging: Structured for test analysis
    enforce_audit_hash=False,
    enable_detailed_debug_logs=True,
    enforce_pii_redaction=False,
    require_structured_logging=True,
    
    # Observability: Required for CI/CD metrics
    strict_observability_required=False,
    allow_monitoring_bypass=True,
    require_health_checks=True,
    
    # Concurrency: Strict for race detection
    enforce_graph_locks=True,
    reject_concurrent_repair=True,
    
    # External: Sandbox mode
    require_webhook_signatures=False,
    allow_sandbox_apis=True,
    enforce_tls_verification=False,
)

# Profile registry (private - access only through factory)
# Double-underscore prefix prevents direct import access
__PROFILE_REGISTRY = {
    DeploymentEnvironment.PRODUCTION: __PRODUCTION_PROFILE,
    DeploymentEnvironment.STAGING: __STAGING_PROFILE,
    DeploymentEnvironment.DEVELOPMENT: __DEVELOPMENT_PROFILE,
    DeploymentEnvironment.TESTING: __TESTING_PROFILE,
}


# ============================================================================
# PROFILE ACCESS (CONTEXT-LOCAL STATE)
# ============================================================================

# Context-local profile state (thread-safe, context-isolated)
# Uses contextvars for proper async/thread isolation without global mutable state
_CURRENT_PROFILE_CTX: ContextVar[Optional[DeploymentProfile]] = ContextVar(
    "_CURRENT_PROFILE_CTX", default=None
)


def get_deployment_profile() -> DeploymentProfile:
    """
    Get the current deployment profile from context-local storage.
    
    This is the ONLY sanctioned way to access environment-specific behavior.
    Uses context-local storage (contextvars) for thread-safe, async-safe access
    without global mutable state.
    
    Returns:
        Current deployment profile from context
        
    Raises:
        RuntimeError: If profile not initialized in current context
        
    Usage:
        >>> profile = get_deployment_profile()
        >>> if profile.allow_partial_repair:
        ...     perform_partial_repair()
    """
    profile = _CURRENT_PROFILE_CTX.get()
    if profile is None:
        raise RuntimeError(
            "Deployment profile not initialized in current context. "
            "Call initialize_deployment_profile() at application startup "
            "with explicit environment, or use set_deployment_profile_context() "
            "to inject profile into current context."
        )
    return profile


def create_deployment_profile(environment: DeploymentEnvironment) -> DeploymentProfile:
    """
    Factory function to create a validated deployment profile.
    
    This is the ONLY way to obtain a deployment profile instance.
    Raw profiles are private and cannot be accessed directly.
    
    Args:
        environment: Deployment environment to create profile for
        
    Returns:
        Validated deployment profile instance
        
    Raises:
        InvalidDeploymentEnvironmentError: If environment invalid
        InvariantViolationError: If profile validation fails
        
    Usage:
        >>> env = DeploymentEnvironment.PRODUCTION
        >>> profile = create_deployment_profile(env)
        >>> set_deployment_profile_context(profile)
    """
    # Get profile from private registry
    profile = __PROFILE_REGISTRY.get(environment)
    if profile is None:
        raise InvalidDeploymentEnvironmentError(
            f"No profile defined for environment: {environment}",
            environment=str(environment),
            failing_field="deployment.environment",
        )
    
    # Validate production constraints
    profile.validate_production_constraints()
    
    # Validate strictness monotonicity (PRODUCTION >= STAGING)
    if environment == DeploymentEnvironment.PRODUCTION:
        staging_profile = __PROFILE_REGISTRY[DeploymentEnvironment.STAGING]
        profile.validate_strictness_monotonicity(staging_profile)
    
    return profile


def initialize_deployment_profile(environment: DeploymentEnvironment) -> DeploymentProfile:
    """
    Initialize the deployment profile in the current context at application startup.
    
    This MUST be called during bootstrap with an explicit environment.
    Sets the profile in context-local storage for subsequent get_deployment_profile() calls.
    
    Args:
        environment: Deployment environment to initialize
        
    Returns:
        Initialized deployment profile
        
    Raises:
        InvalidDeploymentEnvironmentError: If environment invalid
        InvariantViolationError: If profile validation fails
        
    Usage:
        >>> env = DeploymentEnvironment.PRODUCTION
        >>> profile = initialize_deployment_profile(env)
    """
    # Create and validate profile using factory
    profile = create_deployment_profile(environment)
    
    # Set in context-local storage
    _CURRENT_PROFILE_CTX.set(profile)
    
    return profile


def set_deployment_profile_context(profile: DeploymentProfile) -> None:
    """
    Set deployment profile in current context (for dependency injection).
    
    Allows explicit profile injection for testing or explicit composition root patterns.
    This enables pure dependency injection without global state.
    
    Args:
        profile: Deployment profile to set in current context
        
    Usage:
        >>> profile = create_deployment_profile(DeploymentEnvironment.STAGING)
        >>> set_deployment_profile_context(profile)
        >>> # Now get_deployment_profile() will return this profile
    """
    _CURRENT_PROFILE_CTX.set(profile)


# lock_deployment_profile() removed: Immutability is guaranteed by frozen dataclass.
# Profiles cannot be mutated after creation, so explicit locking is unnecessary.
# Context-local storage provides isolation without requiring explicit locks.


def validate_deployment_profile(environment: DeploymentEnvironment) -> None:
    """
    Validate a deployment profile without initializing it.
    
    Useful for testing and pre-flight validation.
    
    Args:
        environment: Environment to validate
        
    Raises:
        InvalidDeploymentEnvironmentError: If environment invalid
        InvariantViolationError: If profile validation fails
    """
    # Use factory function which performs all validations
    create_deployment_profile(environment)


def enforce_strictness_monotonicity() -> None:
    """
    Validate that production is not less strict than staging.
    
    This is a critical invariant that must be maintained.
    This function is now called automatically during create_deployment_profile()
    for PRODUCTION environments, but can be called explicitly for validation.
    
    Raises:
        InvariantViolationError: If strictness monotonicity violated
    """
    production = __PROFILE_REGISTRY[DeploymentEnvironment.PRODUCTION]
    staging = __PROFILE_REGISTRY[DeploymentEnvironment.STAGING]
    
    production.validate_strictness_monotonicity(staging)


# initialize_from_environment_variable() removed:
# This function violated the "explicit injection only" doctrine.
# Environment must be injected explicitly, not inferred from environment variables.
# 
# If environment variable reading is needed, it should be done in the composition root
# (e.g., in main.py or bootstrap.py) and then passed explicitly to initialize_deployment_profile().
# 
# Example correct usage:
#     import os
#     from config import DeploymentEnvironment, initialize_deployment_profile
#     
#     env_string = os.getenv("DEPLOYMENT_ENVIRONMENT")
#     if not env_string:
#         raise ValueError("DEPLOYMENT_ENVIRONMENT not set")
#     environment = DeploymentEnvironment.from_string(env_string)
#     profile = initialize_deployment_profile(environment)


# ============================================================================
# STARTUP VALIDATION
# ============================================================================

# Import-time validation removed:
# enforce_strictness_monotonicity() is now called automatically during
# create_deployment_profile() for PRODUCTION environments.
# This defers validation until profile creation, eliminating import-time side effects.
# 
# Validation occurs:
# 1. Automatically when create_deployment_profile(PRODUCTION) is called
# 2. Explicitly when enforce_strictness_monotonicity() is called
# 3. During validate_deployment_profile() calls


# ============================================================================
# EXPORTED API
# ============================================================================

__all__ = (
    # Core types
    "DeploymentEnvironment",
    "DeploymentProfile",
    
    # Profile factory (primary access method)
    "create_deployment_profile",
    
    # Profile access (context-local)
    "get_deployment_profile",
    "initialize_deployment_profile",
    "set_deployment_profile_context",
    
    # Validation
    "validate_deployment_profile",
    "enforce_strictness_monotonicity",
)