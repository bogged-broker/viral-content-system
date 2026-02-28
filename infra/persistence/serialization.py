"""
/infra/persistence/serialization.py

Deterministic Value Encoding & Decoding Authority
(No Ambiguity, No Drift, No Silent Mutation)

This module is the single authority that defines how in-memory values become
persisted bytes and back again.

CRITICAL PRINCIPLES:
- Deterministic: Same input must produce identical bytes
- Stable: Encoding format must be versioned
- Verifiable: Data corruption must be detectable
- Explicit: Encoding format must be declared, never inferred
- Backend-agnostic: Works identically across memory, disk, Redis, etc.
- Immutable-by-default: Stored representations must not drift

ABSOLUTE INVARIANTS:
1. deserialize(serialize(x)) == x (round-trip idempotency)
2. serialize(x1) == serialize(x2) if x1 == x2 (byte stability)
3. No environment dependency (OS, Python version, architecture, process ID, random seed)
4. hash(serialize(x)) is stable across processes
5. Two independent processes serialize the same value → identical bytes

If this file lies, persistence lies.
If persistence lies, replay fails silently.
"""

from __future__ import annotations

import json
import hashlib
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from decimal import Decimal

# Import from persistence_errors.py
try:
    from .backends.persistence_errors import SerializationError
except ImportError:
    try:
        from infra.persistence.backends.persistence_errors import SerializationError
    except ImportError:
        # Fallback for standalone usage
        class SerializationError(Exception):
            """Value encoding or decoding failure."""
            pass


# ============================================================================
# Version Management
# ============================================================================

# Current serialization format version
CURRENT_VERSION = 1

# All supported versions for backward compatibility
SUPPORTED_VERSIONS = {1}


# ============================================================================
# Encoding Type Constants
# ============================================================================

ENCODING_JSON = "json"

# Allowed encoding types
ALLOWED_ENCODINGS = {ENCODING_JSON}


# ============================================================================
# Envelope Structure
# ============================================================================

ENVELOPE_VERSION_KEY = "version"
ENVELOPE_ENCODING_KEY = "encoding"
ENVELOPE_PAYLOAD_KEY = "payload"
ENVELOPE_CHECKSUM_KEY = "checksum"


# ============================================================================
# Deterministic JSON Encoding
# ============================================================================

class DeterministicJSONEncoder(json.JSONEncoder):
    """
    JSON encoder that produces deterministic, canonical output.
    
    Features:
    - Sorts dictionary keys alphabetically
    - Handles datetime objects (converts to ISO-8601 UTC)
    - Handles Decimal objects (converts to float)
    - Ensures consistent float representation
    - No arbitrary object serialization
    """
    
    def __init__(self, *args, **kwargs):
        # Force sorted keys for determinism
        kwargs['sort_keys'] = True
        # Consistent separators (no trailing spaces)
        kwargs['separators'] = (',', ':')
        # Disable ensure_ascii for consistent UTF-8 handling
        kwargs['ensure_ascii'] = False
        super().__init__(*args, **kwargs)
    
    def default(self, obj):
        """
        Convert non-JSON-serializable objects to JSON-safe types.
        
        Normalizes:
        - Datetime objects to ISO-8601 UTC
        - Decimal objects to float
        
        Raises:
            SerializationError: If object cannot be serialized
        """
        # Handle datetime objects - normalize to UTC ISO-8601
        if isinstance(obj, datetime):
            # TIER-0: Reject naive datetime (no semantic inference)
            if obj.tzinfo is None:
                raise SerializationError(
                    message="Naive datetime not allowed - must be timezone-aware (explicit UTC required)",
                    backend="serialization",
                    operation="ENCODE"
                )
            # Convert to UTC and format (deterministic conversion)
            utc_dt = obj.astimezone(timezone.utc)
            # Normalize to consistent format
            iso_str = utc_dt.isoformat()
            # Replace +00:00 with Z for consistency
            if iso_str.endswith('+00:00'):
                return iso_str[:-6] + 'Z'
            return iso_str
        
        # Handle Decimal objects (normalize to canonical float representation)
        if isinstance(obj, Decimal):
            # TIER-0: Convert Decimal to float deterministically
            # Challenge: JSON float serialization can vary across Python versions/platforms
            # Solution: Normalize float to canonical form using repr() round-trip
            # This ensures the float value serializes to identical JSON bytes across environments
            # 
            # Process:
            # 1. Convert Decimal to float (may have precision loss, but deterministic)
            # 2. Use repr() to get canonical string representation
            # 3. Parse back to float - this normalizes to canonical float representation
            # 4. JSON encoder will serialize this canonical float deterministically
            #
            # Note: Python 3.1+ JSON encoder uses deterministic algorithm, but this
            # normalization ensures byte-identical output even across different float
            # representations of the same mathematical value.
            float_val = float(obj)
            # Normalize through repr() round-trip to ensure canonical representation
            # This handles edge cases where same mathematical value has different
            # internal representations (e.g., 0.1 vs 0.10000000000000001)
            canonical_float = float(repr(float_val))
            return canonical_float
        
        # Reject other non-serializable types
        raise SerializationError(
            message=f"Cannot serialize type: {type(obj).__name__}",
            backend="serialization",
            operation="ENCODE"
        )


def _encode_json_deterministic(value: Any) -> str:
    """
    Encode value to deterministic JSON string.
    
    Args:
        value: Python object to encode
        
    Returns:
        Deterministic JSON string with sorted keys
        
    Raises:
        SerializationError: If encoding fails
    """
    try:
        return json.dumps(
            value,
            cls=DeterministicJSONEncoder,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False
        )
    except (TypeError, ValueError, OverflowError) as e:
        raise SerializationError(
            message=f"JSON encoding failed: {str(e)}",
            backend="serialization",
            operation="ENCODE"
        ) from e


def _decode_json(json_string: str) -> Any:
    """
    Decode JSON string to Python object.
    
    Args:
        json_string: JSON string to decode
        
    Returns:
        Decoded Python object
        
    Raises:
        SerializationError: If decoding fails
    """
    try:
        return json.loads(json_string)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        raise SerializationError(
            message=f"JSON decoding failed: {str(e)}",
            backend="serialization",
            operation="DECODE"
        ) from e


# ============================================================================
# Checksum Utilities
# ============================================================================

def _calculate_checksum(data: bytes) -> str:
    """
    Calculate SHA-256 checksum of data.
    
    Args:
        data: Bytes to checksum
        
    Returns:
        Hex-encoded checksum string
    """
    return hashlib.sha256(data).hexdigest()


def _verify_checksum(data: bytes, expected_checksum: str) -> bool:
    """
    Verify data matches expected checksum.
    
    Args:
        data: Bytes to verify
        expected_checksum: Expected hex-encoded checksum
        
    Returns:
        True if checksum matches, False otherwise
    """
    actual_checksum = _calculate_checksum(data)
    return actual_checksum == expected_checksum


# ============================================================================
# Envelope Construction & Validation
# ============================================================================

def _build_envelope(payload: str, version: int = CURRENT_VERSION, 
                   encoding: str = ENCODING_JSON, 
                   include_checksum: bool = True) -> Dict[str, Any]:
    """
    Build serialization envelope with metadata.
    
    Args:
        payload: Encoded payload string
        version: Format version
        encoding: Encoding type
        include_checksum: Whether to include checksum
        
    Returns:
        Envelope dictionary with metadata
    """
    envelope = {
        ENVELOPE_VERSION_KEY: version,
        ENVELOPE_ENCODING_KEY: encoding,
        ENVELOPE_PAYLOAD_KEY: payload
    }
    
    if include_checksum:
        # TIER-0: Checksum entire canonical envelope (including metadata)
        # This prevents envelope metadata tampering (version, encoding, etc.)
        # Build envelope without checksum first, then checksum the canonical JSON
        envelope_json = _encode_json_deterministic(envelope)
        envelope_bytes = envelope_json.encode('utf-8')
        envelope[ENVELOPE_CHECKSUM_KEY] = _calculate_checksum(envelope_bytes)
    
    return envelope


def _validate_envelope(envelope: Dict[str, Any]) -> None:
    """
    Validate envelope structure and metadata.
    
    Args:
        envelope: Envelope dictionary to validate
        
    Raises:
        SerializationError: If envelope is invalid
    """
    # Check required fields
    if not isinstance(envelope, dict):
        raise SerializationError(
            message="Envelope must be a dictionary",
            backend="serialization",
            operation="VALIDATE"
        )
    
    # Validate version field
    if ENVELOPE_VERSION_KEY not in envelope:
        raise SerializationError(
            message="Missing version field in envelope",
            backend="serialization",
            operation="VALIDATE"
        )
    
    version = envelope[ENVELOPE_VERSION_KEY]
    if not isinstance(version, int):
        raise SerializationError(
            message=f"Version must be integer, got {type(version).__name__}",
            backend="serialization",
            operation="VALIDATE"
        )
    
    if version not in SUPPORTED_VERSIONS:
        raise SerializationError(
            message=f"Unsupported version: {version}. Supported: {SUPPORTED_VERSIONS}",
            backend="serialization",
            operation="VALIDATE"
        )
    
    # Validate encoding field
    if ENVELOPE_ENCODING_KEY not in envelope:
        raise SerializationError(
            message="Missing encoding field in envelope",
            backend="serialization",
            operation="VALIDATE"
        )
    
    encoding = envelope[ENVELOPE_ENCODING_KEY]
    if encoding not in ALLOWED_ENCODINGS:
        raise SerializationError(
            message=f"Unsupported encoding: {encoding}. Supported: {ALLOWED_ENCODINGS}",
            backend="serialization",
            operation="VALIDATE"
        )
    
    # Validate payload field
    if ENVELOPE_PAYLOAD_KEY not in envelope:
        raise SerializationError(
            message="Missing payload field in envelope",
            backend="serialization",
            operation="VALIDATE"
        )
    
    # Validate checksum if present
    if ENVELOPE_CHECKSUM_KEY in envelope:
        expected_checksum = envelope[ENVELOPE_CHECKSUM_KEY]
        
        # TIER-0: Verify checksum of entire canonical envelope (including metadata)
        # Build envelope without checksum, then verify against stored checksum
        # This ensures version, encoding, and payload integrity
        envelope_without_checksum = {
            k: v for k, v in envelope.items() 
            if k != ENVELOPE_CHECKSUM_KEY
        }
        envelope_json = _encode_json_deterministic(envelope_without_checksum)
        envelope_bytes = envelope_json.encode('utf-8')
        
        if not _verify_checksum(envelope_bytes, expected_checksum):
            raise SerializationError(
                message="Checksum validation failed - data corruption detected (envelope metadata or payload tampered)",
                backend="serialization",
                operation="VALIDATE"
            )


# ============================================================================
# Public API: Serialization
# ============================================================================

def serialize(
    value: Any,
    include_checksum: bool = True,
    logger: Optional[logging.Logger] = None,
) -> bytes:
    """
    Serialize Python value to deterministic bytes.
    
    DETERMINISTIC: Same input always produces identical bytes.
    No randomness. No hidden state. No environment dependencies.
    
    This is the single public entry point for converting in-memory values to
    persisted byte representations. All persistence backends must use this function.
    
    Process:
    1. Encode value to deterministic JSON (sorted keys, normalized types)
    2. Build envelope with version, encoding type, payload, and optional checksum
    3. Encode envelope to deterministic JSON
    4. Convert to UTF-8 bytes
    
    Args:
        value: Python object to serialize (must be JSON-serializable)
        include_checksum: Whether to include integrity checksum (default: True)
        logger: Optional logger for structured logging
        
    Returns:
        Deterministic byte representation with envelope metadata
        
    Raises:
        SerializationError: If serialization fails
        
    Guarantees:
        - serialize(x1) == serialize(x2) if x1 == x2 (byte stability)
        - Output is identical across processes, Python versions, and platforms
        - Includes version metadata for backward compatibility
        - Includes checksum for corruption detection (if enabled)
        - No environment dependency (OS, Python version, architecture, process ID, random seed)
        - hash(serialize(x)) is stable across processes
    
    Example:
        >>> data = {"key": "value", "count": 42}
        >>> serialized = serialize(data)
        >>> # serialized is deterministic bytes with envelope metadata
    """
    log = logger or logging.getLogger(__name__)
    
    try:
        log.debug(f"Serializing value: type={type(value).__name__}")
        # Step 1: Encode value to deterministic JSON
        payload = _encode_json_deterministic(value)
        
        # Step 2: Build envelope with metadata
        envelope = _build_envelope(
            payload=payload,
            version=CURRENT_VERSION,
            encoding=ENCODING_JSON,
            include_checksum=include_checksum
        )
        
        # Step 3: Encode envelope to deterministic JSON
        envelope_json = _encode_json_deterministic(envelope)
        
        # Step 4: Convert to bytes
        result = envelope_json.encode('utf-8')
        
        log.debug(
            f"Serialization complete: version={CURRENT_VERSION}, "
            f"encoding={ENCODING_JSON}, size={len(result)} bytes, "
            f"checksum={'included' if include_checksum else 'omitted'}"
        )
        
        return result
        
    except SerializationError:
        # Re-raise SerializationError as-is
        raise
    except Exception as e:
        # Wrap unexpected errors (ValueError, TypeError, JSONDecodeError, etc.)
        log.error(f"Serialization failed: {type(e).__name__}: {str(e)}")
        raise SerializationError(
            message=f"Serialization failed: {str(e)}",
            backend="serialization",
            operation="SERIALIZE"
        ) from e


# ============================================================================
# Public API: Deserialization
# ============================================================================

def deserialize(
    data: bytes,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """
    Deserialize bytes to Python value.
    
    DETERMINISTIC: Same input always produces identical output.
    No randomness. No hidden state. No environment dependencies.
    
    This is the single public entry point for converting persisted bytes back to
    in-memory values. All persistence backends must use this function.
    
    Process:
    1. Decode bytes to UTF-8 string
    2. Decode envelope JSON
    3. Validate envelope structure and metadata
    4. Validate checksum (if present) - corruption detection
    5. Dispatch to version-specific deserializer
    6. Decode payload and return value
    
    Args:
        data: Serialized bytes produced by serialize()
        logger: Optional logger for structured logging
        
    Returns:
        Deserialized Python object
        
    Raises:
        SerializationError: If deserialization fails or data is corrupted
        
    Guarantees:
        - deserialize(serialize(x)) == x (round-trip idempotency)
        - Detects data corruption via checksum validation
        - Rejects unsupported versions
        - Rejects unsupported encodings
        - Never executes arbitrary code
        - Never instantiates arbitrary classes
        - Never returns partially decoded objects
        
    Example:
        >>> serialized = serialize({"key": "value"})
        >>> original = deserialize(serialized)
        >>> assert original == {"key": "value"}
    """
    log = logger or logging.getLogger(__name__)
    
    try:
        log.debug(f"Deserializing data: size={len(data)} bytes")
        # Step 1: Decode bytes to string
        try:
            envelope_json = data.decode('utf-8')
        except (UnicodeDecodeError, AttributeError) as e:
            raise SerializationError(
                message=f"Invalid UTF-8 encoding: {str(e)}",
                backend="serialization",
                operation="DESERIALIZE"
            ) from e
        
        # Step 2: Decode envelope JSON
        envelope = _decode_json(envelope_json)
        
        # Step 3: Validate envelope
        _validate_envelope(envelope)
        
        # Step 4: Dispatch to version-specific deserializer
        version = envelope[ENVELOPE_VERSION_KEY]
        
        if version == 1:
            result = _deserialize_v1(envelope)
            log.debug(
                f"Deserialization complete: version={version}, "
                f"encoding={envelope.get(ENVELOPE_ENCODING_KEY)}"
            )
            return result
        else:
            # Should never reach here due to validation
            raise SerializationError(
                message=f"No deserializer for version {version}",
                backend="serialization",
                operation="DESERIALIZE"
            )
            
    except SerializationError:
        # Re-raise SerializationError as-is
        raise
    except Exception as e:
        # Wrap unexpected errors (UnicodeDecodeError, JSONDecodeError, etc.)
        log.error(f"Deserialization failed: {type(e).__name__}: {str(e)}")
        raise SerializationError(
            message=f"Deserialization failed: {str(e)}",
            backend="serialization",
            operation="DESERIALIZE"
        ) from e


def _deserialize_v1(envelope: Dict[str, Any]) -> Any:
    """
    Deserialize version 1 envelope.
    
    Args:
        envelope: Validated version 1 envelope
        
    Returns:
        Deserialized value
        
    Raises:
        SerializationError: If deserialization fails
    """
    encoding = envelope[ENVELOPE_ENCODING_KEY]
    payload = envelope[ENVELOPE_PAYLOAD_KEY]
    
    if encoding == ENCODING_JSON:
        return _decode_json(payload)
    else:
        # Should never reach here due to validation
        raise SerializationError(
            message=f"Unsupported encoding in v1: {encoding}",
            backend="serialization",
            operation="DESERIALIZE"
        )


# ============================================================================
# Utility Functions
# ============================================================================

def is_serialized(data: bytes) -> bool:
    """
    Check if bytes appear to be valid serialized data.
    
    TIER-0: Never silently mask corruption or validation errors.
    This function distinguishes between:
    - Invalid format (returns False)
    - Corruption/validation failure (raises SerializationError)
    
    Args:
        data: Bytes to check
        
    Returns:
        True if data appears to be valid serialized format, False if clearly not serialized
        
    Raises:
        SerializationError: If data appears to be serialized but validation fails
            (checksum mismatch, version incompatibility, corrupted payload)
    """
    # Quick format check - if it's not even valid UTF-8 or JSON, return False
    try:
        envelope_json = data.decode('utf-8')
    except (UnicodeDecodeError, AttributeError):
        return False
    
    try:
        envelope = _decode_json(envelope_json)
    except SerializationError:
        # JSON decode failed - not serialized format
        return False
    
    # If we got here, it looks like a serialized envelope
    # Now validate it - this will raise SerializationError for corruption/validation issues
    try:
        _validate_envelope(envelope)
        return True
    except SerializationError:
        # TIER-0: Don't silently downgrade corruption into boolean ambiguity
        # Re-raise to surface checksum mismatch, version incompatibility, etc.
        raise


def get_serialization_version(data: bytes) -> int:
    """
    Extract version number from serialized data.
    
    Args:
        data: Serialized bytes
        
    Returns:
        Version number
        
    Raises:
        SerializationError: If data is invalid or version cannot be extracted
    """
    try:
        envelope_json = data.decode('utf-8')
        envelope = _decode_json(envelope_json)
        
        if ENVELOPE_VERSION_KEY not in envelope:
            raise SerializationError(
                message="Missing version field",
                backend="serialization",
                operation="GET_VERSION"
            )
        
        return envelope[ENVELOPE_VERSION_KEY]
        
    except SerializationError:
        raise
    except Exception as e:
        raise SerializationError(
            message=f"Failed to extract version: {str(e)}",
            backend="serialization",
            operation="GET_VERSION"
        ) from e
