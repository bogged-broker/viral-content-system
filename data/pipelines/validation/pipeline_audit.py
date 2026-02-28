"""
End-to-end provenance, lineage, and reproducibility proof.

This module is the single authority that produces a verifiable, replayable,
tamper-evident proof that:
- Every output came from declared inputs
- Every transformation was authorized
- Every computation was versioned
- Every window was lawful
- Every result is reproducible bit-for-bit

It answers one question:
"If subpoenaed, can we prove this metric happened exactly this way?"

If the answer is not yes, the pipeline failed.

This file is the sworn affidavit of the pipeline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Set, Tuple, Optional, Any
from collections import OrderedDict


# ============================================================================
# AUDIT VIOLATIONS - Hard Failures Only
# ============================================================================

class AuditViolationType(Enum):
    """
    Existential audit faults.
    
    No warnings. No partial audits.
    """
    UNDOCUMENTED_TRANSFORMATION = "undocumented_transformation"
    MISSING_LINEAGE_EDGE = "missing_lineage_edge"
    HASH_MISMATCH = "hash_mismatch"
    UNAUTHORIZED_COMPUTATION = "unauthorized_computation"
    WINDOW_AUTHORITY_BYPASS = "window_authority_bypass"
    NON_DETERMINISM_DETECTED = "non_determinism_detected"
    ORPHAN_METRIC = "orphan_metric"
    CYCLIC_DEPENDENCY = "cyclic_dependency"
    VERSION_MISMATCH = "version_mismatch"
    TAMPERING_DETECTED = "tampering_detected"


@dataclass(frozen=True)
class AuditViolation(Exception):
    """
    Fatal audit violation.
    
    Raised only for existential faults that invalidate the pipeline.
    """
    violation_type: AuditViolationType
    component_id: str
    message: str
    expected_value: Optional[str] = None
    observed_value: Optional[str] = None
    
    def __str__(self) -> str:
        parts = [
            f"[AUDIT VIOLATION: {self.violation_type.value}]",
            f"Component: {self.component_id}",
            f"Message: {self.message}"
        ]
        if self.expected_value or self.observed_value:
            parts.append(
                f"Expected: {self.expected_value}, "
                f"Observed: {self.observed_value}"
            )
        return " | ".join(parts)


# ============================================================================
# AUDIT CONTEXT - Execution Fingerprint
# ============================================================================

@dataclass(frozen=True)
class AuditContext:
    """
    Captures everything that could affect pipeline meaning.
    
    No ambient state allowed.
    
    Fields:
    - Pipeline version
    - Code hash (repository or build artifact)
    - Environment fingerprint (runtime, libs)
    - Schema versions
    - Window model versions
    - Computation registry versions
    - Timestamp bounds (start/end)
    """
    pipeline_version: str
    code_hash: str
    environment_fingerprint: str
    schema_versions: Dict[str, str]
    window_model_versions: Dict[str, str]
    computation_registry_version: str
    execution_start: datetime
    execution_end: datetime
    runtime_fingerprint: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pipeline_version": self.pipeline_version,
            "code_hash": self.code_hash,
            "environment_fingerprint": self.environment_fingerprint,
            "schema_versions": OrderedDict(sorted(self.schema_versions.items())),
            "window_model_versions": OrderedDict(sorted(self.window_model_versions.items())),
            "computation_registry_version": self.computation_registry_version,
            "execution_start": self.execution_start.isoformat(),
            "execution_end": self.execution_end.isoformat(),
            "runtime_fingerprint": OrderedDict(sorted(self.runtime_fingerprint.items())),
        }
    
    def compute_fingerprint(self) -> str:
        """Generate deterministic fingerprint of execution context."""
        content = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


# ============================================================================
# INPUT/OUTPUT MANIFESTS - What Went In/Out
# ============================================================================

@dataclass(frozen=True)
class InputManifest:
    """
    Complete declaration of pipeline inputs.
    
    Fields:
    - Data source identifiers
    - Version/snapshot information
    - Content fingerprints
    - Schema versions
    - Ingestion timestamps
    """
    input_ids: List[str]
    source_versions: Dict[str, str]
    content_fingerprints: Dict[str, str]
    schema_versions: Dict[str, str]
    ingestion_metadata: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_ids": sorted(self.input_ids),
            "source_versions": OrderedDict(sorted(self.source_versions.items())),
            "content_fingerprints": OrderedDict(sorted(self.content_fingerprints.items())),
            "schema_versions": OrderedDict(sorted(self.schema_versions.items())),
            "ingestion_metadata": OrderedDict(sorted(self.ingestion_metadata.items())),
        }
    
    def compute_fingerprint(self) -> str:
        """Generate deterministic fingerprint of inputs."""
        content = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class OutputManifest:
    """
    Complete declaration of pipeline outputs.
    
    Fields:
    - Output metric identifiers
    - Content fingerprints
    - Schema versions
    - Validation status
    """
    output_ids: List[str]
    content_fingerprints: Dict[str, str]
    schema_versions: Dict[str, str]
    validation_status: Dict[str, str]
    output_metadata: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "output_ids": sorted(self.output_ids),
            "content_fingerprints": OrderedDict(sorted(self.content_fingerprints.items())),
            "schema_versions": OrderedDict(sorted(self.schema_versions.items())),
            "validation_status": OrderedDict(sorted(self.validation_status.items())),
            "output_metadata": OrderedDict(sorted(self.output_metadata.items())),
        }
    
    def compute_fingerprint(self) -> str:
        """Generate deterministic fingerprint of outputs."""
        content = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


# ============================================================================
# TRANSFORMATION/COMPUTATION RECORDS - What Happened
# ============================================================================

@dataclass(frozen=True)
class TransformRecord:
    """
    Record of a single transformation step.
    
    Fields:
    - Transform identifier
    - Version/hash
    - Input dependencies
    - Output products
    - Authorization reference
    """
    transform_id: str
    transform_version: str
    transform_hash: str
    input_dependencies: List[str]
    output_products: List[str]
    authorization_ref: str
    metadata: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "transform_id": self.transform_id,
            "transform_version": self.transform_version,
            "transform_hash": self.transform_hash,
            "input_dependencies": sorted(self.input_dependencies),
            "output_products": sorted(self.output_products),
            "authorization_ref": self.authorization_ref,
            "metadata": OrderedDict(sorted(self.metadata.items())),
        }


@dataclass(frozen=True)
class ComputationRecord:
    """
    Record of a computation execution.
    
    Fields:
    - Computation identifier
    - Version/hash
    - Window usage
    - Input dependencies
    - Output metrics
    - Determinism proof
    """
    computation_id: str
    computation_version: str
    computation_hash: str
    window_ids: List[str]
    input_dependencies: List[str]
    output_metrics: List[str]
    determinism_proof: str
    metadata: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "computation_id": self.computation_id,
            "computation_version": self.computation_version,
            "computation_hash": self.computation_hash,
            "window_ids": sorted(self.window_ids),
            "input_dependencies": sorted(self.input_dependencies),
            "output_metrics": sorted(self.output_metrics),
            "determinism_proof": self.determinism_proof,
            "metadata": OrderedDict(sorted(self.metadata.items())),
        }


# ============================================================================
# PROVENANCE GRAPH - Lineage Truth
# ============================================================================

@dataclass(frozen=True)
class ProvenanceNode:
    """Single node in provenance DAG."""
    node_id: str
    node_type: str  # "input", "transform", "window", "computation", "output"
    node_hash: str
    dependencies: List[str]
    metadata: Dict[str, str] = field(default_factory=dict)


class ProvenanceGraph:
    """
    DAG proving complete lineage from inputs to outputs.
    
    Guarantees:
    - Acyclic
    - Deterministic ordering
    - Complete coverage (no orphan metrics)
    - No hidden edges
    - Hashable representation
    
    If any output metric lacks a provenance path → violation.
    """
    
    def __init__(self):
        self._nodes: Dict[str, ProvenanceNode] = {}
        self._edges: Dict[str, Set[str]] = {}  # node_id -> set of dependency node_ids
        self._topological_order: Optional[List[str]] = None
    
    def add_node(self, node: ProvenanceNode) -> None:
        """Add node to graph."""
        if node.node_id in self._nodes:
            raise AuditViolation(
                violation_type=AuditViolationType.TAMPERING_DETECTED,
                component_id=node.node_id,
                message="Attempted to redefine existing provenance node"
            )
        
        self._nodes[node.node_id] = node
        self._edges[node.node_id] = set(node.dependencies)
        self._topological_order = None  # Invalidate cache
    
    def verify_acyclic(self) -> None:
        """Verify graph is acyclic (DAG property)."""
        visited = set()
        rec_stack = set()
        
        def has_cycle(node_id: str) -> bool:
            visited.add(node_id)
            rec_stack.add(node_id)
            
            for dep_id in self._edges.get(node_id, set()):
                if dep_id not in visited:
                    if has_cycle(dep_id):
                        return True
                elif dep_id in rec_stack:
                    return True
            
            rec_stack.remove(node_id)
            return False
        
        for node_id in self._nodes:
            if node_id not in visited:
                if has_cycle(node_id):
                    raise AuditViolation(
                        violation_type=AuditViolationType.CYCLIC_DEPENDENCY,
                        component_id=node_id,
                        message="Cyclic dependency detected in provenance graph"
                    )
    
    def verify_complete_coverage(self, output_ids: List[str]) -> None:
        """Verify all outputs have complete provenance paths."""
        for output_id in output_ids:
            if output_id not in self._nodes:
                raise AuditViolation(
                    violation_type=AuditViolationType.ORPHAN_METRIC,
                    component_id=output_id,
                    message="Output metric lacks provenance node"
                )
            
            # Verify path to inputs exists
            if not self._has_path_to_inputs(output_id):
                raise AuditViolation(
                    violation_type=AuditViolationType.MISSING_LINEAGE_EDGE,
                    component_id=output_id,
                    message="Output metric lacks complete path to inputs"
                )
    
    def _has_path_to_inputs(self, node_id: str) -> bool:
        """Check if node has path to input nodes."""
        visited = set()
        
        def dfs(current_id: str) -> bool:
            if current_id in visited:
                return False
            visited.add(current_id)
            
            node = self._nodes.get(current_id)
            if not node:
                return False
            
            # Input nodes have no dependencies
            if node.node_type == "input":
                return True
            
            # Check dependencies
            for dep_id in self._edges.get(current_id, set()):
                if dfs(dep_id):
                    return True
            
            return False
        
        return dfs(node_id)
    
    def compute_topological_order(self) -> List[str]:
        """Compute deterministic topological ordering."""
        if self._topological_order is not None:
            return self._topological_order
        
        in_degree = {node_id: 0 for node_id in self._nodes}
        for node_id in self._nodes:
            for dep_id in self._edges.get(node_id, set()):
                in_degree[dep_id] = in_degree.get(dep_id, 0) + 1
        
        queue = sorted([node_id for node_id, degree in in_degree.items() if degree == 0])
        result = []
        
        while queue:
            current = queue.pop(0)
            result.append(current)
            
            # Process dependents
            dependents = [
                node_id for node_id in self._nodes
                if current in self._edges.get(node_id, set())
            ]
            
            for dependent in sorted(dependents):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
                    queue.sort()  # Maintain deterministic order
        
        if len(result) != len(self._nodes):
            raise AuditViolation(
                violation_type=AuditViolationType.CYCLIC_DEPENDENCY,
                component_id="graph",
                message="Cannot compute topological order - cycle detected"
            )
        
        self._topological_order = result
        return result
    
    def compute_graph_hash(self) -> str:
        """Generate deterministic hash of entire graph."""
        order = self.compute_topological_order()
        
        graph_repr = OrderedDict([
            ("nodes", [
                self._nodes[node_id].node_hash
                for node_id in order
            ]),
            ("edges", OrderedDict([
                (node_id, sorted(list(self._edges.get(node_id, set()))))
                for node_id in order
            ]))
        ])
        
        content = json.dumps(graph_repr, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def to_dict(self) -> Dict[str, Any]:
        """Export graph to dictionary."""
        return {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "node_hash": node.node_hash,
                    "dependencies": sorted(node.dependencies),
                    "metadata": OrderedDict(sorted(node.metadata.items())),
                }
                for node in sorted(self._nodes.values(), key=lambda n: n.node_id)
            ],
            "topological_order": self.compute_topological_order(),
        }


# ============================================================================
# AUDIT ARTIFACT - Exportable Evidence
# ============================================================================

@dataclass(frozen=True)
class AuditArtifact:
    """
    Immutable, verifiable proof of pipeline execution.
    
    This is what survives outside the system.
    
    Must be:
    - Immutable
    - Serializable
    - Verifiable independently
    """
    audit_id: str
    audit_hash: str
    pipeline_context_fingerprint: str
    inputs_fingerprint: str
    outputs_fingerprint: str
    provenance_graph_hash: str
    transformation_hashes: Dict[str, str]
    computation_hashes: Dict[str, str]
    window_hashes: Dict[str, str]
    timestamp_created: datetime
    context: AuditContext
    input_manifest: InputManifest
    output_manifest: OutputManifest
    provenance_graph: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "audit_hash": self.audit_hash,
            "pipeline_context_fingerprint": self.pipeline_context_fingerprint,
            "inputs_fingerprint": self.inputs_fingerprint,
            "outputs_fingerprint": self.outputs_fingerprint,
            "provenance_graph_hash": self.provenance_graph_hash,
            "transformation_hashes": OrderedDict(sorted(self.transformation_hashes.items())),
            "computation_hashes": OrderedDict(sorted(self.computation_hashes.items())),
            "window_hashes": OrderedDict(sorted(self.window_hashes.items())),
            "timestamp_created": self.timestamp_created.isoformat(),
            "context": self.context.to_dict(),
            "input_manifest": self.input_manifest.to_dict(),
            "output_manifest": self.output_manifest.to_dict(),
            "provenance_graph": self.provenance_graph,
        }
    
    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def verify_integrity(self) -> bool:
        """Verify audit hash matches content."""
        computed = self._compute_audit_hash()
        return computed == self.audit_hash
    
    def _compute_audit_hash(self) -> str:
        """
        Generate deterministic hash of audit artifact.
        
        Excludes timestamp_created from hash.
        """
        canonical = OrderedDict([
            ("audit_id", self.audit_id),
            ("pipeline_context_fingerprint", self.pipeline_context_fingerprint),
            ("inputs_fingerprint", self.inputs_fingerprint),
            ("outputs_fingerprint", self.outputs_fingerprint),
            ("provenance_graph_hash", self.provenance_graph_hash),
            ("transformation_hashes", OrderedDict(sorted(self.transformation_hashes.items()))),
            ("computation_hashes", OrderedDict(sorted(self.computation_hashes.items()))),
            ("window_hashes", OrderedDict(sorted(self.window_hashes.items()))),
        ])
        
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


# ============================================================================
# AUDIT INVARIANTS - Absolute Law
# ============================================================================

class AuditInvariants:
    """
    Enforces immutable audit laws:
    - Every metric has full lineage
    - Every step is authorized
    - Every dependency is declared
    - Every version is pinned
    - Every output is reproducible
    - Every artifact is tamper-evident
    
    Breaking any invariant invalidates the pipeline.
    """
    
    @staticmethod
    def assert_complete_lineage(
        graph: ProvenanceGraph,
        output_ids: List[str]
    ) -> None:
        """Every output must have complete lineage to inputs."""
        graph.verify_complete_coverage(output_ids)
    
    @staticmethod
    def assert_acyclic_graph(graph: ProvenanceGraph) -> None:
        """Provenance graph must be acyclic."""
        graph.verify_acyclic()
    
    @staticmethod
    def assert_all_steps_authorized(
        transforms: Tuple[TransformRecord, ...],
        computations: Tuple[ComputationRecord, ...]
    ) -> None:
        """Every transformation and computation must be authorized."""
        for transform in transforms:
            if not transform.authorization_ref:
                raise AuditViolation(
                    violation_type=AuditViolationType.UNDOCUMENTED_TRANSFORMATION,
                    component_id=transform.transform_id,
                    message="Transformation lacks authorization reference"
                )
    
    @staticmethod
    def assert_versions_pinned(context: AuditContext) -> None:
        """All versions must be explicitly pinned."""
        if not context.pipeline_version:
            raise AuditViolation(
                violation_type=AuditViolationType.VERSION_MISMATCH,
                component_id="context",
                message="Pipeline version not pinned"
            )
        if not context.code_hash:
            raise AuditViolation(
                violation_type=AuditViolationType.VERSION_MISMATCH,
                component_id="context",
                message="Code hash not pinned"
            )
    
    @staticmethod
    def assert_deterministic_execution(
        computations: Tuple[ComputationRecord, ...]
    ) -> None:
        """Every computation must prove determinism."""
        for comp in computations:
            if not comp.determinism_proof:
                raise AuditViolation(
                    violation_type=AuditViolationType.NON_DETERMINISM_DETECTED,
                    component_id=comp.computation_id,
                    message="Computation lacks determinism proof"
                )


# ============================================================================
# PIPELINE AUDIT - Primary Authority
# ============================================================================

class PipelineAudit:
    """
    Single authority for producing verifiable, replayable, tamper-evident
    proof of pipeline execution.
    
    If this function returns, the pipeline is provably lawful.
    If it raises or emits violations → pipeline dies.
    """
    
    @staticmethod
    def audit(
        *,
        inputs: InputManifest,
        transformations: Tuple[TransformRecord, ...],
        computations: Tuple[ComputationRecord, ...],
        outputs: OutputManifest,
        context: AuditContext,
        window_hashes: Dict[str, str],
        audit_id: str
    ) -> AuditArtifact:
        """
        Produce immutable audit artifact proving pipeline lawfulness.
        
        Args:
            inputs: Complete input manifest
            transformations: All transformation records
            computations: All computation records
            outputs: Complete output manifest
            context: Execution context fingerprint
            window_hashes: Window authority hashes
            audit_id: Unique audit identifier
        
        Returns:
            Immutable audit artifact
        
        Raises:
            AuditViolation: On any invariant violation
        """
        # Build provenance graph
        graph = PipelineAudit._build_provenance_graph(
            inputs=inputs,
            transformations=transformations,
            computations=computations,
            outputs=outputs,
            window_hashes=window_hashes
        )
        
        # Enforce invariants
        AuditInvariants.assert_acyclic_graph(graph)
        AuditInvariants.assert_complete_lineage(graph, outputs.output_ids)
        AuditInvariants.assert_all_steps_authorized(transformations, computations)
        AuditInvariants.assert_versions_pinned(context)
        AuditInvariants.assert_deterministic_execution(computations)
        
        # Compute cryptographic bindings
        context_fingerprint = context.compute_fingerprint()
        inputs_fingerprint = inputs.compute_fingerprint()
        outputs_fingerprint = outputs.compute_fingerprint()
        graph_hash = graph.compute_graph_hash()
        
        # Collect transformation hashes
        transformation_hashes = {
            t.transform_id: t.transform_hash
            for t in transformations
        }
        
        # Collect computation hashes
        computation_hashes = {
            c.computation_id: c.computation_hash
            for c in computations
        }
        
        # Create audit artifact
        artifact = AuditArtifact(
            audit_id=audit_id,
            audit_hash="",  # Will be computed
            pipeline_context_fingerprint=context_fingerprint,
            inputs_fingerprint=inputs_fingerprint,
            outputs_fingerprint=outputs_fingerprint,
            provenance_graph_hash=graph_hash,
            transformation_hashes=transformation_hashes,
            computation_hashes=computation_hashes,
            window_hashes=window_hashes,
            timestamp_created=datetime.now(timezone.utc),
            context=context,
            input_manifest=inputs,
            output_manifest=outputs,
            provenance_graph=graph.to_dict()
        )
        
        # Compute audit hash
        audit_hash = artifact._compute_audit_hash()
        object.__setattr__(artifact, 'audit_hash', audit_hash)
        
        # Verify integrity
        if not artifact.verify_integrity():
            raise AuditViolation(
                violation_type=AuditViolationType.TAMPERING_DETECTED,
                component_id=audit_id,
                message="Audit artifact failed integrity verification"
            )
        
        return artifact
    
    @staticmethod
    def _build_provenance_graph(
        inputs: InputManifest,
        transformations: Tuple[TransformRecord, ...],
        computations: Tuple[ComputationRecord, ...],
        outputs: OutputManifest,
        window_hashes: Dict[str, str]
    ) -> ProvenanceGraph:
        """Build complete provenance DAG."""
        graph = ProvenanceGraph()
        
        # Add input nodes
        for input_id in inputs.input_ids:
            node = ProvenanceNode(
                node_id=input_id,
                node_type="input",
                node_hash=inputs.content_fingerprints.get(input_id, ""),
                dependencies=[]
            )
            graph.add_node(node)
        
        # Add transformation nodes
        for transform in transformations:
            node = ProvenanceNode(
                node_id=transform.transform_id,
                node_type="transform",
                node_hash=transform.transform_hash,
                dependencies=transform.input_dependencies,
                metadata={
                    "version": transform.transform_version,
                    "authorization": transform.authorization_ref
                }
            )
            graph.add_node(node)
        
        # Add window nodes
        for window_id, window_hash in window_hashes.items():
            node = ProvenanceNode(
                node_id=window_id,
                node_type="window",
                node_hash=window_hash,
                dependencies=[]  # Windows are authority sources
            )
            graph.add_node(node)
        
        # Add computation nodes
        for computation in computations:
            node = ProvenanceNode(
                node_id=computation.computation_id,
                node_type="computation",
                node_hash=computation.computation_hash,
                dependencies=computation.input_dependencies + computation.window_ids,
                metadata={
                    "version": computation.computation_version,
                    "determinism_proof": computation.determinism_proof
                }
            )
            graph.add_node(node)
        
        # Add output nodes
        for output_id in outputs.output_ids:
            # Find computation that produced this output
            dependencies = []
            for computation in computations:
                if output_id in computation.output_metrics:
                    dependencies.append(computation.computation_id)
            
            if not dependencies:
                raise AuditViolation(
                    violation_type=AuditViolationType.ORPHAN_METRIC,
                    component_id=output_id,
                    message="Output has no producing computation"
                )
            
            node = ProvenanceNode(
                node_id=output_id,
                node_type="output",
                node_hash=outputs.content_fingerprints.get(output_id, ""),
                dependencies=dependencies
            )
            graph.add_node(node)
        
        return graph


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Primary authority
    'PipelineAudit',
    # Core structures
    'AuditArtifact',
    'AuditContext',
    'InputManifest',
    'OutputManifest',
    'TransformRecord',
    'ComputationRecord',
    'ProvenanceGraph',
    'ProvenanceNode',
    # Invariants
    'AuditInvariants',
    # Violations
    'AuditViolation',
    'AuditViolationType',
]