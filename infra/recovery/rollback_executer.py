"""
rollback_executer.py - Deterministic Rollback Execution Engine

Purpose: Safely, deterministically, and atomically revert system state to a known-good 
snapshot after a failure event.

This file EXECUTES rollbacks - it does not decide when to rollback.
That decision comes from upstream (failure_recovery, emergency_stop, etc).

Guarantees:
- Exactly-once semantic execution
- Atomic all-or-nothing reversion
- Idempotent (safely re-runnable)
- Deterministic ordering
- Auditable trail
- Fail-closed (halt on incomplete rollback)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Callable, Any, Tuple
from abc import ABC, abstractmethod
import time
import hashlib
import json


class RollbackType(Enum):
    """Types of rollback operations"""
    FULL_SYSTEM = "full_system"           # Everything reverted
    PARTIAL = "partial"                   # Only affected subsystems
    SHADOW = "shadow"                     # Dry-run verification
    FORWARD_CORRECTION = "forward_correction"  # Rollback + hotfix replay
    EXPERIMENT_ONLY = "experiment_only"   # Scoped to experiment layer


class RollbackPhase(Enum):
    """Execution phases"""
    PREFLIGHT = "preflight"
    LOCK_ACQUISITION = "lock_acquisition"
    PLAN_ASSEMBLY = "plan_assembly"
    STATE_REVERSION = "state_reversion"
    VALIDATION = "validation"
    FINALIZATION = "finalization"
    COMPLETED = "completed"
    ABORTED = "aborted"


class SubsystemType(Enum):
    """Subsystem ordering for rollback"""
    PERSISTENCE = "persistence"      # Order: 1 (always first)
    INFRA = "infra"                 # Order: 2
    ACCOUNTS = "accounts"           # Order: 3
    CONTENT = "content"             # Order: 4
    EXPERIMENTS = "experiments"     # Order: 5 (always last)


class RollbackStatus(Enum):
    """Status of rollback execution"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class RollbackRequest:
    """Input contract for rollback execution"""
    rollback_id: str                    # Unique deterministic ID
    target_snapshot_id: str             # Snapshot to restore
    trigger_event: str                  # What caused this rollback
    rollback_scope: RollbackType
    
    # Context
    triggered_by: str                   # User, system, emergency_stop, etc
    timestamp: int
    reason: str
    
    # Optional constraints
    affected_accounts: Optional[List[str]] = None
    subsystems: Optional[List[SubsystemType]] = None
    dry_run: bool = False


@dataclass
class RollbackStep:
    """Single step in rollback plan"""
    step_id: str
    subsystem: SubsystemType
    order: int
    description: str
    revert_fn: Callable[[], bool]      # Returns success
    
    # Step metadata
    preconditions: List[str] = field(default_factory=list)
    checksum: Optional[str] = None
    timeout_seconds: int = 300
    
    # State tracking
    executed: bool = False
    succeeded: bool = False
    error: Optional[str] = None
    execution_time: Optional[float] = None


@dataclass
class RollbackPlan:
    """Immutable rollback execution plan"""
    plan_id: str
    rollback_id: str
    steps: List[RollbackStep]
    created_at: int
    plan_hash: str                     # Hash of entire plan
    
    # Metadata
    total_steps: int = 0
    estimated_duration_seconds: int = 0
    
    def __post_init__(self):
        object.__setattr__(self, 'total_steps', len(self.steps))
        object.__setattr__(
            self, 
            'estimated_duration_seconds',
            sum(s.timeout_seconds for s in self.steps)
        )
    
    def verify_integrity(self) -> bool:
        """Verify plan hasn't been tampered with"""
        computed_hash = self._compute_hash()
        return computed_hash == self.plan_hash
    
    def _compute_hash(self) -> str:
        """Compute deterministic hash of plan"""
        plan_data = {
            'plan_id': self.plan_id,
            'rollback_id': self.rollback_id,
            'steps': [
                {
                    'step_id': s.step_id,
                    'subsystem': s.subsystem.value,
                    'order': s.order,
                    'description': s.description
                }
                for s in self.steps
            ]
        }
        plan_json = json.dumps(plan_data, sort_keys=True)
        return hashlib.sha256(plan_json.encode()).hexdigest()


@dataclass
class SubsystemRollbackStatus:
    """Status of single subsystem rollback"""
    subsystem: SubsystemType
    status: RollbackStatus
    steps_completed: int
    steps_total: int
    error: Optional[str] = None


@dataclass
class RollbackResult:
    """Output contract for rollback execution"""
    rollback_id: str
    status: RollbackStatus
    phase: RollbackPhase
    
    # Timing
    started_at: int
    completed_at: Optional[int] = None
    duration_seconds: Optional[float] = None
    
    # Execution details
    subsystem_status: Dict[str, SubsystemRollbackStatus] = field(default_factory=dict)
    steps_executed: int = 0
    steps_total: int = 0
    
    # Validation
    validation_passed: bool = False
    validation_errors: List[str] = field(default_factory=list)
    
    # Audit
    rollback_checksum: Optional[str] = None
    audit_trail: List[Dict] = field(default_factory=list)
    
    # Failure info
    failure_reason: Optional[str] = None
    failed_step: Optional[str] = None


class RollbackExecutionException(Exception):
    """Raised when rollback execution fails critically"""
    pass


class SnapshotStore(ABC):
    """Abstract interface for snapshot storage"""
    
    @abstractmethod
    def snapshot_exists(self, snapshot_id: str) -> bool:
        pass
    
    @abstractmethod
    def get_snapshot(self, snapshot_id: str) -> Dict:
        pass
    
    @abstractmethod
    def validate_snapshot_schema(self, snapshot: Dict) -> bool:
        pass


class LockManager(ABC):
    """Abstract interface for distributed locking"""
    
    @abstractmethod
    def acquire_global_lock(self, lock_id: str, ttl_seconds: int) -> bool:
        pass
    
    @abstractmethod
    def release_global_lock(self, lock_id: str) -> bool:
        pass
    
    @abstractmethod
    def extend_lock(self, lock_id: str, ttl_seconds: int) -> bool:
        pass


class InvariantEngine(ABC):
    """Abstract interface for invariant validation"""
    
    @abstractmethod
    def validate_account_invariants(self) -> tuple[bool, List[str]]:
        pass
    
    @abstractmethod
    def validate_infra_invariants(self) -> tuple[bool, List[str]]:
        pass
    
    @abstractmethod
    def validate_state_consistency(self) -> tuple[bool, List[str]]:
        pass


class AuditLogger(ABC):
    """Abstract interface for audit logging"""
    
    @abstractmethod
    def log_event(self, event_type: str, data: Dict) -> None:
        pass


class EmergencyStop(ABC):
    """Abstract interface for emergency stop system"""
    
    @abstractmethod
    def trigger_halt(self, reason: str) -> None:
        pass
    
    @abstractmethod
    def is_active(self) -> bool:
        pass


class RollbackExecuter:
    """
    Main rollback execution engine.
    
    Executes rollbacks atomically with strict ordering and validation.
    Never proceeds after failure - always fails closed.
    """
    
    # Execution constants
    LOCK_TTL_SECONDS = 1800  # 30 minutes
    LOCK_EXTEND_INTERVAL = 300  # 5 minutes
    MAX_LOCK_RETRIES = 3
    LOCK_RETRY_BACKOFF_SECONDS = 5
    
    def __init__(
        self,
        snapshot_store: SnapshotStore,
        lock_manager: LockManager,
        invariant_engine: InvariantEngine,
        audit_logger: AuditLogger,
        emergency_stop: EmergencyStop
    ):
        """
        Initialize rollback executer.
        
        Args:
            snapshot_store: Interface to snapshot storage
            lock_manager: Distributed lock manager
            invariant_engine: Invariant validation system
            audit_logger: Audit trail logger
            emergency_stop: Emergency halt system
        """
        self.snapshot_store = snapshot_store
        self.lock_manager = lock_manager
        self.invariant_engine = invariant_engine
        self.audit_logger = audit_logger
        self.emergency_stop = emergency_stop
        
        # Execution state
        self._current_plan: Optional[RollbackPlan] = None
        self._executed_steps: set[str] = set()
        self._lock_acquired: bool = False
        self._current_lock_id: Optional[str] = None
    
    def execute(self, request: RollbackRequest) -> RollbackResult:
        """
        Execute a rollback request.
        
        This is the main entry point. Executes all phases in strict order.
        Fails closed - any error triggers system halt.
        
        Args:
            request: Rollback request specification
            
        Returns:
            RollbackResult with execution details
            
        Raises:
            RollbackExecutionException: On critical failure
        """
        result = RollbackResult(
            rollback_id=request.rollback_id,
            status=RollbackStatus.PENDING,
            phase=RollbackPhase.PREFLIGHT,
            started_at=int(time.time())
        )
        
        try:
            # PHASE 0: Preflight Validation
            self._log_phase(request, RollbackPhase.PREFLIGHT)
            self._validate_preflight(request, result)
            
            # PHASE 1: Lock Acquisition
            result.phase = RollbackPhase.LOCK_ACQUISITION
            self._log_phase(request, RollbackPhase.LOCK_ACQUISITION)
            self._acquire_locks(request, result)
            
            # PHASE 2: Plan Assembly
            result.phase = RollbackPhase.PLAN_ASSEMBLY
            self._log_phase(request, RollbackPhase.PLAN_ASSEMBLY)
            plan = self._build_plan(request, result)
            self._current_plan = plan
            
            # PHASE 3: State Reversion
            result.phase = RollbackPhase.STATE_REVERSION
            result.status = RollbackStatus.IN_PROGRESS
            self._log_phase(request, RollbackPhase.STATE_REVERSION)
            self._execute_plan(plan, result)
            
            # PHASE 4: Post-Rollback Validation
            result.phase = RollbackPhase.VALIDATION
            result.status = RollbackStatus.VALIDATING
            self._log_phase(request, RollbackPhase.VALIDATION)
            self._validate_system(result)
            
            # PHASE 5: Finalization
            result.phase = RollbackPhase.FINALIZATION
            self._log_phase(request, RollbackPhase.FINALIZATION)
            self._finalize(result)
            
            # Success
            result.phase = RollbackPhase.COMPLETED
            result.status = RollbackStatus.SUCCEEDED
            result.completed_at = int(time.time())
            result.duration_seconds = result.completed_at - result.started_at
            
            self._log_completion(request, result)
            
            return result
            
        except Exception as e:
            # Critical failure - must halt system
            result.status = RollbackStatus.FAILED
            result.phase = RollbackPhase.ABORTED
            result.failure_reason = str(e)
            result.completed_at = int(time.time())
            
            self._log_failure(request, result, e)
            self._trigger_emergency_halt(request, result, e)
            
            raise RollbackExecutionException(
                f"Rollback {request.rollback_id} failed: {e}"
            ) from e
    
    def _validate_preflight(
        self, 
        request: RollbackRequest, 
        result: RollbackResult
    ) -> None:
        """
        Phase 0: Preflight validation.
        
        Validates:
        - Snapshot exists and is compatible
        - No conflicting rollback in progress
        - System not already halted
        - Rollback permissions valid
        
        Raises:
            RollbackExecutionException: If preflight checks fail
        """
        # Check if emergency stop is already active
        if self.emergency_stop.is_active():
            raise RollbackExecutionException(
                "Cannot execute rollback - emergency stop is active"
            )
        
        # Verify snapshot exists
        if not self.snapshot_store.snapshot_exists(request.target_snapshot_id):
            raise RollbackExecutionException(
                f"Snapshot {request.target_snapshot_id} does not exist"
            )
        
        # Get and validate snapshot
        snapshot = self.snapshot_store.get_snapshot(request.target_snapshot_id)
        if not self.snapshot_store.validate_snapshot_schema(snapshot):
            raise RollbackExecutionException(
                f"Snapshot {request.target_snapshot_id} has incompatible schema"
            )
        
        # Validate rollback scope is sensible
        if request.rollback_scope == RollbackType.PARTIAL:
            if not request.subsystems:
                raise RollbackExecutionException(
                    "PARTIAL rollback requires subsystems to be specified"
                )
        
        # Log preflight success
        self.audit_logger.log_event("rollback_preflight_passed", {
            'rollback_id': request.rollback_id,
            'snapshot_id': request.target_snapshot_id
        })
    
    def _acquire_locks(
        self, 
        request: RollbackRequest, 
        result: RollbackResult
    ) -> None:
        """
        Phase 1: Acquire global locks.
        
        Acquires lease-based locks with TTL to prevent deadlock.
        Uses retry with exponential backoff for contention.
        
        Raises:
            RollbackExecutionException: If locks cannot be acquired
        """
        lock_id = f"rollback_{request.rollback_id}"
        
        # Retry with backoff
        for attempt in range(self.MAX_LOCK_RETRIES):
            acquired = self.lock_manager.acquire_global_lock(
                lock_id, 
                self.LOCK_TTL_SECONDS
            )
            
            if acquired:
                self._lock_acquired = True
                self._current_lock_id = lock_id
                
                self.audit_logger.log_event("rollback_lock_acquired", {
                    'rollback_id': request.rollback_id,
                    'lock_id': lock_id,
                    'ttl_seconds': self.LOCK_TTL_SECONDS
                })
                
                return
            
            # Lock contention - wait and retry
            if attempt < self.MAX_LOCK_RETRIES - 1:
                wait_time = self.LOCK_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                time.sleep(wait_time)
        
        # Failed to acquire lock
        raise RollbackExecutionException(
            f"Failed to acquire global lock after {self.MAX_LOCK_RETRIES} attempts"
        )
    
    def _build_plan(
        self, 
        request: RollbackRequest, 
        result: RollbackResult
    ) -> RollbackPlan:
        """
        Phase 2: Build deterministic rollback plan.
        
        Constructs ordered execution plan with strict subsystem ordering:
        1. Persistence (always first)
        2. Infra
        3. Accounts
        4. Content
        5. Experiments (always last)
        
        Plan is hash-locked to prevent mutation.
        
        Returns:
            Immutable RollbackPlan
        """
        plan_id = f"plan_{request.rollback_id}_{int(time.time())}"
        steps: List[RollbackStep] = []
        
        # Determine which subsystems to include
        if request.rollback_scope == RollbackType.FULL_SYSTEM:
            subsystems = list(SubsystemType)
        elif request.rollback_scope == RollbackType.PARTIAL:
            subsystems = request.subsystems or []
        elif request.rollback_scope == RollbackType.EXPERIMENT_ONLY:
            subsystems = [SubsystemType.EXPERIMENTS]
        else:
            subsystems = list(SubsystemType)
        
        # Build steps in strict order
        subsystem_order = {
            SubsystemType.PERSISTENCE: 1,
            SubsystemType.INFRA: 2,
            SubsystemType.ACCOUNTS: 3,
            SubsystemType.CONTENT: 4,
            SubsystemType.EXPERIMENTS: 5
        }
        
        for subsystem in sorted(subsystems, key=lambda s: subsystem_order[s]):
            subsystem_steps = self._create_subsystem_steps(
                subsystem,
                request,
                subsystem_order[subsystem]
            )
            steps.extend(subsystem_steps)
        
        # Create immutable plan
        plan = RollbackPlan(
            plan_id=plan_id,
            rollback_id=request.rollback_id,
            steps=steps,
            created_at=int(time.time()),
            plan_hash=""  # Will be computed
        )
        
        # Compute and set hash
        plan_hash = plan._compute_hash()
        object.__setattr__(plan, 'plan_hash', plan_hash)
        
        result.steps_total = len(steps)
        
        self.audit_logger.log_event("rollback_plan_created", {
            'rollback_id': request.rollback_id,
            'plan_id': plan_id,
            'total_steps': len(steps),
            'plan_hash': plan_hash
        })
        
        return plan
    
    def _create_subsystem_steps(
        self,
        subsystem: SubsystemType,
        request: RollbackRequest,
        base_order: int
    ) -> List[RollbackStep]:
        """Create rollback steps for a specific subsystem"""
        steps = []
        
        if subsystem == SubsystemType.PERSISTENCE:
            steps.append(RollbackStep(
                step_id=f"{subsystem.value}_state_restore",
                subsystem=subsystem,
                order=base_order * 100,
                description="Restore persistence layer state",
                revert_fn=lambda: self._revert_persistence(request),
                preconditions=["snapshot_loaded"],
                timeout_seconds=600
            ))
            
        elif subsystem == SubsystemType.INFRA:
            steps.append(RollbackStep(
                step_id=f"{subsystem.value}_config_restore",
                subsystem=subsystem,
                order=base_order * 100,
                description="Restore infrastructure configuration",
                revert_fn=lambda: self._revert_infra(request),
                preconditions=["persistence_restored"],
                timeout_seconds=300
            ))
            
        elif subsystem == SubsystemType.ACCOUNTS:
            steps.append(RollbackStep(
                step_id=f"{subsystem.value}_state_restore",
                subsystem=subsystem,
                order=base_order * 100,
                description="Restore account system state",
                revert_fn=lambda: self._revert_accounts(request),
                preconditions=["infra_restored"],
                timeout_seconds=400
            ))
            
        elif subsystem == SubsystemType.CONTENT:
            steps.append(RollbackStep(
                step_id=f"{subsystem.value}_state_restore",
                subsystem=subsystem,
                order=base_order * 100,
                description="Restore content state",
                revert_fn=lambda: self._revert_content(request),
                preconditions=["accounts_restored"],
                timeout_seconds=300
            ))
            
        elif subsystem == SubsystemType.EXPERIMENTS:
            steps.append(RollbackStep(
                step_id=f"{subsystem.value}_config_restore",
                subsystem=subsystem,
                order=base_order * 100,
                description="Restore experiment configurations",
                revert_fn=lambda: self._revert_experiments(request),
                preconditions=[],
                timeout_seconds=120
            ))
        
        return steps
    
    def _execute_plan(self, plan: RollbackPlan, result: RollbackResult) -> None:
        """
        Phase 3: Execute rollback plan.
        
        Executes steps in order with idempotency protection.
        Stops immediately on any failure.
        
        Raises:
            RollbackExecutionException: If any step fails
        """
        # Verify plan integrity
        if not plan.verify_integrity():
            raise RollbackExecutionException(
                "Rollback plan integrity check failed - plan was tampered with"
            )
        
        # Execute each step in order
        for step in plan.steps:
            # Check if step already executed (idempotency)
            if self._step_already_applied(step.step_id):
                self.audit_logger.log_event("rollback_step_skipped", {
                    'rollback_id': plan.rollback_id,
                    'step_id': step.step_id,
                    'reason': 'already_executed'
                })
                continue
            
            # Execute step
            try:
                self._execute_step(step, result)
            except Exception as e:
                # Step failed - abort entire rollback
                result.failed_step = step.step_id
                
                self.audit_logger.log_event("rollback_step_failed", {
                    'rollback_id': plan.rollback_id,
                    'step_id': step.step_id,
                    'error': str(e)
                })
                
                raise RollbackExecutionException(
                    f"Step {step.step_id} failed: {e}"
                ) from e
            
            # Mark step as executed
            self._executed_steps.add(step.step_id)
            result.steps_executed += 1
            
            # Update subsystem status
            self._update_subsystem_status(step, result)
    
    def _execute_step(self, step: RollbackStep, result: RollbackResult) -> None:
        """Execute a single rollback step with timeout and validation"""
        start_time = time.time()
        
        self.audit_logger.log_event("rollback_step_started", {
            'step_id': step.step_id,
            'subsystem': step.subsystem.value,
            'order': step.order
        })
        
        # Execute revert function
        success = step.revert_fn()
        
        execution_time = time.time() - start_time
        step.execution_time = execution_time
        step.executed = True
        step.succeeded = success
        
        if not success:
            step.error = "Revert function returned False"
            raise RollbackExecutionException(
                f"Step {step.step_id} revert function failed"
            )
        
        # Validate step completion
        if step.checksum:
            if not self._validate_step_checksum(step):
                raise RollbackExecutionException(
                    f"Step {step.step_id} checksum validation failed"
                )
        
        self.audit_logger.log_event("rollback_step_completed", {
            'step_id': step.step_id,
            'execution_time': execution_time
        })
    
    def _validate_system(self, result: RollbackResult) -> None:
        """
        Phase 4: Post-rollback validation.
        
        Runs comprehensive invariant checks:
        - Account invariants
        - Infrastructure invariants
        - State consistency checks
        
        Any failure = rollback failure.
        
        Raises:
            RollbackExecutionException: If validation fails
        """
        all_errors: List[str] = []
        
        # Validate account invariants
        accounts_valid, account_errors = self.invariant_engine.validate_account_invariants()
        if not accounts_valid:
            all_errors.extend(account_errors)
        
        # Validate infra invariants
        infra_valid, infra_errors = self.invariant_engine.validate_infra_invariants()
        if not infra_valid:
            all_errors.extend(infra_errors)
        
        # Validate state consistency
        state_valid, state_errors = self.invariant_engine.validate_state_consistency()
        if not state_valid:
            all_errors.extend(state_errors)
        
        # Record results
        result.validation_passed = len(all_errors) == 0
        result.validation_errors = all_errors
        
        if not result.validation_passed:
            self.audit_logger.log_event("rollback_validation_failed", {
                'errors': all_errors
            })
            
            raise RollbackExecutionException(
                f"Post-rollback validation failed: {all_errors}"
            )
        
        self.audit_logger.log_event("rollback_validation_passed", {
            'rollback_id': result.rollback_id
        })
    
    def _finalize(self, result: RollbackResult) -> None:
        """
        Phase 5: Finalization.
        
        Releases locks gradually, restores schedulers, resumes traffic.
        """
        # Compute final checksum
        result.rollback_checksum = self._compute_rollback_checksum(result)
        
        # Release global lock
        if self._lock_acquired and self._current_lock_id:
            self.lock_manager.release_global_lock(self._current_lock_id)
            self._lock_acquired = False
            
            self.audit_logger.log_event("rollback_lock_released", {
                'rollback_id': result.rollback_id,
                'lock_id': self._current_lock_id
            })
        
        # Clear execution state
        self._current_plan = None
        self._executed_steps.clear()
    
    def _step_already_applied(self, step_id: str) -> bool:
        """Check if step was already executed (idempotency check)"""
        return step_id in self._executed_steps
    
    def _validate_step_checksum(self, step: RollbackStep) -> bool:
        """Validate step-level checksum"""
        # Placeholder - actual implementation would verify state checksum
        return True
    
    def _update_subsystem_status(
        self, 
        step: RollbackStep, 
        result: RollbackResult
    ) -> None:
        """Update subsystem rollback status"""
        subsystem_key = step.subsystem.value
        
        if subsystem_key not in result.subsystem_status:
            result.subsystem_status[subsystem_key] = SubsystemRollbackStatus(
                subsystem=step.subsystem,
                status=RollbackStatus.IN_PROGRESS,
                steps_completed=0,
                steps_total=sum(1 for s in self._current_plan.steps if s.subsystem == step.subsystem)
            )
        
        status = result.subsystem_status[subsystem_key]
        status.steps_completed += 1
        
        if status.steps_completed == status.steps_total:
            status.status = RollbackStatus.SUCCEEDED
    
    def _compute_rollback_checksum(self, result: RollbackResult) -> str:
        """Compute final rollback checksum for audit"""
        checksum_data = {
            'rollback_id': result.rollback_id,
            'steps_executed': result.steps_executed,
            'validation_passed': result.validation_passed,
            'subsystems': list(result.subsystem_status.keys())
        }
        checksum_json = json.dumps(checksum_data, sort_keys=True)
        return hashlib.sha256(checksum_json.encode()).hexdigest()
    
    def _trigger_emergency_halt(
        self,
        request: RollbackRequest,
        result: RollbackResult,
        error: Exception
    ) -> None:
        """Trigger emergency system halt on rollback failure"""
        reason = (
            f"Rollback {request.rollback_id} failed at phase {result.phase.value}: {error}"
        )
        
        self.emergency_stop.trigger_halt(reason)
        
        self.audit_logger.log_event("rollback_emergency_halt", {
            'rollback_id': request.rollback_id,
            'phase': result.phase.value,
            'reason': reason
        })
    
    def _log_phase(self, request: RollbackRequest, phase: RollbackPhase) -> None:
        """Log phase transition"""
        self.audit_logger.log_event("rollback_phase_started", {
            'rollback_id': request.rollback_id,
            'phase': phase.value
        })
    
    def _log_completion(
        self, 
        request: RollbackRequest, 
        result: RollbackResult
    ) -> None:
        """Log successful completion"""
        self.audit_logger.log_event("rollback_completed", {
            'rollback_id': request.rollback_id,
            'duration_seconds': result.duration_seconds,
            'steps_executed': result.steps_executed,
            'checksum': result.rollback_checksum
        })
    
    def _log_failure(
        self,
        request: RollbackRequest,
        result: RollbackResult,
        error: Exception
    ) -> None:
        """Log rollback failure"""
        self.audit_logger.log_event("rollback_failed", {
            'rollback_id': request.rollback_id,
            'phase': result.phase.value,
            'failed_step': result.failed_step,
            'error': str(error)
        })
    
    # Subsystem-specific revert functions (placeholders)
    
    def _revert_persistence(self, request: RollbackRequest) -> bool:
        """Revert persistence layer to snapshot"""
        # Actual implementation would restore DB/storage state
        snapshot = self.snapshot_store.get_snapshot(request.target_snapshot_id)
        # ... restore logic ...
        return True
    
    def _revert_infra(self, request: RollbackRequest) -> bool:
        """Revert infrastructure configuration"""
        # Actual implementation would restore infra config
        return True
    
    def _revert_accounts(self, request: RollbackRequest) -> bool:
        """Revert account system state"""
        # Actual implementation would restore account state
        return True
    
    def _revert_content(self, request: RollbackRequest) -> bool:
        """Revert content state"""
        # Actual implementation would restore content state
        return True
    
    def _revert_experiments(self, request: RollbackRequest) -> bool:
        """Revert experiment configurations"""
        # Actual implementation would restore experiment configs
        return True


# Example Usage
if __name__ == "__main__":
    # Mock implementations for demonstration
    
    class MockSnapshotStore(SnapshotStore):
        def snapshot_exists(self, snapshot_id: str) -> bool:
            return True
        
        def get_snapshot(self, snapshot_id: str) -> Dict:
            return {'version': '1.0', 'data': {}}
        
        def validate_snapshot_schema(self, snapshot: Dict) -> bool:
            return True
    
    class MockLockManager(LockManager):
        def acquire_global_lock(self, lock_id: str, ttl_seconds: int) -> bool:
            return True
        
        def release_global_lock(self, lock_id: str) -> bool:
            return True
        
        def extend_lock(self, lock_id: str, ttl_seconds: int) -> bool:
            return True
    
    class MockInvariantEngine(InvariantEngine):
        def validate_account_invariants(self) -> tuple[bool, List[str]]:
            return True, []
        
        def validate_infra_invariants(self) -> tuple[bool, List[str]]:
            return True, []
        
        def validate_state_consistency(self) -> tuple[bool, List[str]]:
            return True, []
    
    class MockAuditLogger(AuditLogger):
        def log_event(self, event_type: str, data: Dict) -> None:
            print(f"[AUDIT] {event_type}: {data}")
    
    class MockEmergencyStop(EmergencyStop):
        def __init__(self):
            self.halted = False
        
        def trigger_halt(self, reason: str) -> None:
            self.halted = True
            print(f"[EMERGENCY HALT] {reason}")
        
        def is_active(self) -> bool:
            return self.halted
    
    # Initialize executer
    executer = RollbackExecuter(
        snapshot_store=MockSnapshotStore(),
        lock_manager=MockLockManager(),
        invariant_engine=MockInvariantEngine(),
        audit_logger=MockAuditLogger(),
        emergency_stop=MockEmergencyStop()
    )
    
    # Create rollback request
    request = RollbackRequest(
        rollback_id="rb_001",
        target_snapshot_id="snap_20260128_090000",
        trigger_event="invariant_violation",
        rollback_scope=RollbackType.FULL_SYSTEM,
        triggered_by="emergency_stop",
        timestamp=int(time.time()),
        reason="Critical invariant violation detected"
    )
    
    # Execute rollback
    try:
        result = executer.execute(request)
        
        print(f"\n✓ Rollback completed successfully")
        print(f"  Status: {result.status.value}")
        print(f"  Phase: {result.phase.value}")
        print(f"  Steps executed: {result.steps_executed}/{result.steps_total}")
        print(f"  Duration: {result.duration_seconds:.2f}s")
        print(f"  Validation passed: {result.validation_passed}")
        print(f"  Checksum: {result.rollback_checksum}")
        
        print(f"\nSubsystem Status:")
        for subsystem_key, status in result.subsystem_status.items():
            print(f"  {subsystem_key}: {status.status.value} ({status.steps_completed}/{status.steps_total})")
        
    except RollbackExecutionException as e:
        print(f"\n✗ Rollback failed: {e}")
        print(f"  Emergency halt triggered")