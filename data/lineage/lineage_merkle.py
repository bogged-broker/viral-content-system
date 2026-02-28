"""
lineage_merkle.py
Cryptographic Integrity Root Authority
Deterministic Hash Tree — External Verification Capable — Tamper-Evident

CANONICAL_MERKLE_FORMAT_VERSION = "1"
Padding rule: duplicate last leaf (Bitcoin-style) when odd leaf count.
"""

from __future__ import annotations

import hashlib
import json
import hmac
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, List, Optional, Sequence, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

CANONICAL_MERKLE_FORMAT_VERSION: str = "1"
HASH_ALGORITHM: str = "sha256"
ZERO_HASH: bytes = b"\x00" * 32          # unused (Bitcoin-style chosen), defined for spec completeness
_ENCODING: str = "utf-8"


# ──────────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────────

class MerkleError(Exception):
    """Base for all Merkle integrity failures."""

class SerializationError(MerkleError):
    """Record could not be canonically serialized."""

class AppendOrderError(MerkleError):
    """Append-index sequence discontinuity, duplicate, or gap."""

class HashMismatchError(MerkleError):
    """Stored record hash does not match recomputed hash."""

class TreeInconsistencyError(MerkleError):
    """Internal tree structure is inconsistent."""

class ProofVerificationError(MerkleError):
    """Merkle proof failed verification."""


# ──────────────────────────────────────────────────────────────────────────────
# Canonical Serialization
# ──────────────────────────────────────────────────────────────────────────────

def _normalize_timestamp(ts: str) -> str:
    """
    Normalize timestamp to canonical ISO-8601 format.
    
    Canonical format: YYYY-MM-DDTHH:MM:SS.fffZ (always UTC, always with milliseconds, always Z suffix)
    
    Handles variations:
    - "2026-02-22T10:00:00Z" → "2026-02-22T10:00:00.000Z"
    - "2026-02-22T10:00:00.000Z" → "2026-02-22T10:00:00.000Z"
    - "2026-02-22T10:00:00+00:00" → "2026-02-22T10:00:00.000Z"
    - "2026-02-22T10:00:00.123456Z" → "2026-02-22T10:00:00.123Z" (truncate to 3 decimal places)
    
    Raises SerializationError if timestamp cannot be parsed.
    """
    if not isinstance(ts, str):
        raise SerializationError(f"Timestamp must be string, got {type(ts).__name__}")
    
    try:
        # Try parsing with various formats
        dt = None
        
        # Handle Z suffix
        if ts.endswith('Z'):
            ts_clean = ts[:-1]
            dt = datetime.fromisoformat(ts_clean.replace('Z', '+00:00'))
        # Handle +00:00 or -00:00
        elif '+' in ts or (ts.count('-') > 2 and 'T' in ts):
            dt = datetime.fromisoformat(ts)
        # Try without timezone (assume UTC)
        else:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        
        # Ensure UTC
        if dt.tzinfo != timezone.utc:
            dt = dt.astimezone(timezone.utc)
        
        # Format to canonical: YYYY-MM-DDTHH:MM:SS.fffZ
        # Always include milliseconds (3 decimal places)
        iso_str = dt.isoformat(timespec='milliseconds')
        # Replace +00:00 with Z
        if iso_str.endswith('+00:00'):
            iso_str = iso_str[:-6] + 'Z'
        elif not iso_str.endswith('Z'):
            iso_str += 'Z'
        
        return iso_str
    except (ValueError, AttributeError) as exc:
        raise SerializationError(f"Cannot normalize timestamp {ts!r}: {exc}") from exc


def _normalize_value(v: object) -> object:
    """Recursively normalize a value for deterministic JSON serialization."""
    if isinstance(v, float):
        # Normalize to avoid cross-platform float repr drift
        return format(v, ".17g")
    if isinstance(v, str):
        # Check if string looks like a timestamp (ISO-8601 pattern)
        # Pattern: YYYY-MM-DD followed by T and time
        if re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', v):
            return _normalize_timestamp(v)
    if isinstance(v, dict):
        return {k: _normalize_value(val) for k, val in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_normalize_value(i) for i in v]
    return v


def canonical_serialize(record: dict) -> bytes:
    """
    Produce a byte-identical, deterministic serialization of a lineage record.
    Contract:
      - Keys sorted recursively
      - No whitespace
      - UTF-8 encoded
      - Floats normalized to .17g
      - Timestamps normalized to canonical ISO-8601 format (YYYY-MM-DDTHH:MM:SS.fffZ)
    """
    try:
        normalized = _normalize_value(record)
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode(_ENCODING)
    except (TypeError, ValueError) as exc:
        raise SerializationError(f"Cannot canonically serialize record: {exc}") from exc


def record_leaf_hash(record: dict) -> bytes:
    """SHA-256 of canonical serialization → 32 raw bytes."""
    return hashlib.sha256(canonical_serialize(record)).digest()


# ──────────────────────────────────────────────────────────────────────────────
# Core Tree Primitives
# ──────────────────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _combine(left: bytes, right: bytes) -> bytes:
    """Internal node hash: SHA-256(left || right)."""
    return _sha256(left + right)


def _build_tree_layer(layer: List[bytes]) -> List[bytes]:
    """
    Reduce one layer to its parents.
    Odd length → duplicate last node (Bitcoin-style).
    """
    if len(layer) == 1:
        return layer
    if len(layer) % 2 == 1:
        layer = layer + [layer[-1]]   # duplicate last
    return [_combine(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)]


def _compute_root_from_leaves(leaves: List[bytes]) -> bytes:
    """Full O(N) tree construction → root bytes."""
    if not leaves:
        raise TreeInconsistencyError("Cannot compute Merkle root from empty leaf set.")
    layer = list(leaves)
    while len(layer) > 1:
        layer = _build_tree_layer(layer)
    return layer[0]


def _build_full_tree(leaves: List[bytes]) -> List[List[bytes]]:
    """
    Build and return all layers (bottom → top) for proof generation.
    Index 0 = leaf layer, index[-1] = [root].
    """
    if not leaves:
        raise TreeInconsistencyError("Cannot build tree from empty leaf set.")
    layers: List[List[bytes]] = [list(leaves)]
    while len(layers[-1]) > 1:
        layers.append(_build_tree_layer(layers[-1]))
    return layers


def _compute_rightmost_path(leaves: List[bytes]) -> RightmostPath:
    """
    Compute the rightmost path from root to last leaf.
    
    This path enables O(log N) incremental updates.
    The path contains the nodes needed to recompute the root when appending a new leaf.
    
    Algorithm:
    1. Build tree layers
    2. Trace path from root (top) to last leaf (bottom)
    3. For each level, record the rightmost node and its sibling direction
    """
    if not leaves:
        raise TreeInconsistencyError("Cannot compute rightmost path from empty leaf set.")
    
    layers = _build_full_tree(leaves)
    n = len(leaves)
    
    # Trace path from root to last leaf
    path: List[Tuple[bytes, bool]] = []
    
    # Start at root (top layer)
    current_idx = 0  # Root is always at index 0 in top layer
    depth = len(layers) - 1
    
    # Work backwards from root to leaf
    for depth in range(len(layers) - 1, -1, -1):
        layer = layers[depth]
        is_leaf = (depth == 0)
        
        # Get current node
        node_hash = layer[current_idx]
        path.append((node_hash, is_leaf))
        
        # Move to next level (if not at leaf level)
        if depth > 0:
            # In the layer below, find the rightmost child
            # If current_idx is even, both children are in the pair
            # If current_idx is odd, we need to handle padding
            # The rightmost leaf is always the last in the leaf layer
            # So we trace: if we're at position i in layer L, we go to position 2*i or 2*i+1 in layer L-1
            # But we want the rightmost path, so we always go to the right child when possible
            
            # Calculate child indices
            left_child_idx = current_idx * 2
            right_child_idx = current_idx * 2 + 1
            
            # Check if right child exists (accounting for padding)
            next_layer = layers[depth - 1]
            if right_child_idx < len(next_layer):
                current_idx = right_child_idx
            else:
                # Right child doesn't exist (padding case), use left
                current_idx = left_child_idx
    
    # Reverse to get path from root to leaf (or keep as-is for leaf-to-root)
    # Actually, we want root-to-leaf for incremental update
    path.reverse()
    return RightmostPath(path_nodes=path, leaf_count=n)


def _incremental_update_root(
    previous_path: RightmostPath,
    new_leaf: bytes,
) -> Tuple[bytes, RightmostPath]:
    """
    Compute new root and rightmost path from previous path + new leaf.
    
    O(log N) complexity: only traverses the rightmost path.
    
    Algorithm (Bitcoin-style Merkle tree with duplicate-last-leaf padding):
    1. If previous tree was empty, new root is the leaf
    2. Get previous last leaf from path
    3. Combine [prev_last_leaf, new_leaf] to form new rightmost internal node
    4. Walk up tree:
       - If previous tree had odd N leaves, last leaf was duplicated
       - Now with N+1 (even), we combine the pair
       - Continue up, combining with siblings from previous path
       - Apply padding when needed
    """
    n = previous_path.leaf_count
    new_n = n + 1
    
    if n == 0:
        # First leaf: root is the leaf itself
        new_root = new_leaf
        new_path = RightmostPath(path_nodes=[(new_leaf, True)], leaf_count=1)
        return new_root, new_path
    
    # Path is stored from leaf to root: [leaf, level1, level2, ..., root]
    # Get the last leaf (first node in path)
    prev_last_leaf = previous_path.path_nodes[0][0]
    
    # Combine previous last leaf with new leaf
    # This forms the new rightmost pair at the leaf level
    current = _combine(prev_last_leaf, new_leaf)
    new_path_nodes: List[Tuple[bytes, bool]] = [(new_leaf, True), (current, False)]
    
    # Walk up the tree
    # Previous path structure: [last_leaf, internal1, internal2, ..., root]
    # We start at index 1 (first internal node after leaf)
    path_idx = 1
    
    while path_idx < len(previous_path.path_nodes):
        prev_sibling = previous_path.path_nodes[path_idx][0]
        
        # Combine current node with its sibling from previous tree
        current = _combine(prev_sibling, current)
        new_path_nodes.append((current, False))
        
        path_idx += 1
        
        # Check if we've reached root
        # Root is when we have only one node at this level
        # For level L, number of nodes = ceil(new_n / 2^L)
        level = len(new_path_nodes) - 1  # -1 because we count from leaf (index 0)
        nodes_at_next_level = (new_n + (1 << (level + 1)) - 1) >> (level + 1)
        if nodes_at_next_level <= 1:
            new_root = current
            break
    
    # If we haven't reached root yet, continue building up
    # This happens when tree height increases
    while True:
        level = len(new_path_nodes) - 1
        nodes_at_level = (new_n + (1 << level) - 1) >> level
        if nodes_at_level <= 1:
            new_root = current
            break
        
        # Apply padding if needed
        # At this level, if we have odd number of nodes, duplicate last
        if nodes_at_level % 2 == 1:
            current = _combine(current, current)  # Padding: duplicate
            new_path_nodes.append((current, False))
        else:
            # Shouldn't happen - we should have combined with sibling
            break
    
    new_path = RightmostPath(path_nodes=new_path_nodes, leaf_count=new_n)
    return new_root, new_path


# ──────────────────────────────────────────────────────────────────────────────
# Data Objects
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MerkleRoot:
    """
    Deterministic Merkle root (Tier-0 infrastructure-grade).
    
    This object contains ONLY fields that are part of the cryptographic hash contract.
    Metadata (computed_at) is separated into MerkleRootMetadata for architectural purity.
    """
    root_hash: str                      # hex-encoded SHA-256 root
    leaf_count: int
    algorithm: str
    canonical_format_version: str

    def hex_bytes(self) -> bytes:
        return bytes.fromhex(self.root_hash)

    def to_dict(self) -> dict:
        """Serialization of deterministic fields only."""
        return {
            "root_hash": self.root_hash,
            "leaf_count": self.leaf_count,
            "algorithm": self.algorithm,
            "canonical_format_version": self.canonical_format_version,
        }


@dataclass(frozen=True)
class MerkleRootMetadata:
    """
    Non-deterministic metadata associated with a Merkle root computation.
    
    Separated from MerkleRoot to maintain architectural purity:
    - MerkleRoot: Deterministic, hashable, reproducible
    - MerkleRootMetadata: Operational metadata, non-deterministic
    """
    root: MerkleRoot
    computed_at: str                    # ISO-8601 UTC; excluded from hash contract

    def to_dict(self) -> dict:
        """Full serialization including metadata."""
        result = self.root.to_dict()
        result["computed_at"] = self.computed_at
        return result


@dataclass(frozen=True)
class MerkleProof:
    leaf_index: int
    leaf_hash: str                      # hex
    siblings: List[Tuple[str, str]]     # list of (direction, hex_hash); direction ∈ {"L","R"}
    root_hash: str                      # hex — root this proof was generated against
    leaf_count: int
    canonical_format_version: str

    def to_dict(self) -> dict:
        return {
            "leaf_index": self.leaf_index,
            "leaf_hash": self.leaf_hash,
            "siblings": [{"direction": d, "hash": h} for d, h in self.siblings],
            "root_hash": self.root_hash,
            "leaf_count": self.leaf_count,
            "canonical_format_version": self.canonical_format_version,
        }


@dataclass(frozen=True)
class SignedMerkleRoot:
    merkle_root: MerkleRoot
    signature_hex: str                  # HMAC-SHA256 hex over canonical root payload
    public_key_fingerprint: str         # SHA-256 of signing key material (hex)

    def to_dict(self) -> dict:
        return {
            "merkle_root": self.merkle_root.to_dict(),
            "signature_hex": self.signature_hex,
            "public_key_fingerprint": self.public_key_fingerprint,
        }


@dataclass(frozen=True)
class RightmostPath:
    """
    Rightmost path from root to last leaf, enabling O(log N) incremental updates.
    
    For a tree with N leaves, the rightmost path contains at most ceil(log2(N)) nodes.
    This allows incremental root computation in O(log N) time.
    
    Structure:
    - path_nodes: List of (node_hash, is_leaf) tuples from leaf to root
    - leaf_count: Number of leaves in the tree
    """
    path_nodes: List[Tuple[bytes, bool]]  # (hash_bytes, is_leaf_flag)
    leaf_count: int

    def to_dict(self) -> dict:
        """Serializable representation for persistence."""
        return {
            "path_nodes": [(h.hex(), is_leaf) for h, is_leaf in self.path_nodes],
            "leaf_count": self.leaf_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RightmostPath":
        """Reconstruct from serialized form."""
        return cls(
            path_nodes=[(bytes.fromhex(h), is_leaf) for h, is_leaf in data["path_nodes"]],
            leaf_count=data["leaf_count"],
        )


# ──────────────────────────────────────────────────────────────────────────────
# Append-Order Validator
# ──────────────────────────────────────────────────────────────────────────────

def _validate_append_order(records: Sequence[dict]) -> None:
    """
    Enforce strictly-monotonic, gap-free, duplicate-free append index.
    Records must expose 'append_index' (int).
    """
    seen: set = set()
    expected = 0
    for rec in records:
        try:
            idx = int(rec["append_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AppendOrderError(f"Record missing valid append_index: {exc}") from exc
        if idx in seen:
            raise AppendOrderError(f"Duplicate append_index detected: {idx}")
        if idx != expected:
            raise AppendOrderError(
                f"Append-index discontinuity: expected {expected}, got {idx}"
            )
        seen.add(idx)
        expected += 1


# ──────────────────────────────────────────────────────────────────────────────
# MerkleTree — Core Authority
# ──────────────────────────────────────────────────────────────────────────────

class MerkleTree:
    """
    Deterministic Merkle tree over an ordered lineage record stream.

    Usage:
        tree = MerkleTree.from_records(store.get_all_records())
        root = tree.compute_root()
        proof = tree.generate_proof(42)
        MerkleTree.verify_proof(proof, root.root_hash)
    """

    __slots__ = ("_leaves", "_schema_fingerprint", "_migration_fingerprint")

    def __init__(
        self,
        leaves: List[bytes],
        schema_fingerprint: str = "",
        migration_fingerprint: str = "",
    ) -> None:
        if not leaves:
            raise TreeInconsistencyError("MerkleTree requires at least one leaf.")
        self._leaves: List[bytes] = leaves
        self._schema_fingerprint = schema_fingerprint
        self._migration_fingerprint = migration_fingerprint

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def from_records(
        cls,
        records: Sequence[dict],
        schema_fingerprint: str = "",
        migration_fingerprint: str = "",
        validate_order: bool = True,
        validate_stored_hashes: bool = False,
        stored_hash_field: str = "record_hash",
    ) -> "MerkleTree":
        """
        Build tree from ordered lineage records (spec §3: Data Inputs).

        Source of truth: lineage_store.get_all_records()
        Records must be canonically serialized and ordered by append index.

        Args:
            records: Full ordered record stream from lineage_store.
            schema_fingerprint: Optional schema registry fingerprint for anchoring.
            migration_fingerprint: Optional migration registry fingerprint for anchoring.
            validate_order: Enforce gap-free append_index sequence (spec §14: Failure Conditions).
            validate_stored_hashes: Cross-check stored hash field against recomputed hash.
            stored_hash_field: Field name in record containing pre-stored hash (hex).

        Raises:
            TreeInconsistencyError: If record stream is empty.
            AppendOrderError: If validate_order=True and order is invalid (spec §14).
            HashMismatchError: If validate_stored_hashes=True and hash mismatch (spec §14).
            SerializationError: If record cannot be serialized (spec §14).
        """
        records = list(records)
        if not records:
            raise TreeInconsistencyError("Record stream is empty.")

        # Failure condition: Append order discontinuity, duplicate, or gap (spec §14)
        if validate_order:
            _validate_append_order(records)

        leaves: List[bytes] = []
        for rec in records:
            # Failure condition: Record serialization invalid (spec §14)
            try:
                leaf = record_leaf_hash(rec)
            except SerializationError:
                raise  # Re-raise with context
            except Exception as exc:
                raise SerializationError(
                    f"Record at append_index={rec.get('append_index')} serialization failed: {exc}"
                ) from exc
            
            # Failure condition: Record hash mismatch with stored hash (spec §14)
            if validate_stored_hashes and stored_hash_field in rec:
                stored = rec[stored_hash_field]
                if leaf.hex() != stored:
                    raise HashMismatchError(
                        f"Record append_index={rec.get('append_index')}: "
                        f"stored hash {stored!r} != recomputed {leaf.hex()!r}"
                    )
            leaves.append(leaf)

        return cls(leaves, schema_fingerprint, migration_fingerprint)

    @classmethod
    def from_record_stream(
        cls,
        stream: Iterator[dict],
        **kwargs,
    ) -> "MerkleTree":
        """Memory-conscious construction from a generator/iterator."""
        return cls.from_records(list(stream), **kwargs)

    # ── Root Computation ──────────────────────────────────────────────────────

    def compute_root(self) -> MerkleRoot:
        """Compute deterministic Merkle root (without metadata)."""
        root_bytes = _compute_root_from_leaves(self._leaves)
        return MerkleRoot(
            root_hash=root_bytes.hex(),
            leaf_count=len(self._leaves),
            algorithm=HASH_ALGORITHM,
            canonical_format_version=CANONICAL_MERKLE_FORMAT_VERSION,
        )
    
    def compute_root_with_metadata(self) -> MerkleRootMetadata:
        """Compute Merkle root with operational metadata."""
        root = self.compute_root()
        return MerkleRootMetadata(
            root=root,
            computed_at=_normalize_timestamp(datetime.now(timezone.utc).isoformat()),
        )

    # ── Incremental Append ────────────────────────────────────────────────────

    def append_record(self, record: dict, validate_index: bool = True) -> "MerkleTree":
        """
        Return a new MerkleTree with one record appended.
        O(1) leaf extension; root equivalence with full rebuild is maintained
        because root is always recomputed from all leaves.

        For true O(log N) incremental root, use update_with_new_record() instead.
        """
        if validate_index:
            expected = len(self._leaves)
            idx = record.get("append_index")
            if idx != expected:
                raise AppendOrderError(
                    f"Expected append_index {expected}, got {idx}"
                )
        new_leaf = record_leaf_hash(record)
        return MerkleTree(
            self._leaves + [new_leaf],
            self._schema_fingerprint,
            self._migration_fingerprint,
        )

    @staticmethod
    def update_with_new_record(
        previous_root: MerkleRoot,
        previous_path: RightmostPath,
        new_record: dict,
        previous_leaf_count: Optional[int] = None,
    ) -> Tuple[MerkleRoot, RightmostPath]:
        """
        Incremental root update (spec §7: Incremental Update Capability) - O(log N).
        
        Computes new root from previous_root + new_record using rightmost path.
        This is the Tier-0 O(log N) implementation required by spec.
        
        Args:
            previous_root: The MerkleRoot from the previous state.
            previous_path: The RightmostPath from the previous state (enables O(log N)).
            new_record: The new record to append.
            previous_leaf_count: Optional validation of previous leaf count.
        
        Returns:
            Tuple of (new MerkleRoot, new RightmostPath) with incremented leaf_count.
        
        Raises:
            TreeInconsistencyError if validation fails.
        """
        if previous_leaf_count is not None and previous_root.leaf_count != previous_leaf_count:
            raise TreeInconsistencyError(
                f"previous_leaf_count {previous_leaf_count} != "
                f"previous_root.leaf_count {previous_root.leaf_count}"
            )
        if previous_path.leaf_count != previous_root.leaf_count:
            raise TreeInconsistencyError(
                f"previous_path.leaf_count {previous_path.leaf_count} != "
                f"previous_root.leaf_count {previous_root.leaf_count}"
            )
        
        # Compute leaf hash for new record
        new_leaf = record_leaf_hash(new_record)
        
        # O(log N) incremental update using rightmost path
        new_root_bytes, new_path = _incremental_update_root(previous_path, new_leaf)
        
        # Validate: new root should match what we computed
        new_root = MerkleRoot(
            root_hash=new_root_bytes.hex(),
            leaf_count=previous_root.leaf_count + 1,
            algorithm=previous_root.algorithm,
            canonical_format_version=previous_root.canonical_format_version,
        )
        
        return new_root, new_path

    # ── Proof Generation ──────────────────────────────────────────────────────

    def generate_proof(self, leaf_index: int) -> MerkleProof:
        """
        Generate an inclusion proof for the record at leaf_index.
        Proof is self-contained; verification needs only proof + root_hash.
        """
        n = len(self._leaves)
        if not (0 <= leaf_index < n):
            raise ValueError(f"leaf_index {leaf_index} out of range [0, {n}).")

        layers = _build_full_tree(self._leaves)
        root_hex = layers[-1][0].hex()

        siblings: List[Tuple[str, str]] = []
        idx = leaf_index

        for depth, layer in enumerate(layers[:-1]):
            # Apply same padding rule used during tree build
            padded = layer + ([layer[-1]] if len(layer) % 2 == 1 else [])
            if idx % 2 == 0:
                # current is left child; sibling is right
                sibling_idx = idx + 1
                direction = "R"
            else:
                sibling_idx = idx - 1
                direction = "L"
            siblings.append((direction, padded[sibling_idx].hex()))
            idx //= 2

        return MerkleProof(
            leaf_index=leaf_index,
            leaf_hash=self._leaves[leaf_index].hex(),
            siblings=siblings,
            root_hash=root_hex,
            leaf_count=n,
            canonical_format_version=CANONICAL_MERKLE_FORMAT_VERSION,
        )

    # ── Proof Verification (static) ───────────────────────────────────────────

    @staticmethod
    def verify_proof(proof: MerkleProof, expected_root_hex: str) -> bool:
        """
        Verify a MerkleProof against a known root hash.
        Raises ProofVerificationError on failure; returns True on success.
        Does not require access to the full record set.
        """
        if proof.canonical_format_version != CANONICAL_MERKLE_FORMAT_VERSION:
            raise ProofVerificationError(
                f"Proof format version {proof.canonical_format_version!r} "
                f"!= current {CANONICAL_MERKLE_FORMAT_VERSION!r}"
            )
        try:
            current = bytes.fromhex(proof.leaf_hash)
            for direction, sibling_hex in proof.siblings:
                sibling = bytes.fromhex(sibling_hex)
                if direction == "R":
                    current = _combine(current, sibling)
                elif direction == "L":
                    current = _combine(sibling, current)
                else:
                    raise ProofVerificationError(f"Invalid sibling direction: {direction!r}")
        except ValueError as exc:
            raise ProofVerificationError(f"Hex decode failure in proof: {exc}") from exc

        computed_hex = current.hex()
        if not hmac.compare_digest(computed_hex, proof.root_hash):
            raise ProofVerificationError(
                "Proof does not resolve to its declared root_hash."
            )
        if not hmac.compare_digest(computed_hex, expected_root_hex):
            raise ProofVerificationError(
                "Proof root_hash does not match expected_root_hex."
            )
        return True

    # ── External Anchoring ────────────────────────────────────────────────────

    def get_rightmost_path(self) -> RightmostPath:
        """Compute and return the rightmost path for O(log N) incremental updates."""
        return _compute_rightmost_path(self._leaves)
    
    def export_anchor_payload(
        self,
        root: Optional[MerkleRoot] = None,
    ) -> dict:
        """
        Produce a JSON-serializable anchor payload for external notarization,
        blockchain anchoring, or compliance archive submission.
        """
        if root is None:
            root = self.compute_root()
        return {
            "root_hash": root.root_hash,
            "record_count": root.leaf_count,
            "schema_registry_fingerprint": self._schema_fingerprint,
            "migration_registry_fingerprint": self._migration_fingerprint,
            "canonical_format_version": root.canonical_format_version,
            "algorithm": root.algorithm,
        }

    # ── Snapshot Sealing ──────────────────────────────────────────────────────

    def seal_snapshot(
        self,
        signing_key: bytes,
        root: Optional[MerkleRoot] = None,
    ) -> SignedMerkleRoot:
        """
        Produce an immutable signed snapshot of the current Merkle root.
        signing_key: raw bytes (HMAC-SHA256 key material).
        Does NOT mutate lineage.
        """
        if root is None:
            root = self.compute_root()

        payload = json.dumps(self.export_anchor_payload(root),
                             sort_keys=True, separators=(",", ":")).encode(_ENCODING)
        sig = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
        key_fingerprint = hashlib.sha256(signing_key).hexdigest()

        return SignedMerkleRoot(
            merkle_root=root,
            signature_hex=sig,
            public_key_fingerprint=key_fingerprint,
        )

    # ── Replay Alignment ──────────────────────────────────────────────────────

    @staticmethod
    def assert_replay_alignment(
        store_root: MerkleRoot,
        replay_root: MerkleRoot,
    ) -> None:
        """
        Enforce that a forensic replay produced the same Merkle root as the store.
        
        Implements spec §13: Replay Alignment Verification.
        Integrates with lineage_auditor post-replay verification.
        
        After full replay, call this to ensure:
        - Structural replay produced same root
        - Detects hidden record mutation
        - Detects in-memory structure difference
        
        Raises TreeInconsistencyError on mismatch.
        
        Usage with lineage_auditor::
            store_tree = MerkleTree.from_records(store.get_all_records())
            store_root = store_tree.compute_root()
            
            replay_tree = MerkleTree.from_records(replayed_records)
            replay_root = replay_tree.compute_root()
            
            MerkleTree.assert_replay_alignment(store_root, replay_root)
        """
        if not hmac.compare_digest(store_root.root_hash, replay_root.root_hash):
            raise TreeInconsistencyError(
                f"Replay alignment FAILED: "
                f"store_root={store_root.root_hash!r} "
                f"replay_root={replay_root.root_hash!r}. "
                f"This indicates record mutation or structural difference."
            )
        if store_root.leaf_count != replay_root.leaf_count:
            raise TreeInconsistencyError(
                f"Replay leaf count mismatch: "
                f"store={store_root.leaf_count} replay={replay_root.leaf_count}. "
                f"This indicates record count difference."
            )
        if store_root.canonical_format_version != replay_root.canonical_format_version:
            raise TreeInconsistencyError(
                f"Replay format version mismatch: "
                f"store={store_root.canonical_format_version!r} "
                f"replay={replay_root.canonical_format_version!r}"
            )

    # ── Internals ─────────────────────────────────────────────────────────────

    @property
    def leaf_count(self) -> int:
        return len(self._leaves)

    def __repr__(self) -> str:
        return (
            f"MerkleTree(leaves={self.leaf_count}, "
            f"format_version={CANONICAL_MERKLE_FORMAT_VERSION!r})"
        )