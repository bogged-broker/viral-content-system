"""
Replay lineage and dependency graph authority.

This module is the single authority for:
- Execution graph construction from lineage records
- Stage dependency inference from lineage data
- Graph topology verification

The runner delegates all graph construction to this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Any, Protocol
from collections import OrderedDict


@dataclass(frozen=True)
class ExecutionGraph:
    """
    Immutable execution graph structure.
    
    Represents the topology of stage dependencies and outputs.
    """
    stages: List[str]
    stage_dependencies: Dict[str, List[str]]  # stage_id -> [dependency_stage_ids]
    stage_outputs: Dict[str, str]  # stage_id -> output_hash
    stage_inputs: Dict[str, str]  # stage_id -> input_fingerprint
    
    def compute_hash(self) -> str:
        """Compute deterministic hash of graph structure."""
        canonical = OrderedDict([
            ("stages", sorted(self.stages)),
            ("stage_dependencies", OrderedDict(
                sorted((k, sorted(v)) for k, v in self.stage_dependencies.items())
            )),
            ("stage_outputs", OrderedDict(sorted(self.stage_outputs.items()))),
            ("stage_inputs", OrderedDict(sorted(self.stage_inputs.items()))),
        ])
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "stages": sorted(self.stages),
            "stage_dependencies": {
                k: sorted(v) for k, v in self.stage_dependencies.items()
            },
            "stage_outputs": self.stage_outputs,
            "stage_inputs": self.stage_inputs,
        }


class LineageAuthority(Protocol):
    """
    Protocol for lineage authority that provides dependency information.
    
    The runner delegates all dependency inference to implementations of this protocol.
    """
    
    def get_stage_dependencies(
        self,
        stage_id: str,
        stage_outputs: Dict[str, Any],
        all_stages: List[str],
        executed_stages: List[str]
    ) -> List[str]:
        """
        Determine which stages a given stage depends on.
        
        Args:
            stage_id: The stage to analyze
            stage_outputs: Output data from the stage (for analysis)
            all_stages: All stages in the replay plan
            executed_stages: Stages that have already been executed
            
        Returns:
            List of stage IDs that this stage depends on
        """
        ...
    
    def build_execution_graph(
        self,
        stages: List[str],
        stage_results: Dict[str, Dict[str, Any]]
    ) -> ExecutionGraph:
        """
        Build execution graph from stage execution results.
        
        Args:
            stages: All stages in the replay plan
            stage_results: Dict mapping stage_id to result data containing:
                - output_hash: str
                - input_fingerprint: str
                - dependencies: List[str] (from get_stage_dependencies)
        
        Returns:
            ExecutionGraph with complete topology
        """
        ...


class DefaultLineageAuthority:
    """
    Default implementation of LineageAuthority.
    
    This is a fallback implementation. In production, this should be replaced
    with a proper lineage system that reads from audit records.
    """
    
    def get_stage_dependencies(
        self,
        stage_id: str,
        stage_outputs: Dict[str, Any],
        all_stages: List[str],
        executed_stages: List[str]
    ) -> List[str]:
        """
        Determine stage dependencies using lineage data.
        
        This implementation uses a simple heuristic as a fallback.
        Production implementations should read from lineage records.
        """
        dependencies = []
        
        # Check if output references other stage outputs
        # In production, this would query lineage records
        output_str = json.dumps(stage_outputs, sort_keys=True)
        for other_stage in all_stages:
            if other_stage == stage_id:
                continue
            
            # Check if this stage's output references the other stage
            if other_stage in output_str and other_stage in executed_stages:
                dependencies.append(other_stage)
        
        # Stages executed before this one are potential dependencies
        # In production, this would be determined from lineage records
        executed_before = [
            s for s in executed_stages
            if s != stage_id
        ]
        
        # Combine and deduplicate
        all_deps = list(set(dependencies + executed_before))
        return sorted(all_deps)
    
    def build_execution_graph(
        self,
        stages: List[str],
        stage_results: Dict[str, Dict[str, Any]]
    ) -> ExecutionGraph:
        """Build execution graph from stage results."""
        stage_dependencies = {}
        stage_outputs = {}
        stage_inputs = {}
        
        for stage_id in stages:
            if stage_id in stage_results:
                result = stage_results[stage_id]
                stage_dependencies[stage_id] = result.get("dependencies", [])
                stage_outputs[stage_id] = result.get("output_hash", "")
                stage_inputs[stage_id] = result.get("input_fingerprint", "")
            else:
                # Stage not executed yet
                stage_dependencies[stage_id] = []
                stage_outputs[stage_id] = ""
                stage_inputs[stage_id] = ""
        
        return ExecutionGraph(
            stages=sorted(stages),
            stage_dependencies=stage_dependencies,
            stage_outputs=stage_outputs,
            stage_inputs=stage_inputs
        )


__all__ = [
    'ExecutionGraph',
    'LineageAuthority',
    'DefaultLineageAuthority',
]
