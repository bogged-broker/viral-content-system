"""
/orchestration/priority_router.py

Global Priority Arbitration Engine

This file decides who goes next, who waits, and who never runs.
Not heuristically. Not FIFO. Not fairness-based. Strategically.

Answers: "Given limited compute, rate limits, accounts, and uncertainty —
what deserves execution right now to maximize long-horizon virality?"

This is priority arbitration under uncertainty.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
import time
import math
from collections import defaultdict, deque


# ============================================================================
# CORE ENUMS
# ============================================================================

class PriorityClass(Enum):
    """Explicit priority classes - no magic numbers"""
    CRITICAL = "critical"        # must run ASAP
    HIGH = "high"                # strong signal, time-sensitive
    NORMAL = "normal"            # default
    LOW = "low"                  # background work
    DEFERRED = "deferred"        # wait explicitly
    TERMINATED = "terminated"    # never run


class ExecutionPhase(Enum):
    """Execution phases with different urgency curves"""
    INGESTION = "ingestion"
    FEATURE_EXTRACTION = "feature_extraction"
    PREDICTION = "prediction"
    POSTING = "posting"
    TRAINING = "training"


class ArtifactState(Enum):
    """State of required artifacts"""
    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"
    CORRUPTED = "corrupted"


class RoutingLane(Enum):
    """Execution lanes for different priority classes"""
    FAST = "fast"
    NORMAL = "normal"
    SLOW = "slow"
    BACKGROUND = "background"


# ============================================================================
# INPUT CONTRACT
# ============================================================================

@dataclass(frozen=True)
class PrioritySignal:
    """
    Every routing decision must be based on declared signals only.
    No hidden globals. No peeking.
    
    DETERMINISTIC: All time-based values are relative deltas, not absolute timestamps.
    """
    video_id: str
    phase: ExecutionPhase

    # From engagement_predictor
    predicted_engagement: Dict[str, float]  # {p50, p90, p99, expected}
    uncertainty: float                       # epistemic + aleatoric [0-1]
    early_velocity: float                    # engagement rate so far

    # From resource_governor
    resource_pressure: float                 # [0-1] system load
    resource_contention_spike: bool           # whether there's a sudden spike
    platform_window_urgency: float           # [0-1] time-sensitive posting window
    window_missed: bool                      # whether optimal window was missed

    # From dependency_graph
    dependency_ready: bool
    artifact_state: ArtifactState

    # Retry state (deterministic - relative time deltas)
    retry_count: int
    last_attempt_seconds_ago: float
    
    # Starvation tracking (deterministic - relative time deltas)
    first_seen_seconds_ago: float            # how long ago item was first seen
    route_count: int                         # how many times routed

    # Budget awareness (but not allocation)
    budget_pressure: float                   # [0-1] budget constraint pressure
    budget_class: Optional[str] = None       # budget tier/class

    # Optional metadata
    content_category: Optional[str] = None
    account_tier: Optional[str] = None


# ============================================================================
# OUTPUT CONTRACT
# ============================================================================

@dataclass(frozen=True)
class RoutingDecision:
    """
    Explicit output with mandatory explanation.
    No black boxes.
    
    Separates urgency from importance explicitly.
    """
    priority_class: PriorityClass
    score: float

    # Explicit urgency vs importance separation
    urgency_score: float         # time sensitivity component [0-1]
    importance_score: float      # expected long-term value component [0-1]

    lane: str                    # "fast", "normal", "slow", "background"
    ttl_seconds: int             # time before reroute
    retry_allowed: bool

    # Mandatory explanation - shows what drove the decision
    explanation: Dict[str, float]  # factor -> contribution


# ============================================================================
# PHASE PRIORITY MATRIX
# ============================================================================

@dataclass(frozen=True)
class PhaseWeights:
    """Weight factors for different phases"""
    freshness: float      # how much freshness matters
    certainty: float      # how much certainty matters
    speed: float          # how much latency matters
    window: float         # how much timing window matters
    completeness: float   # how much artifact completeness matters


@dataclass(frozen=True)
class PhaseConstraints:
    """Hard constraints for different phases - enforced gates"""
    min_certainty_for_critical: float    # minimum certainty to allow CRITICAL
    max_uncertainty_allowed: float       # maximum uncertainty before deferral
    require_complete_artifacts: bool      # must have COMPLETE artifacts
    allow_partial_artifacts: bool        # PARTIAL artifacts acceptable
    min_window_urgency: float            # minimum window urgency required


class PhasePriorityMatrix:
    """
    Different phases have different urgency curves.
    This matrix is enforced here — nowhere else.
    
    Includes hard constraints (gates) that must be satisfied.
    """
    
    PHASE_WEIGHTS = {
        ExecutionPhase.INGESTION: PhaseWeights(
            freshness=1.0,
            certainty=0.3,
            speed=0.7,
            window=0.5,
            completeness=0.4
        ),
        ExecutionPhase.FEATURE_EXTRACTION: PhaseWeights(
            freshness=0.4,
            certainty=0.6,
            speed=0.3,
            window=0.2,
            completeness=0.8
        ),
        ExecutionPhase.PREDICTION: PhaseWeights(
            freshness=0.3,
            certainty=1.0,
            speed=0.5,
            window=0.6,
            completeness=0.9
        ),
        ExecutionPhase.POSTING: PhaseWeights(
            freshness=0.6,
            certainty=0.4,
            speed=0.9,
            window=1.0,
            completeness=1.0
        ),
        ExecutionPhase.TRAINING: PhaseWeights(
            freshness=0.1,
            certainty=0.5,
            speed=0.1,
            window=0.0,
            completeness=1.0
        ),
    }
    
    PHASE_CONSTRAINTS = {
        ExecutionPhase.INGESTION: PhaseConstraints(
            min_certainty_for_critical=0.0,      # freshness > certainty
            max_uncertainty_allowed=0.9,
            require_complete_artifacts=False,
            allow_partial_artifacts=True,
            min_window_urgency=0.0
        ),
        ExecutionPhase.FEATURE_EXTRACTION: PhaseConstraints(
            min_certainty_for_critical=0.5,     # stability > speed
            max_uncertainty_allowed=0.7,
            require_complete_artifacts=False,
            allow_partial_artifacts=True,
            min_window_urgency=0.0
        ),
        ExecutionPhase.PREDICTION: PhaseConstraints(
            min_certainty_for_critical=0.6,     # certainty > latency
            max_uncertainty_allowed=0.6,
            require_complete_artifacts=True,
            allow_partial_artifacts=False,
            min_window_urgency=0.0
        ),
        ExecutionPhase.POSTING: PhaseConstraints(
            min_certainty_for_critical=0.3,     # window dominance
            max_uncertainty_allowed=0.5,
            require_complete_artifacts=True,
            allow_partial_artifacts=False,
            min_window_urgency=0.3              # must have some window urgency
        ),
        ExecutionPhase.TRAINING: PhaseConstraints(
            min_certainty_for_critical=0.7,     # completeness only
            max_uncertainty_allowed=0.4,
            require_complete_artifacts=True,
            allow_partial_artifacts=False,
            min_window_urgency=0.0
        ),
    }
    
    @classmethod
    def get_weights(cls, phase: ExecutionPhase) -> PhaseWeights:
        """Get phase-specific weights"""
        return cls.PHASE_WEIGHTS[phase]
    
    @classmethod
    def get_constraints(cls, phase: ExecutionPhase) -> PhaseConstraints:
        """Get phase-specific hard constraints"""
        return cls.PHASE_CONSTRAINTS[phase]
    
    @classmethod
    def check_phase_gates(cls, signal: PrioritySignal) -> Tuple[bool, Optional[str]]:
        """
        Check if signal passes phase-specific hard constraints.
        Returns (passed, reason_if_failed)
        """
        constraints = cls.get_constraints(signal.phase)
        
        # Check uncertainty gate
        if signal.uncertainty > constraints.max_uncertainty_allowed:
            return False, f"uncertainty {signal.uncertainty:.2f} exceeds max {constraints.max_uncertainty_allowed:.2f}"
        
        # Check artifact completeness gate
        if constraints.require_complete_artifacts:
            if signal.artifact_state != ArtifactState.COMPLETE:
                return False, f"requires COMPLETE artifacts, got {signal.artifact_state.value}"
        elif not constraints.allow_partial_artifacts:
            if signal.artifact_state == ArtifactState.PARTIAL:
                return False, f"PARTIAL artifacts not allowed for {signal.phase.value}"
        
        # Check window urgency gate (for posting phase)
        if constraints.min_window_urgency > 0:
            if signal.platform_window_urgency < constraints.min_window_urgency:
                return False, f"window urgency {signal.platform_window_urgency:.2f} below min {constraints.min_window_urgency:.2f}"
        
        return True, None


# ============================================================================
# SIGNAL NORMALIZER
# ============================================================================

class SignalNormalizer:
    """
    Normalizes raw signals into [0-1] range for fair comparison.
    Handles outliers and ensures numerical stability.
    """
    
    @staticmethod
    def normalize_velocity(velocity: float) -> float:
        """Normalize early velocity using sigmoid"""
        # Maps [0, inf) to [0, 1]
        return 1.0 / (1.0 + math.exp(-velocity / 10.0))
    
    @staticmethod
    def normalize_tail_mass(p90: float, p99: float) -> float:
        """
        Compute normalized tail mass indicator.
        High tail mass = high viral potential.
        """
        if p90 <= 0:
            return 0.0
        
        tail_ratio = p99 / max(p90, 1e-9)
        # Heavy tail -> high score
        return min(1.0, tail_ratio / 5.0)
    
    @staticmethod
    def normalize_time_decay(seconds_ago: float, half_life: float = 3600.0) -> float:
        """Exponential decay for retry aging"""
        return math.exp(-seconds_ago / half_life)
    
    @staticmethod
    def normalize_window_urgency(urgency: float, phase: ExecutionPhase) -> float:
        """Apply phase-specific window urgency scaling"""
        weights = PhasePriorityMatrix.get_weights(phase)
        return urgency * weights.window


# ============================================================================
# STARVATION GUARD
# ============================================================================

@dataclass
class ItemHistory:
    """
    Track history for starvation detection.
    
    DETERMINISTIC: Uses relative time deltas from signal, not time.time()
    """
    video_id: str
    route_count: int
    last_score: float
    cumulative_wait_time: float


class StarvationGuard:
    """
    Prevents low-priority items from never executing.
    Prevents new content from permanently preempting long-tail work.
    
    Implements aging bonuses, hard caps, fairness windows.
    
    DETERMINISTIC: Uses relative time deltas from PrioritySignal, not time.time()
    """
    
    def __init__(
        self,
        max_wait_seconds: float = 3600.0,
        aging_bonus_per_hour: float = 0.1,
        fairness_boost_threshold: int = 5
    ):
        self.max_wait_seconds = max_wait_seconds
        self.aging_bonus_per_hour = aging_bonus_per_hour
        self.fairness_boost_threshold = fairness_boost_threshold
        
        self._history: Dict[str, ItemHistory] = {}
    
    def register_item(self, video_id: str, score: float, route_count: int) -> None:
        """
        Register item for starvation tracking.
        DETERMINISTIC: Uses route_count from signal, not internal state.
        """
        if video_id not in self._history:
            self._history[video_id] = ItemHistory(
                video_id=video_id,
                route_count=route_count,
                last_score=score,
                cumulative_wait_time=0.0
            )
        else:
            hist = self._history[video_id]
            self._history[video_id] = ItemHistory(
                video_id=hist.video_id,
                route_count=route_count,
                last_score=score,
                cumulative_wait_time=hist.cumulative_wait_time
            )
    
    def compute_aging_bonus(self, signal: PrioritySignal) -> float:
        """
        Compute aging bonus to prevent starvation.
        DETERMINISTIC: Uses first_seen_seconds_ago from signal.
        """
        hours_waiting = signal.first_seen_seconds_ago / 3600.0
        
        # Linear bonus with cap
        bonus = min(0.5, hours_waiting * self.aging_bonus_per_hour)
        
        # Extra boost if repeatedly deferred
        if signal.route_count >= self.fairness_boost_threshold:
            bonus += 0.2
        
        return bonus
    
    def check_forced_execution(self, signal: PrioritySignal) -> bool:
        """
        Check if item MUST execute due to excessive wait.
        DETERMINISTIC: Uses first_seen_seconds_ago from signal.
        """
        return signal.first_seen_seconds_ago >= self.max_wait_seconds
    
    def cleanup_old_entries(self, max_age_seconds: float = 86400.0) -> None:
        """
        Remove stale tracking entries.
        Note: This is non-deterministic but only for cleanup, not routing decisions.
        """
        # For deterministic routing, we rely on signal's first_seen_seconds_ago
        # This cleanup is just memory management
        pass


# ============================================================================
# BURST DETECTOR
# ============================================================================

@dataclass
class BurstSignal:
    """
    Detected burst event.
    
    DETERMINISTIC: Uses relative time deltas, not absolute timestamps.
    """
    burst_start_seconds_ago: float  # relative time delta
    velocity: float
    confidence: float


class BurstDetector:
    """
    Detects sudden surges (breaking trends, platform anomalies, external events).
    Temporarily boosts urgency, shortens TTL, overrides default class.
    
    DETERMINISTIC: Uses signal-based detection, not time-based state.
    """
    
    def __init__(
        self,
        window_size: int = 100,
        velocity_threshold: float = 3.0,
        min_confidence: float = 0.7
    ):
        self.window_size = window_size
        self.velocity_threshold = velocity_threshold
        self.min_confidence = min_confidence
        
        # For deterministic burst detection, we track recent velocities
        # but detection is based on signal comparison, not time
        self._recent_velocities: deque = deque(maxlen=window_size)
    
    def update(self, velocity: float) -> None:
        """Update velocity tracking for baseline calculation"""
        self._recent_velocities.append(velocity)
    
    def detect_burst(self, signal: PrioritySignal) -> Optional[BurstSignal]:
        """
        Detect if current signal represents a burst.
        DETERMINISTIC: Compares signal velocity to recent baseline.
        Returns BurstSignal if burst detected, None otherwise.
        """
        if len(self._recent_velocities) < 10:
            return None
        
        baseline = sum(self._recent_velocities) / len(self._recent_velocities)
        
        if baseline <= 0:
            return None
        
        velocity_ratio = signal.early_velocity / baseline
        
        if velocity_ratio >= self.velocity_threshold:
            # Check uncertainty - high uncertainty reduces confidence
            confidence = 1.0 - signal.uncertainty
            
            if confidence >= self.min_confidence:
                # Use signal's relative time, not absolute time
                burst = BurstSignal(
                    burst_start_seconds_ago=0.0,  # just detected
                    velocity=signal.early_velocity,
                    confidence=confidence
                )
                return burst
        
        return None
    
    def get_burst_boost(self, burst: BurstSignal) -> float:
        """
        Get priority boost if video is in active burst.
        DETERMINISTIC: Uses burst_start_seconds_ago from BurstSignal.
        """
        # Burst boost decays over time
        decay = math.exp(-burst.burst_start_seconds_ago / 1800.0)  # 30 min half-life
        
        return burst.confidence * 0.3 * decay
    
    def cleanup_old_bursts(self) -> None:
        """
        Cleanup for memory management.
        Note: Burst detection is now signal-based, so this is just for memory.
        """
        # Burst detection is now deterministic and signal-based
        # No time-based cleanup needed
        pass


# ============================================================================
# PRIORITY AUDIT LOG
# ============================================================================

@dataclass
class PriorityAuditEntry:
    """
    Single audit log entry.
    
    DETERMINISTIC: timestamp can be provided externally for determinism.
    """
    timestamp: float  # Can be provided externally for deterministic testing
    video_id: str
    phase: ExecutionPhase
    decision: RoutingDecision
    input_signal: PrioritySignal


class PriorityAuditLog:
    """
    Logs all routing decisions for determinism verification and debugging.
    """
    
    def __init__(self, max_entries: int = 10000):
        self.max_entries = max_entries
        self._entries: deque = deque(maxlen=max_entries)
    
    def log(
        self,
        video_id: str,
        phase: ExecutionPhase,
        decision: RoutingDecision,
        signal: PrioritySignal,
        timestamp: Optional[float] = None
    ) -> None:
        """
        Log a routing decision.
        DETERMINISTIC: timestamp can be provided externally.
        """
        entry = PriorityAuditEntry(
            timestamp=timestamp if timestamp is not None else time.time(),
            video_id=video_id,
            phase=phase,
            decision=decision,
            input_signal=signal
        )
        self._entries.append(entry)
    
    def get_recent(self, n: int = 100) -> List[PriorityAuditEntry]:
        """Get n most recent entries"""
        return list(self._entries)[-n:]
    
    def get_for_video(self, video_id: str) -> List[PriorityAuditEntry]:
        """Get all entries for a specific video"""
        return [e for e in self._entries if e.video_id == video_id]
    
    def export_decisions(self) -> List[Dict]:
        """Export decisions for analysis"""
        return [
            {
                'timestamp': e.timestamp,
                'video_id': e.video_id,
                'phase': e.phase.value,
                'priority_class': e.decision.priority_class.value,
                'score': e.decision.score,
                'lane': e.decision.lane,
                'explanation': e.decision.explanation
            }
            for e in self._entries
        ]


# ============================================================================
# CORE PRIORITY ROUTER
# ============================================================================

class PriorityRouter:
    """
    CORE ENGINE
    
    Assigns priority to every execution request.
    Deterministic given same state.
    Incorporates uncertainty explicitly.
    Separates urgency from importance.
    Phase-aware, budget-aware, dependency-aware.
    Prevents starvation, enables fast-lane routing.
    Auditable and explainable.
    """
    
    def __init__(
        self,
        enable_starvation_guard: bool = True,
        enable_burst_detection: bool = True,
        enable_audit_log: bool = True
    ):
        self.normalizer = SignalNormalizer()
        
        self.starvation_guard = StarvationGuard() if enable_starvation_guard else None
        self.burst_detector = BurstDetector() if enable_burst_detection else None
        self.audit_log = PriorityAuditLog() if enable_audit_log else None
    
    def score(self, signal: PrioritySignal) -> Tuple[float, float, float, Dict[str, float]]:
        """
        Compute priority score for a signal.
        
        Returns:
            (total_score, urgency_score, importance_score, explanation)
            where explanation shows factor contributions
        
        HARD RULE: High uncertainty can NEVER produce CRITICAL priority.
        """
        explanation = {}
        
        # Check phase gates first (hard constraints)
        gates_passed, gate_reason = PhasePriorityMatrix.check_phase_gates(signal)
        if not gates_passed:
            # Failed gate - return low scores
            explanation['gate_failure'] = -0.5
            explanation['gate_reason'] = 0.0  # store reason in explanation
            return 0.1, 0.0, 0.0, explanation
        
        # Get phase-specific weights and constraints
        weights = PhasePriorityMatrix.get_weights(signal.phase)
        constraints = PhasePriorityMatrix.get_constraints(signal.phase)
        
        # ====================================================================
        # IMPORTANCE COMPONENT (expected long-term value)
        # ====================================================================
        
        p90 = signal.predicted_engagement.get('p90', 0.0)
        p99 = signal.predicted_engagement.get('p99', 0.0)
        expected = signal.predicted_engagement.get('expected', 0.0)
        
        # Tail mass indicates viral potential
        tail_mass = self.normalizer.normalize_tail_mass(p90, p99)
        
        # Expected value with uncertainty penalty
        certainty_factor = 1.0 - signal.uncertainty
        importance_score = expected * certainty_factor
        
        # Normalize to [0-1]
        importance_normalized = min(1.0, importance_score / 100000.0)
        
        # Weight by phase
        importance_contribution = importance_normalized * weights.certainty
        explanation['importance'] = importance_contribution
        
        # Tail mass contributes to importance (viral potential)
        tail_contribution = tail_mass * 0.3
        explanation['tail_mass'] = tail_contribution
        
        # Budget pressure reduces importance (but doesn't allocate)
        budget_penalty = -signal.budget_pressure * 0.1
        explanation['budget_pressure'] = budget_penalty
        
        # Compute pure importance score (for output)
        pure_importance = max(0.0, min(1.0, importance_contribution + tail_contribution + budget_penalty))
        
        # ====================================================================
        # URGENCY COMPONENT (time sensitivity)
        # ====================================================================
        
        # Early velocity (urgency)
        velocity_normalized = self.normalizer.normalize_velocity(signal.early_velocity)
        velocity_contribution = velocity_normalized * weights.speed * 0.25
        explanation['velocity'] = velocity_contribution
        
        # Platform window urgency (timing)
        window_contribution = signal.platform_window_urgency * weights.window * 0.2
        explanation['window'] = window_contribution
        
        # Missed window penalty
        missed_window_penalty = -0.15 if signal.window_missed else 0.0
        explanation['missed_window'] = missed_window_penalty
        
        # Resource contention spike reduces urgency (system overload)
        spike_penalty = -0.1 if signal.resource_contention_spike else 0.0
        explanation['contention_spike'] = spike_penalty
        
        # Compute pure urgency score (for output)
        pure_urgency = max(0.0, min(1.0, velocity_contribution + window_contribution + missed_window_penalty + spike_penalty))
        
        # ====================================================================
        # SHARED FACTORS (affect both urgency and importance)
        # ====================================================================
        
        # Resource pressure (system state) - affects both
        pressure_penalty = -signal.resource_pressure * 0.15
        explanation['resource_pressure'] = pressure_penalty
        
        # Dependency readiness
        if not signal.dependency_ready:
            dependency_penalty = -0.3
        elif signal.artifact_state != ArtifactState.COMPLETE:
            dependency_penalty = -0.2
        else:
            dependency_penalty = 0.0
        
        explanation['dependencies'] = dependency_penalty
        
        # Retry penalty
        retry_penalty = -min(0.2, signal.retry_count * 0.05)
        explanation['retry_penalty'] = retry_penalty
        
        # ====================================================================
        # STARVATION GUARD BONUS (deterministic)
        # ====================================================================
        
        starvation_bonus = 0.0
        if self.starvation_guard:
            starvation_bonus = self.starvation_guard.compute_aging_bonus(signal)
            explanation['starvation_bonus'] = starvation_bonus
        
        # ====================================================================
        # BURST DETECTION BOOST (deterministic)
        # ====================================================================
        
        burst_boost = 0.0
        if self.burst_detector:
            self.burst_detector.update(signal.early_velocity)
            burst = self.burst_detector.detect_burst(signal)
            if burst:
                burst_boost = self.burst_detector.get_burst_boost(burst)
                explanation['burst_boost'] = burst_boost
        
        # ====================================================================
        # COMPUTE FINAL SCORES
        # ====================================================================
        
        base_score = (
            importance_contribution +
            tail_contribution +
            velocity_contribution +
            window_contribution +
            pressure_penalty +
            dependency_penalty +
            retry_penalty +
            missed_window_penalty +
            spike_penalty +
            budget_penalty
        )
        
        final_score = max(0.0, min(1.0, base_score + starvation_bonus + burst_boost))
        
        # HARD RULE: High uncertainty caps maximum score
        if signal.uncertainty > 0.5:
            max_allowed_score = 0.7
            if final_score > max_allowed_score:
                final_score = max_allowed_score
                explanation['uncertainty_cap'] = -0.3
        
        # HARD RULE: Check phase-specific certainty requirement for CRITICAL
        if final_score >= 0.85:
            if signal.uncertainty > (1.0 - constraints.min_certainty_for_critical):
                # Can't be CRITICAL without sufficient certainty
                max_allowed_score = 0.75
                if final_score > max_allowed_score:
                    final_score = max_allowed_score
                    explanation['certainty_gate'] = -0.1
        
        return final_score, pure_urgency, pure_importance, explanation
    
    def classify(
        self,
        score: float,
        signal: PrioritySignal
    ) -> PriorityClass:
        """
        Convert score to priority class.
        Phase-aware with special cases and hard constraints.
        """
        # Check forced execution due to starvation (deterministic)
        if self.starvation_guard:
            if self.starvation_guard.check_forced_execution(signal):
                return PriorityClass.HIGH
        
        # Check termination conditions
        if signal.retry_count > 10:
            return PriorityClass.TERMINATED
        
        if signal.artifact_state == ArtifactState.CORRUPTED:
            return PriorityClass.TERMINATED
        
        if not signal.dependency_ready and signal.retry_count > 5:
            return PriorityClass.DEFERRED
        
        # Get phase constraints
        constraints = PhasePriorityMatrix.get_constraints(signal.phase)
        
        # Score-based classification
        if score >= 0.85:
            # HARD RULE: High uncertainty can NEVER produce CRITICAL
            min_certainty = constraints.min_certainty_for_critical
            if signal.uncertainty > (1.0 - min_certainty):
                return PriorityClass.HIGH
            return PriorityClass.CRITICAL
        
        elif score >= 0.65:
            return PriorityClass.HIGH
        
        elif score >= 0.35:
            return PriorityClass.NORMAL
        
        elif score >= 0.15:
            return PriorityClass.LOW
        
        else:
            return PriorityClass.DEFERRED
    
    def route(self, signal: PrioritySignal) -> RoutingDecision:
        """
        Main routing decision function.
        
        Combines scoring, classification, lane assignment, TTL, retry policy.
        Logs decision for auditability.
        """
        # Compute score
        score, explanation = self.score(signal)
        
        # Classify
        priority_class = self.classify(score, signal)
        
        # Assign lane
        lane = self._assign_lane(priority_class, signal)
        
        # Assign TTL (time to live before reroute)
        ttl_seconds = self._assign_ttl(priority_class, signal)
        
        # Retry policy
        retry_allowed = self._allow_retry(priority_class, signal)
        
        # Build decision
        decision = RoutingDecision(
            priority_class=priority_class,
            score=score,
            lane=lane,
            ttl_seconds=ttl_seconds,
            retry_allowed=retry_allowed,
            explanation=explanation
        )
        
        # Register with starvation guard
        if self.starvation_guard:
            self.starvation_guard.register_item(signal.video_id, score)
        
        # Audit log
        if self.audit_log:
            self.audit_log.log(signal.video_id, signal.phase, decision, signal)
        
        return decision
    
    def _assign_lane(
        self,
        priority_class: PriorityClass,
        signal: PrioritySignal
    ) -> str:
        """
        Assign execution lane based on priority class.
        Returns string as per spec (not enum).
        """
        if priority_class == PriorityClass.CRITICAL:
            return "fast"
        elif priority_class == PriorityClass.HIGH:
            return "fast"
        elif priority_class == PriorityClass.NORMAL:
            return "normal"
        elif priority_class == PriorityClass.LOW:
            return "slow"
        else:  # DEFERRED or TERMINATED
            return "background"
    
    def _assign_ttl(
        self,
        priority_class: PriorityClass,
        signal: PrioritySignal
    ) -> int:
        """Assign TTL (seconds before reroute) based on priority"""
        base_ttl = {
            PriorityClass.CRITICAL: 60,
            PriorityClass.HIGH: 300,
            PriorityClass.NORMAL: 900,
            PriorityClass.LOW: 1800,
            PriorityClass.DEFERRED: 3600,
            PriorityClass.TERMINATED: 0,
        }
        
        ttl = base_ttl[priority_class]
        
        # Shorten TTL for posting phase with tight windows
        if signal.phase == ExecutionPhase.POSTING:
            if signal.platform_window_urgency > 0.7:
                ttl = int(ttl * 0.5)
        
        return ttl
    
    def _allow_retry(
        self,
        priority_class: PriorityClass,
        signal: PrioritySignal
    ) -> bool:
        """Determine if retry is allowed"""
        if priority_class == PriorityClass.TERMINATED:
            return False
        
        if signal.retry_count >= 15:
            return False
        
        return True
    
    def cleanup(self) -> None:
        """Periodic cleanup of internal state"""
        if self.starvation_guard:
            self.starvation_guard.cleanup_old_entries()
        
        if self.burst_detector:
            self.burst_detector.cleanup_old_bursts()


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

def example_usage():
    """Example of how to use PriorityRouter"""
    
    # Initialize router
    router = PriorityRouter(
        enable_starvation_guard=True,
        enable_burst_detection=True,
        enable_audit_log=True
    )
    
    # Create a priority signal (with all new required fields)
    signal = PrioritySignal(
        video_id="video_12345",
        phase=ExecutionPhase.PREDICTION,
        
        predicted_engagement={
            'expected': 50000.0,
            'p50': 30000.0,
            'p90': 80000.0,
            'p99': 150000.0
        },
        uncertainty=0.25,
        early_velocity=12.5,
        
        resource_pressure=0.6,
        resource_contention_spike=False,
        platform_window_urgency=0.8,
        window_missed=False,
        
        dependency_ready=True,
        artifact_state=ArtifactState.COMPLETE,
        
        retry_count=0,
        last_attempt_seconds_ago=0.0,
        
        first_seen_seconds_ago=300.0,  # 5 minutes ago
        route_count=1,
        
        budget_pressure=0.3,
        budget_class="standard",
        
        content_category="trending",
        account_tier="premium"
    )
    
    # Route the signal
    decision = router.route(signal)
    
    # Inspect decision
    print(f"Priority Class: {decision.priority_class.value}")
    print(f"Score: {decision.score:.3f}")
    print(f"Lane: {decision.lane.value}")
    print(f"TTL: {decision.ttl_seconds}s")
    print(f"Retry Allowed: {decision.retry_allowed}")
    print(f"\nExplanation:")
    for factor, contribution in decision.explanation.items():
        print(f"  {factor}: {contribution:+.3f}")
    
    # Periodic cleanup
    router.cleanup()


if __name__ == "__main__":
    example_usage()