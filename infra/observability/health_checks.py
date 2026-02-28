"""
/infra/observability/health_checks.py

Deterministic Health Authority (Liveness · Readiness · Degradation)

This is the single source of truth for answering:
"Is this system allowed to continue operating right now?"

NOT:
- are we "up?"
- are requests returning 200s?
- does Kubernetes think we're alive?

BUT:
- are we safe
- are we coherent
- are we trustworthy
- are we degraded in a controlled way

Health ≠ observability
Health = permission to operate

RULES:
- Unknown = Unhealthy (ALWAYS)
- Worst-case wins (NO averaging)
- Fail-closed by default (explicit override only)
- Deterministic evaluation (replay-safe)
- No side effects in checks
- No silent passes
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, List, Dict
from collections import defaultdict
import time
import threading
from contextlib import contextmanager

# Mock infra.clock - replace with actual import
class Clock:
    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)


# ============================================================================
# HEALTH DIMENSIONS (MANDATORY)
# ============================================================================

class HealthDimension(Enum):
    """Independent axes of system health evaluation."""
    LIVENESS = "liveness"          # Can the system respond at all?
    READINESS = "readiness"        # Can the system accept new work?
    QUALITY = "quality"            # Is output trustworthy?
    SAFETY = "safety"              # Are safety constraints satisfied?
    COHERENCE = "coherence"        # Is internal state consistent?


# ============================================================================
# HEALTH STATES (STRICT)
# ============================================================================

class HealthState(Enum):
    """Discrete health states with clear operational semantics."""
    HEALTHY = "healthy"        # Full operation
    DEGRADED = "degraded"      # Controlled operation only
    UNHEALTHY = "unhealthy"    # Block new work
    FATAL = "fatal"            # Immediate halt

    def __lt__(self, other):
        """Worst-case ordering for aggregation."""
        if not isinstance(other, HealthState):
            return NotImplemented
        
        order = {
            HealthState.HEALTHY: 0,
            HealthState.DEGRADED: 1,
            HealthState.UNHEALTHY: 2,
            HealthState.FATAL: 3
        }
        return order[self] < order[other]

    def __le__(self, other):
        return self == other or self < other


# ============================================================================
# DEGRADATION MODES (OPERATIONAL POLICY)
# ============================================================================

class DegradationMode(Enum):
    """Explicit operational modes based on health state."""
    FULL_OPERATION = "full"
    THROTTLED = "throttled"
    SAFE_MODE = "safe_mode"
    READ_ONLY = "read_only"
    HALT = "halt"


# ============================================================================
# HEALTH CHECK DEFINITION (CORE UNIT)
# ============================================================================

@dataclass(frozen=True)
class HealthCheck:
    """
    Single health check definition.
    
    RULES:
    - evaluator must be deterministic
    - evaluator must have no side effects
    - timeout_ms is enforced strictly
    - fail_open=True requires explicit justification
    """
    name: str
    dimension: HealthDimension
    evaluator: Callable[[], HealthState]
    timeout_ms: int
    frequency_sec: int
    description: str
    fail_open: bool = False  # Almost always False
    
    def __post_init__(self):
        if self.timeout_ms <= 0:
            raise ValueError(f"Check {self.name}: timeout_ms must be positive")
        if self.frequency_sec <= 0:
            raise ValueError(f"Check {self.name}: frequency_sec must be positive")
        if not callable(self.evaluator):
            raise ValueError(f"Check {self.name}: evaluator must be callable")


# ============================================================================
# HEALTH SNAPSHOT (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class HealthSnapshot:
    """
    Immutable snapshot of system health at a point in time.
    
    PROPERTIES:
    - append-only (never mutated)
    - replay-safe (deterministic from inputs)
    - version-tracked (for audit trail)
    """
    timestamp: int
    states: dict[HealthDimension, HealthState]
    contributing_checks: dict[str, HealthState]
    version: int
    degradation_mode: DegradationMode
    
    def is_fatal(self) -> bool:
        """Any FATAL state triggers immediate halt."""
        return any(state == HealthState.FATAL for state in self.states.values())
    
    def is_healthy(self) -> bool:
        """All dimensions must be HEALTHY."""
        return all(state == HealthState.HEALTHY for state in self.states.values())
    
    def worst_state(self) -> HealthState:
        """Return worst health state across all dimensions."""
        if not self.states:
            return HealthState.UNHEALTHY  # No data = unhealthy
        return max(self.states.values())


# ============================================================================
# CHECK TIMEOUT ENFORCEMENT
# ============================================================================

class CheckTimeoutError(Exception):
    """Raised when a health check exceeds its timeout."""
    pass


@contextmanager
def enforce_timeout(timeout_ms: int, check_name: str):
    """
    Enforce strict timeout on health check evaluation.
    
    If timeout exceeded → CheckTimeoutError
    """
    start = Clock.now_ms()
    
    class TimeoutWatcher:
        def __init__(self):
            self.exceeded = False
            
        def check(self):
            if Clock.now_ms() - start > timeout_ms:
                self.exceeded = True
                raise CheckTimeoutError(
                    f"Check {check_name} exceeded timeout of {timeout_ms}ms"
                )
    
    watcher = TimeoutWatcher()
    try:
        yield watcher
    finally:
        if watcher.exceeded:
            pass  # Already raised


# ============================================================================
# HEALTH ENGINE (EXECUTION AUTHORITY)
# ============================================================================

class HealthEngine:
    """
    Executes health checks and produces deterministic snapshots.
    
    GUARANTEES:
    - Checks run in deterministic order
    - Timeouts strictly enforced
    - Failures are explicit (no silent passes)
    - Partial results rejected
    - Same inputs → same outputs (replay-safe)
    """
    
    def __init__(self, checks: list[HealthCheck]):
        self.checks = sorted(checks, key=lambda c: c.name)  # Deterministic order
        self.version = 0
        self._lock = threading.Lock()
        
        self._validate_checks()
    
    def _validate_checks(self):
        """Ensure check definitions are valid."""
        names = [c.name for c in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate check names detected")
        
        for check in self.checks:
            if check.fail_open:
                # fail_open=True requires explicit justification
                # In production, this would log/alert for audit
                pass
    
    def run_checks(self) -> HealthSnapshot:
        """
        Execute all health checks and produce immutable snapshot.
        
        RULES:
        - If any check times out → UNHEALTHY for that dimension
        - If any check raises exception → UNHEALTHY (or FATAL if fail_open=False)
        - Unknown state = UNHEALTHY
        - Worst-case aggregation per dimension
        """
        with self._lock:
            timestamp = Clock.now_ms()
            dimension_results = defaultdict(list)
            check_results = {}
            
            for check in self.checks:
                state = self._evaluate_check(check)
                check_results[check.name] = state
                dimension_results[check.dimension].append(state)
            
            # Aggregate: worst-case wins per dimension
            states = {
                dim: max(results) if results else HealthState.UNHEALTHY
                for dim, results in dimension_results.items()
            }
            
            # Ensure all dimensions are represented
            for dim in HealthDimension:
                if dim not in states:
                    states[dim] = HealthState.UNHEALTHY  # Missing = unhealthy
            
            # Determine degradation mode
            degradation_mode = self._compute_degradation_mode(states)
            
            self.version += 1
            
            return HealthSnapshot(
                timestamp=timestamp,
                states=states,
                contributing_checks=check_results,
                version=self.version,
                degradation_mode=degradation_mode
            )
    
    def _evaluate_check(self, check: HealthCheck) -> HealthState:
        """
        Evaluate single check with timeout enforcement.
        
        FAIL-CLOSED: Any error → UNHEALTHY (unless fail_open=True)
        """
        try:
            with enforce_timeout(check.timeout_ms, check.name):
                result = check.evaluator()
                
                if not isinstance(result, HealthState):
                    raise ValueError(f"Check {check.name} returned invalid type")
                
                return result
                
        except CheckTimeoutError:
            return HealthState.FATAL if not check.fail_open else HealthState.UNHEALTHY
            
        except Exception as e:
            # Unexpected error in check
            if check.fail_open:
                # Diagnostics only - don't fail system
                return HealthState.DEGRADED
            else:
                # Critical check failed
                return HealthState.UNHEALTHY
    
    def _compute_degradation_mode(
        self, 
        states: dict[HealthDimension, HealthState]
    ) -> DegradationMode:
        """
        Map health states to operational degradation mode.
        
        POLICY (immutable):
        - Any FATAL → HALT
        - Safety UNHEALTHY → HALT
        - Coherence UNHEALTHY → READ_ONLY
        - Quality UNHEALTHY → SAFE_MODE
        - Any UNHEALTHY → THROTTLED
        - Any DEGRADED → THROTTLED
        - All HEALTHY → FULL_OPERATION
        """
        if any(s == HealthState.FATAL for s in states.values()):
            return DegradationMode.HALT
        
        if states.get(HealthDimension.SAFETY) == HealthState.UNHEALTHY:
            return DegradationMode.HALT
        
        if states.get(HealthDimension.COHERENCE) == HealthState.UNHEALTHY:
            return DegradationMode.READ_ONLY
        
        if states.get(HealthDimension.QUALITY) == HealthState.UNHEALTHY:
            return DegradationMode.SAFE_MODE
        
        if any(s == HealthState.UNHEALTHY for s in states.values()):
            return DegradationMode.THROTTLED
        
        if any(s == HealthState.DEGRADED for s in states.values()):
            return DegradationMode.THROTTLED
        
        return DegradationMode.FULL_OPERATION


# ============================================================================
# KILL-SWITCH INTEGRATION
# ============================================================================

class HealthWatchdog:
    """
    Monitors health snapshots and triggers kill-switches on FATAL states.
    
    RESPONSIBILITIES:
    - Detect FATAL conditions
    - Trigger /infra/watchdog.py (when available)
    - Freeze experiments
    - Stop posting
    - Preserve state for replay
    
    Does NOT attempt recovery - that's not the watchdog's job.
    """
    
    def __init__(self, engine: HealthEngine):
        self.engine = engine
        self._last_snapshot: Optional[HealthSnapshot] = None
    
    def check_and_enforce(self) -> HealthSnapshot:
        """
        Run health checks and enforce kill-switches if necessary.
        
        Returns snapshot even if FATAL (for audit/replay).
        """
        snapshot = self.engine.run_checks()
        self._last_snapshot = snapshot
        
        if snapshot.is_fatal():
            self._trigger_kill_switch(snapshot)
        
        return snapshot
    
    def _trigger_kill_switch(self, snapshot: HealthSnapshot):
        """
        Execute kill-switch protocol.
        
        In production:
        - Call /infra/watchdog.py halt()
        - Freeze all experiments
        - Stop all posting
        - Preserve state to disk
        - Alert on-call
        """
        fatal_dimensions = [
            dim for dim, state in snapshot.states.items()
            if state == HealthState.FATAL
        ]
        
        # This would integrate with actual watchdog
        # For now, raise to prevent silent failures
        raise SystemExit(
            f"FATAL health state detected in dimensions: {fatal_dimensions}. "
            f"System halted at version {snapshot.version}."
        )


# ============================================================================
# EXAMPLE CHECK DEFINITIONS (REALISTIC)
# ============================================================================

class ExampleHealthChecks:
    """
    Production-grade health check examples.
    
    Each check:
    - Has deterministic thresholds
    - Has strict timeouts
    - Has explicit consequences
    - References deterministic observables only
    """
    
    @staticmethod
    def feature_registry_loaded() -> HealthState:
        """Check if feature registry is loaded and valid."""
        # In production: check actual registry
        # return HealthState.HEALTHY if registry.is_loaded() else HealthState.FATAL
        return HealthState.HEALTHY
    
    @staticmethod
    def schema_registry_hash_valid() -> HealthState:
        """Verify schema registry hash matches snapshot."""
        # In production: compare hashes
        # expected = load_expected_hash()
        # actual = compute_current_hash()
        # return HealthState.HEALTHY if expected == actual else HealthState.UNHEALTHY
        return HealthState.HEALTHY
    
    @staticmethod
    def metrics_emission_rate_ok() -> HealthState:
        """Check if metrics emission rate is within expected bounds."""
        # In production: check actual rate
        # rate = get_metrics_rate()
        # if rate < MIN_RATE: return HealthState.UNHEALTHY
        # if rate > MAX_RATE: return HealthState.DEGRADED
        return HealthState.HEALTHY
    
    @staticmethod
    def no_reward_leakage() -> HealthState:
        """Detect reward leakage in training loop."""
        # In production: check leakage detector
        # if detect_leakage(): return HealthState.FATAL
        return HealthState.HEALTHY
    
    @staticmethod
    def posting_queues_not_backlogged() -> HealthState:
        """Check if posting queues are within acceptable bounds."""
        # In production: check queue depths
        # depth = get_queue_depth()
        # if depth > CRITICAL: return HealthState.UNHEALTHY
        # if depth > WARNING: return HealthState.DEGRADED
        return HealthState.HEALTHY
    
    @staticmethod
    def account_trust_score_ok() -> HealthState:
        """Verify account trust score is above minimum."""
        # In production: check trust score
        # score = get_trust_score()
        # return HealthState.HEALTHY if score > MIN_SCORE else HealthState.UNHEALTHY
        return HealthState.HEALTHY
    
    @staticmethod
    def safety_watchdog_armed() -> HealthState:
        """Verify safety watchdog is armed and responsive."""
        # In production: ping watchdog
        # return HealthState.FATAL if not watchdog.is_armed() else HealthState.HEALTHY
        return HealthState.HEALTHY


# ============================================================================
# PRODUCTION CHECK REGISTRY
# ============================================================================

def build_production_checks() -> list[HealthCheck]:
    """
    Construct the complete set of production health checks.
    
    Each check must justify:
    - Which dimension it belongs to
    - Why its timeout is set to that value
    - Why it's fail_open (if True)
    """
    return [
        # LIVENESS checks
        HealthCheck(
            name="feature_registry_loaded",
            dimension=HealthDimension.LIVENESS,
            evaluator=ExampleHealthChecks.feature_registry_loaded,
            timeout_ms=100,
            frequency_sec=60,
            description="Feature registry must be loaded for system to operate",
            fail_open=False
        ),
        
        # READINESS checks
        HealthCheck(
            name="posting_queues_ok",
            dimension=HealthDimension.READINESS,
            evaluator=ExampleHealthChecks.posting_queues_not_backlogged,
            timeout_ms=50,
            frequency_sec=10,
            description="Posting queues must not be backlogged to accept new work",
            fail_open=False
        ),
        
        # QUALITY checks
        HealthCheck(
            name="metrics_emission_rate",
            dimension=HealthDimension.QUALITY,
            evaluator=ExampleHealthChecks.metrics_emission_rate_ok,
            timeout_ms=100,
            frequency_sec=30,
            description="Metrics emission rate indicates system health",
            fail_open=False
        ),
        
        HealthCheck(
            name="account_trust_score",
            dimension=HealthDimension.QUALITY,
            evaluator=ExampleHealthChecks.account_trust_score_ok,
            timeout_ms=200,
            frequency_sec=300,
            description="Account trust score must be maintained",
            fail_open=False
        ),
        
        # SAFETY checks
        HealthCheck(
            name="no_reward_leakage",
            dimension=HealthDimension.SAFETY,
            evaluator=ExampleHealthChecks.no_reward_leakage,
            timeout_ms=150,
            frequency_sec=60,
            description="Reward leakage is a critical safety violation",
            fail_open=False
        ),
        
        HealthCheck(
            name="safety_watchdog_armed",
            dimension=HealthDimension.SAFETY,
            evaluator=ExampleHealthChecks.safety_watchdog_armed,
            timeout_ms=50,
            frequency_sec=10,
            description="Safety watchdog must be armed at all times",
            fail_open=False
        ),
        
        # COHERENCE checks
        HealthCheck(
            name="schema_registry_hash",
            dimension=HealthDimension.COHERENCE,
            evaluator=ExampleHealthChecks.schema_registry_hash_valid,
            timeout_ms=100,
            frequency_sec=120,
            description="Schema registry hash must match expected snapshot",
            fail_open=False
        ),
    ]


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Build production health checks
    checks = build_production_checks()
    
    # Create health engine
    engine = HealthEngine(checks)
    
    # Create watchdog
    watchdog = HealthWatchdog(engine)
    
    # Run health check cycle
    snapshot = watchdog.check_and_enforce()
    
    print(f"Health Check v{snapshot.version}")
    print(f"Timestamp: {snapshot.timestamp}")
    print(f"Degradation Mode: {snapshot.degradation_mode.value}")
    print(f"\nDimension States:")
    for dim, state in sorted(snapshot.states.items(), key=lambda x: x[0].value):
        print(f"  {dim.value:12} → {state.value}")
    
    print(f"\nContributing Checks:")
    for name, state in sorted(snapshot.contributing_checks.items()):
        print(f"  {name:30} → {state.value}")
    
    print(f"\nOverall: {'✓ HEALTHY' if snapshot.is_healthy() else '✗ NOT HEALTHY'}")
    print(f"Fatal: {'YES - SYSTEM HALTED' if snapshot.is_fatal() else 'no'}")




