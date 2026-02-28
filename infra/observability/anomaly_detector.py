"""
/infra/observability/anomaly_detector.py

Infra-Level Anomaly Authority (Signal, Not Guessing)

This is the system's early-warning nerve center. It detects statistical deviation,
rate explosions, distribution collapse, missing signals, and causal contamination.

It produces signals, not actions. Actions belong to health checks and watchdogs.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Tuple, List, Dict
import math
from collections import defaultdict


# ============================================================================
# ENUMS (STRICT — NO STRINGS)
# ============================================================================


class AnomalyType(Enum):
    """Types of anomalies detectable at the infra level."""

    RATE_SPIKE = "rate_spike"
    RATE_DROP = "rate_drop"
    DISTRIBUTION_SHIFT = "distribution_shift"
    DISTRIBUTION_COLLAPSE = "distribution_collapse"
    MISSING_SIGNAL = "missing_signal"
    CORRELATION_BREAK = "correlation_break"
    UNEXPECTED_VARIANCE = "unexpected_variance"


class AnomalySeverity(Enum):
    """Severity levels for anomalies. Mechanical, not emotional."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================================
# CORE DATA STRUCTURES (IMMUTABLE)
# ============================================================================


@dataclass(frozen=True)
class AnomalySignal:
    """
    Immutable anomaly signal. No guesses. No missing context.
    
    This is what gets emitted when reality deviates from design tolerance.
    """

    anomaly_type: AnomalyType
    severity: AnomalySeverity

    metric_name: str
    observed_value: float | None
    expected_range: tuple[float, float] | None

    window_id: str
    timestamp: int

    baseline_version: str


@dataclass(frozen=True)
class BaselineProfile:
    """
    Baselines are explicit, versioned, immutable.
    
    No runtime drift. Baseline updates require registry version bump.
    """

    metric_name: str
    window_type: str

    expected_min: float
    expected_max: float

    variance: float
    version: str


@dataclass(frozen=True)
class WindowProfile:
    """
    Statistical profile of a metric window for detection.
    """

    metric_name: str
    window_id: str

    mean: float
    median: float
    std_dev: float
    
    min_value: float
    max_value: float

    p25: float
    p75: float
    p95: float
    p99: float

    sample_count: int
    zero_count: int

    timestamp: int


# ============================================================================
# BASELINE MANAGER (CRITICAL)
# ============================================================================


class BaselineManager:
    """
    Manages baseline profiles. No runtime drift allowed.
    
    Rules:
    - Baselines are frozen per run
    - Baseline updates require registry version bump
    - If baseline missing → detector refuses to run
    """

    def __init__(self, baseline_registry: dict[str, BaselineProfile]):
        """
        Initialize with a frozen baseline registry.
        
        Args:
            baseline_registry: Immutable mapping of metric_name -> BaselineProfile
        """
        self._baselines = baseline_registry
        self._version = self._compute_version()

    def _compute_version(self) -> str:
        """Compute deterministic version hash of all baselines."""
        # In production: use content hash of all baseline params
        baseline_keys = sorted(self._baselines.keys())
        version_data = "-".join(
            f"{key}:{self._baselines[key].version}" for key in baseline_keys
        )
        return f"baseline-{hash(version_data) & 0xFFFFFFFF:08x}"

    def load_baselines(self) -> dict[str, BaselineProfile]:
        """
        Load frozen baselines for this run.
        
        Returns:
            Immutable baseline registry
        """
        return self._baselines.copy()

    def get_baseline(self, metric_name: str) -> BaselineProfile | None:
        """
        Get baseline for a specific metric.
        
        Args:
            metric_name: Name of the metric
            
        Returns:
            BaselineProfile if exists, None otherwise
        """
        return self._baselines.get(metric_name)

    def require_baseline(self, metric_name: str) -> BaselineProfile:
        """
        Get baseline or raise if missing (detector refuses to run).
        
        Args:
            metric_name: Name of the metric
            
        Returns:
            BaselineProfile
            
        Raises:
            ValueError: If baseline missing
        """
        baseline = self.get_baseline(metric_name)
        if baseline is None:
            raise ValueError(
                f"Baseline missing for metric '{metric_name}'. "
                f"Detector refuses to run without baseline."
            )
        return baseline

    @property
    def version(self) -> str:
        """Get baseline registry version."""
        return self._version


# ============================================================================
# DETECTION ENGINES (STRICT RESPONSIBILITIES)
# ============================================================================


class RateShiftDetector:
    """
    Detects metric spikes and drops.
    
    Uses:
    - Rolling mean deltas
    - Bounded rate-of-change
    """

    def __init__(
        self,
        spike_threshold: float = 3.0,
        drop_threshold: float = 3.0,
    ):
        """
        Initialize rate shift detector.
        
        Args:
            spike_threshold: Number of std devs for spike detection
            drop_threshold: Number of std devs for drop detection
        """
        self._spike_threshold = spike_threshold
        self._drop_threshold = drop_threshold

    def detect(
        self,
        window: WindowProfile,
        baseline: BaselineProfile,
    ) -> list[AnomalySignal]:
        """
        Detect rate spikes and drops.
        
        Args:
            window: Current window profile
            baseline: Baseline profile
            
        Returns:
            List of anomaly signals
        """
        signals = []

        # Rate-of-change calculation
        expected_mean = (baseline.expected_min + baseline.expected_max) / 2.0
        observed_mean = window.mean

        if baseline.variance > 0:
            std_dev = math.sqrt(baseline.variance)
            deviation = (observed_mean - expected_mean) / std_dev

            # Spike detection
            if deviation > self._spike_threshold:
                severity = self._compute_severity(abs(deviation))
                signals.append(
                    AnomalySignal(
                        anomaly_type=AnomalyType.RATE_SPIKE,
                        severity=severity,
                        metric_name=window.metric_name,
                        observed_value=observed_mean,
                        expected_range=(baseline.expected_min, baseline.expected_max),
                        window_id=window.window_id,
                        timestamp=window.timestamp,
                        baseline_version=baseline.version,
                    )
                )

            # Drop detection
            elif deviation < -self._drop_threshold:
                severity = self._compute_severity(abs(deviation))
                signals.append(
                    AnomalySignal(
                        anomaly_type=AnomalyType.RATE_DROP,
                        severity=severity,
                        metric_name=window.metric_name,
                        observed_value=observed_mean,
                        expected_range=(baseline.expected_min, baseline.expected_max),
                        window_id=window.window_id,
                        timestamp=window.timestamp,
                        baseline_version=baseline.version,
                    )
                )

        return signals

    def _compute_severity(self, deviation_magnitude: float) -> AnomalySeverity:
        """Mechanical severity assignment based on deviation magnitude."""
        if deviation_magnitude >= 6.0:
            return AnomalySeverity.CRITICAL
        elif deviation_magnitude >= 4.5:
            return AnomalySeverity.HIGH
        elif deviation_magnitude >= 3.5:
            return AnomalySeverity.MEDIUM
        else:
            return AnomalySeverity.LOW


class DistributionShiftDetector:
    """
    Detects percentile drift, histogram reshaping, entropy collapse.
    
    No KS tests unless deterministic & versioned.
    """

    def __init__(
        self,
        shift_threshold: float = 0.3,
        collapse_threshold: float = 0.1,
    ):
        """
        Initialize distribution shift detector.
        
        Args:
            shift_threshold: Fractional threshold for distribution shift
            collapse_threshold: Threshold for distribution collapse
        """
        self._shift_threshold = shift_threshold
        self._collapse_threshold = collapse_threshold

    def detect(
        self,
        window: WindowProfile,
        baseline: BaselineProfile,
    ) -> list[AnomalySignal]:
        """
        Detect distribution shifts and collapses.
        
        Args:
            window: Current window profile
            baseline: Baseline profile
            
        Returns:
            List of anomaly signals
        """
        signals = []

        # Distribution shift via percentile drift
        expected_range = baseline.expected_max - baseline.expected_min
        if expected_range > 0:
            observed_range = window.max_value - window.min_value
            range_ratio = observed_range / expected_range

            # Shift detection
            if abs(range_ratio - 1.0) > self._shift_threshold:
                severity = self._compute_shift_severity(abs(range_ratio - 1.0))
                signals.append(
                    AnomalySignal(
                        anomaly_type=AnomalyType.DISTRIBUTION_SHIFT,
                        severity=severity,
                        metric_name=window.metric_name,
                        observed_value=observed_range,
                        expected_range=(baseline.expected_min, baseline.expected_max),
                        window_id=window.window_id,
                        timestamp=window.timestamp,
                        baseline_version=baseline.version,
                    )
                )

        # Distribution collapse (variance collapse)
        if window.std_dev < self._collapse_threshold * math.sqrt(baseline.variance):
            signals.append(
                AnomalySignal(
                    anomaly_type=AnomalyType.DISTRIBUTION_COLLAPSE,
                    severity=AnomalySeverity.HIGH,
                    metric_name=window.metric_name,
                    observed_value=window.std_dev,
                    expected_range=(
                        0.0,
                        math.sqrt(baseline.variance),
                    ),
                    window_id=window.window_id,
                    timestamp=window.timestamp,
                    baseline_version=baseline.version,
                )
            )

        # Unexpected variance
        expected_std = math.sqrt(baseline.variance)
        if expected_std > 0:
            variance_ratio = window.std_dev / expected_std
            if variance_ratio > 2.0 or variance_ratio < 0.5:
                signals.append(
                    AnomalySignal(
                        anomaly_type=AnomalyType.UNEXPECTED_VARIANCE,
                        severity=self._compute_variance_severity(variance_ratio),
                        metric_name=window.metric_name,
                        observed_value=window.std_dev,
                        expected_range=(expected_std * 0.5, expected_std * 2.0),
                        window_id=window.window_id,
                        timestamp=window.timestamp,
                        baseline_version=baseline.version,
                    )
                )

        return signals

    def _compute_shift_severity(self, deviation: float) -> AnomalySeverity:
        """Mechanical severity for distribution shifts."""
        if deviation >= 0.7:
            return AnomalySeverity.CRITICAL
        elif deviation >= 0.5:
            return AnomalySeverity.HIGH
        elif deviation >= 0.3:
            return AnomalySeverity.MEDIUM
        else:
            return AnomalySeverity.LOW

    def _compute_variance_severity(self, ratio: float) -> AnomalySeverity:
        """Mechanical severity for variance changes."""
        if ratio > 3.0 or ratio < 0.33:
            return AnomalySeverity.CRITICAL
        elif ratio > 2.5 or ratio < 0.4:
            return AnomalySeverity.HIGH
        else:
            return AnomalySeverity.MEDIUM


class MissingSignalDetector:
    """
    Detects silent failures, zero-emission conditions, stalled pipelines.
    
    Missing data = anomaly.
    """

    def __init__(
        self,
        min_sample_threshold: int = 10,
        zero_ratio_threshold: float = 0.9,
    ):
        """
        Initialize missing signal detector.
        
        Args:
            min_sample_threshold: Minimum expected samples
            zero_ratio_threshold: Max ratio of zeros before flagging
        """
        self._min_sample_threshold = min_sample_threshold
        self._zero_ratio_threshold = zero_ratio_threshold

    def detect(
        self,
        window: WindowProfile,
        baseline: BaselineProfile,
    ) -> list[AnomalySignal]:
        """
        Detect missing signals and silent failures.
        
        Args:
            window: Current window profile
            baseline: Baseline profile
            
        Returns:
            List of anomaly signals
        """
        signals = []

        # Sample count too low (silent failure)
        if window.sample_count < self._min_sample_threshold:
            signals.append(
                AnomalySignal(
                    anomaly_type=AnomalyType.MISSING_SIGNAL,
                    severity=AnomalySeverity.CRITICAL,
                    metric_name=window.metric_name,
                    observed_value=float(window.sample_count),
                    expected_range=(float(self._min_sample_threshold), None),
                    window_id=window.window_id,
                    timestamp=window.timestamp,
                    baseline_version=baseline.version,
                )
            )

        # Zero-emission condition
        if window.sample_count > 0:
            zero_ratio = window.zero_count / window.sample_count
            if zero_ratio > self._zero_ratio_threshold:
                signals.append(
                    AnomalySignal(
                        anomaly_type=AnomalyType.MISSING_SIGNAL,
                        severity=AnomalySeverity.HIGH,
                        metric_name=window.metric_name,
                        observed_value=zero_ratio,
                        expected_range=(0.0, self._zero_ratio_threshold),
                        window_id=window.window_id,
                        timestamp=window.timestamp,
                        baseline_version=baseline.version,
                    )
                )

        return signals


class CorrelationBreakDetector:
    """
    Detects broken invariant relationships, expected correlations disappearing.
    
    Example: views ↔ engagement decouples suddenly
    """

    def __init__(
        self,
        correlation_registry: dict[tuple[str, str], float],
        break_threshold: float = 0.5,
    ):
        """
        Initialize correlation break detector.
        
        Args:
            correlation_registry: Expected correlations between metric pairs
            break_threshold: Threshold for correlation break detection
        """
        self._correlations = correlation_registry
        self._break_threshold = break_threshold

    def detect(
        self,
        windows: dict[str, WindowProfile],
        baselines: dict[str, BaselineProfile],
    ) -> list[AnomalySignal]:
        """
        Detect correlation breaks across metric pairs.
        
        Args:
            windows: Map of metric_name -> WindowProfile
            baselines: Map of metric_name -> BaselineProfile
            
        Returns:
            List of anomaly signals
        """
        signals = []

        for (metric_a, metric_b), expected_corr in self._correlations.items():
            if metric_a not in windows or metric_b not in windows:
                continue

            window_a = windows[metric_a]
            window_b = windows[metric_b]

            # Simple correlation proxy: normalized mean product
            if metric_a in baselines and metric_b in baselines:
                baseline_a = baselines[metric_a]
                baseline_b = baselines[metric_b]

                expected_mean_a = (
                    baseline_a.expected_min + baseline_a.expected_max
                ) / 2.0
                expected_mean_b = (
                    baseline_b.expected_min + baseline_b.expected_max
                ) / 2.0

                if expected_mean_a > 0 and expected_mean_b > 0:
                    norm_a = window_a.mean / expected_mean_a
                    norm_b = window_b.mean / expected_mean_b

                    # Detect decoupling
                    observed_ratio = norm_a / norm_b if norm_b > 0 else 0.0
                    if abs(observed_ratio - 1.0) > self._break_threshold:
                        signals.append(
                            AnomalySignal(
                                anomaly_type=AnomalyType.CORRELATION_BREAK,
                                severity=self._compute_break_severity(
                                    abs(observed_ratio - 1.0)
                                ),
                                metric_name=f"{metric_a}↔{metric_b}",
                                observed_value=observed_ratio,
                                expected_range=(
                                    1.0 - self._break_threshold,
                                    1.0 + self._break_threshold,
                                ),
                                window_id=window_a.window_id,
                                timestamp=window_a.timestamp,
                                baseline_version=baseline_a.version,
                            )
                        )

        return signals

    def _compute_break_severity(self, deviation: float) -> AnomalySeverity:
        """Mechanical severity for correlation breaks."""
        if deviation >= 1.0:
            return AnomalySeverity.CRITICAL
        elif deviation >= 0.75:
            return AnomalySeverity.HIGH
        elif deviation >= 0.5:
            return AnomalySeverity.MEDIUM
        else:
            return AnomalySeverity.LOW


# ============================================================================
# ANOMALY DETECTOR (CORE ENGINE)
# ============================================================================


class AnomalyDetector:
    """
    Core anomaly detection engine.
    
    Execution guarantees:
    - Deterministic order
    - Fixed windows
    - Versioned baselines
    - Bounded compute
    - No adaptive learning
    
    This is not ML training.
    """

    def __init__(
        self,
        baseline_manager: BaselineManager,
        rate_shift_detector: RateShiftDetector,
        distribution_shift_detector: DistributionShiftDetector,
        missing_signal_detector: MissingSignalDetector,
        correlation_break_detector: CorrelationBreakDetector,
    ):
        """
        Initialize anomaly detector with all detection engines.
        
        Args:
            baseline_manager: Manages baseline profiles
            rate_shift_detector: Detects rate spikes/drops
            distribution_shift_detector: Detects distribution changes
            missing_signal_detector: Detects missing signals
            correlation_break_detector: Detects correlation breaks
        """
        self._baseline_manager = baseline_manager
        self._rate_shift_detector = rate_shift_detector
        self._distribution_shift_detector = distribution_shift_detector
        self._missing_signal_detector = missing_signal_detector
        self._correlation_break_detector = correlation_break_detector

    def detect(self, windows: list[WindowProfile]) -> list[AnomalySignal]:
        """
        Run all detection engines on provided windows.
        
        Args:
            windows: List of window profiles to analyze
            
        Returns:
            List of anomaly signals (deterministic order)
        """
        signals = []
        baselines = self._baseline_manager.load_baselines()
        windows_map = {w.metric_name: w for w in windows}

        # Process windows in deterministic order
        sorted_windows = sorted(windows, key=lambda w: w.metric_name)

        for window in sorted_windows:
            baseline = baselines.get(window.metric_name)
            if baseline is None:
                # Skip if no baseline (or enforce strict mode)
                continue

            # Run all single-metric detectors
            signals.extend(
                self._rate_shift_detector.detect(window, baseline)
            )
            signals.extend(
                self._distribution_shift_detector.detect(window, baseline)
            )
            signals.extend(
                self._missing_signal_detector.detect(window, baseline)
            )

        # Run multi-metric detectors
        signals.extend(
            self._correlation_break_detector.detect(windows_map, baselines)
        )

        # Return in deterministic order
        return sorted(
            signals,
            key=lambda s: (s.timestamp, s.metric_name, s.anomaly_type.value),
        )

    def evaluate(
        self,
        windows: list[WindowProfile],
        strict: bool = True,
    ) -> list[AnomalySignal]:
        """
        Evaluate windows with optional strict mode.
        
        Args:
            windows: List of window profiles to analyze
            strict: If True, require baselines for all metrics
            
        Returns:
            List of anomaly signals
            
        Raises:
            ValueError: If strict=True and baseline missing
        """
        if strict:
            baselines = self._baseline_manager.load_baselines()
            for window in windows:
                if window.metric_name not in baselines:
                    raise ValueError(
                        f"Strict mode: baseline missing for '{window.metric_name}'"
                    )

        return self.detect(windows)


# ============================================================================
# ANOMALY WATCHDOG
# ============================================================================


class AnomalyWatchdog:
    """
    Watches for anomaly patterns and enforces detection integrity.
    
    Does NOT take action — only monitors and reports.
    """

    def __init__(self, max_anomalies_per_window: int = 100):
        """
        Initialize anomaly watchdog.
        
        Args:
            max_anomalies_per_window: Max anomalies before flagging storm
        """
        self._max_anomalies = max_anomalies_per_window
        self._anomaly_history: dict[str, list[AnomalySignal]] = defaultdict(list)

    def observe(
        self,
        signals: list[AnomalySignal],
        window_id: str,
    ) -> dict[str, Any]:
        """
        Observe anomaly signals and compute watchdog metrics.
        
        Args:
            signals: Anomaly signals from detection
            window_id: Window identifier
            
        Returns:
            Watchdog report
        """
        self._anomaly_history[window_id] = signals

        # Compute summary statistics
        severity_counts = defaultdict(int)
        type_counts = defaultdict(int)

        for signal in signals:
            severity_counts[signal.severity.value] += 1
            type_counts[signal.anomaly_type.value] += 1

        # Detect anomaly storm
        is_storm = len(signals) > self._max_anomalies

        report = {
            "window_id": window_id,
            "total_anomalies": len(signals),
            "is_anomaly_storm": is_storm,
            "severity_distribution": dict(severity_counts),
            "type_distribution": dict(type_counts),
            "critical_count": severity_counts[AnomalySeverity.CRITICAL.value],
            "high_count": severity_counts[AnomalySeverity.HIGH.value],
        }

        return report

    def get_history(self, window_id: str) -> list[AnomalySignal]:
        """Get anomaly history for a window."""
        return self._anomaly_history.get(window_id, [])

    def clear_history(self, window_id: str | None = None) -> None:
        """Clear anomaly history (for testing/reset)."""
        if window_id is None:
            self._anomaly_history.clear()
        else:
            self._anomaly_history.pop(window_id, None)


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================


def create_default_detector(
    baseline_registry: dict[str, BaselineProfile],
    correlation_registry: dict[tuple[str, str], float] | None = None,
) -> AnomalyDetector:
    """
    Create a default-configured anomaly detector.
    
    Args:
        baseline_registry: Baseline profiles for metrics
        correlation_registry: Optional correlation expectations
        
    Returns:
        Configured AnomalyDetector instance
    """
    baseline_manager = BaselineManager(baseline_registry)

    rate_detector = RateShiftDetector(
        spike_threshold=3.0,
        drop_threshold=3.0,
    )

    distribution_detector = DistributionShiftDetector(
        shift_threshold=0.3,
        collapse_threshold=0.1,
    )

    missing_detector = MissingSignalDetector(
        min_sample_threshold=10,
        zero_ratio_threshold=0.9,
    )

    correlation_detector = CorrelationBreakDetector(
        correlation_registry=correlation_registry or {},
        break_threshold=0.5,
    )

    return AnomalyDetector(
        baseline_manager=baseline_manager,
        rate_shift_detector=rate_detector,
        distribution_shift_detector=distribution_detector,
        missing_signal_detector=missing_detector,
        correlation_break_detector=correlation_detector,
    )


def create_strict_detector(
    baseline_registry: dict[str, BaselineProfile],
    correlation_registry: dict[tuple[str, str], float] | None = None,
) -> AnomalyDetector:
    """
    Create a strict-mode detector with tighter thresholds.
    
    Args:
        baseline_registry: Baseline profiles for metrics
        correlation_registry: Optional correlation expectations
        
    Returns:
        Configured AnomalyDetector instance with strict thresholds
    """
    baseline_manager = BaselineManager(baseline_registry)

    rate_detector = RateShiftDetector(
        spike_threshold=2.5,
        drop_threshold=2.5,
    )

    distribution_detector = DistributionShiftDetector(
        shift_threshold=0.2,
        collapse_threshold=0.05,
    )

    missing_detector = MissingSignalDetector(
        min_sample_threshold=20,
        zero_ratio_threshold=0.8,
    )

    correlation_detector = CorrelationBreakDetector(
        correlation_registry=correlation_registry or {},
        break_threshold=0.3,
    )

    return AnomalyDetector(
        baseline_manager=baseline_manager,
        rate_shift_detector=rate_detector,
        distribution_shift_detector=distribution_detector,
        missing_signal_detector=missing_detector,
        correlation_break_detector=correlation_detector,
    )