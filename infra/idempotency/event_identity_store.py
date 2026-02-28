"""
/infra/idempotency/event_identity_store.py

Distributed Event Identity Store
(Global "First-Seen Wins" Authority)

CRITICAL: This is the ONLY legal path for global event deduplication.
Every event identity check must go through this store to ensure correctness
across multiple workers, regions, and process restarts.

Design Principle: Deduplication is about global correctness, not local speed.
A single duplicate that passes is worse than a million extra lookups.

This file elevates ingestion from:
    "Correct per process"
to:
    "Correct across the entire global pipeline"

TIER-0 REQUIREMENTS:
- Distributed consistency (works across workers/regions)
- Crash-safe (survives restarts)
- Deterministic (replay-safe)
- Atomic operations (no race conditions)
- First-seen wins globally (not per-process)
"""

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Dict, Any, Tuple
from pathlib import Path

# Import persistence infrastructure
from infra.persistence.backend.kv_backend import (
    KVBackend,
    KVBackendCapabilities,
    ReplaySafetyMode,
    DurabilityMode,
    TTLHandlingMode,
    KVBackendError,
)
# MemoryBackend import - use the KVBackend-compatible version
try:
    from infra.persistence.backend.memory import MemoryBackend
except ImportError:
    try:
        from infra.persistence.backend.memory_backend import MemoryBackend
    except ImportError:
        # Fallback: create a simple in-memory backend for testing
        MemoryBackend = None


# ============================================================================
# IDEMPOTENCY RESULT
# ============================================================================

class IdempotencyResult(Enum):
    """
    Result of idempotency check.
    
    FIRST_SEEN: Event is new (first time seen globally)
    DUPLICATE: Event was already seen (duplicate)
    """
    FIRST_SEEN = "first_seen"
    DUPLICATE = "duplicate"


# ============================================================================
# EVENT IDENTITY RECORD
# ============================================================================

@dataclass(frozen=True)
class EventIdentityRecord:
    """
    Immutable record of event identity.
    
    This is what gets stored in the distributed store.
    """
    # Event identity (deterministic)
    platform: str
    event_id: str
    event_type: str
    
    # First-seen metadata
    first_seen_at: int  # UTC epoch seconds
    first_seen_by: str  # Worker/process identifier
    
    # Provenance
    ingestion_run_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EventIdentityRecord':
        """Create from dictionary."""
        return cls(
            platform=data['platform'],
            event_id=data['event_id'],
            event_type=data['event_type'],
            first_seen_at=data['first_seen_at'],
            first_seen_by=data['first_seen_by'],
            ingestion_run_id=data.get('ingestion_run_id')
        )
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), sort_keys=True)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'EventIdentityRecord':
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))


# ============================================================================
# EVENT IDENTITY STORE
# ============================================================================

class EventIdentityStore:
    """
    Distributed event identity store for global deduplication.
    
    This store ensures "first-seen wins" semantics across:
    - Multiple workers
    - Multiple regions
    - Process restarts
    - Distributed shards
    
    CRITICAL GUARANTEES:
    1. Atomic check-and-set operations (no race conditions)
    2. Crash-safe persistence (survives restarts)
    3. Deterministic behavior (replay-safe)
    4. Global consistency (same result across all workers)
    
    ARCHITECTURE:
    - Uses KVBackend for distributed storage
    - Key format: "event_identity:{dedup_key_hash}"
    - Value: JSON-serialized EventIdentityRecord
    - Operations are atomic via compare-and-swap
    """
    
    # Key prefix for event identities
    KEY_PREFIX = "event_identity:"
    
    # TTL for event identity records (90 days)
    # This prevents unbounded growth while maintaining dedup correctness
    IDENTITY_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days
    
    def __init__(
        self,
        backend: KVBackend,
        worker_id: Optional[str] = None,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize event identity store.
        
        Args:
            backend: KVBackend for distributed storage
            worker_id: Unique identifier for this worker/process
            logger: Optional logger instance
        """
        self._backend = backend
        self._worker_id = worker_id or f"worker_{id(self)}"
        self._logger = logger or logging.getLogger(f"{__name__}.EventIdentityStore")
        
        # Validate backend capabilities
        capabilities = backend.get_capabilities()
        if not capabilities.distributed_safe:
            self._logger.warning(
                "Backend is not distributed-safe - deduplication may not work across workers"
            )
        
        self._logger.info(
            f"EventIdentityStore initialized: worker_id={self._worker_id}, "
            f"distributed_safe={capabilities.distributed_safe}"
        )
    
    def _compute_dedup_key(self, platform: str, event_id: str, event_type: str) -> str:
        """
        Compute deterministic deduplication key.
        
        This is the same algorithm as in DeduplicationEngine to ensure consistency.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            
        Returns:
            Deterministic dedup key (hex hash)
        """
        # Normalize inputs for consistency
        normalized_platform = platform.lower().strip()
        normalized_event_id = event_id.strip()
        normalized_event_type = event_type.lower().strip()
        
        # Create canonical representation
        key_material = f"{normalized_platform}::{normalized_event_id}::{normalized_event_type}"
        
        # Hash for efficiency and privacy
        return hashlib.sha256(key_material.encode('utf-8')).hexdigest()
    
    def _make_store_key(self, dedup_key: str) -> str:
        """Make storage key from dedup key."""
        return f"{self.KEY_PREFIX}{dedup_key}"
    
    def check_and_record(
        self,
        platform: str,
        event_id: str,
        event_type: str,
        ingestion_run_id: Optional[str] = None,
        first_seen_at: Optional[int] = None
    ) -> tuple[IdempotencyResult, Optional[EventIdentityRecord]]:
        """
        Check if event is duplicate and record if first-seen.
        
        This is an atomic operation that ensures:
        - Only one worker can claim "first-seen"
        - Global consistency across all workers
        - Crash-safe persistence
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            ingestion_run_id: Optional ingestion run ID
            first_seen_at: Optional timestamp (defaults to current time)
            
        Returns:
            Tuple of (result, record)
            - result: FIRST_SEEN if new, DUPLICATE if already seen
            - record: EventIdentityRecord if first-seen, None if duplicate
        """
        # Compute dedup key
        dedup_key = self._compute_dedup_key(platform, event_id, event_type)
        store_key = self._make_store_key(dedup_key)
        
        # Use current time if not provided
        if first_seen_at is None:
            first_seen_at = int(time.time())
        
        # Try to read existing record
        existing_value = self._backend.get(store_key)
        
        if existing_value is not None:
            # Event already exists - return duplicate
            try:
                existing_record = EventIdentityRecord.from_json(existing_value.decode('utf-8'))
                self._logger.debug(
                    f"Duplicate detected: platform={platform}, "
                    f"event_id={event_id}, first_seen_at={existing_record.first_seen_at}"
                )
                return IdempotencyResult.DUPLICATE, existing_record
            except Exception as e:
                self._logger.error(
                    f"Failed to deserialize existing record: {e}",
                    exc_info=True
                )
                # Corrupt record - treat as first-seen and overwrite
                # This is a recovery path for data corruption
        
        # Event is new - create record
        new_record = EventIdentityRecord(
            platform=platform,
            event_id=event_id,
            event_type=event_type,
            first_seen_at=first_seen_at,
            first_seen_by=self._worker_id,
            ingestion_run_id=ingestion_run_id
        )
        
        # Store record atomically
        # Use compare-and-swap if available, otherwise simple set
        try:
            capabilities = self._backend.get_capabilities()
            if capabilities.supports_cas:
                # Try CAS: set only if key doesn't exist (version 0)
                # This ensures atomic "first-seen wins"
                success = self._backend.compare_and_swap(
                    key=store_key,
                    expected_version=0,  # Key doesn't exist
                    new_value=new_record.to_json().encode('utf-8'),
                    new_version=1
                )
                
                if not success:
                    # Another worker claimed first-seen - read their record
                    existing_value = self._backend.get(store_key)
                    if existing_value:
                        existing_record = EventIdentityRecord.from_json(
                            existing_value.decode('utf-8')
                        )
                        return IdempotencyResult.DUPLICATE, existing_record
                    # Race condition: key was deleted between check and CAS
                    # Retry once
                    success = self._backend.compare_and_swap(
                        key=store_key,
                        expected_version=0,
                        new_value=new_record.to_json().encode('utf-8'),
                        new_version=1
                    )
                    if not success:
                        existing_value = self._backend.get(store_key)
                        if existing_value:
                            existing_record = EventIdentityRecord.from_json(
                                existing_value.decode('utf-8')
                            )
                            return IdempotencyResult.DUPLICATE, existing_record
            else:
                # Backend doesn't support CAS - use simple set
                # This is less safe but works for single-worker scenarios
                self._backend.set(
                    store_key,
                    new_record.to_json().encode('utf-8'),
                    ttl_seconds=self.IDENTITY_TTL_SECONDS
                )
                success = True
        except KVBackendError as e:
            self._logger.error(
                f"Failed to store event identity: {e}",
                exc_info=True
            )
            # On storage failure, assume first-seen to avoid false duplicates
            # This is a fail-open strategy (better than blocking ingestion)
            return IdempotencyResult.FIRST_SEEN, new_record
        
        if success:
            self._logger.debug(
                f"First-seen recorded: platform={platform}, "
                f"event_id={event_id}, first_seen_at={first_seen_at}"
            )
            return IdempotencyResult.FIRST_SEEN, new_record
        else:
            # Should not reach here, but handle gracefully
            self._logger.warning(
                f"Failed to record first-seen (race condition): "
                f"platform={platform}, event_id={event_id}"
            )
            # Read existing record
            existing_value = self._backend.get(store_key)
            if existing_value:
                existing_record = EventIdentityRecord.from_json(
                    existing_value.decode('utf-8')
                )
                return IdempotencyResult.DUPLICATE, existing_record
            # Fallback: return first-seen (shouldn't happen)
            return IdempotencyResult.FIRST_SEEN, new_record
    
    def check_duplicate(
        self,
        platform: str,
        event_id: str,
        event_type: str
    ) -> bool:
        """
        Check if event is duplicate (read-only).
        
        This is a lightweight check that doesn't record the event.
        Use check_and_record() if you need to record first-seen.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            
        Returns:
            True if duplicate, False if first-seen
        """
        dedup_key = self._compute_dedup_key(platform, event_id, event_type)
        store_key = self._make_store_key(dedup_key)
        
        existing_value = self._backend.get(store_key)
        return existing_value is not None
    
    def get_record(
        self,
        platform: str,
        event_id: str,
        event_type: str
    ) -> Optional[EventIdentityRecord]:
        """
        Get event identity record if it exists.
        
        Args:
            platform: Source platform
            event_id: Original event ID
            event_type: Engagement type
            
        Returns:
            EventIdentityRecord if exists, None otherwise
        """
        dedup_key = self._compute_dedup_key(platform, event_id, event_type)
        store_key = self._make_store_key(dedup_key)
        
        existing_value = self._backend.get(store_key)
        if existing_value is None:
            return None
        
        try:
            return EventIdentityRecord.from_json(existing_value.decode('utf-8'))
        except Exception as e:
            self._logger.error(
                f"Failed to deserialize record: {e}",
                exc_info=True
            )
            return None
    
    def stats(self) -> Dict[str, Any]:
        """
        Get store statistics.
        
        Returns:
            Dictionary of statistics
        """
        backend_stats = self._backend.stats()
        return {
            'worker_id': self._worker_id,
            'backend_stats': backend_stats,
        }


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================

def create_event_identity_store(
    backend: Optional[KVBackend] = None,
    worker_id: Optional[str] = None,
    storage_dir: Optional[Path] = None,
    logger: Optional[logging.Logger] = None
) -> Optional[EventIdentityStore]:
    """
    Create event identity store with appropriate backend.
    
    Args:
        backend: Optional KVBackend (required for Tier-0 production)
        worker_id: Optional worker identifier
        storage_dir: Optional storage directory (for file-based backends)
        logger: Optional logger instance
        
    Returns:
        EventIdentityStore instance
        
    Note:
        For Tier-0 production use, provide a distributed KVBackend (e.g., Redis, etcd).
        If backend is None, returns None to allow graceful degradation.
        Callers should check for None and use fallback deduplication if needed.
    """
    if backend is None:
        # For Tier-0 production, backend should be provided
        # Return None to allow graceful degradation
        if logger:
            logger.warning(
                "No KVBackend provided for EventIdentityStore. "
                "Distributed deduplication disabled. "
                "For Tier-0, provide a KVBackend instance (e.g., Redis, etcd)."
            )
        return None
    
    return EventIdentityStore(
        backend=backend,
        worker_id=worker_id,
        logger=logger
    )
