"""
confidence_estimator.py — Uncertainty, Trust & Belief Modeling Engine

Core principle: Confidence is belief stability under uncertainty.

This module quantifies how much the system should trust measured effects.
It does NOT make decisions, modify effects, or approve rollouts.

Outputs: confidence scores [0, 1] + explanatory factors
Inputs: effect sizes, statistical tests, outcome metadata

HARD INVARIANTS:
- Never decide rollout
- Never interpret business impact
- Never change effect sizes
- Never change p-values
- Never override statistical tests
- Never auto-approve variants

This file only quantifies belief.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
import math
import statistics
import logging
from collections import defaultdict
import json

logger = logging.getLogger(__name__)


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class ConfidenceSpec:
    """
    Defines how confidence is computed for a specific experiment measurement.
    
    No defaults. No guessing.
    """
    experiment_id: str
    metric_name: str
    window: str
    segment: Optional[str]
    
    min_power: float
    min_sample_size: int
    penalize_instability: bool
    penalize_heterogeneity: bool


@dataclass(frozen=True)
class ConfidenceFactors:
    """
    Raw components that feed belief.
    
    These are inputs, not outputs.
    """
    power: Optional[float]
    p_value: float
    adjusted_p_value: Optional[float]
    effect_size: float
    sample_size: int
    
    window_consistency: float
    segment_consistency: float
    
    sensitivity_score: float
    contamination_risk: bool


@dataclass(frozen=True)
class ConfidenceResult:
    """
    Immutable confidence assessment result.
    
    Single scalar + explanations.
    No decisions.
    """
    experiment_id: str
    metric_name: str
    window: str
    segment: Optional[str]
    
    treatment_variant: str
    control_variant: str
    
    confidence_score: float  # [0, 1]
    confidence_level: str    # low / medium / high / extreme
    
    contributing_factors: Dict[str, Any]
    confidence_timestamp: datetime
    
    effect_snapshot_id: str
    test_snapshot_id: str


class ConfidenceLevel(Enum):
    """
    Confidence level classifications.
    
    Mapping logic is explicit and logged.
    """
    LOW = "low"           # observed but fragile
    MEDIUM = "medium"     # credible but context-dependent
    HIGH = "high"         # stable, repeatable
    EXTREME = "extreme"   # exceptionally robust


# ============================================================================
# EVIDENCE WEIGHTING
# ============================================================================

@dataclass(frozen=True)
class WeightConfig:
    """
    Versioned weights for confidence aggregation.
    
    These weights are logged and must be reproducible.
    """
    version: str
    
    # Statistical strength weights
    w_p_value: float
    w_power: float
    w_effect_magnitude: float
    
    # Effect reliability weights
    w_temporal_consistency: float
    w_segment_consistency: float
    w_direction_stability: float
    
    # Sample adequacy weights
    w_sample_size: float
    w_exposure_balance: float
    
    # Structural integrity weights
    w_no_contamination: float
    w_sensitivity: float
    
    # Penalty multipliers
    penalty_low_power: float
    penalty_instability: float
    penalty_heterogeneity: float
    penalty_early_stopping: float


class EvidenceWeighter:
    """
    Converts raw factors into weighted belief components.
    
    All transformations are deterministic and logged.
    """
    
    DEFAULT_WEIGHTS = WeightConfig(
        version="1.0.0",
        w_p_value=0.20,
        w_power=0.15,
        w_effect_magnitude=0.10,
        w_temporal_consistency=0.15,
        w_segment_consistency=0.10,
        w_direction_stability=0.10,
        w_sample_size=0.10,
        w_exposure_balance=0.05,
        w_no_contamination=0.03,
        w_sensitivity=0.02,
        penalty_low_power=0.30,
        penalty_instability=0.25,
        penalty_heterogeneity=0.20,
        penalty_early_stopping=0.15
    )
    
    def __init__(self, weights: Optional[WeightConfig] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS
        logger.info(f"EvidenceWeighter initialized with version {self.weights.version}")
    
    def weight_p_value(self, p_value: float, adjusted_p_value: Optional[float]) -> float:
        """
        Lower p-value → higher belief.
        
        Uses adjusted p-value if available (more conservative).
        """
        p = adjusted_p_value if adjusted_p_value is not None else p_value
        
        # Transform: 1 - log-scale normalized p-value
        if p <= 0.0:
            return 1.0
        if p >= 1.0:
            return 0.0
        
        # Log transformation for better discrimination at low p-values
        score = max(0.0, min(1.0, 1.0 - math.log10(p) / math.log10(0.001)))
        return score
    
    def weight_power(self, power: Optional[float], min_power: float) -> float:
        """
        Power above threshold → boost.
        Underpowered → penalty.
        """
        if power is None:
            return 0.5  # Neutral when power unknown
        
        if power >= min_power:
            # Bonus for exceeding threshold
            excess = (power - min_power) / (1.0 - min_power)
            return 0.5 + 0.5 * excess
        else:
            # Penalty for underpowered
            deficit = power / min_power
            return 0.5 * deficit
    
    def weight_effect_magnitude(self, effect_size: float) -> float:
        """
        Larger absolute effect → slight boost.
        
        But confidence ≠ effect size.
        """
        abs_effect = abs(effect_size)
        
        # Diminishing returns on very large effects
        return min(1.0, math.tanh(abs_effect))
    
    def weight_consistency(self, consistency_score: float) -> float:
        """
        Direct mapping for consistency scores [0, 1].
        """
        return max(0.0, min(1.0, consistency_score))
    
    def weight_sample_size(self, sample_size: int, min_sample_size: int) -> float:
        """
        Sample adequacy score.
        
        Low adequacy ≠ failure — it reduces belief.
        """
        if sample_size >= min_sample_size * 2:
            return 1.0
        elif sample_size >= min_sample_size:
            excess_ratio = (sample_size - min_sample_size) / min_sample_size
            return 0.7 + 0.3 * excess_ratio
        else:
            deficit_ratio = sample_size / min_sample_size
            return 0.7 * deficit_ratio
    
    def weight_contamination(self, contamination_risk: bool) -> float:
        """
        No contamination → full weight.
        Contamination risk → zero weight.
        """
        return 0.0 if contamination_risk else 1.0
    
    def weight_sensitivity(self, sensitivity_score: float) -> float:
        """
        Higher sensitivity → more robust to assumptions.
        """
        return max(0.0, min(1.0, sensitivity_score))


# ============================================================================
# CREDIBILITY AGGREGATION
# ============================================================================

class CredibilityAggregator:
    """
    Aggregates weighted factors into a single confidence score.
    
    Deterministic: same inputs → same output.
    """
    
    def __init__(self, weighter: EvidenceWeighter):
        self.weighter = weighter
    
    def aggregate(
        self,
        factors: ConfidenceFactors,
        spec: ConfidenceSpec
    ) -> Tuple[float, Dict[str, float]]:
        """
        Returns: (confidence_score, component_breakdown)
        
        confidence_score ∈ [0, 1]
        """
        w = self.weighter.weights
        
        # Compute weighted components
        components = {}
        
        # 1. Statistical strength
        components['p_value'] = self.weighter.weight_p_value(
            factors.p_value, 
            factors.adjusted_p_value
        ) * w.w_p_value
        
        components['power'] = self.weighter.weight_power(
            factors.power, 
            spec.min_power
        ) * w.w_power
        
        components['effect_magnitude'] = self.weighter.weight_effect_magnitude(
            factors.effect_size
        ) * w.w_effect_magnitude
        
        # 2. Effect reliability
        components['temporal_consistency'] = self.weighter.weight_consistency(
            factors.window_consistency
        ) * w.w_temporal_consistency
        
        components['segment_consistency'] = self.weighter.weight_consistency(
            factors.segment_consistency
        ) * w.w_segment_consistency
        
        # Direction stability is captured in temporal consistency
        
        # 3. Sample adequacy
        components['sample_size'] = self.weighter.weight_sample_size(
            factors.sample_size,
            spec.min_sample_size
        ) * w.w_sample_size
        
        # Exposure balance assumed captured in sample_size for now
        
        # 4. Structural integrity
        components['no_contamination'] = self.weighter.weight_contamination(
            factors.contamination_risk
        ) * w.w_no_contamination
        
        components['sensitivity'] = self.weighter.weight_sensitivity(
            factors.sensitivity_score
        ) * w.w_sensitivity
        
        # Base score: sum of all weighted components
        base_score = sum(components.values())
        
        # Apply penalties
        penalties = {}
        final_score = base_score
        
        # Penalty: low power
        if factors.power is not None and factors.power < spec.min_power:
            penalty = w.penalty_low_power * (1.0 - factors.power / spec.min_power)
            penalties['low_power'] = penalty
            final_score *= (1.0 - penalty)
        
        # Penalty: instability
        if spec.penalize_instability and factors.window_consistency < 0.7:
            penalty = w.penalty_instability * (1.0 - factors.window_consistency)
            penalties['instability'] = penalty
            final_score *= (1.0 - penalty)
        
        # Penalty: heterogeneity
        if spec.penalize_heterogeneity and factors.segment_consistency < 0.7:
            penalty = w.penalty_heterogeneity * (1.0 - factors.segment_consistency)
            penalties['heterogeneity'] = penalty
            final_score *= (1.0 - penalty)
        
        # Clamp to [0, 1]
        final_score = max(0.0, min(1.0, final_score))
        
        # Build full breakdown
        breakdown = {
            'components': components,
            'penalties': penalties,
            'base_score': base_score,
            'final_score': final_score,
            'weight_version': w.version
        }
        
        return final_score, breakdown
    
    def classify_level(self, score: float) -> ConfidenceLevel:
        """
        Maps score to confidence level.
        
        Thresholds are explicit and logged.
        """
        if score >= 0.85:
            return ConfidenceLevel.EXTREME
        elif score >= 0.70:
            return ConfidenceLevel.HIGH
        elif score >= 0.50:
            return ConfidenceLevel.MEDIUM
        else:
            return ConfidenceLevel.LOW


# ============================================================================
# TEMPORAL STABILITY ANALYSIS
# ============================================================================

@dataclass
class TemporalPattern:
    """Results from temporal stability analysis."""
    sign_flips: int
    early_spike: bool
    late_reversal: bool
    decay_detected: bool
    persistence_score: float
    consistency_score: float


class TemporalStabilityAnalyzer:
    """
    Measures effect stability over time.
    
    Penalizes:
    - Effect sign flips
    - Early-only spikes
    - Late-only reversals
    
    Does not modify effects.
    """
    
    def analyze(
        self,
        effect_sizes_by_window: List[Tuple[str, float]]
    ) -> TemporalPattern:
        """
        Analyzes effect trajectory across time windows.
        
        Returns pattern characteristics and consistency score.
        """
        if not effect_sizes_by_window:
            return TemporalPattern(
                sign_flips=0,
                early_spike=False,
                late_reversal=False,
                decay_detected=False,
                persistence_score=0.0,
                consistency_score=0.0
            )
        
        effects = [e for _, e in effect_sizes_by_window]
        
        # Count sign flips
        sign_flips = 0
        for i in range(1, len(effects)):
            if (effects[i-1] > 0) != (effects[i] > 0):
                sign_flips += 1
        
        # Detect early spike (first 25% much larger than rest)
        early_spike = False
        if len(effects) >= 4:
            early_avg = statistics.mean(abs(e) for e in effects[:len(effects)//4])
            later_avg = statistics.mean(abs(e) for e in effects[len(effects)//4:])
            if early_avg > 2 * later_avg and later_avg > 0:
                early_spike = True
        
        # Detect late reversal (last 25% flips sign)
        late_reversal = False
        if len(effects) >= 4:
            early_sign = statistics.mean(effects[:len(effects)*3//4])
            late_sign = statistics.mean(effects[len(effects)*3//4:])
            if (early_sign > 0) != (late_sign > 0):
                late_reversal = True
        
        # Detect decay (monotonic decrease in absolute magnitude)
        decay_detected = False
        if len(effects) >= 3:
            abs_effects = [abs(e) for e in effects]
            decreasing = all(abs_effects[i] >= abs_effects[i+1] 
                           for i in range(len(abs_effects)-1))
            if decreasing and abs_effects[0] > abs_effects[-1] * 1.5:
                decay_detected = True
        
        # Persistence score (low variance in magnitude)
        abs_effects = [abs(e) for e in effects]
        if len(abs_effects) > 1:
            mean_abs = statistics.mean(abs_effects)
            if mean_abs > 0:
                cv = statistics.stdev(abs_effects) / mean_abs
                persistence_score = max(0.0, 1.0 - cv)
            else:
                persistence_score = 0.0
        else:
            persistence_score = 1.0
        
        # Consistency score (penalize flips, spikes, reversals)
        consistency = 1.0
        consistency -= 0.2 * min(sign_flips, 3)  # Up to -0.6 for flips
        if early_spike:
            consistency -= 0.2
        if late_reversal:
            consistency -= 0.3
        if decay_detected:
            consistency -= 0.1
        
        consistency = max(0.0, consistency)
        
        return TemporalPattern(
            sign_flips=sign_flips,
            early_spike=early_spike,
            late_reversal=late_reversal,
            decay_detected=decay_detected,
            persistence_score=persistence_score,
            consistency_score=consistency
        )


# ============================================================================
# CROSS-METRIC CONSISTENCY
# ============================================================================

@dataclass
class MetricConsistency:
    """Results from cross-metric consistency check."""
    agreement_score: float
    divergent_metrics: List[str]
    aligned_metrics: List[str]
    proxy_divergence: bool


class CrossMetricConsistencyChecker:
    """
    Validates effect agreement across related metrics.
    
    High disagreement → confidence reduced.
    """
    
    def __init__(self, metric_relationships: Optional[Dict[str, List[str]]] = None):
        """
        metric_relationships: mapping of primary metric to related metrics
        e.g., {'engagement': ['retention', 'session_length']}
        """
        self.metric_relationships = metric_relationships or {}
    
    def check(
        self,
        primary_metric: str,
        primary_effect: float,
        related_effects: Dict[str, float]
    ) -> MetricConsistency:
        """
        Checks if related metrics align with primary metric effect.
        
        Returns agreement score and divergent metrics.
        """
        if primary_metric not in self.metric_relationships:
            # No relationships defined, neutral score
            return MetricConsistency(
                agreement_score=0.5,
                divergent_metrics=[],
                aligned_metrics=[],
                proxy_divergence=False
            )
        
        related = self.metric_relationships[primary_metric]
        primary_sign = 1 if primary_effect > 0 else -1
        
        aligned = []
        divergent = []
        
        for metric in related:
            if metric not in related_effects:
                continue
            
            effect = related_effects[metric]
            effect_sign = 1 if effect > 0 else -1
            
            if effect_sign == primary_sign:
                aligned.append(metric)
            else:
                divergent.append(metric)
        
        # Agreement score
        total = len(aligned) + len(divergent)
        if total == 0:
            agreement_score = 0.5
        else:
            agreement_score = len(aligned) / total
        
        # Proxy divergence if majority diverge
        proxy_divergence = len(divergent) > len(aligned)
        
        return MetricConsistency(
            agreement_score=agreement_score,
            divergent_metrics=divergent,
            aligned_metrics=aligned,
            proxy_divergence=proxy_divergence
        )


# ============================================================================
# SAMPLE ADEQUACY EVALUATION
# ============================================================================

@dataclass
class AdequacyAssessment:
    """Results from sample adequacy evaluation."""
    adequacy_score: float
    head_tail_imbalance: bool
    platform_sparsity: Dict[str, bool]
    segment_imbalance: bool
    warnings: List[str]


class SampleAdequacyEvaluator:
    """
    Assesses sample quality and coverage.
    
    Low adequacy ≠ failure — it reduces belief.
    """
    
    def evaluate(
        self,
        sample_size: int,
        min_sample_size: int,
        segment_sizes: Optional[Dict[str, int]] = None,
        platform_sizes: Optional[Dict[str, int]] = None
    ) -> AdequacyAssessment:
        """
        Evaluates sample adequacy across multiple dimensions.
        """
        warnings = []
        
        # Base adequacy from overall sample size
        if sample_size >= min_sample_size * 2:
            adequacy = 1.0
        elif sample_size >= min_sample_size:
            adequacy = 0.7 + 0.3 * ((sample_size - min_sample_size) / min_sample_size)
        else:
            adequacy = 0.7 * (sample_size / min_sample_size)
            warnings.append(f"Sample size {sample_size} below minimum {min_sample_size}")
        
        # Check segment imbalance
        segment_imbalance = False
        if segment_sizes and len(segment_sizes) > 1:
            sizes = list(segment_sizes.values())
            max_size = max(sizes)
            min_size = min(sizes)
            if max_size > 5 * min_size:
                segment_imbalance = True
                adequacy *= 0.85
                warnings.append("Severe segment imbalance detected")
        
        # Check platform sparsity
        platform_sparsity = {}
        if platform_sizes:
            for platform, size in platform_sizes.items():
                sparse = size < min_sample_size * 0.1
                platform_sparsity[platform] = sparse
                if sparse:
                    adequacy *= 0.90
                    warnings.append(f"Platform {platform} has sparse data")
        
        # Check head/tail imbalance (assume time-based if not provided)
        head_tail_imbalance = False
        
        adequacy = max(0.0, min(1.0, adequacy))
        
        return AdequacyAssessment(
            adequacy_score=adequacy,
            head_tail_imbalance=head_tail_imbalance,
            platform_sparsity=platform_sparsity,
            segment_imbalance=segment_imbalance,
            warnings=warnings
        )


# ============================================================================
# CONFIDENCE ESTIMATOR (CORE ENGINE)
# ============================================================================

class ConfidenceEstimator:
    """
    Core engine for confidence estimation.
    
    Flow:
    1. Load immutable effect size
    2. Load statistical test results
    3. Load outcome metadata
    4. Aggregate belief factors
    5. Penalize known instability
    6. Produce confidence score
    7. Persist immutable result
    """
    
    def __init__(
        self,
        weighter: Optional[EvidenceWeighter] = None,
        temporal_analyzer: Optional[TemporalStabilityAnalyzer] = None,
        metric_checker: Optional[CrossMetricConsistencyChecker] = None,
        adequacy_evaluator: Optional[SampleAdequacyEvaluator] = None
    ):
        self.weighter = weighter or EvidenceWeighter()
        self.aggregator = CredibilityAggregator(self.weighter)
        self.temporal_analyzer = temporal_analyzer or TemporalStabilityAnalyzer()
        self.metric_checker = metric_checker or CrossMetricConsistencyChecker()
        self.adequacy_evaluator = adequacy_evaluator or SampleAdequacyEvaluator()
        
        logger.info("ConfidenceEstimator initialized")
    
    def estimate_confidence(
        self,
        spec: ConfidenceSpec,
        factors: ConfidenceFactors,
        treatment_variant: str,
        control_variant: str,
        effect_snapshot_id: str,
        test_snapshot_id: str
    ) -> ConfidenceResult:
        """
        Estimates confidence for a single measurement.
        
        Returns immutable ConfidenceResult.
        """
        logger.info(
            f"Estimating confidence: {spec.experiment_id} / "
            f"{spec.metric_name} / {spec.window} / {spec.segment}"
        )
        
        # Aggregate factors into confidence score
        score, breakdown = self.aggregator.aggregate(factors, spec)
        
        # Classify confidence level
        level = self.aggregator.classify_level(score)
        
        # Build contributing factors for transparency
        contributing = {
            'breakdown': breakdown,
            'raw_factors': {
                'power': factors.power,
                'p_value': factors.p_value,
                'adjusted_p_value': factors.adjusted_p_value,
                'effect_size': factors.effect_size,
                'sample_size': factors.sample_size,
                'window_consistency': factors.window_consistency,
                'segment_consistency': factors.segment_consistency,
                'sensitivity_score': factors.sensitivity_score,
                'contamination_risk': factors.contamination_risk
            },
            'thresholds': {
                'min_power': spec.min_power,
                'min_sample_size': spec.min_sample_size
            }
        }
        
        result = ConfidenceResult(
            experiment_id=spec.experiment_id,
            metric_name=spec.metric_name,
            window=spec.window,
            segment=spec.segment,
            treatment_variant=treatment_variant,
            control_variant=control_variant,
            confidence_score=score,
            confidence_level=level.value,
            contributing_factors=contributing,
            confidence_timestamp=datetime.utcnow(),
            effect_snapshot_id=effect_snapshot_id,
            test_snapshot_id=test_snapshot_id
        )
        
        logger.info(
            f"Confidence estimated: score={score:.3f}, level={level.value}"
        )
        
        return result
    
    def estimate_by_window(
        self,
        base_spec: ConfidenceSpec,
        window_factors: Dict[str, ConfidenceFactors],
        treatment_variant: str,
        control_variant: str,
        effect_snapshot_ids: Dict[str, str],
        test_snapshot_ids: Dict[str, str]
    ) -> Dict[str, ConfidenceResult]:
        """
        Estimates confidence across multiple time windows.
        
        Incorporates temporal stability analysis.
        """
        results = {}
        
        # First pass: individual window confidence
        for window, factors in window_factors.items():
            spec = ConfidenceSpec(
                experiment_id=base_spec.experiment_id,
                metric_name=base_spec.metric_name,
                window=window,
                segment=base_spec.segment,
                min_power=base_spec.min_power,
                min_sample_size=base_spec.min_sample_size,
                penalize_instability=base_spec.penalize_instability,
                penalize_heterogeneity=base_spec.penalize_heterogeneity
            )
            
            result = self.estimate_confidence(
                spec=spec,
                factors=factors,
                treatment_variant=treatment_variant,
                control_variant=control_variant,
                effect_snapshot_id=effect_snapshot_ids[window],
                test_snapshot_id=test_snapshot_ids[window]
            )
            
            results[window] = result
        
        # Temporal stability analysis
        effect_trajectory = [
            (w, window_factors[w].effect_size) 
            for w in sorted(window_factors.keys())
        ]
        
        temporal_pattern = self.temporal_analyzer.analyze(effect_trajectory)
        
        # Adjust confidence based on temporal stability
        if base_spec.penalize_instability:
            for window, result in results.items():
                # Create adjusted result with temporal penalty
                adjusted_score = result.confidence_score * temporal_pattern.consistency_score
                adjusted_level = self.aggregator.classify_level(adjusted_score)
                
                # Update contributing factors
                adjusted_factors = result.contributing_factors.copy()
                adjusted_factors['temporal_analysis'] = {
                    'sign_flips': temporal_pattern.sign_flips,
                    'early_spike': temporal_pattern.early_spike,
                    'late_reversal': temporal_pattern.late_reversal,
                    'decay_detected': temporal_pattern.decay_detected,
                    'persistence_score': temporal_pattern.persistence_score,
                    'consistency_score': temporal_pattern.consistency_score
                }
                
                # Create new result with adjusted confidence
                results[window] = ConfidenceResult(
                    experiment_id=result.experiment_id,
                    metric_name=result.metric_name,
                    window=result.window,
                    segment=result.segment,
                    treatment_variant=result.treatment_variant,
                    control_variant=result.control_variant,
                    confidence_score=adjusted_score,
                    confidence_level=adjusted_level.value,
                    contributing_factors=adjusted_factors,
                    confidence_timestamp=result.confidence_timestamp,
                    effect_snapshot_id=result.effect_snapshot_id,
                    test_snapshot_id=result.test_snapshot_id
                )
        
        return results
    
    def estimate_by_segment(
        self,
        base_spec: ConfidenceSpec,
        segment_factors: Dict[str, ConfidenceFactors],
        treatment_variant: str,
        control_variant: str,
        effect_snapshot_ids: Dict[str, str],
        test_snapshot_ids: Dict[str, str]
    ) -> Dict[str, ConfidenceResult]:
        """
        Estimates confidence across segments.
        
        Incorporates segment heterogeneity analysis.
        """
        results = {}
        
        # Estimate per segment
        for segment, factors in segment_factors.items():
            spec = ConfidenceSpec(
                experiment_id=base_spec.experiment_id,
                metric_name=base_spec.metric_name,
                window=base_spec.window,
                segment=segment,
                min_power=base_spec.min_power,
                min_sample_size=base_spec.min_sample_size,
                penalize_instability=base_spec.penalize_instability,
                penalize_heterogeneity=base_spec.penalize_heterogeneity
            )
            
            result = self.estimate_confidence(
                spec=spec,
                factors=factors,
                treatment_variant=treatment_variant,
                control_variant=control_variant,
                effect_snapshot_id=effect_snapshot_ids[segment],
                test_snapshot_id=test_snapshot_ids[segment]
            )
            
            results[segment] = result
        
        return results


# ============================================================================
# CONFIDENCE STORE
# ============================================================================

class ConfidenceStore:
    """
    Append-only, versioned, queryable, immutable storage.
    
    Used by:
    - rollout logic
    - freeze manager
    - reports
    - RL reward shaping (indirectly)
    """
    
    def __init__(self):
        self._store: List[ConfidenceResult] = []
        self._index: Dict[str, List[int]] = defaultdict(list)
        logger.info("ConfidenceStore initialized")
    
    def append(self, result: ConfidenceResult) -> None:
        """
        Appends immutable confidence result.
        
        No updates. No deletions.
        """
        idx = len(self._store)
        self._store.append(result)
        
        # Build indices
        key = self._make_key(
            result.experiment_id,
            result.metric_name,
            result.window,
            result.segment
        )
        self._index[key].append(idx)
        
        logger.debug(f"Stored confidence result: {key} at index {idx}")
    
    def query(
        self,
        experiment_id: str,
        metric_name: Optional[str] = None,
        window: Optional[str] = None,
        segment: Optional[str] = None
    ) -> List[ConfidenceResult]:
        """
        Queries confidence results.
        
        Returns all matching results in chronological order.
        """
        if metric_name and window:
            key = self._make_key(experiment_id, metric_name, window, segment)
            indices = self._index.get(key, [])
            return [self._store[i] for i in indices]
        
        # Broader query: filter manually
        results = []
        for result in self._store:
            if result.experiment_id != experiment_id:
                continue
            if metric_name and result.metric_name != metric_name:
                continue
            if window and result.window != window:
                continue
            if segment is not None and result.segment != segment:
                continue
            results.append(result)
        
        return results
    
    def get_latest(
        self,
        experiment_id: str,
        metric_name: str,
        window: str,
        segment: Optional[str] = None
    ) -> Optional[ConfidenceResult]:
        """
        Returns most recent confidence result for exact match.
        """
        results = self.query(experiment_id, metric_name, window, segment)
        return results[-1] if results else None
    
    def get_all(self) -> List[ConfidenceResult]:
        """
        Returns all stored confidence results.
        
        For auditing and analysis.
        """
        return list(self._store)
    
    def _make_key(
        self,
        experiment_id: str,
        metric_name: str,
        window: str,
        segment: Optional[str]
    ) -> str:
        """Builds index key."""
        seg = segment if segment else "__NONE__"
        return f"{experiment_id}::{metric_name}::{window}::{seg}"
    
    def export_json(self) -> str:
        """
        Exports entire store as JSON.
        
        For reproducibility and auditing.
        """
        data = []
        for result in self._store:
            data.append({
                'experiment_id': result.experiment_id,
                'metric_name': result.metric_name,
                'window': result.window,
                'segment': result.segment,
                'treatment_variant': result.treatment_variant,
                'control_variant': result.control_variant,
                'confidence_score': result.confidence_score,
                'confidence_level': result.confidence_level,
                'contributing_factors': result.contributing_factors,
                'confidence_timestamp': result.confidence_timestamp.isoformat(),
                'effect_snapshot_id': result.effect_snapshot_id,
                'test_snapshot_id': result.test_snapshot_id
            })
        
        return json.dumps(data, indent=2)


# ============================================================================
# CONFIDENCE WATCHDOG
# ============================================================================

@dataclass
class WatchdogAlert:
    """Alert from confidence watchdog."""
    alert_type: str
    severity: str  # warning / critical
    message: str
    affected_experiments: List[str]
    recommendation: str


class ConfidenceWatchdog:
    """
    Flags anomalies and systematic issues in confidence patterns.
    
    Flags:
    - High confidence with low power
    - Confidence inflation patterns
    - Systematic bias toward "wins"
    - Repeat marginal cases
    
    Can recommend:
    - Experiment redesign
    - Longer run
    - Segmentation refinement
    """
    
    def __init__(self, store: ConfidenceStore):
        self.store = store
        logger.info("ConfidenceWatchdog initialized")
    
    def check_all(self) -> List[WatchdogAlert]:
        """
        Runs all watchdog checks.
        
        Returns list of alerts.
        """
        alerts = []
        
        alerts.extend(self._check_high_confidence_low_power())
        alerts.extend(self._check_confidence_inflation())
        alerts.extend(self._check_win_bias())
        alerts.extend(self._check_marginal_repeats())
        
        logger.info(f"Watchdog generated {len(alerts)} alerts")
        return alerts
    
    def _check_high_confidence_low_power(self) -> List[WatchdogAlert]:
        """
        Flags high confidence results with insufficient power.
        
        This indicates potential over-confidence.
        """
        alerts = []
        suspicious = []
        
        for result in self.store.get_all():
            factors = result.contributing_factors.get('raw_factors', {})
            power = factors.get('power')
            
            if power is None:
                continue
            
            if result.confidence_score >= 0.75 and power < 0.70:
                suspicious.append(result.experiment_id)
        
        if suspicious:
            alerts.append(WatchdogAlert(
                alert_type='high_confidence_low_power',
                severity='warning',
                message=f'Found {len(suspicious)} results with high confidence but low power',
                affected_experiments=list(set(suspicious)),
                recommendation='Review power calculations or extend experiment duration'
            ))
        
        return alerts
    
    def _check_confidence_inflation(self) -> List[WatchdogAlert]:
        """
        Detects if confidence scores are systematically too high.
        
        Expected: roughly balanced distribution across levels.
        """
        alerts = []
        results = self.store.get_all()
        
        if len(results) < 20:
            return alerts  # Not enough data
        
        level_counts = defaultdict(int)
        for result in results:
            level_counts[result.confidence_level] += 1
        
        total = len(results)
        extreme_pct = level_counts['extreme'] / total
        high_pct = level_counts['high'] / total
        
        # If >60% are high/extreme, possible inflation
        if extreme_pct + high_pct > 0.60:
            alerts.append(WatchdogAlert(
                alert_type='confidence_inflation',
                severity='warning',
                message=f'{(extreme_pct + high_pct)*100:.1f}% of results are high/extreme confidence',
                affected_experiments=[],
                recommendation='Review weighting configuration for possible over-confidence'
            ))
        
        return alerts
    
    def _check_win_bias(self) -> List[WatchdogAlert]:
        """
        Checks for systematic bias toward positive effects having higher confidence.
        
        Confidence should be independent of effect direction.
        """
        alerts = []
        results = self.store.get_all()
        
        if len(results) < 30:
            return alerts
        
        positive_confidence = []
        negative_confidence = []
        
        for result in results:
            factors = result.contributing_factors.get('raw_factors', {})
            effect = factors.get('effect_size', 0)
            
            if effect > 0:
                positive_confidence.append(result.confidence_score)
            elif effect < 0:
                negative_confidence.append(result.confidence_score)
        
        if positive_confidence and negative_confidence:
            pos_mean = statistics.mean(positive_confidence)
            neg_mean = statistics.mean(negative_confidence)
            
            # If positive effects have >20% higher confidence, flag it
            if pos_mean > neg_mean * 1.20:
                alerts.append(WatchdogAlert(
                    alert_type='win_bias',
                    severity='critical',
                    message=f'Positive effects show {(pos_mean/neg_mean - 1)*100:.1f}% higher confidence',
                    affected_experiments=[],
                    recommendation='Investigate systematic bias in confidence estimation'
                ))
        
        return alerts
    
    def _check_marginal_repeats(self) -> List[WatchdogAlert]:
        """
        Flags experiments repeatedly producing marginal confidence.
        
        May indicate need for redesign.
        """
        alerts = []
        results = self.store.get_all()
        
        exp_scores = defaultdict(list)
        for result in results:
            exp_scores[result.experiment_id].append(result.confidence_score)
        
        repeat_marginal = []
        for exp_id, scores in exp_scores.items():
            if len(scores) >= 3:
                # Check if most results are in marginal range [0.4, 0.6]
                marginal_count = sum(1 for s in scores if 0.4 <= s <= 0.6)
                if marginal_count / len(scores) >= 0.75:
                    repeat_marginal.append(exp_id)
        
        if repeat_marginal:
            alerts.append(WatchdogAlert(
                alert_type='repeat_marginal',
                severity='warning',
                message=f'{len(repeat_marginal)} experiments consistently produce marginal confidence',
                affected_experiments=repeat_marginal,
                recommendation='Consider longer runs, larger samples, or segmentation refinement'
            ))
        
        return alerts


# ============================================================================
# DETERMINISM & AUDITABILITY
# ============================================================================

class ConfidenceAuditor:
    """
    Ensures determinism and reproducibility.
    
    Given same inputs, confidence scores MUST be identical.
    """
    
    @staticmethod
    def verify_determinism(
        estimator: ConfidenceEstimator,
        spec: ConfidenceSpec,
        factors: ConfidenceFactors,
        treatment: str,
        control: str,
        effect_id: str,
        test_id: str,
        runs: int = 10
    ) -> bool:
        """
        Verifies that repeated runs produce identical results.
        
        Returns True if deterministic, False otherwise.
        """
        results = []
        
        for _ in range(runs):
            result = estimator.estimate_confidence(
                spec=spec,
                factors=factors,
                treatment_variant=treatment,
                control_variant=control,
                effect_snapshot_id=effect_id,
                test_snapshot_id=test_id
            )
            results.append(result.confidence_score)
        
        # All scores must be identical
        first = results[0]
        deterministic = all(abs(score - first) < 1e-10 for score in results)
        
        if deterministic:
            logger.info("Determinism verified: all runs identical")
        else:
            logger.error(f"Determinism FAILED: scores vary {results}")
        
        return deterministic
    
    @staticmethod
    def replay_from_snapshot(
        snapshot: Dict[str, Any],
        weighter: EvidenceWeighter
    ) -> ConfidenceResult:
        """
        Replays confidence estimation from saved snapshot.
        
        Used for auditing historical decisions.
        """
        # Reconstruct spec
        spec = ConfidenceSpec(
            experiment_id=snapshot['experiment_id'],
            metric_name=snapshot['metric_name'],
            window=snapshot['window'],
            segment=snapshot.get('segment'),
            min_power=snapshot['min_power'],
            min_sample_size=snapshot['min_sample_size'],
            penalize_instability=snapshot['penalize_instability'],
            penalize_heterogeneity=snapshot['penalize_heterogeneity']
        )
        
        # Reconstruct factors
        raw = snapshot['raw_factors']
        factors = ConfidenceFactors(
            power=raw['power'],
            p_value=raw['p_value'],
            adjusted_p_value=raw.get('adjusted_p_value'),
            effect_size=raw['effect_size'],
            sample_size=raw['sample_size'],
            window_consistency=raw['window_consistency'],
            segment_consistency=raw['segment_consistency'],
            sensitivity_score=raw['sensitivity_score'],
            contamination_risk=raw['contamination_risk']
        )
        
        # Re-estimate
        estimator = ConfidenceEstimator(weighter=weighter)
        result = estimator.estimate_confidence(
            spec=spec,
            factors=factors,
            treatment_variant=snapshot['treatment_variant'],
            control_variant=snapshot['control_variant'],
            effect_snapshot_id=snapshot['effect_snapshot_id'],
            test_snapshot_id=snapshot['test_snapshot_id']
        )
        
        # Verify match
        original_score = snapshot['confidence_score']
        if abs(result.confidence_score - original_score) > 1e-6:
            logger.warning(
                f"Replay mismatch: original={original_score:.6f}, "
                f"replay={result.confidence_score:.6f}"
            )
        
        return result


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

def example_usage():
    """
    Demonstrates proper usage of confidence estimation system.
    """
    
    # Initialize components
    weighter = EvidenceWeighter()
    estimator = ConfidenceEstimator(weighter=weighter)
    store = ConfidenceStore()
    watchdog = ConfidenceWatchdog(store)
    
    # Define spec
    spec = ConfidenceSpec(
        experiment_id="exp_001",
        metric_name="engagement_rate",
        window="7d",
        segment=None,
        min_power=0.80,
        min_sample_size=1000,
        penalize_instability=True,
        penalize_heterogeneity=True
    )
    
    # Gather factors (would come from upstream modules)
    factors = ConfidenceFactors(
        power=0.85,
        p_value=0.003,
        adjusted_p_value=0.009,
        effect_size=0.12,
        sample_size=5000,
        window_consistency=0.88,
        segment_consistency=0.92,
        sensitivity_score=0.75,
        contamination_risk=False
    )
    
    # Estimate confidence
    result = estimator.estimate_confidence(
        spec=spec,
        factors=factors,
        treatment_variant="variant_a",
        control_variant="control",
        effect_snapshot_id="effect_snap_123",
        test_snapshot_id="test_snap_456"
    )
    
    # Store result
    store.append(result)
    
    # Output
    print(f"Confidence Score: {result.confidence_score:.3f}")
    print(f"Confidence Level: {result.confidence_level}")
    print(f"Timestamp: {result.confidence_timestamp}")
    
    # Check for issues
    alerts = watchdog.check_all()
    for alert in alerts:
        print(f"Alert: {alert.alert_type} - {alert.message}")
    
    # Verify determinism
    is_deterministic = ConfidenceAuditor.verify_determinism(
        estimator=estimator,
        spec=spec,
        factors=factors,
        treatment="variant_a",
        control="control",
        effect_id="effect_snap_123",
        test_id="test_snap_456",
        runs=5
    )
    
    print(f"Deterministic: {is_deterministic}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    example_usage()


# ============================================================================
# HARD INVARIANTS ENFORCEMENT
# ============================================================================

"""
This module enforces the following HARD INVARIANTS:

❌ NEVER decide rollout
❌ NEVER interpret business impact
❌ NEVER change effect sizes
❌ NEVER change p-values
❌ NEVER override statistical tests
❌ NEVER auto-approve variants

This file ONLY quantifies belief.

All outputs are immutable.
All computations are deterministic.
All decisions are deferred to downstream modules.

Confidence is belief stability under uncertainty.
High confidence ≠ large effect.
Low confidence ≠ wrong result.

This file measures trust, not truth.
"""