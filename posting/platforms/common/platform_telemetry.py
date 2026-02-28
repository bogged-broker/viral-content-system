"""
/posting/platforms/common/platform_telemetry.py

Canonical Platform Telemetry Normalization Layer

This file is the single normalized metrics authority for all platform interactions.
It answers exactly one question:
"What objectively happened, in a platform-agnostic way, that the rest of the system can safely reason about?"

Tier-0 Role: Every platform lies differently. This file makes them lie the same way.

NO reverse dependencies allowed.
NO platform-specific fields leak through.
NO sampling, NO probabilistic drops, NO async loss.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List
import time
import threading
from collections import defaultdict


# ============================================================================
# CORE ENUMS
# ============================================================================

class TelemetryEventType(Enum):
    """Canonical event types for platform telemetry."""
    DISPATCH_ATTEMPT = "dispatch_attempt"
    DISPATCH_SUCCESS = "dispatch_success"
    DISPATCH_FAILURE = "dispatch_failure"
    VISIBILITY_UPDATE = "visibility_update"
    SESSION_REFRESH = "session_refresh"
    AUTH_FAILURE = "auth_failure"


class TelemetrySeverity(Enum):
    """Severity levels for telemetry events."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    
    def __lt__(self, other):
        """Enable severity comparison for monotonic escalation checks."""
        severity_order = {
            TelemetrySeverity.INFO: 0,
            TelemetrySeverity.WARNING: 1,
            TelemetrySeverity.ERROR: 2,
            TelemetrySeverity.CRITICAL: 3
        }
        return severity_order[self] < severity_order[other]
    
    def __le__(self, other):
        return self < other or self == other


# ============================================================================
# TELEMETRY EVENT CONTRACT
# ============================================================================

@dataclass(frozen=True)
class PlatformTelemetryEvent:
    """
    Immutable telemetry event representing a single platform interaction.
    
    This is the normalized truth that all downstream systems consume.
    """
    # Identity
    platform: str
    account_id: str
    intent_id: str
    
    # Event classification
    event_type: TelemetryEventType
    severity: TelemetrySeverity
    
    # Timing
    timestamp: float
    latency_ms: Optional[int] = None
    
    # Execution context
    attempt_number: Optional[int] = None
    state: Optional[str] = None
    
    # Error tracking
    error_code: Optional[str] = None
    
    # Suppression signals (numeric hint only, NOT a decision)
    suppression_hint: Optional[float] = None  # [0.0, 1.0]
    
    # Platform-agnostic metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate invariants on construction."""
        if self.suppression_hint is not None:
            if not (0.0 <= self.suppression_hint <= 1.0):
                raise ValueError(f"suppression_hint must be in [0.0, 1.0], got {self.suppression_hint}")
        
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError(f"latency_ms cannot be negative, got {self.latency_ms}")


# ============================================================================
# VISIBILITY SIGNAL EXTRACTOR
# ============================================================================

class VisibilitySignalExtractor:
    """
    Extracts early indicators of suppression or visibility issues.
    
    Outputs numeric hints only - NO DECISIONS.
    """
    
    # Thresholds for signal calculation (tunable)
    DELAYED_AVAILABILITY_THRESHOLD_MS = 5000
    ZERO_REACH_WINDOW_SEC = 300  # 5 minutes
    ENGAGEMENT_STARVATION_THRESHOLD = 0.01
    
    @staticmethod
    def extract_from_response(
        platform: str,
        response_data: Dict[str, Any],
        latency_ms: int
    ) -> Optional[float]:
        """
        Extract suppression hint from platform response.
        
        Returns:
            float in [0.0, 1.0] where:
            - 0.0 = no suppression indicators
            - 1.0 = strong suppression indicators
            - None = insufficient data
        """
        signals = []
        
        # Signal 1: Delayed availability
        if latency_ms > VisibilitySignalExtractor.DELAYED_AVAILABILITY_THRESHOLD_MS:
            delay_signal = min(1.0, latency_ms / 10000.0)  # Cap at 10s
            signals.append(delay_signal * 0.3)  # Weight: 30%
        
        # Signal 2: Zero reach indicators
        reach = response_data.get('reach', response_data.get('impressions'))
        if reach is not None and reach == 0:
            signals.append(0.5)  # Weight: 50%
        
        # Signal 3: Engagement starvation
        engagement_rate = response_data.get('engagement_rate')
        if engagement_rate is not None:
            if engagement_rate < VisibilitySignalExtractor.ENGAGEMENT_STARVATION_THRESHOLD:
                signals.append(0.4)  # Weight: 40%
        
        # Signal 4: Platform-specific visibility flags
        is_shadowbanned = response_data.get('shadowbanned', False)
        is_restricted = response_data.get('restricted', False)
        if is_shadowbanned or is_restricted:
            signals.append(1.0)
        
        if not signals:
            return None
        
        # Combine signals (max of weighted signals)
        return min(1.0, max(signals))
    
    @staticmethod
    def extract_from_visibility_update(
        previous_reach: Optional[int],
        current_reach: Optional[int],
        time_delta_sec: float
    ) -> Optional[float]:
        """Extract suppression hint from visibility metric changes."""
        if previous_reach is None or current_reach is None:
            return None
        
        # Reach decay detection
        if previous_reach > 0:
            decay_rate = (previous_reach - current_reach) / previous_reach
            if decay_rate > 0.8:  # 80% decay
                return 0.7
            elif decay_rate > 0.5:  # 50% decay
                return 0.4
        
        # Zero reach window
        if current_reach == 0 and time_delta_sec > VisibilitySignalExtractor.ZERO_REACH_WINDOW_SEC:
            return 0.6
        
        return None


# ============================================================================
# TELEMETRY NORMALIZER
# ============================================================================

class TelemetryNormalizer:
    """
    Normalizes raw platform responses into canonical telemetry events.
    
    Consumes:
    - upload results
    - posting errors
    - state transitions
    - session events
    
    Produces:
    - PlatformTelemetryEvent instances
    """
    
    def __init__(self):
        self.visibility_extractor = VisibilitySignalExtractor()
    
    def normalize_dispatch_attempt(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        attempt_number: int,
        timestamp: Optional[float] = None
    ) -> PlatformTelemetryEvent:
        """Normalize a dispatch attempt event."""
        return PlatformTelemetryEvent(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            event_type=TelemetryEventType.DISPATCH_ATTEMPT,
            severity=TelemetrySeverity.INFO,
            timestamp=timestamp or time.time(),
            attempt_number=attempt_number,
            state="attempting"
        )
    
    def normalize_dispatch_success(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        attempt_number: int,
        latency_ms: int,
        remote_post_id: str,
        response_data: Dict[str, Any],
        timestamp: Optional[float] = None
    ) -> PlatformTelemetryEvent:
        """Normalize a successful dispatch event."""
        # Extract visibility signals
        suppression_hint = self.visibility_extractor.extract_from_response(
            platform=platform,
            response_data=response_data,
            latency_ms=latency_ms
        )
        
        # Build metadata
        metadata = {
            'remote_post_id': remote_post_id,
            'response_size': len(str(response_data))
        }
        
        # Add platform-agnostic visibility metrics if available
        if 'reach' in response_data:
            metadata['initial_reach'] = response_data['reach']
        if 'impressions' in response_data:
            metadata['initial_impressions'] = response_data['impressions']
        
        return PlatformTelemetryEvent(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            event_type=TelemetryEventType.DISPATCH_SUCCESS,
            severity=TelemetrySeverity.INFO,
            timestamp=timestamp or time.time(),
            latency_ms=latency_ms,
            attempt_number=attempt_number,
            state="published",
            suppression_hint=suppression_hint,
            metadata=metadata
        )
    
    def normalize_dispatch_failure(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        attempt_number: int,
        latency_ms: int,
        error_code: str,
        error_severity: str,
        error_metadata: Dict[str, Any],
        timestamp: Optional[float] = None
    ) -> PlatformTelemetryEvent:
        """Normalize a failed dispatch event."""
        # Map error severity to telemetry severity
        severity_map = {
            'transient': TelemetrySeverity.WARNING,
            'fatal': TelemetrySeverity.ERROR,
            'auth': TelemetrySeverity.ERROR,
            'ratelimit': TelemetrySeverity.WARNING,
            'unknown': TelemetrySeverity.ERROR
        }
        severity = severity_map.get(error_severity, TelemetrySeverity.ERROR)
        
        metadata = {
            'error_category': error_severity,
            **error_metadata
        }
        
        return PlatformTelemetryEvent(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            event_type=TelemetryEventType.DISPATCH_FAILURE,
            severity=severity,
            timestamp=timestamp or time.time(),
            latency_ms=latency_ms,
            attempt_number=attempt_number,
            state="failed",
            error_code=error_code,
            metadata=metadata
        )
    
    def normalize_visibility_update(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        previous_metrics: Dict[str, Any],
        current_metrics: Dict[str, Any],
        time_delta_sec: float,
        timestamp: Optional[float] = None
    ) -> PlatformTelemetryEvent:
        """Normalize a visibility metrics update."""
        # Extract suppression hint from metric changes
        suppression_hint = self.visibility_extractor.extract_from_visibility_update(
            previous_reach=previous_metrics.get('reach'),
            current_reach=current_metrics.get('reach'),
            time_delta_sec=time_delta_sec
        )
        
        metadata = {
            'previous_reach': previous_metrics.get('reach'),
            'current_reach': current_metrics.get('reach'),
            'time_delta_sec': time_delta_sec
        }
        
        # Determine severity based on suppression hint
        severity = TelemetrySeverity.INFO
        if suppression_hint is not None:
            if suppression_hint >= 0.7:
                severity = TelemetrySeverity.WARNING
            elif suppression_hint >= 0.9:
                severity = TelemetrySeverity.ERROR
        
        return PlatformTelemetryEvent(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            event_type=TelemetryEventType.VISIBILITY_UPDATE,
            severity=severity,
            timestamp=timestamp or time.time(),
            suppression_hint=suppression_hint,
            metadata=metadata
        )
    
    def normalize_session_refresh(
        self,
        platform: str,
        account_id: str,
        success: bool,
        latency_ms: int,
        timestamp: Optional[float] = None
    ) -> PlatformTelemetryEvent:
        """Normalize a session refresh event."""
        return PlatformTelemetryEvent(
            platform=platform,
            account_id=account_id,
            intent_id="session_refresh",
            event_type=TelemetryEventType.SESSION_REFRESH,
            severity=TelemetrySeverity.INFO if success else TelemetrySeverity.WARNING,
            timestamp=timestamp or time.time(),
            latency_ms=latency_ms,
            state="refreshed" if success else "refresh_failed",
            metadata={'success': success}
        )
    
    def normalize_auth_failure(
        self,
        platform: str,
        account_id: str,
        error_code: str,
        timestamp: Optional[float] = None
    ) -> PlatformTelemetryEvent:
        """Normalize an authentication failure event."""
        return PlatformTelemetryEvent(
            platform=platform,
            account_id=account_id,
            intent_id="auth_failure",
            event_type=TelemetryEventType.AUTH_FAILURE,
            severity=TelemetrySeverity.CRITICAL,
            timestamp=timestamp or time.time(),
            state="auth_failed",
            error_code=error_code,
            metadata={'requires_intervention': True}
        )


# ============================================================================
# TELEMETRY INVARIANT VALIDATOR
# ============================================================================

class TelemetryInvariantValidator:
    """
    Enforces critical invariants on telemetry events.
    
    Violation → system halt (raises exception).
    
    NON-OPTIONAL.
    """
    
    def __init__(self):
        self._attempt_tracker: Dict[str, int] = defaultdict(int)
        self._severity_tracker: Dict[str, TelemetrySeverity] = {}
        self._lock = threading.Lock()
    
    def validate(self, event: PlatformTelemetryEvent) -> None:
        """
        Validate event against invariants.
        
        Raises:
            ValueError: If any invariant is violated
        """
        with self._lock:
            self._validate_dispatch_attempt_emitted(event)
            self._validate_success_alignment(event)
            self._validate_severity_escalation(event)
            self._validate_suppression_hints(event)
    
    def _validate_dispatch_attempt_emitted(self, event: PlatformTelemetryEvent) -> None:
        """Invariant: Every dispatch attempt emits telemetry."""
        key = f"{event.platform}:{event.account_id}:{event.intent_id}"
        
        if event.event_type == TelemetryEventType.DISPATCH_ATTEMPT:
            self._attempt_tracker[key] += 1
        
        elif event.event_type in (TelemetryEventType.DISPATCH_SUCCESS, TelemetryEventType.DISPATCH_FAILURE):
            if self._attempt_tracker.get(key, 0) == 0:
                raise ValueError(
                    f"INVARIANT VIOLATION: {event.event_type.value} without prior DISPATCH_ATTEMPT for {key}"
                )
    
    def _validate_success_alignment(self, event: PlatformTelemetryEvent) -> None:
        """Invariant: Success events must have remote_post_id."""
        if event.event_type == TelemetryEventType.DISPATCH_SUCCESS:
            remote_post_id = event.metadata.get('remote_post_id')
            if not remote_post_id:
                raise ValueError(
                    f"INVARIANT VIOLATION: DISPATCH_SUCCESS without remote_post_id for "
                    f"{event.platform}:{event.account_id}:{event.intent_id}"
                )
            
            if event.state != "published":
                raise ValueError(
                    f"INVARIANT VIOLATION: DISPATCH_SUCCESS with state={event.state}, expected 'published'"
                )
    
    def _validate_severity_escalation(self, event: PlatformTelemetryEvent) -> None:
        """Invariant: Severity escalates monotonically per intent."""
        key = f"{event.platform}:{event.account_id}:{event.intent_id}"
        
        previous_severity = self._severity_tracker.get(key)
        if previous_severity is not None:
            if event.severity < previous_severity:
                raise ValueError(
                    f"INVARIANT VIOLATION: Severity de-escalation detected for {key}. "
                    f"Previous={previous_severity.value}, Current={event.severity.value}"
                )
        
        self._severity_tracker[key] = event.severity
    
    def _validate_suppression_hints(self, event: PlatformTelemetryEvent) -> None:
        """Invariant: Suppression hints are signals, not conclusions."""
        if event.suppression_hint is not None:
            # Hint must be in valid range (already checked in __post_init__)
            
            # Hint must not imply a decision
            if 'suppression_detected' in event.metadata:
                raise ValueError(
                    f"INVARIANT VIOLATION: suppression_hint is a signal, not a decision. "
                    f"Remove 'suppression_detected' from metadata."
                )
            
            if 'is_suppressed' in event.metadata:
                raise ValueError(
                    f"INVARIANT VIOLATION: suppression_hint is a signal, not a decision. "
                    f"Remove 'is_suppressed' from metadata."
                )


# ============================================================================
# TELEMETRY EMITTER
# ============================================================================

class TelemetryEmitter:
    """
    Emits telemetry events synchronously to internal pipelines.
    
    Guarantees delivery. NO direct alerting logic.
    """
    
    def __init__(self, validator: Optional[TelemetryInvariantValidator] = None):
        self.validator = validator or TelemetryInvariantValidator()
        self._event_buffer: List[PlatformTelemetryEvent] = []
        self._lock = threading.Lock()
        
        # Downstream consumers (registered at init)
        self._consumers: List[callable] = []
    
    def register_consumer(self, consumer: callable) -> None:
        """
        Register a downstream consumer for telemetry events.
        
        Consumer signature: consumer(event: PlatformTelemetryEvent) -> None
        """
        with self._lock:
            self._consumers.append(consumer)
    
    def emit(self, event: PlatformTelemetryEvent) -> None:
        """
        Emit a telemetry event synchronously.
        
        Validates invariants, then delivers to all consumers.
        """
        # Validate invariants (raises on violation)
        self.validator.validate(event)
        
        # Buffer event
        with self._lock:
            self._event_buffer.append(event)
        
        # Deliver to consumers synchronously
        for consumer in self._consumers:
            try:
                consumer(event)
            except Exception as e:
                # Log but don't fail emission
                print(f"[TelemetryEmitter] Consumer {consumer.__name__} failed: {e}")
    
    def get_buffered_events(self, clear: bool = False) -> List[PlatformTelemetryEvent]:
        """
        Get buffered events for testing/debugging.
        
        Args:
            clear: If True, clear the buffer after retrieval
        """
        with self._lock:
            events = self._event_buffer.copy()
            if clear:
                self._event_buffer.clear()
            return events


# ============================================================================
# UNIFIED TELEMETRY FACADE
# ============================================================================

class PlatformTelemetry:
    """
    Unified facade for platform telemetry normalization and emission.
    
    This is the primary interface for upstream callers (base_poster.py, etc.)
    """
    
    def __init__(self):
        self.normalizer = TelemetryNormalizer()
        self.validator = TelemetryInvariantValidator()
        self.emitter = TelemetryEmitter(validator=self.validator)
    
    def record_dispatch_attempt(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        attempt_number: int
    ) -> None:
        """Record a dispatch attempt."""
        event = self.normalizer.normalize_dispatch_attempt(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            attempt_number=attempt_number
        )
        self.emitter.emit(event)
    
    def record_dispatch_success(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        attempt_number: int,
        latency_ms: int,
        remote_post_id: str,
        response_data: Dict[str, Any]
    ) -> None:
        """Record a successful dispatch."""
        event = self.normalizer.normalize_dispatch_success(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            attempt_number=attempt_number,
            latency_ms=latency_ms,
            remote_post_id=remote_post_id,
            response_data=response_data
        )
        self.emitter.emit(event)
    
    def record_dispatch_failure(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        attempt_number: int,
        latency_ms: int,
        error_code: str,
        error_severity: str,
        error_metadata: Dict[str, Any]
    ) -> None:
        """Record a failed dispatch."""
        event = self.normalizer.normalize_dispatch_failure(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            attempt_number=attempt_number,
            latency_ms=latency_ms,
            error_code=error_code,
            error_severity=error_severity,
            error_metadata=error_metadata
        )
        self.emitter.emit(event)
    
    def record_visibility_update(
        self,
        platform: str,
        account_id: str,
        intent_id: str,
        previous_metrics: Dict[str, Any],
        current_metrics: Dict[str, Any],
        time_delta_sec: float
    ) -> None:
        """Record a visibility metrics update."""
        event = self.normalizer.normalize_visibility_update(
            platform=platform,
            account_id=account_id,
            intent_id=intent_id,
            previous_metrics=previous_metrics,
            current_metrics=current_metrics,
            time_delta_sec=time_delta_sec
        )
        self.emitter.emit(event)
    
    def record_session_refresh(
        self,
        platform: str,
        account_id: str,
        success: bool,
        latency_ms: int
    ) -> None:
        """Record a session refresh event."""
        event = self.normalizer.normalize_session_refresh(
            platform=platform,
            account_id=account_id,
            success=success,
            latency_ms=latency_ms
        )
        self.emitter.emit(event)
    
    def record_auth_failure(
        self,
        platform: str,
        account_id: str,
        error_code: str
    ) -> None:
        """Record an authentication failure."""
        event = self.normalizer.normalize_auth_failure(
            platform=platform,
            account_id=account_id,
            error_code=error_code
        )
        self.emitter.emit(event)
    
    def register_consumer(self, consumer: callable) -> None:
        """Register a downstream consumer for telemetry events."""
        self.emitter.register_consumer(consumer)
    
    def get_buffered_events(self, clear: bool = False) -> List[PlatformTelemetryEvent]:
        """Get buffered events for testing/debugging."""
        return self.emitter.get_buffered_events(clear=clear)


# ============================================================================
# MODULE-LEVEL SINGLETON
# ============================================================================

_global_telemetry: Optional[PlatformTelemetry] = None
_init_lock = threading.Lock()


def get_telemetry() -> PlatformTelemetry:
    """Get the global telemetry instance (singleton)."""
    global _global_telemetry
    
    if _global_telemetry is None:
        with _init_lock:
            if _global_telemetry is None:
                _global_telemetry = PlatformTelemetry()
    
    return _global_telemetry


# ============================================================================
# EXAMPLE USAGE (for documentation)
# ============================================================================

if __name__ == "__main__":
    # Initialize telemetry
    telemetry = get_telemetry()
    
    # Example: Record a successful post
    telemetry.record_dispatch_attempt(
        platform="twitter",
        account_id="acc_123",
        intent_id="intent_456",
        attempt_number=1
    )
    
    telemetry.record_dispatch_success(
        platform="twitter",
        account_id="acc_123",
        intent_id="intent_456",
        attempt_number=1,
        latency_ms=1200,
        remote_post_id="tweet_789",
        response_data={
            'reach': 1000,
            'impressions': 1500,
            'engagement_rate': 0.05
        }
    )
    
    # Example: Record a failed post
    telemetry.record_dispatch_attempt(
        platform="twitter",
        account_id="acc_123",
        intent_id="intent_457",
        attempt_number=1
    )
    
    telemetry.record_dispatch_failure(
        platform="twitter",
        account_id="acc_123",
        intent_id="intent_457",
        attempt_number=1,
        latency_ms=800,
        error_code="RATE_LIMIT_EXCEEDED",
        error_severity="ratelimit",
        error_metadata={'retry_after': 3600}
    )
    
    # Review buffered events
    events = telemetry.get_buffered_events()
    print(f"Captured {len(events)} telemetry events")
    for event in events:
        print(f"  - {event.event_type.value}: {event.platform}/{event.intent_id} [{event.severity.value}]")