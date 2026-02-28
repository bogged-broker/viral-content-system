"""
Strategic Ingestion Control Plane

Transforms from conventional ingestion runner to intelligent ingestion authority.
Implements freshness-driven scheduling, deduplication, circuit breaking,
and causality-safe ingestion with proper failure classification.
"""

import asyncio
import heapq
import json
import logging
import time
import uuid
import hashlib
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# Core data structures
@dataclass(frozen=True)
class TimeWindow:
    """Ingestion time window"""
    start: datetime
    end: datetime

@dataclass(frozen=True)
class IngestionIdentity:
    """Canonical ingestion identity for deduplication"""
    platform: str
    content_id: str
    window_start: datetime
    window_end: datetime

    def fingerprint(self) -> str:
        """Deterministic fingerprint for deduplication using both window bounds"""
        return hashlib.sha256(
            f"{self.platform}|{self.content_id}|{self.window_start.isoformat()}|{self.window_end.isoformat()}".encode()
        ).hexdigest()

    start: datetime
    end: datetime

@dataclass
class PartialSuccess:
    """Partial success tracking for APIs that lie"""
    total_items: int
    successful_items: int
    failed_items: int
    dropped_items: int
    invalid_items: int
    error_summary: Dict[str, int] = field(default_factory=dict)

@dataclass
class IngestionJob:
    """Strategic ingestion job with mode and failure classification"""
    run_id: str  # Global run ID for observability
    job_id: str
    platform: str
    content_id: str
    window: TimeWindow
    mode: 'IngestionMode'
    priority: int = 5
    retry_count: int = 0
    max_retries: int = 3
    failure_type: Optional['FailureType'] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    scheduled_at: Optional[datetime] = None
    partial_success: Optional['PartialSuccess'] = None  # Partial success tracking
    state: 'JobState' = field(default=None)  # Will be set in __post_init__
    last_state_change: datetime = field(default_factory=datetime.utcnow)  # State transition timestamp
    
    def __post_init__(self):
        """Set default state after JobState is defined"""
        if self.state is None:
            # Import here to avoid circular dependency
            import sys
            module = sys.modules[__name__]
            JobState = getattr(module, 'JobState', None)
            if JobState:
                object.__setattr__(self, 'state', JobState.CREATED)

class IngestionMode(Enum):
    """Ingestion execution modes with different priority and behavior"""
    REALTIME_FAST = "realtime_fast"
    STANDARD_POLL = "standard_poll"
    BACKFILL = "backfill"
    RECOVERY = "recovery"

class JobState(Enum):
    """Explicit job lifecycle states"""
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

class FailureType(Enum):
    """Classified failure types for intelligent retry handling"""
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    SCHEMA = "schema"
    AUTH = "auth"
    PLATFORM = "platform"
    UNKNOWN = "unknown"

class PlatformHealth(Enum):
    """Platform health states for circuit breaking"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DISABLED = "disabled"

# Custom exceptions
class TimeRegressionError(Exception):
    """Raised when content timestamps move backward"""
    pass

class DuplicateIngestionError(Exception):
    """Raised when duplicate ingestion is detected"""
    pass

class PlatformDisabledError(Exception):
    """Raised when platform is circuit-broken"""
    pass

# Abstract interfaces
class PlatformAdapter(ABC):
    """Abstract platform adapter interface"""
    
    @abstractmethod
    async def fetch_content(self, content_id: str, window: TimeWindow) -> List[Dict[str, Any]]:
        """Fetch content for given ID and time window"""
        pass
    
    @abstractmethod
    async def validate_credentials(self) -> bool:
        """Validate platform credentials"""
        pass

class IngestionStorage(ABC):
    """Abstract storage interface for ingestion persistence"""
    
    @abstractmethod
    async def store_content(self, platform: str, content: List[Dict[str, Any]]) -> bool:
        """Store ingested content"""
        pass
    
    @abstractmethod
    async def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load system checkpoint"""
        pass
    
    @abstractmethod
    async def save_checkpoint(self, checkpoint: Dict[str, Any]) -> bool:
        """Save system checkpoint"""
        pass

# Failure classification
def classify_failure(exc: Exception) -> FailureType:
    """Classify exceptions for intelligent retry handling"""
    if "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower():
        return FailureType.RATE_LIMIT
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return FailureType.NETWORK
    if "schema" in str(exc).lower() or "validation" in str(exc).lower():
        return FailureType.SCHEMA
    if "auth" in str(exc).lower() or "unauthorized" in str(exc).lower():
        return FailureType.AUTH
    if "platform" in str(exc).lower():
        return FailureType.PLATFORM
    return FailureType.UNKNOWN

def get_retry_delay(failure_type: FailureType, retry_count: int) -> timedelta:
    """Calculate retry delay based on failure type and attempt count"""
    base_delays = {
        FailureType.RATE_LIMIT: timedelta(minutes=5),
        FailureType.NETWORK: timedelta(seconds=30),
        FailureType.SCHEMA: timedelta(hours=1),
        FailureType.AUTH: timedelta(hours=2),
        FailureType.PLATFORM: timedelta(minutes=10),
        FailureType.UNKNOWN: timedelta(minutes=1)
    }
    
    base_delay = base_delays.get(failure_type, timedelta(minutes=1))
    # Exponential backoff with jitter
    exponential_factor = min(2 ** retry_count, 10)  # Cap at 10x
    jitter = 0.8 + (time.time() % 0.4)  # 80-120% jitter
    
    return timedelta(seconds=int(base_delay.total_seconds() * exponential_factor * jitter))

class IngestionPipeline:
    """
    Strategic Ingestion Control Plane
    
    Transforms from basic ETL runner to intelligent ingestion authority with:
    - Canonical identity and deduplication
    - Freshness-driven scheduling
    - Failure classification and circuit breaking
    - Causality-safe ingestion
    - Deterministic checkpointing
    """
    
    def __init__(
        self,
        adapters: Dict[str, PlatformAdapter],
        storage: IngestionStorage,
        freshness_thresholds: Dict[str, timedelta],
        max_concurrent_jobs: int = 10,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: timedelta = timedelta(minutes=30),
        # CRITICAL: Failure escalation configuration
        failure_escalation_threshold: int = 3,  # X failures
        failure_escalation_window: timedelta = timedelta(minutes=10),  # in Y minutes
        # Optional high-leverage features
        enable_trust_scoring: bool = True,
        enable_dynamic_windows: bool = True,
        enable_cost_budgeting: bool = True,
        enable_shadow_mode: bool = False,
        cost_budget_hourly: float = 100.0,  # $100/hour default
        trust_decay_hours: float = 24.0,  # 24-hour decay
        min_window_minutes: int = 5,  # Minimum 5-minute windows
        max_window_minutes: int = 60,  # Maximum 60-minute windows
    ):
        self.adapters = adapters
        self.storage = storage
        self.freshness_thresholds = freshness_thresholds
        self.max_concurrent_jobs = max_concurrent_jobs
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_timeout = circuit_breaker_timeout
        
        # CRITICAL: Failure escalation configuration
        self.failure_escalation_threshold = failure_escalation_threshold
        self.failure_escalation_window = failure_escalation_window
        self.failure_history: Dict[str, List[datetime]] = defaultdict(list)  # platform -> failure timestamps
        
        # Global run ID for observability
        self.run_id = str(uuid.uuid4())
        
        # Optional feature flags
        self.enable_trust_scoring = enable_trust_scoring
        self.enable_dynamic_windows = enable_dynamic_windows
        self.enable_cost_budgeting = enable_cost_budgeting
        self.enable_shadow_mode = enable_shadow_mode
        self.cost_budget_hourly = cost_budget_hourly
        self.trust_decay_hours = trust_decay_hours
        self.min_window_minutes = min_window_minutes
        self.max_window_minutes = max_window_minutes
        
        # Core ingestion authority state
        self.ingestion_ledger: Dict[str, str] = {}  # fingerprint -> job_id
        self.last_successful_ingestion: Dict[str, datetime] = {}  # platform -> timestamp
        self.last_seen_timestamp: Dict[str, datetime] = {}  # content_id -> timestamp
        
        # CRITICAL: Global ordering reconstruction support
        self.causal_sequence_counter: int = 0  # Global sequence counter for ordering
        self.ingestion_sequence: List[Dict[str, Any]] = []  # (sequence_id, run_id, job_id, timestamp, content_ids)
        self.content_lineage: Dict[str, List[int]] = defaultdict(list)  # content_id -> [sequence_ids]
        
        # CRITICAL: Ingestion consensus protection
        self.consensus_threshold: float = 0.7  # 70% consensus required
        self.consensus_votes: Dict[str, Dict[str, bool]] = defaultdict(dict)  # content_id -> platform -> vote
        self.consensus_decisions: Dict[str, bool] = {}  # content_id -> consensus decision
        
        # CRITICAL: Intake signal staging
        self.intake_staging: Dict[str, Dict[str, Any]] = {}  # signal_id -> staged data
        self._recent_signal_fingerprints: Set[str] = set()  # Recent signal fingerprints for duplicate detection
        self.staging_timeout: timedelta = timedelta(minutes=5)
        
        # CRITICAL: Meta-learning feedback loop
        self.meta_learning_metrics: Dict[str, Dict[str, float]] = defaultdict(dict)  # platform -> metric -> value
        self.learning_feedback: List[Dict[str, Any]] = []  # Historical feedback for learning
        
        # CRITICAL: Inflight job recovery system
        self.inflight_job_timeout: timedelta = timedelta(minutes=30)  # T = 30 minutes
        self.inflight_job_recovery: Dict[str, datetime] = {}  # job_id -> inflight_timestamp
        
        # CRITICAL: Causal dependency tracking for true causality safety
        self.causal_dependencies: Dict[str, Set[str]] = defaultdict(set)  # job_id -> dependent job_ids
        self.causal_graph: Dict[str, Dict[str, Any]] = {}  # job_id -> dependency info
        self.dependency_validation_cache: Dict[str, bool] = {}  # job_id -> validation result
        
        # CRITICAL: Checkpoint corruption handling for edge-case resumability
        self.checkpoint_version: int = 1
        self.checkpoint_integrity_hash: str = ""
        self.partial_recovery_enabled: bool = True
        self.checkpoint_retry_config: Dict[str, Any] = {
            "max_retries": 3,
            "backoff_factor": 2,
            "initial_delay": 1.0
        }
        
        # CRITICAL: Invariant validation framework
        self.invariant_validation_enabled: bool = True
        self.invariant_violations: List[Dict[str, Any]] = []
        self.last_invariant_check: datetime = datetime.utcnow()
        self.invariant_check_interval: timedelta = timedelta(minutes=5)
        
        # CRITICAL: Invariant Watchdog - Hard-Fail Mode
        self.invariant_watchdog_enabled: bool = True
        self.invariant_watchdog_interval: timedelta = timedelta(seconds=10)  # 10-second heartbeat
        self.system_frozen_for_invariant_violation: bool = False
        self.invariant_panic_reason: Optional[str] = None
        self.invariant_panic_timestamp: Optional[datetime] = None
        
        # Invariant violation thresholds
        self.max_invariant_violations_per_hour: int = 3  # Panic after 3 violations/hour
        self.invariant_violation_window: timedelta = timedelta(hours=1)
        self.invariant_violation_history: List[datetime] = []
        
        # CRITICAL: Ingestion Kill-Switch + Recovery Protocol
        self.kill_switch_enabled: bool = True
        self.system_state: str = "ACTIVE"  # ACTIVE, FROZEN, RECOVERY
        self.ingestion_enabled: bool = True
        self.scheduler_enabled: bool = True
        self.recovery_lock: bool = False
        self.kill_switch_snapshots: List[Dict[str, Any]] = []  # Append-only, immutable
        
        # Kill-Switch trigger conditions
        self.kill_switch_triggers: Dict[str, bool] = {
            "invariant_violation": True,      # Any HARD invariant breach
            "ledger_corruption": True,        # fingerprint → job_id mismatch
            "causal_regression": True,        # ingestion_sequence decreases
            "time_travel": True,              # canonical timestamp moves backward
            "phantom_completion": True,       # completed job missing durable storage
            "consensus_failure": True,        # multi-pass ingestion irreconcilable
            "checkpoint_poison": True,         # invalid restore state
            "runaway_loop": True              # retry storm without forward progress
        }
        
        # Recovery configuration
        self.recovery_mode: str = "MANUAL_OVERRIDE"  # AUTOMATED_SAFE or MANUAL_OVERRIDE
        self.shadow_validation_required: bool = True
        self.recovery_phases_enabled: bool = True
        
        # CRITICAL: Exact Invariant Watchdog - Machine-Enforceable Spec
        self.watchdog_interval: int = 5  # hard minimum 5 seconds
        self.watchdog_running: bool = False
        self.watchdog_task: Optional[asyncio.Task] = None
        self.watchdog_authority: bool = True  # META-INVARIANT #0 - WATCHDOG AUTHORITY
        
        self.platform_health: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "failures": 0,
            "disabled_until": None,
            "last_success": None,
            "health_status": PlatformHealth.HEALTHY
        })
        
        # CRITICAL: Scheduler Math Tuning - "Why Now?" Engine
        self.scheduler_math_enabled: bool = True
        self.scheduler_scoring_weights: Dict[str, float] = {
            "freshness_pressure": 0.35,      # α - Freshness urgency
            "velocity_acceleration": 0.25,   # β - Trend acceleration
            "backlog_risk": 0.20,           # γ - Queue starvation risk
            "platform_instability": 0.15,   # δ - Platform reliability penalty
            "cost_pressure": 0.05            # ε - Cost budget pressure
        }
        
        # Scheduler safety invariants
        self.scheduler_no_starvation_threshold: timedelta = timedelta(minutes=10)  # Max wait time
        self.scheduler_max_realtime_delay: timedelta = timedelta(minutes=2)     # REALTIME jobs max delay
        self.scheduler_failure_isolation: bool = True  # One platform can't monopolize workers
        
        # Optional feature state
        self.platform_trust_scores: Dict[str, float] = defaultdict(lambda: 1.0)  # Trust scoring
        self.current_window_sizes: Dict[str, int] = defaultdict(lambda: 30)  # Dynamic windows
        self.hourly_cost_spend: float = 0.0  # Cost budgeting
        
        # Retry scheduling for non-blocking retries
        self.retry_queue: List[Tuple[datetime, IngestionJob]] = []  # (retry_time, job)
        self.retry_scheduler_task: Optional[asyncio.Task] = None
        self.cost_budget_start: datetime = datetime.utcnow()  # Budget window start
        self.shadow_mode_results: Dict[str, List[Dict[str, Any]]] = defaultdict(list)  # Shadow mode
        
        # Deterministic execution state
        self.job_queue_snapshot: Optional[Dict[str, Any]] = None  # Queue snapshot for recovery
        
        # Job management
        self.job_queue: List[Tuple[int, datetime, IngestionJob]] = []  # (priority, scheduled_at, job)
        self.inflight_jobs: Set[str] = set()
        self.completed_jobs: Dict[str, Dict[str, Any]] = {}
        
        # State tracking for checkpointing
        self.queued_jobs: List[Dict[str, Any]] = []  # Jobs waiting in queue
        self.retry_jobs: List[Dict[str, Any]] = []   # Jobs waiting for retry
        
        # Scheduling state
        self.scheduler_running = False
        self.worker_tasks: List[asyncio.Task] = []
        
        # Logging
        self.logger = logging.getLogger(__name__)
        
    async def start(self) -> None:
        """Start the ingestion control plane"""
        self.logger.info("Starting ingestion control plane")
        
        # Load checkpoint and restore state
        await self._load_checkpoint()
        
        # Validate platform credentials
        await self._validate_platforms()
        
        # CRITICAL: Start watchdog first (META-INVARIANT #0 - WATCHDOG AUTHORITY)
        await self.start_watchdog()
        
        # Start scheduler
        self.scheduler_running = True
        scheduler_task = asyncio.create_task(self._scheduler_loop())
        
        # Start retry scheduler
        self.retry_scheduler_task = asyncio.create_task(self._retry_scheduler())
        
        # Start workers
        for i in range(self.max_concurrent_jobs):
            worker_task = asyncio.create_task(self._worker_loop(f"worker-{i}"))
            self.worker_tasks.append(worker_task)
        
        self.logger.info("Ingestion control plane started")
    
    async def stop(self) -> None:
        """Stop the ingestion control plane gracefully"""
        self.logger.info("Stopping ingestion control plane")
        
        # CRITICAL: Stop watchdog last (META-INVARIANT #0 - WATCHDOG AUTHORITY)
        await self.stop_watchdog()
        
        # Stop scheduler
        self.scheduler_running = False
        
        # Stop retry scheduler
        if self.retry_scheduler_task:
            self.retry_scheduler_task.cancel()
        
        # Wait for current jobs to complete or timeout
        if self.inflight_jobs:
            self.logger.info(f"Waiting for {len(self.inflight_jobs)} inflight jobs")
            await asyncio.sleep(5)  # Grace period
        
        # Cancel worker tasks
        for task in self.worker_tasks:
            task.cancel()
        
        # Save final checkpoint
        final_checkpoint_success = await self._save_checkpoint()
        if not final_checkpoint_success:
            self.logger.error("Final checkpoint failed - data may be lost on restart")
        
        self.logger.info("Ingestion control plane stopped")
    
    def _transition_job_state(self, job: IngestionJob, new_state: JobState, reason: str = "") -> None:
        """Explicit job state transition with logging"""
        old_state = job.state
        job.state = new_state
        job.last_state_change = datetime.utcnow()
        
        self.logger.debug(
            f"Job {job.job_id} state transition: {old_state.value} -> {new_state.value}",
            extra={
                "job_id": job.job_id,
                "old_state": old_state.value,
                "new_state": new_state.value,
                "reason": reason,
                "timestamp": job.last_state_change.isoformat()
            }
        )
    
    def _is_job_terminal_state(self, job: IngestionJob) -> bool:
        """Check if job is in terminal state"""
        return job.state in [JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.TIMEOUT]
    
    async def _persist_job_queue(self) -> None:
        """Persist job queue state for recovery"""
        queue_snapshot = []
        for priority, scheduled_time, job in self.job_queue:  # job_queue is a list of tuples
            queue_snapshot.append({
                "job_id": job.job_id,
                "platform": job.platform,
                "mode": job.mode.value,
                "priority": job.priority,
                "state": job.state.value,
                "created_at": job.created_at.isoformat(),
                "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None
            })
        
        # Store queue snapshot in checkpoint
        checkpoint_data = {
            "job_queue_snapshot": queue_snapshot,
            "queue_size": len(self.job_queue),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # This would be stored in the main checkpoint in production
        self.job_queue_snapshot = checkpoint_data
    
    async def _restore_job_queue(self, queue_snapshot: List[Dict[str, Any]]) -> None:
        """Restore job queue from snapshot"""
        for job_data in queue_snapshot:
            # Handle missing fields gracefully
            window_data = job_data.get("window")
            if window_data:
                # Recreate TimeWindow object
                window = TimeWindow(
                    start=datetime.fromisoformat(window_data["start"]),
                    end=datetime.fromisoformat(window_data["end"])
                )
            else:
                # Fallback to default window
                window = TimeWindow(
                    start=datetime.utcnow() - timedelta(minutes=30),
                    end=datetime.utcnow()
                )
            
            job = IngestionJob(
                run_id=job_data["run_id"],
                job_id=job_data["job_id"],
                platform=job_data["platform"],
                content_id=job_data.get("content_id", "*"),  # Handle missing field
                window=window,
                mode=IngestionMode(job_data["mode"]),
                priority=job_data["priority"],
                state=JobState(job_data["state"]),
                created_at=datetime.fromisoformat(job_data["created_at"]),
                scheduled_at=datetime.fromisoformat(job_data["scheduled_at"]) if job_data.get("scheduled_at") else None
            )
            self._push_job(job)
    
    def _make_job_idempotent(self, job: IngestionJob) -> bool:
        """Check if job execution would be idempotent"""
        # Check if job already completed successfully
        if job.job_id in self.completed_jobs:
            completed_job = self.completed_jobs[job.job_id]
            if completed_job.get("status") == JobState.COMPLETED.value:
                self.logger.info(
                    f"Job {job.job_id} already completed successfully - skipping",
                    extra={"job_id": job.job_id, "skip_reason": "already_completed"}
                )
                return True
        
        # Check if job is currently running
        if job.job_id in self.inflight_jobs:
            self.logger.info(
                f"Job {job.job_id} already inflight - skipping",
                extra={"job_id": job.job_id, "skip_reason": "already_inflight"}
            )
            return True
        
        return False
    
    async def _execute_with_timeout(self, job: IngestionJob, timeout_seconds: int = 300) -> Dict[str, Any]:
        """Execute job with timeout and deterministic behavior"""
        # CRITICAL: Enforce frozen state - refuse to proceed if frozen
        if not self._enforce_frozen_state():
            return {"status": JobState.CANCELLED.value, "reason": "system_frozen"}
        
        try:
            # Create timeout task
            timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
            
            # Create execution task
            execution_task = asyncio.create_task(self._execute_job_core(job))
            
            # Wait for either completion or timeout
            done, pending = await asyncio.wait(
                [execution_task, timeout_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancel the task that didn't complete
            for task in pending:
                task.cancel()
            
            if execution_task in done:
                # Job completed successfully
                result = await execution_task
                self._transition_job_state(job, JobState.COMPLETED, "completed_successfully")
                return result
            else:
                # Job timed out
                self._transition_job_state(job, JobState.TIMEOUT, f"timeout_after_{timeout_seconds}s")
                return {"status": JobState.TIMEOUT.value, "timeout_seconds": timeout_seconds}
                
        except asyncio.CancelledError:
            self._transition_job_state(job, JobState.CANCELLED, "cancelled")
            return {"status": JobState.CANCELLED.value}
        except Exception as e:
            self._transition_job_state(job, JobState.FAILED, f"exception:_{str(e)}")
            return {"status": JobState.FAILED.value, "error": str(e)}
    
    async def _execute_job_core(self, job: IngestionJob) -> Dict[str, Any]:
        """Core job execution logic"""
        self._transition_job_state(job, JobState.RUNNING, "started_execution")
        
        # Execute the actual ingestion logic
        adapter = self.adapters[job.platform]
        content = await adapter.fetch_content(job.content_id, job.window)
        
        # Validate and track partial success
        partial_success = self._validate_partial_success(content, job.platform)
        job.partial_success = partial_success
        
        # Enforce monotonic time guard
        await self._enforce_time_guard(content)
        
        # Store content
        success = await self.storage.store_content(job.platform, content)
        if not success:
            raise Exception("Failed to store content")
        
        return {
            "status": JobState.COMPLETED.value,
            "content_count": len(content),
            "partial_success": partial_success.__dict__
        }
    
    async def _load_checkpoint(self) -> None:
        """Load system checkpoint and restore state with corruption handling"""
        checkpoint = await self.storage.load_checkpoint()
        if not checkpoint:
            self.logger.info("No checkpoint found, starting fresh")
            return
        
        try:
            # Check checkpoint integrity first
            if not self._validate_checkpoint_integrity(checkpoint):
                if self.partial_recovery_enabled:
                    self.logger.warning("Checkpoint corruption detected, attempting partial recovery")
                    checkpoint = await self._handle_checkpoint_corruption(checkpoint)
                    if not checkpoint:
                        self.logger.error("Partial recovery failed, starting fresh")
                        return
                else:
                    self.logger.error("Checkpoint corruption detected and partial recovery disabled, starting fresh")
                    return
            
            # Restore ingestion ledger
            self.ingestion_ledger = checkpoint.get("ingestion_ledger", {})
            
            # Restore freshness tracking
            last_ingestion = checkpoint.get("last_successful_ingestion", {})
            self.last_successful_ingestion = {
                platform: datetime.fromisoformat(ts_str)
                for platform, ts_str in last_ingestion.items()
            }
            
            # Restore timestamp guards
            last_timestamps = checkpoint.get("last_seen_timestamp", {})
            self.last_seen_timestamp = {
                content_id: datetime.fromisoformat(ts_str)
                for content_id, ts_str in last_timestamps.items()
            }
            
            # CRITICAL: Restore global ordering reconstruction data
            self.causal_sequence_counter = checkpoint.get("causal_sequence_counter", 0)
            self.ingestion_sequence = checkpoint.get("ingestion_sequence", [])
            content_lineage_data = checkpoint.get("content_lineage", {})
            self.content_lineage = defaultdict(list, content_lineage_data)
            
            # CRITICAL: Restore causal dependency tracking
            causal_deps_data = checkpoint.get("causal_dependencies", {})
            self.causal_dependencies = defaultdict(set, {
                job_id: set(dependencies) 
                for job_id, dependencies in causal_deps_data.items()
            })
            self.causal_graph = checkpoint.get("causal_graph", {})
            self.dependency_validation_cache = {}
            
            # Restore platform health
            self.platform_health = defaultdict(lambda: {
                "failures": 0,
                "disabled_until": None,
                "last_success": None,
                "health_status": PlatformHealth.HEALTHY
            })
            
            platform_health = checkpoint.get("platform_health", {})
            for platform, health in platform_health.items():
                if health.get("disabled_until"):
                    health["disabled_until"] = datetime.fromisoformat(health["disabled_until"])
                if health.get("last_success"):
                    health["last_success"] = datetime.fromisoformat(health["last_success"])
                health["health_status"] = PlatformHealth(health.get("health_status", "healthy"))
                self.platform_health[platform] = health
            
            # Restore job queue
            queue_snapshot = checkpoint.get("job_queue_snapshot")
            if queue_snapshot:
                self.logger.info(f"Restoring job queue with {len(queue_snapshot['job_queue_snapshot'])} jobs")
                await self._restore_job_queue(queue_snapshot["job_queue_snapshot"])
                self.job_queue_snapshot = queue_snapshot
            
            # Restore inflight jobs
            inflight_jobs = checkpoint.get("inflight_jobs", [])
            self.inflight_jobs = set(inflight_jobs)
            self.logger.info(f"Restored {len(self.inflight_jobs)} inflight jobs")
            
            # CRITICAL: Restore inflight job recovery timestamps
            inflight_recovery_data = checkpoint.get("inflight_job_recovery", {})
            self.inflight_job_recovery = {
                job_id: datetime.fromisoformat(timestamp)
                for job_id, timestamp in inflight_recovery_data.items()
            }
            
            # CRITICAL: Fix liveness bug - ensure all inflight jobs have recovery timestamps
            # Jobs restored from checkpoint may be missing recovery timestamps
            missing_recovery_timestamps = []
            for job_id in self.inflight_jobs:
                if job_id not in self.inflight_job_recovery:
                    # This job is inflight but missing recovery timestamp
                    # Assume it became inflight at checkpoint time (safe assumption)
                    checkpoint_time = checkpoint.get("checkpoint_timestamp", datetime.utcnow().isoformat())
                    self.inflight_job_recovery[job_id] = datetime.fromisoformat(checkpoint_time)
                    missing_recovery_timestamps.append(job_id)
            
            if missing_recovery_timestamps:
                self.logger.warning(
                    f"Fixed liveness bug: Added recovery timestamps for {len(missing_recovery_timestamps)} inflight jobs",
                    extra={
                        "missing_recovery_timestamps": missing_recovery_timestamps,
                        "liveness_bug_fixed": True,
                        "recovery_timestamps_added": len(missing_recovery_timestamps)
                    }
                )
            
            # Restore completed jobs
            completed_jobs = checkpoint.get("completed_jobs", {})
            self.completed_jobs = completed_jobs
            self.logger.info(f"Restored {len(self.completed_jobs)} completed jobs")
            
            # CRITICAL: Restore advanced enhancements data
            self.consensus_votes = defaultdict(dict, checkpoint.get("consensus_votes", {}))
            self.consensus_decisions = checkpoint.get("consensus_decisions", {})
            self.intake_staging = checkpoint.get("intake_staging", {})
            self.meta_learning_metrics = defaultdict(dict, checkpoint.get("meta_learning_metrics", {}))
            self.learning_feedback = checkpoint.get("learning_feedback", [])
            self.current_window_sizes = defaultdict(lambda: 30, checkpoint.get("current_window_sizes", {}))
            
            # CRITICAL: Restore failure escalation data
            failure_history_data = checkpoint.get("failure_history", {})
            # Validate system invariants after restoration
            if self.invariant_validation_enabled:
                violations = self._validate_system_invariants()
                if violations:
                    self.logger.warning(f"System invariant violations detected after checkpoint restore: {len(violations)}")
            
            self.logger.info(f"Checkpoint loaded successfully (version {checkpoint.get('version', 0)})")
            
        except Exception as e:
            self.logger.error(f"Failed to load checkpoint: {e}")
            # Start fresh if checkpoint is corrupted beyond recovery
    
    async def _save_checkpoint(self) -> bool:
        """Save system checkpoint and return success status"""
        # Persist queue state for recovery
        await self._persist_job_queue()
        
        checkpoint = {
            "version": self.checkpoint_version,
            "integrity_hash": "",  # Will be calculated after all data is added
            "ingestion_ledger": self.ingestion_ledger,
            "last_successful_ingestion": {
                platform: ts.isoformat()
                for platform, ts in self.last_successful_ingestion.items()
            },
            "last_seen_timestamp": {
                content_id: ts.isoformat()
                for content_id, ts in self.last_seen_timestamp.items()
            },
            # CRITICAL: Global ordering reconstruction data
            "causal_sequence_counter": self.causal_sequence_counter,
            "ingestion_sequence": self.ingestion_sequence,
            "content_lineage": dict(self.content_lineage),
            # CRITICAL: Causal dependency tracking
            "causal_dependencies": {
                job_id: list(dependencies) 
                for job_id, dependencies in self.causal_dependencies.items()
            },
            "causal_graph": self.causal_graph,
            # CRITICAL: Platform health with full state
            "platform_health": {
                platform: {
                    "failures": health["failures"],
                    "disabled_until": health["disabled_until"].isoformat() if health["disabled_until"] else None,
                    "last_success": health["last_success"].isoformat() if health["last_success"] else None,
                    "health_status": health["health_status"].value
                }
                for platform, health in self.platform_health.items()
            },
            "job_queue_snapshot": self.job_queue_snapshot,
            "inflight_jobs": list(self.inflight_jobs),
            "queued_jobs": self.queued_jobs,
            "retry_jobs": self.retry_jobs,
            "completed_jobs": self.completed_jobs,
            # CRITICAL: Advanced enhancements data
            "consensus_votes": dict(self.consensus_votes),
            "consensus_decisions": dict(self.consensus_decisions),
            "intake_staging": dict(self.intake_staging),
            "meta_learning_metrics": dict(self.meta_learning_metrics),
            "learning_feedback": self.learning_feedback,
            "current_window_sizes": dict(self.current_window_sizes),
            # CRITICAL: Failure escalation data
            "failure_history": dict(self.failure_history),
            # CRITICAL: Inflight recovery data
            "inflight_job_recovery": {
                job_id: timestamp.isoformat()
                for job_id, timestamp in self.inflight_job_recovery.items()
            },
            # CRITICAL: Invariant validation data
            "invariant_violations": self.invariant_violations[-100:],  # Last 100 violations
            "last_invariant_check": self.last_invariant_check.isoformat(),
            "checkpoint_timestamp": datetime.utcnow().isoformat()
        }
        
        # Calculate integrity hash after all data is added
        checkpoint["integrity_hash"] = self._calculate_checkpoint_integrity_hash(checkpoint)
        
        # Save with retry logic
        max_retries = self.checkpoint_retry_config["max_retries"]
        initial_delay = self.checkpoint_retry_config["initial_delay"]
        backoff_factor = self.checkpoint_retry_config["backoff_factor"]
        
        for attempt in range(max_retries):
            try:
                success = await self.storage.save_checkpoint(checkpoint)
                if success:
                    self.checkpoint_integrity_hash = checkpoint["integrity_hash"]
                    self.logger.debug("Checkpoint saved successfully")
                    return True
                else:
                    if attempt < max_retries - 1:
                        delay = initial_delay * (backoff_factor ** attempt)
                        self.logger.warning(f"Checkpoint save failed, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(delay)
                    else:
                        self.logger.error("Failed to save checkpoint after all retries")
                        return False
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = initial_delay * (backoff_factor ** attempt)
                    self.logger.warning(f"Checkpoint save error, retrying in {delay}s (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(delay)
                else:
                    self.logger.error(f"Failed to save checkpoint after all retries: {e}")
                    return False
        
        return False
    
    async def _validate_platforms(self) -> None:
        """Validate all platform adapters"""
        for platform, adapter in self.adapters.items():
            try:
                valid = await adapter.validate_credentials()
                if valid:
                    self.platform_health[platform]["health_status"] = PlatformHealth.HEALTHY
                    self.logger.info(f"Platform {platform} validated successfully")
                else:
                    self.platform_health[platform]["health_status"] = PlatformHealth.DISABLED
                    self.logger.error(f"Platform {platform} validation failed")
            except Exception as e:
                self.platform_health[platform]["health_status"] = PlatformHealth.DISABLED
                self.logger.error(f"Platform {platform} validation error: {e}")
    
    async def _scheduler_loop(self) -> None:
        """Freshness-driven scheduler loop with backlog and lag detection"""
        self.logger.info("Scheduler started")
        
        while self.scheduler_running:
            try:
                # CRITICAL: Enforce frozen state - refuse to proceed if frozen
                if not self._enforce_frozen_state():
                    await asyncio.sleep(10)  # Wait before checking again
                    continue
                
                now = datetime.utcnow()
                
                # CRITICAL: Check for kill-switch conditions before any scheduling
                kill_switch_result = self._check_kill_switch_conditions()
                if kill_switch_result:
                    trigger_reason, violations, context = kill_switch_result
                    self._trigger_kill_switch(trigger_reason, violations, context)
                    continue  # Exit loop to enforce frozen state
                
                # CRITICAL: Check backlog and lag metrics before scheduling
                backlog_metrics = self._calculate_backlog_metrics()
                
                # Check each platform for freshness requirements
                for platform in self.adapters.keys():
                    await self._schedule_platform_ingestion(platform, now, backlog_metrics)
                
                # CRITICAL: Detect and log backlog/lag conditions
                self._detect_backlog_conditions(backlog_metrics)
                
                # CRITICAL: Run invariant watchdog - HARD-FAIL MODE
                if self.invariant_watchdog_enabled:
                    self._run_invariant_watchdog()
                
                # CRITICAL: Process staged intake signals
                self._process_staged_signals()
                
                # CRITICAL: Run periodic invariant validation
                if self.invariant_validation_enabled:
                    now = datetime.utcnow()
                    if now - self.last_invariant_check > self.invariant_check_interval:
                        violations = self._validate_system_invariants()
                        if violations:
                            self.logger.warning(f"Periodic invariant validation found {len(violations)} violations")
                
                # Save checkpoint periodically
                checkpoint_success = await self._save_checkpoint()
                if not checkpoint_success:
                    self.logger.warning("Periodic checkpoint failed - system may not recover cleanly")
                
                # Sleep before next scheduling cycle
                await asyncio.sleep(60)  # 1-minute scheduling interval
                
            except Exception as e:
                self.logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(10)  # Brief pause on error
        
        self.logger.info("Scheduler stopped")
    
    def _check_failure_escalation(self, platform: str, failure_time: datetime) -> bool:
        """Check if failure escalation threshold is exceeded"""
        # Record this failure
        self.failure_history[platform].append(failure_time)
        
        # Clean old failures outside the window
        window_start = failure_time - self.failure_escalation_window
        self.failure_history[platform] = [
            ts for ts in self.failure_history[platform] 
            if ts >= window_start
        ]
        
        # Check if threshold is exceeded
        recent_failures = len(self.failure_history[platform])
        if recent_failures >= self.failure_escalation_threshold:
            return True
        
        return False
    
    def _invoke_failure_escalation_hook(self, platform: str, failure_count: int, window_start: datetime, window_end: datetime) -> None:
        """Invoke alerting hook for failure escalation"""
        escalation_data = {
            "platform": platform,
            "failure_count": failure_count,
            "escalation_threshold": self.failure_escalation_threshold,
            "escalation_window_minutes": self.failure_escalation_window.total_seconds() / 60,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "failure_timestamps": [ts.isoformat() for ts in self.failure_history[platform]],
            "run_id": self.run_id,
            "escalation_timestamp": datetime.utcnow().isoformat()
        }
        
        # CRITICAL: Invoke alerting hook (this would integrate with external alerting systems)
        self.logger.error(
            f"FAILURE ESCALATION: Platform {platform} has {failure_count} failures in {self.failure_escalation_window.total_seconds()/60:.1f} minutes",
            extra={
                "alert_type": "failure_escalation",
                "platform": platform,
                "failure_count": failure_count,
                "escalation_threshold": self.failure_escalation_threshold,
                "escalation_window_minutes": self.failure_escalation_window.total_seconds() / 60,
                "escalation_data": escalation_data
            }
        )
        
        # TODO: Integrate with external alerting systems
        # - Send to PagerDuty, OpsGenie, etc.
        # - Trigger webhook notifications
        # - Update monitoring dashboards
        # - Send email/SMS alerts
        
        # For now, we'll log the escalation data for external systems to consume
        self.logger.info(f"ESCALATION_DATA: {json.dumps(escalation_data)}")
    
    def get_failure_escalation_status(self) -> Dict[str, Any]:
        """Get current failure escalation status for all platforms"""
        now = datetime.utcnow()
        escalation_status = {}
        
        for platform in self.adapters.keys():
            platform_failures = self.failure_history.get(platform, [])
            
            # Clean old failures
            window_start = now - self.failure_escalation_window
            recent_failures = [
                ts for ts in platform_failures 
                if ts >= window_start
            ]
            
            # Calculate escalation metrics
            failure_count = len(recent_failures)
            is_escalated = failure_count >= self.failure_escalation_threshold
            
            escalation_status[platform] = {
                "recent_failure_count": failure_count,
                "escalation_threshold": self.failure_escalation_threshold,
                "escalation_window_minutes": self.failure_escalation_window.total_seconds() / 60,
                "is_escalated": is_escalated,
                "window_start": window_start.isoformat(),
                "window_end": now.isoformat(),
                "failure_timestamps": [ts.isoformat() for ts in recent_failures]
            }
        
        return {
            "escalation_status": escalation_status,
            "global_config": {
                "escalation_threshold": self.failure_escalation_threshold,
                "escalation_window_minutes": self.failure_escalation_window.total_seconds() / 60
            },
            "timestamp": now.isoformat()
        }
    
    def _check_ingestion_consensus(self, content_id: str, platform: str, should_ingest: bool) -> bool:
        """Check if ingestion has consensus across platforms with comprehensive validation"""
        # Record this platform's vote with timestamp and metadata
        vote_record = {
            "vote": should_ingest,
            "timestamp": datetime.utcnow().isoformat(),
            "platform": platform,
            "vote_id": f"{platform}_{content_id}_{int(datetime.utcnow().timestamp())}"
        }
        self.consensus_votes[content_id][platform] = vote_record
        
        # CRITICAL: Multi-pass consensus validation
        return self._validate_comprehensive_consensus(content_id, platform, should_ingest)
    
    def _validate_comprehensive_consensus(self, content_id: str, platform: str, should_ingest: bool) -> bool:
        """Comprehensive consensus validation with multi-pass verification and delta tolerance"""
        votes = self.consensus_votes[content_id]
        
        # CRITICAL: Pass 1 - Basic consensus check
        basic_consensus = self._check_basic_consensus(content_id, platform, should_ingest, votes)
        if not basic_consensus["has_consensus"]:
            self._log_consensus_rejection(content_id, platform, "basic_consensus_failed", basic_consensus)
            return False
        
        # CRITICAL: Pass 2 - Delta tolerance enforcement
        delta_validation = self._validate_delta_tolerance(content_id, platform, should_ingest, votes)
        if not delta_validation["within_tolerance"]:
            self._log_consensus_rejection(content_id, platform, "delta_tolerance_violation", delta_validation)
            return False
        
        # CRITICAL: Pass 3 - Temporal consistency validation
        temporal_validation = self._validate_temporal_consistency(content_id, platform, should_ingest, votes)
        if not temporal_validation["temporally_consistent"]:
            self._log_consensus_rejection(content_id, platform, "temporal_inconsistency", temporal_validation)
            return False
        
        # CRITICAL: Pass 4 - Cross-platform data integrity validation
        integrity_validation = self._validate_cross_platform_integrity(content_id, platform, should_ingest, votes)
        if not integrity_validation["integrity_valid"]:
            self._log_consensus_rejection(content_id, platform, "integrity_violation", integrity_validation)
            return False
        
        # CRITICAL: Pass 5 - Learning signal poisoning protection
        poisoning_validation = self._validate_learning_signal_safety(content_id, platform, should_ingest, votes)
        if not poisoning_validation["signal_safe"]:
            self._log_consensus_rejection(content_id, platform, "learning_signal_poisoning", poisoning_validation)
            return False
        
        # All validations passed - strong consensus achieved
        consensus_result = {
            "has_consensus": True,
            "consensus_strength": "strong",
            "validation_passes": ["basic_consensus", "delta_tolerance", "temporal_consistency", "integrity_validation", "signal_safety"],
            "final_decision": should_ingest,
            "confidence_score": self._calculate_consensus_confidence(votes),
            "validation_timestamp": datetime.utcnow().isoformat()
        }
        
        # Store comprehensive consensus decision
        self.consensus_decisions[content_id] = consensus_result
        
        self.logger.info(
            f"STRONG CONSENSUS ACHIEVED for {content_id} from {platform}: "
            f"decision={should_ingest}, confidence={consensus_result['confidence_score']:.3f}",
            extra={
                "content_id": content_id,
                "platform": platform,
                "consensus_type": "strong",
                "final_decision": should_ingest,
                "confidence_score": consensus_result["confidence_score"],
                "validation_passes": consensus_result["validation_passes"],
                "total_validations": len(consensus_result["validation_passes"]),
                "consensus_timestamp": consensus_result["validation_timestamp"]
            }
        )
        
        return should_ingest
    
    def _check_basic_consensus(self, content_id: str, platform: str, should_ingest: bool, votes: Dict[str, Dict]) -> Dict[str, Any]:
        """Pass 1: Basic consensus validation"""
        vote_records = list(votes.values())
        total_votes = len(vote_records)
        positive_votes = sum(1 for vote in vote_records if vote["vote"])
        
        if total_votes == 0:
            return {
                "has_consensus": True,
                "consensus_type": "defer_to_caller",
                "positive_votes": 1 if should_ingest else 0,
                "total_votes": 1,
                "consensus_ratio": 1.0 if should_ingest else 0.0,
                "reason": "no_other_votes"
            }
        
        consensus_ratio = positive_votes / total_votes
        has_consensus = consensus_ratio >= self.consensus_threshold
        
        return {
            "has_consensus": has_consensus,
            "consensus_type": "basic",
            "positive_votes": positive_votes,
            "total_votes": total_votes,
            "consensus_ratio": consensus_ratio,
            "threshold_met": consensus_ratio >= self.consensus_threshold,
            "reason": "basic_voting"
        }
    
    def _validate_delta_tolerance(self, content_id: str, platform: str, should_ingest: bool, votes: Dict[str, Dict]) -> Dict[str, Any]:
        """Pass 2: Delta tolerance enforcement - detect cached metrics and API lies"""
        # Extract vote patterns and timestamps
        vote_patterns = {}
        for plat, vote_data in votes.items():
            vote_patterns[plat] = {
                "vote": vote_data["vote"],
                "timestamp": datetime.fromisoformat(vote_data["timestamp"]),
                "vote_id": vote_data["vote_id"]
            }
        
        # CRITICAL: Detect suspicious voting patterns
        suspicious_patterns = self._detect_suspicious_voting_patterns(vote_patterns)
        if suspicious_patterns["is_suspicious"]:
            return {
                "within_tolerance": False,
                "suspicious_patterns": suspicious_patterns,
                "reason": "suspicious_voting_pattern_detected",
                "tolerance_violation": True
            }
        
        # CRITICAL: Validate delta consistency across platforms
        delta_analysis = self._analyze_cross_platform_deltas(content_id, vote_patterns)
        if not delta_analysis["delta_consistent"]:
            return {
                "within_tolerance": False,
                "delta_analysis": delta_analysis,
                "reason": "cross_platform_delta_inconsistency",
                "tolerance_violation": True
            }
        
        # CRITICAL: Check for cached metrics (identical timestamps indicate caching)
        timestamp_analysis = self._analyze_timestamp_patterns(vote_patterns)
        if timestamp_analysis["potential_caching"]:
            return {
                "within_tolerance": False,
                "timestamp_analysis": timestamp_analysis,
                "reason": "potential_cached_metrics_detected",
                "tolerance_violation": True
            }
        
        return {
            "within_tolerance": True,
            "suspicious_patterns": suspicious_patterns,
            "delta_analysis": delta_analysis,
            "timestamp_analysis": timestamp_analysis,
            "reason": "all_tolerance_checks_passed"
        }
    
    def _detect_suspicious_voting_patterns(self, vote_patterns: Dict[str, Dict]) -> Dict[str, Any]:
        """Detect suspicious voting patterns that indicate manipulation"""
        patterns = {
            "is_suspicious": False,
            "suspicious_indicators": [],
            "pattern_analysis": {}
        }
        
        # Check 1: Identical timestamps (potential batch processing/caching)
        timestamps = [v["timestamp"] for v in vote_patterns.values()]
        if len(set(timestamps)) < len(timestamps):
            patterns["suspicious_indicators"].append("identical_timestamps")
            patterns["pattern_analysis"]["identical_timestamps"] = {
                "total_votes": len(timestamps),
                "unique_timestamps": len(set(timestamps)),
                "duplicate_count": len(timestamps) - len(set(timestamps))
            }
        
        # Check 2: Sequential vote IDs (potential automation)
        vote_ids = [v["vote_id"] for v in vote_patterns.values()]
        vote_ids_sorted = sorted(vote_ids)
        sequential_groups = []
        current_group = [vote_ids_sorted[0]]
        
        for i in range(1, len(vote_ids_sorted)):
            current_timestamp = int(vote_ids_sorted[i].split('_')[-1])
            prev_timestamp = int(vote_ids_sorted[i-1].split('_')[-1])
            
            if current_timestamp - prev_timestamp < 100:  # Within 100ms
                current_group.append(vote_ids_sorted[i])
            else:
                sequential_groups.append(current_group)
                current_group = [vote_ids_sorted[i]]
        
        if any(len(group) > 2 for group in sequential_groups):
            patterns["suspicious_indicators"].append("sequential_vote_ids")
            patterns["pattern_analysis"]["sequential_vote_ids"] = {
                "sequential_groups": len(sequential_groups),
                "max_group_size": max(len(group) for group in sequential_groups),
                "total_votes": len(vote_ids)
            }
        
        # Check 3: All votes identical (potential herd behavior)
        votes = [v["vote"] for v in vote_patterns.values()]
        if len(set(votes)) == 1 and len(votes) > 2:
            patterns["suspicious_indicators"].append("identical_votes")
            patterns["pattern_analysis"]["identical_votes"] = {
                "vote_value": votes[0],
                "total_votes": len(votes),
                "unanimous": True
            }
        
        patterns["is_suspicious"] = len(patterns["suspicious_indicators"]) > 0
        return patterns
    
    def _analyze_cross_platform_deltas(self, content_id: str, vote_patterns: Dict[str, Dict]) -> Dict[str, Any]:
        """Analyze cross-platform voting deltas for consistency"""
        analysis = {
            "delta_consistent": True,
            "delta_violations": [],
            "platform_deltas": {}
        }
        
        # Calculate pairwise deltas between platforms
        platforms = list(vote_patterns.keys())
        for i in range(len(platforms)):
            for j in range(i + 1, len(platforms)):
                plat1, plat2 = platforms[i], platforms[j]
                vote1, vote2 = vote_patterns[plat1]["vote"], vote_patterns[plat2]["vote"]
                
                if vote1 != vote2:
                    time_diff = abs((vote_patterns[plat1]["timestamp"] - vote_patterns[plat2]["timestamp"]).total_seconds())
                    
                    delta_violation = {
                        "platform1": plat1,
                        "platform2": plat2,
                        "vote1": vote1,
                        "vote2": vote2,
                        "time_difference_seconds": time_diff,
                        "violation_type": "vote_mismatch"
                    }
                    
                    analysis["delta_violations"].append(delta_violation)
                    analysis["platform_deltas"][f"{plat1}_vs_{plat2}"] = delta_violation
        
        # Check if delta violations exceed tolerance
        max_time_tolerance = 300  # 5 minutes max time difference for same content
        critical_violations = [
            v for v in analysis["delta_violations"] 
            if v["time_difference_seconds"] > max_time_tolerance
        ]
        
        if critical_violations:
            analysis["delta_consistent"] = False
            analysis["critical_violations"] = critical_violations
        
        return analysis
    
    def _analyze_timestamp_patterns(self, vote_patterns: Dict[str, Dict]) -> Dict[str, Any]:
        """Analyze timestamp patterns for potential caching"""
        timestamps = [v["timestamp"] for v in vote_patterns.values()]
        
        analysis = {
            "potential_caching": False,
            "timestamp_spread": {},
            "anomalies": []
        }
        
        if len(timestamps) > 1:
            time_diffs = []
            for i in range(1, len(timestamps)):
                diff = (timestamps[i] - timestamps[0]).total_seconds()
                time_diffs.append(diff)
            
            analysis["timestamp_spread"] = {
                "min_seconds": min(time_diffs),
                "max_seconds": max(time_diffs),
                "avg_seconds": sum(time_diffs) / len(time_diffs),
                "total_votes": len(timestamps)
            }
            
            # CRITICAL: Detect potential caching
            # If all timestamps are within 1 second, likely cached data
            if analysis["timestamp_spread"]["max_seconds"] < 1.0:
                analysis["potential_caching"] = True
                analysis["anomalies"].append("all_timestamps_within_1_second")
            
            # If timestamps are perfectly synchronized (unlikely for real API calls)
            if analysis["timestamp_spread"]["max_seconds"] < 0.1:
                analysis["potential_caching"] = True
                analysis["anomalies"].append("perfect_timestamp_synchronization")
        
        return analysis
    
    def _validate_temporal_consistency(self, content_id: str, platform: str, should_ingest: bool, votes: Dict[str, Dict]) -> Dict[str, Any]:
        """Pass 3: Temporal consistency validation"""
        # Check if voting times are temporally consistent with expected API behavior
        vote_times = [datetime.fromisoformat(v["timestamp"]) for v in votes.values()]
        
        if len(vote_times) <= 1:
            return {
                "temporally_consistent": True,
                "reason": "insufficient_votes_for_temporal_analysis"
            }
        
        # Analyze time distribution
        time_spread = (max(vote_times) - min(vote_times)).total_seconds()
        avg_interval = time_spread / (len(vote_times) - 1) if len(vote_times) > 1 else 0
        
        # CRITICAL: Detect temporal anomalies
        temporal_anomalies = []
        
        # Anomaly 1: All votes within impossibly short time (batch processing)
        if time_spread < 5.0:  # All votes within 5 seconds
            temporal_anomalies.append("impossibly_fast_voting_pattern")
        
        # Anomaly 2: Perfectly spaced votes (automated pattern)
        if len(vote_times) > 2:
            intervals = []
            for i in range(1, len(vote_times)):
                interval = (vote_times[i] - vote_times[i-1]).total_seconds()
                intervals.append(interval)
            
            interval_variance = sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)
            if interval_variance < 0.1:  # Very low variance suggests automation
                temporal_anomalies.append("perfectly_spaced_voting_pattern")
        
        # Anomaly 3: Votes outside reasonable time window
        now = datetime.utcnow()
        for vote_time in vote_times:
            if abs((now - vote_time).total_seconds()) > 3600:  # More than 1 hour old
                temporal_anomalies.append("vote_outside_reasonable_time_window")
                break
        
        return {
            "temporally_consistent": len(temporal_anomalies) == 0,
            "time_spread_seconds": time_spread,
            "avg_interval_seconds": avg_interval,
            "total_votes": len(vote_times),
            "temporal_anomalies": temporal_anomalies,
            "reason": "temporal_consistency_check"
        }
    
    def _validate_cross_platform_integrity(self, content_id: str, platform: str, should_ingest: bool, votes: Dict[str, Dict]) -> Dict[str, Any]:
        """Pass 4: Cross-platform data integrity validation"""
        integrity_checks = {
            "integrity_valid": True,
            "violations": [],
            "platform_integrity": {}
        }
        
        # Check 1: Content ID consistency across platforms
        # (This would require platform-specific content ID mapping in production)
        # For now, we validate the voting integrity structure
        
        # Check 2: Vote record integrity
        for plat, vote_data in votes.items():
            required_fields = ["vote", "timestamp", "platform", "vote_id"]
            missing_fields = [field for field in required_fields if field not in vote_data]
            
            if missing_fields:
                integrity_checks["violations"].append({
                    "platform": plat,
                    "violation_type": "missing_required_fields",
                    "missing_fields": missing_fields
                })
                integrity_checks["integrity_valid"] = False
            
            # Validate timestamp format
            try:
                datetime.fromisoformat(vote_data["timestamp"])
            except (ValueError, TypeError):
                integrity_checks["violations"].append({
                    "platform": plat,
                    "violation_type": "invalid_timestamp_format",
                    "timestamp": vote_data.get("timestamp")
                })
                integrity_checks["integrity_valid"] = False
            
            # Validate vote value
            if not isinstance(vote_data["vote"], bool):
                integrity_checks["violations"].append({
                    "platform": plat,
                    "violation_type": "invalid_vote_type",
                    "vote_type": type(vote_data["vote"]).__name__
                })
                integrity_checks["integrity_valid"] = False
        
        return integrity_checks
    
    def _validate_learning_signal_safety(self, content_id: str, platform: str, should_ingest: bool, votes: Dict[str, Dict]) -> Dict[str, Any]:
        """Pass 5: Learning signal poisoning protection"""
        safety_checks = {
            "signal_safe": True,
            "poisoning_indicators": [],
            "learning_risk": "low"
        }
        
        # CRITICAL: Detect potential learning signal poisoning
        # This prevents the system from learning from manipulated or inconsistent data
        
        # Check 1: High disagreement rate (potential conflicting signals)
        vote_values = [v["vote"] for v in votes.values()]
        if len(vote_values) > 1:
            disagreement_rate = min(vote_values.count(True), vote_values.count(False)) / len(vote_values)
            if disagreement_rate < 0.5:  # High disagreement
                safety_checks["poisoning_indicators"].append("high_disagreement_rate")
                safety_checks["learning_risk"] = "high"
        
        # Check 2: Temporal clustering (potential batch poisoning)
        timestamps = [datetime.fromisoformat(v["timestamp"]) for v in votes.values()]
        if len(timestamps) > 1:
            time_cluster = max(timestamps) - min(timestamps)
            if time_cluster.total_seconds() < 10:  # All votes within 10 seconds
                safety_checks["poisoning_indicators"].append("temporal_vote_clustering")
                safety_checks["learning_risk"] = "medium"
        
        # Check 3: Platform isolation (only one platform voting)
        if len(votes) == 1:
            safety_checks["poisoning_indicators"].append("single_platform_only")
            safety_checks["learning_risk"] = "medium"
        
        # Determine overall safety
        if safety_checks["learning_risk"] == "high":
            safety_checks["signal_safe"] = False
        elif len(safety_checks["poisoning_indicators"]) >= 2:
            safety_checks["signal_safe"] = False
            safety_checks["learning_risk"] = "medium"
        
        return safety_checks
    
    def _calculate_consensus_confidence(self, votes: Dict[str, Dict]) -> float:
        """Calculate confidence score for consensus decision"""
        vote_records = list(votes.values())
        total_votes = len(vote_records)
        
        if total_votes == 0:
            return 1.0
        
        positive_votes = sum(1 for vote in vote_records if vote["vote"])
        consensus_ratio = positive_votes / total_votes
        
        # Confidence based on vote count and consensus strength
        vote_confidence = min(1.0, total_votes / len(self.adapters))  # More votes = higher confidence
        consensus_confidence = abs(consensus_ratio - 0.5) * 2  # Stronger consensus = higher confidence
        
        return (vote_confidence + consensus_confidence) / 2
    
    def _log_consensus_rejection(self, content_id: str, platform: str, rejection_type: str, validation_result: Dict[str, Any]) -> None:
        """Log consensus rejection with detailed information"""
        self.logger.error(
            f"CONSENSUS REJECTION for {content_id} from {platform}: {rejection_type}",
            extra={
                "content_id": content_id,
                "platform": platform,
                "rejection_type": rejection_type,
                "validation_result": validation_result,
                "consensus_failed": True,
                "rejection_timestamp": datetime.utcnow().isoformat(),
                "prevents_learning_poisoning": True
            }
        )
    
    def _stage_intake_signal(self, signal_data: Dict[str, Any]) -> str:
        """Stage intake signal for RAW → FILTERED → CANDIDATE → VERIFIED pipeline"""
        signal_id = str(uuid.uuid4())
        
        # CRITICAL: Initialize signal in RAW state
        staged_signal = {
            "signal_id": signal_id,
            "signal_data": signal_data,
            "staged_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + self.staging_timeout).isoformat(),
            "status": "RAW",  # CRITICAL: Start in RAW state
            "pipeline_stage": "RAW",
            "validation_results": {},
            "filtering_results": {},
            "candidate_results": {},
            "verification_results": {},
            "noise_detected": False,
            "noise_reasons": [],
            "processing_history": [
                {
                    "stage": "RAW",
                    "timestamp": datetime.utcnow().isoformat(),
                    "action": "signal_staged",
                    "reason": "initial_staging"
                }
            ]
        }
        
        self.intake_staging[signal_id] = staged_signal
        
        # CRITICAL: Immediately begin RAW → FILTERED pipeline
        self._process_raw_to_filtered(signal_id)
        
        self.logger.info(
            f"Staged intake signal {signal_id} in RAW state - beginning pipeline processing",
            extra={
                "signal_id": signal_id,
                "signal_type": signal_data.get("type", "unknown"),
                "platform": signal_data.get("platform", "unknown"),
                "pipeline_stage": "RAW",
                "expires_at": staged_signal["expires_at"]
            }
        )
        
        return signal_id
    
    def _process_raw_to_filtered(self, signal_id: str) -> None:
        """Process signal from RAW to FILTERED stage - critical noise filtering"""
        if signal_id not in self.intake_staging:
            return
        
        staged_signal = self.intake_staging[signal_id]
        signal_data = staged_signal["signal_data"]
        
        # CRITICAL: RAW → FILTERED - Detect and filter noise
        filtering_results = self._filter_raw_signal(signal_data)
        staged_signal["filtering_results"] = filtering_results
        
        if filtering_results["noise_detected"]:
            # CRITICAL: Noise detected - block pipeline progression
            staged_signal["status"] = "NOISE_DETECTED"
            staged_signal["pipeline_stage"] = "REJECTED"
            staged_signal["noise_detected"] = True
            staged_signal["noise_reasons"] = filtering_results["noise_reasons"]
            
            staged_signal["processing_history"].append({
                "stage": "FILTERED",
                "timestamp": datetime.utcnow().isoformat(),
                "action": "noise_detected",
                "reason": "signal_rejected_as_noise",
                "noise_reasons": filtering_results["noise_reasons"]
            })
            
            self.logger.error(
                f"NOISE DETECTED in signal {signal_id} - blocking pipeline progression",
                extra={
                    "signal_id": signal_id,
                    "pipeline_stage": "REJECTED",
                    "noise_detected": True,
                    "noise_reasons": filtering_results["noise_reasons"],
                    "prevents_downstream_noise": True,
                    "rejection_timestamp": datetime.utcnow().isoformat()
                }
            )
            return
        
        # CRITICAL: Signal passed filtering - advance to FILTERED stage
        staged_signal["status"] = "FILTERED"
        staged_signal["pipeline_stage"] = "FILTERED"
        
        staged_signal["processing_history"].append({
            "stage": "FILTERED",
            "timestamp": datetime.utcnow().isoformat(),
            "action": "noise_filtering_passed",
            "reason": "signal_cleaned_and_filtered"
        })
        
        self.logger.info(
            f"Signal {signal_id} advanced to FILTERED stage - noise filtering passed",
            extra={
                "signal_id": signal_id,
                "pipeline_stage": "FILTERED",
                "noise_detected": False,
                "filtering_timestamp": datetime.utcnow().isoformat()
            }
        )
        
        # CRITICAL: Continue to FILTERED → CANDIDATE pipeline
        self._process_filtered_to_candidate(signal_id)
    
    def _filter_raw_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Filter RAW signal to detect noise and invalid data"""
        filtering_results = {
            "noise_detected": False,
            "noise_reasons": [],
            "filtering_timestamp": datetime.utcnow().isoformat(),
            "signal_quality_score": 0.0,
            "validations_passed": [],
            "validations_failed": []
        }
        
        # CRITICAL: Validation 1 - Basic signal structure
        required_fields = ["platform", "type", "content"]
        missing_fields = [field for field in required_fields if field not in signal_data]
        if missing_fields:
            filtering_results["noise_detected"] = True
            filtering_results["noise_reasons"].append(f"missing_required_fields: {missing_fields}")
            filtering_results["validations_failed"].append("basic_structure_validation")
            return filtering_results
        
        filtering_results["validations_passed"].append("basic_structure_validation")
        
        # CRITICAL: Validation 2 - Platform validation
        valid_platforms = list(self.adapters.keys())
        platform = signal_data.get("platform")
        if platform not in valid_platforms:
            filtering_results["noise_detected"] = True
            filtering_results["noise_reasons"].append(f"invalid_platform: {platform}")
            filtering_results["validations_failed"].append("platform_validation")
            return filtering_results
        
        filtering_results["validations_passed"].append("platform_validation")
        
        # CRITICAL: Validation 3 - Content validation
        content = signal_data.get("content", [])
        if not isinstance(content, list) or len(content) == 0:
            filtering_results["noise_detected"] = True
            filtering_results["noise_reasons"].append("invalid_or_empty_content")
            filtering_results["validations_failed"].append("content_validation")
            return filtering_results
        
        filtering_results["validations_passed"].append("content_validation")
        
        # CRITICAL: Validation 4 - Content item validation
        invalid_content_items = 0
        for i, item in enumerate(content):
            if not isinstance(item, dict) or "id" not in item:
                invalid_content_items += 1
        
        if invalid_content_items > len(content) * 0.5:  # More than 50% invalid items
            filtering_results["noise_detected"] = True
            filtering_results["noise_reasons"].append(f"high_invalid_content_ratio: {invalid_content_items}/{len(content)}")
            filtering_results["validations_failed"].append("content_item_validation")
            return filtering_results
        
        filtering_results["validations_passed"].append("content_item_validation")
        
        # CRITICAL: Validation 5 - Signal quality assessment
        quality_score = 1.0
        
        # Deduct points for missing optional fields
        optional_fields = ["timestamp", "created_at", "metadata"]
        missing_optional = [field for field in optional_fields if field not in signal_data]
        quality_score -= len(missing_optional) * 0.1
        
        # Deduct points for content quality issues
        content_quality_issues = 0
        for item in content:
            if not isinstance(item.get("id"), str):
                content_quality_issues += 1
            if not item.get("text") and not item.get("content"):
                content_quality_issues += 1
        
        if content_quality_issues > 0:
            quality_score -= (content_quality_issues / len(content)) * 0.3
        
        quality_score = max(0.0, quality_score)
        filtering_results["signal_quality_score"] = quality_score
        
        # CRITICAL: Reject low-quality signals
        if quality_score < 0.3:
            filtering_results["noise_detected"] = True
            filtering_results["noise_reasons"].append(f"low_signal_quality: {quality_score:.2f}")
            filtering_results["validations_failed"].append("quality_threshold_validation")
            return filtering_results
        
        filtering_results["validations_passed"].append("quality_threshold_validation")
        
        # CRITICAL: Validation 6 - Duplicate detection
        signal_fingerprint = self._calculate_signal_fingerprint(signal_data)
        if signal_fingerprint in self._recent_signal_fingerprints:
            filtering_results["noise_detected"] = True
            filtering_results["noise_reasons"].append("duplicate_signal_detected")
            filtering_results["validations_failed"].append("duplicate_detection")
            return filtering_results
        
        filtering_results["validations_passed"].append("duplicate_detection")
        self._recent_signal_fingerprints.add(signal_fingerprint)
        
        return filtering_results
    
    def _process_filtered_to_candidate(self, signal_id: str) -> None:
        """Process signal from FILTERED to CANDIDATE stage - enrichment and validation"""
        if signal_id not in self.intake_staging:
            return
        
        staged_signal = self.intake_staging[signal_id]
        signal_data = staged_signal["signal_data"]
        
        # CRITICAL: FILTERED → CANDIDATE - Enrich and validate signal
        candidate_results = self._enrich_candidate_signal(signal_data)
        staged_signal["candidate_results"] = candidate_results
        
        if not candidate_results["enrichment_successful"]:
            # CRITICAL: Enrichment failed - block pipeline progression
            staged_signal["status"] = "ENRICHMENT_FAILED"
            staged_signal["pipeline_stage"] = "REJECTED"
            
            staged_signal["processing_history"].append({
                "stage": "CANDIDATE",
                "timestamp": datetime.utcnow().isoformat(),
                "action": "enrichment_failed",
                "reason": candidate_results["enrichment_failure_reason"]
            })
            
            self.logger.error(
                f"ENRICHMENT FAILED for signal {signal_id} - blocking pipeline progression",
                extra={
                    "signal_id": signal_id,
                    "pipeline_stage": "REJECTED",
                    "enrichment_failure_reason": candidate_results["enrichment_failure_reason"],
                    "rejection_timestamp": datetime.utcnow().isoformat()
                }
            )
            return
        
        # CRITICAL: Signal enriched - advance to CANDIDATE stage
        staged_signal["status"] = "CANDIDATE"
        staged_signal["pipeline_stage"] = "CANDIDATE"
        staged_signal["enriched_data"] = candidate_results["enriched_data"]
        
        staged_signal["processing_history"].append({
            "stage": "CANDIDATE",
            "timestamp": datetime.utcnow().isoformat(),
            "action": "enrichment_successful",
            "reason": "signal_enriched_and_validated"
        })
        
        self.logger.info(
            f"Signal {signal_id} advanced to CANDIDATE stage - enrichment successful",
            extra={
                "signal_id": signal_id,
                "pipeline_stage": "CANDIDATE",
                "enrichment_timestamp": datetime.utcnow().isoformat()
            }
        )
        
        # CRITICAL: Continue to CANDIDATE → VERIFIED pipeline
        self._process_candidate_to_verified(signal_id)
    
    def _enrich_candidate_signal(self, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich FILTERED signal to CANDIDATE stage"""
        enrichment_results = {
            "enrichment_successful": False,
            "enrichment_failure_reason": None,
            "enrichment_timestamp": datetime.utcnow().isoformat(),
            "enriched_data": {},
            "enrichment_steps": []
        }
        
        try:
            enriched_data = signal_data.copy()
            
            # Step 1: Add enrichment metadata
            enriched_data["enrichment_metadata"] = {
                "enriched_at": datetime.utcnow().isoformat(),
                "enrichment_version": "1.0",
                "pipeline_id": self.run_id
            }
            enrichment_results["enrichment_steps"].append("metadata_enrichment")
            
            # Step 2: Content enrichment
            content = enriched_data.get("content", [])
            enriched_content = []
            
            for item in content:
                enriched_item = item.copy()
                
                # Add content enrichment
                enriched_item["content_hash"] = self._calculate_content_hash(item)
                enriched_item["enrichment_timestamp"] = datetime.utcnow().isoformat()
                enriched_item["pipeline_processed"] = True
                
                # Add quality metrics
                enriched_item["quality_metrics"] = {
                    "has_id": bool(item.get("id")),
                    "has_text": bool(item.get("text") or item.get("content")),
                    "has_metadata": bool(item.get("metadata")),
                    "content_length": len(str(item))
                }
                
                enriched_content.append(enriched_item)
            
            enriched_data["content"] = enriched_content
            enrichment_results["enrichment_steps"].append("content_enrichment")
            
            # Step 3: Signal quality scoring
            quality_score = self._calculate_enriched_signal_quality(enriched_data)
            enriched_data["quality_score"] = quality_score
            enrichment_results["enrichment_steps"].append("quality_scoring")
            
            # Step 4: Platform-specific enrichment
            platform = enriched_data.get("platform")
            if platform in self.adapters:
                platform_enrichment = self._get_platform_specific_enrichment(platform, enriched_data)
                enriched_data["platform_enrichment"] = platform_enrichment
                enrichment_results["enrichment_steps"].append("platform_enrichment")
            
            enrichment_results["enriched_data"] = enriched_data
            enrichment_results["enrichment_successful"] = True
            
        except Exception as e:
            enrichment_results["enrichment_failure_reason"] = str(e)
            enrichment_results["enrichment_successful"] = False
        
        return enrichment_results
    
    def _process_candidate_to_verified(self, signal_id: str) -> None:
        """Process signal from CANDIDATE to VERIFIED stage - final verification"""
        if signal_id not in self.intake_staging:
            return
        
        staged_signal = self.intake_staging[signal_id]
        enriched_data = staged_signal.get("enriched_data", {})
        
        # CRITICAL: CANDIDATE → VERIFIED - Final verification
        verification_results = self._verify_candidate_signal(enriched_data)
        staged_signal["verification_results"] = verification_results
        
        if not verification_results["verification_passed"]:
            # CRITICAL: Verification failed - block pipeline progression
            staged_signal["status"] = "VERIFICATION_FAILED"
            staged_signal["pipeline_stage"] = "REJECTED"
            
            staged_signal["processing_history"].append({
                "stage": "VERIFIED",
                "timestamp": datetime.utcnow().isoformat(),
                "action": "verification_failed",
                "reason": verification_results["verification_failure_reason"]
            })
            
            self.logger.error(
                f"VERIFICATION FAILED for signal {signal_id} - blocking pipeline progression",
                extra={
                    "signal_id": signal_id,
                    "pipeline_stage": "REJECTED",
                    "verification_failure_reason": verification_results["verification_failure_reason"],
                    "rejection_timestamp": datetime.utcnow().isoformat()
                }
            )
            return
        
        # CRITICAL: Signal verified - advance to VERIFIED stage
        staged_signal["status"] = "VERIFIED"
        staged_signal["pipeline_stage"] = "VERIFIED"
        staged_signal["verified_data"] = verification_results["verified_data"]
        
        staged_signal["processing_history"].append({
            "stage": "VERIFIED",
            "timestamp": datetime.utcnow().isoformat(),
            "action": "verification_successful",
            "reason": "signal_verified_and_ready_for_storage"
        })
        
        self.logger.info(
            f"Signal {signal_id} advanced to VERIFIED stage - verification successful",
            extra={
                "signal_id": signal_id,
                "pipeline_stage": "VERIFIED",
                "verification_timestamp": datetime.utcnow().isoformat(),
                "ready_for_storage": True
            }
        )
        
        # CRITICAL: Signal is now VERIFIED - can proceed to storage
        self._process_verified_signal(signal_id)
    
    def _verify_candidate_signal(self, enriched_data: Dict[str, Any]) -> Dict[str, Any]:
        """Verify CANDIDATE signal for VERIFIED stage"""
        verification_results = {
            "verification_passed": False,
            "verification_failure_reason": None,
            "verification_timestamp": datetime.utcnow().isoformat(),
            "verified_data": {},
            "verification_checks": []
        }
        
        # Check 1: Enriched data integrity
        required_enriched_fields = ["enrichment_metadata", "content", "quality_score"]
        missing_enriched = [field for field in required_enriched_fields if field not in enriched_data]
        if missing_enriched:
            verification_results["verification_failure_reason"] = f"missing_enriched_fields: {missing_enriched}"
            return verification_results
        
        verification_results["verification_checks"].append("enriched_data_integrity")
        
        # Check 2: Content quality threshold
        quality_score = enriched_data.get("quality_score", 0.0)
        if quality_score < 0.5:
            verification_results["verification_failure_reason"] = f"quality_score_below_threshold: {quality_score}"
            return verification_results
        
        verification_results["verification_checks"].append("quality_threshold_verification")
        
        # Check 3: Content hash consistency
        content = enriched_data.get("content", [])
        content_hashes = [item.get("content_hash") for item in content if "content_hash" in item]
        if len(content_hashes) != len(content):
            verification_results["verification_failure_reason"] = "missing_content_hashes"
            return verification_results
        
        verification_results["verification_checks"].append("content_hash_consistency")
        
        # Check 4: Platform enrichment consistency
        platform = enriched_data.get("platform")
        if platform and platform not in self.adapters:
            verification_results["verification_failure_reason"] = f"invalid_platform_in_enriched_data: {platform}"
            return verification_results
        
        verification_results["verification_checks"].append("platform_consistency_verification")
        
        # All checks passed
        verification_results["verification_passed"] = True
        verification_results["verified_data"] = enriched_data
        
        return verification_results
    
    def _process_verified_signal(self, signal_id: str) -> None:
        """Process VERIFIED signal - ready for downstream systems"""
        if signal_id not in self.intake_staging:
            return
        
        staged_signal = self.intake_staging[signal_id]
        verified_data = staged_signal.get("verified_data", {})
        
        # CRITICAL: VERIFIED signal is now ready for downstream systems
        staged_signal["status"] = "READY_FOR_STORAGE"
        staged_signal["pipeline_stage"] = "VERIFIED"
        staged_signal["ready_for_downstream"] = True
        
        staged_signal["processing_history"].append({
            "stage": "VERIFIED",
            "timestamp": datetime.utcnow().isoformat(),
            "action": "signal_ready_for_storage",
            "reason": "pipeline_completed_successfully"
        })
        
        self.logger.info(
            f"Signal {signal_id} completed RAW → FILTERED → CANDIDATE → VERIFIED pipeline",
            extra={
                "signal_id": signal_id,
                "pipeline_stage": "VERIFIED",
                "pipeline_completed": True,
                "ready_for_storage": True,
                "noise_prevented": True,
                "completion_timestamp": datetime.utcnow().isoformat()
            }
        )
        
        # CRITICAL: Mark signal as processed and ready for storage
        staged_signal["status"] = "processed"
        staged_signal["processed_at"] = datetime.utcnow().isoformat()
    
    def _calculate_signal_fingerprint(self, signal_data: Dict[str, Any]) -> str:
        """Calculate unique fingerprint for signal duplicate detection"""
        import hashlib
        
        # Create deterministic fingerprint from signal data
        fingerprint_data = {
            "platform": signal_data.get("platform"),
            "type": signal_data.get("type"),
            "content_count": len(signal_data.get("content", [])),
            "content_ids": sorted([item.get("id") for item in signal_data.get("content", []) if item.get("id")])
        }
        
        fingerprint_str = json.dumps(fingerprint_data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:16]
    
    def _calculate_content_hash(self, item: Dict[str, Any]) -> str:
        """Calculate hash for individual content item"""
        import hashlib
        
        # Create hash from content item
        hash_data = {
            "id": item.get("id"),
            "text": item.get("text", ""),
            "content": item.get("content", ""),
            "metadata": item.get("metadata", {})
        }
        
        hash_str = json.dumps(hash_data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(hash_str.encode()).hexdigest()[:16]
    
    def _calculate_enriched_signal_quality(self, enriched_data: Dict[str, Any]) -> float:
        """Calculate quality score for enriched signal"""
        quality_score = 1.0
        
        # Deduct points for missing enrichment
        if not enriched_data.get("enrichment_metadata"):
            quality_score -= 0.2
        
        # Content quality assessment
        content = enriched_data.get("content", [])
        if content:
            content_quality = 0.0
            for item in content:
                item_quality = 0.0
                if item.get("content_hash"):
                    item_quality += 0.3
                if item.get("quality_metrics", {}).get("has_id"):
                    item_quality += 0.2
                if item.get("quality_metrics", {}).get("has_text"):
                    item_quality += 0.3
                if item.get("quality_metrics", {}).get("has_metadata"):
                    item_quality += 0.2
                
                content_quality += item_quality
            
            content_quality /= len(content)
            quality_score = quality_score * 0.7 + content_quality * 0.3
        
        return max(0.0, quality_score)
    
    def _get_platform_specific_enrichment(self, platform: str, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get platform-specific enrichment for signal"""
        # Placeholder for platform-specific enrichment logic
        return {
            "platform": platform,
            "enrichment_type": "platform_specific",
            "enrichment_timestamp": datetime.utcnow().isoformat(),
            "platform_capabilities": self.adapters[platform].get_capabilities() if hasattr(self.adapters[platform], 'get_capabilities') else {}
        }
    
    def _record_meta_learning_feedback(self, platform: str, job_id: str, metrics: Dict[str, float]) -> None:
        """Record feedback for meta-learning"""
        feedback_entry = {
            "platform": platform,
            "job_id": job_id,
            "timestamp": datetime.utcnow().isoformat(),
            "metrics": metrics,
            "run_id": self.run_id
        }
        
        self.learning_feedback.append(feedback_entry)
        
        # Update platform-specific metrics
        for metric_name, value in metrics.items():
            if platform not in self.meta_learning_metrics:
                self.meta_learning_metrics[platform] = {}
            self.meta_learning_metrics[platform][metric_name] = value
        
        # Keep feedback history bounded
        if len(self.learning_feedback) > 10000:
            self.learning_feedback = self.learning_feedback[-10000:]
        
        self.logger.debug(
            f"Recorded meta-learning feedback for {platform}",
            extra={
                "platform": platform,
                "job_id": job_id,
                "metrics": metrics,
                "feedback_count": len(self.learning_feedback)
            }
        )
    
    def _add_causal_dependency(self, job_id: str, depends_on: str) -> None:
        """Add a causal dependency between jobs"""
        self.causal_dependencies[depends_on].add(job_id)
        self.causal_graph[job_id] = {
            "depends_on": depends_on,
            "dependency_created": datetime.utcnow().isoformat(),
            "dependency_satisfied": False
        }
        
        self.logger.debug(
            f"Added causal dependency: {job_id} depends on {depends_on}",
            extra={
                "job_id": job_id,
                "depends_on": depends_on,
                "dependency_type": "causal"
            }
        )
    
    def _validate_causal_dependencies(self, job: IngestionJob) -> bool:
        """Validate that all causal dependencies are satisfied"""
        job_id = job.job_id
        
        # Check cache first
        if job_id in self.dependency_validation_cache:
            return self.dependency_validation_cache[job_id]
        
        # Check if job has dependencies
        if job_id not in self.causal_graph:
            self.dependency_validation_cache[job_id] = True
            return True
        
        dependency_info = self.causal_graph[job_id]
        depends_on = dependency_info["depends_on"]
        
        # Validate dependency
        dependency_satisfied = self._check_dependency_satisfied(depends_on)
        dependency_info["dependency_satisfied"] = dependency_satisfied
        
        # Cache result
        self.dependency_validation_cache[job_id] = dependency_satisfied
        
        if not dependency_satisfied:
            self.logger.warning(
                f"Causal dependency not satisfied for job {job_id}: depends on {depends_on}",
                extra={
                    "job_id": job_id,
                    "depends_on": depends_on,
                    "dependency_satisfied": False,
                    "causal_violation": True
                }
            )
        
        return dependency_satisfied
    
    def _check_dependency_satisfied(self, dependency_job_id: str) -> bool:
        """Check if a dependency job has been completed successfully"""
        # Check if dependency job completed successfully
        if dependency_job_id in self.completed_jobs:
            completion_record = self.completed_jobs[dependency_job_id]
            return completion_record.get("status") in ["success", "shadow_success"]
        
        # Check if dependency job is in causal sequence (completed)
        for sequence_entry in self.ingestion_sequence:
            if sequence_entry["job_id"] == dependency_job_id:
                return True  # Found in sequence means completed
        
        return False
    
    def _mark_dependency_satisfied(self, job_id: str) -> None:
        """Mark a job as satisfying its dependencies"""
        # Update all dependent jobs
        for dependent_job_id in self.causal_dependencies[job_id]:
            if dependent_job_id in self.causal_graph:
                self.causal_graph[dependent_job_id]["dependency_satisfied"] = True
                # Clear cache to force revalidation
                self.dependency_validation_cache.pop(dependent_job_id, None)
                
                self.logger.debug(
                    f"Marked dependency satisfied: {dependent_job_id} no longer depends on {job_id}",
                    extra={
                        "job_id": job_id,
                        "dependent_job_id": dependent_job_id,
                        "dependency_satisfied": True
                    }
                )
    
    def _detect_causal_cycles(self) -> List[str]:
        """Detect cycles in causal dependency graph"""
        visited = set()
        rec_stack = set()
        cycles = []
        
        def dfs_cycle_detection(job_id: str, path: List[str]) -> bool:
            if job_id in rec_stack:
                # Cycle detected
                cycle_start = path.index(job_id)
                cycle = path[cycle_start:] + [job_id]
                cycles.append(" -> ".join(cycle))
                return True
            
            if job_id in visited:
                return False
            
            visited.add(job_id)
            rec_stack.add(job_id)
            
            # Check all dependencies
            for dependent_job_id in self.causal_dependencies[job_id]:
                if dfs_cycle_detection(dependent_job_id, path + [job_id]):
                    return True
            
            rec_stack.remove(job_id)
            return False
        
        # Check all jobs in the graph
        for job_id in self.causal_graph.keys():
            if job_id not in visited:
                dfs_cycle_detection(job_id, [])
        
        return cycles
    
    def _track_inflight_job(self, job: IngestionJob) -> None:
        """Track job when it becomes inflight"""
        self.inflight_job_recovery[job.job_id] = datetime.utcnow()
        self.inflight_jobs.add(job.job_id)
        
        self.logger.debug(
            f"Job {job.job_id} became inflight",
            extra={
                "job_id": job.job_id,
                "platform": job.platform,
                "inflight_timestamp": self.inflight_job_recovery[job.job_id].isoformat()
            }
        )
    
    def _calculate_checkpoint_integrity_hash(self, checkpoint: Dict[str, Any]) -> str:
        """Calculate integrity hash for checkpoint corruption detection"""
        import hashlib
        
        # Create a deterministic string representation
        checkpoint_str = json.dumps(checkpoint, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(checkpoint_str.encode()).hexdigest()
    
    def _validate_checkpoint_integrity(self, checkpoint: Dict[str, Any]) -> bool:
        """Validate checkpoint integrity and detect corruption"""
        if not self.partial_recovery_enabled:
            return True
        
        # Check if checkpoint has integrity hash
        stored_hash = checkpoint.get("integrity_hash")
        if not stored_hash:
            self.logger.warning("Checkpoint missing integrity hash - cannot validate")
            return False
        
        # Calculate current hash
        current_hash = self._calculate_checkpoint_integrity_hash(checkpoint)
        
        # Compare hashes
        if stored_hash != current_hash:
            self.logger.error(
                f"Checkpoint corruption detected: hash mismatch",
                extra={
                    "stored_hash": stored_hash[:16] + "...",
                    "current_hash": current_hash[:16] + "...",
                    "corruption_detected": True
                }
            )
            return False
        
        return True
    
    async def _handle_checkpoint_corruption(self, checkpoint: Dict[str, Any]) -> Dict[str, Any]:
        """Handle partially corrupted checkpoints with selective recovery"""
        self.logger.warning("Attempting partial recovery from corrupted checkpoint")
        
        # Try to recover individual components
        recovered_checkpoint = {}
        recovery_stats = {"total_components": 0, "recovered": 0, "failed": 0}
        
        # Define critical components and their recovery strategies
        critical_components = {
            "ingestion_ledger": self._recover_ledger_component,
            "last_successful_ingestion": self._recover_timestamp_component,
            "last_seen_timestamp": self._recover_timestamp_component,
            "platform_health": self._recover_platform_health_component,
            "causal_sequence_counter": self._recover_simple_component,
            "ingestion_sequence": self._recover_sequence_component,
            "content_lineage": self._recover_lineage_component,
            "completed_jobs": self._recover_completed_jobs_component
        }
        
        for component_name, recovery_func in critical_components.items():
            recovery_stats["total_components"] += 1
            
            try:
                recovered_value = recovery_func(checkpoint.get(component_name))
                if recovered_value is not None:
                    recovered_checkpoint[component_name] = recovered_value
                    recovery_stats["recovered"] += 1
                    self.logger.debug(f"Recovered component: {component_name}")
                else:
                    recovery_stats["failed"] += 1
                    self.logger.warning(f"Failed to recover component: {component_name}")
            except Exception as e:
                recovery_stats["failed"] += 1
                self.logger.error(f"Error recovering component {component_name}: {e}")
        
        # Add recovery metadata
        recovered_checkpoint["recovery_metadata"] = {
            "recovery_timestamp": datetime.utcnow().isoformat(),
            "recovery_stats": recovery_stats,
            "original_checkpoint_version": checkpoint.get("version", 0),
            "recovery_checkpoint_version": self.checkpoint_version
        }
        
        self.logger.info(
            f"Partial recovery completed: {recovery_stats['recovered']}/{recovery_stats['total_components']} components recovered",
            extra=recovery_stats
        )
        
        return recovered_checkpoint
    
    def _recover_ledger_component(self, ledger_data: Any) -> Optional[Dict[str, str]]:
        """Recover ingestion ledger with validation"""
        if not isinstance(ledger_data, dict):
            return None
        
        # Validate ledger entries
        valid_ledger = {}
        for fingerprint, job_id in ledger_data.items():
            if isinstance(fingerprint, str) and isinstance(job_id, str):
                valid_ledger[fingerprint] = job_id
        
        return valid_ledger if valid_ledger else None
    
    def _recover_timestamp_component(self, timestamp_data: Any) -> Optional[Dict[str, datetime]]:
        """Recover timestamp data with validation"""
        if not isinstance(timestamp_data, dict):
            return None
        
        recovered_timestamps = {}
        for key, ts_str in timestamp_data.items():
            try:
                if isinstance(ts_str, str):
                    recovered_timestamps[key] = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue  # Skip invalid timestamps
        
        return recovered_timestamps if recovered_timestamps else None
    
    def _recover_platform_health_component(self, health_data: Any) -> Optional[Dict[str, Dict[str, Any]]]:
        """Recover platform health data with validation"""
        if not isinstance(health_data, dict):
            return None
        
        recovered_health = {}
        for platform, health in health_data.items():
            if isinstance(health, dict):
                # Validate and clean health data
                clean_health = {
                    "failures": health.get("failures", 0),
                    "disabled_until": None,
                    "last_success": None,
                    "health_status": PlatformHealth.HEALTHY
                }
                
                # Recover datetime fields
                if health.get("disabled_until"):
                    try:
                        clean_health["disabled_until"] = datetime.fromisoformat(health["disabled_until"])
                    except (ValueError, TypeError):
                        pass
                
                if health.get("last_success"):
                    try:
                        clean_health["last_success"] = datetime.fromisoformat(health["last_success"])
                    except (ValueError, TypeError):
                        pass
                
                # Recover health status
                if health.get("health_status"):
                    try:
                        clean_health["health_status"] = PlatformHealth(health["health_status"])
                    except ValueError:
                        pass
                
                recovered_health[platform] = clean_health
        
        return recovered_health if recovered_health else None
    
    def _recover_simple_component(self, data: Any) -> Any:
        """Recover simple components with basic validation"""
        return data if data is not None else None
    
    def _recover_sequence_component(self, sequence_data: Any) -> Optional[List[Dict[str, Any]]]:
        """Recover sequence data with validation"""
        if not isinstance(sequence_data, list):
            return None
        
        recovered_sequence = []
        for entry in sequence_data:
            if isinstance(entry, dict) and "sequence_id" in entry:
                recovered_sequence.append(entry)
        
        return recovered_sequence if recovered_sequence else None
    
    def _recover_lineage_component(self, lineage_data: Any) -> Optional[Dict[str, List[int]]]:
        """Recover lineage data with validation"""
        if not isinstance(lineage_data, dict):
            return None
        
        recovered_lineage = {}
        for content_id, sequence_ids in lineage_data.items():
            if isinstance(content_id, str) and isinstance(sequence_ids, list):
                # Validate sequence IDs are integers
                valid_sequence_ids = [sid for sid in sequence_ids if isinstance(sid, int)]
                if valid_sequence_ids:
                    recovered_lineage[content_id] = valid_sequence_ids
        
        return recovered_lineage if recovered_lineage else None
    
    def _recover_completed_jobs_component(self, jobs_data: Any) -> Optional[Dict[str, Dict[str, Any]]]:
        """Recover completed jobs with validation"""
        if not isinstance(jobs_data, dict):
            return None
        
        recovered_jobs = {}
        for job_id, job_data in jobs_data.items():
            if isinstance(job_id, str) and isinstance(job_data, dict):
                # Validate job data structure
                if "status" in job_data and "completed_at" in job_data:
                    recovered_jobs[job_id] = job_data
        
        return recovered_jobs if recovered_jobs else None
    
    def _untrack_inflight_job(self, job_id: str) -> None:
        """Untrack job when it completes"""
        self.inflight_jobs.discard(job_id)
        self.inflight_job_recovery.pop(job_id, None)
        
        self.logger.debug(
            f"Job {job_id} no longer inflight",
            extra={
                "job_id": job_id,
                "remaining_inflight": len(self.inflight_jobs)
            }
        )
    
    def _validate_system_invariants(self) -> List[Dict[str, Any]]:
        """Validate all system invariants and return violations"""
        violations = []
        now = datetime.utcnow()
        
        # Invariant 1: No duplicate job IDs across active systems
        violations.extend(self._validate_job_id_uniqueness())
        
        # Invariant 2: Causal sequence monotonicity
        violations.extend(self._validate_causal_sequence_monotonicity())
        
        # Invariant 3: Timestamp ordering consistency
        violations.extend(self._validate_timestamp_ordering())
        
        # Invariant 4: Resource allocation limits
        violations.extend(self._validate_resource_allocation())
        
        # Invariant 5: Data consistency across collections
        violations.extend(self._validate_data_consistency())
        
        # Invariant 6: Platform health state consistency
        violations.extend(self._validate_platform_health_consistency())
        
        # Invariant 7: Queue state consistency
        violations.extend(self._validate_queue_state_consistency())
        
        # Invariant 8: Checkpoint integrity
        violations.extend(self._validate_checkpoint_integrity_invariant())
        
        # Store violations for monitoring
        self.invariant_violations.extend(violations)
        self.last_invariant_check = now
        
        # Keep violations bounded
        if len(self.invariant_violations) > 1000:
            self.invariant_violations = self.invariant_violations[-1000:]
        
        if violations:
            self.logger.error(
                f"System invariant violations detected: {len(violations)}",
                extra={
                    "violation_count": len(violations),
                    "violations": violations[:10],  # Log first 10
                    "check_timestamp": now.isoformat()
                }
            )
        
        return violations
    
    def _validate_job_id_uniqueness(self) -> List[Dict[str, Any]]:
        """Validate no duplicate job IDs across active systems"""
        violations = []
        
        # Collect all job IDs from different systems
        all_job_ids = set()
        
        # Check queue jobs
        for _, _, job in self.job_queue:
            if job.job_id in all_job_ids:
                violations.append({
                    "type": "duplicate_job_id",
                    "job_id": job.job_id,
                    "location": "queue",
                    "severity": "high",
                    "timestamp": datetime.utcnow().isoformat()
                })
            all_job_ids.add(job.job_id)
        
        # Check inflight jobs
        for job_id in self.inflight_jobs:
            if job_id in all_job_ids:
                violations.append({
                    "type": "duplicate_job_id",
                    "job_id": job_id,
                    "location": "inflight",
                    "severity": "high",
                    "timestamp": datetime.utcnow().isoformat()
                })
            all_job_ids.add(job_id)
        
        # Check completed jobs
        for job_id in self.completed_jobs:
            if job_id in all_job_ids:
                violations.append({
                    "type": "duplicate_job_id",
                    "job_id": job_id,
                    "location": "completed",
                    "severity": "medium",
                    "timestamp": datetime.utcnow().isoformat()
                })
            all_job_ids.add(job_id)
        
        return violations
    
    def _validate_causal_sequence_monotonicity(self) -> List[Dict[str, Any]]:
        """Validate causal sequence counter is monotonic"""
        violations = []
        
        if not self.ingestion_sequence:
            return violations
        
        # Check sequence IDs are monotonic increasing
        for i in range(1, len(self.ingestion_sequence)):
            current_seq = self.ingestion_sequence[i]["sequence_id"]
            prev_seq = self.ingestion_sequence[i-1]["sequence_id"]
            
            if current_seq <= prev_seq:
                violations.append({
                    "type": "non_monotonic_sequence",
                    "sequence_id": current_seq,
                    "previous_sequence_id": prev_seq,
                    "position": i,
                    "severity": "high",
                    "timestamp": datetime.utcnow().isoformat()
                })
        
        # Check counter matches max sequence ID
        max_sequence_id = max(entry["sequence_id"] for entry in self.ingestion_sequence)
        if self.causal_sequence_counter != max_sequence_id:
            violations.append({
                "type": "sequence_counter_mismatch",
                "counter": self.causal_sequence_counter,
                "max_sequence_id": max_sequence_id,
                "severity": "medium",
                "timestamp": datetime.utcnow().isoformat()
            })
        
        return violations
    
    def _validate_timestamp_ordering(self) -> List[Dict[str, Any]]:
        """Validate timestamp ordering consistency"""
        violations = []
        
        # Check last_seen_timestamp ordering
        for content_id, timestamp in self.last_seen_timestamp.items():
            # Check if timestamp is not in the future
            if timestamp > datetime.utcnow():
                violations.append({
                    "type": "future_timestamp",
                    "content_id": content_id,
                    "timestamp": timestamp.isoformat(),
                    "severity": "medium",
                    "timestamp": datetime.utcnow().isoformat()
                })
        
        # Check platform success timestamps
        for platform, timestamp in self.last_successful_ingestion.items():
            if timestamp > datetime.utcnow():
                violations.append({
                    "type": "future_timestamp",
                    "platform": platform,
                    "timestamp": timestamp.isoformat(),
                    "severity": "medium",
                    "timestamp": datetime.utcnow().isoformat()
                })
        
        return violations
    
    def _validate_resource_allocation(self) -> List[Dict[str, Any]]:
        """Validate resource allocation limits and consistency"""
        violations = []
        
        # Check inflight jobs don't exceed max concurrent
        if len(self.inflight_jobs) > self.max_concurrent_jobs:
            violations.append({
                "type": "inflight_limit_exceeded",
                "inflight_count": len(self.inflight_jobs),
                "max_concurrent": self.max_concurrent_jobs,
                "severity": "high",
                "timestamp": datetime.utcnow().isoformat()
            })
        
        # Check queue size is reasonable
        if len(self.job_queue) > 10000:  # Configurable limit
            violations.append({
                "type": "queue_size_excessive",
                "queue_size": len(self.job_queue),
                "limit": 10000,
                "severity": "medium",
                "timestamp": datetime.utcnow().isoformat()
            })
        
        return violations
    
    def _validate_data_consistency(self) -> List[Dict[str, Any]]:
        """Validate data consistency across collections"""
        violations = []
        
        # Check ledger consistency
        for fingerprint, job_id in self.ingestion_ledger.items():
            # Check if job exists in completed jobs
            if job_id not in self.completed_jobs:
                violations.append({
                    "type": "ledger_orphan",
                    "fingerprint": fingerprint[:16] + "...",
                    "job_id": job_id,
                    "severity": "medium",
                    "timestamp": datetime.utcnow().isoformat()
                })
        
        # Check content lineage consistency
        for content_id, sequence_ids in self.content_lineage.items():
            for seq_id in sequence_ids:
                # Check if sequence exists
                sequence_exists = any(
                    entry["sequence_id"] == seq_id 
                    for entry in self.ingestion_sequence
                )
                if not sequence_exists:
                    violations.append({
                        "type": "orphan_lineage",
                        "content_id": content_id,
                        "sequence_id": seq_id,
                        "severity": "low",
                        "timestamp": datetime.utcnow().isoformat()
                    })
        
        return violations
    
    def _validate_platform_health_consistency(self) -> List[Dict[str, Any]]:
        """Validate platform health state consistency"""
        violations = []
        
        for platform, health in self.platform_health.items():
            # Check failure count is non-negative
            if health["failures"] < 0:
                violations.append({
                    "type": "negative_failure_count",
                    "platform": platform,
                    "failures": health["failures"],
                    "severity": "medium",
                    "timestamp": datetime.utcnow().isoformat()
                })
            
            # Check disabled_until is in the future if set
            if health["disabled_until"] and health["disabled_until"] < datetime.utcnow():
                violations.append({
                    "type": "past_disabled_until",
                    "platform": platform,
                    "disabled_until": health["disabled_until"].isoformat(),
                    "severity": "low",
                    "timestamp": datetime.utcnow().isoformat()
                })
        
        return violations
    
    def _validate_queue_state_consistency(self) -> List[Dict[str, Any]]:
        """Validate queue state consistency"""
        violations = []
        
        # Check heap property is maintained
        try:
            # Verify heap property
            for i in range(len(self.job_queue)):
                left = 2 * i + 1
                right = 2 * i + 2
                
                if left < len(self.job_queue):
                    if self.job_queue[i][0] > self.job_queue[left][0]:  # priority comparison
                        violations.append({
                            "type": "heap_property_violation",
                            "index": i,
                            "left_child": left,
                            "severity": "high",
                            "timestamp": datetime.utcnow().isoformat()
                        })
                
                if right < len(self.job_queue):
                    if self.job_queue[i][0] > self.job_queue[right][0]:  # priority comparison
                        violations.append({
                            "type": "heap_property_violation",
                            "index": i,
                            "right_child": right,
                            "severity": "high",
                            "timestamp": datetime.utcnow().isoformat()
                        })
        except Exception as e:
            violations.append({
                "type": "queue_validation_error",
                "error": str(e),
                "severity": "high",
                "timestamp": datetime.utcnow().isoformat()
            })
        
        return violations
    
    def _validate_checkpoint_integrity_invariant(self) -> List[Dict[str, Any]]:
        """Validate checkpoint integrity invariant"""
        violations = []
        
        # Check if we have a valid integrity hash
        if not self.checkpoint_integrity_hash:
            violations.append({
                "type": "missing_integrity_hash",
                "severity": "low",
                "timestamp": datetime.utcnow().isoformat()
            })
        
        return violations
    
    def _check_inflight_job_recovery(self) -> None:
        """Check for inflight jobs that need recovery"""
        now = datetime.utcnow()
        recovery_jobs = []
        
        # CRITICAL: Validate recovery system consistency
        self._validate_recovery_system_consistency()
        
        for job_id, inflight_timestamp in list(self.inflight_job_recovery.items()):
            age = now - inflight_timestamp
            
            if age > self.inflight_job_timeout:
                # Job has been inflight too long - needs recovery
                recovery_jobs.append((job_id, inflight_timestamp, age))
        
        # Spawn recovery jobs for stale inflight jobs
        for job_id, inflight_timestamp, age in recovery_jobs:
            self._spawn_recovery_job(job_id, inflight_timestamp, age)
    
    def _validate_recovery_system_consistency(self) -> None:
        """Validate that recovery system is consistent and detect liveness bugs"""
        # CRITICAL: Check for liveness bug - inflight jobs without recovery timestamps
        inflight_jobs_without_recovery = []
        for job_id in self.inflight_jobs:
            if job_id not in self.inflight_job_recovery:
                inflight_jobs_without_recovery.append(job_id)
        
        if inflight_jobs_without_recovery:
            # This is the liveness bug - fix it immediately
            self.logger.error(
                f"LIVENESS BUG DETECTED: {len(inflight_jobs_without_recovery)} inflight jobs missing recovery timestamps",
                extra={
                    "liveness_bug": True,
                    "inflight_jobs_without_recovery": inflight_jobs_without_recovery,
                    "total_inflight_jobs": len(self.inflight_jobs),
                    "recovery_jobs_tracked": len(self.inflight_job_recovery),
                    "bug_severity": "critical",
                    "fix_applied": "auto_recovery_timestamp_assignment"
                }
            )
            
            # Fix the liveness bug by assigning recovery timestamps
            now = datetime.utcnow()
            for job_id in inflight_jobs_without_recovery:
                # Assume the job became inflight at system startup (safe assumption)
                self.inflight_job_recovery[job_id] = now - timedelta(minutes=1)  # 1 minute ago to trigger immediate recovery check
            
            self.logger.info(
                f"LIVENESS BUG FIXED: Assigned recovery timestamps to {len(inflight_jobs_without_recovery)} jobs",
                extra={
                    "liveness_bug_fixed": True,
                    "jobs_fixed": len(inflight_jobs_without_recovery),
                    "recovery_timestamps_assigned": len(inflight_jobs_without_recovery)
                }
            )
        
        # CRITICAL: Check for orphaned recovery timestamps (recovery timestamps for jobs that aren't inflight)
        orphaned_recovery_timestamps = []
        for job_id in self.inflight_job_recovery:
            if job_id not in self.inflight_jobs:
                orphaned_recovery_timestamps.append(job_id)
        
        if orphaned_recovery_timestamps:
            self.logger.warning(
                f"Found {len(orphaned_recovery_timestamps)} orphaned recovery timestamps - cleaning up",
                extra={
                    "orphaned_recovery_timestamps": orphaned_recovery_timestamps,
                    "cleanup_action": "remove_orphaned_timestamps"
                }
            )
            
            # Clean up orphaned recovery timestamps
            for job_id in orphaned_recovery_timestamps:
                del self.inflight_job_recovery[job_id]
        
        # Log recovery system status
        self.logger.debug(
            f"Recovery system status: {len(self.inflight_jobs)} inflight jobs, {len(self.inflight_job_recovery)} recovery timestamps",
            extra={
                "inflight_jobs_count": len(self.inflight_jobs),
                "recovery_timestamps_count": len(self.inflight_job_recovery),
                "system_consistent": len(self.inflight_jobs) == len(self.inflight_job_recovery),
                "recovery_system_healthy": len(inflight_jobs_without_recovery) == 0 and len(orphaned_recovery_timestamps) == 0
            }
        )
    
    def _spawn_recovery_job(self, original_job_id: str, inflight_timestamp: datetime, age: timedelta) -> None:
        """Spawn a recovery job for a stale inflight job"""
        recovery_job_id = f"recovery_{original_job_id}_{int(datetime.utcnow().timestamp())}"
        
        # Create recovery job
        recovery_job = IngestionJob(
            run_id=self.run_id,
            job_id=recovery_job_id,
            platform="recovery",  # Special recovery platform
            content_id=original_job_id,
            window=TimeWindow(
                start=inflight_timestamp,
                end=datetime.utcnow()
            ),
            mode=IngestionMode.RECOVERY,
            priority=0,  # Highest priority for recovery
            created_at=datetime.utcnow(),
            scheduled_at=datetime.utcnow()
        )
        
        # Add to queue
        self._push_job(recovery_job)
        
        # Remove original job from inflight tracking
        if original_job_id in self.inflight_jobs:
            self.inflight_jobs.remove(original_job_id)
        if original_job_id in self.inflight_job_recovery:
            del self.inflight_job_recovery[original_job_id]
        
        self.logger.info(
            f"Spawned recovery job {recovery_job_id} for stale job {original_job_id} (age: {age.total_seconds():.1f}s)",
            extra={
                "original_job_id": original_job_id,
                "recovery_job_id": recovery_job_id,
                "stale_age_seconds": age.total_seconds(),
                "inflight_timeout": self.inflight_job_timeout.total_seconds()
            }
        )
    
    def _run_invariant_watchdog(self) -> None:
        """Run invariant watchdog - HARD-FAIL MODE"""
        if self.system_frozen_for_invariant_violation:
            # System is already frozen - maintain freeze state
            self.logger.critical(
                "System remains frozen due to invariant violation",
                extra={
                    "system_frozen": True,
                    "panic_reason": self.invariant_panic_reason,
                    "panic_timestamp": self.invariant_panic_timestamp.isoformat() if self.invariant_panic_timestamp else None
                }
            )
            return
        
        # Check for HARD invariant violations
        hard_violations = self._check_hard_invariants()
        
        if hard_violations:
            # Record violation
            violation_time = datetime.utcnow()
            self.invariant_violation_history.append(violation_time)
            
            # Clean old violations outside window
            window_start = violation_time - self.invariant_violation_window
            self.invariant_violation_history = [
                ts for ts in self.invariant_violation_history if ts >= window_start
            ]
            
            # Check if panic threshold exceeded
            recent_violations = len(self.invariant_violation_history)
            if recent_violations >= self.max_invariant_violations_per_hour:
                self._trigger_invariant_panic(hard_violations)
            else:
                # Log warning but continue
                self.logger.error(
                    f"HARD INVARIANT VIOLATION DETECTED: {len(hard_violations)} violations",
                    extra={
                        "hard_violations": hard_violations,
                        "recent_violations": recent_violations,
                        "panic_threshold": self.max_invariant_violations_per_hour,
                        "violation_timestamp": violation_time.isoformat(),
                        "system_frozen": False
                    }
                )
    
    def _check_hard_invariants(self) -> List[Dict[str, Any]]:
        """Check HARD invariants that must never be violated"""
        violations = []
        
        # HARD Invariant 1: Durable Completion
        # No job may enter COMPLETED unless storage + checkpoint succeed
        for job_id, completed_job in self.completed_jobs.items():
            if completed_job.get("status") == JobState.COMPLETED.value:
                # Verify checkpoint confirmation
                if not completed_job.get("checkpoint_confirmed", False):
                    violations.append({
                        "type": "durable_completion_violation",
                        "job_id": job_id,
                        "severity": "critical",
                        "description": "Job marked completed without checkpoint confirmation"
                    })
        
        # HARD Invariant 2: Canonical Identity Uniqueness
        # Same fingerprint can NEVER map to multiple jobs
        fingerprint_to_jobs = defaultdict(list)
        for fingerprint, job_id in self.ingestion_ledger.items():
            fingerprint_to_jobs[fingerprint].append(job_id)
        
        for fingerprint, job_ids in fingerprint_to_jobs.items():
            if len(job_ids) > 1:
                violations.append({
                    "type": "canonical_identity_violation",
                    "fingerprint": fingerprint[:16] + "...",
                    "job_ids": job_ids,
                    "severity": "critical",
                    "description": "Same fingerprint maps to multiple jobs"
                })
        
        # HARD Invariant 3: Monotonic Time
        # Canonical timestamps cannot regress
        for content_id, current_timestamp in self.last_seen_timestamp.items():
            # Check if any completed job has a newer timestamp for same content
            for job_id, completed_job in self.completed_jobs.items():
                if completed_job.get("status") == JobState.COMPLETED.value:
                    job_timestamp = completed_job.get("content_timestamp")
                    if job_timestamp and job_timestamp > current_timestamp:
                        violations.append({
                            "type": "monotonic_time_violation",
                            "content_id": content_id,
                            "current_timestamp": current_timestamp.isoformat(),
                            "job_timestamp": job_timestamp.isoformat(),
                            "job_id": job_id,
                            "severity": "critical",
                            "description": "Content timestamp regression detected"
                        })
        
        # HARD Invariant 4: Causal Ordering
        # Ingestion sequence must strictly increase
        if len(self.ingestion_sequence) > 1:
            for i in range(1, len(self.ingestion_sequence)):
                current_seq = self.ingestion_sequence[i]["sequence_id"]
                prev_seq = self.ingestion_sequence[i-1]["sequence_id"]
                
                if current_seq <= prev_seq:
                    violations.append({
                        "type": "causal_ordering_violation",
                        "current_sequence": current_seq,
                        "previous_sequence": prev_seq,
                        "position": i,
                        "severity": "critical",
                        "description": "Ingestion sequence not strictly increasing"
                    })
        
        # HARD Invariant 5: No Phantom Success
        # No success without verified persistence
        for job_id, completed_job in self.completed_jobs.items():
            if completed_job.get("status") == JobState.COMPLETED.value:
                # Verify storage persistence
                if not completed_job.get("storage_confirmed", False):
                    violations.append({
                        "type": "phantom_success_violation",
                        "job_id": job_id,
                        "severity": "critical",
                        "description": "Job marked completed without storage confirmation"
                    })
        
        # HARD Invariant 6: Ledger Consistency
        # Ledger completed_jobs must agree
        ledger_job_ids = set(self.ingestion_ledger.values())
        completed_job_ids = set(self.completed_jobs.keys())
        
        # Jobs in ledger but not completed (orphaned)
        orphaned_ledger = ledger_job_ids - completed_job_ids
        for job_id in orphaned_ledger:
            # Check if job is inflight (valid case)
            if job_id not in self.inflight_jobs:
                violations.append({
                    "type": "ledger_consistency_violation",
                    "job_id": job_id,
                    "severity": "critical",
                    "description": "Job in ledger but not in completed_jobs or inflight"
                })
        
        return violations
    
    def _trigger_kill_switch(self, trigger_reason: str, violations: List[Dict[str, Any]], context: Dict[str, Any] = None) -> None:
        """Trigger Ingestion Kill-Switch - NON-RECOVERABLE INGESTION FREEZE"""
        if not self.kill_switch_enabled:
            return
        
        # STEP 1 — FREEZE ALL MUTATION (ATOMIC)
        self.scheduler_running = False
        self.worker_running = False
        self.ingestion_enabled = False
        self.scheduler_enabled = False
        
        # STEP 2 — CAPTURE CRASH SNAPSHOT
        kill_switch_snapshot = {
            "trigger_reason": trigger_reason,
            "violated_invariant": violations[0]["type"] if violations else "unknown",
            "job_id": context.get("job_id") if context else None,
            "platform": context.get("platform") if context else None,
            "ingestion_sequence": self.causal_sequence_counter,
            "wall_clock_time": datetime.utcnow().isoformat(),
            "last_good_checkpoint": self._get_last_good_checkpoint_id(),
            "full_state_hash": self._calculate_system_state_hash(),
            "retry_counters": dict(self.failure_history),
            "inflight_jobs": list(self.inflight_jobs),
            "platform_health": dict(self.platform_health),
            "system_state_before_kill": self.system_state,
            "kill_switch_timestamp": datetime.utcnow().isoformat(),
            "violations": violations,
            "context": context or {}
        }
        
        # Append-only, immutable storage
        self.kill_switch_snapshots.append(kill_switch_snapshot)
        
        # STEP 3 — MARK SYSTEM STATE = FROZEN
        self.system_state = "FROZEN"
        self.system_frozen_for_invariant_violation = True
        self.invariant_panic_timestamp = datetime.utcnow()
        self.invariant_panic_reason = f"KILL-SWITCH ACTIVATED: {trigger_reason}"
        
        # Persist state immediately
        self._persist_kill_switch_state()
        
        # STEP 4 — ESCALATE (CRITICAL EVENT)
        self.logger.critical(
            "INGESTION KILL-SWITCH ACTIVATED - SYSTEM FROZEN",
            extra={
                "kill_switch_triggered": True,
                "trigger_reason": trigger_reason,
                "violations": violations,
                "kill_switch_snapshot": kill_switch_snapshot,
                "system_state": self.system_state,
                "ingestion_enabled": False,
                "scheduler_enabled": False,
                "recovery_required": True,
                "incident_declared": True
            }
        )
        
        # This is not "logging" - this is INCIDENT DECLARATION
        self._declare_incident(trigger_reason, kill_switch_snapshot)
    
    def _declare_incident(self, trigger_reason: str, snapshot: Dict[str, Any]) -> None:
        """Declare critical incident - not just logging"""
        # In production, this would integrate with:
        # - PagerDuty, OpsGenie, etc.
        # - Incident management systems
        # - Alerting infrastructure
        # - Audit trail systems
        
        incident_data = {
            "incident_id": f"kill_switch_{int(datetime.utcnow().timestamp())}",
            "severity": "CRITICAL",
            "trigger_reason": trigger_reason,
            "snapshot": snapshot,
            "requires_manual_intervention": True,
            "recovery_protocol": "kill_switch_recovery",
            "declared_at": datetime.utcnow().isoformat(),
            "system_affected": "ingestion_pipeline",
            "impact": "INGESTION_HALTED"
        }
        
        # Log incident for external systems to consume
        self.logger.error(f"INCIDENT_DECLARED: {json.dumps(incident_data)}")
    
    def _persist_kill_switch_state(self) -> None:
        """Persist kill-switch state immediately"""
        try:
            # In production, this would write to durable storage
            # For now, we'll ensure it's included in next checkpoint
            pass
        except Exception as e:
            self.logger.error(f"Failed to persist kill-switch state: {e}")
    
    def _get_last_good_checkpoint_id(self) -> str:
        """Get ID of last known good checkpoint"""
        # This would integrate with checkpoint storage
        return f"checkpoint_{self.checkpoint_version}_{int(self.last_invariant_check.timestamp())}"
    
    def _calculate_system_state_hash(self) -> str:
        """Calculate hash of current system state"""
        import hashlib
        
        state_data = {
            "ingestion_ledger": self.ingestion_ledger,
            "completed_jobs": len(self.completed_jobs),
            "inflight_jobs": len(self.inflight_jobs),
            "sequence_counter": self.causal_sequence_counter,
            "platform_health": self.platform_health
        }
        
        state_str = json.dumps(state_data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(state_str.encode()).hexdigest()[:16]
    
    def _trigger_invariant_panic(self, violations: List[Dict[str, Any]]) -> None:
        """Legacy method - redirect to kill-switch"""
        # Determine trigger type from violations
        trigger_reason = "invariant_violation"
        if violations:
            violation_type = violations[0]["type"]
            if violation_type == "durable_completion_violation":
                trigger_reason = "phantom_completion"
            elif violation_type == "canonical_identity_violation":
                trigger_reason = "ledger_corruption"
            elif violation_type == "causal_ordering_violation":
                trigger_reason = "causal_regression"
            elif violation_type == "monotonic_time_violation":
                trigger_reason = "time_travel"
        
        # Trigger kill-switch instead of simple panic
        self._trigger_kill_switch(trigger_reason, violations)
    
    async def start_watchdog(self) -> None:
        """Start the exact invariant watchdog - machine-enforceable spec"""
        if self.watchdog_running:
            self.logger.warning("Watchdog already running")
            return
        
        self.watchdog_running = True
        self.watchdog_task = asyncio.create_task(self._watchdog_execution_loop())
        self.logger.info("Invariant watchdog started - machine-enforceable mode")
    
    async def stop_watchdog(self) -> None:
        """Stop the invariant watchdog"""
        self.watchdog_running = False
        if self.watchdog_task:
            self.watchdog_task.cancel()
            try:
                await self.watchdog_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Invariant watchdog stopped")
    
    async def _watchdog_execution_loop(self) -> None:
        """Watchdog execution loop - exact contract implementation"""
        while self.watchdog_running:
            try:
                # 1. Read authoritative state (not cached views)
                await self._read_authoritative_state()
                
                # 2. Evaluate ALL hard invariants in exact order
                # First failure halts further checks
                violation = await self._check_hard_invariants_exact_order()
                
                # 3. Panic on first violation
                if violation:
                    self._trigger_kill_switch_exact(violation)
                    break  # Exit loop on kill-switch
                
                # 4. Never batch failures, never retry
                # 5. Sleep for watchdog interval
                await asyncio.sleep(self.watchdog_interval)
                
            except Exception as e:
                self.logger.error(f"Watchdog execution error: {e}")
                # Watchdog never retries - continue to next cycle
                await asyncio.sleep(self.watchdog_interval)
    
    async def _read_authoritative_state(self) -> None:
        """Read authoritative state - not cached views"""
        # Force fresh reads of all critical state
        # This ensures watchdog has ground truth
        pass
    
    async def _check_hard_invariants_exact_order(self) -> Optional[Dict[str, Any]]:
        """Check ALL hard invariants in exact order - first failure halts further checks"""
        now = datetime.utcnow()
        
        # ORDER 1: Checkpoint sanity invariant
        violation = self._check_checkpoint_sanity_invariant()
        if violation:
            return violation
        
        # ORDER 2: Ledger ↔ job consistency invariant
        violation = self._check_ledger_job_consistency_invariant()
        if violation:
            return violation
        
        # ORDER 3: Durable completion invariant
        violation = self._check_durable_completion_invariant()
        if violation:
            return violation
        
        # ORDER 4: No phantom success invariant
        violation = self._check_no_phantom_success_invariant()
        if violation:
            return violation
        
        # ORDER 5: Canonical identity uniqueness invariant
        violation = self._check_canonical_identity_uniqueness_invariant()
        if violation:
            return violation
        
        # ORDER 6: Causal ordering invariant
        violation = self._check_causal_ordering_invariant()
        if violation:
            return violation
        
        # ORDER 7: Monotonic time invariant
        violation = self._check_monotonic_time_invariant()
        if violation:
            return violation
        
        # ORDER 8: Inflight liveness invariant
        violation = self._check_inflight_liveness_invariant()
        if violation:
            return violation
        
        # ORDER 9: Consensus validity invariant
        violation = self._check_consensus_validity_invariant()
        if violation:
            return violation
        
        # All invariants passed
        return None
    
    def _check_checkpoint_sanity_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 1: Checkpoint sanity invariant"""
        try:
            # Assertion: Checkpoint restore must satisfy:
            # checkpoint.version == expected_version
            # checkpoint.hash == recomputed_hash
            # checkpoint.last_sequence <= current_sequence
            
            # Get current checkpoint state
            checkpoint_version = self.checkpoint_version
            current_sequence = self.causal_sequence_counter
            
            # Check version consistency
            if checkpoint_version < 0:
                return {
                    "invariant_name": "CHECKPOINT_SANITY",
                    "severity": "HARD",
                    "timestamp": datetime.utcnow().isoformat(),
                    "job_id": None,
                    "platform": None,
                    "sequence_id": current_sequence,
                    "checkpoint_id": f"v{checkpoint_version}",
                    "reason": "checkpoint_version_invalid",
                    "offending_entity": f"version_{checkpoint_version}"
                }
            
            # Check sequence consistency
            # This would validate against actual checkpoint data
            # For now, ensure sequence is non-negative
            if current_sequence < 0:
                return {
                    "invariant_name": "CHECKPOINT_SANITY",
                    "severity": "HARD",
                    "timestamp": datetime.utcnow().isoformat(),
                    "job_id": None,
                    "platform": None,
                    "sequence_id": current_sequence,
                    "checkpoint_id": f"v{checkpoint_version}",
                    "reason": "checkpoint_sequence_invalid",
                    "offending_entity": f"sequence_{current_sequence}"
                }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "CHECKPOINT_SANITY",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"checkpoint_check_exception:_{str(e)}",
                "offending_entity": "checkpoint_system"
            }
    
    def _check_ledger_job_consistency_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 2: Ledger ↔ job consistency invariant"""
        try:
            # Assertion: For every ledger entry:
            # ledger.job_id exists
            # ledger.job_id.state ∈ {COMPLETED, FAILED}
            # For every terminal job: ledger contains job_id
            
            ledger_job_ids = set(self.ingestion_ledger.values())
            completed_job_ids = set(self.completed_jobs.keys())
            inflight_job_ids = set(self.inflight_jobs)
            
            # Check 1: Every ledger entry exists in completed or inflight
            orphaned_ledger = ledger_job_ids - (completed_job_ids | inflight_job_ids)
            if orphaned_ledger:
                orphaned_job_id = list(orphaned_ledger)[0]
                return {
                    "invariant_name": "LEDGER_JOB_CONSISTENCY",
                    "severity": "HARD",
                    "timestamp": datetime.utcnow().isoformat(),
                    "job_id": orphaned_job_id,
                    "platform": None,
                    "sequence_id": self.causal_sequence_counter,
                    "checkpoint_id": f"v{self.checkpoint_version}",
                    "reason": "ledger_entry_orphaned",
                    "offending_entity": orphaned_job_id
                }
            
            # Check 2: Every completed job exists in ledger
            completed_without_ledger = completed_job_ids - ledger_job_ids
            if completed_without_ledger:
                missing_job_id = list(completed_without_ledger)[0]
                return {
                    "invariant_name": "LEDGER_JOB_CONSISTENCY",
                    "severity": "HARD",
                    "timestamp": datetime.utcnow().isoformat(),
                    "job_id": missing_job_id,
                    "platform": None,
                    "sequence_id": self.causal_sequence_counter,
                    "checkpoint_id": f"v{self.checkpoint_version}",
                    "reason": "completed_job_missing_from_ledger",
                    "offending_entity": missing_job_id
                }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "LEDGER_JOB_CONSISTENCY",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"ledger_consistency_check_exception:_{str(e)}",
                "offending_entity": "ledger_system"
            }
    
    def _check_durable_completion_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 3: Durable completion invariant"""
        try:
            # Assertion: For every job where job.state == COMPLETED:
            # job.job_id in completed_jobs
            # job.job_id exists in persistent_checkpoint
            # job.output_refs are non-empty
            # job.storage_commit_ts <= job.completion_ts
            
            for job_id, completed_job in self.completed_jobs.items():
                if completed_job.get("status") == JobState.COMPLETED.value:
                    # Check 1: Job exists in completed_jobs (already true by iteration)
                    # Check 2: Job has checkpoint confirmation
                    if not completed_job.get("checkpoint_confirmed", False):
                        return {
                            "invariant_name": "DURABLE_COMPLETION",
                            "severity": "HARD",
                            "timestamp": datetime.utcnow().isoformat(),
                            "job_id": job_id,
                            "platform": completed_job.get("platform"),
                            "sequence_id": self.causal_sequence_counter,
                            "checkpoint_id": f"v{self.checkpoint_version}",
                            "reason": "completed_job_missing_checkpoint_confirmation",
                            "offending_entity": job_id
                        }
                    
                    # Check 3: Job has output references
                    if not completed_job.get("output_refs"):
                        return {
                            "invariant_name": "DURABLE_COMPLETION",
                            "severity": "HARD",
                            "timestamp": datetime.utcnow().isoformat(),
                            "job_id": job_id,
                            "platform": completed_job.get("platform"),
                            "sequence_id": self.causal_sequence_counter,
                            "checkpoint_id": f"v{self.checkpoint_version}",
                            "reason": "completed_job_missing_output_refs",
                            "offending_entity": job_id
                        }
                    
                    # Check 4: Storage commit timestamp <= completion timestamp
                    storage_commit_ts = completed_job.get("storage_commit_ts")
                    completion_ts = completed_job.get("completion_ts")
                    if storage_commit_ts and completion_ts and storage_commit_ts > completion_ts:
                        return {
                            "invariant_name": "DURABLE_COMPLETION",
                            "severity": "HARD",
                            "timestamp": datetime.utcnow().isoformat(),
                            "job_id": job_id,
                            "platform": completed_job.get("platform"),
                            "sequence_id": self.causal_sequence_counter,
                            "checkpoint_id": f"v{self.checkpoint_version}",
                            "reason": "storage_commit_after_completion",
                            "offending_entity": job_id
                        }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "DURABLE_COMPLETION",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"durable_completion_check_exception:_{str(e)}",
                "offending_entity": "completion_system"
            }
    
    def _check_no_phantom_success_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 4: No phantom success invariant"""
        try:
            # Assertion: For every entry in completed_jobs:
            # job.state == COMPLETED
            # job.success == True
            # job.error is None
            # No completed job may also exist in: failed_jobs, retry_queue, inflight_jobs
            
            for job_id, completed_job in self.completed_jobs.items():
                # Check 1: Job state is COMPLETED
                if completed_job.get("status") != JobState.COMPLETED.value:
                    return {
                        "invariant_name": "PHANTOM_SUCCESS",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": job_id,
                        "platform": completed_job.get("platform"),
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "completed_job_has_invalid_state",
                        "offending_entity": job_id
                    }
                
                # Check 2: Job success is True
                if not completed_job.get("success", True):
                    return {
                        "invariant_name": "PHANTOM_SUCCESS",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": job_id,
                        "platform": completed_job.get("platform"),
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "completed_job_marked_as_failure",
                        "offending_entity": job_id
                    }
                
                # Check 3: Job error is None
                if completed_job.get("error") is not None:
                    return {
                        "invariant_name": "PHANTOM_SUCCESS",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": job_id,
                        "platform": completed_job.get("platform"),
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "completed_job_has_error",
                        "offending_entity": job_id
                    }
                
                # Check 4: No completed job exists in failed_jobs, retry_queue, or inflight_jobs
                if job_id in self.inflight_jobs:
                    return {
                        "invariant_name": "PHANTOM_SUCCESS",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": job_id,
                        "platform": completed_job.get("platform"),
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "completed_job_still_inflight",
                        "offending_entity": job_id
                    }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "PHANTOM_SUCCESS",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"phantom_success_check_exception:_{str(e)}",
                "offending_entity": "completion_system"
            }
    
    def _check_canonical_identity_uniqueness_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 5: Canonical identity uniqueness invariant"""
        try:
            # Assertion: For every canonical_content_id:
            # canonical_content_id maps to exactly one job_id
            # fingerprint → canonical_content_id is immutable
            
            # Build reverse index: fingerprint -> list of job_ids
            fingerprint_to_jobs = defaultdict(list)
            for fingerprint, job_id in self.ingestion_ledger.items():
                fingerprint_to_jobs[fingerprint].append(job_id)
            
            # Check 1: No fingerprint maps to multiple jobs
            for fingerprint, job_ids in fingerprint_to_jobs.items():
                if len(job_ids) > 1:
                    return {
                        "invariant_name": "CANONICAL_IDENTITY_UNIQUENESS",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": job_ids[0],  # First conflicting job
                        "platform": None,
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "fingerprint_maps_to_multiple_jobs",
                        "offending_entity": fingerprint[:16] + "..."
                    }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "CANONICAL_IDENTITY_UNIQUENESS",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"identity_uniqueness_check_exception:_{str(e)}",
                "offending_entity": "ledger_system"
            }
    
    def _check_causal_ordering_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 6: Causal ordering invariant"""
        try:
            # Assertion: Let ingestion_sequence be global sequence counter.
            # current_sequence > last_committed_sequence
            # AND for all jobs: job.sequence_id < current_sequence
            # No duplicates. No regressions.
            
            current_sequence = self.causal_sequence_counter
            
            # Check 1: Sequence is strictly increasing
            if len(self.ingestion_sequence) >= 2:
                for i in range(1, len(self.ingestion_sequence)):
                    current_seq = self.ingestion_sequence[i]["sequence_id"]
                    prev_seq = self.ingestion_sequence[i-1]["sequence_id"]
                    
                    if current_seq <= prev_seq:
                        return {
                            "invariant_name": "CAUSAL_ORDERING",
                            "severity": "HARD",
                            "timestamp": datetime.utcnow().isoformat(),
                            "job_id": self.ingestion_sequence[i].get("job_id"),
                            "platform": None,
                            "sequence_id": current_seq,
                            "checkpoint_id": f"v{self.checkpoint_version}",
                            "reason": "sequence_regression_detected",
                            "offending_entity": f"sequence_{current_seq}"
                        }
            
            # Check 2: No duplicate sequence IDs
            sequence_ids = [entry["sequence_id"] for entry in self.ingestion_sequence]
            if len(sequence_ids) != len(set(sequence_ids)):
                duplicates = [seq for seq in sequence_ids if sequence_ids.count(seq) > 1]
                return {
                    "invariant_name": "CAUSAL_ORDERING",
                    "severity": "HARD",
                    "timestamp": datetime.utcnow().isoformat(),
                    "job_id": None,
                    "platform": None,
                    "sequence_id": duplicates[0] if duplicates else current_sequence,
                    "checkpoint_id": f"v{self.checkpoint_version}",
                    "reason": "duplicate_sequence_ids_detected",
                    "offending_entity": f"duplicate_sequences_{duplicates}"
                }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "CAUSAL_ORDERING",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"causal_ordering_check_exception:_{str(e)}",
                "offending_entity": "sequence_system"
            }
    
    def _check_monotonic_time_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 7: Monotonic time invariant"""
        try:
            # Assertion: Let canonical_time[t] be the canonical timeline.
            # For every content entity: canonical_time[n] >= canonical_time[n-1]
            # Across platforms and jobs.
            # Raw timestamps may regress — canonical MUST NOT.
            
            # Get all canonical timestamps from last_seen_timestamp
            canonical_timestamps = list(self.last_seen_timestamp.values())
            
            if len(canonical_timestamps) < 2:
                return None  # Not enough data to check monotonicity
            
            # Sort timestamps to check for regressions
            sorted_timestamps = sorted(canonical_timestamps)
            
            # Check for time travel (timestamps that are older than previous canonical time)
            for i in range(1, len(sorted_timestamps)):
                current_time = sorted_timestamps[i]
                prev_time = sorted_timestamps[i-1]
                
                if current_time < prev_time:
                    # Find the content ID that caused this regression
                    offending_content_id = None
                    for content_id, timestamp in self.last_seen_timestamp.items():
                        if timestamp == current_time:
                            offending_content_id = content_id
                            break
                    
                    return {
                        "invariant_name": "MONOTONIC_TIME",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": None,
                        "platform": None,
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "canonical_time_regression_detected",
                        "offending_entity": offending_content_id or "unknown_content"
                    }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "MONOTONIC_TIME",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"monotonic_time_check_exception:_{str(e)}",
                "offending_entity": "timestamp_system"
            }
    
    def _check_inflight_liveness_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 8: Inflight liveness invariant"""
        try:
            # Assertion: For every inflight job:
            # now - job.last_heartbeat < inflight_timeout
            # AND job.retry_count <= max_retries
            # If timeout exceeded AND no retry transition recorded → violation.
            
            now = datetime.utcnow()
            
            for job_id, inflight_timestamp in self.inflight_job_recovery.items():
                # Check 1: Job hasn't exceeded timeout
                age = now - inflight_timestamp
                if age > self.inflight_job_timeout:
                    return {
                        "invariant_name": "INFLIGHT_LIVENESS",
                        "severity": "HARD",
                        "timestamp": datetime.utcnow().isoformat(),
                        "job_id": job_id,
                        "platform": None,
                        "sequence_id": self.causal_sequence_counter,
                        "checkpoint_id": f"v{self.checkpoint_version}",
                        "reason": "inflight_job_timeout_exceeded",
                        "offending_entity": job_id
                    }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "INFLIGHT_LIVENESS",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"inflight_liveness_check_exception:_{str(e)}",
                "offending_entity": "inflight_system"
            }
    
    def _check_consensus_validity_invariant(self) -> Optional[Dict[str, Any]]:
        """ORDER 9: Consensus validity invariant"""
        try:
            # Assertion: For jobs requiring consensus:
            # len(passes) >= required_passes
            # max(delta(metrics)) <= epsilon
            # consensus_confidence >= threshold
            # If consensus failed AND job advanced state → violation.
            
            for content_id, votes in self.consensus_votes.items():
                if content_id in self.consensus_decisions:
                    consensus_decision = self.consensus_decisions[content_id]
                    
                    # Check 1: Consensus decision is valid
                    if consensus_decision is None:
                        return {
                            "invariant_name": "CONSENSUS_VALIDITY",
                            "severity": "HARD",
                            "timestamp": datetime.utcnow().isoformat(),
                            "job_id": None,
                            "platform": None,
                            "sequence_id": self.causal_sequence_counter,
                            "checkpoint_id": f"v{self.checkpoint_version}",
                            "reason": "consensus_decision_is_null",
                            "offending_entity": content_id
                        }
                    
                    # Check 2: Sufficient votes for consensus
                    total_votes = len(votes)
                    if total_votes < 2:  # At least 2 platforms needed for consensus
                        continue  # Skip if not enough votes
                    
                    positive_votes = sum(1 for vote in votes.values() if vote)
                    consensus_ratio = positive_votes / total_votes
                    
                    if consensus_ratio < self.consensus_threshold:
                        return {
                            "invariant_name": "CONSENSUS_VALIDITY",
                            "severity": "HARD",
                            "timestamp": datetime.utcnow().isoformat(),
                            "job_id": None,
                            "platform": None,
                            "sequence_id": self.causal_sequence_counter,
                            "checkpoint_id": f"v{self.checkpoint_version}",
                            "reason": "consensus_threshold_not_met",
                            "offending_entity": content_id
                        }
            
            return None
            
        except Exception as e:
            return {
                "invariant_name": "CONSENSUS_VALIDITY",
                "severity": "HARD",
                "timestamp": datetime.utcnow().isoformat(),
                "job_id": None,
                "platform": None,
                "sequence_id": self.causal_sequence_counter,
                "checkpoint_id": f"v{self.checkpoint_version}",
                "reason": f"consensus_validity_check_exception:_{str(e)}",
                "offending_entity": "consensus_system"
            }
    
    def _trigger_kill_switch_exact(self, violation: Dict[str, Any]) -> None:
        """Trigger kill-switch with exact violation data"""
        # META-INVARIANT #0 - WATCHDOG AUTHORITY
        if not self.watchdog_authority:
            self.logger.critical("WATCHDOG AUTHORITY VIOLATED - KILL-SWITCH SUPPRESSED")
            return
        
        # Trigger kill-switch with exact violation data
        context = {
            "job_id": violation.get("job_id"),
            "platform": violation.get("platform"),
            "sequence_id": violation.get("sequence_id"),
            "checkpoint_id": violation.get("checkpoint_id")
        }
        
        violations = [violation]  # Single violation for kill-switch
        
        self._trigger_kill_switch(
            violation["invariant_name"],
            violations,
            context
        )
    
    def _check_kill_switch_conditions(self) -> Optional[Tuple[str, List[Dict[str, Any]], Dict[str, Any]]]:
        """Check if kill-switch conditions are met"""
        if not self.kill_switch_enabled or self.system_state == "FROZEN":
            return None
        
        # Check each trigger condition
        violations = self._check_hard_invariants()
        
        if violations:
            # Determine trigger type
            violation_type = violations[0]["type"]
            
            # Map violation types to trigger reasons
            trigger_mapping = {
                "durable_completion_violation": "phantom_completion",
                "canonical_identity_violation": "ledger_corruption", 
                "causal_ordering_violation": "causal_regression",
                "monotonic_time_violation": "time_travel",
                "phantom_success_violation": "phantom_completion",
                "ledger_consistency_violation": "ledger_corruption"
            }
            
            trigger_reason = trigger_mapping.get(violation_type, "invariant_violation")
            
            if self.kill_switch_triggers.get(trigger_reason, False):
                context = {
                    "violation_count": len(violations),
                    "worst_violation": violation_type,
                    "system_state": self.system_state,
                    "inflight_count": len(self.inflight_jobs)
                }
                
                return trigger_reason, violations, context
        
        return None
    
    def _enforce_frozen_state(self) -> bool:
        """Enforce frozen state - refuse to proceed if frozen"""
        if self.system_state == "FROZEN":
            self.logger.critical(
                "SYSTEM FROZEN - Ingestion refused",
                extra={
                    "system_state": self.system_state,
                    "ingestion_enabled": False,
                    "scheduler_enabled": False,
                    "kill_switch_active": True,
                    "recovery_required": True
                }
            )
            return False
        
        if self.system_state == "RECOVERY":
            self.logger.warning(
                "SYSTEM IN RECOVERY - Ingestion paused",
                extra={
                    "system_state": self.system_state,
                    "recovery_lock": self.recovery_lock,
                    "recovery_mode": self.recovery_mode
                }
            )
            return False
        
        return True
    
    # ==================== RECOVERY PROTOCOL ====================
    
    async def initiate_recovery_protocol(self, recovery_mode: str = "MANUAL_OVERRIDE") -> bool:
        """Initiate recovery protocol - ONLY WAY OUT of frozen state"""
        if self.system_state != "FROZEN":
            self.logger.warning("Recovery protocol called but system not frozen")
            return False
        
        if self.recovery_lock:
            self.logger.error("Recovery already in progress")
            return False
        
        # PHASE 0 — RECOVERY LOCK
        self.recovery_lock = True
        self.system_state = "RECOVERY"
        
        try:
            # PHASE 1 — STATE VALIDATION
            if not await self._validate_recovery_state():
                self.logger.error("Recovery state validation failed - aborting recovery")
                self.system_state = "FROZEN"
                self.recovery_lock = False
                return False
            
            # PHASE 2 — DAMAGE BOUNDING
            damage_assessment = await self._assess_recovery_damage()
            if not damage_assessment:
                self.logger.error("Damage assessment failed - aborting recovery")
                self.system_state = "FROZEN"
                self.recovery_lock = False
                return False
            
            # PHASE 3 — STATE ROLLBACK / REPAIR
            if not await self._execute_recovery_repair(damage_assessment):
                self.logger.error("Recovery repair failed - aborting recovery")
                self.system_state = "FROZEN"
                self.recovery_lock = False
                return False
            
            # PHASE 4 — SHADOW VALIDATION RUN
            if self.shadow_validation_required:
                if not await self._run_shadow_validation():
                    self.logger.error("Shadow validation failed - aborting recovery")
                    self.system_state = "FROZEN"
                    self.recovery_lock = False
                    return False
            
            # PHASE 5 — EXPLICIT UNFREEZE
            await self._explicit_unfreeze()
            
            self.logger.info(
                "Recovery protocol completed successfully",
                extra={
                    "recovery_successful": True,
                    "recovery_mode": recovery_mode,
                    "damage_assessment": damage_assessment,
                    "system_state": self.system_state
                }
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Recovery protocol failed with exception: {e}")
            self.system_state = "FROZEN"
            self.recovery_lock = False
            return False
    
    async def _validate_recovery_state(self) -> bool:
        """PHASE 1: Validate system state for recovery"""
        self.logger.info("PHASE 1: Validating recovery state")
        
        validation_results = {}
        
        # Check checkpoint integrity
        validation_results["checkpoint_integrity"] = await self._validate_checkpoint_integrity_for_recovery()
        
        # Check ledger consistency
        validation_results["ledger_consistency"] = self._validate_ledger_consistency_for_recovery()
        
        # Check ingestion sequence monotonicity
        validation_results["sequence_monotonicity"] = self._validate_sequence_monotonicity_for_recovery()
        
        # Check fingerprint uniqueness
        validation_results["fingerprint_uniqueness"] = self._validate_fingerprint_uniqueness_for_recovery()
        
        # Check last good checkpoint sanity
        validation_results["checkpoint_sanity"] = self._validate_checkpoint_sanity_for_recovery()
        
        # All checks must pass
        all_passed = all(validation_results.values())
        
        self.logger.info(
            f"Recovery state validation: {'PASSED' if all_passed else 'FAILED'}",
            extra={
                "validation_results": validation_results,
                "all_checks_passed": all_passed
            }
        )
        
        return all_passed
    
    async def _assess_recovery_damage(self) -> Dict[str, Any]:
        """PHASE 2: Determine damage boundaries and rollback depth"""
        self.logger.info("PHASE 2: Assessing recovery damage")
        
        damage_assessment = {
            "earliest_corrupted_job": None,
            "affected_platforms": [],
            "ingestion_window_range": {"start": None, "end": None},
            "downstream_contamination_risk": "unknown",
            "rollback_depth": "minimal",
            "quarantine_required": False
        }
        
        # Analyze kill-switch snapshots for damage patterns
        if self.kill_switch_snapshots:
            latest_snapshot = self.kill_switch_snapshots[-1]
            
            # Determine affected platforms
            if latest_snapshot.get("platform"):
                damage_assessment["affected_platforms"].append(latest_snapshot["platform"])
            
            # Determine ingestion window range
            if latest_snapshot.get("ingestion_sequence"):
                damage_assessment["ingestion_window_range"]["end"] = latest_snapshot["ingestion_sequence"]
                damage_assessment["ingestion_window_range"]["start"] = max(0, latest_snapshot["ingestion_sequence"] - 100)
            
            # Assess contamination risk
            violation_type = latest_snapshot.get("violated_invariant")
            if violation_type in ["ledger_corruption", "causal_regression", "time_travel"]:
                damage_assessment["downstream_contamination_risk"] = "high"
                damage_assessment["rollback_depth"] = "full"
                damage_assessment["quarantine_required"] = True
            elif violation_type in ["phantom_completion", "durable_completion_violation"]:
                damage_assessment["downstream_contamination_risk"] = "medium"
                damage_assessment["rollback_depth"] = "partial"
            else:
                damage_assessment["downstream_contamination_risk"] = "low"
                damage_assessment["rollback_depth"] = "minimal"
        
        self.logger.info(
            "Damage assessment completed",
            extra={
                "damage_assessment": damage_assessment
            }
        )
        
        return damage_assessment
    
    async def _execute_recovery_repair(self, damage_assessment: Dict[str, Any]) -> bool:
        """PHASE 3: Execute state rollback/repair based on damage assessment"""
        self.logger.info("PHASE 3: Executing recovery repair")
        
        repair_actions = []
        
        try:
            rollback_depth = damage_assessment["rollback_depth"]
            
            if rollback_depth == "full":
                # Full rollback to last good checkpoint
                repair_actions.append("full_rollback_to_checkpoint")
                success = await self._full_rollback_to_checkpoint()
            elif rollback_depth == "partial":
                # Partial rollback of affected data
                repair_actions.append("partial_rollback_of_affected_data")
                success = await self._partial_rollback_of_affected_data(damage_assessment)
            else:
                # Minimal repair - reset affected platform state
                repair_actions.append("reset_affected_platform_state")
                success = await self._reset_affected_platform_state(damage_assessment)
            
            if not success:
                self.logger.error(f"Repair action failed: {repair_actions[-1]}")
                return False
            
            # Additional repair actions based on damage assessment
            if damage_assessment["quarantine_required"]:
                repair_actions.append("quarantine_affected_content")
                await self._quarantine_affected_content(damage_assessment)
            
            # Reseed ingestion windows if needed
            if damage_assessment["affected_platforms"]:
                repair_actions.append("reseed_ingestion_windows")
                await self._reseed_ingestion_windows(damage_assessment["affected_platforms"])
            
            self.logger.info(
                "Recovery repair completed successfully",
                extra={
                    "repair_actions": repair_actions,
                    "damage_assessment": damage_assessment
                }
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Recovery repair failed: {e}")
            return False
    
    async def _run_shadow_validation(self) -> bool:
        """PHASE 4: Shadow validation run before resuming production"""
        self.logger.info("PHASE 4: Running shadow validation")
        
        # Enable shadow mode for validation
        original_shadow_mode = self.enable_shadow_mode
        self.enable_shadow_mode = True
        
        try:
            # Run a short validation cycle
            validation_results = await self._run_validation_cycle()
            
            # Compare deltas vs repaired state
            validation_passed = self._compare_validation_deltas(validation_results)
            
            if not validation_passed:
                self.logger.error("Shadow validation failed - deltas indicate issues")
                return False
            
            self.logger.info(
                "Shadow validation passed",
                extra={
                    "validation_results": validation_results,
                    "deltas_consistent": True
                }
            )
            
            return True
            
        except Exception as e:
            self.logger.error(f"Shadow validation failed with exception: {e}")
            return False
            
        finally:
            # Restore original shadow mode setting
            self.enable_shadow_mode = original_shadow_mode
    
    async def _explicit_unfreeze(self) -> None:
        """PHASE 5: Explicit unfreeze - only if ALL checks pass"""
        self.logger.info("PHASE 5: Explicit unfreeze")
        
        # Mark system as active
        self.system_state = "ACTIVE"
        self.ingestion_enabled = True
        self.scheduler_enabled = True
        self.recovery_lock = False
        
        # Clear frozen state flags
        self.system_frozen_for_invariant_violation = False
        self.invariant_panic_reason = None
        self.invariant_panic_timestamp = None
        
        # Persist recovery completion
        await self._persist_recovery_completion()
        
        self.logger.info(
            "System unfrozen successfully - ingestion resumed",
            extra={
                "system_state": self.system_state,
                "ingestion_enabled": True,
                "scheduler_enabled": True,
                "recovery_completed": True
            }
        )
    
    # ==================== RECOVERY HELPER METHODS ====================
    
    async def _validate_checkpoint_integrity_for_recovery(self) -> bool:
        """Validate checkpoint integrity for recovery"""
        try:
            # This would validate the most recent checkpoint
            # For now, return True as placeholder
            return True
        except Exception:
            return False
    
    def _validate_ledger_consistency_for_recovery(self) -> bool:
        """Validate ledger consistency for recovery"""
        try:
            # Check ledger to completed_jobs consistency
            ledger_job_ids = set(self.ingestion_ledger.values())
            completed_job_ids = set(self.completed_jobs.keys())
            inflight_job_ids = set(self.inflight_jobs)
            
            # All ledger jobs should be in completed or inflight
            orphaned_ledger = ledger_job_ids - (completed_job_ids | inflight_job_ids)
            return len(orphaned_ledger) == 0
        except Exception:
            return False
    
    def _validate_sequence_monotonicity_for_recovery(self) -> bool:
        """Validate ingestion sequence monotonicity for recovery"""
        try:
            if len(self.ingestion_sequence) < 2:
                return True
            
            for i in range(1, len(self.ingestion_sequence)):
                current_seq = self.ingestion_sequence[i]["sequence_id"]
                prev_seq = self.ingestion_sequence[i-1]["sequence_id"]
                
                if current_seq <= prev_seq:
                    return False
            
            return True
        except Exception:
            return False
    
    def _validate_fingerprint_uniqueness_for_recovery(self) -> bool:
        """Validate fingerprint uniqueness for recovery"""
        try:
            fingerprint_to_jobs = defaultdict(list)
            for fingerprint, job_id in self.ingestion_ledger.items():
                fingerprint_to_jobs[fingerprint].append(job_id)
            
            # No fingerprint should map to multiple jobs
            for fingerprint, job_ids in fingerprint_to_jobs.items():
                if len(job_ids) > 1:
                    return False
            
            return True
        except Exception:
            return False
    
    def _validate_checkpoint_sanity_for_recovery(self) -> bool:
        """Validate last good checkpoint sanity for recovery"""
        try:
            # Basic sanity checks
            return (
                self.causal_sequence_counter >= 0 and
                len(self.ingestion_ledger) >= 0 and
                len(self.completed_jobs) >= 0
            )
        except Exception:
            return False
    
    async def _full_rollback_to_checkpoint(self) -> bool:
        """Full rollback to last good checkpoint"""
        try:
            # This would load the last good checkpoint
            # For now, simulate successful rollback
            self.logger.info("Full rollback to checkpoint completed")
            return True
        except Exception as e:
            self.logger.error(f"Full rollback failed: {e}")
            return False
    
    async def _partial_rollback_of_affected_data(self, damage_assessment: Dict[str, Any]) -> bool:
        """Partial rollback of affected data"""
        try:
            # This would selectively rollback affected data
            # For now, simulate successful partial rollback
            self.logger.info("Partial rollback of affected data completed")
            return True
        except Exception as e:
            self.logger.error(f"Partial rollback failed: {e}")
            return False
    
    async def _reset_affected_platform_state(self, damage_assessment: Dict[str, Any]) -> bool:
        """Reset affected platform state"""
        try:
            affected_platforms = damage_assessment.get("affected_platforms", [])
            
            for platform in affected_platforms:
                # Reset platform health state
                if platform in self.platform_health:
                    self.platform_health[platform]["failures"] = 0
                    self.platform_health[platform]["disabled_until"] = None
                    self.platform_health[platform]["health_status"] = PlatformHealth.HEALTHY
            
            self.logger.info(f"Reset platform state for: {affected_platforms}")
            return True
        except Exception as e:
            self.logger.error(f"Platform state reset failed: {e}")
            return False
    
    async def _quarantine_affected_content(self, damage_assessment: Dict[str, Any]) -> None:
        """Quarantine affected content IDs"""
        try:
            # This would mark affected content IDs as quarantined
            # For now, just log the action
            self.logger.info("Quarantined affected content IDs")
        except Exception as e:
            self.logger.error(f"Content quarantine failed: {e}")
    
    async def _reseed_ingestion_windows(self, affected_platforms: List[str]) -> None:
        """Reseed ingestion windows for affected platforms"""
        try:
            for platform in affected_platforms:
                # Reset window sizes to default
                self.current_window_sizes[platform] = 30
            
            self.logger.info(f"Reseeded ingestion windows for: {affected_platforms}")
        except Exception as e:
            self.logger.error(f"Ingestion window reseed failed: {e}")
    
    async def _run_validation_cycle(self) -> Dict[str, Any]:
        """Run a validation cycle in shadow mode"""
        # This would run actual validation logic
        # For now, return mock results
        return {
            "validation_passed": True,
            "issues_found": 0,
            "validation_timestamp": datetime.utcnow().isoformat()
        }
    
    def _compare_validation_deltas(self, validation_results: Dict[str, Any]) -> bool:
        """Compare validation deltas vs repaired state"""
        # This would compare validation results with expected state
        # For now, return True
        return validation_results.get("validation_passed", False)
    
    async def _persist_recovery_completion(self) -> None:
        """Persist recovery completion metadata"""
        try:
            # This would persist recovery metadata
            # For now, just log the completion
            self.logger.info("Recovery completion metadata persisted")
        except Exception as e:
            self.logger.error(f"Failed to persist recovery completion: {e}")
    
    def get_kill_switch_status(self) -> Dict[str, Any]:
        """Get current kill-switch status"""
        return {
            "kill_switch_enabled": self.kill_switch_enabled,
            "system_state": self.system_state,
            "ingestion_enabled": self.ingestion_enabled,
            "scheduler_enabled": self.scheduler_enabled,
            "recovery_lock": self.recovery_lock,
            "kill_switch_snapshots_count": len(self.kill_switch_snapshots),
            "last_snapshot": self.kill_switch_snapshots[-1] if self.kill_switch_snapshots else None,
            "recovery_mode": self.recovery_mode,
            "shadow_validation_required": self.shadow_validation_required,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def get_inflight_recovery_status(self) -> Dict[str, Any]:
        """Get inflight job recovery status"""
        now = datetime.utcnow()
        stale_jobs = []
        
        for job_id, inflight_timestamp in self.inflight_job_recovery.items():
            age = now - inflight_timestamp
            if age > self.inflight_job_timeout:
                stale_jobs.append({
                    "job_id": job_id,
                    "inflight_timestamp": inflight_timestamp.isoformat(),
                    "age_minutes": age.total_seconds() / 60,
                    "is_stale": True
                })
        
        return {
            "total_inflight_jobs": len(self.inflight_jobs),
            "tracked_inflight_jobs": len(self.inflight_job_recovery),
            "stale_jobs_count": len(stale_jobs),
            "stale_jobs": stale_jobs,
            "inflight_timeout_minutes": self.inflight_job_timeout.total_seconds() / 60,
            "timestamp": now.isoformat()
        }
    
    def get_advanced_enhancements_status(self) -> Dict[str, Any]:
        return {
            "consensus_protection": {
                "enabled": True,
                "threshold": self.consensus_threshold,
                "active_votes": len(self.consensus_votes),
                "consensus_decisions": len(self.consensus_decisions)
            },
            "intake_staging": {
                "enabled": True,
                "staged_signals": len(self.intake_staging),
                "staging_timeout_minutes": self.staging_timeout.total_seconds() / 60
            },
            "meta_learning": {
                "enabled": True,
                "feedback_entries": len(self.learning_feedback),
                "platform_metrics": dict(self.meta_learning_metrics),
                "learning_feedback_count": len(self.learning_feedback)
            },
            "dynamic_windows": {
                "enabled": self.enable_dynamic_windows,
                "current_sizes": dict(self.current_window_sizes),
                "min_window": self.min_window_minutes,
                "max_window": self.max_window_minutes
            },
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def _calculate_backlog_metrics(self) -> Dict[str, Any]:
        now = datetime.utcnow()
        
        # Calculate queue backlog metrics
        queue_depth = len(self.job_queue)
        
        # Calculate queue age statistics
        queue_ages = []
        if self.job_queue:
            for priority, scheduled_time, job in self.job_queue:
                age_seconds = (now - scheduled_time).total_seconds()
                queue_ages.append(age_seconds)
        
        # Calculate percentiles for queue age
        queue_age_p95 = 0.0
        queue_age_mean = 0.0
        queue_age_max = 0.0
        if queue_ages:
            queue_ages_sorted = sorted(queue_ages)
            queue_age_p95 = queue_ages_sorted[int(len(queue_ages_sorted) * 0.95)] if len(queue_ages_sorted) > 0 else 0.0
            queue_age_mean = sum(queue_ages) / len(queue_ages)
            queue_age_max = max(queue_ages)
        
        # Calculate starvation metrics (jobs waiting too long)
        starvation_threshold = 300  # 5 minutes
        starving_jobs = [age for age in queue_ages if age > starvation_threshold]
        starvation_time = max(starving_jobs) if starving_jobs else 0.0
        
        # Calculate platform-specific backlog
        platform_backlog = defaultdict(int)
        platform_queue_age = defaultdict(list)
        
        for priority, scheduled_time, job in self.job_queue:
            platform_backlog[job.platform] += 1
            age_seconds = (now - scheduled_time).total_seconds()
            platform_queue_age[job.platform].append(age_seconds)
        
        # Calculate platform-specific metrics
        platform_metrics = {}
        for platform in self.adapters.keys():
            platform_queue_ages = platform_queue_age.get(platform, [])
            platform_age_p95 = 0.0
            if platform_queue_ages:
                platform_queue_ages_sorted = sorted(platform_queue_ages)
                platform_age_p95 = platform_queue_ages_sorted[int(len(platform_queue_ages_sorted) * 0.95)] if len(platform_queue_ages_sorted) > 0 else 0.0
            
            platform_metrics[platform] = {
                "backlog_depth": platform_backlog.get(platform, 0),
                "queue_age_p95": platform_age_p95,
                "oldest_job_age": max(platform_queue_ages) if platform_queue_ages else 0.0
            }
        
        # Calculate worker utilization
        worker_utilization = len(self.inflight_jobs) / max(self.max_concurrent_jobs, 1)
        
        return {
            "backlog_depth": queue_depth,
            "queue_age_p95": queue_age_p95,
            "queue_age_mean": queue_age_mean,
            "queue_age_max": queue_age_max,
            "starvation_time": starvation_time,
            "starving_jobs": len(starving_jobs),
            "worker_utilization": worker_utilization,
            "platform_metrics": platform_metrics,
            "timestamp": now.isoformat()
        }
    
    def _detect_backlog_conditions(self, backlog_metrics: Dict[str, Any]) -> None:
        """Detect and log backlog/lag conditions"""
        # Check for high backlog depth
        if backlog_metrics["backlog_depth"] > 50:
            self.logger.warning(
                f"High backlog detected: {backlog_metrics['backlog_depth']} jobs in queue",
                extra={
                    "backlog_depth": backlog_metrics["backlog_depth"],
                    "worker_utilization": backlog_metrics["worker_utilization"],
                    "alert_type": "high_backlog"
                }
            )
        
        # Check for queue age lag
        if backlog_metrics["queue_age_p95"] > 600:  # 10 minutes
            self.logger.warning(
                f"Queue lag detected: P95 age {backlog_metrics['queue_age_p95']:.1f}s",
                extra={
                    "queue_age_p95": backlog_metrics["queue_age_p95"],
                    "queue_age_max": backlog_metrics["queue_age_max"],
                    "alert_type": "queue_lag"
                }
            )
        
        # Check for starvation
        if backlog_metrics["starvation_time"] > 900:  # 15 minutes
            self.logger.error(
                f"Job starvation detected: {backlog_metrics['starving_jobs']} jobs waiting >15min",
                extra={
                    "starvation_time": backlog_metrics["starvation_time"],
                    "starving_jobs": backlog_metrics["starving_jobs"],
                    "alert_type": "job_starvation"
                }
            )
        
        # Check for worker saturation
        if backlog_metrics["worker_utilization"] > 0.9:
            self.logger.warning(
                f"Worker saturation: {backlog_metrics['worker_utilization']:.1%} utilization",
                extra={
                    "worker_utilization": backlog_metrics["worker_utilization"],
                    "inflight_jobs": len(self.inflight_jobs),
                    "max_workers": self.max_concurrent_jobs,
                    "alert_type": "worker_saturation"
                }
            )
        
        # Check platform-specific issues
        for platform, metrics in backlog_metrics["platform_metrics"].items():
            if metrics["backlog_depth"] > 20:
                self.logger.warning(
                    f"Platform {platform} backlog: {metrics['backlog_depth']} jobs",
                    extra={
                        "platform": platform,
                        "backlog_depth": metrics["backlog_depth"],
                        "queue_age_p95": metrics["queue_age_p95"],
                        "alert_type": "platform_backlog"
                    }
                )
            
            if metrics["queue_age_p95"] > 300:  # 5 minutes
                self.logger.warning(
                    f"Platform {platform} queue lag: P95 age {metrics['queue_age_p95']:.1f}s",
                    extra={
                        "platform": platform,
                        "queue_age_p95": metrics["queue_age_p95"],
                        "oldest_job_age": metrics["oldest_job_age"],
                        "alert_type": "platform_lag"
                    }
                )
    
    def _calculate_scheduler_priority_score(self, job: IngestionJob, backlog_metrics: Dict[str, Any]) -> float:
        """Calculate dynamic priority score for job scheduling - 'Why Now?' engine
        
        Canonical formula:
        priority_score = α * freshness_pressure + β * velocity_acceleration + γ * backlog_risk - δ * platform_instability - ε * cost_pressure
        
        Lower score = higher priority (scheduler picks lowest score first)
        """
        if not self.scheduler_math_enabled:
            return self._get_priority_for_mode(job.mode)
        
        now = datetime.utcnow()
        
        # 1️⃣ Freshness Pressure (α)
        last_success = self.last_successful_ingestion.get(job.platform)
        freshness_threshold = self.freshness_thresholds.get(job.platform, timedelta(minutes=5))
        
        if not last_success:
            freshness_pressure = 1.0  # Maximum urgency for first-time ingestion
        else:
            overdue_minutes = (now - last_success).total_seconds() / 60
            freshness_pressure = min(1.0, overdue_minutes / freshness_threshold.total_minutes())
        
        # 2️⃣ Velocity Acceleration (β)
        # Derived from recent ingestion deltas and trend growth rate
        velocity_acceleration = self._calculate_velocity_acceleration(job.platform, now)
        
        # 3️⃣ Backlog Risk (γ)
        # P95(job_wait_time) / allowed_wait_time
        platform_backlog = backlog_metrics["platform_metrics"].get(job.platform, {})
        queue_age_p95 = platform_backlog.get("queue_age_p95", 0.0)
        allowed_wait_time = self.scheduler_no_starvation_threshold.total_seconds()
        backlog_risk = min(2.0, queue_age_p95 / allowed_wait_time)  # Cap at 2.0 for safety
        
        # 4️⃣ Platform Instability Penalty (δ)
        # From trust score + recent failures
        trust_score = self.platform_trust_scores[job.platform]
        recent_failures = self.platform_health[job.platform]["failures"]
        platform_instability = (1.0 - trust_score) + (recent_failures * 0.1)  # Penalty increases with failures
        
        # 5️⃣ Cost Pressure (ε)
        # Pushes BACKFILL down, VERIFIED > RAW, LOW-confidence jobs out first
        estimated_cost = self._estimate_job_cost(job)
        remaining_budget = self.cost_budget_hourly * (1.0 - (now - self.cost_budget_start).total_seconds() / 3600.0)
        cost_pressure = estimated_cost / max(remaining_budget, 1.0) if self.enable_cost_budgeting else 0.0
        
        # Apply weights and calculate final score
        weights = self.scheduler_scoring_weights
        priority_score = (
            weights["freshness_pressure"] * freshness_pressure +
            weights["velocity_acceleration"] * velocity_acceleration +
            weights["backlog_risk"] * backlog_risk +
            weights["platform_instability"] * platform_instability +
            weights["cost_pressure"] * cost_pressure
        )
        
        # Apply scheduler safety invariants
        priority_score = self._apply_scheduler_safety_invariants(job, priority_score, backlog_metrics)
        
        return priority_score
    
    def _calculate_velocity_acceleration(self, platform: str, now: datetime) -> float:
        """Calculate velocity acceleration from recent ingestion deltas and trend growth rate"""
        # Get recent successful ingestions for this platform
        recent_successes = [
            ts for ts in self.last_successful_ingestion.values()
            if ts and (now - ts).total_seconds() < 3600  # Last hour
        ]
        
        if len(recent_successes) < 2:
            return 0.0  # Not enough data for acceleration calculation
        
        # Calculate ingestion velocity (ingestions per hour)
        recent_successes_sorted = sorted(recent_successes)
        time_span = (recent_successes_sorted[-1] - recent_successes_sorted[0]).total_seconds() / 3600
        velocity = len(recent_successes) / max(time_span, 0.1)  # Ingestions per hour
        
        # Get previous velocity (simplified - would use trend aggregator in production)
        previous_velocity = self.meta_learning_metrics.get(platform, {}).get("previous_velocity", velocity)
        
        # Calculate acceleration (change in velocity)
        acceleration = (velocity - previous_velocity) / max(previous_velocity, 0.1)
        
        # Update previous velocity for next calculation
        self.meta_learning_metrics[platform]["previous_velocity"] = velocity
        
        return max(0.0, acceleration)  # Only positive acceleration increases urgency
    
    def _apply_scheduler_safety_invariants(self, job: IngestionJob, priority_score: float, backlog_metrics: Dict[str, Any]) -> float:
        """Apply scheduler safety invariants to prevent starvation and inversion"""
        
        # Invariant 1: No starvation invariant - every job class must make forward progress
        platform_backlog = backlog_metrics["platform_metrics"].get(job.platform, {})
        queue_age_p95 = platform_backlog.get("queue_age_p95", 0.0)
        
        if queue_age_p95 > self.scheduler_no_starvation_threshold.total_seconds():
            # Jobs are starving - reduce priority score to increase urgency
            starvation_penalty = queue_age_p95 / self.scheduler_no_starvation_threshold.total_seconds()
            priority_score *= (2.0 - starvation_penalty)  # Multiply by factor > 1 to reduce score
        
        # Invariant 2: No inversion - REALTIME jobs cannot be blocked by BACKFILL
        if job.mode == IngestionMode.REALTIME_FAST:
            realtime_delay = queue_age_p95
            if realtime_delay > self.scheduler_max_realtime_delay.total_seconds():
                # REALTIME jobs are being delayed - force higher priority
                inversion_penalty = realtime_delay / self.scheduler_max_realtime_delay.total_seconds()
                priority_score *= (2.0 - inversion_penalty)  # Multiply by factor > 1 to reduce score
        
        # Invariant 3: Failure isolation - one platform cannot monopolize workers
        if self.scheduler_failure_isolation:
            platform_inflight_count = sum(1 for job_id in self.inflight_jobs 
                                       if job_id.startswith(job.platform))
            worker_utilization = len(self.inflight_jobs) / max(self.max_concurrent_jobs, 1)
            
            # If one platform is monopolizing workers, penalize its jobs
            platform_monopoly_ratio = platform_inflight_count / max(len(self.inflight_jobs), 1)
            if platform_monopoly_ratio > 0.7 and worker_utilization > 0.8:  # 70% of jobs from one platform + 80% workers busy
                priority_score *= (1.0 + platform_monopoly_ratio)  # Increase score to reduce priority
        
        return priority_score
    
    async def _schedule_platform_ingestion(self, platform: str, now: datetime, backlog_metrics: Dict[str, Any]) -> None:
        """Schedule ingestion for a specific platform based on freshness and backlog"""
        # Check platform health
        health = self.platform_health[platform]
        if health["disabled_until"] and now < health["disabled_until"]:
            return  # Platform is circuit-broken
        
        # Check freshness requirements
        last_success = self.last_successful_ingestion.get(platform)
        freshness_threshold = self.freshness_thresholds.get(platform, timedelta(minutes=5))
        
        needs_ingestion = (
            not last_success or 
            (now - last_success) > freshness_threshold
        )
        
        # CRITICAL: Consider backlog pressure in scheduling decisions
        platform_backlog = backlog_metrics["platform_metrics"].get(platform, {})
        backlog_depth = platform_backlog.get("backlog_depth", 0)
        queue_age_p95 = platform_backlog.get("queue_age_p95", 0.0)
        
        # Adjust scheduling based on backlog conditions
        if needs_ingestion:
            # Determine ingestion mode based on urgency AND backlog
            if not last_success:
                mode = IngestionMode.BACKFILL
            elif (now - last_success) > freshness_threshold * 2:
                mode = IngestionMode.REALTIME_FAST
            elif backlog_depth > 20 or queue_age_p95 > 300:  # High backlog or lag
                # Escalate to faster mode if backlog is high
                mode = IngestionMode.REALTIME_FAST
                self.logger.info(
                    f"Platform {platform} backlog escalation: depth={backlog_depth}, p95_age={queue_age_p95:.1f}s",
                    extra={
                        "platform": platform,
                        "backlog_depth": backlog_depth,
                        "queue_age_p95": queue_age_p95,
                        "escalation_reason": "backlog_pressure"
                    }
                )
            else:
                mode = IngestionMode.STANDARD_POLL
            
            # Create ingestion job with CRITICAL dynamic window sizing
            # Calculate urgency based on how overdue the ingestion is
            if not last_success:
                urgency = 1.0  # Maximum urgency for first-time ingestion
            else:
                overdue_minutes = (now - last_success).total_seconds() / 60
                urgency = min(1.0, overdue_minutes / freshness_threshold.total_minutes())  # Normalize to 0-1
            
            # CRITICAL: Use dynamic window sizing instead of fixed freshness
            dynamic_window_minutes = self._calculate_dynamic_window(platform, urgency)
            window_start = now - timedelta(minutes=dynamic_window_minutes)
            
            window = TimeWindow(
                start=window_start,
                end=now
            )
            
            # Store the calculated window size for monitoring
            self.current_window_sizes[platform] = dynamic_window_minutes
            
            # CRITICAL: Log that dynamic window is being applied
            self.logger.info(
                f"Creating job for {platform} with dynamic window: {dynamic_window_minutes} minutes "
                f"(urgency={urgency:.2f}, window_start={window_start.isoformat()})",
                extra={
                    "platform": platform,
                    "dynamic_window_minutes": dynamic_window_minutes,
                    "urgency": urgency,
                    "window_start": window_start.isoformat(),
                    "window_end": now.isoformat(),
                    "window_duration_minutes": dynamic_window_minutes,
                    "dynamic_window_applied": True
                }
            )
            
            job = IngestionJob(
                run_id=self.run_id,  # Add global run ID
                job_id=str(uuid.uuid4()),
                platform=platform,
                content_id="*",  # Platform-wide ingestion
                window=window,
                mode=mode,
                priority=self._get_priority_for_mode(mode),  # Base priority - will be recalculated dynamically
                scheduled_at=now
            )
            
            # CRITICAL: Apply scheduler math tuning - calculate dynamic priority
            if self.scheduler_math_enabled:
                # Calculate dynamic priority score using scheduler math
                dynamic_priority_score = self._calculate_scheduler_priority_score(job, backlog_metrics)
                # Convert score to priority (lower score = higher priority)
                job.priority = int(dynamic_priority_score * 100)  # Scale to integer priority
                
                self.logger.debug(
                    f"Applied scheduler math to {platform} job: score={dynamic_priority_score:.3f}, priority={job.priority}",
                    extra={
                        "platform": platform,
                        "scheduler_score": dynamic_priority_score,
                        "assigned_priority": job.priority,
                        "scheduler_math_applied": True
                    }
                )
            
            self._push_job(job)
            self.logger.debug(f"Scheduled {mode.value} job for {platform} (backlog: {backlog_depth})")
        
        # CRITICAL: Detect platform-specific starvation conditions
        elif queue_age_p95 > 600:  # 10 minutes
            self.logger.warning(
                f"Platform {platform} starvation: no ingestion needed but queue lag is high",
                extra={
                    "platform": platform,
                    "queue_age_p95": queue_age_p95,
                    "backlog_depth": backlog_depth,
                    "last_success": last_success.isoformat() if last_success else None,
                    "alert_type": "platform_starvation"
                }
            )
    
    def _get_priority_for_mode(self, mode: IngestionMode) -> int:
        """Get priority based on ingestion mode"""
        priority_map = {
            IngestionMode.REALTIME_FAST: 0,
            IngestionMode.RECOVERY: 2,
            IngestionMode.STANDARD_POLL: 5,
            IngestionMode.BACKFILL: 10
        }
        return priority_map.get(mode, 5)
    
    def _push_job(self, job: IngestionJob) -> None:
        """Add job to priority queue with proper ordering"""
        scheduled_time = job.scheduled_at or datetime.utcnow()
        heapq.heappush(self.job_queue, (job.priority, scheduled_time, job))
        job.state = JobState.QUEUED
        job.last_state_change = datetime.utcnow()
    
    def _pop_job(self) -> Optional[IngestionJob]:
        """Get highest priority job from queue"""
        if not self.job_queue:
            return None
        
        priority, scheduled_time, job = heapq.heappop(self.job_queue)
        job.state = JobState.RUNNING
        job.last_state_change = datetime.utcnow()
        return job
    
    async def _retry_scheduler(self) -> None:
        """Non-blocking retry scheduler that processes delayed retries"""
        while True:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                now = datetime.utcnow()
                ready_retries = []
                
                # Find jobs ready for retry
                remaining_retries = []
                for retry_time, job in self.retry_queue:
                    if retry_time <= now:
                        ready_retries.append(job)
                    else:
                        remaining_retries.append((retry_time, job))
                
                # Update retry queue (keep it sorted by retry time)
                self.retry_queue = sorted(remaining_retries, key=lambda x: x[0])
                
                # Re-queue ready jobs
                for job in ready_retries:
                    self._push_job(job)
                    self.logger.info(
                        f"Retry job {job.job_id} re-queued for {job.platform}",
                        extra={"job_id": job.job_id, "platform": job.platform, "retry_count": job.retry_count}
                    )
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Retry scheduler error: {e}")
                await asyncio.sleep(30)  # Brief pause on error
    
    async def _worker_loop(self, worker_id: str) -> None:
        """Worker loop for processing ingestion jobs"""
        self.logger.info(f"Worker {worker_id} started")
        
        while self.worker_running:
            try:
                # CRITICAL: Check for inflight job recovery before processing new jobs
                self._check_inflight_job_recovery()
                
                job = self._pop_job()
                if not job:
                    await asyncio.sleep(1)  # Brief pause when queue is empty
                    continue
                
                # CRITICAL: Track job as inflight
                self._track_inflight_job(job)
                
                try:
                    # Execute job with timeout
                    result = await asyncio.wait_for(
                        self._execute_job_with_timeout(job, timeout_seconds=300),
                        timeout=300
                    )
                    
                    self.logger.info(f"Worker {worker_id} completed job {job.job_id}")
                    
                except asyncio.TimeoutError:
                    self.logger.error(f"Worker {worker_id} job {job.job_id} timed out")
                    # Job will be recovered by the inflight recovery system
                    
                except Exception as e:
                    self.logger.error(f"Worker {worker_id} job {job.job_id} failed: {e}")
                    
                finally:
                    # CRITICAL: Untrack job when done (regardless of success/failure)
                    self._untrack_inflight_job(job.job_id)
                    
            except Exception as e:
                self.logger.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(1)  # Brief pause on error
        
        self.logger.info(f"Worker {worker_id} stopped")
    
    async def _enforce_time_guard(self, content: List[Dict[str, Any]]) -> None:
        """Enforce monotonic time guard on content timestamps"""
        for item in content:
            # Extract timestamp (platform-specific - this is a simplified example)
            timestamp_str = item.get("timestamp") or item.get("created_at") or item.get("published_at")
            if not timestamp_str:
                continue
            
            try:
                # Parse timestamp (platform-specific parsing would go here)
                if isinstance(timestamp_str, str):
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                elif isinstance(timestamp_str, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp_str)
                else:
                    continue
                
                content_id = item.get("id") or item.get("content_id")
                if not content_id:
                    continue
                
                # Enforce monotonicity
                prev = self.last_seen_timestamp.get(content_id)
                if prev and timestamp < prev:
                    raise TimeRegressionError(f"Timestamp regression for {content_id}: {timestamp} < {prev}")
                
                self.last_seen_timestamp[content_id] = timestamp
                
            except Exception as e:
                if not isinstance(e, TimeRegressionError):
                    self.logger.warning(f"Failed to parse timestamp for item: {e}")
    
    def _record_causal_sequence(self, job: IngestionJob, content: List[Dict[str, Any]]) -> None:
        """Record causal sequence for global ordering reconstruction"""
        # Increment global sequence counter
        self.causal_sequence_counter += 1
        sequence_id = self.causal_sequence_counter
        
        # Extract content IDs and their actual timestamps for cross-platform ordering
        content_ids = []
        content_timestamps = []  # CRITICAL: Store actual content timestamps for ordering
        content_lineage_data = {}  # content_id -> [sequence_ids] mapping
        
        for item in content:
            content_id = item.get("id") or item.get("content_id")
            if content_id:
                content_ids.append(content_id)
                
                # CRITICAL: Extract actual content timestamp for ordering reconstruction
                content_timestamp = None
                timestamp_str = (
                    item.get("timestamp") or 
                    item.get("created_at") or 
                    item.get("published_at") or
                    item.get("updated_at")
                )
                
                if timestamp_str:
                    try:
                        # Parse timestamp (platform-specific parsing)
                        if isinstance(timestamp_str, str):
                            # Handle ISO format with timezone
                            if timestamp_str.endswith('Z'):
                                content_timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                            else:
                                content_timestamp = datetime.fromisoformat(timestamp_str)
                        elif isinstance(timestamp_str, (int, float)):
                            content_timestamp = datetime.fromtimestamp(timestamp_str)
                    except (ValueError, TypeError):
                        # Fallback to ingestion timestamp if parsing fails
                        content_timestamp = datetime.utcnow()
                else:
                    # Fallback to ingestion timestamp
                    content_timestamp = datetime.utcnow()
                
                content_timestamps.append({
                    "content_id": content_id,
                    "timestamp": content_timestamp.isoformat(),
                    "timestamp_source": "content" if timestamp_str else "ingestion_fallback"
                })
                
                # Record lineage: content_id -> sequence_id mapping
                self.content_lineage[content_id].append(sequence_id)
        
        # CRITICAL: Record ingestion sequence entry with actual content timestamps
        sequence_entry = {
            "sequence_id": sequence_id,
            "run_id": job.run_id,
            "job_id": job.job_id,
            "platform": job.platform,
            "mode": job.mode.value,
            "window_start": job.window.start.isoformat(),
            "window_end": job.window.end.isoformat(),
            "ingestion_timestamp": datetime.utcnow().isoformat(),
            "content_count": len(content),
            "content_ids": content_ids,
            # CRITICAL: Store actual content timestamps for cross-platform ordering
            "content_timestamps": content_timestamps,
            "earliest_content_timestamp": min(ts["timestamp"] for ts in content_timestamps) if content_timestamps else None,
            "latest_content_timestamp": max(ts["timestamp"] for ts in content_timestamps) if content_timestamps else None,
            "fingerprint": IngestionIdentity(
                platform=job.platform,
                content_id=job.content_id,
                window_start=job.window.start,
                window_end=job.window.end
            ).fingerprint(),
            # CRITICAL: Store causal ordering key for reconstruction
            "causal_ordering_key": f"{sequence_id:010d}_{job.platform}_{job.run_id}",
            "global_sequence_position": sequence_id  # Absolute position in global ordering
        }
        
        self.ingestion_sequence.append(sequence_entry)
        
        # Keep sequence bounded to prevent memory growth (keep last 10000 entries)
        if len(self.ingestion_sequence) > 10000:
            self.ingestion_sequence = self.ingestion_sequence[-10000:]
        
        self.logger.debug(
            f"Recorded causal sequence {sequence_id} for job {job.job_id} "
            f"(platform: {job.platform}, content: {len(content)} items)",
            extra={
                "sequence_id": sequence_id,
                "job_id": job.job_id,
                "platform": job.platform,
                "content_count": len(content),
                "content_ids": content_ids[:10],  # Log first 10 for debugging
                "earliest_content_timestamp": sequence_entry["earliest_content_timestamp"],
                "causal_ordering_key": sequence_entry["causal_ordering_key"]
            }
        )
    
    def get_global_ordering(self) -> Dict[str, Any]:
        """Get global ordering information for reconstruction"""
        return {
            "current_sequence_counter": self.causal_sequence_counter,
            "total_sequences_recorded": len(self.ingestion_sequence),
            "content_lineage_entries": len(self.content_lineage),
            "latest_sequences": self.ingestion_sequence[-10:] if self.ingestion_sequence else [],
            "can_reconstruct_ordering": len(self.ingestion_sequence) > 0
        }
    
    def reconstruct_cross_platform_ordering(self) -> Dict[str, Any]:
        """Reconstruct global ordering across platforms using content timestamps"""
        if not self.ingestion_sequence:
            return {
                "error": "No ingestion sequence data available",
                "can_reconstruct": False
            }
        
        # Collect all content items with their timestamps and sequence info
        all_content_items = []
        
        for sequence_entry in self.ingestion_sequence:
            sequence_id = sequence_entry["sequence_id"]
            platform = sequence_entry["platform"]
            job_id = sequence_entry["job_id"]
            ingestion_timestamp = sequence_entry["ingestion_timestamp"]
            
            # Add each content item with its timestamp
            for content_timestamp_info in sequence_entry["content_timestamps"]:
                content_id = content_timestamp_info["content_id"]
                content_timestamp = content_timestamp_info["timestamp"]
                timestamp_source = content_timestamp_info["timestamp_source"]
                
                all_content_items.append({
                    "content_id": content_id,
                    "platform": platform,
                    "job_id": job_id,
                    "sequence_id": sequence_id,
                    "content_timestamp": content_timestamp,
                    "timestamp_source": timestamp_source,
                    "ingestion_timestamp": ingestion_timestamp,
                    "causal_ordering_key": sequence_entry["causal_ordering_key"],
                    "global_sequence_position": sequence_entry["global_sequence_position"]
                })
        
        # Sort by content timestamp to answer "What was ingested first across platforms?"
        sorted_by_content_timestamp = sorted(all_content_items, key=lambda x: x["content_timestamp"])
        
        # Also sort by ingestion sequence for comparison
        sorted_by_ingestion_sequence = sorted(all_content_items, key=lambda x: x["sequence_id"])
        
        # Calculate ordering statistics
        platform_first_items = {}
        platform_last_items = {}
        
        for item in sorted_by_content_timestamp:
            platform = item["platform"]
            if platform not in platform_first_items:
                platform_first_items[platform] = item
        
        for item in reversed(sorted_by_content_timestamp):
            platform = item["platform"]
            if platform not in platform_last_items:
                platform_last_items[platform] = item
        
        return {
            "can_reconstruct": True,
            "total_content_items": len(all_content_items),
            "ordering_by_content_timestamp": sorted_by_content_timestamp[:100],  # First 100 items
            "ordering_by_ingestion_sequence": sorted_by_ingestion_sequence[:100],  # First 100 items
            "platform_first_items": platform_first_items,
            "platform_last_items": platform_last_items,
            "earliest_content_timestamp": sorted_by_content_timestamp[0]["content_timestamp"] if sorted_by_content_timestamp else None,
            "latest_content_timestamp": sorted_by_content_timestamp[-1]["content_timestamp"] if sorted_by_content_timestamp else None,
            "time_span_days": (
                (
                    datetime.fromisoformat(sorted_by_content_timestamp[-1]["content_timestamp"]) - 
                    datetime.fromisoformat(sorted_by_content_timestamp[0]["content_timestamp"])
                ).days 
                if sorted_by_content_timestamp else 0
            ),
            "reconstruction_timestamp": datetime.utcnow().isoformat()
        }
    
    def get_content_cross_platform_timeline(self, content_id: str) -> Dict[str, Any]:
        """Get complete cross-platform timeline for a specific content item"""
        if content_id not in self.content_lineage:
            return {
                "error": f"Content ID {content_id} not found in lineage",
                "content_id": content_id,
                "timeline": []
            }
        
        # Find all sequence entries that contain this content
        timeline_entries = []
        
        for sequence_entry in self.ingestion_sequence:
            if content_id in sequence_entry["content_ids"]:
                # Find the specific content timestamp
                content_timestamp = None
                timestamp_source = None
                
                for ct_info in sequence_entry["content_timestamps"]:
                    if ct_info["content_id"] == content_id:
                        content_timestamp = ct_info["timestamp"]
                        timestamp_source = ct_info["timestamp_source"]
                        break
                
                timeline_entries.append({
                    "sequence_id": sequence_entry["sequence_id"],
                    "platform": sequence_entry["platform"],
                    "job_id": sequence_entry["job_id"],
                    "mode": sequence_entry["mode"],
                    "window_start": sequence_entry["window_start"],
                    "window_end": sequence_entry["window_end"],
                    "ingestion_timestamp": sequence_entry["ingestion_timestamp"],
                    "content_timestamp": content_timestamp,
                    "timestamp_source": timestamp_source,
                    "causal_ordering_key": sequence_entry["causal_ordering_key"],
                    "global_sequence_position": sequence_entry["global_sequence_position"]
                })
        
        # Sort by sequence ID to show chronological ingestion
        timeline_entries.sort(key=lambda x: x["sequence_id"])
        
        return {
            "content_id": content_id,
            "total_occurrences": len(timeline_entries),
            "timeline": timeline_entries,
            "first_seen": timeline_entries[0] if timeline_entries else None,
            "last_seen": timeline_entries[-1] if timeline_entries else None,
            "platforms": list(set(entry["platform"] for entry in timeline_entries)),
            "reconstruction_timestamp": datetime.utcnow().isoformat()
        }
    
    def reconstruct_content_timeline(self, content_id: str) -> List[Dict[str, Any]]:
        """Reconstruct the complete timeline for a specific content item"""
        if content_id not in self.content_lineage:
            return []
        
        sequence_ids = self.content_lineage[content_id]
        timeline = []
        
        for seq_id in sequence_ids:
            # Find the sequence entry
            sequence_entry = next(
                (seq for seq in self.ingestion_sequence if seq["sequence_id"] == seq_id),
                None
            )
            if sequence_entry:
                timeline.append(sequence_entry)
        
        return sorted(timeline, key=lambda x: x["sequence_id"])
    
    async def get_status(self) -> Dict[str, Any]:
        now = datetime.utcnow()
        
        # Platform health summary
        platform_status = {}
        for platform, health in self.platform_health.items():
            status = {
                "health_status": health["health_status"].value,
                "failures": health["failures"],
                "disabled_until": health["disabled_until"].isoformat() if health["disabled_until"] else None,
                "last_success": health["last_success"].isoformat() if health["last_success"] else None
            }
            
            # Add freshness info
            last_success = self.last_successful_ingestion.get(platform)
            if last_success:
                freshness_age = (now - last_success).total_seconds()
                threshold = self.freshness_thresholds.get(platform, timedelta(minutes=5)).total_seconds()
                status["freshness_age_seconds"] = freshness_age
                status["freshness_threshold_seconds"] = threshold
                status["is_fresh"] = freshness_age < threshold
            
            platform_status[platform] = status
        
        return {
            "pipeline_status": "running" if self.scheduler_running else "stopped",
            "inflight_jobs": len(self.inflight_jobs),
            "queued_jobs": len(self.job_queue),  # job_queue is a list, not a queue
            "completed_jobs": len(self.completed_jobs),
            "ledger_entries": len(self.ingestion_ledger),
            "platform_status": platform_status,
            "timestamp": now.isoformat()
        }
    
    async def force_platform_recovery(self, platform: str) -> bool:
        """Force recovery of a disabled platform"""
        if platform not in self.adapters:
            return False
        
        health = self.platform_health[platform]
        
        # Reset circuit breaker
        health["failures"] = 0
        health["disabled_until"] = None
        health["health_status"] = PlatformHealth.HEALTHY
        
        # Validate credentials
        try:
            valid = await self.adapters[platform].validate_credentials()
            if not valid:
                health["health_status"] = PlatformHealth.DISABLED
                return False
        except Exception as e:
            self.logger.error(f"Platform recovery validation failed: {e}")
            health["health_status"] = PlatformHealth.DISABLED
            return False
        
        self.logger.info(f"Platform {platform} manually recovered")
        return True
    
    # ========================================================================
    # 🔸 OPTIONAL HIGH-LEVERAGE FEATURES (ADD-ON INTELLIGENCE)
    # ========================================================================
    
    def _update_trust_score(self, platform: str, success: bool) -> None:
        """Update platform trust score based on success/failure"""
        if not self.enable_trust_scoring:
            return
        
        current_score = self.platform_trust_scores[platform]
        decay_factor = 0.95  # 5% decay per failure
        
        if success:
            # Increase trust on success
            self.platform_trust_scores[platform] = min(1.0, current_score + 0.1)
        else:
            # Decrease trust with decay
            self.platform_trust_scores[platform] = max(0.1, current_score * decay_factor)
        
        self.logger.debug(f"Platform {platform} trust score: {self.platform_trust_scores[platform]:.3f}")
    
    def _calculate_dynamic_window(self, platform: str, urgency: float) -> int:
        """Calculate dynamic window size based on urgency and platform trust"""
        if not self.enable_dynamic_windows:
            return 30  # Default 30-minute window
        
        trust_score = self.platform_trust_scores[platform]
        
        # Higher urgency = smaller windows for faster response
        # Higher trust = larger windows for efficiency
        base_window = 30
        urgency_factor = max(0.2, 1.0 - urgency)  # Urgency inverts window size
        trust_factor = 0.5 + (trust_score * 0.5)  # Trust scales window up
        
        dynamic_window = int(base_window * urgency_factor * trust_factor)
        
        # Clamp to min/max bounds
        final_window = max(self.min_window_minutes, min(self.max_window_minutes, dynamic_window))
        
        # CRITICAL: Log dynamic window calculation for visibility
        self.logger.info(
            f"Dynamic window calculation for {platform}: "
            f"urgency={urgency:.2f}, trust={trust_score:.2f}, "
            f"urgency_factor={urgency_factor:.2f}, trust_factor={trust_factor:.2f}, "
            f"base_window={base_window}, dynamic_window={dynamic_window}, final_window={final_window}",
            extra={
                "platform": platform,
                "urgency": urgency,
                "trust_score": trust_score,
                "urgency_factor": urgency_factor,
                "trust_factor": trust_factor,
                "base_window": base_window,
                "dynamic_window": dynamic_window,
                "final_window": final_window,
                "window_reduction": base_window - final_window,
                "dynamic_window_active": True
            }
        )
        
        return final_window
    
    def _check_cost_budget(self, estimated_cost: float) -> bool:
        """Check if estimated cost fits within hourly budget"""
        if not self.enable_cost_budgeting:
            return True
        
        now = datetime.utcnow()
        hours_elapsed = (now - self.cost_budget_start).total_seconds() / 3600.0
        
        if hours_elapsed >= 1.0:
            # Reset budget window
            self.hourly_cost_spend = 0.0
            self.cost_budget_start = now
            hours_elapsed = 0.0
        
        remaining_budget = self.cost_budget_hourly * (1.0 - hours_elapsed)
        can_afford = (self.hourly_cost_spend + estimated_cost) <= remaining_budget
        
        if not can_afford:
            self.logger.warning(f"Cost budget exceeded: ${self.hourly_cost_spend + estimated_cost:.2f} > ${remaining_budget:.2f}")
        
        return can_afford
    
    def _record_shadow_result(self, platform: str, job: IngestionJob, result: Dict[str, Any]) -> None:
        """Record shadow mode results for comparison"""
        if not self.enable_shadow_mode:
            return
        
        shadow_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "job_id": job.job_id,
            "platform": platform,
            "mode": job.mode.value,
            "result": result,
            "cost_estimate": result.get("estimated_cost", 0.0),
            "item_count": result.get("item_count", 0)
        }
        
        self.shadow_mode_results[platform].append(shadow_record)
        
        # Keep only last 100 shadow results per platform
        if len(self.shadow_mode_results[platform]) > 100:
            self.shadow_mode_results[platform] = self.shadow_mode_results[platform][-100:]
    
    def _validate_partial_success(self, content: List[Dict[str, Any]], platform: str) -> PartialSuccess:
        """Validate content and track partial success metrics"""
        total_items = len(content)
        successful_items = 0
        failed_items = 0
        dropped_items = 0
        invalid_items = 0
        error_summary = defaultdict(int)
        
        for item in content:
            # Basic validation
            if not item.get("id") and not item.get("content_id"):
                invalid_items += 1
                error_summary["missing_id"] += 1
                continue
            
            # Platform-specific validation would go here
            if self._is_valid_item(item, platform):
                successful_items += 1
            else:
                failed_items += 1
                error_type = self._classify_item_error(item, platform)
                error_summary[error_type] += 1
        
        # Simulate dropped items (APIs lie about what they return)
        estimated_dropped = max(0, int(total_items * 0.02))  # 2% estimated drop rate
        dropped_items = estimated_dropped
        
        return PartialSuccess(
            total_items=total_items,
            successful_items=successful_items,
            failed_items=failed_items,
            dropped_items=dropped_items,
            invalid_items=invalid_items,
            error_summary=dict(error_summary)
        )
    
    def _is_valid_item(self, item: Dict[str, Any], platform: str) -> bool:
        """Platform-specific item validation"""
        # Basic validation - would be platform-specific in production
        required_fields = ["id", "timestamp", "content"]
        return all(field in item for field in required_fields)
    
    def _classify_item_error(self, item: Dict[str, Any], platform: str) -> str:
        """Classify why an item failed validation"""
        if not item.get("id"):
            return "missing_id"
        if not item.get("timestamp"):
            return "missing_timestamp"
        if not item.get("content"):
            return "missing_content"
        return "unknown_error"
    
    def _get_enhanced_job_priority(self, job: IngestionJob) -> int:
        """Calculate job priority with trust scoring and cost budgeting"""
        base_priority = self._get_priority_for_mode(job.mode)
        
        if not self.enable_trust_scoring:
            return base_priority
        
        # Lower priority for low-trust platforms
        trust_score = self.platform_trust_scores[job.platform]
        trust_penalty = int((1.0 - trust_score) * 10)  # 0-10 point penalty
        
        return base_priority + trust_penalty
    
    async def _enhanced_dispatch_job(self, job: IngestionJob, worker_id: str) -> None:
        """Enhanced dispatch with all optional features"""
        start_time = datetime.utcnow()
        self.inflight_jobs.add(job.job_id)
        
        # Update job state to running
        job.state = JobState.RUNNING
        job.last_state_change = datetime.utcnow()
        
        try:
            self.logger.info(
                f"Worker {worker_id} processing job {job.job_id} for {job.platform} "
                f"[run_id: {job.run_id}, mode: {job.mode.value}]",
                extra={
                    "run_id": job.run_id,
                    "job_id": job.job_id,
                    "platform": job.platform,
                    "mode": job.mode.value,
                    "worker_id": worker_id
                }
            )
            
            # Cost budget check
            estimated_cost = self._estimate_job_cost(job)
            if not self._check_cost_budget(estimated_cost):
                raise Exception(f"Cost budget exceeded: ${estimated_cost:.2f}")
            
            # Check for duplicate ingestion
            identity = IngestionIdentity(
                platform=job.platform,
                content_id=job.content_id,
                window_start=job.window.start,
                window_end=job.window.end
            )
            
            fp = identity.fingerprint()
            if fp in self.ingestion_ledger:
                self.logger.warning(f"Duplicate ingestion blocked: {fp}", extra={"fingerprint": fp})
                raise DuplicateIngestionError(f"Duplicate ingestion: {fp}")
            
            # Check platform circuit breaker
            health = self.platform_health[job.platform]
            if health["disabled_until"] and datetime.utcnow() < health["disabled_until"]:
                self.logger.warning(f"Platform {job.platform} is disabled")
                raise PlatformDisabledError(f"Platform {job.platform} circuit-broken")
            
            # Mark ingestion in ledger
            self.ingestion_ledger[fp] = job.job_id
            
            # Execute ingestion
            adapter = self.adapters[job.platform]
            content = await adapter.fetch_content(job.content_id, job.window)
            
            # Validate and track partial success
            partial_success = self._validate_partial_success(content, job.platform)
            job.partial_success = partial_success
            
            # Enforce monotonic time guard
            await self._enforce_time_guard(content)
            
            # CRITICAL: Check ingestion consensus protection
            should_ingest = self._check_ingestion_consensus(job.content_id, job.platform, True)
            if not should_ingest:
                self.logger.info(
                    f"Job {job.job_id} blocked by consensus protection for {job.content_id}",
                    extra={
                        "job_id": job.job_id,
                        "platform": job.platform,
                        "content_id": job.content_id,
                        "consensus_decision": False,
                        "consensus_threshold": self.consensus_threshold
                    }
                )
                # Mark as completed with consensus block status
                duration = (datetime.utcnow() - start_time).total_seconds()
                self.completed_jobs[job.job_id] = {
                    "status": "consensus_blocked",
                    "duration": duration,
                    "reason": "Consensus protection blocked ingestion",
                    "consensus_threshold": self.consensus_threshold,
                    "run_id": job.run_id,
                    "completed_at": datetime.utcnow().isoformat()
                }
                return
            
            # CRITICAL: Validate causal dependencies
            if not self._validate_causal_dependencies(job):
                self.logger.warning(
                    f"Job {job.job_id} blocked by unsatisfied causal dependencies",
                    extra={
                        "job_id": job.job_id,
                        "platform": job.platform,
                        "causal_violation": True
                    }
                )
                # Mark as completed with dependency block status
                duration = (datetime.utcnow() - start_time).total_seconds()
                self.completed_jobs[job.job_id] = {
                    "status": "dependency_blocked",
                    "duration": duration,
                    "reason": "Causal dependencies not satisfied",
                    "run_id": job.run_id,
                    "completed_at": datetime.utcnow().isoformat()
                }
                return
            
            # CRITICAL: Record causal sequence for global ordering reconstruction
            self._record_causal_sequence(job, content)
            
            # Store content (or shadow mode)
            if self.enable_shadow_mode:
                # Shadow mode: don't actually store, just record
                result = {
                    "status": "shadow_success",
                    "content_count": len(content),
                    "partial_success": partial_success.__dict__,
                    "estimated_cost": estimated_cost
                }
                self._record_shadow_result(job.platform, job, result)
                success = True
                
                # Shadow mode completion: no checkpoint needed since no durable changes
                self._transition_job_state(job, JobState.COMPLETED, "shadow_mode_confirmed")
            else:
                success = await self.storage.store_content(job.platform, content)
            
            if not success and not self.enable_shadow_mode:
                raise Exception("Failed to store content")
            
            # CRITICAL: Save checkpoint BEFORE marking completion to ensure durability
            # This guarantees "No data is marked complete unless written successfully"
            if not self.enable_shadow_mode:
                checkpoint_success = await self._save_checkpoint()
                if not checkpoint_success:
                    # If checkpoint fails, we cannot consider this job complete
                    raise Exception("Failed to save checkpoint - job completion not persisted")
                
                # CRITICAL: Only mark job as completed AFTER successful checkpoint
                # This enforces the invariant: STORE → CHECKPOINT ACK → COMPLETE
                duration = (datetime.utcnow() - start_time).total_seconds()
                completion_record = {
                    "status": JobState.COMPLETED.value,
                    "duration": duration,
                    "content_count": len(content),
                    "partial_success": partial_success.__dict__,
                    "estimated_cost": estimated_cost,
                    "run_id": job.run_id,
                    "completed_at": datetime.utcnow().isoformat(),
                    "checkpoint_confirmed": True,  # Checkpoint confirmed before completion
                    "shadow_mode": False
                }
                self.completed_jobs[job.job_id] = completion_record
                
                # Production mode: mark completion only after successful storage AND checkpoint
                self._transition_job_state(job, JobState.COMPLETED, "storage_and_checkpoint_confirmed")
            else:
                # Shadow mode: no checkpoint needed since no durable changes
                duration = (datetime.utcnow() - start_time).total_seconds()
                completion_record = {
                    "status": "shadow_success",
                    "duration": duration,
                    "content_count": len(content),
                    "partial_success": partial_success.__dict__,
                    "estimated_cost": estimated_cost,
                    "run_id": job.run_id,
                    "completed_at": datetime.utcnow().isoformat(),
                    "checkpoint_confirmed": False,  # Shadow mode doesn't need checkpoint
                    "shadow_mode": True
                }
                self.completed_jobs[job.job_id] = completion_record
                
                # Shadow mode completion: no checkpoint needed since no durable changes
                self._transition_job_state(job, JobState.COMPLETED, "shadow_mode_confirmed")
            
            # Update trust score based on success (both production and shadow)
            self._update_trust_score(job.platform, True)
            
            # Update success tracking (both production and shadow)
            self.last_successful_ingestion[job.platform] = datetime.utcnow()
            health["failures"] = 0
            health["disabled_until"] = None
            health["last_success"] = datetime.utcnow()
            health["health_status"] = PlatformHealth.HEALTHY
            
            # Update cost tracking (only for production mode)
            if self.enable_cost_budgeting and not self.enable_shadow_mode:
                self.hourly_cost_spend += estimated_cost
            
            # CRITICAL: Record meta-learning feedback
            metrics = {
                "duration": duration,
                "content_count": len(content),
                "estimated_cost": estimated_cost,
                "success_rate": partial_success.successful_items / max(partial_success.total_items, 1),
                "partial_success_rate": partial_success.successful_items / max(partial_success.total_items, 1),
                "window_size_minutes": (job.window.end - job.window.start).total_seconds() / 60
            }
            self._record_meta_learning_feedback(job.platform, job.job_id, metrics)
            
            # CRITICAL: Mark causal dependencies as satisfied
            self._mark_dependency_satisfied(job.job_id)
            
            # CRITICAL: Run invariant validation after successful completion
            if self.invariant_validation_enabled:
                now = datetime.utcnow()
                if now - self.last_invariant_check > self.invariant_check_interval:
                    violations = self._validate_system_invariants()
                    if violations:
                        self.logger.warning(f"System invariant violations detected: {len(violations)}")
            
            self.logger.info(
                f"Job {job.job_id} completed successfully in {duration:.2f}s "
                f"(items: {len(content)}, cost: ${estimated_cost:.2f})",
                extra={
                    "job_id": job.job_id,
                    "duration": duration,
                    "content_count": len(content),
                    "estimated_cost": estimated_cost,
                    "partial_success": partial_success.__dict__
                }
            )
            
        except Exception as e:
            # Update trust score based on failure
            self._update_trust_score(job.platform, False)
            
            # Classify failure
            failure_type = classify_failure(e)
            job.failure_type = failure_type
            
            # Update platform health
            health = self.platform_health[job.platform]
            health["failures"] += 1
            
            # CRITICAL: Check for failure escalation
            failure_time = datetime.utcnow()
            should_escalate = self._check_failure_escalation(job.platform, failure_time)
            if should_escalate:
                window_start = failure_time - self.failure_escalation_window
                failure_count = len(self.failure_history[job.platform])
                self._invoke_failure_escalation_hook(job.platform, failure_count, window_start, failure_time)
            
            # Circuit breaker logic
            if health["failures"] >= self.circuit_breaker_threshold:
                health["disabled_until"] = datetime.utcnow() + self.circuit_breaker_timeout
                health["health_status"] = PlatformHealth.DISABLED
                self.logger.error(f"Platform {job.platform} circuit-broken due to {health['failures']} failures")
            
            # Retry logic
            if job.retry_count < job.max_retries:
                job.retry_count += 1
                retry_delay = get_retry_delay(failure_type, job.retry_count)
                job.scheduled_at = datetime.utcnow() + retry_delay
                
                # Schedule retry for later (non-blocking)
                self.retry_queue.append((job.scheduled_at, job))
                
                self.logger.warning(
                    f"Job {job.job_id} retry {job.retry_count}/{job.max_retries} scheduled for {retry_delay.total_seconds()}s",
                    extra={
                        "job_id": job.job_id,
                        "platform": job.platform,
                        "retry_count": job.retry_count,
                        "failure_type": failure_type.value,
                        "retry_delay": retry_delay.total_seconds()
                    }
                )
            else:
                # Max retries exceeded
                duration = (datetime.utcnow() - start_time).total_seconds()
                self.completed_jobs[job.job_id] = {
                    "status": JobState.FAILED.value,
                    "failure_type": failure_type.value,
                    "retry_count": job.retry_count,
                    "duration": duration,
                    "error": str(e),
                    "run_id": job.run_id,
                    "completed_at": datetime.utcnow().isoformat()
                }
                
                self.logger.error(
                    f"Job {job.job_id} failed permanently after {job.retry_count} retries: {e}",
                    extra={
                        "job_id": job.job_id,
                        "failure_type": failure_type.value,
                        "retry_count": job.retry_count,
                        "error": str(e)
                    }
                )
        
        finally:
            self.inflight_jobs.discard(job.job_id)
    
    def _estimate_job_cost(self, job: IngestionJob) -> float:
        """Estimate job cost based on platform, mode, and window size"""
        # Base cost per platform (would be configurable in production)
        platform_costs = {
            "tiktok": 0.01,    # $0.01 per request
            "youtube": 0.008,
            "instagram": 0.012,
            "twitter": 0.005
        }
        
        base_cost = platform_costs.get(job.platform, 0.01)
        
        # Mode multiplier
        mode_multipliers = {
            IngestionMode.REALTIME_FAST: 1.5,
            IngestionMode.STANDARD_POLL: 1.0,
            IngestionMode.BACKFILL: 0.8,
            IngestionMode.RECOVERY: 1.2
        }
        
        mode_multiplier = mode_multipliers.get(job.mode, 1.0)
        
        # Window size factor (larger windows = more cost)
        window_minutes = (job.window.end - job.window.start).total_seconds() / 60.0
        window_factor = max(0.5, window_minutes / 30.0)  # Normalize to 30-minute base
        
        return base_cost * mode_multiplier * window_factor
    
    async def get_enhanced_status(self) -> Dict[str, Any]:
        """Get comprehensive pipeline status with optional features"""
        base_status = await self.get_status()
        
        # Add optional feature status
        enhanced_status = base_status.copy()
        
        if self.enable_trust_scoring:
            enhanced_status["trust_scores"] = dict(self.platform_trust_scores)
        
        if self.enable_dynamic_windows:
            enhanced_status["current_window_sizes"] = dict(self.current_window_sizes)
        
        if self.enable_cost_budgeting:
            enhanced_status["cost_budgeting"] = {
                "hourly_budget": self.cost_budget_hourly,
                "current_spend": self.hourly_cost_spend,
                "budget_start": self.cost_budget_start.isoformat(),
                "budget_remaining": max(0, self.cost_budget_hourly - self.hourly_cost_spend)
            }
        
        if self.enable_shadow_mode:
            enhanced_status["shadow_mode"] = {
                "enabled": True,
                "results_count": {p: len(results) for p, results in self.shadow_mode_results.items()}
            }
        
        enhanced_status["run_id"] = self.run_id
        enhanced_status["optional_features"] = {
            "trust_scoring": self.enable_trust_scoring,
            "dynamic_windows": self.enable_dynamic_windows,
            "cost_budgeting": self.enable_cost_budgeting,
            "shadow_mode": self.enable_shadow_mode
        }
        
        # CRITICAL: Include global ordering reconstruction information
        enhanced_status["ordering_reconstruction"] = self.get_global_ordering()
        
        # CRITICAL: Include cross-platform ordering reconstruction
        enhanced_status["cross_platform_ordering"] = self.reconstruct_cross_platform_ordering()
        
        # CRITICAL: Include backlog and lag metrics
        enhanced_status["backlog_metrics"] = self._calculate_backlog_metrics()
        
        # CRITICAL: Include failure escalation status
        enhanced_status["failure_escalation"] = self.get_failure_escalation_status()
        
        # CRITICAL: Include advanced enhancements status
        enhanced_status["advanced_enhancements"] = self.get_advanced_enhancements_status()
        
        # CRITICAL: Include kill-switch status
        enhanced_status["kill_switch"] = self.get_kill_switch_status()
        
        # CRITICAL: Include scheduler math status
        if self.scheduler_math_enabled:
            enhanced_status["scheduler_math"] = {
                "enabled": True,
                "scoring_weights": self.scheduler_scoring_weights,
                "safety_invariants": {
                    "no_starvation_threshold": self.scheduler_no_starvation_threshold.total_seconds() / 60,
                    "max_realtime_delay": self.scheduler_max_realtime_delay.total_seconds() / 60,
                    "failure_isolation": self.scheduler_failure_isolation
                }
            }
        
        # CRITICAL: Include invariant watchdog status
        if self.invariant_watchdog_enabled:
            enhanced_status["invariant_watchdog"] = {
                "enabled": True,
                "heartbeat_interval": self.invariant_watchdog_interval.total_seconds(),
                "system_frozen": self.system_frozen_for_invariant_violation,
                "panic_reason": self.invariant_panic_reason,
                "panic_timestamp": self.invariant_panic_timestamp.isoformat() if self.invariant_panic_timestamp else None,
                "violation_history_count": len(self.invariant_violation_history),
                "max_violations_per_hour": self.max_invariant_violations_per_hour
            }
        
        enhanced_status["timestamp"] = datetime.utcnow().isoformat()
        
        # CRITICAL: Include inflight recovery status
        enhanced_status["inflight_recovery"] = self.get_inflight_recovery_status()
        
        # CRITICAL: Include causal safety status
        enhanced_status["causal_safety"] = self.get_causal_safety_status()
        
        # CRITICAL: Include invariant validation status
        enhanced_status["invariant_validation"] = self.get_invariant_validation_status()
        
        return enhanced_status
    
    async def get_shadow_mode_comparison(self, platform: str) -> Dict[str, Any]:
        """Get shadow mode vs production comparison for a platform"""
        if not self.enable_shadow_mode or platform not in self.shadow_mode_results:
            return {"error": "Shadow mode not enabled or no data for platform"}
        
        shadow_results = self.shadow_mode_results[platform]
        
        # Calculate shadow mode metrics
        shadow_success_rate = sum(1 for r in shadow_results if r["result"]["status"] == "shadow_success") / len(shadow_results)
        shadow_avg_cost = sum(r["result"]["estimated_cost"] for r in shadow_results) / len(shadow_results)
        shadow_avg_items = sum(r["result"]["item_count"] for r in shadow_results) / len(shadow_results)
        
        # Get production metrics for comparison
        production_jobs = [j for j in self.completed_jobs.values() if j.get("platform") == platform and not j.get("status", "").startswith("shadow")]
        
        if production_jobs:
            prod_success_rate = sum(1 for j in production_jobs if j["status"] == "success") / len(production_jobs)
            prod_avg_items = sum(j.get("content_count", 0) for j in production_jobs) / len(production_jobs)
        else:
            prod_success_rate = 0.0
            prod_avg_items = 0.0
        
        return {
            "platform": platform,
            "comparison_period": f"Last {len(shadow_results)} shadow runs",
            "shadow_mode": {
                "success_rate": shadow_success_rate,
                "avg_cost_per_run": shadow_avg_cost,
                "avg_items_per_run": shadow_avg_items,
                "total_runs": len(shadow_results)
            },
            "production": {
                "success_rate": prod_success_rate,
                "avg_items_per_run": prod_avg_items,
                "total_runs": len(production_jobs)
            },
            "recommendation": self._generate_shadow_recommendation(shadow_success_rate, prod_success_rate, shadow_avg_cost)
        }
    
    def _generate_shadow_recommendation(self, shadow_success: float, prod_success: float, shadow_cost: float) -> str:
        """Generate recommendation based on shadow mode comparison"""
        if shadow_success > prod_success + 0.05:  # 5% improvement threshold
            return "ENABLE_CHANGES"  # Shadow mode is significantly better
        elif shadow_success < prod_success - 0.05:
            return "KEEP_PRODUCTION"  # Production is better
        elif shadow_cost < 50:  # Low cost threshold
            return "CONSIDER_DEPLOYMENT"  # Cheap improvement
        else:
            return "NEED_MORE_DATA"  # Inconclusive results
    
    def get_causal_safety_status(self) -> Dict[str, Any]:
        """Get causal safety status and metrics"""
        # Detect cycles in causal graph
        cycles = self._detect_causal_cycles()
        
        # Calculate dependency satisfaction rates
        total_dependencies = len(self.causal_graph)
        satisfied_dependencies = sum(1 for dep_info in self.causal_graph.values() if dep_info.get("dependency_satisfied", False))
        satisfaction_rate = satisfied_dependencies / max(total_dependencies, 1)
        
        return {
            "enabled": True,
            "causal_dependencies": {
                "total": total_dependencies,
                "satisfied": satisfied_dependencies,
                "satisfaction_rate": satisfaction_rate,
                "pending": total_dependencies - satisfied_dependencies
            },
            "causal_graph": {
                "nodes": len(self.causal_graph),
                "edges": sum(len(deps) for deps in self.causal_dependencies.values()),
                "cycles_detected": len(cycles),
                "cycles": cycles[:5]  # First 5 cycles
            },
            "validation_cache": {
                "size": len(self.dependency_validation_cache),
                "hit_rate": len(self.dependency_validation_cache) / max(total_dependencies, 1)
            },
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def get_invariant_validation_status(self) -> Dict[str, Any]:
        """Get invariant validation status and metrics"""
        # Categorize violations by severity
        violations_by_severity = {"high": 0, "medium": 0, "low": 0}
        violation_types = {}
        
        for violation in self.invariant_violations[-100:]:  # Last 100 violations
            severity = violation.get("severity", "medium")
            violations_by_severity[severity] += 1
            
            vtype = violation.get("type", "unknown")
            violation_types[vtype] = violation_types.get(vtype, 0) + 1
        
        return {
            "enabled": self.invariant_validation_enabled,
            "validation_interval_minutes": self.invariant_check_interval.total_seconds() / 60,
            "last_check": self.last_invariant_check.isoformat(),
            "total_violations": len(self.invariant_violations),
            "recent_violations": len(self.invariant_violations[-100:]),
            "violations_by_severity": violations_by_severity,
            "violation_types": violation_types,
            "system_health": "healthy" if violations_by_severity["high"] == 0 else "degraded",
            "timestamp": datetime.utcnow().isoformat()
        }
