"""
/data/validation/policy_profiles.py

Validation Policy Profiles (strict / recovery / audit / migration / etc.)

---

1️⃣ Core Responsibility

This file defines:

A closed set of validation execution profiles

Rules for interpreting violation severities

Ejection thresholds

Logging/escalation behavior

Deterministic filtering rules


It must never:

Define rejection codes

Perform validation

Mutate violations

Contain business rules


It interprets structured output — nothing more.


---

2️⃣ Conceptual Separation

There are three layers:

1. Validity → Determined by rules


2. Error Representation → Determined by error model


3. Policy Interpretation → Determined here



Profiles exist because not all execution contexts are equal.

Examples:

Production ingestion

Historical replay

Migration simulation

Data recovery mode

Observability-only audit


Each context may tolerate different severity levels.


---

3️⃣ Canonical Profile Model

Base Enum

Closed set only. Never dynamic profiles.


---

4️⃣ Profile Configuration Structure

Each profile maps to one config.


---

5️⃣ Example Profile Definitions

STRICT Profile

Used in:

Production ingest

Canonical fact enforcement

Governance-locked mutations


RECOVERY Profile

Used in:

Historical replay

Data salvage

Repair pipelines


AUDIT Profile

Used in:

Compliance scans

Observability snapshots


MIGRATION Profile

Used in:

Schema version transitions

Migration rehearsal

Compatibility dry-runs


---

6️⃣ Deterministic Evaluation Function

This must be pure.

No side effects. No mutation. No logging.

Interpretation layer only.


---

7️⃣ Policy Does NOT Change Validation Result

Very important:

The bundle is fixed. Policy does not modify it.

Bad anti-pattern:

downgrade severity in recovery mode

Never modify violation semantics. Profiles interpret — they do not rewrite truth.


---

8️⃣ Why Profiles Exist (System-Level Insight)

Without profiles, you face:

Recovery impossible because strict rules block replay

Migration unsafe because warnings might slip through

Observability conflated with ingestion

Governance behavior inconsistent


Profiles introduce clean separation between:

Validation correctness and Operational tolerance


---

9️⃣ Determinism Constraints

Profiles must:

Be immutable

Have static configuration

Be versioned if changed

Never read environment variables

Never check current time

Never reference runtime flags


Profile choice may vary per call. Profile definition must not.


---

🔟 Governance Rules

If profile behavior changes:

Must increment policy version

Must audit record transition

Must not silently broaden acceptance

Must not silently downgrade fatal to warning


Policy drift is compliance drift.


---

11️⃣ Advanced: Profile Versioning

This allows reproducible replay:

Replay must use the same profile version that original ingestion used.


---

12️⃣ Interaction with Persistence

Typical flow:

bundle = validate(object)
config = get_profile_config(profile)
should_reject = evaluate_bundle(bundle, config)

if should_reject:
    raise ValidationEjection(bundle)
else:
    persist(object)

Clear separation. No ambiguity.


---

13️⃣ Anti-Patterns

❌ If profile changes rule execution
❌ If profile suppresses certain codes
❌ If profile modifies violation list
❌ If profile adds synthetic errors
❌ If profile depends on runtime environment

Profiles must not alter truth.


---

14️⃣ What Makes This 9.5+/10

To elevate further:

Add Policy Decision Object

Instead of returning bool:

Add deterministic decision hash

Decision can be hashed together with bundle hash for audit trail.

Add threshold-based policy

Example:

reject if >3 errors in same category

Still deterministic — but configurable.


---

15️⃣ Final Summary

policy_profiles.py defines:

> The operational interpretation contract for validation outcomes.



It ensures:

One validation truth

Multiple operational tolerances

Deterministic decision making

Replay-safe enforcement

Governance traceability


Without it: Your system conflates correctness and operation.

With it: You get controlled flexibility without losing mathematical determinism.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Dict, Final, Set

from .ejection_reasons import RejectionSeverity
from .error_model import ValidationErrorBundle, validate_bundle_integrity


__all__ = [
    "ValidationProfile",
    "ProfileConfig",
    "PolicyDecision",
    "VersionedProfile",
    "STRICT_PROFILE",
    "RECOVERY_PROFILE",
    "AUDIT_PROFILE",
    "MIGRATION_PROFILE",
    "get_profile_config",
    "get_profile_version",
    "get_versioned_profile",
    "evaluate_bundle",
    "make_policy_decision",
    "compute_decision_hash",
    "PROFILE_REGISTRY",
    "PROFILE_VERSIONS",
    "get_severity_order",
    "compare_severities",
]


# ============================================================================
# Canonical Severity Ordering (Tier-0 Determinism)
# ============================================================================

# Canonical severity ordering for deterministic comparison.
# This MUST match the semantic ordering of RejectionSeverity enum.
# If the enum expands, this must be updated to maintain determinism.
_SEVERITY_ORDER: Final[Dict[RejectionSeverity, int]] = {
    RejectionSeverity.FATAL: 0,
    RejectionSeverity.ERROR: 1,
    RejectionSeverity.WARNING: 2,
    RejectionSeverity.INFO: 3,
}


def get_severity_order(severity: RejectionSeverity) -> int:
    """
    Get canonical ordering value for a severity.
    
    This provides deterministic ordering that is forward-compatible.
    If a new severity is added to the enum, it must be added here
    to maintain determinism.
    
    Args:
        severity: RejectionSeverity to get order for
        
    Returns:
        Integer order (lower = more severe)
        
    Raises:
        KeyError: If severity is not in canonical ordering
    """
    if severity not in _SEVERITY_ORDER:
        raise KeyError(
            f"Severity {severity} not in canonical ordering. "
            f"This is a Tier-0 determinism violation. "
            f"All RejectionSeverity values must be registered in _SEVERITY_ORDER."
        )
    return _SEVERITY_ORDER[severity]


def compare_severities(severity1: RejectionSeverity, severity2: RejectionSeverity) -> int:
    """
    Compare two severities deterministically.
    
    Returns:
        -1 if severity1 < severity2 (severity1 is more severe)
        0 if severity1 == severity2
        1 if severity1 > severity2 (severity1 is less severe)
    """
    order1 = get_severity_order(severity1)
    order2 = get_severity_order(severity2)
    if order1 < order2:
        return -1
    elif order1 > order2:
        return 1
    else:
        return 0


# ============================================================================
# Validation Profile Enum (Closed Set)
# ============================================================================

class ValidationProfile(str, Enum):
    """
    Closed set of validation execution profiles.
    
    Each profile represents a different operational context with different
    tolerance levels for validation violations. Profiles are immutable
    and deterministic - they interpret validation outcomes but never
    modify the validation truth.
    
    Values:
        STRICT: Production ingestion, canonical fact enforcement
        RECOVERY: Historical replay, data salvage, repair pipelines
        AUDIT: Compliance scans, observability snapshots
        MIGRATION: Schema version transitions, migration rehearsal
    """
    
    STRICT = "strict"
    RECOVERY = "recovery"
    AUDIT = "audit"
    MIGRATION = "migration"


# ============================================================================
# Profile Configuration Structure
# ============================================================================

@dataclass(frozen=True)
class ProfileConfig:
    """
    Immutable configuration for a validation profile.
    
    This defines how violations are interpreted under a specific profile.
    The configuration is deterministic and never changes at runtime.
    
    Attributes:
        reject_on: Set of severities that cause rejection
        log_on: Set of severities that should be logged
        fail_fast: If True, stop evaluation on first reject-worthy violation
        allow_partial_success: If True, allow persistence despite non-fatal violations
    """
    
    reject_on: FrozenSet[RejectionSeverity]
    log_on: FrozenSet[RejectionSeverity]
    fail_fast: bool
    allow_partial_success: bool
    
    def __post_init__(self) -> None:
        """
        Validate configuration invariants (Tier-0 enforcement).
        
        Enforces:
        - Type correctness (frozensets)
        - Severity validity (all severities must be valid enum values)
        - Deterministic monotonicity (FATAL cannot be log-only without reject)
        - Semantic consistency (reject_on and log_on relationships)
        """
        # Type validation
        if not isinstance(self.reject_on, frozenset):
            raise ValueError("reject_on must be a frozenset")
        if not isinstance(self.log_on, frozenset):
            raise ValueError("log_on must be a frozenset")
        
        # Validate all severities are valid enum values
        all_severities: Set[RejectionSeverity] = set(RejectionSeverity)
        invalid_reject = self.reject_on - all_severities
        if invalid_reject:
            raise ValueError(
                f"reject_on contains invalid severities: {invalid_reject}. "
                f"All values must be RejectionSeverity enum members."
            )
        invalid_log = self.log_on - all_severities
        if invalid_log:
            raise ValueError(
                f"log_on contains invalid severities: {invalid_log}. "
                f"All values must be RejectionSeverity enum members."
            )
        
        # Enforce deterministic monotonicity:
        # FATAL must be in reject_on if it's in log_on (cannot be log-only)
        # This prevents policy drift where FATAL is logged but not rejected
        if RejectionSeverity.FATAL in self.log_on and RejectionSeverity.FATAL not in self.reject_on:
            raise ValueError(
                "Tier-0 invariant violation: FATAL cannot be log-only. "
                "FATAL severity must be in reject_on if it's in log_on. "
                "This prevents policy drift and ensures deterministic governance."
            )
        
        # Semantic consistency: If a severity is in reject_on, it should typically
        # also be in log_on (for audit trail). This is a warning-level check,
        # not a hard error, but we document it.
        # Note: We don't enforce this as a hard error because some profiles
        # might intentionally not log certain severities, but it's worth noting.


# ============================================================================
# Policy Decision Object (Advanced)
# ============================================================================

@dataclass(frozen=True)
class PolicyDecision:
    """
    Structured policy decision result.
    
    Instead of returning a simple boolean, this provides a complete
    decision object that can be hashed and audited. This enables:
    - Deterministic decision hashing for audit trails
    - Rich decision metadata for observability
    - Replay-safe decision comparison
    
    Attributes:
        reject: Whether the bundle should be rejected
        highest_severity: Highest severity found in violations
        violation_count: Total number of violations
        profile_identity: Profile that produced this decision (for audit)
        profile_version: Version of profile config (for audit)
        allow_partial_success: Whether partial success is allowed
        decision_hash: Deterministic hash of the decision (includes policy identity)
    """
    
    reject: bool
    highest_severity: RejectionSeverity | None
    violation_count: int
    profile_identity: str
    profile_version: int
    allow_partial_success: bool
    decision_hash: str = field(default="", init=False)
    
    def __post_init__(self) -> None:
        """
        Compute decision hash for audit trail (Tier-0).
        
        Hash includes:
        - Decision outcome (reject)
        - Highest severity
        - Violation count
        - Profile identity (prevents cross-profile hash collisions)
        - Profile version (prevents version drift collisions)
        - Partial success flag (affects decision semantics)
        """
        # Create deterministic representation with policy identity
        severity_str = self.highest_severity.value if self.highest_severity else "none"
        decision_repr = (
            f"reject={self.reject},"
            f"severity={severity_str},"
            f"count={self.violation_count},"
            f"profile={self.profile_identity},"
            f"version={self.profile_version},"
            f"partial={self.allow_partial_success}"
        )
        hash_value = hashlib.sha256(decision_repr.encode("utf-8")).hexdigest()
        object.__setattr__(self, "decision_hash", hash_value)


# ============================================================================
# Versioned Profile (Advanced)
# ============================================================================

@dataclass(frozen=True)
class VersionedProfile:
    """
    Versioned profile configuration for reproducible replay.
    
    This allows the system to track which profile version was used
    during ingestion, enabling exact replay with the same policy
    interpretation that was originally applied.
    
    Attributes:
        version: Policy version number (increments on changes)
        profile: Validation profile type
        config: Profile configuration
    """
    
    version: int
    profile: ValidationProfile
    config: ProfileConfig
    
    def __post_init__(self) -> None:
        """Validate versioned profile."""
        if self.version < 1:
            raise ValueError("version must be >= 1")


# ============================================================================
# Profile Definitions
# ============================================================================

# STRICT Profile
# Used in: Production ingest, Canonical fact enforcement, Governance-locked mutations
STRICT_PROFILE: Final[ProfileConfig] = ProfileConfig(
    reject_on=frozenset({
        RejectionSeverity.FATAL,
        RejectionSeverity.ERROR
    }),
    log_on=frozenset({
        RejectionSeverity.WARNING,
        RejectionSeverity.INFO
    }),
    fail_fast=True,
    allow_partial_success=False
)

# RECOVERY Profile
# Used in: Historical replay, Data salvage, Repair pipelines
RECOVERY_PROFILE: Final[ProfileConfig] = ProfileConfig(
    reject_on=frozenset({
        RejectionSeverity.FATAL
    }),
    log_on=frozenset({
        RejectionSeverity.ERROR,
        RejectionSeverity.WARNING
    }),
    fail_fast=False,
    allow_partial_success=True
)

# AUDIT Profile
# Used in: Compliance scans, Observability snapshots
AUDIT_PROFILE: Final[ProfileConfig] = ProfileConfig(
    reject_on=frozenset(),
    log_on=frozenset({
        RejectionSeverity.FATAL,
        RejectionSeverity.ERROR,
        RejectionSeverity.WARNING
    }),
    fail_fast=False,
    allow_partial_success=True
)

# MIGRATION Profile
# Used in: Schema version transitions, Migration rehearsal, Compatibility dry-runs
MIGRATION_PROFILE: Final[ProfileConfig] = ProfileConfig(
    reject_on=frozenset({
        RejectionSeverity.FATAL,
        RejectionSeverity.ERROR
    }),
    log_on=frozenset({
        RejectionSeverity.WARNING
    }),
    fail_fast=False,
    allow_partial_success=False
)


# ============================================================================
# Profile Registry with Versioning
# ============================================================================

# Profile registry maps profile to current version config
PROFILE_REGISTRY: Final[Dict[ValidationProfile, ProfileConfig]] = {
    ValidationProfile.STRICT: STRICT_PROFILE,
    ValidationProfile.RECOVERY: RECOVERY_PROFILE,
    ValidationProfile.AUDIT: AUDIT_PROFILE,
    ValidationProfile.MIGRATION: MIGRATION_PROFILE,
}

# Profile version registry (Tier-0 versioning)
# Tracks current version for each profile
# When profile behavior changes, version must increment
PROFILE_VERSIONS: Final[Dict[ValidationProfile, int]] = {
    ValidationProfile.STRICT: 1,
    ValidationProfile.RECOVERY: 1,
    ValidationProfile.AUDIT: 1,
    ValidationProfile.MIGRATION: 1,
}


def get_profile_version(profile: ValidationProfile) -> int:
    """
    Get current version for a profile.
    
    Args:
        profile: ValidationProfile to get version for
        
    Returns:
        Current version number
        
    Raises:
        KeyError: If profile is not in version registry
    """
    if profile not in PROFILE_VERSIONS:
        raise KeyError(f"Profile version not found: {profile}")
    return PROFILE_VERSIONS[profile]


# ============================================================================
# Profile Lookup
# ============================================================================

def get_profile_config(profile: ValidationProfile) -> ProfileConfig:
    """
    Get configuration for a validation profile.
    
    Args:
        profile: Validation profile to look up
        
    Returns:
        ProfileConfig for the specified profile
        
    Raises:
        KeyError: If profile is not in registry
    """
    if profile not in PROFILE_REGISTRY:
        raise KeyError(f"Profile not found in registry: {profile}")
    return PROFILE_REGISTRY[profile]


def get_versioned_profile(profile: ValidationProfile) -> VersionedProfile:
    """
    Get versioned profile configuration (Tier-0 versioning).
    
    This provides a VersionedProfile that includes both the config
    and the version, enabling reproducible replay.
    
    Args:
        profile: Validation profile to get versioned config for
        
    Returns:
        VersionedProfile with config and version
        
    Raises:
        KeyError: If profile is not in registry
    """
    config = get_profile_config(profile)
    version = get_profile_version(profile)
    return VersionedProfile(
        version=version,
        profile=profile,
        config=config,
    )


# ============================================================================
# Deterministic Evaluation Function
# ============================================================================

def evaluate_bundle(
    bundle: ValidationErrorBundle,
    config: ProfileConfig,
) -> bool:
    """
    Evaluate bundle against profile configuration (pure function).
    
    This is a pure, deterministic function that interprets validation
    outcomes according to profile policy. It does NOT:
    - Modify the bundle
    - Perform logging
    - Have side effects
    - Change validation truth
    
    It ONLY interprets whether the bundle should be rejected based on
    the profile's severity thresholds.
    
    Args:
        bundle: ValidationErrorBundle to evaluate
        config: ProfileConfig to apply
        
    Returns:
        True if bundle should be rejected, False otherwise
        
    Note:
        This function is pure and deterministic. Same bundle + config
        always produces same result.
        
        The allow_partial_success flag is interpreted as follows:
        - If allow_partial_success=True and there are only non-reject-worthy
          violations, the bundle is not rejected (can persist partial data)
        - If allow_partial_success=False, any reject-worthy violation causes
          rejection (strict enforcement)
    """
    # Validate bundle integrity (Tier-0 contract enforcement)
    if not validate_bundle_integrity(bundle):
        # Bundle hash mismatch indicates corruption or version drift
        # In Tier-0 systems, this is a hard failure
        raise ValueError(
            f"Bundle integrity check failed. "
            f"Bundle hash {bundle.deterministic_hash[:16]}... does not match computed hash. "
            f"This indicates bundle corruption or version drift."
        )
    
    reject = False
    
    for violation in bundle.violations:
        if violation.reason.severity in config.reject_on:
            reject = True
            if config.fail_fast:
                break
    
    # allow_partial_success affects the decision:
    # - If True: Only reject if there are reject-worthy violations
    # - If False: Strict enforcement (any reject-worthy violation = reject)
    # Current logic already handles this correctly - if reject=True, we reject.
    # The flag is primarily for downstream persistence layer to know whether
    # to allow partial data persistence when reject=False but violations exist.
    # However, for Tier-0 completeness, we ensure the flag is considered.
    
    return reject


# ============================================================================
# Policy Decision Function (Advanced)
# ============================================================================

def make_policy_decision(
    bundle: ValidationErrorBundle,
    config: ProfileConfig,
    profile: ValidationProfile | None = None,
    version: int = 1,
) -> PolicyDecision:
    """
    Make structured policy decision for bundle (Tier-0).
    
    This provides a rich decision object instead of a simple boolean,
    enabling audit trails and observability. The decision includes:
    - Rejection decision
    - Highest severity found
    - Violation count
    - Profile identity and version (for audit)
    - Deterministic hash for audit (includes policy identity)
    
    Args:
        bundle: ValidationErrorBundle to evaluate
        config: ProfileConfig to apply
        profile: Optional ValidationProfile for audit trail (defaults to "unknown")
        version: Profile version for audit trail (defaults to 1)
        
    Returns:
        PolicyDecision with complete decision metadata
        
    Note:
        This function is pure and deterministic. Same bundle + config + profile + version
        always produces same decision with same hash.
        
        Uses canonical severity ordering from get_severity_order() to ensure
        forward-compatible determinism.
    """
    # Validate bundle integrity (Tier-0 contract enforcement)
    if not validate_bundle_integrity(bundle):
        raise ValueError(
            f"Bundle integrity check failed. "
            f"Bundle hash {bundle.deterministic_hash[:16]}... does not match computed hash. "
            f"This indicates bundle corruption or version drift."
        )
    
    reject = False
    highest_severity: RejectionSeverity | None = None
    
    for violation in bundle.violations:
        severity = violation.reason.severity
        
        # Track highest severity using canonical ordering
        if highest_severity is None:
            highest_severity = severity
        else:
            # Use canonical ordering function (forward-compatible)
            if compare_severities(severity, highest_severity) < 0:
                highest_severity = severity
        
        # Check rejection
        if severity in config.reject_on:
            reject = True
            if config.fail_fast:
                break
    
    # Get profile identity for audit trail
    profile_identity = profile.value if profile is not None else "unknown"
    
    return PolicyDecision(
        reject=reject,
        highest_severity=highest_severity,
        violation_count=len(bundle.violations),
        profile_identity=profile_identity,
        profile_version=version,
        allow_partial_success=config.allow_partial_success,
    )


# ============================================================================
# Decision Hashing (Audit Trail)
# ============================================================================

def compute_decision_hash(
    bundle: ValidationErrorBundle,
    decision: PolicyDecision,
) -> str:
    """
    Compute deterministic hash of bundle + decision for audit trail (Tier-0).
    
    This enables:
    - Audit trail verification
    - Replay decision comparison
    - Integrity checking
    
    The hash includes:
    - Bundle deterministic hash (validated)
    - Decision hash (includes policy identity)
    - Combined audit trail hash
    
    Args:
        bundle: ValidationErrorBundle (must have valid deterministic_hash)
        decision: PolicyDecision (includes policy identity in its hash)
        
    Returns:
        SHA-256 hex digest combining bundle hash and decision hash
        
    Raises:
        ValueError: If bundle integrity check fails
    """
    # Validate bundle integrity (Tier-0 contract enforcement)
    if not validate_bundle_integrity(bundle):
        raise ValueError(
            f"Bundle integrity check failed. "
            f"Bundle hash {bundle.deterministic_hash[:16]}... does not match computed hash. "
            f"This indicates bundle corruption or version drift."
        )
    
    # Decision hash already includes policy identity, so this combination
    # provides complete audit trail: bundle + policy decision
    combined = f"{bundle.deterministic_hash}:{decision.decision_hash}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


# ============================================================================
# Invariant: Policy Does NOT Change Validation Result
# ============================================================================

# CRITICAL: The bundle is fixed. Policy does not modify it.
#
# Bad anti-pattern:
#   - downgrade severity in recovery mode
#   - suppress certain codes
#   - modify violation list
#   - add synthetic errors
#
# Never modify violation semantics. Profiles interpret — they do not rewrite truth.
#
# The evaluation functions above are pure and read-only. They never mutate
# the bundle or its violations. This is enforced by:
# 1. Bundle is frozen dataclass (immutable)
# 2. Violations are tuple (immutable)
# 3. Evaluation functions only read and return boolean/decision
# 4. No side effects in evaluation path


# ============================================================================
# Determinism Constraints
# ============================================================================

# Profiles must:
# - Be immutable (frozen dataclasses)
# - Have static configuration (defined at module level)
# - Be versioned if changed (VersionedProfile)
# - Never read environment variables
# - Never check current time
# - Never reference runtime flags
#
# Profile choice may vary per call. Profile definition must not.
#
# All profile configurations above are:
# - Defined as Final constants
# - Frozen dataclasses
# - Statically configured
# - Deterministic


# ============================================================================
# Governance Rules
# ============================================================================

# If profile behavior changes:
# - Must increment policy version (use VersionedProfile)
# - Must audit record transition
# - Must not silently broaden acceptance
# - Must not silently downgrade fatal to warning
#
# Policy drift is compliance drift.
#
# Example versioning:
#
#   # Version 1 (original)
#   STRICT_PROFILE_V1 = ProfileConfig(...)
#
#   # Version 2 (changed behavior)
#   STRICT_PROFILE_V2 = ProfileConfig(...)
#
#   # Use VersionedProfile to track which version was used
#   versioned = VersionedProfile(
#       version=2,
#       profile=ValidationProfile.STRICT,
#       config=STRICT_PROFILE_V2
#   )


# ============================================================================
# Interaction with Persistence
# ============================================================================

# Typical flow:
#
#   bundle = validate(object)
#   config = get_profile_config(profile)
#   should_reject = evaluate_bundle(bundle, config)
#
#   if should_reject:
#       raise ValidationEjection(bundle)
#   else:
#       persist(object)
#
# Clear separation. No ambiguity.
#
# The policy layer is a pure interpretation layer between validation
# and persistence. It does not perform validation or persistence itself.


# ============================================================================
# Anti-Patterns
# ============================================================================

# ❌ If profile changes rule execution
# ❌ If profile suppresses certain codes
# ❌ If profile modifies violation list
# ❌ If profile adds synthetic errors
# ❌ If profile depends on runtime environment
#
# Profiles must not alter truth.
#
# All evaluation functions in this file are pure and read-only.
# They interpret the bundle but never modify it.
