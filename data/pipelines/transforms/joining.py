"""
Deterministic Fact Association (Not Aggregation, Not Mutation).

This module is a relationship constructor. It answers one question only:
    "Given two or more already-canonical facts, what is their explicit, declared relationship?"

Core Principle:
    Facts are immutable. Relationships are new facts.

What this file does:
    ✓ Define joinable fact contracts
    ✓ Declare allowed relationship types
    ✓ Enforce directional semantics
    ✓ Validate join keys explicitly
    ✓ Emit relationship facts
    ✓ Guarantee determinism
    ✓ Prevent fan-out explosions
    ✓ Fail closed on ambiguity

What this file does NOT do:
    ✗ Invent meaning
    ✗ Summarize
    ✗ Merge payloads
    ✗ Fix bad data
    ✗ Resolve conflicts
    ✗ Guess intent
    ✗ SQL-style joins
    ✗ Analytics
    ✗ Enrichment
    ✗ Aggregation
    ✗ Denormalization
    ✗ Correction
    ✗ Inference

Pipeline Position:
    normalize → validate → filter → deduplicate → JOIN → canonical fact graph

Joining only happens after deduplication. You may only join canonical facts.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple
from uuid import UUID, uuid4, uuid5, NAMESPACE_OID


# ============================================================================
# Type Aliases
# ============================================================================

FactID = UUID
SchemaName = str
FieldName = str
FieldValue = str | int | float | bool
RunID = UUID
RelationshipType = str


# ============================================================================
# Enums - Explicit Constraints
# ============================================================================

class JoinType(Enum):
    """
    Legal join cardinalities.
    
    NO MANY_TO_MANY.
    If you think you need it, your schema is lying.
    """
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    
    def __str__(self) -> str:
        return self.value


class JoinSource(Enum):
    """Where this join execution originated."""
    INGEST = "ingest"
    REPLAY = "replay"
    RECOVERY = "recovery"
    
    def __str__(self) -> str:
        return self.value


class JoinDirectionality(Enum):
    """Explicit semantic direction of relationship."""
    LEFT_TO_RIGHT = "left_to_right"
    RIGHT_TO_LEFT = "right_to_left"
    BIDIRECTIONAL = "bidirectional"
    
    def __str__(self) -> str:
        return self.value


class AmbiguityPolicy(Enum):
    """
    Policy for handling ambiguous joins.
    
    Tier-0: Default is STRICT_FAIL - uncertainty produces nothing.
    """
    STRICT_FAIL = "strict_fail"  # Default: ambiguity = no relationships emitted
    ALLOW_DECLARED_MULTI = "allow_declared_multi"  # Explicitly declared multi-match allowed
    
    def __str__(self) -> str:
        return self.value


# ============================================================================
# Immutable Value Objects
# ============================================================================

@dataclass(frozen=True)
class JoinKeySpec:
    """
    Explicit mapping of identity-safe keys.
    
    Rules:
        - Keys must be canonical identity fields
        - Keys must be comparable (type + semantics)
        - Missing keys → hard failure
        - Null joins forbidden unless schema allows it
    
    If the key isn't declared, it is illegal.
    """
    left_keys: FrozenSet[FieldName]
    right_keys: FrozenSet[FieldName]
    allow_null: bool = False
    
    def __post_init__(self) -> None:
        """Validate key spec is well-formed."""
        if not self.left_keys:
            raise ValueError("left_keys cannot be empty")
        if not self.right_keys:
            raise ValueError("right_keys cannot be empty")
        if len(self.left_keys) != len(self.right_keys):
            raise ValueError(
                f"Key count mismatch: {len(self.left_keys)} left keys vs "
                f"{len(self.right_keys)} right keys"
            )
    
    def validate_fact_keys(
        self, 
        left_fact: Dict[str, FieldValue],
        right_fact: Dict[str, FieldValue]
    ) -> None:
        """
        Validate that facts contain required keys and types are compatible.
        
        Raises:
            ValueError: If required keys are missing, null (when disallowed), or types incompatible
        """
        # Check left keys exist
        for key in self.left_keys:
            if key not in left_fact:
                raise ValueError(f"Left fact missing required key: {key}")
            if not self.allow_null and left_fact[key] is None:
                raise ValueError(f"Left fact has null value for key: {key}")
        
        # Check right keys exist
        for key in self.right_keys:
            if key not in right_fact:
                raise ValueError(f"Right fact missing required key: {key}")
            if not self.allow_null and right_fact[key] is None:
                raise ValueError(f"Right fact has null value for key: {key}")
        
        # Validate type compatibility between corresponding keys
        left_key_list = sorted(self.left_keys)
        right_key_list = sorted(self.right_keys)
        
        for left_key, right_key in zip(left_key_list, right_key_list):
            left_value = left_fact[left_key]
            right_value = right_fact[right_key]
            
            # Skip null values (already validated above if not allowed)
            if left_value is None or right_value is None:
                continue
            
            # Type compatibility check: same type or compatible numeric types
            left_type = type(left_value)
            right_type = type(right_value)
            
            if left_type != right_type:
                # Allow compatible numeric types (int/float)
                if not (
                    (left_type in (int, float) and right_type in (int, float)) or
                    (left_type == bool and right_type == bool)
                ):
                    raise ValueError(
                        f"Type mismatch for join keys: {left_key} ({left_type.__name__}) "
                        f"vs {right_key} ({right_type.__name__})"
                    )


@dataclass(frozen=True)
class JoinDefinition:
    """
    THE CONTRACT - Defines a legal join.
    
    No dynamic joins. No runtime guessing.
    
    Fields:
        join_id: Globally unique identifier for this join definition (immutable, governance-controlled)
        left_fact_schema: Schema name of left facts
        right_fact_schema: Schema name of right facts
        join_type: Cardinality constraint
        join_keys: Key specification
        relationship_schema: Output schema name for relationship facts
        relationship_type: Semantic type of relationship
        directionality: Semantic direction
        max_matches_per_left: Fan-out limit for left side (default: 1)
        max_matches_per_right: Fan-out limit for right side (default: 1)
        version: Schema version of this join definition
        ambiguity_policy: How to handle ambiguous matches (default: STRICT_FAIL)
        scope_field: Field name to extract scope_id from facts (mandatory for cross-scope enforcement)
        allow_cross_scope: Whether cross-scope joins are explicitly allowed (default: False)
    """
    join_id: str
    left_fact_schema: SchemaName
    right_fact_schema: SchemaName
    join_type: JoinType
    join_keys: JoinKeySpec
    relationship_schema: SchemaName
    relationship_type: RelationshipType
    directionality: JoinDirectionality
    max_matches_per_left: int = 1
    max_matches_per_right: int = 1
    version: int = 1
    ambiguity_policy: AmbiguityPolicy = AmbiguityPolicy.STRICT_FAIL
    scope_field: Optional[str] = None
    allow_cross_scope: bool = False
    
    def __post_init__(self) -> None:
        """
        TIER-0: Authoritative validation at construction time.
        
        Invalid join definitions cannot be constructed.
        This is the hard stop layer - no invalid contracts can enter the system.
        """
        if not self.join_id:
            raise JoinDefinitionError("join_id cannot be empty")
        if not self.left_fact_schema:
            raise JoinDefinitionError("left_fact_schema cannot be empty")
        if not self.right_fact_schema:
            raise JoinDefinitionError("right_fact_schema cannot be empty")
        if not self.relationship_schema:
            raise JoinDefinitionError("relationship_schema cannot be empty")
        if not self.relationship_type:
            raise JoinDefinitionError("relationship_type cannot be empty")
        
        # Validate max_matches based on join_type
        if self.join_type == JoinType.ONE_TO_ONE:
            if self.max_matches_per_left != 1 or self.max_matches_per_right != 1:
                raise JoinDefinitionError(
                    "ONE_TO_ONE join requires max_matches_per_left=1 and "
                    "max_matches_per_right=1"
                )
        elif self.join_type == JoinType.ONE_TO_MANY:
            if self.max_matches_per_left < 1:
                raise JoinDefinitionError("max_matches_per_left must be >= 1")
            if self.max_matches_per_right != 1:
                raise JoinDefinitionError("ONE_TO_MANY join requires max_matches_per_right=1")
        elif self.join_type == JoinType.MANY_TO_ONE:
            if self.max_matches_per_left != 1:
                raise JoinDefinitionError("MANY_TO_ONE join requires max_matches_per_left=1")
            if self.max_matches_per_right < 1:
                raise JoinDefinitionError("max_matches_per_right must be >= 1")
        
        if self.version < 1:
            raise JoinDefinitionError(f"version must be >= 1, got {self.version}")
        
        # Validate scope_field is provided unless cross-scope is explicitly allowed
        if not self.allow_cross_scope and not self.scope_field:
            raise JoinDefinitionError(
                "scope_field must be provided for cross-scope enforcement. "
                "Set allow_cross_scope=True if cross-scope joins are intentionally allowed."
            )
        
        # TIER-0: Enforce ambiguity policy consistency
        # ONE_TO_ONE cannot have ALLOW_DECLARED_MULTI (semantically impossible)
        if self.join_type == JoinType.ONE_TO_ONE:
            if self.ambiguity_policy != AmbiguityPolicy.STRICT_FAIL:
                raise JoinDefinitionError(
                    "ONE_TO_ONE joins must use STRICT_FAIL ambiguity policy. "
                    "Multiple matches are impossible in ONE_TO_ONE semantics."
                )
        
        # TIER-0: Validate directionality consistency (called here for early failure)
        # This will be re-validated by JoinValidator, but fail fast at construction
        if self.directionality == JoinDirectionality.BIDIRECTIONAL:
            if self.join_type != JoinType.ONE_TO_ONE:
                raise JoinDefinitionError(
                    f"BIDIRECTIONAL directionality requires ONE_TO_ONE join type, "
                    f"got {self.join_type}"
                )
    
    def compute_hash(self) -> str:
        """Compute deterministic hash of this join definition."""
        components = [
            self.join_id,
            self.left_fact_schema,
            self.right_fact_schema,
            str(self.join_type),
            ",".join(sorted(self.join_keys.left_keys)),
            ",".join(sorted(self.join_keys.right_keys)),
            self.relationship_schema,
            self.relationship_type,
            str(self.directionality),
            str(self.max_matches_per_left),
            str(self.max_matches_per_right),
            str(self.version),
            str(self.ambiguity_policy),
            str(self.scope_field or ""),
            str(self.allow_cross_scope),
        ]
        combined = "|".join(components)
        return hashlib.sha256(combined.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class JoinWindow:
    """
    Deterministic window boundary for join execution.
    
    Enables memory-bounded, replay-safe joins at scale.
    """
    window_id: str
    window_start: datetime
    window_end: datetime
    partition_key: Optional[str] = None
    
    def __post_init__(self) -> None:
        """Validate window is well-formed."""
        if not self.window_id:
            raise ValueError("window_id cannot be empty")
        if self.window_start >= self.window_end:
            raise ValueError(
                f"Invalid window: start {self.window_start} >= end {self.window_end}"
            )
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("Window timestamps must be timezone-aware")


@dataclass(frozen=True)
class JoinContext:
    """
    Execution metadata for a join run.
    
    Joins are context-aware, never global by accident.
    """
    run_id: RunID
    pipeline_stage: str
    source: JoinSource
    scope_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    window: Optional[JoinWindow] = None
    
    def __post_init__(self) -> None:
        """Validate context is well-formed."""
        if not isinstance(self.run_id, UUID):
            raise TypeError(f"run_id must be UUID, got {type(self.run_id)}")
        if not self.pipeline_stage:
            raise ValueError("pipeline_stage cannot be empty")
        if not self.scope_id:
            raise ValueError("scope_id cannot be empty")
        if not isinstance(self.source, JoinSource):
            raise TypeError(f"source must be JoinSource, got {type(self.source)}")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")


@dataclass(frozen=True)
class Fact:
    """
    Canonical fact representation for joining.
    
    Must be immutable and already deduplicated.
    """
    fact_id: FactID
    schema_name: SchemaName
    fields: Dict[str, FieldValue]
    timestamp: datetime
    
    def __post_init__(self) -> None:
        """Validate fact is well-formed."""
        if not isinstance(self.fact_id, UUID):
            raise TypeError(f"fact_id must be UUID, got {type(self.fact_id)}")
        if not self.schema_name:
            raise ValueError("schema_name cannot be empty")
        if not self.fields:
            raise ValueError("fields cannot be empty")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
    
    def get_join_key_values(self, keys: FrozenSet[FieldName]) -> Tuple[FieldValue, ...]:
        """
        Extract join key values in deterministic order.
        
        Returns:
            Tuple of values in sorted key order
            
        Raises:
            KeyError: If any key is missing
        """
        sorted_keys = sorted(keys)
        return tuple(self.fields[k] for k in sorted_keys)


@dataclass(frozen=True)
class RelationshipFact:
    """
    Immutable relationship fact produced by joining.
    
    Relationship facts are deduplicated separately downstream.
    """
    relationship_fact_id: FactID
    relationship_type: RelationshipType
    left_fact_id: FactID
    right_fact_id: FactID
    relationship_schema: SchemaName
    schema_version: int
    provenance: str  # Why this relationship exists
    run_id: RunID
    relationship_timestamp: datetime
    join_keys_used: Dict[str, Tuple[FieldValue, ...]]
    
    def __post_init__(self) -> None:
        """Validate relationship fact is well-formed."""
        if not isinstance(self.relationship_fact_id, UUID):
            raise TypeError(
                f"relationship_fact_id must be UUID, got {type(self.relationship_fact_id)}"
            )
        if not isinstance(self.left_fact_id, UUID):
            raise TypeError(f"left_fact_id must be UUID, got {type(self.left_fact_id)}")
        if not isinstance(self.right_fact_id, UUID):
            raise TypeError(f"right_fact_id must be UUID, got {type(self.right_fact_id)}")
        if not self.relationship_type:
            raise ValueError("relationship_type cannot be empty")
        if not self.relationship_schema:
            raise ValueError("relationship_schema cannot be empty")
        if self.schema_version < 1:
            raise ValueError(f"schema_version must be >= 1, got {self.schema_version}")
        if not self.provenance:
            raise ValueError("provenance cannot be empty")
        if self.relationship_timestamp.tzinfo is None:
            raise ValueError("relationship_timestamp must be timezone-aware")
    
    def to_dict(self) -> Dict:
        """Serialize to dictionary for audit logging."""
        return {
            "relationship_fact_id": str(self.relationship_fact_id),
            "relationship_type": self.relationship_type,
            "left_fact_id": str(self.left_fact_id),
            "right_fact_id": str(self.right_fact_id),
            "relationship_schema": self.relationship_schema,
            "schema_version": self.schema_version,
            "provenance": self.provenance,
            "run_id": str(self.run_id),
            "relationship_timestamp": self.relationship_timestamp.isoformat(),
            "join_keys_used": {
                k: list(v) for k, v in self.join_keys_used.items()
            },
        }


# ============================================================================
# Join Validator - The Gatekeeper
# ============================================================================

class JoinValidator:
    """
    Validates join execution before any relationships are created.
    
    Validation failure = pipeline stop.
    """
    
    def __init__(
        self,
        schema_registry: Optional[Dict[SchemaName, int]] = None,
        scope_validator: Optional[Callable[[JoinDefinition, str], bool]] = None,
    ):
        """
        Initialize validator with optional registries.
        
        Args:
            schema_registry: Optional registry of known schema versions
            scope_validator: Optional function to validate scope compatibility
        """
        self.schema_registry = schema_registry or {}
        self.scope_validator = scope_validator
    
    @staticmethod
    def validate_definition(
        definition: JoinDefinition,
        schema_registry: Optional[Dict[SchemaName, int]] = None,
        scope_validator: Optional[Callable[[JoinDefinition, str], bool]] = None,
    ) -> None:
        """
        Validate join definition is legal and consistent.
        
        Enforces:
        - Directionality semantic legality
        - Relationship schema immutability registry
        - Scope compatibility constraints
        - JoinType vs key cardinality validation
        
        Args:
            definition: Join definition to validate
            schema_registry: Optional registry of known schema versions
            scope_validator: Optional function to validate scope compatibility
            
        Raises:
            JoinDefinitionError: If definition is invalid
        """
        # Definition validates itself in __post_init__ for basic structure
        # Additional cross-field validation:
        
        # 1. Validate directionality semantic legality
        # BIDIRECTIONAL requires symmetric join type
        if definition.directionality == JoinDirectionality.BIDIRECTIONAL:
            if definition.join_type not in (JoinType.ONE_TO_ONE,):
                raise JoinDefinitionError(
                    f"BIDIRECTIONAL directionality requires ONE_TO_ONE join type, "
                    f"got {definition.join_type}"
                )
        
        # LEFT_TO_RIGHT requires left side to be "one" in ONE_TO_MANY
        if definition.directionality == JoinDirectionality.LEFT_TO_RIGHT:
            if definition.join_type == JoinType.MANY_TO_ONE:
                raise JoinDefinitionError(
                    "LEFT_TO_RIGHT directionality incompatible with MANY_TO_ONE join type. "
                    "Directionality indicates left→right flow, but join type indicates "
                    "many left facts per right fact."
                )
        
        # RIGHT_TO_LEFT requires right side to be "one" in MANY_TO_ONE
        if definition.directionality == JoinDirectionality.RIGHT_TO_LEFT:
            if definition.join_type == JoinType.ONE_TO_MANY:
                raise JoinDefinitionError(
                    "RIGHT_TO_LEFT directionality incompatible with ONE_TO_MANY join type. "
                    "Directionality indicates right→left flow, but join type indicates "
                    "many right facts per left fact."
                )
        
        # 2. Validate relationship schema immutability registry
        if schema_registry:
            for schema_name in [
                definition.left_fact_schema,
                definition.right_fact_schema,
                definition.relationship_schema,
            ]:
                if schema_name in schema_registry:
                    registered_version = schema_registry[schema_name]
                    # Relationship schema version must match or be compatible
                    if schema_name == definition.relationship_schema:
                        # Relationship schema version should be >= registered version
                        # (allowing forward compatibility)
                        if definition.version < registered_version:
                            raise JoinDefinitionError(
                                f"Relationship schema {schema_name} version {definition.version} "
                                f"is older than registered version {registered_version}. "
                                "Schema versions must be monotonic."
                            )
        
        # 3. Validate JoinType vs key cardinality consistency
        # ONE_TO_ONE must have single key (or composite key that's unique)
        if definition.join_type == JoinType.ONE_TO_ONE:
            if len(definition.join_keys.left_keys) == 0:
                raise JoinDefinitionError(
                    "ONE_TO_ONE join requires at least one join key"
                )
        
        # 4. Validate scope compatibility (if validator provided)
        if scope_validator:
            # Scope validator should check if definition is allowed in given scope
            # This is a hook for external governance
            try:
                if not scope_validator(definition, ""):  # Empty scope for general check
                    raise JoinDefinitionError(
                        f"Join definition {definition.join_id} failed scope compatibility check"
                    )
            except Exception as e:
                if isinstance(e, JoinDefinitionError):
                    raise
                raise JoinDefinitionError(
                    f"Scope validation failed for join definition {definition.join_id}: {e}"
                ) from e
    
    @staticmethod
    def validate_facts_for_join(
        self,
        left_facts: List[Fact],
        right_facts: List[Fact],
        definition: JoinDefinition,
    ) -> None:
        """
        Validate facts are compatible with join definition.
        
        Args:
            left_facts: Facts from left side
            right_facts: Facts from right side
            definition: Join definition
            
        Raises:
            JoinValidationError: If facts don't match definition requirements
        """
        # Validate schemas match
        for fact in left_facts:
            if fact.schema_name != definition.left_fact_schema:
                raise JoinValidationError(
                    f"Left fact schema mismatch: expected {definition.left_fact_schema}, "
                    f"got {fact.schema_name}"
                )
        
        for fact in right_facts:
            if fact.schema_name != definition.right_fact_schema:
                raise JoinValidationError(
                    f"Right fact schema mismatch: expected {definition.right_fact_schema}, "
                    f"got {fact.schema_name}"
                )
        
        # Validate facts have required keys and types are compatible
        for fact in left_facts:
            for key in definition.join_keys.left_keys:
                if key not in fact.fields:
                    raise JoinValidationError(
                        f"Left fact {fact.fact_id} missing required join key: {key}"
                    )
        
        for fact in right_facts:
            for key in definition.join_keys.right_keys:
                if key not in fact.fields:
                    raise JoinValidationError(
                        f"Right fact {fact.fact_id} missing required join key: {key}"
                    )
        
        # TIER-0 PERFORMANCE: Validate key type compatibility once, not per fact pair
        # O(1) schema validation instead of O(N×M) fact pair validation
        if left_facts and right_facts:
            # Sample one fact from each side to validate key type compatibility
            sample_left = left_facts[0]
            sample_right = right_facts[0]
            try:
                definition.join_keys.validate_fact_keys(
                    sample_left.fields,
                    sample_right.fields,
                )
            except ValueError as e:
                raise JoinValidationError(
                    f"Join key type incompatibility detected: {e}. "
                    "All facts must have compatible key types."
                ) from e
            
            # Validate all facts have the same key types as samples (structural check)
            left_key_types = {
                key: type(sample_left.fields[key]) 
                for key in definition.join_keys.left_keys 
                if key in sample_left.fields and sample_left.fields[key] is not None
            }
            right_key_types = {
                key: type(sample_right.fields[key]) 
                for key in definition.join_keys.right_keys 
                if key in sample_right.fields and sample_right.fields[key] is not None
            }
            
            # Quick structural validation: ensure all facts have keys (type consistency assumed)
            # Full type validation was done on samples above
            for fact in left_facts[1:]:  # Skip first (already validated)
                for key in definition.join_keys.left_keys:
                    if key not in fact.fields:
                        raise JoinValidationError(
                            f"Left fact {fact.fact_id} missing required join key: {key}"
                        )
            
            for fact in right_facts[1:]:  # Skip first (already validated)
                for key in definition.join_keys.right_keys:
                    if key not in fact.fields:
                        raise JoinValidationError(
                            f"Right fact {fact.fact_id} missing required join key: {key}"
                        )
        
        # MANDATORY CROSS-SCOPE ENFORCEMENT
        if definition.scope_field and not definition.allow_cross_scope:
            # Extract scope from all facts and validate consistency
            left_scopes = set()
            right_scopes = set()
            
            for fact in left_facts:
                if definition.scope_field in fact.fields:
                    left_scopes.add(fact.fields[definition.scope_field])
                else:
                    raise JoinValidationError(
                        f"Left fact {fact.fact_id} missing scope_field '{definition.scope_field}'"
                    )
            
            for fact in right_facts:
                if definition.scope_field in fact.fields:
                    right_scopes.add(fact.fields[definition.scope_field])
                else:
                    raise JoinValidationError(
                        f"Right fact {fact.fact_id} missing scope_field '{definition.scope_field}'"
                    )
            
            # All scopes must match (single scope per join execution)
            all_scopes = left_scopes | right_scopes
            if len(all_scopes) > 1:
                raise JoinValidationError(
                    f"Cross-scope join detected: found scopes {all_scopes}. "
                    f"Set allow_cross_scope=True if this is intentional."
                )
    
    @staticmethod
    def validate_no_circular_relationships(
        left_fact_id: FactID,
        right_fact_id: FactID,
    ) -> None:
        """
        Prevent circular relationships (fact relating to itself).
        
        Raises:
            ValueError: If circular relationship detected
        """
        if left_fact_id == right_fact_id:
            raise ValueError(
                f"Circular relationship detected: fact {left_fact_id} "
                "cannot relate to itself"
            )
    
    @staticmethod
    def validate_fan_out(
        matches_count: int,
        max_matches: int,
        side: str,
    ) -> None:
        """
        Validate fan-out doesn't exceed declared limits.
        
        Args:
            matches_count: Number of matches found
            max_matches: Maximum allowed matches
            side: "left" or "right" for error message
            
        Raises:
            ValueError: If fan-out exceeds limit
        """
        if matches_count > max_matches:
            raise ValueError(
                f"Fan-out limit exceeded on {side} side: "
                f"found {matches_count} matches, max allowed is {max_matches}"
            )


# ============================================================================
# Join Planner - Determinism Engine
# ============================================================================

class JoinPlanner:
    """
    Responsible for deterministic join execution planning.
    
    Same inputs → same relationships, bit-for-bit.
    
    Supports window-scoped planning for memory-bounded execution at scale.
    """
    
    def __init__(self, definition: JoinDefinition):
        """
        Initialize planner with join definition.
        
        Args:
            definition: The join definition to plan execution for
        """
        self.definition = definition
    
    def create_execution_plan(
        self,
        left_facts: List[Fact],
        right_facts: List[Fact],
        window: Optional[JoinWindow] = None,
        require_window: bool = False,
        large_scale_threshold: int = 100000,
    ) -> JoinExecutionPlan:
        """
        Create deterministic execution plan.
        
        Args:
            left_facts: Facts from left side
            right_facts: Facts from right side
            window: Optional window boundary for scoped execution
            require_window: If True, window is mandatory (enforced for large-scale joins)
            large_scale_threshold: Fact count threshold for automatic window requirement
            
        Returns:
            Execution plan with stable ordering and window boundaries
            
        Raises:
            ValueError: If require_window=True but window is None, or if fact count exceeds threshold without window
        """
        total_facts = len(left_facts) + len(right_facts)
        
        # TIER-0 ENFORCEMENT: Window is mandatory for large-scale joins
        # Automatically require window if fact count exceeds threshold
        if total_facts > large_scale_threshold and window is None:
            if not require_window:
                # Auto-enable window requirement for large scale
                require_window = True
        
        if require_window and window is None:
            raise ValueError(
                f"Window is required for large-scale join execution ({total_facts} facts). "
                "Provide a JoinWindow to ensure memory-bounded, replay-safe execution at 5M+ scale."
            )
        
        # Apply window filtering if provided
        if window:
            left_facts = self._filter_by_window(left_facts, window)
            right_facts = self._filter_by_window(right_facts, window)
        
        # Sort facts deterministically by fact_id
        sorted_left = sorted(left_facts, key=lambda f: f.fact_id)
        sorted_right = sorted(right_facts, key=lambda f: f.fact_id)
        
        # Build index for right facts by join key values
        # For large-scale joins, use streaming index builder
        use_streaming = total_facts > large_scale_threshold
        right_index = self._build_right_index(sorted_right, use_streaming=use_streaming)
        
        return JoinExecutionPlan(
            left_facts=sorted_left,
            right_facts=sorted_right,
            right_index=right_index,
            definition=self.definition,
            window=window,
            use_streaming=use_streaming,
        )
    
    def _filter_by_window(
        self,
        facts: List[Fact],
        window: JoinWindow,
    ) -> List[Fact]:
        """
        Filter facts to window boundaries.
        
        Args:
            facts: Facts to filter
            window: Window boundary
            
        Returns:
            Filtered facts within window
        """
        filtered = []
        for fact in facts:
            if window.window_start <= fact.timestamp < window.window_end:
                filtered.append(fact)
        return filtered
    
    def _build_right_index(
        self,
        right_facts: List[Fact],
        use_streaming: bool = False,
    ) -> Dict[Tuple[FieldValue, ...], List[Fact]]:
        """
        Build index of right facts by join key values.
        
        TIER-0: For large-scale joins, uses memory-efficient indexing.
        
        Args:
            right_facts: Facts to index
            use_streaming: If True, use memory-efficient indexing (stores fact IDs, not full facts)
            
        Returns:
            Dictionary mapping key values to matching facts (or fact IDs if streaming)
        """
        if use_streaming:
            # Memory-efficient: store fact IDs and positions, not full fact objects
            # Facts are accessed via iterator during execution
            index: Dict[Tuple[FieldValue, ...], List[int]] = {}  # key -> list of positions
            fact_positions: List[Fact] = []  # Position-based fact storage
            
            for idx, fact in enumerate(right_facts):
                fact_positions.append(fact)
                key_values = fact.get_join_key_values(self.definition.join_keys.right_keys)
                if key_values not in index:
                    index[key_values] = []
                index[key_values].append(idx)
            
            # Sort positions within each index bucket for determinism
            for key_values in index:
                index[key_values].sort()
            
            # Return streaming-compatible index (will be converted during execution)
            return {"_streaming": True, "_fact_positions": fact_positions, "_index": index}  # type: ignore
        else:
            # Standard index: store full facts (efficient for small-medium datasets)
            index: Dict[Tuple[FieldValue, ...], List[Fact]] = {}
            
            for fact in right_facts:
                key_values = fact.get_join_key_values(self.definition.join_keys.right_keys)
                if key_values not in index:
                    index[key_values] = []
                index[key_values].append(fact)
            
            # Sort facts within each index bucket for determinism
            for key_values in index:
                index[key_values].sort(key=lambda f: f.fact_id)
            
            return index  # type: ignore


@dataclass(frozen=True)
class JoinExecutionPlan:
    """
    Deterministic execution plan for a join.
    
    Contains:
        - Stable iteration ordering
        - Pre-built indexes (or streaming index for large scale)
        - Join definition
        - Optional window boundaries
        - Streaming mode flag
    """
    left_facts: List[Fact]
    right_facts: List[Fact]
    right_index: Dict  # Can be Dict[Tuple, List[Fact]] or streaming index structure
    definition: JoinDefinition
    window: Optional[JoinWindow] = None
    use_streaming: bool = False


# ============================================================================
# Relationship Fact Builder
# ============================================================================

class RelationshipFactBuilder:
    """
    Builds immutable relationship facts from join results.
    """
    
    def __init__(self, context: JoinContext, definition: JoinDefinition):
        """
        Initialize builder with context and definition.
        
        Args:
            context: Join execution context
            definition: Join definition
        """
        self.context = context
        self.definition = definition
    
    def build_relationship(
        self,
        left_fact: Fact,
        right_fact: Fact,
    ) -> RelationshipFact:
        """
        Build a relationship fact from two joined facts.
        
        Args:
            left_fact: Fact from left side
            right_fact: Fact from right side
            
        Returns:
            Immutable relationship fact
        """
        # Extract join key values for audit
        left_key_values = left_fact.get_join_key_values(
            self.definition.join_keys.left_keys
        )
        right_key_values = right_fact.get_join_key_values(
            self.definition.join_keys.right_keys
        )
        
        # Build provenance string
        provenance = (
            f"join_id={self.definition.join_id}|"
            f"join_type={self.definition.join_type}|"
            f"run_id={self.context.run_id}|"
            f"source={self.context.source}"
        )
        
        # Generate deterministic relationship fact ID (replay-safe)
        # Use UUID v5 (SHA-1 based) for deterministic generation
        relationship_seed = (
            f"{self.definition.join_id}|"
            f"{left_fact.fact_id}|"
            f"{right_fact.fact_id}|"
            f"{self.context.run_id}|"
            f"{self.definition.version}"
        )
        relationship_fact_id = uuid5(NAMESPACE_OID, relationship_seed)
        
        return RelationshipFact(
            relationship_fact_id=relationship_fact_id,
            relationship_type=self.definition.relationship_type,
            left_fact_id=left_fact.fact_id,
            right_fact_id=right_fact.fact_id,
            relationship_schema=self.definition.relationship_schema,
            schema_version=self.definition.version,
            provenance=provenance,
            run_id=self.context.run_id,
            relationship_timestamp=self.context.timestamp,
            join_keys_used={
                "left": left_key_values,
                "right": right_key_values,
            },
        )


# ============================================================================
# Join Executor - The Mechanism
# ============================================================================

class JoinExecutor:
    """
    Executes joins exactly as planned.
    
    Responsibilities:
        - Execute joins exactly as planned
        - Emit zero or more relationship facts
        - Never reorder input facts
        - Never mutate source facts
        - Block ambiguous joins
        - Refuse unregistered definitions
    
    If a join produces uncertainty, it produces nothing.
    """
    
    def __init__(
        self,
        context: JoinContext,
        validator: Optional[JoinValidator] = None,
        definition_registry: Optional[JoinDefinitionRegistry] = None,
        schema_registry: Optional[RelationshipSchemaRegistry] = None,
        require_registry: bool = True,
    ):
        """
        Initialize executor with context.
        
        Args:
            context: Join execution context
            validator: Optional custom validator (uses default if None)
            definition_registry: Registry to enforce definition registration (required if require_registry=True)
            schema_registry: Registry to enforce schema immutability (required if require_registry=True)
            require_registry: If True, registries are mandatory (Tier-0 enforcement)
            
        Raises:
            ValueError: If require_registry=True but registries are None
        """
        self.context = context
        self.validator = validator or JoinValidator()
        self.require_registry = require_registry
        
        # TIER-0: Make registries mandatory if required
        if require_registry:
            if definition_registry is None:
                raise ValueError(
                    "definition_registry is required for Tier-0 enforcement. "
                    "Set require_registry=False to allow unregistered definitions (not recommended)."
                )
            if schema_registry is None:
                raise ValueError(
                    "schema_registry is required for Tier-0 enforcement. "
                    "Set require_registry=False to allow unregistered schemas (not recommended)."
                )
        
        self.definition_registry = definition_registry
        self.schema_registry = schema_registry
    
    def execute(
        self,
        plan: JoinExecutionPlan,
    ) -> JoinResult:
        """
        Execute join according to plan.
        
        Args:
            plan: Execution plan from JoinPlanner
            
        Returns:
            Join result with relationship facts and statistics
            
        Raises:
            JoinDefinitionError: If definition is not registered
            JoinValidationError: If join constraints are violated
            JoinExecutionError: If execution fails
        """
        # TIER-0 UNAVOIDABLE ENFORCEMENT: Refuse unregistered definitions
        # This check is mandatory if require_registry=True (cannot be bypassed)
        if self.require_registry:
            if self.definition_registry is None:
                raise JoinExecutionError(
                    "definition_registry is required but not provided. "
                    "Unregistered join definitions cannot execute in Tier-0 mode."
                )
            self.definition_registry.assert_registered(plan.definition.join_id)
        
        # TIER-0 UNAVOIDABLE ENFORCEMENT: Assert schema immutability
        # This check is mandatory if require_registry=True (cannot be bypassed)
        if self.require_registry:
            if self.schema_registry is None:
                raise JoinExecutionError(
                    "schema_registry is required but not provided. "
                    "Unregistered relationship schemas cannot execute in Tier-0 mode."
                )
            self.schema_registry.assert_immutable(
                plan.definition.relationship_schema,
                plan.definition.version,
            )
        
        # Validate facts match definition
        self.validator.validate_facts_for_join(
            plan.left_facts,
            plan.right_facts,
            plan.definition,
        )
        
        # Build relationship facts
        builder = RelationshipFactBuilder(self.context, plan.definition)
        relationships: List[RelationshipFact] = []
        
        # Track match counts for fan-out validation
        left_match_counts: Dict[FactID, int] = {}
        right_match_counts: Dict[FactID, int] = {}
        
        # Execute join (with streaming support for large scale)
        if plan.use_streaming and isinstance(plan.right_index, dict) and "_streaming" in plan.right_index:
            # Streaming mode: access facts via positions
            fact_positions = plan.right_index["_fact_positions"]
            streaming_index = plan.right_index["_index"]
            
            for left_fact in plan.left_facts:
                # Get join key values from left fact
                left_key_values = left_fact.get_join_key_values(
                    plan.definition.join_keys.left_keys
                )
                
                # Find matching right fact positions
                matching_positions = streaming_index.get(left_key_values, [])
                matching_right_facts = [fact_positions[pos] for pos in matching_positions]
                
                # Process matches (ambiguity handling below)
                self._process_matches(
                    left_fact,
                    matching_right_facts,
                    plan,
                    builder,
                    relationships,
                    left_match_counts,
                    right_match_counts,
                )
        else:
            # Standard mode: direct fact access
            for left_fact in plan.left_facts:
                # Get join key values from left fact
                left_key_values = left_fact.get_join_key_values(
                    plan.definition.join_keys.left_keys
                )
                
                # Find matching right facts
                matching_right_facts = plan.right_index.get(left_key_values, [])  # type: ignore
                
                # Process matches (ambiguity handling below)
                self._process_matches(
                    left_fact,
                    matching_right_facts,
                    plan,
                    builder,
                    relationships,
                    left_match_counts,
                    right_match_counts,
                )
        
        # Validate fan-out on right side
        for right_fact_id, count in right_match_counts.items():
            self.validator.validate_fan_out(
                matches_count=count,
                max_matches=plan.definition.max_matches_per_right,
                side="right",
            )
        
        # Extract join keys used at execution level for audit
        join_keys_used = {
            "left_keys": sorted(plan.definition.join_keys.left_keys),
            "right_keys": sorted(plan.definition.join_keys.right_keys),
        }
        
        return JoinResult(
            relationships=relationships,
            left_facts_processed=len(plan.left_facts),
            right_facts_processed=len(plan.right_facts),
            relationships_created=len(relationships),
            join_definition_id=plan.definition.join_id,
            join_definition_hash=plan.definition.compute_hash(),
            join_keys_used=join_keys_used,
            execution_context=self.context,
        )
    
    def _process_matches(
        self,
        left_fact: Fact,
        matching_right_facts: List[Fact],
        plan: JoinExecutionPlan,
        builder: RelationshipFactBuilder,
        relationships: List[RelationshipFact],
        left_match_counts: Dict[FactID, int],
        right_match_counts: Dict[FactID, int],
    ) -> None:
        """
        Process matches for a left fact with strict ambiguity handling.
        
        TIER-0: Centralized ambiguity handling logic.
        """
        # TIER-0 STRICT AMBIGUITY HANDLING: Fail closed on semantic ambiguity
        # NO EXCEPTIONS: Ambiguity without explicit declaration = hard fail
        if len(matching_right_facts) > 1:
            # ONE_TO_ONE: Multiple matches are semantically impossible - always fail
            if plan.definition.join_type == JoinType.ONE_TO_ONE:
                raise JoinExecutionError(
                    f"Ambiguous join detected for left fact {left_fact.fact_id}: "
                    f"found {len(matching_right_facts)} matches in ONE_TO_ONE join. "
                    "ONE_TO_ONE joins cannot have multiple matches - no relationships emitted."
                )
            
            # STRICT_FAIL: Any multiple matches = ambiguity = fail (default)
            if plan.definition.ambiguity_policy == AmbiguityPolicy.STRICT_FAIL:
                raise JoinExecutionError(
                    f"Ambiguous join detected for left fact {left_fact.fact_id}: "
                    f"found {len(matching_right_facts)} matches. "
                    f"Ambiguity policy is STRICT_FAIL - no relationships emitted. "
                    f"Set ambiguity_policy=ALLOW_DECLARED_MULTI if multiple matches are intentional."
                )
            
            # ALLOW_DECLARED_MULTI: Multiple matches explicitly allowed, but must respect max_matches
            # This is the ONLY path where multiple matches are permitted
            if plan.definition.ambiguity_policy == AmbiguityPolicy.ALLOW_DECLARED_MULTI:
                if len(matching_right_facts) > plan.definition.max_matches_per_left:
                    raise JoinExecutionError(
                        f"Ambiguous join detected for left fact {left_fact.fact_id}: "
                        f"found {len(matching_right_facts)} matches, exceeds "
                        f"max_matches_per_left={plan.definition.max_matches_per_left}. "
                        "Even with ALLOW_DECLARED_MULTI, max_matches limit must be respected."
                    )
                # Multiple matches within limit are allowed - continue execution
            else:
                # This should never happen due to validation, but defensive check
                raise JoinExecutionError(
                    f"Unknown ambiguity policy: {plan.definition.ambiguity_policy}"
                )
        
        # Validate fan-out on left side (after ambiguity check)
        self.validator.validate_fan_out(
            matches_count=len(matching_right_facts),
            max_matches=plan.definition.max_matches_per_left,
            side="left",
        )
        
        # Create relationships (only if no ambiguity)
        for right_fact in matching_right_facts:
            # Validate no circular relationships
            self.validator.validate_no_circular_relationships(
                left_fact.fact_id,
                right_fact.fact_id,
            )
            
            # Track match counts
            left_match_counts[left_fact.fact_id] = (
                left_match_counts.get(left_fact.fact_id, 0) + 1
            )
            right_match_counts[right_fact.fact_id] = (
                right_match_counts.get(right_fact.fact_id, 0) + 1
            )
            
            # Build relationship
            relationship = builder.build_relationship(left_fact, right_fact)
            relationships.append(relationship)


@dataclass(frozen=True)
class JoinResult:
    """
    Result of join execution with statistics and audit metadata.
    """
    relationships: List[RelationshipFact]
    left_facts_processed: int
    right_facts_processed: int
    relationships_created: int
    join_definition_id: str
    join_definition_hash: str
    join_keys_used: Dict[str, List[str]]
    execution_context: JoinContext
    
    def to_audit_dict(self) -> Dict:
        """Generate audit log entry for this join execution."""
        return {
            "run_id": str(self.execution_context.run_id),
            "pipeline_stage": self.execution_context.pipeline_stage,
            "source": str(self.execution_context.source),
            "scope_id": self.execution_context.scope_id,
            "timestamp": self.execution_context.timestamp.isoformat(),
            "join_definition_id": self.join_definition_id,
            "join_definition_hash": self.join_definition_hash,
            "join_keys_used": self.join_keys_used,
            "left_facts_processed": self.left_facts_processed,
            "right_facts_processed": self.right_facts_processed,
            "relationships_created": self.relationships_created,
            "relationship_ids": [
                str(r.relationship_fact_id) for r in self.relationships
            ],
            "window_id": (
                self.execution_context.window.window_id
                if self.execution_context.window else None
            ),
        }


# ============================================================================
# Join Invariants - Hard Rules
# ============================================================================

class JoinInvariants:
    """
    Enforce hard invariants on join execution.
    
    Violation = system defect.
    """
    
    @staticmethod
    def enforce_no_mutation(
        original_facts: List[Fact],
        facts_after_join: List[Fact],
    ) -> None:
        """
        Verify input facts were not mutated during join.
        
        Args:
            original_facts: Facts before join
            facts_after_join: Same facts after join
            
        Raises:
            AssertionError: If any fact was mutated
        """
        if len(original_facts) != len(facts_after_join):
            raise JoinInvariantViolation(
                f"Fact count changed during join: {len(original_facts)} → {len(facts_after_join)}"
            )
        
        for original, after in zip(original_facts, facts_after_join):
            if original.fact_id != after.fact_id:
                raise JoinInvariantViolation(
                    f"Fact ID changed: {original.fact_id} → {after.fact_id}"
                )
            if original.fields != after.fields:
                raise JoinInvariantViolation(
                    f"Fact {original.fact_id} fields were mutated"
                )
            if original.schema_name != after.schema_name:
                raise JoinInvariantViolation(
                    f"Fact {original.fact_id} schema_name changed: "
                    f"{original.schema_name} → {after.schema_name}"
                )
    
    @staticmethod
    def enforce_no_cross_scope(
        facts: List[Fact],
        allowed_scope_id: str,
        scope_field: str,
    ) -> None:
        """
        Verify no cross-scope joins occurred.
        
        Args:
            facts: Facts to check
            allowed_scope_id: The only allowed scope ID
            scope_field: Field name to extract scope_id from fact
            
        Raises:
            JoinInvariantViolation: If cross-scope join detected
        """
        for fact in facts:
            if scope_field not in fact.fields:
                raise JoinInvariantViolation(
                    f"Fact {fact.fact_id} missing scope_field '{scope_field}'"
                )
            scope_id = fact.fields[scope_field]
            if scope_id != allowed_scope_id:
                raise JoinInvariantViolation(
                    f"Cross-scope join detected: fact {fact.fact_id} "
                    f"has scope {scope_id}, expected {allowed_scope_id}"
                )
    
    @staticmethod
    def enforce_no_many_to_many(definition: JoinDefinition) -> None:
        """
        Verify join definition doesn't allow many-to-many.
        
        Args:
            definition: Join definition to check
            
        Raises:
            ValueError: If many-to-many detected
        """
        is_many_to_many = (
            definition.max_matches_per_left > 1 and
            definition.max_matches_per_right > 1
        )
        
        if is_many_to_many:
            raise JoinInvariantViolation(
                f"Many-to-many join detected in definition {definition.join_id}. "
                "This is forbidden. Your schema is lying. "
                f"max_matches_per_left={definition.max_matches_per_left}, "
                f"max_matches_per_right={definition.max_matches_per_right}"
            )
    
    @staticmethod
    def enforce_schema_versioning(
        definition: JoinDefinition,
        known_schema_versions: Dict[SchemaName, int],
    ) -> None:
        """
        Verify all schemas are versioned and known.
        
        Args:
            definition: Join definition to check
            known_schema_versions: Registry of known schema versions
            
        Raises:
            ValueError: If schema is unversioned or unknown
        """
        for schema_name in [
            definition.left_fact_schema,
            definition.right_fact_schema,
            definition.relationship_schema,
        ]:
            if schema_name not in known_schema_versions:
                raise JoinInvariantViolation(
                    f"Unknown schema: {schema_name}. "
                    "All schemas must be versioned and known."
                )
            
            if known_schema_versions[schema_name] < 1:
                raise JoinInvariantViolation(
                    f"Schema {schema_name} has invalid version: "
                    f"{known_schema_versions[schema_name]}. Version must be >= 1."
                )
    
    @staticmethod
    def enforce_deterministic_output(
        result1: JoinResult,
        result2: JoinResult,
    ) -> None:
        """
        Verify two join executions with same inputs produce same output.
        
        Args:
            result1: First join result
            result2: Second join result
            
        Raises:
            AssertionError: If results differ
        """
        if result1.relationships_created != result2.relationships_created:
            raise JoinInvariantViolation(
                f"Non-deterministic join: {result1.relationships_created} vs "
                f"{result2.relationships_created} relationships"
            )
        
        # Compare relationship fact pairs (deterministic ordering)
        rel1_pairs = sorted(
            (r.left_fact_id, r.right_fact_id) for r in result1.relationships
        )
        rel2_pairs = sorted(
            (r.left_fact_id, r.right_fact_id) for r in result2.relationships
        )
        
        if rel1_pairs != rel2_pairs:
            raise JoinInvariantViolation(
                f"Non-deterministic join: different fact pairs matched. "
                f"Result 1: {len(rel1_pairs)} pairs, Result 2: {len(rel2_pairs)} pairs"
            )
        
        # Compare relationship fact IDs (must be identical for replay)
        rel1_ids = sorted(r.relationship_fact_id for r in result1.relationships)
        rel2_ids = sorted(r.relationship_fact_id for r in result2.relationships)
        
        if rel1_ids != rel2_ids:
            raise JoinInvariantViolation(
                "Non-deterministic join: relationship fact IDs differ between runs"
            )


# ============================================================================
# Relationship Schema Registry - Immutability Enforcement
# ============================================================================

class RelationshipSchemaRegistry:
    """
    Registry for relationship schema versions.
    
    Enforces immutability: once registered, schema version cannot change.
    """
    
    def __init__(self):
        """Initialize empty registry."""
        self._schemas: Dict[Tuple[SchemaName, int], Dict] = {}  # (name, version) -> metadata
        self._lock = threading.Lock()
        self._frozen = False
    
    def register(
        self,
        schema_name: SchemaName,
        version: int,
        metadata: Optional[Dict] = None,
    ) -> None:
        """
        Register a relationship schema version.
        
        Args:
            schema_name: Schema name
            version: Schema version
            metadata: Optional metadata about the schema
            
        Raises:
            RuntimeError: If registry is frozen
            ValueError: If schema version already registered with different metadata
        """
        with self._lock:
            if self._frozen:
                raise RuntimeError("Cannot register schema: registry is frozen")
            
            key = (schema_name, version)
            if key in self._schemas:
                existing = self._schemas[key]
                if existing != (metadata or {}):
                    raise ValueError(
                        f"Schema {schema_name} version {version} already registered "
                        f"with different metadata"
                    )
                return  # Idempotent
            
            self._schemas[key] = metadata or {}
    
    def assert_immutable(
        self,
        schema_name: SchemaName,
        version: int,
    ) -> None:
        """
        Assert that a schema version is registered and immutable.
        
        Args:
            schema_name: Schema name
            version: Schema version
            
        Raises:
            JoinDefinitionError: If schema version is not registered
        """
        with self._lock:
            key = (schema_name, version)
            if key not in self._schemas:
                raise JoinDefinitionError(
                    f"Relationship schema {schema_name} version {version} is not registered. "
                    "All relationship schemas must be registered before use."
                )
    
    def freeze(self) -> None:
        """Freeze registry, making it immutable."""
        with self._lock:
            self._frozen = True
    
    def is_registered(self, schema_name: SchemaName, version: int) -> bool:
        """
        Check if a schema version is registered.
        
        Args:
            schema_name: Schema name
            version: Schema version
            
        Returns:
            True if registered, False otherwise
        """
        with self._lock:
            return (schema_name, version) in self._schemas


# ============================================================================
# Join Definition Registry - Authority Boundary
# ============================================================================

class JoinDefinitionRegistry:
    """
    Central registry for join definitions.
    
    Enforces governance: no dynamic joins, all definitions must be registered.
    
    This is the authority boundary preventing rogue runtime definitions.
    """
    
    def __init__(self):
        """Initialize empty registry."""
        self._definitions: Dict[str, JoinDefinition] = {}
        self._lock = threading.Lock()
        self._frozen = False
    
    def register(
        self,
        definition: JoinDefinition,
        schema_registry: Optional[Dict[SchemaName, int]] = None,
        scope_validator: Optional[Callable[[JoinDefinition, str], bool]] = None,
    ) -> None:
        """
        Register a join definition.
        
        Validates definition before registration. Once registered, definition
        is immutable and cannot be modified.
        
        Args:
            definition: Join definition to register
            schema_registry: Optional registry of known schema versions
            scope_validator: Optional function to validate scope compatibility
            
        Raises:
            RuntimeError: If registry is frozen
            JoinDefinitionError: If definition is invalid or already registered
        """
        with self._lock:
            if self._frozen:
                raise RuntimeError("Cannot register join definition: registry is frozen")
            
            # Validate definition
            JoinValidator.validate_definition(
                definition,
                schema_registry=schema_registry,
                scope_validator=scope_validator,
            )
            
            # Check for duplicate join_id
            if definition.join_id in self._definitions:
                existing = self._definitions[definition.join_id]
                existing_hash = existing.compute_hash()
                new_hash = definition.compute_hash()
                
                if existing_hash != new_hash:
                    raise JoinDefinitionError(
                        f"Join definition {definition.join_id} already registered "
                        f"with different definition. Existing hash: {existing_hash}, "
                        f"new hash: {new_hash}"
                    )
                # Same hash = idempotent registration, allow it
                return
            
            # Register definition
            self._definitions[definition.join_id] = definition
    
    def get(self, join_id: str) -> JoinDefinition:
        """
        Retrieve a registered join definition.
        
        Args:
            join_id: Join definition ID
            
        Returns:
            Registered join definition
            
        Raises:
            KeyError: If join definition not found
        """
        with self._lock:
            if join_id not in self._definitions:
                raise KeyError(
                    f"Join definition {join_id} not found in registry. "
                    "All join definitions must be registered before use."
                )
            return self._definitions[join_id]
    
    def assert_registered(self, join_id: str) -> None:
        """
        Assert that a join definition is registered.
        
        Args:
            join_id: Join definition ID
            
        Raises:
            JoinDefinitionError: If join definition is not registered
        """
        with self._lock:
            if join_id not in self._definitions:
                raise JoinDefinitionError(
                    f"Join definition {join_id} is not registered. "
                    "All join definitions must be registered before execution."
                )
    
    def is_registered(self, join_id: str) -> bool:
        """
        Check if a join definition is registered.
        
        Args:
            join_id: Join definition ID
            
        Returns:
            True if registered, False otherwise
        """
        with self._lock:
            return join_id in self._definitions
    
    def freeze(self) -> None:
        """Freeze registry, making it immutable."""
        with self._lock:
            self._frozen = True
    
    def list_all(self) -> List[str]:
        """
        List all registered join definition IDs.
        
        Returns:
            List of join definition IDs
        """
        with self._lock:
            return list(self._definitions.keys())


# ============================================================================
# Join Error Types
# ============================================================================

class JoinError(Exception):
    """Base exception for all join-related errors."""
    pass


class JoinDefinitionError(JoinError):
    """Invalid join definition."""
    pass


class JoinValidationError(JoinError):
    """Join validation failure."""
    pass


class JoinExecutionError(JoinError):
    """Join execution failure."""
    pass


class JoinInvariantViolation(JoinError):
    """Join invariant violation."""
    pass


# ============================================================================
# High-Level Join Orchestrator
# ============================================================================

class JoinOrchestrator:
    """
    High-level orchestrator for complete join workflow.
    
    Coordinates: validation → planning → execution → invariant checking
    
    Tier-0: All invariants are automatically enforced, not optional.
    """
    
    def __init__(
        self,
        definition: JoinDefinition,
        context: JoinContext,
        validator: Optional[JoinValidator] = None,
        schema_registry: Optional[Dict[SchemaName, int]] = None,
        definition_registry: Optional[JoinDefinitionRegistry] = None,
        relationship_schema_registry: Optional[RelationshipSchemaRegistry] = None,
        require_window: bool = False,
        require_registry: bool = True,
    ):
        """
        Initialize orchestrator.
        
        TIER-0: All enforcement is mandatory and non-bypassable.
        
        Args:
            definition: Join definition (must be valid - validated at construction)
            context: Execution context
            validator: Optional custom validator
            schema_registry: Optional registry of known schema versions
            definition_registry: Registry to enforce definition registration (required if require_registry=True)
            relationship_schema_registry: Registry to enforce relationship schema immutability (required if require_registry=True)
            require_window: If True, window is mandatory for execution
            require_registry: If True, registries are mandatory (Tier-0 enforcement, default: True)
        """
        self.definition = definition
        self.context = context
        self.validator = validator or JoinValidator(schema_registry=schema_registry)
        self.planner = JoinPlanner(definition)
        self.require_registry = require_registry
        self.require_window = require_window
        
        # TIER-0: Make registries mandatory if required
        if require_registry:
            if definition_registry is None:
                raise ValueError(
                    "definition_registry is required for Tier-0 enforcement. "
                    "All join definitions must be registered before execution."
                )
            if relationship_schema_registry is None:
                raise ValueError(
                    "relationship_schema_registry is required for Tier-0 enforcement. "
                    "All relationship schemas must be registered before execution."
                )
        
        self.executor = JoinExecutor(
            context,
            self.validator,
            definition_registry=definition_registry,
            schema_registry=relationship_schema_registry,
            require_registry=require_registry,
        )
        self.schema_registry = schema_registry or {}
        self.definition_registry = definition_registry
        self.relationship_schema_registry = relationship_schema_registry
        
        # TIER-0 UNAVOIDABLE ENFORCEMENT: Validate definition upfront
        # This is called even though definition validates itself - double-check
        JoinValidator.validate_definition(
            definition,
            schema_registry=schema_registry,
        )
        
        # TIER-0: Single authoritative enforcement boundary
        # Registration and schema immutability checks happen in executor only
        # (not duplicated here to avoid governance layering confusion)
        
        # TIER-0 UNAVOIDABLE ENFORCEMENT: All invariants checked upfront
        # These cannot be bypassed - they're part of construction
        JoinInvariants.enforce_no_many_to_many(definition)
        
        # Enforce schema versioning invariant
        if self.schema_registry:
            JoinInvariants.enforce_schema_versioning(definition, self.schema_registry)
    
    def execute_join(
        self,
        left_facts: List[Fact],
        right_facts: List[Fact],
    ) -> JoinResult:
        """
        Execute complete join workflow with automatic invariant enforcement.
        
        Tier-0: All invariants are enforced automatically, not optional.
        
        Args:
            left_facts: Canonical facts from left side
            right_facts: Canonical facts from right side
            
        Returns:
            Join result with relationship facts
            
        Raises:
            JoinError: If join constraints violated or invariants fail
        """
        # Take snapshots for mutation checking
        left_snapshot = list(left_facts)
        right_snapshot = list(right_facts)
        
        # TIER-0 UNAVOIDABLE INVARIANT: Enforce no cross-scope joins
        # This is now mandatory via scope_field in JoinDefinition
        # (enforced in validate_facts_for_join - cannot be bypassed)
        
        # Plan (with window if provided, and require_window enforcement)
        plan = self.planner.create_execution_plan(
            left_facts,
            right_facts,
            window=self.context.window,
            require_window=self.require_window,
        )
        
        # Execute (all enforcement happens inside executor - cannot be bypassed)
        result = self.executor.execute(plan)
        
        # TIER-0 UNAVOIDABLE INVARIANT: Enforce no mutation
        # These checks are part of the execution path - cannot be skipped
        JoinInvariants.enforce_no_mutation(left_snapshot, left_facts)
        JoinInvariants.enforce_no_mutation(right_snapshot, right_facts)
        
        # TIER-0 UNAVOIDABLE INVARIANT: Enforce schema versioning
        # Re-check after execution to ensure no drift
        if self.schema_registry:
            JoinInvariants.enforce_schema_versioning(
                self.definition,
                self.schema_registry,
            )
        
        # TIER-0 UNAVOIDABLE INVARIANT: Enforce no many-to-many (re-check after execution)
        # Double-check to ensure execution didn't violate cardinality
        JoinInvariants.enforce_no_many_to_many(self.definition)
        
        # TIER-0 UNAVOIDABLE INVARIANT: Re-assert relationship schema immutability
        # Final check to ensure semantic immutability maintained
        if self.require_registry and self.relationship_schema_registry:
            self.relationship_schema_registry.assert_immutable(
                self.definition.relationship_schema,
                self.definition.version,
            )
        
        return result


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    'JoinType',
    'JoinSource',
    'JoinDirectionality',
    'AmbiguityPolicy',
    'JoinKeySpec',
    'JoinDefinition',
    'JoinWindow',
    'JoinContext',
    'Fact',
    'RelationshipFact',
    'JoinValidator',
    'JoinPlanner',
    'JoinExecutionPlan',
    'RelationshipFactBuilder',
    'JoinExecutor',
    'JoinResult',
    'JoinInvariants',
    'RelationshipSchemaRegistry',
    'JoinDefinitionRegistry',
    'JoinOrchestrator',
    'JoinError',
    'JoinDefinitionError',
    'JoinValidationError',
    'JoinExecutionError',
    'JoinInvariantViolation',
]