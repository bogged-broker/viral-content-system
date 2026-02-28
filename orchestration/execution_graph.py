"""
/orchestration/execution_graph.py

Explicit Dependency DAGs for production-grade orchestration.

This is the causal spine of the system. If this file is wrong:
- workflows appear to work
- failures cascade invisibly
- retries amplify damage
- RL credit assignment becomes corrupted
- audits become impossible

HARD RULE: If an action is not in an ExecutionGraph, it is illegal to execute.

This file defines structure, causality, and legality. It never executes anything.

Mental Model: Think compiler IR + OS dependency graph, not Airflow fluff.
- Nodes = atomic execution units
- Edges = hard causal dependencies
- Graph = contract, not suggestion
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, List, Dict
from collections import defaultdict, deque
import json
from datetime import datetime


# ============================================================================
# Core Enumerations
# ============================================================================

class ExecutionPhase(Enum):
    """
    Strictly ordered execution phases.
    
    Rules:
    - Phases are strictly ordered
    - Backward edges are forbidden
    - Phase order is enforced globally
    """
    INGESTION = "ingestion"
    FEATURE_EXTRACTION = "feature_extraction"
    DECISION = "decision"
    GENERATION = "generation"
    POSTPROCESS = "postprocess"
    DEPLOYMENT = "deployment"
    EVALUATION = "evaluation"
    
    def __lt__(self, other: 'ExecutionPhase') -> bool:
        """Enable phase ordering comparisons."""
        if not isinstance(other, ExecutionPhase):
            return NotImplemented
        phase_order = list(ExecutionPhase)
        return phase_order.index(self) < phase_order.index(other)
    
    def __le__(self, other: 'ExecutionPhase') -> bool:
        return self == other or self < other
    
    def __gt__(self, other: 'ExecutionPhase') -> bool:
        if not isinstance(other, ExecutionPhase):
            return NotImplemented
        return not self <= other
    
    def __ge__(self, other: 'ExecutionPhase') -> bool:
        return self == other or self > other


class FailureType(Enum):
    """Failure tolerance types (mirrors failure_policy.py)."""
    CRITICAL = "critical"           # Must succeed, no tolerance
    DEGRADABLE = "degradable"       # Can continue with degraded service
    OPTIONAL = "optional"           # Failure is acceptable
    RETRIABLE = "retriable"         # Should retry on failure


# ============================================================================
# Core Data Structures
# ============================================================================

@dataclass(frozen=True)
class ExecutionNode:
    """
    Atomic execution unit.
    
    No optional fields. No runtime mutation.
    """
    node_id: str                                    # globally unique
    name: str                                       # human-readable
    owner: str                                      # module or agent
    phase: ExecutionPhase                           # execution phase
    
    inputs: frozenset[str]                          # required artifacts
    outputs: frozenset[str]                         # produced artifacts
    
    side_effects: bool                              # touches external world?
    idempotent: bool                                # safe to retry?
    deterministic: bool                             # same input → same output
    
    failure_tolerance: FailureType                  # from failure_policy.py
    retry_allowed: bool                             # may be retried?
    
    observability_tags: dict[str, str] = field(default_factory=dict)  # audit & tracing
    
    def __post_init__(self):
        """Validate node invariants."""
        # Validate required fields
        if not self.node_id or not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError(f"Node must have non-empty string node_id, got: {self.node_id!r}")
        
        if not self.name or not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"Node {self.node_id} must have non-empty string name")
        
        if not self.owner or not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError(f"Node {self.node_id} must have non-empty string owner")
        
        # Validate node_id format (should be valid identifier)
        if not self.node_id.replace("_", "").replace("-", "").isalnum():
            raise ValueError(
                f"Node ID '{self.node_id}' must be alphanumeric with underscores/hyphens only"
            )
        
        # Validate inputs/outputs are sets of strings
        if not isinstance(self.inputs, frozenset):
            raise TypeError(f"Node {self.node_id} inputs must be frozenset")
        
        if not isinstance(self.outputs, frozenset):
            raise TypeError(f"Node {self.node_id} outputs must be frozenset")
        
        for artifact in self.inputs:
            if not isinstance(artifact, str) or not artifact.strip():
                raise ValueError(
                    f"Node {self.node_id} has invalid input artifact: {artifact!r}"
                )
        
        for artifact in self.outputs:
            if not isinstance(artifact, str) or not artifact.strip():
                raise ValueError(
                    f"Node {self.node_id} has invalid output artifact: {artifact!r}"
                )
        
        # Non-idempotent nodes cannot be retryable
        if not self.idempotent and self.retry_allowed:
            raise ValueError(
                f"Node {self.node_id} is non-idempotent but marked retryable. "
                f"Non-idempotent nodes cannot be safely retried."
            )
        
        # Side-effect nodes must be in DEPLOYMENT phase
        if self.side_effects and self.phase != ExecutionPhase.DEPLOYMENT:
            raise ValueError(
                f"Node {self.node_id} has side effects but is not in DEPLOYMENT phase "
                f"(current phase: {self.phase.value}). Side effects are only allowed in DEPLOYMENT."
            )
        
        # Validate observability_tags
        if not isinstance(self.observability_tags, dict):
            raise TypeError(f"Node {self.node_id} observability_tags must be dict")
        
        for key, value in self.observability_tags.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"Node {self.node_id} observability_tags keys must be strings"
                )
            if not isinstance(value, str):
                raise TypeError(
                    f"Node {self.node_id} observability_tags values must be strings"
                )


@dataclass(frozen=True)
class DependencyEdge:
    """
    Explicit causal dependency.
    
    Every edge is explicit. No inferred dependencies.
    """
    upstream: str           # node_id
    downstream: str         # node_id
    required: bool          # hard vs soft dependency
    artifact: Optional[str] # what flows across edge (None for control-only)
    
    def __post_init__(self):
        """Validate edge."""
        # Validate required fields
        if not self.upstream or not isinstance(self.upstream, str) or not self.upstream.strip():
            raise ValueError(f"Edge must have non-empty string upstream, got: {self.upstream!r}")
        
        if not self.downstream or not isinstance(self.downstream, str) or not self.downstream.strip():
            raise ValueError(f"Edge must have non-empty string downstream, got: {self.downstream!r}")
        
        # Self-loops are forbidden
        if self.upstream == self.downstream:
            raise ValueError(
                f"Self-loop detected: {self.upstream} → {self.downstream}. "
                f"Cycles are forbidden in execution graphs."
            )
        
        # Validate artifact if specified
        if self.artifact is not None:
            if not isinstance(self.artifact, str) or not self.artifact.strip():
                raise ValueError(
                    f"Edge {self.upstream} → {self.downstream} has invalid artifact: {self.artifact!r}"
                )


@dataclass(frozen=True)
class ExecutionPlan:
    """
    Resolved, deterministic execution view.
    
    This is what workflow_manager.py consumes.
    """
    ordered_nodes: list[str]                        # deterministic order
    parallel_groups: list[list[str]]                # safe concurrency groups
    cancellation_map: dict[str, list[str]]          # failure propagation
    
    def __post_init__(self):
        """Validate plan consistency."""
        # All nodes in parallel groups must be in ordered_nodes
        all_parallel = set()
        for group in self.parallel_groups:
            all_parallel.update(group)
        
        ordered_set = set(self.ordered_nodes)
        if not all_parallel.issubset(ordered_set):
            raise ValueError("Parallel groups contain nodes not in ordered_nodes")


# ============================================================================
# Graph Validation Components
# ============================================================================

class CycleDetector:
    """
    Detects cycles in execution graph.
    
    HARD FAIL on any cycle.
    No "allowed cycles." No "feedback loops."
    Feedback is modeled via separate episodes, never DAG loops.
    """
    
    @staticmethod
    def detect_cycle(
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> Optional[list[str]]:
        """
        Detect cycles using DFS.
        
        Returns:
            Cycle path if found, None otherwise
        """
        # Build adjacency list
        graph = defaultdict(list)
        for edge in edges:
            graph[edge.upstream].append(edge.downstream)
        
        # Track visit states: 0=unvisited, 1=visiting, 2=visited
        state = {node_id: 0 for node_id in nodes}
        parent = {}
        
        def dfs(node: str, path: list[str]) -> Optional[list[str]]:
            if state[node] == 1:  # Currently visiting - cycle detected
                # Find cycle start
                cycle_start = path.index(node)
                return path[cycle_start:] + [node]
            
            if state[node] == 2:  # Already visited
                return None
            
            state[node] = 1
            path.append(node)
            
            for neighbor in graph[node]:
                if neighbor in nodes:  # Only follow edges to known nodes
                    cycle = dfs(neighbor, path)
                    if cycle:
                        return cycle
            
            path.pop()
            state[node] = 2
            return None
        
        # Check all nodes
        for node_id in nodes:
            if state[node_id] == 0:
                cycle = dfs(node_id, [])
                if cycle:
                    return cycle
        
        return None


class PhaseBoundaryEnforcer:
    """
    Enforces phase ordering rules.
    
    Rules:
    - FEATURE_EXTRACTION cannot depend on GENERATION
    - DEPLOYMENT cannot precede EVALUATION
    - EVALUATION cannot influence GENERATION in same graph
    - Side-effects only allowed in DEPLOYMENT phase
    
    This protects: causality, RL correctness, audit safety
    """
    
    @staticmethod
    def validate_phase_boundaries(
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> list[str]:
        """
        Validate all phase boundary rules.
        
        Returns:
            List of violation messages (empty if valid)
        """
        violations = []
        
        for edge in edges:
            if edge.upstream not in nodes or edge.downstream not in nodes:
                continue  # Handled by other validators
            
            upstream_node = nodes[edge.upstream]
            downstream_node = nodes[edge.downstream]
            
            # Check phase ordering
            if upstream_node.phase > downstream_node.phase:
                violations.append(
                    f"Phase regression: {edge.upstream} ({upstream_node.phase.value}) "
                    f"→ {edge.downstream} ({downstream_node.phase.value})"
                )
            
            # Specific phase rules
            if (downstream_node.phase == ExecutionPhase.FEATURE_EXTRACTION and
                upstream_node.phase == ExecutionPhase.GENERATION):
                violations.append(
                    f"FEATURE_EXTRACTION cannot depend on GENERATION: "
                    f"{edge.downstream} ← {edge.upstream}"
                )
            
            if (downstream_node.phase == ExecutionPhase.GENERATION and
                upstream_node.phase == ExecutionPhase.EVALUATION):
                violations.append(
                    f"EVALUATION cannot influence GENERATION: "
                    f"{edge.downstream} ← {edge.upstream}"
                )
        
        # Check side-effect nodes
        for node_id, node in nodes.items():
            if node.side_effects and node.phase != ExecutionPhase.DEPLOYMENT:
                violations.append(
                    f"Side-effect node {node_id} must be in DEPLOYMENT phase, "
                    f"not {node.phase.value}"
                )
        
        return violations


class FailurePropagationResolver:
    """
    Defines failure propagation structure (not behavior).
    
    Determines:
    - which downstream nodes are cancelled
    - which optional branches may continue
    - which failures escalate to workflow abort
    
    Example rules:
    - hard dependency failure → cancel downstream
    - soft dependency failure → skip optional branch
    - deployment failure → trigger containment policy
    """
    
    @staticmethod
    def build_cancellation_map(
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> dict[str, list[str]]:
        """
        Build map of node → nodes to cancel on failure.
        
        Returns:
            Dict mapping node_id to list of downstream nodes to cancel
        """
        # Build adjacency list
        downstream_map = defaultdict(set)
        required_edges = set()
        
        for edge in edges:
            downstream_map[edge.upstream].add(edge.downstream)
            if edge.required:
                required_edges.add((edge.upstream, edge.downstream))
        
        cancellation_map = {}
        
        for node_id in nodes:
            # Find all nodes reachable via required dependencies
            to_cancel = set()
            queue = deque([node_id])
            visited = {node_id}
            
            while queue:
                current = queue.popleft()
                
                for downstream in downstream_map[current]:
                    # Only follow required edges
                    if (current, downstream) in required_edges:
                        if downstream not in visited:
                            visited.add(downstream)
                            to_cancel.add(downstream)
                            queue.append(downstream)
            
            cancellation_map[node_id] = sorted(list(to_cancel))
        
        return cancellation_map


class GraphValidator:
    """
    Comprehensive graph validation.
    
    Validates:
    - cycles
    - missing nodes
    - dangling edges
    - phase regressions
    - artifact mismatches
    - node invariants
    - RL-unsafe nodes upstream of reward computation
    
    Silent tolerance is forbidden.
    """
    
    def __init__(self):
        self.cycle_detector = CycleDetector()
        self.phase_enforcer = PhaseBoundaryEnforcer()
    
    def validate(
        self,
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> None:
        """
        Validate entire graph.
        
        Raises:
            ValueError: On any validation failure
        """
        errors = []
        
        # Check for cycles
        cycle = self.cycle_detector.detect_cycle(nodes, edges)
        if cycle:
            errors.append(f"Cycle detected: {' → '.join(cycle)}")
        
        # Check for dangling edges
        node_ids = set(nodes.keys())
        for edge in edges:
            if edge.upstream not in node_ids:
                errors.append(f"Dangling edge: upstream node {edge.upstream} not found")
            if edge.downstream not in node_ids:
                errors.append(f"Dangling edge: downstream node {edge.downstream} not found")
        
        # Check phase boundaries
        phase_violations = self.phase_enforcer.validate_phase_boundaries(nodes, edges)
        errors.extend(phase_violations)
        
        # Check artifact flow consistency
        artifact_errors = self._validate_artifacts(nodes, edges)
        errors.extend(artifact_errors)
        
        # Check input requirements
        input_errors = self._validate_input_requirements(nodes, edges)
        errors.extend(input_errors)
        
        # Check RL-unsafe nodes upstream of reward computation
        rl_errors = self._validate_rl_safety(nodes, edges)
        errors.extend(rl_errors)
        
        if errors:
            raise ValueError(
                f"Graph validation failed with {len(errors)} error(s):\n" +
                "\n".join(f"  - {err}" for err in errors)
            )
    
    def _validate_artifacts(
        self,
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> list[str]:
        """Validate artifact flow consistency."""
        errors = []
        
        # Build map of which nodes produce which artifacts
        producers = defaultdict(list)
        for node_id, node in nodes.items():
            for artifact in node.outputs:
                producers[artifact].append(node_id)
        
        # Check that required artifacts are produced
        for edge in edges:
            if edge.artifact and edge.required:
                if edge.upstream not in nodes or edge.downstream not in nodes:
                    continue  # Handled elsewhere
                
                upstream_node = nodes[edge.upstream]
                downstream_node = nodes[edge.downstream]
                
                # Check upstream produces the artifact
                if edge.artifact not in upstream_node.outputs:
                    errors.append(
                        f"Edge {edge.upstream} → {edge.downstream} requires artifact "
                        f"'{edge.artifact}' but upstream doesn't produce it"
                    )
                
                # Check downstream requires the artifact
                if edge.artifact not in downstream_node.inputs:
                    errors.append(
                        f"Edge {edge.upstream} → {edge.downstream} provides artifact "
                        f"'{edge.artifact}' but downstream doesn't require it"
                    )
        
        return errors
    
    def _validate_input_requirements(
        self,
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> list[str]:
        """Validate that all required inputs are satisfied."""
        errors = []
        
        # Build map of artifacts produced by edges
        artifact_sources = defaultdict(set)
        for edge in edges:
            if edge.artifact and edge.upstream in nodes and edge.downstream in nodes:
                artifact_sources[edge.artifact].add(edge.upstream)
        
        # Check each node's inputs are satisfied
        for node_id, node in nodes.items():
            for required_input in node.inputs:
                # Check if input is provided by any upstream node
                if required_input not in artifact_sources:
                    # Check if it's an external input (not produced by graph)
                    # This is acceptable, but we should verify it's not expected
                    pass
                else:
                    # Verify at least one upstream node produces it
                    upstream_providers = artifact_sources[required_input]
                    # Check if any of these providers actually have an edge to this node
                    has_dependency = any(
                        edge.downstream == node_id and edge.upstream in upstream_providers
                        for edge in edges
                    )
                    if not has_dependency:
                        errors.append(
                            f"Node {node_id} requires artifact '{required_input}' "
                            f"but no dependency edge provides it"
                        )
        
        return errors
    
    def _validate_rl_safety(
        self,
        nodes: dict[str, ExecutionNode],
        edges: list[DependencyEdge]
    ) -> list[str]:
        """
        Validate RL-unsafe nodes are not upstream of reward computation.
        
        RL-unsafe = non-deterministic nodes that corrupt credit assignment.
        """
        errors = []
        
        # Identify reward computation nodes
        # Convention 1: Explicit tag
        # Convention 2: EVALUATION phase nodes producing reward artifacts
        reward_nodes = []
        for node_id, node in nodes.items():
            is_reward_node = False
            
            # Check explicit tag
            if node.observability_tags.get("rl_reward_computation") == "true":
                is_reward_node = True
            
            # Check phase and artifact naming
            if (node.phase == ExecutionPhase.EVALUATION and 
                any("reward" in artifact.lower() for artifact in node.outputs)):
                is_reward_node = True
            
            # Check node name convention
            if "reward" in node.name.lower() and "compute" in node.name.lower():
                is_reward_node = True
            
            if is_reward_node:
                reward_nodes.append(node_id)
        
        if not reward_nodes:
            # No reward nodes found - this is acceptable (RL may not be used)
            return []
        
        # Build reverse adjacency map for efficient upstream traversal
        upstream_map = defaultdict(set)
        for edge in edges:
            if edge.upstream in nodes and edge.downstream in nodes:
                upstream_map[edge.downstream].add(edge.upstream)
        
        # For each reward node, check all upstream nodes
        for reward_node_id in reward_nodes:
            upstream_nodes = self._get_all_upstream(
                reward_node_id, nodes, upstream_map
            )
            
            for upstream_id in upstream_nodes:
                upstream_node = nodes[upstream_id]
                
                # Non-deterministic nodes corrupt RL credit assignment
                if not upstream_node.deterministic:
                    errors.append(
                        f"RL-unsafe node '{upstream_id}' (non-deterministic) "
                        f"is upstream of reward computation node '{reward_node_id}'. "
                        f"This corrupts RL credit assignment. "
                        f"Either make '{upstream_id}' deterministic or remove dependency path."
                    )
        
        return errors
    
    def _get_all_upstream(
        self,
        node_id: str,
        nodes: dict[str, ExecutionNode],
        upstream_map: dict[str, set[str]]
    ) -> set[str]:
        """Get all upstream nodes (transitive closure)."""
        upstream = set()
        queue = deque([node_id])
        visited = {node_id}
        
        while queue:
            current = queue.popleft()
            
            for upstream_id in upstream_map.get(current, set()):
                if upstream_id not in visited:
                    visited.add(upstream_id)
                    upstream.add(upstream_id)
                    queue.append(upstream_id)
        
        return upstream


# ============================================================================
# Core Execution Graph
# ============================================================================

class ExecutionGraph:
    """
    Core execution graph.
    
    Responsibilities:
    - Explicitly declare every executable node
    - Declare all dependencies (no inference)
    - Validate acyclic structure
    - Enforce phase boundaries
    - Support partial graph execution
    - Support failure propagation rules
    - Provide deterministic topological ordering
    - Support audit replay
    - Be RL-compatible (credit assignment safe)
    - Prevent implicit execution paths
    """
    
    def __init__(self):
        self.nodes: dict[str, ExecutionNode] = {}
        self.edges: list[DependencyEdge] = []
        self._validated: bool = False
        self._validator = GraphValidator()
        self._failure_resolver = FailurePropagationResolver()
        
        # Cached structures (invalidated on graph modification)
        self._adjacency_list: Optional[dict[str, list[str]]] = None
        self._reverse_adjacency: Optional[dict[str, list[str]]] = None
        self._dependencies: Optional[dict[str, set[str]]] = None
        self._topological_order: Optional[list[str]] = None
        self._execution_plan: Optional[ExecutionPlan] = None
    
    def add_node(self, node: ExecutionNode) -> None:
        """
        Add node to graph.
        
        Args:
            node: ExecutionNode to add
            
        Raises:
            ValueError: If node_id already exists or node validation fails
            TypeError: If node is not an ExecutionNode
        """
        if not isinstance(node, ExecutionNode):
            raise TypeError(f"Expected ExecutionNode, got {type(node).__name__}")
        
        if node.node_id in self.nodes:
            existing_node = self.nodes[node.node_id]
            raise ValueError(
                f"Node {node.node_id} already exists in graph. "
                f"Existing: {existing_node.name} (owner: {existing_node.owner}), "
                f"New: {node.name} (owner: {node.owner})"
            )
        
        self.nodes[node.node_id] = node
        self._invalidate_caches()
    
    def add_edge(self, edge: DependencyEdge) -> None:
        """
        Add dependency edge to graph.
        
        Args:
            edge: DependencyEdge to add
            
        Raises:
            ValueError: If edge creates immediate self-loop
            TypeError: If edge is not a DependencyEdge
        """
        if not isinstance(edge, DependencyEdge):
            raise TypeError(f"Expected DependencyEdge, got {type(edge).__name__}")
        
        # Quick validation for common errors (full validation in validate())
        if edge.upstream == edge.downstream:
            raise ValueError(f"Self-loop detected: {edge.upstream} → {edge.downstream}")
        
        # Warn about edges to non-existent nodes (will fail in validate)
        if edge.upstream not in self.nodes:
            # Don't fail here - allow building graph incrementally
            pass
        
        if edge.downstream not in self.nodes:
            # Don't fail here - allow building graph incrementally
            pass
        
        self.edges.append(edge)
        self._invalidate_caches()
    
    def _invalidate_caches(self) -> None:
        """Invalidate all cached structures."""
        self._validated = False
        self._adjacency_list = None
        self._reverse_adjacency = None
        self._dependencies = None
        self._topological_order = None
        self._execution_plan = None
    
    def validate(self) -> None:
        """
        Validate graph structure.
        
        MUST be called before creating execution plan.
        
        Raises:
            ValueError: On any validation failure
        """
        self._validator.validate(self.nodes, self.edges)
        self._validated = True
        # Rebuild caches after validation
        self._build_adjacency_structures()
    
    def _build_adjacency_structures(self) -> None:
        """Build and cache adjacency structures for efficient queries."""
        if self._adjacency_list is not None:
            return  # Already built
        
        self._adjacency_list = defaultdict(list)
        self._reverse_adjacency = defaultdict(list)
        self._dependencies = defaultdict(set)
        
        for edge in self.edges:
            if edge.upstream in self.nodes and edge.downstream in self.nodes:
                self._adjacency_list[edge.upstream].append(edge.downstream)
                self._reverse_adjacency[edge.downstream].append(edge.upstream)
                if edge.required:
                    self._dependencies[edge.downstream].add(edge.upstream)
        
        # Sort for determinism
        for key in self._adjacency_list:
            self._adjacency_list[key] = sorted(self._adjacency_list[key])
        for key in self._reverse_adjacency:
            self._reverse_adjacency[key] = sorted(self._reverse_adjacency[key])
    
    def topological_sort(self) -> list[str]:
        """
        Produce deterministic topological ordering.
        
        Uses cached adjacency structures for performance.
        
        Returns:
            List of node_ids in topological order
            
        Raises:
            ValueError: If graph not validated or contains cycles
        """
        if not self._validated:
            raise ValueError("Graph must be validated before topological sort")
        
        if self._topological_order is not None:
            return self._topological_order.copy()
        
        self._build_adjacency_structures()
        
        # Build in-degree map
        in_degree = {node_id: 0 for node_id in self.nodes}
        for edge in self.edges:
            if edge.upstream in self.nodes and edge.downstream in self.nodes:
                in_degree[edge.downstream] += 1
        
        # Kahn's algorithm with deterministic ordering
        queue = deque(sorted([
            node_id for node_id, degree in in_degree.items() if degree == 0
        ]))
        result = []
        
        while queue:
            # Process in sorted order for determinism
            node = queue.popleft()
            result.append(node)
            
            # Use cached adjacency list
            for neighbor in self._adjacency_list.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        if len(result) != len(self.nodes):
            raise ValueError("Topological sort failed - graph contains cycle")
        
        self._topological_order = result
        return result.copy()
    
    def get_ready_nodes(self, completed: set[str]) -> list[str]:
        """
        Get nodes ready to execute given completed nodes.
        
        Uses cached dependency structures for O(n) performance.
        
        Args:
            completed: Set of completed node_ids
            
        Returns:
            List of node_ids ready to execute (sorted for determinism)
        """
        if not self._validated:
            raise ValueError("Graph must be validated before getting ready nodes")
        
        self._build_adjacency_structures()
        
        ready = []
        for node_id in self.nodes:
            if node_id not in completed:
                # Check if all required dependencies are met
                required_deps = self._dependencies.get(node_id, set())
                if required_deps.issubset(completed):
                    ready.append(node_id)
        
        return sorted(ready)  # Deterministic ordering
    
    def get_downstream(self, node_id: str, required_only: bool = False) -> list[str]:
        """
        Get all downstream nodes (transitive closure).
        
        Uses cached adjacency structures for O(n) performance.
        
        Args:
            node_id: Starting node
            required_only: Only follow required dependencies
            
        Returns:
            List of downstream node_ids (sorted for determinism)
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} not found in graph")
        
        if not self._validated:
            raise ValueError("Graph must be validated before querying downstream nodes")
        
        self._build_adjacency_structures()
        
        downstream = set()
        queue = deque([node_id])
        visited = {node_id}
        
        # Build edge requirement map for fast lookup
        required_edges = {
            (edge.upstream, edge.downstream): edge.required
            for edge in self.edges
        } if required_only else {}
        
        while queue:
            current = queue.popleft()
            
            # Use cached adjacency list
            for neighbor in self._adjacency_list.get(current, []):
                if required_only:
                    if not required_edges.get((current, neighbor), True):
                        continue
                
                if neighbor not in visited:
                    visited.add(neighbor)
                    downstream.add(neighbor)
                    queue.append(neighbor)
        
        return sorted(list(downstream))
    
    def get_upstream(self, node_id: str, required_only: bool = False) -> list[str]:
        """
        Get all upstream nodes (transitive closure).
        
        Uses cached reverse adjacency structures for O(n) performance.
        
        Args:
            node_id: Starting node
            required_only: Only follow required dependencies
            
        Returns:
            List of upstream node_ids (sorted for determinism)
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} not found in graph")
        
        if not self._validated:
            raise ValueError("Graph must be validated before querying upstream nodes")
        
        self._build_adjacency_structures()
        
        upstream = set()
        queue = deque([node_id])
        visited = {node_id}
        
        # Build edge requirement map for fast lookup
        required_edges = {
            (edge.upstream, edge.downstream): edge.required
            for edge in self.edges
        } if required_only else {}
        
        while queue:
            current = queue.popleft()
            
            # Use cached reverse adjacency list
            for neighbor in self._reverse_adjacency.get(current, []):
                if required_only:
                    if not required_edges.get((neighbor, current), True):
                        continue
                
                if neighbor not in visited:
                    visited.add(neighbor)
                    upstream.add(neighbor)
                    queue.append(neighbor)
        
        return sorted(list(upstream))
    
    def get_node(self, node_id: str) -> ExecutionNode:
        """
        Get node by ID.
        
        Args:
            node_id: Node identifier
            
        Returns:
            ExecutionNode
            
        Raises:
            ValueError: If node not found
        """
        if node_id not in self.nodes:
            raise ValueError(f"Node {node_id} not found in graph")
        return self.nodes[node_id]
    
    def get_nodes_by_phase(self, phase: ExecutionPhase) -> list[ExecutionNode]:
        """
        Get all nodes in a specific phase.
        
        Args:
            phase: Execution phase
            
        Returns:
            List of nodes in phase (sorted by node_id for determinism)
        """
        nodes_in_phase = [
            node for node in self.nodes.values()
            if node.phase == phase
        ]
        return sorted(nodes_in_phase, key=lambda n: n.node_id)
    
    def get_nodes_by_owner(self, owner: str) -> list[ExecutionNode]:
        """
        Get all nodes owned by a specific module/agent.
        
        Args:
            owner: Owner identifier
            
        Returns:
            List of nodes owned by owner (sorted by node_id for determinism)
        """
        nodes_by_owner = [
            node for node in self.nodes.values()
            if node.owner == owner
        ]
        return sorted(nodes_by_owner, key=lambda n: n.node_id)
    
    def get_statistics(self) -> dict[str, Any]:
        """
        Get graph statistics.
        
        Returns:
            Dictionary with graph statistics
        """
        if not self._validated:
            return {
                "validated": False,
                "node_count": len(self.nodes),
                "edge_count": len(self.edges)
            }
        
        phase_counts = {}
        for phase in ExecutionPhase:
            phase_counts[phase.value] = len(self.get_nodes_by_phase(phase))
        
        # Count nodes by properties
        deterministic_count = sum(1 for n in self.nodes.values() if n.deterministic)
        idempotent_count = sum(1 for n in self.nodes.values() if n.idempotent)
        side_effect_count = sum(1 for n in self.nodes.values() if n.side_effects)
        
        # Count edges by type
        required_edges = sum(1 for e in self.edges if e.required)
        artifact_edges = sum(1 for e in self.edges if e.artifact is not None)
        
        return {
            "validated": True,
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "phase_counts": phase_counts,
            "deterministic_nodes": deterministic_count,
            "idempotent_nodes": idempotent_count,
            "side_effect_nodes": side_effect_count,
            "required_edges": required_edges,
            "optional_edges": len(self.edges) - required_edges,
            "artifact_edges": artifact_edges,
            "control_edges": len(self.edges) - artifact_edges
        }
    
    def to_dict(self) -> dict[str, Any]:
        """
        Serialize graph to dictionary.
        
        Returns:
            Dictionary representation of graph
        """
        return {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "name": node.name,
                    "owner": node.owner,
                    "phase": node.phase.value,
                    "inputs": list(node.inputs),
                    "outputs": list(node.outputs),
                    "side_effects": node.side_effects,
                    "idempotent": node.idempotent,
                    "deterministic": node.deterministic,
                    "failure_tolerance": node.failure_tolerance.value,
                    "retry_allowed": node.retry_allowed,
                    "observability_tags": node.observability_tags
                }
                for node in sorted(self.nodes.values(), key=lambda n: n.node_id)
            ],
            "edges": [
                {
                    "upstream": edge.upstream,
                    "downstream": edge.downstream,
                    "required": edge.required,
                    "artifact": edge.artifact
                }
                for edge in self.edges
            ],
            "validated": self._validated
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'ExecutionGraph':
        """
        Deserialize graph from dictionary.
        
        Args:
            data: Dictionary representation of graph
            
        Returns:
            ExecutionGraph instance
            
        Raises:
            ValueError: If data is invalid
            KeyError: If required fields are missing
        """
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")
        
        graph = cls()
        
        # Reconstruct nodes
        nodes_data = data.get("nodes", [])
        if not isinstance(nodes_data, list):
            raise ValueError(f"Expected nodes to be list, got {type(nodes_data).__name__}")
        
        for idx, node_data in enumerate(nodes_data):
            if not isinstance(node_data, dict):
                raise ValueError(f"Node at index {idx} must be dict, got {type(node_data).__name__}")
            
            try:
                node = ExecutionNode(
                    node_id=node_data["node_id"],
                    name=node_data["name"],
                    owner=node_data["owner"],
                    phase=ExecutionPhase(node_data["phase"]),
                    inputs=frozenset(node_data.get("inputs", [])),
                    outputs=frozenset(node_data.get("outputs", [])),
                    side_effects=node_data.get("side_effects", False),
                    idempotent=node_data.get("idempotent", True),
                    deterministic=node_data.get("deterministic", True),
                    failure_tolerance=FailureType(node_data.get("failure_tolerance", "critical")),
                    retry_allowed=node_data.get("retry_allowed", False),
                    observability_tags=node_data.get("observability_tags", {})
                )
                graph.add_node(node)
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f"Invalid node data at index {idx}: {e}") from e
        
        # Reconstruct edges
        edges_data = data.get("edges", [])
        if not isinstance(edges_data, list):
            raise ValueError(f"Expected edges to be list, got {type(edges_data).__name__}")
        
        for idx, edge_data in enumerate(edges_data):
            if not isinstance(edge_data, dict):
                raise ValueError(f"Edge at index {idx} must be dict, got {type(edge_data).__name__}")
            
            try:
                edge = DependencyEdge(
                    upstream=edge_data["upstream"],
                    downstream=edge_data["downstream"],
                    required=edge_data.get("required", True),
                    artifact=edge_data.get("artifact")
                )
                graph.add_edge(edge)
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f"Invalid edge data at index {idx}: {e}") from e
        
        # Validate if marked as validated
        if data.get("validated", False):
            graph.validate()
        
        return graph
    
    def to_json(self, indent: int | None = 2) -> str:
        """
        Serialize graph to JSON string.
        
        Args:
            indent: JSON indentation (None for compact)
            
        Returns:
            JSON string representation
        """
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'ExecutionGraph':
        """
        Deserialize graph from JSON string.
        
        Args:
            json_str: JSON string representation
            
        Returns:
            ExecutionGraph instance
            
        Raises:
            ValueError: If JSON is invalid
            json.JSONDecodeError: If JSON parsing fails
        """
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}") from e
        
        return cls.from_dict(data)
    
    def create_execution_plan(self) -> ExecutionPlan:
        """
        Create deterministic execution plan.
        
        Caches plan for repeated access.
        
        Returns:
            ExecutionPlan ready for workflow_manager
            
        Raises:
            ValueError: If graph not validated
        """
        if not self._validated:
            raise ValueError("Graph must be validated before creating execution plan")
        
        if self._execution_plan is not None:
            return self._execution_plan
        
        # Get topological ordering (uses cache)
        ordered_nodes = self.topological_sort()
        
        # Build parallel execution groups
        parallel_groups = self._compute_parallel_groups(ordered_nodes)
        
        # Build cancellation map
        cancellation_map = self._failure_resolver.build_cancellation_map(
            self.nodes, self.edges
        )
        
        self._execution_plan = ExecutionPlan(
            ordered_nodes=ordered_nodes,
            parallel_groups=parallel_groups,
            cancellation_map=cancellation_map
        )
        
        return self._execution_plan
    
    def _compute_parallel_groups(self, ordered_nodes: list[str]) -> list[list[str]]:
        """
        Compute groups of nodes that can execute in parallel.
        
        Optimized algorithm: groups nodes that have identical dependency sets
        and no interdependencies. Uses cached dependency structures.
        
        Ensures deterministic group membership and stable ordering.
        """
        self._build_adjacency_structures()
        
        # Use cached dependencies for performance
        dependencies = self._dependencies
        
        groups = []
        processed = set()
        
        for node_id in ordered_nodes:
            if node_id in processed:
                continue
            
            # Find all nodes at same "level" (same dependency set)
            node_deps = dependencies.get(node_id, set())
            
            # Nodes can be in same group if they don't depend on each other
            group = [node_id]
            processed.add(node_id)
            
            for other_id in ordered_nodes:
                if other_id in processed:
                    continue
                
                other_deps = dependencies.get(other_id, set())
                
                # Can parallelize if:
                # 1. Neither depends on the other (no direct dependency)
                # 2. They have identical dependency sets (same level)
                # 3. No transitive dependencies exist
                if (node_id not in other_deps and 
                    other_id not in node_deps and
                    node_deps == other_deps):
                    # Verify no transitive dependency via adjacency check
                    if (other_id not in self._get_transitive_deps(node_id, dependencies) and
                        node_id not in self._get_transitive_deps(other_id, dependencies)):
                        group.append(other_id)
                        processed.add(other_id)
            
            # Sort for determinism
            if group:
                groups.append(sorted(group))
        
        return groups
    
    def _get_transitive_deps(
        self, 
        node_id: str, 
        dependencies: dict[str, set[str]]
    ) -> set[str]:
        """Get transitive dependencies (all ancestors)."""
        transitive = set()
        queue = deque([node_id])
        visited = {node_id}
        
        while queue:
            current = queue.popleft()
            deps = dependencies.get(current, set())
            
            for dep in deps:
                if dep not in visited:
                    visited.add(dep)
                    transitive.add(dep)
                    queue.append(dep)
        
        return transitive


# ============================================================================
# Graph Watchdog (Production Safety)
# ============================================================================

class GraphWatchdog:
    """
    Runtime graph enforcement.
    
    Watches for:
    - runtime graph divergence
    - node executed without graph membership
    - artifact produced outside declared outputs
    - execution order violations
    
    On violation:
    - halt workflow
    - snapshot state
    - escalate to safety systems
    """
    
    def __init__(self, graph: ExecutionGraph):
        if not graph._validated:
            raise ValueError("Cannot create watchdog for unvalidated graph")
        
        self.graph = graph
        self.executed_nodes: set[str] = set()
        self.execution_order: list[str] = []
        self.violations: list[dict[str, Any]] = []
    
    def before_execute(self, node_id: str) -> bool:
        """
        Check if node execution is legal.
        
        Args:
            node_id: Node about to execute
            
        Returns:
            True if execution allowed, False otherwise
        """
        # Check node exists in graph
        if node_id not in self.graph.nodes:
            self._record_violation(
                "unknown_node",
                f"Node {node_id} not in execution graph",
                node_id
            )
            return False
        
        # Check dependencies met
        ready_nodes = self.graph.get_ready_nodes(self.executed_nodes)
        if node_id not in ready_nodes:
            self._record_violation(
                "dependency_violation",
                f"Node {node_id} dependencies not met",
                node_id
            )
            return False
        
        return True
    
    def after_execute(self, node_id: str, produced_artifacts: set[str]) -> bool:
        """
        Validate execution completed correctly.
        
        Args:
            node_id: Node that executed
            produced_artifacts: Artifacts produced
            
        Returns:
            True if valid, False otherwise
        """
        if node_id not in self.graph.nodes:
            return False
        
        node = self.graph.nodes[node_id]
        
        # Check produced artifacts match declaration
        expected = node.outputs
        if produced_artifacts != expected:
            extra = produced_artifacts - expected
            missing = expected - produced_artifacts
            
            msg = f"Node {node_id} artifact mismatch."
            if extra:
                msg += f" Unexpected: {extra}."
            if missing:
                msg += f" Missing: {missing}."
            
            self._record_violation("artifact_mismatch", msg, node_id)
            return False
        
        # Record successful execution
        self.executed_nodes.add(node_id)
        self.execution_order.append(node_id)
        
        return True
    
    def _record_violation(self, violation_type: str, message: str, node_id: str):
        """Record a watchdog violation."""
        self.violations.append({
            "type": violation_type,
            "message": message,
            "node_id": node_id,
            "timestamp": datetime.utcnow().isoformat(),
            "executed_nodes": list(self.executed_nodes),
            "execution_order": list(self.execution_order)
        })
    
    def get_violations(self) -> list[dict[str, Any]]:
        """Get all recorded violations."""
        return list(self.violations)
    
    def has_violations(self) -> bool:
        """Check if any violations occurred."""
        return len(self.violations) > 0
    
    def reset(self) -> None:
        """Reset watchdog state (for reuse with same graph)."""
        self.executed_nodes.clear()
        self.execution_order.clear()
        self.violations.clear()
    
    def get_execution_summary(self) -> dict[str, Any]:
        """Get summary of execution state."""
        return {
            "total_nodes": len(self.graph.nodes),
            "executed_count": len(self.executed_nodes),
            "remaining_count": len(self.graph.nodes) - len(self.executed_nodes),
            "violations_count": len(self.violations),
            "execution_order": list(self.execution_order),
            "has_violations": self.has_violations()
        }


# ============================================================================
# Graph Builder Utilities
# ============================================================================

class ExecutionGraphBuilder:
    """
    Fluent builder for ExecutionGraph.
    
    Provides ergonomic API for graph construction while maintaining
    all validation guarantees.
    
    Example:
        graph = (ExecutionGraphBuilder()
            .node("ingest", "Ingest Data", "ingestion_module", ExecutionPhase.INGESTION,
                  outputs={"raw_data"})
            .node("extract", "Extract Features", "feature_module", ExecutionPhase.FEATURE_EXTRACTION,
                  inputs={"raw_data"}, outputs={"features"})
            .edge("ingest", "extract", artifact="raw_data")
            .build())
    """
    
    def __init__(self):
        self.graph = ExecutionGraph()
    
    def node(
        self,
        node_id: str,
        name: str,
        owner: str,
        phase: ExecutionPhase,
        inputs: set[str] | list[str] | None = None,
        outputs: set[str] | list[str] | None = None,
        side_effects: bool = False,
        idempotent: bool = True,
        deterministic: bool = True,
        failure_tolerance: FailureType = FailureType.CRITICAL,
        retry_allowed: bool = False,
        **tags
    ) -> 'ExecutionGraphBuilder':
        """
        Add node to graph.
        
        Args:
            node_id: Unique node identifier
            name: Human-readable node name
            owner: Module or agent owning this node
            phase: Execution phase
            inputs: Required input artifacts (set or list)
            outputs: Produced output artifacts (set or list)
            side_effects: Whether node has side effects
            idempotent: Whether node is idempotent
            deterministic: Whether node is deterministic
            failure_tolerance: Failure tolerance type
            retry_allowed: Whether node can be retried
            **tags: Observability tags (key-value pairs)
        
        Returns:
            Self for method chaining
        """
        # Convert inputs/outputs to sets if lists
        inputs_set = set(inputs) if inputs else set()
        outputs_set = set(outputs) if outputs else set()
        
        node = ExecutionNode(
            node_id=node_id,
            name=name,
            owner=owner,
            phase=phase,
            inputs=frozenset(inputs_set),
            outputs=frozenset(outputs_set),
            side_effects=side_effects,
            idempotent=idempotent,
            deterministic=deterministic,
            failure_tolerance=failure_tolerance,
            retry_allowed=retry_allowed,
            observability_tags=tags
        )
        self.graph.add_node(node)
        return self
    
    def edge(
        self,
        upstream: str,
        downstream: str,
        required: bool = True,
        artifact: str | None = None
    ) -> 'ExecutionGraphBuilder':
        """
        Add edge to graph.
        
        Args:
            upstream: Upstream node ID
            downstream: Downstream node ID
            required: Whether dependency is required
            artifact: Artifact flowing across edge (optional)
        
        Returns:
            Self for method chaining
        """
        edge = DependencyEdge(
            upstream=upstream,
            downstream=downstream,
            required=required,
            artifact=artifact
        )
        self.graph.add_edge(edge)
        return self
    
    def build(self, validate: bool = True) -> ExecutionGraph:
        """
        Validate and return graph.
        
        Args:
            validate: Whether to validate graph (default: True)
        
        Returns:
            ExecutionGraph (validated if validate=True)
            
        Raises:
            ValueError: If validation fails
        """
        if validate:
            self.graph.validate()
        return self.graph


# ============================================================================
# Export
# ============================================================================

__all__ = [
    'ExecutionPhase',
    'FailureType',
    'ExecutionNode',
    'DependencyEdge',
    'ExecutionPlan',
    'ExecutionGraph',
    'GraphWatchdog',
    'ExecutionGraphBuilder',
    'CycleDetector',
    'PhaseBoundaryEnforcer',
    'FailurePropagationResolver',
    'GraphValidator',
]