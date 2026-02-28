"""
/orchestration/lifecycle_manager.py

Component Lifecycle Management

Manages the lifecycle of system components:
- Initialization
- Startup
- Health checks
- Graceful shutdown
- State tracking
"""

import asyncio
import logging
from enum import Enum
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from datetime import datetime


class ComponentState(Enum):
    """Component lifecycle states."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class ComponentInfo:
    """Information about a component."""
    name: str
    state: ComponentState = ComponentState.UNINITIALIZED
    initialized_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    error: Optional[str] = None
    health_check: Optional[Callable] = None
    shutdown_handler: Optional[Callable] = None


class LifecycleManager:
    """
    Manages component lifecycle across the system.
    
    Usage:
        manager = LifecycleManager()
        await manager.register_component("ingestion", init_func, start_func, stop_func)
        await manager.start_component("ingestion")
        await manager.shutdown_all()
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.components: Dict[str, ComponentInfo] = {}
        self._lock = asyncio.Lock()
    
    async def register_component(
        self,
        name: str,
        init_func: Optional[Callable] = None,
        start_func: Optional[Callable] = None,
        stop_func: Optional[Callable] = None,
        health_check: Optional[Callable] = None
    ):
        """
        Register a component with lifecycle handlers.
        
        Args:
            name: Component name
            init_func: Async function to initialize component
            start_func: Async function to start component
            stop_func: Async function to stop component
            health_check: Async function to check component health
        """
        async with self._lock:
            if name in self.components:
                self.logger.warning(f"Component {name} already registered, updating...")
            
            component = ComponentInfo(
                name=name,
                health_check=health_check,
                shutdown_handler=stop_func
            )
            
            self.components[name] = component
            
            # Initialize if init_func provided
            if init_func:
                await self._initialize_component(name, init_func)
    
    async def _initialize_component(self, name: str, init_func: Callable):
        """Initialize a component."""
        component = self.components[name]
        component.state = ComponentState.INITIALIZING
        
        try:
            await init_func()
            component.state = ComponentState.INITIALIZED
            component.initialized_at = datetime.now()
            self.logger.info(f"Component {name} initialized")
        except Exception as e:
            component.state = ComponentState.FAILED
            component.error = str(e)
            self.logger.error(f"Failed to initialize {name}: {e}", exc_info=True)
            raise
    
    async def start_component(self, name: str, start_func: Optional[Callable] = None):
        """Start a component."""
        async with self._lock:
            if name not in self.components:
                raise ValueError(f"Component {name} not registered")
            
            component = self.components[name]
            
            if component.state == ComponentState.RUNNING:
                self.logger.warning(f"Component {name} already running")
                return
            
            if component.state == ComponentState.FAILED:
                raise RuntimeError(f"Component {name} is in failed state: {component.error}")
            
            component.state = ComponentState.STARTING
            
            try:
                if start_func:
                    await start_func()
                component.state = ComponentState.RUNNING
                component.started_at = datetime.now()
                self.logger.info(f"Component {name} started")
            except Exception as e:
                component.state = ComponentState.FAILED
                component.error = str(e)
                self.logger.error(f"Failed to start {name}: {e}", exc_info=True)
                raise
    
    async def stop_component(self, name: str):
        """Stop a component."""
        async with self._lock:
            if name not in self.components:
                self.logger.warning(f"Component {name} not registered")
                return
            
            component = self.components[name]
            
            if component.state != ComponentState.RUNNING:
                self.logger.warning(f"Component {name} not running (state: {component.state})")
                return
            
            component.state = ComponentState.STOPPING
            
            try:
                if component.shutdown_handler:
                    await component.shutdown_handler()
                component.state = ComponentState.STOPPED
                component.stopped_at = datetime.now()
                self.logger.info(f"Component {name} stopped")
            except Exception as e:
                component.state = ComponentState.FAILED
                component.error = str(e)
                self.logger.error(f"Failed to stop {name}: {e}", exc_info=True)
    
    async def check_health(self, name: str) -> bool:
        """Check component health."""
        if name not in self.components:
            return False
        
        component = self.components[name]
        
        if component.health_check:
            try:
                return await component.health_check()
            except Exception as e:
                self.logger.error(f"Health check failed for {name}: {e}")
                return False
        
        # Default: component is healthy if running
        return component.state == ComponentState.RUNNING
    
    async def shutdown_all(self):
        """Shutdown all components in reverse order of registration."""
        self.logger.info("Shutting down all components...")
        
        # Stop in reverse order
        component_names = list(reversed(self.components.keys()))
        
        for name in component_names:
            if self.components[name].state == ComponentState.RUNNING:
                await self.stop_component(name)
        
        self.logger.info("All components shut down")
    
    def get_status(self) -> Dict[str, Any]:
        """Get status of all components."""
        return {
            name: {
                "state": component.state.value,
                "initialized_at": component.initialized_at.isoformat() if component.initialized_at else None,
                "started_at": component.started_at.isoformat() if component.started_at else None,
                "stopped_at": component.stopped_at.isoformat() if component.stopped_at else None,
                "error": component.error
            }
            for name, component in self.components.items()
        }
