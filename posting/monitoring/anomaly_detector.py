"""
/posting/monitoring/anomaly_detector.py

Cross-Account & Cross-Platform Anomaly Authority

Detects patterns that are individually valid but collectively impossible,
dangerous, or adversarial. This file sits ABOVE all single-source signals
and correlates across time, accounts, platforms, and intents.

CRITICAL: This file NEVER writes truth. It questions truth consistency.

================================================================================
TIER-0 COMPLIANCE SPECIFICATION
================================================================================

This file implements a production-maximum, invariant-locked anomaly detection
system for cross-system integrity monitoring.

ARCHITECTURE:
- AnomalyDetector: Core orchestration engine
- BaselineModel: Multi-scale time-weighted baselines (hourly, daily, weekly)
- CorrelationEngine: Advanced signal correlation with lag detection
- ThresholdPolicy: Account-tier and time-adaptive thresholds
- AnomalyInvariantValidator: Comprehensive validation rules
- AnomalyEscalationEmitter: Structured event emission with rate limiting
- SignalQualityScorer: Signal quality assessment and filtering

DETECTION METHODS:
1. detect_cross_account(): Correlated degradation across accounts
2. detect_cross_platform(): Platform divergence beyond norms
3. detect_temporal(): Latency shifts and failure clustering
4. detect_behavioral(): Shadow-ban detection (policy-compliant punishment)
5. detect_trust_decay_cluster(): Trust decay pattern clustering
6. detect_metric_platform_conflict(): Platform metric vs actual conflict

ANOMALY TYPES:
- CROSS_ACCOUNT_DEGRADATION
- CROSS_PLATFORM_DIVERGENCE
- TEMPORAL_LATENCY_SHIFT
- TRUST_DECAY_CLUSTER
- VISIBILITY_COLLAPSE
- METRIC_PLATFORM_CONFLICT

INTEGRATION (Read-Only):
- TrustSignalRecorderInterface: Trust score access
- CadenceMemoryInterface: Cadence compliance access
- PostingStateStoreInterface: State summary access

DETERMINISM:
- Identical signal streams produce identical events
- No randomness in detection logic
- Fully reproducible results

LOC: ~2,300-3,400 (Tier-0 requirement)
"""

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional, Dict, List, Set, Tuple, Any, Callable
from collections import defaultdict, deque
from itertools import combinations
import time
import math
import statistics
import json
import hashlib
import logging
from abc import ABC, abstractmethod

# Configure logging
logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================


@dataclass
class AnomalyDetectorConfig:
    """Centralized configuration for anomaly detector."""
    # Baseline model configuration
    decay_factor: float = 0.95
    min_samples: int = 10
    spike_resistance_window: float = 300.0  # 5 minutes
    spike_threshold_multiplier: float = 3.0
    
    # Correlation engine configuration
    correlation_time_window: float = 300.0  # 5 minutes
    max_lag: float = 3600.0  # 1 hour
    
    # Threshold policy configuration
    temporal_coherence_window: float = 300.0  # 5 minutes
    
    # Invariant validator configuration
    cooldown_seconds: float = 300.0  # 5 minutes
    min_signal_quality: float = 0.3
    window_duration: float = 3600.0  # 1 hour
    max_events_per_window: int = 3
    re_entry_cooldown_base: float = 600.0  # 10 minutes
    re_entry_cooldown_multiplier: float = 2.0
    severity_change_cooldown: float = 1800.0  # 30 minutes
    decay_window: float = 1800.0  # 30 minutes
    
    # Memory limits
    max_event_log_size: int = 10000
    max_signal_buffer_size: int = 50000
    
    # Evaluation configuration
    evaluation_interval: float = 60.0  # Evaluate every 60s
    signal_threshold: int = 10  # Minimum signals before evaluation
    
    # Rate limiting
    rate_limit_per_minute: int = 10


# ============================================================================
# CORE DATA CONTRACTS
# ============================================================================


class AnomalyType(Enum):
    """Exhaustive enumeration of structural anomaly types."""
    CROSS_ACCOUNT_DEGRADATION = "cross_account_degradation"
    CROSS_PLATFORM_DIVERGENCE = "cross_platform_divergence"
    TEMPORAL_LATENCY_SHIFT = "temporal_latency_shift"
    TRUST_DECAY_CLUSTER = "trust_decay_cluster"
    VISIBILITY_COLLAPSE = "visibility_collapse"
    METRIC_PLATFORM_CONFLICT = "metric_platform_conflict"


class AnomalySeverity(Enum):
    """Severity levels for anomaly events."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    SYSTEMIC = "systemic"  # Implies global action eligibility


class AnomalyLifecycle(Enum):
    """Lifecycle state for anomaly events - formal state machine."""
    QUIET = "quiet"  # No active anomaly
    ACTIVE = "active"  # Currently firing
    COOLDOWN = "cooldown"  # In cooldown after fire
    DECAYING = "decaying"  # In decay window


@dataclass(frozen=True)
class AnomalySignal:
    """
    Input signal from monitoring subsystems.
    Signals are inputs, not conclusions.
    """
    source: str  # File or subsystem that generated this signal
    timestamp: float

    platform: Optional[str]
    account_id: Optional[str]
    intent_id: Optional[str]

    metric_name: str
    metric_value: float
    baseline_value: float

    confidence: float  # 0.0 to 1.0

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Confidence must be [0, 1], got {self.confidence}")
        if self.timestamp <= 0:
            raise ValueError(f"Timestamp must be positive, got {self.timestamp}")


# ============================================================================
# INDEPENDENCE FUNCTION
# ============================================================================


def are_independent(a: AnomalySignal, b: AnomalySignal) -> bool:
    """
    Mechanical definition of signal independence.
    
    Two signals are independent if:
    1. Different sources
    2. Different (account, platform) pair OR different metric
    3. Different metric name
    
    This ensures signals come from different perspectives and cannot
    be correlated through shared systemic failure.
    """
    return (
        a.source != b.source and
        (a.account_id != b.account_id or a.platform != b.platform) and
        a.metric_name != b.metric_name
    )


@dataclass(frozen=True)
class AnomalyEvent:
    """
    Correlated judgment about system-wide anomaly.
    Events are emitted conclusions, not raw signals.
    """
    anomaly_type: AnomalyType
    severity: AnomalySeverity

    detected_at: float
    involved_accounts: Set[str]
    involved_platforms: Set[str]

    supporting_signals: Tuple[AnomalySignal, ...]
    confidence: float  # 0.0 to 1.0

    recommended_action: Optional[str]
    validated: bool = False  # Must be True for emission - set by validator

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Confidence must be [0, 1], got {self.confidence}")
        if len(self.supporting_signals) < 2:
            raise ValueError(f"Events require ≥2 signals, got {len(self.supporting_signals)}")


@dataclass
class BaselineSnapshot:
    """Time-weighted baseline for a specific metric."""
    metric_name: str
    scope: str  # e.g., "platform:youtube" or "account:abc123"
    
    mean: float
    std_dev: float
    sample_count: int
    
    last_updated: float
    decay_factor: float = 0.95  # Time-weighted decay


@dataclass
class CorrelationCluster:
    """Group of correlated signals forming a pattern."""
    signals: List[AnomalySignal]
    cluster_id: str
    correlation_score: float
    common_features: Dict[str, Any]
    temporal_span: float  # Time span of cluster
    metric_diversity: int  # Number of unique metrics
    source_diversity: int  # Number of unique sources


@dataclass
class AccountTier:
    """Account tier classification for threshold adaptation."""
    tier_name: str  # "premium", "standard", "probe"
    trust_score_range: Tuple[float, float]
    threshold_multiplier: float  # Multiplier for base thresholds


@dataclass
class TimeAdaptiveThreshold:
    """Time-adaptive threshold configuration."""
    base_threshold: float
    time_of_day_multipliers: Dict[int, float]  # Hour -> multiplier
    day_of_week_multipliers: Dict[int, float]  # 0=Monday -> multiplier
    seasonal_adjustments: Dict[str, float]  # Season -> multiplier


@dataclass
class SignalQuality:
    """Quality metrics for a signal."""
    freshness_score: float  # 0.0 to 1.0
    source_reliability: float  # 0.0 to 1.0
    metric_consistency: float  # 0.0 to 1.0
    overall_quality: float  # 0.0 to 1.0


@dataclass
class TrustDecayPattern:
    """Pattern of trust decay across accounts."""
    account_ids: Set[str]
    decay_rate: float  # Rate of decay per hour
    initial_trust: float
    current_trust: float
    detected_at: float
    confidence: float


# ============================================================================
# BASELINE MODEL
# ============================================================================


class BaselineModel:
    """
    Defines 'normal' per account, per platform, per content class.
    
    Characteristics:
    - Time-weighted
    - Decay-aware
    - Resistant to short spikes
    - Multi-scale baselines (hourly, daily, weekly)
    - Seasonal adjustments
    """
    
    def __init__(self, config: Optional[AnomalyDetectorConfig] = None, decay_factor: float = 0.95, min_samples: int = 10):
        self.config = config or AnomalyDetectorConfig()
        self.decay_factor = self.config.decay_factor if config else decay_factor
        self.min_samples = self.config.min_samples if config else min_samples
        self._baselines: Dict[str, BaselineSnapshot] = {}
        self._sample_buffers: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._hourly_baselines: Dict[str, Dict[int, BaselineSnapshot]] = defaultdict(dict)
        self._daily_baselines: Dict[str, Dict[int, BaselineSnapshot]] = defaultdict(dict)
        self._weekly_baselines: Dict[str, Dict[int, BaselineSnapshot]] = defaultdict(dict)  # Week of year
        self._content_class_baselines: Dict[str, Dict[str, BaselineSnapshot]] = defaultdict(dict)
        self._account_tier_baselines: Dict[str, Dict[str, BaselineSnapshot]] = defaultdict(dict)  # tier -> baseline
        self._platform_account_baselines: Dict[str, Dict[str, BaselineSnapshot]] = defaultdict(dict)  # platform -> account -> baseline
        self._spike_resistance_window: float = self.config.spike_resistance_window
        self._spike_threshold_multiplier: float = self.config.spike_threshold_multiplier
    
    def update(self, scope: str, metric_name: str, value: float, timestamp: float, 
               content_class: Optional[str] = None, account_tier: Optional[str] = None,
               platform: Optional[str] = None, account_id: Optional[str] = None):
        """Update baseline with new observation (read-only snapshots) with spike resistance."""
        key = f"{scope}:{metric_name}"
        
        # Spike detection and resistance
        existing_baseline = self._baselines.get(key)
        if existing_baseline:
            z_score = abs((value - existing_baseline.mean) / max(existing_baseline.std_dev, 0.01))
            if z_score > self._spike_threshold_multiplier:
                # Potential spike - check if it's isolated
                recent_samples = [
                    (v, ts) for v, ts in self._sample_buffers[key]
                    if timestamp - ts < self._spike_resistance_window
                ]
                if len(recent_samples) < 2:
                    # Isolated spike - reduce weight
                    value = existing_baseline.mean + (value - existing_baseline.mean) * 0.3
        
        self._sample_buffers[key].append((value, timestamp))
        
        samples = self._sample_buffers[key]
        if len(samples) < self.min_samples:
            return  # Not enough data for baseline
        
        # Time-weighted calculation with outlier resistance
        now = time.time()
        weights = [self.decay_factor ** ((now - ts) / 3600) for _, ts in samples]
        values = [v for v, _ in samples]
        
        # Outlier-resistant mean (use median for very large deviations)
        weighted_mean = sum(v * w for v, w in zip(values, weights)) / sum(weights)
        
        # Check for outliers and use robust statistics if needed
        deviations = [abs(v - weighted_mean) for v in values]
        if deviations:
            median_deviation = statistics.median(deviations)
            if median_deviation > 0:
                # Use robust mean (trimmed mean) if outliers present
                sorted_values = sorted(zip(values, weights), key=lambda x: abs(x[0] - weighted_mean))
                trimmed_count = max(1, len(sorted_values) // 10)  # Trim 10% extremes
                trimmed_values = sorted_values[trimmed_count:-trimmed_count] if len(sorted_values) > trimmed_count * 2 else sorted_values
                if trimmed_values:
                    weighted_mean = sum(v * w for v, w in trimmed_values) / sum(w for _, w in trimmed_values)
        
        # Weighted standard deviation
        variance = sum(w * (v - weighted_mean) ** 2 for v, w in zip(values, weights)) / sum(weights)
        weighted_std = math.sqrt(variance) if variance > 0 else 0.01
        
        self._baselines[key] = BaselineSnapshot(
            metric_name=metric_name,
            scope=scope,
            mean=weighted_mean,
            std_dev=weighted_std,
            sample_count=len(samples),
            last_updated=now,
            decay_factor=self.decay_factor
        )
        
        # Update hourly baseline
        hour = int((timestamp % 86400) / 3600)
        self._update_hourly_baseline(key, hour, value, timestamp)
        
        # Update daily baseline
        day_of_week = int((timestamp // 86400) % 7)
        self._update_daily_baseline(key, day_of_week, value, timestamp)
        
        # Update weekly baseline (week of year)
        week_of_year = int((timestamp // 86400) // 7) % 52
        self._update_weekly_baseline(key, week_of_year, value, timestamp)
        
        # Update content class baseline if provided
        if content_class:
            content_key = f"{key}:content:{content_class}"
            self._update_content_class_baseline(content_key, value, timestamp)
        
        # Update account tier baseline if provided
        if account_tier:
            tier_key = f"{key}:tier:{account_tier}"
            if tier_key not in self._account_tier_baselines:
                self._account_tier_baselines[tier_key] = {}
            self._update_account_tier_baseline(tier_key, value, timestamp)
        
        # Update platform-account baseline if provided
        if platform and account_id:
            platform_account_key = f"{platform}:{account_id}:{metric_name}"
            if platform not in self._platform_account_baselines:
                self._platform_account_baselines[platform] = {}
            self._update_platform_account_baseline(platform, account_id, metric_name, value, timestamp)
    
    def _update_weekly_baseline(self, key: str, week_of_year: int, value: float, timestamp: float):
        """Update weekly baseline for seasonal patterns."""
        if key not in self._weekly_baselines:
            self._weekly_baselines[key] = {}
        
        if week_of_year not in self._weekly_baselines[key]:
            self._weekly_baselines[key][week_of_year] = BaselineSnapshot(
                metric_name=key,
                scope=f"{key}:week:{week_of_year}",
                mean=value,
                std_dev=value * 0.15,
                sample_count=1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
        else:
            baseline = self._weekly_baselines[key][week_of_year]
            alpha = 0.02  # Very slow adaptation for weekly patterns
            new_mean = alpha * value + (1 - alpha) * baseline.mean
            new_std = math.sqrt(alpha * (value - new_mean) ** 2 + (1 - alpha) * baseline.std_dev ** 2)
            
            self._weekly_baselines[key][week_of_year] = BaselineSnapshot(
                metric_name=baseline.metric_name,
                scope=baseline.scope,
                mean=new_mean,
                std_dev=max(new_std, 0.01),
                sample_count=baseline.sample_count + 1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
    
    def _update_account_tier_baseline(self, tier_key: str, value: float, timestamp: float):
        """Update account tier-specific baseline."""
        if tier_key not in self._account_tier_baselines:
            self._account_tier_baselines[tier_key] = BaselineSnapshot(
                metric_name=tier_key,
                scope=tier_key,
                mean=value,
                std_dev=value * 0.1,
                sample_count=1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
        else:
            baseline = self._account_tier_baselines[tier_key]
            alpha = 0.05
            new_mean = alpha * value + (1 - alpha) * baseline.mean
            new_std = math.sqrt(alpha * (value - new_mean) ** 2 + (1 - alpha) * baseline.std_dev ** 2)
            
            self._account_tier_baselines[tier_key] = BaselineSnapshot(
                metric_name=baseline.metric_name,
                scope=baseline.scope,
                mean=new_mean,
                std_dev=max(new_std, 0.01),
                sample_count=baseline.sample_count + 1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
    
    def _update_platform_account_baseline(self, platform: str, account_id: str, 
                                        metric_name: str, value: float, timestamp: float):
        """Update platform-account specific baseline."""
        if platform not in self._platform_account_baselines:
            self._platform_account_baselines[platform] = {}
        
        key = f"{account_id}:{metric_name}"
        if key not in self._platform_account_baselines[platform]:
            self._platform_account_baselines[platform][key] = BaselineSnapshot(
                metric_name=metric_name,
                scope=f"platform:{platform}:account:{account_id}",
                mean=value,
                std_dev=value * 0.1,
                sample_count=1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
        else:
            baseline = self._platform_account_baselines[platform][key]
            alpha = 0.1
            new_mean = alpha * value + (1 - alpha) * baseline.mean
            new_std = math.sqrt(alpha * (value - new_mean) ** 2 + (1 - alpha) * baseline.std_dev ** 2)
            
            self._platform_account_baselines[platform][key] = BaselineSnapshot(
                metric_name=baseline.metric_name,
                scope=baseline.scope,
                mean=new_mean,
                std_dev=max(new_std, 0.01),
                sample_count=baseline.sample_count + 1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
    
    def get_account_tier_baseline(self, scope: str, metric_name: str, 
                                  account_tier: str) -> Optional[BaselineSnapshot]:
        """Get account tier-specific baseline."""
        tier_key = f"{scope}:{metric_name}:tier:{account_tier}"
        return self._account_tier_baselines.get(tier_key)
    
    def get_platform_account_baseline(self, platform: str, account_id: str, 
                                     metric_name: str) -> Optional[BaselineSnapshot]:
        """Get platform-account specific baseline."""
        if platform not in self._platform_account_baselines:
            return None
        key = f"{account_id}:{metric_name}"
        return self._platform_account_baselines[platform].get(key)
    
    def _update_hourly_baseline(self, key: str, hour: int, value: float, timestamp: float):
        """Update hourly baseline for time-of-day patterns."""
        if key not in self._hourly_baselines:
            self._hourly_baselines[key] = {}
        
        if hour not in self._hourly_baselines[key]:
            self._hourly_baselines[key][hour] = BaselineSnapshot(
                metric_name=key,
                scope=f"{key}:hour:{hour}",
                mean=value,
                std_dev=value * 0.1,
                sample_count=1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
        else:
            baseline = self._hourly_baselines[key][hour]
            # Exponential moving average
            alpha = 0.1
            new_mean = alpha * value + (1 - alpha) * baseline.mean
            new_std = math.sqrt(alpha * (value - new_mean) ** 2 + (1 - alpha) * baseline.std_dev ** 2)
            
            self._hourly_baselines[key][hour] = BaselineSnapshot(
                metric_name=baseline.metric_name,
                scope=baseline.scope,
                mean=new_mean,
                std_dev=max(new_std, 0.01),
                sample_count=baseline.sample_count + 1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
    
    def _update_daily_baseline(self, key: str, day_of_week: int, value: float, timestamp: float):
        """Update daily baseline for day-of-week patterns."""
        if key not in self._daily_baselines:
            self._daily_baselines[key] = {}
        
        if day_of_week not in self._daily_baselines[key]:
            self._daily_baselines[key][day_of_week] = BaselineSnapshot(
                metric_name=key,
                scope=f"{key}:day:{day_of_week}",
                mean=value,
                std_dev=value * 0.1,
                sample_count=1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
        else:
            baseline = self._daily_baselines[key][day_of_week]
            alpha = 0.05  # Slower adaptation for weekly patterns
            new_mean = alpha * value + (1 - alpha) * baseline.mean
            new_std = math.sqrt(alpha * (value - new_mean) ** 2 + (1 - alpha) * baseline.std_dev ** 2)
            
            self._daily_baselines[key][day_of_week] = BaselineSnapshot(
                metric_name=baseline.metric_name,
                scope=baseline.scope,
                mean=new_mean,
                std_dev=max(new_std, 0.01),
                sample_count=baseline.sample_count + 1,
                last_updated=timestamp,
                decay_factor=self.decay_factor
            )
    
    def _update_content_class_baseline(self, content_key: str, value: float, timestamp: float):
        """Update content class-specific baseline."""
        if content_key not in self._content_class_baselines:
            self._content_class_baselines[content_key] = {}
        
        # Simplified: use same structure as main baseline
        if content_key not in self._content_class_baselines:
            self._content_class_baselines[content_key] = BaselineSnapshot(
                metric_name=content_key,
                scope=content_key,
                mean=value,
                std_dev=value * 0.1,
                sample_count=1,
                last_updated=timestamp,
            decay_factor=self.decay_factor
        )
    
    def get_baseline(self, scope: str, metric_name: str) -> Optional[BaselineSnapshot]:
        """Retrieve baseline snapshot."""
        key = f"{scope}:{metric_name}"
        return self._baselines.get(key)
    
    def get_time_adaptive_baseline(self, scope: str, metric_name: str, 
                                   timestamp: float) -> Optional[BaselineSnapshot]:
        """Get baseline adjusted for time-of-day and day-of-week patterns."""
        base_baseline = self.get_baseline(scope, metric_name)
        if not base_baseline:
            return None
        
        key = f"{scope}:{metric_name}"
        hour = int((timestamp % 86400) / 3600)
        day_of_week = int((timestamp // 86400) % 7)
        
        # Get hourly and daily baselines if available
        hourly = self._hourly_baselines.get(key, {}).get(hour)
        daily = self._daily_baselines.get(key, {}).get(day_of_week)
        
        # Weighted combination: 50% base, 30% hourly, 20% daily
        if hourly and daily:
            adjusted_mean = (0.5 * base_baseline.mean + 
                           0.3 * hourly.mean + 
                           0.2 * daily.mean)
            adjusted_std = math.sqrt(0.5 * base_baseline.std_dev ** 2 +
                                   0.3 * hourly.std_dev ** 2 +
                                   0.2 * daily.std_dev ** 2)
        elif hourly:
            adjusted_mean = 0.7 * base_baseline.mean + 0.3 * hourly.mean
            adjusted_std = math.sqrt(0.7 * base_baseline.std_dev ** 2 +
                                   0.3 * hourly.std_dev ** 2)
        elif daily:
            adjusted_mean = 0.8 * base_baseline.mean + 0.2 * daily.mean
            adjusted_std = math.sqrt(0.8 * base_baseline.std_dev ** 2 +
                                   0.2 * daily.std_dev ** 2)
        else:
            return base_baseline
        
        return BaselineSnapshot(
            metric_name=metric_name,
            scope=f"{scope}:adaptive",
            mean=adjusted_mean,
            std_dev=max(adjusted_std, 0.01),
            sample_count=base_baseline.sample_count,
            last_updated=timestamp,
            decay_factor=base_baseline.decay_factor
        )
    
    def calculate_deviation(self, scope: str, metric_name: str, value: float, 
                           timestamp: Optional[float] = None) -> Optional[float]:
        """Calculate z-score deviation from baseline."""
        if timestamp:
            baseline = self.get_time_adaptive_baseline(scope, metric_name, timestamp)
        else:
            baseline = self.get_baseline(scope, metric_name)
        
        if not baseline or baseline.std_dev == 0:
            return None
        
        z_score = (value - baseline.mean) / baseline.std_dev
        return z_score
    
    def snapshot(self) -> 'BaselineModelSnapshot':
        """
        Create a frozen snapshot of all baselines.
        
        Baselines are read-only snapshots, never learned inline.
        This method returns a frozen view that can be safely consumed
        by the detector without mutation concerns.
        """
        return BaselineModelSnapshot(
            baselines=dict(self._baselines),
            hourly_baselines={k: dict(v) for k, v in self._hourly_baselines.items()},
            daily_baselines={k: dict(v) for k, v in self._daily_baselines.items()},
            weekly_baselines={k: dict(v) for k, v in self._weekly_baselines.items()},
            content_class_baselines={k: dict(v) for k, v in self._content_class_baselines.items()},
            account_tier_baselines=dict(self._account_tier_baselines),
            platform_account_baselines={
                platform: dict(accounts) 
                for platform, accounts in self._platform_account_baselines.items()
            }
        )


@dataclass(frozen=True)
class BaselineModelSnapshot:
    """Frozen snapshot of baselines for read-only consumption."""
    baselines: Dict[str, BaselineSnapshot]
    hourly_baselines: Dict[str, Dict[int, BaselineSnapshot]]
    daily_baselines: Dict[str, Dict[int, BaselineSnapshot]]
    weekly_baselines: Dict[str, Dict[int, BaselineSnapshot]]
    content_class_baselines: Dict[str, Dict[str, BaselineSnapshot]]
    account_tier_baselines: Dict[str, BaselineSnapshot]
    platform_account_baselines: Dict[str, Dict[str, BaselineSnapshot]]
    
    def get_baseline(self, scope: str, metric_name: str) -> Optional[BaselineSnapshot]:
        """Retrieve baseline snapshot."""
        key = f"{scope}:{metric_name}"
        return self.baselines.get(key)
    
    def get_time_adaptive_baseline(self, scope: str, metric_name: str, 
                                   timestamp: float) -> Optional[BaselineSnapshot]:
        """Get baseline adjusted for time-of-day and day-of-week patterns."""
        base_baseline = self.get_baseline(scope, metric_name)
        if not base_baseline:
            return None
        
        key = f"{scope}:{metric_name}"
        hour = int((timestamp % 86400) / 3600)
        day_of_week = int((timestamp // 86400) % 7)
        
        # Get hourly and daily baselines if available
        hourly = self.hourly_baselines.get(key, {}).get(hour)
        daily = self.daily_baselines.get(key, {}).get(day_of_week)
        
        # Weighted combination: 50% base, 30% hourly, 20% daily
        if hourly and daily:
            adjusted_mean = (0.5 * base_baseline.mean + 
                           0.3 * hourly.mean + 
                           0.2 * daily.mean)
            adjusted_std = math.sqrt(0.5 * base_baseline.std_dev ** 2 +
                                   0.3 * hourly.std_dev ** 2 +
                                   0.2 * daily.std_dev ** 2)
        elif hourly:
            adjusted_mean = 0.7 * base_baseline.mean + 0.3 * hourly.mean
            adjusted_std = math.sqrt(0.7 * base_baseline.std_dev ** 2 +
                                   0.3 * hourly.std_dev ** 2)
        elif daily:
            adjusted_mean = 0.8 * base_baseline.mean + 0.2 * daily.mean
            adjusted_std = math.sqrt(0.8 * base_baseline.std_dev ** 2 +
                                   0.2 * daily.std_dev ** 2)
        else:
            return base_baseline
        
        return BaselineSnapshot(
            metric_name=metric_name,
            scope=f"{scope}:adaptive",
            mean=adjusted_mean,
            std_dev=max(adjusted_std, 0.01),
            sample_count=base_baseline.sample_count,
            last_updated=timestamp,
            decay_factor=base_baseline.decay_factor
        )
    
    def get_account_tier_baseline(self, scope: str, metric_name: str, 
                                  account_tier: str) -> Optional[BaselineSnapshot]:
        """Get account tier-specific baseline."""
        tier_key = f"{scope}:{metric_name}:tier:{account_tier}"
        return self.account_tier_baselines.get(tier_key)
    
    def get_platform_account_baseline(self, platform: str, account_id: str, 
                                     metric_name: str) -> Optional[BaselineSnapshot]:
        """Get platform-account specific baseline."""
        if platform not in self.platform_account_baselines:
            return None
        key = f"{account_id}:{metric_name}"
        return self.platform_account_baselines[platform].get(key)
    
    def calculate_deviation(self, scope: str, metric_name: str, value: float, 
                           timestamp: Optional[float] = None) -> Optional[float]:
        """Calculate z-score deviation from baseline."""
        if timestamp:
            baseline = self.get_time_adaptive_baseline(scope, metric_name, timestamp)
        else:
            baseline = self.get_baseline(scope, metric_name)
        
        if not baseline or baseline.std_dev == 0:
            return None
        
        z_score = (value - baseline.mean) / baseline.std_dev
        return z_score


# ============================================================================
# CORRELATION ENGINE
# ============================================================================


class CorrelationEngine:
    """
    Responsibilities:
    - Cross-signal clustering
    - Causality hints (not claims)
    - Confidence scoring
    - Cross-metric correlation
    - Lag detection
    - Pattern matching
    
    No ML black boxes unless auditable.
    """
    
    def __init__(self, config: Optional[AnomalyDetectorConfig] = None):
        self.config = config or AnomalyDetectorConfig()
        self._correlation_threshold = 0.7
        self._lag_detection_window = 3600.0  # 1 hour
    
    def find_temporal_clusters(
        self, 
        signals: List[AnomalySignal], 
        time_window: Optional[float] = None
    ) -> List[CorrelationCluster]:
        """Group signals by temporal proximity with advanced clustering."""
        if not signals:
            return []
        
        if time_window is None:
            time_window = self.config.correlation_time_window
        
        sorted_signals = sorted(signals, key=lambda s: s.timestamp)
        clusters = []
        current_cluster = [sorted_signals[0]]
        
        for signal in sorted_signals[1:]:
            if signal.timestamp - current_cluster[-1].timestamp <= time_window:
                current_cluster.append(signal)
            else:
                if len(current_cluster) >= 2:
                    clusters.append(self._create_cluster(current_cluster, "temporal"))
                current_cluster = [signal]
        
        if len(current_cluster) >= 2:
            clusters.append(self._create_cluster(current_cluster, "temporal"))
        
        return clusters
    
    def find_account_clusters(self, signals: List[AnomalySignal]) -> List[CorrelationCluster]:
        """Group signals by common account degradation with cross-account correlation."""
        account_groups = defaultdict(list)
        
        for signal in signals:
            if signal.account_id:
                account_groups[signal.account_id].append(signal)
        
        clusters = []
        
        # Single-account clusters
        for account_id, group in account_groups.items():
            if len(group) >= 2:
                clusters.append(self._create_cluster(group, "account", {"account_id": account_id}))
        
        # Cross-account correlation (multiple accounts showing similar patterns)
        if len(account_groups) >= 2:
            cross_account_clusters = self._find_cross_account_correlation(account_groups)
            clusters.extend(cross_account_clusters)
        
        return clusters
    
    def _find_cross_account_correlation(
        self, 
        account_groups: Dict[str, List[AnomalySignal]]
    ) -> List[CorrelationCluster]:
        """Find correlated patterns across multiple accounts."""
        clusters = []
        account_ids = list(account_groups.keys())
        
        # Compare pairs of accounts for similar degradation patterns
        for i, acc1 in enumerate(account_ids):
            for acc2 in account_ids[i+1:]:
                signals1 = account_groups[acc1]
                signals2 = account_groups[acc2]
                
                # Find matching metrics
                metrics1 = {s.metric_name for s in signals1}
                metrics2 = {s.metric_name for s in signals2}
                common_metrics = metrics1 & metrics2
                
                if len(common_metrics) >= 1:
                    # Check for similar deviation patterns
                    for metric in common_metrics:
                        sigs1 = [s for s in signals1 if s.metric_name == metric]
                        sigs2 = [s for s in signals2 if s.metric_name == metric]
                        
                        if len(sigs1) >= 1 and len(sigs2) >= 1:
                            # Calculate deviation similarity
                            dev1 = [(s.metric_value - s.baseline_value) / max(s.baseline_value, 0.01) 
                                   for s in sigs1]
                            dev2 = [(s.metric_value - s.baseline_value) / max(s.baseline_value, 0.01) 
                                   for s in sigs2]
                            
                            avg_dev1 = statistics.mean(dev1) if dev1 else 0
                            avg_dev2 = statistics.mean(dev2) if dev2 else 0
                            
                            # Similar degradation pattern
                            if abs(avg_dev1 - avg_dev2) < 0.2 and (avg_dev1 < -0.3 or avg_dev2 < -0.3):
                                combined_signals = sigs1 + sigs2
                                clusters.append(self._create_cluster(
                                    combined_signals, 
                                    "cross_account",
                                    {"account_ids": {acc1, acc2}, "metric": metric}
                                ))
        
        return clusters
    
    def find_platform_clusters(self, signals: List[AnomalySignal]) -> List[CorrelationCluster]:
        """Group signals by platform divergence with advanced analysis."""
        platform_groups = defaultdict(list)
        
        for signal in signals:
            if signal.platform:
                platform_groups[signal.platform].append(signal)
        
        clusters = []
        for platform, group in platform_groups.items():
            if len(group) >= 2:
                clusters.append(self._create_cluster(group, "platform", {"platform": platform}))
        
        return clusters
    
    def find_cross_metric_correlation(
        self, 
        signals: List[AnomalySignal],
        time_window: Optional[float] = None
    ) -> List[CorrelationCluster]:
        """Find correlations between different metrics."""
        if time_window is None:
            time_window = self.config.correlation_time_window
        
        clusters = []
        metric_groups = defaultdict(list)
        
        for signal in signals:
            metric_groups[signal.metric_name].append(signal)
        
        metrics = list(metric_groups.keys())
        
        # Compare pairs of metrics for correlation
        for i, metric1 in enumerate(metrics):
            for metric2 in metrics[i+1:]:
                sigs1 = metric_groups[metric1]
                sigs2 = metric_groups[metric2]
                
                # Find temporally aligned signals
                aligned_pairs = []
                for s1 in sigs1:
                    for s2 in sigs2:
                        if abs(s1.timestamp - s2.timestamp) <= time_window:
                            if (s1.account_id == s2.account_id or 
                                (s1.platform == s2.platform and s1.account_id and s2.account_id)):
                                aligned_pairs.append((s1, s2))
                
                if len(aligned_pairs) >= 2:
                    # Calculate correlation coefficient
                    values1 = [s1.metric_value / max(s1.baseline_value, 0.01) for s1, _ in aligned_pairs]
                    values2 = [s2.metric_value / max(s2.baseline_value, 0.01) for _, s2 in aligned_pairs]
                    
                    if len(values1) >= 2:
                        try:
                            correlation = self._pearson_correlation(values1, values2)
                            if abs(correlation) > 0.6:  # Strong correlation
                                combined_signals = [s1 for s1, _ in aligned_pairs] + [s2 for _, s2 in aligned_pairs]
                                clusters.append(self._create_cluster(
                                    combined_signals,
                                    "cross_metric",
                                    {"metrics": {metric1, metric2}, "correlation": correlation}
                                ))
                        except (ValueError, ZeroDivisionError, ArithmeticError) as e:
                            # Log correlation calculation errors but continue processing
                            logger.debug(f"Correlation calculation failed for metrics {metric1}/{metric2}: {e}")
                            pass
        
        return clusters
    
    def _pearson_correlation(self, x: List[float], y: List[float]) -> float:
        """Calculate Pearson correlation coefficient."""
        if len(x) != len(y) or len(x) < 2:
            return 0.0
        
        mean_x = statistics.mean(x)
        mean_y = statistics.mean(y)
        
        numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(len(x)))
        denom_x = math.sqrt(sum((x[i] - mean_x) ** 2 for i in range(len(x))))
        denom_y = math.sqrt(sum((y[i] - mean_y) ** 2 for i in range(len(y))))
        
        if denom_x == 0 or denom_y == 0:
            return 0.0
        
        return numerator / (denom_x * denom_y)
    
    def detect_lag_patterns(
        self, 
        signals: List[AnomalySignal],
        max_lag: Optional[float] = None
    ) -> List[Tuple[AnomalySignal, AnomalySignal, float]]:
        """Detect lag patterns between signals (causality hints)."""
        if max_lag is None:
            max_lag = self.config.max_lag
        
        lag_pairs = []
        sorted_signals = sorted(signals, key=lambda s: s.timestamp)
        
        for i, s1 in enumerate(sorted_signals):
            for s2 in sorted_signals[i+1:]:
                if s2.timestamp - s1.timestamp > max_lag:
                    break
                
                # Same metric, different accounts/platforms
                if (s1.metric_name == s2.metric_name and 
                    (s1.account_id != s2.account_id or s1.platform != s2.platform)):
                    lag = s2.timestamp - s1.timestamp
                    # Similar deviation pattern suggests propagation
                    dev1 = (s1.metric_value - s1.baseline_value) / max(s1.baseline_value, 0.01)
                    dev2 = (s2.metric_value - s2.baseline_value) / max(s2.baseline_value, 0.01)
                    
                    if abs(dev1 - dev2) < 0.2 and abs(dev1) > 0.3:
                        lag_pairs.append((s1, s2, lag))
        
        return lag_pairs
    
    def calculate_cluster_confidence(self, cluster: CorrelationCluster, 
                                    baseline_snapshot: Optional[BaselineModelSnapshot] = None) -> float:
        """
        Calculate confidence score for a correlation cluster.
        
        Uses multi-stage confidence if baseline_snapshot is provided,
        otherwise falls back to simpler calculation.
        """
        if baseline_snapshot:
            return self.calculate_multi_stage_confidence(cluster, baseline_snapshot)
        
        # Fallback: simpler calculation
        if not cluster.signals:
            return 0.0
        
        # Average individual signal confidence
        avg_confidence = sum(s.confidence for s in cluster.signals) / len(cluster.signals)
        
        # Boost for more signals (up to 1.3x)
        signal_boost = min(1.3, 1.0 + (len(cluster.signals) - 2) * 0.05)
        
        # Boost for source diversity
        source_boost = 1.0 + (cluster.source_diversity - 1) * 0.1
        
        # Correlation score contribution
        final_confidence = min(1.0, avg_confidence * signal_boost * source_boost * cluster.correlation_score)
        
        return final_confidence
    
    def find_multi_dimensional_clusters(
        self,
        signals: List[AnomalySignal],
        dimensions: List[str] = ["account", "platform", "metric", "temporal"]
    ) -> List[CorrelationCluster]:
        """
        Find clusters across multiple dimensions simultaneously.
        
        This is the deep correlation analysis that detects complex patterns
        that simple single-dimension clustering misses.
        """
        if not signals:
            return []
        
        clusters = []
        
        # Build dimension indices
        dimension_groups = {}
        if "account" in dimensions:
            dimension_groups["account"] = defaultdict(list)
            for s in signals:
                if s.account_id:
                    dimension_groups["account"][s.account_id].append(s)
        
        if "platform" in dimensions:
            dimension_groups["platform"] = defaultdict(list)
            for s in signals:
                if s.platform:
                    dimension_groups["platform"][s.platform].append(s)
        
        if "metric" in dimensions:
            dimension_groups["metric"] = defaultdict(list)
            for s in signals:
                dimension_groups["metric"][s.metric_name].append(s)
        
        # Find intersections across dimensions
        if "account" in dimension_groups and "platform" in dimension_groups:
            for account_id, account_sigs in dimension_groups["account"].items():
                for platform, platform_sigs in dimension_groups["platform"].items():
                    intersection = [s for s in account_sigs if s in platform_sigs]
                    if len(intersection) >= 2:
                        clusters.append(self._create_cluster(
                            intersection,
                            "multi_dim",
                            {"account": account_id, "platform": platform}
                        ))
        
        # Temporal + metric correlation
        if "temporal" in dimensions and "metric" in dimensions:
            temporal_clusters = self.find_temporal_clusters(signals)
            for temp_cluster in temporal_clusters:
                metric_groups = defaultdict(list)
                for s in temp_cluster.signals:
                    metric_groups[s.metric_name].append(s)
                
                # Find metrics that co-occur in temporal clusters
                for metric, metric_sigs in metric_groups.items():
                    if len(metric_sigs) >= 2:
                        clusters.append(self._create_cluster(
                            metric_sigs,
                            "temporal_metric",
                            {"metric": metric, "temporal_span": temp_cluster.temporal_span}
                        ))
        
        return clusters
    
    def calculate_multi_stage_confidence(
        self,
        cluster: CorrelationCluster,
        baseline_snapshot: Optional[BaselineModelSnapshot] = None
    ) -> float:
        """
        Multi-stage confidence scoring with reinforcement.
        
        Stages:
        1. Signal-level confidence (individual signal quality)
        2. Cluster-level coherence (pattern strength)
        3. Baseline consistency (deviation from normal)
        4. Cross-validation (independent verification)
        """
        if not cluster.signals:
            return 0.0
        
        # Stage 1: Signal-level confidence
        signal_confidences = [s.confidence for s in cluster.signals]
        stage1_confidence = statistics.mean(signal_confidences)
        
        # Stage 2: Cluster-level coherence
        # Metric alignment
        metric_names = [s.metric_name for s in cluster.signals]
        unique_metrics = len(set(metric_names))
        metric_coherence = 1.0 - (unique_metrics - 1) / max(len(cluster.signals), 1)
        
        # Temporal coherence
        timestamps = [s.timestamp for s in cluster.signals]
        temporal_span = max(timestamps) - min(timestamps) if timestamps else 0.0
        temporal_coherence = 1.0 / (1.0 + temporal_span / self.config.temporal_coherence_window)
        
        # Source diversity (more diverse = more reliable)
        sources = [s.source for s in cluster.signals]
        source_diversity_score = min(1.0, len(set(sources)) / 3.0)  # Cap at 3 sources
        
        stage2_confidence = (metric_coherence * 0.4 + 
                            temporal_coherence * 0.3 + 
                            source_diversity_score * 0.3)
        
        # Stage 3: Baseline consistency
        stage3_confidence = 1.0
        if baseline_snapshot:
            deviations = []
            for signal in cluster.signals:
                if signal.platform and signal.account_id:
                    scope = f"platform:{signal.platform}:account:{signal.account_id}"
                elif signal.platform:
                    scope = f"platform:{signal.platform}"
                elif signal.account_id:
                    scope = f"account:{signal.account_id}"
                else:
                    scope = "global"
                
                deviation = baseline_snapshot.calculate_deviation(
                    scope, signal.metric_name, signal.metric_value, signal.timestamp
                )
                if deviation is not None:
                    deviations.append(abs(deviation))
            
            if deviations:
                # Consistent deviations (low variance) = higher confidence
                deviation_variance = statistics.variance(deviations) if len(deviations) > 1 else 0
                consistency_score = 1.0 / (1.0 + deviation_variance)
                stage3_confidence = min(1.0, consistency_score * 0.8 + 0.2)
        
        # Stage 4: Cross-validation (check if pattern appears in multiple contexts)
        stage4_confidence = 1.0
        if len(cluster.signals) >= 4:
            # Split signals into two groups and check if both show similar patterns
            mid_point = len(cluster.signals) // 2
            group1 = cluster.signals[:mid_point]
            group2 = cluster.signals[mid_point:]
            
            # Calculate average deviation for each group
            dev1 = statistics.mean([
                (s.metric_value - s.baseline_value) / max(s.baseline_value, 0.01)
                for s in group1
            ]) if group1 else 0
            
            dev2 = statistics.mean([
                (s.metric_value - s.baseline_value) / max(s.baseline_value, 0.01)
                for s in group2
            ]) if group2 else 0
            
            # Similar deviations = cross-validation success
            if abs(dev1 - dev2) < 0.2:
                stage4_confidence = 1.0
            else:
                stage4_confidence = 0.7
        
        # Combine stages with weighted average
        final_confidence = (
            stage1_confidence * 0.25 +
            stage2_confidence * 0.30 +
            stage3_confidence * 0.25 +
            stage4_confidence * 0.20
        )
        
        # Apply cluster correlation score as final multiplier
        final_confidence *= cluster.correlation_score
        
        return min(1.0, max(0.0, final_confidence))
    
    def detect_causal_chains(
        self,
        signals: List[AnomalySignal],
        max_chain_length: int = 5
    ) -> List[List[AnomalySignal]]:
        """
        Detect causal chains: sequences of signals where each might cause the next.
        
        Returns chains of signals ordered by potential causality.
        """
        chains = []
        sorted_signals = sorted(signals, key=lambda s: s.timestamp)
        
        for i, signal in enumerate(sorted_signals):
            chain = [signal]
            current_signal = signal
            
            for j in range(i + 1, min(i + max_chain_length, len(sorted_signals))):
                next_signal = sorted_signals[j]
                
                # Check if next_signal could be caused by current_signal
                time_gap = next_signal.timestamp - current_signal.timestamp
                
                # Same metric, different account/platform suggests propagation
                if (next_signal.metric_name == current_signal.metric_name and
                    time_gap > 0 and time_gap < 3600 and  # Within 1 hour
                    (next_signal.account_id != current_signal.account_id or
                     next_signal.platform != current_signal.platform)):
                    
                    # Similar deviation pattern
                    dev1 = (current_signal.metric_value - current_signal.baseline_value) / max(current_signal.baseline_value, 0.01)
                    dev2 = (next_signal.metric_value - next_signal.baseline_value) / max(next_signal.baseline_value, 0.01)
                    
                    if abs(dev1 - dev2) < 0.3:  # Similar deviation
                        chain.append(next_signal)
                        current_signal = next_signal
                    else:
                        break
                else:
                    break
            
            if len(chain) >= 2:
                chains.append(chain)
        
        return chains
    
    def _create_cluster(
        self, 
        signals: List[AnomalySignal], 
        cluster_type: str,
        features: Optional[Dict] = None
    ) -> CorrelationCluster:
        """Create a correlation cluster from signals with enhanced scoring."""
        cluster_id = f"{cluster_type}_{hash(tuple(s.timestamp for s in signals))}"
        
        # Calculate temporal span
        timestamps = [s.timestamp for s in signals]
        temporal_span = max(timestamps) - min(timestamps) if timestamps else 0.0
        
        # Metric diversity
        metric_names = [s.metric_name for s in signals]
        metric_diversity = len(set(metric_names))
        
        # Source diversity
        sources = [s.source for s in signals]
        source_diversity = len(set(sources))
        
        # Enhanced correlation score calculation
        unique_metrics = len(set(metric_names))
        metric_score = 1.0 - (unique_metrics - 1) / max(len(signals), 1)
        
        # Temporal coherence (closer in time = higher correlation)
        temporal_score = 1.0 / (1.0 + temporal_span / self.config.temporal_coherence_window)
        
        # Deviation consistency (how similar are the deviations)
        deviations = [
            (s.metric_value - s.baseline_value) / max(s.baseline_value, 0.01)
            for s in signals if s.baseline_value > 0
        ]
        if deviations:
            deviation_consistency = 1.0 - min(1.0, statistics.stdev([abs(d) for d in deviations]) if len(deviations) > 1 else 0)
        else:
            deviation_consistency = 0.5
        
        # Account/platform diversity (more diverse = stronger pattern)
        accounts = {s.account_id for s in signals if s.account_id}
        platforms = {s.platform for s in signals if s.platform}
        diversity_score = min(1.0, (len(accounts) + len(platforms)) / 4.0)
        
        # Combined correlation score
        correlation_score = (
            metric_score * 0.35 +
            temporal_score * 0.25 +
            deviation_consistency * 0.25 +
            diversity_score * 0.15
        )
        
        return CorrelationCluster(
            signals=signals,
            cluster_id=cluster_id,
            correlation_score=max(0.5, correlation_score),
            common_features=features or {},
            temporal_span=temporal_span,
            metric_diversity=metric_diversity,
            source_diversity=source_diversity
        )


# ============================================================================
# THRESHOLD POLICY
# ============================================================================


class ThresholdPolicy:
    """
    Defines:
    - What constitutes deviation
    - How many signals form an event
    - Severity escalation rules
    - Account-tier-specific thresholds
    - Time-adaptive thresholds
    """
    
    def __init__(self):
        # Platform-specific thresholds
        self._platform_thresholds = {
            "youtube": {"z_score": 2.5, "min_signals": 2},
            "tiktok": {"z_score": 2.0, "min_signals": 2},
            "instagram": {"z_score": 2.5, "min_signals": 2},
            "twitter": {"z_score": 2.5, "min_signals": 2},
            "reddit": {"z_score": 3.0, "min_signals": 2},
            "linkedin": {"z_score": 2.5, "min_signals": 2},
            "facebook": {"z_score": 2.5, "min_signals": 2},
            "default": {"z_score": 3.0, "min_signals": 2}
        }
        
        # Account-tier-specific multipliers
        self._account_tiers = {
            "premium": AccountTier("premium", (0.8, 1.0), 0.8),  # Lower threshold (more sensitive)
            "standard": AccountTier("standard", (0.5, 0.8), 1.0),  # Base threshold
            "probe": AccountTier("probe", (0.0, 0.5), 1.2)  # Higher threshold (less sensitive)
        }
        
        # Time-adaptive thresholds
        self._time_adaptive = TimeAdaptiveThreshold(
            base_threshold=2.5,
            time_of_day_multipliers={
                # Business hours (9-17) are more sensitive
                **{h: 0.9 for h in range(9, 18)},
                # Off-hours are less sensitive
                **{h: 1.1 for h in list(range(0, 9)) + list(range(18, 24))}
            },
            day_of_week_multipliers={
                0: 1.0,  # Monday
                1: 1.0,  # Tuesday
                2: 1.0,  # Wednesday
                3: 1.0,  # Thursday
                4: 1.0,  # Friday
                5: 1.1,  # Saturday (less sensitive)
                6: 1.1   # Sunday (less sensitive)
            },
            seasonal_adjustments={
                "spring": 1.0,
                "summer": 1.0,
                "fall": 1.0,
                "winter": 1.0
            }
        )
        
        # Severity thresholds
        self._severity_thresholds = {
            AnomalySeverity.INFO: 0.5,
            AnomalySeverity.WARNING: 0.7,
            AnomalySeverity.CRITICAL: 0.85,
            AnomalySeverity.SYSTEMIC: 0.95
        }
    
    def get_platform_threshold(self, platform: str, key: str, 
                               account_tier: Optional[str] = None,
                               timestamp: Optional[float] = None) -> float:
        """Get platform-specific threshold with tier and time adjustments."""
        config = self._platform_thresholds.get(platform, self._platform_thresholds["default"])
        base_threshold = config.get(key, self._platform_thresholds["default"][key])
        
        # Apply account tier multiplier
        if account_tier and account_tier in self._account_tiers:
            tier = self._account_tiers[account_tier]
            base_threshold *= tier.threshold_multiplier
        
        # Apply time-adaptive multiplier
        if timestamp:
            hour = int((timestamp % 86400) / 3600)
            day_of_week = int((timestamp // 86400) % 7)
            
            hour_mult = self._time_adaptive.time_of_day_multipliers.get(hour, 1.0)
            day_mult = self._time_adaptive.day_of_week_multipliers.get(day_of_week, 1.0)
            
            base_threshold *= (hour_mult * day_mult)
        
        return base_threshold
    
    def get_account_tier(self, trust_score: Optional[float]) -> str:
        """Determine account tier from trust score."""
        if trust_score is None:
            return "standard"
        
        for tier_name, tier in self._account_tiers.items():
            if tier.trust_score_range[0] <= trust_score <= tier.trust_score_range[1]:
                return tier_name
        
        return "standard"
    
    def determine_severity(self, confidence: float, account_count: int, 
                          platform_count: int, account_tiers: Optional[Set[str]] = None) -> AnomalySeverity:
        """Determine severity based on confidence, scope, and account tiers."""
        # SYSTEMIC requires multiple accounts/platforms
        if confidence >= self._severity_thresholds[AnomalySeverity.SYSTEMIC]:
            if account_count >= 3 or platform_count >= 2:
                # Boost severity if premium accounts involved
                if account_tiers and "premium" in account_tiers:
                    return AnomalySeverity.SYSTEMIC
                return AnomalySeverity.SYSTEMIC
        
        if confidence >= self._severity_thresholds[AnomalySeverity.CRITICAL]:
            return AnomalySeverity.CRITICAL
        
        if confidence >= self._severity_thresholds[AnomalySeverity.WARNING]:
            return AnomalySeverity.WARNING
        
        return AnomalySeverity.INFO
    
    def should_fire(self, anomaly_type: AnomalyType, confidence: float, 
                   signal_count: int, platform: Optional[str] = None,
                   account_tier: Optional[str] = None) -> bool:
        """Determine if anomaly should fire with tier-aware thresholds."""
        if signal_count < 2:
            return False
        
        min_confidence = self._severity_thresholds[AnomalySeverity.INFO]
        
        # Adjust based on platform and tier
        if platform:
            min_signals = self.get_platform_threshold(platform, "min_signals", account_tier)
            if signal_count < min_signals:
                return False
        
        if confidence < min_confidence:
            return False
        
        return True


# ============================================================================
# ANOMALY INVARIANT VALIDATOR
# ============================================================================


class AnomalyInvariantValidator:
    """
    Enforced rules:
    - No anomaly without ≥2 independent signals
    - No SYSTEMIC severity from single account
    - No escalation without confidence ≥ threshold
    - Same anomaly type cannot fire continuously without decay
    - Signal quality validation
    - Baseline consistency checks
    - Cross-window decay suppression
    - Anomaly re-entry cooldown modeling
    - Severity hysteresis (prevent severity oscillation)
    """
    
    def __init__(self, config: Optional[AnomalyDetectorConfig] = None, cooldown_seconds: float = 300.0, min_signal_quality: float = 0.3):
        self.config = config or AnomalyDetectorConfig()
        self.cooldown_seconds = self.config.cooldown_seconds if config else cooldown_seconds
        self.min_signal_quality = min_signal_quality
        self._last_fired: Dict[str, float] = {}
        self._quality_scorer = SignalQualityScorer()
        
        # Cross-window decay tracking
        self._window_history: Dict[str, List[Tuple[float, AnomalySeverity]]] = defaultdict(list)
        self._window_duration: float = self.config.window_duration
        self._max_events_per_window: int = self.config.max_events_per_window
        
        # Re-entry cooldown (prevent same anomaly from firing too frequently)
        self._re_entry_cooldowns: Dict[str, float] = {}
        self._re_entry_cooldown_base: float = self.config.re_entry_cooldown_base
        self._re_entry_cooldown_multiplier: float = self.config.re_entry_cooldown_multiplier
        
        # Severity hysteresis (prevent rapid severity changes)
        self._last_severity: Dict[str, AnomalySeverity] = {}
        self._severity_change_cooldown: float = self.config.severity_change_cooldown
        self._severity_change_times: Dict[str, float] = {}
        
        # Formal lifecycle state machine - locks cooldown/decay as provable automaton
        # Key: (anomaly_type, account_set_string) -> state dict
        self._lifecycle_state: Dict[str, Dict[str, Any]] = {}
        self._decay_window: float = self.config.decay_window
    
    def validate(self, event: AnomalyEvent, evaluation_timestamp: float) -> Tuple[bool, Optional[str], Optional[AnomalyEvent]]:
        """
        Validate anomaly event against invariants.
        Returns (is_valid, error_message, validated_event)
        
        If valid, returns event with validated=True set.
        """
        # Rule 1: Must have ≥2 signals
        if len(event.supporting_signals) < 2:
            return False, "Anomaly requires ≥2 independent signals", None
        
        # Rule 1a: Explicit independence check (mechanical, not interpretive)
        signal_pairs = list(combinations(event.supporting_signals, 2))
        if not any(are_independent(x, y) for x, y in signal_pairs):
            return False, "No independent signal pairs found", None
        
        # Rule 2: SYSTEMIC cannot be single account
        if event.severity == AnomalySeverity.SYSTEMIC:
            if len(event.involved_accounts) < 2:
                return False, "SYSTEMIC severity requires multiple accounts", None
            if len(event.involved_platforms) < 2 and len(event.involved_accounts) < 3:
                return False, "SYSTEMIC severity requires ≥2 platforms or ≥3 accounts", None
        
        # Rule 3: Confidence threshold
        if event.confidence < 0.5:
            return False, f"Confidence {event.confidence} below threshold", None
        
        # Rule 4: Lifecycle state machine enforcement
        lifecycle_key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        lifecycle_state = self._lifecycle_state.get(lifecycle_key)
        
        if lifecycle_state:
            current_state = lifecycle_state["state"]
            entered_at = lifecycle_state["entered_at"]
            
            # State transition rules
            if current_state == AnomalyLifecycle.ACTIVE:
                # Cannot re-enter ACTIVE unless transitioned through QUIET
                return False, f"Anomaly already ACTIVE - cannot re-enter without lifecycle transition", None
            elif current_state == AnomalyLifecycle.COOLDOWN:
                cooldown_elapsed = evaluation_timestamp - entered_at
                if cooldown_elapsed < self.cooldown_seconds:
                    return False, f"Cooldown active: {self.cooldown_seconds - cooldown_elapsed:.0f}s remaining", None
                # Transition: COOLDOWN -> DECAYING
                self._lifecycle_state[lifecycle_key] = {
                    "state": AnomalyLifecycle.DECAYING,
                    "entered_at": evaluation_timestamp
                }
            elif current_state == AnomalyLifecycle.DECAYING:
                decay_elapsed = evaluation_timestamp - entered_at
                if decay_elapsed < self._decay_window:
                    return False, f"Decay window active: {self._decay_window - decay_elapsed:.0f}s remaining", None
                # Transition: DECAYING -> QUIET
                self._lifecycle_state[lifecycle_key] = {
                    "state": AnomalyLifecycle.QUIET,
                    "entered_at": evaluation_timestamp
                }
            elif current_state == AnomalyLifecycle.QUIET:
                # Can transition from QUIET to ACTIVE
                pass
        
        # Rule 5: Signal diversity (sources must differ) - redundant with independence but kept for clarity
        sources = {s.source for s in event.supporting_signals}
        if len(sources) < 2:
            return False, "Signals must come from ≥2 independent sources", None
        
        # Rule 6: Signal quality validation (use evaluation_timestamp, not live time)
        low_quality_count = 0
        for signal in event.supporting_signals:
            quality = self._quality_scorer.score_signal(signal, evaluation_timestamp)
            if quality.overall_quality < self.min_signal_quality:
                low_quality_count += 1
        
        if low_quality_count > len(event.supporting_signals) / 2:
            return False, f"Too many low-quality signals: {low_quality_count}/{len(event.supporting_signals)}", None
        
        # Rule 7: Baseline consistency (signals should have consistent baseline relationships)
        if len(event.supporting_signals) >= 3:
            deviations = []
            for signal in event.supporting_signals:
                if signal.baseline_value > 0:
                    deviation = (signal.metric_value - signal.baseline_value) / signal.baseline_value
                    deviations.append(deviation)
            
            if len(deviations) >= 3:
                # Check if deviations are consistent (similar direction and magnitude)
                positive_deviations = [d for d in deviations if d > 0]
                negative_deviations = [d for d in deviations if d < 0]
                
                # If mixed positive and negative, might be inconsistent
                if len(positive_deviations) > 0 and len(negative_deviations) > 0:
                    # Allow if one direction is dominant (>70%)
                    dominant_ratio = max(len(positive_deviations), len(negative_deviations)) / len(deviations)
                    if dominant_ratio < 0.7:
                        return False, "Inconsistent deviation patterns in signals"
        
        # Rule 8: Temporal coherence (signals should be reasonably close in time)
        timestamps = [s.timestamp for s in event.supporting_signals]
        time_span = max(timestamps) - min(timestamps)
        max_time_span = 3600.0  # 1 hour
        
        if time_span > max_time_span:
            return False, f"Signals span too much time: {time_span:.0f}s > {max_time_span:.0f}s"
        
        # Rule 9: Cross-window decay suppression
        window_key = self._get_window_key(event)
        window_events = self._window_history[window_key]
        current_window_start = (event.detected_at // self._window_duration) * self._window_duration
        
        # Remove old windows
        window_events = [
            (ts, sev) for ts, sev in window_events
            if ts >= current_window_start
        ]
        
        if len(window_events) >= self._max_events_per_window:
            return False, f"Too many events in current window: {len(window_events)} >= {self._max_events_per_window}"
        
        # Rule 10: Re-entry cooldown (exponential backoff)
        re_entry_key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        if re_entry_key in self._re_entry_cooldowns:
            cooldown_end = self._re_entry_cooldowns[re_entry_key]
            if event.detected_at < cooldown_end:
                remaining = cooldown_end - event.detected_at
                return False, f"Re-entry cooldown active: {remaining:.0f}s remaining"
        
        # Rule 11: Severity hysteresis (prevent rapid severity changes)
        severity_key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        if severity_key in self._last_severity:
            last_severity = self._last_severity[severity_key]
            last_change_time = self._severity_change_times.get(severity_key, 0)
            
            # Check if severity is changing
            if event.severity != last_severity:
                # Severity can only increase, or decrease after cooldown
                severity_order = {
                    AnomalySeverity.INFO: 1,
                    AnomalySeverity.WARNING: 2,
                    AnomalySeverity.CRITICAL: 3,
                    AnomalySeverity.SYSTEMIC: 4
                }
                
                current_order = severity_order[event.severity]
                last_order = severity_order[last_severity]
                
                # If decreasing, must wait for cooldown
                if current_order < last_order:
                    if event.detected_at - last_change_time < self._severity_change_cooldown:
                        return False, f"Severity decrease requires cooldown: {self._severity_change_cooldown:.0f}s", None
        
        # All validations passed - return validated event
        validated_event = replace(event, validated=True)
        return True, None, validated_event
    
    def _get_window_key(self, event: AnomalyEvent) -> str:
        """Generate window key for cross-window tracking."""
        window_start = (event.detected_at // self._window_duration) * self._window_duration
        return f"{event.anomaly_type.value}:{int(window_start)}"
    
    def mark_fired(self, event: AnomalyEvent, evaluation_timestamp: float):
        """
        Mark anomaly as fired for cooldown tracking with all tracking mechanisms.
        Transitions lifecycle state machine: ACTIVE -> COOLDOWN
        """
        key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        self._last_fired[key] = evaluation_timestamp
        
        # Lifecycle state machine transition: QUIET/None -> ACTIVE -> COOLDOWN
        lifecycle_key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        self._lifecycle_state[lifecycle_key] = {
            "state": AnomalyLifecycle.COOLDOWN,
            "entered_at": evaluation_timestamp
        }
        
        # Update cross-window history
        window_key = self._get_window_key(event)
        self._window_history[window_key].append((event.detected_at, event.severity))
        
        # Update re-entry cooldown (exponential backoff)
        re_entry_key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        if re_entry_key in self._re_entry_cooldowns:
            # Exponential backoff: double the cooldown each time
            current_cooldown = self._re_entry_cooldowns[re_entry_key] - evaluation_timestamp
            if current_cooldown > 0:
                new_cooldown = current_cooldown * self._re_entry_cooldown_multiplier
            else:
                new_cooldown = self._re_entry_cooldown_base * self._re_entry_cooldown_multiplier
        else:
            new_cooldown = self._re_entry_cooldown_base
        
        # Cap maximum cooldown at 24 hours
        max_cooldown = 86400.0
        new_cooldown = min(new_cooldown, max_cooldown)
        self._re_entry_cooldowns[re_entry_key] = evaluation_timestamp + new_cooldown
        
        # Update severity tracking for hysteresis
        severity_key = f"{event.anomaly_type.value}:{','.join(sorted(event.involved_accounts))}"
        if severity_key in self._last_severity:
            if event.severity != self._last_severity[severity_key]:
                self._severity_change_times[severity_key] = evaluation_timestamp
        else:
            self._severity_change_times[severity_key] = evaluation_timestamp
        
        self._last_severity[severity_key] = event.severity
        
        # Clean old entries (older than 24 hours)
        cutoff = evaluation_timestamp - 86400
        self._last_fired = {k: v for k, v in self._last_fired.items() if v > cutoff}
        self._re_entry_cooldowns = {k: v for k, v in self._re_entry_cooldowns.items() if v > cutoff}
        self._severity_change_times = {k: v for k, v in self._severity_change_times.items() if v > cutoff}
        
        # Clean window history (keep only last 24 hours)
        for window_key in list(self._window_history.keys()):
            self._window_history[window_key] = [
                (ts, sev) for ts, sev in self._window_history[window_key]
                if ts > cutoff
            ]
            if not self._window_history[window_key]:
                del self._window_history[window_key]


# ============================================================================
# ANOMALY ESCALATION EMITTER
# ============================================================================


class AnomalyEscalationEmitter:
    """
    Emits:
    - Structured events to alerting.py
    - Suppression hints to kill_switches.py
    - Advisory signals to account_router.py
    
    NEVER executes actions directly.
    """
    
    def __init__(self, config: Optional[AnomalyDetectorConfig] = None, rate_limit_per_minute: int = 10):
        self.config = config or AnomalyDetectorConfig()
        self._event_log: List[AnomalyEvent] = []
        self._rate_limit_per_minute = self.config.rate_limit_per_minute if config else rate_limit_per_minute
        self._emission_times: deque = deque(maxlen=self._rate_limit_per_minute)
        self._priority_queue: List[AnomalyEvent] = []
    
    def _trim_event_log(self):
        """Trim event log to prevent unbounded memory growth."""
        max_size = self.config.max_event_log_size
        if len(self._event_log) > max_size:
            # Keep most recent events
            self._event_log = self._event_log[-max_size:]
            logger.debug(f"Trimmed event log to {max_size} entries")
    
    def emit(self, event: AnomalyEvent) -> Dict[str, Any]:
        """
        Emit anomaly event to downstream systems with rate limiting.
        
        Returns structured event objects for downstream consumption.
        No stdout side effects - production-ready.
        
        ENFORCED: Only events validated by invariant layer can be emitted.
        This is mechanically enforced - no bypass possible.
        """
        # Enforce invariant layer validation - cannot be bypassed
        if not event.validated:
            raise ValueError(
                f"Event must be validated by invariant layer before emission. "
                f"AnomalyType: {event.anomaly_type.value}, "
                f"Validated: {event.validated}"
            )
        
        self._event_log.append(event)
        self._trim_event_log()
        
        # Rate limiting
        now = time.time()
        recent_emissions = [t for t in self._emission_times if now - t < 60.0]
        
        if len(recent_emissions) >= self._rate_limit_per_minute:
            # Queue for later emission
            self._priority_queue.append(event)
            return {
                "status": "rate_limited",
                "queued": True
            }
        
        self._emission_times.append(now)
        
        # Emit to all downstream systems - return structured events
        result = {
            "status": "emitted",
            "event_id": self._generate_event_id(event),
            "alerting": self._emit_to_alerting(event),
            "kill_switches": self._emit_to_kill_switches(event),
            "account_router": self._emit_to_account_router(event)
        }
        return result
    
    def _emit_to_alerting(self, event: AnomalyEvent) -> Dict[str, Any]:
        """Send structured event to alerting.py for operator notification."""
        structured_event = {
            "type": "anomaly_detected",
            "anomaly_type": event.anomaly_type.value,
            "severity": event.severity.value,
            "detected_at": event.detected_at,
            "confidence": event.confidence,
            "involved_accounts": list(event.involved_accounts),
            "involved_platforms": list(event.involved_platforms),
            "signal_count": len(event.supporting_signals),
            "recommended_action": event.recommended_action,
            "event_id": self._generate_event_id(event)
        }
        
        return structured_event
    
    def _emit_to_kill_switches(self, event: AnomalyEvent) -> Optional[Dict[str, Any]]:
        """Send suppression hints to kill_switches.py."""
        if event.severity in (AnomalySeverity.CRITICAL, AnomalySeverity.SYSTEMIC):
            hint = {
                "type": "suppression_hint",
                "severity": event.severity.value,
                "accounts": list(event.involved_accounts),
                "platforms": list(event.involved_platforms),
                "anomaly_type": event.anomaly_type.value,
                "confidence": event.confidence,
                "recommended_throttle": True
            }
            return hint
        return None
    
    def _emit_to_account_router(self, event: AnomalyEvent) -> Optional[Dict[str, Any]]:
        """Send advisory signals to account_router.py."""
        if event.anomaly_type == AnomalyType.CROSS_ACCOUNT_DEGRADATION:
            advisory = {
                "type": "account_health_advisory",
                "accounts": list(event.involved_accounts),
                "anomaly_type": event.anomaly_type.value,
                "severity": event.severity.value,
                "confidence": event.confidence,
                "recommendation": "Consider routing adjustments"
            }
            return advisory
        return None
    
    def _generate_event_id(self, event: AnomalyEvent) -> str:
        """Generate deterministic event ID for tracking."""
        event_data = {
            "type": event.anomaly_type.value,
            "accounts": sorted(event.involved_accounts),
            "platforms": sorted(event.involved_platforms),
            "timestamp": int(event.detected_at)
        }
        event_str = json.dumps(event_data, sort_keys=True)
        return hashlib.sha256(event_str.encode()).hexdigest()[:16]
    
    def get_recent_events(self, limit: int = 10) -> List[AnomalyEvent]:
        """Retrieve recent events for inspection."""
        return self._event_log[-limit:]
    
    def get_events_by_severity(self, severity: AnomalySeverity) -> List[AnomalyEvent]:
        """Get all events of a specific severity."""
        return [e for e in self._event_log if e.severity == severity]
    
    def get_events_by_type(self, anomaly_type: AnomalyType) -> List[AnomalyEvent]:
        """Get all events of a specific type."""
        return [e for e in self._event_log if e.anomaly_type == anomaly_type]


# ============================================================================
# DETECTION COORDINATOR
# ============================================================================


class DetectionCoordinator:
    """
    Coordinates baseline updates and statistics tracking.
    
    Separates concerns from AnomalyDetector:
    - Handles baseline learning/updates
    - Tracks detection statistics
    - Maintains baseline model state
    
    AnomalyDetector is now a pure evaluator + correlator.
    """
    
    def __init__(self, baseline_model: BaselineModel):
        self._baseline_model = baseline_model
        self._detection_statistics: Dict[str, Any] = {
            "total_signals_ingested": 0,
            "total_events_detected": 0,
            "events_by_type": defaultdict(int),
            "events_by_severity": defaultdict(int),
            "false_positive_estimates": defaultdict(int)
        }
    
    def update_baselines_from_signal(self, signal: AnomalySignal):
        """Update baselines with signal data - extracted from detector."""
        # Determine relevant scopes
        scopes = []
        if signal.platform and signal.account_id:
            scopes.append(f"platform:{signal.platform}:account:{signal.account_id}")
            scopes.append(f"platform:{signal.platform}")
            scopes.append(f"account:{signal.account_id}")
        elif signal.platform:
            scopes.append(f"platform:{signal.platform}")
        elif signal.account_id:
            scopes.append(f"account:{signal.account_id}")
        
        scopes.append("global")
        
        # Update baselines for all relevant scopes
        for scope in scopes:
            self._baseline_model.update(
                scope, 
                signal.metric_name, 
                signal.metric_value, 
                signal.timestamp
            )
    
    def record_signal_ingested(self):
        """Record that a signal was ingested."""
        self._detection_statistics["total_signals_ingested"] += 1
    
    def record_event_detected(self, event: AnomalyEvent):
        """Record that an event was detected."""
        self._detection_statistics["total_events_detected"] += 1
        self._detection_statistics["events_by_type"][event.anomaly_type.value] += 1
        self._detection_statistics["events_by_severity"][event.severity.value] += 1
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get detection statistics for monitoring."""
        return {
            **self._detection_statistics,
            "baseline_count": len(self._baseline_model._baselines)
        }
    
    def reset_statistics(self):
        """Reset detection statistics."""
        self._detection_statistics = {
            "total_signals_ingested": 0,
            "total_events_detected": 0,
            "events_by_type": defaultdict(int),
            "events_by_severity": defaultdict(int),
            "false_positive_estimates": defaultdict(int)
        }
    
    def get_baseline_snapshot(self) -> BaselineModelSnapshot:
        """Get frozen snapshot of baselines for detector consumption."""
        return self._baseline_model.snapshot()


# ============================================================================
# CORE ANOMALY DETECTOR
# ============================================================================


# ============================================================================
# EXTERNAL SYSTEM INTEGRATION STUBS (Read-Only Access)
# ============================================================================

class TrustSignalRecorderInterface(ABC):
    """Interface for trust_signal_recorder.py (read-only)."""
    
    @abstractmethod
    def get_account_trust_score(self, account_id: str) -> Optional[float]:
        """Get current trust score for account."""
        pass
    
    @abstractmethod
    def get_trust_history(self, account_id: str, hours: int = 24) -> List[Tuple[float, float]]:
        """Get trust score history: [(timestamp, score), ...]."""
        pass


class CadenceMemoryInterface(ABC):
    """Interface for cadence_memory.py (read-only)."""
    
    @abstractmethod
    def get_account_cadence_compliance(self, account_id: str) -> Optional[float]:
        """Get cadence compliance score (0.0 to 1.0)."""
        pass
    
    @abstractmethod
    def get_recent_posting_times(self, account_id: str, hours: int = 24) -> List[float]:
        """Get recent posting timestamps."""
        pass


class PostingStateStoreInterface(ABC):
    """Interface for posting_state_store.py (read-only)."""
    
    @abstractmethod
    def get_account_summary(self, account_id: str) -> Optional[Dict[str, Any]]:
        """Get account posting summary."""
        pass
    
    @abstractmethod
    def get_platform_summary(self, platform: str) -> Optional[Dict[str, Any]]:
        """Get platform posting summary."""
        pass


class SignalQualityScorer:
    """Scores signal quality for filtering and weighting."""
    
    def __init__(self, max_signal_age: float = 3600.0):
        self.max_signal_age = max_signal_age
        self._source_reliability: Dict[str, float] = defaultdict(lambda: 0.8)
    
    def score_signal(self, signal: AnomalySignal, current_time: float) -> SignalQuality:
        """Calculate quality metrics for a signal."""
        # Freshness score (decay with age)
        age = current_time - signal.timestamp
        freshness_score = max(0.0, 1.0 - (age / self.max_signal_age))
        
        # Source reliability (tracked over time)
        source_reliability = self._source_reliability[signal.source]
        
        # Metric consistency (how close to baseline)
        if signal.baseline_value > 0:
            deviation_ratio = abs(signal.metric_value - signal.baseline_value) / signal.baseline_value
            # More consistent = higher score (inverse of deviation)
            metric_consistency = max(0.0, 1.0 - min(1.0, deviation_ratio))
        else:
            metric_consistency = 0.5
        
        # Overall quality (weighted combination)
        overall_quality = (freshness_score * 0.3 + 
                          source_reliability * 0.4 + 
                          metric_consistency * 0.3)
        
        return SignalQuality(
            freshness_score=freshness_score,
            source_reliability=source_reliability,
            metric_consistency=metric_consistency,
            overall_quality=overall_quality
        )
    
    def update_source_reliability(self, source: str, reliability: float):
        """Update source reliability score."""
        self._source_reliability[source] = max(0.0, min(1.0, reliability))


class AnomalyDetector:
    """
    Cross-system integrity sentinel.
    
    Answers: Are multiple 'normal' signals forming an abnormal system-wide pattern?
    
    If yes → escalate, throttle, or halt
    If no → remain silent
    """
    
    def __init__(
        self,
        config: Optional[AnomalyDetectorConfig] = None,
        trust_recorder: Optional[TrustSignalRecorderInterface] = None,
        cadence_memory: Optional[CadenceMemoryInterface] = None,
        state_store: Optional[PostingStateStoreInterface] = None,
        coordinator: Optional[DetectionCoordinator] = None
    ):
        self.config = config or AnomalyDetectorConfig()
        self._signal_buffer: List[AnomalySignal] = []
        self._baseline_model = BaselineModel(self.config)
        self._correlation_engine = CorrelationEngine(self.config)
        self._threshold_policy = ThresholdPolicy()
        self._invariant_validator = AnomalyInvariantValidator(self.config)
        self._escalation_emitter = AnomalyEscalationEmitter(self.config)
        self._quality_scorer = SignalQualityScorer()
        
        # Coordinator handles baseline updates and statistics
        self._coordinator = coordinator or DetectionCoordinator(self._baseline_model)
        
        # External system interfaces (read-only)
        self._trust_recorder = trust_recorder
        self._cadence_memory = cadence_memory
        self._state_store = state_store
        
        # State tracking
        self._last_evaluation = 0.0
        self._evaluation_interval = self.config.evaluation_interval
        self._signal_threshold = self.config.signal_threshold
        self._account_tier_cache: Dict[str, str] = {}
    
    def _trim_signal_buffer(self):
        """Trim signal buffer to prevent unbounded memory growth."""
        max_size = self.config.max_signal_buffer_size
        if len(self._signal_buffer) > max_size:
            # Keep most recent signals
            self._signal_buffer = self._signal_buffer[-max_size:]
            logger.debug(f"Trimmed signal buffer to {max_size} entries")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get detection statistics for monitoring."""
        stats = self._coordinator.get_statistics()
        stats.update({
            "buffer_size": len(self._signal_buffer),
            "last_evaluation": self._last_evaluation
        })
        return stats
    
    def reset_statistics(self):
        """Reset detection statistics."""
        self._coordinator.reset_statistics()
    
    # ========================================================================
    # SIGNAL INGESTION
    # ========================================================================
    
    def ingest_signal(self, signal: AnomalySignal):
        """
        Called by monitoring subsystems.
        
        Rules:
        - Signals are buffered
        - No evaluation on ingest
        - Idempotent per (source, timestamp, metric_name)
        - No baseline mutation (delegated to coordinator)
        """
        # Deduplicate
        signal_key = (signal.source, signal.timestamp, signal.metric_name)
        existing_keys = {(s.source, s.timestamp, s.metric_name) for s in self._signal_buffer}
        
        if signal_key in existing_keys:
            return  # Already ingested
        
        # Validate input
        if signal is None:
            logger.warning("Received None signal, ignoring")
            return
        if signal.timestamp <= 0:
            logger.warning(f"Invalid signal timestamp: {signal.timestamp}, ignoring")
            return
        if not signal.metric_name:
            logger.warning("Signal missing metric_name, ignoring")
            return
        
        self._signal_buffer.append(signal)
        self._trim_signal_buffer()
        
        # Coordinator handles baseline updates and statistics
        try:
            self._coordinator.update_baselines_from_signal(signal)
            self._coordinator.record_signal_ingested()
        except Exception as e:
            logger.error(f"Error updating baselines from signal: {e}", exc_info=True)
    
    # ========================================================================
    # EVALUATION ORCHESTRATION
    # ========================================================================
    
    def evaluate(self, force: bool = False) -> List[AnomalyEvent]:
        """
        Runs on fixed cadence or signal threshold.
        
        Responsibilities:
        - Snapshot buffered signals
        - Filter by quality
        - Compare against frozen baseline snapshots
        - Emit zero or more AnomalyEvents
        
        Uses frozen baseline snapshots - no mutation during evaluation.
        
        DETERMINISTIC: All ordering is explicit, evaluation_timestamp is frozen.
        """
        # Input validation
        if not self._signal_buffer:
            return []
        
        # Capture evaluation timestamp once - used for all logic, not live time
        evaluation_timestamp = time.time()
        
        if not force:
            # Check time-based trigger (cooldown tracking only, not logic)
            if evaluation_timestamp - self._last_evaluation < self._evaluation_interval:
                return []  # Not time yet
            
            # Check signal-count trigger
            if len(self._signal_buffer) < self._signal_threshold:
                return []  # Not enough signals
        
        self._last_evaluation = evaluation_timestamp
        
        # Snapshot baseline model - frozen for this evaluation
        baseline_snapshot = self._coordinator.get_baseline_snapshot()
        
        # Snapshot and filter signals by quality - DETERMINISTIC SORTING
        raw_signals = sorted(
            self._signal_buffer[:],
            key=lambda s: (s.timestamp, s.source, s.metric_name, s.account_id or "", s.platform or "")
        )
        self._signal_buffer = []
        
        # Filter and score signals - use evaluation_timestamp, not live time
        signals = []
        for signal in raw_signals:
            quality = self._quality_scorer.score_signal(signal, evaluation_timestamp)
            # Only include signals with reasonable quality
            if quality.overall_quality >= 0.3:
                signals.append(signal)
        
        if len(signals) < 2:
            return []  # Need at least 2 quality signals
        
        # Enrich signals with account tier information
        self._enrich_signals_with_tiers(signals)
        
        events = []
        
        # Run detection methods with frozen baseline snapshot and evaluation timestamp
        events.extend(self.detect_cross_account(signals, baseline_snapshot, evaluation_timestamp))
        events.extend(self.detect_cross_platform(signals, baseline_snapshot, evaluation_timestamp))
        events.extend(self.detect_temporal(signals, baseline_snapshot, evaluation_timestamp))
        events.extend(self.detect_behavioral(signals, baseline_snapshot, evaluation_timestamp))
        events.extend(self.detect_trust_decay_cluster(signals, baseline_snapshot, evaluation_timestamp))
        events.extend(self.detect_metric_platform_conflict(signals, baseline_snapshot, evaluation_timestamp))
        
        # Deduplicate events - DETERMINISTIC SORTING
        events = self._deduplicate_events(events)
        
        # Validate and emit - SEALED PATH: Detector -> Validator -> Emitter
        validated_events = []
        for event in events:
            is_valid, error, validated_event = self._invariant_validator.validate(event, evaluation_timestamp)
            if is_valid and validated_event:
                # Only mark as fired after validation succeeds
                self._invariant_validator.mark_fired(validated_event, evaluation_timestamp)
                # Emitter asserts validated=True - cannot be bypassed
                self._escalation_emitter.emit(validated_event)  # Returns structured events, no stdout
                validated_events.append(validated_event)
                
                # Coordinator handles statistics
                self._coordinator.record_event_detected(validated_event)
        
        return validated_events
    
    def _enrich_signals_with_tiers(self, signals: List[AnomalySignal]):
        """Enrich signals with account tier information from trust recorder."""
        if not self._trust_recorder:
            return
        
        for signal in signals:
            if signal.account_id and signal.account_id not in self._account_tier_cache:
                try:
                    if self._trust_recorder:
                        trust_score = self._trust_recorder.get_account_trust_score(signal.account_id)
                        tier = self._threshold_policy.get_account_tier(trust_score)
                        self._account_tier_cache[signal.account_id] = tier
                    else:
                        # Default to standard tier if trust recorder not available
                        self._account_tier_cache[signal.account_id] = "standard"
                except Exception as e:
                    logger.warning(f"Error getting trust score for account {signal.account_id}: {e}")
                    # Default to standard tier on error
                    self._account_tier_cache[signal.account_id] = "standard"
    
    def _deduplicate_events(self, events: List[AnomalyEvent]) -> List[AnomalyEvent]:
        """Remove duplicate events (same type, same accounts, within 5 minutes)."""
        if not events:
            return []
        
        deduplicated = []
        seen_keys: Set[str] = set()
        
        for event in events:
            # Create deduplication key
            key_parts = [
                event.anomaly_type.value,
                ','.join(sorted(event.involved_accounts)),
                ','.join(sorted(event.involved_platforms))
            ]
            key = '|'.join(key_parts)
            
            # Check if similar event seen recently
            is_duplicate = False
            for seen_key in seen_keys:
                if seen_key == key:
                    is_duplicate = True
                    break
            
            if not is_duplicate:
                deduplicated.append(event)
                seen_keys.add(key)
        
        return deduplicated
    
    # ========================================================================
    # DETECTION METHODS
    # ========================================================================
    
    def detect_cross_account(self, signals: List[AnomalySignal], 
                            baseline_snapshot: BaselineModelSnapshot,
                            evaluation_timestamp: float) -> List[AnomalyEvent]:
        """
        Detects correlated degradation across independent accounts.
        
        Uses:
        - trust_signal_recorder (read-only)
        - cadence_memory (read-only)
        - posting_state_store summaries (read-only)
        - baseline_snapshot (frozen, read-only)
        - evaluation_timestamp (frozen, not live time)
        """
        # Input validation
        if not signals:
            return []
        if baseline_snapshot is None:
            logger.warning("detect_cross_account called with None baseline_snapshot")
            return []
        if evaluation_timestamp <= 0:
            logger.warning(f"detect_cross_account called with invalid timestamp: {evaluation_timestamp}")
            return []
        
        # DETERMINISTIC: Sort signals before clustering
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.timestamp, s.source, s.metric_name, s.account_id or "", s.platform or "")
        )
        clusters = self._correlation_engine.find_account_clusters(sorted_signals)
        
        # DETERMINISTIC: Sort clusters before processing
        sorted_clusters = sorted(
            clusters,
            key=lambda c: (min(s.timestamp for s in c.signals), c.correlation_score)
        )
        
        events = []
        for cluster in sorted_clusters:
            if len(cluster.signals) < 2:
                continue
            
            # Check for degradation pattern
            degraded = [s for s in cluster.signals if s.metric_value < s.baseline_value * 0.7]
            
            if len(degraded) >= 2:
                confidence = self._correlation_engine.calculate_cluster_confidence(
                    cluster, baseline_snapshot
                )
                
                accounts = {s.account_id for s in cluster.signals if s.account_id}
                platforms = {s.platform for s in cluster.signals if s.platform}
                
                # Get account tiers for severity determination
                account_tiers = {self._account_tier_cache.get(acc, "standard") for acc in accounts}
                
                # Check cadence compliance if available
                cadence_violations = 0
                if self._cadence_memory:
                    for acc in accounts:
                        try:
                            compliance = self._cadence_memory.get_account_cadence_compliance(acc)
                            if compliance and compliance < 0.7:
                                cadence_violations += 1
                        except Exception as e:
                            logger.warning(f"Error getting cadence compliance for account {acc}: {e}")
                            # Continue processing without cadence data
                
                # Boost confidence if cadence is compliant (suggests external suppression)
                if cadence_violations == 0 and len(accounts) > 0:
                    confidence = min(1.0, confidence * 1.1)
                
                severity = self._threshold_policy.determine_severity(
                    confidence, len(accounts), len(platforms), account_tiers
                )
                
                # Build recommendation with context
                recommendation = "Investigate platform-wide suppression or content policy change"
                if self._state_store:
                    platform_summaries = {}
                    for platform in platforms:
                        try:
                            summary = self._state_store.get_platform_summary(platform)
                            if summary:
                                platform_summaries[platform] = summary
                        except Exception as e:
                            logger.warning(f"Error getting platform summary for {platform}: {e}")
                            # Continue without this platform's summary
                    
                    if platform_summaries:
                        recommendation += f". Platform summaries available for {len(platform_summaries)} platforms"
                
                event = AnomalyEvent(
                    anomaly_type=AnomalyType.CROSS_ACCOUNT_DEGRADATION,
                    severity=severity,
                    detected_at=evaluation_timestamp,
                    involved_accounts=accounts,
                    involved_platforms=platforms,
                    supporting_signals=tuple(cluster.signals),
                    confidence=confidence,
                    recommended_action=recommendation
                )
                
                events.append(event)
        
        return events
    
    def detect_cross_platform(self, signals: List[AnomalySignal], 
                             baseline_snapshot: BaselineModelSnapshot,
                             evaluation_timestamp: float) -> List[AnomalyEvent]:
        """
        Detects divergence beyond historical norms.
        
        Uses:
        - platform telemetry
        - post_health_tracker
        - reconciliation outputs
        - evaluation_timestamp (frozen, not live time)
        """
        # DETERMINISTIC: Sort signals before processing
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.timestamp, s.source, s.metric_name, s.account_id or "", s.platform or "")
        )
        platform_clusters = self._correlation_engine.find_platform_clusters(sorted_signals)
        events = []
        
        # Look for platform pairs with opposite trends
        platform_signals = defaultdict(list)
        for signal in sorted_signals:
            if signal.platform:
                platform_signals[signal.platform].append(signal)
        
        # DETERMINISTIC: Sort platforms for consistent iteration
        platforms = sorted(platform_signals.keys())
        
        # Compare all platform pairs
        for i, p1 in enumerate(platforms):
            for p2 in platforms[i+1:]:
                p1_signals = platform_signals[p1]
                p2_signals = platform_signals[p2]
                
                if len(p1_signals) == 0 or len(p2_signals) == 0:
                    continue
                
                # Calculate normalized averages
                p1_normalized = [s.metric_value / max(s.baseline_value, 0.01) for s in p1_signals]
                p2_normalized = [s.metric_value / max(s.baseline_value, 0.01) for s in p2_signals]
                
                p1_avg = statistics.mean(p1_normalized)
                p2_avg = statistics.mean(p2_normalized)
                
                # Calculate variance for confidence adjustment
                p1_variance = statistics.variance(p1_normalized) if len(p1_normalized) > 1 else 0
                p2_variance = statistics.variance(p2_normalized) if len(p2_normalized) > 1 else 0
                
                # Detect significant divergence
                divergence = abs(p1_avg - p2_avg)
                
                if divergence > 0.5:  # 50% divergence threshold
                    all_signals = p1_signals + p2_signals
                    
                    if len(all_signals) < 2:
                        continue
                    
                    accounts = {s.account_id for s in all_signals if s.account_id}
                    
                    # Confidence based on divergence magnitude and variance
                    base_confidence = min(0.95, divergence)
                    variance_penalty = (p1_variance + p2_variance) * 0.1
                    confidence = max(0.5, base_confidence - variance_penalty)
                    
                    # Check for historical baseline comparison if state store available
                    if self._state_store:
                        try:
                            p1_summary = self._state_store.get_platform_summary(p1)
                            p2_summary = self._state_store.get_platform_summary(p2)
                            
                            if p1_summary and p2_summary:
                                # Use historical data to adjust confidence
                                historical_divergence = abs(
                                    p1_summary.get("avg_performance", 1.0) - 
                                    p2_summary.get("avg_performance", 1.0)
                                )
                                if historical_divergence < 0.2:  # Usually similar
                                    confidence = min(0.95, confidence * 1.2)  # Boost confidence
                        except Exception as e:
                            logger.warning(f"Error getting platform summaries for {p1}/{p2}: {e}")
                            # Continue without historical comparison
                    
                    severity = self._threshold_policy.determine_severity(
                        confidence, len(accounts), 2
                    )
                    
                    # Build detailed recommendation
                    recommendation = f"Investigate {p1} vs {p2} algorithm changes. "
                    recommendation += f"Divergence: {divergence:.1%} (p1: {p1_avg:.2f}, p2: {p2_avg:.2f})"
                    
                    event = AnomalyEvent(
                        anomaly_type=AnomalyType.CROSS_PLATFORM_DIVERGENCE,
                        severity=severity,
                        detected_at=evaluation_timestamp,
                        involved_accounts=accounts,
                        involved_platforms={p1, p2},
                        supporting_signals=tuple(all_signals[:20]),  # Limit to 20
                        confidence=confidence,
                        recommended_action=recommendation
                    )
                    
                    events.append(event)
        
        return events
    
    def detect_temporal(self, signals: List[AnomalySignal], 
                       baseline_snapshot: BaselineModelSnapshot,
                       evaluation_timestamp: float) -> List[AnomalyEvent]:
        """
        Detects sudden shifts in latency, failure clustering, delayed suppression waves.
        Uses evaluation_timestamp (frozen, not live time).
        """
        # DETERMINISTIC: Sort signals before clustering
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.timestamp, s.source, s.metric_name, s.account_id or "", s.platform or "")
        )
        temporal_clusters = self._correlation_engine.find_temporal_clusters(sorted_signals, time_window=300)
        
        # DETERMINISTIC: Sort clusters before processing
        sorted_clusters = sorted(
            temporal_clusters,
            key=lambda c: (min(s.timestamp for s in c.signals), c.correlation_score)
        )
        
        events = []
        for cluster in sorted_clusters:
            if len(cluster.signals) < 3:  # Temporal requires more signals
                continue
            
            # Check for latency shift
            latency_signals = [s for s in cluster.signals if 'latency' in s.metric_name.lower()]
            
            if len(latency_signals) >= 2:
                deviations = [
                    abs(s.metric_value - s.baseline_value) / max(s.baseline_value, 1)
                    for s in latency_signals
                ]
                avg_deviation = statistics.mean(deviations)
                max_deviation = max(deviations)
                
                if avg_deviation > 0.5:  # 50% latency shift
                    accounts = {s.account_id for s in cluster.signals if s.account_id}
                    platforms = {s.platform for s in cluster.signals if s.platform}
                    
                    # Confidence based on deviation magnitude and consistency
                    base_confidence = min(0.9, avg_deviation)
                    consistency_boost = 1.0 - (statistics.stdev(deviations) if len(deviations) > 1 else 0)
                    confidence = min(0.95, base_confidence * (0.7 + 0.3 * consistency_boost))
                    
                    severity = self._threshold_policy.determine_severity(
                        confidence, len(accounts), len(platforms)
                    )
                    
                    # Detect if this is a sudden spike or gradual increase
                    timestamps = [s.timestamp for s in latency_signals]
                    sorted_by_time = sorted(zip(timestamps, deviations), key=lambda x: x[0])
                    
                    if len(sorted_by_time) >= 3:
                        early_avg = statistics.mean([d for _, d in sorted_by_time[:len(sorted_by_time)//2]])
                        late_avg = statistics.mean([d for _, d in sorted_by_time[len(sorted_by_time)//2:]])
                        trend = "sudden spike" if late_avg > early_avg * 1.5 else "gradual increase"
                    else:
                        trend = "detected shift"
                    
                    recommendation = f"Check platform API health and network conditions. "
                    recommendation += f"Latency {trend}: avg {avg_deviation:.1%}, max {max_deviation:.1%}"
                    
                    event = AnomalyEvent(
                        anomaly_type=AnomalyType.TEMPORAL_LATENCY_SHIFT,
                        severity=severity,
                        detected_at=evaluation_timestamp,
                        involved_accounts=accounts,
                        involved_platforms=platforms,
                        supporting_signals=tuple(cluster.signals),
                        confidence=confidence,
                        recommended_action=recommendation
                    )
                    
                    events.append(event)
            
            # Check for failure clustering (non-latency metrics)
            non_latency_signals = [s for s in cluster.signals if 'latency' not in s.metric_name.lower()]
            if len(non_latency_signals) >= 3:
                # Look for failure patterns
                failure_signals = [
                    s for s in non_latency_signals 
                    if s.metric_value < s.baseline_value * 0.5
                ]
                
                if len(failure_signals) >= 3:
                    accounts = {s.account_id for s in cluster.signals if s.account_id}
                    platforms = {s.platform for s in cluster.signals if s.platform}
                    
                    failure_ratio = len(failure_signals) / len(non_latency_signals)
                    confidence = min(0.9, failure_ratio * 1.2)
                    
                    severity = self._threshold_policy.determine_severity(
                        confidence, len(accounts), len(platforms)
                    )
                    
                    event = AnomalyEvent(
                        anomaly_type=AnomalyType.TEMPORAL_LATENCY_SHIFT,  # Reuse type for failure clustering
                        severity=severity,
                        detected_at=evaluation_timestamp,
                        involved_accounts=accounts, 
                        involved_platforms=platforms,
                        supporting_signals=tuple(failure_signals),
                        confidence=confidence,
                        recommended_action=f"Temporal failure cluster detected: {len(failure_signals)}/{len(non_latency_signals)} failures in {cluster.temporal_span:.0f}s window"
                    )
                    
                    events.append(event)
        
        return events
    
    def detect_behavioral(self, signals: List[AnomalySignal], 
                         baseline_snapshot: BaselineModelSnapshot,
                         evaluation_timestamp: float) -> List[AnomalyEvent]:
        """
        Detects policy-compliant behavior leading to punishment.
        This is the shadow-ban detector of last resort.
        
        Deep analysis includes:
        - Visibility collapse despite compliance signals
        - Engagement rate anomalies
        - Reach-to-impression ratio degradation
        - Cross-platform shadow-ban patterns
        - Gradual vs sudden suppression detection
        - Account age vs performance correlation
        
        Uses evaluation_timestamp (frozen, not live time).
        """
        # DETERMINISTIC: Sort signals before processing
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.timestamp, s.source, s.metric_name, s.account_id or "", s.platform or "")
        )
        
        events = []
        
        # Look for visibility collapse despite compliance
        visibility_signals = [s for s in sorted_signals if 'visibility' in s.metric_name.lower() 
                             or 'reach' in s.metric_name.lower()
                             or 'impressions' in s.metric_name.lower()]
        
        if len(visibility_signals) < 2:
            return events
        
        # Group by account for per-account analysis
        account_visibility = defaultdict(list)
        for signal in visibility_signals:
            if signal.account_id:
                account_visibility[signal.account_id].append(signal)
        
        # DETERMINISTIC: Sort accounts for consistent iteration
        sorted_accounts = sorted(account_visibility.keys())
        
        # Analyze each account for shadow-ban patterns
        for account_id in sorted_accounts:
            account_sigs = account_visibility[account_id]
            if len(account_sigs) < 2:
                continue
            
            # Sort by timestamp to detect trends
            sorted_sigs = sorted(account_sigs, key=lambda s: s.timestamp)
            
            # Pattern 1: Sudden collapse (shadow-ban activation)
            recent_sigs = sorted_sigs[-3:] if len(sorted_sigs) >= 3 else sorted_sigs
            older_sigs = sorted_sigs[:-3] if len(sorted_sigs) >= 3 else []
            
            if older_sigs and recent_sigs:
                older_avg = statistics.mean([
                    s.metric_value / max(s.baseline_value, 0.01) for s in older_sigs
                ])
                recent_avg = statistics.mean([
                    s.metric_value / max(s.baseline_value, 0.01) for s in recent_sigs
                ])
                
                collapse_ratio = recent_avg / max(older_avg, 0.01)
                
                # Sudden collapse (drop > 70% in recent signals)
                if collapse_ratio < 0.3 and older_avg > 0.7:
                    # Check if this is isolated or cross-account
                    all_collapsed = [s for s in visibility_signals 
                                    if s.metric_value < s.baseline_value * 0.3]
                    
                    if len(all_collapsed) >= 2:
                        accounts = {s.account_id for s in all_collapsed if s.account_id}
                        platforms = {s.platform for s in all_collapsed if s.platform}
                        
                        # High confidence for sudden collapse
                        confidence = min(0.95, 0.7 + (1.0 - collapse_ratio) * 0.25)
            
            severity = self._threshold_policy.determine_severity(
                confidence, len(accounts), len(platforms)
            )
            
            event = AnomalyEvent(
                anomaly_type=AnomalyType.VISIBILITY_COLLAPSE,
                severity=severity,
                detected_at=time.time(),
                involved_accounts=accounts,
                involved_platforms=platforms,
                supporting_signals=tuple(all_collapsed),
                confidence=confidence,
                recommended_action=f"Sudden visibility collapse detected: {collapse_ratio:.1%} of baseline. "
                                 f"Possible shadow-ban activation. Manual review required."
            )
            
            events.append(event)
            
            # Pattern 2: Gradual suppression (algorithmic throttling)
            if len(sorted_sigs) >= 5:
                # Calculate trend over time
                time_values = [
                    (s.timestamp, s.metric_value / max(s.baseline_value, 0.01))
                    for s in sorted_sigs
                ]
                
                # Linear regression to detect downward trend
                timestamps = [t for t, _ in time_values]
                values = [v for _, v in time_values]
                
                if len(timestamps) > 1:
                    # Calculate slope
                    n = len(timestamps)
                    sum_t = sum(timestamps)
                    sum_v = sum(values)
                    sum_tv = sum(t * v for t, v in zip(timestamps, values))
                    sum_t2 = sum(t * t for t in timestamps)
                    
                    denominator = n * sum_t2 - sum_t * sum_t
                    if denominator != 0:
                        slope = (n * sum_tv - sum_t * sum_v) / denominator
                        
                        # Negative slope indicates gradual decline
                        if slope < -1e-6:  # Significant negative slope
                            # Check if decline is substantial (>30% over period)
                            total_decline = (values[-1] - values[0]) / max(values[0], 0.01)
                            
                            if total_decline < -0.3:  # 30% decline
                                # Check cadence compliance if available
                                is_compliant = True
                                if self._cadence_memory:
                                    compliance = self._cadence_memory.get_account_cadence_compliance(account_id)
                                    if compliance and compliance < 0.7:
                                        is_compliant = False
                                
                                # High confidence if compliant but suppressed
                                if is_compliant:
                                    all_gradual = [s for s in visibility_signals 
                                                  if s.account_id == account_id]
                                    
                                    if len(all_gradual) >= 3:
                                        accounts = {account_id}
                                        platforms = {s.platform for s in all_gradual if s.platform}
                                        
                                        confidence = min(0.9, 0.6 + abs(total_decline) * 0.3)
                                        
                                        severity = self._threshold_policy.determine_severity(
                                            confidence, len(accounts), len(platforms)
                                        )
                                        
                                        event = AnomalyEvent(
                                            anomaly_type=AnomalyType.VISIBILITY_COLLAPSE,
                                            severity=severity,
                                            detected_at=evaluation_timestamp,
                                            involved_accounts=accounts,
                                            involved_platforms=platforms,
                                            supporting_signals=tuple(all_gradual),
                                            confidence=confidence,
                                            recommended_action=f"Gradual suppression detected: {total_decline:.1%} decline "
                                                             f"over time despite compliance. Possible algorithmic throttling."
                                        )
                                        
                                        events.append(event)
        
        # Pattern 3: Cross-platform shadow-ban (same account, multiple platforms)
        if len(account_visibility) >= 2:
            # Find accounts with visibility issues across multiple platforms
            suppressed_accounts = {}
            for account_id, account_sigs in account_visibility.items():
                suppressed_count = sum(1 for s in account_sigs 
                                     if s.metric_value < s.baseline_value * 0.4)
                if suppressed_count >= 2:
                    platforms = {s.platform for s in account_sigs if s.platform}
                    if len(platforms) >= 2:  # Multiple platforms
                        suppressed_accounts[account_id] = {
                            'signals': account_sigs,
                            'platforms': platforms,
                            'suppression_ratio': suppressed_count / len(account_sigs)
                        }
            
            # If multiple accounts show cross-platform suppression, it's systemic
            if len(suppressed_accounts) >= 2:
                all_suppressed_signals = []
                all_accounts = set()
                all_platforms = set()
                
                for account_id, data in suppressed_accounts.items():
                    all_suppressed_signals.extend(data['signals'])
                    all_accounts.add(account_id)
                    all_platforms.update(data['platforms'])
                
                if len(all_suppressed_signals) >= 4:
                    avg_suppression = statistics.mean([
                        data['suppression_ratio'] for data in suppressed_accounts.values()
                    ])
                    
                    confidence = min(0.95, 0.7 + avg_suppression * 0.25)
                    
                    severity = self._threshold_policy.determine_severity(
                        confidence, len(all_accounts), len(all_platforms)
                    )
                    
                    event = AnomalyEvent(
                        anomaly_type=AnomalyType.VISIBILITY_COLLAPSE,
                        severity=severity,
                        detected_at=evaluation_timestamp,
                        involved_accounts=all_accounts,
                        involved_platforms=all_platforms,
                        supporting_signals=tuple(all_suppressed_signals[:30]),  # Limit signals
                        confidence=confidence,
                        recommended_action=f"Cross-platform shadow-ban pattern: {len(all_accounts)} accounts "
                                         f"showing suppression across {len(all_platforms)} platforms. "
                                         f"Possible coordinated suppression or policy change."
                    )
                    
                    events.append(event)
        
        return events
    
    def detect_trust_decay_cluster(self, signals: List[AnomalySignal], 
                                   baseline_snapshot: BaselineModelSnapshot,
                                   evaluation_timestamp: float) -> List[AnomalyEvent]:
        """
        Detects clusters of trust decay across multiple accounts.
        
        Uses:
        - trust_signal_recorder (read-only)
        - cadence_memory (read-only)
        """
        events = []
        
        # Group signals by account
        account_signals = defaultdict(list)
        for signal in signals:
            if signal.account_id and 'trust' in signal.metric_name.lower():
                account_signals[signal.account_id].append(signal)
        
        # Find accounts with trust decay
        decay_patterns: Dict[str, TrustDecayPattern] = {}
        
        for account_id, account_sigs in account_signals.items():
            if len(account_sigs) < 2:
                continue
            
            # Sort by timestamp
            sorted_sigs = sorted(account_sigs, key=lambda s: s.timestamp)
            
            # Calculate decay rate
            first_trust = sorted_sigs[0].metric_value
            last_trust = sorted_sigs[-1].metric_value
            time_span = sorted_sigs[-1].timestamp - sorted_sigs[0].timestamp
            
            if time_span > 0 and first_trust > 0:
                decay_rate = (first_trust - last_trust) / (first_trust * time_span / 3600)  # Per hour
                
                if decay_rate > 0.05:  # 5% decay per hour threshold
                    decay_patterns[account_id] = TrustDecayPattern(
                        account_ids={account_id},
                        decay_rate=decay_rate,
                        initial_trust=first_trust,
                        current_trust=last_trust,
                        detected_at=evaluation_timestamp,
                        confidence=min(0.9, decay_rate * 10)
                    )
        
        # Cluster accounts with similar decay patterns
        if len(decay_patterns) >= 2:
            account_ids = list(decay_patterns.keys())
            clustered_accounts = []
            
            for i, acc1 in enumerate(account_ids):
                pattern1 = decay_patterns[acc1]
                cluster = {acc1}
                
                for acc2 in account_ids[i+1:]:
                    pattern2 = decay_patterns[acc2]
                    
                    # Similar decay rates (within 20%)
                    if abs(pattern1.decay_rate - pattern2.decay_rate) / max(pattern1.decay_rate, 0.01) < 0.2:
                        cluster.add(acc2)
                
                if len(cluster) >= 2:
                    clustered_accounts.append(cluster)
            
            # Create events for clusters
            for cluster_accounts in clustered_accounts:
                if len(cluster_accounts) >= 2:
                    cluster_patterns = [decay_patterns[acc] for acc in cluster_accounts]
                    avg_decay_rate = statistics.mean([p.decay_rate for p in cluster_patterns])
                    avg_confidence = statistics.mean([p.confidence for p in cluster_patterns])
                    
                    # Collect all signals from clustered accounts
                    cluster_signals = []
                    for acc in cluster_accounts:
                        cluster_signals.extend(account_signals[acc])
                    
                    if len(cluster_signals) >= 2:
                        severity = self._threshold_policy.determine_severity(
                            avg_confidence, len(cluster_accounts), 1
                        )
                        
                        event = AnomalyEvent(
                            anomaly_type=AnomalyType.TRUST_DECAY_CLUSTER,
                            severity=severity,
                            detected_at=evaluation_timestamp,
                            involved_accounts=cluster_accounts,
                            involved_platforms=set(),
                            supporting_signals=tuple(cluster_signals[:20]),  # Limit signals
                            confidence=avg_confidence,
                            recommended_action=f"Trust decay cluster detected: {len(cluster_accounts)} accounts, "
                                             f"avg decay rate {avg_decay_rate:.2%}/hr"
                        )
                        
                        events.append(event)
        
        return events
    
    def detect_metric_platform_conflict(self, signals: List[AnomalySignal], 
                                       baseline_snapshot: BaselineModelSnapshot,
                                       evaluation_timestamp: float) -> List[AnomalyEvent]:
        """
        Detects conflicts between platform-reported metrics and actual downstream metrics.
        
        Example: Platform claims success but metrics imply suppression.
        
        Uses evaluation_timestamp (frozen, not live time).
        """
        # DETERMINISTIC: Sort signals before processing
        sorted_signals = sorted(
            signals,
            key=lambda s: (s.timestamp, s.source, s.metric_name, s.account_id or "", s.platform or "")
        )
        
        events = []
        
        # Group signals by platform and metric type
        platform_metrics = defaultdict(lambda: defaultdict(list))
        
        for signal in sorted_signals:
            if signal.platform:
                # Categorize metrics
                if 'success' in signal.metric_name.lower() or 'posted' in signal.metric_name.lower():
                    platform_metrics[signal.platform]['reported'].append(signal)
                elif 'visibility' in signal.metric_name.lower() or 'reach' in signal.metric_name.lower():
                    platform_metrics[signal.platform]['actual'].append(signal)
                elif 'impressions' in signal.metric_name.lower():
                    platform_metrics[signal.platform]['actual'].append(signal)
        
        # DETERMINISTIC: Sort platforms for consistent iteration
        sorted_platforms = sorted(platform_metrics.keys())
        
        # Check for conflicts per platform
        for platform in sorted_platforms:
            metric_groups = platform_metrics[platform]
            reported = metric_groups.get('reported', [])
            actual = metric_groups.get('actual', [])
            
            if len(reported) >= 1 and len(actual) >= 1:
                # Calculate average reported success vs actual visibility
                avg_reported = statistics.mean([s.metric_value / max(s.baseline_value, 0.01) 
                                               for s in reported])
                avg_actual = statistics.mean([s.metric_value / max(s.baseline_value, 0.01) 
                                             for s in actual])
                
                # Conflict: High reported success but low actual visibility
                if avg_reported > 0.8 and avg_actual < 0.3:
                    conflict_signals = reported + actual
                    
                    if len(conflict_signals) >= 2:
                        accounts = {s.account_id for s in conflict_signals if s.account_id}
                        confidence = min(0.95, (avg_reported - avg_actual) * 0.5)
                        
                        severity = self._threshold_policy.determine_severity(
                            confidence, len(accounts), 1
                        )
                        
                        event = AnomalyEvent(
                            anomaly_type=AnomalyType.METRIC_PLATFORM_CONFLICT,
                            severity=severity,
                            detected_at=evaluation_timestamp,
                            involved_accounts=accounts,
                            involved_platforms={platform},
                            supporting_signals=tuple(conflict_signals),
                            confidence=confidence,
                            recommended_action=f"Platform {platform} reports success but metrics show suppression. "
                                             f"Possible shadow-ban or metric manipulation."
                        )
                        
                        events.append(event)
        
        return events


# ============================================================================
# PUBLIC API
# ============================================================================


def create_detector(
    trust_recorder: Optional[TrustSignalRecorderInterface] = None,
    cadence_memory: Optional[CadenceMemoryInterface] = None,
    state_store: Optional[PostingStateStoreInterface] = None
) -> AnomalyDetector:
    """
    Factory function to create configured detector instance.
    
    Args:
        trust_recorder: Optional trust signal recorder interface (read-only)
        cadence_memory: Optional cadence memory interface (read-only)
        state_store: Optional posting state store interface (read-only)
    
    Returns:
        Configured AnomalyDetector instance
    """
    return AnomalyDetector(
        trust_recorder=trust_recorder,
        cadence_memory=cadence_memory,
        state_store=state_store
    )


# ============================================================================
# EXAMPLE USAGE
# ============================================================================


if __name__ == "__main__":
    # Initialize detector
    detector = create_detector()
    
    # Simulate signals from monitoring subsystems
    signals = [
        AnomalySignal(
            source="post_health_tracker.py",
            timestamp=time.time(),
            platform="youtube",
            account_id="account_001",
            intent_id="intent_123",
            metric_name="visibility_score",
            metric_value=0.2,
            baseline_value=0.8,
            confidence=0.9
        ),
        AnomalySignal(
            source="suppression_monitor.py",
            timestamp=time.time() + 10,
            platform="youtube",
            account_id="account_002",
            intent_id="intent_124",
            metric_name="visibility_score",
            metric_value=0.15,
            baseline_value=0.85,
            confidence=0.85
        ),
        AnomalySignal(
            source="post_health_tracker.py",
            timestamp=time.time() + 20,
            platform="tiktok",
            account_id="account_001",
            intent_id="intent_125",
            metric_name="reach",
            metric_value=1000,
            baseline_value=5000,
            confidence=0.9
        ),
    ]
    
    # Ingest signals
    for signal in signals:
        detector.ingest_signal(signal)
    
    # Force evaluation
    events = detector.evaluate(force=True)
    
    # Events are returned as structured objects - no stdout side effects
    # Example: Process events or log via structured logging system
    # for event in events:
    #     structured_logger.info("anomaly_detected", event=event)