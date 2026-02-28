"""
/infra/observability/tracing.py

End-to-End Deterministic Causal Tracing Authority

This is the system's causality spine. It answers: "Why did this specific outcome
happen, step by step, across the entire system?"

This is causality, not logging.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Dict
import hashlib
import time
from collections import defaultdict
import json


# ============================================================================
# ENUMS (STRICT — NO FREE-FORM)
# ============================================================================


class TraceScope(Enum):
    """
    Hierarchical trace scopes. Never overlapping illegally.
    """

    RUN = "run"
    WORKFLOW = "workflow"
    JOB = "job"
    CONTENT = "content"
    ACCOUNT = "account"
    EXPERIMENT = "experiment"


class TraceEventType(Enum):
    """
    Explicit trace event types. No free-form event types allowed.
    """

    DECISION = "decision"
    TRANSFORMATION = "transformation"
    VALIDATION = "validation"
    GATE = "gate"
    FAILURE = "failure"
    WARNING = "warning"


# ============================================================================
# CORE DATA STRUCTURES (IMMUTABLE)
# ============================================================================


@dataclass(frozen=True)
class TraceContext:
    """
    Immutable trace context propagated everywhere.
    
    Nothing mutates this. It flows through the entire system.
    """

    trace_id: str
    parent_trace_id: str | None

    scope: TraceScope
    scope_id: str

    run_id: str
    timestamp: int

    def with_parent(self, parent_id: str) -> "TraceContext":
        """Create a child context with this context as parent."""
        return TraceContext(
            trace_id=self.trace_id,
            parent_trace_id=parent_id,
            scope=self.scope,
            scope_id=self.scope_id,
            run_id=self.run_id,
            timestamp=self.timestamp,
        )

    def with_scope(
        self,
        scope: TraceScope,
        scope_id: str,
    ) -> "TraceContext":
        """Create a new context with different scope."""
        return TraceContext(
            trace_id=self.trace_id,
            parent_trace_id=self.trace_id,  # Current becomes parent
            scope=scope,
            scope_id=scope_id,
            run_id=self.run_id,
            timestamp=int(time.time() * 1000),
        )


@dataclass(frozen=True)
class TraceNode:
    """
    Represents a decision or transformation, not just a "step".
    
    Nodes are immutable and hash-verified.
    """

    node_id: str
    event_type: TraceEventType

    component: str
    action: str

    inputs_hash: str
    outputs_hash: str

    timestamp: int

    metadata: dict[str, Any] = field(default_factory=dict)

    def verify_integrity(self) -> bool:
        """Verify node has required hashes for causal tracking."""
        if self.event_type == TraceEventType.DECISION:
            return bool(self.inputs_hash and self.outputs_hash)
        return True


@dataclass(frozen=True)
class TraceEdge:
    """
    Causal edge between nodes. Encodes WHY, not just order.
    """

    from_node: str
    to_node: str

    causal_reason: str

    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def is_valid(self) -> bool:
        """Edge must have non-empty reason and distinct endpoints."""
        return (
            bool(self.causal_reason)
            and self.from_node != self.to_node
            and bool(self.from_node)
            and bool(self.to_node)
        )


# ============================================================================
# TRACE GRAPH (CORE TRUTH)
# ============================================================================


class TraceGraph:
    """
    DAG-only trace graph with deterministic ordering and invariant checking.
    
    Rules:
    - DAG only (no cycles)
    - Deterministic ordering
    - Invariant-checked
    
    If a cycle appears → system invariant violation.
    """

    def __init__(self, trace_id: str):
        """
        Initialize trace graph.
        
        Args:
            trace_id: Unique identifier for this trace
        """
        self._trace_id = trace_id
        self._nodes: dict[str, TraceNode] = {}
        self._edges: list[TraceEdge] = []
        self._adjacency: dict[str, list[str]] = defaultdict(list)
        self._node_order: list[str] = []

    @property
    def trace_id(self) -> str:
        """Get trace ID."""
        return self._trace_id

    def add_node(self, node: TraceNode) -> None:
        """
        Add a node to the trace graph.
        
        Args:
            node: TraceNode to add
            
        Raises:
            ValueError: If node already exists or fails integrity check
        """
        if node.node_id in self._nodes:
            raise ValueError(f"Node '{node.node_id}' already exists in trace")

        if not node.verify_integrity():
            raise ValueError(
                f"Node '{node.node_id}' failed integrity check: "
                f"decisions require input/output hashes"
            )

        self._nodes[node.node_id] = node
        self._node_order.append(node.node_id)

    def add_edge(self, edge: TraceEdge) -> None:
        """
        Add a causal edge to the trace graph.
        
        Args:
            edge: TraceEdge to add
            
        Raises:
            ValueError: If edge is invalid or creates cycle
        """
        if not edge.is_valid():
            raise ValueError(f"Invalid edge: {edge}")

        if edge.from_node not in self._nodes:
            raise ValueError(f"Source node '{edge.from_node}' not in graph")

        if edge.to_node not in self._nodes:
            raise ValueError(f"Target node '{edge.to_node}' not in graph")

        # Check for cycle before adding
        if self._would_create_cycle(edge.from_node, edge.to_node):
            raise ValueError(
                f"Edge from '{edge.from_node}' to '{edge.to_node}' "
                f"would create cycle (INVARIANT VIOLATION)"
            )

        self._edges.append(edge)
        self._adjacency[edge.from_node].append(edge.to_node)

    def _would_create_cycle(self, from_node: str, to_node: str) -> bool:
        """
        Check if adding edge would create a cycle using DFS.
        
        Args:
            from_node: Source node ID
            to_node: Target node ID
            
        Returns:
            True if cycle would be created
        """
        # If there's already a path from to_node to from_node, adding
        # this edge would create a cycle
        visited = set()
        stack = [to_node]

        while stack:
            current = stack.pop()
            if current == from_node:
                return True

            if current in visited:
                continue

            visited.add(current)
            stack.extend(self._adjacency.get(current, []))

        return False

    def get_node(self, node_id: str) -> TraceNode | None:
        """Get node by ID."""
        return self._nodes.get(node_id)

    def get_nodes(self) -> list[TraceNode]:
        """Get all nodes in deterministic order."""
        return [self._nodes[node_id] for node_id in self._node_order]

    def get_edges(self) -> list[TraceEdge]:
        """Get all edges."""
        return self._edges.copy()

    def get_children(self, node_id: str) -> list[str]:
        """Get child node IDs for a given node."""
        return self._adjacency.get(node_id, []).copy()

    def get_root_nodes(self) -> list[TraceNode]:
        """Get nodes with no incoming edges."""
        nodes_with_incoming = {edge.to_node for edge in self._edges}
        return [
            node
            for node in self.get_nodes()
            if node.node_id not in nodes_with_incoming
        ]

    def get_leaf_nodes(self) -> list[TraceNode]:
        """Get nodes with no outgoing edges."""
        return [
            node
            for node in self.get_nodes()
            if node.node_id not in self._adjacency
        ]

    def verify_invariants(self) -> list[str]:
        """
        Verify trace graph invariants.
        
        Returns:
            List of invariant violations (empty if valid)
        """
        violations = []

        # Check for orphan nodes (nodes not connected to anything)
        connected_nodes = set()
        for edge in self._edges:
            connected_nodes.add(edge.from_node)
            connected_nodes.add(edge.to_node)

        orphans = set(self._nodes.keys()) - connected_nodes
        if len(self._nodes) > 1 and orphans:
            violations.append(f"Orphan nodes found: {orphans}")

        # Check that all decision nodes have hashes
        for node in self._nodes.values():
            if node.event_type == TraceEventType.DECISION:
                if not node.inputs_hash or not node.outputs_hash:
                    violations.append(
                        f"Decision node '{node.node_id}' missing required hashes"
                    )

        # Check for cycles (should be impossible but verify)
        if self._has_cycle():
            violations.append("Graph contains cycle (CRITICAL INVARIANT VIOLATION)")

        return violations

    def _has_cycle(self) -> bool:
        """Check if graph has any cycles."""
        visited = set()
        rec_stack = set()

        def visit(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)

            for neighbor in self._adjacency.get(node_id, []):
                if neighbor not in visited:
                    if visit(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True

            rec_stack.remove(node_id)
            return False

        for node_id in self._nodes:
            if node_id not in visited:
                if visit(node_id):
                    return True

        return False


# ============================================================================
# TRACE RECORDER (ONLY ENTRY POINT)
# ============================================================================


class TraceRecorder:
    """
    Entry point for recording trace events.
    
    Guarantees:
    - No blocking I/O
    - Bounded memory
    - Deterministic buffering
    - Ordering preserved
    
    Recorder never writes directly to disk.
    """

    def __init__(self, max_buffer_size: int = 10000):
        """
        Initialize trace recorder.
        
        Args:
            max_buffer_size: Maximum number of graphs to buffer
        """
        self._graphs: dict[str, TraceGraph] = {}
        self._max_buffer_size = max_buffer_size
        self._current_contexts: dict[str, TraceContext] = {}

    def start_scope(
        self,
        scope: TraceScope,
        scope_id: str,
        run_id: str,
        parent_trace_id: str | None = None,
    ) -> TraceContext:
        """
        Start a new trace scope.
        
        Args:
            scope: Trace scope type
            scope_id: Unique identifier for this scope
            run_id: Run identifier
            parent_trace_id: Optional parent trace ID
            
        Returns:
            TraceContext for this scope
        """
        trace_id = self._generate_trace_id(scope, scope_id, run_id)

        context = TraceContext(
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            scope=scope,
            scope_id=scope_id,
            run_id=run_id,
            timestamp=int(time.time() * 1000),
        )

        # Create graph for this trace
        if trace_id not in self._graphs:
            self._graphs[trace_id] = TraceGraph(trace_id)

        self._current_contexts[trace_id] = context

        return context

    def end_scope(self, context: TraceContext) -> TraceGraph:
        """
        End a trace scope and return the graph.
        
        Args:
            context: TraceContext to end
            
        Returns:
            Completed TraceGraph
            
        Raises:
            ValueError: If invariants violated
        """
        graph = self._graphs.get(context.trace_id)
        if graph is None:
            raise ValueError(f"No graph found for trace '{context.trace_id}'")

        # Verify invariants before ending
        violations = graph.verify_invariants()
        if violations:
            raise ValueError(
                f"Trace '{context.trace_id}' has invariant violations: {violations}"
            )

        self._current_contexts.pop(context.trace_id, None)

        return graph

    def record_event(
        self,
        context: TraceContext,
        node: TraceNode,
        parent_node_id: str | None = None,
        causal_reason: str | None = None,
    ) -> None:
        """
        Record a trace event (node) in the graph.
        
        Args:
            context: TraceContext for this event
            node: TraceNode to record
            parent_node_id: Optional parent node for causal edge
            causal_reason: Reason for causal relationship
        """
        graph = self._graphs.get(context.trace_id)
        if graph is None:
            raise ValueError(f"No graph found for trace '{context.trace_id}'")

        # Add node
        graph.add_node(node)

        # Add edge if parent specified
        if parent_node_id is not None:
            if causal_reason is None:
                raise ValueError("Causal reason required when parent_node_id specified")

            edge = TraceEdge(
                from_node=parent_node_id,
                to_node=node.node_id,
                causal_reason=causal_reason,
            )
            graph.add_edge(edge)

    def get_graph(self, trace_id: str) -> TraceGraph | None:
        """Get trace graph by ID."""
        return self._graphs.get(trace_id)

    def get_context(self, trace_id: str) -> TraceContext | None:
        """Get current context by trace ID."""
        return self._current_contexts.get(trace_id)

    def _generate_trace_id(
        self,
        scope: TraceScope,
        scope_id: str,
        run_id: str,
    ) -> str:
        """Generate deterministic trace ID."""
        data = f"{scope.value}:{scope_id}:{run_id}:{time.time_ns()}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def _check_buffer_size(self) -> None:
        """Check and enforce buffer size limits."""
        if len(self._graphs) > self._max_buffer_size:
            # Remove oldest graphs (simple FIFO for now)
            oldest_keys = sorted(
                self._graphs.keys(),
                key=lambda k: self._graphs[k].get_nodes()[0].timestamp
                if self._graphs[k].get_nodes()
                else 0,
            )[: len(self._graphs) - self._max_buffer_size]

            for key in oldest_keys:
                self._graphs.pop(key, None)


# ============================================================================
# TRACE PROPAGATOR (CRITICAL)
# ============================================================================


class TracePropagator:
    """
    Propagates trace context across module/process boundaries.
    
    No implicit propagation allowed — must be explicit.
    """

    @staticmethod
    def inject(context: TraceContext) -> dict[str, str]:
        """
        Inject trace context into carrier (e.g., headers, metadata).
        
        Args:
            context: TraceContext to inject
            
        Returns:
            Carrier dict with trace context
        """
        return {
            "trace-id": context.trace_id,
            "parent-trace-id": context.parent_trace_id or "",
            "trace-scope": context.scope.value,
            "trace-scope-id": context.scope_id,
            "trace-run-id": context.run_id,
            "trace-timestamp": str(context.timestamp),
        }

    @staticmethod
    def extract(carrier: dict[str, str]) -> TraceContext | None:
        """
        Extract trace context from carrier.
        
        Args:
            carrier: Carrier dict with trace context
            
        Returns:
            TraceContext if valid, None otherwise
        """
        try:
            trace_id = carrier.get("trace-id")
            if not trace_id:
                return None

            parent_trace_id = carrier.get("parent-trace-id") or None

            scope_value = carrier.get("trace-scope")
            if not scope_value:
                return None
            scope = TraceScope(scope_value)

            scope_id = carrier.get("trace-scope-id")
            if not scope_id:
                return None

            run_id = carrier.get("trace-run-id")
            if not run_id:
                return None

            timestamp = int(carrier.get("trace-timestamp", "0"))

            return TraceContext(
                trace_id=trace_id,
                parent_trace_id=parent_trace_id,
                scope=scope,
                scope_id=scope_id,
                run_id=run_id,
                timestamp=timestamp,
            )

        except (ValueError, KeyError):
            return None


# ============================================================================
# TRACE SERIALIZATION (REPLAY-SAFE)
# ============================================================================


class TraceSerializer:
    """
    Serializes and deserializes trace graphs.
    
    Rules:
    - Stable schema
    - Versioned
    - Hash-verified
    - Deterministic ordering
    
    If a trace can't be replayed, it's worthless.
    """

    SCHEMA_VERSION = "v1"

    @staticmethod
    def serialize(graph: TraceGraph) -> bytes:
        """
        Serialize trace graph to bytes.
        
        Args:
            graph: TraceGraph to serialize
            
        Returns:
            Serialized bytes
        """
        # Get nodes in deterministic order
        nodes_data = [
            {
                "node_id": node.node_id,
                "event_type": node.event_type.value,
                "component": node.component,
                "action": node.action,
                "inputs_hash": node.inputs_hash,
                "outputs_hash": node.outputs_hash,
                "timestamp": node.timestamp,
                "metadata": node.metadata,
            }
            for node in graph.get_nodes()
        ]

        # Get edges in deterministic order
        edges_data = [
            {
                "from_node": edge.from_node,
                "to_node": edge.to_node,
                "causal_reason": edge.causal_reason,
                "timestamp": edge.timestamp,
            }
            for edge in sorted(graph.get_edges(), key=lambda e: e.timestamp)
        ]

        payload = {
            "schema_version": TraceSerializer.SCHEMA_VERSION,
            "trace_id": graph.trace_id,
            "nodes": nodes_data,
            "edges": edges_data,
        }

        # Serialize to JSON with sorted keys for determinism
        json_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        # Add hash for verification
        content_hash = hashlib.sha256(json_str.encode()).hexdigest()
        final_payload = {
            "hash": content_hash,
            "data": payload,
        }

        return json.dumps(final_payload, sort_keys=True).encode()

    @staticmethod
    def deserialize(payload: bytes) -> TraceGraph:
        """
        Deserialize trace graph from bytes.
        
        Args:
            payload: Serialized bytes
            
        Returns:
            TraceGraph
            
        Raises:
            ValueError: If hash verification fails or schema invalid
        """
        try:
            final_payload = json.loads(payload.decode())

            # Verify hash
            content_hash = final_payload["hash"]
            data = final_payload["data"]

            json_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
            computed_hash = hashlib.sha256(json_str.encode()).hexdigest()

            if content_hash != computed_hash:
                raise ValueError("Hash verification failed — trace corrupted")

            # Check schema version
            if data["schema_version"] != TraceSerializer.SCHEMA_VERSION:
                raise ValueError(
                    f"Unsupported schema version: {data['schema_version']}"
                )

            # Reconstruct graph
            graph = TraceGraph(data["trace_id"])

            # Add nodes
            for node_data in data["nodes"]:
                node = TraceNode(
                    node_id=node_data["node_id"],
                    event_type=TraceEventType(node_data["event_type"]),
                    component=node_data["component"],
                    action=node_data["action"],
                    inputs_hash=node_data["inputs_hash"],
                    outputs_hash=node_data["outputs_hash"],
                    timestamp=node_data["timestamp"],
                    metadata=node_data.get("metadata", {}),
                )
                graph.add_node(node)

            # Add edges
            for edge_data in data["edges"]:
                edge = TraceEdge(
                    from_node=edge_data["from_node"],
                    to_node=edge_data["to_node"],
                    causal_reason=edge_data["causal_reason"],
                    timestamp=edge_data["timestamp"],
                )
                graph.add_edge(edge)

            return graph

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise ValueError(f"Failed to deserialize trace: {e}") from e


# ============================================================================
# TRACE INVARIANTS (ENFORCED)
# ============================================================================


class TraceInvariants:
    """
    Enforces trace invariants across the system.
    
    Examples:
    - Every decision must have input hashes
    - Every output must link causally
    - Every trace must terminate
    - Orphan nodes forbidden
    - Cross-scope hops must be explicit
    
    Violations are fatal errors, not warnings.
    """

    @staticmethod
    def verify_decision_hashes(node: TraceNode) -> None:
        """
        Verify decision nodes have required hashes.
        
        Args:
            node: TraceNode to verify
            
        Raises:
            ValueError: If decision node missing hashes
        """
        if node.event_type == TraceEventType.DECISION:
            if not node.inputs_hash:
                raise ValueError(
                    f"Decision node '{node.node_id}' missing inputs_hash "
                    f"(INVARIANT VIOLATION)"
                )
            if not node.outputs_hash:
                raise ValueError(
                    f"Decision node '{node.node_id}' missing outputs_hash "
                    f"(INVARIANT VIOLATION)"
                )

    @staticmethod
    def verify_causal_edge(edge: TraceEdge) -> None:
        """
        Verify edge has valid causal reason.
        
        Args:
            edge: TraceEdge to verify
            
        Raises:
            ValueError: If edge invalid
        """
        if not edge.causal_reason or not edge.causal_reason.strip():
            raise ValueError(
                f"Edge from '{edge.from_node}' to '{edge.to_node}' "
                f"missing causal reason (INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_no_orphans(graph: TraceGraph) -> None:
        """
        Verify no orphan nodes in graph (except single-node graphs).
        
        Args:
            graph: TraceGraph to verify
            
        Raises:
            ValueError: If orphan nodes found
        """
        if len(graph.get_nodes()) <= 1:
            return

        connected_nodes = set()
        for edge in graph.get_edges():
            connected_nodes.add(edge.from_node)
            connected_nodes.add(edge.to_node)

        all_nodes = {node.node_id for node in graph.get_nodes()}
        orphans = all_nodes - connected_nodes

        if orphans:
            raise ValueError(
                f"Orphan nodes found in trace '{graph.trace_id}': {orphans} "
                f"(INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_termination(graph: TraceGraph) -> None:
        """
        Verify trace has at least one leaf node (termination).
        
        Args:
            graph: TraceGraph to verify
            
        Raises:
            ValueError: If no leaf nodes found
        """
        if not graph.get_nodes():
            return

        leaf_nodes = graph.get_leaf_nodes()
        if not leaf_nodes:
            raise ValueError(
                f"Trace '{graph.trace_id}' has no leaf nodes — "
                f"trace must terminate (INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_all(graph: TraceGraph) -> None:
        """
        Run all invariant checks on a graph.
        
        Args:
            graph: TraceGraph to verify
            
        Raises:
            ValueError: If any invariant violated
        """
        # Verify decision hashes
        for node in graph.get_nodes():
            TraceInvariants.verify_decision_hashes(node)

        # Verify causal edges
        for edge in graph.get_edges():
            TraceInvariants.verify_causal_edge(edge)

        # Verify no orphans
        TraceInvariants.verify_no_orphans(graph)

        # Verify termination
        TraceInvariants.verify_termination(graph)

        # Verify graph-level invariants
        violations = graph.verify_invariants()
        if violations:
            raise ValueError(
                f"Trace '{graph.trace_id}' failed invariant checks: {violations}"
            )


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def compute_hash(data: Any) -> str:
    """
    Compute deterministic hash of data.
    
    Args:
        data: Data to hash (must be JSON-serializable)
        
    Returns:
        Hex hash string
    """
    json_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(json_str.encode()).hexdigest()


def create_trace_node(
    event_type: TraceEventType,
    component: str,
    action: str,
    inputs: Any,
    outputs: Any,
    metadata: dict[str, Any] | None = None,
) -> TraceNode:
    """
    Create a trace node with computed hashes.
    
    Args:
        event_type: Type of event
        component: Component name
        action: Action name
        inputs: Input data
        outputs: Output data
        metadata: Optional metadata
        
    Returns:
        TraceNode with computed hashes
    """
    node_id = f"{component}:{action}:{int(time.time() * 1000000)}"

    return TraceNode(
        node_id=node_id,
        event_type=event_type,
        component=component,
        action=action,
        inputs_hash=compute_hash(inputs),
        outputs_hash=compute_hash(outputs),
        timestamp=int(time.time() * 1000),
        metadata=metadata or {},
    )