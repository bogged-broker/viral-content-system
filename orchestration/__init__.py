"""
Orchestration package for Viral Content System

Provides:
- SystemOrchestrator: Main runtime flow controller
- LifecycleManager: Component lifecycle management
- StartupSequence: Deterministic startup ordering
- ShutdownManager: Graceful shutdown handling
"""

from orchestration.system_orchestrator import SystemOrchestrator, ExecutionMode, start_system
from orchestration.lifecycle_manager import LifecycleManager, ComponentState
from orchestration.startup_sequence import StartupSequence, StartupPhase
from orchestration.shutdown_manager import ShutdownManager, ShutdownPhase

__all__ = [
    'SystemOrchestrator',
    'ExecutionMode',
    'start_system',
    'LifecycleManager',
    'ComponentState',
    'StartupSequence',
    'StartupPhase',
    'ShutdownManager',
    'ShutdownPhase',
]
