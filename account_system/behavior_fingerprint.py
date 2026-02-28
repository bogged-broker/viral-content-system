"""
/account_system/behavior_fingerprint.py

Posting & Interaction Pattern Signatures

Computes deterministic behavioral signatures of accounts based on how they act,
not what they say or how well they perform.

Answers: "Does this account behave like a stable, legitimate human or organization —
or like automation, orchestration, or synthetic control?"

HARD BOUNDARIES:
- NOT trust scoring (trust_scoring.py handles that)
- NOT risk classification (enforcement_monitor.py handles that)
- NOT bot detection labels (suppression analysis handles that)
- NOT platform heuristics (platform adapters handle those)
- NOT enforcement logic (enforcement_monitor.py handles that)

This file emits signatures, not conclusions.

Core Principle: Behavior is shape, not outcome.

Virality can spike. Engagement can collapse.
Behavioral structure is harder to fake — and much more predictive.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Any, Tuple, List, Dict
from enum import Enum
import hashlib
import json
import math
import statistics
from collections import defaultdict, Counter


# =============================================================================
# CONSTANTS & CONFIGURATION
# =============================================================================

FINGERPRINT_VERSION = "1.0.0"

# Vector dimensions (FIXED - DO NOT CHANGE without version bump)
FINGERPRINT_VECTOR_DIM = 64

# Minimum activity requirements
MIN_POSTS_FOR_FINGERPRINT = 10
MIN_INTERACTIONS_FOR_FINGERPRINT = 5
MIN_OBSERVATION_DAYS = 3

# Temporal windows (hours)
CADENCE_WINDOW_HOURS = 168  # 7 days
BURST_DETECTION_WINDOW_HOURS = 24
DIURNAL_CYCLE_HOURS = 24

# Risk marker thresholds
ZERO_LATENCY_THRESHOLD_SECONDS = 5
QUANTIZATION_DETECTION_THRESHOLD = 0.15
DIURNAL_ANOMALY_THRESHOLD = 0.3
BURST_SATURATION_THRESHOLD = 0.7

# Stability/Volatility calculation windows
STABILITY_COMPARISON_WINDOWS = [24, 72, 168]  # hours


# =============================================================================
# ENUMS
# =============================================================================

class RiskMarkerType(Enum):
    """Behavioral risk markers (NON-DECISIVE)"""
    ZERO_LATENCY_REACTIONS = "zero_latency_reactions"
    CADENCE_QUANTIZATION = "cadence_quantization"
    DIURNAL_ANOMALY = "diurnal_anomaly"
    BURST_SATURATION = "burst_saturation"
    FORMAT_RIGIDITY = "format_rigidity"
    TIMEZONE_INCONSISTENCY = "timezone_inconsistency"
    IMPOSSIBLE_VELOCITY = "impossible_velocity"
    REST_CYCLE_ABSENCE = "rest_cycle_absence"


# =============================================================================
# CORE DATA STRUCTURES
# =============================================================================

@dataclass(frozen=True)
class FingerprintComponent:
    """Single component of behavioral fingerprint"""
    name: str
    values: dict[str, float]
    vector_segment: tuple[float, ...]
    confidence: float  # 0.0-1.0
    explanation: str


@dataclass(frozen=True)
class BehaviorFingerprint:
    """
    Immutable behavioral fingerprint snapshot.
    
    Deterministic output given same input events.
    Hashable for drift detection and network analysis.
    """
    account_id: str
    platform: str
    snapshot_timestamp: datetime
    
    # Core fingerprint
    fingerprint_vector: tuple[float, ...]
    fingerprint_hash: str
    
    # Component breakdowns
    components: dict[str, FingerprintComponent]
    
    # Stability metrics
    stability_score: float  # 0.0-1.0 (higher = more stable)
    volatility_score: float  # 0.0-1.0 (higher = more volatile)
    
    # Risk indicators (NON-DECISIVE)
    risk_markers: tuple[str, ...]
    
    # Human-readable explanations
    explanations: tuple[str, ...]
    
    # Metadata
    fingerprint_version: str
    observation_window_hours: float
    event_count: int
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict"""
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "snapshot_timestamp": self.snapshot_timestamp.isoformat(),
            "fingerprint_vector": list(self.fingerprint_vector),
            "fingerprint_hash": self.fingerprint_hash,
            "components": {
                name: {
                    "values": comp.values,
                    "vector_segment": list(comp.vector_segment),
                    "confidence": comp.confidence,
                    "explanation": comp.explanation
                }
                for name, comp in self.components.items()
            },
            "stability_score": self.stability_score,
            "volatility_score": self.volatility_score,
            "risk_markers": list(self.risk_markers),
            "explanations": list(self.explanations),
            "fingerprint_version": self.fingerprint_version,
            "observation_window_hours": self.observation_window_hours,
            "event_count": self.event_count
        }


@dataclass
class BehaviorEvent:
    """Raw behavioral event (input data)"""
    timestamp: datetime
    event_type: str  # "post", "comment", "reaction", "edit"
    format_signature: Optional[str] = None  # content format fingerprint
    interaction_target_id: Optional[str] = None  # for reactions/comments
    interaction_target_timestamp: Optional[datetime] = None
    timezone_offset: Optional[int] = None  # minutes from UTC


# =============================================================================
# FINGERPRINT BUILDER
# =============================================================================

class BehaviorFingerprintBuilder:
    """
    Extracts behavioral patterns from event stream.
    
    Deterministic: same events → same fingerprint
    """
    
    def __init__(self, account_id: str, platform: str):
        self.account_id = account_id
        self.platform = platform
        self.events: list[BehaviorEvent] = []
    
    def add_event(self, event: BehaviorEvent) -> None:
        """Add behavioral event"""
        self.events.append(event)
    
    def build(self, snapshot_timestamp: Optional[datetime] = None) -> BehaviorFingerprint:
        """
        Build fingerprint from accumulated events.
        
        Raises:
            ValueError: if insufficient data or validation fails
        """
        if snapshot_timestamp is None:
            snapshot_timestamp = datetime.now(timezone.utc)
        
        # Validate
        self._validate_for_build(snapshot_timestamp)
        
        # Sort events chronologically (determinism)
        sorted_events = sorted(self.events, key=lambda e: e.timestamp)
        
        # Extract components
        components = {}
        vector_segments = []
        
        cadence_comp = self._extract_posting_cadence(sorted_events, snapshot_timestamp)
        components["posting_cadence"] = cadence_comp
        vector_segments.append(cadence_comp.vector_segment)
        
        latency_comp = self._extract_interaction_latency(sorted_events)
        components["interaction_latency"] = latency_comp
        vector_segments.append(latency_comp.vector_segment)
        
        burst_comp = self._extract_burst_dynamics(sorted_events, snapshot_timestamp)
        components["burst_dynamics"] = burst_comp
        vector_segments.append(burst_comp.vector_segment)
        
        format_comp = self._extract_format_persistence(sorted_events)
        components["format_persistence"] = format_comp
        vector_segments.append(format_comp.vector_segment)
        
        diurnal_comp = self._extract_diurnal_signature(sorted_events)
        components["diurnal_signature"] = diurnal_comp
        vector_segments.append(diurnal_comp.vector_segment)
        
        # Concatenate and normalize vector
        raw_vector = tuple(v for seg in vector_segments for v in seg)
        fingerprint_vector = FingerprintNormalizer.normalize(raw_vector)
        
        # Compute hash
        fingerprint_hash = FingerprintHasher.hash_fingerprint(
            fingerprint_vector, self.account_id, snapshot_timestamp
        )
        
        # Compute stability & volatility
        stability = self._compute_stability(sorted_events, components)
        volatility = self._compute_volatility(sorted_events, components)
        
        # Detect risk markers
        risk_markers = self._detect_risk_markers(components, sorted_events)
        
        # Generate explanations
        explanations = ExplanationGenerator.generate(components, risk_markers, stability, volatility)
        
        # Compute observation window
        if sorted_events:
            window_hours = (snapshot_timestamp - sorted_events[0].timestamp).total_seconds() / 3600
        else:
            window_hours = 0.0
        
        return BehaviorFingerprint(
            account_id=self.account_id,
            platform=self.platform,
            snapshot_timestamp=snapshot_timestamp,
            fingerprint_vector=fingerprint_vector,
            fingerprint_hash=fingerprint_hash,
            components=components,
            stability_score=stability,
            volatility_score=volatility,
            risk_markers=tuple(risk_markers),
            explanations=tuple(explanations),
            fingerprint_version=FINGERPRINT_VERSION,
            observation_window_hours=window_hours,
            event_count=len(sorted_events)
        )
    
    def _validate_for_build(self, snapshot_timestamp: datetime) -> None:
        """Validate sufficient data for fingerprinting"""
        posts = [e for e in self.events if e.event_type == "post"]
        interactions = [e for e in self.events if e.event_type in ("comment", "reaction")]
        
        if len(posts) < MIN_POSTS_FOR_FINGERPRINT:
            raise ValueError(
                f"Insufficient posts: {len(posts)} < {MIN_POSTS_FOR_FINGERPRINT}"
            )
        
        if len(interactions) < MIN_INTERACTIONS_FOR_FINGERPRINT:
            raise ValueError(
                f"Insufficient interactions: {len(interactions)} < {MIN_INTERACTIONS_FOR_FINGERPRINT}"
            )
        
        if self.events:
            earliest = min(e.timestamp for e in self.events)
            observation_days = (snapshot_timestamp - earliest).total_seconds() / 86400
            if observation_days < MIN_OBSERVATION_DAYS:
                raise ValueError(
                    f"Insufficient observation window: {observation_days:.1f} < {MIN_OBSERVATION_DAYS} days"
                )
        
        # Check for future events
        future_events = [e for e in self.events if e.timestamp > snapshot_timestamp]
        if future_events:
            raise ValueError(f"Found {len(future_events)} future events")
    
    def _extract_posting_cadence(
        self, events: list[BehaviorEvent], snapshot_timestamp: datetime
    ) -> FingerprintComponent:
        """
        Extract posting cadence patterns.
        
        Measures:
        - Inter-post interval distribution
        - Variance over rolling windows
        - Entropy of posting times
        
        Low entropy + low variance = automation risk
        High variance + periodic rest = human-like
        """
        posts = [e for e in events if e.event_type == "post"]
        
        if len(posts) < 2:
            return FingerprintComponent(
                name="posting_cadence",
                values={},
                vector_segment=tuple([0.0] * 12),
                confidence=0.0,
                explanation="Insufficient posts for cadence analysis"
            )
        
        # Compute inter-post intervals (seconds)
        intervals = []
        for i in range(1, len(posts)):
            interval = (posts[i].timestamp - posts[i-1].timestamp).total_seconds()
            intervals.append(interval)
        
        # Statistics
        mean_interval = statistics.mean(intervals)
        median_interval = statistics.median(intervals)
        stdev_interval = statistics.stdev(intervals) if len(intervals) > 1 else 0.0
        
        # Coefficient of variation (normalized variance)
        cv = stdev_interval / mean_interval if mean_interval > 0 else 0.0
        
        # Entropy of binned intervals (24 hour bins)
        bins = defaultdict(int)
        for interval in intervals:
            bin_idx = int(interval / 3600)  # hour bins
            bins[bin_idx] += 1
        
        total = sum(bins.values())
        entropy = 0.0
        for count in bins.values():
            if count > 0:
                p = count / total
                entropy -= p * math.log2(p)
        
        # Detect quantization (clustering around exact intervals)
        quantization_score = self._detect_quantization(intervals)
        
        # Vector segment (12 dims)
        vector_segment = (
            self._normalize_seconds(mean_interval),
            self._normalize_seconds(median_interval),
            self._normalize_seconds(stdev_interval),
            min(cv, 2.0) / 2.0,  # cap at 2.0 for normalization
            entropy / 10.0,  # normalize entropy
            quantization_score,
            float(len(posts)) / 100.0,  # normalized post count
            0.0, 0.0, 0.0, 0.0, 0.0  # reserved
        )
        
        return FingerprintComponent(
            name="posting_cadence",
            values={
                "mean_interval_seconds": mean_interval,
                "median_interval_seconds": median_interval,
                "stdev_interval_seconds": stdev_interval,
                "coefficient_of_variation": cv,
                "entropy": entropy,
                "quantization_score": quantization_score
            },
            vector_segment=vector_segment,
            confidence=min(len(posts) / 50.0, 1.0),
            explanation=f"Posting cadence: mean {mean_interval/3600:.1f}h, CV {cv:.2f}, entropy {entropy:.2f}"
        )
    
    def _extract_interaction_latency(self, events: list[BehaviorEvent]) -> FingerprintComponent:
        """
        Extract interaction latency patterns.
        
        Measures:
        - Response time to comments
        - Like/reply delay distributions
        - Symmetry (who initiates vs reacts)
        
        Zero-latency reactions are penalized.
        """
        interactions = [e for e in events if e.event_type in ("comment", "reaction")]
        
        if not interactions:
            return FingerprintComponent(
                name="interaction_latency",
                values={},
                vector_segment=tuple([0.0] * 10),
                confidence=0.0,
                explanation="No interactions for latency analysis"
            )
        
        # Compute latencies where we have target timestamp
        latencies = []
        zero_latency_count = 0
        
        for event in interactions:
            if event.interaction_target_timestamp:
                latency = (event.timestamp - event.interaction_target_timestamp).total_seconds()
                if latency >= 0:  # only forward latencies
                    latencies.append(latency)
                    if latency < ZERO_LATENCY_THRESHOLD_SECONDS:
                        zero_latency_count += 1
        
        if not latencies:
            return FingerprintComponent(
                name="interaction_latency",
                values={},
                vector_segment=tuple([0.0] * 10),
                confidence=0.0,
                explanation="No measurable interaction latencies"
            )
        
        # Statistics
        mean_latency = statistics.mean(latencies)
        median_latency = statistics.median(latencies)
        
        # Zero-latency ratio
        zero_latency_ratio = zero_latency_count / len(latencies)
        
        # Vector segment (10 dims)
        vector_segment = (
            self._normalize_seconds(mean_latency),
            self._normalize_seconds(median_latency),
            zero_latency_ratio,
            float(len(latencies)) / 100.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0  # reserved
        )
        
        return FingerprintComponent(
            name="interaction_latency",
            values={
                "mean_latency_seconds": mean_latency,
                "median_latency_seconds": median_latency,
                "zero_latency_ratio": zero_latency_ratio,
                "interaction_count": len(latencies)
            },
            vector_segment=vector_segment,
            confidence=min(len(latencies) / 30.0, 1.0),
            explanation=f"Interaction latency: mean {mean_latency/60:.1f}min, {zero_latency_ratio*100:.1f}% instant"
        )
    
    def _extract_burst_dynamics(
        self, events: list[BehaviorEvent], snapshot_timestamp: datetime
    ) -> FingerprintComponent:
        """
        Extract burst dynamics.
        
        Measures:
        - Burst frequency
        - Burst amplitude
        - Burst decay curves
        
        Organic bursts decay smoothly.
        Synthetic bursts collapse or cliff.
        """
        posts = [e for e in events if e.event_type == "post"]
        
        if len(posts) < 5:
            return FingerprintComponent(
                name="burst_dynamics",
                values={},
                vector_segment=tuple([0.0] * 14),
                confidence=0.0,
                explanation="Insufficient posts for burst analysis"
            )
        
        # Detect bursts (sliding 24h windows)
        bursts = []
        window_seconds = BURST_DETECTION_WINDOW_HOURS * 3600
        
        for i, post in enumerate(posts):
            window_start = post.timestamp
            window_end = window_start + timedelta(seconds=window_seconds)
            
            posts_in_window = [
                p for p in posts[i:]
                if window_start <= p.timestamp < window_end
            ]
            
            if len(posts_in_window) >= 3:  # minimum for burst
                bursts.append(len(posts_in_window))
        
        if not bursts:
            burst_frequency = 0.0
            burst_mean_amplitude = 0.0
        else:
            burst_frequency = len(bursts) / max(len(posts), 1)
            burst_mean_amplitude = statistics.mean(bursts)
        
        # Vector segment (14 dims)
        vector_segment = (
            burst_frequency,
            burst_mean_amplitude / 20.0,  # normalize assuming max ~20 posts/day
            float(len(bursts)) / 10.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0  # reserved
        )
        
        return FingerprintComponent(
            name="burst_dynamics",
            values={
                "burst_frequency": burst_frequency,
                "burst_mean_amplitude": burst_mean_amplitude,
                "burst_count": len(bursts)
            },
            vector_segment=vector_segment,
            confidence=min(len(posts) / 30.0, 1.0),
            explanation=f"Burst dynamics: {len(bursts)} bursts, mean amplitude {burst_mean_amplitude:.1f}"
        )
    
    def _extract_format_persistence(self, events: list[BehaviorEvent]) -> FingerprintComponent:
        """
        Extract format persistence patterns.
        
        Measures:
        - Format switching rate
        - Template reuse frequency
        - Aesthetic continuity
        
        High churn = instability
        Too-low churn = automation risk
        """
        posts_with_format = [e for e in events if e.event_type == "post" and e.format_signature]
        
        if len(posts_with_format) < 3:
            return FingerprintComponent(
                name="format_persistence",
                values={},
                vector_segment=tuple([0.0] * 10),
                confidence=0.0,
                explanation="Insufficient format data"
            )
        
        # Count format switches
        format_switches = 0
        for i in range(1, len(posts_with_format)):
            if posts_with_format[i].format_signature != posts_with_format[i-1].format_signature:
                format_switches += 1
        
        format_switch_rate = format_switches / max(len(posts_with_format) - 1, 1)
        
        # Count unique formats
        format_counts = Counter(p.format_signature for p in posts_with_format)
        format_diversity = len(format_counts) / max(len(posts_with_format), 1)
        
        # Most common format dominance
        most_common_ratio = format_counts.most_common(1)[0][1] / len(posts_with_format)
        
        # Vector segment (10 dims)
        vector_segment = (
            format_switch_rate,
            format_diversity,
            most_common_ratio,
            float(len(format_counts)) / 10.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.0  # reserved
        )
        
        return FingerprintComponent(
            name="format_persistence",
            values={
                "format_switch_rate": format_switch_rate,
                "format_diversity": format_diversity,
                "most_common_format_ratio": most_common_ratio,
                "unique_formats": len(format_counts)
            },
            vector_segment=vector_segment,
            confidence=min(len(posts_with_format) / 20.0, 1.0),
            explanation=f"Format persistence: {format_switch_rate*100:.1f}% switch rate, {len(format_counts)} unique"
        )
    
    def _extract_diurnal_signature(self, events: list[BehaviorEvent]) -> FingerprintComponent:
        """
        Extract diurnal (24h cycle) signature.
        
        Measures:
        - Local-time activity cycles
        - Sleep gaps
        - Timezone consistency
        
        24/7 activity without rest always flags risk.
        """
        posts = [e for e in events if e.event_type == "post"]
        
        if len(posts) < 10:
            return FingerprintComponent(
                name="diurnal_signature",
                values={},
                vector_segment=tuple([0.0] * 18),
                confidence=0.0,
                explanation="Insufficient posts for diurnal analysis"
            )
        
        # Bin posts by hour of day (0-23)
        hour_bins = [0] * 24
        
        for post in posts:
            # Use timezone offset if available, otherwise UTC
            if post.timezone_offset is not None:
                local_time = post.timestamp + timedelta(minutes=post.timezone_offset)
            else:
                local_time = post.timestamp
            
            hour = local_time.hour
            hour_bins[hour] += 1
        
        # Compute entropy (uniformity)
        total = sum(hour_bins)
        entropy = 0.0
        for count in hour_bins:
            if count > 0:
                p = count / total
                entropy -= p * math.log2(p)
        
        max_entropy = math.log2(24)
        normalized_entropy = entropy / max_entropy
        
        # Detect rest gaps (consecutive hours with no activity)
        max_rest_gap = 0
        current_gap = 0
        for count in hour_bins + hour_bins:  # wrap around
            if count == 0:
                current_gap += 1
                max_rest_gap = max(max_rest_gap, current_gap)
            else:
                current_gap = 0
        
        # 24/7 activity flag
        has_rest_cycle = max_rest_gap >= 4  # at least 4 consecutive quiet hours
        
        # Vector segment (18 dims: 24 hours binned to 12 + 6 metrics)
        hour_vector = tuple(
            (hour_bins[i] + hour_bins[i+12]) / max(total, 1)
            for i in range(12)
        )
        
        vector_segment = hour_vector + (
            normalized_entropy,
            float(max_rest_gap) / 24.0,
            1.0 if has_rest_cycle else 0.0,
            0.0, 0.0, 0.0  # reserved
        )
        
        return FingerprintComponent(
            name="diurnal_signature",
            values={
                "entropy": entropy,
                "normalized_entropy": normalized_entropy,
                "max_rest_gap_hours": max_rest_gap,
                "has_rest_cycle": has_rest_cycle
            },
            vector_segment=vector_segment,
            confidence=min(len(posts) / 50.0, 1.0),
            explanation=f"Diurnal: entropy {normalized_entropy:.2f}, max rest {max_rest_gap}h"
        )
    
    def _compute_stability(
        self, events: list[BehaviorEvent], components: dict[str, FingerprintComponent]
    ) -> float:
        """
        Compute stability score (0.0-1.0).
        
        Higher = more stable behavioral patterns
        """
        if not events or len(events) < MIN_POSTS_FOR_FINGERPRINT:
            return 0.0
        
        # Stability indicators:
        # - Low cadence variance
        # - Consistent diurnal pattern
        # - Gradual format evolution
        
        stability_signals = []
        
        # Cadence stability
        cadence = components.get("posting_cadence")
        if cadence and "coefficient_of_variation" in cadence.values:
            cv = cadence.values["coefficient_of_variation"]
            cadence_stability = 1.0 / (1.0 + cv)  # lower CV = higher stability
            stability_signals.append(cadence_stability)
        
        # Diurnal stability
        diurnal = components.get("diurnal_signature")
        if diurnal and "normalized_entropy" in diurnal.values:
            # Mid-range entropy = stable human pattern
            entropy = diurnal.values["normalized_entropy"]
            diurnal_stability = 1.0 - abs(entropy - 0.5) * 2  # peak at 0.5
            stability_signals.append(max(diurnal_stability, 0.0))
        
        # Format stability
        format_comp = components.get("format_persistence")
        if format_comp and "format_switch_rate" in format_comp.values:
            switch_rate = format_comp.values["format_switch_rate"]
            format_stability = 1.0 - min(switch_rate, 1.0)
            stability_signals.append(format_stability)
        
        if not stability_signals:
            return 0.5  # neutral
        
        return statistics.mean(stability_signals)
    
    def _compute_volatility(
        self, events: list[BehaviorEvent], components: dict[str, FingerprintComponent]
    ) -> float:
        """
        Compute volatility score (0.0-1.0).
        
        Higher = more volatile behavioral patterns
        """
        if not events or len(events) < MIN_POSTS_FOR_FINGERPRINT:
            return 0.0
        
        # Volatility indicators:
        # - High cadence variance
        # - Burst saturation
        # - Format churn
        
        volatility_signals = []
        
        # Cadence volatility
        cadence = components.get("posting_cadence")
        if cadence and "coefficient_of_variation" in cadence.values:
            cv = cadence.values["coefficient_of_variation"]
            cadence_volatility = min(cv / 2.0, 1.0)  # normalize to 0-1
            volatility_signals.append(cadence_volatility)
        
        # Burst volatility
        burst = components.get("burst_dynamics")
        if burst and "burst_frequency" in burst.values:
            burst_freq = burst.values["burst_frequency"]
            burst_volatility = min(burst_freq * 2.0, 1.0)
            volatility_signals.append(burst_volatility)
        
        # Format volatility
        format_comp = components.get("format_persistence")
        if format_comp and "format_switch_rate" in format_comp.values:
            switch_rate = format_comp.values["format_switch_rate"]
            format_volatility = min(switch_rate, 1.0)
            volatility_signals.append(format_volatility)
        
        if not volatility_signals:
            return 0.0
        
        return statistics.mean(volatility_signals)
    
    def _detect_risk_markers(
        self, components: dict[str, FingerprintComponent], events: list[BehaviorEvent]
    ) -> list[str]:
        """Detect behavioral risk markers (NON-DECISIVE)"""
        markers = []
        
        # Zero-latency reactions
        latency = components.get("interaction_latency")
        if latency and latency.values.get("zero_latency_ratio", 0.0) > 0.3:
            markers.append(RiskMarkerType.ZERO_LATENCY_REACTIONS.value)
        
        # Cadence quantization
        cadence = components.get("posting_cadence")
        if cadence and cadence.values.get("quantization_score", 0.0) > QUANTIZATION_DETECTION_THRESHOLD:
            markers.append(RiskMarkerType.CADENCE_QUANTIZATION.value)
        
        # Diurnal anomaly
        diurnal = components.get("diurnal_signature")
        if diurnal:
            if not diurnal.values.get("has_rest_cycle", True):
                markers.append(RiskMarkerType.REST_CYCLE_ABSENCE.value)
            
            entropy = diurnal.values.get("normalized_entropy", 0.5)
            if entropy > 0.95:  # too uniform
                markers.append(RiskMarkerType.DIURNAL_ANOMALY.value)
        
        # Burst saturation
        burst = components.get("burst_dynamics")
        if burst and burst.values.get("burst_frequency", 0.0) > BURST_SATURATION_THRESHOLD:
            markers.append(RiskMarkerType.BURST_SATURATION.value)
        
        # Format rigidity
        format_comp = components.get("format_persistence")
        if format_comp:
            switch_rate = format_comp.values.get("format_switch_rate", 1.0)
            if switch_rate < 0.05:  # too rigid
                markers.append(RiskMarkerType.FORMAT_RIGIDITY.value)
        
        return markers
    
    def _detect_quantization(self, intervals: list[float]) -> float:
        """
        Detect if intervals cluster around exact multiples.
        
        Returns quantization score (0.0-1.0)
        """
        if len(intervals) < 5:
            return 0.0
        
        # Check for clustering around 1h, 2h, 4h, 8h, 12h, 24h
        test_intervals = [3600, 7200, 14400, 28800, 43200, 86400]  # seconds
        
        quantized_count = 0
        tolerance = 300  # 5 minute tolerance
        
        for interval in intervals:
            for test in test_intervals:
                if abs(interval - test) < tolerance:
                    quantized_count += 1
                    break
        
        return quantized_count / len(intervals)
    
    def _normalize_seconds(self, seconds: float) -> float:
        """Normalize seconds to 0-1 range (log scale, cap at 7 days)"""
        max_seconds = 7 * 24 * 3600  # 7 days
        capped = min(seconds, max_seconds)
        if capped <= 0:
            return 0.0
        return math.log(1 + capped) / math.log(1 + max_seconds)


# =============================================================================
# FINGERPRINT NORMALIZER
# =============================================================================

class FingerprintNormalizer:
    """Normalize fingerprint vectors to fixed dimensions"""
    
    @staticmethod
    def normalize(raw_vector: tuple[float, ...]) -> tuple[float, ...]:
        """
        Normalize vector to exactly FINGERPRINT_VECTOR_DIM dimensions.
        
        - Pads with zeros if too short
        - Truncates if too long (should never happen)
        - Clips values to [0, 1]
        """
        vector = list(raw_vector[:FINGERPRINT_VECTOR_DIM])
        
        # Pad if needed
        while len(vector) < FINGERPRINT_VECTOR_DIM:
            vector.append(0.0)
        
        # Clip to valid range
        vector = [max(0.0, min(1.0, v)) for v in vector]
        
        # Validate no NaNs or infinities
        if any(math.isnan(v) or math.isinf(v) for v in vector):
            raise ValueError("Fingerprint vector contains NaN or infinity")
        
        return tuple(vector)


# =============================================================================
# FINGERPRINT HASHER
# =============================================================================

class FingerprintHasher:
    """Generate deterministic hashes of fingerprints"""
    
    @staticmethod
    def hash_fingerprint(
        vector: tuple[float, ...],
        account_id: str,
        timestamp: datetime
    ) -> str:
        """
        Generate SHA256 hash of fingerprint.
        
        Deterministic for same inputs.
        Used for drift detection and network analysis.
        """
        # Create canonical representation
        canonical = {
            "vector": [round(v, 6) for v in vector],  # 6 decimal precision
            "account_id": account_id,
            "timestamp": timestamp.isoformat(),
            "version": FINGERPRINT_VERSION
        }
        
        canonical_str = json.dumps(canonical, sort_keys=True)
        return hashlib.sha256(canonical_str.encode()).hexdigest()


# =============================================================================
# STABILITY ANALYZER
# =============================================================================

class StabilityAnalyzer:
    """Analyze fingerprint stability over time"""
    
    @staticmethod
    def compute_drift(
        fingerprint_a: BehaviorFingerprint,
        fingerprint_b: BehaviorFingerprint
    ) -> float:
        """
        Compute drift between two fingerprints (Euclidean distance).
        
        Returns value in [0, ∞) where 0 = identical
        """
        if len(fingerprint_a.fingerprint_vector) != len(fingerprint_b.fingerprint_vector):
            raise ValueError("Fingerprint dimension mismatch")
        
        squared_diffs = [
            (a - b) ** 2
            for a, b in zip(fingerprint_a.fingerprint_vector, fingerprint_b.fingerprint_vector)
        ]
        
        return math.sqrt(sum(squared_diffs))
    
    @staticmethod
    def detect_phase_change(
        historical_fingerprints: list[BehaviorFingerprint],
        threshold: float = 0.3
    ) -> bool:
        """
        Detect sudden phase change in behavior.
        
        Returns True if recent drift exceeds threshold.
        """
        if len(historical_fingerprints) < 2:
            return False
        
        # Compare most recent to previous
        recent = historical_fingerprints[-1]
        previous = historical_fingerprints[-2]
        
        drift = StabilityAnalyzer.compute_drift(recent, previous)
        
        return drift > threshold


# =============================================================================
# VOLATILITY ANALYZER
# =============================================================================

class VolatilityAnalyzer:
    """Analyze fingerprint volatility patterns"""
    
    @staticmethod
    def compute_rolling_volatility(
        fingerprints: list[BehaviorFingerprint],
        window: int = 3
    ) -> float:
        """
        Compute rolling volatility over window.
        
        Returns average drift magnitude.
        """
        if len(fingerprints) < window:
            return 0.0
        
        drifts = []
        for i in range(len(fingerprints) - window + 1):
            window_fps = fingerprints[i:i+window]
            
            # Compute pairwise drifts in window
            window_drifts = []
            for j in range(len(window_fps) - 1):
                drift = StabilityAnalyzer.compute_drift(window_fps[j], window_fps[j+1])
                window_drifts.append(drift)
            
            if window_drifts:
                drifts.append(statistics.mean(window_drifts))
        
        return statistics.mean(drifts) if drifts else 0.0


# =============================================================================
# EXPLANATION GENERATOR
# =============================================================================

class ExplanationGenerator:
    """Generate human-readable explanations"""
    
    @staticmethod
    def generate(
        components: dict[str, FingerprintComponent],
        risk_markers: list[str],
        stability: float,
        volatility: float
    ) -> list[str]:
        """Generate bounded, evidence-linked explanations"""
        explanations = []
        
        # Component explanations
        for comp in components.values():
            if comp.confidence > 0.3:  # only include confident components
                explanations.append(comp.explanation)
        
        # Stability/Volatility
        if stability < 0.3:
            explanations.append(
                f"Low stability ({stability:.2f}) indicates inconsistent behavioral patterns"
            )
        elif stability > 0.7:
            explanations.append(
                f"High stability ({stability:.2f}) indicates consistent behavioral patterns"
            )
        
        if volatility > 0.6:
            explanations.append(
                f"High volatility ({volatility:.2f}) indicates rapid pattern changes"
            )
        
        # Risk markers
        if risk_markers:
            marker_str = ", ".join(risk_markers)
            explanations.append(
                f"Risk markers detected: {marker_str}"
            )
        
        return explanations


# =============================================================================
# FINGERPRINT VALIDATOR
# =============================================================================

class FingerprintValidator:
    """Validate fingerprint integrity"""
    
    @staticmethod
    def validate(fingerprint: BehaviorFingerprint) -> None:
        """
        Validate fingerprint.
        
        Raises ValueError if invalid.
        """
        # Vector dimension
        if len(fingerprint.fingerprint_vector) != FINGERPRINT_VECTOR_DIM:
            raise ValueError(
                f"Invalid vector dimension: {len(fingerprint.fingerprint_vector)} != {FINGERPRINT_VECTOR_DIM}"
            )
        
        # Vector range
        for i, v in enumerate(fingerprint.fingerprint_vector):
            if v < 0.0 or v > 1.0:
                raise ValueError(f"Vector element {i} out of range: {v}")
            if math.isnan(v) or math.isinf(v):
                raise ValueError(f"Vector element {i} is NaN or infinity")
        
        # Scores range
        if not 0.0 <= fingerprint.stability_score <= 1.0:
            raise ValueError(f"Invalid stability score: {fingerprint.stability_score}")
        
        if not 0.0 <= fingerprint.volatility_score <= 1.0:
            raise ValueError(f"Invalid volatility score: {fingerprint.volatility_score}")
        
        # Version match
        if fingerprint.fingerprint_version != FINGERPRINT_VERSION:
            raise ValueError(
                f"Version mismatch: {fingerprint.fingerprint_version} != {FINGERPRINT_VERSION}"
            )
        
        # Hash integrity
        recomputed_hash = FingerprintHasher.hash_fingerprint(
            fingerprint.fingerprint_vector,
            fingerprint.account_id,
            fingerprint.snapshot_timestamp
        )
        
        if recomputed_hash != fingerprint.fingerprint_hash:
            raise ValueError("Hash mismatch - fingerprint may be corrupted")


# =============================================================================
# FINGERPRINT WATCHDOG
# =============================================================================

class FingerprintWatchdog:
    """Monitor for anomalous fingerprint patterns"""
    
    def __init__(self):
        self.alerts: list[dict[str, Any]] = []
    
    def check_fingerprint(
        self,
        fingerprint: BehaviorFingerprint,
        historical: Optional[list[BehaviorFingerprint]] = None
    ) -> list[str]:
        """
        Check fingerprint for anomalies.
        
        Returns list of alert messages.
        """
        alerts = []
        
        # Impossible human patterns
        if self._detect_impossible_pattern(fingerprint):
            alerts.append("Impossible human pattern detected")
        
        # Silent drift
        if historical and len(historical) >= 2:
            if StabilityAnalyzer.detect_phase_change(historical + [fingerprint]):
                alerts.append("Sudden behavioral phase change detected")
        
        # Version mismatch
        if fingerprint.fingerprint_version != FINGERPRINT_VERSION:
            alerts.append(f"Fingerprint version mismatch: {fingerprint.fingerprint_version}")
        
        # Extreme volatility with high trust (handled upstream, but flag here)
        if fingerprint.volatility_score > 0.8 and fingerprint.stability_score < 0.2:
            alerts.append("Extreme volatility with low stability")
        
        # Store alerts
        for alert in alerts:
            self.alerts.append({
                "timestamp": datetime.now(timezone.utc),
                "account_id": fingerprint.account_id,
                "alert": alert,
                "fingerprint_hash": fingerprint.fingerprint_hash
            })
        
        return alerts
    
    def _detect_impossible_pattern(self, fingerprint: BehaviorFingerprint) -> bool:
        """Detect patterns impossible for humans"""
        # 24/7 activity
        diurnal = fingerprint.components.get("diurnal_signature")
        if diurnal and not diurnal.values.get("has_rest_cycle", True):
            return True
        
        # Perfect quantization
        cadence = fingerprint.components.get("posting_cadence")
        if cadence and cadence.values.get("quantization_score", 0.0) > 0.9:
            return True
        
        # Zero-latency everything
        latency = fingerprint.components.get("interaction_latency")
        if latency and latency.values.get("zero_latency_ratio", 0.0) > 0.95:
            return True
        
        return False
    
    def get_alerts(self, account_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Get all alerts, optionally filtered by account"""
        if account_id:
            return [a for a in self.alerts if a["account_id"] == account_id]
        return self.alerts.copy()


# =============================================================================
# USAGE EXAMPLE
# =============================================================================

def example_usage():
    """Example of building a behavior fingerprint"""
    
    # Create builder
    builder = BehaviorFingerprintBuilder(
        account_id="user_12345",
        platform="twitter"
    )
    
    # Add events (typically from database)
    base_time = datetime.now(timezone.utc) - timedelta(days=30)
    
    for i in range(50):
        # Simulate posting pattern
        event = BehaviorEvent(
            timestamp=base_time + timedelta(hours=i*12 + (i % 5)),
            event_type="post",
            format_signature=f"format_{i % 3}",
            timezone_offset=-480  # PST
        )
        builder.add_event(event)
    
    # Build fingerprint
    try:
        fingerprint = builder.build()
        
        # Validate
        FingerprintValidator.validate(fingerprint)
        
        # Check for anomalies
        watchdog = FingerprintWatchdog()
        alerts = watchdog.check_fingerprint(fingerprint)
        
        print(f"Fingerprint: {fingerprint.fingerprint_hash[:16]}...")
        print(f"Stability: {fingerprint.stability_score:.3f}")
        print(f"Volatility: {fingerprint.volatility_score:.3f}")
        print(f"Risk markers: {fingerprint.risk_markers}")
        print(f"Alerts: {alerts}")
        
        # Convert to dict for storage
        fingerprint_dict = fingerprint.to_dict()
        
    except ValueError as e:
        print(f"Fingerprint build failed: {e}")


if __name__ == "__main__":
    example_usage()



