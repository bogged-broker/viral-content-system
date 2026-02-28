"""
factory_registry.py

Central registry for all niche factories. Tracks state, metadata, and performance
to ensure 5M+ baseline views and maximize 30M-300M+ viral potential.

Location: /factories/factory_registry.py
"""

import json
import threading
import datetime
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, asdict
from enum import Enum
import shutil
import os


class FactoryStatus(Enum):
    """Valid factory states"""
    INITIALIZED = "initialized"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class FactoryState:
    """Immutable state snapshot for a factory"""
    niche: str
    status: str
    config_path: str
    ml_agent: str
    rl_agent: str
    last_run: Optional[str]
    active_videos: int
    scheduled_videos: int
    boosters_active: List[str]
    baseline_views_target: int
    created_at: str
    updated_at: str
    total_videos_produced: int = 0
    avg_engagement_score: float = 0.0
    virality_score: float = 0.0
    error_count: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dict for serialization"""
        return asdict(self)


class FactoryRegistry:
    """
    Central registry tracking all factories, their state, metadata, and
    performance targets. Provides thread-safe APIs for orchestration and scaling.
    
    Designed for:
    - O(1) lookups across thousands of factories
    - Atomic updates with multiprocessing support
    - Persistence for crash recovery
    - Real-time state tracking for 5M+ baseline guarantee
    - Historical snapshots for auditability
    """

    def __init__(self, 
                 persistence_path: str = "data/factory_registry.json",
                 snapshots_dir: str = "data/factory_snapshots",
                 logger: Optional[logging.Logger] = None,
                 enable_snapshots: bool = True,
                 snapshot_interval_hours: int = 24):
        """
        Initialize the factory registry with persistence support.

        Args:
            persistence_path: Path to store registry JSON file
            snapshots_dir: Directory for historical snapshots
            logger: Logger instance (creates default if None)
            enable_snapshots: Enable historical snapshotting
            snapshot_interval_hours: Hours between automatic snapshots
        """
        self.persistence_path = Path(persistence_path)
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Snapshot configuration
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.enable_snapshots = enable_snapshots
        self.snapshot_interval_hours = snapshot_interval_hours
        self.last_snapshot_time = None
        
        # Logger setup
        self.logger = logger or self._setup_logger()
        
        self.factories: Dict[str, FactoryState] = {}
        self._lock = threading.RLock()  # Reentrant lock for nested operations
        self._load_registry()
        
        # Performance metrics cache
        self._metrics_cache = {}
        self._cache_lock = threading.Lock()

    def _setup_logger(self) -> logging.Logger:
        """Setup logger for the registry"""
        logger = logging.getLogger("FactoryRegistry")
        logger.setLevel(logging.INFO)
        
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        
        return logger
    
    def _validate_config_path(self, config_path: str) -> bool:
        """
        Validate that config file exists and is readable.
        
        Args:
            config_path: Path to config file
            
        Returns:
            True if valid, False otherwise
        """
        try:
            config_file = Path(config_path)
            if not config_file.exists():
                self.logger.error(f"Config file not found: {config_path}")
                return False
            
            if not config_file.is_file():
                self.logger.error(f"Config path is not a file: {config_path}")
                return False
            
            # Try to read a small portion to verify it's readable
            with open(config_file, 'r') as f:
                f.read(100)  # Read first 100 chars
            
            return True
            
        except Exception as e:
            self.logger.error(f"Config validation failed for {config_path}: {e}")
            return False
    
    def _create_snapshot(self, force: bool = False) -> bool:
        """
        Create a historical snapshot of the current registry state.
        
        Args:
            force: Force snapshot creation regardless of interval
            
        Returns:
            True if snapshot created, False otherwise
        """
        if not self.enable_snapshots:
            return False
        
        current_time = datetime.datetime.utcnow()
        
        # Check if enough time has passed since last snapshot
        if not force and self.last_snapshot_time:
            hours_since_last = (current_time - self.last_snapshot_time).total_seconds() / 3600
            if hours_since_last < self.snapshot_interval_hours:
                return False
        
        try:
            timestamp = current_time.strftime("%Y%m%d_%H%M%S")
            snapshot_filename = f"registry_snapshot_{timestamp}.json"
            snapshot_path = self.snapshots_dir / snapshot_filename
            
            # Create snapshot data with metadata
            snapshot_data = {
                "snapshot_metadata": {
                    "timestamp": current_time.isoformat() + "Z",
                    "total_factories": len(self.factories),
                    "snapshot_type": "periodic" if not force else "manual",
                    "registry_version": "1.0"
                },
                "factories": {niche: state.to_dict() for niche, state in self.factories.items()}
            }
            
            # Atomic write for snapshot
            temp_path = snapshot_path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                json.dump(snapshot_data, f, indent=2)
            
            temp_path.replace(snapshot_path)
            
            self.last_snapshot_time = current_time
            
            # Cleanup old snapshots (keep last 30)
            self._cleanup_old_snapshots()
            
            self.logger.info(f"Created registry snapshot: {snapshot_filename}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to create snapshot: {e}")
            return False
    
    def _cleanup_old_snapshots(self, keep_count: int = 30) -> None:
        """
        Clean up old snapshots, keeping only the most recent ones.
        
        Args:
            keep_count: Number of recent snapshots to keep
        """
        try:
            snapshots = list(self.snapshots_dir.glob("registry_snapshot_*.json"))
            snapshots.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            for old_snapshot in snapshots[keep_count:]:
                old_snapshot.unlink()
                
        except Exception as e:
            self.logger.warning(f"Failed to cleanup old snapshots: {e}")
    
    def _load_registry(self) -> None:
        """
        Load registry from disk or initialize empty registry.
        Thread-safe operation with error handling.
        """
        with self._lock:
            try:
                if self.persistence_path.exists():
                    with open(self.persistence_path, 'r') as f:
                        data = json.load(f)
                        
                    # Reconstruct FactoryState objects from JSON
                    for niche, state_dict in data.items():
                        self.factories[niche] = FactoryState(**state_dict)
                    
                    self.logger.info(f"Loaded {len(self.factories)} factories from disk")
                else:
                    self.logger.info("No existing registry found, starting fresh")
                    
            except Exception as e:
                self.logger.error(f"ERROR loading registry: {e}")
                self.logger.info("Starting with empty registry")
                self.factories = {}

    def _persist_registry(self) -> None:
        """
        Save the registry state to disk atomically.
        Uses atomic write (write to temp, then rename) to prevent corruption.
        """
        try:
            # Convert all FactoryState objects to dicts
            data = {niche: state.to_dict() for niche, state in self.factories.items()}
            
            # Atomic write: write to temp file, then rename
            temp_path = self.persistence_path.with_suffix('.tmp')
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            temp_path.replace(self.persistence_path)
            
        except Exception as e:
            self.logger.error(f"ERROR persisting registry: {e}")

    def _current_timestamp(self) -> str:
        """Returns current UTC timestamp in ISO format"""
        return datetime.datetime.utcnow().isoformat() + "Z"

    def register_factory(
        self,
        niche: str,
        config_path: str,
        ml_agent: str,
        rl_agent: str,
        baseline_views_target: int = 5_000_000
    ) -> bool:
        """
        Add a new factory to the registry.

        Args:
            niche: Unique niche identifier
            config_path: Path to niche YAML config
            ml_agent: Assigned ML model for engagement prediction
            rl_agent: Assigned RL agent for automated boosting
            baseline_views_target: Minimum views target (default 5M)

        Returns:
            True if registered successfully, False if already exists or invalid config
        """
        with self._lock:
            if niche in self.factories:
                self.logger.warning(f"Factory '{niche}' already registered")
                return False
            
            # Validate config path
            if not self._validate_config_path(config_path):
                self.logger.error(f"Failed to register factory '{niche}': invalid config path")
                return False
            
            timestamp = self._current_timestamp()
            self.factories[niche] = FactoryState(
                niche=niche,
                status=FactoryStatus.INITIALIZED.value,
                config_path=config_path,
                ml_agent=ml_agent,
                rl_agent=rl_agent,
                last_run=None,
                active_videos=0,
                scheduled_videos=0,
                boosters_active=[],
                baseline_views_target=baseline_views_target,
                created_at=timestamp,
                updated_at=timestamp
            )
            
            self._persist_registry()
            self.logger.info(f"Registered factory: {niche} (config: {config_path}, ml_agent: {ml_agent}, rl_agent: {rl_agent})")
            return True

    def update_factory_status(self, niche: str, status: str) -> bool:
        """
        Update the status of a factory.

        Args:
            niche: Niche identifier
            status: One of FactoryStatus enum values

        Returns:
            True if updated, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                self.logger.warning(f"Factory '{niche}' not found")
                return False
            
            factory = self.factories[niche]
            # Create new state (immutable pattern)
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(), 
                   "status": status,
                   "last_run": self._current_timestamp(),
                   "updated_at": self._current_timestamp()}
            )
            
            self._persist_registry()
            return True

    def increment_active_videos(self, niche: str, count: int = 1) -> bool:
        """
        Increment active video counter for the niche.

        Args:
            niche: Niche identifier
            count: Number of videos to add

        Returns:
            True if updated, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            factory = self.factories[niche]
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(),
                   "active_videos": factory.active_videos + count,
                   "total_videos_produced": factory.total_videos_produced + count,
                   "updated_at": self._current_timestamp()}
            )
            
            self._persist_registry()
            return True

    def decrement_active_videos(self, niche: str, count: int = 1) -> bool:
        """
        Decrement active video counter (for completed/deleted videos).

        Args:
            niche: Niche identifier
            count: Number of videos to remove

        Returns:
            True if updated, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            factory = self.factories[niche]
            new_count = max(0, factory.active_videos - count)
            
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(),
                   "active_videos": new_count,
                   "updated_at": self._current_timestamp()}
            )
            
            self._persist_registry()
            return True

    def set_scheduled_videos(self, niche: str, count: int) -> bool:
        """
        Set the scheduled video count for a factory.

        Args:
            niche: Niche identifier
            count: Number of scheduled videos

        Returns:
            True if updated, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            factory = self.factories[niche]
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(),
                   "scheduled_videos": count,
                   "updated_at": self._current_timestamp()}
            )
            
            self._persist_registry()
            return True

    def set_boosters(self, niche: str, boosters: List[str]) -> bool:
        """
        Assign active boosters to a factory for high-potential virality.

        Args:
            niche: Niche identifier
            boosters: List of booster names ['thumbnail_opt', 'trend_alignment', etc.]

        Returns:
            True if updated, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            factory = self.factories[niche]
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(),
                   "boosters_active": boosters,
                   "updated_at": self._current_timestamp()}
            )
            
            self._persist_registry()
            self.logger.info(f"Boosters activated for {niche}: {boosters}")
            return True

    def update_performance_metrics(
        self,
        niche: str,
        avg_engagement: Optional[float] = None,
        virality_score: Optional[float] = None
    ) -> bool:
        """
        Update performance metrics for a factory.

        Args:
            niche: Niche identifier
            avg_engagement: Average engagement score
            virality_score: Virality score (0-100)

        Returns:
            True if updated, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            factory = self.factories[niche]
            updates = {"updated_at": self._current_timestamp()}
            
            if avg_engagement is not None:
                updates["avg_engagement_score"] = avg_engagement
            if virality_score is not None:
                updates["virality_score"] = virality_score
            
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(), **updates}
            )
            
            self._persist_registry()
            return True

    def record_error(self, niche: str, error_msg: str) -> bool:
        """
        Record an error for a factory.

        Args:
            niche: Niche identifier
            error_msg: Error message

        Returns:
            True if recorded, False if factory not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            factory = self.factories[niche]
            self.factories[niche] = FactoryState(
                **{**factory.to_dict(),
                   "status": FactoryStatus.ERROR.value,
                   "error_count": factory.error_count + 1,
                   "last_error": error_msg,
                   "updated_at": self._current_timestamp()}
            )
            
            self._persist_registry()
            self.logger.error(f"ERROR recorded for {niche}: {error_msg}")
            return True

    def get_factory_info(self, niche: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve the full factory metadata and state.

        Args:
            niche: Niche identifier

        Returns:
            Factory state dict or None if not found
        """
        with self._lock:
            factory = self.factories.get(niche)
            return factory.to_dict() if factory else None

    def get_all_factories(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all factory states as a dictionary.

        Returns:
            Dict mapping niche names to factory state dicts
        """
        with self._lock:
            return {niche: state.to_dict() for niche, state in self.factories.items()}

    def get_factories_by_status(self, status: str) -> List[str]:
        """
        Get list of factory niches by status.

        Args:
            status: Factory status to filter by

        Returns:
            List of niche identifiers
        """
        with self._lock:
            return [
                niche for niche, state in self.factories.items()
                if state.status == status
            ]

    def get_underperforming_factories(self, threshold: float = 0.5) -> List[str]:
        """
        Identify factories below baseline performance.

        Args:
            threshold: Performance threshold (0.0-1.0)

        Returns:
            List of niche identifiers for underperforming factories
        """
        with self._lock:
            underperforming = []
            for niche, state in self.factories.items():
                # Factory is underperforming if engagement/virality < threshold
                if (state.avg_engagement_score < threshold or 
                    state.virality_score < threshold * 100):
                    underperforming.append(niche)
            
            return underperforming

    def get_high_potential_factories(self, min_virality: float = 75.0) -> List[str]:
        """
        Identify factories with high viral potential.

        Args:
            min_virality: Minimum virality score

        Returns:
            List of niche identifiers for high-potential factories
        """
        with self._lock:
            return [
                niche for niche, state in self.factories.items()
                if state.virality_score >= min_virality
            ]

    def get_registry_stats(self) -> Dict[str, Any]:
        """
        Get aggregate statistics across all factories.

        Returns:
            Dict with registry-wide statistics
        """
        with self._lock:
            if not self.factories:
                return {
                    "total_factories": 0,
                    "status_breakdown": {},
                    "total_active_videos": 0,
                    "avg_virality_score": 0.0
                }
            
            status_counts = {}
            total_videos = 0
            total_virality = 0.0
            
            for state in self.factories.values():
                status_counts[state.status] = status_counts.get(state.status, 0) + 1
                total_videos += state.active_videos
                total_virality += state.virality_score
            
            return {
                "total_factories": len(self.factories),
                "status_breakdown": status_counts,
                "total_active_videos": total_videos,
                "avg_virality_score": total_virality / len(self.factories)
            }

    def remove_factory(self, niche: str) -> bool:
        """
        Remove a factory from the registry.

        Args:
            niche: Niche identifier

        Returns:
            True if removed, False if not found
        """
        with self._lock:
            if niche not in self.factories:
                return False
            
            del self.factories[niche]
            self._persist_registry()
            self.logger.info(f"Removed factory: {niche}")
            return True

    def bulk_update_status(self, niches: List[str], status: str) -> int:
        """
        Update status for multiple factories atomically.

        Args:
            niches: List of niche identifiers
            status: New status for all factories

        Returns:
            Number of factories updated
        """
        with self._lock:
            updated_count = 0
            timestamp = self._current_timestamp()
            
            for niche in niches:
                if niche in self.factories:
                    factory = self.factories[niche]
                    self.factories[niche] = FactoryState(
                        **{**factory.to_dict(),
                           "status": status,
                           "last_run": timestamp,
                           "updated_at": timestamp}
                    )
                    updated_count += 1
            
            if updated_count > 0:
                self._persist_registry()
                self.logger.info(f"Bulk updated {updated_count} factories to status: {status}")
            
            return updated_count
    
    def create_manual_snapshot(self) -> bool:
        """
        Manually trigger a snapshot creation.
        
        Returns:
            True if snapshot created successfully
        """
        return self._create_snapshot(force=True)
    
    def list_snapshots(self) -> List[Dict[str, Any]]:
        """
        List all available snapshots with metadata.
        
        Returns:
            List of snapshot information dictionaries
        """
        snapshots = []
        try:
            for snapshot_file in self.snapshots_dir.glob("registry_snapshot_*.json"):
                try:
                    with open(snapshot_file, 'r') as f:
                        data = json.load(f)
                    
                    metadata = data.get("snapshot_metadata", {})
                    snapshots.append({
                        "filename": snapshot_file.name,
                        "timestamp": metadata.get("timestamp"),
                        "total_factories": metadata.get("total_factories", 0),
                        "snapshot_type": metadata.get("snapshot_type", "unknown"),
                        "file_size": snapshot_file.stat().st_size
                    })
                except Exception as e:
                    self.logger.warning(f"Failed to read snapshot {snapshot_file}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Failed to list snapshots: {e}")
        
        # Sort by timestamp (newest first)
        snapshots.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return snapshots
    
    def restore_from_snapshot(self, snapshot_filename: str) -> bool:
        """
        Restore registry state from a snapshot.
        
        Args:
            snapshot_filename: Name of snapshot file
            
        Returns:
            True if restored successfully
        """
        snapshot_path = self.snapshots_dir / snapshot_filename
        
        if not snapshot_path.exists():
            self.logger.error(f"Snapshot not found: {snapshot_filename}")
            return False
        
        try:
            with open(snapshot_path, 'r') as f:
                snapshot_data = json.load(f)
            
            # Validate snapshot format
            if "factories" not in snapshot_data:
                self.logger.error(f"Invalid snapshot format: {snapshot_filename}")
                return False
            
            with self._lock:
                # Create backup of current state
                backup_timestamp = self._current_timestamp().replace(":", "-")
                backup_path = self.persistence_path.with_suffix(f".backup_{backup_timestamp}")
                if self.persistence_path.exists():
                    shutil.copy2(self.persistence_path, backup_path)
                    self.logger.info(f"Created backup: {backup_path}")
                
                # Restore factories from snapshot
                self.factories.clear()
                for niche, state_dict in snapshot_data["factories"].items():
                    self.factories[niche] = FactoryState(**state_dict)
                
                # Persist restored state
                self._persist_registry()
                
                metadata = snapshot_data.get("snapshot_metadata", {})
                self.logger.info(
                    f"Restored registry from snapshot {snapshot_filename} "
                    f"({metadata.get('total_factories', 0)} factories, "
                    f"timestamp: {metadata.get('timestamp', 'unknown')})"
                )
                
                return True
                
        except Exception as e:
            self.logger.error(f"Failed to restore from snapshot {snapshot_filename}: {e}")
            return False
    
    def get_registry_health(self) -> Dict[str, Any]:
        """
        Get comprehensive health and status information about the registry.
        
        Returns:
            Dictionary with health metrics and status
        """
        with self._lock:
            stats = self.get_registry_stats()
            
            # Error factories
            error_factories = self.get_factories_by_status(FactoryStatus.ERROR.value)
            
            # Config validation status
            invalid_configs = []
            for niche, factory in self.factories.items():
                if not self._validate_config_path(factory.config_path):
                    invalid_configs.append(niche)
            
            # Snapshot status
            snapshots = list(self.snapshots_dir.glob("registry_snapshot_*.json"))
            last_snapshot_age = None
            if self.last_snapshot_time:
                last_snapshot_age = (datetime.datetime.utcnow() - self.last_snapshot_time).total_seconds() / 3600
            
            return {
                "registry_stats": stats,
                "health_status": {
                    "error_factories": {
                        "count": len(error_factories),
                        "niches": error_factories
                    },
                    "invalid_configs": {
                        "count": len(invalid_configs),
                        "niches": invalid_configs
                    },
                    "snapshot_status": {
                        "enabled": self.enable_snapshots,
                        "total_snapshots": len(snapshots),
                        "last_snapshot_hours_ago": last_snapshot_age,
                        "next_snapshot_hours": max(0, self.snapshot_interval_hours - (last_snapshot_age or 0))
                    }
                },
                "system_info": {
                    "persistence_path": str(self.persistence_path),
                    "snapshots_dir": str(self.snapshots_dir),
                    "snapshot_interval_hours": self.snapshot_interval_hours
                }
            }


# Example usage
if __name__ == "__main__":
    # Initialize registry with custom logger and snapshot settings
    import logging
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("MainApp")
    
    registry = FactoryRegistry(
        persistence_path="data/test_factory_registry.json",
        snapshots_dir="data/test_snapshots",
        logger=logger,
        enable_snapshots=True,
        snapshot_interval_hours=1  # Short interval for testing
    )
    
    # Test config validation (will fail if config doesn't exist)
    test_result = registry.register_factory(
        niche="luxury_lifestyle",
        config_path="config/factories/luxury_lifestyle.yaml",
        ml_agent="engagement_predictor_v2",
        rl_agent="factory_agent_01",
        baseline_views_target=5_000_000
    )
    
    if not test_result:
        # Register with a mock config for testing
        registry.register_factory(
            niche="test_factory",
            config_path="test_config.yaml",  # This will fail validation
            ml_agent="test_agent",
            rl_agent="test_rl_agent"
        )
    
    # Create manual snapshot
    snapshot_result = registry.create_manual_snapshot()
    print(f"Manual snapshot created: {snapshot_result}")
    
    # List snapshots
    snapshots = registry.list_snapshots()
    print(f"Available snapshots: {len(snapshots)}")
    for snap in snapshots[:3]:  # Show first 3
        print(f"  - {snap['filename']} ({snap['total_factories']} factories)")
    
    # Get registry health
    health = registry.get_registry_health()
    print(f"\nRegistry Health: {json.dumps(health, indent=2)}")
    
    print("\n✅ Enhanced Factory Registry operational - ready for 5M+ baseline tracking!")
    print("📊 Features: Centralized logging, historical snapshots, config validation")