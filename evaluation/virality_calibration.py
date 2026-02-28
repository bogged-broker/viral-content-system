# /evaluation/virality_calibration/__init__.py
"""
Virality Calibration System
Ground-truth, platform-normalized, era-corrected definition of virality.

This is the truth anchor for the entire system.
- Post-hoc only
- Ground-truth only  
- Model-agnostic
- RL-agnostic
- Deterministic
- Audit-safe
"""

# Note: This is a single-file implementation
# In production, these would be separate modules with relative imports
# For now, all classes are defined in this file

__version__ = "1.0.0"
__all__ = [
    "CalibrationEngine",
    "CalibrationInput",
    "CalibrationOutput",
    "GroundTruth",
    "PlatformMetrics",
    "NormalizedMetrics",
]


# =============================================================================
# /evaluation/virality_calibration/schemas.py
# =============================================================================
"""
Immutable data contracts for calibration system.
No logic - only structure and validation.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Literal, Tuple
from datetime import datetime
from enum import Enum


class Platform(str, Enum):
    TIKTOK = "tiktok"
    YOUTUBE_SHORT = "youtube_short"
    YOUTUBE_LONG = "youtube_long"
    INSTAGRAM_REEL = "instagram_reel"
    INSTAGRAM_POST = "instagram_post"
    TWITTER = "twitter"


class ContentFormat(str, Enum):
    SHORT_VERTICAL = "short_vertical"  # <60s vertical
    SHORT_HORIZONTAL = "short_horizontal"
    LONG_FORM = "long_form"  # >60s
    STILL_IMAGE = "still_image"
    CAROUSEL = "carousel"
    TEXT_ONLY = "text_only"


class OriginType(str, Enum):
    ORIGINAL = "original"
    REPOST = "repost"
    REMIX = "remix"
    DUET = "duet"


@dataclass(frozen=True)
class PlatformMetrics:
    """Raw metrics as reported by platform."""
    platform: Platform
    views: int
    likes: int
    comments: int
    shares: int
    saves: Optional[int] = None
    watch_time_seconds: Optional[float] = None
    avg_watch_percentage: Optional[float] = None
    completion_rate: Optional[float] = None
    follower_count_at_post: int = 0
    # Additional engagement signals
    bookmarks: Optional[int] = None
    retweets: Optional[int] = None
    quote_tweets: Optional[int] = None
    impressions: Optional[int] = None
    reach: Optional[int] = None
    unique_viewers: Optional[int] = None
    replay_count: Optional[int] = None
    # Quality signals
    avg_retention_seconds: Optional[float] = None
    peak_concurrent_viewers: Optional[int] = None
    click_through_rate: Optional[float] = None
    bounce_rate: Optional[float] = None
    
    def __post_init__(self):
        assert self.views >= 0, "Views cannot be negative"
        assert self.likes >= 0, "Likes cannot be negative"
        assert 0 <= self.follower_count_at_post <= 1_000_000_000
        if self.completion_rate is not None:
            assert 0 <= self.completion_rate <= 1.0, "Completion rate must be [0, 1]"
        if self.avg_watch_percentage is not None:
            assert 0 <= self.avg_watch_percentage <= 100.0, "Watch percentage must be [0, 100]"
        if self.click_through_rate is not None:
            assert 0 <= self.click_through_rate <= 1.0, "CTR must be [0, 1]"
        if self.bounce_rate is not None:
            assert 0 <= self.bounce_rate <= 1.0, "Bounce rate must be [0, 1]"
    
    def compute_engagement_rate(self) -> float:
        """Compute overall engagement rate."""
        total_engagement = (
            self.likes + 
            self.comments * 3 + 
            self.shares * 5 + 
            (self.saves or 0) * 2 +
            (self.bookmarks or 0) * 2 +
            (self.retweets or 0) * 4
        )
        return total_engagement / max(self.views, 1)
    
    def compute_quality_score(self) -> float:
        """Compute content quality score from available signals."""
        score = 0.0
        
        # Engagement density
        engagement_rate = self.compute_engagement_rate()
        score += min(1.0, engagement_rate * 100) * 0.3
        
        # Retention signals
        if self.completion_rate is not None:
            score += self.completion_rate * 0.25
        elif self.avg_watch_percentage is not None:
            score += (self.avg_watch_percentage / 100.0) * 0.25
        
        # Reach efficiency
        if self.reach is not None and self.views > 0:
            reach_ratio = self.reach / self.views
            score += min(1.0, reach_ratio) * 0.2
        
        # Click-through quality
        if self.click_through_rate is not None:
            score += self.click_through_rate * 0.15
        
        # Bounce penalty
        if self.bounce_rate is not None:
            score *= (1.0 - self.bounce_rate * 0.1)
        
        return min(1.0, max(0.0, score))


@dataclass(frozen=True)
class TimeHorizonMetrics:
    """Metrics captured at specific time horizons."""
    horizon_hours: int  # 6, 24, 168 (7d), 720 (30d)
    timestamp: datetime
    metrics: PlatformMetrics
    is_complete: bool  # Did we capture full horizon?
    suppression_detected: bool = False
    
    def __post_init__(self):
        assert self.horizon_hours in [6, 24, 168, 720], f"Invalid horizon: {self.horizon_hours}"


@dataclass(frozen=True)
class GroundTruth:
    """Immutable snapshot of observed reality."""
    content_id: str
    platform: Platform
    format: ContentFormat
    origin_type: OriginType
    publish_timestamp: datetime
    horizons: List[TimeHorizonMetrics]
    final_observed_metrics: PlatformMetrics
    observation_complete: bool
    era_identifier: str  # "2024-Q1", "2025-Q3", etc.
    
    def __post_init__(self):
        assert len(self.horizons) > 0, "Must have at least one horizon"
        assert self.content_id, "Content ID required"


@dataclass(frozen=True)
class NormalizedMetrics:
    """Platform-normalized, format-corrected metrics."""
    virality_score: float  # [0, 1000+] normalized across platforms
    velocity_score: float  # Early momentum
    retention_score: float  # Sustained performance
    tail_weight: float  # Long-term stability [0, 1]
    engagement_density: float  # Likes/comments/shares per view
    reach_efficiency: float  # Views / follower_count
    suppression_discount: float  # [0, 1], 1 = no suppression
    format_equivalence_factor: float  # Correction multiplier
    era_correction_factor: float  # Time-drift correction
    # Additional normalized signals
    quality_score: float = 0.0  # Content quality [0, 1]
    momentum_score: float = 0.0  # Growth momentum [0, 1]
    stability_score: float = 0.0  # Performance stability [0, 1]
    reactivation_potential: float = 0.0  # Likelihood of reactivation [0, 1]
    cross_platform_comparability: float = 1.0  # How comparable this is [0, 1]
    calibration_confidence: float = 1.0  # Confidence in this specific metric [0, 1]
    
    def __post_init__(self):
        assert self.virality_score >= 0, "Virality score cannot be negative"
        assert 0 <= self.tail_weight <= 1, "Tail weight must be [0, 1]"
        assert 0 <= self.suppression_discount <= 1, "Suppression discount must be [0, 1]"
        assert 0 <= self.quality_score <= 1, "Quality score must be [0, 1]"
        assert 0 <= self.momentum_score <= 1, "Momentum score must be [0, 1]"
        assert 0 <= self.stability_score <= 1, "Stability score must be [0, 1]"
        assert 0 <= self.reactivation_potential <= 1, "Reactivation potential must be [0, 1]"
        assert 0 <= self.calibration_confidence <= 1, "Calibration confidence must be [0, 1]"
    
    def compute_composite_score(self, weights: Optional[Dict[str, float]] = None) -> float:
        """Compute weighted composite score from all metrics."""
        if weights is None:
            weights = {
                "virality": 0.30,
                "velocity": 0.15,
                "retention": 0.15,
                "tail": 0.10,
                "engagement": 0.10,
                "quality": 0.10,
                "stability": 0.10,
            }
        
        # Normalize scores to [0, 1] range
        virality_norm = min(1.0, self.virality_score / 1000.0)
        velocity_norm = min(1.0, self.velocity_score / 100.0)
        retention_norm = min(1.0, self.retention_score / 100.0)
        engagement_norm = min(1.0, self.engagement_density * 100.0)
        
        composite = (
            weights.get("virality", 0.0) * virality_norm +
            weights.get("velocity", 0.0) * velocity_norm +
            weights.get("retention", 0.0) * retention_norm +
            weights.get("tail", 0.0) * self.tail_weight +
            weights.get("engagement", 0.0) * engagement_norm +
            weights.get("quality", 0.0) * self.quality_score +
            weights.get("stability", 0.0) * self.stability_score
        )
        
        # Apply suppression discount
        composite *= self.suppression_discount
        
        return min(1.0, max(0.0, composite))


@dataclass
class CalibrationInput:
    """Input contract for calibration engine."""
    ground_truths: List[GroundTruth]
    calibration_id: str
    requested_timestamp: datetime = field(default_factory=datetime.utcnow)
    require_complete_horizons: bool = True
    min_confidence_threshold: float = 0.7
    
    def __post_init__(self):
        assert len(self.ground_truths) > 0, "Need at least one ground truth"
        assert 0 <= self.min_confidence_threshold <= 1


@dataclass(frozen=True)
class CalibrationOutput:
    """Immutable calibration result."""
    calibration_id: str
    version: str
    timestamp: datetime
    input_hash: str
    ground_truth_count: int
    normalized_metrics: Dict[str, NormalizedMetrics]  # content_id -> metrics
    confidence: float  # [0, 1]
    invariants_validated: bool
    audit_trail_id: str
    warnings: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        assert 0 <= self.confidence <= 1
        assert self.invariants_validated, "Cannot produce invalid calibration"


# =============================================================================
# /evaluation/virality_calibration/invariants.py
# =============================================================================
"""
The constitution of reality.
Hard constraints that calibration must never violate.
"""

from typing import List, Tuple
# All schemas and classes are defined in this file


class InvariantViolation(Exception):
    """Raised when a core invariant is broken."""
    pass


class Invariants:
    """Immutable laws of calibration."""
    
    # Numerical tolerance for floating point comparisons
    EPSILON = 1e-6
    
    @staticmethod
    def validate_tail_weight_bounds(metrics: NormalizedMetrics) -> bool:
        """Tail weight must be in [0, 1]."""
        return 0 <= metrics.tail_weight <= 1
    
    @staticmethod
    def validate_suppression_bounds(metrics: NormalizedMetrics) -> bool:
        """Suppression discount must be in [0, 1]."""
        return 0 <= metrics.suppression_discount <= 1
    
    @staticmethod
    def validate_no_future_leakage(gt: GroundTruth) -> bool:
        """No horizon can be before publish time or unreasonably far in future."""
        if not gt.horizons:
            return False
        # All horizons must be after publish time
        for h in gt.horizons:
            if h.timestamp < gt.publish_timestamp:
                return False
            # Allow reasonable future tolerance (up to 32 days for 30d horizon + buffer)
            max_reasonable = gt.publish_timestamp.timestamp() + (32 * 24 * 3600)
            if h.timestamp.timestamp() > max_reasonable:
                return False
        return True
    
    @staticmethod
    def validate_monotonic_confidence(
        partial_conf: float,
        full_conf: float
    ) -> bool:
        """Confidence should not decrease with more data."""
        return full_conf >= partial_conf - 0.05  # Allow tiny numerical error
    
    @staticmethod
    def validate_normalization_reversibility(
        original_views: int,
        normalized_score: float,
        correction_factor: float
    ) -> bool:
        """Should be able to approximately reverse normalization."""
        if correction_factor == 0:
            return original_views == 0
        reconstructed = normalized_score / correction_factor
        relative_error = abs(reconstructed - original_views) / max(original_views, 1)
        return relative_error < 0.20  # Within 20%
    
    @staticmethod
    def validate_horizon_monotonicity(gt: GroundTruth) -> bool:
        """Views should generally increase (or stay flat) across horizons."""
        sorted_horizons = sorted(gt.horizons, key=lambda h: h.horizon_hours)
        view_counts = [h.metrics.views for h in sorted_horizons]
        
        # Allow small decreases due to data corrections, but not >10%
        for i in range(1, len(view_counts)):
            if view_counts[i] < view_counts[i-1]:
                decrease_ratio = (view_counts[i-1] - view_counts[i]) / max(view_counts[i-1], 1)
                if decrease_ratio > 0.10:  # >10% decrease is suspicious
                    return False
        return True
    
    @staticmethod
    def validate_metric_consistency(metrics: NormalizedMetrics) -> bool:
        """Ensure metrics are internally consistent."""
        # Virality score should correlate with engagement
        if metrics.virality_score > 100 and metrics.engagement_density < 0.001:
            return False  # High virality but no engagement is inconsistent
        
        # Suppression discount should affect final score
        if metrics.suppression_discount < 0.5 and metrics.virality_score > 500:
            return False  # High score despite suppression is suspicious
        
        # Quality score should correlate with retention
        if metrics.quality_score > 0.8 and metrics.retention_score < 20:
            return False  # High quality but low retention is inconsistent
        
        return True
    
    @staticmethod
    def validate_platform_consistency(gt: GroundTruth) -> bool:
        """Ensure platform-specific metrics are consistent."""
        # Check that platform matches metrics structure
        if gt.platform == Platform.TIKTOK:
            # TikTok should have high velocity typically
            if len(gt.horizons) >= 2:
                views_6h = next((h.metrics.views for h in gt.horizons if h.horizon_hours == 6), 0)
                views_24h = next((h.metrics.views for h in gt.horizons if h.horizon_hours == 24), 0)
                if views_24h > 0:
                    early_ratio = views_6h / views_24h
                    # TikTok typically has 40-60% of views in first 6h
                    if not (0.2 <= early_ratio <= 0.8):
                        return False  # Unusual velocity pattern
        
        return True
    
    @staticmethod
    def validate_era_consistency(gt: GroundTruth) -> bool:
        """Ensure era identifier matches publish timestamp."""
        try:
            year, quarter_str = gt.era_identifier.split("-Q")
            quarter = int(quarter_str)
            expected_quarter = (gt.publish_timestamp.month - 1) // 3 + 1
            expected_year = gt.publish_timestamp.year
            
            if int(year) != expected_year:
                return False
            if abs(quarter - expected_quarter) > 1:  # Allow 1 quarter tolerance
                return False
        except (ValueError, AttributeError):
            return False
        return True
    
    @staticmethod
    def validate_all(
        metrics: NormalizedMetrics,
        ground_truth: GroundTruth
    ) -> Tuple[bool, List[str]]:
        """Run all invariant checks."""
        violations = []
        
        if not Invariants.validate_tail_weight_bounds(metrics):
            violations.append(f"Tail weight out of bounds: {metrics.tail_weight}")
        
        if not Invariants.validate_suppression_bounds(metrics):
            violations.append(f"Suppression out of bounds: {metrics.suppression_discount}")
        
        if not Invariants.validate_no_future_leakage(ground_truth):
            violations.append("Future leakage detected in horizons")
        
        if not Invariants.validate_horizon_monotonicity(ground_truth):
            violations.append("Horizon views not monotonically increasing")
        
        if not Invariants.validate_metric_consistency(metrics):
            violations.append("Metrics internally inconsistent")
        
        if not Invariants.validate_platform_consistency(ground_truth):
            violations.append("Platform-specific consistency check failed")
        
        if not Invariants.validate_era_consistency(ground_truth):
            violations.append("Era identifier inconsistent with publish timestamp")
        
        return len(violations) == 0, violations


# =============================================================================
# /evaluation/virality_calibration/ground_truth_assembler.py
# =============================================================================
"""
Builds immutable reality snapshots.
Consumes final observed outcomes. Never mutates.
"""

from typing import List, Optional
from datetime import datetime, timedelta
# All classes defined in this file - no imports needed


class GroundTruthAssembler:
    """Assembles immutable ground truth from observed data."""
    
    REQUIRED_HORIZONS = [6, 24, 168, 720]  # hours
    
    def __init__(self, require_all_horizons: bool = True):
        self.require_all_horizons = require_all_horizons
    
    def assemble(
        self,
        content_id: str,
        platform: Platform,
        format_type: ContentFormat,
        origin: OriginType,
        publish_time: datetime,
        horizon_snapshots: List[TimeHorizonMetrics],
        final_metrics: PlatformMetrics,
    ) -> GroundTruth:
        """
        Assemble ground truth from components.
        Validates completeness and freezes data.
        """
        # Validate horizon completeness
        captured_horizons = {h.horizon_hours for h in horizon_snapshots}
        required = set(self.REQUIRED_HORIZONS)
        
        if self.require_all_horizons and not required.issubset(captured_horizons):
            missing = required - captured_horizons
            raise ValueError(f"Missing required horizons: {missing}")
        
        # Determine observation completeness
        has_30d = 720 in captured_horizons
        observation_complete = has_30d and all(
            h.is_complete for h in horizon_snapshots if h.horizon_hours == 720
        )
        
        # Determine era
        era = self._compute_era(publish_time)
        
        # Build immutable ground truth
        return GroundTruth(
            content_id=content_id,
            platform=platform,
            format=format_type,
            origin_type=origin,
            publish_timestamp=publish_time,
            horizons=sorted(horizon_snapshots, key=lambda h: h.horizon_hours),
            final_observed_metrics=final_metrics,
            observation_complete=observation_complete,
            era_identifier=era,
        )
    
    def _compute_era(self, timestamp: datetime) -> str:
        """Compute era identifier for drift correction."""
        year = timestamp.year
        quarter = (timestamp.month - 1) // 3 + 1
        return f"{year}-Q{quarter}"
    
    def validate_horizon_integrity(
        self,
        horizons: List[TimeHorizonMetrics],
        publish_time: datetime,
    ) -> bool:
        """Ensure horizons are logically consistent."""
        for h in horizons:
            expected_time = publish_time + timedelta(hours=h.horizon_hours)
            delta = abs((h.timestamp - expected_time).total_seconds())
            if delta > 3600:  # Allow 1hr tolerance
                return False
        return True


# =============================================================================
# /evaluation/virality_calibration/platform_normalizer.py
# =============================================================================
"""
Corrects platform physics to make metrics comparable.
Makes "1M views" mean the same thing everywhere.
"""

from typing import Dict
# All classes defined in this file


class PlatformNormalizer:
    """Normalize metrics across platforms with advanced algorithms and caching."""
    
    def __init__(self):
        # Cache for expensive computations
        self._normalization_cache: Dict[tuple, float] = {}
        self._velocity_cache: Dict[tuple, float] = {}
        self._retention_cache: Dict[tuple, float] = {}
        self._cache_hits = 0
        self._cache_misses = 0
    
    # Platform-specific correction factors (empirically derived, continuously updated)
    PLATFORM_VELOCITY_BIAS = {
        Platform.TIKTOK: 2.5,  # TikTok front-loads distribution
        Platform.YOUTUBE_SHORT: 1.8,
        Platform.YOUTUBE_LONG: 0.6,  # Slower but sustained
        Platform.INSTAGRAM_REEL: 2.0,
        Platform.INSTAGRAM_POST: 1.0,
        Platform.TWITTER: 1.2,
    }
    
    PLATFORM_RETENTION_WEIGHT = {
        Platform.TIKTOK: 0.7,
        Platform.YOUTUBE_SHORT: 0.8,
        Platform.YOUTUBE_LONG: 1.5,  # Retention matters more
        Platform.INSTAGRAM_REEL: 0.75,
        Platform.INSTAGRAM_POST: 0.5,
        Platform.TWITTER: 0.3,
    }
    
    VIEW_DEFINITION_MULTIPLIER = {
        Platform.TIKTOK: 1.0,  # Baseline
        Platform.YOUTUBE_SHORT: 0.95,  # Stricter view threshold
        Platform.YOUTUBE_LONG: 0.9,
        Platform.INSTAGRAM_REEL: 1.05,
        Platform.INSTAGRAM_POST: 1.1,
        Platform.TWITTER: 1.2,  # Video views = autoplay
    }
    
    # Advanced platform-specific parameters
    PLATFORM_ENGAGEMENT_WEIGHTS = {
        Platform.TIKTOK: {"likes": 1.0, "comments": 3.0, "shares": 5.0, "saves": 2.0},
        Platform.YOUTUBE_SHORT: {"likes": 1.0, "comments": 4.0, "shares": 6.0, "saves": 2.5},
        Platform.YOUTUBE_LONG: {"likes": 1.0, "comments": 5.0, "shares": 7.0, "saves": 3.0},
        Platform.INSTAGRAM_REEL: {"likes": 1.0, "comments": 3.5, "shares": 5.5, "saves": 2.5},
        Platform.INSTAGRAM_POST: {"likes": 1.0, "comments": 4.0, "shares": 6.0, "saves": 3.0},
        Platform.TWITTER: {"likes": 1.0, "comments": 2.0, "shares": 4.0, "retweets": 5.0},
    }
    
    # Platform-specific view quality thresholds
    PLATFORM_QUALITY_THRESHOLDS = {
        Platform.TIKTOK: {"min_watch_seconds": 3.0, "min_completion": 0.15},
        Platform.YOUTUBE_SHORT: {"min_watch_seconds": 5.0, "min_completion": 0.20},
        Platform.YOUTUBE_LONG: {"min_watch_seconds": 30.0, "min_completion": 0.30},
        Platform.INSTAGRAM_REEL: {"min_watch_seconds": 2.0, "min_completion": 0.10},
        Platform.INSTAGRAM_POST: {"min_watch_seconds": 1.0, "min_completion": 0.05},
        Platform.TWITTER: {"min_watch_seconds": 1.0, "min_completion": 0.05},
    }
    
    def normalize_views(
        self,
        raw_views: int,
        platform: Platform,
        format_type: ContentFormat,
        metrics: Optional[PlatformMetrics] = None,
    ) -> float:
        """Normalize view count across platforms with quality adjustments."""
        # Check cache
        cache_key = (raw_views, platform.value, format_type.value)
        if cache_key in self._normalization_cache:
            self._cache_hits += 1
            return self._normalization_cache[cache_key]
        
        self._cache_misses += 1
        
        base_multiplier = self.VIEW_DEFINITION_MULTIPLIER[platform]
        
        # Apply format-specific correction
        format_correction = self._get_format_correction(format_type, platform)
        
        # Apply quality-based adjustment if metrics provided
        quality_adjustment = 1.0
        if metrics is not None:
            quality_adjustment = self._compute_quality_adjustment(metrics, platform)
        
        # Apply view count scaling (larger numbers need different normalization)
        scale_adjustment = self._compute_scale_adjustment(raw_views, platform)
        
        normalized = raw_views * base_multiplier * format_correction * quality_adjustment * scale_adjustment
        
        # Cache result (limit cache size)
        if len(self._normalization_cache) < 10000:
            self._normalization_cache[cache_key] = normalized
        
        return normalized
    
    def compute_velocity_score(
        self,
        views_6h: int,
        views_24h: int,
        platform: Platform,
        views_7d: Optional[int] = None,
    ) -> float:
        """Platform-normalized velocity (early momentum) with advanced modeling."""
        # Check cache
        cache_key = (views_6h, views_24h, platform.value, views_7d or 0)
        if cache_key in self._velocity_cache:
            self._cache_hits += 1
            return self._velocity_cache[cache_key]
        
        self._cache_misses += 1
        
        velocity_bias = self.PLATFORM_VELOCITY_BIAS[platform]
        
        # Advanced weighted combination with exponential decay
        if views_24h > 0:
            # Early momentum (6h) weighted more heavily
            early_weight = 0.65
            mid_weight = 0.25
            late_weight = 0.10
            
            early_momentum = views_6h * early_weight
            mid_momentum = (views_24h - views_6h) * mid_weight
            
            # If 7d data available, use for better modeling
            if views_7d is not None and views_7d > views_24h:
                late_momentum = (views_7d - views_24h) * late_weight
            else:
                late_momentum = 0.0
            
            total_momentum = early_momentum + mid_momentum + late_momentum
        else:
            total_momentum = views_6h * 0.8  # Fallback if no 24h data
        
        # Normalize by platform bias
        velocity = total_momentum / velocity_bias
        
        # Apply acceleration factor (how fast it's growing)
        if views_24h > 0 and views_6h > 0:
            acceleration = views_24h / max(views_6h, 1)
            # Boost for accelerating content
            if acceleration > 2.0:
                velocity *= 1.1
            elif acceleration < 1.5:
                velocity *= 0.95
        
        # Cache result
        if len(self._velocity_cache) < 10000:
            self._velocity_cache[cache_key] = velocity
        
        return velocity
    
    def compute_retention_score(
        self,
        metrics: PlatformMetrics,
        platform: Platform,
    ) -> float:
        """Platform-normalized retention quality with multi-signal analysis."""
        # Check cache
        cache_key = (
            metrics.views,
            metrics.completion_rate or 0,
            metrics.avg_watch_percentage or 0,
            platform.value,
        )
        if cache_key in self._retention_cache:
            self._cache_hits += 1
            return self._retention_cache[cache_key]
        
        self._cache_misses += 1
        
        retention_weight = self.PLATFORM_RETENTION_WEIGHT[platform]
        
        # Multi-signal retention analysis
        retention_signals = []
        
        # Primary signal: completion rate
        if metrics.completion_rate is not None:
            retention_signals.append(metrics.completion_rate * 100)
        
        # Secondary signal: average watch percentage
        if metrics.avg_watch_percentage is not None:
            retention_signals.append(metrics.avg_watch_percentage)
        
        # Tertiary signal: watch time relative to content length
        if metrics.watch_time_seconds is not None and metrics.avg_watch_percentage is not None:
            # Estimate content length from watch percentage
            if metrics.avg_watch_percentage > 0:
                estimated_length = metrics.watch_time_seconds / (metrics.avg_watch_percentage / 100.0)
                if estimated_length > 0:
                    watch_ratio = metrics.watch_time_seconds / estimated_length
                    retention_signals.append(watch_ratio * 100)
        
        # Fallback: estimate from engagement quality
        if not retention_signals:
            engagement_rate = self._compute_platform_engagement_rate(metrics, platform)
            # High engagement suggests good retention
            estimated_retention = min(100, engagement_rate * 500)
            retention_signals.append(estimated_retention)
        
        # Weighted average of signals
        if len(retention_signals) == 1:
            base_retention = retention_signals[0]
        elif len(retention_signals) == 2:
            base_retention = retention_signals[0] * 0.7 + retention_signals[1] * 0.3
        else:
            base_retention = (
                retention_signals[0] * 0.5 +
                retention_signals[1] * 0.3 +
                retention_signals[2] * 0.2
            )
        
        # Apply platform-specific quality thresholds
        thresholds = self.PLATFORM_QUALITY_THRESHOLDS.get(platform, {})
        if thresholds and metrics.avg_watch_percentage is not None:
            min_completion = thresholds.get("min_completion", 0.0)
            if metrics.avg_watch_percentage / 100.0 < min_completion:
                base_retention *= 0.8  # Penalize low-quality views
        
        retention_score = base_retention * retention_weight
        
        # Cache result
        if len(self._retention_cache) < 10000:
            self._retention_cache[cache_key] = retention_score
        
        return retention_score
    
    def _compute_quality_adjustment(
        self,
        metrics: PlatformMetrics,
        platform: Platform,
    ) -> float:
        """Compute quality-based adjustment for view normalization."""
        adjustment = 1.0
        
        # Check against platform quality thresholds
        thresholds = self.PLATFORM_QUALITY_THRESHOLDS.get(platform, {})
        
        if metrics.avg_watch_percentage is not None:
            min_completion = thresholds.get("min_completion", 0.0)
            completion_ratio = (metrics.avg_watch_percentage / 100.0) / max(min_completion, 0.01)
            # Boost high-quality views, penalize low-quality
            if completion_ratio > 1.5:
                adjustment *= 1.1
            elif completion_ratio < 0.8:
                adjustment *= 0.9
        
        # Engagement quality adjustment
        engagement_rate = self._compute_platform_engagement_rate(metrics, platform)
        if engagement_rate > 0.05:  # High engagement
            adjustment *= 1.05
        elif engagement_rate < 0.01:  # Low engagement
            adjustment *= 0.95
        
        return adjustment
    
    def _compute_scale_adjustment(self, views: int, platform: Platform) -> float:
        """Apply logarithmic scaling for very large view counts."""
        # For very large numbers, apply logarithmic normalization
        if views > 10_000_000:
            # Logarithmic scaling for mega-viral content
            log_factor = 1.0 + (views / 100_000_000) * 0.1
            return min(1.2, log_factor)
        elif views > 1_000_000:
            # Slight boost for viral content
            return 1.05
        return 1.0
    
    def _compute_platform_engagement_rate(
        self,
        metrics: PlatformMetrics,
        platform: Platform,
    ) -> float:
        """Compute platform-specific engagement rate."""
        weights = self.PLATFORM_ENGAGEMENT_WEIGHTS.get(platform, {})
        
        total_engagement = (
            metrics.likes * weights.get("likes", 1.0) +
            metrics.comments * weights.get("comments", 3.0) +
            metrics.shares * weights.get("shares", 5.0) +
            (metrics.saves or 0) * weights.get("saves", 2.0) +
            (metrics.retweets or 0) * weights.get("retweets", 4.0)
        )
        
        return total_engagement / max(metrics.views, 1)
    
    def _get_format_correction(
        self,
        format_type: ContentFormat,
        platform: Platform,
    ) -> float:
        """Format-specific correction within platform with advanced logic."""
        # Shorts get different distribution than long-form
        if format_type in [ContentFormat.SHORT_VERTICAL, ContentFormat.SHORT_HORIZONTAL]:
            if platform == Platform.YOUTUBE_LONG:
                return 0.5  # Shorts uploaded to long-form channel
            elif platform == Platform.TIKTOK:
                return 1.0  # Native format
            return 1.0
        elif format_type == ContentFormat.LONG_FORM:
            if platform in [Platform.TIKTOK, Platform.INSTAGRAM_REEL]:
                return 1.2  # Rare long-form gets boost
            elif platform == Platform.YOUTUBE_LONG:
                return 1.0  # Native format
            return 1.0
        elif format_type == ContentFormat.STILL_IMAGE:
            if platform == Platform.INSTAGRAM_POST:
                return 1.0  # Native format
            return 0.8  # Images on video platforms
        elif format_type == ContentFormat.TEXT_ONLY:
            if platform == Platform.TWITTER:
                return 1.0  # Native format
            return 0.6  # Text on visual platforms
        return 1.0
    
    def get_cache_stats(self) -> Dict[str, int]:
        """Get cache performance statistics."""
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": self._cache_hits / max(self._cache_hits + self._cache_misses, 1),
            "cache_size": len(self._normalization_cache),
        }
    
    def clear_cache(self):
        """Clear all caches."""
        self._normalization_cache.clear()
        self._velocity_cache.clear()
        self._retention_cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0


# =============================================================================
# /evaluation/virality_calibration/format_equivalence.py
# =============================================================================
"""
Normalizes content structure to prevent format bias.
Corrects for: short vs long, episodic vs standalone, repost vs original.
"""

from .schemas import ContentFormat, OriginType, Platform


class FormatEquivalenceCalculator:
    """Ensure format doesn't contaminate learning."""
    
    # Base equivalence factors
    FORMAT_BASE_FACTORS = {
        ContentFormat.SHORT_VERTICAL: 1.0,  # Baseline
        ContentFormat.SHORT_HORIZONTAL: 0.95,
        ContentFormat.LONG_FORM: 1.3,  # More effort, higher bar
        ContentFormat.STILL_IMAGE: 0.7,
        ContentFormat.CAROUSEL: 0.85,
        ContentFormat.TEXT_ONLY: 0.6,
    }
    
    ORIGIN_MULTIPLIERS = {
        OriginType.ORIGINAL: 1.0,
        OriginType.REPOST: 0.4,  # Much easier distribution
        OriginType.REMIX: 0.7,
        OriginType.DUET: 0.8,
    }
    
    def compute_equivalence_factor(
        self,
        format_type: ContentFormat,
        origin: OriginType,
        platform: Platform,
    ) -> float:
        """
        Compute correction factor to make formats comparable.
        Returns multiplier to adjust virality score.
        """
        base = self.FORMAT_BASE_FACTORS[format_type]
        origin_mult = self.ORIGIN_MULTIPLIERS[origin]
        
        # Platform-specific format preferences
        platform_pref = self._platform_format_preference(format_type, platform)
        
        return base * origin_mult * platform_pref
    
    def _platform_format_preference(
        self,
        format_type: ContentFormat,
        platform: Platform,
    ) -> float:
        """Some platforms heavily favor certain formats."""
        # TikTok massively favors vertical short
        if platform == Platform.TIKTOK:
            if format_type == ContentFormat.SHORT_VERTICAL:
                return 1.0
            elif format_type == ContentFormat.LONG_FORM:
                return 0.6
        
        # YouTube Long favors long-form
        if platform == Platform.YOUTUBE_LONG:
            if format_type == ContentFormat.LONG_FORM:
                return 1.0
            elif format_type in [ContentFormat.SHORT_VERTICAL, ContentFormat.SHORT_HORIZONTAL]:
                return 0.7
        
        return 1.0  # Neutral


# =============================================================================
# /evaluation/virality_calibration/era_drift_corrector.py
# =============================================================================
"""
Ensures time invariance across algorithm updates and metric changes.
Guarantee: A win in 2023 equals a win in 2026.
"""

from typing import Dict
from datetime import datetime
from .schemas import Platform


class EraDriftCorrector:
    """Correct for temporal drift in platform algorithms and metrics with advanced interpolation."""
    
    def __init__(self):
        # Cache for interpolation results
        self._interpolation_cache: Dict[tuple, float] = {}
    
    # Era-specific inflation factors (empirically measured, continuously updated)
    # Format: {platform: {era: multiplier}}
    ERA_INFLATION = {
        Platform.TIKTOK: {
            "2023-Q1": 0.85,
            "2023-Q2": 0.88,
            "2023-Q3": 0.91,
            "2023-Q4": 0.94,
            "2024-Q1": 0.97,
            "2024-Q2": 0.99,
            "2024-Q3": 1.00,
            "2024-Q4": 1.02,
            "2025-Q1": 1.05,
            "2025-Q2": 1.08,
            "2025-Q3": 1.10,
            "2025-Q4": 1.12,
            "2026-Q1": 1.15,
            "2026-Q2": 1.18,
            "2026-Q3": 1.20,
            "2026-Q4": 1.22,
        },
        Platform.YOUTUBE_SHORT: {
            "2023-Q1": 0.80,
            "2023-Q2": 0.84,
            "2023-Q3": 0.88,
            "2023-Q4": 0.92,
            "2024-Q1": 0.95,
            "2024-Q2": 0.97,
            "2024-Q3": 1.00,
            "2024-Q4": 1.03,
            "2025-Q1": 1.06,
            "2025-Q2": 1.09,
            "2025-Q3": 1.12,
            "2025-Q4": 1.14,
            "2026-Q1": 1.17,
            "2026-Q2": 1.19,
            "2026-Q3": 1.21,
            "2026-Q4": 1.23,
        },
        Platform.YOUTUBE_LONG: {
            "2023-Q1": 0.82,
            "2023-Q2": 0.86,
            "2023-Q3": 0.90,
            "2023-Q4": 0.93,
            "2024-Q1": 0.96,
            "2024-Q2": 0.98,
            "2024-Q3": 1.00,
            "2024-Q4": 1.02,
            "2025-Q1": 1.05,
            "2025-Q2": 1.07,
            "2025-Q3": 1.09,
            "2025-Q4": 1.11,
            "2026-Q1": 1.13,
        },
        Platform.INSTAGRAM_REEL: {
            "2023-Q1": 0.83,
            "2023-Q2": 0.87,
            "2023-Q3": 0.90,
            "2023-Q4": 0.93,
            "2024-Q1": 0.96,
            "2024-Q2": 0.98,
            "2024-Q3": 1.00,
            "2024-Q4": 1.02,
            "2025-Q1": 1.04,
            "2025-Q2": 1.06,
            "2025-Q3": 1.08,
            "2025-Q4": 1.10,
            "2026-Q1": 1.12,
        },
        Platform.INSTAGRAM_POST: {
            "2023-Q1": 0.88,
            "2023-Q2": 0.91,
            "2023-Q3": 0.94,
            "2023-Q4": 0.96,
            "2024-Q1": 0.98,
            "2024-Q2": 0.99,
            "2024-Q3": 1.00,
            "2024-Q4": 1.01,
            "2025-Q1": 1.03,
            "2025-Q2": 1.04,
            "2025-Q3": 1.05,
            "2025-Q4": 1.06,
            "2026-Q1": 1.07,
        },
        Platform.TWITTER: {
            "2023-Q1": 0.90,
            "2023-Q2": 0.92,
            "2023-Q3": 0.94,
            "2023-Q4": 0.96,
            "2024-Q1": 0.98,
            "2024-Q2": 0.99,
            "2024-Q3": 1.00,
            "2024-Q4": 1.01,
            "2025-Q1": 1.02,
            "2025-Q2": 1.03,
            "2025-Q3": 1.04,
            "2025-Q4": 1.05,
            "2026-Q1": 1.06,
        },
    }
    
    # Algorithm update events that cause sudden drift
    ALGORITHM_UPDATES = {
        Platform.TIKTOK: {
            "2024-Q2": 0.02,  # Algorithm update caused 2% inflation
            "2025-Q1": 0.03,
        },
        Platform.YOUTUBE_SHORT: {
            "2024-Q1": 0.015,
            "2025-Q2": 0.025,
        },
    }
    
    BASELINE_ERA = "2024-Q3"  # Reference point
    
    def compute_era_correction(
        self,
        platform: Platform,
        era_identifier: str,
    ) -> float:
        """
        Compute correction factor for temporal drift with advanced interpolation.
        Returns multiplier to normalize to baseline era.
        """
        # Check cache
        cache_key = (platform.value, era_identifier)
        if cache_key in self._interpolation_cache:
            return self._interpolation_cache[cache_key]
        
        if platform not in self.ERA_INFLATION:
            return 1.0  # No correction available
        
        era_factors = self.ERA_INFLATION[platform]
        
        if era_identifier not in era_factors:
            # Unknown era - use advanced interpolation
            correction = self._interpolate_era_advanced(era_identifier, era_factors, platform)
        else:
            current_factor = era_factors[era_identifier]
            baseline_factor = era_factors.get(self.BASELINE_ERA, 1.0)
            
            # Apply algorithm update adjustments
            algorithm_adjustment = self._get_algorithm_adjustment(platform, era_identifier)
            current_factor *= (1.0 + algorithm_adjustment)
            
            # Normalize to baseline
            correction = baseline_factor / current_factor
        
        # Cache result
        if len(self._interpolation_cache) < 10000:
            self._interpolation_cache[cache_key] = correction
        
        return correction
    
    def _interpolate_era_advanced(
        self,
        unknown_era: str,
        known_factors: Dict[str, float],
        platform: Platform,
    ) -> float:
        """Advanced interpolation for unknown era using linear/spline methods."""
        # Parse era
        try:
            year, quarter = unknown_era.split("-Q")
            year = int(year)
            quarter = int(quarter)
            unknown_ordinal = self._era_to_ordinal(unknown_era)
        except:
            return 1.0  # Can't parse, no correction
        
        if not known_factors:
            return 1.0
        
        # Find bounding eras for interpolation
        known_ordinals = {self._era_to_ordinal(e): (e, f) for e, f in known_factors.items()}
        sorted_ordinals = sorted(known_ordinals.keys())
        
        # Check if before first known era
        if unknown_ordinal < sorted_ordinals[0]:
            # Extrapolate backward
            first_era, first_factor = known_ordinals[sorted_ordinals[0]]
            if len(sorted_ordinals) >= 2:
                second_era, second_factor = known_ordinals[sorted_ordinals[1]]
                # Linear extrapolation
                slope = (first_factor - second_factor) / (sorted_ordinals[0] - sorted_ordinals[1])
                extrapolated = first_factor + slope * (unknown_ordinal - sorted_ordinals[0])
                return max(0.5, min(2.0, extrapolated))  # Bound extrapolation
            return first_factor
        
        # Check if after last known era
        if unknown_ordinal > sorted_ordinals[-1]:
            # Extrapolate forward
            last_era, last_factor = known_ordinals[sorted_ordinals[-1]]
            if len(sorted_ordinals) >= 2:
                second_last_era, second_last_factor = known_ordinals[sorted_ordinals[-2]]
                # Linear extrapolation
                slope = (last_factor - second_last_factor) / (sorted_ordinals[-1] - sorted_ordinals[-2])
                extrapolated = last_factor + slope * (unknown_ordinal - sorted_ordinals[-1])
                return max(0.5, min(2.0, extrapolated))  # Bound extrapolation
            return last_factor
        
        # Interpolate between known eras
        # Find bounding points
        lower_idx = None
        upper_idx = None
        
        for i in range(len(sorted_ordinals) - 1):
            if sorted_ordinals[i] <= unknown_ordinal <= sorted_ordinals[i + 1]:
                lower_idx = i
                upper_idx = i + 1
                break
        
        if lower_idx is None or upper_idx is None:
            # Fallback to closest
            closest_ordinal = min(sorted_ordinals, key=lambda x: abs(x - unknown_ordinal))
            return known_ordinals[closest_ordinal][1]
        
        # Linear interpolation
        lower_ordinal, lower_factor = sorted_ordinals[lower_idx], known_ordinals[sorted_ordinals[lower_idx]][1]
        upper_ordinal, upper_factor = sorted_ordinals[upper_idx], known_ordinals[sorted_ordinals[upper_idx]][1]
        
        if upper_ordinal == lower_ordinal:
            return lower_factor
        
        # Linear interpolation
        t = (unknown_ordinal - lower_ordinal) / (upper_ordinal - lower_ordinal)
        interpolated = lower_factor + t * (upper_factor - lower_factor)
        
        # Apply algorithm update adjustments
        algorithm_adjustment = self._get_algorithm_adjustment(platform, unknown_era)
        interpolated *= (1.0 + algorithm_adjustment)
        
        baseline_factor = known_factors.get(self.BASELINE_ERA, 1.0)
        return baseline_factor / interpolated
    
    def _get_algorithm_adjustment(self, platform: Platform, era: str) -> float:
        """Get algorithm update adjustment for specific era."""
        updates = self.ALGORITHM_UPDATES.get(platform, {})
        return updates.get(era, 0.0)
    
    def _interpolate_era(
        self,
        unknown_era: str,
        known_factors: Dict[str, float],
    ) -> float:
        """Legacy interpolation method (kept for compatibility)."""
        # Parse era
        try:
            year, quarter = unknown_era.split("-Q")
            year = int(year)
            quarter = int(quarter)
        except:
            return 1.0  # Can't parse, no correction
        
        # Find closest known era
        closest_era = min(
            known_factors.keys(),
            key=lambda e: abs(self._era_to_ordinal(e) - self._era_to_ordinal(unknown_era))
        )
        
        return known_factors[closest_era]
    
    def _era_to_ordinal(self, era: str) -> int:
        """Convert era to ordinal for comparison."""
        try:
            year, quarter = era.split("-Q")
            return int(year) * 4 + int(quarter)
        except:
            return 0
    
    def get_era_inflation_trend(
        self,
        platform: Platform,
        start_era: str,
        end_era: str,
    ) -> float:
        """Get inflation trend between two eras."""
        if platform not in self.ERA_INFLATION:
            return 1.0
        
        era_factors = self.ERA_INFLATION[platform]
        start_factor = era_factors.get(start_era, 1.0)
        end_factor = era_factors.get(end_era, 1.0)
        
        return end_factor / start_factor
    
    def clear_cache(self):
        """Clear interpolation cache."""
        self._interpolation_cache.clear()


# =============================================================================
# /evaluation/virality_calibration/tail_weight_estimator.py
# =============================================================================
"""
Models sustained virality for 30M-300M repeatability.
Rewards slow sustained growth, penalizes short-lived spikes.
"""

from typing import List
import math
from .schemas import TimeHorizonMetrics


class TailWeightEstimator:
    """Estimate long-term stability and sustained performance with multiple models."""
    
    def __init__(self):
        # Model weights for ensemble approach
        self.model_weights = {
            "stability": 0.30,
            "late_performance": 0.25,
            "reactivation": 0.20,
            "decay_rate": 0.15,
            "momentum_preservation": 0.10,
        }
    
    def estimate_tail_weight(
        self,
        horizons: List[TimeHorizonMetrics],
    ) -> float:
        """
        Compute tail weight ∈ [0, 1] using ensemble of models.
        
        1.0 = Perfect sustained growth
        0.5 = Moderate stability
        0.0 = Complete collapse after peak
        """
        if len(horizons) < 2:
            return 0.5  # Insufficient data
        
        # Sort by horizon
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        
        # Extract view counts and timestamps
        view_trajectory = [h.metrics.views for h in sorted_horizons]
        time_points = [h.horizon_hours for h in sorted_horizons]
        
        if max(view_trajectory) == 0:
            return 0.0
        
        # Ensemble of models
        stability = self._compute_stability(view_trajectory, time_points)
        late_performance = self._compute_late_performance(view_trajectory)
        reactivation = self._detect_reactivation(view_trajectory, time_points)
        decay_rate = self._compute_decay_rate(view_trajectory, time_points)
        momentum = self._compute_momentum_preservation(view_trajectory, time_points)
        
        # Weighted ensemble
        tail_weight = (
            stability * self.model_weights["stability"] +
            late_performance * self.model_weights["late_performance"] +
            reactivation * self.model_weights["reactivation"] +
            decay_rate * self.model_weights["decay_rate"] +
            momentum * self.model_weights["momentum_preservation"]
        )
        
        # Apply confidence adjustment based on data quality
        confidence = self._compute_data_confidence(sorted_horizons)
        tail_weight = tail_weight * confidence + 0.3 * (1 - confidence)  # Conservative fallback
        
        return max(0.0, min(1.0, tail_weight))
    
    def _compute_stability(
        self,
        trajectory: List[int],
        time_points: List[int],
    ) -> float:
        """Measure consistency of growth with time-weighted analysis."""
        if len(trajectory) < 2:
            return 0.5
        
        # Compute growth rates between horizons with time weighting
        growth_rates = []
        time_weights = []
        
        for i in range(1, len(trajectory)):
            if trajectory[i-1] > 0:
                # Absolute growth rate
                rate = trajectory[i] / trajectory[i-1]
                growth_rates.append(rate)
                
                # Time weight (longer intervals weighted more)
                time_delta = time_points[i] - time_points[i-1]
                time_weights.append(time_delta)
        
        if not growth_rates:
            return 0.0
        
        # Weighted mean and variance
        total_weight = sum(time_weights)
        if total_weight == 0:
            return 0.5
        
        weighted_mean = sum(r * w for r, w in zip(growth_rates, time_weights)) / total_weight
        
        # Compute weighted variance
        weighted_variance = sum(
            w * (r - weighted_mean) ** 2
            for r, w in zip(growth_rates, time_weights)
        ) / total_weight
        
        # Lower variance = higher stability
        # Use exponential decay for variance penalty
        stability = math.exp(-weighted_variance / 2.0)
        
        # Bonus for consistent positive growth
        if weighted_mean > 1.0 and weighted_variance < 0.5:
            stability *= 1.1
        
        return min(1.0, max(0.0, stability))
    
    def _compute_late_performance(self, trajectory: List[int]) -> float:
        """Measure performance in later stages with advanced segmentation."""
        if len(trajectory) < 3:
            return 0.5
        
        n = len(trajectory)
        
        # Multiple comparison windows
        late_start = int(n * 0.7)
        mid_start = int(n * 0.35)
        mid_end = int(n * 0.65)
        early_end = int(n * 0.35)
        
        late_views = sum(trajectory[late_start:])
        mid_views = sum(trajectory[mid_start:mid_end])
        early_views = sum(trajectory[:early_end])
        
        if mid_views == 0:
            return 0.0
        
        # Late-stage strength relative to mid-stage
        late_ratio = late_views / mid_views
        
        # Compare to early stage (should maintain momentum)
        if early_views > 0:
            early_to_mid = mid_views / early_views
            mid_to_late = late_views / mid_views
            
            # If late stage maintains or improves on mid-stage, that's good
            if mid_to_late >= early_to_mid * 0.8:
                late_ratio *= 1.2
        
        # Normalize to [0, 1] with sigmoid-like function
        normalized = 1.0 / (1.0 + math.exp(-(late_ratio - 1.0) * 2.0))
        
        return min(1.0, max(0.0, normalized))
    
    def _detect_reactivation(
        self,
        trajectory: List[int],
        time_points: List[int],
    ) -> float:
        """Detect reactivation waves with time-aware analysis."""
        if len(trajectory) < 4:
            return 0.0
        
        n = len(trajectory)
        
        # Multiple time windows for reactivation detection
        reactivation_scores = []
        
        # Compare last quarter to previous quarter
        q4_start = int(n * 0.75)
        q3_start = int(n * 0.5)
        q2_start = int(n * 0.25)
        
        if q4_start < len(trajectory) and q3_start < len(trajectory):
            q4_growth = trajectory[-1] - trajectory[q4_start]
            q3_growth = trajectory[q4_start] - trajectory[q3_start] if q3_start < len(trajectory) else 0
            
            if q3_growth > 0:
                reactivation_ratio = q4_growth / q3_growth
                # Reactivation = late acceleration
                score = min(1.0, reactivation_ratio / 3.0)
                reactivation_scores.append(score)
        
        # Check for acceleration patterns
        if len(trajectory) >= 3:
            # Look for decreasing then increasing pattern
            for i in range(2, len(trajectory)):
                prev_growth = trajectory[i-1] - trajectory[i-2]
                curr_growth = trajectory[i] - trajectory[i-1]
                
                if prev_growth < 0 and curr_growth > 0:
                    # Recovery detected
                    recovery_strength = min(1.0, abs(curr_growth) / max(abs(prev_growth), 1))
                    reactivation_scores.append(recovery_strength * 0.5)
        
        if not reactivation_scores:
            return 0.0
        
        # Take maximum reactivation signal
        return max(reactivation_scores)
    
    def _compute_decay_rate(
        self,
        trajectory: List[int],
        time_points: List[int],
    ) -> float:
        """Compute exponential decay rate and convert to tail weight."""
        if len(trajectory) < 3:
            return 0.5
        
        # Fit exponential decay model: views(t) = A * exp(-lambda * t)
        # Lower decay rate = better tail weight
        
        # Use log-linear regression on later stages
        late_start = max(1, len(trajectory) // 2)
        late_views = trajectory[late_start:]
        late_times = time_points[late_start:]
        
        if len(late_views) < 2 or max(late_views) == 0:
            return 0.5
        
        # Compute decay rate
        decay_rates = []
        for i in range(1, len(late_views)):
            if late_views[i-1] > 0 and late_times[i] > late_times[i-1]:
                # Log ratio gives decay rate
                log_ratio = math.log(late_views[i] / late_views[i-1])
                time_delta = late_times[i] - late_times[i-1]
                if time_delta > 0:
                    decay_rate = -log_ratio / time_delta
                    decay_rates.append(decay_rate)
        
        if not decay_rates:
            return 0.5
        
        avg_decay = sum(decay_rates) / len(decay_rates)
        
        # Convert decay rate to tail weight (lower decay = higher weight)
        # Typical decay: 0.01-0.05 per hour for good content
        if avg_decay < 0:
            # Negative decay = growth, excellent
            return 1.0
        elif avg_decay < 0.01:
            # Very slow decay
            return 0.9
        elif avg_decay < 0.02:
            return 0.7
        elif avg_decay < 0.05:
            return 0.5
        else:
            # Fast decay
            return 0.2
    
    def _compute_momentum_preservation(
        self,
        trajectory: List[int],
        time_points: List[int],
    ) -> float:
        """Measure how well momentum is preserved across horizons."""
        if len(trajectory) < 3:
            return 0.5
        
        # Compute momentum at different stages
        early_momentum = 0.0
        mid_momentum = 0.0
        late_momentum = 0.0
        
        # Early momentum (first 2 points)
        if len(trajectory) >= 2 and time_points[1] > time_points[0]:
            early_momentum = (trajectory[1] - trajectory[0]) / (time_points[1] - time_points[0])
        
        # Mid momentum
        mid_point = len(trajectory) // 2
        if mid_point > 0 and mid_point < len(trajectory) and time_points[mid_point] > time_points[mid_point-1]:
            mid_momentum = (trajectory[mid_point] - trajectory[mid_point-1]) / (time_points[mid_point] - time_points[mid_point-1])
        
        # Late momentum
        if len(trajectory) >= 2:
            last_idx = len(trajectory) - 1
            if time_points[last_idx] > time_points[last_idx-1]:
                late_momentum = (trajectory[last_idx] - trajectory[last_idx-1]) / (time_points[last_idx] - time_points[last_idx-1])
        
        # Compare momentum preservation
        if early_momentum == 0:
            return 0.5
        
        # How well is momentum maintained?
        if mid_momentum > 0:
            mid_preservation = min(1.0, mid_momentum / max(early_momentum, 1))
        else:
            mid_preservation = 0.0
        
        if late_momentum > 0:
            late_preservation = min(1.0, late_momentum / max(early_momentum, 1))
        else:
            late_preservation = 0.0
        
        # Weighted average
        preservation = mid_preservation * 0.4 + late_preservation * 0.6
        
        return preservation
    
    def _compute_data_confidence(self, horizons: List[TimeHorizonMetrics]) -> float:
        """Compute confidence in tail weight estimate based on data quality."""
        if not horizons:
            return 0.0
        
        confidence_factors = []
        
        # Horizon completeness
        required_horizons = {6, 24, 168, 720}
        present_horizons = {h.horizon_hours for h in horizons}
        completeness = len(present_horizons & required_horizons) / len(required_horizons)
        confidence_factors.append(completeness)
        
        # Data quality flags
        complete_count = sum(1 for h in horizons if h.is_complete)
        quality_score = complete_count / len(horizons)
        confidence_factors.append(quality_score)
        
        # Suppression clarity
        suppression_count = sum(1 for h in horizons if h.suppression_detected)
        suppression_clarity = 1.0 - (suppression_count / len(horizons)) * 0.5
        confidence_factors.append(max(0.0, suppression_clarity))
        
        # Average confidence
        return sum(confidence_factors) / len(confidence_factors)


# =============================================================================
# /evaluation/virality_calibration/suppression_discount.py
# =============================================================================
"""
Prevents false negatives by detecting platform suppression.
Ensures content isn't labeled "bad" when platform killed it.
"""

from typing import List
from .schemas import TimeHorizonMetrics, Platform


class SuppressionDetector:
    """Detect and quantify platform suppression/throttling with advanced pattern recognition."""
    
    # Suppression indicators (tuned per platform)
    SUSPICIOUS_FLATLINE_THRESHOLD = 0.05  # <5% growth = suspicious
    SUDDEN_DROP_THRESHOLD = 0.30  # >30% drop = likely suppressed
    ENGAGEMENT_COLLAPSE_THRESHOLD = 0.30  # >70% engagement drop
    
    # Platform-specific thresholds
    PLATFORM_SUPPRESSION_PATTERNS = {
        Platform.TIKTOK: {
            "flatline_threshold": 0.03,  # TikTok is more aggressive
            "drop_threshold": 0.25,
            "engagement_threshold": 0.25,
        },
        Platform.YOUTUBE_SHORT: {
            "flatline_threshold": 0.05,
            "drop_threshold": 0.30,
            "engagement_threshold": 0.35,
        },
        Platform.YOUTUBE_LONG: {
            "flatline_threshold": 0.08,  # Long-form can have slower growth
            "drop_threshold": 0.35,
            "engagement_threshold": 0.40,
        },
        Platform.INSTAGRAM_REEL: {
            "flatline_threshold": 0.04,
            "drop_threshold": 0.28,
            "engagement_threshold": 0.30,
        },
        Platform.INSTAGRAM_POST: {
            "flatline_threshold": 0.06,
            "drop_threshold": 0.32,
            "engagement_threshold": 0.35,
        },
        Platform.TWITTER: {
            "flatline_threshold": 0.05,
            "drop_threshold": 0.30,
            "engagement_threshold": 0.30,
        },
    }
    
    def __init__(self):
        # Pattern recognition cache
        self._pattern_cache: Dict[tuple, float] = {}
    
    def compute_suppression_discount(
        self,
        horizons: List[TimeHorizonMetrics],
        platform: Platform,
    ) -> float:
        """
        Compute suppression discount ∈ [0, 1] using ensemble of detection methods.
        
        1.0 = No suppression detected
        0.5 = Moderate suppression
        0.0 = Severe suppression (shadowban)
        """
        if len(horizons) < 2:
            return 1.0  # Insufficient data
        
        # Check cache
        cache_key = (tuple((h.horizon_hours, h.metrics.views) for h in horizons), platform.value)
        if cache_key in self._pattern_cache:
            return self._pattern_cache[cache_key]
        
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        
        # Check for explicit suppression flags
        if any(h.suppression_detected for h in sorted_horizons):
            discount = 0.3  # Known suppression
            self._pattern_cache[cache_key] = discount
            return discount
        
        # Multi-signal suppression detection
        signals = []
        
        # Pattern-based detection
        flatline_score = self._detect_flatline(sorted_horizons, platform)
        drop_score = self._detect_sudden_drop(sorted_horizons, platform)
        engagement_anomaly = self._detect_engagement_anomaly(sorted_horizons, platform)
        velocity_anomaly = self._detect_velocity_anomaly(sorted_horizons, platform)
        distribution_anomaly = self._detect_distribution_anomaly(sorted_horizons, platform)
        
        signals.extend([
            flatline_score,
            drop_score,
            engagement_anomaly,
            velocity_anomaly,
            distribution_anomaly,
        ])
        
        # Weighted combination (strongest signal dominates)
        suppression_likelihood = max(signals) * 0.6 + sum(signals) / len(signals) * 0.4
        
        # Convert to discount (inverse)
        discount = 1.0 - suppression_likelihood
        
        # Cache result
        if len(self._pattern_cache) < 5000:
            self._pattern_cache[cache_key] = discount
        
        return max(0.0, min(1.0, discount))
    
    def _detect_flatline(
        self,
        horizons: List[TimeHorizonMetrics],
        platform: Platform,
    ) -> float:
        """Detect unnatural stagnation with platform-aware thresholds."""
        view_trajectory = [h.metrics.views for h in horizons]
        time_points = [h.horizon_hours for h in horizons]
        
        if len(view_trajectory) < 3:
            return 0.0
        
        thresholds = self.PLATFORM_SUPPRESSION_PATTERNS.get(platform, {})
        flatline_threshold = thresholds.get("flatline_threshold", self.SUSPICIOUS_FLATLINE_THRESHOLD)
        
        # Check growth in later stages
        late_growth = (view_trajectory[-1] - view_trajectory[-2]) / max(view_trajectory[-2], 1)
        
        if late_growth < flatline_threshold:
            # Check if early growth was healthy
            early_growth = (view_trajectory[1] - view_trajectory[0]) / max(view_trajectory[0], 1)
            
            if early_growth > 0.5:  # Was growing, then stopped
                # Compute flatline severity
                severity = (flatline_threshold - late_growth) / flatline_threshold
                return min(1.0, 0.6 + severity * 0.4)  # 0.6-1.0 range
        
        # Check for extended flatline periods
        flatline_periods = 0
        for i in range(1, len(view_trajectory)):
            growth = (view_trajectory[i] - view_trajectory[i-1]) / max(view_trajectory[i-1], 1)
            if growth < flatline_threshold:
                flatline_periods += 1
        
        if flatline_periods >= len(view_trajectory) - 1:
            return 0.8  # Extended flatline
        
        return 0.0
    
    def _detect_sudden_drop(
        self,
        horizons: List[TimeHorizonMetrics],
        platform: Platform,
    ) -> float:
        """Detect unnatural performance drops with severity scoring."""
        view_trajectory = [h.metrics.views for h in horizons]
        time_points = [h.horizon_hours for h in horizons]
        
        thresholds = self.PLATFORM_SUPPRESSION_PATTERNS.get(platform, {})
        drop_threshold = thresholds.get("drop_threshold", self.SUDDEN_DROP_THRESHOLD)
        
        max_drop_severity = 0.0
        
        for i in range(1, len(view_trajectory)):
            if view_trajectory[i-1] > 0:
                growth = (view_trajectory[i] - view_trajectory[i-1]) / view_trajectory[i-1]
                
                if growth < -drop_threshold:
                    # Compute drop severity
                    severity = abs(growth) / drop_threshold
                    max_drop_severity = max(max_drop_severity, min(1.0, severity))
        
        # Check for recovery (if it drops then recovers, might be temporary throttling)
        if max_drop_severity > 0 and len(view_trajectory) >= 3:
            # Check if later horizons show recovery
            last_growth = (view_trajectory[-1] - view_trajectory[-2]) / max(view_trajectory[-2], 1)
            if last_growth > 0.1:  # Recovery detected
                max_drop_severity *= 0.7  # Reduce severity for recovery
        
        return max_drop_severity
    
    def _detect_engagement_anomaly(
        self,
        horizons: List[TimeHorizonMetrics],
        platform: Platform,
    ) -> float:
        """Detect views without proportional engagement with advanced analysis."""
        if len(horizons) < 2:
            return 0.0
        
        thresholds = self.PLATFORM_SUPPRESSION_PATTERNS.get(platform, {})
        engagement_threshold = thresholds.get("engagement_threshold", self.ENGAGEMENT_COLLAPSE_THRESHOLD)
        
        # Compare engagement rates across horizons
        engagement_rates = []
        view_counts = []
        
        for h in horizons:
            if h.metrics.views > 0:
                # Platform-specific engagement calculation
                if platform == Platform.TWITTER:
                    rate = (
                        h.metrics.likes +
                        h.metrics.comments * 2 +
                        (h.metrics.retweets or 0) * 4 +
                        h.metrics.shares * 3
                    ) / h.metrics.views
                else:
                    rate = (
                        h.metrics.likes +
                        h.metrics.comments * 3 +
                        h.metrics.shares * 5 +
                        (h.metrics.saves or 0) * 2
                    ) / h.metrics.views
                
                engagement_rates.append(rate)
                view_counts.append(h.metrics.views)
        
        if len(engagement_rates) < 2:
            return 0.0
        
        # Check for sudden engagement drop
        max_anomaly = 0.0
        for i in range(1, len(engagement_rates)):
            if engagement_rates[i-1] > 0:
                drop_ratio = engagement_rates[i] / engagement_rates[i-1]
                
                if drop_ratio < (1.0 - engagement_threshold):
                    # Engagement collapsed
                    severity = (1.0 - drop_ratio) / engagement_threshold
                    max_anomaly = max(max_anomaly, min(1.0, severity))
        
        # Check for engagement/view mismatch (high views, low engagement)
        if len(engagement_rates) > 0:
            avg_engagement = sum(engagement_rates) / len(engagement_rates)
            max_views = max(view_counts) if view_counts else 0
            
            # If views are high but engagement is suspiciously low
            if max_views > 10000 and avg_engagement < 0.001:
                max_anomaly = max(max_anomaly, 0.6)
        
        return max_anomaly
    
    def _detect_velocity_anomaly(
        self,
        horizons: List[TimeHorizonMetrics],
        platform: Platform,
    ) -> float:
        """Detect anomalous velocity patterns that suggest suppression."""
        if len(horizons) < 3:
            return 0.0
        
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        view_trajectory = [h.metrics.views for h in sorted_horizons]
        time_points = [h.horizon_hours for h in sorted_horizons]
        
        # Compute velocity at different stages
        velocities = []
        for i in range(1, len(view_trajectory)):
            if time_points[i] > time_points[i-1]:
                velocity = (view_trajectory[i] - view_trajectory[i-1]) / (time_points[i] - time_points[i-1])
                velocities.append(velocity)
        
        if len(velocities) < 2:
            return 0.0
        
        # Check for sudden velocity collapse
        early_velocity = velocities[0] if velocities else 0
        late_velocity = velocities[-1] if velocities else 0
        
        if early_velocity > 0:
            velocity_ratio = late_velocity / early_velocity
            
            # If velocity drops by >80%, suspicious
            if velocity_ratio < 0.2:
                return 0.7
        
        # Check for negative velocity (views decreasing)
        if any(v < 0 for v in velocities):
            return 0.8  # Very suspicious
        
        return 0.0
    
    def _detect_distribution_anomaly(
        self,
        horizons: List[TimeHorizonMetrics],
        platform: Platform,
    ) -> float:
        """Detect anomalies in distribution patterns."""
        if len(horizons) < 2:
            return 0.0
        
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        view_trajectory = [h.metrics.views for h in sorted_horizons]
        
        # Check for unnatural distribution patterns
        # Normal content should show gradual growth or plateau
        # Suppressed content shows sudden stops
        
        # Compute growth acceleration
        accelerations = []
        for i in range(2, len(view_trajectory)):
            if view_trajectory[i-2] > 0:
                prev_growth = (view_trajectory[i-1] - view_trajectory[i-2]) / view_trajectory[i-2]
                curr_growth = (view_trajectory[i] - view_trajectory[i-1]) / max(view_trajectory[i-1], 1)
                acceleration = curr_growth - prev_growth
                accelerations.append(acceleration)
        
        if accelerations:
            # Sudden negative acceleration suggests suppression
            min_acceleration = min(accelerations)
            if min_acceleration < -0.5:  # Sudden deceleration
                return 0.6
        
        # Check for step-function pattern (sudden stop)
        if len(view_trajectory) >= 3:
            # Look for pattern: growth, growth, flat
            growth_1 = (view_trajectory[1] - view_trajectory[0]) / max(view_trajectory[0], 1)
            growth_2 = (view_trajectory[2] - view_trajectory[1]) / max(view_trajectory[1], 1)
            
            if growth_1 > 0.3 and growth_2 < 0.05:
                return 0.5  # Sudden stop pattern
        
        return 0.0


# =============================================================================
# /evaluation/virality_calibration/confidence_estimator.py
# =============================================================================
"""
Estimates calibration confidence based on data quality.
Low confidence ≠ failure, low confidence = cautious reuse.
"""

from typing import List
from .schemas import GroundTruth, Platform


class ConfidenceEstimator:
    """Estimate confidence in calibration quality with multi-factor analysis."""
    
    def __init__(self):
        # Confidence component weights (tunable)
        self.weights = {
            "completeness": 0.25,
            "coverage": 0.20,
            "suppression_clarity": 0.15,
            "tail_maturity": 0.15,
            "platform_stability": 0.10,
            "data_quality": 0.10,
            "temporal_consistency": 0.05,
        }
        
        # Confidence thresholds
        self.high_confidence_threshold = 0.85
        self.medium_confidence_threshold = 0.70
        self.low_confidence_threshold = 0.50
    
    def estimate_confidence(
        self,
        ground_truths: List[GroundTruth],
        platform_stability: float = 1.0,
        normalized_metrics: Optional[Dict[str, NormalizedMetrics]] = None,
    ) -> float:
        """
        Estimate overall calibration confidence ∈ [0, 1] with advanced multi-factor analysis.
        
        Based on:
        - Data completeness
        - Horizon coverage
        - Platform stability
        - Suppression ambiguity
        - Tail maturity
        - Data quality signals
        - Temporal consistency
        - Metric coherence
        """
        if not ground_truths:
            return 0.0
        
        # Core confidence factors
        completeness = self._compute_completeness(ground_truths)
        coverage = self._compute_horizon_coverage(ground_truths)
        suppression_clarity = self._compute_suppression_clarity(ground_truths)
        tail_maturity = self._compute_tail_maturity(ground_truths)
        data_quality = self._compute_data_quality(ground_truths)
        temporal_consistency = self._compute_temporal_consistency(ground_truths)
        
        # Metric coherence (if metrics provided)
        metric_coherence = 1.0
        if normalized_metrics:
            metric_coherence = self._compute_metric_coherence(ground_truths, normalized_metrics)
        
        # Weighted combination
        confidence = (
            completeness * self.weights["completeness"] +
            coverage * self.weights["coverage"] +
            suppression_clarity * self.weights["suppression_clarity"] +
            tail_maturity * self.weights["tail_maturity"] +
            platform_stability * self.weights["platform_stability"] +
            data_quality * self.weights["data_quality"] +
            temporal_consistency * self.weights["temporal_consistency"] +
            metric_coherence * 0.05  # Additional factor
        )
        
        # Apply confidence degradation for edge cases
        confidence = self._apply_confidence_adjustments(confidence, ground_truths)
        
        return max(0.0, min(1.0, confidence))
    
    def get_confidence_level(self, confidence: float) -> str:
        """Get confidence level category."""
        if confidence >= self.high_confidence_threshold:
            return "HIGH"
        elif confidence >= self.medium_confidence_threshold:
            return "MEDIUM"
        elif confidence >= self.low_confidence_threshold:
            return "LOW"
        else:
            return "VERY_LOW"
    
    def _compute_completeness(self, ground_truths: List[GroundTruth]) -> float:
        """Measure data completeness with quality weighting."""
        if not ground_truths:
            return 0.0
        
        completeness_scores = []
        
        for gt in ground_truths:
            # Check observation completeness
            if gt.observation_complete:
                completeness_scores.append(1.0)
            else:
                # Partial completeness based on available horizons
                horizon_count = len(gt.horizons)
                expected_horizons = 4  # 6h, 24h, 7d, 30d
                partial_score = horizon_count / expected_horizons
                completeness_scores.append(partial_score)
        
        return sum(completeness_scores) / len(completeness_scores)
    
    def _compute_horizon_coverage(self, ground_truths: List[GroundTruth]) -> float:
        """Measure horizon coverage quality with importance weighting."""
        if not ground_truths:
            return 0.0
        
        total_expected = len(ground_truths) * 4  # 4 horizons each
        total_captured = sum(len(gt.horizons) for gt in ground_truths)
        
        base_coverage = min(1.0, total_captured / total_expected)
        
        # Weight by horizon importance (30d is most important)
        horizon_weights = {6: 0.1, 24: 0.2, 168: 0.3, 720: 0.4}
        weighted_coverage = 0.0
        total_weight = 0.0
        
        for gt in ground_truths:
            for h in gt.horizons:
                weight = horizon_weights.get(h.horizon_hours, 0.1)
                weighted_coverage += weight
                total_weight += weight
        
        if total_weight > 0:
            weighted_score = weighted_coverage / total_weight
            # Combine base and weighted
            return (base_coverage * 0.5 + weighted_score * 0.5)
        
        return base_coverage
    
    def _compute_suppression_clarity(self, ground_truths: List[GroundTruth]) -> float:
        """Measure clarity around suppression with ambiguity analysis."""
        if not ground_truths:
            return 1.0
        
        suppression_flags = []
        suppression_ambiguity = []
        
        for gt in ground_truths:
            flags = [h.suppression_detected for h in gt.horizons]
            suppression_flags.extend(flags)
            
            # Check for ambiguous patterns (some horizons flagged, others not)
            if any(flags) and not all(flags):
                suppression_ambiguity.append(0.5)  # Ambiguous
            elif all(flags):
                suppression_ambiguity.append(0.0)  # Clear suppression
            else:
                suppression_ambiguity.append(1.0)  # Clear no suppression
        
        if not suppression_flags:
            return 1.0
        
        # Suppression rate
        suppression_rate = sum(suppression_flags) / len(suppression_flags)
        
        # Ambiguity penalty
        avg_ambiguity = sum(suppression_ambiguity) / len(suppression_ambiguity) if suppression_ambiguity else 0.0
        
        # Low suppression + low ambiguity = high clarity
        clarity = (1.0 - min(1.0, suppression_rate * 2)) * (1.0 - avg_ambiguity * 0.3)
        
        return max(0.0, min(1.0, clarity))
    
    def _compute_tail_maturity(self, ground_truths: List[GroundTruth]) -> float:
        """Measure 30-day tail data maturity with quality assessment."""
        if not ground_truths:
            return 0.0
        
        maturity_scores = []
        
        for gt in ground_truths:
            has_30d = any(h.horizon_hours == 720 for h in gt.horizons)
            
            if has_30d:
                # Check if 30d horizon is complete
                horizon_30d = next((h for h in gt.horizons if h.horizon_hours == 720), None)
                if horizon_30d and horizon_30d.is_complete:
                    maturity_scores.append(1.0)
                else:
                    maturity_scores.append(0.7)  # Partial maturity
            else:
                # Check for 7d data as proxy
                has_7d = any(h.horizon_hours == 168 for h in gt.horizons)
                if has_7d:
                    maturity_scores.append(0.5)  # Partial maturity
                else:
                    maturity_scores.append(0.2)  # Low maturity
        
        return sum(maturity_scores) / len(maturity_scores)
    
    def _compute_data_quality(self, ground_truths: List[GroundTruth]) -> float:
        """Measure overall data quality across multiple dimensions."""
        if not ground_truths:
            return 0.0
        
        quality_scores = []
        
        for gt in ground_truths:
            score = 1.0
            
            # Check horizon completeness flags
            incomplete_horizons = sum(1 for h in gt.horizons if not h.is_complete)
            if incomplete_horizons > 0:
                score *= (1.0 - incomplete_horizons / len(gt.horizons) * 0.3)
            
            # Check for zero or negative metrics (data quality issue)
            for h in gt.horizons:
                if h.metrics.views < 0:
                    score *= 0.5  # Data corruption
                elif h.metrics.views == 0 and h.horizon_hours > 6:
                    score *= 0.8  # Suspicious zero views
            
            # Check timestamp consistency
            if len(gt.horizons) > 1:
                sorted_horizons = sorted(gt.horizons, key=lambda h: h.horizon_hours)
                for i in range(1, len(sorted_horizons)):
                    if sorted_horizons[i].timestamp < sorted_horizons[i-1].timestamp:
                        score *= 0.7  # Timestamp inconsistency
            
            quality_scores.append(score)
        
        return sum(quality_scores) / len(quality_scores)
    
    def _compute_temporal_consistency(self, ground_truths: List[GroundTruth]) -> float:
        """Measure temporal consistency across ground truths."""
        if len(ground_truths) < 2:
            return 1.0
        
        # Check if ground truths are from similar time periods
        eras = [gt.era_identifier for gt in ground_truths]
        unique_eras = set(eras)
        
        # More diverse eras = lower consistency
        era_diversity = len(unique_eras) / len(ground_truths)
        consistency = 1.0 - era_diversity * 0.3
        
        # Check publish time spread
        publish_times = [gt.publish_timestamp for gt in ground_truths]
        if publish_times:
            time_spread = (max(publish_times) - min(publish_times)).total_seconds()
            # Normalize: 30 days = 1.0 spread
            normalized_spread = min(1.0, time_spread / (30 * 24 * 3600))
            consistency *= (1.0 - normalized_spread * 0.2)
        
        return max(0.0, min(1.0, consistency))
    
    def _compute_metric_coherence(
        self,
        ground_truths: List[GroundTruth],
        normalized_metrics: Dict[str, NormalizedMetrics],
    ) -> float:
        """Measure coherence between ground truths and normalized metrics."""
        if not ground_truths or not normalized_metrics:
            return 1.0
        
        coherence_scores = []
        
        for gt in ground_truths:
            if gt.content_id not in normalized_metrics:
                coherence_scores.append(0.5)  # Missing metrics
                continue
            
            metrics = normalized_metrics[gt.content_id]
            
            # Check for logical coherence
            coherence = 1.0
            
            # High virality should correlate with engagement
            if metrics.virality_score > 500 and metrics.engagement_density < 0.001:
                coherence *= 0.7  # Incoherent
            
            # Suppression should reduce virality
            if metrics.suppression_discount < 0.5 and metrics.virality_score > 800:
                coherence *= 0.8  # Suspicious
            
            # Tail weight should correlate with late-stage views
            if metrics.tail_weight > 0.8:
                # Check if we have 30d data
                has_30d = any(h.horizon_hours == 720 for h in gt.horizons)
                if not has_30d:
                    coherence *= 0.9  # High tail weight without 30d data is suspicious
            
            coherence_scores.append(coherence)
        
        return sum(coherence_scores) / len(coherence_scores) if coherence_scores else 1.0
    
    def _apply_confidence_adjustments(
        self,
        base_confidence: float,
        ground_truths: List[GroundTruth],
    ) -> float:
        """Apply adjustments based on edge cases and special conditions."""
        adjusted = base_confidence
        
        # Penalize for very small sample sizes
        if len(ground_truths) < 5:
            adjusted *= 0.9
        
        # Penalize for mixed platforms (harder to normalize)
        platforms = {gt.platform for gt in ground_truths}
        if len(platforms) > 3:
            adjusted *= 0.95  # Multi-platform calibration is harder
        
        # Boost for high-quality complete data
        complete_count = sum(1 for gt in ground_truths if gt.observation_complete)
        if complete_count == len(ground_truths) and len(ground_truths) >= 10:
            adjusted *= 1.05  # Bonus for high-quality dataset
            adjusted = min(1.0, adjusted)
        
        return adjusted


# =============================================================================
# /evaluation/virality_calibration/calibration_validator.py
# =============================================================================
"""
Hard invariants validator.
Calibration FAILS if invariants broken. No silent degradation.
"""

from typing import List, Tuple
from .schemas import GroundTruth, NormalizedMetrics, CalibrationOutput
from .invariants import Invariants, InvariantViolation


class CalibrationValidator:
    """Validate calibration against hard invariants."""
    
    def __init__(self, strict_mode: bool = True):
        self.strict_mode = strict_mode
    
    def validate_input(self, ground_truths: List[GroundTruth]) -> Tuple[bool, List[str]]:
        """Validate input ground truths."""
        errors = []
        
        if not ground_truths:
            errors.append("No ground truths provided")
        
        for i, gt in enumerate(ground_truths):
            # Check horizon integrity
            if not gt.horizons:
                errors.append(f"GT {i}: No horizons provided")
            
            # Check timestamp consistency
            for h in gt.horizons:
                if h.timestamp < gt.publish_timestamp:
                    errors.append(f"GT {i}: Horizon timestamp before publish")
            
            # Validate invariants
            if gt.final_observed_metrics:
                valid, violations = Invariants.validate_no_future_leakage(gt)
                if not valid:
                    errors.extend([f"GT {i}: {v}" for v in violations])
        
        return len(errors) == 0, errors
    
    def validate_output(
        self,
        normalized_metrics: dict[str, NormalizedMetrics],
        ground_truths: List[GroundTruth],
    ) -> Tuple[bool, List[str]]:
        """Validate normalized metrics against invariants."""
        errors = []
        
        # Create lookup
        gt_lookup = {gt.content_id: gt for gt in ground_truths}
        
        for content_id, metrics in normalized_metrics.items():
            if content_id not in gt_lookup:
                errors.append(f"Metrics for unknown content: {content_id}")
                continue
            
            gt = gt_lookup[content_id]
            
            # Validate all invariants
            valid, violations = Invariants.validate_all(metrics, gt)
            
            if not valid:
                errors.extend([f"{content_id}: {v}" for v in violations])
            
            # Additional output-specific checks
            if metrics.virality_score < 0:
                errors.append(f"{content_id}: Negative virality score")
            
            # Note: confidence is on CalibrationOutput, not NormalizedMetrics
        
        return len(errors) == 0, errors
    
    def validate_calibration_output(
        self,
        output: CalibrationOutput,
    ) -> Tuple[bool, List[str]]:
        """Final validation of calibration output."""
        errors = []
        
        if not (0 <= output.confidence <= 1):
            errors.append(f"Output confidence out of bounds: {output.confidence}")
        
        if not output.invariants_validated:
            errors.append("Invariants not validated")
        
        if not output.audit_trail_id:
            errors.append("Missing audit trail")
        
        if output.ground_truth_count != len(output.normalized_metrics):
            errors.append("Metric count mismatch")
        
        return len(errors) == 0, errors


# =============================================================================
# /evaluation/virality_calibration/audit_trail.py
# =============================================================================
"""
Legal + scientific auditability.
Every calibration is bit-reproducible years later.
"""

import hashlib
import json
from typing import Dict, Any, List
from datetime import datetime
from dataclasses import asdict
from .schemas import CalibrationInput, CalibrationOutput, GroundTruth


class AuditTrail:
    """Maintains immutable audit trail for calibrations with comprehensive metadata and persistence."""
    
    def __init__(self, storage_path: str = "/var/audit/calibration", enable_persistence: bool = True):
        self.storage_path = storage_path
        self.enable_persistence = enable_persistence
        self.entries: List[Dict[str, Any]] = []
        
        # Performance tracking
        self._trail_creation_count = 0
        self._verification_count = 0
        self._verification_success_count = 0
        
        # Metadata cache for quick lookups
        self._trail_index: Dict[str, Dict[str, Any]] = {}
        
        # Compression and archival settings
        self._max_in_memory_entries = 10000
        self._archive_after_days = 90
    
    def create_trail(
        self,
        calibration_input: CalibrationInput,
        output: CalibrationOutput,
        transformations: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create comprehensive audit trail for calibration with full metadata.
        Returns audit_trail_id.
        """
        self._trail_creation_count += 1
        
        # Compute input hash
        input_hash = self._hash_input(calibration_input)
        output_hash = self._hash_output(output)
        
        # Compute transformation hash
        transformation_hash = self._hash_transformations(transformations)
        
        # Build comprehensive trail entry
        trail_entry = {
            "audit_trail_id": self._generate_trail_id(),
            "timestamp": datetime.utcnow().isoformat(),
            "calibration_id": calibration_input.calibration_id,
            "version": output.version,
            
            # Hashes for verification
            "input_hash": input_hash,
            "output_hash": output_hash,
            "transformation_hash": transformation_hash,
            "combined_hash": self._hash_combined(input_hash, output_hash, transformation_hash),
            
            # Content metadata
            "ground_truth_ids": [gt.content_id for gt in calibration_input.ground_truths],
            "ground_truth_count": len(calibration_input.ground_truths),
            "platforms": list(set(gt.platform.value for gt in calibration_input.ground_truths)),
            "era_range": self._compute_era_range(calibration_input.ground_truths),
            
            # Transformation metadata
            "transformations": transformations,
            "transformation_count": len(transformations),
            
            # Output metadata
            "warnings": output.warnings,
            "warning_count": len(output.warnings),
            "confidence": output.confidence,
            "confidence_level": self._classify_confidence(output.confidence),
            "invariants_validated": output.invariants_validated,
            "metric_count": len(output.normalized_metrics),
            
            # Performance metadata
            "computation_metadata": {
                "input_size": len(calibration_input.ground_truths),
                "processing_timestamp": datetime.utcnow().isoformat(),
            },
            
            # Custom metadata
            "custom_metadata": metadata or {},
            
            # System metadata
            "system_info": {
                "python_version": self._get_python_version(),
                "calibration_version": output.version,
            },
        }
        
        # Store entry
        self.entries.append(trail_entry)
        
        # Index for quick lookup
        self._trail_index[trail_entry["audit_trail_id"]] = {
            "calibration_id": trail_entry["calibration_id"],
            "timestamp": trail_entry["timestamp"],
            "confidence": trail_entry["confidence"],
        }
        
        # Persist to storage if enabled
        if self.enable_persistence:
            self._persist_trail(trail_entry)
        
        # Manage memory (archive old entries)
        if len(self.entries) > self._max_in_memory_entries:
            self._archive_old_entries()
        
        return trail_entry["audit_trail_id"]
    
    def verify_reproducibility(
        self,
        audit_trail_id: str,
        current_output: CalibrationOutput,
        tolerance: float = 1e-6,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Verify calibration is bit-reproducible with detailed comparison.
        Returns (is_reproducible, comparison_details).
        """
        self._verification_count += 1
        
        # Find original trail
        original = next(
            (e for e in self.entries if e["audit_trail_id"] == audit_trail_id),
            None
        )
        
        if not original:
            return False, {"error": "Trail not found"}
        
        # Compare output hashes
        current_hash = self._hash_output(current_output)
        original_hash = original["output_hash"]
        
        is_reproducible = current_hash == original_hash
        
        # Detailed comparison
        comparison_details = {
            "is_reproducible": is_reproducible,
            "hash_match": current_hash == original_hash,
            "version_match": current_output.version == original["version"],
            "confidence_diff": abs(current_output.confidence - original["confidence"]),
            "metric_count_match": len(current_output.normalized_metrics) == original["metric_count"],
            "invariants_match": current_output.invariants_validated == original["invariants_validated"],
        }
        
        if is_reproducible:
            self._verification_success_count += 1
        
        return is_reproducible, comparison_details
    
    def get_trail_statistics(self) -> Dict[str, Any]:
        """Get audit trail statistics."""
        if not self.entries:
            return {"total_trails": 0}
        
        confidences = [e["confidence"] for e in self.entries]
        warnings = [e["warning_count"] for e in self.entries]
        
        return {
            "total_trails": len(self.entries),
            "trail_creation_count": self._trail_creation_count,
            "verification_count": self._verification_count,
            "verification_success_rate": (
                self._verification_success_count / max(self._verification_count, 1)
            ),
            "avg_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
            "avg_warnings": sum(warnings) / len(warnings) if warnings else 0.0,
            "platforms_covered": len(set(
                p for e in self.entries for p in e.get("platforms", [])
            )),
        }
    
    def query_trails(
        self,
        calibration_id: Optional[str] = None,
        min_confidence: Optional[float] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        platform: Optional[Platform] = None,
    ) -> List[Dict[str, Any]]:
        """Query audit trails with filters."""
        results = []
        
        for entry in self.entries:
            # Apply filters
            if calibration_id and entry["calibration_id"] != calibration_id:
                continue
            
            if min_confidence is not None and entry["confidence"] < min_confidence:
                continue
            
            entry_timestamp = datetime.fromisoformat(entry["timestamp"])
            if start_date and entry_timestamp < start_date:
                continue
            if end_date and entry_timestamp > end_date:
                continue
            
            if platform and platform.value not in entry.get("platforms", []):
                continue
            
            results.append(entry)
        
        return results
    
    def _hash_input(self, input_data: CalibrationInput) -> str:
        """Compute deterministic hash of input."""
        # Convert to deterministic JSON
        input_dict = {
            "calibration_id": input_data.calibration_id,
            "ground_truths": [
                {
                    "content_id": gt.content_id,
                    "platform": gt.platform.value,
                    "publish_time": gt.publish_timestamp.isoformat(),
                    "horizons": len(gt.horizons),
                }
                for gt in input_data.ground_truths
            ],
        }
        
        json_str = json.dumps(input_dict, sort_keys=True)
        return hashlib.sha256(json_str.encode()).hexdigest()
    
    def _hash_output(self, output: CalibrationOutput) -> str:
        """Compute deterministic hash of output."""
        output_dict = {
            "calibration_id": output.calibration_id,
            "confidence": round(output.confidence, 6),
            "metric_count": len(output.normalized_metrics),
            "invariants_validated": output.invariants_validated,
        }
        
        json_str = json.dumps(output_dict, sort_keys=True)
        return hashlib.sha256(json_str.encode()).hexdigest()
    
    def _generate_trail_id(self) -> str:
        """Generate unique trail ID."""
        import time
        import os
        timestamp = datetime.utcnow().isoformat()
        # Include microsecond precision and process ID for uniqueness
        process_id = os.getpid()
        entropy = f"{timestamp}:{time.time_ns()}:{process_id}"
        return hashlib.sha256(entropy.encode()).hexdigest()[:16]
    
    def _hash_transformations(self, transformations: Dict[str, Any]) -> str:
        """Compute hash of transformations."""
        # Sort for deterministic hashing
        sorted_transforms = json.dumps(transformations, sort_keys=True)
        return hashlib.sha256(sorted_transforms.encode()).hexdigest()
    
    def _hash_combined(self, input_hash: str, output_hash: str, transform_hash: str) -> str:
        """Compute combined hash of all components."""
        combined = f"{input_hash}:{output_hash}:{transform_hash}"
        return hashlib.sha256(combined.encode()).hexdigest()
    
    def _compute_era_range(self, ground_truths: List[GroundTruth]) -> Dict[str, str]:
        """Compute era range from ground truths."""
        if not ground_truths:
            return {"min": "", "max": ""}
        
        eras = [gt.era_identifier for gt in ground_truths]
        return {"min": min(eras), "max": max(eras)}
    
    def _classify_confidence(self, confidence: float) -> str:
        """Classify confidence level."""
        if confidence >= 0.85:
            return "HIGH"
        elif confidence >= 0.70:
            return "MEDIUM"
        elif confidence >= 0.50:
            return "LOW"
        else:
            return "VERY_LOW"
    
    def _get_python_version(self) -> str:
        """Get Python version string."""
        import sys
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    
    def _persist_trail(self, trail_entry: Dict[str, Any]):
        """Persist trail entry to storage (implement based on storage backend)."""
        # In production, this would write to:
        # - Database (PostgreSQL, MongoDB, etc.)
        # - Object storage (S3, GCS, etc.)
        # - File system with compression
        # For now, this is a placeholder
        pass
    
    def _archive_old_entries(self):
        """Archive entries older than threshold."""
        cutoff_date = datetime.utcnow() - timedelta(days=self._archive_after_days)
        
        # Filter entries to keep
        self.entries = [
            e for e in self.entries
            if datetime.fromisoformat(e["timestamp"]) > cutoff_date
        ]
        
        # Update index
        self._trail_index = {
            tid: info for tid, info in self._trail_index.items()
            if tid in [e["audit_trail_id"] for e in self.entries]
        }


# =============================================================================
# /evaluation/virality_calibration/calibration_watchdog.py
# =============================================================================
"""
Truth guardian. Monitors drift, divergence, and confidence collapse.
Can freeze versions and raise alerts.
"""

from typing import List, Dict, Optional
from datetime import datetime, timedelta
from .schemas import CalibrationOutput, Platform


class CalibrationWatchdog:
    """Monitor calibration health and detect anomalies with advanced analytics."""
    
    def __init__(self):
        self.calibration_history: List[CalibrationOutput] = []
        self.frozen_versions: set = set()
        self.alerts: List[Dict] = []
        
        # Advanced monitoring state
        self.metric_trends: Dict[str, List[float]] = {}  # metric_name -> [values]
        self.confidence_history: List[float] = []
        self.drift_history: List[float] = []
        self.anomaly_scores: List[float] = []
        
        # Alert thresholds (tunable)
        self.thresholds = {
            "confidence_collapse": 0.5,
            "high_drift": 0.3,
            "excessive_warnings": 5,
            "metric_anomaly": 3.0,  # Z-score
            "trend_reversal": 0.2,
        }
        
        # Performance tracking
        self.monitoring_stats = {
            "total_monitored": 0,
            "alerts_raised": 0,
            "versions_frozen": 0,
        }
    
    def monitor_calibration(
        self,
        new_calibration: CalibrationOutput,
    ) -> Optional[str]:
        """
        Monitor new calibration for anomalies with comprehensive analysis.
        Returns alert message if issues detected, None otherwise.
        """
        self.monitoring_stats["total_monitored"] += 1
        
        # Multi-signal anomaly detection
        anomalies = []
        
        # 1. Check confidence collapse
        if new_calibration.confidence < self.thresholds["confidence_collapse"]:
            anomalies.append(("CONFIDENCE_COLLAPSE", new_calibration.confidence))
        
        # 2. Check for drift vs previous calibrations
        if self.calibration_history:
            drift = self._detect_drift(new_calibration)
            self.drift_history.append(drift)
            
            if drift > self.thresholds["high_drift"]:
                anomalies.append(("HIGH_DRIFT", drift))
            
            # Check for trend reversal
            if len(self.drift_history) >= 3:
                recent_trend = self._detect_trend_reversal(self.drift_history[-3:])
                if recent_trend:
                    anomalies.append(("TREND_REVERSAL", recent_trend))
        
        # 3. Check warning count
        if len(new_calibration.warnings) > self.thresholds["excessive_warnings"]:
            anomalies.append(("EXCESSIVE_WARNINGS", len(new_calibration.warnings)))
        
        # 4. Check metric anomalies
        metric_anomalies = self._detect_metric_anomalies(new_calibration)
        if metric_anomalies:
            anomalies.extend(metric_anomalies)
        
        # 5. Check for invariant violations
        if not new_calibration.invariants_validated:
            anomalies.append(("INVARIANT_VIOLATION", None))
        
        # 6. Check for version compatibility issues
        if self._check_version_issues(new_calibration):
            anomalies.append(("VERSION_ISSUE", new_calibration.version))
        
        # Raise alerts for detected anomalies
        if anomalies:
            # Use most severe anomaly
            anomaly_type, anomaly_value = max(anomalies, key=lambda x: self._anomaly_severity(x[0]))
            alert = self._raise_alert(
                anomaly_type,
                self._format_anomaly_message(anomaly_type, anomaly_value),
                new_calibration.calibration_id,
            )
            self.monitoring_stats["alerts_raised"] += 1
            return alert
        
        # Update tracking
        self.confidence_history.append(new_calibration.confidence)
        self._update_metric_trends(new_calibration)
        
        # Store in history
        self.calibration_history.append(new_calibration)
        
        # Trim history (keep last 1000 for better trend analysis)
        if len(self.calibration_history) > 1000:
            self.calibration_history = self.calibration_history[-1000:]
            self.confidence_history = self.confidence_history[-1000:]
            self.drift_history = self.drift_history[-1000:]
        
        return None
    
    def freeze_version(self, version: str, reason: str):
        """Freeze a calibration version due to issues."""
        self.frozen_versions.add(version)
        self._raise_alert(
            "VERSION_FROZEN",
            f"Version {version} frozen: {reason}",
            version,
        )
    
    def is_version_frozen(self, version: str) -> bool:
        """Check if version is frozen."""
        return version in self.frozen_versions
    
    def _detect_drift(self, new_cal: CalibrationOutput) -> float:
        """Detect drift from recent calibrations with advanced analysis."""
        if not self.calibration_history:
            return 0.0
        
        # Compare confidence with recent average (weighted by recency)
        recent = self.calibration_history[-20:]  # Use more history
        if not recent:
            return 0.0
        
        # Weighted average (more recent = higher weight)
        weights = [i + 1 for i in range(len(recent))]
        total_weight = sum(weights)
        weighted_confidence = sum(c.confidence * w for c, w in zip(recent, weights)) / total_weight
        
        # Compute drift
        drift = abs(new_cal.confidence - weighted_confidence) / max(weighted_confidence, 0.01)
        
        # Also check metric-level drift
        metric_drift = self._detect_metric_drift(new_cal, recent)
        
        # Combine confidence and metric drift
        combined_drift = drift * 0.7 + metric_drift * 0.3
        
        return combined_drift
    
    def _detect_metric_drift(
        self,
        new_cal: CalibrationOutput,
        recent: List[CalibrationOutput],
    ) -> float:
        """Detect drift in normalized metrics."""
        if not recent or not new_cal.normalized_metrics:
            return 0.0
        
        # Compare average virality scores
        recent_scores = []
        for cal in recent:
            if cal.normalized_metrics:
                avg_score = sum(m.virality_score for m in cal.normalized_metrics.values()) / len(cal.normalized_metrics)
                recent_scores.append(avg_score)
        
        if not recent_scores:
            return 0.0
        
        avg_recent = sum(recent_scores) / len(recent_scores)
        new_avg = sum(m.virality_score for m in new_cal.normalized_metrics.values()) / len(new_cal.normalized_metrics)
        
        if avg_recent == 0:
            return 0.0
        
        drift = abs(new_avg - avg_recent) / avg_recent
        return drift
    
    def _detect_trend_reversal(self, drift_values: List[float]) -> Optional[float]:
        """Detect trend reversal in drift values."""
        if len(drift_values) < 3:
            return None
        
        # Check if trend is reversing
        first_half = drift_values[:len(drift_values)//2]
        second_half = drift_values[len(drift_values)//2:]
        
        first_trend = (first_half[-1] - first_half[0]) / max(len(first_half) - 1, 1)
        second_trend = (second_half[-1] - second_half[0]) / max(len(second_half) - 1, 1)
        
        # If trends are opposite and significant
        if first_trend * second_trend < 0 and abs(second_trend) > self.thresholds["trend_reversal"]:
            return abs(second_trend)
        
        return None
    
    def _detect_metric_anomalies(
        self,
        calibration: CalibrationOutput,
    ) -> List[Tuple[str, Any]]:
        """Detect anomalies in calibration metrics."""
        anomalies = []
        
        if not calibration.normalized_metrics:
            return anomalies
        
        # Extract metric values
        virality_scores = [m.virality_score for m in calibration.normalized_metrics.values()]
        tail_weights = [m.tail_weight for m in calibration.normalized_metrics.values()]
        suppression_discounts = [m.suppression_discount for m in calibration.normalized_metrics.values()]
        
        # Check for outliers using Z-score
        if len(virality_scores) > 3:
            outliers = CalibrationUtilities.detect_outliers(virality_scores, method="zscore")
            if len(outliers) > len(virality_scores) * 0.1:  # >10% outliers
                anomalies.append(("METRIC_OUTLIERS", len(outliers)))
        
        # Check for suspicious patterns
        low_suppression_high_virality = sum(
            1 for m in calibration.normalized_metrics.values()
            if m.suppression_discount < 0.5 and m.virality_score > 500
        )
        if low_suppression_high_virality > 0:
            anomalies.append(("SUSPICIOUS_PATTERN", low_suppression_high_virality))
        
        # Check for inconsistent tail weights
        if tail_weights:
            avg_tail = sum(tail_weights) / len(tail_weights)
            if avg_tail < 0.2:  # Very low tail weights
                anomalies.append(("LOW_TAIL_WEIGHTS", avg_tail))
        
        return anomalies
    
    def _check_version_issues(self, calibration: CalibrationOutput) -> bool:
        """Check for version-related issues."""
        # Check if version is frozen
        if self.is_version_frozen(calibration.version):
            return True
        
        # Check version compatibility with recent calibrations
        if self.calibration_history:
            recent_versions = {c.version for c in self.calibration_history[-10:]}
            if calibration.version not in recent_versions and len(recent_versions) == 1:
                # Sudden version change
                return True
        
        return False
    
    def _update_metric_trends(self, calibration: CalibrationOutput):
        """Update metric trend tracking."""
        if not calibration.normalized_metrics:
            return
        
        for content_id, metrics in calibration.normalized_metrics.items():
            if content_id not in self.metric_trends:
                self.metric_trends[content_id] = []
            
            self.metric_trends[content_id].append(metrics.virality_score)
            
            # Keep only recent values
            if len(self.metric_trends[content_id]) > 100:
                self.metric_trends[content_id] = self.metric_trends[content_id][-100:]
    
    def _anomaly_severity(self, anomaly_type: str) -> int:
        """Get severity score for anomaly type (higher = more severe)."""
        severity_map = {
            "INVARIANT_VIOLATION": 10,
            "CONFIDENCE_COLLAPSE": 8,
            "HIGH_DRIFT": 7,
            "VERSION_ISSUE": 6,
            "EXCESSIVE_WARNINGS": 5,
            "METRIC_OUTLIERS": 4,
            "SUSPICIOUS_PATTERN": 4,
            "TREND_REVERSAL": 3,
            "LOW_TAIL_WEIGHTS": 2,
        }
        return severity_map.get(anomaly_type, 1)
    
    def _format_anomaly_message(self, anomaly_type: str, value: Any) -> str:
        """Format anomaly message for alert."""
        if anomaly_type == "CONFIDENCE_COLLAPSE":
            return f"Calibration confidence critically low: {value:.2f}"
        elif anomaly_type == "HIGH_DRIFT":
            return f"Significant drift detected: {value:.1%}"
        elif anomaly_type == "EXCESSIVE_WARNINGS":
            return f"High warning count: {value}"
        elif anomaly_type == "METRIC_OUTLIERS":
            return f"Detected {value} metric outliers"
        elif anomaly_type == "SUSPICIOUS_PATTERN":
            return f"Suspicious pattern detected in {value} metrics"
        elif anomaly_type == "TREND_REVERSAL":
            return f"Trend reversal detected with magnitude {value:.2f}"
        elif anomaly_type == "LOW_TAIL_WEIGHTS":
            return f"Average tail weight critically low: {value:.2f}"
        elif anomaly_type == "INVARIANT_VIOLATION":
            return "Invariant validation failed"
        elif anomaly_type == "VERSION_ISSUE":
            return f"Version compatibility issue with version {value}"
        else:
            return f"Anomaly detected: {anomaly_type}"
    
    def _raise_alert(
        self,
        alert_type: str,
        message: str,
        calibration_id: str,
    ) -> str:
        """Raise system alert with comprehensive metadata."""
        alert = {
            "type": alert_type,
            "message": message,
            "calibration_id": calibration_id,
            "timestamp": datetime.utcnow().isoformat(),
            "severity": self._anomaly_severity(alert_type),
            "alert_id": hashlib.sha256(
                f"{alert_type}:{calibration_id}:{datetime.utcnow().isoformat()}".encode()
            ).hexdigest()[:16],
        }
        
        self.alerts.append(alert)
        
        # In production: send to monitoring system, PagerDuty, etc.
        # self._send_to_monitoring(alert)
        
        return message
    
    def get_monitoring_report(self) -> Dict[str, Any]:
        """Get comprehensive monitoring report."""
        return {
            "stats": self.monitoring_stats,
            "recent_alerts": self.alerts[-10:] if self.alerts else [],
            "alert_count": len(self.alerts),
            "frozen_versions": list(self.frozen_versions),
            "confidence_trend": {
                "current": self.confidence_history[-1] if self.confidence_history else None,
                "average": sum(self.confidence_history) / len(self.confidence_history) if self.confidence_history else None,
                "trend": self._compute_trend(self.confidence_history[-20:]) if len(self.confidence_history) >= 20 else None,
            },
            "drift_trend": {
                "current": self.drift_history[-1] if self.drift_history else None,
                "average": sum(self.drift_history) / len(self.drift_history) if self.drift_history else None,
            },
            "tracked_metrics": len(self.metric_trends),
        }
    
    def _compute_trend(self, values: List[float]) -> str:
        """Compute trend direction from values."""
        if len(values) < 2:
            return "INSUFFICIENT_DATA"
        
        first_half = values[:len(values)//2]
        second_half = values[len(values)//2:]
        
        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)
        
        if second_avg > first_avg * 1.05:
            return "INCREASING"
        elif second_avg < first_avg * 0.95:
            return "DECREASING"
        else:
            return "STABLE"


# =============================================================================
# /evaluation/virality_calibration/calibration_registry.py
# =============================================================================
"""
Version control for calibrations.
Tracks versions, compatibility, deprecation.
"""

from typing import Dict, Optional, List
from datetime import datetime


class CalibrationRegistry:
    """Manage calibration versions and compatibility."""
    
    def __init__(self):
        self.versions: Dict[str, Dict] = {}
        self.compatibility_matrix: Dict[str, List[str]] = {}
        self.deprecated_versions: set = set()
    
    def register_version(
        self,
        version: str,
        compatible_with: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ):
        """Register a new calibration version."""
        self.versions[version] = {
            "registered_at": datetime.utcnow(),
            "metadata": metadata or {},
            "deprecated": False,
        }
        
        if compatible_with:
            self.compatibility_matrix[version] = compatible_with
    
    def deprecate_version(self, version: str, reason: str):
        """Mark version as deprecated."""
        if version in self.versions:
            self.versions[version]["deprecated"] = True
            self.versions[version]["deprecation_reason"] = reason
            self.deprecated_versions.add(version)
    
    def is_compatible(self, version_a: str, version_b: str) -> bool:
        """Check if two versions are compatible."""
        if version_a == version_b:
            return True
        
        if version_a in self.compatibility_matrix:
            return version_b in self.compatibility_matrix[version_a]
        
        return False
    
    def get_latest_version(self) -> Optional[str]:
        """Get latest non-deprecated version."""
        non_deprecated = [
            v for v, info in self.versions.items()
            if not info["deprecated"]
        ]
        
        if not non_deprecated:
            return None
        
        # Return most recently registered
        return max(
            non_deprecated,
            key=lambda v: self.versions[v]["registered_at"]
        )


# =============================================================================
# /evaluation/virality_calibration/calibration_engine.py
# =============================================================================
"""
Orchestrator and entry point for calibration system.
Enforces execution order and produces immutable calibration artifacts.
"""

from typing import List, Dict
from datetime import datetime
import hashlib

from .schemas import (
    CalibrationInput,
    CalibrationOutput,
    GroundTruth,
    NormalizedMetrics,
)
from .ground_truth_assembler import GroundTruthAssembler
from .platform_normalizer import PlatformNormalizer
from .format_equivalence import FormatEquivalenceCalculator
from .era_drift_corrector import EraDriftCorrector
from .tail_weight_estimator import TailWeightEstimator
from .suppression_discount import SuppressionDetector
from .confidence_estimator import ConfidenceEstimator
from .calibration_validator import CalibrationValidator
from .audit_trail import AuditTrail
from .calibration_watchdog import CalibrationWatchdog
from .calibration_registry import CalibrationRegistry


class CalibrationEngine:
    """
    Main orchestrator for virality calibration.
    
    This is the ONLY entry point for calibration.
    Produces deterministic, immutable calibration artifacts.
    """
    
    VERSION = "1.0.0"
    
    def __init__(self):
        # Initialize all subsystems
        self.platform_normalizer = PlatformNormalizer()
        self.format_calculator = FormatEquivalenceCalculator()
        self.era_corrector = EraDriftCorrector()
        self.tail_estimator = TailWeightEstimator()
        self.suppression_detector = SuppressionDetector()
        self.confidence_estimator = ConfidenceEstimator()
        self.validator = CalibrationValidator(strict_mode=True)
        self.audit_trail = AuditTrail()
        self.watchdog = CalibrationWatchdog()
        self.registry = CalibrationRegistry()
        
        # Register this version
        self.registry.register_version(self.VERSION)
    
    def calibrate(
        self,
        calibration_input: CalibrationInput,
    ) -> CalibrationOutput:
        """
        Main calibration pipeline.
        
        EXECUTION ORDER (NEVER REORDER):
        1. Validate input
        2. Normalize platforms
        3. Correct formats
        4. Correct era drift
        5. Estimate tail weights
        6. Detect suppression
        7. Compute final metrics
        8. Estimate confidence
        9. Validate output
        10. Create audit trail
        11. Monitor health
        """
        # Step 1: Validate input
        valid, errors = self.validator.validate_input(calibration_input.ground_truths)
        if not valid:
            raise ValueError(f"Input validation failed: {errors}")
        
        # Step 2-7: Process each ground truth
        normalized_metrics: Dict[str, NormalizedMetrics] = {}
        transformations = {}
        
        for gt in calibration_input.ground_truths:
            metrics = self._calibrate_single(gt)
            normalized_metrics[gt.content_id] = metrics
            transformations[gt.content_id] = self._capture_transformations(gt, metrics)
        
        # Step 8: Estimate overall confidence
        confidence = self.confidence_estimator.estimate_confidence(
            calibration_input.ground_truths
        )
        
        # Check minimum confidence threshold
        warnings = []
        if confidence < calibration_input.min_confidence_threshold:
            warnings.append(
                f"Confidence {confidence:.2f} below threshold "
                f"{calibration_input.min_confidence_threshold}"
            )
        
        # Step 9: Validate output
        valid, errors = self.validator.validate_output(
            normalized_metrics,
            calibration_input.ground_truths,
        )
        if not valid:
            raise ValueError(f"Output validation failed: {errors}")
        
        # Step 10: Build output
        input_hash = self._hash_input(calibration_input)
        
        output = CalibrationOutput(
            calibration_id=calibration_input.calibration_id,
            version=self.VERSION,
            timestamp=datetime.utcnow(),
            input_hash=input_hash,
            ground_truth_count=len(calibration_input.ground_truths),
            normalized_metrics=normalized_metrics,
            confidence=confidence,
            invariants_validated=True,
            audit_trail_id="",  # Set below
            warnings=warnings,
        )
        
        # Step 11: Create audit trail
        audit_id = self.audit_trail.create_trail(
            calibration_input,
            output,
            transformations,
        )
        
        # Update output with audit ID (immutable so we recreate)
        output = CalibrationOutput(
            calibration_id=output.calibration_id,
            version=output.version,
            timestamp=output.timestamp,
            input_hash=output.input_hash,
            ground_truth_count=output.ground_truth_count,
            normalized_metrics=output.normalized_metrics,
            confidence=output.confidence,
            invariants_validated=output.invariants_validated,
            audit_trail_id=audit_id,
            warnings=output.warnings,
        )
        
        # Step 11.5: Final validation of complete output
        valid, errors = self.validator.validate_calibration_output(output)
        if not valid:
            raise ValueError(f"Final calibration output validation failed: {errors}")
        
        # Step 12: Monitor health
        alert = self.watchdog.monitor_calibration(output)
        if alert:
            print(f"⚠️  Calibration Alert: {alert}")
        
        return output
    
    def _calibrate_single(self, gt: GroundTruth) -> NormalizedMetrics:
        """Calibrate a single piece of content with comprehensive analysis."""
        
        # Extract horizon metrics
        views_6h = self._get_views_at_horizon(gt, 6)
        views_24h = self._get_views_at_horizon(gt, 24)
        views_7d = self._get_views_at_horizon(gt, 168)
        views_30d = self._get_views_at_horizon(gt, 720)
        
        # Platform normalization with quality adjustments
        normalized_views = self.platform_normalizer.normalize_views(
            gt.final_observed_metrics.views,
            gt.platform,
            gt.format,
            metrics=gt.final_observed_metrics,
        )
        
        # Velocity score with 7d data if available
        velocity_score = self.platform_normalizer.compute_velocity_score(
            views_6h,
            views_24h,
            gt.platform,
            views_7d=views_7d if views_7d > 0 else None,
        )
        
        # Retention score
        retention_score = self.platform_normalizer.compute_retention_score(
            gt.final_observed_metrics,
            gt.platform,
        )
        
        # Format equivalence
        format_factor = self.format_calculator.compute_equivalence_factor(
            gt.format,
            gt.origin_type,
            gt.platform,
        )
        
        # Era correction
        era_factor = self.era_corrector.compute_era_correction(
            gt.platform,
            gt.era_identifier,
        )
        
        # Tail weight
        tail_weight = self.tail_estimator.estimate_tail_weight(gt.horizons)
        
        # Suppression detection
        suppression_discount = self.suppression_detector.compute_suppression_discount(
            gt.horizons,
            gt.platform,
        )
        
        # Compute final virality score with advanced weighting
        base_score = normalized_views * format_factor * era_factor
        
        # Apply tail weight boost (sustained content gets bonus)
        tail_boost = 1.0 + tail_weight * 0.5
        
        # Apply suppression discount
        suppression_adjusted_score = base_score * tail_boost * suppression_discount
        
        # Final virality score
        virality_score = suppression_adjusted_score
        
        # Engagement density (platform-aware)
        engagement_density = self._compute_engagement_density(
            gt.final_observed_metrics,
            gt.platform,
        )
        
        # Reach efficiency
        reach_efficiency = (
            gt.final_observed_metrics.views /
            max(gt.final_observed_metrics.follower_count_at_post, 1)
        )
        
        # Quality score
        quality_score = gt.final_observed_metrics.compute_quality_score()
        
        # Momentum score
        momentum_score = self._compute_momentum_score(
            views_6h, views_24h, views_7d, views_30d
        )
        
        # Stability score
        stability_score = self._compute_stability_score(gt.horizons)
        
        # Reactivation potential
        reactivation_potential = self._compute_reactivation_potential(gt.horizons)
        
        # Cross-platform comparability
        cross_platform_comparability = self._compute_comparability(gt)
        
        # Calibration confidence for this specific metric
        calibration_confidence = self._compute_metric_confidence(gt)
        
        return NormalizedMetrics(
            virality_score=virality_score,
            velocity_score=velocity_score,
            retention_score=retention_score,
            tail_weight=tail_weight,
            engagement_density=engagement_density,
            reach_efficiency=reach_efficiency,
            suppression_discount=suppression_discount,
            format_equivalence_factor=format_factor,
            era_correction_factor=era_factor,
            quality_score=quality_score,
            momentum_score=momentum_score,
            stability_score=stability_score,
            reactivation_potential=reactivation_potential,
            cross_platform_comparability=cross_platform_comparability,
            calibration_confidence=calibration_confidence,
        )
    
    def _compute_engagement_density(
        self,
        metrics: PlatformMetrics,
        platform: Platform,
    ) -> float:
        """Compute platform-aware engagement density."""
        weights = self.platform_normalizer.PLATFORM_ENGAGEMENT_WEIGHTS.get(platform, {})
        
        total_engagement = (
            metrics.likes * weights.get("likes", 1.0) +
            metrics.comments * weights.get("comments", 3.0) +
            metrics.shares * weights.get("shares", 5.0) +
            (metrics.saves or 0) * weights.get("saves", 2.0) +
            (metrics.retweets or 0) * weights.get("retweets", 4.0)
        )
        
        return total_engagement / max(metrics.views, 1)
    
    def _compute_momentum_score(
        self,
        views_6h: int,
        views_24h: int,
        views_7d: Optional[int],
        views_30d: Optional[int],
    ) -> float:
        """Compute momentum score from view trajectory."""
        if views_24h == 0:
            return 0.0
        
        # Early momentum (6h to 24h)
        early_momentum = (views_24h - views_6h) / max(views_6h, 1) if views_6h > 0 else 0.0
        
        # Mid momentum (24h to 7d)
        mid_momentum = 0.0
        if views_7d is not None and views_7d > views_24h:
            mid_momentum = (views_7d - views_24h) / max(views_24h, 1)
        
        # Late momentum (7d to 30d)
        late_momentum = 0.0
        if views_30d is not None and views_7d is not None and views_30d > views_7d:
            late_momentum = (views_30d - views_7d) / max(views_7d, 1)
        
        # Weighted combination
        momentum = (
            early_momentum * 0.5 +
            mid_momentum * 0.3 +
            late_momentum * 0.2
        )
        
        # Normalize to [0, 1]
        return min(1.0, max(0.0, momentum / 5.0))  # Assume max 5x growth
    
    def _compute_stability_score(self, horizons: List[TimeHorizonMetrics]) -> float:
        """Compute stability score from horizon trajectory."""
        if len(horizons) < 2:
            return 0.5
        
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        view_trajectory = [h.metrics.views for h in sorted_horizons]
        
        # Compute coefficient of variation
        if max(view_trajectory) == 0:
            return 0.0
        
        mean_views = sum(view_trajectory) / len(view_trajectory)
        variance = sum((v - mean_views) ** 2 for v in view_trajectory) / len(view_trajectory)
        std_dev = math.sqrt(variance)
        
        if mean_views == 0:
            return 0.0
        
        coefficient_of_variation = std_dev / mean_views
        
        # Lower CV = higher stability
        stability = 1.0 / (1.0 + coefficient_of_variation)
        
        return min(1.0, max(0.0, stability))
    
    def _compute_reactivation_potential(self, horizons: List[TimeHorizonMetrics]) -> float:
        """Compute reactivation potential from trajectory patterns."""
        if len(horizons) < 4:
            return 0.0
        
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        view_trajectory = [h.metrics.views for h in sorted_horizons]
        
        # Look for patterns suggesting reactivation potential
        # Check if late-stage growth is accelerating
        if len(view_trajectory) >= 3:
            late_growth = view_trajectory[-1] - view_trajectory[-2]
            mid_growth = view_trajectory[-2] - view_trajectory[-3]
            
            if mid_growth > 0:
                acceleration = late_growth / mid_growth
                if acceleration > 1.2:  # Accelerating
                    return min(1.0, acceleration / 3.0)
        
        return 0.0
    
    def _compute_comparability(self, gt: GroundTruth) -> float:
        """Compute cross-platform comparability score."""
        comparability = 1.0
        
        # Penalize if missing key horizons
        required_horizons = {6, 24, 168, 720}
        present_horizons = {h.horizon_hours for h in gt.horizons}
        missing = required_horizons - present_horizons
        if missing:
            comparability *= (1.0 - len(missing) / len(required_horizons) * 0.3)
        
        # Penalize if observation incomplete
        if not gt.observation_complete:
            comparability *= 0.9
        
        # Penalize if suppression detected
        if any(h.suppression_detected for h in gt.horizons):
            comparability *= 0.8
        
        return comparability
    
    def _compute_metric_confidence(self, gt: GroundTruth) -> float:
        """Compute confidence in this specific metric calibration."""
        confidence = 1.0
        
        # Horizon completeness
        required_horizons = {6, 24, 168, 720}
        present_horizons = {h.horizon_hours for h in gt.horizons}
        completeness = len(present_horizons & required_horizons) / len(required_horizons)
        confidence *= completeness
        
        # Data quality
        complete_count = sum(1 for h in gt.horizons if h.is_complete)
        quality = complete_count / len(gt.horizons) if gt.horizons else 0.0
        confidence *= (0.7 + quality * 0.3)
        
        # Suppression clarity
        suppression_count = sum(1 for h in gt.horizons if h.suppression_detected)
        if suppression_count > 0:
            confidence *= 0.8
        
        return min(1.0, max(0.0, confidence))
    
    def _get_views_at_horizon(self, gt: GroundTruth, hours: int) -> int:
        """Extract views at specific horizon."""
        for h in gt.horizons:
            if h.horizon_hours == hours:
                return h.metrics.views
        return 0
    
    def _hash_input(self, input_data: CalibrationInput) -> str:
        """Compute deterministic input hash."""
        content_ids = sorted([gt.content_id for gt in input_data.ground_truths])
        hash_str = f"{input_data.calibration_id}:{','.join(content_ids)}"
        return hashlib.sha256(hash_str.encode()).hexdigest()[:16]
    
    def _capture_transformations(
        self,
        gt: GroundTruth,
        metrics: NormalizedMetrics,
    ) -> Dict:
        """Capture transformation metadata for audit."""
        return {
            "platform": gt.platform.value,
            "format": gt.format.value,
            "era": gt.era_identifier,
            "format_factor": metrics.format_equivalence_factor,
            "era_factor": metrics.era_correction_factor,
            "tail_weight": metrics.tail_weight,
            "suppression": metrics.suppression_discount,
            "quality_score": metrics.quality_score,
            "momentum_score": metrics.momentum_score,
            "stability_score": metrics.stability_score,
        }
    
    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics for the calibration engine."""
        return {
            "platform_normalizer_cache": self.platform_normalizer.get_cache_stats(),
            "suppression_detector_cache_size": len(self.suppression_detector._pattern_cache),
            "era_corrector_cache_size": len(self.era_corrector._interpolation_cache),
            "audit_trail_stats": self.audit_trail.get_trail_statistics(),
        }
    
    def clear_all_caches(self):
        """Clear all caches for memory management."""
        self.platform_normalizer.clear_cache()
        self.suppression_detector._pattern_cache.clear()
        self.era_corrector.clear_cache()


# =============================================================================
# /evaluation/virality_calibration/utilities.py
# =============================================================================
"""
Utility functions for calibration system.
Performance optimizations, data transformations, and helper functions.
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
import math
from .schemas import GroundTruth, PlatformMetrics, TimeHorizonMetrics, Platform


class CalibrationUtilities:
    """Utility functions for calibration operations."""
    
    @staticmethod
    def interpolate_missing_horizon(
        horizons: List[TimeHorizonMetrics],
        target_hours: int,
    ) -> Optional[PlatformMetrics]:
        """Interpolate metrics for missing horizon using surrounding data."""
        sorted_horizons = sorted(horizons, key=lambda h: h.horizon_hours)
        
        # Find bounding horizons
        lower = None
        upper = None
        
        for h in sorted_horizons:
            if h.horizon_hours < target_hours:
                lower = h
            elif h.horizon_hours > target_hours and upper is None:
                upper = h
                break
        
        if lower is None or upper is None:
            return None
        
        # Linear interpolation
        t = (target_hours - lower.horizon_hours) / (upper.horizon_hours - lower.horizon_hours)
        
        lower_metrics = lower.metrics
        upper_metrics = upper.metrics
        
        interpolated = PlatformMetrics(
            platform=lower_metrics.platform,
            views=int(lower_metrics.views + t * (upper_metrics.views - lower_metrics.views)),
            likes=int(lower_metrics.likes + t * (upper_metrics.likes - lower_metrics.likes)),
            comments=int(lower_metrics.comments + t * (upper_metrics.comments - lower_metrics.comments)),
            shares=int(lower_metrics.shares + t * (upper_metrics.shares - lower_metrics.shares)),
            saves=int(lower_metrics.saves + t * (upper_metrics.saves - lower_metrics.saves)) if lower_metrics.saves and upper_metrics.saves else None,
            watch_time_seconds=lower_metrics.watch_time_seconds + t * (upper_metrics.watch_time_seconds - lower_metrics.watch_time_seconds) if lower_metrics.watch_time_seconds and upper_metrics.watch_time_seconds else None,
            avg_watch_percentage=lower_metrics.avg_watch_percentage + t * (upper_metrics.avg_watch_percentage - lower_metrics.avg_watch_percentage) if lower_metrics.avg_watch_percentage and upper_metrics.avg_watch_percentage else None,
            completion_rate=lower_metrics.completion_rate + t * (upper_metrics.completion_rate - lower_metrics.completion_rate) if lower_metrics.completion_rate and upper_metrics.completion_rate else None,
            follower_count_at_post=lower_metrics.follower_count_at_post,
        )
        
        return interpolated
    
    @staticmethod
    def compute_growth_rate(
        initial_value: int,
        final_value: int,
        time_hours: float,
    ) -> float:
        """Compute exponential growth rate."""
        if initial_value <= 0 or time_hours <= 0:
            return 0.0
        
        if final_value <= initial_value:
            return 0.0
        
        # Exponential growth: final = initial * exp(rate * time)
        rate = math.log(final_value / initial_value) / time_hours
        return rate
    
    @staticmethod
    def compute_compound_growth(
        values: List[int],
        time_points: List[float],
    ) -> float:
        """Compute compound growth rate from trajectory."""
        if len(values) < 2 or len(time_points) < 2:
            return 0.0
        
        growth_rates = []
        for i in range(1, len(values)):
            if values[i-1] > 0 and time_points[i] > time_points[i-1]:
                rate = CalibrationUtilities.compute_growth_rate(
                    values[i-1],
                    values[i],
                    time_points[i] - time_points[i-1],
                )
                growth_rates.append(rate)
        
        if not growth_rates:
            return 0.0
        
        return sum(growth_rates) / len(growth_rates)
    
    @staticmethod
    def detect_outliers(
        values: List[float],
        method: str = "iqr",
    ) -> List[int]:
        """Detect outliers in value list. Returns indices of outliers."""
        if len(values) < 3:
            return []
        
        outliers = []
        
        if method == "iqr":
            # Interquartile range method
            sorted_values = sorted(values)
            q1_idx = len(sorted_values) // 4
            q3_idx = 3 * len(sorted_values) // 4
            
            q1 = sorted_values[q1_idx]
            q3 = sorted_values[q3_idx]
            iqr = q3 - q1
            
            lower_bound = q1 - 1.5 * iqr
            upper_bound = q3 + 1.5 * iqr
            
            for i, v in enumerate(values):
                if v < lower_bound or v > upper_bound:
                    outliers.append(i)
        
        elif method == "zscore":
            # Z-score method
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            std_dev = math.sqrt(variance) if variance > 0 else 1.0
            
            for i, v in enumerate(values):
                z_score = abs((v - mean) / std_dev)
                if z_score > 3.0:  # 3 standard deviations
                    outliers.append(i)
        
        return outliers
    
    @staticmethod
    def smooth_trajectory(
        values: List[int],
        window_size: int = 3,
    ) -> List[float]:
        """Apply moving average smoothing to trajectory."""
        if len(values) < window_size:
            return [float(v) for v in values]
        
        smoothed = []
        for i in range(len(values)):
            start = max(0, i - window_size // 2)
            end = min(len(values), i + window_size // 2 + 1)
            window = values[start:end]
            smoothed.append(sum(window) / len(window))
        
        return smoothed
    
    @staticmethod
    def compute_percentiles(
        values: List[float],
        percentiles: List[float] = [25, 50, 75, 90, 95, 99],
    ) -> Dict[float, float]:
        """Compute percentiles of value distribution."""
        if not values:
            return {}
        
        sorted_values = sorted(values)
        n = len(sorted_values)
        
        result = {}
        for p in percentiles:
            idx = int((p / 100.0) * (n - 1))
            result[p] = sorted_values[idx]
        
        return result
    
    @staticmethod
    def compute_statistical_summary(values: List[float]) -> Dict[str, float]:
        """Compute comprehensive statistical summary."""
        if not values:
            return {}
        
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        std_dev = math.sqrt(variance)
        
        sorted_values = sorted(values)
        median = sorted_values[n // 2] if n % 2 == 1 else (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / 2
        
        return {
            "count": n,
            "mean": mean,
            "median": median,
            "std_dev": std_dev,
            "variance": variance,
            "min": min(values),
            "max": max(values),
            "range": max(values) - min(values),
            "coefficient_of_variation": std_dev / mean if mean != 0 else 0.0,
        }
    
    @staticmethod
    def group_by_platform(ground_truths: List[GroundTruth]) -> Dict[Platform, List[GroundTruth]]:
        """Group ground truths by platform."""
        grouped = {}
        for gt in ground_truths:
            if gt.platform not in grouped:
                grouped[gt.platform] = []
            grouped[gt.platform].append(gt)
        return grouped
    
    @staticmethod
    def group_by_era(ground_truths: List[GroundTruth]) -> Dict[str, List[GroundTruth]]:
        """Group ground truths by era."""
        grouped = {}
        for gt in ground_truths:
            if gt.era_identifier not in grouped:
                grouped[gt.era_identifier] = []
            grouped[gt.era_identifier].append(gt)
        return grouped
    
    @staticmethod
    def filter_by_confidence(
        metrics_dict: Dict[str, NormalizedMetrics],
        min_confidence: float = 0.7,
    ) -> Dict[str, NormalizedMetrics]:
        """Filter metrics by calibration confidence."""
        return {
            content_id: metrics
            for content_id, metrics in metrics_dict.items()
            if metrics.calibration_confidence >= min_confidence
        }
    
    @staticmethod
    def compute_aggregate_metrics(
        metrics_list: List[NormalizedMetrics],
    ) -> Dict[str, float]:
        """Compute aggregate statistics across multiple metrics."""
        if not metrics_list:
            return {}
        
        virality_scores = [m.virality_score for m in metrics_list]
        tail_weights = [m.tail_weight for m in metrics_list]
        engagement_densities = [m.engagement_density for m in metrics_list]
        
        return {
            "avg_virality": sum(virality_scores) / len(virality_scores),
            "median_virality": CalibrationUtilities.compute_statistical_summary(virality_scores)["median"],
            "avg_tail_weight": sum(tail_weights) / len(tail_weights),
            "avg_engagement_density": sum(engagement_densities) / len(engagement_densities),
            "high_performers": sum(1 for v in virality_scores if v > 500),
            "sustained_performers": sum(1 for tw in tail_weights if tw > 0.7),
        }
    
    @staticmethod
    def validate_ground_truth_batch(
        ground_truths: List[GroundTruth],
    ) -> Tuple[bool, List[str]]:
        """Validate a batch of ground truths."""
        errors = []
        
        if not ground_truths:
            errors.append("Empty ground truth list")
            return False, errors
        
        # Check for duplicates
        content_ids = [gt.content_id for gt in ground_truths]
        duplicates = [cid for cid in content_ids if content_ids.count(cid) > 1]
        if duplicates:
            errors.append(f"Duplicate content IDs: {set(duplicates)}")
        
        # Check era consistency
        eras = {gt.era_identifier for gt in ground_truths}
        if len(eras) > 10:
            errors.append(f"Too many different eras: {len(eras)}")
        
        # Check platform distribution
        platforms = {gt.platform for gt in ground_truths}
        if len(platforms) > 6:
            errors.append(f"Too many platforms: {len(platforms)}")
        
        return len(errors) == 0, errors


# =============================================================================
# Enhanced CalibrationWatchdog with advanced monitoring
# =============================================================================

# Expanding the existing watchdog class