"""
safety_events.py - Normalized Catastrophe & Safety Event Authority

Location: /infra/safety/safety_events.py

Purpose:
    Define the canonical language of failure across the entire system.
    
    Answers: "When something goes wrong, what exactly happened — 
              in terms everyone agrees on?"

    This ensures:
        - Invariants
        - Watchdogs
        - Recovery logic
        - Kill switches
        - Audits
        - Alerts
    
    ALL speak the same vocabulary.

What this file is NOT:
    ❌ Not a logger
    ❌ Not an alert system
    ❌ Not retry logic
    ❌ Not failure handling
    ❌ Not business logic

This file defines events, it does not act on them.

Authority Ordering:
    invariant_engine
         ↓
    safety_events
         ↓
    watchdog / kill_switch / recovery / alerts

Design Principle:
    If two teams describe the same failure differently, 
    you don't know what happened.

Mental Model:
    - Invariants detect violation
    - Safety events name the catastrophe
    - Watchdogs respond
    - Kill switches act
    - Recovery contains
    - Audit remembers forever
    
    Without this file, failure becomes noise.
"""

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Type


# ============================================================================
# SAFETY EVENT CATEGORY - Orthogonal Classification
# ============================================================================

class SafetyEventCategory(Enum):
    """
    Orthogonal event categories.
    
    Categories are not cosmetic - they determine routing and response.
    """
    INVARIANT = "invariant"         # Invariant violations
    DATA = "data"                   # Data corruption
    LOCKING = "locking"             # Lock failures
    REPLAY = "replay"               # Replay divergence
    ACCOUNT = "account"             # Account enforcement
    PLATFORM = "platform"           # Platform rejections
    INFRA = "infra"                 # Infrastructure failures
    SYSTEM = "system"               # System integrity


# ============================================================================
# SAFETY EVENT SEVERITY - Impact Level
# ============================================================================

class SafetyEventSeverity(Enum):
    """
    Safety event severity levels.
    
    Rules:
        - CRITICAL → immediate kill-switch eligibility
        - HIGH → automatic containment
        - MEDIUM → investigation required
    
    No "low severity" for safety events.
    If it's low, it's not a safety event.
    """
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


# ============================================================================
# BASE SAFETY EVENT - Canonical Schema
# ============================================================================

@dataclass(frozen=True)
class SafetyEvent:
    """
    Base schema for all safety events.
    
    Rules:
        - Schema is immutable
        - Context must be JSON-serializable
        - Timestamps must be monotonic
        - Version required for evolution
    
    This is the universal contract.
    """
    # Event identity
    event_type: str
    category: SafetyEventCategory
    severity: SafetyEventSeverity
    
    # Timing
    occurred_at: int                # Logical timestamp (monotonic)
    detected_by: str                # Component that detected the event
    
    # Execution context
    execution_id: str
    run_id: str
    
    # Description
    description: str
    context: dict                   # Additional data (JSON-serializable)
    
    # Schema versioning
    version: str
    
    # Unique event ID (computed)
    event_id: str = field(default="")
    
    def __post_init__(self):
        """Generate event ID if not provided."""
        if not self.event_id:
            # Use object.__setattr__ since dataclass is frozen
            object.__setattr__(self, 'event_id', self._generate_event_id())
    
    def _generate_event_id(self) -> str:
        """Generate unique, deterministic event ID."""
        components = [
            self.event_type,
            str(self.occurred_at),
            self.execution_id,
            self.detected_by
        ]
        raw = ":".join(components)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        data['category'] = self.category.value
        data['severity'] = self.severity.value
        return data
    
    def to_json_canonical(self) -> str:
        """
        Canonical JSON serialization for deterministic replay.
        
        Must:
            - Serialize deterministically
            - Round-trip exactly
            - Re-emit identically in replay
            - Never depend on wall-clock time
        """
        data = self.to_dict()
        return json.dumps(data, sort_keys=True, separators=(',', ':'))
    
    def compute_hash(self) -> str:
        """Compute cryptographic hash of event."""
        return hashlib.sha256(self.to_json_canonical().encode()).hexdigest()
    
    def is_critical(self) -> bool:
        """Check if event is critical severity."""
        return self.severity == SafetyEventSeverity.CRITICAL
    
    def requires_emergency_stop(self) -> bool:
        """Check if event should trigger emergency stop."""
        # Override in subclasses for specific logic
        return self.severity == SafetyEventSeverity.CRITICAL


# ============================================================================
# SPECIFIC EVENT TYPES - Canonical Failures
# ============================================================================

@dataclass(frozen=True)
class InvariantViolationEvent(SafetyEvent):
    """
    Invariant violation detected.
    
    Emitted ONLY by invariant_engine.py.
    """
    invariant_name: str
    invariant_scope: str
    expected_value: Optional[Any] = None
    actual_value: Optional[Any] = None
    violation_details: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate invariant event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.INVARIANT
        assert self.invariant_name, "invariant_name required"
        assert self.invariant_scope, "invariant_scope required"


@dataclass(frozen=True)
class DataCorruptionEvent(SafetyEvent):
    """
    Data corruption detected.
    
    Triggers forensic analysis + freeze.
    """
    storage_backend: str
    object_id: str
    corruption_type: str
    expected_checksum: Optional[str] = None
    actual_checksum: Optional[str] = None
    
    def __post_init__(self):
        """Validate corruption event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.DATA
        assert self.storage_backend, "storage_backend required"
        assert self.object_id, "object_id required"
        assert self.corruption_type, "corruption_type required"


@dataclass(frozen=True)
class LockLossEvent(SafetyEvent):
    """
    Lock ownership lost unexpectedly.
    
    Signals split-brain risk.
    """
    lock_id: str
    lock_scope: str
    owner_id: str
    lost_at: int
    held_duration_ms: Optional[int] = None
    
    def __post_init__(self):
        """Validate lock loss event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.LOCKING
        assert self.lock_id, "lock_id required"
        assert self.lock_scope, "lock_scope required"
        assert self.owner_id, "owner_id required"
    
    def requires_emergency_stop(self) -> bool:
        """Lock loss for global scope requires emergency stop."""
        return self.lock_scope == "global" or self.is_critical()


@dataclass(frozen=True)
class ReplayDivergenceEvent(SafetyEvent):
    """
    Replay execution diverged from original.
    
    Replay mismatch = learning invalid.
    """
    replay_id: str
    snapshot_id: str
    checksum_expected: str
    checksum_actual: str
    divergence_type: str
    divergence_details: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate replay divergence event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.REPLAY
        assert self.snapshot_id, "snapshot_id required"
        assert self.checksum_expected, "checksum_expected required"
        assert self.checksum_actual, "checksum_actual required"
    
    def requires_emergency_stop(self) -> bool:
        """Replay divergence always requires investigation."""
        return self.severity in (SafetyEventSeverity.CRITICAL, SafetyEventSeverity.HIGH)


@dataclass(frozen=True)
class AccountEnforcementEvent(SafetyEvent):
    """
    Account enforcement action detected.
    
    Trust degradation or restriction detected.
    """
    account_id: str
    platform: str
    enforcement_action: str
    enforcement_reason: Optional[str] = None
    enforcement_details: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate account enforcement event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.ACCOUNT
        assert self.account_id, "account_id required"
        assert self.platform, "platform required"
        assert self.enforcement_action, "enforcement_action required"


@dataclass(frozen=True)
class PlatformRejectionEvent(SafetyEvent):
    """
    Platform rejected an action.
    
    Signals risk of shadow suppression or bans.
    """
    platform: str
    action_type: str
    reason_code: str
    payload_id: str
    http_status: Optional[int] = None
    rejection_details: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate platform rejection event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.PLATFORM
        assert self.platform, "platform required"
        assert self.reason_code, "reason_code required"
        assert self.payload_id, "payload_id required"


@dataclass(frozen=True)
class SystemIntegrityEvent(SafetyEvent):
    """
    System integrity issue detected.
    
    Catch-all for existential threats.
    """
    subsystem: str
    integrity_signal: str
    health_status: str
    integrity_details: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate system integrity event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.SYSTEM
        assert self.subsystem, "subsystem required"
        assert self.integrity_signal, "integrity_signal required"
    
    def requires_emergency_stop(self) -> bool:
        """Critical system integrity issues require emergency stop."""
        return self.is_critical()


@dataclass(frozen=True)
class InfrastructureFailureEvent(SafetyEvent):
    """
    Infrastructure failure detected.
    """
    component: str
    failure_type: str
    failure_mode: str
    recovery_possible: bool
    failure_details: dict = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate infrastructure failure event."""
        super().__post_init__()
        assert self.category == SafetyEventCategory.INFRA
        assert self.component, "component required"
        assert self.failure_type, "failure_type required"


# ============================================================================
# SCHEMA VALIDATION - Enforcement
# ============================================================================

class SafetyEventValidationError(Exception):
    """Raised when safety event validation fails."""
    pass


class SafetyEventSchemaValidator:
    """
    Validates safety event schemas.
    
    Validation MUST enforce:
        - Required fields present
        - Enums valid
        - Timestamps sane
        - Version compatibility
        - Context size limits
    
    Invalid event → hard failure.
    """
    
    # Schema version
    CURRENT_VERSION = "1.0.0"
    
    # Context size limit (100KB)
    MAX_CONTEXT_SIZE = 100 * 1024
    
    # Known event types
    VALID_EVENT_TYPES = {
        "invariant_violation",
        "data_corruption",
        "lock_loss",
        "replay_divergence",
        "account_enforcement",
        "platform_rejection",
        "system_integrity",
        "infrastructure_failure"
    }
    
    @classmethod
    def validate(cls, event: SafetyEvent) -> None:
        """
        Validate safety event schema.
        
        Raises:
            SafetyEventValidationError: If validation fails
        """
        # Required fields
        cls._validate_required_fields(event)
        
        # Enums
        cls._validate_enums(event)
        
        # Timestamps
        cls._validate_timestamps(event)
        
        # Version
        cls._validate_version(event)
        
        # Context
        cls._validate_context(event)
        
        # Event type
        cls._validate_event_type(event)
    
    @staticmethod
    def _validate_required_fields(event: SafetyEvent):
        """Validate required fields are present."""
        required = [
            'event_type', 'category', 'severity',
            'occurred_at', 'detected_by',
            'execution_id', 'run_id',
            'description', 'version'
        ]
        
        for field_name in required:
            value = getattr(event, field_name, None)
            if value is None or value == "":
                raise SafetyEventValidationError(
                    f"Required field missing: {field_name}"
                )
    
    @staticmethod
    def _validate_enums(event: SafetyEvent):
        """Validate enum values."""
        if not isinstance(event.category, SafetyEventCategory):
            raise SafetyEventValidationError(
                f"Invalid category: {event.category}"
            )
        
        if not isinstance(event.severity, SafetyEventSeverity):
            raise SafetyEventValidationError(
                f"Invalid severity: {event.severity}"
            )
    
    @staticmethod
    def _validate_timestamps(event: SafetyEvent):
        """Validate timestamps are sane."""
        # Must be positive
        if event.occurred_at <= 0:
            raise SafetyEventValidationError(
                f"Invalid timestamp: {event.occurred_at}"
            )
        
        # Must not be in far future (1 year from now in ms)
        now_ms = int(time.time() * 1000)
        one_year_ms = 365 * 24 * 60 * 60 * 1000
        if event.occurred_at > now_ms + one_year_ms:
            raise SafetyEventValidationError(
                f"Timestamp too far in future: {event.occurred_at}"
            )
    
    @classmethod
    def _validate_version(cls, event: SafetyEvent):
        """Validate version compatibility."""
        if not event.version:
            raise SafetyEventValidationError("Version required")
        
        # In production, this would do proper semver comparison
        # For now, just check it exists
        parts = event.version.split('.')
        if len(parts) != 3:
            raise SafetyEventValidationError(
                f"Invalid version format: {event.version}"
            )
    
    @classmethod
    def _validate_context(cls, event: SafetyEvent):
        """Validate context is JSON-serializable and within size limits."""
        if not isinstance(event.context, dict):
            raise SafetyEventValidationError(
                "Context must be a dictionary"
            )
        
        # Check JSON-serializable
        try:
            context_json = json.dumps(event.context)
        except (TypeError, ValueError) as e:
            raise SafetyEventValidationError(
                f"Context not JSON-serializable: {e}"
            )
        
        # Check size
        if len(context_json) > cls.MAX_CONTEXT_SIZE:
            raise SafetyEventValidationError(
                f"Context too large: {len(context_json)} bytes > {cls.MAX_CONTEXT_SIZE}"
            )
    
    @classmethod
    def _validate_event_type(cls, event: SafetyEvent):
        """Validate event type is recognized."""
        if event.event_type not in cls.VALID_EVENT_TYPES:
            raise SafetyEventValidationError(
                f"Unknown event type: {event.event_type}"
            )


# ============================================================================
# SAFETY EVENT FACTORY - Canonical Creation
# ============================================================================

class SafetyEventFactory:
    """
    Factory for creating safety events.
    
    Rules:
        - No raw instantiation elsewhere
        - Factory pins versions
        - Factory enforces normalization
    
    This prevents "creative" failures.
    """
    
    VERSION = "1.0.0"
    
    @classmethod
    def create_invariant_violation(
        cls,
        invariant_name: str,
        invariant_scope: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.CRITICAL,
        expected_value: Optional[Any] = None,
        actual_value: Optional[Any] = None,
        context: Optional[dict] = None
    ) -> InvariantViolationEvent:
        """Create invariant violation event."""
        event = InvariantViolationEvent(
            event_type="invariant_violation",
            category=SafetyEventCategory.INVARIANT,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            invariant_name=invariant_name,
            invariant_scope=invariant_scope,
            expected_value=expected_value,
            actual_value=actual_value,
            violation_details={}
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_data_corruption(
        cls,
        storage_backend: str,
        object_id: str,
        corruption_type: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.CRITICAL,
        expected_checksum: Optional[str] = None,
        actual_checksum: Optional[str] = None,
        context: Optional[dict] = None
    ) -> DataCorruptionEvent:
        """Create data corruption event."""
        event = DataCorruptionEvent(
            event_type="data_corruption",
            category=SafetyEventCategory.DATA,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            storage_backend=storage_backend,
            object_id=object_id,
            corruption_type=corruption_type,
            expected_checksum=expected_checksum,
            actual_checksum=actual_checksum
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_lock_loss(
        cls,
        lock_id: str,
        lock_scope: str,
        owner_id: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.HIGH,
        held_duration_ms: Optional[int] = None,
        context: Optional[dict] = None
    ) -> LockLossEvent:
        """Create lock loss event."""
        now_ms = int(time.time() * 1000)
        
        event = LockLossEvent(
            event_type="lock_loss",
            category=SafetyEventCategory.LOCKING,
            severity=severity,
            occurred_at=now_ms,
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            lock_id=lock_id,
            lock_scope=lock_scope,
            owner_id=owner_id,
            lost_at=now_ms,
            held_duration_ms=held_duration_ms
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_replay_divergence(
        cls,
        replay_id: str,
        snapshot_id: str,
        checksum_expected: str,
        checksum_actual: str,
        divergence_type: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.HIGH,
        context: Optional[dict] = None
    ) -> ReplayDivergenceEvent:
        """Create replay divergence event."""
        event = ReplayDivergenceEvent(
            event_type="replay_divergence",
            category=SafetyEventCategory.REPLAY,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            replay_id=replay_id,
            snapshot_id=snapshot_id,
            checksum_expected=checksum_expected,
            checksum_actual=checksum_actual,
            divergence_type=divergence_type,
            divergence_details={}
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_account_enforcement(
        cls,
        account_id: str,
        platform: str,
        enforcement_action: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.HIGH,
        enforcement_reason: Optional[str] = None,
        context: Optional[dict] = None
    ) -> AccountEnforcementEvent:
        """Create account enforcement event."""
        event = AccountEnforcementEvent(
            event_type="account_enforcement",
            category=SafetyEventCategory.ACCOUNT,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            account_id=account_id,
            platform=platform,
            enforcement_action=enforcement_action,
            enforcement_reason=enforcement_reason,
            enforcement_details={}
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_platform_rejection(
        cls,
        platform: str,
        action_type: str,
        reason_code: str,
        payload_id: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.MEDIUM,
        http_status: Optional[int] = None,
        context: Optional[dict] = None
    ) -> PlatformRejectionEvent:
        """Create platform rejection event."""
        event = PlatformRejectionEvent(
            event_type="platform_rejection",
            category=SafetyEventCategory.PLATFORM,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            platform=platform,
            action_type=action_type,
            reason_code=reason_code,
            payload_id=payload_id,
            http_status=http_status,
            rejection_details={}
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_system_integrity(
        cls,
        subsystem: str,
        integrity_signal: str,
        health_status: str,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.CRITICAL,
        context: Optional[dict] = None
    ) -> SystemIntegrityEvent:
        """Create system integrity event."""
        event = SystemIntegrityEvent(
            event_type="system_integrity",
            category=SafetyEventCategory.SYSTEM,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            subsystem=subsystem,
            integrity_signal=integrity_signal,
            health_status=health_status,
            integrity_details={}
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event
    
    @classmethod
    def create_infrastructure_failure(
        cls,
        component: str,
        failure_type: str,
        failure_mode: str,
        recovery_possible: bool,
        detected_by: str,
        execution_id: str,
        run_id: str,
        description: str,
        severity: SafetyEventSeverity = SafetyEventSeverity.HIGH,
        context: Optional[dict] = None
    ) -> InfrastructureFailureEvent:
        """Create infrastructure failure event."""
        event = InfrastructureFailureEvent(
            event_type="infrastructure_failure",
            category=SafetyEventCategory.INFRA,
            severity=severity,
            occurred_at=int(time.time() * 1000),
            detected_by=detected_by,
            execution_id=execution_id,
            run_id=run_id,
            description=description,
            context=context or {},
            version=cls.VERSION,
            component=component,
            failure_type=failure_type,
            failure_mode=failure_mode,
            recovery_possible=recovery_possible,
            failure_details={}
        )
        
        SafetyEventSchemaValidator.validate(event)
        return event


# ============================================================================
# EVENT TYPE REGISTRY - Runtime Introspection
# ============================================================================

class SafetyEventTypeRegistry:
    """Registry of all known safety event types."""
    
    _registry: Dict[str, Type[SafetyEvent]] = {
        "invariant_violation": InvariantViolationEvent,
        "data_corruption": DataCorruptionEvent,
        "lock_loss": LockLossEvent,
        "replay_divergence": ReplayDivergenceEvent,
        "account_enforcement": AccountEnforcementEvent,
        "platform_rejection": PlatformRejectionEvent,
        "system_integrity": SystemIntegrityEvent,
        "infrastructure_failure": InfrastructureFailureEvent
    }
    
    @classmethod
    def get_event_class(cls, event_type: str) -> Optional[Type[SafetyEvent]]:
        """Get event class by type string."""
        return cls._registry.get(event_type)
    
    @classmethod
    def is_registered(cls, event_type: str) -> bool:
        """Check if event type is registered."""
        return event_type in cls._registry
    
    @classmethod
    def list_event_types(cls) -> List[str]:
        """List all registered event types."""
        return list(cls._registry.keys())


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

if __name__ == "__main__":
    print("Safety Events System Demo")
    print("=" * 60)
    
    # Create invariant violation
    print("\n1. Invariant Violation Event")
    inv_event = SafetyEventFactory.create_invariant_violation(
        invariant_name="state_consistency",
        invariant_scope="workflow_state",
        detected_by="invariant_engine",
        execution_id="exec_123",
        run_id="run_456",
        description="Workflow state became inconsistent with database",
        expected_value="RUNNING",
        actual_value="UNKNOWN",
        context={"workflow_id": "wf_789", "state_before": "PENDING"}
    )
    print(f"   Event ID: {inv_event.event_id}")
    print(f"   Severity: {inv_event.severity.value}")
    print(f"   Requires Stop: {inv_event.requires_emergency_stop()}")
    
    # Create lock loss
    print("\n2. Lock Loss Event")
    lock_event = SafetyEventFactory.create_lock_loss(
        lock_id="global_execution_lock",
        lock_scope="global",
        owner_id="worker_001",
        detected_by="lock_monitor",
        execution_id="exec_123",
        run_id="run_456",
        description="Global execution lock lost unexpectedly",
        held_duration_ms=5000,
        context={"last_heartbeat": "2024-01-01T00:00:00Z"}
    )
    print(f"   Event ID: {lock_event.event_id}")
    print(f"   Scope: {lock_event.lock_scope}")
    print(f"   Requires Stop: {lock_event.requires_emergency_stop()}")
    
    # Create replay divergence
    print("\n3. Replay Divergence Event")
    replay_event = SafetyEventFactory.create_replay_divergence(
        replay_id="replay_001",
        snapshot_id="snap_456",
        checksum_expected="abc123",
        checksum_actual="def456",
        divergence_type="checksum_mismatch",
        detected_by="replay_engine",
        execution_id="exec_123",
        run_id="run_456",
        description="Replay output did not match original execution",
        context={"divergence_point": "step_5"}
    )
    print(f"   Event ID: {replay_event.event_id}")
    print(f"   Expected: {replay_event.checksum_expected}")
    print(f"   Actual: {replay_event.checksum_actual}")
    
    # Create platform rejection
    print("\n4. Platform Rejection Event")
    platform_event = SafetyEventFactory.create_platform_rejection(
        platform="twitter",
        action_type="post_tweet",
        reason_code="rate_limit",
        payload_id="tweet_123",
        detected_by="post_dispatcher",
        execution_id="exec_123",
        run_id="run_456",
        description="Tweet rejected by Twitter API",
        http_status=429,
        context={"retry_after": 900}
    )
    print(f"   Event ID: {platform_event.event_id}")
    print(f"   Platform: {platform_event.platform}")
    print(f"   Reason: {platform_event.reason_code}")
    
    # Demonstrate serialization
    print("\n5. Event Serialization")
    canonical = inv_event.to_json_canonical()
    print(f"   Canonical JSON (first 100 chars):")
    print(f"   {canonical[:100]}...")
    
    event_hash = inv_event.compute_hash()
    print(f"   Event Hash: {event_hash}")
    
    # Show all event types
    print("\n6. Registered Event Types")
    for event_type in SafetyEventTypeRegistry.list_event_types():
        print(f"   - {event_type}")
    
    print("\n" + "=" * 60)
    print("Safety events are now the canonical language of failure.")