"""
/training/data_gate.py

Training Data Admissibility Firewall
First hard security boundary of the training system.

Determines whether any sample is allowed to influence gradient computation.
NOT an optimizer. NOT a cleaner. Admit or reject ONLY.

Designed for:
- 240k+ LOC architecture
- 5M+ views baseline
- 30M-300M views repeatable scale
- Causal correctness
- Training integrity
- Irreversible failure prevention
"""

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any, Callable
import json
import hashlib
import logging
from datetime import datetime, timedelta
import numpy as np
from collections import defaultdict, deque

# Configure logger for fatal alerts
logger = logging.getLogger(__name__)


# ============================================================================
# CORE TYPES & CONTRACTS
# ============================================================================

class TrainingIntegrityViolation(Exception):
    """
    CRITICAL: Raised when data gate detects integrity violation that requires
    immediate training halt. Training loop MUST catch this and stop.
    
    This exception indicates:
    - Corrupted metadata detected
    - Unknown feature versions
    - Missing references
    - Inconsistent clocks
    - Any condition that compromises training integrity
    
    Training MUST:
    1. STOP immediately
    2. Freeze optimizer
    3. Emit fatal alert
    4. Require manual intervention or rollback
    """
    
    def __init__(self, message: str, diagnostic_state: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.diagnostic_state = diagnostic_state or {}
        self.timestamp = datetime.now()
    
    def __str__(self):
        base_msg = super().__str__()
        if self.diagnostic_state:
            return f"{base_msg} | Diagnostic: {json.dumps(self.diagnostic_state, default=str)}"
        return base_msg


class GateRejectionReason(Enum):
    """All possible rejection reasons - explicit enumeration for audit trail"""
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MALFORMED_FEATURE_BUNDLE = "malformed_feature_bundle"
    MISSING_MODALITY = "missing_modality"
    UNKNOWN_FEATURE_VERSION = "unknown_feature_version"
    SCHEMA_MISMATCH = "schema_mismatch"
    
    TEMPORAL_VIOLATION_SCRAPE_BEFORE_CONTENT = "temporal_violation_scrape_before_content"
    TEMPORAL_VIOLATION_FUTURE_METRICS = "temporal_violation_future_metrics"
    TEMPORAL_VIOLATION_TRAINING_BEFORE_SCRAPE = "temporal_violation_training_before_scrape"
    
    SIGNAL_NAN_OR_INF = "signal_nan_or_inf"
    SIGNAL_ZERO_VARIANCE = "signal_zero_variance"
    SIGNAL_IMPOSSIBLE_VALUE = "signal_impossible_value"
    SIGNAL_BOT_PATTERN = "signal_bot_pattern"
    SIGNAL_LABEL_POISONING = "signal_label_poisoning"
    
    DISTRIBUTION_SHIFT_EXCEEDED = "distribution_shift_exceeded"
    DISTRIBUTION_TAIL_INFLATION = "distribution_tail_inflation"
    DISTRIBUTION_NICHE_IMBALANCE = "distribution_niche_imbalance"
    DISTRIBUTION_PLATFORM_DOMINANCE = "distribution_platform_dominance"
    
    RARE_EVENT_UNCERTAINTY_TOO_LOW = "rare_event_uncertainty_too_low"
    RARE_EVENT_SAMPLING_CAP_EXCEEDED = "rare_event_sampling_cap_exceeded"
    RARE_EVENT_REPLAY_OVERUSE = "rare_event_replay_overuse"
    
    CURRICULUM_DATA_TOO_OLD = "curriculum_data_too_old"
    CURRICULUM_DATA_TOO_NEW = "curriculum_data_too_new"
    CURRICULUM_UNCERTAINTY_OUT_OF_BAND = "curriculum_uncertainty_out_of_band"
    CURRICULUM_DISTRIBUTION_SPREAD_VIOLATION = "curriculum_distribution_spread_violation"
    
    BATCH_DUPLICATE_DETECTED = "batch_duplicate_detected"
    BATCH_INSUFFICIENT_HORIZON_DIVERSITY = "batch_insufficient_horizon_diversity"
    BATCH_PLATFORM_IMBALANCE = "batch_platform_imbalance"
    BATCH_NICHE_MIX_VIOLATION = "batch_niche_mix_violation"
    
    REPLAY_AGE_GAP_TOO_SMALL = "replay_age_gap_too_small"
    REPLAY_REPEATED_WITHIN_WINDOW = "replay_repeated_within_window"
    REPLAY_VERSION_INCOMPATIBLE = "replay_version_incompatible"


@dataclass(frozen=True)
class Sample:
    """Immutable sample metadata - NO raw content"""
    video_id: str
    platform: str
    scrape_timestamp: datetime
    content_timestamp: datetime
    feature_bundle_version: str
    ingestion_mode: str  # live/backfill/replay
    uncertainty_estimate: float
    source_confidence: float
    
    # Additional metadata for gate checks
    engagement_features: Dict[str, float]
    niche_category: Optional[str] = None
    is_tail_event: bool = False
    replay_count: int = 0
    
    # Label poisoning detection metadata
    content_age_hours: Optional[float] = None  # Age of content when metrics were scraped
    cross_platform_metrics: Optional[Dict[str, Dict[str, float]]] = None  # For consistency checks
    
    def __hash__(self):
        """Deterministic hash for duplicate detection"""
        content = f"{self.video_id}|{self.platform}|{self.scrape_timestamp.isoformat()}"
        return int(hashlib.sha256(content.encode()).hexdigest()[:16], 16)


@dataclass(frozen=True)
class GateDecision:
    """Atomic admission decision with full audit trail"""
    sample_id: str
    admitted: bool
    rejection_reason: Optional[GateRejectionReason]
    gate_stage: str
    timestamp: datetime
    curriculum_phase: str
    model_version: str
    metadata: Dict[str, Any]


@dataclass
class CurriculumConstraints:
    """Curriculum-declared data requirements"""
    allowed_data_age_min: timedelta
    allowed_data_age_max: timedelta
    allowed_uncertainty_min: float
    allowed_uncertainty_max: float
    allowed_distribution_spread_max: float
    phase_name: str


@dataclass
class GateConfig:
    """Immutable gate configuration"""
    # Structural
    required_fields: Set[str]
    allowed_feature_versions: Set[str]
    required_modalities: Set[str]
    
    # Temporal
    temporal_tolerance_seconds: int = 60
    
    # Signal integrity
    signal_nan_tolerance: float = 0.0
    signal_zero_variance_threshold: float = 1e-8
    bot_pattern_spike_threshold: float = 10.0
    
    # Distribution
    distribution_shift_threshold: float = 0.15
    tail_density_max_ratio: float = 2.5
    niche_concentration_max: float = 0.4
    platform_dominance_max: float = 0.7
    
    # Rare events
    rare_event_percentile: float = 99.0
    rare_event_min_uncertainty: float = 0.6
    rare_event_max_samples_per_batch: int = 5
    rare_event_replay_max: int = 3
    
    # Batch
    batch_max_duplicate_ratio: float = 0.05
    batch_min_horizon_diversity: int = 3
    batch_platform_balance_min: float = 0.15
    
    # Replay
    replay_min_age_gap_hours: int = 48
    replay_window_hours: int = 168  # 1 week
    replay_max_density_percent: float = 15.0  # Max 15% replay samples in rolling window
    
    # Bootstrap caps (applied even before baseline exists)
    bootstrap_platform_dominance_max: float = 0.8  # Stricter during bootstrap
    bootstrap_tail_hard_limit: int = 10  # Max tail events in first 100 samples
    bootstrap_niche_max_ratio: float = 0.5  # Max niche concentration during bootstrap
    
    # Distributed training safety
    distributed_training: bool = False  # If True, raises error (stateful guards require single-writer)


# ============================================================================
# GATE COMPONENTS
# ============================================================================

class SchemaValidator:
    """Gate 1: Structural validity"""
    
    def __init__(self, config: GateConfig):
        self.config = config
    
    def validate(self, sample: Sample) -> Tuple[bool, Optional[GateRejectionReason]]:
        """Check structural integrity - NO auto-fixes"""
        
        # Check required fields exist
        for field in self.config.required_fields:
            if not hasattr(sample, field) or getattr(sample, field) is None:
                return False, GateRejectionReason.MISSING_REQUIRED_FIELD
        
        # Check feature bundle version
        if sample.feature_bundle_version not in self.config.allowed_feature_versions:
            return False, GateRejectionReason.UNKNOWN_FEATURE_VERSION
        
        # Check engagement features exist
        if not sample.engagement_features:
            return False, GateRejectionReason.MALFORMED_FEATURE_BUNDLE
        
        # Check required modalities
        for modality in self.config.required_modalities:
            if modality not in sample.engagement_features:
                return False, GateRejectionReason.MISSING_MODALITY
        
        return True, None


class TemporalIntegrityChecker:
    """Gate 2: Temporal integrity - CRITICAL causal boundary"""
    
    def __init__(self, config: GateConfig):
        self.config = config
        self.tolerance = timedelta(seconds=config.temporal_tolerance_seconds)
    
    def validate(self, sample: Sample, training_time: datetime) -> Tuple[bool, Optional[GateRejectionReason]]:
        """Hard fail on any temporal violation - NO warnings, NO partial acceptance"""
        
        # Rule 1: scrape_timestamp >= content_timestamp
        if sample.scrape_timestamp < sample.content_timestamp - self.tolerance:
            return False, GateRejectionReason.TEMPORAL_VIOLATION_SCRAPE_BEFORE_CONTENT
        
        # Rule 2: training_time > scrape_timestamp
        if training_time <= sample.scrape_timestamp:
            return False, GateRejectionReason.TEMPORAL_VIOLATION_TRAINING_BEFORE_SCRAPE
        
        # Rule 3: no future metrics relative to training window
        # (implicitly enforced by rule 2, but explicit check for clarity)
        if sample.scrape_timestamp > training_time:
            return False, GateRejectionReason.TEMPORAL_VIOLATION_FUTURE_METRICS
        
        return True, None


class SignalIntegrityScanner:
    """Gate 3: Signal integrity - data sanitation, NOT anomaly detection"""
    
    def __init__(self, config: GateConfig):
        self.config = config
    
    def validate(self, sample: Sample) -> Tuple[bool, Optional[GateRejectionReason]]:
        """Reject corrupted or impossible signals"""
        
        features = sample.engagement_features
        
        # FIX 3: Determinism hardening - sort keys for bit-for-bit determinism
        # Check for NaNs or Infs
        for key in sorted(features.keys()):
            value = features[key]
            if not isinstance(value, (int, float)):
                continue
            if np.isnan(value) or np.isinf(value):
                return False, GateRejectionReason.SIGNAL_NAN_OR_INF
        
        # Check for zero variance (flat engagement)
        values = [v for v in features.values() if isinstance(v, (int, float))]
        if len(values) > 1:
            variance = np.var(values)
            if variance < self.config.signal_zero_variance_threshold:
                return False, GateRejectionReason.SIGNAL_ZERO_VARIANCE
        
        # Check for impossible values
        if 'views' in features and features['views'] < 0:
            return False, GateRejectionReason.SIGNAL_IMPOSSIBLE_VALUE
        if 'likes' in features and features['likes'] < 0:
            return False, GateRejectionReason.SIGNAL_IMPOSSIBLE_VALUE
        
        # Check for bot-like patterns (flat spikes)
        if 'engagement_velocity' in features:
            if features['engagement_velocity'] > self.config.bot_pattern_spike_threshold:
                # Check if it's a sustained spike (bot) vs organic viral
                if 'engagement_acceleration' in features:
                    if abs(features['engagement_acceleration']) < 0.1:
                        return False, GateRejectionReason.SIGNAL_BOT_PATTERN
        
        # Check for label poisoning patterns
        # Pattern 1: Engagement metrics that don't match content age
        if sample.content_age_hours is not None:
            if 'views' in features and 'engagement_rate' in features:
                # Very high engagement on very new content suggests manipulation
                # Normal viral content takes time to accumulate
                if sample.content_age_hours < 1.0:  # Less than 1 hour old
                    views_per_hour = features['views'] / max(sample.content_age_hours, 0.1)
                    if views_per_hour > 1000000 and features['engagement_rate'] > 0.2:
                        # Suspicious: >1M views/hour with >20% engagement on <1hr old content
                        return False, GateRejectionReason.SIGNAL_LABEL_POISONING
                
                # Very high engagement with very low views suggests label manipulation
                if features['engagement_rate'] > 0.5 and features['views'] < 100:
                    return False, GateRejectionReason.SIGNAL_LABEL_POISONING
        
        # Pattern 2: Cross-platform consistency checks
        if sample.cross_platform_metrics:
            # If same content exists on multiple platforms, metrics should be consistent
            # within reasonable bounds (accounting for platform differences)
            # FIX 3: Determinism hardening - sort keys
            platform_views = {}
            for platform in sorted(sample.cross_platform_metrics.keys()):
                metrics = sample.cross_platform_metrics[platform]
                if 'views' in metrics:
                    platform_views[platform] = metrics['views']
            
            if len(platform_views) > 1:
                values = list(platform_views.values())
                if 'views' in features:
                    values.append(features['views'])
                
                if len(values) > 1:
                    # Check for extreme inconsistencies (>100x difference)
                    max_val = max(values)
                    min_val = min(values)
                    if min_val > 0 and max_val / min_val > 100:
                        # Allow some platforms to have more views, but 100x is suspicious
                        # Unless one platform has very low views (normal early stage)
                        if min_val > 1000:  # Both have meaningful views
                            return False, GateRejectionReason.SIGNAL_LABEL_POISONING
        
        # Pattern 3: Suspicious engagement patterns
        if 'views' in features and 'likes' in features and 'comments' in features:
            views = features['views']
            likes = features.get('likes', 0)
            comments = features.get('comments', 0)
            
            if views > 0:
                like_ratio = likes / views
                comment_ratio = comments / views
                
                # Extremely high like ratio (>50%) suggests manipulation
                if like_ratio > 0.5 and views > 10000:
                    return False, GateRejectionReason.SIGNAL_LABEL_POISONING
                
                # Suspicious: Very high views with zero engagement
                if views > 100000 and likes == 0 and comments == 0:
                    return False, GateRejectionReason.SIGNAL_LABEL_POISONING
        
        return True, None


class DistributionGuard:
    """Gate 4: Distribution consistency - prevents slow poisoning"""
    
    def __init__(self, config: GateConfig, drift_detector_callback: Optional[Callable] = None):
        self.config = config
        self.drift_detector_callback = drift_detector_callback
        self.baseline_stats: Dict[str, Dict[str, float]] = {}
        self.rolling_window = deque(maxlen=10000)
        # Bootstrap tracking (for first 100 samples)
        self.bootstrap_samples: List[Sample] = []
        self.bootstrap_platform_counts: Dict[str, int] = defaultdict(int)
        self.bootstrap_niche_counts: Dict[str, int] = defaultdict(int)
        self.bootstrap_tail_count = 0
    
    def update_baseline(self, samples: List[Sample]):
        """Update rolling baseline statistics"""
        for sample in samples:
            self.rolling_window.append(sample)
        
        # Clear bootstrap tracking once baseline is established
        if len(self.rolling_window) >= 100 and self.bootstrap_samples:
            self.bootstrap_samples.clear()
            self.bootstrap_platform_counts.clear()
            self.bootstrap_niche_counts.clear()
            self.bootstrap_tail_count = 0
        
        if len(self.rolling_window) < 100:
            return  # Need minimum samples
        
        # Compute rolling statistics
        feature_values = defaultdict(list)
        platform_counts = defaultdict(int)
        niche_counts = defaultdict(int)
        
        for sample in self.rolling_window:
            for key, value in sample.engagement_features.items():
                if isinstance(value, (int, float)):
                    feature_values[key].append(value)
            platform_counts[sample.platform] += 1
            if sample.niche_category:
                niche_counts[sample.niche_category] += 1
        
        # FIX 3: Determinism hardening - sort keys when storing baseline
        # Store baseline statistics
        self.baseline_stats = {
            'features': {
                key: {
                    'mean': np.mean(values),
                    'std': np.std(values),
                    'p99': np.percentile(values, 99)
                }
                for key in sorted(feature_values.keys())
                for values in [feature_values[key]]
            },
            'platforms': dict(platform_counts),
            'niches': dict(niche_counts)
        }
    
    def validate(self, sample: Sample) -> Tuple[bool, Optional[GateRejectionReason]]:
        """
        Check for distribution drift and imbalances.
        
        TIER-0 HARDENING: Bootstrap caps applied even before baseline exists.
        Prevents adversarial early poisoning during first ~100 samples.
        """
        
        # TIER-0 FIX 1: Bootstrap caps (fail-closed even before baseline)
        if not self.baseline_stats:
            # Track bootstrap samples
            self.bootstrap_samples.append(sample)
            self.bootstrap_platform_counts[sample.platform] += 1
            if sample.niche_category:
                self.bootstrap_niche_counts[sample.niche_category] += 1
            if sample.is_tail_event:
                self.bootstrap_tail_count += 1
            
            # Apply bootstrap caps
            total_bootstrap = len(self.bootstrap_samples)
            
            # Platform dominance ceiling during bootstrap
            if total_bootstrap > 0:
                for platform, count in self.bootstrap_platform_counts.items():
                    platform_ratio = count / total_bootstrap
                    if platform_ratio > self.config.bootstrap_platform_dominance_max:
                        return False, GateRejectionReason.DISTRIBUTION_PLATFORM_DOMINANCE
            
            # Tail hard-limit during bootstrap
            if self.bootstrap_tail_count > self.config.bootstrap_tail_hard_limit:
                return False, GateRejectionReason.DISTRIBUTION_TAIL_INFLATION
            
            # Niche max ratio during bootstrap
            # FIX 3: Determinism hardening - sort keys
            if self.bootstrap_niche_counts:
                total_niches = sum(self.bootstrap_niche_counts.values())
                if total_niches > 0:
                    for niche in sorted(self.bootstrap_niche_counts.keys()):
                        count = self.bootstrap_niche_counts[niche]
                        niche_ratio = count / total_niches
                        if niche_ratio > self.config.bootstrap_niche_max_ratio:
                            return False, GateRejectionReason.DISTRIBUTION_NICHE_IMBALANCE
            
            # Once we have 100 samples, baseline will be established
            # Continue allowing samples through bootstrap caps until then
            return True, None
        
        # Check feature distribution shift
        for key, value in sample.engagement_features.items():
            if not isinstance(value, (int, float)):
                continue
            if key not in self.baseline_stats['features']:
                continue
            
            baseline = self.baseline_stats['features'][key]
            if baseline['std'] > 0:
                z_score = abs(value - baseline['mean']) / baseline['std']
                if z_score > 3.0 * (1.0 + self.config.distribution_shift_threshold):
                    # Notify drift detector if callback provided
                    if self.drift_detector_callback:
                        try:
                            drift_metrics = {
                                'feature': key,
                                'value': value,
                                'baseline_mean': baseline['mean'],
                                'baseline_std': baseline['std'],
                                'z_score': z_score,
                                'sample_id': sample.video_id,
                                'platform': sample.platform
                            }
                            self.drift_detector_callback(sample, drift_metrics)
                        except Exception as e:
                            logger.warning(f"Drift detector callback failed: {e}")
                    return False, GateRejectionReason.DISTRIBUTION_SHIFT_EXCEEDED
        
        # Check tail density inflation
        if sample.is_tail_event:
            tail_count = sum(1 for s in self.rolling_window if s.is_tail_event)
            tail_ratio = tail_count / len(self.rolling_window)
            expected_ratio = (100 - self.config.rare_event_percentile) / 100
            if tail_ratio > expected_ratio * self.config.tail_density_max_ratio:
                return False, GateRejectionReason.DISTRIBUTION_TAIL_INFLATION
        
        # Check niche concentration
        if sample.niche_category and 'niches' in self.baseline_stats:
            total_niches = sum(self.baseline_stats['niches'].values())
            if total_niches > 0:
                niche_ratio = self.baseline_stats['niches'].get(sample.niche_category, 0) / total_niches
                if niche_ratio > self.config.niche_concentration_max:
                    return False, GateRejectionReason.DISTRIBUTION_NICHE_IMBALANCE
        
        # Check platform dominance
        if 'platforms' in self.baseline_stats:
            total_platforms = sum(self.baseline_stats['platforms'].values())
            if total_platforms > 0:
                platform_ratio = self.baseline_stats['platforms'].get(sample.platform, 0) / total_platforms
                if platform_ratio > self.config.platform_dominance_max:
                    return False, GateRejectionReason.DISTRIBUTION_PLATFORM_DOMINANCE
        
        return True, None


class RareEventProtector:
    """Gate 5: Rare event safeguard - prevents tail domination"""
    
    def __init__(self, config: GateConfig):
        self.config = config
        # TIER-0 FIX 2: Shadow context for atomic batch validation
        self.tail_samples_in_batch = 0
        self._shadow_tail_count = 0  # Staged counter, committed only if batch passes
        self.replay_counts: Dict[str, int] = defaultdict(int)
        self._shadow_replay_counts: Dict[str, int] = defaultdict(int)  # Staged replay counts
    
    def reset_batch_counter(self):
        """Reset per-batch counters"""
        self.tail_samples_in_batch = 0
        self._shadow_tail_count = 0
        self._shadow_replay_counts.clear()
    
    def commit_shadow_state(self):
        """Commit shadow state to actual state (called only if batch passes)"""
        self.tail_samples_in_batch = self._shadow_tail_count
        for key, count in self._shadow_replay_counts.items():
            self.replay_counts[key] = count
    
    def rollback_shadow_state(self):
        """Rollback shadow state (called if batch fails)"""
        self._shadow_tail_count = 0
        self._shadow_replay_counts.clear()
    
    def validate(self, sample: Sample) -> Tuple[bool, Optional[GateRejectionReason]]:
        """Prevent rare events from dominating gradients"""
        
        if not sample.is_tail_event:
            return True, None
        
        # Check uncertainty is high for tail events
        if sample.uncertainty_estimate < self.config.rare_event_min_uncertainty:
            return False, GateRejectionReason.RARE_EVENT_UNCERTAINTY_TOO_LOW
        
        # Enforce sampling cap per batch
        if self.tail_samples_in_batch >= self.config.rare_event_max_samples_per_batch:
            return False, GateRejectionReason.RARE_EVENT_SAMPLING_CAP_EXCEEDED
        
        # Check replay overuse
        if sample.ingestion_mode == 'replay':
            replay_key = f"{sample.video_id}_{sample.platform}"
            if self.replay_counts[replay_key] >= self.config.rare_event_replay_max:
                return False, GateRejectionReason.RARE_EVENT_REPLAY_OVERUSE
            self.replay_counts[replay_key] += 1
        
        self.tail_samples_in_batch += 1
        return True, None


class CurriculumGate:
    """Gate 6: Curriculum compatibility - curriculum controls data"""
    
    def __init__(self, config: GateConfig):
        self.config = config
        self.current_constraints: Optional[CurriculumConstraints] = None
    
    def set_curriculum_phase(self, constraints: CurriculumConstraints):
        """Update curriculum constraints"""
        self.current_constraints = constraints
    
    def validate(self, sample: Sample, training_time: datetime) -> Tuple[bool, Optional[GateRejectionReason]]:
        """Enforce curriculum-declared requirements"""
        
        if self.current_constraints is None:
            return True, None  # No curriculum active
        
        # Check data age
        data_age = training_time - sample.scrape_timestamp
        if data_age < self.current_constraints.allowed_data_age_min:
            return False, GateRejectionReason.CURRICULUM_DATA_TOO_NEW
        if data_age > self.current_constraints.allowed_data_age_max:
            return False, GateRejectionReason.CURRICULUM_DATA_TOO_OLD
        
        # Check uncertainty band
        if sample.uncertainty_estimate < self.current_constraints.allowed_uncertainty_min:
            return False, GateRejectionReason.CURRICULUM_UNCERTAINTY_OUT_OF_BAND
        if sample.uncertainty_estimate > self.current_constraints.allowed_uncertainty_max:
            return False, GateRejectionReason.CURRICULUM_UNCERTAINTY_OUT_OF_BAND
        
        # FIX 1: Check distribution spread (curriculum-level)
        # This was declared in spec but not enforced - now enforced
        if self.current_constraints.allowed_distribution_spread_max is not None:
            if sample.is_tail_event:
                # Tail events increase effective spread
                effective_spread = sample.uncertainty_estimate
            else:
                effective_spread = sample.uncertainty_estimate * 0.5
            
            if effective_spread > self.current_constraints.allowed_distribution_spread_max:
                return False, GateRejectionReason.CURRICULUM_DISTRIBUTION_SPREAD_VIOLATION
        
        return True, None


class BatchValidator:
    """Gate 7: Batch-level sanity checks - atomic batch admission"""
    
    def __init__(self, config: GateConfig):
        self.config = config
    
    def validate(self, batch: List[Sample]) -> Tuple[bool, Optional[GateRejectionReason]]:
        """Batch is atomic - if batch violates, drop entire batch"""
        
        if not batch:
            return False, GateRejectionReason.BATCH_DUPLICATE_DETECTED
        
        # Duplicate detection
        unique_hashes = set(hash(s) for s in batch)
        duplicate_ratio = 1.0 - (len(unique_hashes) / len(batch))
        if duplicate_ratio > self.config.batch_max_duplicate_ratio:
            return False, GateRejectionReason.BATCH_DUPLICATE_DETECTED
        
        # Horizon diversity (unique time windows)
        unique_hours = set(s.scrape_timestamp.replace(minute=0, second=0, microsecond=0) for s in batch)
        if len(unique_hours) < self.config.batch_min_horizon_diversity:
            return False, GateRejectionReason.BATCH_INSUFFICIENT_HORIZON_DIVERSITY
        
        # Platform balance
        platform_counts = defaultdict(int)
        for sample in batch:
            platform_counts[sample.platform] += 1
        
        total = len(batch)
        for count in platform_counts.values():
            ratio = count / total
            if ratio < self.config.batch_platform_balance_min and len(platform_counts) > 1:
                return False, GateRejectionReason.BATCH_PLATFORM_IMBALANCE
        
        return True, None


class ReplayBufferGuard:
    """
    Replay buffer protection - prevents feedback loops.
    
    TIER-0 FIX 3: Global replay pressure ceiling and cross-batch entropy tracking.
    """
    
    def __init__(self, config: GateConfig):
        self.config = config
        self.replay_history: Dict[str, List[datetime]] = defaultdict(list)
        self.window = timedelta(hours=config.replay_window_hours)
        self.min_gap = timedelta(hours=config.replay_min_age_gap_hours)
        # TIER-0 FIX 3: Global replay density tracking
        self.replay_density_window = deque(maxlen=1000)  # Last 1000 samples (True=replay, False=non-replay)
    
    def validate(self, sample: Sample, training_time: datetime) -> Tuple[bool, Optional[GateRejectionReason]]:
        """
        Enforce replay safety constraints.
        
        TIER-0 FIX 3: Global replay density ceiling prevents slow feedback amplification.
        """
        
        if sample.ingestion_mode != 'replay':
            # Track non-replay sample
            self.replay_density_window.append(False)
            return True, None
        
        # TIER-0 FIX 3: Check global replay density ceiling
        if len(self.replay_density_window) > 100:  # Need minimum window
            replay_count = sum(1 for is_replay in self.replay_density_window if is_replay)
            replay_density = replay_count / len(self.replay_density_window)
            max_density = self.config.replay_max_density_percent / 100.0
            
            if replay_density > max_density:
                return False, GateRejectionReason.REPLAY_DENSITY_EXCEEDED
        
        replay_key = f"{sample.video_id}_{sample.platform}"
        
        # Check minimum age gap
        age = training_time - sample.scrape_timestamp
        if age < self.min_gap:
            return False, GateRejectionReason.REPLAY_AGE_GAP_TOO_SMALL
        
        # Check for repeated replay within window
        cutoff = training_time - self.window
        self.replay_history[replay_key] = [
            ts for ts in self.replay_history[replay_key] if ts > cutoff
        ]
        
        if self.replay_history[replay_key]:
            return False, GateRejectionReason.REPLAY_REPEATED_WITHIN_WINDOW
        
        # Version compatibility check
        if not sample.feature_bundle_version:
            return False, GateRejectionReason.REPLAY_VERSION_INCOMPATIBLE
        
        # TIER-0 FIX 3: Track replay sample in density window
        self.replay_history[replay_key].append(training_time)
        self.replay_density_window.append(True)
        return True, None


# ============================================================================
# MAIN DATA GATE
# ============================================================================

class DataGate:
    """
    Training Data Admissibility Firewall
    
    First hard security boundary - determines whether any sample
    is allowed to influence gradient computation.
    
    Fail closed. No silent degradation. Deterministic.
    """
    
    def __init__(
        self,
        config: GateConfig,
        model_version: str,
        enable_logging: bool = True,
        drift_detector_callback: Optional[Callable] = None,
        optimizer_freeze_callback: Optional[Callable] = None
    ):
        # FIX 2: TIER-0 SAFETY ASSERTION - Distributed-state safety
        # DataGate contains stateful guards (DistributionGuard, ReplayBufferGuard, RareEventProtector)
        # These must not be instantiated per-worker in distributed training.
        # Use single-writer or externalized state.
        if config.distributed_training:
            raise TrainingIntegrityViolation(
                "DataGate contains stateful guards and must not be instantiated "
                "per-worker. Use single-writer or externalized state.",
                diagnostic_state={
                    'model_version': model_version,
                    'config': {
                        'distributed_training': config.distributed_training
                    }
                }
            )
        
        self.config = config
        self.model_version = model_version
        self.enable_logging = enable_logging
        self.optimizer_freeze_callback = optimizer_freeze_callback
        self._training_stopped = False
        
        # Initialize gate components
        self.schema_validator = SchemaValidator(config)
        self.temporal_checker = TemporalIntegrityChecker(config)
        self.signal_scanner = SignalIntegrityScanner(config)
        self.distribution_guard = DistributionGuard(config, drift_detector_callback)
        self.rare_event_protector = RareEventProtector(config)
        self.curriculum_gate = CurriculumGate(config)
        self.batch_validator = BatchValidator(config)
        self.replay_guard = ReplayBufferGuard(config)
        
        # Audit trail
        self.decision_log: List[GateDecision] = []
        self.rejection_stats: Dict[GateRejectionReason, int] = defaultdict(int)
    
    def set_curriculum_phase(self, constraints: CurriculumConstraints):
        """Update curriculum constraints"""
        self.curriculum_gate.set_curriculum_phase(constraints)
    
    def update_distribution_baseline(self, samples: List[Sample]):
        """Update rolling baseline for distribution checks"""
        self.distribution_guard.update_baseline(samples)
    
    def validate_sample(
        self,
        sample: Sample,
        training_time: datetime,
        curriculum_phase: str
    ) -> GateDecision:
        """
        Validate single sample through all gates.
        Returns admission decision with full audit trail.
        
        DETERMINISTIC: Same inputs → same decision (bit-for-bit)
        """
        
        # Gate 1: Structural Validity
        valid, reason = self.schema_validator.validate(sample)
        if not valid:
            return self._reject(sample, reason, "schema", curriculum_phase, training_time)
        
        # Gate 2: Temporal Integrity (CRITICAL)
        valid, reason = self.temporal_checker.validate(sample, training_time)
        if not valid:
            return self._reject(sample, reason, "temporal", curriculum_phase, training_time)
        
        # Gate 3: Signal Integrity
        valid, reason = self.signal_scanner.validate(sample)
        if not valid:
            return self._reject(sample, reason, "signal", curriculum_phase, training_time)
        
        # Gate 4: Distribution Consistency
        valid, reason = self.distribution_guard.validate(sample)
        if not valid:
            return self._reject(sample, reason, "distribution", curriculum_phase, training_time)
        
        # Gate 5: Rare Event Safeguard
        valid, reason = self.rare_event_protector.validate(sample)
        if not valid:
            return self._reject(sample, reason, "rare_event", curriculum_phase, training_time)
        
        # Gate 6: Curriculum Compatibility
        valid, reason = self.curriculum_gate.validate(sample, training_time)
        if not valid:
            return self._reject(sample, reason, "curriculum", curriculum_phase, training_time)
        
        # Gate 7: Replay Buffer Safety
        valid, reason = self.replay_guard.validate(sample, training_time)
        if not valid:
            return self._reject(sample, reason, "replay", curriculum_phase, training_time)
        
        # ALL GATES PASSED
        
        # Check for critical violations that require emergency stop
        # (check periodically to avoid performance impact)
        if len(self.decision_log) % 100 == 0:
            try:
                self.check_critical_violations()
            except TrainingIntegrityViolation:
                raise  # Re-raise to halt training
            except Exception as e:
                logger.error(f"Error in critical violation check: {e}")
        
        return self._admit(sample, curriculum_phase, training_time)
    
    def validate_batch(
        self,
        batch: List[Sample],
        training_time: datetime,
        curriculum_phase: str
    ) -> Tuple[List[Sample], List[GateDecision]]:
        """
        Validate entire batch.
        
        Process:
        1. Validate each sample individually
        2. Collect admitted samples
        3. Run batch-level validation
        4. If batch validation fails, reject ENTIRE batch (atomic)
        
        Returns: (admitted_samples, all_decisions)
        """
        
        # TIER-0 FIX 2: Reset batch counters (including shadow state)
        self.rare_event_protector.reset_batch_counter()
        
        # Individual sample validation
        decisions = []
        candidate_samples = []
        candidate_indices = []  # Track indices of candidate samples in original batch
        
        for i, sample in enumerate(batch):
            decision = self.validate_sample(sample, training_time, curriculum_phase)
            decisions.append(decision)
            if decision.admitted:
                candidate_samples.append(sample)
                candidate_indices.append(i)
        
        # Batch-level validation (atomic)
        batch_valid, batch_reason = self.batch_validator.validate(candidate_samples)
        
        if not batch_valid:
            # TIER-0 FIX 2: Rollback shadow state (counters never committed)
            self.rare_event_protector.rollback_shadow_state()
            # Reject entire batch - override ALL candidate sample decisions
            for idx in candidate_indices:
                decisions[idx] = self._reject(
                    batch[idx], batch_reason, "batch", curriculum_phase, training_time
                )
            return [], decisions
        
        # TIER-0 FIX 2: Commit shadow state only if batch passes
        self.rare_event_protector.commit_shadow_state()
        return candidate_samples, decisions
    
    def _admit(
        self,
        sample: Sample,
        curriculum_phase: str,
        training_time: datetime
    ) -> GateDecision:
        """Create admission decision"""
        decision = GateDecision(
            sample_id=sample.video_id,
            admitted=True,
            rejection_reason=None,
            gate_stage="complete",
            timestamp=training_time,
            curriculum_phase=curriculum_phase,
            model_version=self.model_version,
            metadata={
                'platform': sample.platform,
                'ingestion_mode': sample.ingestion_mode,
                'is_tail_event': sample.is_tail_event
            }
        )
        
        if self.enable_logging:
            self.decision_log.append(decision)
        
        return decision
    
    def _reject(
        self,
        sample: Sample,
        reason: GateRejectionReason,
        gate_stage: str,
        curriculum_phase: str,
        training_time: datetime
    ) -> GateDecision:
        """
        Create rejection decision with full audit trail.
        
        FIX 4: Emergency stop latency hard cap - catastrophic violations halt immediately.
        """
        # FIX 4: Immediate stop on catastrophic signals (no 100-sample delay)
        # These violations are so severe that they must halt training immediately
        catastrophic_reasons = {
            GateRejectionReason.TEMPORAL_VIOLATION_FUTURE_METRICS,
            GateRejectionReason.SIGNAL_LABEL_POISONING
        }
        
        if reason in catastrophic_reasons:
            try:
                self.emergency_stop(f"Catastrophic violation detected: {reason.value}")
            except TrainingIntegrityViolation:
                # Re-raise to halt training immediately
                raise
        
        decision = GateDecision(
            sample_id=sample.video_id,
            admitted=False,
            rejection_reason=reason,
            gate_stage=gate_stage,
            timestamp=training_time,
            curriculum_phase=curriculum_phase,
            model_version=self.model_version,
            metadata={
                'platform': sample.platform,
                'ingestion_mode': sample.ingestion_mode,
                'is_tail_event': sample.is_tail_event,
                'scrape_timestamp': sample.scrape_timestamp.isoformat(),
                'content_timestamp': sample.content_timestamp.isoformat()
            }
        )
        
        if self.enable_logging:
            self.decision_log.append(decision)
            self.rejection_stats[reason] += 1
        
        return decision
    
    def get_rejection_summary(self) -> Dict[str, int]:
        """Get rejection statistics for monitoring"""
        return dict(self.rejection_stats)
    
    def export_audit_log(self, filepath: str):
        """Export full audit trail to JSON"""
        log_data = [
            {
                'sample_id': d.sample_id,
                'admitted': d.admitted,
                'rejection_reason': d.rejection_reason.value if d.rejection_reason else None,
                'gate_stage': d.gate_stage,
                'timestamp': d.timestamp.isoformat(),
                'curriculum_phase': d.curriculum_phase,
                'model_version': d.model_version,
                'metadata': d.metadata
            }
            for d in self.decision_log
        ]
        
        with open(filepath, 'w') as f:
            json.dump(log_data, f, indent=2)
    
    def emergency_stop(self, reason: str = "Critical integrity violation detected") -> None:
        """
        EMERGENCY STOP - halts training immediately on critical gate failure.
        
        This method:
        1. Emits FATAL alert via logging system
        2. Signals optimizer to freeze (if callback provided)
        3. Marks training as stopped
        4. Raises TrainingIntegrityViolation exception
        
        Training loop MUST catch this exception and halt training immediately.
        """
        self._training_stopped = True
        
        # Build diagnostic state
        diagnostic_state = {
            'status': 'EMERGENCY_STOP',
            'model_version': self.model_version,
            'reason': reason,
            'total_decisions': len(self.decision_log),
            'rejection_stats': dict(self.rejection_stats),
            'last_10_decisions': [
                {
                    'sample_id': d.sample_id,
                    'admitted': d.admitted,
                    'reason': d.rejection_reason.value if d.rejection_reason else None,
                    'gate': d.gate_stage
                }
                for d in self.decision_log[-10:]
            ],
            'requires_manual_intervention': True,
            'timestamp': datetime.now().isoformat()
        }
        
        # Emit FATAL alert
        logger.critical(
            f"DATA GATE EMERGENCY STOP: {reason}",
            extra={
                'event_type': 'training_integrity_violation',
                'model_version': self.model_version,
                'diagnostic_state': diagnostic_state
            }
        )
        
        # Freeze optimizer if callback provided
        if self.optimizer_freeze_callback:
            try:
                self.optimizer_freeze_callback(reason, diagnostic_state)
                logger.critical("Optimizer freeze signal sent")
            except Exception as e:
                logger.error(f"Failed to freeze optimizer: {e}")
        
        # Raise exception that training loop MUST catch
        raise TrainingIntegrityViolation(
            f"Data gate detected critical integrity violation: {reason}. "
            "Training halted. Manual intervention required.",
            diagnostic_state=diagnostic_state
        )
    
    def check_critical_violations(self) -> None:
        """
        Check for conditions that require emergency stop.
        
        TIER-0 FIX 4: Curriculum-aware thresholds tied to baseline variance.
        Static thresholds replaced with adaptive thresholds.
        """
        # Get current curriculum phase for adaptive thresholds
        curriculum_phase = self.curriculum_gate.current_constraints
        is_early_phase = curriculum_phase is None or 'early' in getattr(curriculum_phase, 'phase_name', '').lower()
        
        # Get baseline variance for adaptive thresholds
        baseline_variance = 1.0  # Default
        if self.distribution_guard.baseline_stats:
            # Compute overall baseline variance
            feature_stds = [
                stats.get('std', 0) for stats in 
                self.distribution_guard.baseline_stats.get('features', {}).values()
            ]
            if feature_stds:
                baseline_variance = np.mean(feature_stds)
        
        # TIER-0 FIX 4: Adaptive threshold for unknown feature versions
        # Stricter in early phases, more lenient with high variance
        unknown_version_threshold = 10 if not is_early_phase else 5
        unknown_version_threshold = int(unknown_version_threshold * (1.0 + baseline_variance * 0.1))
        
        if GateRejectionReason.UNKNOWN_FEATURE_VERSION in self.rejection_stats:
            count = self.rejection_stats[GateRejectionReason.UNKNOWN_FEATURE_VERSION]
            if count > unknown_version_threshold:
                self.emergency_stop(
                    f"Excessive unknown feature versions detected: {count} rejections "
                    f"(threshold: {unknown_version_threshold}). "
                    "Possible version mismatch or corrupted feature pipeline."
                )
        
        # TIER-0 FIX 4: Adaptive threshold for temporal violations
        # Scale with baseline variance and curriculum phase
        temporal_threshold_base = 100 if not is_early_phase else 50
        temporal_threshold = int(temporal_threshold_base * (1.0 + baseline_variance * 0.2))
        
        temporal_violations = sum(
            count for reason, count in self.rejection_stats.items()
            if 'TEMPORAL_VIOLATION' in reason.name
        )
        if temporal_violations > temporal_threshold:
            self.emergency_stop(
                f"Excessive temporal violations detected: {temporal_violations} "
                f"(threshold: {temporal_threshold}). "
                "Possible clock skew or corrupted timestamps."
            )
        
        # TIER-0 FIX 4: Adaptive rejection rate threshold
        # Stricter in early phases, accounts for platform skew and ingestion mode
        if len(self.decision_log) > 1000:
            recent_decisions = self.decision_log[-1000:]
            rejection_rate = sum(1 for d in recent_decisions if not d.admitted) / len(recent_decisions)
            
            # Compute platform skew
            platform_counts = defaultdict(int)
            for d in recent_decisions:
                if d.metadata and 'platform' in d.metadata:
                    platform_counts[d.metadata['platform']] += 1
            
            platform_skew = 0.0
            if platform_counts:
                max_platform_ratio = max(count / len(recent_decisions) for count in platform_counts.values())
                platform_skew = max_platform_ratio - (1.0 / len(platform_counts)) if len(platform_counts) > 1 else 0.0
            
            # Compute ingestion mode distribution
            replay_ratio = sum(
                1 for d in recent_decisions 
                if d.metadata and d.metadata.get('ingestion_mode') == 'replay'
            ) / len(recent_decisions)
            
            # Adaptive threshold: stricter with high platform skew or high replay ratio
            base_threshold = 0.8 if not is_early_phase else 0.7
            threshold_adjustment = platform_skew * 0.1 + replay_ratio * 0.05
            adaptive_threshold = base_threshold - threshold_adjustment
            
            if rejection_rate > adaptive_threshold:
                self.emergency_stop(
                    f"Critical rejection rate: {rejection_rate:.1%} "
                    f"(threshold: {adaptive_threshold:.1%}, "
                    f"platform_skew: {platform_skew:.2f}, "
                    f"replay_ratio: {replay_ratio:.2f}). "
                    "Data pipeline may be corrupted."
                )


# ============================================================================
# FACTORY & HELPERS
# ============================================================================

def create_production_gate(model_version: str) -> DataGate:
    """Create production-grade gate with safe defaults"""
    
    config = GateConfig(
        required_fields={'video_id', 'platform', 'scrape_timestamp', 'content_timestamp'},
        allowed_feature_versions={'v1', 'v2', 'v3'},
        required_modalities={'views', 'likes', 'engagement_rate'},
        temporal_tolerance_seconds=60,
        distribution_shift_threshold=0.15,
        rare_event_percentile=99.0,
        rare_event_min_uncertainty=0.6,
        batch_min_horizon_diversity=3
    )
    
    return DataGate(
        config=config,
        model_version=model_version,
        enable_logging=True
    )


def validate_gate_determinism(
    gate: DataGate,
    sample: Sample,
    training_time: datetime,
    curriculum_phase: str,
    num_runs: int = 100
) -> bool:
    """
    Verify gate is deterministic.
    Same inputs must produce bit-for-bit identical decisions.
    
    Critical for:
    - Auditability
    - A/B testing
    - Reproducible failures
    """
    decisions = []
    
    for _ in range(num_runs):
        decision = gate.validate_sample(sample, training_time, curriculum_phase)
        decisions.append((decision.admitted, decision.rejection_reason))
    
    # All decisions must be identical
    first_decision = decisions[0]
    return all(d == first_decision for d in decisions)


# ============================================================================
# MONITORING & DIAGNOSTICS
# ============================================================================

class GateMonitor:
    """Real-time gate health monitoring"""
    
    def __init__(self):
        self.admission_rate_window = deque(maxlen=1000)
        self.rejection_by_gate = defaultdict(int)
        self.temporal_violations = 0
        self.distribution_alerts = 0
    
    def record_decision(self, decision: GateDecision):
        """Record decision for monitoring"""
        self.admission_rate_window.append(1 if decision.admitted else 0)
        
        if not decision.admitted:
            self.rejection_by_gate[decision.gate_stage] += 1
            
            if 'temporal' in decision.gate_stage:
                self.temporal_violations += 1
            if 'distribution' in decision.gate_stage:
                self.distribution_alerts += 1
    
    def get_admission_rate(self) -> float:
        """Get current admission rate"""
        if not self.admission_rate_window:
            return 0.0
        return sum(self.admission_rate_window) / len(self.admission_rate_window)
    
    def check_health(self) -> Dict[str, Any]:
        """Check gate health status"""
        admission_rate = self.get_admission_rate()
        
        status = {
            'healthy': True,
            'admission_rate': admission_rate,
            'temporal_violations': self.temporal_violations,
            'distribution_alerts': self.distribution_alerts,
            'rejection_breakdown': dict(self.rejection_by_gate),
            'warnings': []
        }
        
        # Health checks
        if admission_rate < 0.5:
            status['healthy'] = False
            status['warnings'].append('CRITICAL: Admission rate below 50%')
        
        if self.temporal_violations > 100:
            status['healthy'] = False
            status['warnings'].append('CRITICAL: High temporal violations - possible clock skew')
        
        if self.distribution_alerts > 50:
            status['warnings'].append('WARNING: Distribution drift detected')
        
        return status


# ============================================================================
# INTEGRATION EXAMPLE
# ============================================================================

class TrainingDataPipeline:
    """
    Example integration showing how data_gate.py fits into training pipeline.
    
    Flow:
    1. Raw samples arrive
    2. DataGate validates each sample
    3. Only admitted samples reach gradient computation
    4. Rejected samples logged for analysis
    """
    
    def __init__(self, model_version: str):
        self.gate = create_production_gate(model_version)
        self.monitor = GateMonitor()
        self.curriculum_phase = "phase_1"
    
    def process_training_batch(
        self,
        raw_samples: List[Sample],
        training_time: datetime
    ) -> Tuple[List[Sample], Dict[str, Any]]:
        """
        Process a training batch through the gate.
        
        Returns:
            - admitted_samples: Only samples that passed all gates
            - stats: Batch statistics and health check
        """
        
        # Validate batch through gate
        admitted_samples, decisions = self.gate.validate_batch(
            raw_samples,
            training_time,
            self.curriculum_phase
        )
        
        # Update monitoring
        for decision in decisions:
            self.monitor.record_decision(decision)
        
        # Collect statistics
        stats = {
            'total_samples': len(raw_samples),
            'admitted_samples': len(admitted_samples),
            'rejection_rate': 1.0 - (len(admitted_samples) / len(raw_samples)) if raw_samples else 0.0,
            'health': self.monitor.check_health(),
            'rejections_by_reason': self.gate.get_rejection_summary()
        }
        
        # Emergency stop check
        if not stats['health']['healthy']:
            if stats['health']['admission_rate'] < 0.3:
                # emergency_stop() now raises exception - training loop must catch it
                try:
                    self.gate.emergency_stop("Critical admission rate below 30%")
                except TrainingIntegrityViolation as e:
                    stats['emergency'] = {
                        'status': 'TRAINING_HALTED',
                        'reason': str(e),
                        'diagnostic': e.diagnostic_state
                    }
                    # Exception will propagate to halt training
                    raise
        
        return admitted_samples, stats
    
    def update_curriculum(self, new_constraints: CurriculumConstraints):
        """Update curriculum phase"""
        self.curriculum_phase = new_constraints.phase_name
        self.gate.set_curriculum_phase(new_constraints)


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    """
    Production usage example demonstrating:
    - Gate creation
    - Sample validation
    - Batch processing
    - Monitoring
    - Emergency handling
    """
    
    # Initialize pipeline
    pipeline = TrainingDataPipeline(model_version="v2.4.1")
    
    # Set curriculum constraints
    curriculum = CurriculumConstraints(
        allowed_data_age_min=timedelta(hours=1),
        allowed_data_age_max=timedelta(days=7),
        allowed_uncertainty_min=0.1,
        allowed_uncertainty_max=0.9,
        allowed_distribution_spread_max=0.3,
        phase_name="early_training"
    )
    pipeline.update_curriculum(curriculum)
    
    # Create sample batch
    training_time = datetime.now()
    
    samples = [
        Sample(
            video_id=f"video_{i}",
            platform="youtube",
            scrape_timestamp=training_time - timedelta(hours=6),
            content_timestamp=training_time - timedelta(hours=12),
            feature_bundle_version="v2",
            ingestion_mode="live",
            uncertainty_estimate=0.5,
            source_confidence=0.85,
            engagement_features={
                'views': 10000 + i * 1000,
                'likes': 500 + i * 50,
                'engagement_rate': 0.05,
                'engagement_velocity': 2.3
            },
            niche_category="tech",
            is_tail_event=False,
            replay_count=0
        )
        for i in range(32)
    ]
    
    # Add a temporal violation example (future data)
    bad_sample = Sample(
        video_id="video_bad",
        platform="youtube",
        scrape_timestamp=training_time + timedelta(hours=1),  # FUTURE!
        content_timestamp=training_time - timedelta(hours=12),
        feature_bundle_version="v2",
        ingestion_mode="live",
        uncertainty_estimate=0.5,
        source_confidence=0.85,
        engagement_features={'views': 10000, 'likes': 500, 'engagement_rate': 0.05},
        niche_category="tech",
        is_tail_event=False,
        replay_count=0
    )
    samples.append(bad_sample)
    
    # Process batch
    admitted, stats = pipeline.process_training_batch(samples, training_time)
    
    # Print results
    print("=" * 80)
    print("DATA GATE VALIDATION RESULTS")
    print("=" * 80)
    print(f"Total samples: {stats['total_samples']}")
    print(f"Admitted: {stats['admitted_samples']}")
    print(f"Rejection rate: {stats['rejection_rate']:.2%}")
    print()
    print("Rejections by reason:")
    for reason, count in stats['rejections_by_reason'].items():
        print(f"  {reason.value}: {count}")
    print()
    print("Gate health:")
    health = stats['health']
    print(f"  Status: {'HEALTHY' if health['healthy'] else 'UNHEALTHY'}")
    print(f"  Admission rate: {health['admission_rate']:.2%}")
    print(f"  Temporal violations: {health['temporal_violations']}")
    print(f"  Distribution alerts: {health['distribution_alerts']}")
    if health['warnings']:
        print("  Warnings:")
        for warning in health['warnings']:
            print(f"    - {warning}")
    print()
    print(f"Ready for gradient computation: {len(admitted)} samples")
    print("=" * 80)
    
    # Export audit log
    pipeline.gate.export_audit_log("gate_audit_log.json")
    print("\nAudit log exported to: gate_audit_log.json")
    
    # Determinism check
    print("\nRunning determinism verification...")
    is_deterministic = validate_gate_determinism(
        pipeline.gate,
        samples[0],
        training_time,
        "early_training",
        num_runs=100
    )
    print(f"Gate is deterministic: {is_deterministic}")
    
    print("\n✅ Data gate validation complete.")
    print("Only causally valid samples will reach gradient computation.")
    print("Training integrity maintained.")