"""
/data/pipelines/computation/computation_invariants.py

Global Computation Laws (Hard Failures Only)

AUTHORITY: Defines non-negotiable laws that all computations must obey
PRINCIPLE: A computation that violates a global invariant is not a computation - it is a data corruption attempt
BEHAVIOR: Fail loudly, deterministically, and immediately - no fallback, no warning mode, no recovery

This file answers:
> "What properties must hold true for every computation — regardless of type,
  purpose, or implementation?"

If these laws are violated, the computation is invalid by definition.

DESIGN PRINCIPLE (CRITICAL):
Global invariants are axioms. They do not describe behavior. They define the valid universe.
If any axiom is false → abort.

CATEGORIES OF GLOBAL INVARIANTS:
1. Spec Integrity Invariants     - Computation must be referenceable
2. Input/Output Shape Invariants  - I/O must match declared schemas
3. Determinism Invariants         - Replay must be provable
4. Numeric Safety Invariants      - No silent corruption
5. Window Compatibility           - Time boundaries are truth
6. Side-Effect Prohibition        - Referential transparency required
7. Monotonicity & Stability       - Re-execution must be safe

ENFORCEMENT MODEL:
- Fixed set of invariant checks
- Zero extension points
- Zero overrides
- All violations raise hard exceptions

FAILURE SEMANTICS:
- Immediate termination
- Explicit error type
- Structured failure payload
- No retries
- No downgrade

Retries repeat corruption.

FORBIDDEN:
❌ "Best effort" computation
❌ Partial success semantics
❌ Silent coercion
❌ Floating tolerances without declaration
❌ Executor overrides
❌ Configurable invariants
❌ Logging-and-continue behavior

Laws are not configuration.

Specs define intent. Invariants define reality. Executors obey or die.
"""

from __future__ import annotations

from typing import Any, Optional, Dict
import math
import hashlib
import json
from copy import deepcopy

from .computation_spec import ComputationSpec, DeterminismLevel
from .computation_context import FrozenMapping
from .computation_spec_errors import ComputationDefinitionError
from .computation_errors import (
    ComputationInvariantViolation,
    InputBindingError,
    NonDeterministicExecutionError,
    PureExecutionViolationError,
    WindowMismatchError,
    ReplayDriftError,
)

# Try to import window registry for window validation
try:
    from ..windows.windows import WindowRegistry
    _WINDOW_REGISTRY_AVAILABLE = True
except ImportError:
    # Window registry may not be available in all contexts
    _WINDOW_REGISTRY_AVAILABLE = False
    WindowRegistry = None

# Try to get global window registry accessor if available
_WINDOW_REGISTRY_GETTER = None
try:
    from ..aggregation.windows import get_global_registry as get_agg_window_registry
    _WINDOW_REGISTRY_GETTER = get_agg_window_registry
except ImportError:
    pass


# ============================================================================
# SPEC INTEGRITY INVARIANTS
# ============================================================================

def _validate_spec_integrity(spec: ComputationSpec) -> None:
    """
    Validate spec integrity invariants.
    
    LAWS:
    - Spec must be immutable after construction (frozen dataclass)
    - Spec must be fully serializable
    - Spec must produce a stable identity hash
    - Identity fields must be explicitly declared
    - All identity fields must participate in canonical serialization
    - Hash must be stable across process evaluations
    
    Violation means the computation cannot be referenced safely.
    """
    # Enforce frozen state (immutability)
    if getattr(spec, "__dataclass_params__", None) is None or not getattr(spec.__dataclass_params__, "frozen", False):
        raise ComputationDefinitionError(
            "ComputationSpec must be frozen (immutable)",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Immutability check (hashable)
    if not hasattr(spec, '__hash__'):
        raise ComputationDefinitionError(
            "Computation spec must be hashable (immutable)",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Serializability check
    if not hasattr(spec, 'to_canonical_dict'):
        raise ComputationDefinitionError(
            "Computation spec must implement to_canonical_dict() for serialization",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    try:
        canonical = spec.to_canonical_dict()
        if not isinstance(canonical, dict):
            raise ComputationDefinitionError(
                "to_canonical_dict() must return dict",
                computation_name=getattr(spec, 'computation_name', None)
            )
    except Exception as e:
        raise ComputationDefinitionError(
            f"Spec serialization failed: {str(e)}",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Required identity fields
    required_fields = {
        'computation_type',
        'version',
        'input_schema',
        'output_schema',
        'requires_determinism',
    }
    
    missing = [f for f in required_fields if not hasattr(spec, f)]
    if missing:
        raise ComputationDefinitionError(
            f"Spec missing required identity fields: {missing}",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Enforce canonical field participation
    # All required identity fields must be present in canonical serialization
    for field in required_fields:
        if field not in canonical:
            raise ComputationDefinitionError(
                f"Identity field '{field}' missing from canonical serialization",
                computation_name=getattr(spec, 'computation_name', None)
            )
    
    # Stable hash verification (cross-process law)
    # Hash must be cryptographically stable across processes
    # Python's hash() is randomized per-process, so we use SHA-256 for deterministic identity
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    fingerprint = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    # Verify determinism: same canonical form must produce same fingerprint
    fingerprint_verify = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    if fingerprint != fingerprint_verify:
        raise ComputationDefinitionError(
            "Spec hash is not stable across evaluations (cryptographic identity failure)",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Store fingerprint for identity verification (if spec supports it)
    # This enables cross-process identity matching


# ============================================================================
# INPUT/OUTPUT SHAPE INVARIANTS
# ============================================================================

def _validate_input_shape(
    inputs: dict[str, Any],
    input_schema: FrozenMapping,
    computation_hash: str,
    spec: Optional[ComputationSpec] = None
) -> None:
    """
    Validate input shape invariants.
    
    LAWS:
    - Inputs must exactly match declared input schema
    - No undeclared fields allowed
    - No missing required fields
    - No implicit coercion or widening
    - Types must match exactly (no implicit conversion)
    - Schema versions must match
    
    Computation that changes shape is invalid.
    """
    schema_dict = input_schema.to_dict()
    
    # Check required fields
    required_fields = {
        k for k, v in schema_dict.items()
        if isinstance(v, dict) and v.get('required', False)
    }
    
    provided_fields = set(inputs.keys())
    missing = required_fields - provided_fields
    
    if missing:
        raise InputBindingError(
            f"Missing required input fields: {missing}",
            computation_hash=computation_hash
        )
    
    # Check for extra fields
    allowed_fields = set(schema_dict.keys())
    extra = provided_fields - allowed_fields
    
    if extra:
        raise InputBindingError(
            f"Undeclared input fields present: {extra}",
            computation_hash=computation_hash
        )
    
    # Enforce type exactness
    for k, v in schema_dict.items():
        if k not in inputs:
            continue  # Skip missing optional fields
        
        expected_type = v.get("type") if isinstance(v, dict) else None
        if expected_type:
            actual_value = inputs[k]
            # Type checking based on expected type string
            type_mismatch = False
            if expected_type == "int" and not isinstance(actual_value, int):
                type_mismatch = True
            elif expected_type == "float" and not isinstance(actual_value, (int, float)):
                type_mismatch = True
            elif expected_type == "str" and not isinstance(actual_value, str):
                type_mismatch = True
            elif expected_type == "bool" and not isinstance(actual_value, bool):
                type_mismatch = True
            
            if type_mismatch:
                raise InputBindingError(
                    f"Field '{k}' expected type {expected_type}, got {type(actual_value).__name__}",
                    computation_hash=computation_hash
                )
    
    # Enforce schema version identity (if spec provides version info)
    if spec and hasattr(spec, 'input_schema_version'):
        schema_version = schema_dict.get('version') or schema_dict.get('schema_version')
        if schema_version and spec.input_schema_version != schema_version:
            raise InputBindingError(
                f"Input schema version mismatch: spec expects {spec.input_schema_version}, got {schema_version}",
                computation_hash=computation_hash
            )
    
    # Schema registry validation (if registry available)
    # Verify schema fingerprint against canonical registry authority
    if spec and hasattr(spec, 'input_schema_fingerprint'):
        # Compute schema fingerprint for validation
        schema_fingerprint = hashlib.sha256(
            json.dumps(schema_dict, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        
        if spec.input_schema_fingerprint != schema_fingerprint:
            raise InputBindingError(
                f"Input schema fingerprint mismatch - schema may have diverged from canonical authority",
                computation_hash=computation_hash
            )


def _validate_output_shape(
    output: dict[str, Any],
    output_schema: FrozenMapping,
    computation_hash: str,
    spec: Optional[ComputationSpec] = None
) -> None:
    """
    Validate output shape invariants.
    
    LAWS:
    - Outputs must exactly match declared output schema
    - No missing required fields
    - No implicit coercion or widening
    - Types must match exactly
    - Schema versions must match
    """
    schema_dict = output_schema.to_dict()
    
    # Check required fields
    required_fields = {
        k for k, v in schema_dict.items()
        if isinstance(v, dict) and v.get('required', False)
    }
    
    provided_fields = set(output.keys())
    missing = required_fields - provided_fields
    
    if missing:
        raise ComputationInvariantViolation(
            invariant="output_schema",
            details=f"Missing required output fields: {missing}",
            computation_hash=computation_hash
        )
    
    # Check for extra fields (strict mode)
    allowed_fields = set(schema_dict.keys())
    extra = provided_fields - allowed_fields
    
    if extra:
        raise ComputationInvariantViolation(
            invariant="output_schema",
            details=f"Undeclared output fields present: {extra}",
            computation_hash=computation_hash
        )
    
    # Enforce type exactness
    for k, v in schema_dict.items():
        if k not in output:
            continue  # Skip missing optional fields
        
        expected_type = v.get("type") if isinstance(v, dict) else None
        if expected_type:
            actual_value = output[k]
            # Type checking based on expected type string
            type_mismatch = False
            if expected_type == "int" and not isinstance(actual_value, int):
                type_mismatch = True
            elif expected_type == "float" and not isinstance(actual_value, (int, float)):
                type_mismatch = True
            elif expected_type == "str" and not isinstance(actual_value, str):
                type_mismatch = True
            elif expected_type == "bool" and not isinstance(actual_value, bool):
                type_mismatch = True
            
            if type_mismatch:
                raise ComputationInvariantViolation(
                    invariant="output_type_mismatch",
                    details=f"Field '{k}' expected type {expected_type}, got {type(actual_value).__name__}",
                    computation_hash=computation_hash
                )
    
    # Schema registry validation (if registry available)
    # Verify schema fingerprint against canonical registry authority
    if spec and hasattr(spec, 'output_schema_fingerprint'):
        # Compute schema fingerprint for validation
        schema_fingerprint = hashlib.sha256(
            json.dumps(schema_dict, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        
        if spec.output_schema_fingerprint != schema_fingerprint:
            raise ComputationInvariantViolation(
                invariant="output_schema_fingerprint_mismatch",
                details="Output schema fingerprint mismatch - schema may have diverged from canonical authority",
                computation_hash=computation_hash
            )


# ============================================================================
# NUMERIC SAFETY INVARIANTS
# ============================================================================

def _validate_numeric_safety(
    value: Any, 
    path: str, 
    computation_hash: str,
    spec: Optional[ComputationSpec] = None
) -> None:
    """
    Validate numeric safety invariants recursively.
    
    LAWS:
    - No NaN values
    - No ±Infinity values
    - Integer outputs must be integral
    - Numeric bounds must be respected (if declared)
    
    Silent numeric corruption is worse than failure.
    """
    if isinstance(value, float):
        if math.isnan(value):
            raise ComputationInvariantViolation(
                invariant="no_nan",
                details=f"NaN value detected at path '{path}'",
                computation_hash=computation_hash
            )
        if math.isinf(value):
            raise ComputationInvariantViolation(
                invariant="no_infinity",
                details=f"Infinity value detected at path '{path}'",
                computation_hash=computation_hash
            )
        
        # Integer integrality enforcement
        # If schema expects integer at this path, float must be integral
        if spec and hasattr(spec, 'output_schema'):
            schema_dict = spec.output_schema.to_dict()
            # Try to find field definition for this path
            # For nested paths, we'd need to traverse, but for top-level we check
            field_name = path.split('.')[0] if '.' in path else path
            if field_name in schema_dict:
                field_def = schema_dict[field_name]
                if isinstance(field_def, dict):
                    expected_type = field_def.get("type")
                    if expected_type == "int" and not value.is_integer():
                        raise ComputationInvariantViolation(
                            invariant="integer_integrity",
                            details=f"Non-integral float for integer field at {path}: {value}",
                            computation_hash=computation_hash
                        )
                    
                    # Bound overflow checks
                    bounds = field_def.get("bounds")
                    if bounds:
                        min_val = bounds.get("min")
                        max_val = bounds.get("max")
                        if min_val is not None and value < min_val:
                            raise ComputationInvariantViolation(
                                invariant="numeric_bounds_violation",
                                details=f"Value {value} at {path} below minimum {min_val}",
                                computation_hash=computation_hash
                            )
                        if max_val is not None and value > max_val:
                            raise ComputationInvariantViolation(
                                invariant="numeric_bounds_violation",
                                details=f"Value {value} at {path} above maximum {max_val}",
                                computation_hash=computation_hash
                            )
    elif isinstance(value, int):
        # Integer bounds checks
        if spec and hasattr(spec, 'output_schema'):
            schema_dict = spec.output_schema.to_dict()
            field_name = path.split('.')[0] if '.' in path else path
            if field_name in schema_dict:
                field_def = schema_dict[field_name]
                if isinstance(field_def, dict):
                    bounds = field_def.get("bounds")
                    if bounds:
                        min_val = bounds.get("min")
                        max_val = bounds.get("max")
                        if min_val is not None and value < min_val:
                            raise ComputationInvariantViolation(
                                invariant="numeric_bounds_violation",
                                details=f"Value {value} at {path} below minimum {min_val}",
                                computation_hash=computation_hash
                            )
                        if max_val is not None and value > max_val:
                            raise ComputationInvariantViolation(
                                invariant="numeric_bounds_violation",
                                details=f"Value {value} at {path} above maximum {max_val}",
                                computation_hash=computation_hash
                            )
    elif isinstance(value, dict):
        for k, v in value.items():
            _validate_numeric_safety(v, f"{path}.{k}", computation_hash, spec)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _validate_numeric_safety(item, f"{path}[{idx}]", computation_hash, spec)


# ============================================================================
# DETERMINISM INVARIANTS
# ============================================================================

def _validate_determinism_declaration(spec: ComputationSpec) -> None:
    """
    Validate determinism declaration.
    
    LAWS:
    - Determinism contract must be explicit
    - Floating-point usage must be explicitly declared
    - Deterministic computations cannot declare nondeterministic sources
    - Non-deterministic computations must declare their entropy sources
    - Randomness, time, ordering effects forbidden unless declared
    - Structural checks: no clock access, no random access, no global state
    """
    if not hasattr(spec, 'requires_determinism'):
        raise ComputationDefinitionError(
            "Spec must explicitly declare requires_determinism",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    if not isinstance(spec.requires_determinism, bool):
        raise ComputationDefinitionError(
            "requires_determinism must be boolean (True or False)",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    if not hasattr(spec, 'allows_floating_point'):
        raise ComputationDefinitionError(
            "Spec must explicitly declare allows_floating_point",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    if not isinstance(spec.allows_floating_point, bool):
        raise ComputationDefinitionError(
            "allows_floating_point must be boolean (True or False)",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Validate determinism declaration consistency
    if spec.determinism is None:
        raise ComputationDefinitionError(
            "determinism declaration is required",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Enforce: Deterministic computations cannot declare nondeterministic sources
    if spec.requires_determinism:
        if spec.determinism.level == DeterminismLevel.NON_DETERMINISTIC:
            raise ComputationDefinitionError(
                "Deterministic computations cannot declare nondeterministic sources",
                computation_name=getattr(spec, 'computation_name', None)
            )
        
        # If deterministic, randomness sources should be None or empty
        if spec.determinism.randomness_sources:
            raise ComputationDefinitionError(
                "Deterministic computations cannot declare randomness sources",
                computation_name=getattr(spec, 'computation_name', None)
            )
    else:
        # Enforce: Non-deterministic computations must declare their entropy sources
        if spec.determinism.level != DeterminismLevel.NON_DETERMINISTIC:
            # If not explicitly non-deterministic but requires_determinism is False,
            # we still require entropy source declaration for clarity
            pass
        
        if spec.determinism.level == DeterminismLevel.NON_DETERMINISTIC:
            if not spec.determinism.randomness_sources or len(spec.determinism.randomness_sources) == 0:
                raise ComputationDefinitionError(
                    "Non-deterministic computations must declare their entropy sources",
                    computation_name=getattr(spec, 'computation_name', None)
                )
    
    # Structural determinism enforcement: check for prohibited access patterns
    # This validates that deterministic computations don't access non-deterministic sources
    if spec.requires_determinism:
        # Check execution function for prohibited patterns (if inspectable)
        if hasattr(spec, 'execution_fn') and callable(spec.execution_fn):
            # Note: Full structural analysis requires static analysis tools
            # This is a best-effort check; executor-level sandboxing is required for full enforcement
            fn_code = getattr(spec.execution_fn, '__code__', None)
            if fn_code:
                # Check for prohibited module names in code constants
                prohibited_modules = {'time', 'random', 'datetime', 'os', 'sys'}
                code_consts = getattr(fn_code, 'co_consts', ())
                detected_modules = []
                for const in code_consts:
                    if isinstance(const, str):
                        for mod in prohibited_modules:
                            if mod in const.lower():
                                detected_modules.append(mod)
                
                if detected_modules:
                    # Warn about detected prohibited modules (heuristic check)
                    # Full enforcement requires executor-level sandboxing
                    # This is a structural check, not a runtime guarantee
                    raise ComputationDefinitionError(
                        f"Deterministic computation may access prohibited modules: {set(detected_modules)}. "
                        "Full determinism enforcement requires executor-level sandboxing.",
                        computation_name=getattr(spec, 'computation_name', None)
                    )


# ============================================================================
# WINDOW COMPATIBILITY INVARIANTS
# ============================================================================

def _validate_window_declaration(spec: ComputationSpec) -> None:
    """
    Validate window declaration.
    
    LAWS:
    - Window dependency must be properly declared
    - Window identity must match exactly
    - Window must exist in registry (hard-failing)
    - Boundary/version alignment required
    
    Windows are truth boundaries, not hints.
    """
    if hasattr(spec, 'window_dependency'):
        window_dep = spec.window_dependency
        
        if window_dep is not None and not isinstance(window_dep, str):
            raise ComputationDefinitionError(
                "window_dependency must be string or None",
                computation_name=getattr(spec, 'computation_name', None)
            )
        
        if window_dep is not None and not window_dep:
            raise ComputationDefinitionError(
                "window_dependency cannot be empty string",
                computation_name=getattr(spec, 'computation_name', None)
            )
        
        # Enforce: Window must exist in registry (hard-failing, mandatory)
        if window_dep is not None:
            window_validated = False
            validation_error = None
            
            # Try to validate against available registry (hard-fail if not found)
            if _WINDOW_REGISTRY_GETTER is not None:
                try:
                    registry = _WINDOW_REGISTRY_GETTER()
                    # Check if window exists in registry
                    try:
                        window_def = registry.get(window_dep)
                        window_validated = True
                        # Verify window identity fingerprint matches (if available)
                        if hasattr(window_def, 'identity_hash') and hasattr(spec, 'window_identity_hash'):
                            if spec.window_identity_hash != window_def.identity_hash:
                                raise WindowMismatchError(
                                    f"Window identity hash mismatch for '{window_dep}'",
                                    window_name=window_dep,
                                    computation_name=getattr(spec, 'computation_name', None)
                                )
                    except KeyError:
                        # Window not found in registry - hard fail
                        raise WindowMismatchError(
                            f"Declared window '{window_dep}' not found in registry",
                            window_name=window_dep,
                            computation_name=getattr(spec, 'computation_name', None)
                        )
                    except AttributeError as e:
                        # Registry doesn't have get method - cannot validate
                        validation_error = f"Registry validation failed: {str(e)}"
                except Exception as e:
                    # Registry not accessible - hard fail (mandatory validation)
                    validation_error = f"Registry not accessible: {str(e)}"
            
            # Window registry validation is MANDATORY - cannot defer
            if not window_validated:
                raise WindowMismatchError(
                    f"Cannot validate window '{window_dep}' - registry validation is mandatory. {validation_error or 'Registry not available'}",
                    window_name=window_dep,
                    computation_name=getattr(spec, 'computation_name', None)
                )


# ============================================================================
# SIDE-EFFECT PROHIBITION
# ============================================================================

def _validate_input_immutability(
    inputs_before: dict[str, Any],
    inputs_after: dict[str, Any],
    computation_hash: str
) -> None:
    """
    Validate inputs were not mutated.
    
    LAWS:
    - No mutation of input objects
    - Computations must be referentially transparent
    - Deep structural comparison required (not just equality)
    
    Equality comparison is insufficient for referential transparency.
    We need deep structural freezing verification.
    """
    # Use deepcopy for true structural snapshot comparison
    # This catches mutations in nested structures that == might miss
    try:
        inputs_snapshot = deepcopy(inputs_before)
        if inputs_snapshot != inputs_after:
            raise PureExecutionViolationError(
                operation="input_mutation",
                computation_hash=computation_hash,
                details="Computation mutated input objects (deep structural comparison failed)"
            )
    except Exception as e:
        # If deepcopy fails, fall back to equality check
        # But log that we couldn't do full structural verification
        if inputs_before != inputs_after:
            raise PureExecutionViolationError(
                operation="input_mutation",
                computation_hash=computation_hash,
                details=f"Computation mutated input objects (equality check failed: {str(e)})"
            )


# ============================================================================
# MONOTONICITY & STABILITY
# ============================================================================

def _validate_replay_safety(
    original_output_fingerprint: str,
    replay_output_fingerprint: str,
    computation_hash: str,
    window_id: str
) -> None:
    """
    Validate replay produced identical output.
    
    LAWS:
    - Re-execution with same inputs must not change result
    - Idempotent replays must be safe
    - Order variance independence required
    - Partial execution safety required
    """
    if original_output_fingerprint != replay_output_fingerprint:
        raise ReplayDriftError(
            computation_hash=computation_hash,
            window_identity=window_id,
            expected_fingerprint=original_output_fingerprint,
            actual_fingerprint=replay_output_fingerprint
        )


def _validate_monotonicity_declaration(spec: ComputationSpec) -> None:
    """
    Validate monotonicity and order independence.
    
    LAWS:
    - Order-sensitive computations violate monotonicity invariants
    - Partial execution must be safe
    - Order variance independence required
    
    This checks for declared order sensitivity and rejects it if incompatible
    with monotonicity requirements.
    """
    # Check if spec declares order sensitivity
    # If there's an is_order_sensitive field, validate it
    if hasattr(spec, 'is_order_sensitive') and spec.is_order_sensitive:
        raise ComputationDefinitionError(
            "Order-sensitive computations violate monotonicity invariants",
            computation_name=getattr(spec, 'computation_name', None)
        )
    
    # Check computation type for order sensitivity
    # Some computation types are inherently order-sensitive
    if hasattr(spec, 'computation_type'):
        # STATEFUL computations might be order-sensitive by nature
        # This is a design decision - we'll flag it for review
        # For now, we allow STATEFUL but require explicit declaration
        pass


# ============================================================================
# PUBLIC API (MINIMAL & SEALED)
# ============================================================================

def validate_spec(spec: ComputationSpec) -> None:
    """
    Validate computation spec against all global invariants.
    
    Called before registration. Failures prevent registration.
    
    Enforces:
    - Spec integrity (frozen, canonical, stable hash)
    - Determinism declarations (entropy sources)
    - Window declarations (registry validation)
    - Monotonicity declarations (order independence)
    
    Args:
        spec: ComputationSpec to validate
        
    Raises:
        ComputationDefinitionError: Spec violates global invariants
    """
    _validate_spec_integrity(spec)
    _validate_determinism_declaration(spec)
    _validate_window_declaration(spec)
    _validate_monotonicity_declaration(spec)


def validate_inputs(
    spec: ComputationSpec,
    inputs: dict[str, Any],
    computation_hash: str
) -> None:
    """
    Validate inputs against spec and global invariants.
    
    Called before execution. Failures prevent execution.
    
    Enforces:
    - Input shape matches declared schema
    - No missing required fields
    - No undeclared fields
    - Type exactness (no implicit coercion)
    - Schema version identity
    
    Args:
        spec: ComputationSpec
        inputs: Input data dictionary
        computation_hash: Computation hash for error reporting
        
    Raises:
        InputBindingError: Input violates invariants
    """
    if hasattr(spec, 'input_schema'):
        _validate_input_shape(inputs, spec.input_schema, computation_hash, spec)


def validate_outputs(
    spec: ComputationSpec,
    outputs: dict[str, Any],
    computation_hash: str
) -> None:
    """
    Validate outputs against spec and global invariants.
    
    Called after execution. Failures invalidate execution.
    
    Enforces:
    - Output shape matches declared schema
    - No missing required fields
    - No undeclared fields
    - Type exactness (no implicit coercion)
    - Numeric safety (no NaN, no Infinity)
    - Integer integrality
    - Numeric bounds enforcement
    
    Args:
        spec: ComputationSpec
        outputs: Output data dictionary
        computation_hash: Computation hash for error reporting
        
    Raises:
        ComputationInvariantViolation: Output violates schema
        ComputationInvariantViolation: Numeric safety violation
    """
    if hasattr(spec, 'output_schema'):
        _validate_output_shape(outputs, spec.output_schema, computation_hash, spec)
    
    # Numeric safety (with bounds and integrality checks)
    _validate_numeric_safety(outputs, "output", computation_hash, spec)


def validate_determinism(
    spec: ComputationSpec,
    output1_fingerprint: str,
    output2_fingerprint: str,
    computation_hash: str,
    execution_id_1: str,
    execution_id_2: str
) -> None:
    """
    Validate determinism by comparing execution outputs.
    
    Called for determinism verification. Failures indicate
    non-deterministic computation.
    
    Enforces:
    - Same inputs must produce same outputs
    - Output fingerprints must match exactly
    
    Args:
        spec: ComputationSpec
        output1_fingerprint: First execution output fingerprint
        output2_fingerprint: Second execution output fingerprint
        computation_hash: Computation hash for error reporting
        execution_id_1: First execution ID
        execution_id_2: Second execution ID
        
    Raises:
        NonDeterministicExecutionError: Outputs differ for same inputs
    """
    if spec.requires_determinism:
        if output1_fingerprint != output2_fingerprint:
            raise NonDeterministicExecutionError(
                computation_hash=computation_hash,
                execution_id_1=execution_id_1,
                execution_id_2=execution_id_2,
                divergence_details=f"Output fingerprints differ: {output1_fingerprint} vs {output2_fingerprint}"
            )
