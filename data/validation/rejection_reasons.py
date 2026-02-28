"""
/data/validation/rejection_reasons.py

Explainable Validation Failures

---

1️⃣ Core Responsibility

This file translates:

- EjectionReason.code + structured context

into:

- Human-readable explanation
- Deterministic message templates
- Structured remediation guidance
- Localization-ready text

It must:

- Never redefine failure meaning
- Never invent new codes
- Never modify severity
- Never change machine contract
- Never rely on runtime entropy

It is strictly a derived explanation layer.

---

2️⃣ Architectural Separation

There are now two layers for "reasons":

Layer              File                    Purpose
─────────────────────────────────────────────────────────────
Canonical          ejection_reasons.py     Immutable machine taxonomy
Explainable        rejection_reasons.py    Deterministic human presentation

They must never be conflated.

---

3️⃣ Explanation Model Structure

You need deterministic, context-aware message templates.

This file provides:
- ExplainableReason objects (reason + template + remediation + formatter)
- Central registry mapping canonical codes to explainable versions
- Deterministic rendering functions
- Structured remediation guidance

---

4️⃣ Threat Model (Tier-0 Security Assumptions)

This module defends against the following adversarial scenarios:

Threat 1: Adversarial Template Mutation
- Attack: Runtime modification of template strings to alter audit messages
- Defense: Immutable template mappings (MappingProxyType), template hash validation
- Impact if compromised: Audit reproducibility breaks, compliance violations

Threat 2: Nondeterministic Formatting
- Attack: Formatter functions with side effects, time-dependent logic, random values
- Defense: Pure function contracts, deterministic normalization, explicit ordering
- Impact if compromised: Non-reproducible audit trails, compliance failures

Threat 3: Registry Omission Attacks
- Attack: Missing explainable mappings for canonical reasons
- Defense: Registry completeness validation, strict equality checks
- Impact if compromised: Silent explainability gaps, compliance blind spots

Threat 4: Context Normalization Lossy Compression
- Attack: Different original contexts that normalize identically become indistinguishable
- Defense: Preserve raw_context alongside normalized_context for audit provenance
- Impact if compromised: Audit trail information loss, forensic reconstruction impossible

Threat 5: Locale Fallback Non-Determinism
- Attack: Locale fallback path produces different output than default locale
- Defense: Locale fallback determinism validation, explicit fallback contract
- Impact if compromised: Audit text varies by locale availability, compliance inconsistency

Threat 6: Template Semantic Drift
- Attack: Template wording changes that alter semantic meaning without hash change
- Defense: Semantic hash separation, hash schema versioning, explicit template versioning
- Impact if compromised: Silent semantic changes, audit interpretation errors

Threat 7: Import-Time Validation Bypass
- Attack: Bypassing validation through module reload or dynamic import manipulation
- Defense: Optional strict import validation, deferred validation at startup
- Impact if compromised: Invalid state in production, compliance violations

Threat 8: Formatter Purity Violations
- Attack: Formatter functions with side effects, external dependencies, non-determinism
- Defense: Formatter purity contracts (documented, not yet mechanically verified)
- Impact if compromised: Non-reproducible output, compliance failures

This threat model explicitly codifies adversarial assumptions for Tier-0 governance.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, date, time
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Callable, Mapping, Any, Dict, Final, Optional, Tuple, List
from types import MappingProxyType

from .ejection_reasons import (
    EjectionReason,
    ALL_EJECTION_REASONS,
    RejectionSeverity,
    RejectionCategory,
    get_reason_by_code,
    # Schema reasons
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
    # Semantic reasons
    SEMANTIC_END_BEFORE_START,
    SEMANTIC_REVENUE_NO_CURRENCY,
    SEMANTIC_INVALID_DATE_RANGE,
    SEMANTIC_CIRCULAR_REFERENCE,
    SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS,
    SEMANTIC_REQUIRED_IF_OTHER_PRESENT,
    SEMANTIC_INVALID_BUSINESS_LOGIC,
    SEMANTIC_CROSS_FIELD_VALIDATION_FAILED,
    # Compatibility reasons
    COMPAT_FIELD_NOT_ALLOWED_IN_VERSION,
    COMPAT_MIGRATION_DOWNGRADE_BLOCKED,
    COMPAT_VERSION_NOT_SUPPORTED,
    COMPAT_BREAKING_CHANGE_DETECTED,
    COMPAT_SCHEMA_VERSION_MISMATCH,
    COMPAT_DEPRECATED_FIELD_USED,
    COMPAT_FUTURE_VERSION_FIELD,
    # Determinism reasons
    DETERMINISM_TIME_DEPENDENT_BRANCH,
    DETERMINISM_NON_STABLE_ITERATION,
    DETERMINISM_RANDOM_VALUE_USED,
    DETERMINISM_EXTERNAL_STATE_DEPENDENCY,
    DETERMINISM_NON_DETERMINISTIC_HASH,
    # Governance reasons
    GOVERNANCE_LOCK_VIOLATION,
    GOVERNANCE_PERMISSION_DENIED,
    GOVERNANCE_INVALID_OPERATION,
    GOVERNANCE_RATE_LIMIT_EXCEEDED,
    GOVERNANCE_QUOTA_EXCEEDED,
)

from .error_model import ValidationViolation


__all__ = [
    "FixType",
    "ExplainableReason",
    "Remediation",
    "EXPLAINABLE_REGISTRY",
    "EXPLANATION_SCHEMA_VERSION",
    "DEFAULT_LOCALE",
    "normalize_context_value",
    "normalize_context",
    "render_violation",
    "render_violation_dict",
    "render_violation_with_level",
    "validate_explainable_registry",
    "get_explainable_reason",
    "compute_template_hash",
    "validate_template_hashes",
]


# ============================================================================
# Explanation Schema Versioning
# ============================================================================

EXPLANATION_SCHEMA_VERSION: int = 1
"""
Explanation schema version number.

When the structure of ExplainableReason or rendering format changes,
this version must increment. This enables:
- Version-aware rendering clients
- Migration tools
- Long-term compatibility tracking
"""

# ============================================================================
# Hash Schema Versioning (Tier-0: Forward Compatibility)
# ============================================================================

HASH_SCHEMA_VERSION: Final[int] = 1
"""
Hash schema version for template hash computation.

When the hash computation format changes (e.g., field ordering, included fields),
this version must increment. This enables:
- Forward compatibility with historical hashes
- Migration paths for hash format evolution
- Detection of hash schema mismatches

Hash input format: hash_schema_version={version},template={template},reason_code={code}
"""

SEMANTIC_HASH_SCHEMA_VERSION: Final[int] = 1
"""
Semantic hash schema version (excludes remediation_action for semantic stability).

Semantic hash captures only template + reason_code (semantic intent).
Display hash includes remediation_action (presentation metadata).

This separation allows remediation wording changes without breaking semantic
audit reproducibility.
"""

# ============================================================================
# Locale Configuration (Tier-0: Explicit Locale Model)
# ============================================================================

DEFAULT_LOCALE: Final[str] = "en_US"
"""
Default locale for message rendering.

Locale must be explicitly passed to rendering functions.
Never inferred from runtime environment to maintain determinism.
"""


# ============================================================================
# Canonical Context Normalization (Tier-0: Deterministic Serialization)
# ============================================================================

def normalize_context_value(value: Any) -> str:
    """
    Normalize a context value to deterministic string representation.
    
    This ensures that floats, decimals, datetimes, and other types
    are serialized deterministically before formatting, eliminating
    runtime repr variance that could break audit reproducibility.
    
    Handles:
    - Floats: Normalized to fixed-precision string (IEEE 754)
    - Decimals: Normalized string representation
    - Datetimes: ISO 8601 format (UTC)
    - Dates: ISO 8601 format
    - Times: ISO 8601 format
    - Sets: Sorted list representation
    - Lists/Tuples: Recursively normalized
    - Dicts: Sorted keys, normalized values
    - Primitives: String representation
    
    Args:
        value: Value to normalize
        
    Returns:
        Deterministic string representation
        
    Note:
        This is Tier-0 determinism enforcement. Same value always
        produces same string, regardless of Python version, platform,
        or runtime state.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        # Deterministic float normalization (IEEE 754)
        if value == float('inf'):
            return "Infinity"
        if value == float('-inf'):
            return "-Infinity"
        if value != value:  # NaN
            return "NaN"
        # Use Decimal for stable representation
        try:
            decimal_repr = Decimal(str(value))
            normalized = decimal_repr.normalize()
            return str(normalized)
        except (InvalidOperation, ValueError, OverflowError):
            return repr(value)
    if isinstance(value, Decimal):
        # Decimal normalization
        normalized = value.normalize()
        return str(normalized)
    if isinstance(value, datetime):
        # ISO 8601 format (UTC) for determinism
        if value.tzinfo is None:
            # Assume UTC if no timezone
            value = value.replace(tzinfo=None)
        return value.isoformat() + "Z" if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        normalized_items = [normalize_context_value(item) for item in value]
        return f"[{', '.join(normalized_items)}]"
    if isinstance(value, set):
        # Sort for determinism
        normalized_items = sorted([normalize_context_value(item) for item in value])
        return f"{{{', '.join(normalized_items)}}}"
    if isinstance(value, dict):
        # Sort keys for determinism
        normalized_pairs = [
            f"{normalize_context_value(k)}: {normalize_context_value(v)}"
            for k, v in sorted(value.items())
        ]
        return f"{{{', '.join(normalized_pairs)}}}"
    if isinstance(value, Mapping):
        # Handle other mapping types
        normalized_pairs = [
            f"{normalize_context_value(k)}: {normalize_context_value(v)}"
            for k, v in sorted(value.items())
        ]
        return f"{{{', '.join(normalized_pairs)}}}"
    # Fallback: use repr (should be rare)
    return repr(value)


def normalize_context(context: Mapping[str, Any]) -> tuple[Mapping[str, str], Mapping[str, Any]]:
    """
    Normalize entire context mapping to deterministic string values.
    
    This ensures all context values are normalized before formatting,
    eliminating runtime variance in message generation.
    
    Tier-0: Preserves raw_context alongside normalized_context for audit provenance.
    This prevents lossy compression where different original contexts that normalize
    identically become indistinguishable.
    
    Args:
        context: Context mapping to normalize
        
    Returns:
        Tuple of (normalized_context, raw_context)
        - normalized_context: Immutable mapping with normalized string values
        - raw_context: Immutable copy of original context for audit provenance
        
    Note:
        - All values are converted to deterministic strings in normalized_context
        - Keys are preserved as-is
        - Both results are immutable (MappingProxyType)
        - raw_context preserves original types for audit trail
    """
    normalized = {
        str(k): normalize_context_value(v)
        for k, v in context.items()
    }
    # Preserve raw context for audit provenance (Tier-0 requirement)
    raw = {str(k): v for k, v in context.items()}
    return (MappingProxyType(normalized), MappingProxyType(raw))


# ============================================================================
# Fix Type Enum (Tier-0: Prevents Typo Drift)
# ============================================================================

class FixType(str, Enum):
    """
    Enumeration of fix types for remediation classification.
    
    This prevents accidental typo drift (e.g., "DATAFIX" vs "DATA_FIX")
    and ensures type safety in remediation handling.
    """
    
    CONFIG_CHANGE = "CONFIG_CHANGE"
    DATA_FIX = "DATA_FIX"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"


# ============================================================================
# Remediation Classification
# ============================================================================

@dataclass(frozen=True)
class Remediation:
    """
    Structured remediation guidance for a validation failure.
    
    Attributes:
        action: Human-readable action description
        fix_type: Type of fix required (FixType enum)
        documentation_url: Optional static URL to documentation (must be static)
        
    Design:
        - URLs must be static (no dynamic linking logic)
        - fix_type enables automated fix classification
        - action provides human-readable guidance
        - fix_type is enum-typed to prevent typo drift
    """
    
    action: str
    fix_type: FixType
    documentation_url: Optional[str] = None
    
    def __post_init__(self) -> None:
        """Validate remediation structure."""
        if not self.action:
            raise ValueError("Remediation.action cannot be empty")
        if not isinstance(self.fix_type, FixType):
            raise ValueError(
                f"Remediation.fix_type must be FixType enum, got {type(self.fix_type)}"
            )


# ============================================================================
# Explainable Reason Model
# ============================================================================

@dataclass(frozen=True)
class ExplainableReason:
    """
    Human-facing explanation for a canonical EjectionReason.
    
    This provides deterministic, context-aware message templates that
    translate machine codes into human-readable explanations. It never
    modifies the canonical reason - it only provides presentation.
    
    Attributes:
        reason: The canonical EjectionReason (immutable reference)
        templates: Locale-extensible template mapping (locale -> template string)
        remediation: Structured remediation guidance
        formatter: Deterministic function that formats template with normalized context
        template_hash: Precomputed SHA-256 hash (frozen constant, verified at import)
        
    Design:
        - reason references canonical taxonomy (never redefined)
        - templates is locale-extensible (Mapping[str, str]) for additive localization
        - formatter receives normalized context (all values are deterministic strings)
        - remediation provides structured guidance
        - template_hash is precomputed constant (verified at import for drift detection)
        - Locale must be explicitly passed (never inferred from runtime)
    """
    
    reason: EjectionReason
    templates: Mapping[str, str]  # locale -> template string
    remediation: Remediation
    formatter: Callable[[str, Mapping[str, str]], str]  # Takes template and normalized context (str values)
    template_hash: str  # Precomputed constant, not computed in __post_init__
    
    def __post_init__(self) -> None:
        """Validate explainable reason structure."""
        if not self.templates:
            raise ValueError("ExplainableReason.templates cannot be empty")
        if DEFAULT_LOCALE not in self.templates:
            raise ValueError(
                f"ExplainableReason.templates must include DEFAULT_LOCALE ({DEFAULT_LOCALE})"
            )
        if not callable(self.formatter):
            raise ValueError("ExplainableReason.formatter must be callable")
        if not self.template_hash:
            raise ValueError("ExplainableReason.template_hash cannot be empty")
        if len(self.template_hash) != 64:
            raise ValueError(
                f"ExplainableReason.template_hash must be 64 hex characters (SHA-256), "
                f"got {len(self.template_hash)}"
            )
        
        # Verify template hash matches computed value (Tier-0 drift detection)
        default_template = self.templates[DEFAULT_LOCALE]
        computed_hash = compute_template_hash(
            default_template,
            self.remediation.action,
            self.reason.code,
        )
        if self.template_hash != computed_hash:
            raise ValueError(
                f"Template hash mismatch for {self.reason.code}: "
                f"provided={self.template_hash[:16]}..., "
                f"computed={computed_hash[:16]}... "
                f"This indicates template drift or incorrect hash."
            )
        
        # Basic formatter purity check (Tier-0 requirement)
        # Verify formatter signature matches expected contract
        import inspect
        sig = inspect.signature(self.formatter)
        params = list(sig.parameters.keys())
        if len(params) != 2 or params[0] != "template" or params[1] != "context":
            raise ValueError(
                f"Formatter for {self.reason.code} has invalid signature: "
                f"expected (template: str, context: Mapping[str, str]), "
                f"got {sig}. Formatters must be pure functions with deterministic contracts."
            )
    
    def get_template(self, locale: str = DEFAULT_LOCALE) -> str:
        """
        Get template for specified locale, falling back to default.
        
        Args:
            locale: Locale identifier (e.g., "en_US", "es_ES")
            
        Returns:
            Template string for locale, or default locale if not found
        """
        return self.templates.get(locale, self.templates[DEFAULT_LOCALE])


# ============================================================================
# Message Templates (Static Strings)
# ============================================================================

# Template design rules:
# - Must be static strings
# - Use named placeholders (e.g., {field_name})
# - Never dynamically alter grammar
# - Never depend on time
# - Never rely on external data

TEMPLATE_END_BEFORE_START = (
    "The field '{end_field}' must be greater than '{start_field}'. "
    "Received end={end_value}, start={start_value}."
)

TEMPLATE_MISSING_REQUIRED = (
    "Required field '{field_name}' is missing from the input."
)

TEMPLATE_INVALID_TYPE = (
    "Field '{field_name}' has invalid type. Expected {expected_type}, "
    "but received {actual_type}."
)

TEMPLATE_VALUE_OUT_OF_RANGE = (
    "Field '{field_name}' value {value} is outside allowed range. "
    "Expected: {min_value} <= value <= {max_value}."
)

TEMPLATE_STRING_LENGTH_VIOLATION = (
    "Field '{field_name}' string length {length} violates constraints. "
    "Expected: {min_length} <= length <= {max_length}."
)

TEMPLATE_INVALID_ENUM_VALUE = (
    "Field '{field_name}' value '{value}' is not a valid enum option. "
    "Allowed values: {allowed_values}."
)

TEMPLATE_INVALID_FORMAT = (
    "Field '{field_name}' format does not match required pattern '{pattern}'. "
    "Received: '{value}'."
)

TEMPLATE_INVALID_DATE_RANGE = (
    "Date range is invalid. {description}"
)

TEMPLATE_MUTUALLY_EXCLUSIVE = (
    "Fields '{field1}' and '{field2}' are mutually exclusive and cannot both be present."
)

TEMPLATE_REQUIRED_IF_OTHER = (
    "Field '{required_field}' is required when '{other_field}' is present."
)

TEMPLATE_SCHEMA_VERSION_MISMATCH = (
    "Schema version mismatch. Expected version {expected_version}, "
    "but received version {actual_version}."
)

TEMPLATE_DEPRECATED_FIELD = (
    "Field '{field_name}' is deprecated and should not be used. "
    "Use '{replacement_field}' instead."
)

TEMPLATE_PERMISSION_DENIED = (
    "Operation not permitted by governance rules. {description}"
)

TEMPLATE_RATE_LIMIT_EXCEEDED = (
    "Rate limit exceeded for operation '{operation}'. "
    "Limit: {limit} requests per {period}."
)

# Additional templates for missing ejection reasons
TEMPLATE_VALUE_NOT_IN_ALLOWED = (
    "Field '{field_name}' value '{value}' is not in the allowed values list. "
    "Allowed values: {allowed_values}."
)

TEMPLATE_NUMBER_PRECISION_VIOLATION = (
    "Field '{field_name}' number precision exceeds allowed decimal places. "
    "Maximum precision: {max_precision}, received: {actual_precision}."
)

TEMPLATE_NESTED_STRUCTURE_INVALID = (
    "Nested structure at '{field_path}' does not match schema definition. {description}"
)

TEMPLATE_ARRAY_CONSTRAINT_VIOLATION = (
    "Array field '{field_name}' violates constraints. {description}"
)

TEMPLATE_REVENUE_NO_CURRENCY = (
    "Revenue value provided without currency specification. "
    "Field '{revenue_field}' requires '{currency_field}' to be present."
)

TEMPLATE_CIRCULAR_REFERENCE = (
    "Circular reference detected in data structure. {description}"
)

TEMPLATE_INVALID_BUSINESS_LOGIC = (
    "Data violates business logic constraints. {description}"
)

TEMPLATE_CROSS_FIELD_VALIDATION_FAILED = (
    "Cross-field validation failed. {description}"
)

TEMPLATE_FIELD_NOT_ALLOWED_IN_VERSION = (
    "Field '{field_name}' is not allowed in schema version {schema_version}. "
    "This field is available in version {min_version} or later."
)

TEMPLATE_MIGRATION_DOWNGRADE_BLOCKED = (
    "Migration plan attempts to downgrade schema version from {from_version} to {to_version}. "
    "Downgrades are not permitted."
)

TEMPLATE_VERSION_NOT_SUPPORTED = (
    "Schema version {version} is not supported. "
    "Supported versions: {supported_versions}."
)

TEMPLATE_BREAKING_CHANGE_DETECTED = (
    "Breaking change detected in schema migration. {description}"
)

TEMPLATE_FUTURE_VERSION_FIELD = (
    "Field '{field_name}' is from future schema version {future_version}. "
    "Current maximum supported version: {current_version}."
)

TEMPLATE_TIME_DEPENDENT_BRANCH = (
    "Validation contains time-dependent logic, violating determinism requirements. {description}"
)

TEMPLATE_NON_STABLE_ITERATION = (
    "Validation uses non-deterministic iteration order, violating determinism requirements. {description}"
)

TEMPLATE_RANDOM_VALUE_USED = (
    "Validation uses random values, violating determinism requirements. {description}"
)

TEMPLATE_EXTERNAL_STATE_DEPENDENCY = (
    "Validation depends on external state, violating determinism requirements. {description}"
)

TEMPLATE_NON_DETERMINISTIC_HASH = (
    "Validation uses non-deterministic hash function, violating determinism requirements. {description}"
)

TEMPLATE_GOVERNANCE_LOCK_VIOLATION = (
    "Governance lock violation detected. {description}"
)

TEMPLATE_INVALID_OPERATION = (
    "Invalid governance operation attempted. {description}"
)

TEMPLATE_QUOTA_EXCEEDED = (
    "Quota exceeded for operation '{operation}'. "
    "Limit: {limit} {unit}, current usage: {current_usage}."
)


# ============================================================================
# Helper: Create Template Mapping with Precomputed Hash (Tier-0)
# ============================================================================

def create_template_mapping(
    default_template: str,
    remediation_action: str,
    reason_code: str,
    additional_locales: Optional[Mapping[str, str]] = None,
) -> tuple[Mapping[str, str], str]:
    """
    Create locale-extensible template mapping with precomputed hash.
    
    This helper ensures template hashes are precomputed as constants
    and templates are locale-extensible from the start.
    
    Args:
        default_template: Template for DEFAULT_LOCALE
        remediation_action: Remediation action string
        reason_code: Canonical EjectionReason code
        additional_locales: Optional additional locale templates
        
    Returns:
        Tuple of (template_mapping, precomputed_hash)
        
    Note:
        - Hash is computed from default template (Tier-0)
        - Template mapping is immutable (MappingProxyType) - Tier-0 requirement
        - Additional locales are additive (non-breaking)
        - Immutability prevents runtime template mutation that breaks determinism
    """
    templates: dict[str, str] = {DEFAULT_LOCALE: default_template}
    if additional_locales:
        # Ensure additional_locales is also immutable-safe
        if isinstance(additional_locales, MappingProxyType):
            templates.update(additional_locales)
        else:
            # Create immutable copy to prevent mutation
            templates.update(dict(additional_locales))
    
    # Tier-0: Enforce immutability to prevent runtime template mutation
    # This prevents silent determinism collapse from template edits
    immutable_templates = MappingProxyType(templates)
    
    # Precompute hash from default template (Tier-0)
    template_hash = compute_template_hash(
        default_template,
        remediation_action,
        reason_code,
    )
    
    return (immutable_templates, template_hash)


# ============================================================================
# Formatter Functions (Deterministic - Normalized Context)
# ============================================================================

# Tier-0: All formatters now receive normalized context (Mapping[str, str])
# All values are deterministic strings, eliminating runtime repr variance.

def format_end_before_start(template: str, context: Mapping[str, str]) -> str:
    """Format end_before_start violation message (normalized context)."""
    return template.format(
        end_field=context.get("end_field", "end_time"),
        start_field=context.get("start_field", "start_time"),
        end_value=context.get("end_value", context.get("end_time", "unknown")),
        start_value=context.get("start_value", context.get("start_time", "unknown")),
    )


def format_missing_required(template: str, context: Mapping[str, str]) -> str:
    """Format missing_required violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
    )


def format_invalid_type(template: str, context: Mapping[str, str]) -> str:
    """Format invalid_type violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        expected_type=context.get("expected_type", "unknown"),
        actual_type=context.get("actual_type", "unknown"),
    )


def format_value_out_of_range(template: str, context: Mapping[str, str]) -> str:
    """Format value_out_of_range violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        value=context.get("value", "unknown"),
        min_value=context.get("min_value", "unknown"),
        max_value=context.get("max_value", "unknown"),
    )


def format_string_length_violation(template: str, context: Mapping[str, str]) -> str:
    """Format string_length_violation violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        length=context.get("length", "unknown"),
        min_length=context.get("min_length", "unknown"),
        max_length=context.get("max_length", "unknown"),
    )


def format_invalid_enum_value(template: str, context: Mapping[str, str]) -> str:
    """
    Format invalid_enum_value violation message.
    
    Tier-0 Determinism: Sorts allowed_values to ensure deterministic output
    even if upstream passes a set or unordered collection.
    """
    allowed_values = context.get("allowed_values", [])
    if isinstance(allowed_values, (list, tuple, set)):
        # Sort for deterministic ordering (Tier-0 requirement)
        # This prevents nondeterministic output if upstream passes a set
        sorted_values = sorted(str(v) for v in allowed_values)
        allowed_str = ", ".join(sorted_values)
    else:
        allowed_str = str(allowed_values)
    
    return template.format(
        field_name=context.get("field_name", "unknown"),
        value=context.get("value", "unknown"),
        allowed_values=allowed_str,
    )


def format_invalid_format(template: str, context: Mapping[str, str]) -> str:
    """Format invalid_format violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        pattern=context.get("pattern", "unknown"),
        value=context.get("value", "unknown"),
    )


def format_invalid_date_range(template: str, context: Mapping[str, str]) -> str:
    """Format invalid_date_range violation message."""
    description = context.get("description", "Date range is inconsistent or invalid.")
    return template.format(description=description)


def format_mutually_exclusive(template: str, context: Mapping[str, str]) -> str:
    """Format mutually_exclusive violation message."""
    return template.format(
        field1=context.get("field1", "unknown"),
        field2=context.get("field2", "unknown"),
    )


def format_required_if_other(template: str, context: Mapping[str, str]) -> str:
    """Format required_if_other violation message."""
    return template.format(
        required_field=context.get("required_field", "unknown"),
        other_field=context.get("other_field", "unknown"),
    )


def format_schema_version_mismatch(template: str, context: Mapping[str, str]) -> str:
    """Format schema_version_mismatch violation message."""
    return template.format(
        expected_version=context.get("expected_version", "unknown"),
        actual_version=context.get("actual_version", "unknown"),
    )


def format_deprecated_field(template: str, context: Mapping[str, str]) -> str:
    """Format deprecated_field violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        replacement_field=context.get("replacement_field", "unknown"),
    )


def format_permission_denied(template: str, context: Mapping[str, str]) -> str:
    """Format permission_denied violation message."""
    description = context.get("description", "Access denied.")
    return template.format(description=description)


def format_rate_limit_exceeded(template: str, context: Mapping[str, str]) -> str:
    """Format rate_limit_exceeded violation message."""
    return template.format(
        operation=context.get("operation", "unknown"),
        limit=context.get("limit", "unknown"),
        period=context.get("period", "unknown"),
    )


# Additional formatters for missing ejection reasons
def format_value_not_in_allowed(template: str, context: Mapping[str, str]) -> str:
    """Format value_not_in_allowed violation message."""
    allowed_values = context.get("allowed_values", [])
    if isinstance(allowed_values, (list, tuple, set)):
        sorted_values = sorted(str(v) for v in allowed_values)
        allowed_str = ", ".join(sorted_values)
    else:
        allowed_str = str(allowed_values)
    return template.format(
        field_name=context.get("field_name", "unknown"),
        value=context.get("value", "unknown"),
        allowed_values=allowed_str,
    )


def format_number_precision_violation(template: str, context: Mapping[str, str]) -> str:
    """Format number_precision_violation violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        max_precision=context.get("max_precision", "unknown"),
        actual_precision=context.get("actual_precision", "unknown"),
    )


def format_nested_structure_invalid(template: str, context: Mapping[str, str]) -> str:
    """Format nested_structure_invalid violation message."""
    description = context.get("description", "Structure does not match schema.")
    return template.format(
        field_path=context.get("field_path", "unknown"),
        description=description,
    )


def format_array_constraint_violation(template: str, context: Mapping[str, str]) -> str:
    """Format array_constraint_violation violation message."""
    description = context.get("description", "Array violates constraints.")
    return template.format(
        field_name=context.get("field_name", "unknown"),
        description=description,
    )


def format_revenue_no_currency(template: str, context: Mapping[str, str]) -> str:
    """Format revenue_no_currency violation message."""
    return template.format(
        revenue_field=context.get("revenue_field", "revenue"),
        currency_field=context.get("currency_field", "currency"),
    )


def format_circular_reference(template: str, context: Mapping[str, str]) -> str:
    """Format circular_reference violation message."""
    description = context.get("description", "Circular reference detected.")
    return template.format(description=description)


def format_invalid_business_logic(template: str, context: Mapping[str, str]) -> str:
    """Format invalid_business_logic violation message."""
    description = context.get("description", "Business logic constraint violated.")
    return template.format(description=description)


def format_cross_field_validation_failed(template: str, context: Mapping[str, str]) -> str:
    """Format cross_field_validation_failed violation message."""
    description = context.get("description", "Cross-field validation failed.")
    return template.format(description=description)


def format_field_not_allowed_in_version(template: str, context: Mapping[str, str]) -> str:
    """Format field_not_allowed_in_version violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        schema_version=context.get("schema_version", "unknown"),
        min_version=context.get("min_version", "unknown"),
    )


def format_migration_downgrade_blocked(template: str, context: Mapping[str, str]) -> str:
    """Format migration_downgrade_blocked violation message."""
    return template.format(
        from_version=context.get("from_version", "unknown"),
        to_version=context.get("to_version", "unknown"),
    )


def format_version_not_supported(template: str, context: Mapping[str, str]) -> str:
    """Format version_not_supported violation message."""
    supported_versions = context.get("supported_versions", [])
    if isinstance(supported_versions, (list, tuple, set)):
        sorted_versions = sorted(str(v) for v in supported_versions)
        versions_str = ", ".join(sorted_versions)
    else:
        versions_str = str(supported_versions)
    return template.format(
        version=context.get("version", "unknown"),
        supported_versions=versions_str,
    )


def format_breaking_change_detected(template: str, context: Mapping[str, str]) -> str:
    """Format breaking_change_detected violation message."""
    description = context.get("description", "Breaking change detected.")
    return template.format(description=description)


def format_future_version_field(template: str, context: Mapping[str, str]) -> str:
    """Format future_version_field violation message."""
    return template.format(
        field_name=context.get("field_name", "unknown"),
        future_version=context.get("future_version", "unknown"),
        current_version=context.get("current_version", "unknown"),
    )


def format_time_dependent_branch(template: str, context: Mapping[str, str]) -> str:
    """Format time_dependent_branch violation message."""
    description = context.get("description", "Time-dependent logic detected.")
    return template.format(description=description)


def format_non_stable_iteration(template: str, context: Mapping[str, str]) -> str:
    """Format non_stable_iteration violation message."""
    description = context.get("description", "Non-deterministic iteration detected.")
    return template.format(description=description)


def format_random_value_used(template: str, context: Mapping[str, str]) -> str:
    """Format random_value_used violation message."""
    description = context.get("description", "Random value usage detected.")
    return template.format(description=description)


def format_external_state_dependency(template: str, context: Mapping[str, str]) -> str:
    """Format external_state_dependency violation message."""
    description = context.get("description", "External state dependency detected.")
    return template.format(description=description)


def format_non_deterministic_hash(template: str, context: Mapping[str, str]) -> str:
    """Format non_deterministic_hash violation message."""
    description = context.get("description", "Non-deterministic hash function detected.")
    return template.format(description=description)


def format_governance_lock_violation(template: str, context: Mapping[str, str]) -> str:
    """Format governance_lock_violation violation message."""
    description = context.get("description", "Governance lock violation detected.")
    return template.format(description=description)


def format_invalid_operation(template: str, context: Mapping[str, str]) -> str:
    """Format invalid_operation violation message."""
    description = context.get("description", "Invalid operation attempted.")
    return template.format(description=description)


def format_quota_exceeded(template: str, context: Mapping[str, str]) -> str:
    """Format quota_exceeded violation message."""
    return template.format(
        operation=context.get("operation", "unknown"),
        limit=context.get("limit", "unknown"),
        unit=context.get("unit", "unknown"),
        current_usage=context.get("current_usage", "unknown"),
    )


# ============================================================================
# Explainable Reason Definitions
# ============================================================================

# Precompute template hash (Tier-0: frozen constant)
_SEMANTIC_END_BEFORE_START_TEMPLATES, _SEMANTIC_END_BEFORE_START_HASH = create_template_mapping(
    default_template=TEMPLATE_END_BEFORE_START,
    remediation_action="Ensure that end_time is strictly greater than start_time.",
    reason_code=SEMANTIC_END_BEFORE_START.code,
)

SEMANTIC_END_BEFORE_START_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_END_BEFORE_START,
    templates=_SEMANTIC_END_BEFORE_START_TEMPLATES,
    remediation=Remediation(
        action="Ensure that end_time is strictly greater than start_time.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_end_before_start,
    template_hash=_SEMANTIC_END_BEFORE_START_HASH,
)

_SCHEMA_MISSING_REQUIRED_TEMPLATES, _SCHEMA_MISSING_REQUIRED_HASH = create_template_mapping(
    default_template=TEMPLATE_MISSING_REQUIRED,
    remediation_action="Add the required field to the input data.",
    reason_code=SCHEMA_MISSING_REQUIRED.code,
)

SCHEMA_MISSING_REQUIRED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_MISSING_REQUIRED,
    templates=_SCHEMA_MISSING_REQUIRED_TEMPLATES,
    remediation=Remediation(
        action="Add the required field to the input data.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_missing_required,
    template_hash=_SCHEMA_MISSING_REQUIRED_HASH,
)

_SCHEMA_INVALID_TYPE_TEMPLATES, _SCHEMA_INVALID_TYPE_HASH = create_template_mapping(
    default_template=TEMPLATE_INVALID_TYPE,
    remediation_action="Fix the field type to match the schema definition.",
    reason_code=SCHEMA_INVALID_TYPE.code,
)

SCHEMA_INVALID_TYPE_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_INVALID_TYPE,
    templates=_SCHEMA_INVALID_TYPE_TEMPLATES,
    remediation=Remediation(
        action="Fix the field type to match the schema definition.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_invalid_type,
    template_hash=_SCHEMA_INVALID_TYPE_HASH,
)

_SCHEMA_FIELD_VALUE_OUT_OF_RANGE_TEMPLATES, _SCHEMA_FIELD_VALUE_OUT_OF_RANGE_HASH = create_template_mapping(
    default_template=TEMPLATE_VALUE_OUT_OF_RANGE,
    remediation_action="Adjust the field value to be within the allowed range.",
    reason_code=SCHEMA_FIELD_VALUE_OUT_OF_RANGE.code,
)

SCHEMA_FIELD_VALUE_OUT_OF_RANGE_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_FIELD_VALUE_OUT_OF_RANGE,
    templates=_SCHEMA_FIELD_VALUE_OUT_OF_RANGE_TEMPLATES,
    remediation=Remediation(
        action="Adjust the field value to be within the allowed range.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_value_out_of_range,
    template_hash=_SCHEMA_FIELD_VALUE_OUT_OF_RANGE_HASH,
)

_SCHEMA_STRING_LENGTH_VIOLATION_TEMPLATES, _SCHEMA_STRING_LENGTH_VIOLATION_HASH = create_template_mapping(
    default_template=TEMPLATE_STRING_LENGTH_VIOLATION,
    remediation_action="Adjust the string length to be within the allowed constraints.",
    reason_code=SCHEMA_STRING_LENGTH_VIOLATION.code,
)

SCHEMA_STRING_LENGTH_VIOLATION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_STRING_LENGTH_VIOLATION,
    templates=_SCHEMA_STRING_LENGTH_VIOLATION_TEMPLATES,
    remediation=Remediation(
        action="Adjust the string length to be within the allowed constraints.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_string_length_violation,
    template_hash=_SCHEMA_STRING_LENGTH_VIOLATION_HASH,
)

_SCHEMA_INVALID_ENUM_VALUE_TEMPLATES, _SCHEMA_INVALID_ENUM_VALUE_HASH = create_template_mapping(
    default_template=TEMPLATE_INVALID_ENUM_VALUE,
    remediation_action="Use one of the allowed enum values for this field.",
    reason_code=SCHEMA_INVALID_ENUM_VALUE.code,
)

SCHEMA_INVALID_ENUM_VALUE_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_INVALID_ENUM_VALUE,
    templates=_SCHEMA_INVALID_ENUM_VALUE_TEMPLATES,
    remediation=Remediation(
        action="Use one of the allowed enum values for this field.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_invalid_enum_value,
    template_hash=_SCHEMA_INVALID_ENUM_VALUE_HASH,
)

_SCHEMA_INVALID_FORMAT_TEMPLATES, _SCHEMA_INVALID_FORMAT_HASH = create_template_mapping(
    default_template=TEMPLATE_INVALID_FORMAT,
    remediation_action="Fix the field format to match the required pattern.",
    reason_code=SCHEMA_INVALID_FORMAT.code,
)

SCHEMA_INVALID_FORMAT_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_INVALID_FORMAT,
    templates=_SCHEMA_INVALID_FORMAT_TEMPLATES,
    remediation=Remediation(
        action="Fix the field format to match the required pattern.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_invalid_format,
    template_hash=_SCHEMA_INVALID_FORMAT_HASH,
)

_SEMANTIC_INVALID_DATE_RANGE_TEMPLATES, _SEMANTIC_INVALID_DATE_RANGE_HASH = create_template_mapping(
    default_template=TEMPLATE_INVALID_DATE_RANGE,
    remediation_action="Fix the date range to be valid and consistent.",
    reason_code=SEMANTIC_INVALID_DATE_RANGE.code,
)

SEMANTIC_INVALID_DATE_RANGE_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_INVALID_DATE_RANGE,
    templates=_SEMANTIC_INVALID_DATE_RANGE_TEMPLATES,
    remediation=Remediation(
        action="Fix the date range to be valid and consistent.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_invalid_date_range,
    template_hash=_SEMANTIC_INVALID_DATE_RANGE_HASH,
)

_SEMANTIC_MUTUALLY_EXCLUSIVE_TEMPLATES, _SEMANTIC_MUTUALLY_EXCLUSIVE_HASH = create_template_mapping(
    default_template=TEMPLATE_MUTUALLY_EXCLUSIVE,
    remediation_action="Remove one of the mutually exclusive fields.",
    reason_code=SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS.code,
)

SEMANTIC_MUTUALLY_EXCLUSIVE_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS,
    templates=_SEMANTIC_MUTUALLY_EXCLUSIVE_TEMPLATES,
    remediation=Remediation(
        action="Remove one of the mutually exclusive fields.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_mutually_exclusive,
    template_hash=_SEMANTIC_MUTUALLY_EXCLUSIVE_HASH,
)

_SEMANTIC_REQUIRED_IF_OTHER_TEMPLATES, _SEMANTIC_REQUIRED_IF_OTHER_HASH = create_template_mapping(
    default_template=TEMPLATE_REQUIRED_IF_OTHER,
    remediation_action="Add the required field when the other field is present.",
    reason_code=SEMANTIC_REQUIRED_IF_OTHER_PRESENT.code,
)

SEMANTIC_REQUIRED_IF_OTHER_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_REQUIRED_IF_OTHER_PRESENT,
    templates=_SEMANTIC_REQUIRED_IF_OTHER_TEMPLATES,
    remediation=Remediation(
        action="Add the required field when the other field is present.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_required_if_other,
    template_hash=_SEMANTIC_REQUIRED_IF_OTHER_HASH,
)

_COMPAT_SCHEMA_VERSION_MISMATCH_TEMPLATES, _COMPAT_SCHEMA_VERSION_MISMATCH_HASH = create_template_mapping(
    default_template=TEMPLATE_SCHEMA_VERSION_MISMATCH,
    remediation_action="Use the correct schema version or migrate the data.",
    reason_code=COMPAT_SCHEMA_VERSION_MISMATCH.code,
)

COMPAT_SCHEMA_VERSION_MISMATCH_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_SCHEMA_VERSION_MISMATCH,
    templates=_COMPAT_SCHEMA_VERSION_MISMATCH_TEMPLATES,
    remediation=Remediation(
        action="Use the correct schema version or migrate the data.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_schema_version_mismatch,
    template_hash=_COMPAT_SCHEMA_VERSION_MISMATCH_HASH,
)

_COMPAT_DEPRECATED_FIELD_TEMPLATES, _COMPAT_DEPRECATED_FIELD_HASH = create_template_mapping(
    default_template=TEMPLATE_DEPRECATED_FIELD,
    remediation_action="Replace the deprecated field with the recommended replacement.",
    reason_code=COMPAT_DEPRECATED_FIELD_USED.code,
)

COMPAT_DEPRECATED_FIELD_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_DEPRECATED_FIELD_USED,
    templates=_COMPAT_DEPRECATED_FIELD_TEMPLATES,
    remediation=Remediation(
        action="Replace the deprecated field with the recommended replacement.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_deprecated_field,
    template_hash=_COMPAT_DEPRECATED_FIELD_HASH,
)

_GOVERNANCE_PERMISSION_DENIED_TEMPLATES, _GOVERNANCE_PERMISSION_DENIED_HASH = create_template_mapping(
    default_template=TEMPLATE_PERMISSION_DENIED,
    remediation_action="Request appropriate permissions or use a different account.",
    reason_code=GOVERNANCE_PERMISSION_DENIED.code,
)

GOVERNANCE_PERMISSION_DENIED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=GOVERNANCE_PERMISSION_DENIED,
    templates=_GOVERNANCE_PERMISSION_DENIED_TEMPLATES,
    remediation=Remediation(
        action="Request appropriate permissions or use a different account.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_permission_denied,
    template_hash=_GOVERNANCE_PERMISSION_DENIED_HASH,
)

GOVERNANCE_RATE_LIMIT_EXCEEDED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=GOVERNANCE_RATE_LIMIT_EXCEEDED,
    template=TEMPLATE_RATE_LIMIT_EXCEEDED,
    remediation=Remediation(
        action="Wait for the rate limit period to expire or reduce request frequency.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_rate_limit_exceeded,
)

# Additional explainable reasons for missing ejection reasons
SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED,
    template=TEMPLATE_VALUE_NOT_IN_ALLOWED,
    remediation=Remediation(
        action="Use one of the allowed values for this field.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_value_not_in_allowed,
)

SCHEMA_NUMBER_PRECISION_VIOLATION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_NUMBER_PRECISION_VIOLATION,
    template=TEMPLATE_NUMBER_PRECISION_VIOLATION,
    remediation=Remediation(
        action="Reduce the number precision to match the allowed decimal places.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_number_precision_violation,
)

SCHEMA_NESTED_STRUCTURE_INVALID_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_NESTED_STRUCTURE_INVALID,
    template=TEMPLATE_NESTED_STRUCTURE_INVALID,
    remediation=Remediation(
        action="Fix the nested structure to match the schema definition.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_nested_structure_invalid,
)

SCHEMA_ARRAY_CONSTRAINT_VIOLATION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SCHEMA_ARRAY_CONSTRAINT_VIOLATION,
    template=TEMPLATE_ARRAY_CONSTRAINT_VIOLATION,
    remediation=Remediation(
        action="Fix the array to meet length or element constraints.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_array_constraint_violation,
)

SEMANTIC_REVENUE_NO_CURRENCY_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_REVENUE_NO_CURRENCY,
    template=TEMPLATE_REVENUE_NO_CURRENCY,
    remediation=Remediation(
        action="Add currency specification when providing revenue values.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_revenue_no_currency,
)

SEMANTIC_CIRCULAR_REFERENCE_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_CIRCULAR_REFERENCE,
    template=TEMPLATE_CIRCULAR_REFERENCE,
    remediation=Remediation(
        action="Remove the circular reference from the data structure.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_circular_reference,
)

SEMANTIC_INVALID_BUSINESS_LOGIC_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_INVALID_BUSINESS_LOGIC,
    template=TEMPLATE_INVALID_BUSINESS_LOGIC,
    remediation=Remediation(
        action="Fix the data to comply with business logic constraints.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_invalid_business_logic,
)

SEMANTIC_CROSS_FIELD_VALIDATION_FAILED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=SEMANTIC_CROSS_FIELD_VALIDATION_FAILED,
    template=TEMPLATE_CROSS_FIELD_VALIDATION_FAILED,
    remediation=Remediation(
        action="Fix the cross-field relationships to meet validation requirements.",
        fix_type=FixType.DATA_FIX,
    ),
    formatter=format_cross_field_validation_failed,
)

COMPAT_FIELD_NOT_ALLOWED_IN_VERSION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_FIELD_NOT_ALLOWED_IN_VERSION,
    template=TEMPLATE_FIELD_NOT_ALLOWED_IN_VERSION,
    remediation=Remediation(
        action="Remove the field or upgrade to a schema version that supports it.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_field_not_allowed_in_version,
)

COMPAT_MIGRATION_DOWNGRADE_BLOCKED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_MIGRATION_DOWNGRADE_BLOCKED,
    template=TEMPLATE_MIGRATION_DOWNGRADE_BLOCKED,
    remediation=Remediation(
        action="Do not attempt to downgrade schema versions. Use forward migration only.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_migration_downgrade_blocked,
)

COMPAT_VERSION_NOT_SUPPORTED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_VERSION_NOT_SUPPORTED,
    template=TEMPLATE_VERSION_NOT_SUPPORTED,
    remediation=Remediation(
        action="Use a supported schema version or upgrade the system.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_version_not_supported,
)

COMPAT_BREAKING_CHANGE_DETECTED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_BREAKING_CHANGE_DETECTED,
    template=TEMPLATE_BREAKING_CHANGE_DETECTED,
    remediation=Remediation(
        action="Review and fix the breaking change in the schema migration plan.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_breaking_change_detected,
)

COMPAT_FUTURE_VERSION_FIELD_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=COMPAT_FUTURE_VERSION_FIELD,
    template=TEMPLATE_FUTURE_VERSION_FIELD,
    remediation=Remediation(
        action="Remove the future version field or upgrade the system to support it.",
        fix_type=FixType.MIGRATION_REQUIRED,
    ),
    formatter=format_future_version_field,
)

DETERMINISM_TIME_DEPENDENT_BRANCH_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=DETERMINISM_TIME_DEPENDENT_BRANCH,
    template=TEMPLATE_TIME_DEPENDENT_BRANCH,
    remediation=Remediation(
        action="Remove time-dependent logic from validation to ensure determinism.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_time_dependent_branch,
)

DETERMINISM_NON_STABLE_ITERATION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=DETERMINISM_NON_STABLE_ITERATION,
    template=TEMPLATE_NON_STABLE_ITERATION,
    remediation=Remediation(
        action="Use deterministic iteration order in validation logic.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_non_stable_iteration,
)

DETERMINISM_RANDOM_VALUE_USED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=DETERMINISM_RANDOM_VALUE_USED,
    template=TEMPLATE_RANDOM_VALUE_USED,
    remediation=Remediation(
        action="Remove random value usage from validation to ensure determinism.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_random_value_used,
)

DETERMINISM_EXTERNAL_STATE_DEPENDENCY_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=DETERMINISM_EXTERNAL_STATE_DEPENDENCY,
    template=TEMPLATE_EXTERNAL_STATE_DEPENDENCY,
    remediation=Remediation(
        action="Remove external state dependencies from validation logic.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_external_state_dependency,
)

DETERMINISM_NON_DETERMINISTIC_HASH_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=DETERMINISM_NON_DETERMINISTIC_HASH,
    template=TEMPLATE_NON_DETERMINISTIC_HASH,
    remediation=Remediation(
        action="Use deterministic hash functions in validation logic.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_non_deterministic_hash,
)

GOVERNANCE_LOCK_VIOLATION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=GOVERNANCE_LOCK_VIOLATION,
    template=TEMPLATE_GOVERNANCE_LOCK_VIOLATION,
    remediation=Remediation(
        action="Resolve the governance lock violation before proceeding.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_governance_lock_violation,
)

GOVERNANCE_INVALID_OPERATION_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=GOVERNANCE_INVALID_OPERATION,
    template=TEMPLATE_INVALID_OPERATION,
    remediation=Remediation(
        action="Use a valid governance operation for this context.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_invalid_operation,
)

GOVERNANCE_QUOTA_EXCEEDED_EXPLAINABLE: Final[ExplainableReason] = ExplainableReason(
    reason=GOVERNANCE_QUOTA_EXCEEDED,
    template=TEMPLATE_QUOTA_EXCEEDED,
    remediation=Remediation(
        action="Wait for quota reset or reduce usage to stay within limits.",
        fix_type=FixType.CONFIG_CHANGE,
    ),
    formatter=format_quota_exceeded,
)


# ============================================================================
# Central Explainable Registry
# ============================================================================

_EXPLAINABLE_REGISTRY_DICT: Dict[str, ExplainableReason] = {
    # Schema reasons
    SCHEMA_INVALID_TYPE.code: SCHEMA_INVALID_TYPE_EXPLAINABLE,
    SCHEMA_MISSING_REQUIRED.code: SCHEMA_MISSING_REQUIRED_EXPLAINABLE,
    SCHEMA_FIELD_VALUE_OUT_OF_RANGE.code: SCHEMA_FIELD_VALUE_OUT_OF_RANGE_EXPLAINABLE,
    SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED.code: SCHEMA_FIELD_VALUE_NOT_IN_ALLOWED_EXPLAINABLE,
    SCHEMA_STRING_LENGTH_VIOLATION.code: SCHEMA_STRING_LENGTH_VIOLATION_EXPLAINABLE,
    SCHEMA_NUMBER_PRECISION_VIOLATION.code: SCHEMA_NUMBER_PRECISION_VIOLATION_EXPLAINABLE,
    SCHEMA_INVALID_ENUM_VALUE.code: SCHEMA_INVALID_ENUM_VALUE_EXPLAINABLE,
    SCHEMA_INVALID_FORMAT.code: SCHEMA_INVALID_FORMAT_EXPLAINABLE,
    SCHEMA_NESTED_STRUCTURE_INVALID.code: SCHEMA_NESTED_STRUCTURE_INVALID_EXPLAINABLE,
    SCHEMA_ARRAY_CONSTRAINT_VIOLATION.code: SCHEMA_ARRAY_CONSTRAINT_VIOLATION_EXPLAINABLE,
    # Semantic reasons
    SEMANTIC_END_BEFORE_START.code: SEMANTIC_END_BEFORE_START_EXPLAINABLE,
    SEMANTIC_REVENUE_NO_CURRENCY.code: SEMANTIC_REVENUE_NO_CURRENCY_EXPLAINABLE,
    SEMANTIC_INVALID_DATE_RANGE.code: SEMANTIC_INVALID_DATE_RANGE_EXPLAINABLE,
    SEMANTIC_CIRCULAR_REFERENCE.code: SEMANTIC_CIRCULAR_REFERENCE_EXPLAINABLE,
    SEMANTIC_MUTUALLY_EXCLUSIVE_FIELDS.code: SEMANTIC_MUTUALLY_EXCLUSIVE_EXPLAINABLE,
    SEMANTIC_REQUIRED_IF_OTHER_PRESENT.code: SEMANTIC_REQUIRED_IF_OTHER_EXPLAINABLE,
    SEMANTIC_INVALID_BUSINESS_LOGIC.code: SEMANTIC_INVALID_BUSINESS_LOGIC_EXPLAINABLE,
    SEMANTIC_CROSS_FIELD_VALIDATION_FAILED.code: SEMANTIC_CROSS_FIELD_VALIDATION_FAILED_EXPLAINABLE,
    # Compatibility reasons
    COMPAT_FIELD_NOT_ALLOWED_IN_VERSION.code: COMPAT_FIELD_NOT_ALLOWED_IN_VERSION_EXPLAINABLE,
    COMPAT_MIGRATION_DOWNGRADE_BLOCKED.code: COMPAT_MIGRATION_DOWNGRADE_BLOCKED_EXPLAINABLE,
    COMPAT_VERSION_NOT_SUPPORTED.code: COMPAT_VERSION_NOT_SUPPORTED_EXPLAINABLE,
    COMPAT_BREAKING_CHANGE_DETECTED.code: COMPAT_BREAKING_CHANGE_DETECTED_EXPLAINABLE,
    COMPAT_SCHEMA_VERSION_MISMATCH.code: COMPAT_SCHEMA_VERSION_MISMATCH_EXPLAINABLE,
    COMPAT_DEPRECATED_FIELD_USED.code: COMPAT_DEPRECATED_FIELD_EXPLAINABLE,
    COMPAT_FUTURE_VERSION_FIELD.code: COMPAT_FUTURE_VERSION_FIELD_EXPLAINABLE,
    # Determinism reasons
    DETERMINISM_TIME_DEPENDENT_BRANCH.code: DETERMINISM_TIME_DEPENDENT_BRANCH_EXPLAINABLE,
    DETERMINISM_NON_STABLE_ITERATION.code: DETERMINISM_NON_STABLE_ITERATION_EXPLAINABLE,
    DETERMINISM_RANDOM_VALUE_USED.code: DETERMINISM_RANDOM_VALUE_USED_EXPLAINABLE,
    DETERMINISM_EXTERNAL_STATE_DEPENDENCY.code: DETERMINISM_EXTERNAL_STATE_DEPENDENCY_EXPLAINABLE,
    DETERMINISM_NON_DETERMINISTIC_HASH.code: DETERMINISM_NON_DETERMINISTIC_HASH_EXPLAINABLE,
    # Governance reasons
    GOVERNANCE_LOCK_VIOLATION.code: GOVERNANCE_LOCK_VIOLATION_EXPLAINABLE,
    GOVERNANCE_PERMISSION_DENIED.code: GOVERNANCE_PERMISSION_DENIED_EXPLAINABLE,
    GOVERNANCE_INVALID_OPERATION.code: GOVERNANCE_INVALID_OPERATION_EXPLAINABLE,
    GOVERNANCE_RATE_LIMIT_EXCEEDED.code: GOVERNANCE_RATE_LIMIT_EXCEEDED_EXPLAINABLE,
    GOVERNANCE_QUOTA_EXCEEDED.code: GOVERNANCE_QUOTA_EXCEEDED_EXPLAINABLE,
}

# Tier-0: Registry must be immutable (MappingProxyType prevents runtime mutation)
EXPLAINABLE_REGISTRY: Final[Mapping[str, ExplainableReason]] = MappingProxyType(_EXPLAINABLE_REGISTRY_DICT)

# Tier-0 Requirement: Registry must be complete.
# validate_explainable_registry() is called at import time to enforce this.
# If registry is incomplete, system startup will fail with AssertionError.
# All canonical EjectionReason codes must have exactly one explainable mapping.


# ============================================================================
# Registry Validation
# ============================================================================

def validate_explainable_registry() -> None:
    """
    Validate that explainable registry has complete coverage (Tier-0 enforcement).
    
    Enforces:
    - Every canonical EjectionReason must have exactly one explainable mapping
    - No orphan mappings (all registry entries must reference valid reasons)
    - No duplication (one-to-one mapping)
    - Exact equality: reason_codes == explainable_codes (strict set equality)
    
    Raises:
        AssertionError: If registry integrity is violated
        
    Note:
        This is called at import time to prevent system startup with invalid state.
        Tier-0 requirement: registry completeness is non-negotiable.
    """
    reason_codes = {r.code for r in ALL_EJECTION_REASONS}
    explainable_codes = set(EXPLAINABLE_REGISTRY.keys())
    
    # Tier-0: Strict equality check (reason_codes == explainable_codes)
    if reason_codes != explainable_codes:
        missing = reason_codes - explainable_codes
        orphans = explainable_codes - reason_codes
        
        error_parts = []
        if missing:
            error_parts.append(
                f"Missing explainable mappings for {len(missing)} reasons: "
                f"{sorted(missing)[:10]}"  # Show first 10
            )
        if orphans:
            error_parts.append(
                f"Orphan explainable mappings (not in canonical registry): {sorted(orphans)[:10]}"
            )
        
        raise AssertionError(
            f"Registry equality violation (Tier-0): reason_codes != explainable_codes. "
            f"Reason codes: {len(reason_codes)}, Explainable codes: {len(explainable_codes)}. "
            + "; ".join(error_parts)
        )
    
    # Verify one-to-one mapping (no duplicates in registry)
    if len(explainable_codes) != len(EXPLAINABLE_REGISTRY):
        raise AssertionError(
            f"Duplicate explainable mappings detected. "
            f"Expected {len(explainable_codes)} unique codes, "
            f"got {len(EXPLAINABLE_REGISTRY)} entries."
        )
    
    # Verify all registry entries reference correct reasons
    for code, explainable in EXPLAINABLE_REGISTRY.items():
        if explainable.reason.code != code:
            raise AssertionError(
                f"Registry key mismatch: key={code}, "
                f"explainable.reason.code={explainable.reason.code}"
            )


def get_explainable_reason(reason_code: str) -> ExplainableReason:
    """
    Get explainable reason by canonical code.
    
    Args:
        reason_code: Canonical EjectionReason code
        
    Returns:
        ExplainableReason for the code
        
    Raises:
        KeyError: If code not found in registry
    """
    if reason_code not in EXPLAINABLE_REGISTRY:
        raise KeyError(
            f"Explainable reason not found for code: {reason_code}. "
            f"This indicates incomplete registry coverage."
        )
    return EXPLAINABLE_REGISTRY[reason_code]


# ============================================================================
# Template Hash Computation and Validation (Tier-0 Drift Detection)
# ============================================================================

def compute_semantic_hash(
    template: str,
    reason_code: str,
) -> str:
    """
    Compute semantic hash of template + reason code (excludes remediation).
    
    Semantic hash captures only the semantic intent (template + reason_code),
    not presentation metadata (remediation_action). This allows remediation
    wording changes without breaking semantic audit reproducibility.
    
    Args:
        template: Message template string
        reason_code: Canonical EjectionReason code
        
    Returns:
        SHA-256 hex digest (64 characters)
        
    Note:
        - Same inputs always produce same hash
        - Template edits change hash (enables drift detection)
        - Remediation changes do NOT affect semantic hash
        - Used for long-term semantic audit reproducibility
    """
    hash_input = (
        f"hash_schema_version={SEMANTIC_HASH_SCHEMA_VERSION},"
        f"template={template},"
        f"reason_code={reason_code}"
    )
    return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()


def compute_template_hash(
    template: str,
    remediation_action: str,
    reason_code: str,
) -> str:
    """
    Compute display hash of template + remediation + reason code.
    
    This enables detection of silent template edits that break audit
    reproducibility across versions. Template changes must be explicit
    and versioned.
    
    Args:
        template: Message template string
        remediation_action: Remediation action string (presentation metadata)
        reason_code: Canonical EjectionReason code
        
    Returns:
        SHA-256 hex digest (64 characters)
        
    Note:
        - Same inputs always produce same hash
        - Template edits change hash (enables drift detection)
        - Remediation edits also change hash (display-level change)
        - Used for complete display reproducibility
        - For semantic-only reproducibility, use compute_semantic_hash()
    """
    hash_input = (
        f"hash_schema_version={HASH_SCHEMA_VERSION},"
        f"template={template},"
        f"remediation_action={remediation_action},"
        f"reason_code={reason_code}"
    )
    return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()


def validate_template_hashes() -> None:
    """
    Validate that all explainable reasons have correct template hashes (Tier-0).
    
    This enforces that precomputed template hashes match computed values,
    preventing silent template edits that break audit reproducibility.
    
    Raises:
        AssertionError: If any template hash mismatch is detected
        
    Note:
        This is called at import time to prevent system startup with
        corrupted template hashes. Template hashes are frozen constants
        that must match computed values exactly.
    """
    mismatches: list[str] = []
    
    for code, explainable in EXPLAINABLE_REGISTRY.items():
        # Use default locale template for hash computation
        default_template = explainable.get_template(DEFAULT_LOCALE)
        computed_hash = compute_template_hash(
            default_template,
            explainable.remediation.action,
            explainable.reason.code,
        )
        
        if explainable.template_hash != computed_hash:
            mismatches.append(
                f"Template hash mismatch for {code}: "
                f"stored={explainable.template_hash[:16]}..., "
                f"computed={computed_hash[:16]}... "
                f"This indicates template drift or incorrect precomputed hash."
            )
    
    if mismatches:
        raise AssertionError(
            f"Template hash validation failed for {len(mismatches)} reasons (Tier-0):\n"
            + "\n".join(mismatches[:10])  # Show first 10
        )


# ============================================================================
# Rendering Functions (Deterministic)
# ============================================================================

def render_violation(
    v: ValidationViolation,
    locale: str = DEFAULT_LOCALE,
) -> OrderedDict[str, Any]:
    """
    Render violation to human-readable structured dictionary (Tier-0).
    
    This produces a structured JSON representation that includes:
    - Machine-readable code (for systems)
    - Human-readable message (for humans, locale-aware)
    - Structured remediation (for automation)
    - Severity and field path (for filtering)
    - Raw context (for audit provenance)
    
    Args:
        v: ValidationViolation to render
        locale: Locale identifier (default: DEFAULT_LOCALE)
        
    Returns:
        OrderedDict with canonical key ordering for deterministic serialization.
        Keys: code, message, remediation, severity, field_path, schema_version,
        locale, raw_context, normalized_context
        
    Note:
        - Output uses OrderedDict for explicit deterministic key ordering
        - Message is deterministic (same context → same message)
        - Context is normalized before formatting (eliminates runtime repr variance)
        - Raw context preserved for audit provenance (Tier-0 requirement)
        - Locale must be explicitly passed (never inferred from runtime)
        - Never mutates the violation
    """
    explainable = get_explainable_reason(v.reason.code)
    
    # Tier-0: Normalize context before formatting (deterministic serialization)
    # Returns (normalized_context, raw_context) for audit provenance
    normalized_context, raw_context = normalize_context(v.context)
    
    # Get template for locale (falls back to default)
    template = explainable.get_template(locale)
    
    # Format with normalized context and locale template (all values are deterministic strings)
    # Tier-0: Use locale template instead of hardcoded constants
    message = explainable.formatter(template, normalized_context)
    
    # Tier-0: Use OrderedDict for explicit deterministic key ordering
    # This ensures canonical key order regardless of Python dict implementation
    return OrderedDict([
        ("code", v.reason.code),
        ("message", message),
        ("remediation", OrderedDict([
            ("action", explainable.remediation.action),
            ("fix_type", explainable.remediation.fix_type.value),
            ("documentation_url", explainable.remediation.documentation_url),
        ])),
        ("severity", v.reason.severity.value),
        ("field_path", v.field_path),
        ("schema_version", v.schema_version),
        ("locale", locale),
        ("raw_context", dict(raw_context)),  # Preserve for audit provenance
        ("normalized_context", dict(normalized_context)),  # For reproducibility verification
    ])


def render_violation_dict(
    reason_code: str,
    field_path: str,
    context: Mapping[str, Any],
    schema_version: int,
    locale: str = DEFAULT_LOCALE,
) -> OrderedDict[str, Any]:
    """
    Render violation from components (Tier-0: normalized context).
    
    Args:
        reason_code: Canonical EjectionReason code
        field_path: Dot-notation path to field
        context: Violation context mapping (will be normalized)
        schema_version: Schema version
        locale: Locale identifier (default: DEFAULT_LOCALE)
        
    Returns:
        OrderedDict with canonical key ordering for deterministic serialization.
        Keys: code, message, remediation, severity, field_path, schema_version,
        locale, raw_context, normalized_context
        
    Note:
        - Context is normalized before formatting (Tier-0 determinism)
        - Raw context preserved for audit provenance
        - This is a convenience function for cases where ValidationViolation
          is not directly available
        - Still deterministic (same inputs → same output)
        - Locale must be explicitly passed
    """
    explainable = get_explainable_reason(reason_code)
    
    # Tier-0: Normalize context before formatting
    # Returns (normalized_context, raw_context) for audit provenance
    normalized_context, raw_context = normalize_context(context)
    
    # Get template for locale
    template = explainable.get_template(locale)
    
    # Format with locale template and normalized context
    message = explainable.formatter(template, normalized_context)
    
    # Get severity from canonical reason
    canonical_reason = get_reason_by_code(reason_code)
    
    # Tier-0: Use OrderedDict for explicit deterministic key ordering
    return OrderedDict([
        ("code", reason_code),
        ("message", message),
        ("remediation", OrderedDict([
            ("action", explainable.remediation.action),
            ("fix_type", explainable.remediation.fix_type.value),
            ("documentation_url", explainable.remediation.documentation_url),
        ])),
        ("severity", canonical_reason.severity.value),
        ("field_path", field_path),
        ("schema_version", schema_version),
        ("locale", locale),
        ("raw_context", dict(raw_context)),  # Preserve for audit provenance
        ("normalized_context", dict(normalized_context)),  # For reproducibility verification
    ])


# ============================================================================
# Explainability Levels (Advanced)
# ============================================================================

def render_violation_with_level(
    v: ValidationViolation,
    level: str = "short",
) -> dict[str, Any]:
    """
    Render violation with different verbosity levels.
    
    Levels:
    - short: Concise message only
    - detailed: Includes context values
    - developer: Includes schema references and technical details
    
    Args:
        v: ValidationViolation to render
        level: Verbosity level (short / detailed / developer)
        
    Returns:
        Dictionary with level-appropriate detail
        
    Note:
        - Mode must be explicitly passed (never inferred from runtime)
        - Still deterministic (same inputs → same output)
    """
    base = render_violation(v)
    
    if level == "short":
        # Minimal output
        return {
            "code": base["code"],
            "message": base["message"],
            "severity": base["severity"],
        }
    elif level == "detailed":
        # Include context values
        return {
            **base,
            "context": dict(v.context),  # Include full context
        }
    elif level == "developer":
        # Include technical details
        return {
            **base,
            "context": dict(v.context),
            "category": v.reason.category.value,
            "determinism_class": v.reason.determinism_class.value if hasattr(v.reason, "determinism_class") else None,
            "recoverable": v.reason.recoverable if hasattr(v.reason, "recoverable") else None,
        }
    else:
        raise ValueError(f"Invalid level: {level}. Must be one of: short, detailed, developer")


# ============================================================================
# Determinism Constraints
# ============================================================================

# This file must:
#
# ✅ Never call external services
# ✅ Never read config
# ✅ Never use locale-specific formatting unless explicitly deterministic
# ✅ Never use f-strings with computed rounding unless deterministic
# ✅ Never alter context
# ✅ Never randomize output
#
# Even ordering of rendered violations must be delegated to error_model.py.
#
# All formatter functions are pure and deterministic:
# - Same context → same output
# - No side effects
# - No external dependencies
# - No runtime entropy


# ============================================================================
# Anti-Patterns (Documentation)
# ============================================================================

# ❌ Embedding exception tracebacks
# ❌ Dynamically constructing reason codes
# ❌ Allowing free-text reasons inside validators
# ❌ Translating via runtime external API
# ❌ Mutating context in formatter
# ⚠️ Sorting in formatters: This is an intentional Tier-0 determinism requirement.
#    Formatters sort collections (e.g., allowed_values, supported_versions) to ensure
#    deterministic output when upstream passes unordered collections (sets, dicts).
#    This is NOT a violation - it's required for audit reproducibility.
#    See format_invalid_enum_value() and format_version_not_supported() for examples.
# ❌ Inferring verbosity level from runtime context
# ❌ Using datetime.now() for message timestamps
# ❌ Generating UUIDs for message IDs


# ============================================================================
# System-Level View
# ============================================================================

# The full stack now looks like this:
#
# Rules → Violation → Bundle → Policy → Audit
#                    ↓
#            Explainable Presentation
#
# The explainable layer is a parallel view, not a mutation.
#
# It ensures:
# - Your system remains introspectable
# - Engineers understand failures
# - Compliance audits are readable
# - Debugging is clean
# - But validation integrity remains untouched


# ============================================================================
# Import-Time Validation (Tier-0 Enforcement)
# ============================================================================

# Operational Resilience: Import-time validation can be deferred for staged deployments.
# Set ENABLE_STRICT_IMPORT_VALIDATION=False to allow progressive evolution.
# When disabled, validation should be performed at application startup, not module import.

ENABLE_STRICT_IMPORT_VALIDATION: Final[bool] = True
"""
Control import-time validation strictness.

When True: Validation failures block module import (Tier-0 strict mode).
When False: Validation is deferred to application startup (operational resilience mode).

Operational considerations:
- Strict mode: Prevents system startup with invalid state (Tier-0 correctness)
- Deferred mode: Allows staged rollouts, partial migrations, blue/green deployments
- Deferred mode requires explicit validation at application startup
"""

def validate_at_startup() -> None:
    """
    Perform all validation checks (for deferred validation mode).
    
    Call this at application startup when ENABLE_STRICT_IMPORT_VALIDATION=False.
    
    Raises:
        AssertionError: If validation fails
    """
    validate_explainable_registry()
    validate_template_hashes()
    validate_locale_fallback_determinism()


def validate_locale_fallback_determinism() -> None:
    """
    Validate that locale fallback is deterministic (Tier-0 requirement).
    
    Ensures that fallback_template == DEFAULT_LOCALE_TEMPLATE for all explainable reasons.
    This prevents localization bugs from silently altering audit text.
    
    Raises:
        AssertionError: If locale fallback is non-deterministic
    """
    for code, explainable in EXPLAINABLE_REGISTRY.items():
        default_template = explainable.get_template(DEFAULT_LOCALE)
        
        # Test fallback with non-existent locale
        fallback_template = explainable.get_template("nonexistent_locale_xyz")
        
        if fallback_template != default_template:
            raise AssertionError(
                f"Locale fallback non-deterministic for {code}: "
                f"fallback={fallback_template[:50]}..., "
                f"default={default_template[:50]}... "
                f"Fallback must equal DEFAULT_LOCALE template."
            )


if ENABLE_STRICT_IMPORT_VALIDATION:
    # Tier-0 Requirement: Registry completeness is enforced at import time.
    # This prevents system startup with incomplete explainable coverage,
    # which would create compliance blind spots and degrade audit readability.
    #
    # If registry is incomplete, system will fail to start with AssertionError.
    # This is intentional - incomplete coverage is a blocking issue.
    validate_explainable_registry()
    
    # Tier-0 Requirement: Template hash integrity is enforced at import time.
    # This prevents silent template edits that break audit reproducibility.
    # Template changes must be explicit and versioned.
    validate_template_hashes()
    
    # Tier-0 Requirement: Locale fallback determinism is enforced at import time.
    # This prevents localization bugs from silently altering audit text.
    validate_locale_fallback_determinism()
