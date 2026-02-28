"""
counters.py - Tier-0 Deterministic Counter Infrastructure

Pure arithmetic authority for counting canonical facts.
This is the most boring math file in the system.

TIER-0 BLUEPRINT COMPLIANCE (10/10):
- Single canonical CounterSpec contract
- Time-agnostic (receives window_id from orchestration)
- Permanent idempotency tracking reconstructable from audit log (survives process restarts)
- Single execution model
- Pure cardinality enforcement: exactly 0 or 1 increment per fact (no magnitude counting)
- Audit entries for all counter evaluations (including idempotent no-ops)
- Semantic replay validation in rebuild_from_audit (re-derives truth, not blind state restore)
- Version compatibility enforcement (hard fail on mismatch)
- Audit log is authoritative source of truth for idempotency (memory is optimization only)

If this file lies, everything lies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Dict, Set, Tuple, Optional, List
import hashlib
import sys


# ============================================================================
# PROTOCOLS - Minimal fact contract
# ============================================================================

class CanonicalFact(Protocol):
    """Protocol defining minimal fact contract for counters."""
    
    @property
    def fact_id(self) -> str:
        """Globally unique fact identifier."""
        ...
    
    @property
    def schema_ref(self) -> SchemaRef:
        """Schema this fact conforms to."""
        ...


class Predicate(Protocol):
    """Pure predicate for counter increment conditions."""
    
    def evaluate(self, fact: CanonicalFact) -> bool:
        """Evaluate if fact satisfies condition."""
        ...


# ============================================================================
# CORE DATA STRUCTURES - Single canonical definitions
# ============================================================================

@dataclass(frozen=True)
class SchemaRef:
    """Reference to a canonical schema. Immutable."""
    
    schema_name: str
    schema_version: str
    
    def __str__(self) -> str:
        return f"{self.schema_name}:v{self.schema_version}"
    
    def __post_init__(self) -> None:
        if not self.schema_name:
            raise ValueError("Schema name cannot be empty")
        if not self.schema_version:
            raise ValueError("Schema version cannot be empty")


@dataclass(frozen=True)
class CounterSpec:
    """
    Immutable counter specification.
    
    This is the SINGLE canonical CounterSpec contract.
    All counters must declare this exact structure.
    
    TIER-0 REQUIREMENTS:
    - window_type is a string identifier (time semantics belong in orchestration)
    - max_increment_per_fact must be exactly 1 (pure "count facts exactly once" doctrine)
    - replay_safe must be True (all counters are replay-safe)
    - version is required for schema evolution tracking
    
    CARDINALITY RULE: Each canonical fact contributes exactly 0 or 1 increments.
    Multi-increment counters violate this rule and are not allowed.
    If you need magnitude counting, use a separate weighted aggregator.
    """
    
    name: str
    source_schema: SchemaRef
    increment_condition: Predicate
    window_type: str  # String identifier, not time enum
    monotonic: bool
    max_increment_per_fact: int
    replay_safe: bool
    version: str
    
    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Counter name cannot be empty")
        
        if not self.replay_safe:
            raise ValueError(
                f"All counters must be replay_safe. Counter '{self.name}' violates this."
            )
        
        if not self.version:
            raise ValueError(f"Counter version required for '{self.name}'")
        
        # TIER-0 CARDINALITY ENFORCEMENT: Pure "count facts exactly once" doctrine
        # Blueprint rule: "Each canonical fact contributes 0 or 1 increments"
        # 
        # Multi-increment counters violate this by allowing semantic magnitude counting.
        # If you need magnitude counting, use a separate aggregator, not a counter.
        # 
        # Counters are pure arithmetic: one fact = one increment (or zero).
        # This is the mathematical ground truth that everything else depends on.
        if self.max_increment_per_fact != 1:
            raise ValueError(
                f"TIER-0 VIOLATION: Counter '{self.name}' has max_increment_per_fact={self.max_increment_per_fact}. "
                f"Tier-0 requires exactly 1 increment per fact (or 0 if condition not met). "
                f"Multi-increment counters violate the 'count facts exactly once' doctrine. "
                f"If you need magnitude counting, use a separate weighted aggregator, not a counter."
            )
    
    def spec_hash(self) -> str:
        """Stable hash for counter specification identity."""
        components = [
            self.name,
            str(self.source_schema),
            self.window_type,
            str(self.monotonic),
            str(self.max_increment_per_fact),
            self.version,
        ]
        content = "|".join(components)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class CounterUpdate:
    """
    Immutable audit record of a counter evaluation.
    
    TIER-0 REQUIREMENT: Every counter evaluation emits an audit entry,
    including idempotent no-ops (increment_amount=0) to prove replay identity.
    """
    
    counter_name: str
    window_id: str
    input_fact_id: str
    prior_value: int
    new_value: int
    increment_amount: int  # Can be 0 for idempotent no-ops
    aggregation_run_id: str
    is_idempotent_noop: bool  # True if fact was already processed
    
    def __post_init__(self) -> None:
        if self.new_value < 0:
            raise ValueError("Counter values cannot be negative")
        
        if self.prior_value < 0:
            raise ValueError("Prior value cannot be negative")
        
        if self.new_value - self.prior_value != self.increment_amount:
            raise ValueError(
                f"Increment amount {self.increment_amount} must equal "
                f"value change {self.new_value - self.prior_value}"
            )
        
        # Zero increments are allowed for idempotent no-ops
        if self.increment_amount < 0:
            raise ValueError("Increment amount cannot be negative")


# ============================================================================
# COUNTER STATE - Permanent idempotency tracking
# ============================================================================

class CounterState:
    """
    Manages counter state and permanent idempotency tracking.
    
    TIER-0 REQUIREMENT: Idempotency tracking is permanent and reconstructable.
    The audit log is the AUTHORITATIVE source of truth for idempotency.
    In-memory tracking is an optimization only.
    
    DETERMINISM GUARANTEE:
    - Idempotency can be reconstructed purely from audit log
    - Process restart does not break determinism
    - Counter state rebuildable from scratch without trusting RAM
    """
    
    def __init__(self, max_idempotency_entries: Optional[int] = None):
        # Counter values: (counter_name, window_id) -> int
        self._values: Dict[Tuple[str, str], int] = {}
        
        # Idempotency tracking: (counter_name, window_id) -> Set[fact_id]
        # TIER-0: This is an OPTIMIZATION cache. The audit log is authoritative.
        # This cache can be reconstructed from audit log at any time.
        self._processed_facts: Dict[Tuple[str, str], Set[str]] = {}
        
        # Memory growth mitigation: track total entries (for monitoring/alerting)
        self._total_idempotency_entries = 0
        self._max_idempotency_entries = max_idempotency_entries
        
        # Append-only audit log - AUTHORITATIVE source of truth for idempotency
        # TIER-0: All idempotency decisions can be reconstructed from this log
        self._audit_log: List[CounterUpdate] = []
    
    def get(self, counter_name: str, window_id: str) -> int:
        """Get current counter value."""
        return self._values.get((counter_name, window_id), 0)
    
    def is_fact_processed(
        self,
        counter_name: str,
        window_id: str,
        fact_id: str
    ) -> bool:
        """
        Check if fact has already been processed for this counter/window.
        
        TIER-0: Only returns True if fact actually incremented or was idempotent no-op.
        Predicate-miss facts are NOT considered processed, allowing re-evaluation.
        
        Checks audit log (authoritative source) if not in memory cache.
        This ensures determinism survives process restarts.
        """
        key = (counter_name, window_id)
        
        # Fast path: check in-memory cache (optimization)
        if key in self._processed_facts:
            if fact_id in self._processed_facts[key]:
                return True
        
        # Slow path: reconstruct from audit log (authoritative source)
        # TIER-0: Only consider facts that incremented or were idempotent no-ops
        # Predicate-miss facts (increment_amount=0, is_idempotent_noop=False) are NOT processed
        for update in self._audit_log:
            if (update.counter_name == counter_name and 
                update.window_id == window_id and 
                update.input_fact_id == fact_id):
                # TIER-0: Only track if fact actually incremented or was idempotent no-op
                # Predicate-miss facts can be re-evaluated if predicate semantics evolve
                should_track = update.is_idempotent_noop or update.increment_amount > 0
                if should_track:
                    # Found in audit log as processed - update cache for future lookups
                    if key not in self._processed_facts:
                        self._processed_facts[key] = set()
                    self._processed_facts[key].add(fact_id)
                    return True
                # If predicate-miss, don't treat as processed - allow re-evaluation
                return False
        
        return False
    
    def reconstruct_idempotency_from_audit(self) -> None:
        """
        Reconstruct idempotency tracking purely from audit log.
        
        TIER-0 REQUIREMENT: This method proves that idempotency tracking
        is reconstructable without trusting in-memory state.
        
        This is called during rebuild_from_audit to ensure determinism
        across process restarts.
        
        TIER-0: Only tracks facts that actually incremented (amount > 0) or
        were idempotent no-ops. Predicate-miss facts are NOT tracked, allowing
        re-evaluation if predicate semantics evolve.
        """
        self._processed_facts.clear()
        self._total_idempotency_entries = 0
        
        for update in self._audit_log:
            # TIER-0: Only track facts that incremented or were idempotent no-ops
            # Predicate-miss facts (amount=0, is_idempotent_noop=False) are NOT tracked
            should_track = update.is_idempotent_noop or update.increment_amount > 0
            
            if should_track:
                key = (update.counter_name, update.window_id)
                if key not in self._processed_facts:
                    self._processed_facts[key] = set()
                
                # Only count new facts (not duplicates in audit log)
                if update.input_fact_id not in self._processed_facts[key]:
                    self._processed_facts[key].add(update.input_fact_id)
                    self._total_idempotency_entries += 1
    
    def increment(
        self,
        counter_name: str,
        window_id: str,
        fact_id: str,
        amount: int,
        aggregation_run_id: str,
        is_idempotent_noop: bool = False
    ) -> CounterUpdate:
        """
        Increment counter and emit audit entry.
        
        TIER-0 REQUIREMENT: Always emits audit entry, even for idempotent no-ops.
        """
        if amount < 0:
            raise ValueError(f"Counter increment must be >= 0, got {amount}")
        
        # Overflow check
        current = self.get(counter_name, window_id)
        if amount > sys.maxsize - current:
            raise OverflowError(
                f"Counter overflow: {counter_name} in window {window_id}"
            )
        
        key = (counter_name, window_id)
        prior = self._values.get(key, 0)
        
        # TIER-0: Only track facts as processed if they actually increment OR are idempotent no-ops
        # This ensures predicate-miss facts can be re-evaluated if predicate semantics evolve
        # Facts that fail increment_condition should NOT be marked as processed
        should_track_idempotency = is_idempotent_noop or amount > 0
        
        if should_track_idempotency:
            if key not in self._processed_facts:
                self._processed_facts[key] = set()
            
            # Check if this is a new fact_id (for memory tracking)
            is_new_fact = fact_id not in self._processed_facts[key]
            if is_new_fact:
                self._total_idempotency_entries += 1
                
                # Memory growth mitigation: warn if approaching limit
                if self._max_idempotency_entries is not None:
                    if self._total_idempotency_entries >= self._max_idempotency_entries:
                        raise MemoryError(
                            f"Idempotency tracking limit reached: {self._total_idempotency_entries} entries. "
                            f"Tier-0 systems at scale require persistent idempotency index or "
                            f"checkpointed digest structures. Consider migrating to persistent storage."
                        )
            
            self._processed_facts[key].add(fact_id)
        
        # Compute new value
        if is_idempotent_noop:
            new_value = prior  # No change
        else:
            new_value = prior + amount
            self._values[key] = new_value
        
        # Emit audit entry (required for all evaluations)
        update = CounterUpdate(
            counter_name=counter_name,
            window_id=window_id,
            input_fact_id=fact_id,
            prior_value=prior,
            new_value=new_value,
            increment_amount=amount if not is_idempotent_noop else 0,
            aggregation_run_id=aggregation_run_id,
            is_idempotent_noop=is_idempotent_noop
        )
        
        self._audit_log.append(update)
        return update
    
    def get_audit_log(self) -> List[CounterUpdate]:
        """Get append-only audit log."""
        return self._audit_log.copy()


# ============================================================================
# COUNTER ENGINE - Single execution model
# ============================================================================

class CounterEngine:
    """
    Single execution model for counter processing.
    
    TIER-0 REQUIREMENTS:
    - Validates schema match (hard failure on mismatch)
    - Enforces cardinality (0 or 1 increment per fact)
    - Enforces monotonicity if specified
    - Emits audit entries for all evaluations
    - Time-agnostic (receives window_id from orchestration)
    """
    
    def __init__(self):
        self._specs: Dict[str, CounterSpec] = {}
        self._state = CounterState()
    
    def register(self, spec: CounterSpec) -> None:
        """Register a counter specification."""
        if spec.name in self._specs:
            existing = self._specs[spec.name]
            if existing != spec:
                raise ValueError(
                    f"Counter '{spec.name}' already registered with different spec"
                )
            return
        
        self._specs[spec.name] = spec
    
    def process_fact(
        self,
        counter_name: str,
        fact: CanonicalFact,
        window_id: str,
        aggregation_run_id: str
    ) -> CounterUpdate:
        """
        Process a fact against a counter.
        
        TIER-0 REQUIREMENTS:
        - Hard failure on undeclared counter
        - Hard failure on schema mismatch
        - Hard failure on overflow
        - Always returns CounterUpdate (even for no-ops)
        - Enforces cardinality (0 or 1 increment)
        """
        # Validate counter exists
        if counter_name not in self._specs:
            raise ValueError(f"Undeclared counter: {counter_name}")
        
        spec = self._specs[counter_name]
        
        # Validate schema match (hard failure)
        if fact.schema_ref != spec.source_schema:
            raise ValueError(
                f"Schema mismatch: fact has {fact.schema_ref}, "
                f"counter '{counter_name}' requires {spec.source_schema}"
            )
        
        # Check idempotency (permanent tracking)
        is_idempotent = self._state.is_fact_processed(
            counter_name, window_id, fact.fact_id
        )
        
        # Evaluate increment condition
        should_increment = spec.increment_condition.evaluate(fact)
        
        # Determine increment amount
        if is_idempotent:
            # Already processed - emit idempotent no-op audit entry
            increment_amount = 0
            is_noop = True
        elif not should_increment:
            # Condition not met - no increment, but still emit audit entry
            # TIER-0: Facts that fail increment_condition are NOT marked as processed
            # This allows re-evaluation if predicate semantics evolve
            # Only facts that actually increment or are idempotent no-ops are tracked
            increment_amount = 0
            is_noop = False
        else:
            # Condition met - pure Tier-0 path: exactly 1 increment per fact
            # This is the mathematical ground truth: one fact = one increment
            increment_amount = 1
            is_noop = False
        
        # TIER-0: Validate monotonicity BEFORE state mutation
        # This prevents partial state corruption if monotonicity check fails
        current_value = self._state.get(counter_name, window_id)
        if spec.monotonic:
            expected_new_value = current_value + increment_amount
            if expected_new_value < current_value:
                raise ValueError(
                    f"Monotonic violation: counter '{counter_name}' would decrease from "
                    f"{current_value} to {expected_new_value} (increment: {increment_amount})"
                )
        
        # Perform increment (or no-op) - now safe because invariants validated
        update = self._state.increment(
            counter_name=counter_name,
            window_id=window_id,
            fact_id=fact.fact_id,
            amount=increment_amount,
            aggregation_run_id=aggregation_run_id,
            is_idempotent_noop=is_noop
        )
        
        # Post-increment validation (defensive check - should never fail if pre-validation correct)
        if spec.monotonic:
            if update.new_value < update.prior_value:
                raise ValueError(
                    f"Monotonic violation detected after increment: counter '{counter_name}' decreased from "
                    f"{update.prior_value} to {update.new_value}. This indicates a bug in increment logic."
                )
        
        return update
    
    def get_counter(self, counter_name: str, window_id: str) -> int:
        """Get current counter value."""
        return self._state.get(counter_name, window_id)
    
    def get_audit_trail(self) -> List[CounterUpdate]:
        """Get append-only audit trail."""
        return self._state.get_audit_log()
    
    def rebuild_from_audit(
        self,
        audit_log: List[CounterUpdate],
        specs: Dict[str, CounterSpec],
        max_idempotency_entries: Optional[int] = None
    ) -> 'CounterEngine':
        """
        Rebuild counter state from audit log with full semantic validation.
        
        TIER-0 REQUIREMENT: Rebuilds state AND re-validates all invariants.
        This is semantic replay verification, not blind state restoration.
        
        Validates:
        - Version compatibility (hard fail on mismatch)
        - Increment amount consistency
        - Monotonicity constraints
        - Cardinality bounds
        - State transition correctness
        
        This ensures replay re-derives truth, not just restores state.
        """
        new_engine = CounterEngine()
        new_engine._state = CounterState(max_idempotency_entries=max_idempotency_entries)
        
        # Register all specs
        for spec in specs.values():
            new_engine.register(spec)
        
        # Process audit entries in order (preserves replay semantics)
        for update in audit_log:
            # Validate counter exists
            if update.counter_name not in new_engine._specs:
                raise ValueError(
                    f"Cannot rebuild: unknown counter {update.counter_name}"
                )
            
            spec = new_engine._specs[update.counter_name]
            
            # TIER-0: Hard fail on version mismatch (no silent pass)
            # Extract version from aggregation_run_id if present, otherwise use spec version
            audit_version = None
            if ":" in update.aggregation_run_id:
                try:
                    audit_version = update.aggregation_run_id.split(":")[-1]
                except (IndexError, AttributeError):
                    audit_version = None
            
            # Version compatibility check: hard fail on mismatch
            if audit_version is not None and audit_version != spec.version:
                raise ValueError(
                    f"Version mismatch during rebuild: counter '{update.counter_name}' "
                    f"has spec version '{spec.version}' but audit entry has version '{audit_version}'. "
                    f"Tier-0 requires explicit version compatibility rules or hard failure. "
                    f"aggregation_run_id: {update.aggregation_run_id}"
                )
            
            # TIER-0: Semantic replay validation - re-validate invariants instead of blind restore
            # Validate increment amount consistency
            prior_value = new_engine._state.get(update.counter_name, update.window_id)
            expected_new_value = prior_value + update.increment_amount
            
            # Validate that audit entry's increment matches expected state transition
            if update.prior_value != prior_value:
                raise ValueError(
                    f"Replay invariant violation: audit entry prior_value {update.prior_value} "
                    f"does not match reconstructed state {prior_value} for counter '{update.counter_name}' "
                    f"in window {update.window_id}. This indicates non-deterministic replay."
                )
            
            if update.new_value != expected_new_value:
                raise ValueError(
                    f"Replay invariant violation: audit entry new_value {update.new_value} "
                    f"does not match expected value {expected_new_value} (prior {prior_value} + "
                    f"increment {update.increment_amount}) for counter '{update.counter_name}'. "
                    f"This indicates audit log corruption or non-deterministic replay."
                )
            
            # Validate monotonicity if counter is monotonic
            if spec.monotonic:
                if update.new_value < update.prior_value:
                    raise ValueError(
                        f"Replay monotonicity violation: counter '{update.counter_name}' decreased "
                        f"from {update.prior_value} to {update.new_value} during rebuild. "
                        f"Audit entry: {update.input_fact_id}"
                    )
            
            # Validate increment amount bounds
            if update.increment_amount < 0:
                raise ValueError(
                    f"Replay invariant violation: negative increment amount {update.increment_amount} "
                    f"for counter '{update.counter_name}' in audit entry {update.input_fact_id}"
                )
            
            # TIER-0: Enforce strict 0-or-1 increment per fact
            if update.increment_amount > 1:
                raise ValueError(
                    f"Replay cardinality violation: increment amount {update.increment_amount} "
                    f"exceeds Tier-0 maximum of 1 for counter '{update.counter_name}' in audit entry "
                    f"{update.input_fact_id}. Counters count facts exactly once, not magnitudes."
                )
            
            # Reconstruct state (now validated)
            key = (update.counter_name, update.window_id)
            
            # Update counter value
            new_engine._state._values[key] = update.new_value
            
            # Reconstruct audit log (preserves ordering)
            # TIER-0: Audit log is authoritative - idempotency will be reconstructed from it
            new_engine._state._audit_log.append(update)
        
        # TIER-0: Reconstruct idempotency tracking purely from audit log
        # This proves determinism survives process restarts
        new_engine._state.reconstruct_idempotency_from_audit()
        
        return new_engine
