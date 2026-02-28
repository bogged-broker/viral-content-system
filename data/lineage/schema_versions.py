"""
/data/lineage/schema_versions.py

Canonical Schema Version Registry
(Static, Monotonic, Immutable, Registry-Aligned)

---

1️⃣ Authority Scope

This file is the single source of truth for:

All valid SchemaVersionID
Their artifact family association
Their ordinal ordering
Their compatibility relationships
Their deprecation status
Their release provenance

Nothing in the system may:

Define schema versions dynamically
Add versions via configuration
Modify version behavior at runtime

All schema evolution must originate here.

---

Final Definition

/data/lineage/schema_versions.py is:

> The immutable, monotonic, artifact-scoped schema timeline that defines the lawful structural evolution space of every artifact in the lineage system.

Without it: Versions are strings.

With it: Versions are governed chronology.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Tuple

from lineage_types import ArtifactType, SchemaVersionID

__all__ = [
    "SchemaVersionDefinition",
    "SchemaVersionError",
    "SCHEMA_REGISTRY",
    "get_current_version",
    "get_next_version",
    "get_previous_version",
    "validate_version_exists",
    "is_deprecated",
    "is_backward_compatible",
    "run_schema_startup_self_check",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SchemaVersionError(Exception):
    """Base class for all schema version violations. Always fatal."""


class UnknownSchemaVersionError(SchemaVersionError):
    """Version does not exist for the given ArtifactType."""


class UnknownArtifactFamilyError(SchemaVersionError):
    """ArtifactType has no schema version declarations."""


class DeprecatedVersionProductionError(SchemaVersionError):
    """Attempt to produce a new artifact at a deprecated schema version."""


class SchemaVersionSelfCheckError(SchemaVersionError):
    """Registry structural consistency check failed at startup."""


# ---------------------------------------------------------------------------
# SchemaVersionDefinition
# ---------------------------------------------------------------------------

class SchemaVersionDefinition:
    """
    Immutable declaration of a single schema version for one artifact family.

    Core Data Model:
        version                  — the SchemaVersionID this definition covers
        ordinal                  — strict 1-based sequential position within
                                   the artifact family's version history
        introduced_in_release    — release tag string (e.g. "2026.02.1");
                                   used for audit, governance, rollback tracing
        backward_compatible_with — immediately preceding version this version
                                   can be safely read by (None if not compatible)
        deprecated               — True if new production at this version is
                                   forbidden; historical reads remain valid

    Non-Negotiable Rules:
        - artifact_type is immutable and unique per version_id (implicit via registry)
        - ordinal starts at 1 per artifact family
        - Ordinals must be continuous: 1..N
        - No duplicate (artifact_type, version_id)
        - No duplicate (artifact_type, ordinal)
        - backward_compatible_with may only reference immediate predecessor
        - If deprecated=True, version remains valid historically

    Immutable after construction. Ordinal is the sole ordering authority.
    Version comparison must use ordinal, never semantic string parsing or lexicographic comparison.
    """

    __slots__ = (
        "version",
        "ordinal",
        "introduced_in_release",
        "backward_compatible_with",
        "deprecated",
    )

    def __init__(
        self,
        *,
        version: SchemaVersionID,
        ordinal: int,
        introduced_in_release: str,
        backward_compatible_with: Optional[SchemaVersionID] = None,
        deprecated: bool = False,
    ) -> None:
        if not isinstance(version, SchemaVersionID):
            raise TypeError(f"version must be SchemaVersionID, got {type(version)!r}")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise TypeError(f"ordinal must be a positive int >= 1, got {ordinal!r}")
        if not isinstance(introduced_in_release, str) or not introduced_in_release.strip():
            raise ValueError(
                f"introduced_in_release must be a non-empty string, got {introduced_in_release!r}"
            )
        if backward_compatible_with is not None and not isinstance(
            backward_compatible_with, SchemaVersionID
        ):
            raise TypeError(
                f"backward_compatible_with must be SchemaVersionID or None, "
                f"got {type(backward_compatible_with)!r}"
            )
        if not isinstance(deprecated, bool):
            raise TypeError(f"deprecated must be bool, got {type(deprecated)!r}")

        object.__setattr__(self, "version",                  version)
        object.__setattr__(self, "ordinal",                  ordinal)
        object.__setattr__(self, "introduced_in_release",    introduced_in_release)
        object.__setattr__(self, "backward_compatible_with", backward_compatible_with)
        object.__setattr__(self, "deprecated",               deprecated)

    # -- immutability --------------------------------------------------------

    def __setattr__(self, *_: object) -> None:
        raise TypeError("SchemaVersionDefinition is immutable.")

    def __delattr__(self, *_: object) -> None:
        raise TypeError("SchemaVersionDefinition is immutable.")

    # -- ordering (ordinal only) ---------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SchemaVersionDefinition):
            return NotImplemented
        return self.ordinal == other.ordinal and self.version == other.version

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SchemaVersionDefinition):
            return NotImplemented
        return self.ordinal < other.ordinal

    def __le__(self, other: object) -> bool:
        return self == other or self.__lt__(other)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SchemaVersionDefinition):
            return NotImplemented
        return other.__lt__(self)

    def __ge__(self, other: object) -> bool:
        return self == other or self.__gt__(other)

    def __hash__(self) -> int:
        return hash((self.version, self.ordinal))

    def __repr__(self) -> str:
        return (
            f"SchemaVersionDefinition("
            f"version={self.version!r}, "
            f"ordinal={self.ordinal!r}, "
            f"release={self.introduced_in_release!r}, "
            f"compat={self.backward_compatible_with!r}, "
            f"deprecated={self.deprecated!r}"
            f")"
        )


# ---------------------------------------------------------------------------
# Canonical Schema Registry
#
# Registry Structure:
#   SCHEMA_REGISTRY: Dict[
#       ArtifactType,
#       Tuple[SchemaVersionDefinition, ...]   # Ordered by ordinal
#   ]
#
# Properties:
#   - Immutable tuple (not list)
#   - Ordered strictly by ordinal
#   - No runtime mutation
#   - Loaded at import time
#
# Deterministic Ordering:
#   Version comparison must use ordinal, never:
#   - Semantic string parsing
#   - Lexicographic comparison
#   - Release string comparison
#
# Order is structural, not lexical.
#
# GOVERNANCE: This is the sole authoritative source of version history.
# Adding a version requires:
#   1. A new SchemaVersionDefinition entry below.
#   2. A corresponding MigrationID + MigrationSpec in lineage_registry.py.
#   3. A SCHEMA_TRANSITION_RULES entry in lineage_registry.py.
#   4. A migration implementation in the migration executor.
# Startup self-check will fail if these are inconsistent.
# ---------------------------------------------------------------------------

_S = SchemaVersionID  # brevity alias

SCHEMA_REGISTRY: Dict[ArtifactType, Tuple[SchemaVersionDefinition, ...]] = {

    ArtifactType.CANONICAL_CONTENT: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
        SchemaVersionDefinition(
            version=_S(2),
            ordinal=2,
            introduced_in_release="2026.02",
            backward_compatible_with=_S(1),
            deprecated=False,
        ),
    ),

    ArtifactType.CANONICAL_FACT: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
        SchemaVersionDefinition(
            version=_S(2),
            ordinal=2,
            introduced_in_release="2026.02",
            backward_compatible_with=_S(1),
            deprecated=False,
        ),
    ),

    ArtifactType.AGGREGATE_WINDOW: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
        SchemaVersionDefinition(
            version=_S(2),
            ordinal=2,
            introduced_in_release="2026.02",
            backward_compatible_with=_S(1),
            deprecated=False,
        ),
    ),

    ArtifactType.EXPERIMENT_STATE: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
        SchemaVersionDefinition(
            version=_S(2),
            ordinal=2,
            introduced_in_release="2026.02",
            backward_compatible_with=_S(1),
            deprecated=False,
        ),
    ),

    ArtifactType.ACCOUNT_IDENTITY: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
        SchemaVersionDefinition(
            version=_S(2),
            ordinal=2,
            introduced_in_release="2026.02",
            backward_compatible_with=_S(1),
            deprecated=False,
        ),
    ),

    ArtifactType.MIGRATION_SNAPSHOT: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
        SchemaVersionDefinition(
            version=_S(2),
            ordinal=2,
            introduced_in_release="2026.02",
            backward_compatible_with=_S(1),
            deprecated=False,
        ),
    ),

    ArtifactType.RECOVERY_SUBGRAPH: (
        SchemaVersionDefinition(
            version=_S(1),
            ordinal=1,
            introduced_in_release="2026.01",
            backward_compatible_with=None,
            deprecated=False,
        ),
    ),
}


# ---------------------------------------------------------------------------
# Internal lookup structures (built once at import time, never mutated)
# ---------------------------------------------------------------------------

# (artifact_type, version_id) → SchemaVersionDefinition
_LOOKUP: Dict[Tuple[ArtifactType, SchemaVersionID], SchemaVersionDefinition] = {}

# artifact_type → ordinal → SchemaVersionDefinition
_ORDINAL_LOOKUP: Dict[ArtifactType, Dict[int, SchemaVersionDefinition]] = {}

for _art, _defs in SCHEMA_REGISTRY.items():
    _ORDINAL_LOOKUP[_art] = {}
    for _d in _defs:
        _LOOKUP[(_art, _d.version)] = _d
        _ORDINAL_LOOKUP[_art][_d.ordinal] = _d


# ---------------------------------------------------------------------------
# Public query API — pure, deterministic, side-effect-free
# ---------------------------------------------------------------------------

def _get_definition(
    artifact_type: ArtifactType,
    version: SchemaVersionID,
) -> SchemaVersionDefinition:
    """Internal: retrieve definition or raise."""
    if artifact_type not in SCHEMA_REGISTRY:
        raise UnknownArtifactFamilyError(
            f"ArtifactType {artifact_type.value!r} has no entries in SCHEMA_REGISTRY."
        )
    key = (artifact_type, version)
    if key not in _LOOKUP:
        raise UnknownSchemaVersionError(
            f"SchemaVersionID {version!r} does not exist for "
            f"ArtifactType {artifact_type.value!r}."
        )
    return _LOOKUP[key]


def validate_version_exists(
    artifact_type: ArtifactType,
    version_id: SchemaVersionID,
) -> SchemaVersionDefinition:
    """
    Assert that *version_id* is a declared schema version for *artifact_type*.

    Returns the SchemaVersionDefinition on success.
    Raises UnknownSchemaVersionError or UnknownArtifactFamilyError on failure.

    Used during replay to reject artifacts whose versions are absent from history.

    Pure function: deterministic, side-effect-free.
    """
    if not isinstance(artifact_type, ArtifactType):
        raise TypeError(f"Expected ArtifactType, got {type(artifact_type)!r}")
    if not isinstance(version_id, SchemaVersionID):
        raise TypeError(f"Expected SchemaVersionID, got {type(version_id)!r}")
    return _get_definition(artifact_type, version_id)


def get_current_version(artifact_type: ArtifactType) -> SchemaVersionDefinition:
    """
    Return the latest (highest ordinal) non-deprecated SchemaVersionDefinition
    for *artifact_type*.

    Raises SchemaVersionError if no non-deprecated version exists.

    Pure function: deterministic, side-effect-free.
    """
    if artifact_type not in SCHEMA_REGISTRY:
        raise UnknownArtifactFamilyError(
            f"ArtifactType {artifact_type.value!r} has no entries in SCHEMA_REGISTRY."
        )
    candidates = [d for d in SCHEMA_REGISTRY[artifact_type] if not d.deprecated]
    if not candidates:
        raise SchemaVersionError(
            f"All schema versions for ArtifactType {artifact_type.value!r} are deprecated. "
            "A migration to a new version is required."
        )
    return max(candidates, key=lambda d: d.ordinal)


def get_latest_version(artifact_type: ArtifactType) -> SchemaVersionID:
    """
    Return the highest ordinal SchemaVersionID for *artifact_type*.

    Must not return deprecated-only version unless no active exists.
    Returns the version ID (not the definition) for consistency with spec.

    Pure function: deterministic, side-effect-free.
    """
    if artifact_type not in SCHEMA_REGISTRY:
        raise UnknownArtifactFamilyError(
            f"ArtifactType {artifact_type.value!r} has no entries in SCHEMA_REGISTRY."
        )
    defs = SCHEMA_REGISTRY[artifact_type]
    if not defs:
        raise UnknownArtifactFamilyError(
            f"ArtifactType {artifact_type.value!r} has no version definitions."
        )
    # Return highest ordinal (may be deprecated if all are deprecated)
    latest = max(defs, key=lambda d: d.ordinal)
    return latest.version


def get_version_definition(
    artifact_type: ArtifactType,
    version_id: SchemaVersionID,
) -> SchemaVersionDefinition:
    """
    Direct lookup of SchemaVersionDefinition, O(1).

    Pure function: deterministic, side-effect-free.
    """
    return _get_definition(artifact_type, version_id)


def get_next_version(
    artifact_type: ArtifactType,
    current_version: SchemaVersionID,
) -> Optional[SchemaVersionDefinition]:
    """
    Return the SchemaVersionDefinition whose ordinal is exactly
    current_version.ordinal + 1, or None if *current_version* is the latest.

    Ordering is by ordinal only — never lexical or semantic.

    Pure function: deterministic, side-effect-free.
    """
    definition = _get_definition(artifact_type, current_version)
    next_ordinal = definition.ordinal + 1
    return _ORDINAL_LOOKUP[artifact_type].get(next_ordinal)


def get_previous_version(
    artifact_type: ArtifactType,
    current_version: SchemaVersionID,
) -> Optional[SchemaVersionDefinition]:
    """
    Return the SchemaVersionDefinition whose ordinal is exactly
    current_version.ordinal - 1, or None if *current_version* is the first.
    """
    definition = _get_definition(artifact_type, current_version)
    prev_ordinal = definition.ordinal - 1
    if prev_ordinal < 1:
        return None
    return _ORDINAL_LOOKUP[artifact_type].get(prev_ordinal)


def is_deprecated(
    artifact_type: ArtifactType,
    version: SchemaVersionID,
) -> bool:
    """
    Return True if *version* is marked deprecated for *artifact_type*.
    Deprecated versions remain valid historically but must not be produced.
    """
    return _get_definition(artifact_type, version).deprecated


def validate_production_version(
    artifact_type: ArtifactType,
    version: SchemaVersionID,
) -> None:
    """
    Raise DeprecatedVersionProductionError if *version* is deprecated.

    Call before creating any new artifact at this version.
    Deprecated versions may exist in history but must not be produced anew.
    """
    defn = _get_definition(artifact_type, version)
    if defn.deprecated:
        current = get_current_version(artifact_type)
        raise DeprecatedVersionProductionError(
            f"Schema version {version!r} for ArtifactType {artifact_type.value!r} is deprecated. "
            f"New artifacts must be produced at version {current.version!r} or later. "
            "Migrate existing artifacts via the declared migration path."
        )


def is_backward_compatible(
    artifact_type: ArtifactType,
    newer: SchemaVersionID,
    older: SchemaVersionID,
) -> bool:
    """
    Return True only if:
      newer.ordinal > older.ordinal
      newer.backward_compatible_with == older.version_id

    No multi-hop implied compatibility.

    Pure function: deterministic, side-effect-free.
    """
    newer_defn = _get_definition(artifact_type, newer)
    older_defn = _get_definition(artifact_type, older)
    
    # Must be strictly newer
    if newer_defn.ordinal <= older_defn.ordinal:
        return False
    
    # Must explicitly reference the older version
    return newer_defn.backward_compatible_with == older


def get_version_chain(
    artifact_type: ArtifactType,
) -> Tuple[SchemaVersionDefinition, ...]:
    """
    Return all SchemaVersionDefinitions for *artifact_type* in strict ordinal
    order (ascending). Immutable tuple — the canonical ordered history.
    """
    if artifact_type not in SCHEMA_REGISTRY:
        raise UnknownArtifactFamilyError(
            f"ArtifactType {artifact_type.value!r} has no entries in SCHEMA_REGISTRY."
        )
    return SCHEMA_REGISTRY[artifact_type]  # already sorted by construction + self-check


# ---------------------------------------------------------------------------
# Startup self-consistency check
# ---------------------------------------------------------------------------

def run_schema_startup_self_check() -> None:
    """
    Validate the structural integrity of SCHEMA_REGISTRY on startup.

    Checks:
      1.  Every ArtifactType enum member has a SCHEMA_REGISTRY entry.
      2.  Each family's ordinals start at 1 and are strictly continuous.
      3.  No duplicate SchemaVersionIDs within a family.
      4.  No two families share the same (version_id, artifact_type) pair
          under a cross-family collision scan.
      5.  backward_compatible_with (if set) equals the immediately preceding
          version's SchemaVersionID — no skipping.
      6.  backward_compatible_with on ordinal-1 entries must be None.
      7.  Definitions are sorted by ascending ordinal (registry ordering
          must match declared ordinal).
      8.  introduced_in_release is non-empty and lexicographically
          non-decreasing across the version chain of each family.
      9.  No family has zero version definitions.
      10. If every version in a family is deprecated, a SchemaVersionError
          would result from get_current_version() — flag as a configuration
          warning (non-fatal but recorded; treat as error in strict mode).
      11. Alignment with lineage_registry.SCHEMA_TRANSITION_RULES:
          every cross-version step (ordinal N → N+1) that exists in the
          SCHEMA_REGISTRY must have a corresponding SCHEMA_TRANSITION_RULES
          entry if the step is not an identity (always true here, since
          ordinals differ), and vice versa — every transition declared in
          lineage_registry must correspond to a valid ordinal step here.

    Raises SchemaVersionSelfCheckError with all violations enumerated.
    """
    errors: List[str] = []

    # Registry not empty
    if not SCHEMA_REGISTRY:
        errors.append("SCHEMA_REGISTRY is empty — at least one artifact type must be registered.")

    # 1. Every ArtifactType has a registry entry (ArtifactTypes match known enum)
    all_artifact_types = frozenset(ArtifactType)
    missing_families   = all_artifact_types - frozenset(SCHEMA_REGISTRY.keys())
    if missing_families:
        errors.append(
            f"ArtifactType(s) missing from SCHEMA_REGISTRY: "
            f"{sorted(t.value for t in missing_families)!r}."
        )

    # Check for duplicate version IDs across artifact families (global uniqueness)
    global_version_ids: Dict[SchemaVersionID, List[Tuple[ArtifactType, int]]] = {}
    for art, defs in SCHEMA_REGISTRY.items():
        for d in defs:
            if d.version not in global_version_ids:
                global_version_ids[d.version] = []
            global_version_ids[d.version].append((art, d.ordinal))
    
    # Report cross-family duplicates (strongly recommended to be unique)
    for vid, occurrences in global_version_ids.items():
        if len(occurrences) > 1:
            families = ", ".join(f"{art.value}(ordinal={ord})" for art, ord in occurrences)
            errors.append(
                f"SchemaVersionID {vid!r} appears in multiple artifact families: {families}. "
                "Global version ID uniqueness is strongly recommended."
            )

    for art, defs in SCHEMA_REGISTRY.items():

        family_tag = f"ArtifactType.{art.value}"

        # 9. No empty families
        if not defs:
            errors.append(f"{family_tag}: version list is empty.")
            continue

        # 7. Sorted by ascending ordinal
        ordinals = [d.ordinal for d in defs]
        if ordinals != sorted(ordinals):
            errors.append(
                f"{family_tag}: definitions are not in ascending ordinal order: {ordinals!r}."
            )

        # 2. Ordinals start at 1 and are continuous
        expected_ordinals = list(range(1, len(defs) + 1))
        if ordinals != expected_ordinals:
            errors.append(
                f"{family_tag}: ordinals {ordinals!r} are not a continuous "
                f"sequence starting at 1 (expected {expected_ordinals!r})."
            )

        # 3. No duplicate SchemaVersionIDs within the family
        seen_vids: Dict[SchemaVersionID, int] = {}
        for d in defs:
            if d.version in seen_vids:
                errors.append(
                    f"{family_tag}: duplicate SchemaVersionID {d.version!r} "
                    f"at ordinals {seen_vids[d.version]!r} and {d.ordinal!r}."
                )
            else:
                seen_vids[d.version] = d.ordinal

        # 5 & 6. backward_compatible_with integrity
        for i, d in enumerate(defs):
            if d.ordinal == 1:
                # First version: compatibility must be None
                if d.backward_compatible_with is not None:
                    errors.append(
                        f"{family_tag} v{d.version} (ordinal=1): "
                        f"backward_compatible_with must be None for the first version, "
                        f"got {d.backward_compatible_with!r}."
                    )
            else:
                predecessor = defs[i - 1]
                if d.backward_compatible_with is not None:
                    if d.backward_compatible_with != predecessor.version:
                        errors.append(
                            f"{family_tag} v{d.version} (ordinal={d.ordinal}): "
                            f"backward_compatible_with must be the immediate predecessor "
                            f"v{predecessor.version!r} or None, "
                            f"got {d.backward_compatible_with!r}."
                        )

        # 8. introduced_in_release non-empty and lexicographically non-decreasing
        prev_release: Optional[str] = None
        for d in defs:
            if not d.introduced_in_release.strip():
                errors.append(
                    f"{family_tag} v{d.version} (ordinal={d.ordinal}): "
                    "introduced_in_release is empty."
                )
            elif prev_release is not None and d.introduced_in_release < prev_release:
                errors.append(
                    f"{family_tag} v{d.version} (ordinal={d.ordinal}): "
                    f"introduced_in_release {d.introduced_in_release!r} precedes "
                    f"predecessor release {prev_release!r}. "
                    "Release tags must be non-decreasing across version history."
                )
            prev_release = d.introduced_in_release

        # 10. Fully-deprecated family check
        if all(d.deprecated for d in defs):
            errors.append(
                f"{family_tag}: all versions are deprecated. "
                "get_current_version() will raise — a non-deprecated version must exist."
            )

    # 11. Registry alignment — deferred import to avoid circular imports at
    #     module level; the self-check is the only call site.
    try:
        import lineage_registry as _reg  # type: ignore[import]

        transition_rules = _reg.SCHEMA_TRANSITION_RULES
        migration_reg    = _reg.MIGRATION_REGISTRY

        # Every consecutive ordinal step in SCHEMA_REGISTRY must have a
        # SCHEMA_TRANSITION_RULES entry.
        from lineage_registry import _SchemaTransitionKey  # type: ignore[import]

        for art, defs in SCHEMA_REGISTRY.items():
            for i in range(len(defs) - 1):
                frm = defs[i].version
                to  = defs[i + 1].version
                key = _SchemaTransitionKey(art, frm, to)
                if key not in transition_rules:
                    errors.append(
                        f"ArtifactType.{art.value}: consecutive schema step "
                        f"v{frm} → v{to} has no SCHEMA_TRANSITION_RULES entry "
                        "in lineage_registry."
                    )
                else:
                    # The declared MigrationID must also be in MIGRATION_REGISTRY
                    mid = transition_rules[key]
                    if mid not in migration_reg:
                        errors.append(
                            f"ArtifactType.{art.value} v{frm} → v{to}: "
                            f"SCHEMA_TRANSITION_RULES references MigrationID "
                            f"{mid.to_string()!r} which is absent from MIGRATION_REGISTRY."
                        )

        # Every SCHEMA_TRANSITION_RULES entry must correspond to a valid
        # consecutive step in SCHEMA_REGISTRY.
        for key, mid in transition_rules.items():
            art, frm, to = key.artifact_type, key.from_version, key.to_version
            if art not in SCHEMA_REGISTRY:
                errors.append(
                    f"SCHEMA_TRANSITION_RULES references ArtifactType {art.value!r} "
                    "which is absent from SCHEMA_REGISTRY."
                )
                continue
            frm_def = _LOOKUP.get((art, frm))
            to_def  = _LOOKUP.get((art, to))
            if frm_def is None:
                errors.append(
                    f"SCHEMA_TRANSITION_RULES[{key!r}]: from_version {frm!r} "
                    f"not found in SCHEMA_REGISTRY for {art.value!r}."
                )
            if to_def is None:
                errors.append(
                    f"SCHEMA_TRANSITION_RULES[{key!r}]: to_version {to!r} "
                    f"not found in SCHEMA_REGISTRY for {art.value!r}."
                )
            if frm_def is not None and to_def is not None:
                if to_def.ordinal != frm_def.ordinal + 1:
                    errors.append(
                        f"SCHEMA_TRANSITION_RULES[{key!r}]: declares non-consecutive "
                        f"step v{frm}(ordinal={frm_def.ordinal}) → "
                        f"v{to}(ordinal={to_def.ordinal}). "
                        "Only consecutive ordinal steps are permitted unless "
                        "an explicit skip entry is declared."
                    )

    except ImportError:
        # lineage_registry not yet available (e.g. during isolated unit tests).
        # Registry alignment check is skipped; callers must ensure the full
        # startup sequence runs both self-checks together.
        pass

    if errors:
        formatted = "\n  ".join(f"[{i + 1}] {e}" for i, e in enumerate(errors))
        raise SchemaVersionSelfCheckError(
            f"Schema version self-check failed with {len(errors)} violation(s):\n  {formatted}"
        )


# ---------------------------------------------------------------------------
# Module-level: run self-check on import.
# Structural correctness of the version history must be verified before
# any code can query schema versions.
# ---------------------------------------------------------------------------

run_schema_startup_self_check()


# ---------------------------------------------------------------------------
# Absolute Invariants (Non-Negotiable)
# ---------------------------------------------------------------------------
#
# 1. Every version belongs to one artifact type.
# 2. Ordinals are continuous and start at 1.
# 3. Version comparison uses ordinal only.
# 4. Compatibility only references immediate predecessor.
# 5. No version deletion.
# 6. No downgrade allowed.
# 7. Registry immutable after import.
# 8. No implicit compatibility.
# 9. Migration registry must align.
# 10. Replay must validate version existence.
#
# ---------------------------------------------------------------------------
# Forbidden Behavior
# ---------------------------------------------------------------------------
#
# No environment-based branching
# No config-driven version enabling
# No runtime additions
# No version aliasing
# No cross-family reuse of version_id
# No downgrades permitted
#
# ---------------------------------------------------------------------------
# Security Properties
# ---------------------------------------------------------------------------
#
# Prevents:
# - Version spoofing
# - Schema injection
# - Replay divergence
# - Silent compatibility assumptions
# - Artifact type misalignment
# - Hidden version rewrite
# - Untracked schema evolution
#
# Schema history becomes immutable system history.
#
# ---------------------------------------------------------------------------
# Performance Constraints
# ---------------------------------------------------------------------------
#
# All lookups must be:
# - O(1) for version validation
# - O(1) for ordinal comparison
# - O(N) maximum only at startup validation
#
# No dynamic scanning in hot path.
#
# ---------------------------------------------------------------------------