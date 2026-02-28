"""
/data/pipelines/computation/computation_spec_errors.py

Computation Specification Validation Errors

AUTHORITY: Defines errors for invalid computation specifications
PRINCIPLE: A computation that cannot be properly defined cannot be registered
BEHAVIOR: Fail at definition-time, before registration

This file handles definition-time validation errors, separate from
execution-time computation errors.

These errors occur when:
- Spec is malformed or incomplete
- Required fields are missing
- Constraints are contradictory
- Serialization fails

This is NOT execution-time. This is specification-time.
"""

from __future__ import annotations

from typing import Any, Optional, Dict


class ComputationDefinitionError(RuntimeError):
    """
    Raised when computation specification is invalid or incomplete.
    
    Examples:
    - Missing required fields
    - Invalid parameter definitions
    - Contradictory constraints
    - Malformed computation spec
    
    This is a definition-time error, not runtime.
    """
    
    def __init__(self, message: str, computation_name: Optional[str] = None):
        self.computation_name = computation_name
        prefix = f"[{computation_name}] " if computation_name else ""
        super().__init__(f"{prefix}Computation definition invalid: {message}")
    
    def to_dict(self) -> dict[str, Any]:
        """Serialize error for audit trails."""
        d = {
            'error_type': self.__class__.__name__,
            'message': str(self),
        }
        if self.computation_name:
            d['computation_name'] = self.computation_name
        return d
