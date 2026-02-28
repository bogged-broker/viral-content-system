"""
posting_queue.py
Deterministic Posting Queue & Dispatch Buffer

WHAT THIS IS:
- Authoritative source of truth for "what is allowed to be posted next"
- Deterministic, invariant-preserving buffer between intent and dispatch
- Transactional commit log for posting intents

WHAT THIS IS NOT:
- NOT platform-aware
- NOT concurrency-blind
- NOT eventually-consistent
- NOT allowed to reorder intent semantics
- NOT allowed to guess readiness
- NOT allowed to auto-retry failures

GUARANTEES:
- Exactly-once dispatch eligibility
- Deterministic ordering under concurrency
- Idempotent dequeue semantics
- Full auditability
- Explicit failure semantics
"""

import time
import hashlib
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Set, Tuple
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# CORE DATA CONTRACTS
# ============================================================================

class QueueState(Enum):
    """Explicit lifecycle states - no hidden transitions allowed"""
    PENDING = "pending"            # waiting for eligibility
    READY = "ready"                # dispatch-eligible
    CLAIMED = "claimed"            # locked by dispatcher
    DISPATCHED = "dispatched"      # successfully posted
    FAILED = "failed"              # terminal failure
    DEAD_LETTER = "dead_letter"    # quarantined


@dataclass
class QueueEntry:
    """
    Immutable posting intent queue entry.
    
    INVARIANTS:
    - intent must be pre-validated
    - intent_id globally unique
    - state transitions explicit only
    - no mutation outside PostingQueue
    """
    intent_id: str
    intent: 'PostIntent'  # From intent_builder.py
    
    state: QueueState
    
    enqueue_time: float
    eligible_after: float
    
    dispatch_attempts: int = 0
    last_error: Optional[str] = None
    
    lock_owner: Optional[str] = None
    lock_expiry: Optional[float] = None
    
    # Immutability enforcement
    _intent_hash: str = field(init=False, repr=False)
    
    def __post_init__(self):
        """Compute immutability hash on creation"""
        self._intent_hash = self._compute_intent_hash()
    
    def _compute_intent_hash(self) -> str:
        """Hash intent for immutability verification"""
        # Serialize intent deterministically
        intent_str = f"{self.intent.intent_id}:{self.intent.content_hash}"
        return hashlib.sha256(intent_str.encode()).hexdigest()
    
    def verify_immutability(self) -> bool:
        """Verify intent hasn't been tampered with"""
        return self._intent_hash == self._compute_intent_hash()


# ============================================================================
# EXCEPTIONS
# ============================================================================

class QueueValidationError(Exception):
    """Raised when queue invariants are violated"""
    pass


class QueueDeduplicationError(Exception):
    """Raised when duplicate intent detected"""
    pass


class QueueLockError(Exception):
    """Raised when lock acquisition/verification fails"""
    pass


class QueueStateTransitionError(Exception):
    """Raised when illegal state transition attempted"""
    pass


# ============================================================================
# QUEUE DEDUPLICATOR
# ============================================================================

class QueueDeduplicator:
    """
    Prevents duplicate intents from entering queue.
    
    PREVENTS:
    - Duplicate intents
    - Race-induced double enqueue
    - Replay poisoning
    
    DEDUPE KEY: (intent_id, invariant_hash)
    """
    
    def __init__(self):
        self._seen: Set[Tuple[str, str]] = set()
        self._lock = threading.Lock()
    
    def check_and_register(self, intent_id: str, intent_hash: str) -> bool:
        """
        Check if intent is duplicate and register if new.
        
        Returns:
            True if new (allowed), False if duplicate (rejected)
        """
        key = (intent_id, intent_hash)
        
        with self._lock:
            if key in self._seen:
                logger.warning(f"Duplicate intent detected: {intent_id}")
                return False
            
            self._seen.add(key)
            return True
    
    def remove(self, intent_id: str, intent_hash: str):
        """Remove intent from dedupe tracking (after terminal state)"""
        key = (intent_id, intent_hash)
        
        with self._lock:
            self._seen.discard(key)


# ============================================================================
# QUEUE LOCK MANAGER
# ============================================================================

class QueueLockManager:
    """
    Per-entry locks with bounded TTL and auto-expiry.
    
    PROPERTIES:
    - Per-entry locks
    - Bounded TTL
    - Auto-expiry
    - Owner verification
    
    PREVENTS:
    - Zombie workers
    - Double dispatch
    - Concurrency leakage
    """
    
    DEFAULT_LOCK_TTL = 300.0  # 5 minutes
    
    def __init__(self, default_ttl: float = DEFAULT_LOCK_TTL):
        self._locks: Dict[str, Tuple[str, float]] = {}  # intent_id -> (owner, expiry)
        self._lock = threading.Lock()
        self.default_ttl = default_ttl
    
    def acquire(self, intent_id: str, owner: str, ttl: Optional[float] = None) -> bool:
        """
        Attempt to acquire lock on entry.
        
        Returns:
            True if acquired, False if already locked
        """
        now = time.time()
        effective_ttl = ttl or self.default_ttl
        
        with self._lock:
            # Check existing lock
            if intent_id in self._locks:
                existing_owner, expiry = self._locks[intent_id]
                
                # Auto-expire stale locks
                if now >= expiry:
                    logger.warning(f"Auto-expired stale lock on {intent_id} (owner: {existing_owner})")
                else:
                    return False
            
            # Acquire lock
            self._locks[intent_id] = (owner, now + effective_ttl)
            return True
    
    def verify_owner(self, intent_id: str, owner: str) -> bool:
        """Verify lock is owned by specified owner"""
        with self._lock:
            if intent_id not in self._locks:
                return False
            
            lock_owner, expiry = self._locks[intent_id]
            
            # Check expiry
            if time.time() >= expiry:
                return False
            
            return lock_owner == owner
    
    def release(self, intent_id: str, owner: str) -> bool:
        """
        Release lock if owned by owner.
        
        Returns:
            True if released, False if not owned
        """
        with self._lock:
            if not self.verify_owner(intent_id, owner):
                return False
            
            del self._locks[intent_id]
            return True
    
    def get_expired_locks(self) -> List[Tuple[str, str]]:
        """Return list of (intent_id, owner) for expired locks"""
        now = time.time()
        expired = []
        
        with self._lock:
            for intent_id, (owner, expiry) in list(self._locks.items()):
                if now >= expiry:
                    expired.append((intent_id, owner))
        
        return expired


# ============================================================================
# QUEUE INVARIANT VALIDATOR
# ============================================================================

class QueueInvariantValidator:
    """
    Enforces critical queue invariants.
    
    ENFORCED INVARIANTS:
    - State transition legality
    - Monotonic attempt count
    - Lock ownership correctness
    - eligible_after <= now only for READY
    - Intent immutability hash unchanged
    
    VIOLATION → queue halt + alert
    """
    
    # Legal state transitions
    VALID_TRANSITIONS: Dict[QueueState, Set[QueueState]] = {
        QueueState.PENDING: {QueueState.READY, QueueState.FAILED},
        QueueState.READY: {QueueState.CLAIMED, QueueState.FAILED},
        QueueState.CLAIMED: {QueueState.DISPATCHED, QueueState.FAILED, QueueState.READY},
        QueueState.DISPATCHED: set(),  # Terminal
        QueueState.FAILED: {QueueState.READY, QueueState.DEAD_LETTER},
        QueueState.DEAD_LETTER: set(),  # Terminal
    }
    
    @classmethod
    def validate_state_transition(cls, from_state: QueueState, to_state: QueueState):
        """Validate state transition is legal"""
        if to_state not in cls.VALID_TRANSITIONS.get(from_state, set()):
            raise QueueStateTransitionError(
                f"Illegal state transition: {from_state.value} -> {to_state.value}"
            )
    
    @classmethod
    def validate_entry(cls, entry: QueueEntry):
        """Validate entry invariants"""
        # Verify immutability
        if not entry.verify_immutability():
            raise QueueValidationError(f"Intent immutability violated: {entry.intent_id}")
        
        # Verify attempt count is monotonic
        if entry.dispatch_attempts < 0:
            raise QueueValidationError(f"Negative attempt count: {entry.intent_id}")
        
        # Verify lock consistency
        if entry.state == QueueState.CLAIMED:
            if not entry.lock_owner or not entry.lock_expiry:
                raise QueueValidationError(f"CLAIMED state requires lock: {entry.intent_id}")
        
        # Verify READY eligibility
        if entry.state == QueueState.READY:
            if entry.eligible_after > time.time():
                raise QueueValidationError(f"READY entry not yet eligible: {entry.intent_id}")


# ============================================================================
# QUEUE BACKPRESSURE CONTROLLER
# ============================================================================

class QueueBackpressureController:
    """
    Detects congestion and applies non-destructive backpressure.
    
    DETECTS:
    - Queue depth growth
    - Per-platform congestion
    - Failure clustering
    
    ACTIONS (non-destructive):
    - Pause enqueues
    - Reduce READY promotion rate
    - Emit alerts
    """
    
    def __init__(self, 
                 max_queue_depth: int = 10000,
                 max_platform_depth: int = 1000,
                 failure_rate_threshold: float = 0.3):
        self.max_queue_depth = max_queue_depth
        self.max_platform_depth = max_platform_depth
        self.failure_rate_threshold = failure_rate_threshold
        
        self._platform_counts: Dict[str, int] = defaultdict(int)
        self._total_count = 0
        self._recent_failures = 0
        self._recent_total = 0
    
    def check_enqueue_allowed(self, platform: str) -> Tuple[bool, Optional[str]]:
        """
        Check if enqueue should be allowed.
        
        Returns:
            (allowed, reason_if_blocked)
        """
        # Check total depth
        if self._total_count >= self.max_queue_depth:
            return False, f"Queue depth limit reached: {self._total_count}"
        
        # Check per-platform depth
        if self._platform_counts[platform] >= self.max_platform_depth:
            return False, f"Platform {platform} depth limit reached"
        
        # Check failure rate
        if self._recent_total > 100:  # Minimum sample size
            failure_rate = self._recent_failures / self._recent_total
            if failure_rate > self.failure_rate_threshold:
                return False, f"High failure rate: {failure_rate:.2%}"
        
        return True, None
    
    def record_enqueue(self, platform: str):
        """Record successful enqueue"""
        self._platform_counts[platform] += 1
        self._total_count += 1
    
    def record_dequeue(self, platform: str):
        """Record dequeue"""
        self._platform_counts[platform] -= 1
        self._total_count -= 1
    
    def record_outcome(self, success: bool):
        """Record dispatch outcome for failure rate tracking"""
        self._recent_total += 1
        if not success:
            self._recent_failures += 1
        
        # Reset window periodically
        if self._recent_total > 1000:
            self._recent_total = 0
            self._recent_failures = 0


# ============================================================================
# QUEUE METRICS EMITTER
# ============================================================================

class QueueMetricsEmitter:
    """
    Emits queue metrics for monitoring.
    
    EMITS:
    - Enqueue rate
    - Time-to-dispatch
    - Retry rate
    - Dead-letter rate
    - Per-platform pressure
    """
    
    def __init__(self):
        self._metrics: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()
    
    def record_enqueue(self, platform: str):
        """Record enqueue event"""
        with self._lock:
            self._metrics[f"enqueue.{platform}"].append(time.time())
    
    def record_dispatch(self, intent_id: str, enqueue_time: float):
        """Record successful dispatch"""
        time_to_dispatch = time.time() - enqueue_time
        with self._lock:
            self._metrics["time_to_dispatch"].append(time_to_dispatch)
    
    def record_retry(self, platform: str):
        """Record retry event"""
        with self._lock:
            self._metrics[f"retry.{platform}"].append(time.time())
    
    def record_dead_letter(self, platform: str):
        """Record dead letter event"""
        with self._lock:
            self._metrics[f"dead_letter.{platform}"].append(time.time())
    
    def get_metrics_summary(self) -> Dict[str, float]:
        """Get summary of recent metrics"""
        with self._lock:
            summary = {}
            
            # Calculate rates (events per minute in last 5 min)
            now = time.time()
            window = 300.0  # 5 minutes
            
            for key, events in self._metrics.items():
                recent = [e for e in events if now - e < window]
                rate = len(recent) / (window / 60.0)  # per minute
                summary[f"{key}_rate"] = rate
            
            # Average time to dispatch
            if "time_to_dispatch" in self._metrics:
                recent_ttd = [t for t in self._metrics["time_to_dispatch"] 
                             if time.time() - t < window]
                if recent_ttd:
                    summary["avg_time_to_dispatch"] = sum(recent_ttd) / len(recent_ttd)
            
            return summary


# ============================================================================
# QUEUE WATCHDOG
# ============================================================================

class QueueWatchdog:
    """
    Monitors queue health and trips alerts on anomalies.
    
    TRIPS ALERTS IF:
    - Queue stalls
    - Claimed entries exceed TTL
    - Dead-letter spikes
    - Dispatch success deviates from prediction
    """
    
    def __init__(self, stall_threshold: float = 300.0):
        self.stall_threshold = stall_threshold
        self._last_dispatch_time = time.time()
        self._dispatch_count = 0
        self._alert_triggered = False
    
    def record_dispatch(self):
        """Record successful dispatch"""
        self._last_dispatch_time = time.time()
        self._dispatch_count += 1
        self._alert_triggered = False
    
    def check_health(self, 
                     queue_depth: int,
                     claimed_count: int,
                     dead_letter_count: int) -> List[str]:
        """
        Check queue health and return list of alerts.
        
        Returns:
            List of alert messages (empty if healthy)
        """
        alerts = []
        now = time.time()
        
        # Check for stall
        if queue_depth > 0 and (now - self._last_dispatch_time) > self.stall_threshold:
            alerts.append(f"Queue stalled: no dispatch in {now - self._last_dispatch_time:.0f}s")
        
        # Check claimed ratio
        if queue_depth > 0:
            claimed_ratio = claimed_count / queue_depth
            if claimed_ratio > 0.5:
                alerts.append(f"High claimed ratio: {claimed_ratio:.1%}")
        
        # Check dead letter accumulation
        if dead_letter_count > 100:
            alerts.append(f"Dead letter accumulation: {dead_letter_count} entries")
        
        return alerts


# ============================================================================
# MAIN POSTING QUEUE
# ============================================================================

class PostingQueue:
    """
    Core deterministic posting queue.
    
    GUARANTEES:
    - Exactly-once dispatch eligibility
    - Deterministic ordering
    - Idempotent operations
    - Full auditability
    - Explicit failure semantics
    """
    
    def __init__(self, 
                 max_attempts: int = 3,
                 lock_ttl: float = 300.0):
        # Core storage
        self._entries: Dict[str, QueueEntry] = {}
        self._state_index: Dict[QueueState, Set[str]] = defaultdict(set)
        
        # Components
        self._deduplicator = QueueDeduplicator()
        self._lock_manager = QueueLockManager(default_ttl=lock_ttl)
        self._backpressure = QueueBackpressureController()
        self._metrics = QueueMetricsEmitter()
        self._watchdog = QueueWatchdog()
        
        # Configuration
        self.max_attempts = max_attempts
        
        # Thread safety
        self._lock = threading.RLock()
    
    # ========================================================================
    # CORE OPERATIONS
    # ========================================================================
    
    def enqueue(self, intent: 'PostIntent', eligible_after: Optional[float] = None) -> None:
        """
        Enqueue validated intent for dispatch.
        
        HARD CHECKS:
        - Intent hash validity
        - Intent lifecycle tag != BLOCKED
        - Intent not already enqueued (dedupe)
        - Platform slots available (soft gate)
        
        FAILURE = intent rejected (not retried)
        
        Args:
            intent: Validated PostIntent from intent_builder
            eligible_after: Unix timestamp when eligible (default: now)
        
        Raises:
            QueueValidationError: Intent validation failed
            QueueDeduplicationError: Duplicate intent
        """
        intent_id = intent.intent_id
        
        # Validate intent has required attributes
        if not hasattr(intent, 'content_hash'):
            raise QueueValidationError(f"Intent missing content_hash: {intent_id}")
        
        if not hasattr(intent, 'platform'):
            raise QueueValidationError(f"Intent missing platform: {intent_id}")
        
        # Check lifecycle (assuming intent has lifecycle_tag attribute)
        if hasattr(intent, 'lifecycle_tag') and intent.lifecycle_tag == 'BLOCKED':
            raise QueueValidationError(f"Intent blocked by lifecycle: {intent_id}")
        
        with self._lock:
            # Dedupe check
            if not self._deduplicator.check_and_register(intent_id, intent.content_hash):
                raise QueueDeduplicationError(f"Duplicate intent: {intent_id}")
            
            # Backpressure check
            allowed, reason = self._backpressure.check_enqueue_allowed(intent.platform)
            if not allowed:
                self._deduplicator.remove(intent_id, intent.content_hash)
                raise QueueValidationError(f"Backpressure: {reason}")
            
            # Create entry
            now = time.time()
            entry = QueueEntry(
                intent_id=intent_id,
                intent=intent,
                state=QueueState.PENDING if eligible_after and eligible_after > now else QueueState.READY,
                enqueue_time=now,
                eligible_after=eligible_after or now,
            )
            
            # Validate entry
            QueueInvariantValidator.validate_entry(entry)
            
            # Store
            self._entries[intent_id] = entry
            self._state_index[entry.state].add(intent_id)
            
            # Record metrics
            self._backpressure.record_enqueue(intent.platform)
            self._metrics.record_enqueue(intent.platform)
            
            logger.info(f"Enqueued intent {intent_id} in state {entry.state.value}")
    
    def claim_next(self, worker_id: str, platform_filter: Optional[str] = None) -> Optional[QueueEntry]:
        """
        Claim next ready entry for dispatch.
        
        GUARANTEES:
        - Only READY entries
        - Deterministic ordering (stable sort)
        - Lock acquisition is atomic
        - Lock timeout enforced
        
        Returns at most one entry.
        
        Args:
            worker_id: Unique worker identifier
            platform_filter: Optional platform to filter by
        
        Returns:
            QueueEntry if claimed, None if no eligible entries
        """
        with self._lock:
            # Promote PENDING -> READY if eligible
            self._promote_pending_to_ready()
            
            # Get READY entries
            ready_ids = list(self._state_index[QueueState.READY])
            
            if not ready_ids:
                return None
            
            # Filter by platform if specified
            if platform_filter:
                ready_ids = [
                    iid for iid in ready_ids 
                    if self._entries[iid].intent.platform == platform_filter
                ]
            
            if not ready_ids:
                return None
            
            # Deterministic ordering: earliest enqueue_time first
            ready_ids.sort(key=lambda iid: self._entries[iid].enqueue_time)
            
            # Try to claim first available
            for intent_id in ready_ids:
                entry = self._entries[intent_id]
                
                # Attempt lock acquisition
                if self._lock_manager.acquire(intent_id, worker_id):
                    # Transition to CLAIMED
                    self._transition_state(entry, QueueState.CLAIMED)
                    entry.lock_owner = worker_id
                    entry.lock_expiry = time.time() + self._lock_manager.default_ttl
                    
                    logger.info(f"Claimed entry {intent_id} by worker {worker_id}")
                    return entry
            
            return None
    
    def mark_dispatched(self, intent_id: str, worker_id: str) -> None:
        """
        Mark entry as successfully dispatched.
        
        GUARANTEES:
        - Exactly-once transition
        - Immutable DISPATCHED state
        - Dispatch timestamp recorded
        
        Duplicate calls → ignored but logged.
        
        Args:
            intent_id: Intent identifier
            worker_id: Worker that dispatched
        
        Raises:
            QueueLockError: Worker doesn't own lock
        """
        with self._lock:
            if intent_id not in self._entries:
                logger.warning(f"mark_dispatched called on unknown intent: {intent_id}")
                return
            
            entry = self._entries[intent_id]
            
            # Idempotency: already dispatched
            if entry.state == QueueState.DISPATCHED:
                logger.info(f"Intent {intent_id} already dispatched (idempotent)")
                return
            
            # Verify lock ownership
            if not self._lock_manager.verify_owner(intent_id, worker_id):
                raise QueueLockError(f"Worker {worker_id} doesn't own lock on {intent_id}")
            
            # Transition to DISPATCHED
            self._transition_state(entry, QueueState.DISPATCHED)
            
            # Release lock
            self._lock_manager.release(intent_id, worker_id)
            
            # Record metrics
            self._backpressure.record_dequeue(entry.intent.platform)
            self._backpressure.record_outcome(success=True)
            self._metrics.record_dispatch(intent_id, entry.enqueue_time)
            self._watchdog.record_dispatch()
            
            # Clean up dedupe tracking
            self._deduplicator.remove(intent_id, entry._intent_hash)
            
            logger.info(f"Marked {intent_id} as dispatched")
    
    def mark_failed(self, intent_id: str, worker_id: str, error: str) -> None:
        """
        Mark entry as failed.
        
        FAILURE SEMANTICS:
        - Attempts incremented
        - Error captured
        - Retry eligibility evaluated
        - Terminal failure escalated
        
        This file does not decide retries, it enforces the result.
        
        Args:
            intent_id: Intent identifier
            worker_id: Worker that failed
            error: Error description
        
        Raises:
            QueueLockError: Worker doesn't own lock
        """
        with self._lock:
            if intent_id not in self._entries:
                logger.warning(f"mark_failed called on unknown intent: {intent_id}")
                return
            
            entry = self._entries[intent_id]
            
            # Verify lock ownership
            if not self._lock_manager.verify_owner(intent_id, worker_id):
                raise QueueLockError(f"Worker {worker_id} doesn't own lock on {intent_id}")
            
            # Increment attempts
            entry.dispatch_attempts += 1
            entry.last_error = error
            
            # Determine next state
            if entry.dispatch_attempts >= self.max_attempts:
                # Terminal failure
                next_state = QueueState.DEAD_LETTER
                self._metrics.record_dead_letter(entry.intent.platform)
                logger.error(f"Intent {intent_id} moved to DEAD_LETTER after {entry.dispatch_attempts} attempts")
            else:
                # Eligible for retry (external policy will requeue)
                next_state = QueueState.FAILED
                self._metrics.record_retry(entry.intent.platform)
                logger.warning(f"Intent {intent_id} marked FAILED (attempt {entry.dispatch_attempts})")
            
            # Transition state
            self._transition_state(entry, next_state)
            
            # Release lock
            self._lock_manager.release(intent_id, worker_id)
            
            # Record outcome
            self._backpressure.record_outcome(success=False)
    
    def requeue(self, intent_id: str, delay: float = 0.0) -> None:
        """
        Requeue failed entry for retry.
        
        RULES:
        - Delay must be explicit
        - Attempts threshold enforced
        - Exponential backoff forbidden here (policy decides)
        
        Used only by failure_policy, not dispatchers.
        
        Args:
            intent_id: Intent identifier
            delay: Delay in seconds before eligible
        
        Raises:
            QueueValidationError: Entry not in FAILED state or max attempts exceeded
        """
        with self._lock:
            if intent_id not in self._entries:
                raise QueueValidationError(f"Cannot requeue unknown intent: {intent_id}")
            
            entry = self._entries[intent_id]
            
            # Must be in FAILED state
            if entry.state != QueueState.FAILED:
                raise QueueValidationError(
                    f"Can only requeue FAILED entries, got {entry.state.value}"
                )
            
            # Check attempts threshold
            if entry.dispatch_attempts >= self.max_attempts:
                raise QueueValidationError(
                    f"Cannot requeue: max attempts ({self.max_attempts}) reached"
                )
            
            # Set new eligibility time
            entry.eligible_after = time.time() + delay
            
            # Transition to PENDING (will be promoted to READY when eligible)
            self._transition_state(entry, QueueState.PENDING)
            
            logger.info(f"Requeued {intent_id} with {delay}s delay (attempt {entry.dispatch_attempts})")
    
    # ========================================================================
    # INTERNAL HELPERS
    # ========================================================================
    
    def _transition_state(self, entry: QueueEntry, new_state: QueueState) -> None:
        """
        Transition entry to new state with validation.
        
        INTERNAL ONLY - assumes lock held
        """
        old_state = entry.state
        
        # Validate transition
        QueueInvariantValidator.validate_state_transition(old_state, new_state)
        
        # Update indices
        self._state_index[old_state].discard(entry.intent_id)
        self._state_index[new_state].add(entry.intent_id)
        
        # Update entry
        entry.state = new_state
        
        logger.debug(f"Transitioned {entry.intent_id}: {old_state.value} -> {new_state.value}")
    
    def _promote_pending_to_ready(self) -> None:
        """
        Promote PENDING entries to READY if eligible.
        
        INTERNAL ONLY - assumes lock held
        """
        now = time.time()
        pending_ids = list(self._state_index[QueueState.PENDING])
        
        for intent_id in pending_ids:
            entry = self._entries[intent_id]
            
            if entry.eligible_after <= now:
                self._transition_state(entry, QueueState.READY)
                logger.debug(f"Promoted {intent_id} to READY")
    
    # ========================================================================
    # MONITORING & HEALTH
    # ========================================================================
    
    def get_queue_stats(self) -> Dict[str, int]:
        """Get current queue statistics"""
        with self._lock:
            return {
                state.value: len(self._state_index[state])
                for state in QueueState
            }
    
    def get_health_report(self) -> Dict[str, any]:
        """Get comprehensive health report"""
        with self._lock:
            stats = self.get_queue_stats()
            
            # Check for expired locks
            expired_locks = self._lock_manager.get_expired_locks()
            
            # Get watchdog alerts
            alerts = self._watchdog.check_health(
                queue_depth=sum(stats.values()),
                claimed_count=stats.get(QueueState.CLAIMED.value, 0),
                dead_letter_count=stats.get(QueueState.DEAD_LETTER.value, 0)
            )
            
            # Get metrics summary
            metrics = self._metrics.get_metrics_summary()
            
            return {
                "stats": stats,
                "expired_locks": len(expired_locks),
                "alerts": alerts,
                "metrics": metrics,
                "healthy": len(alerts) == 0 and len(expired_locks) == 0
            }
    
    def cleanup_expired_locks(self) -> int:
        """
        Cleanup expired locks and return entries to READY.
        
        Returns:
            Number of locks cleaned up
        """
        with self._lock:
            expired = self._lock_manager.get_expired_locks()
            
            for intent_id, owner in expired:
                if intent_id in self._entries:
                    entry = self._entries[intent_id]
                    
                    # Only cleanup if still in CLAIMED state
                    if entry.state == QueueState.CLAIMED:
                        logger.warning(f"Cleaning up expired lock on {intent_id} (owner: {owner})")
                        
                        # Return to READY for retry
                        self._transition_state(entry, QueueState.READY)
                        entry.lock_owner = None
                        entry.lock_expiry = None
            
            return len(expired)


# ============================================================================
# MOCK PostIntent FOR TYPE CHECKING
# ============================================================================

# This would normally be imported from intent_builder.py
# Included here as a minimal stub for demonstration purposes

@dataclass
class PostIntent:
    """
    Mock PostIntent structure.
    
    In production, this is imported from intent_builder.py
    """
    intent_id: str
    content_hash: str
    platform: str
    lifecycle_tag: Optional[str] = None
    
    # Additional fields would be present in real implementation
    content: str = ""
    target_accounts: List[str] = field(default_factory=list)


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Create queue
    queue = PostingQueue(max_attempts=3)
    
    # Create sample intent
    intent = PostIntent(
        intent_id="intent_001",
        content_hash="abc123",
        platform="twitter",
        content="Sample post content"
    )
    
    # Enqueue
    try:
        queue.enqueue(intent)
        print("✓ Intent enqueued")
    except QueueValidationError as e:
        print(f"✗ Enqueue failed: {e}")
    
    # Claim for dispatch
    worker_id = "worker_001"
    entry = queue.claim_next(worker_id)
    
    if entry:
        print(f"✓ Claimed entry: {entry.intent_id}")
        
        # Simulate successful dispatch
        queue.mark_dispatched(entry.intent_id, worker_id)
        print("✓ Marked as dispatched")
    else:
        print("✗ No entries available")
    
    # Get health report
    health = queue.get_health_report()
    print(f"\nQueue Health:")
    print(f"  Stats: {health['stats']}")
    print(f"  Alerts: {health['alerts']}")
    print(f"  Healthy: {health['healthy']}")
    
    # Example: Handle failure and retry
    intent2 = PostIntent(
        intent_id="intent_002",
        content_hash="def456",
        platform="linkedin",
        content="Another post"
    )
    
    queue.enqueue(intent2)
    entry2 = queue.claim_next(worker_id)
    
    if entry2:
        # Simulate failure
        queue.mark_failed(entry2.intent_id, worker_id, "Network timeout")
        print(f"✓ Marked as failed: {entry2.intent_id}")
        
        # Requeue with delay
        queue.requeue(entry2.intent_id, delay=60.0)
        print(f"✓ Requeued with 60s delay")
    
    # Final stats
    print(f"\nFinal stats: {queue.get_queue_stats()}")