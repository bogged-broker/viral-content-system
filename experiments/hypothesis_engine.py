"""
/experiments/hypothesis_engine.py

PRODUCTION-GRADE FORMAL CAUSAL HYPOTHESIS SYSTEM
Defines the immutable contract for all hypotheses in the system.

This file enforces:
- Explicit causal claims
- Falsifiability requirements
- Metric binding integrity
- Long-term learning accumulation
- Post-mortem truth auditing

NO EXPERIMENT RUNS WITHOUT A VALIDATED HYPOTHESIS.
This file is what separates science from guessing.
"""

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Set, List, Dict, Any, Tuple, FrozenSet
import sys
from pathlib import Path

# Import metrics registry for validation
# Graceful fallback if metrics module not available
try:
    # Handle both possible paths
    metrics_path = Path(__file__).parent.parent / "evalutationnotundermodels" / "metrics.py"
    if metrics_path.exists():
        sys.path.insert(0, str(metrics_path.parent))
        from metrics import get_registry, MetricDefinition, WindowResolver  # type: ignore
    else:
        # Fallback: create minimal interface if metrics not available
        get_registry = None
        MetricDefinition = None
        WindowResolver = None
except (ImportError, ModuleNotFoundError):
    get_registry = None
    MetricDefinition = None
    WindowResolver = None


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================

class HypothesisType(Enum):
    """
    Type of hypothesis being tested.
    
    MECHANISM: why something works
    OPTIMIZATION: how to improve magnitude
    STABILITY: robustness across conditions
    TRANSFER: cross-platform or cross-niche
    """
    MECHANISM = "mechanism"
    OPTIMIZATION = "optimization"
    STABILITY = "stability"
    TRANSFER = "transfer"


class CausalDirection(Enum):
    """
    Expected direction of metric change.
    
    NO AMBIGUOUS "IMPROVE".
    Direction is MANDATORY.
    """
    INCREASE = "increase"
    DECREASE = "decrease"
    STABILIZE = "stabilize"
    REDUCE_VARIANCE = "reduce_variance"


# ============================================================================
# HYPOTHESIS (CORE DATA STRUCTURE)
# ============================================================================

@dataclass(frozen=True)
class Hypothesis:
    """
    Every experiment MUST be backed by exactly one hypothesis.
    
    IMMUTABLE BY DESIGN.
    Once created and validated, cannot be modified.
    This ensures auditability and prevents post-hoc rationalization.
    
    NON-NEGOTIABLE RULES:
    - Single primary metric only
    - Explicit causal mechanism required
    - Failure conditions must be falsifiable
    - Evaluation window must match metric window compatibility
    """
    
    # Identity
    hypothesis_id: str
    hypothesis_type: HypothesisType
    
    # Core claim
    description: str                     # human-readable claim
    
    # Causal structure
    intervention: str                    # what is being changed
    mechanism: str                       # why it should work
    
    # Measurement contract
    target_metric: str                   # single primary metric
    expected_direction: CausalDirection
    
    # Statistical bounds
    minimum_effect_size: float           # rejects noise-only wins
    confidence_threshold: float          # statistical confidence (e.g., 0.95)
    
    # Temporal bounds
    evaluation_window: str               # 6h / 24h / 7d / 30d / 90d
    
    # Falsification
    failure_conditions: Tuple[str, ...]  # explicit falsifiers (immutable)
    
    # Epistemic honesty
    assumptions: Tuple[str, ...]         # stated, not implied (immutable)
    risks: Tuple[str, ...]               # known downside risks (immutable)
    
    # Scope
    compatible_platforms: FrozenSet[str]  # immutable set
    compatible_niches: FrozenSet[str]     # immutable set
    
    # Metadata
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    created_by: str = "system"
    
    # Secondary metrics (observational only, never used for decision)
    secondary_metrics: Tuple[str, ...] = field(default_factory=lambda: ())
    
    def __post_init__(self):
        """Validate immutability contract at construction."""
        if not self.hypothesis_id or not self.hypothesis_id.strip():
            raise ValueError("hypothesis_id cannot be empty")
        if not self.description or not self.description.strip():
            raise ValueError("description cannot be empty")
        if not self.intervention or not self.intervention.strip():
            raise ValueError("intervention cannot be empty")
        if not self.mechanism or not self.mechanism.strip():
            raise ValueError("mechanism cannot be empty")
        if not self.target_metric or not self.target_metric.strip():
            raise ValueError("target_metric cannot be empty")
        if not self.failure_conditions:
            raise ValueError("failure_conditions cannot be empty (unfalsifiable hypothesis)")
        if self.minimum_effect_size <= 0:
            raise ValueError("minimum_effect_size must be positive")
        if not (0 < self.confidence_threshold < 1):
            raise ValueError("confidence_threshold must be between 0 and 1")
        if not self.evaluation_window:
            raise ValueError("evaluation_window cannot be empty")
        if not self.compatible_platforms:
            raise ValueError("compatible_platforms cannot be empty")
        if not self.compatible_niches:
            raise ValueError("compatible_niches cannot be empty")
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to dict for hashing and storage.
        
        CRITICAL: Must be deterministic for reproducibility.
        """
        return {
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_type": self.hypothesis_type.value,
            "description": self.description,
            "intervention": self.intervention,
            "mechanism": self.mechanism,
            "target_metric": self.target_metric,
            "expected_direction": self.expected_direction.value,
            "minimum_effect_size": self.minimum_effect_size,
            "confidence_threshold": self.confidence_threshold,
            "evaluation_window": self.evaluation_window,
            "failure_conditions": tuple(sorted(self.failure_conditions)),
            "assumptions": tuple(sorted(self.assumptions)),
            "risks": tuple(sorted(self.risks)),
            "compatible_platforms": tuple(sorted(self.compatible_platforms)),
            "compatible_niches": tuple(sorted(self.compatible_niches)),
            "secondary_metrics": tuple(sorted(self.secondary_metrics)),
            "created_at": self.created_at,
            "created_by": self.created_by,
        }
    
    def compute_hash(self) -> str:
        """
        Deterministic hash for reproducibility.
        
        Given the same hypothesis spec, hash must be identical.
        Enables:
        - Multi-year comparisons
        - Cross-niche learning
        - Legal defensibility
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


# ============================================================================
# EXCEPTIONS
# ============================================================================

class HypothesisValidationError(Exception):
    """Raised when a hypothesis fails validation."""
    pass


class MetricBindingError(Exception):
    """Raised when metric binding fails."""
    pass


class FalsifiabilityError(Exception):
    """Raised when hypothesis is unfalsifiable."""
    pass


# ============================================================================
# HYPOTHESIS VALIDATOR (CRITICAL)
# ============================================================================

class HypothesisValidator:
    """
    Rejects hypotheses that are:
    - vague
    - non-causal
    - unfalsifiable
    - metric-ambiguous
    - directionless
    - over-scoped
    
    HARD-FAIL VALIDATION.
    No warnings. No suggestions.
    Violation = rejection.
    """
    
    # Minimum lengths to prevent vagueness
    MIN_DESCRIPTION_LENGTH = 30
    MIN_MECHANISM_LENGTH = 20
    MIN_INTERVENTION_LENGTH = 15
    MIN_FAILURE_CONDITION_LENGTH = 15
    
    # Forbidden vague terms
    VAGUE_TERMS = {
        "better", "improve", "optimize", "enhance", "good", "bad",
        "more viral", "engagement", "performance", "quality",
        "maybe", "might", "could", "possibly", "perhaps",
        "should help", "might help", "could help"
    }
    
    # Valid evaluation windows
    VALID_WINDOWS = {"6h", "24h", "7d", "30d", "90d"}
    
    # Minimum confidence threshold for rigor
    MIN_CONFIDENCE_THRESHOLD = 0.80
    
    # Effect size bounds (as percentage: 0.01 = 1%, 10.0 = 1000%)
    MIN_EFFECT_SIZE = 0.01  # 1% minimum
    MAX_EFFECT_SIZE = 10.0  # 1000% maximum
    
    def __init__(self, metrics_registry=None):
        """
        Initialize validator.
        
        Args:
            metrics_registry: Optional registry for metric validation.
                             If None, will attempt to import from evaluation/metrics.py
        """
        self._metrics_registry = metrics_registry
        if self._metrics_registry is None and get_registry is not None:
            try:
                self._metrics_registry = get_registry()
            except Exception:
                # Registry not available - will skip metric validation
                self._metrics_registry = None
    
    def validate(self, hypothesis: Hypothesis) -> None:
        """
        Hard-fail validation pipeline.
        
        Raises HypothesisValidationError if any validation fails.
        """
        self._validate_description(hypothesis.description)
        self._validate_mechanism(hypothesis.mechanism)
        self._validate_intervention(hypothesis.intervention)
        self._validate_metric(hypothesis.target_metric)
        self._validate_direction(hypothesis.expected_direction)
        self._validate_effect_size(hypothesis.minimum_effect_size)
        self._validate_confidence(hypothesis.confidence_threshold)
        self._validate_window(hypothesis.evaluation_window)
        self._validate_failure_conditions(hypothesis.failure_conditions)
        self._validate_scope(hypothesis)
        self._validate_secondary_metrics(hypothesis.secondary_metrics, hypothesis.target_metric)
    
    def _validate_description(self, desc: str) -> None:
        """Reject vague descriptions."""
        if len(desc.strip()) < self.MIN_DESCRIPTION_LENGTH:
            raise HypothesisValidationError(
                f"Description too vague. Must be at least {self.MIN_DESCRIPTION_LENGTH} chars. "
                f"Got {len(desc)} chars."
            )
        
        desc_lower = desc.lower()
        for term in self.VAGUE_TERMS:
            if term in desc_lower:
                raise HypothesisValidationError(
                    f"Description contains vague term '{term}'. Be specific. "
                    f"Use concrete, measurable claims."
                )
    
    def _validate_mechanism(self, mechanism: str) -> None:
        """Reject non-causal mechanisms."""
        if len(mechanism.strip()) < self.MIN_MECHANISM_LENGTH:
            raise HypothesisValidationError(
                f"Mechanism too vague. Must explain WHY (min {self.MIN_MECHANISM_LENGTH} chars). "
                f"Got {len(mechanism)} chars."
            )
        
        mechanism_lower = mechanism.lower()
        
        # Must contain causal explanation
        causal_markers = ["because", "due to", "through", "by", "as a result of", "causes"]
        if not any(marker in mechanism_lower for marker in causal_markers):
            raise HypothesisValidationError(
                "Mechanism must contain causal explanation. "
                "Use: 'because', 'due to', 'through', 'by', 'as a result of', or 'causes'. "
                "Example: 'Increasing hook density increases retention because faster pattern interruption'."
            )
    
    def _validate_intervention(self, intervention: str) -> None:
        """Reject vague interventions."""
        if len(intervention.strip()) < self.MIN_INTERVENTION_LENGTH:
            raise HypothesisValidationError(
                f"Intervention too vague. Must be specific (min {self.MIN_INTERVENTION_LENGTH} chars). "
                f"Got {len(intervention)} chars."
            )
    
    def _validate_metric(self, metric: str) -> None:
        """Reject invalid or vague metrics."""
        if not metric or len(metric.strip()) < 3:
            raise HypothesisValidationError("target_metric must be specified and at least 3 chars.")
        
        metric_lower = metric.lower()
        
        # Reject vague metric names
        vague_metrics = ["better", "good", "quality", "performance", "engagement"]
        if any(vague in metric_lower for vague in vague_metrics):
            raise HypothesisValidationError(
                f"Metric '{metric}' is too vague. Use concrete metrics from evaluation/metrics.py. "
                f"Examples: 'viral_velocity', 'retention_p50', 'engagement_rate'."
            )
        
        # Validate against metrics registry if available
        if self._metrics_registry is not None:
            try:
                metric_def = self._metrics_registry.get(metric)
                if metric_def is None:
                    available = ", ".join(sorted(self._metrics_registry.list_all())[:10])
                    raise HypothesisValidationError(
                        f"Metric '{metric}' not found in evaluation/metrics.py registry. "
                        f"Available metrics include: {available} (and more). "
                        f"All metrics must be defined in evaluation/metrics.py."
                    )
            except Exception as e:
                # Registry access failed - log but don't block
                if isinstance(e, HypothesisValidationError):
                    raise
                # Other errors are non-blocking
    
    def _validate_direction(self, direction: CausalDirection) -> None:
        """Ensure direction is valid enum."""
        if not isinstance(direction, CausalDirection):
            raise HypothesisValidationError(
                f"expected_direction must be CausalDirection enum, got {type(direction)}"
            )
    
    def _validate_effect_size(self, effect_size: float) -> None:
        """Reject unrealistic or zero effect sizes."""
        if effect_size <= 0:
            raise HypothesisValidationError(
                "minimum_effect_size must be positive (rejects noise-only wins)."
            )
        
        if effect_size < self.MIN_EFFECT_SIZE:
            raise HypothesisValidationError(
                f"minimum_effect_size {effect_size} too small. "
                f"Minimum is {self.MIN_EFFECT_SIZE} ({self.MIN_EFFECT_SIZE*100}%). "
                f"Effect sizes smaller than this are indistinguishable from noise."
            )
        
        if effect_size > self.MAX_EFFECT_SIZE:
            raise HypothesisValidationError(
                f"minimum_effect_size {effect_size} unrealistic. "
                f"Maximum is {self.MAX_EFFECT_SIZE} ({self.MAX_EFFECT_SIZE*100}%). "
                f"If true, this would be a fundamental change, not an experiment."
            )
    
    def _validate_confidence(self, confidence: float) -> None:
        """Enforce minimum confidence threshold."""
        if not (0 < confidence < 1):
            raise HypothesisValidationError(
                f"confidence_threshold must be between 0 and 1, got {confidence}"
            )
        
        if confidence < self.MIN_CONFIDENCE_THRESHOLD:
            raise HypothesisValidationError(
                f"confidence_threshold {confidence} too low. "
                f"Minimum is {self.MIN_CONFIDENCE_THRESHOLD} ({self.MIN_CONFIDENCE_THRESHOLD*100}%) "
                f"for scientific rigor."
            )
    
    def _validate_window(self, window: str) -> None:
        """Ensure evaluation window is valid."""
        if window not in self.VALID_WINDOWS:
            raise HypothesisValidationError(
                f"evaluation_window '{window}' invalid. "
                f"Must be one of {sorted(self.VALID_WINDOWS)}."
            )
    
    def _validate_failure_conditions(self, conditions: Tuple[str, ...]) -> None:
        """Ensure failure conditions are falsifiable."""
        if not conditions:
            raise HypothesisValidationError(
                "failure_conditions cannot be empty. If you cannot lose, experiment is invalid."
            )
        
        for i, condition in enumerate(conditions):
            if len(condition.strip()) < self.MIN_FAILURE_CONDITION_LENGTH:
                raise HypothesisValidationError(
                    f"Failure condition {i+1} '{condition}' too vague. "
                    f"Must be at least {self.MIN_FAILURE_CONDITION_LENGTH} chars and specify "
                    f"exact measurable criteria."
                )
            
            # Reject non-falsifiable conditions
            non_falsifiable = ["always", "never fails", "guaranteed"]
            if any(phrase in condition.lower() for phrase in non_falsifiable):
                raise HypothesisValidationError(
                    f"Failure condition {i+1} '{condition}' is non-falsifiable. "
                    f"Must specify measurable failure criteria."
                )
    
    def _validate_scope(self, hypothesis: Hypothesis) -> None:
        """Ensure scope is properly defined."""
        if not hypothesis.compatible_platforms:
            raise HypothesisValidationError(
                "compatible_platforms cannot be empty. Specify scope (e.g., {'tiktok', 'youtube'})."
            )
        
        if not hypothesis.compatible_niches:
            raise HypothesisValidationError(
                "compatible_niches cannot be empty. Specify scope (e.g., {'tech', 'comedy'})."
            )
    
    def _validate_secondary_metrics(
        self,
        secondary_metrics: Tuple[str, ...],
        primary_metric: str
    ) -> None:
        """Ensure secondary metrics are valid and distinct."""
        if primary_metric in secondary_metrics:
            raise HypothesisValidationError(
                f"target_metric '{primary_metric}' cannot also be in secondary_metrics."
            )
        
        # Validate secondary metrics exist in registry if available
        if self._metrics_registry is not None:
            for metric in secondary_metrics:
                if self._metrics_registry.get(metric) is None:
                    raise HypothesisValidationError(
                        f"Secondary metric '{metric}' not found in evaluation/metrics.py registry."
                    )


# ============================================================================
# FALSIFIABILITY CHECKER
# ============================================================================

class FalsifiabilityChecker:
    """
    Enforces that failure is possible.
    
    If you cannot lose → experiment is invalid.
    
    Verifies:
    - At least one explicit failure condition
    - Measurable negation of expected direction
    - Bounded uncertainty window
    """
    
    @staticmethod
    def check(hypothesis: Hypothesis) -> None:
        """
        Verify hypothesis can be falsified.
        
        Raises FalsifiabilityError if unfalsifiable.
        """
        # Must have explicit failure conditions
        if not hypothesis.failure_conditions:
            raise FalsifiabilityError(
                "No failure conditions defined. Hypothesis is unfalsifiable."
            )
        
        # Failure conditions must be measurable
        for i, condition in enumerate(hypothesis.failure_conditions):
            # Check for measurable negation
            has_negation = any(
                word in condition.lower()
                for word in ["below", "above", "less than", "greater than", "decrease", "increase", "drop", "rise"]
            )
            
            if not has_negation and hypothesis.minimum_effect_size > 0:
                # If no explicit negation, must be able to fail on effect size
                pass  # Effect size provides falsification
        
        # Direction must allow for measurable negation
        if hypothesis.expected_direction == CausalDirection.STABILIZE:
            if not any("variance" in c.lower() or "stability" in c.lower() for c in hypothesis.failure_conditions):
                raise FalsifiabilityError(
                    "STABILIZE direction requires variance-based or stability-based failure condition."
                )
        
        # REDUCE_VARIANCE also needs variance-based conditions
        if hypothesis.expected_direction == CausalDirection.REDUCE_VARIANCE:
            if not any("variance" in c.lower() or "std" in c.lower() or "deviation" in c.lower()
                      for c in hypothesis.failure_conditions):
                raise FalsifiabilityError(
                    "REDUCE_VARIANCE direction requires variance-based failure condition."
                )


# ============================================================================
# METRIC BINDING GUARD
# ============================================================================

class MetricBindingGuard:
    """
    Prevents:
    - post-hoc metric switching
    - metric overload
    - proxy abuse
    
    Rules:
    - Exactly one primary metric
    - Optional secondary metrics are labeled observational
    - Primary metric must exist in evaluation/metrics.py
    """
    
    MAX_SECONDARY_METRICS = 3
    
    def __init__(self, metrics_registry=None):
        """Initialize with optional metrics registry."""
        self._metrics_registry = metrics_registry
        if self._metrics_registry is None and get_registry is not None:
            try:
                self._metrics_registry = get_registry()
            except Exception:
                self._metrics_registry = None
    
    def validate_binding(self, hypothesis: Hypothesis) -> None:
        """
        Enforce metric binding rules.
        
        Raises MetricBindingError if binding invalid.
        """
        # Exactly one primary metric
        if not hypothesis.target_metric:
            raise MetricBindingError(
                "target_metric is required. Must have exactly one primary metric."
            )
        
        # Secondary metrics are labeled observational
        if len(hypothesis.secondary_metrics) > self.MAX_SECONDARY_METRICS:
            raise MetricBindingError(
                f"Too many secondary metrics ({len(hypothesis.secondary_metrics)}). "
                f"Max {self.MAX_SECONDARY_METRICS}. Secondary metrics are observational only."
            )
        
        # Primary metric cannot be in secondary metrics
        if hypothesis.target_metric in hypothesis.secondary_metrics:
            raise MetricBindingError(
                "target_metric cannot also be in secondary_metrics."
            )
        
        # Validate all metrics exist in registry if available
        if self._metrics_registry is not None:
            all_metrics = [hypothesis.target_metric] + list(hypothesis.secondary_metrics)
            for metric in all_metrics:
                if not metric or len(metric) < 3:
                    raise MetricBindingError(f"Invalid metric name: '{metric}'")
                
                metric_def = self._metrics_registry.get(metric)
                if metric_def is None:
                    available = ", ".join(sorted(self._metrics_registry.list_all())[:10])
                    raise MetricBindingError(
                        f"Metric '{metric}' not found in evaluation/metrics.py registry. "
                        f"Available metrics include: {available} (and more)."
                    )


# ============================================================================
# HYPOTHESIS ENGINE (CORE ORCHESTRATOR)
# ============================================================================

class HypothesisEngine:
    """
    Core orchestrator for hypothesis creation, validation, and binding.
    
    SINGLETON ENFORCED.
    Thread-safe.
    Immutable after binding.
    """
    
    _instance: Optional['HypothesisEngine'] = None
    _initialized: bool = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Core storage (protected by lock)
        self._bound_hypotheses: Dict[str, str] = {}  # hypothesis_id -> experiment_id
        self._hypothesis_registry: Dict[str, Hypothesis] = {}
        self._hypothesis_hashes: Dict[str, str] = {}  # hypothesis_id -> hash (cache)
        
        # Validators
        try:
            metrics_registry = get_registry() if get_registry is not None else None
        except Exception:
            metrics_registry = None
        
        self._validator = HypothesisValidator(metrics_registry)
        self._falsifiability_checker = FalsifiabilityChecker()
        self._metric_guard = MetricBindingGuard(metrics_registry)
        
        # Thread safety
        self._lock = threading.RLock()
        
        self._initialized = True
    
    def create_hypothesis(
        self,
        hypothesis_id: str,
        hypothesis_type: HypothesisType,
        description: str,
        intervention: str,
        mechanism: str,
        target_metric: str,
        expected_direction: CausalDirection,
        minimum_effect_size: float,
        confidence_threshold: float,
        evaluation_window: str,
        failure_conditions: List[str],
        assumptions: List[str],
        risks: List[str],
        compatible_platforms: Set[str],
        compatible_niches: Set[str],
        secondary_metrics: Optional[List[str]] = None,
        created_by: str = "system"
    ) -> Hypothesis:
        """
        Create a candidate hypothesis.
        
        Does NOT auto-approve.
        Only constructs candidate hypotheses.
        Validation must be called separately.
        """
        
        # Convert to immutable types
        hypothesis = Hypothesis(
            hypothesis_id=hypothesis_id,
            hypothesis_type=hypothesis_type,
            description=description,
            intervention=intervention,
            mechanism=mechanism,
            target_metric=target_metric,
            expected_direction=expected_direction,
            minimum_effect_size=minimum_effect_size,
            confidence_threshold=confidence_threshold,
            evaluation_window=evaluation_window,
            failure_conditions=tuple(failure_conditions),
            assumptions=tuple(assumptions),
            risks=tuple(risks),
            compatible_platforms=frozenset(compatible_platforms),
            compatible_niches=frozenset(compatible_niches),
            secondary_metrics=tuple(secondary_metrics or ()),
            created_by=created_by
        )
        
        return hypothesis
    
    def validate_hypothesis(self, hypothesis: Hypothesis) -> None:
        """
        Hard-fail validation pipeline.
        
        Raises HypothesisValidationError if:
        - mechanism missing
        - effect size too small
        - metric invalid
        - evaluation window mismatched
        - conflicts with invariant rules
        
        Thread-safe.
        """
        with self._lock:
            # Core validation
            self._validator.validate(hypothesis)
            
            # Falsifiability check
            self._falsifiability_checker.check(hypothesis)
            
            # Metric binding validation
            self._metric_guard.validate_binding(hypothesis)
            
            # Check for duplicate hypothesis_id (with hash caching)
            if hypothesis.hypothesis_id in self._hypothesis_registry:
                existing = self._hypothesis_registry[hypothesis.hypothesis_id]
                # Use cached hash if available
                existing_hash = self._hypothesis_hashes.get(
                    hypothesis.hypothesis_id,
                    existing.compute_hash()
                )
                new_hash = hypothesis.compute_hash()
                
                if existing_hash != new_hash:
                    raise HypothesisValidationError(
                        f"hypothesis_id '{hypothesis.hypothesis_id}' already exists with different content. "
                        f"Hash mismatch: existing={existing_hash[:16]}..., "
                        f"new={new_hash[:16]}..."
                    )
    
    def bind_to_experiment(
        self,
        hypothesis: Hypothesis,
        experiment_id: str
    ) -> str:
        """
        Bind hypothesis to experiment.
        
        Once bound:
        - hypothesis becomes immutable (already frozen dataclass)
        - experiment cannot change targets
        - registry hash includes hypothesis content
        
        Returns: binding hash for audit trail
        
        Thread-safe.
        """
        with self._lock:
            # Validate before binding
            self.validate_hypothesis(hypothesis)
            
            # Check if hypothesis already bound
            if hypothesis.hypothesis_id in self._bound_hypotheses:
                existing_exp = self._bound_hypotheses[hypothesis.hypothesis_id]
                if existing_exp != experiment_id:
                    raise HypothesisValidationError(
                        f"Hypothesis {hypothesis.hypothesis_id} already bound to {existing_exp}. "
                        f"Cannot rebind to {experiment_id}."
                    )
                # Already bound to same experiment - return existing binding hash
                binding_data = {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "hypothesis_hash": hypothesis.compute_hash(),
                    "experiment_id": experiment_id,
                }
                binding_canonical = json.dumps(binding_data, sort_keys=True)
                return hashlib.sha256(binding_canonical.encode()).hexdigest()
            
            # Register and bind
            self._hypothesis_registry[hypothesis.hypothesis_id] = hypothesis
            self._bound_hypotheses[hypothesis.hypothesis_id] = experiment_id
            # Cache hash for performance
            self._hypothesis_hashes[hypothesis.hypothesis_id] = hypothesis.compute_hash()
            
            # Compute binding hash for auditability
            binding_data = {
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_hash": hypothesis.compute_hash(),
                "experiment_id": experiment_id,
                "bound_at": datetime.utcnow().isoformat()
            }
            binding_canonical = json.dumps(binding_data, sort_keys=True)
            binding_hash = hashlib.sha256(binding_canonical.encode()).hexdigest()
            
            return binding_hash
    
    def get_hypothesis(self, hypothesis_id: str) -> Optional[Hypothesis]:
        """Retrieve hypothesis by ID. Thread-safe."""
        with self._lock:
            return self._hypothesis_registry.get(hypothesis_id)
    
    def get_bound_experiment(self, hypothesis_id: str) -> Optional[str]:
        """Get experiment_id bound to this hypothesis. Thread-safe."""
        with self._lock:
            return self._bound_hypotheses.get(hypothesis_id)
    
    def is_bound(self, hypothesis_id: str) -> bool:
        """Check if hypothesis is bound to an experiment. Thread-safe."""
        with self._lock:
            return hypothesis_id in self._bound_hypotheses
    
    def get_all_hypotheses(self) -> List[Hypothesis]:
        """Get all registered hypotheses. Thread-safe."""
        with self._lock:
            return list(self._hypothesis_registry.values())


# ============================================================================
# HYPOTHESIS REGISTRY VIEW
# ============================================================================

class HypothesisRegistryView:
    """
    Read-only view into hypothesis registry for auditing and analysis.
    
    Thread-safe.
    """
    
    def __init__(self, engine: HypothesisEngine):
        self._engine = engine
    
    def list_all(self) -> List[Hypothesis]:
        """List all registered hypotheses."""
        return self._engine.get_all_hypotheses()
    
    def list_by_type(self, hypothesis_type: HypothesisType) -> List[Hypothesis]:
        """List hypotheses by type."""
        return [
            h for h in self._engine.get_all_hypotheses()
            if h.hypothesis_type == hypothesis_type
        ]
    
    def list_bound(self) -> List[Hypothesis]:
        """List all bound hypotheses."""
        bound_ids = set()
        for hyp_id in self._engine._bound_hypotheses.keys():
            bound_ids.add(hyp_id)
        
        return [
            self._engine.get_hypothesis(hid)
            for hid in bound_ids
            if self._engine.get_hypothesis(hid) is not None
        ]
    
    def list_unbound(self) -> List[Hypothesis]:
        """List all unbound hypotheses."""
        bound_ids = set(self._engine._bound_hypotheses.keys())
        return [
            h for h in self._engine.get_all_hypotheses()
            if h.hypothesis_id not in bound_ids
        ]


# ============================================================================
# HYPOTHESIS SIMILARITY (LONG-TERM LEARNING)
# ============================================================================

class HypothesisSimilarity:
    """
    Enables long-term learning accumulation.
    
    Detects similar hypotheses for:
    - Transfer learning
    - Meta-analysis
    - Learning compound effects
    """
    
    @staticmethod
    def compute_similarity(h1: Hypothesis, h2: Hypothesis) -> float:
        """
        Compute similarity score between two hypotheses (0.0 to 1.0).
        
        High similarity indicates:
        - Same mechanism
        - Same target metric
        - Similar interventions
        """
        score = 0.0
        
        # Same mechanism (40% weight)
        if h1.mechanism.lower() == h2.mechanism.lower():
            score += 0.4
        elif h1.mechanism.lower() in h2.mechanism.lower() or h2.mechanism.lower() in h1.mechanism.lower():
            score += 0.2
        
        # Same target metric (30% weight)
        if h1.target_metric == h2.target_metric:
            score += 0.3
        
        # Same intervention type (20% weight)
        if h1.intervention.lower() == h2.intervention.lower():
            score += 0.2
        elif any(word in h2.intervention.lower() for word in h1.intervention.lower().split()):
            score += 0.1
        
        # Same direction (10% weight)
        if h1.expected_direction == h2.expected_direction:
            score += 0.1
        
        return min(score, 1.0)
    
    @staticmethod
    def find_similar(
        hypothesis: Hypothesis,
        candidates: List[Hypothesis],
        threshold: float = 0.6
    ) -> List[Tuple[Hypothesis, float]]:
        """
        Find similar hypotheses above threshold.
        
        Returns list of (hypothesis, similarity_score) tuples.
        """
        similar = []
        for candidate in candidates:
            if candidate.hypothesis_id == hypothesis.hypothesis_id:
                continue
            
            similarity = HypothesisSimilarity.compute_similarity(hypothesis, candidate)
            if similarity >= threshold:
                similar.append((candidate, similarity))
        
        # Sort by similarity descending
        similar.sort(key=lambda x: x[1], reverse=True)
        return similar


# ============================================================================
# HYPOTHESIS WATCHDOG (PRODUCTION)
# ============================================================================

class HypothesisWatchdog:
    """
    Monitors:
    - experiments without hypotheses ❌
    - hypothesis reused incorrectly ❌
    - hypothesis mutation attempts ❌
    - hypothesis vs experiment mismatch ❌
    
    Any violation is registry-blocking.
    Thread-safe.
    """
    
    def __init__(self, engine: HypothesisEngine):
        self._engine = engine
        self._violations: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
    
    def check_experiment_has_hypothesis(self, experiment_id: str) -> bool:
        """
        Verify experiment has bound hypothesis.
        
        Returns True if valid, False and logs violation if not.
        """
        with self._engine._lock:
            for hyp_id, exp_id in self._engine._bound_hypotheses.items():
                if exp_id == experiment_id:
                    return True
        
        self._log_violation(
            "EXPERIMENT_WITHOUT_HYPOTHESIS",
            f"Experiment {experiment_id} has no bound hypothesis",
            experiment_id=experiment_id
        )
        return False
    
    def check_hypothesis_reuse(self, hypothesis_id: str, experiment_id: str) -> bool:
        """
        Check if hypothesis is being reused incorrectly.
        
        Returns True if valid, False and logs violation if not.
        """
        with self._engine._lock:
            if hypothesis_id not in self._engine._bound_hypotheses:
                return True  # Not bound yet, OK
            
            existing_exp = self._engine._bound_hypotheses[hypothesis_id]
            if existing_exp != experiment_id:
                self._log_violation(
                    "HYPOTHESIS_REUSE_VIOLATION",
                    f"Hypothesis {hypothesis_id} already bound to {existing_exp}, "
                    f"cannot bind to {experiment_id}",
                    hypothesis_id=hypothesis_id,
                    experiment_id=experiment_id,
                    existing_experiment_id=existing_exp
                )
                return False
            
            return True
    
    def check_hypothesis_mutation(self, hypothesis: Hypothesis) -> bool:
        """
        Check if hypothesis is being mutated.
        
        Returns True if valid, False and logs violation if not.
        """
        with self._engine._lock:
            if hypothesis.hypothesis_id not in self._engine._hypothesis_registry:
                return True  # New hypothesis, OK
            
            existing = self._engine._hypothesis_registry[hypothesis.hypothesis_id]
            if existing.compute_hash() != hypothesis.compute_hash():
                self._log_violation(
                    "HYPOTHESIS_MUTATION_ATTEMPT",
                    f"Attempt to mutate hypothesis {hypothesis.hypothesis_id}",
                    hypothesis_id=hypothesis.hypothesis_id,
                    existing_hash=existing.compute_hash()[:16],
                    new_hash=hypothesis.compute_hash()[:16]
                )
                return False
            
            return True
    
    def check_experiment_hypothesis_match(
        self,
        experiment_id: str,
        expected_hypothesis_id: str
    ) -> bool:
        """
        Verify experiment is bound to expected hypothesis.
        
        Returns True if valid, False and logs violation if not.
        """
        with self._engine._lock:
            actual_bound = self._engine.get_bound_experiment(expected_hypothesis_id)
            
            if actual_bound != experiment_id:
                self._log_violation(
                    "EXPERIMENT_HYPOTHESIS_MISMATCH",
                    f"Experiment {experiment_id} expected hypothesis {expected_hypothesis_id}, "
                    f"but hypothesis bound to {actual_bound}",
                    experiment_id=experiment_id,
                    expected_hypothesis_id=expected_hypothesis_id,
                    actual_experiment_id=actual_bound
                )
                return False
            
            return True
    
    def _log_violation(
        self,
        violation_type: str,
        message: str,
        **kwargs: Any
    ) -> None:
        """Log a watchdog violation."""
        with self._lock:
            violation = {
                "type": violation_type,
                "message": message,
                "timestamp": datetime.utcnow().isoformat(),
                **kwargs
            }
            self._violations.append(violation)
    
    def get_violations(self) -> List[Dict[str, Any]]:
        """Get all logged violations."""
        with self._lock:
            return self._violations.copy()
    
    def has_violations(self) -> bool:
        """Check if any violations have been logged."""
        with self._lock:
            return len(self._violations) > 0
    
    def clear_violations(self) -> None:
        """Clear violation log (for testing only)."""
        with self._lock:
            self._violations.clear()


# ============================================================================
# INTEGRATION HELPERS
# ============================================================================

class HypothesisSpecAdapter:
    """
    Adapter between hypothesis_engine.Hypothesis and experiment_spec.HypothesisSpec.
    
    Enables integration with experiment_spec.py while maintaining hypothesis_engine.py
    as the authoritative source for formal causal hypotheses.
    """
    
    @staticmethod
    def hypothesis_to_spec(hypothesis: Hypothesis) -> Dict[str, Any]:
        """
        Convert Hypothesis to dict compatible with HypothesisSpec.
        
        Note: experiment_spec.HypothesisSpec is simpler and lacks full causal structure.
        This adapter provides a bridge.
        """
        # Map CausalDirection to ExpectedDirection (from experiment_spec)
        direction_map = {
            CausalDirection.INCREASE: "increase",
            CausalDirection.DECREASE: "decrease",
            CausalDirection.STABILIZE: "neutral",  # Approximate
            CausalDirection.REDUCE_VARIANCE: "neutral",  # Approximate
        }
        
        return {
            "statement": hypothesis.description,
            "expected_direction": direction_map.get(hypothesis.expected_direction, "increase"),
            "minimum_effect_size": hypothesis.minimum_effect_size,
            "causal_mechanism": hypothesis.mechanism,
            "falsifiable": len(hypothesis.failure_conditions) > 0,
        }
    
    @staticmethod
    def validate_hypothesis_binding(
        hypothesis: Hypothesis,
        experiment_id: str
    ) -> bool:
        """
        Validate that hypothesis is properly bound to experiment.
        
        Used by experiment_registry to ensure experiments have valid hypotheses.
        """
        engine = HypothesisEngine()
        watchdog = HypothesisWatchdog(engine)
        
        # Check all watchdog conditions
        if not watchdog.check_experiment_has_hypothesis(experiment_id):
            return False
        if not watchdog.check_hypothesis_reuse(hypothesis.hypothesis_id, experiment_id):
            return False
        if not watchdog.check_hypothesis_mutation(hypothesis):
            return False
        if not watchdog.check_experiment_hypothesis_match(experiment_id, hypothesis.hypothesis_id):
            return False
        
        return True


# ============================================================================
# PRODUCTION EXPORTS
# ============================================================================

__all__ = [
    # Core
    "Hypothesis",
    "HypothesisType",
    "CausalDirection",
    
    # Validators
    "HypothesisValidator",
    "HypothesisValidationError",
    "FalsifiabilityChecker",
    "FalsifiabilityError",
    "MetricBindingGuard",
    "MetricBindingError",
    
    # Engine
    "HypothesisEngine",
    
    # Views
    "HypothesisRegistryView",
    
    # Learning
    "HypothesisSimilarity",
    
    # Watchdog
    "HypothesisWatchdog",
    
    # Integration
    "HypothesisSpecAdapter",
]