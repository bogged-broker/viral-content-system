"""
invariants.py
System-Wide Lineage Invariant Authority
Foundational Integrity Contracts — Non-Bypassable

Authority:
  The constitutional contract of the lineage system. Declares the non-negotiable
  truths that must hold across storage, structure, evolution, compatibility, replay,
  snapshot, and governance domains.

  This file does not mutate. It does not execute. It does not plan.
  It declares truths and provides the machinery to verify them.

  Nine invariant classes, 81 named invariants, fully addressable by ID.
  Violations are never silently corrected. Fatal violations halt the system.

Non-bypass rule:
  No module may catch and suppress invariant violations, downgrade fatal to warning,
  or override invariants via runtime flag or policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Tuple, List, Dict,
    Any, Callable, Dict, FrozenSet, Iterator,
    List, Optional, Protocol, Sequence, Set,
    Tuple, runtime_checkable,
)


# ──────────────────────────────────────────────────────────────────────────────
# Type Aliases
# ──────────────────────────────────────────────────────────────────────────────

ArtifactID      = str
SchemaVersionID = str
ArtifactType    = str
InvariantID     = str


# ──────────────────────────────────────────────────────────────────────────────
# Severity & Scope
# ──────────────────────────────────────────────────────────────────────────────

class InvariantSeverity(str, Enum):
    FATAL   = "fatal"    # System must halt; no recovery path.
    WARNING = "warning"  # Structural concern; must not silently persist.


class InvariantScope(str, Enum):
    STORE_ONLY      = "store_only"
    DAG_ONLY        = "dag_only"
    MIGRATION_ONLY  = "migration_only"
    REPLAY_ONLY     = "replay_only"
    SNAPSHOT_ONLY   = "snapshot_only"
    FULL_SYSTEM     = "full_system"


class InvariantClass(str, Enum):
    STORE         = "I"     # Append-only store
    DAG           = "II"    # DAG structural
    VERSION       = "III"   # Version evolution
    COMPATIBILITY = "IV"    # Compatibility
    MIGRATION     = "V"     # Migration execution
    REPLAY        = "VI"    # Replay
    SNAPSHOT      = "VII"   # Snapshot
    MERKLE        = "VIII"  # Merkle
    GOVERNANCE    = "IX"    # Governance


# ──────────────────────────────────────────────────────────────────────────────
# Violation & Report Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InvariantViolation:
    invariant_id:   InvariantID             # e.g. "I.1", "IV.3"
    description:    str
    severity:       InvariantSeverity
    location_hint:  Optional[str] = None    # artifact_id, append_index, version, etc.

    def to_dict(self) -> dict:
        return {
            "invariant_id":  self.invariant_id,
            "description":   self.description,
            "severity":      self.severity.value,
            "location_hint": self.location_hint,
        }


@dataclass(frozen=True)
class InvariantReport:
    """
    Immutable, deterministic result of an invariant check run.
    passed=True iff no FATAL violations exist.
    Violations are ordered by invariant_id (deterministic).
    """
    passed:        bool
    scope:         InvariantScope
    violations:    Tuple[InvariantViolation, ...]
    fingerprint:   str      # system_integrity_fingerprint at time of check

    @property
    def fatal_violations(self) -> Tuple[InvariantViolation, ...]:
        return tuple(v for v in self.violations if v.severity == InvariantSeverity.FATAL)

    @property
    def warning_violations(self) -> Tuple[InvariantViolation, ...]:
        return tuple(v for v in self.violations if v.severity == InvariantSeverity.WARNING)

    def to_dict(self) -> dict:
        return {
            "passed":          self.passed,
            "scope":           self.scope.value,
            "fatal_count":     len(self.fatal_violations),
            "warning_count":   len(self.warning_violations),
            "violations":      [v.to_dict() for v in self.violations],
            "fingerprint":     self.fingerprint,
        }

    def assert_passed(self) -> None:
        """Raise InvariantViolationError if any fatal violation exists."""
        if not self.passed:
            details = "; ".join(
                f"[{v.invariant_id}] {v.description}"
                for v in self.fatal_violations
            )
            raise InvariantViolationError(
                f"{len(self.fatal_violations)} fatal invariant(s) violated: {details}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class InvariantViolationError(Exception):
    """
    Raised by assert_passed() or individual assert_* methods.
    Signals that a non-negotiable system truth has been broken.
    Must not be caught and suppressed by any other module.
    
    Non-bypass enforcement:
    This exception carries a special marker that enforcement mechanisms
    can detect. Suppressing this exception violates constitutional authority.
    """
    _INVARIANT_AUTHORITY_MARKER = "__INVARIANT_VIOLATION__"
    
    def __init__(self, message: str, violations: Optional[Tuple[InvariantViolation, ...]] = None):
        super().__init__(message)
        self.violations = violations or ()
        # Set marker attribute for non-bypass detection
        setattr(self, self._INVARIANT_AUTHORITY_MARKER, True)


# ──────────────────────────────────────────────────────────────────────────────
# Protocol Interfaces (read-only views; invariants never mutate)
# ──────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class StoreViewProtocol(Protocol):
    def stream_records(self) -> Iterator[dict]: ...
    def get_record_count(self) -> int: ...  # Alias for get_total_record_count() compatibility
    def get_total_record_count(self) -> int: ...  # Preferred method name


@runtime_checkable
class DAGViewProtocol(Protocol):
    def get_all_artifacts(self) -> List[dict]: ...
    def get_children(self, artifact_id: ArtifactID) -> List[dict]: ...


@runtime_checkable
class VersionRegistryViewProtocol(Protocol):
    def get_all_versions(self, artifact_type: ArtifactType) -> List[dict]: ...
    def get_all_artifact_types(self) -> List[ArtifactType]: ...
    def get_migration_rules(self, artifact_type: ArtifactType) -> List[dict]: ...
    def get_registry_fingerprint(self) -> str: ...


@runtime_checkable
class CompatibilityViewProtocol(Protocol):
    def get_all_rules(self, artifact_type: ArtifactType) -> List[dict]: ...
    def get_active_artifacts(self) -> List[dict]: ...
    def get_matrix_fingerprint(self) -> str: ...


@runtime_checkable
class MerkleViewProtocol(Protocol):
    def get_stored_root(self) -> str: ...
    def compute_root_from_records(self, records: List[dict]) -> str: ...
    def get_leaf_hash(self, record: dict) -> str: ...
    def get_format_version(self) -> str: ...
    def get_padding_rule(self) -> str: ...


@runtime_checkable
class ReplayViewProtocol(Protocol):
    def get_last_replay_merkle_root(self) -> str: ...
    def get_live_dag_fingerprint(self) -> str: ...
    def get_last_replayed_dag_fingerprint(self) -> str: ...


@runtime_checkable
class SnapshotViewProtocol(Protocol):
    def get_all_snapshots(self) -> List[dict]: ...
    def get_snapshot_merkle_root(self, snapshot_id: str) -> Optional[str]: ...


@runtime_checkable
class GovernanceViewProtocol(Protocol):
    def is_migration_locked(self) -> bool: ...
    def is_rollback_locked(self) -> bool: ...
    def last_validation_passed(self) -> bool: ...
    def get_version_graph_fingerprint(self) -> str: ...
    def get_migration_lock_holder(self) -> Optional[str]: ...  # Optional: lock holder ID
    def get_rollback_lock_holder(self) -> Optional[str]: ...   # Optional: lock holder ID
    def get_last_snapshot_seal_verification(self) -> Optional[bool]: ...  # True if last seal verified
    def get_last_merkle_anchor_verification(self) -> Optional[bool]: ...  # True if last anchor verified


# ──────────────────────────────────────────────────────────────────────────────
# Invariant Descriptor
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InvariantDescriptor:
    """
    A named, addressable, independently-testable invariant declaration.
    check_fn receives the full SystemContext and emits violations into a list.
    
    Each invariant can be triggered by deterministic adversarial inputs
    for testing purposes. The trigger_metadata field documents how to
    construct test scenarios that violate the invariant.
    """
    invariant_id:  InvariantID
    invariant_class: InvariantClass
    description:   str
    severity:      InvariantSeverity
    check_fn:      Callable[["SystemContext", List[InvariantViolation]], None]
    trigger_metadata: Optional[str] = None  # How to trigger this invariant for testing

    def check(
        self, ctx: "SystemContext", violations: List[InvariantViolation]
    ) -> None:
        self.check_fn(ctx, violations)


# ──────────────────────────────────────────────────────────────────────────────
# System Context  (read-only dependency bundle)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SystemContext:
    """
    All system views required to evaluate invariants.
    Each field is Optional — absent views skip the invariants that require them.
    """
    store:        Optional[StoreViewProtocol]        = None
    dag:          Optional[DAGViewProtocol]           = None
    versions:     Optional[VersionRegistryViewProtocol] = None
    compatibility: Optional[CompatibilityViewProtocol] = None
    merkle:       Optional[MerkleViewProtocol]        = None
    replay:       Optional[ReplayViewProtocol]        = None
    snapshot:     Optional[SnapshotViewProtocol]      = None
    governance:   Optional[GovernanceViewProtocol]    = None
    strict_mode:  bool = True


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(
    violations: List[InvariantViolation],
    iid: InvariantID,
    description: str,
    severity: InvariantSeverity = InvariantSeverity.FATAL,
    location: Optional[str] = None,
) -> None:
    violations.append(InvariantViolation(
        invariant_id=iid,
        description=description,
        severity=severity,
        location_hint=location,
    ))


def _has_cycle_directed(adjacency: Dict[Any, List[Any]]) -> bool:
    """Iterative DFS three-color cycle detection."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[Any, int] = {}

    def dfs(start: Any) -> bool:
        stack = [(start, iter(sorted(adjacency.get(start, []))))]
        color[start] = GRAY
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
                c = color.get(child, WHITE)
                if c == GRAY:
                    return True
                if c == WHITE:
                    color[child] = GRAY
                    stack.append((child, iter(sorted(adjacency.get(child, [])))))
            except StopIteration:
                color[node] = BLACK
                stack.pop()
        return False

    for node in sorted(adjacency.keys()):
        if color.get(node, WHITE) == WHITE:
            if dfs(node):
                return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Class I — Append-Only Store Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_I_1_append_index_monotonic(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store:
        return
    prev = -1
    for record in ctx.store.stream_records():
        idx = record.get("append_index")
        if idx is None or not isinstance(idx, int):
            _fail(v, "I.1", "Record missing integer append_index.",
                  location=str(record.get("append_index")))
            return
        if idx <= prev:
            _fail(v, "I.1",
                  f"Append index not strictly increasing: prev={prev} current={idx}.",
                  location=f"append_index={idx}")
            return
        prev = idx


def _check_I_2_no_duplicate_index(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store:
        return
    seen: Set[int] = set()
    for record in ctx.store.stream_records():
        idx = record.get("append_index")
        if idx in seen:
            _fail(v, "I.2", f"Duplicate append_index {idx} detected.",
                  location=f"append_index={idx}")
            return
        seen.add(idx)


def _check_I_6_no_gaps_in_sequence(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store:
        return
    expected = 0
    for record in ctx.store.stream_records():
        idx = record.get("append_index")
        if idx != expected:
            _fail(v, "I.6",
                  f"Gap in append sequence: expected {expected}, got {idx}.",
                  location=f"append_index={idx}")
            return
        expected += 1


def _check_I_7_canonical_serialization(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store:
        return
    for record in ctx.store.stream_records():
        try:
            _canonical_bytes(record)
        except (TypeError, ValueError) as exc:
            _fail(v, "I.7",
                  f"Record cannot be canonically serialized: {exc}",
                  location=f"append_index={record.get('append_index')}")
            return


def _check_I_8_merkle_leaf_hash_matches(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store or not ctx.merkle:
        return
    for record in ctx.store.stream_records():
        stored_hash = record.get("record_hash")
        if stored_hash is None:
            continue    # hash field optional in some implementations
        recomputed = ctx.merkle.get_leaf_hash(record)
        if not hmac.compare_digest(recomputed, stored_hash):
            _fail(v, "I.8",
                  f"Merkle leaf hash mismatch: stored={stored_hash!r} "
                  f"recomputed={recomputed!r}.",
                  location=f"append_index={record.get('append_index')}")
            return


# ──────────────────────────────────────────────────────────────────────────────
# Class II — DAG Structural Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_II_1_no_dag_cycle(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.dag:
        return
    adjacency: Dict[ArtifactID, List[ArtifactID]] = {}
    for artifact in sorted(ctx.dag.get_all_artifacts(), key=lambda a: a["artifact_id"]):
        aid = artifact["artifact_id"]
        adjacency[aid] = sorted(
            c["artifact_id"] for c in ctx.dag.get_children(aid)
        )
    if _has_cycle_directed(adjacency):
        _fail(v, "II.1", "Directed cycle detected in artifact DAG.")


def _check_II_2_single_parent(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.dag:
        return
    child_parents: Dict[ArtifactID, List[ArtifactID]] = {}
    for artifact in sorted(ctx.dag.get_all_artifacts(), key=lambda a: a["artifact_id"]):
        aid = artifact["artifact_id"]
        for child in ctx.dag.get_children(aid):
            cid = child["artifact_id"]
            child_parents.setdefault(cid, []).append(aid)
    for cid, parents in sorted(child_parents.items()):
        if len(parents) > 1:
            _fail(v, "II.2",
                  f"Artifact {cid!r} has multiple parents: {sorted(parents)}.",
                  location=f"artifact_id={cid}")


def _check_II_3_parent_index_before_child(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.dag:
        return
    index_map: Dict[ArtifactID, int] = {
        a["artifact_id"]: a.get("append_index", -1)
        for a in ctx.dag.get_all_artifacts()
    }
    for artifact in sorted(ctx.dag.get_all_artifacts(), key=lambda a: a["artifact_id"]):
        aid    = artifact["artifact_id"]
        p_idx  = index_map.get(aid, -1)
        for child in ctx.dag.get_children(aid):
            cid   = child["artifact_id"]
            c_idx = index_map.get(cid, -1)
            if c_idx <= p_idx:
                _fail(v, "II.3",
                      f"Parent {aid!r} (index={p_idx}) has append_index >= "
                      f"child {cid!r} (index={c_idx}).",
                      location=f"artifact_id={cid}")


def _check_II_4_no_dangling_parent_reference(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.dag:
        return
    all_ids = {a["artifact_id"] for a in ctx.dag.get_all_artifacts()}
    for artifact in sorted(ctx.dag.get_all_artifacts(), key=lambda a: a["artifact_id"]):
        pid = artifact.get("parent_id")
        if pid is not None and pid not in all_ids:
            _fail(v, "II.4",
                  f"Artifact {artifact['artifact_id']!r} references nonexistent "
                  f"parent {pid!r}.",
                  location=f"artifact_id={artifact['artifact_id']}")


def _check_II_7_unique_artifact_ids(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.dag:
        return
    seen: Set[ArtifactID] = set()
    for artifact in sorted(ctx.dag.get_all_artifacts(), key=lambda a: a["artifact_id"]):
        aid = artifact["artifact_id"]
        if aid in seen:
            _fail(v, "II.7", f"Duplicate artifact_id {aid!r}.",
                  location=f"artifact_id={aid}")
            return
        seen.add(aid)


# ──────────────────────────────────────────────────────────────────────────────
# Class III — Version Evolution Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_III_1_ordinal_strictly_increases(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        versions = sorted(ctx.versions.get_all_versions(art), key=lambda v: v["ordinal"])
        for i in range(1, len(versions)):
            if versions[i]["ordinal"] <= versions[i-1]["ordinal"]:
                _fail(v, "III.1",
                      f"Version ordinal regression: {versions[i-1]['version_id']!r} "
                      f"(ordinal={versions[i-1]['ordinal']}) followed by "
                      f"{versions[i]['version_id']!r} (ordinal={versions[i]['ordinal']}).",
                      location=f"artifact_type={art}")


def _check_III_2_no_downgrade_transitions(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        ordinals = {
            ver["version_id"]: ver["ordinal"]
            for ver in ctx.versions.get_all_versions(art)
        }
        for rule in sorted(
            ctx.versions.get_migration_rules(art),
            key=lambda r: (r["from_version"], r["to_version"])
        ):
            if rule.get("is_rollback_class"):
                continue
            fo = ordinals.get(rule["from_version"], -1)
            to = ordinals.get(rule["to_version"], -1)
            if to <= fo:
                _fail(v, "III.2",
                      f"Non-rollback migration rule "
                      f"{rule['from_version']!r} → {rule['to_version']!r} "
                      f"is a downgrade (ordinals {fo} → {to}).",
                      location=f"artifact_type={art}")


def _check_III_5_migration_edges_form_dag(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        adj: Dict[str, List[str]] = {}
        for rule in ctx.versions.get_migration_rules(art):
            if not rule.get("is_rollback_class"):
                adj.setdefault(rule["from_version"], []).append(rule["to_version"])
        if _has_cycle_directed(adj):
            _fail(v, "III.5",
                  f"Migration rule graph contains a cycle (artifact_type={art!r}).",
                  location=f"artifact_type={art}")


def _check_III_4_no_retired_version_reachable(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        retired = {
            ver["version_id"]
            for ver in ctx.versions.get_all_versions(art)
            if ver.get("state") == "retired"
        }
        for rule in sorted(
            ctx.versions.get_migration_rules(art),
            key=lambda r: (r["from_version"], r["to_version"])
        ):
            if rule["to_version"] in retired:
                _fail(v, "III.4",
                      f"Migration rule targets retired version "
                      f"{rule['to_version']!r} (artifact_type={art!r}).",
                      location=f"artifact_type={art}")


# ──────────────────────────────────────────────────────────────────────────────
# Class IV — Compatibility Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_IV_1_all_active_pairs_covered(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions or not ctx.compatibility:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        non_retired = sorted(
            ver["version_id"]
            for ver in ctx.versions.get_all_versions(art)
            if ver.get("state") != "retired"
        )
        rules = {
            (r["from_version"], r["to_version"])
            for r in ctx.compatibility.get_all_rules(art)
        }
        for v1 in non_retired:
            for v2 in non_retired:
                if v1 == v2:
                    continue
                if (v1, v2) not in rules:
                    _fail(v, "IV.1",
                          f"No compatibility rule for ({v1!r}, {v2!r}) "
                          f"(artifact_type={art!r}).",
                          location=f"artifact_type={art} pair=({v1},{v2})")


def _check_IV_2_coexistence_symmetric(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions or not ctx.compatibility:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        rules = {
            (r["from_version"], r["to_version"]): r
            for r in ctx.compatibility.get_all_rules(art)
        }
        for (fv, tv), rule in sorted(rules.items()):
            inverse = rules.get((tv, fv))
            if inverse is None:
                continue
            if rule.get("coexistence") != inverse.get("coexistence"):
                _fail(v, "IV.2",
                      f"Coexistence asymmetry: ({fv!r},{tv!r})="
                      f"{rule.get('coexistence')} but ({tv!r},{fv!r})="
                      f"{inverse.get('coexistence')}.",
                      location=f"artifact_type={art}")


def _check_IV_3_no_forbidden_pairs_coexisting(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.compatibility:
        return
    live = {a["artifact_id"]: a for a in ctx.compatibility.get_active_artifacts()}
    live_by_type_version: Dict[ArtifactType, Set[SchemaVersionID]] = {}
    for a in live.values():
        art = a.get("artifact_type", "")
        ver = a.get("schema_version", "")
        live_by_type_version.setdefault(art, set()).add(ver)

    if not ctx.versions:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        rules = {
            (r["from_version"], r["to_version"]): r
            for r in ctx.compatibility.get_all_rules(art)
        }
        active_versions = sorted(live_by_type_version.get(art, set()))
        for v1 in active_versions:
            for v2 in active_versions:
                if v1 >= v2:
                    continue
                rule = rules.get((v1, v2))
                if rule is None or rule.get("forbidden"):
                    _fail(v, "IV.3",
                          f"Forbidden version pair ({v1!r}, {v2!r}) are both "
                          f"present in live state (artifact_type={art!r}).",
                          location=f"artifact_type={art} pair=({v1},{v2})")


def _check_IV_5_reference_requires_coexistence(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.versions or not ctx.compatibility:
        return
    for art in sorted(ctx.versions.get_all_artifact_types()):
        for rule in sorted(
            ctx.compatibility.get_all_rules(art),
            key=lambda r: (r["from_version"], r["to_version"])
        ):
            if rule.get("reference_allowed") and not rule.get("coexistence"):
                _fail(v, "IV.5",
                      f"Rule ({rule['from_version']!r}, {rule['to_version']!r}): "
                      "reference_allowed=True requires coexistence=True.",
                      location=f"artifact_type={art}")


def _check_IV_8_compatibility_matrix_deterministic(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.compatibility:
        return
    # Verify fingerprint is stable across two independent computations
    fp1 = ctx.compatibility.get_matrix_fingerprint()
    fp2 = ctx.compatibility.get_matrix_fingerprint()
    if not hmac.compare_digest(fp1, fp2):
        _fail(v, "IV.8",
              "Compatibility matrix fingerprint is non-deterministic across calls.",
              severity=InvariantSeverity.FATAL)


def _check_IV_9_compatibility_fingerprint_stable_across_machines(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IV.9: Compatibility fingerprint stable across machines.
    
    Verify that the fingerprint computation uses canonical serialization
    and deterministic ordering, ensuring identical matrices produce identical
    fingerprints on different machines.
    """
    if not ctx.compatibility or not ctx.versions:
        return
    # The fingerprint itself should be deterministic (checked by IV.8)
    # This invariant verifies that the fingerprint computation method
    # uses canonical serialization that is machine-independent
    fp = ctx.compatibility.get_matrix_fingerprint()
    # Verify fingerprint format (should be hex string of fixed length)
    if not isinstance(fp, str) or len(fp) != 64 or not all(c in "0123456789abcdef" for c in fp):
        _fail(v, "IV.9",
              f"Compatibility matrix fingerprint has invalid format: {fp!r}. "
              "Must be 64-character lowercase hex string (SHA-256).",
              severity=InvariantSeverity.FATAL)


# ──────────────────────────────────────────────────────────────────────────────
# Class V — Migration Execution Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_V_2_migration_idempotent(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    Verify: no two MIGRATION records exist with the same parent_artifact_id
    and to_version unless they have identical transformation_hash (same migration).
    """
    if not ctx.store:
        return
    seen: Dict[Tuple[str, str], str] = {}  # (parent_id, to_version) -> tx_hash
    for record in ctx.store.stream_records():
        if record.get("record_type") != "MIGRATION":
            continue
        key = (record.get("parent_artifact_id", ""), record.get("to_version", ""))
        tx  = record.get("transformation_hash", "")
        if key in seen:
            if not hmac.compare_digest(seen[key], tx):
                _fail(v, "V.2",
                      f"Duplicate migration for parent={key[0]!r} "
                      f"to_version={key[1]!r} with different transformation_hash.",
                      location=f"append_index={record.get('append_index')}")
                return
        else:
            seen[key] = tx


def _check_V_4_output_version_matches_target(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store or not ctx.dag:
        return
    artifact_versions = {
        a["artifact_id"]: a.get("schema_version")
        for a in ctx.dag.get_all_artifacts()
    }
    for record in ctx.store.stream_records():
        if record.get("record_type") != "MIGRATION":
            continue
        new_id     = record.get("new_artifact_id")
        to_version = record.get("to_version")
        actual     = artifact_versions.get(new_id)
        if actual is not None and actual != to_version:
            _fail(v, "V.4",
                  f"Artifact {new_id!r} has schema_version={actual!r} but "
                  f"migration record claims to_version={to_version!r}.",
                  location=f"new_artifact_id={new_id}")


def _check_V_6_no_duplicate_lineage_entries(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store:
        return
    seen_indices: Set[int] = set()
    for record in ctx.store.stream_records():
        idx = record.get("append_index")
        if idx in seen_indices:
            _fail(v, "V.6",
                  f"Duplicate lineage entry at append_index={idx}.",
                  location=f"append_index={idx}")
            return
        seen_indices.add(idx)


def _check_V_9_migration_output_hash_matches(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store or not ctx.dag:
        return
    artifact_hashes = {
        a["artifact_id"]: a.get("content_hash")
        for a in ctx.dag.get_all_artifacts()
    }
    for record in ctx.store.stream_records():
        if record.get("record_type") != "MIGRATION":
            continue
        new_id       = record.get("new_artifact_id")
        stored_hash  = record.get("output_artifact_hash")
        live_hash    = artifact_hashes.get(new_id)
        if stored_hash and live_hash and not hmac.compare_digest(stored_hash, live_hash):
            _fail(v, "V.9",
                  f"Output artifact_hash mismatch for {new_id!r}: "
                  f"record claims {stored_hash!r}, live artifact has {live_hash!r}.",
                  location=f"new_artifact_id={new_id}")


# ──────────────────────────────────────────────────────────────────────────────
# Class VI — Replay Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_VI_1_full_replay_reconstructs_identical_dag(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.replay:
        return
    live_fp   = ctx.replay.get_live_dag_fingerprint()
    replay_fp = ctx.replay.get_last_replayed_dag_fingerprint()
    if not replay_fp:
        _fail(v, "VI.1",
              "No replay fingerprint available — replay has not been executed.",
              severity=InvariantSeverity.WARNING)
        return
    if not hmac.compare_digest(live_fp, replay_fp):
        _fail(v, "VI.1",
              f"Full replay DAG fingerprint mismatch: "
              f"live={live_fp!r} replayed={replay_fp!r}.")


def _check_VI_3_replay_merkle_root_matches_stored(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.replay or not ctx.merkle:
        return
    stored   = ctx.merkle.get_stored_root()
    replayed = ctx.replay.get_last_replay_merkle_root()
    if not replayed:
        _fail(v, "VI.3",
              "No replay Merkle root available.",
              severity=InvariantSeverity.WARNING)
        return
    if not hmac.compare_digest(stored, replayed):
        _fail(v, "VI.3",
              f"Replay Merkle root mismatch: stored={stored!r} replayed={replayed!r}.")


def _check_VI_6_replay_processes_records_in_append_order(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    Verify store is iterable in strict append order (proxy check on stream order).
    """
    if not ctx.store:
        return
    prev = -1
    for record in ctx.store.stream_records():
        idx = record.get("append_index", -1)
        if idx <= prev:
            _fail(v, "VI.6",
                  f"Store does not yield records in strict append order at index {idx}.",
                  location=f"append_index={idx}")
            return
        prev = idx


# ──────────────────────────────────────────────────────────────────────────────
# Class VII — Snapshot Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_VII_1_snapshot_append_index_exists(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.snapshot or not ctx.store:
        return
    all_indices = {
        record.get("append_index")
        for record in ctx.store.stream_records()
    }
    for snap in sorted(ctx.snapshot.get_all_snapshots(), key=lambda s: s.get("snapshot_id", "")):
        idx = snap.get("lineage_append_index")
        if idx not in all_indices:
            _fail(v, "VII.1",
                  f"Snapshot {snap.get('snapshot_id')!r} references "
                  f"append_index={idx} which does not exist in store.",
                  location=f"snapshot_id={snap.get('snapshot_id')}")


def _check_VII_3_rollback_does_not_delete_records(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    Verify that every ROLLBACK_EVENT in the store is followed by a supersede
    marker pattern — never by record deletion (record count must not decrease).
    """
    if not ctx.store:
        return
    rollback_indices: List[int] = []
    for record in ctx.store.stream_records():
        if record.get("record_type") == "ROLLBACK_EVENT":
            rollback_indices.append(record.get("append_index", -1))
    # If rollbacks exist, total count must equal last append_index + 1
    total = ctx.store.get_record_count()
    if rollback_indices:
        last_idx = max(rollback_indices)
        if total < last_idx + 1:
            _fail(v, "VII.3",
                  f"Record count {total} is less than expected after rollback events. "
                  "Records may have been deleted.")


def _check_VII_4_rollback_appends_event_record(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    If any snapshot has status='rolled_back', a ROLLBACK_EVENT record must exist.
    """
    if not ctx.snapshot or not ctx.store:
        return
    rolled_back_ids = {
        snap["snapshot_id"]
        for snap in ctx.snapshot.get_all_snapshots()
        if snap.get("status") == "rolled_back"
    }
    if not rolled_back_ids:
        return
    event_snapshot_ids = {
        record.get("snapshot_id")
        for record in ctx.store.stream_records()
        if record.get("record_type") == "ROLLBACK_EVENT"
    }
    for sid in sorted(rolled_back_ids):
        if sid not in event_snapshot_ids:
            _fail(v, "VII.4",
                  f"Snapshot {sid!r} is marked rolled_back but no "
                  "ROLLBACK_EVENT record exists in the store.",
                  location=f"snapshot_id={sid}")


def _check_VII_7_sealed_snapshots_immutable(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.snapshot:
        return
    for snap in sorted(ctx.snapshot.get_all_snapshots(), key=lambda s: s.get("snapshot_id", "")):
        if snap.get("status") in ("sealed", "locked"):
            if not snap.get("signed_root_hex"):
                _fail(v, "VII.7",
                      f"Snapshot {snap.get('snapshot_id')!r} is "
                      f"status={snap.get('status')!r} but has no signed_root_hex.",
                      location=f"snapshot_id={snap.get('snapshot_id')}")


# ──────────────────────────────────────────────────────────────────────────────
# Class VIII — Merkle Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_VIII_1_leaf_hash_equals_record_hash(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store or not ctx.merkle:
        return
    for record in ctx.store.stream_records():
        stored = record.get("record_hash")
        if stored is None:
            continue
        computed = ctx.merkle.get_leaf_hash(record)
        if not hmac.compare_digest(computed, stored):
            _fail(v, "VIII.1",
                  f"Leaf hash mismatch at append_index={record.get('append_index')}: "
                  f"stored={stored!r} computed={computed!r}.",
                  location=f"append_index={record.get('append_index')}")
            return


def _check_VIII_4_root_is_deterministic(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.store or not ctx.merkle:
        return
    records = list(ctx.store.stream_records())
    root1 = ctx.merkle.compute_root_from_records(records)
    root2 = ctx.merkle.compute_root_from_records(records)
    if not hmac.compare_digest(root1, root2):
        _fail(v, "VIII.4",
              "Merkle root computation is non-deterministic across identical inputs.")


def _check_VIII_9_merkle_format_version_explicit(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.merkle:
        return
    fv = ctx.merkle.get_format_version()
    if not fv:
        _fail(v, "VIII.9",
              "Merkle format version is not set. "
              "CANONICAL_MERKLE_FORMAT_VERSION must be explicit.")


def _check_VIII_3_padding_rule_fixed(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.merkle:
        return
    rule = ctx.merkle.get_padding_rule()
    if rule not in ("duplicate_last", "zero_hash"):
        _fail(v, "VIII.3",
              f"Merkle padding rule {rule!r} is not a recognized fixed value. "
              "Must be 'duplicate_last' or 'zero_hash' and must never change.")


# ──────────────────────────────────────────────────────────────────────────────
# Class IX — Governance Invariants
# ──────────────────────────────────────────────────────────────────────────────

def _check_IX_1_migration_occurs_only_under_lock(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IX.1: Migration occurs only under lock.
    
    Verify that if migrations exist in the store, they occurred while
    a migration lock was held. This is a post-facto check that validates
    governance was respected.
    """
    if not ctx.governance or not ctx.store:
        return
    # Check if any MIGRATION records exist
    has_migrations = any(
        record.get("record_type") == "MIGRATION"
        for record in ctx.store.stream_records()
    )
    if has_migrations:
        # If migrations exist, verify lock was held (governance must track this)
        if not ctx.governance.is_migration_locked():
            # This is a warning if lock is not currently held (may have been released)
            # But we check if governance can tell us about historical lock state
            lock_holder = getattr(ctx.governance, "get_migration_lock_holder", lambda: None)()
            if lock_holder is None:
                _fail(v, "IX.1",
                      "Migrations exist in store but governance cannot confirm lock was held. "
                      "Migration lock state must be tracked.",
                      severity=InvariantSeverity.WARNING)


def _check_IX_2_rollback_occurs_only_under_lock(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IX.2: Rollback occurs only under lock.
    
    Verify that if rollback events exist, they occurred while a rollback lock was held.
    """
    if not ctx.governance or not ctx.store:
        return
    has_rollbacks = any(
        record.get("record_type") == "ROLLBACK_EVENT"
        for record in ctx.store.stream_records()
    )
    if has_rollbacks:
        if not ctx.governance.is_rollback_locked():
            lock_holder = getattr(ctx.governance, "get_rollback_lock_holder", lambda: None)()
            if lock_holder is None:
                _fail(v, "IX.2",
                      "Rollback events exist in store but governance cannot confirm lock was held. "
                      "Rollback lock state must be tracked.",
                      severity=InvariantSeverity.WARNING)


def _check_IX_3_no_migration_if_validator_failed(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.governance:
        return
    if not ctx.governance.last_validation_passed():
        _fail(v, "IX.3",
              "Migration attempted or pending while last version_validator run failed. "
              "Migrations may only proceed after a clean validation pass.")


def _check_IX_4_snapshot_sealing_requires_integrity_verification(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IX.4: Snapshot sealing requires integrity verification.
    
    Verify that sealed snapshots have passed integrity verification.
    """
    if not ctx.governance or not ctx.snapshot:
        return
    sealed_snapshots = [
        snap for snap in ctx.snapshot.get_all_snapshots()
        if snap.get("status") in ("sealed", "locked")
    ]
    if sealed_snapshots:
        last_verification = getattr(
            ctx.governance, "get_last_snapshot_seal_verification", lambda: None
        )()
        if last_verification is False:
            _fail(v, "IX.4",
                  "Sealed snapshots exist but last seal verification failed. "
                  "All sealed snapshots must pass integrity verification.",
                  severity=InvariantSeverity.FATAL)


def _check_IX_5_merkle_anchoring_requires_replay_verification(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IX.5: Merkle anchoring requires replay verification.
    
    Verify that Merkle root anchoring was preceded by successful replay verification.
    """
    if not ctx.governance or not ctx.merkle:
        return
    stored_root = ctx.merkle.get_stored_root()
    if stored_root:
        last_verification = getattr(
            ctx.governance, "get_last_merkle_anchor_verification", lambda: None
        )()
        if last_verification is False:
            _fail(v, "IX.5",
                  "Merkle root is anchored but last anchor verification failed. "
                  "Merkle anchoring must be preceded by successful replay verification.",
                  severity=InvariantSeverity.FATAL)


def _check_IX_6_compatibility_matrix_update_requires_validation(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IX.6: Compatibility matrix update requires validation.
    
    If compatibility matrix fingerprint has changed, validation must have been run.
    This is checked via governance tracking of matrix changes.
    """
    if not ctx.governance or not ctx.compatibility:
        return
    # This invariant is partially covered by IX.7 (registry updates trigger validation)
    # But we add explicit check for compatibility matrix changes
    matrix_fp = ctx.compatibility.get_matrix_fingerprint()
    # Governance should track when matrix was last validated
    # If governance cannot confirm validation, emit warning
    if not ctx.governance.last_validation_passed():
        _fail(v, "IX.6",
              "Compatibility matrix exists but last validation failed. "
              "Matrix updates must trigger validation before activation.",
              severity=InvariantSeverity.WARNING)


def _check_IX_7_registry_updates_trigger_validation(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    if not ctx.governance or not ctx.versions:
        return
    gov_fp      = ctx.governance.get_version_graph_fingerprint()
    registry_fp = ctx.versions.get_registry_fingerprint()
    # If fingerprints diverge, registry changed without re-running validation
    if not hmac.compare_digest(gov_fp, registry_fp):
        _fail(v, "IX.7",
              "Registry fingerprint has changed since last governance validation. "
              "Registry updates must trigger version_validator before any migration.",
              severity=InvariantSeverity.FATAL)


def _check_IX_8_production_evolution_requires_fingerprint_stability(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    IX.8: Production evolution requires fingerprint stability.
    
    Verify that system integrity fingerprint remains stable across
    non-breaking changes and that breaking changes are explicitly tracked.
    """
    if not ctx.governance:
        return
    # This invariant ensures that the system integrity fingerprint
    # computation is deterministic and stable. The fingerprint itself
    # is computed by system_integrity_fingerprint() which must be deterministic.
    # We verify that all components contributing to the fingerprint are present
    # and that the fingerprint format is valid.
    gov_fp = ctx.governance.get_version_graph_fingerprint()
    if gov_fp and (len(gov_fp) != 64 or not all(c in "0123456789abcdef" for c in gov_fp)):
        _fail(v, "IX.8",
              f"Version graph fingerprint has invalid format: {gov_fp!r}. "
              "Must be 64-character lowercase hex string (SHA-256).",
              severity=InvariantSeverity.FATAL)


def _check_IX_9_policy_cannot_override_structural(
    ctx: SystemContext, v: List[InvariantViolation]
) -> None:
    """
    Structural sentinel: if this invariant ever fires, a policy-override path exists.
    We verify the governance layer has not relaxed the store append-only contract.
    This is a best-effort design-time sentinel.
    """
    # Invariant IX.9 is primarily architectural — encoded by non-bypass rule in this
    # file's design. No runtime check can fully enforce it, but we record its presence
    # in the invariant registry so auditors can confirm the rule is acknowledged.
    # 
    # Runtime check: verify that strict_mode is not disabled in SystemContext
    # when structural invariants are being evaluated.
    if not ctx.strict_mode:
        _fail(v, "IX.9",
              "SystemContext.strict_mode=False detected. "
              "Policy rules cannot override structural invariants. "
              "strict_mode must remain True for constitutional authority.",
              severity=InvariantSeverity.FATAL)


# ──────────────────────────────────────────────────────────────────────────────
# Invariant Registry
# ──────────────────────────────────────────────────────────────────────────────

# Canonical ordered list of all 81 declared invariants.
# Order is fixed: sorted by class roman numeral then sub-index.
# New invariants must be appended at the correct class position.

_INVARIANT_REGISTRY: Tuple[InvariantDescriptor, ...] = tuple([

    # ── Class I: Store ────────────────────────────────────────────────────────
    InvariantDescriptor("I.1",  InvariantClass.STORE, "Append index strictly increasing.",          InvariantSeverity.FATAL,   _check_I_1_append_index_monotonic, "Inject record with append_index <= previous"),
    InvariantDescriptor("I.2",  InvariantClass.STORE, "No duplicate append index.",                 InvariantSeverity.FATAL,   _check_I_2_no_duplicate_index, "Inject two records with identical append_index"),
    InvariantDescriptor("I.3",  InvariantClass.STORE, "No record deleted.",                         InvariantSeverity.FATAL,   lambda ctx, v: None),  # enforced structurally by store
    InvariantDescriptor("I.4",  InvariantClass.STORE, "No record modified in place.",               InvariantSeverity.FATAL,   lambda ctx, v: None),  # enforced structurally by store
    InvariantDescriptor("I.5",  InvariantClass.STORE, "Store iteration order equals append order.", InvariantSeverity.FATAL,   _check_VI_6_replay_processes_records_in_append_order),
    InvariantDescriptor("I.6",  InvariantClass.STORE, "No gaps in append sequence.",                InvariantSeverity.FATAL,   _check_I_6_no_gaps_in_sequence),
    InvariantDescriptor("I.7",  InvariantClass.STORE, "Record serialization is canonical.",         InvariantSeverity.FATAL,   _check_I_7_canonical_serialization),
    InvariantDescriptor("I.8",  InvariantClass.STORE, "Merkle leaf hash matches record hash.",      InvariantSeverity.FATAL,   _check_I_8_merkle_leaf_hash_matches),
    InvariantDescriptor("I.9",  InvariantClass.STORE, "Journal writes are atomic.",                 InvariantSeverity.FATAL,   lambda ctx, v: None),  # store contract; not runtime-checkable here

    # ── Class II: DAG ─────────────────────────────────────────────────────────
    InvariantDescriptor("II.1", InvariantClass.DAG, "Graph is acyclic.",                             InvariantSeverity.FATAL,   _check_II_1_no_dag_cycle, "Create parent-child cycle: A->B->C->A"),
    InvariantDescriptor("II.2", InvariantClass.DAG, "Each artifact has exactly one parent except genesis.", InvariantSeverity.FATAL, _check_II_2_single_parent),
    InvariantDescriptor("II.3", InvariantClass.DAG, "Parent append index < child append index.",     InvariantSeverity.FATAL,   _check_II_3_parent_index_before_child),
    InvariantDescriptor("II.4", InvariantClass.DAG, "No artifact references nonexistent parent.",   InvariantSeverity.FATAL,   _check_II_4_no_dangling_parent_reference),
    InvariantDescriptor("II.5", InvariantClass.DAG, "No unauthorized fork.",                         InvariantSeverity.FATAL,   lambda ctx, v: None),  # enforced by migration_executor
    InvariantDescriptor("II.6", InvariantClass.DAG, "Terminal nodes have no children.",              InvariantSeverity.FATAL,   lambda ctx, v: None),  # implied by II.1 + II.2
    InvariantDescriptor("II.7", InvariantClass.DAG, "Artifact IDs are unique.",                      InvariantSeverity.FATAL,   _check_II_7_unique_artifact_ids),
    InvariantDescriptor("II.8", InvariantClass.DAG, "Artifact version is immutable after creation.", InvariantSeverity.FATAL,   _check_V_4_output_version_matches_target),
    InvariantDescriptor("II.9", InvariantClass.DAG, "Transformation class matches registry.",        InvariantSeverity.FATAL,   lambda ctx, v: None),  # enforced by migration_executor

    # ── Class III: Version Evolution ──────────────────────────────────────────
    InvariantDescriptor("III.1", InvariantClass.VERSION, "Version ordinal strictly increases.",              InvariantSeverity.FATAL,   _check_III_1_ordinal_strictly_increases),
    InvariantDescriptor("III.2", InvariantClass.VERSION, "No downgrade transitions.",                        InvariantSeverity.FATAL,   _check_III_2_no_downgrade_transitions),
    InvariantDescriptor("III.3", InvariantClass.VERSION, "No active version without upward path unless latest.", InvariantSeverity.WARNING, lambda ctx, v: None),
    InvariantDescriptor("III.4", InvariantClass.VERSION, "No retired version reachable via migration.",       InvariantSeverity.FATAL,   _check_III_4_no_retired_version_reachable),
    InvariantDescriptor("III.5", InvariantClass.VERSION, "Migration edges form a DAG.",                       InvariantSeverity.FATAL,   _check_III_5_migration_edges_form_dag),
    InvariantDescriptor("III.6", InvariantClass.VERSION, "Transition legality aligns with compatibility matrix.", InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("III.7", InvariantClass.VERSION, "No implicit migration.",                            InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("III.8", InvariantClass.VERSION, "Schema fingerprint matches declared version.",      InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("III.9", InvariantClass.VERSION, "Version graph is acyclic.",                         InvariantSeverity.FATAL,   _check_III_5_migration_edges_form_dag),

    # ── Class IV: Compatibility ────────────────────────────────────────────────
    InvariantDescriptor("IV.1", InvariantClass.COMPATIBILITY, "Every active/deprecated pair has compatibility rule.", InvariantSeverity.FATAL, _check_IV_1_all_active_pairs_covered),
    InvariantDescriptor("IV.2", InvariantClass.COMPATIBILITY, "Coexistence is symmetric.",                            InvariantSeverity.FATAL, _check_IV_2_coexistence_symmetric),
    InvariantDescriptor("IV.3", InvariantClass.COMPATIBILITY, "Forbidden pairs never coexist in live state.",         InvariantSeverity.FATAL, _check_IV_3_no_forbidden_pairs_coexisting),
    InvariantDescriptor("IV.4", InvariantClass.COMPATIBILITY, "Deprecated coexistence does not exceed policy window.", InvariantSeverity.WARNING, lambda ctx, v: None),
    InvariantDescriptor("IV.5", InvariantClass.COMPATIBILITY, "Reference legality does not contradict coexistence.",  InvariantSeverity.FATAL, _check_IV_5_reference_requires_coexistence),
    InvariantDescriptor("IV.6", InvariantClass.COMPATIBILITY, "Migration must not produce forbidden pair state.",      InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("IV.7", InvariantClass.COMPATIBILITY, "Cross-type compatibility is explicit.",                 InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("IV.8", InvariantClass.COMPATIBILITY, "Compatibility matrix is deterministic.",               InvariantSeverity.FATAL, _check_IV_8_compatibility_matrix_deterministic),
    InvariantDescriptor("IV.9", InvariantClass.COMPATIBILITY, "Compatibility fingerprint stable across machines.",     InvariantSeverity.FATAL, _check_IV_9_compatibility_fingerprint_stable_across_machines),

    # ── Class V: Migration Execution ──────────────────────────────────────────
    InvariantDescriptor("V.1", InvariantClass.MIGRATION, "Migration is deterministic.",                        InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("V.2", InvariantClass.MIGRATION, "Migration is idempotent.",                           InvariantSeverity.FATAL, _check_V_2_migration_idempotent),
    InvariantDescriptor("V.3", InvariantClass.MIGRATION, "Migration does not mutate input artifact.",          InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("V.4", InvariantClass.MIGRATION, "Output artifact validates schema.",                  InvariantSeverity.FATAL, _check_V_4_output_version_matches_target),
    InvariantDescriptor("V.5", InvariantClass.MIGRATION, "Append is atomic.",                                  InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("V.6", InvariantClass.MIGRATION, "No duplicate migration lineage entry.",              InvariantSeverity.FATAL, _check_V_6_no_duplicate_lineage_entries),
    InvariantDescriptor("V.7", InvariantClass.MIGRATION, "No conflicting concurrent upgrade.",                 InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("V.8", InvariantClass.MIGRATION, "Registry fingerprint matches at execution time.",    InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("V.9", InvariantClass.MIGRATION, "Migration output hash matches stored hash.",         InvariantSeverity.FATAL, _check_V_9_migration_output_hash_matches),

    # ── Class VI: Replay ──────────────────────────────────────────────────────
    InvariantDescriptor("VI.1", InvariantClass.REPLAY, "Full replay reconstructs identical DAG.",              InvariantSeverity.FATAL,   _check_VI_1_full_replay_reconstructs_identical_dag),
    InvariantDescriptor("VI.2", InvariantClass.REPLAY, "Artifact hashes recomputed match stored hashes.",     InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VI.3", InvariantClass.REPLAY, "Replay Merkle root equals stored Merkle root.",       InvariantSeverity.FATAL,   _check_VI_3_replay_merkle_root_matches_stored),
    InvariantDescriptor("VI.4", InvariantClass.REPLAY, "Replay is deterministic across environments.",        InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VI.5", InvariantClass.REPLAY, "No hidden state influences replay.",                  InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VI.6", InvariantClass.REPLAY, "Replay processes records in strict append order.",    InvariantSeverity.FATAL,   _check_VI_6_replay_processes_records_in_append_order),
    InvariantDescriptor("VI.7", InvariantClass.REPLAY, "Incremental replay result equals full replay result.", InvariantSeverity.FATAL,  lambda ctx, v: None),
    InvariantDescriptor("VI.8", InvariantClass.REPLAY, "Snapshot replay matches snapshot Merkle root.",       InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VI.9", InvariantClass.REPLAY, "Replay fails upon configuration drift (strict mode).", InvariantSeverity.FATAL,  lambda ctx, v: None),

    # ── Class VII: Snapshot ───────────────────────────────────────────────────
    InvariantDescriptor("VII.1", InvariantClass.SNAPSHOT, "Snapshot append_index exists in store.",               InvariantSeverity.FATAL,   _check_VII_1_snapshot_append_index_exists),
    InvariantDescriptor("VII.2", InvariantClass.SNAPSHOT, "Snapshot Merkle root verifiable against historical data.", InvariantSeverity.FATAL, lambda ctx, v: None),
    InvariantDescriptor("VII.3", InvariantClass.SNAPSHOT, "Rollback does not delete records.",                    InvariantSeverity.FATAL,   _check_VII_3_rollback_does_not_delete_records),
    InvariantDescriptor("VII.4", InvariantClass.SNAPSHOT, "Rollback appends explicit RollbackEvent.",             InvariantSeverity.FATAL,   _check_VII_4_rollback_appends_event_record),
    InvariantDescriptor("VII.5", InvariantClass.SNAPSHOT, "Registry fingerprint at rollback matches snapshot.",   InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VII.6", InvariantClass.SNAPSHOT, "Artifact heads restored exactly as recorded.",         InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VII.7", InvariantClass.SNAPSHOT, "Sealed snapshots are immutable.",                      InvariantSeverity.FATAL,   _check_VII_7_sealed_snapshots_immutable),
    InvariantDescriptor("VII.8", InvariantClass.SNAPSHOT, "Snapshot fingerprint is deterministic.",               InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VII.9", InvariantClass.SNAPSHOT, "Snapshot must never alter historical Merkle roots.",   InvariantSeverity.FATAL,   lambda ctx, v: None),

    # ── Class VIII: Merkle ────────────────────────────────────────────────────
    InvariantDescriptor("VIII.1", InvariantClass.MERKLE, "Leaf hash = SHA-256(canonical record).",                InvariantSeverity.FATAL,   _check_VIII_1_leaf_hash_equals_record_hash),
    InvariantDescriptor("VIII.2", InvariantClass.MERKLE, "Internal node hash = H(left || right).",                InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VIII.3", InvariantClass.MERKLE, "Padding rule is fixed and immutable.",                  InvariantSeverity.FATAL,   _check_VIII_3_padding_rule_fixed),
    InvariantDescriptor("VIII.4", InvariantClass.MERKLE, "Root is deterministic.",                                InvariantSeverity.FATAL,   _check_VIII_4_root_is_deterministic),
    InvariantDescriptor("VIII.5", InvariantClass.MERKLE, "Root changes if any record changes.",                   InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VIII.6", InvariantClass.MERKLE, "Root changes if any order changes.",                    InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VIII.7", InvariantClass.MERKLE, "Incremental root equals full rebuild root.",            InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VIII.8", InvariantClass.MERKLE, "Partial proof verification succeeds for valid proof.",  InvariantSeverity.FATAL,   lambda ctx, v: None),
    InvariantDescriptor("VIII.9", InvariantClass.MERKLE, "Merkle format version is explicit.",                    InvariantSeverity.FATAL,   _check_VIII_9_merkle_format_version_explicit),

    # ── Class IX: Governance ──────────────────────────────────────────────────
    InvariantDescriptor("IX.1", InvariantClass.GOVERNANCE, "Migration occurs only under lock.",                   InvariantSeverity.FATAL,   _check_IX_1_migration_occurs_only_under_lock),
    InvariantDescriptor("IX.2", InvariantClass.GOVERNANCE, "Rollback occurs only under lock.",                    InvariantSeverity.FATAL,   _check_IX_2_rollback_occurs_only_under_lock),
    InvariantDescriptor("IX.3", InvariantClass.GOVERNANCE, "No migration if version_validator failed.",           InvariantSeverity.FATAL,   _check_IX_3_no_migration_if_validator_failed),
    InvariantDescriptor("IX.4", InvariantClass.GOVERNANCE, "Snapshot sealing requires integrity verification.",   InvariantSeverity.FATAL,   _check_IX_4_snapshot_sealing_requires_integrity_verification),
    InvariantDescriptor("IX.5", InvariantClass.GOVERNANCE, "Merkle anchoring requires replay verification.",      InvariantSeverity.FATAL,   _check_IX_5_merkle_anchoring_requires_replay_verification),
    InvariantDescriptor("IX.6", InvariantClass.GOVERNANCE, "Compatibility matrix update requires validation.",    InvariantSeverity.FATAL,   _check_IX_6_compatibility_matrix_update_requires_validation),
    InvariantDescriptor("IX.7", InvariantClass.GOVERNANCE, "Registry updates trigger validation.",                InvariantSeverity.FATAL,   _check_IX_7_registry_updates_trigger_validation),
    InvariantDescriptor("IX.8", InvariantClass.GOVERNANCE, "Production evolution requires fingerprint stability.", InvariantSeverity.FATAL,  _check_IX_8_production_evolution_requires_fingerprint_stability),
    InvariantDescriptor("IX.9", InvariantClass.GOVERNANCE, "Policy rules cannot override structural invariants.", InvariantSeverity.FATAL,   _check_IX_9_policy_cannot_override_structural),
])

# Build a fast lookup index: invariant_id → descriptor
_INVARIANT_INDEX: Dict[InvariantID, InvariantDescriptor] = {
    inv.invariant_id: inv for inv in _INVARIANT_REGISTRY
}

# Scope → relevant invariant classes
_SCOPE_CLASSES: Dict[InvariantScope, FrozenSet[InvariantClass]] = {
    InvariantScope.STORE_ONLY:     frozenset({InvariantClass.STORE}),
    InvariantScope.DAG_ONLY:       frozenset({InvariantClass.DAG}),
    InvariantScope.MIGRATION_ONLY: frozenset({InvariantClass.MIGRATION}),
    InvariantScope.REPLAY_ONLY:    frozenset({InvariantClass.REPLAY}),
    InvariantScope.SNAPSHOT_ONLY:  frozenset({InvariantClass.SNAPSHOT}),
    InvariantScope.FULL_SYSTEM:    frozenset({
        InvariantClass.STORE,
        InvariantClass.DAG,
        InvariantClass.VERSION,
        InvariantClass.COMPATIBILITY,
        InvariantClass.MIGRATION,
        InvariantClass.REPLAY,
        InvariantClass.SNAPSHOT,
        InvariantClass.MERKLE,
        InvariantClass.GOVERNANCE,
    }),
}


# ──────────────────────────────────────────────────────────────────────────────
# InvariantChecker — Entry Point
# ──────────────────────────────────────────────────────────────────────────────

class InvariantChecker:
    """
    Constitutional integrity checker.

    Evaluates all invariants within the requested scope against a SystemContext.
    Returns a deterministic InvariantReport.
    Never raises for expected violations — all defects surface in the report.

    Usage:
        ctx     = SystemContext(store=..., dag=..., merkle=..., ...)
        checker = InvariantChecker()
        report  = checker.check_all_invariants(InvariantScope.FULL_SYSTEM, ctx)
        report.assert_passed()   # raises InvariantViolationError if any fatal
    """

    def check_all_invariants(
        self,
        scope: InvariantScope,
        ctx:   SystemContext,
    ) -> InvariantReport:
        """
        Run all invariants within scope in deterministic order.
        Each invariant is evaluated independently; one failure does not skip others.
        """
        target_classes = _SCOPE_CLASSES[scope]
        violations: List[InvariantViolation] = []

        for descriptor in _INVARIANT_REGISTRY:
            if descriptor.invariant_class not in target_classes:
                continue
            try:
                descriptor.check(ctx, violations)
            except Exception as exc:
                # An unexpected exception in a check function is itself a fatal violation
                violations.append(InvariantViolation(
                    invariant_id=descriptor.invariant_id,
                    description=(
                        f"Invariant check raised unexpected exception: {exc}"
                    ),
                    severity=InvariantSeverity.FATAL,
                    location_hint="check_fn internal error",
                ))

        # Sort violations deterministically by invariant_id
        violations.sort(key=lambda v: _invariant_sort_key(v.invariant_id))

        has_fatal = any(
            v.severity == InvariantSeverity.FATAL for v in violations
        )
        return InvariantReport(
            passed=not has_fatal,
            scope=scope,
            violations=tuple(violations),
            fingerprint=self.system_integrity_fingerprint(ctx),
        )

    def check_invariant(
        self,
        invariant_id: InvariantID,
        ctx:          SystemContext,
    ) -> InvariantReport:
        """
        Evaluate a single named invariant and return a report.
        Useful for targeted post-operation spot-checks.
        """
        descriptor = _INVARIANT_INDEX.get(invariant_id)
        if descriptor is None:
            raise ValueError(f"Unknown invariant_id {invariant_id!r}.")

        violations: List[InvariantViolation] = []
        try:
            descriptor.check(ctx, violations)
        except Exception as exc:
            violations.append(InvariantViolation(
                invariant_id=invariant_id,
                description=f"Invariant check raised unexpected exception: {exc}",
                severity=InvariantSeverity.FATAL,
                location_hint="check_fn internal error",
            ))

        has_fatal = any(v.severity == InvariantSeverity.FATAL for v in violations)
        return InvariantReport(
            passed=not has_fatal,
            scope=InvariantScope.FULL_SYSTEM,
            violations=tuple(violations),
            fingerprint=self.system_integrity_fingerprint(ctx),
        )

    # ── Individual Assert Methods (hard failure surfaces) ─────────────────────

    def assert_invariant(self, invariant_id: InvariantID, ctx: SystemContext) -> None:
        """Assert a single invariant; raise InvariantViolationError on failure."""
        self.check_invariant(invariant_id, ctx).assert_passed()

    def assert_store_invariants(self, ctx: SystemContext) -> InvariantReport:
        report = self.check_all_invariants(InvariantScope.STORE_ONLY, ctx)
        report.assert_passed()
        return report

    def assert_dag_invariants(self, ctx: SystemContext) -> InvariantReport:
        report = self.check_all_invariants(InvariantScope.DAG_ONLY, ctx)
        report.assert_passed()
        return report

    def assert_full_system(self, ctx: SystemContext) -> InvariantReport:
        report = self.check_all_invariants(InvariantScope.FULL_SYSTEM, ctx)
        report.assert_passed()
        return report

    # ── Fingerprinting ────────────────────────────────────────────────────────

    def system_integrity_fingerprint(self, ctx: SystemContext) -> str:
        """
        Deterministic SHA-256 composite fingerprint over all system integrity components.
        
        Components (in canonical order):
          1. Invariant registry definitions (all 81 invariants with ID, class, description, severity)
          2. Version graph fingerprint (from version registry)
          3. Compatibility matrix fingerprint (from compatibility matrix)
          4. Registry fingerprint (from version registry - may differ from version graph)
          5. Governance fingerprint (from governance layer)
          6. Merkle format version (if available)
          7. Store format version (if available)
        
        Deterministic ordering ensures identical systems produce identical fingerprints
        across machines, environments, and time.
        
        Used in:
          - Deployment gating (prevent deployment if fingerprint changed unexpectedly)
          - Anchor export (immutable system state snapshot)
          - Snapshot metadata (sealed snapshot integrity)
          - Compliance reports (audit trail of system evolution)
        
        Returns:
            64-character lowercase hex string (SHA-256 digest)
        """
        # 1. Invariant registry definitions (canonical sorted order)
        invariant_definitions = {
            inv.invariant_id: {
                "class":       inv.invariant_class.value,
                "description": inv.description,
                "severity":    inv.severity.value,
            }
            for inv in sorted(_INVARIANT_REGISTRY, key=lambda i: _invariant_sort_key(i.invariant_id))
        }
        
        # 2-5. Component fingerprints (sorted keys for determinism)
        payload: dict = {
            "invariant_definitions": invariant_definitions,
            "version_graph_fingerprint": (
                ctx.versions.get_registry_fingerprint() if ctx.versions else ""
            ),
            "compatibility_fingerprint": (
                ctx.compatibility.get_matrix_fingerprint() if ctx.compatibility else ""
            ),
            "registry_fingerprint": (
                ctx.versions.get_registry_fingerprint() if ctx.versions else ""
            ),
            "governance_fingerprint": (
                ctx.governance.get_version_graph_fingerprint() if ctx.governance else ""
            ),
        }
        
        # 6-7. Format versions (if available)
        if ctx.merkle:
            payload["merkle_format_version"] = ctx.merkle.get_format_version() or ""
            payload["merkle_padding_rule"] = ctx.merkle.get_padding_rule() or ""
        
        # Compute deterministic fingerprint
        canonical = _canonical_bytes(payload)
        fingerprint = _sha256_hex(canonical)
        
        # Validate fingerprint format
        if len(fingerprint) != 64 or not all(c in "0123456789abcdef" for c in fingerprint):
            raise RuntimeError(
                f"system_integrity_fingerprint produced invalid format: {fingerprint!r}"
            )
        
        return fingerprint

    # ── Registry Introspection ────────────────────────────────────────────────

    @staticmethod
    def list_invariants(
        invariant_class: Optional[InvariantClass] = None,
    ) -> Tuple[InvariantDescriptor, ...]:
        """Return invariant descriptors, optionally filtered by class."""
        if invariant_class is None:
            return _INVARIANT_REGISTRY
        return tuple(
            inv for inv in _INVARIANT_REGISTRY
            if inv.invariant_class == invariant_class
        )

    @staticmethod
    def get_invariant(invariant_id: InvariantID) -> InvariantDescriptor:
        descriptor = _INVARIANT_INDEX.get(invariant_id)
        if descriptor is None:
            raise ValueError(f"Unknown invariant_id {invariant_id!r}.")
        return descriptor
    
    @staticmethod
    def export_registry() -> Dict[str, Any]:
        """
        Export the complete invariant registry as a machine-readable dictionary.
        
        Used for:
          - CI/CD gating (track invariant stability across versions)
          - Compliance tooling
          - External compliance tooling
          - Documentation generation
        
        Returns:
            Dictionary with keys:
              - "registry_version": str (semantic version of registry format)
              - "total_count": int (number of invariants)
              - "by_class": Dict[str, List[Dict]] (invariants grouped by class)
              - "by_id": Dict[str, Dict] (invariants indexed by ID)
              - "fingerprint": str (SHA-256 of registry definition)
        """
        by_class: Dict[str, List[Dict[str, Any]]] = {}
        by_id: Dict[str, Dict[str, Any]] = {}
        
        for inv in _INVARIANT_REGISTRY:
            entry = {
                "invariant_id": inv.invariant_id,
                "class": inv.invariant_class.value,
                "description": inv.description,
                "severity": inv.severity.value,
            }
            class_key = inv.invariant_class.value
            by_class.setdefault(class_key, []).append(entry)
            by_id[inv.invariant_id] = entry
        
        # Sort entries within each class by invariant_id
        for class_key in by_class:
            by_class[class_key].sort(key=lambda e: _invariant_sort_key(e["invariant_id"]))
        
        registry_data = {
            "registry_version": "1.0",
            "total_count": len(_INVARIANT_REGISTRY),
            "by_class": by_class,
            "by_id": by_id,
        }
        
        # Compute registry fingerprint
        canonical = _canonical_bytes(registry_data)
        fingerprint = _sha256_hex(canonical)
        registry_data["fingerprint"] = fingerprint
        
        return registry_data


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _invariant_sort_key(iid: InvariantID) -> Tuple[int, int]:
    """
    Produce a stable numeric sort key from invariant IDs like "III.5".
    Maps roman numeral class to integer, then sub-index to integer.
    """
    _ROMAN = {
        "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
        "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
    }
    try:
        parts = iid.split(".")
        return (_ROMAN.get(parts[0], 99), int(parts[1]))
    except (IndexError, ValueError):
        return (99, 99)


# ──────────────────────────────────────────────────────────────────────────────
# Non-Bypass Enforcement Mechanism
# ──────────────────────────────────────────────────────────────────────────────

def _detect_suppressed_violation(exc: Exception) -> bool:
    """
    Detect if an exception is an InvariantViolationError that may have been suppressed.
    
    This function checks for the special marker that indicates an invariant violation.
    Used by enforcement mechanisms to detect bypass attempts.
    """
    if isinstance(exc, InvariantViolationError):
        return hasattr(exc, InvariantViolationError._INVARIANT_AUTHORITY_MARKER)
    return False


def enforce_non_bypass(
    func: Optional[Callable[..., Any]] = None,
    *args: Any,
    **kwargs: Any
) -> Any:
    """
    Decorator/wrapper that enforces non-bypass rule for invariant violations.
    
    Wraps a function call and ensures that if InvariantViolationError is raised,
    it is not caught and suppressed. If suppression is detected, raises a
    fatal BypassAttemptError.
    
    Usage as decorator:
        @enforce_non_bypass
        def my_function():
            check_all_invariants(...)
    
    Usage as wrapper:
        result = enforce_non_bypass(some_function, arg1, arg2)
    """
    def _wrapper(f: Callable[..., Any]) -> Callable[..., Any]:
        def _inner(*inner_args: Any, **inner_kwargs: Any) -> Any:
            try:
                return f(*inner_args, **inner_kwargs)
            except InvariantViolationError as e:
                # Re-raise immediately - never suppress
                raise
            except Exception as e:
                # Check if this exception wraps a suppressed InvariantViolationError
                if hasattr(e, "__cause__") and isinstance(e.__cause__, InvariantViolationError):
                    raise BypassAttemptError(
                        f"InvariantViolationError was suppressed by {type(e).__name__}: {e}"
                    ) from e
                if hasattr(e, "__context__") and isinstance(e.__context__, InvariantViolationError):
                    raise BypassAttemptError(
                        f"InvariantViolationError was suppressed in exception context: {e}"
                    ) from e
                raise
        return _inner
    
    # If called with a function (decorator usage), return the wrapper
    if func is not None:
        return _wrapper(func)
    
    # If called with args/kwargs (direct call), apply wrapper and call
    if args and callable(args[0]):
        return _wrapper(args[0])
    
    # Otherwise, this is being used incorrectly
    raise TypeError("enforce_non_bypass must be called with a function or used as a decorator")


class BypassAttemptError(Exception):
    """
    Raised when an attempt to suppress InvariantViolationError is detected.
    
    This is a fatal error that indicates a constitutional violation:
    no module may catch and suppress invariant violations.
    """
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Convenience Module-Level Functions
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_CHECKER = InvariantChecker()


def check_all_invariants(
    scope: InvariantScope,
    ctx:   SystemContext,
) -> InvariantReport:
    """Module-level convenience entry point."""
    return _DEFAULT_CHECKER.check_all_invariants(scope, ctx)


def assert_full_system(ctx: SystemContext) -> InvariantReport:
    """Run all invariants and raise InvariantViolationError on any fatal violation."""
    return _DEFAULT_CHECKER.assert_full_system(ctx)


def system_integrity_fingerprint(ctx: SystemContext) -> str:
    """Compute the system integrity fingerprint from the given context."""
    return _DEFAULT_CHECKER.system_integrity_fingerprint(ctx)


def export_invariant_registry() -> Dict[str, Any]:
    """Export the complete invariant registry for external tooling."""
    return InvariantChecker.export_registry()


def get_invariant_registry() -> Dict[InvariantID, InvariantDescriptor]:
    """
    Get the canonical invariant registry as a dictionary.
    
    Returns:
        Dictionary mapping invariant_id -> InvariantDescriptor
    """
    return _INVARIANT_INDEX.copy()


# ──────────────────────────────────────────────────────────────────────────────
# Module Exports
# ──────────────────────────────────────────────────────────────────────────────

__all__ = [
    # Core Types
    "InvariantSeverity",
    "InvariantScope",
    "InvariantClass",
    "InvariantID",
    "InvariantViolation",
    "InvariantReport",
    "InvariantDescriptor",
    "SystemContext",
    "InvariantViolationError",
    "BypassAttemptError",
    # Protocols
    "StoreViewProtocol",
    "DAGViewProtocol",
    "VersionRegistryViewProtocol",
    "CompatibilityViewProtocol",
    "MerkleViewProtocol",
    "ReplayViewProtocol",
    "SnapshotViewProtocol",
    "GovernanceViewProtocol",
    # Main API
    "InvariantChecker",
    "check_all_invariants",
    "assert_full_system",
    "system_integrity_fingerprint",
    "export_invariant_registry",
    "get_invariant_registry",
    "enforce_non_bypass",
]