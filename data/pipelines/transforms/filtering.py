"""
/data/pipelines/transforms/filtering.py

Deterministic Inclusion / Exclusion Authority (No Heuristics, No Guessing)

WHAT THIS FILE ACTUALLY IS (plain English):
filtering.py is the explicit gate that decides which normalized facts are allowed
to continue and which are deliberately excluded — with receipts.

It answers:
> "Given our declared rules, does this fact belong in the canonical record?"

Every exclusion is intentional, explainable, and auditable.

WHAT THIS FILE IS NOT (STRICT):
❌ Not normalization
❌ Not deduplication
❌ Not validation
❌ Not ranking
❌ Not prioritization
❌ Not sampling
❌ Not backpressure

This file does not manage load or quality — it enforces policy-based membership.

DESIGN PRINCIPLE (CRITICAL):
> Filtering is a policy decision, not a data cleanliness decision.

If something is excluded, it is because we chose to exclude it — not because it was inconvenient.

CORE RESPONSIBILITIES (NON-NEGOTIABLE):
filtering.py MUST:
1. Apply explicit inclusion rules
2. Apply explicit exclusion rules
3. Enforce scope-based policies
4. Support versioned filter policies
5. Produce exclusion evidence
6. Preserve rejected facts for audit
7. Never silently drop data

Filtering decides membership, not correctness.

MENTAL MODEL (LOCK THIS):
> Validation says "is it real?"
Filtering says "do we care?"

Both matter. They are not interchangeable.

Policy-driven, audit-proof, replay-safe, and sealed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, FrozenSet, Callable
from abc import ABC, abstractmethod
import hashlib
import json
from datetime import datetime

from transform_invariants import (
    TransformStage,
    TransformInvariants,
    TransformInput,
    TransformOutput,
    TransformDeclaration,
    Fact,
    RejectionReason,
    Scope,
    SchemaVersion,
    InvariantViolation,
)


class FilterDecision(Enum):
    """
    Strict filtering decision enum.
    
    Binary and final.
    
    No MAYBE. No DEFER. No SAMPLE.
    """
    INCLUDE = "include"
    EXCLUDE = "exclude"


class FilteringErrorCode(Enum):
    """
    Stable, enumerable error codes for filtering failures.
    
    Filtering failures are fatal and explicit.
    """
    MISSING_POLICY = "missing_policy"
    UNKNOWN_SCHEMA = "unknown_schema"
    INVALID_RULE = "invalid_rule"
    MULTIPLE_TERMINAL_RULES = "multiple_terminal_rules"
    INVARIANT_VIOLATION = "invariant_violation"
    MISSING_CONTEXT = "missing_context"
    INVALID_DECISION = "invalid_decision"
    INVALID_EXCLUSION = "invalid_exclusion"
    INVALID_POLICY = "invalid_policy"


class FilteringError(Exception):
    """
    Fatal filtering error. System must halt.
    
    If filtering cannot decide, it must block, not pass.
    """
    def __init__(self, error_code: FilteringErrorCode, details: str):
        self.error_code = error_code
        self.details = details
        super().__init__(f"FILTERING ERROR [{error_code.value}] {details}")


class FilteringSource(Enum):
    """Where this filtering execution originated."""
    INGEST = "ingest"
    RECOVERY = "recovery"
    REPLAY = "replay"


@dataclass(frozen=True)
class FilteringContext:
    """
    Immutable execution metadata for filtering operation.
    
    Context is mandatory for traceability.
    """
    pipeline_name: str
    stage_name: str
    schema_name: str
    schema_version: str
    scope: str  # global / account / content / workflow
    scope_id: str
    source: FilteringSource
    run_id: str
    timestamp: str  # Logical timestamp (not wall clock)
    
    def __post_init__(self) -> None:
        """Validate context is complete and well-formed."""
        required_fields = {
            "pipeline_name": self.pipeline_name,
            "stage_name": self.stage_name,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
        }
        
        missing = [k for k, v in required_fields.items() if not v]
        if missing:
            raise FilteringError(
                FilteringErrorCode.MISSING_CONTEXT,
                f"Missing required context fields: {missing}"
            )
        
        if not isinstance(self.source, FilteringSource):
            raise TypeError(
                f"source must be FilteringSource, got {type(self.source)}"
            )


@dataclass(frozen=True)
class FilterPolicy:
    """
    Immutable declarative policy defining what belongs.
    
    No inline logic. No dynamic tuning. No environment-based behavior.
    Policies are immutable once activated.
    """
    policy_id: str
    policy_version: str
    
    allowed_schemas: FrozenSet[str]
    blocked_schemas: FrozenSet[str]
    
    allowed_sources: FrozenSet[str]
    blocked_sources: FrozenSet[str]
    
    allowed_scopes: FrozenSet[str]
    blocked_scopes: FrozenSet[str]
    
    field_presence_requirements: FrozenSet[str]
    field_value_constraints: Dict[str, Any]
    
    default_decision: FilterDecision
    
    def __post_init__(self) -> None:
        """Validate policy is complete and well-formed."""
        if not self.policy_id or not self.policy_version:
            raise FilteringError(
                FilteringErrorCode.MISSING_POLICY,
                "Policy must have ID and version"
            )
        
        # Validate no conflicting rules
        if self.allowed_schemas & self.blocked_schemas:
            raise FilteringError(
                FilteringErrorCode.INVALID_RULE,
                f"Schema cannot be both allowed and blocked: {self.allowed_schemas & self.blocked_schemas}"
            )
        
        if self.allowed_sources & self.blocked_sources:
            raise FilteringError(
                FilteringErrorCode.INVALID_RULE,
                f"Source cannot be both allowed and blocked: {self.allowed_sources & self.blocked_sources}"
            )
        
        if self.allowed_scopes & self.blocked_scopes:
            raise FilteringError(
                FilteringErrorCode.INVALID_RULE,
                f"Scope cannot be both allowed and blocked: {self.allowed_scopes & self.blocked_scopes}"
            )
        
        # Validate default decision is explicit
        if not isinstance(self.default_decision, FilterDecision):
            raise FilteringError(
                FilteringErrorCode.INVALID_RULE,
                f"default_decision must be FilterDecision, got {type(self.default_decision)}"
            )
    
    def to_fingerprint(self) -> str:
        """Compute deterministic fingerprint of this policy."""
        serialized = json.dumps({
            'policy_id': self.policy_id,
            'policy_version': self.policy_version,
            'allowed_schemas': sorted(self.allowed_schemas),
            'blocked_schemas': sorted(self.blocked_schemas),
            'allowed_sources': sorted(self.allowed_sources),
            'blocked_sources': sorted(self.blocked_sources),
            'allowed_scopes': sorted(self.allowed_scopes),
            'blocked_scopes': sorted(self.blocked_scopes),
            'field_presence_requirements': sorted(self.field_presence_requirements),
            'field_value_constraints': self.field_value_constraints,
            'default_decision': self.default_decision.value,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class FilterRule:
    """
    Atomic, single-purpose filtering rule.
    
    Each rule is:
    - Single-purpose
    - Deterministic
    - Side-effect free
    - Independently auditable
    """
    rule_id: str
    rule_name: str
    rule_description: str
    decision: FilterDecision
    terminal: bool
    
    def __post_init__(self) -> None:
        """Validate rule is complete and well-formed."""
        if not self.rule_id or not self.rule_name:
            raise FilteringError(
                FilteringErrorCode.INVALID_RULE,
                "Rule must have ID and name"
            )
        
        if not isinstance(self.decision, FilterDecision):
            raise FilteringError(
                FilteringErrorCode.INVALID_RULE,
                f"decision must be FilterDecision, got {type(self.decision)}"
            )


@dataclass(frozen=True)
class ExclusionEvidence:
    """
    Mandatory evidence for every exclusion.
    
    Enables:
    - Forensic reconstruction
    - Replay correctness
    - Policy audits
    - Regulatory disclosure
    """
    fact_id: str
    exclusion_reason: str
    rule_id: str
    rule_name: str
    policy_id: str
    policy_version: str
    payload_hash: str
    timestamp: str
    run_id: str
    context: FilteringContext
    
    def __post_init__(self) -> None:
        """Validate exclusion evidence is complete."""
        required_fields = {
            "fact_id": self.fact_id,
            "exclusion_reason": self.exclusion_reason,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "payload_hash": self.payload_hash,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
        }
        
        missing = [k for k, v in required_fields.items() if not v]
        if missing:
            raise FilteringError(
                FilteringErrorCode.INVALID_EXCLUSION,
                f"Missing required exclusion evidence fields: {missing}"
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for audit logging."""
        return {
            "fact_id": self.fact_id,
            "exclusion_reason": self.exclusion_reason,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "payload_hash": self.payload_hash,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "context": {
                "pipeline_name": self.context.pipeline_name,
                "stage_name": self.context.stage_name,
                "schema_name": self.context.schema_name,
                "scope": self.context.scope,
                "source": str(self.context.source.value) if hasattr(self.context.source, 'value') else str(self.context.source),
            }
        }


class FilterRuleEvaluator(ABC):
    """Base class for filter rule evaluation."""
    
    @abstractmethod
    def evaluate(self, fact: Fact, context: FilteringContext) -> Optional[FilterDecision]:
        """
        Evaluate rule against fact.
        
        Returns:
            FilterDecision if rule matches, None if rule doesn't apply
        """
        pass
    
    @abstractmethod
    def get_rule(self) -> FilterRule:
        """Return the rule metadata."""
        pass


class SchemaFilterRule(FilterRuleEvaluator):
    """Filter based on schema allow/block lists."""
    
    def __init__(
        self,
        rule_id: str,
        allowed_schemas: FrozenSet[str],
        blocked_schemas: FrozenSet[str]
    ):
        self._rule = FilterRule(
            rule_id=rule_id,
            rule_name="schema_filter",
            rule_description=f"Allow schemas: {allowed_schemas}, Block schemas: {blocked_schemas}",
            decision=FilterDecision.EXCLUDE,
            terminal=True
        )
        self._allowed = allowed_schemas
        self._blocked = blocked_schemas
    
    def evaluate(self, fact: Fact, context: FilteringContext) -> Optional[FilterDecision]:
        schema_name = fact.schema_version.schema_name
        
        if self._blocked and schema_name in self._blocked:
            return FilterDecision.EXCLUDE
        
        if self._allowed and schema_name not in self._allowed:
            return FilterDecision.EXCLUDE
        
        return None
    
    def get_rule(self) -> FilterRule:
        return self._rule


class SourceFilterRule(FilterRuleEvaluator):
    """Filter based on source allow/block lists."""
    
    def __init__(
        self,
        rule_id: str,
        allowed_sources: FrozenSet[str],
        blocked_sources: FrozenSet[str]
    ):
        self._rule = FilterRule(
            rule_id=rule_id,
            rule_name="source_filter",
            rule_description=f"Allow sources: {allowed_sources}, Block sources: {blocked_sources}",
            decision=FilterDecision.EXCLUDE,
            terminal=True
        )
        self._allowed = allowed_sources
        self._blocked = blocked_sources
    
    def evaluate(self, fact: Fact, context: FilteringContext) -> Optional[FilterDecision]:
        source = fact.scope.source
        
        if self._blocked and source in self._blocked:
            return FilterDecision.EXCLUDE
        
        if self._allowed and source not in self._allowed:
            return FilterDecision.EXCLUDE
        
        return None
    
    def get_rule(self) -> FilterRule:
        return self._rule


class ScopeFilterRule(FilterRuleEvaluator):
    """Filter based on scope allow/block lists."""
    
    def __init__(
        self,
        rule_id: str,
        allowed_scopes: FrozenSet[str],
        blocked_scopes: FrozenSet[str]
    ):
        self._rule = FilterRule(
            rule_id=rule_id,
            rule_name="scope_filter",
            rule_description=f"Allow scopes: {allowed_scopes}, Block scopes: {blocked_scopes}",
            decision=FilterDecision.EXCLUDE,
            terminal=True
        )
        self._allowed = allowed_scopes
        self._blocked = blocked_scopes
    
    def evaluate(self, fact: Fact, context: FilteringContext) -> Optional[FilterDecision]:
        scope_type = fact.scope.scope_type
        
        if self._blocked and scope_type in self._blocked:
            return FilterDecision.EXCLUDE
        
        if self._allowed and scope_type not in self._allowed:
            return FilterDecision.EXCLUDE
        
        return None
    
    def get_rule(self) -> FilterRule:
        return self._rule


class FieldPresenceFilterRule(FilterRuleEvaluator):
    """Filter based on required field presence."""
    
    def __init__(
        self,
        rule_id: str,
        required_fields: FrozenSet[str]
    ):
        self._rule = FilterRule(
            rule_id=rule_id,
            rule_name="field_presence_filter",
            rule_description=f"Require fields: {required_fields}",
            decision=FilterDecision.EXCLUDE,
            terminal=True
        )
        self._required = required_fields
    
    def evaluate(self, fact: Fact, context: FilteringContext) -> Optional[FilterDecision]:
        if not self._required:
            return None
        
        missing_fields = self._required - set(fact.payload.keys())
        if missing_fields:
            return FilterDecision.EXCLUDE
        
        return None
    
    def get_rule(self) -> FilterRule:
        return self._rule


class FieldValueFilterRule(FilterRuleEvaluator):
    """Filter based on field value constraints."""
    
    def __init__(
        self,
        rule_id: str,
        field_name: str,
        allowed_values: FrozenSet[Any]
    ):
        self._rule = FilterRule(
            rule_id=rule_id,
            rule_name="field_value_filter",
            rule_description=f"Field '{field_name}' must be in {allowed_values}",
            decision=FilterDecision.EXCLUDE,
            terminal=True
        )
        self._field = field_name
        self._allowed = allowed_values
    
    def evaluate(self, fact: Fact, context: FilteringContext) -> Optional[FilterDecision]:
        if self._field not in fact.payload:
            return None
        
        value = fact.payload[self._field]
        if value not in self._allowed:
            return FilterDecision.EXCLUDE
        
        return None
    
    def get_rule(self) -> FilterRule:
        return self._rule


class FilterEvaluator:
    """
    Deterministic rule evaluation engine (BRAIN).
    
    Responsible for deciding, not enforcing.
    
    Conceptual guarantees:
    - Deterministic rule ordering
    - First matching terminal rule wins
    - Explicit default (include or exclude — never implicit)
    - Fail-closed on ambiguity
    
    Same input + same policy = same decision.
    """
    
    def __init__(self, policy: FilterPolicy):
        """
        Initialize filter evaluator.
        
        Args:
            policy: Filter policy to evaluate against
        """
        self._policy = policy
        self._rules: List[FilterRuleEvaluator] = self._build_rules()
    
    def _build_rules(self) -> List[FilterRuleEvaluator]:
        """Build ordered list of rule evaluators from policy."""
        rules: List[FilterRuleEvaluator] = []
        
        if self._policy.blocked_schemas or self._policy.allowed_schemas:
            rules.append(SchemaFilterRule(
                rule_id=f"{self._policy.policy_id}::schema",
                allowed_schemas=self._policy.allowed_schemas,
                blocked_schemas=self._policy.blocked_schemas
            ))
        
        if self._policy.blocked_sources or self._policy.allowed_sources:
            rules.append(SourceFilterRule(
                rule_id=f"{self._policy.policy_id}::source",
                allowed_sources=self._policy.allowed_sources,
                blocked_sources=self._policy.blocked_sources
            ))
        
        if self._policy.blocked_scopes or self._policy.allowed_scopes:
            rules.append(ScopeFilterRule(
                rule_id=f"{self._policy.policy_id}::scope",
                allowed_scopes=self._policy.allowed_scopes,
                blocked_scopes=self._policy.blocked_scopes
            ))
        
        if self._policy.field_presence_requirements:
            rules.append(FieldPresenceFilterRule(
                rule_id=f"{self._policy.policy_id}::field_presence",
                required_fields=self._policy.field_presence_requirements
            ))
        
        # Canonicalize rule creation order: sort field names for replay-safe determinism
        # Dict iteration order is not guaranteed cross-runtime, so we must sort keys
        for field_name in sorted(self._policy.field_value_constraints.keys()):
            allowed_values = self._policy.field_value_constraints[field_name]
            if isinstance(allowed_values, (set, frozenset, list)):
                rules.append(FieldValueFilterRule(
                    rule_id=f"{self._policy.policy_id}::field_value::{field_name}",
                    field_name=field_name,
                    allowed_values=frozenset(allowed_values)
                ))
        
        return rules
    
    def evaluate(
        self,
        fact: Fact,
        context: FilteringContext
    ) -> Tuple[FilterDecision, Optional[FilterRule]]:
        """
        Evaluate fact against all rules.
        
        Deterministic rule ordering: rules are evaluated in the order they were built.
        First matching terminal rule wins.
        If no rule matches, default decision is used.
        
        Args:
            fact: Fact to evaluate
            context: Filtering execution context
            
        Returns:
            Tuple of (decision, matching_rule) where matching_rule is None if using default
            
        Raises:
            FilteringError: If multiple terminal rules match (ambiguity)
        """
        matching_terminal_rules: List[FilterRule] = []
        first_matching_decision: Optional[FilterDecision] = None
        first_matching_rule: Optional[FilterRule] = None
        
        # Evaluate ALL rules first to detect ambiguity (fail-closed on ambiguity)
        for rule_evaluator in self._rules:
            decision = rule_evaluator.evaluate(fact, context)
            if decision is not None:
                rule = rule_evaluator.get_rule()
                if rule.terminal:
                    matching_terminal_rules.append(rule)
                    # Track first match for deterministic application
                    if first_matching_decision is None:
                        first_matching_decision = decision
                        first_matching_rule = rule
        
        # Check for ambiguity BEFORE applying decision (fail-closed guarantee)
        if len(matching_terminal_rules) > 1:
            raise FilteringError(
                FilteringErrorCode.MULTIPLE_TERMINAL_RULES,
                f"Multiple terminal rules matched: {[r.rule_id for r in matching_terminal_rules]}"
            )
        
        # Apply first matching terminal rule deterministically (if exactly one matched)
        if first_matching_decision is not None:
            return (first_matching_decision, first_matching_rule)
        
        # No rule matched → use default decision
        return (self._policy.default_decision, None)


class FilterExecutor:
    """
    Mechanism that enforces filtering decisions (MECHANISM).
    
    Enforces the decision:
    
    INCLUDED → pass through unchanged
    
    EXCLUDED → emit exclusion record + block downstream flow
    
    Executor responsibilities:
    - Record exclusion reason
    - Attach policy & rule IDs
    - Preserve original payload hash
    - Emit audit event
    
    Excluded data is never destroyed.
    """
    
    def __init__(self, policy: FilterPolicy, context: FilteringContext):
        """
        Initialize filter executor.
        
        Args:
            policy: Filter policy to execute
            context: Filtering execution context
        """
        self._policy = policy
        self._context = context
        self._evaluator = FilterEvaluator(policy)
    
    def execute(
        self,
        facts: Tuple[Fact, ...]
    ) -> Tuple[Tuple[Fact, ...], Tuple[ExclusionEvidence, ...]]:
        """
        Execute filtering on facts.
        
        Args:
            facts: Facts to filter (immutable)
            
        Returns:
            Tuple of (included_facts, exclusion_evidence)
            - included_facts: Facts that passed filtering (unchanged)
            - exclusion_evidence: Evidence records for excluded facts
            
        Raises:
            FilteringError: If filtering cannot decide (must block, not pass)
        """
        included: List[Fact] = []
        excluded: List[ExclusionEvidence] = []
        
        # Process facts in deterministic order
        for fact in facts:
            try:
                decision, rule = self._evaluator.evaluate(fact, self._context)
            except FilteringError:
                # Re-raise filtering errors as-is
                raise
            except Exception as e:
                # Wrap unexpected errors as filtering errors
                raise FilteringError(
                    FilteringErrorCode.INVARIANT_VIOLATION,
                    f"Unexpected error during filtering evaluation: {e}"
                ) from e
            
            if decision == FilterDecision.INCLUDE:
                # Pass through unchanged (no mutation)
                included.append(fact)
            elif decision == FilterDecision.EXCLUDE:
                # Create exclusion evidence (mandatory)
                evidence = self._create_exclusion_evidence(fact, rule)
                excluded.append(evidence)
            else:
                raise FilteringError(
                    FilteringErrorCode.INVALID_DECISION,
                    f"Unexpected decision: {decision}. Only INCLUDE and EXCLUDE are allowed."
                )
        
        return (tuple(included), tuple(excluded))
    
    def _create_exclusion_evidence(
        self,
        fact: Fact,
        rule: Optional[FilterRule]
    ) -> ExclusionEvidence:
        """Create mandatory exclusion evidence."""
        if rule is None:
            exclusion_reason = f"Default policy decision: {self._policy.default_decision.value}"
            rule_id = f"{self._policy.policy_id}::default"
            rule_name = "default_decision"
        else:
            exclusion_reason = rule.rule_description
            rule_id = rule.rule_id
            rule_name = rule.rule_name
        
        return ExclusionEvidence(
            fact_id=fact.fact_id,
            exclusion_reason=exclusion_reason,
            rule_id=rule_id,
            rule_name=rule_name,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.policy_version,
            payload_hash=fact.payload_hash,
            timestamp=self._context.timestamp,
            run_id=self._context.run_id,
            context=self._context
        )


class FilteringInvariants:
    """
    Absolute invariants for filtering operations.
    
    Enforces:
    - no mutation of payload
    - no partial exclusion
    - no probabilistic rules
    - no time-based guessing
    - no data-dependent policy changes
    - no silent exclusions
    
    Violation → immediate hard stop.
    """
    
    @staticmethod
    def enforce_no_mutation(
        input_facts: Tuple[Fact, ...],
        output_facts: Tuple[Fact, ...]
    ) -> None:
        """Ensure no facts were mutated during filtering."""
        input_map = {f.fact_id: f.payload_hash for f in input_facts}
        
        for fact in output_facts:
            if fact.fact_id in input_map:
                if fact.payload_hash != input_map[fact.fact_id]:
                    raise InvariantViolation(
                        "FilteringInvariants",
                        "no_mutation",
                        f"Fact {fact.fact_id} was mutated during filtering"
                    )
    
    @staticmethod
    def enforce_complete_accounting(
        input_facts: Tuple[Fact, ...],
        output_facts: Tuple[Fact, ...],
        exclusions: Tuple[ExclusionEvidence, ...]
    ) -> None:
        """Ensure all facts are accounted for (included or excluded)."""
        input_ids = {f.fact_id for f in input_facts}
        output_ids = {f.fact_id for f in output_facts}
        excluded_ids = {e.fact_id for e in exclusions}
        
        if output_ids & excluded_ids:
            raise InvariantViolation(
                "FilteringInvariants",
                "no_partial_exclusion",
                f"Facts both included and excluded: {output_ids & excluded_ids}"
            )
        
        accounted_ids = output_ids | excluded_ids
        if accounted_ids != input_ids:
            missing = input_ids - accounted_ids
            extra = accounted_ids - input_ids
            raise InvariantViolation(
                "FilteringInvariants",
                "complete_accounting",
                f"Missing: {missing}, Extra: {extra}"
            )
    
    @staticmethod
    def enforce_exclusion_evidence(exclusions: Tuple[ExclusionEvidence, ...]) -> None:
        """
        Ensure all exclusions have mandatory evidence.
        
        Hard rule: no silent exclusions.
        
        Args:
            exclusions: Exclusion evidence records to validate
            
        Raises:
            InvariantViolation: If any exclusion is missing mandatory evidence
        """
        for evidence in exclusions:
            if not evidence.exclusion_reason:
                raise InvariantViolation(
                    "FilteringInvariants",
                    "exclusion_evidence",
                    f"Exclusion {evidence.fact_id} missing reason. No evidence → exclusion is illegal."
                )
            if not evidence.rule_id or not evidence.policy_id:
                raise InvariantViolation(
                    "FilteringInvariants",
                    "exclusion_evidence",
                    f"Exclusion {evidence.fact_id} missing rule/policy ID. No evidence → exclusion is illegal."
                )
            if not evidence.payload_hash:
                raise InvariantViolation(
                    "FilteringInvariants",
                    "exclusion_evidence",
                    f"Exclusion {evidence.fact_id} missing payload_hash. No evidence → exclusion is illegal."
                )
    
    @staticmethod
    def enforce_no_payload_mutation(
        input_facts: Tuple[Fact, ...],
        output_facts: Tuple[Fact, ...]
    ) -> None:
        """
        Verify no facts were mutated during filtering.
        
        Hard rule: no mutation of payload.
        
        Args:
            input_facts: Original facts
            output_facts: Facts after filtering
            
        Raises:
            InvariantViolation: If any fact was mutated
        """
        input_map = {f.fact_id: f.payload_hash for f in input_facts}
        
        for fact in output_facts:
            if fact.fact_id in input_map:
                if fact.payload_hash != input_map[fact.fact_id]:
                    raise InvariantViolation(
                        "FilteringInvariants",
                        "no_mutation",
                        f"Fact {fact.fact_id} was mutated during filtering. Payload must remain unchanged."
                    )


class FilteringPipeline:
    """
    Complete filtering pipeline with invariant enforcement.
    
    Coordinates:
    - Policy evaluation
    - Decision execution
    - Evidence collection
    - Invariant checking
    """
    
    def __init__(
        self,
        policy: FilterPolicy,
        transform_invariants: TransformInvariants,
        schema_version: SchemaVersion
    ):
        self._policy = policy
        self._invariants = transform_invariants
        self._schema_version = schema_version
    
    def filter(
        self,
        facts: Tuple[Fact, ...],
        context: FilteringContext
    ) -> Tuple[TransformInput, TransformOutput]:
        """
        Execute filtering with full invariant enforcement.
        
        Traceability: run_id is derived solely from context (single source of truth).
        
        Args:
            facts: Facts to filter
            context: Filtering execution context (contains authoritative run_id)
            
        Returns:
            (transform_input, transform_output) for downstream processing
        """
        # Derive run_id from context (single authoritative source)
        run_id = context.run_id
        
        config = {
            'policy_id': self._policy.policy_id,
            'policy_version': self._policy.policy_version,
            'policy_fingerprint': self._policy.to_fingerprint(),
            'context': {
                'pipeline_name': context.pipeline_name,
                'scope': context.scope,
                'source': context.source,
            }
        }
        
        declaration = self._invariants.create_declaration(
            transform_name=f"filter::{self._policy.policy_id}",
            stage=TransformStage.FILTERING,
            schema_version=self._schema_version,
            config=config
        )
        
        transform_input = TransformInput(
            facts=facts,
            declaration=declaration,
            run_id=run_id
        )
        
        self._invariants.enforce_pre_execution(transform_input)
        
        executor = FilterExecutor(self._policy, context)
        included_facts, exclusion_evidence = executor.execute(facts)
        
        FilteringInvariants.enforce_no_mutation(facts, included_facts)
        FilteringInvariants.enforce_complete_accounting(facts, included_facts, exclusion_evidence)
        FilteringInvariants.enforce_exclusion_evidence(exclusion_evidence)
        
        rejections = tuple(
            RejectionReason(
                reason_code=f"FILTERED::{e.rule_name}",
                reason_detail=e.exclusion_reason,
                rejected_fact_id=e.fact_id
            )
            for e in exclusion_evidence
        )
        
        transform_output = TransformOutput(
            facts=included_facts,
            rejections=rejections,
            declaration=declaration,
            input_fact_ids=tuple(sorted(f.fact_id for f in facts)),
            run_id=run_id
        )
        
        self._invariants.enforce_post_execution(transform_input, transform_output)
        
        return (transform_input, transform_output)


def create_default_policy(
    policy_id: str = "default",
    policy_version: str = "1.0.0",
    allow_non_production: bool = False
) -> FilterPolicy:
    """
    Create a permissive default policy that includes everything.
    
    TIER-0 WARNING: This function is a production footgun.
    
    A permissive default policy (default_decision=INCLUDE) violates the blueprint mandate:
    > "No implicit inclusion"
    > "No default allow without policy"
    
    This function is ONLY available for non-production use (testing, development, migration).
    In production, explicit policies MUST be used.
    
    Args:
        policy_id: Policy identifier
        policy_version: Policy version
        allow_non_production: MUST be True to use this function. Prevents accidental
            production use of permissive default policy.
        
    Returns:
        Permissive filter policy with default_decision=INCLUDE
        
    Raises:
        FilteringError: If allow_non_production is False (production safety gate)
    """
    if not allow_non_production:
        raise FilteringError(
            FilteringErrorCode.INVALID_POLICY,
            "create_default_policy() is forbidden in production. "
            "Set allow_non_production=True only for testing/development. "
            "Production must use explicit policies (create_strict_policy or custom FilterPolicy)."
        )
    
    return FilterPolicy(
        policy_id=policy_id,
        policy_version=policy_version,
        allowed_schemas=frozenset(),
        blocked_schemas=frozenset(),
        allowed_sources=frozenset(),
        blocked_sources=frozenset(),
        allowed_scopes=frozenset(),
        blocked_scopes=frozenset(),
        field_presence_requirements=frozenset(),
        field_value_constraints={},
        default_decision=FilterDecision.INCLUDE
    )


def create_strict_policy(
    policy_id: str,
    policy_version: str,
    allowed_schemas: Set[str],
    allowed_sources: Set[str],
    allowed_scopes: Set[str]
) -> FilterPolicy:
    """
    Create a strict allowlist-based policy.
    
    Only explicitly allowed schemas, sources, and scopes are included.
    Everything else is excluded by default.
    
    Args:
        policy_id: Policy identifier
        policy_version: Policy version
        allowed_schemas: Set of allowed schema names
        allowed_sources: Set of allowed source names
        allowed_scopes: Set of allowed scope names
        
    Returns:
        Strict filter policy with default_decision=EXCLUDE
    """
    return FilterPolicy(
        policy_id=policy_id,
        policy_version=policy_version,
        allowed_schemas=frozenset(allowed_schemas),
        blocked_schemas=frozenset(),
        allowed_sources=frozenset(allowed_sources),
        blocked_sources=frozenset(),
        allowed_scopes=frozenset(allowed_scopes),
        blocked_scopes=frozenset(),
        field_presence_requirements=frozenset(),
        field_value_constraints={},
        default_decision=FilterDecision.EXCLUDE
    )


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    'FilterDecision',
    'FilteringSource',
    'FilteringErrorCode',
    'FilteringError',
    'FilteringContext',
    'FilterPolicy',
    'FilterRule',
    'ExclusionEvidence',
    'FilterRuleEvaluator',
    'SchemaFilterRule',
    'SourceFilterRule',
    'ScopeFilterRule',
    'FieldPresenceFilterRule',
    'FieldValueFilterRule',
    'FilterEvaluator',
    'FilterExecutor',
    'FilteringInvariants',
    'FilteringPipeline',
    'create_default_policy',
    'create_strict_policy',
]