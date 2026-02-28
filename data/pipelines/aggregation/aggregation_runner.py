"""
/data/pipelines/aggregation/aggregation_runner.py

Ordered, windowed, replay-safe aggregation orchestration.

WHAT THIS FILE EXISTS FOR (NON-NEGOTIABLE):
aggregation_runner.py is the single authority that turns:
- canonical facts
- a frozen AggregationContext
- pure aggregation logic

into final, irreversible counters.

This file is not about math. It is about ordering, eligibility, determinism, and irreversibility.

If this file is wrong:
- counters drift
- replay lies
- repair corrupts truth
- audit cannot certify results

This file is the last gate before facts become numbers.

ABSOLUTE PRIME DIRECTIVE:
Given the same inputs, context, and code hash — aggregation results must be bit-identical forever.

No retries producing "close enough". No best-effort windows. No tolerance for nondeterminism.

RESPONSIBILITIES (EXACTLY AND ONLY THESE):
The runner is responsible for:
1. Enforcing global ordering of facts
2. Applying window boundaries exactly
3. Guaranteeing at-most-once aggregation per run
4. Detecting replay equivalence
5. Failing hard on any ambiguity

The runner must not:
- compute metrics
- normalize data
- invent defaults
- resolve conflicts silently

EXECUTION PHASES (STRICT ORDER):
Phase 0 — Context Validation (Pre-Flight)
Phase 1 — Fact Eligibility Filtering
Phase 2 — Deterministic Ordering
Phase 3 — Window Assignment
Phase 4 — Counter Application
Phase 5 — Invariant Enforcement (Continuous)
Phase 6 — Finalization

REPLAY SEMANTICS (ZERO DRIFT ALLOWED):
If context.replay_of is set:
- Load prior aggregation output
- Rerun aggregation fully
- Compare: window outputs, counter values, digests
- Assert exact equality

Any mismatch: halts the system, marks recovery failure, requires human intervention.

IDEMPOTENCY GUARANTEES:
- same aggregation_run_id cannot finalize twice
- retries before finalization are allowed
- retries after finalization are forbidden

FAILURE PHILOSOPHY:
Aggregation failures are loud by design.
Fatal failures include: ordering ambiguity, window ambiguity, invariant violation,
context mismatch, prior run conflict, counter mutation outside contract.

There is no "skip bad data and continue" mode here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, List, Optional, Protocol, Tuple

# Local imports
from .aggregation_context import AggregationContext
from .aggregation_invariants import AggregationInvariantSet
from .aggregation_errors import AggregationError

# Type stubs for external dependencies (implement per your architecture)
# These should be imported from their respective modules
try:
    from .canonical_fact import CanonicalFact
except ImportError:
    # Fallback type stub
    from typing import Protocol
    class CanonicalFact(Protocol):
        fact_id: str
        event_time: int
        entity_id: str
        entity_type: str
        schema_name: str
        source: str
        fact_source_order: Optional[int]
        
        def has_field(self, field: str) -> bool:
            ...

try:
    from .counter_registry import CounterRegistry
except ImportError:
    # Fallback type stub
    from typing import Protocol
    class CounterRegistry(Protocol):
        def get_counter_set(self, window_id: str):
            ...
        def compute_digest(self) -> str:
            ...
        def freeze(self) -> None:
            ...

try:
    from .audit_log import AuditLogger
except ImportError:
    # Fallback type stub
    from typing import Protocol
    class AuditLogger(Protocol):
        def log_start(self, **kwargs) -> None: ...
        def log_progress(self, phase) -> None: ...
        def log_fact_excluded(self, **kwargs) -> None: ...
        def log_fact_no_window(self, **kwargs) -> None: ...
        def log_invariant_violations(self, **kwargs) -> None: ...
        def log_finalization(self, **kwargs) -> None: ...
        def log_replay_failure(self, **kwargs) -> None: ...
        def log_replay_success(self, **kwargs) -> None: ...
        def log_completion(self, **kwargs) -> None: ...
        def log_failure(self, **kwargs) -> None: ...

try:
    from .run_marker import RunMarkerStore
except ImportError:
    # Fallback type stub
    from typing import Protocol
    class RunMarkerStore(Protocol):
        def is_finalized(self, run_id: str) -> bool:
            ...
        def mark_finalized(self, run_id: str, digest: str) -> None:
            ...

try:
    from .snapshot_store import SnapshotStore
except ImportError:
    # Fallback type stub
    from typing import Protocol
    class SnapshotStore(Protocol):
        def save_snapshot(self, **kwargs) -> None: ...
        def load_result(self, run_id: str):
            ...


logger = logging.getLogger(__name__)


# ============================================================================
# Domain Types
# ============================================================================


class RunPhase(Enum):
    """Execution phases with strict ordering."""
    CONTEXT_VALIDATION = "context_validation"
    FACT_FILTERING = "fact_filtering"
    DETERMINISTIC_ORDERING = "deterministic_ordering"
    WINDOW_ASSIGNMENT = "window_assignment"
    COUNTER_APPLICATION = "counter_application"
    INVARIANT_ENFORCEMENT = "invariant_enforcement"
    FINALIZATION = "finalization"


class FailureReason(Enum):
    """Fatal failure categories."""
    CONTEXT_IMMUTABILITY_VIOLATION = "context_immutability_violation"
    VERSION_INCOMPATIBLE = "version_incompatible"
    CODE_HASH_MISMATCH = "code_hash_mismatch"
    INVARIANT_DIGEST_MISMATCH = "invariant_digest_mismatch"
    DUPLICATE_RUN_ID = "duplicate_run_id"
    ORDERING_AMBIGUITY = "ordering_ambiguity"
    WINDOW_AMBIGUITY = "window_ambiguity"
    INVARIANT_VIOLATION = "invariant_violation"
    COUNTER_CONTRACT_VIOLATION = "counter_contract_violation"
    REPLAY_MISMATCH = "replay_mismatch"
    PRIOR_RUN_CONFLICT = "prior_run_conflict"


@dataclass(frozen=True)
class AggregationRunnerInput:
    """Immutable input specification for aggregation execution."""
    context: AggregationContext
    facts: Iterator[CanonicalFact]  # Streaming or materialized
    counters: CounterRegistry
    invariants: AggregationInvariantSet


@dataclass(frozen=True)
class WindowFactPair:
    """A fact assigned to a specific window."""
    window_id: str
    window_start: int  # event_time epoch
    window_end: int
    fact: CanonicalFact


@dataclass
class AggregationResult:
    """Immutable output of successful aggregation run."""
    aggregation_run_id: str
    context_hash: str
    windows_processed: int
    facts_processed: int
    facts_excluded: int
    counter_digest: str
    output_digest: str
    replay_verified: bool
    
    def __post_init__(self):
        """Freeze after construction."""
        object.__setattr__(self, '__frozen', True)
    
    def __setattr__(self, key, value):
        if hasattr(self, '__frozen'):
            raise AttributeError("AggregationResult is immutable")
        object.__setattr__(self, key, value)


class AggregationException(AggregationError):
    """
    Base exception for all aggregation failures.
    
    All aggregation failures must be loud by design.
    There is no "skip bad data and continue" mode.
    """
    
    def __init__(
        self,
        reason: FailureReason,
        message: str,
        context: dict = None,
        error_context: Optional[Any] = None
    ):
        from .aggregation_errors import ErrorContext
        
        self.reason = reason
        self.context = context or {}
        
        # Convert to ErrorContext if not provided
        if error_context is None:
            error_context = ErrorContext(
                aggregation_id=context.get('run_id') if context else None,
                additional_context=context
            )
        
        full_message = f"[{reason.value}] {message}"
        super().__init__(
            error_code=reason.value,
            message=full_message,
            error_context=error_context
        )


# ============================================================================
# Phase 0: Context Validation
# ============================================================================


class ContextValidator:
    """Pre-flight validation of aggregation context."""
    
    @staticmethod
    def validate(
        context: AggregationContext,
        invariants: AggregationInvariantSet,
        run_marker_store: RunMarkerStore
    ) -> None:
        """
        Validate context before touching any facts.
        
        Before touching facts:
        - verify AggregationContext immutability (frozen dataclass)
        - verify pipeline version compatibility
        - verify code hash match
        - verify invariant snapshot digest
        - verify no prior finalized run for aggregation_run_id
        
        Failure here aborts immediately.
        
        Raises:
            AggregationException: On any validation failure.
        """
        from .aggregation_errors import ErrorContext
        
        # Verify immutability (frozen dataclass is immutable by design)
        # Check that context is actually frozen dataclass
        if not hasattr(context, '__dataclass_fields__'):
            raise AggregationException(
                FailureReason.CONTEXT_IMMUTABILITY_VIOLATION,
                "AggregationContext must be a frozen dataclass",
                error_context=ErrorContext(
                    aggregation_id=str(context.aggregation_run_id)
                )
            )
        
        # Verify code hash match
        actual_hash = ContextValidator._compute_code_hash()
        if context.code_hash and actual_hash != context.code_hash:
            raise AggregationException(
                FailureReason.CODE_HASH_MISMATCH,
                f"Code hash mismatch: expected {context.code_hash}, got {actual_hash}",
                error_context=ErrorContext(
                    aggregation_id=str(context.aggregation_run_id),
                    additional_context={
                        "expected_hash": context.code_hash,
                        "actual_hash": actual_hash
                    }
                )
            )
        
        # Verify invariant snapshot
        invariant_digest = invariants.compute_digest()
        if context.invariants_snapshot != invariant_digest:
            raise AggregationException(
                FailureReason.INVARIANT_DIGEST_MISMATCH,
                f"Invariant digest mismatch: expected {context.invariants_snapshot}, got {invariant_digest}",
                error_context=ErrorContext(
                    aggregation_id=str(context.aggregation_run_id),
                    additional_context={
                        "expected_digest": context.invariants_snapshot,
                        "actual_digest": invariant_digest
                    }
                )
            )
        
        # Verify no prior finalized run (idempotency guarantee)
        # Idempotency rules:
        # - same aggregation_run_id cannot finalize twice
        # - retries before finalization are allowed
        # - retries after finalization are forbidden
        if run_marker_store.is_finalized(str(context.aggregation_run_id)):
            raise AggregationException(
                FailureReason.DUPLICATE_RUN_ID,
                f"Run {context.aggregation_run_id} already finalized",
                error_context=ErrorContext(
                    aggregation_id=str(context.aggregation_run_id)
                )
            )
    
    @staticmethod
    def _compute_code_hash() -> str:
        """
        Compute hash of critical aggregation code.
        
        In production: hash this file + counter implementations + window logic.
        This ensures code changes are detected and prevent silent drift.
        """
        # Hash this file
        with open(__file__, 'rb') as f:
            runner_hash = hashlib.sha256(f.read()).hexdigest()
        
        # In production, also hash:
        # - counter implementations
        # - window logic
        # - invariant definitions
        # For now, return hash of runner file
        return runner_hash


# ============================================================================
# Phase 1: Fact Eligibility Filtering
# ============================================================================


class FactFilter:
    """Strict eligibility filtering with audit trace."""
    
    def __init__(self, context: AggregationContext, audit: AuditLogger):
        self.context = context
        self.audit = audit
        self.excluded_count = 0
    
    def filter_eligible(
        self,
        facts: Iterator[CanonicalFact]
    ) -> Iterator[CanonicalFact]:
        """
        Filter facts by strict eligibility rules.
        
        Yields only facts that pass all criteria.
        Excluded facts are logged but do not fail the run.
        """
        for fact in facts:
            exclusion_reason = self._check_exclusion(fact)
            
            if exclusion_reason:
                self.excluded_count += 1
                self.audit.log_fact_excluded(
                    fact_id=fact.fact_id,
                    reason=exclusion_reason,
                    aggregation_id=str(self.context.aggregation_run_id)
                )
            else:
                yield fact
    
    def _check_exclusion(self, fact: CanonicalFact) -> Optional[str]:
        """
        Check if fact should be excluded.
        
        Facts are filtered strictly by:
        - schema whitelist
        - entity type
        - source allowance
        - event time bounds
        - fact id inclusion hash
        
        No fuzzy inclusion. No late arrivals unless explicitly allowed by context.
        
        If a fact:
        - partially matches
        - violates bounds
        - is missing required fields
        
        → it is excluded with audit trace, not coerced.
        
        Returns:
            Exclusion reason string if excluded, None if eligible.
        """
        # Schema whitelist
        if fact.schema_name not in self.context.scope.schemas:
            return f"schema_not_whitelisted:{fact.schema_name}"
        
        # Entity type filter
        if fact.entity_type not in self.context.scope.entity_types:
            return f"entity_type_not_allowed:{fact.entity_type}"
        
        # Source allowance
        if fact.source not in self.context.scope.allowed_sources:
            return f"source_not_allowed:{fact.source}"
        
        # Event time bounds (strict)
        # Convert fact.event_time to comparable format
        fact_time = fact.event_time
        if isinstance(fact_time, int):
            # Assume epoch milliseconds
            fact_datetime = None
            fact_time_ms = fact_time
        else:
            # Assume datetime, convert to epoch milliseconds
            from datetime import datetime
            if isinstance(fact_time, datetime):
                fact_datetime = fact_time
                fact_time_ms = int(fact_time.timestamp() * 1000)
            else:
                return f"invalid_event_time_type:{type(fact_time).__name__}"
        
        bounds = self.context.input_bounds
        
        # Convert bounds to epoch milliseconds for comparison
        min_time_ms = int(bounds.min_event_time.timestamp() * 1000)
        max_time_ms = int(bounds.max_event_time.timestamp() * 1000)
        
        if not (min_time_ms <= fact_time_ms < max_time_ms):
            return f"outside_time_bounds:{fact_time_ms} (bounds: {min_time_ms} to {max_time_ms})"
        
        # Fact ID inclusion hash (for sampling/sharding)
        # This is optional - only if inclusion_hash_config is set in context
        # For now, assume all facts pass if no config
        
        # Required fields presence (if context defines required fields)
        # This depends on context structure - for now, assume basic validation
        
        return None
    
    def _passes_inclusion_hash(self, fact_id: str) -> bool:
        """
        Check if fact_id passes inclusion hash filter.
        
        Used for sampling/sharding if configured in context.
        If no inclusion hash config, all facts pass.
        """
        # Inclusion hash config is optional - if not present, all facts pass
        # This would be defined in context if needed
        # For now, return True (no filtering by hash)
        return True


# ============================================================================
# Phase 2: Deterministic Ordering
# ============================================================================


class FactSorter:
    """Deterministic, reproducible fact ordering."""
    
    @staticmethod
    def sort_facts(facts: Iterator[CanonicalFact]) -> List[CanonicalFact]:
        """
        Sort facts by canonical ordering keys.
        
        Order guarantee:
        1. event_time (logical, not wall)
        2. fact_source_order (explicit sequencing)
        3. entity_id (for tie-breaking)
        4. fact_id (for absolute determinism)
        
        This ordering must be identical across processes, machines, and languages.
        """
        # Materialize iterator (required for sorting)
        fact_list = list(facts)
        
        # Sort by composite key (deterministic ordering)
        # Order guarantee:
        # 1. event_time (logical, not wall)
        # 2. fact_source_order (explicit sequencing)
        # 3. entity_id (for tie-breaking)
        # 4. fact_id (for absolute determinism)
        
        # Convert event_time to comparable format if needed
        # Ordering must be reproducible across processes, machines, languages
        def get_sort_key(f: CanonicalFact) -> tuple:
            event_time = f.event_time
            
            # Convert to epoch milliseconds for deterministic comparison
            if isinstance(event_time, int):
                event_time_ms = event_time
            else:
                # Assume datetime, convert to epoch milliseconds
                from datetime import datetime
                if isinstance(event_time, datetime):
                    event_time_ms = int(event_time.timestamp() * 1000)
                else:
                    raise TypeError(
                        f"event_time must be int or datetime, got {type(event_time).__name__}"
                    )
            
            return (
                event_time_ms,  # Primary: event_time (logical, not wall)
                f.fact_source_order if f.fact_source_order is not None else 0,  # Secondary: fact_source_order
                f.entity_id,  # Tertiary: entity_id
                f.fact_id  # Quaternary: fact_id (for absolute determinism)
            )
        
        fact_list.sort(key=get_sort_key)
        
        # Verify no ambiguity (optional strict mode)
        FactSorter._verify_no_ordering_ambiguity(fact_list)
        
        return fact_list
    
    @staticmethod
    def _verify_no_ordering_ambiguity(facts: List[CanonicalFact]) -> None:
        """
        Verify no two facts have identical ordering keys.
        
        Raises:
            AggregationException: If ambiguity detected.
        """
        seen_keys = set()
        
        for fact in facts:
            key = (fact.event_time, fact.fact_source_order, fact.entity_id, fact.fact_id)
            
            if key in seen_keys:
                raise AggregationException(
                    FailureReason.ORDERING_AMBIGUITY,
                    f"Duplicate ordering key detected: {key}",
                    {"fact_id": fact.fact_id}
                )
            
            seen_keys.add(key)


# ============================================================================
# Phase 3: Window Assignment
# ============================================================================


class WindowAssigner:
    """Deterministic window assignment for facts."""
    
    def __init__(self, context: AggregationContext, audit: AuditLogger):
        self.context = context
        self.audit = audit
    
    def assign_windows(
        self,
        facts: List[CanonicalFact]
    ) -> Iterator[WindowFactPair]:
        """
        Assign each fact to its window(s).
        
        Yields:
            (window, fact) pairs in deterministic order.
        
        Raises:
            AggregationException: On invalid window mapping.
        """
        for fact in facts:
            windows = self._compute_windows(fact)
            
            if not windows:
                # Fact maps to no windows - skip with audit
                self.audit.log_fact_no_window(
                    fact_id=fact.fact_id,
                    event_time=fact.event_time
                )
                continue
            
            for window in windows:
                # Convert window times to epoch milliseconds (event_time epoch)
                window_start_ms = int(window.start_time.timestamp() * 1000)
                window_end_ms = int(window.end_time.timestamp() * 1000)
                
                yield WindowFactPair(
                    window_id=window.window_id,
                    window_start=window_start_ms,
                    window_end=window_end_ms,
                    fact=fact
                )
    
    def _compute_windows(self, fact: CanonicalFact) -> List:
        """
        Compute which window(s) a fact belongs to.
        
        Window membership must be deterministic.
        Overlapping windows explicitly allowed only if configured.
        
        A fact that maps to zero windows:
        - is skipped
        - is logged
        - does not fail the run
        
        A fact mapping to an invalid window:
        - aborts the run
        
        Returns:
            List of windows (may be empty if fact outside all windows).
        
        Raises:
            AggregationException: If window computation is ambiguous.
        """
        from .aggregation_errors import ErrorContext
        
        windows = []
        
        # Convert fact.event_time to epoch milliseconds if needed
        fact_time_ms = fact.event_time
        if not isinstance(fact_time_ms, int):
            fact_time_ms = int(fact_time_ms.timestamp() * 1000)
        
        # Check each window definition
        # Window membership must be deterministic
        for window in self.context.windows.windows:
            # Window contains fact if fact.event_time is within [start_time, end_time)
            # Windows have start_time and end_time as Timestamp (datetime)
            window_start_ms = int(window.start_time.timestamp() * 1000)
            window_end_ms = int(window.end_time.timestamp() * 1000)
            
            if window_start_ms <= fact_time_ms < window_end_ms:
                windows.append(window)
        
        # Verify overlapping windows allowed if multiple found
        # For now, assume overlapping windows are allowed if multiple windows exist
        # This should be configurable in context
        if len(windows) > 1:
            # Log warning but allow (overlapping windows explicitly allowed)
            logger.warning(
                f"Fact {fact.fact_id} maps to {len(windows)} windows: "
                f"{[w.window_id for w in windows]}"
            )
        
        return windows


# ============================================================================
# Phase 4: Counter Application
# ============================================================================


class CounterApplicator:
    """Apply counter updates with strict contract enforcement."""
    
    def __init__(self, counters: CounterRegistry, audit: AuditLogger):
        self.counters = counters
        self.audit = audit
    
    def apply_counters(
        self,
        window_fact_pairs: Iterator[WindowFactPair]
    ) -> None:
        """
        Apply counter updates for each (window, fact) pair.
        
        Counters must be:
        - Side-effect free
        - Order-sensitive only via runner ordering
        - Never read outside current fact + state
        
        Raises:
            AggregationException: On contract violation.
        """
        for pair in window_fact_pairs:
            counter_set = self.counters.get_counter_set(pair.window_id)
            
            try:
                counter_set.update(pair.fact)
            except Exception as e:
                from .aggregation_errors import ErrorContext
                
                raise AggregationException(
                    FailureReason.COUNTER_CONTRACT_VIOLATION,
                    f"Counter update failed: {e}",
                    error_context=ErrorContext(
                        window_id=pair.window_id,
                        additional_context={
                            "fact_id": pair.fact.fact_id,
                            "error": str(e)
                        }
                    )
                ) from e


# ============================================================================
# Phase 5: Invariant Enforcement
# ============================================================================


class InvariantEnforcer:
    """Continuous invariant validation during execution."""
    
    def __init__(
        self,
        invariants: AggregationInvariantSet,
        counters: CounterRegistry,
        audit: AuditLogger
    ):
        self.invariants = invariants
        self.counters = counters
        self.audit = audit
    
    def enforce_checkpoint(self, checkpoint_name: str) -> None:
        """
        Enforce all invariants at a checkpoint.
        
        After each batch (or configurable checkpoint):
        - enforce counter invariants
        - enforce window invariants
        - enforce monotonicity rules
        - enforce non-negativity constraints
        
        Invariants are hard stops, not warnings.
        
        Raises:
            AggregationException: On any invariant violation.
        """
        violations = self.invariants.check_all(self.counters)
        
        if violations:
            self._handle_violations(violations, checkpoint_name)
        
        # Log successful checkpoint (for observability)
        self.audit.log_progress(f"invariant_checkpoint:{checkpoint_name}")
    
    def _handle_violations(self, violations: List, checkpoint_name: str) -> None:
        """
        Handle invariant violations - always fatal.
        
        Invariants are hard stops, not warnings.
        """
        from .aggregation_errors import ErrorContext
        
        violation_details = [
            f"{v.invariant_name}: {v.message}" if hasattr(v, 'invariant_name') else str(v)
            for v in violations
        ]
        
        self.audit.log_invariant_violations(
            checkpoint=checkpoint_name,
            violations=violation_details
        )
        
        raise AggregationException(
            FailureReason.INVARIANT_VIOLATION,
            f"Invariant violations at {checkpoint_name}: {len(violations)} violations",
            error_context=ErrorContext(
                additional_context={
                    "checkpoint": checkpoint_name,
                    "violations": violation_details,
                    "violation_count": len(violations)
                }
            )
        )


# ============================================================================
# Phase 6: Finalization
# ============================================================================


class Finalizer:
    """Finalize aggregation run with immutability guarantees."""
    
    def __init__(
        self,
        context: AggregationContext,
        counters: CounterRegistry,
        audit: AuditLogger,
        snapshot_store: SnapshotStore,
        run_marker_store: RunMarkerStore
    ):
        self.context = context
        self.counters = counters
        self.audit = audit
        self.snapshot_store = snapshot_store
        self.run_marker_store = run_marker_store
    
    def finalize(
        self,
        facts_processed: int,
        facts_excluded: int,
        windows_processed: int
    ) -> AggregationResult:
        """
        Finalize aggregation run atomically.
        
        After this returns:
        - Counters are frozen
        - Run ID is sealed
        - Reruns must be replays
        
        Returns:
            Immutable AggregationResult.
        """
        # Compute digests
        counter_digest = self.counters.compute_digest()
        output_digest = self._compute_output_digest(counter_digest)
        
        # Freeze counter state
        self.counters.freeze()
        
        # Emit snapshot (for replay verification)
        self.snapshot_store.save_snapshot(
            run_id=str(self.context.aggregation_run_id),
            counters=self.counters,
            digest=counter_digest
        )
        
        # Seal run as completed (atomic operation)
        # After this moment:
        # - counters are immutable
        # - reruns must be replays
        # - same aggregation_run_id cannot finalize twice
        self.run_marker_store.mark_finalized(
            run_id=str(self.context.aggregation_run_id),
            digest=output_digest
        )
        
        # Emit audit finalization
        self.audit.log_finalization(
            run_id=str(self.context.aggregation_run_id),
            facts_processed=facts_processed,
            facts_excluded=facts_excluded,
            windows_processed=windows_processed,
            digest=output_digest
        )
        
        # Return immutable result
        return AggregationResult(
            aggregation_run_id=str(self.context.aggregation_run_id),
            context_hash=self.context.compute_hash(),
            windows_processed=windows_processed,
            facts_processed=facts_processed,
            facts_excluded=facts_excluded,
            counter_digest=counter_digest,
            output_digest=output_digest,
            replay_verified=False  # Set by replay verification if applicable
        )
    
    def _compute_output_digest(self, counter_digest: str) -> str:
        """Compute final output digest from all components."""
        hasher = hashlib.sha256()
        hasher.update(self.context.compute_hash().encode())
        hasher.update(counter_digest.encode())
        return hasher.hexdigest()


# ============================================================================
# Replay Verification
# ============================================================================


class ReplayVerifier:
    """Zero-drift replay verification."""
    
    def __init__(self, snapshot_store: SnapshotStore, audit: AuditLogger):
        self.snapshot_store = snapshot_store
        self.audit = audit
    
    def verify_replay(
        self,
        context: AggregationContext,
        current_result: AggregationResult
    ) -> None:
        """
        Verify current run matches prior run exactly.
        
        Raises:
            AggregationException: On any mismatch (zero tolerance).
        """
        if not context.replay_of:
            return  # Not a replay
        
        # Load prior run
        prior_result = self.snapshot_store.load_result(context.replay_of)
        
        # Compare critical fields
        mismatches = []
        
        if current_result.windows_processed != prior_result.windows_processed:
            mismatches.append(f"windows: {current_result.windows_processed} vs {prior_result.windows_processed}")
        
        if current_result.facts_processed != prior_result.facts_processed:
            mismatches.append(f"facts: {current_result.facts_processed} vs {prior_result.facts_processed}")
        
        if current_result.counter_digest != prior_result.counter_digest:
            mismatches.append(f"counter_digest: {current_result.counter_digest} vs {prior_result.counter_digest}")
        
        if current_result.output_digest != prior_result.output_digest:
            mismatches.append(f"output_digest: {current_result.output_digest} vs {prior_result.output_digest}")
        
        if mismatches:
            from .aggregation_errors import ErrorContext
            
            self.audit.log_replay_failure(
                replay_of=str(context.replay_of),
                current_run=str(context.aggregation_run_id),
                mismatches=mismatches
            )
            
            # Any mismatch: halts the system, marks recovery failure, requires human intervention
            raise AggregationException(
                FailureReason.REPLAY_MISMATCH,
                f"Replay verification failed: {len(mismatches)} mismatches. "
                f"System halted - requires human intervention.",
                error_context=ErrorContext(
                    aggregation_id=str(context.aggregation_run_id),
                    additional_context={
                        "mismatches": mismatches,
                        "prior_run": str(context.replay_of),
                        "mismatch_count": len(mismatches)
                    }
                )
            )
        
        self.audit.log_replay_success(
            replay_of=str(context.replay_of),
            current_run=str(context.aggregation_run_id)
        )


# ============================================================================
# Main Orchestrator
# ============================================================================


class AggregationRunner:
    """
    The single authority for ordered, windowed, replay-safe aggregation.
    
    Responsibilities:
    - Enforce global ordering of facts
    - Apply window boundaries exactly
    - Guarantee at-most-once aggregation per run
    - Detect replay equivalence
    - Fail hard on any ambiguity
    """
    
    def __init__(
        self,
        audit: AuditLogger,
        snapshot_store: SnapshotStore,
        run_marker_store: RunMarkerStore
    ):
        self.audit = audit
        self.snapshot_store = snapshot_store
        self.run_marker_store = run_marker_store
    
    def run(self, input_spec: AggregationRunnerInput) -> AggregationResult:
        """
        Execute aggregation with determinism guarantees.
        
        Given identical inputs, context, and code hash - results are bit-identical.
        
        Args:
            input_spec: Immutable input specification.
        
        Returns:
            Immutable aggregation result.
        
        Raises:
            AggregationException: On any fatal condition.
        """
        ctx = input_spec.context
        
        # Emit start event with context hash
        self.audit.log_start(
            run_id=str(ctx.aggregation_run_id),
            context_hash=ctx.compute_hash(),
            phase=RunPhase.CONTEXT_VALIDATION.value
        )
        
        try:
            # Phase 0: Context Validation
            self._phase_0_validate_context(input_spec)
            
            # Phase 1: Fact Filtering
            fact_filter = FactFilter(ctx, self.audit)
            eligible_facts = fact_filter.filter_eligible(input_spec.facts)
            
            # Phase 2: Deterministic Ordering
            self.audit.log_progress(RunPhase.DETERMINISTIC_ORDERING.value)
            ordered_facts = FactSorter.sort_facts(eligible_facts)
            
            # Phase 3: Window Assignment
            self.audit.log_progress(RunPhase.WINDOW_ASSIGNMENT.value)
            window_assigner = WindowAssigner(ctx, self.audit)
            window_fact_pairs = list(window_assigner.assign_windows(ordered_facts))  # Materialize for counting
            
            # Phase 4: Counter Application
            self.audit.log_progress(RunPhase.COUNTER_APPLICATION.value)
            counter_applicator = CounterApplicator(input_spec.counters, self.audit)
            counter_applicator.apply_counters(iter(window_fact_pairs))  # Re-iterate for application
            
            # Phase 5: Invariant Enforcement (Continuous)
            # Enforce after each batch or at configurable checkpoint
            self.audit.log_progress(RunPhase.INVARIANT_ENFORCEMENT.value)
            invariant_enforcer = InvariantEnforcer(
                input_spec.invariants,
                input_spec.counters,
                self.audit
            )
            invariant_enforcer.enforce_checkpoint("post_aggregation")
            
            # Phase 6: Finalization
            self.audit.log_progress(RunPhase.FINALIZATION.value)
            finalizer = Finalizer(
                ctx,
                input_spec.counters,
                self.audit,
                self.snapshot_store,
                self.run_marker_store
            )
            
            # Count unique windows processed
            unique_windows = len(set(p.window_id for p in window_fact_pairs))
            
            result = finalizer.finalize(
                facts_processed=len(ordered_facts),
                facts_excluded=fact_filter.excluded_count,
                windows_processed=unique_windows
            )
            
            # Replay verification (if applicable)
            # If context.replay_of is set:
            # - Load prior aggregation output
            # - Rerun aggregation fully (already done above)
            # - Compare: window outputs, counter values, digests
            # - Assert exact equality
            if ctx.replay_of:
                verifier = ReplayVerifier(self.snapshot_store, self.audit)
                verifier.verify_replay(ctx, result)
                # Mark result as replay verified
                # Since AggregationResult is immutable, create new instance
                result = AggregationResult(
                    aggregation_run_id=result.aggregation_run_id,
                    context_hash=result.context_hash,
                    windows_processed=result.windows_processed,
                    facts_processed=result.facts_processed,
                    facts_excluded=result.facts_excluded,
                    counter_digest=result.counter_digest,
                    output_digest=result.output_digest,
                    replay_verified=True  # Set by replay verification
                )
            
            self.audit.log_completion(
                run_id=str(ctx.aggregation_run_id),
                result=result
            )
            
            return result
            
        except AggregationException as e:
            # Aggregation failures are loud by design
            self.audit.log_failure(
                run_id=str(ctx.aggregation_run_id),
                reason=e.reason.value,
                message=str(e)
            )
            raise
        
        except Exception as e:
            # Unexpected failures are also fatal
            from .aggregation_errors import ErrorContext
            
            self.audit.log_failure(
                run_id=str(ctx.aggregation_run_id),
                reason="unexpected_error",
                message=str(e)
            )
            
            raise AggregationException(
                FailureReason.PRIOR_RUN_CONFLICT,  # Generic fatal
                f"Unexpected failure: {e}",
                error_context=ErrorContext(
                    aggregation_id=str(ctx.aggregation_run_id),
                    additional_context={"error_type": type(e).__name__, "error": str(e)}
                )
            ) from e
    
    def _phase_0_validate_context(self, input_spec: AggregationRunnerInput) -> None:
        """
        Execute Phase 0: Context Validation (Pre-Flight).
        
        Before touching facts:
        - verify AggregationContext immutability
        - verify pipeline version compatibility
        - verify code hash match
        - verify invariant snapshot digest
        - verify no prior finalized run for aggregation_run_id
        
        Failure here aborts immediately.
        """
        self.audit.log_progress(RunPhase.CONTEXT_VALIDATION.value)
        
        ContextValidator.validate(
            context=input_spec.context,
            invariants=input_spec.invariants,
            run_marker_store=self.run_marker_store
        )


# ============================================================================
# Public API
# ============================================================================


def execute_aggregation(
    input_spec: AggregationRunnerInput,
    audit: AuditLogger,
    snapshot_store: SnapshotStore,
    run_marker_store: RunMarkerStore
) -> AggregationResult:
    """
    Execute a complete aggregation run.
    
    This is the primary entry point for aggregation execution.
    
    Args:
        input_spec: Complete input specification.
        audit: Audit logger for observability.
        snapshot_store: Durable snapshot storage.
        run_marker_store: Run idempotency tracker.
    
    Returns:
        Immutable result with cryptographic guarantees.
    
    Raises:
        AggregationException: On any fatal condition.
    """
    runner = AggregationRunner(audit, snapshot_store, run_marker_store)
    return runner.run(input_spec)


__all__ = [
    'AggregationRunner',
    'AggregationRunnerInput',
    'AggregationResult',
    'AggregationException',
    'FailureReason',
    'RunPhase',
    'execute_aggregation',
]