"""
/models/evaluation/metrics.py

Authoritative Metric Contract Layer (NOT dashboards, NOT KPIs)

What this file ACTUALLY does (precise):
This file answers exactly one question:
"What quantitative measurements are allowed to exist in the system, 
and how are they computed in a causally valid way?"

It does NOT answer:
  - "What's good?"
  - "What should we optimize?"
  - "Which video wins?"

It defines measurement, not judgment.

Why this file is critical:
Every failure mode at scale usually comes from:
  - metrics doing double duty
  - metrics leaking future information
  - metrics quietly mutating
  - metrics being re-interpreted downstream

This file exists to:
  - Freeze semantics
  - Prevent silent redefinition
  - Protect causality

Architectural Placement (LOCK THIS):
models/ml_models/
models/rl_agents/
        ↓
models/evaluation/
  ├── metrics.py              ← YOU ARE HERE
  ├── validation_pipeline.py
  ├── early_signal_detector.py
        ↓
training / orchestration / dashboards

All metrics must originate here or be rejected.

Core Principle (NON-NEGOTIABLE):
Metrics describe reality — they never decide actions.
The moment a metric makes a decision, the system becomes un-debuggable.

LOC: ~2,493 (production-grade, Tier-0 hardened, audit-defensible)
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Callable, Tuple
import json
import hashlib
import numpy as np
from collections import defaultdict


# ============================================================================
# METRIC TAXONOMY (STRICT HIERARCHY)
# ============================================================================

class MetricCategory(Enum):
    """
    Strict categorization of all allowed metrics.
    
    All metrics fall into exactly one category.
    """
    # 1. Primitive Metrics (ATOMIC) - Direct measurements. No inference.
    PRIMITIVE = "primitive"
    
    # 2. Temporal Delta Metrics - Change-over-time measurements
    TEMPORAL_DELTA = "temporal_delta"
    
    # 3. Ratio Metrics (LOCAL ONLY) - Computed at same timestamp
    RATIO = "ratio"
    
    # 4. Retention Metrics - Never extrapolated, window-locked
    RETENTION = "retention"
    
    # 5. Structural Metrics - Derived from content structure, not engagement
    STRUCTURAL = "structural"
    
    # Legacy categories (for backward compatibility)
    ENGAGEMENT = "engagement"  # Maps to PRIMITIVE
    VELOCITY = "velocity"  # Maps to TEMPORAL_DELTA
    DISTRIBUTION = "distribution"  # Maps to PRIMITIVE
    STRUCTURAL_STABILITY = "structural_stability"  # Maps to STRUCTURAL
    LONG_TAIL = "long_tail"  # Maps to TEMPORAL_DELTA
    RISK = "risk"  # Maps to PRIMITIVE
    DIAGNOSTIC = "diagnostic"  # Maps to PRIMITIVE


class MetricUsagePolicy(Enum):
    """Defines what can consume this metric."""
    OBSERVATION_ONLY = "observation_only"  # Dashboards, logs
    RL_REWARD = "rl_reward"  # RL agents can optimize
    RL_CONSTRAINT = "rl_constraint"  # RL agents must respect
    EARLY_SIGNAL = "early_signal"  # Early detection systems
    STRATEGIC_ONLY = "strategic_only"  # Long-term planning, not real-time
    DEBUG_ONLY = "debug_only"  # Never in optimization loops


class GamingAction(Enum):
    """Automatic actions taken when gaming is detected."""
    NONE = "none"  # No action
    WARN = "warn"  # Log warning
    PENALIZE = "penalize"  # Auto-downweight metric value
    QUARANTINE = "quarantine"  # Remove from RL, block computation
    DEPRECATE = "deprecate"  # Auto-deprecate metric


class InvariantSeverity(Enum):
    """Severity levels for metric invariants with typed escalation."""
    WARN = "warn"  # Log warning, continue
    FAIL = "fail"  # Log error, quarantine metrics
    HALT = "halt"  # System halt on violation


class MetricFailureMode(Enum):
    """Formal failure modes for metrics."""
    DATA_MISSING = "data_missing"
    TIME_VIOLATION = "time_violation"
    GAMING_DETECTED = "gaming_detected"
    DRIFT_DETECTED = "drift_detected"
    INVARIANT_BREACH = "invariant_breach"
    QUARANTINED = "quarantined"
    DEPRECATED = "deprecated"


# ============================================================================
# TIME SAFETY ENFORCEMENT
# ============================================================================

@dataclass(frozen=True)
class TimeWindow:
    """Enforces temporal validity of metric computation."""
    min_age_seconds: int
    max_age_seconds: int
    uses_future_data: bool = False
    
    def __post_init__(self):
        if self.min_age_seconds < 0:
            raise ValueError("min_age_seconds must be >= 0")
        if self.max_age_seconds <= self.min_age_seconds:
            raise ValueError("max_age_seconds must be > min_age_seconds")
        if self.uses_future_data:
            raise ValueError("FORBIDDEN: No metric may use future data")
    
    def is_valid_age(self, video_age_seconds: int) -> bool:
        """Check if video age is within valid window."""
        return self.min_age_seconds <= video_age_seconds <= self.max_age_seconds
    
    def validate_or_fail(self, video_age_seconds: int, metric_name: str):
        """Hard fail if time window violated."""
        if not self.is_valid_age(video_age_seconds):
            raise MetricTimeSafetyViolation(
                f"Metric '{metric_name}' requires age {self.min_age_seconds}-{self.max_age_seconds}s, "
                f"got {video_age_seconds}s"
            )


# ============================================================================
# PROVENANCE & CONFIDENCE MODELING (TRUST WEIGHTING)
# ============================================================================

@dataclass
class CorrelationProvenance:
    """Provenance metadata for correlation matrix inputs."""
    source: str  # Where the correlation came from
    confidence: float  # Confidence in correlation [0.0, 1.0]
    sample_size: int  # Number of samples used
    timestamp: datetime  # When correlation was computed
    computation_method: str  # Method used (e.g., "pearson", "spearman")
    trust_level: float = 1.0  # Trust level [0.0, 1.0] based on source reliability
    
    def compute_trust_weight(self) -> float:
        """Compute trust weight based on provenance metadata."""
        # Base confidence
        weight = self.confidence
        
        # Sample size penalty (small samples less trustworthy)
        if self.sample_size < 100:
            weight *= 0.7
        elif self.sample_size < 500:
            weight *= 0.9
        
        # Source trust level
        weight *= self.trust_level
        
        # Age decay (older correlations less trustworthy)
        age_days = (datetime.utcnow() - self.timestamp).days
        if age_days > 30:
            weight *= max(0.5, 1.0 - (age_days - 30) * 0.01)
        
        return max(0.0, min(1.0, weight))


@dataclass
class AnomalyProvenance:
    """Provenance metadata for anomaly score inputs."""
    detector_type: str  # Type of anomaly detector
    confidence: float  # Detector confidence [0.0, 1.0]
    evidence_count: int  # Number of evidence pieces
    cross_validation_score: float = 0.0  # Cross-validation confidence
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def compute_trust_weight(self) -> float:
        """Compute trust weight for anomaly score."""
        weight = self.confidence
        
        # Evidence count weighting
        if self.evidence_count < 3:
            weight *= 0.6
        elif self.evidence_count < 5:
            weight *= 0.8
        
        # Cross-validation boost
        if self.cross_validation_score > 0.7:
            weight = min(1.0, weight * 1.1)
        
        return max(0.0, min(1.0, weight))


def extract_provenance(context: Dict[str, Any]) -> Tuple[Optional[CorrelationProvenance], Optional[AnomalyProvenance]]:
    """Extract provenance metadata from context."""
    corr_provenance = None
    if 'correlation_provenance' in context:
        prov_data = context['correlation_provenance']
        corr_provenance = CorrelationProvenance(
            source=prov_data.get('source', 'unknown'),
            confidence=prov_data.get('confidence', 0.5),
            sample_size=prov_data.get('sample_size', 0),
            timestamp=prov_data.get('timestamp', datetime.utcnow()),
            computation_method=prov_data.get('method', 'pearson'),
            trust_level=prov_data.get('trust_level', 0.5)
        )
    
    anomaly_provenance = None
    if 'anomaly_provenance' in context:
        prov_data = context['anomaly_provenance']
        anomaly_provenance = AnomalyProvenance(
            detector_type=prov_data.get('detector_type', 'unknown'),
            confidence=prov_data.get('confidence', 0.5),
            evidence_count=prov_data.get('evidence_count', 0),
            cross_validation_score=prov_data.get('cross_validation', 0.0),
            timestamp=prov_data.get('timestamp', datetime.utcnow())
        )
    
    return corr_provenance, anomaly_provenance


# ============================================================================
# ANTI-GAMING GUARDS
# ============================================================================

@dataclass
class AntiGamingSpec:
    """Defines what a metric correlates with and manipulation sensitivity."""
    intended_signal: str
    forbidden_correlations: List[str]
    saturation_limit: Optional[float] = None
    manipulation_sensitivity: float = 1.0  # 0.0 = robust, 1.0 = fragile
    action_thresholds: Dict[GamingAction, float] = field(default_factory=lambda: {
        GamingAction.WARN: 0.4,
        GamingAction.PENALIZE: 0.6,
        GamingAction.QUARANTINE: 0.75,
        GamingAction.DEPRECATE: 0.9
    })
    
    def get_action(self, risk_score: float) -> GamingAction:
        """Determine action based on risk score."""
        if risk_score >= self.action_thresholds.get(GamingAction.DEPRECATE, 0.9):
            return GamingAction.DEPRECATE
        elif risk_score >= self.action_thresholds.get(GamingAction.QUARANTINE, 0.75):
            return GamingAction.QUARANTINE
        elif risk_score >= self.action_thresholds.get(GamingAction.PENALIZE, 0.6):
            return GamingAction.PENALIZE
        elif risk_score >= self.action_thresholds.get(GamingAction.WARN, 0.4):
            return GamingAction.WARN
        return GamingAction.NONE
    
    def check_gaming_risk(self, metric_value: float, context: Dict[str, Any]) -> float:
        """
        Returns gaming risk score [0.0, 1.0].
        Higher = more likely being gamed.
        
        Checks:
        1. Saturation limits
        2. Forbidden correlations with suspicious signals (trust-weighted)
        3. Anomaly patterns in correlation matrix (trust-weighted)
        """
        risk = 0.0
        
        # Check saturation (always trusted - direct metric value)
        if self.saturation_limit and metric_value > self.saturation_limit:
            risk += 0.5
        
        # Extract provenance for trust weighting
        corr_provenance, anomaly_provenance = extract_provenance(context)
        
        # Check forbidden correlations (with provenance trust weighting)
        if self.forbidden_correlations and 'correlation_matrix' in context:
            corr_matrix = context['correlation_matrix']
            
            # Compute trust weight for correlation matrix
            corr_trust_weight = 1.0
            if corr_provenance:
                corr_trust_weight = corr_provenance.compute_trust_weight()
            elif 'correlation_confidence' in context:
                # Fallback to simple confidence if provenance not provided
                corr_trust_weight = context.get('correlation_confidence', 0.5)
            else:
                # No provenance = lower trust (partial-trust weighting)
                corr_trust_weight = 0.6  # Default low trust for untagged inputs
            
            for forbidden_signal in self.forbidden_correlations:
                if forbidden_signal in corr_matrix:
                    corr_value = corr_matrix[forbidden_signal]
                    # High positive correlation with forbidden signal indicates gaming
                    if corr_value > 0.7:
                        risk_contribution = 0.3 * corr_trust_weight
                        risk += risk_contribution
                    elif corr_value > 0.5:
                        risk_contribution = 0.15 * corr_trust_weight
                        risk += risk_contribution
        
        # Check for suspicious signal patterns in context
        if 'suspicious_signals' in context:
            suspicious_count = len(context['suspicious_signals'])
            if suspicious_count > 0:
                # Apply trust weighting if available
                signal_trust = context.get('suspicious_signals_confidence', 0.8)
                risk += min(suspicious_count * 0.1, 0.3) * signal_trust
        
        # Check for anomalous patterns (bot-like behavior) with provenance
        if 'anomaly_score' in context:
            anomaly = context['anomaly_score']
            
            # Compute trust weight for anomaly score
            anomaly_trust_weight = 1.0
            if anomaly_provenance:
                anomaly_trust_weight = anomaly_provenance.compute_trust_weight()
            elif 'anomaly_confidence' in context:
                anomaly_trust_weight = context.get('anomaly_confidence', 0.5)
            else:
                # No provenance = lower trust
                anomaly_trust_weight = 0.6
            
            if anomaly > 0.8:
                risk += 0.2 * anomaly_trust_weight
            elif anomaly > 0.5:
                risk += 0.1 * anomaly_trust_weight
        
        # Apply manipulation sensitivity multiplier
        final_risk = min(risk * self.manipulation_sensitivity, 1.0)
        return final_risk


# ============================================================================
# METRIC VERSIONING
# ============================================================================

@dataclass(frozen=True)
class MetricVersion:
    """Semantic versioning for metrics."""
    major: int
    minor: int
    patch: int
    
    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"
    
    def is_compatible_with(self, other: 'MetricVersion') -> bool:
        """Check backward compatibility (same major version)."""
        return self.major == other.major
    
    @classmethod
    def from_string(cls, version_str: str) -> 'MetricVersion':
        """Parse 'v1.2.3' format."""
        parts = version_str.lstrip('v').split('.')
        return cls(int(parts[0]), int(parts[1]), int(parts[2]))


# ============================================================================
# METRIC SPECIFICATION (CANONICAL DEFINITION)
# ============================================================================

# FORBIDDEN METRIC NAMES (HARD RULE)
FORBIDDEN_METRIC_NAMES = {
    'virality_score',
    'quality_score',
    'boost_score',
    'trending_score',
    'shadow_rank',
    'heuristic_composite',
    'composite_score',
    'ranking_score',
    'popularity_score',
    'win_score'
}

FORBIDDEN_PATTERNS = [
    '_score',  # No scoring metrics
    '_rank',   # No ranking metrics
    '_boost',  # No boosting metrics
    '_trend',  # No trending metrics
    '_viral',  # No virality metrics
    '_quality' # No quality metrics
]


@dataclass
class MetricSpec:
    """
    Canonical specification for a single metric.
    
    This is the ONLY way to define a metric in the system.
    
    Metric Definition Contract (MANDATORY):
    Every metric must define all required fields or be rejected.
    """
    name: str
    category: MetricCategory
    version: MetricVersion
    time_window: TimeWindow
    usage_policy: MetricUsagePolicy
    anti_gaming: AntiGamingSpec
    description: str
    unit: str
    compute_fn: Callable[[Dict[str, Any]], float]
    
    # NEW SPEC REQUIREMENTS
    inputs: List[str] = field(default_factory=list)  # Required input field names
    aggregation: Optional[str] = None  # Allowed: "sum", "mean", "median", "percentile"
    min_points: Optional[int] = None  # Minimum data points required
    allows_null: bool = False  # Whether metric can return null/None
    deterministic: bool = True  # Must be True - no randomness allowed
    
    # Metadata
    requires_retention_data: bool = False
    requires_distribution_data: bool = False
    confidence_weighted: bool = False
    
    # Causal assumptions (machine-readable)
    assumed_causal_parents: List[str] = field(default_factory=list)
    forbidden_causal_dependents: List[str] = field(default_factory=list)
    
    # Deprecation
    deprecated: bool = False
    deprecation_date: Optional[datetime] = None
    replacement_metric: Optional[str] = None
    
    def __post_init__(self):
        """Validate specification integrity."""
        # FORBIDDEN METRIC NAME CHECK (HARD RULE)
        if self.name in FORBIDDEN_METRIC_NAMES:
            raise ValueError(
                f"FORBIDDEN: Metric name '{self.name}' is explicitly forbidden. "
                f"Metrics must describe reality, not make judgments."
            )
        
        # Check for forbidden patterns
        name_lower = self.name.lower()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern in name_lower:
                raise ValueError(
                    f"FORBIDDEN: Metric name '{self.name}' contains forbidden pattern '{pattern}'. "
                    f"Metrics must measure, not score/rank/boost."
                )
        
        # Name validation
        if not self.name or not isinstance(self.name, str):
            raise ValueError("Metric name must be non-empty string")
        if not self.name.replace('_', '').replace('-', '').isalnum():
            raise ValueError(f"Metric name '{self.name}' must be alphanumeric with underscores/dashes only")
        
        # NEW SPEC VALIDATION: Determinism requirement
        if not self.deterministic:
            raise ValueError(
                f"FORBIDDEN: Metric '{self.name}' must be deterministic. "
                f"No randomness allowed in metric computation."
            )
        
        # NEW SPEC VALIDATION: Aggregation rules
        if self.aggregation is not None:
            allowed_aggregations = {'sum', 'mean', 'median', 'percentile'}
            if self.aggregation not in allowed_aggregations:
                raise ValueError(
                    f"Metric '{self.name}' aggregation must be one of {allowed_aggregations}, "
                    f"got '{self.aggregation}'"
                )
        
        # NEW SPEC VALIDATION: Category-specific rules
        if self.category == MetricCategory.RATIO:
            # Ratio metrics must be computed at same timestamp
            if self.time_window.max_age_seconds > 3600:  # > 1 hour
                raise ValueError(
                    f"Ratio metric '{self.name}' time window too large. "
                    f"Ratio metrics must be computed at same timestamp (local only)."
                )
        
        if self.category == MetricCategory.TEMPORAL_DELTA:
            # Temporal delta requires min_points
            if self.min_points is None:
                if 'velocity' in self.name.lower():
                    self.min_points = 3  # ≥3 points for velocity
                elif 'acceleration' in self.name.lower():
                    self.min_points = 5  # ≥5 points for acceleration
                else:
                    self.min_points = 3  # Default for temporal delta
        
        # Description and unit validation
        if not self.description or not isinstance(self.description, str):
            raise ValueError(f"Metric '{self.name}' must have non-empty description")
        if not self.unit or not isinstance(self.unit, str):
            raise ValueError(f"Metric '{self.name}' must have non-empty unit")
        
        # Compute function validation
        if not callable(self.compute_fn):
            raise ValueError(f"Metric '{self.name}' compute_fn must be callable")
        
        # Deprecation validation
        if self.deprecated and not self.replacement_metric:
            raise ValueError(f"Deprecated metric '{self.name}' must specify replacement")
        
        # Usage policy constraints
        if self.category == MetricCategory.DIAGNOSTIC:
            if self.usage_policy != MetricUsagePolicy.DEBUG_ONLY:
                raise ValueError("Diagnostic metrics must be DEBUG_ONLY")
        
        if self.category == MetricCategory.LONG_TAIL:
            if self.usage_policy == MetricUsagePolicy.RL_REWARD:
                raise ValueError("Long-tail metrics cannot be RL_REWARD (strategic only)")
        
        # Anti-gaming validation
        if not isinstance(self.anti_gaming, AntiGamingSpec):
            raise ValueError(f"Metric '{self.name}' anti_gaming must be AntiGamingSpec")
        if self.anti_gaming.manipulation_sensitivity < 0.0 or self.anti_gaming.manipulation_sensitivity > 1.0:
            raise ValueError(f"Metric '{self.name}' manipulation_sensitivity must be in [0.0, 1.0]")
    
    def compute(self, data: Dict[str, Any], video_age_seconds: int, computation_timestamp: Optional[datetime] = None) -> 'MetricResult':
        """
        Compute metric value with full validation.
        
        DETERMINISM CONTRACT:
        Given same inputs, same time window, same version → identical output.
        """
        # Time safety check
        self.time_window.validate_or_fail(video_age_seconds, self.name)
        
        # Data availability check
        if self.requires_retention_data and 'retention_curve' not in data:
            return MetricResult.unavailable(self.name, self.version, "retention_data_missing", computation_timestamp)
        
        if self.requires_distribution_data and 'distribution' not in data:
            return MetricResult.unavailable(self.name, self.version, "distribution_data_missing", computation_timestamp)
        
        # Compute with redundant validation layers
        try:
            # REDUNDANT VALIDATION LAYER 1: Data structure validation
            if self.requires_retention_data:
                if 'retention_curve' not in data:
                    return MetricResult.unavailable(self.name, self.version, "retention_data_missing", computation_timestamp)
                validate_retention_data(data['retention_curve'], self.name)
            
            if self.requires_distribution_data:
                if 'distribution' not in data:
                    return MetricResult.unavailable(self.name, self.version, "distribution_data_missing", computation_timestamp)
                validate_distribution_data(data['distribution'], self.name)
            
            # REDUNDANT VALIDATION LAYER 2: Compute function validation
            if not callable(self.compute_fn):
                return MetricResult.failed(self.name, self.version, "compute_fn_not_callable", computation_timestamp)
            
            # REDUNDANT VALIDATION LAYER 3: Data type validation
            if not isinstance(data, dict):
                return MetricResult.failed(self.name, self.version, f"data_must_be_dict_got_{type(data).__name__}", computation_timestamp)
            
            # Execute computation
            value = self.compute_fn(data)
            
            # REDUNDANT VALIDATION LAYER 4: Post-computation validation
            if value is None:
                return MetricResult.failed(self.name, self.version, "compute_fn_returned_none", computation_timestamp)
            
            # Validate computed value (primary validation)
            value = validate_metric_value(value, self.name)
            
            # REDUNDANT VALIDATION LAYER 5: Bounds checking (category-specific)
            if self.category == MetricCategory.RETENTION:
                if value < 0.0 or value > 1.0:
                    # Allow out-of-bounds but flag
                    if value < -1.0 or value > 2.0:
                        return MetricResult.failed(self.name, self.version, f"retention_value_out_of_bounds_{value}", computation_timestamp)
            
            # Gaming risk check
            gaming_risk = self.anti_gaming.check_gaming_risk(value, data)
            
            # Check forbidden causal dependents
            if self.forbidden_causal_dependents and 'correlation_matrix' in data:
                corr_matrix = data['correlation_matrix']
                for forbidden_dep in self.forbidden_causal_dependents:
                    if forbidden_dep in corr_matrix:
                        corr_value = corr_matrix[forbidden_dep]
                        if corr_value > 0.8:  # Strong correlation with forbidden dependent
                            gaming_risk = max(gaming_risk, 0.9)  # Force high risk
            
            # Confidence weighting
            confidence = 1.0
            if self.confidence_weighted:
                confidence = data.get('data_confidence', 1.0)
                if not isinstance(confidence, (int, float)) or confidence < 0.0 or confidence > 1.0:
                    confidence = 1.0
            
            # Deterministic timestamp: use provided timestamp or derive from data
            if computation_timestamp is None:
                # Use timestamp from data if available (deterministic)
                if 'computation_timestamp' in data:
                    computation_timestamp = data['computation_timestamp']
                elif 'timestamp' in data:
                    # Try to parse timestamp from data
                    ts = data['timestamp']
                    if isinstance(ts, datetime):
                        computation_timestamp = ts
                    elif isinstance(ts, str):
                        computation_timestamp = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    else:
                        # Fallback: use epoch + video_age_seconds for determinism
                        computation_timestamp = datetime(1970, 1, 1) + timedelta(seconds=video_age_seconds)
                else:
                    # Deterministic fallback: epoch + video_age_seconds
                    computation_timestamp = datetime(1970, 1, 1) + timedelta(seconds=video_age_seconds)
            
            return MetricResult(
                metric_name=self.name,
                version=self.version,
                value=value,
                confidence=confidence,
                gaming_risk=gaming_risk,
                timestamp=computation_timestamp,
                video_age_seconds=video_age_seconds
            )
        except Exception as e:
            return MetricResult.failed(self.name, self.version, str(e), computation_timestamp)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize spec for audit trail."""
        return {
            'name': self.name,
            'category': self.category.value,
            'version': str(self.version),
            'time_window': {
                'min_age_seconds': self.time_window.min_age_seconds,
                'max_age_seconds': self.time_window.max_age_seconds
            },
            'usage_policy': self.usage_policy.value,
            'deprecated': self.deprecated
        }


# ============================================================================
# METRIC RESULT (COMPUTATION OUTPUT)
# ============================================================================

@dataclass
class MetricResult:
    """Result of a metric computation."""
    metric_name: str
    version: MetricVersion
    value: Optional[float]
    confidence: float
    gaming_risk: float
    timestamp: datetime
    video_age_seconds: int
    
    available: bool = True
    failure_reason: Optional[str] = None
    
    @classmethod
    def unavailable(cls, name: str, version: MetricVersion, reason: str, computation_timestamp: Optional[datetime] = None) -> 'MetricResult':
        """Create unavailable result."""
        if computation_timestamp is None:
            # Deterministic fallback: use epoch
            computation_timestamp = datetime(1970, 1, 1)
        return cls(
            metric_name=name,
            version=version,
            value=None,
            confidence=0.0,
            gaming_risk=0.0,
            timestamp=computation_timestamp,
            video_age_seconds=0,
            available=False,
            failure_reason=reason
        )
    
    @classmethod
    def failed(cls, name: str, version: MetricVersion, error: str, computation_timestamp: Optional[datetime] = None) -> 'MetricResult':
        """Create failed result."""
        return cls.unavailable(name, version, f"computation_failed: {error}", computation_timestamp)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/API."""
        return {
            'metric_name': self.metric_name,
            'version': str(self.version),
            'value': self.value,
            'confidence': self.confidence,
            'gaming_risk': self.gaming_risk,
            'timestamp': self.timestamp.isoformat(),
            'video_age_seconds': self.video_age_seconds,
            'available': self.available,
            'failure_reason': self.failure_reason
        }


# ============================================================================
# CROSS-METRIC INVARIANT ENFORCEMENT
# ============================================================================

@dataclass(frozen=True)
class MetricInvariant:
    """Cross-metric invariant that must hold."""
    name: str
    metrics: Tuple[str, ...]
    condition: Callable[[Dict[str, float]], bool]
    severity: InvariantSeverity  # Typed severity enum
    description: str
    
    def check(self, metric_values: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """
        Check if invariant holds.
        
        Returns:
            (is_valid, error_message)
        """
        # Check all required metrics are present
        missing = [m for m in self.metrics if m not in metric_values]
        if missing:
            return True, None  # Can't check if data missing
        
        try:
            is_valid = self.condition(metric_values)
            if not is_valid:
                return False, f"Invariant '{self.name}' violated: {self.description}"
            return True, None
        except Exception as e:
            return False, f"Invariant '{self.name}' check failed: {str(e)}"


# ============================================================================
# METRIC DRIFT DETECTION
# ============================================================================

@dataclass
class DriftReport:
    """Report of metric drift between versions."""
    metric_name: str
    old_version: MetricVersion
    new_version: MetricVersion
    ks_statistic: float
    p_value: float
    mean_shift: float
    mean_shift_sigma: float
    tail_mass_change: float
    drift_detected: bool
    recommendation: str  # "allow" | "shadow" | "block"
    timestamp: datetime


class MetricDriftMonitor:
    """Monitors metric drift across version changes."""
    
    def __init__(self):
        self._version_samples: Dict[Tuple[str, MetricVersion], List[float]] = defaultdict(list)
    
    def record_sample(self, metric_name: str, version: MetricVersion, value: float):
        """Record a metric value sample for drift detection."""
        key = (metric_name, version)
        self._version_samples[key].append(value)
        # Keep only last 10,000 samples per version
        if len(self._version_samples[key]) > 10000:
            self._version_samples[key] = self._version_samples[key][-10000:]
    
    def compare_distributions(
        self,
        metric_name: str,
        old_version: MetricVersion,
        new_version: MetricVersion,
        min_samples: int = 100
    ) -> DriftReport:
        """
        Compare distributions between versions using KS-test.
        
        Returns:
            DriftReport with drift analysis
        """
        old_key = (metric_name, old_version)
        new_key = (metric_name, new_version)
        
        old_samples = self._version_samples.get(old_key, [])
        new_samples = self._version_samples.get(new_key, [])
        
        if len(old_samples) < min_samples or len(new_samples) < min_samples:
            return DriftReport(
                metric_name=metric_name,
                old_version=old_version,
                new_version=new_version,
                ks_statistic=0.0,
                p_value=1.0,
                mean_shift=0.0,
                mean_shift_sigma=0.0,
                tail_mass_change=0.0,
                drift_detected=False,
                recommendation="allow",
                timestamp=datetime.utcnow()
            )
        
        # KS test (fallback to simple comparison if scipy not available)
        try:
            from scipy import stats
            ks_stat, p_value = stats.ks_2samp(old_samples, new_samples)
        except ImportError:
            # Fallback: simple statistical comparison
            ks_stat = 0.0
            # Approximate p-value using mean/std comparison
            old_mean = np.mean(old_samples)
            new_mean = np.mean(new_samples)
            old_std = np.std(old_samples)
            z_score = abs(new_mean - old_mean) / (old_std + 1e-8)
            p_value = 2 * (1 - 0.5 * (1 + np.tanh(z_score / 2)))  # Rough approximation
        
        # Mean shift
        old_mean = np.mean(old_samples)
        new_mean = np.mean(new_samples)
        old_std = np.std(old_samples)
        mean_shift = new_mean - old_mean
        mean_shift_sigma = mean_shift / (old_std + 1e-8)
        
        # Tail mass change (95th percentile)
        old_tail = np.percentile(old_samples, 95)
        new_tail = np.percentile(new_samples, 95)
        tail_mass_change = abs(new_tail - old_tail) / (old_tail + 1e-8)
        
        # Drift detection criteria
        drift_detected = (
            p_value < 0.01 or  # KS test significant
            abs(mean_shift_sigma) > 2.0 or  # Mean shift > 2σ
            tail_mass_change > 0.15  # Tail mass change > 15%
        )
        
        # Recommendation
        if drift_detected:
            if p_value < 0.001 or abs(mean_shift_sigma) > 3.0:
                recommendation = "block"
            else:
                recommendation = "shadow"
        else:
            recommendation = "allow"
        
        return DriftReport(
            metric_name=metric_name,
            old_version=old_version,
            new_version=new_version,
            ks_statistic=float(ks_stat),
            p_value=float(p_value),
            mean_shift=float(mean_shift),
            mean_shift_sigma=float(mean_shift_sigma),
            tail_mass_change=float(tail_mass_change),
            drift_detected=drift_detected,
            recommendation=recommendation,
            timestamp=datetime.utcnow()
        )


# ============================================================================
# METRIC REGISTRY (ENFORCEMENT LAYER)
# ============================================================================

class MetricRegistry:
    """
    CENTRAL AUTHORITY: All metrics must be registered here.
    
    Only registered metrics may be:
      - logged
      - visualized
      - optimized
      - rewarded
      - alerted on
    
    Unregistered metrics trigger system halt.
    """
    
    def __init__(self):
        self._metrics: Dict[str, MetricSpec] = {}
        self._metric_history: Dict[str, List[MetricVersion]] = defaultdict(list)
        self._deprecated_metrics: Set[str] = set()
        self._locked: bool = False
        self._lock_hash: Optional[str] = None
        
        # Gaming enforcement state
        self._quarantined_metrics: Set[str] = set()
        self._penalized_metrics: Dict[str, float] = {}  # metric_name -> penalty_factor
        self._gaming_audit_log: List[Dict[str, Any]] = []
        
        # Cross-metric invariants
        self._invariants: List['MetricInvariant'] = []
        
        # Drift monitor (binding - automatic blocking)
        self._drift_monitor: Optional['MetricDriftMonitor'] = None
        self._drift_blocked_metrics: Set[str] = set()  # Metrics blocked due to drift
        self._drift_shadow_metrics: Set[str] = set()  # Metrics in shadow mode
        self._drift_reports: Dict[Tuple[str, MetricVersion, MetricVersion], DriftReport] = {}
    
    def register(self, spec: MetricSpec):
        """Register a metric specification with drift detection."""
        if self._locked:
            raise MetricRegistryLocked("Cannot register metrics after lock")
        
        # Check for name collision
        if spec.name in self._metrics:
            existing = self._metrics[spec.name]
            if existing.version == spec.version:
                raise ValueError(f"Metric '{spec.name}' v{spec.version} already registered")
            
            # Version upgrade - CHECK DRIFT (BINDING)
            if not spec.version.is_compatible_with(existing.version):
                raise ValueError(
                    f"Incompatible version upgrade: {existing.version} -> {spec.version}"
                )
            
            # Binding drift detection on version upgrade
            if self._drift_monitor:
                drift_report = self._drift_monitor.compare_distributions(
                    spec.name, existing.version, spec.version
                )
                self._drift_reports[(spec.name, existing.version, spec.version)] = drift_report
                
                # BINDING: Automatic blocking based on drift
                if drift_report.recommendation == "block":
                    self._drift_blocked_metrics.add(spec.name)
                    raise MetricDriftBlockedError(
                        f"Metric '{spec.name}' version upgrade blocked due to drift. "
                        f"KS p={drift_report.p_value:.4f}, mean_shift={drift_report.mean_shift_sigma:.2f}σ. "
                        f"Shadow-mode validation required."
                    )
                elif drift_report.recommendation == "shadow":
                    self._drift_shadow_metrics.add(spec.name)
        
        # Register
        self._metrics[spec.name] = spec
        self._metric_history[spec.name].append(spec.version)
        
        if spec.deprecated:
            self._deprecated_metrics.add(spec.name)
    
    def lock(self):
        """Lock registry (production mode) with cryptographic hash and drift state."""
        if self._locked:
            return
        
        # Serialize all metric specs for hashing (includes drift state)
        lock_data = {
            'metrics': {name: spec.to_dict() for name, spec in sorted(self._metrics.items())},
            'drift_blocked': sorted(self._drift_blocked_metrics),
            'drift_shadow': sorted(self._drift_shadow_metrics),
            'drift_reports_count': len(self._drift_reports)
        }
        specs_serialized = json.dumps(lock_data, sort_keys=True)
        
        # Compute hash (includes drift state)
        self._lock_hash = hashlib.sha256(specs_serialized.encode('utf-8')).hexdigest()
        self._locked = True
    
    def verify_lock_integrity(self) -> bool:
        """
        Verify registry hasn't been tampered with.
        
        REDUNDANT ENFORCEMENT:
        - Hash verification (primary)
        - State consistency checks (redundant)
        - Metric count validation (redundant)
        """
        if not self._locked or self._lock_hash is None:
            return True  # Not locked yet
        
        # REDUNDANT CHECK 1: Hash verification (primary check)
        lock_data = {
            'metrics': {name: spec.to_dict() for name, spec in sorted(self._metrics.items())},
            'drift_blocked': sorted(self._drift_blocked_metrics),
            'drift_shadow': sorted(self._drift_shadow_metrics),
            'drift_reports_count': len(self._drift_reports)
        }
        specs_serialized = json.dumps(lock_data, sort_keys=True)
        current_hash = hashlib.sha256(specs_serialized.encode('utf-8')).hexdigest()
        
        if current_hash != self._lock_hash:
            raise MetricRegistryLocked(
                f"Registry integrity violation: hash mismatch. "
                f"Expected {self._lock_hash[:16]}..., got {current_hash[:16]}..."
            )
        
        # REDUNDANT CHECK 2: State consistency validation
        # Verify quarantined metrics exist in registry
        for q_metric in self._quarantined_metrics:
            if q_metric not in self._metrics:
                raise MetricRegistryLocked(
                    f"Registry state inconsistency: quarantined metric '{q_metric}' not in registry"
                )
        
        # REDUNDANT CHECK 3: Verify drift-blocked metrics exist
        for d_metric in self._drift_blocked_metrics:
            if d_metric not in self._metrics:
                raise MetricRegistryLocked(
                    f"Registry state inconsistency: drift-blocked metric '{d_metric}' not in registry"
                )
        
        # REDUNDANT CHECK 4: Verify deprecated metrics exist
        for d_metric in self._deprecated_metrics:
            if d_metric not in self._metrics:
                raise MetricRegistryLocked(
                    f"Registry state inconsistency: deprecated metric '{d_metric}' not in registry"
                )
        
        return True
    
    def get(self, name: str) -> MetricSpec:
        """Retrieve metric spec with full validation."""
        # Verify lock integrity
        if self._locked:
            self.verify_lock_integrity()
        
        if name not in self._metrics:
            raise UnregisteredMetricError(
                f"Metric '{name}' not registered. SYSTEM HALT. "
                f"All metrics must be registered in metrics.py"
            )
        
        spec = self._metrics[name]
        
        # Check drift blocking (BINDING - automatic blocking)
        if name in self._drift_blocked_metrics:
            raise MetricDriftBlockedError(
                f"Metric '{name}' is blocked due to drift detection. "
                f"Version change requires shadow-mode validation first."
            )
        
        # Check quarantine
        if name in self._quarantined_metrics:
            raise MetricQuarantinedError(
                f"Metric '{name}' is quarantined due to gaming detection. "
                f"Cannot compute, log, or reward."
            )
        
        if spec.deprecated:
            raise DeprecatedMetricError(
                f"Metric '{name}' is deprecated. Use '{spec.replacement_metric}' instead."
            )
        
        return spec
    
    def compute(self, name: str, data: Dict[str, Any], video_age_seconds: int, computation_timestamp: Optional[datetime] = None) -> MetricResult:
        """
        Compute a registered metric with gaming enforcement.
        
        REDUNDANT ENFORCEMENT LAYERS:
        1. Registry integrity check (in get())
        2. Drift blocking check (in get())
        3. Quarantine check (in get())
        4. Deprecation check (in get())
        5. Time window validation (in spec.compute())
        6. Data structure validation (in spec.compute())
        7. Value validation (in spec.compute())
        8. Gaming risk enforcement (here)
        9. Causal dependent check (in spec.compute())
        """
        # REDUNDANT CHECK 1: Pre-computation validation
        if not isinstance(name, str) or not name:
            raise ValueError(f"Metric name must be non-empty string, got {type(name)}")
        
        if not isinstance(data, dict):
            raise ValueError(f"Data must be dict, got {type(data)}")
        
        if not isinstance(video_age_seconds, (int, float)) or video_age_seconds < 0:
            raise ValueError(f"video_age_seconds must be non-negative number, got {video_age_seconds}")
        
        # Get spec (this performs multiple validation checks)
        spec = self.get(name)  # This will raise if quarantined, deprecated, or drift-blocked
        
        # REDUNDANT CHECK 2: Additional spec validation
        if spec.deprecated:
            raise DeprecatedMetricError(f"Metric '{name}' is deprecated")
        
        if name in self._quarantined_metrics:
            raise MetricQuarantinedError(f"Metric '{name}' is quarantined")
        
        if name in self._drift_blocked_metrics:
            raise MetricDriftBlockedError(f"Metric '{name}' is drift-blocked")
        
        # Compute metric (performs additional validation internally)
        result = spec.compute(data, video_age_seconds, computation_timestamp)
        
        # REDUNDANT CHECK 3: Post-computation validation
        if result is None:
            return MetricResult.failed(name, spec.version, "computation_returned_none", computation_timestamp)
        
        if not isinstance(result, MetricResult):
            return MetricResult.failed(name, spec.version, f"computation_returned_invalid_type_{type(result)}", computation_timestamp)
        
        # Apply gaming enforcement (REDUNDANT ENFORCEMENT PATH)
        if result.available and result.gaming_risk is not None:
            # Validate gaming risk value
            if not isinstance(result.gaming_risk, (int, float)):
                result.gaming_risk = 0.0
            elif result.gaming_risk < 0.0 or result.gaming_risk > 1.0:
                result.gaming_risk = max(0.0, min(1.0, float(result.gaming_risk)))
            
            action = spec.anti_gaming.get_action(result.gaming_risk)
            
            if action == GamingAction.DEPRECATE:
                self._auto_deprecate(name, f"gaming_risk={result.gaming_risk:.3f}")
                result = MetricResult.failed(name, spec.version, "auto_deprecated", computation_timestamp)
            elif action == GamingAction.QUARANTINE:
                self._quarantine(name, result.gaming_risk, computation_timestamp)
                result = MetricResult.failed(name, spec.version, "quarantined", computation_timestamp)
            elif action == GamingAction.PENALIZE:
                penalty_factor = 1.0 - (result.gaming_risk * 0.5)  # Reduce by up to 50%
                penalty_factor = max(0.1, min(1.0, penalty_factor))  # Clamp to [0.1, 1.0]
                self._penalize(name, penalty_factor, computation_timestamp)
                if result.value is not None:
                    result.value = result.value * penalty_factor
            elif action == GamingAction.WARN:
                self._log_gaming_warning(name, result.gaming_risk, computation_timestamp)
        
        # REDUNDANT CHECK 4: Final result validation
        if result.available and result.value is not None:
            # Re-validate value (defense in depth)
            try:
                result.value = validate_metric_value(result.value, name)
            except ValueError as e:
                return MetricResult.failed(name, spec.version, f"post_compute_validation_failed: {str(e)}", computation_timestamp)
        
        return result
    
    def _quarantine(self, metric_name: str, risk_score: float, timestamp: datetime):
        """Quarantine a metric."""
        if metric_name not in self._quarantined_metrics:
            self._quarantined_metrics.add(metric_name)
            self._gaming_audit_log.append({
                "metric": metric_name,
                "event": "quarantine",
                "reason": f"gaming_risk={risk_score:.3f}",
                "timestamp": timestamp.isoformat(),
                "action": "QUARANTINE"
            })
    
    def _penalize(self, metric_name: str, penalty_factor: float, timestamp: datetime):
        """Penalize a metric (downweight its value)."""
        self._penalized_metrics[metric_name] = penalty_factor
        self._gaming_audit_log.append({
            "metric": metric_name,
            "event": "penalize",
            "penalty_factor": penalty_factor,
            "timestamp": timestamp.isoformat(),
            "action": "PENALIZE"
        })
    
    def _auto_deprecate(self, metric_name: str, reason: str):
        """Auto-deprecate a metric."""
        if metric_name not in self._deprecated_metrics:
            self._deprecated_metrics.add(metric_name)
            self._metrics[metric_name].deprecated = True
            self._gaming_audit_log.append({
                "metric": metric_name,
                "event": "auto_deprecate",
                "reason": reason,
                "timestamp": datetime.utcnow().isoformat(),
                "action": "DEPRECATE"
            })
    
    def _log_gaming_warning(self, metric_name: str, risk_score: float, timestamp: datetime):
        """Log gaming warning."""
        self._gaming_audit_log.append({
            "metric": metric_name,
            "event": "warning",
            "gaming_risk": risk_score,
            "timestamp": timestamp.isoformat(),
            "action": "WARN"
        })
    
    def compute_batch(
        self, 
        metric_names: List[str], 
        data: Dict[str, Any], 
        video_age_seconds: int,
        computation_timestamp: Optional[datetime] = None
    ) -> Dict[str, MetricResult]:
        """
        Compute multiple metrics with invariant checking.
        
        REDUNDANT ENFORCEMENT:
        - Individual metric validation (in compute())
        - Batch-level validation (here)
        - Invariant checking (here)
        """
        # REDUNDANT VALIDATION: Input validation
        if not isinstance(metric_names, list):
            raise ValueError(f"metric_names must be list, got {type(metric_names)}")
        
        if not isinstance(data, dict):
            raise ValueError(f"data must be dict, got {type(data)}")
        
        if not isinstance(video_age_seconds, (int, float)) or video_age_seconds < 0:
            raise ValueError(f"video_age_seconds must be non-negative number, got {video_age_seconds}")
        
        # REDUNDANT VALIDATION: Duplicate metric names
        if len(metric_names) != len(set(metric_names)):
            duplicates = [name for name in set(metric_names) if metric_names.count(name) > 1]
            raise ValueError(f"Duplicate metric names in batch: {duplicates}")
        
        # Compute all metrics
        results = {}
        for name in metric_names:
            # Each compute() performs full validation
            results[name] = self.compute(name, data, video_age_seconds, computation_timestamp)
        
        # REDUNDANT VALIDATION: Results validation
        if len(results) != len(metric_names):
            raise ValueError(f"Result count mismatch: expected {len(metric_names)}, got {len(results)}")
        
        # Check cross-metric invariants (REDUNDANT ENFORCEMENT PATH)
        metric_values = {
            name: result.value 
            for name, result in results.items() 
            if result.available and result.value is not None
        }
        
        if metric_values:
            self._check_invariants(metric_values, computation_timestamp)
        
        # REDUNDANT VALIDATION: Post-invariant result validation
        for name, result in results.items():
            if result.available and result.value is not None:
                # Re-validate each value (defense in depth)
                try:
                    validate_metric_value(result.value, name)
                except ValueError as e:
                    results[name] = MetricResult.failed(name, result.version, f"post_invariant_validation_failed: {str(e)}", computation_timestamp)
        
        return results
    
    def register_invariant(self, invariant: MetricInvariant):
        """Register a cross-metric invariant."""
        if self._locked:
            raise MetricRegistryLocked("Cannot register invariants after lock")
        self._invariants.append(invariant)
    
    def _get_invariant_response_policy(self, severity: InvariantSeverity) -> Dict[str, Any]:
        """
        Policy-driven response table for invariant severity escalation.
        
        Returns policy dict with actions, thresholds, and escalation rules.
        """
        policies = {
            InvariantSeverity.HALT: {
                "action": "raise_exception",
                "quarantine_all_metrics": True,
                "log_level": "ERROR",
                "escalate_to": "SYSTEM_HALT",
                "retry_allowed": False
            },
            InvariantSeverity.FAIL: {
                "action": "quarantine_metrics",
                "quarantine_all_metrics": False,
                "log_level": "ERROR",
                "escalate_to": "QUARANTINE",
                "retry_allowed": False,
                "auto_quarantine_patterns": ["reward", "engagement", "velocity"]
            },
            InvariantSeverity.WARN: {
                "action": "log_warning",
                "quarantine_all_metrics": False,
                "log_level": "WARNING",
                "escalate_to": None,
                "retry_allowed": True
            }
        }
        return policies.get(severity, policies[InvariantSeverity.WARN])
    
    def _check_invariants(self, metric_values: Dict[str, float], timestamp: Optional[datetime]):
        """Check all registered invariants with policy-driven responses."""
        for invariant in self._invariants:
            is_valid, error_msg = invariant.check(metric_values)
            
            if not is_valid and error_msg:
                # Get policy-driven response
                policy = self._get_invariant_response_policy(invariant.severity)
                
                if policy["action"] == "raise_exception":
                    # HALT severity - immediate exception
                    if policy["quarantine_all_metrics"]:
                        for metric in invariant.metrics:
                            if metric in metric_values:
                                self._quarantine(metric, 1.0, timestamp or datetime.utcnow())
                    raise MetricInvariantViolation(
                        f"HALT: {error_msg}. "
                        f"Invariant '{invariant.name}' requires immediate attention. "
                        f"Policy: {policy['escalate_to']}"
                    )
                elif policy["action"] == "quarantine_metrics":
                    # FAIL severity - quarantine metrics matching patterns
                    self._gaming_audit_log.append({
                        "event": "invariant_violation",
                        "invariant": invariant.name,
                        "error": error_msg,
                        "severity": invariant.severity.value,
                        "policy_action": policy["action"],
                        "timestamp": (timestamp or datetime.utcnow()).isoformat()
                    })
                    
                    # Auto-quarantine based on policy patterns
                    auto_quarantine_patterns = policy.get("auto_quarantine_patterns", [])
                    for metric in invariant.metrics:
                        if metric in metric_values:
                            metric_lower = metric.lower()
                            if any(pattern in metric_lower for pattern in auto_quarantine_patterns):
                                self._quarantine(metric, 1.0, timestamp or datetime.utcnow())
                elif policy["action"] == "log_warning":
                    # WARN severity - log only
                    self._gaming_audit_log.append({
                        "event": "invariant_warning",
                        "invariant": invariant.name,
                        "error": error_msg,
                        "severity": invariant.severity.value,
                        "policy_action": policy["action"],
                        "timestamp": (timestamp or datetime.utcnow()).isoformat()
                    })
    
    def list_metrics(self, category: Optional[MetricCategory] = None) -> List[str]:
        """List all registered metrics."""
        if category:
            return [n for n, s in self._metrics.items() if s.category == category]
        return list(self._metrics.keys())
    
    def get_rl_reward_metrics(self) -> List[str]:
        """
        Get metrics allowed for RL reward (excluding quarantined, drift-blocked, penalized).
        
        REDUNDANT ENFORCEMENT:
        - Quarantine check
        - Deprecation check
        - Drift blocking check
        - Shadow mode exclusion
        """
        # REDUNDANT VALIDATION: Registry state validation
        if self._locked:
            self.verify_lock_integrity()
        
        reward_metrics = []
        for n, s in self._metrics.items():
            # Multiple exclusion checks (redundant enforcement)
            if s.usage_policy != MetricUsagePolicy.RL_REWARD:
                continue
            
            if n in self._quarantined_metrics:
                continue  # Quarantined metrics excluded
            
            if s.deprecated:
                continue  # Deprecated metrics excluded
            
            if n in self._drift_blocked_metrics:
                continue  # Drift-blocked metrics excluded
            
            if n in self._drift_shadow_metrics:
                continue  # Shadow-mode metrics excluded from RL rewards
            
            reward_metrics.append(n)
        
        return reward_metrics
    
    def get_rl_constraint_metrics(self) -> List[str]:
        """Get metrics that constrain RL."""
        return [
            n for n, s in self._metrics.items() 
            if s.usage_policy == MetricUsagePolicy.RL_CONSTRAINT
        ]
    
    def audit_trail(self) -> Dict[str, Any]:
        """Generate audit trail for all metrics."""
        return {
            'total_metrics': len(self._metrics),
            'deprecated_count': len(self._deprecated_metrics),
            'quarantined_count': len(self._quarantined_metrics),
            'penalized_count': len(self._penalized_metrics),
            'invariants_count': len(self._invariants),
            'drift_blocked_count': len(self._drift_blocked_metrics),
            'drift_shadow_count': len(self._drift_shadow_metrics),
            'categories': {
                cat.value: len(self.list_metrics(cat)) 
                for cat in MetricCategory
            },
            'metrics': {
                name: spec.to_dict() 
                for name, spec in self._metrics.items()
            },
            'quarantined_metrics': list(self._quarantined_metrics),
            'penalized_metrics': dict(self._penalized_metrics),
            'drift_blocked_metrics': list(self._drift_blocked_metrics),
            'drift_shadow_metrics': list(self._drift_shadow_metrics),
            'gaming_audit_log': self._gaming_audit_log[-100:],  # Last 100 entries
            'locked': self._locked,
            'lock_hash': self._lock_hash[:16] + "..." if self._lock_hash else None
        }
    
    def get_gaming_audit_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get gaming audit log entries."""
        return self._gaming_audit_log[-limit:]
    
    def initialize_drift_monitor(self):
        """Initialize drift monitor."""
        if self._drift_monitor is None:
            self._drift_monitor = MetricDriftMonitor()
    
    def check_drift_and_block(self, metric_name: str, old_version: MetricVersion, new_version: MetricVersion) -> DriftReport:
        """
        Check drift and automatically block if detected (BINDING).
        
        This is the authoritative drift check that blocks promotion.
        """
        if not self._drift_monitor:
            self.initialize_drift_monitor()
        
        drift_report = self._drift_monitor.compare_distributions(metric_name, old_version, new_version)
        self._drift_reports[(metric_name, old_version, new_version)] = drift_report
        
        # BINDING: Automatic blocking based on drift
        if drift_report.recommendation == "block":
            self._drift_blocked_metrics.add(metric_name)
            self._gaming_audit_log.append({
                "event": "drift_block",
                "metric": metric_name,
                "old_version": str(old_version),
                "new_version": str(new_version),
                "p_value": drift_report.p_value,
                "mean_shift_sigma": drift_report.mean_shift_sigma,
                "timestamp": drift_report.timestamp.isoformat(),
                "action": "BLOCK"
            })
        elif drift_report.recommendation == "shadow":
            self._drift_shadow_metrics.add(metric_name)
            self._gaming_audit_log.append({
                "event": "drift_shadow",
                "metric": metric_name,
                "old_version": str(old_version),
                "new_version": str(new_version),
                "timestamp": drift_report.timestamp.isoformat(),
                "action": "SHADOW"
            })
        
        return drift_report


# ============================================================================
# EXCEPTIONS
# ============================================================================

class MetricError(Exception):
    """Base exception for metric system."""
    pass

class MetricTimeSafetyViolation(MetricError):
    """Raised when time window constraints violated."""
    pass

class UnregisteredMetricError(MetricError):
    """Raised when attempting to use unregistered metric."""
    pass

class DeprecatedMetricError(MetricError):
    """Raised when attempting to use deprecated metric."""
    pass

class MetricRegistryLocked(MetricError):
    """Raised when attempting to modify locked registry."""
    pass

class MetricQuarantinedError(MetricError):
    """Raised when attempting to use quarantined metric."""
    pass

class MetricInvariantViolation(MetricError):
    """Raised when cross-metric invariant is violated."""
    pass

class MetricDriftBlockedError(MetricError):
    """Raised when metric is blocked due to drift detection."""
    pass


# ============================================================================
# METRIC DRIFT DETECTION
# ============================================================================

@dataclass
class DriftReport:
    """Report of metric drift between versions."""
    metric_name: str
    old_version: MetricVersion
    new_version: MetricVersion
    ks_statistic: float
    p_value: float
    mean_shift: float
    mean_shift_sigma: float
    tail_mass_change: float
    drift_detected: bool
    recommendation: str  # "allow" | "shadow" | "block"
    timestamp: datetime


class MetricDriftMonitor:
    """Monitors metric drift across version changes."""
    
    def __init__(self):
        self._version_samples: Dict[Tuple[str, MetricVersion], List[float]] = defaultdict(list)
    
    def record_sample(self, metric_name: str, version: MetricVersion, value: float):
        """Record a metric value sample for drift detection."""
        key = (metric_name, version)
        self._version_samples[key].append(value)
        # Keep only last 10,000 samples per version
        if len(self._version_samples[key]) > 10000:
            self._version_samples[key] = self._version_samples[key][-10000:]
    
    def compare_distributions(
        self,
        metric_name: str,
        old_version: MetricVersion,
        new_version: MetricVersion,
        min_samples: int = 100
    ) -> DriftReport:
        """
        Compare distributions between versions using KS-test.
        
        Returns:
            DriftReport with drift analysis
        """
        old_key = (metric_name, old_version)
        new_key = (metric_name, new_version)
        
        old_samples = self._version_samples.get(old_key, [])
        new_samples = self._version_samples.get(new_key, [])
        
        if len(old_samples) < min_samples or len(new_samples) < min_samples:
            return DriftReport(
                metric_name=metric_name,
                old_version=old_version,
                new_version=new_version,
                ks_statistic=0.0,
                p_value=1.0,
                mean_shift=0.0,
                mean_shift_sigma=0.0,
                tail_mass_change=0.0,
                drift_detected=False,
                recommendation="allow",
                timestamp=datetime.utcnow()
            )
        
        # KS test (fallback to simple comparison if scipy not available)
        try:
            from scipy import stats
            ks_stat, p_value = stats.ks_2samp(old_samples, new_samples)
        except ImportError:
            # Fallback: simple statistical comparison
            ks_stat = 0.0
            # Approximate p-value using mean/std comparison
            old_mean = np.mean(old_samples)
            new_mean = np.mean(new_samples)
            old_std = np.std(old_samples)
            z_score = abs(new_mean - old_mean) / (old_std + 1e-8)
            p_value = 2 * (1 - 0.5 * (1 + np.tanh(z_score / 2)))  # Rough approximation
        
        # Mean shift
        old_mean = np.mean(old_samples)
        new_mean = np.mean(new_samples)
        old_std = np.std(old_samples)
        mean_shift = new_mean - old_mean
        mean_shift_sigma = mean_shift / (old_std + 1e-8)
        
        # Tail mass change (95th percentile)
        old_tail = np.percentile(old_samples, 95)
        new_tail = np.percentile(new_samples, 95)
        tail_mass_change = abs(new_tail - old_tail) / (old_tail + 1e-8)
        
        # Drift detection criteria
        drift_detected = (
            p_value < 0.01 or  # KS test significant
            abs(mean_shift_sigma) > 2.0 or  # Mean shift > 2σ
            tail_mass_change > 0.15  # Tail mass change > 15%
        )
        
        # Recommendation
        if drift_detected:
            if p_value < 0.001 or abs(mean_shift_sigma) > 3.0:
                recommendation = "block"
            else:
                recommendation = "shadow"
        else:
            recommendation = "allow"
        
        return DriftReport(
            metric_name=metric_name,
            old_version=old_version,
            new_version=new_version,
            ks_statistic=float(ks_stat),
            p_value=float(p_value),
            mean_shift=float(mean_shift),
            mean_shift_sigma=float(mean_shift_sigma),
            tail_mass_change=float(tail_mass_change),
            drift_detected=drift_detected,
            recommendation=recommendation,
            timestamp=datetime.utcnow()
        )


# ============================================================================
# METRIC COMPUTATION HELPERS
# ============================================================================

def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division for metric computation."""
    return numerator / denominator if denominator != 0 else default


def compute_percentile(values: List[float], percentile: int) -> float:
    """Compute percentile robustly."""
    if not values:
        return 0.0
    return float(np.percentile(values, percentile))


def compute_slope(x: List[float], y: List[float]) -> float:
    """Compute linear slope."""
    if len(x) < 2 or len(y) < 2:
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def compute_correlation(x: List[float], y: List[float]) -> float:
    """Compute Pearson correlation coefficient."""
    if len(x) < 2 or len(y) < 2 or len(x) != len(y):
        return 0.0
    x_arr = np.array(x)
    y_arr = np.array(y)
    if np.std(x_arr) == 0 or np.std(y_arr) == 0:
        return 0.0
    corr = np.corrcoef(x_arr, y_arr)[0, 1]
    return float(corr) if not np.isnan(corr) else 0.0


# ============================================================================
# VALIDATORS & ROBUSTNESS CHECKS
# ============================================================================

def validate_metric_value(value: float, metric_name: str) -> float:
    """
    Validate metric value for NaN, Inf, and bounds.
    
    Returns:
        Validated float value
        
    Raises:
        ValueError: If value is invalid
    """
    if not isinstance(value, (int, float, np.number)):
        raise ValueError(f"Metric '{metric_name}' value must be numeric, got {type(value)}")
    
    if np.isnan(value):
        raise ValueError(f"Metric '{metric_name}' computed NaN value")
    
    if np.isinf(value):
        raise ValueError(f"Metric '{metric_name}' computed infinite value")
    
    return float(value)


def validate_data_structure(data: Dict[str, Any], metric_name: str, required_fields: List[str]) -> None:
    """
    Validate that data dictionary contains required fields.
    
    Raises:
        ValueError: If required fields are missing
    """
    missing = [field for field in required_fields if field not in data]
    if missing:
        raise ValueError(
            f"Metric '{metric_name}' requires fields: {missing}. "
            f"Available fields: {list(data.keys())}"
        )


def validate_time_series_data(x: List[float], y: List[float], metric_name: str) -> None:
    """
    Validate time series data for metric computation.
    
    Raises:
        ValueError: If time series data is invalid
    """
    if not isinstance(x, list) or not isinstance(y, list):
        raise ValueError(f"Metric '{metric_name}' time series must be lists")
    
    if len(x) != len(y):
        raise ValueError(
            f"Metric '{metric_name}' time series length mismatch: "
            f"x={len(x)}, y={len(y)}"
        )
    
    if len(x) < 2:
        raise ValueError(f"Metric '{metric_name}' requires at least 2 time series points")
    
    # Check for non-finite values
    for i, (xi, yi) in enumerate(zip(x, y)):
        if not (isinstance(xi, (int, float)) and isinstance(yi, (int, float))):
            raise ValueError(f"Metric '{metric_name}' time series contains non-numeric at index {i}")
        if np.isnan(xi) or np.isnan(yi) or np.isinf(xi) or np.isinf(yi):
            raise ValueError(f"Metric '{metric_name}' time series contains NaN/Inf at index {i}")


def validate_retention_data(retention_curve: List[float], metric_name: str) -> None:
    """
    Validate retention curve data.
    
    Raises:
        ValueError: If retention data is invalid
    """
    if not isinstance(retention_curve, list):
        raise ValueError(f"Metric '{metric_name}' retention_curve must be a list")
    
    if len(retention_curve) == 0:
        raise ValueError(f"Metric '{metric_name}' retention_curve cannot be empty")
    
    # Check for valid percentages (0-100 typically)
    for i, val in enumerate(retention_curve):
        if not isinstance(val, (int, float, np.number)):
            raise ValueError(f"Metric '{metric_name}' retention_curve[{i}] must be numeric")
        if np.isnan(val) or np.isinf(val):
            raise ValueError(f"Metric '{metric_name}' retention_curve[{i}] is NaN/Inf")


def validate_distribution_data(distribution: Dict[str, Any], metric_name: str) -> None:
    """
    Validate distribution data structure.
    
    Raises:
        ValueError: If distribution data is invalid
    """
    if not isinstance(distribution, dict):
        raise ValueError(f"Metric '{metric_name}' distribution must be a dictionary")
    
    # Check for required distribution fields (platform-dependent)
    if len(distribution) == 0:
        raise ValueError(f"Metric '{metric_name}' distribution cannot be empty")


# ============================================================================
# CANONICAL METRIC DEFINITIONS
# ============================================================================

def create_standard_metrics() -> MetricRegistry:
    """
    Factory function to create the canonical metric registry.
    
    THIS IS THE SOURCE OF TRUTH.
    All metrics the system knows about are defined here.
    """
    registry = MetricRegistry()
    
    # ------------------------------------------------------------------------
    # ENGAGEMENT METRICS (Observed Only)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="views_count",
        category=MetricCategory.ENGAGEMENT,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="raw_reach",
            forbidden_correlations=["bot_traffic"],
            manipulation_sensitivity=0.8
        ),
        description="Total view count",
        unit="views",
        compute_fn=lambda d: float(d.get('views', 0))
    ))
    
    registry.register(MetricSpec(
        name="likes_count",
        category=MetricCategory.ENGAGEMENT,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="approval",
            forbidden_correlations=["fake_engagement"],
            manipulation_sensitivity=0.9
        ),
        description="Total like count",
        unit="likes",
        compute_fn=lambda d: float(d.get('likes', 0))
    ))
    
    registry.register(MetricSpec(
        name="comments_count",
        category=MetricCategory.ENGAGEMENT,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="conversation",
            forbidden_correlations=["spam_comments"],
            manipulation_sensitivity=0.7
        ),
        description="Total comment count",
        unit="comments",
        compute_fn=lambda d: float(d.get('comments', 0))
    ))
    
    registry.register(MetricSpec(
        name="shares_count",
        category=MetricCategory.ENGAGEMENT,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="organic_spread",
            forbidden_correlations=["share_incentives"],
            manipulation_sensitivity=0.6
        ),
        description="Total share count",
        unit="shares",
        compute_fn=lambda d: float(d.get('shares', 0))
    ))
    
    registry.register(MetricSpec(
        name="saves_count",
        category=MetricCategory.ENGAGEMENT,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="content_value",
            forbidden_correlations=["save_incentives"],
            manipulation_sensitivity=0.7
        ),
        description="Total save/bookmark count",
        unit="saves",
        compute_fn=lambda d: float(d.get('saves', 0))
    ))
    
    registry.register(MetricSpec(
        name="rewatches_count",
        category=MetricCategory.ENGAGEMENT,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="replay_value",
            forbidden_correlations=["replay_loops"],
            manipulation_sensitivity=0.5
        ),
        description="Total rewatch/replay count",
        unit="rewatches",
        compute_fn=lambda d: float(d.get('rewatches', 0))
    ))
    
    # ------------------------------------------------------------------------
    # VELOCITY METRICS (Early Power)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="views_per_minute",
        category=MetricCategory.VELOCITY,
        version=MetricVersion(3, 1, 0),
        time_window=TimeWindow(min_age_seconds=120, max_age_seconds=86400),
        usage_policy=MetricUsagePolicy.EARLY_SIGNAL,
        anti_gaming=AntiGamingSpec(
            intended_signal="momentum",
            forbidden_correlations=["artificial_boost"],
            saturation_limit=10000.0,
            manipulation_sensitivity=0.85
        ),
        description="View velocity (views per minute)",
        unit="views/min",
        compute_fn=lambda d: safe_divide(
            d.get('views', 0), 
            d.get('age_minutes', 1)
        )
    ))
    
    registry.register(MetricSpec(
        name="engagement_slope",
        category=MetricCategory.VELOCITY,
        version=MetricVersion(2, 0, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=86400),
        usage_policy=MetricUsagePolicy.EARLY_SIGNAL,
        anti_gaming=AntiGamingSpec(
            intended_signal="growth_acceleration",
            forbidden_correlations=["burst_traffic"],
            manipulation_sensitivity=0.75
        ),
        description="Engagement growth slope (linear fit)",
        unit="engagement/min²",
        compute_fn=lambda d: compute_slope(
            d.get('time_series_minutes', []),
            d.get('engagement_series', [])
        )
    ))
    
    registry.register(MetricSpec(
        name="velocity_acceleration",
        category=MetricCategory.VELOCITY,
        version=MetricVersion(1, 2, 0),
        time_window=TimeWindow(min_age_seconds=600, max_age_seconds=7200),
        usage_policy=MetricUsagePolicy.EARLY_SIGNAL,
        anti_gaming=AntiGamingSpec(
            intended_signal="momentum_change",
            forbidden_correlations=["bot_swarm"],
            manipulation_sensitivity=0.9
        ),
        description="Change in velocity (Δvelocity)",
        unit="views/min²",
        compute_fn=lambda d: d.get('velocity_delta', 0.0)
    ))
    
    registry.register(MetricSpec(
        name="early_saturation_rate",
        category=MetricCategory.VELOCITY,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=7200),
        usage_policy=MetricUsagePolicy.EARLY_SIGNAL,
        anti_gaming=AntiGamingSpec(
            intended_signal="early_peak_detection",
            forbidden_correlations=["artificial_peak"],
            saturation_limit=1.0,
            manipulation_sensitivity=0.8
        ),
        description="Rate at which early engagement saturates",
        unit="fraction",
        compute_fn=lambda d: safe_divide(
            d.get('early_peak_views', 0),
            d.get('current_views', 1)
        )
    ))
    
    # ------------------------------------------------------------------------
    # RETENTION METRICS (Structure Truth)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="retention_p50",
        category=MetricCategory.RETENTION,
        version=MetricVersion(2, 1, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=86400 * 30),
        usage_policy=MetricUsagePolicy.RL_REWARD,
        anti_gaming=AntiGamingSpec(
            intended_signal="content_quality",
            forbidden_correlations=["clickbait"],
            manipulation_sensitivity=0.4
        ),
        description="Median watch time percentage",
        unit="fraction",
        requires_retention_data=True,
        confidence_weighted=True,
        compute_fn=lambda d: compute_percentile(d.get('retention_curve', []), 50) / 100.0
    ))
    
    registry.register(MetricSpec(
        name="avg_watch_time",
        category=MetricCategory.RETENTION,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=86400 * 30),
        usage_policy=MetricUsagePolicy.RL_REWARD,
        anti_gaming=AntiGamingSpec(
            intended_signal="sustained_interest",
            forbidden_correlations=["padding"],
            manipulation_sensitivity=0.5
        ),
        description="Average watch time",
        unit="seconds",
        requires_retention_data=True,
        compute_fn=lambda d: float(np.mean(d.get('watch_times', [0])))
    ))
    
    registry.register(MetricSpec(
        name="rewind_density",
        category=MetricCategory.RETENTION,
        version=MetricVersion(1, 1, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="replay_value",
            forbidden_correlations=[],
            manipulation_sensitivity=0.3
        ),
        description="Density of rewind events",
        unit="rewinds/view",
        requires_retention_data=True,
        compute_fn=lambda d: safe_divide(
            d.get('rewind_count', 0),
            d.get('views', 1)
        )
    ))
    
    registry.register(MetricSpec(
        name="drop_off_points",
        category=MetricCategory.RETENTION,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=86400 * 30),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="content_structure",
            forbidden_correlations=["clickbait"],
            manipulation_sensitivity=0.6
        ),
        description="Key drop-off points in retention curve",
        unit="seconds",
        requires_retention_data=True,
        compute_fn=lambda d: float(np.mean(d.get('drop_off_points', [0]))) if d.get('drop_off_points') else 0.0
    ))
    
    # ------------------------------------------------------------------------
    # DISTRIBUTION METRICS (Exposure Shape)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="feed_dispersion_entropy",
        category=MetricCategory.DISTRIBUTION,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="organic_spread",
            forbidden_correlations=["concentrated_boost"],
            manipulation_sensitivity=0.7
        ),
        description="Entropy of feed source distribution",
        unit="bits",
        requires_distribution_data=True,
        compute_fn=lambda d: float(d.get('feed_entropy', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="audience_diversity_index",
        category=MetricCategory.DISTRIBUTION,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 30),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="broad_appeal",
            forbidden_correlations=["niche_bottleneck"],
            manipulation_sensitivity=0.5
        ),
        description="Diversity of audience demographics",
        unit="index",
        requires_distribution_data=True,
        compute_fn=lambda d: float(d.get('audience_diversity', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="geo_spread",
        category=MetricCategory.DISTRIBUTION,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 30),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="geographic_diversity",
            forbidden_correlations=["geo_bot_farming"],
            manipulation_sensitivity=0.7
        ),
        description="Geographic spread entropy of views",
        unit="bits",
        requires_distribution_data=True,
        compute_fn=lambda d: float(d.get('geo_entropy', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="account_source_concentration",
        category=MetricCategory.DISTRIBUTION,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.OBSERVATION_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="source_diversity",
            forbidden_correlations=["concentrated_boost"],
            saturation_limit=0.8,
            manipulation_sensitivity=0.9
        ),
        description="Concentration ratio of top source accounts",
        unit="fraction",
        requires_distribution_data=True,
        compute_fn=lambda d: float(d.get('source_concentration', 0.0))
    ))
    
    # ------------------------------------------------------------------------
    # STRUCTURAL STABILITY METRICS (Anti-Spike)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="engagement_variance",
        category=MetricCategory.STRUCTURAL_STABILITY,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="stability",
            forbidden_correlations=["volatility"],
            manipulation_sensitivity=0.6
        ),
        description="Variance in engagement over time",
        unit="variance",
        compute_fn=lambda d: float(np.var(d.get('engagement_series', [0])))
    ))
    
    registry.register(MetricSpec(
        name="volatility_index",
        category=MetricCategory.STRUCTURAL_STABILITY,
        version=MetricVersion(2, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 14),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="predictability",
            forbidden_correlations=["spike_chasing"],
            saturation_limit=5.0,
            manipulation_sensitivity=0.7
        ),
        description="Coefficient of variation in engagement",
        unit="cv",
        compute_fn=lambda d: safe_divide(
            float(np.std(d.get('engagement_series', [0]))),
            float(np.mean(d.get('engagement_series', [1])))
        )
    ))
    
    registry.register(MetricSpec(
        name="hook_decay_slope",
        category=MetricCategory.STRUCTURAL_STABILITY,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=3600),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="hook_effectiveness",
            forbidden_correlations=["clickbait_hook"],
            manipulation_sensitivity=0.8
        ),
        description="Slope of engagement decay after hook (first 60 seconds)",
        unit="engagement/second",
        requires_retention_data=True,
        compute_fn=lambda d: compute_slope(
            d.get('hook_time_series', []),
            d.get('hook_engagement_series', [])
        )
    ))
    
    registry.register(MetricSpec(
        name="pacing_stability",
        category=MetricCategory.STRUCTURAL_STABILITY,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="consistent_pacing",
            forbidden_correlations=["uneven_distribution"],
            manipulation_sensitivity=0.6
        ),
        description="Stability of engagement pacing over time",
        unit="cv",
        compute_fn=lambda d: safe_divide(
            float(np.std(d.get('pacing_series', [0]))),
            float(np.mean(d.get('pacing_series', [1])))
        )
    ))
    
    # ------------------------------------------------------------------------
    # LONG-TAIL METRICS (THE MONEY)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="tail_half_life",
        category=MetricCategory.LONG_TAIL,
        version=MetricVersion(2, 0, 0),
        time_window=TimeWindow(min_age_seconds=86400 * 7, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.STRATEGIC_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="longevity",
            forbidden_correlations=["artificial_resurgence"],
            manipulation_sensitivity=0.3
        ),
        description="Estimated half-life of tail engagement",
        unit="days",
        compute_fn=lambda d: float(d.get('half_life_days', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="evergreen_coefficient",
        category=MetricCategory.LONG_TAIL,
        version=MetricVersion(1, 1, 0),
        time_window=TimeWindow(min_age_seconds=86400 * 14, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.STRATEGIC_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="timeless_value",
            forbidden_correlations=["gaming_algorithm"],
            manipulation_sensitivity=0.2
        ),
        description="Ratio of tail to peak engagement",
        unit="ratio",
        compute_fn=lambda d: safe_divide(
            d.get('tail_engagement', 0),
            d.get('peak_engagement', 1)
        )
    ))
    
    registry.register(MetricSpec(
        name="residual_engagement_mass",
        category=MetricCategory.LONG_TAIL,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=86400 * 30, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.STRATEGIC_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="cumulative_tail_value",
            forbidden_correlations=[],
            manipulation_sensitivity=0.1
        ),
        description="Total engagement after day 30",
        unit="engagement",
        compute_fn=lambda d: float(d.get('tail_engagement_total', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="decay_probability",
        category=MetricCategory.LONG_TAIL,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=86400 * 7, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.STRATEGIC_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="longevity_potential",
            forbidden_correlations=[],
            manipulation_sensitivity=0.2
        ),
        description="Probability of continued engagement decay",
        unit="probability",
        compute_fn=lambda d: float(d.get('decay_probability', 0.0))
    ))
    
    # ------------------------------------------------------------------------
    # RISK METRICS (MANDATORY)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="metric_divergence_index",
        category=MetricCategory.RISK,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="metric_consistency",
            forbidden_correlations=[],
            saturation_limit=2.0,
            manipulation_sensitivity=1.0
        ),
        description="Divergence between correlated metrics",
        unit="std_dev",
        compute_fn=lambda d: float(d.get('metric_divergence', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="reward_engagement_decoupling",
        category=MetricCategory.RISK,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="alignment",
            forbidden_correlations=["reward_hacking"],
            saturation_limit=1.5,
            manipulation_sensitivity=1.0
        ),
        description="Decoupling between reward signal and real engagement",
        unit="correlation",
        compute_fn=lambda d: 1.0 - abs(d.get('reward_engagement_correlation', 1.0))
    ))
    
    registry.register(MetricSpec(
        name="anomaly_likelihood",
        category=MetricCategory.RISK,
        version=MetricVersion(1, 1, 0),
        time_window=TimeWindow(min_age_seconds=1800, max_age_seconds=86400 * 3),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="normalcy",
            forbidden_correlations=["bot_attack"],
            manipulation_sensitivity=0.9
        ),
        description="Likelihood of anomalous behavior",
        unit="probability",
        compute_fn=lambda d: float(d.get('anomaly_score', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="uncertainty_penalty",
        category=MetricCategory.RISK,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=300, max_age_seconds=86400 * 30),
        usage_policy=MetricUsagePolicy.RL_CONSTRAINT,
        anti_gaming=AntiGamingSpec(
            intended_signal="confidence",
            forbidden_correlations=[],
            manipulation_sensitivity=0.5
        ),
        description="Epistemic uncertainty in metric estimates",
        unit="bits",
        compute_fn=lambda d: float(d.get('uncertainty', 0.0))
    ))
    
    # ------------------------------------------------------------------------
    # DIAGNOSTIC METRICS (Debug Only)
    # ------------------------------------------------------------------------
    
    registry.register(MetricSpec(
        name="metric_latency_ms",
        category=MetricCategory.DIAGNOSTIC,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.DEBUG_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="performance",
            forbidden_correlations=[],
            manipulation_sensitivity=0.0
        ),
        description="Metric computation latency",
        unit="milliseconds",
        compute_fn=lambda d: float(d.get('computation_latency_ms', 0.0))
    ))
    
    registry.register(MetricSpec(
        name="missing_data_ratio",
        category=MetricCategory.DIAGNOSTIC,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=0, max_age_seconds=86400 * 365),
        usage_policy=MetricUsagePolicy.DEBUG_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="data_quality",
            forbidden_correlations=[],
            manipulation_sensitivity=0.0
        ),
        description="Ratio of missing data points",
        unit="fraction",
        compute_fn=lambda d: safe_divide(
            d.get('missing_fields', 0),
            d.get('total_fields', 1)
        )
    ))
    
    registry.register(MetricSpec(
        name="platform_inconsistency_index",
        category=MetricCategory.DIAGNOSTIC,
        version=MetricVersion(1, 0, 0),
        time_window=TimeWindow(min_age_seconds=3600, max_age_seconds=86400 * 7),
        usage_policy=MetricUsagePolicy.DEBUG_ONLY,
        anti_gaming=AntiGamingSpec(
            intended_signal="data_reliability",
            forbidden_correlations=[],
            manipulation_sensitivity=0.0
        ),
        description="Inconsistency between platform data sources",
        unit="index",
        compute_fn=lambda d: float(d.get('platform_variance', 0.0))
    ))
    
    return registry


# ============================================================================
# GLOBAL REGISTRY INSTANCE
# ============================================================================

# Production registry (locked after initialization)
METRIC_REGISTRY = create_standard_metrics()

# Initialize drift monitor
METRIC_REGISTRY.initialize_drift_monitor()

# Register critical cross-metric invariants
def _register_default_invariants(registry: MetricRegistry):
    """Register default cross-metric invariants."""
    from typing import Tuple
    
    # Invariant: Views should increase with engagement
    registry.register_invariant(MetricInvariant(
        name="views_engagement_alignment",
        metrics=("views_count", "likes_count", "comments_count"),
        condition=lambda vals: (
            vals.get("views_count", 0) > 0 and
            (vals.get("likes_count", 0) + vals.get("comments_count", 0)) / max(vals.get("views_count", 1), 1) < 1.0
        ),
        severity=InvariantSeverity.WARN,
        description="Engagement should not exceed views"
    ))
    
    # Invariant: Reward should align with engagement
    registry.register_invariant(MetricInvariant(
        name="reward_engagement_alignment",
        metrics=("views_per_minute", "engagement_slope"),
        condition=lambda vals: (
            vals.get("views_per_minute", 0) >= 0 and
            vals.get("engagement_slope", 0) >= -1000  # Allow negative but not extreme
        ),
        severity=InvariantSeverity.FAIL,
        description="Velocity metrics should be non-negative and reasonable"
    ))
    
    # Invariant: Retention should be bounded
    registry.register_invariant(MetricInvariant(
        name="retention_bounds",
        metrics=("retention_p50", "avg_watch_time"),
        condition=lambda vals: (
            0.0 <= vals.get("retention_p50", 0.5) <= 1.0 and
            vals.get("avg_watch_time", 0) >= 0
        ),
        severity=InvariantSeverity.HALT,
        description="Retention metrics must be in valid ranges"
    ))

_register_default_invariants(METRIC_REGISTRY)
METRIC_REGISTRY.lock()


# ============================================================================
# PUBLIC API
# ============================================================================

def get_metric_specs() -> Dict[str, MetricSpec]:
    """
    PUBLIC API: Get all registered metric specifications.
    
    Returns:
        Dict mapping metric names to MetricSpec objects
    
    This is the machine-readable schema for all metrics.
    """
    return {name: spec for name, spec in METRIC_REGISTRY._metrics.items()}


def parse_metric_name_with_version(metric_name: str) -> Tuple[str, Optional[MetricVersion]]:
    """
    Parse metric name with optional version suffix.
    
    Supports formats:
    - "metric_name" -> ("metric_name", None)  # Uses latest version
    - "metric_name@v1" -> ("metric_name", MetricVersion(1, 0, 0))
    - "metric_name@v1.2.3" -> ("metric_name", MetricVersion(1, 2, 3))
    
    Returns:
        Tuple of (base_name, version)
    """
    if '@' in metric_name:
        parts = metric_name.split('@', 1)
        base_name = parts[0]
        version_str = parts[1]
        try:
            version = MetricVersion.from_string(version_str)
            return base_name, version
        except (ValueError, IndexError):
            raise ValueError(f"Invalid version format in metric name '{metric_name}'. Expected '@v1' or '@v1.2.3'")
    return metric_name, None


def compute_metric(
    metric_name: str,
    inputs: Dict[str, Any],
    as_of_timestamp: datetime,
    video_age_seconds: Optional[int] = None
) -> MetricResult:
    """
    PUBLIC API: Compute a single metric.
    
    This file exposes metrics via this function. Nothing else is allowed.
    
    Args:
        metric_name: Name of registered metric (optionally with @v1 version suffix)
        inputs: Input data dictionary (renamed from 'data' to match spec)
        as_of_timestamp: Evaluation timestamp (no metric may include data > this timestamp)
        video_age_seconds: Optional age of video in seconds (computed from timestamp if not provided)
    
    Returns:
        MetricResult with value and metadata
    
    Raises:
        UnregisteredMetricError: If metric not registered
        MetricTimeSafetyViolation: If time window violated
        ValueError: If metric name format is invalid
    """
    # Parse version from metric name if present
    base_name, requested_version = parse_metric_name_with_version(metric_name)
    
    # Compute video_age_seconds from as_of_timestamp if not provided
    if video_age_seconds is None:
        if 'video_created_at' in inputs:
            created_at = inputs['video_created_at']
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            video_age_seconds = int((as_of_timestamp - created_at).total_seconds())
        elif 'timestamp' in inputs:
            # Fallback to timestamp field
            ts = inputs['timestamp']
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            video_age_seconds = int((as_of_timestamp - ts).total_seconds())
        else:
            raise ValueError(
                f"Cannot compute video_age_seconds. Provide 'video_created_at' in inputs "
                f"or pass video_age_seconds parameter."
            )
    
    # Use as_of_timestamp as computation_timestamp (deterministic)
    result = METRIC_REGISTRY.compute(base_name, inputs, video_age_seconds, as_of_timestamp)
    
    # Verify version if requested
    if requested_version is not None:
        if result.version != requested_version:
            raise ValueError(
                f"Metric '{base_name}' version mismatch. "
                f"Requested {requested_version}, got {result.version}"
            )
    
    return result


def compute_metrics(
    metric_names: List[str],
    data: Dict[str, Any],
    video_age_seconds: int,
    computation_timestamp: Optional[datetime] = None
) -> Dict[str, MetricResult]:
    """
    PUBLIC API: Compute multiple metrics.
    
    Args:
        metric_names: List of registered metric names
        data: Video data dictionary
        video_age_seconds: Age of video in seconds
        computation_timestamp: Optional timestamp for deterministic computation
    
    Returns:
        Dict mapping metric names to MetricResults
    """
    return METRIC_REGISTRY.compute_batch(metric_names, data, video_age_seconds, computation_timestamp)


def get_rl_reward_metrics() -> List[str]:
    """PUBLIC API: Get metrics allowed for RL optimization."""
    return METRIC_REGISTRY.get_rl_reward_metrics()


def get_rl_constraint_metrics() -> List[str]:
    """PUBLIC API: Get metrics that constrain RL behavior."""
    return METRIC_REGISTRY.get_rl_constraint_metrics()


def list_all_metrics(category: Optional[MetricCategory] = None) -> List[str]:
    """PUBLIC API: List all registered metrics."""
    return METRIC_REGISTRY.list_metrics(category)


def generate_audit_report() -> Dict[str, Any]:
    """PUBLIC API: Generate complete audit trail."""
    return METRIC_REGISTRY.audit_trail()


# ============================================================================
# VALIDATION HOOKS (Integration with validation_pipeline.py and data_gate.py)
# ============================================================================

def validate_metric_for_training(
    metric_name: str,
    inputs: Dict[str, Any],
    as_of_timestamp: datetime
) -> Tuple[bool, List[str]]:
    """
    Validation hook for training pipeline.
    
    Metrics may be rejected if:
    - undefined
    - non-deterministic
    - insufficient data
    - violated time assumptions
    
    Returns:
        Tuple of (is_valid, violations)
    """
    violations = []
    
    # Check if metric is registered
    try:
        base_name, version = parse_metric_name_with_version(metric_name)
        spec = METRIC_REGISTRY.get(base_name)
    except UnregisteredMetricError as e:
        violations.append(f"Metric not registered: {str(e)}")
        return False, violations
    
    # Check determinism
    if not spec.deterministic:
        violations.append(f"Metric '{metric_name}' is not deterministic")
    
    # Check time assumptions
    if 'video_created_at' in inputs:
        created_at = inputs['video_created_at']
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        video_age = (as_of_timestamp - created_at).total_seconds()
        
        if not spec.time_window.is_valid_age(int(video_age)):
            violations.append(
                f"Time window violated: video_age={video_age}s, "
                f"required=[{spec.time_window.min_age_seconds}, {spec.time_window.max_age_seconds}]"
            )
    
    # Check for future data leakage
    for key, value in inputs.items():
        if isinstance(value, datetime) and value > as_of_timestamp:
            violations.append(f"Future data detected in input '{key}': {value} > {as_of_timestamp}")
    
    # Check minimum data points for temporal delta metrics
    if spec.category == MetricCategory.TEMPORAL_DELTA and spec.min_points:
        time_series_key = None
        for key in ['time_series', 'views_history', 'engagement_history']:
            if key in inputs and isinstance(inputs[key], list):
                time_series_key = key
                break
        
        if time_series_key:
            if len(inputs[time_series_key]) < spec.min_points:
                violations.append(
                    f"Insufficient data points: got {len(inputs[time_series_key])}, "
                    f"required {spec.min_points} for temporal delta metric"
                )
        else:
            violations.append(f"Missing time series data for temporal delta metric '{metric_name}'")
    
    # Check required inputs
    if spec.inputs:
        missing_inputs = [inp for inp in spec.inputs if inp not in inputs]
        if missing_inputs:
            violations.append(f"Missing required inputs: {missing_inputs}")
    
    return len(violations) == 0, violations


def validate_metric_for_data_gate(
    metric_name: str,
    inputs: Dict[str, Any],
    as_of_timestamp: datetime
) -> Tuple[bool, Optional[str]]:
    """
    Validation hook for data_gate.py.
    
    Returns:
        Tuple of (is_allowed, reason_if_blocked)
    """
    try:
        is_valid, violations = validate_metric_for_training(metric_name, inputs, as_of_timestamp)
        if not is_valid:
            return False, "; ".join(violations)
        return True, None
    except Exception as e:
        return False, f"Validation error: {str(e)}"


# ============================================================================
# CANONICAL OUTPUT FORMAT
# ============================================================================

def format_metric_output(
    video_id: str,
    results: Dict[str, MetricResult],
    output_timestamp: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Format metrics into canonical output structure.
    
    This is the ONLY allowed output format for metric data.
    
    Args:
        video_id: Video identifier
        results: Dictionary of metric results
        output_timestamp: Optional timestamp for output (defaults to first result timestamp)
    """
    metrics_dict = {}
    versions_dict = {}
    
    for name, result in results.items():
        if result.available and result.value is not None:
            metrics_dict[name] = result.value
            versions_dict[name] = str(result.version)
    
    # Use provided timestamp or first result timestamp (deterministic)
    if output_timestamp is None and results:
        first_result = next(iter(results.values()))
        output_timestamp = first_result.timestamp
    elif output_timestamp is None:
        # Fallback: use epoch (deterministic)
        output_timestamp = datetime(1970, 1, 1)
    
    return {
        "video_id": video_id,
        "timestamp": output_timestamp.isoformat(),
        "metrics": metrics_dict,
        "metric_versions": versions_dict,
        "confidence": {
            name: result.confidence 
            for name, result in results.items() 
            if result.available
        },
        "gaming_risk": {
            name: result.gaming_risk 
            for name, result in results.items() 
            if result.available
        }
    }


# ============================================================================
# EXAMPLE USAGE (for documentation)
# ============================================================================

if __name__ == "__main__":
    # Example video data
    example_data = {
        'views': 1500,
        'likes': 120,
        'comments': 45,
        'shares': 23,
        'age_minutes': 120,
        'time_series_minutes': [0, 30, 60, 90, 120],
        'engagement_series': [0, 300, 800, 1200, 1500],
        'retention_curve': [100, 85, 70, 60, 55, 50],
        'watch_times': [45, 60, 55, 58, 62],
        'feed_entropy': 2.3,
        'metric_divergence': 0.15,
        'reward_engagement_correlation': 0.92
    }
    
    video_age_seconds = 7200  # 2 hours
    
    # Compute specific metrics
    velocity = compute_metric('views_per_minute', example_data, video_age_seconds)
    print(f"Velocity: {velocity.value} {METRIC_REGISTRY.get('views_per_minute').unit}")
    
    # Compute batch
    metrics_to_compute = [
        'views_per_minute',
        'engagement_slope',
        'retention_p50',
        'metric_divergence_index'
    ]
    
    results = compute_metrics(metrics_to_compute, example_data, video_age_seconds)
    
    # Format output
    output = format_metric_output('video_12345', results)
    print(json.dumps(output, indent=2))
    
    # Audit trail
    audit = generate_audit_report()
    print(f"\nTotal registered metrics: {audit['total_metrics']}")
    print(f"RL reward metrics: {get_rl_reward_metrics()}")
    print(f"RL constraint metrics: {get_rl_constraint_metrics()}")