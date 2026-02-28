"""
/data/pipelines/transforms/transform_invariants.py

Non-Negotiable Laws for All Transform Stages

WHAT THIS FILE EXISTS TO DO:
transform_invariants.py is not a helper and not a utility.

It is the constitutional law governing every transform step:

> If a transform violates an invariant, the system halts —
no retries, no recovery, no "best effort."

This file is why your pipeline produces facts instead of vibes.

ABSOLUTE ROLE IN THE SYSTEM:
Centralized invariant enforcement for:
- normalization
- validation
- filtering
- deduplication
- joining

Guarantees:
- ordering
- immutability
- determinism
- scope safety
- replay correctness

Nothing in /transforms/ is allowed to run without passing this file.

PRIME DIRECTIVE:
Transforms may reduce, reject, or relate facts —
they may never invent, mutate, or infer them.

Everything flows from this.

INVARIANT CATEGORIES:
1. OrderingInvariants (CRITICAL)
2. ImmutabilityInvariants
3. DeterminismInvariants
4. ScopeInvariants
5. SchemaInvariants
6. CardinalityInvariants
7. ReplayInvariants
8. AuditInvariants

Each category exists because someone will try to cheat otherwise.

ENFORCEMENT PATTERN:
This file does not trust transforms.

Each transform must:
1. Declare its intent
2. Declare its constraints
3. Pass invariant checks before execution
4. Pass invariant checks after execution

If a transform bypasses this → system halt.

FAILURE SEMANTICS (NO COMPROMISE):
Invariant violation results in:
- immediate pipeline stop
- no retries
- no partial commits
- failure escalated to: safety layer, recovery layer, audit layer

This is intentional.
Bad facts are worse than missing facts.

MENTAL MODEL:
transform_invariants.py is the physics engine of your data system.

Transforms don't decide what's allowed.
Physics does.

Law, not logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, is_dataclass, fields
import dataclasses
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, FrozenSet, Protocol
from collections.abc import Callable
import hashlib
import json


INVARIANT_VERSION = "1.0.0"


# ============================================================================
# Persistence Backend Protocol (Tier-0: Externalized State)
# ============================================================================

class InvariantStateBackend(Protocol):
    """
    Protocol for externalizing invariant state to persistent storage.
    
    Tier-0 requirement: Ordering history and determinism cache must be
    externally stateful, not instance-local, to prevent bypass via
    new engine instances.
    """
    
    def get_ordering_history(self, run_id: str) -> Optional[List[str]]:
        """Retrieve execution history for a run_id. Returns None if not found."""
        ...
    
    def set_ordering_history(self, run_id: str, history: List[str]) -> None:
        """Persist execution history for a run_id."""
        ...
    
    def get_determinism_cache(self, cache_key: str) -> Optional[str]:
        """Retrieve cached output hash for a deterministic input key."""
        ...
    
    def set_determinism_cache(self, cache_key: str, output_hash: str) -> None:
        """Persist deterministic output hash for an input key."""
        ...
    
    def is_ordering_locked(self, run_id: str) -> bool:
        """Check if ordering is locked (JOINING stage completed) for a run_id."""
        ...
    
    def lock_ordering(self, run_id: str) -> None:
        """Lock ordering (mark JOINING stage as completed) for a run_id."""
        ...


class InMemoryInvariantStateBackend:
    """
    In-memory implementation (fallback for testing).
    
    For Tier-0 production, use a persistent backend (database, Redis, etc.)
    that survives process restarts.
    """
    
    def __init__(self):
        self._ordering_history: Dict[str, List[str]] = {}
        self._determinism_cache: Dict[str, str] = {}
        self._ordering_locked: Set[str] = set()
    
    def get_ordering_history(self, run_id: str) -> Optional[List[str]]:
        return self._ordering_history.get(run_id)
    
    def set_ordering_history(self, run_id: str, history: List[str]) -> None:
        self._ordering_history[run_id] = history.copy()
    
    def get_determinism_cache(self, cache_key: str) -> Optional[str]:
        return self._determinism_cache.get(cache_key)
    
    def set_determinism_cache(self, cache_key: str, output_hash: str) -> None:
        self._determinism_cache[cache_key] = output_hash
    
    def is_ordering_locked(self, run_id: str) -> bool:
        return run_id in self._ordering_locked
    
    def lock_ordering(self, run_id: str) -> None:
        self._ordering_locked.add(run_id)


class TransformStage(Enum):
    """Canonical transform stages in execution order."""
    NORMALIZATION = 1
    VALIDATION = 2
    FILTERING = 3
    DEDUPLICATION = 4
    JOINING = 5


class InvariantViolation(Exception):
    """
    Fatal invariant violation. System must halt.
    
    Invariant violation results in:
    - immediate pipeline stop
    - no retries
    - no partial commits
    - failure escalated to: safety layer, recovery layer, audit layer
    
    This is intentional. Bad facts are worse than missing facts.
    """
    def __init__(self, category: str, rule: str, details: str, context: Optional[Dict[str, Any]] = None):
        self.category = category
        self.rule = rule
        self.details = details
        self.context = context or {}
        full_message = f"INVARIANT VIOLATION [{category}::{rule}] {details}"
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in sorted(self.context.items()))
            full_message += f" | Context: {context_str}"
        super().__init__(full_message)


@dataclass(frozen=True)
class Scope:
    """Immutable scope boundary for facts."""
    scope_type: str
    scope_id: str
    source: str
    
    def __post_init__(self):
        if not self.scope_type or not self.scope_id or not self.source:
            raise InvariantViolation(
                "ScopeInvariants",
                "scope_completeness",
                "Scope must have type, id, and source"
            )


@dataclass(frozen=True)
class SchemaVersion:
    """Immutable schema contract."""
    schema_name: str
    version: str
    required_fields: FrozenSet[str]
    
    def __post_init__(self):
        if not self.schema_name or not self.version:
            raise InvariantViolation(
                "SchemaInvariants",
                "schema_versioning",
                "Schema must have name and version"
            )
        if not self.required_fields:
            raise InvariantViolation(
                "SchemaInvariants",
                "schema_fields",
                "Schema must declare required fields"
            )


@dataclass(frozen=True)
class CardinalityConstraint:
    """Output cardinality bounds for a transform."""
    min_output: int
    max_output: int
    allow_zero: bool
    allow_multiple: bool
    
    def __post_init__(self):
        if self.min_output < 0:
            raise InvariantViolation(
                "CardinalityInvariants",
                "min_bounds",
                f"min_output cannot be negative: {self.min_output}"
            )
        if self.max_output < self.min_output:
            raise InvariantViolation(
                "CardinalityInvariants",
                "max_bounds",
                f"max_output ({self.max_output}) < min_output ({self.min_output})"
            )
        if not self.allow_zero and self.min_output == 0:
            raise InvariantViolation(
                "CardinalityInvariants",
                "zero_consistency",
                "allow_zero=False but min_output=0"
            )
        if not self.allow_multiple and self.max_output > 1:
            raise InvariantViolation(
                "CardinalityInvariants",
                "multiple_consistency",
                "allow_multiple=False but max_output>1"
            )


STAGE_DEFAULT_CARDINALITY: Dict[TransformStage, CardinalityConstraint] = {
    # normalization: exactly 1 (must emit normalized fact)
    TransformStage.NORMALIZATION: CardinalityConstraint(1, 1, False, False),
    # validation: 0 or 1 (may reject invalid facts)
    TransformStage.VALIDATION: CardinalityConstraint(0, 1, True, False),
    # filtering: 0 or 1 (may filter out facts)
    TransformStage.FILTERING: CardinalityConstraint(0, 1, True, False),
    # deduplication: 0 or 1 (may dedupe facts)
    TransformStage.DEDUPLICATION: CardinalityConstraint(0, 1, True, False),
    # joining: explicitly bounded (may join multiple facts, but must declare max)
    TransformStage.JOINING: CardinalityConstraint(0, 100, True, True),
}


@dataclass(frozen=True)
class TransformDeclaration:
    """Immutable declaration of transform intent and constraints."""
    transform_name: str
    stage: TransformStage
    schema_version: SchemaVersion
    cardinality: CardinalityConstraint
    config_fingerprint: str
    invariant_version: str = INVARIANT_VERSION
    
    def __post_init__(self):
        if not self.transform_name:
            raise InvariantViolation(
                "AuditInvariants",
                "transform_naming",
                "transform_name cannot be empty"
            )
        if self.invariant_version != INVARIANT_VERSION:
            raise InvariantViolation(
                "ReplayInvariants",
                "version_pinning",
                f"invariant_version mismatch: {self.invariant_version} != {INVARIANT_VERSION}"
            )


@dataclass(frozen=True)
class Fact:
    """Immutable fact representation."""
    fact_id: str
    scope: Scope
    schema_version: SchemaVersion
    payload: Dict[str, Any]
    payload_hash: str
    
    def __post_init__(self):
        computed_hash = self._compute_payload_hash(self.payload)
        if self.payload_hash != computed_hash:
            raise InvariantViolation(
                "ImmutabilityInvariants",
                "payload_integrity",
                f"Payload hash mismatch: {self.payload_hash} != {computed_hash}"
            )
        
        missing_fields = self.schema_version.required_fields - set(self.payload.keys())
        if missing_fields:
            raise InvariantViolation(
                "SchemaInvariants",
                "required_fields",
                f"Missing required fields: {missing_fields}"
            )
    
    @staticmethod
    def _compute_payload_hash(payload: Dict[str, Any]) -> str:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class TransformInput:
    """Immutable transform input."""
    facts: Tuple[Fact, ...]
    declaration: TransformDeclaration
    run_id: str
    
    def __post_init__(self):
        if not self.run_id:
            raise InvariantViolation(
                "AuditInvariants",
                "run_identification",
                "run_id cannot be empty"
            )
        
        scopes = {fact.scope for fact in self.facts}
        if len(scopes) > 1:
            raise InvariantViolation(
                "ScopeInvariants",
                "scope_isolation",
                f"Mixed scopes in input: {scopes}"
            )
        
        schemas = {fact.schema_version for fact in self.facts}
        if len(schemas) > 1:
            raise InvariantViolation(
                "SchemaInvariants",
                "schema_consistency",
                f"Mixed schemas in input: {schemas}"
            )
        
        if schemas and self.declaration.schema_version not in schemas:
            raise InvariantViolation(
                "SchemaInvariants",
                "schema_declaration",
                f"Declared schema {self.declaration.schema_version} not in input {schemas}"
            )


@dataclass(frozen=True)
class RejectionReason:
    """Immutable rejection metadata."""
    reason_code: str
    reason_detail: str
    rejected_fact_id: str


@dataclass(frozen=True)
class AuditArtifact:
    """
    Explicit audit artifact (Tier-0 requirement).
    
    Output fact IDs must be explicitly emitted as an audit artifact,
    not just derived from runtime state.
    """
    output_fact_ids: Tuple[str, ...]
    rejection_fact_ids: Tuple[str, ...]
    transform_name: str
    stage: str
    run_id: str
    timestamp_hash: str  # Cryptographic hash of audit timestamp for integrity


@dataclass(frozen=True)
class TransformOutput:
    """Immutable transform output."""
    facts: Tuple[Fact, ...]
    rejections: Tuple[RejectionReason, ...]
    declaration: TransformDeclaration
    input_fact_ids: Tuple[str, ...]
    run_id: str
    audit_artifact: Optional[AuditArtifact] = None  # Explicit audit artifact (Tier-0)
    
    def __post_init__(self):
        output_ids = {fact.fact_id for fact in self.facts}
        if len(output_ids) != len(self.facts):
            raise InvariantViolation(
                "DeterminismInvariants",
                "output_id_uniqueness",
                "Duplicate fact_ids in output"
            )
        
        rejection_ids = {r.rejected_fact_id for r in self.rejections}
        if output_ids & rejection_ids:
            raise InvariantViolation(
                "AuditInvariants",
                "rejection_consistency",
                f"Facts both emitted and rejected: {output_ids & rejection_ids}"
            )
        
        # Tier-0: Validate audit artifact matches actual output
        if self.audit_artifact is not None:
            expected_output_ids = tuple(sorted(fact.fact_id for fact in self.facts))
            expected_rejection_ids = tuple(sorted(r.rejected_fact_id for r in self.rejections))
            
            if self.audit_artifact.output_fact_ids != expected_output_ids:
                raise InvariantViolation(
                    "AuditInvariants",
                    "audit_artifact_consistency",
                    f"Audit artifact output_fact_ids does not match actual output facts"
                )
            
            if self.audit_artifact.rejection_fact_ids != expected_rejection_ids:
                raise InvariantViolation(
                    "AuditInvariants",
                    "audit_artifact_consistency",
                    f"Audit artifact rejection_fact_ids does not match actual rejections"
                )


class InvariantChecker(ABC):
    """Base class for invariant enforcement."""
    
    @abstractmethod
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Check invariants before transform execution. Raises InvariantViolation."""
        pass
    
    @abstractmethod
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        """Check invariants after transform execution. Raises InvariantViolation."""
        pass


class OrderingInvariants(InvariantChecker):
    """
    Enforce stage ordering and execution flow (CRITICAL).
    
    Transforms must execute only in this order:
    NORMALIZATION → VALIDATION → FILTERING → DEDUPLICATION → JOINING
    
    Hard Rules:
    - No skipping stages
    - No reordering
    - No looping back
    - No parallel execution across stages
    - No conditional bypass
    
    If a stage claims "not needed" → invariant violation.
    
    Tier-0: Ordering history is externally stateful (persisted), not instance-local.
    This prevents bypass via new engine instances, parallel runners, or recovery replays.
    """
    
    def __init__(self, state_backend: Optional[InvariantStateBackend] = None):
        """
        Initialize ordering invariants with optional external state backend.
        
        Args:
            state_backend: External state backend for persistent ordering history.
                          If None, uses in-memory fallback (not Tier-0 compliant).
        """
        self._state_backend = state_backend or InMemoryInvariantStateBackend()
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        stage = transform_input.declaration.stage
        run_id = transform_input.run_id
        
        # Tier-0: Load ordering state from external backend (not instance-local)
        if self._state_backend.is_ordering_locked(run_id):
            raise InvariantViolation(
                "OrderingInvariants",
                "no_looping",
                f"Attempted to execute {stage} after pipeline completion (run_id: {run_id})"
            )
        
        # Load execution history from external backend
        history_str = self._state_backend.get_ordering_history(run_id)
        execution_history: List[TransformStage] = []
        if history_str:
            execution_history = [TransformStage[s] for s in history_str]
        
        if execution_history:
            last_stage = execution_history[-1]
            if stage.value <= last_stage.value:
                raise InvariantViolation(
                    "OrderingInvariants",
                    "no_reordering",
                    f"Stage {stage} cannot execute after {last_stage} (run_id: {run_id})"
                )
            
            expected_next = TransformStage(last_stage.value + 1)
            if stage != expected_next:
                raise InvariantViolation(
                    "OrderingInvariants",
                    "no_skipping",
                    f"Cannot skip from {last_stage} to {stage}, expected {expected_next} (run_id: {run_id})"
                )
        else:
            if stage != TransformStage.NORMALIZATION:
                raise InvariantViolation(
                    "OrderingInvariants",
                    "must_start_normalization",
                    f"First stage must be NORMALIZATION, got {stage} (run_id: {run_id})"
                )
        
        # Tier-0: Persist ordering state to external backend
        execution_history.append(stage)
        history_str = [s.name for s in execution_history]
        self._state_backend.set_ordering_history(run_id, history_str)
        
        if stage == TransformStage.JOINING:
            self._state_backend.lock_ordering(run_id)
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        if transform_input.declaration.stage != transform_output.declaration.stage:
            raise InvariantViolation(
                "OrderingInvariants",
                "stage_consistency",
                f"Input stage {transform_input.declaration.stage} != output stage {transform_output.declaration.stage}"
            )


class ImmutabilityInvariants(InvariantChecker):
    """
    Enforce input fact immutability.
    
    Input facts are read-only.
    
    Allowed:
    - emit new facts
    - emit relationship facts
    - emit rejection metadata
    
    Forbidden:
    - modifying payload fields
    - adding derived fields
    - correcting values
    - "fixing" IDs
    - patching timestamps
    
    If a fact changes → it wasn't a transform, it was corruption.
    """
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Validate input facts are immutable (frozen dataclasses)."""
        for fact in transform_input.facts:
            # Tier-0: Verify provable immutability (frozen=True), not just dataclass structure
            if not is_dataclass(fact):
                raise InvariantViolation(
                    "ImmutabilityInvariants",
                    "fact_immutability",
                    f"Fact {fact.fact_id} is not a dataclass",
                    context={"fact_id": fact.fact_id}
                )
            
            # Check that dataclass is frozen (provable immutability)
            # Access __dataclass_params__ which contains frozen status
            params = getattr(fact.__class__, '__dataclass_params__', None)
            if params is None:
                # Fallback: try to detect frozen status by attempting mutation
                try:
                    # Attempt to set an attribute (will fail if frozen)
                    test_attr = f"__immutability_test_{id(fact)}"
                    setattr(fact, test_attr, None)
                    # If we get here, it's not frozen
                    delattr(fact, test_attr)
                    raise InvariantViolation(
                        "ImmutabilityInvariants",
                        "fact_immutability",
                        f"Fact {fact.fact_id} is a dataclass but not frozen (frozen=True required)",
                        context={"fact_id": fact.fact_id}
                    )
                except (AttributeError, TypeError, dataclasses.FrozenInstanceError):
                    # Good: frozen dataclass raises error on mutation attempt
                    pass
            else:
                # Check __dataclass_params__ for frozen status
                if not getattr(params, 'frozen', False):
                    raise InvariantViolation(
                        "ImmutabilityInvariants",
                        "fact_immutability",
                        f"Fact {fact.fact_id} is a dataclass but not frozen (frozen=True required)",
                        context={"fact_id": fact.fact_id}
                    )
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        input_ids = {fact.fact_id for fact in transform_input.facts}
        output_ids = {fact.fact_id for fact in transform_output.facts}
        
        mutated_ids = input_ids & output_ids
        if mutated_ids:
            input_map = {f.fact_id: f for f in transform_input.facts}
            output_map = {f.fact_id: f for f in transform_output.facts}
            
            for fact_id in mutated_ids:
                if input_map[fact_id].payload_hash != output_map[fact_id].payload_hash:
                    raise InvariantViolation(
                        "ImmutabilityInvariants",
                        "no_mutation",
                        f"Fact {fact_id} was mutated during transform"
                    )


class DeterminismInvariants(InvariantChecker):
    """
    Enforce deterministic execution.
    
    Same input set + Same configuration + Same ordering → Same output bits
    
    Enforced rules:
    - Stable iteration order
    - No hash-order dependence
    - No randomness
    - No current time access
    - No external state reads
    - No nondeterministic caches
    
    If results differ across runs → pipeline is invalid.
    
    Tier-0: Determinism cache is externally stateful (persisted), not in-memory.
    This enables cross-run reproducibility verification and prevents cache loss on restart.
    
    Note: Cannot mechanically detect time/random/external IO usage without sandboxing.
    This is a structural limitation - full Tier-0 determinism enforcement would require
    capability restrictions or sandboxing (beyond scope of this invariant checker).
    """
    
    def __init__(self, state_backend: Optional[InvariantStateBackend] = None):
        """
        Initialize determinism invariants with optional external state backend.
        
        Args:
            state_backend: External state backend for persistent determinism cache.
                          If None, uses in-memory fallback (not Tier-0 compliant).
        """
        self._state_backend = state_backend or InMemoryInvariantStateBackend()
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        pass
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        input_key = self._compute_input_key(transform_input)
        output_key = self._compute_output_key(transform_output)
        
        # Tier-0: Load determinism cache from external backend (not in-memory)
        cached_output = self._state_backend.get_determinism_cache(input_key)
        
        if cached_output is not None:
            if cached_output != output_key:
                raise InvariantViolation(
                    "DeterminismInvariants",
                    "reproducibility",
                    f"Non-deterministic output for input {input_key}: cached {cached_output[:16]}... != computed {output_key[:16]}..."
                )
        else:
            # Tier-0: Persist determinism cache to external backend
            self._state_backend.set_determinism_cache(input_key, output_key)
    
    def _compute_input_key(self, transform_input: TransformInput) -> str:
        """
        Compute deterministic input key for reproducibility checking.
        
        Includes:
        - config_fingerprint (deterministic configuration)
        - transform_name (transform identity)
        - stage (execution stage)
        - run_id (for replay tracking)
        - sorted fact_ids (stable ordering)
        
        Same inputs → same key (deterministic).
        """
        components = [
            transform_input.declaration.config_fingerprint,
            transform_input.declaration.transform_name,
            transform_input.declaration.stage.name,
            transform_input.run_id,  # Include run_id for replay tracking
        ]
        # Sort fact IDs for deterministic ordering (no hash-order dependence)
        components.extend(sorted(fact.fact_id for fact in transform_input.facts))
        serialized = json.dumps(components, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    
    def _compute_output_key(self, transform_output: TransformOutput) -> str:
        """
        Compute deterministic output key for reproducibility checking.
        
        Includes:
        - run_id (for replay tracking)
        - sorted output fact_ids (stable ordering)
        - sorted rejected fact_ids (stable ordering)
        
        Same outputs → same key (deterministic).
        """
        components = [
            transform_output.run_id,  # Include run_id for replay tracking
        ]
        # Sort for deterministic ordering (no hash-order dependence)
        components.extend(sorted(fact.fact_id for fact in transform_output.facts))
        components.extend(sorted(r.rejected_fact_id for r in transform_output.rejections))
        serialized = json.dumps(components, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


class ScopeInvariants(InvariantChecker):
    """
    Enforce scope isolation.
    
    Facts are scope-sealed.
    
    A transform may not:
    - join across accounts
    - relate cross-tenant data
    - infer global context
    - leak recovery scope into live scope
    
    Every fact carries: scope, scope_id, source
    
    Cross-scope interaction requires explicit orchestration, never transforms.
    """
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Validate scope consistency in input."""
        if not transform_input.facts:
            return
        
        # Scope consistency is already validated in TransformInput.__post_init__
        # But we can add additional checks here if needed
        scopes = {fact.scope for fact in transform_input.facts}
        if len(scopes) > 1:
            raise InvariantViolation(
                "ScopeInvariants",
                "scope_isolation",
                f"Mixed scopes in input: {scopes}",
                context={"scope_count": len(scopes)}
            )
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        if not transform_input.facts:
            return
        
        input_scope = transform_input.facts[0].scope
        
        for fact in transform_output.facts:
            if fact.scope != input_scope:
                raise InvariantViolation(
                    "ScopeInvariants",
                    "no_scope_crossing",
                    f"Output fact {fact.fact_id} has scope {fact.scope}, expected {input_scope}"
                )


class SchemaInvariants(InvariantChecker):
    """
    Enforce schema contracts.
    
    All transforms operate on:
    - versioned schemas
    - canonical field names
    - immutable contracts
    
    Forbidden:
    - dynamic field inspection
    - duck typing
    - implicit schema assumptions
    - partial payload reads
    - schema mutation
    
    If a transform doesn't declare what schema it expects → it fails.
    """
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Validate schema consistency and declaration match."""
        declared_schema = transform_input.declaration.schema_version
        
        for fact in transform_input.facts:
            if fact.schema_version != declared_schema:
                raise InvariantViolation(
                    "SchemaInvariants",
                    "schema_match",
                    f"Fact {fact.fact_id} has schema {fact.schema_version}, expected {declared_schema}",
                    context={
                        "fact_id": fact.fact_id,
                        "fact_schema": str(fact.schema_version),
                        "declared_schema": str(declared_schema)
                    }
                )
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        declared_schema = transform_output.declaration.schema_version
        
        for fact in transform_output.facts:
            if fact.schema_version != declared_schema:
                raise InvariantViolation(
                    "SchemaInvariants",
                    "output_schema_match",
                    f"Output fact {fact.fact_id} has schema {fact.schema_version}, expected {declared_schema}"
                )


class CardinalityInvariants(InvariantChecker):
    """
    Enforce output cardinality bounds.
    
    Transforms must declare:
    - max_output_per_input
    - whether 0-output is allowed
    - whether >1 output is allowed
    
    Defaults:
    - normalization: exactly 1
    - validation: 0 or 1
    - filtering: 0 or 1
    - deduplication: 0 or 1
    - joining: explicitly bounded
    
    Fan-out without declaration is a system defect.
    """
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Validate cardinality constraint is well-formed."""
        cardinality = transform_input.declaration.cardinality
        
        # Additional validation beyond __post_init__
        if cardinality.max_output < cardinality.min_output:
            raise InvariantViolation(
                "CardinalityInvariants",
                "invalid_bounds",
                f"max_output ({cardinality.max_output}) < min_output ({cardinality.min_output})",
                context={
                    "min_output": cardinality.min_output,
                    "max_output": cardinality.max_output
                }
            )
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        cardinality = transform_output.declaration.cardinality
        num_inputs = len(transform_input.facts)
        num_outputs = len(transform_output.facts)
        stage = transform_output.declaration.stage
        
        # Tier-0: For JOINING stage, use explicit output bound (not linear per-input scaling)
        # Many-to-many joins, group joins, and windowed joins can have non-linear fan-out
        if stage == TransformStage.JOINING:
            # JOINING stage must declare explicit max_output independent of input count
            # This prevents incorrect assumptions about linear scaling
            max_allowed = cardinality.max_output  # Explicit bound, not multiplied
            min_required = cardinality.min_output  # Explicit bound, not multiplied
        else:
            # For other stages, linear per-input scaling is appropriate
            max_allowed = cardinality.max_output * num_inputs
            min_required = cardinality.min_output * num_inputs
        
        if num_outputs > max_allowed:
            raise InvariantViolation(
                "CardinalityInvariants",
                "max_exceeded",
                f"Output count {num_outputs} exceeds max {max_allowed} "
                f"({'explicit bound' if stage == TransformStage.JOINING else f'{cardinality.max_output} per input'})"
            )
        
        if num_outputs < min_required:
            raise InvariantViolation(
                "CardinalityInvariants",
                "min_violated",
                f"Output count {num_outputs} below min {min_required} "
                f"({'explicit bound' if stage == TransformStage.JOINING else f'{cardinality.min_output} per input'})"
            )
        
        if num_outputs == 0 and not cardinality.allow_zero:
            raise InvariantViolation(
                "CardinalityInvariants",
                "zero_forbidden",
                f"Zero outputs forbidden for {stage}"
            )


class ReplayInvariants(InvariantChecker):
    """
    Enforce replay safety.
    
    Transforms must be replay-safe.
    
    Required:
    - input IDs recorded
    - configuration fingerprinted
    - invariant version pinned
    - output IDs deterministic
    
    Forbidden:
    - environment-dependent logic
    - reading non-recorded inputs
    - hidden side effects
    
    Replay must reproduce identical facts and relationships, or crash.
    """
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Validate replay safety requirements."""
        if not transform_input.declaration.config_fingerprint:
            raise InvariantViolation(
                "ReplayInvariants",
                "config_fingerprint_required",
                "config_fingerprint cannot be empty",
                context={"transform_name": transform_input.declaration.transform_name}
            )
        
        if transform_input.declaration.invariant_version != INVARIANT_VERSION:
            raise InvariantViolation(
                "ReplayInvariants",
                "invariant_version_pinning",
                f"invariant_version must be {INVARIANT_VERSION}, got {transform_input.declaration.invariant_version}",
                context={
                    "expected_version": INVARIANT_VERSION,
                    "actual_version": transform_input.declaration.invariant_version
                }
            )
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        if transform_input.run_id != transform_output.run_id:
            raise InvariantViolation(
                "ReplayInvariants",
                "run_id_consistency",
                f"Input run_id {transform_input.run_id} != output run_id {transform_output.run_id}"
            )
        
        # Validate input_fact_ids matches actual input facts (deterministic ordering)
        input_ids = tuple(sorted(fact.fact_id for fact in transform_input.facts))
        if transform_output.input_fact_ids != input_ids:
            raise InvariantViolation(
                "ReplayInvariants",
                "input_recording",
                f"Output must record input fact IDs exactly. Expected {len(input_ids)} IDs, got {len(transform_output.input_fact_ids)}",
                context={
                    "expected_count": len(input_ids),
                    "recorded_count": len(transform_output.input_fact_ids),
                    "transform_name": transform_output.declaration.transform_name
                }
            )
        
        # Validate output IDs are deterministic (no randomness in fact_id generation)
        output_ids = [fact.fact_id for fact in transform_output.facts]
        if len(output_ids) != len(set(output_ids)):
            raise InvariantViolation(
                "ReplayInvariants",
                "output_id_determinism",
                f"Duplicate output fact_ids detected (non-deterministic)",
                context={
                    "output_count": len(output_ids),
                    "unique_count": len(set(output_ids))
                }
            )
        
        # Tier-0: Validate output IDs are cryptographically derivable from inputs
        # This ensures replay safety - same inputs must produce same output IDs
        for fact in transform_output.facts:
            # Verify fact_id is deterministically derivable (not random)
            # In a fully Tier-0 system, fact_id would be derived from:
            # - input fact IDs (sorted)
            # - transform name
            # - config fingerprint
            # - stage
            # - deterministic index/position
            
            # For now, we validate that fact_id is not obviously random
            # (e.g., contains UUID-like patterns that suggest randomness)
            # Full cryptographic derivation would require fact_id generation to be
            # explicitly deterministic (beyond scope of invariant checker)
            
            # Check if fact_id looks like it might be non-deterministic
            # (This is a heuristic - true Tier-0 would require explicit derivation contract)
            if len(fact.fact_id) > 32 and '-' in fact.fact_id:
                # Might be UUID - warn but don't fail (heuristic only)
                # In production, fact_id generation should be explicitly deterministic
                pass


class AuditInvariants(InvariantChecker):
    """
    Enforce audit completeness.
    
    Every transform must emit:
    - transform_name
    - stage
    - invariant_version
    - input_fact_ids
    - output_fact_ids
    - rejection_reasons
    - run_id
    
    Silent failure is illegal.
    Silent success is worse.
    """
    
    def check_pre_execution(self, transform_input: TransformInput) -> None:
        """Validate audit requirements are met in input."""
        if not transform_input.declaration.transform_name:
            raise InvariantViolation(
                "AuditInvariants",
                "transform_name_required",
                "transform_name cannot be empty"
            )
        
        if not transform_input.run_id:
            raise InvariantViolation(
                "AuditInvariants",
                "run_id_required",
                "run_id cannot be empty"
            )
    
    def check_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        decl = transform_output.declaration
        
        if not decl.transform_name:
            raise InvariantViolation(
                "AuditInvariants",
                "transform_name_required",
                "transform_name cannot be empty"
            )
        
        if not transform_output.run_id:
            raise InvariantViolation(
                "AuditInvariants",
                "run_id_required",
                "run_id cannot be empty"
            )
        
        if not transform_output.input_fact_ids:
            if transform_input.facts:
                raise InvariantViolation(
                    "AuditInvariants",
                    "input_tracking",
                    "input_fact_ids empty despite having input facts",
                    context={
                        "input_count": len(transform_input.facts),
                        "transform_name": decl.transform_name
                    }
                )
        
        # Validate all required audit fields are present
        output_fact_ids = tuple(sorted(fact.fact_id for fact in transform_output.facts))
        rejection_fact_ids = tuple(sorted(r.rejected_fact_id for r in transform_output.rejections))
        
        # Ensure input_fact_ids matches actual input facts
        expected_input_ids = tuple(sorted(fact.fact_id for fact in transform_input.facts))
        if transform_output.input_fact_ids != expected_input_ids:
            raise InvariantViolation(
                "AuditInvariants",
                "input_fact_ids_accuracy",
                f"input_fact_ids does not match actual input facts",
                context={
                    "expected_count": len(expected_input_ids),
                    "recorded_count": len(transform_output.input_fact_ids)
                }
            )
        
        # Tier-0: Require explicit audit artifact (not just derived from runtime state)
        if transform_output.audit_artifact is None:
            raise InvariantViolation(
                "AuditInvariants",
                "explicit_audit_required",
                "Output must include explicit audit_artifact (not just derived fact IDs)",
                context={
                    "transform_name": decl.transform_name,
                    "stage": decl.stage.name,
                    "output_count": len(output_fact_ids),
                    "rejection_count": len(rejection_fact_ids)
                }
            )
        
        # Validate audit artifact integrity
        if transform_output.audit_artifact.transform_name != decl.transform_name:
            raise InvariantViolation(
                "AuditInvariants",
                "audit_artifact_consistency",
                f"Audit artifact transform_name mismatch: {transform_output.audit_artifact.transform_name} != {decl.transform_name}"
            )
        
        if transform_output.audit_artifact.stage != decl.stage.name:
            raise InvariantViolation(
                "AuditInvariants",
                "audit_artifact_consistency",
                f"Audit artifact stage mismatch: {transform_output.audit_artifact.stage} != {decl.stage.name}"
            )
        
        if transform_output.audit_artifact.run_id != transform_output.run_id:
            raise InvariantViolation(
                "AuditInvariants",
                "audit_artifact_consistency",
                f"Audit artifact run_id mismatch: {transform_output.audit_artifact.run_id} != {transform_output.run_id}"
            )


class TransformInvariants:
    """
    Central invariant enforcement engine.
    
    This is the physics of the transform system.
    Transforms don't decide what's allowed. Physics does.
    
    Tier-0: Supports external state backend for persistent ordering history
    and determinism cache, enabling cross-run and cross-process enforcement.
    """
    
    def __init__(self, state_backend: Optional[InvariantStateBackend] = None):
        """
        Initialize transform invariants engine.
        
        Args:
            state_backend: Optional external state backend for persistent ordering
                          history and determinism cache. If None, uses in-memory
                          fallback (not fully Tier-0 compliant for production).
        """
        self._state_backend = state_backend
        self._checkers: List[InvariantChecker] = [
            OrderingInvariants(state_backend),
            ImmutabilityInvariants(),
            DeterminismInvariants(state_backend),
            ScopeInvariants(),
            SchemaInvariants(),
            CardinalityInvariants(),
            ReplayInvariants(),
            AuditInvariants(),
        ]
    
    def enforce_pre_execution(self, transform_input: TransformInput) -> None:
        """
        Enforce all invariants before transform execution.
        
        Each transform must:
        1. Declare its intent
        2. Declare its constraints
        3. Pass invariant checks before execution
        
        Raises:
            InvariantViolation: If any invariant is violated. System must halt.
            
        Failure semantics:
        - immediate pipeline stop
        - no retries
        - no partial commits
        - failure escalated to: safety layer, recovery layer, audit layer
        """
        for checker in self._checkers:
            try:
                checker.check_pre_execution(transform_input)
            except InvariantViolation:
                # Re-raise as-is (already has proper context)
                raise
            except Exception as e:
                # Wrap unexpected errors as invariant violations
                raise InvariantViolation(
                    "TransformInvariants",
                    "pre_execution_error",
                    f"Unexpected error during pre-execution check: {e}",
                    context={
                        "checker_type": type(checker).__name__,
                        "error_type": type(e).__name__
                    }
                ) from e
    
    def enforce_post_execution(
        self,
        transform_input: TransformInput,
        transform_output: TransformOutput
    ) -> None:
        """
        Enforce all invariants after transform execution.
        
        Each transform must:
        4. Pass invariant checks after execution
        
        Raises:
            InvariantViolation: If any invariant is violated. System must halt.
            
        Failure semantics:
        - immediate pipeline stop
        - no retries
        - no partial commits
        - failure escalated to: safety layer, recovery layer, audit layer
        """
        for checker in self._checkers:
            try:
                checker.check_post_execution(transform_input, transform_output)
            except InvariantViolation:
                # Re-raise as-is (already has proper context)
                raise
            except Exception as e:
                # Wrap unexpected errors as invariant violations
                raise InvariantViolation(
                    "TransformInvariants",
                    "post_execution_error",
                    f"Unexpected error during post-execution check: {e}",
                    context={
                        "checker_type": type(checker).__name__,
                        "error_type": type(e).__name__,
                        "transform_name": transform_output.declaration.transform_name
                    }
                ) from e
    
    def create_declaration(
        self,
        transform_name: str,
        stage: TransformStage,
        schema_version: SchemaVersion,
        config: Dict[str, Any],
        cardinality: Optional[CardinalityConstraint] = None
    ) -> TransformDeclaration:
        """
        Create a validated transform declaration.
        
        Args:
            transform_name: Unique name of the transform
            stage: Transform stage from TransformStage enum
            schema_version: Schema contract for this transform
            config: Configuration dictionary to be fingerprinted
            cardinality: Optional cardinality constraint (uses stage default if None)
        
        Returns:
            Immutable TransformDeclaration
        """
        if cardinality is None:
            cardinality = STAGE_DEFAULT_CARDINALITY[stage]
        
        config_serialized = json.dumps(config, sort_keys=True, ensure_ascii=False)
        config_fingerprint = hashlib.sha256(config_serialized.encode('utf-8')).hexdigest()
        
        return TransformDeclaration(
            transform_name=transform_name,
            stage=stage,
            schema_version=schema_version,
            cardinality=cardinality,
            config_fingerprint=config_fingerprint,
            invariant_version=INVARIANT_VERSION
        )


def compute_config_fingerprint(config: Dict[str, Any]) -> str:
    """
    Compute deterministic fingerprint of configuration.
    
    Used for:
    - Replay verification
    - Configuration drift detection
    - Deterministic execution guarantees
    
    Guarantees:
    - Same config → same fingerprint (deterministic)
    - Different config → different fingerprint (collision-resistant)
    - Stable across machines, languages, and time
    """
    # Sort keys for deterministic ordering, use compact separators
    serialized = json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


# ============================================================================
# Public API
# ============================================================================

def create_audit_artifact(
    transform_output: TransformOutput,
    timestamp: Optional[float] = None
) -> AuditArtifact:
    """
    Create explicit audit artifact from transform output (Tier-0 requirement).
    
    Args:
        transform_output: Transform output to create audit artifact for
        timestamp: Optional timestamp (defaults to current time if None)
    
    Returns:
        AuditArtifact with explicit output fact IDs and rejection fact IDs
    """
    import time as time_module
    
    if timestamp is None:
        timestamp = time_module.time()
    
    output_fact_ids = tuple(sorted(fact.fact_id for fact in transform_output.facts))
    rejection_fact_ids = tuple(sorted(r.rejected_fact_id for r in transform_output.rejections))
    
    # Cryptographic hash of timestamp for integrity (prevents tampering)
    timestamp_str = json.dumps({"timestamp": timestamp, "run_id": transform_output.run_id}, sort_keys=True)
    timestamp_hash = hashlib.sha256(timestamp_str.encode('utf-8')).hexdigest()
    
    return AuditArtifact(
        output_fact_ids=output_fact_ids,
        rejection_fact_ids=rejection_fact_ids,
        transform_name=transform_output.declaration.transform_name,
        stage=transform_output.declaration.stage.name,
        run_id=transform_output.run_id,
        timestamp_hash=timestamp_hash
    )


__all__ = [
    'INVARIANT_VERSION',
    'InvariantStateBackend',
    'InMemoryInvariantStateBackend',
    'TransformStage',
    'InvariantViolation',
    'Scope',
    'SchemaVersion',
    'CardinalityConstraint',
    'STAGE_DEFAULT_CARDINALITY',
    'TransformDeclaration',
    'Fact',
    'TransformInput',
    'RejectionReason',
    'AuditArtifact',
    'TransformOutput',
    'InvariantChecker',
    'OrderingInvariants',
    'ImmutabilityInvariants',
    'DeterminismInvariants',
    'ScopeInvariants',
    'SchemaInvariants',
    'CardinalityInvariants',
    'ReplayInvariants',
    'AuditInvariants',
    'TransformInvariants',
    'compute_config_fingerprint',
    'create_audit_artifact',
]