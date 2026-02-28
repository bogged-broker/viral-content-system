"""
/data/versioning/policy/compatibility_policy.py

Core Responsibility

This file defines:

- Backward compatibility rules
- Forward compatibility rules
- Reader/writer contract symmetry
- Schema negotiation guarantees
- Enforcement boundary for safety checks
- Compatibility classification semantics
- Deterministic evaluation logic

It does not:

- Perform migrations
- Modify schema graphs
- Infer version bumps
- Read external configuration

It is pure logic.

---

Conceptual Model

Compatibility is directional.

Given versions A and B:

- Backward Compatible? → Can B read data written by A?
- Forward Compatible? → Can A read data written by B?

These are not equivalent.

And your system must explicitly encode both.

---

Architectural Placement

/data/versioning/model/
    semantic_policy.py  ← Defines what patch/minor/major means

/data/versioning/policy/
    compatibility_policy.py  ← Defines compatibility guarantees (THIS FILE)

/data/validation/
    validators.py  ← Calls compatibility assertions

Clean separation:

Model defines semantics. Policy defines law. Validation enforces runtime compliance.

---

Deterministic Contract Requirement

Compatibility evaluation must be:

- Pure function
- Stateless
- Environment-agnostic
- Config-free
- Time-independent

Why?

Because compatibility must not vary across:

- Nodes
- Deployments
- Regions
- CI pipelines

Otherwise distributed safety breaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, List

from ..model.version import SchemaVersion
from ..model.semantic_policy import (
    SemanticVersionPolicy,
    VersionChangeType,
    StructuralChange,
)


# ============================================================================
# COMPATIBILITY DIRECTION
# ============================================================================

class CompatibilityDirection(str, Enum):
    """Direction of compatibility evaluation."""
    
    BACKWARD = "backward"
    """Backward compatibility: Can newer version read data from older version?"""
    
    FORWARD = "forward"
    """Forward compatibility: Can older version read data from newer version?"""


# ============================================================================
# COMPATIBILITY LEVEL
# ============================================================================

class CompatibilityLevel(str, Enum):
    """
    Compatibility evaluation strictness level.
    
    Different deployment scenarios require different compatibility guarantees.
    This enum allows policy to be configured per use case.
    """
    
    STRICT = "strict"
    """
    Strict compatibility: Only verifiable additive changes allowed.
    
    For MINOR backward compatibility:
    - Requires explicit structural change verification
    - Rejects if structural changes contain breaking elements
    - Safe for production deployments with zero-downtime requirements
    """
    
    LENIENT = "lenient"
    """
    Lenient compatibility: Trusts semantic versioning intent.
    
    For MINOR backward compatibility:
    - Assumes semantic versioning intent is correct
    - Does not require structural change verification
    - Suitable for library/API compatibility where schema is not directly managed
    """
    
    MIGRATION_WINDOW = "migration_window"
    """
    Migration window: Allows compatibility during active migration.
    
    For forward compatibility:
    - Allows MINOR updates to be forward compatible if deserialization ignores unknown fields
    - Enables gradual rollouts with mixed-version clusters
    - Requires explicit protocol negotiation support
    """


# ============================================================================
# STRONGLY TYPED RESULT MODEL
# ============================================================================

@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    """
    Immutable, structured compatibility evaluation result.
    
    Why not return bool only?
    
    Because in production:
    - CI pipelines need reason codes
    - Audit logs need explanations
    - Recovery systems need structured output
    - Governance dashboards need programmatic signals
    
    You don't want string parsing hacks later.
    
    Attributes:
        source: The source (older) schema version
        target: The target (newer) schema version
        direction: The direction of compatibility evaluation
        compatible: Whether the versions are compatible in this direction
        reason: Human-readable explanation of the compatibility decision
    """
    
    source: SchemaVersion
    target: SchemaVersion
    direction: CompatibilityDirection
    compatible: bool
    reason: str
    
    def __post_init__(self) -> None:
        """Validate result consistency."""
        if not isinstance(self.source, SchemaVersion):
            raise TypeError(
                f"source must be SchemaVersion, got {type(self.source).__name__}"
            )
        if not isinstance(self.target, SchemaVersion):
            raise TypeError(
                f"target must be SchemaVersion, got {type(self.target).__name__}"
            )
        if not isinstance(self.direction, CompatibilityDirection):
            raise TypeError(
                f"direction must be CompatibilityDirection, got {type(self.direction).__name__}"
            )
        if not isinstance(self.compatible, bool):
            raise TypeError(
                f"compatible must be bool, got {type(self.compatible).__name__}"
            )
        if not isinstance(self.reason, str):
            raise TypeError(
                f"reason must be str, got {type(self.reason).__name__}"
            )


# ============================================================================
# COMPATIBILITY VIOLATION EXCEPTION
# ============================================================================

class CompatibilityViolation(Exception):
    """
    Raised when compatibility assertions fail.
    
    This exception provides structured information about why compatibility
    was violated, enabling recovery systems and audit logs to take appropriate
    action.
    """
    
    def __init__(
        self,
        result: CompatibilityResult,
        message: str | None = None,
    ) -> None:
        """
        Initialize compatibility violation.
        
        Args:
            result: The compatibility result that indicates incompatibility
            message: Optional custom message. If None, uses result.reason
        """
        if message is None:
            message = result.reason
        
        super().__init__(message)
        self.result = result


# ============================================================================
# COMPATIBILITY POLICY ENGINE
# ============================================================================

class CompatibilityPolicy:
    """
    Pure compatibility evaluation engine.
    
    This class provides deterministic compatibility evaluation based on
    semantic versioning rules. It does not perform migrations, modify
    schemas, or read external configuration.
    
    It only answers:
    > Are these versions compatible under current contract?
    
    Usage:
        # With explicit dependency injection (Tier-0 recommended)
        semantic_policy = SemanticVersionPolicy()
        policy = CompatibilityPolicy(
            semantic_policy=semantic_policy,
            backward_level=CompatibilityLevel.STRICT,
            forward_level=CompatibilityLevel.MIGRATION_WINDOW,
        )
        
        # Check backward compatibility with structural verification
        result = policy.is_backward_compatible(
            previous=SchemaVersion(2, 1, 0),
            candidate=SchemaVersion(2, 2, 0),
            structural_changes=[...],  # Optional but required for STRICT level
        )
        
        if not result.compatible:
            raise CompatibilityViolation(result)
    """
    
    def __init__(
        self,
        semantic_policy: SemanticVersionPolicy | None = None,
        backward_level: CompatibilityLevel = CompatibilityLevel.STRICT,
        forward_level: CompatibilityLevel = CompatibilityLevel.STRICT,
    ) -> None:
        """
        Initialize compatibility policy engine.
        
        Args:
            semantic_policy: Semantic version policy instance. If None, creates default.
                            For Tier-0 compliance, always inject explicitly.
            backward_level: Strictness level for backward compatibility evaluation.
            forward_level: Strictness level for forward compatibility evaluation.
        
        Note:
            Explicit dependency injection ensures:
            - Audit traceability
            - Deterministic injection
            - Explicit governance versioning
        """
        if semantic_policy is None:
            semantic_policy = SemanticVersionPolicy()
        
        self._semantic_policy = semantic_policy
        self._backward_level = backward_level
        self._forward_level = forward_level
    
    # ========================================================================
    # BACKWARD COMPATIBILITY
    # ========================================================================
    
    def is_backward_compatible(
        self,
        previous: SchemaVersion,
        candidate: SchemaVersion,
        structural_changes: list[StructuralChange] | None = None,
    ) -> CompatibilityResult:
        """
        Evaluate backward compatibility.
        
        Interpretation:
        > Can the candidate version safely read data written under previous?
        
        Evaluation Logic:
        1. Handle pre-release and build metadata edge cases
        2. Compare semantic category (major/minor/patch)
        3. Forbid major bumps (breaking changes)
        4. Allow patch bumps (non-structural fixes)
        5. For MINOR: Verify structural changes are additive-only (if STRICT level)
        
        Args:
            previous: The previous (older) schema version
            candidate: The candidate (newer) schema version
            structural_changes: Optional list of structural changes between versions.
                              Required for STRICT level MINOR compatibility.
                              If None and STRICT level, assumes unsafe.
        
        Returns:
            CompatibilityResult with structured reasoning
        
        Note:
            This method is pure and stateless. It does not:
            - Read VersionGraph
            - Decide allowed transitions
            - Perform migrations
            - Mutate schema
        """
        # Handle pre-release versions explicitly
        # Pre-release versions are not considered stable and require special handling
        if previous.prerelease is not None or candidate.prerelease is not None:
            # Pre-release compatibility is conservative: only same pre-release chain
            if previous.prerelease != candidate.prerelease:
                return CompatibilityResult(
                    source=previous,
                    target=candidate,
                    direction=CompatibilityDirection.BACKWARD,
                    compatible=False,
                    reason=(
                        f"Pre-release versions require explicit compatibility verification. "
                        f"Previous: {previous}, Candidate: {candidate}. "
                        f"Pre-release transitions are not automatically backward compatible."
                    ),
                )
        
        # Build metadata does not affect compatibility (by SemVer spec)
        # We compare versions ignoring build metadata
        
        # Validate input ordering (ignoring build metadata)
        if candidate < previous:
            return CompatibilityResult(
                source=previous,
                target=candidate,
                direction=CompatibilityDirection.BACKWARD,
                compatible=False,
                reason=(
                    f"Candidate version {candidate} is older than previous version {previous}. "
                    f"Backward compatibility requires candidate >= previous."
                ),
            )
        
        # If versions are equal (ignoring build metadata), they are compatible
        if previous == candidate:
            return CompatibilityResult(
                source=previous,
                target=candidate,
                direction=CompatibilityDirection.BACKWARD,
                compatible=True,
                reason="Equal versions are backward compatible (build metadata ignored).",
            )
        
        # Classify version delta
        try:
            category = self._semantic_policy.classify_version_delta(
                base=previous,
                target=candidate,
            )
        except Exception as e:
            return CompatibilityResult(
                source=previous,
                target=candidate,
                direction=CompatibilityDirection.BACKWARD,
                compatible=False,
                reason=f"Version delta classification failed: {e}",
            )
        
        # Evaluate based on category
        if category == VersionChangeType.PATCH:
            return CompatibilityResult(
                source=previous,
                target=candidate,
                direction=CompatibilityDirection.BACKWARD,
                compatible=True,
                reason=(
                    "Patch updates are backward compatible. "
                    "They contain non-structural fixes that do not alter the schema contract."
                ),
            )
        
        if category == VersionChangeType.MINOR:
            # Minor version backward compatibility depends on strictness level
            if self._backward_level == CompatibilityLevel.STRICT:
                # STRICT level: Require explicit structural change verification
                if structural_changes is None:
                    return CompatibilityResult(
                        source=previous,
                        target=candidate,
                        direction=CompatibilityDirection.BACKWARD,
                        compatible=False,
                        reason=(
                            "STRICT compatibility level requires structural change verification "
                            "for MINOR version updates. Provide structural_changes parameter to verify "
                            "that changes are additive-only (no field removals, no required field additions)."
                        ),
                    )
                
                # Verify structural changes are additive-only
                from ..model.semantic_policy import StructuralChangeType
                
                breaking_changes = [
                    change for change in structural_changes
                    if self._semantic_policy.is_breaking_change(change)
                ]
                
                if breaking_changes:
                    breaking_descriptions = [
                        f"{c.change_type.value} at '{c.field_path}'"
                        for c in breaking_changes
                    ]
                    return CompatibilityResult(
                        source=previous,
                        target=candidate,
                        direction=CompatibilityDirection.BACKWARD,
                        compatible=False,
                        reason=(
                            f"MINOR version update contains breaking changes: "
                            f"{', '.join(breaking_descriptions)}. "
                            f"Breaking changes require MAJOR version bump."
                        ),
                    )
                
                # Verify all changes are allowed for MINOR using public API
                disallowed_changes = []
                for change in structural_changes:
                    required_bump = self._semantic_policy.required_bump_level(change)
                    if required_bump == VersionChangeType.MAJOR:
                        disallowed_changes.append(change)
                
                if disallowed_changes:
                    disallowed_descriptions = [
                        f"{c.change_type.value} at '{c.field_path}'"
                        for c in disallowed_changes
                    ]
                    return CompatibilityResult(
                        source=previous,
                        target=candidate,
                        direction=CompatibilityDirection.BACKWARD,
                        compatible=False,
                        reason=(
                            f"MINOR version update contains changes requiring MAJOR bump: "
                            f"{', '.join(disallowed_descriptions)}. "
                            f"These changes are not allowed in MINOR version updates."
                        ),
                    )
                
                return CompatibilityResult(
                    source=previous,
                    target=candidate,
                    direction=CompatibilityDirection.BACKWARD,
                    compatible=True,
                    reason=(
                        "Minor version update is backward compatible. "
                        "Structural changes verified as additive-only (no breaking changes detected)."
                    ),
                )
            
            else:
                # LENIENT level: Trust semantic versioning intent
                return CompatibilityResult(
                    source=previous,
                    target=candidate,
                    direction=CompatibilityDirection.BACKWARD,
                    compatible=True,
                    reason=(
                        "Minor version updates are backward compatible (LENIENT level). "
                        "Semantic versioning intent is trusted. "
                        "For production safety, use STRICT level with structural change verification."
                    ),
                )
        
        if category == VersionChangeType.MAJOR:
            return CompatibilityResult(
                source=previous,
                target=candidate,
                direction=CompatibilityDirection.BACKWARD,
                compatible=False,
                reason=(
                    "Major version updates are not backward compatible. "
                    "They contain breaking changes that require migration."
                ),
            )
        
        # Should not reach here, but handle gracefully
        return CompatibilityResult(
            source=previous,
            target=candidate,
            direction=CompatibilityDirection.BACKWARD,
            compatible=False,
            reason=f"Unknown version change category: {category}",
        )
    
    # ========================================================================
    # FORWARD COMPATIBILITY
    # ========================================================================
    
    def is_forward_compatible(
        self,
        earlier: SchemaVersion,
        later: SchemaVersion,
        structural_changes: list[StructuralChange] | None = None,
    ) -> CompatibilityResult:
        """
        Evaluate forward compatibility.
        
        Interpretation:
        > Can earlier version read data written under later?
        
        This is usually stricter than backward compatibility because:
        - Field additions may break deserializers that don't ignore unknown fields
        - Enum expansions may break strict matching
        - Structural reordering may break canonical hashing
        
        Args:
            earlier: The earlier (older) schema version
            later: The later (newer) schema version
            structural_changes: Optional list of structural changes between versions.
                              Used for MIGRATION_WINDOW level to verify unknown-field tolerance.
        
        Returns:
            CompatibilityResult with structured reasoning
        
        Note:
            Forward compatibility is generally more restrictive than backward
            compatibility. You must encode your organization's actual forward
            guarantees here.
        """
        # Handle pre-release versions explicitly
        if earlier.prerelease is not None or later.prerelease is not None:
            if earlier.prerelease != later.prerelease:
                return CompatibilityResult(
                    source=earlier,
                    target=later,
                    direction=CompatibilityDirection.FORWARD,
                    compatible=False,
                    reason=(
                        f"Pre-release versions require explicit compatibility verification. "
                        f"Earlier: {earlier}, Later: {later}. "
                        f"Pre-release transitions are not automatically forward compatible."
                    ),
                )
        
        # Build metadata does not affect compatibility
        # Validate input ordering (ignoring build metadata)
        if later < earlier:
            return CompatibilityResult(
                source=earlier,
                target=later,
                direction=CompatibilityDirection.FORWARD,
                compatible=False,
                reason=(
                    f"Later version {later} is older than earlier version {earlier}. "
                    f"Forward compatibility requires later >= earlier."
                ),
            )
        
        # If versions are equal (ignoring build metadata), they are compatible
        if earlier == later:
            return CompatibilityResult(
                source=earlier,
                target=later,
                direction=CompatibilityDirection.FORWARD,
                compatible=True,
                reason="Equal versions are forward compatible (build metadata ignored).",
            )
        
        # Classify version delta
        try:
            category = self._semantic_policy.classify_version_delta(
                base=earlier,
                target=later,
            )
        except Exception as e:
            return CompatibilityResult(
                source=earlier,
                target=later,
                direction=CompatibilityDirection.FORWARD,
                compatible=False,
                reason=f"Version delta classification failed: {e}",
            )
        
        # Evaluate based on category
        if category == VersionChangeType.PATCH:
            # Patch updates are assumed symmetric (forward compatible)
            # They don't add new fields or change structure
            return CompatibilityResult(
                source=earlier,
                target=later,
                direction=CompatibilityDirection.FORWARD,
                compatible=True,
                reason=(
                    "Patch updates are forward compatible. "
                    "They contain non-structural fixes that do not alter the schema contract."
                ),
            )
        
        if category == VersionChangeType.MINOR:
            # Forward compatibility for MINOR depends on level
            if self._forward_level == CompatibilityLevel.MIGRATION_WINDOW:
                # MIGRATION_WINDOW: Allow if deserialization ignores unknown fields
                # This enables gradual rollouts with mixed-version clusters
                if structural_changes is None:
                    # Without structural changes, conservatively assume unsafe
                    return CompatibilityResult(
                        source=earlier,
                        target=later,
                        direction=CompatibilityDirection.FORWARD,
                        compatible=False,
                        reason=(
                            "MIGRATION_WINDOW level requires structural change verification "
                            "to confirm unknown-field tolerance. Provide structural_changes parameter."
                        ),
                    )
                
                # Verify changes are only additive (no breaking changes)
                from ..model.semantic_policy import StructuralChangeType
                
                breaking_changes = [
                    change for change in structural_changes
                    if self._semantic_policy.is_breaking_change(change)
                ]
                
                if breaking_changes:
                    return CompatibilityResult(
                        source=earlier,
                        target=later,
                        direction=CompatibilityDirection.FORWARD,
                        compatible=False,
                        reason=(
                            f"MINOR version update contains breaking changes. "
                            f"Forward compatibility requires additive-only changes."
                        ),
                    )
                
                # In migration window, assume unknown fields are tolerated
                return CompatibilityResult(
                    source=earlier,
                    target=later,
                    direction=CompatibilityDirection.FORWARD,
                    compatible=True,
                    reason=(
                        "Minor version update is forward compatible (MIGRATION_WINDOW level). "
                        "Assumes deserialization strategy tolerates unknown fields. "
                        "Structural changes verified as additive-only."
                    ),
                )
            
            else:
                # STRICT or LENIENT: Conservative default (not forward compatible)
                return CompatibilityResult(
                    source=earlier,
                    target=later,
                    direction=CompatibilityDirection.FORWARD,
                    compatible=False,
                    reason=(
                        f"Minor version updates are not forward compatible ({self._forward_level.value} level). "
                        "They may introduce new fields that older readers cannot process. "
                        "Use MIGRATION_WINDOW level if your deserialization strategy ignores unknown fields."
                    ),
                )
        
        if category == VersionChangeType.MAJOR:
            return CompatibilityResult(
                source=earlier,
                target=later,
                direction=CompatibilityDirection.FORWARD,
                compatible=False,
                reason=(
                    "Major version updates are not forward compatible. "
                    "They contain breaking changes that older readers cannot process."
                ),
            )
        
        # Should not reach here, but handle gracefully
        return CompatibilityResult(
            source=earlier,
            target=later,
            direction=CompatibilityDirection.FORWARD,
            compatible=False,
            reason=f"Unknown version change category: {category}",
        )
    
    # ========================================================================
    # ASSERTION WRAPPERS
    # ========================================================================
    
    def assert_backward_compatible(
        self,
        previous: SchemaVersion,
        candidate: SchemaVersion,
    ) -> None:
        """
        Assert backward compatibility, raising exception if not compatible.
        
        This creates a clean throw boundary for validation layers.
        
        Args:
            previous: The previous (older) schema version
            candidate: The candidate (newer) schema version
        
        Raises:
            CompatibilityViolation: If versions are not backward compatible
        """
        result = self.is_backward_compatible(previous, candidate)
        
        if not result.compatible:
            raise CompatibilityViolation(result)
    
    def assert_forward_compatible(
        self,
        earlier: SchemaVersion,
        later: SchemaVersion,
    ) -> None:
        """
        Assert forward compatibility, raising exception if not compatible.
        
        This creates a clean throw boundary for validation layers.
        
        Args:
            earlier: The earlier (older) schema version
            later: The later (newer) schema version
        
        Raises:
            CompatibilityViolation: If versions are not forward compatible
        """
        result = self.is_forward_compatible(earlier, later)
        
        if not result.compatible:
            raise CompatibilityViolation(result)


# ============================================================================
# MODULE-LEVEL CONVENIENCE FUNCTIONS
# ============================================================================

# Create a default policy instance for module-level convenience functions
# Uses STRICT level for production safety
_default_policy = CompatibilityPolicy(
    semantic_policy=SemanticVersionPolicy(),
    backward_level=CompatibilityLevel.STRICT,
    forward_level=CompatibilityLevel.STRICT,
)


def is_backward_compatible(
    previous: SchemaVersion,
    candidate: SchemaVersion,
    structural_changes: list[StructuralChange] | None = None,
) -> CompatibilityResult:
    """
    Module-level convenience function for backward compatibility evaluation.
    
    Args:
        previous: The previous (older) schema version
        candidate: The candidate (newer) schema version
        structural_changes: Optional list of structural changes between versions.
                          Required for STRICT level MINOR compatibility.
    
    Returns:
        CompatibilityResult with structured reasoning
    """
    return _default_policy.is_backward_compatible(previous, candidate, structural_changes)


def is_forward_compatible(
    earlier: SchemaVersion,
    later: SchemaVersion,
    structural_changes: list[StructuralChange] | None = None,
) -> CompatibilityResult:
    """
    Module-level convenience function for forward compatibility evaluation.
    
    Args:
        earlier: The earlier (older) schema version
        later: The later (newer) schema version
        structural_changes: Optional list of structural changes between versions.
                          Used for MIGRATION_WINDOW level.
    
    Returns:
        CompatibilityResult with structured reasoning
    """
    return _default_policy.is_forward_compatible(earlier, later, structural_changes)


def assert_backward_compatible(
    previous: SchemaVersion,
    candidate: SchemaVersion,
) -> None:
    """
    Module-level convenience function for backward compatibility assertion.
    
    Args:
        previous: The previous (older) schema version
        candidate: The candidate (newer) schema version
    
    Raises:
        CompatibilityViolation: If versions are not backward compatible
    """
    _default_policy.assert_backward_compatible(previous, candidate)


def assert_forward_compatible(
    earlier: SchemaVersion,
    later: SchemaVersion,
) -> None:
    """
    Module-level convenience function for forward compatibility assertion.
    
    Args:
        earlier: The earlier (older) schema version
        later: The later (newer) schema version
    
    Raises:
        CompatibilityViolation: If versions are not forward compatible
    """
    _default_policy.assert_forward_compatible(earlier, later)


# ============================================================================
# PUBLIC SURFACE (SEALED)
# ============================================================================

__all__: Final = [
    # Core types
    "CompatibilityDirection",
    "CompatibilityLevel",
    "CompatibilityResult",
    "CompatibilityViolation",
    "CompatibilityPolicy",
    
    # Module-level convenience functions
    "is_backward_compatible",
    "is_forward_compatible",
    "assert_backward_compatible",
    "assert_forward_compatible",
]
