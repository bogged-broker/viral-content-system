"""
What to replay, why, and under whose authority.

This module is the sole authority that defines the explicit scope, justification,
and authorization of a replay.

It answers three questions - and only these three:
1. What exact entities, windows, and computations are being replayed?
2. Why is this replay happening?
3. Under whose authority is this replay permitted?

If any of those are ambiguous, replay must not start.

This file is the court order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, List, Set, Any, TYPE_CHECKING
from collections import OrderedDict

if TYPE_CHECKING:
    from replay_context import ReplayContext


# ============================================================================
# JUSTIFICATION - WHY Replay Exists
# ============================================================================

class ReplayJustification(Enum):
    """
    Explicit, structured reason for replay.
    
    Allowed categories (enum, not stringly-typed):
    - DETERMINISM_VERIFICATION: Verify execution is deterministic
    - INCIDENT_FORENSICS: Investigate production incident
    - SCHEMA_EVOLUTION_CONFIRMATION: Verify schema migration correctness
    - COMPUTATION_CHANGE_IMPACT: Assess impact of computation changes
    - REGULATORY_PROOF: Demonstrate compliance for regulators
    - BACKFILL_VALIDATION: Validate historical backfill correctness
    
    Rules:
    - Exactly one primary justification
    - Justification affects allowed scope, not behavior
    """
    DETERMINISM_VERIFICATION = "determinism_verification"
    INCIDENT_FORENSICS = "incident_forensics"
    SCHEMA_EVOLUTION_CONFIRMATION = "schema_evolution_confirmation"
    COMPUTATION_CHANGE_IMPACT = "computation_change_impact"
    REGULATORY_PROOF = "regulatory_proof"
    BACKFILL_VALIDATION = "backfill_validation"


# ============================================================================
# AUTHORITY - WHO Allows This Replay
# ============================================================================

@dataclass(frozen=True)
class ReplayAuthority:
    """
    Declares who or what authorized touching the past.
    
    The most important field in replay.
    
    Authority may be:
    - System automation (named, versioned)
    - Human actor (ID, role)
    - Incident ticket ID
    - Regulatory mandate reference
    
    Rules:
    - Must be globally identifiable
    - Must be non-mutable
    - Must be logged verbatim into replay results
    
    No anonymous replay. Ever.
    """
    authority_type: str  # "human", "system", "incident", "regulatory"
    authority_id: str    # Globally unique identifier
    authority_role: Optional[str] = None
    authority_version: Optional[str] = None  # For system automation
    
    def __post_init__(self):
        """Validate authority completeness."""
        if not self.authority_type:
            raise ValueError("authority_type cannot be empty")
        if not self.authority_id:
            raise ValueError("authority_id cannot be empty")
        
        valid_types = {"human", "system", "incident", "regulatory"}
        if self.authority_type not in valid_types:
            raise ValueError(
                f"authority_type must be one of {valid_types}, got '{self.authority_type}'"
            )
    
    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "authority_type": self.authority_type,
            "authority_id": self.authority_id,
            "authority_role": self.authority_role,
            "authority_version": self.authority_version,
        }
    
    def get_identifier(self) -> str:
        """Get globally unique identifier for this authority."""
        if self.authority_version:
            return f"{self.authority_type}:{self.authority_id}@{self.authority_version}"
        return f"{self.authority_type}:{self.authority_id}"


# ============================================================================
# SCOPE - WHAT Is Replayed
# ============================================================================

@dataclass(frozen=True)
class ReplayScope:
    """
    Defines the explicit surface area of replay.
    
    Scope includes:
    - Content IDs
    - Account IDs
    - Computation IDs
    - Window identities
    - Pipeline stages
    - Time partitions
    
    Rules:
    - Scope must be explicitly finite
    - No wildcards unless audit artifact already enumerates them
    - Scope must be derivable from audit lineage
    
    If you can't name it, you can't replay it.
    """
    entities: List[str]
    windows: List[str]
    computations: List[str]
    stages: List[str]
    time_partitions: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate scope is non-empty and finite."""
        if not self.entities and not self.computations and not self.stages:
            raise ValueError("Scope cannot be completely empty")
        
        # Verify all collections are finite
        for field_name in ["entities", "windows", "computations", "stages", "time_partitions"]:
            field_value = getattr(self, field_name)
            if not isinstance(field_value, list):
                raise ValueError(f"{field_name} must be a list")
        
        # Check for wildcards
        all_items = (
            self.entities + self.windows + self.computations + 
            self.stages + self.time_partitions
        )
        for item in all_items:
            if "*" in item or "%" in item:
                raise ValueError(
                    f"Wildcards not allowed in scope without enumeration: '{item}'"
                )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "entities": sorted(self.entities),
            "windows": sorted(self.windows),
            "computations": sorted(self.computations),
            "stages": sorted(self.stages),
            "time_partitions": sorted(self.time_partitions),
            "metadata": OrderedDict(sorted(self.metadata.items())),
        }
    
    def is_subset_of(self, audit_scope: ReplayScope) -> bool:
        """Verify this scope is a subset of audit lineage scope."""
        return (
            set(self.entities).issubset(set(audit_scope.entities)) and
            set(self.windows).issubset(set(audit_scope.windows)) and
            set(self.computations).issubset(set(audit_scope.computations)) and
            set(self.stages).issubset(set(audit_scope.stages))
        )
    
    def get_total_size(self) -> int:
        """Get total number of items in scope."""
        return (
            len(self.entities) + len(self.windows) + 
            len(self.computations) + len(self.stages)
        )


# ============================================================================
# CONSTRAINTS - SAFETY LIMITS
# ============================================================================

@dataclass(frozen=True)
class ReplayConstraints:
    """
    Hard bounds on what replay may do.
    
    Examples:
    - Max rows scanned
    - Max compute cost
    - Forbidden persistence
    - Forbidden mutation
    - Forbidden external writes
    
    Constraints can only tighten, never loosen, what context allows.
    """
    max_rows_scanned: Optional[int] = None
    max_compute_cost: Optional[float] = None
    persistence_allowed: bool = False
    mutation_allowed: bool = False
    external_writes_allowed: bool = False
    max_duration_seconds: Optional[int] = None
    
    def __post_init__(self):
        """Validate constraint sanity."""
        if self.max_rows_scanned is not None and self.max_rows_scanned <= 0:
            raise ValueError("max_rows_scanned must be positive")
        if self.max_compute_cost is not None and self.max_compute_cost <= 0:
            raise ValueError("max_compute_cost must be positive")
        if self.max_duration_seconds is not None and self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_rows_scanned": self.max_rows_scanned,
            "max_compute_cost": self.max_compute_cost,
            "persistence_allowed": self.persistence_allowed,
            "mutation_allowed": self.mutation_allowed,
            "external_writes_allowed": self.external_writes_allowed,
            "max_duration_seconds": self.max_duration_seconds,
        }
    
    def is_stricter_than(self, other: ReplayConstraints) -> bool:
        """Verify these constraints are at least as strict as another set."""
        if self.persistence_allowed and not other.persistence_allowed:
            return False
        if self.mutation_allowed and not other.mutation_allowed:
            return False
        if self.external_writes_allowed and not other.external_writes_allowed:
            return False
        
        if self.max_rows_scanned and other.max_rows_scanned:
            if self.max_rows_scanned > other.max_rows_scanned:
                return False
        
        return True


# ============================================================================
# REPLAY CONTEXT REFERENCE - The Past
# ============================================================================

@dataclass(frozen=True)
class ReplayContextRef:
    """
    Reference to an already-validated ReplayContext.
    
    Rules:
    - Must be immutable
    - Must already have passed invariant checks
    - Cannot be created or modified here
    
    ReplayPlan depends on context - never constructs it.
    """
    context_id: str
    context_hash: str
    pipeline_version: str
    original_execution_hash: str
    
    def to_dict(self) -> Dict[str, str]:
        return {
            "context_id": self.context_id,
            "context_hash": self.context_hash,
            "pipeline_version": self.pipeline_version,
            "original_execution_hash": self.original_execution_hash,
        }


# ============================================================================
# REPLAY PLAN - The Court Order
# ============================================================================

@dataclass(frozen=True)
class ReplayPlan:
    """
    Immutable declaration of replay intent, scope, and authorization.
    
    Answers three questions:
    1. WHAT: Exact entities, windows, computations (scope)
    2. WHY: Explicit justification (justification)
    3. WHO: Authority permitting this replay (authority)
    
    All fields are mandatory. There is no default replay.
    
    ReplayPlan MUST be:
    - Serializable
    - Hashable
    - Comparable
    - Stable across identical invocations
    
    Two identical replay plans must produce identical behavior.
    Plan drift is a hard failure.
    """
    plan_id: str
    replay_context: ReplayContextRef
    scope: ReplayScope
    justification: ReplayJustification
    authority: ReplayAuthority
    constraints: ReplayConstraints
    justification_notes: str = ""
    plan_hash: str = field(default="", init=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def __post_init__(self):
        """Validate plan integrity and generate hash."""
        # Basic validation (scope non-empty already checked in ReplayScope)
        if self.scope.get_total_size() == 0:
            raise ValueError("Scope cannot be completely empty")
        
        # Generate deterministic plan hash
        object.__setattr__(self, 'plan_hash', self._compute_plan_hash())
    
    @classmethod
    def create_with_full_validation(
        cls,
        plan_id: str,
        replay_context: ReplayContext,  # Full context, not just ref
        scope: ReplayScope,
        justification: ReplayJustification,
        authority: ReplayAuthority,
        constraints: ReplayConstraints,
        justification_notes: str = ""
    ) -> ReplayPlan:
        """
        Create ReplayPlan with fail-closed legitimacy enforcement.
        
        This factory method enforces all Tier-0 requirements:
        - Scope must be subset of audit lineage
        - Constraints must be stricter than context
        - Context hash must match
        - Justification-scope compatibility
        
        This is the ONLY safe way to create a ReplayPlan.
        """
        from replay_invariants import (
            PlanValidationInvariants,
            AssertionContext,
            ReplayPhase
        )
        from replay_context import ReplayContext as RC
        
        # Create context reference from full context
        context_ref = ReplayContextRef(
            context_id=replay_context.context_id,
            context_hash=replay_context.context_hash,
            pipeline_version=replay_context.audit_artifact.pipeline_version,
            original_execution_hash=replay_context.audit_artifact.artifact_hash
        )
        
        # Fail-closed: Verify context hash matches
        ctx = AssertionContext(
            phase=ReplayPhase.PLAN_CONSTRUCTION,
            component_id="context_verification"
        )
        PlanValidationInvariants.assert_context_hash_matches(
            declared_hash=context_ref.context_hash,
            computed_hash=replay_context.context_hash,
            ctx=ctx
        )
        
        # Fail-closed: Verify scope is subset of audit lineage
        ctx = AssertionContext(
            phase=ReplayPhase.PLAN_CONSTRUCTION,
            component_id="scope_lineage_validation"
        )
        PlanValidationInvariants.assert_scope_within_audit_lineage(
            plan_scope=scope,
            audit_entity_manifest=replay_context.audit_artifact.entity_manifest,
            audit_computation_manifest=replay_context.audit_artifact.computation_manifest,
            ctx=ctx
        )
        
        # Fail-closed: Verify constraints are stricter than context
        ctx = AssertionContext(
            phase=ReplayPhase.PLAN_CONSTRUCTION,
            component_id="constraint_strictness_validation"
        )
        PlanValidationInvariants.assert_constraints_stricter_than_context(
            plan_constraints=constraints,
            context_allows_mutation=replay_context.allows_mutation(),
            context_allows_persistence=replay_context.allows_persistence(),
            context_allows_external_writes=False,  # Contexts never allow external writes
            ctx=ctx
        )
        
        # Fail-closed: Verify justification-scope compatibility
        ctx = AssertionContext(
            phase=ReplayPhase.PLAN_CONSTRUCTION,
            component_id="justification_scope_compatibility"
        )
        PlanValidationInvariants.assert_justification_scope_compatibility(
            justification=justification,
            scope=scope,
            constraints=constraints,
            ctx=ctx
        )
        
        # All validations passed - create plan
        plan = cls(
            plan_id=plan_id,
            replay_context=context_ref,
            scope=scope,
            justification=justification,
            authority=authority,
            constraints=constraints,
            justification_notes=justification_notes
        )
        
        return plan
    
    def _compute_plan_hash(self) -> str:
        """
        Generate deterministic hash of plan contents.
        
        Excludes created_at from hash to maintain determinism.
        """
        canonical = self._to_canonical_dict(include_timestamp=False)
        content = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _to_canonical_dict(self, include_timestamp: bool = True) -> Dict[str, Any]:
        """Serialize to canonical dictionary form."""
        data = OrderedDict([
            ("plan_id", self.plan_id),
            ("replay_context", self.replay_context.to_dict()),
            ("scope", self.scope.to_dict()),
            ("justification", self.justification.value),
            ("authority", self.authority.to_dict()),
            ("constraints", self.constraints.to_dict()),
            ("justification_notes", self.justification_notes),
        ])
        
        if include_timestamp:
            data["created_at"] = self.created_at.isoformat()
            data["plan_hash"] = self.plan_hash
        
        return data
    
    def to_dict(self) -> Dict[str, Any]:
        """Export complete plan including metadata."""
        return self._to_canonical_dict(include_timestamp=True)
    
    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON with deterministic ordering."""
        return json.dumps(self.to_dict(), indent=indent)
    
    def verify_integrity(self) -> bool:
        """Verify plan hash matches content."""
        computed_hash = self._compute_plan_hash()
        return computed_hash == self.plan_hash
    
    def get_authority_identifier(self) -> str:
        """Get globally unique authority identifier."""
        return self.authority.get_identifier()
    
    def is_compatible_with_context(self, context_version: str) -> bool:
        """Verify plan is compatible with context version."""
        return self.replay_context.pipeline_version == context_version


# ============================================================================
# NOTE: Builder and Validator classes moved to replay_invariants.py
# ============================================================================
# 
# ReplayPlanBuilder and ReplayPlanValidator have been moved to
# replay_invariants.py as PlanValidationInvariants to maintain
# separation of concerns: replay_plan.py is purely declarative.


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def create_human_authorized_plan(
    plan_id: str,
    replay_context: ReplayContext,  # Full context required for validation
    scope: ReplayScope,
    justification: ReplayJustification,
    human_id: str,
    human_role: str,
    constraints: Optional[ReplayConstraints] = None,
    notes: str = ""
) -> ReplayPlan:
    """
    Create replay plan with human authority.
    
    Uses fail-closed validation via create_with_full_validation.
    """
    
    authority = ReplayAuthority(
        authority_type="human",
        authority_id=human_id,
        authority_role=human_role
    )
    
    if constraints is None:
        constraints = ReplayConstraints()
    
    return ReplayPlan.create_with_full_validation(
        plan_id=plan_id,
        replay_context=replay_context,
        scope=scope,
        justification=justification,
        authority=authority,
        constraints=constraints,
        justification_notes=notes
    )


def create_system_authorized_plan(
    plan_id: str,
    replay_context: ReplayContext,  # Full context required for validation
    scope: ReplayScope,
    justification: ReplayJustification,
    system_id: str,
    system_version: str,
    constraints: Optional[ReplayConstraints] = None,
    notes: str = ""
) -> ReplayPlan:
    """
    Create replay plan with system authority.
    
    Uses fail-closed validation via create_with_full_validation.
    """
    
    authority = ReplayAuthority(
        authority_type="system",
        authority_id=system_id,
        authority_version=system_version
    )
    
    if constraints is None:
        constraints = ReplayConstraints()
    
    return ReplayPlan.create_with_full_validation(
        plan_id=plan_id,
        replay_context=replay_context,
        scope=scope,
        justification=justification,
        authority=authority,
        constraints=constraints,
        justification_notes=notes
    )


# ============================================================================
# MODULE INTERFACE
# ============================================================================

__all__ = [
    # Core plan
    'ReplayPlan',
    # Components
    'ReplayContextRef',
    'ReplayScope',
    'ReplayJustification',
    'ReplayAuthority',
    'ReplayConstraints',
    # Convenience
    'create_human_authorized_plan',
    'create_system_authorized_plan',
]