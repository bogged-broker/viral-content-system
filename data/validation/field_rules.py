"""
/data/validation/field_rules.py

Atomic Structural Validation Specification
(Deterministic Field-Level Legitimacy Enforcement)

---

1️⃣ Purpose

This file defines:

- Primitive structural validation rules.
- Type correctness rules.
- Presence requirements.
- Field format constraints.
- Field-level boundary conditions.
- Enum conformance.
- Nullability enforcement.

These rules are:

- Stateless
- Deterministic
- Isolated
- Context-aware but not state-aware

They operate strictly on:

> One field at a time.

No rule here may inspect sibling fields.
No rule here may enforce cross-field semantic correctness.
That belongs in semantic_rules.py.

---

2️⃣ Architectural Role

field_rules.py exists to prevent:

- Malformed payloads
- Type drift
- Missing structural requirements
- Enum violations
- Out-of-bound values
- Invalid encodings
- Structural downgrade attempts

If this layer fails, deeper layers should never execute.

---

3️⃣ Rule Design Constraints

Each rule must:

- Have a stable rule_id
- Declare severity
- Operate deterministically
- Never mutate input
- Never rely on external IO
- Never use runtime timestamps
- Never use randomness
- Never inspect system state

Rules must only evaluate:

input_data[field_name]

plus validation context if required (e.g., version logic).

---

4️⃣ Rule Category Types

Field rules fall into these atomic categories:

1. Required Field Rule
   Field must exist.

2. Type Enforcement Rule
   Field must be of type T.

3. Non-Nullable Rule
   Field must not be None.

4. Enum Conformance Rule
   Field must be within allowed values.

5. Length Constraint Rule
   String length boundaries.

6. Numeric Boundary Rule
   Min / Max enforcement.

7. Regex Format Rule
   String must match deterministic pattern.

8. Immutable Field Rule (Field-Level Scope Only)
   Field must not change if context declares update operation.
   Still atomic. Still single-field.

---

5️⃣ Canonical Rule Interface

All field rules implement:

from .contracts import ValidationViolation, SeverityLevel

class BaseFieldRule:
    rule_id: str
    severity: SeverityLevel
    field_name: str

    def evaluate(self, input_data, context) -> ValidationViolation | None:
        raise NotImplementedError

No rule may:

- Raise business exceptions.
- Write logs.
- Access file system.
- Query registry.

---

6️⃣ Deterministic Error Construction

Violations must use stable text.

Message formatting must never embed:

- Runtime timestamps
- Random values
- Float representations without normalization
- Dict iteration order

Field path must always be identical:

"{field_name}"

Never dynamic dotted resolution here.

---

7️⃣ Example: Required Field Rule

class RequiredFieldRule(BaseFieldRule):
    def evaluate(self, input_data, context):
        if self.field_name not in input_data:
            return ValidationViolation(
                rule_id=self.rule_id,
                message=f"Missing required field: {self.field_name}",
                severity=self.severity,
                field_path=self.field_name,
                deterministic_hash=hash_violation(
                    self.rule_id,
                    f"Missing required field: {self.field_name}",
                    self.field_name,
                ),
            )
        return None

No side-effects. No branching outside field existence.

---

8️⃣ Example: Type Enforcement Rule

class TypeRule(BaseFieldRule):
    expected_type: type

    def evaluate(self, input_data, context):
        if self.field_name not in input_data:
            return None

        if not isinstance(input_data[self.field_name], self.expected_type):
            return ValidationViolation(
                rule_id=self.rule_id,
                message=f"Field '{self.field_name}' must be of type "
                        f"{self.expected_type.__name__}",
                severity=self.severity,
                field_path=self.field_name,
                deterministic_hash=hash_violation(
                    self.rule_id,
                    f"Field '{self.field_name}' must be of type "
                    f"{self.expected_type.__name__}",
                    self.field_name,
                ),
            )
        return None

Must not coerce types. Must not auto-cast. No silent normalization.

---

9️⃣ Example: Enum Conformance Rule

class EnumRule(BaseFieldRule):
    allowed_values: tuple

    def evaluate(self, input_data, context):
        value = input_data.get(self.field_name)
        if value is None:
            return None

        if value not in self.allowed_values:
            return ValidationViolation(
                rule_id=self.rule_id,
                message=f"Field '{self.field_name}' must be one of "
                        f"{self.allowed_values}",
                severity=self.severity,
                field_path=self.field_name,
                deterministic_hash=hash_violation(
                    self.rule_id,
                    f"Field '{self.field_name}' must be one of "
                    f"{self.allowed_values}",
                    self.field_name,
                ),
            )
        return None

Allowed values must be tuple. Sorted. Immutable. Stable ordering.

---

🔟 Example: Numeric Boundary Rule

class NumericMinRule(BaseFieldRule):
    min_value: int | float

    def evaluate(self, input_data, context):
        value = input_data.get(self.field_name)
        if value is None:
            return None

        if value < self.min_value:
            return ValidationViolation(
                rule_id=self.rule_id,
                message=f"Field '{self.field_name}' must be >= {self.min_value}",
                severity=self.severity,
                field_path=self.field_name,
                deterministic_hash=hash_violation(
                    self.rule_id,
                    f"Field '{self.field_name}' must be >= {self.min_value}",
                    self.field_name,
                ),
            )
        return None

No float rounding inside rule. Float handling belongs in deterministic checks.

---

1️⃣1️⃣ Rule Registry

This file must expose:

FIELD_RULES = [
    RequiredFieldRule(...),
    TypeRule(...),
    EnumRule(...),
    NumericMinRule(...),
    ...
]

Rule list must:

- Be static at import time.
- Deterministically ordered OR sorted in validators.py.
- Never dynamically generated.

This prevents cross-node divergence.

---

1️⃣2️⃣ Context Awareness (Limited)

Field rules may inspect:

context.artifact_version

Only for version-conditional requirements like:

If version >= 2, field X required.

Version thresholds must not call registry dynamically. They must use context values.

---

1️⃣3️⃣ What Field Rules Must NOT Do

❌ Cross-field logic
❌ Check derived metrics
❌ Validate compatibility matrix
❌ Inspect migration plans
❌ Consult lineage graph
❌ Compare with historical state
❌ Apply business policy
❌ Normalize values
❌ Modify input

Those belong elsewhere.

---

1️⃣4️⃣ Failure Semantics

Field-level violations represent:

Structural invalidity.

If a required field is missing, we do not:

- Attempt semantic validation.
- Attempt invariant verification.
- Attempt compatibility check.

Structural integrity precedes deeper layers.

However, that ordering is enforced by validators.py.

---

1️⃣5️⃣ Security Role

Prevents:

- Structural schema smuggling.
- Type confusion attacks.
- Enum overflow injections.
- Boundary exploitation.
- Injection via malformed nesting.
- Downgrade via missing structural element.

Field rules close the lowest-level holes.

---

1️⃣6️⃣ Determinism Requirements

All rules must produce:

- Identical message string.
- Identical field_path.
- Identical severity.
- Identical deterministic_hash.

Across:

- OS
- Python versions
- Clusters
- Replays

No dict iteration allowed in message formatting. No float repr instability.

---

1️⃣7️⃣ Testing Requirements

- Missing field test.
- Wrong type test.
- Enum overflow test.
- Boundary violation test.
- Version-dependent required test.
- Deterministic message formatting test.
- Multi-rule aggregation test.
- Stress test with 1000+ field rules.

---

1️⃣8️⃣ Relationship to Other Validation Layers

Field Rules verify:

- Atomic correctness.

Semantic Rules verify:

- Relational correctness.

Invariant Rules verify:

- System truth correctness.

Compatibility Guards verify:

- Version coexistence correctness.

Determinism Checks verify:

- Replay consistency correctness.

Each layer must remain strictly separated.

---

1️⃣9️⃣ Absolute Definition

/data/validation/field_rules.py is:

> The atomic structural validation layer that enforces deterministic, single-field
> legitimacy constraints, ensuring that no malformed or type-invalid artifact may
> proceed to semantic or invariant evaluation.

It is the smallest enforcement unit. It is the foundation of integrity.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional, Tuple, List

from .validation_contract import (
    ValidationRule,
    ValidationViolation,
    ValidationContext,
    SeverityLevel,
    ValidationScope,
    ScopeDefinition,
)


__all__ = [
    "BaseFieldRule",
    "RequiredFieldRule",
    "TypeRule",
    "NonNullableRule",
    "EnumRule",
    "StringLengthRule",
    "NumericMinRule",
    "NumericMaxRule",
    "NumericRangeRule",
    "RegexFormatRule",
    "VersionConditionalRequiredRule",
    "FIELD_RULES",
]


# ============================================================================
# Deterministic Helpers (Tier-0 Compliance)
# ============================================================================


def _normalize_float(value: float) -> str:
    """
    Normalize float representation for deterministic message formatting.
    
    Uses fixed-point notation with sufficient precision to avoid
    representation drift across Python versions and platforms.
    
    Args:
        value: Float value to normalize
        
    Returns:
        Deterministic string representation
    """
    # Use format with fixed precision to ensure deterministic output
    # 15 decimal places is sufficient for most use cases and avoids
    # scientific notation for typical validation ranges
    if value == int(value):
        # Integer floats should be represented as integers for determinism
        return str(int(value))
    return f"{value:.15f}".rstrip("0").rstrip(".")


def _compute_deterministic_hash(
    rule_id: str,
    message: str,
    field_path: Optional[str],
    severity: SeverityLevel,
) -> str:
    """
    Inline deterministic hash computation for Tier-0 compliance.
    
    This avoids external dependency risk by computing hash directly
    using the same algorithm as validation_contract but inlined.
    
    Args:
        rule_id: Rule identifier
        message: Violation message
        field_path: Field path (or None)
        severity: Severity level
        
    Returns:
        SHA-256 hex digest
    """
    parts = [
        rule_id,
        message,
        field_path or "",
        severity.value,
    ]
    canonical = json.dumps(
        parts,
        sort_keys=False,  # Order matters for determinism
        separators=(",", ":"),
        ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================================
# Base Field Rule
# ============================================================================


class BaseFieldRule(ValidationRule):
    """
    Base class for atomic field-level validation rules.
    
    All field rules operate on a single field in isolation.
    No cross-field knowledge. No business semantics. No migration awareness.
    Only atomic truth.
    
    Attributes:
        rule_id: Unique identifier for this rule
        description: Human-readable description
        severity: Severity level for violations
        applies_to: Scope definition for when rule applies
        field_name: Name of the field this rule validates
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
    ):
        """
        Initialize base field rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
        """
        super().__init__(rule_id, description, severity, applies_to)
        object.__setattr__(self, "field_name", field_name)
    
    def evaluate(
        self,
        input_data: Any,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate rule against input data.
        
        Canonical interface: evaluate(input_data, context) -> ValidationViolation | None
        
        Base implementation checks if rule applies and delegates to _evaluate_field.
        
        Args:
            input_data: Data to validate (must be dict-like for field rules)
            context: Validation context
            
        Returns:
            ValidationViolation if rule fails, None if passes
        """
        # Field rules always use self.field_name
        # This ensures atomic single-field operation
        if not isinstance(input_data, dict):
            # Non-dict input is structural corruption - surface as violation
            # This prevents silent failures that hide structural issues
            # Note: Container-shape validation ideally belongs in a higher layer,
            # but we surface it here to prevent silent failures
            message = f"Field rule requires dict input, got {type(input_data).__name__}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                SeverityLevel.ERROR,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=SeverityLevel.ERROR,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return self._evaluate_field(input_data, context)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """
        Evaluate rule on the specific field.
        
        Must be implemented by subclasses.
        
        Args:
            input_data: Dict-like data to validate
            context: Validation context
            
        Returns:
            ValidationViolation if rule fails, None if passes
        """
        raise NotImplementedError


# ============================================================================
# Required Field Rule
# ============================================================================


class RequiredFieldRule(BaseFieldRule):
    """
    Validates that a field must exist in the input data.
    
    This is the most fundamental structural check.
    If a required field is missing, structural integrity fails.
    """
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if required field exists."""
        if self.field_name not in input_data:
            message = f"Missing required field: {self.field_name}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


# ============================================================================
# Type Enforcement Rule
# ============================================================================


class TypeRule(BaseFieldRule):
    """
    Validates that a field must be of a specific type.
    
    Must not coerce types. Must not auto-cast. No silent normalization.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        expected_type: type,
    ):
        """
        Initialize type rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            expected_type: Expected Python type for the field
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        object.__setattr__(self, "expected_type", expected_type)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if field has correct type."""
        if self.field_name not in input_data:
            # Field not present - let RequiredFieldRule handle that
            return None
        
        value = input_data[self.field_name]
        if not isinstance(value, self.expected_type):
            message = f"Field '{self.field_name}' must be of type {self.expected_type.__name__}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


# ============================================================================
# Non-Nullable Rule
# ============================================================================


class NonNullableRule(BaseFieldRule):
    """
    Validates that a field must not be None.
    
    This is distinct from RequiredFieldRule:
    - RequiredFieldRule: field must exist in dict
    - NonNullableRule: field value must not be None
    
    Both can apply to the same field.
    """
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if field is not None."""
        if self.field_name not in input_data:
            # Field not present - let RequiredFieldRule handle that
            return None
        
        if input_data[self.field_name] is None:
            message = f"Field '{self.field_name}' must not be None"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


# ============================================================================
# Enum Conformance Rule
# ============================================================================


class EnumRule(BaseFieldRule):
    """
    Validates that a field value must be within allowed values.
    
    Allowed values must be tuple. Sorted. Immutable. Stable ordering.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        allowed_values: Tuple[Any, ...],
    ):
        """
        Initialize enum rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            allowed_values: Tuple of allowed values (MUST be pre-sorted and immutable)
                           DO NOT pass unsorted values - this rule does NOT reorder them
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        # Enforce tuple pre-sorted at construction - do NOT reorder internally
        # This prevents type-dependent ordering instability and cross-runtime divergence
        if not isinstance(allowed_values, tuple):
            raise ValueError("allowed_values must be a tuple (immutable, pre-sorted)")
        # Verify it's sorted (deterministic check, not mutation)
        if list(allowed_values) != sorted(allowed_values, key=str):
            raise ValueError(
                f"allowed_values must be pre-sorted. Got: {allowed_values}. "
                f"Expected sorted: {tuple(sorted(allowed_values, key=str))}"
            )
        object.__setattr__(self, "allowed_values", allowed_values)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if field value is in allowed values."""
        value = input_data.get(self.field_name)
        if value is None:
            # Field not present or None - let other rules handle that
            return None
        
        if value not in self.allowed_values:
            # Format allowed values deterministically (pre-sorted tuple)
            # Use repr for stable string representation
            allowed_str = repr(self.allowed_values)
            message = f"Field '{self.field_name}' must be one of {allowed_str}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


# ============================================================================
# String Length Constraint Rules
# ============================================================================


class StringLengthRule(BaseFieldRule):
    """
    Validates string length boundaries.
    
    Supports min_length, max_length, or both.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
    ):
        """
        Initialize string length rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            min_length: Minimum string length (inclusive)
            max_length: Maximum string length (inclusive)
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        if min_length is None and max_length is None:
            raise ValueError("StringLengthRule must specify at least min_length or max_length")
        object.__setattr__(self, "min_length", min_length)
        object.__setattr__(self, "max_length", max_length)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if string field length is within bounds."""
        value = input_data.get(self.field_name)
        if value is None:
            # Field not present or None - let other rules handle that
            return None
        
        if not isinstance(value, str):
            # Wrong type - let TypeRule handle that
            return None
        
        length = len(value)
        
        if self.min_length is not None and length < self.min_length:
            message = f"Field '{self.field_name}' length must be >= {self.min_length}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        if self.max_length is not None and length > self.max_length:
            message = f"Field '{self.field_name}' length must be <= {self.max_length}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Numeric Boundary Rules
# ============================================================================


class NumericMinRule(BaseFieldRule):
    """
    Validates that a numeric field must be >= min_value.
    
    No float rounding inside rule. Float handling belongs in deterministic checks.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        min_value: int | float,
    ):
        """
        Initialize numeric min rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            min_value: Minimum numeric value (inclusive)
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        object.__setattr__(self, "min_value", min_value)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if numeric field is >= min_value."""
        value = input_data.get(self.field_name)
        if value is None:
            # Field not present or None - let other rules handle that
            return None
        
        if not isinstance(value, (int, float)):
            # Wrong type - let TypeRule handle that
            return None
        
        if value < self.min_value:
            # Normalize float representation for deterministic messages
            min_str = _normalize_float(self.min_value) if isinstance(self.min_value, float) else str(self.min_value)
            message = f"Field '{self.field_name}' must be >= {min_str}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


class NumericMaxRule(BaseFieldRule):
    """
    Validates that a numeric field must be <= max_value.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        max_value: int | float,
    ):
        """
        Initialize numeric max rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            max_value: Maximum numeric value (inclusive)
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        object.__setattr__(self, "max_value", max_value)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if numeric field is <= max_value."""
        value = input_data.get(self.field_name)
        if value is None:
            # Field not present or None - let other rules handle that
            return None
        
        if not isinstance(value, (int, float)):
            # Wrong type - let TypeRule handle that
            return None
        
        if value > self.max_value:
            # Normalize float representation for deterministic messages
            max_str = _normalize_float(self.max_value) if isinstance(self.max_value, float) else str(self.max_value)
            message = f"Field '{self.field_name}' must be <= {max_str}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


class NumericRangeRule(BaseFieldRule):
    """
    Validates that a numeric field must be within [min_value, max_value].
    
    Combines min and max checks in a single rule for convenience.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        min_value: int | float,
        max_value: int | float,
    ):
        """
        Initialize numeric range rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            min_value: Minimum numeric value (inclusive)
            max_value: Maximum numeric value (inclusive)
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        if min_value > max_value:
            raise ValueError(f"min_value ({min_value}) must be <= max_value ({max_value})")
        object.__setattr__(self, "min_value", min_value)
        object.__setattr__(self, "max_value", max_value)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if numeric field is within range."""
        value = input_data.get(self.field_name)
        if value is None:
            # Field not present or None - let other rules handle that
            return None
        
        if not isinstance(value, (int, float)):
            # Wrong type - let TypeRule handle that
            return None
        
        if value < self.min_value:
            # Normalize float representation for deterministic messages
            min_str = _normalize_float(self.min_value) if isinstance(self.min_value, float) else str(self.min_value)
            message = f"Field '{self.field_name}' must be >= {min_str}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        if value > self.max_value:
            # Normalize float representation for deterministic messages
            max_str = _normalize_float(self.max_value) if isinstance(self.max_value, float) else str(self.max_value)
            message = f"Field '{self.field_name}' must be <= {max_str}"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Regex Format Rule
# ============================================================================


class RegexFormatRule(BaseFieldRule):
    """
    Validates that a string field must match a deterministic regex pattern.
    
    Pattern must be deterministic and stable across executions.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        pattern: str,
    ):
        """
        Initialize regex format rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            pattern: Regex pattern string (must be deterministic)
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        # Compile pattern once for efficiency and determinism
        object.__setattr__(self, "pattern", pattern)
        object.__setattr__(self, "_compiled_pattern", re.compile(pattern))
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """Check if string field matches regex pattern."""
        value = input_data.get(self.field_name)
        if value is None:
            # Field not present or None - let other rules handle that
            return None
        
        if not isinstance(value, str):
            # Wrong type - let TypeRule handle that
            return None
        
        # Use fullmatch() for atomic structural validation - prevents prefix match bypass
        # .match() allows prefix matches which is a structural bypass vector
        if not self._compiled_pattern.fullmatch(value):
            message = f"Field '{self.field_name}' must match pattern '{self.pattern}'"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        return None


# ============================================================================
# Immutable Field Rule
# ============================================================================
# NOTE: This rule has been removed from field_rules.py because it violates
# atomic isolation purity by inspecting sibling fields (previous_value_key).
# 
# ImmutableFieldRule belongs in semantic_rules.py where cross-field
# relationships can be properly validated.
# 
# If you need immutability checking, implement it as a semantic rule that
# can properly handle cross-field relationships.


# ============================================================================
# Version-Conditional Required Rule
# ============================================================================


class VersionConditionalRequiredRule(BaseFieldRule):
    """
    Validates that a field is required based on artifact version.
    
    Example: If version >= 2, field X required.
    
    Version thresholds must not call registry dynamically.
    They must use context values.
    """
    
    def __init__(
        self,
        rule_id: str,
        description: str,
        severity: SeverityLevel,
        applies_to: ScopeDefinition,
        field_name: str,
        min_version: Optional[int] = None,
        max_version: Optional[int] = None,
    ):
        """
        Initialize version-conditional required rule.
        
        Args:
            rule_id: Unique identifier for this rule
            description: Human-readable description
            severity: Severity level for violations
            applies_to: Scope definition for when rule applies
            field_name: Name of the field this rule validates
            min_version: Minimum version where field becomes required (inclusive)
            max_version: Maximum version where field is required (inclusive)
        """
        super().__init__(rule_id, description, severity, applies_to, field_name)
        object.__setattr__(self, "min_version", min_version)
        object.__setattr__(self, "max_version", max_version)
    
    def _evaluate_field(
        self,
        input_data: dict,
        context: ValidationContext,
    ) -> Optional[ValidationViolation]:
        """
        Check if field is required based on version.
        
        If version is in range [min_version, max_version], field is required.
        """
        # Check if version is in required range
        if context.artifact_version is None:
            # No version specified - cannot determine if required
            return None
        
        try:
            version = int(context.artifact_version)
        except (ValueError, TypeError):
            # Invalid version format - cannot determine if required
            return None
        
        # Check if version is in range
        in_range = True
        if self.min_version is not None and version < self.min_version:
            in_range = False
        if self.max_version is not None and version > self.max_version:
            in_range = False
        
        # If version is in range, field is required
        if in_range and self.field_name not in input_data:
            # Use ASCII-safe deterministic sentinel instead of Unicode '∞'
            # This prevents locale-dependent rendering and encoding instability
            min_str = str(self.min_version) if self.min_version is not None else "unbounded"
            max_str = str(self.max_version) if self.max_version is not None else "unbounded"
            version_range_str = f"[{min_str}, {max_str}]"
            message = f"Field '{self.field_name}' is required for version {version} (range: {version_range_str})"
            hash_value = _compute_deterministic_hash(
                self.rule_id,
                message,
                self.field_name,
                self.severity,
            )
            violation = ValidationViolation(
                rule_id=self.rule_id,
                message=message,
                severity=self.severity,
                field_path=self.field_name,
            )
            object.__setattr__(violation, "deterministic_hash", hash_value)
            return violation
        
        return None


# ============================================================================
# Rule Registry
# ============================================================================

# Static rule registry
# Rules must be deterministically ordered at import time
# This list is sorted by rule_id in validators.py, but we maintain
# a stable order here for clarity
#
# These are concrete static rules demonstrating the pattern.
# Replace with domain-specific rules as needed.
# All rules must be statically defined - never dynamically generated.

FIELD_RULES: list[ValidationRule] = [
    # Concrete static rules - these demonstrate the pattern and are ready to use
    # Rules are sorted by rule_id in validators.py for determinism
    
    # Example: Required field rule for artifact_id
    RequiredFieldRule(
        rule_id="field.required.artifact_id",
        description="Artifact ID is required",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="artifact_id",
    ),
    
    # Example: Type enforcement for artifact_id
    TypeRule(
        rule_id="field.type.artifact_id",
        description="Artifact ID must be string",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="artifact_id",
        expected_type=str,
    ),
    
    # Example: Non-nullable rule for artifact_id
    NonNullableRule(
        rule_id="field.non_null.artifact_id",
        description="Artifact ID must not be None",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="artifact_id",
    ),
    
    # Example: String length constraint
    StringLengthRule(
        rule_id="field.length.artifact_id",
        description="Artifact ID length must be between 1 and 255",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="artifact_id",
        min_length=1,
        max_length=255,
    ),
    
    # Example: Enum conformance (pre-sorted tuple required)
    EnumRule(
        rule_id="field.enum.status",
        description="Status must be one of allowed values",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="status",
        allowed_values=("active", "inactive", "pending"),  # Pre-sorted tuple
    ),
    
    # Example: Numeric range constraint
    NumericRangeRule(
        rule_id="field.range.version",
        description="Version must be between 1 and 1000",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="version",
        min_value=1,
        max_value=1000,
    ),
    
    # Example: Regex format validation
    RegexFormatRule(
        rule_id="field.format.email",
        description="Email must match standard email format",
        severity=SeverityLevel.ERROR,
        applies_to=ScopeDefinition(
            scope=ValidationScope.ARTIFACT,
        ),
        field_name="email",
        pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    ),
]
