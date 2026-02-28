"""
orchestration/agent_comms.py

Deterministic Inter-Agent Signaling & Coordination Bus

This module provides typed, versioned message passing between autonomous components
without direct coupling. Ensures RL replay validity, orchestration determinism,
and failure isolation.

Core Principles:
- Agents may NOT call each other directly
- All communication via declared signals
- Deterministic delivery ordering
- Audit-safe traces
- Schema-validated payloads
"""

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Optional, Callable, Dict, List, Any, Set, Union, get_origin, get_args
from collections import defaultdict
import hashlib
import time
import json
import logging
from threading import Lock, get_ident

logger = logging.getLogger(__name__)


# ============================================================================
# Signal Type Taxonomy
# ============================================================================

class SignalType(Enum):
    """Exhaustive enumeration of all valid signal types."""
    PRIORITY_UPDATE = "priority_update"
    EXECUTION_ADMISSION_RESULT = "execution_admission_result"
    METRIC_UPDATE = "metric_update"
    MODEL_PREDICTION = "model_prediction"
    ANOMALY_ALERT = "anomaly_alert"
    FAILURE_EVENT = "failure_event"
    RECOVERY_EVENT = "recovery_event"
    SHUTDOWN = "shutdown"


class SignalScope(Enum):
    """Signal delivery scope isolation."""
    VIDEO = "video"          # Single video context
    FACTORY = "factory"      # Factory-wide
    GLOBAL = "global"        # System-wide


class DeliveryPolicy(Enum):
    """Message delivery semantics."""
    AT_MOST_ONCE = "at_most_once"              # Fire and forget
    AT_LEAST_ONCE = "at_least_once"            # Retry until delivered
    EXACTLY_ONCE_SIMULATED = "exactly_once"    # RL-safe deduplication


# ============================================================================
# Signal Schemas
# ============================================================================

@dataclass(frozen=True)
class PredictionSchema:
    """Schema for MODEL_PREDICTION signals."""
    video_id: str
    model_name: str
    prediction: Dict[str, float]
    confidence: float
    timestamp: float


@dataclass(frozen=True)
class AnomalySchema:
    """Schema for ANOMALY_ALERT signals."""
    video_id: str
    anomaly_type: str
    severity: str  # "low", "medium", "high", "critical"
    detected_at: float
    metrics: Dict[str, float]


@dataclass(frozen=True)
class PriorityUpdateSchema:
    """Schema for PRIORITY_UPDATE signals."""
    video_id: str
    old_priority: float
    new_priority: float
    reason: str
    updated_at: float


@dataclass(frozen=True)
class AdmissionResultSchema:
    """Schema for EXECUTION_ADMISSION_RESULT signals."""
    video_id: str
    admitted: bool
    factory_id: Optional[str]
    reason: str
    timestamp: float


@dataclass(frozen=True)
class MetricUpdateSchema:
    """Schema for METRIC_UPDATE signals."""
    entity_id: str
    entity_type: str  # "video", "factory", "system"
    metrics: Dict[str, float]
    timestamp: float


@dataclass(frozen=True)
class FailureEventSchema:
    """Schema for FAILURE_EVENT signals."""
    component: str
    video_id: Optional[str]
    error_type: str
    error_message: str
    timestamp: float
    stack_trace: Optional[str]


@dataclass(frozen=True)
class RecoveryEventSchema:
    """Schema for RECOVERY_EVENT signals."""
    component: str
    video_id: Optional[str]
    recovery_action: str
    timestamp: float


@dataclass(frozen=True)
class ShutdownSchema:
    """Schema for SHUTDOWN signals."""
    initiator: str
    reason: str
    graceful: bool
    timestamp: float


# Schema Registry
SIGNAL_SCHEMA_REGISTRY: Dict[SignalType, type] = {
    SignalType.MODEL_PREDICTION: PredictionSchema,
    SignalType.ANOMALY_ALERT: AnomalySchema,
    SignalType.PRIORITY_UPDATE: PriorityUpdateSchema,
    SignalType.EXECUTION_ADMISSION_RESULT: AdmissionResultSchema,
    SignalType.METRIC_UPDATE: MetricUpdateSchema,
    SignalType.FAILURE_EVENT: FailureEventSchema,
    SignalType.RECOVERY_EVENT: RecoveryEventSchema,
    SignalType.SHUTDOWN: ShutdownSchema,
}


# ============================================================================
# Core Message Models
# ============================================================================

@dataclass(frozen=True)
class AgentSignal:
    """
    Base immutable signal type.
    
    All inter-agent communication uses this structure.
    No anonymous signals. No free-form payloads.
    """
    signal_id: str
    signal_type: SignalType
    scope: SignalScope
    
    source: str                      # Emitting component
    target: Optional[str]            # Explicit target or None for broadcast
    
    created_at: float
    payload: Any                     # Strictly typed by signal_type
    
    version: str = "1.0.0"          # Schema version
    replay_epoch: int = 0           # RL replay epoch (monotonic)
    
    def __post_init__(self):
        """Validate signal invariants."""
        if not self.signal_id:
            raise ValueError("signal_id cannot be empty")
        if not self.source:
            raise ValueError("source cannot be empty")
        if self.created_at <= 0:
            raise ValueError("created_at must be positive")
        if self.replay_epoch < 0:
            raise ValueError("replay_epoch must be non-negative")


@dataclass
class SignalEnvelope:
    """
    Delivery wrapper with bounded semantics.
    
    Enforces TTL, retry limits, and delivery policies.
    """
    signal: AgentSignal
    
    ttl_seconds: int = 60
    max_retries: int = 3
    delivery_policy: DeliveryPolicy = DeliveryPolicy.AT_MOST_ONCE
    
    # Internal delivery tracking
    attempts: int = field(default=0, init=False)
    delivered: bool = field(default=False, init=False)
    first_attempt_at: Optional[float] = field(default=None, init=False)
    sequence_id: int = field(default=0, init=False)
    
    def is_expired(self) -> bool:
        """Check if envelope has exceeded TTL."""
        if self.first_attempt_at is None:
            return False
        return (time.time() - self.first_attempt_at) > self.ttl_seconds
    
    def can_retry(self) -> bool:
        """Check if retry is allowed."""
        return self.attempts < self.max_retries and not self.is_expired()
    
    def mark_attempt(self):
        """Record delivery attempt."""
        if self.first_attempt_at is None:
            self.first_attempt_at = time.time()
        self.attempts += 1
    
    def mark_delivered(self):
        """Mark as successfully delivered."""
        self.delivered = True


# ============================================================================
# Signal Validation
# ============================================================================

class SignalValidator:
    """
    Enforces schema compliance and version compatibility.
    
    Schema drift is a hard failure.
    """
    
    @staticmethod
    def validate(signal: AgentSignal) -> None:
        """
        Validate signal against registered schema.
        
        Raises:
            ValueError: If validation fails
        """
        # Check signal type is registered
        if signal.signal_type not in SIGNAL_SCHEMA_REGISTRY:
            raise ValueError(f"Unknown signal type: {signal.signal_type}")
        
        # Get expected schema
        schema_cls = SIGNAL_SCHEMA_REGISTRY[signal.signal_type]
        
        # Validate payload type
        if not isinstance(signal.payload, schema_cls):
            raise ValueError(
                f"Invalid payload type for {signal.signal_type}. "
                f"Expected {schema_cls.__name__}, got {type(signal.payload).__name__}"
            )
        
        # Validate payload fields and types
        SignalValidator._validate_payload_fields(signal.payload)
        
        # Validate scope constraints
        SignalValidator._validate_scope(signal)
        
        # Validate version
        SignalValidator._validate_version(signal)
    
    @staticmethod
    def _validate_scope(signal: AgentSignal) -> None:
        """Validate scope-specific constraints."""
        if signal.scope == SignalScope.VIDEO:
            # VIDEO-scoped signals must have video_id in payload
            if not hasattr(signal.payload, 'video_id'):
                raise ValueError("VIDEO-scoped signal missing video_id")
        
        elif signal.scope == SignalScope.GLOBAL:
            # GLOBAL signals cannot target a single component
            if signal.target is not None:
                raise ValueError("GLOBAL signals cannot have explicit target")
    
    @staticmethod
    def _validate_version(signal: AgentSignal) -> None:
        """Validate schema version compatibility."""
        # Simple version validation - can be extended
        parts = signal.version.split('.')
        if len(parts) != 3:
            raise ValueError(f"Invalid version format: {signal.version}")
        
        try:
            major, minor, patch = map(int, parts)
        except ValueError:
            raise ValueError(f"Invalid version format: {signal.version}")
        
        # Major version must be 1 for now
        if major != 1:
            raise ValueError(f"Unsupported major version: {major}")
    
    @staticmethod
    def _validate_payload_fields(payload: Any) -> None:
        """Validate dataclass fields and runtime types."""
        if not is_dataclass(payload):
            raise ValueError("Payload must be a dataclass instance")
        
        for field_info in fields(payload):
            value = getattr(payload, field_info.name)
            expected_type = field_info.type
            if not SignalValidator._is_instance(value, expected_type):
                raise ValueError(
                    f"Invalid field type for {field_info.name}. "
                    f"Expected {expected_type}, got {type(value).__name__}"
                )
    
    @staticmethod
    def _is_instance(value: Any, expected_type: Any) -> bool:
        """Runtime type validation with typing support."""
        if expected_type is Any:
            return True
        
        origin = get_origin(expected_type)
        args = get_args(expected_type)
        
        if origin is None:
            if expected_type is float:
                return isinstance(value, (float, int))
            return isinstance(value, expected_type)
        
        if origin is list:
            if not isinstance(value, list):
                return False
            if not args:
                return True
            return all(SignalValidator._is_instance(item, args[0]) for item in value)
        
        if origin is set:
            if not isinstance(value, set):
                return False
            if not args:
                return True
            return all(SignalValidator._is_instance(item, args[0]) for item in value)
        
        if origin is dict:
            if not isinstance(value, dict):
                return False
            if len(args) != 2:
                return True
            key_type, value_type = args
            return all(
                SignalValidator._is_instance(k, key_type)
                and SignalValidator._is_instance(v, value_type)
                for k, v in value.items()
            )
        
        if origin is Union:
            return any(SignalValidator._is_instance(value, option) for option in args)
        
        return isinstance(value, expected_type)


# ============================================================================
# Replay Fence (RL-Safe)
# ============================================================================

class ReplayFence:
    """
    Prevents signal duplication during RL replay.
    
    Enforces monotonic epoch and prevents time-travel leakage.
    """
    
    def __init__(self):
        self._seen_signals: Set[str] = set()
        self._current_epoch: int = 0
        self._lock = Lock()
    
    def check_and_mark(self, signal: AgentSignal) -> bool:
        """
        Check if signal should be delivered.
        
        Returns:
            True if signal is new, False if duplicate
        """
        with self._lock:
            if signal.replay_epoch != self._current_epoch:
                logger.warning(
                    f"Replay epoch mismatch for {signal.signal_id}: "
                    f"{signal.replay_epoch} != {self._current_epoch}"
                )
                return False
            if signal.signal_id in self._seen_signals:
                logger.warning(f"Duplicate signal blocked: {signal.signal_id}")
                return False
            
            self._seen_signals.add(signal.signal_id)
            return True
    
    def advance_epoch(self):
        """Advance replay epoch (for training)."""
        with self._lock:
            self._current_epoch += 1
            logger.info(f"Replay epoch advanced to {self._current_epoch}")
    
    def get_epoch(self) -> int:
        """Get current replay epoch."""
        with self._lock:
            return self._current_epoch
    
    def reset(self):
        """Reset fence (for new replay session)."""
        with self._lock:
            self._seen_signals.clear()
            self._current_epoch = 0
            logger.info("Replay fence reset")


# ============================================================================
# Signal Router
# ============================================================================

class SignalRouter:
    """
    Determines signal routing and scope isolation.
    
    Enforces:
    - VIDEO-scoped signals never reach FACTORY handlers
    - GLOBAL signals never target single video
    - Ordering of delivery
    """
    
    def __init__(self):
        # handlers[signal_type][scope] -> List[handler]
        self._handlers: Dict[SignalType, Dict[SignalScope, List[Callable]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._lock = Lock()
    
    def subscribe(
        self, 
        signal_type: SignalType, 
        scope: SignalScope,
        handler: Callable[[AgentSignal], None]
    ) -> None:
        """
        Subscribe handler to signal type and scope.
        
        Args:
            signal_type: Type of signal to handle
            scope: Scope to listen on
            handler: Callback function
        """
        with self._lock:
            self._handlers[signal_type][scope].append(handler)
            logger.info(
                f"Subscribed handler to {signal_type.value} "
                f"on scope {scope.value}"
            )
    
    def route(self, signal: AgentSignal) -> List[Callable]:
        """
        Get handlers for signal based on routing rules.
        
        Returns:
            List of handlers to invoke
        """
        with self._lock:
            handlers = []
            
            # Exact scope match
            if signal.signal_type in self._handlers:
                scope_handlers = self._handlers[signal.signal_type]
                
                if signal.scope in scope_handlers:
                    handlers.extend(scope_handlers[signal.scope])
                
                # GLOBAL scope handlers receive all signals of that type
                if signal.scope != SignalScope.GLOBAL and SignalScope.GLOBAL in scope_handlers:
                    handlers.extend(scope_handlers[SignalScope.GLOBAL])
            
            return handlers
    
    def unsubscribe_all(self, signal_type: SignalType, scope: SignalScope) -> None:
        """Remove all handlers for signal type and scope."""
        with self._lock:
            if signal_type in self._handlers:
                self._handlers[signal_type][scope].clear()


# ============================================================================
# Communication Audit Log
# ============================================================================

class CommAuditLog:
    """
    Mandatory audit trail for all signals.
    
    Required for debugging, audits, and rollback analysis.
    """
    
    def __init__(
        self,
        max_entries: int = 10000,
        persist_path: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None
    ):
        self._entries: List[Dict[str, Any]] = []
        self._max_entries = max_entries
        self._persist_path = persist_path
        self._clock = clock or time.time
        self._last_hash: Optional[str] = None
        self._lock = Lock()
    
    def log_publish(self, envelope: SignalEnvelope) -> None:
        """Log signal publication."""
        signal = envelope.signal
        entry = {
            "event": "publish",
            "signal_id": signal.signal_id,
            "type": signal.signal_type.value,
            "scope": signal.scope.value,
            "source": signal.source,
            "target": signal.target,
            "timestamp": signal.created_at,
            "delivery_policy": envelope.delivery_policy.value,
        }
        self._add_entry(entry)
    
    def log_delivery(self, signal: AgentSignal, handler: str, success: bool) -> None:
        """Log signal delivery attempt."""
        entry = {
            "event": "delivery",
            "signal_id": signal.signal_id,
            "type": signal.signal_type.value,
            "handler": handler,
            "success": success,
            "timestamp": self._clock(),
        }
        self._add_entry(entry)
    
    def log_drop(self, signal: AgentSignal, reason: str) -> None:
        """Log signal drop."""
        entry = {
            "event": "drop",
            "signal_id": signal.signal_id,
            "type": signal.signal_type.value,
            "reason": reason,
            "timestamp": self._clock(),
        }
        self._add_entry(entry)
    
    def _add_entry(self, entry: Dict[str, Any]) -> None:
        """Add entry to log with rotation."""
        with self._lock:
            entry["prev_hash"] = self._last_hash
            entry_json = json.dumps(entry, sort_keys=True)
            entry_hash = hashlib.sha256(entry_json.encode("utf-8")).hexdigest()
            entry["hash"] = entry_hash
            self._last_hash = entry_hash

            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                self._entries.pop(0)
            
            if self._persist_path:
                with open(self._persist_path, "a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps(entry) + "\n")
    
    def get_recent(self, n: int = 100) -> List[Dict[str, Any]]:
        """Get recent log entries."""
        with self._lock:
            return self._entries[-n:]
    
    def export_json(self) -> str:
        """Export log as JSON."""
        with self._lock:
            return json.dumps(self._entries, indent=2)


# ============================================================================
# Agent Communication Bus (CORE ENGINE)
# ============================================================================

class AgentCommBus:
    """
    Deterministic inter-agent communication bus.
    
    Properties:
    - Synchronous by default
    - Deterministic ordering
    - Explicitly drained by orchestration
    - No hidden threads
    
    This is the ONLY safe way agents communicate.
    """
    
    def __init__(
        self,
        enable_replay_fence: bool = True,
        audit_log_path: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
        enforce_single_publisher: bool = True
    ):
        self._clock = clock or time.time
        self._router = SignalRouter()
        self._validator = SignalValidator()
        self._audit_log = CommAuditLog(persist_path=audit_log_path, clock=self._clock)
        self._replay_fence = ReplayFence() if enable_replay_fence else None
        
        self._pending_envelopes: List[SignalEnvelope] = []
        self._lock = Lock()
        self._sequence_id = 0
        self._publisher_thread_id: Optional[int] = None
        self._enforce_single_publisher = enforce_single_publisher
        
        logger.info("AgentCommBus initialized")
    
    def publish(self, envelope: SignalEnvelope) -> None:
        """
        Publish signal for delivery.
        
        Validates schema, scope, version. Enforces TTL and delivery policy.
        Records audit entry.
        
        Args:
            envelope: Signal envelope to publish
            
        Raises:
            ValueError: If validation fails
        """
        signal = envelope.signal
        
        # Enforce single-threaded publishing for determinism
        if self._enforce_single_publisher:
            current_thread_id = get_ident()
            if self._publisher_thread_id is None:
                self._publisher_thread_id = current_thread_id
            elif self._publisher_thread_id != current_thread_id:
                raise RuntimeError(
                    "Non-deterministic publish detected: multiple publisher threads."
                )
        
        # Step 1: Validate schema
        self._validator.validate(signal)
        
        # Step 2: Check replay fence (RL safety)
        if self._replay_fence:
            if not self._replay_fence.check_and_mark(signal):
                self._audit_log.log_drop(signal, "replay_fence_blocked")
                raise ValueError("Replay fence blocked signal delivery")
        
        # Step 3: Record audit
        self._audit_log.log_publish(envelope)
        
        # Step 4: Add to pending queue
        with self._lock:
            self._sequence_id += 1
            envelope.sequence_id = self._sequence_id
            self._pending_envelopes.append(envelope)
        
        logger.debug(
            f"Published signal {signal.signal_id} "
            f"type={signal.signal_type.value} "
            f"source={signal.source}"
        )
    
    def subscribe(
        self, 
        signal_type: SignalType,
        handler: Callable[[AgentSignal], None],
        scope: SignalScope = SignalScope.GLOBAL
    ) -> None:
        """
        Subscribe to signal type.
        
        Args:
            signal_type: Type of signal to receive
            handler: Callback function(signal) -> None
            scope: Scope to listen on
        """
        self._router.subscribe(signal_type, scope, handler)
    
    def drain(self) -> int:
        """
        Process all pending signals.
        
        Invoked explicitly by orchestration layer.
        Guarantees deterministic ordering.
        
        Returns:
            Number of signals processed
        """
        processed = 0
        
        with self._lock:
            envelopes_to_process = self._pending_envelopes[:]
            self._pending_envelopes.clear()
        
        envelopes_to_process.sort(key=lambda env: env.sequence_id)
        
        for envelope in envelopes_to_process:
            signal = envelope.signal
            
            # Check expiration
            if envelope.is_expired():
                self._audit_log.log_drop(signal, "ttl_expired")
                logger.warning(f"Signal {signal.signal_id} expired")
                continue
            
            # Mark attempt
            envelope.mark_attempt()
            
            # Route to handlers
            handlers = self._router.route(signal)
            
            if not handlers:
                logger.debug(f"No handlers for {signal.signal_type.value}")
                envelope.mark_delivered()
                processed += 1
                continue
            
            # Deliver to handlers
            delivery_success = True
            for handler in handlers:
                try:
                    handler(signal)
                    self._audit_log.log_delivery(
                        signal, 
                        handler.__name__, 
                        True
                    )
                except Exception as e:
                    logger.error(
                        f"Handler {handler.__name__} failed for "
                        f"signal {signal.signal_id}: {e}"
                    )
                    self._audit_log.log_delivery(
                        signal, 
                        handler.__name__, 
                        False
                    )
                    delivery_success = False
            
            # Handle delivery policy
            if delivery_success:
                envelope.mark_delivered()
                processed += 1
            else:
                # Retry logic
                if envelope.delivery_policy == DeliveryPolicy.AT_LEAST_ONCE:
                    if envelope.can_retry():
                        with self._lock:
                            self._pending_envelopes.append(envelope)
                        logger.info(
                            f"Retrying signal {signal.signal_id} "
                            f"(attempt {envelope.attempts}/{envelope.max_retries})"
                        )
                    else:
                        self._audit_log.log_drop(signal, "max_retries_exceeded")
                        logger.error(
                            f"Signal {signal.signal_id} dropped after "
                            f"{envelope.attempts} attempts"
                        )
        
        if processed > 0:
            logger.info(f"Drained {processed} signals")
        
        return processed
    
    def get_audit_log(self) -> CommAuditLog:
        """Get audit log for inspection."""
        return self._audit_log
    
    def get_pending_count(self) -> int:
        """Get count of pending signals."""
        with self._lock:
            return len(self._pending_envelopes)
    
    def clear_pending(self) -> None:
        """Clear all pending signals (emergency use only)."""
        with self._lock:
            dropped = len(self._pending_envelopes)
            self._pending_envelopes.clear()
        logger.warning(f"Cleared {dropped} pending signals")
    
    def get_replay_epoch(self) -> int:
        """Get current replay epoch."""
        if not self._replay_fence:
            return 0
        return self._replay_fence.get_epoch()
    
    def advance_replay_epoch(self) -> None:
        """Advance replay epoch (training use)."""
        if not self._replay_fence:
            raise RuntimeError("Replay fence is disabled")
        self._replay_fence.advance_epoch()


# ============================================================================
# Convenience Functions
# ============================================================================

def create_signal(
    signal_type: SignalType,
    source: str,
    payload: Any,
    scope: SignalScope = SignalScope.GLOBAL,
    target: Optional[str] = None,
    signal_id: Optional[str] = None,
    created_at: Optional[float] = None,
    replay_epoch: int = 0
) -> AgentSignal:
    """
    Create a properly-formed signal.
    
    Args:
        signal_type: Type of signal
        source: Emitting component name
        payload: Typed payload matching signal_type schema
        scope: Delivery scope
        target: Explicit target or None for broadcast
        signal_id: Optional ID (auto-generated if None)
    
    Returns:
        AgentSignal instance
    """
    if created_at is None:
        created_at = time.time()
    
    if signal_id is None:
        signal_id = f"{source}_{signal_type.value}_{int(created_at * 1000000)}"
    
    return AgentSignal(
        signal_id=signal_id,
        signal_type=signal_type,
        scope=scope,
        source=source,
        target=target,
        created_at=created_at,
        payload=payload,
        replay_epoch=replay_epoch
    )


def create_envelope(
    signal: AgentSignal,
    ttl_seconds: int = 60,
    max_retries: int = 3,
    delivery_policy: DeliveryPolicy = DeliveryPolicy.AT_MOST_ONCE
) -> SignalEnvelope:
    """
    Wrap signal in delivery envelope.
    
    Args:
        signal: Signal to wrap
        ttl_seconds: Time-to-live
        max_retries: Maximum retry attempts
        delivery_policy: Delivery semantics
    
    Returns:
        SignalEnvelope instance
    """
    return SignalEnvelope(
        signal=signal,
        ttl_seconds=ttl_seconds,
        max_retries=max_retries,
        delivery_policy=delivery_policy
    )


# ============================================================================
# Global Bus Instance (Optional Singleton)
# ============================================================================

_global_bus: Optional[AgentCommBus] = None


def get_global_bus() -> AgentCommBus:
    """Get or create global communication bus."""
    global _global_bus
    if _global_bus is None:
        _global_bus = AgentCommBus()
    return _global_bus


def reset_global_bus():
    """Reset global bus (testing only)."""
    global _global_bus
    _global_bus = None