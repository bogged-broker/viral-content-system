"""
/data/lineage/lineage_graph.py

Deterministic Directed Acyclic Graph Authority
Immutable Structure · Replay-Stable · Strictly Validated
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

from lineage_record import LineageRecord
from lineage_types import (
    ArtifactID,
    ArtifactType,
    LineageNodeID,
    MigrationID,
    SchemaVersionID,
    TransformationType,
)

__all__ = ["LineageGraph", "LineageGraphError", "GenesisPolicy"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LineageGraphError(Exception):
    """Base class for all structural graph violations. Always fatal."""


class DuplicateNodeError(LineageGraphError):
    """A record with this lineage_node_id is already registered."""


class DuplicateArtifactError(LineageGraphError):
    """An artifact with this output_artifact_id is already produced."""


class MissingParentError(LineageGraphError):
    """A required parent artifact does not exist in the graph."""


class CycleError(LineageGraphError):
    """Appending this record would introduce a cycle."""


class MonotonicityError(LineageGraphError):
    """logical_timestamp is not strictly greater than the previous one."""


class IntegrityError(LineageGraphError):
    """validate_integrity() found a structural inconsistency."""


class GenesisViolationError(LineageGraphError):
    """A genesis-policy violation was detected."""


class MutationError(LineageGraphError):
    """An attempt was made to mutate an immutable graph structure."""


# ---------------------------------------------------------------------------
# Genesis policy
# ---------------------------------------------------------------------------

class GenesisPolicy:
    """
    Declares which ArtifactTypes are permitted to have zero parents.

    Only explicitly registered types may act as genesis nodes.
    Implicit genesis (parentless records with an unregistered type) is forbidden.
    """

    __slots__ = ("_allowed_types",)

    def __init__(self, allowed_types: FrozenSet[ArtifactType]) -> None:
        if not isinstance(allowed_types, frozenset):
            raise TypeError("GenesisPolicy requires a frozenset of ArtifactType")
        for t in allowed_types:
            if not isinstance(t, ArtifactType):
                raise TypeError(f"GenesisPolicy entry must be ArtifactType, got {type(t)!r}")
        object.__setattr__(self, "_allowed_types", allowed_types)

    def allows(self, artifact_type: ArtifactType) -> bool:
        return artifact_type in self._allowed_types

    def __setattr__(self, *_: object) -> None:
        raise MutationError("GenesisPolicy is immutable.")

    def __repr__(self) -> str:
        return f"GenesisPolicy(allowed={self._allowed_types!r})"


# ---------------------------------------------------------------------------
# LineageGraph
# ---------------------------------------------------------------------------

class LineageGraph:
    """
    Deterministic, append-only directed acyclic graph of LineageRecord objects.

    Enforces:
      - Acyclicity (structurally guaranteed by parent-must-preexist rule)
      - Single-origin artifact provenance
      - Referential integrity across all parent references
      - Strict monotonic logical timestamp ordering
      - Deterministic topological traversal matching append order
      - Replay-equal reconstruction from ordered records alone

    This is structural truth enforcement — not storage, not migration,
    not recovery. Those layers build on top of this one.
    """

    __slots__ = (
        "_records_by_node_id",
        "_record_by_output_artifact",
        "_children_index",
        "_parent_index",
        "_logical_order",
        "_known_artifacts",
        "_last_logical_timestamp",
        "_genesis_policy",
    )

    def __init__(self, genesis_policy: Optional[GenesisPolicy] = None) -> None:
        object.__setattr__(self, "_records_by_node_id",        {})   # Dict[LineageNodeID, LineageRecord]
        object.__setattr__(self, "_record_by_output_artifact", {})   # Dict[ArtifactID, LineageRecord]
        object.__setattr__(self, "_children_index",            {})   # Dict[ArtifactID, List[ArtifactID]]
        object.__setattr__(self, "_parent_index",              {})   # Dict[ArtifactID, Tuple[ArtifactID, ...]]
        object.__setattr__(self, "_logical_order",             [])   # List[LineageNodeID]
        object.__setattr__(self, "_known_artifacts",           set()) # Set[ArtifactID]
        object.__setattr__(self, "_last_logical_timestamp",    -1)
        object.__setattr__(self, "_genesis_policy",            genesis_policy)

    # -- immutability guard --------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise MutationError(
            f"LineageGraph does not support direct attribute assignment ({name!r}). "
            "Use append() to extend the graph."
        )

    # -- internal unsafe setters (bypasses guard) ----------------------------

    def _set(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)

    # -- append --------------------------------------------------------------

    def append(self, record: LineageRecord) -> None:
        """
        Append a LineageRecord to the graph.

        Enforces all structural invariants atomically:
          - Uniqueness of lineage_node_id
          - Uniqueness of output_artifact_id (single-origin provenance)
          - Existence of all parent artifacts
          - Genesis policy compliance
          - Self-dependency absence
          - Strict monotonic logical_timestamp
          - Acyclicity (guaranteed by parent-must-preexist + no self-ref)

        Raises a subclass of LineageGraphError on any violation.
        """
        if not isinstance(record, LineageRecord):
            raise TypeError(f"append() requires a LineageRecord, got {type(record)!r}")

        node_id        = record.lineage_node_id
        out_id         = record.output_artifact_id
        parent_ids     = record.input_artifact_ids
        ts             = record.logical_timestamp
        artifact_type  = record.artifact_type

        # 1. Unique node ID
        if node_id in self._records_by_node_id:
            raise DuplicateNodeError(
                f"LineageNodeID {node_id.to_string()!r} is already registered in the graph."
            )

        # 2. Unique output artifact (single-origin provenance)
        if out_id in self._record_by_output_artifact:
            raise DuplicateArtifactError(
                f"ArtifactID {out_id.to_string()!r} is already produced by "
                f"node {self._record_by_output_artifact[out_id].lineage_node_id.to_string()!r}."
            )

        # 3. Monotonic logical timestamp
        last_ts: int = self._last_logical_timestamp
        if ts <= last_ts:
            raise MonotonicityError(
                f"logical_timestamp must be strictly increasing; "
                f"got {ts!r}, previous was {last_ts!r}."
            )

        # 4. Self-dependency (output must not be a parent — also caught by
        #    LineageRecord constructor, but we re-check at graph level)
        if out_id in parent_ids:
            raise CycleError(
                f"output_artifact_id {out_id.to_string()!r} appears in its own parent set."
            )

        # 5. Parent existence + genesis policy
        if len(parent_ids) == 0:
            # Genesis record: must be permitted by policy
            policy: Optional[GenesisPolicy] = self._genesis_policy
            if policy is None or not policy.allows(artifact_type):
                raise GenesisViolationError(
                    f"Record with zero parents is not permitted for ArtifactType "
                    f"{artifact_type.value!r}. Register a GenesisPolicy to allow this."
                )
        else:
            missing = [p for p in parent_ids if p not in self._known_artifacts]
            if missing:
                raise MissingParentError(
                    f"Parent artifact(s) not found in graph: "
                    f"{[a.to_string() for a in missing]!r}. "
                    "Parents must be appended before their dependants."
                )

        # 6. Acyclicity — in an append-only graph where parents must preexist,
        #    a cycle can only arise if:
        #      a) output is identical to a parent (caught above), OR
        #      b) output is an ancestor of one of its declared parents.
        #    Condition (b) is impossible because the output is not yet in
        #    _known_artifacts; it is being introduced for the first time (ensured
        #    by the DuplicateArtifactError check above). Therefore no additional
        #    DFS traversal is required — the structural invariant is sufficient.
        #
        #    This O(1) argument holds strictly for append-only, parent-must-preexist
        #    graphs and is deliberately documented for auditability.
        #
        #    For Tier-0 defensive correctness, we also explicitly verify that the
        #    output artifact is not in the ancestor closure of any parent:
        for parent_id in parent_ids:
            # Compute ancestor closure of this parent (all transitive ancestors)
            # Use deterministic BFS traversal (sorted frontier) for replay-equal behavior
            ancestor_closure: Set[ArtifactID] = set()
            ancestor_frontier: List[ArtifactID] = [parent_id]
            while ancestor_frontier:
                # Sort frontier to ensure deterministic processing order
                ancestor_frontier.sort()
                current = ancestor_frontier.pop(0)  # Use pop(0) for queue-like behavior
                if current in ancestor_closure:
                    continue
                ancestor_closure.add(current)
                # Add all parents of current to frontier (deterministic order via sorted tuple)
                for grandparent in sorted(self._parent_index.get(current, ())):
                    if grandparent not in ancestor_closure:
                        ancestor_frontier.append(grandparent)
            # Defensive check: output must not be in any parent's ancestor closure
            if out_id in ancestor_closure:
                raise CycleError(
                    f"output_artifact_id {out_id.to_string()!r} is an ancestor of "
                    f"parent {parent_id.to_string()!r}, which would create a cycle."
                )

        # --- all checks passed; commit ---------------------------------------

        records_by_node: Dict[LineageNodeID, LineageRecord] = self._records_by_node_id
        record_by_out:   Dict[ArtifactID, LineageRecord]    = self._record_by_output_artifact
        children_idx:    Dict[ArtifactID, List[ArtifactID]] = self._children_index
        parent_idx:      Dict[ArtifactID, Tuple[ArtifactID, ...]] = self._parent_index
        logical_order:   List[LineageNodeID]                = self._logical_order
        known:           Set[ArtifactID]                    = self._known_artifacts

        records_by_node[node_id]   = record
        record_by_out[out_id]      = record
        parent_idx[out_id]         = parent_ids          # already canonical-sorted tuple
        logical_order.append(node_id)
        known.add(out_id)

        # Initialise children list for this artifact (no children yet)
        children_idx.setdefault(out_id, [])

        # Register this artifact as a child of each parent
        for p in parent_ids:
            children_idx.setdefault(p, []).append(out_id)

        self._set("_last_logical_timestamp", ts)

    # -- provenance queries --------------------------------------------------

    def get_record(self, node_id: LineageNodeID) -> LineageRecord:
        """Return the LineageRecord for the given node ID. O(1)."""
        try:
            return self._records_by_node_id[node_id]
        except KeyError:
            raise KeyError(f"LineageNodeID {node_id.to_string()!r} not found in graph.")

    def get_record_by_artifact(self, artifact_id: ArtifactID) -> LineageRecord:
        """Return the record that produced the given artifact. O(1)."""
        try:
            return self._record_by_output_artifact[artifact_id]
        except KeyError:
            raise KeyError(
                f"ArtifactID {artifact_id.to_string()!r} has no producing record in graph."
            )

    def get_parents(self, artifact_id: ArtifactID) -> Tuple[ArtifactID, ...]:
        """
        Return the parent ArtifactIDs of the given artifact.
        Canonical-sorted tuple. Empty tuple for genesis nodes. O(1).
        """
        if artifact_id not in self._known_artifacts:
            raise KeyError(f"ArtifactID {artifact_id.to_string()!r} not found in graph.")
        return self._parent_index.get(artifact_id, ())

    def get_children(self, artifact_id: ArtifactID) -> Tuple[ArtifactID, ...]:
        """
        Return the child ArtifactIDs of the given artifact in append order.
        Returns empty tuple if the artifact has no children. O(1).
        """
        if artifact_id not in self._known_artifacts:
            raise KeyError(f"ArtifactID {artifact_id.to_string()!r} not found in graph.")
        return tuple(self._children_index.get(artifact_id, []))

    def contains_artifact(self, artifact_id: ArtifactID) -> bool:
        return artifact_id in self._known_artifacts

    def contains_node(self, node_id: LineageNodeID) -> bool:
        return node_id in self._records_by_node_id

    # -- deterministic traversal ---------------------------------------------

    def topological_order(self) -> Tuple[LineageNodeID, ...]:
        """
        Return all node IDs in topological order.

        In an append-only DAG where parents must preexist, insertion order IS
        topological order — no recomputation required. Guaranteed stable across
        replay when records are replayed in original logical_timestamp order.
        """
        return tuple(self._logical_order)

    def iter_records(self) -> Iterator[LineageRecord]:
        """Iterate all records in topological (append) order."""
        for node_id in self._logical_order:
            yield self._records_by_node_id[node_id]

    # -- subgraph extraction -------------------------------------------------

    def extract_subgraph(self, root_artifact_id: ArtifactID) -> "LineageGraph":
        """
        Return a new LineageGraph containing only the root artifact and all
        of its transitive ancestors, preserving original topological order.

        Non-mutating. Safe for concurrent read access. O(n) in ancestors.
        Critical for: recovery repair, snapshot export, targeted replay.
        """
        if root_artifact_id not in self._known_artifacts:
            raise KeyError(
                f"ArtifactID {root_artifact_id.to_string()!r} not found in graph."
            )

        # Collect all ancestor artifact IDs via BFS with deterministic ordering
        # Frontier is processed in deterministic order (sorted) to ensure replay-equal traversal
        visited: Set[ArtifactID] = set()
        frontier: List[ArtifactID] = [root_artifact_id]
        while frontier:
            # Sort frontier to ensure deterministic processing order (Tier-0 requirement)
            frontier.sort()
            current = frontier.pop(0)  # Use pop(0) for queue-like BFS behavior
            if current in visited:
                continue
            visited.add(current)
            # Parents are already in canonical-sorted tuple order, but ensure deterministic
            # iteration by explicitly sorting (defensive for Tier-0 determinism)
            for parent in sorted(self._parent_index.get(current, ())):
                if parent not in visited:
                    frontier.append(parent)

        # Collect corresponding records in original topological order
        ancestor_output_ids: Set[ArtifactID] = visited
        subgraph_records: List[LineageRecord] = [
            self._records_by_node_id[node_id]
            for node_id in self._logical_order
            if self._record_by_output_artifact.get(
                self._records_by_node_id[node_id].output_artifact_id
            ) is not None
            and self._records_by_node_id[node_id].output_artifact_id in ancestor_output_ids
        ]

        # Reconstruct into a new graph using the same genesis policy
        sub = LineageGraph(genesis_policy=self._genesis_policy)
        for rec in subgraph_records:
            sub.append(rec)
        return sub

    # -- integrity validation ------------------------------------------------

    def validate_integrity(self) -> None:
        """
        Full structural integrity audit. Raises IntegrityError on any violation.

        Checks:
          - Every node_id in logical_order is registered
          - Every registered record's node_id matches re-derivation (content hash)
          - Every registered parent exists in known artifacts
          - Logical timestamps are strictly increasing across the order
          - Every output artifact maps back to exactly one record
          - Schema version rules are respected per record
          - Migration linkage consistency
          - No cycles (acyclicity re-verified via DFS)

        Call on startup, post-replay, post-migration, and post-recovery.
        """
        seen_artifacts: Set[ArtifactID] = set()
        last_ts = -1

        for node_id in self._logical_order:
            # 1. Node ID registered
            if node_id not in self._records_by_node_id:
                raise IntegrityError(
                    f"logical_order references node {node_id.to_string()!r} "
                    "which is not in _records_by_node_id."
                )

            record = self._records_by_node_id[node_id]

            # 2. Logical timestamp monotonicity
            if record.logical_timestamp <= last_ts:
                raise IntegrityError(
                    f"Non-monotonic logical_timestamp {record.logical_timestamp!r} "
                    f"after {last_ts!r} at node {node_id.to_string()!r}."
                )
            last_ts = record.logical_timestamp

            # 3. output_artifact_id maps back to this record
            out_id = record.output_artifact_id
            mapped = self._record_by_output_artifact.get(out_id)
            if mapped is None or mapped.lineage_node_id != node_id:
                raise IntegrityError(
                    f"_record_by_output_artifact for {out_id.to_string()!r} "
                    f"does not point back to node {node_id.to_string()!r}."
                )

            # 4. No duplicate output artifact across the validated set
            if out_id in seen_artifacts:
                raise IntegrityError(
                    f"ArtifactID {out_id.to_string()!r} appears as output in multiple records."
                )
            seen_artifacts.add(out_id)

            # 5. All parents exist (must have been seen already in topological order)
            for parent_id in record.input_artifact_ids:
                if parent_id not in seen_artifacts:
                    raise IntegrityError(
                        f"Parent {parent_id.to_string()!r} of node {node_id.to_string()!r} "
                        "either does not exist or appears after its child in topological order."
                    )

            # 6. Schema version rules
            if record.transformation_type is TransformationType.MIGRATION:
                if record.migration_id is None:
                    raise IntegrityError(
                        f"MIGRATION node {node_id.to_string()!r} has no migration_id."
                    )
                if record.input_schema_version == record.output_schema_version:
                    raise IntegrityError(
                        f"MIGRATION node {node_id.to_string()!r} has identical "
                        "input and output schema versions."
                    )
            else:
                if record.migration_id is not None:
                    raise IntegrityError(
                        f"Non-MIGRATION node {node_id.to_string()!r} carries migration_id "
                        f"{record.migration_id.to_string()!r}."
                    )
                if record.input_schema_version != record.output_schema_version:
                    raise IntegrityError(
                        f"Non-MIGRATION node {node_id.to_string()!r} changes schema version "
                        f"from {record.input_schema_version!r} to {record.output_schema_version!r}."
                    )

            # 7. Content hash re-verification
            #    Re-derive the node_id from the record's serialised content and compare.
            #    Note: node_id derivation excludes logical_timestamp, so we reconstruct with None.
            reconstructed = LineageRecord(
                output_artifact_id=record.output_artifact_id,
                input_artifact_ids=record.input_artifact_ids,
                artifact_type=record.artifact_type,
                transformation_type=record.transformation_type,
                input_schema_version=record.input_schema_version,
                output_schema_version=record.output_schema_version,
                migration_id=record.migration_id,
                transformation_payload_hash=record.transformation_payload_hash,
                logical_timestamp=None,  # Not part of node_id derivation
            )
            # Restore timestamp for completeness (node_id derivation excludes it)
            if record.logical_timestamp is not None:
                object.__setattr__(reconstructed, "logical_timestamp", record.logical_timestamp)
            if reconstructed.lineage_node_id != node_id:
                raise IntegrityError(
                    f"Node {node_id.to_string()!r} fails content hash re-verification. "
                    f"Derived ID: {reconstructed.lineage_node_id.to_string()!r}. "
                    "Record may have been tampered with."
                )

        # 8. Acyclicity re-verification via DFS over the full graph
        self._verify_acyclic()

    def _verify_acyclic(self) -> None:
        """
        Full DFS cycle detection over the complete artifact dependency graph.
        Raises IntegrityError if any cycle is found.
        
        Uses deterministic traversal order (sorted artifacts) to ensure
        replay-equal cycle detection across all Python versions and processes.
        """
        WHITE, GREY, BLACK = 0, 1, 2
        # Initialize color dict with deterministic iteration order
        # Sort artifacts to ensure deterministic DFS traversal
        sorted_artifacts = sorted(self._known_artifacts)
        color: Dict[ArtifactID, int] = {a: WHITE for a in sorted_artifacts}

        def dfs(node: ArtifactID) -> None:
            color[node] = GREY
            # Children list maintains append order, but sort for deterministic iteration
            # (defensive for Tier-0 determinism guarantee)
            for child in sorted(self._children_index.get(node, [])):
                if color[child] == GREY:
                    raise IntegrityError(
                        f"Cycle detected in lineage graph involving artifact "
                        f"{node.to_string()!r} → {child.to_string()!r}."
                    )
                if color[child] == WHITE:
                    dfs(child)
            color[node] = BLACK

        # Iterate in deterministic sorted order
        for artifact in sorted_artifacts:
            if color[artifact] == WHITE:
                dfs(artifact)

    # -- sizing --------------------------------------------------------------

    def __len__(self) -> int:
        """Number of records in the graph."""
        return len(self._logical_order)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, LineageNodeID):
            return self.contains_node(item)
        if isinstance(item, ArtifactID):
            return self.contains_artifact(item)
        return False

    # -- repr ----------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"LineageGraph("
            f"records={len(self._logical_order)}, "
            f"artifacts={len(self._known_artifacts)}, "
            f"last_ts={self._last_logical_timestamp!r}"
            f")"
        )