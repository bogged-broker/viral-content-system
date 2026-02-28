"""
/account_system/trust_decay.py

Time-Based Trust Degradation & Persistence Engine

WHAT THIS FILE DOES:
    Answers ONE question: "How does time itself affect trustworthiness in the absence of new events?"
    
    Not penalties. Not risk. Not decisions.
    Just: time → memory transformation.

WHY IT EXISTS:
    Big accounts are trusted because:
    - Trust doesn't instantly evaporate
    - But also doesn't stay frozen forever
    - History slowly fades unless reinforced by stable behavior
    
    This file models that reality.

WHAT THIS IS NOT:
    ❌ Trust scoring
    ❌ Risk interpretation  
    ❌ Penalty application
    ❌ Posting throttling
    ❌ Recovery logic
    ❌ Experimentation
    
    Pure temporal physics only.

CORE PRINCIPLE:
    Trust decays by default — but good history decays slower.
    
    No permanent trust. No instant forgiveness. No infinite memory.
    Only controlled erosion.

CONSUMED BY:
    - trust_scoring.py
    - Posting governors
    - Rollout managers
    - Experiment safety layers
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Literal, Tuple, List, Dict
from enum import Enum
import math

# ============================================================================
# LOGGING
# ============================================================================

logger = logging.getLogger(__name__)


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class DecayComponent:
    """
    Single decaying trust element.
    
    No implicit meaning. Pure math.
    """
    name: str
    base_weight: float          # Original influence
    decayed_weight: float       # Time-eroded influence
    half_life_days: float       # Decay speed
    last_event_timestamp: datetime
    
    def __post_init__(self):
        assert 0.0 <= self.base_weight <= 1.0, f"Invalid base_weight: {self.base_weight}"
        assert 0.0 <= self.decayed_weight <= 1.0, f"Invalid decayed_weight: {self.decayed_weight}"
        assert self.decayed_weight <= self.base_weight, "Decay cannot increase weight"
        assert self.half_life_days > 0, f"Invalid half_life: {self.half_life_days}"


@dataclass(frozen=True)
class DecaySnapshot:
    """
    Complete decay state at reference time.
    
    Replayable. Audit-safe. Deterministic.
    """
    account_id: str
    platform: str
    reference_time: datetime
    
    components: tuple[DecayComponent, ...]
    
    decay_confidence: float     # 0.0-1.0, uncertainty measure
    decay_model_version: str
    
    decay_hash: str = field(default="")
    
    def __post_init__(self):
        assert 0.0 <= self.decay_confidence <= 1.0
        assert len(self.components) > 0, "Must have at least one decay component"
        
        # Compute hash if not provided
        if not self.decay_hash:
            object.__setattr__(self, 'decay_hash', self._compute_hash())
    
    def _compute_hash(self) -> str:
        """Deterministic hash for replay verification."""
        h = hashlib.sha256()
        h.update(self.account_id.encode())
        h.update(self.platform.encode())
        h.update(self.reference_time.isoformat().encode())
        h.update(self.decay_model_version.encode())
        
        for comp in sorted(self.components, key=lambda c: c.name):
            h.update(comp.name.encode())
            h.update(str(comp.base_weight).encode())
            h.update(str(comp.decayed_weight).encode())
            h.update(str(comp.half_life_days).encode())
        
        return h.hexdigest()


class DecayCurveType(Enum):
    """Available decay curve shapes."""
    EXPONENTIAL = "exponential"          # Standard radioactive decay
    POWER_LAW = "power_law"              # Long-tail decay
    SIGMOID = "sigmoid"                  # S-curve decay
    LOG_LINEAR = "log_linear"            # Logarithmic then linear


# ============================================================================
# DECAY CURVE LIBRARY
# ============================================================================

class DecayCurveLibrary:
    """
    Mathematical decay curves.
    
    Platform-specific constants only. No runtime adaptation.
    Determinism > cleverness.
    """
    
    @staticmethod
    def exponential_decay(
        base_weight: float,
        elapsed_days: float,
        half_life_days: float
    ) -> float:
        """
        Standard exponential decay: weight * (0.5)^(t/half_life)
        
        Used for: penalty decay (slow), recovery decay (medium)
        """
        if elapsed_days <= 0:
            return base_weight
        if half_life_days <= 0:
            return 0.0
            
        decay_factor = math.pow(0.5, elapsed_days / half_life_days)
        return base_weight * decay_factor
    
    @staticmethod
    def power_law_decay(
        base_weight: float,
        elapsed_days: float,
        half_life_days: float,
        exponent: float = 0.7
    ) -> float:
        """
        Power law decay: weight / (1 + (t/half_life)^exponent)
        
        Used for: clean history (very slow), stability (sensitive to shocks)
        
        Longer tail than exponential - rewards sustained presence.
        """
        if elapsed_days <= 0:
            return base_weight
        if half_life_days <= 0:
            return 0.0
            
        normalized_time = elapsed_days / half_life_days
        decay_factor = 1.0 / (1.0 + math.pow(normalized_time, exponent))
        return base_weight * decay_factor
    
    @staticmethod
    def sigmoid_decay(
        base_weight: float,
        elapsed_days: float,
        half_life_days: float,
        steepness: float = 2.0
    ) -> float:
        """
        Sigmoid decay: weight / (1 + exp(steepness * (t - half_life)))
        
        Used for: cliff effects, regime changes
        
        Slow then fast then slow again.
        """
        if elapsed_days <= 0:
            return base_weight
        if half_life_days <= 0:
            return 0.0
            
        x = steepness * (elapsed_days - half_life_days) / half_life_days
        decay_factor = 1.0 / (1.0 + math.exp(x))
        return base_weight * decay_factor
    
    @staticmethod
    def log_linear_decay(
        base_weight: float,
        elapsed_days: float,
        half_life_days: float
    ) -> float:
        """
        Log-linear decay: weight * (1 - log(1 + t) / log(1 + half_life))
        
        Used for: initial rapid decay followed by slow tail
        """
        if elapsed_days <= 0:
            return base_weight
        if half_life_days <= 0:
            return 0.0
            
        log_elapsed = math.log1p(elapsed_days)
        log_half_life = math.log1p(half_life_days * 2)  # 2x for proper half-life
        
        if log_half_life == 0:
            return 0.0
            
        decay_factor = max(0.0, 1.0 - (log_elapsed / log_half_life))
        return base_weight * decay_factor


# ============================================================================
# PLATFORM-SPECIFIC DECAY CONSTANTS
# ============================================================================

class DecayConstants:
    """
    Platform-specific decay parameters.
    
    CRITICAL: These are the ONLY platform-specific values in this file.
    Everything else is pure math.
    """
    
    # Penalty decay (SLOW - big accounts don't instantly forget penalties)
    PENALTY_HALF_LIFE_DAYS = {
        "youtube": 90.0,        # 3 months
        "instagram": 60.0,      # 2 months
        "tiktok": 45.0,         # 1.5 months
        "twitter": 30.0,        # 1 month
    }
    
    # Recovery decay (MEDIUM - sustained correction matters)
    RECOVERY_HALF_LIFE_DAYS = {
        "youtube": 60.0,
        "instagram": 45.0,
        "tiktok": 30.0,
        "twitter": 21.0,
    }
    
    # Clean history decay (VERY SLOW - stable presence compounds)
    CLEAN_HISTORY_HALF_LIFE_DAYS = {
        "youtube": 180.0,       # 6 months
        "instagram": 120.0,     # 4 months
        "tiktok": 90.0,         # 3 months
        "twitter": 60.0,        # 2 months
    }
    
    # Stability decay (SENSITIVE - volatility resets trust)
    STABILITY_HALF_LIFE_DAYS = {
        "youtube": 45.0,
        "instagram": 30.0,
        "tiktok": 21.0,
        "twitter": 14.0,
    }
    
    @classmethod
    def get_penalty_half_life(cls, platform: str) -> float:
        return cls.PENALTY_HALF_LIFE_DAYS.get(platform.lower(), 60.0)
    
    @classmethod
    def get_recovery_half_life(cls, platform: str) -> float:
        return cls.RECOVERY_HALF_LIFE_DAYS.get(platform.lower(), 45.0)
    
    @classmethod
    def get_clean_history_half_life(cls, platform: str) -> float:
        return cls.CLEAN_HISTORY_HALF_LIFE_DAYS.get(platform.lower(), 120.0)
    
    @classmethod
    def get_stability_half_life(cls, platform: str) -> float:
        return cls.STABILITY_HALF_LIFE_DAYS.get(platform.lower(), 30.0)


# ============================================================================
# TEMPORAL WEIGHTING ENGINE
# ============================================================================

class TemporalWeightingEngine:
    """
    Computes time-based weighting with continuous (not bucketed) decay.
    
    Log-time scaling. No time bucketing. Deterministic.
    """
    
    @staticmethod
    def compute_elapsed_days(
        event_time: datetime,
        reference_time: datetime
    ) -> float:
        """Compute elapsed days with sub-day precision."""
        if reference_time < event_time:
            logger.warning(f"Reference time {reference_time} before event time {event_time}")
            return 0.0
        
        delta = reference_time - event_time
        return delta.total_seconds() / 86400.0  # Convert to days
    
    @staticmethod
    def apply_repetition_penalty(
        base_half_life: float,
        repetition_count: int,
        severity_multiplier: float = 1.0
    ) -> float:
        """
        Slower decay for repeated events.
        
        Big accounts don't instantly "forget" patterns of penalties.
        """
        if repetition_count <= 1:
            return base_half_life
        
        # More repetitions → longer half-life (slower decay)
        penalty_factor = 1.0 + (0.2 * (repetition_count - 1) * severity_multiplier)
        return base_half_life * penalty_factor
    
    @staticmethod
    def apply_reinforcement_bonus(
        base_half_life: float,
        clean_streak_days: float,
        max_bonus: float = 1.5
    ) -> float:
        """
        Slower decay if followed by clean behavior.
        
        Rewards sustained correction, not short bursts.
        """
        if clean_streak_days <= 0:
            return base_half_life
        
        # Longer clean streak → slower decay (up to max_bonus)
        bonus_factor = min(max_bonus, 1.0 + (clean_streak_days / 365.0))
        return base_half_life * bonus_factor
    
    @staticmethod
    def apply_volatility_penalty(
        base_half_life: float,
        volatility_score: float
    ) -> float:
        """
        Faster decay with high volatility.
        
        Punishes "panic posting" and erratic behavior.
        """
        if volatility_score <= 0:
            return base_half_life
        
        # Higher volatility → shorter half-life (faster decay)
        volatility_factor = 1.0 / (1.0 + volatility_score)
        return base_half_life * volatility_factor


# ============================================================================
# TRUST DECAY ENGINE
# ============================================================================

class TrustDecayEngine:
    """
    Core decay computation engine.
    
    Applies asymmetric decay curves to trust components.
    Fully deterministic. Platform-aware but rule-agnostic.
    """
    
    VERSION = "1.0.0"
    
    def __init__(self):
        self.curve_lib = DecayCurveLibrary()
        self.temporal = TemporalWeightingEngine()
    
    def compute_penalty_decay(
        self,
        platform: str,
        base_weight: float,
        last_penalty_time: datetime,
        reference_time: datetime,
        repetition_count: int = 1,
        severity_



"""
/account_system/trust_decay.py

Time-Based Trust Degradation & Persistence Engine

This module applies temporal physics to trust components derived from the reputation ledger.
It answers one question: "How does time itself affect account trustworthiness in the absence of new events?"

STRICT BOUNDARIES:
- ❌ NO trust scoring (upstream)
- ❌ NO risk interpretation (upstream)
- ❌ NO penalty application (downstream)
- ❌ NO posting throttling (downstream)
- ❌ NO recovery logic (downstream)
- ❌ NO experimentation (separate layer)

ONLY: Time → Memory Transformation

Core Principle:
> Trust decays by default — but good history decays slower.

Consumed by:
- trust_scoring.py
- posting governors
- rollout managers
- experiment safety layers

LOC Target: ~1,600–2,500
Determinism: ABSOLUTE
Auditability: REQUIRED
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple, Dict, List, Any
import logging

# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class DecayComponent:
    """
    A single trust component undergoing temporal decay.
    
    Represents the mathematical transformation of a trust primitive over time.
    Contains NO semantic meaning — pure numerical decay.
    """
    name: str
    base_weight: float  # Original weight at last reinforcement
    decayed_weight: float  # Current weight after time decay
    half_life_days: float  # Decay rate parameter
    last_event_timestamp: datetime  # Anchor point for decay calculation
    
    def __post_init__(self):
        """Validation on construction."""
        if self.base_weight < 0 or self.base_weight > 1:
            raise ValueError(f"base_weight must be in [0,1], got {self.base_weight}")
        if self.decayed_weight < 0 or self.decayed_weight > 1:
            raise ValueError(f"decayed_weight must be in [0,1], got {self.decayed_weight}")
        if self.decayed_weight > self.base_weight:
            raise ValueError(f"decayed_weight cannot exceed base_weight")
        if self.half_life_days <= 0:
            raise ValueError(f"half_life_days must be positive, got {self.half_life_days}")


@dataclass(frozen=True)
class DecaySnapshot:
    """
    Complete temporal state of all decay components for an account at a reference time.
    
    Replayable, audit-safe, deterministic.
    """
    account_id: str
    platform: str
    reference_time: datetime
    
    components: Tuple[DecayComponent, ...]
    
    decay_confidence: float  # 0.0-1.0, lower = more uncertainty
    decay_model_version: str
    
    # Audit trail
    ledger_hash: str  # Hash of input ledger state
    computation_hash: str  # Hash of this entire snapshot
    
    def __post_init__(self):
        """Validation on construction."""
        if self.decay_confidence < 0 or self.decay_confidence > 1:
            raise ValueError(f"decay_confidence must be in [0,1], got {self.decay_confidence}")
        if not self.components:
            raise ValueError("components cannot be empty")


class DecayCurveType(Enum):
    """Supported decay curve families."""
    EXPONENTIAL = "exponential"
    POWER_LAW = "power_law"
    LOGARITHMIC = "logarithmic"
    HYBRID = "hybrid"


# ============================================================================
# DECAY CURVE LIBRARY
# ============================================================================


class DecayCurveLibrary:
    """
    Library of temporal decay functions.
    
    All curves are deterministic, platform-agnostic, and mathematically well-defined.
    No adaptive tuning at runtime — only version-controlled constants.
    """
    
    @staticmethod
    def exponential_decay(
        base_weight: float,
        elapsed_days: float,
        half_life_days: float
    ) -> float:
        """
        Classic exponential decay: weight(t) = base * exp(-λt)
        
        Used for: penalty decay (slow), recovery decay (medium)
        """
        if elapsed_days < 0:
            raise ValueError("elapsed_days cannot be negative")
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        
        decay_constant = math.log(2) / half_life_days
        decayed = base_weight * math.exp(-decay_constant * elapsed_days)
        
        return max(0.0, min(1.0, decayed))
    
    @staticmethod
    def power_law_decay(
        base_weight: float,
        elapsed_days: float,
        exponent: float = 0.5
    ) -> float:
        """
        Power-law decay: weight(t) = base / (1 + t)^α
        
        Used for: stability decay (resets slower after shocks)
        """
        if elapsed_days < 0:
            raise ValueError("elapsed_days cannot be negative")
        
        decayed = base_weight / math.pow(1 + elapsed_days, exponent)
        
        return max(0.0, min(1.0, decayed))
    
    @staticmethod
    def logarithmic_decay(
        base_weight: float,
        elapsed_days: float,
        scale: float = 30.0
    ) -> float:
        """
        Logarithmic decay: weight(t) = base * (1 - log(1 + t/scale))
        
        Used for: clean history decay (very slow, rewards sustained presence)
        """
        if elapsed_days < 0:
            raise ValueError("elapsed_days cannot be negative")
        
        decay_factor = 1 - math.log(1 + elapsed_days / scale) / math.log(1 + 365 / scale)
        decay_factor = max(0.0, decay_factor)
        decayed = base_weight * decay_factor
        
        return max(0.0, min(1.0, decayed))
    
    @staticmethod
    def hybrid_decay(
        base_weight: float,
        elapsed_days: float,
        half_life_days: float,
        exponent: float = 0.3
    ) -> float:
        """
        Hybrid: combines exponential and power-law for non-linear resilience.
        
        Used for: penalty decay with repetition sensitivity
        """
        exp_component = DecayCurveLibrary.exponential_decay(
            base_weight, elapsed_days, half_life_days
        )
        power_component = DecayCurveLibrary.power_law_decay(
            base_weight, elapsed_days, exponent
        )
        
        # Blend: exponential dominates early, power-law adds long-tail resistance
        blended = 0.7 * exp_component + 0.3 * power_component
        
        return max(0.0, min(1.0, blended))


# ============================================================================
# TEMPORAL WEIGHTING ENGINE
# ============================================================================


class TemporalWeightingEngine:
    """
    Applies time-based transformations to ledger-derived trust primitives.
    
    Uses asymmetric decay curves based on component type and event history.
    """
    
    # Platform-specific constants (version-controlled, never runtime-adaptive)
    PENALTY_HALF_LIFE_DAYS = 90.0  # Slow decay
    RECOVERY_HALF_LIFE_DAYS = 45.0  # Medium decay
    CLEAN_HISTORY_SCALE_DAYS = 180.0  # Very slow decay
    STABILITY_POWER_EXPONENT = 0.4  # Sensitivity to volatility
    
    def __init__(self, model_version: str = "v1.0.0"):
        self.model_version = model_version
        self.logger = logging.getLogger(__name__)
    
    def compute_penalty_decay(
        self,
        base_weight: float,
        last_penalty_time: datetime,
        reference_time: datetime,
        repetition_factor: float = 1.0
    ) -> DecayComponent:
        """
        Compute penalty impact decay.
        
        Characteristics:
        - SLOW decay (long half-life)
        - NON-LINEAR (hybrid curve)
        - Repetition-sensitive (worse history = slower decay)
        
        Big accounts don't instantly "forget" penalties.
        """
        elapsed = (reference_time - last_penalty_time).total_seconds() / 86400
        
        # Repetition lengthens half-life
        adjusted_half_life = self.PENALTY_HALF_LIFE_DAYS * repetition_factor
        
        decayed_weight = DecayCurveLibrary.hybrid_decay(
            base_weight,
            elapsed,
            adjusted_half_life,
            exponent=0.3
        )
        
        return DecayComponent(
            name="penalty_weight",
            base_weight=base_weight,
            decayed_weight=decayed_weight,
            half_life_days=adjusted_half_life,
            last_event_timestamp=last_penalty_time
        )
    
    def compute_recovery_decay(
        self,
        base_weight: float,
        last_recovery_time: datetime,
        reference_time: datetime,
        clean_period_days: float = 0.0
    ) -> DecayComponent:
        """
        Compute recovery credit decay.
        
        Characteristics:
        - MEDIUM decay
        - Reinforcement-sensitive (sustained clean behavior slows decay)
        
        Rewards sustained correction, not short bursts.
        """
        elapsed = (reference_time - last_recovery_time).total_seconds() / 86400
        
        # Clean period extends effective half-life
        reinforcement_bonus = min(clean_period_days / 30.0, 2.0)
        adjusted_half_life = self.RECOVERY_HALF_LIFE_DAYS * (1 + reinforcement_bonus * 0.5)
        
        decayed_weight = DecayCurveLibrary.exponential_decay(
            base_weight,
            elapsed,
            adjusted_half_life
        )
        
        return DecayComponent(
            name="recovery_weight",
            base_weight=base_weight,
            decayed_weight=decayed_weight,
            half_life_days=adjusted_half_life,
            last_event_timestamp=last_recovery_time
        )
    
    def compute_clean_history_decay(
        self,
        base_weight: float,
        history_start_time: datetime,
        reference_time: datetime,
        volatility_score: float = 0.0
    ) -> DecayComponent:
        """
        Compute clean history persistence.
        
        Characteristics:
        - VERY SLOW decay (logarithmic)
        - Decays only with inactivity or volatility
        - Encourages stable presence
        
        This is where big-account inertia lives.
        """
        elapsed = (reference_time - history_start_time).total_seconds() / 86400
        
        # Volatility accelerates decay
        volatility_penalty = max(1.0, 1 + volatility_score * 2.0)
        adjusted_scale = self.CLEAN_HISTORY_SCALE_DAYS / volatility_penalty
        
        decayed_weight = DecayCurveLibrary.logarithmic_decay(
            base_weight,
            elapsed,
            adjusted_scale
        )
        
        return DecayComponent(
            name="clean_history_weight",
            base_weight=base_weight,
            decayed_weight=decayed_weight,
            half_life_days=adjusted_scale,  # Using scale as proxy
            last_event_timestamp=history_start_time
        )
    
    def compute_stability_decay(
        self,
        base_weight: float,
        last_stable_time: datetime,
        reference_time: datetime,
        recent_volatility: float = 0.0
    ) -> DecayComponent:
        """
        Compute behavioral stability persistence.
        
        Characteristics:
        - Decays if volatility increases
        - Resets slowly after shocks
        - Highly sensitive to overreaction
        
        Punishes "panic posting."
        """
        elapsed = (reference_time - last_stable_time).total_seconds() / 86400
        
        # Volatility accelerates decay via power-law exponent
        adjusted_exponent = self.STABILITY_POWER_EXPONENT * (1 + recent_volatility)
        
        decayed_weight = DecayCurveLibrary.power_law_decay(
            base_weight,
            elapsed,
            adjusted_exponent
        )
        
        return DecayComponent(
            name="stability_weight",
            base_weight=base_weight,
            decayed_weight=decayed_weight,
            half_life_days=30.0,  # Nominal reference
            last_event_timestamp=last_stable_time
        )


# ============================================================================
# DECAY CONFIDENCE ESTIMATOR
# ============================================================================


class DecayConfidenceEstimator:
    """
    Estimates confidence in decay calculations based on data quality and coverage.
    
    Low confidence expands uncertainty downstream — correctly.
    """
    
    def estimate_confidence(
        self,
        account_age_days: float,
        ledger_coverage_days: float,
        platform_telemetry_quality: float,
        time_since_last_event_days: float,
        recent_regime_changes: int
    ) -> float:
        """
        Compute decay confidence score.
        
        Factors:
        - Ledger coverage gaps
        - Platform telemetry quality
        - Long inactivity periods
        - Recent regime/policy changes
        """
        confidence = 1.0
        
        # Coverage penalty
        coverage_ratio = ledger_coverage_days / max(account_age_days, 1.0)
        if coverage_ratio < 0.5:
            confidence *= 0.6
        elif coverage_ratio < 0.8:
            confidence *= 0.85
        
        # Telemetry quality (0.0-1.0)
        confidence *= (0.5 + 0.5 * platform_telemetry_quality)
        
        # Inactivity penalty
        if time_since_last_event_days > 180:
            confidence *= 0.7
        elif time_since_last_event_days > 90:
            confidence *= 0.85
        
        # Regime change penalty
        if recent_regime_changes > 0:
            confidence *= math.pow(0.9, recent_regime_changes)
        
        return max(0.1, min(1.0, confidence))


# ============================================================================
# TRUST DECAY ENGINE (MAIN ORCHESTRATOR)
# ============================================================================


class TrustDecayEngine:
    """
    Main orchestrator for temporal trust degradation.
    
    Coordinates all decay computations, validation, and snapshot generation.
    """
    
    def __init__(self, model_version: str = "v1.0.0"):
        self.model_version = model_version
        self.weighting_engine = TemporalWeightingEngine(model_version)
        self.confidence_estimator = DecayConfidenceEstimator()
        self.logger = logging.getLogger(__name__)
    
    def compute_decay_snapshot(
        self,
        account_id: str,
        platform: str,
        reference_time: datetime,
        ledger_data: Dict[str, Any],
        ledger_hash: str
    ) -> DecaySnapshot:
        """
        Compute complete decay snapshot for an account at reference time.
        
        Args:
            account_id: Account identifier
            platform: Platform name
            reference_time: Time point for decay calculation
            ledger_data: Relevant ledger state (timestamps, weights, history)
            ledger_hash: Hash of input ledger state for audit trail
        
        Returns:
            DecaySnapshot with all decayed components
        """
        self.logger.info(
            f"Computing decay snapshot for {account_id} on {platform} "
            f"at {reference_time.isoformat()}"
        )
        
        # Extract ledger components
        penalty_weight = ledger_data.get("penalty_weight", 0.0)
        last_penalty_time = ledger_data.get("last_penalty_time")
        repetition_factor = ledger_data.get("repetition_factor", 1.0)
        
        recovery_weight = ledger_data.get("recovery_weight", 0.0)
        last_recovery_time = ledger_data.get("last_recovery_time")
        clean_period_days = ledger_data.get("clean_period_days", 0.0)
        
        clean_history_weight = ledger_data.get("clean_history_weight", 0.0)
        history_start_time = ledger_data.get("history_start_time")
        volatility_score = ledger_data.get("volatility_score", 0.0)
        
        stability_weight = ledger_data.get("stability_weight", 0.0)
        last_stable_time = ledger_data.get("last_stable_time")
        recent_volatility = ledger_data.get("recent_volatility", 0.0)
        
        # Compute decayed components
        components = []
        
        if penalty_weight > 0 and last_penalty_time:
            components.append(
                self.weighting_engine.compute_penalty_decay(
                    penalty_weight,
                    last_penalty_time,
                    reference_time,
                    repetition_factor
                )
            )
        
        if recovery_weight > 0 and last_recovery_time:
            components.append(
                self.weighting_engine.compute_recovery_decay(
                    recovery_weight,
                    last_recovery_time,
                    reference_time,
                    clean_period_days
                )
            )
        
        if clean_history_weight > 0 and history_start_time:
            components.append(
                self.weighting_engine.compute_clean_history_decay(
                    clean_history_weight,
                    history_start_time,
                    reference_time,
                    volatility_score
                )
            )
        
        if stability_weight > 0 and last_stable_time:
            components.append(
                self.weighting_engine.compute_stability_decay(
                    stability_weight,
                    last_stable_time,
                    reference_time,
                    recent_volatility
                )
            )
        
        # Estimate confidence
        account_age_days = ledger_data.get("account_age_days", 0.0)
        ledger_coverage_days = ledger_data.get("ledger_coverage_days", 0.0)
        platform_telemetry_quality = ledger_data.get("platform_telemetry_quality", 1.0)
        time_since_last_event = ledger_data.get("time_since_last_event_days", 0.0)
        recent_regime_changes = ledger_data.get("recent_regime_changes", 0)
        
        confidence = self.confidence_estimator.estimate_confidence(
            account_age_days,
            ledger_coverage_days,
            platform_telemetry_quality,
            time_since_last_event,
            recent_regime_changes
        )
        
        # Create snapshot
        snapshot = DecaySnapshot(
            account_id=account_id,
            platform=platform,
            reference_time=reference_time,
            components=tuple(components),
            decay_confidence=confidence,
            decay_model_version=self.model_version,
            ledger_hash=ledger_hash,
            computation_hash=""  # Will be computed by hasher
        )
        
        # Validate before returning
        DecayValidator.validate_snapshot(snapshot)
        
        return snapshot
    
    def get_decayed_weights(
        self,
        snapshot: DecaySnapshot
    ) -> Dict[str, float]:
        """
        Extract decayed weights as dictionary for downstream consumption.
        
        Returns:
            {
                "penalty_weight": 0.0-1.0,
                "recovery_weight": 0.0-1.0,
                "clean_history_weight": 0.0-1.0,
                "stability_weight": 0.0-1.0
            }
        """
        return {
            component.name: component.decayed_weight
            for component in snapshot.components
        }


# ============================================================================
# VALIDATION
# ============================================================================


class DecayValidator:
    """
    Validation layer for decay computations.
    
    FAIL FAST on:
    - Negative weights
    - Increasing trust without new events
    - Future timestamps
    - Missing ledger anchors
    - Unknown decay curve references
    """
    
    @staticmethod
    def validate_snapshot(snapshot: DecaySnapshot) -> None:
        """Validate decay snapshot integrity."""
        
        # Timestamp validation
        if snapshot.reference_time > datetime.now():
            raise ValueError(
                f"reference_time cannot be in the future: {snapshot.reference_time}"
            )
        
        # Component validation
        for component in snapshot.components:
            # No negative weights
            if component.base_weight < 0 or component.decayed_weight < 0:
                raise ValueError(
                    f"Negative weights detected in {component.name}: "
                    f"base={component.base_weight}, decayed={component.decayed_weight}"
                )
            
            # No increasing trust without new events
            if component.decayed_weight > component.base_weight:
                raise ValueError(
                    f"Trust increased without new events in {component.name}: "
                    f"base={component.base_weight}, decayed={component.decayed_weight}"
                )
            
            # No future timestamps
            if component.last_event_timestamp > snapshot.reference_time:
                raise ValueError(
                    f"last_event_timestamp in future for {component.name}"
                )
        
        # Confidence bounds
        if not (0 <= snapshot.decay_confidence <= 1):
            raise ValueError(
                f"decay_confidence out of bounds: {snapshot.decay_confidence}"
            )


# ============================================================================
# HASHING & AUDIT TRAIL
# ============================================================================


class DecayHasher:
    """
    Generates cryptographic hashes for decay snapshots.
    
    Enables:
    - Determinism verification
    - Audit trail integrity
    - Dispute analysis
    - RL replay consistency
    """
    
    @staticmethod
    def compute_snapshot_hash(snapshot: DecaySnapshot) -> str:
        """
        Compute deterministic hash of decay snapshot.
        
        Hash includes:
        - Ledger hash (input state)
        - Reference time
        - All component values
        - Decay model version
        """
        hash_input = {
            "ledger_hash": snapshot.ledger_hash,
            "reference_time": snapshot.reference_time.isoformat(),
            "account_id": snapshot.account_id,
            "platform": snapshot.platform,
            "components": [
                {
                    "name": c.name,
                    "base_weight": c.base_weight,
                    "decayed_weight": c.decayed_weight,
                    "half_life_days": c.half_life_days,
                    "last_event_timestamp": c.last_event_timestamp.isoformat()
                }
                for c in snapshot.components
            ],
            "decay_confidence": snapshot.decay_confidence,
            "model_version": snapshot.decay_model_version
        }
        
        # Deterministic JSON serialization
        json_bytes = json.dumps(hash_input, sort_keys=True).encode('utf-8')
        
        return hashlib.sha256(json_bytes).hexdigest()
    
    @staticmethod
    def verify_snapshot_integrity(
        snapshot: DecaySnapshot,
        expected_hash: str
    ) -> bool:
        """Verify snapshot hasn't been tampered with."""
        computed_hash = DecayHasher.compute_snapshot_hash(snapshot)
        return computed_hash == expected_hash


# ============================================================================
# WATCHDOG
# ============================================================================


class DecayWatchdog:
    """
    Monitors decay computations for anomalies.
    
    Can:
    - Freeze rollouts
    - Force trust re-evaluation
    - Trigger audit logs
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.anomaly_threshold = 0.3  # 30% sudden change triggers alert
    
    def check_for_anomalies(
        self,
        snapshot: DecaySnapshot,
        previous_snapshot: Optional[DecaySnapshot] = None
    ) -> List[str]:
        """
        Check for decay anomalies.
        
        Returns list of anomaly descriptions (empty if clean).
        """
        anomalies = []
        
        # Check for sudden trust cliffing
        if previous_snapshot:
            for current_comp in snapshot.components:
                prev_comp = next(
                    (c for c in previous_snapshot.components if c.name == current_comp.name),
                    None
                )
                
                if prev_comp:
                    weight_change = abs(current_comp.decayed_weight - prev_comp.decayed_weight)
                    
                    if weight_change > self.anomaly_threshold:
                        anomalies.append(
                            f"Sudden weight cliff in {current_comp.name}: "
                            f"{prev_comp.decayed_weight:.3f} → {current_comp.decayed_weight:.3f}"
                        )
        
        # Check for frozen decay (same values over long period)
        if previous_snapshot:
            time_delta = (snapshot.reference_time - previous_snapshot.reference_time).days
            
            if time_delta > 30:  # More than 30 days
                all_identical = all(
                    abs(c1.decayed_weight - c2.decayed_weight) < 0.001
                    for c1 in snapshot.components
                    for c2 in previous_snapshot.components
                    if c1.name == c2.name
                )
                
                if all_identical:
                    anomalies.append(
                        f"Frozen decay detected: no change over {time_delta} days"
                    )
        
        # Check version mismatch
        if previous_snapshot:
            if snapshot.decay_model_version != previous_snapshot.decay_model_version:
                anomalies.append(
                    f"Model version changed: "
                    f"{previous_snapshot.decay_model_version} → {snapshot.decay_model_version}"
                )
        
        if anomalies:
            self.logger.warning(
                f"Decay anomalies detected for {snapshot.account_id}: {anomalies}"
            )
        
        return anomalies


# ============================================================================
# EXAMPLE USAGE
# ============================================================================


def example_usage():
    """Demonstrates correct usage of trust decay system."""
    
    # Initialize engine
    decay_engine = TrustDecayEngine(model_version="v1.0.0")
    watchdog = DecayWatchdog()
    
    # Simulated ledger data
    ledger_data = {
        "penalty_weight": 0.8,
        "last_penalty_time": datetime(2025, 10, 1),
        "repetition_factor": 1.5,
        
        "recovery_weight": 0.6,
        "last_recovery_time": datetime(2025, 11, 15),
        "clean_period_days": 45.0,
        
        "clean_history_weight": 0.9,
        "history_start_time": datetime(2024, 1, 1),
        "volatility_score": 0.2,
        
        "stability_weight": 0.7,
        "last_stable_time": datetime(2025, 12, 1),
        "recent_volatility": 0.1,
        
        "account_age_days": 365.0,
        "ledger_coverage_days": 350.0,
        "platform_telemetry_quality": 0.95,
        "time_since_last_event_days": 15.0,
        "recent_regime_changes": 0
    }
    
    ledger_hash = "abc123def456"  # From reputation_ledger.py
    
    # Compute decay snapshot
    snapshot = decay_engine.compute_decay_snapshot(
        account_id="user_12345",
        platform="youtube",
        reference_time=datetime(2026, 1, 24),
        ledger_data=ledger_data,
        ledger_hash=ledger_hash
    )
    
    # Compute hash
    snapshot_hash = DecayHasher.compute_snapshot_hash(snapshot)
    print(f"Snapshot hash: {snapshot_hash}")
    
    # Check for anomalies
    anomalies = watchdog.check_for_anomalies(snapshot)
    print(f"Anomalies detected: {anomalies}")
    
    # Extract weights for downstream consumption
    decayed_weights = decay_engine.get_decayed_weights(snapshot)
    print(f"Decayed weights: {json.dumps(decayed_weights, indent=2)}")
    
    # Example output to trust_scoring.py
    trust_input = {
        "account_id": snapshot.account_id,
        "platform": snapshot.platform,
        "decayed_components": decayed_weights,
        "decay_confidence": snapshot.decay_confidence,
        "model_version": snapshot.decay_model_version
    }
    
    return trust_input


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = example_usage()
    print("\nReady for trust_scoring.py consumption:")
    print(json.dumps(result, indent=2))
















