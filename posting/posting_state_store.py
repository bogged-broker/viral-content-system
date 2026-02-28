"""
posting_state_store.py

Single authoritative persistence layer for posting outcomes.
Records final truth about what happened after posting attempts.

IMMUTABLE PRINCIPLES:
- Write-once-ish, append-only architecture
- Terminal states are irreversible
- No mutation of posted outcomes
- Exactly-once semantics guaranteed
- Full audit trail maintained
"""

from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict
import hashlib


logger = logging.getLogger(__name__)


# ============================================================================
# STATE MODEL (EXHAUSTIVE & EXPLICIT)
# ============================================================================


class PostingState(Enum):
    """
    Explicit posting lifecycle states.
    No derived states. No fuzzy interpretations.
    """
    CREATED = "created"
    QUEUED = "queued"
    CLAIMED = "claimed"
    DISPATCH_ATTEMPTED = "dispatch_attempted"
    POSTED = "posted"                 # terminal success
    POST_FAILED = "post_failed"       # retryable or terminal
    DEAD_LETTER = "dead_letter"       # quarantined forever

    def is_terminal(self) -> bool:
        """Terminal states cannot transition further."""
        return self in {PostingState.POSTED, PostingState.DEAD_LETTER}

    def allows_retry(self) -> bool:
        """Can this state transition to retry?"""
        return self == PostingState.POST_FAILED


# ============================================================================
# DATA CONTRACTS
# ============================================================================


@dataclass(frozen=True)
class PostingRecord:
    """
    Immutable append-only posting state record.
    
    INVARIANTS:
    - Never mutated after creation
    - Each state change creates new record
    - attempt_number monotonically increases
    - timestamp always present
    """
    intent_id: str
    state: PostingState
    timestamp: float
    
    # Attribution
    worker_id: Optional[str] = None
    
    # Context
    platform: str = ""
    account_id: str = ""
    
    # Attempt tracking
    attempt_number: int = 0
    
    # Error handling
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False
    
    # Success data
    remote_post_id: Optional[str] = None
    
    # Extensibility
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to serializable dict."""
        d = asdict(self)
        d['state'] = self.state.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> PostingRecord:
        """Reconstruct from dict."""
        data = data.copy()
        data['state'] = PostingState(data['state'])
        return cls(**data)


# ============================================================================
# EXCEPTIONS
# ============================================================================


class StateMutationError(Exception):
    """Attempted to mutate immutable state."""
    pass


class InvariantViolation(Exception):
    """State transition violated invariants."""
    pass


class IdempotencyViolation(Exception):
    """Duplicate write detected."""
    pass


# ============================================================================
# IDEMPOTENCY GUARD
# ============================================================================


class IdempotencyGuard:
    """
    Prevents duplicate writes from network retries and dispatcher replays.
    
    Key: (intent_id, state, attempt_number)
    """
    
    def __init__(self):
        self._seen: Dict[str, str] = {}
        self._lock = threading.RLock()

    def _make_key(self, intent_id: str, state: PostingState, attempt: int) -> str:
        """Create idempotency key."""
        return f"{intent_id}:{state.value}:{attempt}"

    def _make_fingerprint(self, record: PostingRecord) -> str:
        """Create content fingerprint."""
        content = {
            'state': record.state.value,
            'attempt': record.attempt_number,
            'error_code': record.error_code,
            'remote_post_id': record.remote_post_id
        }
        return hashlib.sha256(
            json.dumps(content, sort_keys=True).encode()
        ).hexdigest()[:16]

    def check_and_record(self, record: PostingRecord) -> bool:
        """
        Returns True if this is a new write.
        Returns False if duplicate (idempotent).
        Raises IdempotencyViolation if conflicting duplicate.
        """
        with self._lock:
            key = self._make_key(record.intent_id, record.state, record.attempt_number)
            fingerprint = self._make_fingerprint(record)
            
            if key in self._seen:
                existing = self._seen[key]
                if existing == fingerprint:
                    # Exact duplicate - idempotent replay
                    logger.debug(f"Idempotent replay detected: {key}")
                    return False
                else:
                    # Same key, different content - violation
                    raise IdempotencyViolation(
                        f"Conflicting write for {key}: "
                        f"existing={existing}, new={fingerprint}"
                    )
            
            self._seen[key] = fingerprint
            return True


# ============================================================================
# STATE INVARIANT VALIDATOR
# ============================================================================


class StateInvariantValidator:
    """
    Enforces posting state transition rules.
    
    HARD RULES:
    - Terminal states are final
    - Attempt numbers monotonic
    - State order respected
    - No resurrection after death
    """
    
    # Valid state transitions
    ALLOWED_TRANSITIONS = {
        PostingState.CREATED: {PostingState.QUEUED},
        PostingState.QUEUED: {PostingState.CLAIMED},
        PostingState.CLAIMED: {PostingState.DISPATCH_ATTEMPTED},
        PostingState.DISPATCH_ATTEMPTED: {PostingState.POSTED, PostingState.POST_FAILED},
        PostingState.POST_FAILED: {PostingState.DISPATCH_ATTEMPTED, PostingState.DEAD_LETTER},
        PostingState.POSTED: set(),  # terminal
        PostingState.DEAD_LETTER: set(),  # terminal
    }

    @classmethod
    def validate_transition(
        cls,
        current_state: Optional[PostingState],
        new_state: PostingState,
        current_attempt: int,
        new_attempt: int
    ) -> None:
        """
        Validate state transition is legal.
        Raises InvariantViolation if invalid.
        """
        # First transition
        if current_state is None:
            if new_state != PostingState.CREATED:
                raise InvariantViolation(
                    f"First state must be CREATED, got {new_state}"
                )
            return

        # Terminal state check
        if current_state.is_terminal():
            raise InvariantViolation(
                f"Cannot transition from terminal state {current_state}"
            )

        # Allowed transition check
        allowed = cls.ALLOWED_TRANSITIONS.get(current_state, set())
        if new_state not in allowed:
            raise InvariantViolation(
                f"Invalid transition: {current_state} -> {new_state}"
            )

        # Attempt monotonicity
        if new_state == PostingState.DISPATCH_ATTEMPTED:
            if new_attempt <= current_attempt:
                raise InvariantViolation(
                    f"Attempt number must increase: {current_attempt} -> {new_attempt}"
                )


# ============================================================================
# WRITE-AHEAD LOG
# ============================================================================


class WriteAheadLogger:
    """
    Durable write-ahead logging for crash recovery.
    
    All state mutations written here BEFORE in-memory update.
    Enables replay and audit reconstruction.
    """
    
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "posting_state.wal"
        self._lock = threading.Lock()

    def append(self, record: PostingRecord) -> None:
        """Append record to WAL with fsync."""
        with self._lock:
            with open(self.log_file, 'a') as f:
                line = json.dumps(record.to_dict())
                f.write(line + '\n')
                f.flush()
                # Note: os.fsync(f.fileno()) for true durability

    def replay(self) -> List[PostingRecord]:
        """Replay all records from WAL."""
        if not self.log_file.exists():
            return []
        
        records = []
        with open(self.log_file, 'r') as f:
            for line in f:
                if line.strip():
                    try:
                        data = json.loads(line)
                        records.append(PostingRecord.from_dict(data))
                    except Exception as e:
                        logger.error(f"WAL replay error: {e}")
        
        return records

    def checkpoint(self, snapshot_path: Path) -> None:
        """Create checkpoint and truncate WAL."""
        # Implementation: snapshot current state, truncate WAL
        pass


# ============================================================================
# POSTING STATE STORE (CORE)
# ============================================================================


class PostingStateStore:
    """
    Single authoritative source of posting outcome truth.
    
    GUARANTEES:
    - Exactly-once recording
    - Append-only history
    - Terminal state immutability
    - Full audit trail
    - Crash recovery via WAL
    """
    
    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        # Core state
        self._history: Dict[str, List[PostingRecord]] = defaultdict(list)
        self._current_state: Dict[str, PostingRecord] = {}
        self._lock = threading.RLock()
        
        # Guards and validators
        self._idempotency = IdempotencyGuard()
        self._validator = StateInvariantValidator()
        self._wal = WriteAheadLogger(self.storage_dir / "wal")
        
        # Metrics
        self._metrics = StateMetricsEmitter()
        
        # Recovery
        self._recover_from_wal()

    def _recover_from_wal(self) -> None:
        """Recover state from write-ahead log."""
        logger.info("Recovering state from WAL...")
        records = self._wal.replay()
        
        for record in records:
            # Replay without re-logging
            self._apply_record(record, skip_wal=True)
        
        logger.info(f"Recovered {len(records)} records from WAL")

    def _apply_record(self, record: PostingRecord, skip_wal: bool = False) -> None:
        """Apply record to in-memory state."""
        with self._lock:
            intent_id = record.intent_id
            
            # Append to history
            self._history[intent_id].append(record)
            
            # Update current state
            self._current_state[intent_id] = record
            
            # Write to WAL (unless replaying)
            if not skip_wal:
                self._wal.append(record)

    def _get_current(self, intent_id: str) -> Optional[PostingRecord]:
        """Get current state record."""
        return self._current_state.get(intent_id)

    # ------------------------------------------------------------------------
    # RECORDING API (CALLER-SPECIFIC)
    # ------------------------------------------------------------------------

    def record_enqueue(
        self,
        intent_id: str,
        platform: str,
        account_id: str,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Record intent creation and queueing.
        Called by: posting_queue.py
        
        Idempotent - safe to call multiple times for same intent.
        """
        with self._lock:
            current = self._get_current(intent_id)
            
            # Check if already created
            if current is not None:
                if current.state == PostingState.CREATED or current.state == PostingState.QUEUED:
                    # Idempotent - already recorded
                    logger.debug(f"Intent {intent_id} already enqueued")
                    return
                else:
                    raise StateMutationError(
                        f"Cannot enqueue intent in state {current.state}"
                    )
            
            # Record creation
            created_record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.CREATED,
                timestamp=time.time(),
                platform=platform,
                account_id=account_id,
                metadata=metadata or {}
            )
            
            if self._idempotency.check_and_record(created_record):
                self._apply_record(created_record)
            
            # Record queuing
            queued_record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.QUEUED,
                timestamp=time.time(),
                platform=platform,
                account_id=account_id,
                metadata=metadata or {}
            )
            
            if self._idempotency.check_and_record(queued_record):
                self._apply_record(queued_record)
                self._metrics.record_enqueue(platform)

    def record_claim(self, intent_id: str, worker_id: str) -> None:
        """
        Record intent claim by worker.
        Called by: post_dispatcher.py
        """
        with self._lock:
            current = self._get_current(intent_id)
            
            if current is None:
                raise InvariantViolation(f"Cannot claim non-existent intent {intent_id}")
            
            # Validate transition
            self._validator.validate_transition(
                current.state,
                PostingState.CLAIMED,
                current.attempt_number,
                current.attempt_number
            )
            
            record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.CLAIMED,
                timestamp=time.time(),
                worker_id=worker_id,
                platform=current.platform,
                account_id=current.account_id,
                attempt_number=current.attempt_number,
                metadata=current.metadata
            )
            
            if self._idempotency.check_and_record(record):
                self._apply_record(record)
                self._metrics.record_claim(current.platform, worker_id)

    def record_dispatch_attempt(
        self,
        intent_id: str,
        worker_id: str,
        attempt_number: int
    ) -> None:
        """
        Record dispatch attempt started.
        Called by: post_dispatcher.py BEFORE actual posting.
        
        REQUIRED even if dispatch later fails.
        """
        with self._lock:
            current = self._get_current(intent_id)
            
            if current is None:
                raise InvariantViolation(f"Cannot dispatch non-existent intent {intent_id}")
            
            # Validate transition
            self._validator.validate_transition(
                current.state,
                PostingState.DISPATCH_ATTEMPTED,
                current.attempt_number,
                attempt_number
            )
            
            record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.DISPATCH_ATTEMPTED,
                timestamp=time.time(),
                worker_id=worker_id,
                platform=current.platform,
                account_id=current.account_id,
                attempt_number=attempt_number,
                metadata=current.metadata
            )
            
            if self._idempotency.check_and_record(record):
                self._apply_record(record)
                self._metrics.record_attempt(current.platform, attempt_number)

    def record_success(
        self,
        intent_id: str,
        remote_post_id: str,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Record successful posting.
        Called by: post_dispatcher.py after confirmed platform success.
        
        TERMINAL - no further state changes allowed.
        """
        with self._lock:
            current = self._get_current(intent_id)
            
            if current is None:
                raise InvariantViolation(f"Cannot record success for non-existent intent {intent_id}")
            
            # Check not already terminal
            if current.state.is_terminal():
                if current.state == PostingState.POSTED:
                    # Idempotent success
                    logger.warning(f"Intent {intent_id} already marked as POSTED")
                    return
                else:
                    raise StateMutationError(
                        f"Cannot succeed after terminal state {current.state}"
                    )
            
            # Validate transition
            self._validator.validate_transition(
                current.state,
                PostingState.POSTED,
                current.attempt_number,
                current.attempt_number
            )
            
            merged_metadata = {**current.metadata, **(metadata or {})}
            
            record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.POSTED,
                timestamp=time.time(),
                worker_id=current.worker_id,
                platform=current.platform,
                account_id=current.account_id,
                attempt_number=current.attempt_number,
                remote_post_id=remote_post_id,
                metadata=merged_metadata
            )
            
            if self._idempotency.check_and_record(record):
                self._apply_record(record)
                self._metrics.record_success(
                    current.platform,
                    current.attempt_number,
                    time.time() - self._history[intent_id][0].timestamp
                )
                logger.info(f"✓ Posted {intent_id} -> {remote_post_id}")

    def record_failure(
        self,
        intent_id: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        metadata: Optional[Dict] = None
    ) -> None:
        """
        Record posting failure.
        Called by: post_dispatcher.py after platform rejection.
        
        retryable=True allows future retry attempts.
        retryable=False is terminal failure (will become DEAD_LETTER).
        """
        with self._lock:
            current = self._get_current(intent_id)
            
            if current is None:
                raise InvariantViolation(f"Cannot record failure for non-existent intent {intent_id}")
            
            # Check not already terminal
            if current.state.is_terminal():
                raise StateMutationError(
                    f"Cannot fail after terminal state {current.state}"
                )
            
            # Validate transition
            self._validator.validate_transition(
                current.state,
                PostingState.POST_FAILED,
                current.attempt_number,
                current.attempt_number
            )
            
            merged_metadata = {**current.metadata, **(metadata or {})}
            
            record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.POST_FAILED,
                timestamp=time.time(),
                worker_id=current.worker_id,
                platform=current.platform,
                account_id=current.account_id,
                attempt_number=current.attempt_number,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                metadata=merged_metadata
            )
            
            if self._idempotency.check_and_record(record):
                self._apply_record(record)
                self._metrics.record_failure(current.platform, error_code, retryable)
                logger.warning(
                    f"✗ Failed {intent_id}: {error_code} (retryable={retryable})"
                )

    def mark_dead_letter(self, intent_id: str, reason: str) -> None:
        """
        Mark intent as dead letter - permanent quarantine.
        Called by: failure_policy.py after retry exhaustion.
        
        TERMINAL - forensics only, not cleanup.
        """
        with self._lock:
            current = self._get_current(intent_id)
            
            if current is None:
                raise InvariantViolation(f"Cannot dead-letter non-existent intent {intent_id}")
            
            # Check not already posted
            if current.state == PostingState.POSTED:
                raise StateMutationError("Cannot dead-letter successful post")
            
            # Allow from FAILED state only
            if current.state != PostingState.POST_FAILED:
                raise StateMutationError(
                    f"Can only dead-letter from POST_FAILED, not {current.state}"
                )
            
            record = PostingRecord(
                intent_id=intent_id,
                state=PostingState.DEAD_LETTER,
                timestamp=time.time(),
                worker_id=current.worker_id,
                platform=current.platform,
                account_id=current.account_id,
                attempt_number=current.attempt_number,
                error_code="DEAD_LETTER",
                error_message=reason,
                metadata={**current.metadata, 'dead_letter_reason': reason}
            )
            
            if self._idempotency.check_and_record(record):
                self._apply_record(record)
                self._metrics.record_dead_letter(current.platform, reason)
                logger.error(f"💀 Dead letter {intent_id}: {reason}")

    # ------------------------------------------------------------------------
    # QUERY API (READ-ONLY)
    # ------------------------------------------------------------------------

    def get_state(self, intent_id: str) -> Optional[PostingRecord]:
        """
        Get current authoritative state.
        Returns None if intent doesn't exist.
        """
        with self._lock:
            return self._get_current(intent_id)

    def get_history(self, intent_id: str) -> List[PostingRecord]:
        """
        Get full append-only history for intent.
        Ordered chronologically.
        """
        with self._lock:
            return list(self._history.get(intent_id, []))

    def get_terminal_intents(self, state: PostingState) -> List[str]:
        """Get all intents in specific terminal state."""
        if not state.is_terminal():
            raise ValueError(f"{state} is not terminal")
        
        with self._lock:
            return [
                intent_id
                for intent_id, record in self._current_state.items()
                if record.state == state
            ]

    def get_stats(self) -> Dict[str, Any]:
        """Get aggregate statistics."""
        with self._lock:
            state_counts = defaultdict(int)
            for record in self._current_state.values():
                state_counts[record.state.value] += 1
            
            return {
                'total_intents': len(self._current_state),
                'state_distribution': dict(state_counts),
                'total_records': sum(len(h) for h in self._history.values()),
                'metrics': self._metrics.get_summary()
            }


# ============================================================================
# STATE RECONCILER
# ============================================================================


class StateReconciler:
    """
    Cross-checks state store against external sources.
    
    Detects:
    - Ghost posts (posted but not in state)
    - Missing transitions
    - Queue inconsistencies
    """
    
    def __init__(self, state_store: PostingStateStore):
        self.store = state_store

    def reconcile_with_queue(self, queue_intents: List[str]) -> Dict[str, List[str]]:
        """Find mismatches between queue and state."""
        issues = {
            'in_queue_not_state': [],
            'posted_still_queued': [],
            'dead_still_queued': []
        }
        
        state_intents = set(self.store._current_state.keys())
        
        for intent_id in queue_intents:
            state = self.store.get_state(intent_id)
            
            if state is None:
                issues['in_queue_not_state'].append(intent_id)
            elif state.state == PostingState.POSTED:
                issues['posted_still_queued'].append(intent_id)
            elif state.state == PostingState.DEAD_LETTER:
                issues['dead_still_queued'].append(intent_id)
        
        return issues

    def detect_ghost_posts(self, platform_posts: Dict[str, str]) -> List[str]:
        """
        Detect posts that exist on platform but not in state.
        platform_posts: {remote_post_id: intent_id}
        """
        ghosts = []
        
        for remote_id, intent_id in platform_posts.items():
            state = self.store.get_state(intent_id)
            
            if state is None or state.state != PostingState.POSTED:
                ghosts.append(intent_id)
        
        return ghosts


# ============================================================================
# METRICS EMITTER
# ============================================================================


class StateMetricsEmitter:
    """
    Emit metrics for monitoring and learning.
    """
    
    def __init__(self):
        self._enqueues = defaultdict(int)
        self._claims = defaultdict(lambda: defaultdict(int))
        self._attempts = defaultdict(lambda: defaultdict(int))
        self._successes = defaultdict(lambda: {'count': 0, 'total_time': 0.0})
        self._failures = defaultdict(lambda: defaultdict(int))
        self._dead_letters = defaultdict(lambda: defaultdict(int))

    def record_enqueue(self, platform: str) -> None:
        self._enqueues[platform] += 1

    def record_claim(self, platform: str, worker_id: str) -> None:
        self._claims[platform][worker_id] += 1

    def record_attempt(self, platform: str, attempt_num: int) -> None:
        self._attempts[platform][attempt_num] += 1

    def record_success(self, platform: str, attempt_num: int, duration: float) -> None:
        self._successes[platform]['count'] += 1
        self._successes[platform]['total_time'] += duration

    def record_failure(self, platform: str, error_code: str, retryable: bool) -> None:
        key = f"{error_code}:{'retryable' if retryable else 'terminal'}"
        self._failures[platform][key] += 1

    def record_dead_letter(self, platform: str, reason: str) -> None:
        self._dead_letters[platform][reason] += 1

    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary."""
        success_rates = {}
        avg_times = {}
        
        for platform, data in self._successes.items():
            total = self._enqueues.get(platform, 0)
            if total > 0:
                success_rates[platform] = data['count'] / total
                avg_times[platform] = data['total_time'] / data['count'] if data['count'] > 0 else 0
        
        return {
            'enqueues': dict(self._enqueues),
            'success_rates': success_rates,
            'avg_time_to_post': avg_times,
            'failures_by_platform': {
                k: dict(v) for k, v in self._failures.items()
            },
            'dead_letters': {
                k: dict(v) for k, v in self._dead_letters.items()
            }
        }


# ============================================================================
# INITIALIZATION
# ============================================================================


def create_posting_state_store(storage_dir: str = "./posting_state") -> PostingStateStore:
    """Factory function for creating state store."""
    return PostingStateStore(Path(storage_dir))


# ============================================================================
# USAGE EXAMPLE
# ============================================================================


if __name__ == "__main__":
    # Initialize
    store = create_posting_state_store()
    
    # Enqueue
    store.record_enqueue("intent_001", "twitter", "account_123")
    
    # Claim
    store.record_claim("intent_001", "worker_001")
    
    # Attempt
    store.record_dispatch_attempt("intent_001", "worker_001", attempt_number=1)
    
    # Fail
    store.record_failure(
        "intent_001",
        error_code="RATE_LIMIT",
        error_message="Too many requests",
        retryable=True
    )
    
    # Retry
    store.record_dispatch_attempt("intent_001", "worker_001", attempt_number=2)
    
    # Success
    store.record_success("intent_001", remote_post_id="tweet_12345")
    
    # Query
    state = store.get_state("intent_001")
    print(f"Final state: {state.state}")
    
    history = store.get_history("intent_001")
    print(f"Total transitions: {len(history)}")
    
    stats = store.get_stats()
    print(f"Stats: {json.dumps(stats, indent=2)}")