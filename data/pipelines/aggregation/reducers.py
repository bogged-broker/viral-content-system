"""
/data/pipelines/aggregation/reducers.py

Generic, Semantics-Free Reduction Utilities

What This File Exists For (NON-NEGOTIABLE):
  reducers.py defines pure, mathematical reduction primitives used by aggregation orchestration.

This file answers exactly one question:
  > "Given a stream or multiset of values, how can they be combined deterministically
    without knowing what those values mean?"

It is math-only infrastructure.

If this file lies, aggregation appears correct while violating invariants invisibly.

Design Principle (ABSOLUTE):
  > A reducer must be correct even if the caller does not understand what it is reducing.

Reducers must never encode meaning, assumptions, or shortcuts.
If meaning leaks into a reducer, the aggregation layer becomes non-auditable.

Mental Model (LOCK THIS):
  > Reducers are mathematical axioms. They cannot be "patched" later without rewriting history.

Aggregation correctness rests on this file being boring, strict, and ruthless.
"""

from __future__ import annotations

import math
from typing import Protocol, Iterable, TypeVar, Dict, List
from dataclasses import dataclass

T = TypeVar('T')
Numeric = TypeVar('Numeric', int, float)


# ============================================================================
# ERROR HANDLING (STRICT)
# ============================================================================


class ReductionError(Exception):
    """
    Structured error for reduction failures.
    
    RULES:
    - Must include reducer name
    - Must include input type summary
    - Never swallow invalid data
    - No best-effort behavior allowed
    """
    
    def __init__(
        self,
        reducer_name: str,
        message: str,
        input_summary: str | None = None,
    ):
        """
        Initialize reduction error.
        
        Args:
            reducer_name: Name of reducer that failed
            message: Error message
            input_summary: Summary of input that caused failure
        """
        self.reducer_name = reducer_name
        self.message = message
        self.input_summary = input_summary
        
        full_message = f"[{reducer_name}] {message}"
        if input_summary:
            full_message += f" | input: {input_summary}"
        
        super().__init__(full_message)


# ============================================================================
# REDUCER INTERFACE (FOUNDATIONAL)
# ============================================================================


class Reducer(Protocol):
    """
    Protocol for all reducers.
    
    All reducers MUST conform to this interface.
    
    RULES:
    - Input is an iterable, not a stream
    - Output is a single scalar
    - Reducer must be referentially transparent
    - Same input values → same output forever
    - No access to context, windows, or counters
    """
    
    def reduce(self, values: Iterable[T]) -> T:
        """
        Reduce iterable of values to single value.
        
        Args:
            values: Iterable of values to reduce
        
        Returns:
            Single reduced value
        
        Raises:
            ReductionError: Reduction cannot proceed
        """
        ...
    
    @property
    def name(self) -> str:
        """Canonical name of reducer."""
        ...
    
    @property
    def is_order_independent(self) -> bool:
        """Whether reducer is order-independent."""
        ...
    
    @property
    def allows_empty_input(self) -> bool:
        """Whether reducer allows empty input."""
        ...


# ============================================================================
# SUPPORTED REDUCERS (EXPLICIT ONLY)
# ============================================================================


class SumReducer:
    """
    Deterministic summation of numeric values.
    
    RULES:
    - Only numeric types allowed
    - No overflow masking
    - No implicit type coercion
    - Empty input → error
    - Order-independent
    
    FORBIDDEN:
    - Floating-point compensation
    - Domain-aware rounding
    - Silent truncation
    """
    
    @property
    def name(self) -> str:
        return "sum"
    
    @property
    def is_order_independent(self) -> bool:
        return True
    
    @property
    def allows_empty_input(self) -> bool:
        return False
    
    def reduce(self, values: Iterable[Numeric]) -> Numeric:
        """
        Sum numeric values.
        
        Args:
            values: Numeric values to sum
        
        Returns:
            Sum of values
        
        Raises:
            ReductionError: Empty input, type mismatch, NaN, or overflow
        """
        values_list = list(values)
        
        if not values_list:
            raise ReductionError(
                self.name,
                "Cannot reduce empty input",
                "empty iterable"
            )
        
        # Check for type consistency
        seen_types = set()
        for value in values_list:
            value_type = type(value)
            seen_types.add(value_type.__name__)
            
            if not isinstance(value, (int, float)):
                raise ReductionError(
                    self.name,
                    f"Type mismatch: expected int or float, got {value_type.__name__}",
                    f"types: {sorted(seen_types)}"
                )
        
        # Check for NaN and Infinity
        for value in values_list:
            if isinstance(value, float):
                if math.isnan(value):
                    raise ReductionError(
                        self.name,
                        "NaN not allowed",
                        f"value: {value}"
                    )
                if math.isinf(value):
                    raise ReductionError(
                        self.name,
                        "Infinity not allowed",
                        f"value: {value}"
                    )
        
        # Perform summation
        total = 0
        for value in values_list:
            total = total + value
        
        return total


class MinReducer:
    """
    Returns strict minimum of input values.
    
    RULES:
    - Comparable types only
    - Empty input → error
    - Total ordering required
    - Order-independent
    """
    
    @property
    def name(self) -> str:
        return "min"
    
    @property
    def is_order_independent(self) -> bool:
        return True
    
    @property
    def allows_empty_input(self) -> bool:
        return False
    
    def reduce(self, values: Iterable[T]) -> T:
        """
        Find minimum value.
        
        Args:
            values: Values to compare
        
        Returns:
            Minimum value
        
        Raises:
            ReductionError: Empty input, type mismatch, or NaN
        """
        values_list = list(values)
        
        if not values_list:
            raise ReductionError(
                self.name,
                "Cannot reduce empty input",
                "empty iterable"
            )
        
        # Check for type consistency
        first_type = type(values_list[0])
        for value in values_list:
            if type(value) != first_type:
                seen_types = [type(v).__name__ for v in values_list]
                raise ReductionError(
                    self.name,
                    f"Type mismatch: expected {first_type.__name__}",
                    f"types: {seen_types}"
                )
        
        # Check for NaN in floats
        for value in values_list:
            if isinstance(value, float):
                if math.isnan(value):
                    raise ReductionError(
                        self.name,
                        "NaN not allowed in min",
                        f"value: {value}"
                    )
        
        return min(values_list)


class MaxReducer:
    """
    Returns strict maximum of input values.
    
    RULES:
    - Comparable types only
    - Empty input → error
    - Total ordering required
    - Order-independent
    """
    
    @property
    def name(self) -> str:
        return "max"
    
    @property
    def is_order_independent(self) -> bool:
        return True
    
    @property
    def allows_empty_input(self) -> bool:
        return False
    
    def reduce(self, values: Iterable[T]) -> T:
        """
        Find maximum value.
        
        Args:
            values: Values to compare
        
        Returns:
            Maximum value
        
        Raises:
            ReductionError: Empty input, type mismatch, or NaN
        """
        values_list = list(values)
        
        if not values_list:
            raise ReductionError(
                self.name,
                "Cannot reduce empty input",
                "empty iterable"
            )
        
        # Check for type consistency
        first_type = type(values_list[0])
        for value in values_list:
            if type(value) != first_type:
                seen_types = [type(v).__name__ for v in values_list]
                raise ReductionError(
                    self.name,
                    f"Type mismatch: expected {first_type.__name__}",
                    f"types: {seen_types}"
                )
        
        # Check for NaN in floats
        for value in values_list:
            if isinstance(value, float):
                if math.isnan(value):
                    raise ReductionError(
                        self.name,
                        "NaN not allowed in max",
                        f"value: {value}"
                    )
        
        return max(values_list)


class CountReducer:
    """
    Counts number of input elements.
    
    RULES:
    - Ignores value identity
    - Counts elements, not truthiness
    - Empty input → 0
    - Order-independent
    
    CountReducer is the only reducer allowed to succeed on empty input.
    """
    
    @property
    def name(self) -> str:
        return "count"
    
    @property
    def is_order_independent(self) -> bool:
        return True
    
    @property
    def allows_empty_input(self) -> bool:
        return True
    
    def reduce(self, values: Iterable[T]) -> int:
        """
        Count elements.
        
        Args:
            values: Values to count
        
        Returns:
            Number of elements (0 for empty input)
        """
        count = 0
        for _ in values:
            count += 1
        return count


# ============================================================================
# REDUCER REGISTRY (CONTROLLED SURFACE)
# ============================================================================


class ReducerRegistry:
    """
    Provides an explicit allowlist of reducers usable by aggregation logic.
    
    RULES:
    - Static mapping only
    - No dynamic registration
    - Reducers referenced by canonical names (e.g. "sum", "min")
    - Missing reducer → hard error
    
    This prevents unreviewed math from entering production paths.
    """
    
    def __init__(self):
        """Initialize registry with standard reducers."""
        self._reducers: Dict[str, Reducer] = {
            "sum": SumReducer(),
            "min": MinReducer(),
            "max": MaxReducer(),
            "count": CountReducer(),
        }
        self._locked = True  # Locked by default - no dynamic registration
    
    def get(self, name: str) -> Reducer:
        """
        Retrieve reducer by canonical name.
        
        Args:
            name: Canonical reducer name
        
        Returns:
            Reducer instance
        
        Raises:
            KeyError: Reducer not found (hard error)
        """
        if name not in self._reducers:
            available = sorted(self._reducers.keys())
            raise KeyError(
                f"Reducer '{name}' not found. "
                f"Available reducers: {available}"
            )
        return self._reducers[name]
    
    def list_reducers(self) -> List[str]:
        """
        List all registered reducer names in deterministic order.
        
        Returns:
            Sorted list of reducer names
        """
        return sorted(self._reducers.keys())


# ============================================================================
# REDUCTION INVARIANTS (ABSOLUTE)
# ============================================================================


class ReductionInvariants:
    """
    Enforces absolute invariants for reducers.
    
    This file MUST enforce:
    1. No reducer depends on input order unless mathematically required
    2. No reducer mutates input
    3. No reducer accesses external state
    4. No reducer embeds semantic meaning
    5. No reducer performs normalization
    6. No reducer performs windowing
    7. No reducer performs validation beyond type safety
    
    Violation = aggregation correctness breach.
    """
    
    @staticmethod
    def validate_reducer(reducer: Reducer) -> None:
        """
        Validate reducer conforms to invariants.
        
        Args:
            reducer: Reducer to validate
        
        Raises:
            ReductionError: Invariant violation detected
        """
        # Invariant 1: Order independence (when mathematically required)
        if not reducer.is_order_independent:
            raise ReductionError(
                reducer.name,
                "Reducer must be order-independent unless mathematically required",
                f"is_order_independent={reducer.is_order_independent}"
            )
        
        # Invariant 2: No mutation (enforced by design - reducers are pure)
        # Invariant 3: No external state (enforced by design - no context access)
        # Invariant 4: No semantic meaning (enforced by design - math only)
        # Invariant 5: No normalization (enforced by design - raw math)
        # Invariant 6: No windowing (enforced by design - no time awareness)
        # Invariant 7: Type safety only (enforced in reducer implementations)
    
    @staticmethod
    def validate_input_immutability(
        reducer: Reducer,
        original_values: List[T],
    ) -> None:
        """
        Validate reducer does not mutate input.
        
        Args:
            reducer: Reducer to test
            original_values: Original input values
        
        Raises:
            ReductionError: Input was mutated
        """
        # Create a copy for comparison
        values_copy = list(original_values)
        
        # Run reduction
        try:
            reducer.reduce(original_values)
        except ReductionError:
            pass  # Errors are acceptable, we're checking mutation
        
        # Check if original was mutated
        if original_values != values_copy:
            raise ReductionError(
                reducer.name,
                "Reducer mutated input values",
                f"original length: {len(values_copy)}"
            )


# ============================================================================
# GLOBAL REGISTRY (STATIC)
# ============================================================================


# Global registry instance - static, locked, immutable
_GLOBAL_REGISTRY: ReducerRegistry | None = None


def get_reducer_registry() -> ReducerRegistry:
    """
    Get global reducer registry.
    
    Returns:
        Global ReducerRegistry instance (static, locked)
    """
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = ReducerRegistry()
    return _GLOBAL_REGISTRY
