"""
/posting/monitoring/rollout_controller.py

Gradual Exposure & Controlled Scale Authority

Tier-0 component that controls the rate of exposure increase across platforms,
accounts, and content classes. Enforces that growth happens only as fast as
trust allows, and never faster than recovery permits.

Philosophy: Platforms punish slope, not volume.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Tuple
import time
import hashlib
import json


CONTROLLER_VERSION = "1.0.0"


# ============================================================================
# CORE ENUMS & STATE
# ============================================================================


class RolloutPhase(Enum):
    """
    Explicit, finite set of rollout phases.
    Transitions are explicit, never inferred.
    """
    COLD_START = "cold_start"           # New account/platform, extreme caution
    TRUST_BUILDING = "trust_building"   # Building reputation, slow increases
    STEADY_STATE = "steady_state"       # Normal operation, moderate growth
    ACCELERATION = "acceleration"       # High trust, faster growth allowed
    CONSTRAINED = "constrained"         # Under pressure, no increases
    ROLLBACK = "rollback"              # Active reduction in exposure


@dataclass(frozen=True)
class RolloutState:
    """
    Persisted state for rollout decisions.
    Append-only, never rewritten.
    """
    platform: str
    account_id: str
    
    current_exposure: float
    phase: RolloutPhase
    
    last_increase_at: float
    last_decrease_at: float
    
    cumulative_increases: int
    cumulative_rollbacks: int
    
    state_version: str
    
    def to_dict(self) -> Dict:
        return {
            'platform': self.platform,
            'account_id': self.account_id,
            'current_exposure': self.current_exposure,
            'phase': self.phase.value,
            'last_increase_at': self.last_increase_at,
            'last_decrease_at': self.last_decrease_at,
            'cumulative_increases': self.cumulative_increases,
            'cumulative_rollbacks': self.cumulative_rollbacks,
            'state_version': self.state_version
        }
    
    @staticmethod
    def from_dict(data: Dict) -> 'RolloutState':
        return RolloutState(
            platform=data['platform'],
            account_id=data['account_id'],
            current_exposure=data['current_exposure'],
            phase=RolloutPhase(data['phase']),
            last_increase_at=data['last_increase_at'],
            last_decrease_at=data['last_decrease_at'],
            cumulative_increases=data['cumulative_increases'],
            cumulative_rollbacks=data['cumulative_rollbacks'],
            state_version=data['state_version']
        )


@dataclass(frozen=True)
class RolloutDecision:
    """
    Output contract for all rollout evaluations.
    No booleans. No go/no-go. Only bounded deltas.
    """
    allowed_delta: float          # ≥ 0.0
    max_exposure_cap: float
    hold_reason: Optional[str]
    confidence: float             # ∈ [0.0, 1.0]
    evaluated_at: float
    controller_version: str
    
    def __post_init__(self):
        assert self.allowed_delta >= 0.0, "Delta cannot be negative"
        assert 0.0 <= self.confidence <= 1.0, "Confidence must be in [0, 1]"


@dataclass(frozen=True)
class ExposureBudget:
    """
    Defines how much change is allowed per unit time.
    Platform-specific and conservative by default.
    """
    max_delta_per_hour: float
    max_delta_per_day: float
    max_single_step_delta: float
    
    def __post_init__(self):
        assert self.max_delta_per_hour > 0, "Hourly budget must be positive"
        assert self.max_delta_per_day >= self.max_delta_per_hour, \
            "Daily budget must be >= hourly"
        assert self.max_single_step_delta <= self.max_delta_per_hour, \
            "Single step cannot exceed hourly budget"


# ============================================================================
# PLATFORM-SPECIFIC BUDGETS
# ============================================================================


DEFAULT_BUDGETS: Dict[str, ExposureBudget] = {
    'twitter': ExposureBudget(
        max_delta_per_hour=2.0,
        max_delta_per_day=10.0,
        max_single_step_delta=0.5
    ),
    'linkedin': ExposureBudget(
        max_delta_per_hour=1.0,
        max_delta_per_day=5.0,
        max_single_step_delta=0.25
    ),
    'reddit': ExposureBudget(
        max_delta_per_hour=1.5,
        max_delta_per_day=8.0,
        max_single_step_delta=0.3
    ),
    'default': ExposureBudget(
        max_delta_per_hour=1.0,
        max_delta_per_day=5.0,
        max_single_step_delta=0.2
    )
}


# ============================================================================
# ROLLOUT POLICY
# ============================================================================


@dataclass(frozen=True)
class RolloutPolicy:
    """
    Pure policy object that defines rollout constraints.
    Deterministic, versioned, centrally defined.
    No local overrides.
    """
    phase: RolloutPhase
    risk_multiplier: float        # Applied to budget
    cooldown_hours: float         # After rollback
    min_observation_hours: float  # Before acceleration
    
    policy_version: str = "1.0.0"
    
    def __post_init__(self):
        assert 0.0 <= self.risk_multiplier <= 1.0, \
            "Risk multiplier must be in [0, 1]"
        assert self.cooldown_hours >= 0, "Cooldown cannot be negative"


# Phase-specific policies
PHASE_POLICIES: Dict[RolloutPhase, RolloutPolicy] = {
    RolloutPhase.COLD_START: RolloutPolicy(
        phase=RolloutPhase.COLD_START,
        risk_multiplier=0.1,
        cooldown_hours=24.0,
        min_observation_hours=72.0
    ),
    RolloutPhase.TRUST_BUILDING: RolloutPolicy(
        phase=RolloutPhase.TRUST_BUILDING,
        risk_multiplier=0.3,
        cooldown_hours=12.0,
        min_observation_hours=48.0
    ),
    RolloutPhase.STEADY_STATE: RolloutPolicy(
        phase=RolloutPhase.STEADY_STATE,
        risk_multiplier=0.6,
        cooldown_hours=6.0,
        min_observation_hours=24.0
    ),
    RolloutPhase.ACCELERATION: RolloutPolicy(
        phase=RolloutPhase.ACCELERATION,
        risk_multiplier=0.9,
        cooldown_hours=3.0,
        min_observation_hours=12.0
    ),
    RolloutPhase.CONSTRAINED: RolloutPolicy(
        phase=RolloutPhase.CONSTRAINED,
        risk_multiplier=0.0,
        cooldown_hours=24.0,
        min_observation_hours=48.0
    ),
    RolloutPhase.ROLLBACK: RolloutPolicy(
        phase=RolloutPhase.ROLLBACK,
        risk_multiplier=0.0,
        cooldown_hours=48.0,
        min_observation_hours=96.0
    )
}


# ============================================================================
# ROLLOUT CALCULATOR
# ============================================================================


class RolloutCalculator:
    """
    Core engine that computes allowed exposure delta.
    
    Rules:
    - Risk ↑ ⇒ delta ↓
    - Fresh accounts ⇒ delta ↓
    - Recent rollback ⇒ cooldown enforced
    - Stable low risk ⇒ slow increase only
    - No jumps. No skips.
    """
    
    def __init__(self, budgets: Dict[str, ExposureBudget] = None):
        self.budgets = budgets or DEFAULT_BUDGETS
    
    def calculate_allowed_delta(
        self,
        state: RolloutState,
        risk_score: float,  # From risk_evaluator.py, ∈ [0, 1]
        current_time: float = None
    ) -> Tuple[float, Optional[str]]:
        """
        Calculate how much exposure can safely increase.
        
        Returns:
            (allowed_delta, hold_reason)
        """
        current_time = current_time or time.time()
        
        # Get budget for platform
        budget = self.budgets.get(state.platform, self.budgets['default'])
        policy = PHASE_POLICIES[state.phase]
        
        # CONSTRAINED or ROLLBACK: no increases allowed
        if state.phase in (RolloutPhase.CONSTRAINED, RolloutPhase.ROLLBACK):
            return 0.0, f"Phase {state.phase.value} prohibits increases"
        
        # Check cooldown after rollback
        if state.last_decrease_at > 0:
            hours_since_rollback = (current_time - state.last_decrease_at) / 3600
            if hours_since_rollback < policy.cooldown_hours:
                remaining = policy.cooldown_hours - hours_since_rollback
                return 0.0, f"Cooldown: {remaining:.1f}h remaining after rollback"
        
        # Check time-based budget enforcement
        hours_since_increase = (current_time - state.last_increase_at) / 3600 \
            if state.last_increase_at > 0 else 999999
        
        # Enforce minimum observation time for phase
        if state.phase == RolloutPhase.COLD_START:
            total_time = (current_time - state.last_increase_at) / 3600 \
                if state.last_increase_at > 0 else 0
            if total_time < policy.min_observation_hours:
                remaining = policy.min_observation_hours - total_time
                return 0.0, f"Cold start observation: {remaining:.1f}h remaining"
        
        # Calculate base delta from single step budget
        base_delta = budget.max_single_step_delta
        
        # Apply phase-specific risk multiplier
        base_delta *= policy.risk_multiplier
        
        # Apply risk score dampening (risk ↑ ⇒ delta ↓)
        # Risk score of 0 = no dampening, risk score of 1 = zero delta
        risk_dampening = max(0.0, 1.0 - risk_score)
        base_delta *= risk_dampening
        
        # Apply time-based throttling to prevent rapid-fire increases
        if hours_since_increase < 1.0:
            # Reduce delta for increases within the same hour
            time_factor = hours_since_increase
            base_delta *= time_factor
        
        # Fresh accounts get additional dampening
        if state.cumulative_increases < 5:
            freshness_factor = (state.cumulative_increases + 1) / 6.0
            base_delta *= freshness_factor
        
        # Accounts with recent rollbacks get dampening
        if state.cumulative_rollbacks > 0:
            rollback_penalty = 0.8 ** state.cumulative_rollbacks
            base_delta *= rollback_penalty
        
        # Final sanity check against budget
        base_delta = min(base_delta, budget.max_single_step_delta)
        
        if base_delta < 0.01:  # Effectively zero
            return 0.0, "Calculated delta too small given risk and constraints"
        
        return base_delta, None
    
    def calculate_confidence(
        self,
        state: RolloutState,
        risk_score: float,
        current_time: float = None
    ) -> float:
        """
        Calculate confidence in the rollout decision.
        Higher confidence = more certainty about safety.
        """
        current_time = current_time or time.time()
        
        # Start with base confidence
        confidence = 0.5
        
        # Higher confidence in later phases
        phase_confidence = {
            RolloutPhase.COLD_START: 0.3,
            RolloutPhase.TRUST_BUILDING: 0.5,
            RolloutPhase.STEADY_STATE: 0.7,
            RolloutPhase.ACCELERATION: 0.8,
            RolloutPhase.CONSTRAINED: 0.6,
            RolloutPhase.ROLLBACK: 0.5
        }
        confidence = phase_confidence.get(state.phase, 0.5)
        
        # Lower confidence with higher risk
        confidence *= (1.0 - risk_score * 0.5)
        
        # Higher confidence with more history
        if state.cumulative_increases > 10:
            confidence = min(1.0, confidence * 1.2)
        
        # Lower confidence with recent rollbacks
        if state.cumulative_rollbacks > 0:
            confidence *= (0.8 ** state.cumulative_rollbacks)
        
        return max(0.0, min(1.0, confidence))


# ============================================================================
# ROLLOUT DECAY ENGINE
# ============================================================================


class RolloutDecayEngine:
    """
    Handles rollback behavior.
    
    - Gradual decay, not instant zero
    - Steeper decay when confidence is high
    - Shallow decay when confidence is low
    - Rollback is recoverable
    """
    
    def calculate_decay_delta(
        self,
        current_exposure: float,
        confidence: float,
        severity: float = 0.5  # ∈ [0, 1], how aggressive the rollback
    ) -> float:
        """
        Calculate how much to reduce exposure.
        Returns negative delta.
        """
        assert 0.0 <= confidence <= 1.0
        assert 0.0 <= severity <= 1.0
        
        if current_exposure <= 0:
            return 0.0
        
        # Base decay rate: percentage of current exposure
        base_decay_rate = 0.3  # 30% reduction
        
        # Steeper decay with high confidence
        confidence_factor = 0.5 + (confidence * 0.5)  # ∈ [0.5, 1.0]
        
        # Severity increases decay rate
        severity_factor = 1.0 + severity  # ∈ [1.0, 2.0]
        
        decay_amount = current_exposure * base_decay_rate * confidence_factor * severity_factor
        
        # Cap decay to not go below zero
        decay_amount = min(decay_amount, current_exposure)
        
        # Return as negative delta
        return -decay_amount
    
    def should_exit_rollback(
        self,
        state: RolloutState,
        risk_score: float,
        current_time: float = None
    ) -> bool:
        """
        Determine if system can exit rollback phase.
        """
        current_time = current_time or time.time()
        
        if state.phase != RolloutPhase.ROLLBACK:
            return False
        
        # Require minimum observation time
        policy = PHASE_POLICIES[RolloutPhase.ROLLBACK]
        hours_since_rollback = (current_time - state.last_decrease_at) / 3600
        
        if hours_since_rollback < policy.min_observation_hours:
            return False
        
        # Require low risk
        if risk_score > 0.3:
            return False
        
        # Exposure must be stable (near zero or stabilized)
        if state.current_exposure > 5.0:
            return False
        
        return True


# ============================================================================
# INVARIANT VALIDATOR
# ============================================================================


class RolloutInvariantValidator:
    """
    Enforces critical invariants.
    Violation ⇒ hard failure.
    
    Invariants:
    - Exposure never decreases unless rollback state
    - No positive delta during CONSTRAINED
    - Monotonic exposure history
    - Time-based budget enforcement
    - Reproducibility under identical inputs
    """
    
    def validate_decision(
        self,
        decision: RolloutDecision,
        state: RolloutState,
        previous_state: Optional[RolloutState] = None
    ) -> List[str]:
        """
        Validate rollout decision against invariants.
        Returns list of violations (empty if valid).
        """
        violations = []
        
        # Invariant 1: allowed_delta must be non-negative
        if decision.allowed_delta < 0:
            violations.append(
                f"CRITICAL: Negative delta {decision.allowed_delta}"
            )
        
        # Invariant 2: No positive delta during CONSTRAINED or ROLLBACK
        if state.phase in (RolloutPhase.CONSTRAINED, RolloutPhase.ROLLBACK):
            if decision.allowed_delta > 0:
                violations.append(
                    f"CRITICAL: Positive delta {decision.allowed_delta} "
                    f"during {state.phase.value}"
                )
        
        # Invariant 3: Confidence must be in valid range
        if not (0.0 <= decision.confidence <= 1.0):
            violations.append(
                f"CRITICAL: Invalid confidence {decision.confidence}"
            )
        
        # Invariant 4: Check monotonic exposure (if previous state exists)
        if previous_state:
            if state.current_exposure < previous_state.current_exposure:
                # Only valid if in rollback
                if state.phase != RolloutPhase.ROLLBACK:
                    violations.append(
                        f"CRITICAL: Exposure decreased outside rollback: "
                        f"{previous_state.current_exposure} → {state.current_exposure}"
                    )
        
        # Invariant 5: Version consistency
        if decision.controller_version != CONTROLLER_VERSION:
            violations.append(
                f"WARNING: Version mismatch {decision.controller_version} "
                f"!= {CONTROLLER_VERSION}"
            )
        
        # Invariant 6: Time consistency
        if state.last_increase_at > decision.evaluated_at:
            violations.append(
                "CRITICAL: Last increase timestamp in future"
            )
        
        if state.last_decrease_at > decision.evaluated_at:
            violations.append(
                "CRITICAL: Last decrease timestamp in future"
            )
        
        return violations
    
    def validate_state_transition(
        self,
        old_state: RolloutState,
        new_state: RolloutState,
        applied_delta: float
    ) -> List[str]:
        """
        Validate state transition is legal.
        """
        violations = []
        
        # Platform and account must not change
        if old_state.platform != new_state.platform:
            violations.append("CRITICAL: Platform changed during transition")
        
        if old_state.account_id != new_state.account_id:
            violations.append("CRITICAL: Account changed during transition")
        
        # Exposure change must match delta
        expected_exposure = old_state.current_exposure + applied_delta
        if abs(new_state.current_exposure - expected_exposure) > 0.001:
            violations.append(
                f"CRITICAL: Exposure mismatch. Expected {expected_exposure}, "
                f"got {new_state.current_exposure}"
            )
        
        # Counters must only increment
        if new_state.cumulative_increases < old_state.cumulative_increases:
            violations.append("CRITICAL: Cumulative increases decreased")
        
        if new_state.cumulative_rollbacks < old_state.cumulative_rollbacks:
            violations.append("CRITICAL: Cumulative rollbacks decreased")
        
        # Timestamp sanity
        if applied_delta > 0:
            if new_state.last_increase_at <= old_state.last_increase_at:
                violations.append("CRITICAL: Increase timestamp not updated")
        
        if applied_delta < 0:
            if new_state.last_decrease_at <= old_state.last_decrease_at:
                violations.append("CRITICAL: Decrease timestamp not updated")
        
        return violations


# ============================================================================
# MAIN CONTROLLER
# ============================================================================


class RolloutController:
    """
    Single authority over exposure increase rate.
    
    Converts safety signals into bounded, reversible exposure increases.
    """
    
    def __init__(
        self,
        budgets: Dict[str, ExposureBudget] = None,
        enable_invariant_checks: bool = True
    ):
        self.calculator = RolloutCalculator(budgets)
        self.decay_engine = RolloutDecayEngine()
        self.validator = RolloutInvariantValidator()
        self.enable_invariant_checks = enable_invariant_checks
    
    def evaluate_rollout(
        self,
        state: RolloutState,
        risk_score: float,
        current_time: float = None
    ) -> RolloutDecision:
        """
        Main evaluation function.
        
        Args:
            state: Current rollout state
            risk_score: From risk_evaluator.py, ∈ [0, 1]
            current_time: Unix timestamp (defaults to now)
        
        Returns:
            RolloutDecision with allowed delta and metadata
        """
        current_time = current_time or time.time()
        
        # Calculate allowed delta
        allowed_delta, hold_reason = self.calculator.calculate_allowed_delta(
            state, risk_score, current_time
        )
        
        # Get platform budget for cap
        budget = self.calculator.budgets.get(
            state.platform,
            self.calculator.budgets['default']
        )
        max_cap = state.current_exposure + budget.max_delta_per_day
        
        # Calculate confidence
        confidence = self.calculator.calculate_confidence(
            state, risk_score, current_time
        )
        
        # Create decision
        decision = RolloutDecision(
            allowed_delta=allowed_delta,
            max_exposure_cap=max_cap,
            hold_reason=hold_reason,
            confidence=confidence,
            evaluated_at=current_time,
            controller_version=CONTROLLER_VERSION
        )
        
        # Validate if enabled
        if self.enable_invariant_checks:
            violations = self.validator.validate_decision(decision, state)
            if violations:
                critical = [v for v in violations if 'CRITICAL' in v]
                if critical:
                    raise RuntimeError(
                        f"Invariant violations detected: {'; '.join(critical)}"
                    )
        
        return decision
    
    def evaluate_rollback(
        self,
        state: RolloutState,
        confidence: float,
        severity: float = 0.5
    ) -> float:
        """
        Calculate rollback delta (negative).
        
        Args:
            state: Current rollout state
            confidence: Confidence in rollback decision ∈ [0, 1]
            severity: How aggressive the rollback ∈ [0, 1]
        
        Returns:
            Negative delta for exposure reduction
        """
        return self.decay_engine.calculate_decay_delta(
            state.current_exposure,
            confidence,
            severity
        )
    
    def apply_decision(
        self,
        state: RolloutState,
        decision: RolloutDecision,
        current_time: float = None
    ) -> RolloutState:
        """
        Apply decision to state, creating new state.
        Validates transition if invariant checking enabled.
        
        Args:
            state: Current state
            decision: Decision to apply
            current_time: Unix timestamp
        
        Returns:
            New state with delta applied
        """
        current_time = current_time or time.time()
        
        new_exposure = state.current_exposure + decision.allowed_delta
        
        # Update timestamps and counters
        if decision.allowed_delta > 0:
            new_state = RolloutState(
                platform=state.platform,
                account_id=state.account_id,
                current_exposure=new_exposure,
                phase=state.phase,
                last_increase_at=current_time,
                last_decrease_at=state.last_decrease_at,
                cumulative_increases=state.cumulative_increases + 1,
                cumulative_rollbacks=state.cumulative_rollbacks,
                state_version=CONTROLLER_VERSION
            )
        elif decision.allowed_delta < 0:
            new_state = RolloutState(
                platform=state.platform,
                account_id=state.account_id,
                current_exposure=new_exposure,
                phase=state.phase,
                last_increase_at=state.last_increase_at,
                last_decrease_at=current_time,
                cumulative_increases=state.cumulative_increases,
                cumulative_rollbacks=state.cumulative_rollbacks + 1,
                state_version=CONTROLLER_VERSION
            )
        else:
            # No change
            new_state = state
        
        # Validate transition
        if self.enable_invariant_checks and decision.allowed_delta != 0:
            violations = self.validator.validate_state_transition(
                state, new_state, decision.allowed_delta
            )
            if violations:
                critical = [v for v in violations if 'CRITICAL' in v]
                if critical:
                    raise RuntimeError(
                        f"State transition violations: {'; '.join(critical)}"
                    )
        
        return new_state
    
    def transition_phase(
        self,
        state: RolloutState,
        new_phase: RolloutPhase,
        reason: str
    ) -> RolloutState:
        """
        Explicitly transition to a new phase.
        Transitions are never inferred, always explicit.
        """
        return RolloutState(
            platform=state.platform,
            account_id=state.account_id,
            current_exposure=state.current_exposure,
            phase=new_phase,
            last_increase_at=state.last_increase_at,
            last_decrease_at=state.last_decrease_at,
            cumulative_increases=state.cumulative_increases,
            cumulative_rollbacks=state.cumulative_rollbacks,
            state_version=CONTROLLER_VERSION
        )


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def create_initial_state(
    platform: str,
    account_id: str,
    initial_exposure: float = 0.0
) -> RolloutState:
    """
    Create initial rollout state for new account/platform.
    """
    return RolloutState(
        platform=platform,
        account_id=account_id,
        current_exposure=initial_exposure,
        phase=RolloutPhase.COLD_START,
        last_increase_at=0.0,
        last_decrease_at=0.0,
        cumulative_increases=0,
        cumulative_rollbacks=0,
        state_version=CONTROLLER_VERSION
    )


def state_fingerprint(state: RolloutState) -> str:
    """
    Generate deterministic fingerprint of state.
    Used for reproducibility verification.
    """
    state_dict = state.to_dict()
    state_json = json.dumps(state_dict, sort_keys=True)
    return hashlib.sha256(state_json.encode()).hexdigest()[:16]


# ============================================================================
# EXAMPLE USAGE
# ============================================================================


if __name__ == "__main__":
    # Initialize controller
    controller = RolloutController()
    
    # Create initial state
    state = create_initial_state(
        platform="twitter",
        account_id="acct_12345"
    )
    
    print(f"Initial state: {state.phase.value}")
    print(f"Current exposure: {state.current_exposure}")
    
    # Simulate rollout with low risk
    risk_score = 0.2
    decision = controller.evaluate_rollout(state, risk_score)
    
    print(f"\nRollout decision:")
    print(f"  Allowed delta: {decision.allowed_delta}")
    print(f"  Confidence: {decision.confidence:.2f}")
    print(f"  Hold reason: {decision.hold_reason}")
    
    # Apply decision
    if decision.allowed_delta > 0:
        state = controller.apply_decision(state, decision)
        print(f"\nNew exposure: {state.current_exposure}")
        print(f"Cumulative increases: {state.cumulative_increases}")
    
    # Transition to trust building after observation
    state = controller.transition_phase(
        state,
        RolloutPhase.TRUST_BUILDING,
        "Observation period complete"
    )
    print(f"\nTransitioned to: {state.phase.value}")
    
    # Simulate high risk scenario
    risk_score = 0.8
    decision = controller.evaluate_rollout(state, risk_score)
    print(f"\nHigh risk decision:")
    print(f"  Allowed delta: {decision.allowed_delta}")
    print(f"  Confidence: {decision.confidence:.2f}")
    
    print("\n✓ Rollout controller operational")
    print(f"✓ Version: {CONTROLLER_VERSION}")
    print(f"✓ State fingerprint: {state_fingerprint(state)}")