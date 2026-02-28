"""
viral_score.py - Canonical Virality Scoring Engine (Ground-Truth Layer)

PRODUCTION-GRADE SPECIFICATION COMPLIANCE:

Computes the single authoritative virality score for content at a given time horizon.
Deterministic, auditable, horizon-aware scalar projection of real virality.

NOT a predictor. NOT a ranking model. Only observed outcomes.

ABSOLUTE ARCHITECTURAL POSITION:
evaluation/
├── metrics.py
├── early_signal_detector.py
├── virality_predictor.py        (forecasting)
├── validation_pipeline.py
└── viral_score.py               ← YOU ARE HERE

Everything downstream trusts this output.

CORE RESPONSIBILITY (LOCK THIS):
viral_score.py answers exactly one question:
"Given all observed signals so far, how viral is this content, in a way that 
is comparable, reproducible, and reward-safe?"

DESIGN PRINCIPLES (Non-Negotiable):
- Observed > Predicted
- Time-normalized, not time-biased
- Platform-aware but platform-agnostic
- Uncertainty explicit
- Composable, not opaque
- Reward-safe for RL

LOC: ~3,100-4,300 (Production-grade requirement)
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, List, Protocol, Any, Callable, Set
from enum import Enum
from abc import ABC, abstractmethod
import math
import json
import time
import logging
from functools import lru_cache
from collections import defaultdict
import hashlib

logger = logging.getLogger(__name__)

# ============================================================================
# PROTOCOL DEFINITIONS (Interface Contracts)
# ============================================================================

class MetricsProviderProtocol(Protocol):
    """Protocol for metrics data providers (read-only)."""
    
    def get_observed_metrics(self, content_id: str, platform: str) -> Optional[Dict[str, Any]]:
        """Retrieve observed metrics for content. Returns None if not available."""
        ...
    
    def get_suppression_signals(self, content_id: str, platform: str) -> Optional[Dict[str, Any]]:
        """Retrieve suppression signals for content. Returns None if not available."""
        ...
    
    def get_platform_baselines(self, platform: str) -> Optional[Dict[str, float]]:
        """Retrieve platform-specific baselines. Returns None if not available."""
        ...


class PostingStateInterface(Protocol):
    """Protocol for posting state store (read-only)."""
    
    def get_content_state(self, content_id: str) -> Optional[Dict[str, Any]]:
        """Get current state for content. Returns state dict or None."""
        ...
    
    def get_suppression_level(self, platform: str, account_id: str) -> Optional[str]:
        """Get suppression level for account. Returns 'NONE', 'LOW', 'MEDIUM', 'HIGH', or None."""
        ...


# ============================================================================
# TYPE DEFINITIONS
# ============================================================================

class Platform(Enum):
    """Supported platforms with distinct distribution dynamics."""
    TIKTOK = "tiktok"
    YOUTUBE_SHORTS = "youtube_shorts"
    INSTAGRAM_REELS = "instagram_reels"
    TWITTER = "twitter"
    LINKEDIN = "linkedin"
    
    @classmethod
    def from_string(cls, platform_str: str) -> 'Platform':
        """Convert string to Platform enum with normalization."""
        normalized = platform_str.lower().strip().replace(" ", "_")
        for platform in cls:
            if platform.value == normalized:
                return platform
        raise ValueError(f"Unknown platform: {platform_str}")


class SuppressionType(Enum):
    """Types of content suppression patterns."""
    SHADOW_BAN = "shadow_ban"
    THROTTLED = "throttled"
    POLICY_FRICTION = "policy_friction"
    ACCOUNT_DAMPENING = "account_dampening"
    NONE = "none"
    
    @classmethod
    def from_string(cls, suppression_str: str) -> 'SuppressionType':
        """Convert string to SuppressionType enum."""
        normalized = suppression_str.lower().strip().replace(" ", "_")
        for stype in cls:
            if stype.value == normalized:
                return stype
        return cls.NONE


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass(frozen=True)
class ViralScoreConfig:
    """Configuration for viral score computation weights and thresholds."""
    
    # Component weights (must sum to 1.0)
    reach_velocity_weight: float = 0.30
    retention_strength_weight: float = 0.20
    engagement_quality_weight: float = 0.25
    propagation_factor_weight: float = 0.15
    decay_resistance_weight: float = 0.10
    
    # Horizon scaling parameters
    short_horizon_threshold_hours: int = 6
    long_horizon_threshold_hours: int = 72
    uncertainty_inflation_factor: float = 1.5
    horizon_scaling_smoothness: float = 0.3  # Controls transition smoothness
    
    # Suppression penalty multipliers [0.0, 1.0]
    suppression_penalties: Dict[SuppressionType, float] = field(default_factory=lambda: {
        SuppressionType.SHADOW_BAN: 0.3,
        SuppressionType.THROTTLED: 0.6,
        SuppressionType.POLICY_FRICTION: 0.8,
        SuppressionType.ACCOUNT_DAMPENING: 0.7,
        SuppressionType.NONE: 1.0
    })
    
    # Platform normalization baselines (median performance per hour)
    platform_baselines: Dict[Platform, float] = field(default_factory=lambda: {
        Platform.TIKTOK: 5000.0,
        Platform.YOUTUBE_SHORTS: 3000.0,
        Platform.INSTAGRAM_REELS: 4000.0,
        Platform.TWITTER: 1000.0,
        Platform.LINKEDIN: 500.0
    })
    
    # Platform difficulty multipliers (harder platforms get boost)
    platform_difficulty: Dict[Platform, float] = field(default_factory=lambda: {
        Platform.TIKTOK: 1.0,
        Platform.YOUTUBE_SHORTS: 1.1,
        Platform.INSTAGRAM_REELS: 1.05,
        Platform.TWITTER: 1.3,
        Platform.LINKEDIN: 1.4
    })
    
    # Platform median retention baselines
    platform_median_retention: Dict[Platform, float] = field(default_factory=lambda: {
        Platform.TIKTOK: 0.45,
        Platform.YOUTUBE_SHORTS: 0.40,
        Platform.INSTAGRAM_REELS: 0.42,
        Platform.TWITTER: 0.35,
        Platform.LINKEDIN: 0.50
    })
    
    # Confidence estimation thresholds
    min_data_points_for_high_confidence: int = 100
    min_observation_hours_for_stability: int = 4
    variance_stability_threshold: float = 0.7
    
    # Component computation parameters
    cold_start_window_hours: float = 2.0
    cold_start_discount_factor: float = 0.7
    second_half_retention_bonus_multiplier: float = 20.0
    stability_bonus_threshold: float = 0.5
    
    # Engagement quality weights
    engagement_weights: Dict[str, float] = field(default_factory=lambda: {
        'comments': 5.0,
        'shares': 4.0,
        'saves': 3.5,
        'rewatches': 2.5,
        'likes': 1.0
    })
    
    # Propagation scoring parameters
    share_rate_multiplier: float = 1000.0
    share_score_max: float = 40.0
    stitch_rate_multiplier: float = 20.0
    stitch_score_max: float = 30.0
    embed_score_multiplier: float = 2.0
    embed_score_max: float = 20.0
    adoption_score_multiplier: float = 5.0
    adoption_score_max: float = 30.0
    
    # Decay resistance parameters
    half_life_velocity_threshold: float = 0.5  # 50% of peak
    half_life_score_multiplier: float = 10.0
    min_trajectory_points_for_decay: int = 3
    
    # Performance optimization
    enable_caching: bool = True
    cache_ttl_seconds: float = 300.0  # 5 minutes
    batch_computation_threshold: int = 10
    
    # Validation strictness
    require_all_components: bool = False  # Allow partial computation
    min_confidence_for_valid_score: float = 0.3
    
    def __post_init__(self):
        """Validate configuration invariants."""
        # Weight sum validation
        total_weight = (
            self.reach_velocity_weight +
            self.retention_strength_weight +
            self.engagement_quality_weight +
            self.propagation_factor_weight +
            self.decay_resistance_weight
        )
        assert abs(total_weight - 1.0) < 0.001, \
            f"Weights must sum to 1.0, got {total_weight:.6f}"
        
        # Penalty bounds validation
        for stype, penalty in self.suppression_penalties.items():
            assert 0.0 <= penalty <= 1.0, \
                f"Penalty for {stype} must be in [0,1], got {penalty}"
        
        # Positive thresholds
        assert self.short_horizon_threshold_hours > 0
        assert self.long_horizon_threshold_hours > self.short_horizon_threshold_hours
        assert self.uncertainty_inflation_factor >= 1.0
        
        # Positive multipliers
        for platform, multiplier in self.platform_difficulty.items():
            assert multiplier > 0, f"Difficulty multiplier for {platform} must be positive"
        
        # Retention bounds
        for platform, retention in self.platform_median_retention.items():
            assert 0.0 <= retention <= 1.0, \
                f"Median retention for {platform} must be in [0,1]"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class ViralScoreComponents:
    """Individual components of the viral score (all independently computed)."""
    reach_velocity: float          # dv/dt adjusted for platform cold-start
    retention_strength: float      # area under retention curve above median
    engagement_quality: float      # weighted engagement per impression
    propagation_factor: float      # network amplification signal
    decay_resistance: float        # half-life of attention
    
    def __post_init__(self):
        """Validate component bounds."""
        assert 0.0 <= self.reach_velocity <= 100.0, \
            f"Reach velocity out of bounds: {self.reach_velocity}"
        assert 0.0 <= self.retention_strength <= 100.0, \
            f"Retention strength out of bounds: {self.retention_strength}"
        assert 0.0 <= self.engagement_quality <= 100.0, \
            f"Engagement quality out of bounds: {self.engagement_quality}"
        assert 0.0 <= self.propagation_factor <= 100.0, \
            f"Propagation factor out of bounds: {self.propagation_factor}"
        assert 0.0 <= self.decay_resistance <= 100.0, \
            f"Decay resistance out of bounds: {self.decay_resistance}"
    
    def to_dict(self) -> Dict[str, float]:
        """Convert to dictionary representation."""
        return {
            'reach_velocity': self.reach_velocity,
            'retention_strength': self.retention_strength,
            'engagement_quality': self.engagement_quality,
            'propagation_factor': self.propagation_factor,
            'decay_resistance': self.decay_resistance
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, float]) -> 'ViralScoreComponents':
        """Create from dictionary representation."""
        return cls(
            reach_velocity=data['reach_velocity'],
            retention_strength=data['retention_strength'],
            engagement_quality=data['engagement_quality'],
            propagation_factor=data['propagation_factor'],
            decay_resistance=data['decay_resistance']
        )


@dataclass(frozen=True)
class ObservedMetrics:
    """Raw observed metrics from the platform (immutable facts only)."""
    content_id: str
    platform: Platform
    observation_timestamp: float
    content_age_hours: float
    
    # Reach metrics
    total_views: int
    unique_viewers: int
    impressions: int
    
    # Retention metrics (0.0-1.0 at different time marks)
    retention_curve: Dict[float, float]  # {time_pct: retention_pct}
    avg_watch_time_seconds: float
    completion_rate: float
    
    # Engagement metrics
    likes: int
    comments: int
    shares: int
    saves: int
    rewatches: int
    
    # Propagation metrics
    stitches_duets: int
    embeds: int
    secondary_creator_adoptions: int
    
    # Temporal decay metrics
    hourly_view_trajectory: Dict[int, int]  # {hour: views_in_that_hour}
    
    # Suppression signals
    detected_suppression: SuppressionType
    throttle_percentage: Optional[float] = None
    
    # Data quality
    data_point_count: int = 0
    variance_stability_score: float = 0.0
    
    # Metadata
    account_id: Optional[str] = None
    upload_timestamp: Optional[float] = None
    
    def __post_init__(self):
        """Validate metrics invariants."""
        assert self.total_views >= 0, "Total views cannot be negative"
        assert self.impressions >= 0, "Impressions cannot be negative"
        assert self.content_age_hours >= 0, "Content age cannot be negative"
        assert 0.0 <= self.completion_rate <= 1.0, \
            f"Completion rate must be in [0,1], got {self.completion_rate}"
        assert 0.0 <= self.variance_stability_score <= 1.0, \
            f"Variance stability must be in [0,1], got {self.variance_stability_score}"
        
        # Validate retention curve
        for time_pct, retention_pct in self.retention_curve.items():
            assert 0.0 <= time_pct <= 1.0, \
                f"Retention curve time_pct must be in [0,1], got {time_pct}"
            assert 0.0 <= retention_pct <= 1.0, \
                f"Retention curve retention_pct must be in [0,1], got {retention_pct}"
        
        # Validate throttle percentage
        if self.throttle_percentage is not None:
            assert 0.0 <= self.throttle_percentage <= 100.0, \
                f"Throttle percentage must be in [0,100], got {self.throttle_percentage}"
    
    def get_views_per_hour(self) -> float:
        """Compute views per hour."""
        if self.content_age_hours < 0.1:
            return 0.0
        return self.total_views / self.content_age_hours
    
    def get_engagement_total(self) -> int:
        """Get total engagement count."""
        return (
            self.likes + self.comments + self.shares + 
            self.saves + self.rewatches
        )
    
    def has_sufficient_data(self, min_data_points: int = 3) -> bool:
        """Check if metrics have sufficient data for scoring."""
        return (
            self.total_views > 0 and
            self.content_age_hours > 0 and
            self.data_point_count >= min_data_points
        )


@dataclass(frozen=True)
class ViralScoreSnapshot:
    """Immutable output of viral score computation (canonical record)."""
    content_id: str
    platform: str
    timestamp: float
    
    raw_score: float              # pre-normalization
    normalized_score: float       # cross-platform comparable [0-100]
    
    confidence: float             # [0.0, 1.0] - observation completeness
    horizon_hours: int
    
    components: Dict[str, float]  # named breakdown
    penalties: Dict[str, float]   # suppression / uncertainty
    
    # Metadata
    computation_version: str = "1.0.0"
    computation_id: Optional[str] = None
    
    def __post_init__(self):
        """Validate snapshot invariants."""
        assert 0.0 <= self.raw_score <= 100.0, \
            f"Raw score out of bounds: {self.raw_score}"
        assert 0.0 <= self.normalized_score <= 100.0, \
            f"Normalized score out of bounds: {self.normalized_score}"
        assert 0.0 <= self.confidence <= 1.0, \
            f"Confidence out of bounds: {self.confidence}"
        assert self.horizon_hours > 0, \
            f"Invalid horizon: {self.horizon_hours}"
    
    def to_json(self) -> str:
        """Serialize to JSON for persistence."""
        return json.dumps({
            'content_id': self.content_id,
            'platform': self.platform,
            'timestamp': self.timestamp,
            'raw_score': self.raw_score,
            'normalized_score': self.normalized_score,
            'confidence': self.confidence,
            'horizon_hours': self.horizon_hours,
            'components': self.components,
            'penalties': self.penalties,
            'computation_version': self.computation_version,
            'computation_id': self.computation_id
        }, indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'ViralScoreSnapshot':
        """Deserialize from JSON."""
        data = json.loads(json_str)
        # Handle backward compatibility
        if 'computation_version' not in data:
            data['computation_version'] = "1.0.0"
        if 'computation_id' not in data:
            data['computation_id'] = None
        return cls(**data)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'content_id': self.content_id,
            'platform': self.platform,
            'timestamp': self.timestamp,
            'raw_score': self.raw_score,
            'normalized_score': self.normalized_score,
            'confidence': self.confidence,
            'horizon_hours': self.horizon_hours,
            'components': self.components,
            'penalties': self.penalties,
            'computation_version': self.computation_version,
            'computation_id': self.computation_id
        }
    
    def get_component_breakdown(self) -> Dict[str, Any]:
        """Get detailed component breakdown for analysis."""
        return {
            'components': self.components,
            'raw_score': self.raw_score,
            'normalized_score': self.normalized_score,
            'confidence': self.confidence,
            'penalties_applied': self.penalties,
            'effective_score': self.normalized_score * self.confidence
        }


# ============================================================================
# METRICS LOADER
# ============================================================================

class MetricsLoader:
    """Loads and validates metrics from various sources."""
    
    def __init__(
        self,
        metrics_provider: Optional[MetricsProviderProtocol] = None,
        state_interface: Optional[PostingStateInterface] = None
    ):
        self.metrics_provider = metrics_provider
        self.state_interface = state_interface
    
    def load_observed_metrics(
        self,
        content_id: str,
        platform: str,
        observation_timestamp: Optional[float] = None
    ) -> Optional[ObservedMetrics]:
        """
        Load observed metrics for content.
        
        Args:
            content_id: Unique identifier for content
            platform: Platform name (will be converted to Platform enum)
            observation_timestamp: Timestamp of observation (defaults to now)
            
        Returns:
            ObservedMetrics or None if metrics unavailable
        """
        if observation_timestamp is None:
            observation_timestamp = time.time()
        
        try:
            platform_enum = Platform.from_string(platform)
        except ValueError:
            logger.warning(f"Unknown platform: {platform}, skipping metrics load")
            return None
        
        # Try to load from metrics provider
        metrics_data = None
        if self.metrics_provider:
            try:
                metrics_data = self.metrics_provider.get_observed_metrics(content_id, platform)
            except Exception as e:
                logger.warning(f"Error loading metrics from provider: {e}")
        
        # If no provider or provider failed, return None
        if not metrics_data:
            return None
        
        # Extract suppression signals
        suppression_type = SuppressionType.NONE
        throttle_pct = None
        
        if self.metrics_provider:
            try:
                suppression_data = self.metrics_provider.get_suppression_signals(
                    content_id, platform
                )
                if suppression_data:
                    suppression_type = SuppressionType.from_string(
                        suppression_data.get('type', 'none')
                    )
                    throttle_pct = suppression_data.get('throttle_percentage')
            except Exception as e:
                logger.debug(f"Error loading suppression signals: {e}")
        
        # Also check state interface for account-level suppression
        if self.state_interface:
            try:
                account_id = metrics_data.get('account_id')
                if account_id:
                    suppression_level = self.state_interface.get_suppression_level(
                        platform, account_id
                    )
                    if suppression_level and suppression_level != 'NONE':
                        if suppression_type == SuppressionType.NONE:
                            # Map state suppression level to type
                            suppression_type = self._map_suppression_level(suppression_level)
            except Exception as e:
                logger.debug(f"Error checking state interface: {e}")
        
        # Build ObservedMetrics
        try:
            upload_ts = metrics_data.get('upload_timestamp')
            if upload_ts:
                content_age_hours = (observation_timestamp - upload_ts) / 3600.0
            else:
                content_age_hours = metrics_data.get('content_age_hours', 0.0)
            
            return ObservedMetrics(
                content_id=content_id,
                platform=platform_enum,
                observation_timestamp=observation_timestamp,
                content_age_hours=max(0.0, content_age_hours),
                total_views=metrics_data.get('total_views', 0),
                unique_viewers=metrics_data.get('unique_viewers', 0),
                impressions=metrics_data.get('impressions', 0),
                retention_curve=metrics_data.get('retention_curve', {}),
                avg_watch_time_seconds=metrics_data.get('avg_watch_time_seconds', 0.0),
                completion_rate=metrics_data.get('completion_rate', 0.0),
                likes=metrics_data.get('likes', 0),
                comments=metrics_data.get('comments', 0),
                shares=metrics_data.get('shares', 0),
                saves=metrics_data.get('saves', 0),
                rewatches=metrics_data.get('rewatches', 0),
                stitches_duets=metrics_data.get('stitches_duets', 0),
                embeds=metrics_data.get('embeds', 0),
                secondary_creator_adoptions=metrics_data.get('secondary_creator_adoptions', 0),
                hourly_view_trajectory=metrics_data.get('hourly_view_trajectory', {}),
                detected_suppression=suppression_type,
                throttle_percentage=throttle_pct,
                data_point_count=metrics_data.get('data_point_count', 0),
                variance_stability_score=metrics_data.get('variance_stability_score', 0.0),
                account_id=metrics_data.get('account_id'),
                upload_timestamp=upload_ts
            )
        except Exception as e:
            logger.error(f"Error constructing ObservedMetrics: {e}")
            return None
    
    @staticmethod
    def _map_suppression_level(level: str) -> SuppressionType:
        """Map state suppression level to SuppressionType."""
        level_lower = level.lower()
        if 'shadow' in level_lower or 'ban' in level_lower:
            return SuppressionType.SHADOW_BAN
        elif 'throttle' in level_lower or 'rate_limit' in level_lower:
            return SuppressionType.THROTTLED
        elif 'policy' in level_lower or 'friction' in level_lower:
            return SuppressionType.POLICY_FRICTION
        elif 'dampen' in level_lower or 'restrict' in level_lower:
            return SuppressionType.ACCOUNT_DAMPENING
        else:
            return SuppressionType.NONE


# ============================================================================
# HORIZON-AWARE SCALER
# ============================================================================

class HorizonAwareScaler:
    """Applies time-horizon adjustments to raw component scores."""
    
    def __init__(self, config: ViralScoreConfig):
        self.config = config
    
    def scale(self, raw_score: float, horizon_hours: int) -> float:
        """
        Scale score based on observation horizon.
        
        Short horizon → uncertainty inflated (conservative)
        Long horizon → decay weighted stronger
        
        Uses smooth transitions for continuous scoring.
        """
        if horizon_hours < self.config.short_horizon_threshold_hours:
            # Early signals are less certain - inflate uncertainty
            # Smooth transition from 0 to short_horizon_threshold_hours
            progress = horizon_hours / self.config.short_horizon_threshold_hours
            uncertainty_factor = 1.0 - progress
            
            # Apply smooth penalty curve
            penalty_magnitude = uncertainty_factor * (self.config.uncertainty_inflation_factor - 1.0)
            smooth_penalty = penalty_magnitude * self.config.horizon_scaling_smoothness
            
            return raw_score * (1.0 - smooth_penalty * 0.2)
        
        elif horizon_hours > self.config.long_horizon_threshold_hours:
            # Long-tail content - boost decay resistance contribution
            # Smooth transition beyond long_horizon_threshold_hours
            excess_hours = horizon_hours - self.config.long_horizon_threshold_hours
            longtail_factor = 1.0 + (excess_hours / self.config.long_horizon_threshold_hours)
            longtail_factor = min(longtail_factor, 2.0)  # Cap at 2x
            
            # Smooth boost application
            boost = (longtail_factor - 1.0) * 0.1 * self.config.horizon_scaling_smoothness
            
            return raw_score * (1.0 + boost)
        
        else:
            # Sweet spot - minimal adjustment
            # Very slight adjustment based on proximity to thresholds
            progress_from_short = (
                (horizon_hours - self.config.short_horizon_threshold_hours) /
                (self.config.long_horizon_threshold_hours - self.config.short_horizon_threshold_hours)
            )
            
            # Minimal smoothing in the sweet spot
            return raw_score * (1.0 + progress_from_short * 0.01)
    
    def get_scaling_factor(self, horizon_hours: int) -> float:
        """Get the scaling factor for a given horizon (for analysis)."""
        dummy_score = 100.0
        scaled = self.scale(dummy_score, horizon_hours)
        return scaled / dummy_score


# ============================================================================
# SUPPRESSION PENALTY MODEL
# ============================================================================

class SuppressionPenaltyModel:
    """Applies explicit negative multipliers for distribution suppression."""
    
    def __init__(self, config: ViralScoreConfig):
        self.config = config
    
    def compute_penalty(
        self,
        suppression_type: SuppressionType,
        throttle_pct: Optional[float] = None,
        account_suppression_level: Optional[str] = None
    ) -> Tuple[float, Dict[str, float]]:
        """
        Compute suppression penalty multiplier.
        
        Returns:
            (penalty_multiplier, penalty_breakdown)
            penalty_multiplier ∈ [0.0, 1.0]
        """
        # Base penalty from suppression type
        base_penalty = self.config.suppression_penalties.get(
            suppression_type,
            self.config.suppression_penalties[SuppressionType.NONE]
        )
        
        penalties = {
            'base_suppression': base_penalty,
            'suppression_type': suppression_type.value
        }
        
        # Additional throttle penalty if detected
        if throttle_pct is not None and throttle_pct > 0:
            # Throttle penalty scales with throttle percentage
            throttle_penalty_factor = 1.0 - (throttle_pct / 100.0) * 0.5
            penalties['throttle_penalty'] = throttle_penalty_factor
            base_penalty *= throttle_penalty_factor
        
        # Account-level suppression (if provided)
        if account_suppression_level and account_suppression_level != 'NONE':
            # Map account suppression level to additional penalty
            account_penalty_map = {
                'LOW': 0.95,
                'MEDIUM': 0.85,
                'HIGH': 0.70,
                'UNKNOWN': 0.90
            }
            account_penalty = account_penalty_map.get(account_suppression_level.upper(), 1.0)
            if account_penalty < 1.0:
                penalties['account_suppression'] = account_penalty
                base_penalty *= account_penalty
        
        # Ensure penalty is in valid range
        base_penalty = max(0.0, min(1.0, base_penalty))
        
        return base_penalty, penalties


# ============================================================================
# CONFIDENCE ESTIMATOR
# ============================================================================

class ConfidenceEstimator:
    """Estimates confidence in the viral score based on observation completeness."""
    
    def __init__(self, config: ViralScoreConfig):
        self.config = config
        
        # Platform maturity scores (data richness)
        self.platform_maturity = {
            Platform.TIKTOK: 0.95,
            Platform.YOUTUBE_SHORTS: 0.90,
            Platform.INSTAGRAM_REELS: 0.90,
            Platform.TWITTER: 0.85,
            Platform.LINKEDIN: 0.80
        }
    
    def estimate(
        self,
        metrics: ObservedMetrics,
        horizon_hours: int
    ) -> float:
        """
        Estimate confidence ∈ [0.0, 1.0].
        
        Confidence represents completeness of observation, NOT accuracy.
        """
        confidence_factors = []
        
        # Factor 1: Data volume
        data_volume_conf = min(
            metrics.data_point_count / self.config.min_data_points_for_high_confidence,
            1.0
        )
        confidence_factors.append(max(0.1, data_volume_conf))  # Minimum 0.1
        
        # Factor 2: Observation time
        time_conf = min(
            metrics.content_age_hours / self.config.min_observation_hours_for_stability,
            1.0
        )
        confidence_factors.append(max(0.1, time_conf))
        
        # Factor 3: Variance stability
        variance_conf = metrics.variance_stability_score
        confidence_factors.append(max(0.1, variance_conf))
        
        # Factor 4: Suppression ambiguity (lower confidence if suppressed)
        suppression_conf = 1.0 if metrics.detected_suppression == SuppressionType.NONE else 0.7
        confidence_factors.append(suppression_conf)
        
        # Factor 5: Platform maturity (more data on mature platforms)
        platform_conf = self.platform_maturity.get(metrics.platform, 0.75)
        confidence_factors.append(platform_conf)
        
        # Factor 6: Horizon adequacy (how well horizon matches observation)
        horizon_adequacy = min(
            horizon_hours / max(metrics.content_age_hours, 0.1),
            1.0
        )
        # If horizon is way beyond observation, confidence decreases
        if horizon_hours > metrics.content_age_hours * 2.0:
            horizon_adequacy *= 0.8
        confidence_factors.append(max(0.1, horizon_adequacy))
        
        # Factor 7: Data completeness (presence of optional metrics)
        completeness_score = self._compute_completeness_score(metrics)
        confidence_factors.append(completeness_score)
        
        # Geometric mean for balanced contribution (all factors matter)
        if len(confidence_factors) > 0:
            product = math.prod(confidence_factors)
            if product > 0:
                confidence = math.pow(product, 1.0 / len(confidence_factors))
            else:
                confidence = 0.0
        else:
            confidence = 0.5  # Default if no factors
        
        return min(max(confidence, 0.0), 1.0)
    
    def _compute_completeness_score(self, metrics: ObservedMetrics) -> float:
        """Compute data completeness score based on available metrics."""
        completeness_factors = []
        
        # Required metrics (always present if we got this far)
        if metrics.total_views > 0:
            completeness_factors.append(1.0)
        else:
            completeness_factors.append(0.0)
        
        # Optional but valuable metrics
        if metrics.retention_curve:
            completeness_factors.append(0.9)
        else:
            completeness_factors.append(0.5)
        
        if metrics.hourly_view_trajectory and len(metrics.hourly_view_trajectory) >= 3:
            completeness_factors.append(0.9)
        else:
            completeness_factors.append(0.5)
        
        if metrics.impressions > 0:
            completeness_factors.append(0.8)
        else:
            completeness_factors.append(0.4)
        
        # Engagement metrics presence
        if metrics.get_engagement_total() > 0:
            completeness_factors.append(0.8)
        else:
            completeness_factors.append(0.3)
        
        if completeness_factors:
            return sum(completeness_factors) / len(completeness_factors)
        else:
            return 0.5


# ============================================================================
# COMPONENT COMPUTER
# ============================================================================

class ComponentComputer:
    """Computes individual viral score components from observed metrics."""
    
    def __init__(self, config: ViralScoreConfig):
        self.config = config
    
    def compute_reach_velocity(self, metrics: ObservedMetrics) -> float:
        """
        Measures views per unit time, normalized for platform cold-start bias.
        
        dv/dt adjusted by early-lift baselines.
        Accounts for:
        - platform cold-start bias
        - upload time windows
        """
        if metrics.content_age_hours < 0.1:
            return 0.0
        
        # Raw velocity (views per hour)
        velocity = metrics.get_views_per_hour()
        
        if velocity <= 0:
            return 0.0
        
        # Platform baseline normalization
        baseline = self.config.platform_baselines.get(metrics.platform, 5000.0)
        normalized_velocity = velocity / baseline
        
        # Cold-start adjustment (first hours have natural boost)
        if metrics.content_age_hours < self.config.cold_start_window_hours:
            # Gradual discount removal as content ages
            progress = metrics.content_age_hours / self.config.cold_start_window_hours
            coldstart_factor = (
                self.config.cold_start_discount_factor + 
                (1.0 - self.config.cold_start_discount_factor) * progress
            )
            normalized_velocity *= coldstart_factor
        
        # Logarithmic scaling for high-velocity content (diminishing returns)
        # Use log10 scale: log10(1 + normalized_velocity * 9) maps [0, 1] to [0, 1]
        scaled_velocity = math.log10(1.0 + normalized_velocity * 9.0)
        
        # Clamp and scale to [0-100]
        return min(scaled_velocity * 100.0, 100.0)
    
    def compute_retention_strength(self, metrics: ObservedMetrics) -> float:
        """
        Area under retention curve above platform median.
        
        Penalizes front-loaded bounces and cliff drops.
        Rewards second-half stability.
        """
        if not metrics.retention_curve:
            # Fallback to completion rate
            return metrics.completion_rate * 50.0
        
        median = self.config.platform_median_retention.get(metrics.platform, 0.40)
        
        # Compute area under curve above median using trapezoidal integration
        sorted_points = sorted(metrics.retention_curve.items())
        
        if len(sorted_points) < 2:
            return metrics.completion_rate * 50.0
        
        area_above_median = 0.0
        total_area = 0.0
        
        for i in range(len(sorted_points) - 1):
            t1, r1 = sorted_points[i]
            t2, r2 = sorted_points[i + 1]
            
            # Trapezoid area
            width = t2 - t1
            avg_retention = (r1 + r2) / 2.0
            segment_area = width * avg_retention
            total_area += segment_area
            
            # Area above median
            if avg_retention > median:
                height = avg_retention - median
                area_above_median += width * height
        
        # Normalize area above median by total possible area
        if total_area > 0:
            excess_ratio = area_above_median / total_area
        else:
            excess_ratio = 0.0
        
        # Bonus for second-half stability (50%-100% of video)
        stability_bonus = 0.0
        second_half_retention = [
            r for t, r in sorted_points if t >= self.config.stability_bonus_threshold
        ]
        if second_half_retention:
            avg_second_half = sum(second_half_retention) / len(second_half_retention)
            if avg_second_half > median:
                # Bonus scales with how much above median
                stability_bonus = (
                    (avg_second_half - median) * 
                    self.config.second_half_retention_bonus_multiplier
                )
        
        # Combine area score and stability bonus
        area_score = excess_ratio * 100.0
        total_score = area_score + stability_bonus
        
        return min(total_score, 100.0)
    
    def compute_engagement_quality(self, metrics: ObservedMetrics) -> float:
        """
        Weighted engagement per impression (NOT raw counts).
        
        Likes alone are insufficient.
        """
        if metrics.impressions == 0:
            return 0.0
        
        # Weight different engagement types by quality
        weighted_engagement = (
            metrics.comments * self.config.engagement_weights['comments'] +
            metrics.shares * self.config.engagement_weights['shares'] +
            metrics.saves * self.config.engagement_weights['saves'] +
            metrics.rewatches * self.config.engagement_weights['rewatches'] +
            metrics.likes * self.config.engagement_weights['likes']
        )
        
        # Normalize by impressions
        engagement_per_impression = weighted_engagement / metrics.impressions
        
        # Logarithmic scaling for high engagement (diminishing returns)
        # Scale to [0-100] with logarithmic scaling
        score = min(math.log1p(engagement_per_impression * 1000) * 15.0, 100.0)
        
        return score
    
    def compute_propagation_factor(self, metrics: ObservedMetrics) -> float:
        """
        Network amplification signal - what separates viral from popular.
        
        Includes reshares, stitches/duets, embeds, secondary creator adoption.
        """
        # Primary propagation: shares as % of views
        share_score = 0.0
        if metrics.total_views > 0:
            share_rate = metrics.shares / metrics.total_views
            share_score = min(share_rate * self.config.share_rate_multiplier, self.config.share_score_max)
        
        # Secondary creation (stitches, duets, remixes)
        stitch_score = 0.0
        if metrics.total_views > 0:
            # Normalize by views/100 to get reasonable rate
            denominator = max(metrics.total_views / 100, 1)
            stitch_rate = metrics.stitches_duets / denominator
            stitch_score = min(stitch_rate * self.config.stitch_rate_multiplier, self.config.stitch_score_max)
        
        # Embed velocity (off-platform spread)
        embed_score = min(metrics.embeds * self.config.embed_score_multiplier, self.config.embed_score_max)
        
        # Secondary creator adoption (true virality marker)
        adoption_score = min(
            metrics.secondary_creator_adoptions * self.config.adoption_score_multiplier,
            self.config.adoption_score_max
        )
        
        total_propagation = share_score + stitch_score + embed_score + adoption_score
        
        return min(total_propagation, 100.0)
    
    def compute_decay_resistance(self, metrics: ObservedMetrics) -> float:
        """
        Measures half-life of attention (time until 50% velocity drop).
        
        Higher = stronger long-tail potential.
        """
        trajectory = metrics.hourly_view_trajectory
        
        if len(trajectory) < self.config.min_trajectory_points_for_decay:
            return 50.0  # neutral default for insufficient data
        
        sorted_hours = sorted(trajectory.items())
        
        # Find peak hour
        if not sorted_hours:
            return 50.0
        
        peak_hour, peak_views = max(sorted_hours, key=lambda x: x[1])
        
        if peak_views <= 0:
            return 50.0
        
        # Find when views drop to threshold of peak
        threshold_views = peak_views * self.config.half_life_velocity_threshold
        half_life_hours = None
        
        for hour, views in sorted_hours:
            if hour > peak_hour and views <= threshold_views:
                half_life_hours = hour - peak_hour
                break
        
        if half_life_hours is None:
            # Still above threshold - excellent decay resistance
            # Score based on how long we've observed without decay
            if len(sorted_hours) > 0:
                last_hour = sorted_hours[-1][0]
                time_since_peak = last_hour - peak_hour
                if time_since_peak > 0:
                    # Bonus for sustained high velocity
                    return min(100.0, 80.0 + (time_since_peak * 2.0))
            return 100.0
        
        # Score based on half-life duration
        # Longer half-life = better score
        # Use logarithmic scaling for very long half-lives
        if half_life_hours > 0:
            score = min(half_life_hours * self.config.half_life_score_multiplier, 100.0)
        else:
            score = 0.0
        
        return max(0.0, score)


# ============================================================================
# INVARIANT VALIDATOR
# ============================================================================

class ViralScoreInvariantValidator:
    """Validates invariants on computed viral scores (mandatory checks)."""
    
    @staticmethod
    def validate(snapshot: ViralScoreSnapshot) -> None:
        """
        Validate all invariants. Raises AssertionError on violation.
        
        This is a hard failure - never returns invalid scores.
        """
        # Score bounds
        assert 0.0 <= snapshot.raw_score <= 100.0, \
            f"Raw score out of bounds: {snapshot.raw_score}"
        
        assert 0.0 <= snapshot.normalized_score <= 100.0, \
            f"Normalized score out of bounds: {snapshot.normalized_score}"
        
        # Confidence bounds
        assert 0.0 <= snapshot.confidence <= 1.0, \
            f"Confidence out of bounds: {snapshot.confidence}"
        
        # Horizon sanity
        assert snapshot.horizon_hours > 0, \
            f"Invalid horizon: {snapshot.horizon_hours}"
        
        # Component bounds
        required_components = [
            'reach_velocity', 'retention_strength', 'engagement_quality',
            'propagation_factor', 'decay_resistance'
        ]
        for name in required_components:
            assert name in snapshot.components, \
                f"Missing required component: {name}"
            value = snapshot.components[name]
            assert 0.0 <= value <= 100.0, \
                f"Component {name} out of bounds: {value}"
        
        # Penalty bounds (all multipliers should be <= 1.0)
        for name, value in snapshot.penalties.items():
            # Some penalty entries might be metadata (strings), skip those
            if isinstance(value, (int, float)):
                assert 0.0 <= value <= 1.0, \
                    f"Penalty {name} out of bounds: {value}"
        
        # Normalized score consistency check
        # If penalties were applied, normalized should be <= raw
        penalty_values = [
            v for v in snapshot.penalties.values() 
            if isinstance(v, (int, float)) and v <= 1.0
        ]
        if penalty_values:
            min_penalty = min(penalty_values)
            # Allow some tolerance for platform normalization (can boost)
            expected_max = snapshot.raw_score * 1.2  # 20% tolerance for platform boost
            if snapshot.normalized_score > expected_max:
                logger.warning(
                    f"Normalized score {snapshot.normalized_score} exceeds expected max "
                    f"{expected_max} (raw: {snapshot.raw_score})"
                )
    
    @staticmethod
    def validate_metrics(metrics: ObservedMetrics) -> None:
        """Validate metrics before computation."""
        assert metrics.total_views >= 0, "Views cannot be negative"
        assert metrics.impressions >= 0, "Impressions cannot be negative"
        assert metrics.content_age_hours >= 0, "Content age cannot be negative"
        
        # Validate all engagement metrics are non-negative
        assert metrics.likes >= 0, "Likes cannot be negative"
        assert metrics.comments >= 0, "Comments cannot be negative"
        assert metrics.shares >= 0, "Shares cannot be negative"
        assert metrics.saves >= 0, "Saves cannot be negative"
        assert metrics.rewatches >= 0, "Rewatches cannot be negative"


# ============================================================================
# CORE ENGINE
# ============================================================================

class ViralScoreEngine:
    """
    Core engine for computing canonical viral scores.
    
    Single entry point for deterministic, auditable viral score computation.
    """
    
    def __init__(
        self,
        config: Optional[ViralScoreConfig] = None,
        metrics_loader: Optional[MetricsLoader] = None
    ):
        self.config = config or ViralScoreConfig()
        self.horizon_scaler = HorizonAwareScaler(self.config)
        self.suppression_model = SuppressionPenaltyModel(self.config)
        self.confidence_estimator = ConfidenceEstimator(self.config)
        self.component_computer = ComponentComputer(self.config)
        self.metrics_loader = metrics_loader
        
        # Performance optimization: caching
        self._cache: Dict[str, Tuple[ViralScoreSnapshot, float]] = {}
        self._cache_enabled = self.config.enable_caching
    
    def compute(
        self,
        metrics: ObservedMetrics,
        horizon_hours: int,
        account_suppression_level: Optional[str] = None,
        use_cache: bool = True
    ) -> ViralScoreSnapshot:
        """
        Compute canonical viral score for content at given horizon.
        
        STRICT ORDER:
        1. Load observed metrics (input)
        2. Validate sufficiency
        3. Compute raw components
        4. Apply horizon scaling
        5. Apply suppression penalties
        6. Normalize across platform
        7. Estimate confidence
        8. Emit immutable snapshot
        
        Any skip → invalid score.
        
        Args:
            metrics: Observed metrics for the content
            horizon_hours: Time horizon for score computation
            account_suppression_level: Optional account-level suppression ('NONE', 'LOW', etc.)
            use_cache: Whether to use caching (if enabled)
            
        Returns:
            ViralScoreSnapshot: Immutable canonical score record
        """
        # Check cache first
        if use_cache and self._cache_enabled:
            cache_key = self._generate_cache_key(metrics, horizon_hours, account_suppression_level)
            if cache_key in self._cache:
                cached_snapshot, cached_time = self._cache[cache_key]
                age = time.time() - cached_time
                if age < self.config.cache_ttl_seconds:
                    logger.debug(f"Cache hit for {metrics.content_id} at {horizon_hours}h")
                    return cached_snapshot
                else:
                    # Expired, remove from cache
                    del self._cache[cache_key]
        
        # Step 1: Metrics already loaded (input parameter)
        
        # Step 2: Validate sufficiency
        self._validate_metrics_sufficiency(metrics)
        ViralScoreInvariantValidator.validate_metrics(metrics)
        
        # Step 3: Compute raw components
        components = self.compute_components(metrics)
        
        # Step 4: Compute raw weighted score
        raw_score = self._compute_weighted_score(components)
        
        # Step 5: Apply horizon scaling
        horizon_scaled_score = self.horizon_scaler.scale(raw_score, horizon_hours)
        
        # Step 6: Apply suppression penalties
        suppression_multiplier, penalty_breakdown = self.suppression_model.compute_penalty(
            metrics.detected_suppression,
            metrics.throttle_percentage,
            account_suppression_level
        )
        penalized_score = horizon_scaled_score * suppression_multiplier
        
        # Step 7: Normalize across platform
        normalized_score = self.normalize_across_platforms(
            penalized_score,
            metrics.platform
        )
        
        # Step 8: Estimate confidence
        confidence = self.confidence_estimator.estimate(metrics, horizon_hours)
        
        # Generate computation ID for traceability
        computation_id = self._generate_computation_id(metrics, horizon_hours)
        
        # Create immutable snapshot
        snapshot = ViralScoreSnapshot(
            content_id=metrics.content_id,
            platform=metrics.platform.value,
            timestamp=metrics.observation_timestamp,
            raw_score=raw_score,
            normalized_score=normalized_score,
            confidence=confidence,
            horizon_hours=horizon_hours,
            components=components.to_dict(),
            penalties=penalty_breakdown,
            computation_id=computation_id
        )
        
        # Validate invariants (hard failure on violation)
        ViralScoreInvariantValidator.validate(snapshot)
        
        # Store in cache
        if use_cache and self._cache_enabled:
            cache_key = self._generate_cache_key(metrics, horizon_hours, account_suppression_level)
            self._cache[cache_key] = (snapshot, time.time())
            # Clean old cache entries if cache gets too large
            if len(self._cache) > 1000:
                self._clean_cache()
        
        return snapshot
    
    def compute_from_content_id(
        self,
        content_id: str,
        platform: str,
        horizon_hours: int,
        observation_timestamp: Optional[float] = None
    ) -> Optional[ViralScoreSnapshot]:
        """
        Compute viral score from content ID (loads metrics automatically).
        
        Args:
            content_id: Unique identifier for content
            platform: Platform name
            horizon_hours: Time horizon for score computation
            observation_timestamp: Optional observation timestamp (defaults to now)
            
        Returns:
            ViralScoreSnapshot or None if metrics unavailable
        """
        if not self.metrics_loader:
            raise ValueError("MetricsLoader not configured. Cannot load metrics from content_id.")
        
        metrics = self.metrics_loader.load_observed_metrics(
            content_id,
            platform,
            observation_timestamp
        )
        
        if not metrics:
            logger.warning(f"Could not load metrics for {content_id} on {platform}")
            return None
        
        # Get account suppression level if available
        account_suppression_level = None
        if self.metrics_loader.state_interface and metrics.account_id:
            try:
                account_suppression_level = self.metrics_loader.state_interface.get_suppression_level(
                    platform, metrics.account_id
                )
            except Exception as e:
                logger.debug(f"Error getting account suppression level: {e}")
        
        return self.compute(metrics, horizon_hours, account_suppression_level)
    
    def compute_batch(
        self,
        metrics_list: List[ObservedMetrics],
        horizon_hours: int,
        account_suppression_levels: Optional[Dict[str, str]] = None
    ) -> List[ViralScoreSnapshot]:
        """
        Compute viral scores for multiple pieces of content.
        
        Args:
            metrics_list: List of observed metrics
            horizon_hours: Time horizon for score computation (same for all)
            account_suppression_levels: Optional dict mapping account_id to suppression level
            
        Returns:
            List of ViralScoreSnapshot (same order as input)
        """
        results = []
        
        for metrics in metrics_list:
            account_level = None
            if account_suppression_levels and metrics.account_id:
                account_level = account_suppression_levels.get(metrics.account_id)
            
            try:
                snapshot = self.compute(metrics, horizon_hours, account_level)
                results.append(snapshot)
            except Exception as e:
                logger.error(f"Error computing score for {metrics.content_id}: {e}")
                # Optionally create error snapshot or skip
                # For now, skip failed items
                continue
        
        return results
    
    def compute_components(self, metrics: ObservedMetrics) -> ViralScoreComponents:
        """Compute individual viral score components."""
        return ViralScoreComponents(
            reach_velocity=self.component_computer.compute_reach_velocity(metrics),
            retention_strength=self.component_computer.compute_retention_strength(metrics),
            engagement_quality=self.component_computer.compute_engagement_quality(metrics),
            propagation_factor=self.component_computer.compute_propagation_factor(metrics),
            decay_resistance=self.component_computer.compute_decay_resistance(metrics)
        )
    
    def _compute_weighted_score(self, components: ViralScoreComponents) -> float:
        """Apply configured weights to components."""
        score = (
            components.reach_velocity * self.config.reach_velocity_weight +
            components.retention_strength * self.config.retention_strength_weight +
            components.engagement_quality * self.config.engagement_quality_weight +
            components.propagation_factor * self.config.propagation_factor_weight +
            components.decay_resistance * self.config.decay_resistance_weight
        )
        return score
    
    def normalize_across_platforms(
        self,
        score: float,
        platform: Platform
    ) -> float:
        """
        Normalize score to be comparable across platforms.
        
        Accounts for platform-specific distribution characteristics.
        """
        multiplier = self.config.platform_difficulty.get(platform, 1.0)
        normalized = score * multiplier
        
        return min(normalized, 100.0)
    
    def _validate_metrics_sufficiency(self, metrics: ObservedMetrics) -> None:
        """Validate that metrics contain sufficient data for scoring."""
        if not metrics.has_sufficient_data():
            raise ValueError(
                f"Insufficient data for {metrics.content_id}: "
                f"views={metrics.total_views}, age={metrics.content_age_hours:.2f}h, "
                f"data_points={metrics.data_point_count}"
            )
        
        if metrics.content_age_hours < 0.01:
            raise ValueError(f"Content age too small: {metrics.content_age_hours}")
    
    def _generate_cache_key(
        self,
        metrics: ObservedMetrics,
        horizon_hours: int,
        account_suppression_level: Optional[str]
    ) -> str:
        """Generate cache key for metrics and horizon."""
        # Use content_id, platform, horizon, and key metric values
        key_data = {
            'content_id': metrics.content_id,
            'platform': metrics.platform.value,
            'horizon': horizon_hours,
            'views': metrics.total_views,
            'age': round(metrics.content_age_hours, 2),
            'suppression': metrics.detected_suppression.value,
            'account_suppression': account_suppression_level or 'none'
        }
        key_str = json.dumps(key_data, sort_keys=True)
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def _generate_computation_id(
        self,
        metrics: ObservedMetrics,
        horizon_hours: int
    ) -> str:
        """Generate unique computation ID for traceability."""
        data = {
            'content_id': metrics.content_id,
            'platform': metrics.platform.value,
            'timestamp': metrics.observation_timestamp,
            'horizon': horizon_hours
        }
        data_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()[:16]
    
    def _clean_cache(self) -> None:
        """Clean expired cache entries."""
        current_time = time.time()
        expired_keys = [
            key for key, (_, cached_time) in self._cache.items()
            if current_time - cached_time > self.config.cache_ttl_seconds
        ]
        for key in expired_keys:
            del self._cache[key]
    
    def clear_cache(self) -> None:
        """Clear all cached scores."""
        self._cache.clear()


# ============================================================================
# EXAMPLE USAGE & INTEGRATION
# ============================================================================

def example_usage():
    """Demonstrates typical usage of ViralScoreEngine."""
    
    # Create engine with custom config
    config = ViralScoreConfig(
        reach_velocity_weight=0.35,  # Emphasize reach more
        propagation_factor_weight=0.20  # Value viral spread
    )
    engine = ViralScoreEngine(config)
    
    # Simulate observed metrics for a piece of content
    metrics = ObservedMetrics(
        content_id="video_12345",
        platform=Platform.TIKTOK,
        observation_timestamp=1704067200.0,
        content_age_hours=24.0,
        total_views=150000,
        unique_viewers=120000,
        impressions=180000,
        retention_curve={
            0.0: 1.0,
            0.25: 0.65,
            0.5: 0.48,
            0.75: 0.35,
            1.0: 0.28
        },
        avg_watch_time_seconds=12.5,
        completion_rate=0.28,
        likes=8500,
        comments=420,
        shares=1200,
        saves=850,
        rewatches=2400,
        stitches_duets=45,
        embeds=12,
        secondary_creator_adoptions=8,
        hourly_view_trajectory={
            1: 5000, 2: 12000, 3: 25000, 4: 35000, 5: 28000,
            6: 18000, 12: 12000, 18: 8000, 24: 7000
        },
        detected_suppression=SuppressionType.NONE,
        data_point_count=150,
        variance_stability_score=0.85
    )
    
    # Compute viral score at 24-hour horizon
    snapshot = engine.compute(metrics, horizon_hours=24)
    
    print(f"Viral Score: {snapshot.normalized_score:.2f}")
    print(f"Confidence: {snapshot.confidence:.2f}")
    print(f"Components: {snapshot.components}")
    print(f"Penalties: {snapshot.penalties}")
    
    # Persist snapshot
    json_output = snapshot.to_json()
    print(f"\nPersistable JSON:\n{json_output}")
    
    # Example batch computation
    metrics_list = [metrics]  # Add more metrics as needed
    snapshots = engine.compute_batch(metrics_list, horizon_hours=24)
    print(f"\nComputed {len(snapshots)} scores in batch")


if __name__ == "__main__":
    example_usage()
