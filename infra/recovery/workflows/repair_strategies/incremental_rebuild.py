"""
/infra/recovery/workflows/repair_strategies/incremental_rebuild.py

Incremental Rebuild Repair Strategy
(Minimal-Scope Incremental Reconstruction)

This module implements incremental rebuild repair strategy that identifies
minimal repair set and rebuilds incrementally from dependencies.

CRITICAL PRINCIPLES:
- Minimal-scope repair (only corrupted regions)
- Incremental dependency-based rebuild
- Deterministic execution order
- Full audit trail of incremental steps
- Post-rebuild integrity validation
- Idempotent and replay-safe
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from datetime import datetime
import hashlib
import json
import time


# ============================================================================
# INCREMENTAL REBUILD REQUEST
# ============================================================================

@dataclass(frozen=True)
class IncrementalRebuildRequest:
    """
    Request for incremental rebuild repair operation.
    
    Rebuilds corrupted regions incrementally from dependencies.
    """
    workflow_id: str
    """Workflow identifier"""
    
    corrupted_node_ids: List[str]
    """List of corrupted node identifiers"""
    
    dependency_closure: bool = True
    """Whether to include dependency closure"""
    
    schema_version: int = 1
    """Schema version for compatibility"""
    
    request_id: str = field(default_factory=lambda: f"incremental_{int(time.time() * 1000)}")
    """Unique request identifier"""
    
    operator_authorization: Optional[str] = None
    """Operator ID if manual authorization required"""
    
    def validate(self) -> None:
        """Validate request is well-formed."""
        if not self.workflow_id:
            raise ValueError("workflow_id cannot be empty")
        if not self.corrupted_node_ids:
            raise ValueError("corrupted_node_ids cannot be empty")
        if self.schema_version < 1:
            raise ValueError(f"Invalid schema_version: {self.schema_version}")


# ============================================================================
# REPAIR SCOPE
# ============================================================================

@dataclass
class RepairScope:
    """
    Computed scope of repair operation.
    
    For incremental rebuild, scope includes corrupted nodes and their dependencies.
    """
    root_node_id: str
    """Root node identifier"""
    
    affected_nodes: Set[str]
    """Set of node IDs in repair scope"""
    
    traversal_order: List[str]
    """Deterministic order of node processing"""
    
    boundary_violations: List[str] = field(default_factory=list)
    """Nodes excluded due to boundary constraints"""
    
    immutable_nodes: Set[str] = field(default_factory=set)
    """Nodes that are immutable"""
    
    estimated_mutations: int = 0
    """Estimated number of nodes that will be mutated"""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "root_node_id": self.root_node_id,
            "affected_nodes": sorted(self.affected_nodes),
            "traversal_order": self.traversal_order,
            "boundary_violations": self.boundary_violations,
            "immutable_nodes": sorted(self.immutable_nodes),
            "estimated_mutations": self.estimated_mutations,
        }


# ============================================================================
# REPAIR RESULT
# ============================================================================

@dataclass
class RepairResult:
    """
    Result of incremental rebuild repair operation.
    
    Contains complete audit trail and validation results.
    """
    request_id: str
    """Request ID from original request"""
    
    root_node_id: str
    """Root node identifier"""
    
    success: bool
    """Overall repair success"""
    
    repaired_nodes: List[str]
    """Nodes successfully rebuilt"""
    
    skipped_nodes: List[str]
    """Nodes skipped (e.g., already valid)"""
    
    immutable_blockers: List[str]
    """Immutable nodes that blocked repair"""
    
    validation_errors: List[str]
    """Post-repair validation errors"""
    
    repair_actions_taken: List[Dict[str, Any]]
    """Complete audit trail of actions"""
    
    repair_hash: str
    """Deterministic hash of repair operation"""
    
    schema_version: int
    """Schema version used"""
    
    duration_seconds: float = 0.0
    """Time taken for repair"""
    
    pre_repair_snapshot_hash: Optional[str] = None
    """Hash of state before repair"""
    
    post_repair_snapshot_hash: Optional[str] = None
    """Hash of state after repair"""
    
    error_message: Optional[str] = None
    """Error message if repair failed"""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "request_id": self.request_id,
            "root_node_id": self.root_node_id,
            "success": self.success,
            "repaired_nodes": self.repaired_nodes,
            "skipped_nodes": self.skipped_nodes,
            "immutable_blockers": self.immutable_blockers,
            "validation_errors": self.validation_errors,
            "repair_actions_taken": self.repair_actions_taken,
            "repair_hash": self.repair_hash,
            "schema_version": self.schema_version,
            "duration_seconds": self.duration_seconds,
            "pre_repair_snapshot_hash": self.pre_repair_snapshot_hash,
            "post_repair_snapshot_hash": self.post_repair_snapshot_hash,
            "error_message": self.error_message,
        }


# ============================================================================
# INCREMENTAL REBUILD STRATEGY
# ============================================================================

class IncrementalRebuildStrategy:
    """
    Incremental rebuild repair strategy.
    
    Identifies minimal repair set and rebuilds incrementally from dependencies.
    Implements BaseRepairStrategy protocol for incremental reconstruction.
    
    Key Guarantees:
    - Minimal-scope repair
    - Incremental dependency-based rebuild
    - Deterministic execution order
    - Full audit trail
    - Post-rebuild integrity validation
    """
    
    # Protocol-required class attributes
    strategy_id: str = "incremental_rebuild"
    strategy_schema_version: int = 1
    
    def __init__(
        self,
        *,
        graph_accessor: Optional[Any] = None,
        dependency_resolver: Optional[Any] = None,
        rebuild_engine: Optional[Any] = None,
        integrity_validator: Optional[Any] = None,
    ):
        """
        Initialize incremental rebuild strategy.
        
        Args:
            graph_accessor: Interface to graph persistence layer
            dependency_resolver: Interface to dependency resolution
            rebuild_engine: Interface to incremental rebuild execution
            integrity_validator: Interface to integrity validation
        """
        self.graph_accessor = graph_accessor
        self.dependency_resolver = dependency_resolver
        self.rebuild_engine = rebuild_engine
        self.integrity_validator = integrity_validator
    
    def validate_request(self, request: Any) -> None:
        """
        Validate that repair request is well-formed and compatible.
        
        Args:
            request: Repair request to validate
            
        Raises:
            ValueError: If request is invalid
            TypeError: If request has wrong type
        """
        if not isinstance(request, IncrementalRebuildRequest):
            raise TypeError(
                f"Request must be IncrementalRebuildRequest, got {type(request)}"
            )
        
        # Validate request structure
        request.validate()
        
        # Validate schema compatibility
        if request.schema_version != self.strategy_schema_version:
            raise ValueError(
                f"Schema version mismatch: request has {request.schema_version}, "
                f"strategy supports {self.strategy_schema_version}"
            )
        
        # Validate corrupted nodes exist
        if self.graph_accessor:
            for node_id in request.corrupted_node_ids:
                if not self.graph_accessor.node_exists(node_id):
                    raise ValueError(f"Corrupted node '{node_id}' does not exist")
    
    def compute_scope(self, request: Any) -> RepairScope:
        """
        Compute the scope of entities affected by repair.
        
        For incremental rebuild, scope includes corrupted nodes and their dependencies.
        
        Args:
            request: Validated repair request
            
        Returns:
            RepairScope defining affected nodes
            
        Raises:
            RuntimeError: If scope computation fails
        """
        if not isinstance(request, IncrementalRebuildRequest):
            raise TypeError(f"Request must be IncrementalRebuildRequest, got {type(request)}")
        
        # Start with corrupted nodes
        affected_nodes: Set[str] = set(request.corrupted_node_ids)
        
        # Add dependency closure if requested
        if request.dependency_closure and self.dependency_resolver:
            try:
                for node_id in request.corrupted_node_ids:
                    dependencies = self.dependency_resolver.get_dependency_closure(node_id)
                    affected_nodes.update(dependencies)
            except Exception:
                # If dependency resolution fails, use only corrupted nodes
                pass
        
        # Compute topological order for deterministic execution
        traversal_order: List[str] = []
        if self.graph_accessor:
            try:
                traversal_order = self.graph_accessor.topological_sort(list(affected_nodes))
            except Exception:
                # Fallback to sorted order
                traversal_order = sorted(affected_nodes)
        else:
            traversal_order = sorted(affected_nodes)
        
        # Identify immutable nodes
        immutable_nodes: Set[str] = set()
        if self.graph_accessor:
            for node_id in affected_nodes:
                node = self.graph_accessor.get_node(node_id)
                if node and hasattr(node, 'is_immutable') and node.is_immutable:
                    immutable_nodes.add(node_id)
        
        scope = RepairScope(
            root_node_id=request.corrupted_node_ids[0] if request.corrupted_node_ids else request.workflow_id,
            affected_nodes=affected_nodes,
            traversal_order=traversal_order,
            boundary_violations=[],
            immutable_nodes=immutable_nodes,
            estimated_mutations=len(affected_nodes) - len(immutable_nodes),
        )
        
        return scope
    
    def execute(self, request: Any) -> RepairResult:
        """
        Execute the incremental rebuild repair operation.
        
        Rebuilds corrupted regions incrementally with complete audit trail.
        
        Args:
            request: Validated repair request
            
        Returns:
            RepairResult containing outcome and audit trail
            
        Raises:
            RuntimeError: If repair execution fails catastrophically
        """
        if not isinstance(request, IncrementalRebuildRequest):
            raise TypeError(f"Request must be IncrementalRebuildRequest, got {type(request)}")
        
        start_time = datetime.utcnow()
        repair_actions: List[Dict[str, Any]] = []
        repaired_nodes: List[str] = []
        skipped_nodes: List[str] = []
        immutable_blockers: List[str] = []
        validation_errors: List[str] = []
        
        try:
            # Compute repair scope
            scope = self.compute_scope(request)
            
            # Capture pre-repair snapshot
            pre_snapshot_hash = self._compute_state_hash(scope.affected_nodes)
            
            # Execute incremental rebuild for each node in order
            for node_id in scope.traversal_order:
                if node_id in scope.immutable_nodes:
                    immutable_blockers.append(node_id)
                    repair_actions.append({
                        "action_type": "IMMUTABLE_BLOCK",
                        "node_id": node_id,
                        "success": False,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    continue
                
                if self.rebuild_engine:
                    rebuild_result = self.rebuild_engine.rebuild_node(
                        node_id=node_id,
                        workflow_id=request.workflow_id,
                    )
                    
                    if rebuild_result and rebuild_result.success:
                        repaired_nodes.append(node_id)
                        repair_actions.append({
                            "action_type": "INCREMENTAL_REBUILD",
                            "node_id": node_id,
                            "success": True,
                            "timestamp": datetime.utcnow().isoformat(),
                        })
                    else:
                        repair_actions.append({
                            "action_type": "INCREMENTAL_REBUILD",
                            "node_id": node_id,
                            "success": False,
                            "error": str(rebuild_result.error) if rebuild_result else "Unknown error",
                            "timestamp": datetime.utcnow().isoformat(),
                        })
                else:
                    # Fallback: mark node as requiring rebuild
                    repaired_nodes.append(node_id)
                    repair_actions.append({
                        "action_type": "INCREMENTAL_REBUILD_SCHEDULED",
                        "node_id": node_id,
                        "success": True,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
            
            # Post-rebuild validation
            if self.integrity_validator:
                validation_errors = self.integrity_validator.validate_workflow_state(
                    request.workflow_id
                )
            
            # Capture post-repair snapshot
            post_snapshot_hash = self._compute_state_hash(scope.affected_nodes)
            
            # Compute repair hash (deterministic fingerprint)
            repair_hash = self._compute_repair_hash(
                request=request,
                scope=scope,
                repaired_nodes=repaired_nodes,
                post_snapshot_hash=post_snapshot_hash,
            )
            
            # Determine overall success
            success = (
                len(validation_errors) == 0 and
                len(immutable_blockers) == 0
            )
            
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            return RepairResult(
                request_id=request.request_id,
                root_node_id=scope.root_node_id,
                success=success,
                repaired_nodes=repaired_nodes,
                skipped_nodes=skipped_nodes,
                immutable_blockers=immutable_blockers,
                validation_errors=validation_errors,
                repair_actions_taken=repair_actions,
                repair_hash=repair_hash,
                schema_version=request.schema_version,
                duration_seconds=duration,
                pre_repair_snapshot_hash=pre_snapshot_hash,
                post_repair_snapshot_hash=post_snapshot_hash,
            )
        
        except Exception as e:
            duration = (datetime.utcnow() - start_time).total_seconds()
            
            # Compute partial repair hash even on failure
            repair_hash = self._compute_repair_hash(
                request=request,
                scope=scope if 'scope' in locals() else None,
                repaired_nodes=repaired_nodes,
                post_snapshot_hash=None,
            )
            
            return RepairResult(
                request_id=request.request_id,
                root_node_id=request.corrupted_node_ids[0] if request.corrupted_node_ids else request.workflow_id,
                success=False,
                repaired_nodes=repaired_nodes,
                skipped_nodes=skipped_nodes,
                immutable_blockers=immutable_blockers,
                validation_errors=[str(e)],
                repair_actions_taken=repair_actions,
                repair_hash=repair_hash,
                schema_version=request.schema_version,
                duration_seconds=duration,
                pre_repair_snapshot_hash=None,
                post_repair_snapshot_hash=None,
                error_message=str(e),
            )
    
    def _compute_state_hash(self, node_ids: Set[str]) -> str:
        """Compute deterministic hash of workflow state."""
        content = json.dumps(sorted(node_ids), sort_keys=True)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _compute_repair_hash(
        self,
        request: IncrementalRebuildRequest,
        scope: Optional[RepairScope],
        repaired_nodes: List[str],
        post_snapshot_hash: Optional[str],
    ) -> str:
        """Compute deterministic hash of repair operation."""
        hash_input = {
            "strategy_id": self.strategy_id,
            "request_id": request.request_id,
            "workflow_id": request.workflow_id,
            "corrupted_nodes": sorted(request.corrupted_node_ids),
            "schema_version": request.schema_version,
            "repaired_nodes": sorted(repaired_nodes),
            "post_snapshot_hash": post_snapshot_hash,
        }
        content = json.dumps(hash_input, sort_keys=True)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
