"""
/orchestration/dependency_graph.py

Artifact-Level Dependency Control for 240k+ LOC systems.

This file defines WHAT depends on WHAT at the data level.
Separate from execution_graph.py which defines WHEN things run.

Core Principle:
- Explicit artifact causality
- No implicit coupling
- Deterministic lineage
- RL-safe isolation
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Set, Tuple, Any
import hashlib
import json
import logging
from datetime import datetime
from collections import defaultdict

# Configure logging for dependency graph operations
logger = logging.getLogger(__name__)


# ============================================================================
# EXECUTION PHASE (imported concept from execution_graph.py)
# ============================================================================

class ExecutionPhase(Enum):
    """Phase boundaries enforced by execution graph"""
    INGESTION = "ingestion"
    EXTRACTION = "extraction"
    FEATURE_ENGINEERING = "feature_engineering"
    TRAINING = "training"
    EVALUATION = "evaluation"
    RANKING = "ranking"
    DEPLOYMENT = "deployment"


# ============================================================================
# ARTIFACT STATE
# ============================================================================

class ArtifactState(Enum):
    """Runtime state of an artifact"""
    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"
    STALE = "stale"
    INVALIDATED = "invalidated"


# ============================================================================
# ARTIFACT DEFINITION (NON-NEGOTIABLE)
# ============================================================================

@dataclass(frozen=True)
class ArtifactDefinition:
    """
    Complete specification of a single artifact.
    No optional fields. No dynamic schemas.
    """
    artifact_id: str                          # globally unique
    description: str
    
    producer_node: str                        # ExecutionNode.node_id
    phase: ExecutionPhase                     # enforced phase boundary
    
    schema_hash: str                          # structural identity
    version: str                              # semantic version (e.g., "1.2.0")
    
    required: bool                            # must exist?
    partial_allowed: bool                     # streaming/early-use allowed?
    
    deterministic: bool                       # reproducibility guarantee
    cacheable: bool                           # allowed to reuse?
    
    rl_safe: bool                             # allowed upstream of reward?
    
    observability_tags: Dict[str, str] = field(default_factory=dict)
    
    def __post_init__(self):
        # Validation: non-deterministic artifacts cannot be cacheable
        if self.cacheable and not self.deterministic:
            raise ValueError(
                f"Artifact {self.artifact_id}: non-deterministic artifacts "
                f"cannot be cacheable"
            )


# ============================================================================
# ARTIFACT DEPENDENCY (EXPLICIT CONSUMER CONTRACT)
# ============================================================================

@dataclass(frozen=True)
class ArtifactDependency:
    """
    Explicit declaration of consumer → artifact relationship.
    Every consumer relationship MUST be declared.
    """
    consumer_node: str                        # ExecutionNode.node_id
    artifact_id: str
    
    min_version: str                          # minimum acceptable version
    allow_partial: bool                       # can consume PARTIAL state?
    strict: bool                              # hard vs soft dependency


# ============================================================================
# ARTIFACT VERSION
# ============================================================================

@dataclass
class ArtifactVersion:
    """Runtime version tracking for artifact instance"""
    artifact_id: str
    version: str
    schema_hash: str
    
    created_at: datetime
    producer_node: str
    
    state: ArtifactState
    completion_percentage: float = 0.0        # for PARTIAL artifacts
    
    content_hash: Optional[str] = None        # actual data hash
    lineage: List[str] = field(default_factory=list)  # upstream artifact_ids
    
    # Deterministic verification fields
    producer_code_hash: Optional[str] = None  # hash of producer code at creation
    input_hashes: List[str] = field(default_factory=list)  # hashes of input artifacts


# ============================================================================
# COMPATIBILITY RESOLVER
# ============================================================================

class CompatibilityResolver:
    """Resolves version compatibility between artifacts and consumers"""
    
    @staticmethod
    def is_compatible(
        artifact_version: str,
        min_version: str
    ) -> bool:
        """
        Check if artifact version satisfies minimum version requirement.
        Uses semantic versioning comparison.
        """
        try:
            artifact_parts = [int(x) for x in artifact_version.split('.')]
            min_parts = [int(x) for x in min_version.split('.')]
            
            # Pad to same length
            max_len = max(len(artifact_parts), len(min_parts))
            artifact_parts += [0] * (max_len - len(artifact_parts))
            min_parts += [0] * (max_len - len(min_parts))
            
            return artifact_parts >= min_parts
        except (ValueError, AttributeError):
            # Fallback to string comparison if not semantic versioning
            return artifact_version >= min_version
    
    @staticmethod
    def check_schema_compatibility(
        artifact_hash: str,
        expected_hash: str
    ) -> bool:
        """Check if schema hashes match exactly"""
        return artifact_hash == expected_hash
    
    @staticmethod
    def find_version_conflicts(
        artifact_requirements: Dict[str, List[str]]
    ) -> List[Tuple[str, List[str]]]:
        """
        Find version conflicts where multiple consumers require incompatible versions.
        
        Returns: List of (artifact_id, [conflicting_versions]) tuples
        """
        conflicts = []
        
        for artifact_id, versions in artifact_requirements.items():
            if len(versions) < 2:
                continue  # No conflict possible with single requirement
            
            # Find maximum version requirement
            max_version = max(versions, key=lambda v: tuple(int(x) for x in v.split('.')))
            
            # Check if all versions are compatible with max
            incompatible = []
            for version in versions:
                if not CompatibilityResolver.is_compatible(max_version, version):
                    incompatible.append(version)
            
            if incompatible:
                conflicts.append((artifact_id, incompatible + [max_version]))
        
        return conflicts
    
    @staticmethod
    def compute_expected_hash(
        input_hashes: List[str],
        producer_code_hash: str,
        schema_hash: str,
        artifact_id: str
    ) -> str:
        """
        Compute expected content hash for deterministic artifact.
        
        Formula: hash(input_hashes + producer_code_hash + schema_hash + artifact_id)
        """
        combined = json.dumps({
            'inputs': sorted(input_hashes),  # Sort for determinism
            'producer_code': producer_code_hash,
            'schema': schema_hash,
            'artifact_id': artifact_id
        }, sort_keys=True)
        
        return hashlib.sha256(combined.encode()).hexdigest()


# ============================================================================
# COMPLETENESS CHECKER
# ============================================================================

class CompletenessChecker:
    """
    Enforces minimum data requirements and completeness constraints.
    Prevents early cutoff bias and partial signal leakage.
    """
    
    @staticmethod
    def check_completeness(
        artifact_state: ArtifactState,
        completion_percentage: float,
        allow_partial: bool,
        phase: ExecutionPhase
    ) -> Tuple[bool, str]:
        """
        Check if artifact meets completeness requirements.
        
        Returns: (is_valid, error_message)
        """
        # MISSING always fails
        if artifact_state == ArtifactState.MISSING:
            return False, "Artifact is MISSING"
        
        # INVALIDATED always fails
        if artifact_state == ArtifactState.INVALIDATED:
            return False, "Artifact is INVALIDATED"
        
        # STALE fails for critical phases
        if artifact_state == ArtifactState.STALE:
            if phase in [ExecutionPhase.TRAINING, ExecutionPhase.EVALUATION]:
                return False, "STALE artifacts not allowed in training/evaluation"
        
        # PARTIAL handling
        if artifact_state == ArtifactState.PARTIAL:
            if not allow_partial:
                return False, "PARTIAL state not allowed for this consumer"
            
            # Training/evaluation require COMPLETE
            if phase in [ExecutionPhase.TRAINING, ExecutionPhase.EVALUATION]:
                return False, "Training/evaluation requires COMPLETE artifacts"
            
            # Check minimum completion threshold
            if completion_percentage < 0.95:  # 95% minimum for partial use
                return False, f"Completion {completion_percentage:.1%} below minimum"
        
        return True, ""
    
    @staticmethod
    def validate_training_readiness(
        artifacts: Dict[str, ArtifactVersion]
    ) -> Tuple[bool, List[str]]:
        """
        Validate all artifacts are ready for training.
        Returns: (is_ready, list_of_issues)
        """
        issues = []
        
        for artifact_id, version in artifacts.items():
            if version.state != ArtifactState.COMPLETE:
                issues.append(
                    f"Artifact {artifact_id} is {version.state.value}, "
                    f"must be COMPLETE"
                )
            
            if version.completion_percentage < 1.0:
                issues.append(
                    f"Artifact {artifact_id} only {version.completion_percentage:.1%} "
                    f"complete"
                )
        
        return len(issues) == 0, issues


# ============================================================================
# INVALIDATION ENGINE (CRITICAL)
# ============================================================================

class InvalidationEngine:
    """
    Manages artifact invalidation cascades.
    When something changes, all affected artifacts must be invalidated.
    """
    
    def __init__(self):
        self.invalidation_log: List[Dict] = []
    
    def invalidate_artifact(
        self,
        artifact_id: str,
        reason: str,
        timestamp: datetime,
        dependency_graph: 'DependencyGraph'
    ) -> Set[str]:
        """
        Invalidate an artifact and cascade downstream.
        
        Returns: Set of all invalidated artifact_ids
        """
        invalidated = set()
        to_invalidate = [(artifact_id, reason, 0)]  # (id, reason, depth)
        max_depth = 1000  # Prevent infinite loops
        
        while to_invalidate:
            current_id, current_reason, depth = to_invalidate.pop(0)
            
            if current_id in invalidated:
                continue
            
            if depth > max_depth:
                logger.error(f"Invalidation cascade exceeded max depth for {current_id}")
                continue
            
            # Mark as invalidated
            if current_id in dependency_graph.artifact_versions:
                version = dependency_graph.artifact_versions[current_id]
                version.state = ArtifactState.INVALIDATED
            
            invalidated.add(current_id)
            
            # Log invalidation
            self.invalidation_log.append({
                'artifact_id': current_id,
                'reason': current_reason,
                'timestamp': timestamp.isoformat(),
                'cascade_from': artifact_id if current_id != artifact_id else None,
                'depth': depth
            })
            
            # Find all consumers (optimized: use index)
            consumers = dependency_graph.get_consumers(current_id)
            for consumer_artifact_id in consumers:
                to_invalidate.append(
                    (consumer_artifact_id, f"Upstream invalidation: {current_id}", depth + 1)
                )
        
        return invalidated
    
    def check_invalidation_triggers(
        self,
        artifact_def: ArtifactDefinition,
        new_schema_hash: str,
        new_version: str,
        producer_code_hash: str,
        previous_code_hash: Optional[str]
    ) -> Optional[str]:
        """
        Check if changes trigger invalidation.
        
        Returns: invalidation reason if triggered, None otherwise
        """
        # Schema change always invalidates
        if new_schema_hash != artifact_def.schema_hash:
            return f"Schema hash changed: {artifact_def.schema_hash} → {new_schema_hash}"
        
        # Version downgrade invalidates
        if not CompatibilityResolver.is_compatible(new_version, artifact_def.version):
            return f"Version incompatible: {new_version} < {artifact_def.version}"
        
        # Producer code change invalidates deterministic artifacts
        if artifact_def.deterministic and previous_code_hash:
            if producer_code_hash != previous_code_hash:
                return "Producer code changed for deterministic artifact"
        
        return None


# ============================================================================
# LINEAGE TRACKER (AUDIT CORE)
# ============================================================================

class LineageTracker:
    """
    Tracks full upstream chain per artifact.
    Enables reproducible replay and audit trails.
    """
    
    def __init__(self):
        self.lineage_graph: Dict[str, Set[str]] = {}  # artifact_id → upstream_ids
        self.provenance_log: List[Dict] = []
    
    def record_production(
        self,
        artifact_id: str,
        producer_node: str,
        inputs: List[str],
        timestamp: datetime,
        metadata: Dict
    ):
        """Record artifact production event"""
        self.lineage_graph[artifact_id] = set(inputs)
        
        self.provenance_log.append({
            'artifact_id': artifact_id,
            'producer_node': producer_node,
            'inputs': inputs,
            'timestamp': timestamp.isoformat(),
            'metadata': metadata
        })
    
    def get_full_lineage(self, artifact_id: str) -> Set[str]:
        """Get complete upstream dependency tree"""
        lineage = set()
        to_visit = [artifact_id]
        
        while to_visit:
            current = to_visit.pop()
            if current in lineage:
                continue
            
            lineage.add(current)
            
            if current in self.lineage_graph:
                to_visit.extend(self.lineage_graph[current])
        
        lineage.discard(artifact_id)  # Remove self
        return lineage
    
    def get_replay_graph(self, artifact_id: str) -> Dict[str, List[str]]:
        """
        Get dependency graph needed to reproduce artifact.
        Returns: {artifact_id: [upstream_artifact_ids]}
        """
        lineage = self.get_full_lineage(artifact_id)
        lineage.add(artifact_id)
        
        replay_graph = {}
        for aid in lineage:
            if aid in self.lineage_graph:
                replay_graph[aid] = list(self.lineage_graph[aid])
            else:
                replay_graph[aid] = []
        
        return replay_graph
    
    def explain_artifact(self, artifact_id: str) -> str:
        """Human-readable explanation of artifact production"""
        if artifact_id not in self.lineage_graph:
            return f"No lineage recorded for {artifact_id}"
        
        events = [e for e in self.provenance_log if e['artifact_id'] == artifact_id]
        if not events:
            return f"No provenance events for {artifact_id}"
        
        event = events[-1]  # Most recent
        inputs = event['inputs']
        full_lineage = self.get_full_lineage(artifact_id)
        
        explanation = [
            f"Artifact: {artifact_id}",
            f"Produced by: {event['producer_node']}",
            f"Timestamp: {event['timestamp']}",
            f"Direct inputs ({len(inputs)}): {', '.join(inputs) if inputs else 'none'}",
            f"Total upstream dependencies: {len(full_lineage)}",
            f"Upstream chain: {' → '.join(list(full_lineage)[:5])}{' ...' if len(full_lineage) > 5 else ''}"
        ]
        
        if event.get('metadata'):
            explanation.append(f"Metadata: {json.dumps(event['metadata'], indent=2)}")
        
        return '\n'.join(explanation)


# ============================================================================
# DEPENDENCY GRAPH (CORE ENGINE)
# ============================================================================

class DependencyGraph:
    """
    Single source of truth for artifact causality.
    Answers: What was produced by what? What depends on what?
    
    This is the artifact-level dependency control system, separate from execution_graph.py
    which handles WHEN things run. This handles WHAT depends on WHAT at the data level.
    """
    
    def __init__(self, enable_caching: bool = True):
        self.artifacts: Dict[str, ArtifactDefinition] = {}
        self.dependencies: List[ArtifactDependency] = []
        self.artifact_versions: Dict[str, ArtifactVersion] = {}
        
        self.lineage_tracker = LineageTracker()
        self.invalidation_engine = InvalidationEngine()
        self.completeness_checker = CompletenessChecker()
        
        # Optimized indices for fast lookups
        self._dependency_index: Dict[str, Set[str]] = {}  # artifact_id → consumer_nodes
        self._producer_index: Dict[str, str] = {}  # artifact_id → producer_node
        self._consumer_index: Dict[str, List[ArtifactDependency]] = defaultdict(list)  # consumer_node → deps
        self._version_requirements: Dict[str, List[str]] = defaultdict(list)  # artifact_id → [min_versions]
        
        # Caching for expensive operations
        self._enable_caching = enable_caching
        self._validation_cache: Optional[Tuple[datetime, Tuple[List[str], List[str]]]] = None
        self._last_validation_time: Optional[datetime] = None
        
        # Statistics
        self._stats = {
            'artifacts_registered': 0,
            'dependencies_registered': 0,
            'invalidations': 0,
            'validations_run': 0
        }
    
    # ========================================================================
    # REGISTRATION
    # ========================================================================
    
    def register_artifact(
        self,
        artifact_def: ArtifactDefinition,
        execution_graph_nodes: Set[str],
        phase_map: Dict[str, ExecutionPhase]
    ):
        """
        Register an artifact definition.
        
        Fails if:
        - duplicate artifact_id
        - unknown producer node
        - phase mismatch with producer
        - non-deterministic artifact marked cacheable
        - RL-unsafe artifact upstream of reward
        """
        # Check duplicate
        if artifact_def.artifact_id in self.artifacts:
            raise ValueError(
                f"Duplicate artifact_id: {artifact_def.artifact_id}"
            )
        
        # Check producer exists
        if artifact_def.producer_node not in execution_graph_nodes:
            raise ValueError(
                f"Unknown producer node: {artifact_def.producer_node} "
                f"for artifact {artifact_def.artifact_id}"
            )
        
        # Check phase match
        producer_phase = phase_map.get(artifact_def.producer_node)
        if producer_phase and producer_phase != artifact_def.phase:
            raise ValueError(
                f"Phase mismatch: artifact {artifact_def.artifact_id} "
                f"declares {artifact_def.phase.value}, "
                f"but producer is in {producer_phase.value}"
            )
        
        # Store artifact
        self.artifacts[artifact_def.artifact_id] = artifact_def
        self._producer_index[artifact_def.artifact_id] = artifact_def.producer_node
        
        # Initialize dependency tracking
        self._dependency_index[artifact_def.artifact_id] = set()
        
        # Update statistics
        self._stats['artifacts_registered'] += 1
        
        # Invalidate validation cache
        self._validation_cache = None
        
        logger.debug(f"Registered artifact: {artifact_def.artifact_id} from {artifact_def.producer_node}")
    
    def register_dependency(
        self,
        dependency: ArtifactDependency
    ):
        """
        Register a consumer → artifact dependency.
        
        Fails if:
        - artifact does not exist
        - consumer references future phase artifact
        - partial consumption violates artifact contract
        - version constraints are ambiguous
        """
        # Check artifact exists
        if dependency.artifact_id not in self.artifacts:
            raise ValueError(
                f"Cannot register dependency on unknown artifact: "
                f"{dependency.artifact_id}"
            )
        
        artifact_def = self.artifacts[dependency.artifact_id]
        
        # Check partial consumption contract
        if dependency.allow_partial and not artifact_def.partial_allowed:
            raise ValueError(
                f"Consumer {dependency.consumer_node} requests partial "
                f"consumption of {dependency.artifact_id}, "
                f"but artifact does not allow partial use"
            )
        
        # Validate version format
        if not dependency.min_version or '.' not in dependency.min_version:
            raise ValueError(
                f"Ambiguous version constraint: {dependency.min_version}"
            )
        
        # Store dependency
        self.dependencies.append(dependency)
        self._dependency_index[dependency.artifact_id].add(dependency.consumer_node)
        self._consumer_index[dependency.consumer_node].append(dependency)
        self._version_requirements[dependency.artifact_id].append(dependency.min_version)
        
        # Update statistics
        self._stats['dependencies_registered'] += 1
        
        # Invalidate validation cache
        self._validation_cache = None
        
        logger.debug(
            f"Registered dependency: {dependency.consumer_node} → {dependency.artifact_id} "
            f"(min_version={dependency.min_version})"
        )
    
    # ========================================================================
    # VALIDATION
    # ========================================================================
    
    def validate(
        self, 
        phase_map: Dict[str, ExecutionPhase],
        execution_graph_nodes: Optional[Dict[str, Any]] = None,
        strict_orphan_check: bool = False,
        use_cache: bool = True
    ) -> Tuple[List[str], List[str]]:
        """
        Validate entire dependency graph.
        
        Checks:
        - every required artifact has ≥1 producer
        - no artifact consumed without declaration
        - no version conflicts
        - no forward-in-time dependency
        - no hidden artifact reuse
        - no orphaned artifacts
        - no RL contamination paths
        
        Args:
            phase_map: Map of node_id → ExecutionPhase
            execution_graph_nodes: Optional dict of ExecutionNode objects for cross-validation
            strict_orphan_check: If True, orphaned artifacts are errors, not warnings
        
        Returns: (errors, warnings) - List of validation errors and warnings
        """
        # Check cache if enabled
        if (use_cache and self._enable_caching and 
            self._validation_cache is not None and
            self._last_validation_time and
            (datetime.utcnow() - self._last_validation_time).total_seconds() < 60):
            # Return cached result if recent
            return self._validation_cache
        
        self._stats['validations_run'] += 1
        
        errors = []
        warnings = []
        
        # Check required artifacts have producers
        for artifact_id, artifact_def in self.artifacts.items():
            if artifact_def.required:
                if artifact_id not in self._producer_index:
                    errors.append(
                        f"Required artifact {artifact_id} has no producer"
                    )
        
        # Check no forward-in-time dependencies
        phase_order = list(ExecutionPhase)
        for dep in self.dependencies:
            artifact_def = self.artifacts[dep.artifact_id]
            consumer_phase = phase_map.get(dep.consumer_node)
            
            if consumer_phase:
                artifact_phase_idx = phase_order.index(artifact_def.phase)
                consumer_phase_idx = phase_order.index(consumer_phase)
                
                if artifact_phase_idx > consumer_phase_idx:
                    errors.append(
                        f"Forward-in-time dependency: {dep.consumer_node} "
                        f"(phase {consumer_phase.value}) depends on "
                        f"{dep.artifact_id} (phase {artifact_def.phase.value})"
                    )
        
        # Check version conflicts (NEW)
        conflicts = CompatibilityResolver.find_version_conflicts(self._version_requirements)
        for artifact_id, conflicting_versions in conflicts:
            errors.append(
                f"Version conflict for {artifact_id}: incompatible requirements "
                f"{conflicting_versions}"
            )
        
        # Check hidden artifact reuse (NEW)
        if execution_graph_nodes:
            hidden_reuse = self._detect_hidden_reuse(execution_graph_nodes)
            for node_id, artifact_ids in hidden_reuse.items():
                errors.append(
                    f"Hidden artifact reuse: node {node_id} references artifacts "
                    f"{artifact_ids} without declared dependencies"
                )
        
        # Check undeclared consumption (ENHANCED)
        if execution_graph_nodes:
            undeclared = self._detect_undeclared_consumption(execution_graph_nodes)
            for node_id, artifact_ids in undeclared.items():
                errors.append(
                    f"Undeclared consumption: node {node_id} consumes artifacts "
                    f"{artifact_ids} without dependency declarations"
                )
        
        # Check RL contamination (artifacts upstream of reward must be RL-safe)
        reward_artifacts = {
            aid for aid, adef in self.artifacts.items()
            if 'reward' in adef.description.lower() or 
               'rl' in adef.observability_tags.get('type', '').lower() or
               adef.observability_tags.get('rl_safe') == 'false'
        }
        
        for reward_artifact_id in reward_artifacts:
            upstream = self.lineage_tracker.get_full_lineage(reward_artifact_id)
            for upstream_id in upstream:
                if upstream_id in self.artifacts:
                    upstream_def = self.artifacts[upstream_id]
                    if not upstream_def.rl_safe:
                        errors.append(
                            f"RL contamination: {upstream_id} (rl_safe=False) "
                            f"is upstream of reward artifact {reward_artifact_id}"
                        )
        
        # Check for orphaned artifacts (ENHANCED)
        consumed_artifacts = {dep.artifact_id for dep in self.dependencies}
        orphaned = []
        for artifact_id, artifact_def in self.artifacts.items():
            if artifact_id not in consumed_artifacts and not artifact_def.required:
                orphaned.append(artifact_id)
        
        if orphaned:
            msg = f"Orphaned artifacts (produced but never consumed): {orphaned}"
            if strict_orphan_check:
                errors.append(msg)
            else:
                warnings.append(msg)
                logger.warning(msg)
        
        result = (errors, warnings)
        
        # Cache result
        if use_cache and self._enable_caching:
            self._validation_cache = result
            self._last_validation_time = datetime.utcnow()
        
        if errors:
            logger.error(f"Validation found {len(errors)} errors, {len(warnings)} warnings")
        elif warnings:
            logger.warning(f"Validation found {len(warnings)} warnings (no errors)")
        else:
            logger.info("Validation passed with no errors or warnings")
        
        return result
    
    def _detect_hidden_reuse(
        self, 
        execution_graph_nodes: Dict[str, Any]
    ) -> Dict[str, List[str]]:
        """
        Detect artifacts referenced in execution graph but not in dependency declarations.
        This catches implicit/hidden artifact reuse.
        """
        hidden_reuse = {}
        
        for node_id, node in execution_graph_nodes.items():
            # Get artifacts this node references (from inputs/outputs)
            referenced = set()
            if hasattr(node, 'inputs'):
                referenced.update(node.inputs if isinstance(node.inputs, (set, list)) else [])
            if hasattr(node, 'outputs'):
                referenced.update(node.outputs if isinstance(node.outputs, (set, list)) else [])
            
            # Check which are not declared as dependencies
            declared_artifacts = {
                dep.artifact_id for dep in self._consumer_index.get(node_id, [])
            }
            
            hidden = referenced - declared_artifacts - {node_id}  # Exclude self
            if hidden:
                hidden_reuse[node_id] = list(hidden)
        
        return hidden_reuse
    
    def _detect_undeclared_consumption(
        self,
        execution_graph_nodes: Dict[str, Any]
    ) -> Dict[str, List[str]]:
        """
        Detect artifacts consumed by nodes without dependency declarations.
        """
        undeclared = {}
        
        for node_id, node in execution_graph_nodes.items():
            if hasattr(node, 'inputs'):
                consumed = set(node.inputs if isinstance(node.inputs, (set, list)) else [])
                declared = {
                    dep.artifact_id for dep in self._consumer_index.get(node_id, [])
                }
                
                missing = consumed - declared
                if missing:
                    undeclared[node_id] = list(missing)
        
        return undeclared
    
    # ========================================================================
    # QUERIES
    # ========================================================================
    
    def get_consumers(self, artifact_id: str) -> Set[str]:
        """Get all nodes that consume this artifact (optimized)"""
        return self._dependency_index.get(artifact_id, set()).copy()
    
    def get_all_consumers_recursive(self, artifact_id: str) -> Set[str]:
        """
        Get all nodes that consume this artifact, including indirect consumers
        through downstream artifacts.
        """
        consumers = set()
        to_check = [artifact_id]
        checked = set()
        
        while to_check:
            current = to_check.pop()
            if current in checked:
                continue
            checked.add(current)
            
            # Get direct consumers
            direct_consumers = self._dependency_index.get(current, set())
            consumers.update(direct_consumers)
            
            # For each consumer, check if they produce artifacts that are consumed
            for consumer in direct_consumers:
                # Find artifacts produced by this consumer
                produced = {
                    aid for aid, producer in self._producer_index.items()
                    if producer == consumer
                }
                to_check.extend(produced)
        
        return consumers
    
    def get_producer(self, artifact_id: str) -> Optional[str]:
        """Get the producer node for this artifact (optimized)"""
        return self._producer_index.get(artifact_id)
    
    def get_production_chain(self, artifact_id: str) -> List[str]:
        """
        Get the full production chain from root inputs to this artifact.
        Returns list of artifact_ids in production order.
        """
        chain = []
        visited = set()
        
        def traverse(aid: str):
            if aid in visited:
                return
            visited.add(aid)
            
            # Get inputs (lineage)
            if aid in self.artifact_versions:
                inputs = self.artifact_versions[aid].lineage
                for input_id in inputs:
                    traverse(input_id)
            
            chain.append(aid)
        
        traverse(artifact_id)
        return chain
    
    def get_dependencies(self, consumer_node: str) -> List[ArtifactDependency]:
        """Get all artifacts consumed by this node (optimized with index)"""
        return self._consumer_index.get(consumer_node, []).copy()
    
    def check_artifact_ready(
        self,
        artifact_id: str,
        consumer_node: str,
        consumer_phase: ExecutionPhase
    ) -> Tuple[bool, str]:
        """
        Check if artifact is ready for consumption by specific consumer.
        
        Returns: (is_ready, error_message)
        """
        # Find dependency declaration (optimized lookup)
        dep = next(
            (d for d in self._consumer_index.get(consumer_node, [])
             if d.artifact_id == artifact_id),
            None
        )
        
        if not dep:
            return False, (
                f"No dependency declared for {consumer_node} → {artifact_id}. "
                f"Register dependency with register_dependency() first."
            )
        
        # Check artifact exists
        if artifact_id not in self.artifact_versions:
            if dep.strict:
                return False, (
                    f"Artifact {artifact_id} does not exist (strict dependency). "
                    f"Producer: {self._producer_index.get(artifact_id, 'unknown')}"
                )
            else:
                return True, ""  # Soft dependency, allow missing
        
        version = self.artifact_versions[artifact_id]
        artifact_def = self.artifacts[artifact_id]
        
        # Check version compatibility
        if not CompatibilityResolver.is_compatible(version.version, dep.min_version):
            return False, (
                f"Version incompatible: artifact {artifact_id} has version {version.version}, "
                f"but {consumer_node} requires minimum {dep.min_version}"
            )
        
        # Check completeness
        is_complete, error = self.completeness_checker.check_completeness(
            version.state,
            version.completion_percentage,
            dep.allow_partial,
            consumer_phase
        )
        
        if not is_complete:
            return False, (
                f"Artifact {artifact_id} not ready: {error}. "
                f"State: {version.state.value}, "
                f"Completion: {version.completion_percentage:.1%}"
            )
        
        return True, ""
    
    # ========================================================================
    # INVALIDATION
    # ========================================================================
    
    def invalidate(
        self,
        artifact_id: str,
        reason: str
    ) -> Set[str]:
        """
        Invalidate artifact and cascade downstream.
        
        Returns: Set of all invalidated artifact_ids
        """
        # Invalidate validation cache
        self._validation_cache = None
        self._stats['invalidations'] += 1
        
        logger.info(f"Invalidating artifact {artifact_id}: {reason}")
        
        invalidated = self.invalidation_engine.invalidate_artifact(
            artifact_id,
            reason,
            datetime.utcnow(),
            self
        )
        
        if len(invalidated) > 1:
            logger.info(f"Invalidation cascade affected {len(invalidated)} artifacts: {invalidated}")
        
        return invalidated
    
    # ========================================================================
    # LINEAGE
    # ========================================================================
    
    def record_artifact_production(
        self,
        artifact_id: str,
        inputs: List[str],
        content_hash: str,
        completion_percentage: float = 1.0,
        metadata: Optional[Dict] = None,
        producer_code_hash: Optional[str] = None
    ):
        """
        Record production of an artifact.
        
        For deterministic artifacts, automatically verifies hash if producer_code_hash provided.
        """
        if artifact_id not in self.artifacts:
            raise ValueError(f"Unknown artifact: {artifact_id}")
        
        artifact_def = self.artifacts[artifact_id]
        
        # Compute input hashes for deterministic verification
        input_hashes = []
        for input_id in inputs:
            if input_id in self.artifact_versions:
                input_version = self.artifact_versions[input_id]
                if input_version.content_hash:
                    input_hashes.append(input_version.content_hash)
                else:
                    logger.warning(
                        f"Input artifact {input_id} has no content_hash, "
                        f"cannot verify deterministic hash for {artifact_id}"
                    )
        
        # Verify deterministic hash if applicable
        if artifact_def.deterministic and producer_code_hash:
            expected_hash = CompatibilityResolver.compute_expected_hash(
                input_hashes,
                producer_code_hash,
                artifact_def.schema_hash,
                artifact_id
            )
            
            if content_hash != expected_hash:
                error_msg = (
                    f"Deterministic hash mismatch for {artifact_id}: "
                    f"expected {expected_hash[:16]}..., got {content_hash[:16]}..."
                )
                logger.error(error_msg)
                raise ValueError(error_msg)
        
        # Create version
        version = ArtifactVersion(
            artifact_id=artifact_id,
            version=artifact_def.version,
            schema_hash=artifact_def.schema_hash,
            created_at=datetime.utcnow(),
            producer_node=artifact_def.producer_node,
            state=ArtifactState.COMPLETE if completion_percentage >= 1.0 else ArtifactState.PARTIAL,
            completion_percentage=completion_percentage,
            content_hash=content_hash,
            lineage=inputs,
            producer_code_hash=producer_code_hash,
            input_hashes=input_hashes
        )
        
        self.artifact_versions[artifact_id] = version
        
        # Record lineage
        self.lineage_tracker.record_production(
            artifact_id,
            artifact_def.producer_node,
            inputs,
            datetime.utcnow(),
            metadata or {}
        )
    
    def verify_deterministic_hash(
        self,
        artifact_id: str,
        producer_code_hash: str
    ) -> Tuple[bool, str]:
        """
        Verify that a deterministic artifact's content_hash matches expected value.
        
        Returns: (is_valid, error_message)
        """
        if artifact_id not in self.artifacts:
            return False, f"Unknown artifact: {artifact_id}"
        
        artifact_def = self.artifacts[artifact_id]
        
        if not artifact_def.deterministic:
            return True, ""  # Not deterministic, no verification needed
        
        if artifact_id not in self.artifact_versions:
            return False, f"Artifact {artifact_id} has no version recorded"
        
        version = self.artifact_versions[artifact_id]
        
        if not version.producer_code_hash:
            # Try to verify with provided code hash
            if not version.input_hashes:
                return False, f"Cannot verify: missing input hashes for {artifact_id}"
            
            expected_hash = CompatibilityResolver.compute_expected_hash(
                version.input_hashes,
                producer_code_hash,
                artifact_def.schema_hash,
                artifact_id
            )
        else:
            # Use stored code hash
            if producer_code_hash != version.producer_code_hash:
                return False, (
                    f"Producer code hash mismatch: expected {version.producer_code_hash[:16]}..., "
                    f"got {producer_code_hash[:16]}..."
                )
            
            expected_hash = CompatibilityResolver.compute_expected_hash(
                version.input_hashes,
                version.producer_code_hash,
                artifact_def.schema_hash,
                artifact_id
            )
        
        if not version.content_hash:
            return False, f"Artifact {artifact_id} has no content_hash"
        
        if version.content_hash != expected_hash:
            return False, (
                f"Content hash mismatch: expected {expected_hash[:16]}..., "
                f"got {version.content_hash[:16]}..."
            )
        
        return True, ""
    
    def get_lineage(self, artifact_id: str) -> Set[str]:
        """Get full upstream lineage"""
        return self.lineage_tracker.get_full_lineage(artifact_id)
    
    def explain_artifact(self, artifact_id: str) -> str:
        """Get human-readable explanation of artifact"""
        return self.lineage_tracker.explain_artifact(artifact_id)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about dependency graph operations"""
        return {
            **self._stats,
            'total_artifacts': len(self.artifacts),
            'total_dependencies': len(self.dependencies),
            'total_versions': len(self.artifact_versions),
            'cache_enabled': self._enable_caching,
            'last_validation': self._last_validation_time.isoformat() if self._last_validation_time else None
        }
    
    def get_artifact_summary(self, artifact_id: str) -> Dict[str, Any]:
        """Get comprehensive summary of an artifact"""
        if artifact_id not in self.artifacts:
            raise ValueError(f"Unknown artifact: {artifact_id}")
        
        artifact_def = self.artifacts[artifact_id]
        version = self.artifact_versions.get(artifact_id)
        consumers = self.get_consumers(artifact_id)
        producer = self.get_producer(artifact_id)
        lineage = self.get_lineage(artifact_id)
        
        return {
            'artifact_id': artifact_id,
            'definition': {
                'description': artifact_def.description,
                'phase': artifact_def.phase.value,
                'version': artifact_def.version,
                'schema_hash': artifact_def.schema_hash,
                'deterministic': artifact_def.deterministic,
                'cacheable': artifact_def.cacheable,
                'rl_safe': artifact_def.rl_safe,
                'required': artifact_def.required,
                'partial_allowed': artifact_def.partial_allowed
            },
            'producer': producer,
            'consumers': list(consumers),
            'version_info': {
                'state': version.state.value if version else 'MISSING',
                'completion_percentage': version.completion_percentage if version else 0.0,
                'created_at': version.created_at.isoformat() if version else None,
                'content_hash': version.content_hash[:16] + '...' if version and version.content_hash else None
            },
            'lineage': {
                'direct_inputs': version.lineage if version else [],
                'full_upstream': list(lineage),
                'total_upstream_count': len(lineage)
            },
            'dependencies': [
                {
                    'consumer': dep.consumer_node,
                    'min_version': dep.min_version,
                    'allow_partial': dep.allow_partial,
                    'strict': dep.strict
                }
                for dep in self.dependencies if dep.artifact_id == artifact_id
            ]
        }


# ============================================================================
# DEPENDENCY WATCHDOG (PRODUCTION)
# ============================================================================

class DependencyWatchdog:
    """
    Runtime monitoring for dependency violations.
    Detects and escalates violations immediately.
    """
    
    def __init__(self, dependency_graph: DependencyGraph):
        self.graph = dependency_graph
        self.violations: List[Dict] = []
    
    def check_artifact_access(
        self,
        consumer_node: str,
        artifact_id: str,
        consumer_phase: ExecutionPhase
    ) -> Optional[str]:
        """
        Check if artifact access is valid.
        Returns violation reason if invalid, None if valid.
        """
        # Check registration
        deps = [d for d in self.graph.dependencies 
                if d.consumer_node == consumer_node and d.artifact_id == artifact_id]
        
        if not deps:
            violation = (
                f"VIOLATION: {consumer_node} accessing {artifact_id} "
                f"without registration"
            )
            self._record_violation(violation, consumer_node, artifact_id)
            return violation
        
        # Check readiness
        is_ready, error = self.graph.check_artifact_ready(
            artifact_id,
            consumer_node,
            consumer_phase
        )
        
        if not is_ready:
            violation = (
                f"VIOLATION: {consumer_node} accessing non-ready {artifact_id}: "
                f"{error}"
            )
            self._record_violation(violation, consumer_node, artifact_id)
            return violation
        
        return None
    
    def check_state_mismatch(
        self,
        artifact_id: str,
        expected_state: ArtifactState,
        actual_state: ArtifactState
    ) -> Optional[str]:
        """Check for state mismatches"""
        if expected_state != actual_state:
            violation = (
                f"VIOLATION: Artifact {artifact_id} state mismatch: "
                f"expected {expected_state.value}, got {actual_state.value}"
            )
            self._record_violation(violation, None, artifact_id)
            return violation
        
        return None
    
    def _record_violation(
        self,
        violation: str,
        consumer_node: Optional[str],
        artifact_id: str
    ):
        """Record violation for audit"""
        self.violations.append({
            'timestamp': datetime.utcnow().isoformat(),
            'violation': violation,
            'consumer_node': consumer_node,
            'artifact_id': artifact_id
        })
    
    def get_violations(self) -> List[Dict]:
        """Get all recorded violations"""
        return self.violations.copy()
    
    def halt_on_violation(self) -> bool:
        """Check if workflow should halt due to violations"""
        return len(self.violations) > 0
    
    def get_violation_summary(self) -> Dict[str, Any]:
        """Get summary of violations for reporting"""
        if not self.violations:
            return {"total": 0, "by_type": {}, "recent": []}
        
        by_type = defaultdict(int)
        for violation in self.violations:
            violation_type = violation['violation'].split(':')[0] if ':' in violation['violation'] else 'UNKNOWN'
            by_type[violation_type] += 1
        
        return {
            "total": len(self.violations),
            "by_type": dict(by_type),
            "recent": self.violations[-10:]  # Last 10 violations
        }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Initialize
    graph = DependencyGraph()
    
    # Mock execution graph nodes
    nodes = {
        "ingest_views", "extract_metadata", "compute_features",
        "train_model", "evaluate_model"
    }
    
    phase_map = {
        "ingest_views": ExecutionPhase.INGESTION,
        "extract_metadata": ExecutionPhase.EXTRACTION,
        "compute_features": ExecutionPhase.FEATURE_ENGINEERING,
        "train_model": ExecutionPhase.TRAINING,
        "evaluate_model": ExecutionPhase.EVALUATION
    }
    
    # Register artifacts
    view_data = ArtifactDefinition(
        artifact_id="raw_view_data",
        description="Raw view events from ingestion",
        producer_node="ingest_views",
        phase=ExecutionPhase.INGESTION,
        schema_hash="abc123",
        version="1.0.0",
        required=True,
        partial_allowed=False,
        deterministic=True,
        cacheable=True,
        rl_safe=True,
        observability_tags={"source": "kafka"}
    )
    
    graph.register_artifact(view_data, nodes, phase_map)
    
    metadata = ArtifactDefinition(
        artifact_id="video_metadata",
        description="Extracted video metadata",
        producer_node="extract_metadata",
        phase=ExecutionPhase.EXTRACTION,
        schema_hash="def456",
        version="2.1.0",
        required=True,
        partial_allowed=True,
        deterministic=True,
        cacheable=True,
        rl_safe=True,
        observability_tags={"extractor": "v2"}
    )
    
    graph.register_artifact(metadata, nodes, phase_map)
    
    # Register dependencies
    graph.register_dependency(ArtifactDependency(
        consumer_node="extract_metadata",
        artifact_id="raw_view_data",
        min_version="1.0.0",
        allow_partial=False,
        strict=True
    ))
    
    graph.register_dependency(ArtifactDependency(
        consumer_node="compute_features",
        artifact_id="video_metadata",
        min_version="2.0.0",
        allow_partial=True,
        strict=True
    ))
    
    # Validate
    errors, warnings = graph.validate(phase_map)
    print(f"Validation errors: {errors}")
    print(f"Validation warnings: {warnings}")
    
    # Simulate artifact production
    graph.record_artifact_production(
        artifact_id="raw_view_data",
        inputs=[],
        content_hash="hash_abc",
        completion_percentage=1.0,
        producer_code_hash="code_hash_123"
    )
    
    graph.record_artifact_production(
        artifact_id="video_metadata",
        inputs=["raw_view_data"],
        content_hash="hash_def",
        completion_percentage=1.0,
        producer_code_hash="code_hash_456"
    )
    
    # Test deterministic hash verification
    is_valid, msg = graph.verify_deterministic_hash(
        "raw_view_data",
        "code_hash_123"
    )
    print(f"\nDeterministic hash verification: {is_valid}, {msg}")
    
    # Check readiness
    ready, msg = graph.check_artifact_ready(
        "video_metadata",
        "compute_features",
        ExecutionPhase.FEATURE_ENGINEERING
    )
    print(f"Metadata ready: {ready}, {msg}")
    
    # Get lineage
    print(f"\nLineage: {graph.explain_artifact('video_metadata')}")
    
    # Test invalidation
    invalidated = graph.invalidate("raw_view_data", "Schema change detected")
    print(f"\nInvalidated artifacts: {invalidated}")
    
    # Watchdog
    watchdog = DependencyWatchdog(graph)
    violation = watchdog.check_artifact_access(
        "compute_features",
        "video_metadata",
        ExecutionPhase.FEATURE_ENGINEERING
    )
    print(f"\nWatchdog violation: {violation}")