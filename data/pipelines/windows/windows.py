"""
/data/pipelines/windows/windows.py

Window Semantics & Event-Time Boundary Authority
(Zero Math, Zero State, Zero Heuristics)

What This File Exists For (NON-NEGOTIABLE):
  windows.py is the single, global authority that defines:
  > How events are grouped into windows, purely by time or logical boundary, under deterministic rules.

This file answers exactly one question:
  > "Given a canonical event and a frozen execution context, which window does this event belong to?"

Nothing more.
Nothing less.

If this file is wrong, every downstream computation can be numerically correct and semantically false.
That is an unacceptable failure mode.

Design Principle (LOCKED):
  > A window is a deterministic boundary, not a heuristic.

Given:
  - the same canonical event
  - the same event-time
  - the same window definition
  - the same execution context

The assigned window MUST be identical:
  - across machines
  - across processes
  - across deployments
  - across years

Forever.

Mental Model (DO NOT BREAK):
  > Aggregation answers "how much."
  > Windows answer "of what, exactly."

If window semantics are wrong, nothing else matters.
"""

from __future__ import annotations

from typing import List, Optional, Dict, Any, FrozenSet, Tuple

from .window_models import (
    WindowDefinition,
    WindowPolicy,
    WindowAssignment,
    CanonicalEvent,
    WindowType,
    WindowAssignmentStatus,
    TimestampExtractionStrategy,
)
from .window_identity import (
    WindowIdentityFactory,
    WindowIdentityMaterial,
    WindowIdentity,
)
from .window_invariants import (
    enforce_window_definition_invariants,
    enforce_window_assignment_invariants,
    enforce_window_identity_invariants,
)
from .window_errors import (
    InvalidWindowDefinitionError,
    TemporalViolationError,
    AlignmentViolationError,
)


# ============================================================================
# EXECUTION CONTEXT (FROZEN)
# ============================================================================


class ExecutionContext:
    """
    Frozen execution context for window resolution.
    
    RULES:
    - Immutable
    - Read-only
    - No system clock access
    - Deterministic
    """
    
    def __init__(
        self,
        processing_watermark_ms: Optional[int] = None,
        context_fingerprint: Optional[str] = None,
        replay_mode: bool = False,
    ):
        self.processing_watermark_ms = processing_watermark_ms
        self.context_fingerprint = context_fingerprint
        self.replay_mode = replay_mode
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ExecutionContext):
            return False
        return (
            self.processing_watermark_ms == other.processing_watermark_ms
            and self.context_fingerprint == other.context_fingerprint
            and self.replay_mode == other.replay_mode
        )
    
    def __hash__(self) -> int:
        return hash((
            self.processing_watermark_ms,
            self.context_fingerprint,
            self.replay_mode,
        ))


# ============================================================================
# WINDOW SEMANTICS (EXPLICIT PER TYPE)
# ============================================================================


class WindowSemantics:
    """
    Explicit semantics for each window type.
    
    Each window type has deterministic boundary computation rules.
    No heuristics. No state. No system clock.
    """
    
    @staticmethod
    def compute_tumbling_boundaries(
        event_time_ms: int,
        window_size_ms: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute tumbling window boundaries.
        
        Tumbling windows: Fixed, non-overlapping time buckets.
        
        Rules:
        - Window size divides evenly from alignment epoch
        - No overlap between windows
        - Deterministic boundary computation
        
        Args:
            event_time_ms: Event time in UTC epoch milliseconds
            window_size_ms: Window size in milliseconds
            alignment_epoch_ms: Alignment epoch in UTC epoch milliseconds
        
        Returns:
            (window_start_ms, window_end_ms)
        """
        offset_from_epoch = event_time_ms - alignment_epoch_ms
        
        # Handle negative offsets (events before alignment epoch)
        if offset_from_epoch < 0:
            # Window number is negative, compute start/end accordingly
            window_number = (offset_from_epoch // window_size_ms) - 1
        else:
            window_number = offset_from_epoch // window_size_ms
        
        window_start_ms = alignment_epoch_ms + (window_number * window_size_ms)
        window_end_ms = window_start_ms + window_size_ms
        
        return (window_start_ms, window_end_ms)
    
    @staticmethod
    def compute_sliding_boundaries(
        event_time_ms: int,
        window_size_ms: int,
        slide_ms: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute primary sliding window boundaries.
        
        Sliding windows: Fixed size with deterministic overlap.
        
        Rules:
        - Slide divides window size
        - Window start aligns to slide from alignment epoch
        - Event belongs to the window that contains it
        
        Args:
            event_time_ms: Event time in UTC epoch milliseconds
            window_size_ms: Window size in milliseconds
            slide_ms: Slide interval in milliseconds
            alignment_epoch_ms: Alignment epoch in UTC epoch milliseconds
        
        Returns:
            (window_start_ms, window_end_ms) for primary window
        """
        offset_from_epoch = event_time_ms - alignment_epoch_ms
        
        # Handle negative offsets
        if offset_from_epoch < 0:
            window_number = (offset_from_epoch // slide_ms) - 1
        else:
            window_number = offset_from_epoch // slide_ms
        
        window_start_ms = alignment_epoch_ms + (window_number * slide_ms)
        
        # Find the window that contains the event
        # The event belongs to the window that starts at or before the event time
        # and ends after the event time
        while window_start_ms + window_size_ms <= event_time_ms:
            window_start_ms += slide_ms
        
        window_end_ms = window_start_ms + window_size_ms
        
        return (window_start_ms, window_end_ms)
    
    @staticmethod
    def compute_all_sliding_windows(
        event_time_ms: int,
        window_size_ms: int,
        slide_ms: int,
        alignment_epoch_ms: int,
    ) -> List[tuple[int, int]]:
        """
        Compute all sliding windows that contain the event.
        
        For sliding windows, an event can belong to multiple overlapping windows.
        
        Returns:
            List of (window_start_ms, window_end_ms) tuples
        """
        windows = []
        
        # Find the earliest window that could contain this event
        offset_from_epoch = event_time_ms - alignment_epoch_ms
        earliest_window_start = alignment_epoch_ms
        
        if offset_from_epoch >= 0:
            # Find earliest window start that could contain this event
            earliest_window_number = max(0, (offset_from_epoch - window_size_ms + 1) // slide_ms)
            earliest_window_start = alignment_epoch_ms + (earliest_window_number * slide_ms)
        else:
            # Event before alignment epoch
            earliest_window_number = (offset_from_epoch - window_size_ms + 1) // slide_ms - 1
            earliest_window_start = alignment_epoch_ms + (earliest_window_number * slide_ms)
        
        # Collect all windows that contain the event
        window_start_ms = earliest_window_start
        while window_start_ms <= event_time_ms:
            window_end_ms = window_start_ms + window_size_ms
            
            if window_start_ms <= event_time_ms < window_end_ms:
                windows.append((window_start_ms, window_end_ms))
            
            window_start_ms += slide_ms
        
        return windows
    
    @staticmethod
    def compute_hopping_boundaries(
        event_time_ms: int,
        window_size_ms: int,
        hop_size_ms: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute hopping window boundaries.
        
        Hopping windows: Similar to sliding, but with explicit hop size.
        
        Args:
            event_time_ms: Event time in UTC epoch milliseconds
            window_size_ms: Window size in milliseconds
            hop_size_ms: Hop size in milliseconds
            alignment_epoch_ms: Alignment epoch in UTC epoch milliseconds
        
        Returns:
            (window_start_ms, window_end_ms)
        """
        offset_from_epoch = event_time_ms - alignment_epoch_ms
        
        if offset_from_epoch < 0:
            hop_number = (offset_from_epoch // hop_size_ms) - 1
        else:
            hop_number = offset_from_epoch // hop_size_ms
        
        window_start_ms = alignment_epoch_ms + (hop_number * hop_size_ms)
        window_end_ms = window_start_ms + window_size_ms
        
        return (window_start_ms, window_end_ms)
    
    @staticmethod
    def compute_session_boundaries(
        event_time_ms: int,
        session_gap_ms: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute session window boundaries.
        
        Session windows: Gap-based, transitive, monotonic.
        
        NOTE: For a single event, we compute a deterministic session window.
        In a full implementation, session boundaries would be computed from
        event-time ordering across multiple events. For stateless resolution,
        we compute a session window that contains this event based on gap rules.
        
        The session window for an event is determined by:
        - Session starts at the earliest event time that is within gap of this event
        - Session ends at the latest event time that is within gap of this event
        
        For a single event, the session is the event itself.
        In practice, session boundaries are computed from event streams.
        
        Args:
            event_time_ms: Event time in UTC epoch milliseconds
            session_gap_ms: Session gap in milliseconds
            alignment_epoch_ms: Alignment epoch (not used for sessions, but required for consistency)
        
        Returns:
            (window_start_ms, window_end_ms)
        
        NOTE: This is a simplified session computation. Full session semantics
        require processing multiple events to determine transitive membership.
        """
        # For a single event, the session window is the event time itself
        # In practice, session boundaries would be computed from event streams
        # This is a placeholder that ensures deterministic behavior
        window_start_ms = event_time_ms
        window_end_ms = event_time_ms + session_gap_ms
        
        return (window_start_ms, window_end_ms)
    
    @staticmethod
    def compute_lifetime_boundaries(
        event_time_ms: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute lifetime window boundaries.
        
        Lifetime windows: Single, unbounded logical window.
        
        Args:
            event_time_ms: Event time (not used, but required for consistency)
            alignment_epoch_ms: Window start (typically 0 or first event time)
        
        Returns:
            (window_start_ms, window_end_ms) where end is effectively unbounded
        """
        window_start_ms = alignment_epoch_ms
        # Use maximum safe integer for unbounded end
        window_end_ms = (1 << 53) - 1  # JavaScript safe integer max
        
        return (window_start_ms, window_end_ms)
    
    @staticmethod
    def compute_global_boundaries(
        event_time_ms: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute global window boundaries.
        
        Global windows: Single, unbounded window for all events.
        
        Args:
            event_time_ms: Event time (not used)
            alignment_epoch_ms: Window start (typically 0)
        
        Returns:
            (window_start_ms, window_end_ms) where end is effectively unbounded
        """
        window_start_ms = 0
        window_end_ms = (1 << 53) - 1  # JavaScript safe integer max
        
        return (window_start_ms, window_end_ms)
    
    @staticmethod
    def compute_fixed_event_boundaries(
        event_time_ms: int,
        event_count: int,
        alignment_epoch_ms: int,
    ) -> tuple[int, int]:
        """
        Compute fixed event window boundaries.
        
        Fixed event windows: Event-count-based membership.
        
        NOTE: For count-based windows, boundaries are determined by event ordering.
        Without access to event ordering, we use event_time_ms as a deterministic
        proxy for ordering. In practice, count-based windows require event
        ordering information.
        
        Args:
            event_time_ms: Event time in UTC epoch milliseconds
            event_count: Number of events per window
            alignment_epoch_ms: Alignment epoch
        
        Returns:
            (window_start_ms, window_end_ms)
        
        NOTE: This is a simplified computation. Full count-based semantics
        require event ordering information.
        """
        # For count-based windows, we use event_time_ms as a proxy for ordering
        # The window number is determined by dividing event_time_ms by a derived interval
        # This is a deterministic approximation
        interval_ms = 1000  # 1 second intervals as proxy for event ordering
        window_number = (event_time_ms - alignment_epoch_ms) // interval_ms
        window_number = window_number // event_count
        
        window_start_ms = alignment_epoch_ms + (window_number * event_count * interval_ms)
        window_end_ms = window_start_ms + (event_count * interval_ms)
        
        return (window_start_ms, window_end_ms)


# ============================================================================
# WINDOW RESOLVER (PURE CLASSIFIER)
# ============================================================================


class WindowResolver:
    """
    Pure classifier for window membership.
    
    RESPONSIBILITIES:
    - Compute deterministic boundaries
    - Enforce alignment rules
    - Enforce policy constraints
    - Compute canonical window identity
    
    MUST NOT:
    - Access state
    - Inspect counters
    - Depend on system time
    - Adjust boundaries heuristically
    - Merge or split windows dynamically
    
    Same input → same output. Always.
    """
    
    def __init__(self, policy: WindowPolicy):
        """
        Initialize window resolver with policy.
        
        Args:
            policy: Window policy to enforce
        """
        self.policy = policy
    
    def resolve(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: ExecutionContext,
    ) -> WindowAssignment:
        """
        Resolve window membership for event.
        
        This is the canonical entry point for window resolution.
        
        Args:
            event: Canonical event to classify
            window_def: Window definition
            context: Frozen execution context
        
        Returns:
            WindowAssignment with deterministic boundaries
        
        Raises:
            InvalidWindowDefinitionError: Definition invalid
            TemporalViolationError: Temporal violation
            AlignmentViolationError: Alignment violation
        """
        # Enforce invariants on definition
        enforce_window_definition_invariants(window_def, self.policy)
        
        # Extract event timestamp
        event_time_ms = event.extract_timestamp(
            window_def.timestamp_extraction_strategy,
            window_def.timestamp_field,
        )
        
        # Validate event timestamp
        if event_time_ms < 0:
            return WindowAssignment(
                window_id="",
                window_type=window_def.window_type,
                window_start_ms=0,
                window_end_ms=0,
                event_time_ms=event_time_ms,
                alignment_epoch_ms=window_def.alignment_epoch_ms,
                window_version=window_def.definition_version,
                identity_format_version=window_def.identity_format_version,
                status=WindowAssignmentStatus.INVALID_TIMESTAMP,
                definition_fingerprint=window_def.definition_fingerprint(),
            )
        
        # Compute window boundaries based on window type
        if window_def.window_type == WindowType.TUMBLING_TIME:
            window_start_ms, window_end_ms = WindowSemantics.compute_tumbling_boundaries(
                event_time_ms,
                window_def.window_size_ms,  # type: ignore
                window_def.alignment_epoch_ms,
            )
        
        elif window_def.window_type == WindowType.SLIDING_TIME:
            window_start_ms, window_end_ms = WindowSemantics.compute_sliding_boundaries(
                event_time_ms,
                window_def.window_size_ms,  # type: ignore
                window_def.slide_ms,  # type: ignore
                window_def.alignment_epoch_ms,
            )
        
        elif window_def.window_type == WindowType.HOPPING_TIME:
            window_start_ms, window_end_ms = WindowSemantics.compute_hopping_boundaries(
                event_time_ms,
                window_def.window_size_ms,  # type: ignore
                window_def.hop_size_ms,  # type: ignore
                window_def.alignment_epoch_ms,
            )
        
        elif window_def.window_type == WindowType.SESSION:
            window_start_ms, window_end_ms = WindowSemantics.compute_session_boundaries(
                event_time_ms,
                window_def.session_gap_ms,  # type: ignore
                window_def.alignment_epoch_ms,
            )
        
        elif window_def.window_type == WindowType.LIFETIME:
            window_start_ms, window_end_ms = WindowSemantics.compute_lifetime_boundaries(
                event_time_ms,
                window_def.alignment_epoch_ms,
            )
        
        elif window_def.window_type == WindowType.GLOBAL:
            window_start_ms, window_end_ms = WindowSemantics.compute_global_boundaries(
                event_time_ms,
                window_def.alignment_epoch_ms,
            )
        
        elif window_def.window_type == WindowType.FIXED_EVENT:
            window_start_ms, window_end_ms = WindowSemantics.compute_fixed_event_boundaries(
                event_time_ms,
                window_def.event_count,  # type: ignore
                window_def.alignment_epoch_ms,
            )
        
        else:
            raise InvalidWindowDefinitionError(
                f"Unsupported window type: {window_def.window_type}",
                window_type=window_def.window_type.value,
            )
        
        # Enforce identity invariants
        enforce_window_identity_invariants(
            window_type=window_def.window_type,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            window_size_ms=window_def.window_size_ms,
            slide_ms=window_def.slide_ms,
            allow_unaligned=self.policy.allow_unaligned_windows,
        )
        
        # Compute window identity
        # Convert string identity_format_version to enum for Tier-0 type safety
        from .window_identity import IdentityFormatVersion
        identity_format_version = IdentityFormatVersion(window_def.identity_format_version) if isinstance(window_def.identity_format_version, str) else window_def.identity_format_version
        
        identity_material = WindowIdentityMaterial(
            window_type=window_def.window_type,  # Use enum directly, not .value
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            window_definition_version=window_def.definition_version,
            identity_format_version=identity_format_version,
            session_gap_ms=window_def.session_gap_ms,
            hop_size_ms=window_def.hop_size_ms,
            aggregation_context={},  # Tier-0: REQUIRED field, always present
        )
        
        window_identity = WindowIdentityFactory.create_identity(identity_material)
        
        # Check lateness
        status = self._check_lateness(
            event_time_ms,
            window_end_ms,
            window_def,
            context,
        )
        
        # Create assignment
        assignment = WindowAssignment(
            window_id=window_identity.window_id,
            window_type=window_def.window_type,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            event_time_ms=event_time_ms,
            alignment_epoch_ms=window_def.alignment_epoch_ms,
            window_version=window_def.definition_version,
            identity_format_version=window_def.identity_format_version,
            status=status,
            definition_fingerprint=window_def.definition_fingerprint(),
        )
        
        # Enforce assignment invariants
        enforce_window_assignment_invariants(assignment, event_time_ms)
        
        return assignment
    
    def resolve_multiple(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: ExecutionContext,
    ) -> List[WindowAssignment]:
        """
        Resolve multiple windows for event (e.g., sliding windows).
        
        For most window types, returns single assignment.
        For sliding windows, returns all overlapping windows.
        
        Args:
            event: Canonical event to classify
            window_def: Window definition
            context: Frozen execution context
        
        Returns:
            List of WindowAssignments
        """
        if window_def.window_type == WindowType.SLIDING_TIME:
            return self._resolve_all_sliding_windows(event, window_def, context)
        
        return [self.resolve(event, window_def, context)]
    
    def _resolve_all_sliding_windows(
        self,
        event: CanonicalEvent,
        window_def: WindowDefinition,
        context: ExecutionContext,
    ) -> List[WindowAssignment]:
        """Resolve all overlapping sliding windows for event."""
        # Enforce invariants
        enforce_window_definition_invariants(window_def, self.policy)
        
        # Extract timestamp
        event_time_ms = event.extract_timestamp(
            window_def.timestamp_extraction_strategy,
            window_def.timestamp_field,
        )
        
        if event_time_ms < 0:
            return [WindowAssignment(
                window_id="",
                window_type=window_def.window_type,
                window_start_ms=0,
                window_end_ms=0,
                event_time_ms=event_time_ms,
                alignment_epoch_ms=window_def.alignment_epoch_ms,
                window_version=window_def.definition_version,
                identity_format_version=window_def.identity_format_version,
                status=WindowAssignmentStatus.INVALID_TIMESTAMP,
                definition_fingerprint=window_def.definition_fingerprint(),
            )]
        
        # Compute all windows
        windows = WindowSemantics.compute_all_sliding_windows(
            event_time_ms,
            window_def.window_size_ms,  # type: ignore
            window_def.slide_ms,  # type: ignore
            window_def.alignment_epoch_ms,
        )
        
        assignments = []
        for window_start_ms, window_end_ms in windows:
            # Enforce identity invariants
            enforce_window_identity_invariants(
                window_type=window_def.window_type,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                alignment_epoch_ms=window_def.alignment_epoch_ms,
                window_size_ms=window_def.window_size_ms,
                slide_ms=window_def.slide_ms,
                allow_unaligned=self.policy.allow_unaligned_windows,
            )
            
            # Compute identity
            # Convert string identity_format_version to enum for Tier-0 type safety
            from .window_identity import IdentityFormatVersion
            identity_format_version = IdentityFormatVersion(window_def.identity_format_version) if isinstance(window_def.identity_format_version, str) else window_def.identity_format_version
            
            identity_material = WindowIdentityMaterial(
                window_type=window_def.window_type,  # Use enum directly, not .value
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                alignment_epoch_ms=window_def.alignment_epoch_ms,
                window_definition_version=window_def.definition_version,
                identity_format_version=identity_format_version,
                aggregation_context={},  # Tier-0: REQUIRED field, always present
            )
            
            window_identity = WindowIdentityFactory.create_identity(identity_material)
            
            # Check lateness
            status = self._check_lateness(
                event_time_ms,
                window_end_ms,
                window_def,
                context,
            )
            
            # Create assignment
            assignment = WindowAssignment(
                window_id=window_identity.window_id,
                window_type=window_def.window_type,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                event_time_ms=event_time_ms,
                alignment_epoch_ms=window_def.alignment_epoch_ms,
                window_version=window_def.definition_version,
                identity_format_version=window_def.identity_format_version,
                status=status,
                definition_fingerprint=window_def.definition_fingerprint(),
            )
            
            # Enforce assignment invariants
            enforce_window_assignment_invariants(assignment, event_time_ms)
            
            assignments.append(assignment)
        
        return assignments
    
    def _check_lateness(
        self,
        event_time_ms: int,
        window_end_ms: int,
        window_def: WindowDefinition,
        context: ExecutionContext,
    ) -> WindowAssignmentStatus:
        """
        Check if event is too late for window.
        
        windows.py MAY:
        - Mark event as out-of-bounds
        - Classify as too-late
        
        windows.py MUST NOT:
        - Reassign events
        - Extend or shrink windows
        - Compensate dynamically
        
        Late handling is policy, not classification.
        """
        # If no watermark, cannot determine lateness
        if context.processing_watermark_ms is None:
            # Check if event is within window bounds
            if event_time_ms < window_def.alignment_epoch_ms:
                return WindowAssignmentStatus.OUT_OF_BOUNDS
            return WindowAssignmentStatus.ASSIGNED
        
        # Compute effective allowed lateness
        effective_lateness_ms = self.policy.effective_allowed_lateness_ms(window_def)
        latest_allowed_time = window_end_ms + effective_lateness_ms
        
        # Check if event is too late
        if context.processing_watermark_ms > latest_allowed_time:
            return WindowAssignmentStatus.TOO_LATE
        
        # Check if event is out of bounds
        if event_time_ms < window_def.alignment_epoch_ms:
            return WindowAssignmentStatus.OUT_OF_BOUNDS
        
        return WindowAssignmentStatus.ASSIGNED


# ============================================================================
# WINDOW REGISTRY (STATIC ALLOW-LIST)
# ============================================================================


class WindowRegistry:
    """
    Central allow-list of known windows.
    
    RULES:
    - No dynamic registration
    - No config-time injection
    - No environment conditionals
    - Deterministic ordering
    
    Registry serialization hash MUST be stable across deploys.
    This prevents window drift.
    """
    
    def __init__(self):
        """Initialize empty registry."""
        self._registry: Dict[str, WindowDefinition] = {}
        self._locked = False
    
    def register(self, name: str, definition: WindowDefinition) -> None:
        """
        Register window definition.
        
        Args:
            name: Window name (must be unique)
            definition: Window definition
        
        Raises:
            RuntimeError: Registry is locked
            ValueError: Window name already registered
        """
        if self._locked:
            raise RuntimeError("Registry is locked, cannot register new definitions")
        
        if name in self._registry:
            raise ValueError(f"Window definition {name} already registered")
        
        self._registry[name] = definition
    
    def lock(self) -> None:
        """Lock registry to prevent further modifications."""
        self._locked = True
    
    def get(self, name: str) -> WindowDefinition:
        """
        Retrieve window definition by name.
        
        Args:
            name: Window name
        
        Returns:
            Window definition
        
        Raises:
            KeyError: Window not found
        """
        if name not in self._registry:
            raise KeyError(f"Window definition {name} not found in registry")
        return self._registry[name]
    
    def list_definitions(self) -> List[str]:
        """
        List all registered window definitions in deterministic order.
        
        Returns:
            Sorted list of window names
        """
        return sorted(self._registry.keys())
    
    def serialize(self) -> bytes:
        """
        Serialize registry to canonical form.
        
        Returns:
            Canonical byte representation of registry
        
        Registry serialization hash MUST be stable across deploys.
        """
        registry_dict = {
            name: {
                "window_type": defn.window_type.value,
                "window_size_ms": defn.window_size_ms,
                "slide_ms": defn.slide_ms,
                "session_gap_ms": defn.session_gap_ms,
                "hop_size_ms": defn.hop_size_ms,
                "event_count": defn.event_count,
                "alignment_epoch_ms": defn.alignment_epoch_ms,
                "allowed_lateness_ms": defn.allowed_lateness_ms,
                "timestamp_field": defn.timestamp_field,
                "timestamp_extraction_strategy": defn.timestamp_extraction_strategy.value,
                "definition_version": defn.definition_version,
                "identity_format_version": defn.identity_format_version,
            }
            for name, defn in sorted(self._registry.items())
        }
        
        import json
        canonical = json.dumps(
            registry_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        return canonical.encode('utf-8')
    
    def registry_hash(self) -> str:
        """
        Compute stable hash of registry contents.
        
        Returns:
            64-character hex hash of registry serialization
        
        This hash MUST be stable across deploys.
        """
        import hashlib
        canonical = self.serialize()
        return hashlib.sha256(canonical).hexdigest()
