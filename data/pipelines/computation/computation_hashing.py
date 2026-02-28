"""
/data/pipelines/computation/computation_hashing.py

Cryptographic Identity for Computation Contracts

AUTHORITY: Single authority for turning computation contracts into stable cryptographic identity
PRINCIPLE: Identity must depend only on declared meaning - never on execution
BEHAVIOR: Deterministic canonicalization → collision-resistant hash → immutable identity

This file answers:
> "Are these two computations provably the same, forever?"

If the hash lies:
- Registry deduplication fails
- Replay lies
- Caching corrupts results
- Provenance becomes fiction

There is no fallback from a wrong hash.

DESIGN PRINCIPLE (CRITICAL):
If something affects output but is not included in the hash, the system is corrupt.

CONCEPTUAL MODEL:
Computation Identity = 
    canonical(spec) +
    canonical(window identities) +
    canonical(input/output schemas) +
    canonical(determinism declaration) +
    version salt

Execution is irrelevant.

IDENTITY INPUTS (EXPLICIT):
MUST include:
✅ computation_type
✅ computation_version
✅ declared inputs schema
✅ declared outputs schema
✅ window identities (if any)
✅ determinism declaration
✅ numeric precision declarations
✅ full parameter set

MUST NOT include:
❌ Runtime context
❌ Executor code
❌ System clocks
❌ Environment variables

CANONICALIZATION RULES (HARD LAW):
1. Convert all identity material to pure Python primitives
2. Remove all non-identity fields
3. Sort all dictionaries by key
4. Sort all lists only if order is not semantically meaningful
5. Serialize using canonical JSON (UTF-8, no whitespace, no trailing zeros)
6. Reject any non-serializable value

If canonicalization is ambiguous → fail.

CRYPTOGRAPHIC RULES:
- Algorithm: SHA-256
- Output format: lowercase hex
- No truncation
- No salting except explicit version salt
- Version salt MUST be declared in spec

Hashing is identity, not security theater.

A computation hash is a mathematical claim:
"These two executions mean the same thing."

If that claim is false once, the system is untrustworthy forever.
"""

from __future__ import annotations

from typing import Any, Optional, Dict
import hashlib
import json
import math

from .computation_spec import ComputationSpec


# ============================================================================
# CANONICAL JSON SERIALIZATION
# ============================================================================

def canonical_json(data: dict[str, Any]) -> bytes:
    """
    Canonical JSON serialization for hashing.
    
    This is mandatory formatting. Any deviation breaks identity.
    
    RULES:
    - UTF-8 encoding
    - No whitespace (separators=(',', ':'))
    - Sorted keys
    - ensure_ascii=False for deterministic Unicode
    
    Args:
        data: Dictionary to serialize
        
    Returns:
        UTF-8 encoded canonical JSON bytes
        
    Raises:
        TypeError: Non-serializable value
        ValueError: NaN or Infinity detected
    """
    # Validate for ambiguous values
    _validate_serializable(data)
    
    # Serialize with canonical formatting
    json_str = json.dumps(
        data,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,  # Reject NaN/Infinity
    )
    
    return json_str.encode('utf-8')


def _validate_serializable(obj: Any) -> None:
    """
    Recursively validate object is safely serializable.
    
    Rejects:
    - NaN, Infinity (ambiguous)
    - Functions, lambdas, closures
    - Custom objects without explicit serialization
    
    Raises:
        TypeError: Non-serializable value
        ValueError: Ambiguous value (NaN, Infinity)
    """
    if isinstance(obj, float):
        if math.isnan(obj):
            raise ValueError(
                "Cannot hash computation with NaN values - ambiguous identity"
            )
        if math.isinf(obj):
            raise ValueError(
                "Cannot hash computation with Infinity values - ambiguous identity"
            )
    
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"Dictionary keys must be strings for canonical JSON, got {type(key)}"
                )
            _validate_serializable(value)
    
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _validate_serializable(item)
    
    elif callable(obj):
        raise TypeError(
            f"Cannot serialize callable {obj} - functions not allowed in identity material"
        )
    
    elif isinstance(obj, type):
        raise TypeError(
            f"Cannot serialize type {obj} - types not allowed in identity material"
        )
    
    elif not isinstance(obj, (str, int, bool, type(None))):
        # Check if it has a to_dict method (FrozenMapping, etc.)
        if hasattr(obj, 'to_dict'):
            # Tier-0: Validate that to_dict() returns canonicalizable primitives
            try:
                result = obj.to_dict()
                if not isinstance(result, (dict, list, str, int, bool, type(None))):
                    raise TypeError(
                        f"to_dict() must return canonicalizable primitives (dict/list/primitive), "
                        f"got {type(result)}"
                    )
                # Recursively validate the result
                _validate_serializable(result)
            except (TypeError, ValueError) as e:
                raise TypeError(
                    f"Cannot trust to_dict() on {type(obj)} - validation failed: {e}"
                ) from e
        else:
            raise TypeError(
                f"Cannot serialize {type(obj)} - only primitives allowed in identity material"
            )


# ============================================================================
# IDENTITY MATERIAL EXTRACTION
# ============================================================================

def extract_identity_material(
    spec: ComputationSpec,
    window_registry: Optional[Any] = None
) -> dict[str, Any]:
    """
    Extract identity material from computation spec.
    
    If a field affects output, it must appear here.
    
    INCLUDED FIELDS:
    - type: computation_type
    - version: computation_version (with explicit salt validation)
    - inputs: input_schema
    - outputs: output_schema
    - windows: window_identities (canonically sorted, validated against registry)
    - determinism: structured determinism declaration
    - numeric_contract: complete numeric precision declarations
    - parameters: full parameter set
    
    TIER-0 INVARIANT:
    If spec has windows, window_registry MUST be provided.
    Identity validity cannot be environment-dependent.
    
    Args:
        spec: ComputationSpec to extract from
        window_registry: Window registry for authoritative window validation.
                        REQUIRED if spec has windows (required_windows or window_dependency).
                        Must be None only if spec has no window dependencies.
        
    Returns:
        Canonical identity dictionary
        
    Raises:
        ValueError: Missing required identity field, invalid version salt, unregistered window,
                    or missing window_registry when windows are present
        TypeError: Invalid version type
    """
    identity: dict[str, Any] = {}
    
    # Computation type (required)
    identity['type'] = spec.computation_type.name
    
    # Version (required) - Tier-0: Enforce explicit semantic identity salt
    if not spec.version:
        raise ValueError(
            "Missing computation version salt - identity unsafe. "
            "Version must be explicit semantic salt string."
        )
    if not isinstance(spec.version, str):
        raise TypeError(
            f"version must be immutable semantic salt string, got {type(spec.version)}"
        )
    identity['version'] = spec.version
    
    # Input schema (required)
    identity['inputs'] = _canonicalize_value(spec.input_schema)
    
    # Output schema (required)
    identity['outputs'] = _canonicalize_value(spec.output_schema)
    
    # Window identities (if any) - Tier-0: Canonically sorted for deterministic identity
    # TIER-0 ENFORCEMENT: Registry validation is MANDATORY when windows are present
    if spec.required_windows:
        # Tier-0 invariant: window_registry MUST be provided when windows exist
        if window_registry is None:
            raise ValueError(
                "Tier-0 invariant violation: window_registry is required when spec has windows. "
                "Identity validity cannot be environment-dependent. "
                f"Spec has {len(spec.required_windows)} required windows but no registry provided."
            )
        
        # Validate all windows against registry (MANDATORY)
        for w in spec.required_windows:
            try:
                # Attempt to retrieve window to verify it exists and is registered
                window_registry.get(w.window_identity)
            except (KeyError, AttributeError) as e:
                raise ValueError(
                    f"Window identity '{w.window_identity}' (version '{w.window_version}') "
                    f"not found in registry - invalid computation spec"
                ) from e
        
        # Tier-0: Canonical sort by (window_identity, window_version) for deterministic ordering
        # This ensures identical window sets hash identically regardless of declaration order
        identity['windows'] = sorted(
            (
                {
                    'window_identity': w.window_identity,
                    'window_version': w.window_version,
                }
                for w in spec.required_windows
            ),
            key=lambda x: (x['window_identity'], x['window_version'])
        )
    elif spec.window_dependency:
        # Tier-0 invariant: window_registry MUST be provided when window_dependency exists
        if window_registry is None:
            raise ValueError(
                "Tier-0 invariant violation: window_registry is required when spec has window_dependency. "
                "Identity validity cannot be environment-dependent. "
                f"Spec has window_dependency '{spec.window_dependency}' but no registry provided."
            )
        
        # Validate single window dependency against registry (MANDATORY)
        try:
            window_registry.get(spec.window_dependency)
        except (KeyError, AttributeError) as e:
            raise ValueError(
                f"Window dependency '{spec.window_dependency}' not found in registry - "
                f"invalid computation spec"
            ) from e
        identity['windows'] = [{'window_identity': spec.window_dependency}]
    
    # Determinism declaration (required) - Tier-0: Structured contract, not boolean
    if spec.determinism is None:
        raise ValueError("determinism declaration is required for identity hashing")
    
    # Build structured determinism contract from DeterminismDeclaration
    identity['determinism'] = {
        'requires_determinism': spec.requires_determinism,
        'level': spec.determinism.level.name,
        'uses_floating_point': spec.determinism.uses_floating_point,
        'numerical_tolerance': spec.determinism.numerical_tolerance,
        'randomness_sources': (
            sorted(list(spec.determinism.randomness_sources))
            if spec.determinism.randomness_sources else None
        ),
        'replay_guarantee': spec.determinism.replay_guarantee,
    }
    
    # Numeric contract (required) - Tier-0: Complete precision identity
    # Include all numeric determinism declarations to prevent false equivalence
    identity['numeric_contract'] = {
        'allows_floating_point': spec.allows_floating_point,
        'uses_floating_point': spec.determinism.uses_floating_point,
        'numerical_tolerance': spec.determinism.numerical_tolerance,
        'determinism_level': spec.determinism.level.name,
    }
    
    # Parameters (if any)
    if spec.parameters:
        identity['parameters'] = _canonicalize_value(spec.parameters)
    
    return identity


def _canonicalize_value(value: Any) -> Any:
    """
    Canonicalize value for hashing.
    
    Handles:
    - FrozenMapping → dict
    - Nested structures
    - Recursive canonicalization
    - Tier-0: Validates to_dict() results are deterministic
    
    Args:
        value: Value to canonicalize
        
    Returns:
        Canonicalized value (pure Python primitives)
        
    Raises:
        TypeError: If to_dict() returns non-canonicalizable structure
    """
    if hasattr(value, 'to_dict'):
        # FrozenMapping or similar
        # Tier-0: Validate to_dict() returns canonicalizable primitives
        canonical = value.to_dict()
        if not isinstance(canonical, (dict, list, str, int, bool, type(None), float)):
            raise TypeError(
                f"to_dict() must return deterministic primitive structures "
                f"(dict/list/primitive), got {type(canonical)}"
            )
        # Recursively validate and canonicalize
        _validate_serializable(canonical)
        return _canonicalize_value(canonical)
    elif isinstance(value, dict):
        # Sort keys for deterministic ordering
        return {
            k: _canonicalize_value(v)
            for k, v in sorted(value.items())
        }
    elif isinstance(value, (list, tuple)):
        # Lists: preserve order (order may be semantically meaningful)
        # Only sort if explicitly marked as order-insensitive
        return [_canonicalize_value(item) for item in value]
    else:
        return value


# ============================================================================
# COMPUTATION HASHING
# ============================================================================

def compute_computation_hash(
    spec: ComputationSpec,
    window_registry: Optional[Any] = None
) -> str:
    """
    Compute SHA-256 hash of computation spec.
    
    ALGORITHM:
    1. Extract identity material (with Tier-0 validation)
    2. Canonicalize to deterministic dict
    3. Serialize to canonical JSON
    4. Hash with SHA-256
    5. Return lowercase hex
    
    No retries.
    No error masking.
    Exceptions propagate.
    
    TIER-0 INVARIANT:
    If spec has windows, window_registry MUST be provided.
    Identity validity cannot be environment-dependent.
    
    Args:
        spec: ComputationSpec to hash
        window_registry: Window registry for authoritative window validation.
                        REQUIRED if spec has windows (required_windows or window_dependency).
                        Must be None only if spec has no window dependencies.
        
    Returns:
        64-character lowercase hex SHA-256 hash
        
    Raises:
        TypeError: Non-serializable value in identity, invalid version type
        ValueError: Ambiguous value, missing required field, invalid version salt, unregistered window,
                    or missing window_registry when windows are present
    """
    # STEP 1: Extract identity material (with Tier-0 validation)
    identity = extract_identity_material(spec, window_registry=window_registry)
    
    # STEP 2: Already canonicalized by extractor
    
    # STEP 3: Serialize to canonical JSON
    payload = canonical_json(identity)
    
    # STEP 4: Hash with SHA-256
    hash_digest = hashlib.sha256(payload).digest()
    
    # STEP 5: Return lowercase hex
    return hash_digest.hex()


def canonicalize_identity(
    spec: ComputationSpec,
    window_registry: Optional[Any] = None
) -> bytes:
    """
    Canonicalize computation identity to bytes.
    
    Used for:
    - Spec fingerprinting
    - Collision detection
    - Debugging hash differences
    
    TIER-0 INVARIANT:
    If spec has windows, window_registry MUST be provided.
    Identity validity cannot be environment-dependent.
    
    Args:
        spec: ComputationSpec to canonicalize
        window_registry: Window registry for authoritative window validation.
                        REQUIRED if spec has windows (required_windows or window_dependency).
                        Must be None only if spec has no window dependencies.
        
    Returns:
        Canonical UTF-8 JSON bytes
        
    Raises:
        TypeError: Non-serializable value, invalid version type
        ValueError: Ambiguous or invalid value, missing version salt, unregistered window,
                    or missing window_registry when windows are present
    """
    identity = extract_identity_material(spec, window_registry=window_registry)
    return canonical_json(identity)
