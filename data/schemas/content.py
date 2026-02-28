"""
/data/schemas/content.py

Canonical Content Entity Schemas (Immutable, Versioned, Provable)

This file defines the authoritative structural truth for content objects.

It answers exactly one question:
    "What must be true for something to legitimately be called 'content'?"

NOT engagement. NOT virality. NOT performance. Just what it IS.

WHAT THIS FILE IS:
  - The canonical definition of content identity
  - The immutable content payload structure
  - The encoding of provenance & authorship
  - The structural lifecycle declarations
  - The foundation for deterministic hashing & replay

WHAT THIS FILE IS NOT:
  ❌ Not a posting model
  ❌ Not a rendering model
  ❌ Not ranking metadata
  ❌ Not analytics
  ❌ Not platform-specific
  ❌ Not mutable or stateful

Anything behavioral lives elsewhere.

DESIGN PRINCIPLE (NON-NEGOTIABLE):
    Content is an immutable artifact with a provable origin.
    
    Once created, content never changes — only new content can exist.
    Edits create replacements, not mutations.

DETERMINISM GUARANTEES:
  - Same payload → same content_id
  - Same schema → same hash
  - Same bytes → same meaning
  - No clock dependence
  - No environment dependence
  
  Replay depends on this.

MENTAL MODEL:
    Content is evidence, not a post.
    Posts can be removed. Content records must never lie.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple, Dict, Any, FrozenSet, List
from datetime import datetime
import hashlib
import json


# =============================================================================
# IMPORTS FROM BASE SCHEMA (ASSUMED TO EXIST)
# =============================================================================

# This file depends on base.py which defines CanonicalSchema
# For now, we define the protocol inline, but in production this imports from base.py

class CanonicalSchema(ABC):
    """
    Base class for all canonical schemas.
    
    Enforces:
      - Immutability (frozen dataclasses)
      - Versioning
      - Validation
      - Deterministic serialization
    """
    
    @abstractmethod
    def validate(self) -> None:
        """
        Validate schema invariants.
        
        MUST raise ValueError for any violation.
        MUST be deterministic.
        """
        pass
    
    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to deterministic dictionary.
        
        MUST preserve field order.
        MUST be reversible.
        """
        pass
    
    @abstractmethod
    def compute_hash(self) -> str:
        """
        Compute deterministic content hash.
        
        MUST be reproducible.
        MUST be collision-resistant.
        """
        pass


# =============================================================================
# CONTENT TAXONOMY (AUTHORITATIVE)
# =============================================================================


class ContentKind(Enum):
    """
    Structural content classification.
    
    NOT semantic classification (that's metadata elsewhere).
    
    Kinds define payload expectations, nothing else.
    """
    VIDEO = "video"
    IMAGE = "image"
    TEXT = "text"
    AUDIO = "audio"
    COMPOSITE = "composite"  # Multiple media types
    DOCUMENT = "document"  # PDFs, office docs, etc.
    CODE = "code"  # Source code, scripts
    DATA = "data"  # Structured data (JSON, CSV, etc.)
    
    def requires_duration(self) -> bool:
        """Does this kind require duration_seconds?"""
        return self in (ContentKind.VIDEO, ContentKind.AUDIO)
    
    def supports_duration(self) -> bool:
        """Can this kind have duration_seconds?"""
        return self.requires_duration()
    
    def is_media(self) -> bool:
        """Is this rich media content?"""
        return self in (
            ContentKind.VIDEO,
            ContentKind.IMAGE,
            ContentKind.AUDIO,
            ContentKind.COMPOSITE,
        )


class ContentOrigin(Enum):
    """
    Origin classification for provenance tracking.
    
    Describes HOW content entered the system, not WHO created it.
    """
    USER_UPLOAD = "user_upload"  # Directly uploaded by user
    SYSTEM_GENERATED = "system_generated"  # Created by system process
    AI_GENERATED = "ai_generated"  # Created by AI model
    INGESTED = "ingested"  # Imported from external source
    DERIVED = "derived"  # Transformed from parent content
    SNAPSHOT_RESTORED = "snapshot_restored"  # Restored from snapshot


class ContentLifecycleStage(Enum):
    """
    Lifecycle stage — descriptive, not controlling.
    
    This is a MARKER, not a permission system.
    No "deleted", no "hidden", no "suppressed" — those are system states.
    
    Content records describe facts, not opinions about visibility.
    """
    CREATED = "created"  # Just created, not yet processed
    INGESTED = "ingested"  # Fully processed and stored
    ARCHIVED = "archived"  # Moved to long-term storage
    REPLICATED = "replicated"  # Copied to backup/replica
    
    def is_active(self) -> bool:
        """Is content actively available?"""
        return self in (
            ContentLifecycleStage.CREATED,
            ContentLifecycleStage.INGESTED,
        )


# =============================================================================
# CONTENT IDENTITY
# =============================================================================


@dataclass(frozen=True)
class ContentIdentity:
    """
    Immutable content identity.
    
    RULES:
      - content_id MUST be derived from payload hash
      - NO random UUIDs
      - NO time-based IDs
      - MUST be deterministic
    
    The content_id IS the content — same bytes, same ID.
    """
    content_id: str  # Deterministic, content-addressable
    schema_name: str  # Always "content"
    schema_version: int  # Explicit versioning
    
    def validate(self) -> None:
        """Validate identity invariants."""
        if not self.content_id:
            raise ValueError("content_id cannot be empty")
        
        if self.schema_name != "content":
            raise ValueError(
                f"schema_name must be 'content', got '{self.schema_name}'"
            )
        
        if self.schema_version < 1:
            raise ValueError(
                f"schema_version must be >= 1, got {self.schema_version}"
            )
        
        # content_id must be valid hex hash (SHA-256)
        if len(self.content_id) != 64:
            raise ValueError(
                f"content_id must be 64-char SHA-256 hex, got {len(self.content_id)} chars"
            )
        
        try:
            int(self.content_id, 16)
        except ValueError:
            raise ValueError("content_id must be valid hex string")


# =============================================================================
# CONTENT PROVENANCE
# =============================================================================


@dataclass(frozen=True)
class ContentProvenance:
    """
    Immutable provenance tracking.
    
    RULES:
      - Empty parents allowed (root content)
      - Parents are immutable references
      - Lineage is explicit, never inferred
      - Creator may be None (system content)
    
    Provenance MUST be auditable and traceable.
    """
    origin: ContentOrigin
    creator_account_id: Optional[str] = None  # None for system content
    source_snapshot_id: Optional[str] = None  # Snapshot this was restored from
    parent_content_ids: Tuple[str, ...] = field(default_factory=tuple)  # Immutable ancestry
    creation_context: Optional[str] = None  # Opaque context (e.g., "web_upload", "api_v2")
    
    def validate(self, self_content_id: str) -> None:
        """
        Validate provenance invariants.
        
        Args:
            self_content_id: The content_id of the content this provenance belongs to
        """
        # Creator ID format validation
        if self.creator_account_id is not None:
            if not self.creator_account_id:
                raise ValueError("creator_account_id cannot be empty string")
        
        # Snapshot ID format validation
        if self.source_snapshot_id is not None:
            if not self.source_snapshot_id:
                raise ValueError("source_snapshot_id cannot be empty string")
        
        # Parent validation
        for parent_id in self.parent_content_ids:
            if not parent_id:
                raise ValueError("parent_content_ids cannot contain empty strings")
            
            if parent_id == self_content_id:
                raise ValueError(
                    "Content cannot be its own parent (circular reference)"
                )
            
            # Parent IDs must be valid hashes
            if len(parent_id) != 64:
                raise ValueError(
                    f"parent_content_id must be 64-char SHA-256 hex, got {len(parent_id)} chars"
                )
        
        # No duplicate parents
        if len(self.parent_content_ids) != len(set(self.parent_content_ids)):
            raise ValueError("parent_content_ids contains duplicates")
        
        # Origin-specific validation
        if self.origin == ContentOrigin.USER_UPLOAD:
            if self.creator_account_id is None:
                raise ValueError(
                    "USER_UPLOAD origin requires creator_account_id"
                )
        
        if self.origin == ContentOrigin.SNAPSHOT_RESTORED:
            if self.source_snapshot_id is None:
                raise ValueError(
                    "SNAPSHOT_RESTORED origin requires source_snapshot_id"
                )
        
        if self.origin == ContentOrigin.DERIVED:
            if not self.parent_content_ids:
                raise ValueError(
                    "DERIVED origin requires at least one parent_content_id"
                )
    
    def is_root(self) -> bool:
        """Is this root content with no parents?"""
        return len(self.parent_content_ids) == 0
    
    def get_ancestry_depth(self) -> int:
        """
        Get ancestry depth.
        
        Note: This only counts direct parents, not full genealogy depth.
        """
        return len(self.parent_content_ids)


# =============================================================================
# CONTENT PAYLOAD
# =============================================================================


@dataclass(frozen=True)
class ContentPayload:
    """
    Immutable content payload descriptor.
    
    Payload is STRUCTURAL only — no semantics.
    
    RULES:
      - uri_ref is opaque — never parsed here
      - Payload bytes are NEVER embedded
      - byte_size must be exact
      - Hash mismatch = invalid content
      - content_hash is SHA-256 of raw bytes
    
    The payload describes the content, it does NOT contain it.
    """
    kind: ContentKind
    mime_type: str
    byte_size: int
    content_hash: str  # SHA-256 of raw bytes
    uri_ref: str  # Opaque storage reference (e.g., "blob://abc123")
    encoding: Optional[str] = None  # Character encoding for text (e.g., "utf-8")
    compression: Optional[str] = None  # Compression format (e.g., "gzip", "zstd")
    
    def validate(self) -> None:
        """Validate payload invariants."""
        if not self.mime_type:
            raise ValueError("mime_type cannot be empty")
        
        if self.byte_size < 0:
            raise ValueError(
                f"byte_size must be >= 0, got {self.byte_size}"
            )
        
        if not self.content_hash:
            raise ValueError("content_hash cannot be empty")
        
        # content_hash must be valid SHA-256 hex
        if len(self.content_hash) != 64:
            raise ValueError(
                f"content_hash must be 64-char SHA-256 hex, got {len(self.content_hash)} chars"
            )
        
        try:
            int(self.content_hash, 16)
        except ValueError:
            raise ValueError("content_hash must be valid hex string")
        
        if not self.uri_ref:
            raise ValueError("uri_ref cannot be empty")
        
        # MIME type basic validation
        if "/" not in self.mime_type:
            raise ValueError(
                f"mime_type must be in format 'type/subtype', got '{self.mime_type}'"
            )
        
        # Text content should specify encoding
        if self.kind == ContentKind.TEXT and self.encoding is None:
            raise ValueError("TEXT kind requires encoding to be specified")
        
        # Zero-byte content is legal but suspicious for media
        if self.byte_size == 0 and self.kind.is_media():
            # Warning: zero-byte media is unusual but not invalid
            pass
    
    def compute_content_id(self) -> str:
        """
        Compute deterministic content_id from payload.
        
        content_id IS content_hash — they are the same.
        
        Returns:
            str: SHA-256 hex digest
        """
        return self.content_hash
    
    def is_compressed(self) -> bool:
        """Is payload compressed?"""
        return self.compression is not None
    
    def get_size_mb(self) -> float:
        """Get size in megabytes."""
        return self.byte_size / (1024 * 1024)


# =============================================================================
# OPTIONAL METADATA (STRICTLY BOUNDED)
# =============================================================================


@dataclass(frozen=True)
class ContentMetadata:
    """
    Optional, bounded metadata.
    
    STRICT RULES:
      - duration_seconds only for AUDIO/VIDEO
      - language is ISO 639-1 or None
      - All fields immutable
      - No analytics
      - No engagement metrics
      - No ranking hints
    
    If it's not structural, it doesn't belong here.
    """
    language: Optional[str] = None  # ISO 639-1 code (e.g., "en", "es")
    duration_seconds: Optional[float] = None  # For audio/video only
    width_pixels: Optional[int] = None  # For image/video
    height_pixels: Optional[int] = None  # For image/video
    frame_rate: Optional[float] = None  # For video
    bit_rate: Optional[int] = None  # For audio/video
    sample_rate: Optional[int] = None  # For audio
    color_space: Optional[str] = None  # For image/video (e.g., "sRGB", "P3")
    has_alpha: Optional[bool] = None  # For image (transparency)
    
    def validate(self, kind: ContentKind) -> None:
        """
        Validate metadata against content kind.
        
        Args:
            kind: The ContentKind this metadata describes
        """
        # Duration validation
        if self.duration_seconds is not None:
            if not kind.supports_duration():
                raise ValueError(
                    f"duration_seconds not allowed for {kind.value} content"
                )
            if self.duration_seconds < 0:
                raise ValueError(
                    f"duration_seconds must be >= 0, got {self.duration_seconds}"
                )
        elif kind.requires_duration():
            raise ValueError(
                f"{kind.value} content requires duration_seconds"
            )
        
        # Dimension validation
        if self.width_pixels is not None:
            if kind not in (ContentKind.IMAGE, ContentKind.VIDEO, ContentKind.COMPOSITE):
                raise ValueError(
                    f"width_pixels not allowed for {kind.value} content"
                )
            if self.width_pixels <= 0:
                raise ValueError("width_pixels must be > 0")
        
        if self.height_pixels is not None:
            if kind not in (ContentKind.IMAGE, ContentKind.VIDEO, ContentKind.COMPOSITE):
                raise ValueError(
                    f"height_pixels not allowed for {kind.value} content"
                )
            if self.height_pixels <= 0:
                raise ValueError("height_pixels must be > 0")
        
        # Frame rate validation
        if self.frame_rate is not None:
            if kind != ContentKind.VIDEO:
                raise ValueError(
                    f"frame_rate not allowed for {kind.value} content"
                )
            if self.frame_rate <= 0:
                raise ValueError("frame_rate must be > 0")
        
        # Sample rate validation
        if self.sample_rate is not None:
            if kind not in (ContentKind.AUDIO, ContentKind.VIDEO):
                raise ValueError(
                    f"sample_rate not allowed for {kind.value} content"
                )
            if self.sample_rate <= 0:
                raise ValueError("sample_rate must be > 0")
        
        # Language validation
        if self.language is not None:
            if not self.language:
                raise ValueError("language cannot be empty string")
            # ISO 639-1 is 2 characters, ISO 639-3 is 3
            if len(self.language) not in (2, 3):
                raise ValueError(
                    f"language must be 2 or 3 char ISO code, got '{self.language}'"
                )
            if not self.language.islower():
                raise ValueError(
                    f"language code must be lowercase, got '{self.language}'"
                )


# =============================================================================
# CANONICAL CONTENT RECORD (FINAL FORM)
# =============================================================================


@dataclass(frozen=True)
class ContentRecord(CanonicalSchema):
    """
    The canonical, immutable content record.
    
    This is the SINGLE SOURCE OF TRUTH for what content IS.
    
    VALIDATION RULES (HARD FAILURES):
      - schema_name == "content"
      - schema_version is supported
      - content_id == payload.content_hash
      - byte_size >= 0
      - duration_seconds only for AUDIO/VIDEO
      - No empty mime_type
      - No mutable containers
      - parent IDs != self ID
    
    Any violation → reject content entirely.
    
    SERIALIZATION ORDER (DETERMINISTIC):
      1. identity
      2. payload
      3. provenance
      4. lifecycle
      5. optional metadata
    
    No field omission. No optional collapse.
    """
    
    # === IDENTITY ===
    content_id: str
    schema_name: str
    schema_version: int
    
    # === STRUCTURE ===
    payload: ContentPayload
    provenance: ContentProvenance
    
    # === LIFECYCLE ===
    lifecycle_stage: ContentLifecycleStage
    
    # === OPTIONAL METADATA ===
    metadata: ContentMetadata = field(default_factory=ContentMetadata)
    
    # === TIMESTAMPS (INFORMATIONAL ONLY) ===
    created_at: Optional[datetime] = None  # When record was created
    
    # === EXTENSIONS (OPAQUE) ===
    extensions: Dict[str, Any] = field(default_factory=dict)  # Opaque extensions
    
    def __post_init__(self):
        """Post-initialization validation."""
        # Validate immediately on construction
        self.validate()
    
    def validate(self) -> None:
        """
        Validate all content invariants.
        
        MUST raise ValueError for any violation.
        MUST be deterministic.
        
        Raises:
            ValueError: If any invariant is violated
        """
        # Schema name validation
        if self.schema_name != "content":
            raise ValueError(
                f"schema_name must be 'content', got '{self.schema_name}'"
            )
        
        # Schema version validation
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported schema_version {self.schema_version}, "
                f"supported versions: {SUPPORTED_SCHEMA_VERSIONS}"
            )
        
        # Identity validation
        identity = ContentIdentity(
            content_id=self.content_id,
            schema_name=self.schema_name,
            schema_version=self.schema_version,
        )
        identity.validate()
        
        # Payload validation
        self.payload.validate()
        
        # Content ID must match payload hash
        expected_id = self.payload.compute_content_id()
        if self.content_id != expected_id:
            raise ValueError(
                f"content_id mismatch: expected {expected_id}, got {self.content_id}"
            )
        
        # Provenance validation
        self.provenance.validate(self_content_id=self.content_id)
        
        # Metadata validation
        self.metadata.validate(kind=self.payload.kind)
        
        # Extensions validation (must be JSON-safe)
        if self.extensions:
            try:
                json.dumps(self.extensions)
            except (TypeError, ValueError) as e:
                raise ValueError(f"extensions must be JSON-safe: {e}")
        
        # Timestamp validation
        if self.created_at is not None:
            # Timestamps are informational but must be sane
            if self.created_at.year < 2000 or self.created_at.year > 2100:
                raise ValueError(
                    f"created_at year must be 2000-2100, got {self.created_at.year}"
                )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to deterministic dictionary.
        
        FIELD ORDER (DETERMINISTIC):
          1. identity
          2. payload
          3. provenance
          4. lifecycle
          5. metadata
          6. timestamps
          7. extensions
        
        Returns:
            Dict[str, Any]: Serialized content
        """
        # Helper to serialize dataclass
        def serialize_dataclass(obj) -> Dict[str, Any]:
            if isinstance(obj, Enum):
                return obj.value
            elif hasattr(obj, '__dataclass_fields__'):
                return {
                    k: serialize_dataclass(v)
                    for k, v in obj.__dict__.items()
                }
            elif isinstance(obj, (list, tuple)):
                return [serialize_dataclass(item) for item in obj]
            elif isinstance(obj, dict):
                return {k: serialize_dataclass(v) for k, v in obj.items()}
            elif isinstance(obj, datetime):
                return obj.isoformat()
            else:
                return obj
        
        return {
            # Identity
            "content_id": self.content_id,
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            
            # Payload
            "payload": {
                "kind": self.payload.kind.value,
                "mime_type": self.payload.mime_type,
                "byte_size": self.payload.byte_size,
                "content_hash": self.payload.content_hash,
                "uri_ref": self.payload.uri_ref,
                "encoding": self.payload.encoding,
                "compression": self.payload.compression,
            },
            
            # Provenance
            "provenance": {
                "origin": self.provenance.origin.value,
                "creator_account_id": self.provenance.creator_account_id,
                "source_snapshot_id": self.provenance.source_snapshot_id,
                "parent_content_ids": list(self.provenance.parent_content_ids),
                "creation_context": self.provenance.creation_context,
            },
            
            # Lifecycle
            "lifecycle_stage": self.lifecycle_stage.value,
            
            # Metadata
            "metadata": serialize_dataclass(self.metadata),
            
            # Timestamps
            "created_at": self.created_at.isoformat() if self.created_at else None,
            
            # Extensions
            "extensions": self.extensions,
        }
    
    def compute_hash(self) -> str:
        """
        Compute deterministic content hash.
        
        The hash is based on STRUCTURAL content only:
          - content_id (which is payload hash)
          - provenance
          - metadata
        
        This allows verification that content record is intact.
        
        Returns:
            str: SHA-256 hex digest
        """
        # Serialize to deterministic JSON
        data = self.to_dict()
        
        # Remove non-structural fields
        data.pop("created_at", None)
        data.pop("extensions", None)
        
        # Deterministic JSON serialization
        json_bytes = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        
        return hashlib.sha256(json_bytes).hexdigest()
    
    def is_root(self) -> bool:
        """Is this root content with no parents?"""
        return self.provenance.is_root()
    
    def is_active(self) -> bool:
        """Is content in active lifecycle stage?"""
        return self.lifecycle_stage.is_active()
    
    def is_media(self) -> bool:
        """Is this media content?"""
        return self.payload.kind.is_media()
    
    def get_parent_ids(self) -> Tuple[str, ...]:
        """Get immutable parent content IDs."""
        return self.provenance.parent_content_ids
    
    def get_size_mb(self) -> float:
        """Get content size in megabytes."""
        return self.payload.get_size_mb()


# =============================================================================
# SCHEMA VERSIONING
# =============================================================================


CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset([1])


def validate_schema_version(version: int) -> None:
    """
    Validate schema version is supported.
    
    Args:
        version: Schema version to validate
    
    Raises:
        ValueError: If version not supported
    """
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported schema version {version}, "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )


# =============================================================================
# CONTENT FACTORY (DETERMINISTIC CONSTRUCTION)
# =============================================================================


class ContentFactory:
    """
    Factory for creating valid ContentRecord instances.
    
    Ensures all invariants are satisfied at construction time.
    """
    
    @staticmethod
    def create_from_bytes(
        raw_bytes: bytes,
        kind: ContentKind,
        mime_type: str,
        uri_ref: str,
        origin: ContentOrigin,
        creator_account_id: Optional[str] = None,
        parent_content_ids: Tuple[str, ...] = (),
        encoding: Optional[str] = None,
        compression: Optional[str] = None,
        metadata: Optional[ContentMetadata] = None,
        extensions: Optional[Dict[str, Any]] = None,
        creation_context: Optional[str] = None,
    ) -> ContentRecord:
        """
        Create ContentRecord from raw bytes.
        
        This is the PRIMARY constructor for content.
        
        Args:
            raw_bytes: The actual content bytes
            kind: Content kind
            mime_type: MIME type
            uri_ref: Storage URI reference
            origin: Content origin
            creator_account_id: Optional creator ID
            parent_content_ids: Parent content IDs
            encoding: Character encoding (for text)
            compression: Compression format
            metadata: Optional metadata
            extensions: Optional extensions
            creation_context: Optional creation context
        
        Returns:
            ContentRecord: Valid content record
        """
        # Compute content hash from bytes
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        
        # Create payload
        payload = ContentPayload(
            kind=kind,
            mime_type=mime_type,
            byte_size=len(raw_bytes),
            content_hash=content_hash,
            uri_ref=uri_ref,
            encoding=encoding,
            compression=compression,
        )
        
        # Create provenance
        provenance = ContentProvenance(
            origin=origin,
            creator_account_id=creator_account_id,
            parent_content_ids=parent_content_ids,
            creation_context=creation_context,
        )
        
        # Create content record
        return ContentRecord(
            content_id=content_hash,  # content_id IS content_hash
            schema_name="content",
            schema_version=CURRENT_SCHEMA_VERSION,
            payload=payload,
            provenance=provenance,
            lifecycle_stage=ContentLifecycleStage.CREATED,
            metadata=metadata or ContentMetadata(),
            created_at=datetime.utcnow(),
            extensions=extensions or {},
        )
    
    @staticmethod
    def create_derived(
        raw_bytes: bytes,
        kind: ContentKind,
        mime_type: str,
        uri_ref: str,
        parent_content_ids: Tuple[str, ...],
        creator_account_id: Optional[str] = None,
        encoding: Optional[str] = None,
        compression: Optional[str] = None,
        metadata: Optional[ContentMetadata] = None,
        extensions: Optional[Dict[str, Any]] = None,
    ) -> ContentRecord:
        """
        Create derived content from parent content.
        
        Args:
            raw_bytes: The derived content bytes
            kind: Content kind
            mime_type: MIME type
            uri_ref: Storage URI reference
            parent_content_ids: Parent content IDs (must be non-empty)
            creator_account_id: Optional creator ID
            encoding: Character encoding
            compression: Compression format
            metadata: Optional metadata
            extensions: Optional extensions
        
        Returns:
            ContentRecord: Valid derived content record
        """
        if not parent_content_ids:
            raise ValueError("Derived content requires at least one parent")
        
        return ContentFactory.create_from_bytes(
            raw_bytes=raw_bytes,
            kind=kind,
            mime_type=mime_type,
            uri_ref=uri_ref,
            origin=ContentOrigin.DERIVED,
            creator_account_id=creator_account_id,
            parent_content_ids=parent_content_ids,
            encoding=encoding,
            compression=compression,
            metadata=metadata,
            extensions=extensions,
            creation_context="derived",
        )


# =============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# =============================================================================

"""
STRICTLY FORBIDDEN in this file:

❌ Mutable metadata
❌ Inline analytics
❌ Platform IDs (Twitter, YouTube, etc.)
❌ Ranking hints
❌ Engagement counters (likes, views, shares)
❌ "Updated at" fields
❌ Soft deletion flags
❌ User preferences
❌ Display formatting
❌ Moderation flags
❌ Performance metrics
❌ A/B test variants
❌ Geographic restrictions
❌ Pricing information

This file describes FACTS, not OPINIONS.

Content is EVIDENCE, not a POST.
Posts can be removed. Content records must never lie.
"""


# =============================================================================
# VERSION INFO
# =============================================================================

__version__ = "1.0.0"
__schema_version__ = CURRENT_SCHEMA_VERSION

# Content is evidence. This file is law.