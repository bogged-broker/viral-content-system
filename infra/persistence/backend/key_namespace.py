"""
Deterministic Key Namespace Authority (Collision Isolation & Structural Guarantees)

This module is the single authority defining how persistence keys are structured,
partitioned, and isolated across the system. It prevents collisions across domains,
environments, and lifecycles.

This file does NOT:
- Talk to a backend
- Write or read data
- Store values
- Contain business logic

It strictly:
- Defines canonical key structure
- Enforces namespacing rules
- Prevents collision classes
- Encodes deterministic partition boundaries

ARCHITECTURAL NOTE:
This file combines namespace schema authority with parsing and validation utilities.
For strict Tier-0 architectural purity, consider splitting into:
- key_schema.py (canonical structure definition)
- key_parser.py (parsing and validation)
- key_collision_guard.py (operational collision checks)

The current combined approach prioritizes cohesion and convenience over strict
separation of concerns.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional, List, Dict, Any, Type
from dataclasses import dataclass


# ============================================================================
# Constants
# ============================================================================

# Immutable separator - NEVER change without versioning
KEY_SEPARATOR = ":"

# Legal character pattern for key segments
LEGAL_SEGMENT_PATTERN = re.compile(r'^[a-z0-9_-]+$')

# Maximum segment lengths to prevent abuse
MAX_SEGMENT_LENGTH = 255
MAX_TOTAL_KEY_LENGTH = 2048


# ============================================================================
# Enumerations
# ============================================================================

class Environment(Enum):
    """
    Environment isolation boundary.
    All keys must declare environment - zero exceptions.
    """
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"
    REPLAY = "replay"
    TEST = "test"


class Domain(Enum):
    """
    Domain isolation boundary.
    Prevents semantic collisions across functional areas.
    """
    INGESTION = "ingestion"
    AGGREGATION = "aggregation"
    EVALUATION = "evaluation"
    CHECKPOINT = "checkpoint"
    REPLAY = "replay"
    METRICS = "metrics"
    EXPERIMENTS = "experiments"
    CONTENT = "content"
    STATE = "state"


class EntityType(Enum):
    """
    Entity type classification.
    Prevents semantic ambiguity within domains.
    """
    EVENT = "event"
    CONTENT = "content"
    METRIC = "metric"
    COUNTER = "counter"
    SNAPSHOT = "snapshot"
    CHECKPOINT = "checkpoint"
    AGGREGATE = "aggregate"
    WINDOW = "window"
    FACT = "fact"
    INDEX = "index"


# ============================================================================
# Exceptions
# ============================================================================

class KeyNamespaceError(Exception):
    """Base exception for key namespace violations."""
    pass


class InvalidSegmentError(KeyNamespaceError):
    """Segment contains illegal characters or format."""
    def __init__(self, segment: str, position: int, reason: str):
        super().__init__(
            f"Invalid segment at position {position}: '{segment}' - {reason}"
        )
        self.segment = segment
        self.position = position
        self.reason = reason


class InvalidKeyStructureError(KeyNamespaceError):
    """Key does not conform to canonical structure."""
    def __init__(self, key: str, reason: str):
        super().__init__(f"Invalid key structure: '{key}' - {reason}")
        self.key = key
        self.reason = reason


class SegmentTooLongError(KeyNamespaceError):
    """Segment exceeds maximum length."""
    def __init__(self, segment: str, max_length: int):
        super().__init__(
            f"Segment '{segment[:50]}...' exceeds maximum length {max_length}"
        )
        self.segment = segment
        self.max_length = max_length


class KeyTooLongError(KeyNamespaceError):
    """Complete key exceeds maximum length."""
    def __init__(self, key: str, max_length: int):
        super().__init__(
            f"Key '{key[:100]}...' exceeds maximum length {max_length}"
        )
        self.key = key
        self.max_length = max_length


class EmptySegmentError(KeyNamespaceError):
    """Segment is empty when value required."""
    def __init__(self, position: int):
        super().__init__(f"Empty segment at position {position}")
        self.position = position


class InvalidEnumValueError(KeyNamespaceError):
    """String value does not match any enum member."""
    def __init__(self, value: str, enum_type: str, valid_values: List[str]):
        super().__init__(
            f"Invalid {enum_type} value: '{value}'. Must be one of: {', '.join(valid_values)}"
        )
        self.value = value
        self.enum_type = enum_type
        self.valid_values = valid_values


# ============================================================================
# Key Components
# ============================================================================

@dataclass(frozen=True)
class KeyComponents:
    """
    Parsed key components.
    Immutable representation of a structured key.
    """
    environment: str
    domain: str
    subdomain: Optional[str]
    entity_type: str
    entity_id: str
    version: Optional[str]
    shard: Optional[str]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "environment": self.environment,
            "domain": self.domain,
            "subdomain": self.subdomain,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "version": self.version,
            "shard": self.shard,
        }


# ============================================================================
# Key Builder
# ============================================================================

class KeyBuilder:
    """
    Canonical key construction authority.
    
    Enforces:
    - Deterministic segment ordering
    - Validation before construction
    - No silent transformations
    - Collision-proof addressing
    - Canonical enum enforcement (no arbitrary strings)
    """
    
    @staticmethod
    def _validate_enum_value(value: str, enum_class: Type[Enum], enum_name: str) -> str:
        """
        Validate that a string value matches an enum member.
        
        Raises:
            InvalidEnumValueError: If value does not match any enum member
        """
        valid_values = [e.value for e in enum_class]
        if value not in valid_values:
            raise InvalidEnumValueError(value, enum_name, valid_values)
        return value
    
    @staticmethod
    def _normalize_environment(environment: Environment | str) -> str:
        """Normalize environment to canonical string, validating enum membership."""
        if isinstance(environment, Environment):
            return environment.value
        # Validate string matches enum
        return KeyBuilder._validate_enum_value(environment, Environment, "Environment")
    
    @staticmethod
    def _normalize_domain(domain: Domain | str) -> str:
        """Normalize domain to canonical string, validating enum membership."""
        if isinstance(domain, Domain):
            return domain.value
        # Validate string matches enum
        return KeyBuilder._validate_enum_value(domain, Domain, "Domain")
    
    @staticmethod
    def _normalize_entity_type(entity_type: EntityType | str) -> str:
        """Normalize entity_type to canonical string, validating enum membership."""
        if isinstance(entity_type, EntityType):
            return entity_type.value
        # Validate string matches enum
        return KeyBuilder._validate_enum_value(entity_type, EntityType, "EntityType")
    
    @staticmethod
    def build_key(
        environment: Environment | str,
        domain: Domain | str,
        entity_type: EntityType | str,
        entity_id: str,
        subdomain: Optional[str] = None,
        version: Optional[str] = None,
        shard: Optional[str] = None
    ) -> str:
        """
        Build canonical key from components.
        
        Canonical structure:
        {env}:{domain}:{subdomain}:{entity_type}:{entity_id}:{version}:{shard}
        
        Args:
            environment: Execution environment (required, must be Environment enum or valid enum value)
            domain: Functional domain (required, must be Domain enum or valid enum value)
            entity_type: Entity classification (required, must be EntityType enum or valid enum value)
            entity_id: Deterministic entity identifier (required)
            subdomain: Optional domain subdivision (explicit None or empty string for empty segment)
            version: Optional schema/format version (explicit None or empty string for empty segment)
            shard: Optional deterministic shard identifier (explicit None or empty string for empty segment)
            
        Returns:
            Canonical key string
            
        Raises:
            KeyNamespaceError: On validation failure
            InvalidEnumValueError: If string values do not match enum members
        """
        # Normalize and validate enum values (enforces canonical vocabulary)
        env_str = KeyBuilder._normalize_environment(environment)
        domain_str = KeyBuilder._normalize_domain(domain)
        entity_type_str = KeyBuilder._normalize_entity_type(entity_type)
        
        # Empty segment handling for canonical 7-segment structure
        # The canonical key format requires exactly 7 segments, so optional segments
        # (subdomain, version, shard) must be represented as empty strings when absent.
        # This conversion from None to "" is a structural requirement, not an implicit default.
        # For maximum explicitness, callers can pass "" directly instead of None.
        subdomain_segment = "" if subdomain is None else subdomain
        version_segment = "" if version is None else version
        shard_segment = "" if shard is None else shard
        
        # Build ordered segment list
        segments = [
            env_str,
            domain_str,
            subdomain_segment,
            entity_type_str,
            entity_id,
            version_segment,
            shard_segment,
        ]
        
        # Validate each segment
        for i, segment in enumerate(segments):
            if segment:  # Only validate non-empty segments
                KeyBuilder._validate_segment(segment, i)
        
        # Validate required segments are not empty
        if not env_str:
            raise EmptySegmentError(position=0)
        if not domain_str:
            raise EmptySegmentError(position=1)
        if not entity_type_str:
            raise EmptySegmentError(position=3)
        if not entity_id:
            raise EmptySegmentError(position=4)
        
        # Construct key
        key = KEY_SEPARATOR.join(segments)
        
        # Validate total length
        if len(key) > MAX_TOTAL_KEY_LENGTH:
            raise KeyTooLongError(key, MAX_TOTAL_KEY_LENGTH)
        
        return key
    
    @staticmethod
    def build_ingestion_key(
        environment: Environment | str,
        entity_id: str,
        version: Optional[str] = None
    ) -> str:
        """Build key for ingestion domain."""
        return KeyBuilder.build_key(
            environment=environment,
            domain=Domain.INGESTION,
            entity_type=EntityType.EVENT,
            entity_id=entity_id,
            version=version
        )
    
    @staticmethod
    def build_aggregation_key(
        environment: Environment | str,
        window_id: str,
        subdomain: Optional[str] = None,
        version: Optional[str] = None
    ) -> str:
        """Build key for aggregation domain."""
        return KeyBuilder.build_key(
            environment=environment,
            domain=Domain.AGGREGATION,
            subdomain=subdomain,
            entity_type=EntityType.AGGREGATE,
            entity_id=window_id,
            version=version
        )
    
    @staticmethod
    def build_checkpoint_key(
        environment: Environment | str,
        checkpoint_id: str,
        version: Optional[str] = None
    ) -> str:
        """Build key for checkpoint domain."""
        return KeyBuilder.build_key(
            environment=environment,
            domain=Domain.CHECKPOINT,
            entity_type=EntityType.CHECKPOINT,
            entity_id=checkpoint_id,
            version=version
        )
    
    @staticmethod
    def build_metric_key(
        environment: Environment | str,
        metric_id: str,
        subdomain: Optional[str] = None,
        version: Optional[str] = None
    ) -> str:
        """Build key for metrics domain."""
        return KeyBuilder.build_key(
            environment=environment,
            domain=Domain.METRICS,
            subdomain=subdomain,
            entity_type=EntityType.METRIC,
            entity_id=metric_id,
            version=version
        )
    
    @staticmethod
    def build_replay_key(
        replay_id: str,
        domain: Domain | str,
        entity_type: EntityType | str,
        entity_id: str,
        version: Optional[str] = None
    ) -> str:
        """
        Build key for replay domain.
        
        Replay keys are ALWAYS in replay environment to prevent
        contamination of live data.
        """
        return KeyBuilder.build_key(
            environment=Environment.REPLAY,
            domain=domain,
            subdomain=replay_id,  # Use subdomain for replay isolation
            entity_type=entity_type,
            entity_id=entity_id,
            version=version
        )
    
    @staticmethod
    def build_sharded_key(
        environment: Environment | str,
        domain: Domain | str,
        entity_type: EntityType | str,
        entity_id: str,
        shard_count: int,
        version: Optional[str] = None
    ) -> str:
        """
        Build sharded key with deterministic shard assignment.
        
        Shard is derived from entity_id for determinism.
        """
        # Deterministic shard assignment
        shard_id = KeyBuilder._compute_shard(entity_id, shard_count)
        
        return KeyBuilder.build_key(
            environment=environment,
            domain=domain,
            entity_type=entity_type,
            entity_id=entity_id,
            version=version,
            shard=f"shard-{shard_id}"
        )
    
    @staticmethod
    def _validate_segment(segment: str, position: int) -> None:
        """
        Validate individual segment.
        
        Rules:
        - Legal characters only (lowercase alphanumeric, hyphen, underscore)
        - No whitespace
        - No control characters
        - No newlines
        - Maximum length
        - No path traversal symbols
        - No backend-reserved tokens
        - No unsafe bytes
        """
        # Check length
        if len(segment) > MAX_SEGMENT_LENGTH:
            raise SegmentTooLongError(segment, MAX_SEGMENT_LENGTH)
        
        # Explicitly check for whitespace (spaces, tabs, newlines)
        # This catches all whitespace including newlines
        if any(c.isspace() for c in segment):
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason="Contains whitespace (spaces, tabs, newlines not allowed)"
            )
        
        # Explicitly check for non-printable control characters (excluding whitespace already checked)
        # Control characters are ord < 32, but whitespace (space=32, tab=9, newline=10, cr=13) already checked
        if any(ord(c) < 32 for c in segment):
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason="Contains control characters"
            )
        
        # Check legal characters (regex pattern enforces lowercase alphanumeric + hyphen/underscore)
        if not LEGAL_SEGMENT_PATTERN.match(segment):
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason="Contains illegal characters (only lowercase a-z, 0-9, -, _ allowed)"
            )
        
        # Explicitly forbid dangerous patterns
        if '..' in segment:
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason="Path traversal pattern detected"
            )
        
        if '/' in segment or '\\' in segment:
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason="Path separator detected"
            )
        
        # Check for backend-reserved tokens (common patterns that backends might reserve)
        # This is a conservative list - backends should document their reserved tokens
        reserved_patterns = ['null', 'none', 'true', 'false']  # Common reserved values
        if segment.lower() in reserved_patterns:
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason=f"Reserved token detected: '{segment}'"
            )
        
        # Check for unsafe bytes (non-ASCII characters)
        try:
            segment.encode('ascii')
        except UnicodeEncodeError:
            raise InvalidSegmentError(
                segment=segment,
                position=position,
                reason="Contains non-ASCII characters (unsafe bytes)"
            )
    
    @staticmethod
    def _compute_shard(entity_id: str, shard_count: int) -> int:
        """
        Compute deterministic shard ID from entity_id.
        
        Uses simple hash modulo for stable distribution.
        """
        # Use Python's built-in hash for determinism within same runtime
        # For cross-runtime stability, use explicit hash like hashlib
        import hashlib
        hash_bytes = hashlib.sha256(entity_id.encode('utf-8')).digest()
        hash_int = int.from_bytes(hash_bytes[:8], byteorder='big')
        return hash_int % shard_count

# ============================================================================
# Key Parser
# ============================================================================

class KeyParser:
    """
    Canonical key parsing and validation authority.
    
    Validates structure and extracts components.
    """
    
    @staticmethod
    def parse_key(key: str) -> KeyComponents:
        """
        Parse key into components.
        
        Args:
            key: Canonical key string
            
        Returns:
            KeyComponents with extracted segments
            
        Raises:
            InvalidKeyStructureError: If key is malformed
        """
        # Validate key not empty
        if not key:
            raise InvalidKeyStructureError(key, "Empty key")
        
        # Validate total length
        if len(key) > MAX_TOTAL_KEY_LENGTH:
            raise KeyTooLongError(key, MAX_TOTAL_KEY_LENGTH)
        
        # Split into segments
        segments = key.split(KEY_SEPARATOR)
        
        # Validate segment count (7 segments in canonical structure)
        if len(segments) != 7:
            raise InvalidKeyStructureError(
                key,
                f"Expected 7 segments, got {len(segments)}"
            )
        
        # Extract components
        environment = segments[0]
        domain = segments[1]
        subdomain = segments[2] if segments[2] else None
        entity_type = segments[3]
        entity_id = segments[4]
        version = segments[5] if segments[5] else None
        shard = segments[6] if segments[6] else None
        
        # Validate required segments are not empty
        if not environment:
            raise InvalidKeyStructureError(key, "Empty environment segment")
        if not domain:
            raise InvalidKeyStructureError(key, "Empty domain segment")
        if not entity_type:
            raise InvalidKeyStructureError(key, "Empty entity_type segment")
        if not entity_id:
            raise InvalidKeyStructureError(key, "Empty entity_id segment")
        
        # Validate each segment
        for i, segment in enumerate(segments):
            if segment:  # Only validate non-empty segments
                KeyBuilder._validate_segment(segment, i)
        
        return KeyComponents(
            environment=environment,
            domain=domain,
            subdomain=subdomain,
            entity_type=entity_type,
            entity_id=entity_id,
            version=version,
            shard=shard
        )
    
    @staticmethod
    def validate_key(key: str) -> None:
        """
        Validate key structure.
        
        Raises:
            KeyNamespaceError: On validation failure
        """
        # Validation happens in parse_key
        KeyParser.parse_key(key)
    
    @staticmethod
    def extract_environment(key: str) -> str:
        """Extract environment from key."""
        components = KeyParser.parse_key(key)
        return components.environment
    
    @staticmethod
    def extract_domain(key: str) -> str:
        """Extract domain from key."""
        components = KeyParser.parse_key(key)
        return components.domain
    
    @staticmethod
    def extract_version(key: str) -> Optional[str]:
        """Extract version from key."""
        components = KeyParser.parse_key(key)
        return components.version
    
    @staticmethod
    def extract_entity_id(key: str) -> str:
        """Extract entity ID from key."""
        components = KeyParser.parse_key(key)
        return components.entity_id
    
    @staticmethod
    def extract_shard(key: str) -> Optional[str]:
        """Extract shard from key."""
        components = KeyParser.parse_key(key)
        return components.shard


# ============================================================================
# Collision Guarantees
# ============================================================================

class CollisionGuard:
    """
    Operational collision validation utilities.
    
    NOTE: Structural collision prevention is guaranteed by the canonical
    key structure itself (distinct segments: env, domain, subdomain, etc.).
    This class provides operational utilities for validating isolation
    boundaries at runtime, but is not required for namespace authority purity.
    
    Use cases:
    - Runtime validation of isolation boundaries
    - Debugging namespace collisions
    - Operational safety checks
    
    The canonical key structure already prevents collisions structurally:
    {env}:{domain}:{subdomain}:{entity_type}:{entity_id}:{version}:{shard}
    
    This class adds operational convenience, not structural guarantees.
    """
    
    @staticmethod
    def check_collision(key1: str, key2: str) -> bool:
        """
        Check if two keys are structurally identical (collision).
        
        Structural collision is prevented by the canonical key structure
        which enforces distinct segments. This method validates that
        two keys are byte-for-byte identical.
        
        Returns:
            True if keys are identical (collision), False otherwise
        """
        # Parse both keys to ensure structural validity
        KeyParser.parse_key(key1)
        KeyParser.parse_key(key2)
        return key1 == key2
    
    @staticmethod
    def check_environment_isolation(key1: str, key2: str) -> bool:
        """
        Check if keys are in different environments.
        
        Returns:
            True if environments differ (isolated), False if same
        """
        env1 = KeyParser.extract_environment(key1)
        env2 = KeyParser.extract_environment(key2)
        return env1 != env2
    
    @staticmethod
    def check_domain_isolation(key1: str, key2: str) -> bool:
        """
        Check if keys are in different domains.
        
        Returns:
            True if domains differ (isolated), False if same
        """
        domain1 = KeyParser.extract_domain(key1)
        domain2 = KeyParser.extract_domain(key2)
        return domain1 != domain2
    
    @staticmethod
    def check_version_isolation(key1: str, key2: str) -> bool:
        """
        Check if keys are in different versions.
        
        Returns:
            True if versions differ (isolated), False if same or both None
        """
        v1 = KeyParser.extract_version(key1)
        v2 = KeyParser.extract_version(key2)
        
        # If both have no version, not isolated
        if v1 is None and v2 is None:
            return False
        
        # If only one has version, isolated
        if v1 is None or v2 is None:
            return True
        
        # Both have versions, check if different
        return v1 != v2
    
    @staticmethod
    def is_replay_key(key: str) -> bool:
        """
        Check if key is in replay environment.
        
        Returns:
            True if key is in replay environment
        """
        env = KeyParser.extract_environment(key)
        return env == Environment.REPLAY.value
    
    @staticmethod
    def verify_replay_isolation(replay_key: str, live_key: str) -> None:
        """
        Verify replay key cannot collide with live key.
        
        Raises:
            InvalidKeyStructureError: If replay key not properly isolated
        """
        if not CollisionGuard.is_replay_key(replay_key):
            raise InvalidKeyStructureError(
                replay_key,
                "Replay key must be in replay environment"
            )
        
        if CollisionGuard.check_collision(replay_key, live_key):
            raise InvalidKeyStructureError(
                replay_key,
                f"Replay key collides with live key: {live_key}"
            )


# ============================================================================
# Key Namespace Validator (Protocol Implementation)
# ============================================================================

class KeyNamespaceValidator:
    """
    Primary validator implementing the validation protocol.
    
    This is the main interface for external consumers.
    """
    
    @staticmethod
    def validate_key_format(key: str) -> bool:
        """
        Validate key conforms to namespace rules.
        
        Returns:
            True if valid, False otherwise
        """
        try:
            KeyParser.validate_key(key)
            return True
        except KeyNamespaceError:
            return False
    
    @staticmethod
    def extract_environment(key: str) -> str:
        """Extract environment identifier from key."""
        return KeyParser.extract_environment(key)
    
    @staticmethod
    def extract_version(key: str) -> Optional[str]:
        """Extract version from key if present."""
        return KeyParser.extract_version(key)
    
    @staticmethod
    def extract_domain(key: str) -> str:
        """Extract domain identifier from key."""
        return KeyParser.extract_domain(key)
    
    @staticmethod
    def parse(key: str) -> KeyComponents:
        """Parse key into components."""
        return KeyParser.parse_key(key)


# ============================================================================
# Convenience Functions
# ============================================================================

def build_key(
    environment: Environment | str,
    domain: Domain | str,
    entity_type: EntityType | str,
    entity_id: str,
    **kwargs
) -> str:
    """
    Convenience wrapper for KeyBuilder.build_key.
    
    Note: String values for environment, domain, and entity_type must
    match valid enum values. Use enum types directly for type safety.
    """
    return KeyBuilder.build_key(
        environment=environment,
        domain=domain,
        entity_type=entity_type,
        entity_id=entity_id,
        **kwargs
    )


def parse_key(key: str) -> KeyComponents:
    """Convenience wrapper for KeyParser.parse_key."""
    return KeyParser.parse_key(key)


def validate_key(key: str) -> None:
    """Convenience wrapper for KeyParser.validate_key."""
    KeyParser.validate_key(key)
