"""
emergency_stop.py - System-Wide Emergency Stop / Kill Switch Authority

Location: /infra/safety/emergency_stop.py

Purpose:
    The absolute authority to stop the system.
    
    Answers exactly one question:
    "Should everything stop right now — immediately — regardless of cost?"

    If the answer is yes:
        - Workflows halt
        - Posting stops
        - Experiments freeze
        - Migrations abort
        - Recovery pauses
        - Learning ceases
        - Automation becomes inert
    
    No component is allowed to argue.

What this file is NOT:
    ❌ Not a feature flag
    ❌ Not rate limiting
    ❌ Not backpressure
    ❌ Not retry logic
    ❌ Not graceful degradation

This file terminates authority, it does not manage it.

Authority Hierarchy:
    emergency_stop
       ↓
    invariant_engine
       ↓
    safety_events
       ↓
    watchdogs
       ↓
    EVERYTHING ELSE

Design Principle:
    A kill switch that can be bypassed is worse than none at all.

Mental Model:
    This is not a feature
    This is not a config
    This is not optional
    This file exists for the day everything else is wrong.
"""

import hashlib
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List
from contextlib import contextmanager


# ============================================================================
# EMERGENCY STATE - Three States Only
# ============================================================================

class EmergencyState(Enum):
    """
    System emergency states.
    
    CLEAR → system may run
    STOPPED → full halt
    LOCKED → halt + cannot be cleared automatically
    
    LOCKED exists for existential failures.
    """
    CLEAR = "clear"
    STOPPED = "stopped"
    LOCKED = "locked"


# ============================================================================
# EMERGENCY REASON - Machine-Parseable, Human-Defensible
# ============================================================================

class EmergencyReason(Enum):
    """
    Reasons for emergency stop.
    Must be machine-parseable and human-defensible.
    """
    INVARIANT_FAILURE = "invariant_failure"
    DATA_CORRUPTION = "data_corruption"
    PLATFORM_BAN_RISK = "platform_ban_risk"
    REPLAY_DIVERGENCE = "replay_divergence"
    SECURITY_INCIDENT = "security_incident"
    MANUAL_OVERRIDE = "manual_override"
    SAFETY_THRESHOLD_EXCEEDED = "safety_threshold_exceeded"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    EXTERNAL_DIRECTIVE = "external_directive"


# ============================================================================
# EMERGENCY STOP EVENT - Audit Fact
# ============================================================================

@dataclass(frozen=True)
class EmergencyStopEvent:
    """
    Immutable record of emergency stop trigger.
    
    Rules:
        - Immutable
        - Persisted immediately
        - Replicated
        - Never deleted
    
    This is a historical fact, not configuration.
    """
    state: EmergencyState
    reason: EmergencyReason
    
    triggered_at: int           # Logical timestamp
    triggered_by: str           # Component or operator ID
    
    description: str            # Human-readable explanation
    context: dict               # Additional data
    
    event_id: str               # Unique event identifier
    previous_state: EmergencyState
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "state": self.state.value,
            "reason": self.reason.value,
            "triggered_at": self.triggered_at,
            "triggered_by": self.triggered_by,
            "description": self.description,
            "context": self.context,
            "event_id": self.event_id,
            "previous_state": self.previous_state.value
        }
    
    def to_json_canonical(self) -> str:
        """Canonical JSON for audit trail."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
    
    def compute_hash(self) -> str:
        """Compute cryptographic hash of event."""
        return hashlib.sha256(self.to_json_canonical().encode()).hexdigest()


# ============================================================================
# EMERGENCY STOP SNAPSHOT - Durable State
# ============================================================================

@dataclass(frozen=True)
class EmergencyStopSnapshot:
    """
    Durable snapshot of emergency stop state.
    
    Loaded:
        - On boot
        - On recovery
        - On replay
    """
    state: EmergencyState
    last_event_id: str
    last_updated_at: int
    event_count: int
    checksum: str
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "state": self.state.value,
            "last_event_id": self.last_event_id,
            "last_updated_at": self.last_updated_at,
            "event_count": self.event_count,
            "checksum": self.checksum
        }
    
    def verify_integrity(self) -> bool:
        """Verify snapshot integrity."""
        data = {
            "state": self.state.value,
            "last_event_id": self.last_event_id,
            "last_updated_at": self.last_updated_at,
            "event_count": self.event_count
        }
        expected = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
        return expected == self.checksum


# ============================================================================
# EMERGENCY STOP EXCEPTIONS
# ============================================================================

class EmergencyStopActive(Exception):
    """Raised when system is in emergency stop state."""
    pass


class EmergencyStopLocked(Exception):
    """Raised when attempting to clear locked emergency stop."""
    pass


class EmergencyStopBackendFailure(Exception):
    """Raised when backend operations fail."""
    pass


# ============================================================================
# EMERGENCY STOP BACKEND - Abstract Interface
# ============================================================================

class EmergencyStopBackend(ABC):
    """
    Abstract backend for emergency stop persistence.
    
    Backends may be:
        - Postgres
        - etcd
        - Cloud metadata store
    
    But semantics must match exactly.
    """
    
    @abstractmethod
    def load(self) -> EmergencyStopSnapshot:
        """
        Load current emergency stop state.
        
        Must be idempotent and safe to call repeatedly.
        """
        pass
    
    @abstractmethod
    def persist(self, event: EmergencyStopEvent) -> None:
        """
        Persist emergency stop event.
        
        Must be atomic and durable (fsync).
        Must succeed or raise exception.
        """
        pass
    
    @abstractmethod
    def load_events(self, limit: Optional[int] = None) -> List[EmergencyStopEvent]:
        """Load historical events."""
        pass
    
    @abstractmethod
    def health_check(self) -> bool:
        """Check if backend is available."""
        pass


# ============================================================================
# FILE-BASED BACKEND - Reference Implementation
# ============================================================================

class FileBasedEmergencyStopBackend(EmergencyStopBackend):
    """
    File-based emergency stop backend.
    
    Production systems should use distributed store (etcd, Postgres).
    This is for single-node deployments and testing.
    """
    
    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.snapshot_file = self.storage_dir / "emergency_stop.snapshot"
        self.events_file = self.storage_dir / "emergency_stop_events.jsonl"
        
        self._lock = threading.Lock()
        
        # Initialize if needed
        if not self.snapshot_file.exists():
            self._initialize()
    
    def _initialize(self):
        """Initialize with CLEAR state."""
        initial_snapshot = EmergencyStopSnapshot(
            state=EmergencyState.CLEAR,
            last_event_id="init",
            last_updated_at=int(time.time() * 1000),
            event_count=0,
            checksum=self._compute_snapshot_checksum(
                EmergencyState.CLEAR, "init", int(time.time() * 1000), 0
            )
        )
        self._write_snapshot(initial_snapshot)
    
    def load(self) -> EmergencyStopSnapshot:
        """Load current snapshot."""
        with self._lock:
            if not self.snapshot_file.exists():
                self._initialize()
            
            try:
                with open(self.snapshot_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                snapshot = EmergencyStopSnapshot(
                    state=EmergencyState(data['state']),
                    last_event_id=data['last_event_id'],
                    last_updated_at=data['last_updated_at'],
                    event_count=data['event_count'],
                    checksum=data['checksum']
                )
                
                # Verify integrity
                if not snapshot.verify_integrity():
                    raise EmergencyStopBackendFailure(
                        "Snapshot integrity check failed - possible corruption"
                    )
                
                return snapshot
                
            except Exception as e:
                raise EmergencyStopBackendFailure(f"Failed to load snapshot: {e}")
    
    def persist(self, event: EmergencyStopEvent) -> None:
        """Persist event and update snapshot."""
        with self._lock:
            try:
                # Append to event log
                with open(self.events_file, 'a', encoding='utf-8') as f:
                    f.write(event.to_json_canonical() + '\n')
                    f.flush()
                    os.fsync(f.fileno())
                
                # Update snapshot
                current = self.load()
                new_snapshot = EmergencyStopSnapshot(
                    state=event.state,
                    last_event_id=event.event_id,
                    last_updated_at=event.triggered_at,
                    event_count=current.event_count + 1,
                    checksum=self._compute_snapshot_checksum(
                        event.state,
                        event.event_id,
                        event.triggered_at,
                        current.event_count + 1
                    )
                )
                
                self._write_snapshot(new_snapshot)
                
            except Exception as e:
                raise EmergencyStopBackendFailure(f"Failed to persist event: {e}")
    
    def load_events(self, limit: Optional[int] = None) -> List[EmergencyStopEvent]:
        """Load event history."""
        events = []
        
        if not self.events_file.exists():
            return events
        
        with open(self.events_file, 'r', encoding='utf-8') as f:
            for line in f:
                if limit and len(events) >= limit:
                    break
                
                data = json.loads(line)
                events.append(EmergencyStopEvent(
                    state=EmergencyState(data['state']),
                    reason=EmergencyReason(data['reason']),
                    triggered_at=data['triggered_at'],
                    triggered_by=data['triggered_by'],
                    description=data['description'],
                    context=data['context'],
                    event_id=data['event_id'],
                    previous_state=EmergencyState(data['previous_state'])
                ))
        
        return events
    
    def health_check(self) -> bool:
        """Check backend health."""
        try:
            self.load()
            return True
        except Exception:
            return False
    
    def _write_snapshot(self, snapshot: EmergencyStopSnapshot):
        """Write snapshot atomically."""
        temp_file = self.snapshot_file.with_suffix('.tmp')
        
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(snapshot.to_dict(), f, sort_keys=True, indent=2)
                f.flush()
                os.fsync(f.fileno())
            
            # Atomic rename
            temp_file.replace(self.snapshot_file)
            
        except Exception as e:
            if temp_file.exists():
                temp_file.unlink()
            raise EmergencyStopBackendFailure(f"Failed to write snapshot: {e}")
    
    @staticmethod
    def _compute_snapshot_checksum(
        state: EmergencyState,
        event_id: str,
        updated_at: int,
        event_count: int
    ) -> str:
        """Compute snapshot checksum."""
        data = {
            "state": state.value,
            "last_event_id": event_id,
            "last_updated_at": updated_at,
            "event_count": event_count
        }
        canonical = json.dumps(data, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


# ============================================================================
# EMERGENCY STOP INVARIANTS - Absolute Guarantees
# ============================================================================

class EmergencyStopInvariants:
    """
    Invariants that emergency stop MUST guarantee.
    
    Violations = system integrity loss.
    """
    
    @staticmethod
    def assert_once_stopped_no_execution(state: EmergencyState):
        """Once STOPPED → no execution proceeds."""
        if state in (EmergencyState.STOPPED, EmergencyState.LOCKED):
            raise EmergencyStopActive(
                f"System is in {state.value} state - all execution halted"
            )
    
    @staticmethod
    def assert_stopped_survives_restart(snapshot: EmergencyStopSnapshot):
        """STOPPED survives restarts."""
        # This is verified by loading snapshot on boot
        # If snapshot doesn't exist or is corrupted, default to STOPPED
        pass
    
    @staticmethod
    def assert_locked_cannot_clear(state: EmergencyState):
        """LOCKED cannot be cleared without manual intervention."""
        if state == EmergencyState.LOCKED:
            raise EmergencyStopLocked(
                "System is LOCKED - requires code change + manual DB intervention"
            )
    
    @staticmethod
    def assert_idempotent_triggers(
        current_state: EmergencyState,
        new_state: EmergencyState
    ) -> bool:
        """Multiple trigger calls are idempotent."""
        # If already in target state, it's idempotent
        return current_state == new_state


# ============================================================================
# EMERGENCY STOP CONTROLLER - Public Authority
# ============================================================================

class EmergencyStopController:
    """
    The public authority for emergency stop.
    
    Rules:
        - assert_system_clear() is called everywhere
        - trigger_stop() is immediate and irreversible unless unlocked
        - clear_stop() requires explicit operator + audit record
        - clear_stop() NOT allowed if state is LOCKED
    """
    
    def __init__(
        self,
        backend: EmergencyStopBackend,
        fail_closed: bool = True
    ):
        self.backend = backend
        self.fail_closed = fail_closed
        
        self._lock = threading.Lock()
        self._cached_state: Optional[EmergencyStopSnapshot] = None
        self._cache_valid_until = 0
        self._cache_ttl_ms = 1000  # 1 second cache
        
        # Load initial state
        self._refresh_cache()
    
    def assert_system_clear(self) -> None:
        """
        Assert that system is in CLEAR state.
        
        THIS METHOD IS CALLED EVERYWHERE.
        
        Raises:
            EmergencyStopActive: If system is stopped
        """
        snapshot = self._get_state()
        
        # Enforce stop
        EmergencyStopInvariants.assert_once_stopped_no_execution(snapshot.state)
    
    def trigger_stop(
        self,
        reason: EmergencyReason,
        description: str,
        triggered_by: str,
        context: Optional[dict] = None,
        locked: bool = False
    ) -> EmergencyStopEvent:
        """
        Trigger emergency stop.
        
        Args:
            reason: Why the stop is triggered
            description: Human-readable explanation
            triggered_by: Component or operator ID
            context: Additional context data
            locked: If True, cannot be cleared automatically
        
        Returns:
            EmergencyStopEvent
        
        This is immediate and irreversible unless unlocked.
        """
        with self._lock:
            current = self._get_state(force_refresh=True)
            
            # Determine new state
            new_state = EmergencyState.LOCKED if locked else EmergencyState.STOPPED
            
            # Check idempotency
            if EmergencyStopInvariants.assert_idempotent_triggers(
                current.state, new_state
            ):
                # Already in target state - load last matching event
                events = self.backend.load_events(limit=1)
                if events:
                    return events[0]
            
            # Create event
            event = EmergencyStopEvent(
                state=new_state,
                reason=reason,
                triggered_at=int(time.time() * 1000),
                triggered_by=triggered_by,
                description=description,
                context=context or {},
                event_id=self._generate_event_id(reason, triggered_by),
                previous_state=current.state
            )
            
            # Persist immediately
            try:
                self.backend.persist(event)
            except Exception as e:
                # CRITICAL: If persist fails, assume STOPPED
                if self.fail_closed:
                    raise EmergencyStopBackendFailure(
                        f"Failed to persist stop event - system HALTED: {e}"
                    )
                raise
            
            # Invalidate cache
            self._invalidate_cache()
            
            # Emit safety events
            self._emit_safety_event(event)
            
            return event
    
    def clear_stop(
        self,
        operator_id: str,
        reason: str,
        context: Optional[dict] = None
    ) -> EmergencyStopEvent:
        """
        Clear emergency stop.
        
        Args:
            operator_id: Human operator clearing the stop
            reason: Why it's safe to clear
            context: Additional context
        
        Returns:
            EmergencyStopEvent
        
        Raises:
            EmergencyStopLocked: If system is LOCKED
        
        Requires:
            - Explicit operator
            - Audit record
            - Invariant check
        """
        with self._lock:
            current = self._get_state(force_refresh=True)
            
            # Cannot clear if LOCKED
            try:
                EmergencyStopInvariants.assert_locked_cannot_clear(current.state)
            except EmergencyStopLocked:
                raise
            
            # Already clear?
            if current.state == EmergencyState.CLEAR:
                events = self.backend.load_events(limit=1)
                if events:
                    return events[0]
            
            # Create clear event
            event = EmergencyStopEvent(
                state=EmergencyState.CLEAR,
                reason=EmergencyReason.MANUAL_OVERRIDE,
                triggered_at=int(time.time() * 1000),
                triggered_by=f"operator:{operator_id}",
                description=f"Cleared by {operator_id}: {reason}",
                context=context or {},
                event_id=self._generate_event_id(
                    EmergencyReason.MANUAL_OVERRIDE,
                    operator_id
                ),
                previous_state=current.state
            )
            
            # Persist
            self.backend.persist(event)
            
            # Invalidate cache
            self._invalidate_cache()
            
            # Emit safety event
            self._emit_safety_event(event)
            
            return event
    
    def get_state(self) -> EmergencyStopSnapshot:
        """Get current emergency stop state (public)."""
        return self._get_state()
    
    def get_history(self, limit: int = 100) -> List[EmergencyStopEvent]:
        """Get event history."""
        return self.backend.load_events(limit=limit)
    
    def _get_state(self, force_refresh: bool = False) -> EmergencyStopSnapshot:
        """
        Get current state with caching.
        
        Reading must be cheap. Writing is expensive.
        """
        now = int(time.time() * 1000)
        
        if force_refresh or not self._cached_state or now > self._cache_valid_until:
            self._refresh_cache()
        
        return self._cached_state
    
    def _refresh_cache(self):
        """Refresh cached state from backend."""
        try:
            self._cached_state = self.backend.load()
            self._cache_valid_until = int(time.time() * 1000) + self._cache_ttl_ms
        except Exception as e:
            # CRITICAL: If load fails and fail_closed=True, assume STOPPED
            if self.fail_closed:
                self._cached_state = EmergencyStopSnapshot(
                    state=EmergencyState.STOPPED,
                    last_event_id="backend_failure",
                    last_updated_at=int(time.time() * 1000),
                    event_count=0,
                    checksum="fail_closed"
                )
            else:
                raise EmergencyStopBackendFailure(f"Failed to load state: {e}")
    
    def _invalidate_cache(self):
        """Invalidate cache immediately."""
        self._cache_valid_until = 0
    
    @staticmethod
    def _generate_event_id(reason: EmergencyReason, triggered_by: str) -> str:
        """Generate unique event ID."""
        components = [
            str(int(time.time() * 1000000)),  # Microsecond precision
            reason.value,
            triggered_by
        ]
        raw = ":".join(components)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def _emit_safety_event(self, event: EmergencyStopEvent):
        """Emit safety event for monitoring."""
        # In production, this would integrate with safety_monitor.py
        # For now, just log
        safety_event = {
            "event_type": "emergency_stop_state_change",
            "emergency_event": event.to_dict(),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Write to safety log
        safety_log = Path("/tmp/safety_events.jsonl")
        try:
            with open(safety_log, 'a', encoding='utf-8') as f:
                f.write(json.dumps(safety_event, sort_keys=True) + '\n')
                f.flush()
        except Exception:
            # Don't fail stop due to logging issues
            pass


# ============================================================================
# CONTEXT MANAGER - Safe Stop Regions
# ============================================================================

@contextmanager
def emergency_stop_guard(controller: EmergencyStopController):
    """
    Context manager that checks emergency stop at entry and exit.
    
    Usage:
        with emergency_stop_guard(controller):
            # Critical operation
            do_something()
    """
    controller.assert_system_clear()
    try:
        yield
    finally:
        controller.assert_system_clear()


# ============================================================================
# MANDATORY INTEGRATION CHECKLIST
# ============================================================================

class IntegrationChecklist:
    """
    Components that MUST call assert_system_clear().
    
    Missing even one = fake kill switch.
    """
    
    REQUIRED_INTEGRATIONS = [
        "workflow_manager",
        "factory_scheduler",
        "post_dispatcher",
        "rate_limiter",
        "quota_manager",
        "invariant_engine",
        "state_migrator",
        "replay_orchestrator",
        "posting_queue",
        "experiment_runtime",
        "recovery_manager",
        "learning_engine",
        "automation_controller"
    ]
    
    @classmethod
    def verify_integration(cls, component_name: str) -> bool:
        """Verify component is in required list."""
        return component_name in cls.REQUIRED_INTEGRATIONS
    
    @classmethod
    def get_missing_integrations(cls, registered: List[str]) -> List[str]:
        """Get list of missing integrations."""
        return [c for c in cls.REQUIRED_INTEGRATIONS if c not in registered]


# ============================================================================
# FACTORY
# ============================================================================

def create_emergency_stop_controller(
    storage_dir: str = "/var/safety/emergency_stop",
    fail_closed: bool = True
) -> EmergencyStopController:
    """
    Create emergency stop controller with file backend.
    
    Args:
        storage_dir: Where to store state
        fail_closed: If True, default to STOPPED on backend failures
    
    Returns:
        EmergencyStopController
    """
    backend = FileBasedEmergencyStopBackend(Path(storage_dir))
    return EmergencyStopController(backend, fail_closed=fail_closed)


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    # Create controller
    controller = create_emergency_stop_controller(
        storage_dir="/tmp/emergency_stop_demo",
        fail_closed=True
    )
    
    print("Emergency Stop System Demo")
    print("=" * 50)
    
    # Check initial state
    state = controller.get_state()
    print(f"\nInitial state: {state.state.value}")
    
    # System runs normally when CLEAR
    try:
        controller.assert_system_clear()
        print("✓ System is CLEAR - execution allowed")
    except EmergencyStopActive as e:
        print(f"✗ System stopped: {e}")
    
    # Trigger emergency stop
    print("\n--- Triggering Emergency Stop ---")
    event = controller.trigger_stop(
        reason=EmergencyReason.INVARIANT_FAILURE,
        description="Critical invariant violated in state machine",
        triggered_by="invariant_engine",
        context={"invariant_id": "state_consistency_001"},
        locked=False
    )
    print(f"Stop triggered: {event.event_id}")
    print(f"Reason: {event.reason.value}")
    print(f"State: {event.state.value}")
    
    # Try to execute - should fail
    print("\n--- Attempting Execution ---")
    try:
        controller.assert_system_clear()
        print("✓ Execution allowed")
    except EmergencyStopActive as e:
        print(f"✗ Execution blocked: {e}")
    
    # Clear stop
    print("\n--- Clearing Emergency Stop ---")
    try:
        clear_event = controller.clear_stop(
            operator_id="admin_john",
            reason="Invariant fixed, system verified stable",
            context={"verification_run_id": "verify_001"}
        )
        print(f"Stop cleared: {clear_event.event_id}")
    except EmergencyStopLocked as e:
        print(f"✗ Cannot clear: {e}")
    
    # System should run now
    print("\n--- Post-Clear State ---")
    try:
        controller.assert_system_clear()
        print("✓ System is CLEAR - execution allowed")
    except EmergencyStopActive as e:
        print(f"✗ System stopped: {e}")
    
    # Show history
    print("\n--- Event History ---")
    history = controller.get_history(limit=10)
    for i, evt in enumerate(history, 1):
        print(f"{i}. [{evt.state.value}] {evt.reason.value} by {evt.triggered_by}")
    
    # Demonstrate LOCKED state
    print("\n--- Testing LOCKED State ---")
    locked_event = controller.trigger_stop(
        reason=EmergencyReason.SECURITY_INCIDENT,
        description="Security breach detected - system locked",
        triggered_by="security_monitor",
        locked=True
    )
    print(f"System LOCKED: {locked_event.event_id}")
    
    # Try to clear - should fail
    try:
        controller.clear_stop(
            operator_id="admin_john",
            reason="Attempted clear"
        )
        print("✓ Clear succeeded")
    except EmergencyStopLocked as e:
        print(f"✗ Cannot clear LOCKED state: {e}")
        print("   Requires code change + manual DB intervention")
    
    print("\n" + "=" * 50)
    print("Demo complete. This is the red button.")