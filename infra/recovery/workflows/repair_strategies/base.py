"""
Base types and contracts for repair strategies.

This module defines the fundamental types used across all repair strategies.
Every repair strategy must operate within these contracts.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Any, Literal, Tuple, Dict
from datetime import datetime


class RepairRisk(Enum):
    """Risk level classification for repair operations."""
    SAFE = "safe"              # No data loss, fully reversible
    LOW = "low"                # Minimal risk, well-tested path
    MEDIUM = "medium"          # Some risk, requires validation
    HIGH = "high"              # Significant risk, needs approval
    CRITICAL = "critical"      # Dangerous, requires manual review


class NodeRepairType(Enum):
    """Types of node-level repairs."""
    RECOMPUTE = "recompute"                      # Full node re-execution
    PARAMETER_PATCH = "parameter_patch"          # Fix node parameters
    CACHE_INVALIDATION = "cache_invalidation"    # Clear corrupted cache
    OUTPUT_REGENERATION = "output_regeneration"  # Regenerate outputs
    SCHEMA_ALIGNMENT = "schema_alignment"        # Fix schema mismatches


class DeterminismLevel(Enum):
    """Determinism guarantees for repair operations."""
    GUARANTEED = "guaranteed"        # Always produces same output
    SEEDED = "seeded"               # Deterministic with seed injection
    BOUNDED = "bounded"             # Deterministic within constraints
    NON_DETERMINISTIC = "non_deterministic"  # Cannot guarantee determinism


class DamageType(Enum):
    """Classification of node damage."""
    CORRUPT_OUTPUT = "corrupt_output"
    INVALID_PARAMETERS = "invalid_parameters"
    CACHE_POISONING = "cache_poisoning"
    SCHEMA_MISMATCH = "schema_mismatch"
    NON_DETERMINISTIC_EXECUTION = "non_deterministic_execution"
    HASH_MISMATCH = "hash_mismatch"
    MISSING_ARTIFACT = "missing_artifact"


@dataclass(frozen=True)
class WorkflowNode:
    """Immutable representation of a workflow node."""
    node_id: str
    node_type: str
    parameters: dict[str, Any]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    
    # Execution metadata
    is_deterministic: bool
    is_replayable: bool
    has_side_effects: bool
    is_repairable: bool
    
    # Versioning
    schema_version: str
    execution_hash: Optional[str] = None
    
    # Constraints
    requires_seed: bool = False
    requires_time_abstraction: bool = False
    
    def __post_init__(self):
        """Validate node invariants."""
        if not self.node_id:
            raise ValueError("node_id cannot be empty")
        if self.has_side_effects and self.is_replayable:
            raise ValueError("Nodes with side effects cannot be replayable")


@dataclass(frozen=True)
class NodeDamageReport:
    """Immutable damage assessment for a node."""
    node_id: str
    damage_type: DamageType
    severity: RepairRisk
    
    # Evidence
    corrupted_outputs: tuple[str, ...]
    invalid_parameters: tuple[str, ...]
    hash_mismatches: dict[str, tuple[str, str]]  # artifact -> (expected, actual)
    
    # Context
    detected_at: datetime
    detection_method: str
    is_ambiguous: bool
    
    # Constraints
    blast_radius: int  # Number of downstream nodes affected
    can_isolate: bool  # Can repair be isolated to this node


@dataclass(frozen=True)
class RepairConstraints:
    """Constraints that govern repair operations."""
    allow_recomputation: bool
    require_determinism: bool
    max_blast_radius: int
    allow_parameter_modification: bool
    allow_cache_invalidation: bool
    
    # Replay requirements
    require_seed_injection: bool
    require_time_abstraction: bool
    
    # Safety
    require_justification: bool
    require_risk_approval: dict[RepairRisk, bool]
    
    # Limits
    max_repair_attempts: int = 3
    timeout_seconds: int = 300


@dataclass(frozen=True)
class WorkflowDAG:
    """Immutable snapshot of workflow structure."""
    workflow_id: str
    nodes: dict[str, WorkflowNode]
    edges: tuple[tuple[str, str], ...]  # (from_node, to_node)
    
    # Metadata
    snapshot_time: datetime
    workflow_version: str
    
    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        """Safely retrieve a node."""
        return self.nodes.get(node_id)
    
    def get_inputs(self, node_id: str) -> tuple[str, ...]:
        """Get direct input nodes."""
        return tuple(src for src, dst in self.edges if dst == node_id)
    
    def get_outputs(self, node_id: str) -> tuple[str, ...]:
        """Get direct output nodes."""
        return tuple(dst for src, dst in self.edges if src == node_id)


@dataclass(frozen=True)
class NodeRepairContext:
    """Complete context for node repair decision."""
    node: WorkflowNode
    damage_report: NodeDamageReport
    workflow_snapshot: WorkflowDAG
    repair_constraints: RepairConstraints
    
    # Additional context
    previous_repair_attempts: int = 0
    related_failures: tuple[str, ...] = field(default_factory=tuple)
    
    def __post_init__(self):
        """Validate context consistency."""
        if self.node.node_id != self.damage_report.node_id:
            raise ValueError("Node ID mismatch between node and damage report")
        
        if self.node.node_id not in self.workflow_snapshot.nodes:
            raise ValueError(f"Node {self.node.node_id} not found in workflow snapshot")


@dataclass(frozen=True)
class RecomputeSpec:
    """Specification for node recomputation."""
    node_id: str
    preserve_parameters: bool
    inject_seed: Optional[int]
    abstract_time: bool
    
    # Scope
    recompute_inputs: tuple[str, ...]
    invalidate_outputs: tuple[str, ...]
    
    # Validation
    expected_determinism: DeterminismLevel
    validation_hashes: dict[str, str]  # artifact -> expected_hash


@dataclass(frozen=True)
class NodeRepairAction:
    """Declarative repair action output (IMMUTABLE)."""
    node_id: str
    repair_type: NodeRepairType
    
    # What changes
    updated_node: WorkflowNode
    affected_artifacts: tuple[str, ...]
    
    # Recomputation requirements
    recompute_required: bool
    recompute_spec: Optional[RecomputeSpec]
    
    # Determinism guarantees
    determinism_level: DeterminismLevel
    determinism_required: bool
    
    # Safety metadata
    justification: str
    risk_level: RepairRisk
    
    # Replay requirements
    replay_requirements: dict[str, Any] = field(default_factory=dict)
    
    # Reversibility
    rollback_checkpoint: Optional[str] = None
    is_reversible: bool = True
    
    # Proof
    proof_metadata: dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate repair action invariants."""
        if self.recompute_required and self.recompute_spec is None:
            raise ValueError("recompute_spec required when recompute_required=True")
        
        if self.determinism_required and self.determinism_level == DeterminismLevel.NON_DETERMINISTIC:
            raise ValueError("Cannot require determinism for non-deterministic repair")
        
        if not self.justification:
            raise ValueError("Justification is required for all repairs")
        
        if not self.is_reversible:
            raise ValueError("All repairs must be reversible (Rule 4)")


@dataclass(frozen=True)
class RepairRejection:
    """Immutable record of why a repair was rejected."""
    node_id: str
    reason: str
    damage_type: DamageType
    
    # Classification
    rejection_category: Literal[
        "non_repairable",
        "side_effects",
        "non_deterministic",
        "ambiguous_damage",
        "constraint_violation",
        "safety_rejection"
    ]
    
    # Evidence
    evidence: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


class RepairException(Exception):
    """Base exception for repair operations."""
    pass


class NonRepairableNodeError(RepairException):
    """Node is marked as non-repairable."""
    pass


class AmbiguousDamageError(RepairException):
    """Damage cannot be clearly classified."""
    pass


class DeterminismViolationError(RepairException):
    """Repair would violate determinism requirements."""
    pass


class ConstraintViolationError(RepairException):
    """Repair would violate operational constraints."""
    pass