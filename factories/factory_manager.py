"""
Factory Manager - Core Controller for Content Factory Operations

Orchestrates all niche factories, ensuring content production, posting, and 
virality optimization to meet baseline and high-end view targets.

Location: /factories/factory_manager.py
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import json
from pathlib import Path
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import random
import numpy as np


class BaselineEnforcement(Enum):
    """Baseline enforcement policies"""
    MONITOR_ONLY = "monitor_only"
    SOFT_BLOCK = "soft_block"
    HARD_BLOCK = "hard_block"
    AUTO_REGENERATE = "auto_regenerate"


class FactoryState(Enum):
    """Factory operational states"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"
    SCALING = "scaling"
    BACKPRESSURE = "backpressure"


@dataclass
class ContentQueueItem:
    """Represents a content item in the generation queue"""
    id: str
    niche: str
    content_params: Dict[str, Any]
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    scheduled_time: Optional[datetime] = None
    projected_views: int = 0
    baseline_compliant: bool = False
    retry_count: int = 0
    max_retries: int = 3


@dataclass
class WorkerPoolMetrics:
    """Worker pool performance metrics"""
    active_workers: int = 0
    queued_items: int = 0
    processing_rate: float = 0.0
    avg_processing_time: float = 0.0
    success_rate: float = 0.0
    backpressure_active: bool = False


@dataclass
class ScalingMetrics:
    """System scaling metrics"""
    current_capacity: int = 0
    target_capacity: int = 0
    scale_events_24h: int = 0
    resource_utilization: float = 0.0
    queue_depth: int = 0
    throughput_per_minute: float = 0.0


@dataclass
class FactoryMetrics:
    """Performance metrics for a factory"""
    total_videos_produced: int = 0
    total_views: int = 0
    avg_views_per_video: float = 0.0
    videos_above_baseline: int = 0
    videos_below_baseline: int = 0
    viral_videos_30m_plus: int = 0
    viral_videos_300m_plus: int = 0
    last_updated: datetime = field(default_factory=datetime.now)
    
    def calculate_baseline_hit_rate(self, baseline: int = 5_000_000) -> float:
        """Calculate percentage of videos hitting baseline"""
        if self.total_videos_produced == 0:
            return 0.0
        return (self.videos_above_baseline / self.total_videos_produced) * 100


@dataclass
class FactoryInstance:
    """Represents a single niche factory instance"""
    niche: str
    state: FactoryState
    config: Dict[str, Any]
    metrics: FactoryMetrics
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    scheduled_posts: List[Dict] = field(default_factory=list)
    active_boosters: List[str] = field(default_factory=list)
    resource_allocation: Dict[str, Any] = field(default_factory=dict)
    content_queue: queue.PriorityQueue = field(default_factory=lambda: queue.PriorityQueue())
    worker_pool: Optional[ThreadPoolExecutor] = None
    worker_metrics: WorkerPoolMetrics = field(default_factory=WorkerPoolMetrics)
    scaling_metrics: ScalingMetrics = field(default_factory=ScalingMetrics)
    baseline_enforcement: BaselineEnforcement = BaselineEnforcement.HARD_BLOCK
    rl_policy_cache: Dict[str, Any] = field(default_factory=dict)
    trend_cache: Dict[str, Any] = field(default_factory=dict)
    feature_cache: Dict[str, Any] = field(default_factory=dict)


class FactoryManager:
    """
    Orchestrates all niche factories, ensuring content production,
    posting, and virality optimization to meet baseline and high-end goals.
    
    Responsibilities:
    - Factory lifecycle management (start, stop, pause, resume)
    - Content production scheduling
    - Integration with ingestion and feature pipelines
    - Performance monitoring and alerting
    - RL agent integration for optimization
    - Horizontal scaling management
    """

    def __init__(
        self, 
        config_loader,
        data_dir: str,
        rl_agent_manager,
        baseline_views: int = 5_000_000,
        viral_tier_1: int = 30_000_000,
        viral_tier_2: int = 300_000_000
    ):
        """
        Initialize the Factory Manager.
        
        Args:
            config_loader: Loads global & per-niche YAML configs
            data_dir: Path to processed data and historical engagement
            rl_agent_manager: Manages RL agents for automation
            baseline_views: Minimum target views per video (default: 5M)
            viral_tier_1: First viral tier target (default: 30M)
            viral_tier_2: Second viral tier target (default: 300M)
        """
        self.config_loader = config_loader
        self.data_dir = Path(data_dir)
        self.rl_agent_manager = rl_agent_manager
        
        # View targets
        self.baseline_views = baseline_views
        self.viral_tier_1 = viral_tier_1
        self.viral_tier_2 = viral_tier_2
        
        # Factory registry
        self.factories: Dict[str, FactoryInstance] = {}
        
        # Monitoring state
        self.monitoring_active = False
        self.monitoring_task: Optional[asyncio.Task] = None
        
        # Performance tracking
        self.global_metrics = {
            'total_factories': 0,
            'active_factories': 0,
            'total_videos': 0,
            'total_views': 0,
            'baseline_hit_rate': 0.0,
            'system_throughput': 0.0,
            'avg_processing_time': 0.0,
            'backpressure_events': 0
        }
        
        # Worker pool management
        self.global_worker_pool: Optional[ThreadPoolExecutor] = None
        self.max_workers = 100  # Global worker limit
        self.worker_timeout = 300  # 5 minutes per task
        
        # Queue management
        self.global_queue: queue.PriorityQueue = field(default_factory=lambda: queue.PriorityQueue())
        self.queue_max_depth = 10000
        self.backpressure_threshold = 0.8  # Trigger at 80% capacity
        
        # Scaling configuration
        self.scaling_enabled = True
        self.scale_up_threshold = 0.9
        self.scale_down_threshold = 0.3
        self.min_workers_per_factory = 2
        self.max_workers_per_factory = 20
        
        # RL integration
        self.rl_decision_timeout = 30  # seconds
        self.rl_fallback_enabled = True
        
        # Trend pipeline
        self.trend_update_interval = 300  # 5 minutes
        self.feature_update_interval = 600  # 10 minutes
        
        # Initialize logger
        self.logger = logging.getLogger(__name__)
        
        # Initialize global worker pool
        self._initialize_global_worker_pool()
        
        # Load existing factories
        self._load_factories()

    def _initialize_global_worker_pool(self) -> None:
        """
        Initialize the global worker pool for shared tasks.
        
        This pool is used for cross-factory operations and shared resources.
        Individual factories have their own worker pools for niche-specific tasks.
        """
        if self.global_worker_pool is None:
            self.global_worker_pool = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="GlobalWorker"
            )
            self.logger.info(f"Initialized global worker pool with {self.max_workers} workers")

    def _load_factories(self) -> None:
        """
        Load all niche factories with their configs and state.
        
        Discovers available factory configs and initializes FactoryInstance
        objects for each niche.
        """
        try:
            # Load global config
            global_config = self.config_loader.load_global_config()
            
            # Load all factory configs from /config/factories/*.yaml
            factory_configs = self.config_loader.load_all_factory_configs()
            
            for niche, config in factory_configs.items():
                # Load historical state if exists
                state_file = self.data_dir / 'factory_states' / f'{niche}.json'
                
                if state_file.exists():
                    with open(state_file, 'r') as f:
                        saved_state = json.load(f)
                    state = FactoryState(saved_state.get('state', 'stopped'))
                    metrics_data = saved_state.get('metrics', {})
                    metrics = FactoryMetrics(**metrics_data)
                else:
                    state = FactoryState.STOPPED
                    metrics = FactoryMetrics()
                
                # Create factory instance
                factory = FactoryInstance(
                    niche=niche,
                    state=state,
                    config=config,
                    metrics=metrics,
                    resource_allocation=self._calculate_resource_allocation(config)
                )
                
                self.factories[niche] = factory
                self.logger.info(f"Loaded factory: {niche} (state: {state.value})")
            
            self._update_global_metrics()
            
        except Exception as e:
            self.logger.error(f"Error loading factories: {e}", exc_info=True)
            raise

    def _calculate_resource_allocation(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate GPU/CPU/memory allocation based on expected content output.
        
        Args:
            config: Factory configuration
            
        Returns:
            Resource allocation dictionary
        """
        videos_per_day = config.get('videos_per_day', 10)
        complexity = config.get('content_complexity', 'medium')
        
        # Base allocation
        if complexity == 'low':
            gpu_units = 0.5
            cpu_cores = 2
            memory_gb = 4
        elif complexity == 'medium':
            gpu_units = 1.0
            cpu_cores = 4
            memory_gb = 8
        else:  # high
            gpu_units = 2.0
            cpu_cores = 8
            memory_gb = 16
        
        # Scale by volume
        volume_multiplier = videos_per_day / 10
        
        return {
            'gpu_units': gpu_units * volume_multiplier,
            'cpu_cores': int(cpu_cores * volume_multiplier),
            'memory_gb': int(memory_gb * volume_multiplier),
            'estimated_videos_per_day': videos_per_day
        }

    async def start_factory(self, niche: str) -> bool:
        """
        Starts a factory for a given niche.
        
        - Allocates resources
        - Schedules content production
        - Connects ingestion → feature extraction → ML → RL pipeline
        
        Args:
            niche: Name of the niche factory to start
            
        Returns:
            True if started successfully, False otherwise
        """
        if niche not in self.factories:
            self.logger.error(f"Factory not found: {niche}")
            return False
        
        factory = self.factories[niche]
        
        if factory.state == FactoryState.RUNNING:
            self.logger.warning(f"Factory {niche} is already running")
            return True
        
        try:
            self.logger.info(f"Starting factory: {niche}")
            factory.state = FactoryState.STARTING
            
            # 1. Allocate resources
            await self._allocate_resources(factory)
            
            # 2. Initialize content pipeline connection
            await self._connect_content_pipeline(factory)
            
            # 3. Connect to trend aggregator
            await self._connect_trend_aggregator(factory)
            
            # 4. Connect to feature extraction engine
            await self._connect_feature_engine(factory)
            
            # 5. Initialize RL agent for this factory
            await self._initialize_rl_agent(factory)
            
            # 6. Schedule initial content batch
            await self._schedule_initial_content(factory)
            
            # Update state
            factory.state = FactoryState.RUNNING
            factory.last_activity = datetime.now()
            
            # Save state
            self._save_factory_state(factory)
            
            self.logger.info(f"Factory {niche} started successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error starting factory {niche}: {e}", exc_info=True)
            factory.state = FactoryState.ERROR
            return False

    async def stop_factory(self, niche: str, graceful: bool = True) -> bool:
        """
        Stops a factory safely, saving current state and metrics.
        
        Args:
            niche: Name of the niche factory to stop
            graceful: If True, waits for pending operations to complete
            
        Returns:
            True if stopped successfully, False otherwise
        """
        if niche not in self.factories:
            self.logger.error(f"Factory not found: {niche}")
            return False
        
        factory = self.factories[niche]
        
        try:
            self.logger.info(f"Stopping factory: {niche} (graceful={graceful})")
            factory.state = FactoryState.STOPPING
            
            if graceful:
                # Wait for pending content to finish
                await self._wait_for_pending_operations(factory)
            
            # Disconnect pipelines
            await self._disconnect_pipelines(factory)
            
            # Release resources
            await self._release_resources(factory)
            
            # Save final state and metrics
            self._save_factory_state(factory)
            
            factory.state = FactoryState.STOPPED
            factory.last_activity = datetime.now()
            
            self.logger.info(f"Factory {niche} stopped successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error stopping factory {niche}: {e}", exc_info=True)
            factory.state = FactoryState.ERROR
            return False

    async def pause_factory(self, niche: str) -> bool:
        """
        Temporarily pauses content production while retaining state.
        
        Args:
            niche: Name of the niche factory to pause
            
        Returns:
            True if paused successfully, False otherwise
        """
        if niche not in self.factories:
            self.logger.error(f"Factory not found: {niche}")
            return False
        
        factory = self.factories[niche]
        
        if factory.state != FactoryState.RUNNING:
            self.logger.warning(f"Factory {niche} is not running")
            return False
        
        try:
            self.logger.info(f"Pausing factory: {niche}")
            
            # Pause scheduling but keep pipelines connected
            factory.state = FactoryState.PAUSED
            factory.last_activity = datetime.now()
            
            self._save_factory_state(factory)
            
            self.logger.info(f"Factory {niche} paused successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error pausing factory {niche}: {e}", exc_info=True)
            return False

    async def resume_factory(self, niche: str) -> bool:
        """
        Resumes a paused factory, continuing scheduled operations.
        
        Args:
            niche: Name of the niche factory to resume
            
        Returns:
            True if resumed successfully, False otherwise
        """
        if niche not in self.factories:
            self.logger.error(f"Factory not found: {niche}")
            return False
        
        factory = self.factories[niche]
        
        if factory.state != FactoryState.PAUSED:
            self.logger.warning(f"Factory {niche} is not paused")
            return False
        
        try:
            self.logger.info(f"Resuming factory: {niche}")
            
            # Resume scheduling
            factory.state = FactoryState.RUNNING
            factory.last_activity = datetime.now()
            
            # Reschedule any missed content
            await self._reschedule_missed_content(factory)
            
            self._save_factory_state(factory)
            
            self.logger.info(f"Factory {niche} resumed successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Error resuming factory {niche}: {e}", exc_info=True)
            return False

    async def monitor_factories(self) -> None:
        """
        Continuous monitoring of all factories:
        
        - Check video performance
        - Compare against baseline views
        - Trigger boosts or RL interventions if falling short
        """
        self.monitoring_active = True
        self.logger.info("Starting factory monitoring")
        
        while self.monitoring_active:
            try:
                for niche, factory in self.factories.items():
                    if factory.state == FactoryState.RUNNING:
                        await self._monitor_factory_performance(factory)
                
                # Update global metrics
                self._update_global_metrics()
                
                # Sleep before next monitoring cycle
                await asyncio.sleep(60)  # Monitor every minute
            except Exception as e:
                self.logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _monitor_factory_performance(self, factory: FactoryInstance) -> None:
        """
        Monitor individual factory performance and trigger interventions.
        Implements hard baseline enforcement with fail-closed logic.
        
        Args:
            factory: Factory instance to monitor
        """
        # Load recent video performance data
        recent_videos = await self._load_recent_videos(factory.niche)
        
        for video in recent_videos:
            video_id = video['id']
            views = video['views']
            age_hours = video['age_hours']
            
            # Calculate projected final views based on early metrics
            projected_views = await self._project_final_views(video)
            
            # Hard baseline enforcement logic
            if projected_views < self.baseline_views:
                self.logger.warning(
                    f"Video {video_id} in {factory.niche} projected below baseline: "
                    f"{projected_views:,} (target: {self.baseline_views:,})"
                )
                
                # Apply enforcement based on policy
                enforcement_result = await self._apply_baseline_enforcement(
                    factory, video_id, projected_views, video
                )
                
                if not enforcement_result:
                    # Hard block - prevent posting
                    await self._block_video_posting(factory, video_id)
                    await self._send_alert(
                        factory.niche,
                        f"Video {video_id} BLOCKED - below baseline threshold",
                        severity='critical'
                    )
                
                # Update metrics
                factory.metrics.videos_below_baseline += 1
            else:
                # Above baseline - allow posting
                factory.metrics.videos_above_baseline += 1
                await self._allow_video_posting(factory, video_id)
            
            # Update general metrics
            factory.metrics.total_videos_produced += 1
            factory.metrics.total_views += views
            
            if views >= self.viral_tier_2:
                factory.metrics.viral_videos_300m_plus += 1
            elif views >= self.viral_tier_1:
                factory.metrics.viral_videos_30m_plus += 1
            
            factory.metrics.avg_views_per_video = (
                factory.metrics.total_views / factory.metrics.total_videos_produced
            )
            factory.metrics.last_updated = datetime.now()

    async def schedule_daily_content(self) -> None:
        """
        Automatically schedules daily content for each niche:
        
        - Uses posting times from niche YAML
        - Ensures baseline target for all videos
        - Incorporates boosters: hook optimization, trending music, thumbnails
        """
        for niche, factory in self.factories.items():
            if factory.state != FactoryState.RUNNING:
                continue
            
            try:
                # Get posting schedule from config
                posting_schedule = factory.config.get('posting_schedule', [])
                videos_per_day = factory.config.get('videos_per_day', 10)
                
                # Generate content schedule for next 24 hours
                schedule = []
                
                for i in range(videos_per_day):
                    # Determine optimal posting time
                    post_time = self._calculate_optimal_post_time(
                        factory,
                        posting_schedule,
                        i
                    )
                    
                    # Load trend data
                    trends = await self._get_current_trends(factory.niche)
                    
                    # Generate content parameters with boosters
                    content_params = {
                        'niche': niche,
                        'post_time': post_time,
                        'trends': trends,
                        'boosters': self._select_boosters(factory, trends),
                        'baseline_target': self.baseline_views,
                        'viral_potential_target': self.viral_tier_1
                    }
                    
                    schedule.append(content_params)
                
                # Queue content for generation
                await self._queue_content_generation(factory, schedule)
                
                factory.scheduled_posts = schedule
                factory.last_activity = datetime.now()
                
                self.logger.info(
                    f"Scheduled {len(schedule)} videos for {niche}"
                )
                
            except Exception as e:
                self.logger.error(
                    f"Error scheduling content for {niche}: {e}",
                    exc_info=True
                )

    async def trigger_boosters(
        self,
        niche: str,
        video_id: str,
        booster_type: str = 'auto'
    ) -> bool:
        """
        Activates viral boosters for high potential videos:
        
        - Emotional arc optimization
        - Thumbnail optimization
        - Trend alignment
        - Reposting to underperforming slots
        
        Args:
            niche: Factory niche
            video_id: Video identifier
            booster_type: Type of booster ('auto', 'baseline_rescue', 'viral_push')
            
        Returns:
            True if boosters applied successfully
        """
        if niche not in self.factories:
            return False
        
        factory = self.factories[niche]
        
        try:
            self.logger.info(
                f"Triggering {booster_type} boosters for video {video_id}"
            )
            
            # Load video data
            video_data = await self._load_video_data(video_id)
            
            # Determine which boosters to apply
            boosters_to_apply = self._determine_boosters(
                factory,
                video_data,
                booster_type
            )
            
            # Apply each booster
            for booster_name in boosters_to_apply:
                await self._apply_booster(video_id, booster_name, video_data)
            
            # Track active boosters
            factory.active_boosters.extend(boosters_to_apply)
            
            # Notify RL agent
            await self._notify_rl_agent(factory, video_id, boosters_to_apply)
            
            return True
            
        except Exception as e:
            self.logger.error(
                f"Error applying boosters: {e}",
                exc_info=True
            )
            return False

    def _select_boosters(
        self,
        factory: FactoryInstance,
        trends: Dict[str, Any]
    ) -> List[str]:
        """
        Select appropriate boosters based on factory config and trends.
        
        Args:
            factory: Factory instance
            trends: Current trend data
            
        Returns:
            List of booster names to apply
        """
        available_boosters = factory.config.get('available_boosters', [])
        selected = []
        
        # Hook optimization (always include for baseline)
        if 'hook_optimization' in available_boosters:
            selected.append('hook_optimization')
        
        # Trend alignment
        if trends.get('trending_topics') and 'trend_alignment' in available_boosters:
            selected.append('trend_alignment')
        
        # Trending music
        if trends.get('trending_music') and 'trending_music' in available_boosters:
            selected.append('trending_music')
        
        # Thumbnail optimization
        if 'thumbnail_optimization' in available_boosters:
            selected.append('thumbnail_optimization')
        
        # Emotional arc (for high-potential videos)
        if factory.metrics.viral_videos_30m_plus > 0:
            if 'emotional_arc' in available_boosters:
                selected.append('emotional_arc')
        
        return selected

    def _save_factory_state(self, factory: FactoryInstance) -> None:
        """Save factory state to disk"""
        try:
            state_dir = self.data_dir / 'factory_states'
            state_dir.mkdir(parents=True, exist_ok=True)
            
            state_file = state_dir / f'{factory.niche}.json'
            
            state_data = {
                'niche': factory.niche,
                'state': factory.state.value,
                'metrics': {
                    'total_videos_produced': factory.metrics.total_videos_produced,
                    'total_views': factory.metrics.total_views,
                    'avg_views_per_video': factory.metrics.avg_views_per_video,
                    'videos_above_baseline': factory.metrics.videos_above_baseline,
                    'videos_below_baseline': factory.metrics.videos_below_baseline,
                    'viral_videos_30m_plus': factory.metrics.viral_videos_30m_plus,
                    'viral_videos_300m_plus': factory.metrics.viral_videos_300m_plus,
                    'last_updated': factory.metrics.last_updated.isoformat()
                },
                'last_activity': factory.last_activity.isoformat(),
                'resource_allocation': factory.resource_allocation
            }
            
            with open(state_file, 'w') as f:
                json.dump(state_data, f, indent=2)
                
        except Exception as e:
            self.logger.error(f"Error saving factory state: {e}", exc_info=True)

    def _update_global_metrics(self) -> None:
        """Update global performance metrics"""
        total_factories = len(self.factories)
        active_factories = sum(
            1 for f in self.factories.values()
            if f.state == FactoryState.RUNNING
        )
        
        total_videos = sum(
            f.metrics.total_videos_produced
            for f in self.factories.values()
        )
        
        total_views = sum(
            f.metrics.total_views
            for f in self.factories.values()
        )
        
        videos_above_baseline = sum(
            f.metrics.videos_above_baseline
            for f in self.factories.values()
        )
        
        baseline_hit_rate = (
            (videos_above_baseline / total_videos * 100)
            if total_videos > 0 else 0.0
        )
        
        self.global_metrics = {
            'total_factories': total_factories,
            'active_factories': active_factories,
            'total_videos': total_videos,
            'total_views': total_views,
            'baseline_hit_rate': baseline_hit_rate
        }

    async def _apply_baseline_enforcement(
        self, 
        factory: FactoryInstance, 
        video_id: str, 
        projected_views: int, 
        video_data: Dict
    ) -> bool:
        """
        Apply baseline enforcement policy based on factory configuration.
        
        Returns:
            True if video should be allowed, False if blocked
        """
        if factory.baseline_enforcement == BaselineEnforcement.MONITOR_ONLY:
            # Just monitor, don't block
            await self.trigger_boosters(factory.niche, video_id, 'baseline_rescue')
            return True
            
        elif factory.baseline_enforcement == BaselineEnforcement.SOFT_BLOCK:
            # Try to rescue first, then block if fails
            rescue_success = await self._attempt_baseline_rescue(
                factory, video_id, projected_views, video_data
            )
            return rescue_success
            
        elif factory.baseline_enforcement == BaselineEnforcement.HARD_BLOCK:
            # Immediate block for anything below baseline
            return False
            
        elif factory.baseline_enforcement == BaselineEnforcement.AUTO_REGENERATE:
            # Try to regenerate content that meets baseline
            regeneration_success = await self._attempt_content_regeneration(
                factory, video_id, video_data
            )
            return regeneration_success
            
        return False

    async def _attempt_baseline_rescue(
        self, 
        factory: FactoryInstance, 
        video_id: str, 
        projected_views: int, 
        video_data: Dict
    ) -> bool:
        """
        Attempt to rescue a video below baseline using boosters.
        
        Returns:
            True if rescue successful, False otherwise
        """
        try:
            # Calculate deficit from baseline
            view_deficit = self.baseline_views - projected_views
            deficit_ratio = view_deficit / self.baseline_views
            
            # Determine rescue strategy based on deficit
            if deficit_ratio > 0.5:  # More than 50% below baseline
                # Aggressive rescue with all boosters
                boosters_to_apply = [
                    'premium_thumbnail', 'viral_hook', 'trending_music', 
                    'emotional_arc', 'hook_optimization', 'trend_alignment'
                ]
            elif deficit_ratio > 0.2:  # 20-50% below baseline
                # Moderate rescue
                boosters_to_apply = [
                    'thumbnail_optimization', 'hook_optimization', 
                    'trending_music', 'trend_alignment'
                ]
            else:  # Less than 20% below baseline
                # Light rescue
                boosters_to_apply = ['hook_optimization', 'thumbnail_optimization']
            
            # Apply boosters
            rescue_success = True
            for booster in boosters_to_apply:
                try:
                    await self._apply_booster(video_id, booster, video_data)
                except Exception as e:
                    self.logger.warning(f"Booster {booster} failed for {video_id}: {e}")
                    rescue_success = False
            
            # Re-project after boosters
            if rescue_success:
                updated_video = await self._load_video_data(video_id)
                new_projected_views = await self._project_final_views(updated_video)
                
                if new_projected_views >= self.baseline_views:
                    self.logger.info(
                        f"Successfully rescued video {video_id}: "
                        f"{projected_views:,} -> {new_projected_views:,} views"
                    )
                    return True
                else:
                    self.logger.warning(
                        f"Rescue insufficient for {video_id}: "
                        f"still {new_projected_views:,} < {self.baseline_views:,}"
                    )
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error in baseline rescue for {video_id}: {e}")
            return False

    async def _attempt_content_regeneration(
        self, 
        factory: FactoryInstance, 
        video_id: str, 
        original_video_data: Dict
    ) -> bool:
        """
        Attempt to regenerate content that meets baseline requirements.
        
        Returns:
            True if regeneration successful, False otherwise
        """
        try:
            max_attempts = 3
            
            for attempt in range(max_attempts):
                self.logger.info(
                    f"Regeneration attempt {attempt + 1}/{max_attempts} for video {video_id}"
                )
                
                # Generate new content parameters
                new_content_params = await self._regenerate_content_for_baseline(
                    original_video_data.get('content_params', {}),
                    factory.niche
                )
                
                # Project views for new content
                new_projected_views = await self._project_content_views(new_content_params)
                
                if new_projected_views >= self.baseline_views:
                    # Regeneration successful - replace video
                    await self._replace_video_content(video_id, new_content_params)
                    
                    self.logger.info(
                        f"Successfully regenerated video {video_id}: "
                        f"projected {new_projected_views:,} views >= baseline"
                    )
                    return True
                
                # Wait before next attempt
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
            
            self.logger.error(
                f"Failed to regenerate video {video_id} after {max_attempts} attempts"
            )
            return False
            
        except Exception as e:
            self.logger.error(f"Error in content regeneration for {video_id}: {e}")
            return False

    async def _block_video_posting(self, factory: FactoryInstance, video_id: str) -> None:
        """Block video from being posted"""
        try:
            # Add to blocked list
            if not hasattr(factory, 'blocked_videos'):
                factory.blocked_videos = set()
            
            factory.blocked_videos.add(video_id)
            
            # Remove from scheduled posts if present
            factory.scheduled_posts = [
                post for post in factory.scheduled_posts 
                if post.get('video_id') != video_id
            ]
            
            # Log blocking event
            self.logger.warning(f"Video {video_id} blocked from posting in {factory.niche}")
            
            # Update metrics
            if not hasattr(factory.metrics, 'blocked_videos'):
                factory.metrics.blocked_videos = 0
            factory.metrics.blocked_videos += 1
            
        except Exception as e:
            self.logger.error(f"Error blocking video {video_id}: {e}")

    async def _allow_video_posting(self, factory: FactoryInstance, video_id: str) -> None:
        """Allow video to be posted"""
        try:
            # Remove from blocked list if present
            if hasattr(factory, 'blocked_videos'):
                factory.blocked_videos.discard(video_id)
            
            self.logger.debug(f"Video {video_id} allowed for posting in {factory.niche}")
            
        except Exception as e:
            self.logger.error(f"Error allowing video {video_id}: {e}")

    async def _replace_video_content(self, video_id: str, new_content_params: Dict) -> None:
        """Replace video content with regenerated version"""
        try:
            # This would integrate with actual content generation system
            self.logger.info(f"Replacing content for video {video_id}")
            
            # Simulate content replacement
            await asyncio.sleep(1)  # Simulate processing time
            
            self.logger.info(f"Content replacement completed for {video_id}")
            
        except Exception as e:
            self.logger.error(f"Error replacing content for {video_id}: {e}")
            raise
    async def _allocate_resources(self, factory: FactoryInstance) -> None:
        """Allocate computational resources to factory"""
        pass

    async def _connect_content_pipeline(self, factory: FactoryInstance) -> None:
        """Connect to content generation pipeline"""
        pass

    async def _connect_trend_aggregator(self, factory: FactoryInstance) -> None:
        """Connect to trend aggregation system and start real-time updates"""
        try:
            self.logger.info(f"Connecting trend aggregator for {factory.niche}")
            
            # Initialize trend connection
            factory.trend_cache = {
                'last_update': datetime.now(),
                'trending_topics': [],
                'trending_music': [],
                'viral_patterns': [],
                'engagement_signals': {}
            }
            
            # Start trend monitoring task
            if not hasattr(self, '_trend_tasks'):
                self._trend_tasks = {}
            
            self._trend_tasks[factory.niche] = asyncio.create_task(
                self._monitor_trends(factory.niche)
            )
            
            self.logger.info(f"Trend aggregator connected for {factory.niche}")
            
        except Exception as e:
            self.logger.error(f"Failed to connect trend aggregator: {e}")
            raise

    async def _connect_feature_engine(self, factory: FactoryInstance) -> None:
        """Connect to virality feature extraction engine"""
        try:
            self.logger.info(f"Connecting feature engine for {factory.niche}")
            
            # Initialize feature cache
            factory.feature_cache = {
                'last_update': datetime.now(),
                'virality_features': {
                    'hook_strength': 0.0,
                    'emotional_arc': 0.0,
                    'visual_quality': 0.0,
                    'audio_quality': 0.0,
                    'timing_optimization': 0.0
                },
                'performance_predictions': {},
                'feature_weights': {}
            }
            
            # Start feature monitoring task
            if not hasattr(self, '_feature_tasks'):
                self._feature_tasks = {}
            
            self._feature_tasks[factory.niche] = asyncio.create_task(
                self._monitor_features(factory.niche)
            )
            
            self.logger.info(f"Feature engine connected for {factory.niche}")
            
        except Exception as e:
            self.logger.error(f"Failed to connect feature engine: {e}")
            raise

    async def _monitor_trends(self, niche: str) -> None:
        """Continuously monitor and update trend data"""
        while niche in self.factories and self.factories[niche].state == FactoryState.RUNNING:
            try:
                factory = self.factories[niche]
                
                # Fetch fresh trend data
                trend_data = await self._fetch_trend_data(niche)
                
                if trend_data:
                    # Update cache with new trends
                    factory.trend_cache.update({
                        'last_update': datetime.now(),
                        **trend_data
                    })
                    
                    # Trigger content re-evaluation if significant trends detected
                    if self._detect_significant_trends(trend_data):
                        await self._reevaluate_scheduled_content(factory)
                
                await asyncio.sleep(self.trend_update_interval)
                
            except Exception as e:
                self.logger.error(f"Error in trend monitoring for {niche}: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _monitor_features(self, niche: str) -> None:
        """Continuously monitor and update feature data"""
        while niche in self.factories and self.factories[niche].state == FactoryState.RUNNING:
            try:
                factory = self.factories[niche]
                
                # Fetch fresh feature data
                feature_data = await self._fetch_feature_data(niche)
                
                if feature_data:
                    # Update cache with new features
                    factory.feature_cache.update({
                        'last_update': datetime.now(),
                        **feature_data
                    })
                    
                    # Update projection models
                    await self._update_projection_models(factory, feature_data)
                
                await asyncio.sleep(self.feature_update_interval)
                
            except Exception as e:
                self.logger.error(f"Error in feature monitoring for {niche}: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _fetch_trend_data(self, niche: str) -> Optional[Dict[str, Any]]:
        """Fetch current trend data for niche"""
        try:
            # Simulate trend API call - replace with actual integration
            trending_topics = [
                f"trending_topic_{random.randint(1, 100)}" 
                for _ in range(random.randint(3, 8))
            ]
            
            trending_music = [
                f"trending_song_{random.randint(1, 50)}" 
                for _ in range(random.randint(2, 5))
            ]
            
            viral_patterns = [
                "hook_first_3s", "emotional_peak_mid", "call_to_action_end"
            ]
            
            engagement_signals = {
                "avg_watch_time": random.uniform(0.3, 0.8),
                "completion_rate": random.uniform(0.2, 0.7),
                "share_rate": random.uniform(0.01, 0.15),
                "comment_rate": random.uniform(0.005, 0.05)
            }
            
            return {
                'trending_topics': trending_topics,
                'trending_music': trending_music,
                'viral_patterns': viral_patterns,
                'engagement_signals': engagement_signals
            }
            
        except Exception as e:
            self.logger.error(f"Error fetching trend data: {e}")
            return None

    async def _fetch_feature_data(self, niche: str) -> Optional[Dict[str, Any]]:
        """Fetch current feature data for niche"""
        try:
            # Simulate feature extraction - replace with actual integration
            virality_features = {
                'hook_strength': random.uniform(0.4, 0.9),
                'emotional_arc': random.uniform(0.3, 0.8),
                'visual_quality': random.uniform(0.5, 0.95),
                'audio_quality': random.uniform(0.6, 0.9),
                'timing_optimization': random.uniform(0.4, 0.85)
            }
            
            performance_predictions = {
                'baseline_probability': random.uniform(0.6, 0.95),
                'viral_probability': random.uniform(0.05, 0.3),
                'expected_engagement': random.uniform(0.2, 0.8)
            }
            
            feature_weights = {
                'hook_strength': 0.25,
                'emotional_arc': 0.20,
                'visual_quality': 0.20,
                'audio_quality': 0.15,
                'timing_optimization': 0.20
            }
            
            return {
                'virality_features': virality_features,
                'performance_predictions': performance_predictions,
                'feature_weights': feature_weights
            }
            
        except Exception as e:
            self.logger.error(f"Error fetching feature data: {e}")
            return None

    def _detect_significant_trends(self, trend_data: Dict[str, Any]) -> bool:
        """Detect if trends are significant enough to trigger re-evaluation"""
        # Check for high-impact trends
        trending_topics = trend_data.get('trending_topics', [])
        trending_music = trend_data.get('trending_music', [])
        
        # Consider significant if we have new trending topics or music
        return len(trending_topics) > 5 or len(trending_music) > 3

    async def _reevaluate_scheduled_content(self, factory: FactoryInstance) -> None:
        """Reevaluate scheduled content based on new trends"""
        try:
            # Get queued content
            temp_queue = []
            
            while not factory.content_queue.empty():
                try:
                    _, item = factory.content_queue.get_nowait()
                    temp_queue.append(item)
                except queue.Empty:
                    break
            
            # Re-prioritize based on new trends
            for item in temp_queue:
                # Update content parameters with new trends
                item.content_params['trends'] = factory.trend_cache
                
                # Recalculate priority
                item.priority = self._calculate_content_priority(
                    item.content_params, 
                    item.baseline_compliant
                )
                
                # Re-queue with updated priority
                factory.content_queue.put((-item.priority, item))
            
            self.logger.info(f"Reevaluated {len(temp_queue)} content items for {factory.niche}")
            
        except Exception as e:
            self.logger.error(f"Error reevaluating content: {e}")

    async def _update_projection_models(self, factory: FactoryInstance, feature_data: Dict[str, Any]) -> None:
        """Update view projection models with new feature data"""
        try:
            # Update feature weights based on performance
            performance_preds = feature_data.get('performance_predictions', {})
            
            # Adjust weights based on prediction accuracy
            if performance_preds.get('baseline_probability', 0) > 0.8:
                # Increase weight for successful features
                factory.feature_cache['feature_weights'] = {
                    k: v * 1.1 for k, v in factory.feature_cache.get('feature_weights', {}).items()
                }
            
            self.logger.debug(f"Updated projection models for {factory.niche}")
            
        except Exception as e:
            self.logger.error(f"Error updating projection models: {e}")

    async def _initialize_rl_agent(self, factory: FactoryInstance) -> None:
        """Initialize RL agent for factory optimization"""
        try:
            self.logger.info(f"Initializing RL agent for {factory.niche}")
            
            # Initialize RL policy cache
            factory.rl_policy_cache = {
                'last_update': datetime.now(),
                'policy_version': '1.0',
                'actions': {
                    'booster_selection': {},
                    'posting_times': {},
                    'content_prioritization': {},
                    'resource_allocation': {}
                },
                'rewards': {
                    'baseline_hits': 0.0,
                    'viral_content': 0.0,
                    'engagement_rate': 0.0,
                    'resource_efficiency': 0.0
                },
                'model_metrics': {
                    'accuracy': 0.0,
                    'confidence': 0.0,
                    'prediction_count': 0
                }
            }
            
            # Start RL decision task
            if not hasattr(self, '_rl_tasks'):
                self._rl_tasks = {}
            
            self._rl_tasks[factory.niche] = asyncio.create_task(
                self._rl_decision_loop(factory.niche)
            )
            
            self.logger.info(f"RL agent initialized for {factory.niche}")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize RL agent: {e}")
            raise

    async def _rl_decision_loop(self, niche: str) -> None:
        """Continuous RL decision-making loop"""
        while niche in self.factories and self.factories[niche].state == FactoryState.RUNNING:
            try:
                factory = self.factories[niche]
                
                # Get current state for RL decision
                current_state = await self._get_rl_state(factory)
                
                # Request RL decision
                rl_decision = await self._request_rl_decision(factory, current_state)
                
                if rl_decision:
                    # Apply RL decision
                    await self._apply_rl_decision(factory, rl_decision)
                    
                    # Update policy cache
                    factory.rl_policy_cache['last_update'] = datetime.now()
                    factory.rl_policy_cache['model_metrics']['prediction_count'] += 1
                
                await asyncio.sleep(30)  # RL decisions every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Error in RL decision loop for {niche}: {e}")
                await asyncio.sleep(60)  # Wait before retry

    async def _get_rl_state(self, factory: FactoryInstance) -> Dict[str, Any]:
        """Get current state representation for RL agent"""
        try:
            # Performance metrics
            performance_state = {
                'baseline_hit_rate': factory.metrics.calculate_baseline_hit_rate(self.baseline_views),
                'avg_views_per_video': factory.metrics.avg_views_per_video,
                'viral_rate': (
                    (factory.metrics.viral_videos_30m_plus + factory.metrics.viral_videos_300m_plus) /
                    max(factory.metrics.total_videos_produced, 1)
                ),
                'queue_depth': factory.content_queue.qsize(),
                'worker_utilization': (
                    factory.worker_metrics.active_workers / 
                    max(self.max_workers_per_factory, 1)
                )
            }
            
            # Trend state
            trend_state = {
                'trending_topics_count': len(factory.trend_cache.get('trending_topics', [])),
                'trending_music_count': len(factory.trend_cache.get('trending_music', [])),
                'engagement_signals': factory.trend_cache.get('engagement_signals', {})
            }
            
            # Feature state
            feature_state = {
                'virality_features': factory.feature_cache.get('virality_features', {}),
                'performance_predictions': factory.feature_cache.get('performance_predictions', {})
            }
            
            # Resource state
            resource_state = {
                'cpu_allocated': factory.resource_allocation.get('cpu_cores', 0),
                'gpu_allocated': factory.resource_allocation.get('gpu_units', 0),
                'memory_allocated': factory.resource_allocation.get('memory_gb', 0)
            }
            
            return {
                'performance': performance_state,
                'trends': trend_state,
                'features': feature_state,
                'resources': resource_state,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error getting RL state: {e}")
            return {}

    async def _request_rl_decision(self, factory: FactoryInstance, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Request decision from RL agent"""
        try:
            # Simulate RL API call - replace with actual integration
            await asyncio.sleep(0.1)  # Simulate network latency
            
            # Mock RL decision based on heuristics
            decision = self._generate_mock_rl_decision(factory, state)
            
            # Cache decision for learning
            factory.rl_policy_cache['actions'].update(decision.get('actions', {}))
            
            return decision
            
        except Exception as e:
            self.logger.error(f"Error requesting RL decision: {e}")
            
            if self.rl_fallback_enabled:
                return self._get_fallback_decision(factory, state)
            
            return None

    def _generate_mock_rl_decision(self, factory: FactoryInstance, state: Dict[str, Any]) -> Dict[str, Any]:
        """Generate mock RL decision for testing"""
        performance = state.get('performance', {})
        
        # Decision logic based on current performance
        baseline_hit_rate = performance.get('baseline_hit_rate', 0)
        queue_depth = performance.get('queue_depth', 0)
        
        actions = {}
        
        # Booster selection decision
        if baseline_hit_rate < 80:
            actions['booster_selection'] = {
                'strategy': 'aggressive',
                'boosters': ['premium_thumbnail', 'viral_hook', 'trending_music']
            }
        elif baseline_hit_rate < 95:
            actions['booster_selection'] = {
                'strategy': 'moderate',
                'boosters': ['hook_optimization', 'thumbnail_optimization']
            }
        else:
            actions['booster_selection'] = {
                'strategy': 'minimal',
                'boosters': ['hook_optimization']
            }
        
        # Posting time optimization
        actions['posting_times'] = {
            'strategy': 'peak_hours' if queue_depth > 10 else 'optimal',
            'preferred_hours': [18, 19, 20, 21] if baseline_hit_rate < 90 else [12, 13, 18, 19]
        }
        
        # Content prioritization
        if queue_depth > 20:
            actions['content_prioritization'] = {
                'strategy': 'baseline_first',
                'priority_threshold': 80
            }
        else:
            actions['content_prioritization'] = {
                'strategy': 'balanced',
                'priority_threshold': 50
            }
        
        # Resource allocation
        if queue_depth > 30:
            actions['resource_allocation'] = {
                'action': 'scale_up',
                'additional_workers': min(5, self.max_workers_per_factory - factory.worker_metrics.active_workers)
            }
        elif queue_depth < 5 and factory.worker_metrics.active_workers > self.min_workers_per_factory:
            actions['resource_allocation'] = {
                'action': 'scale_down',
                'remove_workers': max(1, factory.worker_metrics.active_workers - self.min_workers_per_factory)
            }
        else:
            actions['resource_allocation'] = {'action': 'maintain'}
        
        return {
            'actions': actions,
            'confidence': random.uniform(0.7, 0.95),
            'reasoning': f"Decision based on baseline hit rate: {baseline_hit_rate:.1f}%, queue depth: {queue_depth}",
            'timestamp': datetime.now().isoformat()
        }

    def _get_fallback_decision(self, factory: FactoryInstance, state: Dict[str, Any]) -> Dict[str, Any]:
        """Get fallback decision when RL is unavailable"""
        return {
            'actions': {
                'booster_selection': {
                    'strategy': 'conservative',
                    'boosters': ['hook_optimization', 'thumbnail_optimization']
                },
                'posting_times': {
                    'strategy': 'standard',
                    'preferred_hours': [12, 18, 20]
                },
                'content_prioritization': {
                    'strategy': 'baseline_first',
                    'priority_threshold': 70
                },
                'resource_allocation': {'action': 'maintain'}
            },
            'confidence': 0.5,
            'reasoning': 'Fallback decision due to RL unavailability',
            'timestamp': datetime.now().isoformat()
        }

    async def _apply_rl_decision(self, factory: FactoryInstance, decision: Dict[str, Any]) -> None:
        """Apply RL decision to factory operations"""
        try:
            actions = decision.get('actions', {})
            
            # Apply booster selection strategy
            if 'booster_selection' in actions:
                booster_action = actions['booster_selection']
                self.logger.info(
                    f"Applying RL booster strategy for {factory.niche}: {booster_action['strategy']}"
                )
                # Update factory config with new booster strategy
                factory.config['rl_booster_strategy'] = booster_action
            
            # Apply posting time optimization
            if 'posting_times' in actions:
                posting_action = actions['posting_times']
                self.logger.info(
                    f"Applying RL posting strategy for {factory.niche}: {posting_action['strategy']}"
                )
                factory.config['rl_posting_strategy'] = posting_action
            
            # Apply content prioritization
            if 'content_prioritization' in actions:
                priority_action = actions['content_prioritization']
                self.logger.info(
                    f"Applying RL priority strategy for {factory.niche}: {priority_action['strategy']}"
                )
                factory.config['rl_priority_strategy'] = priority_action
            
            # Apply resource allocation decisions
            if 'resource_allocation' in actions:
                resource_action = actions['resource_allocation']
                if resource_action['action'] == 'scale_up':
                    await self._scale_workers_up(factory, resource_action.get('additional_workers', 2))
                elif resource_action['action'] == 'scale_down':
                    await self._scale_workers_down(factory, resource_action.get('remove_workers', 1))
            
            self.logger.info(f"RL decision applied for {factory.niche} with confidence: {decision.get('confidence', 0):.2f}")
            
        except Exception as e:
            self.logger.error(f"Error applying RL decision: {e}")

    async def _scale_workers_up(self, factory: FactoryInstance, additional_workers: int) -> None:
        """Scale up workers for factory"""
        try:
            if not self.scaling_enabled:
                return
            
            current_workers = factory.worker_metrics.active_workers
            max_allowed = self.max_workers_per_factory
            
            workers_to_add = min(additional_workers, max_allowed - current_workers)
            
            if workers_to_add <= 0:
                self.logger.warning(f"Cannot scale up {factory.niche}: already at max workers")
                return
            
            self.logger.info(
                f"Scaling up {factory.niche}: {current_workers} -> {current_workers + workers_to_add} workers"
            )
            
            # Create additional workers
            if not factory.worker_pool:
                factory.worker_pool = ThreadPoolExecutor(
                    max_workers=self.max_workers_per_factory,
                    thread_name_prefix=f"{factory.niche}_Worker"
                )
            
            # Update metrics
            factory.worker_metrics.active_workers += workers_to_add
            factory.scaling_metrics.current_capacity = factory.worker_metrics.active_workers
            factory.scaling_metrics.scale_events_24h += 1
            
            # Update state temporarily
            factory.state = FactoryState.SCALING
            
            await asyncio.sleep(1)  # Simulate scaling time
            
            factory.state = FactoryState.RUNNING
            
            self.logger.info(f"Successfully scaled up {factory.niche} by {workers_to_add} workers")
            
        except Exception as e:
            self.logger.error(f"Error scaling up workers for {factory.niche}: {e}")
            factory.state = FactoryState.ERROR

    async def _scale_workers_down(self, factory: FactoryInstance, remove_workers: int) -> None:
        """Scale down workers for factory"""
        try:
            if not self.scaling_enabled:
                return
            
            current_workers = factory.worker_metrics.active_workers
            min_allowed = self.min_workers_per_factory
            
            workers_to_remove = min(remove_workers, current_workers - min_allowed)
            
            if workers_to_remove <= 0:
                self.logger.warning(f"Cannot scale down {factory.niche}: already at min workers")
                return
            
            self.logger.info(
                f"Scaling down {factory.niche}: {current_workers} -> {current_workers - workers_to_remove} workers"
            )
            
            # Update metrics
            factory.worker_metrics.active_workers -= workers_to_remove
            factory.scaling_metrics.current_capacity = factory.worker_metrics.active_workers
            factory.scaling_metrics.scale_events_24h += 1
            
            # Update state temporarily
            factory.state = FactoryState.SCALING
            
            await asyncio.sleep(1)  # Simulate scaling time
            
            factory.state = FactoryState.RUNNING
            
            self.logger.info(f"Successfully scaled down {factory.niche} by {workers_to_remove} workers")
            
        except Exception as e:
            self.logger.error(f"Error scaling down workers for {factory.niche}: {e}")
            factory.state = FactoryState.ERROR

    async def _auto_scale_workers(self, factory: FactoryInstance) -> None:
        """Automatically scale workers based on load"""
        try:
            queue_depth = factory.content_queue.qsize()
            current_workers = factory.worker_metrics.active_workers
            processing_rate = factory.worker_metrics.processing_rate
            
            # Calculate load metrics
            items_per_worker = queue_depth / max(current_workers, 1)
            utilization = processing_rate / (current_workers * 10)  # Assume 10 items/worker capacity
            
            # Scale up conditions
            if (queue_depth > 20 and items_per_worker > 5 and 
                current_workers < self.max_workers_per_factory and 
                utilization > self.scale_up_threshold):
                
                scale_amount = min(3, self.max_workers_per_factory - current_workers)
                await self._scale_workers_up(factory, scale_amount)
                
            # Scale down conditions
            elif (queue_depth < 5 and items_per_worker < 1 and 
                  current_workers > self.min_workers_per_factory and 
                  utilization < self.scale_down_threshold):
                
                scale_amount = min(2, current_workers - self.min_workers_per_factory)
                await self._scale_workers_down(factory, scale_amount)
            
        except Exception as e:
            self.logger.error(f"Error in auto-scaling for {factory.niche}: {e}")

    async def _process_content_queue(self, factory: FactoryInstance) -> None:
        """Process items from content queue with workers"""
        try:
            while not factory.content_queue.empty() and factory.worker_metrics.active_workers > 0:
                try:
                    # Get next item from queue
                    priority, item = factory.content_queue.get_nowait()
                    
                    # Submit to worker pool
                    if factory.worker_pool:
                        future = factory.worker_pool.submit(
                            self._process_content_item, factory, item
                        )
                        
                        # Add callback for completion
                        future.add_done_callback(
                            lambda f: self._handle_content_completion(factory, item, f)
                        )
                    
                except queue.Empty:
                    break
                except Exception as e:
                    self.logger.error(f"Error processing queue item: {e}")
                    
        except Exception as e:
            self.logger.error(f"Error in content queue processing: {e}")

    def _process_content_item(self, factory: FactoryInstance, item: ContentQueueItem) -> Dict[str, Any]:
        """Process individual content item"""
        try:
            start_time = time.time()
            
            # Simulate content processing
            processing_time = random.uniform(0.5, 3.0)  # 0.5-3 seconds per item
            time.sleep(processing_time)
            
            # Generate content
            content_result = {
                'item_id': item.id,
                'niche': factory.niche,
                'success': True,
                'processing_time': processing_time,
                'projected_views': item.projected_views,
                'baseline_compliant': item.baseline_compliant,
                'boosters_applied': item.content_params.get('boosters', []),
                'timestamp': datetime.now().isoformat()
            }
            
            # Update metrics
            factory.worker_metrics.processing_rate = (
                factory.worker_metrics.processing_rate * 0.9 + (1.0 / processing_time) * 0.1
            )
            factory.worker_metrics.avg_processing_time = (
                factory.worker_metrics.avg_processing_time * 0.9 + processing_time * 0.1
            )
            
            return content_result
            
        except Exception as e:
            self.logger.error(f"Error processing content item {item.id}: {e}")
            return {
                'item_id': item.id,
                'niche': factory.niche,
                'success': False,
                'error': str(e),
                'timestamp': datetime.now().isoformat()
            }

    def _handle_content_completion(self, factory: FactoryInstance, item: ContentQueueItem, future) -> None:
        """Handle completion of content processing"""
        try:
            result = future.result()
            
            if result.get('success', False):
                # Update success metrics
                factory.worker_metrics.success_rate = (
                    factory.worker_metrics.success_rate * 0.95 + 0.05
                )
                
                # Update factory metrics
                if result.get('baseline_compliant', False):
                    factory.metrics.videos_above_baseline += 1
                else:
                    factory.metrics.videos_below_baseline += 1
                
                factory.metrics.total_videos_produced += 1
                
                self.logger.debug(
                    f"Successfully processed content item {item.id} for {factory.niche}"
                )
            else:
                # Handle failure
                self.logger.error(
                    f"Failed to process content item {item.id}: {result.get('error', 'Unknown error')}"
                )
                
                # Retry logic
                if item.retry_count < item.max_retries:
                    item.retry_count += 1
                    factory.content_queue.put((-item.priority, item))
                    self.logger.info(f"Retrying content item {item.id} (attempt {item.retry_count})")
                
        except Exception as e:
            self.logger.error(f"Error handling content completion: {e}")

    async def _monitor_system_scaling(self) -> None:
        """Monitor and manage system-wide scaling"""
        while True:
            try:
                total_queue_depth = sum(
                    factory.content_queue.qsize() for factory in self.factories.values()
                )
                
                total_workers = sum(
                    factory.worker_metrics.active_workers for factory in self.factories.values()
                )
                
                # Calculate system-wide metrics
                system_throughput = sum(
                    factory.worker_metrics.processing_rate for factory in self.factories.values()
                )
                
                # Update global scaling metrics
                for factory in self.factories.values():
                    factory.scaling_metrics.queue_depth = factory.content_queue.qsize()
                    factory.scaling_metrics.throughput_per_minute = factory.worker_metrics.processing_rate
                    factory.scaling_metrics.resource_utilization = (
                        factory.worker_metrics.active_workers / self.max_workers_per_factory
                    )
                
                # Check if we need system-wide scaling
                avg_queue_per_factory = total_queue_depth / max(len(self.factories), 1)
                
                if avg_queue_per_factory > 50:  # High load across all factories
                    self.logger.warning(
                        f"High system load detected: {total_queue_depth} items queued, "
                        f"{total_workers} workers active"
                    )
                    
                    # Trigger auto-scaling for each factory
                    for factory in self.factories.values():
                        if factory.state == FactoryState.RUNNING:
                            await self._auto_scale_workers(factory)
                
                # Update global metrics
                self.global_metrics['system_throughput'] = system_throughput
                self.global_metrics['avg_processing_time'] = (
                    sum(f.worker_metrics.avg_processing_time for f in self.factories.values()) /
                    max(len(self.factories), 1)
                )
                
                await asyncio.sleep(60)  # Check every minute
                
            except Exception as e:
                self.logger.error(f"Error in system scaling monitor: {e}")
                await asyncio.sleep(60)

    async def _disconnect_pipelines(self, factory: FactoryInstance) -> None:
        """Disconnect all pipeline connections"""
        pass

    async def _release_resources(self, factory: FactoryInstance) -> None:
        """Release allocated resources"""
        pass

    async def _reschedule_missed_content(self, factory: FactoryInstance) -> None:
        """Reschedule content missed during pause"""
        pass

    async def _load_recent_videos(self, niche: str) -> List[Dict]:
        """Load recent video performance data"""
        return []

    async def _project_final_views(self, video: Dict) -> int:
        """Project final view count based on early metrics"""
        return 0

    async def _send_alert(self, niche: str, message: str, severity: str) -> None:
        """Send alert via alerting system"""
        pass

    def _calculate_optimal_post_time(
        self,
        factory: FactoryInstance,
        schedule: List,
        index: int
    ) -> datetime:
        """Calculate optimal posting time"""
        return datetime.now() + timedelta(hours=index)

    async def _get_current_trends(self, niche: str) -> Dict[str, Any]:
        """Get current trends for niche"""
        return {}

    def _determine_boosters(
        self,
        factory: FactoryInstance,
        video_data: Dict,
        booster_type: str
    ) -> List[str]:
        """Determine which boosters to apply"""
        return []

    async def _apply_booster(
        self,
        video_id: str,
        booster_name: str,
        video_data: Dict
    ) -> None:
        """Apply specific booster"""
        pass

    async def _notify_rl_agent(
        self,
        factory: FactoryInstance,
        video_id: str,
        boosters: List[str]
    ) -> None:
        """Notify RL agent of booster application"""
        pass

    def get_factory_status(self, niche: str) -> Optional[Dict[str, Any]]:
        """Get current status of a factory"""
        if niche not in self.factories:
            return None
        
        factory = self.factories[niche]
        return {
            'niche': factory.niche,
            'state': factory.state.value,
            'metrics': {
                'total_videos': factory.metrics.total_videos_produced,
                'total_views': factory.metrics.total_views,
                'avg_views': factory.metrics.avg_views_per_video,
                'baseline_hit_rate': factory.metrics.calculate_baseline_hit_rate(
                    self.baseline_views
                ),
                'viral_30m_plus': factory.metrics.viral_videos_30m_plus,
                'viral_300m_plus': factory.metrics.viral_videos_300m_plus
            },
            'resource_allocation': factory.resource_allocation,
            'active_boosters': factory.active_boosters,
            'last_activity': factory.last_activity.isoformat()
        }

    def get_global_status(self) -> Dict[str, Any]:
        """Get global system status"""
        return {
            'global_metrics': self.global_metrics,
            'factories': {
                niche: self.get_factory_status(niche)
                for niche in self.factories.keys()
            },
            'baseline_target': self.baseline_views,
            'viral_targets': {
                'tier_1': self.viral_tier_1,
                'tier_2': self.viral_tier_2
            }
        }