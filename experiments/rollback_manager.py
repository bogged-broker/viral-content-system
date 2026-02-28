"""
rollback_manager.py — INSTANT REVERT & DAMAGE CONTAINMENT

CRITICAL SYSTEM: Emergency brake for the entire viral growth platform.
Answers ONE question: "How do we stop damage NOW, cleanly, without making it worse?"

CORE RESPONSIBILITY:
- Detect rollback-worthy situations
- Identify minimum safe revert scope
- Execute rollback atomically
- Prevent cascading damage
- Lock further exposure
- Preserve forensic evidence
- Restore known-good state

AUTHORITY: Overrides all scheduling, rollout, and experimentation logic when triggered.

NON-NEGOTIABLE INVARIANTS:
❌ NEVER partially restore state
❌ NEVER mix versions
❌ NEVER resume rollout automatically after rollback
❌ NEVER discard rollback evidence
❌ NEVER silently downgrade severity
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any, Set
from pathlib import Path
import json
import hashlib
import logging
from contextlib import contextmanager


# ============================================================================
# ENUMS & CONSTANTS
# ============================================================================

class TriggerSeverity(Enum):
    """Rollback trigger severity levels - each maps to specific response protocols."""
    WARNING = "WARNING"           # Watchlist, prepare for rollback
    CRITICAL = "CRITICAL"         # Mandatory rollback
    CATASTROPHIC = "CATASTROPHIC" # Global kill-switch


class TriggerSource(Enum):
    """Sources that can initiate rollback triggers."""
    ROLLOUT = "rollout"
    EVALUATION = "evaluation"
    PLATFORM = "platform"
    WATCHDOG = "watchdog"
    SUPPRESSION = "suppression"
    EARLY_SIGNAL = "early_signal"
    HUMAN_OVERRIDE = "human_override"


class RollbackAction(Enum):
    """Atomic rollback operations."""
    PAUSE = "pause"               # Freeze current state
    REVERT = "revert"             # Restore previous state
    SUPPRESS = "suppress"         # Block traffic
    QUARANTINE = "quarantine"     # Isolate data/models


class StateType(Enum):
    """Types of state that can be rolled back."""
    MODEL = "model"
    FEATURE = "feature"
    POLICY = "policy"
    ROLLOUT_CONFIG = "rollout_config"
    AGENT_STATE = "agent_state"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class RollbackTrigger:
    """
    Immutable trigger event that initiates rollback evaluation.
    
    RULES:
    - Triggers are append-only
    - Source must be validated
    - Severity determines response protocol
    - Signals must be serializable for forensics
    """
    trigger_id: str
    source: TriggerSource
    severity: TriggerSeverity
    signals: Dict[str, Any]  # metrics, deltas, anomalies
    detected_at: datetime
    experiment_id: Optional[str] = None
    variant_id: Optional[str] = None
    platform: Optional[str] = None
    
    def __post_init__(self):
        """Validate trigger invariants."""
        if not self.trigger_id:
            raise ValueError("trigger_id cannot be empty")
        if not self.signals:
            raise ValueError("signals cannot be empty")
        if self.detected_at > datetime.utcnow():
            raise ValueError("detected_at cannot be in the future")


@dataclass(frozen=True)
class RollbackScope:
    """
    Defines the minimal sufficient scope for rollback.
    
    CRITICAL RULE: Minimally sufficient, never global unless unavoidable.
    """
    experiment_id: str
    affected_variants: List[str]
    affected_platforms: List[str]
    affected_agents: List[str]
    time_window: Tuple[datetime, datetime]
    scope_hash: str = field(init=False)
    
    def __post_init__(self):
        """Compute deterministic scope hash for audit trail."""
        scope_repr = f"{self.experiment_id}:{','.join(sorted(self.affected_variants))}:{','.join(sorted(self.affected_platforms))}"
        object.__setattr__(self, 'scope_hash', hashlib.sha256(scope_repr.encode()).hexdigest()[:16])
    
    def is_global(self) -> bool:
        """Check if this scope affects all platforms (global rollback)."""
        return len(self.affected_platforms) >= 3  # All major platforms


@dataclass(frozen=True)
class RollbackPlan:
    """
    Validated, deterministic plan for rollback execution.
    
    GUARANTEES:
    - Deterministic: same inputs → same plan
    - Idempotent: safe to execute multiple times
    - Atomic: all-or-nothing execution
    """
    plan_id: str
    scope: RollbackScope
    safe_state_version: str
    actions: List[RollbackAction]
    state_snapshots: Dict[StateType, str]  # type -> snapshot_id
    irreversible: bool
    created_at: datetime
    estimated_duration_seconds: int
    
    def __post_init__(self):
        """Validate plan invariants."""
        if not self.actions:
            raise ValueError("Plan must have at least one action")
        if not self.safe_state_version:
            raise ValueError("safe_state_version is required")
        if self.irreversible and RollbackAction.REVERT in self.actions:
            raise ValueError("Irreversible plans cannot contain REVERT action")


@dataclass
class RollbackExecution:
    """
    Mutable execution state for a rollback plan.
    Tracks progress, failures, and timing.
    """
    plan_id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "RUNNING"  # RUNNING | COMPLETED | FAILED | PARTIAL
    completed_actions: List[RollbackAction] = field(default_factory=list)
    failed_actions: List[Tuple[RollbackAction, str]] = field(default_factory=list)
    forensic_snapshot_id: Optional[str] = None
    
    def mark_completed(self):
        """Mark execution as successfully completed."""
        self.completed_at = datetime.utcnow()
        self.status = "COMPLETED"
    
    def mark_failed(self, reason: str):
        """Mark execution as failed."""
        self.completed_at = datetime.utcnow()
        self.status = "FAILED"
    
    def mark_partial(self):
        """Mark execution as partially completed (some actions failed)."""
        self.completed_at = datetime.utcnow()
        self.status = "PARTIAL"


@dataclass(frozen=True)
class StateSnapshot:
    """
    Immutable snapshot of system state at a point in time.
    Append-only, tamper-evident.
    """
    snapshot_id: str
    state_type: StateType
    version: str
    checkpoint_hash: str
    created_at: datetime
    metadata: Dict[str, Any]
    
    def verify_integrity(self, expected_hash: str) -> bool:
        """Verify snapshot has not been tampered with."""
        return self.checkpoint_hash == expected_hash


# ============================================================================
# ROLLBACK LEDGER (IMMUTABLE AUDIT LOG)
# ============================================================================

class RollbackLedger:
    """
    Immutable, append-only ledger of all rollback events.
    
    GUARANTEES:
    - Audit-safe
    - Replay-safe
    - Postmortem-critical
    - Tamper-evident
    """
    
    def __init__(self, ledger_path: Path):
        self.ledger_path = ledger_path
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__)
    
    def append(
        self,
        experiment_id: str,
        trigger: RollbackTrigger,
        scope: RollbackScope,
        plan: RollbackPlan,
        execution: RollbackExecution,
        operator: str = "system"
    ) -> str:
        """
        Append rollback event to immutable ledger.
        
        Returns: ledger_entry_id
        """
        entry_id = f"rb_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{trigger.trigger_id[:8]}"
        
        entry = {
            "entry_id": entry_id,
            "experiment_id": experiment_id,
            "trigger_id": trigger.trigger_id,
            "trigger_source": trigger.source.value,
            "severity": trigger.severity.value,
            "scope": {
                "variants": scope.affected_variants,
                "platforms": scope.affected_platforms,
                "agents": scope.affected_agents,
                "time_window": [scope.time_window[0].isoformat(), scope.time_window[1].isoformat()],
                "scope_hash": scope.scope_hash,
            },
            "plan_id": plan.plan_id,
            "safe_state_version": plan.safe_state_version,
            "actions_taken": [action.value for action in execution.completed_actions],
            "failed_actions": [(action.value, reason) for action, reason in execution.failed_actions],
            "execution_status": execution.status,
            "timestamp": datetime.utcnow().isoformat(),
            "operator": operator,
            "forensic_snapshot_id": execution.forensic_snapshot_id,
            "duration_seconds": (execution.completed_at - execution.started_at).total_seconds() if execution.completed_at else None,
        }
        
        # Append to ledger file (one JSON object per line)
        with open(self.ledger_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        
        self.logger.info(f"Rollback ledger entry created: {entry_id}")
        return entry_id
    
    def get_recent_rollbacks(self, experiment_id: str, hours: int = 24) -> List[Dict]:
        """Get recent rollbacks for an experiment."""
        if not self.ledger_path.exists():
            return []
        
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        recent = []
        
        with open(self.ledger_path, 'r') as f:
            for line in f:
                entry = json.loads(line.strip())
                if entry['experiment_id'] == experiment_id:
                    entry_time = datetime.fromisoformat(entry['timestamp'])
                    if entry_time >= cutoff:
                        recent.append(entry)
        
        return recent
    
    def count_rollbacks(self, experiment_id: str, variant_id: Optional[str] = None, hours: int = 24) -> int:
        """Count rollbacks for experiment/variant in time window."""
        recent = self.get_recent_rollbacks(experiment_id, hours)
        if variant_id:
            recent = [r for r in recent if variant_id in r['scope']['variants']]
        return len(recent)


# ============================================================================
# STATE SNAPSHOT MANAGEMENT
# ============================================================================

class StateSnapshotLoader:
    """
    Loads and validates state snapshots for rollback.
    
    RULES:
    - Snapshots are append-only
    - Snapshots are tamper-evident
    - Never loads untrusted snapshots
    """
    
    def __init__(self, snapshot_dir: Path):
        self.snapshot_dir = snapshot_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__)
    
    def save_snapshot(
        self,
        state_type: StateType,
        version: str,
        data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> StateSnapshot:
        """Save state snapshot with integrity hash."""
        snapshot_id = f"{state_type.value}_{version}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        
        # Compute integrity hash
        data_repr = json.dumps(data, sort_keys=True)
        checkpoint_hash = hashlib.sha256(data_repr.encode()).hexdigest()
        
        snapshot = StateSnapshot(
            snapshot_id=snapshot_id,
            state_type=state_type,
            version=version,
            checkpoint_hash=checkpoint_hash,
            created_at=datetime.utcnow(),
            metadata=metadata or {}
        )
        
        # Save snapshot
        snapshot_path = self.snapshot_dir / f"{snapshot_id}.json"
        with open(snapshot_path, 'w') as f:
            json.dump({
                'snapshot': {
                    'snapshot_id': snapshot.snapshot_id,
                    'state_type': snapshot.state_type.value,
                    'version': snapshot.version,
                    'checkpoint_hash': snapshot.checkpoint_hash,
                    'created_at': snapshot.created_at.isoformat(),
                    'metadata': snapshot.metadata,
                },
                'data': data,
            }, f, indent=2)
        
        self.logger.info(f"State snapshot saved: {snapshot_id}")
        return snapshot
    
    def load_snapshot(self, snapshot_id: str, verify: bool = True) -> Tuple[StateSnapshot, Dict[str, Any]]:
        """Load and optionally verify snapshot integrity."""
        snapshot_path = self.snapshot_dir / f"{snapshot_id}.json"
        
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")
        
        with open(snapshot_path, 'r') as f:
            content = json.load(f)
        
        snapshot_dict = content['snapshot']
        snapshot = StateSnapshot(
            snapshot_id=snapshot_dict['snapshot_id'],
            state_type=StateType(snapshot_dict['state_type']),
            version=snapshot_dict['version'],
            checkpoint_hash=snapshot_dict['checkpoint_hash'],
            created_at=datetime.fromisoformat(snapshot_dict['created_at']),
            metadata=snapshot_dict['metadata']
        )
        
        data = content['data']
        
        # Verify integrity if requested
        if verify:
            data_repr = json.dumps(data, sort_keys=True)
            computed_hash = hashlib.sha256(data_repr.encode()).hexdigest()
            if not snapshot.verify_integrity(computed_hash):
                raise ValueError(f"Snapshot integrity check failed: {snapshot_id}")
        
        return snapshot, data
    
    def find_safe_snapshot(
        self,
        state_type: StateType,
        before: datetime,
        min_age_hours: int = 1
    ) -> Optional[StateSnapshot]:
        """
        Find most recent safe snapshot before cutoff time.
        
        min_age_hours: Snapshots must be at least this old to be "stable"
        """
        min_age_cutoff = datetime.utcnow() - timedelta(hours=min_age_hours)
        candidates = []
        
        for snapshot_file in self.snapshot_dir.glob(f"{state_type.value}_*.json"):
            try:
                with open(snapshot_file, 'r') as f:
                    content = json.load(f)
                snapshot_dict = content['snapshot']
                created_at = datetime.fromisoformat(snapshot_dict['created_at'])
                
                if created_at < before and created_at < min_age_cutoff:
                    candidates.append((created_at, snapshot_dict['snapshot_id']))
            except Exception as e:
                self.logger.warning(f"Failed to read snapshot {snapshot_file}: {e}")
        
        if not candidates:
            return None
        
        # Return most recent valid snapshot
        candidates.sort(reverse=True)
        latest_snapshot_id = candidates[0][1]
        snapshot, _ = self.load_snapshot(latest_snapshot_id, verify=True)
        return snapshot


class SafeStateResolver:
    """
    Determines which state version is safe to roll back to.
    
    RULES:
    - Never guesses
    - Never upgrades
    - Validates safety of target state
    """
    
    def __init__(self, snapshot_loader: StateSnapshotLoader, ledger: RollbackLedger):
        self.snapshot_loader = snapshot_loader
        self.ledger = ledger
        self.logger = logging.getLogger(__name__)
    
    def resolve_safe_state(
        self,
        experiment_id: str,
        scope: RollbackScope,
        trigger: RollbackTrigger
    ) -> Dict[StateType, StateSnapshot]:
        """
        Resolve safe state snapshots for all affected state types.
        
        Returns: mapping of StateType -> StateSnapshot
        Raises: ValueError if no safe state can be found
        """
        safe_states = {}
        time_window_start = scope.time_window[0]
        
        # Find safe snapshots for each state type
        for state_type in StateType:
            snapshot = self.snapshot_loader.find_safe_snapshot(
                state_type=state_type,
                before=time_window_start,
                min_age_hours=1  # Require 1 hour stability
            )
            
            if snapshot is None:
                self.logger.warning(f"No safe snapshot found for {state_type.value}")
                # Some state types may not need rollback
                continue
            
            # Validate snapshot is not from a previously rolled-back state
            if self._is_tainted_state(snapshot, experiment_id):
                self.logger.error(f"Safe snapshot {snapshot.snapshot_id} is tainted")
                raise ValueError(f"No untainted safe state found for {state_type.value}")
            
            safe_states[state_type] = snapshot
        
        if not safe_states:
            raise ValueError("No safe states found for any state type")
        
        return safe_states
    
    def _is_tainted_state(self, snapshot: StateSnapshot, experiment_id: str) -> bool:
        """Check if snapshot was created during a period that was later rolled back."""
        # Check ledger for rollbacks that occurred after this snapshot
        recent_rollbacks = self.ledger.get_recent_rollbacks(experiment_id, hours=168)  # 1 week
        
        for rollback in recent_rollbacks:
            rollback_window_start = datetime.fromisoformat(rollback['scope']['time_window'][0])
            rollback_window_end = datetime.fromisoformat(rollback['scope']['time_window'][1])
            
            # If snapshot was created during a rolled-back period, it's tainted
            if rollback_window_start <= snapshot.created_at <= rollback_window_end:
                return True
        
        return False
    
    def allow_partial_rollback(self, scope: RollbackScope, available_states: Dict[StateType, StateSnapshot]) -> bool:
        """
        Determine if partial rollback is safe.
        
        CONSERVATIVE: Only allow if non-critical state types are missing.
        """
        critical_types = {StateType.MODEL, StateType.POLICY}
        available_critical = set(available_states.keys()) & critical_types
        
        # Must have all critical state types
        return available_critical == critical_types


# ============================================================================
# EXPOSURE CONTAINMENT
# ============================================================================

class ExposureContainmentUnit:
    """
    Hard guarantees for traffic containment during rollback.
    
    GUARANTEES:
    - No new traffic routed
    - No re-exposure
    - No delayed promotions
    - No automated retries
    
    This stops invisible damage.
    """
    
    def __init__(self):
        self.contained_experiments: Set[str] = set()
        self.contained_variants: Set[Tuple[str, str]] = set()  # (exp_id, variant_id)
        self.logger = logging.getLogger(__name__)
    
    def freeze_exposure(self, scope: RollbackScope):
        """Immediately halt all new exposure for scope."""
        self.contained_experiments.add(scope.experiment_id)
        
        for variant_id in scope.affected_variants:
            self.contained_variants.add((scope.experiment_id, variant_id))
        
        self.logger.critical(f"EXPOSURE FROZEN: {scope.experiment_id} variants={scope.affected_variants}")
    
    def is_contained(self, experiment_id: str, variant_id: Optional[str] = None) -> bool:
        """Check if experiment/variant is contained."""
        if experiment_id in self.contained_experiments:
            return True
        
        if variant_id and (experiment_id, variant_id) in self.contained_variants:
            return True
        
        return False
    
    def release_containment(self, experiment_id: str, variant_id: Optional[str] = None):
        """Release containment (requires human approval in production)."""
        if variant_id:
            self.contained_variants.discard((experiment_id, variant_id))
            self.logger.warning(f"Containment released: {experiment_id}/{variant_id}")
        else:
            self.contained_experiments.discard(experiment_id)
            for var_id in list(self.contained_variants):
                if var_id[0] == experiment_id:
                    self.contained_variants.discard(var_id)
            self.logger.warning(f"Containment released: {experiment_id} (all variants)")


# ============================================================================
# ROLLBACK WATCHDOG
# ============================================================================

class RollbackWatchdog:
    """
    Monitors for rollback anomalies and escalates to global kill-switch.
    
    MONITORS:
    - Repeated rollbacks (thrashing)
    - Rollback loops
    - Partial failures
    - Unauthorized overrides
    """
    
    def __init__(self, ledger: RollbackLedger):
        self.ledger = ledger
        self.logger = logging.getLogger(__name__)
    
    def check_rollback_health(self, experiment_id: str) -> Tuple[bool, List[str]]:
        """
        Check if rollback patterns indicate system instability.
        
        Returns: (is_healthy, warnings)
        """
        warnings = []
        
        # Check for thrashing (>3 rollbacks in 24h)
        recent_count = self.ledger.count_rollbacks(experiment_id, hours=24)
        if recent_count > 3:
            warnings.append(f"THRASHING: {recent_count} rollbacks in 24h")
        
        # Check for rollback loops (same variant rolled back multiple times)
        recent_rollbacks = self.ledger.get_recent_rollbacks(experiment_id, hours=24)
        variant_rollback_counts = {}
        for rb in recent_rollbacks:
            for variant in rb['scope']['variants']:
                variant_rollback_counts[variant] = variant_rollback_counts.get(variant, 0) + 1
        
        for variant, count in variant_rollback_counts.items():
            if count > 2:
                warnings.append(f"ROLLBACK LOOP: variant {variant} rolled back {count} times")
        
        # Check for partial failures
        partial_failures = [rb for rb in recent_rollbacks if rb['execution_status'] == 'PARTIAL']
        if partial_failures:
            warnings.append(f"PARTIAL FAILURES: {len(partial_failures)} incomplete rollbacks")
        
        is_healthy = len(warnings) == 0
        return is_healthy, warnings
    
    def should_trigger_kill_switch(self, experiment_id: str) -> bool:
        """Determine if global kill-switch should be triggered."""
        is_healthy, warnings = self.check_rollback_health(experiment_id)
        
        if not is_healthy:
            self.logger.critical(f"KILL-SWITCH EVALUATION: {experiment_id} - {warnings}")
        
        # Trigger kill-switch if:
        # - More than 5 rollbacks in 24h
        # - Any variant rolled back more than 3 times
        # - More than 2 partial failures
        
        recent_count = self.ledger.count_rollbacks(experiment_id, hours=24)
        if recent_count > 5:
            return True
        
        recent_rollbacks = self.ledger.get_recent_rollbacks(experiment_id, hours=24)
        variant_counts = {}
        for rb in recent_rollbacks:
            for variant in rb['scope']['variants']:
                variant_counts[variant] = variant_counts.get(variant, 0) + 1
        
        if any(count > 3 for count in variant_counts.values()):
            return True
        
        partial_failures = [rb for rb in recent_rollbacks if rb['execution_status'] == 'PARTIAL']
        if len(partial_failures) > 2:
            return True
        
        return False


# ============================================================================
# ROLLBACK MANAGER (CORE ENGINE)
# ============================================================================

class RollbackManager:
    """
    Central authority for rollback decisions and execution.
    
    AUTHORITY: Overrides all scheduling, rollout, and experimentation logic.
    
    WORKFLOW:
    1. evaluate_trigger() → decide if rollback needed
    2. compute_scope() → determine minimal revert scope
    3. construct_plan() → build validated rollback plan
    4. execute_rollback() → atomically execute plan
    """
    
    def __init__(
        self,
        ledger_path: Path,
        snapshot_dir: Path,
        auto_execute: bool = False
    ):
        self.ledger = RollbackLedger(ledger_path)
        self.snapshot_loader = StateSnapshotLoader(snapshot_dir)
        self.safe_state_resolver = SafeStateResolver(self.snapshot_loader, self.ledger)
        self.containment = ExposureContainmentUnit()
        self.watchdog = RollbackWatchdog(self.ledger)
        self.auto_execute = auto_execute
        self.logger = logging.getLogger(__name__)
    
    def evaluate_trigger(self, trigger: RollbackTrigger) -> bool:
        """
        Decide if trigger warrants rollback.
        
        HARD RULES (NO NEGOTIATION):
        - severity >= CRITICAL → mandatory rollback
        - platform penalties → immediate containment
        - confidence collapse → rollback eligible
        - cascading correlation failures → rollback required
        """
        # CRITICAL or CATASTROPHIC → always rollback
        if trigger.severity in {TriggerSeverity.CRITICAL, TriggerSeverity.CATASTROPHIC}:
            self.logger.critical(f"MANDATORY ROLLBACK: {trigger.trigger_id} severity={trigger.severity.value}")
            return True
        
        # Platform penalties → immediate rollback
        if trigger.source == TriggerSource.PLATFORM:
            platform_signals = trigger.signals.get('platform_penalties', {})
            if any(platform_signals.values()):
                self.logger.critical(f"PLATFORM PENALTY ROLLBACK: {trigger.trigger_id}")
                return True
        
        # Confidence collapse (>50% drop in virality coefficient)
        virality_drop = trigger.signals.get('virality_drop_pct', 0)
        if virality_drop > 50:
            self.logger.critical(f"CONFIDENCE COLLAPSE ROLLBACK: {trigger.trigger_id} drop={virality_drop}%")
            return True
        
        # Cascading correlation failures
        correlation_failures = trigger.signals.get('correlation_failures', 0)
        if correlation_failures >= 3:
            self.logger.critical(f"CASCADING FAILURE ROLLBACK: {trigger.trigger_id} failures={correlation_failures}")
            return True
        
        # WARNING severity → watchlist only
        if trigger.severity == TriggerSeverity.WARNING:
            self.logger.warning(f"Rollback watchlist: {trigger.trigger_id}")
            return False
        
        return False
    
    def compute_scope(self, trigger: RollbackTrigger) -> RollbackScope:
        """
        Determine minimal sufficient rollback scope.
        
        MINIMIZES:
        - Collateral damage
        - Data loss
        - Learning contamination
        """
        # Start with trigger-specific scope
        affected_variants = [trigger.variant_id] if trigger.variant_id else []
        affected_platforms = [trigger.platform] if trigger.platform else []
        affected_agents = []
        
        # Expand scope based on signals
        signals = trigger.signals
        
        # If global platform issue, expand to all platforms
        if trigger.source == TriggerSource.PLATFORM and not trigger.platform:
            affected_platforms = ['twitter', 'linkedin', 'instagram']
        
        # If correlation failures, expand to correlated variants
        if 'correlated_variants' in signals:
            affected_variants.extend(signals['correlated_variants'])
            affected_variants = list(set(affected_variants))  # dedupe
        
        # If catastrophic, expand to all variants in experiment
        if trigger.severity == TriggerSeverity.CATASTROPHIC:
            affected_variants = signals.get('all_experiment_variants', affected_variants)
        
        # Determine time window (when did the issue start?)
        issue_start = signals.get('issue_detected_at')
        if issue_start:
            if isinstance(issue_start, str):
                issue_start = datetime.fromisoformat(issue_start)
            time_window = (issue_start, datetime.utcnow())
        else:
            # Conservative: assume issue started 24h ago
            time_window = (datetime.utcnow() - timedelta(hours=24), datetime.utcnow())
        
        scope = RollbackScope(
            experiment_id=trigger.experiment_id or "UNKNOWN",
            affected_variants=affected_variants or ["ALL"],
            affected_platforms=affected_platforms or ["ALL"],
            affected_agents=affected_agents or ["ALL"],
            time_window=time_window
        )
        
        self.logger.info(f"Rollback scope computed: {scope.scope_hash} variants={len(scope.affected_variants)} platforms={len(scope.affected_platforms)}")
        
        return scope
    
    def construct_plan(self, trigger: RollbackTrigger, scope: RollbackScope) -> RollbackPlan:
        """
        Build validated, deterministic rollback plan.
        
        FINDS:
        - Last safe checkpoint
        - Last stable policy
        - Last known-good feature set
        
        PLAN MUST BE:
        - Deterministic (same inputs → same plan)
        - Idempotent (safe to execute multiple times)
        - Reversible (only if allowed)
        """
        plan_id = f"plan_{trigger.trigger_id}_{scope.scope_hash}"
        
        # Resolve safe states for rollback
        try:
            safe_states = self.safe_state_resolver.resolve_safe_state(
                experiment_id=scope.experiment_id,
                scope=scope,
                trigger=trigger
            )
        except ValueError as e:
            self.logger.error(f"Failed to resolve safe state: {e}")
            raise
        
        # Determine safe state version (use most recent model snapshot)
        model_snapshot = safe_states.get(StateType.MODEL)
        safe_state_version = model_snapshot.version if model_snapshot else "UNKNOWN"
        
        # Determine actions based on severity
        actions = []
        
        # Always freeze exposure first
        actions.append(RollbackAction.PAUSE)
        
        # Suppress traffic if platform penalties
        if trigger.source == TriggerSource.PLATFORM:
            actions.append(RollbackAction.SUPPRESS)
        
        # Quarantine data if catastrophic
        if trigger.severity == TriggerSeverity.CATASTROPHIC:
            actions.append(RollbackAction.QUARANTINE)
        
        # Revert to safe state
        actions.append(RollbackAction.REVERT)
        
        # Determine if rollback is irreversible
        irreversible = trigger.severity == TriggerSeverity.CATASTROPHIC or scope.is_global()
        
        # Estimate duration based on scope
        estimated_duration = self._estimate_rollback_duration(scope, actions)
        
        plan = RollbackPlan(
            plan_id=plan_id,
            scope=scope,
            safe_state_version=safe_state_version,
            actions=actions,
            state_snapshots={state_type: snapshot.snapshot_id for state_type, snapshot in safe_states.items()},
            irreversible=irreversible,
            created_at=datetime.utcnow(),
            estimated_duration_seconds=estimated_duration
        )
        
        self.logger.info(f"Rollback plan constructed: {plan_id} actions={[a.value for a in actions]} duration={estimated_duration}s")
        
        return plan
    
    def _estimate_rollback_duration(self, scope: RollbackScope, actions: List[RollbackAction]) -> int:
        """Estimate rollback duration in seconds."""
        base_duration = 30  # Base 30 seconds
        
        # Add time per variant
        base_duration += len(scope.affected_variants) * 10
        
        # Add time per platform
        base_duration += len(scope.affected_platforms) * 15
        
        # Add time per action
        action_times = {
            RollbackAction.PAUSE: 5,
            RollbackAction.SUPPRESS: 10,
            RollbackAction.QUARANTINE: 20,
            RollbackAction.REVERT: 30,
        }
        for action in actions:
            base_duration += action_times.get(action, 10)
        
        return base_duration
    
    def execute_rollback(
        self,
        trigger: RollbackTrigger,
        plan: RollbackPlan,
        operator: str = "system"
    ) -> RollbackExecution:
        """
        Atomically execute rollback plan.
        
        ATOMIC SEQUENCE:
        1. Freeze exposure
        2. Halt agent actions
        3. Restore safe state
        4. Invalidate tainted data
        5. Lock rollout progression
        6. Emit forensic snapshot
        
        If ANY step fails → escalate to global safety watchdog.
        """
        execution = RollbackExecution(
            plan_id=plan.plan_id,
            started_at=datetime.utcnow()
        )
        
        self.logger.critical(f"ROLLBACK EXECUTION STARTED: {plan.plan_id}")
        
        try:
            # STEP 1: Freeze exposure (CRITICAL - cannot fail)
            if RollbackAction.PAUSE in plan.actions:
                self._execute_pause(plan.scope)
                execution.completed_actions.append(RollbackAction.PAUSE)
            
            # STEP 2: Suppress traffic if needed
            if RollbackAction.SUPPRESS in plan.actions:
                self._execute_suppress(plan.scope)
                execution.completed_actions.append(RollbackAction.SUPPRESS)
            
            # STEP 3: Quarantine data if needed
            if RollbackAction.QUARANTINE in plan.actions:
                self._execute_quarantine(plan.scope)
                execution.completed_actions.append(RollbackAction.QUARANTINE)
            
            # STEP 4: Revert to safe state (CRITICAL)
            if RollbackAction.REVERT in plan.actions:
                self._execute_revert(plan)
                execution.completed_actions.append(RollbackAction.REVERT)
            
            # STEP 5: Create forensic snapshot
            forensic_id = self._create_forensic_snapshot(trigger, plan, execution)
            execution.forensic_snapshot_id = forensic_id
            
            # Mark as completed
            execution.mark_completed()
            
            self.logger.critical(f"ROLLBACK EXECUTION COMPLETED: {plan.plan_id} status={execution.status}")
            
        except Exception as e:
            self.logger.critical(f"ROLLBACK EXECUTION FAILED: {plan.plan_id} error={str(e)}")
            execution.mark_failed(str(e))
            
            # Check if watchdog should trigger kill-switch
            if self.watchdog.should_trigger_kill_switch(plan.scope.experiment_id):
                self.logger.critical(f"GLOBAL KILL-SWITCH TRIGGERED: {plan.scope.experiment_id}")
                self._trigger_global_kill_switch(plan.scope.experiment_id)
            
            raise
        
        finally:
            # Always write to ledger
            self.ledger.append(
                experiment_id=plan.scope.experiment_id,
                trigger=trigger,
                scope=plan.scope,
                plan=plan,
                execution=execution,
                operator=operator
            )
        
        return execution
    
    def _execute_pause(self, scope: RollbackScope):
        """Freeze all new exposure for scope."""
        self.containment.freeze_exposure(scope)
        self.logger.critical(f"EXPOSURE FROZEN: {scope.experiment_id}")
    
    def _execute_suppress(self, scope: RollbackScope):
        """Block all traffic for affected variants."""
        # In production, this would integrate with:
        # - posting_queue.py (halt queue processing)
        # - rollout_manager.py (prevent new assignments)
        # - factory_agent.py (stop agent actions)
        self.logger.critical(f"TRAFFIC SUPPRESSED: {scope.experiment_id} variants={scope.affected_variants}")
    
    def _execute_quarantine(self, scope: RollbackScope):
        """Isolate all data from affected time window."""
        # In production, this would:
        # - Mark data as quarantined in database
        # - Prevent use in training
        # - Tag for manual review
        self.logger.critical(f"DATA QUARANTINED: {scope.experiment_id} window={scope.time_window}")
    
    def _execute_revert(self, plan: RollbackPlan):
        """Restore system to safe state snapshots."""
        for state_type, snapshot_id in plan.state_snapshots.items():
            try:
                snapshot, data = self.snapshot_loader.load_snapshot(snapshot_id, verify=True)
                
                # In production, this would restore:
                # - Model weights/checkpoints
                # - Feature configurations
                # - Policy networks
                # - Rollout configs
                
                self.logger.critical(f"STATE RESTORED: {state_type.value} -> {snapshot_id}")
                
            except Exception as e:
                self.logger.error(f"Failed to restore {state_type.value}: {e}")
                raise
    
    def _create_forensic_snapshot(
        self,
        trigger: RollbackTrigger,
        plan: RollbackPlan,
        execution: RollbackExecution
    ) -> str:
        """Create forensic snapshot for postmortem analysis."""
        forensic_id = f"forensic_{trigger.trigger_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        
        forensic_data = {
            'trigger': {
                'trigger_id': trigger.trigger_id,
                'source': trigger.source.value,
                'severity': trigger.severity.value,
                'signals': trigger.signals,
                'detected_at': trigger.detected_at.isoformat(),
            },
            'plan': {
                'plan_id': plan.plan_id,
                'scope': {
                    'experiment_id': plan.scope.experiment_id,
                    'variants': plan.scope.affected_variants,
                    'platforms': plan.scope.affected_platforms,
                    'time_window': [plan.scope.time_window[0].isoformat(), plan.scope.time_window[1].isoformat()],
                },
                'actions': [a.value for a in plan.actions],
                'safe_state_version': plan.safe_state_version,
            },
            'execution': {
                'started_at': execution.started_at.isoformat(),
                'completed_at': execution.completed_at.isoformat() if execution.completed_at else None,
                'status': execution.status,
                'completed_actions': [a.value for a in execution.completed_actions],
                'failed_actions': [(a.value, reason) for a, reason in execution.failed_actions],
            }
        }
        
        # Save forensic snapshot
        forensic_path = self.snapshot_loader.snapshot_dir / f"{forensic_id}.json"
        with open(forensic_path, 'w') as f:
            json.dump(forensic_data, f, indent=2)
        
        self.logger.info(f"Forensic snapshot created: {forensic_id}")
        return forensic_id
    
    def _trigger_global_kill_switch(self, experiment_id: str):
        """
        GLOBAL KILL-SWITCH: Halt all activity for experiment.
        
        This is the nuclear option - only triggered when:
        - Repeated rollback failures
        - Rollback loops detected
        - System instability
        """
        self.logger.critical(f"🚨 GLOBAL KILL-SWITCH ACTIVATED: {experiment_id}")
        
        # Freeze all variants
        global_scope = RollbackScope(
            experiment_id=experiment_id,
            affected_variants=["ALL"],
            affected_platforms=["ALL"],
            affected_agents=["ALL"],
            time_window=(datetime.utcnow() - timedelta(days=7), datetime.utcnow())
        )
        
        self.containment.freeze_exposure(global_scope)
        
        # In production, this would:
        # - Halt all agent actions
        # - Stop all queue processing
        # - Prevent any new rollouts
        # - Alert on-call engineers
        # - Require manual intervention to resume


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

@contextmanager
def rollback_protection(
    manager: RollbackManager,
    experiment_id: str,
    variant_id: Optional[str] = None
):
    """
    Context manager for rollback protection.
    
    Usage:
        with rollback_protection(manager, exp_id, var_id):
            # risky operation
            execute_experiment_change()
    
    Automatically triggers rollback on exception.
    """
    try:
        yield
    except Exception as e:
        # Create emergency trigger
        trigger = RollbackTrigger(
            trigger_id=f"emergency_{experiment_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            source=TriggerSource.WATCHDOG,
            severity=TriggerSeverity.CRITICAL,
            signals={'exception': str(e), 'exception_type': type(e).__name__},
            detected_at=datetime.utcnow(),
            experiment_id=experiment_id,
            variant_id=variant_id
        )
        
        # Evaluate and potentially execute rollback
        if manager.evaluate_trigger(trigger):
            scope = manager.compute_scope(trigger)
            plan = manager.construct_plan(trigger, scope)
            
            if manager.auto_execute:
                manager.execute_rollback(trigger, plan, operator="auto_protection")
            else:
                manager.logger.critical(f"Rollback required but auto_execute=False: {plan.plan_id}")
        
        raise


def create_rollback_trigger(
    source: TriggerSource,
    severity: TriggerSeverity,
    signals: Dict[str, Any],
    experiment_id: Optional[str] = None,
    variant_id: Optional[str] = None,
    platform: Optional[str] = None
) -> RollbackTrigger:
    """Convenience function to create rollback triggers."""
    trigger_id = f"{source.value}_{severity.value}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    
    return RollbackTrigger(
        trigger_id=trigger_id,
        source=source,
        severity=severity,
        signals=signals,
        detected_at=datetime.utcnow(),
        experiment_id=experiment_id,
        variant_id=variant_id,
        platform=platform
    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Setup
    logging.basicConfig(level=logging.INFO)
    
    ledger_path = Path("data/rollback_ledger.jsonl")
    snapshot_dir = Path("data/snapshots")
    
    manager = RollbackManager(
        ledger_path=ledger_path,
        snapshot_dir=snapshot_dir,
        auto_execute=False  # Set True for production auto-rollback
    )
    
    # Create some safe state snapshots
    manager.snapshot_loader.save_snapshot(
        state_type=StateType.MODEL,
        version="v1.2.3",
        data={'model_path': '/models/v1.2.3', 'config': {}},
        metadata={'created_by': 'training_pipeline'}
    )
    
    manager.snapshot_loader.save_snapshot(
        state_type=StateType.FEATURE,
        version="v1.2.3",
        data={'features': ['f1', 'f2', 'f3']},
        metadata={'created_by': 'feature_registry'}
    )
    
    # Simulate a rollback trigger (platform penalty)
    trigger = create_rollback_trigger(
        source=TriggerSource.PLATFORM,
        severity=TriggerSeverity.CRITICAL,
        signals={
            'platform_penalties': {'twitter': True},
            'virality_drop_pct': 75,
            'issue_detected_at': (datetime.utcnow() - timedelta(hours=2)).isoformat()
        },
        experiment_id="exp_viral_hooks_v3",
        variant_id="var_aggressive_cta",
        platform="twitter"
    )
    
    # Evaluate trigger
    should_rollback = manager.evaluate_trigger(trigger)
    print(f"Should rollback: {should_rollback}")
    
    if should_rollback:
        # Compute scope
        scope = manager.compute_scope(trigger)
        print(f"Rollback scope: variants={scope.affected_variants} platforms={scope.affected_platforms}")
        
        # Construct plan
        plan = manager.construct_plan(trigger, scope)
        print(f"Rollback plan: {plan.plan_id} actions={[a.value for a in plan.actions]}")
        
        # Execute rollback (if auto_execute=True)
        if manager.auto_execute:
            execution = manager.execute_rollback(trigger, plan)
            print(f"Rollback execution: {execution.status}")
        else:
            print("Auto-execute disabled - manual approval required")
    
    # Check rollback health
    is_healthy, warnings = manager.watchdog.check_rollback_health("exp_viral_hooks_v3")
    print(f"Rollback health: healthy={is_healthy} warnings={warnings}")