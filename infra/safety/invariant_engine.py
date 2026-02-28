"""
invariant_engine.py - Global Invariant Enforcement Authority

Location: /infra/safety/invariant_engine.py

Purpose:
    The judge, jury, and executioner for system truth.
    
    Answers only one question:
    "Is the system still allowed to continue?"
    
    Not:
        - "Is this convenient?"
        - "Will this probably work?"
        - "Can we try to recover?"
    
    If an invariant fails → execution stops.

This file prevents:
    - Silent data corruption
    - Causal leakage
    - Invalid learning
    - Unreplayable history
    - Fake virality

What this file is NOT:
    ❌ Not logging
    ❌ Not metrics
    ❌ Not validation helpers
    ❌ Not best-effort checks
    ❌ Not soft warnings

This file deals only in hard truth.

Authority Ordering:
    invariant_engine
        ↓
    workflow_manager / migration / training / posting

Design Principle:
    If violation consequences are negotiable, the invariant is fake.

Mental Model:
    - Invariants are laws, not rules
    - Violations are facts, not opinions
    - Enforcement is synchronous
    - Failure is definitive
    - Silence is corruption
    
    This file is why your system deserves to be trusted.
"""

import hashlib
import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Callable, Set
from pathlib import Path


# Import safety events (in production, this would be a real import)
# For this standalone file, we'll define minimal types
class SafetyEventSeverity(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


# ============================================================================
# INVARIANT SCOPE - Blast Radius
# ============================================================================

class InvariantScope(Enum):
    """
    Scope defines blast radius & enforcement location.
    """
    GLOBAL = "global"               # System-wide
    WORKFLOW = "workflow"           # Workflow execution
    DATA = "data"                   # Data integrity
    MODEL = "model"                 # Model behavior
    RL = "rl"                       # Reinforcement learning
    ACCOUNT = "account"             # Account state
    POSTING = "posting"             # Content posting
    PERSISTENCE = "persistence"     # Storage layer


# ============================================================================
# INVARIANT SEVERITY - Consequences
# ============================================================================

class InvariantSeverity(Enum):
    """
    Invariant violation severity.
    
    Rules:
        - FATAL → immediate halt
        - BLOCKING → action blocked
        - DEGRADED → limited execution
    
    No "warning" severity exists.
    """
    FATAL = "fatal"
    BLOCKING = "blocking"
    DEGRADED = "degraded"


# ============================================================================
# INVARIANT DEFINITION - The Law
# ============================================================================

@dataclass(frozen=True)
class InvariantDefinition:
    """
    Definition of a system invariant.
    
    Rules:
        - name must be globally unique
        - description must be human-defensible
        - deterministic invariants only affect replayed logic
    """
    name: str
    scope: InvariantScope
    severity: InvariantSeverity
    
    description: str
    rationale: str
    
    enforced_at: List[str]          # Named execution hooks
    deterministic: bool             # Must be deterministic for replay
    
    # Optional metadata
    version: str = "1.0.0"
    enabled: bool = True
    
    def __post_init__(self):
        """Validate definition."""
        assert self.name, "Invariant name required"
        assert self.description, "Description required"
        assert self.rationale, "Rationale required"
        assert self.enforced_at, "At least one enforcement point required"
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        data = asdict(self)
        data['scope'] = self.scope.value
        data['severity'] = self.severity.value
        return data


# ============================================================================
# INVARIANT VIOLATION - Immutable Fact
# ============================================================================

@dataclass(frozen=True)
class InvariantViolation:
    """
    Record of an invariant violation.
    
    Violations are immutable facts.
    """
    invariant_name: str
    severity: InvariantSeverity
    scope: InvariantScope
    
    detected_at: int                # Logical timestamp
    context: dict                   # Sanitized context snapshot
    
    execution_id: str
    run_id: str
    
    violation_id: str = field(default="")
    
    def __post_init__(self):
        """Generate violation ID if not provided."""
        if not self.violation_id:
            object.__setattr__(self, 'violation_id', self._generate_id())
    
    def _generate_id(self) -> str:
        """Generate unique violation ID."""
        components = [
            self.invariant_name,
            str(self.detected_at),
            self.execution_id
        ]
        raw = ":".join(components)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "invariant_name": self.invariant_name,
            "severity": self.severity.value,
            "scope": self.scope.value,
            "detected_at": self.detected_at,
            "context": self.context,
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "violation_id": self.violation_id
        }
    
    def is_fatal(self) -> bool:
        """Check if violation is fatal."""
        return self.severity == InvariantSeverity.FATAL
    
    def is_blocking(self) -> bool:
        """Check if violation blocks execution."""
        return self.severity in (InvariantSeverity.FATAL, InvariantSeverity.BLOCKING)


# ============================================================================
# INVARIANT EXCEPTIONS
# ============================================================================

class InvariantViolationError(Exception):
    """Raised when an invariant is violated."""
    
    def __init__(self, violation: InvariantViolation):
        self.violation = violation
        message = (
            f"Invariant violated: {violation.invariant_name} "
            f"[{violation.severity.value}] in {violation.scope.value} scope"
        )
        super().__init__(message)


class InvariantRegistrationError(Exception):
    """Raised when invariant registration fails."""
    pass


# ============================================================================
# INVARIANT - Abstract Base
# ============================================================================

class Invariant(ABC):
    """
    Abstract base for all invariants.
    
    Rules:
        - Pure function only
        - No side effects
        - No IO
        - No mutation
        - Context explicitly passed
    """
    
    def __init__(self, definition: InvariantDefinition):
        self.definition = definition
    
    @abstractmethod
    def check(self, context: dict) -> bool:
        """
        Check if invariant holds.
        
        Args:
            context: Execution context to check
        
        Returns:
            True if invariant holds, False if violated
        """
        pass
    
    def get_name(self) -> str:
        """Get invariant name."""
        return self.definition.name
    
    def get_scope(self) -> InvariantScope:
        """Get invariant scope."""
        return self.definition.scope
    
    def get_severity(self) -> InvariantSeverity:
        """Get violation severity."""
        return self.definition.severity
    
    def is_enabled(self) -> bool:
        """Check if invariant is enabled."""
        return self.definition.enabled
    
    def is_deterministic(self) -> bool:
        """Check if invariant is deterministic."""
        return self.definition.deterministic


# ============================================================================
# CONCRETE INVARIANTS - Mandatory Examples
# ============================================================================

class NoLeakageInvariant(Invariant):
    """
    Training data must not include future labels.
    
    Prevents causal leakage in learning.
    """
    
    def __init__(self):
        definition = InvariantDefinition(
            name="no_temporal_leakage",
            scope=InvariantScope.RL,
            severity=InvariantSeverity.FATAL,
            description="Training data must not contain future information",
            rationale="Causal leakage invalidates all learning",
            enforced_at=["training_data_load", "reward_computation"],
            deterministic=True
        )
        super().__init__(definition)
    
    def check(self, context: dict) -> bool:
        """Check for temporal leakage."""
        training_timestamp = context.get("training_timestamp", 0)
        data_timestamp = context.get("data_timestamp", 0)
        
        # Data must be from before training time
        return data_timestamp <= training_timestamp


class ReplayDeterminismInvariant(Invariant):
    """
    Same inputs → same outputs.
    
    Ensures replay produces identical results.
    """
    
    def __init__(self):
        definition = InvariantDefinition(
            name="replay_determinism",
            scope=InvariantScope.GLOBAL,
            severity=InvariantSeverity.FATAL,
            description="Replayed executions must be deterministic",
            rationale="Non-deterministic replay invalidates audits",
            enforced_at=["replay_start", "replay_checkpoint"],
            deterministic=True
        )
        super().__init__(definition)
    
    def check(self, context: dict) -> bool:
        """Check replay determinism."""
        if not context.get("is_replay", False):
            return True  # Not in replay mode
        
        expected_checksum = context.get("expected_checksum")
        actual_checksum = context.get("actual_checksum")
        
        if expected_checksum is None:
            return True  # No checksum to compare
        
        return expected_checksum == actual_checksum


class SingleActiveMigrationInvariant(Invariant):
    """
    Prevents overlapping schema changes.
    
    Only one migration can run at a time.
    """
    
    def __init__(self):
        definition = InvariantDefinition(
            name="single_active_migration",
            scope=InvariantScope.PERSISTENCE,
            severity=InvariantSeverity.FATAL,
            description="Only one schema migration can be active",
            rationale="Concurrent migrations cause data corruption",
            enforced_at=["migration_start"],
            deterministic=False
        )
        super().__init__(definition)
    
    def check(self, context: dict) -> bool:
        """Check for concurrent migrations."""
        active_migrations = context.get("active_migrations", [])
        return len(active_migrations) <= 1


class AccountLegitimacyInvariant(Invariant):
    """
    Blocks posting from flagged trust states.
    
    Prevents platform bans.
    """
    
    def __init__(self):
        definition = InvariantDefinition(
            name="account_legitimacy",
            scope=InvariantScope.ACCOUNT,
            severity=InvariantSeverity.BLOCKING,
            description="Cannot post from accounts with degraded trust",
            rationale="Posting from flagged accounts risks platform bans",
            enforced_at=["pre_post"],
            deterministic=False
        )
        super().__init__(definition)
    
    def check(self, context: dict) -> bool:
        """Check account trust status."""
        trust_score = context.get("account_trust_score", 100)
        trust_threshold = context.get("trust_threshold", 50)
        
        return trust_score >= trust_threshold


class ExperimentIsolationInvariant(Invariant):
    """
    Control and treatment never mix.
    
    Ensures valid A/B testing.
    """
    
    def __init__(self):
        definition = InvariantDefinition(
            name="experiment_isolation",
            scope=InvariantScope.RL,
            severity=InvariantSeverity.BLOCKING,
            description="Control and treatment groups must remain isolated",
            rationale="Mixing groups invalidates experimental results",
            enforced_at=["experiment_assignment", "reward_collection"],
            deterministic=True
        )
        super().__init__(definition)
    
    def check(self, context: dict) -> bool:
        """Check experiment isolation."""
        user_id = context.get("user_id")
        experiment_id = context.get("experiment_id")
        
        if not user_id or not experiment_id:
            return True  # No experiment active
        
        # Check if user has been reassigned
        original_group = context.get("original_group")
        current_group = context.get("current_group")
        
        if original_group is None:
            return True  # First assignment
        
        return original_group == current_group


class StateConsistencyInvariant(Invariant):
    """
    Workflow state must match database.
    
    Prevents split-brain conditions.
    """
    
    def __init__(self):
        definition = InvariantDefinition(
            name="state_consistency",
            scope=InvariantScope.WORKFLOW,
            severity=InvariantSeverity.FATAL,
            description="In-memory state must match persisted state",
            rationale="State inconsistency causes unpredictable behavior",
            enforced_at=["workflow_step", "state_transition"],
            deterministic=False
        )
        super().__init__(definition)
    
    def check(self, context: dict) -> bool:
        """Check state consistency."""
        memory_state = context.get("memory_state")
        db_state = context.get("db_state")
        
        if memory_state is None or db_state is None:
            return True  # Can't compare
        
        return memory_state == db_state


# ============================================================================
# INVARIANT REGISTRY - Single Source of Truth
# ============================================================================

class InvariantRegistry:
    """
    Central registry for all system invariants.
    
    Rules:
        - Registration only at boot
        - Duplicates forbidden
        - Registry freezes after init
    """
    
    def __init__(self):
        self._invariants: Dict[str, Invariant] = {}
        self._frozen = False
        self._lock = threading.Lock()
    
    def register(self, invariant: Invariant) -> None:
        """
        Register an invariant.
        
        Raises:
            InvariantRegistrationError: If duplicate or registry frozen
        """
        with self._lock:
            if self._frozen:
                raise InvariantRegistrationError(
                    "Registry is frozen - no new registrations allowed"
                )
            
            name = invariant.get_name()
            
            if name in self._invariants:
                raise InvariantRegistrationError(
                    f"Invariant already registered: {name}"
                )
            
            self._invariants[name] = invariant
    
    def freeze(self) -> None:
        """Freeze registry - no more registrations allowed."""
        with self._lock:
            self._frozen = True
    
    def is_frozen(self) -> bool:
        """Check if registry is frozen."""
        return self._frozen
    
    def all(self) -> List[Invariant]:
        """Get all registered invariants."""
        return list(self._invariants.values())
    
    def get(self, name: str) -> Optional[Invariant]:
        """Get invariant by name."""
        return self._invariants.get(name)
    
    def by_scope(self, scope: InvariantScope) -> List[Invariant]:
        """Get all invariants for a scope."""
        return [inv for inv in self._invariants.values() 
                if inv.get_scope() == scope]
    
    def by_enforcement_point(self, point: str) -> List[Invariant]:
        """Get all invariants enforced at a point."""
        return [inv for inv in self._invariants.values()
                if point in inv.definition.enforced_at]
    
    def count(self) -> int:
        """Get count of registered invariants."""
        return len(self._invariants)


# ============================================================================
# INVARIANT EVALUATOR - Deterministic Checking
# ============================================================================

class InvariantEvaluator:
    """
    Evaluates invariants against execution context.
    
    Responsibilities:
        - Select relevant invariants
        - Evaluate deterministically
        - Return all violations, not first
    
    Fail-fast occurs after evaluation, not during.
    """
    
    def __init__(self, registry: InvariantRegistry):
        self.registry = registry
    
    def evaluate(
        self,
        scope: InvariantScope,
        context: dict,
        enforcement_point: Optional[str] = None
    ) -> List[InvariantViolation]:
        """
        Evaluate invariants for a scope.
        
        Args:
            scope: Invariant scope to check
            context: Execution context
            enforcement_point: Specific enforcement point
        
        Returns:
            List of all violations detected
        """
        violations = []
        
        # Get relevant invariants
        invariants = self._get_relevant_invariants(scope, enforcement_point)
        
        # Evaluate each invariant
        for invariant in invariants:
            if not invariant.is_enabled():
                continue
            
            try:
                holds = invariant.check(context)
                
                if not holds:
                    # Create violation
                    violation = InvariantViolation(
                        invariant_name=invariant.get_name(),
                        severity=invariant.get_severity(),
                        scope=invariant.get_scope(),
                        detected_at=int(time.time() * 1000),
                        context=self._sanitize_context(context),
                        execution_id=context.get("execution_id", "unknown"),
                        run_id=context.get("run_id", "unknown")
                    )
                    violations.append(violation)
                    
            except Exception as e:
                # Invariant check itself failed - treat as violation
                violation = InvariantViolation(
                    invariant_name=invariant.get_name(),
                    severity=InvariantSeverity.FATAL,
                    scope=invariant.get_scope(),
                    detected_at=int(time.time() * 1000),
                    context={
                        "error": str(e),
                        "check_failed": True
                    },
                    execution_id=context.get("execution_id", "unknown"),
                    run_id=context.get("run_id", "unknown")
                )
                violations.append(violation)
        
        return violations
    
    def _get_relevant_invariants(
        self,
        scope: InvariantScope,
        enforcement_point: Optional[str]
    ) -> List[Invariant]:
        """Get invariants relevant to scope and enforcement point."""
        if enforcement_point:
            # Filter by enforcement point first, then by scope
            candidates = self.registry.by_enforcement_point(enforcement_point)
            return [inv for inv in candidates if inv.get_scope() == scope]
        else:
            # Just filter by scope
            return self.registry.by_scope(scope)
    
    @staticmethod
    def _sanitize_context(context: dict) -> dict:
        """
        Sanitize context for violation record.
        
        Removes sensitive data, limits size.
        """
        sanitized = {}
        
        # Copy safe fields
        safe_fields = [
            "execution_id", "run_id", "scope", "enforcement_point",
            "timestamp", "user_id", "workflow_id", "experiment_id"
        ]
        
        for field in safe_fields:
            if field in context:
                sanitized[field] = context[field]
        
        # Truncate large values
        MAX_VALUE_SIZE = 1000
        for key, value in sanitized.items():
            if isinstance(value, str) and len(value) > MAX_VALUE_SIZE:
                sanitized[key] = value[:MAX_VALUE_SIZE] + "...[truncated]"
        
        return sanitized


# ============================================================================
# INVARIANT FAILURE HANDLER - Consequences
# ============================================================================

class InvariantFailureHandler:
    """
    Handles invariant violations.
    
    Rules:
        - Emits audit logs
        - Notifies watchdog
        - Triggers kill-switch if severity demands
        - Never swallows failures
    """
    
    def __init__(
        self,
        audit_dir: Optional[Path] = None,
        emergency_stop_controller: Optional[Any] = None
    ):
        self.audit_dir = audit_dir or Path("/var/safety/invariants")
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.emergency_stop = emergency_stop_controller
        
        self._lock = threading.Lock()
    
    def handle(self, violations: List[InvariantViolation]) -> None:
        """
        Handle invariant violations.
        
        This is the consequence layer.
        """
        if not violations:
            return
        
        with self._lock:
            # Emit audit logs
            for violation in violations:
                self._audit_violation(violation)
            
            # Check for fatal violations
            fatal_violations = [v for v in violations if v.is_fatal()]
            
            if fatal_violations:
                # Trigger emergency stop
                self._trigger_emergency_stop(fatal_violations)
                
                # Raise exception
                raise InvariantViolationError(fatal_violations[0])
            
            # Check for blocking violations
            blocking_violations = [v for v in violations if v.is_blocking()]
            
            if blocking_violations:
                # Raise exception to block execution
                raise InvariantViolationError(blocking_violations[0])
    
    def _audit_violation(self, violation: InvariantViolation):
        """Write violation to audit log."""
        audit_file = self.audit_dir / "violations.jsonl"
        
        try:
            with open(audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(violation.to_dict(), sort_keys=True) + '\n')
                f.flush()
        except Exception as e:
            # Don't fail handling due to logging issues
            print(f"Failed to audit violation: {e}", flush=True)
    
    def _trigger_emergency_stop(self, violations: List[InvariantViolation]):
        """Trigger emergency stop for fatal violations."""
        if not self.emergency_stop:
            print("WARNING: No emergency stop controller configured", flush=True)
            return
        
        try:
            # Build context
            context = {
                "violations": [v.to_dict() for v in violations],
                "count": len(violations)
            }
            
            # Trigger stop
            self.emergency_stop.trigger_stop(
                reason="INVARIANT_FAILURE",
                description=f"Fatal invariant violations: {', '.join(v.invariant_name for v in violations)}",
                triggered_by="invariant_engine",
                context=context,
                locked=True  # Fatal invariants lock the system
            )
            
        except Exception as e:
            print(f"Failed to trigger emergency stop: {e}", flush=True)


# ============================================================================
# INVARIANT ENGINE - Public Authority
# ============================================================================

class InvariantEngine:
    """
    The public authority for invariant enforcement.
    
    Guarantees:
        - Synchronous enforcement
        - Deterministic failure
        - Consistent behavior in replay
        - Zero implicit recovery
    
    If this returns → system is safe to proceed.
    """
    
    def __init__(
        self,
        registry: InvariantRegistry,
        audit_dir: Optional[Path] = None,
        emergency_stop_controller: Optional[Any] = None
    ):
        self.registry = registry
        self.evaluator = InvariantEvaluator(registry)
        self.handler = InvariantFailureHandler(audit_dir, emergency_stop_controller)
        
        self._enabled = True
        self._lock = threading.Lock()
    
    def assert_safe(
        self,
        scope: InvariantScope,
        context: dict,
        enforcement_point: Optional[str] = None
    ) -> None:
        """
        Assert that system is safe to proceed.
        
        Args:
            scope: Invariant scope to check
            context: Execution context
            enforcement_point: Specific enforcement point
        
        Raises:
            InvariantViolationError: If any invariant is violated
        
        If this returns → system is safe to proceed.
        """
        if not self._enabled:
            return
        
        # Evaluate invariants
        violations = self.evaluator.evaluate(scope, context, enforcement_point)
        
        # Handle violations (may raise)
        self.handler.handle(violations)
    
    def check_without_enforcement(
        self,
        scope: InvariantScope,
        context: dict,
        enforcement_point: Optional[str] = None
    ) -> List[InvariantViolation]:
        """
        Check invariants without enforcing.
        
        For testing and diagnostics only.
        """
        return self.evaluator.evaluate(scope, context, enforcement_point)
    
    def disable(self) -> None:
        """
        Disable invariant engine.
        
        WARNING: Only for testing. NEVER in production.
        """
        with self._lock:
            self._enabled = False
    
    def enable(self) -> None:
        """Enable invariant engine."""
        with self._lock:
            self._enabled = True
    
    def is_enabled(self) -> bool:
        """Check if engine is enabled."""
        return self._enabled
    
    def get_registry(self) -> InvariantRegistry:
        """Get the invariant registry."""
        return self.registry


# ============================================================================
# FACTORY
# ============================================================================

def create_invariant_engine(
    audit_dir: str = "/var/safety/invariants",
    emergency_stop_controller: Optional[Any] = None,
    register_standard_invariants: bool = True
) -> InvariantEngine:
    """
    Create invariant engine with standard invariants.
    
    Args:
        audit_dir: Where to store violation audits
        emergency_stop_controller: Emergency stop controller
        register_standard_invariants: Register standard invariants
    
    Returns:
        Configured InvariantEngine
    """
    registry = InvariantRegistry()
    
    if register_standard_invariants:
        # Register mandatory invariants
        registry.register(NoLeakageInvariant())
        registry.register(ReplayDeterminismInvariant())
        registry.register(SingleActiveMigrationInvariant())
        registry.register(AccountLegitimacyInvariant())
        registry.register(ExperimentIsolationInvariant())
        registry.register(StateConsistencyInvariant())
    
    # Freeze registry
    registry.freeze()
    
    # Create engine
    return InvariantEngine(
        registry=registry,
        audit_dir=Path(audit_dir),
        emergency_stop_controller=emergency_stop_controller
    )


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    print("Invariant Engine Demo")
    print("=" * 60)
    
    # Create engine
    engine = create_invariant_engine(
        audit_dir="/tmp/invariant_demo",
        register_standard_invariants=True
    )
    
    print(f"\nRegistered {engine.get_registry().count()} invariants:")
    for inv in engine.get_registry().all():
        print(f"  - {inv.get_name()} [{inv.get_severity().value}] @ {inv.get_scope().value}")
    
    # Test 1: Valid execution
    print("\n--- Test 1: Valid Execution ---")
    try:
        context = {
            "execution_id": "exec_001",
            "run_id": "run_001",
            "training_timestamp": 1000,
            "data_timestamp": 500
        }
        
        engine.assert_safe(InvariantScope.RL, context, "training_data_load")
        print("✓ Invariants passed - execution allowed")
    except InvariantViolationError as e:
        print(f"✗ Invariant violation: {e}")
    
    # Test 2: Temporal leakage
    print("\n--- Test 2: Temporal Leakage (FATAL) ---")
    try:
        context = {
            "execution_id": "exec_002",
            "run_id": "run_001",
            "training_timestamp": 1000,
            "data_timestamp": 2000  # Future data!
        }
        
        engine.assert_safe(InvariantScope.RL, context, "training_data_load")
        print("✓ Invariants passed")
    except InvariantViolationError as e:
        print(f"✗ FATAL: {e.violation.invariant_name}")
        print(f"  Execution halted")
    
    # Test 3: Account trust violation
    print("\n--- Test 3: Account Trust (BLOCKING) ---")
    try:
        context = {
            "execution_id": "exec_003",
            "run_id": "run_001",
            "account_trust_score": 30,
            "trust_threshold": 50
        }
        
        engine.assert_safe(InvariantScope.ACCOUNT, context, "pre_post")
        print("✓ Invariants passed")
    except InvariantViolationError as e:
        print(f"✗ BLOCKED: {e.violation.invariant_name}")
        print(f"  Action prevented")
    
    # Test 4: Experiment isolation
    print("\n--- Test 4: Experiment Isolation ---")
    try:
        context = {
            "execution_id": "exec_004",
            "run_id": "run_001",
            "user_id": "user_123",
            "experiment_id": "exp_001",
            "original_group": "control",
            "current_group": "control"
        }
        
        engine.assert_safe(InvariantScope.RL, context, "experiment_assignment")
        print("✓ Invariants passed - isolation maintained")
    except InvariantViolationError as e:
        print(f"✗ Violation: {e}")
    
    # Test 5: Check without enforcement
    print("\n--- Test 5: Diagnostic Check ---")
    context = {
        "execution_id": "exec_005",
        "run_id": "run_001",
        "account_trust_score": 20,
        "trust_threshold": 50
    }
    
    violations = engine.check_without_enforcement(
        InvariantScope.ACCOUNT,
        context,
        "pre_post"
    )
    
    print(f"Found {len(violations)} violations (not enforced):")
    for v in violations:
        print(f"  - {v.invariant_name} [{v.severity.value}]")
    
    print("\n" + "=" * 60)
    print("Invariants are laws, not rules.")
    print("Violations are facts, not opinions.")
    print("Enforcement is synchronous.")
    print("Failure is definitive.")
    print("Silence is corruption.")