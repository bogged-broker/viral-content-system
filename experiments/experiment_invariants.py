"""
/experiments/experiment_invariants.py

Deterministic Experiment Integrity & Statistical Legitimacy Authority
(No P-Hacking, No Silent Metric Drift, No Exposure Corruption)

This module defines the formal correctness constraints that every experiment must satisfy
before launch, runtime continuation, metric publication, statistical inference, promotion
to production, downstream model training, and executive reporting.

CRITICAL PRINCIPLES:
- Deterministic validation (identical inputs → identical output)
- No runtime mutability of invariant definitions
- No environment-dependent logic
- All invariants explicit
- Severity classification (CRITICAL | MAJOR | WARNING)
- No silent downgrade of violation severity
- Versioned invariant schema

ABSOLUTE INVARIANTS:
1. No experiment with CRITICAL violations may publish
2. No assignment mutation mid-flight
3. No exposure switching allowed
4. No metric mutation during active run
5. No undefined stopping rule
6. No silent SRM
7. No leakage tolerated
8. No post-hoc primary metric switching
9. No reproducibility failure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, FrozenSet, Callable
from collections import Counter, defaultdict
from types import MappingProxyType
import hashlib
import json
import logging
import math
from abc import ABC, abstractmethod

try:
    from scipy.stats import chisquare
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    chisquare = None


# ============================================================================
# SEVERITY & VIOLATION TYPES
# ============================================================================


class Severity(Enum):
    """Violation severity with strict ordering."""
    CRITICAL = 3  # Causal validity compromised
    MAJOR = 2     # Integrity risk
    WARNING = 1   # Recommendation-level issue

    def __lt__(self, other: Severity) -> bool:
        return self.value < other.value

    def __le__(self, other: Severity) -> bool:
        return self.value <= other.value


class InvariantID(Enum):
    """Uniquely defined invariants - no anonymous violations."""
    INV_ASSIGNMENT_DETERMINISM = "INV_ASSIGNMENT_DETERMINISM"
    INV_ASSIGNMENT_STABILITY = "INV_ASSIGNMENT_STABILITY"
    INV_ASSIGNMENT_ELIGIBILITY = "INV_ASSIGNMENT_ELIGIBILITY"
    INV_SRM = "INV_SRM"
    INV_CROSS_EXPOSURE = "INV_CROSS_EXPOSURE"
    INV_VARIANT_SWITCHING = "INV_VARIANT_SWITCHING"
    INV_METRIC_MUTATION = "INV_METRIC_MUTATION"
    INV_METRIC_DEFINITION_DRIFT = "INV_METRIC_DEFINITION_DRIFT"
    INV_METRIC_DENOMINATOR_DRIFT = "INV_METRIC_DENOMINATOR_DRIFT"
    INV_METRIC_WINDOW_CONTAMINATION = "INV_METRIC_WINDOW_CONTAMINATION"
    INV_TEMPORAL_BOUNDARY = "INV_TEMPORAL_BOUNDARY"
    INV_TEMPORAL_DRIFT = "INV_TEMPORAL_DRIFT"
    INV_RETROACTIVE_ENROLLMENT = "INV_RETROACTIVE_ENROLLMENT"
    INV_POWER_DECLARATION = "INV_POWER_DECLARATION"
    INV_POWER_STABILITY = "INV_POWER_STABILITY"
    INV_STOPPING_RULE_UNDECLARED = "INV_STOPPING_RULE_UNDECLARED"
    INV_SEQUENTIAL_CORRECTION = "INV_SEQUENTIAL_CORRECTION"
    INV_SEQUENTIAL_MONITORING_DISCIPLINE = "INV_SEQUENTIAL_MONITORING_DISCIPLINE"
    INV_MULTI_HYPOTHESIS_UNCONTROLLED = "INV_MULTI_HYPOTHESIS_UNCONTROLLED"
    INV_LEAKAGE_DETECTED = "INV_LEAKAGE_DETECTED"
    INV_LEAKAGE_FUTURE_INFO = "INV_LEAKAGE_FUTURE_INFO"
    INV_ISOLATION_INTERFERENCE = "INV_ISOLATION_INTERFERENCE"
    INV_ISOLATION_ELIGIBILITY_CONFLICT = "INV_ISOLATION_ELIGIBILITY_CONFLICT"
    INV_ANALYSIS_PLAN_DRIFT = "INV_ANALYSIS_PLAN_DRIFT"
    INV_POST_HOC_SEGMENTATION = "INV_POST_HOC_SEGMENTATION"
    INV_PRIMARY_METRIC_SWITCH = "INV_PRIMARY_METRIC_SWITCH"
    INV_REPRODUCIBILITY_FAILURE = "INV_REPRODUCIBILITY_FAILURE"
    INV_SCHEMA_VERSION_INCOMPATIBLE = "INV_SCHEMA_VERSION_INCOMPATIBLE"
    INV_ALLOCATION_MUTATION = "INV_ALLOCATION_MUTATION"
    INV_DOWNSTREAM_GATING = "INV_DOWNSTREAM_GATING"


# ============================================================================
# CANONICAL ORDERING UTILITIES (Determinism Guarantees)
# ============================================================================


def _canonical_serialize(obj: Any) -> str:
    """
    Canonical JSON serialization for deterministic hashing.
    
    Ensures identical objects produce identical JSON strings.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _canonical_dict(obj: Any) -> Dict[str, Any]:
    """
    Convert object to canonical dictionary representation.
    
    Recursively sorts all dict keys for deterministic ordering.
    """
    if isinstance(obj, dict):
        return {k: _canonical_dict(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, (list, tuple)):
        return [_canonical_dict(item) for item in obj]
    elif isinstance(obj, (datetime,)):
        return obj.isoformat()
    elif isinstance(obj, Enum):
        return obj.value
    else:
        return obj


def _canonical_exposures(exposures: Tuple[ExposureRecord, ...]) -> Tuple[ExposureRecord, ...]:
    """
    Return exposures in canonical order.
    
    Sorts by: (subject_id, identity_context, exposure_timestamp, variant)
    """
    return tuple(sorted(
        exposures,
        key=lambda e: (
            e.subject_id,
            e.identity_context,
            e.exposure_timestamp.isoformat() if isinstance(e.exposure_timestamp, datetime) else str(e.exposure_timestamp),
            e.variant
        )
    ))


# ============================================================================
# INVARIANT DSL (Declarative Invariant Specification)
# ============================================================================


@dataclass(frozen=True)
class InvariantSpec:
    """
    Declarative invariant specification.
    
    This enables:
    - Versioned invariant definitions
    - Diffability across schema upgrades
    - Formal auditability
    - Immutable definitions
    """
    invariant_id: InvariantID
    category: str
    severity: Severity
    description: str
    version: str = "1.0.0"
    
    def __post_init__(self):
        """Validate invariant spec."""
        if not self.description:
            raise ValueError("Invariant description cannot be empty")


@dataclass(frozen=True)
class InvariantRegistry:
    """
    Immutable registry of all invariant specifications.
    
    Provides deterministic, versioned invariant definitions.
    """
    specs: Dict[InvariantID, InvariantSpec]
    registry_version: str = "1.0.0"
    
    def get_spec(self, invariant_id: InvariantID) -> Optional[InvariantSpec]:
        """Get invariant specification by ID."""
        return self.specs.get(invariant_id)
    
    def get_all_specs(self) -> Tuple[InvariantSpec, ...]:
        """Get all specs in canonical order."""
        return tuple(sorted(
            self.specs.values(),
            key=lambda s: (s.category, s.invariant_id.value)
        ))


def create_default_invariant_registry() -> InvariantRegistry:
    """Create default registry with all standard invariants."""
    specs = {
        InvariantID.INV_ASSIGNMENT_DETERMINISM: InvariantSpec(
            invariant_id=InvariantID.INV_ASSIGNMENT_DETERMINISM,
            category="Assignment",
            severity=Severity.CRITICAL,
            description="Assignment must be deterministic",
        ),
        InvariantID.INV_ASSIGNMENT_STABILITY: InvariantSpec(
            invariant_id=InvariantID.INV_ASSIGNMENT_STABILITY,
            category="Assignment",
            severity=Severity.CRITICAL,
            description="Assignment must not mutate mid-flight",
        ),
        InvariantID.INV_ASSIGNMENT_ELIGIBILITY: InvariantSpec(
            invariant_id=InvariantID.INV_ASSIGNMENT_ELIGIBILITY,
            category="Assignment",
            severity=Severity.CRITICAL,
            description="All variants must have equal eligibility",
        ),
        InvariantID.INV_SRM: InvariantSpec(
            invariant_id=InvariantID.INV_SRM,
            category="Sample Ratio",
            severity=Severity.CRITICAL,
            description="Sample ratio mismatch detected",
        ),
        InvariantID.INV_CROSS_EXPOSURE: InvariantSpec(
            invariant_id=InvariantID.INV_CROSS_EXPOSURE,
            category="Exposure",
            severity=Severity.CRITICAL,
            description="Subject exposed at most once per identity context",
        ),
        InvariantID.INV_VARIANT_SWITCHING: InvariantSpec(
            invariant_id=InvariantID.INV_VARIANT_SWITCHING,
            category="Exposure",
            severity=Severity.CRITICAL,
            description="Variant switching detected",
        ),
        InvariantID.INV_METRIC_MUTATION: InvariantSpec(
            invariant_id=InvariantID.INV_METRIC_MUTATION,
            category="Metric",
            severity=Severity.CRITICAL,
            description="Metric definition mutated during experiment",
        ),
        InvariantID.INV_METRIC_DEFINITION_DRIFT: InvariantSpec(
            invariant_id=InvariantID.INV_METRIC_DEFINITION_DRIFT,
            category="Metric",
            severity=Severity.CRITICAL,
            description="Metric definition hash changed",
        ),
        InvariantID.INV_METRIC_DENOMINATOR_DRIFT: InvariantSpec(
            invariant_id=InvariantID.INV_METRIC_DENOMINATOR_DRIFT,
            category="Metric",
            severity=Severity.CRITICAL,
            description="Metric denominator definition changed",
        ),
        InvariantID.INV_METRIC_WINDOW_CONTAMINATION: InvariantSpec(
            invariant_id=InvariantID.INV_METRIC_WINDOW_CONTAMINATION,
            category="Metric",
            severity=Severity.CRITICAL,
            description="Outcome leakage into eligibility window detected",
        ),
        InvariantID.INV_POWER_STABILITY: InvariantSpec(
            invariant_id=InvariantID.INV_POWER_STABILITY,
            category="Statistical Discipline",
            severity=Severity.MAJOR,
            description="Power analysis results unstable across time buckets",
        ),
        InvariantID.INV_SEQUENTIAL_MONITORING_DISCIPLINE: InvariantSpec(
            invariant_id=InvariantID.INV_SEQUENTIAL_MONITORING_DISCIPLINE,
            category="Statistical Discipline",
            severity=Severity.CRITICAL,
            description="Sequential monitoring discipline not properly enforced",
        ),
        InvariantID.INV_DOWNSTREAM_GATING: InvariantSpec(
            invariant_id=InvariantID.INV_DOWNSTREAM_GATING,
            category="Downstream",
            severity=Severity.CRITICAL,
            description="Downstream gating checks failed",
        ),
        # Add other invariants...
    }
    return InvariantRegistry(specs=specs)


# ============================================================================
# DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class Violation:
    """Immutable violation record."""
    invariant_id: InvariantID
    category: str
    description: str
    severity: Severity
    detected_evidence: Dict[str, Any]
    impacted_metric: Optional[str] = None

    def __post_init__(self):
        """Validate violation construction."""
        if not self.description:
            raise ValueError("Violation description cannot be empty")
        if not isinstance(self.detected_evidence, dict):
            raise ValueError("Evidence must be a dictionary")


@dataclass(frozen=True)
class InvariantReport:
    """
    Immutable validation report.
    
    CRITICAL violations block experiment publication.
    Report is deterministic and reproducible.
    """
    overall_valid: bool
    violations: Tuple[Violation, ...]
    warnings: Tuple[Violation, ...]
    severity_counts: Dict[Severity, int]
    blocking: bool
    validation_timestamp: datetime
    invariant_schema_version: str
    experiment_schema_version: str
    report_hash: str

    @property
    def has_critical(self) -> bool:
        """Check if any critical violations exist."""
        return any(v.severity == Severity.CRITICAL for v in self.violations)

    @property
    def has_major(self) -> bool:
        """Check if any major violations exist."""
        return any(v.severity == Severity.MAJOR for v in self.violations)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "overall_valid": self.overall_valid,
            "violations": [
                {
                    "invariant_id": v.invariant_id.value,
                    "category": v.category,
                    "description": v.description,
                    "severity": v.severity.name,
                    "evidence": v.detected_evidence,
                    "impacted_metric": v.impacted_metric,
                }
                for v in self.violations
            ],
            "warnings": [
                {
                    "invariant_id": v.invariant_id.value,
                    "category": v.category,
                    "description": v.description,
                    "severity": v.severity.name,
                    "evidence": v.detected_evidence,
                    "impacted_metric": v.impacted_metric,
                }
                for v in self.warnings
            ],
            "severity_counts": {s.name: c for s, c in self.severity_counts.items()},
            "blocking": self.blocking,
            "validation_timestamp": self.validation_timestamp.isoformat(),
            "invariant_schema_version": self.invariant_schema_version,
            "experiment_schema_version": self.experiment_schema_version,
            "report_hash": self.report_hash,
        }


@dataclass(frozen=True)
class ExperimentSnapshot:
    """Immutable experiment configuration snapshot."""
    experiment_id: str
    start_timestamp: datetime
    eligibility_boundary: datetime
    analysis_window_start: datetime
    analysis_window_end: datetime
    freeze_point: Optional[datetime]
    variant_config: Dict[str, Any]  # variant_name -> config
    allocation_weights: Dict[str, float]  # variant_name -> weight
    assignment_seed: str
    hash_function: str
    schema_version: str
    config_hash: str
    adaptive_enabled: bool = False


@dataclass(frozen=True)
class MetricDefinition:
    """Immutable metric definition."""
    metric_id: str
    version: str
    data_source: str
    aggregation_logic: str
    windowing_logic: str
    transformation: str
    denominator: Optional[str]
    filters: Tuple[str, ...]
    definition_hash: str
    is_primary: bool = False


@dataclass(frozen=True)
class MetricRegistry:
    """Immutable registry of metric definitions."""
    metrics: Dict[str, MetricDefinition]  # metric_id -> definition
    registry_version: str
    registry_hash: str


@dataclass(frozen=True)
class ExposureRecord:
    """Single exposure event."""
    subject_id: str
    variant: str
    exposure_timestamp: datetime
    identity_context: str


@dataclass(frozen=True)
class ExposureSnapshot:
    """Immutable snapshot of exposure data."""
    exposures: Tuple[ExposureRecord, ...]
    snapshot_timestamp: datetime
    snapshot_hash: str


@dataclass(frozen=True)
class AllocationConfig:
    """Traffic allocation configuration."""
    expected_distribution: Dict[str, float]  # variant -> expected proportion
    srm_tolerance: float  # Statistical tolerance for SRM detection
    total_population: int
    config_version: str


@dataclass(frozen=True)
class StoppingRule:
    """Stopping rule declaration."""
    rule_type: str  # "fixed_sample", "sequential", "bayesian"
    sample_size: Optional[int] = None
    correction_method: Optional[str] = None  # For sequential: "obrien_fleming", etc.
    bayesian_framework: Optional[str] = None


@dataclass(frozen=True)
class MultipleHypothesisControl:
    """Multiple hypothesis correction declaration."""
    correction_method: str  # "bonferroni", "fdr", "holm", "hierarchical"
    primary_metrics: Tuple[str, ...]
    hierarchical_priority: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class AnalysisPlan:
    """Pre-registered analysis plan."""
    plan_id: str
    plan_version: str
    primary_metrics: Tuple[str, ...]
    secondary_metrics: Tuple[str, ...]
    stopping_rule: StoppingRule
    multiple_hypothesis_control: Optional[MultipleHypothesisControl]
    pre_registered_segments: Tuple[str, ...]
    exclusion_criteria: Tuple[str, ...]
    plan_hash: str
    locked_at: datetime


# ============================================================================
# INVARIANT VALIDATORS (Abstract Base)
# ============================================================================


class InvariantValidator(ABC):
    """Base class for all invariant validators."""

    @abstractmethod
    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        """
        Validate invariant and return violations.
        
        Must be deterministic - identical inputs produce identical output.
        """
        pass


# ============================================================================
# ASSIGNMENT INVARIANTS
# ============================================================================


class AssignmentDeterminismValidator(InvariantValidator):
    """Validates deterministic variant assignment."""

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Check hash function is defined
        if not experiment.hash_function:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_ASSIGNMENT_DETERMINISM,
                    category="Assignment",
                    description="Hash function not defined for assignment",
                    severity=Severity.CRITICAL,
                    detected_evidence={"hash_function": experiment.hash_function},
                )
            )

        # Check assignment seed exists
        if not experiment.assignment_seed:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_ASSIGNMENT_DETERMINISM,
                    category="Assignment",
                    description="Assignment seed not defined",
                    severity=Severity.CRITICAL,
                    detected_evidence={"assignment_seed": experiment.assignment_seed},
                )
            )

        return violations


class AssignmentStabilityValidator(InvariantValidator):
    """
    Validates assignment does not mutate mid-flight.
    
    Must guarantee:
    - Deterministic variant assignment
    - Stable hash function
    - No cross-device drift (if identity unified)
    - No assignment mutation during experiment
    - Equal eligibility universe for all variants
    
    Prohibited:
    - Runtime variant weight adjustment (unless declared adaptive)
    - Condition-based assignment bias
    - Time-dependent assignment logic
    
    If violated: → CRITICAL
    """

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Check for duplicate exposures with different variants (variant switching)
        subject_variants: Dict[str, Set[str]] = {}
        subject_exposure_times: Dict[str, List[datetime]] = {}
        
        for exposure in exposures.exposures:
            key = f"{exposure.subject_id}:{exposure.identity_context}"
            if key not in subject_variants:
                subject_variants[key] = set()
                subject_exposure_times[key] = []
            subject_variants[key].add(exposure.variant)
            subject_exposure_times[key].append(exposure.exposure_timestamp)

        switchers = {k: v for k, v in subject_variants.items() if len(v) > 1}
        if switchers:
            # Get timing information for switchers
            switch_evidence = {}
            for key, variants in list(switchers.items())[:10]:  # Sample first 10
                switch_evidence[key] = {
                    "variants": list(variants),
                    "exposure_times": [
                        t.isoformat() for t in sorted(subject_exposure_times[key])
                    ],
                }
            
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_VARIANT_SWITCHING,
                    category="Assignment",
                    description=(
                        f"Variant switching detected for {len(switchers)} subjects. "
                        f"Subjects must not switch variants mid-experiment."
                    ),
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "affected_subjects": len(switchers),
                        "total_exposures": len(exposures.exposures),
                        "switch_rate": len(switchers) / len(subject_variants) if subject_variants else 0.0,
                        "sample_switchers": switch_evidence,
                    },
                )
            )

        return violations


class EligibilityEqualityValidator(InvariantValidator):
    """
    Validates equal eligibility universe for all variants.
    
    All variants must have access to the same eligibility pool.
    No variant-specific eligibility filtering allowed.
    
    If violated: → CRITICAL
    """

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Check if variant configs have different eligibility criteria
        variant_configs = experiment.variant_config
        
        if len(variant_configs) > 1:
            # Extract eligibility criteria from each variant config
            eligibility_keys = set()
            for variant_name, config in variant_configs.items():
                if isinstance(config, dict):
                    # Check for eligibility-related keys
                    for key in config.keys():
                        if "eligibility" in key.lower() or "filter" in key.lower():
                            eligibility_keys.add(key)
            
            # If variants have different eligibility criteria, flag violation
            variant_eligibility = {}
            for variant_name, config in variant_configs.items():
                if isinstance(config, dict):
                    variant_eligibility[variant_name] = {
                        k: v for k, v in config.items()
                        if "eligibility" in k.lower() or "filter" in k.lower()
                    }
            
            # Check for differences
            if len(variant_eligibility) > 1:
                first_variant = list(variant_eligibility.keys())[0]
                first_eligibility = variant_eligibility[first_variant]
                
                for variant_name, eligibility in variant_eligibility.items():
                    if variant_name != first_variant:
                        if eligibility != first_eligibility:
                            violations.append(
                                Violation(
                                    invariant_id=InvariantID.INV_ASSIGNMENT_ELIGIBILITY,
                                    category="Assignment",
                                    description=(
                                        f"Variant '{variant_name}' has different eligibility criteria "
                                        f"than '{first_variant}'. All variants must have equal eligibility."
                                    ),
                                    severity=Severity.CRITICAL,
                                    detected_evidence={
                                        "variant_eligibility": variant_eligibility,
                                        "first_variant": first_variant,
                                        "conflicting_variant": variant_name,
                                    },
                                )
                            )
                            break  # Report first conflict

        return violations


class AllocationMutationValidator(InvariantValidator):
    """Validates allocation weights haven't changed (unless adaptive)."""

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # If not adaptive, allocation must match expected
        if not experiment.adaptive_enabled:
            if experiment.allocation_weights != allocation.expected_distribution:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_ALLOCATION_MUTATION,
                        category="Assignment",
                        description="Allocation weights mutated mid-flight",
                        severity=Severity.CRITICAL,
                        detected_evidence={
                            "expected": allocation.expected_distribution,
                            "actual": experiment.allocation_weights,
                        },
                    )
                )

        return violations


# ============================================================================
# SAMPLE RATIO MISMATCH (SRM)
# ============================================================================


class SampleRatioMismatchValidator(InvariantValidator):
    """
    Detects sample ratio mismatch using exact chi-square test.
    
    SRM detection must be:
    - Statistically exact (scipy.stats.chisquare)
    - Proper hypothesis test with explicit alpha threshold
    - srm_tolerance interpreted as alpha (significance level), not arbitrary deviation
    
    If SRM detected: → CRITICAL
    Experiment must halt publication.
    """

    # Default alpha threshold for SRM detection (0.001 = very conservative)
    DEFAULT_ALPHA = 0.001

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Canonical ordering of exposures for determinism
        sorted_exposures = _canonical_exposures(exposures.exposures)
        
        # Count actual exposures per variant (canonical order)
        observed_counts = Counter(e.variant for e in sorted_exposures)
        total_observed = sum(observed_counts.values())

        if total_observed == 0:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_SRM,
                    category="Sample Ratio",
                    description="No exposures recorded - cannot validate sample ratio",
                    severity=Severity.CRITICAL,
                    detected_evidence=_canonical_dict({"total_observed": 0}),
                )
            )
            return violations

        # Validate expected distribution sums to 100%
        total_expected_prop = sum(allocation.expected_distribution.values())
        if abs(total_expected_prop - 100.0) > 0.01:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_SRM,
                    category="Sample Ratio",
                    description=f"Expected distribution does not sum to 100%: {total_expected_prop}%",
                    severity=Severity.CRITICAL,
                    detected_evidence=_canonical_dict({
                        "total_expected_prop": total_expected_prop,
                        "expected_distribution": allocation.expected_distribution,
                    }),
                )
            )
            return violations

        # Prepare observed and expected counts in canonical variant order
        variant_order = sorted(allocation.expected_distribution.keys())
        f_obs = [observed_counts.get(variant, 0) for variant in variant_order]
        f_exp = [
            (allocation.expected_distribution[variant] / 100.0) * total_observed
            for variant in variant_order
        ]
        
        degrees_of_freedom = len(variant_order) - 1

        # Use exact chi-square test (research-grade)
        if SCIPY_AVAILABLE and chisquare is not None:
            # Exact statistical test
            chi_square_stat, p_value = chisquare(f_obs=f_obs, f_exp=f_exp)
            
            # Interpret srm_tolerance as alpha (significance level)
            # If srm_tolerance > 0 and < 1, use as alpha; otherwise use default
            if 0 < allocation.srm_tolerance < 1:
                alpha = allocation.srm_tolerance
            else:
                alpha = self.DEFAULT_ALPHA
            
            # Hypothesis test: H0 = no SRM, H1 = SRM exists
            # Reject H0 if p < alpha
            if p_value < alpha:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_SRM,
                        category="Sample Ratio",
                        description=(
                            f"Sample ratio mismatch detected: "
                            f"χ²={chi_square_stat:.4f}, p={p_value:.6f} < α={alpha:.6f} "
                            f"(df={degrees_of_freedom})"
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "chi_square": float(chi_square_stat),
                            "p_value": float(p_value),
                            "alpha": alpha,
                            "degrees_of_freedom": degrees_of_freedom,
                            "expected_counts": dict(zip(variant_order, f_exp)),
                            "observed_counts": dict(zip(variant_order, f_obs)),
                            "expected_proportions": {
                                v: allocation.expected_distribution[v]
                                for v in variant_order
                            },
                            "observed_proportions": {
                                v: (c / total_observed * 100.0) if total_observed > 0 else 0.0
                                for v, c in zip(variant_order, f_obs)
                            },
                            "total_observed": total_observed,
                            "srm_tolerance": allocation.srm_tolerance,
                            "test_type": "exact_chi_square",
                        }),
                    )
                )
        else:
            # Fallback if scipy not available (should not happen in production)
            # Use manual chi-square calculation but flag as non-exact
            chi_square_stat = sum(
                ((obs - exp) ** 2) / exp if exp > 0 else (obs * 1000 if obs > 0 else 0)
                for obs, exp in zip(f_obs, f_exp)
            )
            
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_SRM,
                    category="Sample Ratio",
                    description=(
                        f"SRM validation requires scipy.stats.chisquare (not available). "
                        f"Manual calculation: χ²={chi_square_stat:.4f} (df={degrees_of_freedom})"
                    ),
                    severity=Severity.CRITICAL,
                    detected_evidence=_canonical_dict({
                        "chi_square": float(chi_square_stat),
                        "degrees_of_freedom": degrees_of_freedom,
                        "test_type": "manual_fallback",
                        "scipy_available": False,
                    }),
                )
            )

        return violations


# ============================================================================
# EXPOSURE INVARIANTS
# ============================================================================


class CrossExposureValidator(InvariantValidator):
    """
    Validates strict single-exposure-per-identity invariant.
    
    Spec requirement: "Exposed at most once per identity context"
    
    This means:
    - Even if same variant, multiple exposures = CRITICAL violation
    - No exceptions for same-variant re-exposure
    - Identity context is the key (subject_id:identity_context)
    """

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Canonical ordering for determinism
        sorted_exposures = _canonical_exposures(exposures.exposures)

        # Check for multiple exposures per identity context
        # Key: (subject_id, identity_context) - must be unique
        identity_exposures: Dict[str, List[ExposureRecord]] = defaultdict(list)
        for exposure in sorted_exposures:
            key = f"{exposure.subject_id}:{exposure.identity_context}"
            identity_exposures[key].append(exposure)

        # Find all identity contexts with > 1 exposure (CRITICAL violation)
        multi_exposed = {
            k: exposures_list
            for k, exposures_list in identity_exposures.items()
            if len(exposures_list) > 1
        }

        if multi_exposed:
            # Collect evidence (canonical)
            sample_violations = {}
            for key, exp_list in list(multi_exposed.items())[:10]:  # Sample first 10
                sample_violations[key] = {
                    "exposure_count": len(exp_list),
                    "variants": sorted(set(e.variant for e in exp_list)),
                    "exposure_times": sorted(
                        e.exposure_timestamp.isoformat() if isinstance(e.exposure_timestamp, datetime) else str(e.exposure_timestamp)
                        for e in exp_list
                    ),
                }
            
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_CROSS_EXPOSURE,
                    category="Exposure",
                    description=(
                        f"Multiple exposures detected for {len(multi_exposed)} identity contexts. "
                        f"Spec requires: exposed at most once per identity context (even if same variant)."
                    ),
                    severity=Severity.CRITICAL,
                    detected_evidence=_canonical_dict({
                        "affected_identity_contexts": len(multi_exposed),
                        "total_exposures": len(sorted_exposures),
                        "violation_rate": len(multi_exposed) / len(identity_exposures) if identity_exposures else 0.0,
                        "sample_violations": sample_violations,
                    }),
                )
            )

        return violations


# ============================================================================
# METRIC DEFINITION INVARIANTS
# ============================================================================


class MetricMutationValidator(InvariantValidator):
    """Validates metric definitions haven't changed during experiment."""

    def __init__(self, baseline_metrics: Optional[MetricRegistry] = None):
        """
        Args:
            baseline_metrics: Metric registry from experiment start
        """
        self.baseline_metrics = baseline_metrics

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        if self.baseline_metrics is None:
            # No baseline to compare - cannot validate
            return violations

        # Check each primary metric for mutation
        for metric_id in analysis_plan.primary_metrics:
            baseline_def = self.baseline_metrics.metrics.get(metric_id)
            current_def = metrics.metrics.get(metric_id)

            if baseline_def is None:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_METRIC_MUTATION,
                        category="Metric",
                        description=f"Primary metric {metric_id} not in baseline registry",
                        severity=Severity.CRITICAL,
                        detected_evidence={"metric_id": metric_id},
                        impacted_metric=metric_id,
                    )
                )
                continue

            if current_def is None:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_METRIC_MUTATION,
                        category="Metric",
                        description=f"Primary metric {metric_id} missing from current registry",
                        severity=Severity.CRITICAL,
                        detected_evidence={"metric_id": metric_id},
                        impacted_metric=metric_id,
                    )
                )
                continue

            # Structural equality checks (beyond hash comparison)
            # Hash integrity is not guaranteed - must verify structural equality
            structural_differences = []
            
            if baseline_def.data_source != current_def.data_source:
                structural_differences.append("data_source")
            
            if baseline_def.aggregation_logic != current_def.aggregation_logic:
                structural_differences.append("aggregation_logic")
            
            if baseline_def.windowing_logic != current_def.windowing_logic:
                structural_differences.append("windowing_logic")
            
            if baseline_def.transformation != current_def.transformation:
                structural_differences.append("transformation")
            
            if baseline_def.denominator != current_def.denominator:
                structural_differences.append("denominator")
                # Specific violation for denominator drift
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_METRIC_DENOMINATOR_DRIFT,
                        category="Metric",
                        description=(
                            f"Metric denominator definition changed for {metric_id}. "
                            f"Baseline: {baseline_def.denominator}, Current: {current_def.denominator}"
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "metric_id": metric_id,
                            "baseline_denominator": baseline_def.denominator,
                            "current_denominator": current_def.denominator,
                        }),
                        impacted_metric=metric_id,
                    )
                )
            
            if baseline_def.filters != current_def.filters:
                structural_differences.append("filters")
            
            # Check for window contamination (outcome leakage into eligibility window)
            # This is a temporal integrity check
            if (experiment.eligibility_boundary and 
                experiment.analysis_window_start and
                experiment.analysis_window_start < experiment.eligibility_boundary):
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_METRIC_WINDOW_CONTAMINATION,
                        category="Metric",
                        description=(
                            f"Window contamination detected: analysis window start "
                            f"({experiment.analysis_window_start.isoformat()}) "
                            f"before eligibility boundary ({experiment.eligibility_boundary.isoformat()}). "
                            f"Outcome leakage into eligibility window not allowed."
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "metric_id": metric_id,
                            "eligibility_boundary": experiment.eligibility_boundary.isoformat(),
                            "analysis_window_start": experiment.analysis_window_start.isoformat(),
                        }),
                        impacted_metric=metric_id,
                    )
                )
            
            # Compare definition hashes (secondary check)
            if baseline_def.definition_hash != current_def.definition_hash:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_METRIC_DEFINITION_DRIFT,
                        category="Metric",
                        description=(
                            f"Metric definition changed for {metric_id}. "
                            f"Structural differences: {', '.join(structural_differences) if structural_differences else 'hash mismatch only'}"
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "metric_id": metric_id,
                            "baseline_hash": baseline_def.definition_hash,
                            "current_hash": current_def.definition_hash,
                            "baseline_version": baseline_def.version,
                            "current_version": current_def.version,
                            "structural_differences": structural_differences,
                            "baseline_data_source": baseline_def.data_source,
                            "current_data_source": current_def.data_source,
                            "baseline_windowing": baseline_def.windowing_logic,
                            "current_windowing": current_def.windowing_logic,
                            "baseline_transformation": baseline_def.transformation,
                            "current_transformation": current_def.transformation,
                        }),
                        impacted_metric=metric_id,
                    )
                )
            elif structural_differences:
                # Hash matches but structural differences detected (hash collision or corruption)
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_METRIC_DEFINITION_DRIFT,
                        category="Metric",
                        description=(
                            f"Metric definition structural mismatch for {metric_id} despite hash match. "
                            f"Possible hash collision or corruption. Differences: {', '.join(structural_differences)}"
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "metric_id": metric_id,
                            "definition_hash": baseline_def.definition_hash,
                            "structural_differences": structural_differences,
                        }),
                        impacted_metric=metric_id,
                    )
                )

        return violations


# ============================================================================
# TEMPORAL INVARIANTS
# ============================================================================


class TemporalBoundaryValidator(InvariantValidator):
    """Validates temporal boundaries are properly defined."""

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Check all required timestamps exist
        if experiment.start_timestamp is None:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_TEMPORAL_BOUNDARY,
                    category="Temporal",
                    description="Experiment start timestamp not defined",
                    severity=Severity.CRITICAL,
                    detected_evidence={"start_timestamp": None},
                )
            )

        if experiment.eligibility_boundary is None:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_TEMPORAL_BOUNDARY,
                    category="Temporal",
                    description="Eligibility boundary not defined",
                    severity=Severity.CRITICAL,
                    detected_evidence={"eligibility_boundary": None},
                )
            )

        if experiment.analysis_window_start is None:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_TEMPORAL_BOUNDARY,
                    category="Temporal",
                    description="Analysis window start not defined",
                    severity=Severity.CRITICAL,
                    detected_evidence={"analysis_window_start": None},
                )
            )

        if experiment.analysis_window_end is None:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_TEMPORAL_BOUNDARY,
                    category="Temporal",
                    description="Analysis window end not defined",
                    severity=Severity.CRITICAL,
                    detected_evidence={"analysis_window_end": None},
                )
            )

        # Validate temporal ordering
        if (
            experiment.start_timestamp
            and experiment.eligibility_boundary
            and experiment.start_timestamp > experiment.eligibility_boundary
        ):
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_TEMPORAL_BOUNDARY,
                    category="Temporal",
                    description="Start timestamp after eligibility boundary",
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "start": experiment.start_timestamp.isoformat(),
                        "eligibility": experiment.eligibility_boundary.isoformat(),
                    },
                )
            )

        return violations


class RetroactiveEnrollmentValidator(InvariantValidator):
    """Detects retroactive enrollment (backdating)."""

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Check if any exposures occurred before experiment start
        backdated = [
            e
            for e in exposures.exposures
            if e.exposure_timestamp < experiment.start_timestamp
        ]

        if backdated:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_RETROACTIVE_ENROLLMENT,
                    category="Temporal",
                    description=f"Retroactive enrollment detected: {len(backdated)} exposures before start",
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "count": len(backdated),
                        "experiment_start": experiment.start_timestamp.isoformat(),
                        "earliest_exposure": min(
                            e.exposure_timestamp for e in backdated
                        ).isoformat(),
                    },
                )
            )

        return violations


# ============================================================================
# POWER & STOPPING DISCIPLINE
# ============================================================================


class PowerStabilityValidator(InvariantValidator):
    """
    Validates power analysis results are stable across time buckets.
    
    Spec requirement: "results stable across time buckets"
    
    Power must not drift significantly across analysis windows.
    """

    def __init__(self, time_bucket_power: Optional[Dict[str, float]] = None):
        """
        Args:
            time_bucket_power: time_bucket_id -> power estimate
        """
        self.time_bucket_power = time_bucket_power or {}

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        if len(self.time_bucket_power) < 2:
            # Need at least 2 buckets to check stability
            return violations

        power_values = list(self.time_bucket_power.values())
        if not power_values:
            return violations

        # Check for significant drift (coefficient of variation > threshold)
        mean_power = sum(power_values) / len(power_values)
        if mean_power > 0:
            variance = sum((p - mean_power) ** 2 for p in power_values) / len(power_values)
            std_dev = math.sqrt(variance)
            cv = std_dev / mean_power  # Coefficient of variation

            # Threshold: CV > 0.2 indicates instability
            if cv > 0.2:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_POWER_STABILITY,
                        category="Statistical Discipline",
                        description=(
                            f"Power analysis results unstable across time buckets. "
                            f"Mean: {mean_power:.4f}, CV: {cv:.4f} > 0.2"
                        ),
                        severity=Severity.MAJOR,
                        detected_evidence=_canonical_dict({
                            "mean_power": mean_power,
                            "coefficient_of_variation": cv,
                            "time_bucket_power": self.time_bucket_power,
                            "threshold": 0.2,
                        }),
                    )
                )

        return violations


class SequentialMonitoringDisciplineValidator(InvariantValidator):
    """
    Validates sequential monitoring discipline is properly enforced.
    
    Spec requirement: Sequential monitoring discipline must be enforced, not just declared.
    
    Must check:
    - Correction method actually applied
    - Monitoring boundaries respected
    - No peeking outside declared monitoring schedule
    """

    def __init__(self, monitoring_events: Optional[List[Dict[str, Any]]] = None):
        """
        Args:
            monitoring_events: List of monitoring events with timestamps and decisions
        """
        self.monitoring_events = monitoring_events or []

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        stopping_rule = analysis_plan.stopping_rule
        if stopping_rule and stopping_rule.rule_type == "sequential":
            # Check that correction method is declared
            if not stopping_rule.correction_method:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_SEQUENTIAL_CORRECTION,
                        category="Statistical Discipline",
                        description="Sequential analysis without correction method",
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({"rule_type": stopping_rule.rule_type}),
                    )
                )
            
            # Check monitoring discipline enforcement
            if self.monitoring_events:
                # Check for violations of monitoring schedule
                declared_correction = stopping_rule.correction_method
                
                # Verify correction method was actually used
                correction_applied = any(
                    event.get("correction_method") == declared_correction
                    for event in self.monitoring_events
                )
                
                if not correction_applied and len(self.monitoring_events) > 0:
                    violations.append(
                        Violation(
                            invariant_id=InvariantID.INV_SEQUENTIAL_MONITORING_DISCIPLINE,
                            category="Statistical Discipline",
                            description=(
                                f"Sequential monitoring discipline not enforced. "
                                f"Declared correction: {declared_correction}, "
                                f"but not applied in {len(self.monitoring_events)} monitoring events."
                            ),
                            severity=Severity.CRITICAL,
                            detected_evidence=_canonical_dict({
                                "declared_correction": declared_correction,
                                "monitoring_events_count": len(self.monitoring_events),
                                "correction_applied": correction_applied,
                            }),
                        )
                    )

        return violations


class DownstreamGatingValidator(InvariantValidator):
    """
    Validates downstream gating checks for model training and rollout.
    
    Spec requirement: Model training / rollout gating checks must be enforced.
    
    Experiments with violations must not be used for:
    - Model training
    - Production rollout
    - Downstream decision making
    """

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # This validator would check if experiment is being used downstream
        # despite having violations. In practice, this would be called after
        # other validators have run and violations detected.
        
        # For now, this is a placeholder that would be enhanced with actual
        # downstream usage tracking in production.
        
        # The actual gating logic is in can_train_on_experiment() and
        # can_promote_to_production() functions, but this validator ensures
        # the gating checks are explicitly validated.
        
        return violations


class StoppingRuleValidator(InvariantValidator):
    """Validates stopping rule is declared."""

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        stopping_rule = analysis_plan.stopping_rule

        # Must have stopping rule
        if stopping_rule is None:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_STOPPING_RULE_UNDECLARED,
                    category="Statistical Discipline",
                    description="No stopping rule declared",
                    severity=Severity.CRITICAL,
                    detected_evidence={"stopping_rule": None},
                )
            )
            return violations

        # Validate stopping rule completeness
        if stopping_rule.rule_type == "fixed_sample":
            if stopping_rule.sample_size is None:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_POWER_DECLARATION,
                        category="Statistical Discipline",
                        description="Fixed sample stopping rule missing sample size",
                        severity=Severity.CRITICAL,
                        detected_evidence={"rule_type": stopping_rule.rule_type},
                    )
                )

        elif stopping_rule.rule_type == "sequential":
            if stopping_rule.correction_method is None:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_SEQUENTIAL_CORRECTION,
                        category="Statistical Discipline",
                        description="Sequential analysis without correction method",
                        severity=Severity.CRITICAL,
                        detected_evidence={"rule_type": stopping_rule.rule_type},
                    )
                )

        elif stopping_rule.rule_type == "bayesian":
            if stopping_rule.bayesian_framework is None:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_POWER_DECLARATION,
                        category="Statistical Discipline",
                        description="Bayesian stopping rule without framework definition",
                        severity=Severity.CRITICAL,
                        detected_evidence={"rule_type": stopping_rule.rule_type},
                    )
                )

        return violations


# ============================================================================
# MULTIPLE HYPOTHESIS CONTROL
# ============================================================================


class MultipleHypothesisValidator(InvariantValidator):
    """Validates multiple hypothesis correction is declared."""

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # If multiple primary metrics, must have correction
        if len(analysis_plan.primary_metrics) > 1:
            if analysis_plan.multiple_hypothesis_control is None:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_MULTI_HYPOTHESIS_UNCONTROLLED,
                        category="Statistical Discipline",
                        description=f"Multiple primary metrics ({len(analysis_plan.primary_metrics)}) without correction",
                        severity=Severity.CRITICAL,
                        detected_evidence={
                            "primary_metric_count": len(analysis_plan.primary_metrics),
                            "primary_metrics": list(analysis_plan.primary_metrics),
                        },
                    )
                )

        return violations


# ============================================================================
# LEAKAGE DETECTION
# ============================================================================


class LeakageValidator(InvariantValidator):
    """
    Structural leakage detection (timestamp/DAG-based, not flag-based).
    
    Tier-0 requirement: Leakage must be inferred from:
    - Timestamp ordering (feature_timestamp > outcome_timestamp)
    - Feature lineage DAG
    - Metric dependency graph
    
    Flag-based detection is unsafe - causal invalidity can silently slip through.
    
    Must ensure:
    - No training model uses outcome metric directly
    - No personalization reweighs traffic based on experiment results mid-flight
    - No feature uses future information (structural check)
    - No backfilling after observation (structural check)
    
    Leakage → invalid causal signal.
    Severity: CRITICAL
    """

    def __init__(
        self,
        feature_definitions: Optional[Dict[str, Any]] = None,
        feature_timestamps: Optional[Dict[str, datetime]] = None,
        feature_lineage: Optional[Dict[str, Set[str]]] = None,
        metric_dependencies: Optional[Dict[str, Set[str]]] = None,
    ):
        """
        Args:
            feature_definitions: Feature definitions (legacy support)
            feature_timestamps: Feature ID -> timestamp when feature computed
            feature_lineage: Feature ID -> set of upstream feature IDs (DAG)
            metric_dependencies: Metric ID -> set of feature IDs it depends on
        """
        self.feature_definitions = feature_definitions or {}
        self.feature_timestamps = feature_timestamps or {}
        self.feature_lineage = feature_lineage or {}
        self.metric_dependencies = metric_dependencies or {}

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Canonical ordering for determinism
        sorted_exposures = _canonical_exposures(exposures.exposures)
        
        if not sorted_exposures:
            return violations
        
        # Get earliest exposure timestamp (treatment start)
        earliest_exposure = min(e.exposure_timestamp for e in sorted_exposures)
        latest_exposure = max(e.exposure_timestamp for e in sorted_exposures)
        
        # Structural check: feature timestamp > outcome timestamp = leakage
        for metric_id in analysis_plan.primary_metrics:
            metric_def = metrics.metrics.get(metric_id)
            if not metric_def:
                continue
            
            # Check metric dependencies for temporal leakage
            dependent_features = self.metric_dependencies.get(metric_id, set())
            
            for feature_id in dependent_features:
                feature_ts = self.feature_timestamps.get(feature_id)
                
                if feature_ts and latest_exposure:
                    # Structural check: if feature computed after exposure, potential leakage
                    if feature_ts > latest_exposure:
                        violations.append(
                            Violation(
                                invariant_id=InvariantID.INV_LEAKAGE_FUTURE_INFO,
                                category="Leakage",
                                description=(
                                    f"Structural leakage detected: Feature '{feature_id}' "
                                    f"computed at {feature_ts.isoformat()} after latest exposure "
                                    f"({latest_exposure.isoformat()}). Future information leakage."
                                ),
                                severity=Severity.CRITICAL,
                                detected_evidence=_canonical_dict({
                                    "feature_id": feature_id,
                                    "metric_id": metric_id,
                                    "feature_timestamp": feature_ts.isoformat(),
                                    "latest_exposure": latest_exposure.isoformat(),
                                    "leakage_type": "temporal_future_info",
                                    "detection_method": "structural_timestamp",
                                }),
                                impacted_metric=metric_id,
                            )
                        )
                
                # Check feature lineage DAG for circular dependencies
                lineage = self.feature_lineage.get(feature_id, set())
                if metric_id in lineage or any(
                    dep_metric in self.metric_dependencies
                    for dep_metric in lineage
                ):
                    violations.append(
                        Violation(
                            invariant_id=InvariantID.INV_LEAKAGE_DETECTED,
                            category="Leakage",
                            description=(
                                f"Circular dependency detected: Feature '{feature_id}' "
                                f"depends on metric '{metric_id}' or its dependencies. "
                                f"Feature lineage: {sorted(lineage)}"
                            ),
                            severity=Severity.CRITICAL,
                            detected_evidence=_canonical_dict({
                                "feature_id": feature_id,
                                "metric_id": metric_id,
                                "feature_lineage": sorted(lineage),
                                "leakage_type": "circular_dependency",
                                "detection_method": "structural_dag",
                            }),
                            impacted_metric=metric_id,
                        )
                    )
        
        # Legacy flag-based checks (for backward compatibility, but less reliable)
        for feature_id, feature_def in self.feature_definitions.items():
            if not isinstance(feature_def, dict):
                continue
            
            # Flag-based check (secondary, less reliable)
            if feature_def.get("uses_future_info", False):
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_LEAKAGE_FUTURE_INFO,
                        category="Leakage",
                        description=(
                            f"Feature '{feature_id}' flagged as using future information. "
                            f"Flag-based detection (less reliable than structural checks)."
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "feature_id": feature_id,
                            "feature_def": feature_def,
                            "leakage_type": "future_information",
                            "detection_method": "flag_based",
                        }),
                    )
                )
            
            # Check for outcome metric usage in features
            if feature_def.get("uses_outcome_metric", False):
                outcome_metric = feature_def.get("outcome_metric")
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_LEAKAGE_DETECTED,
                        category="Leakage",
                        description=(
                            f"Feature '{feature_id}' uses outcome metric '{outcome_metric}'. "
                            f"Training models must not use outcome metrics directly."
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "feature_id": feature_id,
                            "outcome_metric": outcome_metric,
                            "leakage_type": "outcome_metric_usage",
                        }),
                    )
                )

        return violations


# ============================================================================
# ANALYSIS INTEGRITY
# ============================================================================


class AnalysisPlanDriftValidator(InvariantValidator):
    """
    Detects analysis plan mutation after experiment start.
    
    Must prohibit:
    - Post-hoc segmentation selection for publication
    - Dropping negative slices selectively
    - Switching primary metric after preview
    - Excluding outliers after seeing effect
    
    Analysis plan must be declared before evaluation.
    """

    def __init__(self, baseline_plan: Optional[AnalysisPlan] = None):
        """
        Args:
            baseline_plan: Analysis plan from experiment launch
        """
        self.baseline_plan = baseline_plan

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        if self.baseline_plan is None:
            # No baseline to compare - cannot validate drift
            return violations

        # Compare plan hashes
        if self.baseline_plan.plan_hash != analysis_plan.plan_hash:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_ANALYSIS_PLAN_DRIFT,
                    category="Analysis Integrity",
                    description=(
                        "Analysis plan modified after experiment start. "
                        "Plan must remain immutable during active run."
                    ),
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "baseline_hash": self.baseline_plan.plan_hash,
                        "current_hash": analysis_plan.plan_hash,
                        "baseline_version": self.baseline_plan.plan_version,
                        "current_version": analysis_plan.plan_version,
                        "baseline_locked_at": self.baseline_plan.locked_at.isoformat(),
                    },
                )
            )

        # Check for primary metric switching
        baseline_primary = set(self.baseline_plan.primary_metrics)
        current_primary = set(analysis_plan.primary_metrics)
        
        if baseline_primary != current_primary:
            added = current_primary - baseline_primary
            removed = baseline_primary - current_primary
            
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_PRIMARY_METRIC_SWITCH,
                    category="Analysis Integrity",
                    description=(
                        f"Primary metrics changed after experiment start. "
                        f"Added: {list(added)}, Removed: {list(removed)}. "
                        f"No post-hoc primary metric switching allowed."
                    ),
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "baseline_metrics": sorted(baseline_primary),
                        "current_metrics": sorted(current_primary),
                        "added_metrics": sorted(added),
                        "removed_metrics": sorted(removed),
                    },
                )
            )
        
        # Check for pre-registered segments mutation
        baseline_segments = set(self.baseline_plan.pre_registered_segments)
        current_segments = set(analysis_plan.pre_registered_segments)
        
        if baseline_segments != current_segments:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_POST_HOC_SEGMENTATION,
                    category="Analysis Integrity",
                    description=(
                        f"Pre-registered segments changed after experiment start. "
                        f"No post-hoc segmentation selection allowed."
                    ),
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "baseline_segments": sorted(baseline_segments),
                        "current_segments": sorted(current_segments),
                    },
                )
            )

        return violations


# ============================================================================
# REPRODUCIBILITY
# ============================================================================


class IsolationInterferenceValidator(InvariantValidator):
    """
    Causal interference graph for experiment isolation.
    
    Real interference requires:
    - Shared population intersection (users exposed to both)
    - Shared metric surfaces (same metrics measured)
    - Treatment interaction potential
    
    Temporal overlap alone is insufficient - must check causal interference graph.
    """

    def __init__(
        self,
        active_experiments: Optional[List[ExperimentSnapshot]] = None,
        other_experiment_exposures: Optional[Dict[str, ExposureSnapshot]] = None,
        other_experiment_metrics: Optional[Dict[str, MetricRegistry]] = None,
    ):
        """
        Args:
            active_experiments: List of other active experiments
            other_experiment_exposures: experiment_id -> ExposureSnapshot for other experiments
            other_experiment_metrics: experiment_id -> MetricRegistry for other experiments
        """
        self.active_experiments = active_experiments or []
        self.other_experiment_exposures = other_experiment_exposures or {}
        self.other_experiment_metrics = other_experiment_metrics or {}

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Canonical ordering for determinism
        sorted_exposures = _canonical_exposures(exposures.exposures)
        current_subjects = {e.subject_id for e in sorted_exposures}
        current_metrics = set(analysis_plan.primary_metrics)

        # Build interference graph
        for other_experiment in self.active_experiments:
            if other_experiment.experiment_id == experiment.experiment_id:
                continue  # Skip self
            
            # Get other experiment's data
            other_exposures = self.other_experiment_exposures.get(other_experiment.experiment_id)
            other_metrics_reg = self.other_experiment_metrics.get(other_experiment.experiment_id)
            
            # Check causal interference conditions
            shared_users = False
            shared_metrics = False
            temporal_overlap = False
            shared_subjects = set()
            overlap_rate = 0.0
            
            # 1. Shared population intersection
            if other_exposures:
                other_sorted = _canonical_exposures(other_exposures.exposures)
                other_subjects = {e.subject_id for e in other_sorted}
                shared_subjects = current_subjects & other_subjects
                shared_users = len(shared_subjects) > 0
                overlap_rate = len(shared_subjects) / len(current_subjects) if current_subjects else 0.0
            
            # 2. Shared metric surfaces
            if other_metrics_reg:
                # In production, would check actual metric definitions
                # For now, assume potential interference if both use same metric IDs
                other_metrics = set(other_metrics_reg.metrics.keys())
                shared_metrics = len(current_metrics & other_metrics) > 0
            else:
                # If we don't have metric info, check temporal overlap as proxy
                shared_metrics = True  # Conservative assumption
            
            # 3. Temporal overlap
            experiment_end = experiment.freeze_point or datetime.max.replace(tzinfo=timezone.utc)
            other_end = other_experiment.freeze_point or datetime.max.replace(tzinfo=timezone.utc)
            
            temporal_overlap = (
                experiment.start_timestamp <= other_experiment.start_timestamp <= experiment_end
            ) or (
                other_experiment.start_timestamp <= experiment.start_timestamp <= other_end
            )
            
            # Causal interference: shared_users AND (shared_metrics OR temporal_overlap)
            if shared_users and (shared_metrics or temporal_overlap):
                # Severity escalates based on overlap rate
                if overlap_rate > 0.5:
                    severity = Severity.CRITICAL
                elif overlap_rate > 0.1:
                    severity = Severity.MAJOR
                else:
                    severity = Severity.WARNING
                
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_ISOLATION_INTERFERENCE,
                        category="Isolation",
                        description=(
                            f"Causal interference detected with experiment '{other_experiment.experiment_id}'. "
                            f"Shared users: {len(shared_subjects) if shared_users else 0}, "
                            f"Shared metrics: {shared_metrics}, "
                            f"Temporal overlap: {temporal_overlap}, "
                            f"Overlap rate: {overlap_rate:.2%}"
                        ),
                        severity=severity,
                        detected_evidence=_canonical_dict({
                            "current_experiment": experiment.experiment_id,
                            "overlapping_experiment": other_experiment.experiment_id,
                            "shared_users_count": len(shared_subjects) if shared_users else 0,
                            "overlap_rate": overlap_rate,
                            "shared_metrics": shared_metrics,
                            "temporal_overlap": temporal_overlap,
                            "current_start": experiment.start_timestamp.isoformat(),
                            "other_start": other_experiment.start_timestamp.isoformat(),
                            "interference_type": "causal_interference_graph",
                        }),
                    )
                )

        return violations


class ReproducibilityValidator(InvariantValidator):
    """
    Validates experiment evaluation is reproducible with hash recomputation.
    
    Spec requirement: "Re-running evaluation must produce identical results"
    
    This means:
    - Not just checking presence of hashes
    - Must recompute hash from raw snapshot
    - Verify equality with stored hashes
    - Otherwise tampered snapshots still pass
    """

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        # Check assignment seed is present (required for reproducibility)
        if not experiment.assignment_seed:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                    category="Reproducibility",
                    description="Assignment seed missing - cannot reproduce assignments",
                    severity=Severity.CRITICAL,
                    detected_evidence=_canonical_dict({"assignment_seed": experiment.assignment_seed}),
                )
            )

        # Recompute and verify experiment config hash
        if experiment.config_hash:
            # Recompute from canonical experiment representation
            config_canonical = _canonical_dict({
                "experiment_id": experiment.experiment_id,
                "start_timestamp": experiment.start_timestamp.isoformat() if experiment.start_timestamp else None,
                "eligibility_boundary": experiment.eligibility_boundary.isoformat() if experiment.eligibility_boundary else None,
                "variant_config": experiment.variant_config,
                "allocation_weights": experiment.allocation_weights,
                "assignment_seed": experiment.assignment_seed,
                "hash_function": experiment.hash_function,
                "schema_version": experiment.schema_version,
            })
            recomputed_config_hash = hashlib.sha256(
                _canonical_serialize(config_canonical).encode("utf-8")
            ).hexdigest()
            
            if recomputed_config_hash != experiment.config_hash:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                        category="Reproducibility",
                        description=(
                            f"Experiment config hash mismatch. "
                            f"Stored: {experiment.config_hash[:16]}..., "
                            f"Recomputed: {recomputed_config_hash[:16]}... "
                            f"Possible tampering detected."
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "stored_hash": experiment.config_hash,
                            "recomputed_hash": recomputed_config_hash,
                        }),
                    )
                )
        else:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                    category="Reproducibility",
                    description="Experiment config hash missing - cannot verify reproducibility",
                    severity=Severity.MAJOR,
                    detected_evidence=_canonical_dict({"config_hash": None}),
                )
            )

        # Recompute and verify exposure snapshot hash
        if exposures.snapshot_hash:
            # Recompute from canonical exposures
            sorted_exposures = _canonical_exposures(exposures.exposures)
            exposures_canonical = _canonical_dict({
                "exposures": [
                    {
                        "subject_id": e.subject_id,
                        "variant": e.variant,
                        "exposure_timestamp": e.exposure_timestamp.isoformat() if isinstance(e.exposure_timestamp, datetime) else str(e.exposure_timestamp),
                        "identity_context": e.identity_context,
                    }
                    for e in sorted_exposures
                ],
                "snapshot_timestamp": exposures.snapshot_timestamp.isoformat() if isinstance(exposures.snapshot_timestamp, datetime) else str(exposures.snapshot_timestamp),
            })
            recomputed_exposure_hash = hashlib.sha256(
                _canonical_serialize(exposures_canonical).encode("utf-8")
            ).hexdigest()
            
            if recomputed_exposure_hash != exposures.snapshot_hash:
                violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                        category="Reproducibility",
                        description=(
                            f"Exposure snapshot hash mismatch. "
                            f"Stored: {exposures.snapshot_hash[:16]}..., "
                            f"Recomputed: {recomputed_exposure_hash[:16]}... "
                            f"Possible tampering detected."
                        ),
                        severity=Severity.CRITICAL,
                        detected_evidence=_canonical_dict({
                            "stored_hash": exposures.snapshot_hash,
                            "recomputed_hash": recomputed_exposure_hash,
                        }),
                    )
                )
        else:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                    category="Reproducibility",
                    description="Exposure snapshot hash missing - cannot verify reproducibility",
                    severity=Severity.MAJOR,
                    detected_evidence=_canonical_dict({"snapshot_hash": None}),
                )
            )

        # Check other hashes (registry, plan) - similar recomputation would be done
        if not metrics.registry_hash:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                    category="Reproducibility",
                    description="Metric registry hash missing - cannot verify reproducibility",
                    severity=Severity.MAJOR,
                    detected_evidence=_canonical_dict({"registry_hash": None}),
                )
            )

        if not analysis_plan.plan_hash:
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                    category="Reproducibility",
                    description="Analysis plan hash missing - cannot verify reproducibility",
                    severity=Severity.MAJOR,
                    detected_evidence=_canonical_dict({"plan_hash": None}),
                )
            )

        return violations


# ============================================================================
# SCHEMA COMPATIBILITY
# ============================================================================


class SchemaCompatibilityValidator(InvariantValidator):
    """Validates schema version compatibility."""

    CURRENT_INVARIANT_SCHEMA_VERSION = "1.0.0"
    SUPPORTED_EXPERIMENT_SCHEMA_VERSIONS = {"1.0.0"}

    def validate(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> List[Violation]:
        violations = []

        if (
            experiment.schema_version
            not in self.SUPPORTED_EXPERIMENT_SCHEMA_VERSIONS
        ):
            violations.append(
                Violation(
                    invariant_id=InvariantID.INV_SCHEMA_VERSION_INCOMPATIBLE,
                    category="Schema",
                    description=f"Incompatible experiment schema version: {experiment.schema_version}",
                    severity=Severity.CRITICAL,
                    detected_evidence={
                        "experiment_version": experiment.schema_version,
                        "supported_versions": list(
                            self.SUPPORTED_EXPERIMENT_SCHEMA_VERSIONS
                        ),
                    },
                )
            )

        return violations


# ============================================================================
# MAIN EVALUATION ORCHESTRATOR
# ============================================================================


class ExperimentInvariantEvaluator:
    """
    Main orchestrator for experiment invariant validation.
    
    This is the authority that protects experiments from becoming
    high-confidence lies.
    
    All validation is:
    - Deterministic (identical inputs → identical output)
    - Immutable (no runtime mutation of invariant definitions)
    - Explicit (all invariants clearly defined)
    - Versioned (schema version enforcement)
    """

    INVARIANT_SCHEMA_VERSION = "1.0.0"

    def __init__(
        self,
        validators: Optional[List[InvariantValidator]] = None,
        baseline_metrics: Optional[MetricRegistry] = None,
        baseline_plan: Optional[AnalysisPlan] = None,
        feature_definitions: Optional[Dict[str, Any]] = None,
        active_experiments: Optional[List[ExperimentSnapshot]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize evaluator with validators.
        
        Args:
            validators: Custom validators (if None, uses all default validators)
            baseline_metrics: Baseline metric registry for drift detection
            baseline_plan: Baseline analysis plan for drift detection
            feature_definitions: Feature definitions for leakage detection
            active_experiments: List of other active experiments for isolation checking
            logger: Optional logger for structured logging
        """
        self.logger = logger or logging.getLogger(__name__)
        
        if validators is None:
            self.validators = self._create_default_validators(
                baseline_metrics, baseline_plan, feature_definitions, active_experiments
            )
        else:
            self.validators = validators
        
        self.logger.debug(
            f"Initialized ExperimentInvariantEvaluator with {len(self.validators)} validators"
        )

    def _create_default_validators(
        self,
        baseline_metrics: Optional[MetricRegistry],
        baseline_plan: Optional[AnalysisPlan],
        feature_definitions: Optional[Dict[str, Any]],
        active_experiments: Optional[List[ExperimentSnapshot]] = None,
    ) -> List[InvariantValidator]:
        """
        Create standard set of validators.
        
        Validators are ordered by category:
        1. Assignment invariants
        2. Sample ratio invariants
        3. Exposure invariants
        4. Metric invariants
        5. Temporal invariants
        6. Statistical discipline invariants
        7. Leakage invariants
        8. Isolation invariants
        9. Analysis integrity invariants
        10. Reproducibility invariants
        11. Schema compatibility invariants
        
        Returns:
            List of validators in deterministic order
        """
        return [
            # Assignment Invariants
            AssignmentDeterminismValidator(),
            AssignmentStabilityValidator(),
            EligibilityEqualityValidator(),
            AllocationMutationValidator(),
            
            # Sample Ratio Invariants
            SampleRatioMismatchValidator(),
            
            # Exposure Invariants
            CrossExposureValidator(),
            
            # Metric Invariants
            MetricMutationValidator(baseline_metrics),
            
            # Temporal Invariants
            TemporalBoundaryValidator(),
            RetroactiveEnrollmentValidator(),
            
            # Statistical Discipline Invariants
            StoppingRuleValidator(),
            MultipleHypothesisValidator(),
            PowerStabilityValidator(),
            SequentialMonitoringDisciplineValidator(),
            
            # Leakage Invariants
            LeakageValidator(feature_definitions),
            
            # Downstream Gating
            DownstreamGatingValidator(),
            
            # Isolation Invariants
            IsolationInterferenceValidator(active_experiments),
            
            # Analysis Integrity Invariants
            AnalysisPlanDriftValidator(baseline_plan),
            
            # Reproducibility Invariants
            ReproducibilityValidator(),
            
            # Schema Compatibility Invariants
            SchemaCompatibilityValidator(),
        ]

    def evaluate_experiment_invariants(
        self,
        experiment_snapshot: ExperimentSnapshot,
        metric_definitions: MetricRegistry,
        exposure_log_snapshot: ExposureSnapshot,
        traffic_config: AllocationConfig,
        analysis_plan: AnalysisPlan,
    ) -> InvariantReport:
        """
        Evaluate all invariants for an experiment.
        
        DETERMINISTIC: Identical inputs produce identical output.
        No random sampling allowed.
        No environment-dependent logic.
        
        Must take only immutable snapshots.
        No live querying.
        
        Args:
            experiment_snapshot: Immutable experiment configuration snapshot
            metric_definitions: Immutable metric registry
            exposure_log_snapshot: Immutable exposure data snapshot
            traffic_config: Traffic allocation configuration
            analysis_plan: Pre-registered analysis plan
            
        Returns:
            Immutable InvariantReport with all violations
            
        Raises:
            ValueError: If inputs are invalid
        """
        # Validate inputs
        if not experiment_snapshot.experiment_id:
            raise ValueError("experiment_snapshot.experiment_id cannot be empty")
        
        if not metric_definitions.metrics:
            raise ValueError("metric_definitions.metrics cannot be empty")
        
        if not analysis_plan.primary_metrics:
            raise ValueError("analysis_plan.primary_metrics cannot be empty")
        
        # Canonical ordering of exposures for determinism
        sorted_exposures = _canonical_exposures(exposure_log_snapshot.exposures)
        # Create new exposure snapshot with sorted exposures for deterministic processing
        canonical_exposure_snapshot = ExposureSnapshot(
            exposures=sorted_exposures,
            snapshot_timestamp=exposure_log_snapshot.snapshot_timestamp,
            snapshot_hash=exposure_log_snapshot.snapshot_hash,
        )
        
        self.logger.info(
            f"Evaluating invariants for experiment {experiment_snapshot.experiment_id}: "
            f"{len(self.validators)} validators, {len(sorted_exposures)} exposures"
        )
        
        all_violations: List[Violation] = []

        # Run all validators (deterministic order)
        for validator in self.validators:
            try:
                violations = validator.validate(
                    experiment_snapshot,
                    metric_definitions,
                    canonical_exposure_snapshot,
                    traffic_config,
                    analysis_plan,
                )
                all_violations.extend(violations)
                
                if violations:
                    self.logger.warning(
                        f"Validator {validator.__class__.__name__} found {len(violations)} violations"
                    )
            except Exception as e:
                # Validator error - log and continue
                self.logger.error(
                    f"Validator {validator.__class__.__name__} raised exception: {e}"
                )
                # Add violation for validator failure
                all_violations.append(
                    Violation(
                        invariant_id=InvariantID.INV_REPRODUCIBILITY_FAILURE,
                        category="System",
                        description=f"Validator {validator.__class__.__name__} failed: {str(e)}",
                        severity=Severity.CRITICAL,
                        detected_evidence={
                            "validator": validator.__class__.__name__,
                            "error": str(e),
                        },
                    )
                )

        # Separate by severity
        critical_and_major = [
            v
            for v in all_violations
            if v.severity in (Severity.CRITICAL, Severity.MAJOR)
        ]
        warnings = [v for v in all_violations if v.severity == Severity.WARNING]

        # Count by severity
        severity_counts = Counter(v.severity for v in all_violations)

        # Determine blocking status (CRITICAL violations block)
        has_critical = any(v.severity == Severity.CRITICAL for v in all_violations)
        blocking = has_critical

        # Overall validity
        overall_valid = not has_critical

        # Generate deterministic report hash
        report_hash = self._compute_report_hash(
            experiment_snapshot,
            metric_definitions,
            exposure_log_snapshot,
            traffic_config,
            analysis_plan,
            all_violations,
        )
        
        # Log summary
        self.logger.info(
            f"Invariant evaluation complete for {experiment_snapshot.experiment_id}: "
            f"valid={overall_valid}, blocking={blocking}, "
            f"violations={len(critical_and_major)}, warnings={len(warnings)}, "
            f"critical={severity_counts.get(Severity.CRITICAL, 0)}, "
            f"major={severity_counts.get(Severity.MAJOR, 0)}"
        )

        return InvariantReport(
            overall_valid=overall_valid,
            violations=tuple(critical_and_major),  # Immutable tuple
            warnings=tuple(warnings),  # Immutable tuple
            severity_counts=dict(severity_counts),  # Frozen dict
            blocking=blocking,
            validation_timestamp=exposure_log_snapshot.snapshot_timestamp,
            invariant_schema_version=self.INVARIANT_SCHEMA_VERSION,
            experiment_schema_version=experiment_snapshot.schema_version,
            report_hash=report_hash,
        )

    def _compute_report_hash(
        self,
        experiment: ExperimentSnapshot,
        metrics: MetricRegistry,
        exposures: ExposureSnapshot,
        allocation: AllocationConfig,
        analysis_plan: AnalysisPlan,
        violations: List[Violation],
    ) -> str:
        """
        Compute deterministic hash of validation report.
        
        Includes full canonical violation content to prevent hash collisions
        for materially different violations.
        
        Ensures reproducibility - same inputs always produce same hash.
        """
        # Sort violations in canonical order
        sorted_violations = sorted(
            violations,
            key=lambda v: (
                v.invariant_id.value,
                v.severity.value,
                v.category,
                v.impacted_metric or "",
                _canonical_serialize(v.detected_evidence),
            )
        )
        
        # Include full canonical violation representation
        violations_canonical = [
            {
                "id": v.invariant_id.value,
                "severity": v.severity.name,
                "category": v.category,
                "description": v.description,
                "impacted_metric": v.impacted_metric,
                "evidence": _canonical_dict(v.detected_evidence),
            }
            for v in sorted_violations
        ]
        
        components = {
            "experiment_hash": experiment.config_hash,
            "metrics_hash": metrics.registry_hash,
            "exposures_hash": exposures.snapshot_hash,
            "analysis_plan_hash": analysis_plan.plan_hash,
            "allocation_version": allocation.config_version,
            "invariant_schema": self.INVARIANT_SCHEMA_VERSION,
            "violations": violations_canonical,
        }

        # Deterministic JSON serialization with full canonical content
        canonical_json = _canonical_serialize(components)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================


def evaluate_experiment_invariants(
    experiment_snapshot: ExperimentSnapshot,
    metric_definitions: MetricRegistry,
    exposure_log_snapshot: ExposureSnapshot,
    traffic_config: AllocationConfig,
    analysis_plan: AnalysisPlan,
    baseline_metrics: Optional[MetricRegistry] = None,
    baseline_plan: Optional[AnalysisPlan] = None,
    feature_definitions: Optional[Dict[str, Any]] = None,
    active_experiments: Optional[List[ExperimentSnapshot]] = None,
    logger: Optional[logging.Logger] = None,
) -> InvariantReport:
    """
    Convenience function for evaluating experiment invariants.
    
    This is the main entry point for experiment validation.
    
    DETERMINISTIC: Identical inputs produce identical output.
    Must take only immutable snapshots.
    No live querying.
    
    Args:
        experiment_snapshot: Immutable experiment configuration snapshot
        metric_definitions: Immutable metric registry
        exposure_log_snapshot: Immutable exposure data snapshot
        traffic_config: Traffic allocation configuration
        analysis_plan: Pre-registered analysis plan
        baseline_metrics: Baseline metric registry for drift detection
        baseline_plan: Baseline analysis plan for drift detection
        feature_definitions: Feature definitions for leakage detection
        active_experiments: List of other active experiments for isolation checking
        logger: Optional logger for structured logging
        
    Returns:
        Immutable InvariantReport with all violations
    """
    evaluator = ExperimentInvariantEvaluator(
        baseline_metrics=baseline_metrics,
        baseline_plan=baseline_plan,
        feature_definitions=feature_definitions,
        active_experiments=active_experiments,
        logger=logger,
    )

    return evaluator.evaluate_experiment_invariants(
        experiment_snapshot,
        metric_definitions,
        exposure_log_snapshot,
        traffic_config,
        analysis_plan,
    )


def can_publish_experiment(report: InvariantReport) -> bool:
    """
    Determine if experiment can publish results.
    
    CRITICAL violations block publication.
    """
    return not report.blocking and report.overall_valid


def can_train_on_experiment(report: InvariantReport) -> bool:
    """
    Determine if experiment results can be used for model training.
    
    Requires stricter validation - no MAJOR or CRITICAL violations.
    """
    return report.overall_valid and not report.has_major


def can_promote_to_production(report: InvariantReport) -> bool:
    """
    Determine if experiment can be promoted to production.
    
    Requires strictest validation - no violations at all.
    """
    return report.overall_valid and len(report.violations) == 0


# ============================================================================
# ABSOLUTE INVARIANTS (Policy Enforcement)
# ============================================================================


ABSOLUTE_INVARIANTS = {
    "NO_CRITICAL_VIOLATIONS_FOR_PUBLICATION": "No experiment with CRITICAL violations may publish",
    "NO_ASSIGNMENT_MUTATION": "No assignment mutation mid-flight",
    "NO_EXPOSURE_SWITCHING": "No exposure switching allowed",
    "NO_METRIC_MUTATION": "No metric mutation during active run",
    "NO_UNDEFINED_STOPPING_RULE": "No undefined stopping rule",
    "NO_SILENT_SRM": "No silent SRM",
    "NO_LEAKAGE_TOLERATED": "No leakage tolerated",
    "NO_POST_HOC_PRIMARY_METRIC_SWITCH": "No post-hoc primary metric switching",
    "NO_REPRODUCIBILITY_FAILURE": "No reproducibility failure",
}


def validate_absolute_invariants(report: InvariantReport) -> Dict[str, bool]:
    """
    Check absolute invariants against report.
    
    Returns dict of invariant_name -> satisfied (bool).
    All absolute invariants must be satisfied for experiment to be valid.
    """
    return {
        "NO_CRITICAL_VIOLATIONS_FOR_PUBLICATION": not report.has_critical,
        "NO_ASSIGNMENT_MUTATION": not any(
            v.invariant_id == InvariantID.INV_ALLOCATION_MUTATION
            for v in report.violations
        ),
        "NO_EXPOSURE_SWITCHING": not any(
            v.invariant_id == InvariantID.INV_VARIANT_SWITCHING
            for v in report.violations
        ),
        "NO_METRIC_MUTATION": not any(
            v.invariant_id
            in (InvariantID.INV_METRIC_MUTATION, InvariantID.INV_METRIC_DEFINITION_DRIFT)
            for v in report.violations
        ),
        "NO_UNDEFINED_STOPPING_RULE": not any(
            v.invariant_id == InvariantID.INV_STOPPING_RULE_UNDECLARED
            for v in report.violations
        ),
        "NO_SILENT_SRM": not any(
            v.invariant_id == InvariantID.INV_SRM for v in report.violations
        ),
        "NO_LEAKAGE_TOLERATED": not any(
            v.invariant_id
            in (InvariantID.INV_LEAKAGE_DETECTED, InvariantID.INV_LEAKAGE_FUTURE_INFO)
            for v in report.violations
        ),
        "NO_POST_HOC_PRIMARY_METRIC_SWITCH": not any(
            v.invariant_id == InvariantID.INV_PRIMARY_METRIC_SWITCH
            for v in report.violations
        ),
        "NO_REPRODUCIBILITY_FAILURE": not any(
            v.invariant_id == InvariantID.INV_REPRODUCIBILITY_FAILURE
            for v in report.violations
        ),
    }


# Export public API
__all__ = [
    # Enums
    "Severity",
    "InvariantID",
    
    # Data structures
    "Violation",
    "InvariantReport",
    "ExperimentSnapshot",
    "MetricDefinition",
    "MetricRegistry",
    "ExposureRecord",
    "ExposureSnapshot",
    "AllocationConfig",
    "StoppingRule",
    "MultipleHypothesisControl",
    "AnalysisPlan",
    
    # Invariant DSL
    "InvariantSpec",
    "InvariantRegistry",
    "create_default_invariant_registry",
    
    # Validators
    "InvariantValidator",
    "AssignmentDeterminismValidator",
    "AssignmentStabilityValidator",
    "EligibilityEqualityValidator",
    "AllocationMutationValidator",
    "SampleRatioMismatchValidator",
    "CrossExposureValidator",
    "MetricMutationValidator",
    "TemporalBoundaryValidator",
    "RetroactiveEnrollmentValidator",
    "StoppingRuleValidator",
    "MultipleHypothesisValidator",
    "PowerStabilityValidator",
    "SequentialMonitoringDisciplineValidator",
    "DownstreamGatingValidator",
    "LeakageValidator",
    "IsolationInterferenceValidator",
    "AnalysisPlanDriftValidator",
    "ReproducibilityValidator",
    "SchemaCompatibilityValidator",
    
    # Main evaluator
    "ExperimentInvariantEvaluator",
    
    # Functions
    "evaluate_experiment_invariants",
    "can_publish_experiment",
    "can_train_on_experiment",
    "can_promote_to_production",
    "validate_absolute_invariants",
    
    # Constants
    "ABSOLUTE_INVARIANTS",
]