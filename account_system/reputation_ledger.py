"""
reputation_ledger.py

Longitudinal Trust, Penalty & Recovery Memory

Core principle:
    Trust emerges from time-consistent behavior + clean recovery from damage.

This file is the immutable memory of account history. It does NOT:
    - Compute trust scores
    - Assess risk
    - Interpret behavior
    - Control posting

It answers ONE thing:
    "What has happened to this account, over time, in a way that 
     platforms cannot erase or reinterpret?"

Think of it as:
    A blockchain-like journal, deterministic, local, auditable,
    and optimized for causal learning.

Big accounts are trusted because they:
    - Survive volatility
    - Recover without overreaction
    - Do not repeat violations
    - Stabilize after disruptions

This file tracks exactly that.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional, Set
from enum import Enum
import hashlib
import json
from collections import defaultdict
import statistics


class EventType(Enum):
    """Trust-relevant event categories"""
    PENALTY = "penalty"
    WARNING = "warning"
    RECOVERY = "recovery"
    CLEAN_PERIOD = "clean_period"
    RESTRICTION = "restriction"
    RATE_LIMIT = "rate_limit"
    VOLUNTARY_THROTTLE = "voluntary_throttle"
    EXPERIMENT_ISOLATION = "experiment_isolation"
    BEHAVIOR_NORMALIZATION = "behavior_normalization"
    PLATFORM_REGIME_SHIFT = "platform_regime_shift"


class StabilityTrend(Enum):
    """Account stability trajectory"""
    IMPROVING = "improving"
    STABLE = "stable"
    DEGRADING = "degrading"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReputationEvent:
    """
    Immutable event record.
    
    Events are FACTS, not opinions.
    Once written, never modified.
    """
    timestamp: datetime
    event_type: EventType
    severity: float  # normalized [0, 1]
    source: str  # risk_signal, enforcement_monitor, system, etc
    metadata: Dict
    
    def __post_init__(self):
        """Validate event integrity"""
        if not 0.0 <= self.severity <= 1.0:
            raise ValueError(f"Severity must be [0,1], got {self.severity}")
        if not self.source:
            raise ValueError("Event source cannot be empty")


@dataclass(frozen=True)
class LedgerHealth:
    """Current health snapshot of the ledger"""
    penalty_frequency: float  # [0, 1] - how often penalties occur
    recovery_quality: float  # [0, 1] - how cleanly account recovers
    stability_trend: StabilityTrend
    trust_momentum: float  # rate of trust improvement
    behavioral_elasticity: float  # how much behavior snaps back after stress
    clean_period_days: int  # consecutive clean days
    
    # Advanced metrics
    penalty_similarity_index: float  # are penalties repeating?
    recovery_speed_avg: float  # average time to recover (hours)
    volatility_damping: float  # is variance decreasing?


@dataclass(frozen=True)
class LedgerSnapshot:
    """
    Immutable snapshot of ledger state.
    
    This is how downstream systems consume history safely.
    No mutations, no deletions, no rewrites.
    """
    account_id: str
    platform: str
    
    # Immutable event history
    ledger_entries: Tuple[ReputationEvent, ...]
    
    # Computed health metrics
    ledger_health: LedgerHealth
    
    # Versioning & integrity
    ledger_version: str
    ledger_hash: str
    snapshot_timestamp: datetime
    
    # Forensic metadata
    total_events: int
    first_event_timestamp: Optional[datetime]
    last_event_timestamp: Optional[datetime]


class PenaltyTracker:
    """
    Tracks penalty patterns over time.
    
    One penalty ≠ bad
    Repeated identical penalties = bad
    
    Big accounts sometimes get penalties. They don't repeat the same mistake.
    """
    
    def __init__(self):
        self.penalty_windows: List[Tuple[datetime, float]] = []
        self.penalty_types: Dict[str, int] = defaultdict(int)
        
    def add_penalty(self, event: ReputationEvent):
        """Record a penalty event"""
        self.penalty_windows.append((event.timestamp, event.severity))
        
        # Track penalty type for similarity analysis
        penalty_type = event.metadata.get("penalty_type", "unknown")
        self.penalty_types[penalty_type] += 1
    
    def compute_frequency(self, window_days: int = 30) -> float:
        """
        Compute penalty frequency over recent window.
        
        Returns [0, 1] where:
            0 = no penalties
            1 = constant penalties
        """
        if not self.penalty_windows:
            return 0.0
        
        cutoff = datetime.now() - timedelta(days=window_days)
        recent = [s for t, s in self.penalty_windows if t >= cutoff]
        
        if not recent:
            return 0.0
        
        # Normalize by window size and severity
        avg_severity = statistics.mean(recent)
        frequency_factor = len(recent) / window_days
        
        return min(1.0, avg_severity * frequency_factor * 10)
    
    def compute_clustering(self) -> float:
        """
        Detect if penalties are clustered vs spread out.
        
        Clustered = bad (cascading failures)
        Spread = normal variance
        """
        if len(self.penalty_windows) < 2:
            return 0.0
        
        timestamps = [t for t, _ in self.penalty_windows[-10:]]
        if len(timestamps) < 2:
            return 0.0
        
        # Compute time gaps between penalties
        gaps = [(timestamps[i+1] - timestamps[i]).total_seconds() 
                for i in range(len(timestamps) - 1)]
        
        if not gaps:
            return 0.0
        
        # High variance in gaps = low clustering (good)
        # Low variance = high clustering (bad)
        variance = statistics.variance(gaps) if len(gaps) > 1 else 0
        avg_gap = statistics.mean(gaps)
        
        if avg_gap == 0:
            return 1.0
        
        cv = (variance ** 0.5) / avg_gap  # coefficient of variation
        return max(0.0, 1.0 - min(1.0, cv / 2.0))
    
    def compute_similarity_index(self) -> float:
        """
        Are penalties repeating the same mistakes?
        
        Returns [0, 1] where:
            0 = all unique penalties
            1 = same penalty repeated
        """
        if not self.penalty_types:
            return 0.0
        
        total = sum(self.penalty_types.values())
        max_count = max(self.penalty_types.values())
        
        # If one type dominates, similarity is high (bad)
        return max_count / total


class RecoveryTracker:
    """
    Tracks recovery quality after penalties.
    
    Clean recovery increases trust FASTER than never being penalized.
    This is one of your biggest levers.
    """
    
    def __init__(self):
        self.recovery_events: List[Tuple[datetime, float]] = []
        self.penalty_recovery_pairs: List[Tuple[datetime, datetime, float]] = []
        
    def add_recovery(self, event: ReputationEvent, prior_penalty_time: Optional[datetime] = None):
        """Record a successful recovery event"""
        recovery_quality = 1.0 - event.severity  # inverse of remaining damage
        self.recovery_events.append((event.timestamp, recovery_quality))
        
        if prior_penalty_time:
            self.penalty_recovery_pairs.append(
                (prior_penalty_time, event.timestamp, recovery_quality)
            )
    
    def compute_recovery_quality(self) -> float:
        """
        How cleanly does the account recover from penalties?
        
        Measured by:
            - Time to stabilization
            - Behavior normalization
            - Volatility dampening
            - Absence of retaliation/bursting
        """
        if not self.recovery_events:
            return 0.5  # neutral - no data
        
        recent_recoveries = self.recovery_events[-5:]
        qualities = [q for _, q in recent_recoveries]
        
        return statistics.mean(qualities) if qualities else 0.5
    
    def compute_recovery_speed(self) -> float:
        """
        Average time to recover from penalties (in hours).
        
        Faster = better (but not too fast, which looks artificial)
        """
        if not self.penalty_recovery_pairs:
            return 0.0
        
        recovery_times = [
            (recovery_time - penalty_time).total_seconds() / 3600
            for penalty_time, recovery_time, _ in self.penalty_recovery_pairs[-10:]
        ]
        
        return statistics.mean(recovery_times) if recovery_times else 0.0
    
    def compute_behavioral_elasticity(self) -> float:
        """
        How much does behavior snap back after stress?
        
        High elasticity = resilient account
        Low elasticity = permanent damage or overreaction
        """
        if len(self.recovery_events) < 3:
            return 0.5
        
        # Compare recovery qualities over time
        recent = [q for _, q in self.recovery_events[-5:]]
        older = [q for _, q in self.recovery_events[-10:-5]] if len(self.recovery_events) > 5 else recent
        
        if not older or not recent:
            return 0.5
        
        # Improving recovery quality = high elasticity
        avg_recent = statistics.mean(recent)
        avg_older = statistics.mean(older)
        
        improvement = (avg_recent - avg_older) + 0.5
        return max(0.0, min(1.0, improvement))


class StabilityTrendAnalyzer:
    """
    Tracks variance decay and behavior smoothness over time.
    
    Used heavily by:
        - trust_scoring
        - posting rate governors
        - rollout eligibility
    """
    
    def __init__(self):
        self.variance_history: List[Tuple[datetime, float]] = []
        
    def add_variance_sample(self, timestamp: datetime, variance: float):
        """Record variance measurement"""
        self.variance_history.append((timestamp, variance))
        
    def compute_trend(self) -> StabilityTrend:
        """
        Determine if account is improving, stable, or degrading.
        """
        if len(self.variance_history) < 5:
            return StabilityTrend.UNKNOWN
        
        recent = [v for _, v in self.variance_history[-10:]]
        
        # Compute trend via linear regression slope
        n = len(recent)
        x = list(range(n))
        y = recent
        
        x_mean = statistics.mean(x)
        y_mean = statistics.mean(y)
        
        numerator = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(n))
        denominator = sum((x[i] - x_mean) ** 2 for i in range(n))
        
        if denominator == 0:
            return StabilityTrend.STABLE
        
        slope = numerator / denominator
        
        # Classify based on slope and recent variance
        avg_variance = statistics.mean(recent)
        
        if slope < -0.01 and avg_variance < 0.3:
            return StabilityTrend.IMPROVING
        elif slope > 0.01 or avg_variance > 0.6:
            return StabilityTrend.DEGRADING
        elif avg_variance > 0.4:
            return StabilityTrend.VOLATILE
        else:
            return StabilityTrend.STABLE
    
    def compute_volatility_damping(self) -> float:
        """
        Is variance decreasing over time?
        
        Returns [0, 1] where:
            1 = strong damping (good)
            0 = increasing volatility (bad)
        """
        if len(self.variance_history) < 5:
            return 0.5
        
        recent = [v for _, v in self.variance_history[-5:]]
        older = [v for _, v in self.variance_history[-10:-5]] if len(self.variance_history) > 5 else recent
        
        avg_recent = statistics.mean(recent)
        avg_older = statistics.mean(older)
        
        if avg_older == 0:
            return 0.5
        
        damping = 1.0 - (avg_recent / avg_older)
        return max(0.0, min(1.0, damping))


class LedgerHasher:
    """
    Tamper-evident hash chaining.
    
    ledger_hash = SHA256(previous_hash + new_event)
    
    Used for:
        - Tamper detection
        - Forensic replay
        - Cross-system consistency
    """
    
    def __init__(self, genesis_hash: str = "genesis"):
        self.current_hash = genesis_hash
        
    def hash_event(self, event: ReputationEvent) -> str:
        """Compute hash for a new event"""
        event_data = json.dumps({
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type.value,
            "severity": event.severity,
            "source": event.source,
            "metadata": event.metadata
        }, sort_keys=True)
        
        combined = f"{self.current_hash}{event_data}"
        new_hash = hashlib.sha256(combined.encode()).hexdigest()
        
        self.current_hash = new_hash
        return new_hash
    
    def verify_chain(self, events: List[ReputationEvent], expected_hash: str) -> bool:
        """Verify ledger integrity"""
        temp_hasher = LedgerHasher()
        
        for event in events:
            temp_hasher.hash_event(event)
        
        return temp_hasher.current_hash == expected_hash


class LedgerValidator:
    """
    Validates ledger consistency and detects anomalies.
    """
    
    @staticmethod
    def validate_snapshot(snapshot: LedgerSnapshot) -> Tuple[bool, List[str]]:
        """
        Validate snapshot integrity.
        
        Returns (is_valid, list_of_issues)
        """
        issues = []
        
        # Check event ordering
        for i in range(len(snapshot.ledger_entries) - 1):
            if snapshot.ledger_entries[i].timestamp > snapshot.ledger_entries[i+1].timestamp:
                issues.append(f"Event {i} out of chronological order")
        
        # Check hash integrity
        hasher = LedgerHasher()
        for event in snapshot.ledger_entries:
            hasher.hash_event(event)
        
        if hasher.current_hash != snapshot.ledger_hash:
            issues.append("Hash chain integrity violation")
        
        # Check health metrics bounds
        health = snapshot.ledger_health
        if not (0.0 <= health.penalty_frequency <= 1.0):
            issues.append(f"Invalid penalty_frequency: {health.penalty_frequency}")
        if not (0.0 <= health.recovery_quality <= 1.0):
            issues.append(f"Invalid recovery_quality: {health.recovery_quality}")
        
        return (len(issues) == 0, issues)


class LedgerWatchdog:
    """
    Alerts on concerning patterns.
    
    Can trigger:
        - Posting slowdowns
        - Experiment freezes
        - Trust recalculation
    """
    
    def __init__(self):
        self.alert_thresholds = {
            "penalty_cascade": 3,  # 3 penalties in 24 hours
            "recovery_regression": 0.3,  # recovery quality drops below 0.3
            "missing_clean_period": 7,  # no clean period logged in 7 days
        }
        
    def check_penalty_cascade(self, events: List[ReputationEvent]) -> Optional[str]:
        """Detect penalty cascades"""
        cutoff = datetime.now() - timedelta(hours=24)
        recent_penalties = [
            e for e in events 
            if e.event_type == EventType.PENALTY and e.timestamp >= cutoff
        ]
        
        if len(recent_penalties) >= self.alert_thresholds["penalty_cascade"]:
            return f"ALERT: Penalty cascade detected ({len(recent_penalties)} in 24h)"
        
        return None
    
    def check_recovery_regression(self, health: LedgerHealth) -> Optional[str]:
        """Detect recovery quality degradation"""
        if health.recovery_quality < self.alert_thresholds["recovery_regression"]:
            return f"ALERT: Recovery regression (quality={health.recovery_quality:.2f})"
        
        return None
    
    def check_missing_clean_periods(self, events: List[ReputationEvent]) -> Optional[str]:
        """Detect missing clean period events"""
        cutoff = datetime.now() - timedelta(days=self.alert_thresholds["missing_clean_period"])
        recent_clean = [
            e for e in events
            if e.event_type == EventType.CLEAN_PERIOD and e.timestamp >= cutoff
        ]
        
        if not recent_clean:
            return f"ALERT: No clean period logged in {self.alert_thresholds['missing_clean_period']} days"
        
        return None
    
    def run_checks(self, snapshot: LedgerSnapshot) -> List[str]:
        """Run all watchdog checks"""
        alerts = []
        
        alert = self.check_penalty_cascade(list(snapshot.ledger_entries))
        if alert:
            alerts.append(alert)
        
        alert = self.check_recovery_regression(snapshot.ledger_health)
        if alert:
            alerts.append(alert)
        
        alert = self.check_missing_clean_periods(list(snapshot.ledger_entries))
        if alert:
            alerts.append(alert)
        
        return alerts


class ReputationLedger:
    """
    Main ledger interface.
    
    Append-only, hash-chained, tamper-evident event log.
    
    This is legally defensible history.
    """
    
    def __init__(self, account_id: str, platform: str):
        self.account_id = account_id
        self.platform = platform
        
        # Event storage (append-only)
        self._events: List[ReputationEvent] = []
        
        # Trackers
        self.penalty_tracker = PenaltyTracker()
        self.recovery_tracker = RecoveryTracker()
        self.stability_analyzer = StabilityTrendAnalyzer()
        
        # Integrity
        self.hasher = LedgerHasher()
        self.validator = LedgerValidator()
        self.watchdog = LedgerWatchdog()
        
        # Versioning
        self.version = "1.0.0"
        
    def append_event(self, event: ReputationEvent) -> str:
        """
        Append new event to ledger.
        
        Returns: event hash
        """
        # Validate event
        if self._events and event.timestamp < self._events[-1].timestamp:
            raise ValueError("Events must be appended in chronological order")
        
        # Store event
        self._events.append(event)
        
        # Update trackers
        if event.event_type == EventType.PENALTY:
            self.penalty_tracker.add_penalty(event)
        elif event.event_type == EventType.RECOVERY:
            # Find most recent penalty
            recent_penalties = [
                e for e in reversed(self._events[:-1])
                if e.event_type == EventType.PENALTY
            ]
            prior_penalty_time = recent_penalties[0].timestamp if recent_penalties else None
            self.recovery_tracker.add_recovery(event, prior_penalty_time)
        
        # Compute and store variance if available
        if "behavior_variance" in event.metadata:
            self.stability_analyzer.add_variance_sample(
                event.timestamp,
                event.metadata["behavior_variance"]
            )
        
        # Hash event
        event_hash = self.hasher.hash_event(event)
        
        return event_hash
    
    def snapshot(self) -> LedgerSnapshot:
        """
        Create immutable snapshot of current ledger state.
        """
        # Compute health metrics
        penalty_freq = self.penalty_tracker.compute_frequency()
        recovery_qual = self.recovery_tracker.compute_recovery_quality()
        stability = self.stability_analyzer.compute_trend()
        
        # Advanced metrics
        trust_momentum = self._compute_trust_momentum()
        behavioral_elasticity = self.recovery_tracker.compute_behavioral_elasticity()
        clean_days = self._compute_clean_period_days()
        penalty_similarity = self.penalty_tracker.compute_similarity_index()
        recovery_speed = self.recovery_tracker.compute_recovery_speed()
        volatility_damp = self.stability_analyzer.compute_volatility_damping()
        
        health = LedgerHealth(
            penalty_frequency=penalty_freq,
            recovery_quality=recovery_qual,
            stability_trend=stability,
            trust_momentum=trust_momentum,
            behavioral_elasticity=behavioral_elasticity,
            clean_period_days=clean_days,
            penalty_similarity_index=penalty_similarity,
            recovery_speed_avg=recovery_speed,
            volatility_damping=volatility_damp
        )
        
        snapshot = LedgerSnapshot(
            account_id=self.account_id,
            platform=self.platform,
            ledger_entries=tuple(self._events),
            ledger_health=health,
            ledger_version=self.version,
            ledger_hash=self.hasher.current_hash,
            snapshot_timestamp=datetime.now(),
            total_events=len(self._events),
            first_event_timestamp=self._events[0].timestamp if self._events else None,
            last_event_timestamp=self._events[-1].timestamp if self._events else None
        )
        
        # Validate before returning
        is_valid, issues = self.validator.validate_snapshot(snapshot)
        if not is_valid:
            raise RuntimeError(f"Snapshot validation failed: {issues}")
        
        return snapshot
    
    def replay(self, from_timestamp: Optional[datetime] = None) -> List[ReputationEvent]:
        """
        Forensic replay of events.
        
        Used for:
            - Debugging trust calculations
            - Auditing decisions
            - Proving account history
        """
        if from_timestamp is None:
            return list(self._events)
        
        return [e for e in self._events if e.timestamp >= from_timestamp]
    
    def _compute_trust_momentum(self) -> float:
        """
        Rate of trust improvement, not just level.
        
        Returns [-1, 1] where:
            1 = rapidly improving
            0 = stable
            -1 = rapidly degrading
        """
        if len(self._events) < 10:
            return 0.0
        
        # Look at recent vs older penalty/recovery ratio
        recent_events = self._events[-20:]
        older_events = self._events[-40:-20] if len(self._events) > 40 else recent_events
        
        def score_events(events):
            penalties = sum(1 for e in events if e.event_type == EventType.PENALTY)
            recoveries = sum(1 for e in events if e.event_type == EventType.RECOVERY)
            clean = sum(1 for e in events if e.event_type == EventType.CLEAN_PERIOD)
            return (recoveries + clean - penalties * 2) / max(len(events), 1)
        
        recent_score = score_events(recent_events)
        older_score = score_events(older_events)
        
        momentum = recent_score - older_score
        return max(-1.0, min(1.0, momentum * 2))
    
    def _compute_clean_period_days(self) -> int:
        """
        Consecutive days without penalties.
        
        Clean-time accumulation is an event.
        Big accounts are trusted largely because they survive long clean periods.
        """
        if not self._events:
            return 0
        
        # Find most recent penalty
        penalties = [e for e in reversed(self._events) if e.event_type == EventType.PENALTY]
        
        if not penalties:
            # No penalties ever - use first event timestamp
            first_event = self._events[0].timestamp
            return (datetime.now() - first_event).days
        
        most_recent_penalty = penalties[0].timestamp
        return (datetime.now() - most_recent_penalty).days


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

def example_usage():
    """Demonstration of ledger usage"""
    
    # Initialize ledger
    ledger = ReputationLedger(
        account_id="acc_12345",
        platform="instagram"
    )
    
    # Record initial clean period
    ledger.append_event(ReputationEvent(
        timestamp=datetime.now() - timedelta(days=30),
        event_type=EventType.CLEAN_PERIOD,
        severity=0.0,
        source="system",
        metadata={"days": 30}
    ))
    
    # Record a penalty
    ledger.append_event(ReputationEvent(
        timestamp=datetime.now() - timedelta(days=7),
        event_type=EventType.PENALTY,
        severity=0.3,
        source="enforcement_monitor",
        metadata={
            "penalty_type": "rate_limit_violation",
            "duration_hours": 24
        }
    ))
    
    # Record recovery
    ledger.append_event(ReputationEvent(
        timestamp=datetime.now() - timedelta(days=5),
        event_type=EventType.RECOVERY,
        severity=0.1,  # low residual damage
        source="system",
        metadata={
            "behavior_normalized": True,
            "behavior_variance": 0.15
        }
    ))
    
    # Record voluntary throttling (POWERFUL SIGNAL)
    ledger.append_event(ReputationEvent(
        timestamp=datetime.now() - timedelta(days=2),
        event_type=EventType.VOLUNTARY_THROTTLE,
        severity=0.0,
        source="posting_governor",
        metadata={
            "throttle_reason": "risk_elevation",
            "reduction_factor": 0.5
        }
    ))
    
    # Create snapshot
    snapshot = ledger.snapshot()
    
    # Run watchdog checks
    alerts = ledger.watchdog.run_checks(snapshot)
    
    # Output results
    print(f"Account: {snapshot.account_id}")
    print(f"Platform: {snapshot.platform}")
    print(f"Total events: {snapshot.total_events}")
    print(f"\nLedger Health:")
    print(f"  Penalty frequency: {snapshot.ledger_health.penalty_frequency:.3f}")
    print(f"  Recovery quality: {snapshot.ledger_health.recovery_quality:.3f}")
    print(f"  Stability trend: {snapshot.ledger_health.stability_trend.value}")
    print(f"  Trust momentum: {snapshot.ledger_health.trust_momentum:.3f}")
    print(f"  Clean period days: {snapshot.ledger_health.clean_period_days}")
    print(f"\nAlerts: {alerts if alerts else 'None'}")
    
    return snapshot


if __name__ == "__main__":
    example_usage()


