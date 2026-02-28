"""
repair_strategies.py

Pluggable, bounded, post-rollback repair engine.
Applies explicit, auditable repairs to known-safe damage classes.

CRITICAL RULES:
- Repair happens ONLY after rollback completes
- NO autonomous improvisation
- ALL repairs must pass invariant checks
- Idempotent and replay-compatible ALWAYS
- Applicability must be proven, not assumed

Mental Model:
  Rollback restores truth.
  Repair restores usability.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Set
import time
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# CORE ENUMS
# ============================================================================

class RepairStrategyType(Enum):
    """
    Whitelist of allowed repair strategies.
    NO runtime strategy creation permitted.
    """
    QUEUE_REPAIR = "queue_repair"
    WORKFLOW_REPAIR = "workflow_repair"
    ACCOUNT_ISOLATION = "account_isolation"
    INDEX_REBUILD = "index_rebuild"
    EXPERIMENT_RESET = "experiment_reset"
    CONFIG_REHYDRATION = "config_rehydration"


class RepairPhase(Enum):
    """Repair execution phases for audit trail"""
    REQUESTED = "requested"
    VALIDATING = "validating"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


# ============================================================================
# DATA MODELS (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class RepairRequest:
    """
    Explicit repair request.
    All repairs must be formally requested, never automatic.
    """
    repair_id: str
    strategy_type: RepairStrategyType
    damage_report_id: str
    
    affected_resources: List[str]
    constraints: Dict[str, Any]
    
    requested_at: int
    requested_by: str
    
    def __post_init__(self):
        """Validate required fields"""
        if not self.repair_id:
            raise ValueError("repair_id cannot be empty")
        if not self.damage_report_id:
            raise ValueError("damage_report_id cannot be empty")
        if not self.affected_resources:
            raise ValueError("affected_resources cannot be empty")


@dataclass(frozen=True)
class RepairResult:
    """
    Immutable repair outcome.
    Complete audit trail of what was done.
    """
    repair_id: str
    success: bool
    applied_actions: List[str]
    
    post_state_hash: str
    violations: Optional[List[str]]
    
    started_at: int
    completed_at: int
    
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def duration_ms(self) -> int:
        """Execution duration in milliseconds"""
        return self.completed_at - self.started_at


@dataclass(frozen=True)
class DamageReport:
    """
    Input from damage_assessor.py
    Used to determine repair applicability
    """
    report_id: str
    damage_type: str
    severity: str
    affected_resources: List[str]
    root_cause: Optional[str]
    invariants_broken: List[str]
    safe_to_repair: bool
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# BASE REPAIR STRATEGY (ABSTRACT)
# ============================================================================

class BaseRepairStrategy(ABC):
    """
    Abstract base for all repair strategies.
    
    NON-NEGOTIABLE CONTRACT:
    1. Applicability must be proven from damage_report
    2. Preconditions must check invariants
    3. Execution must be deterministic
    4. Postconditions must re-validate invariants
    5. Must be idempotent
    """
    
    def __init__(self, invariant_engine, persistence_layer):
        """
        Args:
            invariant_engine: For validating system invariants
            persistence_layer: For state queries and updates
        """
        self.invariant_engine = invariant_engine
        self.persistence = persistence_layer
        self._audit_trail: List[str] = []
    
    @abstractmethod
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """
        Determine if this strategy can repair the reported damage.
        
        Must return False if ANY uncertainty exists.
        """
        pass
    
    @abstractmethod
    def preconditions(self, request: RepairRequest) -> None:
        """
        Validate all preconditions before repair.
        
        Raises:
            PreconditionViolation: If any precondition fails
        """
        pass
    
    @abstractmethod
    def execute(self, request: RepairRequest) -> RepairResult:
        """
        Execute the repair strategy.
        
        Must be:
        - Deterministic
        - Idempotent
        - Scoped to affected_resources only
        
        Returns:
            RepairResult with complete audit trail
        """
        pass
    
    @abstractmethod
    def postconditions(self, result: RepairResult) -> None:
        """
        Re-validate invariants after repair.
        
        Raises:
            PostconditionViolation: If invariants broken
        """
        pass
    
    def _log_action(self, action: str) -> None:
        """Record action in audit trail"""
        self._audit_trail.append(action)
        logger.info(f"[REPAIR ACTION] {action}")
    
    def _compute_state_hash(self, resources: List[str]) -> str:
        """Compute deterministic hash of resource states"""
        state_data = []
        for resource in sorted(resources):
            state = self.persistence.get_state(resource)
            state_data.append(f"{resource}:{json.dumps(state, sort_keys=True)}")
        
        combined = "|".join(state_data)
        return hashlib.sha256(combined.encode()).hexdigest()


# ============================================================================
# CUSTOM EXCEPTIONS
# ============================================================================

class RepairError(Exception):
    """Base exception for repair failures"""
    pass


class PreconditionViolation(RepairError):
    """Precondition check failed"""
    pass


class PostconditionViolation(RepairError):
    """Postcondition check failed"""
    pass


class InvariantViolation(RepairError):
    """System invariant broken during repair"""
    pass


class ApplicabilityCheckFailed(RepairError):
    """Strategy not applicable to damage"""
    pass


# ============================================================================
# CONCRETE REPAIR STRATEGIES
# ============================================================================

class QueueRepairStrategy(BaseRepairStrategy):
    """
    Repair stalled or orphaned queues.
    
    ALLOWED:
    - Requeue safe jobs
    - Drop poison messages
    - Reset offsets
    
    FORBIDDEN:
    - Creating new jobs
    - Altering job payloads
    """
    
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """Applicable to queue-related damage only"""
        if damage_report.damage_type != "queue_stall":
            return False
        if not damage_report.safe_to_repair:
            return False
        if "queue" not in damage_report.metadata:
            return False
        return True
    
    def preconditions(self, request: RepairRequest) -> None:
        """Validate queue is in repairable state"""
        # Check invariants
        if not self.invariant_engine.check("queue_consistency"):
            raise PreconditionViolation("Queue consistency invariant failed")
        
        # Verify queues exist
        for resource in request.affected_resources:
            if not self.persistence.queue_exists(resource):
                raise PreconditionViolation(f"Queue does not exist: {resource}")
        
        # Verify not in active processing
        for resource in request.affected_resources:
            if self.persistence.is_queue_processing(resource):
                raise PreconditionViolation(f"Queue still processing: {resource}")
    
    def execute(self, request: RepairRequest) -> RepairResult:
        """Execute queue repair"""
        started_at = int(time.time() * 1000)
        self._audit_trail = []
        
        try:
            for queue_id in request.affected_resources:
                # Identify poison messages
                poison_msgs = self._identify_poison_messages(queue_id)
                
                if poison_msgs:
                    self._log_action(f"Dropping {len(poison_msgs)} poison messages from {queue_id}")
                    self.persistence.drop_messages(queue_id, poison_msgs)
                
                # Reset offset if stuck
                if self.persistence.is_offset_stuck(queue_id):
                    self._log_action(f"Resetting offset for {queue_id}")
                    self.persistence.reset_queue_offset(queue_id)
                
                # Requeue safe jobs
                safe_jobs = self._identify_safe_jobs(queue_id)
                if safe_jobs:
                    self._log_action(f"Requeuing {len(safe_jobs)} safe jobs to {queue_id}")
                    self.persistence.requeue_jobs(queue_id, safe_jobs)
            
            post_hash = self._compute_state_hash(request.affected_resources)
            
            return RepairResult(
                repair_id=request.repair_id,
                success=True,
                applied_actions=self._audit_trail.copy(),
                post_state_hash=post_hash,
                violations=None,
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
        
        except Exception as e:
            logger.error(f"Queue repair failed: {e}")
            return RepairResult(
                repair_id=request.repair_id,
                success=False,
                applied_actions=self._audit_trail.copy(),
                post_state_hash="",
                violations=[str(e)],
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
    
    def postconditions(self, result: RepairResult) -> None:
        """Verify queues are healthy post-repair"""
        if not result.success:
            raise PostconditionViolation("Repair did not complete successfully")
        
        if not self.invariant_engine.check("queue_consistency"):
            raise PostconditionViolation("Queue consistency violated post-repair")
    
    def _identify_poison_messages(self, queue_id: str) -> List[str]:
        """Identify messages causing repeated failures"""
        return self.persistence.get_poison_messages(queue_id, threshold=3)
    
    def _identify_safe_jobs(self, queue_id: str) -> List[str]:
        """Identify jobs safe to requeue"""
        return self.persistence.get_requeue_candidates(queue_id)


class WorkflowRepairStrategy(BaseRepairStrategy):
    """
    Repair DAG execution state.
    
    ACTIONS:
    - Resume blocked nodes
    - Mark completed edges
    - Cancel impossible branches
    
    MUST PRESERVE:
    - DAG structure
    - Execution ordering
    """
    
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """Applicable to workflow/DAG execution failures"""
        return (
            damage_report.damage_type == "workflow_stuck" and
            damage_report.safe_to_repair and
            "workflow_id" in damage_report.metadata
        )
    
    def preconditions(self, request: RepairRequest) -> None:
        """Validate workflow state"""
        if not self.invariant_engine.check("dag_acyclic"):
            raise PreconditionViolation("DAG acyclicity violated")
        
        for workflow_id in request.affected_resources:
            if not self.persistence.workflow_exists(workflow_id):
                raise PreconditionViolation(f"Workflow not found: {workflow_id}")
    
    def execute(self, request: RepairRequest) -> RepairResult:
        """Repair workflow execution"""
        started_at = int(time.time() * 1000)
        self._audit_trail = []
        
        try:
            for workflow_id in request.affected_resources:
                # Identify blocked nodes
                blocked_nodes = self.persistence.get_blocked_nodes(workflow_id)
                
                for node_id in blocked_nodes:
                    # Check if dependencies actually completed
                    deps = self.persistence.get_node_dependencies(workflow_id, node_id)
                    if all(self.persistence.is_node_complete(workflow_id, d) for d in deps):
                        self._log_action(f"Resuming node {node_id} in {workflow_id}")
                        self.persistence.resume_node(workflow_id, node_id)
                
                # Cancel impossible branches
                impossible = self.persistence.get_impossible_branches(workflow_id)
                for branch_id in impossible:
                    self._log_action(f"Canceling impossible branch {branch_id}")
                    self.persistence.cancel_branch(workflow_id, branch_id)
            
            post_hash = self._compute_state_hash(request.affected_resources)
            
            return RepairResult(
                repair_id=request.repair_id,
                success=True,
                applied_actions=self._audit_trail.copy(),
                post_state_hash=post_hash,
                violations=None,
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
        
        except Exception as e:
            return RepairResult(
                repair_id=request.repair_id,
                success=False,
                applied_actions=self._audit_trail.copy(),
                post_state_hash="",
                violations=[str(e)],
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
    
    def postconditions(self, result: RepairResult) -> None:
        """Verify DAG integrity"""
        if not result.success:
            raise PostconditionViolation("Workflow repair failed")
        
        if not self.invariant_engine.check("dag_acyclic"):
            raise PostconditionViolation("DAG acyclicity broken")


class AccountIsolationRepairStrategy(BaseRepairStrategy):
    """
    Contain trust-risk spread.
    
    ACTIONS:
    - Detach accounts from posting
    - Freeze risky identities
    - Downgrade privileges
    
    ⚠️ NEVER INCREASES TRUST
    """
    
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """Applicable to trust violations"""
        return (
            damage_report.damage_type in ["trust_violation", "account_compromise"] and
            damage_report.safe_to_repair
        )
    
    def preconditions(self, request: RepairRequest) -> None:
        """Validate accounts can be safely isolated"""
        if not self.invariant_engine.check("trust_monotonic_decrease"):
            raise PreconditionViolation("Trust monotonicity violated")
        
        for account_id in request.affected_resources:
            if not self.persistence.account_exists(account_id):
                raise PreconditionViolation(f"Account not found: {account_id}")
    
    def execute(self, request: RepairRequest) -> RepairResult:
        """Isolate compromised accounts"""
        started_at = int(time.time() * 1000)
        self._audit_trail = []
        
        try:
            for account_id in request.affected_resources:
                # Freeze posting capability
                self._log_action(f"Freezing posting for {account_id}")
                self.persistence.freeze_posting(account_id)
                
                # Downgrade privileges (NEVER upgrade)
                current_level = self.persistence.get_trust_level(account_id)
                self._log_action(f"Downgrading {account_id} from level {current_level}")
                self.persistence.set_trust_level(account_id, max(0, current_level - 1))
                
                # Detach from active campaigns
                self._log_action(f"Detaching {account_id} from campaigns")
                self.persistence.detach_from_campaigns(account_id)
            
            post_hash = self._compute_state_hash(request.affected_resources)
            
            return RepairResult(
                repair_id=request.repair_id,
                success=True,
                applied_actions=self._audit_trail.copy(),
                post_state_hash=post_hash,
                violations=None,
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
        
        except Exception as e:
            return RepairResult(
                repair_id=request.repair_id,
                success=False,
                applied_actions=self._audit_trail.copy(),
                post_state_hash="",
                violations=[str(e)],
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
    
    def postconditions(self, result: RepairResult) -> None:
        """Verify trust levels decreased"""
        if not result.success:
            raise PostconditionViolation("Isolation failed")
        
        # Trust must NEVER increase during repair
        if not self.invariant_engine.check("trust_monotonic_decrease"):
            raise PostconditionViolation("Trust increased during repair - CRITICAL")


class IndexRebuildRepairStrategy(BaseRepairStrategy):
    """
    Repair corrupted or stale derived indices.
    
    ACTIONS:
    - Drop unsafe index
    - Rebuild from snapshot
    - Validate referential integrity
    
    Source-of-truth is IMMUTABLE.
    """
    
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """Applicable to index corruption"""
        return (
            damage_report.damage_type == "index_corruption" and
            damage_report.safe_to_repair and
            "source_intact" in damage_report.metadata and
            damage_report.metadata["source_intact"]
        )
    
    def preconditions(self, request: RepairRequest) -> None:
        """Verify source data is intact"""
        for index_id in request.affected_resources:
            source = self.persistence.get_index_source(index_id)
            if not self.persistence.verify_source_integrity(source):
                raise PreconditionViolation(f"Source corrupted for index {index_id}")
    
    def execute(self, request: RepairRequest) -> RepairResult:
        """Rebuild indices from source"""
        started_at = int(time.time() * 1000)
        self._audit_trail = []
        
        try:
            for index_id in request.affected_resources:
                # Drop corrupted index
                self._log_action(f"Dropping corrupted index {index_id}")
                self.persistence.drop_index(index_id)
                
                # Rebuild from source snapshot
                source = self.persistence.get_index_source(index_id)
                self._log_action(f"Rebuilding {index_id} from {source}")
                self.persistence.rebuild_index(index_id, source)
                
                # Validate referential integrity
                self._log_action(f"Validating referential integrity for {index_id}")
                if not self.persistence.validate_index_integrity(index_id):
                    raise RepairError(f"Index integrity check failed: {index_id}")
            
            post_hash = self._compute_state_hash(request.affected_resources)
            
            return RepairResult(
                repair_id=request.repair_id,
                success=True,
                applied_actions=self._audit_trail.copy(),
                post_state_hash=post_hash,
                violations=None,
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
        
        except Exception as e:
            return RepairResult(
                repair_id=request.repair_id,
                success=False,
                applied_actions=self._audit_trail.copy(),
                post_state_hash="",
                violations=[str(e)],
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
    
    def postconditions(self, result: RepairResult) -> None:
        """Verify index integrity"""
        if not result.success:
            raise PostconditionViolation("Index rebuild failed")
        
        if not self.invariant_engine.check("index_referential_integrity"):
            raise PostconditionViolation("Referential integrity broken")


class ExperimentResetRepairStrategy(BaseRepairStrategy):
    """
    Neutralize compromised experiments.
    
    ACTIONS:
    - Freeze rollout
    - Reset allocation
    - Archive bad variants
    
    NEVER deletes experiment history.
    """
    
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """Applicable to experiment corruption"""
        return (
            damage_report.damage_type == "experiment_corruption" and
            damage_report.safe_to_repair
        )
    
    def preconditions(self, request: RepairRequest) -> None:
        """Validate experiments can be reset"""
        for exp_id in request.affected_resources:
            if not self.persistence.experiment_exists(exp_id):
                raise PreconditionViolation(f"Experiment not found: {exp_id}")
    
    def execute(self, request: RepairRequest) -> RepairResult:
        """Reset compromised experiments"""
        started_at = int(time.time() * 1000)
        self._audit_trail = []
        
        try:
            for exp_id in request.affected_resources:
                # Freeze rollout
                self._log_action(f"Freezing rollout for experiment {exp_id}")
                self.persistence.freeze_experiment(exp_id)
                
                # Archive bad variants (preserve history)
                bad_variants = self.persistence.get_bad_variants(exp_id)
                if bad_variants:
                    self._log_action(f"Archiving {len(bad_variants)} bad variants")
                    self.persistence.archive_variants(exp_id, bad_variants)
                
                # Reset allocation to safe defaults
                self._log_action(f"Resetting allocation for {exp_id}")
                self.persistence.reset_experiment_allocation(exp_id)
            
            post_hash = self._compute_state_hash(request.affected_resources)
            
            return RepairResult(
                repair_id=request.repair_id,
                success=True,
                applied_actions=self._audit_trail.copy(),
                post_state_hash=post_hash,
                violations=None,
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
        
        except Exception as e:
            return RepairResult(
                repair_id=request.repair_id,
                success=False,
                applied_actions=self._audit_trail.copy(),
                post_state_hash="",
                violations=[str(e)],
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
    
    def postconditions(self, result: RepairResult) -> None:
        """Verify experiment state is safe"""
        if not result.success:
            raise PostconditionViolation("Experiment reset failed")
        
        # Verify no history was deleted
        if not self.invariant_engine.check("experiment_history_immutable"):
            raise PostconditionViolation("Experiment history deleted - CRITICAL")


class ConfigRehydrationRepairStrategy(BaseRepairStrategy):
    """
    Restore config integrity.
    
    ACTIONS:
    - Reload from config_registry
    - Reapply version locks
    - Validate schema compatibility
    """
    
    def is_applicable(self, damage_report: DamageReport) -> bool:
        """Applicable to config corruption"""
        return (
            damage_report.damage_type == "config_corruption" and
            damage_report.safe_to_repair and
            "registry_intact" in damage_report.metadata and
            damage_report.metadata["registry_intact"]
        )
    
    def preconditions(self, request: RepairRequest) -> None:
        """Verify config registry is intact"""
        if not self.invariant_engine.check("config_registry_intact"):
            raise PreconditionViolation("Config registry corrupted")
    
    def execute(self, request: RepairRequest) -> RepairResult:
        """Rehydrate configs from registry"""
        started_at = int(time.time() * 1000)
        self._audit_trail = []
        
        try:
            for config_key in request.affected_resources:
                # Reload from registry
                self._log_action(f"Reloading config {config_key} from registry")
                canonical = self.persistence.get_canonical_config(config_key)
                self.persistence.restore_config(config_key, canonical)
                
                # Reapply version locks
                self._log_action(f"Reapplying version lock for {config_key}")
                self.persistence.lock_config_version(config_key)
                
                # Validate schema
                self._log_action(f"Validating schema for {config_key}")
                if not self.persistence.validate_config_schema(config_key):
                    raise RepairError(f"Schema validation failed: {config_key}")
            
            post_hash = self._compute_state_hash(request.affected_resources)
            
            return RepairResult(
                repair_id=request.repair_id,
                success=True,
                applied_actions=self._audit_trail.copy(),
                post_state_hash=post_hash,
                violations=None,
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
        
        except Exception as e:
            return RepairResult(
                repair_id=request.repair_id,
                success=False,
                applied_actions=self._audit_trail.copy(),
                post_state_hash="",
                violations=[str(e)],
                started_at=started_at,
                completed_at=int(time.time() * 1000)
            )
    
    def postconditions(self, result: RepairResult) -> None:
        """Verify config integrity"""
        if not result.success:
            raise PostconditionViolation("Config rehydration failed")
        
        if not self.invariant_engine.check("config_schema_valid"):
            raise PostconditionViolation("Config schema invalid post-repair")


# ============================================================================
# REPAIR STRATEGY REGISTRY
# ============================================================================

class RepairStrategyRegistry:
    """
    Whitelist-only registry of repair strategies.
    
    RULES:
    - No runtime injection
    - Versioned mappings
    - Safe defaults
    """
    
    def __init__(self, invariant_engine, persistence_layer):
        """Initialize with required dependencies"""
        self._strategies: Dict[RepairStrategyType, BaseRepairStrategy] = {}
        self._invariant_engine = invariant_engine
        self._persistence = persistence_layer
        self._initialize_strategies()
    
    def _initialize_strategies(self) -> None:
        """Initialize all allowed strategies (WHITELIST ONLY)"""
        self._strategies = {
            RepairStrategyType.QUEUE_REPAIR: QueueRepairStrategy(
                self._invariant_engine, self._persistence
            ),
            RepairStrategyType.WORKFLOW_REPAIR: WorkflowRepairStrategy(
                self._invariant_engine, self._persistence
            ),
            RepairStrategyType.ACCOUNT_ISOLATION: AccountIsolationRepairStrategy(
                self._invariant_engine, self._persistence
            ),
            RepairStrategyType.INDEX_REBUILD: IndexRebuildRepairStrategy(
                self._invariant_engine, self._persistence
            ),
            RepairStrategyType.EXPERIMENT_RESET: ExperimentResetRepairStrategy(
                self._invariant_engine, self._persistence
            ),
            RepairStrategyType.CONFIG_REHYDRATION: ConfigRehydrationRepairStrategy(
                self._invariant_engine, self._persistence
            ),
        }
    
    def get(self, strategy_type: RepairStrategyType) -> BaseRepairStrategy:
        """
        Get strategy by type.
        
        Raises:
            KeyError: If strategy not registered (FAIL CLOSED)
        """
        if strategy_type not in self._strategies:
            raise KeyError(f"Unknown repair strategy: {strategy_type}")
        
        return self._strategies[strategy_type]
    
    def list_strategies(self) -> List[RepairStrategyType]:
        """List all registered strategies"""
        return list(self._strategies.keys())


# ============================================================================
# REPAIR EXECUTOR (ORCHESTRATION INTERFACE)
# ============================================================================

class RepairExecutor:
    """
    High-level interface for executing repairs.
    Called by recovery_orchestrator.py
    """
    
    def __init__(self, registry: RepairStrategyRegistry):
        self.registry = registry
        self._active_repairs: Dict[str, RepairPhase] = {}
    
    def execute_repair(
        self,
        request: RepairRequest,
        damage_report: DamageReport
    ) -> RepairResult:
        """
        Execute a repair request with full validation.
        
        Workflow:
        1. Verify applicability
        2. Check preconditions
        3. Execute repair
        4. Verify postconditions
        5. Return immutable result
        """
        self._active_repairs[request.repair_id] = RepairPhase.VALIDATING
        
        try:
            # Get strategy
            strategy = self.registry.get(request.strategy_type)
            
            # Verify applicability
            if not strategy.is_applicable(damage_report):
                raise ApplicabilityCheckFailed(
                    f"{request.strategy_type} not applicable to {damage_report.damage_type}"
                )
            
            # Check preconditions
            logger.info(f"Checking preconditions for repair {request.repair_id}")
            strategy.preconditions(request)
            
            # Execute
            self._active_repairs[request.repair_id] = RepairPhase.EXECUTING
            logger.info(f"Executing repair {request.repair_id}")
            result = strategy.execute(request)
            
            # Verify postconditions
            self._active_repairs[request.repair_id] = RepairPhase.VERIFYING
            logger.info(f"Verifying postconditions for repair {request.repair_id}")
            strategy.postconditions(result)
            
            # Mark complete
            self._active_repairs[request.repair_id] = RepairPhase.COMPLETED
            logger.info(f"Repair {request.repair_id} completed successfully")
            
            return result
        
        except (PreconditionViolation, PostconditionViolation, InvariantViolation) as e:
            # Critical failures -> emergency escalation
            self._active_repairs[request.repair_id] = RepairPhase.ABORTED
            logger.error(f"CRITICAL: Repair {request.repair_id} violated invariants: {e}")
            raise
        
        except Exception as e:
            # Other failures -> mark failed
            self._active_repairs[request.repair_id] = RepairPhase.FAILED
            logger.error(f"Repair {request.repair_id} failed: {e}")
            raise
    
    def get_repair_status(self, repair_id: str) -> Optional[RepairPhase]:
        """Get current phase of a repair"""
        return self._active_repairs.get(repair_id)
    
    def list_active_repairs(self) -> Dict[str, RepairPhase]:
        """List all active repairs and their phases"""
        return self._active_repairs.copy()


# ============================================================================
# OBSERVABILITY & AUDIT
# ============================================================================

class RepairAuditor:
    """
    Emit structured audit events for all repair operations.
    
    MANDATORY EVENTS:
    - repair_requested
    - repair_started
    - repair_step_applied
    - repair_completed
    - repair_failed
    """
    
    def __init__(self, event_bus):
        self.event_bus = event_bus
    
    def log_requested(self, request: RepairRequest) -> None:
        """Log repair request"""
        self.event_bus.emit({
            "event": "repair_requested",
            "repair_id": request.repair_id,
            "strategy": request.strategy_type.value,
            "damage_report_id": request.damage_report_id,
            "resources": request.affected_resources,
            "timestamp": request.requested_at
        })
    
    def log_started(self, repair_id: str, strategy: str) -> None:
        """Log repair start"""
        self.event_bus.emit({
            "event": "repair_started",
            "repair_id": repair_id,
            "strategy": strategy,
            "timestamp": int(time.time() * 1000)
        })
    
    def log_step(self, repair_id: str, action: str) -> None:
        """Log individual repair step"""
        self.event_bus.emit({
            "event": "repair_step_applied",
            "repair_id": repair_id,
            "action": action,
            "timestamp": int(time.time() * 1000)
        })
    
    def log_completed(self, result: RepairResult) -> None:
        """Log successful completion"""
        self.event_bus.emit({
            "event": "repair_completed",
            "repair_id": result.repair_id,
            "success": result.success,
            "actions": result.applied_actions,
            "state_hash": result.post_state_hash,
            "duration_ms": result.duration_ms,
            "timestamp": result.completed_at
        })
    
    def log_failed(self, repair_id: str, error: str, violations: List[str]) -> None:
        """Log failure"""
        self.event_bus.emit({
            "event": "repair_failed",
            "repair_id": repair_id,
            "error": error,
            "violations": violations,
            "timestamp": int(time.time() * 1000)
        })


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Mock dependencies
    class MockInvariantEngine:
        def check(self, name): return True
    
    class MockPersistence:
        def get_state(self, r): return {"status": "ok"}
        def queue_exists(self, q): return True
        def is_queue_processing(self, q): return False
        def get_poison_messages(self, q, threshold): return []
        def is_offset_stuck(self, q): return False
        def get_requeue_candidates(self, q): return []
        def drop_messages(self, q, msgs): pass
        def reset_queue_offset(self, q): pass
        def requeue_jobs(self, q, jobs): pass
    
    class MockEventBus:
        def emit(self, event): print(f"📡 {event}")
    
    # Initialize system
    invariants = MockInvariantEngine()
    persistence = MockPersistence()
    registry = RepairStrategyRegistry(invariants, persistence)
    executor = RepairExecutor(registry)
    auditor = RepairAuditor(MockEventBus())
    
    # Create damage report
    damage = DamageReport(
        report_id="dmg_001",
        damage_type="queue_stall",
        severity="medium",
        affected_resources=["queue_123"],
        root_cause="poison_message",
        invariants_broken=[],
        safe_to_repair=True,
        metadata={"queue": "queue_123"}
    )
    
    # Create repair request
    request = RepairRequest(
        repair_id="rep_001",
        strategy_type=RepairStrategyType.QUEUE_REPAIR,
        damage_report_id="dmg_001",
        affected_resources=["queue_123"],
        constraints={},
        requested_at=int(time.time() * 1000),
        requested_by="recovery_orchestrator"
    )
    
    # Execute repair
    auditor.log_requested(request)
    result = executor.execute_repair(request, damage)
    auditor.log_completed(result)
    
    print(f"\n✅ Repair completed: {result.success}")
    print(f"📋 Actions taken: {len(result.applied_actions)}")
    print(f"⏱️  Duration: {result.duration_ms}ms")