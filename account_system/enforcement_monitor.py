"""
/account_system/enforcement_monitor.py

Platform Enforcement Observation, Normalization & Impact Surface

PURPOSE:
    Answers ONE question: "What actions has the platform already taken 
    against this account, and how severe, persistent, and contagious are they?"

    This is ground truth observation, not prediction.
    Platforms always act before they explain.
    This file makes that visible.

STRICT BOUNDARIES:
    ✓ Observes explicit platform enforcement signals
    ✓ Normalizes them into common schema
    ✓ Tracks severity, scope, persistence
    ✓ Detects statistically validated soft suppression
    ✓ Records time-bounded enforcement states
    ✓ Deterministic & replayable
    
    ✗ NOT risk scoring
    ✗ NOT trust scoring
    ✗ NOT posting control
    ✗ NOT penalty application
    ✗ NOT evasion logic
    ✗ NOT appeal logic

CORE PRINCIPLE:
    "Enforcement is asymmetric, opaque, and cumulative."
    
    You don't wait for a ban.
    You react to pressure gradients.

CONSUMED BY:
    - trust_scoring.py
    - posting governors
    - rollout managers
    - experiment safety layers
    - escalation controls
"""

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum
from collections import defaultdict
import logging

# ============================================================================
# CONSTANTS & CONFIGURATION
# ============================================================================

VERSION = "1.0.0"

# Severity normalization (platform-agnostic)
class EnforcementSeverity:
    """Normalized severity scale across all platforms"""
    INFORMATIONAL = 0.1      # Warning, notice
    THROTTLE_LIGHT = 0.3     # Feature throttling
    SUPPRESSION = 0.5        # Distribution suppression
    FEATURE_LOSS = 0.7       # Monetization disabled, feature removal
    STRIKE = 0.9             # Official strike, cooldown
    BAN_RISK = 1.0          # Ban indicators, final warnings

# Scope types
class EnforcementScope(Enum):
    ACCOUNT = "account"       # Entire account affected
    CONTENT = "content"       # Specific content only
    FEATURE = "feature"       # Specific feature restricted

# Signal confidence thresholds
CONFIDENCE_THRESHOLD_EXPLICIT = 0.9  # Direct platform notifications
CONFIDENCE_THRESHOLD_IMPLICIT = 0.7  # Statistically validated suppression
CONFIDENCE_THRESHOLD_SPECULATIVE = 0.5  # Low confidence, track but don't act

# Persistence decay
PERSISTENCE_DECAY_DAYS = 30  # How long enforcement memory lasts
PERSISTENCE_COMPOUND_FACTOR = 1.5  # Multiplier for repeated enforcement

# Suppression detection
SUPPRESSION_WINDOW_HOURS = 48
SUPPRESSION_BASELINE_DEVIATION = 2.5  # Std devs below baseline
SUPPRESSION_MIN_SAMPLES = 10

# Watchdog thresholds
WATCHDOG_SEVERITY_JUMP = 0.3
WATCHDOG_TOGGLE_RATE = 3  # Max toggles per day
WATCHDOG_VANISH_SUSPICIOUS = True


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class EnforcementSignal:
    """
    Atomic enforcement observation from platform.
    Immutable once recorded.
    """
    signal_type: str  # "strike", "warning", "throttle", "suppression", etc.
    raw_indicator: str  # Platform-specific raw data
    severity_estimate: float  # 0.0-1.0
    detection_confidence: float  # 0.0-1.0
    timestamp: datetime
    platform: str
    source: str  # "api", "ui", "statistical", "notification"
    
    def __post_init__(self):
        assert 0.0 <= self.severity_estimate <= 1.0
        assert 0.0 <= self.detection_confidence <= 1.0


@dataclass(frozen=True)
class EnforcementState:
    """
    Derived enforcement state for an account.
    Replayable from signals.
    """
    enforcement_type: str
    severity: float  # 0.0-1.0 normalized
    scope: EnforcementScope
    
    start_time: datetime
    expiry_time: Optional[datetime]
    
    persistence_score: float  # Compound effect over time
    confidence: float  # 0.0-1.0
    
    signal_ids: List[str] = field(default_factory=list)
    
    def is_active(self, now: datetime) -> bool:
        """Check if enforcement is currently active"""
        if self.expiry_time and now > self.expiry_time:
            return False
        return True
    
    def time_remaining(self, now: datetime) -> Optional[timedelta]:
        """Time until expiry, if applicable"""
        if not self.expiry_time:
            return None
        if now > self.expiry_time:
            return timedelta(0)
        return self.expiry_time - now


@dataclass(frozen=True)
class SuppressionState:
    """Soft suppression detection state"""
    is_suppressed: bool
    suppression_strength: float  # 0.0-1.0
    confidence: float  # 0.0-1.0
    detection_window_start: datetime
    detection_window_end: datetime
    baseline_deviation: float  # Std devs below baseline
    sample_count: int


@dataclass
class EnforcementSnapshot:
    """Complete enforcement state snapshot"""
    account_id: str
    platform: str
    observation_time: datetime
    
    active_enforcements: List[EnforcementState]
    suppression_state: SuppressionState
    
    aggregate_pressure: float  # 0.0-1.0 combined pressure score
    enforcement_model_version: str
    
    snapshot_hash: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "observation_time": self.observation_time.isoformat(),
            "active_enforcements": [
                {
                    "enforcement_type": e.enforcement_type,
                    "severity": e.severity,
                    "scope": e.scope.value,
                    "start_time": e.start_time.isoformat(),
                    "expiry_time": e.expiry_time.isoformat() if e.expiry_time else None,
                    "persistence_score": e.persistence_score,
                    "confidence": e.confidence
                }
                for e in self.active_enforcements
            ],
            "suppression_state": {
                "is_suppressed": self.suppression_state.is_suppressed,
                "suppression_strength": self.suppression_state.suppression_strength,
                "confidence": self.suppression_state.confidence
            },
            "aggregate_pressure": self.aggregate_pressure,
            "enforcement_model_version": self.enforcement_model_version,
            "snapshot_hash": self.snapshot_hash
        }


# ============================================================================
# SEVERITY CALIBRATOR
# ============================================================================

class SeverityCalibrator:
    """
    Normalizes platform-specific enforcement signals into
    universal severity scores.
    
    CRITICAL: No platform is allowed to override this mapping silently.
    """
    
    def __init__(self):
        # Platform-agnostic severity mappings
        self.severity_map = {
            # Informational (0.1)
            "notice": EnforcementSeverity.INFORMATIONAL,
            "reminder": EnforcementSeverity.INFORMATIONAL,
            "community_guidelines_info": EnforcementSeverity.INFORMATIONAL,
            
            # Light throttle (0.3)
            "rate_limit": EnforcementSeverity.THROTTLE_LIGHT,
            "feature_cooldown": EnforcementSeverity.THROTTLE_LIGHT,
            "posting_slowdown": EnforcementSeverity.THROTTLE_LIGHT,
            
            # Suppression (0.5)
            "reach_limitation": EnforcementSeverity.SUPPRESSION,
            "distribution_reduction": EnforcementSeverity.SUPPRESSION,
            "recommendation_removal": EnforcementSeverity.SUPPRESSION,
            
            # Feature loss (0.7)
            "monetization_disabled": EnforcementSeverity.FEATURE_LOSS,
            "feature_restriction": EnforcementSeverity.FEATURE_LOSS,
            "age_gate": EnforcementSeverity.FEATURE_LOSS,
            
            # Strike (0.9)
            "strike": EnforcementSeverity.STRIKE,
            "violation_notice": EnforcementSeverity.STRIKE,
            "official_warning": EnforcementSeverity.STRIKE,
            
            # Ban risk (1.0)
            "final_warning": EnforcementSeverity.BAN_RISK,
            "suspension_risk": EnforcementSeverity.BAN_RISK,
            "account_review": EnforcementSeverity.BAN_RISK
        }
    
    def calibrate(self, signal_type: str, platform: str) -> float:
        """
        Return normalized severity for signal type.
        Platform parameter allows future platform-specific tuning.
        """
        base_severity = self.severity_map.get(
            signal_type.lower(), 
            EnforcementSeverity.THROTTLE_LIGHT  # Conservative default
        )
        
        # Platform-specific adjustments could go here
        # For now, return base severity
        return base_severity


# ============================================================================
# PERSISTENCE TRACKER
# ============================================================================

class PersistenceTracker:
    """
    Tracks enforcement persistence and compounding effects.
    
    Short strikes fade.
    Repeated pressure compounds.
    """
    
    def __init__(self, decay_days: int = PERSISTENCE_DECAY_DAYS):
        self.decay_days = decay_days
        self.enforcement_history: Dict[str, List[datetime]] = defaultdict(list)
    
    def record_enforcement(self, account_id: str, enforcement_type: str, 
                          timestamp: datetime):
        """Record new enforcement event"""
        key = f"{account_id}:{enforcement_type}"
        self.enforcement_history[key].append(timestamp)
    
    def calculate_persistence(self, account_id: str, enforcement_type: str,
                            current_time: datetime) -> float:
        """
        Calculate persistence score based on:
        - Recency
        - Repetition
        - Decay over time
        
        Returns: 0.0-1.0 persistence multiplier
        """
        key = f"{account_id}:{enforcement_type}"
        history = self.enforcement_history.get(key, [])
        
        if not history:
            return 1.0  # First occurrence
        
        # Filter to recent events within decay window
        decay_window = current_time - timedelta(days=self.decay_days)
        recent_events = [t for t in history if t > decay_window]
        
        if not recent_events:
            return 1.0
        
        # Base persistence on count and recency
        count_factor = min(len(recent_events) * PERSISTENCE_COMPOUND_FACTOR, 3.0)
        
        # Most recent event recency factor
        most_recent = max(recent_events)
        days_since = (current_time - most_recent).days
        recency_factor = max(0.5, 1.0 - (days_since / self.decay_days))
        
        return min(count_factor * recency_factor, 3.0)
    
    def cleanup_old_history(self, current_time: datetime):
        """Remove enforcement history outside decay window"""
        cutoff = current_time - timedelta(days=self.decay_days * 2)
        
        for key in list(self.enforcement_history.keys()):
            self.enforcement_history[key] = [
                t for t in self.enforcement_history[key] if t > cutoff
            ]
            if not self.enforcement_history[key]:
                del self.enforcement_history[key]


# ============================================================================
# SCOPE RESOLVER
# ============================================================================

class ScopeResolver:
    """Determines enforcement scope from signal characteristics"""
    
    def resolve_scope(self, signal: EnforcementSignal) -> EnforcementScope:
        """Infer scope from signal type and context"""
        signal_lower = signal.signal_type.lower()
        
        # Account-level signals
        if any(x in signal_lower for x in [
            "account", "suspension", "ban", "review"
        ]):
            return EnforcementScope.ACCOUNT
        
        # Feature-level signals
        if any(x in signal_lower for x in [
            "monetization", "feature", "posting", "cooldown"
        ]):
            return EnforcementScope.FEATURE
        
        # Content-level signals
        if any(x in signal_lower for x in [
            "content", "post", "violation", "removal"
        ]):
            return EnforcementScope.CONTENT
        
        # Default to account if unclear
        return EnforcementScope.ACCOUNT


# ============================================================================
# SOFT SUPPRESSION DETECTOR
# ============================================================================

class SoftSuppressionDetector:
    """
    Detects statistically validated soft suppression.
    
    CAREFUL: Only triggers if:
    - Statistically validated upstream
    - Persistent across windows
    - Normalized by platform baselines
    - Decoupled from content quality signals
    
    Otherwise → ignored. Prevents paranoia.
    """
    
    def __init__(self):
        self.baselines: Dict[str, float] = {}
        self.baseline_stddevs: Dict[str, float] = {}
    
    def set_baseline(self, account_id: str, platform: str, 
                    mean_reach: float, stddev: float):
        """Set baseline metrics for account"""
        key = f"{account_id}:{platform}"
        self.baselines[key] = mean_reach
        self.baseline_stddevs[key] = stddev
    
    def detect_suppression(self, account_id: str, platform: str,
                          recent_reach_samples: List[float],
                          observation_time: datetime) -> SuppressionState:
        """
        Detect if account is experiencing soft suppression.
        
        Returns SuppressionState with confidence score.
        """
        key = f"{account_id}:{platform}"
        
        # Insufficient data
        if len(recent_reach_samples) < SUPPRESSION_MIN_SAMPLES:
            return SuppressionState(
                is_suppressed=False,
                suppression_strength=0.0,
                confidence=0.0,
                detection_window_start=observation_time,
                detection_window_end=observation_time,
                baseline_deviation=0.0,
                sample_count=len(recent_reach_samples)
            )
        
        # No baseline established
        if key not in self.baselines:
            return SuppressionState(
                is_suppressed=False,
                suppression_strength=0.0,
                confidence=0.0,
                detection_window_start=observation_time,
                detection_window_end=observation_time,
                baseline_deviation=0.0,
                sample_count=len(recent_reach_samples)
            )
        
        baseline = self.baselines[key]
        stddev = self.baseline_stddevs[key]
        
        # Calculate current performance
        current_mean = sum(recent_reach_samples) / len(recent_reach_samples)
        
        # Deviation from baseline in standard deviations
        if stddev > 0:
            deviation = (baseline - current_mean) / stddev
        else:
            deviation = 0.0
        
        # Suppression detected if significantly below baseline
        is_suppressed = deviation > SUPPRESSION_BASELINE_DEVIATION
        
        # Strength based on deviation magnitude
        suppression_strength = min(deviation / 5.0, 1.0) if is_suppressed else 0.0
        
        # Confidence based on sample count and consistency
        sample_confidence = min(len(recent_reach_samples) / (SUPPRESSION_MIN_SAMPLES * 2), 1.0)
        variance_confidence = 1.0 / (1.0 + (sum((x - current_mean)**2 for x in recent_reach_samples) / len(recent_reach_samples)))
        confidence = (sample_confidence + variance_confidence) / 2.0
        
        window_start = observation_time - timedelta(hours=SUPPRESSION_WINDOW_HOURS)
        
        return SuppressionState(
            is_suppressed=is_suppressed,
            suppression_strength=suppression_strength,
            confidence=confidence,
            detection_window_start=window_start,
            detection_window_end=observation_time,
            baseline_deviation=deviation,
            sample_count=len(recent_reach_samples)
        )


# ============================================================================
# ENFORCEMENT HASHER
# ============================================================================

class EnforcementHasher:
    """
    Creates deterministic hashes of enforcement state.
    
    Prevents:
    - Retroactive enforcement rewriting
    - Silent pressure adjustment
    """
    
    @staticmethod
    def hash_snapshot(snapshot: EnforcementSnapshot) -> str:
        """Generate deterministic hash of enforcement snapshot"""
        # Create stable representation
        hash_data = {
            "account_id": snapshot.account_id,
            "platform": snapshot.platform,
            "observation_time": snapshot.observation_time.isoformat(),
            "active_enforcements": [
                {
                    "type": e.enforcement_type,
                    "severity": round(e.severity, 4),
                    "scope": e.scope.value,
                    "start": e.start_time.isoformat(),
                    "expiry": e.expiry_time.isoformat() if e.expiry_time else None
                }
                for e in sorted(snapshot.active_enforcements, 
                              key=lambda x: (x.enforcement_type, x.start_time))
            ],
            "suppression": {
                "is_suppressed": snapshot.suppression_state.is_suppressed,
                "strength": round(snapshot.suppression_state.suppression_strength, 4)
            },
            "aggregate_pressure": round(snapshot.aggregate_pressure, 4),
            "version": snapshot.enforcement_model_version
        }
        
        # Deterministic JSON serialization
        canonical_json = json.dumps(hash_data, sort_keys=True, separators=(',', ':'))
        
        # SHA256 hash
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


# ============================================================================
# ENFORCEMENT WATCHDOG
# ============================================================================

class EnforcementWatchdog:
    """
    Monitors for suspicious enforcement patterns.
    
    Alerts if:
    - Enforcement suddenly disappears
    - Severity jumps unexpectedly
    - Suppression toggles rapidly
    - Expiry mismatches platform norms
    
    Can trigger:
    - Posting slowdown
    - Experiment freeze
    - Factory isolation
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.previous_snapshots: Dict[str, EnforcementSnapshot] = {}
        self.toggle_history: Dict[str, List[datetime]] = defaultdict(list)
    
    def check_snapshot(self, snapshot: EnforcementSnapshot) -> List[str]:
        """
        Check snapshot for suspicious patterns.
        Returns list of alert messages.
        """
        alerts = []
        key = f"{snapshot.account_id}:{snapshot.platform}"
        
        if key not in self.previous_snapshots:
            self.previous_snapshots[key] = snapshot
            return alerts
        
        prev = self.previous_snapshots[key]
        
        # Check for sudden severity jump
        if snapshot.aggregate_pressure - prev.aggregate_pressure > WATCHDOG_SEVERITY_JUMP:
            alerts.append(
                f"SEVERITY_JUMP: Pressure increased by "
                f"{snapshot.aggregate_pressure - prev.aggregate_pressure:.2f} "
                f"({prev.aggregate_pressure:.2f} → {snapshot.aggregate_pressure:.2f})"
            )
        
        # Check for enforcement vanishing
        if WATCHDOG_VANISH_SUSPICIOUS:
            prev_types = {e.enforcement_type for e in prev.active_enforcements}
            curr_types = {e.enforcement_type for e in snapshot.active_enforcements}
            vanished = prev_types - curr_types
            
            if vanished and prev.aggregate_pressure > 0.5:
                alerts.append(
                    f"ENFORCEMENT_VANISH: Active enforcements disappeared: {vanished}"
                )
        
        # Check for rapid suppression toggling
        if prev.suppression_state.is_suppressed != snapshot.suppression_state.is_suppressed:
            self.toggle_history[key].append(snapshot.observation_time)
            
            # Count recent toggles
            day_ago = snapshot.observation_time - timedelta(days=1)
            recent_toggles = [t for t in self.toggle_history[key] if t > day_ago]
            
            if len(recent_toggles) > WATCHDOG_TOGGLE_RATE:
                alerts.append(
                    f"RAPID_TOGGLE: Suppression toggled {len(recent_toggles)} times in 24h"
                )
        
        # Update previous snapshot
        self.previous_snapshots[key] = snapshot
        
        # Log alerts
        for alert in alerts:
            self.logger.warning(f"[WATCHDOG] {key}: {alert}")
        
        return alerts


# ============================================================================
# MAIN ENFORCEMENT MONITOR
# ============================================================================

class EnforcementMonitor:
    """
    Main enforcement monitoring system.
    
    Observes platform enforcement, normalizes signals,
    tracks persistence, detects suppression.
    
    STRICTLY OBSERVATIONAL - NEVER ACTS DIRECTLY.
    """
    
    def __init__(self):
        self.severity_calibrator = SeverityCalibrator()
        self.persistence_tracker = PersistenceTracker()
        self.scope_resolver = ScopeResolver()
        self.suppression_detector = SoftSuppressionDetector()
        self.hasher = EnforcementHasher()
        self.watchdog = EnforcementWatchdog()
        
        self.logger = logging.getLogger(__name__)
        
        # Signal storage
        self.signals: Dict[str, List[EnforcementSignal]] = defaultdict(list)
    
    def collect_explicit_signals(self, account_id: str, platform: str,
                                raw_signals: List[Dict[str, Any]],
                                timestamp: datetime) -> List[EnforcementSignal]:
        """
        Collect and normalize explicit enforcement signals.
        
        Args:
            account_id: Account identifier
            platform: Platform name
            raw_signals: List of raw platform signals
            timestamp: Observation timestamp
        
        Returns:
            List of normalized EnforcementSignals
        """
        signals = []
        
        for raw in raw_signals:
            signal_type = raw.get("type", "unknown")
            
            # Calibrate severity
            severity = self.severity_calibrator.calibrate(signal_type, platform)
            
            # Explicit signals have high confidence
            confidence = raw.get("confidence", CONFIDENCE_THRESHOLD_EXPLICIT)
            
            signal = EnforcementSignal(
                signal_type=signal_type,
                raw_indicator=json.dumps(raw),
                severity_estimate=severity,
                detection_confidence=confidence,
                timestamp=timestamp,
                platform=platform,
                source=raw.get("source", "api")
            )
            
            signals.append(signal)
            
            # Store signal
            key = f"{account_id}:{platform}"
            self.signals[key].append(signal)
        
        return signals
    
    def detect_soft_suppression(self, account_id: str, platform: str,
                               recent_reach_samples: List[float],
                               observation_time: datetime) -> SuppressionState:
        """
        Detect statistically validated soft suppression.
        
        Returns SuppressionState with confidence score.
        """
        return self.suppression_detector.detect_suppression(
            account_id, platform, recent_reach_samples, observation_time
        )
    
    def normalize_enforcement(self, account_id: str, platform: str,
                            signals: List[EnforcementSignal],
                            observation_time: datetime) -> List[EnforcementState]:
        """
        Normalize signals into EnforcementStates.
        
        Combines related signals, resolves scope, tracks persistence.
        """
        states = []
        
        # Group signals by type
        signals_by_type: Dict[str, List[EnforcementSignal]] = defaultdict(list)
        for sig in signals:
            if sig.detection_confidence >= CONFIDENCE_THRESHOLD_IMPLICIT:
                signals_by_type[sig.signal_type].append(sig)
        
        # Create state for each enforcement type
        for enforcement_type, type_signals in signals_by_type.items():
            # Use most recent signal as representative
            latest_signal = max(type_signals, key=lambda s: s.timestamp)
            
            # Resolve scope
            scope = self.scope_resolver.resolve_scope(latest_signal)
            
            # Calculate persistence
            self.persistence_tracker.record_enforcement(
                account_id, enforcement_type, latest_signal.timestamp
            )
            persistence = self.persistence_tracker.calculate_persistence(
                account_id, enforcement_type, observation_time
            )
            
            # Determine expiry (platform-specific, could be enhanced)
            expiry = self._estimate_expiry(enforcement_type, latest_signal.timestamp)
            
            # Combined confidence from all signals
            confidence = max(s.detection_confidence for s in type_signals)
            
            state = EnforcementState(
                enforcement_type=enforcement_type,
                severity=latest_signal.severity_estimate,
                scope=scope,
                start_time=min(s.timestamp for s in type_signals),
                expiry_time=expiry,
                persistence_score=persistence,
                confidence=confidence,
                signal_ids=[id(s) for s in type_signals]
            )
            
            # Only include active enforcements
            if state.is_active(observation_time):
                states.append(state)
        
        return states
    
    def _estimate_expiry(self, enforcement_type: str, 
                        start_time: datetime) -> Optional[datetime]:
        """Estimate enforcement expiry time based on type"""
        expiry_durations = {
            "rate_limit": timedelta(hours=6),
            "posting_slowdown": timedelta(hours=12),
            "feature_cooldown": timedelta(days=1),
            "strike": timedelta(days=7),
            "violation_notice": timedelta(days=14),
            # Most enforcement doesn't auto-expire
        }
        
        duration = expiry_durations.get(enforcement_type)
        if duration:
            return start_time + duration
        return None
    
    def calculate_aggregate_pressure(self, states: List[EnforcementState],
                                     suppression: SuppressionState) -> float:
        """
        Calculate aggregate enforcement pressure.
        
        Combines:
        - Severity × persistence × scope weight
        - Suppression strength
        
        Returns: 0.0-1.0 pressure score
        """
        scope_weights = {
            EnforcementScope.CONTENT: 0.5,
            EnforcementScope.FEATURE: 0.75,
            EnforcementScope.ACCOUNT: 1.0
        }
        
        enforcement_pressure = 0.0
        for state in states:
            scope_weight = scope_weights[state.scope]
            contribution = (
                state.severity * 
                min(state.persistence_score / 2.0, 1.0) * 
                scope_weight *
                state.confidence
            )
            enforcement_pressure += contribution
        
        # Add suppression pressure
        suppression_pressure = (
            suppression.suppression_strength * 
            suppression.confidence * 
            0.5  # Weight suppression at 50% of direct enforcement
        )
        
        # Combine and cap
        total_pressure = min(enforcement_pressure + suppression_pressure, 1.0)
        
        return total_pressure
    
    def observe(self, account_id: str, platform: str,
               raw_signals: List[Dict[str, Any]],
               recent_reach_samples: List[float],
               observation_time: Optional[datetime] = None) -> EnforcementSnapshot:
        """
        Main observation method - creates complete enforcement snapshot.
        
        Args:
            account_id: Account identifier
            platform: Platform name
            raw_signals: Raw enforcement signals from platform
            recent_reach_samples: Recent reach metrics for suppression detection
            observation_time: Observation timestamp (default: now)
        
        Returns:
            Complete EnforcementSnapshot
        """
        if observation_time is None:
            observation_time = datetime.utcnow()
        
        # Collect explicit signals
        signals = self.collect_explicit_signals(
            account_id, platform, raw_signals, observation_time
        )
        
        # Detect soft suppression
        suppression = self.detect_soft_suppression(
            account_id, platform, recent_reach_samples, observation_time
        )
        
        # Normalize into enforcement states
        states = self.normalize_enforcement(
            account_id, platform, signals, observation_time
        )
        
        # Calculate aggregate pressure
        pressure = self.calculate_aggregate_pressure(states, suppression)
        
        # Create snapshot
        snapshot = EnforcementSnapshot(
            account_id=account_id,
            platform=platform,
            observation_time=observation_time,
            active_enforcements=states,
            suppression_state=suppression,
            aggregate_pressure=pressure,
            enforcement_model_version=VERSION
        )
        
        # Generate hash
        snapshot.snapshot_hash = self.hasher.hash_snapshot(snapshot)
        
        # Run watchdog checks
        alerts = self.watchdog.check_snapshot(snapshot)
        if alerts:
            self.logger.warning(
                f"Enforcement watchdog alerts for {account_id}:{platform}: {alerts}"
            )
        
        return snapshot
    
    def set_baseline(self, account_id: str, platform: str,
                    mean_reach: float, stddev: float):
        """Set suppression detection baseline for account"""
        self.suppression_detector.set_baseline(
            account_id, platform, mean_reach, stddev
        )
    
    def cleanup(self, current_time: datetime):
        """Clean up old enforcement history"""
        self.persistence_tracker.cleanup_old_history(current_time)


# ============================================================================
# ENFORCEMENT VALIDATOR
# ============================================================================

class EnforcementValidator:
    """
    Validates enforcement snapshots for consistency and correctness.
    
    Used in testing and audit pipelines.
    """
    
    @staticmethod
    def validate_snapshot(snapshot: EnforcementSnapshot) -> Tuple[bool, List[str]]:
        
        #Validate enforcement snapshot.



