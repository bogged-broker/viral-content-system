"""
/data/lineage/compatibility_matrix.py

Formal Schema Compatibility Authority
(Coexistence, Interoperability, Enforcement-Grade)

Authority Scope (spec §1):
  Defines formal compatibility contracts between schema versions.
  Governs: version coexistence, cross-version reference legality,
  reader compatibility, writer compatibility, deprecation enforcement,
  forbidden combinations, cross-artifact interaction compatibility.

Does NOT:
  Perform migrations, plan upgrades, transform artifacts, audit lineage.

Defines what is legally compatible inside the system.

Core Question It Answers (spec §2):
  > "Can these two schema versions legally exist or interact in the same operational state?"

Without heuristics. Without runtime guesswork. Formal matrix only.

Philosophy (spec §6):
  Nothing is compatible unless explicitly declared.
  Absent pair → forbidden. No defaults. No inference. No heuristics.
  Matrix lookup is O(1), deterministic, and side-effect-free.

Enforcement points (spec §9):
  artifact creation, migration execution, migration plan validation,
  snapshot sealing, rollback validation, orchestrator preflight,
  runtime deployment gate, external API serving layer.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Set, Tuple, List


# ──────────────────────────────────────────────────────────────────────────────
# Type Aliases
# ──────────────────────────────────────────────────────────────────────────────

SchemaVersionID = str
ArtifactType    = str
VersionPair     = Tuple[SchemaVersionID, SchemaVersionID]   # (A, B) ≠ (B, A)


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class CompatibilityError(Exception):
    """Base for all compatibility matrix failures."""

class MissingCompatibilityRuleError(CompatibilityError):
    """A version pair has no registered rule and is therefore forbidden."""

class ForbiddenPairError(CompatibilityError):
    """An explicitly forbidden version pair was queried in an enforcement context."""

class ContradictoryRuleError(CompatibilityError):
    """A rule set contains mutually contradictory declarations."""

class UnknownVersionError(CompatibilityError):
    """A version referenced in a rule is not registered with this matrix."""

class SymmetryViolationError(CompatibilityError):
    """Coexistence symmetry is violated: coexistence(A,B) ≠ coexistence(B,A)."""

class DeprecatedPairWindowExpiredError(CompatibilityError):
    """A deprecated pair is still active beyond its allowed tolerance window."""

class MatrixFrozenError(CompatibilityError):
    """Attempted to modify a frozen/sealed compatibility matrix."""

class ActivationError(CompatibilityError):
    """Matrix activation failed due to validation errors."""


# ──────────────────────────────────────────────────────────────────────────────
# CompatibilityRule
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CompatibilityRule:
    """
    Explicit compatibility contract between an ordered pair (from_version, to_version) (spec §5).
    
    All fields are required. No default-True behavior anywhere.
    All values explicit. No default True behavior. Matrix must be exhaustive for active versions.

    Directional semantics:
      backward_compatible: to_version can safely consume from_version data.
      forward_compatible:  from_version can safely consume to_version data.

    Invariants enforced at registration:
      - forbidden=True must not combine with coexistence=True or reference_allowed=True.
      - deprecated_pair=True requires coexistence=True (tolerated but flagged).
    """
    coexistence:         bool
    reference_allowed:   bool
    backward_compatible: bool
    forward_compatible:  bool
    deprecated_pair:     bool
    forbidden:           bool
    justification:       str

    def __post_init__(self) -> None:
        if self.forbidden and (self.coexistence or self.reference_allowed):
            raise ContradictoryRuleError(
                "A forbidden rule must not declare coexistence=True or "
                f"reference_allowed=True. justification={self.justification!r}"
            )
        if self.deprecated_pair and not self.coexistence:
            raise ContradictoryRuleError(
                "A deprecated_pair rule requires coexistence=True "
                "(it is tolerated but not allowed to vanish silently). "
                f"justification={self.justification!r}"
            )
        if self.reference_allowed and not self.coexistence:
            raise ContradictoryRuleError(
                "reference_allowed=True requires coexistence=True: "
                "you cannot reference an artifact that may not coexist. "
                f"justification={self.justification!r}"
            )

    def to_dict(self) -> dict:
        return {
            "coexistence":         self.coexistence,
            "reference_allowed":   self.reference_allowed,
            "backward_compatible": self.backward_compatible,
            "forward_compatible":  self.forward_compatible,
            "deprecated_pair":     self.deprecated_pair,
            "forbidden":           self.forbidden,
            "justification":       self.justification,
        }

    # ── Preset constructors for common patterns ───────────────────────────────

    @classmethod
    def full(cls, justification: str = "Non-breaking minor evolution") -> "CompatibilityRule":
        """Both versions fully interoperable in all directions."""
        return cls(
            coexistence=True, reference_allowed=True,
            backward_compatible=True, forward_compatible=True,
            deprecated_pair=False, forbidden=False,
            justification=justification,
        )

    @classmethod
    def backward_only(cls, justification: str) -> "CompatibilityRule":
        """Newer version can read older data; older cannot read newer."""
        return cls(
            coexistence=True, reference_allowed=True,
            backward_compatible=True, forward_compatible=False,
            deprecated_pair=False, forbidden=False,
            justification=justification,
        )

    @classmethod
    def breaking(cls, justification: str) -> "CompatibilityRule":
        """Full breaking change; coexistence forbidden without migration."""
        return cls(
            coexistence=False, reference_allowed=False,
            backward_compatible=False, forward_compatible=False,
            deprecated_pair=False, forbidden=True,
            justification=justification,
        )

    @classmethod
    def deprecated_window(cls, justification: str) -> "CompatibilityRule":
        """Transitional window; coexistence allowed but deprecation is flagged."""
        return cls(
            coexistence=True, reference_allowed=True,
            backward_compatible=True, forward_compatible=False,
            deprecated_pair=True, forbidden=False,
            justification=justification,
        )

    @classmethod
    def forbidden_pair(cls, justification: str) -> "CompatibilityRule":
        """Explicitly forbidden at all enforcement points."""
        return cls(
            coexistence=False, reference_allowed=False,
            backward_compatible=False, forward_compatible=False,
            deprecated_pair=False, forbidden=True,
            justification=justification,
        )


# ──────────────────────────────────────────────────────────────────────────────
# CompatibilityMatrix
# ──────────────────────────────────────────────────────────────────────────────

class CompatibilityMatrix:
    """
    Formal, enforcement-grade compatibility authority for a single ArtifactType (spec §4).

    Core Structure (spec §4):
      artifact_type: ArtifactType
      version_pairs: Dict[Tuple[SchemaVersionID, SchemaVersionID], CompatibilityRule]
    
    Each pair defines rules in both directions (A,B ≠ B,A necessarily).

    Keyed by ordered (from_version, to_version) pairs — direction matters.
    Absent pair → forbidden (closed-world assumption, spec §6).

    For cross-artifact-type constraints, use CrossTypeCompatibilityMatrix (spec §8).

    Thread-Safety (Tier-0):
      - Read operations are thread-safe via RLock
      - Write operations (register, freeze) are thread-safe via RLock
      - Once frozen, all mutations raise MatrixFrozenError

    Lifecycle:
      1. Construction: mutable registration phase
      2. Activation: validates coverage, symmetry, then freezes
      3. Frozen: immutable, read-only, thread-safe
    """

    def __init__(
        self,
        artifact_type: ArtifactType,
        known_versions: Optional[Set[SchemaVersionID]] = None,
        deprecated_window_seconds: Optional[float] = None,
    ) -> None:
        self._artifact_type  = artifact_type
        self._rules:   Dict[VersionPair, CompatibilityRule] = {}
        self._known:   Set[SchemaVersionID] = set(known_versions or [])
        self._frozen:  bool = False
        self._lock:    threading.RLock = threading.RLock()
        self._deprecated_window_seconds: Optional[float] = deprecated_window_seconds
        self._deprecated_pair_registration_time: Dict[VersionPair, float] = {}
        self._active_versions_on_activation: Optional[Set[SchemaVersionID]] = None

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        from_version: SchemaVersionID,
        to_version:   SchemaVersionID,
        rule:         CompatibilityRule,
    ) -> None:
        """
        Register an explicit compatibility rule for an ordered pair.
        Raises UnknownVersionError if either version is not in known_versions.
        Raises ContradictoryRuleError if a different rule already exists for this pair.
        Raises MatrixFrozenError if matrix is frozen.
        Raises SymmetryViolationError if coexistence symmetry is violated at registration-time.
        """
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError(
                    f"Cannot register rule for ({from_version!r}, {to_version!r}): "
                    f"matrix for {self._artifact_type!r} is frozen."
                )
            
            if self._known:
                for v in (from_version, to_version):
                    if v not in self._known:
                        raise UnknownVersionError(
                            f"Version {v!r} is not registered with this matrix "
                            f"(artifact_type={self._artifact_type!r})."
                        )
            pair = (from_version, to_version)
            if pair in self._rules:
                existing = self._rules[pair]
                if existing != rule:
                    raise ContradictoryRuleError(
                        f"Contradictory rule for pair {pair}: "
                        f"existing={existing.justification!r} new={rule.justification!r}"
                    )
                return  # idempotent re-registration of identical rule is fine
            
            # Registration-time symmetry validation (Improvement #5)
            inverse_pair = (to_version, from_version)
            if inverse_pair in self._rules:
                inverse_rule = self._rules[inverse_pair]
                if rule.coexistence != inverse_rule.coexistence:
                    raise SymmetryViolationError(
                        f"Registration-time symmetry violation: "
                        f"coexistence({from_version!r},{to_version!r})={rule.coexistence} but "
                        f"coexistence({to_version!r},{from_version!r})={inverse_rule.coexistence}. "
                        "Coexistence must be symmetric."
                    )
            
            self._rules[pair] = rule
            
            # Track deprecated pair registration time for intrinsic enforcement
            if rule.deprecated_pair:
                self._deprecated_pair_registration_time[pair] = time.time()

    def register_symmetric(
        self,
        v1:   SchemaVersionID,
        v2:   SchemaVersionID,
        rule: CompatibilityRule,
    ) -> None:
        """Register the same rule for both (v1,v2) and (v2,v1)."""
        self.register(v1, v2, rule)
        self.register(v2, v1, rule)

    def add_known_version(self, version_id: SchemaVersionID) -> None:
        """Add a known version. Raises MatrixFrozenError if frozen."""
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError(
                    f"Cannot add known version {version_id!r}: "
                    f"matrix for {self._artifact_type!r} is frozen."
                )
            self._known.add(version_id)

    # ── O(1) Deterministic Query API ─────────────────────────────────────────

    def _get_rule(
        self,
        from_version: SchemaVersionID,
        to_version:   SchemaVersionID,
        enforce: bool = True,
    ) -> Optional[CompatibilityRule]:
        """
        Return the rule for (from_version, to_version).
        If absent and enforce=True, raises MissingCompatibilityRuleError.
        Thread-safe read operation.
        """
        with self._lock:
            rule = self._rules.get((from_version, to_version))
            if rule is None and enforce:
                raise MissingCompatibilityRuleError(
                    f"No compatibility rule defined for "
                    f"({from_version!r}, {to_version!r}) in artifact_type={self._artifact_type!r}. "
                    "Absent pair is treated as forbidden (closed-world assumption)."
                )
            return rule

    def is_coexistent(
        self, v1: SchemaVersionID, v2: SchemaVersionID
    ) -> bool:
        """
        Deterministic validation method (spec §11).
        
        O(1), deterministic, no fallback inference, no runtime schema inspection.
        Matrix lookup only.
        """
        rule = self._get_rule(v1, v2)
        return rule.coexistence and not rule.forbidden   # type: ignore[union-attr]

    def is_reference_allowed(
        self, from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> bool:
        """
        Deterministic validation method (spec §11).
        
        O(1), deterministic, no fallback inference, no runtime schema inspection.
        Matrix lookup only.
        """
        rule = self._get_rule(from_version, to_version)
        return rule.reference_allowed and not rule.forbidden  # type: ignore[union-attr]

    def is_backward_compatible(
        self, from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> bool:
        """
        Deterministic validation method (spec §11).
        
        to_version can safely consume from_version data.
        
        O(1), deterministic, no fallback inference, no runtime schema inspection.
        Matrix lookup only.
        """
        rule = self._get_rule(from_version, to_version)
        return rule.backward_compatible   # type: ignore[union-attr]

    def is_forward_compatible(
        self, from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> bool:
        """
        Deterministic validation method (spec §11).
        
        from_version can safely consume to_version data.
        
        O(1), deterministic, no fallback inference, no runtime schema inspection.
        Matrix lookup only.
        """
        rule = self._get_rule(from_version, to_version)
        return rule.forward_compatible   # type: ignore[union-attr]

    def is_forbidden(
        self, v1: SchemaVersionID, v2: SchemaVersionID
    ) -> bool:
        """Thread-safe read operation."""
        with self._lock:
            rule = self._rules.get((v1, v2))
            if rule is None:
                return True   # absent == forbidden
            return rule.forbidden

    def is_deprecated_pair(
        self, v1: SchemaVersionID, v2: SchemaVersionID
    ) -> bool:
        rule = self._get_rule(v1, v2)
        return rule.deprecated_pair   # type: ignore[union-attr]

    def check_deprecated_window_expired(
        self, v1: SchemaVersionID, v2: SchemaVersionID
    ) -> bool:
        """
        Intrinsic deprecated window enforcement (Improvement #4).
        
        Returns True if the deprecated pair has expired beyond its tolerance window.
        Uses intrinsic policy clock (deprecated_window_seconds) if configured.
        
        Raises MissingCompatibilityRuleError if pair not found.
        """
        if self._deprecated_window_seconds is None:
            return False  # No window policy configured, external enforcement required
        
        pair = (v1, v2)
        rule = self._get_rule(v1, v2)
        
        if not rule.deprecated_pair:
            return False  # Not a deprecated pair
        
        registration_time = self._deprecated_pair_registration_time.get(pair)
        if registration_time is None:
            return False  # No registration time tracked (should not happen)
        
        elapsed = time.time() - registration_time
        return elapsed > self._deprecated_window_seconds

    def enforce_deprecated_window(
        self, v1: SchemaVersionID, v2: SchemaVersionID
    ) -> None:
        """
        Enforce deprecated window policy (Improvement #4).
        
        Raises DeprecatedPairWindowExpiredError if pair has expired.
        """
        if self.check_deprecated_window_expired(v1, v2):
            raise DeprecatedPairWindowExpiredError(
                f"Deprecated pair ({v1!r}, {v2!r}) in {self._artifact_type!r} "
                f"has exceeded tolerance window of {self._deprecated_window_seconds}s."
            )

    def get_rule(
        self, from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> CompatibilityRule:
        return self._get_rule(from_version, to_version)  # type: ignore[return-value]

    # ── Enforcement ───────────────────────────────────────────────────────────

    def enforce_coexistence(
        self, v1: SchemaVersionID, v2: SchemaVersionID
    ) -> None:
        """Raise ForbiddenPairError if these versions may not coexist."""
        if not self.is_coexistent(v1, v2):
            raise ForbiddenPairError(
                f"Coexistence of {v1!r} and {v2!r} is forbidden "
                f"(artifact_type={self._artifact_type!r})."
            )

    def enforce_reference(
        self, from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> None:
        """Raise ForbiddenPairError if a reference from from_version → to_version is illegal."""
        if not self.is_reference_allowed(from_version, to_version):
            raise ForbiddenPairError(
                f"Reference from {from_version!r} to {to_version!r} is forbidden "
                f"(artifact_type={self._artifact_type!r})."
            )

    def enforce_migration_legality(
        self, from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> None:
        """
        Enforce that a migration from_version → to_version is permitted.
        Checks that the pair is not forbidden; coexistence during transition
        is the migration window and may be handled by deprecated_pair rules.
        """
        rule = self._get_rule(from_version, to_version)
        if rule.forbidden:                                    # type: ignore[union-attr]
            raise ForbiddenPairError(
                f"Migration from {from_version!r} to {to_version!r} is explicitly "
                f"forbidden (artifact_type={self._artifact_type!r}). "
                f"Reason: {rule.justification!r}"            # type: ignore[union-attr]
            )

    # ── Activation & Freezing ─────────────────────────────────────────────────

    def activate(
        self,
        active_versions: Set[SchemaVersionID],
        enforce_coverage: bool = True,
        enforce_symmetry: bool = True,
    ) -> None:
        """
        Activate and freeze the matrix (Tier-0: Improvement #1, #2).
        
        Performs exhaustive validation:
          1. Coverage: all active version pairs must have rules (if enforce_coverage=True)
          2. Symmetry: coexistence must be symmetric (if enforce_symmetry=True)
          3. Freezes matrix: prevents all future mutations
        
        Raises ActivationError if validation fails.
        Raises MatrixFrozenError if already frozen.
        
        After activation, matrix is immutable and thread-safe for concurrent reads.
        """
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError(
                    f"Matrix for {self._artifact_type!r} is already frozen."
                )
            
            errors: list[str] = []
            
            # Coverage enforcement at activation (Improvement #2)
            if enforce_coverage:
                coverage_errors = self.validate_coverage(active_versions)
                if coverage_errors:
                    errors.extend(coverage_errors)
            
            # Symmetry validation (redundant check, but ensures completeness)
            if enforce_symmetry:
                try:
                    self._validate_coexistence_symmetry_internal()
                except SymmetryViolationError as exc:
                    errors.append(f"Symmetry violation: {exc}")
            
            if errors:
                raise ActivationError(
                    f"Matrix activation failed for {self._artifact_type!r}:\n" +
                    "\n".join(f"  - {e}" for e in errors)
                )
            
            # Freeze the matrix (Improvement #1)
            self._frozen = True
            self._active_versions_on_activation = frozenset(active_versions)
            
            # Convert mutable dicts to immutable views for thread-safety
            # (Rules dict remains but mutations are blocked by _frozen flag)

    def freeze(self) -> None:
        """
        Freeze the matrix without activation validation.
        Use activate() for full validation, or freeze() if validation is done externally.
        """
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError(
                    f"Matrix for {self._artifact_type!r} is already frozen."
                )
            self._frozen = True

    @property
    def is_frozen(self) -> bool:
        """Check if matrix is frozen. Thread-safe read."""
        with self._lock:
            return self._frozen

    # ── Symmetry Validation ───────────────────────────────────────────────────

    def validate_coexistence_symmetry(self) -> None:
        """
        Enforce: coexistence(A,B) == coexistence(B,A) for all registered pairs.
        Direction is asymmetric for read/write compat but never for coexistence.
        Raises SymmetryViolationError on first violation.
        """
        with self._lock:
            self._validate_coexistence_symmetry_internal()

    def _validate_coexistence_symmetry_internal(self) -> None:
        """Internal symmetry validation (assumes lock held)."""
        for (a, b), rule in self._rules.items():
            inverse = self._rules.get((b, a))
            if inverse is None:
                continue  # inverse absence handled by coverage validation
            if rule.coexistence != inverse.coexistence:
                raise SymmetryViolationError(
                    f"coexistence({a!r},{b!r})={rule.coexistence} but "
                    f"coexistence({b!r},{a!r})={inverse.coexistence}. "
                    "Coexistence must be symmetric."
                )

    # ── Coverage Validation ───────────────────────────────────────────────────

    def validate_coverage(
        self, active_versions: Set[SchemaVersionID]
    ) -> list[str]:
        """
        Return a list of error strings for any active version pair without a rule.
        An empty list means full coverage.
        """
        errors: list[str] = []
        versions = sorted(active_versions)
        for v1 in versions:
            for v2 in versions:
                if v1 == v2:
                    continue
                if (v1, v2) not in self._rules:
                    errors.append(
                        f"Missing compatibility rule: ({v1!r}, {v2!r}) "
                        f"in artifact_type={self._artifact_type!r}"
                    )
        return errors

    # ── Fingerprinting ────────────────────────────────────────────────────────

    def matrix_fingerprint(self) -> str:
        """
        Matrix fingerprinting (spec §14).
        
        Deterministic SHA-256 fingerprint of the entire matrix.
        Based on: sorted version pairs, sorted rule definitions, canonical serialization.
        
        Deterministic across machines. Used in:
          - Migration plan hash
          - Snapshot sealing
          - Merkle anchor payload
          - Audit export
        
        Sorted by pair key → canonical JSON → SHA-256.
        Byte-identical across machines for identical rule sets.
        """
        payload = {
            "artifact_type": self._artifact_type,
            "rules": {
                f"{a}::{b}": rule.to_dict()
                for (a, b), rule in sorted(self._rules.items())
            },
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def artifact_type(self) -> ArtifactType:
        return self._artifact_type

    @property
    def known_versions(self) -> FrozenSet[SchemaVersionID]:
        return frozenset(self._known)

    def all_rules(self) -> Dict[VersionPair, CompatibilityRule]:
        """Thread-safe read operation. Returns a copy."""
        with self._lock:
            return dict(self._rules)

    def deprecated_pairs(self) -> list[VersionPair]:
        """Thread-safe read operation."""
        with self._lock:
            return [pair for pair, rule in self._rules.items() if rule.deprecated_pair]

    def forbidden_pairs(self) -> list[VersionPair]:
        """Thread-safe read operation."""
        with self._lock:
            return [pair for pair, rule in self._rules.items() if rule.forbidden]

    @property
    def active_versions_on_activation(self) -> Optional[FrozenSet[SchemaVersionID]]:
        """Returns the active versions set at activation time, or None if not activated."""
        with self._lock:
            return self._active_versions_on_activation


# ──────────────────────────────────────────────────────────────────────────────
# CrossTypeCompatibilityMatrix
# ──────────────────────────────────────────────────────────────────────────────

class CrossTypeCompatibilityMatrix:
    """
    Governs compatibility between different ArtifactTypes.
    Example: Aggregate v3 may not legally reference Event v1.

    Keyed by (ArtifactType, SchemaVersionID, ArtifactType, SchemaVersionID).
    Absent pair → forbidden (same closed-world assumption).

    Thread-Safety (Tier-0):
      - Read operations are thread-safe via RLock
      - Write operations (register, freeze) are thread-safe via RLock
      - Once frozen, all mutations raise MatrixFrozenError
    """

    CrossKey = Tuple[ArtifactType, SchemaVersionID, ArtifactType, SchemaVersionID]

    def __init__(self) -> None:
        self._rules: Dict["CrossTypeCompatibilityMatrix.CrossKey", CompatibilityRule] = {}
        self._frozen: bool = False
        self._lock: threading.RLock = threading.RLock()

    def register(
        self,
        from_type:    ArtifactType,
        from_version: SchemaVersionID,
        to_type:      ArtifactType,
        to_version:   SchemaVersionID,
        rule:         CompatibilityRule,
    ) -> None:
        """Register cross-type rule. Raises MatrixFrozenError if frozen."""
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError(
                    f"Cannot register cross-type rule: matrix is frozen."
                )
            key = (from_type, from_version, to_type, to_version)
            if key in self._rules and self._rules[key] != rule:
                raise ContradictoryRuleError(
                    f"Contradictory cross-type rule for {key}."
                )
            self._rules[key] = rule

    def freeze(self) -> None:
        """Freeze the cross-type matrix, preventing all future mutations."""
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError("Cross-type matrix is already frozen.")
            self._frozen = True

    @property
    def is_frozen(self) -> bool:
        """Check if matrix is frozen. Thread-safe read."""
        with self._lock:
            return self._frozen

    def is_reference_allowed(
        self,
        from_type:    ArtifactType,
        from_version: SchemaVersionID,
        to_type:      ArtifactType,
        to_version:   SchemaVersionID,
    ) -> bool:
        """Thread-safe read operation."""
        with self._lock:
            key = (from_type, from_version, to_type, to_version)
            rule = self._rules.get(key)
            if rule is None:
                return False    # absent == forbidden
            return rule.reference_allowed and not rule.forbidden

    def enforce_reference(
        self,
        from_type:    ArtifactType,
        from_version: SchemaVersionID,
        to_type:      ArtifactType,
        to_version:   SchemaVersionID,
    ) -> None:
        if not self.is_reference_allowed(from_type, from_version, to_type, to_version):
            raise ForbiddenPairError(
                f"Cross-type reference from ({from_type!r}, {from_version!r}) "
                f"to ({to_type!r}, {to_version!r}) is forbidden."
            )

    def matrix_fingerprint(self) -> str:
        """Thread-safe read operation."""
        with self._lock:
            payload = {
                f"{ft}:{fv}->{tt}:{tv}": rule.to_dict()
                for (ft, fv, tt, tv), rule in sorted(self._rules.items())
            }
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(raw).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# MatrixRegistry  —  top-level authority aggregating all per-type matrices
# ──────────────────────────────────────────────────────────────────────────────

class MatrixRegistry:
    """
    Global compatibility authority.
    Aggregates per-ArtifactType matrices and the cross-type matrix into a single
    queryable, fingerprintable authority consulted at every enforcement point.

    Thread-Safety (Tier-0):
      - All operations are thread-safe via RLock
      - Once frozen, all mutations raise MatrixFrozenError
    """

    def __init__(self) -> None:
        self._matrices:   Dict[ArtifactType, CompatibilityMatrix] = {}
        self._cross_type: CrossTypeCompatibilityMatrix = CrossTypeCompatibilityMatrix()
        self._frozen: bool = False
        self._lock: threading.RLock = threading.RLock()

    # ── Registration ──────────────────────────────────────────────────────────

    def register_matrix(self, matrix: CompatibilityMatrix) -> None:
        """Register a matrix. Raises MatrixFrozenError if registry is frozen."""
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError(
                    "Cannot register matrix: registry is frozen."
                )
            art = matrix.artifact_type
            if art in self._matrices:
                raise CompatibilityError(
                    f"A matrix for artifact_type={art!r} is already registered."
                )
            self._matrices[art] = matrix

    def get_matrix(self, artifact_type: ArtifactType) -> CompatibilityMatrix:
        """Thread-safe read operation."""
        with self._lock:
            m = self._matrices.get(artifact_type)
            if m is None:
                raise MissingCompatibilityRuleError(
                    f"No compatibility matrix registered for artifact_type={artifact_type!r}."
                )
            return m

    @property
    def cross_type(self) -> CrossTypeCompatibilityMatrix:
        """Thread-safe read operation."""
        with self._lock:
            return self._cross_type

    # ── Activation & Freezing ─────────────────────────────────────────────────

    def activate_all(
        self,
        active_versions_by_type: Dict[ArtifactType, Set[SchemaVersionID]],
        enforce_coverage: bool = True,
        enforce_symmetry: bool = True,
    ) -> None:
        """
        Activate and freeze all registered matrices (Tier-0: Improvement #1, #2).
        
        Performs exhaustive validation across all matrices:
          1. Coverage: all active version pairs must have rules (if enforce_coverage=True)
          2. Symmetry: coexistence must be symmetric (if enforce_symmetry=True)
          3. Freezes all matrices and registry: prevents all future mutations
        
        Raises ActivationError if validation fails.
        Raises MatrixFrozenError if already frozen.
        """
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError("Registry is already frozen.")
            
            errors: list[str] = []
            
            # Activate each matrix
            for art, matrix in self._matrices.items():
                active_versions = active_versions_by_type.get(art, set())
                try:
                    matrix.activate(
                        active_versions,
                        enforce_coverage=enforce_coverage,
                        enforce_symmetry=enforce_symmetry,
                    )
                except ActivationError as exc:
                    errors.append(f"{art}: {exc}")
            
            if errors:
                raise ActivationError(
                    "Registry activation failed:\n" +
                    "\n".join(f"  - {e}" for e in errors)
                )
            
            # Freeze cross-type matrix
            self._cross_type.freeze()
            
            # Freeze registry
            self._frozen = True

    def freeze(self) -> None:
        """
        Freeze the registry and all matrices without activation validation.
        Use activate_all() for full validation, or freeze() if validation is done externally.
        """
        with self._lock:
            if self._frozen:
                raise MatrixFrozenError("Registry is already frozen.")
            
            # Freeze all matrices
            for matrix in self._matrices.values():
                if not matrix.is_frozen:
                    matrix.freeze()
            
            # Freeze cross-type matrix
            if not self._cross_type.is_frozen:
                self._cross_type.freeze()
            
            # Freeze registry
            self._frozen = True

    @property
    def is_frozen(self) -> bool:
        """Check if registry is frozen. Thread-safe read."""
        with self._lock:
            return self._frozen

    # ── Unified enforcement surface ───────────────────────────────────────────

    def enforce_coexistence(
        self, artifact_type: ArtifactType,
        v1: SchemaVersionID, v2: SchemaVersionID
    ) -> None:
        self.get_matrix(artifact_type).enforce_coexistence(v1, v2)

    def enforce_reference(
        self, artifact_type: ArtifactType,
        from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> None:
        self.get_matrix(artifact_type).enforce_reference(from_version, to_version)

    def enforce_migration_legality(
        self, artifact_type: ArtifactType,
        from_version: SchemaVersionID, to_version: SchemaVersionID
    ) -> None:
        self.get_matrix(artifact_type).enforce_migration_legality(from_version, to_version)

    # ── Global fingerprint ────────────────────────────────────────────────────

    def global_fingerprint(self) -> str:
        """
        Deterministic fingerprint over ALL registered matrices + cross-type matrix.
        Used in snapshot sealing, Merkle anchor payload, and deployment gating.
        Thread-safe read operation.
        """
        with self._lock:
            payload = {
                "per_type": {
                    art: m.matrix_fingerprint()
                    for art, m in sorted(self._matrices.items())
                },
                "cross_type": self._cross_type.matrix_fingerprint(),
            }
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(raw).hexdigest()

    # ── Global symmetry + coverage audit ─────────────────────────────────────

    def validate_all_symmetry(self) -> None:
        """Validate coexistence symmetry across all registered matrices."""
        for matrix in self._matrices.values():
            matrix.validate_coexistence_symmetry()

    def validate_all_coverage(
        self, active_versions_by_type: Dict[ArtifactType, Set[SchemaVersionID]]
    ) -> Dict[ArtifactType, list[str]]:
        """
        Return per-type coverage errors (spec §15: Failure Conditions).
        Empty inner list = full coverage for that type.
        
        Validates that all active version pairs have rules defined.
        """
        return {
            art: self._matrices[art].validate_coverage(active_versions_by_type.get(art, set()))
            for art in self._matrices
        }

    def validate_all_failure_conditions(
        self,
        active_versions_by_type: Dict[ArtifactType, Set[SchemaVersionID]],
        deprecated_window_expired: Optional[Dict[ArtifactType, Set[VersionPair]]] = None,
        live_forbidden_pairs: Optional[Dict[ArtifactType, Set[VersionPair]]] = None,
    ) -> list[str]:
        """
        Validate all failure conditions from spec §15.
        
        Checks:
          1. Missing rule for active version pair
          2. Symmetry required but not defined
          3. Version referenced not in schema_versions (handled by validate_coverage)
          4. Multiple contradictory rules exist (handled at registration)
          5. Deprecated pair active beyond allowed window
          6. Forbidden pair detected in live state
        
        Returns list of error strings. Empty list = all checks passed.
        """
        errors: list[str] = []
        
        # 1. Missing rule for active version pair
        coverage_errors = self.validate_all_coverage(active_versions_by_type)
        for art, art_errors in coverage_errors.items():
            errors.extend(art_errors)
        
        # 2. Symmetry required but not defined
        # (Handled by validate_all_symmetry, but we check here too)
        try:
            self.validate_all_symmetry()
        except SymmetryViolationError as exc:
            errors.append(f"Symmetry violation: {exc}")
        
        # 5. Deprecated pair active beyond allowed window (intrinsic enforcement)
        for art, matrix in self._matrices.items():
            # Use intrinsic deprecated window enforcement if available
            for pair in matrix.deprecated_pairs():
                try:
                    matrix.enforce_deprecated_window(pair[0], pair[1])
                except DeprecatedPairWindowExpiredError as exc:
                    errors.append(f"{art}: {exc}")
        
        # Also check external deprecated_window_expired if provided (backward compatibility)
        if deprecated_window_expired:
            for art, expired_pairs in deprecated_window_expired.items():
                if art in self._matrices:
                    matrix = self._matrices[art]
                    for pair in expired_pairs:
                        if matrix.is_deprecated_pair(pair[0], pair[1]):
                            # Intrinsic check already done above, but log external flag
                            errors.append(
                                f"Deprecated pair {pair} in {art!r} is still active "
                                "beyond allowed tolerance window (external flag)."
                            )
        
        # 6. Forbidden pair detected in live state
        if live_forbidden_pairs:
            for art, forbidden_pairs in live_forbidden_pairs.items():
                if art in self._matrices:
                    matrix = self._matrices[art]
                    for pair in forbidden_pairs:
                        if matrix.is_forbidden(pair[0], pair[1]):
                            errors.append(
                                f"Forbidden pair {pair} detected in live state for {art!r}."
                            )
        
        return errors

    def validate_migration_coordination(
        self,
        artifact_type: ArtifactType,
        from_version: SchemaVersionID,
        to_version: SchemaVersionID,
        active_versions: Set[SchemaVersionID],
        allow_temporary_coexistence: bool = False,
    ) -> None:
        """
        Migration coordination rule (spec §12).
        
        Refuse upgrade plan if target version incompatible with any still-active version
        unless policy permits temporary coexistence.
        
        This prevents half-migrated incompatible states.
        
        Raises ForbiddenPairError if migration would create incompatible coexistence.
        """
        matrix = self.get_matrix(artifact_type)
        
        # Check if target version can coexist with all active versions
        for active_version in active_versions:
            if active_version == to_version:
                continue  # Same version, always compatible
            
            # Check both directions for coexistence
            if not matrix.is_coexistent(to_version, active_version):
                if not allow_temporary_coexistence:
                    raise ForbiddenPairError(
                        f"Migration from {from_version!r} to {to_version!r} would create "
                        f"incompatible coexistence with active version {active_version!r} "
                        f"in {artifact_type!r}. Policy does not allow temporary coexistence."
                    )
                # If temporary coexistence allowed, check if it's deprecated (transition window)
                if not matrix.is_deprecated_pair(to_version, active_version):
                    raise ForbiddenPairError(
                        f"Migration from {from_version!r} to {to_version!r} would create "
                        f"incompatible coexistence with active version {active_version!r} "
                        f"in {artifact_type!r}. Even with temporary coexistence allowed, "
                        "the pair is not marked as deprecated (no transition window)."
                    )