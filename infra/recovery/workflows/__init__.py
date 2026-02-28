"""
/recovery/workflows/__init__.py

Workflow Recovery Boundary Definition

This file defines what the workflow subsystem exposes to the rest of the system.

PUBLIC EXPORTS ONLY:
    - Core workflow models
    - Damage models
    - Repair models

EXPLICITLY NOT EXPOSED:
    - Repair strategies (internal implementation)
    - Replay executors (internal implementation)
    - Merge logic (internal implementation)
    - Validation engines (internal implementation)

This keeps layering clean and replay-safe.
"""

from .workflow_models import (
    # Core Workflow Models
    WorkflowId,
    WorkflowNode,
    WorkflowEdge,
    WorkflowArtifact,
    WorkflowDAG,
    
    # Damage Models
    WorkflowDamage,
    DamageAssessment,
    
    # Damage Type Constants
    DAMAGE_TYPE_NODE_CORRUPTION,
    DAMAGE_TYPE_EDGE_INVALID,
    DAMAGE_TYPE_ARTIFACT_MISMATCH,
    DAMAGE_TYPE_SCHEMA_DRIFT,
    DAMAGE_TYPE_MISSING_OUTPUT,
    DAMAGE_TYPE_UNEXPECTED_OUTPUT,
    DAMAGE_TYPE_NON_DETERMINISM,
    VALID_DAMAGE_TYPES,
    
    # Repair Models
    RepairPlan,
    RepairPlanStep,
    WorkflowRepairResult,
    
    # Repair Scope Constants
    REPAIR_SCOPE_NODE,
    REPAIR_SCOPE_EDGE,
    REPAIR_SCOPE_SUBGRAPH,
    VALID_REPAIR_SCOPES,
)

__all__ = [
    # Core Workflow Models
    'WorkflowId',
    'WorkflowNode',
    'WorkflowEdge',
    'WorkflowArtifact',
    'WorkflowDAG',
    
    # Damage Models
    'WorkflowDamage',
    'DamageAssessment',
    
    # Damage Type Constants
    'DAMAGE_TYPE_NODE_CORRUPTION',
    'DAMAGE_TYPE_EDGE_INVALID',
    'DAMAGE_TYPE_ARTIFACT_MISMATCH',
    'DAMAGE_TYPE_SCHEMA_DRIFT',
    'DAMAGE_TYPE_MISSING_OUTPUT',
    'DAMAGE_TYPE_UNEXPECTED_OUTPUT',
    'DAMAGE_TYPE_NON_DETERMINISM',
    'VALID_DAMAGE_TYPES',
    
    # Repair Models
    'RepairPlan',
    'RepairPlanStep',
    'WorkflowRepairResult',
    
    # Repair Scope Constants
    'REPAIR_SCOPE_NODE',
    'REPAIR_SCOPE_EDGE',
    'REPAIR_SCOPE_SUBGRAPH',
    'VALID_REPAIR_SCOPES',
]

__version__ = '1.0.0'