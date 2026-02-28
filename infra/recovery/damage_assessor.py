"""
damage_assessor.py - Post-Failure Blast Radius & Damage Classification Engine

Purpose: Deterministic, evidence-based damage analysis immediately after failure.
Answers: "What exactly is broken, and how far did the blast radius go?"

This file does ZERO remediation - only produces ground truth.

Forensic pathologist of the system:
- Measures what's broken
- Determines scope and severity
- Decides recovery aggressiveness
- Feeds exact inputs to rollback planners
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Set, Tuple
from abc import ABC, abstractmethod
import time
import hashlib
import json


class DamageSeverity(Enum):
    """Damage severity levels (max-risk aggregation, not averages)"""
    NONE = "none"
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    CRITICAL = "critical"
    CATASTROPHIC = "catastrophic"
    
    def __lt__(self, other):
        order = [
            DamageSeverity.NONE,
            DamageSeverity.MINOR,
            DamageSeverity.MODERATE,
            DamageSeverity.SEVERE,
            DamageSeverity.CRITICAL,
            DamageSeverity.CATASTROPHIC
        ]
        return order.index(self) < order.index(other)


class RollbackScope(Enum):
    """Required rollback scope"""
    NO_ROLLBACK = "no_rollback"
    EXPERIMENT_ONLY = "experiment_only"
    PARTIAL = "partial"
    FULL_SYSTEM = "full_system"


class ReplaySafety(Enum):
    """Replay feasibility classification"""
    REPLAY_SAFE = "replay_safe"
    REPLAY_PARTIAL = "replay_partial"
    REPLAY_UNSAFE = "replay_unsafe"


class SubsystemType(Enum):
    """System subsystems for damage analysis"""
    INFRA = "infra"
    PERSISTENCE = "persistence"
    ACCOUNT_SYSTEM = "account_system"
    CONTENT_PIPELINES = "content_pipelines"
    EXPERIMENTS = "experiments"
    ENFORCEMENT = "enforcement"


class TriggerType(Enum):
    """Failure trigger classification"""
    INVARIANT = "invariant"
    ANOMALY = "anomaly"
    MANUAL = "manual"
    CRASH = "crash"
    PLATFORM_ENFORCEMENT = "platform_enforcement"


@dataclass(frozen=True)
class FailureEvent:
    """Input event that triggered damage assessment"""
    event_id: str
    trigger_type: TriggerType
    timestamp: int
    correlation_id: Optional[str]
    
    source: str
    description: str
    context: Dict
    
    # Optional enrichment
    affected_accounts: Optional[List[str]] = None
    affected_workflows: Optional[List[str]] = None


@dataclass
class SubsystemDamage:
    """Damage assessment for a single subsystem"""
    name: SubsystemType
    integrity_score: float  # 0.0 (destroyed) to 1.0 (pristine)
    corrupted: bool
    
    # Evidence
    hash_mismatch: bool = False
    invariant_violations: List[str] = field(default_factory=list)
    temporal_drift_seconds: float = 0.0
    partial_writes_detected: bool = False
    
    # Boundaries
    safe_boundary: Optional[str] = None  # Where contamination stops
    contamination_depth: int = 0  # Layers deep
    
    # Metadata
    evidence_hash: Optional[str] = None


@dataclass
class SnapshotScore:
    """Trust score for a candidate snapshot"""
    snapshot_id: str
    trust_score: float  # 0.0 to 1.0
    usable: bool
    
    # Scoring breakdown
    completeness_score: float = 0.0
    schema_compatibility_score: float = 0.0
    temporal_proximity_score: float = 0.0
    cross_subsystem_alignment_score: float = 0.0
    
    # Metadata
    snapshot_timestamp: Optional[int] = None
    age_seconds: Optional[float] = None
    incompatibility_reasons: List[str] = field(default_factory=list)


@dataclass
class ContaminationNode:
    """Node in contamination graph"""
    module: str
    contaminated: bool
    contamination_source: Optional[str] = None
    downstream_modules: List[str] = field(default_factory=list)


@dataclass
class BlastRadius:
    """Measured blast radius of failure"""
    scope: RollbackScope
    affected_subsystems: List[SubsystemType]
    safe_subsystems: List[SubsystemType]
    
    # Isolation boundaries
    isolation_boundaries: List[str] = field(default_factory=list)
    cascading_damage_detected: bool = False
    
    # Justification
    reasoning: str = ""


@dataclass(frozen=True)
class DamageReport:
    """Immutable, signed damage assessment report (PRIMARY OUTPUT)"""
    report_id: str
    failure_event_id: str
    assessed_at: int
    
    # Core assessment
    severity: DamageSeverity
    blast_radius: BlastRadius
    rollback_required: bool
    recommended_scope: RollbackScope
    confidence: float  # 0.0 to 1.0
    
    # Detailed findings
    corrupted_state: List[str]
    safe_state: List[str]
    subsystem_damage: Dict[str, SubsystemDamage]
    
    # Snapshot recommendations
    snapshot_trust_ladder: List[SnapshotScore]
    recommended_snapshot_id: Optional[str] = None
    
    # Replay assessment
    replay_safety: ReplaySafety = ReplaySafety.REPLAY_UNSAFE
    replay_feasibility_notes: str = ""
    
    # Forward-fix possibility
    forward_fix_possible: bool = False
    forward_fix_path: Optional[str] = None
    
    # Escalation
    human_escalation_required: bool = False
    isolation_duration_seconds: Optional[int] = None
    
    # Audit
    evidence_bundle_hash: str = ""
    report_signature: str = ""
    justification_trail: List[str] = field(default_factory=list)
    
    def verify_signature(self) -> bool:
        """Verify report hasn't been tampered with"""
        computed = self._compute_signature()
        return computed == self.report_signature
    
    def _compute_signature(self) -> str:
        """Compute deterministic signature"""
        sig_data = {
            'report_id': self.report_id,
            'failure_event_id': self.failure_event_id,
            'severity': self.severity.value,
            'rollback_required': self.rollback_required,
            'recommended_scope': self.recommended_scope.value,
            'confidence': self.confidence,
            'evidence_hash': self.evidence_bundle_hash
        }
        sig_json = json.dumps(sig_data, sort_keys=True)
        return hashlib.sha256(sig_json.encode()).hexdigest()


@dataclass
class EvidenceBundle:
    """Frozen evidence snapshot for deterministic analysis"""
    bundle_id: str
    captured_at: int
    
    # State hashes
    current_state_hash: str
    last_snapshot_hash: str
    
    # Frozen metrics
    frozen_metrics: Dict
    frozen_audit_logs: List[Dict]
    last_good_boundary: Optional[str] = None
    
    # Hash for integrity
    bundle_hash: str = ""
    
    def compute_hash(self) -> str:
        """Compute deterministic hash of evidence"""
        bundle_data = {
            'bundle_id': self.bundle_id,
            'current_state_hash': self.current_state_hash,
            'last_snapshot_hash': self.last_snapshot_hash,
            'metrics_count': len(self.frozen_metrics),
            'logs_count': len(self.frozen_audit_logs)
        }
        bundle_json = json.dumps(bundle_data, sort_keys=True)
        return hashlib.sha256(bundle_json.encode()).hexdigest()


class StateBackend(ABC):
    """Abstract interface for state access"""
    
    @abstractmethod
    def get_current_state_hash(self) -> str:
        pass
    
    @abstractmethod
    def get_snapshot_hash(self, snapshot_id: str) -> str:
        pass
    
    @abstractmethod
    def get_subsystem_state(self, subsystem: SubsystemType) -> Dict:
        pass


class InvariantValidator(ABC):
    """Abstract interface for invariant validation"""
    
    @abstractmethod
    def validate_subsystem(self, subsystem: SubsystemType) -> Tuple[bool, List[str]]:
        """Returns (valid, violations)"""
        pass


class AuditLogStore(ABC):
    """Abstract interface for audit log access"""
    
    @abstractmethod
    def get_logs_since(self, timestamp: int) -> List[Dict]:
        pass
    
    @abstractmethod
    def get_last_good_marker(self) -> Optional[str]:
        pass


class SnapshotStore(ABC):
    """Abstract interface for snapshot access"""
    
    @abstractmethod
    def list_snapshots(self, limit: int = 10) -> List[str]:
        pass
    
    @abstractmethod
    def get_snapshot_metadata(self, snapshot_id: str) -> Dict:
        pass
    
    @abstractmethod
    def get_snapshot(self, snapshot_id: str) -> Dict:
        pass


class MetricsStore(ABC):
    """Abstract interface for metrics"""
    
    @abstractmethod
    def get_current_metrics(self) -> Dict:
        pass
    
    @abstractmethod
    def get_metric_deltas(self, since: int) -> Dict:
        pass


class DamageAssessor:
    """
    Post-failure damage assessment engine.
    
    Forensic pathologist that produces ground truth about system damage.
    Does NO remediation - only analysis.
    """
    
    # Severity thresholds
    INTEGRITY_CRITICAL_THRESHOLD = 0.3  # Below 30% integrity = critical
    INTEGRITY_SEVERE_THRESHOLD = 0.5
    INTEGRITY_MODERATE_THRESHOLD = 0.7
    
    # Trust thresholds for snapshots
    SNAPSHOT_USABLE_THRESHOLD = 0.7
    SNAPSHOT_HIGH_TRUST_THRESHOLD = 0.85
    
    # Replay safety thresholds
    CLOCK_DRIFT_TOLERANCE_SECONDS = 5.0
    
    def __init__(
        self,
        state_backend: StateBackend,
        invariant_validator: InvariantValidator,
        audit_log_store: AuditLogStore,
        snapshot_store: SnapshotStore,
        metrics_store: MetricsStore
    ):
        """
        Initialize damage assessor.
        
        Args:
            state_backend: Access to current and snapshot state
            invariant_validator: Invariant validation system
            audit_log_store: Audit log access
            snapshot_store: Snapshot repository
            metrics_store: Metrics access
        """
        self.state_backend = state_backend
        self.invariant_validator = invariant_validator
        self.audit_log_store = audit_log_store
        self.snapshot_store = snapshot_store
        self.metrics_store = metrics_store
        
        # Assessment state
        self._evidence_bundle: Optional[EvidenceBundle] = None
        self._subsystem_scans: Dict[SubsystemType, SubsystemDamage] = {}
        self._justification_trail: List[str] = []
    
    def assess(self, failure_event: FailureEvent) -> DamageReport:
        """
        Perform comprehensive damage assessment.
        
        This is the main entry point. Executes all 6 phases in order.
        
        Args:
            failure_event: The triggering failure event
            
        Returns:
            DamageReport with complete assessment
        """
        report_id = f"damage_report_{failure_event.event_id}_{int(time.time())}"
        self._justification_trail = []
        
        # PHASE 1: Evidence Freezing
        self._log_phase("evidence_freezing")
        evidence = self._freeze_evidence(failure_event)
        self._evidence_bundle = evidence
        
        # PHASE 2: Subsystem Integrity Scans
        self._log_phase("subsystem_integrity_scans")
        subsystem_damage = self._scan_all_subsystems(failure_event)
        self._subsystem_scans = subsystem_damage
        
        # PHASE 3: Contamination Graph Construction
        self._log_phase("contamination_graph_construction")
        contamination_graph = self._build_contamination_graph(
            failure_event, 
            subsystem_damage
        )
        
        # PHASE 4: Snapshot Trust Evaluation
        self._log_phase("snapshot_trust_evaluation")
        snapshot_ladder = self._evaluate_snapshot_trust(failure_event)
        
        # PHASE 5: Replay Feasibility Analysis
        self._log_phase("replay_feasibility_analysis")
        replay_safety, replay_notes = self._analyze_replay_feasibility(
            failure_event,
            evidence
        )
        
        # PHASE 6: Damage Synthesis
        self._log_phase("damage_synthesis")
        
        # Compute overall severity (max-risk aggregation)
        severity = self._compute_overall_severity(subsystem_damage)
        
        # Determine blast radius
        blast_radius = self._determine_blast_radius(
            failure_event,
            subsystem_damage,
            contamination_graph
        )
        
        # Decide if rollback required
        rollback_required = self._is_rollback_required(
            severity,
            blast_radius,
            subsystem_damage
        )
        
        # Determine recommended scope
        recommended_scope = blast_radius.scope
        
        # Check forward-fix possibility
        forward_fix_possible, forward_fix_path = self._assess_forward_fix(
            severity,
            replay_safety,
            subsystem_damage
        )
        
        # Determine confidence
        confidence = self._compute_confidence(
            evidence,
            subsystem_damage,
            snapshot_ladder
        )
        
        # Extract corrupted and safe state
        corrupted_state = [
            s.name.value for s in subsystem_damage.values() if s.corrupted
        ]
        safe_state = [
            s.name.value for s in subsystem_damage.values() if not s.corrupted
        ]
        
        # Recommend best snapshot
        recommended_snapshot = None
        if snapshot_ladder:
            best = max(snapshot_ladder, key=lambda s: s.trust_score)
            if best.usable:
                recommended_snapshot = best.snapshot_id
        
        # Determine escalation need
        human_escalation = (
            severity in [DamageSeverity.CRITICAL, DamageSeverity.CATASTROPHIC]
            or not forward_fix_possible
        )
        
        # Determine isolation duration
        isolation_duration = self._compute_isolation_duration(severity)
        
        # Create report
        report = DamageReport(
            report_id=report_id,
            failure_event_id=failure_event.event_id,
            assessed_at=int(time.time()),
            severity=severity,
            blast_radius=blast_radius,
            rollback_required=rollback_required,
            recommended_scope=recommended_scope,
            confidence=confidence,
            corrupted_state=corrupted_state,
            safe_state=safe_state,
            subsystem_damage={s.name.value: s for s in subsystem_damage.values()},
            snapshot_trust_ladder=snapshot_ladder,
            recommended_snapshot_id=recommended_snapshot,
            replay_safety=replay_safety,
            replay_feasibility_notes=replay_notes,
            forward_fix_possible=forward_fix_possible,
            forward_fix_path=forward_fix_path,
            human_escalation_required=human_escalation,
            isolation_duration_seconds=isolation_duration,
            evidence_bundle_hash=evidence.bundle_hash,
            report_signature="",  # Will compute
            justification_trail=self._justification_trail.copy()
        )
        
        # Sign report
        signature = report._compute_signature()
        object.__setattr__(report, 'report_signature', signature)
        
        self._log_justification(f"Final assessment: {severity.value} severity")
        self._log_justification(f"Rollback required: {rollback_required}")
        self._log_justification(f"Recommended scope: {recommended_scope.value}")
        
        return report
    
    def _freeze_evidence(self, event: FailureEvent) -> EvidenceBundle:
        """
        Phase 1: Freeze evidence for deterministic analysis.
        
        Captures:
        - Current state hashes
        - Snapshot hashes
        - Volatile metrics
        - Audit logs
        - Last-good markers
        """
        bundle_id = f"evidence_{event.event_id}_{int(time.time())}"
        
        # Snapshot current state hash
        current_hash = self.state_backend.get_current_state_hash()
        
        # Get last snapshot hash (most recent)
        snapshots = self.snapshot_store.list_snapshots(limit=1)
        last_snapshot_hash = ""
        if snapshots:
            last_snapshot_hash = self.state_backend.get_snapshot_hash(snapshots[0])
        
        # Freeze metrics
        frozen_metrics = self.metrics_store.get_current_metrics()
        
        # Freeze audit logs since failure
        frozen_logs = self.audit_log_store.get_logs_since(event.timestamp)
        
        # Get last good boundary
        last_good = self.audit_log_store.get_last_good_marker()
        
        bundle = EvidenceBundle(
            bundle_id=bundle_id,
            captured_at=int(time.time()),
            current_state_hash=current_hash,
            last_snapshot_hash=last_snapshot_hash,
            frozen_metrics=frozen_metrics,
            frozen_audit_logs=frozen_logs,
            last_good_boundary=last_good
        )
        
        # Hash bundle
        bundle.bundle_hash = bundle.compute_hash()
        
        self._log_justification(f"Evidence frozen: {bundle.bundle_hash[:16]}...")
        
        return bundle
    
    def _scan_all_subsystems(
        self, 
        event: FailureEvent
    ) -> Dict[SubsystemType, SubsystemDamage]:
        """
        Phase 2: Scan all subsystems for damage.
        
        For each subsystem:
        - Hash comparison vs snapshot
        - Invariant validation
        - Temporal gap measurement
        - Partial write detection
        """
        damage_map = {}
        
        for subsystem in SubsystemType:
            damage = self._scan_subsystem(subsystem, event)
            damage_map[subsystem] = damage
            
            self._log_justification(
                f"{subsystem.value}: integrity={damage.integrity_score:.2f}, "
                f"corrupted={damage.corrupted}"
            )
        
        return damage_map
    
    def _scan_subsystem(
        self, 
        subsystem: SubsystemType, 
        event: FailureEvent
    ) -> SubsystemDamage:
        """Scan a single subsystem for damage"""
        # Get current subsystem state
        current_state = self.state_backend.get_subsystem_state(subsystem)
        
        # Validate invariants
        invariants_valid, violations = self.invariant_validator.validate_subsystem(
            subsystem
        )
        
        # Hash comparison (simplified - real impl would compare with snapshot)
        hash_mismatch = len(violations) > 0
        
        # Compute integrity score based on violations
        if not invariants_valid:
            integrity_score = max(0.0, 1.0 - (len(violations) * 0.2))
        else:
            integrity_score = 1.0
        
        # Mark as corrupted if below threshold or has violations
        corrupted = (
            not invariants_valid 
            or integrity_score < self.INTEGRITY_MODERATE_THRESHOLD
        )
        
        # Temporal drift (simplified)
        temporal_drift = 0.0
        if event.timestamp:
            temporal_drift = time.time() - event.timestamp
        
        # Partial writes (check from context)
        partial_writes = event.context.get(f'{subsystem.value}_partial_write', False)
        
        return SubsystemDamage(
            name=subsystem,
            integrity_score=integrity_score,
            corrupted=corrupted,
            hash_mismatch=hash_mismatch,
            invariant_violations=violations,
            temporal_drift_seconds=temporal_drift,
            partial_writes_detected=partial_writes
        )
    
    def _build_contamination_graph(
        self,
        event: FailureEvent,
        subsystem_damage: Dict[SubsystemType, SubsystemDamage]
    ) -> Dict[str, ContaminationNode]:
        """
        Phase 3: Build contamination graph.
        
        Maps failure_origin → affected_module → downstream_state
        
        Used to:
        - Identify safe isolation boundaries
        - Prevent unnecessary rollback
        - Detect cascading damage
        """
        graph = {}
        
        # Determine origin
        origin = event.source
        origin_node = ContaminationNode(
            module=origin,
            contaminated=True,
            contamination_source=None
        )
        graph[origin] = origin_node
        
        # Build dependency graph (simplified - real impl would use actual deps)
        # Contamination flows: persistence → accounts → content
        contamination_flow = {
            'persistence': ['account_system', 'content_pipelines'],
            'account_system': ['content_pipelines', 'enforcement'],
            'infra': ['persistence', 'account_system'],
            'experiments': []  # Isolated
        }
        
        # Propagate contamination
        for subsystem, damage in subsystem_damage.items():
            subsystem_key = subsystem.value
            
            if subsystem_key not in graph:
                graph[subsystem_key] = ContaminationNode(
                    module=subsystem_key,
                    contaminated=damage.corrupted
                )
            
            # Add downstream modules
            if subsystem_key in contamination_flow:
                graph[subsystem_key].downstream_modules = contamination_flow[subsystem_key]
                
                # Mark downstream as contaminated if source is contaminated
                if damage.corrupted:
                    for downstream in contamination_flow[subsystem_key]:
                        if downstream not in graph:
                            graph[downstream] = ContaminationNode(
                                module=downstream,
                                contaminated=True,
                                contamination_source=subsystem_key
                            )
                        else:
                            graph[downstream].contaminated = True
                            graph[downstream].contamination_source = subsystem_key
        
        return graph
    
    def _evaluate_snapshot_trust(
        self, 
        event: FailureEvent
    ) -> List[SnapshotScore]:
        """
        Phase 4: Evaluate trust of candidate snapshots.
        
        Each snapshot scored on:
        - Completeness
        - Schema compatibility
        - Temporal proximity
        - Cross-subsystem alignment
        """
        snapshots = self.snapshot_store.list_snapshots(limit=10)
        trust_ladder = []
        
        for snapshot_id in snapshots:
            score = self._score_snapshot(snapshot_id, event)
            trust_ladder.append(score)
        
        # Sort by trust score (highest first)
        trust_ladder.sort(key=lambda s: s.trust_score, reverse=True)
        
        return trust_ladder
    
    def _score_snapshot(
        self, 
        snapshot_id: str, 
        event: FailureEvent
    ) -> SnapshotScore:
        """Score a single snapshot for trustworthiness"""
        metadata = self.snapshot_store.get_snapshot_metadata(snapshot_id)
        
        # Completeness score (all subsystems present)
        completeness = metadata.get('completeness', 0.9)
        
        # Schema compatibility (can we restore to this?)
        schema_compat = metadata.get('schema_version_compatible', True)
        schema_score = 1.0 if schema_compat else 0.0
        
        # Temporal proximity (how recent is it?)
        snapshot_ts = metadata.get('timestamp', 0)
        age_seconds = event.timestamp - snapshot_ts
        
        # Penalize very old snapshots
        if age_seconds < 3600:  # < 1 hour
            temporal_score = 1.0
        elif age_seconds < 86400:  # < 1 day
            temporal_score = 0.8
        elif age_seconds < 604800:  # < 1 week
            temporal_score = 0.6
        else:
            temporal_score = 0.3
        
        # Cross-subsystem alignment (all subsystems from same point in time)
        alignment_score = metadata.get('alignment_score', 0.95)
        
        # Compute overall trust score (weighted average)
        trust_score = (
            completeness * 0.3 +
            schema_score * 0.3 +
            temporal_score * 0.2 +
            alignment_score * 0.2
        )
        
        # Usable if above threshold and schema compatible
        usable = (
            trust_score >= self.SNAPSHOT_USABLE_THRESHOLD 
            and schema_compat
        )
        
        incompatibility_reasons = []
        if not schema_compat:
            incompatibility_reasons.append("Schema version incompatible")
        if completeness < 0.8:
            incompatibility_reasons.append("Incomplete snapshot")
        
        return SnapshotScore(
            snapshot_id=snapshot_id,
            trust_score=trust_score,
            usable=usable,
            completeness_score=completeness,
            schema_compatibility_score=schema_score,
            temporal_proximity_score=temporal_score,
            cross_subsystem_alignment_score=alignment_score,
            snapshot_timestamp=snapshot_ts,
            age_seconds=age_seconds,
            incompatibility_reasons=incompatibility_reasons
        )
    
    def _analyze_replay_feasibility(
        self,
        event: FailureEvent,
        evidence: EvidenceBundle
    ) -> Tuple[ReplaySafety, str]:
        """
        Phase 5: Analyze if deterministic replay is safe.
        
        Checks:
        - Input completeness
        - Clock monotonicity
        - External side effects
        - Non-deterministic branches
        """
        notes = []
        
        # Check input completeness
        inputs_complete = event.context.get('replay_inputs_complete', False)
        if not inputs_complete:
            notes.append("Replay inputs incomplete")
        
        # Check clock monotonicity
        clock_drift = event.context.get('clock_drift_seconds', 0.0)
        clock_monotonic = abs(clock_drift) < self.CLOCK_DRIFT_TOLERANCE_SECONDS
        if not clock_monotonic:
            notes.append(f"Clock drift: {clock_drift:.2f}s")
        
        # Check for external side effects
        has_external_effects = event.context.get('external_side_effects', False)
        if has_external_effects:
            notes.append("External side effects detected")
        
        # Check for non-deterministic branches
        has_nondeterminism = event.context.get('nondeterministic_branches', False)
        if has_nondeterminism:
            notes.append("Non-deterministic execution paths")
        
        # Determine safety level
        if inputs_complete and clock_monotonic and not has_external_effects and not has_nondeterminism:
            safety = ReplaySafety.REPLAY_SAFE
            notes.append("All replay safety checks passed")
        elif inputs_complete and clock_monotonic:
            safety = ReplaySafety.REPLAY_PARTIAL
            notes.append("Partial replay possible with limitations")
        else:
            safety = ReplaySafety.REPLAY_UNSAFE
            notes.append("Replay not safe - missing prerequisites")
        
        return safety, "; ".join(notes)
    
    def _compute_overall_severity(
        self, 
        subsystem_damage: Dict[SubsystemType, SubsystemDamage]
    ) -> DamageSeverity:
        """
        Compute overall severity using max-risk aggregation.
        
        One corrupted trust ledger trumps 1000 healthy metrics.
        """
        max_severity = DamageSeverity.NONE
        
        for subsystem, damage in subsystem_damage.items():
            subsystem_severity = self._classify_subsystem_severity(
                subsystem, 
                damage
            )
            
            if subsystem_severity > max_severity:
                max_severity = subsystem_severity
                self._log_justification(
                    f"Severity escalated to {max_severity.value} due to "
                    f"{subsystem.value} damage"
                )
        
        return max_severity
    
    def _classify_subsystem_severity(
        self,
        subsystem: SubsystemType,
        damage: SubsystemDamage
    ) -> DamageSeverity:
        """Classify severity for a single subsystem"""
        # Critical subsystems (persistence, account_system) have higher severity
        critical_subsystems = {
            SubsystemType.PERSISTENCE,
            SubsystemType.ACCOUNT_SYSTEM
        }
        
        is_critical = subsystem in critical_subsystems
        
        # Catastrophic conditions
        if damage.corrupted and is_critical and damage.integrity_score < 0.2:
            return DamageSeverity.CATASTROPHIC
        
        if len(damage.invariant_violations) > 5 and is_critical:
            return DamageSeverity.CATASTROPHIC
        
        # Critical conditions
        if damage.integrity_score < self.INTEGRITY_CRITICAL_THRESHOLD:
            return DamageSeverity.CRITICAL if is_critical else DamageSeverity.SEVERE
        
        # Severe conditions
        if damage.integrity_score < self.INTEGRITY_SEVERE_THRESHOLD:
            return DamageSeverity.SEVERE if is_critical else DamageSeverity.MODERATE
        
        # Moderate conditions
        if damage.integrity_score < self.INTEGRITY_MODERATE_THRESHOLD:
            return DamageSeverity.MODERATE
        
        # Minor or none
        if damage.corrupted or len(damage.invariant_violations) > 0:
            return DamageSeverity.MINOR
        
        return DamageSeverity.NONE
    
    def _determine_blast_radius(
        self,
        event: FailureEvent,
        subsystem_damage: Dict[SubsystemType, SubsystemDamage],
        contamination_graph: Dict[str, ContaminationNode]
    ) -> BlastRadius:
        """
        Determine blast radius and required rollback scope.
        
        Tight by default, aggressive only when necessary.
        """
        affected = [
            subsystem for subsystem, damage in subsystem_damage.items()
            if damage.corrupted
        ]
        
        safe = [
            subsystem for subsystem, damage in subsystem_damage.items()
            if not damage.corrupted
        ]
        
        # Detect cascading damage
        cascading = any(
            node.contamination_source is not None
            for node in contamination_graph.values()
            if node.contaminated
        )
        
        # Determine scope based on affected subsystems
        scope = self._classify_rollback_scope(affected, event)
        
        # Find isolation boundaries
        isolation_boundaries = self._find_isolation_boundaries(contamination_graph)
        
        reasoning = self._build_blast_radius_reasoning(affected, safe, scope)
        
        return BlastRadius(
            scope=scope,
            affected_subsystems=affected,
            safe_subsystems=safe,
            isolation_boundaries=isolation_boundaries,
            cascading_damage_detected=cascading,
            reasoning=reasoning
        )
    
    def _classify_rollback_scope(
        self,
        affected: List[SubsystemType],
        event: FailureEvent
    ) -> RollbackScope:
        """
        Classify required rollback scope based on blast radius.
        
        Rules:
        - Single experiment divergence → EXPERIMENT_ONLY
        - Persistence + trust ledger → FULL_SYSTEM
        - Enforcement flags only → PARTIAL
        - Metrics-only anomaly → NO_ROLLBACK
        """
        # No corruption detected
        if not affected:
            self._log_justification("No corrupted subsystems - no rollback needed")
            return RollbackScope.NO_ROLLBACK
        
        # Only experiments affected
        if affected == [SubsystemType.EXPERIMENTS]:
            self._log_justification("Only experiments affected - scoped rollback")
            return RollbackScope.EXPERIMENT_ONLY
        
        # Persistence or account system corrupted → FULL_SYSTEM
        critical_systems = {SubsystemType.PERSISTENCE, SubsystemType.ACCOUNT_SYSTEM}
        if any(s in critical_systems for s in affected):
            self._log_justification(
                "Critical subsystem corrupted (persistence/accounts) - full rollback required"
            )
            return RollbackScope.FULL_SYSTEM
        
        # Only enforcement affected
        if affected == [SubsystemType.ENFORCEMENT]:
            self._log_justification("Only enforcement affected - partial rollback")
            return RollbackScope.PARTIAL
        
        # Multiple non-critical subsystems
        if len(affected) >= 3:
            self._log_justification(
                f"{len(affected)} subsystems affected - full rollback safer"
            )
            return RollbackScope.FULL_SYSTEM
        
        # Default to partial for 1-2 non-critical subsystems
        self._log_justification(
            f"{len(affected)} subsystems affected - partial rollback"
        )
        return RollbackScope.PARTIAL
    
    def _find_isolation_boundaries(
        self,
        contamination_graph: Dict[str, ContaminationNode]
    ) -> List[str]:
        """Find safe isolation boundaries in contamination graph"""
        boundaries = []
        
        for module, node in contamination_graph.items():
            # If this module is safe but has contaminated upstream
            if not node.contaminated and node.contamination_source is None:
                # Check if any downstream is contaminated
                has_contaminated_downstream = any(
                    contamination_graph.get(downstream, ContaminationNode(downstream, False)).contaminated
                    for downstream in node.downstream_modules
                )
                
                if has_contaminated_downstream:
                    boundaries.append(module)
        
        return boundaries
    
    def _build_blast_radius_reasoning(
        self,
        affected: List[SubsystemType],
        safe: List[SubsystemType],
        scope: RollbackScope
    ) -> str:
        """Build human-readable reasoning for blast radius determination"""
        affected_names = [s.value for s in affected]
        safe_names = [s.value for s in safe]
        
        reasoning = (
            f"Blast radius analysis: {len(affected)} subsystems corrupted "
            f"({', '.join(affected_names) if affected_names else 'none'}), "
            f"{len(safe)} subsystems safe "
            f"({', '.join(safe_names) if safe_names else 'none'}). "
            f"Recommended scope: {scope.value}"
        )
        
        return reasoning
    
    def _is_rollback_required(
        self,
        severity: DamageSeverity,
        blast_radius: BlastRadius,
        subsystem_damage: Dict[SubsystemType, SubsystemDamage]
    ) -> bool:
        """
        Determine if rollback is required.
        
        Always required for:
        - CRITICAL or CATASTROPHIC severity
        - Persistence corruption
        - Account system corruption
        """
        # Always rollback for catastrophic/critical
        if severity in [DamageSeverity.CATASTROPHIC, DamageSeverity.CRITICAL]:
            self._log_justification(
                f"Rollback required due to {severity.value} severity"
            )
            return True
        
        # Always rollback if critical subsystems corrupted
        critical_corrupted = any(
            damage.corrupted and subsystem in {
                SubsystemType.PERSISTENCE, 
                SubsystemType.ACCOUNT_SYSTEM
            }
            for subsystem, damage in subsystem_damage.items()
        )
        
        if critical_corrupted:
            self._log_justification(
                "Rollback required due to critical subsystem corruption"
            )
            return True
        
        # No rollback for NONE severity
        if severity == DamageSeverity.NONE:
            return False
        
        # Rollback for SEVERE or worse
        if severity >= DamageSeverity.SEVERE:
            self._log_justification(
                f"Rollback required due to {severity.value} severity"
            )
            return True
        
        # No rollback needed for MINOR/MODERATE if no critical systems affected
        return False
    
    def _assess_forward_fix(
        self,
        severity: DamageSeverity,
        replay_safety: ReplaySafety,
        subsystem_damage: Dict[SubsystemType, SubsystemDamage]
    ) -> Tuple[bool, Optional[str]]:
        """
        Assess if forward-fix is possible instead of rollback.
        
        Cannot forward-fix if:
        - Replay unsafe
        - CATASTROPHIC severity
        - State corruption detected
        """
        # Never forward-fix catastrophic damage
        if severity == DamageSeverity.CATASTROPHIC:
            return False, None
        
        # Cannot forward-fix if replay unsafe
        if replay_safety == ReplaySafety.REPLAY_UNSAFE:
            return False, None
        
        # Cannot forward-fix if state corrupted
        has_corruption = any(
            damage.corrupted and damage.integrity_score < 0.5
            for damage in subsystem_damage.values()
        )
        
        if has_corruption:
            return False, None
        
        # Forward-fix possible for MINOR/MODERATE with safe replay
        if severity in [DamageSeverity.MINOR, DamageSeverity.MODERATE]:
            if replay_safety == ReplaySafety.REPLAY_SAFE:
                return True, "replay_with_correction"
        
        # Partial forward-fix possible
        if severity == DamageSeverity.SEVERE:
            if replay_safety == ReplaySafety.REPLAY_PARTIAL:
                return True, "partial_replay_with_manual_validation"
        
        return False, None
    
    def _compute_confidence(
        self,
        evidence: EvidenceBundle,
        subsystem_damage: Dict[SubsystemType, SubsystemDamage],
        snapshot_ladder: List[SnapshotScore]
    ) -> float:
        """
        Compute confidence in damage assessment.
        
        Based on:
        - Evidence completeness
        - Subsystem scan coverage
        - Snapshot availability
        """
        # Evidence quality (0.0 - 0.4)
        evidence_score = 0.4 if evidence.bundle_hash else 0.2
        
        # Subsystem coverage (0.0 - 0.3)
        scanned_count = len(subsystem_damage)
        total_subsystems = len(SubsystemType)
        coverage_score = (scanned_count / total_subsystems) * 0.3
        
        # Snapshot availability (0.0 - 0.3)
        usable_snapshots = sum(1 for s in snapshot_ladder if s.usable)
        snapshot_score = min(usable_snapshots / 3.0, 1.0) * 0.3
        
        confidence = evidence_score + coverage_score + snapshot_score
        
        return min(1.0, confidence)
    
    def _compute_isolation_duration(self, severity: DamageSeverity) -> Optional[int]:
        """Compute required isolation duration in seconds"""
        duration_map = {
            DamageSeverity.NONE: None,
            DamageSeverity.MINOR: 300,        # 5 minutes
            DamageSeverity.MODERATE: 900,     # 15 minutes
            DamageSeverity.SEVERE: 3600,      # 1 hour
            DamageSeverity.CRITICAL: 7200,    # 2 hours
            DamageSeverity.CATASTROPHIC: None # Indefinite (manual)
        }
        
        return duration_map.get(severity)
    
    def _log_phase(self, phase: str):
        """Log assessment phase"""
        self._justification_trail.append(f"=== Phase: {phase} ===")
    
    def _log_justification(self, message: str):
        """Log justification for audit trail"""
        self._justification_trail.append(message)


# Example Usage
if __name__ == "__main__":
    # Mock implementations for demonstration
    
    class MockStateBackend(StateBackend):
        def get_current_state_hash(self) -> str:
            return hashlib.sha256(b"current_state").hexdigest()
        
        def get_snapshot_hash(self, snapshot_id: str) -> str:
            return hashlib.sha256(snapshot_id.encode()).hexdigest()
        
        def get_subsystem_state(self, subsystem: SubsystemType) -> Dict:
            return {"subsystem": subsystem.value, "healthy": True}
    
    class MockInvariantValidator(InvariantValidator):
        def validate_subsystem(self, subsystem: SubsystemType) -> Tuple[bool, List[str]]:
            # Simulate persistence corruption
            if subsystem == SubsystemType.PERSISTENCE:
                return False, ["state_hash_mismatch", "partial_write_detected"]
            return True, []
    
    class MockAuditLogStore(AuditLogStore):
        def get_logs_since(self, timestamp: int) -> List[Dict]:
            return [
                {"event": "write_started", "timestamp": timestamp},
                {"event": "write_failed", "timestamp": timestamp + 10}
            ]
        
        def get_last_good_marker(self) -> Optional[str]:
            return "checkpoint_20260128_090000"
    
    class MockSnapshotStore(SnapshotStore):
        def list_snapshots(self, limit: int = 10) -> List[str]:
            return ["snap_001", "snap_002", "snap_003"]
        
        def get_snapshot_metadata(self, snapshot_id: str) -> Dict:
            return {
                "timestamp": int(time.time()) - 3600,  # 1 hour ago
                "completeness": 0.95,
                "schema_version_compatible": True,
                "alignment_score": 0.98
            }
        
        def get_snapshot(self, snapshot_id: str) -> Dict:
            return {"version": "1.0", "data": {}}
    
    class MockMetricsStore(MetricsStore):
        def get_current_metrics(self) -> Dict:
            return {
                "cpu_usage": 0.45,
                "memory_usage": 0.67,
                "error_rate": 0.02
            }
        
        def get_metric_deltas(self, since: int) -> Dict:
            return {
                "error_rate_delta": 0.015,
                "latency_spike": True
            }
    
    # Initialize assessor
    assessor = DamageAssessor(
        state_backend=MockStateBackend(),
        invariant_validator=MockInvariantValidator(),
        audit_log_store=MockAuditLogStore(),
        snapshot_store=MockSnapshotStore(),
        metrics_store=MockMetricsStore()
    )
    
    # Create failure event (simulating persistence corruption)
    event = FailureEvent(
        event_id="failure_001",
        trigger_type=TriggerType.INVARIANT,
        timestamp=int(time.time()),
        correlation_id="corr_123",
        source="persistence",
        description="State hash mismatch detected during write",
        context={
            "persistence_partial_write": True,
            "replay_inputs_complete": True,
            "clock_drift_seconds": 0.5,
            "external_side_effects": False,
            "nondeterministic_branches": False
        }
    )
    
    # Perform damage assessment
    report = assessor.assess(event)
    
    print("=" * 80)
    print("DAMAGE ASSESSMENT REPORT")
    print("=" * 80)
    print(f"Report ID: {report.report_id}")
    print(f"Failure Event: {report.failure_event_id}")
    print(f"Assessed At: {report.assessed_at}")
    print()
    
    print(f"SEVERITY: {report.severity.value.upper()}")
    print(f"Confidence: {report.confidence * 100:.1f}%")
    print()
    
    print(f"BLAST RADIUS:")
    print(f"  Scope: {report.blast_radius.scope.value}")
    print(f"  Affected Subsystems: {len(report.blast_radius.affected_subsystems)}")
    for subsystem in report.blast_radius.affected_subsystems:
        print(f"    - {subsystem.value}")
    print(f"  Safe Subsystems: {len(report.blast_radius.safe_subsystems)}")
    for subsystem in report.blast_radius.safe_subsystems:
        print(f"    - {subsystem.value}")
    print(f"  Cascading Damage: {report.blast_radius.cascading_damage_detected}")
    print(f"  Reasoning: {report.blast_radius.reasoning}")
    print()
    
    print(f"ROLLBACK ASSESSMENT:")
    print(f"  Required: {report.rollback_required}")
    print(f"  Recommended Scope: {report.recommended_scope.value}")
    print(f"  Recommended Snapshot: {report.recommended_snapshot_id}")
    print()
    
    print(f"REPLAY ASSESSMENT:")
    print(f"  Safety: {report.replay_safety.value}")
    print(f"  Notes: {report.replay_feasibility_notes}")
    print()
    
    print(f"FORWARD-FIX ASSESSMENT:")
    print(f"  Possible: {report.forward_fix_possible}")
    if report.forward_fix_path:
        print(f"  Path: {report.forward_fix_path}")
    print()
    
    print(f"ESCALATION:")
    print(f"  Human Required: {report.human_escalation_required}")
    if report.isolation_duration_seconds:
        print(f"  Isolation Duration: {report.isolation_duration_seconds}s ({report.isolation_duration_seconds / 60:.1f} min)")
    print()
    
    print(f"SUBSYSTEM DAMAGE DETAILS:")
    for subsystem_name, damage in report.subsystem_damage.items():
        print(f"  {subsystem_name}:")
        print(f"    Integrity Score: {damage.integrity_score:.2f}")
        print(f"    Corrupted: {damage.corrupted}")
        if damage.invariant_violations:
            print(f"    Violations: {', '.join(damage.invariant_violations)}")
        print(f"    Temporal Drift: {damage.temporal_drift_seconds:.2f}s")
    print()
    
    print(f"SNAPSHOT TRUST LADDER:")
    for i, snapshot in enumerate(report.snapshot_trust_ladder[:3], 1):
        print(f"  #{i} {snapshot.snapshot_id}:")
        print(f"    Trust Score: {snapshot.trust_score:.2f}")
        print(f"    Usable: {snapshot.usable}")
        print(f"    Age: {snapshot.age_seconds / 60:.1f} minutes")
    print()
    
    print(f"AUDIT:")
    print(f"  Evidence Hash: {report.evidence_bundle_hash[:16]}...")
    print(f"  Report Signature: {report.report_signature[:16]}...")
    print(f"  Signature Valid: {report.verify_signature()}")
    print()
    
    print(f"JUSTIFICATION TRAIL:")
    for justification in report.justification_trail:
        print(f"  {justification}")
    print()
    
    print("=" * 80)