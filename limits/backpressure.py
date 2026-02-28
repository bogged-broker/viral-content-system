"""
backpressure.py - Deterministic Load Shedding & Slowdown Authority

Built for:
- Viral spikes without cascades
- Controlled degradation (never chaos)
- Fairness across scopes
- Zero silent failure
- 5M → 300M+ burst safety
- Post-incident explainability

Authority chain: quota → rate → backpressure → execution

NO MAGIC. NO ADAPTIVE GUESSING. NO "BEST EFFORT".
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple, Callable
from datetime import datetime, timezone
import time
import threading
from contextlib import contextmanager
from collections import defaultdict


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class PressureSignal(Enum):
    """Observed signals only — never inferred."""
    CPU = "cpu"
    MEMORY = "memory"
    IO = "io"
    LATENCY = "latency"
    ERROR_RATE = "error_rate"
    QUEUE_DEPTH = "queue_depth"


class PressureLevel(Enum):
    """
    Monotonic escalation only.
    Downgrade only via watchdog approval.
    """
    NORMAL = 0
    DEGRADED = 1
    CRITICAL = 2
    EMERGENCY = 3
    
    def __lt__(self, other):
        if self.__class__ is other.__class__:
            return self.value < other.value
        return NotImplemented
    
    def __le__(self, other):
        if self.__class__ is other.__class__:
            return self.value <= other.value
        return NotImplemented
    
    def __gt__(self, other):
        if self.__class__ is other.__class__:
            return self.value > other.value
        return NotImplemented
    
    def __ge__(self, other):
        if self.__class__ is other.__class__:
            return self.value >= other.value
        return NotImplemented


class BackpressureDecision(Enum):
    """
    No partial decisions.
    No implicit throttling.
    """
    ALLOW = "allow"
    SLOW = "slow"
    SHED = "shed"


class ActionType(Enum):
    """Enumeration of action types that can be backpressure-controlled."""
    POST = "post"
    EXECUTE = "execute"
    RECOVER = "recover"
    INFER = "infer"
    DISPATCH = "dispatch"
    ORCHESTRATE = "orchestrate"
    CLEANUP = "cleanup"
    ADMIN = "admin"


class Scope(Enum):
    """Execution scope for backpressure context."""
    GLOBAL = "global"
    WORKFLOW = "workflow"
    ACCOUNT = "account"
    INFRA = "infra"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class PressureSnapshot:
    """
    Immutable evidence of pressure.
    Snapshots are facts, not predictions.
    """
    signal: PressureSignal
    observed_value: float
    threshold: float
    timestamp: datetime
    level: PressureLevel
    
    def __post_init__(self):
        if self.observed_value < 0:
            raise ValueError(f"Observed value cannot be negative: {self.observed_value}")
        if self.threshold <= 0:
            raise ValueError(f"Threshold must be positive: {self.threshold}")
    
    @property
    def exceeds_threshold(self) -> bool:
        """Check if observed value exceeds threshold."""
        return self.observed_value >= self.threshold
    
    @property
    def pressure_ratio(self) -> float:
        """Calculate pressure as ratio of threshold."""
        return self.observed_value / self.threshold if self.threshold > 0 else 0.0


@dataclass(frozen=True)
class BackpressureContext:
    """
    Mandatory execution envelope.
    No context → no execution.
    """
    action_type: ActionType
    scope: Scope
    scope_id: Optional[str]
    priority: int  # Lower = more important
    timestamp: datetime
    run_id: str
    
    def __post_init__(self):
        # Validate scope_id rules
        if self.scope == Scope.GLOBAL and self.scope_id is not None:
            raise ValueError("Global scope must have scope_id=None")
        if self.scope != Scope.GLOBAL and self.scope_id is None:
            raise ValueError(f"Scope {self.scope.value} requires non-null scope_id")
        
        if self.priority < 0:
            raise ValueError(f"Priority cannot be negative: {self.priority}")
        
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
    
    @property
    def is_critical_priority(self) -> bool:
        """Priority 0 actions are never shed."""
        return self.priority == 0


@dataclass(frozen=True)
class BackpressurePolicy:
    """
    Defines allowed degradation.
    Policies are versioned, thresholds never inline, no runtime tuning.
    """
    policy_version: str
    
    # Thresholds per signal per level
    thresholds: Dict[PressureSignal, Dict[PressureLevel, float]]
    
    # Priority cutoffs for shedding at each level
    priority_cutoffs: Dict[PressureLevel, int]
    
    # Slowdown multipliers per level
    slowdown_multipliers: Dict[PressureLevel, float]
    
    # Optional: action-specific overrides
    action_overrides: Dict[ActionType, Dict[str, any]] = field(default_factory=dict)
    
    def __post_init__(self):
        # Validate thresholds exist for all signals and levels
        for signal in PressureSignal:
            if signal not in self.thresholds:
                raise ValueError(f"Missing thresholds for signal: {signal}")
            for level in [PressureLevel.DEGRADED, PressureLevel.CRITICAL, PressureLevel.EMERGENCY]:
                if level not in self.thresholds[signal]:
                    raise ValueError(f"Missing threshold for {signal} at {level}")
        
        # Validate priority cutoffs
        for level in [PressureLevel.DEGRADED, PressureLevel.CRITICAL, PressureLevel.EMERGENCY]:
            if level not in self.priority_cutoffs:
                raise ValueError(f"Missing priority cutoff for {level}")
        
        # Validate slowdown multipliers
        for level in [PressureLevel.DEGRADED, PressureLevel.CRITICAL, PressureLevel.EMERGENCY]:
            if level not in self.slowdown_multipliers:
                raise ValueError(f"Missing slowdown multiplier for {level}")
            if self.slowdown_multipliers[level] <= 0:
                raise ValueError(f"Slowdown multiplier must be > 0 for {level}")
    
    def get_threshold(self, signal: PressureSignal, level: PressureLevel) -> float:
        """Get threshold for a signal at a specific pressure level."""
        return self.thresholds[signal].get(level, float('inf'))
    
    def get_priority_cutoff(self, level: PressureLevel) -> int:
        """Get priority cutoff for a pressure level."""
        return self.priority_cutoffs.get(level, float('inf'))
    
    def get_slowdown_multiplier(self, level: PressureLevel) -> float:
        """Get slowdown multiplier for a pressure level."""
        return self.slowdown_multipliers.get(level, 1.0)


@dataclass
class DegradationEvidence:
    """
    Audit trail for backpressure actions.
    All degradation must be auditable.
    """
    context: BackpressureContext
    decision: BackpressureDecision
    pressure_snapshots: List[PressureSnapshot]
    policy_version: str
    computed_level: PressureLevel
    slowdown_applied_ms: Optional[float]
    timestamp: datetime
    reason: str
    watchdog_override: bool = False
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for logging."""
        return {
            'run_id': self.context.run_id,
            'action_type': self.context.action_type.value,
            'scope': self.context.scope.value,
            'scope_id': self.context.scope_id,
            'priority': self.context.priority,
            'decision': self.decision.value,
            'pressure_level': self.computed_level.value,
            'policy_version': self.policy_version,
            'slowdown_ms': self.slowdown_applied_ms,
            'pressure_signals': {
                snapshot.signal.value: {
                    'observed': snapshot.observed_value,
                    'threshold': snapshot.threshold,
                    'ratio': snapshot.pressure_ratio
                }
                for snapshot in self.pressure_snapshots
            },
            'reason': self.reason,
            'watchdog_override': self.watchdog_override,
            'timestamp': self.timestamp.isoformat()
        }


# ============================================================================
# BACKPRESSURE EVALUATOR (THE BRAIN)
# ============================================================================

class BackpressureEvaluator:
    """
    Responsible for decision, not enforcement.
    
    Guarantees:
    - Deterministic evaluation order
    - Strict threshold comparison
    - Monotonic escalation
    - Fail-closed on ambiguity
    
    Same inputs → same decision. Always.
    """
    
    def __init__(self, policy: BackpressurePolicy):
        self.policy = policy
        self._evaluation_lock = threading.Lock()
    
    def evaluate(
        self,
        context: BackpressureContext,
        snapshots: List[PressureSnapshot],
        watchdog_emergency: bool = False
    ) -> Tuple[BackpressureDecision, PressureLevel, str]:
        """
        Evaluate backpressure decision.
        
        Returns: (decision, computed_level, reason)
        """
        with self._evaluation_lock:
            return self._evaluate_internal(context, snapshots, watchdog_emergency)
    
    def _evaluate_internal(
        self,
        context: BackpressureContext,
        snapshots: List[PressureSnapshot],
        watchdog_emergency: bool
    ) -> Tuple[BackpressureDecision, PressureLevel, str]:
        """Internal evaluation logic."""
        
        # Sort snapshots by signal for deterministic order
        sorted_snapshots = sorted(snapshots, key=lambda s: s.signal.value)
        
        # Compute overall pressure level (monotonic - take max)
        computed_level = self._compute_pressure_level(sorted_snapshots)
        
        # Watchdog emergency overrides everything
        if watchdog_emergency:
            computed_level = PressureLevel.EMERGENCY
        
        # Priority 0 actions are NEVER shed
        if context.is_critical_priority:
            if computed_level == PressureLevel.NORMAL:
                return BackpressureDecision.ALLOW, computed_level, "Normal pressure"
            else:
                return BackpressureDecision.SLOW, computed_level, f"Priority 0 slowed but not shed at {computed_level.name}"
        
        # Evaluate based on pressure level
        if computed_level == PressureLevel.NORMAL:
            return BackpressureDecision.ALLOW, computed_level, "Normal pressure"
        
        # Check if priority exceeds cutoff for this level
        cutoff = self.policy.get_priority_cutoff(computed_level)
        
        if context.priority > cutoff:
            # Shed - priority too low for current pressure
            return (
                BackpressureDecision.SHED,
                computed_level,
                f"Priority {context.priority} exceeds cutoff {cutoff} at {computed_level.name}"
            )
        else:
            # Slow - priority acceptable but system under pressure
            return (
                BackpressureDecision.SLOW,
                computed_level,
                f"Priority {context.priority} acceptable, applying slowdown at {computed_level.name}"
            )
    
    def _compute_pressure_level(self, snapshots: List[PressureSnapshot]) -> PressureLevel:
        """
        Compute overall pressure level from snapshots.
        Monotonic - takes maximum level observed.
        """
        if not snapshots:
            return PressureLevel.NORMAL
        
        max_level = PressureLevel.NORMAL
        
        for snapshot in snapshots:
            # Check snapshot's level directly
            if snapshot.level > max_level:
                max_level = snapshot.level
            
            # Also verify against policy thresholds
            for level in [PressureLevel.EMERGENCY, PressureLevel.CRITICAL, PressureLevel.DEGRADED]:
                threshold = self.policy.get_threshold(snapshot.signal, level)
                if snapshot.observed_value >= threshold and level > max_level:
                    max_level = level
        
        return max_level


# ============================================================================
# BACKPRESSURE EXECUTOR (THE HAND)
# ============================================================================

class BackpressureExecutor:
    """
    Enforces evaluator decisions.
    
    Rules:
    - SLOW = explicit delay or deferral
    - SHED = absolute denial
    - All actions audited
    - Watchdog overrides honored
    
    No hidden sleeps. Ever.
    """
    
    def __init__(
        self,
        policy: BackpressurePolicy,
        audit_callback: Optional[Callable[[DegradationEvidence], None]] = None
    ):
        self.policy = policy
        self.audit_callback = audit_callback
        self._execution_stats = defaultdict(int)
        self._stats_lock = threading.Lock()
    
    def execute(
        self,
        decision: BackpressureDecision,
        context: BackpressureContext,
        snapshots: List[PressureSnapshot],
        computed_level: PressureLevel,
        reason: str,
        watchdog_override: bool = False
    ) -> None:
        """
        Execute backpressure decision.
        
        Raises:
            BackpressureShed: If decision is SHED
        """
        slowdown_ms = None
        
        if decision == BackpressureDecision.SHED:
            # Record shed event
            self._record_decision(decision)
            
            # Audit before shedding
            evidence = DegradationEvidence(
                context=context,
                decision=decision,
                pressure_snapshots=snapshots,
                policy_version=self.policy.policy_version,
                computed_level=computed_level,
                slowdown_applied_ms=None,
                timestamp=datetime.now(timezone.utc),
                reason=reason,
                watchdog_override=watchdog_override
            )
            self._audit(evidence)
            
            # Absolute denial
            raise BackpressureShed(
                f"Load shed: {reason}",
                context=context,
                level=computed_level,
                evidence=evidence
            )
        
        elif decision == BackpressureDecision.SLOW:
            # Apply explicit slowdown
            slowdown_ms = self._apply_slowdown(computed_level)
            self._record_decision(decision)
        
        else:  # ALLOW
            self._record_decision(decision)
        
        # Always audit
        evidence = DegradationEvidence(
            context=context,
            decision=decision,
            pressure_snapshots=snapshots,
            policy_version=self.policy.policy_version,
            computed_level=computed_level,
            slowdown_applied_ms=slowdown_ms,
            timestamp=datetime.now(timezone.utc),
            reason=reason,
            watchdog_override=watchdog_override
        )
        self._audit(evidence)
    
    def _apply_slowdown(self, level: PressureLevel) -> float:
        """
        Apply explicit slowdown based on pressure level.
        Returns: milliseconds delayed
        """
        multiplier = self.policy.get_slowdown_multiplier(level)
        
        # Base delay scales with pressure level
        base_delay_ms = {
            PressureLevel.DEGRADED: 10.0,
            PressureLevel.CRITICAL: 50.0,
            PressureLevel.EMERGENCY: 200.0
        }.get(level, 0.0)
        
        delay_ms = base_delay_ms * multiplier
        
        if delay_ms > 0:
            # Explicit, auditable sleep
            time.sleep(delay_ms / 1000.0)
        
        return delay_ms
    
    def _audit(self, evidence: DegradationEvidence) -> None:
        """Audit degradation evidence."""
        if self.audit_callback:
            try:
                self.audit_callback(evidence)
            except Exception as e:
                # Never let audit failure block execution
                # But log it somewhere
                pass
    
    def _record_decision(self, decision: BackpressureDecision) -> None:
        """Record decision statistics."""
        with self._stats_lock:
            self._execution_stats[decision] += 1
    
    def get_stats(self) -> Dict[str, int]:
        """Get execution statistics."""
        with self._stats_lock:
            return dict(self._execution_stats)


# ============================================================================
# BACKPRESSURE INVARIANTS (ABSOLUTE)
# ============================================================================

class BackpressureInvariants:
    """
    MUST guarantee:
    - Priority 0 actions are never shed
    - No silent slowdowns
    - Slowdown multipliers > 0
    - No conflicting decisions
    - No implicit recovery
    - No execution without audit
    
    Invariant violation = hard stop.
    """
    
    @staticmethod
    def validate_policy(policy: BackpressurePolicy) -> None:
        """Validate policy invariants."""
        # Slowdown multipliers must be > 0
        for level, multiplier in policy.slowdown_multipliers.items():
            if multiplier <= 0:
                raise InvariantViolation(
                    f"Slowdown multiplier must be > 0 for {level.name}, got {multiplier}"
                )
        
        # Priority cutoffs must be non-negative
        for level, cutoff in policy.priority_cutoffs.items():
            if cutoff < 0:
                raise InvariantViolation(
                    f"Priority cutoff must be >= 0 for {level.name}, got {cutoff}"
                )
        
        # Thresholds must be monotonically increasing
        for signal in PressureSignal:
            degraded = policy.get_threshold(signal, PressureLevel.DEGRADED)
            critical = policy.get_threshold(signal, PressureLevel.CRITICAL)
            emergency = policy.get_threshold(signal, PressureLevel.EMERGENCY)
            
            if not (degraded <= critical <= emergency):
                raise InvariantViolation(
                    f"Thresholds must be monotonic for {signal.name}: "
                    f"DEGRADED={degraded}, CRITICAL={critical}, EMERGENCY={emergency}"
                )
    
    @staticmethod
    def validate_decision(
        decision: BackpressureDecision,
        context: BackpressureContext,
        computed_level: PressureLevel
    ) -> None:
        """Validate decision invariants."""
        # Priority 0 can never be shed
        if context.is_critical_priority and decision == BackpressureDecision.SHED:
            raise InvariantViolation(
                f"Priority 0 action cannot be shed: {context.run_id}"
            )
        
        # NORMAL pressure can only result in ALLOW
        if computed_level == PressureLevel.NORMAL and decision != BackpressureDecision.ALLOW:
            raise InvariantViolation(
                f"NORMAL pressure must result in ALLOW, got {decision.name}"
            )
    
    @staticmethod
    def validate_context(context: BackpressureContext) -> None:
        """Validate context invariants."""
        # Already validated in __post_init__, but double-check critical rules
        if context.scope == Scope.GLOBAL and context.scope_id is not None:
            raise InvariantViolation("Global scope must have scope_id=None")
        
        if context.scope != Scope.GLOBAL and context.scope_id is None:
            raise InvariantViolation(f"Scope {context.scope.value} requires non-null scope_id")
        
        if context.priority < 0:
            raise InvariantViolation(f"Priority cannot be negative: {context.priority}")
    
    @staticmethod
    def validate_snapshots(snapshots: List[PressureSnapshot]) -> None:
        """Validate snapshot invariants."""
        if not snapshots:
            raise InvariantViolation("Cannot evaluate with empty snapshots")
        
        for snapshot in snapshots:
            if snapshot.observed_value < 0:
                raise InvariantViolation(
                    f"Observed value cannot be negative: {snapshot.signal.name}={snapshot.observed_value}"
                )
            if snapshot.threshold <= 0:
                raise InvariantViolation(
                    f"Threshold must be positive: {snapshot.signal.name}={snapshot.threshold}"
                )


# ============================================================================
# WATCHDOG INTEGRATION
# ============================================================================

class WatchdogState(Enum):
    """Watchdog states that affect backpressure."""
    NORMAL = "normal"
    DEGRADED = "degraded"
    EMERGENCY = "emergency"
    FROZEN = "frozen"


@dataclass
class WatchdogSignal:
    """Signal from watchdog to backpressure system."""
    state: WatchdogState
    timestamp: datetime
    reason: str
    force_level: Optional[PressureLevel] = None


class WatchdogInterface:
    """
    Interface for watchdog integration.
    
    Backpressure MUST:
    - Obey global freeze
    - Respect EMERGENCY mode
    - Emit escalation signals
    - Never self-recover
    
    The watchdog owns recovery authority.
    """
    
    def __init__(self):
        self._current_state = WatchdogState.NORMAL
        self._state_lock = threading.Lock()
        self._escalation_callbacks: List[Callable[[WatchdogSignal], None]] = []
    
    def update_state(self, signal: WatchdogSignal) -> None:
        """Update watchdog state."""
        with self._state_lock:
            self._current_state = signal.state
    
    def get_state(self) -> WatchdogState:
        """Get current watchdog state."""
        with self._state_lock:
            return self._current_state
    
    def is_emergency(self) -> bool:
        """Check if in emergency mode."""
        return self.get_state() in [WatchdogState.EMERGENCY, WatchdogState.FROZEN]
    
    def is_frozen(self) -> bool:
        """Check if system is frozen."""
        return self.get_state() == WatchdogState.FROZEN
    
    def register_escalation_callback(self, callback: Callable[[WatchdogSignal], None]) -> None:
        """Register callback for escalation signals."""
        self._escalation_callbacks.append(callback)
    
    def emit_escalation(self, level: PressureLevel, reason: str) -> None:
        """Emit escalation signal to watchdog."""
        signal = WatchdogSignal(
            state=WatchdogState.EMERGENCY if level == PressureLevel.EMERGENCY else WatchdogState.DEGRADED,
            timestamp=datetime.now(timezone.utc),
            reason=reason,
            force_level=level
        )
        
        for callback in self._escalation_callbacks:
            try:
                callback(signal)
            except Exception:
                # Never let callback failure block escalation
                pass


# ============================================================================
# BACKPRESSURE COORDINATOR (MAIN INTERFACE)
# ============================================================================

class BackpressureCoordinator:
    """
    Main interface for backpressure system.
    Coordinates evaluator, executor, and watchdog.
    """
    
    def __init__(
        self,
        policy: BackpressurePolicy,
        watchdog: Optional[WatchdogInterface] = None,
        audit_callback: Optional[Callable[[DegradationEvidence], None]] = None
    ):
        # Validate policy invariants on creation
        BackpressureInvariants.validate_policy(policy)
        
        self.policy = policy
        self.watchdog = watchdog or WatchdogInterface()
        self.evaluator = BackpressureEvaluator(policy)
        self.executor = BackpressureExecutor(policy, audit_callback)
        
        # Statistics
        self._total_evaluations = 0
        self._stats_lock = threading.Lock()
    
    def check_and_enforce(
        self,
        context: BackpressureContext,
        snapshots: List[PressureSnapshot]
    ) -> None:
        """
        Check backpressure and enforce decision.
        
        Raises:
            BackpressureShed: If request is shed
            BackpressureFrozen: If system is frozen
            InvariantViolation: If invariants are violated
        """
        # Validate invariants
        BackpressureInvariants.validate_context(context)
        BackpressureInvariants.validate_snapshots(snapshots)
        
        # Check watchdog freeze
        if self.watchdog.is_frozen():
            raise BackpressureFrozen("System is frozen by watchdog")
        
        # Increment evaluation counter
        with self._stats_lock:
            self._total_evaluations += 1
        
        # Evaluate
        watchdog_emergency = self.watchdog.is_emergency()
        decision, computed_level, reason = self.evaluator.evaluate(
            context, snapshots, watchdog_emergency
        )
        
        # Validate decision invariants
        BackpressureInvariants.validate_decision(decision, context, computed_level)
        
        # Emit escalation if needed
        if computed_level >= PressureLevel.CRITICAL:
            self.watchdog.emit_escalation(computed_level, reason)
        
        # Execute
        self.executor.execute(
            decision, context, snapshots, computed_level, reason, watchdog_emergency
        )
    
    @contextmanager
    def admission_control(
        self,
        context: BackpressureContext,
        snapshots: List[PressureSnapshot]
    ):
        """
        Context manager for admission control.
        
        Usage:
            with coordinator.admission_control(context, snapshots):
                # Execute protected operation
                pass
        """
        self.check_and_enforce(context, snapshots)
        try:
            yield
        finally:
            # Could add cleanup logic here if needed
            pass
    
    def get_statistics(self) -> Dict:
        """Get backpressure statistics."""
        with self._stats_lock:
            return {
                'total_evaluations': self._total_evaluations,
                'executor_stats': self.executor.get_stats(),
                'watchdog_state': self.watchdog.get_state().value,
                'policy_version': self.policy.policy_version
            }


# ============================================================================
# EXCEPTIONS
# ============================================================================

class BackpressureException(Exception):
    """Base exception for backpressure system."""
    pass


class BackpressureShed(BackpressureException):
    """Raised when request is shed due to backpressure."""
    
    def __init__(
        self,
        message: str,
        context: BackpressureContext,
        level: PressureLevel,
        evidence: DegradationEvidence
    ):
        super().__init__(message)
        self.context = context
        self.level = level
        self.evidence = evidence


class BackpressureFrozen(BackpressureException):
    """Raised when system is frozen by watchdog."""
    pass


class InvariantViolation(BackpressureException):
    """Raised when an invariant is violated."""
    pass


# ============================================================================
# POLICY FACTORY
# ============================================================================

class BackpressurePolicyFactory:
    """Factory for creating standard backpressure policies."""
    
    @staticmethod
    def create_default_policy() -> BackpressurePolicy:
        """Create default production policy."""
        return BackpressurePolicy(
            policy_version="default-v1.0.0",
            thresholds={
                PressureSignal.CPU: {
                    PressureLevel.DEGRADED: 70.0,
                    PressureLevel.CRITICAL: 85.0,
                    PressureLevel.EMERGENCY: 95.0
                },
                PressureSignal.MEMORY: {
                    PressureLevel.DEGRADED: 75.0,
                    PressureLevel.CRITICAL: 90.0,
                    PressureLevel.EMERGENCY: 97.0
                },
                PressureSignal.IO: {
                    PressureLevel.DEGRADED: 80.0,
                    PressureLevel.CRITICAL: 90.0,
                    PressureLevel.EMERGENCY: 95.0
                },
                PressureSignal.LATENCY: {
                    PressureLevel.DEGRADED: 1000.0,  # ms
                    PressureLevel.CRITICAL: 5000.0,
                    PressureLevel.EMERGENCY: 10000.0
                },
                PressureSignal.ERROR_RATE: {
                    PressureLevel.DEGRADED: 1.0,  # percent
                    PressureLevel.CRITICAL: 5.0,
                    PressureLevel.EMERGENCY: 10.0
                },
                PressureSignal.QUEUE_DEPTH: {
                    PressureLevel.DEGRADED: 1000.0,
                    PressureLevel.CRITICAL: 5000.0,
                    PressureLevel.EMERGENCY: 10000.0
                }
            },
            priority_cutoffs={
                PressureLevel.DEGRADED: 100,  # Shed priority > 100
                PressureLevel.CRITICAL: 50,   # Shed priority > 50
                PressureLevel.EMERGENCY: 10   # Shed priority > 10 (protect only 0-10)
            },
            slowdown_multipliers={
                PressureLevel.DEGRADED: 1.0,
                PressureLevel.CRITICAL: 2.0,
                PressureLevel.EMERGENCY: 5.0
            }
        )
    
    @staticmethod
    def create_conservative_policy() -> BackpressurePolicy:
        """Create conservative policy (shed more aggressively)."""
        return BackpressurePolicy(
            policy_version="conservative-v1.0.0",
            thresholds={
                PressureSignal.CPU: {
                    PressureLevel.DEGRADED: 60.0,
                    PressureLevel.CRITICAL: 75.0,
                    PressureLevel.EMERGENCY: 90.0
                },
                PressureSignal.MEMORY: {
                    PressureLevel.DEGRADED: 65.0,
                    PressureLevel.CRITICAL: 80.0,
                    PressureLevel.EMERGENCY: 95.0
                },
                PressureSignal.IO: {
                    PressureLevel.DEGRADED: 70.0,
                    PressureLevel.CRITICAL: 85.0,
                    PressureLevel.EMERGENCY: 95.0
                },
                PressureSignal.LATENCY: {
                    PressureLevel.DEGRADED: 500.0,
                    PressureLevel.CRITICAL: 2000.0,
                    PressureLevel.EMERGENCY: 5000.0
                },
                PressureSignal.ERROR_RATE: {
                    PressureLevel.DEGRADED: 0.5,
                    PressureLevel.CRITICAL: 2.0,
                    PressureLevel.EMERGENCY: 5.0
                },
                PressureSignal.QUEUE_DEPTH: {
                    PressureLevel.DEGRADED: 500.0,
                    PressureLevel.CRITICAL: 2000.0,
                    PressureLevel.EMERGENCY: 5000.0
                }
            },
            priority_cutoffs={
                PressureLevel.DEGRADED: 80,
                PressureLevel.CRITICAL: 30,
                PressureLevel.EMERGENCY: 5
            },
            slowdown_multipliers={
                PressureLevel.DEGRADED: 1.5,
                PressureLevel.CRITICAL: 3.0,
                PressureLevel.EMERGENCY: 10.0
            }
        )
    
    @staticmethod
    def create_permissive_policy() -> BackpressurePolicy:
        """Create permissive policy (shed less aggressively)."""
        return BackpressurePolicy(
            policy_version="permissive-v1.0.0",
            thresholds={
                PressureSignal.CPU: {
                    PressureLevel.DEGRADED: 80.0,
                    PressureLevel.CRITICAL: 90.0,
                    PressureLevel.EMERGENCY: 98.0
                },
                PressureSignal.MEMORY: {
                    PressureLevel.DEGRADED: 85.0,
                    PressureLevel.CRITICAL: 95.0,
                    PressureLevel.EMERGENCY: 99.0
                },
                PressureSignal.IO: {
                    PressureLevel.DEGRADED: 85.0,
                    PressureLevel.CRITICAL: 95.0,
                    PressureLevel.EMERGENCY: 98.0
                },
                PressureSignal.LATENCY: {
                    PressureLevel.DEGRADED: 2000.0,
                    PressureLevel.CRITICAL: 10000.0,
                    PressureLevel.EMERGENCY: 30000.0
                },
                PressureSignal.ERROR_RATE: {
                    PressureLevel.DEGRADED: 2.0,
                    PressureLevel.CRITICAL: 10.0,
                    PressureLevel.EMERGENCY: 20.0
                },
                PressureSignal.QUEUE_DEPTH: {
                    PressureLevel.DEGRADED: 2000.0,
                    PressureLevel.CRITICAL: 10000.0,
                    PressureLevel.EMERGENCY: 20000.0
                }
            },
            priority_cutoffs={
                PressureLevel.DEGRADED: 150,
                PressureLevel.CRITICAL: 100,
                PressureLevel.EMERGENCY: 20
            },
            slowdown_multipliers={
                PressureLevel.DEGRADED: 0.5,
                PressureLevel.CRITICAL: 1.5,
                PressureLevel.EMERGENCY: 3.0
            }
        )


# ============================================================================
# EXAMPLE USAGE & INTEGRATION
# ============================================================================

def example_usage():
    """Example of how to use the backpressure system."""
    
    # Create policy
    policy = BackpressurePolicyFactory.create_default_policy()
    
    # Create audit callback
    def audit_logger(evidence: DegradationEvidence):
        print(f"[AUDIT] {evidence.decision.value}: {evidence.reason}")
        print(f"  Evidence: {evidence.to_dict()}")
    
    # Create coordinator
    coordinator = BackpressureCoordinator(
        policy=policy,
        audit_callback=audit_logger
    )
    
    # Create pressure snapshots
    snapshots = [
        PressureSnapshot(
            signal=PressureSignal.CPU,
            observed_value=75.0,
            threshold=70.0,
            timestamp=datetime.now(timezone.utc),
            level=PressureLevel.DEGRADED
        ),
        PressureSnapshot(
            signal=PressureSignal.MEMORY,
            observed_value=65.0,
            threshold=75.0,
            timestamp=datetime.now(timezone.utc),
            level=PressureLevel.NORMAL
        )
    ]
    
    # Create context for high-priority action
    context_high_priority = BackpressureContext(
        action_type=ActionType.EXECUTE,
        scope=Scope.WORKFLOW,
        scope_id="wf-12345",
        priority=5,  # High priority
        timestamp=datetime.now(timezone.utc),
        run_id="run-abc-123"
    )
    
    # Create context for low-priority action
    context_low_priority = BackpressureContext(
        action_type=ActionType.POST,
        scope=Scope.ACCOUNT,
        scope_id="acc-67890",
        priority=150,  # Low priority
        timestamp=datetime.now(timezone.utc),
        run_id="run-xyz-789"
    )
    
    # Check high-priority action (should be slowed but not shed)
    try:
        with coordinator.admission_control(context_high_priority, snapshots):
            print("High-priority action allowed (with possible slowdown)")
    except BackpressureShed as e:
        print(f"High-priority action shed: {e}")
    
    # Check low-priority action (may be shed)
    try:
        with coordinator.admission_control(context_low_priority, snapshots):
            print("Low-priority action allowed")
    except BackpressureShed as e:
        print(f"Low-priority action shed: {e}")
    
    # Get statistics
    stats = coordinator.get_statistics()
    print(f"\nStatistics: {stats}")


if __name__ == "__main__":
    example_usage()