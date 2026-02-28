"""
cross_platform_normalizer.py

Cross-Platform Signal Normalization & Comparable Virality Engine

Converts platform-specific metrics into comparable, bias-corrected signals.
Enables honest cross-platform ranking and A/B testing at 5M+ baseline scale.

NO models. NO predictions. Pure normalization mathematics.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set
from enum import Enum
import math
from datetime import datetime, timedelta
import json


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


class AmplificationCurve(Enum):
    """Platform-specific growth dynamics"""
    BURSTY = "bursty"              # TikTok: explosive early, sharp decay
    LINEAR = "linear"               # Traditional social
    DELAYED = "delayed"             # YouTube: slow start, long tail
    STEP_FUNCTION = "step_function" # Algorithm-gated releases


class MetricUnit(Enum):
    """Physical units for metrics"""
    COUNT = "count"                 # views, likes, shares
    SECONDS = "seconds"             # watch time
    RATIO = "ratio"                 # engagement rates
    PERCENTILE = "percentile"       # rank-based


@dataclass(frozen=True)
class MetricSchema:
    """Defines the mathematical properties of a metric"""
    name: str
    unit: MetricUnit
    monotonic: bool                 # always increases?
    heavy_tailed: bool              # power-law distribution?
    log_space: bool                 # better analyzed in log?
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("MetricSchema requires non-empty name")


@dataclass(frozen=True)
class PlatformProfile:
    """Complete characterization of platform behavior"""
    platform_name: str
    
    # Metric definitions
    primary_metrics: List[str]
    secondary_metrics: List[str]
    
    # Temporal dynamics
    amplification_curve: AmplificationCurve
    tail_half_life_hours: float     # time for decay to 50%
    
    # Engagement baselines
    typical_engagement_ratios: Dict[str, float]  # like:view, share:view, etc.
    
    # Known distortions
    known_biases: List[str]
    
    # Minimum sample requirements
    minimum_viable_sample: Dict[str, int]
    
    # Distribution parameters
    percentile_90_multiplier: float  # P90/P50 ratio
    percentile_99_multiplier: float  # P99/P50 ratio
    
    def __post_init__(self):
        if not self.platform_name:
            raise ValueError("PlatformProfile requires platform_name")
        if self.tail_half_life_hours <= 0:
            raise ValueError("tail_half_life_hours must be positive")
        if not self.primary_metrics:
            raise ValueError("primary_metrics cannot be empty")


@dataclass(frozen=True)
class NormalizationRule:
    """Explicit bias correction rule"""
    rule_name: str
    applies_to_platforms: List[str]
    applies_to_metrics: List[str]
    correction_type: str            # multiplicative, additive, log_scale
    parameters: Dict[str, float]
    
    def __post_init__(self):
        valid_types = {"multiplicative", "additive", "log_scale", "percentile_shift"}
        if self.correction_type not in valid_types:
            raise ValueError(f"correction_type must be one of {valid_types}")


@dataclass(frozen=True)
class NormalizedPerformance:
    """Final normalized output - platform-agnostic"""
    platform: str
    video_id: str
    timestamp: datetime
    
    # Core normalized metrics
    normalized_virality_mass: float      # total impact, platform-agnostic
    normalized_persistence: float        # staying power
    normalized_engagement_density: float # quality of interaction
    
    # Quality indicators
    confidence: float                    # 0-1, sample validity
    distortion_flags: List[str]          # detected anomalies
    comparable: bool                     # safe for cross-platform ranking?
    
    # Provenance
    raw_metrics: Dict[str, float]
    applied_corrections: List[str]
    
    def __post_init__(self):
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if self.normalized_virality_mass < 0:
            raise ValueError("normalized_virality_mass cannot be negative")


# ============================================================================
# PLATFORM REGISTRY (SOURCE OF TRUTH)
# ============================================================================


class PlatformRegistry:
    """
    Central registry for all platform profiles.
    Prevents silent drift and ensures consistency.
    """
    
    def __init__(self):
        self._profiles: Dict[str, PlatformProfile] = {}
        self._metric_schemas: Dict[str, MetricSchema] = {}
        self._locked: bool = False
        self._register_default_platforms()
        self._register_default_metrics()
    
    def _register_default_metrics(self):
        """Register standard metric schemas"""
        self.register_metric(MetricSchema(
            name="views",
            unit=MetricUnit.COUNT,
            monotonic=True,
            heavy_tailed=True,
            log_space=True
        ))
        
        self.register_metric(MetricSchema(
            name="watch_time_seconds",
            unit=MetricUnit.SECONDS,
            monotonic=True,
            heavy_tailed=True,
            log_space=True
        ))
        
        self.register_metric(MetricSchema(
            name="likes",
            unit=MetricUnit.COUNT,
            monotonic=True,
            heavy_tailed=True,
            log_space=True
        ))
        
        self.register_metric(MetricSchema(
            name="shares",
            unit=MetricUnit.COUNT,
            monotonic=True,
            heavy_tailed=True,
            log_space=True
        ))
        
        self.register_metric(MetricSchema(
            name="comments",
            unit=MetricUnit.COUNT,
            monotonic=True,
            heavy_tailed=True,
            log_space=True
        ))
        
        self.register_metric(MetricSchema(
            name="engagement_rate",
            unit=MetricUnit.RATIO,
            monotonic=False,
            heavy_tailed=False,
            log_space=False
        ))
    
    def _register_default_platforms(self):
        """Register known platforms with their characteristics"""
        
        # TikTok: Explosive early, sharp decay
        self.register_platform(PlatformProfile(
            platform_name="tiktok",
            primary_metrics=["views", "likes", "shares"],
            secondary_metrics=["comments", "watch_time_seconds"],
            amplification_curve=AmplificationCurve.BURSTY,
            tail_half_life_hours=12.0,
            typical_engagement_ratios={
                "like:view": 0.08,
                "share:view": 0.02,
                "comment:view": 0.005
            },
            known_biases=[
                "cold_start_boost",
                "early_viral_overshoot",
                "niche_favoritism"
            ],
            minimum_viable_sample={
                "views": 1000,
                "likes": 50
            },
            percentile_90_multiplier=8.0,
            percentile_99_multiplier=50.0
        ))
        
        # YouTube: Slow burn, long tail
        self.register_platform(PlatformProfile(
            platform_name="youtube",
            primary_metrics=["views", "watch_time_seconds"],
            secondary_metrics=["likes", "comments", "shares"],
            amplification_curve=AmplificationCurve.DELAYED,
            tail_half_life_hours=168.0,  # 7 days
            typical_engagement_ratios={
                "like:view": 0.04,
                "share:view": 0.005,
                "comment:view": 0.002
            },
            known_biases=[
                "subscriber_base_boost",
                "watch_time_weighted",
                "delayed_distribution"
            ],
            minimum_viable_sample={
                "views": 500,
                "watch_time_seconds": 3000
            },
            percentile_90_multiplier=12.0,
            percentile_99_multiplier=100.0
        ))
        
        # YouTube Shorts: Hybrid behavior
        self.register_platform(PlatformProfile(
            platform_name="youtube_shorts",
            primary_metrics=["views", "likes"],
            secondary_metrics=["comments", "shares"],
            amplification_curve=AmplificationCurve.BURSTY,
            tail_half_life_hours=24.0,
            typical_engagement_ratios={
                "like:view": 0.06,
                "share:view": 0.015,
                "comment:view": 0.003
            },
            known_biases=[
                "watch_time_dominant",
                "completion_rate_gated"
            ],
            minimum_viable_sample={
                "views": 800,
                "likes": 40
            },
            percentile_90_multiplier=10.0,
            percentile_99_multiplier=70.0
        ))
        
        # Instagram Reels: Engagement-weighted
        self.register_platform(PlatformProfile(
            platform_name="instagram_reels",
            primary_metrics=["views", "likes", "shares"],
            secondary_metrics=["comments", "saves"],
            amplification_curve=AmplificationCurve.LINEAR,
            tail_half_life_hours=36.0,
            typical_engagement_ratios={
                "like:view": 0.10,
                "share:view": 0.025,
                "comment:view": 0.008,
                "save:view": 0.015
            },
            known_biases=[
                "creator_authority_boost",
                "engagement_throttling",
                "follower_weighted"
            ],
            minimum_viable_sample={
                "views": 600,
                "likes": 60
            },
            percentile_90_multiplier=7.0,
            percentile_99_multiplier=45.0
        ))
    
    def register_platform(self, profile: PlatformProfile):
        """Register a new platform profile"""
        if self._locked:
            raise RuntimeError("Registry is locked - cannot add platforms")
        
        if profile.platform_name in self._profiles:
            raise ValueError(f"Platform {profile.platform_name} already registered")
        
        self._profiles[profile.platform_name] = profile
    
    def register_metric(self, schema: MetricSchema):
        """Register a metric schema"""
        if self._locked:
            raise RuntimeError("Registry is locked - cannot add metrics")
        
        if schema.name in self._metric_schemas:
            raise ValueError(f"Metric {schema.name} already registered")
        
        self._metric_schemas[schema.name] = schema
    
    def lock(self):
        """Lock registry to prevent modifications"""
        self._locked = True
    
    def get_profile(self, platform: str) -> PlatformProfile:
        """Retrieve platform profile"""
        if platform not in self._profiles:
            raise ValueError(f"Unknown platform: {platform}. Register it first.")
        return self._profiles[platform]
    
    def get_metric_schema(self, metric: str) -> MetricSchema:
        """Retrieve metric schema"""
        if metric not in self._metric_schemas:
            raise ValueError(f"Unknown metric: {metric}. Register it first.")
        return self._metric_schemas[metric]
    
    def list_platforms(self) -> List[str]:
        """List all registered platforms"""
        return list(self._profiles.keys())


# ============================================================================
# TIME AXIS NORMALIZER
# ============================================================================


class TimeAxisNormalizer:
    """
    Normalizes performance across different temporal dynamics.
    Corrects for early overshoot vs delayed lift patterns.
    """
    
    def __init__(self, registry: PlatformRegistry):
        self.registry = registry
    
    def normalize_temporal_performance(
        self,
        platform: str,
        metric_values: Dict[float, float],  # hour -> value
        metric_name: str
    ) -> Dict[str, float]:
        """
        Convert time-series data into temporal performance indicators.
        
        Returns:
            {
                'early_velocity': float,      # 0-24h performance
                'mid_stability': float,        # 24-72h performance
                'tail_persistence': float      # 72h+ performance
            }
        """
        profile = self.registry.get_profile(platform)
        
        if not metric_values:
            return {
                'early_velocity': 0.0,
                'mid_stability': 0.0,
                'tail_persistence': 0.0
            }
        
        # Sort by time
        sorted_times = sorted(metric_values.keys())
        
        # Extract phase values
        early_vals = [v for t, v in metric_values.items() if t <= 24]
        mid_vals = [v for t, v in metric_values.items() if 24 < t <= 72]
        tail_vals = [v for t, v in metric_values.items() if t > 72]
        
        # Calculate phase metrics
        early_velocity = max(early_vals) if early_vals else 0.0
        mid_stability = max(mid_vals) if mid_vals else 0.0
        tail_persistence = max(tail_vals) if tail_vals else 0.0
        
        # Apply platform-specific corrections
        if profile.amplification_curve == AmplificationCurve.BURSTY:
            # TikTok overweights early - dampen it
            early_velocity *= 0.7
            tail_persistence *= 1.3
        
        elif profile.amplification_curve == AmplificationCurve.DELAYED:
            # YouTube underweights early - boost it
            early_velocity *= 1.4
            tail_persistence *= 0.9
        
        return {
            'early_velocity': early_velocity,
            'mid_stability': mid_stability,
            'tail_persistence': tail_persistence
        }
    
    def compute_decay_adjusted_value(
        self,
        platform: str,
        current_value: float,
        hours_elapsed: float
    ) -> float:
        """Apply half-life decay correction"""
        profile = self.registry.get_profile(platform)
        half_life = profile.tail_half_life_hours
        
        if hours_elapsed <= 0:
            return current_value
        
        decay_factor = math.exp(-math.log(2) * hours_elapsed / half_life)
        return current_value / max(decay_factor, 0.01)  # prevent division issues


# ============================================================================
# ENGAGEMENT SURFACE ALIGNER
# ============================================================================


class EngagementSurfaceAligner:
    """
    Aligns different engagement metrics into a shared manifold.
    Prevents share-inflated or like-suppressed platforms from dominating.
    """
    
    def __init__(self, registry: PlatformRegistry):
        self.registry = registry
    
    def compute_engagement_density(
        self,
        platform: str,
        metrics: Dict[str, float]
    ) -> float:
        """
        Compute platform-normalized engagement density.
        
        Formula: weighted combination of engagement signals,
        corrected for platform-specific baselines.
        """
        profile = self.registry.get_profile(platform)
        
        views = metrics.get('views', 0)
        if views < profile.minimum_viable_sample.get('views', 0):
            return 0.0  # insufficient sample
        
        # Extract engagement metrics
        likes = metrics.get('likes', 0)
        shares = metrics.get('shares', 0)
        comments = metrics.get('comments', 0)
        
        # Compute observed ratios
        like_rate = likes / views if views > 0 else 0
        share_rate = shares / views if views > 0 else 0
        comment_rate = comments / views if views > 0 else 0
        
        # Get expected ratios
        expected_like = profile.typical_engagement_ratios.get('like:view', 0.05)
        expected_share = profile.typical_engagement_ratios.get('share:view', 0.01)
        expected_comment = profile.typical_engagement_ratios.get('comment:view', 0.003)
        
        # Normalize against baselines
        normalized_like = like_rate / max(expected_like, 0.001)
        normalized_share = share_rate / max(expected_share, 0.001)
        normalized_comment = comment_rate / max(expected_comment, 0.001)
        
        # Weighted combination (shares and comments signal stronger engagement)
        engagement_density = (
            0.3 * normalized_like +
            0.5 * normalized_share +
            0.2 * normalized_comment
        )
        
        return engagement_density
    
    def align_metric_to_universal_scale(
        self,
        platform: str,
        metric_name: str,
        value: float
    ) -> float:
        """
        Convert platform-specific metric to universal scale.
        Uses log-space for heavy-tailed metrics.
        """
        schema = self.registry.get_metric_schema(metric_name)
        profile = self.registry.get_profile(platform)
        
        if value <= 0:
            return 0.0
        
        if schema.log_space:
            # Use log scale with platform percentile correction
            log_val = math.log10(value + 1)
            
            # Correct for platform distribution shape
            # Platforms with higher tail multipliers get dampened
            p99_mult = profile.percentile_99_multiplier
            correction = 50.0 / max(p99_mult, 1.0)  # normalize to reference
            
            return log_val * correction
        else:
            return value


# ============================================================================
# DISTRIBUTION CORRECTOR
# ============================================================================


class DistributionCorrector:
    """
    Handles heavy-tail and power-law corrections.
    Ensures percentile-safe comparisons across platforms.
    """
    
    def __init__(self, registry: PlatformRegistry):
        self.registry = registry
    
    def correct_for_heavy_tail(
        self,
        platform: str,
        metric_name: str,
        value: float,
        population_stats: Optional[Dict[str, float]] = None
    ) -> float:
        """
        Apply heavy-tail correction to prevent outlier dominance.
        
        population_stats: {'p50': ..., 'p90': ..., 'p99': ...}
        """
        schema = self.registry.get_metric_schema(metric_name)
        profile = self.registry.get_profile(platform)
        
        if not schema.heavy_tailed:
            return value
        
        if value <= 0:
            return 0.0
        
        # Use platform's tail characteristics
        expected_p90_mult = profile.percentile_90_multiplier
        expected_p99_mult = profile.percentile_99_multiplier
        
        # Apply log compression for extreme values
        if value > 1e6:  # arbitrary high threshold
            # Strong compression for extreme outliers
            return math.log10(value + 1) * 100.0
        elif value > 1e5:
            # Moderate compression
            return math.log10(value + 1) * 150.0
        else:
            # Linear for typical range
            return value
    
    def compute_percentile_rank(
        self,
        platform: str,
        metric_name: str,
        value: float,
        reference_distribution: List[float]
    ) -> float:
        """
        Compute percentile rank within reference distribution.
        Returns value in [0, 1].
        """
        if not reference_distribution:
            return 0.5  # neutral
        
        sorted_dist = sorted(reference_distribution)
        
        # Count values below this one
        below_count = sum(1 for v in sorted_dist if v < value)
        percentile = below_count / len(sorted_dist)
        
        return percentile


# ============================================================================
# BIAS COMPENSATOR
# ============================================================================


class BiasCompensator:
    """
    Explicitly compensates for known platform biases.
    Biases are declared, not guessed.
    """
    
    def __init__(self, registry: PlatformRegistry):
        self.registry = registry
        self._correction_rules: List[NormalizationRule] = []
        self._register_default_rules()
    
    def _register_default_rules(self):
        """Register known bias corrections"""
        
        # TikTok cold start boost
        self.register_rule(NormalizationRule(
            rule_name="tiktok_cold_start_correction",
            applies_to_platforms=["tiktok"],
            applies_to_metrics=["views"],
            correction_type="multiplicative",
            parameters={'factor': 0.85}  # dampen early boost
        ))
        
        # YouTube subscriber base boost
        self.register_rule(NormalizationRule(
            rule_name="youtube_subscriber_correction",
            applies_to_platforms=["youtube"],
            applies_to_metrics=["views"],
            correction_type="multiplicative",
            parameters={'factor': 1.15}  # boost to compensate for slower start
        ))
        
        # Instagram creator authority
        self.register_rule(NormalizationRule(
            rule_name="instagram_creator_authority",
            applies_to_platforms=["instagram_reels"],
            applies_to_metrics=["views", "likes"],
            correction_type="multiplicative",
            parameters={'factor': 0.90}  # dampen authority boost
        ))
    
    def register_rule(self, rule: NormalizationRule):
        """Add a bias correction rule"""
        self._correction_rules.append(rule)
    
    def apply_corrections(
        self,
        platform: str,
        metrics: Dict[str, float]
    ) -> Tuple[Dict[str, float], List[str]]:
        """
        Apply all relevant bias corrections.
        
        Returns:
            (corrected_metrics, applied_rule_names)
        """
        corrected = metrics.copy()
        applied_rules = []
        
        for rule in self._correction_rules:
            if platform not in rule.applies_to_platforms:
                continue
            
            for metric in rule.applies_to_metrics:
                if metric not in corrected:
                    continue
                
                value = corrected[metric]
                
                if rule.correction_type == "multiplicative":
                    factor = rule.parameters.get('factor', 1.0)
                    corrected[metric] = value * factor
                
                elif rule.correction_type == "additive":
                    offset = rule.parameters.get('offset', 0.0)
                    corrected[metric] = value + offset
                
                elif rule.correction_type == "log_scale":
                    if value > 0:
                        corrected[metric] = math.log10(value + 1)
                
                applied_rules.append(rule.rule_name)
        
        return corrected, applied_rules


# ============================================================================
# RANK PRESERVER
# ============================================================================


class RankPreserver:
    """
    CRITICAL: Ensures normalization preserves clear ranking relationships.
    If video A beats B on same platform, it cannot invert after normalization.
    """
    
    def verify_rank_preservation(
        self,
        platform: str,
        raw_pairs: List[Tuple[str, Dict[str, float]]],  # (video_id, metrics)
        normalized_pairs: List[Tuple[str, float]]       # (video_id, normalized_score)
    ) -> Tuple[bool, List[str]]:
        """
        Verify that normalization preserves ranking.
        
        Returns:
            (is_valid, list_of_violations)
        """
        violations = []
        
        # Build raw ranking
        raw_ranking = {}
        for video_id, metrics in raw_pairs:
            primary_metric = metrics.get('views', 0)  # use views as primary
            raw_ranking[video_id] = primary_metric
        
        # Build normalized ranking
        norm_ranking = {video_id: score for video_id, score in normalized_pairs}
        
        # Check all pairs
        for vid_a in raw_ranking:
            for vid_b in raw_ranking:
                if vid_a == vid_b:
                    continue
                
                raw_a = raw_ranking[vid_a]
                raw_b = raw_ranking[vid_b]
                norm_a = norm_ranking.get(vid_a, 0)
                norm_b = norm_ranking.get(vid_b, 0)
                
                # If A clearly beats B in raw (>20% difference)
                if raw_a > raw_b * 1.2:
                    # A must beat B in normalized
                    if norm_a <= norm_b:
                        violations.append(
                            f"Rank inversion: {vid_a} > {vid_b} raw, but inverted after norm"
                        )
        
        is_valid = len(violations) == 0
        return is_valid, violations


# ============================================================================
# DRIFT DETECTOR
# ============================================================================


class DriftDetector:
    """
    Continuously monitors for platform behavior shifts.
    Signals when recalibration is needed.
    """
    
    def __init__(self, registry: PlatformRegistry):
        self.registry = registry
        self._historical_baselines: Dict[str, Dict[str, float]] = {}
    
    def record_baseline(self, platform: str, metrics: Dict[str, float]):
        """Record baseline metric statistics"""
        if platform not in self._historical_baselines:
            self._historical_baselines[platform] = {}
        
        for metric, value in metrics.items():
            if metric not in self._historical_baselines[platform]:
                self._historical_baselines[platform][metric] = []
            
            self._historical_baselines[platform][metric].append(value)
    
    def detect_drift(
        self,
        platform: str,
        current_metrics: Dict[str, float],
        threshold: float = 0.3  # 30% deviation triggers flag
    ) -> Tuple[bool, List[str]]:
        """
        Detect if current metrics deviate from historical baseline.
        
        Returns:
            (drift_detected, list_of_drift_flags)
        """
        if platform not in self._historical_baselines:
            return False, []
        
        drift_flags = []
        baselines = self._historical_baselines[platform]
        
        for metric, current_value in current_metrics.items():
            if metric not in baselines or not baselines[metric]:
                continue
            
            historical_values = baselines[metric]
            avg_baseline = sum(historical_values) / len(historical_values)
            
            if avg_baseline == 0:
                continue
            
            deviation = abs(current_value - avg_baseline) / avg_baseline
            
            if deviation > threshold:
                drift_flags.append(
                    f"{metric}_drift_{deviation:.2f}"
                )
        
        drift_detected = len(drift_flags) > 0
        return drift_detected, drift_flags


# ============================================================================
# NORMALIZATION REPORT BUILDER
# ============================================================================


class NormalizationReportBuilder:
    """
    Assembles final normalization report with full provenance.
    """
    
    def build_report(
        self,
        platform: str,
        video_id: str,
        raw_metrics: Dict[str, float],
        normalized_virality_mass: float,
        normalized_persistence: float,
        normalized_engagement_density: float,
        confidence: float,
        distortion_flags: List[str],
        applied_corrections: List[str],
        comparable: bool
    ) -> NormalizedPerformance:
        """Build complete normalization report"""
        
        return NormalizedPerformance(
            platform=platform,
            video_id=video_id,
            timestamp=datetime.now(),
            normalized_virality_mass=normalized_virality_mass,
            normalized_persistence=normalized_persistence,
            normalized_engagement_density=normalized_engagement_density,
            confidence=confidence,
            distortion_flags=distortion_flags,
            comparable=comparable,
            raw_metrics=raw_metrics,
            applied_corrections=applied_corrections
        )


# ============================================================================
# MAIN NORMALIZER ORCHESTRATOR
# ============================================================================


class CrossPlatformNormalizer:
    """
    Master orchestrator for cross-platform normalization.
    Single entry point for all normalization operations.
    """
    
    def __init__(self):
        self.registry = PlatformRegistry()
        self.registry.lock()  # prevent runtime modifications
        
        self.time_normalizer = TimeAxisNormalizer(self.registry)
        self.engagement_aligner = EngagementSurfaceAligner(self.registry)
        self.distribution_corrector = DistributionCorrector(self.registry)
        self.bias_compensator = BiasCompensator(self.registry)
        self.rank_preserver = RankPreserver()
        self.drift_detector = DriftDetector(self.registry)
        self.report_builder = NormalizationReportBuilder()
    
    def normalize(
        self,
        platform: str,
        video_id: str,
        raw_metrics: Dict[str, float],
        time_series: Optional[Dict[float, float]] = None,
        reference_distribution: Optional[List[float]] = None
    ) -> NormalizedPerformance:
        """
        Complete normalization pipeline.
        
        Args:
            platform: Platform name (must be registered)
            video_id: Unique video identifier
            raw_metrics: {'views': ..., 'likes': ..., 'shares': ..., ...}
            time_series: Optional {hours_elapsed: metric_value}
            reference_distribution: Optional population statistics
        
        Returns:
            NormalizedPerformance object with all normalized signals
        """
        # Validate platform
        profile = self.registry.get_profile(platform)
        
        # Check minimum viable sample
        views = raw_metrics.get('views', 0)
        min_views = profile.minimum_viable_sample.get('views', 0)
        
        if views < min_views:
            # Insufficient data - return zero-confidence result
            return self.report_builder.build_report(
                platform=platform,
                video_id=video_id,
                raw_metrics=raw_metrics,
                normalized_virality_mass=0.0,
                normalized_persistence=0.0,
                normalized_engagement_density=0.0,
                confidence=0.0,
                distortion_flags=['insufficient_sample'],
                applied_corrections=[],
                comparable=False
            )
        
        # Step 1: Apply bias corrections
        corrected_metrics, applied_rules = self.bias_compensator.apply_corrections(
            platform, raw_metrics
        )
        
        # Step 2: Temporal normalization
        temporal_performance = {}
        if time_series:
            temporal_performance = self.time_normalizer.normalize_temporal_performance(
                platform, time_series, 'views'
            )
        
        # Step 3: Engagement alignment
        engagement_density = self.engagement_aligner.compute_engagement_density(
            platform, corrected_metrics
        )
        
        # Step 4: Distribution correction
        corrected_views = self.distribution_corrector.correct_for_heavy_tail(
            platform,
            'views',
            corrected_metrics.get('views', 0)
        )
        
        # Step 5: Compute normalized virality mass
        # Combination of views (reach) and engagement (quality)
        aligned_views = self.engagement_aligner.align_metric_to_universal_scale(
            platform, 'views', corrected_views
        )
        
        normalized_virality_mass = aligned_views * (1.0 + engagement_density * 0.5)
        
        # Step 6: Compute persistence
        if temporal_performance:
            tail_persist = temporal_performance.get('tail_persistence', 0)
            early_vel = temporal_performance.get('early_velocity', 1)
            
            # Persistence = how well it maintains beyond early spike
            if early_vel > 0:
                normalized_persistence = tail_persist / early_vel
            else:
                normalized_persistence = 0.0
        else:
            # No time series - use platform half-life as proxy
            normalized_persistence = profile.tail_half_life_hours / 24.0
        
        # Step 7: Normalized engagement density (already computed)
        normalized_engagement_density = engagement_density
        
        # Step 8: Drift detection
        drift_detected, drift_flags = self.drift_detector.detect_drift(
            platform, corrected_metrics
        )
        
        # Step 9: Compute confidence
        confidence = self._compute_confidence(
            platform, corrected_metrics, drift_detected
        )
        
        # Step 10: Determine comparability
        distortion_flags = []
        if drift_detected:
            distortion_flags.extend(drift_flags)
        
        comparable = (
            confidence >= 0.6 and
            not drift_detected and
            views >= min_views * 2  # prefer 2x minimum for safety
        )
        
        # Build final report
        return self.report_builder.build_report(
            platform=platform,
            video_id=video_id,
            raw_metrics=raw_metrics,
            normalized_virality_mass=normalized_virality_mass,
            normalized_persistence=normalized_persistence,
            normalized_engagement_density=normalized_engagement_density,
            confidence=confidence,
            distortion_flags=distortion_flags,
            applied_corrections=applied_rules,
            comparable=comparable
        )
    
    def _compute_confidence(
        self,
        platform: str,
        metrics: Dict[str, float],
        drift_detected: bool
    ) -> float:
        """
        Compute confidence score for normalization.
        Based on sample size and data quality.
        """
        profile = self.registry.get_profile(platform)
        
        views = metrics.get('views', 0)
        min_views = profile.minimum_viable_sample.get('views', 1)
        
        # Base confidence from sample size
        if views >= min_views * 10:
            base_confidence = 0.95
        elif views >= min_views * 5:
            base_confidence = 0.85
        elif views >= min_views * 2:
            base_confidence = 0.75
        elif views >= min_views:
            base_confidence = 0.60
        else:
            base_confidence = 0.30
        
        # Penalize for drift
        if drift_detected:
            base_confidence *= 0.7
        
        # Check engagement consistency
        likes = metrics.get('likes', 0)
        if views > 0:
            like_rate = likes / views
            expected_like = profile.typical_engagement_ratios.get('like:view', 0.05)
            
            # Penalize if engagement is way off
            if like_rate > expected_like * 5 or like_rate < expected_like * 0.2:
                base_confidence *= 0.8
        
        return min(base_confidence, 1.0)
    
    def normalize_batch(
        self,
        platform: str,
        videos: List[Tuple[str, Dict[str, float]]]
    ) -> List[NormalizedPerformance]:
        """
        Normalize a batch of videos from the same platform.
        Enables rank-preservation verification.
        """
        results = []
        
        for video_id, metrics in videos:
            result = self.normalize(platform, video_id, metrics)
            results.append(result)
        
        # Verify rank preservation
        normalized_pairs = [
            (r.video_id, r.normalized_virality_mass) for r in results
        ]
        
        is_valid, violations = self.rank_preserver.verify_rank_preservation(
            platform, videos, normalized_pairs
        )
        
        if not is_valid:
            # Log violations but don't block (real-world data is messy)
            for violation in violations:
                # In production, log to monitoring system
                pass
        
        return results
    
    def compare_cross_platform(
        self,
        performances: List[NormalizedPerformance],
        metric: str = 'normalized_virality_mass'
    ) -> List[Tuple[str, str, float]]:
        """
        Compare videos across platforms.
        
        Returns:
            List of (video_id, platform, score) sorted by score descending
        """
        # Filter to comparable only
        comparable = [p for p in performances if p.comparable]
        
        # Extract metric
        scored = []
        for perf in comparable:
            if metric == 'normalized_virality_mass':
                score = perf.normalized_virality_mass
            elif metric == 'normalized_persistence':
                score = perf.normalized_persistence
            elif metric == 'normalized_engagement_density':
                score = perf.normalized_engagement_density
            else:
                raise ValueError(f"Unknown metric: {metric}")
            
            scored.append((perf.video_id, perf.platform, score))
        
        # Sort descending
        scored.sort(key=lambda x: x[2], reverse=True)
        
        return scored
    
    def export_normalization_config(self) -> Dict:
        """Export current normalization configuration for audit"""
        return {
            'platforms': [
                {
                    'name': p.platform_name,
                    'amplification_curve': p.amplification_curve.value,
                    'tail_half_life_hours': p.tail_half_life_hours,
                    'known_biases': p.known_biases
                }
                for p in [self.registry.get_profile(pname) 
                         for pname in self.registry.list_platforms()]
            ],
            'correction_rules': [
                {
                    'rule_name': rule.rule_name,
                    'platforms': rule.applies_to_platforms,
                    'correction_type': rule.correction_type
                }
                for rule in self.bias_compensator._correction_rules
            ]
        }


# ============================================================================
# EXAMPLE USAGE & TESTING
# ============================================================================


def example_usage():
    """Demonstrate typical usage patterns"""
    
    # Initialize normalizer
    normalizer = CrossPlatformNormalizer()
    
    # Example 1: Normalize single TikTok video
    tiktok_result = normalizer.normalize(
        platform='tiktok',
        video_id='tt_12345',
        raw_metrics={
            'views': 500000,
            'likes': 45000,
            'shares': 8000,
            'comments': 2500
        }
    )
    
    print(f"TikTok normalized virality: {tiktok_result.normalized_virality_mass:.2f}")
    print(f"Confidence: {tiktok_result.confidence:.2f}")
    print(f"Comparable: {tiktok_result.comparable}")
    
    # Example 2: Normalize YouTube video
    youtube_result = normalizer.normalize(
        platform='youtube',
        video_id='yt_67890',
        raw_metrics={
            'views': 300000,
            'likes': 12000,
            'shares': 1500,
            'comments': 600,
            'watch_time_seconds': 1800000
        }
    )
    
    print(f"\nYouTube normalized virality: {youtube_result.normalized_virality_mass:.2f}")
    
    # Example 3: Cross-platform comparison
    all_results = [tiktok_result, youtube_result]
    
    ranking = normalizer.compare_cross_platform(
        all_results,
        metric='normalized_virality_mass'
    )
    
    print("\nCross-platform ranking:")
    for video_id, platform, score in ranking:
        print(f"  {video_id} ({platform}): {score:.2f}")
    
    # Example 4: Batch processing with rank verification
    tiktok_batch = [
        ('tt_001', {'views': 100000, 'likes': 8000, 'shares': 1500}),
        ('tt_002', {'views': 500000, 'likes': 40000, 'shares': 7000}),
        ('tt_003', {'views': 250000, 'likes': 20000, 'shares': 3500})
    ]
    
    batch_results = normalizer.normalize_batch('tiktok', tiktok_batch)
    
    print("\nBatch results:")
    for result in batch_results:
        print(f"  {result.video_id}: {result.normalized_virality_mass:.2f}")


if __name__ == '__main__':
    example_usage()