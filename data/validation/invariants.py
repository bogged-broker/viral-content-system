"""
/data/validation/invariants.py

System-Wide Truth Enforcement Specification
(Constitutional Integrity Gate)

---

1️⃣ Purpose

This file enforces:

Hash integrity (canonical JSON matches declared hash)

ID immutability (artifact IDs cannot change)

Lineage parent existence (parent artifacts must exist)

Fingerprint matching (declared fingerprints match computed)

Content integrity (canonical representation matches declared)

Identity stability (artifact identity fields are immutable)

Derivation legitimacy (derived artifacts reference valid parents)

This layer answers:

> "Even if this artifact is structurally, semantically, and version-compatible — does it satisfy system-wide constitutional truths?"

Invariants are not syntax.
Invariants are not compatibility.
Invariants are constitutional law.

---

2️⃣ Architectural Position

Validation stack:

1. field_rules.py → atomic integrity

2. semantic_rules.py → internal coherence

3. compatibility_guards.py → cross-version legality

4. invariants.py → system-wide truth enforcement

5. deterministic_checks.py → replay safety

Invariants execute after compatibility guards because:

If version is illegal, invariant enforcement is irrelevant.

But once version is legal, constitutional truths must hold.

No hash or lineage check matters if version is prohibited.

But if version is legal, hash and lineage must be correct.

---

3️⃣ Core Responsibility

This file enforces that:

Declared hash matches canonical JSON representation.

Artifact IDs are immutable across mutations.

Parent artifacts exist in lineage (if declared).

Fingerprints match computed values.

Content integrity is preserved.

Identity fields cannot be mutated.

Derivation relationships are legitimate.

---

4️⃣ Inputs

Invariants rely on:

ValidationContext.artifact_version
ValidationContext.invariants_fingerprint
ValidationContext.registry_fingerprint (for lineage lookups via fingerprint)
ValidationContext.compatibility_fingerprint (for cross-version checks)

Extended context fields (via getattr):
- parent_artifact_id (for lineage parent existence)
- existing_artifact_id (for ID immutability checks)
- declared_hash (for hash matching)
- declared_fingerprint (for fingerprint matching)

They must NOT:

Query lineage graph directly.

Pull parent artifacts dynamically.

Read external state.

Use caller-provided lineage data from input_data.

The validator receives deterministic fingerprints from the caller.

That keeps validation pure.

Fingerprint Decoding Architecture:

Fingerprints are decoded ONCE in InvariantRule.evaluate() base method,
then passed to rule-specific _evaluate_invariant() methods. This ensures:

- No redundant decoding across rules
- Deterministic single-pass decoding
- Performance optimization for high-traffic systems
- Centralized fingerprint validation

---

5️⃣ Types of Invariant Enforcement

1️⃣ Hash Integrity Check

Declared hash must match canonical JSON representation.

2️⃣ ID Immutability Check

Artifact ID must not change if artifact already exists.

3️⃣ Lineage Parent Existence Check

If parent_artifact_id is declared, parent must exist in lineage.

4️⃣ Fingerprint Matching Check

Declared fingerprint must match computed fingerprint.

5️⃣ Content Integrity Check

Canonical representation must match declared content hash.

6️⃣ Identity Stability Check

Identity fields (id, type, version) must be immutable.

7️⃣ Derivation Legitimacy Check

Derived artifacts must reference valid parent artifacts.

---

6️⃣ Rule Interface

Each invariant rule must implement:

class InvariantRule:
    rule_id: str
    severity: SeverityLevel

    def evaluate(self, input_data, context) -> ValidationViolation | None:
        ...

They must:

Not call lineage API directly.

Not read files.

Not query database.

Use only context-provided fingerprints or lineage data.

---

7️⃣ Example: Hash Integrity Rule

class HashIntegrityRule:
    rule_id = "INV_HASH_MISMATCH"
    severity = SeverityLevel.CRITICAL

    def evaluate(self, input_data, context):
        declared_hash = getattr(context, "declared_hash", None)
        
        if not declared_hash:
            return None  # Hash not required for all artifacts
        
        canonical_json = json.dumps(input_data, sort_keys=True, separators=(',', ':'))
        computed_hash = hashlib.sha256(canonical_json.encode()).hexdigest()
        
        if declared_hash != computed_hash:
            return build_violation(
                self.rule_id,
                f"Declared hash {declared_hash} does not match computed hash {computed_hash}",
                self.severity,
                field_path="hash",
            )
        return None

Important:

Hash computation must be deterministic and canonical.

---

8️⃣ Example: ID Immutability Rule

class IDImmutabilityRule:
    rule_id = "INV_ID_MUTATION"
    severity = SeverityLevel.CRITICAL

    def evaluate(self, input_data, context):
        artifact_id = input_data.get("id")
        existing_id = getattr(context, "existing_artifact_id", None)
        
        if not artifact_id or not existing_id:
            return None  # Not applicable if no existing artifact
        
        if artifact_id != existing_id:
            return build_violation(
                self.rule_id,
                f"Artifact ID cannot be mutated: declared {artifact_id}, existing {existing_id}",
                self.severity,
                field_path="id",
            )
        return None

ID immutability is a constitutional requirement.

---

9️⃣ Example: Lineage Parent Existence Rule

class LineageParentExistenceRule:
    rule_id = "INV_PARENT_NOT_FOUND"
    severity = SeverityLevel.ERROR

    def evaluate(self, input_data, context):
        parent_id = input_data.get("parent_artifact_id")
        
        if not parent_id:
            return None  # Not all artifacts have parents
        
        # Check parent existence via invariants fingerprint
        parent_exists = check_parent_exists(parent_id, context.invariants_fingerprint)
        
        if not parent_exists:
            return build_violation(
                self.rule_id,
                f"Parent artifact {parent_id} does not exist in lineage",
                self.severity,
                field_path="parent_artifact_id",
            )
        return None

Parent existence must be verified via fingerprint-derived lineage state.

---

🔟 Rule Registry

Expose static registry:

INVARIANT_RULES = [
    HashIntegrityRule(),
    IDImmutabilityRule(),
    LineageParentExistenceRule(),
    ...
]

Rules must:

Not be dynamically injected.

Not vary between nodes.

Be static at import time.

Be sorted by orchestrator.

---

1️⃣1️⃣ Hash Computation Requirements

Hash computation must:

Use canonical JSON representation.

Use deterministic sorting.

Use consistent separators.

Never include timestamps.

Never include random values.

Be deterministic.

Example safe computation:

canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
hash_value = hashlib.sha256(canonical.encode()).hexdigest()

No semantic interpretation ambiguity.

---

1️⃣2️⃣ Relationship to /data/lineage/

Invariants depend conceptually on:

lineage_graph.py

artifact_store.py

hash_computation.py

However:

They cannot call them dynamically.

Instead:

Calling layer computes invariants fingerprint, which is passed through ValidationContext.

Validation uses fingerprint to ensure:

Lineage state has not changed between nodes.

---

1️⃣3️⃣ Determinism Constraints

Invariant checks must not:

Use runtime lineage lookups.

Use global mutable state.

Depend on node-specific configuration.

Depend on wall-clock date.

Check feature flags unless fingerprinted.

All invariant decision inputs must be deterministic from:

context
input_data

Only.

---

1️⃣4️⃣ Failure Semantics

Invariant violations are severe.

Severity recommendations:

Hash mismatch → CRITICAL

ID mutation → CRITICAL

Parent not found → ERROR

Fingerprint mismatch → CRITICAL

Content corruption → CRITICAL

Identity mutation → CRITICAL

Derivation illegitimacy → ERROR

Policy must be explicit.

---

1️⃣5️⃣ Security Role

Invariants prevent:

Hash corruption attacks.

ID manipulation attacks.

Orphaned artifact injection.

Fingerprint spoofing.

Content tampering.

Identity mutation attacks.

Invalid derivation chains.

Lineage graph corruption.

Without this layer:

Your lineage becomes untrustworthy.

Attackers can inject corrupted hashes, mutate IDs, create orphaned artifacts,
spoof fingerprints, tamper with content, and corrupt derivation chains.

---

1️⃣6️⃣ Replay Protection

Invariant enforcement must behave identically during replay.

Which requires:

Invariants fingerprint included in context.

Deterministic rule ordering.

Deterministic violation fingerprinting.

Replay must not reinterpret past invariant decisions.

---

1️⃣7️⃣ Testing Requirements

Hash mismatch rejection test.

ID mutation rejection test.

Parent not found rejection test.

Fingerprint mismatch rejection test.

Content corruption rejection test.

Identity mutation rejection test.

Derivation illegitimacy rejection test.

Cross-node fingerprint equivalence test.

Hash computation correctness test.

Mixed compatibility + invariant violation test.

---

1️⃣8️⃣ Interaction With Strict Mode

Strict mode may escalate:

WARNING → rejection.

Invariant CRITICAL must always reject.

Invariant ERROR must always reject.

Strict mode never downgrades severity.

---

1️⃣9️⃣ Absolute Definition

/data/validation/invariants.py is:

> The deterministic system-wide truth enforcement layer that ensures artifacts satisfy constitutional integrity requirements (hash matching, ID immutability, lineage parent existence, fingerprint matching) before any artifact or mutation is permitted to enter the lineage system.

Field rules prevent malformed shape.
Semantic rules prevent contradiction.
Compatibility guards prevent temporal illegality.
Invariants prevent constitutional violation.

---

Next file:

/data/validation/deterministic_checks.py

This is where invariants end and replay safety begins.
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
    Tier-0 defensive guard: Enforce that invariants fingerprint is present and authoritative.
    
    This prevents silent fallback to local lineage lookups or dynamic queries.
    All invariant decisions MUST be fingerprint-derived.
    
    Raises:
        AssertionError: If fingerprint is missing or invalid
    """
    assert context.invariants_fingerprint, (
        "invariants_fingerprint is required for invariant validation. "
        "Cannot fall back to local lineage - all decisions must be fingerprint-derived."
    )
    # Ensure fingerprint is non-empty string (not just whitespace)
    assert context.invariants_fingerprint.strip(), (
        "invariants_fingerprint cannot be empty or whitespace-only. "
        "Governance state must be explicitly provided."
    )


def _compute_canonical_hash(data: Any) -> str:
    """
    Tier-0 constitutional: Compute deterministic canonical hash.
    
    Uses canonical JSON representation with deterministic sorting.
    This ensures cross-node hash equivalence.
    
    Args:
        data: Data to hash (must be JSON-serializable)
        
    Returns:
        SHA-256 hex digest of canonical JSON representation
    """
    # Tier-0: Canonical JSON with deterministic sorting
    canonical = json.dumps(
        data,
        sort_keys=True,  # Deterministic key ordering
        separators=(',', ':'),  # No whitespace
        ensure_ascii=True  # ASCII-only encoding
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _anchor_violation_to_fingerprint(
    violation: ValidationViolation,
    invariants_fingerprint: str,
) -> ValidationViolation:
    """
    Tier-0 replay guarantee: Anchor violation to fingerprint for replay legitimacy.
    
    Ensures that all invariant decisions are explicitly tied to the governance
    fingerprint that produced them. This prevents replay drift after policy updates.
    
    Recomputes violation hash with fingerprint anchor to ensure replay decisions
    match original governance state.
    
    Args:
        violation: Violation to anchor
        invariants_fingerprint: Invariants fingerprint that produced this decision
        
    Returns:
        Violation with fingerprint-anchored hash
    """
    # Compute fingerprint hash for anchoring
    fp_hash = hashlib.sha256(invariants_fingerprint.encode("utf-8")).hexdigest()[:16]
    
    # Recompute hash with fingerprint anchor
    anchored_hash = _compute_violation_hash(
        violation.rule_id,
        violation.message,
        violation.field_path,
        violation.severity,
        invariants_fingerprint_hash=fp_hash,
    )
    
    # Update violation with anchored hash
    object.__setattr__(violation, "deterministic_hash", anchored_hash)
    return violation


# ============================================================================
# Centralized Severity Policy
# ============================================================================

class InvariantSeverityPolicy:
    """
    Tier-0 centralized severity policy enforcement.
    
    Prevents accidental severity drift and ensures consistent governance semantics.
    All invariant rule severities must align with this policy.
    """
    
    # Hash and integrity violations are CRITICAL
    HASH_MISMATCH = SeverityLevel.CRITICAL
    CONTENT_CORRUPTION = SeverityLevel.CRITICAL
    FINGERPRINT_MISMATCH = SeverityLevel.CRITICAL
    
    # ID and identity violations are CRITICAL
    ID_MUTATION = SeverityLevel.CRITICAL
    IDENTITY_MUTATION = SeverityLevel.CRITICAL
    
    # Lineage violations are ERROR
    PARENT_NOT_FOUND = SeverityLevel.ERROR
    DERIVATION_ILLEGITIMATE = SeverityLevel.ERROR
    
    # Fingerprint corruption is CRITICAL (governance integrity failure)
    INVALID_INVARIANTS_FINGERPRINT = SeverityLevel.CRITICAL
    
    @classmethod
    def assert_severity(cls, rule_id: str, severity: SeverityLevel) -> None:
        """
        Assert that rule severity matches centralized policy.
        
        Raises:
            AssertionError: If severity does not match policy
        """
        policy_map = {
            "INV_HASH_MISMATCH": cls.HASH_MISMATCH,
            "INV_CONTENT_CORRUPTION": cls.CONTENT_CORRUPTION,
            "INV_FINGERPRINT_MISMATCH": cls.FINGERPRINT_MISMATCH,
            "INV_ID_MUTATION": cls.ID_MUTATION,
            "INV_IDENTITY_MUTATION": cls.IDENTITY_MUTATION,
            "INV_PARENT_NOT_FOUND": cls.PARENT_NOT_FOUND,
            "INV_DERIVATION_ILLEGITIMATE": cls.DERIVATION_ILLEGITIMATE,
            "INV_INVALID_INVARIANTS_FINGERPRINT": cls.INVALID_INVARIANTS_FINGERPRINT,
        }
        
        expected_severity = policy_map.get(rule_id)
        if expected_severity is not None:
            assert severity == expected_severity, (
                f"Rule {rule_id} severity {severity.value} does not match "
                f"centralized policy {expected_severity.value}. "
                f"This prevents accidental severity drift."
            )


# ============================================================================
# Fingerprint Decoding Utilities
# ============================================================================

def _decode_invariants_fingerprint(fingerprint: str) -> Tuple[Dict[str, Any], Optional[ValidationViolation]]:
    """
    Decode invariants fingerprint into structured data.
    
    Fingerprints are deterministic JSON-encoded structures containing:
    - lineage_artifacts: Dict[artifact_id, artifact_metadata]
    - parent_relationships: Dict[child_id, parent_id]
    - hash_algorithms: List of supported hash algorithms
    - identity_fields: List of immutable identity fields
    
    Args:
        fingerprint: Invariants fingerprint string
        
    Returns:
        Tuple of (decoded invariants data dict, violation if fingerprint is invalid)
        
    Note:
        This is a pure function that decodes deterministic fingerprints.
        It does NOT query the lineage directly.
        
        Invalid fingerprints are governance integrity failures and must be
        reported as CRITICAL violations, not silently ignored.
    """
    if not fingerprint:
        violation = ValidationViolation(
            rule_id="INV_INVALID_INVARIANTS_FINGERPRINT",
            message="Invariants fingerprint is empty or missing - governance integrity failure",
            severity=SeverityLevel.CRITICAL,
            field_path="invariants_fingerprint",
        )
        hash_value = _compute_violation_hash(
            "INV_INVALID_INVARIANTS_FINGERPRINT",
            violation.message,
            "invariants_fingerprint",
            SeverityLevel.CRITICAL,
        )
        object.__setattr__(violation, "deterministic_hash", hash_value)
        return {
            "lineage_artifacts": {},
            "parent_relationships": {},
            "hash_algorithms": ["sha256"],
            "identity_fields": ["id", "type", "version"],
        }, violation
    
    try:
        data = json.loads(fingerprint)
        
        # Tier-0: Validate fingerprint schema structure (hard fail)
        schema_error = _validate_fingerprint_schema(data)
        if schema_error:
            message = (
                f"Invariants fingerprint schema validation failed - governance integrity failure: {schema_error}"
            )
            violation = ValidationViolation(
                rule_id="INV_INVALID_INVARIANTS_FINGERPRINT",
                message=message,
                severity=SeverityLevel.CRITICAL,
                field_path="invariants_fingerprint",
            )
            hash_value = _compute_violation_hash(
                "INV_INVALID_INVARIANTS_FINGERPRINT",
                message,
                "invariants_fingerprint",
                SeverityLevel.CRITICAL,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return {
                "lineage_artifacts": {},
                "parent_relationships": {},
                "hash_algorithms": ["sha256"],
                "identity_fields": ["id", "type", "version"],
            }, violation
        
        # Convert to deterministic structures
        return {
            "lineage_artifacts": data.get("lineage_artifacts", {}),
            "parent_relationships": data.get("parent_relationships", {}),
            "hash_algorithms": data.get("hash_algorithms", ["sha256"]),
            "identity_fields": data.get("identity_fields", ["id", "type", "version"]),
        }, None
    except (json.JSONDecodeError, TypeError) as e:
        # Invalid fingerprint format = governance integrity failure
        message = (
            f"Invariants fingerprint is malformed or corrupted - governance integrity failure: {str(e)}"
        )
        violation = ValidationViolation(
            rule_id="INV_INVALID_INVARIANTS_FINGERPRINT",
            message=message,
            severity=SeverityLevel.CRITICAL,
            field_path="invariants_fingerprint",
        )
        hash_value = _compute_violation_hash(
            "INV_INVALID_INVARIANTS_FINGERPRINT",
            message,
            "invariants_fingerprint",
            SeverityLevel.CRITICAL,
        )
        object.__setattr__(violation, "deterministic_hash", hash_value)
        return {
            "lineage_artifacts": {},
            "parent_relationships": {},
            "hash_algorithms": ["sha256"],
            "identity_fields": ["id", "type", "version"],
        }, violation


# Tier-0 constitutional: Fingerprint schema versioning
EXPECTED_INVARIANTS_FINGERPRINT_SCHEMA_VERSION = 1


def _validate_fingerprint_schema(data: Dict[str, Any]) -> Optional[str]:
    """
    Tier-0 constitutional: Strict fingerprint schema validation (hard fail).
    
    Ensures fingerprint matches expected schema exactly to prevent structural governance drift.
    Validates schema version to prevent silent reinterpretation.
    
    Args:
        data: Decoded fingerprint data
        
    Returns:
        Error message if schema invalid, None if valid
    """
    # Tier-0: Validate schema version
    schema_version = data.get("fingerprint_schema_version")
    if schema_version != EXPECTED_INVARIANTS_FINGERPRINT_SCHEMA_VERSION:
        return (
            f"Invariants fingerprint schema version mismatch. "
            f"Expected {EXPECTED_INVARIANTS_FINGERPRINT_SCHEMA_VERSION}, got {schema_version}. "
            f"This prevents silent reinterpretation of governance state."
        )
    
    # Tier-0: Strict required fields validation
    required_fields = {
        "fingerprint_schema_version",
        "lineage_artifacts",
        "parent_relationships",
    }
    actual_fields = set(data.keys())
    
    # Check all required fields present
    missing_fields = required_fields - actual_fields
    if missing_fields:
        return (
            f"Invariants fingerprint missing required fields: {sorted(missing_fields)}. "
            f"Expected at least: {sorted(required_fields)}. "
            f"Got: {sorted(actual_fields)}"
        )
    
    # Validate field types strictly
    if not isinstance(data["lineage_artifacts"], dict):
        return f"Invariants fingerprint lineage_artifacts must be dict, got {type(data['lineage_artifacts'])}"
    if not isinstance(data["parent_relationships"], dict):
        return f"Invariants fingerprint parent_relationships must be dict, got {type(data['parent_relationships'])}"
    
    return None


# ============================================================================
# Invariant Rule Base
# ============================================================================

class InvariantRule(ValidationRule):
    """
    Base class for invariant validation rules.
    
    All invariant rules operate on system-wide truth constraints
    using deterministic fingerprints from ValidationContext.
    
    Architecture:
    - Fingerprint decoding occurs ONCE in evaluate() method (centralized)
    - Decoded data is passed to _evaluate_invariant() for rule-specific logic
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
        Initialize invariant rule.
        
        Tier-0 enforcement: Validates severity against centralized policy.
        """
        # Tier-0: Assert severity matches centralized policy
        InvariantSeverityPolicy.assert_severity(rule_id, severity)
        
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
        Evaluate invariant rule.
        
        Tier-0 enforcement:
        - Asserts fingerprint purity (no fallback to local lineage)
        - Decodes fingerprint once (centralized, deterministic)
        - Anchors violations to fingerprint (replay legitimacy)
        
        Note: field_path parameter is inherited from ValidationRule interface
        but is not used by invariant rules (they operate on context-level state).
        """
        # Tier-0 defensive guard: Enforce fingerprint purity
        _assert_fingerprint_purity(context)
        
        # Check for fingerprint corruption first (governance integrity)
        invariants_data, invariants_violation = _decode_invariants_fingerprint(context.invariants_fingerprint)
        if invariants_violation:
            # Anchor violation to fingerprint for replay legitimacy
            return _anchor_violation_to_fingerprint(
                invariants_violation,
                context.invariants_fingerprint,
            )
        
        # Delegate to rule-specific evaluation with pre-validated fingerprint data
        violation = self._evaluate_invariant(input_data, context, invariants_data)
        
        # Anchor any violation to fingerprint for replay legitimacy
        if violation:
            return _anchor_violation_to_fingerprint(
                violation,
                context.invariants_fingerprint,
            )
        
        return None
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """
        Rule-specific invariant evaluation.
        
        Subclasses implement this method to perform actual invariant checks.
        Fingerprint is pre-decoded and validated.
        
        Args:
            input_data: Data to validate
            context: Validation context
            invariants_data: Decoded invariants fingerprint data
            
        Returns:
            ValidationViolation if rule fails, None if passes
        """
        raise NotImplementedError


# ============================================================================
# Hash Integrity Rule
# ============================================================================

class HashIntegrityRule(InvariantRule):
    """
    Validates that declared hash matches canonical JSON representation.
    
    Rule ID: INV_HASH_MISMATCH
    Severity: CRITICAL
    
    This is a fundamental integrity check. If hash doesn't match,
    content has been corrupted or tampered with.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="INV_HASH_MISMATCH",
            description="Declared hash must match canonical JSON representation",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if declared hash matches computed hash."""
        # Get declared hash from extended context
        declared_hash = getattr(context, "declared_hash", None)
        
        if not declared_hash:
            # Hash not required for all artifacts - skip if not declared
            return None
        
        # Tier-0: Compute canonical hash deterministically
        computed_hash = _compute_canonical_hash(input_data)
        
        if declared_hash != computed_hash:
            message = (
                f"Hash mismatch: declared '{declared_hash}' does not match "
                f"computed '{computed_hash}'. Content may be corrupted or tampered."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "hash",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="hash",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# ID Immutability Rule
# ============================================================================

class IDImmutabilityRule(InvariantRule):
    """
    Validates that artifact IDs are immutable.
    
    Rule ID: INV_ID_MUTATION
    Severity: CRITICAL
    
    Artifact IDs cannot change once created. This is a constitutional requirement.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="INV_ID_MUTATION",
            description="Artifact ID must be immutable",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if artifact ID is being mutated."""
        artifact_id = input_data.get("id") if isinstance(input_data, dict) else None
        
        if not artifact_id:
            # ID not present - might be handled by field rules
            return None
        
        # Get existing artifact ID from extended context
        existing_id = getattr(context, "existing_artifact_id", None)
        
        if not existing_id:
            # No existing artifact - ID mutation check doesn't apply
            return None
        
        # Tier-0 constitutional: ID immutability is absolute
        if artifact_id != existing_id:
            message = (
                f"Artifact ID cannot be mutated: declared '{artifact_id}' differs from "
                f"existing '{existing_id}'. ID immutability is a constitutional requirement."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "id",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="id",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Lineage Parent Existence Rule
# ============================================================================

class LineageParentExistenceRule(InvariantRule):
    """
    Validates that parent artifacts exist in lineage.
    
    Rule ID: INV_PARENT_NOT_FOUND
    Severity: ERROR
    
    If an artifact declares a parent, that parent must exist in the lineage.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="INV_PARENT_NOT_FOUND",
            description="Parent artifact must exist in lineage",
            severity=SeverityLevel.ERROR,
        )
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if parent artifact exists in lineage."""
        if not isinstance(input_data, dict):
            return None
        
        parent_id = input_data.get("parent_artifact_id")
        
        if not parent_id:
            # Not all artifacts have parents - skip if not declared
            return None
        
        # Tier-0: Check parent existence via fingerprint-derived lineage state
        lineage_artifacts = invariants_data.get("lineage_artifacts", {})
        
        if parent_id not in lineage_artifacts:
            message = (
                f"Parent artifact '{parent_id}' does not exist in lineage. "
                f"Derived artifacts must reference valid parent artifacts."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "parent_artifact_id",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="parent_artifact_id",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Fingerprint Matching Rule
# ============================================================================

class FingerprintMatchingRule(InvariantRule):
    """
    Validates that declared fingerprint matches computed fingerprint.
    
    Rule ID: INV_FINGERPRINT_MISMATCH
    Severity: CRITICAL
    
    Fingerprints must match to ensure governance state integrity.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="INV_FINGERPRINT_MISMATCH",
            description="Declared fingerprint must match computed fingerprint",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if declared fingerprint matches computed fingerprint."""
        # Get declared fingerprint from extended context
        declared_fingerprint = getattr(context, "declared_fingerprint", None)
        
        if not declared_fingerprint:
            # Fingerprint not required for all artifacts - skip if not declared
            return None
        
        # Tier-0: Compute fingerprint from invariants data
        # This should match the declared fingerprint
        computed_fingerprint = _compute_canonical_hash(invariants_data)
        
        if declared_fingerprint != computed_fingerprint:
            message = (
                f"Fingerprint mismatch: declared '{declared_fingerprint[:16]}...' does not match "
                f"computed '{computed_fingerprint[:16]}...'. Governance state may be corrupted."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "fingerprint",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="fingerprint",
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Identity Stability Rule
# ============================================================================

class IdentityStabilityRule(InvariantRule):
    """
    Validates that identity fields (id, type, version) are immutable.
    
    Rule ID: INV_IDENTITY_MUTATION
    Severity: CRITICAL
    
    Identity fields cannot change once an artifact is created.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="INV_IDENTITY_MUTATION",
            description="Identity fields (id, type, version) must be immutable",
            severity=SeverityLevel.CRITICAL,
        )
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if identity fields are being mutated."""
        if not isinstance(input_data, dict):
            return None
        
        # Get identity fields from fingerprint (defaults if not specified)
        identity_fields = invariants_data.get("identity_fields", ["id", "type", "version"])
        
        # Get existing identity from extended context
        existing_identity = getattr(context, "existing_identity", None)
        
        if not existing_identity:
            # No existing artifact - identity mutation check doesn't apply
            return None
        
        if not isinstance(existing_identity, dict):
            return None
        
        # Tier-0 constitutional: Identity fields are immutable
        for field_name in identity_fields:
            declared_value = input_data.get(field_name)
            existing_value = existing_identity.get(field_name)
            
            if declared_value is not None and existing_value is not None:
                if declared_value != existing_value:
                    message = (
                        f"Identity field '{field_name}' cannot be mutated: declared '{declared_value}' "
                        f"differs from existing '{existing_value}'. Identity immutability is a constitutional requirement."
                    )
                    hash_value = _compute_violation_hash(
                        self.rule_id,
                        message,
                        field_name,
                        self.severity,
                    )
                    violation = ValidationViolation(
                        rule_id=self.rule_id,
                        message=message,
                        severity=self.severity,
                        field_path=field_name,
                    )
                    object.__setattr__(violation, "deterministic_hash", hash_value)
                    return violation
        
        return None


# ============================================================================
# Derivation Legitimacy Rule
# ============================================================================

class DerivationLegitimacyRule(InvariantRule):
    """
    Validates that derivation relationships are legitimate.
    
    Rule ID: INV_DERIVATION_ILLEGITIMATE
    Severity: ERROR
    
    Derived artifacts must reference valid parent artifacts with correct relationships.
    """
    
    def __init__(self):
        super().__init__(
            rule_id="INV_DERIVATION_ILLEGITIMATE",
            description="Derivation relationships must be legitimate",
            severity=SeverityLevel.ERROR,
        )
    
    def _evaluate_invariant(
        self,
        input_data: Any,
        context: ValidationContext,
        invariants_data: Dict[str, Any],
    ) -> Optional[ValidationViolation]:
        """Check if derivation relationship is legitimate."""
        if not isinstance(input_data, dict):
            return None
        
        artifact_id = input_data.get("id")
        parent_id = input_data.get("parent_artifact_id")
        
        if not artifact_id or not parent_id:
            # Not a derivation relationship - skip
            return None
        
        # Tier-0: Check parent relationship via fingerprint-derived lineage state
        parent_relationships = invariants_data.get("parent_relationships", {})
        
        # Check if parent relationship is declared in fingerprint
        declared_parent = parent_relationships.get(artifact_id)
        
        if declared_parent is not None and declared_parent != parent_id:
            message = (
                f"Derivation relationship mismatch: artifact '{artifact_id}' declares parent '{parent_id}', "
                f"but fingerprint declares parent '{declared_parent}'. Derivation relationships must be consistent."
            )
            hash_value = _compute_violation_hash(
                self.rule_id,
                message,
                "parent_artifact_id",
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path="parent_artifact_id",
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
    invariants_fingerprint_hash: Optional[str] = None,
) -> str:
    """
    Compute deterministic hash for validation violation.
    
    Tier-0 replay guarantee: Includes fingerprint hash for replay legitimacy.
    This ensures violations are anchored to the governance state that produced them.
    
    Uses same algorithm as validation_contract for consistency, with fingerprint anchoring.
    
    Args:
        rule_id: Rule identifier
        message: Violation message
        field_path: Field path (if applicable)
        severity: Severity level
        invariants_fingerprint_hash: Optional hash of invariants fingerprint (for anchoring)
    """
    parts = [
        rule_id,
        message,
        field_path or "",
        severity.value,
    ]
    
    # Tier-0: Anchor to fingerprint for replay legitimacy
    if invariants_fingerprint_hash:
        parts.append(f"invariants_fp:{invariants_fingerprint_hash}")
    
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

INVARIANT_RULES: Tuple[InvariantRule, ...] = (
    # Order matters for deterministic execution
    # 1. Check hash integrity first (fundamental)
    HashIntegrityRule(),
    # 2. Check fingerprint matching
    FingerprintMatchingRule(),
    # 3. Check ID immutability
    IDImmutabilityRule(),
    # 4. Check identity stability
    IdentityStabilityRule(),
    # 5. Check lineage parent existence
    LineageParentExistenceRule(),
    # 6. Check derivation legitimacy
    DerivationLegitimacyRule(),
)

# Tier-0 constitutional: Rule ordering integrity hash
# This provides replay-verifiable rule ordering integrity
# Ensures rule execution order is locked and cannot drift
_INVARIANT_RULES_ORDERING_HASH = hashlib.sha256(
    "|".join(rule.rule_id for rule in INVARIANT_RULES).encode("utf-8")
).hexdigest()

# Expose for verification and audit
INVARIANT_RULES_ORDERING_HASH: str = _INVARIANT_RULES_ORDERING_HASH

__all__ = [
    "InvariantRule",
    "InvariantSeverityPolicy",
    "HashIntegrityRule",
    "IDImmutabilityRule",
    "LineageParentExistenceRule",
    "FingerprintMatchingRule",
    "IdentityStabilityRule",
    "DerivationLegitimacyRule",
    "INVARIANT_RULES",
    "_decode_invariants_fingerprint",
    "_assert_fingerprint_purity",
    "_compute_canonical_hash",
    "_anchor_violation_to_fingerprint",
    "_validate_fingerprint_schema",
    "INVARIANT_RULES_ORDERING_HASH",
    "EXPECTED_INVARIANTS_FINGERPRINT_SCHEMA_VERSION",
]
