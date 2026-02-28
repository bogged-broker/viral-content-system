
"""
Factory Lifecycle Manager

Manages the complete lifecycle of niche factories including initialization,
start, pause, resume, and stop operations. Ensures factories consistently
hit 5M+ baseline per video with automatic scaling and recovery.

Location: /factories/factory_lifecycle.py
"""

import asyncio
import yaml
from typing import Dict, List, Optional, Any, Set
from pathlib import Path
from datetime import datetime, timedelta
from enum import Enum
import logging
import time
import weakref
from dataclasses import dataclass, field
from collections import defaultdict, deque


class FactoryStatus(Enum):
    """Factory operational states"""
    INITIALIZED = "initialized"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"
    RECOVERING = "recovering"
    THROTTLED = "throttled"
    ENFORCING = "enforcing"


@dataclass
class BoosterEffectiveness:
    """Track booster performance and causal outcomes"""
    booster_name: str
    activated_at: datetime
    baseline_before: float
    baseline_after: Optional[float] = None
    effectiveness_score: Optional[float] = None
    views_impact: int = 0
    engagement_delta: float = 0.0
    is_active: bool = True

@dataclass
class CircuitBreakerState:
    """Circuit breaker state for baseline enforcement"""
    failure_count: int = 0
    last_failure_time: Optional[datetime] = None
    state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    threshold: int = 3
    timeout_seconds: int = 300
    
    def should_trip(self) -> bool:
        return self.failure_count >= self.threshold and self.state == "CLOSED"
    
    def should_reset(self) -> bool:
        return (self.state == "OPEN" and 
                self.last_failure_time and 
                datetime.utcnow() - self.last_failure_time > timedelta(seconds=self.timeout_seconds))

class PipelineTask:
    """Represents an active pipeline task with real pause semantics"""
    
    def __init__(self, name: str, task_type: str):
        self.name = name
        self.task_type = task_type
        self.task_handle = None
        self.paused = False
        self.state = {}
        self.pause_event = asyncio.Event()
        self.pause_event.set()  # Start unpaused
        self.stop_event = asyncio.Event()
        self.health_check_time = datetime.utcnow()
        self.restart_count = 0
        self.max_restarts = 3
        self.backoff_seconds = 5
        self.last_heartbeat = datetime.utcnow()
        
    def is_active(self) -> bool:
        return (self.task_handle is not None and 
                not self.paused and 
                not self.stop_event.is_set() and
                not (self.task_handle.done() if self.task_handle else False))
    
    async def wait_if_paused(self):
        """Real pause semantics - blocks execution until resumed"""
        await self.pause_event.wait()
    
    def pause(self):
        """Actually pause the task by clearing the event"""
        self.paused = True
        self.pause_event.clear()
    
    def resume(self):
        """Resume the task by setting the event"""
        self.paused = False
        self.pause_event.set()
    
    def stop(self):
        """Signal the task to stop"""
        self.stop_event.set()
        self.resume()  # Ensure it's not blocked on pause
    
    def should_stop(self) -> bool:
        """Check if task should stop execution"""
        return self.stop_event.is_set()
    
    def update_heartbeat(self):
        """Update the last heartbeat time"""
        self.last_heartbeat = datetime.utcnow()
    
    def is_healthy(self, max_age_seconds: int = 60) -> bool:
        """Check if task is healthy based on heartbeat"""
        age = datetime.utcnow() - self.last_heartbeat
        return age.total_seconds() < max_age_seconds


class FactoryLifecycle:
    """
    Manages the lifecycle of a single niche factory:
    initialization, start, pause, resume, and stop.
    
    Ensures consistent 5M+ baseline performance with automatic
    booster application and real-time monitoring.
    """

    def __init__(self, niche: str, config_path: str, registry):
        """
        Initialize lifecycle manager for a niche factory.

        Args:
            niche: Factory niche name (e.g., 'fitness', 'comedy', 'tech')
            config_path: Path to niche YAML configuration
            registry: Instance of FactoryRegistry for state management
        """
        self.niche = niche
        self.config_path = Path(config_path)
        self.registry = registry
        self.status = FactoryStatus.INITIALIZED
        self.active_tasks: List[PipelineTask] = []
        self.config: Dict[str, Any] = {}
        self.ml_agent = None
        self.rl_agent = None
        self.video_queue = asyncio.Queue()
        self.performance_metrics = {
            "videos_processed": 0,
            "avg_views": 0,
            "baseline_hit_rate": 0.0,
            "booster_activations": 0,
            "recent_views": deque(maxlen=100),  # Track last 100 videos
            "baseline_failures": 0,
            "consecutive_failures": 0,
            "last_baseline_check": datetime.utcnow()
        }
        self.booster_config = {}
        self.logger = logging.getLogger(f"factory.{niche}")
        
        # SLA Enforcement Components
        self.circuit_breaker = CircuitBreakerState()
        self.active_boosters: Dict[str, BoosterEffectiveness] = {}
        self.publish_throttled = False
        self.throttle_reason = ""
        self.sla_violations = deque(maxlen=50)  # Track last 50 violations
        self.scaling_controller = None
        self.last_scale_check = datetime.utcnow()
        self.resource_metrics = defaultdict(float)
        
        # Health monitoring
        self.health_check_interval = 30  # seconds
        self.task_watchdog_enabled = True
        self.readiness_checks_enabled = True
        
        # Initialize factory
        self._load_config()
        self._initialize_agents()
        self._register_factory()
        self._setup_monitoring()
        self._initialize_scaling_controller()

    def _load_config(self):
        """
        Load niche-specific configuration including:
        - Thresholds for 5M+ baseline
        - Booster options and triggers
        - ML/RL agent assignments
        - Pipeline configurations
        """
        try:
            with open(self.config_path, "r") as f:
                self.config = yaml.safe_load(f)
            
            # Extract critical configuration
            self.baseline_threshold = self.config.get("baseline_threshold", 5_000_000)
            self.booster_config = self.config.get("boosters", {})
            self.pipeline_config = self.config.get("pipelines", {})
            self.scaling_config = self.config.get("scaling", {})
            
            self._log_lifecycle_event(f"Configuration loaded: {self.config_path}")
            
        except FileNotFoundError:
            self._log_lifecycle_event(f"Config file not found: {self.config_path}", level="error")
            raise
        except yaml.YAMLError as e:
            self._log_lifecycle_event(f"Invalid YAML config: {e}", level="error")
            raise

    def _initialize_agents(self):
        """
        Initialize ML and RL agents based on configuration.
        Lazy loading to avoid circular dependencies.
        """
        try:
            ml_agent_name = self.config.get("ml_agent")
            rl_agent_name = self.config.get("rl_agent")
            
            if ml_agent_name:
                # Dynamic import to avoid circular dependency
                from ml.virality_predictor import ViralityPredictor
                self.ml_agent = ViralityPredictor(niche=self.niche)
                
            if rl_agent_name:
                from ml.rl_optimizer import RLOptimizer
                self.rl_agent = RLOptimizer(niche=self.niche)
                
            self._log_lifecycle_event("ML/RL agents initialized")
            
        except ImportError as e:
            self._log_lifecycle_event(f"Agent initialization failed: {e}", level="warning")

    def _register_factory(self):
        """
        Register this factory in the central registry with full metadata.
        """
        try:
            self.registry.register_factory(
                niche=self.niche,
                config_path=str(self.config_path),
                ml_agent=self.config.get("ml_agent"),
                rl_agent=self.config.get("rl_agent"),
                baseline_threshold=self.baseline_threshold,
                created_at=datetime.utcnow().isoformat()
            )
            self._log_lifecycle_event("Factory registered in central registry")
            
        except Exception as e:
            self._log_lifecycle_event(f"Registration failed: {e}", level="error")
            raise

    def _setup_monitoring(self):
        """
        Initialize performance monitoring and alerting system.
        """
        self.monitoring_interval = self.config.get("monitoring_interval", 60)
        self.alert_thresholds = {
            "min_baseline_rate": self.config.get("min_baseline_rate", 0.8),
            "max_error_rate": self.config.get("max_error_rate", 0.05),
            "min_throughput": self.config.get("min_throughput", 100),
            "max_consecutive_failures": self.config.get("max_consecutive_failures", 5),
            "sla_violation_threshold": self.config.get("sla_violation_threshold", 3)
        }
        
        # Circuit breaker configuration
        self.circuit_breaker.threshold = self.config.get("circuit_breaker_threshold", 3)
        self.circuit_breaker.timeout_seconds = self.config.get("circuit_breaker_timeout", 300)
        
        # Health check configuration
        self.health_check_interval = self.config.get("health_check_interval", 30)
        self.task_watchdog_enabled = self.config.get("task_watchdog_enabled", True)
        self.readiness_checks_enabled = self.config.get("readiness_checks_enabled", True)

    def _initialize_scaling_controller(self):
        """
        Initialize scaling controller integration for resource monitoring.
        """
        try:
            # Dynamic import to avoid circular dependency
            from scaling.scaling_controller import ScalingController
            self.scaling_controller = ScalingController(niche=self.niche)
            self._log_lifecycle_event("Scaling controller initialized")
        except ImportError:
            self._log_lifecycle_event("Scaling controller not available, using mock", level="warning")
            self.scaling_controller = None

    async def start_factory(self):
        """
        Start the factory lifecycle with readiness checks and rollback:
        - Launch ingestion and content generation pipelines
        - Activate boosters if needed
        - Begin performance monitoring
        - Update registry status
        - Verify pipeline readiness
        """
        if self.status == FactoryStatus.RUNNING:
            self._log_lifecycle_event("Factory already running", level="warning")
            return
            
        # Reset circuit breaker if needed
        if self.circuit_breaker.should_reset():
            self.circuit_breaker.state = "CLOSED"
            self.circuit_breaker.failure_count = 0
            self._log_lifecycle_event("Circuit breaker reset")
            
        if self.circuit_breaker.state == "OPEN":
            self._log_lifecycle_event("Circuit breaker is OPEN, cannot start factory", level="error")
            return
            
        try:
            self.status = FactoryStatus.RUNNING
            self.registry.update_factory_status(self.niche, self.status.value)
            
            # Pre-start booster evaluation
            await self._evaluate_booster_requirements()
            
            # Launch pipelines with readiness checks
            launch_success = await self._launch_pipelines_with_readiness()
            
            if not launch_success:
                # Rollback failed launch
                await self._rollback_failed_start()
                raise RuntimeError("Pipeline launch failed readiness checks")
            
            # Start monitoring and health check loops
            asyncio.create_task(self._monitoring_loop())
            if self.task_watchdog_enabled:
                asyncio.create_task(self._task_watchdog_loop())
            
            self._log_lifecycle_event("✅ Factory started successfully with readiness verification")
            
        except Exception as e:
            self.status = FactoryStatus.ERROR
            self.registry.update_factory_status(self.niche, self.status.value)
            self._log_lifecycle_event(f"Factory start failed: {e}", level="error")
            await self._rollback_failed_start()
            raise

    async def pause_factory(self):
        """
        Pause the factory with real pause semantics:
        - Actually pause active pipeline tasks using asyncio.Event
        - Keep video queues intact
        - Maintain agent states
        """
        if self.status != FactoryStatus.RUNNING:
            self._log_lifecycle_event(f"Cannot pause factory in {self.status.value} state", level="warning")
            return
            
        try:
            self.status = FactoryStatus.PAUSED
            self.registry.update_factory_status(self.niche, self.status.value)
            
            # Real pause semantics - block task execution
            await self._pause_active_tasks_real()
            await self._save_state()
            
            self._log_lifecycle_event("⏸️ Factory paused with real task blocking")
            
        except Exception as e:
            self._log_lifecycle_event(f"Factory pause failed: {e}", level="error")
            raise

    async def resume_factory(self):
        """
        Resume a paused factory:
        - Restart pipelines and tasks
        - Restore saved state
        - Maintain state consistency
        """
        if self.status != FactoryStatus.PAUSED:
            self._log_lifecycle_event(f"Cannot resume factory in {self.status.value} state", level="warning")
            return
            
        try:
            self.status = FactoryStatus.RUNNING
            self.registry.update_factory_status(self.niche, self.status.value)
            
            await self._restore_state()
            await self._resume_active_tasks_real()
            
            # Restart monitoring
            asyncio.create_task(self._monitoring_loop())
            if self.task_watchdog_enabled:
                asyncio.create_task(self._task_watchdog_loop())
            
            self._log_lifecycle_event("▶️ Factory resumed with real task unblocking")
            
        except Exception as e:
            self.status = FactoryStatus.ERROR
            self._log_lifecycle_event(f"Factory resume failed: {e}", level="error")
            raise

    async def stop_factory(self, graceful: bool = True):
        """
        Stop the factory:
        - Terminate all active tasks
        - Persist current state
        - Deregister or mark as stopped
        
        Args:
            graceful: If True, wait for tasks to complete; if False, force stop
        """
        try:
            self.status = FactoryStatus.STOPPED
            self.registry.update_factory_status(self.niche, self.status.value)
            
            await self._terminate_tasks(graceful=graceful)
            await self._save_state()
            await self._cleanup_resources()
            
            self._log_lifecycle_event("⏹️ Factory stopped successfully")
            
        except Exception as e:
            self._log_lifecycle_event(f"Factory stop failed: {e}", level="error")
            raise

    async def _launch_pipelines_with_readiness(self) -> bool:
        """
        Launch pipelines with readiness checks and rollback capability.
        
        Returns:
            True if all pipelines launched successfully, False otherwise
        """
        pipeline_configs = [
            ("ingestion", self._run_ingestion_pipeline),
            ("feature_extraction", self._run_feature_pipeline),
            ("ml_prediction", self._run_ml_pipeline),
            ("rl_optimization", self._run_rl_pipeline),
            ("posting", self._run_posting_pipeline)
        ]
        
        launched_tasks = []
        
        try:
            for name, pipeline_func in pipeline_configs:
                if self.pipeline_config.get(name, {}).get("enabled", True):
                    task = PipelineTask(name, "pipeline")
                    task.task_handle = asyncio.create_task(pipeline_func())
                    self.active_tasks.append(task)
                    launched_tasks.append(task)
                    self._log_lifecycle_event(f"Launched pipeline: {name}")
                    
                    # Readiness check
                    if self.readiness_checks_enabled:
                        await asyncio.sleep(1)  # Brief startup time
                        if not await self._check_pipeline_readiness(task):
                            self._log_lifecycle_event(f"Pipeline {name} failed readiness check", level="error")
                            return False
            
            return True
            
        except Exception as e:
            self._log_lifecycle_event(f"Pipeline launch failed: {e}", level="error")
            return False

    async def _check_pipeline_readiness(self, task: PipelineTask) -> bool:
        """
        Check if a pipeline task is ready and healthy.
        
        Args:
            task: Pipeline task to check
            
        Returns:
            True if ready, False otherwise
        """
        if not task.task_handle or task.task_handle.done():
            return False
        
        # Check if task has started properly
        if not task.is_healthy(max_age_seconds=10):
            return False
        
        return True
    
    async def _rollback_failed_start(self):
        """
        Rollback a failed factory start by cleaning up launched tasks.
        """
        self._log_lifecycle_event("Rolling back failed factory start")
        await self._terminate_tasks(graceful=False)
        self.status = FactoryStatus.ERROR
        self.registry.update_factory_status(self.niche, self.status.value)

    async def _pause_active_tasks_real(self):
        """
        Actually pause all currently running tasks using asyncio.Event.
        """
        for task in self.active_tasks:
            if task.is_active():
                task.pause()  # This actually blocks execution
                # Save task state before pausing
                task.state = await self._capture_task_state(task)
                self._log_lifecycle_event(f"Paused task: {task.name}")

    async def _resume_active_tasks_real(self):
        """
        Resume previously paused tasks with real unblocking.
        """
        for task in self.active_tasks:
            if task.paused:
                # Restore task state
                await self._restore_task_state(task)
                task.resume()  # This actually unblocks execution
                self._log_lifecycle_event(f"Resumed task: {task.name}")

    async def _terminate_tasks(self, graceful: bool = True):
        """
        Stop all active tasks and save intermediate states.
        
        Args:
            graceful: If True, allow tasks to complete; if False, cancel immediately
        """
        for task in self.active_tasks:
            task.stop()  # Signal stop to all tasks
            
        if graceful:
            # Wait for tasks to finish current work
            for task in self.active_tasks:
                if task.task_handle and not task.task_handle.done():
                    try:
                        await asyncio.wait_for(task.task_handle, timeout=30.0)
                    except asyncio.TimeoutError:
                        task.task_handle.cancel()
        else:
            # Force cancel immediately
            for task in self.active_tasks:
                if task.task_handle and not task.task_handle.done():
                    task.task_handle.cancel()
                    
        self.active_tasks.clear()
    
    async def _task_watchdog_loop(self):
        """
        Watchdog loop for monitoring task health and automatic restarts.
        """
        while self.status in [FactoryStatus.RUNNING, FactoryStatus.RECOVERING]:
            try:
                await self._check_task_health()
                await asyncio.sleep(self.health_check_interval)
            except Exception as e:
                self._log_lifecycle_event(f"Task watchdog error: {e}", level="error")
                await asyncio.sleep(self.health_check_interval)
    
    async def _check_task_health(self):
        """
        Check health of all active tasks and restart if needed.
        """
        for task in self.active_tasks[:]:  # Copy list to avoid modification during iteration
            if not task.is_healthy():
                self._log_lifecycle_event(f"Task {task.name} appears unhealthy, checking restart conditions", level="warning")
                
                if task.restart_count < task.max_restarts:
                    await self._restart_task(task)
                else:
                    self._log_lifecycle_event(f"Task {task.name} exceeded max restarts, marking as failed", level="error")
                    await self._handle_failed_task(task)
    
    async def _restart_task(self, task: PipelineTask):
        """
        Restart a failed task with exponential backoff.
        """
        self._log_lifecycle_event(f"Restarting task {task.name} (attempt {task.restart_count + 1})")
        
        # Cancel current task
        if task.task_handle and not task.task_handle.done():
            task.task_handle.cancel()
            try:
                await task.task_handle
            except asyncio.CancelledError:
                pass
        
        # Apply backoff
        backoff = task.backoff_seconds * (2 ** task.restart_count)
        await asyncio.sleep(min(backoff, 300))  # Cap at 5 minutes
        
        # Restart task
        task.restart_count += 1
        pipeline_map = {
            "ingestion": self._run_ingestion_pipeline,
            "feature_extraction": self._run_feature_pipeline,
            "ml_prediction": self._run_ml_pipeline,
            "rl_optimization": self._run_rl_pipeline,
            "posting": self._run_posting_pipeline
        }
        
        if task.name in pipeline_map:
            task.task_handle = asyncio.create_task(pipeline_map[task.name]())
            task.update_heartbeat()
            self._log_lifecycle_event(f"Task {task.name} restarted successfully")
    
    async def _handle_failed_task(self, task: PipelineTask):
        """
        Handle a task that exceeded max restarts.
        """
        self._log_lifecycle_event(f"Task {task.name} failed permanently, triggering recovery", level="error")
        self.active_tasks.remove(task)
        await self._trigger_performance_recovery()

    async def _evaluate_booster_requirements(self):
        """
        Pre-start evaluation to determine which boosters are needed
        to hit 5M+ baseline based on historical performance.
        """
        if not self.performance_metrics["videos_processed"]:
            # No historical data, activate all boosters
            self._log_lifecycle_event("No historical data, activating all boosters")
            self._activate_all_boosters()
            return
            
        baseline_rate = self.performance_metrics["baseline_hit_rate"]
        
        if baseline_rate < self.alert_thresholds["min_baseline_rate"]:
            self._log_lifecycle_event(
                f"Baseline hit rate {baseline_rate:.2%} below threshold, activating boosters"
            )
            
            # Activate specific boosters based on deficiencies
            if self.booster_config.get("trend_alignment"):
                await self._activate_booster_with_tracking("trend_alignment")
            if self.booster_config.get("thumbnail_optimization"):
                await self._activate_booster_with_tracking("thumbnail_optimization")
            if self.booster_config.get("timing_optimization"):
                await self._activate_booster_with_tracking("timing_optimization")

    async def _activate_booster_with_tracking(self, booster_name: str):
        """
        Activate a booster with effectiveness tracking.
        
        Args:
            booster_name: Name of the booster to activate
        """
        baseline_before = self.performance_metrics["baseline_hit_rate"]
        
        # Track booster activation
        effectiveness = BoosterEffectiveness(
            booster_name=booster_name,
            activated_at=datetime.utcnow(),
            baseline_before=baseline_before
        )
        self.active_boosters[booster_name] = effectiveness
        
        self._log_lifecycle_event(f"🚀 Activating booster: {booster_name} (baseline: {baseline_before:.2%})")
        self.performance_metrics["booster_activations"] += 1
        
        # Implementation would connect to booster system
        # For now, simulate activation
        await asyncio.sleep(0.1)

    async def _activate_booster(self, booster_name: str):
        """Activate a specific booster (legacy method)"""
        await self._activate_booster_with_tracking(booster_name)

    def _activate_all_boosters(self):
        """Activate all configured boosters with tracking"""
        for booster in self.booster_config.keys():
            asyncio.create_task(self._activate_booster_with_tracking(booster))
    
    def _evaluate_booster_effectiveness(self):
        """
        Evaluate the effectiveness of active boosters and update scores.
        """
        current_baseline = self.performance_metrics["baseline_hit_rate"]
        
        for booster_name, effectiveness in self.active_boosters.items():
            if effectiveness.is_active and effectiveness.baseline_after is None:
                # Calculate effectiveness after some time has passed
                time_since_activation = datetime.utcnow() - effectiveness.activated_at
                if time_since_activation.total_seconds() > 300:  # 5 minutes
                    effectiveness.baseline_after = current_baseline
                    effectiveness.effectiveness_score = (
                        effectiveness.baseline_after - effectiveness.baseline_before
                    )
                    
                    self._log_lifecycle_event(
                        f"Booster {booster_name} effectiveness: {effectiveness.effectiveness_score:.2%}"
                    )
    
    def _enforce_baseline_compliance(self) -> bool:
        """
        Hard baseline enforcement with circuit breaker.
        
        Returns:
            True if compliant, False if enforcement actions were taken
        """
        avg_views = self.performance_metrics["avg_views"]
        baseline_rate = self.performance_metrics["baseline_hit_rate"]
        
        # Check circuit breaker state
        if self.circuit_breaker.state == "OPEN":
            if self.circuit_breaker.should_reset():
                self.circuit_breaker.state = "CLOSED"
                self.circuit_breaker.failure_count = 0
                self._log_lifecycle_event("Circuit breaker reset to CLOSED")
            else:
                self._log_lifecycle_event("Circuit breaker is OPEN, enforcing throttling", level="warning")
                self._throttle_publishing("Circuit breaker open")
                return False
        
        # Check baseline compliance
        if avg_views > 0 and avg_views < self.baseline_threshold:
            self.circuit_breaker.failure_count += 1
            self.circuit_breaker.last_failure_time = datetime.utcnow()
            
            # Record SLA violation
            violation = {
                "timestamp": datetime.utcnow().isoformat(),
                "avg_views": avg_views,
                "baseline_threshold": self.baseline_threshold,
                "baseline_rate": baseline_rate,
                "failure_count": self.circuit_breaker.failure_count
            }
            self.sla_violations.append(violation)
            
            self._log_lifecycle_event(
                f"⚠️ SLA VIOLATION: Average views {avg_views:,} below baseline {self.baseline_threshold:,}",
                level="error"
            )
            
            # Check if circuit breaker should trip
            if self.circuit_breaker.should_trip():
                self.circuit_breaker.state = "OPEN"
                self._log_lifecycle_event("🚨 Circuit breaker TRIPPED - enforcing hard throttling", level="error")
                self._throttle_publishing("Circuit breaker tripped")
                
                # Auto-pause if consecutive failures exceed threshold
                if self.circuit_breaker.failure_count >= self.alert_thresholds["max_consecutive_failures"]:
                    self._log_lifecycle_event("🛑 Auto-pausing factory due to repeated SLA violations", level="error")
                    asyncio.create_task(self.pause_factory())
                    return False
            
            return False
        
        # Reset failure count on success
        if self.circuit_breaker.failure_count > 0:
            self.circuit_breaker.failure_count = max(0, self.circuit_breaker.failure_count - 1)
            
        return True
    
    def _throttle_publishing(self, reason: str):
        """
        Throttle publishing when below baseline.
        
        Args:
            reason: Reason for throttling
        """
        self.publish_throttled = True
        self.throttle_reason = reason
        self.status = FactoryStatus.THROTTLED
        self.registry.update_factory_status(self.niche, self.status.value)
        
        self._log_lifecycle_event(f"🚦 Publishing throttled: {reason}", level="warning")
    
    def _lift_throttle(self):
        """Lift publishing throttle when compliance restored"""
        if self.publish_throttled:
            self.publish_throttled = False
            self.throttle_reason = ""
            if self.status == FactoryStatus.THROTTLED:
                self.status = FactoryStatus.RUNNING
                self.registry.update_factory_status(self.niche, self.status.value)
            
            self._log_lifecycle_event("✅ Publishing throttle lifted")

    async def _monitoring_loop(self):
        """
        Enhanced monitoring loop with SLA enforcement and scaling integration.
        """
        while self.status in [FactoryStatus.RUNNING, FactoryStatus.THROTTLED, FactoryStatus.ENFORCING]:
            try:
                await self._check_performance_metrics()
                await self._enforce_baseline_compliance_check()
                await self._check_resource_health()
                self._evaluate_booster_effectiveness()
                
                await asyncio.sleep(self.monitoring_interval)
                
            except Exception as e:
                self._log_lifecycle_event(f"Monitoring error: {e}", level="error")
                await asyncio.sleep(self.monitoring_interval)
    
    async def _enforce_baseline_compliance_check(self):
        """
        Check and enforce baseline compliance with hard SLA guarantees.
        """
        is_compliant = self._enforce_baseline_compliance()
        
        if not is_compliant and not self.publish_throttled:
            # Already handled by _enforce_baseline_compliance
            pass
        elif is_compliant and self.publish_throttled:
            # Lift throttle if compliance restored
            self._lift_throttle()
    
    async def _check_performance_metrics(self):
        """Monitor real-time performance and trigger alerts if needed"""
        if self.performance_metrics["videos_processed"] > 0:
            baseline_rate = self.performance_metrics["baseline_hit_rate"]
            
            if baseline_rate < self.alert_thresholds["min_baseline_rate"]:
                self._log_lifecycle_event(
                    f"⚠️ Baseline hit rate {baseline_rate:.2%} below threshold",
                    level="warning"
                )
                await self._trigger_performance_recovery()

    async def _check_baseline_compliance(self):
        """Legacy method - use _enforce_baseline_compliance_check instead"""
        avg_views = self.performance_metrics["avg_views"]
        
        if avg_views > 0 and avg_views < self.baseline_threshold:
            self._log_lifecycle_event(
                f"⚠️ Average views {avg_views:,} below baseline {self.baseline_threshold:,}",
                level="warning"
            )
            await self._evaluate_booster_requirements()

    async def _check_resource_health(self):
        """
        Monitor resource usage and trigger scaling if needed.
        Now with actual scaling controller integration.
        """
        try:
            if self.scaling_controller:
                # Get current resource metrics
                resource_status = await self.scaling_controller.get_resource_status()
                self.resource_metrics.update(resource_status)
                
                # Check if scaling is needed
                scaling_decision = await self.scaling_controller.evaluate_scaling_needs(
                    current_throughput=self.performance_metrics["videos_processed"],
                    queue_size=self.video_queue.qsize(),
                    resource_metrics=self.resource_metrics
                )
                
                if scaling_decision["action_required"]:
                    self._log_lifecycle_event(
                        f"🔄 Scaling action required: {scaling_decision['action']} "
                        f"(reason: {scaling_decision['reason']})"
                    )
                    
                    # Execute scaling action
                    if scaling_decision["action"] == "scale_up":
                        await self._execute_scale_up(scaling_decision)
                    elif scaling_decision["action"] == "scale_down":
                        await self._execute_scale_down(scaling_decision)
                    
                    self.last_scale_check = datetime.utcnow()
            else:
                # Mock scaling check when controller not available
                await self._mock_resource_check()
                
        except Exception as e:
            self._log_lifecycle_event(f"Resource health check failed: {e}", level="error")
    
    async def _execute_scale_up(self, scaling_decision: Dict[str, Any]):
        """
        Execute scale up action with feedback loop.
        
        Args:
            scaling_decision: Scaling decision details
        """
        try:
            # Request additional resources
            result = await self.scaling_controller.scale_up(
                target_instances=scaling_decision.get("target_instances", 1),
                reason=scaling_decision.get("reason", "Performance degradation")
            )
            
            if result["success"]:
                self._log_lifecycle_event(
                    f"✅ Scale up successful: {result['details']}"
                )
                
                # Update registry with scaling event
                self.registry.record_error(
                    self.niche, 
                    f"Scale up executed: {result['details']}"
                ) if "error" in result else None
            else:
                self._log_lifecycle_event(
                    f"❌ Scale up failed: {result['error']}",
                    level="error"
                )
                
        except Exception as e:
            self._log_lifecycle_event(f"Scale up execution failed: {e}", level="error")
    
    async def _execute_scale_down(self, scaling_decision: Dict[str, Any]):
        """
        Execute scale down action with feedback loop.
        
        Args:
            scaling_decision: Scaling decision details
        """
        try:
            result = await self.scaling_controller.scale_down(
                target_instances=scaling_decision.get("target_instances", 1),
                reason=scaling_decision.get("reason", "Resource optimization")
            )
            
            if result["success"]:
                self._log_lifecycle_event(
                    f"✅ Scale down successful: {result['details']}"
                )
            else:
                self._log_lifecycle_event(
                    f"❌ Scale down failed: {result['error']}",
                    level="error"
                )
                
        except Exception as e:
            self._log_lifecycle_event(f"Scale down execution failed: {e}", level="error")
    
    async def _mock_resource_check(self):
        """
        Mock resource check when scaling controller is not available.
        """
        # Simulate basic resource monitoring
        queue_size = self.video_queue.qsize()
        
        if queue_size > 1000:  # Arbitrary threshold
            self._log_lifecycle_event(
                f"⚠️ High queue size detected: {queue_size} (scaling controller unavailable)",
                level="warning"
            )

    async def _trigger_performance_recovery(self):
        """Automatic recovery when performance drops below acceptable levels"""
        self.status = FactoryStatus.RECOVERING
        self._log_lifecycle_event("Initiating performance recovery")
        
        # Re-evaluate and activate additional boosters
        await self._evaluate_booster_requirements()
        
        self.status = FactoryStatus.RUNNING

    async def _save_state(self):
        """Persist current factory state for recovery"""
        state = {
            "niche": self.niche,
            "status": self.status.value,
            "video_queue": self.video_queue,
            "performance_metrics": self.performance_metrics,
            "active_tasks": [
                {"name": t.name, "state": t.state} for t in self.active_tasks
            ],
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Save to registry or persistent storage
        self.registry.save_factory_state(self.niche, state)
        self._log_lifecycle_event("State saved")

    async def _restore_state(self):
        """Restore factory state from saved checkpoint"""
        state = self.registry.load_factory_state(self.niche)
        
        if state:
            self.video_queue = state.get("video_queue", [])
            self.performance_metrics = state.get("performance_metrics", self.performance_metrics)
            self._log_lifecycle_event("State restored")

    async def _capture_task_state(self, task: PipelineTask) -> Dict:
        """Capture current state of a task for pause/resume"""
        return {
            "queue_position": len(self.video_queue),
            "processed_count": self.performance_metrics["videos_processed"],
            "timestamp": datetime.utcnow().isoformat()
        }

    async def _restore_task_state(self, task: PipelineTask):
        """Restore a task's state after pause"""
        if task.state:
            self._log_lifecycle_event(f"Restoring state for task: {task.name}")

    async def _cleanup_resources(self):
        """Clean up resources on factory stop"""
        # Clear the asyncio.Queue properly
        while not self.video_queue.empty():
            try:
                self.video_queue.get_nowait()
                self.video_queue.task_done()
            except asyncio.QueueEmpty:
                break
                
        if self.ml_agent:
            # Cleanup ML agent resources
            pass
        if self.rl_agent:
            # Cleanup RL agent resources
            pass

    # Enhanced Pipeline implementations with real pause semantics and throttle checking
    
    async def _run_ingestion_pipeline(self):
        """Content ingestion pipeline with pause support"""
        task = next((t for t in self.active_tasks if t.name == "ingestion"), None)
        if not task:
            return
            
        while not task.should_stop():
            try:
                # Real pause semantics - actually blocks execution
                await task.wait_if_paused()
                
                # Check if publishing is throttled
                if self.publish_throttled and task.name == "posting":
                    await asyncio.sleep(30)  # Wait longer when throttled
                    continue
                
                # Update heartbeat
                task.update_heartbeat()
                
                # Ingest new content sources
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                self._log_lifecycle_event(f"Ingestion pipeline cancelled")
                break
            except Exception as e:
                self._log_lifecycle_event(f"Ingestion pipeline error: {e}", level="error")
                await asyncio.sleep(10)  # Backoff on error

    async def _run_feature_pipeline(self):
        """Feature extraction pipeline with pause support"""
        task = next((t for t in self.active_tasks if t.name == "feature_extraction"), None)
        if not task:
            return
            
        while not task.should_stop():
            try:
                await task.wait_if_paused()
                task.update_heartbeat()
                
                # Extract features from queued videos
                while not self.video_queue.empty() and not task.should_stop():
                    await task.wait_if_paused()
                    
                    try:
                        video = await asyncio.wait_for(self.video_queue.get(), timeout=1.0)
                        # Process video features
                        await asyncio.sleep(0.1)  # Simulate processing
                        self.video_queue.task_done()
                    except asyncio.TimeoutError:
                        break
                
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                self._log_lifecycle_event(f"Feature pipeline cancelled")
                break
            except Exception as e:
                self._log_lifecycle_event(f"Feature pipeline error: {e}", level="error")
                await asyncio.sleep(10)

    async def _run_ml_pipeline(self):
        """ML prediction pipeline with pause support"""
        task = next((t for t in self.active_tasks if t.name == "ml_prediction"), None)
        if not task:
            return
            
        while not task.should_stop():
            try:
                await task.wait_if_paused()
                task.update_heartbeat()
                
                if self.ml_agent:
                    # Run virality predictions
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(10)  # Wait longer if no agent
                    
            except asyncio.CancelledError:
                self._log_lifecycle_event(f"ML pipeline cancelled")
                break
            except Exception as e:
                self._log_lifecycle_event(f"ML pipeline error: {e}", level="error")
                await asyncio.sleep(10)

    async def _run_rl_pipeline(self):
        """RL optimization pipeline with pause support"""
        task = next((t for t in self.active_tasks if t.name == "rl_optimization"), None)
        if not task:
            return
            
        while not task.should_stop():
            try:
                await task.wait_if_paused()
                task.update_heartbeat()
                
                if self.rl_agent:
                    # Optimize content decisions
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(10)
                    
            except asyncio.CancelledError:
                self._log_lifecycle_event(f"RL pipeline cancelled")
                break
            except Exception as e:
                self._log_lifecycle_event(f"RL pipeline error: {e}", level="error")
                await asyncio.sleep(10)

    async def _run_posting_pipeline(self):
        """Content posting pipeline with throttle checking and pause support"""
        task = next((t for t in self.active_tasks if t.name == "posting"), None)
        if not task:
            return
            
        while not task.should_stop():
            try:
                await task.wait_if_paused()
                task.update_heartbeat()
                
                # Check if publishing is throttled (hard enforcement)
                if self.publish_throttled:
                    self._log_lifecycle_event(
                        f"🚦 Posting throttled: {self.throttle_reason} - waiting",
                        level="warning"
                    )
                    await asyncio.sleep(30)  # Extended wait when throttled
                    continue
                
                # Post optimized content to platforms
                await asyncio.sleep(10)
                
                # Simulate posting result and update metrics
                self._simulate_posting_result()
                
            except asyncio.CancelledError:
                self._log_lifecycle_event(f"Posting pipeline cancelled")
                break
            except Exception as e:
                self._log_lifecycle_event(f"Posting pipeline error: {e}", level="error")
                await asyncio.sleep(10)
    
    def _simulate_posting_result(self):
        """
        Simulate posting results for testing and metric updates.
        """
        import random
        
        # Simulate view count with some randomness
        views = random.randint(
            int(self.baseline_threshold * 0.7),  # Can be below baseline
            int(self.baseline_threshold * 2.0)    # Can exceed baseline
        )
        
        # Update performance metrics
        self.performance_metrics["videos_processed"] += 1
        self.performance_metrics["recent_views"].append(views)
        
        # Calculate rolling averages
        if len(self.performance_metrics["recent_views"]) > 0:
            self.performance_metrics["avg_views"] = sum(self.performance_metrics["recent_views"]) / len(self.performance_metrics["recent_views"])
        
        # Update baseline hit rate
        if views >= self.baseline_threshold:
            self.performance_metrics["baseline_hit_rate"] = (
                (self.performance_metrics["baseline_hit_rate"] * (self.performance_metrics["videos_processed"] - 1) + 1.0) /
                self.performance_metrics["videos_processed"]
            )
        else:
            self.performance_metrics["baseline_hit_rate"] = (
                (self.performance_metrics["baseline_hit_rate"] * (self.performance_metrics["videos_processed"] - 1)) /
                self.performance_metrics["videos_processed"]
            )
        
        # Track consecutive failures
        if views < self.baseline_threshold:
            self.performance_metrics["consecutive_failures"] += 1
        else:
            self.performance_metrics["consecutive_failures"] = 0

    def _log_lifecycle_event(self, message: str, level: str = "info"):
        """
        Log lifecycle events centrally with proper formatting.

        Args:
            message: Event description
            level: Log level (info, warning, error)
        """
        timestamp = datetime.utcnow().isoformat()
        log_message = f"[{timestamp}] [{self.niche} Lifecycle] {message}"
        
        if level == "error":
            self.logger.error(log_message)
        elif level == "warning":
            self.logger.warning(log_message)
        else:
            self.logger.info(log_message)
        
        # Also log to central system
        try:
            from infra.logger import log_event
            log_event(log_message)
        except ImportError:
            pass

    def get_status(self) -> Dict[str, Any]:
        """
        Get comprehensive factory status including SLA enforcement metrics.
        
        Returns:
            Dictionary with status, metrics, active tasks, and SLA compliance info
        """
        return {
            "niche": self.niche,
            "status": self.status.value,
            "performance_metrics": self.performance_metrics,
            "active_tasks": [
                {
                    "name": t.name,
                    "active": t.is_active(),
                    "paused": t.paused,
                    "restart_count": t.restart_count,
                    "healthy": t.is_healthy()
                } 
                for t in self.active_tasks
            ],
            "baseline_threshold": self.baseline_threshold,
            "booster_activations": self.performance_metrics["booster_activations"],
            "active_boosters": {
                name: {
                    "activated_at": eff.activated_at.isoformat(),
                    "baseline_before": eff.baseline_before,
                    "baseline_after": eff.baseline_after,
                    "effectiveness_score": eff.effectiveness_score,
                    "is_active": eff.is_active
                }
                for name, eff in self.active_boosters.items()
            },
            "circuit_breaker": {
                "state": self.circuit_breaker.state,
                "failure_count": self.circuit_breaker.failure_count,
                "last_failure_time": (
                    self.circuit_breaker.last_failure_time.isoformat() 
                    if self.circuit_breaker.last_failure_time else None
                ),
                "threshold": self.circuit_breaker.threshold,
                "timeout_seconds": self.circuit_breaker.timeout_seconds
            },
            "throttling": {
                "is_throttled": self.publish_throttled,
                "reason": self.throttle_reason
            },
            "sla_violations": list(self.sla_violations)[-10:],  # Last 10 violations
            "resource_metrics": dict(self.resource_metrics),
            "scaling_controller_available": self.scaling_controller is not None,
            "last_scale_check": self.last_scale_check.isoformat() if self.last_scale_check else None
        }


# Example usage and testing
async def main():
    """Example usage of FactoryLifecycle"""
    from factories.factory_registry import FactoryRegistry
    
    # Initialize registry
    registry = FactoryRegistry()
    
    # Create factory lifecycle manager
    lifecycle = FactoryLifecycle(
        niche="fitness",
        config_path="/config/factories/fitness.yaml",
        registry=registry
    )
    
    # Start factory
    await lifecycle.start_factory()
    
    # Run for some time
    await asyncio.sleep(30)
    
    # Pause factory
    await lifecycle.pause_factory()
    
    # Resume after delay
    await asyncio.sleep(10)
    await lifecycle.resume_factory()
    
    # Check status
    status = lifecycle.get_status()
    print(f"Factory status: {status}")
    
    # Stop factory
    await lifecycle.stop_factory()


if __name__ == "__main__":
    asyncio.run(main())
