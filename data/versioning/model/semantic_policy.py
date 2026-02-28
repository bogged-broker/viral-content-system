"""
/data/versioning/model/semantic_policy.py

Formal semantic versioning policy framework.

This file defines the mathematical classification framework for schema evolution.
It answers: "Given two SchemaVersions + a structural diff, what category of change is this?"

Core Responsibility:
    Defines the formal semantic meaning of version deltas.

This file does NOT:
    - Execute migrations
    - Inspect rollout state
    - Query live data
    - Decide if a change is allowed in production

This file strictly defines:
    > Given two SchemaVersions + a structural diff, what category of change is this?

Architectural Position:
    SchemaVersion  →  VersionGraph  →  SemanticPolicy
          |                   |               |
       Identity            Movement        Rules of motion

Compatibility enforcement later asks:
    > Is this declared MINOR bump actually MINOR-compliant?

This file provides the rulebook.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Set, Optional, FrozenSet, Tuple, Dict
from .version import SchemaVersion


# ============================================================================
# ERROR TYPES
# ============================================================================

class SemanticPolicyError(ValueError):
    """Base exception for semantic policy violations."""


class InvalidVersionDelta(SemanticPolicyError):
    """Raised when version delta violates semantic versioning rules."""


class StructuralComplianceViolation(SemanticPolicyError):
    """Raised when structural changes violate declared version bump category."""


# ============================================================================
# VERSION CHANGE CATEGORIES
# ============================================================================

class VersionChangeType(Enum):
    """
    Explicit enumeration of version change categories.
    
    No strings allowed in business logic.
    Only these canonical categories exist.
    """
    
    NO_CHANGE = "NO_CHANGE"
    """No version change detected."""
    
    PATCH = "PATCH"
    """
    Patch-level change: non-structural, backward-compatible fix.
    
    Allowed changes:
    - Documentation-only adjustments
    - Internal constraint tightening that does not alter serialized structure
    - Bug fixes that don't change schema contract
    
    Forbidden:
    - Any structural schema change
    - Field additions/removals
    - Type changes
    - Default value changes
    """
    
    MINOR = "MINOR"
    """
    Minor-level change: backward-compatible addition.
    
    Allowed changes:
    - Adding optional fields
    - Adding enum values
    - Relaxing constraints (e.g., making field nullable)
    
    Forbidden:
    - Removing fields
    - Adding required fields
    - Changing field types
    - Removing enum values
    - Changing serialization format
    """
    
    MAJOR = "MAJOR"
    """
    Major-level change: breaking change.
    
    Allowed changes:
    - Any structural change
    - Removing fields
    - Adding required fields
    - Changing field types
    - Removing enum values
    - Changing serialization format
    - Any change that breaks backward compatibility
    """


# ============================================================================
# STRUCTURAL CHANGE TYPES
# ============================================================================

class StructuralChangeType(Enum):
    """
    Explicit enumeration of structural change types.
    
    These must exist as explicit types, not ad-hoc booleans.
    This allows deterministic rule mapping.
    """
    
    # Field-level changes
    FIELD_ADDED_OPTIONAL = "FIELD_ADDED_OPTIONAL"
    """A new optional field was added to the schema."""
    
    FIELD_ADDED_REQUIRED = "FIELD_ADDED_REQUIRED"
    """A new required field was added to the schema."""
    
    FIELD_REMOVED = "FIELD_REMOVED"
    """An existing field was removed from the schema."""
    
    FIELD_TYPE_CHANGED = "FIELD_TYPE_CHANGED"
    """An existing field's type was changed."""
    
    FIELD_NULLABILITY_CHANGED = "FIELD_NULLABILITY_CHANGED"
    """A field's nullability constraint changed (nullable <-> non-nullable)."""
    
    FIELD_DEFAULT_CHANGED = "FIELD_DEFAULT_CHANGED"
    """A field's default value was changed."""
    
    # Enum-level changes
    ENUM_VALUE_ADDED = "ENUM_VALUE_ADDED"
    """A new value was added to an enum type."""
    
    ENUM_VALUE_REMOVED = "ENUM_VALUE_REMOVED"
    """A value was removed from an enum type."""
    
    # Schema-level changes
    SERIALIZATION_FORMAT_CHANGED = "SERIALIZATION_FORMAT_CHANGED"
    """The serialization format changed (e.g., JSON -> MessagePack)."""
    
    # Constraint changes
    CONSTRAINT_ADDED = "CONSTRAINT_ADDED"
    """A new constraint was added (e.g., min/max, pattern, length)."""
    
    CONSTRAINT_REMOVED = "CONSTRAINT_REMOVED"
    """An existing constraint was removed."""
    
    # Documentation-only (non-structural)
    DOCUMENTATION_ONLY = "DOCUMENTATION_ONLY"
    """Only documentation changed; no structural impact."""


# ============================================================================
# STRUCTURAL CHANGE MODEL
# ============================================================================

@dataclass(frozen=True, slots=True)
class StructuralChange:
    """
    Immutable representation of a single structural change.
    
    Attributes:
        change_type: The type of structural change.
        field_path: Dot-separated path to the affected field (e.g., "user.profile.email").
                    Empty string for schema-level changes.
        description: Human-readable description of the change.
    """
    
    change_type: StructuralChangeType
    field_path: str
    description: str = ""
    
    def __post_init__(self) -> None:
        """Validate structural change."""
        if not isinstance(self.change_type, StructuralChangeType):
            raise SemanticPolicyError(
                f"change_type must be StructuralChangeType, got {type(self.change_type).__name__}"
            )


# ============================================================================
# ALLOWED STRUCTURAL DELTAS PER VERSION CATEGORY
# ============================================================================

# This is the core rulebook: what structural changes are allowed at each version level.
# This mapping lives here, not in compatibility layer, not in migration layer.
# This is the authoritative source of truth.

_ALLOWED_CHANGES: dict[VersionChangeType, FrozenSet[StructuralChangeType]] = {
    VersionChangeType.NO_CHANGE: frozenset({
        StructuralChangeType.DOCUMENTATION_ONLY,
    }),
    
    VersionChangeType.PATCH: frozenset({
        StructuralChangeType.DOCUMENTATION_ONLY,
        # PATCH is strictly non-structural
        # Internal constraint tightening that doesn't alter serialized structure
        # is allowed, but we don't have a specific type for that yet.
    }),
    
    VersionChangeType.MINOR: frozenset({
        StructuralChangeType.DOCUMENTATION_ONLY,
        StructuralChangeType.FIELD_ADDED_OPTIONAL,
        StructuralChangeType.ENUM_VALUE_ADDED,
        StructuralChangeType.CONSTRAINT_REMOVED,  # Relaxing constraints is backward-compatible
        StructuralChangeType.FIELD_NULLABILITY_CHANGED,  # Making nullable is backward-compatible
    }),
    
    VersionChangeType.MAJOR: frozenset({
        # MAJOR allows all structural changes
        StructuralChangeType.DOCUMENTATION_ONLY,
        StructuralChangeType.FIELD_ADDED_OPTIONAL,
        StructuralChangeType.FIELD_ADDED_REQUIRED,
        StructuralChangeType.FIELD_REMOVED,
        StructuralChangeType.FIELD_TYPE_CHANGED,
        StructuralChangeType.FIELD_NULLABILITY_CHANGED,
        StructuralChangeType.FIELD_DEFAULT_CHANGED,
        StructuralChangeType.ENUM_VALUE_ADDED,
        StructuralChangeType.ENUM_VALUE_REMOVED,
        StructuralChangeType.SERIALIZATION_FORMAT_CHANGED,
        StructuralChangeType.CONSTRAINT_ADDED,
        StructuralChangeType.CONSTRAINT_REMOVED,
    }),
}


# ============================================================================
# BREAKING CHANGE DETECTION
# ============================================================================

_BREAKING_CHANGES: FrozenSet[StructuralChangeType] = frozenset({
    StructuralChangeType.FIELD_REMOVED,
    StructuralChangeType.FIELD_ADDED_REQUIRED,
    StructuralChangeType.FIELD_TYPE_CHANGED,
    StructuralChangeType.ENUM_VALUE_REMOVED,
    StructuralChangeType.SERIALIZATION_FORMAT_CHANGED,
    StructuralChangeType.FIELD_DEFAULT_CHANGED,  # Can break deserialization assumptions
    StructuralChangeType.CONSTRAINT_ADDED,  # Can make previously valid data invalid
})


# ============================================================================
# COMPLIANCE VALIDATION RESULT
# ============================================================================

@dataclass(frozen=True, slots=True)
class ComplianceViolation:
    """
    Immutable representation of a single compliance violation.
    
    Attributes:
        change: The structural change that violated policy.
        declared_bump: The version bump category that was declared.
        required_bump: The minimum version bump category required for this change.
        reason: Human-readable explanation of the violation.
    """
    
    change: StructuralChange
    declared_bump: VersionChangeType
    required_bump: VersionChangeType
    reason: str


@dataclass(frozen=True, slots=True)
class ComplianceReport:
    """
    Immutable report of structural compliance validation.
    
    Attributes:
        is_compliant: True if all changes comply with declared bump category.
        declared_bump: The version bump category that was declared.
        violations: List of compliance violations (empty if compliant).
    """
    
    is_compliant: bool
    declared_bump: VersionChangeType
    violations: tuple[ComplianceViolation, ...]
    
    def __post_init__(self) -> None:
        """Validate report consistency."""
        if self.is_compliant and len(self.violations) > 0:
            raise SemanticPolicyError(
                "ComplianceReport cannot be compliant with violations"
            )
        if not self.is_compliant and len(self.violations) == 0:
            raise SemanticPolicyError(
                "ComplianceReport cannot be non-compliant without violations"
            )


# ============================================================================
# SEMANTIC VERSION POLICY
# ============================================================================

class SemanticVersionPolicy:
    """
    Formal semantic versioning policy engine.
    
    This class provides deterministic classification and validation of version changes.
    It does not enforce policy operationally - that belongs to the compatibility layer.
    
    This is the rulebook that turns:
        "Feels like a minor change"
    into:
        "Provably requires a MAJOR bump."
    """
    
    # ========================================================================
    # VERSION DELTA CLASSIFICATION
    # ========================================================================
    
    @staticmethod
    def classify_version_delta(
        base: SchemaVersion,
        target: SchemaVersion
    ) -> VersionChangeType:
        """
        Determine the version change category between two SchemaVersions.
        
        Rules:
        - If major differs → MAJOR
        - Else if minor differs → MINOR
        - Else if patch differs → PATCH
        - Else → NO_CHANGE
        
        This method forbids:
        - Simultaneous major and minor change (impossible by definition)
        - Minor increment without patch reset (enforced by SchemaVersion)
        - Patch bump when prerelease semantics invalid
        
        Args:
            base: The base (older) version.
            target: The target (newer) version.
        
        Returns:
            The version change category.
        
        Raises:
            InvalidVersionDelta: If version delta violates semantic versioning rules.
        """
        # Validate that target is actually newer than base
        if target < base:
            raise InvalidVersionDelta(
                f"Target version {target} is not newer than base version {base}. "
                f"Downgrades are not allowed."
            )
        
        # If versions are equal, no change
        if base == target:
            return VersionChangeType.NO_CHANGE
        
        # Classify by component difference
        if base.major != target.major:
            # Major version changed
            # Validate that minor and patch were reset
            if target.minor != 0 or target.patch != 0:
                raise InvalidVersionDelta(
                    f"Major version bump from {base} to {target} must reset "
                    f"minor and patch to 0, but got {target.minor}.{target.patch}"
                )
            return VersionChangeType.MAJOR
        
        elif base.minor != target.minor:
            # Minor version changed
            # Validate that patch was reset
            if target.patch != 0:
                raise InvalidVersionDelta(
                    f"Minor version bump from {base} to {target} must reset "
                    f"patch to 0, but got {target.patch}"
                )
            return VersionChangeType.MINOR
        
        elif base.patch != target.patch:
            # Patch version changed
            return VersionChangeType.PATCH
        
        else:
            # Only prerelease or build metadata differs
            # This is still considered a change, but categorize as PATCH
            # (prerelease transitions are typically patch-level)
            return VersionChangeType.PATCH
    
    # ========================================================================
    # BREAKING CHANGE DETECTION
    # ========================================================================
    
    @staticmethod
    def is_breaking_change(change: StructuralChange) -> bool:
        """
        Determine if a structural change is breaking.
        
        Breaking changes require MAJOR version bumps.
        
        Args:
            change: The structural change to evaluate.
        
        Returns:
            True if the change is breaking, False otherwise.
        """
        return change.change_type in _BREAKING_CHANGES
    
    @staticmethod
    def required_bump_level(change: StructuralChange) -> VersionChangeType:
        """
        Determine the minimum required version bump level for a structural change.
        
        Args:
            change: The structural change to evaluate.
        
        Returns:
            The minimum required version bump category.
        """
        if change.change_type == StructuralChangeType.DOCUMENTATION_ONLY:
            return VersionChangeType.NO_CHANGE
        
        if SemanticVersionPolicy.is_breaking_change(change):
            return VersionChangeType.MAJOR
        
        # Check if it's allowed in MINOR
        if change.change_type in _ALLOWED_CHANGES[VersionChangeType.MINOR]:
            return VersionChangeType.MINOR
        
        # Check if it's allowed in PATCH
        if change.change_type in _ALLOWED_CHANGES[VersionChangeType.PATCH]:
            return VersionChangeType.PATCH
        
        # Default to MAJOR if not explicitly allowed at lower levels
        return VersionChangeType.MAJOR
    
    # ========================================================================
    # STRUCTURAL COMPLIANCE VALIDATION
    # ========================================================================
    
    def validate_structural_compliance(
        self,
        base: SchemaVersion,
        target: SchemaVersion,
        structural_changes: List[StructuralChange]
    ) -> ComplianceReport:
        """
        Validate that structural changes comply with declared version bump category.
        
        Process:
        1. Determine bump category from version delta.
        2. Check every structural change against allowed set.
        3. Return violation report if any violation exists.
        
        This method does NOT raise policy exceptions - it returns deterministic
        violation reports. The compatibility layer will then enforce rejection.
        
        Args:
            base: The base (older) version.
            target: The target (newer) version.
            structural_changes: List of structural changes detected between versions.
        
        Returns:
            ComplianceReport indicating compliance status and any violations.
        """
        # Determine declared bump category
        try:
            declared_bump = self.classify_version_delta(base, target)
        except InvalidVersionDelta as e:
            # Version delta itself is invalid
            # Create a synthetic violation for this
            violation = ComplianceViolation(
                change=StructuralChange(
                    change_type=StructuralChangeType.DOCUMENTATION_ONLY,
                    field_path="",
                    description="Version delta validation"
                ),
                declared_bump=VersionChangeType.MAJOR,  # Placeholder
                required_bump=VersionChangeType.MAJOR,  # Placeholder
                reason=f"Invalid version delta: {e}"
            )
            return ComplianceReport(
                is_compliant=False,
                declared_bump=VersionChangeType.MAJOR,  # Placeholder
                violations=(violation,)
            )
        
        # Get allowed changes for declared bump level
        allowed_changes = _ALLOWED_CHANGES.get(declared_bump, frozenset())
        
        # Check each structural change
        violations: List[ComplianceViolation] = []
        
        for change in structural_changes:
            # Skip documentation-only changes (always allowed)
            if change.change_type == StructuralChangeType.DOCUMENTATION_ONLY:
                continue
            
            # Check if change is allowed at declared bump level
            if change.change_type not in allowed_changes:
                # Determine required bump level
                required_bump = self.required_bump_level(change)
                
                violation = ComplianceViolation(
                    change=change,
                    declared_bump=declared_bump,
                    required_bump=required_bump,
                    reason=(
                        f"Change '{change.change_type.value}' at '{change.field_path}' "
                        f"requires {required_bump.value} bump, but {declared_bump.value} "
                        f"was declared."
                    )
                )
                violations.append(violation)
        
        # Return compliance report
        return ComplianceReport(
            is_compliant=len(violations) == 0,
            declared_bump=declared_bump,
            violations=tuple(violations)
        )
    
    # ========================================================================
    # INVARIANT ASSERTIONS
    # ========================================================================
    
    def assert_compliant(
        self,
        base: SchemaVersion,
        target: SchemaVersion,
        structural_changes: List[StructuralChange]
    ) -> None:
        """
        Assert that version delta and structural changes are compliant.
        
        This is a convenience method that raises an exception if non-compliant.
        Use this when you want enforcement to happen immediately.
        
        For non-blocking validation, use validate_structural_compliance() instead.
        
        Args:
            base: The base (older) version.
            target: The target (newer) version.
            structural_changes: List of structural changes detected between versions.
        
        Raises:
            StructuralComplianceViolation: If changes are not compliant.
        """
        report = self.validate_structural_compliance(
            base, target, structural_changes
        )
        
        if not report.is_compliant:
            violation_messages = [
                f"  - {v.reason}" for v in report.violations
            ]
            raise StructuralComplianceViolation(
                f"Version bump from {base} to {target} violates semantic policy:\n"
                + "\n".join(violation_messages)
            )
    
    # ========================================================================
    # INVARIANT 1: PATCH is strictly non-structural
    # ========================================================================
    
    def assert_patch_non_structural(
        self,
        structural_changes: List[StructuralChange]
    ) -> None:
        """
        Assert that PATCH-level changes contain no structural modifications.
        
        Invariant 1: PATCH may only allow:
        - Documentation-only adjustments
        - Internal constraint tightening that does not alter serialized structure
        
        Anything structural must escalate.
        
        Args:
            structural_changes: List of structural changes to validate.
        
        Raises:
            StructuralComplianceViolation: If structural changes are present.
        """
        structural = [
            c for c in structural_changes
            if c.change_type != StructuralChangeType.DOCUMENTATION_ONLY
        ]
        
        if structural:
            change_descriptions = [
                f"  - {c.change_type.value} at '{c.field_path}'"
                for c in structural
            ]
            raise StructuralComplianceViolation(
                f"PATCH-level changes must be non-structural, but found:\n"
                + "\n".join(change_descriptions)
            )
    
    # ========================================================================
    # INVARIANT 2: MINOR cannot break backward compatibility
    # ========================================================================
    
    def assert_minor_backward_compatible(
        self,
        structural_changes: List[StructuralChange]
    ) -> None:
        """
        Assert that MINOR-level changes maintain backward compatibility.
        
        Invariant 2: MINOR cannot break backward compatibility.
        Formally: All previously valid serialized instances must remain valid.
        
        Args:
            structural_changes: List of structural changes to validate.
        
        Raises:
            StructuralComplianceViolation: If breaking changes are present.
        """
        breaking = [
            c for c in structural_changes
            if self.is_breaking_change(c)
        ]
        
        if breaking:
            change_descriptions = [
                f"  - {c.change_type.value} at '{c.field_path}'"
                for c in breaking
            ]
            raise StructuralComplianceViolation(
                f"MINOR-level changes must be backward-compatible, but found breaking changes:\n"
                + "\n".join(change_descriptions)
            )
    
    # ========================================================================
    # INVARIANT 3: MAJOR is required for breaking changes
    # ========================================================================
    
    def assert_major_for_breaking(
        self,
        declared_bump: VersionChangeType,
        structural_changes: List[StructuralChange]
    ) -> None:
        """
        Assert that breaking changes require MAJOR version bump.
        
        Invariant 3: MAJOR is required for breaking changes.
        Breaking change is defined structurally, not emotionally.
        
        Args:
            declared_bump: The declared version bump category.
            structural_changes: List of structural changes to validate.
        
        Raises:
            StructuralComplianceViolation: If breaking changes exist but MAJOR not declared.
        """
        breaking = [
            c for c in structural_changes
            if self.is_breaking_change(c)
        ]
        
        if breaking and declared_bump != VersionChangeType.MAJOR:
            change_descriptions = [
                f"  - {c.change_type.value} at '{c.field_path}'"
                for c in breaking
            ]
            raise StructuralComplianceViolation(
                f"Breaking changes require MAJOR version bump, but {declared_bump.value} "
                f"was declared. Breaking changes found:\n"
                + "\n".join(change_descriptions)
            )
    
    # ========================================================================
    # INVARIANT 4: Version delta must match declared structural severity
    # ========================================================================
    
    def assert_version_delta_matches_severity(
        self,
        base: SchemaVersion,
        target: SchemaVersion,
        structural_changes: List[StructuralChange]
    ) -> None:
        """
        Assert that version delta matches the minimum required bump level.
        
        Invariant 4: Version delta must match declared structural severity.
        You cannot remove a field and bump MINOR.
        The classification must catch this.
        
        Args:
            base: The base (older) version.
            target: The target (newer) version.
            structural_changes: List of structural changes to validate.
        
        Raises:
            StructuralComplianceViolation: If version delta is insufficient.
        """
        # Determine actual version delta
        declared_bump = self.classify_version_delta(base, target)
        
        # Determine minimum required bump from structural changes
        if not structural_changes:
            # No structural changes - any bump level is fine
            return
        
        required_bumps = [
            self.required_bump_level(c) for c in structural_changes
        ]
        
        # Find the highest required bump level
        # Order: NO_CHANGE < PATCH < MINOR < MAJOR
        bump_priority = {
            VersionChangeType.NO_CHANGE: 0,
            VersionChangeType.PATCH: 1,
            VersionChangeType.MINOR: 2,
            VersionChangeType.MAJOR: 3,
        }
        
        max_required = max(
            required_bumps,
            key=lambda b: bump_priority[b]
        )
        
        # Check if declared bump is sufficient
        if bump_priority[declared_bump] < bump_priority[max_required]:
            raise StructuralComplianceViolation(
                f"Version delta from {base} to {target} declares {declared_bump.value} bump, "
                f"but structural changes require at least {max_required.value} bump."
            )
