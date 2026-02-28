"""
/config/config_policy.py

Global Configuration Policy
(What Is Allowed To Vary)

This module defines which configuration fields are allowed to vary across
environments, and under what constraints.

CRITICAL PRINCIPLES:
- Not all configuration is equal
- Some config may vary per environment (e.g., logging level)
- Some config may vary per region (e.g., object store bucket)
- Some config must never vary (e.g., window definitions, computation semantics)
- Some config may vary within bounds (e.g., rate limits)

ABSOLUTE INVARIANTS:
1. If a field affects replay semantics → it is IMMUTABLE and IDENTITY_SENSITIVE
2. If a field affects window definitions → it is IMMUTABLE
3. If a field affects computation spec → it is IMMUTABLE
4. Changing immutable fields requires system version bump and full replay
5. No silent policy relaxation allowed
6. Policy enforcement is deterministic (no time/env/RNG dependence)

This is governance, not validation.
This is doctrine, not defaults.
This is the "you cannot do that" layer.

Without policy:
- Systems drift
- Replay lies
- Engineers break invariants accidentally

With policy:
- Config becomes law
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, FrozenSet
from types import MappingProxyType
import logging

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
try:
    from .config_types import SystemConfig
except ImportError:
    try:
        from config.config_types import SystemConfig
    except ImportError:
        # Fallback - will need to be defined
        SystemConfig = Any


# ============================================================================
# Policy Categories
# ============================================================================


class PolicyCategory(Enum):
    """Configuration policy categories defining variation constraints."""
    
    IMMUTABLE = "immutable"  # Never varies across deployments
    BOUNDED = "bounded"  # Varies within numeric bounds
    ENV_SCOPED = "env_scoped"  # Varies by environment only
    RUNTIME_ONLY = "runtime_only"  # Excluded from identity hash


# ============================================================================
# Policy Violations
# ============================================================================


class ConfigPolicyViolation(ConfigurationError):
    """
    Raised when configuration violates policy constraints.
    
    This is a FATAL error that must halt system startup.
    Policy violations mean the system cannot trust its configuration.
    
    Categories:
    - ImmutableFieldChanged: Immutable field modified
    - OutOfBounds: Bounded field exceeds constraints
    - DisallowedVariation: Field varies in disallowed context
    - IdentityImpactViolation: Identity-sensitive field handling incorrect
    - EnvironmentLeakage: Environment-scoped config leaks across boundaries
    """
    
    def __init__(
        self,
        field_path: str,
        violation_type: str,
        message: str,
        current_value: Any = None,
        expected: Any = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        """
        Initialize policy violation error.
        
        Args:
            field_path: Dot-separated field path (e.g., "windows.window_size_ms")
            violation_type: Violation category
            message: Human-readable error description
            current_value: Current field value (if applicable)
            expected: Expected value (if applicable)
            environment: Deployment environment
            config_version: Configuration schema version
        """
        self.field_path = field_path
        self.violation_type = violation_type
        self.current_value = current_value
        self.expected = expected
        
        full_message = (
            f"Policy violation in '{field_path}' [{violation_type}]: {message}"
        )
        if current_value is not None:
            full_message += f" (current: {current_value})"
        if expected is not None:
            full_message += f" (expected: {expected})"
        
        super().__init__(
            full_message,
            error_type=violation_type,
            environment=environment,
            config_version=config_version,
            failing_field=field_path,
        )


class ImmutableFieldChanged(ConfigPolicyViolation):
    """
    Raised when an immutable field has been modified.
    
    Immutable fields must never vary.
    Changing them requires system version bump and full replay.
    """
    
    def __init__(
        self,
        field_path: str,
        current: Any,
        canonical: Any,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            field_path=field_path,
            violation_type="ImmutableFieldChanged",
            message=(
                "Immutable field cannot be changed. "
                "Requires system version bump and full replay."
            ),
            current_value=current,
            expected=canonical,
            environment=environment,
            config_version=config_version,
        )


class OutOfBounds(ConfigPolicyViolation):
    """
    Raised when a bounded field exceeds declared constraints.
    
    Bounded fields may vary but only within declared numeric constraints.
    Must enforce numeric bounds explicitly.
    """
    
    def __init__(
        self,
        field_path: str,
        value: Any,
        min_val: Optional[Any] = None,
        max_val: Optional[Any] = None,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        bounds = []
        if min_val is not None:
            bounds.append(f"min={min_val}")
        if max_val is not None:
            bounds.append(f"max={max_val}")
        bounds_str = ", ".join(bounds) if bounds else "no bounds declared"
        
        super().__init__(
            field_path=field_path,
            violation_type="OutOfBounds",
            message=f"Value exceeds allowed bounds ({bounds_str})",
            current_value=value,
            expected=f"[{min_val}, {max_val}]" if min_val is not None and max_val is not None else None,
            environment=environment,
            config_version=config_version,
        )


class DisallowedVariation(ConfigPolicyViolation):
    """
    Raised when a field varies in a disallowed context.
    
    Fields may only vary within their declared policy constraints.
    """
    
    def __init__(
        self,
        field_path: str,
        context: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            field_path=field_path,
            violation_type="DisallowedVariation",
            message=f"Field not allowed to vary in context: {context}",
            environment=environment,
            config_version=config_version,
        )


class IdentityImpactViolation(ConfigPolicyViolation):
    """
    Raised when identity-sensitive field handling is incorrect.
    
    Identity-sensitive fields affect config identity hash.
    Policy must define which fields affect identity hash.
    """
    
    def __init__(
        self,
        field_path: str,
        message: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            field_path=field_path,
            violation_type="IdentityImpactViolation",
            message=message,
            environment=environment,
            config_version=config_version,
        )


class EnvironmentLeakage(ConfigPolicyViolation):
    """
    Raised when environment-scoped config leaks across boundaries.
    
    Environment-scoped fields may vary by environment but must not
    leak across environment boundaries or affect replay determinism.
    """
    
    def __init__(
        self,
        field_path: str,
        message: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            field_path=field_path,
            violation_type="EnvironmentLeakage",
            message=message,
            environment=environment,
            config_version=config_version,
        )


class UngovernedFieldViolation(ConfigPolicyViolation):
    """
    Raised when a configuration field exists without a declared policy.
    
    Every field must either:
    - have a declared policy
    - OR be explicitly marked "governance_exempt"
    
    Otherwise → violation.
    
    This prevents silent semantic drift via:
    - new config flags
    - experimental knobs
    - replay-affecting parameters accidentally added
    """
    
    def __init__(
        self,
        field_path: str,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ):
        super().__init__(
            field_path=field_path,
            violation_type="UngovernedFieldViolation",
            message=(
                "Field exists in configuration but has no declared policy. "
                "Every field must be governed by policy or explicitly marked governance_exempt."
            ),
            environment=environment,
            config_version=config_version,
        )


# ============================================================================
# Field Policy Declaration
# ============================================================================


@dataclass(frozen=True)
class FieldPolicy:
    """
    Policy declaration for a single configuration field.
    
    DECLARATIVE: Policy must be explicit, not inferred.
    No dynamic constraints. No environment-dependent rules.
    
    Attributes:
        category: Policy category determining variation constraints
        min_value: Minimum allowed value for BOUNDED fields
        max_value: Maximum allowed value for BOUNDED fields
        identity_sensitive: Whether field affects config identity hash
        canonical_value: Expected value for IMMUTABLE fields (baseline reference)
        baseline_value: Baseline value for version-aware immutable comparison
        optional: FIX #1: If True, field may be missing from config. If False, field is required.
        description: Optional human-readable description of policy
    """
    
    category: PolicyCategory
    """Policy category (IMMUTABLE, BOUNDED, ENV_SCOPED, RUNTIME_ONLY)"""
    
    min_value: Optional[int | float] = None
    """Minimum allowed value for BOUNDED fields"""
    
    max_value: Optional[int | float] = None
    """Maximum allowed value for BOUNDED fields"""
    
    identity_sensitive: bool = True
    """Whether field affects config identity hash"""
    
    canonical_value: Any = None
    """Expected value for IMMUTABLE fields (legacy, use baseline_value for version-aware)"""
    
    baseline_value: Any = None
    """Baseline value for version-aware immutable comparison (preferred over canonical_value)"""
    
    optional: bool = False
    """FIX #1: If True, field may be missing from config. If False, field is required."""
    
    description: Optional[str] = None
    """Optional human-readable description of policy"""
    
    def __post_init__(self):
        """Validate policy declaration consistency."""
        if self.category == PolicyCategory.BOUNDED:
            if self.min_value is None and self.max_value is None:
                raise ValueError(
                    f"BOUNDED policy must declare at least one bound"
                )
        
        if self.category == PolicyCategory.IMMUTABLE:
            if not self.identity_sensitive:
                raise ValueError(
                    "IMMUTABLE fields must be identity_sensitive=True"
                )
        
        if self.category == PolicyCategory.RUNTIME_ONLY:
            if self.identity_sensitive:
                raise ValueError(
                    "RUNTIME_ONLY fields must be identity_sensitive=False"
                )
        
        if self.category == PolicyCategory.ENV_SCOPED:
            if self.identity_sensitive:
                raise ValueError(
                    "ENV_SCOPED fields must be identity_sensitive=False"
                )


# ============================================================================
# Policy Registry
# ============================================================================


class PolicyRegistry:
    """
    Central registry of field policies.
    
    This is the single source of truth for configuration governance.
    Defines which fields are immutable, bounded, environment-scoped,
    or runtime-only, along with their constraints.
    
    CRITICAL LAW:
    If a field:
    - affects replay semantics
    - affects window definitions
    - affects computation spec
    
    Then it is IMMUTABLE and IDENTITY_SENSITIVE.
    Changing it requires system version bump and full replay.
    """
    
    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        governance_exempt_allowlist: Optional[Set[str]] = None,
    ):
        """
        Initialize policy registry.
        
        FIX #2: Governance exempt fields must be explicitly allowlisted at initialization.
        This prevents runtime abuse of the exemption mechanism.
        
        Args:
            logger: Optional logger for structured logging
            governance_exempt_allowlist: Explicit allowlist of fields that may be
                                        marked as governance exempt. If None, empty set (no exemptions).
        """
        self._policies: Dict[str, FieldPolicy] = {}
        self._governance_exempt_fields: Set[str] = set()
        self._governance_exempt_allowlist: Set[str] = governance_exempt_allowlist or set()
        self._governance_exempt_audit_log: List[Tuple[str, str, float]] = []  # (field_path, reason, timestamp)
        self._logger = logger or logging.getLogger(__name__)
        self._register_policies()
        
        # FIX #8: Validate constitutional integrity at initialization
        self._validate_registry_integrity()
        
        self._logger.debug(
            f"PolicyRegistry initialized: {len(self._policies)} policies registered, "
            f"governance_exempt_allowlist={len(self._governance_exempt_allowlist)} fields"
        )
    
    def _register_policies(self) -> None:
        """Register all field policies. This is the constitution."""
        
        # ====================================================================
        # IMMUTABLE: Window Definitions
        # ====================================================================
        # These define computation semantics. Changing them requires
        # full replay and system version bump.
        
        self._register(
            "windows.window_size_ms",
            PolicyCategory.IMMUTABLE,
            canonical_value=60000,
        )
        
        self._register(
            "windows.allowed_lateness_ms",
            PolicyCategory.IMMUTABLE,
            canonical_value=30000,
        )
        
        self._register(
            "windows.watermark_idle_timeout_ms",
            PolicyCategory.IMMUTABLE,
            canonical_value=300000,
        )
        
        # ====================================================================
        # IMMUTABLE: Computation Semantics
        # ====================================================================
        # Core computation definitions that affect replay correctness.
        
        self._register(
            "computation.version",
            PolicyCategory.IMMUTABLE,
            canonical_value="1.0.0",
        )
        
        self._register(
            "computation.aggregation_function",
            PolicyCategory.IMMUTABLE,
            canonical_value="sum",
        )
        
        self._register(
            "computation.deduplication_window_ms",
            PolicyCategory.IMMUTABLE,
            canonical_value=60000,
        )
        
        # ====================================================================
        # IMMUTABLE: Schema Versions
        # ====================================================================
        # Schema versions must not vary to ensure data compatibility.
        
        self._register(
            "schema.event_version",
            PolicyCategory.IMMUTABLE,
            canonical_value="2.0",
        )
        
        self._register(
            "schema.state_version",
            PolicyCategory.IMMUTABLE,
            canonical_value="1.0",
        )
        
        # ====================================================================
        # BOUNDED: Processing Limits
        # ====================================================================
        # These may vary but only within safe operational bounds.
        
        self._register(
            "limits.max_events_per_run",
            PolicyCategory.BOUNDED,
            min_value=1000,
            max_value=10000,
        )
        
        self._register(
            "limits.max_batch_size",
            PolicyCategory.BOUNDED,
            min_value=100,
            max_value=5000,
        )
        
        self._register(
            "limits.max_memory_mb",
            PolicyCategory.BOUNDED,
            min_value=512,
            max_value=8192,
        )
        
        self._register(
            "limits.retry_count",
            PolicyCategory.BOUNDED,
            min_value=0,
            max_value=5,
        )
        
        self._register(
            "limits.timeout_seconds",
            PolicyCategory.BOUNDED,
            min_value=30,
            max_value=600,
        )
        
        # ====================================================================
        # ENV_SCOPED: Logging and Observability
        # ====================================================================
        # These may vary by environment but don't affect replay semantics.
        
        self._register(
            "logging.level",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        self._register(
            "logging.format",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        self._register(
            "telemetry.sampling_rate",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        self._register(
            "telemetry.export_interval_seconds",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        # ====================================================================
        # ENV_SCOPED: Infrastructure Configuration
        # ====================================================================
        # Infrastructure details that vary by deployment.
        
        self._register(
            "storage.bucket_name",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        self._register(
            "storage.region",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        self._register(
            "cache.size_mb",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        self._register(
            "cache.ttl_seconds",
            PolicyCategory.ENV_SCOPED,
            identity_sensitive=False,
        )
        
        # ====================================================================
        # RUNTIME_ONLY: Secrets and Credentials
        # ====================================================================
        # These must never be part of config identity hash.
        
        self._register(
            "secrets.database_url",
            PolicyCategory.RUNTIME_ONLY,
            identity_sensitive=False,
        )
        
        self._register(
            "secrets.api_key",
            PolicyCategory.RUNTIME_ONLY,
            identity_sensitive=False,
        )
        
        self._register(
            "secrets.signing_key",
            PolicyCategory.RUNTIME_ONLY,
            identity_sensitive=False,
        )
        
        self._register(
            "secrets.encryption_key",
            PolicyCategory.RUNTIME_ONLY,
            identity_sensitive=False,
        )
    
    def _register(
        self,
        field_path: str,
        category: PolicyCategory,
        min_value: Optional[int | float] = None,
        max_value: Optional[int | float] = None,
        identity_sensitive: bool = True,
        canonical_value: Any = None,
        baseline_value: Any = None,
        optional: bool = False,
        description: Optional[str] = None,
    ) -> None:
        """
        Register a field policy.
        
        Args:
            field_path: Dot-separated field path (e.g., "windows.window_size_ms")
            category: Policy category
            min_value: Minimum value for BOUNDED fields
            max_value: Maximum value for BOUNDED fields
            identity_sensitive: Whether field affects config identity hash
            canonical_value: Expected value for IMMUTABLE fields (legacy, use baseline_value)
            baseline_value: Baseline value for version-aware immutable comparison (preferred)
            optional: FIX #1: If True, field may be missing. If False, field is required.
            description: Optional description of policy
        """
        if not field_path:
            raise ValueError("field_path cannot be empty")
        
        if field_path in self._policies:
            self._logger.warning(
                f"Overwriting existing policy for field: {field_path}"
            )
        
        # FIX #1: IMMUTABLE fields cannot be optional
        if category == PolicyCategory.IMMUTABLE and optional:
            raise ValueError(
                f"IMMUTABLE field '{field_path}' cannot be optional. "
                f"Immutable fields are required and define computation semantics."
            )
        
        policy = FieldPolicy(
            category=category,
            min_value=min_value,
            max_value=max_value,
            identity_sensitive=identity_sensitive,
            canonical_value=canonical_value,
            baseline_value=baseline_value,
            optional=optional,
            description=description,
        )
        
        self._policies[field_path] = policy
        
        self._logger.debug(
            f"Registered policy: path={field_path}, category={category.value}, "
            f"identity_sensitive={identity_sensitive}"
        )
    
    def get_policy(self, field_path: str) -> Optional[FieldPolicy]:
        """Get policy for a field path, or None if not registered."""
        return self._policies.get(field_path)
    
    def get_all_policies(self) -> MappingProxyType[str, FieldPolicy]:
        """
        Get all registered policies.
        
        Returns:
            Immutable mapping of field paths to policies
        """
        return MappingProxyType(self._policies)
    
    def get_identity_sensitive_fields(self) -> Set[str]:
        """Get all fields that affect config identity hash."""
        return {
            path for path, policy in self._policies.items()
            if policy.identity_sensitive
        }
    
    def get_identity_excluded_fields(self) -> Set[str]:
        """Get all fields excluded from config identity hash."""
        return {
            path for path, policy in self._policies.items()
            if not policy.identity_sensitive
        }
    
    def _validate_registry_integrity(self) -> None:
        """
        FIX #8: Validate constitutional integrity of policy registry.
        
        CRITICAL: This is the constitutional integrity check that runs at
        registry initialization. It ensures the policy registry itself is
        internally consistent and doctrinally correct.
        
        Ensures:
        - No duplicate semantic paths
        - No conflicting category assignments
        - No identity flag contradictions
        - Cross-category invariants hold (FIX #5)
        - Global identity sensitivity rules enforced (FIX #6)
        
        This prevents the constitution itself from being internally inconsistent.
        """
        violations: List[str] = []
        
        # Check for duplicate paths (shouldn't happen, but validate)
        seen_paths: Set[str] = set()
        for path in self._policies.keys():
            if path in seen_paths:
                violations.append(f"Duplicate policy path: {path}")
            seen_paths.add(path)
        
        # ========================================================================
        # CROSS-CATEGORY INVARIANT VALIDATION (FIX #5, #6)
        # ========================================================================
        # CRITICAL: These are constitutional laws that must hold across all policies.
        # They ensure category semantics are strictly enforced and identity
        # sensitivity rules are globally consistent.
        #
        # Invariants:
        # - IMMUTABLE → identity_sensitive=True (enforced)
        # - RUNTIME_ONLY → identity_sensitive=False (enforced)
        # - ENV_SCOPED → identity_sensitive=False (enforced)
        # - RUNTIME_ONLY → no bounds (min_value/max_value must be None)
        # - BOUNDED fields warned if identity_sensitive (documentation required)
        # ========================================================================
        for field_path, policy in self._policies.items():
            # IMMUTABLE must NOT be env-scoped (already enforced in __post_init__)
            if policy.category == PolicyCategory.IMMUTABLE:
                if not policy.identity_sensitive:
                    violations.append(
                        f"IMMUTABLE field '{field_path}' must be identity_sensitive=True"
                    )
            
            # RUNTIME_ONLY must never be bounded (no min/max values)
            if policy.category == PolicyCategory.RUNTIME_ONLY:
                if policy.min_value is not None or policy.max_value is not None:
                    violations.append(
                        f"RUNTIME_ONLY field '{field_path}' cannot have bounds (min_value/max_value)"
                    )
                if policy.identity_sensitive:
                    violations.append(
                        f"RUNTIME_ONLY field '{field_path}' must be identity_sensitive=False"
                    )
            
            # ENV_SCOPED must never be identity_sensitive
            if policy.category == PolicyCategory.ENV_SCOPED:
                if policy.identity_sensitive:
                    violations.append(
                        f"ENV_SCOPED field '{field_path}' must be identity_sensitive=False"
                    )
            
            # FIX #5: BOUNDED fields must NOT be identity-sensitive unless explicitly declared
            # (This is a strict check - bounded fields typically should not affect identity)
            if policy.category == PolicyCategory.BOUNDED:
                if policy.identity_sensitive:
                    # Allow but log warning - some bounded fields may legitimately affect identity
                    self._logger.warning(
                        f"BOUNDED field '{field_path}' is identity_sensitive=True. "
                        f"Ensure this is intentional and documented."
                    )
        
        if violations:
            raise ValueError(
                f"Policy registry integrity violations:\n" + "\n".join(f"  - {v}" for v in violations)
            )
    
    def mark_governance_exempt(
        self,
        field_path: str,
        reason: str = "No reason provided",
    ) -> None:
        """
        Mark a field as exempt from mandatory governance coverage.
        
        FIX #2: Governance exempt fields must be in the allowlist set at initialization.
        This prevents runtime abuse and requires explicit declaration of exemptions.
        
        This should be used extremely sparingly, only for fields that:
        - Are truly experimental/transient
        - Cannot be governed by policy
        - Are explicitly documented as exempt
        - Are in the governance_exempt_allowlist
        
        Args:
            field_path: Field path to mark as exempt
            reason: Required reason for exemption (audit trail)
        
        Raises:
            ValueError: If field_path is not in the governance_exempt_allowlist
        """
        import time
        
        # FIX #2: Enforce allowlist - field must be explicitly allowlisted
        if field_path not in self._governance_exempt_allowlist:
            raise ValueError(
                f"Field '{field_path}' is not in governance_exempt_allowlist. "
                f"Governance exemptions must be explicitly allowlisted at registry initialization. "
                f"Current allowlist: {sorted(self._governance_exempt_allowlist)}"
            )
        
        # Audit log entry
        timestamp = time.time()
        self._governance_exempt_audit_log.append((field_path, reason, timestamp))
        
        self._governance_exempt_fields.add(field_path)
        self._logger.warning(
            f"Field '{field_path}' marked as governance_exempt: {reason} "
            f"(audit log entry #{len(self._governance_exempt_audit_log)})"
        )
    
    def is_governance_exempt(self, field_path: str) -> bool:
        """Check if a field is marked as governance exempt."""
        return field_path in self._governance_exempt_fields
    
    def get_governance_exempt_audit_log(self) -> List[Tuple[str, str, float]]:
        """
        Get audit log of governance exemptions.
        
        FIX #2: Provides audit trail of all governance exemptions for compliance.
        
        Returns:
            List of (field_path, reason, timestamp) tuples
        """
        return list(self._governance_exempt_audit_log)


# ============================================================================
# Policy Enforcement
# ============================================================================


class ConfigPolicy:
    """
    Configuration policy enforcement engine.
    
    This is the "you cannot do that" layer.
    
    Enforces policy constraints on resolved configuration.
    Enforcement is deterministic, non-mutating, and fail-fast.
    
    Enforcement timing:
    - After resolution
    - Before hashing
    - Before pipeline initialization
    
    Order: Loader → Resolver → Schema → Policy → Hashing
    
    If policy fails → system must not start.
    
    FORBIDDEN BEHAVIOR:
    - Auto-adjust out-of-range values
    - Downgrade violations to warnings
    - Allow override with flag
    - Treat immutable as "strongly discouraged"
    - Environment-dependent rule sets
    - Mutate config to comply
    
    Policy enforces. It does not fix.
    """
    
    def __init__(
        self,
        registry: Optional[PolicyRegistry] = None,
        logger: Optional[logging.Logger] = None,
        baseline_config: Optional[SystemConfig] = None,
        baseline_env_configs: Optional[Dict[str, SystemConfig]] = None,
        hash_module_validator: Optional[Any] = None,
    ):
        """
        Initialize policy enforcement engine.
        
        Args:
            registry: Policy registry (defaults to new instance)
            logger: Optional logger for structured logging
            baseline_config: Baseline config for version-aware immutable comparison (FIX #3)
            baseline_env_configs: Per-environment baseline configs for ENV_SCOPED validation (FIX #7)
            hash_module_validator: Optional validator for hash module alignment (FIX #3)
                                 Must have get_identity_sensitive_fields() and get_identity_excluded_fields()
        """
        self.registry = registry or PolicyRegistry(logger=logger)
        self._logger = logger or logging.getLogger(__name__)
        self._baseline_config = baseline_config
        self._baseline_env_configs = baseline_env_configs or {}
        self._hash_module_validator = hash_module_validator
        
        # FIX #3: Validate hash module alignment at initialization
        if self._hash_module_validator is not None:
            self._validate_hash_module_alignment()
    
    def enforce(
        self,
        config: SystemConfig,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Enforce policy constraints on resolved configuration.
        
        DETERMINISTIC: Same config always produces same enforcement result.
        No randomness. No hidden state. No environment dependencies.
        
        This method:
        - Performs hard checks
        - Raises ConfigPolicyViolation on failure
        - Does NOT mutate config
        - Does NOT return modified config
        
        Args:
            config: Resolved system configuration
            environment: Deployment environment (for error context)
            config_version: Configuration schema version (for error context)
        
        Raises:
            ConfigPolicyViolation: If any policy constraint is violated
        
        Critical Invariant:
            This method NEVER mutates config. It only validates.
            If policy fails, system must not start.
        """
        self._logger.debug(
            f"Enforcing policy constraints: environment={environment}, "
            f"config_version={config_version}"
        )
        
        violations: List[ConfigPolicyViolation] = []
        
        # ========================================================================
        # CONSTITUTIONAL ENFORCEMENT (FIX #1, #9)
        # ========================================================================
        # CRITICAL: Policy enforcement is driven by DECLARED POLICIES, not
        # discovered config fields. This makes the registry the authoritative
        # source of truth - the constitution defines what is governed.
        #
        # Enforcement order: sorted(registry.get_all_policies().keys())
        # NOT: sorted(extracted runtime fields)
        #
        # This ensures:
        # - New fields cannot exist without explicit policy declaration
        # - Constitutional law drives evaluation, not runtime config shape
        # - Deterministic, registry-ordered enforcement
        # ========================================================================
        all_policies = self.registry.get_all_policies()
        
        self._logger.debug(
            f"Enforcing {len(all_policies)} declared policies (constitutional order)"
        )
        
        # Track which config fields we've seen (for ungoverned field detection)
        governed_config_fields: Set[str] = set()
        
        # FIX #1 & #9: Enforcement order derives from registry constitution
        # Iterate over DECLARED POLICIES, not discovered config fields
        for field_path in sorted(all_policies.keys()):  # Deterministic constitutional order
            policy = all_policies[field_path]
            
            try:
                value = self._get_field_value(config, field_path)
                governed_config_fields.add(field_path)
            except (KeyError, AttributeError) as e:
                # ====================================================================
                # MISSING FIELD HANDLING (FIX #1, #10)
                # ====================================================================
                # FIX #1: Optionality must be explicitly declared in policy.
                # - IMMUTABLE fields: Always required (cannot be optional)
                # - Other fields: Required unless policy.optional=True
                #
                # FIX #10: Missing immutable fields are FATAL violations.
                # Missing required non-immutable fields are also violations.
                # ====================================================================
                if policy.category == PolicyCategory.IMMUTABLE:
                    # IMMUTABLE fields are always required
                    violations.append(
                        ConfigPolicyViolation(
                            field_path=field_path,
                            violation_type="MissingImmutableField",
                            message=(
                                "Immutable field is missing from configuration. "
                                "Immutable fields are required and cannot be omitted. "
                                "This is a FATAL violation that prevents system startup."
                            ),
                            environment=environment,
                            config_version=config_version,
                        )
                    )
                    self._logger.error(
                        f"Missing immutable field: {field_path} (FATAL violation - system cannot start)"
                    )
                    continue
                elif not policy.optional:
                    # FIX #1: Non-optional, non-immutable fields are required
                    violations.append(
                        ConfigPolicyViolation(
                            field_path=field_path,
                            violation_type="MissingRequiredField",
                            message=(
                                f"Required field is missing from configuration. "
                                f"Field has policy.optional=False and must be present. "
                                f"To allow missing fields, set policy.optional=True when registering."
                            ),
                            environment=environment,
                            config_version=config_version,
                        )
                    )
                    self._logger.error(
                        f"Missing required field: {field_path} (policy.optional=False)"
                    )
                    continue
                else:
                    # Field is explicitly marked as optional - allowed to be missing
                    self._logger.debug(
                        f"Field {field_path} not present in config (policy.optional=True, allowed)"
                    )
                    continue
            
            try:
                self._enforce_field_policy(
                    field_path, value, policy, environment, config_version
                )
            except ConfigPolicyViolation as e:
                violations.append(e)
                self._logger.error(
                    f"Policy violation detected: path={field_path}, "
                    f"type={e.violation_type}, value={value}"
                )
        
        # ========================================================================
        # UNGOVERNED FIELD DETECTION (FIX #2)
        # ========================================================================
        # CRITICAL: Every field in config MUST have a declared policy OR be
        # explicitly marked governance_exempt. This prevents silent semantic drift.
        #
        # We extract config fields ONLY to detect ungoverned ones - NOT for
        # enforcement. Enforcement is driven by declared policies above.
        #
        # This ensures:
        # - No new config flags can exist without doctrine
        # - No experimental knobs can slip through
        # - No replay-affecting parameters accidentally added
        # ========================================================================
        all_config_fields = self._extract_field_paths(config)
        ungoverned_fields = all_config_fields - governed_config_fields
        
        for ungoverned_field in sorted(ungoverned_fields):
            # Skip if explicitly marked as governance exempt
            if self.registry.is_governance_exempt(ungoverned_field):
                self._logger.debug(
                    f"Field {ungoverned_field} is governance_exempt (allowed)"
                )
                continue
            
            # FIX #2: Ungoverned field is a FATAL violation
            # No silent drift allowed - every field must be governed
            violations.append(
                UngovernedFieldViolation(
                    field_path=ungoverned_field,
                    environment=environment,
                    config_version=config_version,
                )
            )
            self._logger.error(
                f"Ungoverned field detected: {ungoverned_field} (no policy declared) - FATAL"
            )
        
        # ========================================================================
        # IDENTITY HASH CONTRACT ENFORCEMENT (FIX #4)
        # ========================================================================
        # CRITICAL: Identity hash contract must be actively enforced, not just
        # declared. This validates that:
        # - identity_sensitive ∩ identity_excluded == ∅
        # - IMMUTABLE → identity_sensitive=True (enforced)
        # - RUNTIME_ONLY → identity_sensitive=False (enforced)
        #
        # This ensures identity doctrine is legally binding, not just on paper.
        # ========================================================================
        identity_violations = self._validate_identity_hash_contract(
            environment, config_version
        )
        violations.extend(identity_violations)
        
        # FIX #3: Validate hash module alignment during enforcement
        if self._hash_module_validator is not None:
            hash_alignment_violations = self._validate_hash_module_alignment_at_enforcement(
                environment, config_version
            )
            violations.extend(hash_alignment_violations)
        
        # Fail-fast: raise first violation
        if violations:
            violation = violations[0]
            self._logger.critical(
                f"Policy enforcement failed: {len(violations)} violation(s), "
                f"first={violation.field_path}, type={violation.violation_type}"
            )
            raise violation
        
        self._logger.info(
            f"Policy enforcement passed: {len(all_policies)} policies enforced, "
            f"{len(governed_config_fields)} config fields governed"
        )
    
    def _enforce_field_policy(
        self,
        field_path: str,
        value: Any,
        policy: FieldPolicy,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Enforce policy for a single field.
        
        DETERMINISTIC: Same field + value + policy → same result.
        """
        if policy.category == PolicyCategory.IMMUTABLE:
            self._enforce_immutable(
                field_path, value, policy, environment, config_version
            )
        
        elif policy.category == PolicyCategory.BOUNDED:
            self._enforce_bounded(
                field_path, value, policy, environment, config_version
            )
        
        elif policy.category == PolicyCategory.ENV_SCOPED:
            self._enforce_env_scoped(
                field_path, value, policy, environment, config_version
            )
        
        elif policy.category == PolicyCategory.RUNTIME_ONLY:
            self._enforce_runtime_only(
                field_path, value, policy, environment, config_version
            )
        
        else:
            raise ConfigPolicyViolation(
                field_path=field_path,
                violation_type="UnknownPolicyCategory",
                message=f"Unknown policy category: {policy.category}",
                environment=environment,
                config_version=config_version,
            )
    
    def _enforce_immutable(
        self,
        field_path: str,
        value: Any,
        policy: FieldPolicy,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Enforce IMMUTABLE policy.
        
        FIX #3: Immutable fields must match baseline for this version,
        not hardcoded constant forever. Supports version-aware comparison.
        
        CRITICAL LAW:
        If a field affects replay semantics, window definitions, or computation spec,
        then it is IMMUTABLE and IDENTITY_SENSITIVE.
        No exceptions. No overrides. No silent relaxation.
        
        Raises:
            ImmutableFieldChanged: If value does not match baseline
            IdentityImpactViolation: If identity_sensitive is False
        """
        # IMMUTABLE fields must be identity_sensitive
        if not policy.identity_sensitive:
            raise IdentityImpactViolation(
                field_path=field_path,
                message="IMMUTABLE field must be identity_sensitive=True",
                environment=environment,
                config_version=config_version,
            )
        
        # ========================================================================
        # BASELINE COMPARISON (FIX #3)
        # ========================================================================
        # CRITICAL: Immutable means "must not vary across deployments for this
        # version", NOT "must equal hardcoded constant forever".
        #
        # We support version-aware baseline comparison:
        # 1. If baseline_config provided → use field value from baseline
        # 2. Else if policy.baseline_value set → use that
        # 3. Else fallback to policy.canonical_value (legacy support)
        #
        # This allows forward evolution while maintaining immutability within
        # a deployment version. Changing immutable fields requires version bump.
        # ========================================================================
        baseline_value = None
        if self._baseline_config is not None:
            try:
                baseline_value = self._get_field_value(self._baseline_config, field_path)
            except (KeyError, AttributeError):
                # Baseline doesn't have this field, use baseline_value or canonical_value if available
                baseline_value = policy.baseline_value or policy.canonical_value
        else:
            # No baseline config, use baseline_value or canonical_value from policy
            baseline_value = policy.baseline_value or policy.canonical_value
        
        # If baseline/canonical value declared, must match exactly
        # This enforces immutability across deployments for this version
        if baseline_value is not None:
            if value != baseline_value:
                raise ImmutableFieldChanged(
                    field_path=field_path,
                    current=value,
                    canonical=baseline_value,
                    environment=environment,
                    config_version=config_version,
                )
        
        self._logger.debug(
            f"IMMUTABLE field validated: path={field_path}, value={value}, "
            f"baseline={baseline_value}"
        )
    
    def _enforce_bounded(
        self,
        field_path: str,
        value: Any,
        policy: FieldPolicy,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Enforce BOUNDED policy.
        
        Bounded fields must fall within declared numeric constraints.
        Must enforce numeric bounds explicitly.
        
        Raises:
            OutOfBounds: If value exceeds declared constraints
        """
        # Must be numeric type
        if not isinstance(value, (int, float)):
            raise OutOfBounds(
                field_path=field_path,
                value=value,
                min_val=policy.min_value,
                max_val=policy.max_value,
                environment=environment,
                config_version=config_version,
            )
        
        # Check minimum bound
        if policy.min_value is not None and value < policy.min_value:
            raise OutOfBounds(
                field_path=field_path,
                value=value,
                min_val=policy.min_value,
                max_val=policy.max_value,
                environment=environment,
                config_version=config_version,
            )
        
        # Check maximum bound
        if policy.max_value is not None and value > policy.max_value:
            raise OutOfBounds(
                field_path=field_path,
                value=value,
                min_val=policy.min_value,
                max_val=policy.max_value,
                environment=environment,
                config_version=config_version,
            )
        
        self._logger.debug(
            f"BOUNDED field validated: path={field_path}, value={value}, "
            f"bounds=[{policy.min_value}, {policy.max_value}]"
        )
    
    def _enforce_env_scoped(
        self,
        field_path: str,
        value: Any,
        policy: FieldPolicy,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Enforce ENV_SCOPED policy.
        
        FIX #7: Environment-scoped fields must not vary between nodes within same environment.
        Must enforce per-environment consistency and cross-node config alignment.
        
        Environment-scoped fields may vary by environment but must not
        affect replay determinism.
        
        Must not influence replay determinism.
        Must not be identity_sensitive.
        
        Raises:
            IdentityImpactViolation: If identity_sensitive is True
            EnvironmentLeakage: If value differs from environment baseline
        """
        # ENV_SCOPED fields must not be identity_sensitive
        if policy.identity_sensitive:
            raise IdentityImpactViolation(
                field_path=field_path,
                message="ENV_SCOPED field must be identity_sensitive=False",
                environment=environment,
                config_version=config_version,
            )
        
        # FIX #7: Enforce per-environment consistency
        if environment and environment in self._baseline_env_configs:
            baseline_env_config = self._baseline_env_configs[environment]
            try:
                baseline_value = self._get_field_value(baseline_env_config, field_path)
                if value != baseline_value:
                    raise EnvironmentLeakage(
                        field_path=field_path,
                        message=(
                            f"ENV_SCOPED field value differs from environment baseline. "
                            f"Expected {baseline_value} for environment '{environment}', got {value}. "
                            f"ENV_SCOPED fields must be consistent within the same environment."
                        ),
                        environment=environment,
                        config_version=config_version,
                    )
            except (KeyError, AttributeError):
                # Baseline doesn't have this field, which is acceptable for new fields
                self._logger.debug(
                    f"ENV_SCOPED field {field_path} not in environment baseline "
                    f"(new field, no consistency check)"
                )
        
        self._logger.debug(
            f"ENV_SCOPED field validated: path={field_path}, value={value}, "
            f"environment={environment}"
        )
    
    def _enforce_runtime_only(
        self,
        field_path: str,
        value: Any,
        policy: FieldPolicy,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> None:
        """
        Enforce RUNTIME_ONLY policy.
        
        Runtime-only fields must be excluded from config identity hash.
        These are typically secrets and credentials.
        
        Must be declared explicitly as excluded from identity.
        Must not be identity_sensitive.
        
        Raises:
            IdentityImpactViolation: If identity_sensitive is True
        """
        # RUNTIME_ONLY fields must not be identity_sensitive
        if policy.identity_sensitive:
            raise IdentityImpactViolation(
                field_path=field_path,
                message="RUNTIME_ONLY field must be identity_sensitive=False",
                environment=environment,
                config_version=config_version,
            )
        
        self._logger.debug(
            f"RUNTIME_ONLY field validated: path={field_path} "
            f"(excluded from identity hash)"
        )
    
    def _extract_field_paths(self, config: SystemConfig) -> Set[str]:
        """
        Extract all field paths from config object.
        
        DETERMINISTIC: Same config always produces same field paths.
        Paths are sorted for deterministic processing.
        
        Returns:
            Set of dot-separated paths like "windows.window_size_ms"
        """
        paths: Set[str] = set()
        
        def traverse(obj: Any, prefix: str = "") -> None:
            """Recursively traverse config structure."""
            # Handle dataclass objects
            if hasattr(obj, "__dataclass_fields__"):
                for field_name in obj.__dataclass_fields__:
                    field_value = getattr(obj, field_name, None)
                    field_path = f"{prefix}.{field_name}" if prefix else field_name
                    paths.add(field_path)
                    
                    # Recurse into nested dataclass objects
                    if field_value is not None and hasattr(field_value, "__dataclass_fields__"):
                        traverse(field_value, field_path)
            
            # Handle regular objects with __dict__
            elif hasattr(obj, "__dict__"):
                # Sort keys for deterministic traversal
                for field_name in sorted(obj.__dict__.keys()):
                    field_value = obj.__dict__[field_name]
                    field_path = f"{prefix}.{field_name}" if prefix else field_name
                    paths.add(field_path)
                    
                    # Recurse into nested objects
                    if hasattr(field_value, "__dict__") or hasattr(field_value, "__dataclass_fields__"):
                        traverse(field_value, field_path)
            
            # Handle dictionaries
            elif isinstance(obj, dict):
                # Sort keys for deterministic traversal
                for key in sorted(obj.keys()):
                    value = obj[key]
                    field_path = f"{prefix}.{key}" if prefix else str(key)
                    paths.add(field_path)
                    
                    # Recurse into nested dicts or objects
                    if isinstance(value, dict) or hasattr(value, "__dict__") or hasattr(value, "__dataclass_fields__"):
                        traverse(value, field_path)
        
        traverse(config)
        return paths
    
    def _get_field_value(self, config: SystemConfig, field_path: str) -> Any:
        """
        Get value of a field from config using dot-separated path.
        
        DETERMINISTIC: Same path always produces same value extraction.
        
        Example: "windows.window_size_ms" -> config.windows.window_size_ms
        
        Args:
            config: System configuration object
            field_path: Dot-separated field path
        
        Returns:
            Field value
        
        Raises:
            KeyError: If field path not found
        """
        parts = field_path.split(".")
        value = config
        
        for part in parts:
            # Try dataclass field first
            if hasattr(value, "__dataclass_fields__") and part in value.__dataclass_fields__:
                value = getattr(value, part)
            # Try regular attribute
            elif hasattr(value, part):
                value = getattr(value, part)
            # Try dictionary key
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise KeyError(f"Field path not found: {field_path} (failed at: {part})")
        
        return value
    
    def get_identity_sensitive_fields(self) -> Set[str]:
        """
        Get all fields that affect config identity hash.
        
        This is used by config_hashing.py to determine which fields
        to include in the hash computation.
        """
        return self.registry.get_identity_sensitive_fields()
    
    def get_identity_excluded_fields(self) -> Set[str]:
        """
        Get all fields excluded from config identity hash.
        
        This is used by config_hashing.py to determine which fields
        to exclude from the hash computation.
        """
        return self.registry.get_identity_excluded_fields()
    
    def _validate_identity_hash_contract(
        self,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> List[ConfigPolicyViolation]:
        """
        FIX #4: Enforce identity hash contract alignment.
        
        Validates that:
        - identity_sensitive ∩ runtime_only == ∅
        - identity_excluded ∩ immutable == ∅ (except for explicit exemptions)
        - Hash module alignment (if hash module provides declared paths)
        
        Returns:
            List of violations (empty if contract is satisfied)
        """
        violations: List[ConfigPolicyViolation] = []
        
        identity_sensitive = self.registry.get_identity_sensitive_fields()
        identity_excluded = self.registry.get_identity_excluded_fields()
        
        # Check: identity_sensitive and identity_excluded should be disjoint
        overlap = identity_sensitive & identity_excluded
        if overlap:
            violations.append(
                IdentityImpactViolation(
                    field_path="<registry>",
                    message=(
                        f"Identity hash contract violation: fields cannot be both "
                        f"sensitive and excluded. Overlapping fields: {sorted(overlap)}"
                    ),
                    environment=environment,
                    config_version=config_version,
                )
            )
        
        # Check: IMMUTABLE fields must be identity_sensitive
        all_policies = self.registry.get_all_policies()
        immutable_not_sensitive = [
            path for path, policy in all_policies.items()
            if policy.category == PolicyCategory.IMMUTABLE
            and not policy.identity_sensitive
        ]
        if immutable_not_sensitive:
            violations.append(
                IdentityImpactViolation(
                    field_path="<registry>",
                    message=(
                        f"IMMUTABLE fields must be identity_sensitive=True. "
                        f"Violating fields: {sorted(immutable_not_sensitive)}"
                    ),
                    environment=environment,
                    config_version=config_version,
                )
            )
        
        # Check: RUNTIME_ONLY fields must not be identity_sensitive
        runtime_only_sensitive = [
            path for path, policy in all_policies.items()
            if policy.category == PolicyCategory.RUNTIME_ONLY
            and policy.identity_sensitive
        ]
        if runtime_only_sensitive:
            violations.append(
                IdentityImpactViolation(
                    field_path="<registry>",
                    message=(
                        f"RUNTIME_ONLY fields must be identity_sensitive=False. "
                        f"Violating fields: {sorted(runtime_only_sensitive)}"
                    ),
                    environment=environment,
                    config_version=config_version,
                )
            )
        
        return violations
    
    def _validate_hash_module_alignment(self) -> None:
        """
        FIX #3: Validate hash module alignment at initialization.
        
        Ensures that the hash module's declared include/exclude paths match
        the registry's identity_sensitive/excluded fields. This prevents
        determinism risks from misaligned hash inputs.
        
        Raises:
            IdentityImpactViolation: If hash module alignment fails
        """
        if self._hash_module_validator is None:
            return
        
        try:
            # Get hash module's declared fields
            hash_sensitive = set()
            hash_excluded = set()
            
            if hasattr(self._hash_module_validator, 'get_identity_sensitive_fields'):
                hash_sensitive = self._hash_module_validator.get_identity_sensitive_fields()
            if hasattr(self._hash_module_validator, 'get_identity_excluded_fields'):
                hash_excluded = self._hash_module_validator.get_identity_excluded_fields()
            
            # Get registry's declared fields
            registry_sensitive = self.registry.get_identity_sensitive_fields()
            registry_excluded = self.registry.get_identity_excluded_fields()
            
            # Validate alignment
            mismatches = []
            
            # Fields in hash but not in registry
            hash_only_sensitive = hash_sensitive - registry_sensitive
            hash_only_excluded = hash_excluded - registry_excluded
            
            # Fields in registry but not in hash
            registry_only_sensitive = registry_sensitive - hash_sensitive
            registry_only_excluded = registry_excluded - hash_excluded
            
            if hash_only_sensitive:
                mismatches.append(
                    f"Hash module includes {len(hash_only_sensitive)} identity-sensitive fields "
                    f"not in registry: {sorted(hash_only_sensitive)[:5]}..."
                )
            if hash_only_excluded:
                mismatches.append(
                    f"Hash module excludes {len(hash_only_excluded)} fields not in registry: "
                    f"{sorted(hash_only_excluded)[:5]}..."
                )
            if registry_only_sensitive:
                mismatches.append(
                    f"Registry declares {len(registry_only_sensitive)} identity-sensitive fields "
                    f"not in hash module: {sorted(registry_only_sensitive)[:5]}..."
                )
            if registry_only_excluded:
                mismatches.append(
                    f"Registry declares {len(registry_only_excluded)} excluded fields "
                    f"not in hash module: {sorted(registry_only_excluded)[:5]}..."
                )
            
            if mismatches:
                raise IdentityImpactViolation(
                    field_path="<hash_module_alignment>",
                    message=(
                        f"Hash module alignment failure:\n" + "\n".join(f"  - {m}" for m in mismatches) +
                        "\nThis is a Tier-0 determinism risk. Hash inputs must match policy declarations."
                    ),
                )
            
            self._logger.info(
                f"Hash module alignment validated: "
                f"{len(registry_sensitive)} identity-sensitive, "
                f"{len(registry_excluded)} excluded fields aligned"
            )
            
        except Exception as e:
            if isinstance(e, IdentityImpactViolation):
                raise
            self._logger.warning(
                f"Could not validate hash module alignment: {e}. "
                f"This may indicate a determinism risk."
            )
    
    def _validate_hash_module_alignment_at_enforcement(
        self,
        environment: Optional[str] = None,
        config_version: Optional[str] = None,
    ) -> List[ConfigPolicyViolation]:
        """
        FIX #3: Validate hash module alignment during enforcement.
        
        Re-validates alignment at enforcement time to catch runtime misalignment.
        
        Returns:
            List of violations (empty if aligned)
        """
        violations: List[ConfigPolicyViolation] = []
        
        try:
            # Quick validation - check if sets still match
            if self._hash_module_validator is None:
                return violations
            
            hash_sensitive = set()
            hash_excluded = set()
            
            if hasattr(self._hash_module_validator, 'get_identity_sensitive_fields'):
                hash_sensitive = self._hash_module_validator.get_identity_sensitive_fields()
            if hasattr(self._hash_module_validator, 'get_identity_excluded_fields'):
                hash_excluded = self._hash_module_validator.get_identity_excluded_fields()
            
            registry_sensitive = self.registry.get_identity_sensitive_fields()
            registry_excluded = self.registry.get_identity_excluded_fields()
            
            if hash_sensitive != registry_sensitive or hash_excluded != registry_excluded:
                violations.append(
                    IdentityImpactViolation(
                        field_path="<hash_module_alignment>",
                        message=(
                            f"Hash module alignment mismatch detected at enforcement time. "
                            f"This is a Tier-0 determinism risk. "
                            f"Registry sensitive: {len(registry_sensitive)}, hash sensitive: {len(hash_sensitive)}. "
                            f"Registry excluded: {len(registry_excluded)}, hash excluded: {len(hash_excluded)}."
                        ),
                        environment=environment,
                        config_version=config_version,
                    )
                )
        except Exception as e:
            # Don't fail enforcement on validation errors, but log warning
            self._logger.warning(
                f"Hash module alignment check failed during enforcement: {e}"
            )
        
        return violations


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "PolicyCategory",
    "FieldPolicy",
    "PolicyRegistry",
    "ConfigPolicy",
    "ConfigPolicyViolation",
    "ImmutableFieldChanged",
    "OutOfBounds",
    "DisallowedVariation",
    "IdentityImpactViolation",
    "EnvironmentLeakage",
    "UngovernedFieldViolation",
]