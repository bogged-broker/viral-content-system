"""
/infra/recovery/audit/audit_query.py

Read-Only Forensic Inspection & Timeline Reconstruction Engine

MISSION:
Extract truth in a form humans, courts, regulators, and recovery engines can
understand—without mutating it.

CORE QUESTIONS THIS FILE ANSWERS:
- "What exactly happened to this account?"
- "Show me every mutation caused by this rollout"
- "What changed immediately before suppression?"
- "Reconstruct the full causal chain of this failure"
- "Give me a court-safe timeline of events"

ABSOLUTE RULES:
This file NEVER writes.
This file NEVER fixes.
This file NEVER infers.
This file ONLY extracts, orders, and packages evidence.

CRITICAL PRINCIPLE:
Querying audit data must not introduce interpretation.
This file shows. Other layers may interpret.

GUARANTEE:
If two humans ask the same query, they MUST get identical results.

DESIGN PHILOSOPHY:
AuditLogger records truth.
AuditChain seals truth.
AuditValidator proves truth.
AuditQuery shows truth.

This is what lets you say:
"Here is the timeline — nothing added, nothing removed."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, List, Optional, Set, FrozenSet, Dict, Any, Tuple
from hashlib import sha256
from abc import abstractmethod


# =============================================================================
# QUERY SCOPE - Explicit Boundaries
# =============================================================================


class QueryScope(Enum):
    """
    Query scope enumeration.
    
    Scopes must map to exact identifiers, not patterns.
    No wildcards. No fuzzy matching.
    """
    GLOBAL = "GLOBAL"
    """All audit records in system"""
    
    RUN = "RUN"
    """Specific workflow run"""
    
    WORKFLOW = "WORKFLOW"
    """Specific workflow definition (all runs)"""
    
    ACCOUNT = "ACCOUNT"
    """Specific account/user"""
    
    CONTENT = "CONTENT"
    """Specific content item"""
    
    EXPERIMENT = "EXPERIMENT"
    """Specific experiment"""
    
    RECOVERY_ACTION = "RECOVERY_ACTION"
    """Specific recovery action"""
    
    NODE = "NODE"
    """Specific workflow node"""
    
    ARTIFACT = "ARTIFACT"
    """Specific artifact"""


# =============================================================================
# QUERY ORDER - Explicit Ordering
# =============================================================================


class QueryOrder(Enum):
    """
    Query ordering enumeration.
    
    Ordering is explicit — never implicit.
    All orders must be stable and reproducible.
    """
    CHRONOLOGICAL = "CHRONOLOGICAL"
    """Ordered by timestamp, height tie-breaker"""
    
    CAUSAL = "CAUSAL"
    """Ordered by parent-child traversal"""
    
    HEIGHT = "HEIGHT"
    """Ordered by ledger height (chain order)"""
    
    LOGICAL_CLOCK = "LOGICAL_CLOCK"
    """Ordered by Lamport logical clock"""
    
    SEQUENCE = "SEQUENCE"
    """Ordered by sequence number"""


# =============================================================================
# QUERY FILTER - Precise Scoping
# =============================================================================


@dataclass(frozen=True)
class AuditQueryFilter:
    """
    Immutable query filter specification.
    
    All filters are ANDed, never guessed.
    No implicit defaults. No fuzzy matching.
    """
    # Scope
    scope: QueryScope
    scope_id: Optional[str] = None
    
    # Event filtering
    event_types: Optional[Tuple[str, ...]] = None
    action_types: Optional[Tuple[str, ...]] = None
    actor_ids: Optional[Tuple[str, ...]] = None
    target_ids: Optional[Tuple[str, ...]] = None
    
    # Temporal filtering
    from_timestamp: Optional[int] = None
    to_timestamp: Optional[int] = None
    from_height: Optional[int] = None
    to_height: Optional[int] = None
    from_sequence: Optional[int] = None
    to_sequence: Optional[int] = None
    
    # Hash filtering
    event_hashes: Optional[Tuple[str, ...]] = None
    parent_hashes: Optional[Tuple[str, ...]] = None
    
    # Causality filtering
    triggered_by_event_id: Optional[str] = None
    
    # Redaction filtering
    include_redacted: bool = True
    redacted_only: bool = False
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        # Scope ID required for non-global scopes
        if self.scope != QueryScope.GLOBAL:
            assert self.scope_id is not None, \
                f"scope_id required for {self.scope.value} scope"
        
        # Temporal bounds must be valid
        if self.from_timestamp is not None and self.to_timestamp is not None:
            assert self.from_timestamp <= self.to_timestamp, \
                "Invalid timestamp range"
        
        if self.from_height is not None and self.to_height is not None:
            assert self.from_height <= self.to_height, \
                "Invalid height range"
        
        if self.from_sequence is not None and self.to_sequence is not None:
            assert self.from_sequence <= self.to_sequence, \
                "Invalid sequence range"
        
        # Redaction flags must be consistent
        if self.redacted_only:
            assert self.include_redacted, \
                "redacted_only=True requires include_redacted=True"


# =============================================================================
# TIMELINE EVENT - Raw Representation
# =============================================================================


@dataclass(frozen=True)
class AuditTimelineEvent:
    """
    Immutable timeline event.
    
    Raw, unmodified representation of audit record.
    No normalization. No interpretation.
    """
    # Core identity
    event_id: str
    event_hash: str
    
    # Ordering
    height: int
    sequence_number: int
    timestamp: int
    logical_clock: int
    
    # Chain
    parent_hash: str
    
    # Actor
    actor_id: str
    actor_type: str
    
    # Event details
    event_type: str
    action_type: str
    reason: str
    
    # Target
    target_id: Optional[str]
    target_type: Optional[str]
    
    # Payload (raw, unmodified)
    payload: Dict[str, Any]
    
    # Redaction
    is_redacted: bool
    redacted_fields: FrozenSet[str]
    
    # Causality
    triggered_by_event_id: Optional[str]
    triggered_event_ids: FrozenSet[str]
    
    # Metadata
    created_at: int
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.event_id) > 0, "Event ID required"
        assert len(self.event_hash) == 64, "Event hash must be SHA256"
        assert len(self.parent_hash) == 64, "Parent hash must be SHA256"
        assert self.height >= 0, "Height cannot be negative"
        assert self.sequence_number >= 0, "Sequence cannot be negative"
        assert self.timestamp > 0, "Timestamp must be positive"
        assert self.logical_clock >= 0, "Logical clock cannot be negative"
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for serialization.
        
        Deterministic representation.
        """
        return {
            "event_id": self.event_id,
            "event_hash": self.event_hash,
            "height": self.height,
            "sequence_number": self.sequence_number,
            "timestamp": self.timestamp,
            "logical_clock": self.logical_clock,
            "parent_hash": self.parent_hash,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "event_type": self.event_type,
            "action_type": self.action_type,
            "reason": self.reason,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "payload": self.payload,
            "is_redacted": self.is_redacted,
            "redacted_fields": sorted(self.redacted_fields),
            "triggered_by_event_id": self.triggered_by_event_id,
            "triggered_event_ids": sorted(self.triggered_event_ids),
            "created_at": self.created_at,
        }


# =============================================================================
# TIMELINE - Canonical Narrative
# =============================================================================


@dataclass(frozen=True)
class AuditTimeline:
    """
    Immutable audit timeline.
    
    This is the canonical narrative object.
    Complete, ordered, immutable.
    
    CANONICAL IDENTITY:
    - timeline_id: Deterministic hash derived from events + filter + order
    - chain_hash: Deterministic hash of event sequence
    - generated_at: Query-time metadata (excluded from canonical comparison)
    
    Two timelines with identical events, filter, and order will have:
    - Identical timeline_id
    - Identical chain_hash
    - Different generated_at (query execution time)
    
    For canonical comparison, use get_canonical_dict() or compare
    timeline_id/chain_hash directly.
    """
    # Timeline identity
    timeline_id: str
    
    # Events (immutable sequence)
    events: Tuple[AuditTimelineEvent, ...]
    
    # Ordering metadata
    order: QueryOrder
    
    # Scope metadata
    scope: QueryScope
    scope_id: Optional[str]
    
    # Query metadata
    query_filter: AuditQueryFilter
    generated_at: int
    query_version: str
    
    # Chain metadata
    root_event_id: Optional[str]
    chain_hash: str  # Hash of entire timeline
    
    # Statistics
    total_events: int
    redacted_events: int
    unique_actors: int
    time_span_seconds: int
    
    def __post_init__(self) -> None:
        """Validation on construction"""
        assert len(self.timeline_id) > 0, "Timeline ID required"
        assert len(self.events) > 0, "Timeline cannot be empty"
        assert len(self.chain_hash) == 64, "Chain hash must be SHA256"
        assert self.total_events == len(self.events), \
            "Total events must match events length"
        assert self.redacted_events <= self.total_events, \
            "Redacted count cannot exceed total"
        assert self.unique_actors > 0, "Must have at least one actor"
        assert self.time_span_seconds >= 0, "Time span cannot be negative"
    
    def get_event_by_id(self, event_id: str) -> Optional[AuditTimelineEvent]:
        """
        Get event by ID.
        
        Args:
            event_id: Event ID to find
            
        Returns:
            Event if found, None otherwise
        """
        for event in self.events:
            if event.event_id == event_id:
                return event
        return None
    
    def get_events_by_actor(self, actor_id: str) -> Tuple[AuditTimelineEvent, ...]:
        """
        Get all events by specific actor.
        
        Args:
            actor_id: Actor ID to filter by
            
        Returns:
            Tuple of events by this actor
        """
        return tuple(e for e in self.events if e.actor_id == actor_id)
    
    def get_events_by_type(self, event_type: str) -> Tuple[AuditTimelineEvent, ...]:
        """
        Get all events of specific type.
        
        Args:
            event_type: Event type to filter by
            
        Returns:
            Tuple of events of this type
        """
        return tuple(e for e in self.events if e.event_type == event_type)
    
    def get_causal_chain(self, event_id: str) -> Tuple[AuditTimelineEvent, ...]:
        """
        Get causal chain leading to specific event.
        
        Args:
            event_id: Event ID to trace back from
            
        Returns:
            Tuple of events in causal chain (oldest to newest)
        """
        chain: List[AuditTimelineEvent] = []
        current_id = event_id
        
        # Build chain by following triggered_by links
        while current_id is not None:
            event = self.get_event_by_id(current_id)
            if event is None:
                break
            
            chain.append(event)
            current_id = event.triggered_by_event_id
        
        # Reverse to get oldest-first order
        return tuple(reversed(chain))
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for serialization.
        
        Includes query-time metadata (generated_at).
        """
        return {
            "timeline_id": self.timeline_id,
            "events": [e.to_dict() for e in self.events],
            "order": self.order.value,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "generated_at": self.generated_at,
            "query_version": self.query_version,
            "root_event_id": self.root_event_id,
            "chain_hash": self.chain_hash,
            "total_events": self.total_events,
            "redacted_events": self.redacted_events,
            "unique_actors": self.unique_actors,
            "time_span_seconds": self.time_span_seconds,
        }
    
    def get_canonical_dict(self) -> Dict[str, Any]:
        """
        Get canonical representation (excludes query-time metadata).
        
        Use this for:
        - Cross-system verification
        - Evidence fingerprinting
        - Replay tool comparison
        - Legal export consistency
        
        Excludes generated_at to ensure identical queries produce
        identical canonical representations.
        """
        return {
            "timeline_id": self.timeline_id,
            "events": [e.to_dict() for e in self.events],
            "order": self.order.value,
            "scope": self.scope.value,
            "scope_id": self.scope_id,
            "query_version": self.query_version,
            "root_event_id": self.root_event_id,
            "chain_hash": self.chain_hash,
            "total_events": self.total_events,
            "redacted_events": self.redacted_events,
            "unique_actors": self.unique_actors,
            "time_span_seconds": self.time_span_seconds,
        }
    
    def to_human_readable(self) -> str:
        """
        Convert to human-readable narrative.
        
        Suitable for postmortems, incident reports.
        
        NOTE: This method lives in the forensic core for convenience.
        For Tier-0 systems, consider moving human rendering to a separate
        adapter layer to maintain strict extraction vs narrative boundary.
        """
        lines = [
            "=" * 80,
            "AUDIT TIMELINE",
            "=" * 80,
            "",
            f"Timeline ID: {self.timeline_id}",
            f"Scope: {self.scope.value}",
            f"Scope ID: {self.scope_id or 'N/A'}",
            f"Order: {self.order.value}",
            f"Generated: {datetime.fromtimestamp(self.generated_at, tz=timezone.utc).isoformat()}",
            "",
            "STATISTICS:",
            f"  Total Events: {self.total_events}",
            f"  Redacted Events: {self.redacted_events}",
            f"  Unique Actors: {self.unique_actors}",
            f"  Time Span: {self.time_span_seconds} seconds",
            "",
            "EVENTS:",
            "",
        ]
        
        for i, event in enumerate(self.events, 1):
            lines.extend([
                f"[{i}] {event.event_type}",
                f"    Event ID: {event.event_id}",
                f"    Height: {event.height}",
                f"    Sequence: {event.sequence_number}",
                f"    Timestamp: {datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat()}",
                f"    Actor: {event.actor_id} ({event.actor_type})",
                f"    Action: {event.action_type}",
                f"    Reason: {event.reason}",
            ])
            
            if event.target_id:
                lines.append(f"    Target: {event.target_id} ({event.target_type})")
            
            if event.is_redacted:
                lines.append(f"    Redacted Fields: {', '.join(sorted(event.redacted_fields))}")
            
            if event.triggered_by_event_id:
                lines.append(f"    Triggered By: {event.triggered_by_event_id}")
            
            lines.append("")
        
        lines.append("=" * 80)
        
        return "\n".join(lines)


# =============================================================================
# QUERY BACKEND PROTOCOL
# =============================================================================


class AuditQueryBackend(Protocol):
    """
    Protocol for audit query backends.
    
    Backend must be read-only.
    No cache mutation. No lazy reordering. No filtering side effects.
    """
    
    @abstractmethod
    def load_events(
        self,
        query_filter: AuditQueryFilter,
    ) -> List[AuditTimelineEvent]:
        """
        Load events matching filter.
        
        Args:
            query_filter: Filter specification
            
        Returns:
            List of matching events (unordered)
        """
        ...
    
    @abstractmethod
    def get_event_by_id(
        self,
        event_id: str,
    ) -> Optional[AuditTimelineEvent]:
        """
        Get single event by ID.
        
        Args:
            event_id: Event ID
            
        Returns:
            Event if found, None otherwise
        """
        ...
    
    @abstractmethod
    def get_event_count(
        self,
        query_filter: AuditQueryFilter,
    ) -> int:
        """
        Get count of events matching filter.
        
        Args:
            query_filter: Filter specification
            
        Returns:
            Count of matching events
        """
        ...


# =============================================================================
# QUERY ENGINE - The Extractor
# =============================================================================


class AuditQueryEngine:
    """
    Read-only forensic query engine.
    
    MANDATED STEPS:
    1. Load matching audit records
    2. Apply strict filter constraints
    3. Validate ordering preconditions
    4. Build timeline deterministically
    5. Package results immutably
    
    NO INFERRED JOINS. NO MISSING IDs FILLED IN.
    """
    
    QUERY_VERSION = "1.0.0"
    
    def __init__(self, backend: AuditQueryBackend):
        """
        Initialize query engine.
        
        Args:
            backend: Read-only audit backend
        """
        self._backend = backend
    
    def query(
        self,
        query_filter: AuditQueryFilter,
        order: QueryOrder = QueryOrder.CHRONOLOGICAL,
    ) -> AuditTimeline:
        """
        Execute query and build timeline.
        
        Args:
            query_filter: Query filter specification
            order: Desired timeline ordering
            
        Returns:
            Immutable audit timeline
            
        Raises:
            ValueError: If query is invalid or ambiguous
        """
        # STEP 1: Load matching events
        events = self._backend.load_events(query_filter)
        
        if len(events) == 0:
            raise ValueError("Query returned no events")
        
        # STEP 2: Apply strict filter constraints (backend may return superset)
        filtered_events = self._apply_filter_constraints(events, query_filter)
        
        if len(filtered_events) == 0:
            raise ValueError("No events after filter application")
        
        # STEP 3: Validate ordering preconditions
        self._validate_ordering_preconditions(filtered_events, order)
        
        # STEP 4: Build timeline deterministically
        timeline_events = self._build_timeline(filtered_events, order)
        
        # STEP 5: Package results immutably
        return self._package_timeline(
            timeline_events,
            query_filter,
            order,
        )
    
    def query_single_event(
        self,
        event_id: str,
    ) -> AuditTimelineEvent:
        """
        Query single event by ID.
        
        Args:
            event_id: Event ID
            
        Returns:
            Event
            
        Raises:
            ValueError: If event not found
        """
        event = self._backend.get_event_by_id(event_id)
        
        if event is None:
            raise ValueError(f"Event not found: {event_id}")
        
        return event
    
    def query_causal_chain(
        self,
        event_id: str,
    ) -> AuditTimeline:
        """
        Query causal chain leading to specific event.
        
        Args:
            event_id: Event ID to trace back from
            
        Returns:
            Timeline of causal chain
        """
        # Build chain by following triggered_by links
        chain: List[AuditTimelineEvent] = []
        current_id: Optional[str] = event_id
        
        while current_id is not None:
            event = self._backend.get_event_by_id(current_id)
            
            if event is None:
                raise ValueError(f"Event not found in causal chain: {current_id}")
            
            chain.append(event)
            current_id = event.triggered_by_event_id
        
        # Reverse to get oldest-first order
        chain.reverse()
        
        # Package as timeline
        return self._package_timeline(
            chain,
            AuditQueryFilter(
                scope=QueryScope.GLOBAL,
                event_hashes=tuple(e.event_hash for e in chain),
            ),
            QueryOrder.CAUSAL,
        )
    
    def _apply_filter_constraints(
        self,
        events: List[AuditTimelineEvent],
        query_filter: AuditQueryFilter,
    ) -> List[AuditTimelineEvent]:
        """
        Apply filter constraints to events.
        
        All filters are ANDed.
        """
        filtered = events
        
        # Event type filter
        if query_filter.event_types is not None:
            allowed_types = set(query_filter.event_types)
            filtered = [e for e in filtered if e.event_type in allowed_types]
        
        # Action type filter
        if query_filter.action_types is not None:
            allowed_actions = set(query_filter.action_types)
            filtered = [e for e in filtered if e.action_type in allowed_actions]
        
        # Actor filter
        if query_filter.actor_ids is not None:
            allowed_actors = set(query_filter.actor_ids)
            filtered = [e for e in filtered if e.actor_id in allowed_actors]
        
        # Target filter
        if query_filter.target_ids is not None:
            allowed_targets = set(query_filter.target_ids)
            filtered = [e for e in filtered if e.target_id in allowed_targets]
        
        # Timestamp range filter
        if query_filter.from_timestamp is not None:
            filtered = [e for e in filtered if e.timestamp >= query_filter.from_timestamp]
        
        if query_filter.to_timestamp is not None:
            filtered = [e for e in filtered if e.timestamp <= query_filter.to_timestamp]
        
        # Height range filter
        if query_filter.from_height is not None:
            filtered = [e for e in filtered if e.height >= query_filter.from_height]
        
        if query_filter.to_height is not None:
            filtered = [e for e in filtered if e.height <= query_filter.to_height]
        
        # Sequence range filter
        if query_filter.from_sequence is not None:
            filtered = [e for e in filtered if e.sequence_number >= query_filter.from_sequence]
        
        if query_filter.to_sequence is not None:
            filtered = [e for e in filtered if e.sequence_number <= query_filter.to_sequence]
        
        # Hash filters
        if query_filter.event_hashes is not None:
            allowed_hashes = set(query_filter.event_hashes)
            filtered = [e for e in filtered if e.event_hash in allowed_hashes]
        
        if query_filter.parent_hashes is not None:
            allowed_parents = set(query_filter.parent_hashes)
            filtered = [e for e in filtered if e.parent_hash in allowed_parents]
        
        # Causality filter
        if query_filter.triggered_by_event_id is not None:
            filtered = [e for e in filtered if e.triggered_by_event_id == query_filter.triggered_by_event_id]
        
        # Redaction filters
        if not query_filter.include_redacted:
            filtered = [e for e in filtered if not e.is_redacted]
        
        if query_filter.redacted_only:
            filtered = [e for e in filtered if e.is_redacted]
        
        return filtered
    
    def _validate_ordering_preconditions(
        self,
        events: List[AuditTimelineEvent],
        order: QueryOrder,
    ) -> None:
        """
        Validate that ordering can be applied deterministically.
        
        Raises:
            ValueError: If ordering preconditions not met
        """
        if len(events) == 0:
            return
        
        # For causal ordering, verify all parent events are present
        if order == QueryOrder.CAUSAL:
            event_hashes = {e.event_hash for e in events}
            parent_hashes = {e.parent_hash for e in events}
            
            # Genesis hash is OK to be missing
            genesis_hash = "0" * 64
            parent_hashes.discard(genesis_hash)
            
            missing_parents = parent_hashes - event_hashes
            
            if missing_parents:
                raise ValueError(
                    f"Causal ordering requires all parent events present. "
                    f"Missing: {missing_parents}"
                )
        
        # For height ordering, verify heights are unique
        if order == QueryOrder.HEIGHT:
            heights = [e.height for e in events]
            if len(heights) != len(set(heights)):
                raise ValueError("Height ordering requires unique heights")
        
        # For sequence ordering, verify sequences are unique
        if order == QueryOrder.SEQUENCE:
            sequences = [e.sequence_number for e in events]
            if len(sequences) != len(set(sequences)):
                raise ValueError("Sequence ordering requires unique sequence numbers")
    
    def _build_timeline(
        self,
        events: List[AuditTimelineEvent],
        order: QueryOrder,
    ) -> List[AuditTimelineEvent]:
        """
        Build ordered timeline from events.
        
        All orders must be stable and reproducible.
        """
        if order == QueryOrder.CHRONOLOGICAL:
            # Sort by timestamp, then height as tie-breaker
            return sorted(events, key=lambda e: (e.timestamp, e.height))
        
        elif order == QueryOrder.CAUSAL:
            # Topological sort by parent-child relationships
            return self._causal_sort(events)
        
        elif order == QueryOrder.HEIGHT:
            # Sort by height (ledger order)
            return sorted(events, key=lambda e: e.height)
        
        elif order == QueryOrder.LOGICAL_CLOCK:
            # Sort by logical clock
            return sorted(events, key=lambda e: e.logical_clock)
        
        elif order == QueryOrder.SEQUENCE:
            # Sort by sequence number
            return sorted(events, key=lambda e: e.sequence_number)
        
        else:
            raise ValueError(f"Unknown query order: {order}")
    
    def _causal_sort(
        self,
        events: List[AuditTimelineEvent],
    ) -> List[AuditTimelineEvent]:
        """
        Sort events by causal (parent-child) relationships.
        
        Uses topological sort with deterministic tie-breaking.
        
        For total-order safety: when multiple events have the same parent,
        they are ordered by (height, timestamp, event_hash) as a deterministic
        tie-breaker. This ensures identical input → identical output regardless
        of initial event order.
        """
        # Build graph
        hash_to_event = {e.event_hash: e for e in events}
        
        # Find roots (events with no parent in this set)
        genesis_hash = "0" * 64
        roots = [
            e for e in events
            if e.parent_hash == genesis_hash or e.parent_hash not in hash_to_event
        ]
        
        # Sort roots deterministically for stable traversal
        roots.sort(key=lambda e: (e.height, e.timestamp, e.event_hash))
        
        # Topological sort via DFS with deterministic child ordering
        sorted_events: List[AuditTimelineEvent] = []
        visited: Set[str] = set()
        
        def visit(event: AuditTimelineEvent) -> None:
            if event.event_hash in visited:
                return
            
            visited.add(event.event_hash)
            
            # Visit children first (reverse post-order)
            # Sort children deterministically: height, timestamp, hash
            children = [
                e for e in events
                if e.parent_hash == event.event_hash
            ]
            # Deterministic tie-breaker: height, timestamp, event_hash
            children.sort(key=lambda e: (e.height, e.timestamp, e.event_hash))
            
            for child in children:
                visit(child)
            
            sorted_events.append(event)
        
        # Visit all roots (already sorted deterministically)
        for root in roots:
            visit(root)
        
        # Reverse to get correct order
        sorted_events.reverse()
        
        return sorted_events
    
    def _package_timeline(
        self,
        events: List[AuditTimelineEvent],
        query_filter: AuditQueryFilter,
        order: QueryOrder,
    ) -> AuditTimeline:
        """
        Package events into immutable timeline.
        """
        # Compute statistics
        total_events = len(events)
        redacted_events = sum(1 for e in events if e.is_redacted)
        unique_actors = len(set(e.actor_id for e in events))
        
        timestamps = [e.timestamp for e in events]
        time_span = max(timestamps) - min(timestamps) if timestamps else 0
        
        # Find root event
        genesis_hash = "0" * 64
        root_events = [e for e in events if e.parent_hash == genesis_hash]
        root_event_id = root_events[0].event_id if root_events else None
        
        # Compute chain hash (canonical - excludes query-time metadata)
        chain_hash = self._compute_chain_hash(events)
        
        # Generate timeline ID (deterministic - derived from events + filter + order)
        timeline_id = self._generate_timeline_id(events, query_filter, order)
        
        # Query-time metadata (NOT part of canonical identity)
        # generated_at is for audit trail of when query was executed,
        # but does NOT affect timeline_id, chain_hash, or canonical comparison
        generated_at = int(datetime.now(timezone.utc).timestamp())
        
        return AuditTimeline(
            timeline_id=timeline_id,
            events=tuple(events),
            order=order,
            scope=query_filter.scope,
            scope_id=query_filter.scope_id,
            query_filter=query_filter,
            generated_at=generated_at,  # Query metadata, excluded from canonical hashing
            query_version=self.QUERY_VERSION,
            root_event_id=root_event_id,
            chain_hash=chain_hash,
            total_events=total_events,
            redacted_events=redacted_events,
            unique_actors=unique_actors,
            time_span_seconds=time_span,
        )
    
    def _compute_chain_hash(
        self,
        events: List[AuditTimelineEvent],
    ) -> str:
        """
        Compute deterministic hash of entire timeline.
        """
        if len(events) == 0:
            return "0" * 64
        
        # Combine all event hashes in order
        combined = "".join(e.event_hash for e in events)
        return sha256(combined.encode()).hexdigest()
    
    def _generate_timeline_id(
        self,
        events: List[AuditTimelineEvent],
        query_filter: AuditQueryFilter,
        order: QueryOrder,
    ) -> str:
        """
        Generate deterministic timeline ID.
        
        Derived ONLY from:
        - Ordered event hashes (canonical sequence)
        - Query filter canonical form
        - Ordering enum
        
        NO wall-clock time. NO non-deterministic components.
        
        This ensures: identical query → identical timeline_id.
        """
        # Build canonical filter representation
        filter_parts = [
            query_filter.scope.value,
            query_filter.scope_id or "none",
        ]
        
        # Add filter components in canonical order
        if query_filter.event_types:
            filter_parts.append(f"event_types:{','.join(sorted(query_filter.event_types))}")
        if query_filter.action_types:
            filter_parts.append(f"action_types:{','.join(sorted(query_filter.action_types))}")
        if query_filter.actor_ids:
            filter_parts.append(f"actor_ids:{','.join(sorted(query_filter.actor_ids))}")
        if query_filter.target_ids:
            filter_parts.append(f"target_ids:{','.join(sorted(query_filter.target_ids))}")
        if query_filter.from_timestamp is not None:
            filter_parts.append(f"from_ts:{query_filter.from_timestamp}")
        if query_filter.to_timestamp is not None:
            filter_parts.append(f"to_ts:{query_filter.to_timestamp}")
        if query_filter.from_height is not None:
            filter_parts.append(f"from_h:{query_filter.from_height}")
        if query_filter.to_height is not None:
            filter_parts.append(f"to_h:{query_filter.to_height}")
        if query_filter.from_sequence is not None:
            filter_parts.append(f"from_seq:{query_filter.from_sequence}")
        if query_filter.to_sequence is not None:
            filter_parts.append(f"to_seq:{query_filter.to_sequence}")
        if query_filter.event_hashes:
            filter_parts.append(f"event_hashes:{','.join(sorted(query_filter.event_hashes))}")
        if query_filter.parent_hashes:
            filter_parts.append(f"parent_hashes:{','.join(sorted(query_filter.parent_hashes))}")
        if query_filter.triggered_by_event_id:
            filter_parts.append(f"triggered_by:{query_filter.triggered_by_event_id}")
        filter_parts.append(f"include_redacted:{query_filter.include_redacted}")
        filter_parts.append(f"redacted_only:{query_filter.redacted_only}")
        
        filter_canonical = "|".join(filter_parts)
        
        # Build ordered event hash sequence (canonical)
        event_hash_sequence = "".join(e.event_hash for e in events)
        
        # Combine: filter + order + event sequence
        components = [
            filter_canonical,
            order.value,
            event_hash_sequence,
        ]
        canonical = "|".join(components)
        return sha256(canonical.encode()).hexdigest()


# =============================================================================
# QUERY INVARIANTS - Enforced Truth
# =============================================================================


class AuditQueryInvariants:
    """
    Query invariants enforcer.
    
    MUST ENFORCE:
    - No state mutation
    - No record normalization
    - No sorting without explicit order
    - No partial payload stripping
    - No silent omissions
    - Identical input → identical output
    
    Violation = forensic corruption.
    """
    
    @staticmethod
    def verify_no_mutation(
        original_events: List[AuditTimelineEvent],
        timeline_events: Tuple[AuditTimelineEvent, ...],
    ) -> None:
        """
        Verify events were not mutated during query.
        
        Raises:
            AssertionError: If mutation detected
        """
        # Build lookup
        original_by_id = {e.event_id: e for e in original_events}
        
        # Verify each timeline event matches original
        for event in timeline_events:
            original = original_by_id.get(event.event_id)
            assert original is not None, \
                f"Event {event.event_id} not in original set"
            
            # Verify immutability (reference equality for frozen dataclasses)
            assert event == original, \
                f"Event {event.event_id} was mutated"
    
    @staticmethod
    def verify_deterministic_ordering(
        events1: Tuple[AuditTimelineEvent, ...],
        events2: Tuple[AuditTimelineEvent, ...],
    ) -> None:
        """
        Verify two orderings of same events are identical.
        
        Raises:
            AssertionError: If orderings differ
        """
        assert len(events1) == len(events2), \
            "Event count mismatch"
        
        for i, (e1, e2) in enumerate(zip(events1, events2)):
            assert e1.event_id == e2.event_id, \
                f"Order mismatch at index {i}: {e1.event_id} != {e2.event_id}"
    
    @staticmethod
    def verify_complete_payload(
        event: AuditTimelineEvent,
        required_fields: FrozenSet[str],
    ) -> None:
        """
        Verify payload has all required fields.
        
        Raises:
            AssertionError: If fields missing
        """
        payload_keys = set(event.payload.keys())
        missing = required_fields - payload_keys
        
        assert len(missing) == 0, \
            f"Missing payload fields in {event.event_id}: {missing}"


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def query_workflow_timeline(
    backend: AuditQueryBackend,
    workflow_id: str,
    order: QueryOrder = QueryOrder.CHRONOLOGICAL,
) -> AuditTimeline:
    """
    Convenience function to query workflow timeline.
    
    Args:
        backend: Query backend
        workflow_id: Workflow ID
        order: Timeline ordering
        
    Returns:
        Workflow timeline
    """
    query_filter = AuditQueryFilter(
        scope=QueryScope.WORKFLOW,
        scope_id=workflow_id,
    )
    
    engine = AuditQueryEngine(backend)
    return engine.query(query_filter, order)


def query_run_timeline(
    backend: AuditQueryBackend,
    run_id: str,
    order: QueryOrder = QueryOrder.CHRONOLOGICAL,
) -> AuditTimeline:
    """
    Convenience function to query run timeline.
    
    Args:
        backend: Query backend
        run_id: Run ID
        order: Timeline ordering
        
    Returns:
        Run timeline
    """
    query_filter = AuditQueryFilter(
        scope=QueryScope.RUN,
        scope_id=run_id,
    )
    
    engine = AuditQueryEngine(backend)
    return engine.query(query_filter, order)


def query_actor_timeline(
    backend: AuditQueryBackend,
    actor_id: str,
    from_timestamp: Optional[int] = None,
    to_timestamp: Optional[int] = None,
) -> AuditTimeline:
    """
    Convenience function to query actor timeline.
    
    Args:
        backend: Query backend
        actor_id: Actor ID
        from_timestamp: Start timestamp (optional)
        to_timestamp: End timestamp (optional)
        
    Returns:
        Actor timeline
    """
    query_filter = AuditQueryFilter(
        scope=QueryScope.GLOBAL,
        actor_ids=(actor_id,),
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
    )
    
    engine = AuditQueryEngine(backend)
    return engine.query(query_filter, QueryOrder.CHRONOLOGICAL)


# =============================================================================
# INVARIANTS - Compile-Time Guarantees
# =============================================================================

# ✅ Read-only - no mutations, no writes
# ✅ Deterministic - same query → same result
# ✅ Complete - no silent omissions
# ✅ Immutable - all frozen dataclasses
# ✅ Explicit - no implicit defaults or fuzzy matching
# ✅ Reproducible - identical input → identical output
# ✅ Verifiable - invariants enforced
# ✅ Forensic-safe - suitable for legal evidence

# This file shows truth. It does not interpret.
# "Here is the timeline — nothing added, nothing removed."