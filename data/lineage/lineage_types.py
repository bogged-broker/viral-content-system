"""
/data/lineage/lineage_types.py

Deterministic Primitive Type Authority
Hash-Stable · Immutable · Zero Logic · Zero Side Effects
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sha256_hex(*parts: str) -> str:
    """Canonical UTF-8 SHA-256 over ordered parts. Deterministic everywhere."""
    h = hashlib.sha256()
    for p in parts:
        encoded = p.encode("utf-8")
        # Length-prefix each segment to prevent concatenation collisions.
        h.update(len(encoded).to_bytes(4, "big"))
        h.update(encoded)
    return h.hexdigest()


def _deterministic_hash(value: str) -> int:
    """
    Deterministic hash function for use in __hash__ methods.
    Uses first 8 bytes of SHA-256, converted to signed int.
    Guaranteed identical across runs, machines, and environments.
    
    Note: Truncates to 32-bit signed range (2^31 - 1) for Python hash table
    compatibility. This is a performance tradeoff: the underlying SHA-256
    digest provides full cryptographic collision resistance, but the final
    hash value has reduced collision resistance due to range truncation.
    For identity primitives, the string representation itself remains the
    authoritative identity; this hash is only for dict/set membership.
    """
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    # Use first 8 bytes, convert to signed int (Python's hash() range)
    hash_int = int.from_bytes(digest[:8], "big", signed=False)
    # Convert to signed int range (Python's hash typically returns signed)
    # Use modulo to fit in reasonable range, but keep it deterministic
    max_signed_32 = 2**31 - 1
    return hash_int % max_signed_32


def _reject_mutation(self: Any, *_: Any, **__: Any) -> None:  # type: ignore[return]
    raise TypeError(f"{type(self).__name__} is immutable.")


# ---------------------------------------------------------------------------
# 1. ArtifactID
# ---------------------------------------------------------------------------

class ArtifactID(str):
    """
    Unique, deterministic identity of a data artifact.

    Derived exclusively from artifact content — never from time or host.
    Same canonical content → identical ArtifactID, anywhere, always.
    """

    _PREFIX = "aid:"
    _HEX_LEN = 64  # full SHA-256

    __slots__ = ()

    def __new__(cls, value: str) -> "ArtifactID":
        if not isinstance(value, str):
            raise TypeError(f"ArtifactID requires str, got {type(value).__name__!r}")
        if not value.startswith(cls._PREFIX):
            raise ValueError(
                f"ArtifactID must start with {cls._PREFIX!r}, got {value!r}"
            )
        hex_part = value[len(cls._PREFIX):]
        if len(hex_part) != cls._HEX_LEN or not all(c in "0123456789abcdef" for c in hex_part):
            raise ValueError(
                f"ArtifactID hex part must be {cls._HEX_LEN} lowercase hex chars, got {hex_part!r}"
            )
        return super().__new__(cls, value)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_content(cls, canonical_content: bytes) -> "ArtifactID":
        """Derive ArtifactID from canonical artifact bytes."""
        if not isinstance(canonical_content, bytes):
            raise TypeError("canonical_content must be bytes")
        hex_digest = hashlib.sha256(canonical_content).hexdigest()
        return cls(f"{cls._PREFIX}{hex_digest}")

    @classmethod
    def from_fields(cls, artifact_type: "ArtifactType", **deterministic_fields: Any) -> "ArtifactID":
        """
        Derive ArtifactID from typed fields.
        Fields are serialised with sorted keys for determinism.
        """
        canonical = json.dumps(
            {"artifact_type": artifact_type.value, **deterministic_fields},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),  # Canonical: no whitespace, cross-version stable
        ).encode("utf-8")
        return cls.from_content(canonical)

    # -- serialisation -------------------------------------------------------

    def to_string(self) -> str:
        return str(self)

    @classmethod
    def from_string(cls, value: str) -> "ArtifactID":
        return cls(value)

    # -- immutability --------------------------------------------------------

    def __setattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __delattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __hash__(self) -> int:  # type: ignore[override]
        return _deterministic_hash(str(self))

    def __repr__(self) -> str:
        return f"ArtifactID({str(self)!r})"


# ---------------------------------------------------------------------------
# 2. LineageNodeID
# ---------------------------------------------------------------------------

class LineageNodeID(str):
    """
    Unique, deterministic identity of a lineage transformation event.

    NOT an artifact identity — encodes a specific transformation in the graph.
    Derived from all causal inputs; stable across replay.
    """

    _PREFIX = "lnid:"
    _HEX_LEN = 64

    __slots__ = ()

    def __new__(cls, value: str) -> "LineageNodeID":
        if not isinstance(value, str):
            raise TypeError(f"LineageNodeID requires str, got {type(value).__name__!r}")
        if not value.startswith(cls._PREFIX):
            raise ValueError(
                f"LineageNodeID must start with {cls._PREFIX!r}, got {value!r}"
            )
        hex_part = value[len(cls._PREFIX):]
        if len(hex_part) != cls._HEX_LEN or not all(c in "0123456789abcdef" for c in hex_part):
            raise ValueError(
                f"LineageNodeID hex part must be {cls._HEX_LEN} lowercase hex chars"
            )
        return super().__new__(cls, value)

    # -- construction --------------------------------------------------------

    @classmethod
    def derive(
        cls,
        *,
        input_artifact_ids: Iterable["ArtifactID"],
        output_artifact_id: "ArtifactID",
        schema_version: "SchemaVersionID",
        transformation_type: "TransformationType",
        migration_id: "MigrationID | None" = None,
    ) -> "LineageNodeID":
        """
        Deterministically derive a LineageNodeID from all causal inputs.
        Order of input_artifact_ids is sorted for stability.
        """
        sorted_inputs = sorted(str(a) for a in input_artifact_ids)
        # Use explicit sentinel to avoid collision with real migration ID "none"
        # Format: "migration:<id>" or "migration:<NULL>"
        # Angle brackets are not in MigrationID charset (a-zA-Z0-9_-), guaranteeing no collision
        migration_part = (
            f"migration:{migration_id.to_string()}"
            if migration_id is not None
            else "migration:<NULL>"
        )
        parts = [
            f"inputs:{','.join(sorted_inputs)}",
            f"output:{output_artifact_id.to_string()}",
            f"schema:{schema_version.to_string()}",
            f"transform:{transformation_type.value}",
            migration_part,
        ]
        hex_digest = _sha256_hex(*parts)
        return cls(f"{cls._PREFIX}{hex_digest}")

    # -- serialisation -------------------------------------------------------

    def to_string(self) -> str:
        return str(self)

    @classmethod
    def from_string(cls, value: str) -> "LineageNodeID":
        return cls(value)

    # -- immutability --------------------------------------------------------

    def __setattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __delattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __hash__(self) -> int:  # type: ignore[override]
        return _deterministic_hash(str(self))

    def __repr__(self) -> str:
        return f"LineageNodeID({str(self)!r})"


# ---------------------------------------------------------------------------
# 3. SchemaVersionID
# ---------------------------------------------------------------------------

class SchemaVersionID(int):
    """
    Monotonically-increasing, positive integer schema version identity.
    Numeric ordering preserved. No floating-point, no semver.
    """

    __slots__ = ()

    _MIN = 1

    def __new__(cls, value: int) -> "SchemaVersionID":
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(
                f"SchemaVersionID requires int, got {type(value).__name__!r}"
            )
        if value < cls._MIN:
            raise ValueError(
                f"SchemaVersionID must be >= {cls._MIN}, got {value!r}"
            )
        return super().__new__(cls, value)

    # -- serialisation -------------------------------------------------------

    def to_string(self) -> str:
        return str(int(self))

    @classmethod
    def from_string(cls, value: str) -> "SchemaVersionID":
        try:
            return cls(int(value))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot parse SchemaVersionID from {value!r}") from exc

    # -- immutability --------------------------------------------------------

    def __setattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __delattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __hash__(self) -> int:  # type: ignore[override]
        # For int subclasses, use deterministic hash of string representation
        return _deterministic_hash(str(int(self)))

    def __repr__(self) -> str:
        return f"SchemaVersionID({int(self)!r})"


# ---------------------------------------------------------------------------
# 4. MigrationID
# ---------------------------------------------------------------------------

_MIGRATION_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_MIGRATION_ID_MIN_LEN = 3
_MIGRATION_ID_MAX_LEN = 128


class MigrationID(str):
    """
    Human-readable, registry-anchored migration operation identity.
    One-to-one with a version transition. No auto-generation.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> "MigrationID":
        if not isinstance(value, str):
            raise TypeError(f"MigrationID requires str, got {type(value).__name__!r}")
        if not (_MIGRATION_ID_MIN_LEN <= len(value) <= _MIGRATION_ID_MAX_LEN):
            raise ValueError(
                f"MigrationID length must be [{_MIGRATION_ID_MIN_LEN}, {_MIGRATION_ID_MAX_LEN}], "
                f"got {len(value)}"
            )
        invalid = set(value) - _MIGRATION_ID_CHARS
        if invalid:
            raise ValueError(
                f"MigrationID contains forbidden characters {invalid!r} in {value!r}"
            )
        return super().__new__(cls, value)

    # -- serialisation -------------------------------------------------------

    def to_string(self) -> str:
        return str(self)

    @classmethod
    def from_string(cls, value: str) -> "MigrationID":
        return cls(value)

    # -- immutability --------------------------------------------------------

    def __setattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __delattr__(self, *a: Any) -> None:  # type: ignore[override]
        _reject_mutation(self)

    def __hash__(self) -> int:  # type: ignore[override]
        return _deterministic_hash(str(self))

    def __repr__(self) -> str:
        return f"MigrationID({str(self)!r})"


# ---------------------------------------------------------------------------
# 5. ArtifactType
# ---------------------------------------------------------------------------

class ArtifactType(str, Enum):
    """
    Exhaustive, centrally-declared enumeration of artifact families.
    No dynamic registration permitted.
    """

    CANONICAL_CONTENT    = "CANONICAL_CONTENT"
    CANONICAL_FACT       = "CANONICAL_FACT"
    AGGREGATE_WINDOW     = "AGGREGATE_WINDOW"
    EXPERIMENT_STATE     = "EXPERIMENT_STATE"
    ACCOUNT_IDENTITY     = "ACCOUNT_IDENTITY"
    MIGRATION_SNAPSHOT   = "MIGRATION_SNAPSHOT"
    RECOVERY_SUBGRAPH    = "RECOVERY_SUBGRAPH"

    # Prevent accidental .value mutation on str-Enum
    def __setattr__(self, name: str, value: Any) -> None:  # type: ignore[override]
        if name in ("_value_", "_name_", "_member_map_"):
            super().__setattr__(name, value)
        else:
            _reject_mutation(self)

    def to_string(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, value: str) -> "ArtifactType":
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"Unknown ArtifactType: {value!r}")


# ---------------------------------------------------------------------------
# 6. TransformationType
# ---------------------------------------------------------------------------

class TransformationType(str, Enum):
    """
    Exhaustive, centrally-declared enumeration of lineage transformation classes.
    Every lineage node carries exactly one TransformationType.
    No runtime additions permitted.
    """

    INGESTION              = "INGESTION"
    CANONICALIZATION       = "CANONICALIZATION"
    AGGREGATION            = "AGGREGATION"
    MIGRATION              = "MIGRATION"
    RECOVERY_REPAIR        = "RECOVERY_REPAIR"
    EXPERIMENT_ALLOCATION  = "EXPERIMENT_ALLOCATION"
    DERIVATION             = "DERIVATION"
    SNAPSHOT_RESTORE       = "SNAPSHOT_RESTORE"

    def __setattr__(self, name: str, value: Any) -> None:  # type: ignore[override]
        if name in ("_value_", "_name_", "_member_map_"):
            super().__setattr__(name, value)
        else:
            _reject_mutation(self)

    def to_string(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, value: str) -> "TransformationType":
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"Unknown TransformationType: {value!r}")


# ---------------------------------------------------------------------------
# Module-level guard: zero side effects, no IO, no env reads
# All six public symbols explicitly declared.
# ---------------------------------------------------------------------------

__all__ = [
    "ArtifactID",
    "LineageNodeID",
    "SchemaVersionID",
    "MigrationID",
    "ArtifactType",
    "TransformationType",
]