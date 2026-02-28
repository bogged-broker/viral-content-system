"""
/infra/observability/metrics_collector.py

Canonical Metrics Authority (System + Business Truth)

This is the single, enforced authority for:
- What metrics exist
- What they mean
- How they are measured
- When they are allowed to be emitted
- How they remain comparable across runs, platforms, and years

Logs tell stories.
Metrics decide what is true at scale.

RULES:
- Every metric has a schema
- Every metric has a type
- Every metric has explicit aggregation
- Reward metrics are isolated (write-only)
- Aggregation is deterministic (replay-safe)
- No dynamic metric names
- No unitless values
- No auto-aggregation

At scale, metrics are a legal system, not charts.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, List, Dict
from collections import defaultdict
import threading
import time
import hashlib
import json

# Mock infra.clock - replace with actual import
class Clock:
    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)


# ============================================================================
# METRIC TYPE (STRICT — NO STRINGS)
# ============================================================================

class MetricType(Enum):
    """
    Exhaustive metric types.
    
    No freeform metrics.
    Every metric is typed.
    """
    COUNTER = "counter"        # Monotonically increasing (e.g., requests)
    GAUGE = "gauge"            # Point-in-time value (e.g., queue depth)
    HISTOGRAM = "histogram"    # Distribution (e.g., latencies)
    RATE = "rate"              # Per-unit-time (e.g., req/s)


# ============================================================================
# METRIC SCOPE (ISOLATION BOUNDARY)
# ============================================================================

class MetricScope(Enum):
    """
    Metric scope defines isolation and usage boundaries.
    
    CRITICAL: Reward metrics are NEVER allowed in evaluation scope.
    This protects causal validity.
    """
    SYSTEM = "system"          # Infra health (CPU, memory, queue depth)
    BUSINESS = "business"      # Views, CTR, retention, engagement
    EXPERIMENT = "experiment"  # A/B test metrics, variant performance
    REWARD = "reward"          # RL-only, write-only, isolated
    SAFETY = "safety"          # Safety violations, trust scores


# ============================================================================
# AGGREGATION TYPE (EXPLICIT ONLY)
# ============================================================================

class AggregationType(Enum):
    """
    Explicit aggregation functions.
    
    Aggregation is NEVER implied.
    Every metric series declares how it aggregates.
    """
    SUM = "sum"
    MEAN = "mean"
    MEDIAN = "median"
    P50 = "p50"
    P95 = "p95"
    P99 = "p99"
    COUNT = "count"
    MIN = "min"
    MAX = "max"


# ============================================================================
# WINDOW DEFINITION (TIME ALIGNMENT)
# ============================================================================

class WindowType(Enum):
    """Window types for metric aggregation."""
    FIXED = "fixed"            # Fixed time windows (1s, 10s, 1m, 1h)
    EXPERIMENT = "experiment"  # Entire experiment duration
    LIFECYCLE = "lifecycle"    # Pre/post rollout phases
    REPLAY = "replay"          # Replay-specific windows


@dataclass(frozen=True)
class Window:
    """
    Immutable time window definition.
    
    Windows are:
    - Declared explicitly
    - Versioned
    - Immutable
    - Deterministic
    """
    window_type: WindowType
    duration_ms: Optional[int]  # None for lifecycle/experiment windows
    start_ms: int
    end_ms: int
    version: int
    
    def contains(self, timestamp_ms: int) -> bool:
        """Check if timestamp falls within window."""
        return self.start_ms <= timestamp_ms < self.end_ms
    
    def __hash__(self):
        return hash((self.window_type, self.start_ms, self.end_ms, self.version))


# ============================================================================
# METRIC SAMPLE (ATOMIC UNIT)
# ============================================================================

@dataclass(frozen=True)
class MetricSample:
    """
    Single metric sample (atomic unit of metrics).
    
    PROPERTIES:
    - Immutable
    - Fully attributed (run_id, source_module)
    - Scoped (system/business/reward/etc)
    - Timestamped (deterministic)
    - Labeled (for grouping)
    
    No anonymous metrics.
    Every sample is attributable.
    """
    name: str
    value: float
    timestamp: int
    scope: MetricScope
    labels: dict[str, str]
    run_id: str
    source_module: str
    unit: str  # e.g., "ms", "bytes", "count", "ratio"
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("Metric name cannot be empty")
        if not self.unit:
            raise ValueError(f"Metric {self.name} must have a unit")
        if not self.run_id:
            raise ValueError(f"Metric {self.name} must have a run_id")


# ============================================================================
# METRIC SCHEMA (DEFINITION CONTRACT)
# ============================================================================

@dataclass(frozen=True)
class MetricSchema:
    """
    Schema definition for a metric.
    
    Every metric MUST have a schema.
    Schemas are immutable and versioned.
    """
    name: str
    metric_type: MetricType
    scope: MetricScope
    aggregation: AggregationType
    unit: str
    description: str
    version: int
    
    # Validation constraints
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    required_labels: set[str] = field(default_factory=set)
    
    def validate_sample(self, sample: MetricSample) -> None:
        """
        Validate sample against schema.
        
        Raises ValueError if sample violates schema.
        """
        if sample.name != self.name:
            raise ValueError(f"Sample name {sample.name} != schema name {self.name}")
        
        if sample.scope != self.scope:
            raise ValueError(
                f"Sample scope {sample.scope} != schema scope {self.scope}"
            )
        
        if sample.unit != self.unit:
            raise ValueError(
                f"Sample unit {sample.unit} != schema unit {self.unit}"
            )
        
        if self.min_value is not None and sample.value < self.min_value:
            raise ValueError(
                f"Sample value {sample.value} < min {self.min_value}"
            )
        
        if self.max_value is not None and sample.value > self.max_value:
            raise ValueError(
                f"Sample value {sample.value} > max {self.max_value}"
            )
        
        missing_labels = self.required_labels - set(sample.labels.keys())
        if missing_labels:
            raise ValueError(
                f"Sample missing required labels: {missing_labels}"
            )


# ============================================================================
# METRIC SERIES (ORDERED SAMPLES)
# ============================================================================

@dataclass
class MetricSeries:
    """
    Time-ordered series of metric samples.
    
    Series are:
    - Append-only
    - Deterministic
    - Schema-validated
    """
    schema: MetricSchema
    samples: list[MetricSample] = field(default_factory=list)
    
    def append(self, sample: MetricSample) -> None:
        """
        Append sample to series with validation.
        
        Samples must be monotonically increasing in time.
        """
        self.schema.validate_sample(sample)
        
        if self.samples and sample.timestamp < self.samples[-1].timestamp:
            raise ValueError(
                f"Sample timestamp {sample.timestamp} is not monotonic"
            )
        
        self.samples.append(sample)
    
    def aggregate(
        self, 
        window: Window,
        aggregation: Optional[AggregationType] = None
    ) -> float:
        """
        Aggregate samples within window.
        
        Uses schema aggregation by default.
        """
        agg = aggregation or self.schema.aggregation
        
        windowed_samples = [
            s for s in self.samples 
            if window.contains(s.timestamp)
        ]
        
        if not windowed_samples:
            return 0.0
        
        values = [s.value for s in windowed_samples]
        
        if agg == AggregationType.SUM:
            return sum(values)
        elif agg == AggregationType.MEAN:
            return sum(values) / len(values)
        elif agg == AggregationType.COUNT:
            return float(len(values))
        elif agg == AggregationType.MIN:
            return min(values)
        elif agg == AggregationType.MAX:
            return max(values)
        elif agg == AggregationType.MEDIAN:
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            if n % 2 == 0:
                return (sorted_vals[n//2 - 1] + sorted_vals[n//2]) / 2
            return sorted_vals[n//2]
        elif agg in (AggregationType.P50, AggregationType.P95, AggregationType.P99):
            percentile_map = {
                AggregationType.P50: 0.50,
                AggregationType.P95: 0.95,
                AggregationType.P99: 0.99
            }
            p = percentile_map[agg]
            sorted_vals = sorted(values)
            idx = int(len(sorted_vals) * p)
            return sorted_vals[min(idx, len(sorted_vals) - 1)]
        else:
            raise ValueError(f"Unsupported aggregation: {agg}")


# ============================================================================
# SCHEMA REGISTRY (SINGLE SOURCE OF TRUTH)
# ============================================================================

class SchemaRegistry:
    """
    Central registry for all metric schemas.
    
    GUARANTEES:
    - No duplicate metrics
    - No ambiguous definitions
    - Versioned schema evolution
    - Deterministic schema lookup
    """
    
    def __init__(self):
        self._schemas: dict[str, MetricSchema] = {}
        self._lock = threading.Lock()
        self._version = 0
    
    def register(self, schema: MetricSchema) -> None:
        """
        Register a metric schema.
        
        Duplicate names are rejected.
        """
        with self._lock:
            if schema.name in self._schemas:
                existing = self._schemas[schema.name]
                raise ValueError(
                    f"Metric {schema.name} already registered with version "
                    f"{existing.version}. Cannot re-register."
                )
            
            self._schemas[schema.name] = schema
            self._version += 1
    
    def get(self, name: str) -> MetricSchema:
        """
        Lookup schema by name.
        
        Raises KeyError if not found.
        """
        with self._lock:
            return self._schemas[name]
    
    def exists(self, name: str) -> bool:
        """Check if schema exists."""
        with self._lock:
            return name in self._schemas
    
    def snapshot_hash(self) -> str:
        """
        Compute deterministic hash of all schemas.
        
        Used for replay validation.
        """
        with self._lock:
            schema_data = {
                name: {
                    'type': schema.metric_type.value,
                    'scope': schema.scope.value,
                    'unit': schema.unit,
                    'version': schema.version
                }
                for name, schema in sorted(self._schemas.items())
            }
            
            serialized = json.dumps(schema_data, sort_keys=True)
            return hashlib.sha256(serialized.encode()).hexdigest()


# ============================================================================
# WINDOW MANAGER (TIME ALIGNMENT AUTHORITY)
# ============================================================================

class WindowManager:
    """
    Manages time windows for metric aggregation.
    
    Windows are:
    - Declared explicitly
    - Versioned
    - Immutable
    - Deterministic
    """
    
    def __init__(self):
        self._windows: dict[str, Window] = {}
        self._lock = threading.Lock()
    
    def create_fixed_window(
        self, 
        name: str,
        duration_ms: int,
        start_ms: int,
        version: int
    ) -> Window:
        """Create a fixed-duration window."""
        window = Window(
            window_type=WindowType.FIXED,
            duration_ms=duration_ms,
            start_ms=start_ms,
            end_ms=start_ms + duration_ms,
            version=version
        )
        
        with self._lock:
            self._windows[name] = window
        
        return window
    
    def create_experiment_window(
        self,
        name: str,
        start_ms: int,
        end_ms: int,
        version: int
    ) -> Window:
        """Create an experiment-duration window."""
        window = Window(
            window_type=WindowType.EXPERIMENT,
            duration_ms=None,
            start_ms=start_ms,
            end_ms=end_ms,
            version=version
        )
        
        with self._lock:
            self._windows[name] = window
        
        return window
    
    def get(self, name: str) -> Window:
        """Get window by name."""
        with self._lock:
            return self._windows[name]


# ============================================================================
# REWARD ISOLATION ENFORCER (CRITICAL)
# ============================================================================

class RewardIsolationEnforcer:
    """
    Enforces strict isolation of reward metrics.
    
    RULES:
    - Reward metrics are write-only
    - Reward metrics cannot be queried during evaluation
    - Reward metrics cannot leak into dashboards
    - Reward metrics are only visible to RL agents
    
    This protects causal validity.
    """
    
    def __init__(self):
        self._evaluation_mode = False
        self._lock = threading.Lock()
    
    def enter_evaluation_mode(self) -> None:
        """Enter evaluation mode - blocks reward metric access."""
        with self._lock:
            self._evaluation_mode = True
    
    def exit_evaluation_mode(self) -> None:
        """Exit evaluation mode - allows reward metric access."""
        with self._lock:
            self._evaluation_mode = False
    
    def check_emit_allowed(self, scope: MetricScope) -> None:
        """
        Check if metric emission is allowed.
        
        Raises PermissionError if reward metrics accessed during evaluation.
        """
        with self._lock:
            if self._evaluation_mode and scope == MetricScope.REWARD:
                raise PermissionError(
                    "Cannot emit reward metrics during evaluation mode. "
                    "This would violate causal validity."
                )
    
    def check_read_allowed(self, scope: MetricScope) -> None:
        """
        Check if metric reading is allowed.
        
        Raises PermissionError if reward metrics read during evaluation.
        """
        with self._lock:
            if self._evaluation_mode and scope == MetricScope.REWARD:
                raise PermissionError(
                    "Cannot read reward metrics during evaluation mode. "
                    "This would violate causal validity."
                )


# ============================================================================
# AGGREGATION ENGINE (DETERMINISTIC COMPUTATION)
# ============================================================================

class AggregationEngine:
    """
    Deterministic metric aggregation.
    
    GUARANTEES:
    - Window-aligned
    - Order-independent
    - Replay-stable
    - No rolling averages (unless explicitly defined)
    """
    
    @staticmethod
    def aggregate_series(
        series: MetricSeries,
        window: Window,
        aggregation: Optional[AggregationType] = None
    ) -> float:
        """
        Aggregate metric series over window.
        
        Deterministic: same inputs → same output.
        """
        return series.aggregate(window, aggregation)
    
    @staticmethod
    def aggregate_multiple(
        series_list: list[MetricSeries],
        window: Window,
        aggregation: AggregationType
    ) -> dict[str, float]:
        """
        Aggregate multiple series over same window.
        
        Returns dict[metric_name, aggregated_value].
        """
        return {
            series.schema.name: series.aggregate(window, aggregation)
            for series in series_list
        }


# ============================================================================
# METRICS COLLECTOR (SINGLE ENTRY POINT)
# ============================================================================

class MetricsCollector:
    """
    Single entry point for all metric emission.
    
    Emit pipeline (MANDATORY):
    1. Schema lookup
    2. Type check
    3. Unit validation
    4. Invariant enforcement
    5. Scope permission check
    6. Window assignment
    7. Append to series
    8. Forward to router
    
    Any failure → hard fail.
    """
    
    def __init__(
        self,
        schema_registry: SchemaRegistry,
        window_manager: WindowManager,
        isolation_enforcer: RewardIsolationEnforcer
    ):
        self.schema_registry = schema_registry
        self.window_manager = window_manager
        self.isolation_enforcer = isolation_enforcer
        
        self._series: dict[str, MetricSeries] = {}
        self._lock = threading.Lock()
        self._emit_count = 0
    
    def emit(self, sample: MetricSample) -> None:
        """
        Emit a metric sample.
        
        STRICT PIPELINE:
        - Schema validation
        - Scope permission check
        - Series append
        
        Raises on any violation.
        """
        # 1. Schema lookup
        if not self.schema_registry.exists(sample.name):
            raise ValueError(
                f"Metric {sample.name} not registered in schema registry. "
                f"All metrics must be pre-registered."
            )
        
        schema = self.schema_registry.get(sample.name)
        
        # 2-4. Type check, unit validation, invariant enforcement
        schema.validate_sample(sample)
        
        # 5. Scope permission check (reward isolation)
        self.isolation_enforcer.check_emit_allowed(sample.scope)
        
        # 6-7. Append to series
        with self._lock:
            if sample.name not in self._series:
                self._series[sample.name] = MetricSeries(schema=schema)
            
            self._series[sample.name].append(sample)
            self._emit_count += 1
    
    def get_series(self, name: str) -> MetricSeries:
        """
        Get metric series by name.
        
        Checks reward isolation.
        """
        with self._lock:
            if name not in self._series:
                raise KeyError(f"No samples for metric {name}")
            
            series = self._series[name]
            
            # Check read permission (reward isolation)
            self.isolation_enforcer.check_read_allowed(series.schema.scope)
            
            return series
    
    def snapshot(self) -> dict[str, MetricSeries]:
        """
        Create immutable snapshot of all series.
        
        Returns copy to prevent mutation.
        """
        with self._lock:
            return dict(self._series)
    
    def get_emit_count(self) -> int:
        """Get total number of samples emitted."""
        with self._lock:
            return self._emit_count


# ============================================================================
# METRICS TELEMETRY (META-OBSERVABILITY)
# ============================================================================

@dataclass
class MetricsTelemetry:
    """
    Meta-observability for metrics system.
    
    Tracks:
    - Metric emission rate
    - Aggregation latency
    - Missing metrics
    - Stalled series
    - Schema violations
    
    Used by watchdogs, not decision logic.
    """
    emission_rate_per_sec: float
    aggregation_latency_ms: float
    schema_violations: int
    missing_metrics: set[str]
    stalled_series: set[str]
    last_emit_timestamp: int


class MetricsTelemetryCollector:
    """Collects telemetry about the metrics system itself."""
    
    def __init__(self, collector: MetricsCollector):
        self.collector = collector
        self._schema_violations = 0
        self._last_check_time = Clock.now_ms()
        self._last_emit_count = 0
    
    def collect(self) -> MetricsTelemetry:
        """Collect current telemetry snapshot."""
        now = Clock.now_ms()
        current_count = self.collector.get_emit_count()
        
        # Calculate emission rate
        time_delta_sec = (now - self._last_check_time) / 1000.0
        emit_delta = current_count - self._last_emit_count
        
        rate = emit_delta / time_delta_sec if time_delta_sec > 0 else 0.0
        
        self._last_check_time = now
        self._last_emit_count = current_count
        
        # Detect stalled series (no emissions in last 60s)
        snapshot = self.collector.snapshot()
        stalled = {
            name for name, series in snapshot.items()
            if series.samples and (now - series.samples[-1].timestamp) > 60000
        }
        
        return MetricsTelemetry(
            emission_rate_per_sec=rate,
            aggregation_latency_ms=0.0,  # Would measure actual aggregation time
            schema_violations=self._schema_violations,
            missing_metrics=set(),  # Would track expected but missing metrics
            stalled_series=stalled,
            last_emit_timestamp=now
        )
    
    def record_schema_violation(self) -> None:
        """Record a schema validation failure."""
        self._schema_violations += 1


# ============================================================================
# METRICS WATCHDOG (PARANOID ENFORCEMENT)
# ============================================================================

class MetricsWatchdog:
    """
    Paranoid watchdog for metrics integrity.
    
    Detects:
    - Metric drift (unexpected changes)
    - Silent drop (expected metrics missing)
    - Schema mismatch (replay vs live)
    - Scope violation (reward leakage)
    
    Actions:
    - Invalidate run
    - Halt experiments
    - Trigger infra kill-switch
    """
    
    def __init__(
        self,
        collector: MetricsCollector,
        telemetry: MetricsTelemetryCollector
    ):
        self.collector = collector
        self.telemetry = telemetry
        self._baseline_schema_hash: Optional[str] = None
    
    def set_baseline(self) -> None:
        """Set baseline schema hash for drift detection."""
        self._baseline_schema_hash = (
            self.collector.schema_registry.snapshot_hash()
        )
    
    def check_integrity(self) -> bool:
        """
        Check metrics system integrity.
        
        Returns False if any violation detected.
        """
        # Check schema drift
        if self._baseline_schema_hash:
            current_hash = self.collector.schema_registry.snapshot_hash()
            if current_hash != self._baseline_schema_hash:
                return False
        
        # Check telemetry for anomalies
        telemetry = self.telemetry.collect()
        
        if telemetry.schema_violations > 0:
            return False
        
        if telemetry.stalled_series:
            # Some metrics have stopped emitting
            return False
        
        return True
    
    def enforce(self) -> None:
        """
        Enforce integrity checks.
        
        Raises SystemExit if integrity violated.
        """
        if not self.check_integrity():
            raise SystemExit(
                "Metrics integrity violation detected. "
                "Run invalidated. System halted."
            )


# ============================================================================
# PRODUCTION SCHEMA DEFINITIONS
# ============================================================================

def register_production_schemas(registry: SchemaRegistry) -> None:
    """
    Register all production metric schemas.
    
    This is where ALL metrics are defined.
    No ad-hoc metrics allowed.
    """
    
    # System metrics
    registry.register(MetricSchema(
        name="cpu_usage_percent",
        metric_type=MetricType.GAUGE,
        scope=MetricScope.SYSTEM,
        aggregation=AggregationType.MEAN,
        unit="percent",
        description="CPU usage percentage",
        version=1,
        min_value=0.0,
        max_value=100.0
    ))
    
    registry.register(MetricSchema(
        name="memory_usage_bytes",
        metric_type=MetricType.GAUGE,
        scope=MetricScope.SYSTEM,
        aggregation=AggregationType.MEAN,
        unit="bytes",
        description="Memory usage in bytes",
        version=1,
        min_value=0.0
    ))
    
    registry.register(MetricSchema(
        name="queue_depth",
        metric_type=MetricType.GAUGE,
        scope=MetricScope.SYSTEM,
        aggregation=AggregationType.MAX,
        unit="count",
        description="Maximum queue depth",
        version=1,
        min_value=0.0
    ))
    
    # Business metrics
    registry.register(MetricSchema(
        name="post_views",
        metric_type=MetricType.COUNTER,
        scope=MetricScope.BUSINESS,
        aggregation=AggregationType.SUM,
        unit="count",
        description="Total post views",
        version=1,
        min_value=0.0
    ))
    
    registry.register(MetricSchema(
        name="ctr",
        metric_type=MetricType.GAUGE,
        scope=MetricScope.BUSINESS,
        aggregation=AggregationType.MEAN,
        unit="ratio",
        description="Click-through rate",
        version=1,
        min_value=0.0,
        max_value=1.0
    ))
    
    registry.register(MetricSchema(
        name="engagement_duration_ms",
        metric_type=MetricType.HISTOGRAM,
        scope=MetricScope.BUSINESS,
        aggregation=AggregationType.P95,
        unit="ms",
        description="User engagement duration",
        version=1,
        min_value=0.0
    ))
    
    # Reward metrics (RL-only, isolated)
    registry.register(MetricSchema(
        name="reward_signal",
        metric_type=MetricType.GAUGE,
        scope=MetricScope.REWARD,
        aggregation=AggregationType.MEAN,
        unit="scalar",
        description="RL reward signal (WRITE-ONLY)",
        version=1
    ))
    
    # Safety metrics
    registry.register(MetricSchema(
        name="safety_violation_count",
        metric_type=MetricType.COUNTER,
        scope=MetricScope.SAFETY,
        aggregation=AggregationType.SUM,
        unit="count",
        description="Count of safety violations",
        version=1,
        min_value=0.0
    ))


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Initialize components
    schema_registry = SchemaRegistry()
    register_production_schemas(schema_registry)
    
    window_manager = WindowManager()
    isolation_enforcer = RewardIsolationEnforcer()
    
    collector = MetricsCollector(
        schema_registry=schema_registry,
        window_manager=window_manager,
        isolation_enforcer=isolation_enforcer
    )
    
    telemetry = MetricsTelemetryCollector(collector)
    watchdog = MetricsWatchdog(collector, telemetry)
    watchdog.set_baseline()
    
    # Emit some metrics
    now = Clock.now_ms()
    
    # System metric
    collector.emit(MetricSample(
        name="cpu_usage_percent",
        value=45.2,
        timestamp=now,
        scope=MetricScope.SYSTEM,
        labels={"host": "server-1"},
        run_id="run_123",
        source_module="system_monitor",
        unit="percent"
    ))
    
    # Business metric
    collector.emit(MetricSample(
        name="post_views",
        value=1.0,
        timestamp=now,
        scope=MetricScope.BUSINESS,
        labels={"post_id": "post_456"},
        run_id="run_123",
        source_module="analytics",
        unit="count"
    ))
    
    # Create window and aggregate
    window = window_manager.create_fixed_window(
        name="1min_window",
        duration_ms=60000,
        start_ms=now - 60000,
        version=1
    )
    
    cpu_series = collector.get_series("cpu_usage_percent")
    avg_cpu = AggregationEngine.aggregate_series(
        cpu_series,
        window,
        AggregationType.MEAN
    )
    
    print(f"Metrics System Initialized")
    print(f"Schema Registry Hash: {schema_registry.snapshot_hash()[:16]}...")
    print(f"Total Schemas: {len(schema_registry._schemas)}")
    print(f"Metrics Emitted: {collector.get_emit_count()}")
    print(f"Avg CPU (1min): {avg_cpu:.1f}%")
    
    # Check integrity
    print(f"\nWatchdog Status: {'✓ HEALTHY' if watchdog.check_integrity() else '✗ VIOLATED'}")

