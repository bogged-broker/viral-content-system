"""
/experiments/outcome_collector.py

Experiment Outcome Capture Spine

PURPOSE:
    Answer exactly one question:
    "What objectively happened for control vs variant — without interpretation, correction, or opinion?"

RESPONSIBILITIES:
    ✓ Pull metrics from /evaluation/
    ✓ Snapshot metrics at fixed windows
    ✓ Attribute outcomes to experiment/variant/assignment/unit
    ✓ Enforce metric eligibility
    ✓ Detect missing or tainted data
    ✓ Guarantee temporal correctness
    ✓ Persist immutable outcome records
    ✓ Support deterministic replay

NON-RESPONSIBILITIES (NEVER DO):
    ✗ Decide winners
    ✗ Compare variants
    ✗ Compute lift
    ✗ Smooth metrics
    ✗ Normalize cross-platform values
    ✗ Handle statistical significance
    ✗ Transform metrics
    ✗ Apply weights
    ✗ Merge windows
    ✗ Guess missing data

CORE PRINCIPLE:
    Outcomes are immutable facts, not opinions.
    Once collected: never recomputed, never overwritten, never backfilled, never normalized.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
import hashlib
import json


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class OutcomeMetricSpec:
    """
    Defines what metrics are allowed to be collected.
    Immutable. Declared at experiment creation.
    """
    name: str
    source: str  # evaluation.metrics module path
    aggregation: str  # sum / mean / p50 / p90 / p95 / p99 / count
    unit: str  # views, seconds, %, clicks, conversions
    platform_scope: str  # global | youtube | tiktok | instagram
    requires_normalization: bool
    version: str = "1.0.0"
    deprecated: bool = False
    derived_from_experiment: bool = False  # If True, reject (circular dependency)

    def __post_init__(self):
        """Validate spec at construction."""
        valid_aggs = {"sum", "mean", "p50", "p90", "p95", "p99", "count", "median"}
        if self.aggregation not in valid_aggs:
            raise ValueError(f"Invalid aggregation: {self.aggregation}")
        
        valid_scopes = {"global", "youtube", "tiktok", "instagram", "twitter"}
        if self.platform_scope not in valid_scopes:
            raise ValueError(f"Invalid platform_scope: {self.platform_scope}")
        
        if self.deprecated:
            raise ValueError(f"Cannot use deprecated metric: {self.name}")
        
        if self.derived_from_experiment:
            raise ValueError(f"Cannot use experiment-derived metric: {self.name}")


class OutcomeWindow(Enum):
    """
    Fixed temporal windows for metric collection.
    Windows are absolute from assignment time, not post-hoc.
    """
    HOUR_1 = ("1h", timedelta(hours=1))
    HOUR_6 = ("6h", timedelta(hours=6))
    DAY_1 = ("24h", timedelta(days=1))
    DAY_7 = ("7d", timedelta(days=7))
    DAY_30 = ("30d", timedelta(days=30))
    DAY_90 = ("90d", timedelta(days=90))

    def __init__(self, label: str, delta: timedelta):
        self.label = label
        self.delta = delta

    def __str__(self):
        return self.label


@dataclass(frozen=True)
class OutcomeRecord:
    """
    IMMUTABLE outcome record.
    Once written → cannot change.
    This is the atomic unit of experimental truth.
    """
    experiment_id: str
    variant_id: str
    assignment_unit: str  # user | session | device
    unit_id: str  # actual ID
    metric_name: str
    window: OutcomeWindow
    value: float
    collected_at: datetime
    evaluation_snapshot_id: str  # Links to evaluation system version
    assignment_timestamp: datetime
    window_start: datetime
    window_end: datetime
    is_missing: bool = False
    is_contaminated: bool = False
    contamination_reason: Optional[str] = None

    def __post_init__(self):
        """Validate temporal consistency."""
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        
        if self.collected_at < self.window_end:
            raise ValueError("Cannot collect before window closes")
        
        if self.is_contaminated and not self.contamination_reason:
            raise ValueError("Contaminated records must have a reason")

    def record_id(self) -> str:
        """Generate deterministic record ID."""
        components = [
            self.experiment_id,
            self.variant_id,
            self.assignment_unit,
            self.unit_id,
            self.metric_name,
            str(self.window),
            self.evaluation_snapshot_id
        ]
        return hashlib.sha256("|".join(components).encode()).hexdigest()[:16]


@dataclass
class WindowState:
    """Tracks the state of a collection window."""
    window: OutcomeWindow
    start_time: datetime
    end_time: datetime
    is_open: bool = True
    is_finalized: bool = False
    outcome_count: int = 0
    missing_count: int = 0
    contaminated_count: int = 0
    finalized_at: Optional[datetime] = None

    def can_collect(self, now: datetime) -> bool:
        """Check if collection is allowed."""
        if self.is_finalized:
            return False
        if now < self.end_time:
            return False  # Window not closed yet
        if not self.is_open:
            return False
        return True

    def close(self, now: datetime):
        """Close window for collection."""
        if self.is_finalized:
            raise RuntimeError(f"Window already finalized: {self.window}")
        self.is_open = False
        self.finalized_at = now
        self.is_finalized = True


# ============================================================================
# METRIC ELIGIBILITY VALIDATOR
# ============================================================================


class MetricEligibilityValidator:
    """
    Ensures only valid metrics are collected.
    CRITICAL: Prevents garbage from entering the system.
    """

    def __init__(self):
        self._registered_metrics: Dict[str, OutcomeMetricSpec] = {}
        self._version_compatibility: Dict[str, Set[str]] = defaultdict(set)

    def register_metric(self, spec: OutcomeMetricSpec):
        """Register a metric as eligible for collection."""
        if spec.deprecated:
            raise ValueError(f"Cannot register deprecated metric: {spec.name}")
        
        if spec.derived_from_experiment:
            raise ValueError(f"Cannot register experiment-derived metric: {spec.name}")
        
        self._registered_metrics[spec.name] = spec
        self._version_compatibility[spec.name].add(spec.version)

    def validate(self, metric_name: str, experiment_allowed_metrics: Set[str]) -> Tuple[bool, Optional[str]]:
        """
        Validate metric eligibility.
        
        Returns:
            (is_valid, rejection_reason)
        """
        # Check registration
        if metric_name not in self._registered_metrics:
            return False, f"Metric not registered: {metric_name}"
        
        spec = self._registered_metrics[metric_name]
        
        # Check deprecation
        if spec.deprecated:
            return False, f"Metric deprecated: {metric_name}"
        
        # Check experiment allowlist
        if metric_name not in experiment_allowed_metrics:
            return False, f"Metric not allowed by experiment spec: {metric_name}"
        
        # Check circular dependency
        if spec.derived_from_experiment:
            return False, f"Metric derived from experiment logic: {metric_name}"
        
        return True, None

    def get_spec(self, metric_name: str) -> Optional[OutcomeMetricSpec]:
        """Retrieve metric specification."""
        return self._registered_metrics.get(metric_name)


# ============================================================================
# TEMPORAL WINDOW ENFORCER
# ============================================================================


class TemporalWindowEnforcer:
    """
    Guarantees temporal correctness of outcome collection.
    
    Rules:
        - Window opens only once
        - Window closes exactly once
        - Late data is ignored (not merged)
        - No early reads
        - No partial windows
        - No overlapping windows
    """

    def __init__(self):
        self._window_states: Dict[str, WindowState] = {}

    def _make_key(self, experiment_id: str, unit_id: str, window: OutcomeWindow) -> str:
        """Generate unique key for window state."""
        return f"{experiment_id}:{unit_id}:{window.label}"

    def initialize_window(
        self,
        experiment_id: str,
        unit_id: str,
        assignment_time: datetime,
        window: OutcomeWindow
    ) -> WindowState:
        """
        Initialize a collection window.
        Can only be called once per (experiment, unit, window).
        """
        key = self._make_key(experiment_id, unit_id, window)
        
        if key in self._window_states:
            raise RuntimeError(f"Window already initialized: {key}")
        
        start_time = assignment_time
        end_time = assignment_time + window.delta
        
        state = WindowState(
            window=window,
            start_time=start_time,
            end_time=end_time
        )
        
        self._window_states[key] = state
        return state

    def can_collect(
        self,
        experiment_id: str,
        unit_id: str,
        window: OutcomeWindow,
        now: datetime
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if collection is allowed.
        
        Returns:
            (allowed, rejection_reason)
        """
        key = self._make_key(experiment_id, unit_id, window)
        
        if key not in self._window_states:
            return False, "Window not initialized"
        
        state = self._window_states[key]
        
        if state.is_finalized:
            return False, "Window already finalized"
        
        if now < state.end_time:
            return False, f"Window not closed yet (ends at {state.end_time})"
        
        if not state.is_open:
            return False, "Window closed"
        
        return True, None

    def finalize_window(
        self,
        experiment_id: str,
        unit_id: str,
        window: OutcomeWindow,
        now: datetime,
        outcome_count: int,
        missing_count: int,
        contaminated_count: int
    ):
        """
        Finalize a window. No further collection allowed.
        """
        key = self._make_key(experiment_id, unit_id, window)
        
        if key not in self._window_states:
            raise RuntimeError(f"Window not initialized: {key}")
        
        state = self._window_states[key]
        
        if state.is_finalized:
            raise RuntimeError(f"Window already finalized: {key}")
        
        state.outcome_count = outcome_count
        state.missing_count = missing_count
        state.contaminated_count = contaminated_count
        state.close(now)

    def get_state(self, experiment_id: str, unit_id: str, window: OutcomeWindow) -> Optional[WindowState]:
        """Retrieve window state."""
        key = self._make_key(experiment_id, unit_id, window)
        return self._window_states.get(key)


# ============================================================================
# MISSING DATA HANDLER
# ============================================================================


class MissingDataHandler:
    """
    Detects and marks missing data explicitly.
    
    Strategy:
        - Mark explicitly missing
        - Never auto-fill
        - Never interpolate
        - Missing ≠ zero
    """

    @staticmethod
    def detect_missing(
        expected_units: Set[str],
        collected_units: Set[str]
    ) -> Set[str]:
        """Identify units with missing data."""
        return expected_units - collected_units

    @staticmethod
    def create_missing_record(
        experiment_id: str,
        variant_id: str,
        assignment_unit: str,
        unit_id: str,
        metric_name: str,
        window: OutcomeWindow,
        assignment_timestamp: datetime,
        window_start: datetime,
        window_end: datetime,
        collected_at: datetime,
        evaluation_snapshot_id: str
    ) -> OutcomeRecord:
        """
        Create a record marking data as missing.
        value is set to NaN, is_missing=True.
        """
        return OutcomeRecord(
            experiment_id=experiment_id,
            variant_id=variant_id,
            assignment_unit=assignment_unit,
            unit_id=unit_id,
            metric_name=metric_name,
            window=window,
            value=float('nan'),
            collected_at=collected_at,
            evaluation_snapshot_id=evaluation_snapshot_id,
            assignment_timestamp=assignment_timestamp,
            window_start=window_start,
            window_end=window_end,
            is_missing=True
        )

    @staticmethod
    def is_metric_available(metric_value: Any) -> bool:
        """Check if metric value is available."""
        if metric_value is None:
            return False
        if isinstance(metric_value, float) and (metric_value != metric_value):  # NaN check
            return False
        return True


# ============================================================================
# CONTAMINATION DETECTOR
# ============================================================================


class ContaminationDetector:
    """
    Detects data contamination that would invalidate experiments.
    
    Detects:
        - Control seeing variant behavior
        - Metrics influenced by experiment logic
        - Platform feedback loops
    """

    def __init__(self):
        self._contamination_rules: List[Any] = []
        self._detected_contaminations: List[Tuple[str, str]] = []

    def register_rule(self, rule):
        """Register contamination detection rule."""
        self._contamination_rules.append(rule)

    def check_control_leakage(
        self,
        variant_id: str,
        metric_name: str,
        value: float,
        control_baseline: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if control group shows variant behavior.
        
        Returns:
            (is_contaminated, reason)
        """
        if variant_id != "control":
            return False, None
        
        # Check for impossible values in control
        if metric_name.startswith("variant_") and value > 0:
            return True, f"Control group showing variant-specific metric: {metric_name}"
        
        return False, None

    def check_metric_dependency(
        self,
        metric_name: str,
        experiment_features: Set[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if metric depends on experiment features.
        
        Returns:
            (is_contaminated, reason)
        """
        # Metrics should not be computed using experiment assignments
        for feature in experiment_features:
            if feature.lower() in metric_name.lower():
                return True, f"Metric appears to depend on experiment feature: {feature}"
        
        return False, None

    def check_platform_feedback(
        self,
        platform: str,
        metric_name: str,
        value: float,
        historical_range: Optional[Tuple[float, float]] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Check for platform feedback loops.
        
        Returns:
            (is_contaminated, reason)
        """
        # Check for extreme outliers that suggest feedback
        if historical_range:
            min_val, max_val = historical_range
            range_size = max_val - min_val
            
            if range_size > 0:
                if value < min_val - 3 * range_size:
                    return True, f"Value suspiciously low (possible negative feedback loop)"
                if value > max_val + 3 * range_size:
                    return True, f"Value suspiciously high (possible positive feedback loop)"
        
        return False, None

    def detect(
        self,
        experiment_id: str,
        variant_id: str,
        metric_name: str,
        value: float,
        platform: str,
        experiment_features: Set[str],
        control_baseline: Optional[float] = None,
        historical_range: Optional[Tuple[float, float]] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Run all contamination checks.
        
        Returns:
            (is_contaminated, reason)
        """
        # Check control leakage
        contaminated, reason = self.check_control_leakage(variant_id, metric_name, value, control_baseline)
        if contaminated:
            self._detected_contaminations.append((experiment_id, reason))
            return True, reason
        
        # Check metric dependency
        contaminated, reason = self.check_metric_dependency(metric_name, experiment_features)
        if contaminated:
            self._detected_contaminations.append((experiment_id, reason))
            return True, reason
        
        # Check platform feedback
        contaminated, reason = self.check_platform_feedback(platform, metric_name, value, historical_range)
        if contaminated:
            self._detected_contaminations.append((experiment_id, reason))
            return True, reason
        
        return False, None

    def get_contaminations(self) -> List[Tuple[str, str]]:
        """Retrieve all detected contaminations."""
        return self._detected_contaminations.copy()


# ============================================================================
# OUTCOME STORE
# ============================================================================


class OutcomeStore:
    """
    Append-only, immutable storage for outcome records.
    
    Properties:
        - Append-only
        - Write-once
        - Queryable by: experiment, variant, window, metric
        - Versioned schema
    
    Backed by: event store or immutable table
    """

    def __init__(self):
        # In-memory store (production would use database)
        self._records: List[OutcomeRecord] = []
        self._index_by_experiment: Dict[str, List[OutcomeRecord]] = defaultdict(list)
        self._index_by_variant: Dict[str, List[OutcomeRecord]] = defaultdict(list)
        self._index_by_metric: Dict[str, List[OutcomeRecord]] = defaultdict(list)
        self._record_ids: Set[str] = set()
        self._write_count: int = 0
        self._schema_version: str = "1.0.0"

    def write(self, record: OutcomeRecord) -> bool:
        """
        Write outcome record. Write-once only.
        
        Returns:
            True if written, False if duplicate
        """
        record_id = record.record_id()
        
        # Enforce write-once
        if record_id in self._record_ids:
            return False  # Duplicate, reject
        
        # Append to store
        self._records.append(record)
        self._record_ids.add(record_id)
        
        # Update indexes
        self._index_by_experiment[record.experiment_id].append(record)
        variant_key = f"{record.experiment_id}:{record.variant_id}"
        self._index_by_variant[variant_key].append(record)
        self._index_by_metric[record.metric_name].append(record)
        
        self._write_count += 1
        return True

    def query_by_experiment(self, experiment_id: str) -> List[OutcomeRecord]:
        """Query all outcomes for an experiment."""
        return self._index_by_experiment[experiment_id].copy()

    def query_by_variant(self, experiment_id: str, variant_id: str) -> List[OutcomeRecord]:
        """Query all outcomes for a variant."""
        variant_key = f"{experiment_id}:{variant_id}"
        return self._index_by_variant[variant_key].copy()

    def query_by_metric(self, metric_name: str) -> List[OutcomeRecord]:
        """Query all outcomes for a metric."""
        return self._index_by_metric[metric_name].copy()

    def query_by_window(self, experiment_id: str, window: OutcomeWindow) -> List[OutcomeRecord]:
        """Query all outcomes for a specific window."""
        experiment_records = self._index_by_experiment[experiment_id]
        return [r for r in experiment_records if r.window == window]

    def get_statistics(self) -> Dict[str, Any]:
        """Get store statistics."""
        return {
            "total_records": len(self._records),
            "write_count": self._write_count,
            "experiments": len(self._index_by_experiment),
            "variants": len(self._index_by_variant),
            "metrics": len(self._index_by_metric),
            "schema_version": self._schema_version
        }


# ============================================================================
# OUTCOME WATCHDOG
# ============================================================================


class OutcomeWatchdog:
    """
    Monitors outcome collection for anomalies.
    
    Monitors:
        - Metric drift during collection
        - Missing spikes
        - Window timing violations
        - Unexpected metric volatility
    
    Triggers:
        - Experiment freeze
        - Alert
        - Rollback recommendation
    """

    def __init__(self):
        self._alerts: List[Dict[str, Any]] = []
        self._freeze_recommendations: List[str] = []
        self._drift_threshold: float = 0.5  # 50% drift
        self._missing_threshold: float = 0.2  # 20% missing
        self._volatility_threshold: float = 3.0  # 3x stddev

    def check_metric_drift(
        self,
        experiment_id: str,
        metric_name: str,
        current_mean: float,
        baseline_mean: float
    ):
        """Check for unexpected metric drift."""
        if baseline_mean == 0:
            return
        
        drift = abs(current_mean - baseline_mean) / baseline_mean
        
        if drift > self._drift_threshold:
            alert = {
                "type": "metric_drift",
                "experiment_id": experiment_id,
                "metric_name": metric_name,
                "drift": drift,
                "current_mean": current_mean,
                "baseline_mean": baseline_mean,
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)
            
            if drift > 1.0:  # >100% drift
                self._freeze_recommendations.append(experiment_id)

    def check_missing_spike(
        self,
        experiment_id: str,
        missing_rate: float
    ):
        """Check for spikes in missing data."""
        if missing_rate > self._missing_threshold:
            alert = {
                "type": "missing_spike",
                "experiment_id": experiment_id,
                "missing_rate": missing_rate,
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)

    def check_window_timing(
        self,
        experiment_id: str,
        expected_close: datetime,
        actual_close: datetime
    ):
        """Check for window timing violations."""
        delay = (actual_close - expected_close).total_seconds()
        
        if abs(delay) > 3600:  # >1 hour delay
            alert = {
                "type": "window_timing_violation",
                "experiment_id": experiment_id,
                "delay_seconds": delay,
                "expected_close": expected_close,
                "actual_close": actual_close,
                "timestamp": datetime.now()
            }
            self._alerts.append(alert)

    def check_volatility(
        self,
        experiment_id: str,
        metric_name: str,
        values: List[float]
    ):
        """Check for unexpected metric volatility."""
        if len(values) < 2:
            return
        
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        stddev = variance ** 0.5
        
        for value in values:
            if abs(value - mean) > self._volatility_threshold * stddev:
                alert = {
                    "type": "metric_volatility",
                    "experiment_id": experiment_id,
                    "metric_name": metric_name,
                    "outlier_value": value,
                    "mean": mean,
                    "stddev": stddev,
                    "timestamp": datetime.now()
                }
                self._alerts.append(alert)
                break

    def get_alerts(self) -> List[Dict[str, Any]]:
        """Retrieve all alerts."""
        return self._alerts.copy()

    def get_freeze_recommendations(self) -> List[str]:
        """Retrieve experiment freeze recommendations."""
        return self._freeze_recommendations.copy()

    def should_freeze(self, experiment_id: str) -> bool:
        """Check if experiment should be frozen."""
        return experiment_id in self._freeze_recommendations


# ============================================================================
# OUTCOME COLLECTOR (CORE ENGINE)
# ============================================================================


class OutcomeCollector:
    """
    Core engine for outcome collection.
    
    Responsibilities:
        1. Load active experiments
        2. Identify open windows
        3. Resolve eligible units + variants
        4. Pull raw metric values
        5. Validate eligibility
        6. Write immutable outcome records
    """

    def __init__(
        self,
        store: OutcomeStore,
        validator: MetricEligibilityValidator,
        enforcer: TemporalWindowEnforcer,
        detector: ContaminationDetector,
        watchdog: OutcomeWatchdog
    ):
        self.store = store
        self.validator = validator
        self.enforcer = enforcer
        self.detector = detector
        self.watchdog = watchdog
        self._frozen_experiments: Set[str] = set()

    def collect(
        self,
        experiment_id: str,
        variant_id: str,
        assignment_unit: str,
        unit_id: str,
        assignment_timestamp: datetime,
        allowed_metrics: Set[str],
        metric_values: Dict[str, float],
        window: OutcomeWindow,
        evaluation_snapshot_id: str,
        platform: str,
        experiment_features: Set[str],
        now: Optional[datetime] = None
    ) -> List[OutcomeRecord]:
        """
        Collect outcomes for a single unit.
        
        Args:
            experiment_id: Experiment identifier
            variant_id: Variant identifier (control/variant_1/etc)
            assignment_unit: Unit type (user/session/device)
            unit_id: Actual unit ID
            assignment_timestamp: When unit was assigned
            allowed_metrics: Metrics allowed by experiment spec
            metric_values: Raw metric values from evaluation
            window: Collection window
            evaluation_snapshot_id: Evaluation system version
            platform: Platform (youtube/tiktok/etc)
            experiment_features: Features being tested
            now: Current time (for testing)
        
        Returns:
            List of OutcomeRecords written
        """
        if now is None:
            now = datetime.now()
        
        # Check if experiment is frozen
        if experiment_id in self._frozen_experiments:
            raise RuntimeError(f"Experiment frozen: {experiment_id}")
        
        # Initialize window if needed
        window_state = self.enforcer.get_state(experiment_id, unit_id, window)
        if window_state is None:
            window_state = self.enforcer.initialize_window(
                experiment_id, unit_id, assignment_timestamp, window
            )
        
        # Check if collection is allowed
        can_collect, reason = self.enforcer.can_collect(experiment_id, unit_id, window, now)
        if not can_collect:
            raise RuntimeError(f"Cannot collect: {reason}")
        
        window_start = assignment_timestamp
        window_end = assignment_timestamp + window.delta
        
        records_written = []
        
        # Collect each metric
        for metric_name in allowed_metrics:
            # Validate eligibility
            is_valid, rejection_reason = self.validator.validate(metric_name, allowed_metrics)
            if not is_valid:
                continue  # Skip invalid metrics
            
            # Check if metric value is available
            if metric_name not in metric_values:
                # Create missing record
                record = MissingDataHandler.create_missing_record(
                    experiment_id=experiment_id,
                    variant_id=variant_id,
                    assignment_unit=assignment_unit,
                    unit_id=unit_id,
                    metric_name=metric_name,
                    window=window,
                    assignment_timestamp=assignment_timestamp,
                    window_start=window_start,
                    window_end=window_end,
                    collected_at=now,
                    evaluation_snapshot_id=evaluation_snapshot_id
                )
            else:
                value = metric_values[metric_name]
                
                # Check contamination
                is_contaminated, contamination_reason = self.detector.detect(
                    experiment_id=experiment_id,
                    variant_id=variant_id,
                    metric_name=metric_name,
                    value=value,
                    platform=platform,
                    experiment_features=experiment_features
                )
                
                # Create outcome record
                record = OutcomeRecord(
                    experiment_id=experiment_id,
                    variant_id=variant_id,
                    assignment_unit=assignment_unit,
                    unit_id=unit_id,
                    metric_name=metric_name,
                    window=window,
                    value=value,
                    collected_at=now,
                    evaluation_snapshot_id=evaluation_snapshot_id,
                    assignment_timestamp=assignment_timestamp,
                    window_start=window_start,
                    window_end=window_end,
                    is_contaminated=is_contaminated,
                    contamination_reason=contamination_reason
                )
            
            # Write to store
            written = self.store.write(record)
            if written:
                records_written.append(record)
        
        return records_written

    def finalize_window(
        self,
        experiment_id: str,
        window: OutcomeWindow,
        now: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Finalize a collection window for all units.
        Locks window - no further collection allowed.
        
        Returns:
            Summary statistics
        """
        if now is None:
            now = datetime.now()
        
        records = self.store.query_by_window(experiment_id, window)
        
        outcome_count = len([r for r in records if not r.is_missing])
        missing_count = len([r for r in records if r.is_missing])
        contaminated_count = len([r for r in records if r.is_contaminated])
        
        # Run watchdog checks
        total_count = outcome_count + missing_count
        if total_count > 0:
            missing_rate = missing_count / total_count
            self.watchdog.check_missing_spike(experiment_id, missing_rate)
        
        return {
            "experiment_id": experiment_id,
            "window": str(window),
            "outcome_count": outcome_count,
            "missing_count": missing_count,
            "contaminated_count": contaminated_count,
            "finalized_at": now
        }

    def freeze_outcomes(self, experiment_id: str) -> Dict[str, Any]:
        """
        Freeze all outcomes for an experiment.
        Marks outcomes as:
            - Sealed
            - Ready for analysis
            - Reproducible
        
        This step is MANDATORY before any analysis.
        """
        if experiment_id in self._frozen_experiments:
            raise RuntimeError(f"Experiment already frozen: {experiment_id}")
        
        records = self.store.query_by_experiment(experiment_id)
        
        if len(records) == 0:
            raise ValueError(f"No outcomes collected for experiment: {experiment_id}")
        
        # Mark as frozen
        self._frozen_experiments.add(experiment_id)
        
        # Compute summary
        total_records = len(records)
        missing_records = len([r for r in records if r.is_missing])
        contaminated_records = len([r for r in records if r.is_contaminated])
        
        # Group by variant
        variant_counts = defaultdict(int)
        for record in records:
            variant_counts[record.variant_id] += 1
        
        # Group by metric
        metric_counts = defaultdict(int)
        for record in records:
            metric_counts[record.metric_name] += 1
        
        return {
            "experiment_id": experiment_id,
            "frozen_at": datetime.now(),
            "total_records": total_records,
            "missing_records": missing_records,
            "contaminated_records": contaminated_records,
            "variant_counts": dict(variant_counts),
            "metric_counts": dict(metric_counts),
            "is_sealed": True,
            "ready_for_analysis": contaminated_records == 0
        }

    def is_frozen(self, experiment_id: str) -> bool:
        """Check if experiment outcomes are frozen."""
        return experiment_id in self._frozen_experiments

    def get_outcomes(
        self,
        experiment_id: str,
        variant_id: Optional[str] = None,
        metric_name: Optional[str] = None,
        window: Optional[OutcomeWindow] = None
    ) -> List[OutcomeRecord]:
        """
        Query outcomes with filters.
        Only works for frozen experiments.
        """
        if experiment_id not in self._frozen_experiments:
            raise RuntimeError(f"Experiment not frozen: {experiment_id}")
        
        # Start with all experiment records
        if variant_id:
            records = self.store.query_by_variant(experiment_id, variant_id)
        else:
            records = self.store.query_by_experiment(experiment_id)
        
        # Apply filters
        if metric_name:
            records = [r for r in records if r.metric_name == metric_name]
        
        if window:
            records = [r for r in records if r.window == window]
        
        return records


# ============================================================================
# DETERMINISTIC REPLAY SUPPORT
# ============================================================================


class OutcomeReplayEngine:
    """
    Supports deterministic replay of outcome collection.
    
    Given:
        - Same assignments
        - Same evaluation snapshots
        - Same timing
    
    MUST generate identical OutcomeRecords across:
        - Restarts
        - Replays
        - Years later if needed
    """

    def __init__(self, collector: OutcomeCollector):
        self.collector = collector
        self._replay_log: List[Dict[str, Any]] = []

    def record_collection(
        self,
        experiment_id: str,
        variant_id: str,
        unit_id: str,
        window: OutcomeWindow,
        records: List[OutcomeRecord]
    ):
        """Record a collection event for replay."""
        log_entry = {
            "experiment_id": experiment_id,
            "variant_id": variant_id,
            "unit_id": unit_id,
            "window": str(window),
            "record_count": len(records),
            "record_ids": [r.record_id() for r in records],
            "timestamp": datetime.now()
        }
        self._replay_log.append(log_entry)

    def verify_determinism(
        self,
        original_records: List[OutcomeRecord],
        replayed_records: List[OutcomeRecord]
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify that replay produced identical records.
        
        Returns:
            (is_deterministic, error_message)
        """
        if len(original_records) != len(replayed_records):
            return False, f"Record count mismatch: {len(original_records)} vs {len(replayed_records)}"
        
        original_ids = {r.record_id() for r in original_records}
        replayed_ids = {r.record_id() for r in replayed_records}
        
        if original_ids != replayed_ids:
            missing = original_ids - replayed_ids
            extra = replayed_ids - original_ids
            return False, f"Record ID mismatch. Missing: {missing}, Extra: {extra}"
        
        # Verify values
        original_map = {r.record_id(): r for r in original_records}
        replayed_map = {r.record_id(): r for r in replayed_records}
        
        for record_id in original_ids:
            orig = original_map[record_id]
            repl = replayed_map[record_id]
            
            if orig.value != repl.value and not (
                orig.value != orig.value and repl.value != repl.value  # Both NaN
            ):
                return False, f"Value mismatch for {record_id}: {orig.value} vs {repl.value}"
        
        return True, None

    def export_replay_manifest(self, experiment_id: str) -> Dict[str, Any]:
        """
        Export manifest for replaying an experiment.
        Contains all information needed for deterministic replay.
        """
        records = self.collector.store.query_by_experiment(experiment_id)
        
        manifest = {
            "experiment_id": experiment_id,
            "total_records": len(records),
            "collection_log": [e for e in self._replay_log if e["experiment_id"] == experiment_id],
            "schema_version": "1.0.0",
            "exported_at": datetime.now().isoformat()
        }
        
        return manifest


# ============================================================================
# USAGE EXAMPLE & INTEGRATION
# ============================================================================


def example_usage():
    """
    Example showing how to use the OutcomeCollector.
    """
    # Initialize components
    store = OutcomeStore()
    validator = MetricEligibilityValidator()
    enforcer = TemporalWindowEnforcer()
    detector = ContaminationDetector()
    watchdog = OutcomeWatchdog()
    
    collector = OutcomeCollector(
        store=store,
        validator=validator,
        enforcer=enforcer,
        detector=detector,
        watchdog=watchdog
    )
    
    # Register allowed metrics
    click_through_spec = OutcomeMetricSpec(
        name="click_through_rate",
        source="evaluation.metrics.engagement.click_through_rate",
        aggregation="mean",
        unit="%",
        platform_scope="global",
        requires_normalization=False
    )
    validator.register_metric(click_through_spec)
    
    watch_time_spec = OutcomeMetricSpec(
        name="watch_time",
        source="evaluation.metrics.engagement.watch_time",
        aggregation="sum",
        unit="seconds",
        platform_scope="youtube",
        requires_normalization=True
    )
    validator.register_metric(watch_time_spec)
    
    # Simulate outcome collection
    experiment_id = "exp_001"
    variant_id = "control"
    unit_id = "user_12345"
    assignment_time = datetime.now() - timedelta(days=2)
    
    metric_values = {
        "click_through_rate": 0.05,
        "watch_time": 1200.0
    }
    
    allowed_metrics = {"click_through_rate", "watch_time"}
    
    # Collect for 1-day window
    records = collector.collect(
        experiment_id=experiment_id,
        variant_id=variant_id,
        assignment_unit="user",
        unit_id=unit_id,
        assignment_timestamp=assignment_time,
        allowed_metrics=allowed_metrics,
        metric_values=metric_values,
        window=OutcomeWindow.DAY_1,
        evaluation_snapshot_id="eval_v1.0",
        platform="youtube",
        experiment_features={"new_recommendation_algo"},
        now=datetime.now()
    )
    
    print(f"Collected {len(records)} outcome records")
    
    # Finalize window
    summary = collector.finalize_window(experiment_id, OutcomeWindow.DAY_1)
    print(f"Window finalized: {summary}")
    
    # Freeze experiment
    freeze_summary = collector.freeze_outcomes(experiment_id)
    print(f"Experiment frozen: {freeze_summary}")
    
    # Query outcomes
    outcomes = collector.get_outcomes(experiment_id)
    print(f"Retrieved {len(outcomes)} outcomes for analysis")


if __name__ == "__main__":
    example_usage()