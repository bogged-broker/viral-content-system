"""
/data/lineage/version_validator.py

Schema Version & Compatibility Integrity Enforcer
(Deterministic Structural Validator, Registry-Consistent)

Authority Scope (spec §1):
  Validates: schema version definitions, ordinal monotonicity, activation states,
  deprecation safety, compatibility matrix completeness, migration rule coverage,
  lifecycle coherence.

  Does NOT: execute migrations, plan migrations, modify schema definitions,
  modify compatibility rules.

  Audits static configuration for structural correctness.

Core Question It Answers (spec §2):
  > "Is this version history and its compatibility contract internally coherent
  and legally evolvable?"

  If this fails, your entire migration system becomes untrustworthy.

Validation Domains (spec §3):
  The validator covers six domains:
    1. Ordinal Structure
    2. Version Lifecycle States
    3. Migration Coverage
    4. Compatibility Matrix Coverage
    5. Registry Alignment
    6. Governance Constraints

  All deterministic.

Returns a fully structured ValidationReport. Never raises for expected
validation failures — every defect surfaces as a typed error or warning
inside the report.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import (, Tuple, List, Dict
    Any, Dict, FrozenSet, List, Optional, Protocol,
    Set, Tuple, runtime_checkable,
)


# ──────────────────────────────────────────────────────────────────────────────
# Type Aliases
# ──────────────────────────────────────────────────────────────────────────────

SchemaVersionID = str
ArtifactType    = str
MigrationRuleID = str


# ──────────────────────────────────────────────────────────────────────────────
# Version Lifecycle
# ──────────────────────────────────────────────────────────────────────────────

class VersionState(str, Enum):
    ACTIVE     = "active"
    DEPRECATED = "deprecated"
    RETIRED    = "retired"


# ──────────────────────────────────────────────────────────────────────────────
# Domain Objects (lightweight; validator is read-only)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SchemaVersion:
    version_id:    SchemaVersionID
    ordinal:       int
    state:         VersionState
    artifact_type: ArtifactType
    compatibility_profile_reference: Optional[str] = None   # must be set before activation


@dataclass(frozen=True)
class MigrationRuleDescriptor:
    rule_id:       MigrationRuleID
    artifact_type: ArtifactType
    from_version:  SchemaVersionID
    to_version:    SchemaVersionID
    is_rollback_class: bool = False


@dataclass(frozen=True)
class CompatibilityRuleDescriptor:
    """Lightweight mirror of CompatibilityRule for validator consumption."""
    from_version:        SchemaVersionID
    to_version:          SchemaVersionID
    coexistence:         bool
    reference_allowed:   bool
    backward_compatible: bool
    forward_compatible:  bool
    deprecated_pair:     bool
    forbidden:           bool

    def to_dict(self) -> dict:
        return {
            "from_version":        self.from_version,
            "to_version":          self.to_version,
            "coexistence":         self.coexistence,
            "reference_allowed":   self.reference_allowed,
            "backward_compatible": self.backward_compatible,
            "forward_compatible":  self.forward_compatible,
            "deprecated_pair":     self.deprecated_pair,
            "forbidden":           self.forbidden,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Validation Output Types
# ──────────────────────────────────────────────────────────────────────────────

class ValidationDomain(str, Enum):
    ORDINAL_STRUCTURE       = "ordinal_structure"
    LIFECYCLE_STATES        = "lifecycle_states"
    MIGRATION_COVERAGE      = "migration_coverage"
    COMPATIBILITY_COVERAGE  = "compatibility_coverage"
    REGISTRY_ALIGNMENT      = "registry_alignment"
    GOVERNANCE_CONSTRAINTS  = "governance_constraints"


@dataclass(frozen=True)
class ValidationError:
    domain:  ValidationDomain
    code:    str
    message: str
    context: str = ""

    def to_dict(self) -> dict:
        return {
            "domain":   self.domain.value,
            "code":     self.code,
            "message":  self.message,
            "context":  self.context,
        }


@dataclass(frozen=True)
class ValidationWarning:
    domain:  ValidationDomain
    code:    str
    message: str
    context: str = ""

    def to_dict(self) -> dict:
        return {
            "domain":   self.domain.value,
            "code":     self.code,
            "message":  self.message,
            "context":  self.context,
        }


@dataclass(frozen=True)
class ValidationReport:
    """
    Structured, immutable result of a full validate_all() run.
    valid=True iff errors is empty.
    fingerprint is computed regardless of validity.
    """
    valid:       bool
    errors:      Tuple[ValidationError, ...]
    warnings:    Tuple[ValidationWarning, ...]
    fingerprint: str    # version_graph_fingerprint at time of validation

    def to_dict(self) -> dict:
        return {
            "valid":       self.valid,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors":      [e.to_dict() for e in self.errors],
            "warnings":    [w.to_dict() for w in self.warnings],
            "fingerprint": self.fingerprint,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Governance Policy
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ValidatorGovernancePolicy:
    allow_multiple_active_versions:    bool = False
    allow_ordinal_gaps:                bool = False
    max_ordinal_gap:                   int  = 10
    ordinal_gap_warning_threshold:     int  = 5
    enforce_compatibility_profile_ref: bool = True
    strict_mode:                       bool = False     # warnings become errors
    max_migration_chain_length:        int  = 20        # warn beyond this
    allow_downgrade_migration_rules:   bool = False
    enforce_latest:                    bool = True      # enforce latest version governance (spec §11)


# ──────────────────────────────────────────────────────────────────────────────
# Protocols (dependency injection boundaries)
# ──────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class SchemaRegistryProtocol(Protocol):
    def get_versions(self, artifact_type: ArtifactType) -> List[SchemaVersion]: ...
    def get_migration_rules(self, artifact_type: ArtifactType) -> List[MigrationRuleDescriptor]: ...
    def get_compatibility_rules(self, artifact_type: ArtifactType) -> List[CompatibilityRuleDescriptor]: ...
    def get_all_artifact_types(self) -> List[ArtifactType]: ...
    def get_registry_fingerprint(self) -> str: ...


# ──────────────────────────────────────────────────────────────────────────────
# Internal Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _canonical_bytes(obj: Any) -> bytes:
    """
    Canonical JSON serialization for deterministic hashing.
    
    Uses sort_keys=True to ensure identical objects produce identical bytes
    regardless of dict insertion order or Python version.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _has_cycle(adjacency: Dict[SchemaVersionID, Set[SchemaVersionID]]) -> bool:
    """
    Iterative DFS cycle detection over directed migration graph.
    
    Formal Determinism Guarantee:
      - Nodes processed in sorted lexicographic order
      - Edges processed in sorted lexicographic order at each node
      - Visited set tracking ensures complete cycle detection
      - Identical graph structure → identical cycle detection result
    
    Algorithm: Three-color DFS (WHITE/GRAY/BLACK) with explicit visited tracking.
    Returns True if any cycle exists, False if graph is acyclic (DAG).
    
    Time: O(V + E) where V = nodes, E = edges
    Space: O(V) for color tracking
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[SchemaVersionID, int] = defaultdict(int)

    def dfs(node: SchemaVersionID) -> bool:
        # Sort children for deterministic traversal
        children = sorted(adjacency.get(node, set()))
        stack = [(node, iter(children))]
        color[node] = GRAY
        while stack:
            current, children_iter = stack[-1]
            try:
                child = next(children_iter)
                if color[child] == GRAY:
                    return True
                if color[child] == WHITE:
                    color[child] = GRAY
                    # Sort children for deterministic traversal
                    sorted_children = sorted(adjacency.get(child, set()))
                    stack.append((child, iter(sorted_children)))
            except StopIteration:
                color[current] = BLACK
                stack.pop()
        return False

    # Sort nodes for deterministic iteration
    for node in sorted(adjacency.keys()):
        if color[node] == WHITE:
            if dfs(node):
                return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# VersionValidator
# ──────────────────────────────────────────────────────────────────────────────

class VersionValidator:
    """
    Deterministic structural auditor of schema version history and compatibility
    governance (spec: /data/lineage/version_validator.py).

    Validation order is fixed and non-negotiable (spec §14):
      1. Ordinal structure
      2. Lifecycle states
      3. Migration coverage
      4. Compatibility matrix coverage
      5. Registry alignment
      6. Governance constraints

    Determinism guarantee (spec §17): identical configuration → identical report,
    fingerprint, and diagnostic ordering across all machines and Python versions.
    
    Formal Determinism Proof:
      All nondeterministic operations are explicitly canonicalized:
      
      1. Collection Iteration:
         - Artifact types: sorted() before iteration
         - Versions: sorted by ordinal before processing
         - Migration rules: sorted by (from_version, to_version)
         - Compatibility rules: sorted by (from_version, to_version)
         - Dict keys: sorted() before iteration (adjacency.keys())
         - Set elements: sorted() before iteration (neighbors in BFS/DFS)
      
      2. Graph Traversal:
         - Cycle detection: DFS with sorted node and edge iteration
         - BFS reachability: queue maintains sorted order at each level
         - Shortest path: neighbors processed in sorted order
      
      3. Error/Warning Ordering:
         - Final sort by (domain.value, code, message, context)
         - Ensures identical validation failures → identical error order
      
      4. Fingerprinting:
         - All data structures sorted before JSON serialization
         - JSON uses sort_keys=True for canonical key ordering
         - Policy state included in hash for complete determinism
         - SHA-256 ensures cryptographic stability
      
      5. Validation Domain Order:
         - Fixed sequence: 1→2→3→4→5→6 (non-negotiable)
         - No conditional skipping or reordering
      
    This guarantees: same input + same policy → same output (bit-identical).

    Security Guarantees (spec §18):
      Prevents: version drift corruption, silent breaking compatibility,
      migration deadlocks, rogue migration injection, lifecycle inconsistency,
      retired version resurrection, incoherent governance state,
      schema downgrade injection, compatibility matrix manipulation.

    It secures the evolution model.
    """

    def __init__(
        self,
        registry: SchemaRegistryProtocol,
        policy:   Optional[ValidatorGovernancePolicy] = None,
    ) -> None:
        self._registry = registry
        self._policy   = policy or ValidatorGovernancePolicy()

    # ── Public Entry Point ────────────────────────────────────────────────────

    def validate_all(self) -> ValidationReport:
        """
        Run all six validation domains in fixed order (spec §14).
        
        Validation order is strictly enforced and non-negotiable:
          1. Ordinal structure
          2. Lifecycle states
          3. Migration coverage
          4. Compatibility matrix coverage
          5. Registry alignment
          6. Governance constraints
        
        Returns a fully structured ValidationReport.
        Never raises for expected validation failures.
        
        Determinism: All iterations are over sorted sequences to ensure
        identical configuration produces identical reports across machines.
        """
        errors:   List[ValidationError]   = []
        warnings: List[ValidationWarning] = []

        fingerprint = self.version_graph_fingerprint()

        for artifact_type in sorted(self._registry.get_all_artifact_types()):
            versions     = sorted(
                self._registry.get_versions(artifact_type),
                key=lambda v: v.ordinal,
            )
            rules        = sorted(
                self._registry.get_migration_rules(artifact_type),
                key=lambda r: (r.from_version, r.to_version),
            )
            compat_rules = sorted(
                self._registry.get_compatibility_rules(artifact_type),
                key=lambda c: (c.from_version, c.to_version),
            )

            ctx = f"artifact_type={artifact_type!r}"

            # Domain 1 — Ordinal structure
            self._validate_ordinal_structure(versions, errors, warnings, ctx)

            # Domain 2 — Lifecycle states
            self._validate_lifecycle_states(versions, errors, warnings, ctx)

            # Domain 3 — Migration coverage
            self._validate_migration_coverage(versions, rules, errors, warnings, ctx)

            # Domain 4 — Compatibility coverage
            self._validate_compatibility_coverage(
                versions, compat_rules, errors, warnings, ctx
            )

            # Domain 5 — Registry alignment
            self._validate_registry_alignment(
                versions, rules, compat_rules, errors, warnings, ctx
            )

            # Domain 6 — Governance constraints
            self._validate_governance_constraints(
                versions, rules, compat_rules, errors, warnings, ctx
            )

        # Strict mode: promote warnings to errors
        if self._policy.strict_mode:
            errors.extend(
                ValidationError(
                    domain=w.domain,
                    code=f"STRICT_{w.code}",
                    message=f"[strict mode] {w.message}",
                    context=w.context,
                )
                for w in warnings
            )
            warnings = []

        # Sort errors and warnings for deterministic ordering
        # Sort by domain, then code, then message for stable ordering
        sorted_errors = sorted(
            errors,
            key=lambda e: (e.domain.value, e.code, e.message, e.context)
        )
        sorted_warnings = sorted(
            warnings,
            key=lambda w: (w.domain.value, w.code, w.message, w.context)
        )

        return ValidationReport(
            valid=len(sorted_errors) == 0,
            errors=tuple(sorted_errors),
            warnings=tuple(sorted_warnings),
            fingerprint=fingerprint,
        )

    # ── Domain 1: Ordinal Structure ───────────────────────────────────────────

    def _validate_ordinal_structure(
        self,
        versions:  List[SchemaVersion],
        errors:    List[ValidationError],
        warnings:  List[ValidationWarning],
        ctx:       str,
    ) -> None:
        D = ValidationDomain.ORDINAL_STRUCTURE
        seen_ordinals:    Dict[int, SchemaVersionID]  = {}
        seen_version_ids: Dict[SchemaVersionID, int]  = {}

        for v in versions:
            # Negative ordinal
            if v.ordinal < 0:
                errors.append(ValidationError(
                    D, "NEGATIVE_ORDINAL",
                    f"Version {v.version_id!r} has negative ordinal {v.ordinal}.",
                    ctx,
                ))

            # Duplicate ordinal
            if v.ordinal in seen_ordinals:
                errors.append(ValidationError(
                    D, "DUPLICATE_ORDINAL",
                    f"Ordinal {v.ordinal} shared by {v.version_id!r} and "
                    f"{seen_ordinals[v.ordinal]!r}.",
                    ctx,
                ))
            else:
                seen_ordinals[v.ordinal] = v.version_id

            # Duplicate version_id
            if v.version_id in seen_version_ids:
                errors.append(ValidationError(
                    D, "DUPLICATE_VERSION_ID",
                    f"Duplicate version_id {v.version_id!r} at "
                    f"ordinals {seen_version_ids[v.version_id]} and {v.ordinal}.",
                    ctx,
                ))
            else:
                seen_version_ids[v.version_id] = v.ordinal

        # Ordinal gap detection (on sorted list)
        ordinals = sorted(seen_ordinals.keys())
        for i in range(1, len(ordinals)):
            gap = ordinals[i] - ordinals[i - 1]
            if gap > 1:
                if not self._policy.allow_ordinal_gaps:
                    if gap > self._policy.max_ordinal_gap:
                        errors.append(ValidationError(
                            D, "EXCESSIVE_ORDINAL_GAP",
                            f"Ordinal gap of {gap} between "
                            f"{ordinals[i-1]} and {ordinals[i]}.",
                            ctx,
                        ))
                if gap > self._policy.ordinal_gap_warning_threshold:
                    warnings.append(ValidationWarning(
                        D, "LARGE_ORDINAL_GAP",
                        f"Ordinal gap of {gap} between "
                        f"{ordinals[i-1]} and {ordinals[i]}.",
                        ctx,
                    ))

    # ── Domain 2: Lifecycle States ────────────────────────────────────────────

    def _validate_lifecycle_states(
        self,
        versions: List[SchemaVersion],
        errors:   List[ValidationError],
        warnings: List[ValidationWarning],
        ctx:      str,
    ) -> None:
        """
        Version Lifecycle Rules Validation (spec §5).
        
        States allowed: active, deprecated, retired.
        
        Constraints:
          - Only one "latest active" version per artifact_type
          - Retired versions cannot have active compatibility edges
          - Deprecated versions must have explicit migration path to active
          - Active version cannot reference retired version in compatibility matrix
          - No future version may be marked retired
        
        Strict.
        """
        D = ValidationDomain.LIFECYCLE_STATES
        active = [v for v in versions if v.state == VersionState.ACTIVE]
        deprecated = [v for v in versions if v.state == VersionState.DEPRECATED]
        retired = [v for v in versions if v.state == VersionState.RETIRED]

        # Multiple active versions
        if len(active) > 1 and not self._policy.allow_multiple_active_versions:
            errors.append(ValidationError(
                D, "MULTIPLE_ACTIVE_VERSIONS",
                f"{len(active)} active versions found: "
                f"{[v.version_id for v in active]}. "
                "Policy requires exactly one active version.",
                ctx,
            ))
        elif len(active) > 1:
            warnings.append(ValidationWarning(
                D, "MULTIPLE_ACTIVE_VERSIONS",
                f"{len(active)} active versions; governance permits transitional window.",
                ctx,
            ))

        # Compatibility profile reference required for activation
        if self._policy.enforce_compatibility_profile_ref:
            for v in active + deprecated:
                if not v.compatibility_profile_reference:
                    errors.append(ValidationError(
                        D, "MISSING_COMPATIBILITY_PROFILE_REF",
                        f"Version {v.version_id!r} (state={v.state.value}) "
                        "has no compatibility_profile_reference. "
                        "Required before activation.",
                        ctx,
                    ))

        # Deprecated version with no upward active path
        active_ids = {v.version_id for v in active}
        deprecated_ids = {v.version_id for v in deprecated}
        retired_ids = {v.version_id for v in retired}

        # Deprecated without active target → strict error per spec
        if deprecated and not active:
            errors.append(ValidationError(
                D, "DEPRECATED_NO_ACTIVE_TARGET",
                f"Deprecated versions exist but no active version: "
                f"{[v.version_id for v in deprecated]}. "
                "Deprecated versions must have an active migration target.",
                ctx,
            ))

        # No future version may be marked retired (retired must be highest ordinal)
        if retired:
            max_ordinal = max(v.ordinal for v in versions)
            retired_ordinals = {v.ordinal for v in retired}
            future_retired = [v for v in retired if v.ordinal < max_ordinal]
            if future_retired:
                errors.append(ValidationError(
                    D, "FUTURE_VERSION_MARKED_RETIRED",
                    f"Versions {[v.version_id for v in future_retired]} are marked retired "
                    f"but are not at maximum ordinal {max_ordinal}. "
                    "Only the highest ordinal version may be retired.",
                    ctx,
                ))

    # ── Domain 3: Migration Coverage ──────────────────────────────────────────

    def _validate_migration_coverage(
        self,
        versions: List[SchemaVersion],
        rules:    List[MigrationRuleDescriptor],
        errors:   List[ValidationError],
        warnings: List[ValidationWarning],
        ctx:      str,
    ) -> None:
        D = ValidationDomain.MIGRATION_COVERAGE

        version_map = {v.version_id: v for v in versions}
        ordinal_map = {v.version_id: v.ordinal for v in versions}
        active_ids  = {v.version_id for v in versions if v.state == VersionState.ACTIVE}
        retired_ids = {v.version_id for v in versions if v.state == VersionState.RETIRED}
        non_retired = {v.version_id for v in versions
                       if v.state != VersionState.RETIRED}

        # Build directed adjacency (forward migration graph, non-rollback only)
        # Deterministic: rules are already sorted, so adjacency construction order
        # is deterministic. Set insertion order doesn't affect determinism since
        # we sort all set iterations when traversing.
        adjacency: Dict[SchemaVersionID, Set[SchemaVersionID]] = defaultdict(set)
        for r in rules:
            if not r.is_rollback_class:
                adjacency[r.from_version].add(r.to_version)

        # Cycle detection in migration graph
        # Formal guarantee: _has_cycle() uses sorted iteration, ensuring
        # identical graph structure → identical cycle detection result
        if _has_cycle(dict(adjacency)):
            errors.append(ValidationError(
                D, "MIGRATION_GRAPH_CYCLE",
                "Cycle detected in the migration rule graph. "
                "Migration rules must form a DAG.",
                ctx,
            ))

        # No downgrade rules unless policy permits
        if not self._policy.allow_downgrade_migration_rules:
            for r in rules:
                if not r.is_rollback_class:
                    fo = ordinal_map.get(r.from_version, -1)
                    to = ordinal_map.get(r.to_version, -1)
                    if to <= fo:
                        errors.append(ValidationError(
                            D, "ILLEGAL_DOWNGRADE_RULE",
                            f"Migration rule {r.rule_id!r} goes from ordinal {fo} "
                            f"to {to} (downgrade) without is_rollback_class=True.",
                            ctx,
                        ))

        # Reachability: every deprecated version must reach at least one active version
        active_ids_set = active_ids
        for v in versions:
            if v.state != VersionState.DEPRECATED:
                continue
            reachable = _bfs_reachable(v.version_id, adjacency)
            if not reachable & active_ids_set:
                errors.append(ValidationError(
                    D, "DEPRECATED_NO_UPGRADE_PATH",
                    f"Deprecated version {v.version_id!r} has no migration path "
                    "to any active version.",
                    ctx,
                ))

        # Warn on long migration chains
        for v in versions:
            if v.state in (VersionState.ACTIVE, VersionState.DEPRECATED):
                chain_len = _shortest_path_length(v.version_id, active_ids_set, adjacency)
                if (
                    chain_len is not None
                    and chain_len > self._policy.max_migration_chain_length
                ):
                    warnings.append(ValidationWarning(
                        D, "LONG_MIGRATION_CHAIN",
                        f"Version {v.version_id!r} requires {chain_len} hops to reach "
                        "an active version. Consider compaction.",
                        ctx,
                    ))

        # Retired versions must not be reachable via upgrade rules
        for r in rules:
            if not r.is_rollback_class and r.to_version in retired_ids:
                errors.append(ValidationError(
                    D, "MIGRATION_TARGETS_RETIRED",
                    f"Migration rule {r.rule_id!r} targets retired version "
                    f"{r.to_version!r}.",
                    ctx,
                ))

        # Ordinal-adjacency migration rule enforcement (spec requirement)
        # If version A(n) and B(n+1) both exist and are active/deprecated,
        # there must be a direct migration rule OR explicit policy gap declaration
        version_by_ordinal = {v.ordinal: v for v in versions}
        rule_pairs = {(r.from_version, r.to_version) for r in rules if not r.is_rollback_class}
        
        for ordinal in sorted(version_by_ordinal.keys()):
            v_n = version_by_ordinal.get(ordinal)
            v_n1 = version_by_ordinal.get(ordinal + 1)
            
            if v_n is None or v_n1 is None:
                continue
            
            # Both must be active or deprecated (not retired)
            if v_n.state == VersionState.RETIRED or v_n1.state == VersionState.RETIRED:
                continue
            
            # Check if direct migration rule exists
            has_direct_rule = (v_n.version_id, v_n1.version_id) in rule_pairs
            
            # If policy allows ordinal gaps, this is a legal gap
            if self._policy.allow_ordinal_gaps:
                continue
            
            # Otherwise, adjacency requires a direct rule
            if not has_direct_rule:
                errors.append(ValidationError(
                    D, "MISSING_ORDINAL_ADJACENCY_MIGRATION_RULE",
                    f"Versions {v_n.version_id!r} (ordinal {ordinal}) and "
                    f"{v_n1.version_id!r} (ordinal {ordinal + 1}) are both "
                    f"{v_n.state.value}/{v_n1.state.value} but no direct migration rule exists. "
                    "Adjacent ordinal versions must have explicit migration rule "
                    "or policy must allow ordinal gaps.",
                    ctx,
                ))

    # ── Domain 4: Compatibility Coverage ─────────────────────────────────────

    def _validate_compatibility_coverage(
        self,
        versions:     List[SchemaVersion],
        compat_rules: List[CompatibilityRuleDescriptor],
        errors:       List[ValidationError],
        warnings:     List[ValidationWarning],
        ctx:          str,
    ) -> None:
        D = ValidationDomain.COMPATIBILITY_COVERAGE

        non_retired = sorted(
            v.version_id for v in versions if v.state != VersionState.RETIRED
        )
        rule_index: Dict[Tuple[SchemaVersionID, SchemaVersionID], CompatibilityRuleDescriptor] = {
            (r.from_version, r.to_version): r for r in compat_rules
        }
        version_ids_in_schema = {v.version_id for v in versions}

        # Every non-retired pair must have an explicit rule
        for v1 in non_retired:
            for v2 in non_retired:
                if v1 == v2:
                    continue
                if (v1, v2) not in rule_index:
                    errors.append(ValidationError(
                        D, "MISSING_COMPATIBILITY_RULE",
                        f"No compatibility rule for pair ({v1!r}, {v2!r}).",
                        ctx,
                    ))

        # Rules must not reference unknown versions
        for r in compat_rules:
            for vid in (r.from_version, r.to_version):
                if vid not in version_ids_in_schema:
                    errors.append(ValidationError(
                        D, "UNKNOWN_VERSION_IN_COMPAT_RULE",
                        f"Compatibility rule ({r.from_version!r}, {r.to_version!r}) "
                        f"references unknown version {vid!r}.",
                        ctx,
                    ))

        # Retired versions must not appear in compat rules (except explicit policy)
        retired_ids = {v.version_id for v in versions if v.state == VersionState.RETIRED}
        active_ids = {v.version_id for v in versions if v.state == VersionState.ACTIVE}
        for r in compat_rules:
            if r.from_version in retired_ids or r.to_version in retired_ids:
                errors.append(ValidationError(
                    D, "RETIRED_VERSION_IN_COMPAT_RULE",
                    f"Compatibility rule ({r.from_version!r}, {r.to_version!r}) "
                    "involves a retired version.",
                    ctx,
                ))
        
        # Explicit check: Retired must not be compatible with active (governance invariant)
        for r in compat_rules:
            from_retired = r.from_version in retired_ids
            to_retired = r.to_version in retired_ids
            from_active = r.from_version in active_ids
            to_active = r.to_version in active_ids
            
            if (from_retired and to_active) or (from_active and to_retired):
                if r.coexistence or r.reference_allowed:
                    errors.append(ValidationError(
                        D, "RETIRED_ACTIVE_COMPATIBILITY_FORBIDDEN",
                        f"Compatibility rule ({r.from_version!r}, {r.to_version!r}): "
                        "Retired versions cannot be compatible with active versions. "
                        "This violates governance invariants.",
                        ctx,
                    ))
        
        # Explicit check: Active must not reference retired (governance invariant)
        for r in compat_rules:
            from_active = r.from_version in active_ids
            to_retired = r.to_version in retired_ids
            if from_active and to_retired and r.reference_allowed:
                errors.append(ValidationError(
                    D, "ACTIVE_REFERENCES_RETIRED",
                    f"Compatibility rule ({r.from_version!r}, {r.to_version!r}): "
                    "Active version cannot reference retired version. "
                    "This violates lifecycle governance.",
                    ctx,
                ))

        # Coexistence symmetry enforcement
        for r in compat_rules:
            inverse = rule_index.get((r.to_version, r.from_version))
            if inverse is None:
                continue    # missing inverse caught above
            if r.coexistence != inverse.coexistence:
                errors.append(ValidationError(
                    D, "COEXISTENCE_SYMMETRY_VIOLATION",
                    f"coexistence({r.from_version!r},{r.to_version!r})={r.coexistence} "
                    f"but coexistence({r.to_version!r},{r.from_version!r})="
                    f"{inverse.coexistence}. Coexistence must be symmetric.",
                    ctx,
                ))

        # Internal consistency: reference_allowed implies coexistence
        for r in compat_rules:
            if r.reference_allowed and not r.coexistence and not r.forbidden:
                errors.append(ValidationError(
                    D, "REFERENCE_WITHOUT_COEXISTENCE",
                    f"Compatibility rule ({r.from_version!r}, {r.to_version!r}): "
                    "reference_allowed=True requires coexistence=True.",
                    ctx,
                ))

        # Forbidden + coexistence contradiction
        for r in compat_rules:
            if r.forbidden and r.coexistence:
                errors.append(ValidationError(
                    D, "FORBIDDEN_COEXISTENCE_CONTRADICTION",
                    f"Compatibility rule ({r.from_version!r}, {r.to_version!r}): "
                    "forbidden=True contradicts coexistence=True.",
                    ctx,
                ))

    # ── Domain 5: Registry Alignment ─────────────────────────────────────────

    def _validate_registry_alignment(
        self,
        versions:     List[SchemaVersion],
        rules:        List[MigrationRuleDescriptor],
        compat_rules: List[CompatibilityRuleDescriptor],
        errors:       List[ValidationError],
        warnings:     List[ValidationWarning],
        ctx:          str,
    ) -> None:
        D = ValidationDomain.REGISTRY_ALIGNMENT

        version_ids   = {v.version_id for v in versions}
        ordinal_map   = {v.version_id: v.ordinal for v in versions}
        compat_index  = {
            (r.from_version, r.to_version): r for r in compat_rules
        }

        for r in rules:
            # from/to versions must exist
            for vid in (r.from_version, r.to_version):
                if vid not in version_ids:
                    errors.append(ValidationError(
                        D, "RULE_REFERENCES_UNKNOWN_VERSION",
                        f"Migration rule {r.rule_id!r} references unknown version {vid!r}.",
                        ctx,
                    ))

            # to_version ordinal must be > from_version ordinal unless rollback
            if not r.is_rollback_class:
                fo = ordinal_map.get(r.from_version, -1)
                to = ordinal_map.get(r.to_version, -1)
                if to <= fo:
                    errors.append(ValidationError(
                        D, "RULE_ORDINAL_REGRESSION",
                        f"Migration rule {r.rule_id!r}: "
                        f"to_version ordinal {to} ≤ from_version ordinal {fo}.",
                        ctx,
                    ))

            # Compatibility matrix must not forbid coexistence during migration
            compat = compat_index.get((r.from_version, r.to_version))
            if compat is not None and compat.forbidden:
                errors.append(ValidationError(
                    D, "MIGRATION_RULE_FORBIDDEN_BY_MATRIX",
                    f"Migration rule {r.rule_id!r} transitions "
                    f"{r.from_version!r} → {r.to_version!r} but the compatibility "
                    "matrix marks this pair as forbidden.",
                    ctx,
                ))

            # Lifecycle state check: cannot migrate from/to retired
            state_map = {v.version_id: v.state for v in versions}
            if state_map.get(r.to_version) == VersionState.RETIRED:
                errors.append(ValidationError(
                    D, "RULE_TARGETS_RETIRED_VERSION",
                    f"Migration rule {r.rule_id!r} targets retired version "
                    f"{r.to_version!r}.",
                    ctx,
                ))

    # ── Domain 6: Governance Constraints ─────────────────────────────────────

    def _validate_governance_constraints(
        self,
        versions:     List[SchemaVersion],
        rules:        List[MigrationRuleDescriptor],
        compat_rules: List[CompatibilityRuleDescriptor],
        errors:       List[ValidationError],
        warnings:     List[ValidationWarning],
        ctx:          str,
    ) -> None:
        """
        Governance Coherence Check (spec §11) and Forbidden State Detection (spec §10).
        
        Must enforce:
          - If policy says enforce_latest=True, then deprecated versions must have:
            - No active compatibility references
            - No reference_allowed=True edges with active version except during migration window
        
        Validator must detect:
          - Active + active incompatible coexistence
          - Deprecated version marked but no migration path exists
          - Retired version still compatible with active version
          - Version with no upward migration path
          - Compatibility rule that allows reference but forbids coexistence (inconsistent)
          - Migration rule allowing forbidden matrix pair
        
        Any forbidden state → fail.
        """
        D = ValidationDomain.GOVERNANCE_CONSTRAINTS

        active_ids     = {v.version_id for v in versions if v.state == VersionState.ACTIVE}
        deprecated_ids = {v.version_id for v in versions if v.state == VersionState.DEPRECATED}

        compat_index = {
            (r.from_version, r.to_version): r for r in compat_rules
        }

        # Active-active incompatible coexistence
        for v1 in sorted(active_ids):
            for v2 in sorted(active_ids):
                if v1 >= v2:
                    continue
                rule = compat_index.get((v1, v2))
                if rule is None or rule.forbidden or not rule.coexistence:
                    errors.append(ValidationError(
                        D, "ACTIVE_ACTIVE_INCOMPATIBLE_COEXISTENCE",
                        f"Active versions {v1!r} and {v2!r} cannot legally coexist "
                        "but both are marked active.",
                        ctx,
                    ))

        # enforce_latest governance policy (spec §11)
        if self._policy.enforce_latest:
            # Build migration adjacency to check for migration paths
            # Deterministic: rules are already sorted, adjacency construction is deterministic
            migration_adjacency: Dict[SchemaVersionID, Set[SchemaVersionID]] = defaultdict(set)
            for r in rules:
                if not r.is_rollback_class:
                    migration_adjacency[r.from_version].add(r.to_version)
            
            # Deprecated versions must not have active compatibility references
            # unless there's an active migration path (migration window)
            for dep_id in sorted(deprecated_ids):
                for act_id in sorted(active_ids):
                    rule = compat_index.get((dep_id, act_id))
                    if rule is None:
                        continue
                    
                    # Check if there's a migration path from deprecated to active
                    has_migration_path = act_id in _bfs_reachable(dep_id, migration_adjacency)
                    
                    # If enforce_latest is True, deprecated should not coexist with active
                    # unless explicitly marked as deprecated_pair (migration window)
                    if rule.coexistence and not rule.deprecated_pair:
                        errors.append(ValidationError(
                            D, "ENFORCE_LATEST_DEPRECATED_COEXISTENCE_VIOLATION",
                            f"Deprecated version {dep_id!r} has coexistence=True with "
                            f"active version {act_id!r} but enforce_latest=True requires "
                            "deprecated_pair flag for migration window.",
                            ctx,
                        ))
                    
                    # Reference allowed only during migration window (deprecated_pair or migration path)
                    if rule.reference_allowed and not rule.deprecated_pair and not has_migration_path:
                        errors.append(ValidationError(
                            D, "ENFORCE_LATEST_DEPRECATED_REFERENCE_VIOLATION",
                            f"Deprecated version {dep_id!r} has reference_allowed=True "
                            f"to active version {act_id!r} without deprecated_pair flag "
                            "or active migration path. enforce_latest=True requires "
                            "explicit migration window declaration.",
                            ctx,
                        ))
        
        # Deprecated versions must not have reference_allowed=True with active
        # unless explicitly within a migration window (non-enforce_latest case)
        if not self._policy.enforce_latest and not self._policy.allow_multiple_active_versions:
            for dep_id in sorted(deprecated_ids):
                for act_id in sorted(active_ids):
                    rule = compat_index.get((dep_id, act_id))
                    if rule is not None and rule.reference_allowed and not rule.deprecated_pair:
                        warnings.append(ValidationWarning(
                            D, "DEPRECATED_ACTIVE_REFERENCE_OUTSIDE_WINDOW",
                            f"Deprecated version {dep_id!r} has reference_allowed=True "
                            f"to active version {act_id!r} without deprecated_pair flag. "
                            "Possible governance window violation.",
                            ctx,
                        ))
        
        # Retired version still compatible with active version (governance invariant)
        retired_ids = {v.version_id for v in versions if v.state == VersionState.RETIRED}
        for r in compat_rules:
            from_retired = r.from_version in retired_ids
            to_retired = r.to_version in retired_ids
            from_active = r.from_version in active_ids
            to_active = r.to_version in active_ids
            
            if (from_retired and to_active) or (from_active and to_retired):
                if r.coexistence or r.reference_allowed:
                    errors.append(ValidationError(
                        D, "RETIRED_ACTIVE_COMPATIBILITY_VIOLATION",
                        f"Compatibility rule ({r.from_version!r}, {r.to_version!r}): "
                        "Retired version cannot be compatible with active version. "
                        "This violates governance invariants.",
                        ctx,
                    ))

    # ── Fingerprinting ────────────────────────────────────────────────────────

    def version_graph_fingerprint(self) -> str:
        """
        Deterministic fingerprinting (spec §12).
        
        Formal Determinism Guarantee:
          - SHA-256 hash over canonical JSON representation
          - All collections sorted before serialization
          - Policy state included for complete determinism
          - Identical configuration + policy → identical fingerprint
        
        Canonical Ordering:
          - Artifact types: sorted lexicographically
          - Versions: sorted by ordinal (ascending)
          - Migration rules: sorted by (from_version, to_version)
          - Compatibility rules: sorted by (from_version, to_version)
          - JSON keys: sorted via sort_keys=True
          - Policy: included in hash for governance determinism
        
        Used in:
          - Migration plan hash
          - Snapshot metadata
          - Merkle anchor export
          - Deployment gating
        
        Must be deterministic across machines (spec §17).
        Byte-identical across machines for identical configuration.
        """
        payload: dict = {}
        for art in sorted(self._registry.get_all_artifact_types()):
            versions = sorted(
                self._registry.get_versions(art), key=lambda v: v.ordinal
            )
            rules = sorted(
                self._registry.get_migration_rules(art),
                key=lambda r: (r.from_version, r.to_version),
            )
            compat = sorted(
                self._registry.get_compatibility_rules(art),
                key=lambda c: (c.from_version, c.to_version),
            )
            payload[art] = {
                "versions": [
                    {
                        "version_id": v.version_id,
                        "ordinal":    v.ordinal,
                        "state":      v.state.value,
                        "compat_ref": v.compatibility_profile_reference,
                    }
                    for v in versions
                ],
                "migration_rules": [
                    {
                        "rule_id":        r.rule_id,
                        "from_version":   r.from_version,
                        "to_version":     r.to_version,
                        "is_rollback":    r.is_rollback_class,
                    }
                    for r in rules
                ],
                "compatibility_rules": [c.to_dict() for c in compat],
            }
        
        # Include policy state in fingerprint for complete determinism
        # Policy affects validation results, so it must be part of the hash
        policy_dict = {
            "allow_multiple_active_versions":    self._policy.allow_multiple_active_versions,
            "allow_ordinal_gaps":                self._policy.allow_ordinal_gaps,
            "max_ordinal_gap":                   self._policy.max_ordinal_gap,
            "ordinal_gap_warning_threshold":     self._policy.ordinal_gap_warning_threshold,
            "enforce_compatibility_profile_ref": self._policy.enforce_compatibility_profile_ref,
            "strict_mode":                       self._policy.strict_mode,
            "max_migration_chain_length":        self._policy.max_migration_chain_length,
            "allow_downgrade_migration_rules":   self._policy.allow_downgrade_migration_rules,
            "enforce_latest":                    self._policy.enforce_latest,
        }
        payload["_policy"] = policy_dict
        
        raw = _canonical_bytes(payload)
        return _sha256_hex(raw)

    # ── Single-Type Convenience ───────────────────────────────────────────────

    def validate_artifact_type(self, artifact_type: ArtifactType) -> ValidationReport:
        """Run full validation pipeline for a single artifact type only."""
        original_types = self._registry.get_all_artifact_types

        class _SingleTypeAdapter:
            def get_all_artifact_types(_self) -> List[ArtifactType]:
                return [artifact_type]
            def get_versions(_self, at: ArtifactType): return self._registry.get_versions(at)
            def get_migration_rules(_self, at: ArtifactType): return self._registry.get_migration_rules(at)
            def get_compatibility_rules(_self, at: ArtifactType): return self._registry.get_compatibility_rules(at)
            def get_registry_fingerprint(_self) -> str: return self._registry.get_registry_fingerprint()

        return VersionValidator(_SingleTypeAdapter(), self._policy).validate_all()  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────────
# Graph Utilities (private)
# ──────────────────────────────────────────────────────────────────────────────

def _bfs_reachable(
    start:    SchemaVersionID,
    adj:      Dict[SchemaVersionID, Set[SchemaVersionID]],
) -> Set[SchemaVersionID]:
    """
    BFS — returns all nodes reachable from start (excluding start itself).
    
    Deterministic: processes nodes in sorted order at each level.
    """
    visited: Set[SchemaVersionID] = set()
    # Sort initial neighbors for deterministic processing
    queue = sorted(adj.get(start, set()))
    while queue:
        node = queue.pop(0)
        if node not in visited:
            visited.add(node)
            # Sort neighbors before adding to queue for deterministic order
            new_neighbors = sorted(adj.get(node, set()) - visited)
            queue.extend(new_neighbors)
    return visited


def _shortest_path_length(
    start:   SchemaVersionID,
    targets: Set[SchemaVersionID],
    adj:     Dict[SchemaVersionID, Set[SchemaVersionID]],
) -> Optional[int]:
    """BFS shortest path length from start to any node in targets. None if unreachable."""
    if start in targets:
        return 0
    visited = {start}
    frontier = [(start, 0)]
    while frontier:
        node, depth = frontier.pop(0)
        for neighbor in sorted(adj.get(node, set())):
            if neighbor in targets:
                return depth + 1
            if neighbor not in visited:
                visited.add(neighbor)
                frontier.append((neighbor, depth + 1))
    return None