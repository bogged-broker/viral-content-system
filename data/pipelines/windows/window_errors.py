"""
/data/pipelines/windows/window_errors.py

Window Error Taxonomy — Typed Exceptions for Invariant Violations

This module defines the only allowed error types for window invariant violations.
All violations MUST raise typed exceptions from this module.

No generic exceptions.
No string-based errors.
"""

from __future__ import annotations


class WindowError(Exception):
    """
    Base exception for all window-related errors.
    
    All window errors inherit from this class.
    """
    pass


class InvalidWindowDefinitionError(WindowError):
    """
    Raised when a WindowDefinition violates logical consistency.
    
    Examples:
    - Sliding window without slide_ms
    - Session window with window_size_ms
    - FixedEventWindow with time parameters
    - LifetimeWindow with bounded end
    - Allowed lateness > window span
    - Session gap == 0
    """
    
    def __init__(
        self,
        message: str,
        window_type: str | None = None,
        violation_details: str | None = None,
    ):
        self.window_type = window_type
        self.violation_details = violation_details
        super().__init__(message)


class TemporalViolationError(WindowError):
    """
    Raised when temporal invariants are violated.
    
    Examples:
    - window_start_ms >= window_end_ms
    - Zero-length windows
    - Negative timestamps
    - Overflow / NaN timestamps
    - Event time outside window bounds
    - Alignment epoch < 0
    """
    
    def __init__(
        self,
        message: str,
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
        event_time_ms: int | None = None,
        violation_type: str | None = None,
    ):
        self.window_start_ms = window_start_ms
        self.window_end_ms = window_end_ms
        self.event_time_ms = event_time_ms
        self.violation_type = violation_type
        super().__init__(message)


class AlignmentViolationError(WindowError):
    """
    Raised when window boundaries violate alignment requirements.
    
    Examples:
    - Tumbling windows not aligned to alignment_epoch_ms
    - Sliding windows where slide does not divide window size
    - Session windows with non-event-time-derived boundaries
    - Lifetime windows with multiple boundaries
    """
    
    def __init__(
        self,
        message: str,
        window_type: str | None = None,
        alignment_epoch_ms: int | None = None,
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
        violation_details: str | None = None,
    ):
        self.window_type = window_type
        self.alignment_epoch_ms = alignment_epoch_ms
        self.window_start_ms = window_start_ms
        self.window_end_ms = window_end_ms
        self.violation_details = violation_details
        super().__init__(message)


class DeterminismViolationError(WindowError):
    """
    Raised when a construct could resolve differently on replay.
    
    Examples:
    - Dependency on ingestion order
    - Dependency on processing time
    - Environment-derived defaults
    - Mutable structures
    - Floating-point time math
    - Unordered iteration affecting output
    """
    
    def __init__(
        self,
        message: str,
        violation_type: str | None = None,
        violation_details: str | None = None,
    ):
        self.violation_type = violation_type
        self.violation_details = violation_details
        super().__init__(message)


class PolicyViolationError(WindowError):
    """
    Raised when a window definition violates policy constraints.
    
    Examples:
    - Window type not in allowed_window_types
    - Window span exceeds max_window_span_ms
    - Allowed lateness exceeds max_allowed_lateness_ms
    - Window span below min_window_span_ms
    """
    
    def __init__(
        self,
        message: str,
        policy_constraint: str | None = None,
        window_value: int | str | None = None,
        policy_limit: int | str | None = None,
    ):
        self.policy_constraint = policy_constraint
        self.window_value = window_value
        self.policy_limit = policy_limit
        super().__init__(message)
