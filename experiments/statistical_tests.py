"""
/experiments/statistical_tests.py

Significance, Power & False-Positive Control

PURPOSE:
    Answer exactly one question:
    "Given a measured effect, how confident are we that it is not random?"

NOT:
    - Is the effect big?
    - Is the effect good?
    - Should we ship?

Just: is it distinguishable from chance?

RESPONSIBILITIES:
    ✓ Run statistical tests precisely
    ✓ Compute p-values correctly
    ✓ Apply multiple testing corrections
    ✓ Calculate statistical power
    ✓ Check assumptions
    ✓ Test robustness
    ✓ Persist immutable results

NON-RESPONSIBILITIES (NEVER DO):
    ✗ Decide rollout
    ✗ Compare effect sizes
    ✗ Normalize outcomes
    ✗ Smooth metrics
    ✗ Hide assumption violations
    ✗ Auto-choose tests
    ✗ Interpret business meaning

CORE PRINCIPLE:
    Statistical significance is about uncertainty, not importance.
    A tiny but real effect ≠ valuable
    A huge but noisy effect ≠ trustworthy
    This file separates signal from coincidence.

DEPENDENCY DIRECTION:
    outcome_collector.py → effect_size_analyzer.py → statistical_tests.py → confidence_estimator.py
    ONE-WAY ONLY. This file NEVER reaches "up".
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
import math
import random


# Import from upstream dependencies
from outcome_collector import OutcomeRecord, OutcomeWindow, OutcomeStore
from effect_size_analyzer import EffectSizeResult, EffectStore, EffectScope


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


class TestType(Enum):
    """
    Allowed statistical tests.
    No exotic stats. Predictability > cleverness.
    """
    T_TEST = "t_test"                    # Approximately normal metrics
    MANN_WHITNEY = "mann_whitney"        # Skewed distributions
    BOOTSTRAP = "bootstrap"              # Heavy-tailed virality
    PERMUTATION = "permutation"          # Distribution-free

    def __str__(self):
        return self.value


class CorrectionMethod(Enum):
    """Multiple testing correction methods."""
    NONE = "none"
    BONFERRONI = "bonferroni"
    HOLM = "holm"
    BENJAMINI_HOCHBERG = "benjamini_hochberg"  # FDR control

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class StatisticalTestSpec:
    """
    Defines exactly which test is allowed.
    No auto-test selection. No silent substitution.
    """
    metric_name: str
    test_type: TestType
    alpha: float                      # Significance level (e.g., 0.05)
    two_sided: bool                   # Two-sided vs one-sided test
    minimum_power: float              # Minimum acceptable power (e.g., 0.8)
    correction_group: str             # Family name for multiple testing

    def __post_init__(self):
        """Validate spec at construction."""
        if not 0 < self.alpha < 1:
            raise ValueError(f"alpha must be in (0, 1), got {self.alpha}")
        
        if not 0 < self.minimum_power < 1:
            raise ValueError(f"minimum_power must be in (0, 1), got {self.minimum_power}")


@dataclass(frozen=True)
class TestAssumptions:
    """
    Tracks what must hold for test validity.
    If violated → flagged, not ignored.
    """
    independence: bool                # Observations independent
    normality_required: bool          # Requires normal distribution
    equal_variance: bool              # Requires equal variance
    sample_size_minimum: int          # Minimum sample size needed

    def all_met(self) -> bool:
        """Check if all assumptions are met."""
        # Independence is always required
        if not self.independence:
            return False
        return True


@dataclass(frozen=True)
class TestResult:
    """
    IMMUTABLE test result.
    No decisions. No "pass/fail" flags.
    """
    experiment_id: str
    metric_name: str
    window: str
    segment: Optional[str]
    
    treatment_variant: str
    control_variant: str
    
    # Test details
    test_type: str
    p_value: float                    # Raw p-value
    adjusted_p_value: Optional[float] # After multiple testing correction
    power: Optional[float]            # Statistical power (if computed)
    
    # Quality indicators
    assumptions_met: bool
    assumption_violations: List[str]
    
    # Metadata
    test_timestamp: datetime
    outcome_snapshot_id: str
    effect_snapshot_id: str
    
    # Sample info
    treatment_n: int
    control_n: int
    
    # Random seed for reproducibility
    random_seed: Optional[int] = None

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
            self.test_type,
            self.outcome_snapshot_id,
            self.effect_snapshot_id
        ]
        return hashlib.sha256("|".join(components).encode()).hexdigest()[:16]


# ============================================================================
# PRECONDITIONS VALIDATOR
# ============================================================================


class PreconditionsValidator:
    """
    Blocks tests if preconditions not met.
    Running bad tests is worse than running none.
    """
    
    def __init__(
        self,
        outcome_store: OutcomeStore,
        effect_store: EffectStore
    ):
        self.outcome_store = outcome_store
        self.effect_store = effect_store
        self._frozen_experiments: Set[str] = set()
        self._computed_effects: Set[str] = set()
    
    def mark_frozen(self, experiment_id: str):
        """Mark experiment outcomes as frozen."""
        self._frozen_experiments.add(experiment_id)
    
    def mark_effect_computed(self, effect_result_id: str):
        """Mark effect as computed."""
        self._computed_effects.add(effect_result_id)
    
    def validate(
        self,
        effect_result: EffectSizeResult,
        test_spec: StatisticalTestSpec,
        treatment_outcomes: List[OutcomeRecord],
        control_outcomes: List[OutcomeRecord]
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate all preconditions.
        
        Returns:
            (is_valid, error_message)
        """
        # 1. Check outcomes frozen
        if effect_result.experiment_id not in self._frozen_experiments:
            return False, f"Experiment not frozen: {effect_result.experiment_id}"
        
        # 2. Check effect computed
        if effect_result.result_id() not in self._computed_effects:
            return False, f"Effect not computed: {effect_result.result_id()}"
        
        # 3. Check minimum sample size
        if effect_result.treatment_n < 2 or effect_result.control_n < 2:
            return False, f"Sample size too small: treatment={effect_result.treatment_n}, control={effect_result.control_n}"
        
        # 4. Check for contamination
        if effect_result.has_contamination:
            return False, "Contamination detected in effect result"
        
        treatment_contaminated = any(r.is_contaminated for r in treatment_outcomes)
        control_contaminated = any(r.is_contaminated for r in control_outcomes)
        
        if treatment_contaminated or control_contaminated:
            return False, "Contamination detected in outcomes"
        
        # 5. Check assignment imbalance (severe cases)
        if effect_result.treatment_n > 0 and effect_result.control_n > 0:
            ratio = max(effect_result.treatment_n, effect_result.control_n) / \
                    min(effect_result.treatment_n, effect_result.control_n)
            
            if ratio > 20.0:  # Extreme imbalance
                return False, f"Severe assignment imbalance: ratio={ratio:.2f}"
        
        return True, None


# ============================================================================
# ASSUMPTION CHECKER
# ============================================================================


class AssumptionChecker:
    """
    Validates statistical assumptions.
    
    If violated:
        - Test still runs
        - Result flagged
        - Downstream confidence reduced
    
    No hidden failures.
    """
    
    def check_independence(
        self,
        treatment_outcomes: List[OutcomeRecord],
        control_outcomes: List[OutcomeRecord]
    ) -> Tuple[bool, Optional[str]]:
        """
        Check independence assumption.
        
        Returns:
            (is_met, violation_description)
        """
        # Check for duplicate unit IDs (indicates lack of independence)
        treatment_ids = [r.unit_id for r in treatment_outcomes]
        control_ids = [r.unit_id for r in control_outcomes]
        
        treatment_duplicates = len(treatment_ids) != len(set(treatment_ids))
        control_duplicates = len(control_ids) != len(set(control_ids))
        
        if treatment_duplicates or control_duplicates:
            return False, "Duplicate unit IDs detected (violates independence)"
        
        # Check for overlap between groups
        overlap = set(treatment_ids) & set(control_ids)
        if overlap:
            return False, f"Unit overlap between groups: {len(overlap)} units"
        
        return True, None
    
    def check_normality(
        self,
        values: List[float],
        threshold: float = 0.05
    ) -> Tuple[bool, Optional[str]]:
        """
        Check normality using simple tests.
        
        Returns:
            (is_met, violation_description)
        """
        if len(values) < 8:
            return False, "Sample too small for normality check"
        
        # Compute skewness and kurtosis
        n = len(values)
        mean = sum(values) / n
        
        variance = sum((x - mean) ** 2 for x in values) / n
        if variance == 0:
            return False, "Zero variance"
        
        stddev = math.sqrt(variance)
        
        # Skewness
        skewness = sum((x - mean) ** 3 for x in values) / (n * stddev ** 3)
        
        # Kurtosis (excess)
        kurtosis = sum((x - mean) ** 4 for x in values) / (n * stddev ** 4) - 3.0
        
        # Rough thresholds
        if abs(skewness) > 2.0:
            return False, f"High skewness: {skewness:.2f}"
        
        if abs(kurtosis) > 4.0:
            return False, f"High kurtosis: {kurtosis:.2f}"
        
        return True, None
    
    def check_equal_variance(
        self,
        treatment_values: List[float],
        control_values: List[float]
    ) -> Tuple[bool, Optional[str]]:
        """
        Check equal variance assumption (Levene's test approximation).
        
        Returns:
            (is_met, violation_description)
        """
        if len(treatment_values) < 2 or len(control_values) < 2:
            return False, "Insufficient sample size"
        
        # Compute variances
        treatment_mean = sum(treatment_values) / len(treatment_values)
        control_mean = sum(control_values) / len(control_values)
        
        treatment_var = sum((x - treatment_mean) ** 2 for x in treatment_values) / len(treatment_values)
        control_var = sum((x - control_mean) ** 2 for x in control_values) / len(control_values)
        
        if treatment_var == 0 or control_var == 0:
            return False, "Zero variance in one group"
        
        # Variance ratio test
        ratio = max(treatment_var, control_var) / min(treatment_var, control_var)
        
        if ratio > 4.0:  # Rule of thumb
            return False, f"Variance ratio too large: {ratio:.2f}"
        
        return True, None
    
    def check_all(
        self,
        test_type: TestType,
        treatment_outcomes: List[OutcomeRecord],
        control_outcomes: List[OutcomeRecord]
    ) -> Tuple[bool, List[str]]:
        """
        Check all relevant assumptions for test type.
        
        Returns:
            (all_met, violations)
        """
        violations = []
        
        # Independence (always required)
        is_met, violation = self.check_independence(treatment_outcomes, control_outcomes)
        if not is_met:
            violations.append(violation)
        
        treatment_values = [r.value for r in treatment_outcomes if not r.is_missing]
        control_values = [r.value for r in control_outcomes if not r.is_missing]
        
        # Test-specific assumptions
        if test_type == TestType.T_TEST:
            # Check normality
            is_met, violation = self.check_normality(treatment_values)
            if not is_met:
                violations.append(f"Treatment normality: {violation}")
            
            is_met, violation = self.check_normality(control_values)
            if not is_met:
                violations.append(f"Control normality: {violation}")
            
            # Check equal variance
            is_met, violation = self.check_equal_variance(treatment_values, control_values)
            if not is_met:
                violations.append(f"Equal variance: {violation}")
        
        all_met = len(violations) == 0
        return all_met, violations


# ============================================================================
# STATISTICAL TEST ENGINE (CORE)
# ============================================================================


class StatisticalTestEngine:
    """
    Core engine for running statistical tests.
    
    Flow:
        1. Load immutable outcomes
        2. Load corresponding effect size
        3. Validate preconditions
        4. Check assumptions
        5. Run allowed test
        6. Apply corrections
        7. Persist immutable test result
    """
    
    def __init__(
        self,
        outcome_store: OutcomeStore,
        effect_store: EffectStore,
        preconditions_validator: PreconditionsValidator,
        assumption_checker: AssumptionChecker
    ):
        self.outcome_store = outcome_store
        self.effect_store = effect_store
        self.preconditions_validator = preconditions_validator
        self.assumption_checker = assumption_checker
        
        # Test implementations
        self._test_registry = {
            TestType.T_TEST: self._run_t_test,
            TestType.MANN_WHITNEY: self._run_mann_whitney,
            TestType.BOOTSTRAP: self._run_bootstrap,
            TestType.PERMUTATION: self._run_permutation
        }
    
    def run_test(
        self,
        effect_result: EffectSizeResult,
        test_spec: StatisticalTestSpec,
        random_seed: Optional[int] = None
    ) -> TestResult:
        """
        Run statistical test on effect result.
        
        Args:
            effect_result: Computed effect size
            test_spec: Test specification
            random_seed: For reproducibility (bootstrap/permutation)
        
        Returns:
            Immutable test result
        """
        # Load outcomes
        treatment_outcomes = self._load_outcomes(
            effect_result.experiment_id,
            effect_result.treatment_variant,
            effect_result.metric_name,
            effect_result.window
        )
        
        control_outcomes = self._load_outcomes(
            effect_result.experiment_id,
            effect_result.control_variant,
            effect_result.metric_name,
            effect_result.window
        )
        
        # Validate preconditions
        is_valid, error = self.preconditions_validator.validate(
            effect_result,
            test_spec,
            treatment_outcomes,
            control_outcomes
        )
        
        if not is_valid:
            raise ValueError(f"Precondition validation failed: {error}")
        
        # Check assumptions
        assumptions_met, violations = self.assumption_checker.check_all(
            test_spec.test_type,
            treatment_outcomes,
            control_outcomes
        )
        
        # Get test function
        test_fn = self._test_registry.get(test_spec.test_type)
        if not test_fn:
            raise ValueError(f"Unknown test type: {test_spec.test_type}")
        
        # Run test
        treatment_values = [r.value for r in treatment_outcomes if not r.is_missing]
        control_values = [r.value for r in control_outcomes if not r.is_missing]
        
        p_value = test_fn(
            treatment_values,
            control_values,
            test_spec.two_sided,
            random_seed
        )
        
        # Compute power if requested
        power = self._compute_power(
            effect_result,
            treatment_values,
            control_values,
            test_spec.alpha
        )
        
        # Build result
        result = TestResult(
            experiment_id=effect_result.experiment_id,
            metric_name=effect_result.metric_name,
            window=effect_result.window,
            segment=effect_result.segment,
            treatment_variant=effect_result.treatment_variant,
            control_variant=effect_result.control_variant,
            test_type=str(test_spec.test_type),
            p_value=p_value,
            adjusted_p_value=None,  # Set by correction controller
            power=power,
            assumptions_met=assumptions_met,
            assumption_violations=violations,
            test_timestamp=datetime.now(),
            outcome_snapshot_id=effect_result.outcome_snapshot_id,
            effect_snapshot_id=effect_result.result_id(),
            treatment_n=len(treatment_values),
            control_n=len(control_values),
            random_seed=random_seed
        )
        
        return result
    
    def _load_outcomes(
        self,
        experiment_id: str,
        variant_id: str,
        metric_name: str,
        window: str
    ) -> List[OutcomeRecord]:
        """Load outcome records for a variant."""
        records = self.outcome_store.query_by_variant(experiment_id, variant_id)
        
        # Filter by metric and window
        records = [
            r for r in records
            if r.metric_name == metric_name and str(r.window) == window
        ]
        
        return records
    
    # ========================================================================
    # TEST IMPLEMENTATIONS
    # ========================================================================
    
    def _run_t_test(
        self,
        treatment_values: List[float],
        control_values: List[float],
        two_sided: bool,
        random_seed: Optional[int]
    ) -> float:
        """
        Two-sample t-test (Welch's version for unequal variances).
        
        Returns:
            p-value
        """
        n1 = len(treatment_values)
        n2 = len(control_values)
        
        if n1 < 2 or n2 < 2:
            return 1.0  # Cannot compute
        
        # Compute means
        mean1 = sum(treatment_values) / n1
        mean2 = sum(control_values) / n2
        
        # Compute variances
        var1 = sum((x - mean1) ** 2 for x in treatment_values) / (n1 - 1)
        var2 = sum((x - mean2) ** 2 for x in control_values) / (n2 - 1)
        
        # Welch's t-statistic
        se = math.sqrt(var1 / n1 + var2 / n2)
        
        if se == 0:
            return 1.0  # No variance
        
        t_stat = (mean1 - mean2) / se
        
        # Degrees of freedom (Welch-Satterthwaite)
        df = (var1 / n1 + var2 / n2) ** 2 / (
            (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
        )
        
        # Compute p-value using t-distribution approximation
        p_value = self._t_distribution_pvalue(abs(t_stat), df)
        
        if two_sided:
            p_value *= 2.0
        
        return min(p_value, 1.0)
    
    def _run_mann_whitney(
        self,
        treatment_values: List[float],
        control_values: List[float],
        two_sided: bool,
        random_seed: Optional[int]
    ) -> float:
        """
        Mann-Whitney U test (non-parametric).
        
        Returns:
            p-value
        """
        n1 = len(treatment_values)
        n2 = len(control_values)
        
        if n1 < 2 or n2 < 2:
            return 1.0
        
        # Combine and rank
        combined = [(v, 0) for v in treatment_values] + [(v, 1) for v in control_values]
        combined.sort(key=lambda x: x[0])
        
        # Assign ranks (average for ties)
        ranks = []
        i = 0
        while i < len(combined):
            j = i
            while j < len(combined) and combined[j][0] == combined[i][0]:
                j += 1
            
            avg_rank = (i + j + 1) / 2.0
            for k in range(i, j):
                ranks.append((avg_rank, combined[k][1]))
            
            i = j
        
        # Compute U statistic
        rank_sum_treatment = sum(r for r, group in ranks if group == 0)
        
        U1 = rank_sum_treatment - n1 * (n1 + 1) / 2.0
        U2 = n1 * n2 - U1
        
        U = min(U1, U2)
        
        # Normal approximation
        mean_U = n1 * n2 / 2.0
        std_U = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
        
        if std_U == 0:
            return 1.0
        
        z = (U - mean_U) / std_U
        
        # Compute p-value
        p_value = self._normal_distribution_pvalue(abs(z))
        
        if two_sided:
            p_value *= 2.0
        
        return min(p_value, 1.0)
    
    def _run_bootstrap(
        self,
        treatment_values: List[float],
        control_values: List[float],
        two_sided: bool,
        random_seed: Optional[int]
    ) -> float:
        """
        Bootstrap hypothesis test.
        
        Returns:
            p-value
        """
        if random_seed is not None:
            random.seed(random_seed)
        
        n1 = len(treatment_values)
        n2 = len(control_values)
        
        if n1 < 2 or n2 < 2:
            return 1.0
        
        # Observed difference
        obs_diff = sum(treatment_values) / n1 - sum(control_values) / n2
        
        # Pool samples under null hypothesis
        pooled = treatment_values + control_values
        
        # Bootstrap resampling
        n_bootstrap = 10000
        count_extreme = 0
        
        for _ in range(n_bootstrap):
            # Resample
            bootstrap_treatment = [random.choice(pooled) for _ in range(n1)]
            bootstrap_control = [random.choice(pooled) for _ in range(n2)]
            
            # Compute difference
            bootstrap_diff = sum(bootstrap_treatment) / n1 - sum(bootstrap_control) / n2
            
            # Count extreme values
            if two_sided:
                if abs(bootstrap_diff) >= abs(obs_diff):
                    count_extreme += 1
            else:
                if bootstrap_diff >= obs_diff:
                    count_extreme += 1
        
        p_value = count_extreme / n_bootstrap
        return p_value
    
    def _run_permutation(
        self,
        treatment_values: List[float],
        control_values: List[float],
        two_sided: bool,
        random_seed: Optional[int]
    ) -> float:
        """
        Permutation test (randomization test).
        
        Returns:
            p-value
        """
        if random_seed is not None:
            random.seed(random_seed)
        
        n1 = len(treatment_values)
        n2 = len(control_values)
        
        if n1 < 2 or n2 < 2:
            return 1.0
        
        # Observed difference
        obs_diff = sum(treatment_values) / n1 - sum(control_values) / n2
        
        # Combine all values
        all_values = treatment_values + control_values
        
        # Permutation test
        n_permutations = 10000
        count_extreme = 0
        
        for _ in range(n_permutations):
            # Shuffle
            shuffled = all_values.copy()
            random.shuffle(shuffled)
            
            # Split
            perm_treatment = shuffled[:n1]
            perm_control = shuffled[n1:]
            
            # Compute difference
            perm_diff = sum(perm_treatment) / n1 - sum(perm_control) / n2
            
            # Count extreme values
            if two_sided:
                if abs(perm_diff) >= abs(obs_diff):
                    count_extreme += 1
            else:
                if perm_diff >= obs_diff:
                    count_extreme += 1
        
        p_value = count_extreme / n_permutations
        return p_value
    
    # ========================================================================
    # HELPER FUNCTIONS
    # ========================================================================
    
    def _t_distribution_pvalue(self, t: float, df: float) -> float:
        """
        Compute p-value from t-distribution (approximation).
        
        For production: use scipy.stats.t.sf(t, df)
        """
        # Simplified approximation
        # For large df, t-distribution approaches normal
        if df > 30:
            return self._normal_distribution_pvalue(t)
        
        # Very rough approximation for small df
        # Production should use proper t-distribution
        return self._normal_distribution_pvalue(t * math.sqrt(df / (df + t**2)))
    
    def _normal_distribution_pvalue(self, z: float) -> float:
        """
        Compute one-tailed p-value from standard normal.
        
        For production: use scipy.stats.norm.sf(z)
        """
        # Error function approximation
        # P(Z > z) ≈ 0.5 * erfc(z / sqrt(2))
        
        # Simplified: using complementary error function approximation
        x = abs(z) / math.sqrt(2.0)
        
        # Abramowitz and Stegun approximation
        t = 1.0 / (1.0 + 0.3275911 * x)
        erfcx = ((((
            1.061405429 * t +
           -1.453152027) * t +
            1.421413741) * t +
           -0.284496736) * t +
            0.254829592) * t * math.exp(-x * x)
        
        p_value = 0.5 * erfcx
        return p_value
    
    def _compute_power(
        self,
        effect_result: EffectSizeResult,
        treatment_values: List[float],
        control_values: List[float],
        alpha: float
    ) -> Optional[float]:
        """
        Compute statistical power (achieved).
        
        Returns:
            Power estimate (0 to 1) or None if cannot compute
        """
        n1 = len(treatment_values)
        n2 = len(control_values)
        
        if n1 < 2 or n2 < 2:
            return None
        
        # Effect size (Cohen's d approximation)
        mean1 = sum(treatment_values) / n1
        mean2 = sum(control_values) / n2
        
        var1 = sum((x - mean1) ** 2 for x in treatment_values) / (n1 - 1)
        var2 = sum((x - mean2) ** 2 for x in control_values) / (n2 - 1)
        
        pooled_std = math.sqrt((var1 + var2) / 2.0)
        
        if pooled_std == 0:
            return None
        
        cohens_d = abs(mean1 - mean2) / pooled_std
        
        # Compute non-centrality parameter
        ncp = cohens_d * math.sqrt(n1 * n2 / (n1 + n2))
        
        # Critical value (z-score for alpha)
        z_alpha = self._inverse_normal_cdf(1.0 - alpha / 2.0)  # Two-sided
        
        # Power approximation
        z_beta = ncp - z_alpha
        power = 1.0 - self._normal_distribution_pvalue(-z_beta)
        
        return min(max(power, 0.0), 1.0)
    
    def _inverse_normal_cdf(self, p: float) -> float:
        """
        Inverse of standard normal CDF (approximation).
        
        For production: use scipy.stats.norm.ppf(p)
        """
        # Beasley-Springer-Moro algorithm (simplified)
        if p <= 0.0:
            return float('-inf')
        if p >= 1.0:
            return float('inf')
        
        # Approximation for 0.0 < p < 1.0
        if p < 0.5:
            return -self._inverse_normal_cdf(1.0 - p)
        
        # Rational approximation
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        
        c0 = 2.515517
        c1 = 0.802853
        c2 = 0.010328
        d1 = 1.432788
        d2 = 0.189269
        d3 = 0.001308
        
        z = t - (c0 + c1 * t + c2 * t**2) / (1.0 + d1 * t + d2 * t**2 + d3 * t**3)
        
        return z
    
    # ========================================================================
    # POWER ANALYSIS & MDE
    # ========================================================================
    
    def power_analysis(
        self,
        effect_result: EffectSizeResult,
        test_spec: StatisticalTestSpec
    ) -> Dict[str, Any]:
        """
        Comprehensive power analysis.
        
        Returns:
            Power analysis report
        """
        # Load outcomes
        treatment_outcomes = self._load_outcomes(
            effect_result.experiment_id,
            effect_result.treatment_variant,
            effect_result.metric_name,
            effect_result.window
        )
        
        control_outcomes = self._load_outcomes(
            effect_result.experiment_id,
            effect_result.control_variant,
            effect_result.metric_name,
            effect_result.window
        )
        
        treatment_values = [r.value for r in treatment_outcomes if not r.is_missing]
        control_values = [r.value for r in control_outcomes if not r.is_missing]
        
        # Compute achieved power
        power = self._compute_power(
            effect_result,
            treatment_values,
            control_values,
            test_spec.alpha
        )
        
        # Check if underpowered
        is_underpowered = power is not None and power < test_spec.minimum_power
        
        warnings = []
        if is_underpowered:
            warnings.append(f"UNDERPOWERED: achieved power {power:.3f} < minimum {test_spec.minimum_power}")
        
        return {
            "achieved_power": power,
            "minimum_power": test_spec.minimum_power,
            "is_underpowered": is_underpowered,
            "treatment_n": len(treatment_values),
            "control_n": len(control_values),
            "warnings": warnings
        }
    
    def minimum_detectable_effect(
        self,
        treatment_values: List[float],
        control_values: List[float],
        alpha: float,
        power: float
    ) -> float:
        """
        Compute minimum detectable effect (MDE) at given power.
        
        Args:
            treatment_values: Treatment sample
            control_values: Control sample
            alpha: Significance level
            power: Desired power
        
        Returns:
            MDE in standardized units (Cohen's d)
        """
        n1 = len(treatment_values)
        n2 = len(control_values)
        
        if n1 < 2 or n2 < 2:
            return float('inf')
        
        # Critical values
        z_alpha = self._inverse_normal_cdf(1.0 - alpha / 2.0)  # Two-sided
        z_beta = self._inverse_normal_cdf(power)
        
        # MDE formula
        mde = (z_alpha + z_beta) * math.sqrt((n1 + n2) / (n1 * n2))
        
        return mde


# ============================================================================
# MULTIPLE TESTING CONTROLLER
# ============================================================================


class MultipleTestingController:
    """
    Handles multiple testing corrections.
    
    Rules:
        - Grouping declared in experiment_spec
        - Correction mandatory for multiple metrics
        - No silent correction skipping
    """
    
    def __init__(self):
        self._correction_groups: Dict[str, List[TestResult]] = defaultdict(list)
    
    def register_result(self, result: TestResult, correction_group: str):
        """Register a test result for correction."""
        self._correction_groups[correction_group].append(result)
    
    def apply_correction(
        self,
        correction_group: str,
        method: CorrectionMethod
    ) -> List[TestResult]:
        """
        Apply multiple testing correction to a group.
        
        Returns:
            Updated results with adjusted p-values
        """
        results = self._correction_groups.get(correction_group, [])
        
        if not results:
            return []
        
        if method == CorrectionMethod.NONE:
            # No correction
            return results
        
        elif method == CorrectionMethod.BONFERRONI:
            return self._apply_bonferroni(results)
        
        elif method == CorrectionMethod.HOLM:
            return self._apply_holm(results)
        
        elif method == CorrectionMethod.BENJAMINI_HOCHBERG:
            return self._apply_benjamini_hochberg(results)
        
        else:
            raise ValueError(f"Unknown correction method: {method}")
    
    def _apply_bonferroni(self, results: List[TestResult]) -> List[TestResult]:
        """Apply Bonferroni correction."""
        m = len(results)
        
        corrected_results = []
        for result in results:
            adjusted_p = min(result.p_value * m, 1.0)
            
            # Create new result with adjusted p-value
            corrected_result = TestResult(
                experiment_id=result.experiment_id,
                metric_name=result.metric_name,
                window=result.window,
                segment=result.segment,
                treatment_variant=result.treatment_variant,
                control_variant=result.control_variant,
                test_type=result.test_type,
                p_value=result.p_value,
                adjusted_p_value=adjusted_p,
                power=result.power,
                assumptions_met=result.assumptions_met,
                assumption_violations=result.assumption_violations,
                test_timestamp=result.test_timestamp,
                outcome_snapshot_id=result.outcome_snapshot_id,
                effect_snapshot_id=result.effect_snapshot_id,
                treatment_n=result.treatment_n,
                control_n=result.control_n,
                random_seed=result.random_seed
            )
            corrected_results.append(corrected_result)
        
        return corrected_results
    
    def _apply_holm(self, results: List[TestResult]) -> List[TestResult]:
        """Apply Holm-Bonferroni correction."""
        m = len(results)
        
        # Sort by p-value
        sorted_results = sorted(results, key=lambda r: r.p_value)
        
        corrected_results = []
        for i, result in enumerate(sorted_results):
            # Holm correction: multiply by (m - i)
            adjusted_p = min(result.p_value * (m - i), 1.0)
            
            # Enforce monotonicity
            if i > 0 and adjusted_p < corrected_results[-1].adjusted_p_value:
                adjusted_p = corrected_results[-1].adjusted_p_value
            
            corrected_result = TestResult(
                experiment_id=result.experiment_id,
                metric_name=result.metric_name,
                window=result.window,
                segment=result.segment,
                treatment_variant=result.treatment_variant,
                control_variant=result.control_variant,
                test_type=result.test_type,
                p_value=result.p_value,
                adjusted_p_value=adjusted_p,
                power=result.power,
                assumptions_met=result.assumptions_met,
                assumption_violations=result.assumption_violations,
                test_timestamp=result.test_timestamp,
                outcome_snapshot_id=result.outcome_snapshot_id,
                effect_snapshot_id=result.effect_snapshot_id,
                treatment_n=result.treatment_n,
                control_n=result.control_n,
                random_seed=result.random_seed
            )
            corrected_results.append(corrected_result)
        
        return corrected_results
    
    def _apply_benjamini_hochberg(self, results: List[TestResult]) -> List[TestResult]:
        """Apply Benjamini-Hochberg FDR correction."""
        m = len(results)
        
        # Sort by p-value
        sorted_results = sorted(results, key=lambda r: r.p_value)
        
        corrected_results = []
        for i, result in enumerate(sorted_results, start=1):
            # BH correction: multiply by m/i
            adjusted_p = min(result.p_value * m / i, 1.0)
            
            # Enforce monotonicity (backwards)
            if corrected_results and adjusted_p > corrected_results[-1].adjusted_p_value:
                adjusted_p = corrected_results[-1].adjusted_p_value
            
            corrected_result = TestResult(
                experiment_id=result.experiment_id,
                metric_name=result.metric_name,
                window=result.window,
                segment=result.segment,
                treatment_variant=result.treatment_variant,
                control_variant=result.control_variant,
                test_type=result.test_type,
                p_value=result.p_value,
                adjusted_p_value=adjusted_p,
                power=result.power,
                assumptions_met=result.assumptions_met,
                assumption_violations=result.assumption_violations,
                test_timestamp=result.test_timestamp,
                outcome_snapshot_id=result.outcome_snapshot_id,
                effect_snapshot_id=result.effect_snapshot_id,
                treatment_n=result.treatment_n,
                control_n=result.control_n,
                random_seed=result.random_seed
            )
            corrected_results.append(corrected_result)
        
        # Reverse to restore monotonicity
        corrected_results.reverse()
        for i in range(len(corrected_results) - 1):
            if corrected_results[i].adjusted_p_value < corrected_results[i + 1].adjusted_p_value:
                corrected_results[i] = TestResult(
                    experiment_id=corrected_results[i].experiment_id,
                    metric_name=corrected_results[i].metric_name,
                    window=corrected_results[i].window,
                    segment=corrected_results[i].segment,
                    treatment_variant=corrected_results[i].treatment_variant,
                    control_variant=corrected_results[i].control_variant,
                    test_type=corrected_results[i].test_type,
                    p_value=corrected_results[i].p_value,
                    adjusted_p_value=corrected_results[i + 1].adjusted_p_value,
                    power=corrected_results[i].power,
                    assumptions_met=corrected_results[i].assumptions_met,
                    assumption_violations=corrected_results[i].assumption_violations,
                    test_timestamp=corrected_results[i].test_timestamp,
                    outcome_snapshot_id=corrected_results[i].outcome_snapshot_id,
                    effect_snapshot_id=corrected_results[i].effect_snapshot_id,
                    treatment_n=corrected_results[i].treatment_n,
                    control_n=corrected_results[i].control_n,
                    random_seed=corrected_results[i].random_seed
                )
        
        return corrected_results


# ============================================================================
# ROBUSTNESS TESTER
# ============================================================================


class RobustnessTester:
    """
    Tests robustness of statistical results.
    
    Does NOT alter p-values.
    Only provides diagnostic information.
    """
    
    def test_outlier_sensitivity(
        self,
        test_engine: StatisticalTestEngine,
        effect_result: EffectSizeResult,
        test_spec: StatisticalTestSpec,
        outlier_threshold: float = 3.0
    ) -> Dict[str, Any]:
        """
        Test sensitivity to outlier removal.
        
        Args:
            outlier_threshold: Number of standard deviations
        
        Returns:
            Sensitivity report
        """
        # Load outcomes
        treatment_outcomes = test_engine._load_outcomes(
            effect_result.experiment_id,
            effect_result.treatment_variant,
            effect_result.metric_name,
            effect_result.window
        )
        
        control_outcomes = test_engine._load_outcomes(
            effect_result.experiment_id,
            effect_result.control_variant,
            effect_result.metric_name,
            effect_result.window
        )
        
        treatment_values = [r.value for r in treatment_outcomes if not r.is_missing]
        control_values = [r.value for r in control_outcomes if not r.is_missing]
        
        # Identify outliers
        def remove_outliers(values, threshold):
            if len(values) < 3:
                return values
            
            mean = sum(values) / len(values)
            variance = sum((x - mean) ** 2 for x in values) / len(values)
            stddev = math.sqrt(variance)
            
            if stddev == 0:
                return values
            
            return [x for x in values if abs(x - mean) <= threshold * stddev]
        
        treatment_no_outliers = remove_outliers(treatment_values, outlier_threshold)
        control_no_outliers = remove_outliers(control_values, outlier_threshold)
        
        outliers_removed = (
            len(treatment_values) - len(treatment_no_outliers) +
            len(control_values) - len(control_no_outliers)
        )
        
        if outliers_removed == 0:
            return {
                "outliers_removed": 0,
                "is_sensitive": False,
                "message": "No outliers detected"
            }
        
        # Run test without outliers
        test_fn = test_engine._test_registry.get(test_spec.test_type)
        p_value_no_outliers = test_fn(
            treatment_no_outliers,
            control_no_outliers,
            test_spec.two_sided,
            None
        )
        
        # Compare to original
        original_test = test_engine.run_test(effect_result, test_spec)
        p_value_change = abs(original_test.p_value - p_value_no_outliers)
        
        is_sensitive = p_value_change > 0.01  # Threshold
        
        return {
            "outliers_removed": outliers_removed,
            "original_p_value": original_test.p_value,
            "p_value_no_outliers": p_value_no_outliers,
            "p_value_change": p_value_change,
            "is_sensitive": is_sensitive,
            "warning": "OUTLIER_SENSITIVE" if is_sensitive else None
        }
    
    def test_window_consistency(
        self,
        test_results_by_window: Dict[str, TestResult]
    ) -> Dict[str, Any]:
        """
        Test consistency of significance across windows.
        
        Returns:
            Consistency report
        """
        if len(test_results_by_window) < 2:
            return {"consistency": "insufficient_windows"}
        
        # Check for significance flips
        significant_windows = []
        for window, result in test_results_by_window.items():
            p_value = result.adjusted_p_value if result.adjusted_p_value is not None else result.p_value
            if p_value < 0.05:
                significant_windows.append(window)
        
        consistency = len(significant_windows) / len(test_results_by_window)
        
        warnings = []
        if 0 < consistency < 1:
            warnings.append("SIGNIFICANCE_FLIP: Significance changes across windows")
        
        return {
            "total_windows": len(test_results_by_window),
            "significant_windows": len(significant_windows),
            "consistency": consistency,
            "warnings": warnings
        }
    
    def test_sign_stability(
        self,
        effect_results_by_window: Dict[str, EffectSizeResult]
    ) -> Dict[str, Any]:
        """
        Test stability of effect direction across windows.
        
        Returns:
            Stability report
        """
        if len(effect_results_by_window) < 2:
            return {"stability": "insufficient_windows"}
        
        # Check sign consistency
        positive_count = sum(
            1 for result in effect_results_by_window.values()
            if result.effect_value > 0
        )
        negative_count = len(effect_results_by_window) - positive_count
        
        has_flip = positive_count > 0 and negative_count > 0
        
        warnings = []
        if has_flip:
            warnings.append("SIGN_FLIP: Effect direction changes across windows")
        
        return {
            "total_windows": len(effect_results_by_window),
            "positive_windows": positive_count,
            "negative_windows": negative_count,
            "has_sign_flip": has_flip,
            "warnings": warnings
        }


# ============================================================================
# TEST RESULT STORE
# ============================================================================


class TestResultStore:
    """
    Append-only, immutable storage for test results.
    
    Properties:
        - Append-only
        - Immutable
        - Versioned
        - Queryable by: experiment, metric, variant, window, segment
    """
    
    def __init__(self):
        # In-memory store (production would use database)
        self._results: List[TestResult] = []
        self._index_by_experiment: Dict[str, List[TestResult]] = defaultdict(list)
        self._index_by_metric: Dict[str, List[TestResult]] = defaultdict(list)
        self._result_ids: Set[str] = set()
        self._write_count: int = 0
        self._schema_version: str = "1.0.0"
    
    def write(self, result: TestResult) -> bool:
        """
        Write test result. Write-once only.
        
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
    
    def query_by_experiment(self, experiment_id: str) -> List[TestResult]:
        """Query all results for an experiment."""
        return self._index_by_experiment[experiment_id].copy()
    
    def query_by_metric(self, metric_name: str) -> List[TestResult]:
        """Query all results for a metric."""
        return self._index_by_metric[metric_name].copy()
    
    def query_by_window(
        self,
        experiment_id: str,
        window: str
    ) -> List[TestResult]:
        """Query results for a specific window."""
        experiment_results = self._index_by_experiment[experiment_id]
        return [r for r in experiment_results if r.window == window]
    
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
# STATS WATCHDOG
# ============================================================================


class StatsWatchdog:
    """
    Monitors statistical testing for anomalies.
    
    Alerts on:
        - Repeated marginal p-values
        - Significance flips across windows
        - Improbable success rates
        - P-hacking signatures
    
    Can recommend:
        - Freeze
        - Redesign
        - Longer runs
    """
    
    def __init__(self):
        self._alerts: List[Dict[str, Any]] = []
        self._freeze_recommendations: List[str] = []
        self._redesign_recommendations: List[str] = []
        
        # Thresholds
        self._marginal_p_threshold: Tuple[float, float] = (0.04, 0.06)
        self._success_rate_threshold: float = 0.95  # Too many "wins"
    
    def check_marginal_p_values(
        self,
        results: List[TestResult],
        experiment_id: str
    ):
        """Check for suspiciously many marginal p-values."""
        marginal_count = 0
        
        for result in results:
            p = result.adjusted_p_value if result.adjusted_p_value is not None else result.p_value
            
            if self._marginal_p_threshold[0] <= p <= self._marginal_p_threshold[1]:
                marginal_count += 1
        
        if marginal_count > len(results) * 0.3:  # >30% marginal
            alert = {
                "type": "marginal_p_values",
                "experiment_id": experiment_id,
                "marginal_count": marginal_count,
                "total_tests": len(results),
                "rate": marginal_count / len(results),
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)
    
    def check_significance_flips(
        self,
        results_by_window: Dict[str, List[TestResult]]
    ):
        """Check for significance flips across windows."""
        # Group by variant
        by_variant = defaultdict(list)
        for window, results in results_by_window.items():
            for result in results:
                by_variant[result.treatment_variant].append({
                    "window": window,
                    "p_value": result.adjusted_p_value if result.adjusted_p_value is not None else result.p_value
                })
        
        # Check each variant for flips
        for variant_id, p_values in by_variant.items():
            if len(p_values) < 2:
                continue
            
            # Count significance changes
            flip_count = 0
            for i in range(len(p_values) - 1):
                curr_sig = p_values[i]["p_value"] < 0.05
                next_sig = p_values[i + 1]["p_value"] < 0.05
                if curr_sig != next_sig:
                    flip_count += 1
            
            if flip_count >= 2:
                alert = {
                    "type": "significance_flips",
                    "variant_id": variant_id,
                    "flip_count": flip_count,
                    "window_count": len(p_values),
                    "timestamp": datetime.now()
                }
                self._alerts.append(alert)
                self._redesign_recommendations.append(variant_id)
    
    def check_success_rate(
        self,
        results: List[TestResult],
        experiment_id: str
    ):
        """Check for improbably high success rate."""
        if len(results) < 5:
            return  # Too few tests
        
        significant_count = 0
        for result in results:
            p = result.adjusted_p_value if result.adjusted_p_value is not None else result.p_value
            if p < 0.05:
                significant_count += 1
        
        success_rate = significant_count / len(results)
        
        if success_rate > self._success_rate_threshold:
            alert = {
                "type": "high_success_rate",
                "experiment_id": experiment_id,
                "success_rate": success_rate,
                "significant_count": significant_count,
                "total_tests": len(results),
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)
            self._freeze_recommendations.append(experiment_id)
    
    def check_p_hacking_signature(
        self,
        results: List[TestResult],
        experiment_id: str
    ):
        """Detect potential p-hacking patterns."""
        if len(results) < 3:
            return
        
        p_values = [
            result.adjusted_p_value if result.adjusted_p_value is not None else result.p_value
            for result in results
        ]
        
        # Check for p-values clustered just below 0.05
        just_below_05 = [p for p in p_values if 0.03 < p < 0.05]
        
        if len(just_below_05) > len(p_values) * 0.5:
            alert = {
                "type": "p_hacking_signature",
                "experiment_id": experiment_id,
                "clustered_count": len(just_below_05),
                "total_tests": len(results),
                "message": "Many p-values clustered just below 0.05",
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)
            self._freeze_recommendations.append(experiment_id)
    
    def get_alerts(self) -> List[Dict[str, Any]]:
        """Retrieve all alerts."""
        return self._alerts.copy()
    
    def get_freeze_recommendations(self) -> List[str]:
        """Retrieve freeze recommendations."""
        return list(set(self._freeze_recommendations))
    
    def get_redesign_recommendations(self) -> List[str]:
        """Retrieve redesign recommendations."""
        return list(set(self._redesign_recommendations))


# ============================================================================
# USAGE EXAMPLE & INTEGRATION
# ============================================================================


def example_usage():
    """
    Example showing how to use the StatisticalTestEngine.
    """
    # Initialize components
    outcome_store = OutcomeStore()
    effect_store = EffectStore()
    test_result_store = TestResultStore()
    
    preconditions_validator = PreconditionsValidator(outcome_store, effect_store)
    assumption_checker = AssumptionChecker()
    
    test_engine = StatisticalTestEngine(
        outcome_store=outcome_store,
        effect_store=effect_store,
        preconditions_validator=preconditions_validator,
        assumption_checker=assumption_checker
    )
    
    multiple_testing = MultipleTestingController()
    robustness_tester = RobustnessTester()
    watchdog = StatsWatchdog()
    
    # Define test spec
    test_spec = StatisticalTestSpec(
        metric_name="click_through_rate",
        test_type=TestType.T_TEST,
        alpha=0.05,
        two_sided=True,
        minimum_power=0.8,
        correction_group="primary_metrics"
    )
    
    # Mock effect result (would come from effect_size_analyzer)
    from effect_size_analyzer import EffectSizeResult
    
    effect_result = EffectSizeResult(
        experiment_id="exp_001",
        metric_name="click_through_rate",
        window="7d",
        segment=None,
        treatment_variant="variant_1",
        control_variant="control",
        effect_value=0.05,
        baseline_value=0.10,
        relative_lift=50.0,
        method="diff_means",
        computed_at=datetime.now(),
        outcome_snapshot_id="outcome_v1.0",
        treatment_n=1000,
        control_n=1000,
        meets_minimum_units=True,
        has_contamination=False
    )
    
    # Mark preconditions
    preconditions_validator.mark_frozen("exp_001")
    preconditions_validator.mark_effect_computed(effect_result.result_id())
    
    # Run test
    try:
        test_result = test_engine.run_test(
            effect_result=effect_result,
            test_spec=test_spec,
            random_seed=42
        )
        
        print("Test Result:")
        print(f"  p-value: {test_result.p_value:.4f}")
        print(f"  Power: {test_result.power:.3f}" if test_result.power else "  Power: N/A")
        print(f"  Assumptions met: {test_result.assumptions_met}")
        if test_result.assumption_violations:
            print(f"  Violations: {test_result.assumption_violations}")
        
        # Write to store
        test_result_store.write(test_result)
        
        # Register for multiple testing correction
        multiple_testing.register_result(test_result, "primary_metrics")
        
        # Apply correction
        corrected_results = multiple_testing.apply_correction(
            "primary_metrics",
            CorrectionMethod.BONFERRONI
        )
        
        print(f"\nAfter Bonferroni correction:")
        for result in corrected_results:
            print(f"  Adjusted p-value: {result.adjusted_p_value:.4f}")
        
        # Run watchdog checks
        watchdog.check_marginal_p_values([test_result], "exp_001")
        
        alerts = watchdog.get_alerts()
        if alerts:
            print(f"\nWatchdog alerts: {len(alerts)}")
            for alert in alerts:
                print(f"  - {alert['type']}")
        
    except ValueError as e:
        print(f"Test failed: {e}")


if __name__ == "__main__":
    example_usage()