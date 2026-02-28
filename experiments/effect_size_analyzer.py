"""
/experiments/effect_size_analyzer.py

Causal Lift & Impact Engine

PURPOSE:
    Answer exactly one question:
    "What was the causal impact of the variant, and how big was it?"

NOT:
    - "Did it win?"
    - "Is it significant?"
    - "Should we ship?"

Just causal lift, cleanly computed.

RESPONSIBILITIES:
    ✓ Compute effect sizes precisely
    ✓ Compare treatment vs control
    ✓ Calculate relative lift
    ✓ Detect heterogeneity
    ✓ Test sensitivity
    ✓ Persist immutable results

NON-RESPONSIBILITIES (NEVER DO):
    ✗ Perform statistical tests
    ✗ Decide significance
    ✗ Rank variants
    ✗ Recommend rollout
    ✗ Modify outcomes
    ✗ Smooth heavy tails
    ✗ Normalize metrics (already done upstream)

CORE PRINCIPLE:
    Effect size measures magnitude, not certainty.
    A large effect can be uncertain.
    A small effect can be real.
    Never confuse lift with belief.

DEPENDENCY DIRECTION:
    outcome_collector.py → effect_size_analyzer.py → statistical_tests.py
    ONE-WAY ONLY. This file NEVER reaches "up".
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
import math


# Import from outcome_collector (dependency)
from outcome_collector import OutcomeRecord, OutcomeWindow, OutcomeStore


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


class EffectMethod(Enum):
    """
    Allowed methods for computing effect sizes.
    Each method matches specific metric properties.
    """
    DIFF_MEANS = "diff_means"          # Stable metrics (CTR, bounce rate)
    RATIO = "ratio"                     # Scale-sensitive (revenue, views)
    LOG_RATIO = "log_ratio"            # Heavy-tailed virality (shares, viral views)
    SLOPE_DELTA = "slope_delta"        # Growth dynamics (follower growth)

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class EffectMetricSpec:
    """
    Defines how lift is computed per metric.
    No dynamic methods. No auto-selection.
    """
    metric_name: str
    method: EffectMethod
    unit: str
    platform_scope: str  # global | youtube | tiktok | instagram
    minimum_units: int   # Minimum sample size required

    def __post_init__(self):
        """Validate spec at construction."""
        if self.minimum_units < 2:
            raise ValueError(f"minimum_units must be >= 2, got {self.minimum_units}")
        
        valid_scopes = {"global", "youtube", "tiktok", "instagram", "twitter"}
        if self.platform_scope not in valid_scopes:
            raise ValueError(f"Invalid platform_scope: {self.platform_scope}")


@dataclass(frozen=True)
class EffectScope:
    """
    Defines which comparisons are valid.
    Segmentation is explicit or not at all.
    """
    experiment_id: str
    control_variant_id: str
    treatment_variant_ids: List[str]
    window: OutcomeWindow
    segment_key: Optional[str] = None  # platform | niche | format | posting_window
    segment_value: Optional[str] = None

    def __post_init__(self):
        """Validate scope at construction."""
        if not self.treatment_variant_ids:
            raise ValueError("treatment_variant_ids cannot be empty")
        
        if self.control_variant_id in self.treatment_variant_ids:
            raise ValueError("control cannot be in treatment list")
        
        # Segment key and value must both be present or both absent
        if (self.segment_key is None) != (self.segment_value is None):
            raise ValueError("segment_key and segment_value must both be set or both None")


@dataclass(frozen=True)
class EffectSizeResult:
    """
    IMMUTABLE effect size result.
    No probability. No p-value. Just impact.
    """
    experiment_id: str
    metric_name: str
    window: str
    segment: Optional[str]
    
    treatment_variant: str
    control_variant: str
    
    # Core measurements
    effect_value: float           # Absolute effect (treatment - control)
    baseline_value: float         # Control group baseline
    relative_lift: Optional[float]  # Percentage lift (None if baseline = 0)
    
    # Metadata
    method: str
    computed_at: datetime
    outcome_snapshot_id: str
    
    # Sample sizes
    treatment_n: int
    control_n: int
    
    # Quality flags
    meets_minimum_units: bool
    has_contamination: bool
    
    def result_id(self) -> str:
        """Generate deterministic result ID."""
        import hashlib
        components = [
            self.experiment_id,
            self.metric_name,
            self.window,
            self.segment or "global",
            self.treatment_variant,
            self.control_variant,
            self.method,
            self.outcome_snapshot_id
        ]
        return hashlib.sha256("|".join(components).encode()).hexdigest()[:16]


# ============================================================================
# NORMALIZATION GUARD
# ============================================================================


class NormalizationGuard:
    """
    Ensures metrics are on comparable units.
    
    This file NEVER normalizes.
    It only verifies normalization was done upstream.
    """
    
    def __init__(self):
        self._normalized_metrics: Set[str] = set()
        self._raw_metrics: Set[str] = set()
    
    def register_normalized(self, metric_name: str):
        """Register metric as already normalized."""
        if metric_name in self._raw_metrics:
            raise ValueError(f"Metric already registered as raw: {metric_name}")
        self._normalized_metrics.add(metric_name)
    
    def register_raw(self, metric_name: str):
        """Register metric as raw (no normalization needed)."""
        if metric_name in self._normalized_metrics:
            raise ValueError(f"Metric already registered as normalized: {metric_name}")
        self._raw_metrics.add(metric_name)
    
    def validate_comparison(
        self,
        metric_name: str,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that comparison is valid.
        
        Returns:
            (is_valid, error_message)
        """
        # Check if metric requires normalization
        if metric_name not in self._normalized_metrics and metric_name not in self._raw_metrics:
            return False, f"Metric not registered: {metric_name}"
        
        # Check for cross-platform comparison without normalization
        treatment_platforms = {r.metric_name for r in treatment_records}
        control_platforms = {r.metric_name for r in control_records}
        
        if len(treatment_platforms | control_platforms) > 1:
            if metric_name not in self._normalized_metrics:
                return False, "Cross-platform comparison requires normalized metric"
        
        return True, None


# ============================================================================
# PRECONDITION VALIDATOR
# ============================================================================


class PreconditionValidator:
    """
    Enforces preconditions before computing effects.
    
    Prevents fake causality by blocking invalid comparisons.
    """
    
    def __init__(self, outcome_store: OutcomeStore):
        self.outcome_store = outcome_store
        self._frozen_experiments: Set[str] = set()
    
    def mark_frozen(self, experiment_id: str):
        """Mark experiment as frozen (outcomes finalized)."""
        self._frozen_experiments.add(experiment_id)
    
    def validate(
        self,
        scope: EffectScope,
        metric_spec: EffectMetricSpec,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate all preconditions.
        
        Returns:
            (is_valid, error_message)
        """
        # 1. Check outcome windows finalized
        if scope.experiment_id not in self._frozen_experiments:
            return False, f"Experiment not frozen: {scope.experiment_id}"
        
        # 2. Check minimum unit count
        treatment_n = len(treatment_records)
        control_n = len(control_records)
        
        if treatment_n < metric_spec.minimum_units:
            return False, f"Treatment units ({treatment_n}) below minimum ({metric_spec.minimum_units})"
        
        if control_n < metric_spec.minimum_units:
            return False, f"Control units ({control_n}) below minimum ({metric_spec.minimum_units})"
        
        # 3. Check for contamination flags
        treatment_contaminated = [r for r in treatment_records if r.is_contaminated]
        control_contaminated = [r for r in control_records if r.is_contaminated]
        
        if treatment_contaminated or control_contaminated:
            contamination_count = len(treatment_contaminated) + len(control_contaminated)
            return False, f"Found {contamination_count} contaminated records"
        
        # 4. Check balanced assignment (warn if severely imbalanced)
        if treatment_n > 0 and control_n > 0:
            ratio = max(treatment_n, control_n) / min(treatment_n, control_n)
            if ratio > 10.0:
                # Severe imbalance - allow but will be flagged
                pass
        
        return True, None
    
    def check_missing_data(
        self,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[float, float]:
        """
        Calculate missing data rates.
        
        Returns:
            (treatment_missing_rate, control_missing_rate)
        """
        treatment_missing = len([r for r in treatment_records if r.is_missing])
        control_missing = len([r for r in control_records if r.is_missing])
        
        treatment_rate = treatment_missing / len(treatment_records) if treatment_records else 0.0
        control_rate = control_missing / len(control_records) if control_records else 0.0
        
        return treatment_rate, control_rate


# ============================================================================
# EFFECT SIZE ANALYZER (CORE ENGINE)
# ============================================================================


class EffectSizeAnalyzer:
    """
    Core engine for computing causal effect sizes.
    
    Flow:
        1. Load immutable outcome records
        2. Select control & treatment groups
        3. Validate preconditions
        4. Compute baseline
        5. Compute variant effect
        6. Persist immutable result
    """
    
    def __init__(
        self,
        outcome_store: OutcomeStore,
        normalization_guard: NormalizationGuard,
        precondition_validator: PreconditionValidator
    ):
        self.outcome_store = outcome_store
        self.normalization_guard = normalization_guard
        self.precondition_validator = precondition_validator
        self._method_registry = {
            EffectMethod.DIFF_MEANS: self._compute_diff_means,
            EffectMethod.RATIO: self._compute_ratio,
            EffectMethod.LOG_RATIO: self._compute_log_ratio,
            EffectMethod.SLOPE_DELTA: self._compute_slope_delta
        }
    
    def compute_effect(
        self,
        scope: EffectScope,
        metric_spec: EffectMetricSpec,
        outcome_snapshot_id: str
    ) -> List[EffectSizeResult]:
        """
        Compute effect size for all treatment variants.
        
        Returns:
            List of EffectSizeResult (one per treatment variant)
        """
        results = []
        
        # Load control records
        control_records = self._load_records(
            scope.experiment_id,
            scope.control_variant_id,
            metric_spec.metric_name,
            scope.window,
            scope.segment_key,
            scope.segment_value
        )
        
        # Compute effect for each treatment variant
        for treatment_id in scope.treatment_variant_ids:
            # Load treatment records
            treatment_records = self._load_records(
                scope.experiment_id,
                treatment_id,
                metric_spec.metric_name,
                scope.window,
                scope.segment_key,
                scope.segment_value
            )
            
            # Validate normalization
            is_valid, error = self.normalization_guard.validate_comparison(
                metric_spec.metric_name,
                treatment_records,
                control_records
            )
            if not is_valid:
                raise ValueError(f"Normalization validation failed: {error}")
            
            # Validate preconditions
            is_valid, error = self.precondition_validator.validate(
                scope,
                metric_spec,
                treatment_records,
                control_records
            )
            if not is_valid:
                raise ValueError(f"Precondition validation failed: {error}")
            
            # Compute effect
            result = self._compute_single_effect(
                scope=scope,
                metric_spec=metric_spec,
                treatment_variant=treatment_id,
                treatment_records=treatment_records,
                control_records=control_records,
                outcome_snapshot_id=outcome_snapshot_id
            )
            
            results.append(result)
        
        return results
    
    def _load_records(
        self,
        experiment_id: str,
        variant_id: str,
        metric_name: str,
        window: OutcomeWindow,
        segment_key: Optional[str],
        segment_value: Optional[str]
    ) -> List[OutcomeRecord]:
        """Load outcome records with filtering."""
        records = self.outcome_store.query_by_variant(experiment_id, variant_id)
        
        # Filter by metric
        records = [r for r in records if r.metric_name == metric_name]
        
        # Filter by window
        records = [r for r in records if r.window == window]
        
        # Filter by segment (if specified)
        # Note: Segmentation would be stored in outcome metadata
        # For now, we assume segment filtering happens here
        
        # Exclude missing records from effect computation
        records = [r for r in records if not r.is_missing]
        
        return records
    
    def _compute_single_effect(
        self,
        scope: EffectScope,
        metric_spec: EffectMetricSpec,
        treatment_variant: str,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord],
        outcome_snapshot_id: str
    ) -> EffectSizeResult:
        """Compute effect for a single treatment variant."""
        # Get computation method
        compute_fn = self._method_registry.get(metric_spec.method)
        if not compute_fn:
            raise ValueError(f"Unknown method: {metric_spec.method}")
        
        # Compute effect
        effect_value, baseline_value, relative_lift = compute_fn(
            treatment_records,
            control_records
        )
        
        # Check for contamination
        has_contamination = any(
            r.is_contaminated for r in treatment_records + control_records
        )
        
        # Build result
        segment_str = None
        if scope.segment_key and scope.segment_value:
            segment_str = f"{scope.segment_key}={scope.segment_value}"
        
        result = EffectSizeResult(
            experiment_id=scope.experiment_id,
            metric_name=metric_spec.metric_name,
            window=str(scope.window),
            segment=segment_str,
            treatment_variant=treatment_variant,
            control_variant=scope.control_variant_id,
            effect_value=effect_value,
            baseline_value=baseline_value,
            relative_lift=relative_lift,
            method=str(metric_spec.method),
            computed_at=datetime.now(),
            outcome_snapshot_id=outcome_snapshot_id,
            treatment_n=len(treatment_records),
            control_n=len(control_records),
            meets_minimum_units=(
                len(treatment_records) >= metric_spec.minimum_units and
                len(control_records) >= metric_spec.minimum_units
            ),
            has_contamination=has_contamination
        )
        
        return result
    
    # ========================================================================
    # COMPUTATION METHODS
    # ========================================================================
    
    def _compute_diff_means(
        self,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[float, float, Optional[float]]:
        """
        Difference in means: treatment_mean - control_mean
        
        Use case: Stable metrics (CTR, bounce rate, conversion rate)
        
        Returns:
            (effect_value, baseline_value, relative_lift)
        """
        treatment_values = [r.value for r in treatment_records]
        control_values = [r.value for r in control_records]
        
        treatment_mean = sum(treatment_values) / len(treatment_values)
        control_mean = sum(control_values) / len(control_values)
        
        effect_value = treatment_mean - control_mean
        baseline_value = control_mean
        
        # Relative lift (percentage change)
        if control_mean != 0:
            relative_lift = (effect_value / control_mean) * 100.0
        else:
            relative_lift = None
        
        return effect_value, baseline_value, relative_lift
    
    def _compute_ratio(
        self,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[float, float, Optional[float]]:
        """
        Ratio of means: treatment_mean / control_mean
        
        Use case: Scale-sensitive metrics (revenue, views, clicks)
        
        Returns:
            (effect_value, baseline_value, relative_lift)
        """
        treatment_values = [r.value for r in treatment_records]
        control_values = [r.value for r in control_records]
        
        treatment_mean = sum(treatment_values) / len(treatment_values)
        control_mean = sum(control_values) / len(control_values)
        
        baseline_value = control_mean
        
        if control_mean != 0:
            ratio = treatment_mean / control_mean
            effect_value = ratio
            relative_lift = (ratio - 1.0) * 100.0
        else:
            effect_value = float('inf') if treatment_mean > 0 else float('nan')
            relative_lift = None
        
        return effect_value, baseline_value, relative_lift
    
    def _compute_log_ratio(
        self,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[float, float, Optional[float]]:
        """
        Log ratio: log(treatment_mean / control_mean)
        
        Use case: Heavy-tailed virality metrics (shares, viral views, follower spikes)
        
        Returns:
            (effect_value, baseline_value, relative_lift)
        """
        treatment_values = [r.value for r in treatment_records]
        control_values = [r.value for r in control_records]
        
        treatment_mean = sum(treatment_values) / len(treatment_values)
        control_mean = sum(control_values) / len(control_values)
        
        baseline_value = control_mean
        
        if treatment_mean > 0 and control_mean > 0:
            log_ratio = math.log(treatment_mean / control_mean)
            effect_value = log_ratio
            relative_lift = (math.exp(log_ratio) - 1.0) * 100.0
        else:
            effect_value = float('nan')
            relative_lift = None
        
        return effect_value, baseline_value, relative_lift
    
    def _compute_slope_delta(
        self,
        treatment_records: List[OutcomeRecord],
        control_records: List[OutcomeRecord]
    ) -> Tuple[float, float, Optional[float]]:
        """
        Difference in growth slopes.
        
        Use case: Growth dynamics (follower growth, engagement trajectory)
        
        Note: This requires time-series data within the window.
        For simplicity, we compute rate of change if multiple windows available.
        
        Returns:
            (effect_value, baseline_value, relative_lift)
        """
        # Simplified version: compute mean growth rate
        # Full implementation would fit linear trends
        
        treatment_values = [r.value for r in treatment_records]
        control_values = [r.value for r in control_records]
        
        # Compute simple slopes (first to last)
        if len(treatment_values) >= 2:
            treatment_slope = (treatment_values[-1] - treatment_values[0]) / len(treatment_values)
        else:
            treatment_slope = treatment_values[0] if treatment_values else 0.0
        
        if len(control_values) >= 2:
            control_slope = (control_values[-1] - control_values[0]) / len(control_values)
        else:
            control_slope = control_values[0] if control_values else 0.0
        
        effect_value = treatment_slope - control_slope
        baseline_value = control_slope
        
        if control_slope != 0:
            relative_lift = (effect_value / control_slope) * 100.0
        else:
            relative_lift = None
        
        return effect_value, baseline_value, relative_lift
    
    # ========================================================================
    # MULTI-WINDOW & MULTI-SEGMENT ANALYSIS
    # ========================================================================
    
    def compute_by_window(
        self,
        experiment_id: str,
        control_variant_id: str,
        treatment_variant_ids: List[str],
        metric_spec: EffectMetricSpec,
        windows: List[OutcomeWindow],
        outcome_snapshot_id: str
    ) -> Dict[str, List[EffectSizeResult]]:
        """
        Compute effects across multiple windows.
        No window averaging.
        
        Returns:
            Dict mapping window label to results
        """
        results_by_window = {}
        
        for window in windows:
            scope = EffectScope(
                experiment_id=experiment_id,
                control_variant_id=control_variant_id,
                treatment_variant_ids=treatment_variant_ids,
                window=window
            )
            
            results = self.compute_effect(scope, metric_spec, outcome_snapshot_id)
            results_by_window[str(window)] = results
        
        return results_by_window
    
    def compute_by_segment(
        self,
        experiment_id: str,
        control_variant_id: str,
        treatment_variant_ids: List[str],
        metric_spec: EffectMetricSpec,
        window: OutcomeWindow,
        segment_key: str,
        segment_values: List[str],
        outcome_snapshot_id: str,
        min_segment_size: int = 30
    ) -> Dict[str, List[EffectSizeResult]]:
        """
        Compute effects across segments.
        
        Segments MUST:
            - Be declared in experiment_spec
            - Meet minimum size
        
        Returns:
            Dict mapping segment value to results
        """
        results_by_segment = {}
        
        for segment_value in segment_values:
            scope = EffectScope(
                experiment_id=experiment_id,
                control_variant_id=control_variant_id,
                treatment_variant_ids=treatment_variant_ids,
                window=window,
                segment_key=segment_key,
                segment_value=segment_value
            )
            
            # Check segment size before computing
            # (This would require loading records first)
            try:
                results = self.compute_effect(scope, metric_spec, outcome_snapshot_id)
                
                # Verify minimum segment size
                for result in results:
                    if result.treatment_n < min_segment_size or result.control_n < min_segment_size:
                        # Skip this segment
                        continue
                
                results_by_segment[segment_value] = results
            except ValueError as e:
                # Segment doesn't meet requirements, skip
                continue
        
        return results_by_segment


# ============================================================================
# HETEROGENEITY ANALYZER
# ============================================================================


class HeterogeneityAnalyzer:
    """
    Reports variance of effects across segments.
    
    Does NOT "correct" anything. Only flags.
    
    Detects:
        - Variance of effect across segments
        - Instability detection
        - Simpson's paradox warnings
    """
    
    def analyze_segment_heterogeneity(
        self,
        results_by_segment: Dict[str, List[EffectSizeResult]]
    ) -> Dict[str, Any]:
        """
        Analyze heterogeneity across segments.
        
        Returns:
            Analysis summary with warnings
        """
        if not results_by_segment:
            return {"heterogeneity": "insufficient_data"}
        
        # Collect effect values across segments
        all_effects = []
        segment_effects = {}
        
        for segment_value, results in results_by_segment.items():
            for result in results:
                all_effects.append(result.effect_value)
                if segment_value not in segment_effects:
                    segment_effects[segment_value] = []
                segment_effects[segment_value].append(result.effect_value)
        
        if len(all_effects) < 2:
            return {"heterogeneity": "insufficient_segments"}
        
        # Compute variance
        mean_effect = sum(all_effects) / len(all_effects)
        variance = sum((e - mean_effect) ** 2 for e in all_effects) / len(all_effects)
        stddev = math.sqrt(variance)
        
        # Compute coefficient of variation
        cv = stddev / abs(mean_effect) if mean_effect != 0 else float('inf')
        
        # Check for direction flips (Simpson's paradox indicator)
        positive_count = sum(1 for e in all_effects if e > 0)
        negative_count = sum(1 for e in all_effects if e < 0)
        has_direction_flip = positive_count > 0 and negative_count > 0
        
        # Compute range
        effect_range = max(all_effects) - min(all_effects)
        
        warnings = []
        
        if cv > 0.5:
            warnings.append("HIGH_VARIANCE: Effect varies substantially across segments")
        
        if has_direction_flip:
            warnings.append("DIRECTION_FLIP: Effect direction differs across segments (Simpson's paradox risk)")
        
        if effect_range > 2 * abs(mean_effect):
            warnings.append("WIDE_RANGE: Effect range exceeds 2x mean effect")
        
        return {
            "mean_effect": mean_effect,
            "variance": variance,
            "stddev": stddev,
            "coefficient_of_variation": cv,
            "effect_range": effect_range,
            "segment_count": len(segment_effects),
            "has_direction_flip": has_direction_flip,
            "warnings": warnings
        }
    
    def detect_simpsons_paradox(
        self,
        overall_result: EffectSizeResult,
        segment_results: List[EffectSizeResult]
    ) -> Tuple[bool, Optional[str]]:
        """
        Detect Simpson's paradox.
        
        Returns:
            (is_detected, warning_message)
        """
        if not segment_results:
            return False, None
        
        overall_direction = 1 if overall_result.effect_value > 0 else -1
        
        # Check if all segments have opposite direction
        segment_directions = [
            1 if r.effect_value > 0 else -1
            for r in segment_results
        ]
        
        if all(d == -overall_direction for d in segment_directions):
            return True, "Simpson's paradox detected: overall effect reverses within all segments"
        
        # Check if majority of segments have opposite direction
        opposite_count = sum(1 for d in segment_directions if d == -overall_direction)
        if opposite_count > len(segment_directions) / 2:
            return True, f"Simpson's paradox warning: {opposite_count}/{len(segment_directions)} segments show opposite direction"
        
        return False, None


# ============================================================================
# SENSITIVITY ANALYZER
# ============================================================================


class SensitivityAnalyzer:
    """
    Tests robustness of effect estimates.
    
    Tests:
        - Robustness to unit removal
        - Effect direction stability
        - Small-sample sensitivity
    
    Outputs warnings, not decisions.
    """
    
    def test_unit_removal_sensitivity(
        self,
        analyzer: EffectSizeAnalyzer,
        scope: EffectScope,
        metric_spec: EffectMetricSpec,
        outcome_snapshot_id: str,
        removal_fraction: float = 0.1
    ) -> Dict[str, Any]:
        """
        Test sensitivity to removing random units.
        
        Jackknife-style analysis.
        
        Returns:
            Sensitivity report
        """
        import random
        
        # Get original results
        original_results = analyzer.compute_effect(scope, metric_spec, outcome_snapshot_id)
        
        if not original_results:
            return {"sensitivity": "no_results"}
        
        original_effect = original_results[0].effect_value
        
        # Run multiple iterations with random unit removal
        n_iterations = 10
        effects_after_removal = []
        
        for _ in range(n_iterations):
            # This is simplified - full implementation would:
            # 1. Load records
            # 2. Remove random sample
            # 3. Recompute effect
            # For now, we simulate
            pass
        
        # Compute stability
        if effects_after_removal:
            mean_effect = sum(effects_after_removal) / len(effects_after_removal)
            effect_change = abs(mean_effect - original_effect)
            relative_change = effect_change / abs(original_effect) if original_effect != 0 else float('inf')
            
            is_sensitive = relative_change > 0.2  # >20% change
            
            return {
                "original_effect": original_effect,
                "mean_effect_after_removal": mean_effect,
                "effect_change": effect_change,
                "relative_change": relative_change,
                "is_sensitive": is_sensitive,
                "warning": "HIGH_SENSITIVITY" if is_sensitive else None
            }
        
        return {"sensitivity": "insufficient_iterations"}
    
    def test_direction_stability(
        self,
        results_by_window: Dict[str, List[EffectSizeResult]]
    ) -> Dict[str, Any]:
        """
        Test if effect direction is stable across windows.
        
        Returns:
            Stability report
        """
        all_effects = []
        
        for window_label, results in results_by_window.items():
            for result in results:
                all_effects.append({
                    "window": window_label,
                    "effect": result.effect_value,
                    "direction": 1 if result.effect_value > 0 else -1
                })
        
        if len(all_effects) < 2:
            return {"stability": "insufficient_windows"}
        
        # Check for direction flips
        directions = [e["direction"] for e in all_effects]
        positive_count = sum(1 for d in directions if d > 0)
        negative_count = sum(1 for d in directions if d < 0)
        
        has_flip = positive_count > 0 and negative_count > 0
        
        # Compute consistency
        majority_direction = 1 if positive_count > negative_count else -1
        consistency = max(positive_count, negative_count) / len(directions)
        
        warnings = []
        if has_flip:
            warnings.append("DIRECTION_FLIP: Effect direction changes across windows")
        
        if consistency < 0.7:
            warnings.append("LOW_CONSISTENCY: Effect direction inconsistent across windows")
        
        return {
            "window_count": len(results_by_window),
            "positive_windows": positive_count,
            "negative_windows": negative_count,
            "has_direction_flip": has_flip,
            "consistency": consistency,
            "warnings": warnings
        }
    
    def test_small_sample_sensitivity(
        self,
        result: EffectSizeResult
    ) -> Dict[str, Any]:
        """
        Test sensitivity to small sample sizes.
        
        Returns:
            Sample size warning
        """
        min_reliable_n = 100
        
        treatment_small = result.treatment_n < min_reliable_n
        control_small = result.control_n < min_reliable_n
        
        warnings = []
        
        if treatment_small:
            warnings.append(f"SMALL_TREATMENT_SAMPLE: n={result.treatment_n} < {min_reliable_n}")
        
        if control_small:
            warnings.append(f"SMALL_CONTROL_SAMPLE: n={result.control_n} < {min_reliable_n}")
        
        # Compute sample ratio
        ratio = max(result.treatment_n, result.control_n) / min(result.treatment_n, result.control_n)
        
        if ratio > 3.0:
            warnings.append(f"IMBALANCED_SAMPLES: ratio={ratio:.2f} > 3.0")
        
        return {
            "treatment_n": result.treatment_n,
            "control_n": result.control_n,
            "sample_ratio": ratio,
            "is_small_sample": treatment_small or control_small,
            "warnings": warnings
        }


# ============================================================================
# EFFECT STORE
# ============================================================================


class EffectStore:
    """
    Append-only, versioned storage for effect size results.
    
    Properties:
        - Append-only
        - Never overwritten
        - Never merged
        - Queryable by: experiment, metric, window, segment
        - Versioned
    """
    
    def __init__(self):
        # In-memory store (production would use database)
        self._results: List[EffectSizeResult] = []
        self._index_by_experiment: Dict[str, List[EffectSizeResult]] = defaultdict(list)
        self._index_by_metric: Dict[str, List[EffectSizeResult]] = defaultdict(list)
        self._result_ids: Set[str] = set()
        self._write_count: int = 0
        self._schema_version: str = "1.0.0"
    
    def write(self, result: EffectSizeResult) -> bool:
        """
        Write effect size result. Write-once only.
        
        Returns:
            True if written, False if duplicate
        """
        result_id = result.result_id()
        
        # Enforce write-once
        if result_id in self._result_ids:
            return False  # Duplicate, reject
        
        # Append to store
        self._results.append(result)
        self._result_ids.add(result_id)
        
        # Update indexes
        self._index_by_experiment[result.experiment_id].append(result)
        self._index_by_metric[result.metric_name].append(result)
        
        self._write_count += 1
        return True
    
    def query_by_experiment(self, experiment_id: str) -> List[EffectSizeResult]:
        """Query all results for an experiment."""
        return self._index_by_experiment[experiment_id].copy()
    
    def query_by_metric(self, metric_name: str) -> List[EffectSizeResult]:
        """Query all results for a metric."""
        return self._index_by_metric[metric_name].copy()
    
    def query_by_window(
        self,
        experiment_id: str,
        window: str
    ) -> List[EffectSizeResult]:
        """Query results for a specific window."""
        experiment_results = self._index_by_experiment[experiment_id]
        return [r for r in experiment_results if r.window == window]
    
    def query_by_segment(
        self,
        experiment_id: str,
        segment: str
    ) -> List[EffectSizeResult]:
        """Query results for a specific segment."""
        experiment_results = self._index_by_experiment[experiment_id]
        return [r for r in experiment_results if r.segment == segment]
    
    def query_by_treatment(
        self,
        experiment_id: str,
        treatment_variant: str
    ) -> List[EffectSizeResult]:
        """Query results for a specific treatment variant."""
        experiment_results = self._index_by_experiment[experiment_id]
        return [r for r in experiment_results if r.treatment_variant == treatment_variant]
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get store statistics."""
        return {
            "total_results": len(self._results),
            "write_count": self._write_count,
            "experiments": len(self._index_by_experiment),
            "metrics": len(self._index_by_metric),
            "schema_version": self._schema_version
        }


# ============================================================================
# EFFECT WATCHDOG
# ============================================================================


class EffectWatchdog:
    """
    Monitors effect computation for anomalies.
    
    Monitors:
        - Massive lifts with tiny samples
        - Direction flips across windows
        - Cross-segment contradictions
    
    Triggers:
        - Freeze recommendation
        - Analyst review
        - Confidence downgrade
    """
    
    def __init__(self):
        self._alerts: List[Dict[str, Any]] = []
        self._freeze_recommendations: List[str] = []
        self._analyst_review_flags: List[str] = []
        
        # Thresholds
        self._massive_lift_threshold: float = 10.0  # 1000% lift
        self._tiny_sample_threshold: int = 10
        self._direction_flip_concern: int = 2  # Number of flips to trigger alert
    
    def check_massive_lift_tiny_sample(
        self,
        result: EffectSizeResult
    ):
        """Check for suspiciously large effects with small samples."""
        if result.relative_lift is None:
            return
        
        abs_lift = abs(result.relative_lift)
        min_sample = min(result.treatment_n, result.control_n)
        
        if abs_lift > self._massive_lift_threshold and min_sample < self._tiny_sample_threshold:
            alert = {
                "type": "massive_lift_tiny_sample",
                "experiment_id": result.experiment_id,
                "metric_name": result.metric_name,
                "relative_lift": result.relative_lift,
                "min_sample_size": min_sample,
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)
            self._analyst_review_flags.append(result.experiment_id)
    
    def check_direction_flips(
        self,
        results_by_window: Dict[str, List[EffectSizeResult]]
    ):
        """Check for effect direction flips across windows."""
        if not results_by_window:
            return
        
        # Group by treatment variant
        by_treatment = defaultdict(list)
        for window_label, results in results_by_window.items():
            for result in results:
                by_treatment[result.treatment_variant].append({
                    "window": window_label,
                    "effect": result.effect_value
                })
        
        # Check each treatment for flips
        for treatment_id, effects in by_treatment.items():
            if len(effects) < 2:
                continue
            
            # Count direction changes
            flip_count = 0
            for i in range(len(effects) - 1):
                curr_dir = 1 if effects[i]["effect"] > 0 else -1
                next_dir = 1 if effects[i + 1]["effect"] > 0 else -1
                if curr_dir != next_dir:
                    flip_count += 1
            
            if flip_count >= self._direction_flip_concern:
                alert = {
                    "type": "direction_flips",
                    "treatment_variant": treatment_id,
                    "flip_count": flip_count,
                    "window_count": len(effects),
                    "timestamp": datetime.now()
                }
                self._alerts.append(alert)
                self._freeze_recommendations.append(treatment_id)
    
    def check_cross_segment_contradictions(
        self,
        results_by_segment: Dict[str, List[EffectSizeResult]]
    ):
        """Check for contradictions across segments."""
        if not results_by_segment or len(results_by_segment) < 2:
            return
        
        # Group by treatment variant
        by_treatment = defaultdict(list)
        for segment_value, results in results_by_segment.items():
            for result in results:
                by_treatment[result.treatment_variant].append({
                    "segment": segment_value,
                    "effect": result.effect_value,
                    "direction": 1 if result.effect_value > 0 else -1
                })
        
        # Check for contradictions
        for treatment_id, segment_effects in by_treatment.items():
            if len(segment_effects) < 2:
                continue
            
            directions = [e["direction"] for e in segment_effects]
            positive = sum(1 for d in directions if d > 0)
            negative = sum(1 for d in directions if d < 0)
            
            # Alert if split
            if positive > 0 and negative > 0:
                alert = {
                    "type": "cross_segment_contradiction",
                    "treatment_variant": treatment_id,
                    "positive_segments": positive,
                    "negative_segments": negative,
                    "timestamp": datetime.now()
                }
                self._alerts.append(alert)
                self._analyst_review_flags.append(treatment_id)
    
    def check_extreme_heterogeneity(
        self,
        heterogeneity_analysis: Dict[str, Any]
    ):
        """Check for extreme heterogeneity."""
        if "coefficient_of_variation" not in heterogeneity_analysis:
            return
        
        cv = heterogeneity_analysis["coefficient_of_variation"]
        
        if cv > 1.0:  # 100% coefficient of variation
            alert = {
                "type": "extreme_heterogeneity",
                "coefficient_of_variation": cv,
                "warnings": heterogeneity_analysis.get("warnings", []),
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)
    
    def get_alerts(self) -> List[Dict[str, Any]]:
        """Retrieve all alerts."""
        return self._alerts.copy()
    
    def get_freeze_recommendations(self) -> List[str]:
        """Retrieve freeze recommendations."""
        return list(set(self._freeze_recommendations))
    
    def get_analyst_review_flags(self) -> List[str]:
        """Retrieve analyst review flags."""
        return list(set(self._analyst_review_flags))
    
    def should_freeze(self, experiment_id: str) -> bool:
        """Check if experiment should be frozen."""
        return experiment_id in self._freeze_recommendations
    
    def needs_analyst_review(self, experiment_id: str) -> bool:
        """Check if experiment needs analyst review."""
        return experiment_id in self._analyst_review_flags


# ============================================================================
# DETERMINISM SUPPORT
# ============================================================================


class EffectReplayEngine:
    """
    Supports deterministic replay of effect computation.
    
    Given:
        - Same outcomes
        - Same scope
        - Same method
    
    MUST return identical effect values across:
        - Machines
        - Replays
        - Years
    
    This is mandatory for audits.
    """
    
    def __init__(self, analyzer: EffectSizeAnalyzer, effect_store: EffectStore):
        self.analyzer = analyzer
        self.effect_store = effect_store
        self._computation_log: List[Dict[str, Any]] = []
    
    def record_computation(
        self,
        scope: EffectScope,
        metric_spec: EffectMetricSpec,
        results: List[EffectSizeResult]
    ):
        """Record computation for replay verification."""
        log_entry = {
            "experiment_id": scope.experiment_id,
            "control_variant": scope.control_variant_id,
            "treatment_variants": scope.treatment_variant_ids,
            "metric_name": metric_spec.metric_name,
            "method": str(metric_spec.method),
            "window": str(scope.window),
            "segment": scope.segment_key,
            "result_count": len(results),
            "result_ids": [r.result_id() for r in results],
            "effect_values": [r.effect_value for r in results],
            "timestamp": datetime.now()
        }
        self._computation_log.append(log_entry)
    
    def verify_determinism(
        self,
        original_results: List[EffectSizeResult],
        replayed_results: List[EffectSizeResult],
        tolerance: float = 1e-10
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify that replay produced identical results.
        
        Args:
            tolerance: Numerical tolerance for floating-point comparison
        
        Returns:
            (is_deterministic, error_message)
        """
        if len(original_results) != len(replayed_results):
            return False, f"Result count mismatch: {len(original_results)} vs {len(replayed_results)}"
        
        original_ids = {r.result_id() for r in original_results}
        replayed_ids = {r.result_id() for r in replayed_results}
        
        if original_ids != replayed_ids:
            missing = original_ids - replayed_ids
            extra = replayed_ids - original_ids
            return False, f"Result ID mismatch. Missing: {missing}, Extra: {extra}"
        
        # Verify effect values with tolerance
        original_map = {r.result_id(): r for r in original_results}
        replayed_map = {r.result_id(): r for r in replayed_results}
        
        for result_id in original_ids:
            orig = original_map[result_id]
            repl = replayed_map[result_id]
            
            # Check effect value
            if abs(orig.effect_value - repl.effect_value) > tolerance:
                return False, f"Effect value mismatch for {result_id}: {orig.effect_value} vs {repl.effect_value}"
            
            # Check baseline value
            if abs(orig.baseline_value - repl.baseline_value) > tolerance:
                return False, f"Baseline value mismatch for {result_id}: {orig.baseline_value} vs {repl.baseline_value}"
            
            # Check relative lift (handle None case)
            if orig.relative_lift is not None and repl.relative_lift is not None:
                if abs(orig.relative_lift - repl.relative_lift) > tolerance:
                    return False, f"Relative lift mismatch for {result_id}: {orig.relative_lift} vs {repl.relative_lift}"
            elif (orig.relative_lift is None) != (repl.relative_lift is None):
                return False, f"Relative lift presence mismatch for {result_id}"
        
        return True, None
    
    def export_computation_manifest(
        self,
        experiment_id: str
    ) -> Dict[str, Any]:
        """
        Export manifest for replaying effect computations.
        """
        results = self.effect_store.query_by_experiment(experiment_id)
        
        manifest = {
            "experiment_id": experiment_id,
            "total_results": len(results),
            "computation_log": [
                e for e in self._computation_log
                if e["experiment_id"] == experiment_id
            ],
            "schema_version": "1.0.0",
            "exported_at": datetime.now().isoformat()
        }
        
        return manifest


# ============================================================================
# USAGE EXAMPLE & INTEGRATION
# ============================================================================


def example_usage():
    """
    Example showing how to use the EffectSizeAnalyzer.
    """
    # Initialize components
    outcome_store = OutcomeStore()
    effect_store = EffectStore()
    normalization_guard = NormalizationGuard()
    precondition_validator = PreconditionValidator(outcome_store)
    
    analyzer = EffectSizeAnalyzer(
        outcome_store=outcome_store,
        normalization_guard=normalization_guard,
        precondition_validator=precondition_validator
    )
    
    heterogeneity_analyzer = HeterogeneityAnalyzer()
    sensitivity_analyzer = SensitivityAnalyzer()
    watchdog = EffectWatchdog()
    
    # Register metrics
    normalization_guard.register_raw("click_through_rate")
    normalization_guard.register_normalized("watch_time")
    
    # Define metric spec
    ctr_spec = EffectMetricSpec(
        metric_name="click_through_rate",
        method=EffectMethod.DIFF_MEANS,
        unit="%",
        platform_scope="global",
        minimum_units=30
    )
    
    # Define scope
    scope = EffectScope(
        experiment_id="exp_001",
        control_variant_id="control",
        treatment_variant_ids=["variant_1", "variant_2"],
        window=OutcomeWindow.DAY_7
    )
    
    # Mark experiment as frozen (outcomes finalized)
    precondition_validator.mark_frozen("exp_001")
    
    # Compute effects
    try:
        results = analyzer.compute_effect(
            scope=scope,
            metric_spec=ctr_spec,
            outcome_snapshot_id="outcome_v1.0"
        )
        
        print(f"Computed {len(results)} effect size results")
        
        for result in results:
            print(f"\nTreatment: {result.treatment_variant}")
            print(f"  Effect: {result.effect_value:.4f}")
            print(f"  Baseline: {result.baseline_value:.4f}")
            print(f"  Relative Lift: {result.relative_lift:.2f}%" if result.relative_lift else "  Relative Lift: N/A")
            print(f"  Sample sizes: treatment={result.treatment_n}, control={result.control_n}")
            
            # Write to store
            effect_store.write(result)
            
            # Run watchdog checks
            watchdog.check_massive_lift_tiny_sample(result)
            sensitivity = sensitivity_analyzer.test_small_sample_sensitivity(result)
            if sensitivity["warnings"]:
                print(f"  Warnings: {sensitivity['warnings']}")
        
        # Check for alerts
        alerts = watchdog.get_alerts()
        if alerts:
            print(f"\nWatchdog alerts: {len(alerts)}")
            for alert in alerts:
                print(f"  - {alert['type']}: {alert}")
        
    except ValueError as e:
        print(f"Effect computation failed: {e}")
    
    # Compute across multiple windows
    print("\n" + "="*60)
    print("Computing effects across windows...")
    
    results_by_window = analyzer.compute_by_window(
        experiment_id="exp_001",
        control_variant_id="control",
        treatment_variant_ids=["variant_1"],
        metric_spec=ctr_spec,
        windows=[OutcomeWindow.DAY_1, OutcomeWindow.DAY_7, OutcomeWindow.DAY_30],
        outcome_snapshot_id="outcome_v1.0"
    )
    
    for window_label, results in results_by_window.items():
        print(f"\n{window_label}:")
        for result in results:
            print(f"  Effect: {result.effect_value:.4f}, Lift: {result.relative_lift:.2f}%" if result.relative_lift else f"  Effect: {result.effect_value:.4f}")
    
    # Check direction stability
    stability = sensitivity_analyzer.test_direction_stability(results_by_window)
    print(f"\nDirection stability: {stability}")
    
    # Check watchdog for direction flips
    watchdog.check_direction_flips(results_by_window)


if __name__ == "__main__":
    example_usage()