"""
Computation layer public contract surface.

This module intentionally exposes ONLY declarative, immutable computation
primitives. Importing this package MUST have zero side effects.
"""

from .computation_spec import ComputationSpec
from .computation_context import ComputationContext
from .computation_spec_errors import ComputationDefinitionError
from .computation_errors import ComputationInvariantViolation

__all__ = [
    "ComputationSpec",
    "ComputationContext",
    "ComputationDefinitionError",
    "ComputationInvariantViolation",
]
