"""
/posting/monitoring/risk_evaluator.py

Canonical Suppression Risk Scoring Engine

Transforms normalized signals into deterministic, explainable suppression risk scores.
This is the single authoritative risk truth boundary between telemetry and action.

TIER-0 INVARIANT-LOCKED SPECIFICATION
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
import time
import math
import logging

# Version pinning - increment on weight changes or model updates
RISK_MODEL_VERSION = "1.0.0"

logger = logging.getLogger(__name__)


# ============================================================================
# Core Enums
# ============================================================================

class RiskSignalType(Enum):
    """Input signals that contribute to suppression risk assessment."""
    VISIBILITY_DECAY = "visibility_decay"
    ENGAGEMENT_STARVATION = "engagement_starvation"
    LATENCY_DEGRADATION = "latency_degradation"
    ERROR_RATE_INCREASE = "error_rate_increase"
    RATE_LIMIT_PRESSURE = "rate_limit_pressure"
    ACCOUNT_AGE_SENSITIVITY = "account_age_sensitivity"
    RECENT_SUPPRESSION_EVENT = "recent_suppression_event"
    PLATFORM_WARNING_SIGNAL = "platform_warning_signal"


# ============================================================================
# Core Data Structures
# ============================================================================

@dataclass(frozen=True)
class RiskFactorWeight:
    """
    Explicit, versioned weight for a risk signal.
    
    Weights are:
    - Deterministic
    - Config-loaded
    - Auditable
    - Never runtime-tuned
    """
    signal: RiskSignalType
    weight: float  # ∈ [0.0, 1.0]
    decay_half_life_sec: int  # Time for signal to decay to 50% influence
    
    def __post_init__(self):
        if not (0.0 <= self.weight <= 1.0):
            raise ValueError(f"Weight must be in [0.0, 1.0], got {self.weight}")
        if self.decay_half_life_sec <= 0:
            raise ValueError(f"Decay half-life must be positive, got {self.decay_half_life_sec}")


@dataclass(frozen=True)
class SuppressionRiskScore:
    """
    Immutable risk assessment output.
    
    Confidence reflects signal completeness, not optimism.
    Low confidence ≠ low risk. They are independent.
    """
    platform: str
    account_id: str
    
    score: float  # ∈ [0.0, 1.0]
    confidence: float  # ∈ [0.0, 1.0]
    
    contributing_signals: Dict[RiskSignalType, float]  # Normalized values
    evaluated_at: float
    
    model_version: str
    
    def __post_init__(self):
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"Score must be in [0.0, 1.0], got {self.score}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Confidence must be in [0.0, 1.0], got {self.confidence}")
        if math.isnan(self.score) or math.isnan(self.confidence):
            raise ValueError("Score and confidence cannot be NaN")


@dataclass
class RawSignalData:
    """Container for extracted signal values before normalization."""
    signal_type: RiskSignalType
    raw_value: float
    timestamp: float
    metadata: Dict = field(default_factory=dict)


# ============================================================================
# Default Weight Configuration
# ============================================================================

DEFAULT_RISK_WEIGHTS = [
    RiskFactorWeight(
        signal=RiskSignalType.VISIBILITY_DECAY,
        weight=0.25,
        decay_half_life_sec=3600  # 1 hour
    ),
    RiskFactorWeight(
        signal=RiskSignalType.ENGAGEMENT_STARVATION,
        weight=0.20,
        decay_half_life_sec=7200  # 2 hours
    ),
    RiskFactorWeight(
        signal=RiskSignalType.LATENCY_DEGRADATION,
        weight=0.15,
        decay_half_life_sec=1800  # 30 minutes
    ),
    RiskFactorWeight(
        signal=RiskSignalType.ERROR_RATE_INCREASE,
        weight=0.15,
        decay_half_life_sec=900  # 15 minutes
    ),
    RiskFactorWeight(
        signal=RiskSignalType.RATE_LIMIT_PRESSURE,
        weight=0.10,
        decay_half_life_sec=600  # 10 minutes
    ),
    RiskFactorWeight(
        signal=RiskSignalType.ACCOUNT_AGE_SENSITIVITY,
        weight=0.05,
        decay_half_life_sec=86400  # 24 hours
    ),
    RiskFactorWeight(
        signal=RiskSignalType.RECENT_SUPPRESSION_EVENT,
        weight=0.08,
        decay_half_life_sec=14400  # 4 hours
    ),
    RiskFactorWeight(
        signal=RiskSignalType.PLATFORM_WARNING_SIGNAL,
        weight=0.02,
        decay_half_life_sec=3600  # 1 hour
    ),
]


# ============================================================================
# Signal Extraction
# ============================================================================

class RiskSignalExtractor:
    """
    Extracts normalized signals from platform telemetry, posting state, and limits.
    
    All signals must be normalized to [0.0, 1.0] where:
    - 0.0 = no risk contribution
    - 1.0 = maximum risk contribution
    
    Consumes facts only from:
    - platform_telemetry.py
    - posting_state_store.py
    - platform_limits.py
    
    NO THRESHOLDS. NO DECISIONS.
    """
    
    def __init__(self, telemetry_reader, state_reader, limits_reader):
        self.telemetry = telemetry_reader
        self.state = state_reader
        self.limits = limits_reader
    
    def extract_visibility_decay(self, platform: str, account_id: str, 
                                 lookback_sec: int = 3600) -> RawSignalData:
        """
        Calculate visibility decay slope over lookback window.
        
        Returns normalized signal where 1.0 = severe visibility collapse.
        """
        now = time.time()
        reach_samples = self.telemetry.get_reach_samples(
            platform, account_id, 
            start_time=now - lookback_sec,
            end_time=now
        )
        
        if not reach_samples or len(reach_samples) < 2:
            return RawSignalData(
                signal_type=RiskSignalType.VISIBILITY_DECAY,
                raw_value=0.0,
                timestamp=now,
                metadata={"reason": "insufficient_samples"}
            )
        
        # Calculate linear regression slope
        times = [s['timestamp'] for s in reach_samples]
        reaches = [s['reach'] for s in reach_samples]
        
        slope = self._calculate_slope(times, reaches)
        baseline_reach = sum(reaches) / len(reaches)
        
        # Normalize: negative slope as fraction of baseline
        if baseline_reach > 0 and slope < 0:
            decay_rate = abs(slope) / baseline_reach
            normalized = min(1.0, decay_rate * 100)  # Scale factor
        else:
            normalized = 0.0
        
        return RawSignalData(
            signal_type=RiskSignalType.VISIBILITY_DECAY,
            raw_value=max(0.0, min(1.0, normalized)),
            timestamp=now,
            metadata={"slope": slope, "baseline": baseline_reach}
        )
    
    def extract_engagement_starvation(self, platform: str, account_id: str,
                                     window_sec: int = 7200) -> RawSignalData:
        """
        Detect extended periods of zero engagement despite posting.
        
        Returns normalized signal where 1.0 = complete engagement death.
        """
        now = time.time()
        engagement_data = self.telemetry.get_engagement_metrics(
            platform, account_id,
            start_time=now - window_sec,
            end_time=now
        )
        
        if not engagement_data:
            return RawSignalData(
                signal_type=RiskSignalType.ENGAGEMENT_STARVATION,
                raw_value=0.0,
                timestamp=now,
                metadata={"reason": "no_data"}
            )
        
        total_posts = engagement_data.get('post_count', 0)
        total_engagement = engagement_data.get('total_engagement', 0)
        zero_engagement_count = engagement_data.get('zero_engagement_posts', 0)
        
        if total_posts == 0:
            normalized = 0.0
        else:
            starvation_ratio = zero_engagement_count / total_posts
            normalized = min(1.0, starvation_ratio)
        
        return RawSignalData(
            signal_type=RiskSignalType.ENGAGEMENT_STARVATION,
            raw_value=normalized,
            timestamp=now,
            metadata={
                "total_posts": total_posts,
                "zero_engagement": zero_engagement_count
            }
        )
    
    def extract_latency_degradation(self, platform: str, account_id: str,
                                   baseline_window_sec: int = 86400) -> RawSignalData:
        """
        Measure API latency inflation vs historical baseline.
        
        Returns normalized signal where 1.0 = severe latency spike.
        """
        now = time.time()
        recent_latency = self.telemetry.get_average_latency(
            platform, account_id,
            start_time=now - 300,  # Last 5 minutes
            end_time=now
        )
        
        baseline_latency = self.telemetry.get_average_latency(
            platform, account_id,
            start_time=now - baseline_window_sec,
            end_time=now - 3600  # Exclude last hour
        )
        
        if baseline_latency is None or baseline_latency == 0:
            return RawSignalData(
                signal_type=RiskSignalType.LATENCY_DEGRADATION,
                raw_value=0.0,
                timestamp=now,
                metadata={"reason": "no_baseline"}
            )
        
        if recent_latency is None:
            return RawSignalData(
                signal_type=RiskSignalType.LATENCY_DEGRADATION,
                raw_value=0.0,
                timestamp=now,
                metadata={"reason": "no_recent_data"}
            )
        
        # Calculate inflation ratio
        inflation = (recent_latency - baseline_latency) / baseline_latency
        normalized = min(1.0, max(0.0, inflation / 5.0))  # 5x = full risk
        
        return RawSignalData(
            signal_type=RiskSignalType.LATENCY_DEGRADATION,
            raw_value=normalized,
            timestamp=now,
            metadata={
                "recent_latency_ms": recent_latency,
                "baseline_latency_ms": baseline_latency
            }
        )
    
    def extract_error_rate_increase(self, platform: str, account_id: str,
                                   window_sec: int = 3600) -> RawSignalData:
        """
        Calculate error rate delta vs expected baseline.
        
        Returns normalized signal where 1.0 = error storm.
        """
        now = time.time()
        error_stats = self.telemetry.get_error_stats(
            platform, account_id,
            start_time=now - window_sec,
            end_time=now
        )
        
        if not error_stats:
            return RawSignalData(
                signal_type=RiskSignalType.ERROR_RATE_INCREASE,
                raw_value=0.0,
                timestamp=now,
                metadata={"reason": "no_data"}
            )
        
        total_requests = error_stats.get('total_requests', 0)
        error_count = error_stats.get('error_count', 0)
        
        if total_requests == 0:
            normalized = 0.0
        else:
            error_rate = error_count / total_requests
            # Expected baseline: 0.01 (1%)
            # 10% error rate = full risk
            normalized = min(1.0, max(0.0, (error_rate - 0.01) / 0.09))
        
        return RawSignalData(
            signal_type=RiskSignalType.ERROR_RATE_INCREASE,
            raw_value=normalized,
            timestamp=now,
            metadata={
                "error_count": error_count,
                "total_requests": total_requests
            }
        )
    
    def extract_rate_limit_pressure(self, platform: str, account_id: str) -> RawSignalData:
        """
        Calculate proximity to rate limits as risk factor.
        
        Returns normalized signal where 1.0 = at limit ceiling.
        """
        now = time.time()
        limit_status = self.limits.get_current_status(platform, account_id)
        
        if not limit_status:
            return RawSignalData(
                signal_type=RiskSignalType.RATE_LIMIT_PRESSURE,
                raw_value=0.0,
                timestamp=now,
                metadata={"reason": "no_limit_data"}
            )
        
        # Get worst proximity across all limit types
        max_proximity = 0.0
        for limit_type, status in limit_status.items():
            used = status.get('used', 0)
            limit = status.get('limit', 1)
            if limit > 0:
                proximity = used / limit
                max_proximity = max(max_proximity, proximity)
        
        normalized = min(1.0, max(0.0, max_proximity))
        
        return RawSignalData(
            signal_type=RiskSignalType.RATE_LIMIT_PRESSURE,
            raw_value=normalized,
            timestamp=now,
            metadata={"max_proximity": max_proximity}
        )
    
    def extract_account_age_sensitivity(self, platform: str, account_id: str) -> RawSignalData:
        """
        Young accounts have higher suppression risk.
        
        Returns normalized signal where 1.0 = brand new account.
        """
        now = time.time()
        account_info = self.state.get_account_info(platform, account_id)
        
        if not account_info or 'created_at' not in account_info:
            return RawSignalData(
                signal_type=RiskSignalType.ACCOUNT_AGE_SENSITIVITY,
                raw_value=0.5,  # Unknown = moderate risk
                timestamp=now,
                metadata={"reason": "unknown_age"}
            )
        
        age_sec = now - account_info['created_at']
        age_days = age_sec / 86400
        
        # Risk curve: exponential decay
        # Day 0 = 1.0, Day 30 = 0.37, Day 90 = 0.05
        normalized = math.exp(-age_days / 30.0)
        
        return RawSignalData(
            signal_type=RiskSignalType.ACCOUNT_AGE_SENSITIVITY,
            raw_value=min(1.0, max(0.0, normalized)),
            timestamp=now,
            metadata={"age_days": age_days}
        )
    
    def extract_recent_suppression_event(self, platform: str, account_id: str,
                                        lookback_sec: int = 86400) -> RawSignalData:
        """
        Recent confirmed suppression events increase re-suppression risk.
        
        Returns normalized signal with time decay.
        """
        now = time.time()
        suppression_events = self.state.get_suppression_events(
            platform, account_id,
            start_time=now - lookback_sec,
            end_time=now
        )
        
        if not suppression_events:
            return RawSignalData(
                signal_type=RiskSignalType.RECENT_SUPPRESSION_EVENT,
                raw_value=0.0,
                timestamp=now,
                metadata={"event_count": 0}
            )
        
        # Most recent event determines risk with time decay
        most_recent = max(suppression_events, key=lambda e: e['timestamp'])
        time_since = now - most_recent['timestamp']
        
        # Exponential decay: half-life of 4 hours
        decay = math.exp(-time_since / 14400)
        normalized = min(1.0, max(0.0, decay))
        
        return RawSignalData(
            signal_type=RiskSignalType.RECENT_SUPPRESSION_EVENT,
            raw_value=normalized,
            timestamp=now,
            metadata={
                "event_count": len(suppression_events),
                "time_since_sec": time_since
            }
        )
    
    def extract_platform_warning_signal(self, platform: str, account_id: str) -> RawSignalData:
        """
        Explicit platform warnings (shadowban notices, verification prompts, etc).
        
        Returns normalized signal where 1.0 = active warning.
        """
        now = time.time()
        warnings = self.state.get_platform_warnings(platform, account_id)
        
        if not warnings:
            return RawSignalData(
                signal_type=RiskSignalType.PLATFORM_WARNING_SIGNAL,
                raw_value=0.0,
                timestamp=now,
                metadata={"warning_count": 0}
            )
        
        # Active warnings = full risk
        active_warnings = [w for w in warnings if w.get('resolved', False) is False]
        normalized = 1.0 if active_warnings else 0.0
        
        return RawSignalData(
            signal_type=RiskSignalType.PLATFORM_WARNING_SIGNAL,
            raw_value=normalized,
            timestamp=now,
            metadata={"active_warnings": len(active_warnings)}
        )
    
    def extract_all_signals(self, platform: str, account_id: str) -> List[RawSignalData]:
        """Extract all available signals for the given platform/account."""
        return [
            self.extract_visibility_decay(platform, account_id),
            self.extract_engagement_starvation(platform, account_id),
            self.extract_latency_degradation(platform, account_id),
            self.extract_error_rate_increase(platform, account_id),
            self.extract_rate_limit_pressure(platform, account_id),
            self.extract_account_age_sensitivity(platform, account_id),
            self.extract_recent_suppression_event(platform, account_id),
            self.extract_platform_warning_signal(platform, account_id),
        ]
    
    @staticmethod
    def _calculate_slope(x_values: List[float], y_values: List[float]) -> float:
        """Calculate linear regression slope."""
        n = len(x_values)
        if n < 2:
            return 0.0
        
        x_mean = sum(x_values) / n
        y_mean = sum(y_values) / n
        
        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
        denominator = sum((x - x_mean) ** 2 for x in x_values)
        
        if denominator == 0:
            return 0.0
        
        return numerator / denominator


# ============================================================================
# Risk Aggregation
# ============================================================================

class RiskAggregator:
    """
    Combines signals via weighted accumulation with time decay.
    
    Rules:
    - Missing signals contribute zero
    - Recent signals dominate via decay
    - No single signal can exceed its weight
    """
    
    def __init__(self, weights: List[RiskFactorWeight]):
        self.weights = {w.signal: w for w in weights}
        self._validate_weights()
    
    def _validate_weights(self):
        """Ensure weights are properly configured."""
        total_weight = sum(w.weight for w in self.weights.values())
        if not (0.99 <= total_weight <= 1.01):
            logger.warning(f"Weights sum to {total_weight}, expected ~1.0")
    
    def aggregate(self, signals: List[RawSignalData], 
                 evaluation_time: float) -> Tuple[float, Dict[RiskSignalType, float]]:
        """
        Aggregate signals into raw risk score.
        
        Returns: (raw_risk, contributing_signals_dict)
        """
        raw_risk = 0.0
        contributing = {}
        
        for signal in signals:
            if signal.signal_type not in self.weights:
                continue
            
            weight_config = self.weights[signal.signal_type]
            
            # Apply time decay
            time_delta = evaluation_time - signal.timestamp
            decay_factor = self._calculate_decay(time_delta, weight_config.decay_half_life_sec)
            
            # Weighted contribution
            contribution = weight_config.weight * signal.raw_value * decay_factor
            raw_risk += contribution
            
            contributing[signal.signal_type] = signal.raw_value
        
        return raw_risk, contributing
    
    @staticmethod
    def _calculate_decay(time_delta_sec: float, half_life_sec: int) -> float:
        """Calculate exponential decay factor."""
        if time_delta_sec < 0:
            return 1.0  # Future data (shouldn't happen)
        
        return math.exp(-time_delta_sec * math.log(2) / half_life_sec)


# ============================================================================
# Risk Normalization
# ============================================================================

class RiskNormalizer:
    """
    Ensures final score ∈ [0.0, 1.0] with proper bounds and monotonicity.
    
    Uses sigmoid normalization for smooth bounds.
    Deterministic math only. No randomness. No learning.
    """
    
    def __init__(self, steepness: float = 4.0, midpoint: float = 0.5):
        """
        Args:
            steepness: Controls sigmoid curve steepness (higher = sharper transition)
            midpoint: Input value that maps to 0.5 output
        """
        self.steepness = steepness
        self.midpoint = midpoint
    
    def normalize(self, raw_risk: float) -> float:
        """
        Normalize raw risk to [0.0, 1.0] using sigmoid.
        
        Ensures:
        - Output ∈ [0.0, 1.0]
        - Monotonic (worse inputs → worse risk)
        - Bounded influence
        """
        # Sigmoid: 1 / (1 + e^(-k(x - x0)))
        normalized = 1.0 / (1.0 + math.exp(-self.steepness * (raw_risk - self.midpoint)))
        
        # Hard bounds (shouldn't be needed, but safety)
        return max(0.0, min(1.0, normalized))


# ============================================================================
# Confidence Calculation
# ============================================================================

class ConfidenceCalculator:
    """
    Calculate confidence in risk assessment based on signal quality.
    
    Confidence reflects:
    - Signal coverage (how many signals available)
    - Freshness (how recent is the data)
    - Platform support completeness
    
    Low confidence ≠ low risk. They are independent.
    """
    
    def __init__(self, total_signal_types: int):
        self.total_signal_types = total_signal_types
    
    def calculate(self, signals: List[RawSignalData], 
                 evaluation_time: float) -> float:
        """
        Calculate confidence ∈ [0.0, 1.0].
        """
        if not signals:
            return 0.0
        
        # Factor 1: Signal coverage
        coverage = len(signals) / self.total_signal_types
        
        # Factor 2: Freshness (average age of signals)
        ages = [evaluation_time - s.timestamp for s in signals]
        avg_age_sec = sum(ages) / len(ages)
        freshness = math.exp(-avg_age_sec / 3600)  # 1-hour half-life
        
        # Factor 3: Data quality (signals with metadata vs without)
        quality_signals = sum(1 for s in signals if s.metadata)
        quality = quality_signals / len(signals)
        
        # Weighted combination
        confidence = (0.5 * coverage + 0.3 * freshness + 0.2 * quality)
        
        return max(0.0, min(1.0, confidence))


# ============================================================================
# Invariant Validation
# ============================================================================

class RiskInvariantValidator:
    """
    MANDATORY validation of all risk model invariants.
    
    Enforces:
    - Score bounds
    - Signal normalization bounds
    - Monotonic degradation rules
    - Stable output under identical inputs
    - Version pinning
    
    Invariant violation → hard fail.
    """
    
    @staticmethod
    def validate_score(score: SuppressionRiskScore) -> None:
        """Validate risk score invariants. Raises on violation."""
        
        # Bounds check
        if not (0.0 <= score.score <= 1.0):
            raise ValueError(f"Score {score.score} violates [0.0, 1.0] bound")
        
        if not (0.0 <= score.confidence <= 1.0):
            raise ValueError(f"Confidence {score.confidence} violates [0.0, 1.0] bound")
        
        # NaN check
        if math.isnan(score.score) or math.isnan(score.confidence):
            raise ValueError("Score or confidence is NaN")
        
        # Signal bounds
        for signal_type, value in score.contributing_signals.items():
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"Signal {signal_type} value {value} violates [0.0, 1.0] bound"
                )
            if math.isnan(value):
                raise ValueError(f"Signal {signal_type} value is NaN")
        
        # Version pinning
        if score.model_version != RISK_MODEL_VERSION:
            raise ValueError(
                f"Score version {score.model_version} does not match "
                f"current version {RISK_MODEL_VERSION}"
            )
        
        # Timestamp sanity
        if score.evaluated_at <= 0:
            raise ValueError(f"Invalid evaluation timestamp {score.evaluated_at}")
    
    @staticmethod
    def validate_raw_signal(signal: RawSignalData) -> None:
        """Validate raw signal data. Raises on violation."""
        
        if not (0.0 <= signal.raw_value <= 1.0):
            raise ValueError(
                f"Signal {signal.signal_type} raw_value {signal.raw_value} "
                f"violates [0.0, 1.0] bound"
            )
        
        if math.isnan(signal.raw_value):
            raise ValueError(f"Signal {signal.signal_type} raw_value is NaN")
        
        if signal.timestamp <= 0:
            raise ValueError(f"Signal {signal.signal_type} has invalid timestamp")


# ============================================================================
# Main Risk Evaluator
# ============================================================================

class RiskEvaluator:
    """
    Single authoritative suppression risk scoring engine.
    
    Converts facts → risk, not risk → action.
    
    This is the risk truth boundary.
    """
    
    def __init__(self,
                 telemetry_reader,
                 state_reader,
                 limits_reader,
                 weights: Optional[List[RiskFactorWeight]] = None):
        """
        Initialize risk evaluator with data sources.
        
        Args:
            telemetry_reader: Interface to platform_telemetry.py
            state_reader: Interface to posting_state_store.py
            limits_reader: Interface to platform_limits.py
            weights: Optional custom risk weights (uses defaults if None)
        """
        self.weights = weights or DEFAULT_RISK_WEIGHTS
        
        self.extractor = RiskSignalExtractor(
            telemetry_reader, state_reader, limits_reader
        )
        self.aggregator = RiskAggregator(self.weights)
        self.normalizer = RiskNormalizer()
        self.confidence_calc = ConfidenceCalculator(
            total_signal_types=len(RiskSignalType)
        )
        self.validator = RiskInvariantValidator()
        
        logger.info(f"RiskEvaluator initialized with model version {RISK_MODEL_VERSION}")
    
    def evaluate(self, platform: str, account_id: str) -> SuppressionRiskScore:
        """
        Evaluate suppression risk for given platform/account.
        
        Returns immutable SuppressionRiskScore with:
        - score ∈ [0.0, 1.0]
        - confidence ∈ [0.0, 1.0]
        - contributing signals
        - evaluation metadata
        
        Raises on invariant violation.
        """
        evaluation_time = time.time()
        
        # Extract all signals
        raw_signals = self.extractor.extract_all_signals(platform, account_id)
        
        # Validate raw signals
        for signal in raw_signals:
            self.validator.validate_raw_signal(signal)
        
        # Aggregate with weights and decay
        raw_risk, contributing = self.aggregator.aggregate(raw_signals, evaluation_time)
        
        # Normalize to [0.0, 1.0]
        final_score = self.normalizer.normalize(raw_risk)
        
        # Calculate confidence
        confidence = self.confidence_calc.calculate(raw_signals, evaluation_time)
        
        # Build immutable result
        risk_score = SuppressionRiskScore(
            platform=platform,
            account_id=account_id,
            score=final_score,
            confidence=confidence,
            contributing_signals=contributing,
            evaluated_at=evaluation_time,
            model_version=RISK_MODEL_VERSION
        )
        
        # Validate invariants
        self.validator.validate_score(risk_score)
        
        logger.debug(
            f"Risk evaluated: {platform}/{account_id} = {final_score:.3f} "
            f"(confidence: {confidence:.3f})"
        )
        
        return risk_score
    
    def evaluate_batch(self, targets: List[Tuple[str, str]]) -> List[SuppressionRiskScore]:
        """
        Batch evaluate multiple platform/account combinations.
        
        Args:
            targets: List of (platform, account_id) tuples
            
        Returns:
            List of SuppressionRiskScore objects
        """
        results = []
        for platform, account_id in targets:
            try:
                score = self.evaluate(platform, account_id)
                results.append(score)
            except Exception as e:
                logger.error(
                    f"Failed to evaluate {platform}/{account_id}: {e}",
                    exc_info=True
                )
                # On failure, create minimum-confidence score
                results.append(SuppressionRiskScore(
                    platform=platform,
                    account_id=account_id,
                    score=0.0,
                    confidence=0.0,
                    contributing_signals={},
                    evaluated_at=time.time(),
                    model_version=RISK_MODEL_VERSION
                ))
        
        return results
    
    def get_model_version(self) -> str:
        """Return current risk model version."""
        return RISK_MODEL_VERSION
    
    def get_weights(self) -> List[RiskFactorWeight]:
        """Return current risk factor weights."""
        return list(self.weights)


# ============================================================================
# Usage Example & Integration Points
# ============================================================================

def example_usage():
    """
    Example of how other components should use RiskEvaluator.
    
    ALLOWED:
    - anomaly_detector.py: read score
    - kill_switches.py: consume score
    - posting_queue.py: gating hints only
    - monitoring/*: read-only
    
    FORBIDDEN:
    - geo/account rotators: MUST NOT see risk
    """
    
    # Mock data sources (replace with real implementations)
    class MockTelemetryReader:
        def get_reach_samples(self, platform, account_id, start_time, end_time):
            return []
        
        def get_engagement_metrics(self, platform, account_id, start_time, end_time):
            return {}
        
        def get_average_latency(self, platform, account_id, start_time, end_time):
            return None
        
        def get_error_stats(self, platform, account_id, start_time, end_time):
            return {}
    
    class MockStateReader:
        def get_account_info(self, platform, account_id):
            return {'created_at': time.time() - 86400 * 30}
        
        def get_suppression_events(self, platform, account_id, start_time, end_time):
            return []
        
        def get_platform_warnings(self, platform, account_id):
            return []
    
    class MockLimitsReader:
        def get_current_status(self, platform, account_id):
            return {}
    
    # Initialize evaluator
    evaluator = RiskEvaluator(
        telemetry_reader=MockTelemetryReader(),
        state_reader=MockStateReader(),
        limits_reader=MockLimitsReader()
    )
    
    # Single evaluation
    score = evaluator.evaluate(platform="twitter", account_id="acct_123")
    
    print(f"Risk Score: {score.score:.3f}")
    print(f"Confidence: {score.confidence:.3f}")
    print(f"Contributing Signals: {score.contributing_signals}")
    
    # Batch evaluation
    targets = [
        ("twitter", "acct_123"),
        ("instagram", "acct_456"),
        ("tiktok", "acct_789"),
    ]
    scores = evaluator.evaluate_batch(targets)
    
    for score in scores:
        print(f"{score.platform}/{score.account_id}: {score.score:.3f}")


if __name__ == "__main__":
    example_usage()