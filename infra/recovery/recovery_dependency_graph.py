"""
/infra/recovery/recovery_dependency_graph.py

Explicit dependency graph model for recovery stages.

No implicit ordering - all dependencies must be explicitly declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Optional, Tuple, List

from .recovery_models import RecoveryStage, RecoveryConfig, CheckpointSnapshot, PipelineStateSnapshot


@dataclass(frozen=True)
class StageDependencyGraph:
    """
    Explicit dependency graph for recovery stages.
    
    No implicit ordering - all dependencies must be explicitly declared.
    """
    prerequisites: Mapping[RecoveryStage, FrozenSet[RecoveryStage]]
    """Map from stage to its prerequisite stages"""
    
    @classmethod
    def from_config(cls, config: RecoveryConfig) -> StageDependencyGraph:
        """Create dependency graph from recovery config."""
        prerequisites: Dict[RecoveryStage, FrozenSet[RecoveryStage]] = {}
        
        for stage, deps in config.stage_dependencies.items():
            prerequisites[stage] = frozenset(deps)
        
        return cls(prerequisites=prerequisites)
    
    def get_prerequisites(self, stage: RecoveryStage) -> FrozenSet[RecoveryStage]:
        """Get prerequisite stages for a given stage."""
        return self.prerequisites.get(stage, frozenset())
    
    def validate_dependencies(
        self,
        checkpoint_snapshot: CheckpointSnapshot,
        pipeline_snapshot: PipelineStateSnapshot,
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate stage dependencies are satisfied.
        
        Ensures that prerequisite stages are complete before
        downstream stages can resume.
        
        Returns:
            (is_valid, error_message)
        """
        for stage, prereqs in self.prerequisites.items():
            stage_state = pipeline_snapshot.get_stage(stage)
            if not stage_state:
                continue
            
            # If stage is in progress or failed, check prerequisites
            if stage_state.status in ("IN_PROGRESS", "FAILED"):
                for prereq_stage in prereqs:
                    prereq_state = pipeline_snapshot.get_stage(prereq_stage)
                    if not prereq_state:
                        return False, (
                            f"Stage {stage.value} depends on {prereq_stage.value} "
                            f"but prerequisite state not found"
                        )
                    
                    if prereq_state.status != "COMPLETE":
                        return False, (
                            f"Stage {stage.value} depends on {prereq_stage.value} "
                            f"but prerequisite is {prereq_state.status}"
                        )
                    
                    if not prereq_state.is_atomic_boundary:
                        return False, (
                            f"Stage {stage.value} depends on {prereq_stage.value} "
                            f"but prerequisite is not at atomic boundary"
                        )
        
        return True, None
    
    def get_affected_stages(
        self,
        starting_stage: RecoveryStage,
    ) -> FrozenSet[RecoveryStage]:
        """
        Get all stages that depend on the starting stage (transitive closure).
        
        Returns all stages that would be affected if starting_stage is rewound.
        """
        affected: set[RecoveryStage] = {starting_stage}
        to_process: list[RecoveryStage] = [starting_stage]
        
        while to_process:
            current = to_process.pop()
            # Find all stages that depend on current
            for stage, prereqs in self.prerequisites.items():
                if current in prereqs and stage not in affected:
                    affected.add(stage)
                    to_process.append(stage)
        
        return frozenset(affected)
