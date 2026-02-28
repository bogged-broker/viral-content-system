"""
/data/pipelines/windows/window_identity.py

Canonical Window Identity Authority (Deterministic, Replay-Safe, Cryptographically Stable)

What This File Exists For (NON-NEGOTIABLE):
  window_identity.py is the single authority that defines what a window is at the identity level.
  It answers exactly one question:
  "How do we name a window such that its identity is globally stable, replay-safe, 
  collision-resistant, and immutable forever?"

AUTHORITY: A window is a fact. Facts must have immutable names.

If this file lies, aggregation becomes unreplayable, audits fail, and historical truth fractures.
Wrong window identity is worse than wrong math.

Design Principle (CRITICAL):
  > A window is a fact. Facts must have immutable names.

Identity must survive:
  - replays
  - migrations
  - refactors
  - cluster changes
  - code reordering
  - time

If the same logical window ever gets a different ID, the system is broken.

Core Responsibilities (NON-NEGOTIABLE):
  1. Define the canonical serialization of window identity inputs
  2. Define the stable hashing / ID derivation algorithm
  3. Guarantee collision resistance at system scale
  4. Guarantee byte-identical identity on replay
  5. Encode window definition + boundaries + version
  6. Be independent of runtime state and clocks
  7. Produce human-inspectable identities
  8. Reject ambiguous or incomplete identity material

If identity inputs are uncertain → fail hard.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List, Union
from enum import Enum

from .window_models import WindowType


class IdentityFormatVersion(str, Enum):
    V1 = "v1"
    V2 = "v2"


# Tier-0 Authority: Whitelist of window types supported by identity layer
# This enforces closed-set determinism and catches spec mismatches.
# If WindowType enum has types not in this set, identity creation will fail.
ALLOWED_WINDOW_TYPES_FOR_IDENTITY = frozenset([
    WindowType.TUMBLING_TIME,
    WindowType.SLIDING_TIME,
    WindowType.SESSION,
    WindowType.FIXED_EVENT,
    WindowType.LIFETIME,
    # Note: HOPPING_TIME and GLOBAL are in WindowType enum but may not be
    # in the authoritative identity spec. Add them here if they should be supported.
    # For now, only the 5 core types are whitelisted for Tier-0 purity.
])


@dataclass(frozen=True)
class WindowIdentityMaterial:
    """
    Pure value object representing everything identity depends on.
    
    RULES:
    - Fully populated (no optional fields for identity computation)
    - Immutable
    - Comparable
    - Deterministic
    - Already policy-validated upstream
    - Window type is enum-closed (no stringly-typed drift)
    
    If material is incomplete, identity computation must not proceed.
    """
    window_type: WindowType
    window_start_ms: int
    window_end_ms: int
    alignment_epoch_ms: int
    window_definition_version: str
    identity_format_version: IdentityFormatVersion
    session_gap_ms: Optional[int] = None
    hop_size_ms: Optional[int] = None
    aggregation_context: Dict[str, Any]  # REQUIRED: Always present, never optional. Use {} if no context.
    
    def __post_init__(self):
        if self.window_start_ms < 0:
            raise ValueError(f"window_start_ms must be non-negative: {self.window_start_ms}")
        if self.window_end_ms < 0:
            raise ValueError(f"window_end_ms must be non-negative: {self.window_end_ms}")
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError(
                f"window_end_ms ({self.window_end_ms}) must be > window_start_ms ({self.window_start_ms})"
            )
        if self.alignment_epoch_ms < 0:
            raise ValueError(f"alignment_epoch_ms must be non-negative: {self.alignment_epoch_ms}")
        if not isinstance(self.window_type, WindowType):
            raise ValueError(f"window_type must be WindowType enum, got {type(self.window_type)}")
        if not self.window_definition_version:
            raise ValueError("window_definition_version must be non-empty")
        if not isinstance(self.identity_format_version, IdentityFormatVersion):
            raise ValueError(f"identity_format_version must be IdentityFormatVersion enum, got {type(self.identity_format_version)}")
        
        # Tier-0: Validate window type is in whitelist (closed-set enforcement)
        if self.window_type not in ALLOWED_WINDOW_TYPES_FOR_IDENTITY:
            raise ValueError(
                f"window_type {self.window_type.value} is not in allowed identity types: "
                f"{[wt.value for wt in ALLOWED_WINDOW_TYPES_FOR_IDENTITY]}. "
                "This may indicate a spec mismatch between WindowType enum and identity authority."
            )
        
        # Structural completeness: required fields per window type
        if self.window_type == WindowType.SESSION:
            if self.session_gap_ms is None:
                raise ValueError("SESSION windows require session_gap_ms (structural requirement)")
        if self.window_type == WindowType.HOPPING_TIME:
            if self.hop_size_ms is None:
                raise ValueError("HOPPING_TIME windows require hop_size_ms (structural requirement)")
        
        if self.session_gap_ms is not None and self.session_gap_ms <= 0:
            raise ValueError(f"session_gap_ms must be positive: {self.session_gap_ms}")
        if self.hop_size_ms is not None and self.hop_size_ms <= 0:
            raise ValueError(f"hop_size_ms must be positive: {self.hop_size_ms}")
        
        # Tier-0: aggregation_context is REQUIRED (never optional) to ensure structural consistency.
        # Identity schema must always be the same shape - no optional field omission.
        # Callers MUST provide aggregation_context explicitly (use {} if no context).
        if not isinstance(self.aggregation_context, dict):
            raise ValueError(
                f"aggregation_context is REQUIRED and must be a dict, got {type(self.aggregation_context)}. "
                "Use {} if no context is needed."
            )


@dataclass(frozen=True)
class WindowIdentity:
    """
    Final identity object handed to aggregation.
    
    RULES:
    - window_id is opaque, immutable
    - Identity object is read-only
    - Used downstream verbatim
    - Never recomputed after emission
    - Window type is enum-closed (no stringly-typed drift)
    - No debug artifacts (fingerprint is derivable, not stored)
    
    Once emitted, identity is eternal.
    
    NOTE: identity_material_fingerprint is removed for Tier-0 purity.
    Fingerprints are derivable via WindowIdentityHasher.compute_fingerprint()
    and should not be persisted as part of identity.
    """
    window_id: str
    window_type: WindowType
    window_start_ms: int
    window_end_ms: int
    identity_format_version: IdentityFormatVersion
    window_definition_version: str
    
    def __post_init__(self):
        if not self.window_id:
            raise ValueError("window_id must be non-empty")
        if len(self.window_id) != 64:
            raise ValueError(f"window_id must be 64 hex characters: {len(self.window_id)}")
        if not all(c in '0123456789abcdef' for c in self.window_id):
            raise ValueError("window_id must be lowercase hex")


class WindowIdentitySerializer:
    """
    Responsible for converting identity material into canonical serialized form.
    
    CANONICALIZATION RULES (ABSOLUTE):
    - Field order is lexicographically fixed (via sort_keys=True)
    - Integers serialized as base-10 strings (JSON default)
    - No whitespace (separators=(',', ':'))
    - No locale effects (ensure_ascii=True)
    - UTF-8 encoding only
    - Explicit field names included
    - No implicit defaults
    
    Serialization is identity, not transport.
    
    Example Serialized Form:
    {
      "alignment_epoch_ms": 0,
      "identity_format_version": "v1",
      "window_definition_version": "2026-01",
      "window_end_ms": 1700003600000,
      "window_start_ms": 1700000000000,
      "window_type": "tumbling_time"
    }
    
    Rules:
    - Stable JSON (no reordering)
    - No floating points
    - No timestamps other than epoch ms
    """
    
    @staticmethod
    def serialize(material: WindowIdentityMaterial) -> bytes:
        """
        Convert WindowIdentityMaterial to canonical byte representation.
        
        Returns:
            Deterministic byte sequence representing the identity material.
        
        Raises:
            ValueError: If material is incomplete or invalid.
        """
        canonical_dict = {
            "alignment_epoch_ms": material.alignment_epoch_ms,
            "identity_format_version": material.identity_format_version.value,
            "window_definition_version": material.window_definition_version,
            "window_end_ms": material.window_end_ms,
            "window_start_ms": material.window_start_ms,
            "window_type": material.window_type.value,
        }
        
        if material.session_gap_ms is not None:
            canonical_dict["session_gap_ms"] = material.session_gap_ms
        
        if material.hop_size_ms is not None:
            canonical_dict["hop_size_ms"] = material.hop_size_ms
        
        # Tier-0: aggregation_context is always present (empty dict if no context)
        # This ensures structural consistency - always serialize, never omit.
        canonical_dict["aggregation_context"] = WindowIdentitySerializer._canonicalize_context(
            material.aggregation_context
        )
        
        canonical_json = json.dumps(
            canonical_dict,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        
        return canonical_json.encode('utf-8')
    
    @staticmethod
    def _canonicalize_context(context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recursively canonicalize aggregation context.
        
        Ensures deterministic ordering at all nesting levels.
        Handles:
        - Dicts: sorted keys, recursive canonicalization
        - Lists: sorted if order-independent, otherwise preserved
        - Floats: converted to strings with fixed precision (IEEE 754 deterministic)
        - Nested structures: fully canonicalized
        
        CRITICAL: This must produce identical bytes across all Python versions,
        languages, and platforms for replay determinism.
        """
        if isinstance(context, dict):
            return {
                k: WindowIdentitySerializer._canonicalize_context(v)
                for k, v in sorted(context.items())
            }
        elif isinstance(context, list):
            # Tier-0: Lists MUST be canonically ordered for cross-language determinism.
            # Identity layer enforces ordering policy - callers cannot delegate this responsibility.
            # All lists are sorted recursively using canonical JSON representation as sort key.
            # This ensures identical semantic content produces identical serialization regardless of:
            # - Insertion order
            # - Language/runtime differences
            # - Team implementation variations
            canonicalized_items = [WindowIdentitySerializer._canonicalize_context(item) for item in context]
            
            # Sort using canonical JSON representation for stable, cross-language ordering
            # This handles mixed types (dicts, lists, primitives) deterministically
            def canonical_sort_key(item: Any) -> str:
                """Generate canonical sort key for any JSON-serializable item."""
                try:
                    return json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                except (TypeError, ValueError) as e:
                    raise ValueError(
                        f"List items in aggregation_context must be JSON-serializable for canonical ordering. "
                        f"Item {item} (type {type(item)}) cannot be serialized: {e}"
                    )
            
            return sorted(canonicalized_items, key=canonical_sort_key)
        elif isinstance(context, float):
            # Floats are problematic for cross-language determinism.
            # Convert to string with fixed precision to avoid IEEE 754 variations.
            # Use repr() which gives full precision, but note: this may still vary.
            # For Tier-0, we should reject floats entirely or use decimal strings.
            raise ValueError(
                f"Floats in aggregation_context violate cross-language determinism: {context}. "
                "Use integers or decimal strings instead."
            )
        else:
            return context
    
    @staticmethod
    def deserialize_material(canonical_bytes: bytes) -> WindowIdentityMaterial:
        """
        Reconstruct WindowIdentityMaterial from canonical bytes.
        
        Used for cross-process verification and replay validation.
        """
        canonical_str = canonical_bytes.decode('utf-8')
        data = json.loads(canonical_str)
        
        # Validate and convert enum types
        window_type_str = data["window_type"]
        try:
            window_type = WindowType(window_type_str)
        except ValueError:
            raise ValueError(f"Invalid window_type: {window_type_str}. Must be a valid WindowType enum value.")
        
        identity_format_version_str = data["identity_format_version"]
        try:
            identity_format_version = IdentityFormatVersion(identity_format_version_str)
        except ValueError:
            raise ValueError(f"Invalid identity_format_version: {identity_format_version_str}. Must be a valid IdentityFormatVersion enum value.")
        
        return WindowIdentityMaterial(
            window_type=window_type,
            window_start_ms=data["window_start_ms"],
            window_end_ms=data["window_end_ms"],
            alignment_epoch_ms=data["alignment_epoch_ms"],
            window_definition_version=data["window_definition_version"],
            identity_format_version=identity_format_version,
            session_gap_ms=data.get("session_gap_ms"),
            hop_size_ms=data.get("hop_size_ms"),
            aggregation_context=data.get("aggregation_context", {}),  # REQUIRED field - default to {} for backward compat
        )


class WindowIdentityHasher:
    """
    Responsible for deriving the final ID.
    
    HASHING RULES (ABSOLUTE):
    - SHA-256 (or stronger)
    - Hash only canonical serialized bytes
    - Hex encoding (fixed system-wide, lowercase)
    - No salts
    - No per-deployment variation
    - No truncation (full 64 hex characters)
    
    Example:
        hash = sha256(canonical_bytes).hexdigest()
    
    The hash is the identity. No post-processing allowed.
    """
    
    HASH_ALGORITHM = "sha256"
    HASH_LENGTH_HEX = 64  # SHA-256 produces 64 hex characters
    
    @staticmethod
    def compute_hash(canonical_bytes: bytes) -> str:
        """
        Derive cryptographically stable window ID from canonical bytes.
        
        This is the single authority for window ID computation.
        No other code path may compute window IDs.
        
        Args:
            canonical_bytes: Canonical serialized identity material.
        
        Returns:
            64-character lowercase hex string (SHA-256 digest).
        
        Raises:
            ValueError: If canonical_bytes is empty or invalid.
        """
        if not canonical_bytes:
            raise ValueError("Cannot hash empty canonical bytes")
        
        if not isinstance(canonical_bytes, bytes):
            raise ValueError(f"canonical_bytes must be bytes, got {type(canonical_bytes)}")
        
        hasher = hashlib.sha256()
        hasher.update(canonical_bytes)
        window_id = hasher.hexdigest()
        
        # Enforce invariant: full hash, no truncation
        if len(window_id) != WindowIdentityHasher.HASH_LENGTH_HEX:
            raise ValueError(
                f"Hash length mismatch: expected {WindowIdentityHasher.HASH_LENGTH_HEX}, "
                f"got {len(window_id)}"
            )
        
        return window_id
    
    @staticmethod
    def compute_fingerprint(canonical_bytes: bytes) -> str:
        """
        Compute truncated fingerprint for human inspection.
        
        WARNING: Fingerprints are for debugging only, never for identity.
        """
        full_hash = WindowIdentityHasher.compute_hash(canonical_bytes)
        return full_hash[:16]


class WindowIdentityInvariants:
    """
    Enforces absolute invariants on window identity.
    
    MUST enforce:
    - No identity without full material
    - No mutable identity components
    - No hash recomputation with differing inputs
    - No dynamic fields (clock, node, env)
    - No collisions detectable within system bounds
    - No truncation
    - No human-chosen IDs
    
    Violation → hard failure.
    """
    
    @staticmethod
    def validate_material(material: WindowIdentityMaterial) -> None:
        """
        Guard illegal identity material before ID computation.
        
        Enforces:
        - No identity without full material
        - No mutable identity components (enforced by frozen dataclass)
        - No dynamic fields (clock, node, env)
        - Material completeness
        
        Raises:
            ValueError: If material violates invariants.
        """
        # Material completeness is enforced by WindowIdentityMaterial.__post_init__
        # Additional invariant checks:
        
        # Ensure no dynamic/clock-based fields
        # (window_start_ms and window_end_ms are epoch ms, not current time - validated upstream)
        
        # Ensure required fields for window types (enum-based validation)
        if material.window_type == WindowType.SESSION:
            if material.session_gap_ms is None:
                raise ValueError("SESSION windows require session_gap_ms")
        
        if material.window_type == WindowType.HOPPING_TIME:
            if material.hop_size_ms is None:
                raise ValueError("HOPPING_TIME windows require hop_size_ms")
        
        # Ensure aggregation_context is canonicalizable (always present per Tier-0)
        if not isinstance(material.aggregation_context, dict):
            raise ValueError("aggregation_context must be a dict (empty dict if no context)")
        # Ensure it can be serialized deterministically
        try:
            WindowIdentitySerializer._canonicalize_context(material.aggregation_context)
        except Exception as e:
            raise ValueError(f"aggregation_context cannot be canonicalized: {e}")
    
    @staticmethod
    def validate_identity(identity: WindowIdentity) -> None:
        """
        Verify emitted identity satisfies invariants.
        
        Enforces:
        - No truncation (window_id must be full 64 hex chars)
        - No human-chosen IDs (must be hex hash)
        - Format consistency
        - Immutability (enforced by frozen dataclass)
        
        Raises:
            ValueError: If identity is malformed.
        """
        # window_id validation is in WindowIdentity.__post_init__
        # Additional checks:
        
        # Ensure window_id is not truncated
        if len(identity.window_id) != 64:
            raise ValueError(
                f"window_id must be exactly 64 hex characters, got {len(identity.window_id)}"
            )
        
        # Ensure window_id is lowercase hex (no human-readable prefixes)
        if not all(c in '0123456789abcdef' for c in identity.window_id):
            raise ValueError(
                f"window_id must be lowercase hex only, got: {identity.window_id[:16]}..."
            )
        
        # Ensure identity_format_version is valid enum
        if not isinstance(identity.identity_format_version, IdentityFormatVersion):
            raise ValueError(f"identity_format_version must be IdentityFormatVersion enum, got {type(identity.identity_format_version)}")
        
        # Ensure window_definition_version is valid
        if not identity.window_definition_version:
            raise ValueError("window_definition_version must be non-empty")
        
        # Ensure boundaries are valid
        if identity.window_end_ms <= identity.window_start_ms:
            raise ValueError(
                f"window_end_ms ({identity.window_end_ms}) must be > "
                f"window_start_ms ({identity.window_start_ms})"
            )
        
        # Fingerprint validation removed - fingerprints are derivable, not stored
    
    @staticmethod
    def verify_determinism(
        material: WindowIdentityMaterial,
        expected_id: str
    ) -> None:
        """
        Assert that recomputing identity from material yields expected ID.
        
        Used in replay validation and cross-process verification.
        
        Raises:
            AssertionError: If identity is non-deterministic.
        """
        canonical_bytes = WindowIdentitySerializer.serialize(material)
        recomputed_id = WindowIdentityHasher.compute_hash(canonical_bytes)
        
        if recomputed_id != expected_id:
            raise AssertionError(
                f"Identity determinism violated: expected {expected_id}, got {recomputed_id}"
            )
    
    @staticmethod
    def verify_collision_resistance(identities: list[WindowIdentity]) -> None:
        """
        Detect collisions in a set of identities.
        
        Raises:
            AssertionError: If collision detected.
        """
        seen_ids = set()
        for identity in identities:
            if identity.window_id in seen_ids:
                raise AssertionError(f"Collision detected: {identity.window_id}")
            seen_ids.add(identity.window_id)


class WindowIdentityFactory:
    """
    Primary interface for creating window identities.
    
    Coordinates serialization, hashing, and invariant validation.
    
    This is the single authority for identity creation.
    All window identities must be created through this factory.
    
    Determinism Guarantees (REQUIRED):
    1. Replay Determinism: Same inputs → identical window_id bit-for-bit
    2. Cross-Process Determinism: Different nodes → identical identity
    3. Cross-Language Determinism: Reference serializer spec produces same bytes
    4. Version Isolation: Different identity_format_version → distinct IDs
    """
    
    @staticmethod
    def create_identity(material: WindowIdentityMaterial) -> WindowIdentity:
        """
        Create immutable window identity from validated material.
        
        This is the single authority for identity creation.
        
        Args:
            material: Fully populated, validated identity material.
        
        Returns:
            Immutable WindowIdentity object.
        
        Raises:
            ValueError: If material is invalid or incomplete.
        """
        WindowIdentityInvariants.validate_material(material)
        
        canonical_bytes = WindowIdentitySerializer.serialize(material)
        window_id = WindowIdentityHasher.compute_hash(canonical_bytes)
        
        # Fingerprint removed from identity object for Tier-0 purity.
        # Use WindowIdentityHasher.compute_fingerprint(canonical_bytes) if needed for debugging.
        
        identity = WindowIdentity(
            window_id=window_id,
            window_type=material.window_type,
            window_start_ms=material.window_start_ms,
            window_end_ms=material.window_end_ms,
            identity_format_version=material.identity_format_version,
            window_definition_version=material.window_definition_version,
        )
        
        WindowIdentityInvariants.validate_identity(identity)
        
        return identity
    
    @staticmethod
    def recreate_identity_from_canonical(canonical_bytes: bytes) -> WindowIdentity:
        """
        Reconstruct identity from canonical serialized form.
        
        Used in cross-process verification and replay validation.
        
        Args:
            canonical_bytes: Canonical serialized identity material.
        
        Returns:
            Reconstructed WindowIdentity.
        """
        material = WindowIdentitySerializer.deserialize_material(canonical_bytes)
        return WindowIdentityFactory.create_identity(material)
    
    @staticmethod
    def verify_identity_reproducibility(
        identity: WindowIdentity,
        material: WindowIdentityMaterial
    ) -> None:
        """
        Verify that identity can be exactly reproduced from material.
        
        Tier-0 requirement: Fail loudly on non-determinism, never silently return False.
        This is a hard invariant check, not a soft validation.
        
        Raises:
            AssertionError: If identity cannot be reproduced from material.
            ValueError: If material is invalid or identity is malformed.
        """
        recomputed = WindowIdentityFactory.create_identity(material)
        
        if recomputed.window_id != identity.window_id:
            raise AssertionError(
                f"Identity reproducibility violated: window_id mismatch. "
                f"Expected {identity.window_id}, got {recomputed.window_id}"
            )
        
        if recomputed.window_start_ms != identity.window_start_ms:
            raise AssertionError(
                f"Identity reproducibility violated: window_start_ms mismatch. "
                f"Expected {identity.window_start_ms}, got {recomputed.window_start_ms}"
            )
        
        if recomputed.window_end_ms != identity.window_end_ms:
            raise AssertionError(
                f"Identity reproducibility violated: window_end_ms mismatch. "
                f"Expected {identity.window_end_ms}, got {recomputed.window_end_ms}"
            )
        
        if recomputed.identity_format_version != identity.identity_format_version:
            raise AssertionError(
                f"Identity reproducibility violated: identity_format_version mismatch. "
                f"Expected {identity.identity_format_version}, got {recomputed.identity_format_version}"
            )
        
        if recomputed.window_type != identity.window_type:
            raise AssertionError(
                f"Identity reproducibility violated: window_type mismatch. "
                f"Expected {identity.window_type}, got {recomputed.window_type}"
            )


# ============================================================================
# UTILITY CLASSES (NOT CORE IDENTITY LAW - CAN BE MOVED TO SEPARATE MODULE)
# ============================================================================
# 
# TIER-0 AUTHORITY BOUNDARY:
# The core identity law consists ONLY of:
#   1. WindowIdentityMaterial (schema definition)
#   2. WindowIdentitySerializer (canonical serialization)
#   3. WindowIdentityHasher (SHA-256 hash derivation)
#   4. WindowIdentityFactory (single creation authority)
#   5. WindowIdentityInvariants (validation enforcement)
#
# Everything below this line is a convenience utility and NOT part of the
# trusted identity authority. These utilities can be moved to a separate
# module (e.g., window_identity_utils.py) without affecting core identity law.
#
# Core identity law: Material → Canonical Bytes → SHA-256 → ID
# That's it. Nothing more.
# ============================================================================

class WindowIdentityCodec:
    """
    Encoding/decoding utilities for window identities.
    
    NOTE: This is a utility class, not core identity law.
    Supports serialization for persistence, transmission, and auditing.
    """
    
    @staticmethod
    def encode_identity(identity: WindowIdentity) -> Dict[str, Any]:
        """
        Encode identity to dictionary for JSON serialization.
        """
        return {
            "window_id": identity.window_id,
            "window_type": identity.window_type.value,
            "window_start_ms": identity.window_start_ms,
            "window_end_ms": identity.window_end_ms,
            "identity_format_version": identity.identity_format_version.value,
            "window_definition_version": identity.window_definition_version,
        }
    
    @staticmethod
    def decode_identity(encoded: Dict[str, Any]) -> WindowIdentity:
        """
        Decode identity from dictionary representation.
        """
        # Handle backward compatibility: old encoded identities may have fingerprint
        # but we ignore it since it's derivable
        window_type_str = encoded["window_type"]
        try:
            window_type = WindowType(window_type_str) if isinstance(window_type_str, str) else window_type_str
        except (ValueError, TypeError):
            raise ValueError(f"Invalid window_type: {window_type_str}")
        
        identity_format_version_str = encoded["identity_format_version"]
        try:
            identity_format_version = IdentityFormatVersion(identity_format_version_str) if isinstance(identity_format_version_str, str) else identity_format_version_str
        except (ValueError, TypeError):
            raise ValueError(f"Invalid identity_format_version: {identity_format_version_str}")
        
        return WindowIdentity(
            window_id=encoded["window_id"],
            window_type=window_type,
            window_start_ms=encoded["window_start_ms"],
            window_end_ms=encoded["window_end_ms"],
            identity_format_version=identity_format_version,
            window_definition_version=encoded["window_definition_version"],
        )
    
    @staticmethod
    def encode_material(material: WindowIdentityMaterial) -> Dict[str, Any]:
        """
        Encode identity material for transmission or persistence.
        """
        encoded = {
            "window_type": material.window_type.value,
            "window_start_ms": material.window_start_ms,
            "window_end_ms": material.window_end_ms,
            "alignment_epoch_ms": material.alignment_epoch_ms,
            "window_definition_version": material.window_definition_version,
            "identity_format_version": material.identity_format_version.value,
        }
        
        if material.session_gap_ms is not None:
            encoded["session_gap_ms"] = material.session_gap_ms
        if material.hop_size_ms is not None:
            encoded["hop_size_ms"] = material.hop_size_ms
        # aggregation_context is always present (empty dict if no context)
        encoded["aggregation_context"] = material.aggregation_context
        
        return encoded
    
    @staticmethod
    def decode_material(encoded: Dict[str, Any]) -> WindowIdentityMaterial:
        """
        Decode identity material from dictionary representation.
        """
        window_type_str = encoded["window_type"]
        try:
            window_type = WindowType(window_type_str) if isinstance(window_type_str, str) else window_type_str
        except (ValueError, TypeError):
            raise ValueError(f"Invalid window_type: {window_type_str}")
        
        identity_format_version_str = encoded["identity_format_version"]
        try:
            identity_format_version = IdentityFormatVersion(identity_format_version_str) if isinstance(identity_format_version_str, str) else identity_format_version_str
        except (ValueError, TypeError):
            raise ValueError(f"Invalid identity_format_version: {identity_format_version_str}")
        
        return WindowIdentityMaterial(
            window_type=window_type,
            window_start_ms=encoded["window_start_ms"],
            window_end_ms=encoded["window_end_ms"],
            alignment_epoch_ms=encoded["alignment_epoch_ms"],
            window_definition_version=encoded["window_definition_version"],
            identity_format_version=identity_format_version,
            session_gap_ms=encoded.get("session_gap_ms"),
            hop_size_ms=encoded.get("hop_size_ms"),
            aggregation_context=encoded.get("aggregation_context", {}),  # REQUIRED field - default for backward compat
        )


class WindowIdentityComparator:
    """
    Utilities for comparing window identities and detecting conflicts.
    
    NOTE: This is a utility class, not core identity law.
    """
    
    @staticmethod
    def identities_equal(id1: WindowIdentity, id2: WindowIdentity) -> bool:
        """
        Check if two identities represent the same logical window.
        """
        return (
            id1.window_id == id2.window_id and
            id1.window_type == id2.window_type and
            id1.window_start_ms == id2.window_start_ms and
            id1.window_end_ms == id2.window_end_ms
        )
    
    @staticmethod
    def materials_equivalent(m1: WindowIdentityMaterial, m2: WindowIdentityMaterial) -> bool:
        """
        Check if two materials would produce the same identity.
        """
        return WindowIdentitySerializer.serialize(m1) == WindowIdentitySerializer.serialize(m2)
    
    @staticmethod
    def detect_version_conflict(id1: WindowIdentity, id2: WindowIdentity) -> bool:
        """
        Detect if two identities have same boundaries but different versions.
        
        This indicates a potential migration or corruption issue.
        """
        return (
            id1.window_start_ms == id2.window_start_ms and
            id1.window_end_ms == id2.window_end_ms and
            id1.window_type == id2.window_type and
            (
                id1.identity_format_version != id2.identity_format_version or
                id1.window_definition_version != id2.window_definition_version
            )
        )


def create_window_identity(
    window_type: Union[WindowType, str],
    window_start_ms: int,
    window_end_ms: int,
    window_definition_version: str,
    identity_format_version: Union[IdentityFormatVersion, str] = IdentityFormatVersion.V1,
    alignment_epoch_ms: int = 0,
    session_gap_ms: Optional[int] = None,
    hop_size_ms: Optional[int] = None,
    aggregation_context: Optional[Dict[str, Any]] = None,
) -> WindowIdentity:
    """
    Convenience function for creating window identities.
    
    NOTE: This is a convenience utility, not core identity law.
    The core authority is WindowIdentityFactory.create_identity().
    
    This is the primary public interface for identity creation.
    
    Args:
        window_type: WindowType enum or string (converted to enum)
        identity_format_version: IdentityFormatVersion enum or string (converted to enum)
        Other args: As defined in WindowIdentityMaterial
    
    Returns:
        Immutable WindowIdentity object.
    
    Raises:
        ValueError: If window_type or identity_format_version cannot be converted to enum.
    """
    # Convert string inputs to enums for Tier-0 type safety
    if isinstance(window_type, str):
        try:
            window_type = WindowType(window_type)
        except ValueError:
            raise ValueError(f"Invalid window_type string: {window_type}. Must be a valid WindowType enum value.")
    
    if isinstance(identity_format_version, str):
        try:
            identity_format_version = IdentityFormatVersion(identity_format_version)
        except ValueError:
            raise ValueError(f"Invalid identity_format_version string: {identity_format_version}. Must be a valid IdentityFormatVersion enum value.")
    
    # Tier-0: aggregation_context defaults to empty dict, never None
    if aggregation_context is None:
        aggregation_context = {}
    
    material = WindowIdentityMaterial(
        window_type=window_type,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        alignment_epoch_ms=alignment_epoch_ms,
        window_definition_version=window_definition_version,
        identity_format_version=identity_format_version,
        session_gap_ms=session_gap_ms,
        hop_size_ms=hop_size_ms,
        aggregation_context=aggregation_context,
    )
    
    return WindowIdentityFactory.create_identity(material)