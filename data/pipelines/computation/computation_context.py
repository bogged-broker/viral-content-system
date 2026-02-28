"""
/data/pipelines/computation/computation_context.py

Immutable Computation Execution Context

AUTHORITY: Single source of truth for execution provenance
PRINCIPLE: Facts, not behavior - immutable, hashable, serializable
BEHAVIOR: Once constructed, impossible to alter

This file answers:
> "Exactly what immutable facts define this computation run — such that it can be
  replayed, audited, fingerprinted, and blamed?"

If this context is mutable, implicit, or incomplete:
- Determinism collapses
- Replay becomes probabilistic
- Audit trails lie by omission
- Computation hashing loses meaning

CORE LAW:
A computation_context, once constructed, must be impossible to alter.
If execution changes → new context → new computation identity.

GUARANTEES:
- Execution context cannot lie
- Replay reuses identical authority facts
- Hashing is stable across environments
- Failures are attributable

FORBIDDEN:
- Mutable fields
- Lazy properties
- Runtime discovery logic
- Environment inspection
- Current time access
- Global state references
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Dict
from uuid import UUID
import hashlib
import json


# ============================================================================
# IMMUTABLE MAPPING
# ============================================================================

class FrozenMapping(Mapping[str, str]):
    """
    Immutable, hashable, order-stable mapping for context fields.
    
    Uses deterministic SHA-256 hashing for cross-environment stability.
    """
    
    __slots__ = ('_data', '_sorted_items', '_hash_value')
    
    def __init__(self, data: dict[str, str]) -> None:
        self._data = dict(data)
        self._sorted_items = tuple(sorted(self._data.items()))
        # Compute deterministic hash at construction time
        canonical = json.dumps(dict(self._sorted_items), sort_keys=True, separators=(',', ':'))
        self._hash_value = int(hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16], 16)
    
    def __getitem__(self, key: str) -> str:
        return self._data[key]
    
    def __iter__(self):
        return iter(self._data)
    
    def __len__(self) -> int:
        return len(self._data)
    
    def __hash__(self) -> int:
        """Deterministic hash stable across environments."""
        return self._hash_value
    
    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, (FrozenMapping, dict)):
            return False
        if isinstance(other, FrozenMapping):
            return self._sorted_items == other._sorted_items
        return self._data == other
    
    def __repr__(self) -> str:
        return f"FrozenMapping({dict(self._sorted_items)!r})"
    
    def to_dict(self) -> dict[str, str]:
        """Return regular dict for serialization."""
        return dict(self._sorted_items)


# ============================================================================
# COMPUTATION CONTEXT
# ============================================================================

@dataclass(frozen=True)
class ComputationContext:
    """
    Immutable execution authority for a computation.
    
    Encodes WHO, WHAT, WHEN, WITH WHAT INPUTS, UNDER WHICH INVARIANTS.
    
    PROPERTIES (ABSOLUTE):
    1. Who is allowed to execute (authority)
    2. What is being executed (computation identity)
    3. When it applies (window + temporal anchors)
    4. With what inputs (schemas + lineage)
    5. Under which invariants (versioned laws)
    6. For replay (historical determinism)
    
    IMMUTABILITY:
    - All fields frozen
    - All mappings are FrozenMapping
    - No lazy evaluation
    - No runtime discovery
    - Hashable and equality-comparable
    """
    
    # Computation identity
    computation_hash: str
    computation_version: str
    
    # Temporal window (deterministic facts)
    window_identity: str
    window_start_ts: int
    window_end_ts: int
    
    # Input provenance
    input_schema_hashes: FrozenMapping
    input_fingerprints: FrozenMapping
    
    # Execution tracking
    pipeline_run_id: UUID
    execution_id: UUID
    
    # Invariant authority
    invariant_set_version: str
    
    # Replay flag
    is_replay: bool
    
    def __post_init__(self):
        """Validate invariants on construction."""
        # Validate computation_hash format
        if not self.computation_hash or len(self.computation_hash) != 64:
            raise ValueError(
                f"computation_hash must be 64-character SHA-256 hex string, "
                f"got: {self.computation_hash}"
            )
        
        # Validate version format
        if not self.computation_version:
            raise ValueError("computation_version cannot be empty")
        
        # Validate window identity
        if not self.window_identity:
            raise ValueError("window_identity cannot be empty")
        
        # Validate temporal ordering
        if self.window_start_ts >= self.window_end_ts:
            raise ValueError(
                f"window_start_ts ({self.window_start_ts}) must be strictly less than "
                f"window_end_ts ({self.window_end_ts})"
            )
        
        # Validate input schema hashes exist
        if not isinstance(self.input_schema_hashes, FrozenMapping):
            raise TypeError("input_schema_hashes must be FrozenMapping")
        
        # Validate input fingerprints exist
        if not isinstance(self.input_fingerprints, FrozenMapping):
            raise TypeError("input_fingerprints must be FrozenMapping")
        
        # Validate schema hashes and fingerprints have same keys
        schema_keys = set(self.input_schema_hashes.keys())
        fingerprint_keys = set(self.input_fingerprints.keys())
        
        if schema_keys != fingerprint_keys:
            raise ValueError(
                f"input_schema_hashes and input_fingerprints must have identical keys. "
                f"Schema keys: {schema_keys}, Fingerprint keys: {fingerprint_keys}"
            )
        
        # Validate invariant set version
        if not self.invariant_set_version:
            raise ValueError("invariant_set_version cannot be empty")
        
        # Validate UUIDs
        if not isinstance(self.pipeline_run_id, UUID):
            raise TypeError(f"pipeline_run_id must be UUID, got {type(self.pipeline_run_id)}")
        
        if not isinstance(self.execution_id, UUID):
            raise TypeError(f"execution_id must be UUID, got {type(self.execution_id)}")
    
    def __hash__(self) -> int:
        """
        Deterministic hash stable across environments.
        
        Uses SHA-256 to ensure cross-process and cross-environment stability.
        Includes ALL fields for full structural equality.
        """
        data = {
            'computation_hash': self.computation_hash,
            'computation_version': self.computation_version,
            'window_identity': self.window_identity,
            'window_start_ts': self.window_start_ts,
            'window_end_ts': self.window_end_ts,
            'input_schema_hashes': self.input_schema_hashes.to_dict(),
            'input_fingerprints': self.input_fingerprints.to_dict(),
            'pipeline_run_id': str(self.pipeline_run_id),
            'execution_id': str(self.execution_id),
            'invariant_set_version': self.invariant_set_version,
            'is_replay': self.is_replay,
        }
        
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        hash_hex = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        # Convert first 16 hex chars to int for Python hash compatibility
        return int(hash_hex[:16], 16)
    
    def __eq__(self, other: Any) -> bool:
        """
        Full structural equality - all fields must match exactly.
        
        Two contexts are equal iff ALL fields match exactly.
        This is required for proper dictionary key behavior and audit comparisons.
        """
        if not isinstance(other, ComputationContext):
            return False
        
        return (
            self.computation_hash == other.computation_hash
            and self.computation_version == other.computation_version
            and self.window_identity == other.window_identity
            and self.window_start_ts == other.window_start_ts
            and self.window_end_ts == other.window_end_ts
            and self.input_schema_hashes == other.input_schema_hashes
            and self.input_fingerprints == other.input_fingerprints
            and self.pipeline_run_id == other.pipeline_run_id
            and self.execution_id == other.execution_id
            and self.invariant_set_version == other.invariant_set_version
            and self.is_replay == other.is_replay
        )
    
    def fingerprint(self) -> str:
        """
        Generate deterministic fingerprint of this context.
        
        Used for:
        - Replay verification
        - Audit correlation
        - Blame attribution
        
        Returns: 64-character SHA-256 hex string
        """
        data = {
            'computation_hash': self.computation_hash,
            'computation_version': self.computation_version,
            'window_identity': self.window_identity,
            'window_start_ts': self.window_start_ts,
            'window_end_ts': self.window_end_ts,
            'input_schema_hashes': self.input_schema_hashes.to_dict(),
            'input_fingerprints': self.input_fingerprints.to_dict(),
            'pipeline_run_id': str(self.pipeline_run_id),
            'execution_id': str(self.execution_id),
            'invariant_set_version': self.invariant_set_version,
            'is_replay': self.is_replay,
        }
        
        canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    def to_dict(self) -> dict[str, Any]:
        """
        Serialize context to dictionary for audit logs and replay manifests.
        
        NO binary blobs. NO closures. NO lambdas.
        """
        return {
            'computation_hash': self.computation_hash,
            'computation_version': self.computation_version,
            'window_identity': self.window_identity,
            'window_start_ts': self.window_start_ts,
            'window_end_ts': self.window_end_ts,
            'input_schema_hashes': self.input_schema_hashes.to_dict(),
            'input_fingerprints': self.input_fingerprints.to_dict(),
            'pipeline_run_id': str(self.pipeline_run_id),
            'execution_id': str(self.execution_id),
            'invariant_set_version': self.invariant_set_version,
            'is_replay': self.is_replay,
            'fingerprint': self.fingerprint(),
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComputationContext:
        """
        Deserialize context from dictionary.
        
        Supports lossless round-trip serialization.
        """
        return cls(
            computation_hash=data['computation_hash'],
            computation_version=data['computation_version'],
            window_identity=data['window_identity'],
            window_start_ts=data['window_start_ts'],
            window_end_ts=data['window_end_ts'],
            input_schema_hashes=FrozenMapping(data['input_schema_hashes']),
            input_fingerprints=FrozenMapping(data['input_fingerprints']),
            pipeline_run_id=UUID(data['pipeline_run_id']),
            execution_id=UUID(data['execution_id']),
            invariant_set_version=data['invariant_set_version'],
            is_replay=data['is_replay'],
        )
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True, indent=2)
    
    @classmethod
    def from_json(cls, json_str: str) -> ComputationContext:
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))
