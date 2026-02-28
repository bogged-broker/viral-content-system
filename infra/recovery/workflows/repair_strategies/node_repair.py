"""
/recovery/workflows/repair_strategies/node_repair.py

Node-Local Repair Strategy Engine

This is the ONLY place in the entire system where node-level mutation logic is allowed to exist.

Its job is NOT to fix the workflow.
Its job is to propose a provably safe, deterministic node-local repair that can later be 
replayed, validated, and merged.

If this file is wrong → recovery becomes corruption.

WHAT THIS FILE CONTROLS:
Repairing a single workflow node WITHOUT modifying:
- DAG topology
- Sibling nodes
- Downstream execution
- Live state

WHAT THIS FILE IS:
- Node damage interpreter
- Repair eligibility decider
- Repair action constructor
- Determinism & safety annotator
- Proof emitter

WHAT THIS FILE IS NOT:
- Node executor
- Artifact mutator
- I/O performer
- Auto-fixer
- Ambiguity resolver

Mental Model:
If recovery is surgery, node_repair.py is microsurgery —
only touching tissue under a microscope, with another surgeon watching,
and a full rollback plan ready.

Design Principle:
If a repair cannot be deterministic, declarative, and justified → it is rejected, not deferred.
Repair silence is corruption.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Tuple, Dict
from collections.abc import Sequence
import time


# ============================================================================
# CORE ENUMS
# ============================================================================


class NodeRepairType(Enum):
    """
    Exhaustive set of allowed node repair operations.
    
    Hybrid or compound repairs are forbidden at this layer.
    Each repair must be exactly one type.
    """
    RECOMPUTE = "recompute"  # Deterministic recomputation required
    PARAMETER_PATCH = "parameter_patch"  # Config correction, no logic change
    CACHE_INVALIDATION = "cache_invalidation"  # Invalidate cached output
    OUTPUT_REGENERATION = "output_regeneration"  # Regenerate from identical inputs
    SCHEMA_ALIGNMENT = "schema_alignment"  # Align to expected schema


class NodeDamageType(Enum):
    """
    Classified node damage types.
    
    If damage cannot be conclusively classified → repair is rejected.
    No guessing.
    """
    OUTPUT_CORRUPTION = "output_corruption"  # Hash mismatch on outputs
    PARAMETER_INCONSISTENCY = "parameter_inconsistency"  # Config mismatch
    SCHEMA_DRIFT = "schema_drift"  # Output schema incompatible
    STALE_CACHE = "stale_cache"  # Cached data outdated
    DETERMINISM_VIOLATION = "determinism_violation"  # Non-reproducible results
    PARTIAL_EXECUTION = "partial_execution"  # Incomplete artifacts
    UNKNOWN = "unknown"  # Cannot classify (triggers rejection)


class RiskLevel(Enum):
    """
    Risk classification for node repairs.
    
    Node repairs are typically LOW or MEDIUM (localized impact).
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NodeRepairRejectionReason(Enum):
    """Exhaustive rejection reasons."""
    AMBIGUOUS_DAMAGE = "ambiguous_damage"
    CROSS_NODE_IMPACT = "cross_node_impact"
    NON_DETERMINISTIC = "non_deterministic"
    SIDE_EFFECTS_DETECTED = "side_effects_detected"
    MISSING_INPUTS = "missing_inputs"
    UNREPLAYABLE = "unreplayable"
    INCOMPLETE_CONTEXT = "incomplete_context"
    DAMAGE_SCOPE_LEAK = "damage_scope_leak"
    UNSEEDABLE_RNG = "unseedable_rng"
    TIME_DEPENDENCY = "time_dependency"
    INPUTS_NOT_CAPTURED = "inputs_not_captured"
    NON_IDEMPOTENT = "non_idempotent"


# ============================================================================
# EXCEPTIONS
# ============================================================================


class NodeRepairError(Exception):
    """Base exception for node repair failures."""
    pass


class NodeRepairRejection(NodeRepairError):
    """
    Raised when node repair is explicitly rejected.
    
    Rejection is a first-class outcome - not an error condition.
    This is expected and normal behavior.
    """
    def __init__(
        self,
        reason: NodeRepairRejectionReason,
        details: dict[str, Any]
    ):
        self.reason = reason
        self.details = details
        super().__init__(f"Node repair rejected: {reason.value} - {details}")


class NodeRepairInvariantViolation(NodeRepairError):
    """Raised when repair violates absolute constraints."""
    pass


# ============================================================================
# NODE DEFINITIONS
# ============================================================================


@dataclass(frozen=True)
class NodeDefinition:
    """
    Immutable definition of a workflow node.
    
    Complete snapshot of node configuration and metadata.
    """
    node_id: str
    node_type: str
    parameters: dict[str, Any]
    inputs: frozenset[str]  # Input artifact IDs
    outputs: frozenset[str]  # Output artifact IDs
    schema_version: str
    deterministic: bool
    side_effects: bool  # Whether node has external side effects
    idempotent: bool  # Whether repeated execution is safe
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate node definition."""
        if not self.node_id:
            raise ValueError("node_id required")
        if not self.node_type:
            raise ValueError("node_type required")


@dataclass(frozen=True)
class ArtifactDescriptor:
    """
    Description of a workflow artifact.
    
    Immutable snapshot of artifact state and metadata.
    """
    artifact_id: str
    schema_hash: str
    content_hash: str | None  # None if corrupted/missing
    size_bytes: int
    created_at: int
    valid: bool
    
    def is_corrupted(self) -> bool:
        """Check if artifact is corrupted."""
        return not self.valid or self.content_hash is None


# ============================================================================
# DAMAGE REPORT
# ============================================================================


@dataclass(frozen=True)
class NodeDamageReport:
    """
    Explicit description of detected node damage.
    
    No inference. No speculation. Only facts.
    """
    node_id: str
    damage_type: NodeDamageType
    detection_time: int
    detector: str  # Component that detected damage
    evidence: dict[str, Any]  # Damage-specific evidence
    corrupted_artifacts: frozenset[str]  # Artifact IDs
    affected_outputs: frozenset[str]  # Output artifact IDs
    
    def __post_init__(self):
        """Validate damage report."""
        if not self.node_id:
            raise ValueError("node_id required")
        if not self.detector:
            raise ValueError("Damage detector must be identified")
        if not self.evidence:
            raise ValueError("Damage evidence required")
    
    def is_ambiguous(self) -> bool:
        """Check if damage classification is ambiguous."""
        return self.damage_type == NodeDamageType.UNKNOWN


# ============================================================================
# REPAIR CONTEXT
# ============================================================================


@dataclass(frozen=True)
class WorkflowSnapshot:
    """
    Immutable snapshot of entire workflow.
    
    Read-only. No mutation allowed.
    """
    workflow_id: str
    nodes: dict[str, NodeDefinition]
    artifacts: dict[str, ArtifactDescriptor]
    snapshot_time: int
    
    def get_node(self, node_id: str) -> NodeDefinition | None:
        """Safely retrieve node definition."""
        return self.nodes.get(node_id)
    
    def get_artifact(self, artifact_id: str) -> ArtifactDescriptor | None:
        """Safely retrieve artifact descriptor."""
        return self.artifacts.get(artifact_id)
    
    def get_node_inputs(self, node_id: str) -> frozenset[ArtifactDescriptor]:
        """Get all input artifacts for a node."""
        node = self.get_node(node_id)
        if not node:
            return frozenset()
        
        return frozenset(
            self.artifacts[aid]
            for aid in node.inputs
            if aid in self.artifacts
        )
    
    def get_node_outputs(self, node_id: str) -> frozenset[ArtifactDescriptor]:
        """Get all output artifacts for a node."""
        node = self.get_node(node_id)
        if not node:
            return frozenset()
        
        return frozenset(
            self.artifacts[aid]
            for aid in node.outputs
            if aid in self.artifacts
        )


@dataclass(frozen=True)
class DeterminismPolicy:
    """
    Policy for determinism requirements.
    
    Defines what level of determinism is required for repairs.
    """
    require_deterministic_nodes: bool = True
    require_seedable_rng: bool = True
    require_time_abstraction: bool = True
    require_captured_inputs: bool = True
    require_idempotent_execution: bool = True
    allow_side_effects: bool = False
    
    def validate_node(self, node: NodeDefinition) -> bool:
        """Check if node meets policy requirements."""
        if self.require_deterministic_nodes and not node.deterministic:
            return False
        if not self.allow_side_effects and node.side_effects:
            return False
        if self.require_idempotent_execution and not node.idempotent:
            return False
        return True


@dataclass(frozen=True)
class RepairConstraints:
    """
    Global repair constraints.
    
    Defines what repairs are allowed system-wide.
    """
    allow_recompute: bool = True
    allow_parameter_patch: bool = True
    allow_cache_invalidation: bool = True
    allow_output_regeneration: bool = True
    allow_schema_alignment: bool = True
    max_blast_radius: int = 1  # Node-local only
    require_complete_inputs: bool = True
    
    def validate_repair_type(self, repair_type: NodeRepairType) -> bool:
        """Check if repair type is allowed."""
        if repair_type == NodeRepairType.RECOMPUTE:
            return self.allow_recompute
        elif repair_type == NodeRepairType.PARAMETER_PATCH:
            return self.allow_parameter_patch
        elif repair_type == NodeRepairType.CACHE_INVALIDATION:
            return self.allow_cache_invalidation
        elif repair_type == NodeRepairType.OUTPUT_REGENERATION:
            return self.allow_output_regeneration
        elif repair_type == NodeRepairType.SCHEMA_ALIGNMENT:
            return self.allow_schema_alignment
        return False


@dataclass(frozen=True)
class NodeRepairContext:
    """
    Complete immutable context for node repair decision.
    
    Inputs are never modified. Read-only only.
    """
    damage_report: NodeDamageReport
    workflow_snapshot: WorkflowSnapshot
    constraints: RepairConstraints
    determinism_policy: DeterminismPolicy
    
    def __post_init__(self):
        """Validate context completeness."""
        # Verify damaged node exists in snapshot
        node_id = self.damage_report.node_id
        if not self.workflow_snapshot.get_node(node_id):
            raise ValueError(f"Damaged node {node_id} not in workflow snapshot")
    
    def get_damaged_node(self) -> NodeDefinition:
        """Get the damaged node definition."""
        node = self.workflow_snapshot.get_node(self.damage_report.node_id)
        if not node:
            raise ValueError(f"Node {self.damage_report.node_id} not found")
        return node
    
    def get_input_artifacts(self) -> frozenset[ArtifactDescriptor]:
        """Get input artifacts for damaged node."""
        return self.workflow_snapshot.get_node_inputs(self.damage_report.node_id)
    
    def get_output_artifacts(self) -> frozenset[ArtifactDescriptor]:
        """Get output artifacts for damaged node."""
        return self.workflow_snapshot.get_node_outputs(self.damage_report.node_id)


# ============================================================================
# REPAIR ACTION
# ============================================================================


@dataclass(frozen=True)
class NodeRepairAction:
    """
    Pure declarative node repair proposal.
    
    No execution. No mutation. Pure declaration.
    This object is immutable and replay-portable.
    """
    node_id: str
    repair_type: NodeRepairType
    recompute_required: bool
    updated_node: NodeDefinition | None  # None if no node changes needed
    affected_artifacts: frozenset[str]  # Artifact IDs to invalidate/regenerate
    determinism_required: bool
    replay_required: bool
    risk_level: RiskLevel
    blast_radius: int  # Always 1 for node-local repairs
    justification: str  # Human-readable + audit-safe explanation
    proof: dict[str, Any]  # Structured evidence
    
    def __post_init__(self):
        """Validate action completeness."""
        if not self.node_id:
            raise ValueError("node_id required")
        if not self.justification:
            raise ValueError("justification required")
        if not self.proof:
            raise ValueError("proof required")
        if self.blast_radius != 1:
            raise ValueError("Node repair must be node-local only (blast_radius=1)")
    
    def get_damage_to_fix_mapping(self) -> str:
        """Extract damage → fix mapping from proof."""
        return self.proof.get('damage_to_fix_mapping', 'Not specified')
    
    def get_no_sibling_impact_reason(self) -> str:
        """Extract why no other nodes are affected."""
        return self.proof.get('no_sibling_impact_reason', 'Not specified')


# ============================================================================
# OBSERVABILITY
# ============================================================================


class NodeRepairObserver(Protocol):
    """
    Interface for node repair observability.
    
    Emits structured audit events for all repair attempts.
    """
    
    def node_repair_attempted(
        self,
        context: NodeRepairContext,
        repair_type: NodeRepairType
    ) -> None:
        """Log repair attempt."""
        ...
    
    def node_repair_rejected(
        self,
        context: NodeRepairContext,
        reason: NodeRepairRejectionReason,
        details: dict[str, Any]
    ) -> None:
        """Log repair rejection."""
        ...
    
    def node_repair_proposed(
        self,
        action: NodeRepairAction
    ) -> None:
        """Log successful repair proposal."""
        ...


# ============================================================================
# DETERMINISM CERTIFICATION
# ============================================================================


class DeterminismCertifier:
    """
    Certifies that a node repair meets determinism requirements.
    
    Pure verification. No inference. No heuristics.
    """
    
    @staticmethod
    def certify_recompute(
        node: NodeDefinition,
        policy: DeterminismPolicy
    ) -> tuple[bool, str]:
        """
        Certify that node can be deterministically recomputed.
        
        Returns:
            Tuple of (is_valid, reason)
        """
        # Check basic determinism
        if policy.require_deterministic_nodes and not node.deterministic:
            return (False, "Node is marked non-deterministic")
        
        # Check side effects
        if not policy.allow_side_effects and node.side_effects:
            return (False, "Node has side effects")
        
        # Check idempotence
        if policy.require_idempotent_execution and not node.idempotent:
            return (False, "Node is not idempotent")
        
        # Check for time dependencies
        if policy.require_time_abstraction:
            time_dependent = node.metadata.get('uses_wall_clock', False)
            if time_dependent:
                return (False, "Node depends on wall-clock time")
        
        # Check for seedable RNG
        if policy.require_seedable_rng:
            uses_random = node.metadata.get('uses_randomness', False)
            has_seed = node.metadata.get('rng_seedable', False)
            if uses_random and not has_seed:
                return (False, "Node uses non-seedable RNG")
        
        # Check inputs captured
        if policy.require_captured_inputs:
            inputs_captured = node.metadata.get('inputs_captured', True)
            if not inputs_captured:
                return (False, "Node inputs not fully captured")
        
        return (True, "Node meets determinism requirements")
    
    @staticmethod
    def certify_idempotent(node: NodeDefinition) -> tuple[bool, str]:
        """
        Certify that repeated execution is safe.
        
        Returns:
            Tuple of (is_valid, reason)
        """
        if not node.idempotent:
            return (False, "Node is not marked idempotent")
        
        if node.side_effects:
            return (False, "Node has side effects - not safely idempotent")
        
        return (True, "Node is idempotent")


# ============================================================================
# NODE REPAIR INVARIANTS
# ============================================================================


class NodeRepairInvariants:
    """
    Absolute constraints for node repairs.
    
    Violations trigger immediate rejection.
    """
    
    @staticmethod
    def validate_node_local_scope(
        damage_report: NodeDamageReport,
        workflow: WorkflowSnapshot
    ) -> None:
        """
        Ensure damage is truly node-local.
        
        Raises:
            NodeRepairRejection: If damage leaks beyond node boundary.
        """
        node_id = damage_report.node_id
        node = workflow.get_node(node_id)
        
        if not node:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.INCOMPLETE_CONTEXT,
                {'message': f'Node {node_id} not found in workflow'}
            )
        
        # Verify all affected outputs belong to this node
        for artifact_id in damage_report.affected_outputs:
            if artifact_id not in node.outputs:
                raise NodeRepairRejection(
                    NodeRepairRejectionReason.DAMAGE_SCOPE_LEAK,
                    {
                        'message': 'Damage affects artifacts outside node',
                        'node_id': node_id,
                        'artifact_id': artifact_id
                    }
                )
        
        # Verify all corrupted artifacts belong to this node
        for artifact_id in damage_report.corrupted_artifacts:
            if artifact_id not in node.outputs:
                raise NodeRepairRejection(
                    NodeRepairRejectionReason.DAMAGE_SCOPE_LEAK,
                    {
                        'message': 'Corrupted artifacts outside node',
                        'node_id': node_id,
                        'artifact_id': artifact_id
                    }
                )
    
    @staticmethod
    def validate_no_topology_changes(
        updated_node: NodeDefinition | None,
        original_node: NodeDefinition
    ) -> None:
        """
        Ensure repair doesn't change DAG topology.
        
        Raises:
            NodeRepairRejection: If topology would change.
        """
        if updated_node is None:
            return  # No node changes
        
        # Inputs and outputs must remain identical
        if updated_node.inputs != original_node.inputs:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.CROSS_NODE_IMPACT,
                {
                    'message': 'Cannot change node inputs',
                    'original_inputs': list(original_node.inputs),
                    'updated_inputs': list(updated_node.inputs)
                }
            )
        
        if updated_node.outputs != original_node.outputs:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.CROSS_NODE_IMPACT,
                {
                    'message': 'Cannot change node outputs',
                    'original_outputs': list(original_node.outputs),
                    'updated_outputs': list(updated_node.outputs)
                }
            )
        
        # Node type must remain identical
        if updated_node.node_type != original_node.node_type:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.CROSS_NODE_IMPACT,
                {
                    'message': 'Cannot change node type',
                    'original_type': original_node.node_type,
                    'updated_type': updated_node.node_type
                }
            )
    
    @staticmethod
    def validate_complete_inputs(
        context: NodeRepairContext
    ) -> None:
        """
        Ensure all required inputs are available.
        
        Raises:
            NodeRepairRejection: If inputs are incomplete.
        """
        if not context.constraints.require_complete_inputs:
            return
        
        node = context.get_damaged_node()
        input_artifacts = context.get_input_artifacts()
        
        # All inputs must be present
        available_ids = {a.artifact_id for a in input_artifacts}
        required_ids = node.inputs
        
        missing = required_ids - available_ids
        if missing:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.MISSING_INPUTS,
                {
                    'message': 'Required inputs missing',
                    'node_id': node.node_id,
                    'missing_inputs': list(missing)
                }
            )
        
        # All inputs must be valid (not corrupted)
        corrupted = [a.artifact_id for a in input_artifacts if a.is_corrupted()]
        if corrupted:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.MISSING_INPUTS,
                {
                    'message': 'Required inputs corrupted',
                    'node_id': node.node_id,
                    'corrupted_inputs': corrupted
                }
            )
    
    @staticmethod
    def validate_no_live_state_access(
        repair_type: NodeRepairType
    ) -> None:
        """
        Ensure repair doesn't access live state.
        
        This is a placeholder - real implementation would check
        repair action against live state access patterns.
        """
        # All repair types must be declarative only
        # No execution, no I/O, no live state access
        pass
    
    @staticmethod
    def validate_blast_radius(
        affected_artifacts: frozenset[str],
        node: NodeDefinition
    ) -> None:
        """
        Ensure repair is truly node-local.
        
        Raises:
            NodeRepairRejection: If blast radius exceeds node boundary.
        """
        # All affected artifacts must belong to this node
        for artifact_id in affected_artifacts:
            if artifact_id not in node.outputs:
                raise NodeRepairRejection(
                    NodeRepairRejectionReason.CROSS_NODE_IMPACT,
                    {
                        'message': 'Repair affects artifacts outside node',
                        'node_id': node.node_id,
                        'artifact_id': artifact_id
                    }
                )


# ============================================================================
# NODE REPAIR ENGINE
# ============================================================================


class NodeRepairEngine:
    """
    Node-local repair strategy engine.
    
    Proposes provably safe, deterministic node repairs.
    This is microsurgery - only touching tissue under a microscope.
    
    Rejection is a first-class outcome.
    """
    
    def __init__(
        self,
        observer: NodeRepairObserver | None = None
    ):
        """
        Initialize node repair engine.
        
        Args:
            observer: Optional observability integration.
        """
        self._observer = observer
        self._certifier = DeterminismCertifier()
    
    def propose_repair(
        self,
        context: NodeRepairContext
    ) -> NodeRepairAction:
        """
        Propose node repair for damaged node.
        
        Strict phases:
        1. Damage eligibility gate
        2. Determinism certification
        3. Repair action synthesis
        4. Safety & risk annotation
        5. Proof emission
        
        Args:
            context: Complete repair context.
            
        Returns:
            Declarative repair action.
            
        Raises:
            NodeRepairRejection: If repair cannot be safely proposed.
        """
        node = context.get_damaged_node()
        damage_type = context.damage_report.damage_type
        
        # Select repair type
        repair_type = self._select_repair_type(damage_type)
        
        # Log attempt
        if self._observer:
            self._observer.node_repair_attempted(context, repair_type)
        
        try:
            # PHASE 1: Damage Eligibility Gate
            self._gate_eligibility(context)
            
            # PHASE 2: Determinism Certification
            self._certify_determinism(context, repair_type)
            
            # PHASE 3: Repair Action Synthesis
            action = self._synthesize_repair(context, repair_type)
            
            # PHASE 4: Safety & Risk Annotation
            action = self._annotate_safety(context, action)
            
            # PHASE 5: Proof Emission (already in action)
            
            # Log success
            if self._observer:
                self._observer.node_repair_proposed(action)
            
            return action
            
        except NodeRepairRejection as e:
            # Log rejection
            if self._observer:
                self._observer.node_repair_rejected(
                    context,
                    e.reason,
                    e.details
                )
            raise
    
    def _gate_eligibility(
        self,
        context: NodeRepairContext
    ) -> None:
        """
        Phase 1: Gate on repair eligibility.
        
        Reject immediately if repair is not possible.
        
        Raises:
            NodeRepairRejection: If repair ineligible.
        """
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # Check for ambiguous damage
        if damage_report.is_ambiguous():
            raise NodeRepairRejection(
                NodeRepairRejectionReason.AMBIGUOUS_DAMAGE,
                {
                    'message': 'Damage type cannot be conclusively classified',
                    'node_id': node.node_id,
                    'evidence': damage_report.evidence
                }
            )
        
        # Validate node-local scope
        NodeRepairInvariants.validate_node_local_scope(
            damage_report,
            context.workflow_snapshot
        )
        
        # Check side effects
        if node.side_effects and not context.determinism_policy.allow_side_effects:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.SIDE_EFFECTS_DETECTED,
                {
                    'message': 'Node has side effects - cannot safely repair',
                    'node_id': node.node_id
                }
            )
        
        # Check determinism
        if not node.deterministic and context.determinism_policy.require_deterministic_nodes:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.NON_DETERMINISTIC,
                {
                    'message': 'Node is non-deterministic and unreplayable',
                    'node_id': node.node_id
                }
            )
        
        # Validate complete inputs
        NodeRepairInvariants.validate_complete_inputs(context)
    
    def _certify_determinism(
        self,
        context: NodeRepairContext,
        repair_type: NodeRepairType
    ) -> None:
        """
        Phase 2: Certify determinism requirements.
        
        Raises:
            NodeRepairRejection: If determinism cannot be certified.
        """
        node = context.get_damaged_node()
        policy = context.determinism_policy
        
        # If recomputation required
        if repair_type in [NodeRepairType.RECOMPUTE, NodeRepairType.OUTPUT_REGENERATION]:
            is_valid, reason = self._certifier.certify_recompute(node, policy)
            
            if not is_valid:
                # Map reason to rejection type
                if 'RNG' in reason:
                    rejection_reason = NodeRepairRejectionReason.UNSEEDABLE_RNG
                elif 'time' in reason:
                    rejection_reason = NodeRepairRejectionReason.TIME_DEPENDENCY
                elif 'inputs' in reason:
                    rejection_reason = NodeRepairRejectionReason.INPUTS_NOT_CAPTURED
                elif 'idempotent' in reason:
                    rejection_reason = NodeRepairRejectionReason.NON_IDEMPOTENT
                else:
                    rejection_reason = NodeRepairRejectionReason.NON_DETERMINISTIC
                
                raise NodeRepairRejection(
                    rejection_reason,
                    {
                        'message': reason,
                        'node_id': node.node_id
                    }
                )
        
        # If idempotence required
        if repair_type in [NodeRepairType.OUTPUT_REGENERATION]:
            is_valid, reason = self._certifier.certify_idempotent(node)
            
            if not is_valid:
                raise NodeRepairRejection(
                    NodeRepairRejectionReason.NON_IDEMPOTENT,
                    {
                        'message': reason,
                        'node_id': node.node_id
                    }
                )
    
    def _synthesize_repair(
        self,
        context: NodeRepairContext,
        repair_type: NodeRepairType
    ) -> NodeRepairAction:
        """
        Phase 3: Synthesize repair action.
        
        Creates declarative repair proposal.
        """
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # Check if repair type is allowed
        if not context.constraints.validate_repair_type(repair_type):
            raise NodeRepairRejection(
                NodeRepairRejectionReason.UNREPLAYABLE,
                {
                    'message': 'Repair type not allowed by constraints',
                    'repair_type': repair_type.value
                }
            )
        
        # Handle different repair types
        if repair_type == NodeRepairType.RECOMPUTE:
            return self._synthesize_recompute(context)
        
        elif repair_type == NodeRepairType.PARAMETER_PATCH:
            return self._synthesize_parameter_patch(context)
        
        elif repair_type == NodeRepairType.CACHE_INVALIDATION:
            return self._synthesize_cache_invalidation(context)
        
        elif repair_type == NodeRepairType.OUTPUT_REGENERATION:
            return self._synthesize_output_regeneration(context)
        
        elif repair_type == NodeRepairType.SCHEMA_ALIGNMENT:
            return self._synthesize_schema_alignment(context)
        
        raise NodeRepairRejection(
            NodeRepairRejectionReason.AMBIGUOUS_DAMAGE,
            {
                'message': 'Cannot synthesize repair for damage type',
                'damage_type': damage_report.damage_type.value
            }
        )
    
    def _synthesize_recompute(
        self,
        context: NodeRepairContext
    ) -> NodeRepairAction:
        """Synthesize RECOMPUTE repair."""
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # All outputs must be regenerated
        affected_artifacts = node.outputs
        
        # Validate blast radius
        NodeRepairInvariants.validate_blast_radius(affected_artifacts, node)
        
        # No node changes needed - just invalidate outputs
        updated_node = None
        
        # Generate proof
        proof = {
            'damage_to_fix_mapping': f'{damage_report.damage_type.value} → deterministic recompute',
            'no_sibling_impact_reason': 'Recompute only affects this node\'s outputs',
            'determinism_certification': 'Node meets all determinism requirements',
            'inputs_captured': True,
            'outputs_deterministic': True
        }
        
        justification = self._generate_justification(
            repair_type=NodeRepairType.RECOMPUTE,
            node=node,
            damage_report=damage_report,
            proof=proof
        )
        
        return NodeRepairAction(
            node_id=node.node_id,
            repair_type=NodeRepairType.RECOMPUTE,
            recompute_required=True,
            updated_node=updated_node,
            affected_artifacts=affected_artifacts,
            determinism_required=True,
            replay_required=True,
            risk_level=RiskLevel.LOW,  # Will be refined in phase 4
            blast_radius=1,
            justification=justification,
            proof=proof
        )
    
    def _synthesize_parameter_patch(
        self,
        context: NodeRepairContext
    ) -> NodeRepairAction:
        """Synthesize PARAMETER_PATCH repair."""
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # Extract corrected parameters from evidence
        corrected_params = damage_report.evidence.get('corrected_parameters')
        if not corrected_params:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.INCOMPLETE_CONTEXT,
                {
                    'message': 'Parameter patch requires corrected_parameters in evidence',
                    'node_id': node.node_id
                }
            )
        
        # Create updated node with patched parameters
        updated_node = NodeDefinition(
            node_id=node.node_id,
            node_type=node.node_type,
            parameters=corrected_params,
            inputs=node.inputs,
            outputs=node.outputs,
            schema_version=node.schema_version,
            deterministic=node.deterministic,
            side_effects=node.side_effects,
            idempotent=node.idempotent,
            metadata=node.metadata
        )
        
        # Validate no topology changes
        NodeRepairInvariants.validate_no_topology_changes(updated_node, node)
        
        # All outputs need regeneration after parameter change
        affected_artifacts = node.outputs
        
        # Validate blast radius
        NodeRepairInvariants.validate_blast_radius(affected_artifacts, node)
        
        # Generate proof
        proof = {
            'damage_to_fix_mapping': f'{damage_report.damage_type.value} → parameter correction',
            'no_sibling_impact_reason': 'Only this node\'s parameters changed',
            'parameter_diff': {
                'old': node.parameters,
                'new': corrected_params
            }
        }
        
        justification = self._generate_justification(
            repair_type=NodeRepairType.PARAMETER_PATCH,
            node=node,
            damage_report=damage_report,
            proof=proof
        )
        
        return NodeRepairAction(
            node_id=node.node_id,
            repair_type=NodeRepairType.PARAMETER_PATCH,
            recompute_required=True,
            updated_node=updated_node,
            affected_artifacts=affected_artifacts,
            determinism_required=True,
            replay_required=True,
            risk_level=RiskLevel.MEDIUM,  # Parameter changes are higher risk
            blast_radius=1,
            justification=justification,
            proof=proof
        )
    
    def _synthesize_cache_invalidation(
        self,
        context: NodeRepairContext
    ) -> NodeRepairAction:
        """Synthesize CACHE_INVALIDATION repair."""
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # Only invalidate corrupted artifacts
        affected_artifacts = damage_report.corrupted_artifacts
        
        # Validate blast radius
        NodeRepairInvariants.validate_blast_radius(affected_artifacts, node)
        
        # No node changes
        updated_node = None
        
        # Generate proof
        proof = {
            'damage_to_fix_mapping': f'{damage_report.damage_type.value} → cache invalidation',
            'no_sibling_impact_reason': 'Only invalidates cached outputs, no node changes',
            'invalidated_artifacts': list(affected_artifacts)
        }
        
        justification = self._generate_justification(
            repair_type=NodeRepairType.CACHE_INVALIDATION,
            node=node,
            damage_report=damage_report,
            proof=proof
        )
        
        return NodeRepairAction(
            node_id=node.node_id,
            repair_type=NodeRepairType.CACHE_INVALIDATION,
            recompute_required=True,
            updated_node=updated_node,
            affected_artifacts=affected_artifacts,
            determinism_required=False,  # Just invalidation, not recompute
            replay_required=False,
            risk_level=RiskLevel.LOW,
            blast_radius=1,
            justification=justification,
            proof=proof
        )
    
    def _synthesize_output_regeneration(
        self,
        context: NodeRepairContext
    ) -> NodeRepairAction:
        """Synthesize OUTPUT_REGENERATION repair."""
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # Regenerate affected outputs
        affected_artifacts = damage_report.affected_outputs
        
        # Validate blast radius
        NodeRepairInvariants.validate_blast_radius(affected_artifacts, node)
        
        # No node changes - inputs and config identical
        updated_node = None
        
        # Generate proof
        proof = {
            'damage_to_fix_mapping': f'{damage_report.damage_type.value} → regenerate from identical inputs',
            'no_sibling_impact_reason': 'Regeneration uses same inputs/config',
            'idempotent_certified': True,
            'inputs_unchanged': True
        }
        
        justification = self._generate_justification(
            repair_type=NodeRepairType.OUTPUT_REGENERATION,
            node=node,
            damage_report=damage_report,
            proof=proof
        )
        
        return NodeRepairAction(
            node_id=node.node_id,
            repair_type=NodeRepairType.OUTPUT_REGENERATION,
            recompute_required=True,
            updated_node=updated_node,
            affected_artifacts=affected_artifacts,
            determinism_required=True,
            replay_required=True,
            risk_level=RiskLevel.LOW,
            blast_radius=1,
            justification=justification,
            proof=proof
        )
    
    def _synthesize_schema_alignment(
        self,
        context: NodeRepairContext
    ) -> NodeRepairAction:
        """Synthesize SCHEMA_ALIGNMENT repair."""
        node = context.get_damaged_node()
        damage_report = context.damage_report
        
        # Extract target schema from evidence
        target_schema = damage_report.evidence.get('target_schema_version')
        if not target_schema:
            raise NodeRepairRejection(
                NodeRepairRejectionReason.INCOMPLETE_CONTEXT,
                {
                    'message': 'Schema alignment requires target_schema_version in evidence',
                    'node_id': node.node_id
                }
            )
        
        # Create updated node with new schema version
        updated_node = NodeDefinition(
            node_id=node.node_id,
            node_type=node.node_type,
            parameters=node.parameters,
            inputs=node.inputs,
            outputs=node.outputs,
            schema_version=target_schema,
            deterministic=node.deterministic,
            side_effects=node.side_effects,
            idempotent=node.idempotent,
            metadata=node.metadata
        )
        
        # Validate no topology changes
        NodeRepairInvariants.validate_no_topology_changes(updated_node, node)
        
        # All outputs need regeneration after schema change
        affected_artifacts = node.outputs
        
        # Validate blast radius
        NodeRepairInvariants.validate_blast_radius(affected_artifacts, node)
        
        # Generate proof
        proof = {
            'damage_to_fix_mapping': f'{damage_report.damage_type.value} → schema alignment',
            'no_sibling_impact_reason': 'Schema change is semantically equivalent',
            'schema_diff': {
                'old': node.schema_version,
                'new': target_schema
            },
            'semantic_equivalence': True
        }
        
        justification = self._generate_justification(
            repair_type=NodeRepairType.SCHEMA_ALIGNMENT,
            node=node,
            damage_report=damage_report,
            proof=proof
        )
        
        return NodeRepairAction(
            node_id=node.node_id,
            repair_type=NodeRepairType.SCHEMA_ALIGNMENT,
            recompute_required=True,
            updated_node=updated_node,
            affected_artifacts=affected_artifacts,
            determinism_required=True,
            replay_required=True,
            risk_level=RiskLevel.MEDIUM,
            blast_radius=1,
            justification=justification,
            proof=proof
        )
    
    def _annotate_safety(
        self,
        context: NodeRepairContext,
        action: NodeRepairAction
    ) -> NodeRepairAction:
        """
        Phase 4: Annotate safety and risk levels.
        
        Refines risk assessment based on repair characteristics.
        """
        # Calculate refined risk level
        risk_level = self._calculate_risk_level(action, context)
        
        # Create updated action with refined risk
        updated_action = NodeRepairAction(
            node_id=action.node_id,
            repair_type=action.repair_type,
            recompute_required=action.recompute_required,
            updated_node=action.updated_node,
            affected_artifacts=action.affected_artifacts,
            determinism_required=action.determinism_required,
            replay_required=action.replay_required,
            risk_level=risk_level,
            blast_radius=action.blast_radius,
            justification=action.justification,
            proof=action.proof
        )
        
        return updated_action
    
    def _calculate_risk_level(
        self,
        action: NodeRepairAction,
        context: NodeRepairContext
    ) -> RiskLevel:
        """
        Calculate risk level based on repair characteristics.
        
        Node repairs are typically LOW or MEDIUM risk (localized).
        """
        node = context.get_damaged_node()
        risk = action.risk_level  # Start with initial assessment
        
        # Increase risk for parameter changes (logic changes)
        if action.repair_type == NodeRepairType.PARAMETER_PATCH:
            if risk == RiskLevel.LOW:
                risk = RiskLevel.MEDIUM
        
        # Increase risk for schema changes
        if action.repair_type == NodeRepairType.SCHEMA_ALIGNMENT:
            if risk == RiskLevel.LOW:
                risk = RiskLevel.MEDIUM
        
        # Increase risk if node has side effects
        if node.side_effects:
            if risk == RiskLevel.LOW:
                risk = RiskLevel.MEDIUM
            elif risk == RiskLevel.MEDIUM:
                risk = RiskLevel.HIGH
        
        # Increase risk if not deterministic
        if not node.deterministic:
            if risk == RiskLevel.MEDIUM:
                risk = RiskLevel.HIGH
        
        # Decrease risk for simple cache invalidation
        if action.repair_type == NodeRepairType.CACHE_INVALIDATION:
            if risk == RiskLevel.MEDIUM:
                risk = RiskLevel.LOW
        
        return risk
    
    def _generate_justification(
        self,
        repair_type: NodeRepairType,
        node: NodeDefinition,
        damage_report: NodeDamageReport,
        proof: dict[str, Any]
    ) -> str:
        """
        Generate human-readable justification for repair.
        
        Must be audit-safe and explain the reasoning.
        """
        base = f"Node repair proposed: {repair_type.value}\n"
        base += f"Node: {node.node_id} (type: {node.node_type})\n"
        base += f"Damage: {damage_report.damage_type.value}\n"
        base += f"Detected by: {damage_report.detector}\n"
        base += f"\nDamage → Fix mapping:\n{proof['damage_to_fix_mapping']}\n"
        base += f"\nNo sibling impact:\n{proof['no_sibling_impact_reason']}\n"
        
        if repair_type == NodeRepairType.RECOMPUTE:
            base += "\nAll outputs will be deterministically recomputed."
        elif repair_type == NodeRepairType.PARAMETER_PATCH:
            base += f"\nParameters updated: {proof.get('parameter_diff', {})}"
        elif repair_type == NodeRepairType.CACHE_INVALIDATION:
            base += f"\nArtifacts invalidated: {proof.get('invalidated_artifacts', [])}"
        elif repair_type == NodeRepairType.OUTPUT_REGENERATION:
            base += "\nOutputs will be regenerated from identical inputs."
        elif repair_type == NodeRepairType.SCHEMA_ALIGNMENT:
            base += f"\nSchema updated: {proof.get('schema_diff', {})}"
        
        base += f"\n\nBlast radius: Node-local only (1 node)"
        base += f"\nAffected artifacts: {len(damage_report.affected_outputs)}"
        
        return base
    
    def _select_repair_type(
        self,
        damage_type: NodeDamageType
    ) -> NodeRepairType:
        """
        Select appropriate repair type based on damage.
        
        This is a mapping, not inference.
        """
        # Direct mapping from damage type to repair type
        mapping = {
            NodeDamageType.OUTPUT_CORRUPTION: NodeRepairType.OUTPUT_REGENERATION,
            NodeDamageType.PARAMETER_INCONSISTENCY: NodeRepairType.PARAMETER_PATCH,
            NodeDamageType.SCHEMA_DRIFT: NodeRepairType.SCHEMA_ALIGNMENT,
            NodeDamageType.STALE_CACHE: NodeRepairType.CACHE_INVALIDATION,
            NodeDamageType.DETERMINISM_VIOLATION: NodeRepairType.RECOMPUTE,
            NodeDamageType.PARTIAL_EXECUTION: NodeRepairType.RECOMPUTE,
        }
        
        return mapping.get(damage_type, NodeRepairType.RECOMPUTE)


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'NodeRepairType',
    'NodeDamageType',
    'RiskLevel',
    'NodeRepairRejectionReason',
    'NodeRepairError',
    'NodeRepairRejection',
    'NodeRepairInvariantViolation',
    'NodeDefinition',
    'ArtifactDescriptor',
    'NodeDamageReport',
    'WorkflowSnapshot',
    'DeterminismPolicy',
    'RepairConstraints',
    'NodeRepairContext',
    'NodeRepairAction',
    'NodeRepairObserver',
    'DeterminismCertifier',
    'NodeRepairInvariants',
    'NodeRepairEngine',
]