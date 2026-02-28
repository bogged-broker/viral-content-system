"""
/data/validation/ejection_reasons.py

Typed Rejection Codes & Taxonomy
(Canonical Failure Classification System)

---

1️⃣ What This File Exists For (Non-Negotiable)

This file defines:

> The complete, immutable, versioned classification system for all validation failures in the system.

It is the:

- Single source of truth for rejection codes
- Authority for severity levels
- Taxonomy owner for error grouping
- Replay-stable failure encoding contract

If this file changes casually, you break:

- Observability
- Analytics
- Compatibility
- Auditing
- Migration safety
- Deterministic reprocessing

---

2️⃣ Core Principle

Every rejection must be:

- Globally unique
- Immutable once introduced
- Version-scoped
- Categorized
- Severity-typed
- Deterministically sortable

This file is NOT a convenience layer.
It is the canonical taxonomy of failure for your entire system.

---

3️⃣ Versioning Rules (Extremely Important)

Once introduced:

- code can NEVER change
- stable_sort_key can NEVER change
- category can NEVER change
- severity can NEVER downgrade silently

If severity changes:
- Must introduce new code
- Old code deprecated but retained

---

4️⃣ Anti-Patterns (Automatic Architecture Downgrade)

❌ Free-form message strings
❌ Raising ValueError with dynamic text
❌ Generating UUID per violation
❌ Using Python exception types as rejection identifiers
❌ Deleting old codes after migration
❌ Severity encoded only in message

---

5️⃣ Usage Pattern

In validation rules:

    if end_time <= start_time:
        yield ValidationViolation(
            reason=SEMANTIC_END_BEFORE_START,
            field_path="end_time",
            context={"start_time": start_time}
        )

You NEVER create raw strings like:

    "End time cannot be before start time"

Ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Dict, Set, List, Tuple

from .validation_contract import SeverityLevel


__all__ = [
    "RejectionSeverity",
    "RejectionCategory",
    "RejectionDeterminismClass",
    "EjectionReason",
    "ALL_EJECTION_REASONS",
    "validate_registry_integrity",
    "get_reasons_by_category",
    "get_reasons_by_severity",
    "get_reason_by_code",
    # Schema rejection codes
    "SCHEMA_INVALID_TYPE",
    "SCHEMA_MISSING_REQUIRED",
    "SCHEMA_FIELD_VALUE_OUT_OF_RANGE",
    "SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED",
    "SCHEMA_STRING_LENGTH_VIOLATION",
    "SCHEMA_NUMBER_PRECISION_VIOLATION",
    "SCHEMA_INVALID_ENUM_VALUE",
    "SCHEMA_INVALID_FORMAT",
    "SCHEMA_NESTED_STRUCTURE_INVALID",
    "SCHEMA_ARRAY_CONSTRAINT_VIOLATION",
    # Semantic rejection codes
    "SEMANTIC_END_BEFORE_START",
    "SEMANTIC_REVENUE_NO_CURRENCY",
    "SEMANTIC_INVALID_DATE_RANGE",
    "SEMANTIC_CIRCULAR_REFERENCE",
    "SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS",
    "SEMANTIC_REQUIRED_IF_OTHER_PRESENT",
    "SEMANTIC_INVALID_BUSINESS_LOGIC",
    "SEMANTIC_CROSS_FIELD_VALIDATION_FAILED",
    # Compatibility rejection codes
    "COMPAT_FIELD_NOT_ALLOWED_IN_VERSION",
    "COMPAT_MIGRATION_DOWNGRADE_BLOCKED",
    "COMPAT_VERSION_NOT_SUPPORTED",
    "COMPAT_BREAKING_CHANGE_DETECTED",
    "COMPAT_SCHEMA_VERSION_MISMATCH",
    "COMPAT_DEPRECATED_FIELD_USED",
    "COMPAT_FUTURE_VERSION_FIELD",
    # Determinism rejection codes
    "DETERMINISM_TIME_DEPENDENT_BRANCH",
    "DETERMINISM_NON_STABLE_ITERATION",
    "DETERMINISM_RANDOM_VALUE_USED",
    "DETERMINISM_EXTERNAL_STATE_DEPENDENCY",
    "DETERMINISM_NON_DETERMINISTIC_HASH",
    # Governance rejection codes
    "GOVERNANCE_LOCK_VIOLATION",
    "GOVERNANCE_PERMISSION_DENIED",
    "GOVERNANCE_INVALID_OPERATION",
    "GOVERNANCE_RATE_LIMIT_EXCEEDED",
    "GOVERNANCE_QUOTA_EXCEEDED",
]


# ============================================================================
# Rejection Severity (maps to SeverityLevel)
# ============================================================================

class RejectionSeverity(str, Enum):
    """
    Rejection severity levels.
    
    Maps to SeverityLevel from validation_contract:
    - FATAL → CRITICAL
    - ERROR → ERROR
    - WARNING → WARNING
    - INFO → INFO
    """
    
    FATAL = "fatal"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    
    def to_severity_level(self) -> SeverityLevel:
        """Convert to ValidationContract SeverityLevel."""
        mapping = {
            RejectionSeverity.FATAL: SeverityLevel.CRITICAL,
            RejectionSeverity.ERROR: SeverityLevel.ERROR,
            RejectionSeverity.WARNING: SeverityLevel.WARNING,
            RejectionSeverity.INFO: SeverityLevel.INFO,
        }
        return mapping[self]


# ============================================================================
# Rejection Category
# ============================================================================

class RejectionCategory(str, Enum):
    """
    Hierarchical categorization of rejection types.
    
    Enables:
    - Machine filtering
    - Dashboard grouping
    - Stable analytics aggregation
    - Human readability
    """
    
    SCHEMA = "schema"
    SEMANTIC = "semantic"
    COMPATIBILITY = "compatibility"
    DETERMINISM = "determinism"
    GOVERNANCE = "governance"


# ============================================================================
# Determinism Classification
# ============================================================================

class RejectionDeterminismClass(str, Enum):
    """
    Classification of determinism properties for rejection reasons.
    
    Used for advanced analytics and replay safety verification.
    """
    
    PURE = "pure"  # Rejection is always deterministic
    CONTEXT_DEPENDENT = "context_dependent"  # Depends on validation context
    VERSION_DEPENDENT = "version_dependent"  # Depends on schema version


# ============================================================================
# Ejection Reason Core Structure
# ============================================================================

@dataclass(frozen=True)
class EjectionReason:
    """
    Immutable, versioned rejection reason definition.
    
    This is the canonical representation of a validation failure type.
    Once introduced, the core fields (code, stable_sort_key, category) 
    can NEVER change.
    
    Attributes:
        code: Globally unique hierarchical code (e.g., "SCHEMA.FIELD.MISSING_REQUIRED")
        category: Rejection category for grouping
        severity: Rejection severity level
        description: Human-readable description
        introduced_in_version: Version when this reason was first introduced
        stable_sort_key: Deterministic sort key (never changes)
        determinism_class: Classification of determinism properties
        recoverable: Whether this rejection is recoverable
        constraint_id: Optional machine-readable constraint identifier
    """
    
    code: str
    category: RejectionCategory
    severity: RejectionSeverity
    description: str
    introduced_in_version: int
    stable_sort_key: str
    determinism_class: RejectionDeterminismClass = RejectionDeterminismClass.PURE
    recoverable: bool = False
    constraint_id: str = ""
    
    def __post_init__(self) -> None:
        """Validate reason structure."""
        if not self.code:
            raise ValueError("EjectionReason.code cannot be empty")
        if not self.stable_sort_key:
            raise ValueError("EjectionReason.stable_sort_key cannot be empty")
        if self.introduced_in_version < 1:
            raise ValueError("EjectionReason.introduced_in_version must be >= 1")
    
    def to_severity_level(self) -> SeverityLevel:
        """Convert to ValidationContract SeverityLevel."""
        return self.severity.to_severity_level()
    
    def __repr__(self) -> str:
        return f"EjectionReason(code={self.code!r}, category={self.category.value}, severity={self.severity.value})"


# ============================================================================
# Schema Rejection Codes (001-099)
# ============================================================================

SCHEMA_INVALID_TYPE: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.TYPE.INVALID_TYPE",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Field type does not match schema definition",
    introduced_in_version=1,
    stable_sort_key="001",
    recoverable=False,
)

SCHEMA_MISSING_REQUIRED: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.FIELD.MISSING_REQUIRED",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Required field is missing",
    introduced_in_version=1,
    stable_sort_key="002",
    recoverable=False,
)

SCHEMA_FIELD_VALUE_OUT_OF_RANGE: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.FIELD.VALUE_OUT_OF_RANGE",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Field value is outside allowed range",
    introduced_in_version=1,
    stable_sort_key="003",
    recoverable=False,
)

SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.FIELD.VALUE_NOT_IN_ALLOWED",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Field value is not in allowed values list",
    introduced_in_version=1,
    stable_sort_key="004",
    recoverable=False,
)

SCHEMA_STRING_LENGTH_VIOLATION: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.STRING.LENGTH_VIOLATION",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="String length violates min/max constraints",
    introduced_in_version=1,
    stable_sort_key="005",
    recoverable=False,
)

SCHEMA_NUMBER_PRECISION_VIOLATION: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.NUMBER.PRECISION_VIOLATION",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Number precision exceeds allowed decimal places",
    introduced_in_version=1,
    stable_sort_key="006",
    recoverable=False,
)

SCHEMA_INVALID_ENUM_VALUE: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.ENUM.INVALID_VALUE",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Value is not a valid enum option",
    introduced_in_version=1,
    stable_sort_key="007",
    recoverable=False,
)

SCHEMA_INVALID_FORMAT: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.FORMAT.INVALID",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Field format does not match required pattern",
    introduced_in_version=1,
    stable_sort_key="008",
    recoverable=False,
)

SCHEMA_NESTED_STRUCTURE_INVALID: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.STRUCTURE.NESTED_INVALID",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Nested structure does not match schema definition",
    introduced_in_version=1,
    stable_sort_key="009",
    recoverable=False,
)

SCHEMA_ARRAY_CONSTRAINT_VIOLATION: Final[EjectionReason] = EjectionReason(
    code="SCHEMA.ARRAY.CONSTRAINT_VIOLATION",
    category=RejectionCategory.SCHEMA,
    severity=RejectionSeverity.ERROR,
    description="Array violates min/max length or element constraints",
    introduced_in_version=1,
    stable_sort_key="010",
    recoverable=False,
)


# ============================================================================
# Semantic Rejection Codes (101-199)
# ============================================================================

SEMANTIC_END_BEFORE_START: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.TIME.END_BEFORE_START",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="End time must be greater than start time",
    introduced_in_version=1,
    stable_sort_key="101",
    recoverable=False,
)

SEMANTIC_REVENUE_NO_CURRENCY: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.CURRENCY.MISSING_WITH_REVENUE",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Revenue provided without currency",
    introduced_in_version=2,
    stable_sort_key="102",
    recoverable=False,
)

SEMANTIC_INVALID_DATE_RANGE: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.DATE.INVALID_RANGE",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Date range is invalid or inconsistent",
    introduced_in_version=1,
    stable_sort_key="103",
    recoverable=False,
)

SEMANTIC_CIRCULAR_REFERENCE: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.REFERENCE.CIRCULAR",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Circular reference detected in data structure",
    introduced_in_version=1,
    stable_sort_key="104",
    recoverable=False,
)

SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.CONSTRAINT.MUTUALLY_EXCLUSIVE",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Mutually exclusive fields cannot both be present",
    introduced_in_version=1,
    stable_sort_key="105",
    recoverable=False,
)

SEMANTIC_REQUIRED_IF_OTHER_PRESENT: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.CONSTRAINT.REQUIRED_IF_OTHER",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Field is required when another field is present",
    introduced_in_version=1,
    stable_sort_key="106",
    recoverable=False,
)

SEMANTIC_INVALID_BUSINESS_LOGIC: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.BUSINESS.INVALID_LOGIC",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Data violates business logic constraints",
    introduced_in_version=1,
    stable_sort_key="107",
    recoverable=False,
)

SEMANTIC_CROSS_FIELD_VALIDATION_FAILED: Final[EjectionReason] = EjectionReason(
    code="SEMANTIC.CROSS_FIELD.VALIDATION_FAILED",
    category=RejectionCategory.SEMANTIC,
    severity=RejectionSeverity.ERROR,
    description="Cross-field validation rule failed",
    introduced_in_version=1,
    stable_sort_key="108",
    recoverable=False,
)


# ============================================================================
# Compatibility Rejection Codes (201-299)
# ============================================================================

COMPAT_FIELD_NOT_ALLOWED_IN_VERSION: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.VERSION.FIELD_NOT_ALLOWED",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.FATAL,
    description="Field not allowed in provided schema version",
    introduced_in_version=3,
    stable_sort_key="201",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)

COMPAT_MIGRATION_DOWNGRADE_BLOCKED: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.MIGRATION.DOWNGRADE_BLOCKED",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.FATAL,
    description="Migration plan attempts to downgrade schema version",
    introduced_in_version=1,
    stable_sort_key="202",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)

COMPAT_VERSION_NOT_SUPPORTED: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.VERSION.NOT_SUPPORTED",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.FATAL,
    description="Schema version is not supported",
    introduced_in_version=1,
    stable_sort_key="203",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)

COMPAT_BREAKING_CHANGE_DETECTED: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.CHANGE.BREAKING",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.FATAL,
    description="Breaking change detected in schema migration",
    introduced_in_version=1,
    stable_sort_key="204",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)

COMPAT_SCHEMA_VERSION_MISMATCH: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.SCHEMA.VERSION_MISMATCH",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.ERROR,
    description="Schema version mismatch between components",
    introduced_in_version=1,
    stable_sort_key="205",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)

COMPAT_DEPRECATED_FIELD_USED: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.FIELD.DEPRECATED",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.WARNING,
    description="Deprecated field is being used",
    introduced_in_version=2,
    stable_sort_key="206",
    recoverable=True,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)

COMPAT_FUTURE_VERSION_FIELD: Final[EjectionReason] = EjectionReason(
    code="COMPATIBILITY.VERSION.FUTURE_FIELD",
    category=RejectionCategory.COMPATIBILITY,
    severity=RejectionSeverity.ERROR,
    description="Field from future version is not yet supported",
    introduced_in_version=2,
    stable_sort_key="207",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.VERSION_DEPENDENT,
)


# ============================================================================
# Determinism Rejection Codes (301-399)
# ============================================================================

DETERMINISM_TIME_DEPENDENT_BRANCH: Final[EjectionReason] = EjectionReason(
    code="DETERMINISM.TIME_DEPENDENT_BRANCH",
    category=RejectionCategory.DETERMINISM,
    severity=RejectionSeverity.FATAL,
    description="Validation contains time-dependent logic",
    introduced_in_version=1,
    stable_sort_key="301",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.CONTEXT_DEPENDENT,
)

DETERMINISM_NON_STABLE_ITERATION: Final[EjectionReason] = EjectionReason(
    code="DETERMINISM.NON_STABLE_ITERATION",
    category=RejectionCategory.DETERMINISM,
    severity=RejectionSeverity.FATAL,
    description="Validation uses non-deterministic iteration order",
    introduced_in_version=1,
    stable_sort_key="302",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.CONTEXT_DEPENDENT,
)

DETERMINISM_RANDOM_VALUE_USED: Final[EjectionReason] = EjectionReason(
    code="DETERMINISM.RANDOM_VALUE_USED",
    category=RejectionCategory.DETERMINISM,
    severity=RejectionSeverity.FATAL,
    description="Validation uses random values",
    introduced_in_version=1,
    stable_sort_key="303",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.CONTEXT_DEPENDENT,
)

DETERMINISM_EXTERNAL_STATE_DEPENDENCY: Final[EjectionReason] = EjectionReason(
    code="DETERMINISM.EXTERNAL_STATE_DEPENDENCY",
    category=RejectionCategory.DETERMINISM,
    severity=RejectionSeverity.FATAL,
    description="Validation depends on external state",
    introduced_in_version=1,
    stable_sort_key="304",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.CONTEXT_DEPENDENT,
)

DETERMINISM_NON_DETERMINISTIC_HASH: Final[EjectionReason] = EjectionReason(
    code="DETERMINISM.NON_DETERMINISTIC_HASH",
    category=RejectionCategory.DETERMINISM,
    severity=RejectionSeverity.FATAL,
    description="Validation uses non-deterministic hash function",
    introduced_in_version=1,
    stable_sort_key="305",
    recoverable=False,
    determinism_class=RejectionDeterminismClass.CONTEXT_DEPENDENT,
)


# ============================================================================
# Governance Rejection Codes (401-499)
# ============================================================================

GOVERNANCE_LOCK_VIOLATION: Final[EjectionReason] = EjectionReason(
    code="GOVERNANCE.LOCK.VIOLATION",
    category=RejectionCategory.GOVERNANCE,
    severity=RejectionSeverity.FATAL,
    description="Governance lock violation detected",
    introduced_in_version=1,
    stable_sort_key="401",
    recoverable=False,
)

GOVERNANCE_PERMISSION_DENIED: Final[EjectionReason] = EjectionReason(
    code="GOVERNANCE.PERMISSION.DENIED",
    category=RejectionCategory.GOVERNANCE,
    severity=RejectionSeverity.FATAL,
    description="Operation not permitted by governance rules",
    introduced_in_version=1,
    stable_sort_key="402",
    recoverable=False,
)

GOVERNANCE_INVALID_OPERATION: Final[EjectionReason] = EjectionReason(
    code="GOVERNANCE.OPERATION.INVALID",
    category=RejectionCategory.GOVERNANCE,
    severity=RejectionSeverity.ERROR,
    description="Invalid governance operation attempted",
    introduced_in_version=1,
    stable_sort_key="403",
    recoverable=False,
)

GOVERNANCE_RATE_LIMIT_EXCEEDED: Final[EjectionReason] = EjectionReason(
    code="GOVERNANCE.RATE_LIMIT.EXCEEDED",
    category=RejectionCategory.GOVERNANCE,
    severity=RejectionSeverity.ERROR,
    description="Rate limit exceeded for governance operation",
    introduced_in_version=1,
    stable_sort_key="404",
    recoverable=True,
)

GOVERNANCE_QUOTA_EXCEEDED: Final[EjectionReason] = EjectionReason(
    code="GOVERNANCE.QUOTA.EXCEEDED",
    category=RejectionCategory.GOVERNANCE,
    severity=RejectionSeverity.ERROR,
    description="Quota exceeded for governance operation",
    introduced_in_version=1,
    stable_sort_key="405",
    recoverable=True,
)


# ============================================================================
# Complete Registry
# ============================================================================

ALL_EJECTION_REASONS: Final[Tuple[EjectionReason, ...]] = (
    # Schema (001-010)
    SCHEMA_INVALID_TYPE,
    SCHEMA_MISSING_REQUIRED,
    SCHEMA_FIELD_VALUE_OUT_OF_RANGE,
    SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED,
    SCHEMA_STRING_LENGTH_VIOLATION,
    SCHEMA_NUMBER_PRECISION_VIOLATION,
    SCHEMA_INVALID_ENUM_VALUE,
    SCHEMA_INVALID_FORMAT,
    SCHEMA_NESTED_STRUCTURE_INVALID,
    SCHEMA_ARRAY_CONSTRAINT_VIOLATION,
    # Semantic (101-108)
    SEMANTIC_END_BEFORE_START,
    SEMANTIC_REVENUE_NO_CURRENCY,
    SEMANTIC_INVALID_DATE_RANGE,
    SEMANTIC_CIRCULAR_REFERENCE,
    SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS,
    SEMANTIC_REQUIRED_IF_OTHER_PRESENT,
    SEMANTIC_INVALID_BUSINESS_LOGIC,
    SEMANTIC_CROSS_FIELD_VALIDATION_FAILED,
    # Compatibility (201-207)
    COMPAT_FIELD_NOT_ALLOWED_IN_VERSION,
    COMPAT_MIGRATION_DOWNGRADE_BLOCKED,
    COMPAT_VERSION_NOT_SUPPORTED,
    COMPAT_BREAKING_CHANGE_DETECTED,
    COMPAT_SCHEMA_VERSION_MISMATCH,
    COMPAT_DEPRECATED_FIELD_USED,
    COMPAT_FUTURE_VERSION_FIELD,
    # Determinism (301-305)
    DETERMINISM_TIME_DEPENDENT_BRANCH,
    DETERMINISM_NON_STABLE_ITERATION,
    DETERMINISM_RANDOM_VALUE_USED,
    DETERMINISM_EXTERNAL_STATE_DEPENDENCY,
    DETERMINISM_NON_DETERMINISTIC_HASH,
    # Governance (401-405)
    GOVERNANCE_LOCK_VIOLATION,
    GOVERNANCE_PERMISSION_DENIED,
    GOVERNANCE_INVALID_OPERATION,
    GOVERNANCE_RATE_LIMIT_EXCEEDED,
    GOVERNANCE_QUOTA_EXCEEDED,
)


# ============================================================================
# Registry Integrity Validation
# ============================================================================

def validate_registry_integrity() -> None:
    """
    Validate registry integrity at import time.
    
    Ensures:
    - All codes are globally unique
    - All stable_sort_keys are unique
    - No duplicate entries
    
    Raises:
        AssertionError: If registry integrity is violated
    """
    codes: Set[str] = set()
    sort_keys: Set[str] = set()
    
    for reason in ALL_EJECTION_REASONS:
        # Check code uniqueness
        if reason.code in codes:
            raise AssertionError(
                f"Duplicate rejection code detected: {reason.code}"
            )
        codes.add(reason.code)
        
        # Check sort key uniqueness
        if reason.stable_sort_key in sort_keys:
            raise AssertionError(
                f"Duplicate stable_sort_key detected: {reason.stable_sort_key} "
                f"for code {reason.code}"
            )
        sort_keys.add(reason.stable_sort_key)
    
    # Verify all codes follow hierarchical naming convention
    for reason in ALL_EJECTION_REASONS:
        parts = reason.code.split(".")
        if len(parts) < 2:
            raise AssertionError(
                f"Rejection code must have at least 2 parts: {reason.code}"
            )


# ============================================================================
# Registry Query Functions
# ============================================================================

def get_reasons_by_category(
    category: RejectionCategory,
) -> List[EjectionReason]:
    """
    Get all rejection reasons for a given category.
    
    Args:
        category: Rejection category to filter by
        
    Returns:
        List of rejection reasons, sorted by stable_sort_key
    """
    reasons = [r for r in ALL_EJECTION_REASONS if r.category == category]
    return sorted(reasons, key=lambda r: r.stable_sort_key)


def get_reasons_by_severity(
    severity: RejectionSeverity,
) -> List[EjectionReason]:
    """
    Get all rejection reasons for a given severity.
    
    Args:
        severity: Rejection severity to filter by
        
    Returns:
        List of rejection reasons, sorted by stable_sort_key
    """
    reasons = [r for r in ALL_EJECTION_REASONS if r.severity == severity]
    return sorted(reasons, key=lambda r: r.stable_sort_key)


def get_reason_by_code(code: str) -> EjectionReason:
    """
    Get rejection reason by code.
    
    Args:
        code: Rejection code (e.g., "SCHEMA.FIELD.MISSING_REQUIRED")
        
    Returns:
        EjectionReason with matching code
        
    Raises:
        KeyError: If code not found
    """
    for reason in ALL_EJECTION_REASONS:
        if reason.code == code:
            return reason
    raise KeyError(f"Rejection code not found: {code}")


# ============================================================================
# Deterministic Violation Sorting Helper
# ============================================================================

def sort_violations_by_reason(
    violations: list,
    field_path_key: str = "field_path",
    reason_key: str = "reason",
) -> list:
    """
    Sort violations deterministically by rejection reason.
    
    Sorting order:
    1. Category rank (SCHEMA < SEMANTIC < COMPATIBILITY < DETERMINISM < GOVERNANCE)
    2. Stable sort key
    3. Field path
    4. Code
    
    This is critical for:
    - Replay equality
    - Snapshot comparison
    - Hash-stable validation results
    
    Args:
        violations: List of violation objects with reason and field_path
        field_path_key: Key to access field_path in violation object
        reason_key: Key to access reason in violation object
        
    Returns:
        Sorted list of violations
    """
    category_order = {
        RejectionCategory.SCHEMA: 0,
        RejectionCategory.SEMANTIC: 1,
        RejectionCategory.COMPATIBILITY: 2,
        RejectionCategory.DETERMINISM: 3,
        RejectionCategory.GOVERNANCE: 4,
    }
    
    def sort_key(violation):
        reason = getattr(violation, reason_key, None)
        if not isinstance(reason, EjectionReason):
            # Fallback for violations without EjectionReason
            return (999, "", getattr(violation, field_path_key, ""), "")
        
        field_path = getattr(violation, field_path_key, "") or ""
        
        return (
            category_order.get(reason.category, 999),
            reason.stable_sort_key,
            field_path,
            reason.code,
        )
    
    return sorted(violations, key=sort_key)


# ============================================================================
# Import-Time Validation
# ============================================================================

# Validate registry integrity when module is imported
validate_registry_integrity()
