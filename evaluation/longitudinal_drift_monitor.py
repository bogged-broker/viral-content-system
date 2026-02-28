"""
evaluation/longitudinal_drift_monitor.py

Systemic Virality Drift Detection & Learning Integrity Guardian

Detects when the meaning of performance changes over time, even when metrics
still "look good." Protects against silent virality decay, false optimization,
platform regime shifts, and slow catastrophic learning collapse.

This is a long-horizon truth validator. It never scores content or affects
posting decisions directly. It detects distributional, causal, and reward drift
before revenue and views collapse.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Tuple, List, Dict

import numpy as np
from scipy import stats


# ============================================================================
# Core Data Structures
# ============================================================================

class DriftType(Enum):
    """Categorizes drift mechanisms - no ad-hoc categories allowed."""
    DISTRIBUTIONAL = "distributional"
    CAUSAL = "causal"
    REWARD = "reward"
    PLATFORM_REGIME = "platform_regime"


class DriftSeverity(Enum):
    """Drift severity levels - never binary."""
    LOW = "low"
    MODERATE = "moderate"
    CRITICAL = "critical"


@dataclass(frozen=True)
class DriftEvent:
    """
    Immutable record of detected drift.
    
    Evidence must be interpretable by humans for leadership review,
    rollback justification, and kill-switch decisions.
    """
    drift_type: DriftType
    platform: str
    niche: str | None
    
    onset_timestamp: float
    detection_timestamp: float
    
    severity: float  # [0,1]
    confidence: float  # [0,1]
    
    affected_signals: list[str]
    evidence: list[str]
    
    def __post_init__(self):
        assert 0 <= self.severity <= 1, "severity must be in [0,1]"
        assert 0 <= self.confidence <= 1, "confidence must be in [0,1]"
        assert len(self.evidence) > 0, "evidence is required"


@dataclass(frozen=True)
class DriftMonitorResult:
    """
    Final output contract - no prescriptions, only truth signals.
    """
    platform: str
    niche: str | None
    
    drift_detected: bool
    drift_events: list[DriftEvent]
    
    system_stability_score: float  # [0,1]
    recommendation_flags: list[str]
    
    def __post_init__(self):
        assert 0 <= self.system_stability_score <= 1


@dataclass
class TimeSeriesSnapshot:
    """Single point-in-time evaluation snapshot."""
    timestamp: float
    platform: str
    niche: str | None
    
    # From /evaluation/ outputs only
    metric_distributions: dict[str, np.ndarray]
    viral_scores: np.ndarray
    engagement_envelope: dict[str, float]
    suppression_adjusted_signals: dict[str, float]
    feature_attributions: dict[str, float]


@dataclass
class ReferenceWindow:
    """Stable historical period for drift comparison."""
    start_timestamp: float
    end_timestamp: float
    platform: str
    niche: str | None
    
    snapshots: list[TimeSeriesSnapshot]
    
    # Aggregated statistics
    metric_quantiles: dict[str, dict[str, float]]  # metric -> quantile -> value
    baseline_distributions: dict[str, np.ndarray]
    causal_strengths: dict[str, float]  # feature -> outcome correlation


# ============================================================================
# Reference Window Builder
# ============================================================================

class ReferenceWindowBuilder:
    """
    Defines stable historical periods for drift detection.
    
    Without a reference window, drift detection is meaningless.
    """
    
    def __init__(
        self,
        min_window_days: int = 14,
        max_window_days: int = 90,
        stability_threshold: float = 0.15
    ):
        self.min_window_days = min_window_days
        self.max_window_days = max_window_days
        self.stability_threshold = stability_threshold
    
    def build_reference_window(
        self,
        snapshots: list[TimeSeriesSnapshot],
        platform: str,
        niche: str | None
    ) -> ReferenceWindow | None:
        """
        Identifies last-known-good regime from historical snapshots.
        
        Returns None if no stable window can be established.
        """
        if len(snapshots) < 7:  # Minimum viable window
            return None
        
        # Filter by platform/niche
        filtered = [
            s for s in snapshots
            if s.platform == platform and s.niche == niche
        ]
        
        if len(filtered) < 7:
            return None
        
        # Sort by timestamp
        filtered.sort(key=lambda s: s.timestamp)
        
        # Find most recent stable period
        window = self._find_stable_period(filtered)
        
        if window is None:
            return None
        
        # Compute aggregated statistics
        metric_quantiles = self._compute_metric_quantiles(window)
        baseline_distributions = self._extract_baseline_distributions(window)
        causal_strengths = self._compute_causal_strengths(window)
        
        return ReferenceWindow(
            start_timestamp=window[0].timestamp,
            end_timestamp=window[-1].timestamp,
            platform=platform,
            niche=niche,
            snapshots=window,
            metric_quantiles=metric_quantiles,
            baseline_distributions=baseline_distributions,
            causal_strengths=causal_strengths
        )
    
    def _find_stable_period(
        self,
        snapshots: list[TimeSeriesSnapshot]
    ) -> list[TimeSeriesSnapshot] | None:
        """Identifies period with low variance in key metrics."""
        
        min_days_seconds = self.min_window_days * 86400
        max_days_seconds = self.max_window_days * 86400
        
        # Work backwards from most recent
        for end_idx in range(len(snapshots) - 1, 6, -1):
            for start_idx in range(max(0, end_idx - 100), end_idx - 6):
                window = snapshots[start_idx:end_idx + 1]
                
                duration = window[-1].timestamp - window[0].timestamp
                if duration < min_days_seconds or duration > max_days_seconds:
                    continue
                
                if self._is_stable_window(window):
                    return window
        
        return None
    
    def _is_stable_window(self, window: list[TimeSeriesSnapshot]) -> bool:
        """Checks if window has consistent metric distributions."""
        
        # Extract viral scores across window
        all_scores = []
        for snap in window:
            if len(snap.viral_scores) > 0:
                all_scores.extend(snap.viral_scores)
        
        if len(all_scores) < 20:
            return False
        
        # Check variance stability
        chunk_size = len(all_scores) // 3
        chunks = [
            all_scores[i:i + chunk_size]
            for i in range(0, len(all_scores), chunk_size)
            if len(all_scores[i:i + chunk_size]) > 5
        ]
        
        if len(chunks) < 3:
            return False
        
        variances = [np.var(chunk) for chunk in chunks]
        cv = np.std(variances) / (np.mean(variances) + 1e-9)
        
        return cv < self.stability_threshold
    
    def _compute_metric_quantiles(
        self,
        window: list[TimeSeriesSnapshot]
    ) -> dict[str, dict[str, float]]:
        """Computes quantiles for each metric across window."""
        
        result = defaultdict(dict)
        
        # Aggregate all metric values
        metric_values = defaultdict(list)
        for snap in window:
            for metric_name, dist in snap.metric_distributions.items():
                metric_values[metric_name].extend(dist)
        
        # Compute quantiles
        quantiles = [0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99]
        for metric_name, values in metric_values.items():
            if len(values) > 0:
                q_values = np.quantile(values, quantiles)
                for q, v in zip(quantiles, q_values):
                    result[metric_name][f"p{int(q*100)}"] = float(v)
        
        return dict(result)
    
    def _extract_baseline_distributions(
        self,
        window: list[TimeSeriesSnapshot]
    ) -> dict[str, np.ndarray]:
        """Extracts full distributions for KS/Wasserstein tests."""
        
        result = {}
        metric_values = defaultdict(list)
        
        for snap in window:
            for metric_name, dist in snap.metric_distributions.items():
                metric_values[metric_name].extend(dist)
        
        for metric_name, values in metric_values.items():
            result[metric_name] = np.array(values)
        
        return result
    
    def _compute_causal_strengths(
        self,
        window: list[TimeSeriesSnapshot]
    ) -> dict[str, float]:
        """Computes feature-outcome correlation strengths."""
        
        # Aggregate feature attributions and viral scores
        feature_vectors = defaultdict(list)
        viral_scores = []
        
        for snap in window:
            if len(snap.viral_scores) > 0:
                avg_score = float(np.mean(snap.viral_scores))
                viral_scores.append(avg_score)
                
                for feature, attribution in snap.feature_attributions.items():
                    feature_vectors[feature].append(attribution)
        
        if len(viral_scores) < 5:
            return {}
        
        # Compute correlations
        causal_strengths = {}
        for feature, values in feature_vectors.items():
            if len(values) == len(viral_scores):
                corr, _ = stats.pearsonr(values, viral_scores)
                causal_strengths[feature] = abs(corr)
        
        return causal_strengths


# ============================================================================
# Rolling Baseline Constructor
# ============================================================================

class RollingBaselineConstructor:
    """
    Maintains evolving baselines for drift detection.
    
    Baselines must evolve slowly, not reactively.
    """
    
    def __init__(
        self,
        window_size: int = 30,  # snapshots
        update_rate: float = 0.1  # exponential smoothing factor
    ):
        self.window_size = window_size
        self.update_rate = update_rate
        
        self._rolling_medians: dict[str, float] = {}
        self._quantile_envelopes: dict[str, dict[str, float]] = {}
        self._variance_bands: dict[str, float] = {}
    
    def update_baseline(
        self,
        snapshots: list[TimeSeriesSnapshot],
        metric_name: str
    ):
        """Updates rolling baseline from recent snapshots."""
        
        # Extract metric values
        values = []
        for snap in snapshots[-self.window_size:]:
            if metric_name in snap.metric_distributions:
                values.extend(snap.metric_distributions[metric_name])
        
        if len(values) == 0:
            return
        
        values_array = np.array(values)
        
        # Update rolling median
        new_median = float(np.median(values_array))
        if metric_name in self._rolling_medians:
            self._rolling_medians[metric_name] = (
                (1 - self.update_rate) * self._rolling_medians[metric_name] +
                self.update_rate * new_median
            )
        else:
            self._rolling_medians[metric_name] = new_median
        
        # Update quantile envelope
        quantiles = [0.05, 0.25, 0.75, 0.95]
        q_values = np.quantile(values_array, quantiles)
        self._quantile_envelopes[metric_name] = {
            "p5": float(q_values[0]),
            "p25": float(q_values[1]),
            "p75": float(q_values[2]),
            "p95": float(q_values[3])
        }
        
        # Update variance band
        self._variance_bands[metric_name] = float(np.var(values_array))
    
    def get_median(self, metric_name: str) -> float | None:
        """Returns current rolling median."""
        return self._rolling_medians.get(metric_name)
    
    def get_quantile_envelope(
        self,
        metric_name: str
    ) -> dict[str, float] | None:
        """Returns current quantile envelope."""
        return self._quantile_envelopes.get(metric_name)
    
    def get_variance(self, metric_name: str) -> float | None:
        """Returns current variance estimate."""
        return self._variance_bands.get(metric_name)


# ============================================================================
# Distribution Shift Detector
# ============================================================================

class DistributionShiftDetector:
    """
    Detects metric distribution shifts using statistical tests.
    
    Looks for:
    - Distribution shape changes
    - Tail compression/expansion
    - Variance collapse (very dangerous)
    """
    
    def __init__(
        self,
        ks_threshold: float = 0.15,
        wasserstein_threshold: float = 0.20,
        variance_collapse_threshold: float = 0.30
    ):
        self.ks_threshold = ks_threshold
        self.wasserstein_threshold = wasserstein_threshold
        self.variance_collapse_threshold = variance_collapse_threshold
    
    def detect_shift(
        self,
        reference_dist: np.ndarray,
        current_dist: np.ndarray,
        metric_name: str
    ) -> tuple[bool, float, list[str]]:
        """
        Detects distribution shift between reference and current.
        
        Returns:
            (shift_detected, severity, evidence_strings)
        """
        
        if len(reference_dist) < 10 or len(current_dist) < 10:
            return False, 0.0, []
        
        evidence = []
        severity = 0.0
        
        # KS test for distribution divergence
        ks_stat, ks_pval = stats.ks_2samp(reference_dist, current_dist)
        if ks_stat > self.ks_threshold:
            evidence.append(
                f"KS divergence {ks_stat:.3f} exceeds threshold "
                f"{self.ks_threshold:.3f} for {metric_name}"
            )
            severity = max(severity, ks_stat)
        
        # Wasserstein distance for distribution shift magnitude
        wass_dist = stats.wasserstein_distance(reference_dist, current_dist)
        ref_scale = np.std(reference_dist) + 1e-9
        normalized_wass = wass_dist / ref_scale
        
        if normalized_wass > self.wasserstein_threshold:
            evidence.append(
                f"Wasserstein distance {normalized_wass:.3f} exceeds "
                f"threshold {self.wasserstein_threshold:.3f} for {metric_name}"
            )
            severity = max(severity, normalized_wass)
        
        # Variance collapse detection
        ref_var = np.var(reference_dist)
        cur_var = np.var(current_dist)
        variance_ratio = cur_var / (ref_var + 1e-9)
        
        if variance_ratio < (1 - self.variance_collapse_threshold):
            evidence.append(
                f"Variance collapsed by {(1-variance_ratio)*100:.1f}% "
                f"for {metric_name} (very dangerous)"
            )
            severity = max(severity, 1 - variance_ratio)
        
        # Tail compression/expansion
        ref_p95 = np.percentile(reference_dist, 95)
        cur_p95 = np.percentile(current_dist, 95)
        tail_ratio = cur_p95 / (ref_p95 + 1e-9)
        
        if tail_ratio < 0.7 or tail_ratio > 1.4:
            evidence.append(
                f"P95 tail shifted by {(tail_ratio-1)*100:.1f}% for {metric_name}"
            )
            severity = max(severity, abs(tail_ratio - 1))
        
        shift_detected = len(evidence) > 0
        return shift_detected, min(severity, 1.0), evidence


# ============================================================================
# Feature-Outcome Decoupling Analyzer
# ============================================================================

class FeatureOutcomeDecouplingAnalyzer:
    """
    CRITICAL: Detects when features that used to predict success no longer do.
    
    This catches:
    - Broken feature extraction
    - Platform deprioritizing signals
    - Semantic drift in engagement
    
    If correlation dies silently, learning breaks.
    """
    
    def __init__(
        self,
        decoupling_threshold: float = 0.40,
        min_reference_correlation: float = 0.15
    ):
        self.decoupling_threshold = decoupling_threshold
        self.min_reference_correlation = min_reference_correlation
    
    def analyze_decoupling(
        self,
        reference_causal_strengths: dict[str, float],
        current_snapshots: list[TimeSeriesSnapshot]
    ) -> tuple[bool, float, list[str]]:
        """
        Checks if feature-outcome correlations have degraded.
        
        Returns:
            (decoupling_detected, severity, evidence_strings)
        """
        
        # Compute current causal strengths
        current_strengths = self._compute_current_causal_strengths(
            current_snapshots
        )
        
        evidence = []
        severity = 0.0
        
        # Check each feature that was previously predictive
        for feature, ref_strength in reference_causal_strengths.items():
            if ref_strength < self.min_reference_correlation:
                continue  # Feature wasn't meaningfully predictive
            
            cur_strength = current_strengths.get(feature, 0.0)
            strength_drop = ref_strength - cur_strength
            
            if strength_drop > self.decoupling_threshold:
                evidence.append(
                    f"Feature '{feature}' correlation dropped from "
                    f"{ref_strength:.3f} to {cur_strength:.3f} "
                    f"(Δ = {strength_drop:.3f})"
                )
                severity = max(severity, strength_drop)
        
        decoupling_detected = len(evidence) > 0
        return decoupling_detected, min(severity, 1.0), evidence
    
    def _compute_current_causal_strengths(
        self,
        snapshots: list[TimeSeriesSnapshot]
    ) -> dict[str, float]:
        """Computes feature-outcome correlations for current period."""
        
        feature_vectors = defaultdict(list)
        viral_scores = []
        
        for snap in snapshots:
            if len(snap.viral_scores) > 0:
                avg_score = float(np.mean(snap.viral_scores))
                viral_scores.append(avg_score)
                
                for feature, attribution in snap.feature_attributions.items():
                    feature_vectors[feature].append(attribution)
        
        if len(viral_scores) < 5:
            return {}
        
        causal_strengths = {}
        for feature, values in feature_vectors.items():
            if len(values) == len(viral_scores):
                corr, _ = stats.pearsonr(values, viral_scores)
                causal_strengths[feature] = abs(corr)
        
        return causal_strengths


# ============================================================================
# Reward Alignment Auditor
# ============================================================================

class RewardAlignmentAuditor:
    """
    Validates that optimization targets, evaluation metrics, and actual
    long-term views remain aligned.
    
    Prevents RL agents from optimizing proxy nonsense.
    """
    
    def __init__(
        self,
        misalignment_threshold: float = 0.35
    ):
        self.misalignment_threshold = misalignment_threshold
    
    def audit_alignment(
        self,
        reference_window: ReferenceWindow,
        current_snapshots: list[TimeSeriesSnapshot]
    ) -> tuple[bool, float, list[str]]:
        """
        Checks if reward signals still align with true virality.
        
        Returns:
            (misalignment_detected, severity, evidence_strings)
        """
        
        # Extract viral scores and engagement signals
        ref_viral = []
        ref_engagement = []
        
        for snap in reference_window.snapshots:
            if len(snap.viral_scores) > 0:
                ref_viral.extend(snap.viral_scores)
                avg_engagement = np.mean(list(snap.engagement_envelope.values()))
                ref_engagement.append(avg_engagement)
        
        cur_viral = []
        cur_engagement = []
        
        for snap in current_snapshots:
            if len(snap.viral_scores) > 0:
                cur_viral.extend(snap.viral_scores)
                avg_engagement = np.mean(list(snap.engagement_envelope.values()))
                cur_engagement.append(avg_engagement)
        
        if len(ref_viral) < 10 or len(cur_viral) < 10:
            return False, 0.0, []
        
        # Compute viral-engagement correlation in both periods
        ref_corr, _ = stats.pearsonr(
            ref_viral[:min(len(ref_viral), len(ref_engagement))],
            ref_engagement[:min(len(ref_viral), len(ref_engagement))]
        )
        
        cur_corr, _ = stats.pearsonr(
            cur_viral[:min(len(cur_viral), len(cur_engagement))],
            cur_engagement[:min(len(cur_viral), len(cur_engagement))]
        )
        
        evidence = []
        severity = 0.0
        
        correlation_drop = abs(ref_corr - cur_corr)
        if correlation_drop > self.misalignment_threshold:
            evidence.append(
                f"Viral score vs engagement correlation degraded from "
                f"{ref_corr:.3f} to {cur_corr:.3f} (Δ = {correlation_drop:.3f})"
            )
            severity = correlation_drop
        
        # Check if optimization proxy diverges from true outcomes
        if cur_corr < 0.3 and ref_corr > 0.6:
            evidence.append(
                f"CRITICAL: Reward proxy correlation collapsed to {cur_corr:.3f} "
                f"(was {ref_corr:.3f}). RL agents may be optimizing nonsense."
            )
            severity = max(severity, 0.9)
        
        misalignment_detected = len(evidence) > 0
        return misalignment_detected, min(severity, 1.0), evidence


# ============================================================================
# Platform Regime Change Scanner
# ============================================================================

class PlatformRegimeChangeScanner:
    """
    Looks for synchronized drift across niches.
    
    If many niches shift at once without content changes, this likely
    indicates a platform algorithm change.
    """
    
    def __init__(
        self,
        synchronization_threshold: float = 0.60
    ):
        self.synchronization_threshold = synchronization_threshold
    
    def scan_for_regime_change(
        self,
        drift_events_by_niche: dict[str, list[DriftEvent]],
        platform: str
    ) -> tuple[bool, float, list[str]]:
        """
        Detects platform-wide algorithm changes.
        
        Returns:
            (regime_change_detected, severity, evidence_strings)
        """
        
        if len(drift_events_by_niche) < 3:
            return False, 0.0, []
        
        # Count niches with recent drift
        niches_with_drift = sum(
            1 for events in drift_events_by_niche.values()
            if len(events) > 0
        )
        
        total_niches = len(drift_events_by_niche)
        synchronization_ratio = niches_with_drift / total_niches
        
        evidence = []
        severity = 0.0
        
        if synchronization_ratio > self.synchronization_threshold:
            evidence.append(
                f"{niches_with_drift}/{total_niches} niches show drift on "
                f"{platform} (sync ratio = {synchronization_ratio:.2f})"
            )
            severity = synchronization_ratio
            
            # Check temporal clustering
            all_onset_times = []
            for events in drift_events_by_niche.values():
                all_onset_times.extend([e.onset_timestamp for e in events])
            
            if len(all_onset_times) > 0:
                time_variance = np.var(all_onset_times)
                time_range = max(all_onset_times) - min(all_onset_times)
                
                if time_range < 7 * 86400:  # Within 7 days
                    evidence.append(
                        f"Drift events temporally clustered within "
                        f"{time_range/86400:.1f} days - likely platform change"
                    )
                    severity = max(severity, 0.85)
        
        regime_change_detected = len(evidence) > 0
        return regime_change_detected, min(severity, 1.0), evidence


# ============================================================================
# Temporal Stability Scorer
# ============================================================================

class TemporalStabilityScorer:
    """
    Measures overall system stability over time.
    
    Combines multiple drift signals into a single stability score.
    """
    
    def compute_stability_score(
        self,
        drift_events: list[DriftEvent],
        lookback_days: int = 30
    ) -> float:
        """
        Computes system stability score [0,1].
        
        1.0 = perfectly stable
        0.0 = complete instability
        """
        
        if len(drift_events) == 0:
            return 1.0
        
        # Filter to recent events
        now = max(e.detection_timestamp for e in drift_events)
        lookback_seconds = lookback_days * 86400
        
        recent_events = [
            e for e in drift_events
            if now - e.detection_timestamp < lookback_seconds
        ]
        
        if len(recent_events) == 0:
            return 1.0
        
        # Weight by severity and recency
        total_severity = 0.0
        total_weight = 0.0
        
        for event in recent_events:
            age_days = (now - event.detection_timestamp) / 86400
            recency_weight = math.exp(-age_days / 10.0)  # Exponential decay
            
            total_severity += event.severity * recency_weight
            total_weight += recency_weight
        
        avg_weighted_severity = total_severity / (total_weight + 1e-9)
        
        # Convert severity to stability (inverse)
        stability_score = 1.0 - min(avg_weighted_severity, 1.0)
        
        return stability_score


# ============================================================================
# False Drift Suppressor
# ============================================================================

class FalseDriftSuppressor:
    """
    Filters out short-term volatility, viral outliers, sampling artifacts,
    and reporting delays.
    
    Drift must persist across windows to be trusted.
    """
    
    def __init__(
        self,
        persistence_threshold: int = 3,  # Must appear in N consecutive checks
        outlier_percentile: float = 0.95
    ):
        self.persistence_threshold = persistence_threshold
        self.outlier_percentile = outlier_percentile
        
        self._drift_candidate_history: dict[str, list[float]] = defaultdict(list)
    
    def filter_drift_event(
        self,
        drift_event: DriftEvent,
        current_timestamp: float
    ) -> bool:
        """
        Returns True if drift event is likely genuine, False if likely noise.
        """
        
        # Build unique key for this drift type + platform + niche
        key = f"{drift_event.drift_type.value}:{drift_event.platform}:{drift_event.niche}"
        
        # Record detection
        self._drift_candidate_history[key].append(current_timestamp)
        
        # Keep only recent detections (last 7 days)
        lookback = 7 * 86400
        self._drift_candidate_history[key] = [
            t for t in self._drift_candidate_history[key]
            if current_timestamp - t < lookback
        ]
        
        # Check persistence
        recent_detections = len(self._drift_candidate_history[key])
        
        if recent_detections < self.persistence_threshold:
            return False  # Not persistent enough
        
        # Check temporal clustering (should be spread, not single spike)
        timestamps = sorted(self._drift_candidate_history[key])
        if len(timestamps) >= 3:
            time_gaps = [timestamps[i+1] - timestamps[i] for i in range(len(timestamps)-1)]
            max_gap = max(time_gaps)
            
            if max_gap > 5 * 86400:  # > 5 days between detections
                return False  # Too sporadic
        
        return True
    
    def suppress_outlier_driven_drift(
        self,
        reference_dist: np.ndarray,
        current_dist: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Removes extreme outliers that might trigger false drift signals.
        
        Returns cleaned distributions.
        """
        
        ref_threshold = np.percentile(reference_dist, self.outlier_percentile * 100)
        cur_threshold = np.percentile(current_dist, self.outlier_percentile * 100)
        
        ref_clean = reference_dist[reference_dist <= ref_threshold]
        cur_clean = current_dist[current_dist <= cur_threshold]
        
        return ref_clean, cur_clean


# ============================================================================
# Drift Report Builder
# ============================================================================

class DriftReportBuilder:
    """
    Constructs human-readable drift reports with actionable recommendations.
    """
    
    def build_report(
        self,
        drift_events: list[DriftEvent],
        stability_score: float,
        platform: str,
        niche: str | None
    ) -> DriftMonitorResult:
        """Assembles final drift monitoring result."""
        
        # Generate recommendation flags
        flags = []
        
        critical_events = [e for e in drift_events if e.severity > 0.7]
        if len(critical_events) > 0:
            flags.append("CRITICAL_DRIFT_DETECTED")
        
        causal_drift = [e for e in drift_events if e.drift_type == DriftType.CAUSAL]
        if len(causal_drift) > 0:
            flags.append("FEATURE_OUTCOME_DECOUPLING")
        
        reward_drift = [e for e in drift_events if e.drift_type == DriftType.REWARD]
        if len(reward_drift) > 0:
            flags.append("REWARD_MISALIGNMENT")
        
        platform_regime = [e for e in drift_events if e.drift_type == DriftType.PLATFORM_REGIME]
        if len(platform_regime) > 0:
            flags.append("PLATFORM_ALGORITHM_CHANGE")
        
        if stability_score < 0.5:
            flags.append("LOW_SYSTEM_STABILITY")
        
        drift_detected = len(drift_events) > 0
        
        return DriftMonitorResult(
            platform=platform,
            niche=niche,
            drift_detected=drift_detected,
            drift_events=drift_events,
            system_stability_score=stability_score,
            recommendation_flags=flags
        )


# ============================================================================
# Main Longitudinal Drift Monitor
# ============================================================================

class LongitudinalDriftMonitor:
    """
    Main orchestrator for drift detection.
    
    Answers the question: "Is our system still learning the same reality
    it was trained on?"
    
    This is a seismograph - it detects pressure changes before earthquakes
    happen at the surface.
    """
    
    def __init__(
        self,
        reference_window_builder: ReferenceWindowBuilder | None = None,
        rolling_baseline: RollingBaselineConstructor | None = None,
        distribution_detector: DistributionShiftDetector | None = None,
        decoupling_analyzer: FeatureOutcomeDecouplingAnalyzer | None = None,
        reward_auditor: RewardAlignmentAuditor | None = None,
        regime_scanner: PlatformRegimeChangeScanner | None = None,
        stability_scorer: TemporalStabilityScorer | None = None,
        false_drift_suppressor: FalseDriftSuppressor | None = None,
        report_builder: DriftReportBuilder | None = None
    ):
        # Initialize components with defaults if not provided
        self.reference_builder = reference_window_builder or ReferenceWindowBuilder()
        self.rolling_baseline = rolling_baseline or RollingBaselineConstructor()
        self.distribution_detector = distribution_detector or DistributionShiftDetector()
        self.decoupling_analyzer = decoupling_analyzer or FeatureOutcomeDecouplingAnalyzer()
        self.reward_auditor = reward_auditor or RewardAlignmentAuditor()
        self.regime_scanner = regime_scanner or PlatformRegimeChangeScanner()
        self.stability_scorer = stability_scorer or TemporalStabilityScorer()
        self.false_drift_suppressor = false_drift_suppressor or FalseDriftSuppressor()
        self.report_builder = report_builder or DriftReportBuilder()
        
        # State
        self._reference_windows: dict[tuple[str, str | None], ReferenceWindow] = {}
        self._all_drift_events: list[DriftEvent] = []
    
    def monitor_drift(
        self,
        historical_snapshots: list[TimeSeriesSnapshot],
        current_snapshots: list[TimeSeriesSnapshot],
        platform: str,
        niche: str | None = None,
        current_timestamp: float | None = None
    ) -> DriftMonitorResult:
        """
        Main entry point for drift monitoring.
        
        Args:
            historical_snapshots: Past evaluation snapshots for reference
            current_snapshots: Recent evaluation snapshots to check for drift
            platform: Platform identifier
            niche: Optional niche identifier
            current_timestamp: Current time (for deterministic replay)
        
        Returns:
            DriftMonitorResult with detected drift events and recommendations
        """
        
        if current_timestamp is None:
            current_timestamp = max(s.timestamp for s in current_snapshots)
        
        # Build or retrieve reference window
        key = (platform, niche)
        if key not in self._reference_windows:
            ref_window = self.reference_builder.build_reference_window(
                historical_snapshots,
                platform,
                niche
            )
            if ref_window is None:
                # Cannot establish reference - return no drift
                return DriftMonitorResult(
                    platform=platform,
                    niche=niche,
                    drift_detected=False,
                    drift_events=[],
                    system_stability_score=1.0,
                    recommendation_flags=["INSUFFICIENT_REFERENCE_DATA"]
                )
            self._reference_windows[key] = ref_window
        
        ref_window = self._reference_windows[key]
        
        # Update rolling baselines
        for metric_name in ref_window.baseline_distributions.keys():
            self.rolling_baseline.update_baseline(current_snapshots, metric_name)
        
        # Detect drift across all dimensions
        drift_events = []
        
        # 1. Distributional drift
        dist_events = self._detect_distributional_drift(
            ref_window,
            current_snapshots,
            platform,
            niche,
            current_timestamp
        )
        drift_events.extend(dist_events)
        
        # 2. Causal drift (feature-outcome decoupling)
        causal_events = self._detect_causal_drift(
            ref_window,
            current_snapshots,
            platform,
            niche,
            current_timestamp
        )
        drift_events.extend(causal_events)
        
        # 3. Reward drift
        reward_events = self._detect_reward_drift(
            ref_window,
            current_snapshots,
            platform,
            niche,
            current_timestamp
        )
        drift_events.extend(reward_events)
        
        # Filter false positives
        genuine_drift_events = [
            event for event in drift_events
            if self.false_drift_suppressor.filter_drift_event(event, current_timestamp)
        ]
        
        # Store for regime change detection
        self._all_drift_events.extend(genuine_drift_events)
        
        # Compute stability score
        stability_score = self.stability_scorer.compute_stability_score(
            self._all_drift_events
        )
        
        # Build and return report
        return self.report_builder.build_report(
            genuine_drift_events,
            stability_score,
            platform,
            niche
        )
    
    def detect_platform_regime_change(
        self,
        platform: str,
        niches: list[str | None]
    ) -> tuple[bool, float, list[str]]:
        """
        Analyzes drift patterns across multiple niches to detect
        platform-wide algorithm changes.
        """
        
        # Group drift events by niche
        drift_by_niche = {niche: [] for niche in niches}
        
        for event in self._all_drift_events:
            if event.platform == platform and event.niche in drift_by_niche:
                drift_by_niche[event.niche].append(event)
        
        return self.regime_scanner.scan_for_regime_change(
            drift_by_niche,
            platform
        )
    
    def _detect_distributional_drift(
        self,
        ref_window: ReferenceWindow,
        current_snapshots: list[TimeSeriesSnapshot],
        platform: str,
        niche: str | None,
        current_timestamp: float
    ) -> list[DriftEvent]:
        """Detects distribution shifts in metrics."""
        
        events = []
        
        # Aggregate current distributions
        current_distributions = defaultdict(list)
        for snap in current_snapshots:
            for metric_name, dist in snap.metric_distributions.items():
                current_distributions[metric_name].extend(dist)
        
        # Check each metric
        for metric_name, ref_dist in ref_window.baseline_distributions.items():
            if metric_name not in current_distributions:
                continue
            
            cur_dist = np.array(current_distributions[metric_name])
            
            # Suppress outliers
            ref_clean, cur_clean = self.false_drift_suppressor.suppress_outlier_driven_drift(
                ref_dist,
                cur_dist
            )
            
            # Detect shift
            shift_detected, severity, evidence = self.distribution_detector.detect_shift(
                ref_clean,
                cur_clean,
                metric_name
            )
            
            if shift_detected:
                events.append(DriftEvent(
                    drift_type=DriftType.DISTRIBUTIONAL,
                    platform=platform,
                    niche=niche,
                    onset_timestamp=current_snapshots[0].timestamp,
                    detection_timestamp=current_timestamp,
                    severity=severity,
                    confidence=0.85,  # High confidence from statistical tests
                    affected_signals=[metric_name],
                    evidence=evidence
                ))
        
        return events
    
    def _detect_causal_drift(
        self,
        ref_window: ReferenceWindow,
        current_snapshots: list[TimeSeriesSnapshot],
        platform: str,
        niche: str | None,
        current_timestamp: float
    ) -> list[DriftEvent]:
        """Detects feature-outcome decoupling."""
        
        decoupling_detected, severity, evidence = self.decoupling_analyzer.analyze_decoupling(
            ref_window.causal_strengths,
            current_snapshots
        )
        
        if not decoupling_detected:
            return []
        
        affected_signals = [
            line.split("'")[1]
            for line in evidence
            if "Feature '" in line
        ]
        
        return [DriftEvent(
            drift_type=DriftType.CAUSAL,
            platform=platform,
            niche=niche,
            onset_timestamp=current_snapshots[0].timestamp,
            detection_timestamp=current_timestamp,
            severity=severity,
            confidence=0.80,
            affected_signals=affected_signals,
            evidence=evidence
        )]
    
    def _detect_reward_drift(
        self,
        ref_window: ReferenceWindow,
        current_snapshots: list[TimeSeriesSnapshot],
        platform: str,
        niche: str | None,
        current_timestamp: float
    ) -> list[DriftEvent]:
        """Detects reward misalignment."""
        
        misalignment_detected, severity, evidence = self.reward_auditor.audit_alignment(
            ref_window,
            current_snapshots
        )
        
        if not misalignment_detected:
            return []
        
        return [DriftEvent(
            drift_type=DriftType.REWARD,
            platform=platform,
            niche=niche,
            onset_timestamp=current_snapshots[0].timestamp,
            detection_timestamp=current_timestamp,
            severity=severity,
            confidence=0.75,
            affected_signals=["viral_score", "engagement_envelope"],
            evidence=evidence
        )]


# ============================================================================
# Example Usage
# ============================================================================

def example_usage():
    """Demonstrates how to use the LongitudinalDriftMonitor."""
    
    # Initialize monitor
    monitor = LongitudinalDriftMonitor()
    
    # Simulate historical snapshots (from /evaluation/ outputs)
    historical_snapshots = [
        TimeSeriesSnapshot(
            timestamp=1704067200.0 + i * 86400,  # Daily snapshots
            platform="tiktok",
            niche="fitness",
            metric_distributions={
                "view_rate": np.random.normal(0.15, 0.03, 100),
                "like_rate": np.random.normal(0.05, 0.01, 100)
            },
            viral_scores=np.random.normal(0.65, 0.15, 100),
            engagement_envelope={
                "early_phase": 0.3,
                "mid_phase": 0.5,
                "late_phase": 0.2
            },
            suppression_adjusted_signals={"base_reach": 0.8},
            feature_attributions={
                "hook_strength": 0.35,
                "content_quality": 0.45,
                "timing_score": 0.20
            }
        )
        for i in range(60)  # 60 days of history
    ]
    
    # Simulate current snapshots with potential drift
    current_snapshots = [
        TimeSeriesSnapshot(
            timestamp=1709251200.0 + i * 86400,
            platform="tiktok",
            niche="fitness",
            metric_distributions={
                "view_rate": np.random.normal(0.10, 0.02, 100),  # Drift!
                "like_rate": np.random.normal(0.05, 0.01, 100)
            },
            viral_scores=np.random.normal(0.55, 0.12, 100),  # Drift!
            engagement_envelope={
                "early_phase": 0.4,
                "mid_phase": 0.4,
                "late_phase": 0.2
            },
            suppression_adjusted_signals={"base_reach": 0.75},
            feature_attributions={
                "hook_strength": 0.25,  # Weakening correlation
                "content_quality": 0.40,
                "timing_score": 0.15
            }
        )
        for i in range(14)  # 14 days current
    ]
    
    # Monitor for drift
    result = monitor.monitor_drift(
        historical_snapshots=historical_snapshots,
        current_snapshots=current_snapshots,
        platform="tiktok",
        niche="fitness",
        current_timestamp=1709856000.0
    )
    
    # Print results
    print(f"Drift Detected: {result.drift_detected}")
    print(f"System Stability Score: {result.system_stability_score:.3f}")
    print(f"Recommendation Flags: {result.recommendation_flags}")
    
    for event in result.drift_events:
        print(f"\n{event.drift_type.value.upper()} DRIFT:")
        print(f"  Severity: {event.severity:.3f}")
        print(f"  Confidence: {event.confidence:.3f}")
        print(f"  Affected Signals: {event.affected_signals}")
        print("  Evidence:")
        for evidence_line in event.evidence:
            print(f"    - {evidence_line}")


if __name__ == "__main__":
    example_usage()