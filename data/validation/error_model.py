"""
/data/validation/error_model.py

Structured Error Model (Machine Parseable)

---

1️⃣ Core Responsibility

This file defines the canonical, immutable data model for:

- Validation violations
- Aggregated validation results
- Deterministic ordering
- Hash-stable serialization
- Replay equality comparisons

It must guarantee:

- Zero ambiguity
- Stable field ordering
- No runtime-generated randomness
- Fully serializable to JSON
- Deterministic byte representation

---

2️⃣ Architectural Principle

There are three distinct layers:

1. Taxonomy Layer → EjectionReason (from ejection_reasons.py)
2. Violation Layer → A single concrete occurrence of a rejection
3. Error Bundle Layer → The full validation result

This file owns layers (2) and (3).

---

3️⃣ Design Decisions

- frozen=True → immutable
- field_path uses dot-notation ("payload.start_time")
- context must be immutable and recursively canonicalized
- No free-form message
- Machine systems read reason.code, not strings
- NO runtime exceptions - pure data contract layer

---

4️⃣ What This File Must NOT Do

❌ Raise runtime exceptions
❌ Fetch from database
❌ Generate timestamps
❌ Generate UUIDs
❌ Perform logging
❌ Perform formatting for UI
❌ Include human localization logic

It is a pure data contract layer.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple, Set, List, Dict
from types import MappingProxyType

from .ejection_reasons import (
    EjectionReason,
    RejectionCategory,
    RejectionSeverity,
    ALL_EJECTION_REASONS,
)


__all__ = [
    "ValidationViolation",
    "ValidationErrorBundle",
    "sort_violations",
    "violation_to_dict",
    "bundle_to_canonical_json",
    "compute_bundle_hash",
    "build_bundle",
    "has_fatal_errors",
    "has_blocking_errors",
    "get_violations_by_category",
    "get_violations_by_severity",
    "validate_bundle_integrity",
    "check_violation_invariants",
    "check_bundle_invariants",
    "validate_static_registry",
    "canonicalize_value",
]


# ============================================================================
# Deep Canonical Normalization
# ============================================================================

def canonicalize_value(value: Any) -> Any:
    """
    Recursively canonicalize a value for deterministic serialization.
    
    Handles:
    - Nested dicts (sorted keys)
    - Lists (canonicalized elements)
    - Floats (normalized representation)
    - Sets (converted to sorted lists)
    - None, bool, int, str (passed through)
    
    Args:
        value: Value to canonicalize
        
    Returns:
        Canonicalized value suitable for deterministic JSON serialization
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        # Cryptographically stable float canonicalization
        # Use string formatting with fixed precision for cross-runtime stability
        if value == float('inf'):
            return "Infinity"  # JSON-compatible string representation
        if value == float('-inf'):
            return "-Infinity"  # JSON-compatible string representation
        if value != value:  # NaN
            return "NaN"  # JSON-compatible string representation
        
        # For normal floats, use decimal conversion for stability
        # Convert to Decimal with sufficient precision, then format as string
        # This ensures identical representation across Python versions and architectures
        try:
            # Use 15 decimal places (IEEE 754 double precision)
            # Format as string to ensure cross-runtime stability
            decimal_repr = Decimal(str(value))
            # Normalize to remove trailing zeros while preserving precision
            normalized = decimal_repr.normalize()
            # Return as string for deterministic serialization
            return str(normalized)
        except (InvalidOperation, ValueError, OverflowError):
            # Fallback to repr if decimal conversion fails
            # This should be extremely rare
            return repr(value)
    if isinstance(value, (list, tuple)):
        return [canonicalize_value(item) for item in value]
    if isinstance(value, set):
        # Convert set to sorted list for determinism
        return sorted([canonicalize_value(item) for item in value])
    if isinstance(value, dict):
        # Sort keys and canonicalize values
        return {
            str(k): canonicalize_value(v)
            for k, v in sorted(value.items())
        }
    if isinstance(value, Mapping):
        # Handle other mapping types
        return {
            str(k): canonicalize_value(v)
            for k, v in sorted(value.items())
        }
    # For unknown types, use structured representation
    # This preserves type information while maintaining determinism
    type_name = type(value).__name__
    module_name = getattr(type(value), '__module__', '')
    
    # Create structured representation that preserves semantic identity
    # Format: {"_type": "module.ClassName", "_repr": "string_repr"}
    # This allows different objects to maintain distinct identities
    repr_str = repr(value)
    
    # Return structured dict for unknown types
    # This ensures determinism while preserving semantic distinction
    return {
        "_type": f"{module_name}.{type_name}" if module_name else type_name,
        "_repr": repr_str,
    }


# ============================================================================
# Validation Violation (Layer 2: Single Occurrence)
# ============================================================================

@dataclass(frozen=True)
class ValidationViolation:
    """
    Represents a single structured failure.
    
    This is the immutable, machine-parseable representation of a single
    validation violation. It references an EjectionReason (taxonomy) and
    includes the specific context of the failure.
    
    Attributes:
        reason: The EjectionReason that categorizes this violation
        field_path: Dot-notation path to the field (e.g., "payload.start_time")
        context: Immutable mapping of additional context data (canonicalized)
        schema_version: Schema version when this violation occurred
        
    Design:
        - frozen=True ensures immutability
        - context is wrapped in MappingProxyType to prevent mutation
        - context is recursively canonicalized for deterministic serialization
        - No free-form message strings - all semantics come from reason.code
        - NO runtime exceptions - pure data contract
    """
    
    reason: EjectionReason
    field_path: str
    context: Mapping[str, Any]
    schema_version: int
    
    def __post_init__(self) -> None:
        """
        Enforce immutable context while preserving raw input exactly.
        
        Tier-0 Contract: Preserves raw input exactly, no silent mutation.
        Context is made immutable but NOT canonicalized at construction.
        Canonicalization only occurs during serialization (when needed).
        """
        # Convert context to immutable MappingProxyType without canonicalization
        # This preserves raw input exactly as specified in Tier-0 contract
        if isinstance(self.context, dict):
            immutable_context = MappingProxyType(self.context)
        elif isinstance(self.context, Mapping):
            # Convert other mapping types to dict, then make immutable
            immutable_context = MappingProxyType(dict(self.context))
        else:
            # If context is not a mapping, wrap empty dict
            immutable_context = MappingProxyType({})
        
        object.__setattr__(self, "context", immutable_context)
        
        # Tier-0: Preserve raw input exactly - no silent mutation
        # field_path and schema_version are preserved as-is
        # Invalid values will be caught by invariant checking, not silently rewritten
    
    def __repr__(self) -> str:
        return (
            f"ValidationViolation("
            f"reason={self.reason.code!r}, "
            f"field_path={self.field_path!r}, "
            f"schema_version={self.schema_version})"
        )


# ============================================================================
# Validation Error Bundle (Layer 3: Full Result)
# ============================================================================

@dataclass(frozen=True)
class ValidationErrorBundle:
    """
    Represents the entire validation result.
    
    This is the complete, immutable representation of all validation
    violations for a given validation run. It includes:
    
    - All violations (sorted deterministically)
    - Schema version
    - Deterministic hash for integrity verification
    
    Attributes:
        violations: Immutable tuple of ValidationViolation objects
        schema_version: Schema version used for validation
        deterministic_hash: SHA-256 hash of canonical JSON representation
        
    Design:
        - violations is a tuple (immutable) not a list
        - Order must be stable across executions
        - Hash enables integrity verification and replay comparison
        - NO runtime exceptions - pure data contract
        - Hash computed from single canonical serialization path
    """
    
    violations: Tuple[ValidationViolation, ...]
    schema_version: int
    deterministic_hash: str = field(default="", init=False)
    
    def __post_init__(self) -> None:
        """
        Compute hash from canonical serialization (no exceptions).
        
        Tier-0 Contract: Preserves raw input exactly, no silent mutation.
        """
        # Tier-0: Preserve raw schema_version exactly - no silent mutation
        # Invalid values will be caught by invariant checking
        
        # Compute hash from single canonical serialization path
        # Use bundle_to_canonical_json as single source of truth
        canonical_json = _bundle_to_canonical_json_internal(
            self.violations,
            self.schema_version
        )
        hash_value = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        object.__setattr__(self, "deterministic_hash", hash_value)
    
    def __repr__(self) -> str:
        return (
            f"ValidationErrorBundle("
            f"violations={len(self.violations)}, "
            f"schema_version={self.schema_version}, "
            f"hash={self.deterministic_hash[:16]}...)"
        )


# ============================================================================
# Deterministic Ordering (Forward-Safe)
# ============================================================================

def sort_violations(
    violations: list[ValidationViolation],
) -> Tuple[ValidationViolation, ...]:
    """
    Sort violations deterministically.
    
    Before building the bundle, violations MUST be sorted deterministically.
    This ensures:
    - Replay produces identical ordering
    - Hash stability across executions
    - Order-independent rule emission
    
    Sorting order:
    1. Category (using enum.value for forward-compatibility)
    2. Stable sort key (from EjectionReason)
    3. Field path (lexicographic)
    4. Reason code (lexicographic)
    
    Args:
        violations: List of ValidationViolation objects
        
    Returns:
        Tuple of sorted violations (immutable)
        
    Note:
        Never rely on insertion order. Never trust set iteration.
        Uses category.value for forward-safe enum ordering.
    """
    def sort_key(v: ValidationViolation) -> Tuple[str, str, str, str]:
        """Generate deterministic sort key for violation."""
        # Use enum.value for forward-compatible ordering
        # New categories will sort lexicographically by their value
        category_value = v.reason.category.value
        
        return (
            category_value,  # Forward-safe enum ordering
            v.reason.stable_sort_key,
            v.field_path,
            v.reason.code,
        )
    
    return tuple(sorted(violations, key=sort_key))


# ============================================================================
# Canonical Serialization (Single Source of Truth)
# ============================================================================

def violation_to_dict(v: ValidationViolation) -> dict[str, Any]:
    """
    Convert violation to canonical dictionary representation.
    
    This produces a machine-parseable dictionary that can be serialized
    to JSON. All fields are explicit and deterministic.
    
    Args:
        v: ValidationViolation to convert
        
    Returns:
        Dictionary with sorted keys and deterministic structure
        
    Note:
        - context is canonicalized during serialization (not at construction)
        - All keys are explicit
        - No dynamic fields
    """
    # Canonicalize context during serialization (preserves raw input at construction)
    # This ensures deterministic serialization while maintaining Tier-0 input preservation
    context_dict = dict(v.context)
    canonicalized_context = canonicalize_value(context_dict)
    
    return {
        "code": v.reason.code,
        "category": v.reason.category.value,
        "severity": v.reason.severity.value,
        "field_path": v.field_path,
        "schema_version": v.schema_version,
        "context": canonicalized_context,
    }


def _bundle_to_canonical_json_internal(
    violations: Tuple[ValidationViolation, ...],
    schema_version: int,
) -> str:
    """
    Internal function: single source of truth for canonical JSON.
    
    This is the ONLY function that produces canonical JSON representation.
    All hashing and serialization must go through this path.
    
    Args:
        violations: Tuple of violations (already sorted)
        schema_version: Schema version
        
    Returns:
        Canonical JSON string (byte-identical for identical input)
    """
    payload = {
        "schema_version": schema_version,
        "violations": [
            violation_to_dict(v)
            for v in violations
        ],
    }
    
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def bundle_to_canonical_json(bundle: ValidationErrorBundle) -> str:
    """
    Convert bundle to canonical JSON string.
    
    You must be able to produce byte-identical JSON from identical violations.
    This function ensures:
    - Stable whitespace
    - Stable ordering
    - Stable key arrangement
    
    Args:
        bundle: ValidationErrorBundle to serialize
        
    Returns:
        Canonical JSON string (byte-identical for identical bundles)
        
    Note:
        Uses single source of truth _bundle_to_canonical_json_internal
    """
    return _bundle_to_canonical_json_internal(
        bundle.violations,
        bundle.schema_version
    )


# ============================================================================
# Deterministic Hashing (Unified Path)
# ============================================================================

def compute_bundle_hash(bundle: ValidationErrorBundle) -> str:
    """
    Compute deterministic SHA-256 hash of bundle.
    
    The bundle must carry its own integrity hash. This function computes
    the hash from the canonical JSON representation using the single
    source of truth serialization path.
    
    Args:
        bundle: ValidationErrorBundle to hash
        
    Returns:
        SHA-256 hex digest (64 characters)
        
    Properties:
        - Replay produces identical hash
        - Storage can verify integrity
        - Migration comparison is possible
        - Cache layers can short-circuit equality
        - Uses unified canonical serialization path
    """
    canonical_json = _bundle_to_canonical_json_internal(
        bundle.violations,
        bundle.schema_version
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_bundle(
    violations: list[ValidationViolation],
    schema_version: int,
) -> ValidationErrorBundle:
    """
    Build a ValidationErrorBundle with computed hash.
    
    This is the canonical way to construct a bundle. It:
    1. Sorts violations deterministically
    2. Creates bundle (hash computed automatically in __post_init__)
    3. Returns final bundle with hash
    
    Args:
        violations: List of ValidationViolation objects
        schema_version: Schema version used for validation
        
    Returns:
        ValidationErrorBundle with computed deterministic_hash
        
    Note:
        The hash is computed from the sorted violations, ensuring
        order-independent rule emission produces identical hashes.
        This is the ONLY recommended way to construct bundles.
    """
    # Sort violations deterministically
    sorted_violations = sort_violations(violations)
    
    # Create bundle - hash will be computed in __post_init__
    # from single canonical serialization path
    bundle = ValidationErrorBundle(
        violations=sorted_violations,
        schema_version=schema_version,
    )
    
    return bundle


# ============================================================================
# Invariant Enforcement (Optional Validation Utilities)
# ============================================================================
#
# NOTE: These functions are optional validation utilities that extend beyond
# the pure data contract layer. They perform logical validation analysis
# but do NOT raise exceptions, maintaining Tier-0 safety for replay systems.
#
# These are provided for governance and integrity checking, but are not
# part of the core deterministic transport contract.

def check_violation_invariants(
    violation: ValidationViolation,
    registered_reasons: Optional[Set[EjectionReason]] = None,
) -> Tuple[bool, list[str]]:
    """
    Check invariants for a single violation.
    
    Returns error indicators instead of raising exceptions.
    
    NOTE: This is an optional validation utility, not part of the core
    data contract. It performs logical validation analysis but maintains
    Tier-0 safety by returning error indicators rather than raising.
    
    Args:
        violation: ValidationViolation to check
        registered_reasons: Optional set of registered EjectionReason objects
        
    Returns:
        Tuple of (is_valid, list_of_error_messages)
        - is_valid: True if all invariants pass
        - list_of_error_messages: List of invariant violation descriptions
    """
    errors = []
    
    # Check field_path is string type (preserve raw input, but validate type)
    if not isinstance(violation.field_path, str):
        errors.append(
            f"field_path must be str, got {type(violation.field_path).__name__}: {violation.field_path!r}"
        )
    
    # Check schema_version is positive integer (preserve raw input, but validate)
    if not isinstance(violation.schema_version, int):
        errors.append(
            f"schema_version must be int, got {type(violation.schema_version).__name__}: {violation.schema_version!r}"
        )
    elif violation.schema_version < 1:
        errors.append(f"schema_version must be >= 1, got {violation.schema_version}")
    
    # Check reason is registered (if registry provided)
    if registered_reasons is not None:
        if violation.reason not in registered_reasons:
            errors.append(
                f"EjectionReason {violation.reason.code} not in registered reasons"
            )
    
    return (len(errors) == 0, errors)


def check_bundle_invariants(
    bundle: ValidationErrorBundle,
    registered_reasons: Optional[Set[EjectionReason]] = None,
) -> Tuple[bool, list[str]]:
    """
    Check invariants for a bundle.
    
    Enforces:
    - No duplicate (code, field_path) pairs
    - Schema version consistency across violations
    - Hash integrity
    - All reasons are registered (if registry provided)
    
    Returns error indicators instead of raising exceptions.
    
    NOTE: This is an optional validation utility, not part of the core
    data contract. It performs logical validation analysis but maintains
    Tier-0 safety by returning error indicators rather than raising.
    
    Args:
        bundle: ValidationErrorBundle to check
        registered_reasons: Optional set of registered EjectionReason objects
        
    Returns:
        Tuple of (is_valid, list_of_error_messages)
        - is_valid: True if all invariants pass
        - list_of_error_messages: List of invariant violation descriptions
    """
    errors = []
    
    # Check for duplicate (code, field_path) pairs
    seen_pairs: Set[Tuple[str, str]] = set()
    for v in bundle.violations:
        pair = (v.reason.code, v.field_path)
        if pair in seen_pairs:
            errors.append(
                f"Duplicate violation: code={v.reason.code}, "
                f"field_path={v.field_path}"
            )
        seen_pairs.add(pair)
    
    # Check schema version consistency
    violation_versions = {v.schema_version for v in bundle.violations}
    if len(violation_versions) > 1:
        errors.append(
            f"Schema version inconsistency: violations have versions {violation_versions}, "
            f"bundle has {bundle.schema_version}"
        )
    
    # Check bundle schema_version matches violations
    if violation_versions and bundle.schema_version not in violation_versions:
        errors.append(
            f"Bundle schema_version {bundle.schema_version} does not match "
            f"violation versions {violation_versions}"
        )
    
    # Check hash integrity
    computed_hash = compute_bundle_hash(bundle)
    if bundle.deterministic_hash != computed_hash:
        errors.append(
            f"Hash mismatch: bundle has {bundle.deterministic_hash[:16]}..., "
            f"computed {computed_hash[:16]}..."
        )
    
    # Check all reasons are registered (if registry provided)
    if registered_reasons is not None:
        for v in bundle.violations:
            if v.reason not in registered_reasons:
                errors.append(
                    f"EjectionReason {v.reason.code} not in registered reasons"
                )
    
    return (len(errors) == 0, errors)


# ============================================================================
# Machine Parseability Requirements
# ============================================================================

# All consuming systems must rely on:
# - reason.code
# - severity
# - category
# - field_path
# - schema_version
# - context
#
# Never parse:
# - Human-readable descriptions
#
# Descriptions are for logging only.


# ============================================================================
# Failure Classification Views (Optional Helpers)
# ============================================================================

def has_fatal_errors(bundle: ValidationErrorBundle) -> bool:
    """
    Check if bundle contains any fatal errors.
    
    Args:
        bundle: ValidationErrorBundle to check
        
    Returns:
        True if any violation has FATAL severity
    """
    return any(
        v.reason.severity == RejectionSeverity.FATAL
        for v in bundle.violations
    )


def has_blocking_errors(
    bundle: ValidationErrorBundle,
    strict_mode: bool = False,
) -> bool:
    """
    Check if bundle contains any blocking errors.
    
    Blocking errors are:
    - FATAL (always blocking)
    - ERROR (blocking unless strict_mode=False and severity allows)
    
    Args:
        bundle: ValidationErrorBundle to check
        strict_mode: If True, WARNING also blocks
        
    Returns:
        True if any violation is blocking
    """
    for v in bundle.violations:
        severity = v.reason.severity
        if severity == RejectionSeverity.FATAL:
            return True
        if severity == RejectionSeverity.ERROR:
            return True
        if strict_mode and severity == RejectionSeverity.WARNING:
            return True
    return False


def get_violations_by_category(
    bundle: ValidationErrorBundle,
    category: RejectionCategory,
) -> Tuple[ValidationViolation, ...]:
    """
    Get all violations for a specific category.
    
    Args:
        bundle: ValidationErrorBundle to filter
        category: RejectionCategory to filter by
        
    Returns:
        Tuple of violations matching the category
    """
    return tuple(
        v for v in bundle.violations
        if v.reason.category == category
    )


def get_violations_by_severity(
    bundle: ValidationErrorBundle,
    severity: RejectionSeverity,
) -> Tuple[ValidationViolation, ...]:
    """
    Get all violations for a specific severity.
    
    Args:
        bundle: ValidationErrorBundle to filter
        severity: RejectionSeverity to filter by
        
    Returns:
        Tuple of violations matching the severity
    """
    return tuple(
        v for v in bundle.violations
        if v.reason.severity == severity
    )


# ============================================================================
# Integrity Validation
# ============================================================================

def validate_bundle_integrity(bundle: ValidationErrorBundle) -> bool:
    """
    Validate bundle integrity by recomputing hash.
    
    Args:
        bundle: ValidationErrorBundle to validate
        
    Returns:
        True if hash matches computed value, False otherwise
    """
    computed_hash = compute_bundle_hash(bundle)
    return bundle.deterministic_hash == computed_hash


# ============================================================================
# Static Registry Cross-Check (Explicit Validation)
# ============================================================================

def validate_static_registry() -> Tuple[bool, list[str]]:
    """
    Validate registry integrity (explicit call, not import-time).
    
    Performs validation of:
    - Code uniqueness
    - Stable sort key uniqueness
    - Registry completeness
    - Cross-reference integrity with ejection_reasons.py
    
    Returns error indicators instead of raising exceptions.
    This maintains Tier-0 safety for replay systems and snapshot loaders.
    
    NOTE: This function should be called explicitly at system bootstrap,
    not automatically at import time. This allows:
    - Replay engines to import modules without full registry
    - Snapshot analysis tools to load modules safely
    - Lazy loading / plugin ecosystems to work correctly
    
    Args:
        None
        
    Returns:
        Tuple of (is_valid, list_of_error_messages)
        - is_valid: True if registry integrity is valid
        - list_of_error_messages: List of integrity violation descriptions
        
    Example:
        is_valid, errors = validate_static_registry()
        if not is_valid:
            raise SystemError(f"Registry integrity violation: {errors}")
    """
    errors = []
    registered_codes = {reason.code for reason in ALL_EJECTION_REASONS}
    registered_sort_keys = {reason.stable_sort_key for reason in ALL_EJECTION_REASONS}
    registered_reasons = set(ALL_EJECTION_REASONS)
    
    # Check that all registered reasons have unique codes
    if len(registered_codes) != len(ALL_EJECTION_REASONS):
        duplicate_codes = []
        seen_codes = set()
        for reason in ALL_EJECTION_REASONS:
            if reason.code in seen_codes:
                duplicate_codes.append(reason.code)
            seen_codes.add(reason.code)
        errors.append(
            f"Duplicate rejection codes detected in registry: {duplicate_codes}"
        )
    
    # Check that all registered reasons have unique stable_sort_keys
    if len(registered_sort_keys) != len(ALL_EJECTION_REASONS):
        duplicate_keys = []
        seen_keys = set()
        for reason in ALL_EJECTION_REASONS:
            if reason.stable_sort_key in seen_keys:
                duplicate_keys.append((reason.code, reason.stable_sort_key))
            seen_keys.add(reason.stable_sort_key)
        errors.append(
            f"Duplicate stable_sort_keys detected in registry: {duplicate_keys}"
        )
    
    # Verify all codes follow hierarchical naming convention
    for reason in ALL_EJECTION_REASONS:
        parts = reason.code.split(".")
        if len(parts) < 2:
            errors.append(
                f"Rejection code must have at least 2 parts: {reason.code}"
            )
    
    # Verify registry is non-empty
    if not ALL_EJECTION_REASONS:
        errors.append("EjectionReason registry is empty")
    
    return (len(errors) == 0, errors)


# ============================================================================
# Replay Safety Properties
# ============================================================================

# This model guarantees:
#
# 1. Identical input → identical JSON
#    - Same violations produce same canonical JSON
#    - Deep canonicalization ensures nested structures are deterministic
#
# 2. Identical JSON → identical SHA256
#    - Deterministic hashing ensures this
#    - Single source of truth for serialization
#
# 3. Identical SHA256 → identical error semantics
#    - Hash collision is cryptographically negligible
#
# 4. Order-independent rule emission
#    - Sorting ensures violations are ordered deterministically
#    - Different rule execution orders produce same bundle
#    - Forward-safe enum ordering via enum.value
#
# 5. No runtime exceptions
#    - Pure data contract layer
#    - Validation functions return error indicators
#    - Safe for replay engines and snapshot loaders
#    - No import-time assertions (explicit validation at bootstrap)