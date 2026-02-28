# /infra/clock.py
"""
Monotonic & Replay-Safe Time Authority

This is the single source of temporal truth for the entire system.
Time bugs are the #1 silent killer of:
- reproducibility
- experiments
- RL credit assignment
- platform safety
- postmortems

Core principles (NON-NEGOTIABLE):
1. Monotonicity beats realism
2. Replay correctness beats "now"
3. Determinism beats convenience
4. Wall-clock is advisory only

If anything uses time.time() directly → that code is WRONG.
"""

import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List
from collections import defaultdict

from infra.runtime_context import RuntimeContext, ExecutionMode
from infra.config_registry import ConfigRegistry


# ============================================================================
# ENUMS (EXPLICIT TIME SEMANTICS)
# ============================================================================

class TimeSource(Enum):
    """Source of time information - only MONOTONIC is trusted by default."""
    MONOTONIC = "monotonic"  # Guaranteed monotonic, never goes backward
    WALL = "wall"            # Wall-clock time (advisory only, untrusted)
    LOGICAL = "logical"      # Logical clock for ordering events
    REPLAY = "replay"        # Replayed time from recorded timeline


class ClockMode(Enum):
    """Clock operational mode - determines time behavior."""
    LIVE = "live"              # Real-time, monotonic system clock
    REPLAY = "replay"          # Replaying recorded timeline
    SIMULATION = "simulation"  # Simulated time for testing


# ============================================================================
# CORE DATA TYPES (STRONG TYPING - NO RAW FLOATS)
# ============================================================================

@dataclass(frozen=True)
class TimePoint:
    """
    A point in time with full provenance.
    Never a raw float or datetime - always structured.
    """
    ticks: int                          # Monotonic ticks (primary source of truth)
    wall_time: Optional[datetime]       # Wall-clock annotation (advisory only)
    source: TimeSource                  # Where this time came from
    
    def __post_init__(self):
        if self.ticks < 0:
            raise ValueError(f"TimePoint ticks must be >= 0, got {self.ticks}")
        if self.wall_time is not None and self.wall_time.tzinfo is None:
            raise ValueError("TimePoint wall_time must be timezone-aware")
    
    def __lt__(self, other: 'TimePoint') -> bool:
        """Compare based on monotonic ticks only."""
        return self.ticks < other.ticks
    
    def __le__(self, other: 'TimePoint') -> bool:
        return self.ticks <= other.ticks
    
    def __gt__(self, other: 'TimePoint') -> bool:
        return self.ticks > other.ticks
    
    def __ge__(self, other: 'TimePoint') -> bool:
        return self.ticks >= other.ticks


@dataclass(frozen=True)
class TimeDelta:
    """
    A duration in time.
    Explicit units - no ambiguity about seconds vs milliseconds vs ticks.
    """
    ticks: int
    
    def __post_init__(self):
        if self.ticks < 0:
            raise ValueError(f"TimeDelta ticks must be >= 0, got {self.ticks}")
    
    def to_seconds(self, ticks_per_second: int) -> float:
        """Convert to seconds given tick rate."""
        return self.ticks / ticks_per_second
    
    def __add__(self, other: 'TimeDelta') -> 'TimeDelta':
        return TimeDelta(ticks=self.ticks + other.ticks)
    
    def __sub__(self, other: 'TimeDelta') -> 'TimeDelta':
        if self.ticks < other.ticks:
            raise ValueError("Cannot subtract larger TimeDelta from smaller")
        return TimeDelta(ticks=self.ticks - other.ticks)


# ============================================================================
# TIME POLICY (CONFIG-DRIVEN BEHAVIOR)
# ============================================================================

@dataclass(frozen=True)
class TimePolicy:
    """
    Policy controlling time behavior.
    Strict by default - violations are fatal unless explicitly allowed.
    """
    allow_wall_clock: bool = False        # Allow wall-clock annotations
    max_drift_ticks: int = 1000           # Max allowed tick drift before alarm
    freeze_on_violation: bool = True      # Hard-stop on monotonic violation
    ticks_per_second: int = 1000          # Tick resolution (1ms default)
    enable_logical_clocks: bool = True    # Enable logical clock subsystem
    
    def __post_init__(self):
        if self.ticks_per_second <= 0:
            raise ValueError("ticks_per_second must be > 0")
        if self.max_drift_ticks < 0:
            raise ValueError("max_drift_ticks must be >= 0")


# ============================================================================
# CLOCK SNAPSHOT (REPRODUCIBILITY)
# ============================================================================

@dataclass(frozen=True)
class ClockSnapshot:
    """
    Snapshot of clock state for reproducibility.
    Stored with experiments, training runs, evaluation artifacts.
    """
    snapshot_id: str
    start_ticks: int
    current_ticks: int
    mode: ClockMode
    logical_offsets: Dict[str, int]
    creation_wall_time: datetime
    
    def validate(self) -> None:
        """Validate snapshot internal consistency."""
        if self.current_ticks < self.start_ticks:
            raise ValueError(
                f"Invalid snapshot: current_ticks ({self.current_ticks}) < "
                f"start_ticks ({self.start_ticks})"
            )
        if self.creation_wall_time.tzinfo is None:
            raise ValueError("creation_wall_time must be timezone-aware")


# ============================================================================
# DRIFT DETECTOR (ANOMALY DETECTION)
# ============================================================================

class DriftDetector:
    """
    Monitors for time anomalies:
    - backward tick attempts
    - abnormal jumps
    - wall-clock divergence
    - replay inconsistencies
    
    Any violation: logged, escalated, optionally hard-stop.
    """
    
    def __init__(self, policy: TimePolicy):
        self._policy = policy
        self._violations: list[str] = []
        self._lock = threading.Lock()
    
    def check_monotonic_violation(self, current: int, previous: int) -> None:
        """Check for backward time movement."""
        if current < previous:
            violation = (
                f"CRITICAL: Monotonic violation detected\n"
                f"Previous ticks: {previous}\n"
                f"Current ticks: {current}\n"
                f"Backward movement: {previous - current} ticks"
            )
            self._record_violation(violation)
            if self._policy.freeze_on_violation:
                raise RuntimeError(violation)
    
    def check_jump_anomaly(self, delta: int) -> None:
        """Check for abnormally large time jumps."""
        if delta > self._policy.max_drift_ticks:
            violation = (
                f"WARNING: Abnormal time jump detected\n"
                f"Jump size: {delta} ticks\n"
                f"Max allowed: {self._policy.max_drift_ticks} ticks"
            )
            self._record_violation(violation)
            if self._policy.freeze_on_violation:
                raise RuntimeError(violation)
    
    def check_wall_drift(self, monotonic_delta: int, wall_delta: float) -> None:
        """Check for divergence between monotonic and wall-clock time."""
        if not self._policy.allow_wall_clock:
            return
        
        # Convert monotonic delta to seconds
        monotonic_seconds = monotonic_delta / self._policy.ticks_per_second
        
        # Allow 10% drift tolerance
        if abs(monotonic_seconds - wall_delta) > monotonic_seconds * 0.1:
            violation = (
                f"WARNING: Wall-clock drift detected\n"
                f"Monotonic delta: {monotonic_seconds:.3f}s\n"
                f"Wall delta: {wall_delta:.3f}s\n"
                f"Divergence: {abs(monotonic_seconds - wall_delta):.3f}s"
            )
            self._record_violation(violation)
    
    def _record_violation(self, violation: str) -> None:
        """Record a violation for audit."""
        with self._lock:
            self._violations.append(violation)
            # In production, this would log to monitoring system
            print(f"[DriftDetector] {violation}")
    
    def get_violations(self) -> list[str]:
        """Get all recorded violations."""
        with self._lock:
            return self._violations.copy()


# ============================================================================
# TIME REPLAYER (REPLAY SUPPORT)
# ============================================================================

class TimeReplayer:
    """
    Feeds recorded tick sequences during replay.
    Ensures bit-identical timeline reconstruction.
    
    If time differs → experiment is invalid.
    """
    
    def __init__(self, snapshot: ClockSnapshot):
        self._snapshot = snapshot
        self._snapshot.validate()
        
        self._current_ticks = snapshot.start_ticks
        self._event_index = 0
        self._lock = threading.Lock()
        
        # Recorded timeline (in production, loaded from storage)
        self._timeline: list[int] = []
    
    def load_timeline(self, timeline: list[int]) -> None:
        """Load recorded timeline for replay."""
        with self._lock:
            if not timeline:
                raise ValueError("Timeline cannot be empty")
            if timeline[0] != self._snapshot.start_ticks:
                raise ValueError(
                    f"Timeline start mismatch: "
                    f"expected {self._snapshot.start_ticks}, got {timeline[0]}"
                )
            self._timeline = timeline
    
    def next_tick(self) -> int:
        """Get next tick from recorded timeline."""
        with self._lock:
            if not self._timeline:
                raise RuntimeError("Timeline not loaded")
            
            if self._event_index >= len(self._timeline):
                raise RuntimeError(
                    f"Replay exhausted: requested event {self._event_index}, "
                    f"timeline has {len(self._timeline)} events"
                )
            
            tick = self._timeline[self._event_index]
            self._event_index += 1
            
            # Validate monotonicity
            if tick < self._current_ticks:
                raise RuntimeError(
                    f"Replay violation: non-monotonic timeline at index {self._event_index - 1}\n"
                    f"Previous: {self._current_ticks}, Current: {tick}"
                )
            
            self._current_ticks = tick
            return tick
    
    def is_complete(self) -> bool:
        """Check if replay has consumed entire timeline."""
        with self._lock:
            return self._event_index >= len(self._timeline)


# ============================================================================
# CLOCK (SINGLE SOURCE OF TRUTH)
# ============================================================================

class Clock:
    """
    The one and only source of time for the entire system.
    
    Guarantees:
    - Monotonic progression (never goes backward)
    - Replay correctness (bit-identical reconstruction)
    - Deterministic ordering (logical clocks)
    - Audit trail (snapshots)
    
    Initialized once per run. Singleton pattern.
    """
    
    _instance: Optional['Clock'] = None
    _lock = threading.Lock()
    
    def __init__(
        self,
        runtime_context: RuntimeContext,
        policy: Optional[TimePolicy] = None
    ):
        self._runtime_context = runtime_context
        self._policy = policy or TimePolicy()
        
        # Determine mode from runtime context
        self._mode = self._determine_mode(runtime_context.mode)
        
        # Core state
        self._start_ticks: int = 0
        self._last_ticks: int = 0
        self._state_lock = threading.Lock()
        
        # Logical clocks (for event ordering)
        self._logical_clocks: Dict[str, int] = defaultdict(int)
        self._logical_lock = threading.Lock()
        
        # Components
        self._drift_detector = DriftDetector(self._policy)
        self._replayer: Optional[TimeReplayer] = None
        
        # Initialize
        self._initialize()
    
    @classmethod
    def get_instance(
        cls,
        runtime_context: RuntimeContext,
        policy: Optional[TimePolicy] = None
    ) -> 'Clock':
        """Singleton accessor."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = Clock(runtime_context, policy)
        return cls._instance
    
    def _determine_mode(self, runtime_mode: ExecutionMode) -> ClockMode:
        """Map runtime mode to clock mode."""
        if runtime_mode == ExecutionMode.REPLAY:
            return ClockMode.REPLAY
        elif runtime_mode == ExecutionMode.BACKTEST:
            return ClockMode.SIMULATION
        else:
            return ClockMode.LIVE
    
    def _initialize(self) -> None:
        """Initialize clock based on mode."""
        if self._mode == ClockMode.LIVE:
            # Use system monotonic clock
            self._start_ticks = self._get_system_monotonic_ticks()
            self._last_ticks = self._start_ticks
        elif self._mode == ClockMode.SIMULATION:
            # Start at 0 for simulation
            self._start_ticks = 0
            self._last_ticks = 0
        elif self._mode == ClockMode.REPLAY:
            # Will be initialized from snapshot
            self._start_ticks = 0
            self._last_ticks = 0
        else:
            raise ValueError(f"Unknown clock mode: {self._mode}")
    
    def _get_system_monotonic_ticks(self) -> int:
        """Get system monotonic time converted to ticks."""
        # time.monotonic() returns seconds as float
        return int(time.monotonic() * self._policy.ticks_per_second)
    
    def now(self) -> TimePoint:
        """
        Get current time.
        
        Returns:
            TimePoint with monotonic ticks (always increasing)
            and optional wall-clock annotation.
        
        Never returns raw datetime.now().
        """
        with self._state_lock:
            if self._mode == ClockMode.LIVE:
                current_ticks = self._get_system_monotonic_ticks()
                
                # Validate monotonicity
                self._drift_detector.check_monotonic_violation(
                    current_ticks, self._last_ticks
                )
                
                # Check for abnormal jumps
                if current_ticks > self._last_ticks:
                    delta = current_ticks - self._last_ticks
                    self._drift_detector.check_jump_anomaly(delta)
                
                self._last_ticks = current_ticks
                
                # Optional wall-clock annotation
                wall_time = None
                if self._policy.allow_wall_clock:
                    wall_time = datetime.now(timezone.utc)
                
                return TimePoint(
                    ticks=current_ticks,
                    wall_time=wall_time,
                    source=TimeSource.MONOTONIC
                )
            
            elif self._mode == ClockMode.REPLAY:
                if self._replayer is None:
                    raise RuntimeError("Replayer not initialized for REPLAY mode")
                
                current_ticks = self._replayer.next_tick()
                self._last_ticks = current_ticks
                
                return TimePoint(
                    ticks=current_ticks,
                    wall_time=None,
                    source=TimeSource.REPLAY
                )
            
            elif self._mode == ClockMode.SIMULATION:
                # In simulation, time only advances via explicit tick()
                return TimePoint(
                    ticks=self._last_ticks,
                    wall_time=None,
                    source=TimeSource.MONOTONIC
                )
            
            else:
                raise RuntimeError(f"Invalid clock mode: {self._mode}")
    
    def monotonic(self) -> int:
        """
        Get low-level monotonic tick counter.
        
        Guarantees:
        - no backward movement
        - no jumps (except in simulation mode)
        - no resets
        """
        return self.now().ticks
    
    def logical_time(self, tag: str) -> TimePoint:
        """
        Get logical clock time for a specific tag.
        
        Used by:
        - experiments (window alignment)
        - evaluation windows
        - RL credit windows
        
        Logical clocks allow:
        - deterministic ordering
        - replay alignment
        - multi-agent coordination
        """
        if not self._policy.enable_logical_clocks:
            raise RuntimeError("Logical clocks are disabled in policy")
        
        with self._logical_lock:
            # Increment logical clock for this tag
            self._logical_clocks[tag] += 1
            logical_tick = self._logical_clocks[tag]
            
            return TimePoint(
                ticks=logical_tick,
                wall_time=None,
                source=TimeSource.LOGICAL
            )
    
    def sleep(self, delta: TimeDelta) -> None:
        """
        Sleep for a duration.
        
        In LIVE mode: real sleep
        In REPLAY mode: NO-OP (time comes from timeline)
        In SIMULATION mode: advance simulation time
        """
        if self._mode == ClockMode.LIVE:
            # Real sleep
            sleep_seconds = delta.to_seconds(self._policy.ticks_per_second)
            time.sleep(sleep_seconds)
        
        elif self._mode == ClockMode.REPLAY:
            # NO-OP in replay - time comes from recorded timeline
            pass
        
        elif self._mode == ClockMode.SIMULATION:
            # Advance simulation time
            with self._state_lock:
                self._last_ticks += delta.ticks
        
        else:
            raise RuntimeError(f"Invalid clock mode: {self._mode}")
    
    def tick(self, amount: int = 1) -> None:
        """
        Manually advance clock (simulation mode only).
        Used for testing and deterministic simulation.
        """
        if self._mode != ClockMode.SIMULATION:
            raise RuntimeError("tick() only allowed in SIMULATION mode")
        
        with self._state_lock:
            self._last_ticks += amount
    
    def snapshot(self, snapshot_id: str) -> ClockSnapshot:
        """
        Create a snapshot of current clock state.
        Stored with experiments, training runs, evaluation artifacts.
        """
        with self._state_lock:
            with self._logical_lock:
                return ClockSnapshot(
                    snapshot_id=snapshot_id,
                    start_ticks=self._start_ticks,
                    current_ticks=self._last_ticks,
                    mode=self._mode,
                    logical_offsets=dict(self._logical_clocks),
                    creation_wall_time=datetime.now(timezone.utc)
                )
    
    def restore_from_snapshot(self, snapshot: ClockSnapshot) -> None:
        """
        Restore clock state from snapshot (for replay).
        """
        snapshot.validate()
        
        if self._mode != ClockMode.REPLAY:
            raise RuntimeError("restore_from_snapshot() only allowed in REPLAY mode")
        
        with self._state_lock:
            with self._logical_lock:
                self._start_ticks = snapshot.start_ticks
                self._last_ticks = snapshot.current_ticks
                self._logical_clocks = defaultdict(int, snapshot.logical_offsets)
                
                # Initialize replayer
                self._replayer = TimeReplayer(snapshot)
    
    def get_drift_violations(self) -> list[str]:
        """Get all recorded drift violations."""
        return self._drift_detector.get_violations()
    
    def reset_logical_clock(self, tag: str) -> None:
        """Reset a specific logical clock (testing only)."""
        with self._logical_lock:
            self._logical_clocks[tag] = 0
    
    def get_mode(self) -> ClockMode:
        """Get current clock mode."""
        return self._mode
    
    def get_policy(self) -> TimePolicy:
        """Get current time policy."""
        return self._policy


# ============================================================================
# CLOCK WATCHDOG (ENFORCEMENT)
# ============================================================================

class ClockWatchdog:
    """
    Monitors clock for violations and anomalies.
    
    Triggers on:
    - monotonic violations
    - unauthorized wall-clock usage
    - replay drift
    - time-based nondeterminism
    
    Can:
    - freeze orchestration
    - halt posting
    - invalidate experiments
    - trip global kill-switch
    """
    
    def __init__(self, clock: Clock):
        self._clock = clock
        self._monitoring = False
        self._lock = threading.Lock()
    
    def start_monitoring(self) -> None:
        """Start watchdog monitoring."""
        with self._lock:
            self._monitoring = True
    
    def stop_monitoring(self) -> None:
        """Stop watchdog monitoring."""
        with self._lock:
            self._monitoring = False
    
    def check_violations(self) -> list[str]:
        """Check for any violations and return them."""
        if not self._monitoring:
            return []
        
        violations = self._clock.get_drift_violations()
        
        # In production, this would:
        # - Log to monitoring system
        # - Trigger alerts
        # - Potentially halt execution
        
        return violations
    
    def enforce_no_wall_clock(self) -> None:
        """Enforce that wall-clock is not being used."""
        policy = self._clock.get_policy()
        if policy.allow_wall_clock:
            raise RuntimeError(
                "WATCHDOG VIOLATION: Wall-clock usage detected but not allowed"
            )


# ============================================================================
# MODULE-LEVEL HELPERS
# ============================================================================

def initialize_clock(
    runtime_context: RuntimeContext,
    policy: Optional[TimePolicy] = None
) -> Clock:
    """
    Initialize the global clock system.
    Called once at process boot.
    """
    clock = Clock.get_instance(runtime_context, policy)
    return clock


def get_clock() -> Clock:
    """Get the singleton Clock instance."""
    if Clock._instance is None:
        raise RuntimeError(
            "Clock not initialized. "
            "Call initialize_clock() at process boot."
        )
    return Clock._instance


# ============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# ============================================================================

def _forbidden_time_time():
    """❌ NEVER USE time.time() - use Clock.now() instead"""
    raise NotImplementedError(
        "time.time() is FORBIDDEN. Use Clock.now() instead."
    )


def _forbidden_datetime_now():
    """❌ NEVER USE datetime.now() - use Clock.now() instead"""
    raise NotImplementedError(
        "datetime.now() is FORBIDDEN. Use Clock.now() instead."
    )


def _forbidden_sleep():
    """❌ NEVER USE time.sleep() in core logic - use Clock.sleep() instead"""
    raise NotImplementedError(
        "time.sleep() is FORBIDDEN in core logic. Use Clock.sleep() instead."
    )















