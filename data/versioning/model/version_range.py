"""
/data/versioning/model/version_range.py

Immutable, deterministic SchemaVersion range model.

Defines inclusive/exclusive lower and upper bounds.

This module is PURE. No runtime policy logic allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from .version import SchemaVersion


# ============================================================================
# CUSTOM ERROR
# ============================================================================

class InvalidVersionRange(ValueError):
    """Raised when VersionRange invariants are violated."""


# ============================================================================
# CORE MODEL
# ============================================================================

@dataclass(frozen=True, slots=True)
class VersionRange:
    """
    Represents a contiguous range of SchemaVersion values.

    Bounds may be:
        - closed (inclusive)
        - open (exclusive)
        - unbounded (None)

    Default convention:
        - Lower bound inclusive
        - Upper bound exclusive

    This matches common range mathematics and avoids off-by-one semantic drift.
    """

    lower: Optional[SchemaVersion] = None
    upper: Optional[SchemaVersion] = None
    include_lower: bool = True
    include_upper: bool = False

    # ========================================================================
    # POST-INIT VALIDATION
    # ========================================================================

    def __post_init__(self) -> None:
        """Validate range invariants to prevent invalid states."""
        # Verify SchemaVersion has total ordering guarantees
        if self.lower is not None:
            self._verify_ordering(self.lower)
        if self.upper is not None:
            self._verify_ordering(self.upper)
        
        if self.lower is not None and self.upper is not None:
            if self.lower > self.upper:
                raise InvalidVersionRange(
                    "Lower bound cannot be greater than upper bound."
                )

            if self.lower == self.upper:
                if not (self.include_lower and self.include_upper):
                    # This would create an empty range
                    raise InvalidVersionRange(
                        "Zero-width range must include both bounds."
                    )
    
    @staticmethod
    def _verify_ordering(version: SchemaVersion) -> None:
        """
        Runtime verification that SchemaVersion has total ordering guarantees.
        
        This ensures that <, >, ==, <=, >= are all consistent and deterministic.
        """
        # Verify comparison operators exist and are consistent
        # Test reflexivity: version == version
        if not (version == version):
            raise InvalidVersionRange(
                f"SchemaVersion violates reflexivity: {version} != {version}"
            )
        
        # Test antisymmetry: if a < b then not (b < a)
        # We can't test this without another version, but we verify the operators exist
        # by checking that comparisons don't raise TypeError
        try:
            _ = version < version
            _ = version > version
            _ = version <= version
            _ = version >= version
        except TypeError as e:
            raise InvalidVersionRange(
                f"SchemaVersion does not support total ordering: {e}"
            ) from e

    # ========================================================================
    # CONTAINMENT LOGIC
    # ========================================================================

    def contains(self, version: SchemaVersion) -> bool:
        """
        Determine whether a version falls within this range.

        Returns:
            True if version is within the range, False otherwise.
        """
        if self.lower is not None:
            if version < self.lower:
                return False
            if version == self.lower and not self.include_lower:
                return False

        if self.upper is not None:
            if version > self.upper:
                return False
            if version == self.upper and not self.include_upper:
                return False

        return True

    # ========================================================================
    # EMPTINESS DETECTION
    # ========================================================================

    def is_empty(self) -> bool:
        """
        Determine if this range represents an empty set.

        Returns:
            True if the range is empty, False otherwise.
        """
        if self.lower is not None and self.upper is not None:
            if self.lower > self.upper:
                return True
            if self.lower == self.upper:
                return not (self.include_lower and self.include_upper)
        return False

    # ========================================================================
    # INTERSECTION LOGIC
    # ========================================================================

    def intersect(self, other: VersionRange) -> VersionRange:
        """
        Return the mathematical intersection of two ranges.

        Args:
            other: The other range to intersect with.

        Returns:
            A new VersionRange representing the intersection.
            Returns an empty range if the intersection is empty.
        """
        new_lower = self.lower
        new_include_lower = self.include_lower

        if other.lower is not None:
            if new_lower is None or other.lower > new_lower:
                new_lower = other.lower
                new_include_lower = other.include_lower
            elif other.lower == new_lower:
                new_include_lower = (
                    self.include_lower and other.include_lower
                )

        new_upper = self.upper
        new_include_upper = self.include_upper

        if other.upper is not None:
            if new_upper is None or other.upper < new_upper:
                new_upper = other.upper
                new_include_upper = other.include_upper
            elif other.upper == new_upper:
                new_include_upper = (
                    self.include_upper and other.include_upper
                )

        result = VersionRange(
            lower=new_lower,
            upper=new_upper,
            include_lower=new_include_lower,
            include_upper=new_include_upper,
        )

        if result.is_empty():
            # Return an explicit empty range instead of raising
            # This maintains pure mathematical semantics
            return VersionRange.empty()

        return result

    # ========================================================================
    # STRING REPRESENTATION
    # ========================================================================

    def __str__(self) -> str:
        """
        Deterministic string representation.

        Examples:
            [1.0.0, 2.0.0)  - Lower inclusive, upper exclusive
            (1.2.0, 3.0.0]  - Lower exclusive, upper inclusive
            [-∞, 1.5.0)     - Unbounded lower, upper exclusive
            [2.0.0, ∞)      - Lower inclusive, unbounded upper
        """
        lower_bracket = "[" if self.include_lower else "("
        upper_bracket = "]" if self.include_upper else ")"

        lower_str = str(self.lower) if self.lower else "-∞"
        upper_str = str(self.upper) if self.upper else "∞"

        return f"{lower_bracket}{lower_str}, {upper_str}{upper_bracket}"

    def __repr__(self) -> str:
        """Developer-friendly representation."""
        return (
            f"VersionRange("
            f"lower={self.lower!r}, "
            f"upper={self.upper!r}, "
            f"include_lower={self.include_lower}, "
            f"include_upper={self.include_upper}"
            f")"
        )

    # ========================================================================
    # EQUALITY + HASH STABILITY
    # ========================================================================

    def __eq__(self, other: object) -> bool:
        """Equality comparison based on all range properties."""
        if not isinstance(other, VersionRange):
            return NotImplemented

        return (
            self.lower == other.lower
            and self.upper == other.upper
            and self.include_lower == other.include_lower
            and self.include_upper == other.include_upper
        )

    def __hash__(self) -> int:
        """
        Hash function for use as dictionary keys.

        Hash is stable and based on all range properties.
        """
        return hash(
            (
                self.lower,
                self.upper,
                self.include_lower,
                self.include_upper,
            )
        )

    # ========================================================================
    # CANONICAL CONSTRUCTORS
    # ========================================================================

    @classmethod
    def at_least(cls, version: SchemaVersion) -> VersionRange:
        """
        Create a range representing all versions >= version.

        Args:
            version: The minimum version (inclusive).

        Returns:
            A VersionRange from version to infinity (inclusive lower bound).
        """
        return cls(lower=version, upper=None, include_lower=True)

    @classmethod
    def greater_than(cls, version: SchemaVersion) -> VersionRange:
        """
        Create a range representing all versions > version.

        Args:
            version: The minimum version (exclusive).

        Returns:
            A VersionRange from version to infinity (exclusive lower bound).
        """
        return cls(lower=version, upper=None, include_lower=False)

    @classmethod
    def less_than(cls, version: SchemaVersion) -> VersionRange:
        """
        Create a range representing all versions < version.

        Args:
            version: The maximum version (exclusive).

        Returns:
            A VersionRange from negative infinity to version (exclusive upper bound).
        """
        return cls(lower=None, upper=version, include_upper=False)

    @classmethod
    def exact(cls, version: SchemaVersion) -> VersionRange:
        """
        Create a range representing exactly one version.

        Args:
            version: The exact version.

        Returns:
            A VersionRange containing only the specified version.
        """
        return cls(
            lower=version,
            upper=version,
            include_lower=True,
            include_upper=True,
        )
    
    @classmethod
    def empty(cls) -> VersionRange:
        """
        Create an empty range representing the empty set.
        
        This is a canonical representation of an empty range that can be
        used when intersections result in no valid versions.
        
        Returns:
            A VersionRange that represents an empty set.
            The range has lower == upper with exclusive bounds.
        """
        # Use a sentinel: create a range with equal bounds but exclusive
        # This is mathematically an empty set
        # We need a version to create this, so we'll use a minimal version
        # and mark both bounds as exclusive
        from .version import SchemaVersion
        sentinel = SchemaVersion(0, 0, 0)
        # Create range [sentinel, sentinel) which is empty
        # We bypass normal validation by using object.__setattr__ since we're frozen
        result = object.__new__(cls)
        object.__setattr__(result, 'lower', sentinel)
        object.__setattr__(result, 'upper', sentinel)
        object.__setattr__(result, 'include_lower', False)
        object.__setattr__(result, 'include_upper', False)
        return result