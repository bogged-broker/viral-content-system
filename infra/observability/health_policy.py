"""
/infra/observability/health_policy.py

System Health Tolerance & Enforcement Policy Authority

This file defines what the system is allowed to tolerate. It is the constitution
of system health.

It defines policy, not behavior.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Tuple, List, Dict
import hashlib
import json


# ============================================================================
# CORE ENUMS (STRICT — NO STRINGS)
# ============================================================================


class HealthDimension(Enum):
    """
    Health dimensions are orthogonal by design.
    
    Each dimension represents a separate aspect of system health.
    """

    LATENCY = "latency"
    THROUGHPUT = "throughput"
    ERROR_RATE = "error_rate"
    DATA_INTEGRITY = "data_integrity"
    CAUSALITY = "causality"
    RESOURCE_SATURATION = "resource_saturation"


class HealthState(Enum):
    """
    Mechanical health states, not opinions.
    
    Systems degrade, compound, then collapse. These states capture that progression.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNSTABLE = "unstable"
    CRITICAL = "critical"

    def __lt__(self, other: "HealthState") -> bool:
        """Define ordering for state comparison."""
        order = {
            HealthState.HEALTHY: 0,
            HealthState.DEGRADED: 1,
            HealthState.UNSTABLE: 2,
            HealthState.CRITICAL: 3,
        }
        return order[self] < order[other]

    def __le__(self, other: "HealthState") -> bool:
        """Define ordering for state comparison."""
        return self == other or self < other

    def __gt__(self, other: "HealthState") -> bool:
        """Define ordering for state comparison."""
        return not self <= other

    def __ge__(self, other: "HealthState") -> bool:
        """Define ordering for state comparison."""
        return self == other or self > other


class EscalationAction(Enum):
    """
    Actions are permissions, not commands.
    
    These define what the system is allowed to do at each health state.
    """

    CONTINUE = "continue"
    DEGRADE = "degrade"
    ISOLATE = "isolate"
    PAUSE = "pause"
    SHUTDOWN = "shutdown"


class AggregationType(Enum):
    """Aggregation types for health windows. No free-form allowed."""

    MEAN = "mean"
    MEDIAN = "median"
    P95 = "p95"
    P99 = "p99"
    MAX = "max"
    MIN = "min"
    COUNT = "count"


# ============================================================================
# CORE DATA STRUCTURES (IMMUTABLE)
# ============================================================================


@dataclass(frozen=True)
class HealthThreshold:
    """
    Defines threshold bounds for a specific health dimension and state.
    
    Rules:
    - Thresholds are time-bounded
    - Instantaneous spikes ≠ failures
    """

    dimension: HealthDimension
    state: HealthState

    min_value: float | None
    max_value: float | None

    duration_seconds: int

    def is_violated(self, value: float, duration: int) -> bool:
        """
        Check if a value violates this threshold.
        
        Args:
            value: Observed value
            duration: Duration the value has been at this level (seconds)
            
        Returns:
            True if threshold violated
        """
        # Must exceed duration requirement
        if duration < self.duration_seconds:
            return False

        # Check bounds
        if self.min_value is not None and value < self.min_value:
            return True

        if self.max_value is not None and value > self.max_value:
            return True

        return False

    def validate(self) -> None:
        """
        Validate threshold configuration.
        
        Raises:
            ValueError: If threshold configuration is invalid
        """
        if self.min_value is None and self.max_value is None:
            raise ValueError(
                f"Threshold for {self.dimension.value}:{self.state.value} "
                f"must have at least one bound"
            )

        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError(
                f"Threshold for {self.dimension.value}:{self.state.value} "
                f"has min_value > max_value"
            )

        if self.duration_seconds <= 0:
            raise ValueError(
                f"Threshold duration must be positive, got {self.duration_seconds}"
            )


@dataclass(frozen=True)
class HealthWindow:
    """
    Defines time window for health evaluation.
    
    No free-form aggregation allowed.
    """

    window_size_seconds: int
    aggregation: AggregationType

    def validate(self) -> None:
        """
        Validate window configuration.
        
        Raises:
            ValueError: If window configuration invalid
        """
        if self.window_size_seconds <= 0:
            raise ValueError(
                f"Window size must be positive, got {self.window_size_seconds}"
            )


@dataclass(frozen=True)
class HealthRule:
    """
    Atomic and explicit health rule.
    
    Each rule maps a dimension to thresholds, window, and escalation.
    """

    dimension: HealthDimension

    thresholds: tuple[HealthThreshold, ...]
    window: HealthWindow

    escalation: EscalationAction

    def validate(self) -> None:
        """
        Validate rule configuration.
        
        Raises:
            ValueError: If rule configuration invalid
        """
        if not self.thresholds:
            raise ValueError(f"Rule for {self.dimension.value} has no thresholds")

        # Validate all thresholds
        for threshold in self.thresholds:
            threshold.validate()

            # Ensure threshold matches rule dimension
            if threshold.dimension != self.dimension:
                raise ValueError(
                    f"Threshold dimension {threshold.dimension.value} "
                    f"doesn't match rule dimension {self.dimension.value}"
                )

        # Validate window
        self.window.validate()

        # Ensure thresholds are ordered by state severity
        states_seen = set()
        for threshold in self.thresholds:
            if threshold.state in states_seen:
                raise ValueError(
                    f"Duplicate threshold state {threshold.state.value} "
                    f"in rule for {self.dimension.value}"
                )
            states_seen.add(threshold.state)


# ============================================================================
# ESCALATION MATRIX (CRITICAL)
# ============================================================================


class EscalationMatrix:
    """
    Defines allowed actions for each health state.
    
    Once escalated:
    - Rollback requires clean recovery signal
    - Hysteresis is mandatory
    """

    # Immutable mapping of state -> allowed actions
    _MATRIX: dict[HealthState, set[EscalationAction]] = {
        HealthState.HEALTHY: {EscalationAction.CONTINUE},
        HealthState.DEGRADED: {EscalationAction.CONTINUE, EscalationAction.DEGRADE},
        HealthState.UNSTABLE: {
            EscalationAction.DEGRADE,
            EscalationAction.ISOLATE,
            EscalationAction.PAUSE,
        },
        HealthState.CRITICAL: {EscalationAction.PAUSE, EscalationAction.SHUTDOWN},
    }

    @classmethod
    def get_allowed_actions(cls, state: HealthState) -> set[EscalationAction]:
        """
        Get allowed escalation actions for a health state.
        
        Args:
            state: Current health state
            
        Returns:
            Set of allowed escalation actions
        """
        return cls._MATRIX[state].copy()

    @classmethod
    def is_action_allowed(
        cls,
        state: HealthState,
        action: EscalationAction,
    ) -> bool:
        """
        Check if an action is allowed for a given state.
        
        Args:
            state: Current health state
            action: Proposed action
            
        Returns:
            True if action allowed
        """
        return action in cls._MATRIX[state]

    @classmethod
    def validate_escalation(
        cls,
        from_state: HealthState,
        to_state: HealthState,
        recovery_window_met: bool,
    ) -> bool:
        """
        Validate state transition.
        
        Monotonic escalation: can only get worse without recovery.
        Hysteresis: downgrades require recovery window.
        
        Args:
            from_state: Current state
            to_state: Proposed state
            recovery_window_met: Whether recovery window requirements met
            
        Returns:
            True if transition valid
        """
        # Escalation (worsening) always allowed
        if to_state > from_state:
            return True

        # Same state always allowed
        if to_state == from_state:
            return True

        # Downgrade (improvement) requires recovery window
        if to_state < from_state:
            return recovery_window_met

        return False


# ============================================================================
# HEALTH POLICY (CORE ENGINE)
# ============================================================================


@dataclass
class HealthEvaluation:
    """Result of health policy evaluation."""

    overall_state: HealthState
    dimension_states: dict[HealthDimension, HealthState]
    violated_thresholds: list[HealthThreshold]
    allowed_actions: set[EscalationAction]
    evaluation_timestamp: int


class HealthPolicy:
    """
    Core health policy engine.
    
    Guarantees:
    - Deterministic evaluation order
    - Monotonic escalation
    - No downgrade without recovery window
    - No side effects
    """

    def __init__(
        self,
        rules: list[HealthRule],
        recovery_window_seconds: int = 300,
        policy_version: str = "v1",
    ):
        """
        Initialize health policy.
        
        Args:
            rules: List of health rules to enforce
            recovery_window_seconds: Required stable time before downgrade
            policy_version: Version identifier for this policy
        """
        # Validate all rules
        for rule in rules:
            rule.validate()

        self._rules = {rule.dimension: rule for rule in rules}
        self._recovery_window_seconds = recovery_window_seconds
        self._policy_version = policy_version

        # Track state history for hysteresis
        self._state_history: list[tuple[int, HealthState]] = []

    @property
    def version(self) -> str:
        """Get policy version."""
        return self._policy_version

    def evaluate(
        self,
        signals: dict[HealthDimension, float],
        signal_durations: dict[HealthDimension, int],
        timestamp: int,
    ) -> HealthEvaluation:
        """
        Evaluate health policy against current signals.
        
        Args:
            signals: Current signal values per dimension
            signal_durations: How long each signal has been at current level
            timestamp: Evaluation timestamp
            
        Returns:
            HealthEvaluation with overall state and allowed actions
        """
        dimension_states: dict[HealthDimension, HealthState] = {}
        violated_thresholds: list[HealthThreshold] = []

        # Evaluate each dimension in deterministic order
        for dimension in sorted(self._rules.keys(), key=lambda d: d.value):
            rule = self._rules[dimension]

            # Get signal for this dimension
            signal_value = signals.get(dimension)
            if signal_value is None:
                # Missing signal defaults to healthy
                dimension_states[dimension] = HealthState.HEALTHY
                continue

            signal_duration = signal_durations.get(dimension, 0)

            # Evaluate thresholds for this dimension
            dimension_state = self._evaluate_dimension(
                rule,
                signal_value,
                signal_duration,
                violated_thresholds,
            )

            dimension_states[dimension] = dimension_state

        # Determine overall state (worst dimension wins)
        overall_state = self._compute_overall_state(dimension_states)

        # Record state for hysteresis tracking
        self._state_history.append((timestamp, overall_state))
        self._trim_state_history(timestamp)

        # Get allowed actions for current state
        allowed_actions = self.allowed_actions(overall_state, timestamp)

        return HealthEvaluation(
            overall_state=overall_state,
            dimension_states=dimension_states,
            violated_thresholds=violated_thresholds,
            allowed_actions=allowed_actions,
            evaluation_timestamp=timestamp,
        )

    def _evaluate_dimension(
        self,
        rule: HealthRule,
        signal_value: float,
        signal_duration: int,
        violated_thresholds: list[HealthThreshold],
    ) -> HealthState:
        """
        Evaluate a single dimension's health state.
        
        Args:
            rule: HealthRule for this dimension
            signal_value: Current signal value
            signal_duration: Duration at current value
            violated_thresholds: List to append violations to
            
        Returns:
            HealthState for this dimension
        """
        # Sort thresholds by severity (worst first)
        sorted_thresholds = sorted(
            rule.thresholds,
            key=lambda t: t.state,
            reverse=True,
        )

        # Check thresholds from worst to best
        for threshold in sorted_thresholds:
            if threshold.is_violated(signal_value, signal_duration):
                violated_thresholds.append(threshold)
                return threshold.state

        # No violations = healthy
        return HealthState.HEALTHY

    def _compute_overall_state(
        self,
        dimension_states: dict[HealthDimension, HealthState],
    ) -> HealthState:
        """
        Compute overall health state from dimension states.
        
        Worst dimension wins.
        
        Args:
            dimension_states: States per dimension
            
        Returns:
            Overall HealthState
        """
        if not dimension_states:
            return HealthState.HEALTHY

        # Return worst state
        return max(dimension_states.values())

    def allowed_actions(
        self,
        state: HealthState,
        timestamp: int,
    ) -> set[EscalationAction]:
        """
        Get allowed escalation actions for current state.
        
        Respects hysteresis: downgrades require recovery window.
        
        Args:
            state: Current health state
            timestamp: Current timestamp
            
        Returns:
            Set of allowed escalation actions
        """
        base_actions = EscalationMatrix.get_allowed_actions(state)

        # Check if we can downgrade (improve state)
        if self._state_history:
            previous_state = self._state_history[-1][1]

            # If current state is better, verify recovery window
            if state < previous_state:
                recovery_met = self._check_recovery_window(state, timestamp)
                if not recovery_met:
                    # Can't downgrade yet, restrict actions
                    # Remove actions that would assume improvement
                    return base_actions - {EscalationAction.CONTINUE}

        return base_actions

    def _check_recovery_window(
        self,
        target_state: HealthState,
        current_timestamp: int,
    ) -> bool:
        """
        Check if recovery window requirement met for downgrade.
        
        Args:
            target_state: State we want to downgrade to
            current_timestamp: Current timestamp
            
        Returns:
            True if recovery window met
        """
        if not self._state_history:
            return True

        # Find when we first reached target_state or better
        recovery_start = None
        for timestamp, state in reversed(self._state_history):
            if state <= target_state:
                recovery_start = timestamp
            else:
                break

        if recovery_start is None:
            return False

        # Check if we've been stable long enough
        stable_duration = current_timestamp - recovery_start
        return stable_duration >= self._recovery_window_seconds

    def _trim_state_history(self, current_timestamp: int) -> None:
        """
        Trim state history to keep only relevant window.
        
        Args:
            current_timestamp: Current timestamp
        """
        cutoff = current_timestamp - (self._recovery_window_seconds * 2)
        self._state_history = [
            (ts, state)
            for ts, state in self._state_history
            if ts >= cutoff
        ]

    def get_rule(self, dimension: HealthDimension) -> HealthRule | None:
        """Get rule for a specific dimension."""
        return self._rules.get(dimension)

    def get_all_rules(self) -> list[HealthRule]:
        """Get all rules in deterministic order."""
        return [
            self._rules[dim]
            for dim in sorted(self._rules.keys(), key=lambda d: d.value)
        ]


# ============================================================================
# HEALTH POLICY REGISTRY (VERSIONED)
# ============================================================================


class HealthPolicyRegistry:
    """
    Manages versioned health policies.
    
    Rules:
    - Policies are immutable
    - Versioned per deployment
    - Hash-tracked for audit
    
    Silent policy drift is forbidden.
    """

    def __init__(self):
        """Initialize policy registry."""
        self._policies: dict[str, HealthPolicy] = {}
        self._policy_hashes: dict[str, str] = {}

    def register_policy(self, policy: HealthPolicy) -> str:
        """
        Register a health policy.
        
        Args:
            policy: HealthPolicy to register
            
        Returns:
            Policy version
            
        Raises:
            ValueError: If policy version already registered
        """
        version = policy.version

        if version in self._policies:
            raise ValueError(f"Policy version '{version}' already registered")

        # Compute policy hash for audit trail
        policy_hash = self._compute_policy_hash(policy)

        self._policies[version] = policy
        self._policy_hashes[version] = policy_hash

        return version

    def get_policy(self, version: str) -> HealthPolicy:
        """
        Get policy by version.
        
        Args:
            version: Policy version identifier
            
        Returns:
            HealthPolicy
            
        Raises:
            ValueError: If policy version not found
        """
        policy = self._policies.get(version)
        if policy is None:
            raise ValueError(f"Policy version '{version}' not found in registry")

        return policy

    def get_policy_hash(self, version: str) -> str:
        """
        Get policy hash for audit.
        
        Args:
            version: Policy version identifier
            
        Returns:
            Policy hash
            
        Raises:
            ValueError: If policy version not found
        """
        policy_hash = self._policy_hashes.get(version)
        if policy_hash is None:
            raise ValueError(f"Policy version '{version}' not found in registry")

        return policy_hash

    def list_versions(self) -> list[str]:
        """Get all registered policy versions."""
        return sorted(self._policies.keys())

    def _compute_policy_hash(self, policy: HealthPolicy) -> str:
        """
        Compute deterministic hash of policy configuration.
        
        Args:
            policy: HealthPolicy to hash
            
        Returns:
            Hex hash string
        """
        # Serialize policy rules to JSON
        rules_data = []
        for rule in policy.get_all_rules():
            rule_dict = {
                "dimension": rule.dimension.value,
                "window": {
                    "size": rule.window.window_size_seconds,
                    "aggregation": rule.window.aggregation.value,
                },
                "escalation": rule.escalation.value,
                "thresholds": [
                    {
                        "state": t.state.value,
                        "min": t.min_value,
                        "max": t.max_value,
                        "duration": t.duration_seconds,
                    }
                    for t in sorted(rule.thresholds, key=lambda x: x.state.value)
                ],
            }
            rules_data.append(rule_dict)

        policy_data = {
            "version": policy.version,
            "recovery_window": policy._recovery_window_seconds,
            "rules": rules_data,
        }

        # Compute hash
        json_str = json.dumps(policy_data, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(json_str.encode()).hexdigest()


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================


def create_default_policy(version: str = "v1") -> HealthPolicy:
    """
    Create a default health policy with sensible thresholds.
    
    Args:
        version: Policy version identifier
        
    Returns:
        Configured HealthPolicy
    """
    rules = [
        # Latency rule
        HealthRule(
            dimension=HealthDimension.LATENCY,
            thresholds=(
                HealthThreshold(
                    dimension=HealthDimension.LATENCY,
                    state=HealthState.DEGRADED,
                    min_value=None,
                    max_value=1000.0,  # 1s
                    duration_seconds=60,
                ),
                HealthThreshold(
                    dimension=HealthDimension.LATENCY,
                    state=HealthState.UNSTABLE,
                    min_value=None,
                    max_value=5000.0,  # 5s
                    duration_seconds=30,
                ),
                HealthThreshold(
                    dimension=HealthDimension.LATENCY,
                    state=HealthState.CRITICAL,
                    min_value=None,
                    max_value=10000.0,  # 10s
                    duration_seconds=10,
                ),
            ),
            window=HealthWindow(
                window_size_seconds=300,
                aggregation=AggregationType.P95,
            ),
            escalation=EscalationAction.DEGRADE,
        ),
        # Error rate rule
        HealthRule(
            dimension=HealthDimension.ERROR_RATE,
            thresholds=(
                HealthThreshold(
                    dimension=HealthDimension.ERROR_RATE,
                    state=HealthState.DEGRADED,
                    min_value=None,
                    max_value=0.01,  # 1%
                    duration_seconds=120,
                ),
                HealthThreshold(
                    dimension=HealthDimension.ERROR_RATE,
                    state=HealthState.UNSTABLE,
                    min_value=None,
                    max_value=0.05,  # 5%
                    duration_seconds=60,
                ),
                HealthThreshold(
                    dimension=HealthDimension.ERROR_RATE,
                    state=HealthState.CRITICAL,
                    min_value=None,
                    max_value=0.10,  # 10%
                    duration_seconds=30,
                ),
            ),
            window=HealthWindow(
                window_size_seconds=300,
                aggregation=AggregationType.MEAN,
            ),
            escalation=EscalationAction.ISOLATE,
        ),
        # Throughput rule
        HealthRule(
            dimension=HealthDimension.THROUGHPUT,
            thresholds=(
                HealthThreshold(
                    dimension=HealthDimension.THROUGHPUT,
                    state=HealthState.DEGRADED,
                    min_value=100.0,  # Min ops/sec
                    max_value=None,
                    duration_seconds=180,
                ),
                HealthThreshold(
                    dimension=HealthDimension.THROUGHPUT,
                    state=HealthState.UNSTABLE,
                    min_value=50.0,
                    max_value=None,
                    duration_seconds=120,
                ),
                HealthThreshold(
                    dimension=HealthDimension.THROUGHPUT,
                    state=HealthState.CRITICAL,
                    min_value=10.0,
                    max_value=None,
                    duration_seconds=60,
                ),
            ),
            window=HealthWindow(
                window_size_seconds=300,
                aggregation=AggregationType.MEAN,
            ),
            escalation=EscalationAction.PAUSE,
        ),
    ]

    return HealthPolicy(
        rules=rules,
        recovery_window_seconds=300,
        policy_version=version,
    )


def create_strict_policy(version: str = "v1-strict") -> HealthPolicy:
    """
    Create a strict health policy with tighter thresholds.
    
    Args:
        version: Policy version identifier
        
    Returns:
        Configured HealthPolicy with strict thresholds
    """
    rules = [
        # Stricter latency rule
        HealthRule(
            dimension=HealthDimension.LATENCY,
            thresholds=(
                HealthThreshold(
                    dimension=HealthDimension.LATENCY,
                    state=HealthState.DEGRADED,
                    min_value=None,
                    max_value=500.0,  # 500ms
                    duration_seconds=30,
                ),
                HealthThreshold(
                    dimension=HealthDimension.LATENCY,
                    state=HealthState.UNSTABLE,
                    min_value=None,
                    max_value=2000.0,  # 2s
                    duration_seconds=15,
                ),
                HealthThreshold(
                    dimension=HealthDimension.LATENCY,
                    state=HealthState.CRITICAL,
                    min_value=None,
                    max_value=5000.0,  # 5s
                    duration_seconds=5,
                ),
            ),
            window=HealthWindow(
                window_size_seconds=180,
                aggregation=AggregationType.P99,
            ),
            escalation=EscalationAction.DEGRADE,
        ),
        # Stricter error rate rule
        HealthRule(
            dimension=HealthDimension.ERROR_RATE,
            thresholds=(
                HealthThreshold(
                    dimension=HealthDimension.ERROR_RATE,
                    state=HealthState.DEGRADED,
                    min_value=None,
                    max_value=0.005,  # 0.5%
                    duration_seconds=60,
                ),
                HealthThreshold(
                    dimension=HealthDimension.ERROR_RATE,
                    state=HealthState.UNSTABLE,
                    min_value=None,
                    max_value=0.02,  # 2%
                    duration_seconds=30,
                ),
                HealthThreshold(
                    dimension=HealthDimension.ERROR_RATE,
                    state=HealthState.CRITICAL,
                    min_value=None,
                    max_value=0.05,  # 5%
                    duration_seconds=15,
                ),
            ),
            window=HealthWindow(
                window_size_seconds=180,
                aggregation=AggregationType.MEAN,
            ),
            escalation=EscalationAction.PAUSE,
        ),
    ]

    return HealthPolicy(
        rules=rules,
        recovery_window_seconds=600,  # Longer recovery window
        policy_version=version,
    )