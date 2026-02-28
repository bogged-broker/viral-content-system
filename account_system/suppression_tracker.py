# /account_system/suppression_tracker.py

"""
Shadow / Soft Suppression Inference Engine

Observational, causal, non-evasive detection of algorithmic visibility
suppression that is not explicitly declared by platforms.

CRITICAL LEGAL NOTE:
This module observes outcomes only. It does not:
- Game algorithms
- Probe limits
- Adapt behavior automatically
- Mask suppression
- Circumvent enforcement

It infers statistical deviation from expected distribution curves.
"""

import hashlib
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum
import statistics
import math


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class SuppressionSignal:
    """
    Single observation of reach deviation.
    Weak individually - inference requires aggregation.
    """
    expected_reach: float
    observed_reach: float
    normalized_zscore: float
    time_window: str
    timestamp: datetime
    content_quality_score: float  # Must be neutral/positive for valid signal
    platform_variance_factor: float  # Rule out platform-wide issues
    
    def deviation_magnitude(self) -> float:
        """Absolute deviation from expected."""
        if self.expected_reach == 0:
            return 0.0
        return abs(self.expected_reach - self.observed_reach) / self.expected_reach
    
    def is_underperforming(self) -> bool:
        """True if observed < expected (accounting for noise)."""
        return self.observed_reach < (self.expected_reach * 0.85)


@dataclass(frozen=True)
class SuppressionState:
    """
    Inferred algorithmic suppression state.
    Never binary. Never permanent.
    """
    suppression_strength: float  # 0.0-1.0: magnitude of suppression
    confidence: float  # 0.0-1.0: certainty of inference
    persistence_score: float  # How long has this been observe


"""
/account_system/suppression_tracker.py

Shadow / Soft Suppression Inference Engine

Observational, causal, non-evasive detection of algorithmic visibility
suppression that is not explicitly declared by platforms.

CRITICAL LEGAL NOTE:
This module observes outcomes only. It does not:
- Game algorithms
- Probe limits
- Adapt behavior automatically
- Mask suppression
- Circumvent enforcement

It infers statistical deviation from expected distribution curves.
"""

import hashlib
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum
import statistics
import math


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class SuppressionSignal:
    """
    Single observation of reach deviation.
    Weak individually - inference requires aggregation.
    """
    expected_reach: float
    observed_reach: float
    normalized_zscore: float
    time_window: str
    timestamp: datetime
    content_quality_score: float  # Must be neutral/positive for valid signal
    platform_variance_factor: float  # Rule out platform-wide issues
    
    def deviation_magnitude(self) -> float:
        """Absolute deviation from expected."""
        if self.expected_reach == 0:
            return 0.0
        return abs(self.expected_reach - self.observed_reach) / self.expected_reach
    
    def is_underperforming(self) -> bool:
        """True if observed < expected (accounting for noise)."""
        return self.observed_reach < (self.expected_reach * 0.85)


@dataclass(frozen=True)
class SuppressionState:
    """
    Inferred algorithmic suppression state.
    Never binary. Never permanent.
    """
    suppression_strength: float  # 0.0-1.0: magnitude of suppression
    confidence: float  # 0.0-1.0: certainty of inference
    persistence_score: float  # How long has this been observed
    inferred_start_time: Optional[datetime]
    evidence_signal_count: int
    
    def is_suspected(self) -> bool:
        """High confidence suppression detected."""
        return self.confidence > 0.65 and self.suppression_strength > 0.40
    
    def is_severe(self) -> bool:
        """Severe suppression with high certainty."""
        return self.confidence > 0.80 and self.suppression_strength > 0.70


@dataclass(frozen=True)
class RecoveryWindow:
    """
    Estimated time range for natural suppression decay.
    Non-prescriptive - used for pacing/reporting only.
    """
    earliest: datetime
    latest: datetime
    confidence: float
    
    def days_until_earliest(self, now: datetime) -> int:
        """Days until earliest recovery."""
        delta = self.earliest - now
        return max(0, delta.days)
    
    def days_until_latest(self, now: datetime) -> int:
        """Days until latest expected recovery."""
        delta = self.latest - now
        return max(0, delta.days)


@dataclass
class SuppressionObservation:
    """Complete suppression inference output."""
    account_id: str
    platform: str
    observation_time: datetime
    suppression_state: SuppressionState
    evidence: Dict[str, float]
    expected_recovery_window: Optional[RecoveryWindow]
    tracker_version: str
    inference_hash: str
    
    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "observation_time": self.observation_time.isoformat(),
            "suppression_state": {
                "is_suspected": self.suppression_state.is_suspected(),
                "suppression_strength": self.suppression_state.suppression_strength,
                "confidence": self.suppression_state.confidence,
                "persistence_score": self.suppression_state.persistence_score,
                "inferred_start_time": self.suppression_state.inferred_start_time.isoformat() 
                    if self.suppression_state.inferred_start_time else None,
            },
            "evidence": self.evidence,
            "expected_recovery_window": {
                "earliest": self.expected_recovery_window.earliest.isoformat(),
                "latest": self.expected_recovery_window.latest.isoformat(),
                "confidence": self.expected_recovery_window.confidence,
            } if self.expected_recovery_window else None,
            "tracker_version": self.tracker_version,
            "inference_hash": self.inference_hash,
        }


# ============================================================================
# EXPECTATION MODELING
# ============================================================================


class ExpectationModel:
    """
    Models expected reach based on account baseline and content quality.
    Platform-specific curves with historical calibration.
    """
    
    def __init__(self, platform: str):
        self.platform = platform
        self.baseline_cache: Dict[str, float] = {}
    
    def calculate_expected_reach(
        self,
        account_id: str,
        content_quality_score: float,
        historical_avg_reach: float,
        posting_cadence_factor: float = 1.0,
    ) -> float:
        """
        Calculate expected reach for a piece of content.
        
        Args:
            account_id: Account identifier
            content_quality_score: 0.0-1.0 quality assessment
            historical_avg_reach: Account's historical average
            posting_cadence_factor: Adjustment for posting frequency
        
        Returns:
            Expected reach (impressions/views)
        """
        # Quality multiplier: high quality → higher expected reach
        quality_multiplier = 0.7 + (content_quality_score * 0.6)
        
        # Cadence adjustment: too frequent → lower reach per post
        cadence_adjusted = historical_avg_reach * posting_cadence_factor
        
        # Platform-specific variance
        platform_factor = self._get_platform_factor()
        
        expected = cadence_adjusted * quality_multiplier * platform_factor
        
        # Cache baseline for this account
        self.baseline_cache[account_id] = historical_avg_reach
        
        return expected
    
    def _get_platform_factor(self) -> float:
        """Platform-specific reach variance."""
        factors = {
            "twitter": 1.0,
            "instagram": 0.85,
            "youtube": 1.2,
            "tiktok": 1.3,
            "linkedin": 0.9,
        }
        return factors.get(self.platform.lower(), 1.0)
    
    def is_model_valid(self, account_id: str, historical_posts: int) -> bool:
        """Check if we have enough data for reliable expectations."""
        return historical_posts >= 20 and account_id in self.baseline_cache


# ============================================================================
# DEVIATION ANALYSIS
# ============================================================================


class DeviationAnalyzer:
    """
    Analyzes statistical deviation from expected reach.
    Normalizes against platform-wide variance.
    """
    
    @staticmethod
    def calculate_zscore(
        observed: float,
        expected: float,
        historical_stddev: float,
    ) -> float:
        """
        Calculate z-score for reach deviation.
        
        Returns:
            Z-score (negative = underperforming)
        """
        if historical_stddev == 0:
            return 0.0
        
        return (observed - expected) / historical_stddev
    
    @staticmethod
    def normalize_platform_variance(
        raw_zscore: float,
        platform_variance_factor: float,
    ) -> float:
        """
        Adjust z-score for platform-wide issues.
        
        Args:
            raw_zscore: Raw statistical deviation
            platform_variance_factor: 1.0 = normal, >1.0 = high variance
        
        Returns:
            Normalized z-score
        """
        if platform_variance_factor <= 1.0:
            return raw_zscore
        
        # Reduce severity if platform is experiencing high variance
        return raw_zscore / platform_variance_factor
    
    @staticmethod
    def detect_anomaly(signals: List[SuppressionSignal]) -> bool:
        """
        Detect persistent anomaly across multiple signals.
        
        Requires:
        - Majority underperforming
        - Consistent direction
        - Sufficient magnitude
        """
        if len(signals) < 3:
            return False
        
        underperforming = [s for s in signals if s.is_underperforming()]
        
        # Need >70% underperforming
        if len(underperforming) < len(signals) * 0.7:
            return False
        
        # Check z-score consistency
        zscores = [s.normalized_zscore for s in signals]
        avg_zscore = statistics.mean(zscores)
        
        # Need consistent negative deviation
        return avg_zscore < -1.5


# ============================================================================
# CONFIDENCE ESTIMATION
# ============================================================================


class ConfidenceEstimator:
    """
    Estimates confidence in suppression inference.
    Grows with duration, consistency, signal diversity.
    """
    
    @staticmethod
    def estimate_confidence(
        signals: List[SuppressionSignal],
        persistence_days: int,
        content_quality_avg: float,
        enforcement_explains_drop: bool,
    ) -> float:
        """
        Calculate confidence score for suppression inference.
        
        Returns:
            0.0-1.0 confidence score
        """
        if len(signals) == 0:
            return 0.0
        
        # If enforcement explains it, confidence is 0
        if enforcement_explains_drop:
            return 0.0
        
        # If content quality is poor, not suppression
        if content_quality_avg < 0.4:
            return 0.0
        
        # Base confidence from signal count
        signal_confidence = min(len(signals) / 10.0, 0.4)
        
        # Persistence bonus
        persistence_confidence = min(persistence_days / 14.0, 0.3)
        
        # Consistency bonus
        consistency_confidence = ConfidenceEstimator._calculate_consistency(signals)
        
        total = signal_confidence + persistence_confidence + consistency_confidence
        
        return min(total, 1.0)
    
    @staticmethod
    def _calculate_consistency(signals: List[SuppressionSignal]) -> float:
        """Measure consistency of deviation direction."""
        if len(signals) < 2:
            return 0.0
        
        zscores = [s.normalized_zscore for s in signals]
        
        # All negative?
        all_negative = all(z < 0 for z in zscores)
        if not all_negative:
            return 0.0
        
        # Low variance → high consistency
        variance = statistics.variance(zscores) if len(zscores) > 1 else 0
        consistency = 0.3 * math.exp(-variance / 2.0)
        
        return min(consistency, 0.3)


# ============================================================================
# SUPPRESSION VALIDATION
# ============================================================================


class SuppressionValidator:
    """
    Validates that all preconditions for suppression inference are met.
    Prevents false positives from content quality or platform issues.
    """
    
    @staticmethod
    def validate_inference_conditions(
        expectation_model_valid: bool,
        content_quality_neutral_or_positive: bool,
        deviation_persists: bool,
        platform_variance_ruled_out: bool,
        enforcement_does_not_explain: bool,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check all conditions required for valid suppression inference.
        
        Returns:
            (is_valid, failure_reason)
        """
        if not expectation_model_valid:
            return False, "Insufficient historical data for expectation model"
        
        if not content_quality_neutral_or_positive:
            return False, "Content quality decline explains reach drop"
        
        if not deviation_persists:
            return False, "Deviation not persistent across windows"
        
        if not platform_variance_ruled_out:
            return False, "Platform-wide variance explains deviation"
        
        if not enforcement_does_not_explain:
            return False, "Declared enforcement explains reach drop"
        
        return True, None


# ============================================================================
# SUPPRESSION HASHING (DETERMINISM)
# ============================================================================


class SuppressionHasher:
    """
    Creates deterministic hash of suppression inference.
    Used to detect inference drift and protect against silent rewrites.
    """
    
    @staticmethod
    def hash_inference(
        signals: List[SuppressionSignal],
        observation_time: datetime,
        tracker_version: str,
    ) -> str:
        """
        Create deterministic hash of suppression inference.
        
        Given same inputs → same hash (bit-for-bit).
        """
        # Sort signals by timestamp for determinism
        sorted_signals = sorted(signals, key=lambda s: s.timestamp)
        
        # Build canonical representation
        canonical = {
            "signals": [
                {
                    "expected": s.expected_reach,
                    "observed": s.observed_reach,
                    "zscore": s.normalized_zscore,
                    "timestamp": s.timestamp.isoformat(),
                }
                for s in sorted_signals
            ],
            "observation_time": observation_time.isoformat(),
            "tracker_version": tracker_version,
        }
        
        # Deterministic JSON
        canonical_json = json.dumps(canonical, sort_keys=True)
        
        # SHA256 hash
        return hashlib.sha256(canonical_json.encode()).hexdigest()


# ============================================================================
# SUPPRESSION WATCHDOG
# ============================================================================


class SuppressionWatchdog:
    """
    Alerts on suspicious inference patterns.
    Prevents false positives from cascading into bad decisions.
    """
    
    def __init__(self):
        self.alert_history: List[str] = []
    
    def check_for_anomalies(
        self,
        current_state: SuppressionState,
        previous_state: Optional[SuppressionState],
        enforcement_correlation: float,
    ) -> List[str]:
        """
        Check for suspicious patterns in suppression inference.
        
        Returns:
            List of alert messages
        """
        alerts = []
        
        # Rapid toggling
        if previous_state:
            if (current_state.is_suspected() != previous_state.is_suspected() and
                current_state.persistence_score < 2.0):
                alerts.append("WATCHDOG: Suppression state toggling rapidly")
        
        # Unrealistic confidence spike
        if previous_state:
            confidence_jump = current_state.confidence - previous_state.confidence
            if confidence_jump > 0.4:
                alerts.append("WATCHDOG: Unrealistic confidence spike detected")
        
        # High suppression but no enforcement correlation
        if (current_state.is_severe() and enforcement_correlation < 0.1):
            alerts.append("WATCHDOG: Severe suppression with no enforcement correlation")
        
        # Perfect suppression score (suspiciously precise)
        if current_state.suppression_strength > 0.95:
            alerts.append("WATCHDOG: Suppression strength suspiciously high")
        
        self.alert_history.extend(alerts)
        return alerts


# ============================================================================
# MAIN TRACKER
# ============================================================================


class SuppressionTracker:
    """
    Main suppression inference engine.
    
    Coordinates:
    - Expectation modeling
    - Deviation analysis
    - Confidence estimation
    - Validation
    - Watchdog monitoring
    """
    
    VERSION = "1.0.0"
    
    def __init__(self, platform: str):
        self.platform = platform
        self.expectation_model = ExpectationModel(platform)
        self.deviation_analyzer = DeviationAnalyzer()
        self.confidence_estimator = ConfidenceEstimator()
        self.validator = SuppressionValidator()
        self.hasher = SuppressionHasher()
        self.watchdog = SuppressionWatchdog()
        
        # Signal history per account
        self.signal_history: Dict[str, List[SuppressionSignal]] = {}
        self.state_history: Dict[str, List[SuppressionState]] = {}
    
    def infer_suppression(
        self,
        account_id: str,
        recent_signals: List[SuppressionSignal],
        enforcement_active: bool,
        historical_posts: int,
    ) -> SuppressionObservation:
        """
        Infer suppression state for an account.
        
        Args:
            account_id: Account identifier
            recent_signals: Recent reach deviation signals
            enforcement_active: Whether declared enforcement is active
            historical_posts: Number of historical posts for model validation
        
        Returns:
            Complete suppression observation
        """
        observation_time = datetime.utcnow()
        
        # Store signals
        self.signal_history[account_id] = recent_signals
        
        # Check if expectation model is valid
        model_valid = self.expectation_model.is_model_valid(account_id, historical_posts)
        
        # Check content quality
        if recent_signals:
            avg_quality = statistics.mean([s.content_quality_score for s in recent_signals])
            quality_ok = avg_quality >= 0.5
        else:
            quality_ok = False
        
        # Check deviation persistence
        deviation_persists = self.deviation_analyzer.detect_anomaly(recent_signals)
        
        # Check platform variance
        if recent_signals:
            avg_variance = statistics.mean([s.platform_variance_factor for s in recent_signals])
            platform_ok = avg_variance < 1.5
        else:
            platform_ok = False
        
        # Validate all conditions
        conditions_valid, failure_reason = self.validator.validate_inference_conditions(
            expectation_model_valid=model_valid,
            content_quality_neutral_or_positive=quality_ok,
            deviation_persists=deviation_persists,
            platform_variance_ruled_out=platform_ok,
            enforcement_does_not_explain=not enforcement_active,
        )
        
        if not conditions_valid:
            # No suppression - conditions not met
            suppression_state = SuppressionState(
                suppression_strength=0.0,
                confidence=0.0,
                persistence_score=0.0,
                inferred_start_time=None,
                evidence_signal_count=0,
            )
            
            return SuppressionObservation(
                account_id=account_id,
                platform=self.platform,
                observation_time=observation_time,
                suppression_state=suppression_state,
                evidence={"failure_reason": failure_reason or "unknown"},
                expected_recovery_window=None,
                tracker_version=self.VERSION,
                inference_hash=self.hasher.hash_inference([], observation_time, self.VERSION),
            )
        
        # Calculate suppression metrics
        suppression_strength = self._calculate_suppression_strength(recent_signals)
        persistence_days = self._calculate_persistence(recent_signals)
        
        confidence = self.confidence_estimator.estimate_confidence(
            signals=recent_signals,
            persistence_days=persistence_days,
            content_quality_avg=avg_quality,
            enforcement_explains_drop=enforcement_active,
        )
        
        # Infer start time
        start_time = self._infer_start_time(recent_signals)
        
        suppression_state = SuppressionState(
            suppression_strength=suppression_strength,
            confidence=confidence,
            persistence_score=persistence_days,
            inferred_start_time=start_time,
            evidence_signal_count=len(recent_signals),
        )
        
        # Watchdog check
        previous_state = self.state_history.get(account_id, [None])[-1]
        alerts = self.watchdog.check_for_anomalies(
            current_state=suppression_state,
            previous_state=previous_state,
            enforcement_correlation=1.0 if enforcement_active else 0.0,
        )
        
        # Store state
        if account_id not in self.state_history:
            self.state_history[account_id] = []
        self.state_history[account_id].append(suppression_state)
        
        # Estimate recovery window
        recovery_window = self._estimate_recovery_window(
            suppression_state, observation_time
        )
        
        # Build evidence
        evidence = self._build_evidence(recent_signals)
        if alerts:
            evidence["watchdog_alerts"] = len(alerts)
        
        # Hash inference
        inference_hash = self.hasher.hash_inference(
            signals=recent_signals,
            observation_time=observation_time,
            tracker_version=self.VERSION,
        )
        
        return SuppressionObservation(
            account_id=account_id,
            platform=self.platform,
            observation_time=observation_time,
            suppression_state=suppression_state,
            evidence=evidence,
            expected_recovery_window=recovery_window,
            tracker_version=self.VERSION,
            inference_hash=inference_hash,
        )
    
    def _calculate_suppression_strength(self, signals: List[SuppressionSignal]) -> float:
        """Calculate magnitude of suppression (0.0-1.0)."""
        if not signals:
            return 0.0
        
        deviations = [s.deviation_magnitude() for s in signals]
        avg_deviation = statistics.mean(deviations)
        
        # Map deviation to 0-1 scale
        strength = min(avg_deviation / 0.7, 1.0)
        return strength
    
    def _calculate_persistence(self, signals: List[SuppressionSignal]) -> int:
        """Calculate how many days suppression has persisted."""
        if len(signals) < 2:
            return 0
        
        sorted_signals = sorted(signals, key=lambda s: s.timestamp)
        earliest = sorted_signals[0].timestamp
        latest = sorted_signals[-1].timestamp
        
        delta = latest - earliest
        return delta.days
    
    def _infer_start_time(self, signals: List[SuppressionSignal]) -> Optional[datetime]:
        """Infer when suppression likely started."""
        if not signals:
            return None
        
        sorted_signals = sorted(signals, key=lambda s: s.timestamp)
        return sorted_signals[0].timestamp
    
    def _estimate_recovery_window(
        self,
        state: SuppressionState,
        now: datetime,
    ) -> Optional[RecoveryWindow]:
        """
        Estimate time range for natural recovery.
        Non-prescriptive - for planning only.
        """
        if not state.is_suspected():
            return None
        
        # Mild suppression: 7-14 days
        # Moderate: 14-30 days
        # Severe: 30-60 days
        if state.suppression_strength < 0.5:
            earliest_days = 7
            latest_days = 14
        elif state.suppression_strength < 0.75:
            earliest_days = 14
            latest_days = 30
        else:
            earliest_days = 30
            latest_days = 60
        
        return RecoveryWindow(
            earliest=now + timedelta(days=earliest_days),
            latest=now + timedelta(days=latest_days),
            confidence=state.confidence * 0.7,  # Less certain about timing
        )
    
    def _build_evidence(self, signals: List[SuppressionSignal]) -> Dict[str, float]:
        """Build evidence dictionary for observation."""
        if not signals:
            return {}
        
        deviations = [s.deviation_magnitude() for s in signals]
        zscores = [s.normalized_zscore for s in signals]
        
        return {
            "reach_deviation": statistics.mean(deviations),
            "baseline_violation_duration_days": self._calculate_persistence(signals),
            "platform_normalized_zscore": statistics.mean(zscores),
            "signal_count": len(signals),
        }


# ============================================================================
# INTEGRATION EXAMPLE
# ============================================================================


def example_usage():
    """Example of how this module integrates with account system."""
    
    # Initialize tracker
    tracker = SuppressionTracker(platform="twitter")
    
    # Simulate recent signals
    signals = [
        SuppressionSignal(
            expected_reach=10000,
            observed_reach=4000,
            normalized_zscore=-2.1,
            time_window="2024-01-20 to 2024-01-21",
            timestamp=datetime(2024, 1, 21),
            content_quality_score=0.75,
            platform_variance_factor=1.0,
        ),
        SuppressionSignal(
            expected_reach=12000,
            observed_reach=5000,
            normalized_zscore=-2.3,
            time_window="2024-01-21 to 2024-01-22",
            timestamp=datetime(2024, 1, 22),
            content_quality_score=0.80,
            platform_variance_factor=1.1,
        ),
        SuppressionSignal(
            expected_reach=11000,
            observed_reach=4500,
            normalized_zscore=-2.0,
            time_window="2024-01-22 to 2024-01-23",
            timestamp=datetime(2024, 1, 23),
            content_quality_score=0.78,
            platform_variance_factor=1.0,
        ),
    ]
    
    # Infer suppression
    observation = tracker.infer_suppression(
        account_id="account_123",
        recent_signals=signals,
        enforcement_active=False,
        historical_posts=50,
    )
    
    # Output
    print(json.dumps(observation.to_dict(), indent=2))


if __name__ == "__main__":
    example_usage()






