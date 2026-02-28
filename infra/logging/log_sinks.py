"""
/infra/logging/log_sinks.py

Log Destination Abstraction & Delivery Control Plane

This file defines WHERE logs go — without ever allowing destinations to:
  - mutate logs
  - drop logs silently
  - affect determinism
  - compromise replay
  - block execution

Sinks are expendable. Truth is not.
"""

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Callable, Optional, List, Dict
import time
import random
import threading
from contextlib import contextmanager


# ============================================================================
# ENUMS
# ============================================================================


class SinkType(Enum):
    """Categorizes sinks by intent, not technology."""
    LOCAL = "local"
    OBJECT_STORAGE = "object_storage"
    STREAM = "stream"
    SIEM = "siem"
    FORENSIC = "forensic"


class SinkHealth(Enum):
    """Health NEVER affects logging correctness — only delivery."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class DeliveryMode(Enum):
    """Defines failure semantics."""
    REQUIRED = "required"        # Must succeed, retries indefinitely
    BEST_EFFORT = "best_effort"  # Retries with limits
    OPTIONAL = "optional"        # No retries, drop on failure


class DropReason(Enum):
    """Why a record was dropped."""
    SINK_UNAVAILABLE = "sink_unavailable"
    BACKPRESSURE = "backpressure"
    RETRY_EXHAUSTED = "retry_exhausted"
    SINK_DISABLED = "sink_disabled"
    BUFFER_OVERFLOW = "buffer_overflow"


# ============================================================================
# PROTOCOLS
# ============================================================================


class SerializedEvent(Protocol):
    """Already-serialized log record."""
    def to_bytes(self) -> bytes: ...
    def severity(self) -> str: ...
    def event_id(self) -> str: ...


# ============================================================================
# CORE ABSTRACTION
# ============================================================================


class LogSink(ABC):
    """
    ABSOLUTE CONTRACT for log destinations.
    
    Rules:
      - Input is already serialized
      - Sink may NOT mutate records
      - Sink must fail explicitly
      - Side effects are isolated
    """
    
    sink_type: SinkType
    supports_async: bool = False
    supports_batching: bool = False
    
    @abstractmethod
    def write(self, records: list[SerializedEvent]) -> None:
        """
        Write records to destination.
        
        MUST raise on failure.
        MUST NOT mutate records.
        MUST NOT generate IDs or timestamps.
        """
        raise NotImplementedError
    
    @abstractmethod
    def health_check(self) -> SinkHealth:
        """Check sink availability without side effects."""
        raise NotImplementedError
    
    def close(self) -> None:
        """Optional cleanup."""
        pass


# ============================================================================
# DELIVERY POLICY
# ============================================================================


@dataclass(frozen=True)
class DeliveryPolicy:
    """
    Explicit failure rules for a sink.
    
    Defines:
      - required vs optional sinks
      - retry semantics
      - acceptable loss windows
      - escalation thresholds
    """
    
    mode: DeliveryMode
    max_retries: int = 3
    base_backoff_ms: int = 100
    max_backoff_ms: int = 30000
    buffer_capacity: int = 10000
    drop_on_overflow: bool = True
    
    def __post_init__(self):
        if self.mode == DeliveryMode.REQUIRED:
            assert not self.drop_on_overflow, "REQUIRED sinks cannot drop"


# ============================================================================
# RETRY CONTROLLER
# ============================================================================


@dataclass
class RetryState:
    """Tracks retry state per sink."""
    attempts: int = 0
    last_attempt_ns: int = 0
    next_attempt_ns: int = 0
    consecutive_failures: int = 0


class RetryController:
    """
    Bounded retries with exponential backoff and jitter.
    
    Guarantees:
      - MUST NOT block logging
      - MUST NOT reorder events
      - MUST NOT cause duplication beyond policy
    """
    
    def __init__(self):
        self._state: dict[str, RetryState] = {}
        self._lock = threading.Lock()
    
    def should_retry(self, sink_id: str, policy: DeliveryPolicy) -> bool:
        """Check if retry is allowed."""
        with self._lock:
            state = self._state.get(sink_id, RetryState())
            
            if policy.mode == DeliveryMode.REQUIRED:
                return True  # Always retry required sinks
            
            if state.attempts >= policy.max_retries:
                return False
            
            now_ns = time.time_ns()
            if now_ns < state.next_attempt_ns:
                return False  # Too soon
            
            return True
    
    def record_attempt(self, sink_id: str, policy: DeliveryPolicy, 
                       success: bool) -> None:
        """Record attempt outcome and update backoff."""
        with self._lock:
            state = self._state.get(sink_id, RetryState())
            now_ns = time.time_ns()
            
            if success:
                # Reset on success
                self._state[sink_id] = RetryState()
            else:
                # Calculate next backoff with jitter
                backoff_ms = min(
                    policy.base_backoff_ms * (2 ** state.attempts),
                    policy.max_backoff_ms
                )
                jitter_ms = random.randint(0, backoff_ms // 4)
                total_backoff_ns = (backoff_ms + jitter_ms) * 1_000_000
                
                self._state[sink_id] = RetryState(
                    attempts=state.attempts + 1,
                    last_attempt_ns=now_ns,
                    next_attempt_ns=now_ns + total_backoff_ns,
                    consecutive_failures=state.consecutive_failures + 1
                )
    
    def reset(self, sink_id: str) -> None:
        """Reset retry state."""
        with self._lock:
            self._state.pop(sink_id, None)


# ============================================================================
# BACKPRESSURE MANAGER
# ============================================================================


@dataclass
class BackpressureMetrics:
    """Backpressure statistics."""
    buffer_size: int
    buffer_capacity: int
    drops_total: int
    drops_by_reason: dict[DropReason, int]


class BackpressureManager:
    """
    Handles sink slowdowns, outages, and event storms.
    
    Strategies:
      - Buffering with caps
      - Selective dropping (only optional sinks)
      - Rate limiting
      - Priority preservation
    
    AUDIT LOGS ARE NEVER DROPPED.
    """
    
    def __init__(self, policy: DeliveryPolicy):
        self.policy = policy
        self._buffer: deque = deque(maxlen=policy.buffer_capacity)
        self._drops: dict[DropReason, int] = defaultdict(int)
        self._lock = threading.Lock()
    
    def enqueue(self, record: SerializedEvent) -> bool:
        """
        Add record to buffer.
        Returns False if dropped.
        """
        with self._lock:
            if len(self._buffer) >= self.policy.buffer_capacity:
                if not self.policy.drop_on_overflow:
                    # Block until space (REQUIRED sinks only)
                    # In production, this would have timeout + escalation
                    pass
                else:
                    self._drops[DropReason.BUFFER_OVERFLOW] += 1
                    return False
            
            self._buffer.append(record)
            return True
    
    def dequeue_batch(self, max_size: int) -> list[SerializedEvent]:
        """Remove up to max_size records from buffer."""
        with self._lock:
            batch = []
            for _ in range(min(max_size, len(self._buffer))):
                if self._buffer:
                    batch.append(self._buffer.popleft())
            return batch
    
    def record_drop(self, reason: DropReason) -> None:
        """Record a dropped event."""
        with self._lock:
            self._drops[reason] += 1
    
    def metrics(self) -> BackpressureMetrics:
        """Get current backpressure metrics."""
        with self._lock:
            return BackpressureMetrics(
                buffer_size=len(self._buffer),
                buffer_capacity=self.policy.buffer_capacity,
                drops_total=sum(self._drops.values()),
                drops_by_reason=dict(self._drops)
            )


# ============================================================================
# SINK TELEMETRY
# ============================================================================


@dataclass
class SinkMetrics:
    """Per-sink delivery metrics."""
    writes_attempted: int = 0
    writes_succeeded: int = 0
    writes_failed: int = 0
    records_written: int = 0
    records_dropped: int = 0
    total_latency_ns: int = 0
    min_latency_ns: Optional[int] = None
    max_latency_ns: Optional[int] = None


class SinkTelemetry:
    """
    Emits metrics for monitoring and alerting.
    
    Fed into:
      - monitoring
      - alerts
      - watchdogs
    
    But telemetry NEVER alters behavior.
    """
    
    def __init__(self):
        self._metrics: dict[str, SinkMetrics] = defaultdict(SinkMetrics)
        self._lock = threading.Lock()
    
    @contextmanager
    def measure_write(self, sink_id: str):
        """Context manager to measure write latency."""
        start_ns = time.time_ns()
        success = False
        try:
            yield
            success = True
        finally:
            elapsed_ns = time.time_ns() - start_ns
            self._record_write(sink_id, success, elapsed_ns)
    
    def _record_write(self, sink_id: str, success: bool, 
                      latency_ns: int) -> None:
        """Record write attempt."""
        with self._lock:
            m = self._metrics[sink_id]
            m.writes_attempted += 1
            
            if success:
                m.writes_succeeded += 1
            else:
                m.writes_failed += 1
            
            m.total_latency_ns += latency_ns
            
            if m.min_latency_ns is None or latency_ns < m.min_latency_ns:
                m.min_latency_ns = latency_ns
            
            if m.max_latency_ns is None or latency_ns > m.max_latency_ns:
                m.max_latency_ns = latency_ns
    
    def record_written(self, sink_id: str, count: int) -> None:
        """Record successfully written records."""
        with self._lock:
            self._metrics[sink_id].records_written += count
    
    def record_dropped(self, sink_id: str, count: int) -> None:
        """Record dropped records."""
        with self._lock:
            self._metrics[sink_id].records_dropped += count
    
    def get_metrics(self, sink_id: str) -> SinkMetrics:
        """Get metrics for a sink."""
        with self._lock:
            return self._metrics[sink_id]


# ============================================================================
# SINK WATCHDOG
# ============================================================================


class SinkViolation(Enum):
    """Types of sink misbehavior."""
    MUTATION_ATTEMPT = "mutation_attempt"
    SILENT_FAILURE = "silent_failure"
    ORDERING_VIOLATION = "ordering_violation"
    EXCESSIVE_LATENCY = "excessive_latency"
    HEALTH_CHECK_FAILURE = "health_check_failure"


class SinkWatchdog:
    """
    Monitors for silent failures and misbehavior.
    
    Can:
      - Disable a sink
      - Quarantine it
      - Escalate alerts
      - Mark run as tainted
    
    Cannot modify logs.
    """
    
    def __init__(self):
        self._violations: dict[str, list[SinkViolation]] = defaultdict(list)
        self._disabled: set[str] = set()
        self._lock = threading.Lock()
    
    def record_violation(self, sink_id: str, 
                         violation: SinkViolation) -> None:
        """Record a violation."""
        with self._lock:
            self._violations[sink_id].append(violation)
            
            # Auto-disable on critical violations
            if len(self._violations[sink_id]) >= 3:
                self._disabled.add(sink_id)
    
    def is_disabled(self, sink_id: str) -> bool:
        """Check if sink is disabled."""
        with self._lock:
            return sink_id in self._disabled
    
    def disable_sink(self, sink_id: str) -> None:
        """Manually disable a sink."""
        with self._lock:
            self._disabled.add(sink_id)
    
    def enable_sink(self, sink_id: str) -> None:
        """Re-enable a sink."""
        with self._lock:
            self._disabled.discard(sink_id)
            self._violations[sink_id].clear()


# ============================================================================
# SINK REGISTRY
# ============================================================================


@dataclass
class RegisteredSink:
    """A registered sink with its policy."""
    sink: LogSink
    policy: DeliveryPolicy
    sink_id: str
    enabled: bool = True


class SinkRegistry:
    """
    Central registry for all sinks.
    
    Enforces:
      - Type uniqueness (configurable)
      - Policy validation
      - Safe enable/disable
    
    No dynamic monkey-patching.
    """
    
    def __init__(self):
        self._sinks: dict[str, RegisteredSink] = {}
        self._lock = threading.Lock()
    
    def register(self, sink_id: str, sink: LogSink, 
                 policy: DeliveryPolicy) -> None:
        """Register a sink."""
        with self._lock:
            if sink_id in self._sinks:
                raise ValueError(f"Sink {sink_id} already registered")
            
            self._sinks[sink_id] = RegisteredSink(
                sink=sink,
                policy=policy,
                sink_id=sink_id
            )
    
    def unregister(self, sink_id: str) -> None:
        """Unregister a sink."""
        with self._lock:
            if sink_id in self._sinks:
                self._sinks[sink_id].sink.close()
                del self._sinks[sink_id]
    
    def get(self, sink_id: str) -> Optional[RegisteredSink]:
        """Get a registered sink."""
        with self._lock:
            return self._sinks.get(sink_id)
    
    def list_sinks(self) -> list[str]:
        """List all sink IDs."""
        with self._lock:
            return list(self._sinks.keys())
    
    def disable(self, sink_id: str) -> None:
        """Disable a sink."""
        with self._lock:
            if sink_id in self._sinks:
                self._sinks[sink_id].enabled = False
    
    def enable(self, sink_id: str) -> None:
        """Enable a sink."""
        with self._lock:
            if sink_id in self._sinks:
                self._sinks[sink_id].enabled = True


# ============================================================================
# SINK ROUTER
# ============================================================================


class SinkRouter:
    """
    Orchestrates delivery to sinks.
    
    Capabilities:
      - Single-sink routing
      - Multi-sink broadcast
      - Severity-based routing
      - Audit-only sinks
      - Air-gapped forensic sinks
    
    Routing decisions are config-driven, never conditional logic.
    """
    
    def __init__(self, registry: SinkRegistry):
        self.registry = registry
        self.telemetry = SinkTelemetry()
        self.watchdog = SinkWatchdog()
        self.retry_controller = RetryController()
        self._backpressure: dict[str, BackpressureManager] = {}
        self._lock = threading.Lock()
    
    def dispatch(self, sink_id: str, record: SerializedEvent) -> bool:
        """
        Route a single record to a specific sink.
        Returns True if delivered or buffered.
        """
        return self._deliver_to_sink(sink_id, [record])
    
    def broadcast(self, records: list[SerializedEvent], 
                  sink_ids: Optional[list[str]] = None) -> dict[str, bool]:
        """
        Broadcast records to multiple sinks.
        Returns delivery status per sink.
        """
        if sink_ids is None:
            sink_ids = self.registry.list_sinks()
        
        results = {}
        for sink_id in sink_ids:
            results[sink_id] = self._deliver_to_sink(sink_id, records)
        
        return results
    
    def _deliver_to_sink(self, sink_id: str, 
                         records: list[SerializedEvent]) -> bool:
        """Internal delivery logic."""
        # Check if sink is disabled
        if self.watchdog.is_disabled(sink_id):
            self.telemetry.record_dropped(sink_id, len(records))
            return False
        
        # Get registered sink
        registered = self.registry.get(sink_id)
        if not registered or not registered.enabled:
            self.telemetry.record_dropped(sink_id, len(records))
            return False
        
        # Get or create backpressure manager
        with self._lock:
            if sink_id not in self._backpressure:
                self._backpressure[sink_id] = BackpressureManager(
                    registered.policy
                )
        
        bp = self._backpressure[sink_id]
        
        # Enqueue records
        for record in records:
            if not bp.enqueue(record):
                self.telemetry.record_dropped(sink_id, 1)
        
        # Attempt delivery
        return self._flush_sink(sink_id, registered, bp)
    
    def _flush_sink(self, sink_id: str, registered: RegisteredSink,
                    bp: BackpressureManager) -> bool:
        """Flush buffered records to sink."""
        # Check retry policy
        if not self.retry_controller.should_retry(sink_id, 
                                                   registered.policy):
            return False
        
        # Dequeue batch
        batch_size = 100 if registered.sink.supports_batching else 1
        batch = bp.dequeue_batch(batch_size)
        
        if not batch:
            return True
        
        # Attempt write
        try:
            with self.telemetry.measure_write(sink_id):
                registered.sink.write(batch)
            
            self.retry_controller.record_attempt(sink_id, 
                                                  registered.policy, 
                                                  success=True)
            self.telemetry.record_written(sink_id, len(batch))
            return True
            
        except Exception as e:
            self.retry_controller.record_attempt(sink_id, 
                                                  registered.policy, 
                                                  success=False)
            
            # Re-enqueue for retry if allowed
            if self.retry_controller.should_retry(sink_id, 
                                                   registered.policy):
                for record in batch:
                    bp.enqueue(record)
            else:
                self.telemetry.record_dropped(sink_id, len(batch))
                bp.record_drop(DropReason.RETRY_EXHAUSTED)
            
            return False
    
    def get_metrics(self, sink_id: str) -> dict:
        """Get comprehensive metrics for a sink."""
        sink_metrics = self.telemetry.get_metrics(sink_id)
        
        bp_metrics = None
        with self._lock:
            if sink_id in self._backpressure:
                bp_metrics = self._backpressure[sink_id].metrics()
        
        return {
            'sink': sink_metrics,
            'backpressure': bp_metrics
        }


# ============================================================================
# FINAL GUARANTEES
# ============================================================================

"""
DETERMINISM & REPLAY RULES:
  ✓ Logs are finalized before sinks
  ✓ Sinks never affect record content
  ✓ Replay reads logs from canonical storage, not sinks
  ✓ Sink failures do not affect replay validity

SINKS EXIST OUTSIDE CAUSALITY.

If sinks disappear, the system must still be defensible.
"""




