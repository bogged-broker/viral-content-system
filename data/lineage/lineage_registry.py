"""
/data/lineage/lineage_registry.py

Lineage Policy & Authorization Authority
Explicit · Static · Deterministic · Non-Dynamic
"""

from __future__ import annotations

from typing import Dict, FrozenSet, NamedTuple, Optional, Tuple, List

from lineage_types import (
    ArtifactID,
    ArtifactType,
    LineageNodeID,
    MigrationID,
    SchemaVersionID,
    TransformationType,
)

__all__ = [
    "RegistryError",
    "MigrationSpec",
    "ALLOWED_ARTIFACT_TYPES",
    "ALLOWED_TRANSFORMATION_TYPES",
    "TRANSFORMATION_OUTPUT_RULES",
    "TRANSFORMATION_INPUT_RULES",
    "SCHEMA_TRANSITION_RULES",
    "MIGRATION_REGISTRY",
    "GENESIS_ALLOWED_TYPES",
    "validate_artifact_type",
    "validate_transformation_type",
    "validate_transformation_io",
    "validate_schema_transition",
    "validate_genesis",
    "validate_migration_id",
    "run_startup_self_check",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RegistryError(Exception):
    """
    Raised when any registry policy constraint is violated.
    Always fatal — no fallback, no permissive mode.
    """


class UnauthorizedArtifactTypeError(RegistryError):
    """ArtifactType is not declared in the registry."""


class UnauthorizedTransformationError(RegistryError):
    """TransformationType is not declared in the registry."""


class UnauthorizedTransformationIOError(RegistryError):
    """Transformation → input/output combination is not authorized."""


class UnauthorizedSchemaTransitionError(RegistryError):
    """Schema version transition is not declared."""


class UnauthorizedGenesisError(RegistryError):
    """ArtifactType is not permitted to exist without parents."""


class UnknownMigrationError(RegistryError):
    """MigrationID is not declared in the migration registry."""


class RegistrySelfCheckError(RegistryError):
    """Registry internal consistency check failed at startup."""


# ---------------------------------------------------------------------------
# MigrationSpec — value type for migration registry entries
# ---------------------------------------------------------------------------

class MigrationSpec(NamedTuple):
    """
    Immutable specification of a single valid migration path.

    One-to-one: every MigrationID maps to exactly one (artifact_type,
    from_version, to_version) triple. No two MigrationIDs may share the
    same triple, and no triple may be covered by more than one MigrationID.
    """
    artifact_type:  ArtifactType
    from_version:   SchemaVersionID
    to_version:     SchemaVersionID


# ---------------------------------------------------------------------------
# Schema transition key — hashable triple
# ---------------------------------------------------------------------------

class _SchemaTransitionKey(NamedTuple):
    artifact_type:  ArtifactType
    from_version:   SchemaVersionID
    to_version:     SchemaVersionID


# ---------------------------------------------------------------------------
# 1. Allowed Artifact Types
#
# Explicit allowlist. Every ArtifactType enum member must appear here.
# Enum members absent from this set are illegal regardless of whether the
# enum itself compiles. Prevents silent expansion from enum additions.
# ---------------------------------------------------------------------------

ALLOWED_ARTIFACT_TYPES: FrozenSet[ArtifactType] = frozenset({
    ArtifactType.CANONICAL_CONTENT,
    ArtifactType.CANONICAL_FACT,
    ArtifactType.AGGREGATE_WINDOW,
    ArtifactType.EXPERIMENT_STATE,
    ArtifactType.ACCOUNT_IDENTITY,
    ArtifactType.MIGRATION_SNAPSHOT,
    ArtifactType.RECOVERY_SUBGRAPH,
})


# ---------------------------------------------------------------------------
# 2. Allowed Transformation Types
#
# Same enforcement: every TransformationType enum member must be present.
# ---------------------------------------------------------------------------

ALLOWED_TRANSFORMATION_TYPES: FrozenSet[TransformationType] = frozenset({
    TransformationType.INGESTION,
    TransformationType.CANONICALIZATION,
    TransformationType.AGGREGATION,
    TransformationType.MIGRATION,
    TransformationType.RECOVERY_REPAIR,
    TransformationType.EXPERIMENT_ALLOCATION,
    TransformationType.DERIVATION,
    TransformationType.SNAPSHOT_RESTORE,
})


# ---------------------------------------------------------------------------
# 3. Transformation → Output Authorization
#
# For each TransformationType, the exact set of ArtifactType values it may
# produce. No wildcards. No catch-alls. Violation is fatal.
# ---------------------------------------------------------------------------

TRANSFORMATION_OUTPUT_RULES: Dict[TransformationType, FrozenSet[ArtifactType]] = {
    TransformationType.INGESTION: frozenset({
        ArtifactType.CANONICAL_CONTENT,
    }),
    TransformationType.CANONICALIZATION: frozenset({
        ArtifactType.CANONICAL_FACT,
    }),
    TransformationType.AGGREGATION: frozenset({
        ArtifactType.AGGREGATE_WINDOW,
    }),
    TransformationType.MIGRATION: frozenset({
        # Migration preserves the artifact family — output type == input type.
        # All migratable types listed explicitly; no implicit cross-type migration.
        ArtifactType.CANONICAL_CONTENT,
        ArtifactType.CANONICAL_FACT,
        ArtifactType.AGGREGATE_WINDOW,
        ArtifactType.EXPERIMENT_STATE,
        ArtifactType.ACCOUNT_IDENTITY,
        ArtifactType.MIGRATION_SNAPSHOT,
    }),
    TransformationType.RECOVERY_REPAIR: frozenset({
        ArtifactType.RECOVERY_SUBGRAPH,
    }),
    TransformationType.EXPERIMENT_ALLOCATION: frozenset({
        ArtifactType.EXPERIMENT_STATE,
    }),
    TransformationType.DERIVATION: frozenset({
        ArtifactType.CANONICAL_FACT,
        ArtifactType.AGGREGATE_WINDOW,
    }),
    TransformationType.SNAPSHOT_RESTORE: frozenset({
        ArtifactType.MIGRATION_SNAPSHOT,
    }),
}


# ---------------------------------------------------------------------------
# 4. Transformation → Input Authorization
#
# For each TransformationType, the exact set of ArtifactType values that
# may serve as inputs. Empty frozenset means NO input is accepted (genesis
# or single-parent genesis only — governed separately by GENESIS_ALLOWED_TYPES).
# ---------------------------------------------------------------------------

TRANSFORMATION_INPUT_RULES: Dict[TransformationType, FrozenSet[ArtifactType]] = {
    TransformationType.INGESTION: frozenset({
        # Raw ingestion has no lineage parents; enforced via GENESIS_ALLOWED_TYPES.
        # This set covers re-ingestion from a prior canonical artifact.
        ArtifactType.CANONICAL_CONTENT,
    }),
    TransformationType.CANONICALIZATION: frozenset({
        ArtifactType.CANONICAL_CONTENT,
    }),
    TransformationType.AGGREGATION: frozenset({
        ArtifactType.CANONICAL_FACT,
        ArtifactType.AGGREGATE_WINDOW,  # allows rolling/accumulating aggregations
    }),
    TransformationType.MIGRATION: frozenset({
        ArtifactType.CANONICAL_CONTENT,
        ArtifactType.CANONICAL_FACT,
        ArtifactType.AGGREGATE_WINDOW,
        ArtifactType.EXPERIMENT_STATE,
        ArtifactType.ACCOUNT_IDENTITY,
        ArtifactType.MIGRATION_SNAPSHOT,
    }),
    TransformationType.RECOVERY_REPAIR: frozenset({
        ArtifactType.CANONICAL_CONTENT,
        ArtifactType.CANONICAL_FACT,
        ArtifactType.AGGREGATE_WINDOW,
        ArtifactType.EXPERIMENT_STATE,
        ArtifactType.ACCOUNT_IDENTITY,
        ArtifactType.MIGRATION_SNAPSHOT,
        ArtifactType.RECOVERY_SUBGRAPH,
    }),
    TransformationType.EXPERIMENT_ALLOCATION: frozenset({
        ArtifactType.CANONICAL_FACT,
        ArtifactType.ACCOUNT_IDENTITY,
        ArtifactType.EXPERIMENT_STATE,
    }),
    TransformationType.DERIVATION: frozenset({
        ArtifactType.CANONICAL_FACT,
        ArtifactType.CANONICAL_CONTENT,
        ArtifactType.AGGREGATE_WINDOW,
    }),
    TransformationType.SNAPSHOT_RESTORE: frozenset({
        ArtifactType.CANONICAL_CONTENT,
        ArtifactType.CANONICAL_FACT,
        ArtifactType.AGGREGATE_WINDOW,
        ArtifactType.EXPERIMENT_STATE,
        ArtifactType.ACCOUNT_IDENTITY,
        ArtifactType.MIGRATION_SNAPSHOT,
    }),
}


# ---------------------------------------------------------------------------
# 5. Schema Transition Rules
#
# Maps (artifact_type, from_version, to_version) → Optional[MigrationID].
#   None  → identity transition (non-migration, same version, always valid).
#   MigrationID → the exact migration that authorises this version hop.
#
# Identity transitions (v → v) are not listed here; they are handled
# implicitly by validate_schema_transition(). Only cross-version transitions
# require a registry entry. Schema skipping (v → v+2) is only legal if an
# explicit entry exists for that exact triple.
# ---------------------------------------------------------------------------

_S = SchemaVersionID  # brevity alias for the table below

SCHEMA_TRANSITION_RULES: Dict[_SchemaTransitionKey, MigrationID] = {
    # CANONICAL_CONTENT: v1 → v2
    _SchemaTransitionKey(
        ArtifactType.CANONICAL_CONTENT,
        _S(1), _S(2),
    ): MigrationID("canonical_content_v1_to_v2"),

    # CANONICAL_FACT: v1 → v2
    _SchemaTransitionKey(
        ArtifactType.CANONICAL_FACT,
        _S(1), _S(2),
    ): MigrationID("canonical_fact_v1_to_v2"),

    # AGGREGATE_WINDOW: v1 → v2
    _SchemaTransitionKey(
        ArtifactType.AGGREGATE_WINDOW,
        _S(1), _S(2),
    ): MigrationID("aggregate_window_v1_to_v2"),

    # EXPERIMENT_STATE: v1 → v2
    _SchemaTransitionKey(
        ArtifactType.EXPERIMENT_STATE,
        _S(1), _S(2),
    ): MigrationID("experiment_state_v1_to_v2"),

    # ACCOUNT_IDENTITY: v1 → v2
    _SchemaTransitionKey(
        ArtifactType.ACCOUNT_IDENTITY,
        _S(1), _S(2),
    ): MigrationID("account_identity_v1_to_v2"),

    # MIGRATION_SNAPSHOT: v1 → v2
    _SchemaTransitionKey(
        ArtifactType.MIGRATION_SNAPSHOT,
        _S(1), _S(2),
    ): MigrationID("migration_snapshot_v1_to_v2"),
}


# ---------------------------------------------------------------------------
# 6. Migration Registry
#
# Single authoritative map: MigrationID → MigrationSpec.
# Every MigrationID referenced anywhere in the system must appear here.
# Each (artifact_type, from_version, to_version) triple must be unique —
# no two migrations may cover the same version transition for the same type.
# ---------------------------------------------------------------------------

MIGRATION_REGISTRY: Dict[MigrationID, MigrationSpec] = {
    MigrationID("canonical_content_v1_to_v2"): MigrationSpec(
        artifact_type=ArtifactType.CANONICAL_CONTENT,
        from_version=_S(1),
        to_version=_S(2),
    ),
    MigrationID("canonical_fact_v1_to_v2"): MigrationSpec(
        artifact_type=ArtifactType.CANONICAL_FACT,
        from_version=_S(1),
        to_version=_S(2),
    ),
    MigrationID("aggregate_window_v1_to_v2"): MigrationSpec(
        artifact_type=ArtifactType.AGGREGATE_WINDOW,
        from_version=_S(1),
        to_version=_S(2),
    ),
    MigrationID("experiment_state_v1_to_v2"): MigrationSpec(
        artifact_type=ArtifactType.EXPERIMENT_STATE,
        from_version=_S(1),
        to_version=_S(2),
    ),
    MigrationID("account_identity_v1_to_v2"): MigrationSpec(
        artifact_type=ArtifactType.ACCOUNT_IDENTITY,
        from_version=_S(1),
        to_version=_S(2),
    ),
    MigrationID("migration_snapshot_v1_to_v2"): MigrationSpec(
        artifact_type=ArtifactType.MIGRATION_SNAPSHOT,
        from_version=_S(1),
        to_version=_S(2),
    ),
}


# ---------------------------------------------------------------------------
# 7. Genesis Allowed Types
#
# Only these ArtifactType values may appear in a LineageRecord with zero
# input_artifact_ids. All others require at least one parent.
# ---------------------------------------------------------------------------

GENESIS_ALLOWED_TYPES: FrozenSet[ArtifactType] = frozenset({
    ArtifactType.CANONICAL_CONTENT,     # initial raw ingestion
    ArtifactType.MIGRATION_SNAPSHOT,    # bootstrap snapshot restore
})


# ---------------------------------------------------------------------------
# Validation helpers — pure, deterministic, side-effect-free
# ---------------------------------------------------------------------------

def validate_artifact_type(artifact_type: ArtifactType) -> None:
    """Raise UnauthorizedArtifactTypeError if the type is not in the allowlist."""
    if not isinstance(artifact_type, ArtifactType):
        raise TypeError(f"Expected ArtifactType, got {type(artifact_type)!r}")
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise UnauthorizedArtifactTypeError(
            f"ArtifactType {artifact_type.value!r} is not registered in the lineage registry."
        )


def validate_transformation_type(transformation_type: TransformationType) -> None:
    """Raise UnauthorizedTransformationError if the type is not in the allowlist."""
    if not isinstance(transformation_type, TransformationType):
        raise TypeError(f"Expected TransformationType, got {type(transformation_type)!r}")
    if transformation_type not in ALLOWED_TRANSFORMATION_TYPES:
        raise UnauthorizedTransformationError(
            f"TransformationType {transformation_type.value!r} is not registered "
            "in the lineage registry."
        )


def validate_transformation_io(
    transformation_type: TransformationType,
    input_types: FrozenSet[ArtifactType],
    output_type: ArtifactType,
) -> None:
    """
    Raise UnauthorizedTransformationIOError if the transformation is not
    authorized to consume *input_types* and produce *output_type*.

    input_types must be the set of distinct ArtifactType values across all
    input artifacts (may be empty for genesis records).
    """
    validate_transformation_type(transformation_type)
    validate_artifact_type(output_type)

    # Output check
    allowed_outputs = TRANSFORMATION_OUTPUT_RULES.get(transformation_type, frozenset())
    if output_type not in allowed_outputs:
        raise UnauthorizedTransformationIOError(
            f"TransformationType {transformation_type.value!r} is not authorized to produce "
            f"ArtifactType {output_type.value!r}. "
            f"Authorized outputs: {sorted(t.value for t in allowed_outputs)!r}."
        )

    # Input check (skip for empty input — genesis policy governs that path)
    if input_types:
        allowed_inputs = TRANSFORMATION_INPUT_RULES.get(transformation_type, frozenset())
        illegal_inputs = input_types - allowed_inputs
        if illegal_inputs:
            raise UnauthorizedTransformationIOError(
                f"TransformationType {transformation_type.value!r} is not authorized to consume "
                f"input type(s) {sorted(t.value for t in illegal_inputs)!r}. "
                f"Authorized inputs: {sorted(t.value for t in allowed_inputs)!r}."
            )


def validate_schema_transition(
    artifact_type: ArtifactType,
    from_version: SchemaVersionID,
    to_version: SchemaVersionID,
    migration_id: Optional[MigrationID],
) -> None:
    """
    Raise UnauthorizedSchemaTransitionError if the schema transition is not
    declared, or if the migration_id does not match the declared entry.

    Identity transitions (from_version == to_version) require migration_id=None.
    Cross-version transitions require a matching SCHEMA_TRANSITION_RULES entry
    and a migration_id that agrees with MIGRATION_REGISTRY.
    """
    validate_artifact_type(artifact_type)
    if not isinstance(from_version, SchemaVersionID):
        raise TypeError(f"Expected SchemaVersionID for from_version, got {type(from_version)!r}")
    if not isinstance(to_version, SchemaVersionID):
        raise TypeError(f"Expected SchemaVersionID for to_version, got {type(to_version)!r}")

    if from_version == to_version:
        # Identity transition — only valid for non-migration records
        if migration_id is not None:
            raise UnauthorizedSchemaTransitionError(
                f"Identity schema transition ({from_version} → {to_version}) for "
                f"{artifact_type.value!r} must not carry a migration_id, "
                f"got {migration_id.to_string()!r}."
            )
        return

    # Cross-version transition — must be declared
    key = _SchemaTransitionKey(artifact_type, from_version, to_version)
    if key not in SCHEMA_TRANSITION_RULES:
        raise UnauthorizedSchemaTransitionError(
            f"Schema transition {artifact_type.value!r} v{from_version} → v{to_version} "
            "is not declared in SCHEMA_TRANSITION_RULES. "
            "Add an explicit entry to authorise this transition."
        )

    declared_migration_id = SCHEMA_TRANSITION_RULES[key]

    if migration_id is None:
        raise UnauthorizedSchemaTransitionError(
            f"Schema transition {artifact_type.value!r} v{from_version} → v{to_version} "
            f"requires migration_id {declared_migration_id.to_string()!r}, but None was provided."
        )

    if migration_id != declared_migration_id:
        raise UnauthorizedSchemaTransitionError(
            f"Schema transition {artifact_type.value!r} v{from_version} → v{to_version} "
            f"requires migration_id {declared_migration_id.to_string()!r}, "
            f"but got {migration_id.to_string()!r}."
        )


def validate_genesis(artifact_type: ArtifactType) -> None:
    """
    Raise UnauthorizedGenesisError if this ArtifactType is not permitted
    to exist without parent artifacts.
    """
    validate_artifact_type(artifact_type)
    if artifact_type not in GENESIS_ALLOWED_TYPES:
        raise UnauthorizedGenesisError(
            f"ArtifactType {artifact_type.value!r} is not permitted as a genesis artifact "
            f"(zero parents). Authorized genesis types: "
            f"{sorted(t.value for t in GENESIS_ALLOWED_TYPES)!r}."
        )


def validate_migration_id(migration_id: MigrationID) -> MigrationSpec:
    """
    Raise UnknownMigrationError if the MigrationID is not in MIGRATION_REGISTRY.
    Returns the MigrationSpec on success.
    """
    if not isinstance(migration_id, MigrationID):
        raise TypeError(f"Expected MigrationID, got {type(migration_id)!r}")
    if migration_id not in MIGRATION_REGISTRY:
        raise UnknownMigrationError(
            f"MigrationID {migration_id.to_string()!r} is not declared in MIGRATION_REGISTRY."
        )
    return MIGRATION_REGISTRY[migration_id]


# ---------------------------------------------------------------------------
# Startup self-consistency check
# ---------------------------------------------------------------------------

def run_startup_self_check() -> None:
    """
    Validate the internal consistency of the registry at startup.

    Checks:
      1. Every ArtifactType enum member is in ALLOWED_ARTIFACT_TYPES.
      2. Every TransformationType enum member is in ALLOWED_TRANSFORMATION_TYPES.
      3. Every TransformationType has an entry in both IO rule dicts.
      4. Every ArtifactType referenced in IO rules is in ALLOWED_ARTIFACT_TYPES.
      5. Every MigrationID in SCHEMA_TRANSITION_RULES is in MIGRATION_REGISTRY.
      6. No two MigrationIDs share the same (artifact_type, from_v, to_v) triple.
      7. MIGRATION_REGISTRY triple ↔ SCHEMA_TRANSITION_RULES cross-consistency.
      8. No orphan MigrationIDs (declared in MIGRATION_REGISTRY but unreachable
         via SCHEMA_TRANSITION_RULES).
      9. Every genesis ArtifactType is in ALLOWED_ARTIFACT_TYPES.
      10. from_version != to_version for every SCHEMA_TRANSITION_RULES entry.

    Raises RegistrySelfCheckError with a full description of all violations found.
    """
    errors: list[str] = []

    # 1. All ArtifactType enum members declared
    all_artifact_types   = frozenset(ArtifactType)
    missing_artifact     = all_artifact_types - ALLOWED_ARTIFACT_TYPES
    if missing_artifact:
        errors.append(
            f"ArtifactType enum members not declared in ALLOWED_ARTIFACT_TYPES: "
            f"{sorted(t.value for t in missing_artifact)!r}."
        )

    # 2. All TransformationType enum members declared
    all_transform_types  = frozenset(TransformationType)
    missing_transform    = all_transform_types - ALLOWED_TRANSFORMATION_TYPES
    if missing_transform:
        errors.append(
            f"TransformationType enum members not declared in ALLOWED_TRANSFORMATION_TYPES: "
            f"{sorted(t.value for t in missing_transform)!r}."
        )

    # 3. Every TransformationType has IO rules
    for tt in ALLOWED_TRANSFORMATION_TYPES:
        if tt not in TRANSFORMATION_OUTPUT_RULES:
            errors.append(f"TransformationType {tt.value!r} has no TRANSFORMATION_OUTPUT_RULES entry.")
        elif not TRANSFORMATION_OUTPUT_RULES[tt]:
            errors.append(f"TransformationType {tt.value!r} TRANSFORMATION_OUTPUT_RULES entry is empty.")
        if tt not in TRANSFORMATION_INPUT_RULES:
            errors.append(f"TransformationType {tt.value!r} has no TRANSFORMATION_INPUT_RULES entry.")

    # 4. All ArtifactTypes referenced in IO rules are declared
    for tt, outputs in TRANSFORMATION_OUTPUT_RULES.items():
        illegal = outputs - ALLOWED_ARTIFACT_TYPES
        if illegal:
            errors.append(
                f"TRANSFORMATION_OUTPUT_RULES[{tt.value!r}] references undeclared "
                f"ArtifactType(s): {sorted(t.value for t in illegal)!r}."
            )
    for tt, inputs in TRANSFORMATION_INPUT_RULES.items():
        illegal = inputs - ALLOWED_ARTIFACT_TYPES
        if illegal:
            errors.append(
                f"TRANSFORMATION_INPUT_RULES[{tt.value!r}] references undeclared "
                f"ArtifactType(s): {sorted(t.value for t in illegal)!r}."
            )

    # 5. Every MigrationID in SCHEMA_TRANSITION_RULES is in MIGRATION_REGISTRY
    for key, mid in SCHEMA_TRANSITION_RULES.items():
        if mid not in MIGRATION_REGISTRY:
            errors.append(
                f"SCHEMA_TRANSITION_RULES entry {key!r} references MigrationID "
                f"{mid.to_string()!r} which is absent from MIGRATION_REGISTRY."
            )

    # 6. No two MigrationIDs share the same triple (uniqueness in MIGRATION_REGISTRY)
    seen_triples: dict[tuple, MigrationID] = {}
    for mid, spec in MIGRATION_REGISTRY.items():
        triple = (spec.artifact_type, spec.from_version, spec.to_version)
        if triple in seen_triples:
            errors.append(
                f"Duplicate migration triple {triple!r} covered by both "
                f"{seen_triples[triple].to_string()!r} and {mid.to_string()!r}."
            )
        else:
            seen_triples[triple] = mid

    # 7. Cross-consistency: MIGRATION_REGISTRY ↔ SCHEMA_TRANSITION_RULES
    for mid, spec in MIGRATION_REGISTRY.items():
        key = _SchemaTransitionKey(spec.artifact_type, spec.from_version, spec.to_version)
        if key not in SCHEMA_TRANSITION_RULES:
            errors.append(
                f"MIGRATION_REGISTRY entry {mid.to_string()!r} has no corresponding "
                f"SCHEMA_TRANSITION_RULES entry for {key!r}."
            )
        elif SCHEMA_TRANSITION_RULES[key] != mid:
            errors.append(
                f"MIGRATION_REGISTRY entry {mid.to_string()!r} disagrees with "
                f"SCHEMA_TRANSITION_RULES[{key!r}] = "
                f"{SCHEMA_TRANSITION_RULES[key].to_string()!r}."
            )

    # 8. No orphan MigrationIDs (reachability via SCHEMA_TRANSITION_RULES)
    reachable_mids = frozenset(SCHEMA_TRANSITION_RULES.values())
    orphan_mids    = frozenset(MIGRATION_REGISTRY.keys()) - reachable_mids
    if orphan_mids:
        errors.append(
            f"MigrationID(s) declared in MIGRATION_REGISTRY but unreachable via "
            f"SCHEMA_TRANSITION_RULES: {sorted(m.to_string() for m in orphan_mids)!r}."
        )

    # 9. Genesis types are declared artifact types
    illegal_genesis = GENESIS_ALLOWED_TYPES - ALLOWED_ARTIFACT_TYPES
    if illegal_genesis:
        errors.append(
            f"GENESIS_ALLOWED_TYPES references undeclared ArtifactType(s): "
            f"{sorted(t.value for t in illegal_genesis)!r}."
        )

    # 10. from_version != to_version for every schema transition rule
    for key in SCHEMA_TRANSITION_RULES:
        if key.from_version == key.to_version:
            errors.append(
                f"SCHEMA_TRANSITION_RULES contains identity transition "
                f"(from == to == {key.from_version!r}) for {key.artifact_type.value!r}. "
                "Identity transitions must not be listed explicitly."
            )

    if errors:
        formatted = "\n  ".join(f"[{i+1}] {e}" for i, e in enumerate(errors))
        raise RegistrySelfCheckError(
            f"Registry self-check failed with {len(errors)} violation(s):\n  {formatted}"
        )


# ---------------------------------------------------------------------------
# Module-level: run self-check on import.
#
# Policy consistency must be verified at import time — not lazily at first use.
# Any misconfiguration must abort startup before the system becomes operational.
# ---------------------------------------------------------------------------

run_startup_self_check()