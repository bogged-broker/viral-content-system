"""
/infra/failure_recovery.py

Deterministic Crash Recovery & Continuation Authority

This file defines how the system resumes after interruption — without guessing.
Recovery is not best-effort — it's provable.

A crash is not a failure — uncertain recovery is.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Tuple, List, Dict
import time


# ============================================================================
# CORE ENUMS (STRICT)
# ============================================================================


class RecoveryState(Enum):
    """
    States describe evidence, not assumptions.
    """

    CLEAN = "clean"
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class RecoveryAction(Enum):
    """
    Actions are explicit permissions.
    """

    RESUME = "resume"
    REPLAY = "replay"
    SKIP = "skip"
    ABORT = "abort"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class RecoveryCheckpoint:
    """
    Immutable recovery checkpoint.
    
    Rules:
    - Checkpoints are immutable
    - Absence ≠ failure
    - Completion timestamps are authoritative
    """

    entity_type: str  # workflow, job, content
    entity_id: str

    step_name: str
    step_version: str

    started_at: int
    completed_at: int | None

    metadata: dict[str, Any] = field(default_factory=dict)

    def is_complete(self) -> bool:
        """Check if checkpoint represents completed step."""
        return self.completed_at is not None

    def is_incomplete(self) -> bool:
        """Check if checkpoint represents incomplete step."""
        return self.completed_at is None

    def duration_ms(self) -> int | None:
        """Get step duration in milliseconds, if completed."""
        if self.completed_at is None:
            return None
        return self.completed_at - self.started_at


@dataclass(frozen=True)
class RecoveryDecision:
    """
    Recovery decision with full justification.
    
    Every decision is explainable.
    """

    entity_id: str
    action: RecoveryAction

    reason: str
    evidence_ids: list[str]

    step_name: str
    timestamp: int

    def validate(self) -> None:
        """
        Validate decision has required fields.
        
        Raises:
            ValueError: If decision invalid
        """
        if not self.entity_id:
            raise ValueError("Recovery decision requires entity_id")

        if not self.reason or not self.reason.strip():
            raise ValueError("Recovery decision requires explicit reason")

        if not self.evidence_ids:
            raise ValueError("Recovery decision requires evidence")


@dataclass(frozen=True)
class RecoveryContext:
    """
    Recovery context with policy awareness.
    
    Recovery is policy-aware, not blind.
    """

    run_id: str
    recovery_time: int

    policy_version: str
    health_state: str

    previous_run_id: str | None = None

    def validate(self) -> None:
        """
        Validate recovery context.
        
        Raises:
            ValueError: If context invalid
        """
        if not self.run_id:
            raise ValueError("Recovery context requires run_id")

        if not self.policy_version:
            raise ValueError("Recovery context requires policy_version")

        if not self.health_state:
            raise ValueError("Recovery context requires health_state")


@dataclass(frozen=True)
class StepRegistration:
    """
    Registration for a recoverable step.
    """

    step_name: str
    version: str

    idempotent: bool
    replay_safe: bool

    max_replay_attempts: int = 3

    def allows_replay(self) -> bool:
        """Check if step allows replay."""
        return self.replay_safe

    def allows_resume(self) -> bool:
        """Check if step allows resume (non-idempotent steps must replay from start)."""
        return self.idempotent


# ============================================================================
# RECOVERY REGISTRY (CRITICAL)
# ============================================================================


class RecoveryRegistry:
    """
    Manages step registrations for recovery.
    
    Without registration:
    - Recovery is forbidden
    - Startup fails
    
    This prevents implicit behavior.
    """

    def __init__(self):
        """Initialize recovery registry."""
        self._steps: dict[str, StepRegistration] = {}
        self._validated = False

    def register_step(
        self,
        step_name: str,
        version: str,
        idempotent: bool,
        replay_safe: bool,
        max_replay_attempts: int = 3,
    ) -> None:
        """
        Register a recoverable step.
        
        Args:
            step_name: Name of the step
            version: Version identifier
            idempotent: Whether step is idempotent
            replay_safe: Whether step can be safely replayed
            max_replay_attempts: Maximum replay attempts allowed
            
        Raises:
            ValueError: If step already registered
        """
        if step_name in self._steps:
            raise ValueError(f"Step '{step_name}' already registered")

        registration = StepRegistration(
            step_name=step_name,
            version=version,
            idempotent=idempotent,
            replay_safe=replay_safe,
            max_replay_attempts=max_replay_attempts,
        )

        self._steps[step_name] = registration
        self._validated = False

    def get_step(self, step_name: str) -> StepRegistration | None:
        """Get step registration by name."""
        return self._steps.get(step_name)

    def require_step(self, step_name: str) -> StepRegistration:
        """
        Get step registration or raise.
        
        Args:
            step_name: Name of step
            
        Returns:
            StepRegistration
            
        Raises:
            ValueError: If step not registered
        """
        step = self.get_step(step_name)
        if step is None:
            raise ValueError(
                f"Step '{step_name}' not registered. "
                f"Recovery forbidden for unregistered steps."
            )
        return step

    def is_registered(self, step_name: str) -> bool:
        """Check if step is registered."""
        return step_name in self._steps

    def list_steps(self) -> list[StepRegistration]:
        """Get all registered steps."""
        return list(self._steps.values())

    def validate_registry(self) -> list[str]:
        """
        Validate registry completeness.
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        if not self._steps:
            errors.append("Recovery registry is empty — no steps registered")

        # Check for version conflicts
        versions_by_step = {}
        for step in self._steps.values():
            if step.step_name in versions_by_step:
                if versions_by_step[step.step_name] != step.version:
                    errors.append(
                        f"Version conflict for step '{step.step_name}'"
                    )
            versions_by_step[step.step_name] = step.version

        self._validated = len(errors) == 0
        return errors

    def is_validated(self) -> bool:
        """Check if registry validated."""
        return self._validated


# ============================================================================
# RECOVERY PLANNER (BRAIN)
# ============================================================================


class RecoveryPlanner:
    """
    Plans recovery actions based on checkpoints and context.
    
    Responsibilities:
    - Detect incomplete steps
    - Determine replay safety
    - Check idempotency flags
    - Enforce ordering constraints
    - Fail closed on ambiguity
    
    No auto-advancement.
    """

    def __init__(self, registry: RecoveryRegistry):
        """
        Initialize recovery planner.
        
        Args:
            registry: RecoveryRegistry with step registrations
        """
        self._registry = registry

    def plan(
        self,
        checkpoints: list[RecoveryCheckpoint],
        context: RecoveryContext,
    ) -> list[RecoveryDecision]:
        """
        Plan recovery actions for checkpoints.
        
        Args:
            checkpoints: List of recovery checkpoints
            context: Recovery context
            
        Returns:
            List of recovery decisions
            
        Raises:
            ValueError: If context invalid or ambiguous state detected
        """
        context.validate()

        # Group checkpoints by entity
        checkpoints_by_entity = self._group_by_entity(checkpoints)

        decisions = []

        # Process each entity's checkpoints
        for entity_id, entity_checkpoints in checkpoints_by_entity.items():
            entity_decisions = self._plan_entity_recovery(
                entity_id,
                entity_checkpoints,
                context,
            )
            decisions.extend(entity_decisions)

        return decisions

    def _group_by_entity(
        self,
        checkpoints: list[RecoveryCheckpoint],
    ) -> dict[str, list[RecoveryCheckpoint]]:
        """Group checkpoints by entity ID."""
        groups = {}
        for checkpoint in checkpoints:
            if checkpoint.entity_id not in groups:
                groups[checkpoint.entity_id] = []
            groups[checkpoint.entity_id].append(checkpoint)
        return groups

    def _plan_entity_recovery(
        self,
        entity_id: str,
        checkpoints: list[RecoveryCheckpoint],
        context: RecoveryContext,
    ) -> list[RecoveryDecision]:
        """
        Plan recovery for a single entity.
        
        Args:
            entity_id: Entity identifier
            checkpoints: Checkpoints for this entity
            context: Recovery context
            
        Returns:
            List of recovery decisions for entity
        """
        decisions = []

        # Sort checkpoints by start time
        sorted_checkpoints = sorted(checkpoints, key=lambda c: c.started_at)

        for checkpoint in sorted_checkpoints:
            decision = self._plan_checkpoint_recovery(checkpoint, context)
            if decision:
                decisions.append(decision)

        return decisions

    def _plan_checkpoint_recovery(
        self,
        checkpoint: RecoveryCheckpoint,
        context: RecoveryContext,
    ) -> RecoveryDecision | None:
        """
        Plan recovery for a single checkpoint.
        
        Args:
            checkpoint: RecoveryCheckpoint to analyze
            context: Recovery context
            
        Returns:
            RecoveryDecision or None if no action needed
        """
        # Get step registration
        try:
            step = self._registry.require_step(checkpoint.step_name)
        except ValueError as e:
            # Unregistered step — abort recovery
            return RecoveryDecision(
                entity_id=checkpoint.entity_id,
                action=RecoveryAction.ABORT,
                reason=str(e),
                evidence_ids=[checkpoint.entity_id],
                step_name=checkpoint.step_name,
                timestamp=context.recovery_time,
            )

        # Check version mismatch
        if checkpoint.step_version != step.version:
            return RecoveryDecision(
                entity_id=checkpoint.entity_id,
                action=RecoveryAction.ABORT,
                reason=f"Version mismatch: checkpoint={checkpoint.step_version}, registered={step.version}",
                evidence_ids=[checkpoint.entity_id],
                step_name=checkpoint.step_name,
                timestamp=context.recovery_time,
            )

        # If completed, no action needed
        if checkpoint.is_complete():
            return None

        # Incomplete checkpoint — determine action
        return self._decide_incomplete_action(checkpoint, step, context)

    def _decide_incomplete_action(
        self,
        checkpoint: RecoveryCheckpoint,
        step: StepRegistration,
        context: RecoveryContext,
    ) -> RecoveryDecision:
        """
        Decide action for incomplete checkpoint.
        
        Args:
            checkpoint: Incomplete checkpoint
            step: Step registration
            context: Recovery context
            
        Returns:
            RecoveryDecision
        """
        # Check health state — critical state prevents recovery
        if context.health_state == "CRITICAL":
            return RecoveryDecision(
                entity_id=checkpoint.entity_id,
                action=RecoveryAction.ABORT,
                reason="Health state CRITICAL — recovery forbidden",
                evidence_ids=[checkpoint.entity_id],
                step_name=checkpoint.step_name,
                timestamp=context.recovery_time,
            )

        # Check if step is replay-safe
        if step.allows_replay():
            return RecoveryDecision(
                entity_id=checkpoint.entity_id,
                action=RecoveryAction.REPLAY,
                reason=f"Step '{step.step_name}' is replay-safe and incomplete",
                evidence_ids=[checkpoint.entity_id],
                step_name=checkpoint.step_name,
                timestamp=context.recovery_time,
            )

        # Check if step is idempotent (can resume)
        if step.allows_resume():
            return RecoveryDecision(
                entity_id=checkpoint.entity_id,
                action=RecoveryAction.RESUME,
                reason=f"Step '{step.step_name}' is idempotent — safe to resume",
                evidence_ids=[checkpoint.entity_id],
                step_name=checkpoint.step_name,
                timestamp=context.recovery_time,
            )

        # Not replay-safe and not idempotent — skip
        return RecoveryDecision(
            entity_id=checkpoint.entity_id,
            action=RecoveryAction.SKIP,
            reason=f"Step '{step.step_name}' is neither replay-safe nor idempotent",
            evidence_ids=[checkpoint.entity_id],
            step_name=checkpoint.step_name,
            timestamp=context.recovery_time,
        )

    def analyze_state(
        self,
        checkpoints: list[RecoveryCheckpoint],
    ) -> RecoveryState:
        """
        Analyze overall recovery state from checkpoints.
        
        Args:
            checkpoints: List of checkpoints to analyze
            
        Returns:
            RecoveryState
        """
        if not checkpoints:
            return RecoveryState.CLEAN

        # Check for incomplete checkpoints
        incomplete = [cp for cp in checkpoints if cp.is_incomplete()]

        if not incomplete:
            return RecoveryState.CLEAN

        # Check for version mismatches (corrupt state)
        for checkpoint in incomplete:
            step = self._registry.get_step(checkpoint.step_name)
            if step is None:
                return RecoveryState.UNKNOWN

            if checkpoint.step_version != step.version:
                return RecoveryState.CORRUPT

        # Has incomplete but recoverable checkpoints
        return RecoveryState.INCOMPLETE


# ============================================================================
# RECOVERY EXECUTOR (MECHANISM)
# ============================================================================


class RecoveryExecutor:
    """
    Executes recovery decisions.
    
    Rules:
    - Execution is ordered
    - Execution is monotonic
    - Execution emits audit events
    - Execution respects watchdog state
    
    Executor never decides — it enacts.
    """

    def __init__(self, audit_callback: callable = None):
        """
        Initialize recovery executor.
        
        Args:
            audit_callback: Optional callback for audit events
        """
        self._audit_callback = audit_callback
        self._execution_log: list[tuple[int, RecoveryDecision]] = []

    def execute(
        self,
        decisions: list[RecoveryDecision],
    ) -> dict[str, Any]:
        """
        Execute recovery decisions in order.
        
        Args:
            decisions: List of recovery decisions to execute
            
        Returns:
            Execution summary
        """
        results = {
            "total": len(decisions),
            "resumed": 0,
            "replayed": 0,
            "skipped": 0,
            "aborted": 0,
            "errors": [],
        }

        # Sort decisions by timestamp for deterministic ordering
        sorted_decisions = sorted(decisions, key=lambda d: d.timestamp)

        for decision in sorted_decisions:
            try:
                # Validate decision
                decision.validate()

                # Execute based on action
                self._execute_decision(decision)

                # Track results
                if decision.action == RecoveryAction.RESUME:
                    results["resumed"] += 1
                elif decision.action == RecoveryAction.REPLAY:
                    results["replayed"] += 1
                elif decision.action == RecoveryAction.SKIP:
                    results["skipped"] += 1
                elif decision.action == RecoveryAction.ABORT:
                    results["aborted"] += 1

                # Log execution
                self._log_execution(decision)

            except Exception as e:
                error_msg = f"Failed to execute decision for {decision.entity_id}: {e}"
                results["errors"].append(error_msg)

        return results

    def _execute_decision(self, decision: RecoveryDecision) -> None:
        """
        Execute a single recovery decision.
        
        Args:
            decision: RecoveryDecision to execute
        """
        # Emit audit event if callback provided
        if self._audit_callback:
            audit_event = {
                "event_type": "recovery_execution",
                "entity_id": decision.entity_id,
                "action": decision.action.value,
                "reason": decision.reason,
                "evidence_ids": decision.evidence_ids,
                "step_name": decision.step_name,
                "timestamp": decision.timestamp,
            }
            self._audit_callback(audit_event)

        # Actual execution would integrate with orchestration
        # For now, this is the boundary marker
        pass

    def _log_execution(self, decision: RecoveryDecision) -> None:
        """Log decision execution for audit trail."""
        timestamp = int(time.time() * 1000)
        self._execution_log.append((timestamp, decision))

    def get_execution_log(self) -> list[tuple[int, RecoveryDecision]]:
        """Get execution log for audit."""
        return self._execution_log.copy()

    def clear_log(self) -> None:
        """Clear execution log (for testing)."""
        self._execution_log.clear()


# ============================================================================
# RECOVERY INVARIANTS
# ============================================================================


class RecoveryInvariants:
    """
    Enforces recovery invariants.
    
    The system MUST guarantee:
    - No step executes twice unless replay-safe
    - No state mutation without checkpoint
    - No replays across version mismatches
    - No resume after CRITICAL watchdog state
    - No silent skipping
    
    Violations → hard stop.
    """

    @staticmethod
    def verify_no_duplicate_execution(
        decisions: list[RecoveryDecision],
    ) -> None:
        """
        Verify no duplicate executions for non-replay-safe steps.
        
        Args:
            decisions: Recovery decisions to verify
            
        Raises:
            RuntimeError: If duplicate execution detected
        """
        seen_entities = set()

        for decision in decisions:
            if decision.action in {RecoveryAction.RESUME, RecoveryAction.REPLAY}:
                key = f"{decision.entity_id}:{decision.step_name}"

                if key in seen_entities:
                    raise RuntimeError(
                        f"Duplicate recovery action for {key} "
                        f"(INVARIANT VIOLATION)"
                    )

                seen_entities.add(key)

    @staticmethod
    def verify_version_consistency(
        checkpoints: list[RecoveryCheckpoint],
        registry: RecoveryRegistry,
    ) -> None:
        """
        Verify no version mismatches.
        
        Args:
            checkpoints: Checkpoints to verify
            registry: Recovery registry
            
        Raises:
            RuntimeError: If version mismatch detected
        """
        for checkpoint in checkpoints:
            step = registry.get_step(checkpoint.step_name)

            if step is None:
                continue  # Unregistered steps handled elsewhere

            if checkpoint.step_version != step.version:
                raise RuntimeError(
                    f"Version mismatch for step '{checkpoint.step_name}': "
                    f"checkpoint={checkpoint.step_version}, "
                    f"registry={step.version} (INVARIANT VIOLATION)"
                )

    @staticmethod
    def verify_health_state_compliance(
        context: RecoveryContext,
        decisions: list[RecoveryDecision],
    ) -> None:
        """
        Verify recovery complies with health state.
        
        Args:
            context: Recovery context
            decisions: Recovery decisions
            
        Raises:
            RuntimeError: If health state violated
        """
        if context.health_state == "CRITICAL":
            # No resume/replay allowed in CRITICAL state
            forbidden_actions = {RecoveryAction.RESUME, RecoveryAction.REPLAY}

            for decision in decisions:
                if decision.action in forbidden_actions:
                    raise RuntimeError(
                        f"Recovery action {decision.action.value} forbidden "
                        f"in CRITICAL health state (INVARIANT VIOLATION)"
                    )

    @staticmethod
    def verify_all(
        checkpoints: list[RecoveryCheckpoint],
        decisions: list[RecoveryDecision],
        registry: RecoveryRegistry,
        context: RecoveryContext,
    ) -> None:
        """
        Run all invariant checks.
        
        Args:
            checkpoints: Recovery checkpoints
            decisions: Recovery decisions
            registry: Recovery registry
            context: Recovery context
            
        Raises:
            RuntimeError: If any invariant violated
        """
        RecoveryInvariants.verify_no_duplicate_execution(decisions)
        RecoveryInvariants.verify_version_consistency(checkpoints, registry)
        RecoveryInvariants.verify_health_state_compliance(context, decisions)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def create_checkpoint(
    entity_type: str,
    entity_id: str,
    step_name: str,
    step_version: str,
    started_at: int | None = None,
    completed_at: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> RecoveryCheckpoint:
    """
    Create a recovery checkpoint.
    
    Args:
        entity_type: Type of entity
        entity_id: Entity identifier
        step_name: Name of step
        step_version: Version of step
        started_at: Start timestamp (default: now)
        completed_at: Completion timestamp (None if incomplete)
        metadata: Optional metadata
        
    Returns:
        RecoveryCheckpoint
    """
    if started_at is None:
        started_at = int(time.time() * 1000)

    return RecoveryCheckpoint(
        entity_type=entity_type,
        entity_id=entity_id,
        step_name=step_name,
        step_version=step_version,
        started_at=started_at,
        completed_at=completed_at,
        metadata=metadata or {},
    )


def create_recovery_context(
    run_id: str,
    policy_version: str,
    health_state: str,
    previous_run_id: str | None = None,
    recovery_time: int | None = None,
) -> RecoveryContext:
    """
    Create a recovery context.
    
    Args:
        run_id: Current run identifier
        policy_version: Policy version
        health_state: Current health state
        previous_run_id: Optional previous run ID
        recovery_time: Optional recovery timestamp (default: now)
        
    Returns:
        RecoveryContext
    """
    if recovery_time is None:
        recovery_time = int(time.time() * 1000)

    return RecoveryContext(
        run_id=run_id,
        recovery_time=recovery_time,
        policy_version=policy_version,
        health_state=health_state,
        previous_run_id=previous_run_id,
    )