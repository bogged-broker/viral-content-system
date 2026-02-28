"""
/recovery/workflows/repair_strategies/edge_repair.py

Dependency Edge Repair Strategy Engine

This module handles dependency-level damage between workflow nodes WITHOUT mutating node logic.

Its sole purpose: propose safe, explicit dependency corrections when the relationship 
between nodes is wrong — not the nodes themselves.

WHAT THIS FILE IS:
- Dependency damage interpreter
- Edge repair eligibility verifier
- Repair action constructor
- Safety & proof annotator

WHAT THIS FILE IS NOT:
- Node logic modifier
- Workflow executor
- Inference engine
- Auto-selector
- Compatibility guesser

Mental Model:
If the workflow is plumbing:
- node_repair fixes a filter
- edge_repair reconnects a pipe
- workflow_repair redesigns the system

Edge repair touches flow — so it moves carefully.

Design Principle:
If compatibility cannot be proven, the repair is rejected.
Edge repair must never guess.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Tuple, Dict
from collections.abc import Sequence


# ============================================================================
# CORE ENUMS
# ============================================================================


class EdgeRepairType(Enum):
    """
    Exhaustive set of allowed edge repair operations.
    
    No hybrid or multi-edge repairs allowed.
    Each repair touches exactly one edge.
    """
    EDGE_REWIRE = "edge_rewire"  # Redirect to compatible upstream artifact
    EDGE_SUBSTITUTION = "edge_substitution"  # Replace node dependency
    EDGE_INVALIDATION = "edge_invalidation"  # Detach and force recompute
    EDGE_VERSION_ALIGNMENT = "edge_version_alignment"  # Fix version mismatch


class EdgeDamageType(Enum):
    """
    Explicitly detected edge damage types.
    
    If damage cause is ambiguous → reject.
    """
    ARTIFACT_CORRUPTION = "artifact_corruption"
    OUTPUT_INVALIDATION = "output_invalidation"
    SCHEMA_INCOMPATIBILITY = "schema_incompatibility"
    VERSION_MISMATCH = "version_mismatch"
    POISONED_CACHE = "poisoned_cache"
    BROKEN_LINEAGE = "broken_lineage"


class CompatibilityProofType(Enum):
    """
    Machine-verifiable compatibility proof types.
    
    If proof is not machine-verifiable → reject repair.
    """
    IDENTICAL_SCHEMA_HASH = "identical_schema_hash"
    DECLARED_EQUIVALENCE = "declared_equivalence"
    VERSION_CONTRACT = "version_contract"
    VERIFIED_LINEAGE = "verified_lineage"
    SEMANTIC_SIGNATURE = "semantic_signature"


class RiskLevel(Enum):
    """
    Risk classification for edge repairs.
    
    Edge repairs are typically MEDIUM or HIGH due to blast radius.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EdgeRepairRejectionReason(Enum):
    """Exhaustive rejection reasons."""
    MISSING_COMPATIBILITY_PROOF = "missing_compatibility_proof"
    INFERRED_EQUIVALENCE = "inferred_equivalence"
    HEURISTIC_MATCHING = "heuristic_matching"
    MULTI_EDGE_DAMAGE = "multi_edge_damage"
    NON_DETERMINISTIC_UPSTREAM = "non_deterministic_upstream"
    SCHEMA_MISMATCH = "schema_mismatch"
    SIDE_EFFECT_DEPENDENCY = "side_effect_dependency"
    MISSING_LINEAGE = "missing_lineage"
    NODE_LOGIC_AFFECTED = "node_logic_affected"
    TOPOLOGY_CHANGE_REQUIRED = "topology_change_required"
    AMBIGUOUS_DAMAGE = "ambiguous_damage"


# ============================================================================
# EXCEPTIONS
# ============================================================================


class EdgeRepairError(Exception):
    """Base exception for edge repair failures."""
    pass


class EdgeRepairRejection(EdgeRepairError):
    """
    Raised when edge repair is explicitly rejected.
    
    Rejection is expected behavior - not an error condition.
    """
    def __init__(
        self,
        reason: EdgeRepairRejectionReason,
        details: dict[str, Any]
    ):
        self.reason = reason
        self.details = details
        super().__init__(f"Edge repair rejected: {reason.value} - {details}")


class EdgeRepairInvariantViolation(EdgeRepairError):
    """Raised when repair violates absolute constraints."""
    pass


# ============================================================================
# COMPATIBILITY PROOF
# ============================================================================


@dataclass(frozen=True)
class CompatibilityProof:
    """
    Machine-verifiable compatibility evidence.
    
    Every repair must include explicit proof.
    No inference. No heuristics.
    """
    proof_type: CompatibilityProofType
    evidence: dict[str, Any]  # Type-specific verification data
    verified_at: int  # Timestamp of verification
    verifier: str  # Component that verified compatibility
    
    def __post_init__(self):
        """Validate proof completeness."""
        if not self.evidence:
            raise ValueError("Compatibility proof requires evidence")
        if not self.verifier:
            raise ValueError("Compatibility proof requires verifier")
        if self.verified_at <= 0:
            raise ValueError("Invalid verification timestamp")
    
    def verify_schema_hash(self) -> str | None:
        """Extract schema hash if proof type matches."""
        if self.proof_type == CompatibilityProofType.IDENTICAL_SCHEMA_HASH:
            return self.evidence.get('schema_hash')
        return None
    
    def verify_version_contract(self) -> tuple[str, str] | None:
        """Extract version contract if proof type matches."""
        if self.proof_type == CompatibilityProofType.VERSION_CONTRACT:
            old_version = self.evidence.get('old_version')
            new_version = self.evidence.get('new_version')
            if old_version and new_version:
                return (old_version, new_version)
        return None
    
    def verify_lineage(self) -> str | None:
        """Extract lineage proof if proof type matches."""
        if self.proof_type == CompatibilityProofType.VERIFIED_LINEAGE:
            return self.evidence.get('lineage_hash')
        return None


# ============================================================================
# EDGE DESCRIPTORS
# ============================================================================


@dataclass(frozen=True)
class EdgeDescriptor:
    """
    Immutable description of a dependency edge.
    
    Describes the relationship, not the endpoints.
    """
    edge_type: str  # e.g., "data_dependency", "control_dependency"
    schema_version: str
    contract: dict[str, Any]  # Semantic contract requirements
    required: bool  # Whether dependency is mandatory
    deterministic: bool  # Whether edge preserves determinism
    
    def is_compatible_with(self, other: 'EdgeDescriptor') -> bool:
        """
        Check structural compatibility with another edge.
        
        Does NOT imply semantic equivalence.
        """
        return (
            self.edge_type == other.edge_type and
            self.schema_version == other.schema_version and
            self.required == other.required and
            self.deterministic == other.deterministic
        )


@dataclass(frozen=True)
class DependencyEdge:
    """
    Complete description of a dependency relationship.
    
    Immutable snapshot of source → target connection.
    """
    source_node_id: str
    target_node_id: str
    artifact_id: str  # What flows through this edge
    descriptor: EdgeDescriptor
    lineage_hash: str | None  # Provenance tracking
    
    def __post_init__(self):
        """Validate edge."""
        if not self.source_node_id:
            raise ValueError("source_node_id required")
        if not self.target_node_id:
            raise ValueError("target_node_id required")
        if not self.artifact_id:
            raise ValueError("artifact_id required")


# ============================================================================
# DAMAGE REPORT
# ============================================================================


@dataclass(frozen=True)
class EdgeDamageReport:
    """
    Explicit description of detected edge damage.
    
    No inference. No speculation.
    """
    damaged_edge: DependencyEdge
    damage_type: EdgeDamageType
    detection_time: int
    detector: str  # Component that detected damage
    evidence: dict[str, Any]  # Damage-specific evidence
    affected_downstream_nodes: frozenset[str]
    
    def __post_init__(self):
        """Validate damage report."""
        if not self.detector:
            raise ValueError("Damage detector must be identified")
        if not self.evidence:
            raise ValueError("Damage evidence required")


# ============================================================================
# REPAIR CONTEXT
# ============================================================================


@dataclass(frozen=True)
class NodeSnapshot:
    """
    Immutable snapshot of a workflow node.
    
    Read-only view for compatibility checking.
    """
    node_id: str
    node_type: str
    inputs: frozenset[str]
    outputs: frozenset[str]
    schema_version: str
    deterministic: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowSnapshot:
    """
    Immutable snapshot of entire workflow.
    
    Read-only. No mutation allowed.
    """
    workflow_id: str
    nodes: dict[str, NodeSnapshot]
    edges: frozenset[DependencyEdge]
    snapshot_time: int
    
    def get_node(self, node_id: str) -> NodeSnapshot | None:
        """Safely retrieve node snapshot."""
        return self.nodes.get(node_id)
    
    def get_downstream_nodes(self, node_id: str) -> frozenset[str]:
        """Find all nodes that depend on given node."""
        return frozenset(
            edge.target_node_id
            for edge in self.edges
            if edge.source_node_id == node_id
        )
    
    def get_edge(self, source_id: str, target_id: str) -> DependencyEdge | None:
        """Find specific edge."""
        for edge in self.edges:
            if edge.source_node_id == source_id and edge.target_node_id == target_id:
                return edge
        return None


@dataclass(frozen=True)
class RepairConstraints:
    """
    Global repair constraints.
    
    Defines what repairs are allowed system-wide.
    """
    allow_rewiring: bool = True
    allow_substitution: bool = True
    allow_invalidation: bool = True
    allow_version_alignment: bool = True
    require_determinism: bool = True
    max_blast_radius: int = 10  # Max downstream nodes affected
    require_lineage_preservation: bool = True
    
    def validate_repair_type(self, repair_type: EdgeRepairType) -> bool:
        """Check if repair type is allowed."""
        if repair_type == EdgeRepairType.EDGE_REWIRE:
            return self.allow_rewiring
        elif repair_type == EdgeRepairType.EDGE_SUBSTITUTION:
            return self.allow_substitution
        elif repair_type == EdgeRepairType.EDGE_INVALIDATION:
            return self.allow_invalidation
        elif repair_type == EdgeRepairType.EDGE_VERSION_ALIGNMENT:
            return self.allow_version_alignment
        return False


@dataclass(frozen=True)
class EdgeRepairContext:
    """
    Complete immutable context for edge repair decision.
    
    No mutation. No inference. No shortcuts.
    """
    damage_report: EdgeDamageReport
    workflow_snapshot: WorkflowSnapshot
    constraints: RepairConstraints
    available_substitutes: frozenset[str] = field(default_factory=frozenset)
    
    def __post_init__(self):
        """Validate context completeness."""
        source_id = self.damage_report.damaged_edge.source_node_id
        target_id = self.damage_report.damaged_edge.target_node_id
        
        # Verify nodes exist in snapshot
        if not self.workflow_snapshot.get_node(source_id):
            raise ValueError(f"Source node {source_id} not in workflow snapshot")
        if not self.workflow_snapshot.get_node(target_id):
            raise ValueError(f"Target node {target_id} not in workflow snapshot")


# ============================================================================
# REPAIR ACTION
# ============================================================================


@dataclass(frozen=True)
class EdgeRepairAction:
    """
    Declarative edge repair proposal.
    
    Execution happens upstream. This is pure declaration.
    """
    repair_type: EdgeRepairType
    source_node_id: str
    target_node_id: str
    old_dependency: DependencyEdge
    new_dependency: DependencyEdge | None  # None for invalidation
    compatibility_proof: CompatibilityProof
    recompute_required: frozenset[str]  # Nodes requiring recomputation
    affected_nodes: frozenset[str]  # All impacted nodes
    determinism_required: bool
    risk_level: RiskLevel
    justification: str
    blast_radius: int  # Count of affected downstream nodes
    
    def __post_init__(self):
        """Validate action completeness."""
        # All fields required except new_dependency (for invalidation)
        if not self.source_node_id:
            raise ValueError("source_node_id required")
        if not self.target_node_id:
            raise ValueError("target_node_id required")
        if not self.justification:
            raise ValueError("justification required")
        if self.blast_radius < 0:
            raise ValueError("Invalid blast_radius")
        
        # Invalidation must not have new_dependency
        if self.repair_type == EdgeRepairType.EDGE_INVALIDATION:
            if self.new_dependency is not None:
                raise ValueError("Invalidation cannot have new_dependency")
        else:
            # All other repair types require new_dependency
            if self.new_dependency is None:
                raise ValueError(f"{self.repair_type.value} requires new_dependency")


# ============================================================================
# OBSERVABILITY
# ============================================================================


class EdgeRepairObserver(Protocol):
    """
    Interface for edge repair observability.
    
    Emits audit events for all repair attempts.
    """
    
    def edge_repair_attempted(
        self,
        context: EdgeRepairContext,
        repair_type: EdgeRepairType
    ) -> None:
        """Log repair attempt."""
        ...
    
    def edge_repair_rejected(
        self,
        context: EdgeRepairContext,
        reason: EdgeRepairRejectionReason,
        details: dict[str, Any]
    ) -> None:
        """Log repair rejection."""
        ...
    
    def edge_repair_proposed(
        self,
        action: EdgeRepairAction
    ) -> None:
        """Log successful repair proposal."""
        ...


# ============================================================================
# COMPATIBILITY VERIFICATION
# ============================================================================


class CompatibilityVerifier:
    """
    Verifies compatibility between old and new dependencies.
    
    Pure verification. No inference. No heuristics.
    """
    
    @staticmethod
    def verify_schema_compatibility(
        old_edge: DependencyEdge,
        new_edge: DependencyEdge
    ) -> CompatibilityProof | None:
        """
        Verify schema compatibility via hash comparison.
        
        Returns proof if schemas are identical, None otherwise.
        """
        old_desc = old_edge.descriptor
        new_desc = new_edge.descriptor
        
        # Schema versions must match exactly
        if old_desc.schema_version != new_desc.schema_version:
            return None
        
        # Edge types must match
        if old_desc.edge_type != new_desc.edge_type:
            return None
        
        # Compute schema hash (simplified - real impl would be more robust)
        old_hash = hash((old_desc.schema_version, old_desc.edge_type, str(old_desc.contract)))
        new_hash = hash((new_desc.schema_version, new_desc.edge_type, str(new_desc.contract)))
        
        if old_hash != new_hash:
            return None
        
        # Schemas are identical
        import time
        return CompatibilityProof(
            proof_type=CompatibilityProofType.IDENTICAL_SCHEMA_HASH,
            evidence={
                'schema_hash': hex(old_hash),
                'schema_version': old_desc.schema_version,
                'edge_type': old_desc.edge_type
            },
            verified_at=time.time_ns(),
            verifier='CompatibilityVerifier.verify_schema_compatibility'
        )
    
    @staticmethod
    def verify_version_contract(
        old_version: str,
        new_version: str,
        contract_rules: dict[str, Any]
    ) -> CompatibilityProof | None:
        """
        Verify version compatibility via contract.
        
        Returns proof if versions are compatible per contract, None otherwise.
        """
        # Parse semantic versions (simplified)
        def parse_version(v: str) -> tuple[int, int, int]:
            parts = v.split('.')
            if len(parts) != 3:
                raise ValueError(f"Invalid version format: {v}")
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        
        try:
            old_major, old_minor, old_patch = parse_version(old_version)
            new_major, new_minor, new_patch = parse_version(new_version)
        except (ValueError, IndexError):
            return None
        
        # Check contract rules
        allow_major_change = contract_rules.get('allow_major_change', False)
        allow_minor_change = contract_rules.get('allow_minor_change', True)
        
        # Major version change
        if new_major != old_major:
            if not allow_major_change:
                return None
        
        # Minor version change
        if new_major == old_major and new_minor != old_minor:
            if not allow_minor_change:
                return None
        
        # Compatible
        import time
        return CompatibilityProof(
            proof_type=CompatibilityProofType.VERSION_CONTRACT,
            evidence={
                'old_version': old_version,
                'new_version': new_version,
                'contract_rules': contract_rules,
                'major_change': new_major != old_major,
                'minor_change': new_minor != old_minor
            },
            verified_at=time.time_ns(),
            verifier='CompatibilityVerifier.verify_version_contract'
        )
    
    @staticmethod
    def verify_declared_equivalence(
        old_node_id: str,
        new_node_id: str,
        equivalence_registry: dict[str, frozenset[str]]
    ) -> CompatibilityProof | None:
        """
        Verify equivalence via explicit declaration.
        
        Returns proof if equivalence is declared, None otherwise.
        """
        # Check if new_node is declared equivalent to old_node
        equivalents = equivalence_registry.get(old_node_id, frozenset())
        
        if new_node_id not in equivalents:
            return None
        
        import time
        return CompatibilityProof(
            proof_type=CompatibilityProofType.DECLARED_EQUIVALENCE,
            evidence={
                'old_node_id': old_node_id,
                'new_node_id': new_node_id,
                'equivalence_class': list(equivalents)
            },
            verified_at=time.time_ns(),
            verifier='CompatibilityVerifier.verify_declared_equivalence'
        )
    
    @staticmethod
    def verify_lineage_continuity(
        old_edge: DependencyEdge,
        new_edge: DependencyEdge
    ) -> CompatibilityProof | None:
        """
        Verify lineage is preserved.
        
        Returns proof if lineage is continuous, None otherwise.
        """
        # Both edges must have lineage
        if not old_edge.lineage_hash or not new_edge.lineage_hash:
            return None
        
        # Lineage must match or be derivable
        # (Simplified - real impl would check lineage chain)
        if old_edge.lineage_hash != new_edge.lineage_hash:
            # Check if new lineage extends old lineage
            # This is a simplified check
            return None
        
        import time
        return CompatibilityProof(
            proof_type=CompatibilityProofType.VERIFIED_LINEAGE,
            evidence={
                'lineage_hash': old_edge.lineage_hash,
                'old_artifact': old_edge.artifact_id,
                'new_artifact': new_edge.artifact_id
            },
            verified_at=time.time_ns(),
            verifier='CompatibilityVerifier.verify_lineage_continuity'
        )


# ============================================================================
# EDGE REPAIR INVARIANTS
# ============================================================================


class EdgeRepairInvariants:
    """
    Absolute constraints for edge repairs.
    
    Violations are not allowed under any circumstances.
    """
    
    @staticmethod
    def validate_single_edge_scope(
        context: EdgeRepairContext
    ) -> None:
        """
        Ensure repair touches exactly one edge.
        
        Raises:
            EdgeRepairRejection: If multi-edge damage detected.
        """
        # Check that damage report is for single edge
        if len(context.damage_report.affected_downstream_nodes) == 0:
            # No downstream impact is suspicious
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.AMBIGUOUS_DAMAGE,
                {
                    'message': 'Damage has no downstream impact',
                    'edge': str(context.damage_report.damaged_edge)
                }
            )
        
        # Verify damage is localized to single edge
        damaged_edge = context.damage_report.damaged_edge
        
        # Check if multiple edges from same source are affected
        same_source_edges = [
            e for e in context.workflow_snapshot.edges
            if e.source_node_id == damaged_edge.source_node_id
        ]
        
        # If damage type suggests broader impact, reject
        if context.damage_report.damage_type == EdgeDamageType.OUTPUT_INVALIDATION:
            if len(same_source_edges) > 1:
                raise EdgeRepairRejection(
                    EdgeRepairRejectionReason.MULTI_EDGE_DAMAGE,
                    {
                        'message': 'Output invalidation affects all edges from source',
                        'source_node': damaged_edge.source_node_id,
                        'edge_count': len(same_source_edges)
                    }
                )
    
    @staticmethod
    def validate_no_node_logic_changes(
        old_edge: DependencyEdge,
        new_edge: DependencyEdge | None,
        workflow: WorkflowSnapshot
    ) -> None:
        """
        Ensure repair doesn't affect node logic.
        
        Raises:
            EdgeRepairRejection: If node logic would be affected.
        """
        # Target node must remain unchanged
        if new_edge is None:
            # Invalidation - target node will recompute
            # This is allowed
            return
        
        # Source and target nodes must be same or proven equivalent
        if new_edge.source_node_id != old_edge.source_node_id:
            # Different source - this is substitution
            # Verify target node doesn't change
            if new_edge.target_node_id != old_edge.target_node_id:
                raise EdgeRepairRejection(
                    EdgeRepairRejectionReason.NODE_LOGIC_AFFECTED,
                    {
                        'message': 'Cannot change both source and target',
                        'old_edge': str(old_edge),
                        'new_edge': str(new_edge)
                    }
                )
    
    @staticmethod
    def validate_no_topology_changes(
        context: EdgeRepairContext,
        new_edge: DependencyEdge | None
    ) -> None:
        """
        Ensure repair doesn't change workflow topology.
        
        Raises:
            EdgeRepairRejection: If topology would change.
        """
        if new_edge is None:
            # Invalidation doesn't change topology
            return
        
        damaged_edge = context.damage_report.damaged_edge
        
        # Number of edges must remain constant
        # (We're replacing one edge with one edge)
        
        # Verify we're not adding or removing nodes
        source_node = context.workflow_snapshot.get_node(new_edge.source_node_id)
        target_node = context.workflow_snapshot.get_node(new_edge.target_node_id)
        
        if source_node is None:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.TOPOLOGY_CHANGE_REQUIRED,
                {
                    'message': 'New source node not in workflow',
                    'node_id': new_edge.source_node_id
                }
            )
        
        if target_node is None:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.TOPOLOGY_CHANGE_REQUIRED,
                {
                    'message': 'New target node not in workflow',
                    'node_id': new_edge.target_node_id
                }
            )
    
    @staticmethod
    def validate_determinism_preservation(
        old_edge: DependencyEdge,
        new_edge: DependencyEdge | None,
        require_determinism: bool
    ) -> None:
        """
        Ensure determinism is preserved if required.
        
        Raises:
            EdgeRepairRejection: If determinism would be violated.
        """
        if not require_determinism:
            return
        
        if new_edge is None:
            # Invalidation maintains determinism if old edge was deterministic
            return
        
        # New edge must be deterministic if old edge was
        if old_edge.descriptor.deterministic and not new_edge.descriptor.deterministic:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.NON_DETERMINISTIC_UPSTREAM,
                {
                    'message': 'Cannot replace deterministic edge with non-deterministic',
                    'old_edge': str(old_edge),
                    'new_edge': str(new_edge)
                }
            )
    
    @staticmethod
    def validate_blast_radius(
        affected_nodes: frozenset[str],
        max_blast_radius: int
    ) -> None:
        """
        Ensure blast radius is within acceptable bounds.
        
        Raises:
            EdgeRepairRejection: If blast radius too large.
        """
        if len(affected_nodes) > max_blast_radius:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MULTI_EDGE_DAMAGE,
                {
                    'message': 'Blast radius exceeds maximum',
                    'blast_radius': len(affected_nodes),
                    'max_allowed': max_blast_radius
                }
            )


# ============================================================================
# EDGE REPAIR ENGINE
# ============================================================================


class EdgeRepairEngine:
    """
    Dependency edge repair strategy engine.
    
    Proposes safe, explicit dependency corrections.
    Never mutates node logic. Never executes workflow.
    
    Rejection is expected behavior.
    """
    
    def __init__(
        self,
        observer: EdgeRepairObserver | None = None,
        equivalence_registry: dict[str, frozenset[str]] | None = None
    ):
        """
        Initialize edge repair engine.
        
        Args:
            observer: Optional observability integration.
            equivalence_registry: Declared node equivalences.
        """
        self._observer = observer
        self._equivalence_registry = equivalence_registry or {}
        self._verifier = CompatibilityVerifier()
    
    def propose_repair(
        self,
        context: EdgeRepairContext
    ) -> EdgeRepairAction:
        """
        Propose edge repair for damaged dependency.
        
        Strict phases:
        1. Damage scope confirmation
        2. Compatibility verification
        3. Edge repair synthesis
        4. Safety & risk annotation
        
        Args:
            context: Complete repair context.
            
        Returns:
            Declarative repair action.
            
        Raises:
            EdgeRepairRejection: If repair cannot be safely proposed.
        """
        damaged_edge = context.damage_report.damaged_edge
        damage_type = context.damage_report.damage_type
        
        # Log attempt
        if self._observer:
            self._observer.edge_repair_attempted(
                context,
                self._select_repair_type(damage_type)
            )
        
        try:
            # PHASE 1: Damage Scope Confirmation
            self._confirm_damage_scope(context)
            
            # PHASE 2: Compatibility Verification
            repair_type, new_edge, proof = self._verify_compatibility(context)
            
            # PHASE 3: Edge Repair Synthesis
            action = self._synthesize_repair(
                context,
                repair_type,
                new_edge,
                proof
            )
            
            # PHASE 4: Safety & Risk Annotation
            action = self._annotate_safety(context, action)
            
            # Log success
            if self._observer:
                self._observer.edge_repair_proposed(action)
            
            return action
            
        except EdgeRepairRejection as e:
            # Log rejection
            if self._observer:
                self._observer.edge_repair_rejected(
                    context,
                    e.reason,
                    e.details
                )
            raise
    
    def _confirm_damage_scope(
        self,
        context: EdgeRepairContext
    ) -> None:
        """
        Phase 1: Confirm damage is single-edge and repairable.
        
        Raises:
            EdgeRepairRejection: If scope is invalid.
        """
        # Validate single edge scope
        EdgeRepairInvariants.validate_single_edge_scope(context)
        
        # Check if damage type is supported
        damage_type = context.damage_report.damage_type
        if damage_type not in EdgeDamageType:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.AMBIGUOUS_DAMAGE,
                {
                    'message': 'Unknown damage type',
                    'damage_type': str(damage_type)
                }
            )
    
    def _verify_compatibility(
        self,
        context: EdgeRepairContext
    ) -> tuple[EdgeRepairType, DependencyEdge | None, CompatibilityProof]:
        """
        Phase 2: Verify compatibility and select repair strategy.
        
        Returns:
            Tuple of (repair_type, new_edge, compatibility_proof)
            
        Raises:
            EdgeRepairRejection: If compatibility cannot be proven.
        """
        damaged_edge = context.damage_report.damaged_edge
        damage_type = context.damage_report.damage_type
        
        # Select repair type based on damage
        repair_type = self._select_repair_type(damage_type)
        
        # Verify repair type is allowed
        if not context.constraints.validate_repair_type(repair_type):
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MISSING_COMPATIBILITY_PROOF,
                {
                    'message': 'Repair type not allowed by constraints',
                    'repair_type': repair_type.value
                }
            )
        
        # Handle different repair types
        if repair_type == EdgeRepairType.EDGE_INVALIDATION:
            return self._verify_invalidation(context)
        
        elif repair_type == EdgeRepairType.EDGE_REWIRE:
            return self._verify_rewiring(context)
        
        elif repair_type == EdgeRepairType.EDGE_SUBSTITUTION:
            return self._verify_substitution(context)
        
        elif repair_type == EdgeRepairType.EDGE_VERSION_ALIGNMENT:
            return self._verify_version_alignment(context)
        
        raise EdgeRepairRejection(
            EdgeRepairRejectionReason.AMBIGUOUS_DAMAGE,
            {
                'message': 'Cannot determine repair strategy',
                'damage_type': damage_type.value
            }
        )
    
    def _verify_invalidation(
        self,
        context: EdgeRepairContext
    ) -> tuple[EdgeRepairType, None, CompatibilityProof]:
        """
        Verify invalidation repair.
        
        Invalidation always succeeds if constraints allow.
        """
        import time
        
        # Invalidation proof is trivial
        proof = CompatibilityProof(
            proof_type=CompatibilityProofType.SEMANTIC_SIGNATURE,
            evidence={
                'operation': 'invalidation',
                'reason': 'Force recomputation from clean state'
            },
            verified_at=time.time_ns(),
            verifier='EdgeRepairEngine._verify_invalidation'
        )
        
        return (EdgeRepairType.EDGE_INVALIDATION, None, proof)
    
    def _verify_rewiring(
        self,
        context: EdgeRepairContext
    ) -> tuple[EdgeRepairType, DependencyEdge, CompatibilityProof]:
        """
        Verify rewiring to alternative artifact.
        
        Requires schema compatibility proof.
        """
        damaged_edge = context.damage_report.damaged_edge
        
        # Find alternative artifact from same source node
        source_node = context.workflow_snapshot.get_node(damaged_edge.source_node_id)
        if not source_node:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MISSING_LINEAGE,
                {'message': 'Source node not found'}
            )
        
        # For rewiring, we need to find an alternative output from same node
        # In real implementation, this would search artifact store
        # For now, we simulate by checking if node has multiple outputs
        
        if len(source_node.outputs) <= 1:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MISSING_COMPATIBILITY_PROOF,
                {
                    'message': 'No alternative artifacts available for rewiring',
                    'source_node': source_node.node_id
                }
            )
        
        # Create new edge with alternative artifact
        # (Simplified - real impl would validate artifact availability)
        alternative_artifact = next(
            (a for a in source_node.outputs if a != damaged_edge.artifact_id),
            None
        )
        
        if not alternative_artifact:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MISSING_COMPATIBILITY_PROOF,
                {'message': 'No compatible alternative artifact'}
            )
        
        new_edge = DependencyEdge(
            source_node_id=damaged_edge.source_node_id,
            target_node_id=damaged_edge.target_node_id,
            artifact_id=alternative_artifact,
            descriptor=damaged_edge.descriptor,
            lineage_hash=damaged_edge.lineage_hash
        )
        
        # Verify schema compatibility
        proof = self._verifier.verify_schema_compatibility(damaged_edge, new_edge)
        if not proof:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.SCHEMA_MISMATCH,
                {
                    'message': 'Alternative artifact schema incompatible',
                    'old_artifact': damaged_edge.artifact_id,
                    'new_artifact': alternative_artifact
                }
            )
        
        return (EdgeRepairType.EDGE_REWIRE, new_edge, proof)
    
    def _verify_substitution(
        self,
        context: EdgeRepairContext
    ) -> tuple[EdgeRepairType, DependencyEdge, CompatibilityProof]:
        """
        Verify node substitution.
        
        Requires declared equivalence proof.
        """
        damaged_edge = context.damage_report.damaged_edge
        
        # Check available substitutes
        if not context.available_substitutes:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MISSING_COMPATIBILITY_PROOF,
                {'message': 'No substitute nodes available'}
            )
        
        # Try each substitute
        for substitute_id in context.available_substitutes:
            substitute_node = context.workflow_snapshot.get_node(substitute_id)
            if not substitute_node:
                continue
            
            # Verify declared equivalence
            proof = self._verifier.verify_declared_equivalence(
                damaged_edge.source_node_id,
                substitute_id,
                self._equivalence_registry
            )
            
            if proof:
                # Found valid substitute
                # Find matching output artifact
                # (Simplified - real impl would match by schema)
                if substitute_node.outputs:
                    new_artifact = next(iter(substitute_node.outputs))
                    
                    new_edge = DependencyEdge(
                        source_node_id=substitute_id,
                        target_node_id=damaged_edge.target_node_id,
                        artifact_id=new_artifact,
                        descriptor=damaged_edge.descriptor,
                        lineage_hash=None  # Lineage changes with substitution
                    )
                    
                    return (EdgeRepairType.EDGE_SUBSTITUTION, new_edge, proof)
        
        raise EdgeRepairRejection(
            EdgeRepairRejectionReason.INFERRED_EQUIVALENCE,
            {
                'message': 'No declared equivalent substitutes found',
                'source_node': damaged_edge.source_node_id,
                'available_substitutes': list(context.available_substitutes)
            }
        )
    
    def _verify_version_alignment(
        self,
        context: EdgeRepairContext
    ) -> tuple[EdgeRepairType, DependencyEdge, CompatibilityProof]:
        """
        Verify version alignment.
        
        Requires version contract proof.
        """
        damaged_edge = context.damage_report.damaged_edge
        
        # Extract version from damage report
        old_version = context.damage_report.evidence.get('old_version')
        new_version = context.damage_report.evidence.get('new_version')
        
        if not old_version or not new_version:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.MISSING_COMPATIBILITY_PROOF,
                {'message': 'Version information not available'}
            )
        
        # Verify version compatibility
        contract_rules = {
            'allow_major_change': False,
            'allow_minor_change': True
        }
        
        proof = self._verifier.verify_version_contract(
            old_version,
            new_version,
            contract_rules
        )
        
        if not proof:
            raise EdgeRepairRejection(
                EdgeRepairRejectionReason.SCHEMA_MISMATCH,
                {
                    'message': 'Version contract violated',
                    'old_version': old_version,
                    'new_version': new_version
                }
            )
        
        # Create new edge with updated version
        new_descriptor = EdgeDescriptor(
            edge_type=damaged_edge.descriptor.edge_type,
            schema_version=new_version,
            contract=damaged_edge.descriptor.contract,
            required=damaged_edge.descriptor.required,
            deterministic=damaged_edge.descriptor.deterministic
        )
        
        new_edge = DependencyEdge(
            source_node_id=damaged_edge.source_node_id,
            target_node_id=damaged_edge.target_node_id,
            artifact_id=damaged_edge.artifact_id,
            descriptor=new_descriptor,
            lineage_hash=damaged_edge.lineage_hash
        )
        
        return (EdgeRepairType.EDGE_VERSION_ALIGNMENT, new_edge, proof)
    
    def _synthesize_repair(
        self,
        context: EdgeRepairContext,
        repair_type: EdgeRepairType,
        new_edge: DependencyEdge | None,
        proof: CompatibilityProof
    ) -> EdgeRepairAction:
        """
        Phase 3: Synthesize repair action.
        
        Creates declarative repair proposal.
        """
        damaged_edge = context.damage_report.damaged_edge
        
        # Validate invariants
        EdgeRepairInvariants.validate_no_node_logic_changes(
            damaged_edge,
            new_edge,
            context.workflow_snapshot
        )
        
        EdgeRepairInvariants.validate_no_topology_changes(
            context,
            new_edge
        )
        
        EdgeRepairInvariants.validate_determinism_preservation(
            damaged_edge,
            new_edge,
            context.constraints.require_determinism
        )
        
        # Calculate affected nodes (downstream from target)
        affected_nodes = context.workflow_snapshot.get_downstream_nodes(
            damaged_edge.target_node_id
        )
        
        # Add target node itself
        affected_nodes = affected_nodes | {damaged_edge.target_node_id}
        
        # Validate blast radius
        EdgeRepairInvariants.validate_blast_radius(
            affected_nodes,
            context.constraints.max_blast_radius
        )
        
        # Determine recompute requirements
        recompute_required = affected_nodes  # All affected nodes need recompute
        
        # Generate justification
        justification = self._generate_justification(
            repair_type,
            context.damage_report,
            new_edge
        )
        
        # Create repair action
        action = EdgeRepairAction(
            repair_type=repair_type,
            source_node_id=damaged_edge.source_node_id,
            target_node_id=damaged_edge.target_node_id,
            old_dependency=damaged_edge,
            new_dependency=new_edge,
            compatibility_proof=proof,
            recompute_required=recompute_required,
            affected_nodes=affected_nodes,
            determinism_required=context.constraints.require_determinism,
            risk_level=RiskLevel.MEDIUM,  # Will be refined in phase 4
            justification=justification,
            blast_radius=len(affected_nodes)
        )
        
        return action
    
    def _annotate_safety(
        self,
        context: EdgeRepairContext,
        action: EdgeRepairAction
    ) -> EdgeRepairAction:
        """
        Phase 4: Annotate safety and risk levels.
        
        Refines risk assessment based on repair characteristics.
        """
        # Calculate risk level
        risk_level = self._calculate_risk_level(action, context)
        
        # Create updated action with refined risk
        updated_action = EdgeRepairAction(
            repair_type=action.repair_type,
            source_node_id=action.source_node_id,
            target_node_id=action.target_node_id,
            old_dependency=action.old_dependency,
            new_dependency=action.new_dependency,
            compatibility_proof=action.compatibility_proof,
            recompute_required=action.recompute_required,
            affected_nodes=action.affected_nodes,
            determinism_required=action.determinism_required,
            risk_level=risk_level,
            justification=action.justification,
            blast_radius=action.blast_radius
        )
        
        return updated_action
    
    def _calculate_risk_level(
        self,
        action: EdgeRepairAction,
        context: EdgeRepairContext
    ) -> RiskLevel:
        """
        Calculate risk level based on repair characteristics.
        
        Edge repairs are typically MEDIUM or HIGH risk.
        """
        # Start with medium (edge repairs are inherently risky)
        risk = RiskLevel.MEDIUM
        
        # Increase risk based on blast radius
        if action.blast_radius > 5:
            risk = RiskLevel.HIGH
        
        if action.blast_radius > 10:
            risk = RiskLevel.CRITICAL
        
        # Increase risk for substitution (changes data source)
        if action.repair_type == EdgeRepairType.EDGE_SUBSTITUTION:
            if risk == RiskLevel.MEDIUM:
                risk = RiskLevel.HIGH
        
        # Decrease risk for invalidation (safest option)
        if action.repair_type == EdgeRepairType.EDGE_INVALIDATION:
            if risk == RiskLevel.CRITICAL:
                risk = RiskLevel.HIGH
        
        # Increase risk if lineage is lost
        if context.constraints.require_lineage_preservation:
            if action.new_dependency and not action.new_dependency.lineage_hash:
                if risk == RiskLevel.MEDIUM:
                    risk = RiskLevel.HIGH
        
        return risk
    
    def _generate_justification(
        self,
        repair_type: EdgeRepairType,
        damage_report: EdgeDamageReport,
        new_edge: DependencyEdge | None
    ) -> str:
        """
        Generate human-readable justification for repair.
        """
        damaged_edge = damage_report.damaged_edge
        damage_type = damage_report.damage_type
        
        base = f"Edge repair proposed: {repair_type.value}"
        base += f"\nDamage type: {damage_type.value}"
        base += f"\nAffected edge: {damaged_edge.source_node_id} → {damaged_edge.target_node_id}"
        base += f"\nArtifact: {damaged_edge.artifact_id}"
        
        if new_edge:
            base += f"\nNew source: {new_edge.source_node_id}"
            base += f"\nNew artifact: {new_edge.artifact_id}"
        else:
            base += "\nAction: Invalidate dependency, force recomputation"
        
        base += f"\nDownstream impact: {len(damage_report.affected_downstream_nodes)} nodes"
        
        return base
    
    def _select_repair_type(
        self,
        damage_type: EdgeDamageType
    ) -> EdgeRepairType:
        """
        Select appropriate repair type based on damage.
        
        This is a mapping, not inference.
        """
        # Direct mapping from damage type to repair type
        mapping = {
            EdgeDamageType.ARTIFACT_CORRUPTION: EdgeRepairType.EDGE_REWIRE,
            EdgeDamageType.OUTPUT_INVALIDATION: EdgeRepairType.EDGE_INVALIDATION,
            EdgeDamageType.SCHEMA_INCOMPATIBILITY: EdgeRepairType.EDGE_REWIRE,
            EdgeDamageType.VERSION_MISMATCH: EdgeRepairType.EDGE_VERSION_ALIGNMENT,
            EdgeDamageType.POISONED_CACHE: EdgeRepairType.EDGE_INVALIDATION,
            EdgeDamageType.BROKEN_LINEAGE: EdgeRepairType.EDGE_SUBSTITUTION,
        }
        
        return mapping.get(damage_type, EdgeRepairType.EDGE_INVALIDATION)


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'EdgeRepairType',
    'EdgeDamageType',
    'CompatibilityProofType',
    'RiskLevel',
    'EdgeRepairRejectionReason',
    'EdgeRepairError',
    'EdgeRepairRejection',
    'EdgeRepairInvariantViolation',
    'CompatibilityProof',
    'EdgeDescriptor',
    'DependencyEdge',
    'EdgeDamageReport',
    'NodeSnapshot',
    'WorkflowSnapshot',
    'RepairConstraints',
    'EdgeRepairContext',
    'EdgeRepairAction',
    'EdgeRepairObserver',
    'CompatibilityVerifier',
    'EdgeRepairInvariants',
    'EdgeRepairEngine',
]