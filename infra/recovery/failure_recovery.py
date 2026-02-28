"""
failure_recovery.py - Deterministic Forward-Recovery Authority

Purpose: Triage doctor that decides "Is it safe to attempt recovery, or must the system stop?"
Does NOT perform recovery itself - only makes go/no-go decisions.

Authority Chain:
    emergency_stop → invariant_engine → failure_recovery → recovery/*

If failure_recovery says NO, recovery does not run.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, List, Set, Tuple
import time
from collections import defaultdict


class FailureCategory(Enum):
    """Failure taxonomy for recovery routing"""
    TRANSIENT = "transient"      # Network blips, temporary unavailability
    INFRA = "infra"              # Platform/infrastructure issues
    DATA = "data"                # Corrupted or invalid data
    LOGIC = "logic"              # Business logic violations
    ACCOUNT = "account"          # Account-level restrictions/bans
    PLATFORM = "platform"        # Platform enforcement actions
    SYSTEM = "system"            # Internal system failures


class FailureSeverity(Enum):
    """Severity classification for recovery gating"""
    LOW = "low"                  # Local retry permitted
    MEDIUM = "medium"            # Limited recovery allowed
    HIGH = "high"                # Gated recovery only
    CATASTROPHIC = "catastrophic"  # Emergency stop required


class RecoveryClass(Enum):
    """Recovery strategy classes"""
    RETRY = "retry"              # Simple retry with backoff
    REQUEUE = "requeue"          # Move to back of queue
    DEGRADE = "degrade"          # Continue with reduced functionality
    ISOLATE = "isolate"          # Quarantine affected component
    HALT = "halt"                # Stop all operations
    MANUAL = "manual"            # Require human intervention
    NONE = "none"                # No recovery possible


@dataclass(frozen=True)
class FailureSignal:
    """Normalized failure representation (INPUT CONTRACT)"""
    event_id: str
    category: FailureCategory
    severity: FailureSeverity
    
    source: str                  # Component that detected failure
    description: str
    context: Dict                # Contextual metadata
    
    detected_at: int             # Unix timestamp
    
    # Optional enrichment
    blast_radius: Optional[str] = None  # account/workflow/system
    correlation_id: Optional[str] = None
    retry_count: int = 0


@dataclass(frozen=True)
class RecoveryDecision:
    """Recovery authorization decision (OUTPUT FACT)"""
    allow_recovery: bool
    recovery_class: Optional[RecoveryClass]
    reason: str
    
    decision_at: int             # Unix timestamp
    
    # Decision metadata
    severity: FailureSeverity
    category: FailureCategory
    constraints: Dict = None     # Recovery constraints (max retries, timeout, etc)
    
    # Audit trail
    signal_id: str = ""
    escalation_required: bool = False
    
    def __post_init__(self):
        if self.constraints is None:
            object.__setattr__(self, 'constraints', {})


@dataclass
class SafetyEvent:
    """Generic safety event (compatible with safety system)"""
    event_id: str
    event_type: str
    severity: str
    source: str
    description: str
    context: Dict
    timestamp: int


class FailureClassifier:
    """Normalizes raw failures into structured FailureSignals"""
    
    # Severity upgrade rules based on correlation
    CORRELATION_UPGRADE_THRESHOLD = 3  # Similar failures in time window
    CORRELATION_WINDOW_SECONDS = 300   # 5 minutes
    
    def __init__(self):
        self._recent_signals: List[FailureSignal] = []
        self._correlation_groups: Dict[str, List[str]] = defaultdict(list)
    
    def classify(self, event: SafetyEvent) -> FailureSignal:
        """
        Classify a raw safety event into a structured failure signal.
        
        Args:
            event: Raw safety event from monitoring/watchdog
            
        Returns:
            Normalized FailureSignal with severity and category
        """
        # Determine base category from event type
        category = self._categorize_event(event)
        
        # Determine base severity
        severity = self._assess_severity(event, category)
        
        # Extract blast radius
        blast_radius = self._determine_blast_radius(event)
        
        # Check for correlation and potentially upgrade severity
        correlation_id = self._check_correlation(event, category)
        if correlation_id:
            severity = self._upgrade_severity_if_correlated(
                correlation_id, 
                severity
            )
        
        signal = FailureSignal(
            event_id=event.event_id,
            category=category,
            severity=severity,
            source=event.source,
            description=event.description,
            context=event.context,
            detected_at=event.timestamp,
            blast_radius=blast_radius,
            correlation_id=correlation_id,
            retry_count=event.context.get('retry_count', 0)
        )
        
        # Track for correlation detection
        self._track_signal(signal)
        
        return signal
    
    def _categorize_event(self, event: SafetyEvent) -> FailureCategory:
        """Determine failure category from event characteristics"""
        event_type = event.event_type.lower()
        context = event.context
        
        # Platform enforcement indicators
        if any(kw in event_type for kw in ['ban', 'suspend', 'block', 'restrict']):
            return FailureCategory.PLATFORM
        
        if 'account' in event_type:
            return FailureCategory.ACCOUNT
        
        # Network/transient indicators
        if any(kw in event_type for kw in ['timeout', 'connection', 'network', 'unavailable']):
            return FailureCategory.TRANSIENT
        
        # Data corruption indicators
        if any(kw in event_type for kw in ['corrupt', 'invalid', 'parse', 'schema']):
            return FailureCategory.DATA
        
        # Infrastructure indicators
        if any(kw in event_type for kw in ['infra', 'service', 'resource', 'quota']):
            return FailureCategory.INFRA
        
        # Logic violations
        if any(kw in event_type for kw in ['invariant', 'constraint', 'violation', 'assertion']):
            return FailureCategory.LOGIC
        
        # Check context for more clues
        if context.get('platform_response'):
            return FailureCategory.PLATFORM
        
        if context.get('state_corruption'):
            return FailureCategory.DATA
        
        # Default to system for unknown
        return FailureCategory.SYSTEM
    
    def _assess_severity(
        self, 
        event: SafetyEvent, 
        category: FailureCategory
    ) -> FailureSeverity:
        """Assess base severity before correlation"""
        event_severity = event.severity.lower()
        
        # Direct severity mapping
        severity_map = {
            'critical': FailureSeverity.CATASTROPHIC,
            'catastrophic': FailureSeverity.CATASTROPHIC,
            'high': FailureSeverity.HIGH,
            'medium': FailureSeverity.MEDIUM,
            'low': FailureSeverity.LOW
        }
        
        if event_severity in severity_map:
            base_severity = severity_map[event_severity]
        else:
            base_severity = FailureSeverity.MEDIUM
        
        # Category-based severity rules
        if category == FailureCategory.DATA:
            # Data corruption is always at least HIGH
            if base_severity == FailureSeverity.LOW:
                base_severity = FailureSeverity.HIGH
        
        if category == FailureCategory.PLATFORM:
            # Platform enforcement is always at least HIGH
            if base_severity in [FailureSeverity.LOW, FailureSeverity.MEDIUM]:
                base_severity = FailureSeverity.HIGH
        
        # Check for catastrophic indicators
        context = event.context
        if any([
            context.get('state_divergence'),
            context.get('replay_failure'),
            context.get('emergency_stop_triggered'),
            'corrupt' in event.description.lower() and 'state' in event.description.lower()
        ]):
            return FailureSeverity.CATASTROPHIC
        
        return base_severity
    
    def _determine_blast_radius(self, event: SafetyEvent) -> str:
        """Determine scope of failure impact"""
        context = event.context
        
        if context.get('system_wide'):
            return 'system'
        
        if context.get('workflow_id'):
            return 'workflow'
        
        if context.get('account_id'):
            return 'account'
        
        # Infer from source
        if 'system' in event.source.lower():
            return 'system'
        
        if 'workflow' in event.source.lower():
            return 'workflow'
        
        return 'account'
    
    def _check_correlation(
        self, 
        event: SafetyEvent, 
        category: FailureCategory
    ) -> Optional[str]:
        """Check if this failure correlates with recent ones"""
        now = event.timestamp
        cutoff = now - self.CORRELATION_WINDOW_SECONDS
        
        # Filter recent signals
        recent = [
            s for s in self._recent_signals 
            if s.detected_at >= cutoff
        ]
        
        # Look for similar failures
        similar = [
            s for s in recent
            if s.category == category and s.source == event.source
        ]
        
        if len(similar) >= self.CORRELATION_UPGRADE_THRESHOLD:
            # Generate correlation ID
            correlation_id = f"{category.value}_{event.source}_{now}"
            return correlation_id
        
        return None
    
    def _upgrade_severity_if_correlated(
        self, 
        correlation_id: str,
        current_severity: FailureSeverity
    ) -> FailureSeverity:
        """Upgrade severity if part of correlated failure pattern"""
        if current_severity == FailureSeverity.LOW:
            return FailureSeverity.MEDIUM
        
        if current_severity == FailureSeverity.MEDIUM:
            return FailureSeverity.HIGH
        
        # HIGH and CATASTROPHIC stay as is
        return current_severity
    
    def _track_signal(self, signal: FailureSignal):
        """Track signal for correlation detection"""
        self._recent_signals.append(signal)
        
        # Keep only recent signals
        cutoff = signal.detected_at - self.CORRELATION_WINDOW_SECONDS
        self._recent_signals = [
            s for s in self._recent_signals 
            if s.detected_at >= cutoff
        ]
        
        if signal.correlation_id:
            self._correlation_groups[signal.correlation_id].append(signal.event_id)


class RecoverabilityAssessor:
    """Determines if and how a failure can be recovered"""
    
    # Hard rules for unrecoverable states
    UNRECOVERABLE_STATES = {
        'state_corruption',
        'replay_divergence',
        'invariant_broken',
        'emergency_stop_active'
    }
    
    # Maximum retry attempts by category
    MAX_RETRIES = {
        FailureCategory.TRANSIENT: 5,
        FailureCategory.INFRA: 3,
        FailureCategory.DATA: 0,      # No retries for data issues
        FailureCategory.LOGIC: 1,
        FailureCategory.ACCOUNT: 0,   # No retries for account issues
        FailureCategory.PLATFORM: 0,  # No retries for platform enforcement
        FailureCategory.SYSTEM: 2
    }
    
    def __init__(self, emergency_stop_active_callback=None):
        """
        Args:
            emergency_stop_active_callback: Function that returns if emergency stop is active
        """
        self._emergency_stop_check = emergency_stop_active_callback or (lambda: False)
        self._manual_intervention_required: Set[str] = set()
    
    def assess(self, signal: FailureSignal) -> RecoveryDecision:
        """
        Determine if recovery is safe and what strategy to use.
        
        Args:
            signal: Classified failure signal
            
        Returns:
            RecoveryDecision with authorization and strategy
        """
        decision_time = int(time.time())
        
        # RULE 1: Emergency stop overrides everything
        if self._emergency_stop_check():
            return RecoveryDecision(
                allow_recovery=False,
                recovery_class=RecoveryClass.HALT,
                reason="Emergency stop is active - no recovery permitted",
                decision_at=decision_time,
                severity=signal.severity,
                category=signal.category,
                signal_id=signal.event_id,
                escalation_required=True
            )
        
        # RULE 2: Catastrophic failures require emergency stop
        if signal.severity == FailureSeverity.CATASTROPHIC:
            return RecoveryDecision(
                allow_recovery=False,
                recovery_class=RecoveryClass.HALT,
                reason="Catastrophic failure detected - triggering emergency stop",
                decision_at=decision_time,
                severity=signal.severity,
                category=signal.category,
                signal_id=signal.event_id,
                escalation_required=True
            )
        
        # RULE 3: Check for unrecoverable states
        unrecoverable_reason = self._check_unrecoverable_state(signal)
        if unrecoverable_reason:
            return RecoveryDecision(
                allow_recovery=False,
                recovery_class=RecoveryClass.MANUAL,
                reason=unrecoverable_reason,
                decision_at=decision_time,
                severity=signal.severity,
                category=signal.category,
                signal_id=signal.event_id,
                escalation_required=True
            )
        
        # RULE 4: Check retry exhaustion
        if self._is_retry_exhausted(signal):
            return RecoveryDecision(
                allow_recovery=False,
                recovery_class=RecoveryClass.MANUAL,
                reason=f"Retry limit exceeded for {signal.category.value}",
                decision_at=decision_time,
                severity=signal.severity,
                category=signal.category,
                signal_id=signal.event_id,
                escalation_required=True
            )
        
        # RULE 5: Platform enforcement - no recovery
        if signal.category == FailureCategory.PLATFORM:
            return RecoveryDecision(
                allow_recovery=False,
                recovery_class=RecoveryClass.ISOLATE,
                reason="Platform enforcement action - isolation required",
                decision_at=decision_time,
                severity=signal.severity,
                category=signal.category,
                signal_id=signal.event_id,
                escalation_required=True,
                constraints={'isolate_account': True}
            )
        
        # RULE 6: Determine recovery strategy for recoverable failures
        recovery_class, constraints = self._select_recovery_strategy(signal)
        
        return RecoveryDecision(
            allow_recovery=True,
            recovery_class=recovery_class,
            reason=f"{signal.category.value} failure with {signal.severity.value} severity - {recovery_class.value} permitted",
            decision_at=decision_time,
            severity=signal.severity,
            category=signal.category,
            signal_id=signal.event_id,
            escalation_required=(signal.severity == FailureSeverity.HIGH),
            constraints=constraints
        )
    
    def _check_unrecoverable_state(self, signal: FailureSignal) -> Optional[str]:
        """Check if signal indicates unrecoverable state"""
        context = signal.context
        
        for state in self.UNRECOVERABLE_STATES:
            if context.get(state):
                return f"Unrecoverable state: {state}"
        
        # Data corruption is unrecoverable
        if signal.category == FailureCategory.DATA:
            if 'corrupt' in signal.description.lower():
                return "Data corruption detected - manual intervention required"
        
        # Logic violations are unrecoverable
        if signal.category == FailureCategory.LOGIC:
            if signal.severity == FailureSeverity.HIGH:
                return "High-severity logic violation - manual review required"
        
        return None
    
    def _is_retry_exhausted(self, signal: FailureSignal) -> bool:
        """Check if retry attempts are exhausted"""
        max_retries = self.MAX_RETRIES.get(signal.category, 0)
        return signal.retry_count >= max_retries
    
    def _select_recovery_strategy(
        self, 
        signal: FailureSignal
    ) -> tuple[RecoveryClass, Dict]:
        """Select appropriate recovery strategy and constraints"""
        category = signal.category
        severity = signal.severity
        constraints = {}
        
        # Transient failures - retry with backoff
        if category == FailureCategory.TRANSIENT:
            backoff_seconds = min(60, 2 ** signal.retry_count)
            constraints = {
                'max_retries': self.MAX_RETRIES[category],
                'backoff_seconds': backoff_seconds,
                'exponential_backoff': True
            }
            return RecoveryClass.RETRY, constraints
        
        # Infrastructure failures - requeue or degrade
        if category == FailureCategory.INFRA:
            if severity == FailureSeverity.HIGH:
                constraints = {'degrade_to': 'minimal_mode'}
                return RecoveryClass.DEGRADE, constraints
            else:
                constraints = {
                    'max_retries': self.MAX_RETRIES[category],
                    'delay_seconds': 30
                }
                return RecoveryClass.REQUEUE, constraints
        
        # Account failures - isolate
        if category == FailureCategory.ACCOUNT:
            constraints = {
                'isolate_account': True,
                'notify_admin': True
            }
            return RecoveryClass.ISOLATE, constraints
        
        # Logic failures - careful retry or manual
        if category == FailureCategory.LOGIC:
            if severity == FailureSeverity.HIGH:
                return RecoveryClass.MANUAL, {}
            else:
                constraints = {
                    'max_retries': 1,
                    'validation_required': True
                }
                return RecoveryClass.RETRY, constraints
        
        # System failures - requeue with delay
        if category == FailureCategory.SYSTEM:
            constraints = {
                'max_retries': self.MAX_RETRIES[category],
                'delay_seconds': 60
            }
            return RecoveryClass.REQUEUE, constraints
        
        # Default: manual intervention
        return RecoveryClass.MANUAL, {}


class FailureRecoveryController:
    """Public API for failure recovery decisions"""
    
    def __init__(self, emergency_stop_callback=None):
        """
        Args:
            emergency_stop_callback: Function to check if emergency stop is active
        """
        self.classifier = FailureClassifier()
        self.assessor = RecoverabilityAssessor(emergency_stop_callback)
        self._decision_log: List[RecoveryDecision] = []
        
    def evaluate(self, event: SafetyEvent) -> RecoveryDecision:
        """
        Evaluate a safety event and produce a recovery decision.
        
        This is the main entry point for the recovery authority.
        
        Args:
            event: Safety event from monitoring/watchdog
            
        Returns:
            RecoveryDecision indicating if and how to recover
        """
        # Step 1: Classify the failure
        signal = self.classifier.classify(event)
        
        # Step 2: Assess recoverability
        decision = self.assessor.assess(signal)
        
        # Step 3: Log decision (audit trail)
        self._log_decision(decision)
        
        return decision
    
    def assert_recoverable(self, decision: RecoveryDecision) -> None:
        """
        Assert that recovery is permitted.
        
        If recovery is not allowed, this will raise an exception
        to halt execution.
        
        Args:
            decision: RecoveryDecision to validate
            
        Raises:
            RecoveryNotPermittedException: If recovery is not allowed
        """
        if not decision.allow_recovery:
            raise RecoveryNotPermittedException(
                f"Recovery not permitted: {decision.reason}",
                decision=decision
            )
        
        if decision.recovery_class == RecoveryClass.HALT:
            raise RecoveryNotPermittedException(
                f"System halt required: {decision.reason}",
                decision=decision
            )
        
        if decision.recovery_class == RecoveryClass.MANUAL:
            raise RecoveryNotPermittedException(
                f"Manual intervention required: {decision.reason}",
                decision=decision
            )
    
    def get_decision_history(
        self, 
        limit: Optional[int] = None
    ) -> List[RecoveryDecision]:
        """
        Get recent recovery decisions for audit.
        
        Args:
            limit: Maximum number of decisions to return
            
        Returns:
            List of recent decisions
        """
        if limit:
            return self._decision_log[-limit:]
        return self._decision_log.copy()
    
    def get_escalation_queue(self) -> List[RecoveryDecision]:
        """Get decisions requiring escalation"""
        return [
            d for d in self._decision_log 
            if d.escalation_required
        ]
    
    def _log_decision(self, decision: RecoveryDecision):
        """Log decision for audit trail"""
        self._decision_log.append(decision)
        
        # Keep log bounded (last 1000 decisions)
        if len(self._decision_log) > 1000:
            self._decision_log = self._decision_log[-1000:]


class RecoveryNotPermittedException(Exception):
    """Raised when recovery is not permitted"""
    
    def __init__(self, message: str, decision: RecoveryDecision):
        super().__init__(message)
        self.decision = decision


# Example Usage
if __name__ == "__main__":
    # Initialize controller
    controller = FailureRecoveryController()
    
    # Simulate a transient network failure
    event = SafetyEvent(
        event_id="evt_001",
        event_type="network_timeout",
        severity="medium",
        source="api_client",
        description="Connection timeout to platform API",
        context={
            'retry_count': 0,
            'account_id': 'acc_123'
        },
        timestamp=int(time.time())
    )
    
    # Evaluate
    decision = controller.evaluate(event)
    
    print(f"Recovery allowed: {decision.allow_recovery}")
    print(f"Strategy: {decision.recovery_class.value if decision.recovery_class else 'N/A'}")
    print(f"Reason: {decision.reason}")
    print(f"Constraints: {decision.constraints}")
    
    # Check if recovery is safe
    try:
        controller.assert_recoverable(decision)
        print("\n✓ Recovery is permitted - proceeding with strategy")
    except RecoveryNotPermittedException as e:
        print(f"\n✗ Recovery blocked: {e}")
    
    # Simulate a catastrophic failure
    catastrophic_event = SafetyEvent(
        event_id="evt_002",
        event_type="state_corruption",
        severity="critical",
        source="state_manager",
        description="State replay divergence detected",
        context={
            'state_divergence': True,
            'account_id': 'acc_123'
        },
        timestamp=int(time.time())
    )
    
    decision2 = controller.evaluate(catastrophic_event)
    print(f"\n--- Catastrophic Event ---")
    print(f"Recovery allowed: {decision2.allow_recovery}")
    print(f"Strategy: {decision2.recovery_class.value if decision2.recovery_class else 'N/A'}")
    print(f"Reason: {decision2.reason}")
    print(f"Escalation required: {decision2.escalation_required}")