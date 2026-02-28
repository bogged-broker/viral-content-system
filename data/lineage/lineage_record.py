"""
/data/lineage/lineage_record.py

Atomic Transformation Record Authority
Deterministic · Immutable · Replay-Stable
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional, Tuple

from lineage_types import (
    ArtifactID,
    ArtifactType,
    LineageNodeID,
    MigrationID,
    SchemaVersionID,
    TransformationType,
)

__all__ = ["LineageRecord"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _canonical_json(mapping: dict) -> str:
    """
    Produce a deterministic, locale-independent, sorted-key JSON string.
    No floats. No optional omission. All None values serialised explicitly.
    """
    return json.dumps(mapping, sort_keys=True, ensure_ascii=True, allow_nan=False)


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _deterministic_hash(value: str) -> int:
    """
    Deterministic hash function for use in __hash__ methods.
    Uses first 8 bytes of SHA-256, converted to signed int.
    Guaranteed identical across runs, machines, and environments.
    """
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    # Use first 8 bytes, convert to signed int (Python's hash() range)
    hash_int = int.from_bytes(digest[:8], "big", signed=False)
    # Convert to signed int range (Python's hash typically returns signed)
    # Use modulo to fit in reasonable range, but keep it deterministic
    max_signed_32 = 2**31 - 1
    return hash_int % max_signed_32


def _validate_payload_hash(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(
            f"transformation_payload_hash must be a 64-char SHA-256 hex string, got {value!r}"
        )
    if not all(c in "0123456789abcdef" for c in value):
        raise ValueError(
            f"transformation_payload_hash contains non-hex characters: {value!r}"
        )


# ---------------------------------------------------------------------------
# LineageRecord
# ---------------------------------------------------------------------------

class LineageRecord:
    """
    The immutable, replay-safe, cryptographically-identifiable atomic unit
    of data provenance.

    Represents exactly one transformation event:
      one or more parent artifacts → exactly one output artifact
      under a declared schema version and transformation category.

    Invariants enforced at construction time; no mutation possible thereafter.
    Identical field sets → identical instances, always, everywhere.
    """

    __slots__ = (
        "lineage_node_id",
        "output_artifact_id",
        "input_artifact_ids",
        "artifact_type",
        "transformation_type",
        "input_schema_version",
        "output_schema_version",
        "migration_id",
        "transformation_payload_hash",
        "logical_timestamp",
    )

    # -- construction --------------------------------------------------------

    def __init__(
        self,
        *,
        output_artifact_id: ArtifactID,
        input_artifact_ids: tuple[ArtifactID, ...],
        artifact_type: ArtifactType,
        transformation_type: TransformationType,
        input_schema_version: SchemaVersionID,
        output_schema_version: SchemaVersionID,
        transformation_payload_hash: str,
        logical_timestamp: Optional[int] = None,
        migration_id: Optional[MigrationID] = None,
    ) -> None:
        # --- type guards ----------------------------------------------------
        if not isinstance(output_artifact_id, ArtifactID):
            raise TypeError(f"output_artifact_id must be ArtifactID, got {type(output_artifact_id)!r}")
        if not isinstance(input_artifact_ids, tuple):
            raise TypeError("input_artifact_ids must be a tuple")
        if not all(isinstance(a, ArtifactID) for a in input_artifact_ids):
            raise TypeError("Every element of input_artifact_ids must be an ArtifactID")
        if not isinstance(artifact_type, ArtifactType):
            raise TypeError(f"artifact_type must be ArtifactType, got {type(artifact_type)!r}")
        if not isinstance(transformation_type, TransformationType):
            raise TypeError(f"transformation_type must be TransformationType, got {type(transformation_type)!r}")
        if not isinstance(input_schema_version, SchemaVersionID):
            raise TypeError(f"input_schema_version must be SchemaVersionID, got {type(input_schema_version)!r}")
        if not isinstance(output_schema_version, SchemaVersionID):
            raise TypeError(f"output_schema_version must be SchemaVersionID, got {type(output_schema_version)!r}")
        if migration_id is not None and not isinstance(migration_id, MigrationID):
            raise TypeError(f"migration_id must be MigrationID or None, got {type(migration_id)!r}")
        if logical_timestamp is not None and (not isinstance(logical_timestamp, int) or isinstance(logical_timestamp, bool)):
            raise TypeError(f"logical_timestamp must be int or None, got {type(logical_timestamp)!r}")

        # --- value guards ---------------------------------------------------
        _validate_payload_hash(transformation_payload_hash)

        # Tier-0 governance: logical_timestamp must be None at construction.
        # It is assigned exclusively by lineage_store during append.
        if logical_timestamp is not None:
            raise ValueError(
                f"logical_timestamp must be None at construction (assigned by lineage_store), "
                f"got {logical_timestamp!r}"
            )

        # Deduplicate check on inputs (sorted canonical set)
        if len(input_artifact_ids) != len(set(input_artifact_ids)):
            raise ValueError("input_artifact_ids contains duplicates")

        # Output must not appear as its own parent
        if output_artifact_id in input_artifact_ids:
            raise ValueError(
                f"output_artifact_id {output_artifact_id!r} must not appear in input_artifact_ids"
            )

        # Migration consistency
        if transformation_type is TransformationType.MIGRATION:
            if migration_id is None:
                raise ValueError("MIGRATION transformation requires a migration_id")
            if input_schema_version == output_schema_version:
                raise ValueError(
                    "MIGRATION transformation requires input_schema_version != output_schema_version"
                )
        else:
            if migration_id is not None:
                raise ValueError(
                    f"migration_id must be None for non-MIGRATION transformation, got {migration_id!r}"
                )
            if input_schema_version != output_schema_version:
                raise ValueError(
                    f"Non-MIGRATION transformation requires identical schema versions; "
                    f"got {input_schema_version!r} → {output_schema_version!r}"
                )

        # Canonically sort inputs for determinism
        canonical_inputs: tuple[ArtifactID, ...] = tuple(
            sorted(input_artifact_ids, key=lambda a: a.to_string())
        )

        # --- assign fields first (needed for canonical_json derivation) -----
        object.__setattr__(self, "output_artifact_id",           output_artifact_id)
        object.__setattr__(self, "input_artifact_ids",           canonical_inputs)
        object.__setattr__(self, "artifact_type",                artifact_type)
        object.__setattr__(self, "transformation_type",          transformation_type)
        object.__setattr__(self, "input_schema_version",         input_schema_version)
        object.__setattr__(self, "output_schema_version",        output_schema_version)
        object.__setattr__(self, "migration_id",                 migration_id)
        object.__setattr__(self, "transformation_payload_hash",  transformation_payload_hash)
        # logical_timestamp is assigned by lineage_store, not at construction
        object.__setattr__(self, "logical_timestamp", None)

        # Derive lineage_node_id from canonical JSON hash (Tier-0 requirement).
        # This ensures node_id is the single canonical identity authority,
        # derived directly from the serialized record content (excluding logical_timestamp
        # which is store-assigned and not part of the deterministic identity).
        # We build the dict without node_id and logical_timestamp, then hash it to derive node_id.
        content_dict = self._to_dict_without_node_id()
        canonical_json_str = _canonical_json(content_dict)
        content_hash_hex = _sha256_hex(canonical_json_str)
        final_node_id = LineageNodeID(f"lnid:{content_hash_hex}")

        # Now assign the derived node_id
        object.__setattr__(self, "lineage_node_id", final_node_id)

    # -- immutability --------------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise TypeError(f"LineageRecord is immutable — cannot set {name!r}")

    def __delattr__(self, name: str) -> None:  # type: ignore[override]
        raise TypeError(f"LineageRecord is immutable — cannot delete {name!r}")

    # -- serialisation -------------------------------------------------------

    def _to_dict_without_node_id(self) -> dict:
        """
        Produces a dict with all fields except lineage_node_id and logical_timestamp.
        Used for canonical node_id derivation — node_id is derived from this dict's hash.
        logical_timestamp is excluded because it is assigned by lineage_store, not part of
        the deterministic identity of the transformation event.
        """
        return {
            "output_artifact_id":          self.output_artifact_id.to_string(),
            "input_artifact_ids":          [a.to_string() for a in self.input_artifact_ids],
            "artifact_type":               self.artifact_type.to_string(),
            "transformation_type":         self.transformation_type.to_string(),
            "input_schema_version":        self.input_schema_version.to_string(),
            "output_schema_version":       self.output_schema_version.to_string(),
            "migration_id":                self.migration_id.to_string() if self.migration_id is not None else None,
            "transformation_payload_hash": self.transformation_payload_hash,
        }

    def to_dict(self) -> dict:
        """
        Produces a fully-explicit, deterministic dict.
        None values are preserved — no omissions.
        """
        return {
            "lineage_node_id":             self.lineage_node_id.to_string(),
            "output_artifact_id":          self.output_artifact_id.to_string(),
            "input_artifact_ids":          [a.to_string() for a in self.input_artifact_ids],
            "artifact_type":               self.artifact_type.to_string(),
            "transformation_type":         self.transformation_type.to_string(),
            "input_schema_version":        self.input_schema_version.to_string(),
            "output_schema_version":       self.output_schema_version.to_string(),
            "migration_id":                self.migration_id.to_string() if self.migration_id is not None else None,
            "transformation_payload_hash": self.transformation_payload_hash,
            "logical_timestamp":           int(self.logical_timestamp) if self.logical_timestamp is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LineageRecord":
        """
        Reconstruct a LineageRecord from a previously serialised dict.
        Performs full validation — identical to direct construction.
        Note: logical_timestamp from dict is ignored; it will be None at construction
        and should be assigned by lineage_store during append.
        """
        try:
            migration_id_raw: Optional[str] = data["migration_id"]
            record = cls(
                output_artifact_id=ArtifactID.from_string(data["output_artifact_id"]),
                input_artifact_ids=tuple(
                    ArtifactID.from_string(a) for a in data["input_artifact_ids"]
                ),
                artifact_type=ArtifactType.from_string(data["artifact_type"]),
                transformation_type=TransformationType.from_string(data["transformation_type"]),
                input_schema_version=SchemaVersionID.from_string(data["input_schema_version"]),
                output_schema_version=SchemaVersionID.from_string(data["output_schema_version"]),
                migration_id=MigrationID.from_string(migration_id_raw) if migration_id_raw is not None else None,
                transformation_payload_hash=data["transformation_payload_hash"],
                logical_timestamp=None,  # Always None at construction
            )
            # If the dict had a logical_timestamp, restore it via object.__setattr__
            # (this is for deserialization from stored records)
            if "logical_timestamp" in data and data["logical_timestamp"] is not None:
                object.__setattr__(record, "logical_timestamp", int(data["logical_timestamp"]))
            return record
        except KeyError as exc:
            raise ValueError(f"LineageRecord dict missing required field: {exc}") from exc

    def canonical_json(self) -> str:
        """
        Deterministic, sorted-key JSON representation.
        SHA-256 of this string must match lineage_node_id derivation.
        Excludes logical_timestamp (store-assigned, not part of deterministic identity).
        Stable across runs, environments, locales.
        """
        # Use the same dict structure as node_id derivation (excludes logical_timestamp)
        return _canonical_json(self._to_dict_without_node_id())

    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON. Cross-process integrity fingerprint."""
        return _sha256_hex(self.canonical_json())

    # -- identity & equality -------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LineageRecord):
            return NotImplemented
        # Tier-0 equality: compare full canonical content hash, not just node_id.
        # This ensures equality is based on the complete serialized content,
        # providing stronger guarantees than node_id comparison alone.
        return self.content_hash() == other.content_hash()

    def __hash__(self) -> int:
        # Hash based on content_hash for consistency with __eq__
        return _deterministic_hash(self.content_hash())

    def __lt__(self, other: object) -> bool:
        """Logical ordering by timestamp, then content hash for tie-breaking."""
        if not isinstance(other, LineageRecord):
            return NotImplemented
        # Handle None timestamps (shouldn't happen after store assignment, but defensive)
        self_ts = self.logical_timestamp if self.logical_timestamp is not None else -1
        other_ts = other.logical_timestamp if other.logical_timestamp is not None else -1
        if self_ts != other_ts:
            return self_ts < other_ts
        return self.content_hash() < other.content_hash()

    def __le__(self, other: object) -> bool:
        return self == other or self.__lt__(other)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, LineageRecord):
            return NotImplemented
        return other.__lt__(self)

    def __ge__(self, other: object) -> bool:
        return self == other or self.__gt__(other)

    # -- repr ----------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"LineageRecord("
            f"node={self.lineage_node_id.to_string()!r}, "
            f"output={self.output_artifact_id.to_string()!r}, "
            f"type={self.transformation_type.value!r}, "
            f"ts={self.logical_timestamp!r}"
            f")"
        )