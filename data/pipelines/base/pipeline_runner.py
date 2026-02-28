"""
/data/pipelines/base/pipeline_runner.py

Deterministic Pipeline Orchestration Authority

This module is the ONLY legal execution engine for data pipelines.
It enforces contract compliance, deterministic execution, and complete audit provenance.

Design Principle:
    Execution must follow declaration — or not run at all.
    
Philosophy:
    Pipelines don't run — they are proven.
    If an outcome can't be proven, it shouldn't exist.

Responsibilities:
    - Accept fully-declared pipeline plans
    - Validate step graph ordering
    - Enforce contract compliance
    - Execute steps sequentially and deterministically
    - Produce execution provenance
    - Fail-closed on ambiguity
    - Be replay-identical
    - Emit audit evidence

Forbidden:
    - Parallel execution semantics
    - Retry loops
    - Conditional step execution
    - Timeout-based branching
    - Mutable step graphs
    - Hidden execution paths
    - Error recovery
    - Best-effort completion
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeAlias, Union, final, Tuple, List, Dict

from pipeline_context import (
    ExecutionMode,
    PipelineContext,
    PipelineContextValidator,
)
from pipeline_step import (
    AlgorithmId,
    CanonicalSchema,
    PipelineStep,
    SchemaVersion,
    StepHash,
    StepKind,
)


# ============================================================================
# TYPE DEFINITIONS
# ============================================================================


RunId: TypeAlias = str
"""Unique identifier for a pipeline execution run."""


Timestamp: TypeAlias = int
"""Unix timestamp in milliseconds since epoch."""


# ============================================================================
# EXECUTION STATUS
# ============================================================================


class ExecutionStatus(Enum):
    """
    Pipeline execution terminal states.
    
    COMPLETED: All steps executed successfully, provenance recorded
    FAILED: Pipeline aborted due to step failure or validation error
    ABORTED: Pipeline terminated by external control (watchdog)
    """

    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"

    def is_terminal(self) -> bool:
        """All ExecutionStatus values are terminal states."""
        return True

    def is_success(self) -> bool:
        """Only COMPLETED represents successful execution."""
        return self == ExecutionStatus.COMPLETED


# ============================================================================
# FAILURE REASONS
# ============================================================================


class FailureReason(Enum):
    """
    Exhaustive enumeration of pipeline failure causes.
    
    Each failure reason maps to a specific validation or execution fault.
    """

    # Plan validation failures
    EMPTY_PIPELINE = "empty_pipeline"
    INVALID_PIPELINE_NAME = "invalid_pipeline_name"
    INVALID_PIPELINE_VERSION = "invalid_pipeline_version"
    DUPLICATE_STEP_HASH = "duplicate_step_hash"
    INVALID_STEP_ORDERING = "invalid_step_ordering"

    # Context validation failures
    INVALID_CONTEXT = "invalid_context"
    CONTEXT_EXECUTION_FROZEN = "context_execution_frozen"
    WATCHDOG_OVERRIDE_ACTIVE = "watchdog_override_active"
    EMERGENCY_MODE_ACTIVE = "emergency_mode_active"

    # Input validation failures
    INITIAL_INPUT_SCHEMA_MISMATCH = "initial_input_schema_mismatch"
    INITIAL_INPUT_EMPTY = "initial_input_empty"
    INITIAL_INPUT_INVALID_RECORDS = "initial_input_invalid_records"

    # Step validation failures
    STEP_KIND_UNAVAILABLE = "step_kind_unavailable"
    ALGORITHM_UNAVAILABLE = "algorithm_unavailable"
    SCHEMA_VERSION_INCOMPATIBLE = "schema_version_incompatible"
    STEP_HASH_INCONSISTENT = "step_hash_inconsistent"
    INPUT_CARDINALITY_VIOLATION = "input_cardinality_violation"

    # Execution failures
    STEP_EXECUTION_FAILED = "step_execution_failed"
    OUTPUT_SCHEMA_MISMATCH = "output_schema_mismatch"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    STEP_CONTRACT_VIOLATED = "step_contract_violated"

    # System failures
    AUDIT_EMISSION_FAILED = "audit_emission_failed"
    PROVENANCE_GENERATION_FAILED = "provenance_generation_failed"
    UNKNOWN_ERROR = "unknown_error"


# ============================================================================
# PIPELINE PLAN
# ============================================================================


@dataclass(frozen=True)
class PipelinePlan:
    """
    Immutable declaration of a complete pipeline execution plan.
    
    A PipelinePlan is the contract between pipeline definition and execution.
    It contains all information needed for deterministic, reproducible execution.
    
    Rules:
        - Steps are pre-validated
        - Order is explicit
        - No conditionals
        - No runtime branching
        - Immutable after construction
    
    Attributes:
        pipeline_name: Unique identifier for the pipeline type
        pipeline_version: Semantic version of the pipeline definition
        steps: Ordered tuple of steps to execute (immutable)
        metadata: Optional pipeline-level metadata
    """

    pipeline_name: str
    pipeline_version: str
    steps: tuple[PipelineStep, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate plan invariants at construction time."""
        self._validate_pipeline_name()
        self._validate_pipeline_version()
        self._validate_steps()
        self._validate_step_ordering()
        self._validate_no_duplicate_hashes()

    def _validate_pipeline_name(self) -> None:
        """Ensure pipeline name is valid and non-empty."""
        if not self.pipeline_name:
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_NAME,
                "Pipeline name cannot be empty",
            )
        if not isinstance(self.pipeline_name, str):
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_NAME,
                f"Pipeline name must be string, got {type(self.pipeline_name)}",
            )
        if len(self.pipeline_name) > 255:
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_NAME,
                f"Pipeline name too long: {len(self.pipeline_name)} chars (max 255)",
            )

    def _validate_pipeline_version(self) -> None:
        """Ensure pipeline version follows semantic versioning."""
        if not self.pipeline_version:
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_VERSION,
                "Pipeline version cannot be empty",
            )
        if not isinstance(self.pipeline_version, str):
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_VERSION,
                f"Pipeline version must be string, got {type(self.pipeline_version)}",
            )
        # Validate semantic version format (major.minor.patch)
        parts = self.pipeline_version.split(".")
        if len(parts) != 3:
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_VERSION,
                f"Invalid semver format: {self.pipeline_version} (expected X.Y.Z)",
            )
        try:
            for part in parts:
                int(part)
        except ValueError as e:
            raise PipelinePlanError(
                FailureReason.INVALID_PIPELINE_VERSION,
                f"Invalid semver format: {self.pipeline_version} - {e}",
            ) from e

    def _validate_steps(self) -> None:
        """Ensure pipeline has at least one step."""
        if not self.steps:
            raise PipelinePlanError(
                FailureReason.EMPTY_PIPELINE,
                "Pipeline must contain at least one step",
            )
        if not isinstance(self.steps, tuple):
            raise PipelinePlanError(
                FailureReason.INVALID_STEP_ORDERING,
                f"Steps must be tuple, got {type(self.steps)}",
            )

    def _validate_step_ordering(self) -> None:
        """
        Validate that steps form a valid execution order.
        
        Rules:
            - Each step's input schema must match previous step's output schema
            - First step must have explicitly declared input schema
            - Last step must have explicitly declared output schema
        """
        if len(self.steps) == 0:
            return

        # Validate first step
        first_step = self.steps[0]
        if not first_step.input_schema:
            raise PipelinePlanError(
                FailureReason.INVALID_STEP_ORDERING,
                f"First step '{first_step.step_hash}' must declare input schema",
            )

        # Validate step chaining
        for i in range(len(self.steps) - 1):
            current_step = self.steps[i]
            next_step = self.steps[i + 1]

            if current_step.output_schema != next_step.input_schema:
                raise PipelinePlanError(
                    FailureReason.INVALID_STEP_ORDERING,
                    f"Schema mismatch between steps {i} and {i+1}: "
                    f"step[{i}].output={current_step.output_schema} != "
                    f"step[{i+1}].input={next_step.input_schema}",
                )

        # Validate last step
        last_step = self.steps[-1]
        if not last_step.output_schema:
            raise PipelinePlanError(
                FailureReason.INVALID_STEP_ORDERING,
                f"Last step '{last_step.step_hash}' must declare output schema",
            )

    def _validate_no_duplicate_hashes(self) -> None:
        """Ensure all step hashes are unique within the pipeline."""
        seen_hashes: set[StepHash] = set()
        for step in self.steps:
            if step.step_hash in seen_hashes:
                raise PipelinePlanError(
                    FailureReason.DUPLICATE_STEP_HASH,
                    f"Duplicate step hash in pipeline: {step.step_hash}",
                )
            seen_hashes.add(step.step_hash)

    def get_step_count(self) -> int:
        """Return the number of steps in the pipeline."""
        return len(self.steps)

    def compute_plan_hash(self) -> str:
        """
        Compute a deterministic hash of the entire pipeline plan.
        
        This hash represents the complete execution plan and is used for
        replay detection and plan versioning.
        
        Returns:
            SHA-256 hash of pipeline plan
        """
        hasher = hashlib.sha256()
        hasher.update(self.pipeline_name.encode("utf-8"))
        hasher.update(self.pipeline_version.encode("utf-8"))
        for step in self.steps:
            hasher.update(step.step_hash.encode("utf-8"))
        return hasher.hexdigest()


# ============================================================================
# PIPELINE RESULT
# ============================================================================


@dataclass(frozen=True)
class PipelineResult:
    """
    Immutable execution provenance for a completed pipeline run.
    
    A PipelineResult contains proof of execution, not data.
    It is the official record that a pipeline executed with specific parameters.
    
    Attributes:
        pipeline_name: Name of the executed pipeline
        pipeline_version: Version of the executed pipeline
        run_id: Unique identifier for this execution
        status: Terminal execution status
        step_results: Ordered tuple of executed step hashes
        started_at: Execution start timestamp (ms)
        completed_at: Execution completion timestamp (ms)
        failure_reason: Reason for failure (if status == FAILED)
        failure_message: Detailed failure message (if status == FAILED)
        plan_hash: Hash of the executed pipeline plan
    """

    pipeline_name: str
    pipeline_version: str
    run_id: RunId
    status: ExecutionStatus
    step_results: tuple[StepHash, ...]
    started_at: Timestamp
    completed_at: Timestamp
    plan_hash: str
    failure_reason: FailureReason | None = None
    failure_message: str | None = None

    def __post_init__(self) -> None:
        """Validate result invariants."""
        if self.status == ExecutionStatus.FAILED:
            if self.failure_reason is None:
                raise ValueError("Failed result must have failure_reason")
        if self.completed_at < self.started_at:
            raise ValueError(
                f"Invalid timestamps: completed_at ({self.completed_at}) "
                f"< started_at ({self.started_at})"
            )

    def get_duration_ms(self) -> int:
        """Return execution duration in milliseconds."""
        return self.completed_at - self.started_at

    def get_duration_seconds(self) -> float:
        """Return execution duration in seconds."""
        return self.get_duration_ms() / 1000.0

    def is_success(self) -> bool:
        """Return True if execution completed successfully."""
        return self.status.is_success()


# ============================================================================
# STEP EXECUTION RESULT
# ============================================================================


@dataclass(frozen=True)
class StepExecutionResult:
    """
    Provenance for a single step execution within a pipeline.
    
    Attributes:
        step_hash: Hash of the executed step
        algorithm_id: Algorithm identifier (blueprint: step_hash → algorithm_id provenance link)
        step_index: Position in pipeline (0-indexed)
        started_at: Step start timestamp (ms)
        completed_at: Step completion timestamp (ms)
        input_record_count: Number of records processed
        output_record_count: Number of records produced
        success: Whether step completed successfully
        error_message: Error details (if success == False)
    """

    step_hash: StepHash
    algorithm_id: AlgorithmId
    step_index: int
    started_at: Timestamp
    completed_at: Timestamp
    input_record_count: int
    output_record_count: int
    success: bool
    error_message: str | None = None

    def get_duration_ms(self) -> int:
        """Return step execution duration in milliseconds."""
        return self.completed_at - self.started_at


# ============================================================================
# EXCEPTIONS
# ============================================================================


class PipelineRunnerError(Exception):
    """Base exception for all pipeline runner errors."""

    def __init__(
        self,
        failure_reason: FailureReason,
        message: str,
        original_error: Exception | None = None,
    ) -> None:
        self.failure_reason = failure_reason
        self.message = message
        self.original_error = original_error
        super().__init__(f"[{failure_reason.value}] {message}")


class PipelinePlanError(PipelineRunnerError):
    """Raised when pipeline plan validation fails."""

    pass


class PipelineExecutionError(PipelineRunnerError):
    """Raised when pipeline execution fails."""

    pass


class StepValidationError(PipelineRunnerError):
    """Raised when step validation fails before execution."""

    pass


class WatchdogInterventionError(PipelineRunnerError):
    """Raised when watchdog prevents execution."""

    pass


# ============================================================================
# ALGORITHM REGISTRY PROTOCOL
# ============================================================================


class AlgorithmRegistry(Protocol):
    """
    Protocol for algorithm availability checking.
    
    The runner validates algorithm availability before execution
    but does not manage the registry itself.
    """

    def is_algorithm_available(self, algorithm_id: AlgorithmId) -> bool:
        """Check if algorithm is registered and available for execution."""
        ...

    def get_algorithm_version(self, algorithm_id: AlgorithmId) -> str:
        """Get the version string for a registered algorithm."""
        ...


# ============================================================================
# SCHEMA VALIDATOR PROTOCOL
# ============================================================================


class SchemaValidator(Protocol):
    """
    Protocol for schema validation.
    
    The runner validates schema compatibility but does not
    implement validation logic itself.
    """

    def validate_schema_compatibility(
        self,
        schema_version: SchemaVersion,
        data: Iterable[CanonicalSchema],
    ) -> bool:
        """Validate that data conforms to schema version."""
        ...

    def is_schema_version_compatible(
        self,
        source_version: SchemaVersion,
        target_version: SchemaVersion,
    ) -> bool:
        """Check if two schema versions are compatible."""
        ...


# ============================================================================
# AUDIT EMITTER PROTOCOL
# ============================================================================


class AuditEmitter(Protocol):
    """
    Protocol for audit event emission.
    
    The runner emits audit events at key points but does not
    manage storage or transport.
    """

    def emit_pipeline_started(
        self,
        run_id: RunId,
        pipeline_name: str,
        pipeline_version: str,
        plan_hash: str,
        context: PipelineContext,
    ) -> None:
        """Emit audit event when pipeline starts."""
        ...

    def emit_step_started(
        self,
        run_id: RunId,
        step_hash: StepHash,
        algorithm_id: AlgorithmId,
        step_index: int,
        input_record_count: int,
    ) -> None:
        """Emit audit event when step starts."""
        ...

    def emit_step_completed(
        self,
        run_id: RunId,
        step_hash: StepHash,
        step_index: int,
        result: StepExecutionResult,
    ) -> None:
        """Emit audit event when step completes."""
        ...

    def emit_pipeline_completed(
        self,
        run_id: RunId,
        result: PipelineResult,
    ) -> None:
        """Emit audit event when pipeline completes."""
        ...

    def emit_pipeline_failed(
        self,
        run_id: RunId,
        failure_reason: FailureReason,
        failure_message: str,
        step_index: int | None = None,
    ) -> None:
        """Emit audit event when pipeline fails."""
        ...


# ============================================================================
# WATCHDOG CONTROLLER PROTOCOL
# ============================================================================


class WatchdogController(Protocol):
    """
    Protocol for watchdog state checking.
    
    The runner respects watchdog state but does not control it.
    """

    def is_execution_frozen(self) -> bool:
        """Check if global execution freeze is active."""
        ...

    def is_emergency_mode_active(self) -> bool:
        """Check if emergency mode is active."""
        ...

    def is_override_active(self) -> bool:
        """Check if watchdog override is active."""
        ...


# ============================================================================
# STEP EXECUTOR PROTOCOL
# ============================================================================


class StepExecutor(Protocol):
    """
    Protocol for step execution.
    
    The runner orchestrates execution but delegates actual
    step processing to the executor.
    """

    def execute_step(
        self,
        step: PipelineStep,
        input_data: Iterable[CanonicalSchema],
        context: PipelineContext,
    ) -> Iterable[CanonicalSchema]:
        """
        Execute a single pipeline step.
        
        Args:
            step: The step to execute
            input_data: Input records conforming to step.input_schema
            context: Execution context
            
        Returns:
            Output records conforming to step.output_schema
            
        Raises:
            Exception: If step execution fails
        """
        ...


# ============================================================================
# PIPELINE RUNNER
# ============================================================================


@final
class PipelineRunner:
    """
    Deterministic pipeline orchestration authority.
    
    The PipelineRunner is the ONLY legal execution engine for pipelines.
    It enforces:
        - Sequential, deterministic execution
        - Complete contract compliance
        - Full audit provenance
        - Fail-closed semantics
        - Replay-identical behavior
    
    The runner does NOT:
        - Retry failed steps
        - Execute steps in parallel
        - Recover from errors
        - Optimize performance
        - Make adaptive decisions
    
    Usage:
        runner = PipelineRunner(
            algorithm_registry=registry,
            schema_validator=validator,
            audit_emitter=emitter,
            watchdog=watchdog,
            step_executor=executor,
        )
        
        result = runner.run(
            plan=pipeline_plan,
            context=execution_context,
            initial_input=input_records,
        )
    """

    def __init__(
        self,
        algorithm_registry: AlgorithmRegistry,
        schema_validator: SchemaValidator,
        audit_emitter: AuditEmitter,
        watchdog: WatchdogController,
        step_executor: StepExecutor,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize pipeline runner with required dependencies.
        
        Args:
            algorithm_registry: For algorithm availability checking
            schema_validator: For schema compatibility validation
            audit_emitter: For audit event emission
            watchdog: For execution control checking
            step_executor: For step execution
            logger: Optional logger (creates default if not provided)
        """
        self._algorithm_registry = algorithm_registry
        self._schema_validator = schema_validator
        self._audit_emitter = audit_emitter
        self._watchdog = watchdog
        self._step_executor = step_executor
        self._logger = logger or logging.getLogger(__name__)

        self._context_validator = PipelineContextValidator()

    # ========================================================================
    # PRIMARY EXECUTION INTERFACE
    # ========================================================================

    def run(
        self,
        plan: PipelinePlan,
        context: PipelineContext,
        initial_input: Iterable[CanonicalSchema],
    ) -> PipelineResult:
        """
        Execute a pipeline plan deterministically.
        
        This is the ONLY legal way to execute a pipeline.
        
        Execution follows these phases:
            1. Pre-execution validation (plan, context, watchdog)
            2. Initial input validation
            3. Sequential step execution
            4. Provenance generation
            5. Audit emission
        
        Any failure in any phase causes immediate abort.
        
        Args:
            plan: Fully-declared pipeline plan
            context: Validated execution context
            initial_input: Input records for first step
            
        Returns:
            PipelineResult containing execution provenance
            
        Raises:
            PipelineRunnerError: If execution fails at any phase
        """
        # Generate deterministic run_id (requires input materialization)
        # We need to materialize input early for run_id generation
        # This is acceptable since we validate it immediately after
        try:
            input_list_for_id = list(initial_input)
        except Exception as e:
            raise PipelineExecutionError(
                FailureReason.INITIAL_INPUT_INVALID_RECORDS,
                f"Failed to materialize initial input for run_id generation: {e}",
                original_error=e,
            ) from e
        
        run_id = self._generate_run_id(plan, input_list_for_id)
        started_at = self._get_current_timestamp()

        self._logger.info(
            f"Starting pipeline execution: "
            f"pipeline={plan.pipeline_name} "
            f"version={plan.pipeline_version} "
            f"run_id={run_id} "
            f"steps={plan.get_step_count()}"
        )

        # CRITICAL: Wrap entire execution in try/except to guarantee failure audit emission
        # Blueprint requirement: "Execution is evidence, not mystery"
        # All failures MUST emit audit events, even if execution aborts
        try:
            # Phase 1: Pre-execution validation
            self._validate_pre_execution(plan, context)

            # Phase 2: Initial input validation
            # Use the already-materialized input if available, otherwise re-materialize
            # (Validation will materialize again, but that's acceptable for determinism)
            self._validate_initial_input(plan, input_list_for_id)

            # Emit pipeline started audit event (fatal if fails)
            self._emit_pipeline_started(run_id, plan, context)

            # Phase 3: Sequential step execution
            step_results = self._execute_steps(
                run_id=run_id,
                plan=plan,
                context=context,
                initial_input=input_list_for_id,
            )

            # Phase 4: Generate success result
            completed_at = self._get_current_timestamp()
            result = PipelineResult(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                run_id=run_id,
                status=ExecutionStatus.COMPLETED,
                step_results=tuple(r.step_hash for r in step_results),
                started_at=started_at,
                completed_at=completed_at,
                plan_hash=plan.compute_plan_hash(),
            )

            # Phase 5: Emit completion audit event (fatal if fails)
            self._emit_pipeline_completed(run_id, result)

            self._logger.info(
                f"Pipeline execution completed: "
                f"run_id={run_id} "
                f"duration_ms={result.get_duration_ms()} "
                f"steps_executed={len(step_results)}"
            )

            return result

        except PipelineRunnerError as e:
            # Generate failure result
            completed_at = self._get_current_timestamp()
            failure_result = self._generate_failure_result(
                plan=plan,
                run_id=run_id,
                started_at=started_at,
                completed_at=completed_at,
                error=e,
            )
            
            # CRITICAL: Emit failure audit event (blueprint requirement)
            # This MUST happen even if execution fails
            # If audit emission itself fails, that's also fatal
            self._emit_pipeline_failed(run_id, e)
            
            # Re-raise the original error (fail-closed semantics)
            raise e

    # ========================================================================
    # PRE-EXECUTION VALIDATION
    # ========================================================================

    def _validate_pre_execution(
        self,
        plan: PipelinePlan,
        context: PipelineContext,
    ) -> None:
        """
        Validate all preconditions before execution.
        
        Checks:
            - Context is valid
            - Watchdog allows execution
            - All steps are valid
        
        Raises:
            PipelineRunnerError: If any validation fails
        """
        # Validate context
        self._validate_context(context)

        # Check watchdog state
        self._check_watchdog_state(context)

        # Validate all steps in plan
        for step_index, step in enumerate(plan.steps):
            self._validate_step(step, step_index)

    def _validate_context(self, context: PipelineContext) -> None:
        """
        Validate pipeline context.
        
        Raises:
            PipelineRunnerError: If context is invalid
        """
        try:
            self._context_validator.validate(context)
        except Exception as e:
            raise PipelineExecutionError(
                FailureReason.INVALID_CONTEXT,
                f"Context validation failed: {e}",
                original_error=e,
            ) from e

    def _check_watchdog_state(self, context: PipelineContext) -> None:
        """
        Check watchdog state and abort if execution is not allowed.
        
        Raises:
            WatchdogInterventionError: If watchdog prevents execution
        """
        if self._watchdog.is_execution_frozen():
            raise WatchdogInterventionError(
                FailureReason.CONTEXT_EXECUTION_FROZEN,
                "Execution is frozen by watchdog - global execution halt active",
            )

        if self._watchdog.is_emergency_mode_active():
            raise WatchdogInterventionError(
                FailureReason.EMERGENCY_MODE_ACTIVE,
                "Emergency mode is active - all executions suspended",
            )

        if self._watchdog.is_override_active():
            raise WatchdogInterventionError(
                FailureReason.WATCHDOG_OVERRIDE_ACTIVE,
                "Watchdog override is active - execution not permitted",
            )

        # Also check context execution mode
        if context.execution_mode == ExecutionMode.FROZEN:
            raise WatchdogInterventionError(
                FailureReason.CONTEXT_EXECUTION_FROZEN,
                "Context execution mode is FROZEN",
            )

    def _validate_step(self, step: PipelineStep, step_index: int) -> None:
        """
        Validate a single step before execution.
        
        Checks:
            - Step kind is valid
            - Algorithm is available
            - Schemas are compatible
            - Step hash is consistent
        
        Raises:
            StepValidationError: If step validation fails
        """
        # Validate step kind
        if step.step_kind not in StepKind:
            raise StepValidationError(
                FailureReason.STEP_KIND_UNAVAILABLE,
                f"Step {step_index}: Invalid step kind '{step.step_kind}'",
            )

        # Validate algorithm availability
        if not self._algorithm_registry.is_algorithm_available(step.algorithm_id):
            raise StepValidationError(
                FailureReason.ALGORITHM_UNAVAILABLE,
                f"Step {step_index}: Algorithm '{step.algorithm_id}' not available",
            )

        # Validate schema versions exist and are compatible
        if step.input_schema and step.output_schema:
            if not self._schema_validator.is_schema_version_compatible(
                step.input_schema, step.output_schema
            ):
                raise StepValidationError(
                    FailureReason.SCHEMA_VERSION_INCOMPATIBLE,
                    f"Step {step_index}: Incompatible schemas - "
                    f"input={step.input_schema} output={step.output_schema}",
                )

        # Validate step hash consistency
        # Recompute step hash and verify it matches declared hash
        if not step.step_hash:
            raise StepValidationError(
                FailureReason.STEP_HASH_INCONSISTENT,
                f"Step {step_index}: Missing step hash",
            )
        
        # Recompute hash from step definition
        computed_hash = step.compute_step_hash()
        
        # Verify computed hash matches declared hash
        if computed_hash != step.step_hash:
            raise StepValidationError(
                FailureReason.STEP_HASH_INCONSISTENT,
                f"Step {step_index}: Step hash mismatch - "
                f"computed={computed_hash} declared={step.step_hash}",
            )

    # ========================================================================
    # INITIAL INPUT VALIDATION
    # ========================================================================

    def _validate_initial_input(
        self,
        plan: PipelinePlan,
        initial_input: Union[list[CanonicalSchema], Iterable[CanonicalSchema]],
    ) -> None:
        """
        Validate initial pipeline input.
        
        Checks:
            - Input is not empty
            - Input conforms to first step's input schema
            - All records are valid
        
        Args:
            plan: Pipeline plan
            initial_input: Input data (list if already materialized, otherwise iterable)
        
        Raises:
            PipelineExecutionError: If input validation fails
        """
        first_step = plan.steps[0]

        # Convert to list for validation and length check
        # Accept list directly if already materialized (optimization)
        if isinstance(initial_input, list):
            input_list = initial_input
        else:
            try:
                input_list = list(initial_input)
            except Exception as e:
                raise PipelineExecutionError(
                    FailureReason.INITIAL_INPUT_INVALID_RECORDS,
                    f"Failed to materialize initial input: {e}",
                    original_error=e,
                ) from e

        # Check not empty
        if not input_list:
            raise PipelineExecutionError(
                FailureReason.INITIAL_INPUT_EMPTY,
                "Initial input cannot be empty",
            )

        # Validate schema conformance
        if first_step.input_schema:
            try:
                is_valid = self._schema_validator.validate_schema_compatibility(
                    first_step.input_schema,
                    input_list,
                )
                if not is_valid:
                    raise PipelineExecutionError(
                        FailureReason.INITIAL_INPUT_SCHEMA_MISMATCH,
                        f"Initial input does not conform to schema "
                        f"{first_step.input_schema}",
                    )
            except Exception as e:
                raise PipelineExecutionError(
                    FailureReason.INITIAL_INPUT_SCHEMA_MISMATCH,
                    f"Initial input schema validation failed: {e}",
                    original_error=e,
                ) from e

    # ========================================================================
    # STEP EXECUTION
    # ========================================================================

    def _execute_steps(
        self,
        run_id: RunId,
        plan: PipelinePlan,
        context: PipelineContext,
        initial_input: Iterable[CanonicalSchema],
    ) -> tuple[StepExecutionResult, ...]:
        """
        Execute all steps sequentially.
        
        Steps execute in declared order. Each step consumes the output
        of the previous step. Any failure aborts the entire pipeline.
        
        Args:
            run_id: Unique run identifier
            plan: Pipeline plan
            context: Execution context
            initial_input: Input for first step
            
        Returns:
            Tuple of step execution results
            
        Raises:
            PipelineExecutionError: If any step fails
        """
        step_results: list[StepExecutionResult] = []
        current_data: Iterable[CanonicalSchema] = initial_input

        for step_index, step in enumerate(plan.steps):
            # CRITICAL: Check watchdog state before EVERY step (blueprint requirement)
            # Watchdog can flip mid-execution, so we must re-check before each step
            self._check_watchdog_state(context)
            
            self._logger.info(
                f"Executing step {step_index + 1}/{plan.get_step_count()}: "
                f"step_hash={step.step_hash} "
                f"kind={step.step_kind.value}"
            )

            # Execute single step
            step_result, output_data = self._execute_single_step(
                run_id=run_id,
                step=step,
                step_index=step_index,
                input_data=current_data,
                context=context,
            )

            step_results.append(step_result)

            # Output becomes input for next step
            current_data = output_data

            self._logger.info(
                f"Step {step_index + 1} completed: "
                f"duration_ms={step_result.get_duration_ms()} "
                f"output_records={step_result.output_record_count}"
            )

        return tuple(step_results)

    def _execute_single_step(
        self,
        run_id: RunId,
        step: PipelineStep,
        step_index: int,
        input_data: Iterable[CanonicalSchema],
        context: PipelineContext,
    ) -> tuple[StepExecutionResult, Iterable[CanonicalSchema]]:
        """
        Execute a single pipeline step with full validation.
        
        Args:
            run_id: Unique run identifier
            step: Step to execute
            step_index: Step position in pipeline
            input_data: Input records
            context: Execution context
            
        Returns:
            Tuple of (step result, output data)
            
        Raises:
            PipelineExecutionError: If step execution fails
        """
        started_at = self._get_current_timestamp()

        # Materialize input for counting and validation
        try:
            input_list = list(input_data)
        except Exception as e:
            raise PipelineExecutionError(
                FailureReason.STEP_EXECUTION_FAILED,
                f"Step {step_index}: Failed to materialize input data: {e}",
                original_error=e,
            ) from e

        input_count = len(input_list)

        # Emit step started audit event (includes algorithm_id for provenance)
        self._emit_step_started(run_id, step.step_hash, step.algorithm_id, step_index, input_count)

        # Validate input cardinality if specified
        if hasattr(step, "input_cardinality_min") and step.input_cardinality_min:
            if input_count < step.input_cardinality_min:
                raise PipelineExecutionError(
                    FailureReason.INPUT_CARDINALITY_VIOLATION,
                    f"Step {step_index}: Input count {input_count} < "
                    f"minimum {step.input_cardinality_min}",
                )

        if hasattr(step, "input_cardinality_max") and step.input_cardinality_max:
            if input_count > step.input_cardinality_max:
                raise PipelineExecutionError(
                    FailureReason.INPUT_CARDINALITY_VIOLATION,
                    f"Step {step_index}: Input count {input_count} > "
                    f"maximum {step.input_cardinality_max}",
                )

        # Execute step
        try:
            output_data = self._step_executor.execute_step(
                step=step,
                input_data=input_list,
                context=context,
            )
        except Exception as e:
            error_msg = f"Step {step_index} execution failed: {e}"
            self._logger.error(error_msg, exc_info=True)
            raise PipelineExecutionError(
                FailureReason.STEP_EXECUTION_FAILED,
                error_msg,
                original_error=e,
            ) from e

        # Materialize and validate output
        try:
            output_list = list(output_data)
        except Exception as e:
            raise PipelineExecutionError(
                FailureReason.OUTPUT_VALIDATION_FAILED,
                f"Step {step_index}: Failed to materialize output data: {e}",
                original_error=e,
            ) from e

        output_count = len(output_list)

        # Validate output schema conformance
        if step.output_schema:
            try:
                is_valid = self._schema_validator.validate_schema_compatibility(
                    step.output_schema,
                    output_list,
                )
                if not is_valid:
                    raise PipelineExecutionError(
                        FailureReason.OUTPUT_SCHEMA_MISMATCH,
                        f"Step {step_index}: Output does not conform to schema "
                        f"{step.output_schema}",
                    )
            except Exception as e:
                raise PipelineExecutionError(
                    FailureReason.OUTPUT_VALIDATION_FAILED,
                    f"Step {step_index}: Output schema validation failed: {e}",
                    original_error=e,
                ) from e

        completed_at = self._get_current_timestamp()

        # Create step result (includes algorithm_id for step_hash → algorithm_id provenance link)
        step_result = StepExecutionResult(
            step_hash=step.step_hash,
            algorithm_id=step.algorithm_id,
            step_index=step_index,
            started_at=started_at,
            completed_at=completed_at,
            input_record_count=input_count,
            output_record_count=output_count,
            success=True,
        )

        # Emit step completed audit event
        self._emit_step_completed(run_id, step.step_hash, step_index, step_result)

        return step_result, output_list

    # ========================================================================
    # RESULT GENERATION
    # ========================================================================

    def _generate_failure_result(
        self,
        plan: PipelinePlan,
        run_id: RunId,
        started_at: Timestamp,
        completed_at: Timestamp,
        error: PipelineRunnerError,
    ) -> PipelineResult:
        """
        Generate a failure result from an error.
        
        Args:
            plan: Pipeline plan that failed
            run_id: Run identifier
            started_at: Execution start timestamp
            completed_at: Execution completion timestamp
            error: The error that caused failure
            
        Returns:
            PipelineResult with FAILED status
        """
        return PipelineResult(
            pipeline_name=plan.pipeline_name,
            pipeline_version=plan.pipeline_version,
            run_id=run_id,
            status=ExecutionStatus.FAILED,
            step_results=tuple(),  # No successful steps
            started_at=started_at,
            completed_at=completed_at,
            plan_hash=plan.compute_plan_hash(),
            failure_reason=error.failure_reason,
            failure_message=error.message,
        )

    # ========================================================================
    # AUDIT EMISSION
    # ========================================================================

    def _emit_pipeline_started(
        self,
        run_id: RunId,
        plan: PipelinePlan,
        context: PipelineContext,
    ) -> None:
        """
        Emit pipeline started audit event.
        
        Raises:
            PipelineExecutionError: If audit emission fails (fatal per blueprint)
        """
        try:
            self._audit_emitter.emit_pipeline_started(
                run_id=run_id,
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                plan_hash=plan.compute_plan_hash(),
                context=context,
            )
        except Exception as e:
            # Audit emission failure is fatal per blueprint spec
            # "No provenance → invalid run"
            raise PipelineExecutionError(
                FailureReason.AUDIT_EMISSION_FAILED,
                f"Failed to emit pipeline started event: {e}",
                original_error=e,
            ) from e

    def _emit_step_started(
        self,
        run_id: RunId,
        step_hash: StepHash,
        algorithm_id: AlgorithmId,
        step_index: int,
        input_record_count: int,
    ) -> None:
        """
        Emit step started audit event.
        
        Raises:
            PipelineExecutionError: If audit emission fails (fatal per blueprint)
        """
        try:
            self._audit_emitter.emit_step_started(
                run_id=run_id,
                step_hash=step_hash,
                algorithm_id=algorithm_id,
                step_index=step_index,
                input_record_count=input_record_count,
            )
        except Exception as e:
            # Audit emission failure is fatal per blueprint spec
            raise PipelineExecutionError(
                FailureReason.AUDIT_EMISSION_FAILED,
                f"Failed to emit step started event for step {step_index}: {e}",
                original_error=e,
            ) from e

    def _emit_step_completed(
        self,
        run_id: RunId,
        step_hash: StepHash,
        step_index: int,
        result: StepExecutionResult,
    ) -> None:
        """
        Emit step completed audit event.
        
        Raises:
            PipelineExecutionError: If audit emission fails (fatal per blueprint)
        """
        try:
            self._audit_emitter.emit_step_completed(
                run_id=run_id,
                step_hash=step_hash,
                step_index=step_index,
                result=result,
            )
        except Exception as e:
            # Audit emission failure is fatal per blueprint spec
            raise PipelineExecutionError(
                FailureReason.AUDIT_EMISSION_FAILED,
                f"Failed to emit step completed event for step {step_index}: {e}",
                original_error=e,
            ) from e

    def _emit_pipeline_completed(
        self,
        run_id: RunId,
        result: PipelineResult,
    ) -> None:
        """
        Emit pipeline completed audit event.
        
        Raises:
            PipelineExecutionError: If audit emission fails (fatal per blueprint)
        """
        try:
            self._audit_emitter.emit_pipeline_completed(
                run_id=run_id,
                result=result,
            )
        except Exception as e:
            # Audit emission failure is fatal per blueprint spec
            raise PipelineExecutionError(
                FailureReason.AUDIT_EMISSION_FAILED,
                f"Failed to emit pipeline completed event: {e}",
                original_error=e,
            ) from e

    def _emit_pipeline_failed(
        self,
        run_id: RunId,
        error: PipelineRunnerError,
    ) -> None:
        """
        Emit pipeline failed audit event.
        
        Note: This is called during exception handling, so we log but don't
        raise to avoid masking the original error. However, audit failures
        should be extremely rare and indicate a critical system issue.
        
        Raises:
            PipelineExecutionError: If audit emission fails (fatal per blueprint)
        """
        try:
            self._audit_emitter.emit_pipeline_failed(
                run_id=run_id,
                failure_reason=error.failure_reason,
                failure_message=error.message,
            )
        except Exception as e:
            # Audit emission failure is fatal per blueprint spec
            # Even during failure handling, we must record the failure
            raise PipelineExecutionError(
                FailureReason.AUDIT_EMISSION_FAILED,
                f"Failed to emit pipeline failed event (original error: {error.message}): {e}",
                original_error=e,
            ) from e

    # ========================================================================
    # UTILITY METHODS
    # ========================================================================

    def _generate_run_id(
        self,
        plan: PipelinePlan,
        input_list: list[CanonicalSchema],
    ) -> RunId:
        """
        Generate a deterministic run identifier.
        
        Run ID is computed from:
        - Pipeline plan hash
        - Input data fingerprint
        
        Same plan + same input → same run_id (replay-identical).
        
        Args:
            plan: Pipeline plan
            input_list: Materialized initial input data (list)
            
        Returns:
            Deterministic SHA-256 hash (hex string)
        """
        # Compute plan hash
        plan_hash = plan.compute_plan_hash()
        
        # CRITICAL: Hash actual canonicalized input content (blueprint requirement)
        # Same inputs → same outputs → same result hash
        # CanonicalSchema protocol REQUIRES content_hash() or canonical_serialize()
        # Enforce contract strictly - fail fast if protocol is violated
        record_hashes = []
        for i, record in enumerate(input_list):
            try:
                # Prefer content_hash() (required by CanonicalSchema protocol)
                if hasattr(record, 'content_hash'):
                    record_hash = record.content_hash()
                    # Validate hash format (should be hex string)
                    if not isinstance(record_hash, str) or len(record_hash) < 32:
                        raise PipelineExecutionError(
                            FailureReason.INITIAL_INPUT_INVALID_RECORDS,
                            f"Record {i}: content_hash() returned invalid format: {type(record_hash)}",
                        )
                # Fallback to canonical_serialize() if content_hash() not available
                elif hasattr(record, 'canonical_serialize'):
                    canonical_bytes = record.canonical_serialize()
                    if not isinstance(canonical_bytes, bytes):
                        raise PipelineExecutionError(
                            FailureReason.INITIAL_INPUT_INVALID_RECORDS,
                            f"Record {i}: canonical_serialize() returned {type(canonical_bytes)}, expected bytes",
                        )
                    record_hash = hashlib.sha256(canonical_bytes).hexdigest()
                else:
                    # Protocol violation - fail fast
                    raise PipelineExecutionError(
                        FailureReason.INITIAL_INPUT_INVALID_RECORDS,
                        f"Record {i}: Does not implement CanonicalSchema protocol "
                        f"(missing content_hash() and canonical_serialize())",
                    )
            except PipelineExecutionError:
                # Re-raise our own errors
                raise
            except Exception as e:
                # Protocol method exists but failed - this is a contract violation
                raise PipelineExecutionError(
                    FailureReason.INITIAL_INPUT_INVALID_RECORDS,
                    f"Record {i}: CanonicalSchema protocol method failed: {e}",
                    original_error=e,
                ) from e
            
            record_hashes.append(record_hash)
        
        # Sort record hashes for deterministic ordering (same content → same fingerprint)
        # This ensures that input order doesn't affect run_id (if order doesn't matter)
        # If order matters, remove the sort
        sorted_record_hashes = sorted(record_hashes)
        
        # Create deterministic fingerprint combining all record content hashes
        input_fingerprint_data = {
            "record_count": len(input_list),
            "first_step_input_schema": plan.steps[0].input_schema.value if plan.steps[0].input_schema else None,
            "record_content_hashes": sorted_record_hashes,
        }
        
        # Serialize fingerprint data deterministically
        input_fingerprint_json = json.dumps(
            input_fingerprint_data,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
        input_fingerprint = hashlib.sha256(input_fingerprint_json.encode('utf-8')).hexdigest()
        
        # Combine plan hash and input fingerprint
        combined = f"{plan_hash}:{input_fingerprint}"
        run_id = hashlib.sha256(combined.encode('utf-8')).hexdigest()
        
        return run_id

    def _get_current_timestamp(self) -> Timestamp:
        """
        Get current timestamp in milliseconds.
        
        Returns:
            Unix timestamp in milliseconds since epoch
        """
        return int(time.time() * 1000)


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Primary classes
    "PipelineRunner",
    "PipelinePlan",
    "PipelineResult",
    "StepExecutionResult",
    # Enums
    "ExecutionStatus",
    "FailureReason",
    # Exceptions
    "PipelineRunnerError",
    "PipelinePlanError",
    "PipelineExecutionError",
    "StepValidationError",
    "WatchdogInterventionError",
    # Protocols
    "AlgorithmRegistry",
    "SchemaValidator",
    "AuditEmitter",
    "WatchdogController",
    "StepExecutor",
    # Type aliases
    "RunId",
    "Timestamp",
]