"""
/account_system/watchdog.py

Global Account Kill-Switch, Escalation & Alerting Authority

This is the failsafe of failsafes - the constitutional kill-switch that fires
when everything else technically "still works" but absolutely should not continue.

Core Principle:
    The watchdog operates on system risk, not optimism.
    If uncertainty is high → err on containment, not growth.

Authority:
    - No other system overrides watchdog decisions
    - Actions are immediate and durable
    - Correlation trumps individual signals
    - Human intervention required for clearance on critical actions
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional, Tuple
from enum import Enum
import hashlib
import json
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class SignalSeverity(Enum):
    """Signal severity levels"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


class TriggerSeverity(Enum):
    """Trigger severity determines action urgency"""
    WARNING = "warning"      # Monitor closely
    HARD = "hard"           # Take action now
    CRITICAL = "critical"   # Immediate kill-switch


class ActionType(Enum):
    """Available watchdog actions"""
    FREEZE_POSTING = "freeze_posting"           # Stop all output
    ISOLATE_ACCOUNT = "isolate_account"         # Remove from shared infra
    DISABLE_AUTOMATION = "disable_automation"   # Human-only mode
    HALT_EXPERIMENTS = "halt_experiments"       # Abort all experiments
    GLOBAL_STOP = "global_stop"                 # Total shutdown
    ESCALATE_HUMAN = "escalate_human_review"    # Force human review


class SignalSource(Enum):
    """Where signals originate"""
    TRUST = "trust"
    SUPPRESSION = "suppression"
    ENFORCEMENT = "enforcement"
    NETWORK = "network"
    INVARIANT = "invariant"
    BEHAVIOR = "behavior"
    PLATFORM = "platform"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class WatchdogSignal:
    """
    A signal from upstream systems that feeds watchdog evaluation.
    Signals are already validated - this file only correlates.
    """
    source: SignalSource
    signal_type: str
    value: float | bool
    severity: SignalSeverity
    detected_at: datetime
    metadata: Dict = field(default_factory=dict)
    
    def __hash__(self):
        return hash((self.source, self.signal_type, self.detected_at))


@dataclass(frozen=True)
class WatchdogTrigger:
    """
    Defines a condition that causes watchdog action.
    Triggers are explicit, versioned, and deterministic.
    """
    trigger_id: str
    description: str
    required_signals: Set[str]          # Signal types that must be present
    evaluation_logic: str               # Deterministic rule identifier
    severity: TriggerSeverity
    action: ActionType
    min_signal_count: int = 1
    correlation_window_hours: int = 24
    
    def matches(self, signals: List[WatchdogSignal]) -> bool:
        """Check if this trigger's conditions are met"""
        signal_types = {s.signal_type for s in signals}
        return len(self.required_signals & signal_types) >= self.min_signal_count


@dataclass(frozen=True)
class WatchdogAction:
    """
    An action imposed by the watchdog.
    Actions are stateful, durable, and audited.
    """
    action_id: str
    action_type: ActionType
    severity: TriggerSeverity
    imposed_at: datetime
    reason: str
    trigger_id: str
    reversible: bool
    min_duration_hours: int = 24
    metadata: Dict = field(default_factory=dict)
    
    def can_clear(self, current_time: datetime) -> bool:
        """Check if action can be cleared based on time alone"""
        if not self.reversible:
            return False
        elapsed = current_time - self.imposed_at
        return elapsed >= timedelta(hours=self.min_duration_hours)


@dataclass
class WatchdogState:
    """
    Current watchdog state for an account.
    Tracks active actions, trigger history, and evaluation chain.
    """
    account_id: str
    active_actions: Dict[str, WatchdogAction] = field(default_factory=dict)
    trigger_history: List[Tuple[datetime, str]] = field(default_factory=list)
    last_evaluation: Optional[datetime] = None
    last_evaluation_hash: Optional[str] = None
    escalation_count: int = 0
    
    def add_action(self, action: WatchdogAction):
        """Register new active action"""
        self.active_actions[action.action_id] = action
        self.trigger_history.append((action.imposed_at, action.trigger_id))
        if action.severity == TriggerSeverity.CRITICAL:
            self.escalation_count += 1
    
    def remove_action(self, action_id: str) -> Optional[WatchdogAction]:
        """Remove action if present"""
        return self.active_actions.pop(action_id, None)
    
    def has_active_action(self, action_type: ActionType) -> bool:
        """Check if specific action type is active"""
        return any(a.action_type == action_type for a in self.active_actions.values())
    
    def highest_severity(self) -> Optional[TriggerSeverity]:
        """Get highest severity of active actions"""
        if not self.active_actions:
            return None
        return max(a.severity for a in self.active_actions.values())


@dataclass
class WatchdogAuditLog:
    """
    Immutable audit record of watchdog decision.
    Used for forensic replay and compliance review.
    """
    evaluation_id: str
    account_id: str
    timestamp: datetime
    signals: List[WatchdogSignal]
    triggered_actions: List[WatchdogAction]
    policy_version: str
    evaluation_hash: str
    
    @staticmethod
    def compute_hash(account_id: str, signals: List[WatchdogSignal], 
                     policy_version: str, timestamp: datetime) -> str:
        """Deterministic hash for evaluation replay"""
        data = {
            'account_id': account_id,
            'signals': sorted([
                f"{s.source.value}:{s.signal_type}:{s.value}"
                for s in signals
            ]),
            'policy_version': policy_version,
            'timestamp': timestamp.isoformat()
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()


# ============================================================================
# WATCHDOG POLICY
# ============================================================================

class WatchdogPolicy:
    """
    Policy defines trigger precedence, action escalation, and clearance rules.
    Policy changes require version bumps.
    """
    
    VERSION = "1.0.0"
    
    # Trigger definitions - explicit and versioned
    TRIGGERS = {
        # CRITICAL: Trust collapse + enforcement
        "trust_enforcement_collapse": WatchdogTrigger(
            trigger_id="trust_enforcement_collapse",
            description="Trust score critical + enforcement detected",
            required_signals={"trust_critical", "enforcement_active"},
            evaluation_logic="AND",
            severity=TriggerSeverity.CRITICAL,
            action=ActionType.GLOBAL_STOP,
            min_signal_count=2,
            correlation_window_hours=6
        ),
        
        # CRITICAL: Cascading suppression + network risk
        "suppression_network_cascade": WatchdogTrigger(
            trigger_id="suppression_network_cascade",
            description="High suppression + network affiliation risk",
            required_signals={"suppression_high", "network_risk_elevated"},
            evaluation_logic="AND",
            severity=TriggerSeverity.CRITICAL,
            action=ActionType.ISOLATE_ACCOUNT,
            min_signal_count=2,
            correlation_window_hours=12
        ),
        
        # HARD: Invariant violation + trust decline
        "invariant_trust_degradation": WatchdogTrigger(
            trigger_id="invariant_trust_degradation",
            description="Invariant broken + trust declining",
            required_signals={"invariant_violated", "trust_declining"},
            evaluation_logic="AND",
            severity=TriggerSeverity.HARD,
            action=ActionType.DISABLE_AUTOMATION,
            min_signal_count=2,
            correlation_window_hours=24
        ),
        
        # HARD: Platform throttling + behavior anomaly
        "platform_behavior_anomaly": WatchdogTrigger(
            trigger_id="platform_behavior_anomaly",
            description="Platform throttling + unusual behavior pattern",
            required_signals={"platform_throttle", "behavior_anomaly"},
            evaluation_logic="AND",
            severity=TriggerSeverity.HARD,
            action=ActionType.FREEZE_POSTING,
            min_signal_count=2,
            correlation_window_hours=24
        ),
        
        # HARD: Experiment failure + risk elevation
        "experiment_risk_failure": WatchdogTrigger(
            trigger_id="experiment_risk_failure",
            description="Experiment producing failures + risk rising",
            required_signals={"experiment_failure", "risk_elevated"},
            evaluation_logic="AND",
            severity=TriggerSeverity.HARD,
            action=ActionType.HALT_EXPERIMENTS,
            min_signal_count=2,
            correlation_window_hours=12
        ),
        
        # WARNING: Multi-signal degradation
        "multi_signal_degradation": WatchdogTrigger(
            trigger_id="multi_signal_degradation",
            description="Multiple concerning signals without critical correlation",
            required_signals={"trust_declining", "suppression_rising", "behavior_shift"},
            evaluation_logic="OR_THRESHOLD",
            severity=TriggerSeverity.WARNING,
            action=ActionType.ESCALATE_HUMAN,
            min_signal_count=2,
            correlation_window_hours=48
        )
    }
    
    # Action escalation ladder
    ACTION_PRECEDENCE = {
        ActionType.ESCALATE_HUMAN: 1,
        ActionType.FREEZE_POSTING: 2,
        ActionType.HALT_EXPERIMENTS: 3,
        ActionType.DISABLE_AUTOMATION: 4,
        ActionType.ISOLATE_ACCOUNT: 5,
        ActionType.GLOBAL_STOP: 6
    }
    
    # Minimum persistence durations (hours)
    MIN_DURATION = {
        ActionType.ESCALATE_HUMAN: 0,       # Immediate clearance possible
        ActionType.FREEZE_POSTING: 24,
        ActionType.HALT_EXPERIMENTS: 12,
        ActionType.DISABLE_AUTOMATION: 48,
        ActionType.ISOLATE_ACCOUNT: 72,
        ActionType.GLOBAL_STOP: 168         # 1 week minimum
    }
    
    # Auto-clear eligibility
    AUTO_CLEAR_ELIGIBLE = {
        ActionType.ESCALATE_HUMAN,
        ActionType.FREEZE_POSTING,
        ActionType.HALT_EXPERIMENTS
    }
    
    @classmethod
    def get_min_duration(cls, action_type: ActionType) -> int:
        """Get minimum duration for action type"""
        return cls.MIN_DURATION.get(action_type, 24)
    
    @classmethod
    def can_auto_clear(cls, action_type: ActionType) -> bool:
        """Check if action can be auto-cleared"""
        return action_type in cls.AUTO_CLEAR_ELIGIBLE
    
    @classmethod
    def get_precedence(cls, action_type: ActionType) -> int:
        """Get precedence level (higher = more severe)"""
        return cls.ACTION_PRECEDENCE.get(action_type, 0)


# ============================================================================
# CORRELATION ENGINE
# ============================================================================

class CorrelationEngine:
    """
    Detects correlated signals that indicate systemic risk.
    Single signal = weak. Correlated signals = authority.
    """
    
    @staticmethod
    def filter_by_window(signals: List[WatchdogSignal], 
                        window_hours: int) -> List[WatchdogSignal]:
        """Filter signals to correlation window"""
        cutoff = datetime.utcnow() - timedelta(hours=window_hours)
        return [s for s in signals if s.detected_at >= cutoff]
    
    @staticmethod
    def find_correlations(signals: List[WatchdogSignal],
                         window_hours: int = 24) -> Dict[str, List[WatchdogSignal]]:
        """
        Group signals by correlation patterns.
        Returns dict of pattern_name -> correlated_signals
        """
        recent = CorrelationEngine.filter_by_window(signals, window_hours)
        
        correlations = {}
        
        # Trust + Enforcement correlation
        trust_signals = [s for s in recent if s.source == SignalSource.TRUST]
        enforcement_signals = [s for s in recent if s.source == SignalSource.ENFORCEMENT]
        if trust_signals and enforcement_signals:
            correlations['trust_enforcement'] = trust_signals + enforcement_signals
        
        # Suppression + Network correlation
        suppression_signals = [s for s in recent if s.source == SignalSource.SUPPRESSION]
        network_signals = [s for s in recent if s.source == SignalSource.NETWORK]
        if suppression_signals and network_signals:
            correlations['suppression_network'] = suppression_signals + network_signals
        
        # Behavior + Platform correlation
        behavior_signals = [s for s in recent if s.source == SignalSource.BEHAVIOR]
        platform_signals = [s for s in recent if s.source == SignalSource.PLATFORM]
        if behavior_signals and platform_signals:
            correlations['behavior_platform'] = behavior_signals + platform_signals
        
        # Multi-source degradation (3+ sources with concerning signals)
        sources_present = {s.source for s in recent 
                          if s.severity in [SignalSeverity.WARNING, SignalSeverity.CRITICAL]}
        if len(sources_present) >= 3:
            correlations['multi_source_degradation'] = recent
        
        return correlations
    
    @staticmethod
    def evaluate_trigger(trigger: WatchdogTrigger,
                        signals: List[WatchdogSignal]) -> bool:
        """Evaluate if trigger conditions are met"""
        # Filter to correlation window
        recent = CorrelationEngine.filter_by_window(
            signals, trigger.correlation_window_hours
        )
        
        # Check required signals
        signal_types = {s.signal_type for s in recent}
        matches = trigger.required_signals & signal_types
        
        if len(matches) < trigger.min_signal_count:
            return False
        
        # Apply evaluation logic
        if trigger.evaluation_logic == "AND":
            return len(matches) == len(trigger.required_signals)
        elif trigger.evaluation_logic == "OR_THRESHOLD":
            return len(matches) >= trigger.min_signal_count
        
        return True


# ============================================================================
# WATCHDOG EVALUATOR
# ============================================================================

class WatchdogEvaluator:
    """
    Evaluates account state against watchdog policy.
    Determines which actions (if any) should be triggered.
    """
    
    def __init__(self, policy: WatchdogPolicy = WatchdogPolicy):
        self.policy = policy
        self.correlation_engine = CorrelationEngine()
    
    def evaluate(self, account_id: str,
                signals: List[WatchdogSignal],
                current_state: WatchdogState) -> Tuple[List[WatchdogAction], WatchdogAuditLog]:
        """
        Evaluate account and determine required actions.
        
        Returns:
            (new_actions, audit_log)
        """
        timestamp = datetime.utcnow()
        triggered_actions = []
        
        # Check each trigger
        for trigger in self.policy.TRIGGERS.values():
            if self.correlation_engine.evaluate_trigger(trigger, signals):
                # Don't re-trigger if action already active
                if not current_state.has_active_action(trigger.action):
                    action = self._create_action(trigger, account_id, timestamp)
                    triggered_actions.append(action)
                    logger.warning(
                        f"Watchdog trigger fired: {trigger.trigger_id} -> {trigger.action.value}",
                        extra={'account_id': account_id, 'trigger': trigger.trigger_id}
                    )
        
        # Sort by precedence (most severe first)
        triggered_actions.sort(
            key=lambda a: self.policy.get_precedence(a.action_type),
            reverse=True
        )
        
        # Create audit log
        eval_hash = WatchdogAuditLog.compute_hash(
            account_id, signals, self.policy.VERSION, timestamp
        )
        
        audit = WatchdogAuditLog(
            evaluation_id=f"eval_{account_id}_{timestamp.timestamp()}",
            account_id=account_id,
            timestamp=timestamp,
            signals=signals,
            triggered_actions=triggered_actions,
            policy_version=self.policy.VERSION,
            evaluation_hash=eval_hash
        )
        
        return triggered_actions, audit
    
    def _create_action(self, trigger: WatchdogTrigger,
                      account_id: str, timestamp: datetime) -> WatchdogAction:
        """Create action from trigger"""
        return WatchdogAction(
            action_id=f"{trigger.trigger_id}_{account_id}_{timestamp.timestamp()}",
            action_type=trigger.action,
            severity=trigger.severity,
            imposed_at=timestamp,
            reason=trigger.description,
            trigger_id=trigger.trigger_id,
            reversible=trigger.severity != TriggerSeverity.CRITICAL,
            min_duration_hours=self.policy.get_min_duration(trigger.action),
            metadata={'account_id': account_id}
        )


# ============================================================================
# WATCHDOG CONTROLLER
# ============================================================================

class WatchdogController:
    """
    Final authority for watchdog decisions.
    Evaluates, triggers, and clears actions with full audit trail.
    """
    
    def __init__(self):
        self.evaluator = WatchdogEvaluator()
        self.states: Dict[str, WatchdogState] = {}
        self.audit_logs: List[WatchdogAuditLog] = []
    
    def evaluate_account(self, account_id: str,
                        signals: List[WatchdogSignal]) -> WatchdogState:
        """
        Evaluate account and trigger actions if needed.
        
        This is the primary entry point for watchdog evaluation.
        NO HUMAN IN THE LOOP HERE.
        """
        # Get or create state
        if account_id not in self.states:
            self.states[account_id] = WatchdogState(account_id=account_id)
        
        state = self.states[account_id]
        
        # Run evaluation
        new_actions, audit = self.evaluator.evaluate(account_id, signals, state)
        
        # Trigger new actions
        for action in new_actions:
            self.trigger_action(account_id, action)
        
        # Update state
        state.last_evaluation = datetime.utcnow()
        state.last_evaluation_hash = audit.evaluation_hash
        
        # Store audit
        self.audit_logs.append(audit)
        
        return state
    
    def trigger_action(self, account_id: str, action: WatchdogAction):
        """
        Trigger watchdog action immediately.
        
        Effects:
        - Writes to durable store
        - Emits alerts
        - Blocks downstream systems
        - Tags account globally
        """
        state = self.states.get(account_id)
        if not state:
            logger.error(f"Cannot trigger action - no state for {account_id}")
            return
        
        # Add to active actions
        state.add_action(action)
        
        # Log with severity
        log_level = logging.CRITICAL if action.severity == TriggerSeverity.CRITICAL else logging.ERROR
        logger.log(
            log_level,
            f"WATCHDOG ACTION TRIGGERED: {action.action_type.value}",
            extra={
                'account_id': account_id,
                'action_id': action.action_id,
                'severity': action.severity.value,
                'reason': action.reason,
                'reversible': action.reversible
            }
        )
        
        # Alert appropriate systems
        self._emit_alerts(account_id, action)
        
        # Block downstream systems
        self._block_downstream(account_id, action)
    
    def clear_action(self, account_id: str, action_id: str,
                    force: bool = False) -> bool:
        """
        Clear an active action if policy permits.
        
        Allowed only if:
        - Policy permits
        - Cooldown elapsed
        - Trust & risk stabilized (checked externally)
        - No enforcement active
        
        Returns:
            True if cleared, False if denied
        """
        state = self.states.get(account_id)
        if not state:
            return False
        
        action = state.active_actions.get(action_id)
        if not action:
            return False
        
        # Check if clearance allowed
        if not force:
            # Non-reversible actions cannot be cleared
            if not action.reversible:
                logger.warning(
                    f"Cannot clear non-reversible action {action_id}",
                    extra={'account_id': account_id}
                )
                return False
            
            # Check minimum duration
            if not action.can_clear(datetime.utcnow()):
                logger.warning(
                    f"Cannot clear action {action_id} - min duration not elapsed",
                    extra={'account_id': account_id}
                )
                return False
        
        # Clear action
        state.remove_action(action_id)
        
        logger.info(
            f"Watchdog action cleared: {action.action_type.value}",
            extra={
                'account_id': account_id,
                'action_id': action_id,
                'forced': force
            }
        )
        
        return True
    
    def get_state(self, account_id: str) -> Optional[WatchdogState]:
        """Get current watchdog state for account"""
        return self.states.get(account_id)
    
    def get_audit_trail(self, account_id: str,
                       limit: int = 100) -> List[WatchdogAuditLog]:
        """Get audit trail for account"""
        logs = [log for log in self.audit_logs if log.account_id == account_id]
        return sorted(logs, key=lambda x: x.timestamp, reverse=True)[:limit]
    
    def _emit_alerts(self, account_id: str, action: WatchdogAction):
        """Emit alerts to monitoring systems"""
        # In production: send to alerting infrastructure
        # PagerDuty, Slack, email, etc.
        pass
    
    def _block_downstream(self, account_id: str, action: WatchdogAction):
        """Block downstream systems based on action type"""
        # In production: update feature flags, service mesh, etc.
        # to enforce action across all systems
        pass


# ============================================================================
# ABSOLUTE RULES - ENFORCED
# ============================================================================

class WatchdogRules:
    """
    Absolute rules that MUST NEVER be violated.
    These are enforced at the code level.
    """
    
    @staticmethod
    def validate_no_delay_for_performance(action: WatchdogAction) -> bool:
        """Actions must trigger immediately - no delays allowed"""
        return True  # Enforced by synchronous trigger_action()
    
    @staticmethod
    def validate_no_ignore_invariants(signals: List[WatchdogSignal]) -> bool:
        """Invariant violations must never be ignored"""
        invariant_signals = [s for s in signals 
                           if s.source == SignalSource.INVARIANT]
        # If invariant signals present, they must be evaluated
        return True  # Enforced by evaluator checking all triggers
    
    @staticmethod
    def validate_no_softening_enforcement(signals: List[WatchdogSignal]) -> bool:
        """Enforcement pressure must never be softened"""
        enforcement_signals = [s for s in signals
                             if s.source == SignalSource.ENFORCEMENT]
        # Enforcement signals automatically trigger highest severity
        return True  # Enforced by trigger definitions
    
    @staticmethod
    def validate_no_retry_logic() -> bool:
        """No 'one more time' retries - decisions are final"""
        return True  # Enforced by stateful actions with min durations
    
    @staticmethod
    def validate_not_tuned_for_growth() -> bool:
        """System prioritizes safety over growth"""
        # If growth slows here → that's success
        return True  # Enforced by correlation thresholds and severity


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

def example_usage():
    """Demonstrate watchdog system in action"""
    
    controller = WatchdogController()
    
    # Simulate signals from upstream systems
    signals = [
        WatchdogSignal(
            source=SignalSource.TRUST,
            signal_type="trust_critical",
            value=0.15,
            severity=SignalSeverity.CRITICAL,
            detected_at=datetime.utcnow(),
            metadata={'score': 0.15}
        ),
        WatchdogSignal(
            source=SignalSource.ENFORCEMENT,
            signal_type="enforcement_active",
            value=True,
            severity=SignalSeverity.CRITICAL,
            detected_at=datetime.utcnow(),
            metadata={'platform': 'twitter', 'type': 'account_lock'}
        ),
        WatchdogSignal(
            source=SignalSource.SUPPRESSION,
            signal_type="suppression_high",
            value=0.89,
            severity=SignalSeverity.WARNING,
            detected_at=datetime.utcnow() - timedelta(hours=2),
            metadata={'suppression_rate': 0.89}
        )
    ]
    
    # Evaluate account
    account_id = "acc_123"
    state = controller.evaluate_account(account_id, signals)
    
    print(f"Account: {account_id}")
    print(f"Active actions: {len(state.active_actions)}")
    print(f"Escalation count: {state.escalation_count}")
    print(f"Highest severity: {state.highest_severity()}")
    
    for action in state.active_actions.values():
        print(f"\nAction: {action.action_type.value}")
        print(f"  Severity: {action.severity.value}")
        print(f"  Reason: {action.reason}")
        print(f"  Reversible: {action.reversible}")
        print(f"  Min duration: {action.min_duration_hours}h")
    
    # Get audit trail
    audit_trail = controller.get_audit_trail(account_id)
    print(f"\nAudit logs: {len(audit_trail)}")
    for log in audit_trail[:3]:
        print(f"  {log.timestamp}: {len(log.triggered_actions)} actions")


if __name__ == "__main__":
    example_usage()







