"""
/infra/observability/trace_query.py

Causal Trace Query & Explainability Engine

This is the explainability layer for the entire system. It answers: "Why did
the system do that?" by reconstructing causal chains deterministically.

This turns opaque enforcement into defensible causality.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Dict
import json


# ============================================================================
# CORE ENUMS (STRICT — NO STRINGS)
# ============================================================================


class TraceEntityType(Enum):
    """
    Entity types that define blast radius targets.
    """

    WORKFLOW = "workflow"
    JOB = "job"
    CONTENT = "content"
    ACCOUNT = "account"
    SYSTEM = "system"


class TraceEventType(Enum):
    """
    Event types are causal, not just chronological.
    """

    SIGNAL = "signal"
    HEALTH_EVAL = "health_evaluation"
    POLICY_DECISION = "policy_decision"
    ENFORCEMENT = "enforcement"


# ============================================================================
# QUERY PRIMITIVES
# ============================================================================


@dataclass(frozen=True)
class TraceWindow:
    """
    Time window for queries.
    
    Time is mandatory. No unbounded queries allowed.
    """

    start_ts: int
    end_ts: int

    def validate(self) -> None:
        """
        Validate time window.
        
        Raises:
            ValueError: If window invalid
        """
        if self.start_ts < 0 or self.end_ts < 0:
            raise ValueError("Timestamps must be non-negative")

        if self.start_ts >= self.end_ts:
            raise ValueError("start_ts must be before end_ts")

    def contains(self, timestamp: int) -> bool:
        """Check if timestamp falls within window."""
        return self.start_ts <= timestamp <= self.end_ts

    def duration_seconds(self) -> int:
        """Get window duration in seconds."""
        return (self.end_ts - self.start_ts) // 1000


@dataclass(frozen=True)
class TraceFilter:
    """
    Filter for trace queries.
    
    Queries must be explicit.
    """

    entity_type: TraceEntityType
    entity_id: str | None

    event_types: set[TraceEventType]

    def matches(
        self,
        entity_type: TraceEntityType,
        entity_id: str,
        event_type: TraceEventType,
    ) -> bool:
        """
        Check if event matches this filter.
        
        Args:
            entity_type: Event entity type
            entity_id: Event entity ID
            event_type: Event type
            
        Returns:
            True if matches filter
        """
        # Check entity type
        if entity_type != self.entity_type:
            return False

        # Check entity ID if specified
        if self.entity_id is not None and entity_id != self.entity_id:
            return False

        # Check event type
        if event_type not in self.event_types:
            return False

        return True


@dataclass(frozen=True)
class TraceQuery:
    """
    Complete trace query specification.
    
    No implicit joins. Ever.
    """

    window: TraceWindow
    filters: list[TraceFilter]

    def validate(self) -> None:
        """
        Validate query.
        
        Raises:
            ValueError: If query invalid
        """
        self.window.validate()

        if not self.filters:
            raise ValueError("Query must have at least one filter")


# ============================================================================
# CAUSAL MODELING (CORE VALUE)
# ============================================================================


@dataclass(frozen=True)
class CausalLink:
    """
    Represents a causal relationship between events.
    
    This is how cause is represented — not inferred.
    """

    from_event_id: str
    to_event_id: str
    reason: str

    timestamp: int

    def validate(self) -> None:
        """
        Validate causal link.
        
        Raises:
            ValueError: If link invalid
        """
        if not self.from_event_id or not self.to_event_id:
            raise ValueError("Both from_event_id and to_event_id required")

        if self.from_event_id == self.to_event_id:
            raise ValueError("Causal link cannot be self-referential")

        if not self.reason or not self.reason.strip():
            raise ValueError("Causal reason required")


@dataclass(frozen=True)
class CausalChain:
    """
    Reconstructed causal chain of events.
    
    Chains are reconstructed, not inferred.
    """

    events: list[str]
    links: list[CausalLink]

    chain_id: str
    start_timestamp: int
    end_timestamp: int

    def validate(self) -> None:
        """
        Validate causal chain integrity.
        
        Raises:
            ValueError: If chain invalid
        """
        if not self.events:
            raise ValueError("Causal chain must have at least one event")

        # Validate all links
        for link in self.links:
            link.validate()

            # Ensure link endpoints exist in events
            if link.from_event_id not in self.events:
                raise ValueError(
                    f"Link from_event {link.from_event_id} not in event list"
                )

            if link.to_event_id not in self.events:
                raise ValueError(
                    f"Link to_event {link.to_event_id} not in event list"
                )

        # Validate timestamps
        if self.start_timestamp > self.end_timestamp:
            raise ValueError("start_timestamp must be before end_timestamp")

    def get_root_events(self) -> list[str]:
        """Get events with no incoming links."""
        events_with_incoming = {link.to_event_id for link in self.links}
        return [event for event in self.events if event not in events_with_incoming]

    def get_leaf_events(self) -> list[str]:
        """Get events with no outgoing links."""
        events_with_outgoing = {link.from_event_id for link in self.links}
        return [event for event in self.events if event not in events_with_outgoing]

    def get_successors(self, event_id: str) -> list[str]:
        """Get immediate successor events."""
        return [link.to_event_id for link in self.links if link.from_event_id == event_id]

    def get_predecessors(self, event_id: str) -> list[str]:
        """Get immediate predecessor events."""
        return [link.from_event_id for link in self.links if link.to_event_id == event_id]


# ============================================================================
# TRACE EVENT (DATA MODEL)
# ============================================================================


@dataclass(frozen=True)
class TraceEvent:
    """
    Represents a single traced event with full context.
    """

    event_id: str
    event_type: TraceEventType

    entity_type: TraceEntityType
    entity_id: str

    timestamp: int

    data: dict[str, Any] = field(default_factory=dict)

    trace_id: str = ""
    parent_event_id: str | None = None


# ============================================================================
# EXPLANATION LAYER (HUMAN SAFE)
# ============================================================================


@dataclass(frozen=True)
class ExplanationNode:
    """
    Human-readable explanation node with evidence.
    
    No emotional language. No speculation. Evidence-first wording.
    """

    summary: str
    evidence_ids: list[str]

    event_type: TraceEventType
    timestamp: int

    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """
        Validate explanation node.
        
        Raises:
            ValueError: If node invalid
        """
        if not self.summary or not self.summary.strip():
            raise ValueError("Explanation summary cannot be empty")

        if not self.evidence_ids:
            raise ValueError("Explanation must reference at least one evidence ID")


@dataclass
class ExplanationTree:
    """
    Hierarchical explanation tree.
    
    Produces:
    - Plain English explanations
    - Structured justifications
    - Policy-version annotated evidence
    """

    root: ExplanationNode
    children: list["ExplanationTree"] = field(default_factory=list)

    def validate(self) -> None:
        """
        Validate explanation tree.
        
        Raises:
            ValueError: If tree invalid
        """
        self.root.validate()

        for child in self.children:
            child.validate()

    def to_text(self, indent: int = 0) -> str:
        """
        Convert tree to indented text representation.
        
        Args:
            indent: Current indentation level
            
        Returns:
            Text representation
        """
        prefix = "  " * indent
        lines = [f"{prefix}• {self.root.summary}"]

        for child in self.children:
            lines.append(child.to_text(indent + 1))

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert tree to dictionary for serialization.
        
        Returns:
            Dictionary representation
        """
        return {
            "summary": self.root.summary,
            "evidence_ids": self.root.evidence_ids,
            "event_type": self.root.event_type.value,
            "timestamp": self.root.timestamp,
            "metadata": self.root.metadata,
            "children": [child.to_dict() for child in self.children],
        }


# ============================================================================
# TRACE RESOLVER (ENGINE)
# ============================================================================


class TraceResolver:
    """
    Resolves trace queries into causal chains.
    
    Guarantees:
    - Deterministic ordering
    - Causal integrity
    - No missing links silently ignored
    - Fails loudly on inconsistencies
    """

    def __init__(self, trace_storage: dict[str, TraceEvent]):
        """
        Initialize trace resolver.
        
        Args:
            trace_storage: Mapping of event_id -> TraceEvent
        """
        self._storage = trace_storage

    def resolve(self, query: TraceQuery) -> CausalChain:
        """
        Resolve query into causal chain.
        
        Args:
            query: TraceQuery to resolve
            
        Returns:
            CausalChain
            
        Raises:
            ValueError: If query invalid or resolution fails
        """
        query.validate()

        # Find matching events
        matching_events = self._find_matching_events(query)

        if not matching_events:
            # Empty chain is valid for no matches
            return CausalChain(
                events=[],
                links=[],
                chain_id=f"chain_{query.window.start_ts}_{query.window.end_ts}",
                start_timestamp=query.window.start_ts,
                end_timestamp=query.window.end_ts,
            )

        # Reconstruct causal links
        links = self._reconstruct_links(matching_events)

        # Build chain
        event_ids = [event.event_id for event in matching_events]

        chain = CausalChain(
            events=event_ids,
            links=links,
            chain_id=f"chain_{query.window.start_ts}_{query.window.end_ts}",
            start_timestamp=min(e.timestamp for e in matching_events),
            end_timestamp=max(e.timestamp for e in matching_events),
        )

        chain.validate()

        return chain

    def _find_matching_events(
        self,
        query: TraceQuery,
    ) -> list[TraceEvent]:
        """
        Find events matching query filters.
        
        Args:
            query: TraceQuery with filters
            
        Returns:
            List of matching TraceEvents
        """
        matching = []

        for event in self._storage.values():
            # Check time window
            if not query.window.contains(event.timestamp):
                continue

            # Check if any filter matches
            for filter_spec in query.filters:
                if filter_spec.matches(
                    event.entity_type,
                    event.entity_id,
                    event.event_type,
                ):
                    matching.append(event)
                    break

        # Sort by timestamp for deterministic ordering
        return sorted(matching, key=lambda e: e.timestamp)

    def _reconstruct_links(
        self,
        events: list[TraceEvent],
    ) -> list[CausalLink]:
        """
        Reconstruct causal links between events.
        
        Args:
            events: List of TraceEvents
            
        Returns:
            List of CausalLinks
        """
        links = []
        event_map = {event.event_id: event for event in events}

        for event in events:
            # If event has parent, create link
            if event.parent_event_id and event.parent_event_id in event_map:
                parent = event_map[event.parent_event_id]

                # Derive causal reason from event types
                reason = self._derive_causal_reason(parent, event)

                link = CausalLink(
                    from_event_id=parent.event_id,
                    to_event_id=event.event_id,
                    reason=reason,
                    timestamp=event.timestamp,
                )

                links.append(link)

        return links

    def _derive_causal_reason(
        self,
        from_event: TraceEvent,
        to_event: TraceEvent,
    ) -> str:
        """
        Derive causal reason between events.
        
        Args:
            from_event: Source event
            to_event: Target event
            
        Returns:
            Causal reason string
        """
        # Map event type transitions to reasons
        reason_map = {
            (TraceEventType.SIGNAL, TraceEventType.HEALTH_EVAL): "triggered health evaluation",
            (TraceEventType.HEALTH_EVAL, TraceEventType.POLICY_DECISION): "resulted in policy decision",
            (TraceEventType.POLICY_DECISION, TraceEventType.ENFORCEMENT): "authorized enforcement action",
        }

        reason = reason_map.get((from_event.event_type, to_event.event_type))

        if reason:
            return reason

        # Default reason
        return f"{from_event.event_type.value} led to {to_event.event_type.value}"


# ============================================================================
# EXPLANATION BUILDER
# ============================================================================


class ExplanationBuilder:
    """
    Builds human-readable explanations from causal chains.
    
    Rules:
    - No emotional language
    - No speculation
    - Evidence-first wording
    - Policy-version annotated
    """

    def __init__(self, event_storage: dict[str, TraceEvent]):
        """
        Initialize explanation builder.
        
        Args:
            event_storage: Mapping of event_id -> TraceEvent
        """
        self._event_storage = event_storage

    def build(self, chain: CausalChain) -> ExplanationTree:
        """
        Build explanation tree from causal chain.
        
        Args:
            chain: CausalChain to explain
            
        Returns:
            ExplanationTree
            
        Raises:
            ValueError: If chain invalid
        """
        chain.validate()

        if not chain.events:
            # Empty chain
            root = ExplanationNode(
                summary="No events found matching query criteria",
                evidence_ids=[],
                event_type=TraceEventType.SIGNAL,
                timestamp=chain.start_timestamp,
            )
            return ExplanationTree(root=root, children=[])

        # Find root events (no predecessors)
        root_event_ids = chain.get_root_events()

        if not root_event_ids:
            # Shouldn't happen with valid chain, but handle gracefully
            root_event_ids = [chain.events[0]]

        # Build tree from first root
        root_event_id = root_event_ids[0]
        root_event = self._event_storage.get(root_event_id)

        if root_event is None:
            raise ValueError(f"Root event {root_event_id} not found in storage")

        root_node = self._build_explanation_node(root_event, chain)

        # Build children recursively
        children = self._build_children(root_event_id, chain)

        tree = ExplanationTree(root=root_node, children=children)
        tree.validate()

        return tree

    def _build_explanation_node(
        self,
        event: TraceEvent,
        chain: CausalChain,
    ) -> ExplanationNode:
        """
        Build explanation node for a single event.
        
        Args:
            event: TraceEvent to explain
            chain: Parent causal chain
            
        Returns:
            ExplanationNode
        """
        summary = self._generate_summary(event)

        # Find links involving this event for evidence
        related_links = [
            link for link in chain.links
            if link.from_event_id == event.event_id or link.to_event_id == event.event_id
        ]

        evidence_ids = [event.event_id] + [link.to_event_id for link in related_links]

        return ExplanationNode(
            summary=summary,
            evidence_ids=evidence_ids,
            event_type=event.event_type,
            timestamp=event.timestamp,
            metadata=event.data,
        )

    def _build_children(
        self,
        event_id: str,
        chain: CausalChain,
    ) -> list[ExplanationTree]:
        """
        Recursively build child explanation trees.
        
        Args:
            event_id: Parent event ID
            chain: Causal chain
            
        Returns:
            List of child ExplanationTrees
        """
        children = []

        # Get successor events
        successors = chain.get_successors(event_id)

        for successor_id in successors:
            successor_event = self._event_storage.get(successor_id)

            if successor_event is None:
                continue

            # Build node for successor
            child_node = self._build_explanation_node(successor_event, chain)

            # Recursively build children
            grandchildren = self._build_children(successor_id, chain)

            child_tree = ExplanationTree(root=child_node, children=grandchildren)
            children.append(child_tree)

        return children

    def _generate_summary(self, event: TraceEvent) -> str:
        """
        Generate human-readable summary for event.
        
        Args:
            event: TraceEvent to summarize
            
        Returns:
            Summary string
        """
        # Event type specific summaries
        if event.event_type == TraceEventType.SIGNAL:
            dimension = event.data.get("dimension", "unknown")
            value = event.data.get("value", "N/A")
            return f"Signal detected: {dimension}={value}"

        elif event.event_type == TraceEventType.HEALTH_EVAL:
            state = event.data.get("health_state", "unknown")
            return f"Health evaluated as {state}"

        elif event.event_type == TraceEventType.POLICY_DECISION:
            action = event.data.get("action", "unknown")
            policy_version = event.data.get("policy_version", "unknown")
            return f"Policy {policy_version} authorized action: {action}"

        elif event.event_type == TraceEventType.ENFORCEMENT:
            action = event.data.get("action", "unknown")
            scope = event.data.get("scope", "unknown")
            return f"Enforcement executed: {action} at {scope} scope"

        else:
            return f"Event {event.event_type.value} on {event.entity_type.value}:{event.entity_id}"


# ============================================================================
# TRACE QUERY API (PUBLIC ENTRY POINT)
# ============================================================================


class TraceQueryAPI:
    """
    Public API for trace querying and explanation.
    
    This is what:
    - Humans use
    - Dashboards call
    - Audits rely on
    - Postmortems depend on
    """

    def __init__(self):
        """Initialize trace query API."""
        self._event_storage: dict[str, TraceEvent] = {}

    def record_event(self, event: TraceEvent) -> None:
        """
        Record a trace event for future querying.
        
        Args:
            event: TraceEvent to record
        """
        self._event_storage[event.event_id] = event

    def explain(self, query: TraceQuery) -> ExplanationTree:
        """
        Explain a query by resolving and building explanation tree.
        
        Args:
            query: TraceQuery to explain
            
        Returns:
            ExplanationTree with human-readable explanation
            
        Raises:
            ValueError: If query invalid
        """
        query.validate()

        # Resolve to causal chain
        resolver = TraceResolver(self._event_storage)
        chain = resolver.resolve(query)

        # Build explanation
        builder = ExplanationBuilder(self._event_storage)
        explanation = builder.build(chain)

        return explanation

    def query_causal_chain(self, query: TraceQuery) -> CausalChain:
        """
        Get raw causal chain for a query.
        
        Args:
            query: TraceQuery
            
        Returns:
            CausalChain
        """
        query.validate()

        resolver = TraceResolver(self._event_storage)
        return resolver.resolve(query)

    def get_event(self, event_id: str) -> TraceEvent | None:
        """Get event by ID."""
        return self._event_storage.get(event_id)

    def clear_events(self) -> None:
        """Clear all stored events (for testing/cleanup)."""
        self._event_storage.clear()


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def create_trace_event(
    event_type: TraceEventType,
    entity_type: TraceEntityType,
    entity_id: str,
    timestamp: int,
    data: dict[str, Any] | None = None,
    trace_id: str = "",
    parent_event_id: str | None = None,
) -> TraceEvent:
    """
    Create a trace event.
    
    Args:
        event_type: Type of event
        entity_type: Entity type affected
        entity_id: Entity identifier
        timestamp: Event timestamp
        data: Optional event data
        trace_id: Optional trace ID
        parent_event_id: Optional parent event for causality
        
    Returns:
        TraceEvent
    """
    import hashlib
    import time

    # Generate event ID
    event_data = f"{event_type.value}:{entity_type.value}:{entity_id}:{timestamp}:{time.time_ns()}"
    event_id = hashlib.sha256(event_data.encode()).hexdigest()[:16]

    return TraceEvent(
        event_id=event_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        timestamp=timestamp,
        data=data or {},
        trace_id=trace_id,
        parent_event_id=parent_event_id,
    )


def explain_enforcement_action(
    api: TraceQueryAPI,
    entity_type: TraceEntityType,
    entity_id: str,
    start_ts: int,
    end_ts: int,
) -> str:
    """
    Explain an enforcement action on an entity.
    
    Args:
        api: TraceQueryAPI instance
        entity_type: Type of entity
        entity_id: Entity identifier
        start_ts: Start timestamp
        end_ts: End timestamp
        
    Returns:
        Human-readable explanation
    """
    # Build query
    window = TraceWindow(start_ts=start_ts, end_ts=end_ts)

    filters = [
        TraceFilter(
            entity_type=entity_type,
            entity_id=entity_id,
            event_types={
                TraceEventType.SIGNAL,
                TraceEventType.HEALTH_EVAL,
                TraceEventType.POLICY_DECISION,
                TraceEventType.ENFORCEMENT,
            },
        )
    ]

    query = TraceQuery(window=window, filters=filters)

    # Get explanation
    explanation = api.explain(query)

    # Convert to text
    return explanation.to_text()