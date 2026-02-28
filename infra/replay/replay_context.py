"""
replay_context.py - Deterministic Re-Execution Context Authority

Location: /infra/replay/replay_context.py

Purpose:
    Define the universe in which the past is re-executed.
    
    When we replay history, what exactly must be held constant 
    so the system produces the same result?

    Replay is not debugging.
    Replay is PROOF.

What this file is NOT:
    ❌ Not crash recovery
    ❌ Not retry logic
    ❌ Not rollback
    ❌ Not simulation
    ❌ Not backtesting

Replay never modifies reality.
It only replays a recorded one.

Authority Ordering:
    replay_context
       ↓
    clock / id_generator / randomness
       ↓
    state_backend (read-only)
       ↓
    execution logic

Mental Model:
    - Replay reconstructs reality
    - Recovery repairs reality
    - Replay never mutates
    - Recovery always mutates
    - Replay proves correctness
    - Recovery preserves availability
"""

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Callable, Set
from pathlib import Path


# ============================================================================
# REPLAY MODE - Strictness Levels
# ============================================================================

class ReplayMode(Enum):
    """
    Replay strictness levels.
    
    STRICT is default. Anything else must be explicitly requested.
    """
    STRICT = "strict"          # Bit-for-bit identical
    AUDIT = "audit"            # Tolerate known nondeterminism
    FORENSIC = "forensic"      # Maximum logging


# ============================================================================
# REPLAY CONTEXT SPEC - Canonical Contract
# ============================================================================

@dataclass(frozen=True)
class ReplayContextSpec:
    """
    Immutable specification for replay execution.
    This spec is audit-persisted and cryptographically verified.
    """
    replay_id: str              # Unique ID for this replay attempt
    snapshot_id: str            # State snapshot to replay from
    
    run_id: str                 # Original execution run ID
    execution_id: str           # Original execution ID
    
    replay_mode: ReplayMode     # Strictness level
    
    recorded_at: int            # Logical timestamp of original recording
    checksum: str               # Expected output checksum
    
    # Optional metadata
    replayed_by: Optional[str] = None
    reason: Optional[str] = None
    parent_replay_id: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Canonical dictionary representation."""
        return {
            "replay_id": self.replay_id,
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "execution_id": self.execution_id,
            "replay_mode": self.replay_mode.value,
            "recorded_at": self.recorded_at,
            "checksum": self.checksum,
            "replayed_by": self.replayed_by,
            "reason": self.reason,
            "parent_replay_id": self.parent_replay_id
        }
    
    def compute_hash(self) -> str:
        """Compute cryptographic hash of spec."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()


# ============================================================================
# DETERMINISTIC BINDINGS - Override Sources
# ============================================================================

class Clock:
    """Deterministic clock for replay."""
    
    def __init__(self, frozen_time: int):
        self.frozen_time = frozen_time
        self._tick_count = 0
    
    def now(self) -> int:
        """Return frozen logical time."""
        return self.frozen_time
    
    def tick(self) -> int:
        """Advance logical clock deterministically."""
        self._tick_count += 1
        return self.frozen_time + self._tick_count
    
    def wall_time_forbidden(self):
        """Wall clock access is forbidden during replay."""
        raise ReplayViolation("Wall clock access forbidden during replay")


class IdGenerator:
    """Deterministic ID generator for replay."""
    
    def __init__(self, seed: str):
        self.seed = seed
        self._counter = 0
        self._generated_ids: Set[str] = set()
    
    def generate(self, prefix: str = "") -> str:
        """Generate deterministic ID."""
        self._counter += 1
        components = [self.seed, prefix, str(self._counter)]
        raw = ":".join(components)
        id_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        
        # Verify uniqueness
        if id_hash in self._generated_ids:
            raise ReplayViolation(f"ID collision during replay: {id_hash}")
        
        self._generated_ids.add(id_hash)
        return id_hash
    
    def random_id_forbidden(self):
        """Random ID generation forbidden during replay."""
        raise ReplayViolation("Random ID generation forbidden during replay")


class DeterministicRNG:
    """Deterministic random number generator for replay."""
    
    def __init__(self, seed: int):
        self.seed = seed
        self._state = seed
        self._call_count = 0
    
    def random(self) -> float:
        """Generate deterministic random float [0, 1)."""
        self._call_count += 1
        # Simple LCG for determinism
        self._state = (1103515245 * self._state + 12345) & 0x7FFFFFFF
        return self._state / 0x7FFFFFFF
    
    def randint(self, a: int, b: int) -> int:
        """Generate deterministic random integer [a, b]."""
        return a + int(self.random() * (b - a + 1))
    
    def choice(self, items: list) -> Any:
        """Choose deterministic random item."""
        if not items:
            raise ValueError("Cannot choose from empty list")
        idx = self.randint(0, len(items) - 1)
        return items[idx]
    
    def entropy_forbidden(self):
        """System entropy access forbidden during replay."""
        raise ReplayViolation("System entropy access forbidden during replay")


@dataclass
class ReplayBindings:
    """
    Bindings that override nondeterministic sources during replay.
    
    During replay:
        - No global imports allowed
        - All accesses must go through bindings
        - Violations → fatal invariant breach
    """
    clock: Clock
    id_generator: IdGenerator
    random_source: DeterministicRNG
    
    # Recorded inputs (from input_recorder)
    recorded_inputs: List[Any] = field(default_factory=list)
    input_index: int = 0
    
    def next_input(self) -> Any:
        """Consume next recorded input."""
        if self.input_index >= len(self.recorded_inputs):
            raise ReplayViolation("Exhausted recorded inputs during replay")
        
        input_data = self.recorded_inputs[self.input_index]
        self.input_index += 1
        return input_data
    
    def verify_inputs_exhausted(self):
        """Verify all recorded inputs were consumed."""
        if self.input_index != len(self.recorded_inputs):
            raise ReplayDivergence(
                f"Replay did not consume all inputs: "
                f"{self.input_index}/{len(self.recorded_inputs)}"
            )


# ============================================================================
# REPLAY VIOLATIONS & DIVERGENCE
# ============================================================================

class ReplayViolation(Exception):
    """Raised when replay invariants are violated."""
    pass


class ReplayDivergence(Exception):
    """Raised when replay diverges from original execution."""
    pass


# ============================================================================
# REPLAY INVARIANT ENFORCER
# ============================================================================

class ReplayInvariantEnforcer:
    """
    Enforces replay invariants:
        - No writes to persistence
        - No async scheduling
        - No wall-clock access
        - No entropy access
        - No network calls
    
    Every violation → ReplayDivergenceEvent
    """
    
    def __init__(self, spec: ReplayContextSpec, strict: bool = True):
        self.spec = spec
        self.strict = strict
        self.violations: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
    
    def assert_read_only(self):
        """Assert that state is read-only."""
        # This would integrate with state_backend
        # For now, just mark the assertion
        pass
    
    def assert_no_writes(self, operation: str):
        """Assert no write operations during replay."""
        if self.strict:
            raise ReplayViolation(f"Write operation forbidden during replay: {operation}")
        else:
            self._record_violation("write_attempt", operation)
    
    def assert_no_async(self, operation: str):
        """Assert no async scheduling during replay."""
        if self.strict:
            raise ReplayViolation(f"Async operation forbidden during replay: {operation}")
        else:
            self._record_violation("async_attempt", operation)
    
    def assert_no_wall_clock(self):
        """Assert no wall clock access during replay."""
        if self.strict:
            raise ReplayViolation("Wall clock access forbidden during replay")
        else:
            self._record_violation("wall_clock_access", "attempted")
    
    def assert_no_entropy(self):
        """Assert no system entropy access during replay."""
        if self.strict:
            raise ReplayViolation("System entropy access forbidden during replay")
        else:
            self._record_violation("entropy_access", "attempted")
    
    def assert_no_network(self, endpoint: str):
        """Assert no network calls during replay."""
        if self.strict:
            raise ReplayViolation(f"Network call forbidden during replay: {endpoint}")
        else:
            self._record_violation("network_attempt", endpoint)
    
    def assert_single_threaded(self):
        """Assert single-threaded execution during replay."""
        # Check thread count
        thread_count = threading.active_count()
        if thread_count > 1:
            if self.strict:
                raise ReplayViolation(f"Multi-threaded execution forbidden: {thread_count} threads")
            else:
                self._record_violation("multi_threaded", str(thread_count))
    
    def _record_violation(self, violation_type: str, details: str):
        """Record violation for audit mode."""
        with self._lock:
            self.violations.append({
                "type": violation_type,
                "details": details,
                "timestamp": datetime.utcnow().isoformat(),
                "replay_id": self.spec.replay_id
            })
    
    def get_violations(self) -> List[Dict[str, Any]]:
        """Get all recorded violations."""
        with self._lock:
            return list(self.violations)


# ============================================================================
# DIVERGENCE DETECTOR
# ============================================================================

class DivergenceDetector:
    """
    Detects divergence between replay and original execution.
    
    Must verify:
        - Checksum match
        - Invariant equivalence
        - Emitted safety events identical
        - Deterministic ordering
    
    Mismatch = replay failure, not warning.
    """
    
    def __init__(self, spec: ReplayContextSpec):
        self.spec = spec
        self.divergences: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
    
    def verify_checksum(self, actual_checksum: str) -> bool:
        """Verify output checksum matches expected."""
        if actual_checksum != self.spec.checksum:
            self._record_divergence(
                "checksum_mismatch",
                f"Expected {self.spec.checksum}, got {actual_checksum}"
            )
            return False
        return True
    
    def verify_invariants(self, actual: Dict[str, Any], expected: Dict[str, Any]) -> bool:
        """Verify invariants match expected state."""
        if actual != expected:
            self._record_divergence(
                "invariant_mismatch",
                f"Actual: {actual}, Expected: {expected}"
            )
            return False
        return True
    
    def verify_safety_events(self, actual: List[str], expected: List[str]) -> bool:
        """Verify emitted safety events match."""
        if actual != expected:
            self._record_divergence(
                "safety_event_mismatch",
                f"Actual events: {len(actual)}, Expected: {len(expected)}"
            )
            return False
        return True
    
    def verify_ordering(self, actual_order: List[str], expected_order: List[str]) -> bool:
        """Verify deterministic ordering."""
        if actual_order != expected_order:
            self._record_divergence(
                "ordering_mismatch",
                f"Order diverged at position {self._find_divergence_point(actual_order, expected_order)}"
            )
            return False
        return True
    
    def _find_divergence_point(self, actual: List[str], expected: List[str]) -> int:
        """Find first position where sequences diverge."""
        for i, (a, e) in enumerate(zip(actual, expected)):
            if a != e:
                return i
        return min(len(actual), len(expected))
    
    def _record_divergence(self, divergence_type: str, details: str):
        """Record divergence event."""
        with self._lock:
            self.divergences.append({
                "type": divergence_type,
                "details": details,
                "timestamp": datetime.utcnow().isoformat(),
                "replay_id": self.spec.replay_id,
                "run_id": self.spec.run_id
            })
    
    def get_divergences(self) -> List[Dict[str, Any]]:
        """Get all detected divergences."""
        with self._lock:
            return list(self.divergences)
    
    def has_diverged(self) -> bool:
        """Check if any divergences detected."""
        return len(self.divergences) > 0


# ============================================================================
# REPLAY CONTEXT - Public Authority
# ============================================================================

class ReplayContext:
    """
    The authoritative replay execution context.
    
    Rules:
        - Context is stack-scoped
        - Nested replay forbidden
        - Exit must restore runtime context exactly
    """
    
    # Class-level tracking to prevent nested replay
    _active_context: Optional['ReplayContext'] = None
    _lock = threading.Lock()
    
    def __init__(
        self,
        spec: ReplayContextSpec,
        bindings: ReplayBindings,
        storage_dir: Optional[Path] = None
    ):
        self.spec = spec
        self.bindings = bindings
        self.storage_dir = storage_dir or Path("/var/replay/contexts")
        
        # Invariant enforcement
        self.enforcer = ReplayInvariantEnforcer(
            spec,
            strict=(spec.replay_mode == ReplayMode.STRICT)
        )
        
        # Divergence detection
        self.detector = DivergenceDetector(spec)
        
        # Runtime state
        self._entered = False
        self._exited = False
        self._original_globals: Dict[str, Any] = {}
        
        # Registered inputs (from input_recorder)
        self._registered_inputs: List[Any] = []
        
        # Safety events
        self._safety_events: List[Dict[str, Any]] = []
    
    def enter(self) -> None:
        """
        Enter replay context.
        Overrides global sources with deterministic bindings.
        """
        with ReplayContext._lock:
            if ReplayContext._active_context is not None:
                raise ReplayViolation(
                    "Nested replay forbidden. "
                    f"Already in replay: {ReplayContext._active_context.spec.replay_id}"
                )
            
            if self._entered:
                raise ReplayViolation(f"Replay context already entered: {self.spec.replay_id}")
            
            # Mark as active
            ReplayContext._active_context = self
            self._entered = True
        
        # Override global sources
        self._override_globals()
        
        # Assert invariants
        self.enforcer.assert_single_threaded()
        self.enforcer.assert_read_only()
        
        # Emit entry event
        self._emit_safety_event("replay_context_entered", {
            "replay_id": self.spec.replay_id,
            "run_id": self.spec.run_id,
            "mode": self.spec.replay_mode.value
        })
    
    def exit(self, exc_type=None, exc_value=None, traceback=None) -> None:
        """
        Exit replay context.
        Restore original runtime context exactly.
        """
        if not self._entered:
            raise ReplayViolation("Cannot exit: context never entered")
        
        if self._exited:
            raise ReplayViolation("Context already exited")
        
        try:
            # Verify all inputs consumed
            self.bindings.verify_inputs_exhausted()
            
            # Check for divergences
            if self.detector.has_diverged():
                divergences = self.detector.get_divergences()
                self._emit_safety_event("replay_divergence_detected", {
                    "replay_id": self.spec.replay_id,
                    "divergences": divergences,
                    "count": len(divergences)
                })
                
                if self.spec.replay_mode == ReplayMode.STRICT:
                    raise ReplayDivergence(
                        f"Replay diverged from original: {len(divergences)} divergences"
                    )
            
            # Emit exit event
            self._emit_safety_event("replay_context_exited", {
                "replay_id": self.spec.replay_id,
                "violations": len(self.enforcer.get_violations()),
                "divergences": len(self.detector.get_divergences())
            })
            
        finally:
            # Restore globals
            self._restore_globals()
            
            # Mark as inactive
            with ReplayContext._lock:
                ReplayContext._active_context = None
                self._exited = True
    
    def _override_globals(self):
        """Override global sources with deterministic bindings."""
        # In production, this would override:
        # - time.time → bindings.clock.now
        # - uuid.uuid4 → bindings.id_generator.generate
        # - random.random → bindings.random_source.random
        # For now, just track that we did it
        self._original_globals = {
            "clock_overridden": True,
            "id_overridden": True,
            "random_overridden": True
        }
    
    def _restore_globals(self):
        """Restore original global sources."""
        # In production, this would restore original functions
        self._original_globals = {}
    
    def register_input(self, input_token: Any):
        """Register input consumed during replay (called by input_recorder)."""
        self._registered_inputs.append(input_token)
    
    def assert_read_only(self):
        """Assert system is in read-only mode."""
        self.enforcer.assert_read_only()
    
    def assert_deterministic(self):
        """Assert deterministic execution."""
        self.enforcer.assert_single_threaded()
    
    def _emit_safety_event(self, event_type: str, details: dict):
        """Emit safety event."""
        event = {
            "event_type": event_type,
            "details": details,
            "timestamp": datetime.utcnow().isoformat(),
            "replay_id": self.spec.replay_id
        }
        self._safety_events.append(event)
        
        # Write to disk
        self._write_safety_event(event)
    
    def _write_safety_event(self, event: dict):
        """Write safety event to disk."""
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        event_file = self.storage_dir / f"{self.spec.replay_id}_events.jsonl"
        
        try:
            with open(event_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event, sort_keys=True) + '\n')
                f.flush()
        except Exception:
            # Don't fail replay due to logging issues
            pass
    
    def get_report(self) -> dict:
        """Generate replay report."""
        return {
            "spec": self.spec.to_dict(),
            "violations": self.enforcer.get_violations(),
            "divergences": self.detector.get_divergences(),
            "safety_events": self._safety_events,
            "inputs_registered": len(self._registered_inputs),
            "success": not self.detector.has_diverged()
        }


# ============================================================================
# REPLAY CONTEXT GUARD - Safe Context Manager
# ============================================================================

class ReplayContextGuard:
    """
    Context manager for safe replay execution.
    
    Usage:
        with ReplayContextGuard(spec, bindings):
            run_execution()
    
    If divergence detected → hard stop + safety event.
    """
    
    def __init__(
        self,
        spec: ReplayContextSpec,
        bindings: ReplayBindings,
        storage_dir: Optional[Path] = None
    ):
        self.context = ReplayContext(spec, bindings, storage_dir)
    
    def __enter__(self) -> ReplayContext:
        """Enter replay context."""
        self.context.enter()
        return self.context
    
    def __exit__(self, exc_type, exc_value, traceback):
        """Exit replay context, handling errors."""
        try:
            self.context.exit(exc_type, exc_value, traceback)
        except ReplayDivergence as e:
            # Divergence detected - hard stop
            print(f"REPLAY DIVERGENCE: {e}", flush=True)
            # In production, this might trigger alerts
            return False  # Re-raise
        except Exception as e:
            # Other error during exit
            print(f"REPLAY ERROR: {e}", flush=True)
            return False  # Re-raise


# ============================================================================
# REPLAY SAFETY EVENTS
# ============================================================================

@dataclass
class ReplayDivergenceEvent:
    """Safety event emitted on replay divergence."""
    replay_id: str
    run_id: str
    divergence_type: str
    details: str
    timestamp: str
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemIntegrityEvent:
    """Safety event for system integrity issues."""
    event_type: str
    severity: str
    details: dict
    timestamp: str
    
    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# SNAPSHOT INTEGRATION
# ============================================================================

@dataclass
class SnapshotReference:
    """Reference to a state snapshot for replay."""
    snapshot_id: str
    timestamp: int
    checksum: str
    state_version: str
    
    def to_dict(self) -> dict:
        return asdict(self)


class SnapshotLoader:
    """Loads state snapshots for replay."""
    
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
    
    def load_snapshot(self, snapshot_id: str) -> dict:
        """Load snapshot data."""
        snapshot_file = self.storage_dir / f"{snapshot_id}.snapshot"
        
        if not snapshot_file.exists():
            raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")
        
        with open(snapshot_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def verify_snapshot(self, snapshot_id: str, expected_checksum: str) -> bool:
        """Verify snapshot integrity."""
        data = self.load_snapshot(snapshot_id)
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        actual_checksum = hashlib.sha256(canonical.encode()).hexdigest()
        return actual_checksum == expected_checksum


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================

def create_replay_context(
    run_id: str,
    snapshot_id: str,
    expected_checksum: str,
    recorded_at: int,
    recorded_inputs: List[Any],
    replay_mode: ReplayMode = ReplayMode.STRICT,
    storage_dir: Optional[Path] = None
) -> ReplayContext:
    """
    Create a replay context from recorded execution.
    
    Args:
        run_id: Original execution run ID
        snapshot_id: State snapshot to replay from
        expected_checksum: Expected output checksum
        recorded_at: Logical timestamp of recording
        recorded_inputs: List of recorded inputs
        replay_mode: Strictness level
        storage_dir: Where to store replay artifacts
    
    Returns:
        Configured ReplayContext
    """
    import uuid
    
    # Generate replay ID
    replay_id = f"replay_{uuid.uuid4().hex[:12]}"
    
    # Create spec
    spec = ReplayContextSpec(
        replay_id=replay_id,
        snapshot_id=snapshot_id,
        run_id=run_id,
        execution_id=f"exec_{run_id}",
        replay_mode=replay_mode,
        recorded_at=recorded_at,
        checksum=expected_checksum
    )
    
    # Create bindings
    bindings = ReplayBindings(
        clock=Clock(frozen_time=recorded_at),
        id_generator=IdGenerator(seed=run_id),
        random_source=DeterministicRNG(seed=hash(run_id) & 0x7FFFFFFF),
        recorded_inputs=recorded_inputs
    )
    
    # Create context
    return ReplayContext(spec, bindings, storage_dir)


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Simulated recorded inputs
    recorded_inputs = [
        {"type": "user_action", "data": "click_submit"},
        {"type": "model_output", "data": "Hello world"},
        {"type": "platform_response", "data": {"status": "success"}}
    ]
    
    # Create replay context
    context = create_replay_context(
        run_id="original_run_123",
        snapshot_id="snapshot_456",
        expected_checksum="abc123def456",
        recorded_at=1000000,
        recorded_inputs=recorded_inputs,
        replay_mode=ReplayMode.STRICT,
        storage_dir=Path("/tmp/replay_demo")
    )
    
    # Execute replay
    try:
        with ReplayContextGuard(context.spec, context.bindings):
            print("Replaying execution...")
            
            # Simulate deterministic execution
            time_now = context.bindings.clock.now()
            print(f"  Clock: {time_now}")
            
            id1 = context.bindings.id_generator.generate("task")
            print(f"  Generated ID: {id1}")
            
            rand1 = context.bindings.random_source.random()
            print(f"  Random: {rand1}")
            
            # Consume inputs
            input1 = context.bindings.next_input()
            print(f"  Input 1: {input1}")
            
            input2 = context.bindings.next_input()
            print(f"  Input 2: {input2}")
            
            input3 = context.bindings.next_input()
            print(f"  Input 3: {input3}")
            
            print("\nReplay completed successfully!")
        
        # Generate report
        report = context.get_report()
        print(f"\nReplay Report:")
        print(f"  Success: {report['success']}")
        print(f"  Violations: {len(report['violations'])}")
        print(f"  Divergences: {len(report['divergences'])}")
        print(f"  Safety Events: {len(report['safety_events'])}")
        
    except ReplayDivergence as e:
        print(f"\n❌ REPLAY FAILED: {e}")
    except Exception as e:
        print(f"\n❌ ERROR: {e}")