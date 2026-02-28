"""
/data/versioning/model/version.py

Strongly-typed, immutable SchemaVersion domain model.

This object is:
- Deterministic
- Comparable
- Hash-stable
- Semantically strict
- Runtime-agnostic

This module MUST NOT import anything outside `/data/versioning/model/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
from typing import Optional, Tuple


class InvalidSchemaVersion(ValueError):
    """Raised when SchemaVersion construction violates invariants."""


# ============================================================================
# IMMUTABLE + ORDERED CORE
# ============================================================================

@total_ordering
@dataclass(frozen=True, slots=True)
class SchemaVersion:
    """
    Strongly-typed semantic schema version.

    Supports strict comparison semantics:
        major > minor > patch > prerelease

    Build metadata does NOT affect ordering.
    """

    major: int
    minor: int
    patch: int
    prerelease: Optional[Tuple[str, ...]] = None
    build: Optional[str] = None

    # ========================================================================
    # POST-INIT VALIDATION
    # ========================================================================

    def __post_init__(self) -> None:
        """Strict validation with no silent normalization or coercion."""
        # Strict integer type enforcement
        if not isinstance(self.major, int):
            raise InvalidSchemaVersion(
                f"Major version must be an integer, got {type(self.major).__name__}."
            )
        if not isinstance(self.minor, int):
            raise InvalidSchemaVersion(
                f"Minor version must be an integer, got {type(self.minor).__name__}."
            )
        if not isinstance(self.patch, int):
            raise InvalidSchemaVersion(
                f"Patch version must be an integer, got {type(self.patch).__name__}."
            )

        # Non-negative validation
        if self.major < 0:
            raise InvalidSchemaVersion("Major version must be >= 0.")
        if self.minor < 0:
            raise InvalidSchemaVersion("Minor version must be >= 0.")
        if self.patch < 0:
            raise InvalidSchemaVersion("Patch version must be >= 0.")

        # Prerelease validation
        if self.prerelease is not None:
            if len(self.prerelease) == 0:
                raise InvalidSchemaVersion(
                    "Prerelease identifiers cannot be empty."
                )
            for identifier in self.prerelease:
                if not identifier:
                    raise InvalidSchemaVersion(
                        "Prerelease identifiers must be non-empty strings."
                    )
                # Reject leading zeros in numeric identifiers (SemVer rule)
                if identifier.isdigit() and len(identifier) > 1 and identifier[0] == '0':
                    raise InvalidSchemaVersion(
                        f"Numeric prerelease identifier cannot have leading zeros: {identifier}"
                    )

    # ========================================================================
    # ORDERING SEMANTICS (CRITICAL)
    # ========================================================================

    def __eq__(self, other: object) -> bool:
        """Equality comparison. Build metadata is excluded."""
        if not isinstance(other, SchemaVersion):
            return NotImplemented

        return (
            self.major,
            self.minor,
            self.patch,
            self.prerelease or (),
        ) == (
            other.major,
            other.minor,
            other.patch,
            other.prerelease or (),
        )

    def __lt__(self, other: SchemaVersion) -> bool:
        """
        Less-than comparison with total ordering semantics.

        Rules:
        - Stable release > prerelease
        - Build metadata ignored
        - SemVer-correct prerelease comparison:
          * Numeric identifiers compare numerically
          * Alphanumeric compare lexicographically
          * Numeric < non-numeric
          * Shorter < longer if equal prefix
        """
        if not isinstance(other, SchemaVersion):
            return NotImplemented

        # Compare core version components first
        if (self.major, self.minor, self.patch) != (
            other.major,
            other.minor,
            other.patch,
        ):
            return (
                self.major,
                self.minor,
                self.patch,
            ) < (
                other.major,
                other.minor,
                other.patch,
            )

        # Handle prerelease logic
        if self.prerelease is None and other.prerelease is None:
            return False

        if self.prerelease is None:
            return False  # stable > prerelease

        if other.prerelease is None:
            return True  # prerelease < stable

        # SemVer-correct prerelease comparison
        return self._compare_prerelease(self.prerelease, other.prerelease) < 0

    @staticmethod
    def _compare_prerelease(
        left: Tuple[str, ...], right: Tuple[str, ...]
    ) -> int:
        """
        Compare two prerelease identifier tuples according to SemVer rules.

        Returns:
            -1 if left < right
            0 if left == right
            1 if left > right

        SemVer precedence rules:
        1. Numeric identifiers compare numerically
        2. Alphanumeric identifiers compare lexicographically
        3. Numeric identifiers have lower precedence than non-numeric
        4. Shorter prerelease < longer if equal prefix
        """
        # Compare element by element
        for i in range(max(len(left), len(right))):
            # If one tuple is shorter, it has lower precedence
            if i >= len(left):
                return -1
            if i >= len(right):
                return 1

            left_id = left[i]
            right_id = right[i]

            # If identifiers are equal, continue
            if left_id == right_id:
                continue

            # Check if both are numeric
            left_is_numeric = left_id.isdigit()
            right_is_numeric = right_id.isdigit()

            if left_is_numeric and right_is_numeric:
                # Both numeric: compare as integers
                left_num = int(left_id)
                right_num = int(right_id)
                if left_num < right_num:
                    return -1
                elif left_num > right_num:
                    return 1
                # Equal (shouldn't happen due to earlier check, but safe)
                continue
            elif left_is_numeric:
                # Numeric < non-numeric
                return -1
            elif right_is_numeric:
                # Non-numeric > numeric
                return 1
            else:
                # Both non-numeric: lexicographic comparison
                if left_id < right_id:
                    return -1
                elif left_id > right_id:
                    return 1
                # Equal (shouldn't happen, but safe)
                continue

        # Tuples are equal
        return 0

    # ========================================================================
    # CANONICAL REPRESENTATION
    # ========================================================================

    def __str__(self) -> str:
        """
        Deterministic string representation.

        Format: MAJOR.MINOR.PATCH[-prerelease][+build]
        """
        base = f"{self.major}.{self.minor}.{self.patch}"

        if self.prerelease:
            prerelease_str = ".".join(self.prerelease)
            base = f"{base}-{prerelease_str}"

        if self.build:
            base = f"{base}+{self.build}"

        return base

    def __repr__(self) -> str:
        """Developer-friendly representation."""
        return (
            f"SchemaVersion("
            f"major={self.major}, "
            f"minor={self.minor}, "
            f"patch={self.patch}, "
            f"prerelease={self.prerelease!r}, "
            f"build={self.build!r}"
            f")"
        )

    # ========================================================================
    # HASH STABILITY
    # ========================================================================

    def __hash__(self) -> int:
        """
        Hash function. Same semantics as equality (build metadata excluded).
        """
        return hash(
            (
                self.major,
                self.minor,
                self.patch,
                self.prerelease or (),
            )
        )

    # ========================================================================
    # CONTROLLED PARSING (OPTIONAL BUT SAFE)
    # ========================================================================

    @classmethod
    def parse(cls, value: str) -> SchemaVersion:
        """
        Strict semantic version parser.

        Accepts:
            1.2.3
            1.2.3-alpha
            1.2.3-alpha.1
            1.2.3+build
            1.2.3-alpha+build

        No coercion.
        No partial matches.
        """
        import re

        pattern = (
            r"^(\d+)\.(\d+)\.(\d+)"
            r"(?:-([0-9A-Za-z\-.]+))?"
            r"(?:\+([0-9A-Za-z\-.]+))?$"
        )

        match = re.match(pattern, value)
        if not match:
            raise InvalidSchemaVersion(
                f"Invalid version string: {value}"
            )

        major, minor, patch, prerelease, build = match.groups()

        # Validate no leading zeros in core version components
        if len(major) > 1 and major[0] == '0':
            raise InvalidSchemaVersion(
                f"Major version cannot have leading zeros: {major}"
            )
        if len(minor) > 1 and minor[0] == '0':
            raise InvalidSchemaVersion(
                f"Minor version cannot have leading zeros: {minor}"
            )
        if len(patch) > 1 and patch[0] == '0':
            raise InvalidSchemaVersion(
                f"Patch version cannot have leading zeros: {patch}"
            )

        # Parse and validate prerelease identifiers
        prerelease_tuple = None
        if prerelease:
            identifiers = prerelease.split(".")
            # Validate each identifier
            for identifier in identifiers:
                # Reject leading zeros in numeric identifiers
                if identifier.isdigit() and len(identifier) > 1 and identifier[0] == '0':
                    raise InvalidSchemaVersion(
                        f"Numeric prerelease identifier cannot have leading zeros: {identifier}"
                    )
            prerelease_tuple = tuple(identifiers)

        return cls(
            major=int(major),
            minor=int(minor),
            patch=int(patch),
            prerelease=prerelease_tuple,
            build=build,
        )

    # ========================================================================
    # ADDITIONAL HELPERS (OPTIONAL)
    # ========================================================================

    def is_stable(self) -> bool:
        """Check if this is a stable release (no prerelease)."""
        return self.prerelease is None

    def bump_major(self) -> SchemaVersion:
        """Create new version with major bumped. Resets minor and patch."""
        return SchemaVersion(self.major + 1, 0, 0)

    def bump_minor(self) -> SchemaVersion:
        """Create new version with minor bumped. Resets patch."""
        return SchemaVersion(self.major, self.minor + 1, 0)

    def bump_patch(self) -> SchemaVersion:
        """Create new version with patch bumped."""
        return SchemaVersion(
            self.major, self.minor, self.patch + 1
        )
