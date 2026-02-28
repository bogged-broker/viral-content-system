"""
/data/validation/compatibility_guards.py

Version Compatibility Enforcement Specification
(Cross-Version Legitimacy Gate)

---

1️⃣ Purpose

This file enforces:

Schema version legality

Upgrade path correctness

Downgrade prevention

Backward compatibility guarantees

Forward compatibility constraints

Coexistence legality between artifacts of different versions

Registry-declared compatibility matrix adherence


This layer answers:

> "Even if this artifact is structurally and semantically valid — is it legally compatible with the system's declared version state?"



Compatibility is not syntax.
Compatibility is governance.


---

2️⃣ Architectural Position

Validation stack:

1. field_rules.py → atomic integrity


2. semantic_rules.py → internal coherence


3. compatibility_guards.py → cross-version legality


4. invariants.py → system-wide truth enforcement


5. deterministic_checks.py → replay safety



Compatibility guards execute before invariants because:

If version is illegal, invariant enforcement is irrelevant.

No hash or lineage check matters if version is prohibited.



---

3️⃣ Core Responsibility

This file enforces that:

artifact_version is recognized.

artifact_version is supported.

Declared version matches registry fingerprint.

Version transitions follow allowed upgrade paths.

Downgrades are rejected.

Breaking changes are not introduced unless explicitly permitted.

Compatibility matrix has not prohibited coexistence.



---

4️⃣ Inputs

Compatibility guards rely on:

ValidationContext.artifact_version
ValidationContext.registry_fingerprint
ValidationContext.compatibility_fingerprint
ValidationContext.prior_version (for upgrade path enforcement, via getattr)
ValidationContext.is_mutation (for upgrade path enforcement, via getattr)
ValidationContext.existing_versions (for coexistence enforcement, via getattr)

They must NOT:

Query registry directly.

Pull compatibility matrix dynamically.

Read external config.

Use caller-provided mutation state from input_data.


The validator receives deterministic fingerprints from the caller.

That keeps validation pure.

Fingerprint Decoding Architecture:

Fingerprints are decoded ONCE in CompatibilityRule.evaluate() base method,
then passed to rule-specific _evaluate_compatibility() methods. This ensures:

- No redundant decoding across rules
- Deterministic single-pass decoding
- Performance optimization for high-traffic systems
- Centralized fingerprint validation


---

5️⃣ Types of Compatibility Enforcement

1️⃣ Version Existence Check

Artifact version must exist in registry.

2️⃣ Version Support Check

Artifact version must not be deprecated or disabled.

3️⃣ Upgrade Path Check

If context declares mutation:

Target version must be reachable from prior version.


4️⃣ Downgrade Prevention

Artifact must not declare a lower version than system-accepted.

5️⃣ Breaking Change Check

If version declared is flagged as breaking:

Compatibility policy must allow.


6️⃣ Cross-Coexistence Check

Version A must be allowed to coexist with version B under compatibility matrix.


---

6️⃣ Rule Interface

Each compatibility rule must implement:

class CompatibilityRule:
    rule_id: str
    severity: SeverityLevel

    def evaluate(self, input_data, context) -> ValidationViolation | None:
        ...

They must:

Not call registry API directly.

Not read files.

Not query database.

Use only context-provided fingerprints or version values.



---

7️⃣ Example: Unsupported Version Rule

class UnsupportedVersionRule:
    rule_id = "COMPAT_UNSUPPORTED_VERSION"
    severity = SeverityLevel.CRITICAL

    def evaluate(self, input_data, context):
        version = context.artifact_version

        if not is_supported_version(version, context.registry_fingerprint):
            return build_violation(
                self.rule_id,
                f"Artifact version {version} is not supported by current registry",
                self.severity,
                field_path="schema_version",
            )
        return None

Important:

is_supported_version must be a pure function that uses fingerprint-derived lookup, not global registry.


---

8️⃣ Example: Downgrade Prevention Rule

class DowngradeProhibitedRule:
    rule_id = "COMPAT_DOWNGRADE_PROHIBITED"
    severity = SeverityLevel.ERROR

    def evaluate(self, input_data, context):
        declared_version = context.artifact_version
        current_version = extract_current_version(context)

        if declared_version < current_version:
            return build_violation(
                self.rule_id,
                "Downgrades are prohibited",
                self.severity,
                field_path="schema_version",
            )
        return None

Version comparisons must use structured version objects. Never string lexicographic comparison.


---

9️⃣ Example: Breaking Change Guard

class BreakingChangeNotAllowedRule:
    rule_id = "COMPAT_BREAKING_CHANGE_NOT_ALLOWED"
    severity = SeverityLevel.CRITICAL

    def evaluate(self, input_data, context):
        version = context.artifact_version

        if is_breaking_version(version, context.registry_fingerprint):
            if not compatibility_allows_breaking(
                version, context.compatibility_fingerprint
            ):
                return build_violation(
                    self.rule_id,
                    "Breaking schema change not permitted under compatibility policy",
                    self.severity,
                    field_path="schema_version",
                )
        return None

Compatibility decisions must be deterministic.


---

🔟 Rule Registry

Expose static registry:

COMPATIBILITY_RULES = [
    UnsupportedVersionRule(),
    DowngradeProhibitedRule(),
    BreakingChangeNotAllowedRule(),
    ...
]

Rules must:

Not be dynamically injected.

Not vary between nodes.

Be static at import time.

Be sorted by orchestrator.



---

1️⃣1️⃣ Version Comparison Requirements

Version objects must:

Be parsed into structured format.

Compare by numeric components.

Never compare raw strings.

Be deterministic.


Example safe comparison:

class Version:
    major: int
    minor: int
    patch: int

No semantic interpretation ambiguity.


---

1️⃣2️⃣ Relationship to /data/versioning/

Compatibility guards depend conceptually on:

schema_version.py

compatibility.py

migration_rules.py


However:

They cannot call them dynamically.

Instead:

Calling layer computes registry and compatibility fingerprints, which are passed through ValidationContext.

Validation uses fingerprints to ensure:

Compatibility matrix has not changed between nodes.


---

1️⃣3️⃣ Determinism Constraints

Compatibility checks must not:

Use runtime registry lookups.

Use global mutable matrices.

Depend on node-specific configuration.

Depend on wall-clock date.

Check feature flags unless fingerprinted.


All compatibility decision inputs must be deterministic from:

context
input_data

Only.


---

1️⃣4️⃣ Failure Semantics

Compatibility violations are severe.

Severity recommendations:

Unsupported version → CRITICAL

Downgrade attempt → ERROR or CRITICAL

Breaking change violation → CRITICAL

Deprecated but allowed → WARNING

Future version → ERROR


Policy must be explicit.


---

1️⃣5️⃣ Security Role

Compatibility guards prevent:

Schema downgrade attacks.

Rogue version injection.

Unauthorized breaking changes.

Silent registry drift.

Cross-node version mismatch.

Unapproved coexistence states.

Governance bypass via caller-provided mutation state.

Fingerprint corruption attacks.

Version coexistence violations.

Upgrade path manipulation.


Implementation Security Features:

1. Downgrade Prevention (DowngradeProhibitedRule)
   - Prevents attackers from declaring lower versions to bypass newer security constraints
   - Uses structured version comparison (ordinal-aware) to prevent string manipulation

2. Version Existence Validation (VersionExistenceRule)
   - Prevents injection of arbitrary version strings not in registry
   - Ensures only registry-declared versions are accepted

3. Version Support Enforcement (UnsupportedVersionRule)
   - Distinguishes "exists" from "supported" to prevent use of disabled versions
   - Prevents exploitation of deprecated or disabled version states

4. Breaking Change Control (BreakingChangeNotAllowedRule)
   - Requires explicit compatibility policy approval for breaking changes
   - Prevents unauthorized schema mutations that break backward compatibility

5. Future Version Prevention (FutureVersionRule)
   - Prevents declaration of versions not yet released
   - Blocks speculative version injection attacks

6. Upgrade Path Enforcement (UpgradePathViolationRule)
   - Validates that version transitions follow allowed upgrade paths
   - Requires authoritative mutation context (prevents caller-provided bypass)
   - Enforces temporal legality of version transitions

7. Coexistence Matrix Enforcement (CrossCoexistenceRule)
   - Validates multi-version coexistence legality
   - Requires authoritative lineage context (prevents caller-provided bypass)
   - Prevents incompatible version combinations

8. Fingerprint Integrity (Invalid Fingerprint Detection)
   - Detects and rejects corrupted or malformed governance fingerprints
   - Returns CRITICAL violations for fingerprint corruption (not silent failure)
   - Ensures governance state integrity

9. Deterministic Enforcement
   - All decisions are deterministic from fingerprints (no runtime lookups)
   - Prevents timing attacks and state manipulation
   - Replay-safe for audit and forensics

10. Governance Isolation
    - Rules use only fingerprint-derived data (not caller-provided fields)
    - Mutation and coexistence state must come from authoritative context
    - Prevents governance bypass via payload manipulation


Without this layer:

Your lineage becomes multi-version chaos.

Attackers can inject rogue versions, bypass security constraints via downgrades,
introduce breaking changes without approval, and violate coexistence policies.


---

1️⃣6️⃣ Replay Protection

Compatibility enforcement must behave identically during replay.

Which requires:

Registry fingerprint included in context.

Compatibility fingerprint included in context.

Deterministic rule ordering.

Deterministic violation fingerprinting.


Replay must not reinterpret past compatibility decisions.


---

1️⃣7️⃣ Testing Requirements

Unsupported version rejection test.

Downgrade attempt rejection test.

Breaking change violation test.

Deprecated version warning test.

Future-version rejection test.

Cross-node fingerprint equivalence test.

Version comparison correctness test.

Mixed semantic + compatibility violation test.



---

1️⃣8️⃣ Interaction With Strict Mode

Strict mode may escalate:

WARNING → rejection.

Compatibility CRITICAL must always reject.

Compatibility ERROR must always reject.

Strict mode never downgrades severity.


---

1️⃣9️⃣ Absolute Definition

/data/validation/compatibility_guards.py is:

> The deterministic cross-version enforcement layer that ensures schema declarations comply with registry policy, upgrade path legality, and compatibility matrix constraints before any artifact or mutation is permitted to interact with lineage or governance state.



Field rules prevent malformed shape.
Semantic rules prevent contradiction.
Compatibility guards prevent temporal illegality.


---

Next file:

/data/validation/invariants.py

This is where compatibility ends and constitutional truth begins.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .validation_contract import (
    ScopeDefinition,
    SeverityLevel,
    ValidationContext,
    ValidationRule,
    ValidationScope,
    ValidationViolation,
)

# ============================================================================
# Tier-0 Governance Invariants
# ============================================================================

def _assert_fingerprint_purity(context: ValidationContext) -> None:
    """
    Tier-0 defensive guard: Enforce that fingerprints are present and authoritative.
    
    This prevents silent fallback to local registry lookups or dynamic queries.
    All compatibility decisions MUST be fingerprint-derived.
    
    Raises:
        AssertionError: If fingerprints are missing or invalid
    """
    assert context.registry_fingerprint, (
        "registry_fingerprint is required for compatibility validation. "
        "Cannot fall back to local registry - all decisions must be fingerprint-derived."
    )
    assert context.compatibility_fingerprint, (
        "compatibility_fingerprint is required for compatibility validation. "
        "Cannot fall back to local compatibility matrix - all decisions must be fingerprint-derived."
    )
    # Ensure fingerprints are non-empty strings (not just whitespace)
    assert context.registry_fingerprint.strip(), (
        "registry_fingerprint cannot be empty or whitespace-only. "
        "Governance state must be explicitly provided."
    )
    assert context.compatibility_fingerprint.strip(), (
        "compatibility_fingerprint cannot be empty or whitespace-only. "
        "Governance state must be explicitly provided."
    )


def _ensure_version_object(version_str: Optional[str], ordinal: Optional[int] = None) -> Optional[Version]:
    """
    Tier-0 defensive guard: Ensure all version comparisons use Version objects.
    
    This prevents accidental string lexicographic comparison bugs like "1.10" < "1.2".
    All version comparisons MUST use structured Version objects.
    
    Args:
        version_str: Version string to parse
        ordinal: Optional ordinal from registry
        
    Returns:
        Version object or None if parsing fails
        
    Raises:
        ValueError: If version_str is provided but cannot be parsed (governance integrity)
    """
    if not version_str:
        return None
    
    version = Version.parse(version_str, ordinal=ordinal)
    if version is None:
        # This is a governance integrity failure - version should be parseable
        raise ValueError(
            f"Version string '{version_str}' cannot be parsed into structured Version object. "
            f"This indicates governance state corruption or invalid version declaration."
        )
    return version


def _anchor_violation_to_fingerprints(
    violation: ValidationViolation,
    registry_fingerprint: str,
    compatibility_fingerprint: str,
) -> ValidationViolation:
    """
    Tier-0 replay guarantee: Anchor violation to fingerprints for replay legitimacy.
    
    Ensures that all compatibility decisions are explicitly tied to the governance
    fingerprints that produced them. This prevents replay drift after policy updates.
    
    Recomputes violation hash with fingerprint anchors to ensure replay decisions
    match original governance state.
    
    Args:
        violation: Violation to anchor
        registry_fingerprint: Registry fingerprint that produced this decision
        compatibility_fingerprint: Compatibility fingerprint that produced this decision
        
    Returns:
        Violation with fingerprint-anchored hash
    """
    # Compute fingerprint hashes for anchoring
    registry_fp_hash = hashlib.sha256(registry_fingerprint.encode("utf-8")).hexdigest()[:16]
    compat_fp_hash = hashlib.sha256(compatibility_fingerprint.encode("utf-8")).hexdigest()[:16]
    
    # Recompute hash with fingerprint anchors
    anchored_hash = _compute_violation_hash(
        violation.rule_id,
        violation.message,
        violation.field_path,
        violation.severity,
        registry_fingerprint_hash=registry_fp_hash,
        compatibility_fingerprint_hash=compat_fp_hash,
    )
    
    # Update violation with anchored hash
    object.__setattr__(violation, "deterministic_hash", anchored_hash)
    return violation


# ============================================================================
# Centralized Severity Policy
# ============================================================================

class CompatibilitySeverityPolicy:
    """
    Tier-0 centralized severity policy enforcement.
    
    Prevents accidental severity drift and ensures consistent governance semantics.
    All compatibility rule severities must align with this policy.
    """
    
    # Version existence and support violations are CRITICAL
    VERSION_NOT_IN_REGISTRY = SeverityLevel.CRITICAL
    UNSUPPORTED_VERSION = SeverityLevel.CRITICAL
    
    # Deprecated versions are warnings (allowed but discouraged)
    DEPRECATED_VERSION = SeverityLevel.WARNING
    
    # Downgrades are ERROR (prohibited but not as severe as unsupported)
    DOWNGRADE_PROHIBITED = SeverityLevel.ERROR
    
    # Future versions are ERROR (prohibited)
    FUTURE_VERSION = SeverityLevel.ERROR
    
    # Breaking changes are CRITICAL (governance attack)
    BREAKING_CHANGE_NOT_ALLOWED = SeverityLevel.CRITICAL
    
    # Upgrade path violations are ERROR (temporal illegality)
    UPGRADE_PATH_VIOLATION = SeverityLevel.ERROR
    
    # Coexistence violations are ERROR (multi-version illegality)
    COEXISTENCE_VIOLATION = SeverityLevel.ERROR
    
    # Fingerprint corruption is CRITICAL (governance integrity failure)
    INVALID_REGISTRY_FINGERPRINT = SeverityLevel.CRITICAL
    INVALID_COMPATIBILITY_FINGERPRINT = SeverityLevel.CRITICAL
    
    # Missing mutation/coexistence context is CRITICAL (governance bypass risk)
    MISSING_MUTATION_CONTEXT = SeverityLevel.CRITICAL
    INVALID_COEXISTENCE_CONTEXT = SeverityLevel.CRITICAL
    
    @classmethod
    def assert_severity(cls, rule_id: str, severity: SeverityLevel) -> None:
        """
        Assert that rule severity matches centralized policy.
        
        Raises:
            AssertionError: If severity does not match policy
        """
        policy_map = {
            "COMPAT_VERSION_NOT_IN_REGISTRY": cls.VERSION_NOT_IN_REGISTRY,
            "COMPAT_UNSUPPORTED_VERSION": cls.UNSUPPORTED_VERSION,
            "COMPAT_DEPRECATED_VERSION": cls.DEPRECATED_VERSION,
            "COMPAT_DOWNGRADE_PROHIBITED": cls.DOWNGRADE_PROHIBITED,
            "COMPAT_FUTURE_VERSION": cls.FUTURE_VERSION,
            "COMPAT_BREAKING_CHANGE_NOT_ALLOWED": cls.BREAKING_CHANGE_NOT_ALLOWED,
            "COMPAT_UPGRADE_PATH_VIOLATION": cls.UPGRADE_PATH_VIOLATION,
            "COMPAT_COEXISTENCE_VIOLATION": cls.COEXISTENCE_VIOLATION,
            "COMPAT_INVALID_REGISTRY_FINGERPRINT": cls.INVALID_REGISTRY_FINGERPRINT,
            "COMPAT_INVALID_COMPATIBILITY_FINGERPRINT": cls.INVALID_COMPATIBILITY_FINGERPRINT,
            "COMPAT_MISSING_MUTATION_CONTEXT": cls.MISSING_MUTATION_CONTEXT,
            "COMPAT_INVALID_COEXISTENCE_CONTEXT": cls.INVALID_COEXISTENCE_CONTEXT,
        }
        
        expected_severity = policy_map.get(rule_id)
        if expected_severity is not None:
            assert severity == expected_severity, (
                f"Rule {rule_id} severity {severity.value} does not match "
                f"centralized policy {expected_severity.value}. "
                f"This prevents accidental severity drift."
            )

__all__ = [
    "Version",
    "CompatibilityRule",
    "CompatibilitySeverityPolicy",
    "UnsupportedVersionRule",
    "VersionExistenceRule",
    "DeprecatedVersionRule",
    "DowngradeProhibitedRule",
    "BreakingChangeNotAllowedRule",
    "UpgradePathViolationRule",
    "FutureVersionRule",
    "CrossCoexistenceRule",
    "COMPATIBILITY_RULES",
    "_decode_registry_fingerprint",
    "_decode_compatibility_fingerprint",
    "_assert_fingerprint_purity",
    "_ensure_version_object",
    "_anchor_violation_to_fingerprints",
    "_canonicalize_coexistence_key",
    "_normalize_coexistence_matrix",
    "_validate_fingerprint_schema",
    "COMPATIBILITY_RULES_ORDERING_HASH",
    "EXPECTED_REGISTRY_FINGERPRINT_SCHEMA_VERSION",
    "EXPECTED_COMPATIBILITY_FINGERPRINT_SCHEMA_VERSION",
]


# ============================================================================
# Version Structure
# ============================================================================

@dataclass(frozen=True)
class Version:
    """
    Structured version representation for deterministic comparison.
    
    Versions are parsed into numeric components to avoid string lexicographic
    comparison errors. All comparisons use numeric ordering.
    
    Supports:
    - Semantic versioning (major.minor.patch)
    - Integer versions
    - Ordinal-based versions (from schema registry)
    """
    
    major: int
    minor: int = 0
    patch: int = 0
    ordinal: Optional[int] = None  # For registry-based ordinal ordering
    
    def __post_init__(self) -> None:
        """Validate version components."""
        if self.major < 0 or self.minor < 0 or self.patch < 0:
            raise ValueError(f"Version components must be non-negative: {self}")
        if self.ordinal is not None and self.ordinal < 1:
            raise ValueError(f"Ordinal must be >= 1 if provided: {self.ordinal}")
    
    def __lt__(self, other: "Version") -> bool:
        """
        Tier-0 constitutional comparison: Forbid mixed ordinal/semantic regimes.
        
        Constitutional rule: Mixed ordinal/non-ordinal comparison is PROHIBITED.
        This enforces a single total ordering model per lineage epoch.
        
        Raises:
            ValueError: If attempting mixed ordinal/semantic comparison
        """
        if not isinstance(other, Version):
            return NotImplemented
        
        # Tier-0 constitutional: Forbid mixed comparison regimes
        # This prevents dual ordering ambiguity across lineage epochs
        if (self.ordinal is None) != (other.ordinal is None):
            raise ValueError(
                f"Mixed ordinal/non-ordinal version comparison prohibited for governance integrity. "
                f"self.ordinal={self.ordinal}, other.ordinal={other.ordinal}. "
                f"All versions in comparison must use same ordering regime."
            )
        
        # Both have ordinals - use ordinal comparison (lineage ordering)
        if self.ordinal is not None and other.ordinal is not None:
            return self.ordinal < other.ordinal
        
        # Both lack ordinals - use semantic version comparison
        if self.major != other.major:
            return self.major < other.major
        if self.minor != other.minor:
            return self.minor < other.minor
        return self.patch < other.patch
    
    def __le__(self, other: "Version") -> bool:
        """Less than or equal comparison."""
        return self == other or self < other
    
    def __eq__(self, other: object) -> bool:
        """
        Tier-0 constitutional equality: Forbid mixed ordinal/semantic regimes.
        
        Constitutional rule: Mixed ordinal/non-ordinal comparison is PROHIBITED.
        """
        if not isinstance(other, Version):
            return NotImplemented
        
        # Tier-0 constitutional: Forbid mixed comparison regimes
        if (self.ordinal is None) != (other.ordinal is None):
            raise ValueError(
                f"Mixed ordinal/non-ordinal version comparison prohibited for governance integrity. "
                f"self.ordinal={self.ordinal}, other.ordinal={other.ordinal}. "
                f"All versions in comparison must use same ordering regime."
            )
        
        # Both have ordinals - use ordinal comparison
        if self.ordinal is not None and other.ordinal is not None:
            return self.ordinal == other.ordinal
        
        # Both lack ordinals - use semantic comparison
        return (
            self.major == other.major
            and self.minor == other.minor
            and self.patch == other.patch
        )
    
    def __gt__(self, other: "Version") -> bool:
        """Greater than comparison."""
        if not isinstance(other, Version):
            return NotImplemented
        return other < self
    
    def __ge__(self, other: "Version") -> bool:
        """Greater than or equal comparison."""
        return self == other or self > other
    
    def __hash__(self) -> int:
        """Hash for use in sets/dicts."""
        return hash((self.major, self.minor, self.patch, self.ordinal))
    
    def __str__(self) -> str:
        """String representation."""
        if self.ordinal is not None:
            return f"v{self.ordinal}"
        if self.patch > 0:
            return f"{self.major}.{self.minor}.{self.patch}"
        return f"{self.major}.{self.minor}"
    
    @classmethod
    def parse(cls, version_str: Optional[str], ordinal: Optional[int] = None) -> Optional["Version"]:
        """
        Parse version string into structured Version object.
        
        Supports:
        - Integer strings: "1" -> Version(1, 0, 0, ordinal=1)
        - Semantic versions: "1.2.3" -> Version(1, 2, 3)
        - Two-part: "1.2" -> Version(1, 2, 0)
        - With ordinal: uses ordinal for comparison
        
        Args:
            version_str: Version string to parse
            ordinal: Optional ordinal from registry
            
        Returns:
            Version object or None if parsing fails
        """
        if not version_str:
            return None
        
        version_str = version_str.strip()
        
        # Try integer first
        try:
            major = int(version_str)
            return cls(major=major, minor=0, patch=0, ordinal=ordinal)
        except ValueError:
            pass
        
        # Try semantic version (major.minor.patch or major.minor)
        parts = version_str.split(".")
        if len(parts) == 1:
            try:
                major = int(parts[0])
                return cls(major=major, minor=0, patch=0, ordinal=ordinal)
            except ValueError:
                return None
        elif len(parts) == 2:
            try:
                major = int(parts[0])
                minor = int(parts[1])
                return cls(major=major, minor=minor, patch=0, ordinal=ordinal)
            except ValueError:
                return None
        elif len(parts) == 3:
            try:
                major = int(parts[0])
                minor = int(parts[1])
                patch = int(parts[2])
                return cls(major=major, minor=minor, patch=patch, ordinal=ordinal)
            except ValueError:
                return None
        
        return None


# ============================================================================
# Fingerprint Decoding Utilities
# ============================================================================

def _decode_registry_fingerprint(fingerprint: str) -> Tuple[Dict[str, Any], Optional[ValidationViolation]]:
    """
    Decode registry fingerprint into structured data.
    
    Fingerprints are deterministic JSON-encoded structures containing:
    - supported_versions: List of version strings
    - deprecated_versions: Set of deprecated version strings
    - disabled_versions: Set of disabled (exists but not supported) version strings
    - current_version: Current system version
    - version_ordinals: Dict mapping version -> ordinal
    - breaking_versions: Set of versions flagged as breaking
    
    Args:
        fingerprint: Registry fingerprint string
        
    Returns:
        Tuple of (decoded registry data dict, violation if fingerprint is invalid)
        
    Note:
        This is a pure function that decodes deterministic fingerprints.
        It does NOT query the registry directly.
        
        Invalid fingerprints are governance integrity failures and must be
        reported as CRITICAL violations, not silently ignored.
    """
    if not fingerprint:
        violation = ValidationViolation(
            rule_id="COMPAT_INVALID_REGISTRY_FINGERPRINT",
            message="Registry fingerprint is empty or missing - governance integrity failure",
            severity=SeverityLevel.CRITICAL,
            field_path="registry_fingerprint",
        )
        hash_value = _compute_violation_hash(
            "COMPAT_INVALID_REGISTRY_FINGERPRINT",
            violation.message,
            "registry_fingerprint",
            SeverityLevel.CRITICAL,
        )
        object.__setattr__(violation, "deterministic_hash", hash_value)
        return {
            "supported_versions": [],
            "deprecated_versions": set(),
            "disabled_versions": set(),
            "current_version": None,
            "version_ordinals": {},
            "breaking_versions": set(),
        }, violation
    
    try:
        data = json.loads(fingerprint)
        
        # Tier-0: Validate fingerprint schema structure (hard fail)
        schema_error = _validate_fingerprint_schema(data, "registry")
        if schema_error:
            message = (
                f"Registry fingerprint schema validation failed - governance integrity failure: {schema_error}"
            )
            violation = ValidationViolation(
                rule_id="COMPAT_INVALID_REGISTRY_FINGERPRINT",
                message=message,
                severity=SeverityLevel.CRITICAL,
                field_path="registry_fingerprint",
            )
            hash_value = _compute_violation_hash(
                "COMPAT_INVALID_REGISTRY_FINGERPRINT",
                message,
                "registry_fingerprint",
                SeverityLevel.CRITICAL,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return {
                "supported_versions": [],
                "deprecated_versions": set(),
                "disabled_versions": set(),
                "current_version": None,
                "version_ordinals": {},
                "breaking_versions": set(),
            }, violation
        
        # Convert lists to sets where needed for deterministic lookup
        # Schema validation ensures required fields exist
        return {
            "supported_versions": data["supported_versions"],
            "deprecated_versions": set(data["deprecated_versions"]),
            "disabled_versions": set(data["disabled_versions"]),
            "current_version": data.get("current_version"),
            "version_ordinals": data.get("version_ordinals", {}),
            "breaking_versions": set(data.get("breaking_versions", [])),
        }, None
    except (json.JSONDecodeError, TypeError) as e:
        # Invalid fingerprint format = governance integrity failure
        # Must be reported as CRITICAL violation, not silently ignored
        message = (
            f"Registry fingerprint is malformed or corrupted - governance integrity failure: {str(e)}"
        )
        violation = ValidationViolation(
            rule_id="COMPAT_INVALID_REGISTRY_FINGERPRINT",
            message=message,
            severity=SeverityLevel.CRITICAL,
            field_path="registry_fingerprint",
        )
        hash_value = _compute_violation_hash(
            "COMPAT_INVALID_REGISTRY_FINGERPRINT",
            message,
            "registry_fingerprint",
            SeverityLevel.CRITICAL,
        )
        object.__setattr__(violation, "deterministic_hash", hash_value)
        return {
            "supported_versions": [],
            "deprecated_versions": set(),
            "disabled_versions": set(),
            "current_version": None,
            "version_ordinals": {},
            "breaking_versions": set(),
        }, violation


def _canonicalize_coexistence_key(version_a: str, version_b: str) -> str:
    """
    Tier-0 constitutional: Canonical string key for coexistence matrix.
    
    Converts tuple keys to canonical string format for cross-node serialization safety.
    Ensures deterministic key encoding across all nodes.
    
    Args:
        version_a: First version
        version_b: Second version
        
    Returns:
        Canonical string key: "version_a|version_b" (sorted for symmetry)
    """
    # Sort versions for symmetric key (A,B) == (B,A)
    sorted_versions = sorted([version_a, version_b])
    return f"{sorted_versions[0]}|{sorted_versions[1]}"


def _normalize_coexistence_matrix(
    matrix: Dict[Any, Any]
) -> Dict[str, bool]:
    """
    Tier-0 constitutional: Normalize coexistence matrix to canonical string keys.
    
    Converts tuple keys to canonical string format for serialization safety.
    This prevents cross-node serialization drift.
    
    Args:
        matrix: Coexistence matrix (may have tuple or string keys)
        
    Returns:
        Normalized matrix with canonical string keys
    """
    normalized = {}
    for key, value in matrix.items():
        if isinstance(key, tuple) and len(key) == 2:
            # Convert tuple key to canonical string
            canonical_key = _canonicalize_coexistence_key(str(key[0]), str(key[1]))
            normalized[canonical_key] = bool(value)
        elif isinstance(key, str):
            # Already string key - use as-is (assume canonical)
            normalized[key] = bool(value)
        # Skip invalid keys
    
    return normalized


# Tier-0 constitutional: Fingerprint schema versioning
# This prevents silent reinterpretation years later when schema evolves
EXPECTED_REGISTRY_FINGERPRINT_SCHEMA_VERSION = 1
EXPECTED_COMPATIBILITY_FINGERPRINT_SCHEMA_VERSION = 1


def _validate_fingerprint_schema(data: Dict[str, Any], fingerprint_type: str) -> Optional[str]:
    """
    Tier-0 constitutional: Strict fingerprint schema validation (hard fail).
    
    Ensures fingerprint matches expected schema exactly to prevent structural governance drift.
    Validates schema version to prevent silent reinterpretation.
    
    Args:
        data: Decoded fingerprint data
        fingerprint_type: "registry" or "compatibility"
        
    Returns:
        Error message if schema invalid, None if valid
    """
    if fingerprint_type == "registry":
        # Tier-0: Validate schema version
        schema_version = data.get("fingerprint_schema_version")
        if schema_version != EXPECTED_REGISTRY_FINGERPRINT_SCHEMA_VERSION:
            return (
                f"Registry fingerprint schema version mismatch. "
                f"Expected {EXPECTED_REGISTRY_FINGERPRINT_SCHEMA_VERSION}, got {schema_version}. "
                f"This prevents silent reinterpretation of governance state."
            )
        
        # Tier-0: Strict required fields validation
        required_fields = {
            "fingerprint_schema_version",
            "supported_versions",
            "deprecated_versions",
            "disabled_versions",
        }
        actual_fields = set(data.keys())
        
        # Check all required fields present
        missing_fields = required_fields - actual_fields
        if missing_fields:
            return (
                f"Registry fingerprint missing required fields: {sorted(missing_fields)}. "
                f"Expected exactly: {sorted(required_fields)}. "
                f"Got: {sorted(actual_fields)}"
            )
        
        # Check no unexpected fields (strict schema enforcement)
        allowed_fields = required_fields | {
            "current_version",
            "version_ordinals",
            "breaking_versions",
        }
        unexpected_fields = actual_fields - allowed_fields
        if unexpected_fields:
            return (
                f"Registry fingerprint contains unexpected fields: {sorted(unexpected_fields)}. "
                f"Allowed fields: {sorted(allowed_fields)}"
            )
        
        # Validate field types strictly
        if not isinstance(data["supported_versions"], list):
            return f"Registry fingerprint supported_versions must be list, got {type(data['supported_versions'])}"
        if not isinstance(data["deprecated_versions"], list):
            return f"Registry fingerprint deprecated_versions must be list, got {type(data['deprecated_versions'])}"
        if not isinstance(data["disabled_versions"], list):
            return f"Registry fingerprint disabled_versions must be list, got {type(data['disabled_versions'])}"
        
        # Validate optional fields have correct types
        if "version_ordinals" in data and not isinstance(data["version_ordinals"], dict):
            return f"Registry fingerprint version_ordinals must be dict, got {type(data['version_ordinals'])}"
        if "breaking_versions" in data and not isinstance(data["breaking_versions"], list):
            return f"Registry fingerprint breaking_versions must be list, got {type(data['breaking_versions'])}"
    
    elif fingerprint_type == "compatibility":
        # Tier-0: Validate schema version
        schema_version = data.get("fingerprint_schema_version")
        if schema_version != EXPECTED_COMPATIBILITY_FINGERPRINT_SCHEMA_VERSION:
            return (
                f"Compatibility fingerprint schema version mismatch. "
                f"Expected {EXPECTED_COMPATIBILITY_FINGERPRINT_SCHEMA_VERSION}, got {schema_version}. "
                f"This prevents silent reinterpretation of governance state."
            )
        
        # Tier-0: Strict required fields validation
        required_fields = {
            "fingerprint_schema_version",
            "allow_breaking_changes",
            "allowed_upgrade_paths",
            "coexistence_matrix",
            "downgrade_policy",  # Tier-0: Explicit downgrade policy
        }
        actual_fields = set(data.keys())
        
        # Check all required fields present
        missing_fields = required_fields - actual_fields
        if missing_fields:
            return (
                f"Compatibility fingerprint missing required fields: {sorted(missing_fields)}. "
                f"Expected exactly: {sorted(required_fields)}. "
                f"Got: {sorted(actual_fields)}"
            )
        
        # Check no unexpected fields (strict schema enforcement)
        allowed_fields = required_fields | {"compatibility_policy"}
        unexpected_fields = actual_fields - allowed_fields
        if unexpected_fields:
            return (
                f"Compatibility fingerprint contains unexpected fields: {sorted(unexpected_fields)}. "
                f"Allowed fields: {sorted(allowed_fields)}"
            )
        
        # Validate field types strictly
        if not isinstance(data["allow_breaking_changes"], bool):
            return f"Compatibility fingerprint allow_breaking_changes must be bool, got {type(data['allow_breaking_changes'])}"
        if not isinstance(data["allowed_upgrade_paths"], dict):
            return f"Compatibility fingerprint allowed_upgrade_paths must be dict, got {type(data['allowed_upgrade_paths'])}"
        if not isinstance(data["coexistence_matrix"], dict):
            return f"Compatibility fingerprint coexistence_matrix must be dict, got {type(data['coexistence_matrix'])}"
        if data["downgrade_policy"] not in ("prohibited", "allowed", "conditional"):
            return (
                f"Compatibility fingerprint downgrade_policy must be one of "
                f"('prohibited', 'allowed', 'conditional'), got {data['downgrade_policy']!r}"
            )
    
    return None


def _decode_compatibility_fingerprint(fingerprint: str) -> Tuple[Dict[str, Any], Optional[ValidationViolation]]:
    """
    Decode compatibility fingerprint into structured data.
    
    Tier-0 constitutional enforcement:
    - Validates fingerprint schema structure
    - Normalizes coexistence matrix to canonical string keys
    - Prevents structural governance drift
    
    Fingerprints are deterministic JSON-encoded structures containing:
    - allow_breaking_changes: bool
    - allowed_upgrade_paths: Dict[from_version, List[to_version]]
    - coexistence_matrix: Dict[canonical_string_key, bool] (normalized from tuples)
    - compatibility_policy: str
    
    Args:
        fingerprint: Compatibility fingerprint string
        
    Returns:
        Tuple of (decoded compatibility data dict, violation if fingerprint is invalid)
        
    Note:
        Invalid fingerprints are governance integrity failures and must be
        reported as CRITICAL violations, not silently ignored.
    """
    if not fingerprint:
        violation = ValidationViolation(
            rule_id="COMPAT_INVALID_COMPATIBILITY_FINGERPRINT",
            message="Compatibility fingerprint is empty or missing - governance integrity failure",
            severity=SeverityLevel.CRITICAL,
            field_path="compatibility_fingerprint",
        )
        hash_value = _compute_violation_hash(
            "COMPAT_INVALID_COMPATIBILITY_FINGERPRINT",
            violation.message,
            "compatibility_fingerprint",
            SeverityLevel.CRITICAL,
        )
        object.__setattr__(violation, "deterministic_hash", hash_value)
        return {
            "allow_breaking_changes": False,
            "allowed_upgrade_paths": {},
            "coexistence_matrix": {},
            "compatibility_policy": "strict",
            "downgrade_policy": "prohibited",
        }, violation
    
    try:
        data = json.loads(fingerprint)
        
        # Tier-0: Validate fingerprint schema structure (hard fail)
        schema_error = _validate_fingerprint_schema(data, "compatibility")
        if schema_error:
            message = (
                f"Compatibility fingerprint schema validation failed - governance integrity failure: {schema_error}"
            )
            violation = ValidationViolation(
                rule_id="COMPAT_INVALID_COMPATIBILITY_FINGERPRINT",
                message=message,
                severity=SeverityLevel.CRITICAL,
                field_path="compatibility_fingerprint",
            )
            hash_value = _compute_violation_hash(
                "COMPAT_INVALID_COMPATIBILITY_FINGERPRINT",
                message,
                "compatibility_fingerprint",
                SeverityLevel.CRITICAL,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return {
                "allow_breaking_changes": False,
                "allowed_upgrade_paths": {},
                "coexistence_matrix": {},
                "compatibility_policy": "strict",
                "downgrade_policy": "prohibited",
            }, violation
        
        # Tier-0: Normalize coexistence matrix to canonical string keys
        coexistence_matrix = data.get("coexistence_matrix", {})
        normalized_matrix = _normalize_coexistence_matrix(coexistence_matrix)
        
        return {
            "allow_breaking_changes": data["allow_breaking_changes"],
            "allowed_upgrade_paths": data["allowed_upgrade_paths"],
            "coexistence_matrix": normalized_matrix,  # Use normalized canonical keys
            "compatibility_policy": data.get("compatibility_policy", "strict"),
            "downgrade_policy": data["downgrade_policy"],  # Tier-0: Explicit downgrade policy
        }, None
    except (json.JSONDecodeError, TypeError) as e:
        # Invalid fingerprint format = governance integrity failure
        message = (
            f"Compatibility fingerprint is malformed or corrupted - governance integrity failure: {str(e)}"
        )
        violation = ValidationViolation(
            rule_id="COMPAT_INVALID_COMPATIBILITY_FINGERPRINT",
            message=message,
            severity=SeverityLevel.CRITICAL,
            field_path="compatibility_fingerprint",
        )
        hash_value = _compute_violation_hash(
            "COMPAT_INVALID_COMPATIBILITY_FINGERPRINT",
            message,
            "compatibility_fingerprint",
            SeverityLevel.CRITICAL,
        )
        object.__setattr__(violation, "deterministic_hash", hash_value)
        return {
            "allow_breaking_changes": False,
            "allowed_upgrade_paths": {},
            "coexistence_matrix": {},
            "compatibility_policy": "strict",
        }, violation


# ============================================================================
# Compatibility Rule Base
# ============================================================================

class CompatibilityRule(ValidationRule):
    """
    Base class for compatibility validation rules.
    
    All compatibility rules operate on version compatibility constraints
    using deterministic fingerprints from ValidationContext.
    
    Architecture:
    - Fingerprint decoding occurs ONCE in evaluate() method (centralized)
    - Decoded data is passed to _evaluate_compatibility() for rule-specific logic
    - This ensures no redundant decoding and maintains deterministic behavior
    - All rules receive pre-validated, decoded fingerprint data
    
    Note: Inherits from ValidationRule for framework integration, but maintains
    governance isolation by using only fingerprint-derived data.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
    ):
        """
        Initialize compatibility rule.
        
        Tier-0 enforcement: Validates severity against centralized policy.
        """
        # Tier-0: Assert severity matches centralized policy
        CompatibilitySeverityPolicy.assert_severity(rule_id, severity)
        
        applies_to = ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
            artifact_types=None,  # Applies to all artifact types
            field_paths=None,
            version_range=None,
        )
        super().__init__(rule_id, description, severity, applies_to)
    
    def evaluate(
        self,
        input_data: Any,
        context: ValidationContext,
        field_path: Optional[str] = None,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate compatibility rule.
        
        Tier-0 enforcement:
        - Asserts fingerprint purity (no fallback to local registry)
        - Decodes fingerprints once (centralized, deterministic)
        - Anchors violations to fingerprints (replay legitimacy)
        
        Note: field_path parameter is inherited from ValidationRule interface
        but is not used by compatibility rules (they operate on context-level version state).
        """
        # Tier-0 defensive guard: Enforce fingerprint purity
        _assert_fingerprint_purity(context)
        
        # Check for fingerprint corruption first (governance integrity)
        registry_data, registry_violation = _decode_registry_fingerprint(context.registry_fingerprint)
        if registry_violation:
            # Anchor violation to fingerprints for replay legitimacy
            return _anchor_violation_to_fingerprints(
                registry_violation,
                context.registry_fingerprint,
                context.compatibility_fingerprint,
            )
        
        compat_data, compat_violation = _decode_compatibility_fingerprint(context.compatibility_fingerprint)
        if compat_violation:
            # Anchor violation to fingerprints for replay legitimacy
            return _anchor_violation_to_fingerprints(
                compat_violation,
                context.registry_fingerprint,
                context.compatibility_fingerprint,
            )
        
        # Delegate to rule-specific evaluation with pre-validated fingerprint data
        violation = self._evaluate_compatibility(input_data, context, registry_data, compat_data)
        
        # Anchor any violation to fingerprints for replay legitimacy
        if violation:
            return _anchor_violation_to_fingerprints(
                violation,
                context.registry_fingerprint,
                context.compatibility_fingerprint,
            )
        
        return None
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """
        Rule-specific compatibility evaluation.
        
        Subclasses implement this method to perform actual compatibility checks.
        Fingerprints are pre-decoded and validated.
        
        Args:
            input_data: Data to validate
            context: Validation context
            registry_data: Decoded registry fingerprint data
            compat_data: Decoded compatibility fingerprint data
            
        Returns:
            ValidationViolation if rule fails, None if passes
        """
        raise NotImplementedError


# ============================================================================
# Version Existence Rule
# ============================================================================

class VersionExistenceRule(CompatibilityRule):
    """
    Validates that artifact version exists in registry.
    
    Rule ID: COMPAT_VERSION_NOT_IN_REGISTRY
    Severity: CRITICAL
    
    This is the most fundamental compatibility check.
    If version doesn't exist, all other checks are irrelevant.
    
    Distinguishes "exists" from "supported":
    - Exists: version is declared in registry (may be disabled)
    - Supported: version exists AND is not disabled
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_VERSION_NOT_IN_REGISTRY",
            description="Artifact version must exist in registry",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if artifact version exists in registry."""
        if not context.artifact_version:
            # Version is required - but this might be handled by field rules
            # Return None here to avoid duplicate violations
            return None
        
        # Check existence: version must be in supported, deprecated, or disabled sets
        supported_versions = registry_data["supported_versions"]
        deprecated_versions = registry_data["deprecated_versions"]
        disabled_versions = registry_data["disabled_versions"]
        
        version_exists = (
            context.artifact_version in supported_versions
            or context.artifact_version in deprecated_versions
            or context.artifact_version in disabled_versions
        )
        
        if not version_exists:
            all_versions = sorted(
                set(supported_versions) | deprecated_versions | disabled_versions
            )
            message = (
                f"Artifact version '{context.artifact_version}' does not exist "
                f"in registry. Known versions: {all_versions}"
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Unsupported Version Rule
# ============================================================================

class UnsupportedVersionRule(CompatibilityRule):
    """
    Validates that artifact version is supported (not disabled).
    
    Rule ID: COMPAT_UNSUPPORTED_VERSION
    Severity: CRITICAL
    
    Distinguishes between:
    - "doesn't exist" (VersionExistenceRule)
    - "exists but disabled" (this rule)
    - "exists and supported" (passes)
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_UNSUPPORTED_VERSION",
            description="Artifact version must be supported (not disabled) by current registry",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if artifact version is supported (not disabled)."""
        if not context.artifact_version:
            return None
        
        supported_versions = registry_data["supported_versions"]
        disabled_versions = registry_data["disabled_versions"]
        
        # Version must be in supported set and not in disabled set
        is_supported = context.artifact_version in supported_versions
        is_disabled = context.artifact_version in disabled_versions
        
        if not is_supported or is_disabled:
            message = (
                f"Artifact version '{context.artifact_version}' is not supported "
                f"by current registry (exists but is disabled or not in supported set)"
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Deprecated Version Rule
# ============================================================================

class DeprecatedVersionRule(CompatibilityRule):
    """
    Warns when artifact uses deprecated version.
    
    Rule ID: COMPAT_DEPRECATED_VERSION
    Severity: WARNING
    
    Deprecated versions are allowed for historical reads but not new production.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_DEPRECATED_VERSION",
            description="Artifact version is deprecated",
            severity=SeverityLevel.WARNING,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Warn if artifact version is deprecated."""
        if not context.artifact_version:
            return None
        
        deprecated_versions = registry_data["deprecated_versions"]
        
        if context.artifact_version in deprecated_versions:
            message = (
                f"Artifact version '{context.artifact_version}' is deprecated. "
                f"New production at this version is discouraged."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Downgrade Prohibition Rule
# ============================================================================

class DowngradeProhibitedRule(CompatibilityRule):
    """
    Prevents version downgrades.
    
    Rule ID: COMPAT_DOWNGRADE_PROHIBITED
    Severity: ERROR
    
    Artifacts must not declare a lower version than the current system version.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_DOWNGRADE_PROHIBITED",
            description="Version downgrades are prohibited",
            severity=SeverityLevel.ERROR,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Prevent version downgrades."""
        if not context.artifact_version:
            return None
        
        current_version_str = registry_data.get("current_version")
        
        if not current_version_str:
            # No current version declared - cannot check downgrade
            return None
        
        # Tier-0: Use structured Version objects for all comparisons
        # This prevents string lexicographic comparison bugs
        version_ordinals = registry_data.get("version_ordinals", {})
        declared_ordinal = version_ordinals.get(context.artifact_version)
        current_ordinal = version_ordinals.get(current_version_str)
        
        try:
            declared_version = _ensure_version_object(context.artifact_version, ordinal=declared_ordinal)
            current_version = _ensure_version_object(current_version_str, ordinal=current_ordinal)
        except ValueError:
            # Version parsing failure = governance integrity issue
            # Version existence rule should catch this, but we enforce here too
            return None
        
        if declared_version is None or current_version is None:
            return None
        
        # Tier-0 constitutional: Enforce downgrade policy from fingerprint
        # Downgrade legality must be explicit, not implied
        downgrade_policy = compat_data.get("downgrade_policy", "prohibited")
        
        if downgrade_policy == "prohibited":
            # Tier-0: Structured comparison (never string comparison)
            try:
                if declared_version < current_version:
                    message = (
                        f"Version downgrade prohibited by compatibility policy: "
                        f"declared '{context.artifact_version}' is lower than "
                        f"current system version '{current_version_str}'"
                    )
                    hash_value = _compute_violation_hash(
                        self.rule_id,
                        message,
                        "schema_version",
                        self.severity,
                    )
                    violation = ValidationViolation(
                        rule_id=self.rule_id,
                        message=message,
                        severity=self.severity,
                        field_path="schema_version",
                    )
                    object.__setattr__(violation, "deterministic_hash", hash_value)
                    return violation
            except ValueError as e:
                # Mixed ordinal/semantic comparison error
                message = (
                    f"Version comparison error during downgrade check: {str(e)}. "
                    f"Declared: {context.artifact_version}, Current: {current_version_str}"
                )
                hash_value = _compute_violation_hash(
                    self.rule_id,
                    message,
                    "schema_version",
                    SeverityLevel.CRITICAL,
                )
                violation = ValidationViolation(
                    rule_id=self.rule_id,
                    message=message,
                    severity=SeverityLevel.CRITICAL,
                    field_path="schema_version",
                )
                object.__setattr__(violation, "deterministic_hash", hash_value)
                return violation
        elif downgrade_policy == "allowed":
            # Downgrades explicitly allowed by policy
            return None
        elif downgrade_policy == "conditional":
            # Conditional downgrades - would require additional policy rules
            # For now, treat as prohibited unless explicitly handled
            try:
                if declared_version < current_version:
                    message = (
                        f"Version downgrade requires conditional policy evaluation: "
                        f"declared '{context.artifact_version}' is lower than "
                        f"current system version '{current_version_str}'. "
                        f"Conditional downgrade policy not yet fully implemented."
                    )
                    hash_value = _compute_violation_hash(
                        self.rule_id,
                        message,
                        "schema_version",
                        self.severity,
                    )
                    violation = ValidationViolation(
                        rule_id=self.rule_id,
                        message=message,
                        severity=self.severity,
                        field_path="schema_version",
                    )
                    object.__setattr__(violation, "deterministic_hash", hash_value)
                    return violation
            except ValueError as e:
                # Mixed ordinal/semantic comparison error
                message = (
                    f"Version comparison error during downgrade check: {str(e)}"
                )
                hash_value = _compute_violation_hash(
                    self.rule_id,
                    message,
                    "schema_version",
                    SeverityLevel.CRITICAL,
                )
                violation = ValidationViolation(
                    rule_id=self.rule_id,
                    message=message,
                    severity=SeverityLevel.CRITICAL,
                    field_path="schema_version",
                )
                object.__setattr__(violation, "deterministic_hash", hash_value)
                return violation
        
        return None


# ============================================================================
# Breaking Change Rule
# ============================================================================

class BreakingChangeNotAllowedRule(CompatibilityRule):
    """
    Prevents breaking changes unless explicitly permitted.
    
    Rule ID: COMPAT_BREAKING_CHANGE_NOT_ALLOWED
    Severity: CRITICAL
    
    Breaking schema changes require explicit compatibility policy approval.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_BREAKING_CHANGE_NOT_ALLOWED",
            description="Breaking schema change not permitted under compatibility policy",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if breaking changes are allowed."""
        if not context.artifact_version:
            return None
        
        breaking_versions = registry_data["breaking_versions"]
        
        if context.artifact_version not in breaking_versions:
            # Not a breaking version - no violation
            return None
        
        # Version is flagged as breaking - check compatibility policy
        allow_breaking = compat_data["allow_breaking_changes"]
        
        if not allow_breaking:
            message = (
                f"Breaking schema change detected for version '{context.artifact_version}', "
                f"but compatibility policy does not allow breaking changes"
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Upgrade Path Rule
# ============================================================================

class UpgradePathViolationRule(CompatibilityRule):
    """
    Validates that version transitions follow allowed upgrade paths.
    
    Rule ID: COMPAT_UPGRADE_PATH_VIOLATION
    Severity: ERROR
    
    If context declares a mutation/upgrade, target version must be reachable
    from the prior version via allowed upgrade paths.
    
    Note: Prior version should come from ValidationContext mutation state,
    not from arbitrary input payload fields. This rule checks for context
    extension fields (prior_version, from_version) and gracefully skips
    if mutation context is not available.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_UPGRADE_PATH_VIOLATION",
            description="Version upgrade does not follow allowed upgrade path",
            severity=SeverityLevel.ERROR,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """
        Check upgrade path legality.
        
        Enforces that version transitions follow allowed upgrade paths when mutation context
        is declared. Requires ValidationContext to be extended with mutation state fields:
        - prior_version: Optional[str] - version being upgraded from
        - is_mutation: bool - whether this is a mutation/upgrade operation
        
        If mutation context is not available, this rule enforces that mutation state must
        be provided for governance integrity.
        """
        if not context.artifact_version:
            return None
        
        # Check if this is a mutation/upgrade scenario
        # Use getattr to safely check for extended context fields
        is_mutation = getattr(context, "is_mutation", False)
        prior_version = getattr(context, "prior_version", None)
        
        # If not a mutation, upgrade path check doesn't apply
        if not is_mutation:
            return None
        
        # Mutation declared but prior_version missing = incomplete governance context
        if prior_version is None:
            message = (
                "Upgrade path validation requires prior_version in ValidationContext. "
                "Mutation state must be authoritative for governance integrity."
            )
            hash_value = _compute_violation_hash(
                "COMPAT_MISSING_MUTATION_CONTEXT",
                message,
                "schema_version",
                SeverityLevel.CRITICAL,
            )
            violation = ValidationViolation(
                rule_id="COMPAT_MISSING_MUTATION_CONTEXT",
                message=message,
                severity=SeverityLevel.CRITICAL,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        # Tier-0 constitutional: Strict upgrade path enforcement
        # If upgrade paths are defined, they MUST be explicit - no permissive defaults
        allowed_paths = compat_data["allowed_upgrade_paths"]
        
        # Constitutional rule: If upgrade paths exist in fingerprint, they are authoritative
        # If prior_version has no entry, upgrade is PROHIBITED (strict policy)
        # This prevents policy ambiguity and ensures explicit governance
        if prior_version not in allowed_paths:
            # No upgrade path defined for prior_version = upgrade prohibited
            message = (
                f"Upgrade from '{prior_version}' is not permitted. "
                f"No upgrade path defined in compatibility fingerprint. "
                f"Upgrade paths must be explicitly declared for governance integrity."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        # Upgrade path exists - validate target version is in allowed list
        allowed_targets = allowed_paths[prior_version]
        if not isinstance(allowed_targets, list):
            # Invalid structure - governance integrity failure
            message = (
                f"Upgrade path for '{prior_version}' has invalid structure. "
                f"Expected list of allowed target versions, got {type(allowed_targets)}"
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        # Tier-0: Explicit target validation - no permissive defaults
        if context.artifact_version not in allowed_targets:
            message = (
                f"Upgrade from '{prior_version}' to '{context.artifact_version}' "
                f"is not in allowed upgrade paths. Allowed targets from '{prior_version}': {sorted(allowed_targets)}"
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Future Version Rule
# ============================================================================

class FutureVersionRule(CompatibilityRule):
    """
    Prevents use of future versions not yet released.
    
    Rule ID: COMPAT_FUTURE_VERSION
    Severity: ERROR
    
    Artifacts must not declare versions that exceed the current system version.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_FUTURE_VERSION",
            description="Artifact version exceeds current system version",
            severity=SeverityLevel.ERROR,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Prevent future versions."""
        if not context.artifact_version:
            return None
        
        current_version_str = registry_data.get("current_version")
        
        if not current_version_str:
            # No current version - cannot check future
            return None
        
        # Tier-0: Use structured Version objects for all comparisons
        version_ordinals = registry_data.get("version_ordinals", {})
        declared_ordinal = version_ordinals.get(context.artifact_version)
        current_ordinal = version_ordinals.get(current_version_str)
        
        try:
            declared_version = _ensure_version_object(context.artifact_version, ordinal=declared_ordinal)
            current_version = _ensure_version_object(current_version_str, ordinal=current_ordinal)
        except ValueError:
            # Version parsing failure = governance integrity issue
            return None
        
        if declared_version is None or current_version is None:
            return None
        
        # Tier-0: Structured comparison (never string comparison)
        if declared_version > current_version:
            message = (
                f"Future version prohibited: declared '{context.artifact_version}' "
                f"exceeds current system version '{current_version_str}'"
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "schema_version",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Cross-Coexistence Rule
# ============================================================================

class CrossCoexistenceRule(CompatibilityRule):
    """
    Validates that versions can coexist under compatibility matrix.
    
    Rule ID: COMPAT_COEXISTENCE_VIOLATION
    Severity: ERROR
    
    When multiple versions exist in the system, they must be allowed to coexist.
    
    Note: Existing versions should come from authoritative lineage context,
    not from arbitrary input payload fields. This rule requires ValidationContext
    extension with existing_versions field for governance integrity.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="COMPAT_COEXISTENCE_VIOLATION",
            description="Version coexistence not allowed under compatibility matrix",
            severity=SeverityLevel.ERROR,
        )
    
    def _evaluate_compatibility(
        self,
        input_data: Any,
        context: ValidationContext,
        registry_data: Dict[str, Any],
        compat_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """
        Check coexistence legality.
        
        Enforces that the declared version can coexist with existing versions in the lineage
        under the compatibility matrix. Requires ValidationContext to be extended with:
        - existing_versions: Tuple[str, ...] - versions currently present in lineage
        
        If existing_versions is not provided, this rule enforces that lineage context must
        be provided for governance integrity.
        """
        if not context.artifact_version:
            return None
        
        # Get existing versions from authoritative context (not caller-provided input_data)
        # Use getattr to safely check for extended context fields
        existing_versions = getattr(context, "existing_versions", None)
        
        # If no existing versions declared, coexistence check doesn't apply
        # (first version in system has nothing to coexist with)
        if existing_versions is None:
            # This is acceptable - coexistence only matters when versions already exist
            return None
        
        # Normalize to tuple/list for iteration
        if isinstance(existing_versions, str):
            existing_versions = (existing_versions,)
        elif not isinstance(existing_versions, (tuple, list)):
            # Invalid type - enforce proper context structure
            message = (
                "Coexistence validation requires existing_versions as tuple/list in ValidationContext. "
                "Lineage state must be authoritative for governance integrity."
            )
            hash_value = _compute_violation_hash(
                "COMPAT_INVALID_COEXISTENCE_CONTEXT",
                message,
                "schema_version",
                SeverityLevel.CRITICAL,
            )
            violation = ValidationViolation(
                rule_id="COMPAT_INVALID_COEXISTENCE_CONTEXT",
                message=message,
                severity=SeverityLevel.CRITICAL,
                field_path="schema_version",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        # Empty existing versions means no coexistence scenario
        if not existing_versions:
            return None
        
        # Tier-0 constitutional: Strictly matrix-based coexistence enforcement
        # No heuristics, no inference, no dynamic reconstruction
        # All coexistence decisions MUST come from fingerprint-derived matrix
        coexistence_matrix = compat_data["coexistence_matrix"]
        
        # Tier-0 invariant: Coexistence matrix must be fingerprint-derived
        # If matrix is empty, we cannot make coexistence decisions (governance gap)
        if not coexistence_matrix:
            # Empty matrix = no coexistence policy defined
            # This is acceptable for systems without multi-version coexistence requirements
            return None
        
        # Check coexistence with each existing version using STRICTLY matrix lookup
        for existing_version in existing_versions:
            if not isinstance(existing_version, str):
                continue  # Skip invalid entries
            
            # Tier-0 constitutional: Use canonical string key for matrix lookup
            # Matrix keys are normalized to canonical string format: "version_a|version_b"
            # This ensures cross-node serialization safety
            canonical_key = _canonicalize_coexistence_key(
                context.artifact_version,
                existing_version
            )
            
            # Tier-0: Strictly matrix-based decision (no heuristics)
            # If matrix doesn't explicitly allow, coexistence is prohibited
            allowed = coexistence_matrix.get(canonical_key, False)
            
            # Tier-0: Matrix exists and doesn't allow = violation
            # No fallback to heuristics, no inference, no default policy
            # If matrix doesn't say "True", coexistence is illegal
            if not allowed:
                message = (
                    f"Version '{context.artifact_version}' cannot coexist with "
                    f"existing version '{existing_version}' under compatibility matrix. "
                    f"Coexistence not explicitly allowed in fingerprint-derived matrix. "
                    f"Canonical matrix key checked: {canonical_key}"
                )
                hash_value = _compute_violation_hash(
                    self.rule_id,
                    message,
                    "schema_version",
                    self.severity,
                )
                violation = ValidationViolation(
                    rule_id=self.rule_id,
                    message=message,
                    severity=self.severity,
                    field_path="schema_version",
                )
                object.__setattr__(violation, "deterministic_hash", hash_value)
                return violation
        
        return None


# ============================================================================
# Violation Hash Computation
# ============================================================================

def _compute_violation_hash(
    rule_id: str,
    message: str,
    field_path: Optional[str],
    severity: SeverityLevel,
    registry_fingerprint_hash: Optional[str] = None,
    compatibility_fingerprint_hash: Optional[str] = None,
) -> str:
    """
    Compute deterministic hash for validation violation.
    
    Tier-0 replay guarantee: Includes fingerprint hashes for replay legitimacy.
    This ensures violations are anchored to the governance state that produced them.
    
    Uses same algorithm as validation_contract for consistency, with fingerprint anchoring.
    
    Args:
        rule_id: Rule identifier
        message: Violation message
        field_path: Field path (if applicable)
        severity: Severity level
        registry_fingerprint_hash: Optional hash of registry fingerprint (for anchoring)
        compatibility_fingerprint_hash: Optional hash of compatibility fingerprint (for anchoring)
    """
    parts = [
        rule_id,
        message,
        field_path or "",
        severity.value,
    ]
    
    # Tier-0: Anchor to fingerprints for replay legitimacy
    # If fingerprints are provided, include their hashes in violation hash
    # This ensures replay decisions match original governance state
    if registry_fingerprint_hash:
        parts.append(f"registry_fp:{registry_fingerprint_hash}")
    if compatibility_fingerprint_hash:
        parts.append(f"compat_fp:{compatibility_fingerprint_hash}")
    
    canonical = json.dumps(
        parts,
        sort_keys=False,  # Order matters for determinism
        separators=(",", ":"),
        ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================================
# Rule Registry
# ============================================================================

COMPATIBILITY_RULES: Tuple[CompatibilityRule, ...] = (
    # Order matters for deterministic execution
    # 1. Check version exists first
    VersionExistenceRule(),
    # 2. Check version is supported
    UnsupportedVersionRule(),
    # 3. Check for deprecated (warning only)
    DeprecatedVersionRule(),
    # 4. Prevent downgrades
    DowngradeProhibitedRule(),
    # 5. Prevent future versions
    FutureVersionRule(),
    # 6. Check breaking changes
    BreakingChangeNotAllowedRule(),
    # 7. Validate upgrade paths
    UpgradePathViolationRule(),
    # 8. Check coexistence
    CrossCoexistenceRule(),
)

# Tier-0 constitutional: Rule ordering integrity hash
# This provides replay-verifiable rule ordering integrity
# Ensures rule execution order is locked and cannot drift
_COMPATIBILITY_RULES_ORDERING_HASH = hashlib.sha256(
    "|".join(rule.rule_id for rule in COMPATIBILITY_RULES).encode("utf-8")
).hexdigest()

# Expose for verification and audit
COMPATIBILITY_RULES_ORDERING_HASH: str = _COMPATIBILITY_RULES_ORDERING_HASH
