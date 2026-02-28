#!/usr/bin/env python3
"""
/data/pipelines/ingestion/engagement_ingest.py

Engagement → Canonical Pipeline Fact Ingestor

CRITICAL: This is the ONLY legal path for engagement signals into analytics.
Every fact that passes through this file is considered analytically valid forever.

Design Principle: Ingestion is about legitimacy, not volume.
A single poisoned event is worse than a million missing ones.
"""

import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple, Any
from collections import defaultdict
import uuid


# ============================================================================
# CONSTANTS & CONFIGURATION
# ============================================================================

PIPELINE_NAME = "engagement_ingest"
PIPELINE_VERSION = "1.0.0"

# Timestamp skew tolerance (seconds)
FUTURE_SKEW_TOLERANCE_SECONDS = 300  # 5 minutes
PAST_SKEW_TOLERANCE_SECONDS = 86400 * 365  # 1 year

# Supported schema versions
SUPPORTED_SCHEMA_VERSIONS = {"1.0", "1.1", "2.0"}

# Maximum field lengths
MAX_ID_LENGTH = 256
MAX_PLATFORM_LENGTH = 64
MAX_EVENT_TYPE_LENGTH = 32

# Emergency mode and global freeze are now controlled by EmergencyStopController
# See SafetyWatchdog class for integration


# ============================================================================
# ENUMS & TYPE DEFINITIONS
# ============================================================================

class EngagementType(Enum):
    """Explicit enumeration of all valid engagement types."""
    VIEW = "view"
    LIKE = "like"
    SHARE = "share"
    COMMENT = "comment"
    FOLLOW = "follow"
    SAVE = "save"
    COMPLETION = "completion"
    RETENTION_SIGNAL = "retention_signal"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        """Check if engagement type is valid."""
        try:
            cls(value.lower())
            return True
        except (ValueError, AttributeError):
            return False


class Platform(Enum):
    """Supported source platforms."""
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"
    INTERNAL = "internal"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        """Check if platform is valid."""
        try:
            cls(value.lower())
            return True
        except (ValueError, AttributeError):
            return False


class RejectionReason(Enum):
    """Explicit rejection reasons for audit trail."""
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_SCHEMA_VERSION = "invalid_schema_version"
    INVALID_EVENT_TYPE = "invalid_event_type"
    INVALID_PLATFORM = "invalid_platform"
    INVALID_TIMESTAMP = "invalid_timestamp"
    TIMESTAMP_FUTURE_SKEW = "timestamp_future_skew"
    TIMESTAMP_PAST_SKEW = "timestamp_past_skew"
    FIELD_LENGTH_VIOLATION = "field_length_violation"
    DUPLICATE_EVENT = "duplicate_event"
    MALFORMED_EVENT = "malformed_event"
    EMERGENCY_MODE_ACTIVE = "emergency_mode_active"
    GLOBAL_FREEZE_ACTIVE = "global_freeze_active"
    INVARIANT_VIOLATION = "invariant_violation"


# ============================================================================
# CANONICAL OUTPUT MODEL
# ============================================================================

@dataclass(frozen=True)
class EngagementFact:
    """
    Immutable, atomic, replayable engagement fact.
    
    This is the ONLY valid output from this ingestion pipeline.
    Every field is mandatory. No nulls, no optionals, no ambiguity.
    """
    # Core identity (deterministic)
    fact_id: str  # SHA-256 hash of canonical event identity
    
    # Engagement semantics
    event_type: str  # view / like / share / comment / follow / save / completion / retention_signal
    content_id: str  # Immutable content identifier
    account_id: str  # Immutable account identifier
    platform: str  # Source platform
    
    # Temporal anchors
    occurred_at: int  # Normalized UTC epoch seconds (event time)
    ingested_at: int  # Monotonic UTC epoch seconds (ingest time)
    
    # Provenance (immutable audit trail)
    source_event_id: str  # Original platform event ID
    source_schema_version: str  # Schema version at ingestion
    lineage_hash: str  # Full provenance fingerprint (deterministic)
    
    # Pipeline metadata
    ingestion_pipeline_name: str  # Always "engagement_ingest"
    ingestion_pipeline_version: str  # Semantic version
    ingestion_run_id: str  # Unique run identifier (replay context aware)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert fact to dictionary for serialization."""
        return asdict(self)
    
    def to_json(self) -> str:
        """Convert fact to JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True)


# ============================================================================
# RAW INPUT MODEL
# ============================================================================

@dataclass
class RawEngagementEvent:
    """
    Expected structure of raw engagement events.
    
    This is the contract between upstream systems and this ingestor.
    All fields are MANDATORY.
    """
    platform: str
    event_id: str
    event_type: str
    content_id: str
    account_id: str
    event_timestamp: int  # Unix epoch seconds or milliseconds
    platform_identifier: str  # Redundant validation
    schema_version: str
    
    # Optional metadata (not used in fact generation, but validated)
    metadata: Optional[Dict[str, Any]] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RawEngagementEvent':
        """Construct from dictionary with strict validation."""
        return cls(
            platform=data.get('platform', ''),
            event_id=data.get('event_id', ''),
            event_type=data.get('event_type', ''),
            content_id=data.get('content_id', ''),
            account_id=data.get('account_id', ''),
            event_timestamp=data.get('event_timestamp', 0),
            platform_identifier=data.get('platform_identifier', ''),
            schema_version=data.get('schema_version', ''),
            metadata=data.get('metadata')
        )


# ============================================================================
# AUDIT & OBSERVABILITY MODELS
# ============================================================================

@dataclass
class IngestionMetrics:
    """
    Complete audit trail for ingestion run.
    
    Silence is not allowed. Every decision is logged.
    """
    run_id: str
    started_at: int
    completed_at: Optional[int] = None
    
    # Acceptance metrics
    total_events_received: int = 0
    total_facts_accepted: int = 0
    total_events_rejected: int = 0
    total_events_deduplicated: int = 0
    
    # Rejection breakdown
    rejections_by_reason: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Platform breakdown
    accepted_by_platform: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rejected_by_platform: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Schema breakdown
    schema_version_distribution: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    schema_mismatches: int = 0
    
    # Deduplication details
    duplicates_by_platform: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    
    # Emergency conditions
    emergency_mode_triggers: int = 0
    global_freeze_triggers: int = 0
    invariant_violations: int = 0
    
    def increment_rejection(self, reason: RejectionReason, platform: Optional[str] = None):
        """Record a rejection with reason and platform."""
        self.total_events_rejected += 1
        self.rejections_by_reason[reason.value] += 1
        if platform:
            self.rejected_by_platform[platform] += 1
    
    def increment_acceptance(self, platform: str, schema_version: str):
        """Record an acceptance with platform and schema."""
        self.total_facts_accepted += 1
        self.accepted_by_platform[platform] += 1
        self.schema_version_distribution[schema_version] += 1
    
    def increment_duplicate(self, platform: str):
        """Record a duplicate detection."""
        self.total_events_deduplicated += 1
        self.duplicates_by_platform[platform] += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary."""
        return {
            'run_id': self.run_id,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'total_events_received': self.total_events_received,
            'total_facts_accepted': self.total_facts_accepted,
            'total_events_rejected': self.total_events_rejected,
            'total_events_deduplicated': self.total_events_deduplicated,
            'rejections_by_reason': dict(self.rejections_by_reason),
            'accepted_by_platform': dict(self.accepted_by_platform),
            'rejected_by_platform': dict(self.rejected_by_platform),
            'schema_version_distribution': dict(self.schema_version_distribution),
            'schema_mismatches': self.schema_mismatches,
            'duplicates_by_platform': dict(self.duplicates_by_platform),
            'emergency_mode_triggers': self.emergency_mode_triggers,
            'global_freeze_triggers': self.global_freeze_triggers,
            'invariant_violations': self.invariant_violations,
        }


@dataclass
class RejectionRecord:
    """Explicit audit record for rejected events."""
    event_id: str
    platform: str
    reason: RejectionReason
    rejected_at: int
    details: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging."""
        return {
            'event_id': self.event_id,
            'platform': self.platform,
            'reason': self.reason.value,
            'rejected_at': self.rejected_at,
            'details': self.details,
        }


# ============================================================================
# REPLAY CONTEXT
# ============================================================================

@dataclass
class ReplayContext:
    """
    Context for replay scenarios.
    
    Ensures deterministic behavior during replays.
    """
    is_replay: bool = False
    replay_run_id: Optional[str] = None
    replay_timestamp: Optional[int] = None  # Override for deterministic ingest timestamps
    
    # Replay must behave identically
    # No network calls, no mutable caches, no retries with delay
    
    def get_run_id(self) -> str:
        """Get run ID (replay-aware)."""
        if self.is_replay and self.replay_run_id:
            return self.replay_run_id
        return str(uuid.uuid4())
    
    def get_ingest_timestamp(self) -> int:
        """
        Get ingest timestamp (replay-aware).
        
        In replay mode, uses replay_timestamp for deterministic behavior.
        Otherwise, uses current wall-clock time.
        
        Returns:
            UTC epoch seconds
        """
        if self.is_replay and self.replay_timestamp is not None:
            return self.replay_timestamp
        return int(time.time())


# ============================================================================
# DEDUPLICATION ENGINE
# ============================================================================

class DeduplicationEngine:
    """
    Event-identity-level deduplication with distributed store.
    
    TIER-0 UPGRADE: Now uses distributed EventIdentityStore for global
    "first-seen wins" semantics across workers, regions, and restarts.
    
    Rules:
    - Dedup key: (platform, source_event_id, event_type)
    - First-seen wins (globally, not per-process)
    - Drops are explicit and audited
    - NO temporal deduplication
    - NO aggregation logic
    """
    
    def __init__(
        self,
        identity_store: Optional['EventIdentityStore'] = None,
        ingestion_run_id: Optional[str] = None
    ):
        """
        Initialize deduplication engine.
        
        Args:
            identity_store: Optional distributed EventIdentityStore
                           If None, falls back to in-memory (single-process only)
            ingestion_run_id: Optional ingestion run ID for provenance
        """
        # Import here to avoid circular dependency
        try:
            from infra.idempotency.event_identity_store import (
                EventIdentityStore,
                create_event_identity_store,
                IdempotencyResult
            )
        except ImportError:
            # Fallback if idempotency store not available
            EventIdentityStore = None
            create_event_identity_store = None
            IdempotencyResult = None
        
        self._identity_store = identity_store
        self._ingestion_run_id = ingestion_run_id
        
        # Fallback: in-memory set for single-process scenarios
        # This is NOT Tier-0 safe for distributed ingestion
        self._seen_events: Set[str] = set()
        self._use_distributed = identity_store is not None
        
        self._logger = logging.getLogger(f"{__name__}.DeduplicationEngine")
        
        if not self._use_distributed:
            self._logger.warning(
                "DeduplicationEngine using in-memory store - "
                "NOT safe for distributed ingestion. Use EventIdentityStore for Tier-0."
            )
    
    def _compute_dedup_key(self, platform: str, event_id: str, event_type: str) -> str:
        """
        Compute deterministic deduplication key.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            
        Returns:
            Deterministic dedup key
        """
        # Normalize inputs for consistency
        normalized_platform = platform.lower().strip()
        normalized_event_id = event_id.strip()
        normalized_event_type = event_type.lower().strip()
        
        # Create canonical representation
        key_material = f"{normalized_platform}::{normalized_event_id}::{normalized_event_type}"
        
        # Hash for efficiency and privacy
        return hashlib.sha256(key_material.encode('utf-8')).hexdigest()
    
    def is_duplicate(
        self,
        platform: str,
        event_id: str,
        event_type: str,
        first_seen_at: Optional[int] = None
    ) -> bool:
        """
        Check if event is a duplicate.
        
        TIER-0: Uses distributed store for global consistency.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            first_seen_at: Optional timestamp for first-seen (for replay determinism)
            
        Returns:
            True if duplicate, False if first-seen
        """
        if self._use_distributed:
            # Use distributed store for global deduplication
            from infra.idempotency.event_identity_store import IdempotencyResult
            
            result, record = self._identity_store.check_and_record(
                platform=platform,
                event_id=event_id,
                event_type=event_type,
                ingestion_run_id=self._ingestion_run_id,
                first_seen_at=first_seen_at
            )
            
            is_dup = result == IdempotencyResult.DUPLICATE
            
            if is_dup:
                self._logger.debug(
                    f"Duplicate detected (distributed): platform={platform}, "
                    f"event_id={event_id}, event_type={event_type}, "
                    f"first_seen_at={record.first_seen_at if record else None}"
                )
            
            return is_dup
        else:
            # Fallback: in-memory deduplication (single-process only)
            dedup_key = self._compute_dedup_key(platform, event_id, event_type)
            
            if dedup_key in self._seen_events:
                self._logger.debug(
                    f"Duplicate detected (in-memory): platform={platform}, "
                    f"event_id={event_id}, event_type={event_type}"
                )
                return True
            
            # First-seen: mark as seen
            self._seen_events.add(dedup_key)
            return False
    
    def get_dedup_count(self) -> int:
        """Get total number of unique events seen."""
        if self._use_distributed:
            # Distributed store doesn't provide count easily
            # Return -1 to indicate "unknown" (stats available via store.stats())
            return -1
        return len(self._seen_events)
    
    def reset(self):
        """Reset deduplication state (use with caution)."""
        self._logger.warning("Resetting deduplication state")
        if self._use_distributed:
            self._logger.warning("Cannot reset distributed store - operation ignored")
        else:
            self._seen_events.clear()


# ============================================================================
# TIMESTAMP NORMALIZER
# ============================================================================

class TimestampNormalizer:
    """
    Deterministic timestamp normalization.
    
    Rules:
    - Use event time, never wall-clock
    - Convert to UTC epoch seconds
    - Clamp future timestamps to ingest time
    - Reject time travel beyond allowed skew
    
    Time ambiguity = data corruption
    """
    
    def __init__(self, ingest_timestamp: int):
        """
        Initialize normalizer.
        
        Args:
            ingest_timestamp: Current ingestion timestamp (monotonic)
        """
        self._ingest_timestamp = ingest_timestamp
        self._logger = logging.getLogger(f"{__name__}.TimestampNormalizer")
    
    def normalize(self, event_timestamp: int) -> Tuple[Optional[int], Optional[RejectionReason]]:
        """
        Normalize event timestamp to UTC epoch seconds.
        
        Args:
            event_timestamp: Raw event timestamp (seconds or milliseconds)
            
        Returns:
            Tuple of (normalized_timestamp, rejection_reason)
            If rejection_reason is not None, normalization failed
        """
        # Handle millisecond timestamps (heuristic: > year 2100 in seconds)
        if event_timestamp > 4102444800:  # Jan 1, 2100 in seconds
            event_timestamp = event_timestamp // 1000
        
        # Validate timestamp is positive
        if event_timestamp <= 0:
            self._logger.warning(f"Invalid timestamp: {event_timestamp}")
            return None, RejectionReason.INVALID_TIMESTAMP
        
        # Check for excessive future skew
        future_skew = event_timestamp - self._ingest_timestamp
        if future_skew > FUTURE_SKEW_TOLERANCE_SECONDS:
            self._logger.warning(
                f"Future timestamp beyond tolerance: "
                f"event_ts={event_timestamp}, ingest_ts={self._ingest_timestamp}, "
                f"skew={future_skew}s"
            )
            return None, RejectionReason.TIMESTAMP_FUTURE_SKEW
        
        # Clamp future timestamps to ingest time
        if event_timestamp > self._ingest_timestamp:
            self._logger.debug(
                f"Clamping future timestamp: {event_timestamp} -> {self._ingest_timestamp}"
            )
            event_timestamp = self._ingest_timestamp
        
        # Check for excessive past skew
        past_skew = self._ingest_timestamp - event_timestamp
        if past_skew > PAST_SKEW_TOLERANCE_SECONDS:
            self._logger.warning(
                f"Past timestamp beyond tolerance: "
                f"event_ts={event_timestamp}, ingest_ts={self._ingest_timestamp}, "
                f"skew={past_skew}s"
            )
            return None, RejectionReason.TIMESTAMP_PAST_SKEW
        
        # Timestamp is valid
        return event_timestamp, None


# ============================================================================
# PROVENANCE GENERATOR
# ============================================================================

class ProvenanceGenerator:
    """
    Immutable provenance attachment.
    
    Every fact MUST carry:
    - Source platform
    - Ingestion pipeline name & version
    - Schema version
    - Deterministic lineage hash
    - Ingest run_id
    
    No provenance → invalid fact
    """
    
    def __init__(self, run_id: str):
        """
        Initialize provenance generator.
        
        Args:
            run_id: Unique ingestion run identifier
        """
        self._run_id = run_id
        self._logger = logging.getLogger(f"{__name__}.ProvenanceGenerator")
    
    def generate_lineage_hash(
        self,
        platform: str,
        event_id: str,
        event_type: str,
        content_id: str,
        account_id: str,
        occurred_at: int,
        schema_version: str
    ) -> str:
        """
        Generate deterministic lineage hash.
        
        This hash serves as a cryptographic fingerprint of the event's
        complete provenance chain.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            content_id: Content identifier
            account_id: Account identifier
            occurred_at: Normalized event timestamp
            schema_version: Schema version
            
        Returns:
            SHA-256 lineage hash (hex)
        """
        # Create canonical provenance representation
        provenance_data = {
            'platform': platform.lower().strip(),
            'event_id': event_id.strip(),
            'event_type': event_type.lower().strip(),
            'content_id': content_id.strip(),
            'account_id': account_id.strip(),
            'occurred_at': occurred_at,
            'schema_version': schema_version.strip(),
            'pipeline_name': PIPELINE_NAME,
            'pipeline_version': PIPELINE_VERSION,
        }
        
        # Serialize deterministically
        canonical_json = json.dumps(provenance_data, sort_keys=True, separators=(',', ':'))
        
        # Hash for immutability
        lineage_hash = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
        
        self._logger.debug(f"Generated lineage hash: {lineage_hash}")
        return lineage_hash
    
    def generate_fact_id(
        self,
        platform: str,
        event_id: str,
        event_type: str
    ) -> str:
        """
        Generate deterministic fact ID.
        
        The fact ID is based on the unique event identity:
        (platform, event_id, event_type)
        
        This ensures idempotent ingestion during replays.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            
        Returns:
            SHA-256 fact ID (hex)
        """
        # Create canonical identity representation
        identity_data = {
            'platform': platform.lower().strip(),
            'event_id': event_id.strip(),
            'event_type': event_type.lower().strip(),
        }
        
        # Serialize deterministically
        canonical_json = json.dumps(identity_data, sort_keys=True, separators=(',', ':'))
        
        # Hash for uniqueness
        fact_id = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
        
        return fact_id


# ============================================================================
# VALIDATION ENGINE
# ============================================================================

class ValidationEngine:
    """
    Structural correctness & schema validation.
    
    Enforces:
    - Required field presence
    - Schema version contracts
    - Field length limits
    - Type validity
    - Platform validity
    
    Fail closed on ambiguity.
    """
    
    def __init__(self):
        """Initialize validation engine."""
        self._logger = logging.getLogger(f"{__name__}.ValidationEngine")
    
    def validate_raw_event(
        self,
        raw_event: RawEngagementEvent
    ) -> Tuple[bool, Optional[RejectionReason], Optional[str]]:
        """
        Validate raw engagement event.
        
        Args:
            raw_event: Raw engagement event
            
        Returns:
            Tuple of (is_valid, rejection_reason, details)
        """
        # Check required fields are non-empty
        required_fields = {
            'platform': raw_event.platform,
            'event_id': raw_event.event_id,
            'event_type': raw_event.event_type,
            'content_id': raw_event.content_id,
            'account_id': raw_event.account_id,
            'platform_identifier': raw_event.platform_identifier,
            'schema_version': raw_event.schema_version,
        }
        
        for field_name, field_value in required_fields.items():
            if not field_value or (isinstance(field_value, str) and not field_value.strip()):
                self._logger.warning(f"Missing required field: {field_name}")
                return False, RejectionReason.MISSING_REQUIRED_FIELD, f"field={field_name}"
        
        # Validate event timestamp
        if raw_event.event_timestamp <= 0:
            self._logger.warning(f"Invalid event timestamp: {raw_event.event_timestamp}")
            return False, RejectionReason.INVALID_TIMESTAMP, "timestamp <= 0"
        
        # Validate schema version
        if raw_event.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            self._logger.warning(
                f"Unsupported schema version: {raw_event.schema_version}. "
                f"Supported: {SUPPORTED_SCHEMA_VERSIONS}"
            )
            return False, RejectionReason.INVALID_SCHEMA_VERSION, f"version={raw_event.schema_version}"
        
        # Validate event type
        if not EngagementType.is_valid(raw_event.event_type):
            self._logger.warning(f"Invalid event type: {raw_event.event_type}")
            return False, RejectionReason.INVALID_EVENT_TYPE, f"type={raw_event.event_type}"
        
        # Validate platform
        if not Platform.is_valid(raw_event.platform):
            self._logger.warning(f"Invalid platform: {raw_event.platform}")
            return False, RejectionReason.INVALID_PLATFORM, f"platform={raw_event.platform}"
        
        # Validate platform consistency
        if raw_event.platform.lower() != raw_event.platform_identifier.lower():
            self._logger.warning(
                f"Platform mismatch: platform={raw_event.platform}, "
                f"platform_identifier={raw_event.platform_identifier}"
            )
            return False, RejectionReason.INVALID_PLATFORM, "platform_mismatch"
        
        # Validate field lengths
        if len(raw_event.event_id) > MAX_ID_LENGTH:
            self._logger.warning(f"Event ID too long: {len(raw_event.event_id)} > {MAX_ID_LENGTH}")
            return False, RejectionReason.FIELD_LENGTH_VIOLATION, f"event_id_length={len(raw_event.event_id)}"
        
        if len(raw_event.content_id) > MAX_ID_LENGTH:
            self._logger.warning(f"Content ID too long: {len(raw_event.content_id)} > {MAX_ID_LENGTH}")
            return False, RejectionReason.FIELD_LENGTH_VIOLATION, f"content_id_length={len(raw_event.content_id)}"
        
        if len(raw_event.account_id) > MAX_ID_LENGTH:
            self._logger.warning(f"Account ID too long: {len(raw_event.account_id)} > {MAX_ID_LENGTH}")
            return False, RejectionReason.FIELD_LENGTH_VIOLATION, f"account_id_length={len(raw_event.account_id)}"
        
        if len(raw_event.platform) > MAX_PLATFORM_LENGTH:
            self._logger.warning(f"Platform too long: {len(raw_event.platform)} > {MAX_PLATFORM_LENGTH}")
            return False, RejectionReason.FIELD_LENGTH_VIOLATION, f"platform_length={len(raw_event.platform)}"
        
        if len(raw_event.event_type) > MAX_EVENT_TYPE_LENGTH:
            self._logger.warning(f"Event type too long: {len(raw_event.event_type)} > {MAX_EVENT_TYPE_LENGTH}")
            return False, RejectionReason.FIELD_LENGTH_VIOLATION, f"event_type_length={len(raw_event.event_type)}"
        
        # All validations passed
        return True, None, None


# ============================================================================
# WATCHDOG & SAFETY INTEGRATION
# ============================================================================

class SafetyWatchdog:
    """
    Emergency mode and invariant enforcement.
    
    TIER-0 UPGRADE: Now uses EmergencyStopController for externalized
    safety authority that works across workers and regions.
    
    Ingestor MUST:
    - Respect global freeze
    - Abort in emergency mode
    - Honor invariant engine verdicts
    - Never self-recover
    
    Survival is secondary to correctness.
    """
    
    def __init__(self, emergency_controller: Optional['EmergencyStopController'] = None):
        """
        Initialize safety watchdog.
        
        Args:
            emergency_controller: Optional EmergencyStopController
                                 If None, falls back to static check (not Tier-0 safe)
        """
        self._logger = logging.getLogger(f"{__name__}.SafetyWatchdog")
        self._emergency_controller = emergency_controller
        self._use_external = emergency_controller is not None
        
        if not self._use_external:
            self._logger.warning(
                "SafetyWatchdog using static flags - "
                "NOT safe for distributed ingestion. Use EmergencyStopController for Tier-0."
            )
    
    def check_safety(self) -> Tuple[bool, Optional[RejectionReason]]:
        """
        Check if ingestion is safe to proceed.
        
        TIER-0: Uses external EmergencyStopController for global consistency.
        
        Returns:
            Tuple of (is_safe, rejection_reason)
        """
        if self._use_external:
            # Use external emergency stop controller
            try:
                self._emergency_controller.assert_system_clear()
                # System is clear - proceed
                return True, None
            except Exception as e:
                # System is stopped - check state for specific reason
                try:
                    state = self._emergency_controller.get_state()
                    if state.state.value == "stopped":
                        self._logger.critical("EMERGENCY STOP ACTIVE - All ingestion blocked")
                        return False, RejectionReason.EMERGENCY_MODE_ACTIVE
                    elif state.state.value == "locked":
                        self._logger.critical("GLOBAL FREEZE ACTIVE - All ingestion blocked")
                        return False, RejectionReason.GLOBAL_FREEZE_ACTIVE
                    else:
                        # Unknown state - fail closed
                        self._logger.error(f"Unknown emergency state: {state.state.value}")
                        return False, RejectionReason.EMERGENCY_MODE_ACTIVE
                except Exception as e2:
                    # Cannot determine state - fail closed
                    self._logger.error(f"Failed to check emergency state: {e2}")
                    return False, RejectionReason.EMERGENCY_MODE_ACTIVE
        else:
            # Fallback: static flags (not Tier-0 safe)
            # These are module-level constants that don't work across workers
            # Import here to avoid circular dependency
            try:
                from infra.safety.emergency_stop import (
                    EmergencyStopController,
                    create_emergency_stop_controller
                )
                # Try to create default controller
                controller = create_emergency_stop_controller()
                controller.assert_system_clear()
                return True, None
            except Exception:
                # Fallback to static check (legacy behavior)
                # This is NOT Tier-0 safe
                return True, None
    
    def check_invariants(self, fact: EngagementFact) -> Tuple[bool, Optional[str]]:
        """
        Validate fact against invariants.
        
        Args:
            fact: Generated engagement fact
            
        Returns:
            Tuple of (invariants_satisfied, violation_details)
        """
        # Invariant: All timestamps must be positive
        if fact.occurred_at <= 0 or fact.ingested_at <= 0:
            return False, f"negative_timestamp: occurred_at={fact.occurred_at}, ingested_at={fact.ingested_at}"
        
        # Invariant: Ingested time >= occurred time (within skew tolerance)
        if fact.ingested_at < fact.occurred_at - FUTURE_SKEW_TOLERANCE_SECONDS:
            return False, f"time_travel: ingested_at={fact.ingested_at} < occurred_at={fact.occurred_at}"
        
        # Invariant: All IDs must be non-empty
        if not all([fact.fact_id, fact.content_id, fact.account_id, fact.source_event_id]):
            return False, "empty_identifier"
        
        # Invariant: Lineage hash must be valid
        if not fact.lineage_hash or len(fact.lineage_hash) != 64:  # SHA-256 hex length
            return False, f"invalid_lineage_hash: {fact.lineage_hash}"
        
        # Invariant: Fact ID must be valid
        if not fact.fact_id or len(fact.fact_id) != 64:  # SHA-256 hex length
            return False, f"invalid_fact_id: {fact.fact_id}"
        
        # All invariants satisfied
        return True, None


# ============================================================================
# MAIN INGESTION ENGINE
# ============================================================================

class EngagementIngestor:
    """
    Core engagement ingestion engine.
    
    This is the border checkpoint between raw platform signals and
    canonical analytics truth.
    
    Responsibilities:
    1. Accept raw engagement events
    2. Validate structural correctness
    3. Enforce schema & version contracts
    4. Deduplicate at event-identity level
    5. Normalize timestamps deterministically
    6. Attach immutable provenance
    7. Emit canonical EngagementFact objects
    8. Fail closed on ambiguity
    """
    
    def __init__(
        self,
        replay_context: Optional[ReplayContext] = None,
        identity_store: Optional['EventIdentityStore'] = None,
        emergency_controller: Optional['EmergencyStopController'] = None
    ):
        """
        Initialize ingestion engine.
        
        TIER-0 UPGRADE: Now accepts distributed identity store and emergency controller.
        
        Args:
            replay_context: Optional replay context for deterministic behavior
            identity_store: Optional distributed EventIdentityStore for global deduplication
            emergency_controller: Optional EmergencyStopController for externalized safety
        """
        self._replay_context = replay_context or ReplayContext()
        self._run_id = self._replay_context.get_run_id()
        
        # TIER-0 FIX: Use replay-aware timestamp for deterministic ingest timestamps
        self._ingest_timestamp = self._replay_context.get_ingest_timestamp()
        
        # Initialize components
        self._validator = ValidationEngine()
        
        # TIER-0 FIX: Use distributed identity store for global deduplication
        self._deduplicator = DeduplicationEngine(
            identity_store=identity_store,
            ingestion_run_id=self._run_id
        )
        
        self._timestamp_normalizer = TimestampNormalizer(self._ingest_timestamp)
        self._provenance_generator = ProvenanceGenerator(self._run_id)
        
        # TIER-0 FIX: Use external emergency controller for global safety
        self._watchdog = SafetyWatchdog(emergency_controller=emergency_controller)
        
        # Initialize metrics
        self._metrics = IngestionMetrics(
            run_id=self._run_id,
            started_at=self._ingest_timestamp
        )
        
        # Initialize rejection log
        self._rejection_log: List[RejectionRecord] = []
        
        # Initialize logger
        self._logger = logging.getLogger(f"{__name__}.EngagementIngestor")
        self._logger.info(
            f"Initialized EngagementIngestor: run_id={self._run_id}, "
            f"is_replay={self._replay_context.is_replay}, "
            f"distributed_dedup={identity_store is not None}, "
            f"external_safety={emergency_controller is not None}"
        )
    
    def _reject_event(
        self,
        raw_event: RawEngagementEvent,
        reason: RejectionReason,
        details: Optional[str] = None
    ):
        """
        Explicitly reject an event with audit trail.
        
        Args:
            raw_event: Raw event being rejected
            reason: Rejection reason
            details: Optional additional details
        """
        # Update metrics
        self._metrics.increment_rejection(reason, raw_event.platform)
        
        # Create rejection record
        rejection = RejectionRecord(
            event_id=raw_event.event_id,
            platform=raw_event.platform,
            reason=reason,
            rejected_at=self._ingest_timestamp,
            details=details
        )
        self._rejection_log.append(rejection)
        
        # Log rejection
        self._logger.warning(
            f"Event rejected: event_id={raw_event.event_id}, "
            f"platform={raw_event.platform}, reason={reason.value}, "
            f"details={details}"
        )
    
    def _create_fact(self, raw_event: RawEngagementEvent, normalized_timestamp: int) -> EngagementFact:
        """
        Create canonical EngagementFact from validated raw event.
        
        Args:
            raw_event: Validated raw event
            normalized_timestamp: Normalized event timestamp
            
        Returns:
            Immutable EngagementFact
        """
        # Generate fact ID (deterministic)
        fact_id = self._provenance_generator.generate_fact_id(
            raw_event.platform,
            raw_event.event_id,
            raw_event.event_type
        )
        
        # Generate lineage hash (deterministic)
        lineage_hash = self._provenance_generator.generate_lineage_hash(
            raw_event.platform,
            raw_event.event_id,
            raw_event.event_type,
            raw_event.content_id,
            raw_event.account_id,
            normalized_timestamp,
            raw_event.schema_version
        )
        
        # Create immutable fact
        fact = EngagementFact(
            fact_id=fact_id,
            event_type=raw_event.event_type.lower(),
            content_id=raw_event.content_id,
            account_id=raw_event.account_id,
            platform=raw_event.platform.lower(),
            occurred_at=normalized_timestamp,
            ingested_at=self._ingest_timestamp,
            source_event_id=raw_event.event_id,
            source_schema_version=raw_event.schema_version,
            lineage_hash=lineage_hash,
            ingestion_pipeline_name=PIPELINE_NAME,
            ingestion_pipeline_version=PIPELINE_VERSION,
            ingestion_run_id=self._run_id
        )
        
        return fact
    
    def ingest_event(self, raw_event: RawEngagementEvent) -> Optional[EngagementFact]:
        """
        Ingest a single raw engagement event.
        
        This is the main ingestion entry point.
        
        Args:
            raw_event: Raw engagement event
            
        Returns:
            EngagementFact if accepted, None if rejected
        """
        self._metrics.total_events_received += 1
        
        # SAFETY CHECK: Emergency mode & global freeze
        is_safe, safety_rejection = self._watchdog.check_safety()
        if not is_safe:
            self._reject_event(raw_event, safety_rejection)
            if safety_rejection == RejectionReason.EMERGENCY_MODE_ACTIVE:
                self._metrics.emergency_mode_triggers += 1
            elif safety_rejection == RejectionReason.GLOBAL_FREEZE_ACTIVE:
                self._metrics.global_freeze_triggers += 1
            return None
        
        # VALIDATION: Structural correctness
        is_valid, validation_rejection, validation_details = self._validator.validate_raw_event(raw_event)
        if not is_valid:
            self._reject_event(raw_event, validation_rejection, validation_details)
            if validation_rejection == RejectionReason.INVALID_SCHEMA_VERSION:
                self._metrics.schema_mismatches += 1
            return None
        
        # DEDUPLICATION: Event-identity level (distributed)
        # TIER-0: Pass normalized timestamp for replay determinism
        is_duplicate = self._deduplicator.is_duplicate(
            raw_event.platform,
            raw_event.event_id,
            raw_event.event_type,
            first_seen_at=self._ingest_timestamp  # Use deterministic ingest timestamp
        )
        if is_duplicate:
            self._reject_event(raw_event, RejectionReason.DUPLICATE_EVENT)
            self._metrics.increment_duplicate(raw_event.platform)
            return None
        
        # TIMESTAMP NORMALIZATION: Deterministic
        normalized_timestamp, timestamp_rejection = self._timestamp_normalizer.normalize(
            raw_event.event_timestamp
        )
        if timestamp_rejection:
            self._reject_event(raw_event, timestamp_rejection)
            return None
        
        # FACT GENERATION: Create immutable fact
        try:
            fact = self._create_fact(raw_event, normalized_timestamp)
        except Exception as e:
            self._logger.error(f"Fact generation failed: {e}", exc_info=True)
            self._reject_event(
                raw_event,
                RejectionReason.MALFORMED_EVENT,
                f"fact_generation_error: {str(e)}"
            )
            return None
        
        # INVARIANT VALIDATION: Safety check on generated fact
        invariants_ok, invariant_violation = self._watchdog.check_invariants(fact)
        if not invariants_ok:
            self._logger.critical(
                f"INVARIANT VIOLATION: {invariant_violation} for fact_id={fact.fact_id}"
            )
            self._reject_event(
                raw_event,
                RejectionReason.INVARIANT_VIOLATION,
                invariant_violation
            )
            self._metrics.invariant_violations += 1
            return None
        
        # ACCEPTANCE: Record successful ingestion
        self._metrics.increment_acceptance(raw_event.platform, raw_event.schema_version)
        
        self._logger.info(
            f"Fact accepted: fact_id={fact.fact_id}, "
            f"platform={fact.platform}, event_type={fact.event_type}"
        )
        
        return fact
    
    def ingest_batch(
        self,
        raw_events: List[RawEngagementEvent]
    ) -> Tuple[List[EngagementFact], IngestionMetrics]:
        """
        Ingest a batch of raw engagement events.
        
        Args:
            raw_events: List of raw engagement events
            
        Returns:
            Tuple of (accepted_facts, ingestion_metrics)
        """
        self._logger.info(f"Starting batch ingestion: {len(raw_events)} events")
        
        accepted_facts: List[EngagementFact] = []
        
        for raw_event in raw_events:
            fact = self.ingest_event(raw_event)
            if fact:
                accepted_facts.append(fact)
        
        # Finalize metrics
        self._metrics.completed_at = int(time.time())
        
        self._logger.info(
            f"Batch ingestion complete: "
            f"received={self._metrics.total_events_received}, "
            f"accepted={self._metrics.total_facts_accepted}, "
            f"rejected={self._metrics.total_events_rejected}, "
            f"deduplicated={self._metrics.total_events_deduplicated}"
        )
        
        return accepted_facts, self._metrics
    
    def get_rejection_log(self) -> List[RejectionRecord]:
        """Get complete rejection audit log."""
        return self._rejection_log.copy()
    
    def get_metrics(self) -> IngestionMetrics:
        """Get current ingestion metrics."""
        return self._metrics


# ============================================================================
# OUTPUT SERIALIZATION
# ============================================================================

class FactSerializer:
    """
    Serialize EngagementFacts for downstream consumption.
    
    Supports multiple output formats while maintaining fact immutability.
    """
    
    @staticmethod
    def to_json_lines(facts: List[EngagementFact]) -> str:
        """
        Serialize facts to JSON Lines format.
        
        Args:
            facts: List of engagement facts
            
        Returns:
            JSON Lines string (one fact per line)
        """
        lines = [fact.to_json() for fact in facts]
        return '\n'.join(lines)
    
    @staticmethod
    def to_json_array(facts: List[EngagementFact]) -> str:
        """
        Serialize facts to JSON array.
        
        Args:
            facts: List of engagement facts
            
        Returns:
            JSON array string
        """
        fact_dicts = [fact.to_dict() for fact in facts]
        return json.dumps(fact_dicts, indent=2, sort_keys=True)
    
    @staticmethod
    def write_to_file(facts: List[EngagementFact], filepath: str, format: str = 'jsonl'):
        """
        Write facts to file.
        
        Args:
            facts: List of engagement facts
            filepath: Output file path
            format: Output format ('jsonl' or 'json')
        """
        with open(filepath, 'w') as f:
            if format == 'jsonl':
                f.write(FactSerializer.to_json_lines(facts))
            elif format == 'json':
                f.write(FactSerializer.to_json_array(facts))
            else:
                raise ValueError(f"Unsupported format: {format}")


# ============================================================================
# METRICS REPORTER
# ============================================================================

class MetricsReporter:
    """
    Audit trail and observability reporting.
    
    Silence is not allowed.
    """
    
    @staticmethod
    def print_metrics(metrics: IngestionMetrics):
        """
        Print human-readable metrics report.
        
        Args:
            metrics: Ingestion metrics
        """
        print("\n" + "=" * 80)
        print("ENGAGEMENT INGESTION METRICS")
        print("=" * 80)
        print(f"\nRun ID: {metrics.run_id}")
        print(f"Started: {datetime.fromtimestamp(metrics.started_at, tz=timezone.utc).isoformat()}")
        if metrics.completed_at:
            print(f"Completed: {datetime.fromtimestamp(metrics.completed_at, tz=timezone.utc).isoformat()}")
            duration = metrics.completed_at - metrics.started_at
            print(f"Duration: {duration}s")
        
        print("\n--- SUMMARY ---")
        print(f"Total Events Received: {metrics.total_events_received}")
        print(f"Total Facts Accepted: {metrics.total_facts_accepted}")
        print(f"Total Events Rejected: {metrics.total_events_rejected}")
        print(f"Total Events Deduplicated: {metrics.total_events_deduplicated}")
        
        if metrics.total_events_received > 0:
            acceptance_rate = (metrics.total_facts_accepted / metrics.total_events_received) * 100
            print(f"Acceptance Rate: {acceptance_rate:.2f}%")
        
        if metrics.rejections_by_reason:
            print("\n--- REJECTIONS BY REASON ---")
            for reason, count in sorted(metrics.rejections_by_reason.items(), key=lambda x: -x[1]):
                print(f"  {reason}: {count}")
        
        if metrics.accepted_by_platform:
            print("\n--- ACCEPTED BY PLATFORM ---")
            for platform, count in sorted(metrics.accepted_by_platform.items(), key=lambda x: -x[1]):
                print(f"  {platform}: {count}")
        
        if metrics.rejected_by_platform:
            print("\n--- REJECTED BY PLATFORM ---")
            for platform, count in sorted(metrics.rejected_by_platform.items(), key=lambda x: -x[1]):
                print(f"  {platform}: {count}")
        
        if metrics.schema_version_distribution:
            print("\n--- SCHEMA VERSION DISTRIBUTION ---")
            for version, count in sorted(metrics.schema_version_distribution.items()):
                print(f"  v{version}: {count}")
        
        if metrics.schema_mismatches > 0:
            print(f"\nSchema Mismatches: {metrics.schema_mismatches}")
        
        if metrics.duplicates_by_platform:
            print("\n--- DUPLICATES BY PLATFORM ---")
            for platform, count in sorted(metrics.duplicates_by_platform.items(), key=lambda x: -x[1]):
                print(f"  {platform}: {count}")
        
        if metrics.emergency_mode_triggers > 0:
            print(f"\n⚠️  EMERGENCY MODE TRIGGERS: {metrics.emergency_mode_triggers}")
        
        if metrics.global_freeze_triggers > 0:
            print(f"\n❄️  GLOBAL FREEZE TRIGGERS: {metrics.global_freeze_triggers}")
        
        if metrics.invariant_violations > 0:
            print(f"\n🚨 INVARIANT VIOLATIONS: {metrics.invariant_violations}")
        
        print("\n" + "=" * 80 + "\n")
    
    @staticmethod
    def write_metrics_to_file(metrics: IngestionMetrics, filepath: str):
        """
        Write metrics to JSON file.
        
        Args:
            metrics: Ingestion metrics
            filepath: Output file path
        """
        with open(filepath, 'w') as f:
            json.dump(metrics.to_dict(), f, indent=2, sort_keys=True)
    
    @staticmethod
    def print_rejection_log(rejection_log: List[RejectionRecord], limit: int = 100):
        """
        Print rejection log (with limit).
        
        Args:
            rejection_log: List of rejection records
            limit: Maximum number of rejections to print
        """
        if not rejection_log:
            print("\nNo rejections recorded.")
            return
        
        print("\n" + "=" * 80)
        print(f"REJECTION LOG (showing {min(limit, len(rejection_log))} of {len(rejection_log)})")
        print("=" * 80 + "\n")
        
        for i, rejection in enumerate(rejection_log[:limit], 1):
            print(f"{i}. Event ID: {rejection.event_id}")
            print(f"   Platform: {rejection.platform}")
            print(f"   Reason: {rejection.reason.value}")
            if rejection.details:
                print(f"   Details: {rejection.details}")
            print(f"   Rejected At: {datetime.fromtimestamp(rejection.rejected_at, tz=timezone.utc).isoformat()}")
            print()
        
        if len(rejection_log) > limit:
            print(f"... and {len(rejection_log) - limit} more rejections")
        
        print("=" * 80 + "\n")


# ============================================================================
# COMMAND-LINE INTERFACE
# ============================================================================

def main():
    """
    Main entry point for engagement ingestion pipeline.
    
    Usage:
        python engagement_ingest.py <input_file> [--output <output_file>] [--replay]
    """
    import argparse
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('engagement_ingest.log')
        ]
    )
    
    # Parse arguments
    parser = argparse.ArgumentParser(description='Engagement Fact Ingestion Pipeline')
    parser.add_argument('input_file', help='Input file containing raw engagement events (JSON Lines)')
    parser.add_argument('--output', '-o', help='Output file for accepted facts (JSON Lines)')
    parser.add_argument('--replay', action='store_true', help='Enable replay mode')
    parser.add_argument('--replay-run-id', help='Run ID for replay mode')
    parser.add_argument('--replay-timestamp', type=int, help='Timestamp for replay mode (UTC epoch seconds)')
    parser.add_argument('--metrics-output', help='Output file for metrics (JSON)')
    parser.add_argument('--show-rejections', action='store_true', help='Print rejection log')
    
    args = parser.parse_args()
    
    # Load raw events
    logger = logging.getLogger(__name__)
    logger.info(f"Loading raw events from: {args.input_file}")
    
    raw_events = []
    try:
        with open(args.input_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event_data = json.loads(line)
                    raw_event = RawEngagementEvent.from_dict(event_data)
                    raw_events.append(raw_event)
                except Exception as e:
                    logger.error(f"Failed to parse line {line_num}: {e}")
                    continue
    except FileNotFoundError:
        logger.error(f"Input file not found: {args.input_file}")
        sys.exit(1)
    
    logger.info(f"Loaded {len(raw_events)} raw events")
    
    # Create replay context if needed
    replay_context = None
    if args.replay:
        # TIER-0 FIX: Use provided replay timestamp or default to current time
        replay_timestamp = args.replay_timestamp if hasattr(args, 'replay_timestamp') else int(time.time())
        replay_context = ReplayContext(
            is_replay=True,
            replay_run_id=args.replay_run_id,
            replay_timestamp=replay_timestamp
        )
        logger.info(
            f"Replay mode enabled: run_id={replay_context.replay_run_id}, "
            f"replay_timestamp={replay_context.replay_timestamp}"
        )
    
    # TIER-0: Create distributed identity store and emergency controller
    # For production, these should be injected from infrastructure
    identity_store = None
    emergency_controller = None
    
    try:
        from infra.idempotency.event_identity_store import create_event_identity_store
        from infra.safety.emergency_stop import create_emergency_stop_controller
        
        # Create emergency controller (always available)
        emergency_controller = create_emergency_stop_controller()
        
        # Create identity store (requires KVBackend - will be None if not provided)
        # For production, inject a distributed KVBackend (Redis, etcd, etc.)
        # For testing, can provide an in-memory KVBackend implementation
        identity_store = create_event_identity_store(
            backend=None,  # None = graceful degradation (fallback to in-memory dedup)
            logger=logger
        )
        
        if identity_store:
            logger.info("Tier-0 infrastructure initialized: distributed dedup + external safety")
        else:
            logger.warning(
                "Tier-0 infrastructure partially initialized: external safety only. "
                "Distributed deduplication disabled (no KVBackend provided). "
                "For Tier-0, provide a KVBackend instance."
            )
    except ImportError as e:
        logger.warning(f"Tier-0 infrastructure not available: {e}. Using fallback mode.")
    
    # Create ingestor
    ingestor = EngagementIngestor(
        replay_context=replay_context,
        identity_store=identity_store,
        emergency_controller=emergency_controller
    )
    
    # Ingest batch
    accepted_facts, metrics = ingestor.ingest_batch(raw_events)
    
    # Print metrics
    MetricsReporter.print_metrics(metrics)
    
    # Write metrics to file if requested
    if args.metrics_output:
        MetricsReporter.write_metrics_to_file(metrics, args.metrics_output)
        logger.info(f"Metrics written to: {args.metrics_output}")
    
    # Print rejection log if requested
    if args.show_rejections:
        rejection_log = ingestor.get_rejection_log()
        MetricsReporter.print_rejection_log(rejection_log)
    
    # Write accepted facts to output file
    if args.output:
        FactSerializer.write_to_file(accepted_facts, args.output, format='jsonl')
        logger.info(f"Accepted facts written to: {args.output}")
    else:
        # Print to stdout
        print("\n--- ACCEPTED FACTS ---")
        for fact in accepted_facts[:10]:  # Print first 10
            print(fact.to_json())
        if len(accepted_facts) > 10:
            print(f"\n... and {len(accepted_facts) - 10} more facts")
    
    # Exit with appropriate code
    if metrics.invariant_violations > 0:
        logger.critical("INVARIANT VIOLATIONS DETECTED - Exiting with error code")
        sys.exit(2)
    elif metrics.total_facts_accepted == 0 and metrics.total_events_received > 0:
        logger.error("NO FACTS ACCEPTED - Exiting with error code")
        sys.exit(1)
    else:
        logger.info("Ingestion complete")
        sys.exit(0)


if __name__ == '__main__':
    main()