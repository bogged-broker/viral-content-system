"""
/orchestration/shutdown_manager.py

Graceful Shutdown Management

Manages graceful shutdown of all system components:
- Ordered shutdown sequence
- Timeout handling
- Resource cleanup
- State persistence
"""

import asyncio
import logging
from typing import List, Dict, Any, Callable, Optional
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta


class ShutdownPhase(Enum):
    """Shutdown phases in execution order (reverse of startup)."""
    STOP_MONITORING = "stop_monitoring"
    STOP_COMPONENTS = "stop_components"
    STOP_PIPELINES = "stop_pipelines"
    RELEASE_GOVERNANCE = "release_governance"
    PERSIST_STATE = "persist_state"
    CLEANUP_INFRA = "cleanup_infra"
    FINAL = "final"


@dataclass
class ShutdownStep:
    """A single step in the shutdown sequence."""
    phase: ShutdownPhase
    name: str
    handler: Callable
    timeout: Optional[float] = 30.0
    required: bool = True
    order: int = 0


class ShutdownManager:
    """
    Manages graceful shutdown of system components.
    
    Usage:
        manager = ShutdownManager()
        manager.register_shutdown_step("component", stop_func)
        await manager.shutdown()
    """
    
    def __init__(self, global_timeout: float = 300.0):
        """
        Initialize shutdown manager.
        
        Args:
            global_timeout: Maximum time for entire shutdown (seconds)
        """
        self.logger = logging.getLogger(__name__)
        self.steps: List[ShutdownStep] = []
        self.global_timeout = global_timeout
        self.shutdown_started = False
        self.shutdown_complete = False
    
    def register_shutdown_step(
        self,
        phase: ShutdownPhase,
        name: str,
        handler: Callable,
        timeout: Optional[float] = 30.0,
        required: bool = True,
        order: int = 0
    ):
        """Register a shutdown step."""
        step = ShutdownStep(
            phase=phase,
            name=name,
            handler=handler,
            timeout=timeout,
            required=required,
            order=order
        )
        self.steps.append(step)
        # Sort by phase order, then by custom order
        phase_order = {p: idx for idx, p in enumerate(ShutdownPhase)}
        self.steps.sort(key=lambda s: (phase_order[s.phase], s.order))
    
    async def shutdown(self) -> bool:
        """
        Execute graceful shutdown.
        
        Returns:
            True if shutdown completed successfully, False otherwise
        """
        if self.shutdown_started:
            self.logger.warning("Shutdown already in progress")
            return False
        
        self.shutdown_started = True
        self.logger.info("Initiating graceful shutdown...")
        
        start_time = datetime.now()
        
        try:
            # Execute shutdown with global timeout
            await asyncio.wait_for(self._execute_shutdown(), timeout=self.global_timeout)
            
            elapsed = (datetime.now() - start_time).total_seconds()
            self.logger.info(f"Shutdown completed in {elapsed:.2f}s")
            self.shutdown_complete = True
            return True
            
        except asyncio.TimeoutError:
            elapsed = (datetime.now() - start_time).total_seconds()
            self.logger.error(f"Shutdown timed out after {elapsed:.2f}s")
            return False
        
        except Exception as e:
            self.logger.error(f"Shutdown error: {e}", exc_info=True)
            return False
    
    async def _execute_shutdown(self):
        """Execute shutdown steps."""
        # Group steps by phase
        phase_steps: Dict[ShutdownPhase, List[ShutdownStep]] = {}
        for step in self.steps:
            if step.phase not in phase_steps:
                phase_steps[step.phase] = []
            phase_steps[step.phase].append(step)
        
        # Execute phases in order
        for phase in ShutdownPhase:
            if phase not in phase_steps:
                continue
            
            self.logger.info(f"Shutdown phase: {phase.value}")
            
            # Execute steps in this phase
            for step in phase_steps[phase]:
                self.logger.info(f"Executing shutdown step: {step.name}")
                
                try:
                    await asyncio.wait_for(step.handler(), timeout=step.timeout)
                    self.logger.info(f"Shutdown step {step.name} completed")
                    
                except asyncio.TimeoutError:
                    error_msg = f"Shutdown step {step.name} timed out after {step.timeout}s"
                    if step.required:
                        self.logger.error(error_msg)
                        raise
                    else:
                        self.logger.warning(error_msg)
                
                except Exception as e:
                    error_msg = f"Shutdown step {step.name} failed: {e}"
                    if step.required:
                        self.logger.error(error_msg, exc_info=True)
                        raise
                    else:
                        self.logger.warning(error_msg)
    
    def get_status(self) -> Dict[str, Any]:
        """Get shutdown status."""
        return {
            "shutdown_started": self.shutdown_started,
            "shutdown_complete": self.shutdown_complete,
            "registered_steps": len(self.steps)
        }
