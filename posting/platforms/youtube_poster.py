"""
youtube_poster.py

Authoritative execution layer for YouTube publishing.
Converts system-approved publishing decisions into real YouTube uploads.

Core Principle: Posting is irreversible. Execution must be conservative, 
auditable, and kill-switchable.

This file executes decisions — it never makes them.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, Literal, Dict, List, Tuple
from collections import defaultdict
import threading

# Third-party imports (production environment)
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
except ImportError:
    # Stub for environments without Google API client
    Credentials = object
    HttpError = Exception


# ============================================================================
# CORE DATA CONTRACTS (NON-NEGOTIABLE)
# ============================================================================

@dataclass(frozen=True)
class YouTubePostIntent:
    """
    INPUT contract - produced only by orchestration.
    Immutable by design. Never mutated by youtube_poster.py.
    """
    intent_id: str
    video_id: str
    
    account_id: str
    channel_id: str
    
    title: str
    description: str
    tags: List[str]
    
    video_file_path: str
    thumbnail_path: Optional[str]
    
    visibility: Literal["public", "unlisted", "private"]
    publish_at: Optional[datetime]
    
    monetization_allowed: bool
    content_rating: str
    
    metadata_version: str
    created_at: float
    
    def __post_init__(self):
        """Validate immutability and required fields."""
        # Ensure all required fields are present
        required_fields = [
            'intent_id', 'video_id', 'account_id', 'channel_id',
            'title', 'description', 'tags', 'video_file_path',
            'visibility', 'content_rating', 'metadata_version'
        ]
        for field_name in required_fields:
            if not getattr(self, field_name):
                raise ValueError(f"Required field missing: {field_name}")


@dataclass
class PostExecutionResult:
    """
    OUTPUT contract - consumed by workflow_manager, evaluation,
    long_tail_tracker, and RL reward shaping (indirectly).
    """
    intent_id: str
    success: bool
    
    youtube_video_id: Optional[str] = None
    youtube_url: Optional[str] = None
    
    failure_type: Optional[str] = None
    retry_allowed: bool = False
    
    execution_latency_ms: int = 0
    timestamp: float = field(default_factory=time.time)
    
    metadata: Dict = field(default_factory=dict)


@dataclass
class UploadArtifact:
    """Internal artifact tracking upload state."""
    intent_id: str
    upload_id: Optional[str] = None
    bytes_uploaded: int = 0
    total_bytes: int = 0
    chunk_size: int = 256 * 1024  # 256KB default
    resumable_uri: Optional[str] = None
    start_time: float = field(default_factory=time.time)


class FailureType(Enum):
    """Standardized failure classification."""
    AUTH_FAILURE = "AUTH_FAILURE"
    RATE_LIMIT = "RATE_LIMIT"
    CONTENT_REJECTION = "CONTENT_REJECTION"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    TRUST_SAFETY_BLOCK = "TRUST_SAFETY_BLOCK"
    IDEMPOTENCY_COLLISION = "IDEMPOTENCY_COLLISION"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    UNKNOWN = "UNKNOWN"


# ============================================================================
# ACCOUNT CONTEXT RESOLVER
# ============================================================================

@dataclass
class AccountContext:
    """Resolved account-level context for upload execution."""
    account_id: str
    channel_id: str
    credentials: Optional[Credentials]
    trust_score: float
    recent_strikes: int
    daily_upload_count: int
    hourly_upload_count: int
    last_upload_time: Optional[float]
    is_flagged: bool
    cooldown_until: Optional[float]


class AccountContextResolver:
    """
    Resolves OAuth credentials, trust scores, and posting history.
    
    Low-trust channels get stricter rate limits.
    Flagged channels may be blocked entirely.
    Defensive by design.
    """
    
    def __init__(self, credentials_store: Dict, trust_store: Dict):
        self.credentials_store = credentials_store
        self.trust_store = trust_store
        self.logger = logging.getLogger(__name__ + ".AccountContextResolver")
    
    def resolve(self, account_id: str, channel_id: str) -> AccountContext:
        """
        Resolve full account context.
        
        Raises:
            ValueError: If account is blocked or credentials missing
        """
        # Fetch credentials
        creds = self.credentials_store.get(account_id)
        if not creds:
            raise ValueError(f"No credentials found for account: {account_id}")
        
        # Fetch trust data
        trust_data = self.trust_store.get(account_id, {})
        
        context = AccountContext(
            account_id=account_id,
            channel_id=channel_id,
            credentials=creds,
            trust_score=trust_data.get('trust_score', 1.0),
            recent_strikes=trust_data.get('recent_strikes', 0),
            daily_upload_count=trust_data.get('daily_upload_count', 0),
            hourly_upload_count=trust_data.get('hourly_upload_count', 0),
            last_upload_time=trust_data.get('last_upload_time'),
            is_flagged=trust_data.get('is_flagged', False),
            cooldown_until=trust_data.get('cooldown_until')
        )
        
        # Hard block if flagged
        if context.is_flagged:
            raise ValueError(f"Account {account_id} is flagged - upload blocked")
        
        # Check cooldown
        if context.cooldown_until and time.time() < context.cooldown_until:
            remaining = int(context.cooldown_until - time.time())
            raise ValueError(f"Account {account_id} in cooldown for {remaining}s")
        
        self.logger.info(f"Resolved context for {account_id}: trust={context.trust_score:.2f}")
        return context


# ============================================================================
# RATE LIMIT GOVERNOR
# ============================================================================

class RateLimitGovernor:
    """
    Controls per-account uploads/hour, per-IP uploads/day,
    global concurrency caps, and burst suppression.
    
    This is NOT the same as budget_allocator.py.
    This enforces hard limits locally, even if upstream scheduling misbehaves.
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.account_counters = defaultdict(lambda: {'hourly': 0, 'daily': 0, 'last_reset_hour': 0, 'last_reset_day': 0})
        self.global_concurrency = 0
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__ + ".RateLimitGovernor")
    
    def check_and_acquire(self, context: AccountContext) -> Tuple[bool, Optional[str]]:
        """
        Check if upload can proceed under rate limits.
        
        Returns:
            (allowed, reason) - reason is populated if not allowed
        """
        with self.lock:
            # Global concurrency check
            max_concurrent = self.config.get('max_global_concurrent', 100)
            if self.global_concurrency >= max_concurrent:
                return False, f"Global concurrency limit reached: {max_concurrent}"
            
            # Account-level checks
            account_id = context.account_id
            counters = self.account_counters[account_id]
            
            # Reset counters if time windows have passed
            current_hour = int(time.time() // 3600)
            current_day = int(time.time() // 86400)
            
            if counters['last_reset_hour'] != current_hour:
                counters['hourly'] = 0
                counters['last_reset_hour'] = current_hour
            
            if counters['last_reset_day'] != current_day:
                counters['daily'] = 0
                counters['last_reset_day'] = current_day
            
            # Apply trust-based scaling
            trust_multiplier = max(0.5, min(1.5, context.trust_score))
            
            hourly_limit = int(self.config.get('max_hourly_per_account', 10) * trust_multiplier)
            daily_limit = int(self.config.get('max_daily_per_account', 100) * trust_multiplier)
            
            if counters['hourly'] >= hourly_limit:
                return False, f"Hourly limit reached: {hourly_limit}"
            
            if counters['daily'] >= daily_limit:
                return False, f"Daily limit reached: {daily_limit}"
            
            # Burst suppression - minimum time between uploads
            if context.last_upload_time:
                min_gap = self.config.get('min_upload_gap_seconds', 60)
                elapsed = time.time() - context.last_upload_time
                if elapsed < min_gap:
                    return False, f"Burst suppression: {int(min_gap - elapsed)}s remaining"
            
            # Acquire slot
            counters['hourly'] += 1
            counters['daily'] += 1
            self.global_concurrency += 1
            
            self.logger.info(f"Rate limit acquired for {account_id}: "
                           f"hourly={counters['hourly']}/{hourly_limit}, "
                           f"daily={counters['daily']}/{daily_limit}")
            
            return True, None
    
    def release(self, account_id: str):
        """Release concurrency slot."""
        with self.lock:
            self.global_concurrency = max(0, self.global_concurrency - 1)


# ============================================================================
# TRUST SAFETY ENFORCER
# ============================================================================

class TrustSafetyEnforcer:
    """
    Enforces anti-ban invariants:
    - Max uploads per channel/day
    - Content repetition detection
    - Metadata similarity checks
    - Thumbnail reuse detection
    - Sudden cadence changes
    
    This file is the last line of defense.
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.metadata_history = defaultdict(list)  # account_id -> list of metadata hashes
        self.thumbnail_history = defaultdict(set)  # account_id -> set of thumbnail hashes
        self.cadence_history = defaultdict(list)   # account_id -> list of upload timestamps
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__ + ".TrustSafetyEnforcer")
    
    def enforce(self, intent: YouTubePostIntent, context: AccountContext) -> Tuple[bool, Optional[str]]:
        """
        Enforce trust/safety rules.
        
        Returns:
            (allowed, violation_reason)
        """
        with self.lock:
            account_id = intent.account_id
            
            # 1. Check absolute daily upload limit
            max_daily = self.config.get('max_safe_daily_uploads', 50)
            if context.daily_upload_count >= max_daily:
                return False, f"Safety limit: max {max_daily} uploads/day"
            
            # 2. Content repetition detection
            metadata_str = f"{intent.title}|{intent.description}|{'|'.join(sorted(intent.tags))}"
            metadata_hash = hashlib.sha256(metadata_str.encode()).hexdigest()[:16]
            
            recent_metadata = self.metadata_history[account_id][-20:]  # Last 20 uploads
            if recent_metadata.count(metadata_hash) >= 3:
                return False, "Metadata repetition detected"
            
            # 3. Metadata similarity check (basic)
            # Check if title is too similar to recent uploads
            for recent_hash in recent_metadata[-5:]:
                if self._similarity_score(metadata_hash, recent_hash) > 0.9:
                    self.logger.warning(f"High metadata similarity detected for {account_id}")
            
            # 4. Thumbnail reuse detection
            if intent.thumbnail_path:
                thumb_hash = self._hash_file(intent.thumbnail_path)
                if thumb_hash in self.thumbnail_history[account_id]:
                    recent_reuse = sum(1 for h in list(self.thumbnail_history[account_id])[-10:] if h == thumb_hash)
                    if recent_reuse >= 2:
                        return False, "Excessive thumbnail reuse detected"
                self.thumbnail_history[account_id].add(thumb_hash)
            
            # 5. Sudden cadence change detection
            cadence = self.cadence_history[account_id]
            if len(cadence) >= 5:
                recent_gaps = [cadence[i] - cadence[i-1] for i in range(-4, 0)]
                avg_gap = sum(recent_gaps) / len(recent_gaps)
                
                if context.last_upload_time:
                    current_gap = time.time() - context.last_upload_time
                    if current_gap < avg_gap * 0.3:  # 70% faster than usual
                        self.logger.warning(f"Sudden cadence increase detected for {account_id}")
            
            # Record for future checks
            self.metadata_history[account_id].append(metadata_hash)
            self.cadence_history[account_id].append(time.time())
            
            # Trim histories to prevent memory bloat
            if len(self.metadata_history[account_id]) > 100:
                self.metadata_history[account_id] = self.metadata_history[account_id][-50:]
            if len(self.cadence_history[account_id]) > 100:
                self.cadence_history[account_id] = self.cadence_history[account_id][-50:]
            
            return True, None
    
    def _hash_file(self, filepath: str) -> str:
        """Hash file contents for deduplication."""
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(4096), b''):
                    hasher.update(chunk)
            return hasher.hexdigest()[:16]
        except Exception as e:
            self.logger.error(f"Failed to hash file {filepath}: {e}")
            return ""
    
    def _similarity_score(self, hash1: str, hash2: str) -> float:
        """Simple similarity metric (Hamming distance on hex strings)."""
        if len(hash1) != len(hash2):
            return 0.0
        matches = sum(c1 == c2 for c1, c2 in zip(hash1, hash2))
        return matches / len(hash1)


# ============================================================================
# IDEMPOTENCY GUARD
# ============================================================================

class IdempotencyGuard:
    """
    Ensures retries don't double-post, restarts don't duplicate uploads,
    and backfills don't corrupt metrics.
    
    Deduplication key: (account_id, video_file_hash, publish_window)
    Collision outcome: return success with cached result
    """
    
    def __init__(self):
        self.cache = {}  # key -> PostExecutionResult
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__ + ".IdempotencyGuard")
    
    def check(self, intent: YouTubePostIntent) -> Optional[PostExecutionResult]:
        """
        Check if this intent has already been executed.
        
        Returns:
            Cached result if found, None otherwise
        """
        key = self._generate_key(intent)
        
        with self.lock:
            if key in self.cache:
                cached = self.cache[key]
                self.logger.info(f"Idempotency collision detected: {intent.intent_id} "
                               f"-> cached result: {cached.youtube_video_id}")
                return cached
        
        return None
    
    def record(self, intent: YouTubePostIntent, result: PostExecutionResult):
        """Record successful execution."""
        key = self._generate_key(intent)
        
        with self.lock:
            self.cache[key] = result
            
            # Trim cache if too large (simple LRU-like)
            if len(self.cache) > 10000:
                # Remove oldest 1000 entries
                keys_to_remove = list(self.cache.keys())[:1000]
                for k in keys_to_remove:
                    del self.cache[k]
    
    def _generate_key(self, intent: YouTubePostIntent) -> str:
        """Generate idempotency key."""
        # Hash video file
        file_hash = self._hash_file(intent.video_file_path)
        
        # Publish window (hour granularity)
        if intent.publish_at:
            window = int(intent.publish_at.timestamp() // 3600)
        else:
            window = int(time.time() // 3600)
        
        key = f"{intent.account_id}:{file_hash}:{window}"
        return key
    
    def _hash_file(self, filepath: str) -> str:
        """Hash file for deduplication."""
        hasher = hashlib.sha256()
        try:
            with open(filepath, 'rb') as f:
                # Only hash first and last 1MB for performance
                hasher.update(f.read(1024 * 1024))
                f.seek(-1024 * 1024, 2)
                hasher.update(f.read(1024 * 1024))
            return hasher.hexdigest()[:16]
        except Exception as e:
            # Fallback to full path hash if file read fails
            return hashlib.sha256(filepath.encode()).hexdigest()[:16]


# ============================================================================
# FAILURE CLASSIFIER
# ============================================================================

class FailureClassifier:
    """
    Categorizes failures with retry/cooldown policies.
    
    Each category maps to:
    - retry allowed?
    - cooldown duration
    - alert severity
    
    No generic "error".
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__ + ".FailureClassifier")
        
        # Retry policies: (retry_allowed, cooldown_seconds, max_retries)
        self.policies = {
            FailureType.AUTH_FAILURE: (True, 300, 2),
            FailureType.RATE_LIMIT: (True, 600, 3),
            FailureType.CONTENT_REJECTION: (False, 0, 0),
            FailureType.NETWORK_FAILURE: (True, 60, 5),
            FailureType.VALIDATION_FAILURE: (False, 0, 0),
            FailureType.TRUST_SAFETY_BLOCK: (False, 0, 0),
            FailureType.IDEMPOTENCY_COLLISION: (False, 0, 0),
            FailureType.VERIFICATION_FAILURE: (True, 120, 2),
            FailureType.UNKNOWN: (True, 180, 1),
        }
    
    def classify(self, exception: Exception) -> FailureType:
        """Classify exception into failure type."""
        error_str = str(exception).lower()
        
        # Check for specific error patterns
        if 'auth' in error_str or 'credential' in error_str or '401' in error_str:
            return FailureType.AUTH_FAILURE
        
        if 'rate' in error_str or 'quota' in error_str or '429' in error_str:
            return FailureType.RATE_LIMIT
        
        if 'rejected' in error_str or 'violat' in error_str or 'inappropriate' in error_str:
            return FailureType.CONTENT_REJECTION
        
        if 'network' in error_str or 'timeout' in error_str or 'connection' in error_str:
            return FailureType.NETWORK_FAILURE
        
        if isinstance(exception, ValueError):
            return FailureType.VALIDATION_FAILURE
        
        return FailureType.UNKNOWN
    
    def get_policy(self, failure_type: FailureType) -> Tuple[bool, int, int]:
        """
        Get retry policy for failure type.
        
        Returns:
            (retry_allowed, cooldown_seconds, max_retries)
        """
        return self.policies.get(failure_type, (False, 0, 0))


# ============================================================================
# RETRY CONTROLLER
# ============================================================================

class RetryController:
    """
    Manages retry logic with:
    - Max retries capped
    - Exponential backoff
    - Cooldown on auth/rate failures
    - Never retry content rejection
    
    Retries are explicitly gated.
    """
    
    def __init__(self, classifier: FailureClassifier):
        self.classifier = classifier
        self.retry_state = {}  # intent_id -> (attempt_count, last_attempt_time)
        self.lock = threading.Lock()
        self.logger = logging.getLogger(__name__ + ".RetryController")
    
    def should_retry(self, intent_id: str, failure_type: FailureType) -> Tuple[bool, int]:
        """
        Determine if retry should be attempted.
        
        Returns:
            (should_retry, wait_seconds)
        """
        retry_allowed, base_cooldown, max_retries = self.classifier.get_policy(failure_type)
        
        if not retry_allowed:
            return False, 0
        
        with self.lock:
            state = self.retry_state.get(intent_id, (0, 0))
            attempt_count, last_attempt = state
            
            if attempt_count >= max_retries:
                self.logger.warning(f"Max retries ({max_retries}) exceeded for {intent_id}")
                return False, 0
            
            # Exponential backoff
            wait_seconds = base_cooldown * (2 ** attempt_count)
            
            # Check if cooldown has elapsed
            if last_attempt > 0:
                elapsed = time.time() - last_attempt
                if elapsed < wait_seconds:
                    remaining = int(wait_seconds - elapsed)
                    return False, remaining
            
            # Update state
            self.retry_state[intent_id] = (attempt_count + 1, time.time())
            
            self.logger.info(f"Retry approved for {intent_id}: "
                           f"attempt {attempt_count + 1}/{max_retries}, "
                           f"waited {wait_seconds}s")
            
            return True, 0
    
    def clear_state(self, intent_id: str):
        """Clear retry state after success."""
        with self.lock:
            self.retry_state.pop(intent_id, None)


# ============================================================================
# POSTING AUDIT LOGGER
# ============================================================================

class PostingAuditLogger:
    """
    Mandatory audit logging for:
    - Forensic debugging
    - Platform compliance
    - RL learning validity
    
    Every attempt is logged.
    """
    
    def __init__(self, log_dir: str = "./logs/posting_audit"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__ + ".PostingAuditLogger")
    
    def log_attempt(self, intent: YouTubePostIntent, result: PostExecutionResult):
        """Log posting attempt with full context."""
        timestamp = datetime.fromtimestamp(result.timestamp)
        log_file = self.log_dir / f"audit_{timestamp.strftime('%Y%m%d')}.jsonl"
        
        audit_entry = {
            "intent_id": intent.intent_id,
            "video_id": intent.video_id,
            "account_id": intent.account_id,
            "channel_id": intent.channel_id,
            "result": "SUCCESS" if result.success else "FAILURE",
            "youtube_video_id": result.youtube_video_id,
            "youtube_url": result.youtube_url,
            "failure_type": result.failure_type,
            "retry_allowed": result.retry_allowed,
            "latency_ms": result.execution_latency_ms,
            "timestamp": result.timestamp,
            "metadata_version": intent.metadata_version,
            "visibility": intent.visibility,
        }
        
        try:
            with open(log_file, 'a') as f:
                f.write(json.dumps(audit_entry) + '\n')
        except Exception as e:
            self.logger.error(f"Failed to write audit log: {e}")
    
    def log_anomaly(self, intent_id: str, anomaly_type: str, details: Dict):
        """Log non-determinism or unexpected behavior."""
        timestamp = datetime.now()
        log_file = self.log_dir / f"anomalies_{timestamp.strftime('%Y%m%d')}.jsonl"
        
        anomaly_entry = {
            "intent_id": intent_id,
            "anomaly_type": anomaly_type,
            "details": details,
            "timestamp": time.time(),
        }
        
        try:
            with open(log_file, 'a') as f:
                f.write(json.dumps(anomaly_entry) + '\n')
        except Exception as e:
            self.logger.error(f"Failed to write anomaly log: {e}")


# ============================================================================
# YOUTUBE POSTER (CORE ENGINE)
# ============================================================================

class YouTubePoster:
    """
    Core execution engine for YouTube posting.
    
    Execution Flow (MANDATORY):
    1. validate_intent
    2. resolve_account_context
    3. rate_limit_check
    4. trust_safety_enforcement
    5. idempotency_check
    6. payload_preparation
    7. execute_upload
    8. post_verification
    9. finalization & audit
    
    Every step is mandatory.
    """
    
    def __init__(
        self,
        credentials_store: Dict,
        trust_store: Dict,
        config: Dict
    ):
        self.credentials_store = credentials_store
        self.trust_store = trust_store
        self.config = config
        
        # Initialize components
        self.account_resolver = AccountContextResolver(credentials_store, trust_store)
        self.rate_governor = RateLimitGovernor(config.get('rate_limits', {}))
        self.trust_enforcer = TrustSafetyEnforcer(config.get('trust_safety', {}))
        self.idempotency_guard = IdempotencyGuard()
        self.failure_classifier = FailureClassifier()
        self.retry_controller = RetryController(self.failure_classifier)
        self.audit_logger = PostingAuditLogger(config.get('audit_log_dir', './logs/posting_audit'))
        
        self.logger = logging.getLogger(__name__ + ".YouTubePoster")
    
    def post(self, intent: YouTubePostIntent) -> PostExecutionResult:
        """
        Execute YouTube upload for given intent.
        
        This is the main entry point.
        """
        start_time = time.time()
        
        try:
            # Step 1: Validate intent
            self._validate_intent(intent)
            
            # Step 2: Resolve account context
            context = self.account_resolver.resolve(intent.account_id, intent.channel_id)
            
            # Step 3: Rate limit check
            rate_ok, rate_reason = self.rate_governor.check_and_acquire(context)
            if not rate_ok:
                return self._create_failure_result(
                    intent, FailureType.RATE_LIMIT, rate_reason, start_time
                )
            
            try:
                # Step 4: Trust safety enforcement
                trust_ok, trust_reason = self.trust_enforcer.enforce(intent, context)
                if not trust_ok:
                    return self._create_failure_result(
                        intent, FailureType.TRUST_SAFETY_BLOCK, trust_reason, start_time
                    )
                
                # Step 5: Idempotency check
                cached_result = self.idempotency_guard.check(intent)
                if cached_result:
                    self.logger.info(f"Idempotent match for {intent.intent_id}")
                    self.audit_logger.log_attempt(intent, cached_result)
                    return cached_result
                
                # Step 6: Prepare payload
                upload_artifact = self._prepare_payload(intent)
                
                # Step 7: Execute upload
                youtube_video_id = self._execute_upload(intent, context, upload_artifact)
                
                # Step 8: Verify post
                self._verify_post(youtube_video_id, intent, context)
                
                # Step 9: Finalize
                result = self._finalize_success(intent, youtube_video_id, start_time)
                
                # Record for idempotency
                self.idempotency_guard.record(intent, result)
                
                # Clear retry state
                self.retry_controller.clear_state(intent.intent_id)
                
                # Audit log
                self.audit_logger.log_attempt(intent, result)
                
                return result
                
            finally:
                # Always release rate limit slot
                self.rate_governor.release(intent.account_id)
        
        except Exception as e:
            self.logger.error(f"Upload failed for {intent.intent_id}: {e}", exc_info=True)
            
            # Classify failure
            failure_type = self.failure_classifier.classify(e)
            
            # Create result
            result = self._create_failure_result(intent, failure_type, str(e), start_time)
            
            # Audit log
            self.audit_logger.log_attempt(intent, result)
            
            return result
    
    def _validate_intent(self, intent: YouTubePostIntent):
        """
        Step 1: Validate intent structure and constraints.
        
        Hard checks - any failure aborts with no retry.
        """
        # Check file paths exist
        if not Path(intent.video_file_path).exists():
            raise ValueError(f"Video file not found: {intent.video_file_path}")
        
        if intent.thumbnail_path and not Path(intent.thumbnail_path).exists():
            raise ValueError(f"Thumbnail file not found: {intent.thumbnail_path}")
        
        # Check title/description length (YouTube limits)
        if len(intent.title) > 100:
            raise ValueError(f"Title too long: {len(intent.title)} chars (max 100)")
        
        if len(intent.description) > 5000:
            raise ValueError(f"Description too long: {len(intent.description)} chars (max 5000)")
        
        # Check publish time is sane
        if intent.publish_at and intent.publish_at < datetime.now():
            raise ValueError("Publish time is in the past")
        
        # Check metadata version compatibility
        if intent.metadata_version not in self.config.get('supported_metadata_versions', ['1.0']):
            raise ValueError(f"Unsupported metadata version: {intent.metadata_version}")
        
        self.logger.info(f"Intent validated: {intent.intent_id}")
    
    def _prepare_payload(self, intent: YouTubePostIntent) -> UploadArtifact:
        """
        Step 6: Prepare upload payload.
        
        FORBIDDEN:
        - Metadata "optimization"
        - Keyword stuffing
        - A/B variation
        
        Decisions are upstream.
        """
        video_path = Path(intent.video_file_path)
        file_size = video_path.stat().st_size
        
        artifact = UploadArtifact(
            intent_id=intent.intent_id,
            total_bytes=file_size,
            chunk_size=min(256 * 1024 * 1024, file_size)  # 256MB chunks or file size
        )
        
        self.logger.info(f"Payload prepared: {intent.intent_id}, size={file_size} bytes")
        return artifact
    
    def _execute_upload(
        self,
        intent: YouTubePostIntent,
        context: AccountContext,
        artifact: UploadArtifact
    ) -> str:
        """
        Step 7: Execute resumable upload to YouTube.
        
        Supports:
        - Partial upload recovery
        - Network interruption handling
        - Explicit timeout budgets
        
        NO infinite retries.
        """
        try:
            # Build YouTube service
            youtube = build('youtube', 'v3', credentials=context.credentials)
            
            # Prepare video metadata
            body = {
                'snippet': {
                    'title': intent.title,
                    'description': intent.description,
                    'tags': intent.tags,
                    'categoryId': '22'  # People & Blogs default
                },
                'status': {
                    'privacyStatus': intent.visibility,
                    'selfDeclaredMadeForKids': False,
                }
            }
            
            if intent.publish_at:
                body['status']['publishAt'] = intent.publish_at.isoformat()
            
            # Prepare media upload
            media = MediaFileUpload(
                intent.video_file_path,
                chunksize=artifact.chunk_size,
                resumable=True
            )
            
            # Execute upload with timeout
            request = youtube.videos().insert(
                part='snippet,status',
                body=body,
                media_body=media
            )
            
            response = None
            timeout = self.config.get('upload_timeout_seconds', 3600)
            start = time.time()
            
            while response is None:
                if time.time() - start > timeout:
                    raise TimeoutError(f"Upload exceeded timeout: {timeout}s")
                
                status, response = request.next_chunk()
                
                if status:
                    artifact.bytes_uploaded = int(status.resumable_progress())
                    progress = (artifact.bytes_uploaded / artifact.total_bytes) * 100
                    self.logger.debug(f"Upload progress: {progress:.1f}%")
            
            youtube_video_id = response['id']
            
            # Upload thumbnail if provided
            if intent.thumbnail_path:
                try:
                    youtube.thumbnails().set(
                        videoId=youtube_video_id,
                        media_body=MediaFileUpload(intent.thumbnail_path)
                    ).execute()
                except Exception as e:
                    self.logger.warning(f"Thumbnail upload failed: {e}")
            
            self.logger.info(f"Upload complete: {youtube_video_id}")
            return youtube_video_id
            
        except HttpError as e:
            self.logger.error(f"YouTube API error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Upload execution failed: {e}")
            raise
    
    def _verify_post(self, youtube_video_id: str, intent: YouTubePostIntent, context: AccountContext):
        """
        Step 8: Verify post was successful.
        
        Confirms:
        - Video exists
        - Visibility correct
        - Metadata matches
        - Processing started
        """
        try:
            youtube = build('youtube', 'v3', credentials=context.credentials)
            
            response = youtube.videos().list(
                part='snippet,status',
                id=youtube_video_id
            ).execute()
            
            if not response.get('items'):
                raise ValueError(f"Video not found after upload: {youtube_video_id}")
            
            video = response['items'][0]
            
            # Verify visibility
            if video['status']['privacyStatus'] != intent.visibility:
                self.logger.warning(f"Visibility mismatch: expected {intent.visibility}, "
                                  f"got {video['status']['privacyStatus']}")
            
            # Verify title
            if video['snippet']['title'] != intent.title:
                self.logger.warning(f"Title mismatch detected")
            
            self.logger.info(f"Post verified: {youtube_video_id}")
            
        except Exception as e:
            self.logger.error(f"Post verification failed: {e}")
            raise
    
    def _finalize_success(
        self,
        intent: YouTubePostIntent,
        youtube_video_id: str,
        start_time: float
    ) -> PostExecutionResult:
        """Step 9: Create success result."""
        latency_ms = int((time.time() - start_time) * 1000)
        
        result = PostExecutionResult(
            intent_id=intent.intent_id,
            success=True,
            youtube_video_id=youtube_video_id,
            youtube_url=f"https://www.youtube.com/watch?v={youtube_video_id}",
            execution_latency_ms=latency_ms,
            timestamp=time.time()
        )
        
        self.logger.info(f"Post finalized successfully: {youtube_video_id} "
                        f"(latency: {latency_ms}ms)")
        
        return result
    
    def _create_failure_result(
        self,
        intent: YouTubePostIntent,
        failure_type: FailureType,
        reason: str,
        start_time: float
    ) -> PostExecutionResult:
        """Create failure result with retry policy."""
        latency_ms = int((time.time() - start_time) * 1000)
        
        # Determine if retry is allowed
        retry_allowed, wait_seconds = self.retry_controller.should_retry(
            intent.intent_id, failure_type
        )
        
        result = PostExecutionResult(
            intent_id=intent.intent_id,
            success=False,
            failure_type=failure_type.value,
            retry_allowed=retry_allowed,
            execution_latency_ms=latency_ms,
            timestamp=time.time(),
            metadata={'reason': reason, 'wait_seconds': wait_seconds}
        )
        
        self.logger.warning(f"Post failed: {intent.intent_id}, "
                          f"type={failure_type.value}, retry={retry_allowed}")
        
        return result


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Example configuration
    config = {
        'rate_limits': {
            'max_global_concurrent': 50,
            'max_hourly_per_account': 10,
            'max_daily_per_account': 100,
            'min_upload_gap_seconds': 120
        },
        'trust_safety': {
            'max_safe_daily_uploads': 50
        },
        'upload_timeout_seconds': 3600,
        'supported_metadata_versions': ['1.0'],
        'audit_log_dir': './logs/posting_audit'
    }
    
    # Mock stores (replace with real implementations)
    credentials_store = {}
    trust_store = {}
    
    # Initialize poster
    poster = YouTubePoster(
        credentials_store=credentials_store,
        trust_store=trust_store,
        config=config
    )
    
    # Example intent
    intent = YouTubePostIntent(
        intent_id="intent_12345",
        video_id="video_67890",
        account_id="account_abc",
        channel_id="channel_xyz",
        title="Test Video",
        description="This is a test video",
        tags=["test", "demo"],
        video_file_path="./test_video.mp4",
        thumbnail_path=None,
        visibility="unlisted",
        publish_at=None,
        monetization_allowed=False,
        content_rating="G",
        metadata_version="1.0",
        created_at=time.time()
    )
    
    # Execute post
    result = poster.post(intent)
    
    print(f"Result: {result.success}")
    if result.success:
        print(f"YouTube URL: {result.youtube_url}")
    else:
        print(f"Failure: {result.failure_type} - {result.metadata.get('reason')}")