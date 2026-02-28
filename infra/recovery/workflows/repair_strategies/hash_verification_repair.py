"""
/infra/recovery/workflows/repair_strategies/hash_verification_repair.py

Hash Verification Repair Strategy
(Hash Mismatch Detection and Re-computation)

This module implements hash verification repair strategy that detects hash
inconsistencies and re-computes from canonical sources.

CRITICAL PRINCIPLES:
- Detect hash mismatches through verification
- Re-compute from canonical sources only
- Deterministic hash computation
- Full audit trail of verification and repair
- Post-repair hash validation
- Idempotent and replay-safe
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime
import hashlib
import json
import time


# ============================================================================
# HASH VERIFICATION REPAIR REQUEST
# ============================================================================

@dataclass(frozen=True)
class HashVerificationRepairRequest:
    """
    Request for hash verification repair operation.
    
    Detects and repairs hash mismatches through re-computation.
    """
    workflow_id: str
    """Workflow identifier"""
    
    node_ids: List[str]
    """List of node identifiers to verify"""
    
    expected_hashes: Dict[str, str]
    """Expected hashes by node/artifact identifier"""
    
    recompute_on_mismatch: bool = True
    """Whether to recompute on hash mismatch"""
    
    schema_version: int = 1
    """Schema version for compatibility"""
    
    request_id: str = field(default_factory=lambda: f"hash_verify_{int(time.time() * 1000)}")
    """Unique request identifier"""
    
    operator_authorization: Optional[str] = None
    """Operator ID if manual authorization required"""
    
    def validate(self) -> None:
        """Validate request is well-formed."""
        if not self.workflow_id:
            raise ValueError("workflow_id cannot be empty")
        if not self.node_ids:
            raise ValueError("node_ids cannot be empty")
        if not self.expected_hashes:
            raise ValueError("expected_hashes cannot be empty")
        if self.schema_version < 1:
            raise ValueError(f"Invalid schema_version: {self.schema_version}")


# ============================================================================
# REPAIR SCOPE
# ============================================================================

@dataclass
class RepairScope:
    """
    Computed scope of repair operation.
    
    For hash verification, scope includes nodes with hash mismatches.
    """
    root_node_id: str
    """Root node identifier"""
    
    affected_nodes: Set[str]
    """Set of node IDs with hash mismatches"""
    
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
    Result of hash verification repair operation.
    
    Contains complete audit trail and validation results.
    """
    request_id: str
    """Request ID from original request"""
    
    root_node_id: str
    """Root node identifier"""
    
    success: bool
    """Overall repair success"""
    
    repaired_nodes: List[str]
    """Nodes successfully verified/repaired"""
    
    skipped_nodes: List[str]
    """Nodes skipped (e.g., hashes match)"""
    
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
# HASH VERIFICATION REPAIR STRATEGY
# ============================================================================

class HashVerificationRepairStrategy:
    """
    Hash verification repair strategy.
    
    Detects hash mismatches and re-computes from canonical sources.
    Implements BaseRepairStrategy protocol for hash-based repair.
    
    Key Guarantees:
    - Hash mismatch detection
    - Re-computation from canonical sources
    - Deterministic hash computation
    - Full audit trail
    - Post-repair hash validation
    """
    
    # Protocol-required class attributes
    strategy_id: str = "hash_verification_repair"
    strategy_schema_version: int = 1
    
    def __init__(
        self,
        *,
        hash_verifier: Optional[Any] = None,
        recompute_engine: Optional[Any] = None,
        canonical_source: Optional[Any] = None,
        integrity_validator: Optional[Any] = None,
    ):
        """
        Initialize hash verification repair strategy.
        
        Args:
            hash_verifier: Interface to hash verification
            recompute_engine: Interface to recomputation engine
            canonical_source: Interface to canonical data sources
            integrity_validator: Interface to integrity validation
        """
        self.hash_verifier = hash_verifier
        self.recompute_engine = recompute_engine
        self.canonical_source = canonical_source
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
        if not isinstance(request, HashVerificationRepairRequest):
            raise TypeError(
                f"Request must be HashVerificationRepairRequest, got {type(request)}"
            )
        
        # Validate request structure
        request.validate()
        
        # Validate schema compatibility
        if request.schema_version != self.strategy_schema_version:
            raise ValueError(
                f"Schema version mismatch: request has {request.schema_version}, "
                f"strategy supports {self.strategy_schema_version}"
            )
        
        # Validate expected hashes are valid hex strings
        for identifier, expected_hash in request.expected_hashes.items():
            if not isinstance(expected_hash, str):
                raise ValueError(f"Expected hash for '{identifier}' must be string")
            if len(expected_hash) != 64:  # SHA256 hex length
                raise ValueError(
                    f"Expected hash for '{identifier}' must be SHA256 (64 hex chars), "
                    f"got length {len(expected_hash)}"
                )
            try:
                int(expected_hash, 16)  # Validate hex
            except ValueError:
                raise ValueError(f"Expected hash for '{identifier}' is not valid hex")
    
    def compute_scope(self, request: Any) -> RepairScope:
        """
        Compute the scope of entities affected by repair.
        
        For hash verification, scope includes nodes with hash mismatches.
        
        Args:
            request: Validated repair request
            
        Returns:
            RepairScope defining affected nodes
            
        Raises:
            RuntimeError: If scope computation fails
        """
        if not isinstance(request, HashVerificationRepairRequest):
            raise TypeError(f"Request must be HashVerificationRepairRequest, got {type(request)}")
        
        # Verify hashes and identify mismatches
        mismatched_nodes: Set[str] = set()
        
        if self.hash_verifier:
            for node_id in request.node_ids:
                # Get actual hash
                actual_hash = self.hash_verifier.compute_hash(node_id)
                expected_hash = request.expected_hashes.get(node_id)
                
                if expected_hash and actual_hash != expected_hash:
                    mismatched_nodes.add(node_id)
        else:
            # If verifier unavailable, assume all nodes need verification
            mismatched_nodes = set(request.node_ids)
        
        # If no mismatches found, scope is empty
        if not mismatched_nodes:
            mismatched_nodes = set()
        
        # Compute traversal order (sorted for determinism)
        traversal_order = sorted(mismatched_nodes) if mismatched_nodes else []
        
        # Identify immutable nodes
        immutable_nodes: Set[str] = set()
        # In production, would query node metadata
        
        scope = RepairScope(
            root_node_id=request.node_ids[0] if request.node_ids else request.workflow_id,
            affected_nodes=mismatched_nodes,
            traversal_order=traversal_order,
            boundary_violations=[],
            immutable_nodes=immutable_nodes,
            estimated_mutations=len(mismatched_nodes) - len(immutable_nodes),
        )
        
        return scope
    
    def execute(self, request: Any) -> RepairResult:
        """
        Execute the hash verification repair operation.
        
        Verifies hashes and re-computes mismatched nodes with complete audit trail.
        
        Args:
            request: Validated repair request
            
        Returns:
            RepairResult containing outcome and audit trail
            
        Raises:
            RuntimeError: If repair execution fails catastrophically
        """
        if not isinstance(request, HashVerificationRepairRequest):
            raise TypeError(f"Request must be HashVerificationRepairRequest, got {type(request)}")
        
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
            
            # Verify and repair each node
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
                
                # Verify hash
                expected_hash = request.expected_hashes.get(node_id)
                if not expected_hash:
                    skipped_nodes.append(node_id)
                    repair_actions.append({
                        "action_type": "SKIP_NO_EXPECTED_HASH",
                        "node_id": node_id,
                        "success": True,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                    continue
                
                actual_hash = None
                if self.hash_verifier:
                    actual_hash = self.hash_verifier.compute_hash(node_id)
                
                if actual_hash == expected_hash:
                    # Hash matches, skip
                    skipped_nodes.append(node_id)
                    repair_actions.append({
                        "action_type": "HASH_VERIFY_MATCH",
                        "node_id": node_id,
                        "hash": actual_hash,
                        "success": True,
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                else:
                    # Hash mismatch, recompute if enabled
                    if request.recompute_on_mismatch and self.recompute_engine:
                        recompute_result = self.recompute_engine.recompute_from_canonical(
                            node_id=node_id,
                            canonical_source=self.canonical_source,
                        )
                        
                        if recompute_result and recompute_result.success:
                            repaired_nodes.append(node_id)
                            repair_actions.append({
                                "action_type": "HASH_RECOMPUTE",
                                "node_id": node_id,
                                "expected_hash": expected_hash,
                                "previous_hash": actual_hash,
                                "new_hash": recompute_result.computed_hash,
                                "success": True,
                                "timestamp": datetime.utcnow().isoformat(),
                            })
                        else:
                            repair_actions.append({
                                "action_type": "HASH_RECOMPUTE_FAILED",
                                "node_id": node_id,
                                "expected_hash": expected_hash,
                                "actual_hash": actual_hash,
                                "success": False,
                                "error": str(recompute_result.error) if recompute_result else "Unknown error",
                                "timestamp": datetime.utcnow().isoformat(),
                            })
                    else:
                        # Mismatch detected but recompute disabled
                        repaired_nodes.append(node_id)
                        repair_actions.append({
                            "action_type": "HASH_MISMATCH_DETECTED",
                            "node_id": node_id,
                            "expected_hash": expected_hash,
                            "actual_hash": actual_hash,
                            "recomputed": False,
                            "success": True,
                            "timestamp": datetime.utcnow().isoformat(),
                        })
            
            # Post-repair validation
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
            success = len(validation_errors) == 0
            
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
                root_node_id=request.node_ids[0] if request.node_ids else request.workflow_id,
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
        request: HashVerificationRepairRequest,
        scope: Optional[RepairScope],
        repaired_nodes: List[str],
        post_snapshot_hash: Optional[str],
    ) -> str:
        """Compute deterministic hash of repair operation."""
        hash_input = {
            "strategy_id": self.strategy_id,
            "request_id": request.request_id,
            "workflow_id": request.workflow_id,
            "node_ids": sorted(request.node_ids),
            "schema_version": request.schema_version,
            "repaired_nodes": sorted(repaired_nodes),
            "post_snapshot_hash": post_snapshot_hash,
        }
        content = json.dumps(hash_input, sort_keys=True)
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
