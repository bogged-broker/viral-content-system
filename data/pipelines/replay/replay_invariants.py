"""
Non-negotiable replay laws (hard failures).

This module defines the immutable laws of replay correctness.
These are not guidelines. These are not preferences. These are not configurable.

If any invariant here is violated, replay must abort immediately and irreversibly.

This is the constitution of replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Any, TYPE_CHECKING, List, Dict

if TYPE_CHECKING:
    from replay_plan import ReplayScope, ReplayConstraints, ReplayJustification

from replay_errors import (
    ReplayPhase,
    InvariantCategory,
    # Identity errors
    ReplayPlanHashMismatch,
    ComputationIdentityDivergence,
    PipelineVersionMismatch,
    LineageHashCorruption,
    ExecutionContextHashMismatch,
    # Environment errors
    WallClockAccessDetected,
    UndeclaredEnvironmentVariable,
    RandomnessSourceNotFrozen,
    DependencyGraphMismatch,
    ExternalIOAttempted,
    ConcurrencyModelUndeclared,
    GlobalStateMutationDetected,
    # Scope errors
    UndeclaredEntityEncountered,
    LazyWindowExpansionAttempted,
    RuntimeDiscoveryTriggered,
    ComputationNodeMissing,
    ComputationNodeExtraneous,
    EntityCountMismatch,
    PipelineStagesNotFixed,
    # Execution errors
    NonDeterministicOrderingDetected,
    NonCanonicalOrderingDetected,
    ExecutionGraphMismatch,
    DivergentFailurePoint,
    SchedulerDependentBehavior,
    ExecutionPathDivergence,
    StateTransitionMismatch,
    # Output errors
    OutputSchemaMismatch,
    OutputHashMismatch,
    SilentNormalizationAttempted,
    UnauthorizedCoercion,
    OutputCardinalityMismatch,
    OutputOrderingDivergence,
    MissingOutputArtifact,
    ExtraneoousOutputArtifact,
    DivergenceUndeclared,
)


class InvariantID(Enum):
    """Stable identifiers for all replay invariants."""
    # Identity
    REPLAY_PLAN_HASH_STABLE = "inv.identity.replay_plan_hash_stable"
    REPLAY_CONTEXT_HASH_MATCH = "inv.identity.replay_context_hash_match"
    PIPELINE_VERSION_MATCH = "inv.identity.pipeline_version_match"
    COMPUTATION_IDENTITY_EXACT = "inv.identity.computation_identity_exact"
    LINEAGE_HASH_INTEGRITY = "inv.identity.lineage_hash_integrity"
    # Environment
    TIME_SOURCE_FROZEN = "inv.environment.time_source_frozen"
    RANDOMNESS_DETERMINISTIC = "inv.environment.randomness_deterministic"
    DEPENDENCY_GRAPH_FIXED = "inv.environment.dependency_graph_fixed"
    CONCURRENCY_MODEL_DECLARED = "inv.environment.concurrency_model_declared"
    IO_PERMISSIONS_SEALED = "inv.environment.io_permissions_sealed"
    ENVIRONMENT_VARIABLES_DECLARED = "inv.environment.variables_declared"
    GLOBAL_STATE_IMMUTABLE = "inv.environment.global_state_immutable"
    # Scope
    ENTITIES_ENUMERATED = "inv.scope.entities_enumerated"
    WINDOWS_MATERIALIZED = "inv.scope.windows_materialized"
    COMPUTATIONS_DECLARED = "inv.scope.computations_declared"
    PIPELINE_STAGES_FIXED = "inv.scope.pipeline_stages_fixed"
    NO_LAZY_EXPANSION = "inv.scope.no_lazy_expansion"
    NO_RUNTIME_DISCOVERY = "inv.scope.no_runtime_discovery"
    # Execution
    EXECUTION_ORDER_DETERMINISTIC = "inv.execution.order_deterministic"
    ORDERING_CANONICAL = "inv.execution.ordering_canonical"
    EXECUTION_GRAPH_MATCH = "inv.execution.graph_match"
    NO_NONDETERMINISTIC_BRANCHES = "inv.execution.no_nondeterministic_branches"
    FAILURE_POINTS_IDENTICAL = "inv.execution.failure_points_identical"
    NO_SCHEDULER_DEPENDENCY = "inv.execution.no_scheduler_dependency"
    STATE_TRANSITIONS_DETERMINISTIC = "inv.execution.state_transitions_deterministic"
    # Output
    OUTPUT_SCHEMA_EXACT = "inv.output.schema_exact"
    OUTPUT_HASH_MATCH = "inv.output.hash_match"
    DIVERGENCES_DECLARED = "inv.output.divergences_declared"
    DIVERGENCES_SURFACED = "inv.output.divergences_surfaced"
    NO_SILENT_NORMALIZATION = "inv.output.no_silent_normalization"
    NO_IMPLICIT_COERCION = "inv.output.no_implicit_coercion"
    OUTPUT_CARDINALITY_MATCH = "inv.output.cardinality_match"
    OUTPUT_ORDERING_PRESERVED = "inv.output.ordering_preserved"


@dataclass(frozen=True)
class AssertionContext:
    """Context for invariant assertions."""
    phase: ReplayPhase
    component_id: str
    expected_value: Optional[str] = None
    observed_value: Optional[str] = None
    metadata: dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            object.__setattr__(self, 'metadata', {})


class IdentityInvariants:
    """Identity invariants ensure replay can prove exact original execution."""
    
    @staticmethod
    def assert_replay_plan_hash_stable(expected_hash: str, observed_hash: str, ctx: AssertionContext) -> None:
        if expected_hash != observed_hash:
            raise ReplayPlanHashMismatch(
                invariant_id=InvariantID.REPLAY_PLAN_HASH_STABLE.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="ReplayPlan hash is not stable",
                expected_value=expected_hash, observed_value=observed_hash)
    
    @staticmethod
    def assert_replay_context_hash_match(expected_hash: str, observed_hash: str, ctx: AssertionContext) -> None:
        if expected_hash != observed_hash:
            raise ExecutionContextHashMismatch(
                invariant_id=InvariantID.REPLAY_CONTEXT_HASH_MATCH.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="ReplayContext hash does not match declared lineage",
                expected_value=expected_hash, observed_value=observed_hash)
    
    @staticmethod
    def assert_pipeline_version_match(expected_version: str, observed_version: str, ctx: AssertionContext) -> None:
        if expected_version != observed_version:
            raise PipelineVersionMismatch(
                invariant_id=InvariantID.PIPELINE_VERSION_MATCH.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Pipeline version differs from original execution",
                expected_value=expected_version, observed_value=observed_version)
    
    @staticmethod
    def assert_computation_identity_exact(expected_identity: str, observed_identity: str, ctx: AssertionContext) -> None:
        if expected_identity != observed_identity:
            raise ComputationIdentityDivergence(
                invariant_id=InvariantID.COMPUTATION_IDENTITY_EXACT.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Computation identity hash diverged",
                expected_value=expected_identity, observed_value=observed_identity)
    
    @staticmethod
    def assert_lineage_hash_integrity(lineage_hash: str, recomputed_hash: str, ctx: AssertionContext) -> None:
        if lineage_hash != recomputed_hash:
            raise LineageHashCorruption(
                invariant_id=InvariantID.LINEAGE_HASH_INTEGRITY.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Lineage hash integrity check failed",
                expected_value=lineage_hash, observed_value=recomputed_hash)


class EnvironmentInvariants:
    """Environment invariants ensure replay executes in a sealed environment."""
    
    @staticmethod
    def assert_time_source_frozen(time_access_detected: bool, ctx: AssertionContext) -> None:
        if time_access_detected:
            raise WallClockAccessDetected(
                invariant_id=InvariantID.TIME_SOURCE_FROZEN.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Wall-clock time access detected during replay",
                expected_value="frozen_time", observed_value="wall_clock_access")
    
    @staticmethod
    def assert_randomness_deterministic(randomness_source_frozen: bool, ctx: AssertionContext) -> None:
        if not randomness_source_frozen:
            raise RandomnessSourceNotFrozen(
                invariant_id=InvariantID.RANDOMNESS_DETERMINISTIC.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Non-deterministic randomness source detected",
                expected_value="frozen_seed", observed_value="entropy_source")
    
    @staticmethod
    def assert_dependency_graph_fixed(expected_graph_hash: str, observed_graph_hash: str, ctx: AssertionContext) -> None:
        if expected_graph_hash != observed_graph_hash:
            raise DependencyGraphMismatch(
                invariant_id=InvariantID.DEPENDENCY_GRAPH_FIXED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Dependency graph differs from original execution",
                expected_value=expected_graph_hash, observed_value=observed_graph_hash)
    
    @staticmethod
    def assert_environment_variables_declared(declared_vars: set[str], accessed_vars: set[str], ctx: AssertionContext) -> None:
        undeclared = accessed_vars - declared_vars
        if undeclared:
            raise UndeclaredEnvironmentVariable(
                invariant_id=InvariantID.ENVIRONMENT_VARIABLES_DECLARED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message=f"Undeclared environment variables accessed: {sorted(undeclared)}",
                expected_value=str(sorted(declared_vars)), observed_value=str(sorted(accessed_vars)))
    
    @staticmethod
    def assert_io_permissions_sealed(external_io_attempted: bool, ctx: AssertionContext) -> None:
        if external_io_attempted:
            raise ExternalIOAttempted(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="External I/O attempted outside replay scope",
                expected_value="sealed_io", observed_value="external_io_attempted")
    
    @staticmethod
    def assert_concurrency_model_declared(concurrency_model_declared: bool, ctx: AssertionContext) -> None:
        if not concurrency_model_declared:
            raise ConcurrencyModelUndeclared(
                invariant_id=InvariantID.CONCURRENCY_MODEL_DECLARED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Concurrency model not declared before execution",
                expected_value="declared", observed_value="undeclared")
    
    @staticmethod
    def assert_global_state_immutable(global_state_mutation_detected: bool, ctx: AssertionContext) -> None:
        if global_state_mutation_detected:
            raise GlobalStateMutationDetected(
                invariant_id=InvariantID.GLOBAL_STATE_IMMUTABLE.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Mutable global state detected during replay",
                expected_value="immutable", observed_value="mutation_detected")


class ScopeInvariants:
    """Scope invariants ensure replay scope is explicit and immutable."""
    
    @staticmethod
    def assert_entities_enumerated(declared_entities: set[str], encountered_entities: set[str], ctx: AssertionContext) -> None:
        undeclared = encountered_entities - declared_entities
        if undeclared:
            raise UndeclaredEntityEncountered(
                invariant_id=InvariantID.ENTITIES_ENUMERATED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message=f"Undeclared entities encountered: {sorted(undeclared)}",
                expected_value=str(sorted(declared_entities)), observed_value=str(sorted(encountered_entities)))
    
    @staticmethod
    def assert_windows_materialized(lazy_expansion_detected: bool, ctx: AssertionContext) -> None:
        if lazy_expansion_detected:
            raise LazyWindowExpansionAttempted(
                invariant_id=InvariantID.WINDOWS_MATERIALIZED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Lazy window expansion attempted during replay",
                expected_value="materialized_windows", observed_value="lazy_expansion")
    
    @staticmethod
    def assert_computations_declared(declared_computations: set[str], encountered_computations: set[str], ctx: AssertionContext) -> None:
        missing = declared_computations - encountered_computations
        extraneous = encountered_computations - declared_computations
        if missing:
            raise ComputationNodeMissing(
                invariant_id=InvariantID.COMPUTATIONS_DECLARED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message=f"Missing computation nodes: {sorted(missing)}",
                expected_value=str(sorted(declared_computations)), observed_value=str(sorted(encountered_computations)))
        if extraneous:
            raise ComputationNodeExtraneous(
                invariant_id=InvariantID.COMPUTATIONS_DECLARED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message=f"Extraneous computation nodes: {sorted(extraneous)}",
                expected_value=str(sorted(declared_computations)), observed_value=str(sorted(encountered_computations)))
    
    @staticmethod
    def assert_no_runtime_discovery(runtime_discovery_triggered: bool, ctx: AssertionContext) -> None:
        if runtime_discovery_triggered:
            raise RuntimeDiscoveryTriggered(
                invariant_id=InvariantID.NO_RUNTIME_DISCOVERY.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Runtime entity discovery triggered during replay",
                expected_value="static_scope", observed_value="runtime_discovery")
    
    @staticmethod
    def assert_entity_count_match(expected_count: int, observed_count: int, ctx: AssertionContext) -> None:
        if expected_count != observed_count:
            raise EntityCountMismatch(
                invariant_id=InvariantID.ENTITIES_ENUMERATED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Entity count differs from declared scope",
                expected_value=str(expected_count), observed_value=str(observed_count))
    
    @staticmethod
    def assert_pipeline_stages_fixed(pipeline_stages_fixed: bool, ctx: AssertionContext) -> None:
        if not pipeline_stages_fixed:
            raise PipelineStagesNotFixed(
                invariant_id=InvariantID.PIPELINE_STAGES_FIXED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Pipeline stages not fixed before execution",
                expected_value="fixed", observed_value="not_fixed")


class ExecutionInvariants:
    """Execution invariants ensure replay preserves execution semantics."""
    
    @staticmethod
    def assert_execution_order_deterministic(nondeterministic_ordering_detected: bool, ctx: AssertionContext) -> None:
        if nondeterministic_ordering_detected:
            raise NonDeterministicOrderingDetected(
                invariant_id=InvariantID.EXECUTION_ORDER_DETERMINISTIC.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Non-deterministic execution ordering detected",
                expected_value="deterministic_order", observed_value="nondeterministic_order")
    
    @staticmethod
    def assert_ordering_canonical(ordering_canonical: bool, ctx: AssertionContext) -> None:
        if not ordering_canonical:
            raise NonCanonicalOrderingDetected(
                invariant_id=InvariantID.ORDERING_CANONICAL.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Ordering is not canonical and stable",
                expected_value="canonical", observed_value="non_canonical")
    
    @staticmethod
    def assert_execution_graph_match(expected_graph_hash: str, observed_graph_hash: str, ctx: AssertionContext) -> None:
        if expected_graph_hash != observed_graph_hash:
            raise ExecutionGraphMismatch(
                invariant_id=InvariantID.EXECUTION_GRAPH_MATCH.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Execution graph topology differs",
                expected_value=expected_graph_hash, observed_value=observed_graph_hash)
    
    @staticmethod
    def assert_no_scheduler_dependency(scheduler_dependent_behavior: bool, ctx: AssertionContext) -> None:
        if scheduler_dependent_behavior:
            raise SchedulerDependentBehavior(
                invariant_id=InvariantID.NO_SCHEDULER_DEPENDENCY.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Scheduler-dependent behavior detected",
                expected_value="deterministic_execution", observed_value="scheduler_dependent")
    
    @staticmethod
    def assert_state_transitions_deterministic(expected_state: str, observed_state: str, ctx: AssertionContext) -> None:
        if expected_state != observed_state:
            raise StateTransitionMismatch(
                invariant_id=InvariantID.STATE_TRANSITIONS_DETERMINISTIC.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="State transition differs from original execution",
                expected_value=expected_state, observed_value=observed_state)
    
    @staticmethod
    def assert_failure_points_identical(expected_failure_point: Optional[str], observed_failure_point: Optional[str], ctx: AssertionContext) -> None:
        if expected_failure_point != observed_failure_point:
            raise DivergentFailurePoint(
                invariant_id=InvariantID.FAILURE_POINTS_IDENTICAL.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Replay failed at different point than original",
                expected_value=str(expected_failure_point), observed_value=str(observed_failure_point))
    
    @staticmethod
    def assert_no_nondeterministic_branches(nondeterministic_branch_detected: bool, ctx: AssertionContext) -> None:
        if nondeterministic_branch_detected:
            raise ExecutionPathDivergence(
                invariant_id=InvariantID.NO_NONDETERMINISTIC_BRANCHES.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Non-deterministic branch dependency detected",
                expected_value="deterministic_branches", observed_value="nondeterministic_branch")


class OutputInvariants:
    """Output invariants ensure replay outputs are provably comparable."""
    
    @staticmethod
    def assert_output_schema_exact(expected_schema: str, observed_schema: str, ctx: AssertionContext) -> None:
        if expected_schema != observed_schema:
            raise OutputSchemaMismatch(
                invariant_id=InvariantID.OUTPUT_SCHEMA_EXACT.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Output schema differs from original",
                expected_value=expected_schema, observed_value=observed_schema)
    
    @staticmethod
    def assert_output_hash_match(expected_hash: str, observed_hash: str, divergence_authorized: bool, ctx: AssertionContext) -> None:
        if expected_hash != observed_hash and not divergence_authorized:
            raise OutputHashMismatch(
                invariant_id=InvariantID.OUTPUT_HASH_MATCH.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Output hash mismatch without authorization",
                expected_value=expected_hash, observed_value=observed_hash)
    
    @staticmethod
    def assert_no_silent_normalization(normalization_detected: bool, normalization_declared: bool, ctx: AssertionContext) -> None:
        if normalization_detected and not normalization_declared:
            raise SilentNormalizationAttempted(
                invariant_id=InvariantID.NO_SILENT_NORMALIZATION.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Silent normalization attempted without declaration",
                expected_value="declared_normalization", observed_value="silent_normalization")
    
    @staticmethod
    def assert_no_implicit_coercion(coercion_detected: bool, coercion_authorized: bool, ctx: AssertionContext) -> None:
        if coercion_detected and not coercion_authorized:
            raise UnauthorizedCoercion(
                invariant_id=InvariantID.NO_IMPLICIT_COERCION.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Type coercion without authorization",
                expected_value="explicit_coercion", observed_value="implicit_coercion")
    
    @staticmethod
    def assert_output_cardinality_match(expected_count: int, observed_count: int, ctx: AssertionContext) -> None:
        if expected_count != observed_count:
            raise OutputCardinalityMismatch(
                invariant_id=InvariantID.OUTPUT_CARDINALITY_MATCH.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Output record count differs",
                expected_value=str(expected_count), observed_value=str(observed_count))
    
    @staticmethod
    def assert_output_ordering_preserved(expected_order_hash: str, observed_order_hash: str, ordering_waived: bool, ctx: AssertionContext) -> None:
        if expected_order_hash != observed_order_hash and not ordering_waived:
            raise OutputOrderingDivergence(
                invariant_id=InvariantID.OUTPUT_ORDERING_PRESERVED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Output ordering differs without waiver",
                expected_value=expected_order_hash, observed_value=observed_order_hash)
    
    @staticmethod
    def assert_output_artifacts_complete(expected_artifacts: set[str], observed_artifacts: set[str], ctx: AssertionContext) -> None:
        missing = expected_artifacts - observed_artifacts
        extraneous = observed_artifacts - expected_artifacts
        if missing:
            raise MissingOutputArtifact(
                invariant_id=InvariantID.DIVERGENCES_SURFACED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message=f"Missing output artifacts: {sorted(missing)}",
                expected_value=str(sorted(expected_artifacts)), observed_value=str(sorted(observed_artifacts)))
        if extraneous:
            raise ExtraneoousOutputArtifact(
                invariant_id=InvariantID.DIVERGENCES_SURFACED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message=f"Extraneous output artifacts: {sorted(extraneous)}",
                expected_value=str(sorted(expected_artifacts)), observed_value=str(sorted(observed_artifacts)))
    
    @staticmethod
    def assert_divergences_declared(divergence_detected: bool, divergence_declared: bool, ctx: AssertionContext) -> None:
        if divergence_detected and not divergence_declared:
            raise DivergenceUndeclared(
                invariant_id=InvariantID.DIVERGENCES_DECLARED.value,
                phase=ctx.phase, component_id=ctx.component_id,
                message="Output divergence detected but not declared",
                expected_value="declared", observed_value="undeclared")


# ============================================================================
# PLAN VALIDATION - Fail-Closed Legitimacy Enforcement
# ============================================================================

class PlanValidationInvariants:
    """
    Fail-closed validation for ReplayPlan construction.
    
    These invariants MUST be enforced at plan construction time.
    Invalid plans must not be created.
    """
    
    @staticmethod
    def assert_scope_within_audit_lineage(
        plan_scope: ReplayScope,
        audit_entity_manifest: list[str],
        audit_computation_manifest: list[str],
        ctx: AssertionContext
    ) -> None:
        """
        Enforce that plan scope is subset of audit lineage.
        
        This is a fail-closed requirement: plan construction must fail
        if scope exceeds what was audited.
        """
        # Validate entities
        extra_entities = set(plan_scope.entities) - set(audit_entity_manifest)
        if extra_entities:
            raise UndeclaredEntityEncountered(
                invariant_id=InvariantID.ENTITIES_ENUMERATED.value,
                phase=ctx.phase,
                component_id=ctx.component_id,
                message=f"Plan entities not in audit lineage: {sorted(extra_entities)}",
                expected_value=str(sorted(audit_entity_manifest)),
                observed_value=str(sorted(plan_scope.entities))
            )
        
        # Validate computations
        extra_computations = set(plan_scope.computations) - set(audit_computation_manifest)
        if extra_computations:
            raise ComputationNodeExtraneous(
                invariant_id=InvariantID.COMPUTATIONS_DECLARED.value,
                phase=ctx.phase,
                component_id=ctx.component_id,
                message=f"Plan computations not in audit lineage: {sorted(extra_computations)}",
                expected_value=str(sorted(audit_computation_manifest)),
                observed_value=str(sorted(plan_scope.computations))
            )
    
    @staticmethod
    def assert_constraints_stricter_than_context(
        plan_constraints: ReplayConstraints,
        context_allows_mutation: bool,
        context_allows_persistence: bool,
        context_allows_external_writes: bool,
        ctx: AssertionContext
    ) -> None:
        """
        Enforce that plan constraints are at least as strict as context.
        
        Constraints can only tighten, never loosen context permissions.
        """
        # Plan cannot allow mutation if context forbids it
        if plan_constraints.mutation_allowed and not context_allows_mutation:
            raise GlobalStateMutationDetected(
                invariant_id=InvariantID.GLOBAL_STATE_IMMUTABLE.value,
                phase=ctx.phase,
                component_id=ctx.component_id,
                message="Plan allows mutation but context forbids it",
                expected_value="mutation_forbidden",
                observed_value="mutation_allowed"
            )
        
        # Plan cannot allow persistence if context forbids it
        if plan_constraints.persistence_allowed and not context_allows_persistence:
            raise ExternalIOAttempted(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ctx.phase,
                component_id=ctx.component_id,
                message="Plan allows persistence but context forbids it",
                expected_value="persistence_forbidden",
                observed_value="persistence_allowed"
            )
        
        # Plan cannot allow external writes if context forbids it
        if plan_constraints.external_writes_allowed and not context_allows_external_writes:
            raise ExternalIOAttempted(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ctx.phase,
                component_id=ctx.component_id,
                message="Plan allows external writes but context forbids it",
                expected_value="external_writes_forbidden",
                observed_value="external_writes_allowed"
            )
    
    @staticmethod
    def assert_context_hash_matches(
        declared_hash: str,
        computed_hash: str,
        ctx: AssertionContext
    ) -> None:
        """
        Enforce that context hash matches declared value.
        
        This verifies the context reference is valid and untampered.
        """
        if declared_hash != computed_hash:
            raise ExecutionContextHashMismatch(
                invariant_id=InvariantID.REPLAY_CONTEXT_HASH_MATCH.value,
                phase=ctx.phase,
                component_id=ctx.component_id,
                message="Context hash mismatch - context may be invalid or tampered",
                expected_value=declared_hash,
                observed_value=computed_hash
            )
    
    @staticmethod
    def assert_justification_scope_compatibility(
        justification: ReplayJustification,
        scope: ReplayScope,
        constraints: ReplayConstraints,
        ctx: AssertionContext
    ) -> None:
        """
        Enforce justification-scope-legality matrix.
        
        Different justifications have different allowed scope dimensions
        and constraint requirements.
        """
        from replay_plan import ReplayJustification as RJ
        
        # DETERMINISM_VERIFICATION requires non-empty scope
        if justification == RJ.DETERMINISM_VERIFICATION:
            if scope.get_total_size() == 0:
                raise RuntimeDiscoveryTriggered(
                    invariant_id=InvariantID.NO_RUNTIME_DISCOVERY.value,
                    phase=ctx.phase,
                    component_id=ctx.component_id,
                    message="DETERMINISM_VERIFICATION requires non-empty scope",
                    expected_value="non_empty_scope",
                    observed_value="empty_scope"
                )
        
        # REGULATORY_PROOF must forbid mutations and external writes
        if justification == RJ.REGULATORY_PROOF:
            if constraints.mutation_allowed:
                raise GlobalStateMutationDetected(
                    invariant_id=InvariantID.GLOBAL_STATE_IMMUTABLE.value,
                    phase=ctx.phase,
                    component_id=ctx.component_id,
                    message="REGULATORY_PROOF cannot allow mutations",
                    expected_value="mutation_forbidden",
                    observed_value="mutation_allowed"
                )
            if constraints.external_writes_allowed:
                raise ExternalIOAttempted(
                    invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                    phase=ctx.phase,
                    component_id=ctx.component_id,
                    message="REGULATORY_PROOF cannot allow external writes",
                    expected_value="external_writes_forbidden",
                    observed_value="external_writes_allowed"
                )
        
        # INCIDENT_FORENSICS typically requires bounded scope
        if justification == RJ.INCIDENT_FORENSICS:
            if scope.get_total_size() > 10000:  # Reasonable bound
                # Warning: very large scope for forensics
                pass
        
        # BACKFILL_VALIDATION may require persistence
        if justification == RJ.BACKFILL_VALIDATION:
            # Persistence is optional but recommended
            pass


__all__ = [
    'InvariantID',
    'AssertionContext',
    'IdentityInvariants',
    'EnvironmentInvariants',
    'ScopeInvariants',
    'ExecutionInvariants',
    'OutputInvariants',
    'PlanValidationInvariants',
]
