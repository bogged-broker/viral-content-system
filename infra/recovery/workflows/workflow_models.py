"""
/recovery/workflows/workflow_models.py

Canonical Immutable Workflow, DAG & Damage Contracts

PURPOSE:
    Defines the only legal representations of:
    - Workflow DAGs
    - Nodes, edges, artifacts
    - Detected damage
    - Repair scope boundaries

ABSOLUTE RULES (Non-Negotiable):
    - All models are immutable
    - No execution logic
    - No mutation methods
    - No lazy defaults
    - No environment references
    - No side effects

PRINCIPLE:
    These models describe state, not behavior.
    You cannot repair what you cannot see clearly.

GUARANTEE:
    If it's not defined here:
    - it cannot be validated
    - it cannot be repaired
    - it cannot be replayed
    - it cannot be audited
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Tuple


# ============================================================================
# WORKFLOW IDENTITY
# ============================================================================

@dataclass(frozen=True)
class WorkflowId:
    """
    Explicit identity wrapper (prevents accidental reuse).
    
    INVARIANT:
        Globally unique across recovery timelines
    """
    value: str
    
    def __post_init__(self):
        """Validate workflow ID"""
        if not self.value:
            raise ValueError("WorkflowId.value cannot be empty")
        
        if not isinstance(self.value, str):
            raise TypeError(f"WorkflowId.value must be str, got {type(self.value)}")
        
        # Enforce reasonable length
        if len(self.value) > 256:
            raise ValueError(f"WorkflowId.value too long: {len(self.value)} > 256")
    
    def __str__(self) -> str:
        return self.value
    
    def __repr__(self) -> str:
        return f"WorkflowId('{self.value}')"


# ============================================================================
# CORE WORKFLOW COMPONENTS
# ============================================================================

@dataclass(frozen=True)
class WorkflowNode:
    """
    One execution unit in the DAG.
    
    INVARIANTS:
        - node_id unique
        - No self-dependency
        - Determinism must be declared explicitly
    """
    node_id: str
    op_name: str
    inputs: FrozenSet[str]  # upstream node_ids
    outputs: FrozenSet[str]  # artifact IDs
    schema_version: str
    deterministic: bool
    
    def __post_init__(self):
        """Validate node invariants"""
        # Validate node_id
        if not self.node_id:
            raise ValueError("WorkflowNode.node_id cannot be empty")
        
        if not isinstance(self.node_id, str):
            raise TypeError(f"node_id must be str, got {type(self.node_id)}")
        
        # Validate op_name
        if not self.op_name:
            raise ValueError(f"Node {self.node_id}: op_name cannot be empty")
        
        # Validate inputs are frozenset
        if not isinstance(self.inputs, frozenset):
            raise TypeError(
                f"Node {self.node_id}: inputs must be frozenset, "
                f"got {type(self.inputs)}"
            )
        
        # Validate outputs are frozenset
        if not isinstance(self.outputs, frozenset):
            raise TypeError(
                f"Node {self.node_id}: outputs must be frozenset, "
                f"got {type(self.outputs)}"
            )
        
        # Check for self-dependency (node cannot depend on itself)
        if self.node_id in self.inputs:
            raise ValueError(
                f"Node {self.node_id}: cannot have self-dependency in inputs"
            )
        
        # Validate schema_version
        if not self.schema_version:
            raise ValueError(f"Node {self.node_id}: schema_version cannot be empty")
        
        # Validate determinism flag is explicit
        if not isinstance(self.deterministic, bool):
            raise TypeError(
                f"Node {self.node_id}: deterministic must be bool, "
                f"got {type(self.deterministic)}"
            )


@dataclass(frozen=True)
class WorkflowEdge:
    """
    Explicit dependency edge (never implicit).
    
    INVARIANTS:
        - from_node ≠ to_node
        - artifact_id required
        - Edge implies data dependency
    """
    from_node: str
    to_node: str
    artifact_id: str
    
    def __post_init__(self):
        """Validate edge invariants"""
        # Validate from_node
        if not self.from_node:
            raise ValueError("WorkflowEdge.from_node cannot be empty")
        
        # Validate to_node
        if not self.to_node:
            raise ValueError("WorkflowEdge.to_node cannot be empty")
        
        # Validate artifact_id
        if not self.artifact_id:
            raise ValueError("WorkflowEdge.artifact_id cannot be empty")
        
        # Validate no self-loop
        if self.from_node == self.to_node:
            raise ValueError(
                f"WorkflowEdge: from_node cannot equal to_node (got '{self.from_node}')"
            )
    
    def __repr__(self) -> str:
        return f"Edge({self.from_node} → {self.to_node} via {self.artifact_id})"


@dataclass(frozen=True)
class WorkflowArtifact:
    """
    Output produced by a node.
    
    INVARIANTS:
        - Artifact hashes are immutable
        - Producer must exist in DAG
    """
    artifact_id: str
    producer_node: str
    content_hash: str
    schema_version: str
    size_bytes: int
    
    def __post_init__(self):
        """Validate artifact invariants"""
        # Validate artifact_id
        if not self.artifact_id:
            raise ValueError("WorkflowArtifact.artifact_id cannot be empty")
        
        # Validate producer_node
        if not self.producer_node:
            raise ValueError(
                f"Artifact {self.artifact_id}: producer_node cannot be empty"
            )
        
        # Validate content_hash
        if not self.content_hash:
            raise ValueError(
                f"Artifact {self.artifact_id}: content_hash cannot be empty"
            )
        
        # Validate schema_version
        if not self.schema_version:
            raise ValueError(
                f"Artifact {self.artifact_id}: schema_version cannot be empty"
            )
        
        # Validate size_bytes
        if not isinstance(self.size_bytes, int):
            raise TypeError(
                f"Artifact {self.artifact_id}: size_bytes must be int, "
                f"got {type(self.size_bytes)}"
            )
        
        if self.size_bytes < 0:
            raise ValueError(
                f"Artifact {self.artifact_id}: size_bytes cannot be negative"
            )


@dataclass(frozen=True)
class WorkflowDAG:
    """
    Full immutable workflow graph snapshot.
    
    INVARIANTS:
        - DAG must be acyclic
        - All edge endpoints must exist
        - All artifacts trace to nodes
        - Deterministic node ordering
    """
    workflow_id: WorkflowId
    nodes: Tuple[WorkflowNode, ...]
    edges: Tuple[WorkflowEdge, ...]
    artifacts: Tuple[WorkflowArtifact, ...]
    created_at: int
    producer_version: str
    
    def __post_init__(self):
        """Validate DAG invariants"""
        # Validate workflow_id
        if not isinstance(self.workflow_id, WorkflowId):
            raise TypeError(
                f"workflow_id must be WorkflowId, got {type(self.workflow_id)}"
            )
        
        # Validate nodes is tuple
        if not isinstance(self.nodes, tuple):
            raise TypeError(f"nodes must be tuple, got {type(self.nodes)}")
        
        # Validate edges is tuple
        if not isinstance(self.edges, tuple):
            raise TypeError(f"edges must be tuple, got {type(self.edges)}")
        
        # Validate artifacts is tuple
        if not isinstance(self.artifacts, tuple):
            raise TypeError(f"artifacts must be tuple, got {type(self.artifacts)}")
        
        # Validate created_at
        if not isinstance(self.created_at, int):
            raise TypeError(
                f"created_at must be int, got {type(self.created_at)}"
            )
        
        if self.created_at < 0:
            raise ValueError("created_at cannot be negative")
        
        # Validate producer_version
        if not self.producer_version:
            raise ValueError("producer_version cannot be empty")
        
        # Build node_id set for validation
        node_ids = {node.node_id for node in self.nodes}
        
        # Validate node_id uniqueness
        if len(node_ids) != len(self.nodes):
            raise ValueError(
                f"WorkflowDAG {self.workflow_id}: duplicate node_ids detected"
            )
        
        # Validate all edge endpoints exist
        for edge in self.edges:
            if edge.from_node not in node_ids:
                raise ValueError(
                    f"WorkflowDAG {self.workflow_id}: edge from_node "
                    f"'{edge.from_node}' does not exist in nodes"
                )
            
            if edge.to_node not in node_ids:
                raise ValueError(
                    f"WorkflowDAG {self.workflow_id}: edge to_node "
                    f"'{edge.to_node}' does not exist in nodes"
                )
        
        # Validate all artifacts have existing producers
        for artifact in self.artifacts:
            if artifact.producer_node not in node_ids:
                raise ValueError(
                    f"WorkflowDAG {self.workflow_id}: artifact "
                    f"'{artifact.artifact_id}' producer '{artifact.producer_node}' "
                    f"does not exist in nodes"
                )
    
    def get_node(self, node_id: str) -> WorkflowNode | None:
        """Get node by ID (read-only access)"""
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        return None
    
    def get_artifact(self, artifact_id: str) -> WorkflowArtifact | None:
        """Get artifact by ID (read-only access)"""
        for artifact in self.artifacts:
            if artifact.artifact_id == artifact_id:
                return artifact
        return None
    
    def __repr__(self) -> str:
        return (
            f"WorkflowDAG({self.workflow_id}, "
            f"nodes={len(self.nodes)}, "
            f"edges={len(self.edges)}, "
            f"artifacts={len(self.artifacts)})"
        )


# ============================================================================
# DAMAGE & CORRUPTION MODELS
# ============================================================================

# DamageType - Explicit string constants (ENUM-LIKE)
DAMAGE_TYPE_NODE_CORRUPTION = "node_corruption"
DAMAGE_TYPE_EDGE_INVALID = "edge_invalid"
DAMAGE_TYPE_ARTIFACT_MISMATCH = "artifact_mismatch"
DAMAGE_TYPE_SCHEMA_DRIFT = "schema_drift"
DAMAGE_TYPE_MISSING_OUTPUT = "missing_output"
DAMAGE_TYPE_UNEXPECTED_OUTPUT = "unexpected_output"
DAMAGE_TYPE_NON_DETERMINISM = "non_determinism_detected"

# Valid damage types
VALID_DAMAGE_TYPES = frozenset([
    DAMAGE_TYPE_NODE_CORRUPTION,
    DAMAGE_TYPE_EDGE_INVALID,
    DAMAGE_TYPE_ARTIFACT_MISMATCH,
    DAMAGE_TYPE_SCHEMA_DRIFT,
    DAMAGE_TYPE_MISSING_OUTPUT,
    DAMAGE_TYPE_UNEXPECTED_OUTPUT,
    DAMAGE_TYPE_NON_DETERMINISM,
])


@dataclass(frozen=True)
class WorkflowDamage:
    """
    Atomic fact about damage.
    
    INVARIANTS:
        - Must reference existing workflow
        - Facts only — no speculation
    """
    workflow_id: WorkflowId
    damage_type: str
    affected_nodes: Tuple[str, ...]
    affected_edges: Tuple[Tuple[str, str], ...]
    detected_at: int
    detector_version: str
    severity: int  # bounded scale, e.g. 1-10
    
    def __post_init__(self):
        """Validate damage invariants"""
        # Validate workflow_id
        if not isinstance(self.workflow_id, WorkflowId):
            raise TypeError(
                f"workflow_id must be WorkflowId, got {type(self.workflow_id)}"
            )
        
        # Validate damage_type
        if self.damage_type not in VALID_DAMAGE_TYPES:
            raise ValueError(
                f"Invalid damage_type '{self.damage_type}'. "
                f"Must be one of {VALID_DAMAGE_TYPES}"
            )
        
        # Validate affected_nodes is tuple
        if not isinstance(self.affected_nodes, tuple):
            raise TypeError(
                f"affected_nodes must be tuple, got {type(self.affected_nodes)}"
            )
        
        # Validate affected_edges is tuple
        if not isinstance(self.affected_edges, tuple):
            raise TypeError(
                f"affected_edges must be tuple, got {type(self.affected_edges)}"
            )
        
        # Validate detected_at
        if not isinstance(self.detected_at, int):
            raise TypeError(
                f"detected_at must be int, got {type(self.detected_at)}"
            )
        
        if self.detected_at < 0:
            raise ValueError("detected_at cannot be negative")
        
        # Validate detector_version
        if not self.detector_version:
            raise ValueError("detector_version cannot be empty")
        
        # Validate severity is in bounded range
        if not isinstance(self.severity, int):
            raise TypeError(f"severity must be int, got {type(self.severity)}")
        
        if not 1 <= self.severity <= 10:
            raise ValueError(
                f"severity must be in range [1, 10], got {self.severity}"
            )


@dataclass(frozen=True)
class DamageAssessment:
    """
    Aggregated view of workflow damage.
    
    INVARIANTS:
        - If bounded == False, repair is forbidden
        - Deterministic ordering of damages
    """
    workflow_id: WorkflowId
    damages: Tuple[WorkflowDamage, ...]
    bounded: bool
    assessed_at: int
    
    def __post_init__(self):
        """Validate assessment invariants"""
        # Validate workflow_id
        if not isinstance(self.workflow_id, WorkflowId):
            raise TypeError(
                f"workflow_id must be WorkflowId, got {type(self.workflow_id)}"
            )
        
        # Validate damages is tuple
        if not isinstance(self.damages, tuple):
            raise TypeError(f"damages must be tuple, got {type(self.damages)}")
        
        # Validate all damages reference same workflow
        for damage in self.damages:
            if damage.workflow_id != self.workflow_id:
                raise ValueError(
                    f"Damage references workflow {damage.workflow_id} but "
                    f"assessment is for {self.workflow_id}"
                )
        
        # Validate bounded flag
        if not isinstance(self.bounded, bool):
            raise TypeError(f"bounded must be bool, got {type(self.bounded)}")
        
        # Validate assessed_at
        if not isinstance(self.assessed_at, int):
            raise TypeError(
                f"assessed_at must be int, got {type(self.assessed_at)}"
            )
        
        if self.assessed_at < 0:
            raise ValueError("assessed_at cannot be negative")
    
    def is_repairable(self) -> bool:
        """
        Check if damage is repairable.
        
        CRITICAL RULE:
            If bounded == False, repair is FORBIDDEN
        """
        return self.bounded
    
    def __repr__(self) -> str:
        return (
            f"DamageAssessment({self.workflow_id}, "
            f"damages={len(self.damages)}, "
            f"bounded={self.bounded})"
        )


# ============================================================================
# REPAIR BOUNDARY MODELS
# ============================================================================

# RepairScope - Explicit string values only
REPAIR_SCOPE_NODE = "node"
REPAIR_SCOPE_EDGE = "edge"
REPAIR_SCOPE_SUBGRAPH = "subgraph"

# Valid repair scopes
VALID_REPAIR_SCOPES = frozenset([
    REPAIR_SCOPE_NODE,
    REPAIR_SCOPE_EDGE,
    REPAIR_SCOPE_SUBGRAPH,
])


@dataclass(frozen=True)
class RepairPlanStep:
    """
    One atomic, reversible mutation step.
    
    INVARIANTS:
        - No duplicate order
        - Scope must match targets
    """
    step_id: str
    scope: str  # RepairScope
    targets: Tuple[str, ...]
    strategy_name: str
    order: int
    
    def __post_init__(self):
        """Validate step invariants"""
        # Validate step_id
        if not self.step_id:
            raise ValueError("RepairPlanStep.step_id cannot be empty")
        
        # Validate scope
        if self.scope not in VALID_REPAIR_SCOPES:
            raise ValueError(
                f"Invalid scope '{self.scope}'. "
                f"Must be one of {VALID_REPAIR_SCOPES}"
            )
        
        # Validate targets is tuple
        if not isinstance(self.targets, tuple):
            raise TypeError(f"targets must be tuple, got {type(self.targets)}")
        
        # Validate targets not empty
        if not self.targets:
            raise ValueError(f"Step {self.step_id}: targets cannot be empty")
        
        # Validate strategy_name
        if not self.strategy_name:
            raise ValueError(f"Step {self.step_id}: strategy_name cannot be empty")
        
        # Validate order
        if not isinstance(self.order, int):
            raise TypeError(
                f"Step {self.step_id}: order must be int, got {type(self.order)}"
            )
        
        if self.order < 0:
            raise ValueError(f"Step {self.step_id}: order cannot be negative")


@dataclass(frozen=True)
class RepairPlan:
    """
    Fully ordered repair proposal.
    
    INVARIANTS:
        - Steps are topologically sorted
        - Deterministic plan generation
    """
    workflow_id: WorkflowId
    steps: Tuple[RepairPlanStep, ...]
    plan_version: str
    generated_at: int
    
    def __post_init__(self):
        """Validate plan invariants"""
        # Validate workflow_id
        if not isinstance(self.workflow_id, WorkflowId):
            raise TypeError(
                f"workflow_id must be WorkflowId, got {type(self.workflow_id)}"
            )
        
        # Validate steps is tuple
        if not isinstance(self.steps, tuple):
            raise TypeError(f"steps must be tuple, got {type(self.steps)}")
        
        # Validate plan_version
        if not self.plan_version:
            raise ValueError("plan_version cannot be empty")
        
        # Validate generated_at
        if not isinstance(self.generated_at, int):
            raise TypeError(
                f"generated_at must be int, got {type(self.generated_at)}"
            )
        
        if self.generated_at < 0:
            raise ValueError("generated_at cannot be negative")
        
        # Validate step ordering (no duplicates)
        orders = [step.order for step in self.steps]
        if len(orders) != len(set(orders)):
            raise ValueError(
                f"RepairPlan {self.workflow_id}: duplicate step orders detected"
            )
        
        # Validate steps are sorted by order
        sorted_orders = sorted(orders)
        if orders != sorted_orders:
            raise ValueError(
                f"RepairPlan {self.workflow_id}: steps not sorted by order. "
                f"Expected {sorted_orders}, got {orders}"
            )
    
    def __repr__(self) -> str:
        return (
            f"RepairPlan({self.workflow_id}, "
            f"steps={len(self.steps)}, "
            f"version={self.plan_version})"
        )


# ============================================================================
# REPAIR RESULT CONTRACT
# ============================================================================

@dataclass(frozen=True)
class WorkflowRepairResult:
    """
    Immutable repair result.
    
    INVARIANTS:
        - repair_safe == False ⇒ no mutation committed
        - Immutable post-commit
    """
    workflow_id: WorkflowId
    repaired_nodes: Tuple[str, ...]
    repaired_edges: Tuple[Tuple[str, str], ...]
    strategy_used: str
    repair_safe: bool
    invariants_verified: bool
    completed_at: int
    
    def __post_init__(self):
        """Validate result invariants"""
        # Validate workflow_id
        if not isinstance(self.workflow_id, WorkflowId):
            raise TypeError(
                f"workflow_id must be WorkflowId, got {type(self.workflow_id)}"
            )
        
        # Validate repaired_nodes is tuple
        if not isinstance(self.repaired_nodes, tuple):
            raise TypeError(
                f"repaired_nodes must be tuple, got {type(self.repaired_nodes)}"
            )
        
        # Validate repaired_edges is tuple
        if not isinstance(self.repaired_edges, tuple):
            raise TypeError(
                f"repaired_edges must be tuple, got {type(self.repaired_edges)}"
            )
        
        # Validate strategy_used
        if not self.strategy_used:
            raise ValueError("strategy_used cannot be empty")
        
        # Validate repair_safe
        if not isinstance(self.repair_safe, bool):
            raise TypeError(
                f"repair_safe must be bool, got {type(self.repair_safe)}"
            )
        
        # Validate invariants_verified
        if not isinstance(self.invariants_verified, bool):
            raise TypeError(
                f"invariants_verified must be bool, "
                f"got {type(self.invariants_verified)}"
            )
        
        # CRITICAL INVARIANT: repair_safe == False ⇒ no mutation committed
        if not self.repair_safe and (self.repaired_nodes or self.repaired_edges):
            raise ValueError(
                "INVARIANT VIOLATION: repair_safe=False but mutations recorded. "
                f"repaired_nodes={self.repaired_nodes}, "
                f"repaired_edges={self.repaired_edges}"
            )
        
        # CRITICAL INVARIANT: repair_safe requires invariants_verified
        if self.repair_safe and not self.invariants_verified:
            raise ValueError(
                "INVARIANT VIOLATION: repair_safe=True requires "
                "invariants_verified=True"
            )
        
        # Validate completed_at
        if not isinstance(self.completed_at, int):
            raise TypeError(
                f"completed_at must be int, got {type(self.completed_at)}"
            )
        
        if self.completed_at < 0:
            raise ValueError("completed_at cannot be negative")
    
    def __repr__(self) -> str:
        return (
            f"WorkflowRepairResult({self.workflow_id}, "
            f"safe={self.repair_safe}, "
            f"nodes={len(self.repaired_nodes)}, "
            f"edges={len(self.repaired_edges)})"
        )


# ============================================================================
# MODULE EXPORTS (Public API)
# ============================================================================

__all__ = [
    # Core Workflow Models
    'WorkflowId',
    'WorkflowNode',
    'WorkflowEdge',
    'WorkflowArtifact',
    'WorkflowDAG',
    
    # Damage Models
    'WorkflowDamage',
    'DamageAssessment',
    
    # Damage Type Constants
    'DAMAGE_TYPE_NODE_CORRUPTION',
    'DAMAGE_TYPE_EDGE_INVALID',
    'DAMAGE_TYPE_ARTIFACT_MISMATCH',
    'DAMAGE_TYPE_SCHEMA_DRIFT',
    'DAMAGE_TYPE_MISSING_OUTPUT',
    'DAMAGE_TYPE_UNEXPECTED_OUTPUT',
    'DAMAGE_TYPE_NON_DETERMINISM',
    'VALID_DAMAGE_TYPES',
    
    # Repair Models
    'RepairPlan',
    'RepairPlanStep',
    'WorkflowRepairResult',
    
    # Repair Scope Constants
    'REPAIR_SCOPE_NODE',
    'REPAIR_SCOPE_EDGE',
    'REPAIR_SCOPE_SUBGRAPH',
    'VALID_REPAIR_SCOPES',
]


# ============================================================================
# FORBIDDEN IN THIS FILE
# ============================================================================

# 🚫 Graph traversal - belongs in workflow_validator.py or workflow_replay.py
# 🚫 Validation logic - belongs in workflow_validator.py
# 🚫 Repair logic - belongs in workflow_repair.py
# 🚫 IO operations - belongs in persistence layer
# 🚫 Serialization - belongs in transport layer
# 🚫 Dynamic inference - models are facts, not guesses

# THIS FILE DEFINES FACTS, NOT ACTIONS


if __name__ == '__main__':
    # Demonstration of immutable workflow models
    print("Workflow Models - Canonical Immutable Contracts")
    print("=" * 70)
    print()
    print("ABSOLUTE RULES:")
    print("  ✓ All models are immutable")
    print("  ✓ No execution logic")
    print("  ✓ No mutation methods")
    print("  ✓ No lazy defaults")
    print("  ✓ No environment references")
    print("  ✓ No side effects")
    print()
    print("PRINCIPLE:")
    print("  These models describe state, not behavior.")
    print()
    print("GUARANTEE:")
    print("  If it's not defined here, it cannot be:")
    print("    - validated")
    print("    - repaired")
    print("    - replayed")
    print("    - audited")
    print()
    print("This prevents interpretation drift across recovery layers.")