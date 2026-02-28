"""
Strategy invariants enforcement.

This module validates that all repair strategies obey the four hard rules
from the Repair Strategy Doctrine:

Rule 1: Repairs are LOCAL
Rule 2: Repairs are DECLARATIVE  
Rule 3: Repairs are PROVABLE
Rule 4: Repairs are REVERSIBLE

Every repair action must pass these invariant checks before being accepted.
"""

from dataclasses import dataclass
from typing import Optional, Tuple, Dict
from enum import Enum

from .base import (
    NodeRepairAction,
    WorkflowNode,
    WorkflowDAG,
    RepairRisk,
    DeterminismLevel,
)


class InvariantViolation(Exception):
    """Base exception for invariant violations."""
    pass


class LocalityViolation(InvariantViolation):
    """Violation of Rule 1: Repairs must be LOCAL."""
    pass


class DeclarativeViolation(InvariantViolation):
    """Violation of Rule 2: Repairs must be DECLARATIVE."""
    pass


class ProvabilityViolation(InvariantViolation):
    """Violation of Rule 3: Repairs must be PROVABLE."""
    pass


class ReversibilityViolation(InvariantViolation):
    """Violation of Rule 4: Repairs must be REVERSIBLE."""
    pass


@dataclass(frozen=True)
class InvariantCheckResult:
    """Result of invariant checking."""
    passed: bool
    rule_violations: tuple[str, ...]
    details: dict[str, any]
    
    def raise_if_failed(self):
        """Raise appropriate exception if check failed."""
        if not self.passed:
            violation_msg = "\n".join(self.rule_violations)
            raise InvariantViolation(
                f"Repair strategy invariant violations detected:\n{violation_msg}"
            )


class StrategyInvariantChecker:
    """
    Enforces the four hard rules of repair strategies.
    
    Every repair action must pass ALL invariant checks.
    """
    
    def check_all_invariants(
        self,
        repair_action: NodeRepairAction,
        original_node: WorkflowNode,
        workflow: WorkflowDAG,
    ) -> InvariantCheckResult:
        """
        Check all four repair strategy invariants.
        
        Returns InvariantCheckResult with all violations.
        """
        violations = []
        details = {}
        
        # Rule 1: Locality
        locality_result = self.check_locality(repair_action, original_node, workflow)
        if not locality_result.passed:
            violations.extend(locality_result.rule_violations)
            details["locality"] = locality_result.details
        
        # Rule 2: Declarative
        declarative_result = self.check_declarative(repair_action)
        if not declarative_result.passed:
            violations.extend(declarative_result.rule_violations)
            details["declarative"] = declarative_result.details
        
        # Rule 3: Provable
        provable_result = self.check_provable(repair_action)
        if not provable_result.passed:
            violations.extend(provable_result.rule_violations)
            details["provable"] = provable_result.details
        
        # Rule 4: Reversible
        reversible_result = self.check_reversible(repair_action, original_node)
        if not reversible_result.passed:
            violations.extend(reversible_result.rule_violations)
            details["reversible"] = reversible_result.details
        
        return InvariantCheckResult(
            passed=len(violations) == 0,
            rule_violations=tuple(violations),
            details=details,
        )
    
    # ========================================================================
    # RULE 1: LOCALITY
    # ========================================================================
    
    def check_locality(
        self,
        repair_action: NodeRepairAction,
        original_node: WorkflowNode,
        workflow: WorkflowDAG,
    ) -> InvariantCheckResult:
        """
        Verify Rule 1: Repairs are LOCAL.
        
        A repair may only modify:
        - The node it targets
        - Its direct inputs (references only, not content)
        - Its direct outputs (references only, not content)
        
        Anything broader violates locality.
        """
        violations = []
        details = {}
        
        # Check 1: Repair targets the correct node
        if repair_action.node_id != original_node.node_id:
            violations.append(
                f"Rule 1 Violation: Repair targets {repair_action.node_id} "
                f"but was given node {original_node.node_id}"
            )
            details["target_mismatch"] = {
                "expected": original_node.node_id,
                "actual": repair_action.node_id,
            }
        
        # Check 2: Updated node only modifies the target node
        if repair_action.updated_node.node_id != original_node.node_id:
            violations.append(
                f"Rule 1 Violation: Updated node has different ID "
                f"({repair_action.updated_node.node_id} vs {original_node.node_id})"
            )
            details["node_id_changed"] = True
        
        # Check 3: Inputs and outputs are only from this node's scope
        valid_outputs = set(original_node.outputs)
        affected_outputs = set(repair_action.affected_artifacts)
        
        # Affected artifacts should be a subset of node's outputs
        # (or downstream artifacts, but we need to verify they're directly connected)
        invalid_artifacts = affected_outputs - valid_outputs
        
        # Verify invalid artifacts are at least direct descendants
        if invalid_artifacts:
            downstream_nodes = workflow.get_outputs(original_node.node_id)
            downstream_artifacts = set()
            for downstream_id in downstream_nodes:
                downstream_node = workflow.get_node(downstream_id)
                if downstream_node:
                    downstream_artifacts.update(downstream_node.inputs)
            
            truly_invalid = invalid_artifacts - downstream_artifacts
            if truly_invalid:
                violations.append(
                    f"Rule 1 Violation: Repair affects artifacts outside node scope: "
                    f"{truly_invalid}"
                )
                details["invalid_artifacts"] = list(truly_invalid)
        
        # Check 4: Node topology must not change
        if repair_action.updated_node.inputs != original_node.inputs:
            violations.append(
                "Rule 1 Violation: Repair modifies node inputs (DAG topology change)"
            )
            details["inputs_changed"] = {
                "original": original_node.inputs,
                "updated": repair_action.updated_node.inputs,
            }
        
        if repair_action.updated_node.outputs != original_node.outputs:
            violations.append(
                "Rule 1 Violation: Repair modifies node outputs (DAG topology change)"
            )
            details["outputs_changed"] = {
                "original": original_node.outputs,
                "updated": repair_action.updated_node.outputs,
            }
        
        return InvariantCheckResult(
            passed=len(violations) == 0,
            rule_violations=tuple(violations),
            details=details,
        )
    
    # ========================================================================
    # RULE 2: DECLARATIVE
    # ========================================================================
    
    def check_declarative(
        self,
        repair_action: NodeRepairAction,
    ) -> InvariantCheckResult:
        """
        Verify Rule 2: Repairs are DECLARATIVE.
        
        A repair must output:
        - What must change
        - How it would be recomputed
        - Under what constraints
        
        It must NOT execute the workflow or perform mutations.
        """
        violations = []
        details = {}
        
        # Check 1: Repair declares what must change
        if not repair_action.updated_node:
            violations.append(
                "Rule 2 Violation: Repair does not declare updated node state"
            )
            details["missing_updated_node"] = True
        
        # Check 2: Repair declares affected artifacts
        if not repair_action.affected_artifacts:
            violations.append(
                "Rule 2 Violation: Repair does not declare affected artifacts"
            )
            details["missing_affected_artifacts"] = True
        
        # Check 3: If recomputation is required, spec must be provided
        if repair_action.recompute_required and not repair_action.recompute_spec:
            violations.append(
                "Rule 2 Violation: Recomputation required but no recompute_spec provided"
            )
            details["missing_recompute_spec"] = True
        
        # Check 4: Repair declares determinism constraints
        if repair_action.determinism_level is None:
            violations.append(
                "Rule 2 Violation: Repair does not declare determinism level"
            )
            details["missing_determinism_level"] = True
        
        # Check 5: Repair declares replay requirements
        if not repair_action.replay_requirements:
            violations.append(
                "Rule 2 Violation: Repair does not declare replay requirements"
            )
            details["missing_replay_requirements"] = True
        
        # Check 6: Repair must not contain execution artifacts
        # (In a real system, we'd check for signs of execution like timestamps,
        # process IDs, file handles, etc.)
        if repair_action.proof_metadata.get("executed", False):
            violations.append(
                "Rule 2 Violation: Repair action contains execution artifacts "
                "(repairs must be declarative, not executed)"
            )
            details["contains_execution_artifacts"] = True
        
        return InvariantCheckResult(
            passed=len(violations) == 0,
            rule_violations=tuple(violations),
            details=details,
        )
    
    # ========================================================================
    # RULE 3: PROVABLE
    # ========================================================================
    
    def check_provable(
        self,
        repair_action: NodeRepairAction,
    ) -> InvariantCheckResult:
        """
        Verify Rule 3: Repairs are PROVABLE.
        
        Every repair must:
        - Emit a justification
        - Declare risk level
        - Declare determinism expectations
        
        No silent mutation allowed.
        """
        violations = []
        details = {}
        
        # Check 1: Justification must be present and non-empty
        if not repair_action.justification:
            violations.append(
                "Rule 3 Violation: Repair has no justification (silent mutation forbidden)"
            )
            details["missing_justification"] = True
        elif len(repair_action.justification.strip()) < 10:
            violations.append(
                "Rule 3 Violation: Justification is too brief (must be meaningful)"
            )
            details["insufficient_justification"] = {
                "length": len(repair_action.justification),
                "minimum": 10,
            }
        
        # Check 2: Risk level must be declared
        if not repair_action.risk_level:
            violations.append(
                "Rule 3 Violation: Repair does not declare risk level"
            )
            details["missing_risk_level"] = True
        
        # Check 3: Determinism expectations must be declared
        if not repair_action.determinism_level:
            violations.append(
                "Rule 3 Violation: Repair does not declare determinism level"
            )
            details["missing_determinism_level"] = True
        
        if repair_action.determinism_required is None:
            violations.append(
                "Rule 3 Violation: Repair does not declare if determinism is required"
            )
            details["missing_determinism_required"] = True
        
        # Check 4: Proof metadata must be present
        if not repair_action.proof_metadata:
            violations.append(
                "Rule 3 Violation: Repair has no proof metadata"
            )
            details["missing_proof_metadata"] = True
        else:
            # Verify essential proof fields
            required_proof_fields = ["damage_classification", "repair_decision", "timestamp"]
            missing_fields = [
                field for field in required_proof_fields
                if field not in repair_action.proof_metadata
            ]
            if missing_fields:
                violations.append(
                    f"Rule 3 Violation: Proof metadata missing required fields: {missing_fields}"
                )
                details["missing_proof_fields"] = missing_fields
        
        # Check 5: Determinism requirements must match constraints
        if repair_action.determinism_required:
            if repair_action.determinism_level == DeterminismLevel.NON_DETERMINISTIC:
                violations.append(
                    "Rule 3 Violation: Cannot require determinism for non-deterministic repair"
                )
                details["determinism_contradiction"] = {
                    "required": True,
                    "level": "NON_DETERMINISTIC",
                }
        
        return InvariantCheckResult(
            passed=len(violations) == 0,
            rule_violations=tuple(violations),
            details=details,
        )
    
    # ========================================================================
    # RULE 4: REVERSIBLE
    # ========================================================================
    
    def check_reversible(
        self,
        repair_action: NodeRepairAction,
        original_node: WorkflowNode,
    ) -> InvariantCheckResult:
        """
        Verify Rule 4: Repairs are REVERSIBLE.
        
        If a repair cannot be:
        - Replayed
        - Merged
        - Rolled back
        
        ...it is forbidden.
        """
        violations = []
        details = {}
        
        # Check 1: Repair must declare it is reversible
        if not repair_action.is_reversible:
            violations.append(
                "Rule 4 Violation: Repair is marked as irreversible (forbidden)"
            )
            details["is_reversible"] = False
        
        # Check 2: Rollback checkpoint must be present
        if not repair_action.rollback_checkpoint:
            violations.append(
                "Rule 4 Violation: Repair has no rollback checkpoint"
            )
            details["missing_rollback_checkpoint"] = True
        
        # Check 3: For recomputation repairs, determinism must be achievable
        if repair_action.recompute_required:
            if repair_action.determinism_level == DeterminismLevel.NON_DETERMINISTIC:
                # Non-deterministic repairs cannot be reliably replayed
                violations.append(
                    "Rule 4 Violation: Recomputation repair is non-deterministic "
                    "(cannot guarantee reversibility through replay)"
                )
                details["non_deterministic_recompute"] = True
        
        # Check 4: Replay requirements must be satisfiable
        if not repair_action.replay_requirements:
            violations.append(
                "Rule 4 Violation: No replay requirements specified "
                "(cannot verify reversibility)"
            )
            details["missing_replay_requirements"] = True
        else:
            # Check for conflicting requirements
            requires_seed = repair_action.replay_requirements.get("requires_seed_injection", False)
            if requires_seed and repair_action.recompute_spec:
                if repair_action.recompute_spec.inject_seed is None:
                    violations.append(
                        "Rule 4 Violation: Replay requires seed injection but no seed provided"
                    )
                    details["seed_requirement_unsatisfied"] = True
        
        # Check 5: Updated node must maintain essential structure
        # (We need to be able to identify the node after repair)
        if repair_action.updated_node.node_id != original_node.node_id:
            violations.append(
                "Rule 4 Violation: Node ID changed (breaks rollback reference)"
            )
            details["node_id_changed"] = {
                "original": original_node.node_id,
                "updated": repair_action.updated_node.node_id,
            }
        
        if repair_action.updated_node.node_type != original_node.node_type:
            violations.append(
                "Rule 4 Violation: Node type changed (breaks rollback compatibility)"
            )
            details["node_type_changed"] = {
                "original": original_node.node_type,
                "updated": repair_action.updated_node.node_type,
            }
        
        # Check 6: For high-risk repairs, additional reversibility proof required
        if repair_action.risk_level in [RepairRisk.HIGH, RepairRisk.CRITICAL]:
            if "reversibility_proof" not in repair_action.proof_metadata:
                violations.append(
                    f"Rule 4 Violation: {repair_action.risk_level.value.upper()} risk repair "
                    f"requires explicit reversibility proof in metadata"
                )
                details["missing_reversibility_proof"] = True
        
        return InvariantCheckResult(
            passed=len(violations) == 0,
            rule_violations=tuple(violations),
            details=details,
        )


# ============================================================================
# PUBLIC API
# ============================================================================

def validate_repair_strategy(
    repair_action: NodeRepairAction,
    original_node: WorkflowNode,
    workflow: WorkflowDAG,
) -> InvariantCheckResult:
    """
    Validate that a repair action obeys all four strategy rules.
    
    This should be called before accepting any repair action.
    
    Args:
        repair_action: The proposed repair action
        original_node: The original node being repaired
        workflow: The workflow DAG context
    
    Returns:
        InvariantCheckResult with all violations
    
    Raises:
        InvariantViolation: If check fails and raise_if_failed() is called
    
    Example:
        >>> result = validate_repair_strategy(repair_action, node, workflow)
        >>> result.raise_if_failed()  # Raises if any violations
        >>> # OR
        >>> if not result.passed:
        ...     print(f"Violations: {result.rule_violations}")
    """
    checker = StrategyInvariantChecker()
    return checker.check_all_invariants(repair_action, original_node, workflow)


def enforce_repair_strategy(
    repair_action: NodeRepairAction,
    original_node: WorkflowNode,
    workflow: WorkflowDAG,
) -> None:
    """
    Enforce strategy invariants, raising exception on violations.
    
    This is a strict version that always raises on failure.
    
    Args:
        repair_action: The proposed repair action
        original_node: The original node being repaired
        workflow: The workflow DAG context
    
    Raises:
        InvariantViolation: If any rule is violated
    """
    result = validate_repair_strategy(repair_action, original_node, workflow)
    result.raise_if_failed()