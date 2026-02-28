"""
/data/lineage/lineage_store.py

Append-Only, Crash-Safe, Integrity-Verified Persistence Authority
Deterministic · Atomic · Replay-Stable
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import struct
import tempfile
from pathlib import Path
from typing import Dict, Generator, Iterator, Optional, Set, Tuple

from lineage_record import LineageRecord
from lineage_types import ArtifactID, LineageNodeID, SchemaVersionID, TransformationType

__all__ = [
    "LineageStore",
    "StoreError",
    "CorruptionError",
    "DuplicateRecordError",
    "MonotonicityError",
    "PartialWriteError",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Store constants
# ---------------------------------------------------------------------------

STORE_VERSION       = 1
LOG_FILENAME        = "lineage.log"
META_FILENAME       = "lineage.meta"
LOCK_FILENAME       = "lineage.lock"

# Frame layout: [MAGIC: 4B][SIZE: 4B][RECORD_JSON][HASH: 64B ASCII][SEP: 1B '\n']
FRAME_MAGIC         = b"LIN\x01"
FRAME_MAGIC_SIZE    = 4
FRAME_SIZE_SIZE     = 4   # uint32 big-endian
FRAME_HASH_SIZE     = 64  # SHA-256 hex, ASCII
FRAME_SEP           = b"\n"
FRAME_HEADER_SIZE   = FRAME_MAGIC_SIZE + FRAME_SIZE_SIZE
FRAME_TRAILER_SIZE  = FRAME_HASH_SIZE + len(FRAME_SEP)

_STRUCT_SIZE        = struct.Struct(">I")   # big-endian uint32


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class StoreError(Exception):
    """Base class for all lineage store errors. Always fatal unless in recovery mode."""

class CorruptionError(StoreError):
    """Frame-level or hash-level corruption detected in the log."""

class DuplicateRecordError(StoreError):
    """A record with this node ID or output artifact ID is already stored."""

class MonotonicityError(StoreError):
    """Logical timestamp regression or non-strict increase detected."""

class PartialWriteError(StoreError):
    """An incomplete frame was detected at the tail of the log."""

class LockError(StoreError):
    """Could not acquire exclusive write lock."""

class MetaError(StoreError):
    """Meta file is missing, malformed, or inconsistent."""


# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------

def _canonical_json_dumps(obj: dict) -> str:
    """
    Centralized canonical JSON serialization for deterministic encoding.
    
    Uses the same parameters as lineage_record._canonical_json() for consistency.
    This ensures all serialization in the store uses identical canonicalization.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False)


def _encode_frame(record_json: str) -> bytes:
    """
    Encode a single record as a length-prefixed, hash-verified frame.

    Layout:
        MAGIC   (4B)
        SIZE    (4B big-endian uint32) — byte length of record_json
        PAYLOAD (SIZE bytes, UTF-8)
        HASH    (64B ASCII SHA-256 hex of PAYLOAD)
        SEP     (1B '\n')
    """
    payload = record_json.encode("utf-8")
    size    = _STRUCT_SIZE.pack(len(payload))
    digest  = hashlib.sha256(payload).hexdigest().encode("ascii")
    return FRAME_MAGIC + size + payload + digest + FRAME_SEP


def _decode_frame(data: bytes, offset: int) -> tuple[str, str, int]:
    """
    Decode one frame starting at *offset* within *data*.

    Returns:
        (record_json: str, frame_hash: str, next_offset: int)

    Raises:
        PartialWriteError  — frame exists but is truncated
        CorruptionError    — magic mismatch or hash mismatch
        StopIteration      — no more data
    """
    if offset >= len(data):
        raise StopIteration

    # Magic
    if len(data) < offset + FRAME_HEADER_SIZE:
        raise PartialWriteError(
            f"Truncated frame header at offset {offset}: "
            f"need {FRAME_HEADER_SIZE}B, have {len(data) - offset}B."
        )
    magic = data[offset: offset + FRAME_MAGIC_SIZE]
    if magic != FRAME_MAGIC:
        raise CorruptionError(
            f"Bad frame magic at offset {offset}: expected {FRAME_MAGIC!r}, got {magic!r}."
        )

    # Size
    size_raw = data[offset + FRAME_MAGIC_SIZE: offset + FRAME_HEADER_SIZE]
    payload_size: int = _STRUCT_SIZE.unpack(size_raw)[0]

    # Payload
    payload_start  = offset + FRAME_HEADER_SIZE
    payload_end    = payload_start + payload_size
    trailer_end    = payload_end + FRAME_TRAILER_SIZE

    if len(data) < trailer_end:
        raise PartialWriteError(
            f"Truncated frame body at offset {offset}: "
            f"need {trailer_end - offset}B total, have {len(data) - offset}B."
        )

    payload      = data[payload_start: payload_end]
    stored_hash  = data[payload_end: payload_end + FRAME_HASH_SIZE].decode("ascii")
    sep          = data[payload_end + FRAME_HASH_SIZE: trailer_end]

    if sep != FRAME_SEP:
        raise CorruptionError(
            f"Missing frame separator at offset {payload_end + FRAME_HASH_SIZE}."
        )

    # Hash verification
    computed_hash = hashlib.sha256(payload).hexdigest()
    if computed_hash != stored_hash:
        raise CorruptionError(
            f"Hash mismatch at offset {offset}: "
            f"stored={stored_hash!r}, computed={computed_hash!r}."
        )

    return payload.decode("utf-8"), stored_hash, trailer_end


def _decode_frame_from_file(f: object, offset: int) -> tuple[str, str, int]:
    """
    Decode one frame starting at *offset* from an open file handle.
    
    This is a streaming version that reads only the necessary bytes from the file,
    avoiding loading the entire log into memory.
    
    Returns:
        (record_json: str, frame_hash: str, next_offset: int)
    
    Raises:
        PartialWriteError  — frame exists but is truncated
        CorruptionError    — magic mismatch or hash mismatch
        StopIteration      — no more data
        OSError            — file read error
    """
    # Read header (magic + size)
    f.seek(offset)
    header_data = f.read(FRAME_HEADER_SIZE)
    if len(header_data) < FRAME_HEADER_SIZE:
        if len(header_data) == 0:
            raise StopIteration
        raise PartialWriteError(
            f"Truncated frame header at offset {offset}: "
            f"need {FRAME_HEADER_SIZE}B, have {len(header_data)}B."
        )
    
    # Verify magic
    magic = header_data[:FRAME_MAGIC_SIZE]
    if magic != FRAME_MAGIC:
        raise CorruptionError(
            f"Bad frame magic at offset {offset}: expected {FRAME_MAGIC!r}, got {magic!r}."
        )
    
    # Extract payload size
    size_raw = header_data[FRAME_MAGIC_SIZE:]
    payload_size: int = _STRUCT_SIZE.unpack(size_raw)[0]
    
    # Read payload + trailer (hash + separator)
    payload_start = offset + FRAME_HEADER_SIZE
    frame_total_size = FRAME_HEADER_SIZE + payload_size + FRAME_TRAILER_SIZE
    frame_data = f.read(payload_size + FRAME_TRAILER_SIZE)
    
    if len(frame_data) < payload_size + FRAME_TRAILER_SIZE:
        raise PartialWriteError(
            f"Truncated frame body at offset {offset}: "
            f"need {frame_total_size}B total, have {FRAME_HEADER_SIZE + len(frame_data)}B."
        )
    
    payload = frame_data[:payload_size]
    stored_hash = frame_data[payload_size:payload_size + FRAME_HASH_SIZE].decode("ascii")
    sep = frame_data[payload_size + FRAME_HASH_SIZE:]
    
    if sep != FRAME_SEP:
        raise CorruptionError(
            f"Missing frame separator at offset {payload_start + payload_size + FRAME_HASH_SIZE}."
        )
    
    # Hash verification
    computed_hash = hashlib.sha256(payload).hexdigest()
    if computed_hash != stored_hash:
        raise CorruptionError(
            f"Hash mismatch at offset {offset}: "
            f"stored={stored_hash!r}, computed={computed_hash!r}."
        )
    
    next_offset = offset + frame_total_size
    return payload.decode("utf-8"), stored_hash, next_offset


# ---------------------------------------------------------------------------
# Rolling integrity hash
# ---------------------------------------------------------------------------

class _RollingHash:
    """
    Deterministic, append-only rolling SHA-256.
    Each record's frame hash is folded into the chain:
        chain = SHA-256(prev_chain + frame_hash)
    Initial state: SHA-256(b'LINEAGE_STORE_V1')
    """

    _INIT_SEED = hashlib.sha256(b"LINEAGE_STORE_V1").hexdigest()

    __slots__ = ("_current",)

    def __init__(self, initial: Optional[str] = None) -> None:
        self._current = initial if initial is not None else self._INIT_SEED

    def update(self, frame_hash: str) -> None:
        combined     = (self._current + frame_hash).encode("utf-8")
        self._current = hashlib.sha256(combined).hexdigest()

    @property
    def value(self) -> str:
        return self._current

    def __repr__(self) -> str:
        return f"_RollingHash({self._current!r})"


# ---------------------------------------------------------------------------
# Meta file
# ---------------------------------------------------------------------------

def _write_meta_atomic(meta_path: Path, data: dict) -> None:
    """Write meta JSON atomically via rename."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=meta_path.parent, prefix=".meta_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, meta_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_meta(meta_path: Path) -> dict:
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# LineageStore
# ---------------------------------------------------------------------------

class LineageStore:
    """
    Append-only, crash-safe, integrity-verified durable persistence boundary
    for LineageRecord objects.

    Storage layout::

        store_dir/
            lineage.log     — ordered, frame-encoded record log
            lineage.meta    — version, count, rolling integrity hash
            lineage.lock    — advisory exclusive write lock

    Startup sequence (enforced by callers)::

        store = LineageStore(path)
        store.open()                    # validates and loads meta
        records = store.load_all()      # validates every frame
        for r in records:
            graph.append(r)
        graph.validate_integrity()
        # system now operational

    Single-writer contract: only one process/thread may hold an open
    LineageStore in write mode at a time, enforced via fcntl advisory lock.
    Concurrent reads are safe after ``open()`` returns.
    """

    __slots__ = (
        "_store_dir",
        "_log_path",
        "_meta_path",
        "_lock_path",
        "_lock_fd",
        "_rolling_hash",
        "_record_count",
        "_last_logical_timestamp",
        "_seen_node_ids",
        "_seen_artifact_ids",
        "_seen_migration_payloads",
        "_schema_version",
        "_open",
    )

    def __init__(self, store_dir: str | Path, schema_version: SchemaVersionID) -> None:
        store_dir = Path(store_dir)
        if not isinstance(schema_version, SchemaVersionID):
            raise TypeError(f"schema_version must be SchemaVersionID, got {type(schema_version)!r}")

        object.__setattr__(self, "_store_dir",              store_dir)
        object.__setattr__(self, "_log_path",               store_dir / LOG_FILENAME)
        object.__setattr__(self, "_meta_path",              store_dir / META_FILENAME)
        object.__setattr__(self, "_lock_path",              store_dir / LOCK_FILENAME)
        object.__setattr__(self, "_lock_fd",                None)
        object.__setattr__(self, "_rolling_hash",           _RollingHash())
        object.__setattr__(self, "_record_count",           0)
        object.__setattr__(self, "_last_logical_timestamp", -1)
        object.__setattr__(self, "_seen_node_ids",          set())
        object.__setattr__(self, "_seen_artifact_ids",      set())
        object.__setattr__(self, "_seen_migration_payloads", set())
        object.__setattr__(self, "_schema_version",         schema_version)
        object.__setattr__(self, "_open",                   False)

    # -- attribute guard -----------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:  # type: ignore[override]
        raise TypeError("LineageStore is not directly mutable; use its API.")

    def _set(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)

    # -- lifecycle -----------------------------------------------------------

    def open(self) -> None:
        """
        Open the store for reading and writing.

        - Creates store_dir if absent.
        - Acquires exclusive write lock.
        - Initialises or validates meta file.
        - Performs crash-tail truncation if a partial frame is detected.
        """
        if self._open:
            raise StoreError("LineageStore is already open.")

        store_dir: Path = self._store_dir
        store_dir.mkdir(parents=True, exist_ok=True)

        # Acquire exclusive write lock
        lock_fd = open(self._lock_path, "w")  # noqa: WPS515
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_fd.close()
            raise LockError(
                f"Cannot acquire exclusive write lock on {self._lock_path}: {exc}"
            ) from exc
        self._set("_lock_fd", lock_fd)

        # Initialise or validate meta
        if not self._meta_path.exists():
            if self._log_path.exists() and self._log_path.stat().st_size > 0:
                raise MetaError(
                    f"Log file {self._log_path} exists but meta file is missing. "
                    "Possible corruption — manual recovery required."
                )
            self._write_meta()
        else:
            self._validate_and_load_meta()

        # Crash-tail truncation
        if self._log_path.exists():
            self._truncate_partial_tail()

        # Tier-0: Validate integrity at startup (fail-closed policy)
        # This ensures corruption is detected immediately, not deferred to load_all()
        if self._log_path.exists() and self._log_path.stat().st_size > 0:
            self._validate_integrity_at_startup()

        self._set("_open", True)
        log.info("LineageStore opened at %s (records=%d)", self._store_dir, self._record_count)

    def close(self) -> None:
        """Release the write lock and close the store."""
        if not self._open:
            return
        lock_fd = self._lock_fd
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except OSError:
                pass
        self._set("_lock_fd", None)
        self._set("_open", False)
        log.info("LineageStore closed at %s", self._store_dir)

    def __enter__(self) -> "LineageStore":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- append --------------------------------------------------------------

    def append(self, record: LineageRecord) -> None:
        """
        Durably append a validated LineageRecord to the log.

        Enforces:
          - Store is open
          - Unique lineage_node_id
          - Unique output_artifact_id
          - Strictly increasing logical_timestamp (must be previous + 1)
          - Atomic fsync write
          - Atomic meta update

        Raises StoreError subclasses on any violation.
        """
        self._assert_open()

        if not isinstance(record, LineageRecord):
            raise TypeError(f"append() requires LineageRecord, got {type(record)!r}")

        node_id = record.lineage_node_id
        out_id  = record.output_artifact_id

        # Tier-0 governance: logical_timestamp must be None at construction.
        # Store assigns it here, enforcing strict monotonicity.
        if record.logical_timestamp is not None:
            raise ValueError(
                f"LineageRecord.logical_timestamp must be None at append time "
                f"(will be assigned by store), got {record.logical_timestamp!r}"
            )

        # Assign next logical timestamp (strictly monotonic)
        expected_ts = self._last_logical_timestamp + 1
        ts = expected_ts

        # Uniqueness
        if node_id in self._seen_node_ids:
            raise DuplicateRecordError(
                f"LineageNodeID {node_id.to_string()!r} is already stored."
            )
        if out_id in self._seen_artifact_ids:
            raise DuplicateRecordError(
                f"ArtifactID {out_id.to_string()!r} is already stored."
            )

        # Tier-0: Store-level idempotency guard for migrations (prevents concurrent duplicates)
        # Check for duplicate migration payloads: (canonical_parent_ids, payload_hash)
        if record.transformation_type == TransformationType.MIGRATION:
            # Canonical tuple of parent IDs (already sorted in LineageRecord constructor)
            canonical_parents = tuple(record.input_artifact_ids)
            migration_key = (canonical_parents, record.transformation_payload_hash)
            if migration_key in self._seen_migration_payloads:
                raise DuplicateRecordError(
                    f"Migration with payload_hash {record.transformation_payload_hash!r} "
                    f"from parent(s) {[p.to_string() for p in canonical_parents]!r} "
                    f"already exists. Concurrent duplicate migration prevented."
                )

        # Encode frame with logical_timestamp included immutably
        # Tier-0: Do NOT mutate the caller's record object. Instead, create
        # a serialization dict that includes the timestamp without side effects.
        # This preserves record immutability and prevents breaking upstream hashing.
        record_dict = record.to_dict()
        # Override logical_timestamp in the dict (immutable operation)
        record_dict["logical_timestamp"] = ts
        record_json = _canonical_json_dumps(record_dict)
        frame       = _encode_frame(record_json)

        # Compute frame hash for rolling integrity
        frame_payload = record_json.encode("utf-8")
        frame_hash    = hashlib.sha256(frame_payload).hexdigest()

        # Write + fsync
        with open(self._log_path, "ab") as f:
            f.write(frame)
            f.flush()
            os.fsync(f.fileno())

        # Update in-memory state
        rolling: _RollingHash = self._rolling_hash
        rolling.update(frame_hash)

        seen_nodes: Set[LineageNodeID]    = self._seen_node_ids
        seen_arts:  Set[ArtifactID]       = self._seen_artifact_ids
        seen_migrations: Set[tuple[tuple[ArtifactID, ...], str]] = self._seen_migration_payloads
        seen_nodes.add(node_id)
        seen_arts.add(out_id)
        # Track migration payloads for idempotency
        if record.transformation_type == TransformationType.MIGRATION:
            canonical_parents = tuple(record.input_artifact_ids)
            migration_key = (canonical_parents, record.transformation_payload_hash)
            seen_migrations.add(migration_key)

        self._set("_record_count",           self._record_count + 1)
        self._set("_last_logical_timestamp", ts)

        # Atomic meta update
        self._write_meta()

        log.debug(
            "Appended node=%s ts=%d count=%d",
            node_id.to_string(), ts, self._record_count,
        )

    # -- load ----------------------------------------------------------------

    def load_all(self) -> Generator[LineageRecord, None, None]:
        """
        Read and yield every LineageRecord in exact append order.

        Validates every frame's hash before yielding. If corruption is found,
        raises CorruptionError (startup must abort).

        Tier-0: Streams from file instead of loading entire log into memory.
        This enables deterministic replay for very large logs (millions of records)
        without OOM risk.

        Resets in-memory tracking state so the store reflects the loaded log
        exactly — safe to call once at startup.
        """
        self._assert_open()

        if not self._log_path.exists() or self._log_path.stat().st_size == 0:
            return

        # Tier-0: Stream from file instead of loading entire log into memory
        rolling    = _RollingHash()
        count      = 0
        last_ts    = -1
        seen_nodes: Set[LineageNodeID] = set()
        seen_arts:  Set[ArtifactID]    = set()
        seen_migrations: Set[tuple[tuple[ArtifactID, ...], str]] = set()
        offset     = 0

        with open(self._log_path, "rb") as f:
            while True:
                try:
                    record_json, frame_hash, next_offset = _decode_frame_from_file(f, offset)
                except StopIteration:
                    break
                except PartialWriteError as exc:
                    # Should have been truncated by open() — treat as corruption here
                    raise CorruptionError(
                        f"Unexpected partial frame at offset {offset} during load. "
                        f"Detail: {exc}"
                    ) from exc

                # Deserialise
                try:
                    raw = json.loads(record_json)
                    record = LineageRecord.from_dict(raw)
                except Exception as exc:
                    raise CorruptionError(
                        f"Cannot deserialise record at offset {offset}: {exc}"
                    ) from exc

                # Duplicate checks
                node_id = record.lineage_node_id
                out_id  = record.output_artifact_id

                if node_id in seen_nodes:
                    raise CorruptionError(f"Duplicate lineage_node_id {node_id.to_string()!r} in log.")
                if out_id in seen_arts:
                    raise CorruptionError(f"Duplicate output_artifact_id {out_id.to_string()!r} in log.")

                # Timestamp monotonicity
                ts = record.logical_timestamp
                if ts != last_ts + 1:
                    raise CorruptionError(
                        f"Timestamp gap: expected {last_ts + 1}, got {ts} "
                        f"at offset {offset}."
                    )

                rolling.update(frame_hash)
                seen_nodes.add(node_id)
                seen_arts.add(out_id)
                # Track migration payloads for idempotency
                if record.transformation_type == TransformationType.MIGRATION:
                    canonical_parents = tuple(record.input_artifact_ids)
                    migration_key = (canonical_parents, record.transformation_payload_hash)
                    seen_migrations.add(migration_key)
                count  += 1
                last_ts = ts
                offset  = next_offset

                yield record

        # Reconcile with meta
        meta_count = self._record_count
        if count != meta_count:
            raise CorruptionError(
                f"Log contains {count} records but meta reports {meta_count}. "
                "Possible truncation or injection."
            )

        meta_hash = self._rolling_hash.value
        if rolling.value != meta_hash:
            raise CorruptionError(
                f"Rolling integrity hash mismatch: "
                f"computed={rolling.value!r}, meta={meta_hash!r}. "
                "Log has been tampered with."
            )

        # Sync in-memory state to what we loaded
        self._set("_rolling_hash",           rolling)
        self._set("_record_count",           count)
        self._set("_last_logical_timestamp", last_ts)
        self._set("_seen_node_ids",          seen_nodes)
        self._set("_seen_artifact_ids",      seen_arts)
        self._set("_seen_migration_payloads", seen_migrations)

    # -- integrity validation ------------------------------------------------

    def _validate_integrity_at_startup(self) -> None:
        """
        Tier-0: Validate rolling integrity hash at startup (fail-closed policy).
        
        This ensures corruption is detected immediately during open(), not deferred
        to load_all(). Validates that the meta file's integrity hash matches the
        actual computed hash from the log file.
        
        Raises CorruptionError if integrity hash mismatch is detected.
        """
        # Stream through log to compute rolling hash without loading all records
        rolling = _RollingHash()
        offset = 0
        
        with open(self._log_path, "rb") as f:
            while True:
                try:
                    _record_json, frame_hash, next_offset = _decode_frame_from_file(f, offset)
                    rolling.update(frame_hash)
                    offset = next_offset
                except StopIteration:
                    break
                except PartialWriteError:
                    # Partial tail should have been truncated by _truncate_partial_tail()
                    # If we hit one here, it's a logic error
                    raise CorruptionError(
                        f"Unexpected partial frame at offset {offset} after truncation. "
                        "This indicates a logic error in crash recovery."
                    )
        
        # Verify computed hash matches meta
        meta_hash = self._rolling_hash.value
        if rolling.value != meta_hash:
            raise CorruptionError(
                f"Rolling integrity hash mismatch at startup: "
                f"computed={rolling.value!r}, meta={meta_hash!r}. "
                "Log has been tampered with. Manual recovery required."
            )

    def validate_store_integrity(self) -> None:
        """
        Full integrity audit without yielding records.

        Checks:
          - Every frame reads cleanly
          - Every frame hash is correct
          - No truncated frames
          - Logical timestamps strictly sequential from 0
          - No duplicate node IDs or output artifact IDs
          - Rolling integrity hash matches meta

        Raises CorruptionError on any violation.
        """
        self._assert_open()
        # Re-drive load_all() without storing anything
        for _ in self.load_all():
            pass
        log.info(
            "Store integrity validated: %d records, hash=%s",
            self._record_count, self._rolling_hash.value,
        )

    # -- snapshot export -----------------------------------------------------

    def export_snapshot(self) -> dict:
        """
        Produce a deterministic, fully self-describing snapshot dict.

        Contains:
          - store_version
          - schema_version
          - record_count
          - integrity_hash
          - records: list of canonical record dicts in append order

        Suitable for backup, audit, and recovery bootstrap.
        JSON-serialisable; sorted keys for determinism.
        """
        self._assert_open()

        records_list = []
        for record in self.load_all():
            records_list.append(record.to_dict())

        snapshot = {
            "store_version":   STORE_VERSION,
            "schema_version":  int(self._schema_version),
            "record_count":    self._record_count,
            "integrity_hash":  self._rolling_hash.value,
            "records":         records_list,
        }
        return snapshot

    # -- crash recovery ------------------------------------------------------

    def _truncate_partial_tail(self) -> None:
        """
        Scan the log for a partial frame at the tail (crash residue).
        If found, truncate to the last fully-valid frame boundary.

        Safe to call multiple times (idempotent).
        """
        if not self._log_path.exists():
            return

        data        = self._log_path.read_bytes()
        last_valid  = 0
        offset      = 0

        while offset < len(data):
            try:
                _record_json, _frame_hash, next_offset = _decode_frame(data, offset)
                last_valid = next_offset
                offset     = next_offset
            except StopIteration:
                break
            except PartialWriteError:
                log.warning(
                    "Partial frame detected at offset %d in %s — "
                    "truncating to last valid boundary at %d.",
                    offset, self._log_path, last_valid,
                )
                with open(self._log_path, "r+b") as f:
                    f.truncate(last_valid)
                    f.flush()
                    os.fsync(f.fileno())
                break
            except CorruptionError as exc:
                # Mid-log corruption — cannot auto-repair; caller must fail-close
                raise CorruptionError(
                    f"Mid-log corruption at offset {offset} — cannot auto-truncate. "
                    f"Manual recovery required. Detail: {exc}"
                ) from exc

        # Reconcile record_count from valid frames
        count = 0
        last_ts = -1
        offset  = 0
        data    = self._log_path.read_bytes()   # re-read post-truncation
        rolling = _RollingHash()

        while offset < len(data):
            try:
                _record_json, frame_hash, next_offset = _decode_frame(data, offset)
            except (StopIteration, PartialWriteError):
                break
            rolling.update(frame_hash)
            count  += 1
            last_ts = last_ts + 1   # we trust sequential ts at this stage
            offset  = next_offset

        # Update in-memory state to match truncated log
        old_count = self._record_count
        old_hash = self._rolling_hash.value
        
        self._set("_record_count",           count)
        self._set("_last_logical_timestamp", last_ts)
        self._set("_rolling_hash",           rolling)
        
        # Update meta file if truncation changed the state
        if count != old_count or rolling.value != old_hash:
            self._write_meta()

    # -- meta helpers --------------------------------------------------------

    def _write_meta(self) -> None:
        meta = {
            "store_version":  STORE_VERSION,
            "schema_version": int(self._schema_version),
            "record_count":   self._record_count,
            "integrity_hash": self._rolling_hash.value,
        }
        _write_meta_atomic(self._meta_path, meta)

    def _validate_and_load_meta(self) -> None:
        try:
            meta = _read_meta(self._meta_path)
        except Exception as exc:
            raise MetaError(f"Cannot read meta file {self._meta_path}: {exc}") from exc

        if meta.get("store_version") != STORE_VERSION:
            raise MetaError(
                f"Unsupported store_version {meta.get('store_version')!r}; "
                f"expected {STORE_VERSION!r}."
            )

        self._set("_record_count",           int(meta.get("record_count", 0)))
        self._set("_last_logical_timestamp", int(meta.get("record_count", 0)) - 1)
        self._set(
            "_rolling_hash",
            _RollingHash(initial=meta.get("integrity_hash", _RollingHash._INIT_SEED)),
        )

    # -- guards --------------------------------------------------------------

    def _assert_open(self) -> None:
        if not self._open:
            raise StoreError("LineageStore is not open. Call open() first.")

    # -- repr ----------------------------------------------------------------

    # -- replay guard interface ------------------------------------------------

    def stream_records(
        self, start_index: int, end_index: Optional[int]
    ) -> Iterator[dict]:
        """
        Stream lineage records as dicts in append order, with append_index field.
        
        Converts LineageRecord objects to the dict format expected by replay_guard.
        Uses logical_timestamp as append_index (assuming sequential timestamps starting at 0).
        
        Tier-0: Streams from file and raises CorruptionError on any corruption
        (fail-closed policy). Never silently skips corrupted records.
        
        Args:
            start_index: Starting append index (inclusive)
            end_index: Ending append index (inclusive), or None for all remaining
            
        Yields:
            dict records with append_index, record_type, and other fields expected by replay_guard
            
        Raises:
            CorruptionError: If any frame corruption is detected (fail-closed)
        """
        self._assert_open()
        
        if not self._log_path.exists() or self._log_path.stat().st_size == 0:
            return
        
        # Tier-0: Stream from file instead of loading entire log into memory
        offset = 0
        
        with open(self._log_path, "rb") as f:
            while True:
                try:
                    record_json, frame_hash, next_offset = _decode_frame_from_file(f, offset)
                except StopIteration:
                    break
                except PartialWriteError as exc:
                    # Tier-0: Fail-closed policy - raise on corruption, never silently skip
                    raise CorruptionError(
                        f"Partial frame detected at offset {offset} during stream. "
                        f"Detail: {exc}. Manual recovery required."
                    ) from exc
                
                # Deserialize
                try:
                    raw = json.loads(record_json)
                    record = LineageRecord.from_dict(raw)
                except Exception as exc:
                    # Tier-0: Fail-closed policy - raise on deserialization failure
                    raise CorruptionError(
                        f"Cannot deserialise record at offset {offset}: {exc}. "
                        "Manual recovery required."
                    ) from exc
                
                # Convert logical_timestamp to append_index
                append_idx = record.logical_timestamp
                
                # Filter by index range
                if append_idx < start_index:
                    offset = next_offset
                    continue
                if end_index is not None and append_idx > end_index:
                    break
                
                # Convert LineageRecord to replay_guard format
                record_dict = self._convert_to_replay_format(record, append_idx)
                yield record_dict
                
                offset = next_offset

    def _convert_to_replay_format(self, record: LineageRecord, append_index: int) -> dict:
        """
        Convert LineageRecord to the dict format expected by replay_guard.
        
        Maps:
        - transformation_type -> record_type (MIGRATION, GENESIS, etc.)
        - input_artifact_ids -> parent_artifact_id (first parent for MIGRATION)
        - output_artifact_id -> new_artifact_id (for MIGRATION) or artifact_id (for GENESIS)
        - input_schema_version -> from_version
        - output_schema_version -> to_version or schema_version
        - transformation_payload_hash -> transformation_hash
        
        Note: GENESIS records are identified by having no input artifacts.
        """
        # Determine if this is a GENESIS record (no input artifacts)
        is_genesis = len(record.input_artifact_ids) == 0
        
        if is_genesis:
            # GENESIS record - artifact created from nothing
            record_type = "GENESIS"
            result = {
                "record_type": record_type,
                "artifact_id": record.output_artifact_id.to_string(),
                "artifact_type": record.artifact_type.to_string(),
                "schema_version": record.output_schema_version.to_string(),
                "transformation_hash": record.transformation_payload_hash,
                "input_artifact_hash": "",
                "output_artifact_hash": "",
                "migration_rule_id": "genesis",
                "append_index": append_index,
            }
        elif record.transformation_type == TransformationType.MIGRATION:
            # MIGRATION record - artifact transformed from parent
            record_type = "MIGRATION"
            # For MIGRATION, use first input as parent
            parent_id = record.input_artifact_ids[0].to_string() if record.input_artifact_ids else None
            result = {
                "record_type": record_type,
                "parent_artifact_id": parent_id,
                "new_artifact_id": record.output_artifact_id.to_string(),
                "from_version": record.input_schema_version.to_string(),
                "to_version": record.output_schema_version.to_string(),
                "artifact_type": record.artifact_type.to_string(),
                "migration_rule_id": record.migration_id.to_string() if record.migration_id else "",
                "transformation_hash": record.transformation_payload_hash,
                "input_artifact_hash": "",  # Will be filled from artifact store if available
                "output_artifact_hash": "",  # Will be filled from artifact store if available
                "append_index": append_index,
            }
        else:
            # Other transformation types (INGESTION, CANONICALIZATION, etc.) - treat as MIGRATION-like
            record_type = "MIGRATION"
            parent_id = record.input_artifact_ids[0].to_string() if record.input_artifact_ids else None
            result = {
                "record_type": record_type,
                "parent_artifact_id": parent_id,
                "new_artifact_id": record.output_artifact_id.to_string(),
                "from_version": record.input_schema_version.to_string(),
                "to_version": record.output_schema_version.to_string(),
                "artifact_type": record.artifact_type.to_string(),
                "migration_rule_id": "",
                "transformation_hash": record.transformation_payload_hash,
                "input_artifact_hash": "",
                "output_artifact_hash": "",
                "append_index": append_index,
            }
        
        return result

    def get_current_append_index(self) -> int:
        """
        Get the current append index (highest logical_timestamp).
        Returns -1 if no records exist.
        """
        self._assert_open()
        return self._last_logical_timestamp

    def get_total_record_count(self) -> int:
        """Get the total number of records in the store."""
        self._assert_open()
        return self._record_count

    def get_record_count(self) -> int:
        """Alias for get_total_record_count() for protocol compatibility."""
        return self.get_total_record_count()

    # -- repr ----------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"LineageStore("
            f"path={self._store_dir!r}, "
            f"open={self._open!r}, "
            f"records={self._record_count!r}, "
            f"last_ts={self._last_logical_timestamp!r}"
            f")"
        )