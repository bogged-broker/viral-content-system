"""
evaluation/suppression_analyzer.py
Algorithmic Suppression Detection & Shadow-Ban Attribution Engine

Determines whether content underperformed due to quality or algorithmic suppression.
Answers: "Did this video fail on its own merits, or was distribution artificially constrained?"

Core responsibilities:
- Detect algorithmic suppression patterns
- Separate suppression from organic decay
- Identify suppression type with confidence weighting
- Protect RL agents from poisoned feedback
- Remain deterministic and replayable

NO content scoring, scraping, policy evasion, or posting decisions.
Diagnosis only.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple, List, Dict
import math
from collections import defaultdict


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

class SuppressionType(Enum):
    """Algorithmic suppression classification taxonomy."""
    NONE = "none"
    VELOCITY_CAP = "velocity_cap"
    DISTRIBUTION_THROTTLE = "distribution_throttle"
    CREATOR_COOLDOWN = "creator_cooldown"
    AUDIENCE_SATURATION = "audience_saturation"
    SHADOW_LIMIT = "shadow_limit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SuppressionEvent:
    """Individual suppression occurrence with evidence trail."""
    video_id: str
    platform: str
    suppression_type: SuppressionType
    onset_timestamp: float
    severity: float  # [0,1] - impact magnitude
    confidence: float  # [0,1] - detection certainty
    evidence: list[str]  # human-auditable justifications


@dataclass(frozen=True)
class SuppressionConfidence:
    """Probabilistic suppression attribution with competing explanations."""
    probability: float  # [0,1]
    competing_explanations: dict[str, float]  # alternative causes with weights


@dataclass(frozen=True)
class SuppressionAnalysisResult:
    """Complete suppression diagnostic output."""
    video_id: str
    platform: str
    suppressed: bool
    suppression_type: SuppressionType
    severity: float
    confidence: float
    onset_estimate_seconds: float
    counterfactual_uplift_estimate: float  # what performance would have been
    notes: list[str]


@dataclass(frozen=True)
class NormalizedMetrics:
    """Input from cross_platform_normalizer.py"""
    video_id: str
    platform: str
    timestamps: list[float]
    impressions: list[float]
    engagements: list[float]
    retention_curve: list[float]
    velocity_curve: list[float]


@dataclass(frozen=True)
class ExpectationEnvelope:
    """Performance bounds based on early signals and historical data."""
    timestamps: list[float]
    expected_impressions_median: list[float]
    expected_impressions_p25: list[float]
    expected_impressions_p75: list[float]
    expected_velocity: list[float]
    basis: str  # "cohort_median" | "creator_prior" | "early_signal"


@dataclass(frozen=True)
class VelocityAnomaly:
    """Detected velocity pattern inconsistency."""
    onset_index: int
    onset_timestamp: float
    pre_velocity: float
    post_velocity: float
    severity: float
    pattern_type: str  # "plateau" | "cliff" | "ceiling"


@dataclass(frozen=True)
class PlatformSuppressionProfile:
    """Platform-specific suppression behavior signatures."""
    platform: str
    velocity_cap_threshold: float
    distribution_throttle_patterns: list[str]
    creator_cooldown_window_hours: float
    saturation_decay_rate: float
    known_ceiling_behaviors: dict[str, float]


# ============================================================================
# BASELINE EXPECTATION BUILDER
# ============================================================================

class BaselineExpectationBuilder:
    """
    Constructs expected performance envelope from early signals and historical cohorts.
    Not prediction - only expectation bounds for deviation analysis.
    """
    
    def __init__(self, historical_cohorts: dict, creator_priors: dict):
        self.historical_cohorts = historical_cohorts
        self.creator_priors = creator_priors
    
    def build_expectation(
        self,
        metrics: NormalizedMetrics,
        early_window_seconds: float = 3600.0
    ) -> ExpectationEnvelope:
        """
        Build expected performance envelope from early signals.
        
        Uses:
        - Historical cohort medians for similar content
        - Same-creator performance priors
        - Early velocity anchoring
        """
        early_mask = [t <= early_window_seconds for t in metrics.timestamps]
        early_impressions = [imp for imp, m in zip(metrics.impressions, early_mask) if m]
        
        if not early_impressions:
            return self._fallback_expectation(metrics)
        
        early_velocity = self._compute_velocity(early_impressions, early_window_seconds)
        
        # Match to historical cohort
        cohort_key = self._find_matching_cohort(
            platform=metrics.platform,
            early_velocity=early_velocity,
            engagement_rate=self._compute_engagement_rate(metrics)
        )
        
        cohort_data = self.historical_cohorts.get(cohort_key, {})
        creator_data = self.creator_priors.get(metrics.video_id[:10], {})  # Creator ID prefix
        
        # Build envelope
        expected_impressions_median = []
        expected_impressions_p25 = []
        expected_impressions_p75 = []
        expected_velocity = []
        
        for i, t in enumerate(metrics.timestamps):
            cohort_median = cohort_data.get('impressions_median', [0] * len(metrics.timestamps))[i]
            cohort_p25 = cohort_data.get('impressions_p25', [0] * len(metrics.timestamps))[i]
            cohort_p75 = cohort_data.get('impressions_p75', [0] * len(metrics.timestamps))[i]
            
            # Blend cohort with creator prior
            creator_scale = creator_data.get('typical_scale', 1.0)
            
            expected_impressions_median.append(cohort_median * creator_scale)
            expected_impressions_p25.append(cohort_p25 * creator_scale)
            expected_impressions_p75.append(cohort_p75 * creator_scale)
            expected_velocity.append(early_velocity * math.exp(-t / 86400.0))  # Natural decay
        
        return ExpectationEnvelope(
            timestamps=metrics.timestamps,
            expected_impressions_median=expected_impressions_median,
            expected_impressions_p25=expected_impressions_p25,
            expected_impressions_p75=expected_impressions_p75,
            expected_velocity=expected_velocity,
            basis="cohort_median_with_creator_prior"
        )
    
    def _compute_velocity(self, impressions: list[float], window: float) -> float:
        """Compute impressions per second in early window."""
        if len(impressions) < 2:
            return 0.0
        return (impressions[-1] - impressions[0]) / window
    
    def _compute_engagement_rate(self, metrics: NormalizedMetrics) -> float:
        """Compute overall engagement rate."""
        total_impressions = sum(metrics.impressions)
        total_engagements = sum(metrics.engagements)
        return total_engagements / total_impressions if total_impressions > 0 else 0.0
    
    def _find_matching_cohort(
        self,
        platform: str,
        early_velocity: float,
        engagement_rate: float
    ) -> str:
        """Match to closest historical cohort."""
        # Simple bucketing - production would use KNN or clustering
        velocity_bucket = int(math.log10(early_velocity + 1))
        engagement_bucket = int(engagement_rate * 100)
        return f"{platform}_v{velocity_bucket}_e{engagement_bucket}"
    
    def _fallback_expectation(self, metrics: NormalizedMetrics) -> ExpectationEnvelope:
        """Fallback when no early data exists."""
        zeros = [0.0] * len(metrics.timestamps)
        return ExpectationEnvelope(
            timestamps=metrics.timestamps,
            expected_impressions_median=zeros,
            expected_impressions_p25=zeros,
            expected_impressions_p75=zeros,
            expected_velocity=zeros,
            basis="fallback_insufficient_data"
        )


# ============================================================================
# PERFORMANCE DEVIATION ANALYZER
# ============================================================================

class PerformanceDeviationAnalyzer:
    """
    Compares expected vs observed performance.
    Flags statistically impossible drops.
    """
    
    def __init__(self, significance_threshold: float = 2.5):
        self.significance_threshold = significance_threshold  # sigma threshold
    
    def analyze_deviation(
        self,
        metrics: NormalizedMetrics,
        expectation: ExpectationEnvelope
    ) -> dict:
        """
        Compute deviation metrics between observed and expected.
        
        Returns:
        - deviation_severity: [0,1] normalized deviation magnitude
        - deviation_onset_index: where deviation begins
        - deviation_type: "slope" | "intercept" | "ceiling"
        - statistical_significance: sigma units
        """
        deviations = []
        for i in range(len(metrics.impressions)):
            observed = metrics.impressions[i]
            expected_median = expectation.expected_impressions_median[i]
            expected_p25 = expectation.expected_impressions_p25[i]
            expected_p75 = expectation.expected_impressions_p75[i]
            
            # Z-score approximation using quartile spread
            spread = (expected_p75 - expected_p25) / 1.35  # IQR to sigma
            if spread == 0:
                spread = expected_median * 0.1  # 10% fallback
            
            z_score = (expected_median - observed) / spread if spread > 0 else 0.0
            deviations.append(z_score)
        
        # Detect onset
        onset_index = self._detect_onset(deviations)
        max_deviation = max(deviations) if deviations else 0.0
        
        # Classify deviation type
        deviation_type = self._classify_deviation_type(
            metrics, expectation, onset_index
        )
        
        return {
            'deviation_severity': min(max_deviation / 5.0, 1.0),  # normalize to [0,1]
            'deviation_onset_index': onset_index,
            'deviation_type': deviation_type,
            'statistical_significance': max_deviation,
            'significant': max_deviation >= self.significance_threshold
        }
    
    def _detect_onset(self, deviations: list[float]) -> int:
        """Find index where significant deviation begins."""
        for i, dev in enumerate(deviations):
            if dev >= self.significance_threshold:
                return i
        return -1
    
    def _classify_deviation_type(
        self,
        metrics: NormalizedMetrics,
        expectation: ExpectationEnvelope,
        onset_index: int
    ) -> str:
        """Classify whether deviation is slope, intercept, or ceiling."""
        if onset_index < 0 or onset_index >= len(metrics.impressions):
            return "none"
        
        # Check for ceiling (flat line after growth)
        post_onset = metrics.impressions[onset_index:]
        if len(post_onset) >= 3:
            variance = self._compute_variance(post_onset[-3:])
            if variance < 0.01:  # Very flat
                return "ceiling"
        
        # Check for slope change
        pre_velocity = self._compute_velocity_window(
            metrics.impressions[:onset_index], metrics.timestamps[:onset_index]
        )
        post_velocity = self._compute_velocity_window(
            metrics.impressions[onset_index:], metrics.timestamps[onset_index:]
        )
        
        if pre_velocity > 0 and post_velocity < pre_velocity * 0.3:
            return "slope"
        
        return "intercept"
    
    def _compute_variance(self, values: list[float]) -> float:
        """Compute variance of values."""
        if not values:
            return 0.0
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values) / len(values)
    
    def _compute_velocity_window(
        self,
        impressions: list[float],
        timestamps: list[float]
    ) -> float:
        """Compute velocity over window."""
        if len(impressions) < 2 or len(timestamps) < 2:
            return 0.0
        dt = timestamps[-1] - timestamps[0]
        if dt == 0:
            return 0.0
        return (impressions[-1] - impressions[0]) / dt


# ============================================================================
# VELOCITY ANOMALY DETECTOR
# ============================================================================

class VelocityAnomalyDetector:
    """
    Detects unnatural velocity patterns:
    - Abrupt plateau after acceleration
    - Feed cutoff signatures
    - Engagement without exposure anomalies
    """
    
    def __init__(self, plateau_threshold: float = 0.7):
        self.plateau_threshold = plateau_threshold
    
    def detect_anomalies(self, metrics: NormalizedMetrics) -> list[VelocityAnomaly]:
        """Detect all velocity anomalies in metric timeline."""
        anomalies = []
        
        # Compute velocity curve
        velocities = self._compute_velocities(metrics)
        
        # Detect plateaus
        anomalies.extend(self._detect_plateaus(metrics, velocities))
        
        # Detect cliffs
        anomalies.extend(self._detect_cliffs(metrics, velocities))
        
        # Detect ceilings
        anomalies.extend(self._detect_ceilings(metrics))
        
        return anomalies
    
    def _compute_velocities(self, metrics: NormalizedMetrics) -> list[float]:
        """Compute instantaneous velocities."""
        velocities = []
        for i in range(1, len(metrics.impressions)):
            dt = metrics.timestamps[i] - metrics.timestamps[i-1]
            dv = metrics.impressions[i] - metrics.impressions[i-1]
            velocities.append(dv / dt if dt > 0 else 0.0)
        return [0.0] + velocities  # Pad to match length
    
    def _detect_plateaus(
        self,
        metrics: NormalizedMetrics,
        velocities: list[float]
    ) -> list[VelocityAnomaly]:
        """Detect abrupt plateau after acceleration."""
        anomalies = []
        window = 5
        
        for i in range(window, len(velocities) - window):
            pre_window = velocities[i-window:i]
            post_window = velocities[i:i+window]
            
            pre_avg = sum(pre_window) / len(pre_window)
            post_avg = sum(post_window) / len(post_window)
            
            if pre_avg > 0 and post_avg < pre_avg * self.plateau_threshold:
                anomalies.append(VelocityAnomaly(
                    onset_index=i,
                    onset_timestamp=metrics.timestamps[i],
                    pre_velocity=pre_avg,
                    post_velocity=post_avg,
                    severity=(pre_avg - post_avg) / pre_avg,
                    pattern_type="plateau"
                ))
        
        return anomalies
    
    def _detect_cliffs(
        self,
        metrics: NormalizedMetrics,
        velocities: list[float]
    ) -> list[VelocityAnomaly]:
        """Detect sharp velocity drops."""
        anomalies = []
        
        for i in range(1, len(velocities)):
            if velocities[i-1] > 0 and velocities[i] < velocities[i-1] * 0.2:
                anomalies.append(VelocityAnomaly(
                    onset_index=i,
                    onset_timestamp=metrics.timestamps[i],
                    pre_velocity=velocities[i-1],
                    post_velocity=velocities[i],
                    severity=(velocities[i-1] - velocities[i]) / velocities[i-1],
                    pattern_type="cliff"
                ))
        
        return anomalies
    
    def _detect_ceilings(self, metrics: NormalizedMetrics) -> list[VelocityAnomaly]:
        """Detect ceiling-shaped impression curves."""
        anomalies = []
        window = 10
        
        for i in range(window, len(metrics.impressions)):
            recent = metrics.impressions[i-window:i]
            variance = sum((v - sum(recent)/len(recent))**2 for v in recent) / len(recent)
            
            if variance < 0.001 * (sum(recent)/len(recent)):  # Very flat relative to magnitude
                anomalies.append(VelocityAnomaly(
                    onset_index=i-window,
                    onset_timestamp=metrics.timestamps[i-window],
                    pre_velocity=0.0,
                    post_velocity=0.0,
                    severity=0.8,
                    pattern_type="ceiling"
                ))
                break  # Only report first ceiling
        
        return anomalies


# ============================================================================
# EXPOSURE CONSTRAINT DETECTOR
# ============================================================================

class ExposureConstraintDetector:
    """
    Detects impression caps and throttling:
    - Capped impressions
    - Ceiling-shaped curves
    - Time-bucket throttles
    """
    
    def detect_constraints(self, metrics: NormalizedMetrics) -> dict:
        """Detect exposure constraints in impression data."""
        impressions = metrics.impressions
        
        # Check for hard ceiling
        ceiling_detected, ceiling_value = self._detect_hard_ceiling(impressions)
        
        # Check for time-bucket throttling
        bucket_throttle = self._detect_bucket_throttle(metrics)
        
        # Check for engagement-exposure mismatch
        exposure_mismatch = self._detect_exposure_mismatch(metrics)
        
        return {
            'ceiling_detected': ceiling_detected,
            'ceiling_value': ceiling_value,
            'bucket_throttle': bucket_throttle,
            'exposure_mismatch': exposure_mismatch,
            'constrained': ceiling_detected or bucket_throttle or exposure_mismatch
        }
    
    def _detect_hard_ceiling(self, impressions: list[float]) -> tuple[bool, float]:
        """Detect if impressions hit a hard cap."""
        if len(impressions) < 10:
            return False, 0.0
        
        # Check last 30% of timeline
        tail_start = int(len(impressions) * 0.7)
        tail = impressions[tail_start:]
        
        max_val = max(tail)
        min_val = min(tail)
        variance = (max_val - min_val) / max_val if max_val > 0 else 0.0
        
        # If variance < 1% and sustained, it's a ceiling
        if variance < 0.01 and len(tail) >= 5:
            return True, max_val
        
        return False, 0.0
    
    def _detect_bucket_throttle(self, metrics: NormalizedMetrics) -> bool:
        """Detect time-bucket based throttling."""
        # Look for repeating patterns in velocity
        velocities = []
        for i in range(1, len(metrics.impressions)):
            dt = metrics.timestamps[i] - metrics.timestamps[i-1]
            dv = metrics.impressions[i] - metrics.impressions[i-1]
            velocities.append(dv / dt if dt > 0 else 0.0)
        
        if len(velocities) < 20:
            return False
        
        # Simple periodicity check
        # Production would use FFT or autocorrelation
        periods = [5, 10, 15, 20]  # Common bucket sizes
        for period in periods:
            if self._check_periodicity(velocities, period):
                return True
        
        return False
    
    def _check_periodicity(self, values: list[float], period: int) -> bool:
        """Check if values have periodic pattern."""
        if len(values) < period * 3:
            return False
        
        correlations = []
        for offset in range(period):
            a_vals = [values[i] for i in range(offset, len(values), period)]
            if len(a_vals) < 3:
                continue
            avg = sum(a_vals) / len(a_vals)
            variance = sum((v - avg)**2 for v in a_vals) / len(a_vals)
            correlations.append(variance)
        
        # Low variance across offsets = periodicity
        return min(correlations) < 0.1 * max(correlations) if correlations else False
    
    def _detect_exposure_mismatch(self, metrics: NormalizedMetrics) -> bool:
        """Detect engagement without proportional exposure."""
        total_impressions = sum(metrics.impressions)
        total_engagements = sum(metrics.engagements)
        
        if total_impressions == 0:
            return False
        
        engagement_rate = total_engagements / total_impressions
        
        # Check if engagement is growing but impressions are not
        imp_growth = metrics.impressions[-1] / metrics.impressions[0] if metrics.impressions[0] > 0 else 1.0
        eng_growth = metrics.engagements[-1] / metrics.engagements[0] if metrics.engagements[0] > 0 else 1.0
        
        # Engagement growing faster than impressions = exposure constraint
        return eng_growth > imp_growth * 1.5 and engagement_rate > 0.05


# ============================================================================
# NATURAL DECAY DISCRIMINATOR
# ============================================================================

class NaturalDecayDiscriminator:
    """
    CRITICAL: Prevents false accusations.
    Models natural decay patterns to distinguish from suppression:
    - Boredom decay
    - Replay fatigue
    - Hook collapse
    - Retention cliffs
    """
    
    def __init__(self):
        self.decay_models = {
            'exponential': self._exponential_decay,
            'power_law': self._power_law_decay,
            'logistic': self._logistic_decay
        }
    
    def is_natural_decay(
        self,
        metrics: NormalizedMetrics,
        expectation: ExpectationEnvelope
    ) -> tuple[bool, float, str]:
        """
        Determine if performance pattern matches natural decay.
        
        Returns:
        - is_natural: bool
        - confidence: float [0,1]
        - best_model: str
        """
        # Fit each decay model
        fit_scores = {}
        for model_name, model_func in self.decay_models.items():
            score = self._fit_model(metrics, model_func)
            fit_scores[model_name] = score
        
        best_model = max(fit_scores, key=fit_scores.get)
        best_score = fit_scores[best_model]
        
        # Check retention curve for natural patterns
        retention_natural = self._check_retention_natural(metrics.retention_curve)
        
        # Natural if good model fit AND natural retention
        is_natural = best_score > 0.7 and retention_natural
        confidence = (best_score + (1.0 if retention_natural else 0.0)) / 2.0
        
        return is_natural, confidence, best_model
    
    def _fit_model(self, metrics: NormalizedMetrics, model_func) -> float:
        """Fit decay model to impressions curve, return R²."""
        if len(metrics.impressions) < 5:
            return 0.0
        
        # Normalize time to [0,1]
        t_max = max(metrics.timestamps)
        t_norm = [t / t_max for t in metrics.timestamps]
        
        # Fit model
        predicted = [model_func(t) for t in t_norm]
        
        # Scale predicted to match actual magnitude
        scale = sum(metrics.impressions) / sum(predicted) if sum(predicted) > 0 else 1.0
        predicted = [p * scale for p in predicted]
        
        # Compute R²
        ss_res = sum((o - p)**2 for o, p in zip(metrics.impressions, predicted))
        mean_obs = sum(metrics.impressions) / len(metrics.impressions)
        ss_tot = sum((o - mean_obs)**2 for o in metrics.impressions)
        
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        return max(0.0, r_squared)
    
    def _exponential_decay(self, t: float) -> float:
        """Exponential decay: common for viral content."""
        return math.exp(-3.0 * t)
    
    def _power_law_decay(self, t: float) -> float:
        """Power law decay: common for discovery-driven content."""
        return (t + 0.01) ** -1.5
    
    def _logistic_decay(self, t: float) -> float:
        """Logistic decay: common for audience saturation."""
        return 1.0 / (1.0 + math.exp(10.0 * (t - 0.5)))
    
    def _check_retention_natural(self, retention_curve: list[float]) -> bool:
        """Check if retention curve shows natural viewer behavior."""
        if len(retention_curve) < 10:
            return True  # Insufficient data
        
        # Natural retention curves are monotonically decreasing
        monotonic = all(
            retention_curve[i] >= retention_curve[i+1]
            for i in range(len(retention_curve)-1)
        )
        
        # Natural retention has smooth gradients
        gradients = [
            retention_curve[i] - retention_curve[i+1]
            for i in range(len(retention_curve)-1)
        ]
        gradient_variance = sum((g - sum(gradients)/len(gradients))**2 for g in gradients) / len(gradients)
        smooth = gradient_variance < 0.05
        
        return monotonic and smooth


# ============================================================================
# COUNTERFACTUAL SIMULATOR
# ============================================================================

class CounterfactualSimulator:
    """
    Simulates performance without suppression.
    Used for calibration, RL de-biasing, reactivation logic.
    """
    
    def simulate_unsuppressed(
        self,
        metrics: NormalizedMetrics,
        expectation: ExpectationEnvelope,
        suppression_onset_index: int
    ) -> float:
        """
        Estimate what total impressions would have been without suppression.
        
        Returns counterfactual_total_impressions.
        """
        if suppression_onset_index < 0 or suppression_onset_index >= len(metrics.impressions):
            return sum(metrics.impressions)
        
        # Use pre-suppression trajectory
        pre_suppression = metrics.impressions[:suppression_onset_index]
        
        if len(pre_suppression) < 2:
            return sum(expectation.expected_impressions_median)
        
        # Fit trajectory to pre-suppression data
        growth_rate = self._compute_growth_rate(pre_suppression, metrics.timestamps[:suppression_onset_index])
        
        # Extrapolate with natural decay
        counterfactual = list(pre_suppression)
        last_value = pre_suppression[-1]
        
        for i in range(suppression_onset_index, len(metrics.timestamps)):
            dt = metrics.timestamps[i] - metrics.timestamps[suppression_onset_index]
            # Exponential growth with decay
            decay_factor = math.exp(-dt / 86400.0)  # 1-day half-life
            projected = last_value + growth_rate * dt * decay_factor
            counterfactual.append(projected)
            last_value = projected
        
        return sum(counterfactual)
    
    def _compute_growth_rate(self, impressions: list[float], timestamps: list[float]) -> float:
        """Compute average growth rate from trajectory."""
        if len(impressions) < 2:
            return 0.0
        
        total_growth = impressions[-1] - impressions[0]
        total_time = timestamps[-1] - timestamps[0]
        
        return total_growth / total_time if total_time > 0 else 0.0


# ============================================================================
# PLATFORM SUPPRESSION PROFILES
# ============================================================================

class PlatformSuppressionProfiles:
    """
    Platform-specific suppression behavior signatures.
    Encodes known throttling patterns per platform.
    """
    
    def __init__(self):
        self.profiles = {
            'tiktok': PlatformSuppressionProfile(
                platform='tiktok',
                velocity_cap_threshold=1000.0,  # impressions/sec
                distribution_throttle_patterns=['ceiling', 'plateau'],
                creator_cooldown_window_hours=24.0,
                saturation_decay_rate=0.3,
                known_ceiling_behaviors={'for_you': 500000, 'following': 50000}
            ),
            'youtube_shorts': PlatformSuppressionProfile(
                platform='youtube_shorts',
                velocity_cap_threshold=500.0,
                distribution_throttle_patterns=['slope', 'bucket'],
                creator_cooldown_window_hours=48.0,
                saturation_decay_rate=0.2,
                known_ceiling_behaviors={'browse': 1000000, 'suggested': 100000}
            ),
            'instagram_reels': PlatformSuppressionProfile(
                platform='instagram_reels',
                velocity_cap_threshold=800.0,
                distribution_throttle_patterns=['ceiling', 'cliff'],
                creator_cooldown_window_hours=36.0,
                saturation_decay_rate=0.25,
                known_ceiling_behaviors={'explore': 750000, 'followers': 75000}
            )
        }
    
    def get_profile(self, platform: str) -> Optional[PlatformSuppressionProfile]:
        """Retrieve platform-specific suppression profile."""
        return self.profiles.get(platform.lower())


# ============================================================================
# DRIFT-AWARE THRESHOLDS
# ============================================================================

class DriftAwareThresholds:
    """
    Adaptive thresholds that account for:
    - Algorithm rollout days
    - Platform outages
    - Seasonal variations
    """
    
    def __init__(self):
        self.base_thresholds = {
            'deviation_severity': 0.6,
            'velocity_drop': 0.7,
            'ceiling_confidence': 0.8
        }
        self.drift_events = []  # Platform events that affect thresholds
    
    def get_threshold(
        self,
        metric_name: str,
        platform: str,
        timestamp: float
    ) -> float:
        """Get threshold adjusted for known drift events."""
        base = self.base_thresholds.get(metric_name, 0.5)
        
        # Check for active drift events
        for event in self.drift_events:
            if (event['platform'] == platform and 
                event['start'] <= timestamp <= event['end']):
                # Relax thresholds during known drift periods
                base *= event['threshold_multiplier']
        
        return base
    
    def register_drift_event(
        self,
        platform: str,
        start: float,
        end: float,
        event_type: str,
        threshold_multiplier: float
    ):
        """Register a known drift event (e.g., algorithm rollout)."""
        self.drift_events.append({
            'platform': platform,
            'start': start,
            'end': end,
            'event_type': event_type,
            'threshold_multiplier': threshold_multiplier
        })


# ============================================================================
# SUPPRESSION REPORT BUILDER
# ============================================================================

class SuppressionReportBuilder:
    """
    Assembles final suppression analysis with human-readable evidence.
    """
    
    def build_report(
        self,
        video_id: str,
        platform: str,
        deviation_analysis: dict,
        velocity_anomalies: list[VelocityAnomaly],
        exposure_constraints: dict,
        natural_decay_check: tuple[bool, float, str],
        counterfactual_impressions: float,
        actual_impressions: float
    ) -> SuppressionAnalysisResult:
        """Build complete suppression analysis report."""
        
        # Determine suppression type
        suppression_type = self._classify_suppression_type(
            deviation_analysis,
            velocity_anomalies,
            exposure_constraints,
            natural_decay_check
        )
        
        # If natural decay explains it, no suppression
        is_natural, natural_confidence, decay_model = natural_decay_check
        if is_natural and natural_confidence > 0.7:
            suppression_type = SuppressionType.NONE
        
        # Compute confidence
        confidence = self._compute_confidence(
            deviation_analysis,
            velocity_anomalies,
            exposure_constraints,
            natural_decay_check
        )
        
        # Compute severity
        severity = self._compute_severity(
            deviation_analysis,
            velocity_anomalies,
            counterfactual_impressions,
            actual_impressions
        )
        
        # Build evidence notes
        notes = self._build_evidence_notes(
            deviation_analysis,
            velocity_anomalies,
            exposure_constraints,
            natural_decay_check,
            suppression_type
        )
        
        # Onset estimate
        onset_seconds = self._estimate_onset(deviation_analysis, velocity_anomalies)
        
        # Counterfactual uplift
        uplift = (counterfactual_impressions - actual_impressions) / actual_impressions if actual_impressions > 0 else 0.0
        
        return SuppressionAnalysisResult(
            video_id=video_id,
            platform=platform,
            suppressed=suppression_type != SuppressionType.NONE,
            suppression_type=suppression_type,
            severity=severity,
            confidence=confidence,
            onset_estimate_seconds=onset_seconds,
            counterfactual_uplift_estimate=uplift,
            notes=notes
        )
    
    def _classify_suppression_type(
        self,
        deviation_analysis: dict,
        velocity_anomalies: list[VelocityAnomaly],
        exposure_constraints: dict,
        natural_decay_check: tuple
    ) -> SuppressionType:
        """Classify the type of suppression detected."""
        
        # Check for ceiling patterns
        if exposure_constraints.get('ceiling_detected'):
            return SuppressionType.VELOCITY_CAP
        
        # Check for bucket throttling
        if exposure_constraints.get('bucket_throttle'):
            return SuppressionType.DISTRIBUTION_THROTTLE
        
        # Check for plateau after growth
        plateaus = [a for a in velocity_anomalies if a.pattern_type == 'plateau']
        if plateaus and deviation_analysis.get('significant'):
            return SuppressionType.CREATOR_COOLDOWN
        
        # Check for exposure mismatch
        if exposure_constraints.get('exposure_mismatch'):
            return SuppressionType.AUDIENCE_SATURATION
        
        # If significant but no clear pattern
        if deviation_analysis.get('significant'):
            return SuppressionType.UNKNOWN
        
        return SuppressionType.NONE
    
    def _compute_confidence(
        self,
        deviation_analysis: dict,
        velocity_anomalies: list[VelocityAnomaly],
        exposure_constraints: dict,
        natural_decay_check: tuple
    ) -> float:
        """Compute confidence in suppression detection."""
        is_natural, natural_conf, _ = natural_decay_check
        
        # If natural explains it well, low suppression confidence
        if is_natural and natural_conf > 0.7:
            return 1.0 - natural_conf
        
        # Otherwise, aggregate evidence
        evidence_scores = []
        
        if deviation_analysis.get('significant'):
            evidence_scores.append(deviation_analysis['deviation_severity'])
        
        if velocity_anomalies:
            max_anomaly_severity = max(a.severity for a in velocity_anomalies)
            evidence_scores.append(max_anomaly_severity)
        
        if exposure_constraints.get('constrained'):
            evidence_scores.append(0.8)
        
        # Average evidence with natural decay penalty
        if not evidence_scores:
            return 0.0
        
        raw_confidence = sum(evidence_scores) / len(evidence_scores)
        natural_penalty = natural_conf * 0.5 if is_natural else 0.0
        
        return max(0.0, min(1.0, raw_confidence - natural_penalty))
    
    def _compute_severity(
        self,
        deviation_analysis: dict,
        velocity_anomalies: list[VelocityAnomaly],
        counterfactual: float,
        actual: float
    ) -> float:
        """Compute severity of suppression impact."""
        # Primary measure: counterfactual vs actual gap
        if actual > 0:
            impact_ratio = (counterfactual - actual) / counterfactual
        else:
            impact_ratio = 0.0
        
        # Secondary measure: deviation severity
        deviation_severity = deviation_analysis.get('deviation_severity', 0.0)
        
        # Tertiary measure: velocity drop
        velocity_severity = max(
            (a.severity for a in velocity_anomalies),
            default=0.0
        )
        
        # Weighted combination
        severity = (
            0.5 * impact_ratio +
            0.3 * deviation_severity +
            0.2 * velocity_severity
        )
        
        return max(0.0, min(1.0, severity))
    
    def _build_evidence_notes(
        self,
        deviation_analysis: dict,
        velocity_anomalies: list[VelocityAnomaly],
        exposure_constraints: dict,
        natural_decay_check: tuple,
        suppression_type: SuppressionType
    ) -> list[str]:
        """Build human-readable evidence trail."""
        notes = []
        
        # Natural decay check
        is_natural, natural_conf, decay_model = natural_decay_check
        if is_natural:
            notes.append(f"Performance matches {decay_model} decay model (conf={natural_conf:.2f})")
        
        # Deviation evidence
        if deviation_analysis.get('significant'):
            notes.append(
                f"Significant deviation detected: {deviation_analysis['statistical_significance']:.1f}σ "
                f"at index {deviation_analysis['deviation_onset_index']} ({deviation_analysis['deviation_type']})"
            )
        
        # Velocity anomalies
        for anomaly in velocity_anomalies[:3]:  # Top 3
            notes.append(
                f"{anomaly.pattern_type.upper()} at t={anomaly.onset_timestamp:.0f}s: "
                f"velocity {anomaly.pre_velocity:.1f}→{anomaly.post_velocity:.1f} (severity={anomaly.severity:.2f})"
            )
        
        # Exposure constraints
        if exposure_constraints.get('ceiling_detected'):
            notes.append(
                f"Hard ceiling detected at {exposure_constraints['ceiling_value']:.0f} impressions"
            )
        
        if exposure_constraints.get('bucket_throttle'):
            notes.append("Time-bucket throttling pattern detected")
        
        if exposure_constraints.get('exposure_mismatch'):
            notes.append("Engagement growth without proportional exposure increase")
        
        # Final classification
        notes.append(f"Classification: {suppression_type.value}")
        
        return notes
    
    def _estimate_onset(
        self,
        deviation_analysis: dict,
        velocity_anomalies: list[VelocityAnomaly]
    ) -> float:
        """Estimate when suppression began (seconds into lifecycle)."""
        candidates = []
        
        if deviation_analysis.get('deviation_onset_index', -1) >= 0:
            # Convert index to timestamp would require original metrics
            # For now, use index as proxy
            candidates.append(float(deviation_analysis['deviation_onset_index']) * 300.0)  # Assume 5min intervals
        
        for anomaly in velocity_anomalies:
            candidates.append(anomaly.onset_timestamp)
        
        return min(candidates) if candidates else 0.0


# ============================================================================
# MAIN SUPPRESSION ANALYZER
# ============================================================================

class SuppressionAnalyzer:
    """
    Main orchestrator for suppression detection pipeline.
    Deterministic, replayable, audit-friendly.
    """
    
    def __init__(
        self,
        historical_cohorts: dict,
        creator_priors: dict,
        platform_profiles: Optional[PlatformSuppressionProfiles] = None
    ):
        self.expectation_builder = BaselineExpectationBuilder(
            historical_cohorts, creator_priors
        )
        self.deviation_analyzer = PerformanceDeviationAnalyzer()
        self.velocity_detector = VelocityAnomalyDetector()
        self.exposure_detector = ExposureConstraintDetector()
        self.decay_discriminator = NaturalDecayDiscriminator()
        self.counterfactual_simulator = CounterfactualSimulator()
        self.platform_profiles = platform_profiles or PlatformSuppressionProfiles()
        self.drift_thresholds = DriftAwareThresholds()
        self.report_builder = SuppressionReportBuilder()
    
    def analyze(self, metrics: NormalizedMetrics) -> SuppressionAnalysisResult:
        """
        Complete suppression analysis pipeline.
        
        Deterministic and replayable for identical inputs.
        """
        # Step 1: Build expectation envelope
        expectation = self.expectation_builder.build_expectation(metrics)
        
        # Step 2: Analyze performance deviation
        deviation_analysis = self.deviation_analyzer.analyze_deviation(
            metrics, expectation
        )
        
        # Step 3: Detect velocity anomalies
        velocity_anomalies = self.velocity_detector.detect_anomalies(metrics)
        
        # Step 4: Detect exposure constraints
        exposure_constraints = self.exposure_detector.detect_constraints(metrics)
        
        # Step 5: Check for natural decay
        natural_decay_check = self.decay_discriminator.is_natural_decay(
            metrics, expectation
        )
        
        # Step 6: Simulate counterfactual
        onset_index = deviation_analysis.get('deviation_onset_index', -1)
        counterfactual_impressions = self.counterfactual_simulator.simulate_unsuppressed(
            metrics, expectation, onset_index
        )
        actual_impressions = sum(metrics.impressions)
        
        # Step 7: Build final report
        result = self.report_builder.build_report(
            video_id=metrics.video_id,
            platform=metrics.platform,
            deviation_analysis=deviation_analysis,
            velocity_anomalies=velocity_anomalies,
            exposure_constraints=exposure_constraints,
            natural_decay_check=natural_decay_check,
            counterfactual_impressions=counterfactual_impressions,
            actual_impressions=actual_impressions
        )
        
        return result
    
    def batch_analyze(
        self,
        metrics_batch: list[NormalizedMetrics]
    ) -> list[SuppressionAnalysisResult]:
        """Analyze multiple videos in batch."""
        return [self.analyze(m) for m in metrics_batch]


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

def example_usage():
    """Demonstrate suppression analyzer usage."""
    
    # Mock historical data
    historical_cohorts = {
        'tiktok_v3_e5': {
            'impressions_median': [100 * i for i in range(100)],
            'impressions_p25': [80 * i for i in range(100)],
            'impressions_p75': [120 * i for i in range(100)]
        }
    }
    
    creator_priors = {
        'creator_ab': {
            'typical_scale': 1.5
        }
    }
    
    # Create analyzer
    analyzer = SuppressionAnalyzer(
        historical_cohorts=historical_cohorts,
        creator_priors=creator_priors
    )
    
    # Mock metrics
    metrics = NormalizedMetrics(
        video_id='vid_12345',
        platform='tiktok',
        timestamps=[i * 300.0 for i in range(100)],
        impressions=[100 * i if i < 50 else 5000 for i in range(100)],  # Ceiling
        engagements=[10 * i for i in range(100)],
        retention_curve=[1.0 - 0.01 * i for i in range(100)],
        velocity_curve=[1.0 for _ in range(100)]
    )
    
    # Analyze
    result = analyzer.analyze(metrics)
    
    print(f"Suppressed: {result.suppressed}")
    print(f"Type: {result.suppression_type.value}")
    print(f"Severity: {result.severity:.2f}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Counterfactual uplift: {result.counterfactual_uplift_estimate:.2%}")
    print(f"\nEvidence:")
    for note in result.notes:
        print(f"  - {note}")


if __name__ == '__main__':
    example_usage()