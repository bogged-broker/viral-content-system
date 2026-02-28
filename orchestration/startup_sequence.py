"""
/orchestration/startup_sequence.py

Deterministic Startup Sequence

Defines the exact order in which system components must start.
This ensures proper dependency resolution and initialization.
"""

import logging
from enum import Enum
from typing import List, Dict, Any, Callable, Optional
from dataclasses import dataclass


class StartupPhase(Enum):
    """Startup phases in execution order."""
    BOOTSTRAP = "bootstrap"
    CONFIG_LOAD = "config_load"
    INFRA_INIT = "infra_init"
    LINEAGE_VALIDATE = "lineage_validate"
    GOVERNANCE_LOCK = "governance_lock"
    PIPELINE_LOAD = "pipeline_load"
    COMPONENT_START = "component_start"
    MONITORING_START = "monitoring_start"


@dataclass
class StartupStep:
    """A single step in the startup sequence."""
    phase: StartupPhase
    name: str
    handler: Callable
    dependencies: List[str] = None
    required: bool = True
    timeout: Optional[float] = None
    
    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = []


class StartupSequence:
    """
    Manages deterministic startup sequence.
    
    Ensures components start in the correct order with proper
    dependency resolution.
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.steps: List[StartupStep] = []
        self.completed_phases: set = set()
        self.failed_phases: set = set()
    
    def add_step(
        self,
        phase: StartupPhase,
        name: str,
        handler: Callable,
        dependencies: List[str] = None,
        required: bool = True,
        timeout: Optional[float] = None
    ):
        """Add a step to the startup sequence."""
        step = StartupStep(
            phase=phase,
            name=name,
            handler=handler,
            dependencies=dependencies or [],
            required=required,
            timeout=timeout
        )
        self.steps.append(step)
    
    async def execute(self) -> bool:
        """
        Execute the startup sequence.
        
        Returns:
            True if all required steps completed, False otherwise
        """
        self.logger.info("Executing startup sequence...")
        
        # Sort steps by phase order
        phase_order = {phase: idx for idx, phase in enumerate(StartupPhase)}
        sorted_steps = sorted(self.steps, key=lambda s: phase_order[s.phase])
        
        for step in sorted_steps:
            # Check dependencies
            if step.dependencies:
                missing_deps = [dep for dep in step.dependencies if dep not in self.completed_phases]
                if missing_deps:
                    error_msg = f"Step {step.name} has unmet dependencies: {missing_deps}"
                    if step.required:
                        self.logger.error(error_msg)
                        return False
                    else:
                        self.logger.warning(error_msg)
                        continue
            
            # Skip if phase already completed
            if step.phase in self.completed_phases:
                continue
            
            # Skip if phase already failed
            if step.phase in self.failed_phases:
                if step.required:
                    self.logger.error(f"Required phase {step.phase.value} failed, aborting")
                    return False
                continue
            
            # Execute step
            self.logger.info(f"Executing step: {step.name} (phase: {step.phase.value})")
            
            try:
                import asyncio
                
                if step.timeout:
                    await asyncio.wait_for(step.handler(), timeout=step.timeout)
                else:
                    await step.handler()
                
                self.completed_phases.add(step.phase)
                self.logger.info(f"Step {step.name} completed")
                
            except asyncio.TimeoutError:
                error_msg = f"Step {step.name} timed out after {step.timeout}s"
                if step.required:
                    self.logger.error(error_msg)
                    self.failed_phases.add(step.phase)
                    return False
                else:
                    self.logger.warning(error_msg)
                    self.failed_phases.add(step.phase)
            
            except Exception as e:
                error_msg = f"Step {step.name} failed: {e}"
                if step.required:
                    self.logger.error(error_msg, exc_info=True)
                    self.failed_phases.add(step.phase)
                    return False
                else:
                    self.logger.warning(error_msg)
                    self.failed_phases.add(step.phase)
        
        self.logger.info("Startup sequence completed")
        return True
    
    def get_status(self) -> Dict[str, Any]:
        """Get current startup status."""
        return {
            "completed_phases": [p.value for p in self.completed_phases],
            "failed_phases": [p.value for p in self.failed_phases],
            "total_steps": len(self.steps)
        }
