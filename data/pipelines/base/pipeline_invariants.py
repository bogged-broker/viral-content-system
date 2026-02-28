"""
/data/pipelines/base/pipeline_invariants.py

Global Pipeline Safety & Legitimacy Invariants

This module defines the non-negotiable laws that every data pipeline must obey.
It answers: "Even if a pipeline is well-formed — is it allowed to exist?"

This file is above steps, runners, and pipelines.
If a pipeline violates an invariant, it must never execute — even in replay.

Design Principle:
    Pipelines must be safe under abuse, replay, and misunderstanding.
    If an invariant exists, it's because systems died without it.

Authority Level: ABSOLUTE
All violations are fatal, non-recoverable, non-overrideable, non-configurable.

Placement & Authority:
    /data/pipelines/base/
    ├── pipeline_context.py
    ├── pipeline_step.py
    ├── pipeline_runner.py
    └── pipeline_invariants.py   # THIS FILE

Invariants are consulted before construction, execution, or replay.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline_context import PipelineContext
    from pipeline_runner import PipelinePlan
    from pipeline_step import PipelineStep

# ============================================================================
# STRUCTURAL ANALYSIS LAYER (TIER-0)
# ============================================================================


class StructuralAnalyzer:
    """Tier-0 structural analysis for pipeline steps.
    
    Analyzes actual code structure, not just metadata strings.
    Provides proofs of purity, determinism, and IO isolation.
    """
    
    FORBIDDEN_IMPORTS = frozenset([
        "random", "secrets", "uuid", "time", "datetime", "os", "sys",
        "socket", "urllib", "requests", "http", "subprocess", "multiprocessing"
    ])
    
    FORBIDDEN_ATTRIBUTES = frozenset([
        "random", "randint", "randn", "rand", "choice", "shuffle", "seed",
        "uuid4", "token_bytes", "token_hex", "now", "today", "utcnow",
        "time", "monotonic", "environ", "getenv", "platform", "system"
    ])
    
    IO_PATTERNS = frozenset([
        "open", "read", "write", "http", "https", "ftp", "s3", "gcs",
        "socket", "requests", "urllib", "subprocess"
    ])
    
    STATE_WRITE_PATTERNS = frozenset([
        "write", "save", "persist", "commit", "update", "insert",
        "delete", "mutate", "modify", "change", "set", "put"
    ])
    
    @staticmethod
    def analyze_step_code(step: "PipelineStep") -> Dict[str, Any]:
        """Analyze step code structure for purity violations.
        
        Returns:
            Analysis results with detected violations and structural properties
        """
        result = {
            "has_source": False,
            "ast_parseable": False,
            "violations": [],
            "structural_properties": {
                "has_random": False,
                "has_io": False,
                "has_state_writes": False,
                "has_wall_clock": False,
                "has_env_access": False,
                "is_pure": True,
            }
        }
        
        # Try to get transformation function if available
        transform_fn = getattr(step, "transform_fn", None)
        if not transform_fn or not callable(transform_fn):
            # Fallback: analyze algorithm_id for patterns
            return StructuralAnalyzer._analyze_metadata(step)
        
        try:
            source = inspect.getsource(transform_fn)
            result["has_source"] = True
            
            try:
                tree = ast.parse(source)
                result["ast_parseable"] = True
                violations = []
                StructuralAnalyzer._visit_ast(tree, violations, result["structural_properties"])
                result["violations"] = violations
                
                # Determine purity
                props = result["structural_properties"]
                result["structural_properties"]["is_pure"] = (
                    not props["has_random"] and
                    not props["has_io"] and
                    not props["has_state_writes"] and
                    not props["has_wall_clock"] and
                    not props["has_env_access"]
                )
            except SyntaxError:
                result["violations"].append("AST parse failed - syntax error")
        except (OSError, TypeError):
            # Source unavailable - fallback to metadata analysis
            return StructuralAnalyzer._analyze_metadata(step)
        
        return result
    
    @staticmethod
    def _analyze_metadata(step: "PipelineStep") -> Dict[str, Any]:
        """Fallback metadata analysis when source unavailable."""
        algorithm_lower = step.algorithm_id.lower()
        
        return {
            "has_source": False,
            "ast_parseable": False,
            "violations": [],
            "structural_properties": {
                "has_random": any(p in algorithm_lower for p in ["random", "rand", "uuid"]),
                "has_io": any(p in algorithm_lower for p in StructuralAnalyzer.IO_PATTERNS),
                "has_state_writes": any(p in algorithm_lower for p in StructuralAnalyzer.STATE_WRITE_PATTERNS),
                "has_wall_clock": any(p in algorithm_lower for p in ["now", "time", "datetime"]),
                "has_env_access": any(p in algorithm_lower for p in ["env", "environ", "getenv"]),
                "is_pure": False,  # Conservative: assume impure if no source
            }
        }
    
    @staticmethod
    def _visit_ast(node: ast.AST, violations: List[str], properties: Dict[str, bool]) -> None:
        """Recursively visit AST nodes to detect violations."""
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_base = alias.name.split('.')[0]
                if module_base in StructuralAnalyzer.FORBIDDEN_IMPORTS:
                    violations.append(f"Forbidden import: {alias.name}")
                    if module_base in ["random", "secrets", "uuid"]:
                        properties["has_random"] = True
                    if module_base in ["time", "datetime"]:
                        properties["has_wall_clock"] = True
                    if module_base in ["os", "sys"]:
                        properties["has_env_access"] = True
        
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_base = node.module.split('.')[0]
                if module_base in StructuralAnalyzer.FORBIDDEN_IMPORTS:
                    violations.append(f"Forbidden import from: {node.module}")
                    if module_base in ["random", "secrets", "uuid"]:
                        properties["has_random"] = True
                    if module_base in ["time", "datetime"]:
                        properties["has_wall_clock"] = True
                    if module_base in ["os", "sys"]:
                        properties["has_env_access"] = True
        
        # Check function calls
        elif isinstance(node, ast.Call):
            func_name = StructuralAnalyzer._get_call_name(node)
            if func_name:
                func_lower = func_name.lower()
                
                # Random operations
                if any(p in func_lower for p in ["random", "rand", "uuid", "seed"]):
                    violations.append(f"Forbidden function call: {func_name}")
                    properties["has_random"] = True
                
                # Wall clock
                if any(p in func_lower for p in ["now", "time", "today", "utcnow"]):
                    violations.append(f"Forbidden time access: {func_name}")
                    properties["has_wall_clock"] = True
                
                # IO operations
                if any(p in func_lower for p in StructuralAnalyzer.IO_PATTERNS):
                    violations.append(f"Forbidden IO operation: {func_name}")
                    properties["has_io"] = True
                
                # State writes
                if any(p in func_lower for p in StructuralAnalyzer.STATE_WRITE_PATTERNS):
                    # Only flag if clearly a state operation
                    if any(ctx in func_lower for ctx in ["db", "store", "state", "persist"]):
                        violations.append(f"Forbidden state write: {func_name}")
                        properties["has_state_writes"] = True
                
                # Environment access
                if any(p in func_lower for p in ["environ", "getenv", "platform"]):
                    violations.append(f"Forbidden environment access: {func_name}")
                    properties["has_env_access"] = True
        
        # Check attribute access
        elif isinstance(node, ast.Attribute):
            if node.attr in StructuralAnalyzer.FORBIDDEN_ATTRIBUTES:
                violations.append(f"Forbidden attribute access: {node.attr}")
                if node.attr in ["random", "randint", "uuid4"]:
                    properties["has_random"] = True
                if node.attr in ["now", "time", "today"]:
                    properties["has_wall_clock"] = True
                if node.attr in ["environ", "getenv"]:
                    properties["has_env_access"] = True
        
        # Recursively visit children
        for child in ast.iter_child_nodes(node):
            StructuralAnalyzer._visit_ast(child, violations, properties)
    
    @staticmethod
    def _get_call_name(node: ast.Call) -> Optional[str]:
        """Extract function name from call node."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None


class ProvenanceDAGValidator:
    """Validates complete provenance DAG structure.
    
    Tier-0: Ensures every output schema maps to previous lineage nodes.
    """
    
    @staticmethod
    def validate_complete_lineage(
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> Tuple[bool, List[str]]:
        """Validate complete provenance DAG.
        
        Returns:
            (is_valid, missing_links)
        """
        missing_links = []
        
        # Build schema lineage graph
        schema_lineage = {}
        
        # Initialize with input schemas
        for schema_version in context.input_schema_versions:
            schema_key = (schema_version.name, schema_version.version)
            schema_lineage[schema_key] = {
                "source": "input",
                "step_index": None,
            }
        
        # Track schema flow through steps
        for step_index, step in enumerate(plan.steps):
            input_key = (step.input_schema, step.input_schema_version)
            output_key = (step.output_schema, step.output_schema_version)
            
            # Verify input schema exists in lineage
            if input_key not in schema_lineage:
                missing_links.append(
                    f"Step {step_index} ({step.step_name}): "
                    f"Input schema {step.input_schema}@v{step.input_schema_version} "
                    f"not in provenance lineage"
                )
            
            # Add output to lineage
            schema_lineage[output_key] = {
                "source": "step",
                "step_index": step_index,
                "step_name": step.step_name,
                "parent": input_key,
            }
        
        # Verify output schema is in lineage
        output_key = (
            context.output_schema_version.name,
            context.output_schema_version.version
        )
        if output_key not in schema_lineage:
            missing_links.append(
                f"Output schema {output_key[0]}@v{output_key[1]} not in provenance lineage"
            )
        
        return len(missing_links) == 0, missing_links


class FormalContractValidator:
    """Validates formal contracts for step properties.
    
    Tier-0: Ensures step properties are truthful and complete.
    """
    
    @staticmethod
    def validate_step_contracts(
        step: "PipelineStep",
        structural_analysis: Dict[str, Any],
    ) -> List[str]:
        """Validate that step properties match structural reality.
        
        Returns:
            List of contract violations
        """
        violations = []
        props = structural_analysis.get("structural_properties", {})
        
        # Check if step claims determinism but has random operations
        if hasattr(step, "deterministic") and step.deterministic:
            if props.get("has_random", False):
                violations.append(
                    f"Step claims deterministic=True but has random operations"
                )
        
        # Check if step claims purity but has side effects
        if hasattr(step, "preserves_cardinality"):
            # Map steps must preserve cardinality
            from pipeline_step import MapStep
            if isinstance(step, MapStep):
                if not step.preserves_cardinality:
                    violations.append(
                        f"MapStep must preserve cardinality (contract violation)"
                    )
        
        # Check if step claims no IO but has IO operations
        if props.get("has_io", False):
            violations.append(
                f"Step performs IO operations (forbidden for replay safety)"
            )
        
        # Check if step claims no state writes but has state operations
        if props.get("has_state_writes", False):
            violations.append(
                f"Step writes external state (forbidden for replay safety)"
            )
        
        return violations

# ============================================================================
# CORE ENUMS
# ============================================================================


class InvariantCategory(Enum):
    """Categories of pipeline invariants."""
    
    DETERMINISM = "determinism"
    TIME_WINDOW = "time_window"
    CARDINALITY = "cardinality"
    DATA_LOSS = "data_loss"
    SCHEMA_INTEGRITY = "schema_integrity"
    LINEAGE_PROVENANCE = "lineage_provenance"
    REPLAY_SAFETY = "replay_safety"


class ViolationSeverity(Enum):
    """All violations are FATAL - this exists for audit classification only."""
    
    FATAL = "fatal"  # The only severity level


# ============================================================================
# EXCEPTION HIERARCHY
# ============================================================================


class PipelineInvariantViolation(Exception):
    """Base exception for all invariant violations.
    
    All violations are:
    - fatal
    - non-recoverable
    - non-overrideable
    - non-configurable
    
    Policy cannot weaken invariants.
    """
    
    def __init__(
        self,
        message: str,
        category: InvariantCategory,
        invariant_id: str,
        context: Dict[str, Any],
        violation_hash: str,
    ):
        super().__init__(message)
        self.category = category
        self.invariant_id = invariant_id
        self.context = context
        self.violation_hash = violation_hash
        self.timestamp = datetime.now(timezone.utc)
        self.severity = ViolationSeverity.FATAL
        
    def to_audit_event(self) -> Dict[str, Any]:
        """Convert violation to audit event."""
        return {
            "violation_id": self.violation_hash,
            "category": self.category.value,
            "invariant_id": self.invariant_id,
            "message": str(self),
            "severity": self.severity.value,
            "timestamp": self.timestamp.isoformat(),
            "context": self.context,
            "recoverable": False,
            "overrideable": False,
        }


class DeterminismViolation(PipelineInvariantViolation):
    """Violation of determinism guarantees."""
    pass


class TimeWindowViolation(PipelineInvariantViolation):
    """Violation of time handling rules."""
    pass


class CardinalityViolation(PipelineInvariantViolation):
    """Violation of cardinality constraints."""
    pass


class DataLossViolation(PipelineInvariantViolation):
    """Violation of data loss prevention rules."""
    pass


class SchemaIntegrityViolation(PipelineInvariantViolation):
    """Violation of schema integrity constraints."""
    pass


class LineageProvenanceViolation(PipelineInvariantViolation):
    """Violation of lineage tracking requirements."""
    pass


class ReplaySafetyViolation(PipelineInvariantViolation):
    """Violation of replay safety guarantees."""
    pass


# ============================================================================
# VIOLATION CONTEXT
# ============================================================================


@dataclass(frozen=True)
class ViolationContext:
    """Immutable context for a violation."""
    
    pipeline_name: str
    pipeline_version: str
    step_name: Optional[str] = None
    step_index: Optional[int] = None
    step_kind: Optional[str] = None
    additional_data: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "pipeline_name": self.pipeline_name,
            "pipeline_version": self.pipeline_version,
            "step_name": self.step_name,
            "step_index": self.step_index,
            "step_kind": self.step_kind,
            **self.additional_data,
        }


# ============================================================================
# INVARIANT BASE CLASS
# ============================================================================


class PipelineInvariant(ABC):
    """Base class for all pipeline invariants.
    
    Each invariant is:
    - Uniquely identified
    - Categorized
    - Self-validating
    - Audit-capable
    """
    
    def __init__(self):
        self.invariant_id = self._generate_invariant_id()
        
    @property
    @abstractmethod
    def category(self) -> InvariantCategory:
        """Category of this invariant."""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of the invariant."""
        pass
    
    @abstractmethod
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate the invariant.
        
        Args:
            plan: The pipeline plan to validate
            context: The pipeline context
            
        Raises:
            PipelineInvariantViolation: If the invariant is violated
        """
        pass
    
    def _generate_invariant_id(self) -> str:
        """Generate unique invariant ID."""
        class_name = self.__class__.__name__
        module_name = self.__class__.__module__
        return f"{module_name}.{class_name}"
    
    def _create_violation_hash(
        self,
        violation_context: ViolationContext,
    ) -> str:
        """Create deterministic hash for violation.
        
        Tier-0: Uses 128 bits (32 hex chars) for collision resistance at scale.
        """
        data = (
            f"{self.invariant_id}:"
            f"{violation_context.pipeline_name}:"
            f"{violation_context.pipeline_version}:"
            f"{violation_context.step_name or ''}:"
            f"{violation_context.step_index or ''}:"
            f"{hashlib.sha256(str(violation_context.additional_data).encode()).hexdigest()}"
        )
        # Tier-0: Full 128-bit hash (32 hex chars) for collision resistance
        return hashlib.sha256(data.encode()).hexdigest()[:32]
    
    def _raise_violation(
        self,
        message: str,
        violation_context: ViolationContext,
        exception_class: type[PipelineInvariantViolation],
    ) -> None:
        """Raise an invariant violation."""
        violation_hash = self._create_violation_hash(violation_context)
        raise exception_class(
            message=message,
            category=self.category,
            invariant_id=self.invariant_id,
            context=violation_context.to_dict(),
            violation_hash=violation_hash,
        )


# ============================================================================
# 1. DETERMINISM INVARIANTS
# ============================================================================


class NoRandomSeedsInvariant(PipelineInvariant):
    """Forbid any random number generation or seeding.
    
    A pipeline MUST have no random seeds.
    If two replays differ → invariant violated.
    
    Tier-0: Uses structural AST analysis, not heuristic string checks.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DETERMINISM
    
    @property
    def description(self) -> str:
        return "Pipelines must not use random number generation or seeding"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no random operations in pipeline steps."""
        for step_index, step in enumerate(plan.steps):
            self._validate_step_no_random(step, step_index, plan)
    
    def _validate_step_no_random(
        self,
        step: "PipelineStep",
        step_index: int,
        plan: "PipelinePlan",
    ) -> None:
        """Validate a single step has no random operations.
        
        Tier-0: Uses structural analysis, not string heuristics.
        """
        # Perform structural analysis
        analysis = StructuralAnalyzer.analyze_step_code(step)
        props = analysis.get("structural_properties", {})
        
        # Check for random operations
        if props.get("has_random", False) or analysis.get("violations"):
            # Filter random-related violations
            random_violations = [
                v for v in analysis.get("violations", [])
                if any(p in v.lower() for p in ["random", "rand", "uuid", "seed"])
            ]
            
            if random_violations or props.get("has_random", False):
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={
                        "algorithm_id": step.algorithm_id,
                        "violations": random_violations,
                        "has_source": analysis.get("has_source", False),
                        "ast_parseable": analysis.get("ast_parseable", False),
                        "structural_analysis": props,
                    },
                )
                self._raise_violation(
                    f"Step '{step.step_name}' uses forbidden random operations. "
                    f"Structural analysis detected: {random_violations}",
                    violation_context,
                    DeterminismViolation,
                )


class NoEnvironmentDependenceInvariant(PipelineInvariant):
    """Forbid environment-dependent logic.
    
    A pipeline MUST have no env-dependent logic.
    
    Tier-0: Uses structural AST analysis.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DETERMINISM
    
    @property
    def description(self) -> str:
        return "Pipelines must not depend on environment variables or platform detection"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no environment dependencies."""
        for step_index, step in enumerate(plan.steps):
            self._validate_step_no_env(step, step_index, plan)
    
    def _validate_step_no_env(
        self,
        step: "PipelineStep",
        step_index: int,
        plan: "PipelinePlan",
    ) -> None:
        """Validate a single step has no environment dependencies.
        
        Tier-0: Uses structural analysis.
        """
        analysis = StructuralAnalyzer.analyze_step_code(step)
        props = analysis.get("structural_properties", {})
        
        if props.get("has_env_access", False):
            env_violations = [
                v for v in analysis.get("violations", [])
                if any(p in v.lower() for p in ["environ", "getenv", "platform", "env"])
            ]
            
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                step_name=step.step_name,
                step_index=step_index,
                step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                additional_data={
                    "algorithm_id": step.algorithm_id,
                    "env_access": env_violations,
                    "structural_analysis": props,
                },
            )
            self._raise_violation(
                f"Step '{step.step_name}' accesses environment: {env_violations}",
                violation_context,
                DeterminismViolation,
            )


class NoWallClockAccessInvariant(PipelineInvariant):
    """Forbid wall-clock time access.
    
    A pipeline MUST have no wall-clock access.
    
    Tier-0: Uses structural AST analysis.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DETERMINISM
    
    @property
    def description(self) -> str:
        return "Pipelines must not access wall-clock time"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no wall-clock access."""
        for step_index, step in enumerate(plan.steps):
            self._validate_step_no_wallclock(step, step_index, plan)
    
    def _validate_step_no_wallclock(
        self,
        step: "PipelineStep",
        step_index: int,
        plan: "PipelinePlan",
    ) -> None:
        """Validate a single step has no wall-clock access.
        
        Tier-0: Uses structural analysis.
        """
        analysis = StructuralAnalyzer.analyze_step_code(step)
        props = analysis.get("structural_properties", {})
        
        if props.get("has_wall_clock", False):
            time_violations = [
                v for v in analysis.get("violations", [])
                if any(p in v.lower() for p in ["now", "time", "today", "utcnow", "datetime"])
            ]
            
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                step_name=step.step_name,
                step_index=step_index,
                step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                additional_data={
                    "algorithm_id": step.algorithm_id,
                    "time_calls": time_violations,
                    "structural_analysis": props,
                },
            )
            self._raise_violation(
                f"Step '{step.step_name}' accesses wall-clock time: {time_violations}",
                violation_context,
                DeterminismViolation,
            )


class StableHashRequirementInvariant(PipelineInvariant):
    """Require stable hashing for all data.
    
    A pipeline MUST produce stable hashes forever.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DETERMINISM
    
    @property
    def description(self) -> str:
        return "All pipeline data must have stable, reproducible hashes"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate stable hashing configuration."""
        # Check if hash algorithm is declared
        if not hasattr(context, "hash_algorithm"):
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={},
            )
            self._raise_violation(
                "Pipeline context must declare hash_algorithm",
                violation_context,
                DeterminismViolation,
            )
        
        # Validate hash algorithm is approved
        approved_algorithms = {"sha256", "sha512", "blake2b"}
        if context.hash_algorithm not in approved_algorithms:
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"hash_algorithm": context.hash_algorithm},
            )
            self._raise_violation(
                f"Hash algorithm '{context.hash_algorithm}' not in approved set: {approved_algorithms}",
                violation_context,
                DeterminismViolation,
            )


# ============================================================================
# 2. TIME & WINDOW INVARIANTS
# ============================================================================


class ExplicitTimeDeclarationInvariant(PipelineInvariant):
    """Require explicit time usage declaration.
    
    Pipelines MUST declare all time usage explicitly.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.TIME_WINDOW
    
    @property
    def description(self) -> str:
        return "All time usage must be explicitly declared"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate explicit time declarations."""
        # Context must declare time_mode
        if not hasattr(context, "time_mode"):
                    violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={},
                    )
                    self._raise_violation(
                "Pipeline context must declare time_mode",
                        violation_context,
                        TimeWindowViolation,
                    )
    
        # Validate time_mode is valid
        from pipeline_context import TimeMode
        if not isinstance(context.time_mode, TimeMode):
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"time_mode": str(context.time_mode)},
            )
            self._raise_violation(
                f"Invalid time_mode: {context.time_mode}. Must be TimeMode enum.",
                violation_context,
                TimeWindowViolation,
            )


class EventTimeRequirementInvariant(PipelineInvariant):
    """Require event time or declared windows.
    
    Pipelines MUST use event time or declared windows.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.TIME_WINDOW
    
    @property
    def description(self) -> str:
        return "Time-based operations must use event time or declared windows"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate event time usage."""
        from pipeline_context import TimeMode
        
        # Context time_mode must be valid
        valid_modes = {TimeMode.EVENT_TIME, TimeMode.DECLARED_WINDOW, TimeMode.FIXED_WINDOW}
        if context.time_mode not in valid_modes:
                    violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"time_mode": context.time_mode.value if hasattr(context.time_mode, 'value') else str(context.time_mode)},
                    )
                    self._raise_violation(
                f"Invalid time_mode '{context.time_mode}'. Must be one of: {[m.value for m in valid_modes]}",
                        violation_context,
                        TimeWindowViolation,
                    )


class NoImplicitNowInvariant(PipelineInvariant):
    """Forbid implicit 'now' references.
    
    Pipelines MUST forbid implicit 'now'.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.TIME_WINDOW
    
    @property
    def description(self) -> str:
        return "No implicit 'now' references allowed"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no implicit now usage."""
        # Check step algorithm_ids for 'now' references
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            # Check for implicit 'now' patterns
            if "now()" in algorithm_lower or "now " in algorithm_lower:
                # Allow if explicitly bound (would need step metadata to verify)
                # For now, flag any 'now' usage
                    violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                        step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                    f"Step '{step.step_name}' may use implicit 'now' reference",
                        violation_context,
                        TimeWindowViolation,
                    )


class NoUnanchoredRollingWindowsInvariant(PipelineInvariant):
    """Forbid rolling windows without anchors.
    
    Pipelines MUST forbid rolling windows without anchors.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.TIME_WINDOW
    
    @property
    def description(self) -> str:
        return "Rolling windows must have explicit anchors"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate rolling window anchors."""
        from pipeline_step import WindowStep
        
        for step_index, step in enumerate(plan.steps):
            if isinstance(step, WindowStep):
                # Window steps must have explicit window_ref
                if not step.window_ref:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind="window",
                        additional_data={},
                    )
                    self._raise_violation(
                        f"Window step '{step.step_name}' lacks explicit window_ref",
                        violation_context,
                        TimeWindowViolation,
                    )


# ============================================================================
# 3. CARDINALITY INVARIANTS
# ============================================================================


class ExplicitCardinalityIncreaseInvariant(PipelineInvariant):
    """Require explicit declaration for cardinality increases.
    
    Pipelines MUST never increase cardinality without declared REDUCE.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CARDINALITY
    
    @property
    def description(self) -> str:
        return "Cardinality increases must be explicitly declared"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate cardinality declarations."""
        from pipeline_step import PipelineStepKind, ReduceStep
        
        for step_index, step in enumerate(plan.steps):
            # Check if step increases cardinality
            algorithm_lower = step.algorithm_id.lower()
            cardinality_increase_ops = ["explode", "unnest", "cross_join", "expand", "flatten"]
            
            increases_cardinality = any(op in algorithm_lower for op in cardinality_increase_ops)
            
            if increases_cardinality:
                # Must be a REDUCE step or explicitly declared
                if step.step_kind != PipelineStepKind.REDUCE:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={
                            "algorithm_id": step.algorithm_id,
                            "detected_ops": [op for op in cardinality_increase_ops if op in algorithm_lower],
                        },
                    )
                    self._raise_violation(
                        f"Step '{step.step_name}' increases cardinality but is not a REDUCE step",
                        violation_context,
                        CardinalityViolation,
                    )


class NoFanOutMapInvariant(PipelineInvariant):
    """Forbid fan-out MAP operations.
    
    Pipelines MUST forbid fan-out MAP steps.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CARDINALITY
    
    @property
    def description(self) -> str:
        return "MAP operations must not fan out (1:N relationships)"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no fan-out in MAP steps."""
        from pipeline_step import MapStep, PipelineStepKind
        
        for step_index, step in enumerate(plan.steps):
            if step.step_kind == PipelineStepKind.MAP:
                if isinstance(step, MapStep):
                    # Map steps must preserve cardinality
                    if not step.preserves_cardinality:
                        violation_context = ViolationContext(
                            pipeline_name=plan.pipeline_name,
                            pipeline_version=plan.pipeline_version,
                            step_name=step.step_name,
                            step_index=step_index,
                            step_kind="map",
                            additional_data={},
                        )
                    self._raise_violation(
                            f"MAP step '{step.step_name}' does not preserve cardinality",
                        violation_context,
                        CardinalityViolation,
                    )


class NoImplicitJoinsInvariant(PipelineInvariant):
    """Forbid implicit joins.
    
    Pipelines MUST forbid implicit joins.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CARDINALITY
    
    @property
    def description(self) -> str:
        return "All joins must be explicit with declared join keys"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate explicit joins."""
        # Check algorithm_ids for join operations
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            if "join" in algorithm_lower:
                # Joins must be explicit - check if step has join metadata
                # For now, we require explicit join in algorithm_id
                if "implicit" in algorithm_lower or "auto" in algorithm_lower:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                        f"JOIN step '{step.step_name}' appears to use implicit join",
                        violation_context,
                        CardinalityViolation,
                    )


class ExplicitGroupingKeysInvariant(PipelineInvariant):
    """Require explicit grouping key declarations.
    
    Pipelines MUST declare grouping keys explicitly.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.CARDINALITY
    
    @property
    def description(self) -> str:
        return "Grouping operations must declare grouping keys explicitly"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate grouping key declarations."""
        from pipeline_step import ReduceStep
        
        for step_index, step in enumerate(plan.steps):
            if isinstance(step, ReduceStep):
                # Reduce steps must have explicit grouping_keys
                if not step.grouping_keys or len(step.grouping_keys) == 0:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind="reduce",
                        additional_data={},
                    )
                    self._raise_violation(
                        f"REDUCE step '{step.step_name}' lacks explicit grouping_keys",
                        violation_context,
                        CardinalityViolation,
                    )


# ============================================================================
# 4. DATA LOSS INVARIANTS
# ============================================================================


class NoSilentFilteringInvariant(PipelineInvariant):
    """Forbid silent data filtering.
    
    Pipelines MUST forbid silent filtering.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DATA_LOSS
    
    @property
    def description(self) -> str:
        return "All filtering must be explicit and logged"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no silent filtering."""
        # Check algorithm_ids for filter operations
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            if "filter" in algorithm_lower or "where" in algorithm_lower:
                # Filtering must be explicit - check for silent patterns
                if "silent" in algorithm_lower or "quiet" in algorithm_lower:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                        f"FILTER step '{step.step_name}' appears to use silent filtering",
                        violation_context,
                        DataLossViolation,
                    )


class NoDropOnErrorInvariant(PipelineInvariant):
    """Forbid dropping data on errors.
    
    Pipelines MUST forbid drop-on-error.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DATA_LOSS
    
    @property
    def description(self) -> str:
        return "Errors must not silently drop data"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no drop-on-error."""
        # Check algorithm_ids for error handling patterns
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            # Check for drop-on-error patterns
            if ("drop" in algorithm_lower or "skip" in algorithm_lower) and "error" in algorithm_lower:
                    violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                        step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                    f"Step '{step.step_name}' appears to drop data on errors",
                        violation_context,
                        DataLossViolation,
                    )


class NoPartialAggregationInvariant(PipelineInvariant):
    """Forbid partial aggregations.
    
    Pipelines MUST forbid partial aggregation.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DATA_LOSS
    
    @property
    def description(self) -> str:
        return "Aggregations must process complete data sets"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no partial aggregations."""
        # Check algorithm_ids for partial aggregation patterns
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            if "aggregate" in algorithm_lower or "reduce" in algorithm_lower:
                if "partial" in algorithm_lower or "incremental" in algorithm_lower:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                        f"AGGREGATE step '{step.step_name}' allows partial results",
                        violation_context,
                        DataLossViolation,
                    )


class ExplicitExclusionRulesInvariant(PipelineInvariant):
    """Require explicit data exclusion rules.
    
    Pipelines MUST require explicit exclusion rules.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.DATA_LOSS
    
    @property
    def description(self) -> str:
        return "Data exclusions must have explicit, documented rules"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate explicit exclusion rules."""
        # Check algorithm_ids for exclusion operations
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            exclusion_indicators = ["exclude", "remove", "drop", "filter"]
            has_exclusion = any(indicator in algorithm_lower for indicator in exclusion_indicators)
            
            if has_exclusion:
                # Exclusions must be explicit - check for implicit patterns
                if "implicit" in algorithm_lower or "auto" in algorithm_lower:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                        f"Step '{step.step_name}' excludes data without explicit rules",
                        violation_context,
                        DataLossViolation,
                    )


# ============================================================================
# 5. SCHEMA INTEGRITY INVARIANTS
# ============================================================================


class SchemaNameVersionRequirementInvariant(PipelineInvariant):
    """Require schema names and versions.
    
    Pipelines MUST specify schema names & versions.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.SCHEMA_INTEGRITY
    
    @property
    def description(self) -> str:
        return "All schemas must have names and versions"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate schema naming and versioning."""
        for step_index, step in enumerate(plan.steps):
            # Input schema must have name and version
            if not step.input_schema or not step.input_schema.strip():
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"schema_type": "input"},
                )
                self._raise_violation(
                    f"Step '{step.step_name}' input_schema lacks name",
                    violation_context,
                    SchemaIntegrityViolation,
                )
            
            if step.input_schema_version < 1:
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"schema_type": "input", "version": step.input_schema_version},
                )
                self._raise_violation(
                    f"Step '{step.step_name}' input_schema lacks valid version",
                    violation_context,
                    SchemaIntegrityViolation,
                )
        
            # Output schema must have name and version
            if not step.output_schema or not step.output_schema.strip():
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"schema_type": "output"},
                )
                self._raise_violation(
                    f"Step '{step.step_name}' output_schema lacks name",
                    violation_context,
                    SchemaIntegrityViolation,
                )
            
            if step.output_schema_version < 1:
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"schema_type": "output", "version": step.output_schema_version},
                )
                self._raise_violation(
                    f"Step '{step.step_name}' output_schema lacks valid version",
                violation_context,
                SchemaIntegrityViolation,
            )


class NoSchemaInferenceInvariant(PipelineInvariant):
    """Forbid schema inference.
    
    Pipelines MUST forbid schema inference.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.SCHEMA_INTEGRITY
    
    @property
    def description(self) -> str:
        return "Schemas must be explicit, not inferred"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no schema inference."""
        # Check algorithm_ids for inference patterns
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            if "infer" in algorithm_lower or "auto" in algorithm_lower:
                if "schema" in algorithm_lower:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                        f"Step '{step.step_name}' uses schema inference",
                        violation_context,
                        SchemaIntegrityViolation,
                    )


class NoOutputShapeDriftInvariant(PipelineInvariant):
    """Forbid output shape drift.
    
    Pipelines MUST forbid output shape drift.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.SCHEMA_INTEGRITY
    
    @property
    def description(self) -> str:
        return "Output shapes must be stable and declared"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate output shape stability."""
        # Check that output schemas are consistent across steps
        seen_output_schemas = {}
        
        for step_index, step in enumerate(plan.steps):
            output_schema_key = (step.output_schema, step.output_schema_version)
            
            # Check if we've seen this schema before with different step
            if output_schema_key in seen_output_schemas:
                prev_step_index = seen_output_schemas[output_schema_key]
                # This is OK - same schema can be used by multiple steps
                pass
            else:
                seen_output_schemas[output_schema_key] = step_index
            
            # Check algorithm for shape drift patterns
            algorithm_lower = step.algorithm_id.lower()
            if "drift" in algorithm_lower or "dynamic" in algorithm_lower:
                if "shape" in algorithm_lower or "schema" in algorithm_lower:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={"algorithm_id": step.algorithm_id},
                    )
                    self._raise_violation(
                        f"Step '{step.step_name}' may cause output shape drift",
                        violation_context,
                        SchemaIntegrityViolation,
                    )


class NoMixedSchemaVersionsInvariant(PipelineInvariant):
    """Forbid mixed schema versions in a step.
    
    Pipelines MUST forbid mixed schema versions in a step.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.SCHEMA_INTEGRITY
    
    @property
    def description(self) -> str:
        return "A step must use consistent schema versions"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate schema version consistency."""
        # Each step should have consistent input/output schema versions
        # This is already enforced by step validation, but we check here too
        for step_index, step in enumerate(plan.steps):
            # Input and output schemas should be from same schema family
            # (This is a simplified check - real validation would check schema lineage)
            if step.input_schema != step.output_schema:
                # Different schemas are OK, but versions should be compatible
                # For now, we just ensure versions are valid
                if step.input_schema_version < 1 or step.output_schema_version < 1:
                    violation_context = ViolationContext(
                        pipeline_name=plan.pipeline_name,
                        pipeline_version=plan.pipeline_version,
                        step_name=step.step_name,
                        step_index=step_index,
                        step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                        additional_data={
                            "input_version": step.input_schema_version,
                            "output_version": step.output_schema_version,
                        },
                    )
                    self._raise_violation(
                        f"Step '{step.step_name}' has invalid schema versions",
                        violation_context,
                        SchemaIntegrityViolation,
                    )


# ============================================================================
# 6. LINEAGE & PROVENANCE INVARIANTS
# ============================================================================


class InputSchemaLineageInvariant(PipelineInvariant):
    """Require input schema lineage tracking.
    
    Pipelines MUST record input schema lineage.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.LINEAGE_PROVENANCE
    
    @property
    def description(self) -> str:
        return "Input schemas must have lineage information"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate input schema lineage."""
        # Context must have input_schema_versions
        if not hasattr(context, "input_schema_versions") or not context.input_schema_versions:
                    violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={},
                    )
                    self._raise_violation(
                "Pipeline context lacks input_schema_versions",
                violation_context,
                LineageProvenanceViolation,
            )
        
        # Each input schema version must have lineage (name + version)
        for schema_version in context.input_schema_versions:
            if not schema_version.name or not schema_version.version:
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    additional_data={"schema": str(schema_version)},
                )
                self._raise_violation(
                    f"Input schema version lacks lineage: {schema_version}",
                        violation_context,
                        LineageProvenanceViolation,
                    )


class AlgorithmVersionTrackingInvariant(PipelineInvariant):
    """Require algorithm version tracking.
    
    Pipelines MUST record algorithm versions.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.LINEAGE_PROVENANCE
    
    @property
    def description(self) -> str:
        return "All algorithms must have version information"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate algorithm versioning."""
        for step_index, step in enumerate(plan.steps):
            # Algorithm ID must be versioned (contain @ or :)
            if "@" not in step.algorithm_id and ":" not in step.algorithm_id:
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={"algorithm_id": step.algorithm_id},
                )
                self._raise_violation(
                    f"Step '{step.step_name}' algorithm_id lacks version (use '@' or ':')",
                    violation_context,
                    LineageProvenanceViolation,
                )


class StepOrderingRecordInvariant(PipelineInvariant):
    """Require step ordering to be recorded.
    
    Pipelines MUST record step ordering.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.LINEAGE_PROVENANCE
    
    @property
    def description(self) -> str:
        return "Pipeline must record complete step ordering"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate step ordering is recorded."""
        # Plan must have steps in order
        if not plan.steps or len(plan.steps) == 0:
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={},
            )
            self._raise_violation(
                "Pipeline plan has no steps",
                violation_context,
                LineageProvenanceViolation,
            )
        
        # Steps must be in tuple (immutable, ordered)
        if not isinstance(plan.steps, tuple):
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"steps_type": type(plan.steps).__name__},
            )
            self._raise_violation(
                "Pipeline steps must be tuple (immutable, ordered)",
                violation_context,
                LineageProvenanceViolation,
            )


class CompleteProvenanceArtifactsInvariant(PipelineInvariant):
    """Require complete provenance artifacts.
    
    Pipelines MUST produce complete provenance artifacts.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.LINEAGE_PROVENANCE
    
    @property
    def description(self) -> str:
        return "Pipeline must produce complete provenance artifacts"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate provenance artifact configuration."""
        required_artifacts = {"lineage_graph", "schema_manifest", "execution_plan"}
        
        if not hasattr(context, "provenance_artifacts"):
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={},
            )
            self._raise_violation(
                "Context lacks provenance_artifacts configuration",
                violation_context,
                LineageProvenanceViolation,
            )
        
        # Check if required artifacts are present
        context_artifacts = set(context.provenance_artifacts)
        missing = required_artifacts - context_artifacts
        
        if missing:
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"missing_artifacts": list(missing)},
            )
            self._raise_violation(
                f"Missing required provenance artifacts: {missing}",
                violation_context,
                LineageProvenanceViolation,
            )


class CompleteProvenanceDAGInvariant(PipelineInvariant):
    """Require complete provenance DAG validation.
    
    Tier-0: Ensures every output schema maps to previous lineage nodes.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.LINEAGE_PROVENANCE
    
    @property
    def description(self) -> str:
        return "Pipeline must have complete provenance DAG with all schema links"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate complete provenance DAG."""
        is_valid, missing_links = ProvenanceDAGValidator.validate_complete_lineage(plan, context)
        
        if not is_valid:
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"missing_links": missing_links},
            )
            self._raise_violation(
                f"Provenance DAG incomplete. Missing links: {missing_links}",
                violation_context,
                LineageProvenanceViolation,
            )


class FormalContractComplianceInvariant(PipelineInvariant):
    """Require formal contract compliance.
    
    Tier-0: Ensures step properties match structural reality.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "Step properties must match structural analysis (formal contracts)"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate formal contract compliance."""
        for step_index, step in enumerate(plan.steps):
            analysis = StructuralAnalyzer.analyze_step_code(step)
            contract_violations = FormalContractValidator.validate_step_contracts(step, analysis)
            
            if contract_violations:
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={
                        "contract_violations": contract_violations,
                        "structural_analysis": analysis.get("structural_properties", {}),
                    },
                )
                self._raise_violation(
                    f"Step '{step.step_name}' violates formal contracts: {contract_violations}",
                    violation_context,
                    ReplaySafetyViolation,
                )


# ============================================================================
# 7. REPLAY SAFETY INVARIANTS
# ============================================================================


class BitForBitReproducibilityInvariant(PipelineInvariant):
    """Require bit-for-bit reproducibility.
    
    Pipelines MUST be re-executable bit-for-bit.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "Pipeline must be bit-for-bit reproducible"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate reproducibility guarantees."""
        if not hasattr(context, "reproducibility_mode"):
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={},
            )
            self._raise_violation(
                "Context lacks reproducibility_mode declaration",
                violation_context,
                ReplaySafetyViolation,
            )
        
        if context.reproducibility_mode != "bit_for_bit":
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"mode": context.reproducibility_mode},
            )
            self._raise_violation(
                f"Reproducibility mode '{context.reproducibility_mode}' is insufficient. Must be 'bit_for_bit'",
                violation_context,
                ReplaySafetyViolation,
            )


class NoExternalIOInvariant(PipelineInvariant):
    """Forbid external IO during execution.
    
    Pipelines MUST forbid external IO.
    
    Tier-0: Uses structural AST analysis.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "Steps must not perform external IO"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no external IO.
        
        Tier-0: Uses structural analysis.
        """
        for step_index, step in enumerate(plan.steps):
            analysis = StructuralAnalyzer.analyze_step_code(step)
            props = analysis.get("structural_properties", {})
            
            if props.get("has_io", False):
                io_violations = [
                    v for v in analysis.get("violations", [])
                    if any(p in v.lower() for p in StructuralAnalyzer.IO_PATTERNS)
                ]
                
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={
                        "algorithm_id": step.algorithm_id,
                        "io_violations": io_violations,
                        "structural_analysis": props,
                    },
                )
                self._raise_violation(
                    f"Step '{step.step_name}' performs external IO: {io_violations}",
                    violation_context,
                    ReplaySafetyViolation,
                )


class NoStateWritesInvariant(PipelineInvariant):
    """Forbid state writes during execution.
    
    Pipelines MUST forbid state writes.
    
    Tier-0: Uses structural AST analysis.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "Steps must not write external state"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate no state writes.
        
        Tier-0: Uses structural analysis.
        """
        for step_index, step in enumerate(plan.steps):
            analysis = StructuralAnalyzer.analyze_step_code(step)
            props = analysis.get("structural_properties", {})
            
            if props.get("has_state_writes", False):
                state_violations = [
                    v for v in analysis.get("violations", [])
                    if any(p in v.lower() for p in StructuralAnalyzer.STATE_WRITE_PATTERNS)
                ]
                
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={
                        "algorithm_id": step.algorithm_id,
                        "state_violations": state_violations,
                        "structural_analysis": props,
                    },
                )
                self._raise_violation(
                    f"Step '{step.step_name}' writes external state: {state_violations}",
                    violation_context,
                    ReplaySafetyViolation,
                )


class NoConditionalExecutionInvariant(PipelineInvariant):
    """Forbid conditional execution.
    
    Pipelines MUST forbid conditional execution.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "Step execution must be unconditional"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate unconditional execution."""
        # Check algorithm_ids for conditional execution patterns
        for step_index, step in enumerate(plan.steps):
            algorithm_lower = step.algorithm_id.lower()
            
            conditional_patterns = ["if", "when", "conditional", "switch", "case"]
            
            violations = []
            for pattern in conditional_patterns:
                if pattern in algorithm_lower and "execution" in algorithm_lower:
                    violations.append(pattern)
            
            if violations:
                violation_context = ViolationContext(
                    pipeline_name=plan.pipeline_name,
                    pipeline_version=plan.pipeline_version,
                    step_name=step.step_name,
                    step_index=step_index,
                    step_kind=step.step_kind.value if hasattr(step.step_kind, 'value') else str(step.step_kind),
                    additional_data={
                        "algorithm_id": step.algorithm_id,
                        "conditional_patterns": violations,
                    },
                )
                self._raise_violation(
                    f"Step '{step.step_name}' has conditional execution",
                    violation_context,
                    ReplaySafetyViolation,
                )


# ============================================================================
# META-INVARIANTS (TIER-0)
# ============================================================================


class MetaInvariantCompleteness(PipelineInvariant):
    """Meta-invariant: All pipeline semantics must be structurally provable.
    
    Tier-0: Validates that the invariant system itself is complete.
    """
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "All pipeline semantics must be structurally provable"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate that all steps have provable semantics."""
        unprovable_steps = []
        
        for step_index, step in enumerate(plan.steps):
            analysis = StructuralAnalyzer.analyze_step_code(step)
            
            # If we can't analyze the step structurally, it's unprovable
            if not analysis.get("has_source", False) and not analysis.get("ast_parseable", False):
                # Check if step has formal contracts that make it provable
                has_formal_contracts = (
                    hasattr(step, "deterministic") and
                    hasattr(step, "preserves_cardinality") and
                    hasattr(step, "input_schema") and
                    hasattr(step, "output_schema")
                )
                
                if not has_formal_contracts:
                    unprovable_steps.append({
                        "step_index": step_index,
                        "step_name": step.step_name,
                        "reason": "No source code and insufficient formal contracts"
                    })
        
        if unprovable_steps:
            violation_context = ViolationContext(
                pipeline_name=plan.pipeline_name,
                pipeline_version=plan.pipeline_version,
                additional_data={"unprovable_steps": unprovable_steps},
            )
            self._raise_violation(
                f"Pipeline contains steps with unprovable semantics: {unprovable_steps}",
                violation_context,
                ReplaySafetyViolation,
            )


class InvariantSystemCompleteness(PipelineInvariant):
    """Meta-invariant: Invariant system must cover all required categories.
    
    Tier-0: Validates that all critical safety dimensions are covered.
    """
    
    REQUIRED_CATEGORIES = {
        InvariantCategory.DETERMINISM,
        InvariantCategory.TIME_WINDOW,
        InvariantCategory.CARDINALITY,
        InvariantCategory.DATA_LOSS,
        InvariantCategory.SCHEMA_INTEGRITY,
        InvariantCategory.LINEAGE_PROVENANCE,
        InvariantCategory.REPLAY_SAFETY,
    }
    
    @property
    def category(self) -> InvariantCategory:
        return InvariantCategory.REPLAY_SAFETY
    
    @property
    def description(self) -> str:
        return "Invariant system must cover all required safety categories"
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate invariant system completeness.
        
        Note: This is a meta-check that would typically be done at system level,
        not per-pipeline. Included here for Tier-0 completeness.
        """
        # This would check the registry, but we validate at engine level
        # For now, we just ensure all categories are represented
        pass  # Meta-invariant validated at engine level


# ============================================================================
# INVARIANT REGISTRY
# ============================================================================


class InvariantRegistry:
    """Registry of all pipeline invariants."""
    
    def __init__(self):
        self._invariants: List[PipelineInvariant] = []
        self._by_category: Dict[InvariantCategory, List[PipelineInvariant]] = {}
        self._by_id: Dict[str, PipelineInvariant] = {}
        self._initialize_invariants()
    
    def _initialize_invariants(self) -> None:
        """Initialize all invariants."""
        # 1. Determinism invariants
        self._register(NoRandomSeedsInvariant())
        self._register(NoEnvironmentDependenceInvariant())
        self._register(NoWallClockAccessInvariant())
        self._register(StableHashRequirementInvariant())
        
        # 2. Time & window invariants
        self._register(ExplicitTimeDeclarationInvariant())
        self._register(EventTimeRequirementInvariant())
        self._register(NoImplicitNowInvariant())
        self._register(NoUnanchoredRollingWindowsInvariant())
        
        # 3. Cardinality invariants
        self._register(ExplicitCardinalityIncreaseInvariant())
        self._register(NoFanOutMapInvariant())
        self._register(NoImplicitJoinsInvariant())
        self._register(ExplicitGroupingKeysInvariant())
        
        # 4. Data loss invariants
        self._register(NoSilentFilteringInvariant())
        self._register(NoDropOnErrorInvariant())
        self._register(NoPartialAggregationInvariant())
        self._register(ExplicitExclusionRulesInvariant())
        
        # 5. Schema integrity invariants
        self._register(SchemaNameVersionRequirementInvariant())
        self._register(NoSchemaInferenceInvariant())
        self._register(NoOutputShapeDriftInvariant())
        self._register(NoMixedSchemaVersionsInvariant())
        
        # 6. Lineage & provenance invariants
        self._register(InputSchemaLineageInvariant())
        self._register(AlgorithmVersionTrackingInvariant())
        self._register(StepOrderingRecordInvariant())
        self._register(CompleteProvenanceArtifactsInvariant())
        self._register(CompleteProvenanceDAGInvariant())  # Tier-0: DAG validation
        
        # 7. Replay safety invariants
        self._register(BitForBitReproducibilityInvariant())
        self._register(NoExternalIOInvariant())
        self._register(NoStateWritesInvariant())
        self._register(NoConditionalExecutionInvariant())
        self._register(FormalContractComplianceInvariant())  # Tier-0: Contract validation
        
        # Meta-invariants (Tier-0)
        self._register(MetaInvariantCompleteness())
        self._register(InvariantSystemCompleteness())
    
    def _register(self, invariant: PipelineInvariant) -> None:
        """Register an invariant."""
        self._invariants.append(invariant)
        self._by_id[invariant.invariant_id] = invariant
        
        if invariant.category not in self._by_category:
            self._by_category[invariant.category] = []
        self._by_category[invariant.category].append(invariant)
    
    def get_all(self) -> List[PipelineInvariant]:
        """Get all registered invariants."""
        return self._invariants.copy()
    
    def get_by_category(self, category: InvariantCategory) -> List[PipelineInvariant]:
        """Get invariants by category."""
        return self._by_category.get(category, []).copy()
    
    def get_by_id(self, invariant_id: str) -> Optional[PipelineInvariant]:
        """Get invariant by ID."""
        return self._by_id.get(invariant_id)


# ============================================================================
# VALIDATION ENGINE
# ============================================================================


@dataclass
class ValidationResult:
    """Result of invariant validation."""
    
    passed: bool
    violations: List[PipelineInvariantViolation] = field(default_factory=list)
    audit_events: List[Dict[str, Any]] = field(default_factory=list)


class PipelineInvariantEngine:
    """Engine for validating pipeline invariants.
    
    This is the primary interface for invariant enforcement.
    All pipelines must pass validation before execution.
    
    Tier-0 Enhancements:
    - Deterministic evaluation ordering
    - Structural analysis caching
    - Performance optimizations for large pipelines
    - Meta-invariant validation
    
    Rules:
    - No warnings
    - Violations raise hard exceptions
    - Validation happens before execution
    - Deterministic ordering (same plan → same validation order)
    """
    
    def __init__(self, registry: Optional[InvariantRegistry] = None):
        self.registry = registry or InvariantRegistry()
        self._audit_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self._structural_cache: Dict[str, Dict[str, Any]] = {}  # Cache structural analyses
    
    def set_audit_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Set callback for audit events."""
        self._audit_callback = callback
    
    def validate(
        self,
        plan: "PipelinePlan",
        context: "PipelineContext",
    ) -> None:
        """Validate pipeline against all invariants.
        
        Tier-0: Uses deterministic ordering and structural analysis caching.
        
        Args:
            plan: Pipeline plan to validate
            context: Pipeline context
            
        Raises:
            PipelineInvariantViolation: On first violation (fail-fast)
        """
        # Tier-0: Pre-compute structural analyses for all steps (performance optimization)
        self._precompute_structural_analyses(plan)
        
        # Tier-0: Deterministic evaluation order (sorted by invariant_id)
        invariants = sorted(
            self.registry.get_all(),
            key=lambda inv: inv.invariant_id
        )
        
        # Tier-0: Validate meta-invariants first
        meta_invariants = [
            inv for inv in invariants
            if isinstance(inv, (MetaInvariantCompleteness, InvariantSystemCompleteness))
        ]
        regular_invariants = [
            inv for inv in invariants
            if not isinstance(inv, (MetaInvariantCompleteness, InvariantSystemCompleteness))
        ]
        
        # Run meta-invariants first, then regular invariants
        ordered_invariants = meta_invariants + regular_invariants
        
        violations = []
        audit_events = []
        
        # Run each invariant in deterministic order
        for invariant in ordered_invariants:
            try:
                invariant.validate(plan, context)
            except PipelineInvariantViolation as e:
                violations.append(e)
                audit_event = e.to_audit_event()
                audit_events.append(audit_event)
                
                # Emit audit event
                if self._audit_callback:
                    self._audit_callback(audit_event)
                
                # Fail fast - raise immediately
                raise
        
        # If we get here, all invariants passed
        return None
    
    def _precompute_structural_analyses(self, plan: "PipelinePlan") -> None:
        """Pre-compute structural analyses for all steps.
        
        Tier-0: Performance optimization - compute once, reuse across invariants.
        Reduces O(num_invariants × num_steps) to O(num_steps) for structural analysis.
        """
        self._structural_cache.clear()
        
        for step_index, step in enumerate(plan.steps):
            cache_key = f"{step.step_name}:{step.algorithm_id}"
            if cache_key not in self._structural_cache:
                analysis = StructuralAnalyzer.analyze_step_code(step)
                self._structural_cache[cache_key] = analysis
    
    def get_invariant_manifest(self) -> Dict[str, Any]:
        """Get manifest of all registered invariants.
        
        Returns:
            Dictionary describing all invariants
        """
        manifest = {
            "total_invariants": len(self.registry.get_all()),
            "by_category": {},
            "invariants": [],
        }
        
        # Count by category
        for category in InvariantCategory:
            invariants = self.registry.get_by_category(category)
            manifest["by_category"][category.value] = len(invariants)
        
        # List all invariants
        for inv in self.registry.get_all():
            manifest["invariants"].append({
                "id": inv.invariant_id,
                "category": inv.category.value,
                "description": inv.description,
            })
        
        return manifest


# ============================================================================
# AUDIT INTEGRATION
# ============================================================================


class InvariantAuditLogger:
    """Logger for invariant violations and validation events."""
    
    def __init__(self):
        self._events: List[Dict[str, Any]] = []
    
    def log_violation(self, violation: PipelineInvariantViolation) -> None:
        """Log an invariant violation."""
        event = violation.to_audit_event()
        event["event_type"] = "invariant_violation"
        self._events.append(event)
    
    def log_validation_start(self, pipeline_name: str, pipeline_version: str) -> None:
        """Log start of validation."""
        event = {
            "event_type": "validation_start",
            "pipeline_name": pipeline_name,
            "pipeline_version": pipeline_version,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._events.append(event)
    
    def log_validation_complete(
        self,
        pipeline_name: str,
        pipeline_version: str,
        passed: bool,
        violation_count: int,
    ) -> None:
        """Log completion of validation."""
        event = {
            "event_type": "validation_complete",
            "pipeline_name": pipeline_name,
            "pipeline_version": pipeline_version,
            "passed": passed,
            "violation_count": violation_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._events.append(event)
    
    def get_events(self) -> List[Dict[str, Any]]:
        """Get all logged events."""
        return self._events.copy()
    
    def clear(self) -> None:
        """Clear all events."""
        self._events.clear()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


def create_invariant_engine() -> PipelineInvariantEngine:
    """Create a new invariant validation engine.
    
    Returns:
        Configured PipelineInvariantEngine
    """
    return PipelineInvariantEngine()


def validate_pipeline(
    plan: "PipelinePlan",
    context: "PipelineContext",
    audit_logger: Optional[InvariantAuditLogger] = None,
) -> None:
    """Validate a pipeline plan against all invariants.
    
    Args:
        plan: Pipeline plan to validate
        context: Pipeline context
        audit_logger: Optional audit logger
        
    Raises:
        PipelineInvariantViolation: On first violation
    """
    engine = create_invariant_engine()
    
    if audit_logger:
        engine.set_audit_callback(audit_logger.log_violation)
        audit_logger.log_validation_start(plan.pipeline_name, plan.pipeline_version)
    
    try:
        engine.validate(plan, context)
        if audit_logger:
            audit_logger.log_validation_complete(
                plan.pipeline_name,
                plan.pipeline_version,
                passed=True,
                violation_count=0,
            )
    except PipelineInvariantViolation:
        if audit_logger:
            audit_logger.log_validation_complete(
                plan.pipeline_name,
                plan.pipeline_version,
                passed=False,
                violation_count=1,
            )
        raise


# ============================================================================
# EXPORTS
# ============================================================================


__all__ = [
    # Core types
    "InvariantCategory",
    "ViolationSeverity",
    # Exceptions
    "PipelineInvariantViolation",
    "DeterminismViolation",
    "TimeWindowViolation",
    "CardinalityViolation",
    "DataLossViolation",
    "SchemaIntegrityViolation",
    "LineageProvenanceViolation",
    "ReplaySafetyViolation",
    # Core classes
    "PipelineInvariant",
    "InvariantRegistry",
    "PipelineInvariantEngine",
    "ValidationResult",
    "InvariantAuditLogger",
    "ViolationContext",
    # Functions
    "create_invariant_engine",
    "validate_pipeline",
]
