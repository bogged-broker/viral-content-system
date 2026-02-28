"""
/data/validation/validation_contract.py

Canonical Validation Framework Specification
(Deterministic Pre-State Legitimacy Enforcement)

---

1️⃣ Purpose

Defines:

- What validation means
- How validation results are structured
- Severity levels
- Deterministic evaluation rules
- Rule composition model
- Failure semantics

Every other validator file must depend on this contract.

Nothing may bypass this layer.

---

2️⃣ Core Principle

Validation must be:

- Deterministic
- Side-effect free
- Complete (all failures reported unless fail-fast mode)
- Version-aware
- Context-aware
- Replay-safe
- Serializable

Given identical input, validation must produce identical output.

---

3️⃣ Validation Function Formal Definition

Let:

    Validate : (Input, ValidationContext) → ValidationResult

Properties:

Determinism:
    Validate(x, c) = r
    Validate(x, c) = r'
    ⇒ r = r'

Side-effect free:
    No mutation of system state

Completeness: All applicable rules must be evaluated unless configured fail-fast.

---

4️⃣ Validation Context

Validation must include context for version & governance awareness.

---

5️⃣ Validation Rule Structure

Rules must not:
- Raise exceptions for business logic failure
- Mutate input
- Access global state

---

6️⃣ Severity Levels

System behavior:
- INFO → record only
- WARNING → allowed unless strict_mode
- ERROR → validation failure
- CRITICAL → immediate rejection

---

7️⃣ Validation Result Structure

Fingerprint must be deterministic hash of sorted violations.

---

8️⃣ Rule Composition Model

Validation contract must support:
- Field-level rules
- Cross-field rules
- Context-sensitive rules
- Version-aware rules
- Compatibility-aware rules
- Invariant-related preconditions

---

9️⃣ Deterministic Ordering of Violations

Violations must be:
- Sorted by rule_id
- Sorted by field_path
- Stable across executions

Guarantees replay consistency.

---

🔟 Fail-Fast vs Aggregated Mode

Two modes:
1. Fail-fast → stop on first ERROR/CRITICAL
2. Full-report → collect all violations

Mode must be explicit in context.

---

11️⃣ Validation Scope Types

Rules must declare scope:
- artifact
- mutation_payload
- migration_plan
- version_registry
- compatibility_matrix
- snapshot_request
- governance_operation

Allows isolated rule groups per domain.

---

12️⃣ Validation Invariants

Validation must itself satisfy:
- No nondeterministic randomness
- No wall-clock dependence
- No IO dependence
- No global mutation
- Stable rule ordering
- Stable error formatting

Validation must be reproducible during replay.

---

13️⃣ Integration Points

Must run before:
- lineage_record creation
- mutation proposal submission
- governance lock acquisition (if structural)
- schema registration
- compatibility update
- snapshot sealing
- rollback initiation

Must emit audit hook event:
- ValidationFailed (blocking)
- ValidationWarning (non-blocking)

---

14️⃣ Rejection Semantics

If validation fails:
- Mutation must not reach lineage layer
- Mutation must not reach consensus layer
- Governance lock must not be modified
- No append must occur

Failure is pre-transition rejection.

---

15️⃣ Validation Fingerprint

ValidationResult must produce:

    validation_fingerprint = H(
        passed + sorted(rule_id + message + field_path + severity)
    )

Note: Includes passed status and severity for complete governance-level identity.

Allows:
- Audit traceability
- Cross-node equivalence
- Deterministic rejection proof

---

16️⃣ Formal Relationship to Lineage State Machine

In formal model:

Original transition:
    T(S, μ)

Becomes:
    T(S, μ) defined iff Validate(μ, context).passed

Validation constrains domain of transition function.

---

17️⃣ Security Role

Prevents:
- Malformed schema injection
- Invalid version declaration
- Incompatible coexistence attempt
- Structural mutation without valid plan
- Downgrade disguised as upgrade
- Corrupted artifact payload
- Invariant bypass attempt
- Governance misuse

Validation is attack surface minimizer.

---

18️⃣ Testing Requirements

- Deterministic repeated validation
- Strict vs permissive mode difference
- Cross-field violation detection
- Version-conditional rule enforcement
- Rule ordering stability
- Fingerprint reproducibility
- High-volume rule scaling test
- Nested rule composition test

---

19️⃣ Absolute Definition

/data/validation/validation_contract.py is:

> The deterministic, side-effect-free pre-state enforcement framework that governs
> all structural and semantic legitimacy checks before any data, mutation, or
> governance action is allowed to interact with the lineage system.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

__all__ = [
    "SeverityLevel",
    "ValidationScope",
    "ValidationContext",
    "ValidationRule",
    "ValidationViolation",
    "ValidationResult",
    "ValidationEvaluator",
    "ValidationError",
    "ValidationContract",
    "RuleRegistry",
    "ValidationAuditHook",
    "EMPTY_VALIDATION_FINGERPRINT",
    "assert_deterministic",
]


# ============================================================================
# Severity Levels
# ============================================================================

class SeverityLevel(str, Enum):
    """
    Validation severity levels with defined system behavior.
    
    System behavior:
    - INFO → record only
    - WARNING → allowed unless strict_mode
    - ERROR → validation failure
    - CRITICAL → immediate rejection
    """
    
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    
    def is_blocking(self, strict_mode: bool = False) -> bool:
        """
        Determine if this severity level blocks validation.
        
        Args:
            strict_mode: If True, WARNING also blocks
            
        Returns:
            True if this severity should cause validation failure
        """
        if self == SeverityLevel.CRITICAL:
            return True
        if self == SeverityLevel.ERROR:
            return True
        if self == SeverityLevel.WARNING and strict_mode:
            return True
        return False


# ============================================================================
# Validation Scope Types
# ============================================================================

class ValidationScope(str, Enum):
    """
    Validation scope types for rule isolation.
    
    Rules must declare scope to allow isolated rule groups per domain.
    """
    
    ARTIFACT = "artifact"
    MUTATION_PAYLOAD = "mutation_payload"
    MIGRATION_PLAN = "migration_plan"
    VERSION_REGISTRY = "version_registry"
    COMPATIBILITY_MATRIX = "compatibility_matrix"
    SNAPSHOT_REQUEST = "snapshot_request"
    GOVERNANCE_OPERATION = "governance_operation"


# ============================================================================
# Validation Context
# ============================================================================

@dataclass(frozen=True)
class ValidationContext:
    """
    Validation context ensuring version & governance awareness.
    
    All fields are required for deterministic validation.
    """
    
    artifact_type: Optional[str] = None
    artifact_version: Optional[str] = None
    registry_fingerprint: str = ""
    compatibility_fingerprint: str = ""
    invariants_fingerprint: str = ""
    strict_mode: bool = False
    source: str = ""
    fail_fast: bool = False  # If True, stop on first ERROR/CRITICAL
    
    def __post_init__(self) -> None:
        """Validate required fields."""
        if not self.registry_fingerprint:
            raise ValueError("registry_fingerprint is required")
        if not self.compatibility_fingerprint:
            raise ValueError("compatibility_fingerprint is required")
        if not self.invariants_fingerprint:
            raise ValueError("invariants_fingerprint is required")
        if not self.source:
            raise ValueError("source is required")
        # Enforce explicit fail_fast mode for governance clarity
        if self.fail_fast not in (True, False):
            raise ValueError("fail_fast must be explicitly set (True or False)")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "artifact_type": self.artifact_type,
            "artifact_version": self.artifact_version,
            "registry_fingerprint": self.registry_fingerprint,
            "compatibility_fingerprint": self.compatibility_fingerprint,
            "invariants_fingerprint": self.invariants_fingerprint,
            "strict_mode": self.strict_mode,
            "source": self.source,
            "fail_fast": self.fail_fast,
        }
    
    def canonical_json(self) -> str:
        """Produce canonical JSON representation."""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True
        )


# ============================================================================
# Scope Definition
# ============================================================================

@dataclass(frozen=True)
class ScopeDefinition:
    """
    Defines what a validation rule applies to.
    
    Supports field-level, cross-field, and context-sensitive rules.
    
    All collections are canonicalized to sorted tuples for deterministic serialization.
    """
    
    scope: ValidationScope
    artifact_types: Optional[Tuple[str, ...]] = None  # None means all types, sorted for determinism
    field_paths: Optional[Tuple[str, ...]] = None  # None means all fields, sorted for determinism
    version_range: Optional[Tuple[Optional[int], Optional[int]]] = None  # (min, max), None means unbounded
    
    def __post_init__(self) -> None:
        """Canonicalize collections to sorted tuples for deterministic behavior."""
        # Convert artifact_types to sorted tuple if provided
        if self.artifact_types is not None:
            if isinstance(self.artifact_types, (set, list)):
                object.__setattr__(
                    self,
                    "artifact_types",
                    tuple(sorted(self.artifact_types))
                )
        
        # Convert field_paths to sorted tuple if provided
        if self.field_paths is not None:
            if isinstance(self.field_paths, (set, list)):
                object.__setattr__(
                    self,
                    "field_paths",
                    tuple(sorted(self.field_paths))
                )
    
    def applies_to(self, context: ValidationContext, field_path: Optional[str] = None) -> bool:
        """
        Check if this scope definition applies to the given context.
        
        Args:
            context: Validation context
            field_path: Optional field path being validated
            
        Returns:
            True if this scope applies
        """
        # Check artifact type
        if self.artifact_types is not None:
            if context.artifact_type not in self.artifact_types:
                return False
        
        # Check field path
        if self.field_paths is not None and field_path is not None:
            if field_path not in self.field_paths:
                return False
        
        # Check version range
        if self.version_range is not None and context.artifact_version:
            # Parse version deterministically (supports integer and semantic versioning)
            parsed_version = _parse_version_for_comparison(context.artifact_version)
            if parsed_version is None:
                # Invalid version format must be deterministic decision
                # Rule does NOT apply if version is unreadable (governance-safe)
                return False
            
            min_version, max_version = self.version_range
            if min_version is not None and parsed_version < min_version:
                return False
            if max_version is not None and parsed_version > max_version:
                return False
        
        return True


# ============================================================================
# Validation Violation
# ============================================================================

@dataclass(frozen=True)
class ValidationViolation:
    """
    Individual validation violation with deterministic hash.
    
    Immutable and hashable for deterministic ordering.
    """
    
    rule_id: str
    message: str
    severity: SeverityLevel
    field_path: Optional[str] = None
    deterministic_hash: str = field(default="", init=False)
    
    def __post_init__(self) -> None:
        """Compute deterministic hash if not provided."""
        if not self.deterministic_hash:
            # Compute hash from rule_id + message + field_path + severity
            # Severity must be included for governance-level violation identity stability
            parts = [
                self.rule_id,
                self.message,
                self.field_path or "",
                self.severity.value,
            ]
            canonical = json.dumps(
                parts,
                sort_keys=False,  # Order matters here
                separators=(",", ":"),
                ensure_ascii=True
            )
            hash_value = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            object.__setattr__(self, "deterministic_hash", hash_value)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "rule_id": self.rule_id,
            "message": self.message,
            "severity": self.severity.value,
            "field_path": self.field_path,
            "deterministic_hash": self.deterministic_hash,
        }
    
    def __lt__(self, other: ValidationViolation) -> bool:
        """Comparison for deterministic sorting."""
        if not isinstance(other, ValidationViolation):
            return NotImplemented
        
        # Sort by rule_id, then field_path, then message
        if self.rule_id != other.rule_id:
            return self.rule_id < other.rule_id
        if (self.field_path or "") != (other.field_path or ""):
            return (self.field_path or "") < (other.field_path or "")
        return self.message < other.message
    
    def __hash__(self) -> int:
        """Hash based on deterministic fields."""
        return hash((self.rule_id, self.message, self.field_path, self.severity))
    
    def __eq__(self, other: object) -> bool:
        """Equality based on deterministic fields."""
        if not isinstance(other, ValidationViolation):
            return NotImplemented
        return (
            self.rule_id == other.rule_id
            and self.message == other.message
            and self.field_path == other.field_path
            and self.severity == other.severity
        )


# ============================================================================
# Validation Result
# ============================================================================

# Canonical empty fingerprint constant for deterministic empty validation results
EMPTY_VALIDATION_FINGERPRINT = hashlib.sha256(b"[]").hexdigest()

@dataclass(frozen=True)
class ValidationResult:
    """
    Complete validation result with deterministic fingerprint.
    
    Fingerprint allows audit traceability and cross-node equivalence.
    """
    
    passed: bool
    violations: Tuple[ValidationViolation, ...]  # Immutable tuple for determinism
    validation_fingerprint: str = field(default="", init=False)
    
    def __post_init__(self) -> None:
        """Compute validation fingerprint from sorted violations."""
        if not self.validation_fingerprint:
            # Handle empty violations case with canonical constant
            if not self.violations:
                # Include passed status in empty case for governance equivalence
                parts = [f"passed:{self.passed}"]
                canonical = json.dumps(
                    parts,
                    sort_keys=False,
                    separators=(",", ":"),
                    ensure_ascii=True
                )
                fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                object.__setattr__(self, "validation_fingerprint", fingerprint)
                return
            
            # Sort violations deterministically
            sorted_violations = sorted(self.violations)
            
            # Create canonical representation (include severity for governance-level identity)
            violation_parts = []
            for v in sorted_violations:
                violation_parts.append(
                    f"{v.rule_id}:{v.message}:{v.field_path or ''}:{v.severity.value}"
                )
            
            # Include passed status in fingerprint for cross-node equivalence
            # This ensures identical validation outcomes produce identical fingerprints
            parts = [
                f"passed:{self.passed}",
                *violation_parts,
            ]
            
            # Compute hash
            canonical = json.dumps(
                parts,
                sort_keys=False,  # Already sorted
                separators=(",", ":"),
                ensure_ascii=True
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            object.__setattr__(self, "validation_fingerprint", fingerprint)
    
    @classmethod
    def success(cls) -> ValidationResult:
        """Create a successful validation result with canonical empty fingerprint."""
        # __post_init__ will automatically set EMPTY_VALIDATION_FINGERPRINT for empty violations
        return cls(passed=True, violations=())
    
    @classmethod
    def failure(cls, violations: List[ValidationViolation]) -> ValidationResult:
        """Create a failed validation result."""
        return cls(passed=False, violations=tuple(violations))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "passed": self.passed,
            "violations": [v.to_dict() for v in sorted(self.violations)],
            "validation_fingerprint": self.validation_fingerprint,
        }
    
    def has_blocking_violations(self, strict_mode: bool = False) -> bool:
        """Check if result contains blocking violations."""
        return any(
            v.severity.is_blocking(strict_mode)
            for v in self.violations
        )
    
    def get_blocking_violations(self, strict_mode: bool = False) -> List[ValidationViolation]:
        """Get all blocking violations."""
        return [
            v for v in self.violations
            if v.severity.is_blocking(strict_mode)
        ]


# ============================================================================
# Rule Registry Protocol
# ============================================================================

class RuleRegistry(Protocol):
    """
    Protocol for deterministic rule registry integration.
    
    Ensures governance equivalence across nodes by providing
    a stable, fingerprintable rule set for each scope.
    """
    
    def get_rules(self, scope: ValidationScope) -> Tuple[ValidationRule, ...]:
        """
        Get all rules for a given scope in deterministic order.
        
        Args:
            scope: Validation scope to get rules for
            
        Returns:
            Tuple of rules sorted by rule_id for determinism
        """
        ...
    
    def get_registry_fingerprint(self) -> str:
        """
        Get deterministic fingerprint of the entire rule registry.
        
        Returns:
            SHA-256 hex digest of canonical rule registry representation
        """
        ...


# ============================================================================
# Validation Rule Protocol
# ============================================================================

class ValidationRule(ABC):
    """
    Base class for validation rules.
    
    Rules must not:
    - Raise exceptions for business logic failure
    - Mutate input
    - Access global state
    
    Rules must be deterministic and side-effect free.
    
    Attributes are frozen via __slots__ to prevent mutation and ensure determinism.
    """
    
    __slots__ = ("rule_id", "description", "severity", "applies_to")
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
    ):
        """
        Initialize validation rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
        """
        object.__setattr__(self, "rule_id", rule_id)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "applies_to", applies_to)
    
    def __setattr__(self, name: str, value: Any) -> None:
        """Prevent attribute mutation after initialization for determinism."""
        if hasattr(self, name):
            raise AttributeError(
                f"Cannot modify {name} after initialization. "
                f"ValidationRule attributes are immutable for determinism."
            )
        object.__setattr__(self, name, value)
    
    @abstractmethod
    def evaluate(
        self,
        input_data: Any,
        context: ValidationContext,
        field_path: Optional[str] = None,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate rule against input.
        
        Args:
            input_data: Data to validate
            context: Validation context
            field_path: Optional field path being validated
            
        Returns:
            ValidationViolation if rule fails, None if passes
            
        Raises:
            ValidationError: Only for programming errors, not business logic failures
        """
        pass
    
    def should_apply(self, context: ValidationContext, field_path: Optional[str] = None) -> bool:
        """Check if this rule should be applied to the given context."""
        return self.applies_to.applies_to(context, field_path)
    
    def __repr__(self) -> str:
        return f"ValidationRule(rule_id={self.rule_id!r}, severity={self.severity.value})"


# ============================================================================
# Audit Hook Protocol
# ============================================================================

class ValidationAuditHook(Protocol):
    """
    Protocol for validation audit event integration.
    
    Allows emitting ValidationFailed and ValidationWarning events
    as required by the specification.
    """
    
    def on_failure(self, result: ValidationResult, context: ValidationContext) -> None:
        """
        Called when validation fails (blocking violations detected).
        
        Args:
            result: Validation result with violations
            context: Validation context
        """
        ...
    
    def on_warning(self, result: ValidationResult, context: ValidationContext) -> None:
        """
        Called when validation produces warnings (non-blocking violations).
        
        Args:
            result: Validation result with violations
            context: Validation context
        """
        ...


# ============================================================================
# Validation Evaluator
# ============================================================================

class ValidationEvaluator:
    """
    Orchestrates validation rule evaluation.
    
    Guarantees:
    - Deterministic rule ordering
    - Complete evaluation (unless fail-fast)
    - Stable violation ordering
    - Side-effect free execution
    - Mandatory registry authority enforcement
    - Mandatory audit hook for governance observability
    """
    
    def __init__(
        self,
        rules: List[ValidationRule],
        registry: RuleRegistry,
        audit_hook: ValidationAuditHook,
    ):
        """
        Initialize evaluator with rules.
        
        Args:
            rules: List of validation rules (will be sorted by rule_id for determinism)
            registry: Mandatory rule registry for governance equivalence checking
            audit_hook: Mandatory audit hook for emitting validation events
            
        Raises:
            ValidationError: If registry or audit_hook is None (Tier-0 governance requirement)
        """
        if registry is None:
            raise ValidationError(
                "RuleRegistry is mandatory for Tier-0 governance equivalence. "
                "All validation must use a registered rule set with fingerprint verification."
            )
        if audit_hook is None:
            raise ValidationError(
                "ValidationAuditHook is mandatory for Tier-0 governance observability. "
                "All validation events must be audited for cross-node equivalence."
            )
        
        # Sort rules deterministically by rule_id to ensure stable ordering across nodes
        self.rules = tuple(sorted(rules, key=lambda r: r.rule_id))
        self.registry = registry
        self.audit_hook = audit_hook
    
    def validate(
        self,
        input_data: Any,
        context: ValidationContext,
    ) -> ValidationResult:
        """
        Validate input against all applicable rules.
        
        Args:
            input_data: Data to validate
            context: Validation context
            
        Returns:
            ValidationResult with all violations
            
        Raises:
            ValidationError: Only for programming errors or governance violations
        """
        # Tier-0 Governance Enforcement: Verify registry fingerprint equivalence
        # This ensures all nodes use identical rule sets for deterministic validation
        registry_fingerprint = self.registry.get_registry_fingerprint()
        if registry_fingerprint != context.registry_fingerprint:
            raise ValidationError(
                f"Registry fingerprint mismatch: evaluator has {registry_fingerprint}, "
                f"context requires {context.registry_fingerprint}. "
                "This violates Tier-0 governance equivalence requirements.",
                context=context,
            )
        
        violations: List[ValidationViolation] = []
        
        # Evaluate rules in deterministic order
        for rule in self.rules:
            # Check if rule applies
            if not rule.should_apply(context):
                continue
            
            # Evaluate rule
            try:
                violation = rule.evaluate(input_data, context)
                if violation is not None:
                    violations.append(violation)
                    
                    # Fail-fast if configured and violation is blocking
                    if context.fail_fast and violation.severity.is_blocking(context.strict_mode):
                        break
            except Exception as e:
                # Programming error, not business logic failure
                raise ValidationError(
                    f"Rule {rule.rule_id} raised exception: {e}",
                    rule_id=rule.rule_id,
                ) from e
        
        # Determine if validation passed
        # Passed if no blocking violations (considering strict_mode)
        passed = not any(
            v.severity.is_blocking(context.strict_mode)
            for v in violations
        )
        
        result = ValidationResult(
            passed=passed,
            violations=tuple(violations),
        )
        
        # Tier-0 Governance Requirement: Mandatory audit event emission
        # All validation outcomes must be audited for cross-node equivalence
        if not passed:
            self.audit_hook.on_failure(result, context)
        elif any(v.severity == SeverityLevel.WARNING for v in violations):
            self.audit_hook.on_warning(result, context)
        
        return result


# ============================================================================
# Exceptions
# ============================================================================

class ValidationError(Exception):
    """
    Exception for validation framework errors.
    
    This is for programming errors, not business logic failures.
    Business logic failures should return ValidationViolation objects.
    """
    
    def __init__(
        self,
        message: str,
        rule_id: Optional[str] = None,
        context: Optional[ValidationContext] = None,
    ):
        super().__init__(message)
        self.message = message
        self.rule_id = rule_id
        self.context = context


# ============================================================================
# Validation Contract Protocol
# ============================================================================

class ValidationContract(Protocol):
    """
    Protocol for validation contract implementations.
    
    All validators must implement this interface.
    """
    
    def validate(
        self,
        input_data: Any,
        context: ValidationContext,
    ) -> ValidationResult:
        """
        Validate input data.
        
        Args:
            input_data: Data to validate
            context: Validation context
            
        Returns:
            ValidationResult
        """
        ...


# ============================================================================
# Version Parsing Helper (Tier-0 Determinism)
# ============================================================================

def _parse_version_for_comparison(version_str: str) -> Optional[int]:
    """
    Parse version string to integer for deterministic range comparison.
    
    Supports:
    - Integer versions: "1", "2", "42"
    - Semantic versions: "1.2.3" -> uses major version (1)
    - Semantic versions: "2.0.0" -> uses major version (2)
    
    This ensures deterministic rule applicability across heterogeneous
    versioning systems while maintaining governance equivalence.
    
    Args:
        version_str: Version string to parse
        
    Returns:
        Integer version for comparison, or None if unparseable
        
    Tier-0 Guarantee:
        Same version string always produces same integer across all nodes.
        Unparseable versions deterministically return None (rule does not apply).
    """
    if not version_str:
        return None
    
    # Try direct integer parsing first (most common case)
    try:
        return int(version_str)
    except (ValueError, TypeError):
        pass
    
    # Try semantic versioning (major.minor.patch)
    # Extract major version for comparison
    try:
        parts = version_str.split('.')
        if parts:
            major_part = parts[0].strip()
            # Remove any non-numeric prefix (e.g., "v1.2.3" -> "1")
            major_part = ''.join(c for c in major_part if c.isdigit())
            if major_part:
                return int(major_part)
    except (ValueError, TypeError, AttributeError):
        pass
    
    # Unparseable version - deterministic None return
    return None


# ============================================================================
# Deterministic Hash Helpers
# ============================================================================

def compute_violation_hash(violation: ValidationViolation) -> str:
    """
    Compute deterministic hash of violation.
    
    Args:
        violation: Validation violation
        
    Returns:
        SHA-256 hex digest
    """
    parts = [
        violation.rule_id,
        violation.message,
        violation.field_path or "",
        violation.severity.value,
    ]
    canonical = json.dumps(
        parts,
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_result_fingerprint(result: ValidationResult) -> str:
    """
    Compute deterministic fingerprint of validation result.
    
    Args:
        result: Validation result
        
    Returns:
        SHA-256 hex digest
    """
    # Sort violations deterministically
    sorted_violations = sorted(result.violations)
    
    # Create canonical representation
    violation_parts = []
    for v in sorted_violations:
        violation_parts.append(
            f"{v.rule_id}:{v.message}:{v.field_path or ''}:{v.severity.value}"
        )
    
    # Add passed status
    parts = [
        f"passed:{result.passed}",
        *violation_parts,
    ]
    
    canonical = json.dumps(
        parts,
        sort_keys=False,  # Already sorted
        separators=(",", ":"),
        ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================================
# Determinism Assertion
# ============================================================================

def assert_deterministic(
    result1: ValidationResult,
    result2: ValidationResult,
) -> None:
    """
    Assert that two validation results are deterministically equivalent.
    
    Used in replay test harnesses to verify Tier-0 determinism contract.
    
    Args:
        result1: First validation result
        result2: Second validation result
        
    Raises:
        ValidationError: If results are not deterministically equivalent
    """
    if result1.passed != result2.passed:
        raise ValidationError(
            f"Non-deterministic validation detected: passed mismatch "
            f"({result1.passed} vs {result2.passed})"
        )
    
    if result1.validation_fingerprint != result2.validation_fingerprint:
        raise ValidationError(
            f"Non-deterministic validation detected: fingerprint mismatch "
            f"({result1.validation_fingerprint} vs {result2.validation_fingerprint})"
        )
    
    if len(result1.violations) != len(result2.violations):
        raise ValidationError(
            f"Non-deterministic validation detected: violation count mismatch "
            f"({len(result1.violations)} vs {len(result2.violations)})"
        )
    
    # Compare violations in sorted order
    sorted_v1 = sorted(result1.violations)
    sorted_v2 = sorted(result2.violations)
    
    for v1, v2 in zip(sorted_v1, sorted_v2):
        if v1 != v2:
            raise ValidationError(
                f"Non-deterministic validation detected: violation mismatch "
                f"({v1} vs {v2})"
            )
