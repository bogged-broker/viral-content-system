"""
/data/lineage/migration_plan.py

Deterministic Migration Planning Authority
Pre-Execution Analysis · Graph-Aware · Policy-Enforcing · Non-Mutating
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

import lineage_registry as _reg
import schema_versions as _sv
from lineage_graph import LineageGraph
from lineage_types import (
    ArtifactID,
    ArtifactType,
    MigrationID,
    SchemaVersionID,
    TransformationType,
)

__all__ = [
    "MigrationStep",
    "BlockedArtifact",
    "BlockReason",
    "MigrationPolicy",
    "MigrationPlan",
    "MigrationPlanner",
    "MigrationPlanError",
    "PlanMode",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MigrationPlanError(Exception):
    """
    Raised when the planner cannot produce a valid plan.
    Strict mode: any blocked artifact triggers this.
    Structural: invalid registry, graph, or schema state.
    """


# ---------------------------------------------------------------------------
# Block reason taxonomy
# ---------------------------------------------------------------------------

class BlockReason(str, Enum):
    MISSING_MIGRATION_RULE     = "MISSING_MIGRATION_RULE"
    ILLEGAL_VERSION_GAP        = "ILLEGAL_VERSION_GAP"
    DOWNGRADE_REQUESTED        = "DOWNGRADE_REQUESTED"
    ALREADY_AT_TARGET          = "ALREADY_AT_TARGET"
    DEPRECATED_TARGET          = "DEPRECATED_TARGET"
    ORPHAN_ARTIFACT            = "ORPHAN_ARTIFACT"
    FORK_CONFLICT              = "FORK_CONFLICT"       # multiple descendants of same type
    REGISTRY_MISALIGNMENT      = "REGISTRY_MISALIGNMENT"
    CORRUPTED_VERSION          = "CORRUPTED_VERSION"
    OUT_OF_SCOPE               = "OUT_OF_SCOPE"        # excluded by policy artifact_type_scope
    CONCURRENT_MIGRATION_RISK  = "CONCURRENT_MIGRATION_RISK"


# ---------------------------------------------------------------------------
# PlanMode
# ---------------------------------------------------------------------------

class PlanMode(str, Enum):
    STRICT   = "STRICT"    # any blocked artifact → plan generation fails
    ADVISORY = "ADVISORY"  # blocked artifacts recorded but plan still produced


# ---------------------------------------------------------------------------
# BlockedArtifact
# ---------------------------------------------------------------------------

class BlockedArtifact:
    """
    Immutable record of an artifact that cannot be included in the migration plan.
    Always surfaces in MigrationPlan.blocked_artifacts — never silently dropped.
    """

    __slots__ = ("artifact_id", "artifact_type", "current_version", "target_version",
                 "reason", "detail")

    def __init__(
        self,
        *,
        artifact_id:      ArtifactID,
        artifact_type:    ArtifactType,
        current_version:  SchemaVersionID,
        target_version:   Optional[SchemaVersionID],
        reason:           BlockReason,
        detail:           str,
    ) -> None:
        object.__setattr__(self, "artifact_id",     artifact_id)
        object.__setattr__(self, "artifact_type",   artifact_type)
        object.__setattr__(self, "current_version", current_version)
        object.__setattr__(self, "target_version",  target_version)
        object.__setattr__(self, "reason",          reason)
        object.__setattr__(self, "detail",          detail)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("BlockedArtifact is immutable.")

    def to_dict(self) -> dict:
        return {
            "artifact_id":     self.artifact_id.to_string(),
            "artifact_type":   self.artifact_type.value,
            "current_version": int(self.current_version),
            "target_version":  int(self.target_version) if self.target_version else None,
            "reason":          self.reason.value,
            "detail":          self.detail,
        }

    def __repr__(self) -> str:
        return (
            f"BlockedArtifact({self.artifact_id.to_string()!r}, "
            f"{self.reason.value}, {self.detail!r})"
        )


# ---------------------------------------------------------------------------
# MigrationStep
# ---------------------------------------------------------------------------

class MigrationStep:
    """
    Immutable specification of a single executor call.

    One MigrationStep → one migration_executor.execute_migration() invocation
    for a single consecutive ordinal hop. Multi-hop migrations are expanded
    into multiple MigrationSteps by the planner.
    """

    __slots__ = (
        "artifact_id",
        "artifact_type",
        "from_version",
        "to_version",
        "ordinal_from",
        "ordinal_to",
        "migration_id",
        "requires_chain",   # True if this step is part of a multi-hop chain
    )

    def __init__(
        self,
        *,
        artifact_id:    ArtifactID,
        artifact_type:  ArtifactType,
        from_version:   SchemaVersionID,
        to_version:     SchemaVersionID,
        ordinal_from:   int,
        ordinal_to:     int,
        migration_id:   MigrationID,
        requires_chain: bool = False,
    ) -> None:
        if ordinal_to != ordinal_from + 1:
            raise ValueError(
                f"MigrationStep ordinal_to must be ordinal_from + 1, "
                f"got {ordinal_from!r} → {ordinal_to!r}."
            )
        object.__setattr__(self, "artifact_id",    artifact_id)
        object.__setattr__(self, "artifact_type",  artifact_type)
        object.__setattr__(self, "from_version",   from_version)
        object.__setattr__(self, "to_version",     to_version)
        object.__setattr__(self, "ordinal_from",   ordinal_from)
        object.__setattr__(self, "ordinal_to",     ordinal_to)
        object.__setattr__(self, "migration_id",   migration_id)
        object.__setattr__(self, "requires_chain", requires_chain)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("MigrationStep is immutable.")

    def to_dict(self) -> dict:
        return {
            "artifact_id":    self.artifact_id.to_string(),
            "artifact_type":  self.artifact_type.value,
            "from_version":   int(self.from_version),
            "to_version":     int(self.to_version),
            "ordinal_from":   self.ordinal_from,
            "ordinal_to":     self.ordinal_to,
            "migration_id":   self.migration_id.to_string(),
            "requires_chain": self.requires_chain,
        }

    def __repr__(self) -> str:
        return (
            f"MigrationStep({self.artifact_id.to_string()!r}, "
            f"{self.artifact_type.value} v{self.from_version}→v{self.to_version})"
        )


# ---------------------------------------------------------------------------
# MigrationPolicy
# ---------------------------------------------------------------------------

class MigrationPolicy:
    """
    Immutable planning policy declaration.

    enforce_latest         — plan upgrades for every terminal artifact not at
                             the latest active version for its type.
    forbid_deprecated      — block any artifact currently at a deprecated version
                             that lacks a migration path.
    allow_partial_upgrade  — advisory mode: produce plan even if some artifacts
                             are blocked. In strict mode this is ignored.
    artifact_type_scope    — if set, only artifact types in this frozenset are
                             considered. None means all types.
    mode                   — STRICT or ADVISORY.
    """

    __slots__ = (
        "enforce_latest",
        "forbid_deprecated",
        "allow_partial_upgrade",
        "artifact_type_scope",
        "mode",
    )

    def __init__(
        self,
        *,
        enforce_latest:        bool = True,
        forbid_deprecated:     bool = True,
        allow_partial_upgrade: bool = False,
        artifact_type_scope:   Optional[FrozenSet[ArtifactType]] = None,
        mode:                  PlanMode = PlanMode.STRICT,
    ) -> None:
        if artifact_type_scope is not None:
            if not isinstance(artifact_type_scope, frozenset):
                raise TypeError("artifact_type_scope must be frozenset or None")
            for t in artifact_type_scope:
                if not isinstance(t, ArtifactType):
                    raise TypeError(f"artifact_type_scope entry must be ArtifactType, got {type(t)!r}")
        if not isinstance(mode, PlanMode):
            raise TypeError(f"mode must be PlanMode, got {type(mode)!r}")

        object.__setattr__(self, "enforce_latest",        enforce_latest)
        object.__setattr__(self, "forbid_deprecated",     forbid_deprecated)
        object.__setattr__(self, "allow_partial_upgrade", allow_partial_upgrade)
        object.__setattr__(self, "artifact_type_scope",   artifact_type_scope)
        object.__setattr__(self, "mode",                  mode)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("MigrationPolicy is immutable.")

    @classmethod
    def enforce_latest_strict(cls) -> "MigrationPolicy":
        """Canonical strict policy: every terminal artifact must reach latest version."""
        return cls(
            enforce_latest=True,
            forbid_deprecated=True,
            allow_partial_upgrade=False,
            mode=PlanMode.STRICT,
        )

    @classmethod
    def advisory(
        cls,
        scope: Optional[FrozenSet[ArtifactType]] = None,
    ) -> "MigrationPolicy":
        """Advisory policy: report blockages but produce the plan anyway."""
        return cls(
            enforce_latest=True,
            forbid_deprecated=True,
            allow_partial_upgrade=True,
            artifact_type_scope=scope,
            mode=PlanMode.ADVISORY,
        )

    def __repr__(self) -> str:
        return (
            f"MigrationPolicy(mode={self.mode.value}, "
            f"enforce_latest={self.enforce_latest}, "
            f"scope={self.artifact_type_scope!r})"
        )


# ---------------------------------------------------------------------------
# MigrationPlan
# ---------------------------------------------------------------------------

class MigrationPlan:
    """
    Immutable, deterministically-ordered schema upgrade plan.

    Produced exclusively by MigrationPlanner.build_plan().
    Never mutated after construction. Hashable for governance sign-off.

    steps                       — ordered tuple of MigrationStep objects
    blocked_artifacts           — artifacts excluded from the plan with reasons
    target_versions             — the declared target per ArtifactType
    requires_sequential_execution
                                — True if any steps share a dependency chain
    generation_hash             — deterministic fingerprint of the plan contents
    """

    __slots__ = (
        "steps",
        "total_steps",
        "affected_artifacts",
        "requires_sequential_execution",
        "blocked_artifacts",
        "target_versions",
        "generation_hash",
    )

    def __init__(
        self,
        *,
        steps:                        Tuple[MigrationStep, ...],
        blocked_artifacts:            Tuple[BlockedArtifact, ...],
        target_versions:              Dict[ArtifactType, SchemaVersionID],
        requires_sequential_execution: bool,
        generation_hash:              str,
    ) -> None:
        object.__setattr__(self, "steps",                         steps)
        object.__setattr__(self, "total_steps",                   len(steps))
        object.__setattr__(self, "affected_artifacts",            len({s.artifact_id for s in steps}))
        object.__setattr__(self, "requires_sequential_execution", requires_sequential_execution)
        object.__setattr__(self, "blocked_artifacts",             blocked_artifacts)
        object.__setattr__(self, "target_versions",               dict(target_versions))
        object.__setattr__(self, "generation_hash",               generation_hash)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("MigrationPlan is immutable.")

    @property
    def is_empty(self) -> bool:
        return self.total_steps == 0

    @property
    def has_blocked(self) -> bool:
        return bool(self.blocked_artifacts)

    def to_dict(self) -> dict:
        return {
            "generation_hash":               self.generation_hash,
            "total_steps":                   self.total_steps,
            "affected_artifacts":            self.affected_artifacts,
            "requires_sequential_execution": self.requires_sequential_execution,
            "blocked_count":                 len(self.blocked_artifacts),
            "target_versions":               {
                k.value: int(v) for k, v in sorted(
                    self.target_versions.items(), key=lambda x: x[0].value
                )
            },
            "steps":           [s.to_dict() for s in self.steps],
            "blocked_artifacts": [b.to_dict() for b in self.blocked_artifacts],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)

    def __repr__(self) -> str:
        return (
            f"MigrationPlan("
            f"steps={self.total_steps}, "
            f"artifacts={self.affected_artifacts}, "
            f"blocked={len(self.blocked_artifacts)}, "
            f"sequential={self.requires_sequential_execution}, "
            f"hash={self.generation_hash[:12]!r}...)"
        )


# ---------------------------------------------------------------------------
# Internal fingerprints
# ---------------------------------------------------------------------------

def _schema_registry_fingerprint() -> str:
    parts: List[str] = []
    for art in sorted(_sv.SCHEMA_REGISTRY.keys(), key=lambda a: a.value):
        for d in _sv.SCHEMA_REGISTRY[art]:
            parts.append(f"{art.value}:{d.ordinal}:{int(d.version_id)}:{d.deprecated}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _migration_registry_fingerprint() -> str:
    parts: List[str] = []
    for mid in sorted(_reg.MIGRATION_REGISTRY.keys(), key=lambda m: m.to_string()):
        spec = _reg.MIGRATION_REGISTRY[mid]
        parts.append(
            f"{mid.to_string()}:{spec.artifact_type.value}:"
            f"{int(spec.from_version)}:{int(spec.to_version)}"
        )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _compute_generation_hash(
    steps: List[MigrationStep],
    target_versions: Dict[ArtifactType, SchemaVersionID],
    schema_fp: str,
    migration_fp: str,
) -> str:
    """
    Deterministic generation hash over plan content + registry state.

    Identical lineage state + identical registries → identical hash.
    Any change to steps, targets, or registry definitions changes the hash.
    """
    steps_part = json.dumps(
        [s.to_dict() for s in steps],
        sort_keys=True, ensure_ascii=True,
    )
    targets_part = json.dumps(
        {k.value: int(v) for k, v in sorted(target_versions.items(), key=lambda x: x[0].value)},
        sort_keys=True, ensure_ascii=True,
    )
    combined = "\n".join([steps_part, targets_part, schema_fp, migration_fp])
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# MigrationPlanner
# ---------------------------------------------------------------------------

class MigrationPlanner:
    """
    Deterministic, non-mutating migration planning authority.

    Given a LineageGraph, a MigrationPolicy, and the current registry and
    schema state, computes the complete ordered set of MigrationStep objects
    required to bring all in-scope terminal artifacts to their target versions.

    This class never writes to the store, never appends to the graph,
    never loads artifact content, and never invokes migration functions.
    It is pure analysis.

    Usage::

        planner = MigrationPlanner(graph, policy)
        plan    = planner.build_plan()
        # inspect plan.steps and plan.blocked_artifacts
        # pass plan to executor layer for execution
    """

    __slots__ = ("_graph", "_policy", "_schema_fp", "_migration_fp")

    def __init__(
        self,
        graph:  LineageGraph,
        policy: MigrationPolicy,
    ) -> None:
        if not isinstance(graph, LineageGraph):
            raise TypeError(f"graph must be LineageGraph, got {type(graph)!r}")
        if not isinstance(policy, MigrationPolicy):
            raise TypeError(f"policy must be MigrationPolicy, got {type(policy)!r}")
        
        # Validate registries at initialization (spec §16: Failure Conditions)
        try:
            _sv.run_schema_startup_self_check()
        except _sv.SchemaVersionSelfCheckError as exc:
            raise MigrationPlanError(
                f"Schema registry validation failed: {exc}"
            ) from exc
        
        try:
            _reg.run_startup_self_check()
        except _reg.RegistrySelfCheckError as exc:
            raise MigrationPlanError(
                f"Migration registry validation failed: {exc}"
            ) from exc
        
        object.__setattr__(self, "_graph",        graph)
        object.__setattr__(self, "_policy",       policy)
        object.__setattr__(self, "_schema_fp",    _schema_registry_fingerprint())
        object.__setattr__(self, "_migration_fp", _migration_registry_fingerprint())

    def __setattr__(self, *_: object) -> None:
        raise TypeError("MigrationPlanner is not mutable after construction.")

    # -- primary API ---------------------------------------------------------

    def build_plan(
        self,
        target_overrides: Optional[Dict[ArtifactType, SchemaVersionID]] = None,
        exclude_artifacts: Optional[Set[ArtifactID]] = None,
    ) -> MigrationPlan:
        """
        Compute the deterministic migration plan.

        target_overrides: explicitly override the target version for one or more
            artifact types. Must be validated against the schema registry. If
            not provided, the latest active version is used for every type.

        exclude_artifacts: set of artifact IDs to exclude from planning (for replay
            safety — artifacts already upgraded in a previous partial execution).
            If provided, these artifacts are skipped entirely.

        Returns a MigrationPlan. In STRICT mode, raises MigrationPlanError if
        any artifact is blocked. In ADVISORY mode, blocked artifacts are recorded
        in the plan without raising.

        This method is idempotent and pure — calling it multiple times with the
        same graph state and policy produces an identical plan and hash.
        """
        policy:  MigrationPolicy = self._policy
        graph:   LineageGraph    = self._graph

        # 1. Resolve target versions per artifact type
        target_versions = self._resolve_target_versions(target_overrides)

        # 2. Validate graph integrity (spec §16: Failure Conditions)
        try:
            graph.validate_integrity()
        except Exception as exc:
            raise MigrationPlanError(
                f"Corrupt lineage state detected: {exc}"
            ) from exc
        
        # 3. Identify terminal artifacts per type (no descendants of same type)
        terminal_map = self._find_terminal_artifacts(target_versions)

        # 4. For each terminal artifact, compute steps or record blockage
        all_steps:    List[MigrationStep]  = []
        all_blocked:  List[BlockedArtifact] = []

        for artifact_type in sorted(target_versions.keys(), key=lambda a: a.value):
            target_vid  = target_versions[artifact_type]
            terminals   = terminal_map.get(artifact_type, [])

            # Sort artifacts deterministically for stable plan ordering
            for artifact_id in sorted(terminals, key=lambda a: a.to_string()):
                # Replay safety: skip already-upgraded artifacts (spec §13)
                if exclude_artifacts and artifact_id in exclude_artifacts:
                    continue
                
                # Check for fork conflict (multiple same-type descendants)
                # This was detected in _find_terminal_artifacts but we need to mark it here
                graph = self._graph
                try:
                    current_record = graph.get_record_by_artifact(artifact_id)
                    current_ver = current_record.output_schema_version
                except KeyError:
                    # Already handled as ORPHAN_ARTIFACT in _plan_artifact
                    continue
                
                child_records = [
                    r for r in graph.iter_records()
                    if (r.transformation_type is TransformationType.MIGRATION and
                        artifact_id in r.input_artifact_ids and
                        r.artifact_type == artifact_type)
                ]
                if len(child_records) > 1:
                    all_blocked.append(BlockedArtifact(
                        artifact_id=artifact_id,
                        artifact_type=artifact_type,
                        current_version=current_ver,
                        target_version=target_vid,
                        reason=BlockReason.FORK_CONFLICT,
                        detail=(
                            f"Artifact has {len(child_records)} same-type descendants. "
                            f"Fork conflicts prevent deterministic migration planning."
                        ),
                    ))
                    continue
                
                # Note: Concurrent migration risk (spec §8) is primarily an execution-time concern
                # (detected via locks in MigrationExecutor). The planner detects structural
                # dependencies that require sequential execution via _requires_sequential().
                # Runtime lock conflicts are not detectable at planning time since the planner
                # is non-mutating and has no access to execution state.
                
                steps, blocked = self._plan_artifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    target_version=target_vid,
                )
                all_steps.extend(steps)
                if blocked:
                    all_blocked.append(blocked)

        # 5. Apply deterministic sort across types + artifact IDs + ordinal_from
        all_steps.sort(key=lambda s: (
            s.artifact_type.value,
            s.artifact_id.to_string(),
            s.ordinal_from,
        ))

        # 6. Determine if sequential execution is required
        sequential = self._requires_sequential(all_steps)

        # 7. Compute generation hash
        gen_hash = _compute_generation_hash(
            all_steps, target_versions,
            self._schema_fp, self._migration_fp,
        )

        # 8. Strict mode: any blocked → fail
        if policy.mode is PlanMode.STRICT and all_blocked:
            reasons = "; ".join(
                f"{b.artifact_id.to_string()!r}: {b.reason.value}"
                for b in all_blocked
            )
            raise MigrationPlanError(
                f"Migration plan blocked in STRICT mode — {len(all_blocked)} "
                f"artifact(s) cannot be migrated:\n  {reasons}"
            )

        return MigrationPlan(
            steps=tuple(all_steps),
            blocked_artifacts=tuple(all_blocked),
            target_versions=target_versions,
            requires_sequential_execution=sequential,
            generation_hash=gen_hash,
        )

    # -- target resolution ---------------------------------------------------

    def _resolve_target_versions(
        self,
        overrides: Optional[Dict[ArtifactType, SchemaVersionID]],
    ) -> Dict[ArtifactType, SchemaVersionID]:
        """
        Build the target version map for all in-scope artifact types.

        Applies overrides after defaults. Validates every target against the
        schema registry. Raises MigrationPlanError on invalid overrides.
        """
        policy = self._policy
        scope  = policy.artifact_type_scope

        result: Dict[ArtifactType, SchemaVersionID] = {}
        for art in ArtifactType:
            # Check if artifact type is out of scope (spec §8: Blocked Artifact Detection)
            if scope is not None and art not in scope:
                # If explicitly requested via override, block it
                if overrides and art in overrides:
                    raise MigrationPlanError(
                        f"ArtifactType {art.value!r} is out of scope (policy.artifact_type_scope), "
                        f"but was explicitly requested via target_override. Use OUT_OF_SCOPE to block."
                    )
                # Otherwise, skip it silently (not in scope, not requested)
                continue
            
            if art not in _sv.SCHEMA_REGISTRY:
                continue
            if overrides and art in overrides:
                vid = overrides[art]
                try:
                    _sv.validate_version_exists(art, vid)
                    if policy.forbid_deprecated:
                        _sv.validate_production_version(art, vid)
                except Exception as exc:
                    raise MigrationPlanError(
                        f"Invalid target_override for {art.value!r}: {exc}"
                    ) from exc
                result[art] = vid
            else:
                result[art] = _sv.get_latest_version(art)

        return result

    # -- terminal artifact discovery -----------------------------------------

    def _find_terminal_artifacts(
        self,
        target_versions: Dict[ArtifactType, SchemaVersionID],
    ) -> Dict[ArtifactType, List[ArtifactID]]:
        """
        Find all terminal artifacts per in-scope artifact type.

        A terminal artifact is the output of a LineageRecord with no
        same-type descendants in the graph — i.e., it is a "head" version
        of an artifact lineage chain.

        We iterate over all records in topological order, tracking which
        artifact IDs have been superseded by a same-type child migration.

        Also detects fork conflicts (multiple same-type descendants) and
        duplicate artifact heads (same artifact_id appearing multiple times).
        """
        graph = self._graph

        # Map: artifact_id → artifact_type (for scope filtering)
        art_type_map:  Dict[ArtifactID, ArtifactType] = {}
        # Map: artifact_id → count of same-type children (for fork detection)
        child_count:   Dict[ArtifactID, int] = {}
        # Set of artifact_ids that have a same-type child
        has_child: Set[ArtifactID] = set()
        # Track output artifacts to detect duplicates (spec §16: duplicate heads)
        seen_outputs: Set[ArtifactID] = set()
        duplicate_heads: List[ArtifactID] = []

        for record in graph.iter_records():
            art  = record.artifact_type
            out  = record.output_artifact_id
            art_type_map[out] = art

            # Detect duplicate artifact heads (spec §16)
            if out in seen_outputs:
                duplicate_heads.append(out)
            seen_outputs.add(out)

            if art not in target_versions:
                continue

            # Mark parents that have a same-type migration child
            if record.transformation_type is TransformationType.MIGRATION:
                for parent_id in record.input_artifact_ids:
                    parent_art = art_type_map.get(parent_id)
                    if parent_art == art:
                        has_child.add(parent_id)
                        child_count[parent_id] = child_count.get(parent_id, 0) + 1

        # Fail on duplicate heads (spec §16: Failure Conditions)
        if duplicate_heads:
            dup_str = ", ".join(aid.to_string() for aid in sorted(duplicate_heads, key=lambda a: a.to_string())[:5])
            raise MigrationPlanError(
                f"Duplicate artifact heads detected: {len(duplicate_heads)} artifact(s) "
                f"appear as output in multiple records. Examples: {dup_str}"
            )

        # Detect fork conflicts (spec §8: Blocked Artifact Detection)
        fork_conflicts: List[ArtifactID] = []
        for artifact_id, count in child_count.items():
            if count > 1:
                fork_conflicts.append(artifact_id)

        if fork_conflicts:
            # In strict mode, fail immediately. In advisory, record as blocked.
            if self._policy.mode is PlanMode.STRICT:
                fork_str = ", ".join(aid.to_string() for aid in sorted(fork_conflicts, key=lambda a: a.to_string())[:5])
                raise MigrationPlanError(
                    f"Fork conflicts detected: {len(fork_conflicts)} artifact(s) have "
                    f"multiple same-type descendants. Examples: {fork_str}"
                )
            # In advisory mode, we'll mark these as blocked during planning

        # Terminals: in scope, no same-type child
        result: Dict[ArtifactType, List[ArtifactID]] = {}
        for artifact_id, art in art_type_map.items():
            if art not in target_versions:
                continue
            if artifact_id not in has_child:
                result.setdefault(art, []).append(artifact_id)

        return result

    # -- per-artifact planning -----------------------------------------------

    def _plan_artifact(
        self,
        artifact_id:    ArtifactID,
        artifact_type:  ArtifactType,
        target_version: SchemaVersionID,
    ) -> Tuple[List[MigrationStep], Optional[BlockedArtifact]]:
        """
        Compute the MigrationStep chain for a single artifact, or produce a
        BlockedArtifact if migration is not possible.

        Returns (steps, None) on success, ([], BlockedArtifact) on blockage.
        """
        graph = self._graph

        try:
            record = graph.get_record_by_artifact(artifact_id)
        except KeyError:
            return [], BlockedArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                current_version=SchemaVersionID(1),  # sentinel; cannot resolve
                target_version=target_version,
                reason=BlockReason.ORPHAN_ARTIFACT,
                detail=f"Artifact {artifact_id.to_string()!r} not found in graph.",
            )

        current_version = record.output_schema_version

        # Already at target
        if current_version == target_version:
            return [], None

        # Validate current version exists
        try:
            current_defn = _sv.validate_version_exists(artifact_type, current_version)
        except Exception as exc:
            return [], BlockedArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                current_version=current_version,
                target_version=target_version,
                reason=BlockReason.CORRUPTED_VERSION,
                detail=str(exc),
            )

        # Validate target version exists
        try:
            target_defn = _sv.validate_version_exists(artifact_type, target_version)
        except Exception as exc:
            return [], BlockedArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                current_version=current_version,
                target_version=target_version,
                reason=BlockReason.CORRUPTED_VERSION,
                detail=str(exc),
            )

        # Downgrade guard
        if target_defn.ordinal < current_defn.ordinal:
            return [], BlockedArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                current_version=current_version,
                target_version=target_version,
                reason=BlockReason.DOWNGRADE_REQUESTED,
                detail=(
                    f"Target ordinal {target_defn.ordinal} < current ordinal "
                    f"{current_defn.ordinal}. Downgrades are forbidden."
                ),
            )

        # Deprecated target guard
        if target_defn.deprecated:
            return [], BlockedArtifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                current_version=current_version,
                target_version=target_version,
                reason=BlockReason.DEPRECATED_TARGET,
                detail=f"Target version {target_version!r} is deprecated.",
            )

        # Walk the ordinal chain and resolve each step
        from lineage_registry import _SchemaTransitionKey  # type: ignore[import]

        steps:       List[MigrationStep] = []
        walk_defn    = current_defn
        multi_hop    = (target_defn.ordinal - current_defn.ordinal) > 1
        last_ordinal = walk_defn.ordinal

        while walk_defn.ordinal < target_defn.ordinal:
            next_defn = _sv.get_next_version(artifact_type, walk_defn.version_id)
            if next_defn is None:
                return [], BlockedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    current_version=current_version,
                    target_version=target_version,
                    reason=BlockReason.MISSING_MIGRATION_RULE,
                    detail=(
                        f"No next version exists after ordinal {walk_defn.ordinal} "
                        f"for {artifact_type.value!r}."
                    ),
                )

            # Detect illegal version gap (spec §8: Blocked Artifact Detection)
            if next_defn.ordinal != last_ordinal + 1:
                return [], BlockedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    current_version=current_version,
                    target_version=target_version,
                    reason=BlockReason.ILLEGAL_VERSION_GAP,
                    detail=(
                        f"Illegal ordinal gap: expected ordinal {last_ordinal + 1}, "
                        f"got {next_defn.ordinal} for {artifact_type.value!r}. "
                        f"Ordinal chain must be continuous."
                    ),
                )

            # Detect ordinal inconsistency (spec §16: Failure Conditions)
            if next_defn.ordinal <= walk_defn.ordinal:
                return [], BlockedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    current_version=current_version,
                    target_version=target_version,
                    reason=BlockReason.CORRUPTED_VERSION,
                    detail=(
                        f"Ordinal inconsistency: next version ordinal {next_defn.ordinal} "
                        f"is not greater than current ordinal {walk_defn.ordinal}."
                    ),
                )

            key = _SchemaTransitionKey(artifact_type, walk_defn.version_id, next_defn.version_id)
            mid = _reg.SCHEMA_TRANSITION_RULES.get(key)
            if mid is None:
                return [], BlockedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    current_version=current_version,
                    target_version=target_version,
                    reason=BlockReason.MISSING_MIGRATION_RULE,
                    detail=(
                        f"No SCHEMA_TRANSITION_RULES entry for "
                        f"{artifact_type.value!r} v{walk_defn.version_id} → "
                        f"v{next_defn.version_id}."
                    ),
                )

            if mid not in _reg.MIGRATION_REGISTRY:
                return [], BlockedArtifact(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    current_version=current_version,
                    target_version=target_version,
                    reason=BlockReason.REGISTRY_MISALIGNMENT,
                    detail=(
                        f"MigrationID {mid.to_string()!r} is in SCHEMA_TRANSITION_RULES "
                        "but absent from MIGRATION_REGISTRY."
                    ),
                )

            steps.append(MigrationStep(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                from_version=walk_defn.version_id,
                to_version=next_defn.version_id,
                ordinal_from=walk_defn.ordinal,
                ordinal_to=next_defn.ordinal,
                migration_id=mid,
                requires_chain=multi_hop,
            ))
            walk_defn = next_defn
            last_ordinal = next_defn.ordinal

        return steps, None

    # -- sequential execution analysis ---------------------------------------

    def _requires_sequential(self, steps: List[MigrationStep]) -> bool:
        """
        Determine whether the step list requires sequential execution.

        Sequential execution is required if:
          - Any artifact appears in more than one step (multi-hop chain), OR
          - Any artifact is a parent (in the lineage graph) of another artifact
            that also appears in the plan — executing the child before the parent
            would read a stale version.

        Returns True (conservative) if any dependency overlap is detected.
        Returns False only if all artifacts are provably independent.
        """
        if not steps:
            return False

        graph = self._graph

        # Multi-hop: artifact appears in more than one step
        step_artifact_ids: List[ArtifactID] = [s.artifact_id for s in steps]
        if len(step_artifact_ids) != len(set(step_artifact_ids)):
            return True

        # Parent-child interlock: any planned artifact is a graph-ancestor of another
        planned_set: Set[ArtifactID] = set(step_artifact_ids)
        for artifact_id in planned_set:
            # Walk ancestors
            frontier = list(graph.get_parents(artifact_id))
            visited:  Set[ArtifactID] = set()
            while frontier:
                parent = frontier.pop()
                if parent in visited:
                    continue
                visited.add(parent)
                if parent in planned_set:
                    return True   # ancestor of this artifact is also in the plan
                frontier.extend(graph.get_parents(parent))

        return False

    # -- governance export ---------------------------------------------------

    def export_plan_manifest(self, plan: MigrationPlan) -> str:
        """
        Export a deterministic, canonically-sorted JSON manifest of the plan.

        Suitable for:
          - Pre-migration governance approval
          - Change management audit trail
          - CI/CD plan hash verification
          - Post-execution comparison

        The manifest includes:
          - Step sequence with full detail
          - Artifact counts and version targets
          - Registry and schema fingerprints
          - Plan generation hash
          - Planning timestamp (spec §12: Governance Snapshot Export)
        """
        # Planning timestamp (spec §12)
        planning_timestamp = datetime.now(timezone.utc).isoformat()
        
        manifest = {
            "generation_hash":               plan.generation_hash,
            "schema_registry_fingerprint":   self._schema_fp,
            "migration_registry_fingerprint": self._migration_fp,
            "planning_timestamp":             planning_timestamp,
            "total_steps":                   plan.total_steps,
            "affected_artifacts":            plan.affected_artifacts,
            "blocked_count":                 len(plan.blocked_artifacts),
            "requires_sequential_execution": plan.requires_sequential_execution,
            "target_versions": {
                k.value: int(v)
                for k, v in sorted(plan.target_versions.items(), key=lambda x: x[0].value)
            },
            "steps":             [s.to_dict() for s in plan.steps],
            "blocked_artifacts": [b.to_dict() for b in plan.blocked_artifacts],
        }
        return json.dumps(manifest, sort_keys=True, ensure_ascii=True, indent=2)

    def __repr__(self) -> str:
        return (
            f"MigrationPlanner(policy={self._policy!r}, "
            f"schema_fp={self._schema_fp[:12]!r}...)"
        )