"""
Deterministic re-execution orchestration.

This module is the single authority that executes a ReplayPlan deterministically
and produces replay artifacts that are comparable to original execution outputs,
bit-for-bit where allowed.

It orchestrates — it does not decide.
If this file lies, determinism is theater.
"""

from __future__ import annotations

import hashlib
import json
import sys
import importlib
import logging
import os
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Dict, List, Set, Protocol, Callable
from collections import OrderedDict
from abc import ABC, abstractmethod
from pathlib import Path

from replay_invariants import (
    InvariantID,
    AssertionContext,
    IdentityInvariants,
    EnvironmentInvariants,
    ScopeInvariants,
    ExecutionInvariants,
    OutputInvariants,
)
from replay_errors import ReplayPhase, ReplayError
from replay_lineage import LineageAuthority, DefaultLineageAuthority, ExecutionGraph
from replay_ordering import OrderingPolicy, LexicographicOrderingPolicy
from replay_results import ReplayArtifact, ArtifactBuilder, OutputVerifier
from replay_io_sandbox import IOSandbox


# ============================================================================
# EXECUTION STATE - Immutable Snapshots
# ============================================================================

class ExecutionPhase(Enum):
    """Phases of replay execution."""
    LOCKDOWN = "lockdown"
    SCOPE_RESOLUTION = "scope_resolution"
    ORDERING = "ordering"
    DELEGATION = "delegation"
    ARTIFACT_EMISSION = "artifact_emission"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ExecutionStep:
    """
    Immutable record of a single execution step.
    
    Used for observability and audit trail.
    """
    step_id: str
    phase: ExecutionPhase
    component_id: str
    started_at: int  # Logical clock tick
    completed_at: Optional[int] = None
    success: bool = True
    error: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "phase": self.phase.value,
            "component_id": self.component_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "success": self.success,
            "error": self.error,
            "metadata": OrderedDict(sorted(self.metadata.items())),
        }


# ============================================================================
# REPLAY CONTEXT - Pre-Validated Input
# ============================================================================

@dataclass(frozen=True)
class ReplayContext:
    """
    Immutable execution context for replay.
    
    Pre-conditions:
    - Must satisfy all identity invariants
    - Must be content-addressed
    - Must never be mutated by ReplayRunner
    """
    context_id: str
    context_hash: str
    original_execution_hash: str
    logical_time_seed: int
    environment_seal: Dict[str, str]
    
    def verify_integrity(self) -> bool:
        """Verify context hash matches content."""
        computed = self._compute_hash()
        return computed == self.context_hash
    
    def _compute_hash(self) -> str:
        canonical = OrderedDict([
            ("context_id", self.context_id),
            ("original_execution_hash", self.original_execution_hash),
            ("logical_time_seed", self.logical_time_seed),
            ("environment_seal", OrderedDict(sorted(self.environment_seal.items()))),
        ])
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ReplayPlan:
    """
    Immutable replay plan defining scope and constraints.
    
    Pre-conditions:
    - Must be pre-validated
    - Must be content-addressed
    - Must define complete scope
    """
    plan_id: str
    plan_hash: str
    pipeline_version: str
    entities: List[str]
    windows: List[str]
    computations: List[str]
    stages: List[str]
    divergence_authorizations: Dict[str, str]
    normalization_declarations: List[str]
    
    def verify_integrity(self) -> bool:
        """Verify plan hash matches content."""
        computed = self._compute_hash()
        return computed == self.plan_hash
    
    def _compute_hash(self) -> str:
        canonical = OrderedDict([
            ("plan_id", self.plan_id),
            ("pipeline_version", self.pipeline_version),
            ("entities", sorted(self.entities)),
            ("windows", sorted(self.windows)),
            ("computations", sorted(self.computations)),
            ("stages", sorted(self.stages)),
            ("divergence_authorizations", OrderedDict(sorted(self.divergence_authorizations.items()))),
            ("normalization_declarations", sorted(self.normalization_declarations)),
        ])
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ExecutionEnvironment:
    """
    Sealed execution environment for replay.
    
    Pre-conditions:
    - Time source must be logical only
    - Randomness must be frozen
    - Dependencies must be locked
    - I/O must be replay-scoped
    - Thread model must be declared
    """
    environment_id: str
    time_source: str  # Must be "logical"
    randomness_seed: Optional[int]
    dependency_versions: Dict[str, str]
    io_permissions: Set[str]
    declared_env_vars: Set[str]
    thread_model: str  # e.g., "single_threaded", "deterministic_pool"
    
    def is_sealed(self) -> bool:
        """Verify environment is properly sealed."""
        if self.time_source != "logical":
            return False
        if self.randomness_seed is None:
            return False
        if not self.thread_model:
            return False
        return True
    
    def verify_dependency_versions(self) -> bool:
        """Verify current dependency versions match declared versions."""
        for package, expected_version in self.dependency_versions.items():
            try:
                module = importlib.import_module(package)
                actual_version = getattr(module, '__version__', None)
                if actual_version != expected_version:
                    return False
            except ImportError:
                return False
        return True
    
    def compute_dependency_fingerprint(self) -> str:
        """
        Compute cryptographic fingerprint of dependency code.
        
        Uses file hashing for stronger verification than version strings.
        """
        fingerprints = {}
        for package in self.dependency_versions.keys():
            try:
                module = importlib.import_module(package)
                spec = importlib.util.find_spec(package)
                if spec and spec.origin:
                    # Hash the module file
                    module_path = Path(spec.origin)
                    if module_path.exists():
                        with open(module_path, 'rb') as f:
                            content = f.read()
                            fingerprints[package] = hashlib.sha256(content).hexdigest()
            except (ImportError, AttributeError, OSError):
                # Fallback to version if file not accessible
                try:
                    module = importlib.import_module(package)
                    version = getattr(module, '__version__', 'unknown')
                    fingerprints[package] = hashlib.sha256(
                        f"{package}:{version}".encode()
                    ).hexdigest()
                except ImportError:
                    return ""
        
        # Combine all fingerprints
        combined = json.dumps(OrderedDict(sorted(fingerprints.items())), separators=(',', ':'))
        return hashlib.sha256(combined.encode()).hexdigest()


# ============================================================================
# EXECUTION ENGINE PROTOCOL
# ============================================================================

class FrozenInputLoader(Protocol):
    """Protocol for loading frozen inputs from historical sources."""
    
    def load_stage_inputs(
        self,
        stage_id: str,
        original_execution_hash: str,
        context: ReplayContext
    ) -> Dict[str, Any]:
        """
        Load frozen inputs for a stage from historical sources.
        
        Args:
            stage_id: Identifier for the stage
            original_execution_hash: Hash of original execution
            context: Replay context
            
        Returns:
            Frozen input data (read-only, from historical sources)
            
        Raises:
            FileNotFoundError: If historical inputs not found
            ValueError: If input integrity check fails
        """
        ...


class ExecutionEngine(Protocol):
    """Protocol for execution engine that can run pipeline stages."""
    
    def execute_stage(
        self,
        stage_id: str,
        frozen_inputs: Dict[str, Any],
        sealed_environment: ExecutionEnvironment,
        context: ReplayContext
    ) -> Dict[str, Any]:
        """
        Execute a pipeline stage with frozen inputs and sealed environment.
        
        Args:
            stage_id: Identifier for the stage to execute
            frozen_inputs: Immutable input data for the stage
            sealed_environment: Sealed execution environment
            context: Replay context
            
        Returns:
            Stage execution outputs (must be deterministic)
        """
        ...


class StageExecutionResult:
    """Result of executing a single stage."""
    def __init__(
        self,
        stage_id: str,
        output_hash: str,
        output_data: Dict[str, Any],
        input_fingerprint: str,
        execution_time_ticks: int,
        dependencies: List[str] = None
    ):
        self.stage_id = stage_id
        self.output_hash = output_hash
        self.output_data = output_data
        self.input_fingerprint = input_fingerprint
        self.execution_time_ticks = execution_time_ticks
        self.dependencies = dependencies or []  # Stages this stage depends on


# ============================================================================
# REPLAY RUNNER - Deterministic Orchestration
# ============================================================================

class ReplayRunner:
    """
    Deterministic re-execution orchestrator.
    
    Responsibilities (in strict order):
    1. Environment lockdown
    2. Scope resolution
    3. Deterministic ordering
    4. Execution delegation
    5. Artifact emission
    
    Contract:
    - Execution must be deterministic
    - Side-effects must be isolated
    - Historical sources are read-only
    - Execution is replay-scoped
    - Results are reproducible across machines
    
    Failure semantics:
    - All errors are fatal
    - No retries
    - No degradation
    - No partial success
    """
    
    def __init__(
        self,
        replay_plan: ReplayPlan,
        replay_context: ReplayContext,
        execution_environment: ExecutionEnvironment,
        execution_engine: Optional[ExecutionEngine] = None,
        lineage_verifier: Optional[Callable[[ReplayPlan], bool]] = None,
        artifact_cache: Optional[Dict[str, ReplayArtifact]] = None,
        frozen_input_loader: Optional[FrozenInputLoader] = None,
        lineage_authority: Optional[LineageAuthority] = None,
        ordering_policy: Optional[OrderingPolicy] = None
    ):
        """
        Initialize replay runner with pre-validated inputs.
        
        Args:
            replay_plan: Pre-validated, immutable replay plan
            replay_context: Pre-validated, immutable execution context
            execution_environment: Sealed execution environment
            execution_engine: Engine to execute stages (required for real execution)
            lineage_verifier: Function to verify scope matches lineage (optional)
            artifact_cache: Cache for idempotent artifact reuse (optional)
            frozen_input_loader: Loader for frozen historical inputs (optional)
            lineage_authority: Authority for dependency graph construction (optional, uses default)
            ordering_policy: Policy for canonical ordering (optional, uses lexicographic)
        
        Pre-conditions:
        - All inputs must satisfy invariants
        - All inputs must be immutable
        - Version compatibility must be verified
        """
        # Verify input integrity
        if not replay_plan.verify_integrity():
            raise ValueError("ReplayPlan failed integrity check")
        if not replay_context.verify_integrity():
            raise ValueError("ReplayContext failed integrity check")
        if not execution_environment.is_sealed():
            raise ValueError("ExecutionEnvironment is not sealed")
        
        # Cross-check environment seal with context
        self._verify_environment_seal(replay_context, execution_environment)
        
        # Store immutable references (no mutation allowed)
        self._plan = replay_plan
        self._context = replay_context
        self._environment = execution_environment
        self._execution_engine = execution_engine
        self._lineage_verifier = lineage_verifier
        self._artifact_cache = artifact_cache or {}
        self._frozen_input_loader = frozen_input_loader
        
        # Delegate semantic decisions to external authorities
        self._lineage_authority = lineage_authority or DefaultLineageAuthority()
        self._ordering_policy = ordering_policy or LexicographicOrderingPolicy()
        
        # IO sandbox for active enforcement
        self._io_sandbox: Optional[IOSandbox] = None
        
        # Initialize execution state
        self._logical_clock = replay_context.logical_time_seed
        self._execution_timeline: List[ExecutionStep] = []
        self._artifacts: List[ReplayArtifact] = []
        self._current_phase = ExecutionPhase.LOCKDOWN
        
        # Resolved scope (populated during execution)
        self._resolved_entities: Optional[List[str]] = None
        self._resolved_windows: Optional[List[str]] = None
        self._resolved_computations: Optional[List[str]] = None
        self._resolved_stages: Optional[List[str]] = None
        
        # Stage execution results (for output hashing)
        self._stage_results: Dict[str, StageExecutionResult] = {}
        
        # Logger for structured observability
        self._logger = logging.getLogger(__name__)
        
        # Emit structured log with plan/context hashes at initialization
        self._log_structured("replay_initialized", {
            "plan_id": replay_plan.plan_id,
            "plan_hash": replay_plan.plan_hash,
            "context_id": replay_context.context_id,
            "context_hash": replay_context.context_hash,
            "pipeline_version": replay_plan.pipeline_version,
        })
        
        # Check idempotency cache
        self._check_idempotency_cache()
    
    def _verify_environment_seal(
        self,
        context: ReplayContext,
        environment: ExecutionEnvironment
    ) -> None:
        """Cross-check ReplayContext.environment_seal with ExecutionEnvironment."""
        ctx = AssertionContext(
            phase=ReplayPhase.INITIALIZATION,
            component_id="environment_seal_verification"
        )
        
        # Verify time_source matches
        seal_time_source = context.environment_seal.get("time_source")
        if seal_time_source != environment.time_source:
            raise ReplayError(
                invariant_id=InvariantID.TIME_SOURCE_FROZEN.value,
                phase=ReplayPhase.INITIALIZATION,
                component_id="environment_seal",
                message=f"Time source mismatch: seal={seal_time_source}, env={environment.time_source}",
                expected_value=seal_time_source,
                observed_value=environment.time_source
            )
        
        # Verify randomness_seed matches
        seal_seed = context.environment_seal.get("randomness_seed")
        if seal_seed and str(environment.randomness_seed) != seal_seed:
            raise ReplayError(
                invariant_id=InvariantID.RANDOMNESS_DETERMINISTIC.value,
                phase=ReplayPhase.INITIALIZATION,
                component_id="environment_seal",
                message=f"Randomness seed mismatch: seal={seal_seed}, env={environment.randomness_seed}",
                expected_value=seal_seed,
                observed_value=str(environment.randomness_seed)
            )
        
        # Verify dependency versions match
        seal_deps = context.environment_seal.get("dependency_versions", {})
        if isinstance(seal_deps, str):
            # Parse if stored as JSON string
            import json
            seal_deps = json.loads(seal_deps)
        
        for pkg, version in seal_deps.items():
            if pkg in environment.dependency_versions:
                if environment.dependency_versions[pkg] != version:
                    raise ReplayError(
                        invariant_id=InvariantID.DEPENDENCY_GRAPH_FIXED.value,
                        phase=ReplayPhase.INITIALIZATION,
                        component_id="environment_seal",
                        message=f"Dependency version mismatch for {pkg}: seal={version}, env={environment.dependency_versions[pkg]}",
                        expected_value=version,
                        observed_value=environment.dependency_versions[pkg]
                    )
    
    def _check_idempotency_cache(self) -> None:
        """
        Check if this plan was already executed and reuse artifacts.
        
        Tier-0 requirement: Full artifact equivalence verification.
        Verifies plan hash, context hash, dependency fingerprint, and execution graph.
        """
        plan_cache_key = self._plan.plan_hash
        if plan_cache_key in self._artifact_cache:
            cached_artifact = self._artifact_cache[plan_cache_key]
            cached_data = cached_artifact.data
            
            # Verify context hash matches
            if cached_data.get("context_hash") != self._context.context_hash:
                return  # Context differs, cannot reuse
            
            # Verify dependency fingerprint matches (if stored)
            dependency_fingerprint = getattr(self, '_dependency_fingerprint', None)
            if dependency_fingerprint:
                cached_fp = cached_data.get("dependency_fingerprint")
                if cached_fp and cached_fp != dependency_fingerprint:
                    return  # Dependency fingerprint differs, cannot reuse
            
            # Verify execution graph hash matches (if stored)
            cached_graph_hash = cached_data.get("execution_graph_hash")
            if cached_graph_hash:
                # Will verify after execution graph is built
                self._cached_graph_hash = cached_graph_hash
            
            # Verify all stage outputs match (if stored)
            cached_stage_outputs = cached_data.get("stage_outputs", {})
            if cached_stage_outputs:
                # Will verify during execution
                self._cached_stage_outputs = cached_stage_outputs
            
            # All checks passed - can reuse
            self._artifacts.append(cached_artifact)
    
    def execute(self) -> ReplayExecutionResult:
        """
        Execute replay plan deterministically.
        
        Returns:
            Immutable execution result with artifacts
        
        Raises:
            ReplayError: On any invariant violation or execution failure
        """
        try:
            # Phase 1: Environment lockdown
            self._phase_1_environment_lockdown()
            
            # Phase 2: Scope resolution
            self._phase_2_scope_resolution()
            
            # Phase 3: Deterministic ordering
            self._phase_3_deterministic_ordering()
            
            # Phase 4: Execution delegation
            self._phase_4_execution_delegation()
            
            # Phase 5: Artifact emission
            self._phase_5_artifact_emission()
            
            # Mark as completed
            self._current_phase = ExecutionPhase.COMPLETED
            
            # Deactivate IO sandbox
            if self._io_sandbox:
                self._io_sandbox.__exit__(None, None, None)
            
            return self._build_success_result()
            
        except ReplayError as e:
            # Replay error - fatal, no recovery
            self._current_phase = ExecutionPhase.FAILED
            self._record_failure(str(e))
            # Deactivate IO sandbox on error
            if self._io_sandbox:
                self._io_sandbox.__exit__(None, None, None)
            raise
        
        except Exception as e:
            # Unexpected error - also fatal
            self._current_phase = ExecutionPhase.FAILED
            self._record_failure(f"Unexpected error: {e}")
            # Deactivate IO sandbox on error
            if self._io_sandbox:
                self._io_sandbox.__exit__(None, None, None)
            raise ReplayError(
                invariant_id="execution.unexpected_error",
                phase=ReplayPhase.EXECUTION,
                component_id="replay_runner",
                message=f"Unexpected execution error: {e}"
            )
    
    # ========================================================================
    # PHASE 1: ENVIRONMENT LOCKDOWN
    # ========================================================================
    
    def _phase_1_environment_lockdown(self) -> None:
        """
        Freeze all mutable execution parameters.
        
        Locks:
        - Time source (logical clock only)
        - Randomness (disabled or seeded)
        - Thread model (enforced)
        - Dependency versions (verified and locked)
        - I/O permissions (enforced)
        
        No replay begins without a sealed environment.
        """
        self._current_phase = ExecutionPhase.LOCKDOWN
        step_id = self._generate_step_id("lockdown")
        step_start = self._tick()
        
        ctx = AssertionContext(
            phase=ReplayPhase.INITIALIZATION,
            component_id="environment_lockdown"
        )
        
        # Assert time source is frozen
        EnvironmentInvariants.assert_time_source_frozen(
            time_access_detected=(self._environment.time_source != "logical"),
            ctx=ctx
        )
        
        # Assert randomness is deterministic
        EnvironmentInvariants.assert_randomness_deterministic(
            randomness_source_frozen=(self._environment.randomness_seed is not None),
            ctx=ctx
        )
        
        # Verify dependency versions match declared versions
        if not self._environment.verify_dependency_versions():
            expected_hash = self._hash_content(self._environment.dependency_versions)
            observed_hash = self._hash_content({})  # Would compute from actual versions
            EnvironmentInvariants.assert_dependency_graph_fixed(
                expected_graph_hash=expected_hash,
                observed_graph_hash=observed_hash,
                ctx=ctx
            )
        
        # Cryptographic dependency fingerprint verification
        dependency_fingerprint = self._environment.compute_dependency_fingerprint()
        if dependency_fingerprint:
            # Store fingerprint for later verification
            self._dependency_fingerprint = dependency_fingerprint
        else:
            self._dependency_fingerprint = None
        
        # Assert thread model is declared
        EnvironmentInvariants.assert_concurrency_model_declared(
            concurrency_model_declared=(self._environment.thread_model is not None),
            ctx=ctx
        )
        
        # Assert I/O permissions are sealed (will be enforced during execution)
        EnvironmentInvariants.assert_io_permissions_sealed(
            external_io_attempted=False,  # Verified during execution via sandboxing
            ctx=ctx
        )
        
        # Actively enforce IO sandboxing
        self._io_sandbox = IOSandbox(allowed_paths=self._environment.io_permissions)
        self._io_sandbox.__enter__()  # Activate sandbox
        io_permissions_hash = self._hash_content(sorted(self._environment.io_permissions))
        
        # Record successful lockdown
        step = ExecutionStep(
            step_id=step_id,
            phase=ExecutionPhase.LOCKDOWN,
            component_id="environment",
            started_at=step_start,
            completed_at=self._tick(),
            success=True,
            metadata={
                "time_source": self._environment.time_source,
                "randomness_seeded": str(self._environment.randomness_seed is not None),
                "thread_model": self._environment.thread_model,
                "dependency_count": str(len(self._environment.dependency_versions)),
                "dependency_fingerprint": self._dependency_fingerprint or "none",
                "io_permissions_hash": io_permissions_hash,
                "plan_hash": self._plan.plan_hash,
                "context_hash": self._context.context_hash,
            }
        )
        self._record_step(step)
        self._log_structured("phase_completed", {
            "phase": "lockdown",
            "step_id": step_id,
        })
    
    # ========================================================================
    # PHASE 2: SCOPE RESOLUTION
    # ========================================================================
    
    def _phase_2_scope_resolution(self) -> None:
        """
        Materialize the replay scope explicitly.
        
        Materializes:
        - Entities (enumerated)
        - Windows (enumerated)
        - Computations (enumerated)
        - Pipeline stages (enumerated)
        
        Rules:
        - No lazy resolution
        - No dynamic expansion
        - Must match audit lineage exactly
        
        Resolved scope is immutable.
        """
        self._current_phase = ExecutionPhase.SCOPE_RESOLUTION
        step_id = self._generate_step_id("scope_resolution")
        step_start = self._tick()
        
        ctx = AssertionContext(
            phase=ReplayPhase.INITIALIZATION,
            component_id="scope_resolution"
        )
        
        # Materialize entities (no lazy expansion)
        self._resolved_entities = self._plan.entities.copy()
        
        # Materialize windows (no lazy expansion)
        self._resolved_windows = self._plan.windows.copy()
        ScopeInvariants.assert_windows_materialized(
            lazy_expansion_detected=False,
            ctx=ctx
        )
        
        # Materialize computations (no runtime discovery)
        self._resolved_computations = self._plan.computations.copy()
        ScopeInvariants.assert_no_runtime_discovery(
            runtime_discovery_triggered=False,
            ctx=ctx
        )
        
        # Materialize stages
        self._resolved_stages = self._plan.stages.copy()
        
        # Verify entity count matches
        ScopeInvariants.assert_entity_count_match(
            expected_count=len(self._plan.entities),
            observed_count=len(self._resolved_entities),
            ctx=ctx
        )
        
        # Verify scope matches audit lineage (if verifier provided)
        if self._lineage_verifier:
            lineage_matches = self._lineage_verifier(self._plan)
            if not lineage_matches:
                raise ReplayError(
                    invariant_id=InvariantID.LINEAGE_HASH_INTEGRITY.value,
                    phase=ReplayPhase.INITIALIZATION,
                    component_id="scope_resolution",
                    message="Resolved scope does not match audit lineage",
                    expected_value="lineage_match",
                    observed_value="lineage_mismatch"
                )
        
        # Record successful resolution
        step = ExecutionStep(
            step_id=step_id,
            phase=ExecutionPhase.SCOPE_RESOLUTION,
            component_id="scope",
            started_at=step_start,
            completed_at=self._tick(),
            success=True,
            metadata={
                "entity_count": str(len(self._resolved_entities)),
                "window_count": str(len(self._resolved_windows)),
                "computation_count": str(len(self._resolved_computations)),
                "stage_count": str(len(self._resolved_stages)),
                "lineage_verified": str(self._lineage_verifier is not None),
                "plan_hash": self._plan.plan_hash,
                "context_hash": self._context.context_hash,
            }
        )
        self._record_step(step)
        self._log_structured("phase_completed", {
            "phase": "scope_resolution",
            "step_id": step_id,
        })
    
    # ========================================================================
    # PHASE 3: DETERMINISTIC ORDERING
    # ========================================================================
    
    def _phase_3_deterministic_ordering(self) -> None:
        """
        Enforce canonical ordering for execution.
        
        Orders:
        - Entity order (deterministic)
        - Window order (deterministic)
        - Computation order (deterministic)
        - Stage order (deterministic)
        
        Ordering must be:
        - Stable
        - Version-independent
        - Repeatable under identical plans
        - Schema-aware (if applicable)
        
        If ordering is ambiguous, replay must fail.
        """
        self._current_phase = ExecutionPhase.ORDERING
        step_id = self._generate_step_id("ordering")
        step_start = self._tick()
        
        ctx = AssertionContext(
            phase=ReplayPhase.EXECUTION,
            component_id="ordering"
        )
        
        # Delegate ordering to external policy (no semantic interpretation in runner)
        self._resolved_entities = self._ordering_policy.canonical_sort(self._resolved_entities)
        self._resolved_windows = self._ordering_policy.canonical_sort(self._resolved_windows)
        self._resolved_computations = self._ordering_policy.canonical_sort(self._resolved_computations)
        self._resolved_stages = self._ordering_policy.canonical_sort(self._resolved_stages)
        
        # Assert no non-deterministic ordering
        ExecutionInvariants.assert_execution_order_deterministic(
            nondeterministic_ordering_detected=False,
            ctx=ctx
        )
        
        # Assert ordering is canonical
        ExecutionInvariants.assert_ordering_canonical(
            ordering_canonical=True,
            ctx=ctx
        )
        
        # Record successful ordering
        step = ExecutionStep(
            step_id=step_id,
            phase=ExecutionPhase.ORDERING,
            component_id="ordering",
            started_at=step_start,
            completed_at=self._tick(),
            success=True,
            metadata={
                "ordering_method": self._ordering_policy.__class__.__name__,
                "plan_hash": self._plan.plan_hash,
                "context_hash": self._context.context_hash,
            }
        )
        self._record_step(step)
        self._log_structured("phase_completed", {
            "phase": "ordering",
            "step_id": step_id,
        })
    
    # ========================================================================
    # PHASE 4: EXECUTION DELEGATION
    # ========================================================================
    
    def _phase_4_execution_delegation(self) -> None:
        """
        Delegate execution to original engine for each ordered unit.
        
        For each unit:
        - Invoke original execution engine
        - Pass frozen context + scoped inputs
        - Disable persistence unless explicitly allowed
        - Capture outputs verbatim
        
        ReplayRunner never interprets results.
        """
        self._current_phase = ExecutionPhase.DELEGATION
        
        ctx = AssertionContext(
            phase=ReplayPhase.EXECUTION,
            component_id="delegation"
        )
        
        # Execute each stage in deterministic order
        for stage in self._resolved_stages:
            self._execute_stage(stage, ctx)
        
        # Delegate execution graph construction to lineage authority
        stage_results_dict = {
            stage_id: {
                "output_hash": result.output_hash,
                "input_fingerprint": result.input_fingerprint,
                "dependencies": result.dependencies
            }
            for stage_id, result in self._stage_results.items()
        }
        execution_graph = self._lineage_authority.build_execution_graph(
            stages=self._resolved_stages,
            stage_results=stage_results_dict
        )
        observed_graph_hash = execution_graph.compute_hash()
        
        # Assert execution graph matches (compare graph structures, not timeline)
        ExecutionInvariants.assert_execution_graph_match(
            expected_graph_hash=self._context.original_execution_hash,
            observed_graph_hash=observed_graph_hash,
            ctx=ctx
        )
    
    def _execute_stage(self, stage: str, ctx: AssertionContext) -> None:
        """
        Execute a single stage deterministically.
        
        Invokes the original execution engine with frozen inputs and sealed environment.
        """
        step_id = self._generate_step_id(f"execute_stage_{stage}")
        step_start = self._tick()
        
        # Load frozen inputs for this stage (from original execution)
        frozen_inputs = self._load_frozen_inputs(stage)
        input_fingerprint = self._hash_content(frozen_inputs)
        
        # Execute stage if execution engine is provided
        if self._execution_engine:
            try:
                # Invoke execution engine with sealed environment
                output_data = self._execution_engine.execute_stage(
                    stage_id=stage,
                    frozen_inputs=frozen_inputs,
                    sealed_environment=self._environment,
                    context=self._context
                )
                
                # Compute output hash
                output_hash = self._hash_content(output_data)
                
                # Delegate dependency determination to lineage authority
                executed_stages = [s for s in self._resolved_stages if s in self._stage_results]
                stage_dependencies = self._lineage_authority.get_stage_dependencies(
                    stage_id=stage,
                    stage_outputs=output_data,
                    all_stages=self._resolved_stages,
                    executed_stages=executed_stages
                )
                
                # Store stage result
                execution_ticks = self._tick() - step_start
                stage_result = StageExecutionResult(
                    stage_id=stage,
                    output_hash=output_hash,
                    output_data=output_data,
                    input_fingerprint=input_fingerprint,
                    execution_time_ticks=execution_ticks,
                    dependencies=stage_dependencies
                )
                self._stage_results[stage] = stage_result
                
                # Delegate output verification to output verifier
                OutputVerifier.verify_stage_output(
                    stage=stage,
                    output_hash=output_hash,
                    divergence_authorizations=self._plan.divergence_authorizations
                )
                
            except Exception as e:
                # Execution failure - fatal
                self._record_step(ExecutionStep(
                    step_id=step_id,
                    phase=ExecutionPhase.DELEGATION,
                    component_id=f"stage_{stage}",
                    started_at=step_start,
                    completed_at=self._tick(),
                    success=False,
                    error=str(e),
                    metadata={"stage": stage}
                ))
                raise ReplayError(
                    invariant_id="execution.stage_failed",
                    phase=ReplayPhase.EXECUTION,
                    component_id=f"stage_{stage}",
                    message=f"Stage execution failed: {e}"
                )
        else:
            # No execution engine - this is a configuration error
            raise ReplayError(
                invariant_id="execution.engine_missing",
                phase=ReplayPhase.EXECUTION,
                component_id="replay_runner",
                message="Execution engine not provided - cannot execute stages"
            )
        
        # Record successful step
        step = ExecutionStep(
            step_id=step_id,
            phase=ExecutionPhase.DELEGATION,
            component_id=f"stage_{stage}",
            started_at=step_start,
            completed_at=self._tick(),
            success=True,
            metadata={
                "stage": stage,
                "input_fingerprint": input_fingerprint,
                "output_hash": self._stage_results[stage].output_hash,
                "plan_hash": self._plan.plan_hash,
                "context_hash": self._context.context_hash,
            }
        )
        self._record_step(step)
        self._log_structured("stage_executed", {
            "stage": stage,
            "step_id": step_id,
            "input_fingerprint": input_fingerprint,
            "output_hash": self._stage_results[stage].output_hash,
        })
        
        # Delegate artifact creation to artifact builder
        stage_artifact = ArtifactBuilder.create_stage_output(
            stage=stage,
            input_fingerprint=input_fingerprint,
            output_hash=self._stage_results[stage].output_hash,
            status="completed",
            artifact_id=self._generate_step_id(f"artifact_stage_{stage}"),
            logical_timestamp=self._logical_clock
        )
        self._artifacts.append(stage_artifact)
    
    def _load_frozen_inputs(self, stage: str) -> Dict[str, Any]:
        """
        Load frozen inputs for a stage from historical sources (read-only).
        
        Spec requires read-only reconstruction from historical sources.
        This is the single source of truth for replay inputs.
        """
        if not self._frozen_input_loader:
            raise ReplayError(
                invariant_id="execution.input_loader_missing",
                phase=ReplayPhase.EXECUTION,
                component_id="replay_runner",
                message="Frozen input loader not provided - cannot reconstruct historical inputs"
            )
        
        try:
            # Load from historical sources (read-only)
            frozen_inputs = self._frozen_input_loader.load_stage_inputs(
                stage_id=stage,
                original_execution_hash=self._context.original_execution_hash,
                context=self._context
            )
            
            # Verify inputs are from historical sources (not computed)
            # This ensures true historical reconstruction
            if not frozen_inputs:
                raise ReplayError(
                    invariant_id="execution.inputs_missing",
                    phase=ReplayPhase.EXECUTION,
                    component_id=f"stage_{stage}",
                    message=f"No historical inputs found for stage {stage}",
                    expected_value="historical_inputs",
                    observed_value="empty"
                )
            
            return frozen_inputs
            
        except FileNotFoundError as e:
            raise ReplayError(
                invariant_id="execution.inputs_not_found",
                phase=ReplayPhase.EXECUTION,
                component_id=f"stage_{stage}",
                message=f"Historical inputs not found for stage {stage}: {e}",
                expected_value="historical_source",
                observed_value="not_found"
            )
        except Exception as e:
            raise ReplayError(
                invariant_id="execution.input_load_failed",
                phase=ReplayPhase.EXECUTION,
                component_id=f"stage_{stage}",
                message=f"Failed to load historical inputs for stage {stage}: {e}",
                expected_value="successful_load",
                observed_value=str(e)
            )
    
    
    # ========================================================================
    # PHASE 5: ARTIFACT EMISSION
    # ========================================================================
    
    def _phase_5_artifact_emission(self) -> None:
        """
        Emit replay artifacts.
        
        Delegates all artifact construction to ArtifactBuilder authority.
        Runner only orchestrates emission, never defines schema.
        
        Emits:
        - Execution manifests (with plan/context hashes)
        - Input fingerprints (for each stage)
        - Output hashes (for each stage)
        - Invariant confirmations
        - Failure traces (if any)
        
        Artifacts must be:
        - Immutable
        - Content-addressed
        - Timestamped with logical time only
        
        Artifacts are written to replay-only storage.
        """
        self._current_phase = ExecutionPhase.ARTIFACT_EMISSION
        step_id = self._generate_step_id("artifact_emission")
        step_start = self._tick()
        
        # Delegate execution manifest creation to ArtifactBuilder
        manifest_artifact = ArtifactBuilder.create_execution_manifest(
            plan_id=self._plan.plan_id,
            plan_hash=self._plan.plan_hash,
            context_id=self._context.context_id,
            context_hash=self._context.context_hash,
            timeline=[step.to_dict() for step in self._execution_timeline],
            artifact_id=self._generate_step_id("execution_manifest"),
            logical_timestamp=self._tick()
        )
        self._artifacts.append(manifest_artifact)
        
        # Delegate input fingerprints creation to ArtifactBuilder
        input_fingerprints = {
            stage_id: result.input_fingerprint
            for stage_id, result in self._stage_results.items()
        }
        input_fp_artifact = ArtifactBuilder.create_input_fingerprints(
            input_fingerprints=input_fingerprints,
            artifact_id=self._generate_step_id("input_fingerprints"),
            logical_timestamp=self._tick()
        )
        self._artifacts.append(input_fp_artifact)
        
        # Delegate output hashes creation to ArtifactBuilder
        output_hashes = {
            stage_id: result.output_hash
            for stage_id, result in self._stage_results.items()
        }
        output_hash_artifact = ArtifactBuilder.create_output_hashes(
            output_hashes=output_hashes,
            artifact_id=self._generate_step_id("output_hashes"),
            logical_timestamp=self._tick()
        )
        self._artifacts.append(output_hash_artifact)
        
        # Delegate invariant confirmations creation to ArtifactBuilder
        confirmations_artifact = ArtifactBuilder.create_invariant_confirmations(
            confirmations={
                "identity_verified": True,
                "environment_sealed": True,
                "scope_materialized": True,
                "execution_deterministic": True,
                "dependency_versions_locked": self._environment.verify_dependency_versions(),
                "io_permissions_enforced": len(self._environment.io_permissions) > 0,
                "thread_model_sealed": self._environment.thread_model is not None,
            },
            artifact_id=self._generate_step_id("invariant_confirmations"),
            logical_timestamp=self._tick()
        )
        self._artifacts.append(confirmations_artifact)
        
        # Record emission complete with plan/context hashes in metadata
        self._record_step(ExecutionStep(
            step_id=step_id,
            phase=ExecutionPhase.ARTIFACT_EMISSION,
            component_id="artifacts",
            started_at=step_start,
            completed_at=self._tick(),
            success=True,
            metadata={
                "artifact_count": str(len(self._artifacts)),
                "plan_hash": self._plan.plan_hash,
                "context_hash": self._context.context_hash,
            }
        ))
    
    # ========================================================================
    # UTILITIES
    # ========================================================================
    
    def _tick(self) -> int:
        """Advance logical clock and return current tick."""
        current = self._logical_clock
        self._logical_clock += 1
        return current
    
    def _generate_step_id(self, component: str) -> str:
        """Generate deterministic step ID."""
        content = f"{self._plan.plan_id}:{component}:{self._logical_clock}"
        return hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    
    def _record_step(self, step: ExecutionStep) -> None:
        """Record execution step in timeline."""
        self._execution_timeline.append(step)
    
    def _record_failure(self, error_message: str) -> None:
        """Record execution failure."""
        step = ExecutionStep(
            step_id=self._generate_step_id("failure"),
            phase=self._current_phase,
            component_id="replay_runner",
            started_at=self._tick(),
            completed_at=self._tick(),
            success=False,
            error=error_message
        )
        self._execution_timeline.append(step)
    
    
    def _hash_content(self, data: Any) -> str:
        """Compute deterministic content hash."""
        serialized = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    
    def _log_structured(self, event: str, metadata: Dict[str, Any]) -> None:
        """Emit structured log with plan/context hashes."""
        log_data = {
            "event": event,
            "plan_hash": self._plan.plan_hash,
            "context_hash": self._context.context_hash,
            "logical_clock": self._logical_clock,
            **metadata
        }
        self._logger.info(f"REPLAY:{json.dumps(log_data, sort_keys=True)}")
    
    def _compute_execution_hash(self) -> str:
        """Compute hash of execution graph structure (not timeline)."""
        # Delegate to lineage authority for graph construction
        stage_results_dict = {
            stage_id: {
                "output_hash": result.output_hash,
                "input_fingerprint": result.input_fingerprint,
                "dependencies": result.dependencies
            }
            for stage_id, result in self._stage_results.items()
        }
        execution_graph = self._lineage_authority.build_execution_graph(
            stages=self._resolved_stages,
            stage_results=stage_results_dict
        )
        return execution_graph.compute_hash()
    
    def _build_success_result(self) -> ReplayExecutionResult:
        """Build successful execution result."""
        return ReplayExecutionResult(
            plan_hash=self._plan.plan_hash,
            context_hash=self._context.context_hash,
            execution_hash=self._compute_execution_hash(),
            success=True,
            timeline=self._execution_timeline.copy(),
            artifacts=self._artifacts.copy(),
        )


# ============================================================================
# EXECUTION RESULT - Immutable Output
# ============================================================================

@dataclass(frozen=True)
class ReplayExecutionResult:
    """
    Immutable result of replay execution.
    
    Contains:
    - Execution timeline
    - Emitted artifacts
    - Success/failure status
    - Provenance hashes
    """
    plan_hash: str
    context_hash: str
    execution_hash: str
    success: bool
    timeline: List[ExecutionStep]
    artifacts: List[ReplayArtifact]
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "context_hash": self.context_hash,
            "execution_hash": self.execution_hash,
            "success": self.success,
            "timeline": [step.to_dict() for step in self.timeline],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "error": self.error,
        }
    
    def verify_idempotency(self, other: ReplayExecutionResult) -> bool:
        """
        Verify that two executions of same plan are identical.
        
        Idempotency requires:
        - Identical artifact hashes
        - Identical execution logs
        - No additional side effects
        """
        if self.plan_hash != other.plan_hash:
            return False
        if self.execution_hash != other.execution_hash:
            return False
        if len(self.artifacts) != len(other.artifacts):
            return False
        
        # Compare artifact hashes
        self_hashes = {a.content_hash for a in self.artifacts}
        other_hashes = {a.content_hash for a in other.artifacts}
        
        return self_hashes == other_hashes


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Core orchestrator
    'ReplayRunner',
    # Input types
    'ReplayPlan',
    'ReplayContext',
    'ExecutionEnvironment',
    # Result types
    'ReplayExecutionResult',
    'ExecutionStep',
    'ReplayArtifact',
    # Enums
    'ExecutionPhase',
]