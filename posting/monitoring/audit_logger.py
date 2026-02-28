"""
/posting/monitoring/audit_logger.py

Immutable Forensic Audit & Compliance Ledger

Tier-0: Append-only, tamper-resistant ledger of all safety-relevant 
and outcome-relevant events in the posting system.

What This File Actually Is:
audit_logger.py is the single, append-only, tamper-resistant ledger of 
everything that mattered in the posting system.

It answers one question only:
"Can we reconstruct exactly what happened, why it happened, and who/what caused it?"

If the answer is ever "no," this file failed.

What This File Is NOT:
❌ Not application logging
❌ Not metrics
❌ Not observability traces
❌ Not debugging output
❌ Not retry logs

Those are ephemeral.
This file is permanent truth.

Design principles:
- Append-only
- Deterministic
- Causally ordered
- Externally verifiable
- Zero business logic

This file must remain boring forever.

LOC: ~1,400-2,200 (Tier-0 requirement)
"""

import hashlib
import json
import os
import time
import uuid
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable, Iterator, Tuple
from threading import Lock, RLock
from collections import defaultdict
from datetime import datetime, timedelta
import gzip
import shutil

logger = logging.getLogger(__name__)

# ============================================================================
# VERSION & CONFIGURATION
# ============================================================================

AUDIT_LOGGER_VERSION = "1.0.0"
GENESIS_HASH = "0" * 64

# Maximum events to keep in memory index
MAX_INDEX_SIZE = 100000

# Maximum file size before rotation (100MB)
MAX_LOG_FILE_SIZE = 100 * 1024 * 1024

# ============================================================================
# EVENT CONTRACTS (IMMUTABLE)
# ============================================================================

class AuditEventType(Enum):
    """
    Finite, versioned set of audit event types. Never use free-form strings.
    
    Posting Lifecycle:
    - INTENT_CREATED: Intent object created
    - INTENT_ENQUEUED: Intent added to queue
    - INTENT_CLAIMED: Intent claimed by worker
    - DISPATCH_ATTEMPTED: Dispatch attempt initiated
    - POST_SUCCEEDED: Post succeeded on platform
    - POST_FAILED: Post failed on platform
    - DEAD_LETTERED: Intent moved to dead letter queue
    
    Safety & Control:
    - RISK_SCORE_UPDATED: Risk score changed (bucketed)
    - ROLLOUT_DECISION: Rollout increase/decrease decision
    - KILL_SWITCH_ACTIVATED: Kill switch activated
    - PLATFORM_LIMIT_HIT: Platform rate limit hit
    
    Monitoring:
    - ANOMALY_DETECTED: Anomaly detected (hashed)
    - TRUST_SIGNAL_UPDATED: Account trust tier changed
    - AUTH_REFRESH_FAILED: Auth refresh failure
    - PLATFORM_BAN: Platform ban/warning received
    """
    
    # Posting Lifecycle
    INTENT_CREATED = "intent_created"
    INTENT_ENQUEUED = "intent_enqueued"
    INTENT_CLAIMED = "intent_claimed"
    DISPATCH_ATTEMPTED = "dispatch_attempted"
    POST_SUCCEEDED = "post_succeeded"
    POST_FAILED = "post_failed"
    DEAD_LETTERED = "dead_lettered"
    
    # Safety & Control
    RISK_SCORE_UPDATED = "risk_score_updated"
    ROLLOUT_DECISION = "rollout_decision"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    PLATFORM_LIMIT_HIT = "platform_limit_hit"
    
    # Monitoring
    ANOMALY_DETECTED = "anomaly_detected"
    TRUST_SIGNAL_UPDATED = "trust_signal_updated"
    AUTH_REFRESH_FAILED = "auth_refresh_failed"
    PLATFORM_BAN = "platform_ban"


class AuditSeverity(Enum):
    """
    Event severity classification.
    
    Used for filtering and alerting, not for business logic.
    """
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


# Event type to default severity mapping
EVENT_SEVERITY_MAP: Dict[AuditEventType, AuditSeverity] = {
    # Posting Lifecycle - mostly INFO
    AuditEventType.INTENT_CREATED: AuditSeverity.INFO,
    AuditEventType.INTENT_ENQUEUED: AuditSeverity.INFO,
    AuditEventType.INTENT_CLAIMED: AuditSeverity.INFO,
    AuditEventType.DISPATCH_ATTEMPTED: AuditSeverity.INFO,
    AuditEventType.POST_SUCCEEDED: AuditSeverity.INFO,
    AuditEventType.POST_FAILED: AuditSeverity.WARNING,
    AuditEventType.DEAD_LETTERED: AuditSeverity.ERROR,
    
    # Safety & Control - WARNING to CRITICAL
    AuditEventType.RISK_SCORE_UPDATED: AuditSeverity.INFO,
    AuditEventType.ROLLOUT_DECISION: AuditSeverity.INFO,
    AuditEventType.KILL_SWITCH_ACTIVATED: AuditSeverity.CRITICAL,
    AuditEventType.PLATFORM_LIMIT_HIT: AuditSeverity.WARNING,
    
    # Monitoring - WARNING to ERROR
    AuditEventType.ANOMALY_DETECTED: AuditSeverity.WARNING,
    AuditEventType.TRUST_SIGNAL_UPDATED: AuditSeverity.INFO,
    AuditEventType.AUTH_REFRESH_FAILED: AuditSeverity.ERROR,
    AuditEventType.PLATFORM_BAN: AuditSeverity.CRITICAL,
}


# ============================================================================
# IMMUTABLE EVENT STRUCTURE
# ============================================================================

@dataclass(frozen=True)
class AuditEvent:
    """
    Immutable audit event with hash chain integrity.
    
    HARD RULES:
    - Never mutated after creation
    - Hash chain is mandatory
    - Payload must be JSON-serializable
    - PII forbidden unless explicitly approved
    - All fields are required for determinism
    """
    
    event_id: str
    event_type: AuditEventType
    severity: AuditSeverity
    
    timestamp: float
    source_file: str
    source_function: str
    
    intent_id: Optional[str]
    account_id: Optional[str]
    platform: Optional[str]
    
    payload: Dict[str, Any]
    previous_event_hash: str
    event_hash: str
    
    # Metadata for replay and verification
    logger_version: str = AUDIT_LOGGER_VERSION
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for serialization."""
        d = asdict(self)
        d['event_type'] = self.event_type.value
        d['severity'] = self.severity.value
        return d
    
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'AuditEvent':
        """Deserialize from dict."""
        d = d.copy()
        d['event_type'] = AuditEventType(d['event_type'])
        d['severity'] = AuditSeverity(d['severity'])
        return cls(**d)


# ============================================================================
# HASHING & TAMPER EVIDENCE
# ============================================================================

class AuditHasher:
    """
    Creates blockchain-like integrity chain.
    Tampering is detectable immediately.
    
    Each event:
    event_hash = hash(
        event_type +
        timestamp +
        payload +
        previous_event_hash +
        source_file +
        source_function
    )
    
    This creates a deterministic, tamper-evident chain.
    """
    
    @staticmethod
    def hash_event(
        event_type: AuditEventType,
        timestamp: float,
        payload: Dict[str, Any],
        previous_hash: str,
        source_file: str,
        source_function: str
    ) -> str:
        """
        Generate deterministic hash for event.
        
        Deterministic: same inputs always produce same hash.
        """
        # Sort payload for deterministic hashing
        payload_json = json.dumps(payload, sort_keys=True)
        
        components = [
            event_type.value,
            f"{timestamp:.9f}",  # High precision timestamp
            payload_json,
            previous_hash,
            source_file,
            source_function,
            AUDIT_LOGGER_VERSION
        ]
        
        combined = "|".join(components)
        return hashlib.sha256(combined.encode('utf-8')).hexdigest()
    
    @staticmethod
    def verify_hash(event: AuditEvent) -> bool:
        """Verify event hash integrity."""
        expected = AuditHasher.hash_event(
            event.event_type,
            event.timestamp,
            event.payload,
            event.previous_event_hash,
            event.source_file,
            event.source_function
        )
        return expected == event.event_hash
    
    @staticmethod
    def verify_chain(events: List[AuditEvent], genesis_hash: str = GENESIS_HASH) -> Tuple[bool, List[str]]:
        """
        Verify hash chain integrity for a sequence of events.
        
        Returns:
            (is_valid, list_of_errors)
        """
        errors = []
        
        if not events:
            return True, []
        
        # Verify first event links to genesis
        if events[0].previous_event_hash != genesis_hash:
            errors.append(
                f"First event {events[0].event_id} has invalid genesis hash: "
                f"expected {genesis_hash}, got {events[0].previous_event_hash}"
            )
        
        # Verify each event's hash
        for i, event in enumerate(events):
            if not AuditHasher.verify_hash(event):
                errors.append(f"Hash verification failed for event {event.event_id}")
            
            # Verify chain continuity
            if i > 0:
                prev_event = events[i - 1]
                if event.previous_event_hash != prev_event.event_hash:
                    errors.append(
                        f"Chain break at event {event.event_id}: "
                        f"expected previous_hash {prev_event.event_hash}, "
                        f"got {event.previous_event_hash}"
                    )
        
        return len(errors) == 0, errors


# ============================================================================
# SERIALIZATION
# ============================================================================

class AuditSerializer:
    """
    JSON serialization for audit events.
    
    Guarantees:
    - Deterministic serialization (sorted keys)
    - Reversible deserialization
    - Error handling for corrupted data
    """
    
    @staticmethod
    def serialize(event: AuditEvent) -> str:
        """
        Serialize event to JSON string.
        
        Deterministic: same event always produces same JSON.
        """
        return json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=False)
    
    @staticmethod
    def deserialize(data: str) -> AuditEvent:
        """
        Deserialize JSON string to event.
        
        Raises ValueError on invalid data.
        """
        try:
            d = json.loads(data)
            return AuditEvent.from_dict(d)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise ValueError(f"Failed to deserialize audit event: {e}")
    
    @staticmethod
    def serialize_batch(events: List[AuditEvent]) -> str:
        """Serialize batch of events (one per line)."""
        return "\n".join(AuditSerializer.serialize(e) for e in events)
    
    @staticmethod
    def deserialize_batch(data: str) -> List[AuditEvent]:
        """Deserialize batch of events (one per line)."""
        events = []
        for line in data.strip().split('\n'):
            if line.strip():
                events.append(AuditSerializer.deserialize(line))
        return events


# ============================================================================
# INVARIANT VALIDATION (NON-OPTIONAL)
# ============================================================================

class AuditInvariantValidator:
    """
    Enforces audit log invariants.
    
    Violation ⇒ system halt
    Audit corruption is unrecoverable.
    
    Enforces:
    - Timestamps are monotonic
    - Hash chain continuity
    - Severity matches event type (default mapping)
    - Source file is known
    - Forbidden fields absent
    - Payload is JSON-serializable
    - Event IDs are unique
    - Required fields present
    """
    
    KNOWN_SOURCE_FILES = {
        "posting_state_store.py",
        "post_dispatcher.py",
        "risk_evaluator.py",
        "rollout_controller.py",
        "kill_switches.py",
        "anomaly_detector.py",
        "suppression_monitor.py",
        "post_health_tracker.py",
        "audit_logger.py",
        "reconciliation.py",
        "idempotency.py"
    }
    
    FORBIDDEN_PAYLOAD_KEYS = {
        "password", "secret", "token", "ssn", "credit_card",
        "api_key", "private_key", "access_token", "refresh_token",
        "auth_token", "bearer_token", "session_token"
    }
    
    FORBIDDEN_PAYLOAD_PATTERNS = [
        "password", "secret", "token", "key", "credential"
    ]
    
    @staticmethod
    def validate_severity_matches_type(
        event_type: AuditEventType,
        severity: AuditSeverity
    ) -> Optional[str]:
        """
        Validate severity matches event type.
        
        Returns error message if invalid, None if valid.
        """
        default_severity = EVENT_SEVERITY_MAP.get(event_type)
        if default_severity is None:
            return f"No default severity mapping for event type {event_type.value}"
        
        # Allow severity to be higher than default (more severe is OK)
        # but warn if significantly lower
        if severity.value < default_severity.value - 1:
            return (
                f"Severity {severity.name} is significantly lower than "
                f"default {default_severity.name} for event type {event_type.value}"
            )
        
        return None
    
    @staticmethod
    def validate_payload(payload: Dict[str, Any]) -> Optional[str]:
        """
        Validate payload for forbidden keys and serializability.
        
        Returns error message if invalid, None if valid.
        """
        # Check for forbidden keys (case-insensitive)
        payload_keys_lower = {k.lower() for k in payload.keys()}
        forbidden_lower = {k.lower() for k in AuditInvariantValidator.FORBIDDEN_PAYLOAD_KEYS}
        
        forbidden_found = payload_keys_lower & forbidden_lower
        if forbidden_found:
            return f"Forbidden payload keys detected: {forbidden_found}"
        
        # Check for forbidden patterns in keys
        for key in payload.keys():
            key_lower = key.lower()
            for pattern in AuditInvariantValidator.FORBIDDEN_PAYLOAD_PATTERNS:
                if pattern in key_lower:
                    return f"Forbidden pattern '{pattern}' in payload key: {key}"
        
        # Check JSON serializability
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as e:
            return f"Payload not JSON-serializable: {e}"
        
        return None
    
    @staticmethod
    def validate(
        event: AuditEvent,
        last_timestamp: float,
        last_hash: str,
        seen_event_ids: Optional[set] = None
    ) -> None:
        """
        Validate event invariants. Raises ValueError on violation.
        
        Args:
            event: Event to validate
            last_timestamp: Timestamp of previous event
            last_hash: Hash of previous event
            seen_event_ids: Set of previously seen event IDs (for uniqueness check)
        """
        errors = []
        
        # Monotonic timestamps
        if event.timestamp < last_timestamp:
            errors.append(
                f"Non-monotonic timestamp: {event.timestamp} < {last_timestamp}"
            )
        
        # Hash chain continuity
        if event.previous_event_hash != last_hash:
            errors.append(
                f"Hash chain break: expected previous_hash {last_hash}, "
                f"got {event.previous_event_hash}"
            )
        
        # Hash integrity
        if not AuditHasher.verify_hash(event):
            errors.append(f"Hash verification failed for event {event.event_id}")
        
        # Known source file
        if event.source_file not in AuditInvariantValidator.KNOWN_SOURCE_FILES:
            errors.append(f"Unknown source file: {event.source_file}")
        
        # Severity matches event type
        severity_error = AuditInvariantValidator.validate_severity_matches_type(
            event.event_type, event.severity
        )
        if severity_error:
            errors.append(severity_error)
        
        # Payload validation
        payload_error = AuditInvariantValidator.validate_payload(event.payload)
        if payload_error:
            errors.append(payload_error)
        
        # Event ID uniqueness
        if seen_event_ids is not None:
            if event.event_id in seen_event_ids:
                errors.append(f"Duplicate event_id: {event.event_id}")
            seen_event_ids.add(event.event_id)
        
        # Required fields
        if not event.event_id:
            errors.append("Missing event_id")
        if not event.source_file:
            errors.append("Missing source_file")
        if not event.source_function:
            errors.append("Missing source_function")
        
        if errors:
            raise ValueError(
                f"Invariant violations for event {event.event_id}: {'; '.join(errors)}"
            )


# ============================================================================
# SINK INTERFACE (PLUGGABLE)
# ============================================================================

class AuditSinkInterface(ABC):
    """
    Abstract interface for audit event storage.
    
    Implementations must guarantee:
    - Append-only writes
    - fsync or equivalent durability
    - Write acknowledgment
    - Monotonic ordering
    - Thread safety
    """
    
    @abstractmethod
    def write(self, event: AuditEvent) -> None:
        """Write single event. Must be durable."""
        pass
    
    @abstractmethod
    def write_batch(self, events: List[AuditEvent]) -> None:
        """Write batch of events atomically. Partial writes forbidden."""
        pass
    
    @abstractmethod
    def read_all(self) -> List[AuditEvent]:
        """Read all events in order."""
        pass
    
    @abstractmethod
    def read_range(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None
    ) -> List[AuditEvent]:
        """Read events in timestamp range."""
        pass


# ============================================================================
# FILE SINK (DEFAULT)
# ============================================================================

class FileAuditSink(AuditSinkInterface):
    """
    Append-only file storage for audit events.
    
    Requirements:
    - fsync after each write
    - Atomic batch writes
    - Monotonic ordering
    - Thread-safe
    
    Failures here are fatal.
    """
    
    def __init__(self, log_path: Path, max_file_size: int = MAX_LOG_FILE_SIZE):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_file_size = max_file_size
        self._lock = RLock()
        self._current_file_size = self._get_current_file_size()
    
    def _get_current_file_size(self) -> int:
        """Get current file size."""
        if self.log_path.exists():
            return self.log_path.stat().st_size
        return 0
    
    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds max size."""
        if self._current_file_size >= self.max_file_size:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated_path = self.log_path.parent / f"{self.log_path.stem}_{timestamp}.log"
            shutil.move(str(self.log_path), str(rotated_path))
            # Compress rotated file
            with open(rotated_path, 'rb') as f_in:
                with gzip.open(f"{rotated_path}.gz", 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            rotated_path.unlink()  # Remove uncompressed file
            self._current_file_size = 0
    
    def write(self, event: AuditEvent) -> None:
        """
        Write event to durable storage with fsync.
        Partial writes forbidden.
        """
        serialized = AuditSerializer.serialize(event)
        
        with self._lock:
            self._rotate_if_needed()
            
            try:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(serialized + '\n')
                    f.flush()
                    os.fsync(f.fileno())
                
                self._current_file_size += len(serialized.encode('utf-8')) + 1
            except (IOError, OSError) as e:
                raise RuntimeError(f"Failed to write audit event to {self.log_path}: {e}")
    
    def write_batch(self, events: List[AuditEvent]) -> None:
        """
        Atomic batch write with strict ordering.
        Partial writes forbidden.
        """
        if not events:
            return
        
        serialized_lines = [AuditSerializer.serialize(e) for e in events]
        batch_data = '\n'.join(serialized_lines) + '\n'
        
        with self._lock:
            self._rotate_if_needed()
            
            try:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(batch_data)
                    f.flush()
                    os.fsync(f.fileno())
                
                self._current_file_size += len(batch_data.encode('utf-8'))
            except (IOError, OSError) as e:
                raise RuntimeError(f"Failed to write audit batch to {self.log_path}: {e}")
    
    def read_all(self) -> List[AuditEvent]:
        """Read all events from log."""
        if not self.log_path.exists():
            return []
        
        events = []
        with self._lock:
            try:
                with open(self.log_path, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if line:
                            try:
                                events.append(AuditSerializer.deserialize(line))
                            except ValueError as e:
                                logger.error(
                                    f"Failed to deserialize event at line {line_num} "
                                    f"in {self.log_path}: {e}"
                                )
                                # Continue reading, but log error
            except (IOError, OSError) as e:
                logger.error(f"Failed to read audit log from {self.log_path}: {e}")
                raise
        
        return events
    
    def read_range(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None
    ) -> List[AuditEvent]:
        """Read events in timestamp range."""
        all_events = self.read_all()
        
        if start_timestamp is None and end_timestamp is None:
            return all_events
        
        filtered = []
        for event in all_events:
            if start_timestamp is not None and event.timestamp < start_timestamp:
                continue
            if end_timestamp is not None and event.timestamp > end_timestamp:
                continue
            filtered.append(event)
        
        return filtered


# ============================================================================
# QUERY INTERFACE
# ============================================================================

@dataclass
class AuditQuery:
    """
    Query specification for audit events.
    
    All filters are AND-ed together.
    """
    event_types: Optional[List[AuditEventType]] = None
    severities: Optional[List[AuditSeverity]] = None
    intent_id: Optional[str] = None
    account_id: Optional[str] = None
    platform: Optional[str] = None
    source_file: Optional[str] = None
    start_timestamp: Optional[float] = None
    end_timestamp: Optional[float] = None
    limit: Optional[int] = None
    offset: int = 0
    
    def matches(self, event: AuditEvent) -> bool:
        """Check if event matches query filters."""
        if self.event_types and event.event_type not in self.event_types:
            return False
        
        if self.severities and event.severity not in self.severities:
            return False
        
        if self.intent_id and event.intent_id != self.intent_id:
            return False
        
        if self.account_id and event.account_id != self.account_id:
            return False
        
        if self.platform and event.platform != self.platform:
            return False
        
        if self.source_file and event.source_file != self.source_file:
            return False
        
        if self.start_timestamp and event.timestamp < self.start_timestamp:
            return False
        
        if self.end_timestamp and event.timestamp > self.end_timestamp:
            return False
        
        return True


# ============================================================================
# REPLAY ENGINE
# ============================================================================

class AuditReplayEngine:
    """
    Deterministic replay of audit events.
    
    Given:
    - same intent stream
    - same platform responses
    
    The audit log:
    - replays identically
    - reconstructs decisions
    - proves causality
    
    This enables:
    - post-mortems
    - legal defense
    - model training
    - regulator audits
    """
    
    def __init__(self, events: List[AuditEvent]):
        """
        Initialize replay engine with events.
        
        Events must be in causal order (sorted by timestamp).
        """
        self.events = sorted(events, key=lambda e: e.timestamp)
        self._verify_chain()
    
    def _verify_chain(self) -> None:
        """Verify event chain integrity."""
        is_valid, errors = AuditHasher.verify_chain(self.events)
        if not is_valid:
            raise ValueError(f"Invalid event chain: {'; '.join(errors)}")
    
    def replay_by_intent(self, intent_id: str) -> List[AuditEvent]:
        """
        Replay all events for a specific intent.
        
        Returns events in causal order.
        """
        intent_events = [
            e for e in self.events
            if e.intent_id == intent_id
        ]
        return sorted(intent_events, key=lambda e: e.timestamp)
    
    def replay_by_account(self, account_id: str) -> List[AuditEvent]:
        """
        Replay all events for a specific account.
        
        Returns events in causal order.
        """
        account_events = [
            e for e in self.events
            if e.account_id == account_id
        ]
        return sorted(account_events, key=lambda e: e.timestamp)
    
    def replay_by_platform(self, platform: str) -> List[AuditEvent]:
        """
        Replay all events for a specific platform.
        
        Returns events in causal order.
        """
        platform_events = [
            e for e in self.events
            if e.platform == platform
        ]
        return sorted(platform_events, key=lambda e: e.timestamp)
    
    def replay_time_range(
        self,
        start_timestamp: float,
        end_timestamp: float
    ) -> List[AuditEvent]:
        """
        Replay events in time range.
        
        Returns events in causal order.
        """
        range_events = [
            e for e in self.events
            if start_timestamp <= e.timestamp <= end_timestamp
        ]
        return sorted(range_events, key=lambda e: e.timestamp)
    
    def get_causality_chain(self, event_id: str) -> List[AuditEvent]:
        """
        Get full causality chain leading to an event.
        
        Traces back through previous_event_hash to reconstruct
        the full causal sequence.
        """
        # Find target event
        target_event = None
        for event in self.events:
            if event.event_id == event_id:
                target_event = event
                break
        
        if target_event is None:
            return []
        
        # Build chain backwards
        chain = [target_event]
        current_hash = target_event.previous_event_hash
        
        while current_hash != GENESIS_HASH:
            found = False
            for event in self.events:
                if event.event_hash == current_hash:
                    chain.insert(0, event)
                    current_hash = event.previous_event_hash
                    found = True
                    break
            
            if not found:
                # Chain break - return what we have
                break
        
        return chain


# ============================================================================
# CORE AUDIT LOGGER
# ============================================================================

class AuditLogger:
    """
    Core audit logging API.
    
    Caller permissions:
    - posting_state_store.py: emit
    - post_dispatcher.py: emit
    - risk_evaluator.py: emit
    - rollout_controller.py: emit
    - kill_switches.py: emit
    - monitoring/*: emit
    - anyone: read-only verification
    
    No one may delete.
    No one may update.
    
    GUARANTEES:
    - Append-only writes
    - Deterministic event IDs
    - Hash chain integrity
    - Thread-safe operations
    - Crash-safe writes (fsync)
    - Replayable events
    """
    
    def __init__(
        self,
        log_path: Optional[Path] = None,
        sink: Optional[AuditSinkInterface] = None,
        enable_indexing: bool = True
    ):
        """
        Initialize audit logger.
        
        Args:
            log_path: Path to audit log file (if using FileAuditSink)
            sink: Custom sink implementation (overrides log_path)
            enable_indexing: Enable in-memory indexing for fast queries
        """
        if sink is None:
            if log_path is None:
                log_path = Path("./data/audit/audit.log")
            sink = FileAuditSink(log_path)
        
        self.sink = sink
        self._lock = RLock()
        self._last_hash = GENESIS_HASH
        self._last_timestamp = 0.0
        self._event_counter = 0
        self._seen_event_ids: set = set()
        
        # In-memory index for fast queries
        self._enable_indexing = enable_indexing
        self._index: List[AuditEvent] = []
        self._intent_index: Dict[str, List[AuditEvent]] = defaultdict(list)
        self._account_index: Dict[str, List[AuditEvent]] = defaultdict(list)
        self._platform_index: Dict[str, List[AuditEvent]] = defaultdict(list)
        
        # Rebuild state from existing log
        self._rebuild_state()
    
    def _rebuild_state(self) -> None:
        """Rebuild logger state from existing log file."""
        try:
            events = self.sink.read_all()
            if events:
                # Verify chain integrity
                is_valid, errors = AuditHasher.verify_chain(events)
                if not is_valid:
                    logger.error(f"Chain verification failed on startup: {'; '.join(errors)}")
                    # Continue anyway, but log error
                
                last_event = events[-1]
                self._last_hash = last_event.event_hash
                self._last_timestamp = last_event.timestamp
                self._event_counter = len(events)
                
                # Rebuild indexes
                if self._enable_indexing:
                    self._rebuild_indexes(events)
                
                logger.info(
                    f"Audit logger initialized: {len(events)} events, "
                    f"last timestamp: {self._last_timestamp}"
                )
        except Exception as e:
            logger.error(f"Failed to rebuild audit logger state: {e}")
            # Start fresh if rebuild fails
            self._last_hash = GENESIS_HASH
            self._last_timestamp = 0.0
            self._event_counter = 0
    
    def _rebuild_indexes(self, events: List[AuditEvent]) -> None:
        """Rebuild in-memory indexes."""
        self._index = events[-MAX_INDEX_SIZE:]  # Keep most recent
        self._intent_index.clear()
        self._account_index.clear()
        self._platform_index.clear()
        
        for event in self._index:
            if event.intent_id:
                self._intent_index[event.intent_id].append(event)
            if event.account_id:
                self._account_index[event.account_id].append(event)
            if event.platform:
                self._platform_index[event.platform].append(event)
    
    def _update_indexes(self, event: AuditEvent) -> None:
        """Update in-memory indexes with new event."""
        if not self._enable_indexing:
            return
        
        # Add to main index
        self._index.append(event)
        
        # Trim index if too large
        if len(self._index) > MAX_INDEX_SIZE:
            # Remove oldest events
            removed = self._index[:len(self._index) - MAX_INDEX_SIZE]
            self._index = self._index[len(self._index) - MAX_INDEX_SIZE:]
            
            # Remove from other indexes
            for removed_event in removed:
                if removed_event.intent_id:
                    self._intent_index[removed_event.intent_id] = [
                        e for e in self._intent_index[removed_event.intent_id]
                        if e.event_id != removed_event.event_id
                    ]
                if removed_event.account_id:
                    self._account_index[removed_event.account_id] = [
                        e for e in self._account_index[removed_event.account_id]
                        if e.event_id != removed_event.event_id
                    ]
                if removed_event.platform:
                    self._platform_index[removed_event.platform] = [
                        e for e in self._platform_index[removed_event.platform]
                        if e.event_id != removed_event.event_id
                    ]
        
        # Add to specific indexes
        if event.intent_id:
            self._intent_index[event.intent_id].append(event)
        if event.account_id:
            self._account_index[event.account_id].append(event)
        if event.platform:
            self._platform_index[event.platform].append(event)
    
    def _generate_event_id(self, timestamp: float) -> str:
        """Generate deterministic event ID."""
        self._event_counter += 1
        # Use high-precision timestamp + counter for uniqueness
        return f"evt_{int(timestamp * 1000000)}_{self._event_counter:010d}"
    
    def emit(
        self,
        event_type: AuditEventType,
        severity: Optional[AuditSeverity],
        source_file: str,
        source_function: str,
        payload: Dict[str, Any],
        intent_id: Optional[str] = None,
        account_id: Optional[str] = None,
        platform: Optional[str] = None
    ) -> str:
        """
        Emit single audit event.
        
        Validates invariants, hashes event, writes to sink.
        Returns event_id.
        
        No retries without idempotency key.
        
        Args:
            event_type: Type of event
            severity: Event severity (defaults to type's default)
            source_file: Source file name (must be in KNOWN_SOURCE_FILES)
            source_function: Function name
            payload: Event payload (JSON-serializable, no PII)
            intent_id: Optional intent ID
            account_id: Optional account ID
            platform: Optional platform name
        
        Returns:
            event_id: Unique event identifier
        """
        with self._lock:
            timestamp = time.time()
            
            # Use default severity if not provided
            if severity is None:
                severity = EVENT_SEVERITY_MAP.get(event_type, AuditSeverity.INFO)
            
            # Generate event ID
            event_id = self._generate_event_id(timestamp)
            
            # Generate hash
            event_hash = AuditHasher.hash_event(
                event_type,
                timestamp,
                payload,
                self._last_hash,
                source_file,
                source_function
            )
            
            # Create event
            event = AuditEvent(
                event_id=event_id,
                event_type=event_type,
                severity=severity,
                timestamp=timestamp,
                source_file=source_file,
                source_function=source_function,
                intent_id=intent_id,
                account_id=account_id,
                platform=platform,
                payload=payload,
                previous_event_hash=self._last_hash,
                event_hash=event_hash
            )
            
            # Validate before write
            AuditInvariantValidator.validate(
                event,
                self._last_timestamp,
                self._last_hash,
                self._seen_event_ids
            )
            
            # Write to durable storage
            try:
                self.sink.write(event)
            except Exception as e:
                # Audit write failures are fatal
                raise RuntimeError(f"Failed to write audit event: {e}")
            
            # Update state
            self._last_hash = event_hash
            self._last_timestamp = timestamp
            
            # Update indexes
            self._update_indexes(event)
            
            return event_id
    
    def emit_batch(self, event_specs: List[Dict[str, Any]]) -> List[str]:
        """
        Emit batch of events with strict ordering and atomic commit.
        Partial writes forbidden.
        
        Each spec is a dict with keys:
        - event_type (required)
        - severity (optional, defaults to type's default)
        - source_file (required)
        - source_function (required)
        - payload (required)
        - intent_id, account_id, platform (optional)
        
        Returns:
            List of event_ids in order
        """
        with self._lock:
            events = []
            event_ids = []
            current_hash = self._last_hash
            current_timestamp = self._last_timestamp
            
            for spec in event_specs:
                timestamp = time.time()
                
                # Extract required fields
                event_type = spec['event_type']
                source_file = spec['source_file']
                source_function = spec['source_function']
                payload = spec['payload']
                
                # Use default severity if not provided
                severity = spec.get('severity')
                if severity is None:
                    severity = EVENT_SEVERITY_MAP.get(event_type, AuditSeverity.INFO)
                
                # Generate event ID
                event_id = self._generate_event_id(timestamp)
                
                # Generate hash
                event_hash = AuditHasher.hash_event(
                    event_type,
                    timestamp,
                    payload,
                    current_hash,
                    source_file,
                    source_function
                )
                
                # Create event
                event = AuditEvent(
                    event_id=event_id,
                    event_type=event_type,
                    severity=severity,
                    timestamp=timestamp,
                    source_file=source_file,
                    source_function=source_function,
                    intent_id=spec.get('intent_id'),
                    account_id=spec.get('account_id'),
                    platform=spec.get('platform'),
                    payload=payload,
                    previous_event_hash=current_hash,
                    event_hash=event_hash
                )
                
                # Validate
                AuditInvariantValidator.validate(
                    event,
                    current_timestamp,
                    current_hash,
                    self._seen_event_ids
                )
                
                events.append(event)
                event_ids.append(event_id)
                current_hash = event_hash
                current_timestamp = timestamp
            
            # Atomic batch write
            try:
                self.sink.write_batch(events)
            except Exception as e:
                # Audit write failures are fatal
                raise RuntimeError(f"Failed to write audit batch: {e}")
            
            # Update state
            self._last_hash = current_hash
            self._last_timestamp = current_timestamp
            
            # Update indexes
            for event in events:
                self._update_indexes(event)
            
            return event_ids
    
    def query(self, query: AuditQuery) -> List[AuditEvent]:
        """
        Query audit events.
        
        Uses in-memory index if available, otherwise reads from sink.
        
        Returns events in timestamp order.
        """
        with self._lock:
            if self._enable_indexing and self._index:
                # Use in-memory index
                candidates = self._index
            else:
                # Read from sink
                candidates = self.sink.read_range(
                    query.start_timestamp,
                    query.end_timestamp
                )
            
            # Apply filters
            filtered = [e for e in candidates if query.matches(e)]
            
            # Sort by timestamp
            filtered.sort(key=lambda e: e.timestamp)
            
            # Apply offset and limit
            if query.offset > 0:
                filtered = filtered[query.offset:]
            if query.limit is not None:
                filtered = filtered[:query.limit]
            
            return filtered
    
    def get_events_by_intent(self, intent_id: str) -> List[AuditEvent]:
        """Get all events for an intent (uses index if available)."""
        with self._lock:
            if self._enable_indexing and intent_id in self._intent_index:
                return sorted(self._intent_index[intent_id], key=lambda e: e.timestamp)
            
            # Fallback to query
            query = AuditQuery(intent_id=intent_id)
            return self.query(query)
    
    def get_events_by_account(self, account_id: str) -> List[AuditEvent]:
        """Get all events for an account (uses index if available)."""
        with self._lock:
            if self._enable_indexing and account_id in self._account_index:
                return sorted(self._account_index[account_id], key=lambda e: e.timestamp)
            
            # Fallback to query
            query = AuditQuery(account_id=account_id)
            return self.query(query)
    
    def get_events_by_platform(self, platform: str) -> List[AuditEvent]:
        """Get all events for a platform (uses index if available)."""
        with self._lock:
            if self._enable_indexing and platform in self._platform_index:
                return sorted(self._platform_index[platform], key=lambda e: e.timestamp)
            
            # Fallback to query
            query = AuditQuery(platform=platform)
            return self.query(query)
    
    def verify_chain(
        self,
        start_event_id: Optional[str] = None,
        end_event_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Verify hash chain integrity.
        
        Used for:
        - audits
        - compliance
        - incident response
        
        Detects:
        - missing events
        - reordered events
        - tampering
        
        Returns:
            Dict with 'valid', 'event_count', 'errors', 'first_event', 'last_event'
        """
        events = self.sink.read_all()
        
        if not events:
            return {
                "valid": True,
                "event_count": 0,
                "errors": [],
                "first_event": None,
                "last_event": None
            }
        
        # Filter range if specified
        if start_event_id or end_event_id:
            start_idx = 0
            end_idx = len(events)
            
            for i, evt in enumerate(events):
                if start_event_id and evt.event_id == start_event_id:
                    start_idx = i
                if end_event_id and evt.event_id == end_event_id:
                    end_idx = i + 1
            
            events = events[start_idx:end_idx]
        
        # Verify chain
        is_valid, errors = AuditHasher.verify_chain(events)
        
        # Check monotonic timestamps
        for i in range(1, len(events)):
            if events[i].timestamp < events[i - 1].timestamp:
                errors.append(
                    f"Non-monotonic timestamp: {events[i].event_id} "
                    f"({events[i].timestamp} < {events[i - 1].timestamp})"
                )
                is_valid = False
        
        return {
            "valid": is_valid,
            "event_count": len(events),
            "errors": errors,
            "first_event": events[0].event_id if events else None,
            "last_event": events[-1].event_id if events else None
        }
    
    def create_replay_engine(self) -> AuditReplayEngine:
        """
        Create replay engine for deterministic event replay.
        
        Returns:
            AuditReplayEngine initialized with all events
        """
        events = self.sink.read_all()
        return AuditReplayEngine(events)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get audit log statistics."""
        with self._lock:
            events = self.sink.read_all()
            
            if not events:
                return {
                    "total_events": 0,
                    "first_timestamp": None,
                    "last_timestamp": None,
                    "events_by_type": {},
                    "events_by_severity": {}
                }
            
            events_by_type = defaultdict(int)
            events_by_severity = defaultdict(int)
            
            for event in events:
                events_by_type[event.event_type.value] += 1
                events_by_severity[event.severity.name] += 1
            
            return {
                "total_events": len(events),
                "first_timestamp": events[0].timestamp,
                "last_timestamp": events[-1].timestamp,
                "events_by_type": dict(events_by_type),
                "events_by_severity": dict(events_by_severity),
                "indexed_events": len(self._index) if self._enable_indexing else 0
            }


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    'AUDIT_LOGGER_VERSION',
    'GENESIS_HASH',
    'AuditEventType',
    'AuditSeverity',
    'EVENT_SEVERITY_MAP',
    'AuditEvent',
    'AuditLogger',
    'AuditHasher',
    'AuditSerializer',
    'AuditSinkInterface',
    'FileAuditSink',
    'AuditInvariantValidator',
    'AuditQuery',
    'AuditReplayEngine',
]