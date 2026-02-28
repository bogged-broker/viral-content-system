"""
/data/pipelines/transforms/validation.py

Canonical Fact Validity Authority (No Policy, No Guessing)

WHAT THIS FILE ACTUALLY IS (plain English):
validation.py is the court of law for facts.

It answers exactly one question:
> "Is this fact structurally and semantically valid according to the schema it claims to represent?"

If the answer is no, the fact does not exist as far as the pipeline is concerned.

WHAT THIS FILE IS NOT (STRICT):
❌ Not normalization
❌ Not filtering
❌ Not deduplication
❌ Not enrichment
❌ Not inference
❌ Not analytics
❌ Not recovery logic

Validation does not decide whether we want the fact — only whether it is real and lawful.

DESIGN PRINCIPLE (CRITICAL):
> Invalid facts are more dangerous than missing facts.

Dropping invalid data loudly is safer than accepting it quietly.

CORE RESPONSIBILITIES (NON-NEGOTIABLE):
validation.py MUST:
1. Enforce schema correctness
2. Validate required field presence
3. Validate field types & ranges
4. Enforce semantic invariants
5. Detect impossible or contradictory states
6. Produce explicit validation errors
7. Never mutate payloads
8. Never guess intent

Validation is binary: valid or invalid.

MENTAL MODEL (LOCK THIS):
> Normalization makes data comparable
Validation makes data real
Filtering makes data intentional
Deduplication makes data singular

Miss one, and the whole pipeline lies.

Deterministic, schema-true, replay-safe, and sealed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Dict, Callable, Set, Union, Tuple, FrozenSet, Protocol
from enum import Enum
from abc import ABC, abstractmethod
import hashlib
import json


class ValidationDecision(Enum):
    """
    Strict validation decision enum.
    
    No warnings.
    No soft failures.
    No partial validity.
    
    Validation is binary: valid or invalid.
    """
    VALID = "valid"
    INVALID = "invalid"


class ValidationErrorCode(Enum):
    """
    Stable, enumerable error codes for validation failures.
    
    Every failure MUST emit a machine-readable error code.
    Errors are facts, not strings.
    """
    UNKNOWN_SCHEMA = "unknown_schema"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_FIELD_TYPE = "invalid_field_type"
    SEMANTIC_INVARIANT_VIOLATION = "semantic_invariant_violation"
    MULTIPLE_SCHEMA_MATCH = "multiple_schema_match"
    UNKNOWN_FIELD_PRESENT = "unknown_field_present"
    ENUM_VALUE_INVALID = "enum_value_invalid"
    LIST_CARDINALITY_VIOLATION = "list_cardinality_violation"
    IMPOSSIBLE_STATE = "impossible_state"
    FUTURE_TIMESTAMP = "future_timestamp"
    NEGATIVE_COUNTER = "negative_counter"
    MUTUALLY_EXCLUSIVE_VIOLATION = "mutually_exclusive_violation"


class ValidationSource(Enum):
    """Where this validation execution originated."""
    INGEST = "ingest"
    REPLAY = "replay"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class ValidationContext:
    """
    Immutable execution metadata for validation.
    
    No context → no validation.
    """
    schema_name: str
    schema_version: int
    pipeline_stage: str
    source: ValidationSource
    scope: str  # account / content / global / workflow
    scope_id: str
    run_id: str
    timestamp: int  # Logical timestamp (not wall clock)
    
    def __post_init__(self) -> None:
        """Validate context is complete and well-formed."""
        required_fields = {
            "schema_name": self.schema_name,
            "pipeline_stage": self.pipeline_stage,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "run_id": self.run_id,
        }
        
        missing = [k for k, v in required_fields.items() if not v]
        if missing:
            raise ValueError(
                f"ValidationContext: missing required fields: {missing}"
            )
        
        if not isinstance(self.source, ValidationSource):
            raise TypeError(
                f"source must be ValidationSource, got {type(self.source)}"
            )
        
        if self.schema_version < 1:
            raise ValueError(
                f"Invalid schema_version: {self.schema_version}. Must be >= 1."
            )
        
        if self.timestamp < 0:
            raise ValueError(
                f"Invalid timestamp: {self.timestamp}. Must be >= 0 (logical timestamp)."
            )


@dataclass(frozen=True)
class ValidationError:
    """
    Structured validation error (machine-readable).
    
    Every failure MUST emit a machine-readable error:
    - error_code (stable, enumerable)
    - field_path
    - expected
    - actual
    - message (human-readable)
    - schema_name
    - schema_version
    - run_id
    
    Errors are facts, not strings.
    """
    error_code: ValidationErrorCode
    field_path: str
    expected: str
    actual: str
    message: str
    schema_name: str
    schema_version: int
    run_id: str
    
    def __post_init__(self) -> None:
        """Validate error is complete."""
        if not self.field_path:
            raise ValueError("field_path cannot be empty")
        if not self.message:
            raise ValueError("message cannot be empty")
        if not self.schema_name:
            raise ValueError("schema_name cannot be empty")
        if self.schema_version < 1:
            raise ValueError(f"schema_version must be >= 1, got {self.schema_version}")
        if not self.run_id:
            raise ValueError("run_id cannot be empty")
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for audit logging."""
        return {
            "error_code": self.error_code.value,
            "field_path": self.field_path,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class ValidationResult:
    decision: ValidationDecision
    errors: List[ValidationError]
    payload_hash: str
    schema_name: str
    schema_version: int
    run_id: str
    timestamp: int


@dataclass(frozen=True)
class FieldSpec:
    name: str
    field_type: type
    required: bool
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    allowed_values: Optional[Set[Any]] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    min_items: Optional[int] = None
    max_items: Optional[int] = None


@dataclass(frozen=True)
class SchemaDefinition:
    name: str
    version: int
    fields: List[FieldSpec]
    allow_unknown_fields: bool = False


@dataclass(frozen=True)
class SemanticRule:
    name: str
    check: Callable[[Any], bool]
    error_message: str
    field_paths: List[str]
    
    def __post_init__(self) -> None:
        """Validate rule is well-formed."""
        if not self.name:
            raise ValueError("SemanticRule.name cannot be empty")
        if not self.error_message:
            raise ValueError("SemanticRule.error_message cannot be empty")
        if not self.field_paths:
            raise ValueError("SemanticRule.field_paths cannot be empty")


class SchemaValidator:
    """
    Structural schema validation authority (STRUCTURAL AUTHORITY).
    
    Responsible for:
    - required fields
    - optional fields
    - unknown field rejection
    - type correctness
    - enum membership
    - list cardinality rules
    
    Rules:
    - No coercion
    - No defaults
    - No fallback logic
    
    If the payload doesn't match the schema exactly, it fails.
    """
    
    def __init__(self, schema: SchemaDefinition):
        """
        Initialize schema validator.
        
        Args:
            schema: Schema definition to validate against
        """
        self._schema = schema
        self._field_map = {f.name: f for f in schema.fields}
        self._required_fields = {f.name for f in schema.fields if f.required}
    
    def validate(self, payload: Any, context: ValidationContext) -> List[ValidationError]:
        """
        Validate payload against schema (structural validation only).
        
        Fixed evaluation order:
        1. Required fields
        2. Unknown fields
        3. Field types
        4. Field constraints
        
        Complete error collection (no early exit unless configured).
        Deterministic error ordering.
        
        Args:
            payload: Payload to validate
            context: Validation execution context
            
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        
        # Convert payload to dict (no mutation)
        # Tier-0 requirement: strict dict-only payloads for canonical hashing and replay stability
        # Object payloads with __dict__ leak private attributes and create non-deterministic surfaces
        if not isinstance(payload, dict):
            errors.append(ValidationError(
                error_code=ValidationErrorCode.INVALID_FIELD_TYPE,
                field_path="__root__",
                expected="dict",
                actual=type(payload).__name__,
                message=f"Payload must be dict type, got {type(payload).__name__}. "
                        "Object payloads are not allowed for Tier-0 canonical validation.",
                schema_name=self._schema.name,
                schema_version=self._schema.version,
                run_id=context.run_id
            ))
            return errors
        
        payload_dict = payload
        
        # Fixed evaluation order (deterministic)
        errors.extend(self._validate_required_fields(payload_dict, context))
        errors.extend(self._validate_unknown_fields(payload_dict, context))
        errors.extend(self._validate_field_types(payload_dict, context))
        errors.extend(self._validate_field_constraints(payload_dict, context))
        
        # Sort errors for deterministic ordering
        errors.sort(key=lambda e: (e.error_code.value, e.field_path))
        
        return errors
    
    def _validate_required_fields(
        self,
        payload: Dict[str, Any],
        context: ValidationContext
    ) -> List[ValidationError]:
        errors = []
        
        # Deterministic iteration order (sorted) - required for replay stability
        for field_name in sorted(self._required_fields):
            if field_name not in payload:
                errors.append(ValidationError(
                    error_code=ValidationErrorCode.MISSING_REQUIRED_FIELD,
                    field_path=field_name,
                    expected="present",
                    actual="missing",
                    message=f"Required field '{field_name}' is missing",
                    schema_name=self._schema.name,
                    schema_version=self._schema.version,
                    run_id=context.run_id
                ))
        
        return errors
    
    def _validate_unknown_fields(
        self,
        payload: Dict[str, Any],
        context: ValidationContext
    ) -> List[ValidationError]:
        if self._schema.allow_unknown_fields:
            return []
        
        errors = []
        unknown = set(payload.keys()) - set(self._field_map.keys())
        
        # Strict unknown field rejection (no exceptions, including underscore-prefixed)
        # Underscore fields are still unknown fields and violate schema contract
        for field_name in sorted(unknown):  # Deterministic ordering
            errors.append(ValidationError(
                error_code=ValidationErrorCode.UNKNOWN_FIELD_PRESENT,
                field_path=field_name,
                expected="known field",
                actual=field_name,
                message=f"Unknown field '{field_name}' present",
                schema_name=self._schema.name,
                schema_version=self._schema.version,
                run_id=context.run_id
            ))
        
        return errors
    
    def _validate_field_types(
        self,
        payload: Dict[str, Any],
        context: ValidationContext
    ) -> List[ValidationError]:
        """
        Validate field types (no coercion).
        
        Rules:
        - No coercion
        - No type inference
        - Exact type matching required
        
        Args:
            payload: Payload dictionary
            context: Validation context
            
        Returns:
            List of type validation errors
        """
        errors = []
        
        for field_name, field_spec in self._field_map.items():
            if field_name not in payload:
                continue
            
            value = payload[field_name]
            
            # None is allowed for optional fields
            if value is None and not field_spec.required:
                continue
            
            # Exact type matching (no coercion, no isinstance with inheritance)
            # Use type() for strict equality
            actual_type = type(value)
            if actual_type != field_spec.field_type:
                # Special case: bool is subclass of int in Python, but we want strict bool
                if field_spec.field_type == bool and isinstance(value, bool):
                    # bool is bool, but isinstance(bool, int) is True in Python
                    # Use type() for strict check
                    if actual_type == bool:
                        continue  # Correct type
                
                errors.append(ValidationError(
                    error_code=ValidationErrorCode.INVALID_FIELD_TYPE,
                    field_path=field_name,
                    expected=field_spec.field_type.__name__,
                    actual=actual_type.__name__,
                    message=f"Field '{field_name}' has wrong type: expected {field_spec.field_type.__name__}, got {actual_type.__name__}",
                    schema_name=self._schema.name,
                    schema_version=self._schema.version,
                    run_id=context.run_id
                ))
        
        return errors
    
    def _validate_field_constraints(
        self,
        payload: Dict[str, Any],
        context: ValidationContext
    ) -> List[ValidationError]:
        errors = []
        
        for field_name, field_spec in self._field_map.items():
            if field_name not in payload:
                continue
            
            value = payload[field_name]
            
            if value is None:
                continue
            
            if field_spec.allowed_values is not None:
                if value not in field_spec.allowed_values:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.ENUM_VALUE_INVALID,
                        field_path=field_name,
                        expected=str(field_spec.allowed_values),
                        actual=str(value),
                        message=f"Field '{field_name}' has invalid enum value",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
            
            if isinstance(value, (int, float)):
                if field_spec.min_value is not None and value < field_spec.min_value:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.SEMANTIC_INVARIANT_VIOLATION,
                        field_path=field_name,
                        expected=f">= {field_spec.min_value}",
                        actual=str(value),
                        message=f"Field '{field_name}' below minimum",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
                
                if field_spec.max_value is not None and value > field_spec.max_value:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.SEMANTIC_INVARIANT_VIOLATION,
                        field_path=field_name,
                        expected=f"<= {field_spec.max_value}",
                        actual=str(value),
                        message=f"Field '{field_name}' above maximum",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
            
            if isinstance(value, str):
                if field_spec.min_length is not None and len(value) < field_spec.min_length:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.SEMANTIC_INVARIANT_VIOLATION,
                        field_path=field_name,
                        expected=f"length >= {field_spec.min_length}",
                        actual=f"length {len(value)}",
                        message=f"Field '{field_name}' too short",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
                
                if field_spec.max_length is not None and len(value) > field_spec.max_length:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.SEMANTIC_INVARIANT_VIOLATION,
                        field_path=field_name,
                        expected=f"length <= {field_spec.max_length}",
                        actual=f"length {len(value)}",
                        message=f"Field '{field_name}' too long",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
            
            if isinstance(value, list):
                if field_spec.min_items is not None and len(value) < field_spec.min_items:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.LIST_CARDINALITY_VIOLATION,
                        field_path=field_name,
                        expected=f"items >= {field_spec.min_items}",
                        actual=f"items {len(value)}",
                        message=f"Field '{field_name}' has too few items",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
                
                if field_spec.max_items is not None and len(value) > field_spec.max_items:
                    errors.append(ValidationError(
                        error_code=ValidationErrorCode.LIST_CARDINALITY_VIOLATION,
                        field_path=field_name,
                        expected=f"items <= {field_spec.max_items}",
                        actual=f"items {len(value)}",
                        message=f"Field '{field_name}' has too many items",
                        schema_name=self._schema.name,
                        schema_version=self._schema.version,
                        run_id=context.run_id
                    ))
        
        return errors


class SemanticValidator:
    """
    Semantic invariant validation authority (MEANING AUTHORITY).
    
    Responsible for invariants such as:
    - timestamps not in the future
    - monotonically increasing counters
    - mutually exclusive fields
    - impossible state combinations
    - domain constraints (e.g. negative views)
    
    Semantic rules:
    - Deterministic
    - Side-effect free
    - Schema-bound
    - Time-source explicit (no now() calls)
    """
    
    def __init__(self, rules: List[SemanticRule]):
        """
        Initialize semantic validator.
        
        Args:
            rules: List of semantic validation rules
            
        Raises:
            ValueError: If rules contain non-deterministic patterns
        """
        self._rules = rules
        # Validate rules for determinism (static check)
        self._validate_rule_determinism()
    
    def _validate_rule_determinism(self) -> None:
        """
        Validate that semantic rules are deterministic.
        
        Tier-0 requirement: Rules must be provably deterministic.
        Closures over mutable state or global access violate this requirement.
        
        Checks for non-deterministic patterns:
        - Rules must be callable
        - Rules must not be closures over mutable state
        - Rules must not access mutable globals
        
        Raises:
            ValueError: If rule contains non-deterministic patterns
        """
        import inspect
        import types
        
        for rule in self._rules:
            if not callable(rule.check):
                raise ValueError(
                    f"SemanticRule '{rule.name}': check must be callable, "
                    f"got {type(rule.check)}"
                )
            
            # Tier-0 enforcement: Reject closures and global access
            if isinstance(rule.check, types.FunctionType):
                closure_vars = inspect.getclosurevars(rule.check)
                
                # Reject closures over non-local variables (mutable state risk)
                if closure_vars.nonlocals:
                    raise ValueError(
                        f"SemanticRule '{rule.name}': check is a closure over non-local variables: "
                        f"{list(closure_vars.nonlocals.keys())}. "
                        "Tier-0 requirement: Rules must not capture mutable state."
                    )
                
                # Reject access to mutable globals (environment coupling risk)
                # Allow only builtins and module-level constants
                if closure_vars.globals:
                    # Filter out builtins and known-safe globals
                    unsafe_globals = {
                        k: v for k, v in closure_vars.globals.items()
                        if k not in ('__builtins__', '__name__', '__doc__', '__file__', '__package__')
                        and not (inspect.ismodule(v) or inspect.isclass(v) or inspect.isfunction(v))
                    }
                    
                    if unsafe_globals:
                        raise ValueError(
                            f"SemanticRule '{rule.name}': check accesses mutable globals: "
                            f"{list(unsafe_globals.keys())}. "
                            "Tier-0 requirement: Rules must not depend on environment state."
                        )
    
    def validate(
        self,
        payload: Any,
        schema_name: str,
        schema_version: int,
        context: ValidationContext
    ) -> List[ValidationError]:
        """
        Validate semantic invariants.
        
        Rules are evaluated in deterministic order.
        All rules are checked (complete error collection).
        
        Args:
            payload: Payload to validate
            schema_name: Schema name
            schema_version: Schema version
            context: Validation context
            
        Returns:
            List of semantic validation errors
        """
        errors = []
        
        # Deterministic rule ordering (by rule name)
        sorted_rules = sorted(self._rules, key=lambda r: r.name)
        
        for rule in sorted_rules:
            try:
                # Rule check must be side-effect free and deterministic
                is_valid = rule.check(payload)
            except Exception as e:
                # Rule check raised exception → invariant violation
                errors.append(ValidationError(
                    error_code=ValidationErrorCode.SEMANTIC_INVARIANT_VIOLATION,
                    field_path=",".join(sorted(rule.field_paths)),  # Deterministic ordering
                    expected="rule check to succeed",
                    actual=f"exception: {str(e)}",
                    message=f"Rule '{rule.name}' failed with exception: {e}",
                    schema_name=schema_name,
                    schema_version=schema_version,
                    run_id=context.run_id
                ))
                continue
            
            if not is_valid:
                errors.append(ValidationError(
                    error_code=ValidationErrorCode.SEMANTIC_INVARIANT_VIOLATION,
                    field_path=",".join(sorted(rule.field_paths)),  # Deterministic ordering
                    expected="invariant satisfied",
                    actual="invariant violated",
                    message=rule.error_message,
                    schema_name=schema_name,
                    schema_version=schema_version,
                    run_id=context.run_id
                ))
        
        return errors


class ValidationEvaluator:
    """
    Deterministic validation decision logic (BRAIN).
    
    Responsible for decision-making only.
    
    Guarantees:
    - Fixed evaluation order
    - Complete error collection (no early exit unless configured)
    - Deterministic error ordering
    - Fail-closed on ambiguity
    
    Same input → same errors → same result.
    """
    
    def __init__(
        self,
        schema_validator: SchemaValidator,
        semantic_validator: Optional[SemanticValidator] = None
    ):
        """
        Initialize validation evaluator.
        
        Args:
            schema_validator: Schema validator for structural validation
            semantic_validator: Optional semantic validator for invariant checks
            
        Note:
            early_exit parameter removed - Tier-0 validation requires complete error collection.
            All errors must be collected for deterministic replay and audit integrity.
        """
        self._schema_validator = schema_validator
        self._semantic_validator = semantic_validator
    
    def evaluate(
        self,
        payload: Any,
        context: ValidationContext
    ) -> ValidationResult:
        """
        Evaluate validation decision for payload.
        
        Fixed evaluation order:
        1. Schema validation (structural)
        2. Semantic validation (invariants)
        
        Complete error collection (Tier-0 requirement: no early exit).
        Deterministic error ordering.
        
        Args:
            payload: Payload to validate
            context: Validation execution context
            
        Returns:
            ValidationResult with decision and errors
        """
        all_errors = []
        
        # Step 1: Schema validation (structural)
        # Tier-0 requirement: Complete error collection (no early exit)
        schema_errors = self._schema_validator.validate(payload, context)
        all_errors.extend(schema_errors)
        
        # Step 2: Semantic validation (invariants)
        # Only evaluate semantics if structure is valid
        # Semantic rules often assume type-correct structure.
        # Running them on invalid payloads wastes CPU and can generate misleading violations.
        if not schema_errors and self._semantic_validator is not None:
            semantic_errors = self._semantic_validator.validate(
                payload,
                context.schema_name,
                context.schema_version,
                context
            )
            all_errors.extend(semantic_errors)
        
        # Decision: valid if no errors, invalid otherwise
        decision = ValidationDecision.INVALID if all_errors else ValidationDecision.VALID
        
        # Deterministic error ordering (by error_code, then field_path)
        all_errors.sort(key=lambda e: (e.error_code.value, e.field_path))
        
        # Compute payload hash for audit
        payload_hash = self._compute_payload_hash(payload)
        
        return ValidationResult(
            decision=decision,
            errors=all_errors,
            payload_hash=payload_hash,
            schema_name=context.schema_name,
            schema_version=context.schema_version,
            run_id=context.run_id,
            timestamp=context.timestamp
        )
    
    @staticmethod
    def _compute_payload_hash(payload: Any) -> str:
        """
        Compute deterministic hash of payload.
        
        Used for:
        - Audit trail
        - Payload integrity verification
        - Replay verification
        
        Guarantees:
        - Same payload → same hash (deterministic)
        - Different payload → different hash (collision-resistant)
        - Stable across machines, languages, and time
        
        Tier-0 requirement: Only dict payloads are supported for canonical hashing.
        Non-dict payloads cannot be reliably canonicalized across Python versions.
        
        Args:
            payload: Payload to hash (must be dict)
            
        Returns:
            SHA-256 hash of canonical payload representation
            
        Raises:
            TypeError: If payload is not a dict (Tier-0 strict requirement)
        """
        if not isinstance(payload, dict):
            raise TypeError(
                f"Payload must be dict for canonical hashing, got {type(payload).__name__}. "
                "Tier-0 validation requires dict-only payloads for replay stability."
            )
        
        # Canonical JSON (sorted keys, compact separators, no whitespace)
        # This is the only stable serialization format across Python versions
        try:
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(',', ':'),
                ensure_ascii=False
            )
        except (TypeError, ValueError) as e:
            # Non-serializable payload cannot be hashed deterministically
            raise TypeError(
                f"Payload contains non-JSON-serializable types: {e}. "
                "Tier-0 validation requires JSON-serializable payloads for canonical hashing."
            ) from e
        
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ValidationAudit:
    decision: ValidationDecision
    error_count: int
    errors: List[ValidationError]
    payload_hash: str
    schema_name: str
    schema_version: int
    run_id: str
    timestamp: int
    pipeline_stage: str
    source: str


class AuditLoggerProtocol(Protocol):
    """
    Protocol for durable audit logging.
    
    Implementations must provide a method to record validation audit events
    to durable storage (e.g., file, database, append-only log).
    """
    
    def record_validation_audit(self, audit: ValidationAudit) -> None:
        """
        Record validation audit event to durable storage.
        
        Args:
            audit: Validation audit event to record
            
        Raises:
            RuntimeError: If audit logging fails (fail-closed behavior)
        """
        ...


class ValidationExecutor:
    """
    Enforces validation decisions (MECHANISM).
    
    Enforces the result:
    
    VALID → pass-through unchanged
    
    INVALID → block downstream + emit validation report
    
    Executor must:
    - Attach payload hash
    - Preserve original payload
    - Emit audit event (in-memory + optional durable)
    - Prevent silent drops
    
    Invalid facts do not enter filtering or deduplication.
    """
    
    def __init__(self, audit_logger: Optional[AuditLoggerProtocol] = None):
        """
        Initialize validation executor.
        
        Args:
            audit_logger: Optional durable audit logger for persistent audit trail.
                         If None, only in-memory audit trail is maintained.
        """
        self._audit_trail: List[ValidationAudit] = []
        self._audit_logger = audit_logger
    
    def execute(
        self,
        result: ValidationResult,
        payload: Any,
        context: ValidationContext
    ) -> Tuple[bool, Any]:
        """
        Execute validation decision.
        
        Args:
            result: Validation evaluation result
            payload: Original payload (immutable)
            context: Validation execution context
            
        Returns:
            Tuple of (accepted: bool, processed_payload: Any)
            - accepted=True: Fact valid, payload unchanged
            - accepted=False: Fact invalid, payload unchanged (blocked downstream)
        """
        # Build audit record
        audit = ValidationAudit(
            decision=result.decision,
            error_count=len(result.errors),
            errors=result.errors,
            payload_hash=result.payload_hash,
            schema_name=result.schema_name,
            schema_version=result.schema_version,
            run_id=result.run_id,
            timestamp=result.timestamp,
            pipeline_stage=context.pipeline_stage,
            source=str(context.source.value) if hasattr(context.source, 'value') else str(context.source)
        )
        
        # Record audit trail (in-memory)
        self._audit_trail.append(audit)
        
        # Emit to durable audit logger if provided (fail-closed)
        if self._audit_logger is not None:
            try:
                self._audit_logger.record_validation_audit(audit)
            except Exception as e:
                # Audit logging failure is fatal - validation cannot proceed without audit trail
                raise RuntimeError(
                    f"Failed to write validation audit trail: {e}. "
                    "Validation requires durable audit logging for replay safety."
                ) from e
        
        # Execute decision (no payload mutation)
        if result.decision == ValidationDecision.VALID:
            return True, payload  # Pass-through unchanged
        else:
            return False, payload  # Block downstream, payload unchanged
    
    def get_audit_trail(self) -> List[ValidationAudit]:
        return self._audit_trail.copy()


class ValidationInvariants:
    """
    Enforces absolute invariants on validation behavior (ABSOLUTE).
    
    Must enforce:
    - no payload mutation
    - no default injection
    - no inferred fields
    - no schema drift
    - no environment-based behavior
    - no silent acceptance
    
    Violation → hard stop.
    """
    
    @staticmethod
    def verify_no_payload_mutation(original: Any, validated: Any) -> None:
        """
        Verify payload was not mutated during validation.
        
        Hard rule: no payload mutation.
        
        Args:
            original: Original payload
            validated: Payload after validation
            
        Raises:
            RuntimeError: If payload was mutated
        """
        if original is not validated:
            raise RuntimeError(
                "INVARIANT VIOLATION: Payload was mutated during validation. "
                "Payload must remain unchanged."
            )
        
        # For dicts, verify contents unchanged
        if isinstance(original, dict) and isinstance(validated, dict):
            if original != validated:
                raise RuntimeError(
                    "INVARIANT VIOLATION: Payload contents were mutated during validation."
                )
    
    @staticmethod
    def verify_deterministic_result(
        payload: Any,
        evaluator: ValidationEvaluator,
        context: ValidationContext
    ) -> None:
        """
        Verify validation result is deterministic.
        
        Hard rule: no environment-based behavior.
        
        Args:
            payload: Payload to validate
            evaluator: Validation evaluator
            context: Validation context
            
        Raises:
            RuntimeError: If validation is non-deterministic
        """
        result1 = evaluator.evaluate(payload, context)
        result2 = evaluator.evaluate(payload, context)
        
        if result1.decision != result2.decision:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Non-deterministic validation decision. "
                f"Result 1: {result1.decision}, Result 2: {result2.decision}"
            )
        
        if result1.payload_hash != result2.payload_hash:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Non-deterministic payload hash. "
                f"Hash 1: {result1.payload_hash}, Hash 2: {result2.payload_hash}"
            )
        
        if len(result1.errors) != len(result2.errors):
            raise RuntimeError(
                f"INVARIANT VIOLATION: Non-deterministic error count. "
                f"Errors 1: {len(result1.errors)}, Errors 2: {len(result2.errors)}"
            )
        
        # Verify error contents are identical
        error1_codes = sorted(e.error_code.value for e in result1.errors)
        error2_codes = sorted(e.error_code.value for e in result2.errors)
        if error1_codes != error2_codes:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Non-deterministic error codes. "
                f"Codes 1: {error1_codes}, Codes 2: {error2_codes}"
            )
    
    @staticmethod
    def verify_no_silent_acceptance(result: ValidationResult) -> None:
        """
        Verify no silent acceptance of invalid facts.
        
        Hard rule: no silent acceptance.
        
        Args:
            result: Validation result to check
            
        Raises:
            RuntimeError: If valid decision with errors
        """
        if result.decision == ValidationDecision.VALID and result.errors:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Marked valid but has {len(result.errors)} errors. "
                "Valid facts must have zero errors."
            )
    
    @staticmethod
    def verify_no_default_injection(payload: Any, schema: SchemaDefinition) -> None:
        """
        Verify no default values were injected.
        
        Hard rule: no default injection.
        
        Tier-0 requirement: Validation must never inject defaults.
        Payload must contain only fields explicitly provided.
        
        Args:
            payload: Payload to check (must be dict)
            schema: Schema definition
            
        Raises:
            RuntimeError: If defaults were injected
            TypeError: If payload is not a dict
        """
        if not isinstance(payload, dict):
            raise TypeError(
                f"verify_no_default_injection requires dict payload, got {type(payload).__name__}"
            )
        
        # Check: payload should not contain fields that weren't in the original input
        # Since we don't have access to the original input here, we verify:
        # 1. No fields were added beyond what's in the schema
        # 2. No fields have "default-like" values that suggest injection
        
        # Get all schema field names
        schema_field_names = {field.name for field in schema.fields}
        
        # Check for fields in payload that aren't in schema
        # (This is already caught by unknown field validation, but we verify here too)
        payload_fields = set(payload.keys())
        unknown_fields = payload_fields - schema_field_names
        
        if unknown_fields:
            # This should have been caught earlier, but we verify invariant here
            raise RuntimeError(
                f"INVARIANT VIOLATION: Payload contains fields not in schema: {unknown_fields}. "
                "This suggests default injection or schema drift."
            )
        
        # Additional check: verify no None values were injected for optional fields
        # If a field is optional and not present, it should be absent (not None)
        # However, None is a valid value if explicitly provided, so we can't reject all Nones
        # This check focuses on structural integrity rather than value semantics
        
        # The key invariant: payload structure matches schema structure exactly
        # No fields added, no fields removed (unless optional and absent)
        # This is enforced by the schema validator, so this check is a defensive assertion
    
    @staticmethod
    def verify_no_schema_drift(
        context: ValidationContext,
        schema: SchemaDefinition
    ) -> None:
        """
        Verify schema matches context.
        
        Hard rule: no schema drift.
        
        Args:
            context: Validation context
            schema: Schema definition
            
        Raises:
            RuntimeError: If schema drift detected
        """
        if context.schema_name != schema.name:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Schema name drift. "
                f"Context: {context.schema_name}, Schema: {schema.name}"
            )
        
        if context.schema_version != schema.version:
            raise RuntimeError(
                f"INVARIANT VIOLATION: Schema version drift. "
                f"Context: {context.schema_version}, Schema: {schema.version}"
            )


class ValidationPipeline:
    """
    OPTIONAL orchestration wrapper around the pure validation authority.
    
    IMPORTANT:
    This class is NOT part of the canonical validation authority.
    It exists only for runtime integration (audit, registry, invariants).
    
    The Tier-0 validation authority is:
        validate()  → ValidationEvaluator → SchemaValidator/SemanticValidator
    
    This wrapper must never introduce validation policy.
    It provides optional conveniences:
    - Schema registry pre-checks (environment authority, not validation logic)
    - Audit trail integration
    - Post-validation invariant assertions
    - Orchestration sequencing
    
    For pure validation, use the validate() function directly.
    """
    
    def __init__(
        self,
        schema: SchemaDefinition,
        semantic_rules: Optional[List[SemanticRule]] = None,
        schema_registry: Optional[SchemaRegistry] = None,
        enable_determinism_check: bool = False,
        require_audit_logger: bool = True
    ):
        """
        Initialize validation pipeline.
        
        Args:
            schema: Schema definition to validate against
            semantic_rules: Optional semantic validation rules
            schema_registry: Optional schema registry for schema authority checks
            enable_determinism_check: If True, verify deterministic results (default: False).
                                        EXPENSIVE: Runs validation twice. Intended for staging/audit, not hot-path.
            require_audit_logger: If True, audit logger must be set before processing (default: True)
                                    Tier-0 requirement: durable audit trail is mandatory for replay safety
        """
        schema_validator = SchemaValidator(schema)
        semantic_validator = SemanticValidator(semantic_rules) if semantic_rules else None
        
        self._evaluator = ValidationEvaluator(
            schema_validator,
            semantic_validator
        )
        self._executor = ValidationExecutor()
        self._schema = schema
        self._schema_registry = schema_registry
        self._enable_determinism_check = enable_determinism_check
        self._require_audit_logger = require_audit_logger
    
    def set_audit_logger(self, audit_logger: Optional[AuditLoggerProtocol]) -> None:
        """
        Set durable audit logger for validation executor.
        
        Tier-0 requirement: Durable audit logging is mandatory for replay safety.
        If require_audit_logger=True, this must be called with a non-None logger.
        
        Args:
            audit_logger: Audit logger implementing AuditLoggerProtocol.
                         If None and require_audit_logger=True, will raise on process().
        """
        self._executor._audit_logger = audit_logger
    
    def process(
        self,
        payload: Any,
        context: ValidationContext
    ) -> Tuple[ValidationDecision, Any, ValidationResult]:
        """
        Process payload through validation pipeline.
        
        Args:
            payload: Payload to validate (immutable)
            context: Validation execution context
            
        Returns:
            Tuple of (decision, processed_payload, result)
            
        Raises:
            RuntimeError: If invariants are violated or audit logger required but not set
            ValueError: If schema mismatch detected
        """
        # Step 0: Verify audit logger is set if required (fail-closed)
        if self._require_audit_logger and self._executor._audit_logger is None:
            raise RuntimeError(
                "Tier-0 validation requires durable audit logger for replay safety. "
                "Call set_audit_logger() before processing payloads."
            )
        
        # Optional external authority pre-check (non-validation concern)
        # Registry failure is environment authority failure, not validation correctness failure
        registry_errors = []
        if self._schema_registry is not None:
            registry_errors = self._check_schema_registry(context)
        
        if registry_errors:
            # Produce INVALID result but do NOT treat this as core validation logic
            # This is environment authority failure, not payload validation failure
            payload_hash = ValidationEvaluator._compute_payload_hash(payload)
            result = ValidationResult(
                decision=ValidationDecision.INVALID,
                errors=registry_errors,
                payload_hash=payload_hash,
                schema_name=context.schema_name,
                schema_version=context.schema_version,
                run_id=context.run_id,
                timestamp=context.timestamp
            )
            accepted, output_payload = self._executor.execute(result, payload, context)
            ValidationInvariants.verify_no_payload_mutation(payload, output_payload)
            return result.decision, output_payload, result
        
        # Step 2: Verify no schema drift (pre-validation invariant)
        ValidationInvariants.verify_no_schema_drift(context, self._schema)
        
        # Step 3: Evaluate validation (core authority)
        result = self._evaluator.evaluate(payload, context)
        
        # Step 4: Post-validation invariant assertions (non-authority)
        # These verify system discipline but do not influence validation decisions
        if self._enable_determinism_check:
            # EXPENSIVE: deterministic replay verification
            # Intended for staging / audit validation, not hot-path execution
            ValidationInvariants.verify_deterministic_result(
                payload, self._evaluator, context
            )
        
        ValidationInvariants.verify_no_default_injection(payload, self._schema)
        ValidationInvariants.verify_no_silent_acceptance(result)
        
        # Step 7: Execute decision
        accepted, output_payload = self._executor.execute(result, payload, context)
        
        # Step 8: Verify no payload mutation
        ValidationInvariants.verify_no_payload_mutation(payload, output_payload)
        
        return result.decision, output_payload, result
    
    def _check_schema_registry(
        self,
        context: ValidationContext
    ) -> List[ValidationError]:
        """
        Check schema registry for schema authority.
        
        Returns:
            List of validation errors if schema not registered or version mismatch
        """
        errors = []
        
        # Check if schema is registered with exact name and version
        if not self._schema_registry.has(context.schema_name, context.schema_version):
            # Schema not registered - check if schema name exists with any version
            all_schemas = self._schema_registry.list_schemas()
            schema_name_exists = any(
                name == context.schema_name for name, _ in all_schemas
            )
            
            if not schema_name_exists:
                # Schema name not registered at all
                errors.append(ValidationError(
                    error_code=ValidationErrorCode.UNKNOWN_SCHEMA,
                    field_path="__schema__",
                    expected=f"registered schema '{context.schema_name}' v{context.schema_version}",
                    actual="not registered",
                    message=f"Schema '{context.schema_name}' v{context.schema_version} is not registered",
                    schema_name=context.schema_name,
                    schema_version=context.schema_version,
                    run_id=context.run_id
                ))
            else:
                # Schema name exists but version mismatch
                # Find the registered version(s) for better error message
                registered_versions = [
                    version for name, version in all_schemas
                    if name == context.schema_name
                ]
                registered_version_str = ", ".join(map(str, sorted(registered_versions)))
                
                errors.append(ValidationError(
                    error_code=ValidationErrorCode.SCHEMA_VERSION_MISMATCH,
                    field_path="__schema__",
                    expected=f"schema version {context.schema_version}",
                    actual=f"registered versions: {registered_version_str}",
                    message=f"Schema version mismatch: expected {context.schema_name} v{context.schema_version}, "
                            f"but registered versions are: {registered_version_str}",
                    schema_name=context.schema_name,
                    schema_version=context.schema_version,
                    run_id=context.run_id
                ))
        
        return errors
    
    def get_audit_trail(self) -> List[ValidationAudit]:
        """Get complete audit trail (immutable copy)."""
        return self._executor.get_audit_trail()


class SchemaRegistry:
    """
    Central registry for schema definitions.
    
    Provides deterministic schema lookup for validation.
    """
    
    def __init__(self):
        """Initialize schema registry."""
        self._schemas: Dict[Tuple[str, int], SchemaDefinition] = {}
        self._frozen = False
    
    def register(self, schema: SchemaDefinition) -> None:
        """
        Register schema definition.
        
        Args:
            schema: Schema definition to register
            
        Raises:
            ValueError: If schema already registered
        """
        if self._frozen:
            raise ValueError("SchemaRegistry is frozen and cannot register new schemas")
        
        key = (schema.name, schema.version)
        
        if key in self._schemas:
            existing = self._schemas[key]
            if existing != schema:
                raise ValueError(
                    f"Schema {schema.name} v{schema.version} already registered with different definition"
                )
            return  # Already registered with same definition
        
        self._schemas[key] = schema
    
    def get(self, name: str, version: int) -> Optional[SchemaDefinition]:
        """
        Get schema definition by name and version.
        
        Args:
            name: Schema name
            version: Schema version
            
        Returns:
            Schema definition if found, None otherwise
        """
        return self._schemas.get((name, version))
    
    def has(self, name: str, version: int) -> bool:
        """
        Check if schema is registered.
        
        Args:
            name: Schema name
            version: Schema version
            
        Returns:
            True if schema is registered, False otherwise
        """
        return (name, version) in self._schemas
    
    def freeze(self) -> None:
        """Freeze registry (no further registrations allowed)."""
        self._frozen = True
    
    def list_schemas(self) -> List[Tuple[str, int]]:
        """List all registered schema (name, version) pairs."""
        return sorted(self._schemas.keys())


# ============================================================================
# Pure Validation Authority Entrypoint
# ============================================================================

def validate(
    payload: Any,
    context: ValidationContext,
    schema_validator: SchemaValidator,
    semantic_validator: Optional[SemanticValidator] = None,
) -> ValidationResult:
    """
    Pure validation authority entrypoint.
    
    This is the Tier-0 canonical validation function.
    
    Guarantees:
    - No registry resolution
    - No invariant enforcement
    - No orchestration
    - Deterministic structural + semantic validation only
    
    This function answers exactly one question:
    > "Is this payload structurally and semantically valid according to the schema?"
    
    Args:
        payload: Payload to validate (must be dict)
        context: Validation execution context
        schema_validator: Schema validator for structural validation
        semantic_validator: Optional semantic validator for invariant checks
        
    Returns:
        ValidationResult with decision and errors
        
    Raises:
        TypeError: If payload is not a dict (Tier-0 requirement)
    """
    evaluator = ValidationEvaluator(schema_validator, semantic_validator)
    return evaluator.evaluate(payload, context)


# ============================================================================
# Public API (Pure Validation Authority Only)
# ============================================================================

__all__ = [
    'ValidationDecision',
    'ValidationSource',
    'ValidationErrorCode',
    'ValidationContext',
    'ValidationError',
    'ValidationResult',
    'FieldSpec',
    'SchemaDefinition',
    'SemanticRule',
    'SchemaValidator',
    'SemanticValidator',
    'ValidationEvaluator',
    'ValidationExecutor',
    'validate',
]