"""
Time & Event Window Authority

This module is the single source of truth for window definitions.
It defines membership boundaries, never aggregation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Dict, List, Optional
import hashlib


class WindowType(Enum):
    TUMBLING_TIME = "tumbling_time"
    SLIDING_TIME = "sliding_time"
    SESSION = "session"
    FIXED_EVENT = "fixed_event"
    LIFETIME = "lifetime"


@dataclass(frozen=True)
class WindowDefinition:
    """Immutable, versioned window specification."""
    
    window_type: WindowType
    version: str
    alignment_epoch_ms: int = 0
    allowed_lateness_ms: int = 0
    window_size_ms: Optional[int] = None
    slide_ms: Optional[int] = None
    session_gap_ms: Optional[int] = None
    event_count: Optional[int] = None
    
    def __post_init__(self) -> None:
        if self.window_type == WindowType.TUMBLING_TIME:
            if self.window_size_ms is None or self.window_size_ms <= 0:
                raise ValueError("Tumbling window requires positive window_size_ms")
            if self.slide_ms is not None:
                raise ValueError("Tumbling window must not specify slide_ms")
                
        elif self.window_type == WindowType.SLIDING_TIME:
            if self.window_size_ms is None or self.window_size_ms <= 0:
                raise ValueError("Sliding window requires positive window_size_ms")
            if self.slide_ms is None or self.slide_ms <= 0:
                raise ValueError("Sliding window requires positive slide_ms")
            if self.slide_ms > self.window_size_ms:
                raise ValueError("Slide must not exceed window size")
                
        elif self.window_type == WindowType.SESSION:
            if self.session_gap_ms is None or self.session_gap_ms <= 0:
                raise ValueError("Session window requires positive session_gap_ms")
                
        elif self.window_type == WindowType.FIXED_EVENT:
            if self.event_count is None or self.event_count <= 0:
                raise ValueError("Fixed event window requires positive event_count")
                
        elif self.window_type == WindowType.LIFETIME:
            if any([self.window_size_ms, self.slide_ms, self.session_gap_ms, self.event_count]):
                raise ValueError("Lifetime window takes no size parameters")
    
    def definition_hash(self) -> str:
        """Stable hash for window definition identity."""
        components = [
            self.window_type.value,
            self.version,
            str(self.alignment_epoch_ms),
            str(self.window_size_ms or ""),
            str(self.slide_ms or ""),
            str(self.session_gap_ms or ""),
            str(self.event_count or ""),
        ]
        content = "|".join(components)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class WindowAssignment:
    """Pure value object representing window membership."""
    
    window_id: str
    window_type: WindowType
    window_start_ms: int
    window_end_ms: int
    event_time_ms: int
    alignment_epoch_ms: int
    is_late: bool = False
    
    def __post_init__(self) -> None:
        if self.window_start_ms < 0:
            raise ValueError("Window start cannot be negative")
        if self.window_end_ms < 0:
            raise ValueError("Window end cannot be negative")
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError("Window end must be after start")
        if self.event_time_ms < 0:
            raise ValueError("Event time cannot be negative")


@dataclass(frozen=True)
class WindowPolicy:
    """Global immutable policy constraints."""
    
    allowed_window_types: frozenset[WindowType] = field(
        default_factory=lambda: frozenset(WindowType)
    )
    max_window_span_ms: int = 86400000 * 7  # 7 days
    max_allowed_lateness_ms: int = 3600000  # 1 hour
    replay_compatibility_required: bool = True
    min_window_size_ms: int = 1000  # 1 second
    max_session_gap_ms: int = 3600000  # 1 hour
    
    def validate(self, window_def: WindowDefinition) -> None:
        """Enforce policy constraints. Fail hard on violation."""
        
        if window_def.window_type not in self.allowed_window_types:
            raise ValueError(f"Window type {window_def.window_type} not allowed by policy")
        
        if window_def.allowed_lateness_ms > self.max_allowed_lateness_ms:
            raise ValueError(
                f"Allowed lateness {window_def.allowed_lateness_ms}ms "
                f"exceeds policy max {self.max_allowed_lateness_ms}ms"
            )
        
        if window_def.window_size_ms is not None:
            if window_def.window_size_ms > self.max_window_span_ms:
                raise ValueError(
                    f"Window size {window_def.window_size_ms}ms "
                    f"exceeds policy max {self.max_window_span_ms}ms"
                )
            if window_def.window_size_ms < self.min_window_size_ms:
                raise ValueError(
                    f"Window size {window_def.window_size_ms}ms "
                    f"below policy min {self.min_window_size_ms}ms"
                )
        
        if window_def.session_gap_ms is not None:
            if window_def.session_gap_ms > self.max_session_gap_ms:
                raise ValueError(
                    f"Session gap {window_def.session_gap_ms}ms "
                    f"exceeds policy max {self.max_session_gap_ms}ms"
                )


class CanonicalEvent(Protocol):
    """Protocol defining minimal event contract."""
    
    @property
    def event_time_ms(self) -> int:
        """Canonical event timestamp in milliseconds."""
        ...
    
    @property
    def event_id(self) -> str:
        """Globally unique event identifier."""
        ...


class AggregationContext(Protocol):
    """Protocol defining minimal context contract."""
    
    @property
    def context_id(self) -> str:
        """Unique aggregation context identifier."""
        ...


class WindowInvariants:
    """Enforces absolute window invariants."""
    
    @staticmethod
    def validate_assignment(assignment: WindowAssignment) -> None:
        """Validate window assignment invariants."""
        
        if assignment.window_start_ms >= assignment.window_end_ms:
            raise ValueError("Window start must be before end")
        
        if assignment.window_end_ms - assignment.window_start_ms == 0:
            raise ValueError("Zero-length windows forbidden")
        
        if assignment.event_time_ms < 0:
            raise ValueError("Time travel forbidden")
        
        if assignment.alignment_epoch_ms < 0:
            raise ValueError("Negative alignment epoch forbidden")
    
    @staticmethod
    def validate_no_overlap(
        assignments: List[WindowAssignment],
        allow_overlap: bool = False
    ) -> None:
        """Validate window boundary constraints."""
        
        if allow_overlap:
            return
        
        sorted_windows = sorted(assignments, key=lambda w: w.window_start_ms)
        
        for i in range(len(sorted_windows) - 1):
            current = sorted_windows[i]
            next_window = sorted_windows[i + 1]
            
            if current.window_end_ms > next_window.window_start_ms:
                raise ValueError(
                    f"Overlapping windows forbidden: "
                    f"[{current.window_start_ms}, {current.window_end_ms}) "
                    f"overlaps [{next_window.window_start_ms}, {next_window.window_end_ms})"
                )


class WindowResolver:
    """Pure deterministic window classifier."""
    
    def __init__(self, policy: WindowPolicy):
        self._policy = policy
    
    def resolve(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: AggregationContext
    ) -> List[WindowAssignment]:
        """
        Classify event into windows. Pure function.
        Same inputs -> same outputs, forever.
        """
        
        self._policy.validate(window_def)
        
        if event.event_time_ms < 0:
            raise ValueError("Invalid event time")
        
        if window_def.window_type == WindowType.TUMBLING_TIME:
            return self._resolve_tumbling(event, window_def, context)
        
        elif window_def.window_type == WindowType.SLIDING_TIME:
            return self._resolve_sliding(event, window_def, context)
        
        elif window_def.window_type == WindowType.SESSION:
            return self._resolve_session(event, window_def, context)
        
        elif window_def.window_type == WindowType.FIXED_EVENT:
            return self._resolve_fixed_event(event, window_def, context)
        
        elif window_def.window_type == WindowType.LIFETIME:
            return self._resolve_lifetime(event, window_def, context)
        
        raise ValueError(f"Unknown window type: {window_def.window_type}")
    
    def _resolve_tumbling(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: AggregationContext
    ) -> List[WindowAssignment]:
        """Resolve tumbling time window."""
        
        assert window_def.window_size_ms is not None
        
        offset_time = event.event_time_ms - window_def.alignment_epoch_ms
        window_index = offset_time // window_def.window_size_ms
        
        window_start = (window_index * window_def.window_size_ms) + window_def.alignment_epoch_ms
        window_end = window_start + window_def.window_size_ms
        
        is_late = (event.event_time_ms < window_start - window_def.allowed_lateness_ms)
        
        window_id = self._generate_window_id(
            window_def,
            context,
            window_start,
            window_end
        )
        
        assignment = WindowAssignment(
            window_id=window_id,
            window_type=window_def.window_type,
            window_start_ms=window_start,
            window_end_ms=window_end,
            event_time_ms=event.event_time_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            is_late=is_late
        )
        
        WindowInvariants.validate_assignment(assignment)
        
        return [assignment]
    
    def _resolve_sliding(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: AggregationContext
    ) -> List[WindowAssignment]:
        """Resolve sliding time window."""
        
        assert window_def.window_size_ms is not None
        assert window_def.slide_ms is not None
        
        assignments: List[WindowAssignment] = []
        
        offset_time = event.event_time_ms - window_def.alignment_epoch_ms
        
        first_window_start_index = (offset_time - window_def.window_size_ms + window_def.slide_ms) // window_def.slide_ms
        last_window_start_index = offset_time // window_def.slide_ms
        
        for window_index in range(first_window_start_index, last_window_start_index + 1):
            window_start = (window_index * window_def.slide_ms) + window_def.alignment_epoch_ms
            window_end = window_start + window_def.window_size_ms
            
            if event.event_time_ms < window_start or event.event_time_ms >= window_end:
                continue
            
            is_late = (event.event_time_ms < window_start - window_def.allowed_lateness_ms)
            
            window_id = self._generate_window_id(
                window_def,
                context,
                window_start,
                window_end
            )
            
            assignment = WindowAssignment(
                window_id=window_id,
                window_type=window_def.window_type,
                window_start_ms=window_start,
                window_end_ms=window_end,
                event_time_ms=event.event_time_ms,
                alignment_epoch_ms=window_def.alignment_epoch_ms,
                is_late=is_late
            )
            
            WindowInvariants.validate_assignment(assignment)
            assignments.append(assignment)
        
        return assignments
    
    def _resolve_session(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: AggregationContext
    ) -> List[WindowAssignment]:
        """
        Resolve session window.
        Note: Session windows require stateful merging downstream.
        This assigns preliminary boundaries only.
        """
        
        assert window_def.session_gap_ms is not None
        
        window_start = event.event_time_ms
        window_end = event.event_time_ms + window_def.session_gap_ms
        
        window_id = self._generate_window_id(
            window_def,
            context,
            window_start,
            window_end,
            suffix=event.event_id
        )
        
        assignment = WindowAssignment(
            window_id=window_id,
            window_type=window_def.window_type,
            window_start_ms=window_start,
            window_end_ms=window_end,
            event_time_ms=event.event_time_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            is_late=False
        )
        
        WindowInvariants.validate_assignment(assignment)
        
        return [assignment]
    
    def _resolve_fixed_event(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: AggregationContext
    ) -> List[WindowAssignment]:
        """
        Resolve fixed event count window.
        Returns synthetic time boundaries.
        """
        
        assert window_def.event_count is not None
        
        window_start = 0
        window_end = window_def.event_count
        
        window_id = self._generate_window_id(
            window_def,
            context,
            window_start,
            window_end
        )
        
        assignment = WindowAssignment(
            window_id=window_id,
            window_type=window_def.window_type,
            window_start_ms=window_start,
            window_end_ms=window_end,
            event_time_ms=event.event_time_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            is_late=False
        )
        
        return [assignment]
    
    def _resolve_lifetime(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: AggregationContext
    ) -> List[WindowAssignment]:
        """Resolve lifetime window (unbounded)."""
        
        window_start = 0
        window_end = (1 << 53) - 1  # Max safe integer in ms
        
        window_id = self._generate_window_id(
            window_def,
            context,
            window_start,
            window_end
        )
        
        assignment = WindowAssignment(
            window_id=window_id,
            window_type=window_def.window_type,
            window_start_ms=window_start,
            window_end_ms=window_end,
            event_time_ms=event.event_time_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            is_late=False
        )
        
        return [assignment]
    
    def _generate_window_id(
        self,
        window_def: WindowDefinition,
        context: AggregationContext,
        window_start: int,
        window_end: int,
        suffix: str = ""
    ) -> str:
        """Generate deterministic, globally stable window ID."""
        
        components = [
            window_def.definition_hash(),
            context.context_id,
            str(window_start),
            str(window_end),
            suffix
        ]
        
        content = "|".join(components)
        hash_value = hashlib.sha256(content.encode()).hexdigest()[:16]
        
        return f"{window_def.window_type.value}:{hash_value}"


class WindowRegistry:
    """Central registry of known window definitions."""
    
    def __init__(self):
        self._definitions: Dict[str, WindowDefinition] = {}
        self._policy = WindowPolicy()
    
    def register(self, name: str, window_def: WindowDefinition) -> None:
        """Register a window definition. Immutable after registration."""
        
        if name in self._definitions:
            existing = self._definitions[name]
            if existing != window_def:
                raise ValueError(
                    f"Window '{name}' already registered with different definition"
                )
            return
        
        self._policy.validate(window_def)
        self._definitions[name] = window_def
    
    def get(self, name: str) -> WindowDefinition:
        """Retrieve registered window definition."""
        
        if name not in self._definitions:
            raise KeyError(f"Window '{name}' not registered")
        
        return self._definitions[name]
    
    def list_registered(self) -> List[str]:
        """List all registered window names."""
        return sorted(self._definitions.keys())
    
    def set_policy(self, policy: WindowPolicy) -> None:
        """Update global policy. Validates all existing definitions."""
        
        for name, window_def in self._definitions.items():
            try:
                policy.validate(window_def)
            except ValueError as e:
                raise ValueError(
                    f"Policy change would invalidate window '{name}': {e}"
                )
        
        self._policy = policy


_GLOBAL_REGISTRY = WindowRegistry()


def get_global_registry() -> WindowRegistry:
    """Access the global window registry."""
    return _GLOBAL_REGISTRY