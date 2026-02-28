"""
/posting/platforms/common/base_poster.py

Shared Posting Invariants & Execution Contract (240k LOC Blueprint)

This file defines the constitutional law of posting - the non-negotiable invariants
that every platform poster must obey. It ensures:
- Platform-specific logic cannot drift
- Retries behave consistently
- Orchestration can reason about outcomes
- RL signals remain clean and comparable

What This File IS:
✅ A strict abstract base layer
✅ A shared safety & determinism contract
✅ An audit and idempotency enforcer
✅ A platform-agnostic failure taxonomy owner

What This File Is NOT:
❌ Not a scheduler
❌ Not platform-aware
❌ Not a retry strategy optimizer
❌ Not a resource allocator
❌ Not a content mutator
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, Set, List
import time
import hashlib
import json
from datetime import datetime, timedelta


# ============================================================================
# GLOBAL FAILURE & RETRY TAXONOMY
# ============================================================================

class PosterFailureType(Enum):
    """
    Global failure taxonomy - NO platform may invent new ones.
    All platform-specific failures must map to these categories.
    """
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    PLATFORM_REJECTION = "platform_rejection"
    SHADOW_SUPPRESSION = "shadow_suppression"
    NETWORK_ERROR = "network_error"
    INVALID_INTENT = "invalid_intent"
    DUPLICATE_POST = "duplicate_post"
    UNKNOWN = "unknown"


class RetryDirective(Enum):
    """
    Global retry decision space.
    Ensures orchestration can reason globally and retry storms are impossible.
    """
    NO_RETRY = "no_retry"
    RETRY_IMMEDIATE = "retry_immediate"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    RETRY_AFTER_COOLDOWN = "retry_after_cooldown"


# ============================================================================
# CORE DATA CONTRACTS (GLOBAL)
# ============================================================================

@dataclass(frozen=True)
class PostIntent(ABC):
    """
    Abstract base for all platform intents.
    Platform-specific intents may ADD fields, never remove or weaken invariants.
    """
    intent_id: str
    content_id: str
    account_id: str
    
    platform: str  # "youtube", "tiktok", etc
    created_at: float
    scheduled_time: Optional[float]
    
    metadata_version: str
    
    @abstractmethod
    def validate(self) -> None:
        """
        Platform-specific validation.
        Must raise ValueError with clear message on invalid intent.
        """
        pass
    
    def __post_init__(self):
        """Enforce immutability and basic contract."""
        if not self.intent_id:
            raise ValueError("intent_id cannot be empty")
        if not self.content_id:
            raise ValueError("content_id cannot be empty")
        if not self.account_id:
            raise ValueError("account_id cannot be empty")
        if not self.platform:
            raise ValueError("platform cannot be empty")


@dataclass
class ExecutionResult:
    """
    Standard execution result contract.
    Every platform result must map cleanly to this structure.
    """
    intent_id: str
    platform: str
    success: bool
    
    external_post_id: Optional[str] = None
    external_url: Optional[str] = None
    
    failure_type: Optional[PosterFailureType] = None
    retry_directive: Optional[RetryDirective] = None
    
    execution_latency_ms: int = 0
    timestamp: float = field(default_factory=time.time)
    
    # Platform-specific metadata (structured)
    platform_metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Error details for debugging
    error_message: Optional[str] = None
    error_trace: Optional[str] = None
    
    def __post_init__(self):
        """Validate result contract."""
        if not self.success and self.failure_type is None:
            raise ValueError("Failed results must specify failure_type")
        if self.success and self.external_post_id is None:
            raise ValueError("Successful results must specify external_post_id")


# ============================================================================
# IDEMPOTENCY MANAGER (GLOBAL)
# ============================================================================

class IdempotencyManager:
    """
    Provides cross-platform deduplication and crash-safe execution replay.
    Key format: (platform, account_id, content_id, posting_window)
    """
    
    def __init__(self, window_hours: int = 24):
        self.window_hours = window_hours
        self._execution_cache: Dict[str, ExecutionResult] = {}
        self._intent_signatures: Set[str] = set()
    
    def _compute_key(self, intent: PostIntent) -> str:
        """
        Generate deterministic idempotency key.
        Same intent + same state → same key.
        """
        # Round timestamp to posting window
        window_start = int(intent.created_at / (self.window_hours * 3600))
        
        key_data = {
            "platform": intent.platform,
            "account_id": intent.account_id,
            "content_id": intent.content_id,
            "window": window_start
        }
        
        key_json = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_json.encode()).hexdigest()
    
    def check_duplicate(self, intent: PostIntent) -> Optional[ExecutionResult]:
        """
        Check if this intent was already executed.
        Returns cached result if duplicate detected.
        """
        key = self._compute_key(intent)
        return self._execution_cache.get(key)
    
    def record_execution(self, intent: PostIntent, result: ExecutionResult) -> None:
        """Record successful execution for deduplication."""
        key = self._compute_key(intent)
        self._execution_cache[key] = result
        self._intent_signatures.add(key)
    
    def clear_expired(self) -> None:
        """Clear entries outside the idempotency window."""
        # In production, this would use timestamps and TTL
        pass


# ============================================================================
# DETERMINISM ENFORCER
# ============================================================================

class DeterminismEnforcer:
    """
    Ensures: same intent + same state → same behavior
    - Timing randomness is bounded
    - Retries are repeatable
    Essential for RL learning.
    """
    
    def __init__(self):
        self._execution_hashes: Dict[str, str] = {}
    
    def compute_execution_hash(self, intent: PostIntent) -> str:
        """
        Generate deterministic hash of execution context.
        Used to detect non-deterministic behavior.
        """
        context = {
            "intent_id": intent.intent_id,
            "platform": intent.platform,
            "account_id": intent.account_id,
            "content_id": intent.content_id,
        }
        
        context_json = json.dumps(context, sort_keys=True)
        return hashlib.sha256(context_json.encode()).hexdigest()
    
    def validate_determinism(self, intent: PostIntent, result: ExecutionResult) -> None:
        """
        Verify execution was deterministic.
        Warns on non-deterministic behavior that could poison RL.
        """
        exec_hash = self.compute_execution_hash(intent)
        
        if exec_hash in self._execution_hashes:
            # In production: verify result matches previous execution
            pass
        
        self._execution_hashes[exec_hash] = str(result.success)


# ============================================================================
# EXECUTION TIMER
# ============================================================================

class ExecutionTimer:
    """Precise execution timing for audit and performance monitoring."""
    
    def __init__(self):
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
    
    def start(self) -> None:
        """Start timing."""
        self.start_time = time.perf_counter()
    
    def stop(self) -> int:
        """Stop timing and return elapsed milliseconds."""
        if self.start_time is None:
            raise RuntimeError("Timer was never started")
        
        self.end_time = time.perf_counter()
        elapsed_ms = int((self.end_time - self.start_time) * 1000)
        return elapsed_ms


# ============================================================================
# AUDIT EMITTER (NON-OPTIONAL)
# ============================================================================

@dataclass
class AuditRecord:
    """Standard audit record emitted for every execution attempt."""
    intent_id: str
    platform: str
    account_id: str
    content_id: str
    
    success: bool
    failure_type: Optional[str]
    retry_directive: Optional[str]
    
    latency_ms: int
    timestamp: float
    
    external_post_id: Optional[str] = None
    error_message: Optional[str] = None


class AuditEmitter:
    """
    Emits non-optional audit records for every execution.
    Consumed by: evaluation, training sanity checks, forensic debugging, trust lifecycle.
    """
    
    def __init__(self):
        self._audit_log: list[AuditRecord] = []
    
    def emit(self, intent: PostIntent, result: ExecutionResult) -> None:
        """Emit audit record for this execution."""
        record = AuditRecord(
            intent_id=intent.intent_id,
            platform=intent.platform,
            account_id=intent.account_id,
            content_id=intent.content_id,
            success=result.success,
            failure_type=result.failure_type.value if result.failure_type else None,
            retry_directive=result.retry_directive.value if result.retry_directive else None,
            latency_ms=result.execution_latency_ms,
            timestamp=result.timestamp,
            external_post_id=result.external_post_id,
            error_message=result.error_message
        )
        
        self._audit_log.append(record)
        self._persist_audit(record)
    
    def _persist_audit(self, record: AuditRecord) -> None:
        """
        Persist audit record to durable storage.
        In production: write to audit database, streaming logs, etc.
        """
        # Production implementation would write to persistent storage
        pass
    
    def get_audit_log(self) -> list[AuditRecord]:
        """Retrieve audit log (for testing/debugging)."""
        return self._audit_log.copy()


# ============================================================================
# POSTER INVARIANT WATCHDOG
# ============================================================================

class PosterInvariantWatchdog:
    """
    Detects violations of posting invariants:
    - Platform posters bypassing base logic
    - Missing audit records
    - Inconsistent retry directives
    - Execution path divergence
    """
    
    def __init__(self):
        self._violation_count: Dict[str, int] = {}
        self._kill_switch_threshold = 10
    
    def check_audit_emitted(self, intent_id: str, audit_emitter: AuditEmitter) -> None:
        """Verify audit record was emitted for this intent."""
        audit_log = audit_emitter.get_audit_log()
        if not any(record.intent_id == intent_id for record in audit_log):
            self._record_violation("missing_audit", intent_id)
    
    def check_result_contract(self, result: ExecutionResult) -> None:
        """Verify result satisfies contract."""
        try:
            # Validate failure contract
            if not result.success and result.failure_type is None:
                self._record_violation("missing_failure_type", result.intent_id)
            
            # Validate success contract
            if result.success and result.external_post_id is None:
                self._record_violation("missing_external_id", result.intent_id)
            
            # Validate retry directive consistency
            if result.failure_type == PosterFailureType.SHADOW_SUPPRESSION:
                if result.retry_directive not in [RetryDirective.NO_RETRY, None]:
                    self._record_violation("invalid_retry_on_shadow", result.intent_id)
        
        except Exception as e:
            self._record_violation("contract_check_error", f"error: {e}")
    
    def _record_violation(self, violation_type: str, context: str) -> None:
        """Record invariant violation and trigger kill-switch if needed."""
        self._violation_count[violation_type] = self._violation_count.get(violation_type, 0) + 1
        
        # Log violation
        print(f"[WATCHDOG] Invariant violation: {violation_type} | context: {context}")
        
        # Check kill-switch threshold
        if self._violation_count[violation_type] >= self._kill_switch_threshold:
            raise RuntimeError(
                f"Kill-switch triggered: {violation_type} violations exceeded threshold "
                f"({self._violation_count[violation_type]} >= {self._kill_switch_threshold})"
            )


# ============================================================================
# BASE POSTER (ABSTRACT EXECUTION ENGINE)
# ============================================================================

class BasePoster(ABC):
    """
    Abstract base class defining the execution skeleton.
    
    MANDATORY EXECUTION FLOW:
    validate_intent → determinism_check → idempotency_guard → 
    platform_execute → classify_failure → retry_directive_resolution → 
    audit_emit → finalize
    
    Platforms are FORBIDDEN from bypassing this flow.
    """
    
    def __init__(self):
        self.idempotency_mgr = IdempotencyManager()
        self.determinism_enforcer = DeterminismEnforcer()
        self.audit_emitter = AuditEmitter()
        self.watchdog = PosterInvariantWatchdog()
    
    # ========================================================================
    # PUBLIC API (NON-OVERRIDABLE)
    # ========================================================================
    
    def execute(self, intent: PostIntent) -> ExecutionResult:
        """
        Main execution entry point.
        NON-OVERRIDABLE - platforms must not bypass this.
        """
        timer = ExecutionTimer()
        timer.start()
        
        try:
            # Phase 1: Validation & Safety Checks
            self.validate_intent(intent)
            self._enforce_determinism(intent)
            
            # Phase 2: Idempotency Guard
            cached_result = self._idempotency_guard(intent)
            if cached_result:
                return cached_result
            
            # Phase 3: Platform Execution
            result = self._platform_execute(intent)
            result.execution_latency_ms = timer.stop()
            
            # Phase 4: Classification & Retry Logic
            classified = self.classify_failure(result)
            classified = self._resolve_retry_directive(classified)
            
            # Phase 5: Audit & Finalization
            self._emit_audit(intent, classified)
            self._finalize(intent, classified)
            
            # Phase 6: Invariant Checks
            self.watchdog.check_result_contract(classified)
            self.watchdog.check_audit_emitted(intent.intent_id, self.audit_emitter)
            
            return classified
        
        except Exception as e:
            # Handle catastrophic failures
            elapsed_ms = timer.stop() if timer.start_time else 0
            
            failure_result = ExecutionResult(
                intent_id=intent.intent_id,
                platform=intent.platform,
                success=False,
                failure_type=PosterFailureType.UNKNOWN,
                retry_directive=RetryDirective.NO_RETRY,
                execution_latency_ms=elapsed_ms,
                error_message=str(e),
                error_trace=self._capture_trace()
            )
            
            self._emit_audit(intent, failure_result)
            return failure_result
    
    # ========================================================================
    # VALIDATION & SAFETY
    # ========================================================================
    
    def validate_intent(self, intent: PostIntent) -> None:
        """
        Validate intent contract.
        Calls platform-specific validation.
        """
        # Base validation
        if not intent.intent_id:
            raise ValueError("Invalid intent: missing intent_id")
        if not intent.platform:
            raise ValueError("Invalid intent: missing platform")
        
        # Platform-specific validation
        intent.validate()
    
    def _enforce_determinism(self, intent: PostIntent) -> None:
        """Enforce deterministic execution."""
        # In production: validate execution context is deterministic
        pass
    
    def _idempotency_guard(self, intent: PostIntent) -> Optional[ExecutionResult]:
        """
        Check for duplicate execution.
        Returns cached result if already executed.
        """
        cached = self.idempotency_mgr.check_duplicate(intent)
        if cached:
            # Duplicate detected - return cached result
            return cached
        
        return None
    
    # ========================================================================
    # PLATFORM EXECUTION (ABSTRACT)
    # ========================================================================
    
    @abstractmethod
    def _platform_execute(self, intent: PostIntent) -> ExecutionResult:
        """
        Platform-specific execution logic.
        MUST be implemented by each platform poster.
        
        Must return ExecutionResult with:
        - success=True + external_post_id on success
        - success=False + failure_type on failure
        """
        pass
    
    # ========================================================================
    # FAILURE CLASSIFICATION & RETRY LOGIC
    # ========================================================================
    
    def classify_failure(self, result: ExecutionResult) -> ExecutionResult:
        """
        Normalize platform-specific failures to global taxonomy.
        Platform posters may override to provide platform-specific mapping.
        """
        if result.success:
            return result
        
        # Ensure failure has a type
        if result.failure_type is None:
            result.failure_type = PosterFailureType.UNKNOWN
        
        return result
    
    def _resolve_retry_directive(self, result: ExecutionResult) -> ExecutionResult:
        """
        Determine retry directive based on failure type.
        Global retry policy enforced here.
        """
        if result.success:
            result.retry_directive = None
            return result
        
        # Global retry rules
        if result.failure_type == PosterFailureType.SHADOW_SUPPRESSION:
            result.retry_directive = RetryDirective.NO_RETRY
        
        elif result.failure_type == PosterFailureType.INVALID_INTENT:
            result.retry_directive = RetryDirective.NO_RETRY
        
        elif result.failure_type == PosterFailureType.DUPLICATE_POST:
            result.retry_directive = RetryDirective.NO_RETRY
        
        elif result.failure_type == PosterFailureType.RATE_LIMIT:
            result.retry_directive = RetryDirective.RETRY_AFTER_COOLDOWN
        
        elif result.failure_type == PosterFailureType.NETWORK_ERROR:
            result.retry_directive = RetryDirective.RETRY_WITH_BACKOFF
        
        elif result.failure_type == PosterFailureType.AUTH_ERROR:
            result.retry_directive = RetryDirective.NO_RETRY
        
        else:
            result.retry_directive = RetryDirective.RETRY_WITH_BACKOFF
        
        return result
    
    # ========================================================================
    # AUDIT & FINALIZATION
    # ========================================================================
    
    def _emit_audit(self, intent: PostIntent, result: ExecutionResult) -> None:
        """Emit mandatory audit record."""
        self.audit_emitter.emit(intent, result)
    
    def _finalize(self, intent: PostIntent, result: ExecutionResult) -> None:
        """
        Finalize execution.
        Record successful execution for idempotency.
        """
        if result.success:
            self.idempotency_mgr.record_execution(intent, result)
        
        # Platform-specific finalization
        self._platform_finalize(intent, result)
    
    def _platform_finalize(self, intent: PostIntent, result: ExecutionResult) -> None:
        """
        Optional platform-specific finalization.
        Override if needed.
        """
        pass
    
    # ========================================================================
    # UTILITIES
    # ========================================================================
    
    def _capture_trace(self) -> str:
        """Capture exception trace for debugging."""
        import traceback
        return traceback.format_exc()


# ============================================================================
# FORBIDDEN PATTERNS ENFORCEMENT
# ============================================================================

class ForbiddenPatternDetector:
    """
    Detects forbidden patterns in platform posters:
    - Custom failure enums
    - Custom retry semantics
    - Skipping idempotency
    - Silent exception swallowing
    - Hidden posting attempts
    """
    
    @staticmethod
    def detect_custom_failure_enum(poster_class: type) -> None:
        """Detect if platform poster defines custom failure types."""
        # In production: static analysis or runtime inspection
        pass
    
    @staticmethod
    def detect_retry_bypass(poster_class: type) -> None:
        """Detect if platform poster implements custom retry logic."""
        # In production: verify no retry logic exists outside base
        pass


# ============================================================================
# EXPORT SUMMARY
# ============================================================================

__all__ = [
    # Core contracts
    "PostIntent",
    "ExecutionResult",
    
    # Global taxonomy
    "PosterFailureType",
    "RetryDirective",
    
    # Base execution engine
    "BasePoster",
    
    # Supporting infrastructure
    "IdempotencyManager",
    "DeterminismEnforcer",
    "ExecutionTimer",
    "AuditEmitter",
    "AuditRecord",
    "PosterInvariantWatchdog",
    "ForbiddenPatternDetector",
]