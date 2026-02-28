"""
/data/versioning/model/version_graph.py

Immutable DAG of allowed SchemaVersion transitions.

Pure structural model.
No migration execution.
No compatibility policy logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set, List, Iterable, FrozenSet
from collections import deque
from .version import SchemaVersion


# ============================================================================
# ERROR TYPES
# ============================================================================

class InvalidVersionGraph(ValueError):
    """Raised when graph invariants are violated."""


# ============================================================================
# CORE DATA MODEL
# ============================================================================

@dataclass(frozen=True, slots=True)
class VersionGraph:
    """
    Immutable DAG defining allowed direct transitions between schema versions.

    edges[A] = {B, C}
    Means:
        A → B
        A → C

    This graph is purely structural and does NOT:
    - Execute migrations
    - Enforce compatibility semantics
    - Know about semantic version categories
    - Access runtime state
    - Modify state
    - Query registry
    """

    _edges: Dict[SchemaVersion, FrozenSet[SchemaVersion]]

    # ========================================================================
    # CONSTRUCTION WITH VALIDATION
    # ========================================================================

    @classmethod
    def build(
        cls,
        edges: Dict[SchemaVersion, Iterable[SchemaVersion]]
    ) -> VersionGraph:
        """
        Build and validate a VersionGraph.

        Normalizes input, validates structure, and ensures immutability.

        Args:
            edges: Dictionary mapping source versions to iterable of target versions.

        Returns:
            Validated immutable VersionGraph.

        Raises:
            InvalidVersionGraph: If graph contains cycles or self-loops.
        """
        # Normalize iterables to frozen sets for true immutability
        # This ensures deep immutability: even internal collections cannot be mutated
        normalized: Dict[SchemaVersion, FrozenSet[SchemaVersion]] = {
            src: frozenset(targets) for src, targets in edges.items()
        }

        # Create instance
        graph = cls(_edges=normalized)

        # Validate invariants
        graph._validate_no_self_loops()
        graph._validate_no_cycles()

        return graph

    # ========================================================================
    # CORE INVARIANTS
    # ========================================================================

    def _validate_no_self_loops(self) -> None:
        """Ensure no version transitions to itself."""
        for src, targets in self._edges.items():
            if src in targets:
                raise InvalidVersionGraph(
                    f"Self-loop detected at {src}"
                )

    def _validate_no_cycles(self) -> None:
        """
        Ensure graph is acyclic using DFS cycle detection.

        This is foundational. If this fails, your migration engine is unsafe.
        """
        visited: Set[SchemaVersion] = set()
        stack: Set[SchemaVersion] = set()

        def dfs(node: SchemaVersion) -> None:
            if node in stack:
                raise InvalidVersionGraph(
                    f"Cycle detected involving {node}"
                )

            if node in visited:
                return

            visited.add(node)
            stack.add(node)

            for neighbor in self._edges.get(node, frozenset()):
                dfs(neighbor)

            stack.remove(node)

        # Check all nodes in the graph
        for node in self._edges:
            if node not in visited:
                dfs(node)

    # ========================================================================
    # REACHABILITY
    # ========================================================================

    def is_reachable(
        self,
        source: SchemaVersion,
        target: SchemaVersion
    ) -> bool:
        """
        Determine if target is reachable from source.

        Args:
            source: Starting version.
            target: Target version to check reachability.

        Returns:
            True if target is reachable from source, False otherwise.
        """
        if source == target:
            return True

        visited: Set[SchemaVersion] = set()

        def dfs(node: SchemaVersion) -> bool:
            if node == target:
                return True
            if node in visited:
                return False

            visited.add(node)

            for neighbor in self._edges.get(node, frozenset()):
                if dfs(neighbor):
                    return True

            return False

        return dfs(source)

    # ========================================================================
    # PATH RESOLUTION (DETERMINISTIC)
    # ========================================================================

    def find_path(
        self,
        source: SchemaVersion,
        target: SchemaVersion
    ) -> List[SchemaVersion]:
        """
        Return shortest path from source to target.

        Uses BFS with deterministic neighbor ordering to ensure
        consistent path resolution across runs.

        Args:
            source: Starting version.
            target: Target version.

        Returns:
            List of versions representing the shortest path from source to target.

        Raises:
            InvalidVersionGraph: If no path exists from source to target.
        """
        if source == target:
            return [source]

        queue = deque([[source]])
        visited = {source}

        while queue:
            path = queue.popleft()
            node = path[-1]

            if node == target:
                return path

            # Sort neighbors to ensure deterministic traversal order
            # At scale, non-deterministic traversal causes inconsistent
            # migration ordering across nodes.
            # Note: SchemaVersion implements @total_ordering, so sorting is safe.
            neighbors = self._edges.get(node, frozenset())
            for neighbor in sorted(neighbors):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(path + [neighbor])

        raise InvalidVersionGraph(
            f"No path from {source} to {target}"
        )

    # ========================================================================
    # TOPOLOGICAL ORDER
    # ========================================================================

    def topological_sort(self) -> List[SchemaVersion]:
        """
        Return deterministic topological ordering.

        Uses Kahn's algorithm with deterministic queue ordering.

        Returns:
            List of versions in topological order.

        Raises:
            InvalidVersionGraph: If graph is not acyclic (should never happen
                if validation passed, but included for safety).
        """
        # Calculate in-degrees
        in_degree: Dict[SchemaVersion, int] = {}

        # Initialize degrees for all nodes
        for src in self._edges:
            in_degree.setdefault(src, 0)
            for tgt in self._edges[src]:
                in_degree[tgt] = in_degree.get(tgt, 0) + 1

        # Also include nodes that only appear as targets
        all_nodes = set(self._edges.keys())
        for targets in self._edges.values():
            all_nodes.update(targets)
        for node in all_nodes:
            in_degree.setdefault(node, 0)

        # Start with nodes that have no incoming edges
        # Note: SchemaVersion implements @total_ordering, so sorting is safe.
        queue = sorted(
            [node for node, deg in in_degree.items() if deg == 0]
        )

        result: List[SchemaVersion] = []

        while queue:
            # Pop from front (deterministic: always smallest)
            node = queue.pop(0)
            result.append(node)

            # Process neighbors
            neighbors = self._edges.get(node, frozenset())
            for neighbor in sorted(neighbors):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
                    # Keep queue sorted for determinism
                    queue.sort()

        # Verify we processed all nodes (sanity check)
        if len(result) != len(in_degree):
            raise InvalidVersionGraph("Graph is not acyclic.")

        return result

    # ========================================================================
    # ORPHAN DETECTION
    # ========================================================================

    def find_orphans(self) -> Set[SchemaVersion]:
        """
        Return nodes with no incoming and no outgoing edges.

        Critical for CI: prevents dead schema definitions.

        Returns:
            Set of orphaned versions.
        """
        # Collect all nodes in the graph
        all_nodes = set(self._edges.keys())
        incoming = set()

        # Collect all nodes that have incoming edges
        for targets in self._edges.values():
            incoming.update(targets)

        # Also include nodes that only appear as targets
        all_nodes.update(incoming)

        # Orphans: nodes that have:
        #   1. No incoming edges (not in incoming set)
        #   2. No outgoing edges (either not a key, or key with empty edge set)
        orphans = set()
        for node in all_nodes:
            has_incoming = node in incoming
            has_outgoing = (
                node in self._edges and len(self._edges[node]) > 0
            )
            
            if not has_incoming and not has_outgoing:
                orphans.add(node)

        return orphans

    # ========================================================================
    # QUERY HELPERS
    # ========================================================================

    def get_successors(self, version: SchemaVersion) -> Set[SchemaVersion]:
        """
        Get all direct successors of a version.

        Args:
            version: Source version.

        Returns:
            Set of versions directly reachable from this version.
        """
        # Return a copy to prevent external mutation
        # (though internal storage is already frozen)
        successors = self._edges.get(version, frozenset())
        return set(successors)

    def get_all_nodes(self) -> Set[SchemaVersion]:
        """
        Get all nodes in the graph.

        Returns:
            Set of all versions present in the graph.
        """
        all_nodes = set(self._edges.keys())
        for targets in self._edges.values():
            all_nodes.update(targets)
        return all_nodes

    def has_edge(
        self,
        source: SchemaVersion,
        target: SchemaVersion
    ) -> bool:
        """
        Check if a direct edge exists from source to target.

        Args:
            source: Source version.
            target: Target version.

        Returns:
            True if direct edge exists, False otherwise.
        """
        return target in self._edges.get(source, frozenset())

    # ========================================================================
    # FORK DETECTION
    # ========================================================================

    def find_forks(self) -> Dict[SchemaVersion, FrozenSet[SchemaVersion]]:
        """
        Return nodes that have multiple outgoing edges (forks).

        A fork occurs when a version can transition to multiple different versions.
        This is allowed structurally but may indicate ambiguous upgrade paths.

        Returns:
            Dictionary mapping source versions to their multiple target versions.
            Only includes sources with 2+ outgoing edges.
        """
        forks: Dict[SchemaVersion, FrozenSet[SchemaVersion]] = {}
        for src, targets in self._edges.items():
            if len(targets) > 1:
                forks[src] = targets
        return forks

    # ========================================================================
    # REPRESENTATION
    # ========================================================================

    def __repr__(self) -> str:
        """Developer-friendly representation."""
        edge_count = sum(len(targets) for targets in self._edges.values())
        node_count = len(self.get_all_nodes())
        return (
            f"VersionGraph(nodes={node_count}, edges={edge_count})"
        )
