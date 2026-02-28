"""
/data/pipelines/aggregation/aggregation_context.py

Immutable aggregation execution context.

WHAT THIS FILE EXISTS FOR (NO ABSTRACTION, NO POETRY):
aggregation_context.py defines the unchangeable execution envelope for a single aggregation run.

> If counters answer "how many?"
this file answers "under what authority, scope, and constraints?"

Without this file:
- replays lie
- audits collapse
- recovery becomes unverifiable
- and aggregation quietly drifts into analytics

This file prevents aggregation from inventing reality.

CORE PRINCIPLE:
An aggregation run must be perfectly reproducible from its context alone.

If you have:
- raw canonical facts
- aggregation_context
- aggregation code

You must get bit-identical results, forever.

CONTEXT IS:
✅ immutable
✅ hashable
✅ serializable
✅ audit-visible
✅ recovery-safe

Context is created once, then frozen.

CONTEXT IS NOT:
❌ configuration
❌ mutable state
❌ optimization hints
❌ tuning knobs
❌ environment variables

If it changes execution, it belongs here — permanently.

IMMUTABILITY RULES:
Once created:
- No setters
- No mutation
- No lazy fields
- No derived values added later

Violations = hard crash.

SERIALIZATION GUARANTEES:
Context must serialize canonically:
- stable field ordering
- explicit types
- deterministic encoding
- hash stable across languages

If two serialized contexts differ → they are different runs.

AUDIT RESPONSIBILITIES:
Every aggregation run must emit:
- serialized context
- context hash
- run ID
- parent replay reference (if any)

Audit systems treat context as source-of-truth metadata.

FAILURE MODES (ALL FATAL):
- missing window definition
- missing invariant snapshot
- code hash mismatch
- scope ambiguity
- mutable field access

Aggregation must fail loudly or not run at all.

Aggregation without memory is guessing. This file gives it memory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import FrozenSet, Optional
from uuid import UUID, uuid4


# ============================================================================
# Type Aliases - Explicit semantic types
# ============================================================================

AggregationRunID = UUID
PipelineVersion = str
CodeHash = str
Hash = str
Timestamp = datetime
SchemaRef = str
EntityType = str
SourceID = str


# ============================================================================
# Enums - Canonical value sets
# ============================================================================

class AggregationTrigger(Enum):
    """
    Explicit reason why this aggregation ran.
    
    Trigger impacts:
        - audit severity
        - downstream trust
        - export eligibility
    """
    SCHEDULED = "scheduled"
    BACKFILL = "backfill"
    REPAIR = "repair"
    RECOVERY_REPLAY = "recovery_replay"
    MANUAL_FORCED = "manual_forced"
    
    def __str__(self) -> str:
        return self.value


# ============================================================================
# Immutable Value Objects
# ============================================================================

@dataclass(frozen=True)
class AggregationScope:
    """
    Defines what facts were eligible for this aggregation run.
    
    No implicit "everything" allowed.
    """
    schemas: FrozenSet[SchemaRef]
    entity_types: FrozenSet[EntityType]
    allowed_sources: FrozenSet[SourceID]
    
    def __post_init__(self) -> None:
        """Validate scope is non-empty and well-formed."""
        if not self.schemas:
            raise ValueError("AggregationScope.schemas cannot be empty")
        if not self.entity_types:
            raise ValueError("AggregationScope.entity_types cannot be empty")
        if not self.allowed_sources:
            raise ValueError("AggregationScope.allowed_sources cannot be empty")
    
    def to_dict(self) -> dict:
        """Canonical serialization."""
        return {
            "schemas": sorted(self.schemas),
            "entity_types": sorted(self.entity_types),
            "allowed_sources": sorted(self.allowed_sources),
        }


@dataclass(frozen=True)
class InputBounds:
    """
    Hard limits for inputs to this aggregation run.
    
    Prevents:
        - partial ingestion lies
        - hidden drops
        - silent re-expansion during replay
    """
    max_events: int
    min_event_time: Timestamp
    max_event_time: Timestamp
    fact_id_set_hash: Hash
    
    def __post_init__(self) -> None:
        """Validate bounds are sensible."""
        if self.max_events <= 0:
            raise ValueError(f"max_events must be positive, got {self.max_events}")
        if self.min_event_time >= self.max_event_time:
            raise ValueError(
                f"min_event_time ({self.min_event_time}) must be before "
                f"max_event_time ({self.max_event_time})"
            )
        if not self.fact_id_set_hash:
            raise ValueError("fact_id_set_hash cannot be empty")
    
    def to_dict(self) -> dict:
        """Canonical serialization."""
        return {
            "max_events": self.max_events,
            "min_event_time": self.min_event_time.isoformat(),
            "max_event_time": self.max_event_time.isoformat(),
            "fact_id_set_hash": self.fact_id_set_hash,
        }


@dataclass(frozen=True)
class LogicalClock:
    """
    Logical (not wall) clock for ordering runs.
    
    Used to order runs without trusting system time.
    """
    epoch: int
    tick: int
    
    def __post_init__(self) -> None:
        """Validate clock values."""
        if self.epoch < 0:
            raise ValueError(f"epoch must be non-negative, got {self.epoch}")
        if self.tick < 0:
            raise ValueError(f"tick must be non-negative, got {self.tick}")
    
    def __lt__(self, other: LogicalClock) -> bool:
        """Total ordering for logical clocks."""
        if not isinstance(other, LogicalClock):
            return NotImplemented
        return (self.epoch, self.tick) < (other.epoch, other.tick)
    
    def to_dict(self) -> dict:
        """Canonical serialization."""
        return {
            "epoch": self.epoch,
            "tick": self.tick,
        }


@dataclass(frozen=True)
class Window:
    """
    Frozen definition of a single aggregation window.
    
    Windows cannot be recomputed later differently.
    """
    window_id: str
    start_time: Timestamp
    end_time: Timestamp
    alignment: str
    version: int
    
    def __post_init__(self) -> None:
        """Validate window definition."""
        if not self.window_id:
            raise ValueError("window_id cannot be empty")
        if self.start_time >= self.end_time:
            raise ValueError(
                f"start_time ({self.start_time}) must be before "
                f"end_time ({self.end_time})"
            )
        if self.version < 1:
            raise ValueError(f"version must be >= 1, got {self.version}")
    
    def to_dict(self) -> dict:
        """Canonical serialization."""
        return {
            "window_id": self.window_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "alignment": self.alignment,
            "version": self.version,
        }


@dataclass(frozen=True)
class WindowSet:
    """
    Frozen set of all windows used in this aggregation run.
    """
    windows: FrozenSet[Window]
    
    def __post_init__(self) -> None:
        """Validate window set is non-empty and has no ID collisions."""
        if not self.windows:
            raise ValueError("WindowSet.windows cannot be empty")
        
        window_ids = [w.window_id for w in self.windows]
        if len(window_ids) != len(set(window_ids)):
            raise ValueError("WindowSet contains duplicate window_ids")
    
    def to_dict(self) -> dict:
        """Canonical serialization with stable ordering."""
        return {
            "windows": sorted(
                [w.to_dict() for w in self.windows],
                key=lambda x: x["window_id"]
            )
        }


# ============================================================================
# Main Context - The Constitution
# ============================================================================

@dataclass(frozen=True)
class AggregationContext:
    """
    The unchangeable execution envelope for a single aggregation run.
    
    This is the constitution under which math was allowed to happen.
    
    If someone asks "Can we trust this number?", this file is the first and last answer.
    
    CANONICAL CONTEXT SHAPE:
    Every field is required unless explicitly optional.
    
    FIELD-BY-FIELD SEMANTICS (NON-NEGOTIABLE):
    
    🆔 aggregation_run_id:
        - Globally unique
        - Never reused
        - Never derived from time alone
        - Stable across retries
        - Used everywhere: audit logs, counter updates, exports, recovery proof
    
    🧬 pipeline_version:
        - Explicit semantic version
        - Must match aggregation code + counter specs
        - Changing counter meaning → new version
        - No implicit upgrades allowed
    
    🔐 code_hash:
        - Content hash of: aggregation runner, counter definitions, window logic
        - Prevents "same version, different code" lies
        - If code hash differs → run is invalid
    
    ⚡ trigger:
        - Explicit why this aggregation ran: SCHEDULED, BACKFILL, REPAIR, RECOVERY_REPLAY, MANUAL_FORCED
        - Trigger impacts: audit severity, downstream trust, export eligibility
    
    🎯 scope:
        - Defines what facts were eligible
        - No implicit "everything"
    
    ⏱️ windows:
        - Frozen definition of all windows used in this run
        - Window IDs, Boundaries, Alignment, Version
        - Windows cannot be recomputed later differently
    
    📦 input_bounds:
        - Hard limits for inputs
        - Prevents: partial ingestion lies, hidden drops, silent re-expansion during replay
    
    📜 invariants_snapshot:
        - Digest of: aggregation invariants, counter invariants, pipeline invariants
        - If invariants change → snapshot differs → run incomparable
    
    🕒 clock:
        - Logical (not wall) clock
        - Used to order runs without trusting system time
    
    🔁 replay_of:
        - Set only if this run replays another
        - Must reference valid prior run
        - Results must be identical or system halts
    
    ⏰ created_at:
        - Wall-time only for observability
        - Never used for logic
        - Never affects output
    """
    
    # Required fields - no defaults
    aggregation_run_id: AggregationRunID
    pipeline_version: PipelineVersion
    code_hash: CodeHash
    trigger: AggregationTrigger
    scope: AggregationScope
    windows: WindowSet
    input_bounds: InputBounds
    invariants_snapshot: Hash
    clock: LogicalClock
    
    # Optional fields
    replay_of: Optional[AggregationRunID]
    
    # Observability only - never affects output
    created_at: Timestamp = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def __post_init__(self) -> None:
        """
        Validate context is well-formed.
        
        Violations are FATAL - aggregation must fail loudly or not run at all.
        
        Failure modes (all fatal):
        - missing window definition
        - missing invariant snapshot
        - code hash mismatch (checked at runtime)
        - scope ambiguity
        - mutable field access (prevented by frozen dataclass)
        """
        # Validate required string fields are non-empty
        if not self.pipeline_version:
            raise ValueError("pipeline_version cannot be empty")
        if not self.code_hash:
            raise ValueError("code_hash cannot be empty")
        if not self.invariants_snapshot:
            raise ValueError("invariants_snapshot cannot be empty")
        
        # Validate aggregation_run_id is not empty UUID
        if self.aggregation_run_id == UUID('00000000-0000-0000-0000-000000000000'):
            raise ValueError("aggregation_run_id cannot be empty UUID")
        
        # Validate types
        if not isinstance(self.aggregation_run_id, UUID):
            raise TypeError(
                f"aggregation_run_id must be UUID, got {type(self.aggregation_run_id)}"
            )
        if not isinstance(self.trigger, AggregationTrigger):
            raise TypeError(f"trigger must be AggregationTrigger, got {type(self.trigger)}")
        if not isinstance(self.scope, AggregationScope):
            raise TypeError(f"scope must be AggregationScope, got {type(self.scope)}")
        if not isinstance(self.windows, WindowSet):
            raise TypeError(f"windows must be WindowSet, got {type(self.windows)}")
        if not isinstance(self.input_bounds, InputBounds):
            raise TypeError(f"input_bounds must be InputBounds, got {type(self.input_bounds)}")
        if not isinstance(self.clock, LogicalClock):
            raise TypeError(f"clock must be LogicalClock, got {type(self.clock)}")
        
        # Validate replay_of if set
        if self.replay_of is not None:
            if not isinstance(self.replay_of, UUID):
                raise TypeError(f"replay_of must be UUID, got {type(self.replay_of)}")
            if self.replay_of == self.aggregation_run_id:
                raise ValueError("Cannot replay self")
        
        # Validate created_at
        if not isinstance(self.created_at, datetime):
            raise TypeError(f"created_at must be datetime, got {type(self.created_at)}")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
    
    def to_dict(self) -> dict:
        """
        Canonical serialization.
        
        Serialization guarantees:
        - stable field ordering (alphabetical by key)
        - explicit types (no implicit conversions)
        - deterministic encoding (same input → same output)
        - hash stable across languages (UTF-8, canonical JSON)
        
        If two serialized contexts differ, they are different runs.
        
        This method is used for:
        - audit trail persistence
        - replay verification
        - context comparison
        - hash computation
        """
        # Use sorted keys for stable ordering
        result = {
            "aggregation_run_id": str(self.aggregation_run_id),
            "clock": self.clock.to_dict(),
            "code_hash": self.code_hash,
            "created_at": self.created_at.isoformat(),
            "input_bounds": self.input_bounds.to_dict(),
            "invariants_snapshot": self.invariants_snapshot,
            "pipeline_version": self.pipeline_version,
            "replay_of": str(self.replay_of) if self.replay_of else None,
            "scope": self.scope.to_dict(),
            "trigger": str(self.trigger),
            "windows": self.windows.to_dict(),
        }
        
        return result
    
    def to_json(self) -> str:
        """
        Canonical JSON serialization.
        
        Deterministic encoding:
        - sorted keys (alphabetical)
        - no whitespace variations (compact separators)
        - UTF-8 encoding
        - no trailing commas
        - no comments
        
        This ensures:
        - Same context → same JSON string
        - Same JSON string → same hash
        - Hash stable across languages and platforms
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    
    def compute_hash(self) -> Hash:
        """
        Compute content hash of this context.
        
        Used for:
        - audit trail verification
        - replay validation
        - context comparison
        - deterministic identity
        
        Returns stable SHA-256 hash of canonical JSON representation.
        
        Guarantees:
        - Same context → same hash (deterministic)
        - Different context → different hash (collision-resistant)
        - Hash stable across machines, languages, and time
        - No salting or non-deterministic elements
        """
        canonical_json = self.to_json()
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    def __hash__(self) -> int:
        """
        Hash based on run ID for use in sets/dicts.
        
        Note: This is for Python set/dict usage only.
        For cryptographic hashing, use compute_hash().
        """
        return hash(self.aggregation_run_id)
    
    def __eq__(self, other: object) -> bool:
        """
        Equality based on content hash.
        
        Two contexts are equal if their canonical hashes match.
        This ensures:
        - Same context → equal
        - Different context → not equal
        - Deterministic comparison
        """
        if not isinstance(other, AggregationContext):
            return NotImplemented
        return self.compute_hash() == other.compute_hash()
    
    def is_replay(self) -> bool:
        """
        Check if this context represents a replay run.
        
        Returns:
            True if replay_of is set, False otherwise.
        """
        return self.replay_of is not None
    
    def validate_replay_against(self, original: AggregationContext) -> None:
        """
        Validate this replay context against original context.
        
        Rules (all must pass):
        - Must have replay_of set to original's run ID
        - pipeline_version must match
        - code_hash must match
        - scope must match
        - windows must match
        - input_bounds must match
        - invariants_snapshot must match
        
        Results must be identical or system halts.
        
        Args:
            original: The original context being replayed
            
        Raises:
            ValueError: If replay validation fails (fatal error)
        """
        if not self.is_replay():
            raise ValueError("This context is not a replay")
        
        if self.replay_of != original.aggregation_run_id:
            raise ValueError(
                f"replay_of ({self.replay_of}) does not match "
                f"original run ID ({original.aggregation_run_id})"
            )
        
        mismatches = []
        
        if self.pipeline_version != original.pipeline_version:
            mismatches.append(
                f"pipeline_version: {self.pipeline_version} != {original.pipeline_version}"
            )
        
        if self.code_hash != original.code_hash:
            mismatches.append(
                f"code_hash: {self.code_hash} != {original.code_hash}"
            )
        
        if self.scope != original.scope:
            mismatches.append("scope differs")
        
        if self.windows != original.windows:
            mismatches.append("windows differ")
        
        if self.input_bounds != original.input_bounds:
            mismatches.append("input_bounds differ")
        
        if self.invariants_snapshot != original.invariants_snapshot:
            mismatches.append(
                f"invariants_snapshot: {self.invariants_snapshot} != "
                f"{original.invariants_snapshot}"
            )
        
        if mismatches:
            raise ValueError(
                f"Replay validation failed (system halted):\n  " + "\n  ".join(mismatches)
            )


# ============================================================================
# Builder - Safe context construction
# ============================================================================

class AggregationContextBuilder:
    """
    Builder for constructing valid AggregationContext instances.
    
    Enforces required fields and provides validation before construction.
    """
    
    def __init__(self) -> None:
        self._aggregation_run_id: Optional[AggregationRunID] = None
        self._pipeline_version: Optional[PipelineVersion] = None
        self._code_hash: Optional[CodeHash] = None
        self._trigger: Optional[AggregationTrigger] = None
        self._scope: Optional[AggregationScope] = None
        self._windows: Optional[WindowSet] = None
        self._input_bounds: Optional[InputBounds] = None
        self._invariants_snapshot: Optional[Hash] = None
        self._clock: Optional[LogicalClock] = None
        self._replay_of: Optional[AggregationRunID] = None
        self._created_at: Optional[Timestamp] = None
    
    def with_run_id(self, run_id: AggregationRunID) -> AggregationContextBuilder:
        """Set aggregation run ID."""
        self._aggregation_run_id = run_id
        return self
    
    def with_new_run_id(self) -> AggregationContextBuilder:
        """Generate and set a new aggregation run ID."""
        self._aggregation_run_id = uuid4()
        return self
    
    def with_pipeline_version(self, version: PipelineVersion) -> AggregationContextBuilder:
        """Set pipeline version."""
        self._pipeline_version = version
        return self
    
    def with_code_hash(self, code_hash: CodeHash) -> AggregationContextBuilder:
        """Set code hash."""
        self._code_hash = code_hash
        return self
    
    def with_trigger(self, trigger: AggregationTrigger) -> AggregationContextBuilder:
        """Set trigger."""
        self._trigger = trigger
        return self
    
    def with_scope(self, scope: AggregationScope) -> AggregationContextBuilder:
        """Set scope."""
        self._scope = scope
        return self
    
    def with_windows(self, windows: WindowSet) -> AggregationContextBuilder:
        """Set windows."""
        self._windows = windows
        return self
    
    def with_input_bounds(self, bounds: InputBounds) -> AggregationContextBuilder:
        """Set input bounds."""
        self._input_bounds = bounds
        return self
    
    def with_invariants_snapshot(self, snapshot: Hash) -> AggregationContextBuilder:
        """Set invariants snapshot."""
        self._invariants_snapshot = snapshot
        return self
    
    def with_clock(self, clock: LogicalClock) -> AggregationContextBuilder:
        """Set logical clock."""
        self._clock = clock
        return self
    
    def with_replay_of(self, replay_of: Optional[AggregationRunID]) -> AggregationContextBuilder:
        """Set replay_of reference."""
        self._replay_of = replay_of
        return self
    
    def with_created_at(self, created_at: Timestamp) -> AggregationContextBuilder:
        """Set created_at timestamp."""
        self._created_at = created_at
        return self
    
    def build(self) -> AggregationContext:
        """
        Build and validate the AggregationContext.
        
        Returns:
            Immutable, validated AggregationContext
            
        Raises:
            ValueError: If any required field is missing
        """
        missing = []
        
        if self._aggregation_run_id is None:
            missing.append("aggregation_run_id")
        if self._pipeline_version is None:
            missing.append("pipeline_version")
        if self._code_hash is None:
            missing.append("code_hash")
        if self._trigger is None:
            missing.append("trigger")
        if self._scope is None:
            missing.append("scope")
        if self._windows is None:
            missing.append("windows")
        if self._input_bounds is None:
            missing.append("input_bounds")
        if self._invariants_snapshot is None:
            missing.append("invariants_snapshot")
        if self._clock is None:
            missing.append("clock")
        
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")
        
        kwargs = {
            "aggregation_run_id": self._aggregation_run_id,
            "pipeline_version": self._pipeline_version,
            "code_hash": self._code_hash,
            "trigger": self._trigger,
            "scope": self._scope,
            "windows": self._windows,
            "input_bounds": self._input_bounds,
            "invariants_snapshot": self._invariants_snapshot,
            "clock": self._clock,
            "replay_of": self._replay_of,
        }
        
        if self._created_at is not None:
            kwargs["created_at"] = self._created_at
        
        return AggregationContext(**kwargs)


# ============================================================================
# Utilities
# ============================================================================

def compute_code_hash(*code_strings: str) -> CodeHash:
    """
    Compute deterministic hash of code strings.
    
    Used for hashing:
        - aggregation runner code
        - counter definitions
        - window logic
    
    Args:
        *code_strings: Code content to hash
        
    Returns:
        SHA-256 hash of concatenated code
    """
    combined = "".join(code_strings)
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()


def compute_fact_id_set_hash(fact_ids: set[str]) -> Hash:
    """
    Compute deterministic hash of fact ID set.
    
    Args:
        fact_ids: Set of fact IDs
        
    Returns:
        SHA-256 hash of sorted fact IDs
    """
    sorted_ids = sorted(fact_ids)
    combined = "".join(sorted_ids)
    return hashlib.sha256(combined.encode('utf-8')).hexdigest()


def deserialize_context(data: dict) -> AggregationContext:
    """
    Deserialize AggregationContext from dictionary.
    
    Args:
        data: Dictionary from to_dict()
        
    Returns:
        Reconstructed AggregationContext
        
    Raises:
        ValueError: If data is malformed
    """
    # Parse UUIDs
    aggregation_run_id = UUID(data["aggregation_run_id"])
    replay_of = UUID(data["replay_of"]) if data.get("replay_of") else None
    
    # Parse trigger
    trigger = AggregationTrigger(data["trigger"])
    
    # Parse scope
    scope = AggregationScope(
        schemas=frozenset(data["scope"]["schemas"]),
        entity_types=frozenset(data["scope"]["entity_types"]),
        allowed_sources=frozenset(data["scope"]["allowed_sources"]),
    )
    
    # Parse windows
    windows = WindowSet(
        windows=frozenset(
            Window(
                window_id=w["window_id"],
                start_time=datetime.fromisoformat(w["start_time"]),
                end_time=datetime.fromisoformat(w["end_time"]),
                alignment=w["alignment"],
                version=w["version"],
            )
            for w in data["windows"]["windows"]
        )
    )
    
    # Parse input bounds
    input_bounds = InputBounds(
        max_events=data["input_bounds"]["max_events"],
        min_event_time=datetime.fromisoformat(data["input_bounds"]["min_event_time"]),
        max_event_time=datetime.fromisoformat(data["input_bounds"]["max_event_time"]),
        fact_id_set_hash=data["input_bounds"]["fact_id_set_hash"],
    )
    
    # Parse clock
    clock = LogicalClock(
        epoch=data["clock"]["epoch"],
        tick=data["clock"]["tick"],
    )
    
    # Parse created_at
    created_at = datetime.fromisoformat(data["created_at"])
    
    return AggregationContext(
        aggregation_run_id=aggregation_run_id,
        pipeline_version=data["pipeline_version"],
        code_hash=data["code_hash"],
        trigger=trigger,
        scope=scope,
        windows=windows,
        input_bounds=input_bounds,
        invariants_snapshot=data["invariants_snapshot"],
        clock=clock,
        replay_of=replay_of,
        created_at=created_at,
    )


def deserialize_context_from_json(json_str: str) -> AggregationContext:
    """
    Deserialize AggregationContext from JSON string.
    
    Args:
        json_str: JSON string from to_json()
        
    Returns:
        Reconstructed AggregationContext
    """
    data = json.loads(json_str)
    return deserialize_context(data)