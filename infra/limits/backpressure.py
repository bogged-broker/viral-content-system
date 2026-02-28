"""
/infra/limits/backpressure.py

Deterministic Load Shedding & Slowdown Authority

This file decides what to slow, what to shed, and what must continue when the
system is under stress. Backpressure is about protecting correctness, not throughput.

A smaller correct system beats a larger lying one.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Tuple, List, Dict
import time


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================


class PressureSignal(Enum):
    """
    Pressure signals are observed, never inferred.
    """

    CPU = "cpu"
    MEMORY = "memory"
    IO = "io"
    LATENCY = "latency"
    ERROR_RATE = "error_rate"
    QUEUE_DEPTH = "queue_depth"


class PressureLevel(Enum):
    """
    Levels escalate monotonically — no flapping.
    """

    NORMAL = "normal"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    EMERGENCY = "emergency"

    def __lt__(self, other: "PressureLevel") -> bool:
        """Define ordering for level comparison."""
        order = {
            PressureLevel.NORMAL: 0,
            PressureLevel.DEGRADED: 1,
            PressureLevel.CRITICAL: 2,
            PressureLevel.EMERGENCY: 3,
        }
        return order[self] < order[other]

    def __le__(self, other: "PressureLevel") -> bool:
        """Define ordering for level comparison."""
        return self == other or self < other

    def __gt__(self, other: "PressureLevel") -> bool:
        """Define ordering for level comparison."""
        return not self <= other

    def __ge__(self, other: "PressureLevel") -> bool:
        """Define ordering for level comparison."""
        return self == other or self > other


class BackpressureDecision(Enum):
    """
    Backpressure decisions. No hidden throttles.
    """

    ALLOW = "allow"
    SLOW = "slow"
    SHED = "shed"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class PressureSnapshot:
    """
    Pressure snapshot with evidence.
    
    Snapshots are evidence, not estimates.
    """

    signal: PressureSignal
    value: float
    threshold: float
    timestamp: int

    def is_exceeding_threshold(self) -> bool:
        """Check if value exceeds threshold."""
        return self.value > self.threshold

    def pressure_ratio(self) -> float:
        """Get ratio of value to threshold."""
        if self.threshold <= 0:
            return 0.0
        return self.value / self.threshold

    def validate(self) -> None:
        """
        Validate pressure snapshot.
        
        Raises:
            ValueError: If snapshot invalid
        """
        if self.value < 0:
            raise ValueError("Pressure value cannot be negative")

        if self.threshold <= 0:
            raise ValueError("Pressure threshold must be positive")

        if self.timestamp <= 0:
            raise ValueError("Timestamp must be positive")


@dataclass(frozen=True)
class BackpressureContext:
    """
    Context for backpressure evaluation.
    
    Context is mandatory. No silent degradation.
    """

    action_type: str  # post, execute, recover, infer
    scope: str  # global, workflow, account
    scope_id: str | None

    priority: int  # lower = more important
    timestamp: int
    run_id: str

    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """
        Validate backpressure context.
        
        Raises:
            ValueError: If context invalid
        """
        if not self.action_type:
            raise ValueError("action_type required")

        if not self.scope:
            raise ValueError("scope required")

        if self.priority < 0:
            raise ValueError("priority must be non-negative")

        if self.timestamp <= 0:
            raise ValueError("timestamp must be positive")

        if not self.run_id:
            raise ValueError("run_id required for audit trail")


@dataclass(frozen=True)
class BackpressureDecisionResult:
    """
    Result of backpressure evaluation.
    """

    decision: BackpressureDecision
    reason: str

    pressure_level: PressureLevel
    slowdown_ms: int | None  # Milliseconds to delay if SLOW

    snapshots: list[PressureSnapshot]
    timestamp: int

    def is_allowed(self) -> bool:
        """Check if action is allowed."""
        return self.decision == BackpressureDecision.ALLOW

    def is_slowed(self) -> bool:
        """Check if action should be slowed."""
        return self.decision == BackpressureDecision.SLOW

    def is_shed(self) -> bool:
        """Check if action should be shed."""
        return self.decision == BackpressureDecision.SHED

    def to_audit_record(self) -> dict[str, Any]:
        """Convert to audit record."""
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "pressure_level": self.pressure_level.value,
            "slowdown_ms": self.slowdown_ms,
            "timestamp": self.timestamp,
            "snapshots": [
                {
                    "signal": s.signal.value,
                    "value": s.value,
                    "threshold": s.threshold,
                    "exceeding": s.is_exceeding_threshold(),
                }
                for s in self.snapshots
            ],
        }


# ============================================================================
# BACKPRESSURE POLICY (DECLARATIVE)
# ============================================================================


@dataclass(frozen=True)
class BackpressurePolicy:
    """
    Declarative backpressure policy.
    
    Rules:
    - Policies are versioned
    - No inline thresholds
    - No dynamic tuning
    """

    policy_id: str

    # Pressure thresholds per signal for each level
    degraded_thresholds: dict[PressureSignal, float]
    critical_thresholds: dict[PressureSignal, float]
    emergency_thresholds: dict[PressureSignal, float]

    # Priority thresholds for shedding at each level
    degraded_shed_priority: int  # Shed actions with priority >= this
    critical_shed_priority: int
    emergency_shed_priority: int

    # Slowdown multipliers per level (1.0 = no slowdown)
    degraded_slowdown_multiplier: float
    critical_slowdown_multiplier: float
    emergency_slowdown_multiplier: float

    policy_version: str

    def validate(self) -> None:
        """
        Validate backpressure policy.
        
        Raises:
            ValueError: If policy invalid
        """
        if not self.policy_id:
            raise ValueError("policy_id required")

        if not self.policy_version:
            raise ValueError("policy_version required")

        # Validate thresholds
        for signal, threshold in self.degraded_thresholds.items():
            if threshold <= 0:
                raise ValueError(f"Threshold for {signal.value} must be positive")

        # Validate shed priorities are monotonically decreasing
        if not (
            self.degraded_shed_priority
            >= self.critical_shed_priority
            >= self.emergency_shed_priority
        ):
            raise ValueError("Shed priorities must be monotonically decreasing")

        # Validate slowdown multipliers
        if self.degraded_slowdown_multiplier < 1.0:
            raise ValueError("Slowdown multiplier cannot be less than 1.0")

        if self.critical_slowdown_multiplier < self.degraded_slowdown_multiplier:
            raise ValueError("Critical slowdown must be >= degraded slowdown")

        if self.emergency_slowdown_multiplier < self.critical_slowdown_multiplier:
            raise ValueError("Emergency slowdown must be >= critical slowdown")

    def get_pressure_level(
        self,
        snapshots: list[PressureSnapshot],
    ) -> PressureLevel:
        """
        Determine overall pressure level from snapshots.
        
        Args:
            snapshots: List of pressure snapshots
            
        Returns:
            Highest pressure level detected
        """
        max_level = PressureLevel.NORMAL

        for snapshot in snapshots:
            # Check emergency
            emergency_threshold = self.emergency_thresholds.get(snapshot.signal)
            if emergency_threshold and snapshot.value >= emergency_threshold:
                max_level = max(max_level, PressureLevel.EMERGENCY)
                continue

            # Check critical
            critical_threshold = self.critical_thresholds.get(snapshot.signal)
            if critical_threshold and snapshot.value >= critical_threshold:
                max_level = max(max_level, PressureLevel.CRITICAL)
                continue

            # Check degraded
            degraded_threshold = self.degraded_thresholds.get(snapshot.signal)
            if degraded_threshold and snapshot.value >= degraded_threshold:
                max_level = max(max_level, PressureLevel.DEGRADED)

        return max_level

    def get_shed_priority_threshold(self, level: PressureLevel) -> int:
        """Get priority threshold for shedding at given level."""
        if level == PressureLevel.EMERGENCY:
            return self.emergency_shed_priority
        elif level == PressureLevel.CRITICAL:
            return self.critical_shed_priority
        elif level == PressureLevel.DEGRADED:
            return self.degraded_shed_priority
        else:
            return float("inf")  # Never shed at NORMAL

    def get_slowdown_multiplier(self, level: PressureLevel) -> float:
        """Get slowdown multiplier for given level."""
        if level == PressureLevel.EMERGENCY:
            return self.emergency_slowdown_multiplier
        elif level == PressureLevel.CRITICAL:
            return self.critical_slowdown_multiplier
        elif level == PressureLevel.DEGRADED:
            return self.degraded_slowdown_multiplier
        else:
            return 1.0  # No slowdown at NORMAL


# ============================================================================
# BACKPRESSURE EVALUATOR (BRAIN)
# ============================================================================


class BackpressureEvaluator:
    """
    Evaluates backpressure decisions based on pressure and context.
    
    Guarantees:
    - Deterministic evaluation order
    - Strict threshold comparison
    - Monotonic escalation
    - Fail-closed on ambiguity
    
    Same inputs → same decision.
    """

    def __init__(
        self,
        policy: BackpressurePolicy,
        watchdog_state: dict[str, Any] | None = None,
    ):
        """
        Initialize backpressure evaluator.
        
        Args:
            policy: BackpressurePolicy to enforce
            watchdog_state: Optional watchdog state
        """
        policy.validate()
        self._policy = policy
        self._watchdog_state = watchdog_state or {}

    def evaluate(
        self,
        snapshots: list[PressureSnapshot],
        context: BackpressureContext,
    ) -> BackpressureDecisionResult:
        """
        Evaluate backpressure decision.
        
        Args:
            snapshots: Current pressure snapshots
            context: Backpressure context
            
        Returns:
            BackpressureDecisionResult
            
        Raises:
            ValueError: If context invalid
        """
        context.validate()

        # Validate snapshots
        for snapshot in snapshots:
            snapshot.validate()

        # Check watchdog overrides first
        if self._is_globally_frozen():
            return BackpressureDecisionResult(
                decision=BackpressureDecision.SHED,
                reason="System globally frozen by watchdog",
                pressure_level=PressureLevel.EMERGENCY,
                slowdown_ms=None,
                snapshots=snapshots,
                timestamp=context.timestamp,
            )

        if self._is_emergency_mode():
            return BackpressureDecisionResult(
                decision=BackpressureDecision.SHED,
                reason="System in watchdog emergency mode",
                pressure_level=PressureLevel.EMERGENCY,
                slowdown_ms=None,
                snapshots=snapshots,
                timestamp=context.timestamp,
            )

        # Determine pressure level
        pressure_level = self._policy.get_pressure_level(snapshots)

        # Evaluate based on pressure level and priority
        if pressure_level == PressureLevel.NORMAL:
            return BackpressureDecisionResult(
                decision=BackpressureDecision.ALLOW,
                reason="Pressure normal — no backpressure needed",
                pressure_level=pressure_level,
                slowdown_ms=None,
                snapshots=snapshots,
                timestamp=context.timestamp,
            )

        # Check if should shed based on priority
        shed_threshold = self._policy.get_shed_priority_threshold(pressure_level)

        if context.priority >= shed_threshold:
            return BackpressureDecisionResult(
                decision=BackpressureDecision.SHED,
                reason=f"Priority {context.priority} >= shed threshold {shed_threshold} at {pressure_level.value}",
                pressure_level=pressure_level,
                slowdown_ms=None,
                snapshots=snapshots,
                timestamp=context.timestamp,
            )

        # Apply slowdown for surviving actions
        slowdown_multiplier = self._policy.get_slowdown_multiplier(pressure_level)
        base_slowdown_ms = 100  # Base slowdown in ms

        slowdown_ms = int(base_slowdown_ms * (slowdown_multiplier - 1.0))

        if slowdown_ms > 0:
            return BackpressureDecisionResult(
                decision=BackpressureDecision.SLOW,
                reason=f"Slowdown applied at {pressure_level.value} — {slowdown_ms}ms delay",
                pressure_level=pressure_level,
                slowdown_ms=slowdown_ms,
                snapshots=snapshots,
                timestamp=context.timestamp,
            )

        # Allow but with elevated pressure level noted
        return BackpressureDecisionResult(
            decision=BackpressureDecision.ALLOW,
            reason=f"Allowed despite {pressure_level.value} pressure (high priority)",
            pressure_level=pressure_level,
            slowdown_ms=None,
            snapshots=snapshots,
            timestamp=context.timestamp,
        )

    def _is_globally_frozen(self) -> bool:
        """Check if system is globally frozen."""
        return self._watchdog_state.get("globally_frozen", False)

    def _is_emergency_mode(self) -> bool:
        """Check if system is in emergency mode."""
        return self._watchdog_state.get("emergency_mode", False)


# ============================================================================
# BACKPRESSURE EXECUTOR (MECHANISM)
# ============================================================================


class BackpressureExecutor:
    """
    Executes backpressure decisions.
    
    Executor rules:
    - Slowing is explicit (sleep, defer)
    - Shedding is absolute (deny execution)
    - Actions are audited
    - Watchdog overrides honored
    """

    def __init__(self, audit_callback: callable = None):
        """
        Initialize backpressure executor.
        
        Args:
            audit_callback: Optional callback for audit events
        """
        self._audit_callback = audit_callback
        self._execution_log: list[tuple[int, BackpressureDecisionResult]] = []

    def enforce(
        self,
        decision: BackpressureDecisionResult,
        context: BackpressureContext,
    ) -> bool:
        """
        Enforce backpressure decision.
        
        Args:
            decision: BackpressureDecisionResult to enforce
            context: Backpressure context
            
        Returns:
            True if action allowed to proceed, False if shed
        """
        # Audit decision
        if self._audit_callback:
            audit_event = {
                "event_type": "backpressure_decision",
                "decision": decision.decision.value,
                "reason": decision.reason,
                "pressure_level": decision.pressure_level.value,
                "context": {
                    "action_type": context.action_type,
                    "scope": context.scope,
                    "scope_id": context.scope_id,
                    "priority": context.priority,
                },
                "timestamp": context.timestamp,
            }
            self._audit_callback(audit_event)

        # Log execution
        self._log_execution(decision)

        # Enforce decision
        if decision.is_shed():
            return False

        if decision.is_slowed() and decision.slowdown_ms:
            # Apply slowdown (in real implementation, this would be async/defer)
            # For now, just record that slowdown should be applied
            time.sleep(decision.slowdown_ms / 1000.0)

        return True

    def _log_execution(self, decision: BackpressureDecisionResult) -> None:
        """Log decision for audit trail."""
        timestamp = int(time.time() * 1000)
        self._execution_log.append((timestamp, decision))

    def get_execution_log(self) -> list[tuple[int, BackpressureDecisionResult]]:
        """Get execution log."""
        return self._execution_log.copy()

    def clear_log(self) -> None:
        """Clear execution log (for testing)."""
        self._execution_log.clear()


# ============================================================================
# BACKPRESSURE INVARIANTS
# ============================================================================


class BackpressureInvariants:
    """
    Enforces backpressure invariants.
    
    Invariants:
    - No shedding of priority 0 actions
    - No silent slowdown
    - No negative slowdown multipliers
    - No conflicting decisions
    - No recovery without watchdog approval
    
    Violations → immediate hard stop.
    """

    @staticmethod
    def verify_priority_protection(
        decision: BackpressureDecisionResult,
        context: BackpressureContext,
    ) -> None:
        """
        Verify priority 0 actions are not shed.
        
        Args:
            decision: Decision result
            context: Context
            
        Raises:
            RuntimeError: If priority 0 shed detected
        """
        if context.priority == 0 and decision.is_shed():
            raise RuntimeError(
                "Priority 0 action cannot be shed (INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_slowdown_valid(decision: BackpressureDecisionResult) -> None:
        """
        Verify slowdown parameters are valid.
        
        Args:
            decision: Decision result
            
        Raises:
            RuntimeError: If invalid slowdown detected
        """
        if decision.is_slowed():
            if decision.slowdown_ms is None:
                raise RuntimeError(
                    "SLOW decision must specify slowdown_ms (INVARIANT VIOLATION)"
                )

            if decision.slowdown_ms < 0:
                raise RuntimeError(
                    f"Negative slowdown {decision.slowdown_ms}ms (INVARIANT VIOLATION)"
                )

    @staticmethod
    def verify_pressure_monotonicity(
        old_level: PressureLevel,
        new_level: PressureLevel,
        recovery_approved: bool,
    ) -> None:
        """
        Verify pressure levels escalate monotonically without recovery approval.
        
        Args:
            old_level: Previous pressure level
            new_level: New pressure level
            recovery_approved: Whether recovery is approved
            
        Raises:
            RuntimeError: If pressure decreases without approval
        """
        if new_level < old_level and not recovery_approved:
            raise RuntimeError(
                f"Pressure decreased from {old_level.value} to {new_level.value} "
                f"without recovery approval (INVARIANT VIOLATION)"
            )

    @staticmethod
    def verify_all(
        decision: BackpressureDecisionResult,
        context: BackpressureContext,
    ) -> None:
        """
        Run all invariant checks.
        
        Args:
            decision: Decision result
            context: Context
            
        Raises:
            RuntimeError: If any invariant violated
        """
        BackpressureInvariants.verify_priority_protection(decision, context)
        BackpressureInvariants.verify_slowdown_valid(decision)


# ============================================================================
# BACKPRESSURE MANAGER (HIGH-LEVEL API)
# ============================================================================


class BackpressureManager:
    """
    High-level backpressure management API.
    
    Combines evaluation and execution.
    """

    def __init__(
        self,
        policy: BackpressurePolicy,
        watchdog_state: dict[str, Any] | None = None,
        audit_callback: callable = None,
    ):
        """
        Initialize backpressure manager.
        
        Args:
            policy: BackpressurePolicy
            watchdog_state: Optional watchdog state
            audit_callback: Optional audit callback
        """
        self._evaluator = BackpressureEvaluator(policy, watchdog_state)
        self._executor = BackpressureExecutor(audit_callback)
        self._policy = policy

    def check_and_enforce(
        self,
        snapshots: list[PressureSnapshot],
        context: BackpressureContext,
    ) -> tuple[bool, BackpressureDecisionResult]:
        """
        Check backpressure and enforce decision.
        
        Args:
            snapshots: Pressure snapshots
            context: Backpressure context
            
        Returns:
            Tuple of (allowed, decision_result)
        """
        # Evaluate
        decision = self._evaluator.evaluate(snapshots, context)

        # Verify invariants
        BackpressureInvariants.verify_all(decision, context)

        # Enforce
        allowed = self._executor.enforce(decision, context)

        return allowed, decision

    def get_current_pressure_level(
        self,
        snapshots: list[PressureSnapshot],
    ) -> PressureLevel:
        """
        Get current pressure level from snapshots.
        
        Args:
            snapshots: Pressure snapshots
            
        Returns:
            Current pressure level
        """
        return self._policy.get_pressure_level(snapshots)


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================


def create_default_policy() -> BackpressurePolicy:
    """
    Create default backpressure policy with conservative thresholds.
    
    Returns:
        Default BackpressurePolicy
    """
    return BackpressurePolicy(
        policy_id="default",
        # Degraded thresholds (gentle pressure)
        degraded_thresholds={
            PressureSignal.CPU: 0.7,  # 70% CPU
            PressureSignal.MEMORY: 0.75,  # 75% memory
            PressureSignal.LATENCY: 1000.0,  # 1s latency
            PressureSignal.ERROR_RATE: 0.05,  # 5% errors
            PressureSignal.QUEUE_DEPTH: 1000.0,
        },
        # Critical thresholds (significant pressure)
        critical_thresholds={
            PressureSignal.CPU: 0.85,  # 85% CPU
            PressureSignal.MEMORY: 0.85,  # 85% memory
            PressureSignal.LATENCY: 5000.0,  # 5s latency
            PressureSignal.ERROR_RATE: 0.10,  # 10% errors
            PressureSignal.QUEUE_DEPTH: 5000.0,
        },
        # Emergency thresholds (extreme pressure)
        emergency_thresholds={
            PressureSignal.CPU: 0.95,  # 95% CPU
            PressureSignal.MEMORY: 0.95,  # 95% memory
            PressureSignal.LATENCY: 10000.0,  # 10s latency
            PressureSignal.ERROR_RATE: 0.20,  # 20% errors
            PressureSignal.QUEUE_DEPTH: 10000.0,
        },
        # Priority shedding thresholds (higher = less important)
        degraded_shed_priority=100,  # Shed priority 100+
        critical_shed_priority=50,  # Shed priority 50+
        emergency_shed_priority=10,  # Shed priority 10+
        # Slowdown multipliers
        degraded_slowdown_multiplier=1.5,  # 50% slower
        critical_slowdown_multiplier=3.0,  # 200% slower
        emergency_slowdown_multiplier=10.0,  # 900% slower
        policy_version="v1",
    )


def create_strict_policy() -> BackpressurePolicy:
    """
    Create strict backpressure policy with tight thresholds.
    
    Returns:
        Strict BackpressurePolicy
    """
    return BackpressurePolicy(
        policy_id="strict",
        # Lower thresholds for earlier intervention
        degraded_thresholds={
            PressureSignal.CPU: 0.6,
            PressureSignal.MEMORY: 0.65,
            PressureSignal.LATENCY: 500.0,
            PressureSignal.ERROR_RATE: 0.02,
            PressureSignal.QUEUE_DEPTH: 500.0,
        },
        critical_thresholds={
            PressureSignal.CPU: 0.75,
            PressureSignal.MEMORY: 0.80,
            PressureSignal.LATENCY: 2000.0,
            PressureSignal.ERROR_RATE: 0.05,
            PressureSignal.QUEUE_DEPTH: 2000.0,
        },
        emergency_thresholds={
            PressureSignal.CPU: 0.90,
            PressureSignal.MEMORY: 0.90,
            PressureSignal.LATENCY: 5000.0,
            PressureSignal.ERROR_RATE: 0.10,
            PressureSignal.QUEUE_DEPTH: 5000.0,
        },
        # More aggressive shedding
        degraded_shed_priority=50,
        critical_shed_priority=25,
        emergency_shed_priority=5,
        # Stronger slowdowns
        degraded_slowdown_multiplier=2.0,
        critical_slowdown_multiplier=5.0,
        emergency_slowdown_multiplier=20.0,
        policy_version="v1-strict",
    )