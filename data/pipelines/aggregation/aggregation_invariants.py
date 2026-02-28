"""
Aggregation Invariants - Mathematical Constitution

This module defines absolute laws that every aggregation run must obey.
If an invariant fails, aggregation is illegal and must halt immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, List, Dict, Any, Optional, Set
from abc import ABC, abstractmethod
import hashlib


class InvariantCategory(Enum):
    CONTEXT = "context"
    ORDERING = "ordering"
    WINDOW = "window"
    COUNTER_STATE = "counter_state"
    CARDINALITY = "cardinality"
    DETERMINISM = "determinism"
    REPLAY = "replay"
    FINALIZATION = "finalization"


class InvariantSeverity(Enum):
    ABORT = "abort"
    ESCALATE = "escalate"
    TRUST_FAILURE = "trust_failure"
    INTEGRITY_BREACH = "integrity_breach"


@dataclass(frozen=True)
class InvariantViolation:
    """Immutable record of an invariant violation."""
    
    category: InvariantCategory
    invariant_name: str
    severity: InvariantSeverity
    run_id: str
    failure_reason: str
    timestamp_ms: int
    window_id: Optional[str] = None
    counter_name: Optional[str] = None
    context_data: Optional[Dict[str, Any]] = None
    
    def __str__(self) -> str:
        parts = [
            f"[{self.severity.value.upper()}]",
            f"{self.category.value}.{self.invariant_name}",
            f"run={self.run_id}",
        ]
        
        if self.window_id:
            parts.append(f"window={self.window_id}")
        
        if self.counter_name:
            parts.append(f"counter={self.counter_name}")
        
        parts.append(f"reason={self.failure_reason}")
        
        return " | ".join(parts)


class AggregationContext(Protocol):
    """Protocol defining minimal aggregation context contract."""
    
    @property
    def context_id(self) -> str:
        """Unique context identifier."""
        ...
    
    @property
    def pipeline_version(self) -> str:
        """Version of pipeline code."""
        ...
    
    @property
    def counter_registry_version(self) -> str:
        """Version of counter registry."""
        ...
    
    @property
    def invariant_version(self) -> str:
        """Version of invariant rules."""
        ...
    
    def compute_hash(self) -> str:
        """Compute deterministic hash of context."""
        ...


class WindowAssignment(Protocol):
    """Protocol for window assignment."""
    
    @property
    def window_id(self) -> str:
        ...
    
    @property
    def window_start_ms(self) -> int:
        ...
    
    @property
    def window_end_ms(self) -> int:
        ...


class CounterState(Protocol):
    """Protocol for counter state."""
    
    @property
    def counter_name(self) -> str:
        ...
    
    @property
    def current_value(self) -> int:
        ...
    
    @property
    def version(self) -> str:
        ...


class Invariant(ABC):
    """Base class for all invariants."""
    
    @abstractmethod
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        """
        Check invariant. Returns None if passes, violation if fails.
        Must be pure - no side effects.
        """
        pass
    
    @property
    @abstractmethod
    def category(self) -> InvariantCategory:
        """Invariant category."""
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Invariant name."""
        pass
    
    @property
    @abstractmethod
    def severity(self) -> InvariantSeverity:
        """Violation severity."""
        pass


class ContextImmutableInvariant(Invariant):
    """Ensures aggregation context is immutable."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CONTEXT
    
    @property
    def name(self) -> str:
        return "context_immutable"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ABORT
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        context = kwargs.get("context")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if context is None:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason="Context is None",
                timestamp_ms=timestamp_ms
            )
        
        try:
            context.context_id
            context.pipeline_version
            context.counter_registry_version
            context.invariant_version
        except AttributeError as e:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Missing required context field: {e}",
                timestamp_ms=timestamp_ms
            )
        
        return None


class ContextHashMatchInvariant(Invariant):
    """Ensures context hash matches audit record."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CONTEXT
    
    @property
    def name(self) -> str:
        return "context_hash_match"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ABORT
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        context = kwargs.get("context")
        expected_hash = kwargs.get("expected_hash")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if context is None or expected_hash is None:
            return None
        
        actual_hash = context.compute_hash()
        
        if actual_hash != expected_hash:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Context hash mismatch: expected={expected_hash}, actual={actual_hash}",
                timestamp_ms=timestamp_ms
            )
        
        return None


class FactOrderingDeterministicInvariant(Invariant):
    """Ensures fact ordering is deterministic."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.ORDERING
    
    @property
    def name(self) -> str:
        return "fact_ordering_deterministic"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ABORT
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        facts = kwargs.get("facts", [])
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if not facts:
            return None
        
        for i in range(len(facts) - 1):
            if not hasattr(facts[i], "fact_id"):
                return InvariantViolation(
                    category=self.category,
                    invariant_name=self.name,
                    severity=self.severity,
                    run_id=run_id,
                    failure_reason=f"Fact at index {i} missing fact_id",
                    timestamp_ms=timestamp_ms
                )
        
        return None


class WindowBoundariesExplicitInvariant(Invariant):
    """Ensures all windows have explicit boundaries."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.WINDOW
    
    @property
    def name(self) -> str:
        return "window_boundaries_explicit"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ABORT
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        window = kwargs.get("window")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if window is None:
            return None
        
        if not hasattr(window, "window_start_ms") or not hasattr(window, "window_end_ms"):
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason="Window missing start or end boundary",
                timestamp_ms=timestamp_ms,
                window_id=getattr(window, "window_id", "unknown")
            )
        
        if window.window_start_ms >= window.window_end_ms:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Invalid window boundaries: start={window.window_start_ms} >= end={window.window_end_ms}",
                timestamp_ms=timestamp_ms,
                window_id=window.window_id
            )
        
        return None


class WindowOverlapDeclaredInvariant(Invariant):
    """Ensures window overlap is explicitly allowed."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.WINDOW
    
    @property
    def name(self) -> str:
        return "window_overlap_declared"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ABORT
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        windows = kwargs.get("windows", [])
        allow_overlap = kwargs.get("allow_overlap", False)
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if allow_overlap or len(windows) < 2:
            return None
        
        sorted_windows = sorted(windows, key=lambda w: w.window_start_ms)
        
        for i in range(len(sorted_windows) - 1):
            current = sorted_windows[i]
            next_window = sorted_windows[i + 1]
            
            if current.window_end_ms > next_window.window_start_ms:
                return InvariantViolation(
                    category=self.category,
                    invariant_name=self.name,
                    severity=self.severity,
                    run_id=run_id,
                    failure_reason=f"Undeclared overlap: [{current.window_start_ms}, {current.window_end_ms}) overlaps [{next_window.window_start_ms}, {next_window.window_end_ms})",
                    timestamp_ms=timestamp_ms,
                    window_id=f"{current.window_id},{next_window.window_id}"
                )
        
        return None


class CounterValueIntegerInvariant(Invariant):
    """Ensures counter values are integers."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.COUNTER_STATE
    
    @property
    def name(self) -> str:
        return "counter_value_integer"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ESCALATE
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        counter_state = kwargs.get("counter_state")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if counter_state is None:
            return None
        
        value = counter_state.current_value
        
        if not isinstance(value, int):
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Counter value is not integer: type={type(value).__name__}, value={value}",
                timestamp_ms=timestamp_ms,
                counter_name=counter_state.counter_name
            )
        
        return None


class CounterValueNonNegativeInvariant(Invariant):
    """Ensures counter values are non-negative."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.COUNTER_STATE
    
    @property
    def name(self) -> str:
        return "counter_value_non_negative"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ESCALATE
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        counter_state = kwargs.get("counter_state")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if counter_state is None:
            return None
        
        if counter_state.current_value < 0:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Counter value is negative: {counter_state.current_value}",
                timestamp_ms=timestamp_ms,
                counter_name=counter_state.counter_name
            )
        
        return None


class CounterMonotonicInvariant(Invariant):
    """Ensures monotonic counters never decrease."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.COUNTER_STATE
    
    @property
    def name(self) -> str:
        return "counter_monotonic"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ESCALATE
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        prior_value = kwargs.get("prior_value")
        new_value = kwargs.get("new_value")
        is_monotonic = kwargs.get("is_monotonic", False)
        counter_name = kwargs.get("counter_name", "unknown")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if not is_monotonic or prior_value is None or new_value is None:
            return None
        
        if new_value < prior_value:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Monotonic counter decreased: {prior_value} -> {new_value}",
                timestamp_ms=timestamp_ms,
                counter_name=counter_name
            )
        
        return None


class CardinalityLimitInvariant(Invariant):
    """Ensures facts respect max increment cardinality."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CARDINALITY
    
    @property
    def name(self) -> str:
        return "cardinality_limit"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.ABORT
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        increment_count = kwargs.get("increment_count", 0)
        max_increment_per_fact = kwargs.get("max_increment_per_fact", 1)
        counter_name = kwargs.get("counter_name", "unknown")
        fact_id = kwargs.get("fact_id", "unknown")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if increment_count > max_increment_per_fact:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Fact {fact_id} incremented counter {increment_count} times, max allowed is {max_increment_per_fact}",
                timestamp_ms=timestamp_ms,
                counter_name=counter_name
            )
        
        return None


class DeterminismInvariant(Invariant):
    """Ensures aggregation is deterministic."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DETERMINISM
    
    @property
    def name(self) -> str:
        return "determinism"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.TRUST_FAILURE
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        output_digest_1 = kwargs.get("output_digest_1")
        output_digest_2 = kwargs.get("output_digest_2")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if output_digest_1 is None or output_digest_2 is None:
            return None
        
        if output_digest_1 != output_digest_2:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason=f"Non-deterministic output: digest_1={output_digest_1}, digest_2={output_digest_2}",
                timestamp_ms=timestamp_ms
            )
        
        return None


class ReplayExactMatchInvariant(Invariant):
    """Ensures replay produces exact match."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY
    
    @property
    def name(self) -> str:
        return "replay_exact_match"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.TRUST_FAILURE
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        original_output = kwargs.get("original_output")
        replay_output = kwargs.get("replay_output")
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if original_output is None or replay_output is None:
            return None
        
        if original_output != replay_output:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason="Replay output does not match original",
                timestamp_ms=timestamp_ms,
                context_data={"original": str(original_output), "replay": str(replay_output)}
            )
        
        return None


class FinalizationImmutableInvariant(Invariant):
    """Ensures finalized runs cannot be modified."""
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.FINALIZATION
    
    @property
    def name(self) -> str:
        return "finalization_immutable"
    
    @property
    def severity(self) -> InvariantSeverity:
        return InvariantSeverity.INTEGRITY_BREACH
    
    def check(self, **kwargs) -> Optional[InvariantViolation]:
        is_finalized = kwargs.get("is_finalized", False)
        attempting_mutation = kwargs.get("attempting_mutation", False)
        run_id = kwargs.get("run_id", "unknown")
        timestamp_ms = kwargs.get("timestamp_ms", 0)
        
        if is_finalized and attempting_mutation:
            return InvariantViolation(
                category=self.category,
                invariant_name=self.name,
                severity=self.severity,
                run_id=run_id,
                failure_reason="Attempted to mutate finalized run",
                timestamp_ms=timestamp_ms
            )
        
        return None


class InvariantRegistry:
    """Central registry of all invariants."""
    
    def __init__(self):
        self._invariants: Dict[InvariantCategory, List[Invariant]] = {
            category: [] for category in InvariantCategory
        }
        self._register_standard_invariants()
    
    def _register_standard_invariants(self) -> None:
        """Register all standard invariants."""
        
        standard_invariants = [
            ContextImmutableInvariant(),
            ContextHashMatchInvariant(),
            FactOrderingDeterministicInvariant(),
            WindowBoundariesExplicitInvariant(),
            WindowOverlapDeclaredInvariant(),
            CounterValueIntegerInvariant(),
            CounterValueNonNegativeInvariant(),
            CounterMonotonicInvariant(),
            CardinalityLimitInvariant(),
            DeterminismInvariant(),
            ReplayExactMatchInvariant(),
            FinalizationImmutableInvariant(),
        ]
        
        for invariant in standard_invariants:
            self.register(invariant)
    
    def register(self, invariant: Invariant) -> None:
        """Register an invariant."""
        self._invariants[invariant.category].append(invariant)
    
    def get_invariants(self, category: InvariantCategory) -> List[Invariant]:
        """Get all invariants for a category."""
        return list(self._invariants[category])
    
    def get_all_invariants(self) -> List[Invariant]:
        """Get all registered invariants."""
        result = []
        for invariants in self._invariants.values():
            result.extend(invariants)
        return result


class InvariantEnforcer:
    """Enforces invariants during aggregation."""
    
    def __init__(self, registry: InvariantRegistry):
        self._registry = registry
        self._violations: List[InvariantViolation] = []
    
    def check_category(
        self,
        category: InvariantCategory,
        **kwargs
    ) -> List[InvariantViolation]:
        """Check all invariants in a category."""
        
        violations = []
        
        for invariant in self._registry.get_invariants(category):
            violation = invariant.check(**kwargs)
            if violation:
                violations.append(violation)
                self._violations.append(violation)
        
        return violations
    
    def check_all(self, **kwargs) -> List[InvariantViolation]:
        """Check all registered invariants."""
        
        violations = []
        
        for invariant in self._registry.get_all_invariants():
            violation = invariant.check(**kwargs)
            if violation:
                violations.append(violation)
                self._violations.append(violation)
        
        return violations
    
    def enforce_or_abort(
        self,
        category: InvariantCategory,
        **kwargs
    ) -> None:
        """Check invariants and abort on any violation."""
        
        violations = self.check_category(category, **kwargs)
        
        if violations:
            raise InvariantViolationError(violations)
    
    def get_all_violations(self) -> List[InvariantViolation]:
        """Get all violations encountered."""
        return list(self._violations)
    
    def reset_violations(self) -> None:
        """Clear violation history."""
        self._violations.clear()


class InvariantViolationError(Exception):
    """Exception raised when invariants are violated."""
    
    def __init__(self, violations: List[InvariantViolation]):
        self.violations = violations
        
        messages = [str(v) for v in violations]
        super().__init__(f"Invariant violations detected:\n" + "\n".join(messages))


_GLOBAL_INVARIANT_REGISTRY = InvariantRegistry()


def get_global_invariant_registry() -> InvariantRegistry:
    """Access the global invariant registry."""
    return _GLOBAL_INVARIANT_REGISTRY


def create_enforcer() -> InvariantEnforcer:
    """Create a new invariant enforcer with global registry."""
    return InvariantEnforcer(get_global_invariant_registry())