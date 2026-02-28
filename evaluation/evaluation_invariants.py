"""
evaluation/evaluation_invariants.py

Global Truth Constraints for All Evaluation Outputs

Defines the non-negotiable laws of reality that every evaluation signal
in the system must obey. This is the constitutional layer of /evaluation/.

If metrics violate invariants and still propagate:
- learning corrupts
- dashboards lie
- RL agents optimize garbage
- regressions go undetected

This file enforces shared reality across all evaluation outputs.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Tuple, List

import numpy as np


# ============================================================================
# Core Enums
# ============================================================================

class InvariantScope(Enum):
    """Categorizes where invariants apply."""
    SINGLE_SIGNAL = "single_signal"
    CROSS_SIGNAL = "cross_signal"
    TEMPORAL = "temporal"
    PLATFORM = "platform"
    EXPERIMENT = "experiment"


class InvariantSeverity(Enum):
    """
    Severity levels for invariant violations.
    
    Severity is declared, never implied.
    """
    WARN = "warn"
    HARD_FAIL = "hard_fail"
    KILL_SWITCH = "kill_switch"


# ============================================================================
# Core Data Structures
# ============================================================================

@dataclass(frozen=True)
class EvaluationInvariant:
    """
    Core contract for a single invariant.
    
    Every invariant must be explainable with human-readable rationale.
    """
    name: str
    description: str
    
    scope: InvariantScope
    severity: InvariantSeverity
    
    applies_to: list[str]  # metric names or categories
    platforms: list[str] | None  # None = all platforms
    
    check_fn: Callable[[dict, dict], tuple[bool, str]]  # (passed, evidence)
    
    rationale: str  # human-readable justification
    
    def __post_init__(self):
        assert len(self.name) > 0, "invariant name required"
        assert len(self.rationale) > 0, "rationale required for all invariants"
        assert callable(self.check_fn), "check_fn must be callable"


@dataclass(frozen=True)
class InvariantViolation:
    """
    Immutable record of an invariant violation.
    
    No silent drops - every violation produces evidence.
    """
    invariant_name: str
    severity: InvariantSeverity
    
    affected_metrics: list[str]
    platform: str | None
    timestamp: float
    
    evidence: str
    
    def __post_init__(self):
        assert len(self.evidence) > 0, "evidence required for all violations"


@dataclass(frozen=True)
class InvariantViolationReport:
    """
    Report of all invariant violations for an evaluation snapshot.
    """
    passed: bool
    
    violations: list[InvariantViolation]
    warnings: list[InvariantViolation]
    
    total_checks: int
    total_violations: int
    
    should_reject: bool  # True if any HARD_FAIL or KILL_SWITCH
    should_kill_switch: bool  # True if any KILL_SWITCH
    
    timestamp: float


# ============================================================================
# Invariant Registry (SINGLE SOURCE OF TRUTH)
# ============================================================================

class InvariantRegistry:
    """
    Central registry for all evaluation invariants.
    
    Responsibilities:
    - Register invariants
    - Prevent duplicates
    - Enforce deterministic ordering
    - Expose invariant sets per context
    
    No dynamic mutation at runtime.
    """
    
    def __init__(self):
        self._invariants: list[EvaluationInvariant] = []
        self._invariant_names: set[str] = set()
        self._locked = False
    
    def register(self, invariant: EvaluationInvariant):
        """Register a new invariant."""
        if self._locked:
            raise RuntimeError("InvariantRegistry is locked - no mutations allowed")
        
        if invariant.name in self._invariant_names:
            raise ValueError(f"Duplicate invariant name: {invariant.name}")
        
        self._invariants.append(invariant)
        self._invariant_names.add(invariant.name)
    
    def lock(self):
        """Lock registry to prevent further mutations."""
        self._locked = True
        # Sort for deterministic ordering
        self._invariants.sort(key=lambda inv: inv.name)
    
    def get_all(self) -> list[EvaluationInvariant]:
        """Get all registered invariants in deterministic order."""
        if not self._locked:
            raise RuntimeError("Registry must be locked before accessing invariants")
        return list(self._invariants)
    
    def get_by_scope(self, scope: InvariantScope) -> list[EvaluationInvariant]:
        """Get invariants for a specific scope."""
        return [inv for inv in self.get_all() if inv.scope == scope]
    
    def get_by_platform(self, platform: str) -> list[EvaluationInvariant]:
        """Get invariants applicable to a platform."""
        return [
            inv for inv in self.get_all()
            if inv.platforms is None or platform in inv.platforms
        ]
    
    def get_by_metric(self, metric_name: str) -> list[EvaluationInvariant]:
        """Get invariants that apply to a specific metric."""
        return [
            inv for inv in self.get_all()
            if metric_name in inv.applies_to or "*" in inv.applies_to
        ]


# ============================================================================
# Temporal Invariant Enforcer
# ============================================================================

class TemporalInvariantEnforcer:
    """
    Guarantees temporal correctness:
    - No future data leakage
    - Timestamps are monotonic
    - Evaluation windows align with video age
    - No retrospective metric inflation
    """
    
    @staticmethod
    def no_future_leakage(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Evaluation timestamp must be >= all signal timestamps."""
        
        eval_timestamp = snapshot.get("evaluation_timestamp", 0.0)
        
        # Check all signal timestamps
        signal_timestamps = []
        
        if "metrics" in snapshot:
            for metric_data in snapshot["metrics"].values():
                if isinstance(metric_data, dict) and "timestamp" in metric_data:
                    signal_timestamps.append(metric_data["timestamp"])
        
        if "early_signals" in snapshot:
            if isinstance(snapshot["early_signals"], dict):
                if "timestamp" in snapshot["early_signals"]:
                    signal_timestamps.append(snapshot["early_signals"]["timestamp"])
        
        if len(signal_timestamps) == 0:
            return True, ""
        
        max_signal_time = max(signal_timestamps)
        
        if eval_timestamp < max_signal_time:
            return False, (
                f"Future leakage detected: evaluation_timestamp={eval_timestamp} "
                f"< max_signal_timestamp={max_signal_time}"
            )
        
        return True, ""
    
    @staticmethod
    def monotonic_timestamps(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Timestamps must be monotonically increasing within snapshot."""
        
        timestamps = []
        
        if "temporal_sequence" in snapshot:
            timestamps = snapshot["temporal_sequence"]
        elif "metrics" in snapshot:
            for metric_data in snapshot["metrics"].values():
                if isinstance(metric_data, dict) and "timestamp" in metric_data:
                    timestamps.append(metric_data["timestamp"])
        
        if len(timestamps) < 2:
            return True, ""
        
        for i in range(len(timestamps) - 1):
            if timestamps[i] > timestamps[i + 1]:
                return False, (
                    f"Non-monotonic timestamps: {timestamps[i]} > {timestamps[i+1]} "
                    f"at positions {i} and {i+1}"
                )
        
        return True, ""
    
    @staticmethod
    def evaluation_window_alignment(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Evaluation window must align with content age."""
        
        content_timestamp = snapshot.get("content_timestamp")
        eval_timestamp = snapshot.get("evaluation_timestamp")
        window_hours = snapshot.get("evaluation_window_hours")
        
        if content_timestamp is None or eval_timestamp is None:
            return True, ""  # Cannot validate without timestamps
        
        content_age_hours = (eval_timestamp - content_timestamp) / 3600.0
        
        if window_hours is not None and window_hours > content_age_hours:
            return False, (
                f"Evaluation window ({window_hours}h) exceeds content age "
                f"({content_age_hours:.1f}h)"
            )
        
        return True, ""


# ============================================================================
# Range Invariant Enforcer
# ============================================================================

class RangeInvariantEnforcer:
    """
    Guarantees range correctness:
    - Normalized scores in [0,1]
    - Probabilities sum correctly
    - Percentiles are ordered
    - Viral scores within calibrated bounds
    """
    
    @staticmethod
    def normalized_scores_bounded(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """All normalized scores must be in [0,1]."""
        
        violations = []
        
        # Check viral_score
        if "viral_score" in snapshot:
            score = snapshot["viral_score"]
            if not (0 <= score <= 1):
                violations.append(f"viral_score={score:.4f} out of [0,1]")
        
        # Check normalized metrics
        if "normalized_metrics" in snapshot:
            for metric_name, value in snapshot["normalized_metrics"].items():
                if isinstance(value, (int, float)):
                    if not (0 <= value <= 1):
                        violations.append(
                            f"normalized_metrics[{metric_name}]={value:.4f} out of [0,1]"
                        )
        
        # Check probability fields
        prob_fields = ["virality_probability", "suppression_probability"]
        for field in prob_fields:
            if field in snapshot:
                prob = snapshot[field]
                if not (0 <= prob <= 1):
                    violations.append(f"{field}={prob:.4f} out of [0,1]")
        
        if len(violations) > 0:
            return False, "Normalized score bounds violated: " + "; ".join(violations)
        
        return True, ""
    
    @staticmethod
    def percentiles_ordered(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Percentiles must be monotonically increasing."""
        
        percentile_keys = ["p10", "p25", "p50", "p75", "p90", "p95", "p99"]
        
        if "percentiles" not in snapshot:
            return True, ""
        
        percentiles = snapshot["percentiles"]
        
        # Extract ordered percentile values
        values = []
        for key in percentile_keys:
            if key in percentiles:
                values.append(percentiles[key])
        
        if len(values) < 2:
            return True, ""
        
        # Check ordering
        for i in range(len(values) - 1):
            if values[i] > values[i + 1]:
                return False, (
                    f"Percentiles out of order: {percentile_keys[i]}={values[i]:.4f} "
                    f"> {percentile_keys[i+1]}={values[i+1]:.4f}"
                )
        
        return True, ""
    
    @staticmethod
    def probability_distribution_sums(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Probability distributions must sum to approximately 1.0."""
        
        if "outcome_probabilities" not in snapshot:
            return True, ""
        
        probs = snapshot["outcome_probabilities"]
        
        if isinstance(probs, dict):
            total = sum(probs.values())
        elif isinstance(probs, (list, np.ndarray)):
            total = sum(probs)
        else:
            return True, ""
        
        # Allow small numerical error
        if not (0.99 <= total <= 1.01):
            return False, f"Probability distribution sums to {total:.6f}, not ~1.0"
        
        return True, ""


# ============================================================================
# Distribution Invariant Enforcer
# ============================================================================

class DistributionInvariantEnforcer:
    """
    Guarantees distribution sanity:
    - Score distributions are non-degenerate
    - Variance above minimum entropy thresholds
    - Tail behavior exists for claimed viral content
    
    Prevents "everything is great" failures.
    """
    
    @staticmethod
    def non_degenerate_distribution(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Score distributions must have minimum variance."""
        
        min_variance = context.get("min_variance_threshold", 0.001)
        
        distributions_to_check = []
        
        if "score_distribution" in snapshot:
            distributions_to_check.append(("score_distribution", snapshot["score_distribution"]))
        
        if "metric_distributions" in snapshot:
            for metric_name, dist in snapshot["metric_distributions"].items():
                distributions_to_check.append((metric_name, dist))
        
        violations = []
        
        for name, dist in distributions_to_check:
            if isinstance(dist, (list, np.ndarray)) and len(dist) > 1:
                variance = float(np.var(dist))
                if variance < min_variance:
                    violations.append(
                        f"{name}: variance={variance:.6f} < threshold={min_variance}"
                    )
        
        if len(violations) > 0:
            return False, "Degenerate distributions detected: " + "; ".join(violations)
        
        return True, ""
    
    @staticmethod
    def viral_content_has_tail(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """
        If content is marked as viral, distribution must show tail behavior.
        """
        
        is_viral = snapshot.get("is_viral", False)
        viral_score = snapshot.get("viral_score", 0.0)
        
        # Only check if explicitly marked as viral or high viral score
        if not is_viral and viral_score < 0.7:
            return True, ""
        
        if "score_distribution" not in snapshot:
            return True, ""  # Cannot validate without distribution
        
        dist = snapshot["score_distribution"]
        
        if not isinstance(dist, (list, np.ndarray)) or len(dist) < 10:
            return True, ""
        
        # Check that p95/p50 ratio is significant (indicates tail)
        p50 = float(np.percentile(dist, 50))
        p95 = float(np.percentile(dist, 95))
        
        if p50 < 1e-6:  # Avoid division by zero
            return True, ""
        
        tail_ratio = p95 / p50
        
        if tail_ratio < 1.3:  # Tail should be at least 30% larger than median
            return False, (
                f"Viral content lacks tail behavior: p95/p50 ratio = {tail_ratio:.2f} "
                f"(expected > 1.3 for viral content)"
            )
        
        return True, ""
    
    @staticmethod
    def minimum_entropy(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Distribution must have minimum entropy to be meaningful."""
        
        min_entropy = context.get("min_entropy_threshold", 0.5)
        
        if "score_distribution" not in snapshot:
            return True, ""
        
        dist = snapshot["score_distribution"]
        
        if not isinstance(dist, (list, np.ndarray)) or len(dist) < 2:
            return True, ""
        
        # Compute Shannon entropy
        hist, _ = np.histogram(dist, bins=10, density=True)
        hist = hist + 1e-10  # Avoid log(0)
        hist = hist / hist.sum()  # Normalize
        
        entropy = -np.sum(hist * np.log2(hist))
        max_entropy = np.log2(len(hist))
        normalized_entropy = entropy / max_entropy
        
        if normalized_entropy < min_entropy:
            return False, (
                f"Distribution entropy too low: {normalized_entropy:.3f} "
                f"< threshold={min_entropy}"
            )
        
        return True, ""


# ============================================================================
# Cross-Signal Consistency Enforcer (CRITICAL)
# ============================================================================

class CrossSignalConsistencyEnforcer:
    """
    Ensures logical consistency between metrics.
    
    This is where silent metric lies are caught.
    
    Examples:
    - High viral_score + low reach → contradiction
    - Strong early signals + zero long-term envelope → invalid
    - Suppression detected + no suppression-adjusted metrics → violation
    """
    
    @staticmethod
    def viral_score_reach_consistency(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """High viral score requires proportional reach."""
        
        viral_score = snapshot.get("viral_score", 0.0)
        reach = snapshot.get("reach", 0.0)
        
        # Only check if viral_score is high
        if viral_score < 0.7:
            return True, ""
        
        # High viral score should imply meaningful reach
        min_expected_reach = 0.3
        
        if reach < min_expected_reach:
            return False, (
                f"Inconsistency: viral_score={viral_score:.3f} but reach={reach:.3f} "
                f"(expected reach >= {min_expected_reach} for high viral scores)"
            )
        
        return True, ""
    
    @staticmethod
    def early_signal_envelope_consistency(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """Strong early signals should imply non-zero long-term envelope."""
        
        early_signal_strength = snapshot.get("early_signal_strength", 0.0)
        
        if early_signal_strength < 0.6:
            return True, ""
        
        if "engagement_envelope" not in snapshot:
            return True, ""
        
        envelope = snapshot["engagement_envelope"]
        
        if isinstance(envelope, dict):
            late_phase = envelope.get("late_phase", 0.0)
            
            if late_phase < 0.05:
                return False, (
                    f"Inconsistency: early_signal_strength={early_signal_strength:.3f} "
                    f"but late_phase engagement={late_phase:.3f} (expected > 0.05)"
                )
        
        return True, ""
    
    @staticmethod
    def suppression_adjustment_consistency(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """If suppression detected, suppression-adjusted metrics must exist."""
        
        suppression_detected = snapshot.get("suppression_detected", False)
        suppression_score = snapshot.get("suppression_score", 0.0)
        
        if not suppression_detected and suppression_score < 0.3:
            return True, ""
        
        if "suppression_adjusted_metrics" not in snapshot:
            return False, (
                f"Suppression detected (score={suppression_score:.3f}) but no "
                f"suppression_adjusted_metrics present"
            )
        
        adjusted = snapshot["suppression_adjusted_metrics"]
        
        if not isinstance(adjusted, dict) or len(adjusted) == 0:
            return False, (
                f"Suppression detected but suppression_adjusted_metrics is empty"
            )
        
        return True, ""
    
    @staticmethod
    def score_metric_alignment(
        snapshot: dict,
        context: dict
    ) -> tuple[bool, str]:
        """
        Viral score should align with aggregated metric performance.
        """
        
        viral_score = snapshot.get("viral_score")
        
        if viral_score is None:
            return True, ""
        
        if "normalized_metrics" not in snapshot:
            return True, ""
        
        metrics = snapshot["normalized_metrics"]
        
        if not isinstance(metrics, dict) or len(metrics) == 0:
            return True, ""
        
        # Compute average of normalized metrics
        metric_values = [v for v in metrics.values() if isinstance(v, (int, float))]
        
        if len(metric_values) == 0:
            return True, ""
        
        avg_metric = float(np.mean(metric_values))
        
        # Check for extreme misalignment
        diff = abs(viral_score - avg_metric)
        
        if diff > 0.5:  # More than 0.5 apart on [0,1] scale
            return False, (
                f"Severe misalignment: viral_score={viral_score:.3f} but "
                f"avg(normalized_metrics)={avg_metric:.3f} (diff={diff:.3f})"
            )
        
        return True, ""


# ============================================================================
# Evaluation Invariant Gate (ENFORCEMENT POINT)
# ============================================================================

class EvaluationInvariantGate:
    """
    The ONLY public entrypoint for invariant enforcement.
    
    Every /evaluation/ output MUST pass through this gate.
    No bypassing allowed.
    """
    
    def __init__(self, registry: InvariantRegistry):
        if not registry._locked:
            raise RuntimeError("InvariantRegistry must be locked before use")
        
        self.registry = registry
    
    def enforce(
        self,
        evaluation_snapshot: dict,
        context: dict | None = None
    ) -> InvariantViolationReport:
        """
        Enforce all applicable invariants on an evaluation snapshot.
        
        Args:
            evaluation_snapshot: The evaluation output to validate
            context: Additional context (platform, experiment_id, etc.)
        
        Returns:
            InvariantViolationReport with all violations and warnings
        """
        
        if context is None:
            context = {}
        
        timestamp = time.time()
        
        # Get applicable invariants
        platform = evaluation_snapshot.get("platform") or context.get("platform")
        
        if platform:
            invariants = self.registry.get_by_platform(platform)
        else:
            invariants = self.registry.get_all()
        
        violations = []
        warnings = []
        total_checks = 0
        
        # Run all checks deterministically
        for invariant in invariants:
            total_checks += 1
            
            try:
                passed, evidence = invariant.check_fn(evaluation_snapshot, context)
                
                if not passed:
                    violation = InvariantViolation(
                        invariant_name=invariant.name,
                        severity=invariant.severity,
                        affected_metrics=invariant.applies_to,
                        platform=platform,
                        timestamp=timestamp,
                        evidence=evidence
                    )
                    
                    if invariant.severity == InvariantSeverity.WARN:
                        warnings.append(violation)
                    else:
                        violations.append(violation)
            
            except Exception as e:
                # Invariant check itself failed - treat as HARD_FAIL
                violation = InvariantViolation(
                    invariant_name=invariant.name,
                    severity=InvariantSeverity.HARD_FAIL,
                    affected_metrics=invariant.applies_to,
                    platform=platform,
                    timestamp=timestamp,
                    evidence=f"Invariant check raised exception: {str(e)}"
                )
                violations.append(violation)
        
        # Determine disposition
        should_kill_switch = any(
            v.severity == InvariantSeverity.KILL_SWITCH for v in violations
        )
        
        should_reject = should_kill_switch or any(
            v.severity == InvariantSeverity.HARD_FAIL for v in violations
        )
        
        passed = len(violations) == 0
        
        return InvariantViolationReport(
            passed=passed,
            violations=violations,
            warnings=warnings,
            total_checks=total_checks,
            total_violations=len(violations),
            should_reject=should_reject,
            should_kill_switch=should_kill_switch,
            timestamp=timestamp
        )
    
    def enforce_batch(
        self,
        snapshots: list[dict],
        context: dict | None = None
    ) -> list[InvariantViolationReport]:
        """Enforce invariants on a batch of snapshots."""
        
        return [self.enforce(snapshot, context) for snapshot in snapshots]


# ============================================================================
# Standard Invariant Definitions
# ============================================================================

def register_standard_invariants(registry: InvariantRegistry):
    """
    Register all standard invariants.
    
    This is the canonical set of invariants for the evaluation layer.
    """
    
    # ========================================================================
    # TEMPORAL INVARIANTS
    # ========================================================================
    
    registry.register(EvaluationInvariant(
        name="temporal.no_future_leakage",
        description="Evaluation timestamp must be >= all signal timestamps",
        scope=InvariantScope.TEMPORAL,
        severity=InvariantSeverity.HARD_FAIL,
        applies_to=["*"],
        platforms=None,
        check_fn=TemporalInvariantEnforcer.no_future_leakage,
        rationale=(
            "Future leakage corrupts learning by allowing models to see "
            "data that would not be available at prediction time"
        )
    ))
    
    registry.register(EvaluationInvariant(
        name="temporal.monotonic_timestamps",
        description="Timestamps must be monotonically increasing",
        scope=InvariantScope.TEMPORAL,
        severity=InvariantSeverity.HARD_FAIL,
        applies_to=["*"],
        platforms=None,
        check_fn=TemporalInvariantEnforcer.monotonic_timestamps,
        rationale="Non-monotonic timestamps indicate data corruption or processing errors"
    ))
    
    registry.register(EvaluationInvariant(
        name="temporal.evaluation_window_alignment",
        description="Evaluation window must not exceed content age",
        scope=InvariantScope.TEMPORAL,
        severity=InvariantSeverity.HARD_FAIL,
        applies_to=["*"],
        platforms=None,
        check_fn=TemporalInvariantEnforcer.evaluation_window_alignment,
        rationale="Evaluation windows exceeding content age create impossible scenarios"
    ))
    
    # ========================================================================
    # RANGE INVARIANTS
    # ========================================================================
    
    registry.register(EvaluationInvariant(
        name="range.normalized_scores_bounded",
        description="All normalized scores must be in [0,1]",
        scope=InvariantScope.SINGLE_SIGNAL,
        severity=InvariantSeverity.HARD_FAIL,
        applies_to=["viral_score", "normalized_metrics", "probabilities"],
        platforms=None,
        check_fn=RangeInvariantEnforcer.normalized_scores_bounded,
        rationale="Scores outside [0,1] indicate normalization failures"
    ))
    
    registry.register(EvaluationInvariant(
        name="range.percentiles_ordered",
        description="Percentiles must be monotonically increasing",
        scope=InvariantScope.SINGLE_SIGNAL,
        severity=InvariantSeverity.HARD_FAIL,
        applies_to=["percentiles"],
        platforms=None,
        check_fn=RangeInvariantEnforcer.percentiles_ordered,
        rationale="Out-of-order percentiles indicate calculation errors"
    ))
    
    registry.register(EvaluationInvariant(
        name="range.probability_distribution_sums",
        description="Probability distributions must sum to ~1.0",
        scope=InvariantScope.SINGLE_SIGNAL,
        severity=InvariantSeverity.HARD_FAIL,
        applies_to=["outcome_probabilities"],
        platforms=None,
        check_fn=RangeInvariantEnforcer.probability_distribution_sums,
        rationale="Probability distributions that don't sum to 1.0 are invalid"
    ))
    
    # ========================================================================
    # DISTRIBUTION INVARIANTS
    # ========================================================================
    
    registry.register(EvaluationInvariant(
        name="distribution.non_degenerate",
        description="Score distributions must have minimum variance",
        scope=InvariantScope.SINGLE_SIGNAL,
        severity=InvariantSeverity.WARN,
        applies_to=["score_distribution", "metric_distributions"],
        platforms=None,
        check_fn=DistributionInvariantEnforcer.non_degenerate_distribution,
        rationale="Degenerate distributions indicate systemic issues or metric collapse"
    ))
    
    registry.register(EvaluationInvariant(
        name="distribution.viral_content_has_tail",
        description="Viral content must show tail behavior in distribution",
        scope=InvariantScope.SINGLE_SIGNAL,
        severity=InvariantSeverity.WARN,
        applies_to=["score_distribution"],
        platforms=None,
        check_fn=DistributionInvariantEnforcer.viral_content_has_tail,
        rationale="Viral content without tail behavior contradicts viral classification"
    ))
    
    registry.register(EvaluationInvariant(
        name="distribution.minimum_entropy",
        description="Distributions must have minimum entropy",
        scope=InvariantScope.SINGLE_SIGNAL,
        severity=InvariantSeverity.WARN,
        applies_to=["score_distribution"],
        platforms=None,
        check_fn=DistributionInvariantEnforcer.minimum_entropy,
        rationale="Low entropy indicates lack of meaningful variation"
    ))
    
    # ========================================================================
    # CROSS-SIGNAL CONSISTENCY INVARIANTS
    # ========================================================================
    
    registry.register(EvaluationInvariant(
        name="consistency.viral_score_reach",
        description="High viral score requires proportional reach",
        scope=InvariantScope.CROSS_SIGNAL,
        severity=InvariantSeverity.WARN,
        applies_to=["viral_score", "reach"],
        platforms=None,
        check_fn=CrossSignalConsistencyEnforcer.viral_score_reach_consistency,
        rationale="High viral scores with low reach indicate metric contradiction"
    ))
    
    registry.register(EvaluationInvariant(
        name="consistency.early_signal_envelope",
        description="Strong early signals should imply non-zero long-term envelope",
        scope=InvariantScope.CROSS_SIGNAL,
        severity=InvariantSeverity.WARN,
        applies_to=["early_signal_strength", "engagement_envelope"],
        platforms=None,
        check_fn=CrossSignalConsistencyEnforcer.early_signal_envelope_consistency,
        rationale="Early signals without late engagement suggest metric inconsistency"
    ))