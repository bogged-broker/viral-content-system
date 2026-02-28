
"""
/account_system/risk_signal_extractor.py

Passive Platform Risk Signal Extraction

This module collects passive, observable, non-actionable risk signals emitted by 
platforms without reacting to them. It answers one question only:

"What risk-related signals is the platform emitting about this account — without 
us interpreting, predicting, or responding?"

HARD BOUNDARIES:
- NOT suppression detection
- NOT trust scoring
- NOT enforcement interpretation
- NOT posting throttling
- NOT platform rules modeling
- NOT proactive risk prediction

CORE PRINCIPLE:
Observation ≠ interpretation

This file is passive telemetry only. It must remain dumb but accurate.

PLACEMENT:
/account_system/
├── account_profile.py
├── behavior_fingerprint.py
├── risk_signal_extractor.py   ← YOU ARE HERE
├── trust_scoring.py
├── reputation_ledger.py
├── network_affiliation.py
├── enforcement_monitor.py
└── trust_decay.py

CONSUMED BY:
- trust_scoring.py
- suppression_analyzer.py
- posting governors
- experiment safety checks

NEVER consumed directly by posting logic.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple, Set, FrozenSet, Any
from enum import Enum
from collections import defaultdict, deque
import hashlib
import json
import statistics
from abc import ABC, abstractmethod


# ============================================================================
# CONSTANTS & CONFIGURATION
# ============================================================================

# Extraction version for deterministic replay
EXTRACTION_VERSION = "1.0.0"

# Signal intensity bounds (strict)
MIN_INTENSITY = 0.0
MAX_INTENSITY = 1.0

# Confidence bounds
MIN_CONFIDENCE = 0.0
MAX_CONFIDENCE = 1.0

# Volatility analysis window
VOLATILITY_WINDOW_HOURS = 24
VOLATILITY_MIN_SAMPLES = 3

# Coverage estimation
EXPECTED_SIGNAL_COVERAGE = {
    "twitter": 0.85,
    "instagram": 0.75,
    "facebook": 0.80,
    "linkedin": 0.70,
    "tiktok": 0.65,
}

# Time decay for signal intensity normalization
SIGNAL_DECAY_HALFLIFE_HOURS = 6.0

# Watchdog thresholds
WATCHDOG_SIGNAL_EXPLOSION_THRESHOLD = 10  # signals appearing in <1hr
WATCHDOG_PLATFORM_ANOMALY_THRESHOLD = 0.3  # 30% of accounts affected
WATCHDOG_SILENT_LOSS_HOURS = 12  # no signals for this long triggers alert


# ============================================================================
# PLATFORM DEFINITIONS (NO POLICY ENCODING)
# ============================================================================

class Platform(Enum):
    """
    Supported platforms for signal extraction.
    
    This enum exists only to validate platform names.
    NO platform-specific behavior rules are encoded here.
    """
    TWITTER = "twitter"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    
    @classmethod
    def is_valid(cls, platform: str) -> bool:
        """Check if platform string is valid."""
        try:
            cls(platform.lower())
            return True
        except ValueError:
            return False


# ============================================================================
# SIGNAL CATEGORIES (STRICT TAXONOMY)
# ============================================================================

class SignalCategory(Enum):
    """
    Signal categories for organization only.
    
    Categories do NOT imply semantic meaning or severity.
    They exist purely for structural organization.
    """
    RATE_CONSTRAINT = "rate_constraint"
    DELIVERY_PATH = "delivery_path"
    FEATURE_AVAILABILITY = "feature_availability"
    API_ANOMALY = "api_anomaly"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class RiskSignal:
    """
    Immutable observation of a single risk signal.
    
    NO IMPLICIT MEANING ALLOWED.
    
    This structure describes WHAT was observed, not what it means.
    
    Fields:
        signal_name: Canonical signal identifier (e.g. "rate_limit_429")
        presence: Whether signal is currently active
        intensity: Normalized magnitude [0.0, 1.0]
        confidence: Observation confidence [0.0, 1.0]
        first_observed: When signal first appeared
        last_observed: Most recent observation
        category: Organizational category only
        raw_value: Original platform value (for audit/replay)
        metadata: Additional context (never used for decisions)
    """
    signal_name: str
    presence: bool
    
    intensity: float  # [0.0, 1.0] normalized magnitude
    confidence: float  # [0.0, 1.0] observation confidence
    
    first_observed: datetime
    last_observed: datetime
    
    category: SignalCategory
    raw_value: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validation on construction."""
        if not (MIN_INTENSITY <= self.intensity <= MAX_INTENSITY):
            raise ValueError(
                f"Signal intensity {self.intensity} outside bounds "
                f"[{MIN_INTENSITY}, {MAX_INTENSITY}]"
            )
        
        if not (MIN_CONFIDENCE <= self.confidence <= MAX_CONFIDENCE):
            raise ValueError(
                f"Signal confidence {self.confidence} outside bounds "
                f"[{MIN_CONFIDENCE}, {MAX_CONFIDENCE}]"
            )
        
        if self.last_observed < self.first_observed:
            raise ValueError(
                f"last_observed {self.last_observed} before first_observed "
                f"{self.first_observed}"
            )
        
        now = datetime.now(timezone.utc)
        if self.first_observed > now or self.last_observed > now:
            raise ValueError("Signal timestamps cannot be in the future")
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "signal_name": self.signal_name,
            "presence": self.presence,
            "intensity": self.intensity,
            "confidence": self.confidence,
            "first_observed": self.first_observed.isoformat(),
            "last_observed": self.last_observed.isoformat(),
            "category": self.category.value,
            "raw_value": self.raw_value,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class RiskSignalSnapshot:
    """
    Immutable snapshot of all risk signals for an account at a point in time.
    
    This is the primary output contract for this module.
    
    Fields:
        account_id: Account identifier
        platform: Platform name
        snapshot_timestamp: When snapshot was taken
        risk_signals: Tuple of all observed signals (immutable)
        signal_volatility: Measure of signal stability [0.0, 1.0]
        signal_coverage: Estimated observation completeness [0.0, 1.0]
        extraction_version: Version for deterministic replay
        snapshot_hash: SHA256 hash for integrity verification
    
    GUARANTEES:
    - Immutable (frozen dataclass)
    - Hashable (for deduplication)
    - Deterministic (same inputs → same hash)
    - Replayable (version-tagged)
    """
    account_id: str
    platform: str
    snapshot_timestamp: datetime
    
    risk_signals: Tuple[RiskSignal, ...]
    
    signal_volatility: float  # [0.0, 1.0]
    signal_coverage: float    # [0.0, 1.0]
    
    extraction_version: str
    snapshot_hash: str = field(default="")
    
    def __post_init__(self):
        """Validation and hash computation."""
        if not Platform.is_valid(self.platform):
            raise ValueError(f"Unknown platform: {self.platform}")
        
        now = datetime.now(timezone.utc)
        if self.snapshot_timestamp > now:
            raise ValueError("Snapshot timestamp cannot be in the future")
        
        if not (0.0 <= self.signal_volatility <= 1.0):
            raise ValueError(f"Invalid volatility: {self.signal_volatility}")
        
        if not (0.0 <= self.signal_coverage <= 1.0):
            raise ValueError(f"Invalid coverage: {self.signal_coverage}")
        
        # Check for duplicate signal names
        signal_names = [s.signal_name for s in self.risk_signals]
        if len(signal_names) != len(set(signal_names)):
            raise ValueError("Duplicate signal names detected")
        
        # Compute hash if not provided
        if not self.snapshot_hash:
            object.__setattr__(
                self, 
                'snapshot_hash', 
                self._compute_hash()
            )
    
    def _compute_hash(self) -> str:
        """
        Compute deterministic SHA256 hash of snapshot.
        
        Used for:
        - Integrity verification
        - Deduplication
        - Audit trails
        - Experiment replay
        """
        canonical = {
            "account_id": self.account_id,
            "platform": self.platform,
            "snapshot_timestamp": self.snapshot_timestamp.isoformat(),
            "signals": [s.to_dict() for s in sorted(
                self.risk_signals, 
                key=lambda x: x.signal_name
            )],
            "volatility": self.signal_volatility,
            "coverage": self.signal_coverage,
            "version": self.extraction_version,
        }
        
        canonical_json = json.dumps(canonical, sort_keys=True)
        return hashlib.sha256(canonical_json.encode()).hexdigest()
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary (output contract format)."""
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "snapshot_timestamp": self.snapshot_timestamp.isoformat(),
            "risk_signals": [s.to_dict() for s in self.risk_signals],
            "signal_volatility": self.signal_volatility,
            "signal_coverage": self.signal_coverage,
            "extraction_version": self.extraction_version,
            "snapshot_hash": self.snapshot_hash,
        }


# ============================================================================
# RAW OBSERVATION STRUCTURES
# ============================================================================

@dataclass
class RawPlatformObservation:
    """
    Raw, unprocessed observation from platform API.
    
    This is the input to the extractor. Contains platform-specific data
    that will be normalized into canonical RiskSignals.
    """
    account_id: str
    platform: str
    observation_timestamp: datetime
    
    # Rate limiting observations
    rate_limit_headers: Dict[str, Any] = field(default_factory=dict)
    api_response_codes: List[int] = field(default_factory=list)
    retry_after_values: List[int] = field(default_factory=list)
    
    # Delivery observations
    publish_latencies_ms: List[float] = field(default_factory=list)
    callback_delays_ms: List[float] = field(default_factory=list)
    missing_callbacks: int = 0
    silent_retry_count: int = 0
    
    # Feature availability observations
    disabled_endpoints: Set[str] = field(default_factory=set)
    degraded_features: Dict[str, str] = field(default_factory=dict)
    upload_quality_caps: Dict[str, Any] = field(default_factory=dict)
    
    # API anomaly observations
    response_schema_changes: List[str] = field(default_factory=list)
    soft_error_codes: List[str] = field(default_factory=list)
    latency_percentiles: Dict[str, float] = field(default_factory=dict)
    
    # Additional context
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# SIGNAL NORMALIZATION
# ============================================================================

class SignalNormalizer:
    """
    Normalizes platform-specific raw values into canonical [0.0, 1.0] intensities.
    
    RULES:
    - All intensities ∈ [0, 1]
    - Time-weighted decay (explicit)
    - Platform-specific raw → global canonical
    - No adaptive scaling
    
    This makes signals comparable across platforms.
    """
    
    @staticmethod
    def normalize_rate_limit_intensity(
        remaining: int,
        total: int,
        reset_seconds: int
    ) -> float:
        """
        Normalize rate limit pressure to [0.0, 1.0].
        
        Args:
            remaining: Requests remaining in window
            total: Total requests allowed in window
            reset_seconds: Seconds until reset
        
        Returns:
            Intensity where 1.0 = maximum pressure, 0.0 = no pressure
        """
        if total <= 0:
            return 0.0
        
        # Usage ratio
        usage_ratio = 1.0 - (remaining / total)
        
        # Time pressure (how soon until reset)
        time_pressure = 1.0 / max(1.0, reset_seconds / 60.0)  # normalize to minutes
        time_pressure = min(1.0, time_pressure)
        
        # Combined intensity
        intensity = (usage_ratio * 0.7) + (time_pressure * 0.3)
        
        return max(0.0, min(1.0, intensity))
    
    @staticmethod
    def normalize_latency_intensity(
        observed_latency_ms: float,
        baseline_latency_ms: float,
        max_expected_latency_ms: float
    ) -> float:
        """
        Normalize latency inflation to [0.0, 1.0].
        
        Args:
            observed_latency_ms: Observed latency
            baseline_latency_ms: Expected baseline latency
            max_expected_latency_ms: Maximum reasonable latency
        
        Returns:
            Intensity where 1.0 = severe inflation, 0.0 = normal
        """
        if observed_latency_ms <= baseline_latency_ms:
            return 0.0
        
        inflation = observed_latency_ms - baseline_latency_ms
        max_inflation = max_expected_latency_ms - baseline_latency_ms
        
        if max_inflation <= 0:
            return 0.0
        
        intensity = inflation / max_inflation
        return max(0.0, min(1.0, intensity))
    
    @staticmethod
    def normalize_error_rate_intensity(
        error_count: int,
        total_requests: int,
        baseline_error_rate: float = 0.01
    ) -> float:
        """
        Normalize error rate to [0.0, 1.0].
        
        Args:
            error_count: Number of errors
            total_requests: Total requests made
            baseline_error_rate: Expected baseline error rate
        
        Returns:
            Intensity where 1.0 = severe error rate, 0.0 = normal
        """
        if total_requests <= 0:
            return 0.0
        
        observed_rate = error_count / total_requests
        
        if observed_rate <= baseline_error_rate:
            return 0.0
        
        # Scale above baseline
        excess_rate = observed_rate - baseline_error_rate
        max_meaningful_rate = 0.5  # 50% error rate is max meaningful
        
        intensity = excess_rate / max_meaningful_rate
        return max(0.0, min(1.0, intensity))
    
    @staticmethod
    def apply_time_decay(
        intensity: float,
        age_hours: float,
        halflife_hours: float = SIGNAL_DECAY_HALFLIFE_HOURS
    ) -> float:
        """
        Apply exponential time decay to intensity.
        
        Args:
            intensity: Original intensity
            age_hours: Hours since observation
            halflife_hours: Half-life for decay
        
        Returns:
            Decayed intensity
        """
        if age_hours <= 0:
            return intensity
        
        decay_factor = 0.5 ** (age_hours / halflife_hours)
        decayed = intensity * decay_factor
        
        return max(0.0, min(1.0, decayed))


# ============================================================================
# SIGNAL VOLATILITY ANALYSIS
# ============================================================================

class SignalVolatilityAnalyzer:
    """
    Analyzes signal stability over time.
    
    MEASURES:
    - Appearance/disappearance frequency
    - Intensity swings
    - Temporal clustering
    
    HIGH VOLATILITY ≠ HIGH RISK
    It means environment instability.
    """
    
    def __init__(self, window_hours: int = VOLATILITY_WINDOW_HOURS):
        self.window_hours = window_hours
        self.signal_history: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=100)
        )
    
    def record_signal_state(
        self,
        signal_name: str,
        intensity: float,
        timestamp: datetime
    ):
        """Record signal state for volatility tracking."""
        self.signal_history[signal_name].append({
            "intensity": intensity,
            "timestamp": timestamp,
        })
    
    def compute_volatility(
        self,
        signal_name: str,
        as_of: datetime
    ) -> float:
        """
        Compute volatility score for a signal.
        
        Returns:
            Volatility score [0.0, 1.0] where:
            - 0.0 = perfectly stable
            - 1.0 = maximally volatile
        """
        history = self.signal_history.get(signal_name, deque())
        
        if len(history) < VOLATILITY_MIN_SAMPLES:
            return 0.0  # Insufficient data
        
        # Filter to window
        cutoff = as_of - timedelta(hours=self.window_hours)
        recent = [
            h for h in history 
            if h["timestamp"] >= cutoff
        ]
        
        if len(recent) < VOLATILITY_MIN_SAMPLES:
            return 0.0
        
        # Extract intensities
        intensities = [h["intensity"] for h in recent]
        
        # Compute standard deviation (measure of swings)
        std_dev = statistics.stdev(intensities) if len(intensities) > 1 else 0.0
        
        # Compute transition frequency (presence changes)
        transitions = 0
        for i in range(1, len(recent)):
            prev_present = recent[i-1]["intensity"] > 0.0
            curr_present = recent[i]["intensity"] > 0.0
            if prev_present != curr_present:
                transitions += 1
        
        transition_rate = transitions / len(recent)
        
        # Combined volatility
        volatility = (std_dev * 0.6) + (transition_rate * 0.4)
        
        return max(0.0, min(1.0, volatility))
    
    def compute_overall_volatility(
        self,
        signal_names: List[str],
        as_of: datetime
    ) -> float:
        """
        Compute overall volatility across all signals.
        
        Returns:
            Aggregate volatility score [0.0, 1.0]
        """
        if not signal_names:
            return 0.0
        
        volatilities = [
            self.compute_volatility(name, as_of)
            for name in signal_names
        ]
        
        # Weighted average (higher volatilities get more weight)
        sorted_vols = sorted(volatilities, reverse=True)
        weights = [1.0 / (i + 1) for i in range(len(sorted_vols))]
        weight_sum = sum(weights)
        
        weighted_vol = sum(
            v * w for v, w in zip(sorted_vols, weights)
        ) / weight_sum
        
        return max(0.0, min(1.0, weighted_vol))


# ============================================================================
# SIGNAL COVERAGE ESTIMATION
# ============================================================================

class SignalCoverageEstimator:
    """
    Estimates completeness of signal observation.
    
    MEASURES:
    - % of expected signal surface observed
    - Missing telemetry regions
    - API blind spots
    
    Low coverage → downstream uncertainty expansion.
    """
    
    # Expected signals per category per platform
    EXPECTED_SIGNALS = {
        Platform.TWITTER: {
            SignalCategory.RATE_CONSTRAINT: 3,
            SignalCategory.DELIVERY_PATH: 4,
            SignalCategory.FEATURE_AVAILABILITY: 5,
            SignalCategory.API_ANOMALY: 6,
        },
        Platform.INSTAGRAM: {
            SignalCategory.RATE_CONSTRAINT: 2,
            SignalCategory.DELIVERY_PATH: 3,
            SignalCategory.FEATURE_AVAILABILITY: 4,
            SignalCategory.API_ANOMALY: 5,
        },
        Platform.FACEBOOK: {
            SignalCategory.RATE_CONSTRAINT: 3,
            SignalCategory.DELIVERY_PATH: 3,
            SignalCategory.FEATURE_AVAILABILITY: 5,
            SignalCategory.API_ANOMALY: 5,
        },
        Platform.LINKEDIN: {
            SignalCategory.RATE_CONSTRAINT: 2,
            SignalCategory.DELIVERY_PATH: 2,
            SignalCategory.FEATURE_AVAILABILITY: 3,
            SignalCategory.API_ANOMALY: 4,
        },
        Platform.TIKTOK: {
            SignalCategory.RATE_CONSTRAINT: 2,
            SignalCategory.DELIVERY_PATH: 3,
            SignalCategory.FEATURE_AVAILABILITY: 3,
            SignalCategory.API_ANOMALY: 4,
        },
    }
    
    @classmethod
    def estimate_coverage(
        cls,
        platform: Platform,
        observed_signals: List[RiskSignal]
    ) -> float:
        """
        Estimate observation coverage for this platform.
        
        Args:
            platform: Platform enum
            observed_signals: Signals that were observed
        
        Returns:
            Coverage estimate [0.0, 1.0] where:
            - 1.0 = all expected signals observed
            - 0.0 = no signals observed
        """
        expected = cls.EXPECTED_SIGNALS.get(platform, {})
        
        if not expected:
            return 0.5  # Unknown platform, assume 50% coverage
        
        # Count observed signals by category
        observed_by_category = defaultdict(int)
        for signal in observed_signals:
            observed_by_category[signal.category] += 1
        
        # Compute coverage per category
        category_coverages = []
        for category, expected_count in expected.items():
            observed_count = observed_by_category.get(category, 0)
            coverage = min(1.0, observed_count / expected_count)
            category_coverages.append(coverage)
        
        # Overall coverage (average across categories)
        if not category_coverages:
            return 0.0
        
        overall = sum(category_coverages) / len(category_coverages)
        return max(0.0, min(1.0, overall))
    
    @classmethod
    def identify_blind_spots(
        cls,
        platform: Platform,
        observed_signals: List[RiskSignal]
    ) -> List[SignalCategory]:
        """
        Identify categories with insufficient signal coverage.
        
        Returns:
            List of categories with <50% coverage
        """
        expected = cls.EXPECTED_SIGNALS.get(platform, {})
        
        if not expected:
            return []
        
        observed_by_category = defaultdict(int)
        for signal in observed_signals:
            observed_by_category[signal.category] += 1
        
        blind_spots = []
        for category, expected_count in expected.items():
            observed_count = observed_by_category.get(category, 0)
            coverage = observed_count / expected_count if expected_count > 0 else 0.0
            
            if coverage < 0.5:
                blind_spots.append(category)
        
        return blind_spots


# ============================================================================
# RISK SIGNAL EXTRACTOR (CORE)
# ============================================================================

class RiskSignalExtractor:
    """
    Main extraction engine for platform risk signals.
    
    RESPONSIBILITIES:
    - Extract rate limit signals
    - Extract delivery latency signals
    - Extract feature availability signals
    - Extract API anomaly signals
    
    DOES NOT:
    - Interpret signals
    - Make decisions
    - Apply policy
    - Predict outcomes
    
    This is a passive observation system only.
    """
    
    def __init__(
        self,
        volatility_analyzer: Optional[SignalVolatilityAnalyzer] = None
    ):
        self.volatility_analyzer = volatility_analyzer or SignalVolatilityAnalyzer()
        self.normalizer = SignalNormalizer()
    
    def extract_signals(
        self,
        observation: RawPlatformObservation
    ) -> RiskSignalSnapshot:
        """
        Extract all risk signals from raw observation.
        
        This is the main entry point for signal extraction.
        
        Args:
            observation: Raw platform observation data
        
        Returns:
            Complete risk signal snapshot
        """
        signals: List[RiskSignal] = []
        
        # Extract each category
        signals.extend(self.extract_rate_limit_signals(observation))
        signals.extend(self.extract_delivery_latency_signals(observation))
        signals.extend(self.extract_feature_availability_signals(observation))
        signals.extend(self.extract_api_anomaly_signals(observation))
        
        # Record for volatility tracking
        for signal in signals:
            self.volatility_analyzer.record_signal_state(
                signal.signal_name,
                signal.intensity,
                observation.observation_timestamp
            )
        
        # Compute volatility
        signal_names = [s.signal_name for s in signals]
        volatility = self.volatility_analyzer.compute_overall_volatility(
            signal_names,
            observation.observation_timestamp
        )
        
        # Estimate coverage
        try:
            platform_enum = Platform(observation.platform.lower())
        except ValueError:
            platform_enum = None
        
        if platform_enum:
            coverage = SignalCoverageEstimator.estimate_coverage(
                platform_enum,
                signals
            )
        else:
            coverage = 0.5  # Unknown platform
        
        # Build snapshot
        snapshot = RiskSignalSnapshot(
            account_id=observation.account_id,
            platform=observation.platform,
            snapshot_timestamp=observation.observation_timestamp,
            risk_signals=tuple(signals),
            signal_volatility=volatility,
            signal_coverage=coverage,
            extraction_version=EXTRACTION_VERSION,
        )
        
        return snapshot
    
    def extract_rate_limit_signals(
        self,
        observation: RawPlatformObservation
    ) -> List[RiskSignal]:
        """
        Extract rate constraint signals.
        
        OBSERVED ONLY — NEVER INTERPRETED.
        
        Signals:
        - rate_limit_pressure: Current rate limit usage
        - rate_limit_429: Received 429 Too Many Requests
        - rate_limit_retry_after: Retry-After header present
        """
        signals = []
        now = observation.observation_timestamp
        
        # Rate limit pressure from headers
        headers = observation.rate_limit_headers
        if headers:
            remaining = headers.get("x-ratelimit-remaining", headers.get("remaining"))
            total = headers.get("x-ratelimit-limit", headers.get("limit"))
            reset = headers.get("x-ratelimit-reset", headers.get("reset"))
            
            if remaining is not None and total is not None:
                reset_seconds = 60  # default
                if reset:
                    try:
                        reset_dt = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                        reset_seconds = max(1, int((reset_dt - now).total_seconds()))
                    except (ValueError, TypeError):
                        pass
                
                intensity = self.normalizer.normalize_rate_limit_intensity(
                    int(remaining),
                    int(total),
                    reset_seconds
                )
                
                signals.append(RiskSignal(
                    signal_name="rate_limit_pressure",
                    presence=intensity > 0.0,
                    intensity=intensity,
                    confidence=0.95,  # High confidence from official headers
                    first_observed=now,
                    last_observed=now,
                    category=SignalCategory.RATE_CONSTRAINT,
                    raw_value={"remaining": remaining, "total": total, "reset": reset},
                ))
        
        # 429 responses
        response_codes = observation.api_response_codes
        count_429 = sum(1 for code in response_codes if code == 429)
        
        if count_429 > 0:
            intensity = min(1.0, count_429 / max(1, len(response_codes)))
            
            signals.append(RiskSignal(
                signal_name="rate_limit_429",
                presence=True,
                intensity=intensity,
                confidence=1.0,  # Direct observation
                first_observed=now,
                last_observed=now,
                category=SignalCategory.RATE_CONSTRAINT,
                raw_value={"count": count_429, "total": len(response_codes)},
            ))
        
        # Retry-After presence
        if observation.retry_after_values:
            max_retry = max(observation.retry_after_values)
            # Normalize to [0, 1] assuming max reasonable is 3600 seconds
            intensity = min(1.0, max_retry / 3600.0)
            
            signals.append(RiskSignal(
                signal_name="rate_limit_retry_after",
                presence=True,
                intensity=intensity,
                confidence=1.0,
                first_observed=now,
                last_observed=now,
                category=SignalCategory.RATE_CONSTRAINT,
                raw_value={"max_seconds": max_retry},
            ))
        
        return signals
    
    def extract_delivery_latency_signals(
        self,
        observation: RawPlatformObservation
    ) -> List[RiskSignal]:
        """
        Extract delivery path signals.
        
        KEY FOR SUPPRESSION DIAGNOSIS LATER.
        
        Signals:
        - publish_latency_inflation: Delayed publish confirmations
        - callback_delay: Missing or delayed callbacks
        - silent_retry_amplification: Silent retry patterns
        """
        signals = []
        now = observation.observation_timestamp
        
        # Publish latency
        if observation.publish_latencies_ms:
            observed = statistics.median(observation.publish_latencies_ms)
            baseline = 200.0  # Baseline 200ms for typical API
            max_expected = 5000.0  # 5 seconds
            
            intensity = self.normalizer.normalize_latency_intensity(
                observed,
                baseline,
                max_expected
            )
            
            if intensity > 0.0:
                signals.append(RiskSignal(
                    signal_name="publish_latency_inflation",
                    presence=True,
                    intensity=intensity,
                    confidence=0.85,  # Moderate confidence (network variance)
                    first_observed=now,
                    last_observed=now,
                    category=SignalCategory.DELIVERY_PATH,
                    raw_value={
                        "median_ms": observed,
                        "baseline_ms": baseline,
                    },
                ))
        
        # Callback delays
        if observation.callback_delays_ms:
            observed = statistics.median(observation.callback_delays_ms)
            baseline = 100.0
            max_expected = 10000.0  # 10
intensity = self.normalizer.normalize_latency_intensity(
            observed,
            baseline,
            max_expected
        )
        
        if intensity > 0.0 or observation.missing_callbacks > 0:
            # Factor in missing callbacks
            missing_ratio = observation.missing_callbacks / max(
                1,
                len(observation.callback_delays_ms) + observation.missing_callbacks
            )
            combined_intensity = max(intensity, missing_ratio)
            
            signals.append(RiskSignal(
                signal_name="callback_delay",
                presence=True,
                intensity=combined_intensity,
                confidence=0.80,
                first_observed=now,
                last_observed=now,
                category=SignalCategory.DELIVERY_PATH,
                raw_value={
                    "median_delay_ms": observed,
                    "missing_count": observation.missing_callbacks,
                },
            ))
    
    # Silent retry amplification
    if observation.silent_retry_count > 0:
        # Normalize: 1 retry = low, 10+ retries = high
        intensity = min(1.0, observation.silent_retry_count / 10.0)
        
        signals.append(RiskSignal(
            signal_name="silent_retry_amplification",
            presence=True,
            intensity=intensity,
            confidence=0.90,
            first_observed=now,
            last_observed=now,
            category=SignalCategory.DELIVERY_PATH,
            raw_value={"retry_count": observation.silent_retry_count},
        ))
    
    return signals

def extract_feature_availability_signals(
    self,
    observation: RawPlatformObservation
) -> List[RiskSignal]:
    """
    Extract feature availability signals.
    
    NO ASSUMPTION OF WHY.
    
    Signals:
    - endpoint_disabled: Specific API endpoints unavailable
    - feature_degraded: Features operating in degraded mode
    - upload_quality_capped: Media quality restrictions
    """
    signals = []
    now = observation.observation_timestamp
    
    # Disabled endpoints
    if observation.disabled_endpoints:
        count = len(observation.disabled_endpoints)
        # Normalize: assume 20+ endpoints = full restriction
        intensity = min(1.0, count / 20.0)
        
        signals.append(RiskSignal(
            signal_name="endpoint_disabled",
            presence=True,
            intensity=intensity,
            confidence=1.0,  # Direct observation
            first_observed=now,
            last_observed=now,
            category=SignalCategory.FEATURE_AVAILABILITY,
            raw_value={"endpoints": list(observation.disabled_endpoints)},
        ))
    
    # Degraded features
    if observation.degraded_features:
        count = len(observation.degraded_features)
        intensity = min(1.0, count / 10.0)
        
        signals.append(RiskSignal(
            signal_name="feature_degraded",
            presence=True,
            intensity=intensity,
            confidence=0.90,
            first_observed=now,
            last_observed=now,
            category=SignalCategory.FEATURE_AVAILABILITY,
            raw_value={"features": observation.degraded_features},
        ))
    
    # Upload quality caps
    if observation.upload_quality_caps:
        # Presence indicates restriction
        signals.append(RiskSignal(
            signal_name="upload_quality_capped",
            presence=True,
            intensity=0.7,  # Fixed intensity for binary presence
            confidence=0.95,
            first_observed=now,
            last_observed=now,
            category=SignalCategory.FEATURE_AVAILABILITY,
            raw_value=observation.upload_quality_caps,
        ))
    
    return signals

def extract_api_anomaly_signals(
    self,
    observation: RawPlatformObservation
) -> List[RiskSignal]:
    """
    Extract API anomaly signals.
    
    OFTEN PRECEDE VISIBLE SUPPRESSION.
    
    Signals:
    - response_schema_drift: API response structure changes
    - soft_error_clustering: Elevated soft error rates
    - api_latency_inflation: General API slowness
    """
    signals = []
    now = observation.observation_timestamp
    
    # Schema drift
    if observation.response_schema_changes:
        count = len(observation.response_schema_changes)
        intensity = min(1.0, count / 5.0)  # 5+ changes = high
        
        signals.append(RiskSignal(
            signal_name="response_schema_drift",
            presence=True,
            intensity=intensity,
            confidence=0.85,  # Schema detection has some uncertainty
            first_observed=now,
            last_observed=now,
            category=SignalCategory.API_ANOMALY,
            raw_value={"changes": observation.response_schema_changes},
        ))
    
    # Soft errors
    if observation.soft_error_codes:
        # Soft errors: 5xx, certain 4xx, etc.
        count = len(observation.soft_error_codes)
        total = len(observation.api_response_codes) if observation.api_response_codes else count
        
        intensity = self.normalizer.normalize_error_rate_intensity(
            count,
            total,
            baseline_error_rate=0.01  # 1% baseline
        )
        
        if intensity > 0.0:
            signals.append(RiskSignal(
                signal_name="soft_error_clustering",
                presence=True,
                intensity=intensity,
                confidence=0.95,
                first_observed=now,
                last_observed=now,
                category=SignalCategory.API_ANOMALY,
                raw_value={
                    "error_count": count,
                    "total_requests": total,
                    "codes": observation.soft_error_codes[:10],  # Limit size
                },
            ))
    
    # API latency inflation
    if observation.latency_percentiles:
        p95 = observation.latency_percentiles.get("p95", 0.0)
        baseline = 300.0  # 300ms baseline
        max_expected = 10000.0  # 10s
        
        intensity = self.normalizer.normalize_latency_intensity(
            p95,
            baseline,
            max_expected
        )
        
        if intensity > 0.0:
            signals.append(RiskSignal(
                signal_name="api_latency_inflation",
                presence=True,
                intensity=intensity,
                confidence=0.80,
                first_observed=now,
                last_observed=now,
                category=SignalCategory.API_ANOMALY,
                raw_value=observation.latency_percentiles,
            ))
    
    return signals
============================================================================
SIGNAL VALIDATION
============================================================================
class SignalValidator:
"""
Validates signal snapshots for correctness and consistency.
FAIL-FAST validation rules:
- Signal timestamps not in future
- Intensity within [0,1]
- No duplicate signal names
- Known platform
- Valid snapshot timestamp

Failing closed is intentional.
"""

@staticmethod
def validate_snapshot(snapshot: RiskSignalSnapshot) -> List[str]:
    """
    Validate a signal snapshot.
    
    Args:
        snapshot: Snapshot to validate
    
    Returns:
        List of validation errors (empty if valid)
    """
    errors = []
    
    # Platform validation
    if not Platform.is_valid(snapshot.platform):
        errors.append(f"Unknown platform: {snapshot.platform}")
    
    # Timestamp validation
    now = datetime.now(timezone.utc)
    if snapshot.snapshot_timestamp > now:
        errors.append(
            f"Snapshot timestamp {snapshot.snapshot_timestamp} is in the future"
        )
    
    # Volatility bounds
    if not (0.0 <= snapshot.signal_volatility <= 1.0):
        errors.append(
            f"Signal volatility {snapshot.signal_volatility} outside [0,1]"
        )
    
    # Coverage bounds
    if not (0.0 <= snapshot.signal_coverage <= 1.0):
        errors.append(
            f"Signal coverage {snapshot.signal_coverage} outside [0,1]"
        )
    
    # Signal validation
    signal_names = set()
    for signal in snapshot.risk_signals:
        # Duplicate names
        if signal.signal_name in signal_names:
            errors.append(f"Duplicate signal name: {signal.signal_name}")
        signal_names.add(signal.signal_name)
        
        # Intensity bounds
        if not (MIN_INTENSITY <= signal.intensity <= MAX_INTENSITY):
            errors.append(
                f"Signal {signal.signal_name} intensity {signal.intensity} "
                f"outside [{MIN_INTENSITY}, {MAX_INTENSITY}]"
            )
        
        # Confidence bounds
        if not (MIN_CONFIDENCE <= signal.confidence <= MAX_CONFIDENCE):
            errors.append(
                f"Signal {signal.signal_name} confidence {signal.confidence} "
                f"outside [{MIN_CONFIDENCE}, {MAX_CONFIDENCE}]"
            )
        
        # Timestamp ordering
        if signal.last_observed < signal.first_observed:
            errors.append(
                f"Signal {signal.signal_name} last_observed before first_observed"
            )
        
        # Future timestamps
        if signal.first_observed > now or signal.last_observed > now:
            errors.append(
                f"Signal {signal.signal_name} has future timestamp"
            )
    
    return errors

@staticmethod
def validate_or_raise(snapshot: RiskSignalSnapshot):
    """
    Validate snapshot and raise exception if invalid.
    
    Args:
        snapshot: Snapshot to validate
    
    Raises:
        ValueError: If validation fails
    """
    errors = SignalValidator.validate_snapshot(snapshot)
    if errors:
        raise ValueError(
            f"Snapshot validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
============================================================================
SIGNAL HASHER
============================================================================
class SignalHasher:
"""
Produces deterministic hashes of signal snapshots.
USES:
- Suppression analyzer
- Trust regression
- Risk drift detection
- Deduplication
- Audit trails
"""

@staticmethod
def hash_snapshot(snapshot: RiskSignalSnapshot) -> str:
    """
    Compute SHA256 hash of snapshot.
    
    This is already implemented in RiskSignalSnapshot._compute_hash(),
    but exposed here for explicit use.
    
    Args:
        snapshot: Snapshot to hash
    
    Returns:
        64-character hex SHA256 hash
    """
    return snapshot.snapshot_hash

@staticmethod
def hash_signal(signal: RiskSignal) -> str:
    """
    Compute hash of individual signal.
    
    Args:
        signal: Signal to hash
    
    Returns:
        64-character hex SHA256 hash
    """
    canonical = json.dumps(signal.to_dict(), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()

@staticmethod
def verify_snapshot_integrity(snapshot: RiskSignalSnapshot) -> bool:
    """
    Verify snapshot hash matches content.
    
    Args:
        snapshot: Snapshot to verify
    
    Returns:
        True if hash is valid, False otherwise
    """
    recomputed = snapshot._compute_hash()
    return recomputed == snapshot.snapshot_hash
============================================================================
SIGNAL WATCHDOG
============================================================================
class SignalWatchdog:
"""
Monitors for anomalous signal patterns.
MONITORS:
- Sudden platform-wide anomalies
- Per-account signal explosions
- Extractor version drift
- Silent signal loss

CAN TRIGGER:
- Posting slowdowns
- Experiment freezes
- Trust re-evaluation
"""

def __init__(self):
    self.account_signal_counts: Dict[str, deque] = defaultdict(
        lambda: deque(maxlen=100)
    )
    self.platform_anomaly_tracker: Dict[str, List[datetime]] = defaultdict(list)
    self.last_signal_by_account: Dict[str, datetime] = {}

def check_signal_explosion(
    self,
    account_id: str,
    snapshot: RiskSignalSnapshot
) -> bool:
    """
    Check if account has sudden signal explosion.
    
    Args:
        account_id: Account to check
        snapshot: Latest snapshot
    
    Returns:
        True if explosion detected
    """
    signal_count = len(snapshot.risk_signals)
    timestamp = snapshot.snapshot_timestamp
    
    # Record count
    self.account_signal_counts[account_id].append({
        "count": signal_count,
        "timestamp": timestamp,
    })
    
    history = self.account_signal_counts[account_id]
    
    if len(history) < 2:
        return False
    
    # Check recent hour
    recent_cutoff = timestamp - timedelta(hours=1)
    recent = [h for h in history if h["timestamp"] >= recent_cutoff]
    
    if len(recent) < 2:
        return False
    
    # Explosion = 3x increase in signal count within 1 hour
    counts = [h["count"] for h in recent]
    max_count = max(counts)
    min_count = min(counts[:-1]) if len(counts) > 1 else counts[0]
    
    if min_count > 0 and max_count / min_count >= 3.0:
        return True
    
    # Or absolute threshold
    if signal_count >= WATCHDOG_SIGNAL_EXPLOSION_THRESHOLD:
        return True
    
    return False

def check_platform_anomaly(
    self,
    platform: str,
    snapshots: List[RiskSignalSnapshot]
) -> bool:
    """
    Check if platform has widespread anomalies.
    
    Args:
        platform: Platform to check
        snapshots: Recent snapshots across accounts
    
    Returns:
        True if platform-wide anomaly detected
    """
    if not snapshots:
        return False
    
    # Count accounts with high-intensity signals
    affected_accounts = 0
    
    for snapshot in snapshots:
        if snapshot.platform != platform:
            continue
        
        # Check for any high-intensity signal
        max_intensity = max(
            (s.intensity for s in snapshot.risk_signals),
            default=0.0
        )
        
        if max_intensity >= 0.7:
            affected_accounts += 1
    
    if len(snapshots) == 0:
        return False
    
    affected_ratio = affected_accounts / len(snapshots)
    
    return affected_ratio >= WATCHDOG_PLATFORM_ANOMALY_THRESHOLD

def check_silent_signal_loss(
    self,
    account_id: str,
    current_time: datetime
) -> bool:
    """
    Check if account has stopped emitting signals.
    
    Args:
        account_id: Account to check
        current_time: Current timestamp
    
    Returns:
        True if silent loss detected
    """
    last_signal = self.last_signal_by_account.get(account_id)
    
    if not last_signal:
        return False
    
    hours_since = (current_time - last_signal).total_seconds() / 3600
    
    return hours_since >= WATCHDOG_SILENT_LOSS_HOURS

def record_signal_observation(
    self,
    account_id: str,
    timestamp: datetime
):
    """Record that signals were observed for an account."""
    self.last_signal_by_account[account_id] = timestamp

def check_version_drift(
    self,
    snapshots: List[RiskSignalSnapshot]
) -> bool:
    """
    Check if multiple extractor versions are in use.
    
    Args:
        snapshots: Recent snapshots
    
    Returns:
        True if version drift detected
    """
    versions = {s.extraction_version for s in snapshots}
    return len(versions) > 1
============================================================================
CONVENIENCE FUNCTIONS
============================================================================
def extract_risk_signals(
observation: RawPlatformObservation,
extractor: Optional[RiskSignalExtractor] = None
) -> RiskSignalSnapshot:
"""
Convenience function to extract signals from observation.
Args:
    observation: Raw platform observation
    extractor: Optional extractor instance (creates new if None)

Returns:
    Risk signal snapshot
"""
if extractor is None:
    extractor = RiskSignalExtractor()

snapshot = extractor.extract_signals(observation)

# Validate before returning
SignalValidator.validate_or_raise(snapshot)

return snapshot
def create_empty_snapshot(
account_id: str,
platform: str,
timestamp: Optional[datetime] = None
) -> RiskSignalSnapshot:
"""
Create an empty signal snapshot (no signals observed).
Args:
    account_id: Account identifier
    platform: Platform name
    timestamp: Snapshot timestamp (defaults to now)

Returns:
    Empty snapshot
"""
if timestamp is None:
    timestamp = datetime.now(timezone.utc)

return RiskSignalSnapshot(
    account_id=account_id,
    platform=platform,
    snapshot_timestamp=timestamp,
    risk_signals=tuple(),
    signal_volatility=0.0,
    signal_coverage=0.0,
    extraction_version=EXTRACTION_VERSION,
)
============================================================================
DETERMINISM GUARANTEE
============================================================================
def verify_determinism(
observation: RawPlatformObservation,
expected_hash: str
) -> bool:
"""
Verify that extraction is deterministic.
Given the same observation, must produce the same hash.

Args:
    observation: Raw observation
    expected_hash: Expected snapshot hash

Returns:
    True if hash matches
"""
snapshot = extract_risk_signals(observation)
return snapshot.snapshot_hash == expected_hash

============================================================================
MODULE EXPORTS
============================================================================
all = [
# Core data structures
"RiskSignal",
"RiskSignalSnapshot",
"RawPlatformObservation",
# Enums
"Platform",
"SignalCategory",

# Main extractor
"RiskSignalExtractor",

# Analysis components
"SignalNormalizer",
"SignalVolatilityAnalyzer",
"SignalCoverageEstimator",

# Validation & integrity
"SignalValidator",
"SignalHasher",
"SignalWatchdog",

# Convenience functions
"extract_risk_signals",
"create_empty_snapshot",
"verify_determinism",

# Constants
"EXTRACTION_VERSION",
]
============================================================================
HARD INVARIANTS (ENFORCED AT MODULE LEVEL)
============================================================================
def _enforce_invariants():
"""
Enforce hard invariants at module load time.
These are compile-time checks to prevent architectural violations.
"""
# Ensure no decision logic in this module
module_code = open(__file__).read()

forbidden_patterns = [
    "should_suppress",
    "should_throttle",
    "trust_score",
    "policy_violation",
    "enforcement_action",
    "shadowban",
    "guideline_strike",
]

for pattern in forbidden_patterns:
    if pattern in module_code:
        raise RuntimeError(
            f"ARCHITECTURAL VIOLATION: Found '{pattern}' in risk_signal_extractor.py. "
            f"This module must contain zero decision logic."
        )
Uncomment to enable invariant checking (remove for production to avoid overhead)    #VERY FUCKING IMPORTANT TO KEEP THIS COMMENTED OUT FOR PRODUCTION!
_enforce_invariants()


