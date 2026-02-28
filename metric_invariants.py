"""
/evaluation/metric_invariants.py

Metric Invariant Enforcement & Causal Guardrail Layer

IMMUTABLE LAWS OF REALITY — NO METRIC, MODEL, AGENT, OPTIMIZER, OR DASHBOARD MAY VIOLATE THESE.

Violation = Invalid Result. No fallback. No smoothing. No retries. No silent degradation.
"""

import time
import json
import logging
import hashlib
import threading
from typing import Callable, List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from collections import defaultdict
from pathlib import Path
import numpy as np
from datetime import datetime, timedelta


# ============================================================================
# CORE TYPES
# ============================================================================

class Severity(Enum):
    """Invariant violation severity levels"""
    CRITICAL = "critical"      # Halt immediately
    HIGH = "high"              # Quarantine metric
    MEDIUM = "medium"          # Flag and continue
    LOW = "low"                # Log only
    DIAGNOSTIC = "diagnostic"  # Monitor for drift


class InvariantScope(Enum):
    """Where invariant applies"""
    GLOBAL = "global"                    # All metrics, all contexts
    METRIC_SPECIFIC = "metric_specific"  # Specific metric types
    PLATFORM_SPECIFIC = "platform"       # Platform-dependent
    TEMPORAL = "temporal"                # Time-dependent
    CROSS_METRIC = "cross_metric"        # Relationships between metrics


class RemediationAction(Enum):
    """What to do when invariant violated"""
    HALT_PIPELINE = "halt_metric_pipeline"
    QUARANTINE_METRIC = "quarantine_metric"
    FLAG_AND_CONTINUE = "flag_and_continue"
    LOG_ONLY = "log_only"
    ALERT_HUMANS = "alert_humans"
    FREEZE_AGENT = "freeze_agent"
    HALT_BOOST = "halt_boost"
    PAUSE_TRAINING = "pause_training"


@dataclass
class InvariantSpec:
    """Single invariant specification"""
    name: str
    scope: InvariantScope
    severity: Severity
    applies_to: List[str]  # Metric names or categories
    check_fn: Callable[[Dict[str, Any]], Tuple[bool, Optional[str]]]
    remediation: RemediationAction
    description: str
    category: str
    
    # Drift detection
    violation_count: int = 0
    last_violation_time: Optional[float] = None
    violation_rate_window: List[float] = field(default_factory=list)
    
    # POLISH 2: Auto-escalation tracking
    medium_violation_count: int = 0
    medium_violation_window: List[float] = field(default_factory=list)
    escalated_to_high: bool = False


@dataclass
class InvariantViolation:
    """Record of a violated invariant"""
    invariant_name: str
    severity: Severity
    timestamp: float
    context: Dict[str, Any]
    message: str
    remediation_taken: RemediationAction
    metric_name: Optional[str] = None
    platform: Optional[str] = None
    violation_id: Optional[str] = None  # Unique identifier for audit trail
    
    def __post_init__(self):
        """Generate unique violation ID if not provided"""
        if self.violation_id is None:
            violation_hash = hashlib.sha256(
                f"{self.invariant_name}:{self.timestamp}:{self.message}".encode()
            ).hexdigest()[:16]
            self.violation_id = f"INV-{violation_hash}"


# ============================================================================
# DETERMINISM HELPERS (TIER-0: FIX REGRESSION)
# ============================================================================

def _now(data: Dict[str, Any]) -> float:
    """
    FIX REGRESSION: Helper to get evaluation_time from data.
    Throws if missing - ensures strict determinism.
    """
    if "evaluation_time" not in data:
        # Forward declaration - exception class defined later
        from metric_invariants import InvariantViolationError
        raise InvariantViolationError(
            "evaluation_time missing — determinism violation (wall-clock time banned in invariant logic)"
        )
    return data["evaluation_time"]


# ============================================================================
# DETERMINISM INVARIANTS (TIER-0: CHANGE 1)
# ============================================================================

class DeterminismInvariants:
    """Enforces determinism - evaluation_time must be provided"""
    
    @staticmethod
    def evaluation_time_required(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        CHANGE 1: Hard rule - evaluation_time must be provided.
        This ensures bit-exact replays and legal auditability.
        """
        if "evaluation_time" not in data:
            return False, "evaluation_time missing — determinism violation (wall-clock time banned in invariant checks)"
        return True, None


# ============================================================================
# TEMPORAL INVARIANTS (MOST CRITICAL)
# ============================================================================

class TemporalInvariants:
    """Prevents time-travel bugs and future data leakage"""
    
    @staticmethod
    def no_future_timestamps(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Metric timestamp cannot be in the future"""
        metric_ts = data.get('timestamp', 0)
        # TIER-0 FIX: Use injected evaluation_time, not wall-clock time
        evaluation_time = data.get('evaluation_time')
        if evaluation_time is None:
            return False, "CRITICAL: evaluation_time not provided - determinism violation"
        current_ts = evaluation_time
        
        if metric_ts > current_ts + 1.0:  # 1s tolerance for clock skew
            return False, f"Metric timestamp {metric_ts} is {metric_ts - current_ts:.2f}s in the future"
        return True, None
    
    @staticmethod
    def metric_window_within_content_age(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Metric aggregation window cannot exceed content age"""
        content_created = data.get('content_created_at', 0)
        metric_window_end = data.get('metric_window_end', 0)
        # TIER-0 FIX: Use injected evaluation_time, not wall-clock time
        evaluation_time = data.get('evaluation_time')
        if evaluation_time is None:
            return False, "CRITICAL: evaluation_time not provided - determinism violation"
        current_ts = evaluation_time
        
        content_age = current_ts - content_created
        window_duration = metric_window_end - content_created
        
        if window_duration > content_age + 60:  # 60s tolerance
            return False, f"Metric window {window_duration:.0f}s exceeds content age {content_age:.0f}s"
        return True, None
    
    @staticmethod
    def early_signal_computed_in_window(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Early signals must be computed within their designated window"""
        if not data.get('is_early_signal', False):
            return True, None
        
        content_created = data.get('content_created_at', 0)
        metric_computed = data.get('timestamp', 0)
        early_window_hours = data.get('early_window_hours', 24)
        
        age_at_computation = metric_computed - content_created
        max_age = early_window_hours * 3600
        
        if age_at_computation > max_age:
            return False, f"Early signal computed at {age_at_computation/3600:.1f}h, beyond {early_window_hours}h window"
        return True, None
    
    @staticmethod
    def long_tail_after_cold_start(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Long-tail metrics require minimum content age"""
        if not data.get('is_long_tail', False):
            return True, None
        
        content_created = data.get('content_created_at', 0)
        metric_computed = data.get('timestamp', 0)
        min_age_hours = data.get('min_tail_age_hours', 168)  # 7 days default
        
        age = (metric_computed - content_created) / 3600
        
        if age < min_age_hours:
            return False, f"Long-tail metric computed at {age:.1f}h, needs {min_age_hours}h minimum"
        return True, None
    
    @staticmethod
    def timestamps_monotonic(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Event timestamps must be strictly increasing"""
        events = data.get('events', [])
        if len(events) < 2:
            return True, None
        
        timestamps = [e.get('timestamp', 0) for e in events]
        for i in range(1, len(timestamps)):
            if timestamps[i] < timestamps[i-1]:
                return False, f"Non-monotonic timestamps at index {i}: {timestamps[i-1]} -> {timestamps[i]}"
        return True, None


# ============================================================================
# DATA INTEGRITY INVARIANTS
# ============================================================================

class DataIntegrityInvariants:
    """Detects data corruption, logging bugs, replay buffer poisoning"""
    
    @staticmethod
    def engagement_never_decreases(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Cumulative engagement counts are monotonic"""
        prev_count = data.get('previous_count', 0)
        current_count = data.get('current_count', 0)
        metric_type = data.get('metric_type', 'unknown')
        
        if current_count < prev_count:
            return False, f"{metric_type} decreased from {prev_count} to {current_count}"
        return True, None
    
    @staticmethod
    def engagement_funnel_ordering(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """views ≥ likes ≥ comments ≥ shares"""
        views = data.get('views', 0)
        likes = data.get('likes', 0)
        comments = data.get('comments', 0)
        shares = data.get('shares', 0)
        
        violations = []
        if likes > views:
            violations.append(f"likes ({likes}) > views ({views})")
        if comments > likes:
            violations.append(f"comments ({comments}) > likes ({likes})")
        if shares > comments:
            violations.append(f"shares ({shares}) > comments ({comments})")
        
        if violations:
            return False, "Engagement funnel violated: " + "; ".join(violations)
        return True, None
    
    @staticmethod
    def retention_bounded(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Retention must be in [0, 1]"""
        retention = data.get('retention', 0.5)
        
        if not (0 <= retention <= 1):
            return False, f"Retention {retention} outside [0, 1]"
        return True, None
    
    @staticmethod
    def probability_bounded(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """All probability values in [0, 1]"""
        for key, value in data.items():
            if 'probability' in key.lower() or 'prob' in key.lower():
                if not isinstance(value, (int, float)):
                    continue
                if not (0 <= value <= 1):
                    return False, f"{key} = {value} outside [0, 1]"
        return True, None
    
    @staticmethod
    def no_negative_counts(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Count metrics cannot be negative"""
        for key, value in data.items():
            if any(term in key.lower() for term in ['count', 'views', 'likes', 'shares', 'comments']):
                if isinstance(value, (int, float)) and value < 0:
                    return False, f"{key} is negative: {value}"
        return True, None


# ============================================================================
# CAUSALITY INVARIANTS (ANTI-LEAKAGE)
# ============================================================================

class CausalityInvariants:
    """Prevents future data leakage and false intelligence"""
    
    @staticmethod
    def no_post_boost_data_in_pre_boost_metrics(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Pre-boost metrics cannot contain post-boost data"""
        if not data.get('is_pre_boost_metric', False):
            return True, None
        
        boost_time = data.get('boost_applied_at', float('inf'))
        metric_window_end = data.get('metric_window_end', 0)
        
        if metric_window_end > boost_time:
            return False, f"Pre-boost metric window extends {metric_window_end - boost_time:.0f}s past boost application"
        return True, None
    
    @staticmethod
    def no_reward_influenced_observations(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """RL observations cannot include reward-influenced data"""
        if not data.get('is_rl_observation', False):
            return True, None
        
        last_reward_time = data.get('last_reward_applied_at', 0)
        metric_window_start = data.get('metric_window_start', float('inf'))
        
        if metric_window_start < last_reward_time:
            return False, f"RL observation includes data from after reward at {last_reward_time}"
        return True, None
    
    @staticmethod
    def no_future_aggregation_in_current_decision(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Decision metrics cannot aggregate future data"""
        if not data.get('is_decision_metric', False):
            return True, None
        
        # TIER-0 FIX: Use injected evaluation_time, not wall-clock time
        evaluation_time = data.get('evaluation_time')
        if evaluation_time is None:
            return False, "CRITICAL: evaluation_time not provided - determinism violation"
        decision_time = data.get('decision_time', evaluation_time)
        metric_window_end = data.get('metric_window_end', 0)
        
        if metric_window_end > decision_time:
            return False, f"Decision metric uses data {metric_window_end - decision_time:.0f}s in the future"
        return True, None
    
    @staticmethod
    def prediction_target_after_observation(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Prediction targets must be temporally after observations"""
        if not data.get('is_prediction_task', False):
            return True, None
        
        observation_end = data.get('observation_window_end', 0)
        target_start = data.get('target_window_start', float('inf'))
        
        if target_start <= observation_end:
            return False, f"Prediction target overlaps observation window"
        return True, None


# ============================================================================
# METRIC RELATIONSHIP INVARIANTS
# ============================================================================

class MetricRelationshipInvariants:
    """Ensures metrics agree with reality"""
    
    @staticmethod
    def velocity_requires_views(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Engagement velocity cannot rise if views are flat"""
        velocity_change = data.get('velocity_change', 0)
        views_change = data.get('views_change', 0)
        
        # If velocity increasing significantly but views flat/declining
        if velocity_change > 0.1 and views_change <= 0.01:
            return False, f"Velocity increased {velocity_change:.3f} but views only changed {views_change:.3f}"
        return True, None
    
    @staticmethod
    def tail_half_life_within_content_age(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Decay half-life cannot exceed total content age"""
        half_life = data.get('half_life_hours', 0)
        content_age_hours = data.get('content_age_hours', float('inf'))
        
        if half_life > content_age_hours:
            return False, f"Half-life {half_life:.1f}h exceeds content age {content_age_hours:.1f}h"
        return True, None
    
    @staticmethod
    def decay_probability_increases_with_inactivity(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Longer inactivity periods should have higher decay probability"""
        if not data.get('is_decay_metric', False):
            return True, None
        
        inactivity_hours = data.get('hours_since_last_engagement', 0)
        decay_prob = data.get('decay_probability', 0)
        expected_min_prob = min(0.9, inactivity_hours / 168.0)  # Saturates at 7 days
        
        if decay_prob < expected_min_prob * 0.5:  # Allow 50% tolerance
            return False, f"Decay prob {decay_prob:.3f} too low for {inactivity_hours:.1f}h inactivity"
        return True, None
    
    @staticmethod
    def retention_collapse_precedes_engagement_decay(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Retention must drop before engagement drops"""
        retention_trend = data.get('retention_trend', 0)  # negative = dropping
        engagement_trend = data.get('engagement_trend', 0)
        
        # If engagement dropping significantly but retention still high
        if engagement_trend < -0.2 and retention_trend > -0.05:
            return False, f"Engagement declining {engagement_trend:.3f} but retention stable {retention_trend:.3f}"
        return True, None


# ============================================================================
# SCALE INVARIANTS (ANTI-INFLATION)
# ============================================================================

class ScaleInvariants:
    """Prevents fake virality and artificial inflation"""
    
    @staticmethod
    def velocity_relative_to_account_size(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Velocity must be normalized by creator's follower count"""
        velocity = data.get('velocity', 0)
        followers = data.get('creator_followers', 1)
        
        # Velocity shouldn't exceed 10x follower base per hour
        max_reasonable_velocity = followers * 10.0
        
        if velocity > max_reasonable_velocity:
            return False, f"Velocity {velocity:.0f} unrealistic for {followers} followers"
        return True, None
    
    @staticmethod
    def engagement_saturation_threshold(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Engagement rate cannot exceed platform physical limits"""
        engagement_rate = data.get('engagement_rate', 0)
        max_platform_rate = data.get('platform_max_engagement_rate', 0.5)  # 50% default
        
        if engagement_rate > max_platform_rate * 1.2:  # 20% tolerance
            return False, f"Engagement rate {engagement_rate:.3f} exceeds platform limit {max_platform_rate:.3f}"
        return True, None
    
    @staticmethod
    def platform_exposure_ceiling(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Content cannot reach more users than platform DAU"""
        unique_viewers = data.get('unique_viewers', 0)
        platform_dau = data.get('platform_daily_active_users', float('inf'))
        
        if unique_viewers > platform_dau:
            return False, f"Viewers {unique_viewers} exceeds platform DAU {platform_dau}"
        return True, None
    
    @staticmethod
    def entropy_collapse_detection(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Engagement distribution shouldn't collapse to single user"""
        engagement_distribution = data.get('engagement_distribution', [])
        if not engagement_distribution:
            return True, None
        
        total = sum(engagement_distribution)
        if total == 0:
            return True, None
        
        max_share = max(engagement_distribution) / total
        
        # Single user shouldn't account for >80% of engagement
        if max_share > 0.8:
            return False, f"Engagement entropy collapsed: {max_share:.1%} from single source"
        return True, None


# ============================================================================
# CROSS-PLATFORM INVARIANTS
# ============================================================================

class CrossPlatformInvariants:
    """Protects multi-platform consistency"""
    
    @staticmethod
    def normalized_engagement_alignment(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Normalized engagement ratios should align across platforms"""
        platform_metrics = data.get('platform_metrics', {})
        if len(platform_metrics) < 2:
            return True, None
        
        normalized_rates = {}
        for platform, metrics in platform_metrics.items():
            views = metrics.get('views', 1)
            engagement = metrics.get('total_engagement', 0)
            normalized_rates[platform] = engagement / max(views, 1)
        
        rates = list(normalized_rates.values())
        if not rates:
            return True, None
        
        mean_rate = np.mean(rates)
        max_deviation = max(abs(r - mean_rate) / max(mean_rate, 0.01) for r in rates)
        
        # Platforms shouldn't deviate >5x from mean
        if max_deviation > 5.0:
            return False, f"Cross-platform engagement deviation: {max_deviation:.2f}x"
        return True, None
    
    @staticmethod
    def repost_decay_faster_than_original(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Reposted content should decay faster than originals"""
        if not data.get('is_repost', False):
            return True, None
        
        repost_half_life = data.get('half_life_hours', float('inf'))
        original_half_life = data.get('original_half_life_hours', 0)
        
        if repost_half_life > original_half_life * 1.2:
            return False, f"Repost half-life {repost_half_life:.1f}h exceeds original {original_half_life:.1f}h"
        return True, None
    
    @staticmethod
    def platform_mechanics_respected(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Platform-specific constraints must be honored"""
        platform = data.get('platform', '')
        max_length = data.get('content_length', 0)
        
        platform_limits = {
            'twitter': 280,
            'instagram_caption': 2200,
            'tiktok_duration': 600,  # seconds
        }
        
        if platform in platform_limits:
            limit = platform_limits[platform]
            if max_length > limit:
                return False, f"{platform} content length {max_length} exceeds limit {limit}"
        
        return True, None
    
    @staticmethod
    def no_cross_platform_leakage_artifacts(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """No cross-platform data leakage in metrics"""
        platform_metrics = data.get('platform_metrics', {})
        if len(platform_metrics) < 2:
            return True, None
        
        # Check for impossible cross-platform correlations
        platforms = list(platform_metrics.keys())
        for i, platform1 in enumerate(platforms):
            for platform2 in platforms[i+1:]:
                metrics1 = platform_metrics[platform1]
                metrics2 = platform_metrics[platform2]
                
                # Check if metrics from different platforms have identical values
                # (suggests data leakage or copying)
                if 'timestamp' in metrics1 and 'timestamp' in metrics2:
                    if abs(metrics1['timestamp'] - metrics2['timestamp']) < 0.001:
                        # Same timestamp across platforms is suspicious
                        if metrics1.get('views', 0) == metrics2.get('views', 0) and metrics1.get('views', 0) > 0:
                            return False, f"Identical metrics between {platform1} and {platform2} suggest leakage"
        
        return True, None


# ============================================================================
# DISTRIBUTION INVARIANTS (LONG TAIL SAFETY)
# ============================================================================

class DistributionInvariants:
    """Ensures tail distributions are real"""
    
    @staticmethod
    def long_tail_mass_threshold(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Long-tail engagement must exceed minimum mass"""
        if not data.get('is_long_tail', False):
            return True, None
        
        tail_engagement = data.get('tail_engagement_total', 0)
        min_mass = data.get('min_tail_mass', 100)  # Platform-specific
        
        if tail_engagement < min_mass:
            return False, f"Long-tail mass {tail_engagement} below threshold {min_mass}"
        return True, None
    
    @staticmethod
    def tail_variance_bounded(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Tail engagement variance must be within reasonable bounds"""
        tail_samples = data.get('tail_engagement_samples', [])
        if len(tail_samples) < 10:
            return True, None
        
        mean_engagement = np.mean(tail_samples)
        std_engagement = np.std(tail_samples)
        
        # Coefficient of variation shouldn't exceed 10 for established tails
        if mean_engagement > 0:
            cv = std_engagement / mean_engagement
            if cv > 10.0:
                return False, f"Tail variance too high: CV = {cv:.2f}"
        
        return True, None
    
    @staticmethod
    def decay_curves_monotonic(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Engagement decay should be monotonic in expectation"""
        decay_samples = data.get('decay_curve', [])
        if len(decay_samples) < 5:
            return True, None
        
        # Check for sustained increases (allowing noise)
        window_size = 3
        for i in range(len(decay_samples) - window_size):
            window = decay_samples[i:i+window_size]
            if all(window[j] < window[j+1] for j in range(len(window)-1)):
                # Sustained increase detected
                return False, f"Decay curve non-monotonic at index {i}: {window}"
        
        return True, None


# ============================================================================
# SAFETY INVARIANTS (SYSTEM SURVIVAL)
# ============================================================================

class SafetyInvariants:
    """Critical system health checks"""
    
    @staticmethod
    def metric_divergence_across_agents(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Metrics shouldn't diverge wildly across agents"""
        agent_metrics = data.get('agent_metrics', {})
        if len(agent_metrics) < 2:
            return True, None
        
        values = list(agent_metrics.values())
        mean_val = np.mean(values)
        max_val = max(values)
        min_val = min(values)
        
        if mean_val > 0:
            divergence = (max_val - min_val) / mean_val
            if divergence > 3.0:  # 3x divergence threshold
                return False, f"Agent metric divergence: {divergence:.2f}x"
        
        return True, None
    
    @staticmethod
    def reward_metric_coupling(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Reward and metrics shouldn't become perfectly correlated"""
        if not data.get('check_reward_coupling', False):
            return True, None
        
        correlation = data.get('reward_metric_correlation', 0)
        
        # Perfect correlation (>0.99) suggests reward hacking
        if abs(correlation) > 0.99:
            return False, f"Reward-metric correlation too high: {correlation:.4f}"
        
        return True, None
    
    @staticmethod
    def sustained_invariant_erosion(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Check for gradual invariant weakening over time"""
        violation_rate = data.get('invariant_violation_rate', 0)
        violation_trend = data.get('violation_trend', 0)  # positive = increasing
        
        # Violation rate increasing and exceeding 5%
        if violation_rate > 0.05 and violation_trend > 0:
            return False, f"Invariant erosion detected: {violation_rate:.2%} and rising"
        
        return True, None
    
    @staticmethod
    def unexplained_metric_jumps(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Detect sudden metric changes without explanation"""
        metric_change = data.get('metric_change_percent', 0)
        has_explanation = data.get('has_change_explanation', False)
        
        # >50% change without explanation
        if abs(metric_change) > 0.5 and not has_explanation:
            return False, f"Unexplained metric jump: {metric_change:.1%}"
        
        return True, None


# ============================================================================
# SCALE STRESS INVARIANTS (ADVERSARIAL, RACES, CROSS-DC)
# ============================================================================

class ScaleStressInvariants:
    """Invariants for adversarial scenarios, race conditions, cross-DC issues"""
    
    @staticmethod
    def no_adversarial_burst_artifacts(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Detect adversarial burst patterns (sudden spikes from single source)"""
        engagement_timeline = data.get('engagement_timeline', [])
        if len(engagement_timeline) < 10:
            return True, None
        
        # Check for burst: >80% of engagement in <5% of timeline
        total_engagement = sum(e.get('count', 0) for e in engagement_timeline)
        if total_engagement == 0:
            return True, None
        
        # Find max burst window (5% of timeline)
        window_size = max(1, len(engagement_timeline) // 20)
        max_burst = 0
        
        for i in range(len(engagement_timeline) - window_size):
            window_engagement = sum(
                engagement_timeline[j].get('count', 0) 
                for j in range(i, i + window_size)
            )
            burst_ratio = window_engagement / total_engagement
            max_burst = max(max_burst, burst_ratio)
        
        if max_burst > 0.8:
            return False, f"Adversarial burst detected: {max_burst:.1%} of engagement in {window_size}-event window"
        
        return True, None
    
    @staticmethod
    def no_partial_order_race_conditions(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Detect partial-order event races (events out of causal order)"""
        events = data.get('events', [])
        if len(events) < 2:
            return True, None
        
        # Check for events with same timestamp but different causal order
        timestamp_groups = defaultdict(list)
        for i, event in enumerate(events):
            ts = event.get('timestamp', 0)
            timestamp_groups[ts].append((i, event))
        
        # Events with identical timestamps should have consistent ordering
        for ts, group in timestamp_groups.items():
            if len(group) > 1:
                # Check if causal dependencies are violated
                for i, (idx1, event1) in enumerate(group):
                    for idx2, event2 in group[i+1:]:
                        # If event1 depends on event2 but event2 comes after, it's a race
                        if event1.get('depends_on') == event2.get('event_id'):
                            if idx1 > idx2:
                                return False, f"Partial-order race: event {event1.get('event_id')} depends on {event2.get('event_id')} but appears after"
        
        return True, None
    
    @staticmethod
    def no_shard_inconsistency(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Detect shard inconsistency (same metric different values across shards)"""
        shard_metrics = data.get('shard_metrics', {})
        if len(shard_metrics) < 2:
            return True, None
        
        # Check for same metric having different values across shards
        metric_values = defaultdict(list)
        for shard_id, metrics in shard_metrics.items():
            for metric_name, value in metrics.items():
                metric_values[metric_name].append((shard_id, value))
        
        for metric_name, values in metric_values.items():
            if len(values) < 2:
                continue
            
            # For cumulative metrics, values should be consistent or explainable
            unique_values = set(v[1] for v in values)
            if len(unique_values) > 1:
                # Check if difference is explainable (e.g., different time windows)
                max_val = max(v[1] for v in values)
                min_val = min(v[1] for v in values)
                if max_val > 0:
                    relative_diff = (max_val - min_val) / max_val
                    # >10% difference without explanation is suspicious
                    if relative_diff > 0.1 and not data.get('shard_differences_explained', False):
                        return False, f"Shard inconsistency: {metric_name} varies {relative_diff:.1%} across shards"
        
        return True, None
    
    @staticmethod
    def cross_dc_clock_skew_bounded(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Detect cross-DC clock skew violations"""
        dc_timestamps = data.get('dc_timestamps', {})
        if len(dc_timestamps) < 2:
            return True, None
        
        # Check maximum clock skew between DCs
        timestamps = list(dc_timestamps.values())
        max_skew = max(timestamps) - min(timestamps)
        max_allowed_skew = data.get('max_allowed_clock_skew_seconds', 5.0)
        
        if max_skew > max_allowed_skew:
            return False, f"Cross-DC clock skew {max_skew:.2f}s exceeds limit {max_allowed_skew}s"
        
        return True, None
    
    @staticmethod
    def no_event_reordering_artifacts(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Detect event reordering artifacts (events processed out of order)"""
        events = data.get('events', [])
        if len(events) < 2:
            return True, None
        
        # Check for events that should be ordered but aren't
        for i in range(len(events) - 1):
            event1 = events[i]
            event2 = events[i + 1]
            
            # If event2 has earlier timestamp but later sequence number, it's reordered
            ts1 = event1.get('timestamp', 0)
            ts2 = event2.get('timestamp', 0)
            seq1 = event1.get('sequence_number', 0)
            seq2 = event2.get('sequence_number', 0)
            
            if ts2 < ts1 and seq2 > seq1:
                return False, f"Event reordering: event {seq2} at {ts2} appears after event {seq1} at {ts1}"
        
        return True, None


# ============================================================================
# DRIFT INVARIANTS (CHANGE 2: First-Class Invariant)
# ============================================================================

class DriftInvariants:
    """CHANGE 2: Drift becomes a first-class invariant violation"""
    
    @staticmethod
    def drift_violation(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        CHANGE 2: Drift detection as a registered invariant.
        This makes drift illegal reality, not just a report.
        """
        # Get registry from data context
        registry = data.get('_registry')
        if registry is None:
            return True, None  # Can't check without registry
        
        # FIX REGRESSION: Use _now() helper - requires evaluation_time in data
        try:
            evaluation_time = _now(data)
        except ValueError as e:
            # If evaluation_time missing, fail hard
            return False, str(e)
        
        drift_result = registry.detect_drift(evaluation_time=evaluation_time)
        
        if drift_result.get('drift_detected', False):
            drifting = drift_result.get('drifting_invariants', [])
            if drifting:
                drift_names = [d['invariant'] for d in drifting]
                return False, f"Invariant drift detected — system integrity compromised: {', '.join(drift_names)}"
        
        return True, None


# ============================================================================
# REALITY CONSISTENCY INVARIANTS (CHANGE 5: Meta-Invariant)
# ============================================================================

class RealityConsistencyInvariants:
    """CHANGE 5: Meta-invariant for reality consistency"""
    
    @staticmethod
    def metric_reality_stability(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        CHANGE 5: Prevents silent metric redefinitions.
        This is audit-grade reality locking.
        """
        if data.get("metric_version_changed", False):
            if not data.get("schema_migration_id"):
                return False, "Metric definition changed without migration ID — silent redefinition detected"
        
        return True, None


# ============================================================================
# INVARIANT REGISTRY
# ============================================================================

class InvariantRegistry:
    """Central registry of all invariants"""
    
    def __init__(self):
        self.invariants: List[InvariantSpec] = []
        self.violations: List[InvariantViolation] = []
        self.violation_counts: Dict[str, int] = defaultdict(int)
        self._register_all_invariants()
    
    def _register_all_invariants(self):
        """Register all invariants with proper specs"""
        
        # CHANGE 1: Determinism Invariant (MUST BE FIRST - checks evaluation_time)
        self._register(InvariantSpec(
            name="evaluation_time_required",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["all_metrics"],
            check_fn=DeterminismInvariants.evaluation_time_required,
            remediation=RemediationAction.HALT_PIPELINE,
            description="evaluation_time must be provided - wall-clock time banned in invariant checks",
            category="determinism"
        ))
        
        # Temporal Invariants (CRITICAL)
        self._register(InvariantSpec(
            name="no_future_timestamps",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["all_metrics"],
            check_fn=TemporalInvariants.no_future_timestamps,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Metric timestamps cannot be in the future",
            category="temporal"
        ))
        
        self._register(InvariantSpec(
            name="metric_window_within_content_age",
            scope=InvariantScope.TEMPORAL,
            severity=Severity.CRITICAL,
            applies_to=["all_metrics"],
            check_fn=TemporalInvariants.metric_window_within_content_age,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Metric window cannot exceed content age",
            category="temporal"
        ))
        
        self._register(InvariantSpec(
            name="early_signal_computed_in_window",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["early_signals"],
            check_fn=TemporalInvariants.early_signal_computed_in_window,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Early signals must be computed within designated window",
            category="temporal"
        ))
        
        self._register(InvariantSpec(
            name="long_tail_after_cold_start",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["long_tail_metrics"],
            check_fn=TemporalInvariants.long_tail_after_cold_start,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Long-tail metrics require minimum content age",
            category="temporal"
        ))
        
        self._register(InvariantSpec(
            name="timestamps_monotonic",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["event_streams"],
            check_fn=TemporalInvariants.timestamps_monotonic,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Event timestamps must be strictly increasing",
            category="temporal"
        ))
        
        # Data Integrity Invariants
        self._register(InvariantSpec(
            name="engagement_never_decreases",
            scope=InvariantScope.GLOBAL,
            severity=Severity.HIGH,
            applies_to=["cumulative_metrics"],
            check_fn=DataIntegrityInvariants.engagement_never_decreases,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Cumulative counts must be monotonic",
            category="data_integrity"
        ))
        
        self._register(InvariantSpec(
            name="engagement_funnel_ordering",
            scope=InvariantScope.GLOBAL,
            severity=Severity.HIGH,
            applies_to=["engagement_metrics"],
            check_fn=DataIntegrityInvariants.engagement_funnel_ordering,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="views ≥ likes ≥ comments ≥ shares",
            category="data_integrity"
        ))
        
        self._register(InvariantSpec(
            name="retention_bounded",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["retention_metrics"],
            check_fn=DataIntegrityInvariants.retention_bounded,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Retention ∈ [0, 1]",
            category="data_integrity"
        ))
        
        self._register(InvariantSpec(
            name="probability_bounded",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["probability_metrics"],
            check_fn=DataIntegrityInvariants.probability_bounded,
            remediation=RemediationAction.HALT_PIPELINE,
            description="All probabilities ∈ [0, 1]",
            category="data_integrity"
        ))
        
        self._register(InvariantSpec(
            name="no_negative_counts",
            scope=InvariantScope.GLOBAL,
            severity=Severity.HIGH,
            applies_to=["count_metrics"],
            check_fn=DataIntegrityInvariants.no_negative_counts,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Count metrics cannot be negative",
            category="data_integrity"
        ))
        
        # Causality Invariants (ANTI-LEAKAGE)
        self._register(InvariantSpec(
            name="no_post_boost_data_in_pre_boost_metrics",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["pre_boost_metrics"],
            check_fn=CausalityInvariants.no_post_boost_data_in_pre_boost_metrics,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Pre-boost metrics cannot contain post-boost data",
            category="causality"
        ))
        
        self._register(InvariantSpec(
            name="no_reward_influenced_observations",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["rl_observations"],
            check_fn=CausalityInvariants.no_reward_influenced_observations,
            remediation=RemediationAction.HALT_PIPELINE,
            description="RL observations cannot include reward-influenced data",
            category="causality"
        ))
        
        self._register(InvariantSpec(
            name="no_future_aggregation_in_current_decision",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["decision_metrics"],
            check_fn=CausalityInvariants.no_future_aggregation_in_current_decision,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Decision metrics cannot aggregate future data",
            category="causality"
        ))
        
        self._register(InvariantSpec(
            name="prediction_target_after_observation",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["prediction_tasks"],
            check_fn=CausalityInvariants.prediction_target_after_observation,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Prediction targets must be temporally after observations",
            category="causality"
        ))
        
        # Metric Relationship Invariants
        self._register(InvariantSpec(
            name="velocity_requires_views",
            scope=InvariantScope.CROSS_METRIC,
            severity=Severity.MEDIUM,
            applies_to=["velocity_metrics"],
            check_fn=MetricRelationshipInvariants.velocity_requires_views,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Velocity cannot rise if views are flat",
            category="metric_relationships"
        ))
        
        self._register(InvariantSpec(
            name="tail_half_life_within_content_age",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["decay_metrics"],
            check_fn=MetricRelationshipInvariants.tail_half_life_within_content_age,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Decay half-life cannot exceed content age",
            category="metric_relationships"
        ))
        
        self._register(InvariantSpec(
            name="decay_probability_increases_with_inactivity",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.MEDIUM,
            applies_to=["decay_metrics"],
            check_fn=MetricRelationshipInvariants.decay_probability_increases_with_inactivity,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Longer inactivity should increase decay probability",
            category="metric_relationships"
        ))
        
        self._register(InvariantSpec(
            name="retention_collapse_precedes_engagement_decay",
            scope=InvariantScope.CROSS_METRIC,
            severity=Severity.MEDIUM,
            applies_to=["engagement_metrics"],
            check_fn=MetricRelationshipInvariants.retention_collapse_precedes_engagement_decay,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Retention must drop before engagement",
            category="metric_relationships"
        ))
        
        # Scale Invariants (ANTI-INFLATION)
        self._register(InvariantSpec(
            name="velocity_relative_to_account_size",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["velocity_metrics"],
            check_fn=ScaleInvariants.velocity_relative_to_account_size,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Velocity must be normalized by follower count",
            category="scale"
        ))
        
        self._register(InvariantSpec(
            name="engagement_saturation_threshold",
            scope=InvariantScope.PLATFORM_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["engagement_metrics"],
            check_fn=ScaleInvariants.engagement_saturation_threshold,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Engagement rate cannot exceed platform limits",
            category="scale"
        ))
        
        self._register(InvariantSpec(
            name="platform_exposure_ceiling",
            scope=InvariantScope.PLATFORM_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["reach_metrics"],
            check_fn=ScaleInvariants.platform_exposure_ceiling,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Content cannot reach more users than platform DAU",
            category="scale"
        ))
        
        self._register(InvariantSpec(
            name="entropy_collapse_detection",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.MEDIUM,
            applies_to=["distribution_metrics"],
            check_fn=ScaleInvariants.entropy_collapse_detection,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Engagement distribution shouldn't collapse",
            category="scale"
        ))
        
        # Cross-Platform Invariants
        self._register(InvariantSpec(
            name="normalized_engagement_alignment",
            scope=InvariantScope.PLATFORM_SPECIFIC,
            severity=Severity.MEDIUM,
            applies_to=["cross_platform_metrics"],
            check_fn=CrossPlatformInvariants.normalized_engagement_alignment,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Normalized engagement should align across platforms",
            category="cross_platform"
        ))
        
        self._register(InvariantSpec(
            name="repost_decay_faster_than_original",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.MEDIUM,
            applies_to=["decay_metrics"],
            check_fn=CrossPlatformInvariants.repost_decay_faster_than_original,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Reposts should decay faster than originals",
            category="cross_platform"
        ))
        
        self._register(InvariantSpec(
            name="platform_mechanics_respected",
            scope=InvariantScope.PLATFORM_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["all_metrics"],
            check_fn=CrossPlatformInvariants.platform_mechanics_respected,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Platform-specific constraints must be honored",
            category="cross_platform"
        ))
        
        self._register(InvariantSpec(
            name="no_cross_platform_leakage_artifacts",
            scope=InvariantScope.PLATFORM_SPECIFIC,
            severity=Severity.CRITICAL,
            applies_to=["cross_platform_metrics"],
            check_fn=CrossPlatformInvariants.no_cross_platform_leakage_artifacts,
            remediation=RemediationAction.HALT_PIPELINE,
            description="No cross-platform data leakage artifacts",
            category="cross_platform"
        ))
        
        # Distribution Invariants (LONG TAIL SAFETY)
        self._register(InvariantSpec(
            name="long_tail_mass_threshold",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.HIGH,
            applies_to=["long_tail_metrics"],
            check_fn=DistributionInvariants.long_tail_mass_threshold,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Long-tail engagement must exceed minimum mass",
            category="distribution"
        ))
        
        self._register(InvariantSpec(
            name="tail_variance_bounded",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.MEDIUM,
            applies_to=["long_tail_metrics"],
            check_fn=DistributionInvariants.tail_variance_bounded,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Tail variance must be within bounds",
            category="distribution"
        ))
        
        self._register(InvariantSpec(
            name="decay_curves_monotonic",
            scope=InvariantScope.METRIC_SPECIFIC,
            severity=Severity.MEDIUM,
            applies_to=["decay_metrics"],
            check_fn=DistributionInvariants.decay_curves_monotonic,
            remediation=RemediationAction.FLAG_AND_CONTINUE,
            description="Decay curves should be monotonic in expectation",
            category="distribution"
        ))
        
        # Safety Invariants (SYSTEM SURVIVAL)
        self._register(InvariantSpec(
            name="metric_divergence_across_agents",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["agent_metrics"],
            check_fn=SafetyInvariants.metric_divergence_across_agents,
            remediation=RemediationAction.FREEZE_AGENT,
            description="Metrics shouldn't diverge wildly across agents",
            category="safety"
        ))
        
        self._register(InvariantSpec(
            name="reward_metric_coupling",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["rl_metrics"],
            check_fn=SafetyInvariants.reward_metric_coupling,
            remediation=RemediationAction.PAUSE_TRAINING,
            description="Reward and metrics shouldn't be perfectly correlated",
            category="safety"
        ))
        
        self._register(InvariantSpec(
            name="sustained_invariant_erosion",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["all_metrics"],
            check_fn=SafetyInvariants.sustained_invariant_erosion,
            remediation=RemediationAction.ALERT_HUMANS,
            description="Detect gradual invariant weakening",
            category="safety"
        ))
        
        self._register(InvariantSpec(
            name="unexplained_metric_jumps",
            scope=InvariantScope.GLOBAL,
            severity=Severity.HIGH,
            applies_to=["all_metrics"],
            check_fn=SafetyInvariants.unexplained_metric_jumps,
            remediation=RemediationAction.HALT_BOOST,
            description="Detect sudden metric changes without explanation",
            category="safety"
        ))
        
        # Scale Stress Invariants (TIER-0: Adversarial, Races, Cross-DC)
        self._register(InvariantSpec(
            name="no_adversarial_burst_artifacts",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["engagement_metrics"],
            check_fn=ScaleStressInvariants.no_adversarial_burst_artifacts,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Detect adversarial burst patterns",
            category="scale_stress"
        ))
        
        self._register(InvariantSpec(
            name="no_partial_order_race_conditions",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["event_streams"],
            check_fn=ScaleStressInvariants.no_partial_order_race_conditions,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Detect partial-order event races",
            category="scale_stress"
        ))
        
        self._register(InvariantSpec(
            name="no_shard_inconsistency",
            scope=InvariantScope.GLOBAL,
            severity=Severity.HIGH,
            applies_to=["distributed_metrics"],
            check_fn=ScaleStressInvariants.no_shard_inconsistency,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Detect shard inconsistency",
            category="scale_stress"
        ))
        
        self._register(InvariantSpec(
            name="cross_dc_clock_skew_bounded",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["cross_dc_metrics"],
            check_fn=ScaleStressInvariants.cross_dc_clock_skew_bounded,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Detect cross-DC clock skew violations",
            category="scale_stress"
        ))
        
        self._register(InvariantSpec(
            name="no_event_reordering_artifacts",
            scope=InvariantScope.GLOBAL,
            severity=Severity.HIGH,
            applies_to=["event_streams"],
            check_fn=ScaleStressInvariants.no_event_reordering_artifacts,
            remediation=RemediationAction.QUARANTINE_METRIC,
            description="Detect event reordering artifacts",
            category="scale_stress"
        ))
        
        # CHANGE 2: Drift as First-Class Invariant
        self._register(InvariantSpec(
            name="invariant_drift_detected",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["all_metrics"],
            check_fn=DriftInvariants.drift_violation,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Invariant drift detected — system integrity compromised",
            category="drift_enforcement"
        ))
        
        # CHANGE 5: Reality Consistency Meta-Invariant
        self._register(InvariantSpec(
            name="metric_reality_stability",
            scope=InvariantScope.GLOBAL,
            severity=Severity.CRITICAL,
            applies_to=["all_metrics"],
            check_fn=RealityConsistencyInvariants.metric_reality_stability,
            remediation=RemediationAction.HALT_PIPELINE,
            description="Metric definition changed without migration — silent redefinition detected",
            category="reality_consistency"
        ))
    
    def _register(self, spec: InvariantSpec):
        """Register an invariant"""
        self.invariants.append(spec)
    
    def check_all(self, data: Dict[str, Any], metric_name: str = "unknown") -> List[InvariantViolation]:
        """Check all applicable invariants"""
        violations = []
        
        # CHANGE 1: evaluation_time is now MANDATORY - no fallback to wall-clock
        # The DeterminismInvariant will catch this and fail hard
        evaluation_time = data.get('evaluation_time')
        if evaluation_time is None:
            # Don't inject - let the DeterminismInvariant catch it
            # This ensures strict determinism
            pass
        
        # CHANGE 2: Inject registry into data for drift invariant
        data_with_registry = data.copy()
        data_with_registry['_registry'] = self
        
        for spec in self.invariants:
            # Check if invariant applies to this metric
            if not self._applies_to_metric(spec, metric_name, data_with_registry):
                continue
            
            # Run the check
            passed, message = spec.check_fn(data_with_registry)
            
            if not passed:
                violation = InvariantViolation(
                    invariant_name=spec.name,
                    severity=spec.severity,
                    timestamp=evaluation_time,  # TIER-0 FIX: Use evaluation_time, not wall-clock
                    context=data.copy(),
                    message=message or "Invariant violated",
                    remediation_taken=spec.remediation,
                    metric_name=metric_name,
                    platform=data.get('platform')
                )
                
                violations.append(violation)
                self.violations.append(violation)
                self.violation_counts[spec.name] += 1
                
                # Update drift detection
                spec.violation_count += 1
                spec.last_violation_time = evaluation_time  # CHANGE 1: Use evaluation_time
                spec.violation_rate_window.append(evaluation_time)
                
                # Keep last 1000 violations for rate calculation
                if len(spec.violation_rate_window) > 1000:
                    spec.violation_rate_window.pop(0)
                
                # POLISH 2: Auto-escalation for repeated MEDIUM violations
                if spec.severity == Severity.MEDIUM:
                    spec.medium_violation_count += 1
                    spec.medium_violation_window.append(evaluation_time)
                    
                    # Keep last 100 violations for escalation check
                    if len(spec.medium_violation_window) > 100:
                        spec.medium_violation_window.pop(0)
                    
                    # Auto-escalate if violation rate exceeds threshold
                    if not spec.escalated_to_high and len(spec.medium_violation_window) >= 10:
                        # Check violation rate in last hour
                        cutoff_time = evaluation_time - 3600
                        recent_medium = [t for t in spec.medium_violation_window if t > cutoff_time]
                        medium_rate = len(recent_medium) / 3600.0  # violations per second
                        
                        # Escalate if >5 violations per hour
                        if medium_rate > 5.0 / 3600.0:
                            spec.escalated_to_high = True
                            # Create escalation violation
                            escalation_violation = InvariantViolation(
                                invariant_name=f"auto_escalation_{spec.name}",
                                severity=Severity.HIGH,
                                timestamp=evaluation_time,
                                context={
                                    'original_invariant': spec.name,
                                    'medium_violation_rate': medium_rate,
                                    'recent_medium_violations': len(recent_medium)
                                },
                                message=f"MEDIUM invariant {spec.name} auto-escalated to HIGH: {medium_rate*3600:.1f} violations/hour",
                                remediation_taken=RemediationAction.QUARANTINE_METRIC,
                                metric_name=metric_name,
                                platform=data.get('platform')
                            )
                            violations.append(escalation_violation)
                            self.violations.append(escalation_violation)
                            self.violation_counts[escalation_violation.invariant_name] += 1
        
        return violations
    
    def _applies_to_metric(self, spec: InvariantSpec, metric_name: str, data: Dict[str, Any]) -> bool:
        """Check if invariant applies to this metric"""
        if "all_metrics" in spec.applies_to:
            return True
        
        if metric_name in spec.applies_to:
            return True
        
        # Check category matching
        metric_category = data.get('metric_category', '')
        if metric_category in spec.applies_to:
            return True
        
        return False
    
    def get_violation_summary(self, evaluation_time: Optional[float] = None) -> Dict[str, Any]:
        """Get summary of all violations"""
        # FIX REGRESSION: Allow wall-clock ONLY for stats/logging (system monitoring, not metric truth)
        if evaluation_time is None:
            evaluation_time = time.time()  # OK for stats/logging (system monitoring)
        
        total_violations = len(self.violations)
        
        by_severity = defaultdict(int)
        by_category = defaultdict(int)
        by_invariant = defaultdict(int)
        
        for v in self.violations:
            by_severity[v.severity.value] += 1
            by_invariant[v.invariant_name] += 1
        
        # FIX REGRESSION: Use evaluation_time parameter, not wall-clock
        recent_violations = [v for v in self.violations if evaluation_time - v.timestamp < 3600]
        
        return {
            'total_violations': total_violations,
            'recent_violations_1h': len(recent_violations),
            'by_severity': dict(by_severity),
            'by_invariant': dict(by_invariant),
            'most_violated': sorted(by_invariant.items(), key=lambda x: x[1], reverse=True)[:10]
        }
    
    def detect_drift(self, lookback_hours: float = 24, evaluation_time: Optional[float] = None) -> Dict[str, Any]:
        """Detect invariants showing increasing violation rates"""
        # FIX REGRESSION: Allow wall-clock ONLY for system monitoring (not metric truth)
        if evaluation_time is None:
            evaluation_time = time.time()  # OK for system monitoring
        
        drift_detected = []
        cutoff_time = evaluation_time - (lookback_hours * 3600)
        
        for spec in self.invariants:
            recent_violations = [t for t in spec.violation_rate_window if t > cutoff_time]
            
            if len(recent_violations) < 10:
                continue
            
            violation_rate = len(recent_violations) / lookback_hours
            
            # Check if rate is increasing
            if len(recent_violations) >= 20:
                first_half = [t for t in recent_violations[:len(recent_violations)//2]]
                second_half = [t for t in recent_violations[len(recent_violations)//2:]]
                
                first_rate = len(first_half) / (lookback_hours / 2)
                second_rate = len(second_half) / (lookback_hours / 2)
                
                if second_rate > first_rate * 1.5:  # 50% increase
                    drift_detected.append({
                        'invariant': spec.name,
                        'category': spec.category,
                        'severity': spec.severity.value,
                        'violation_rate': violation_rate,
                        'rate_increase': (second_rate / max(first_rate, 0.01)) - 1,
                        'total_violations': spec.violation_count
                    })
        
        drift_result = {
            'drift_detected': len(drift_detected) > 0,
            'drifting_invariants': drift_detected
        }
        
        # FIX REGRESSION: Enforce drift escalation - drift itself becomes a violation
        # Note: evaluation_time is now a parameter, so it's available in this scope
        if drift_result['drift_detected']:
            for drift_info in drift_detected:
                # Create violation for drift
                drift_violation = InvariantViolation(
                    invariant_name=f"drift_escalation_{drift_info['invariant']}",
                    severity=Severity.CRITICAL,
                    timestamp=evaluation_time,  # FIX REGRESSION: Use evaluation_time parameter
                    context={'drift_info': drift_info},
                    message=f"Invariant {drift_info['invariant']} showing drift: {drift_info['rate_increase']:.1%} increase",
                    remediation_taken=RemediationAction.ALERT_HUMANS,
                    metric_name="system_wide",
                    platform=None
                )
                self.violations.append(drift_violation)
                self.violation_counts[drift_violation.invariant_name] += 1
        
        return drift_result


# ============================================================================
# AUDIT LOGGING & DETERMINISM
# ============================================================================

class AuditLogger:
    """Comprehensive audit logging for legal defensibility"""
    
    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = log_dir or Path("data/invariant_audit_logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log: List[Dict[str, Any]] = []
        self.logger = logging.getLogger('metric_invariants.audit')
        self.lock = threading.Lock()
    
    def log_violation(self, violation: InvariantViolation, data_snapshot: Dict[str, Any]):
        """Log invariant violation for audit trail"""
        with self.lock:
            audit_entry = {
                'violation_id': violation.violation_id,
                'timestamp': datetime.fromtimestamp(violation.timestamp).isoformat(),
                'invariant_name': violation.invariant_name,
                'severity': violation.severity.value,
                'message': violation.message,
                'remediation': violation.remediation_taken.value,
                'metric_name': violation.metric_name,
                'platform': violation.platform,
                'data_snapshot': data_snapshot,
                'context_hash': hashlib.sha256(json.dumps(violation.context, sort_keys=True).encode()).hexdigest()[:16]
            }
            
            self.audit_log.append(audit_entry)
            
            # Flush to disk periodically
            if len(self.audit_log) >= 100:
                self._flush_audit_log()
    
    def _flush_audit_log(self):
        """Flush audit log to disk"""
        if not self.audit_log:
            return
        
        audit_file = self.log_dir / f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl"
        try:
            with open(audit_file, 'a') as f:
                for entry in self.audit_log:
                    f.write(json.dumps(entry, default=str) + "\n")
            self.audit_log.clear()
        except Exception as e:
            self.logger.error(f"Failed to flush audit log: {e}")
    
    def export_violations(self, start_time: Optional[float] = None, 
                          end_time: Optional[float] = None,
                          output_path: Optional[Path] = None) -> Path:
        """Export violations for audit purposes"""
        output_path = output_path or self.log_dir / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        # Load all audit files
        all_violations = []
        for audit_file in self.log_dir.glob("audit_*.jsonl"):
            try:
                with open(audit_file, 'r') as f:
                    for line in f:
                        entry = json.loads(line)
                        entry_time = datetime.fromisoformat(entry['timestamp']).timestamp()
                        if start_time and entry_time < start_time:
                            continue
                        if end_time and entry_time > end_time:
                            continue
                        all_violations.append(entry)
            except Exception as e:
                self.logger.warning(f"Failed to read {audit_file}: {e}")
        
        # Add in-memory violations
        for entry in self.audit_log:
            entry_time = datetime.fromisoformat(entry['timestamp']).timestamp()
            if start_time and entry_time < start_time:
                continue
            if end_time and entry_time > end_time:
                continue
            all_violations.append(entry)
        
        # Write export
        export_data = {
            'export_timestamp': datetime.now().isoformat(),
            'total_violations': len(all_violations),
            'start_time': datetime.fromtimestamp(start_time).isoformat() if start_time else None,
            'end_time': datetime.fromtimestamp(end_time).isoformat() if end_time else None,
            'violations': all_violations
        }
        
        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)
        
        return output_path


class DeterminismVerifier:
    """Verify that invariant checks are deterministic"""
    
    @staticmethod
    def verify_determinism(check_fn: Callable, test_data: Dict[str, Any], 
                          iterations: int = 3) -> Tuple[bool, Optional[str]]:
        """Verify that a check function produces identical results"""
        results = []
        for _ in range(iterations):
            result = check_fn(test_data)
            results.append(result)
        
        # All results should be identical
        first_result = results[0]
        for i, result in enumerate(results[1:], 1):
            if result != first_result:
                return False, f"Non-deterministic check: iteration {i} produced {result}, expected {first_result}"
        
        return True, None


# ============================================================================
# REMEDIATION ACTION HANDLERS
# ============================================================================

class RemediationHandler:
    """Handles remediation actions when invariants are violated"""
    
    def __init__(self):
        self.handlers: Dict[RemediationAction, Callable] = {
            RemediationAction.HALT_PIPELINE: self._halt_pipeline,
            RemediationAction.QUARANTINE_METRIC: self._quarantine_metric,
            RemediationAction.FLAG_AND_CONTINUE: self._flag_and_continue,
            RemediationAction.LOG_ONLY: self._log_only,
            RemediationAction.ALERT_HUMANS: self._alert_humans,
            RemediationAction.FREEZE_AGENT: self._freeze_agent,
            RemediationAction.HALT_BOOST: self._halt_boost,
            RemediationAction.PAUSE_TRAINING: self._pause_training,
        }
        self.quarantined_metrics: Set[str] = set()
        self.frozen_agents: Set[str] = set()
    
    def execute(self, action: RemediationAction, violation: InvariantViolation, 
                context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute remediation action"""
        handler = self.handlers.get(action)
        if handler:
            return handler(violation, context)
        return {'executed': False, 'error': f'Unknown action: {action}'}
    
    def _halt_pipeline(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Halt metric pipeline - TIER-0: System-binding enforcement"""
        # TIER-0 FIX: Actually throw exception to force halt
        raise InvariantViolationError(
            f"PIPELINE HALTED: {violation.invariant_name} - {violation.message}"
        )
    
    def _quarantine_metric(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Quarantine metric"""
        metric_name = violation.metric_name or 'unknown'
        self.quarantined_metrics.add(metric_name)
        return {
            'executed': True,
            'action': 'quarantine_metric',
            'metric_name': metric_name,
            'quarantined_metrics': list(self.quarantined_metrics)
        }
    
    def _flag_and_continue(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Flag violation and continue"""
        return {
            'executed': True,
            'action': 'flag_and_continue',
            'flagged': True
        }
    
    def _log_only(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Log only"""
        return {
            'executed': True,
            'action': 'log_only',
            'logged': True
        }
    
    def _alert_humans(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Alert humans"""
        return {
            'executed': True,
            'action': 'alert_humans',
            'alert_sent': True,
            'severity': violation.severity.value
        }
    
    def _freeze_agent(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Freeze agent - TIER-0: System-binding enforcement"""
        agent_id = context.get('agent_id', 'unknown')
        self.frozen_agents.add(agent_id)
        # TIER-0 FIX: Throw exception to force freeze
        raise InvariantViolationError(
            f"AGENT FROZEN: {agent_id} - {violation.invariant_name} - {violation.message}"
        )
    
    def _halt_boost(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Halt boost - TIER-0: System-binding enforcement"""
        # TIER-0 FIX: Throw exception to force halt
        raise InvariantViolationError(
            f"BOOST HALTED: {violation.invariant_name} - {violation.message}"
        )
    
    def _pause_training(self, violation: InvariantViolation, context: Dict[str, Any]) -> Dict[str, Any]:
        """Pause training - TIER-0: System-binding enforcement"""
        # TIER-0 FIX: Actually throw exception to force pause
        raise InvariantViolationError(
            f"TRAINING PAUSED: {violation.invariant_name} - {violation.message}"
        )


# ============================================================================
# ENFORCEMENT ENGINE
# ============================================================================

class InvariantEnforcer:
    """Main enforcement engine"""
    
    def __init__(self, audit_logger: Optional[AuditLogger] = None,
                 remediation_handler: Optional[RemediationHandler] = None):
        self.registry = InvariantRegistry()
        self.enforcement_log: List[Dict[str, Any]] = []
        self.audit_logger = audit_logger or AuditLogger()
        self.remediation_handler = remediation_handler or RemediationHandler()
        self.determinism_verifier = DeterminismVerifier()
    
    def enforce(self, data: Dict[str, Any], metric_name: str = "unknown") -> Dict[str, Any]:
        """
        Enforce all invariants on data.
        
        Returns enforcement result with:
        - valid: bool (whether data passed all critical invariants)
        - violations: List[InvariantViolation]
        - remediation_actions: List[RemediationAction]
        - quarantined: bool
        - halted: bool
        """
        violations = self.registry.check_all(data, metric_name)
        
        if not violations:
            return {
                'valid': True,
                'violations': [],
                'remediation_actions': [],
                'quarantined': False,
                'halted': False
            }
        
        # Determine worst severity and remediation
        critical_violations = [v for v in violations if v.severity == Severity.CRITICAL]
        high_violations = [v for v in violations if v.severity == Severity.HIGH]
        
        halted = any(v.remediation_taken == RemediationAction.HALT_PIPELINE for v in violations)
        quarantined = any(v.remediation_taken == RemediationAction.QUARANTINE_METRIC for v in violations)
        
        remediation_actions = list(set(v.remediation_taken for v in violations))
        
        result = {
            'valid': len(critical_violations) == 0,
            'violations': violations,
            'remediation_actions': remediation_actions,
            'quarantined': quarantined,
            'halted': halted,
            'critical_count': len(critical_violations),
            'high_count': len(high_violations)
        }
        
        # CHANGE 3: Execute remediation actions - CRITICAL actions throw exceptions
        executed_actions = []
        for violation in violations:
            # CHANGE 3: For CRITICAL violations with halt actions, throw immediately
            if violation.severity == Severity.CRITICAL:
                if violation.remediation_taken in [
                    RemediationAction.HALT_PIPELINE,
                    RemediationAction.PAUSE_TRAINING,
                    RemediationAction.FREEZE_AGENT,
                    RemediationAction.HALT_BOOST
                ]:
                    # CHANGE 3: Force halt via exception - no advisory mode
                    self.remediation_handler.execute(
                        violation.remediation_taken, violation, data
                    )
                    # If we get here, handler didn't throw (shouldn't happen)
                    raise InvariantViolationError(
                        f"CRITICAL violation {violation.invariant_name} should have halted system"
                    )
            
            # For non-CRITICAL or non-halt actions, execute normally
            try:
                action_result = self.remediation_handler.execute(
                    violation.remediation_taken, violation, data
                )
                executed_actions.append({
                    'violation_id': violation.violation_id,
                    'action': violation.remediation_taken.value,
                    'result': action_result
                })
            except InvariantViolationError as e:
                # CHANGE 3: If handler throws (shouldn't for non-CRITICAL), log and continue
                executed_actions.append({
                    'violation_id': violation.violation_id,
                    'action': violation.remediation_taken.value,
                    'result': {'executed': True, 'exception': str(e)}
                })
            
            # Log to audit trail
            self.audit_logger.log_violation(
                violation,
                {k: v for k, v in data.items() if k in ['timestamp', 'platform', 'content_id', 'agent_id']}
            )
        
        # Log enforcement action
        log_entry = {
            'timestamp': time.time(),
            'metric_name': metric_name,
            'result': result,
            'data_snapshot': {k: v for k, v in data.items() if k in ['timestamp', 'platform', 'content_id']},
            'executed_actions': executed_actions
        }
        self.enforcement_log.append(log_entry)
        
        # Flush audit log if needed
        if len(self.audit_logger.audit_log) >= 50:
            self.audit_logger._flush_audit_log()
        
        return result
    
    def get_enforcement_stats(self, evaluation_time: Optional[float] = None) -> Dict[str, Any]:
        """Get enforcement statistics"""
        # FIX REGRESSION: Allow wall-clock for stats (system monitoring, not metric truth)
        if evaluation_time is None:
            evaluation_time = time.time()  # OK for stats (system monitoring)
        
        total_checks = len(self.enforcement_log)
        halted_count = sum(1 for e in self.enforcement_log if e['result']['halted'])
        quarantined_count = sum(1 for e in self.enforcement_log if e['result']['quarantined'])
        
        return {
            'total_checks': total_checks,
            'halted_count': halted_count,
            'quarantined_count': quarantined_count,
            'halt_rate': halted_count / max(total_checks, 1),
            'quarantine_rate': quarantined_count / max(total_checks, 1),
            'violation_summary': self.registry.get_violation_summary(evaluation_time=evaluation_time),
            'drift_analysis': self.registry.detect_drift(evaluation_time=evaluation_time),
            'quarantined_metrics': list(self.remediation_handler.quarantined_metrics),
            'frozen_agents': list(self.remediation_handler.frozen_agents)
        }
    
    def export_audit_records(self, start_time: Optional[float] = None,
                             end_time: Optional[float] = None,
                             output_path: Optional[Path] = None) -> Path:
        """Export audit records for legal defensibility"""
        return self.audit_logger.export_violations(start_time, end_time, output_path)


# ============================================================================
# INTEGRATION HOOKS (safety_watchdog, gradient_governor)
# ============================================================================

class SafetyWatchdogIntegration:
    """Integration hooks for safety_watchdog"""
    
    @staticmethod
    def get_critical_violations(lookback_seconds: float = 3600, 
                                evaluation_time: Optional[float] = None) -> List[InvariantViolation]:
        """Get critical violations for safety_watchdog"""
        # TIER-0 FIX: Use evaluation_time for determinism
        if evaluation_time is None:
            evaluation_time = time.time()
        
        registry = get_invariant_registry()
        cutoff_time = evaluation_time - lookback_seconds
        return [
            v for v in registry.violations
            if v.severity == Severity.CRITICAL and v.timestamp > cutoff_time
        ]
    
    @staticmethod
    def get_violation_rate() -> float:
        """Get violation rate for safety monitoring"""
        registry = get_invariant_registry()
        stats = registry.get_violation_summary()
        recent = stats.get('recent_violations_1h', 0)
        return recent / 3600.0  # Violations per second


class GradientGovernorIntegration:
    """Integration hooks for gradient_governor"""
    
    @staticmethod
    def get_reward_metric_coupling_violations(evaluation_time: Optional[float] = None) -> List[InvariantViolation]:
        """Get reward-metric coupling violations"""
        # TIER-0 FIX: Use evaluation_time for determinism
        if evaluation_time is None:
            evaluation_time = time.time()
        
        registry = get_invariant_registry()
        return [
            v for v in registry.violations
            if v.invariant_name == 'reward_metric_coupling'
            and v.timestamp > evaluation_time - 3600
        ]
    
    @staticmethod
    def should_pause_training(evaluation_time: Optional[float] = None) -> bool:
        """Check if training should be paused based on invariants"""
        # TIER-0 FIX: Use evaluation_time for determinism
        if evaluation_time is None:
            evaluation_time = time.time()
        
        registry = get_invariant_registry()
        recent_violations = [
            v for v in registry.violations
            if v.remediation_taken == RemediationAction.PAUSE_TRAINING
            and v.timestamp > evaluation_time - 3600
        ]
        return len(recent_violations) > 0


# ============================================================================
# INTEGRATION INTERFACE (CHANGE 4: Explicit Context Binding)
# ============================================================================

# POLISH 1: Global singleton with write-once protection
# This prevents cross-request contamination and replay/test interference
_global_enforcer: Optional[InvariantEnforcer] = None
_global_enforcer_lock = threading.Lock()
_global_enforcer_initialized = False


def _get_global_enforcer() -> InvariantEnforcer:
    """
    POLISH 1: Get or create global enforcer with write-once protection.
    This prevents cross-request contamination and ensures isolation.
    """
    global _global_enforcer, _global_enforcer_initialized
    
    if _global_enforcer is not None:
        return _global_enforcer
    
    with _global_enforcer_lock:
        # Double-check after acquiring lock
        if _global_enforcer is not None:
            return _global_enforcer
        
        # POLISH 1: Create write-once global enforcer
        _global_enforcer = InvariantEnforcer()
        _global_enforcer_initialized = True
        return _global_enforcer


def reset_global_enforcer(production_only: bool = True) -> None:
    """
    POLISH 1: Reset global enforcer (for testing/replay isolation).
    
    Args:
        production_only: If True, only allows reset in non-production environments
    """
    global _global_enforcer, _global_enforcer_initialized
    
    if production_only:
        # Check if we're in production (e.g., via environment variable)
        import os
        if os.getenv('PRODUCTION', '').lower() in ('true', '1', 'yes'):
            raise RuntimeError("Cannot reset global enforcer in production environment")
    
    with _global_enforcer_lock:
        _global_enforcer = None
        _global_enforcer_initialized = False


def enforce_invariants(data: Dict[str, Any], metric_name: str = "unknown", 
                       enforcer: Optional[InvariantEnforcer] = None) -> Dict[str, Any]:
    """
    Main entry point for invariant enforcement.
    
    CHANGE 4: Accepts explicit enforcer for multi-run isolation.
    Falls back to global singleton for backward compatibility.
    
    TIER-0: This function will raise InvariantViolationError for CRITICAL violations.
    Callers must handle exceptions, not just check return values.
    
    Usage (explicit enforcer - recommended for replays/simulations):
        enforcer = InvariantEnforcer()
        try:
            result = enforce_invariants(metric_data, "early_velocity", enforcer=enforcer)
        except InvariantViolationError as e:
            handle_critical_violation(e)
    
    Usage (global singleton - prod only):
        try:
            result = enforce_invariants(metric_data, "early_velocity")
        except InvariantViolationError as e:
            handle_critical_violation(e)
    """
    # POLISH 1: Use explicit enforcer if provided, otherwise get global (with write-once protection)
    active_enforcer = enforcer or _get_global_enforcer()
    
    # CHANGE 1: evaluation_time is now MANDATORY - DeterminismInvariant will catch missing
    # No fallback injection - strict determinism required
    
    return active_enforcer.enforce(data, metric_name)


def get_invariant_registry(enforcer: Optional[InvariantEnforcer] = None) -> InvariantRegistry:
    """Get invariant registry (CHANGE 4: accepts explicit enforcer)"""
    active_enforcer = enforcer or _get_global_enforcer()
    return active_enforcer.registry


def get_enforcement_stats(enforcer: Optional[InvariantEnforcer] = None) -> Dict[str, Any]:
    """Get enforcement statistics (CHANGE 4: accepts explicit enforcer)"""
    active_enforcer = enforcer or _get_global_enforcer()
    return active_enforcer.get_enforcement_stats()


def export_audit_records(start_time: Optional[float] = None,
                        end_time: Optional[float] = None,
                        output_path: Optional[Path] = None,
                        enforcer: Optional[InvariantEnforcer] = None) -> Path:
    """Export audit records for legal defensibility (POLISH 1: accepts explicit enforcer)"""
    active_enforcer = enforcer or _get_global_enforcer()
    return active_enforcer.export_audit_records(start_time, end_time, output_path)


def get_safety_watchdog_integration() -> SafetyWatchdogIntegration:
    """Get safety_watchdog integration interface"""
    return SafetyWatchdogIntegration


def get_gradient_governor_integration() -> GradientGovernorIntegration:
    """Get gradient_governor integration interface"""
    return GradientGovernorIntegration


# ============================================================================
# EXCEPTION TYPES
# ============================================================================

class InvariantViolationError(Exception):
    """Raised when critical invariant is violated"""
    pass


class InvariantDriftError(Exception):
    """Raised when invariant drift is detected"""
    pass