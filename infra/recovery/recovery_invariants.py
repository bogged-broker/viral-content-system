"""
recovery_invariants.py

Non-negotiable safety rules for post-failure mutation.
The iron law of recovery - nothing mutates without passing here.

CRITICAL: These invariants are ABSOLUTE and IMMUTABLE.
They outrank all recovery logic except emergency_stop and global invariant_engine.

Mental Model:
  Replay proves truth.
  Rollback restores truth.
  Recovery must not distort truth.

If this file says NO, recovery STOPS. No exceptions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set, Dict, Any, Callable
import time
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# CORE ENUMS
# ============================================================================

class RecoveryPhase(Enum):
    """
    Explicit recovery phases.
    Different invariants apply at different phases.
    """
    PRE_ROLLBACK = "pre_rollback"        # Before any rollback
    POST_ROLLBACK = "post_rollback"      # After rollback, before repair
    REPAIR = "repair"                     # During repair execution
    FINALIZATION = "finalization"         # After repair, resuming normal ops


class InvariantSeverity(Enum):
    """Severity of invariant violations"""
    CRITICAL = "critical"   # Triggers emergency stop
    BLOCKING = "blocking"   # Stops recovery, manual intervention required
    WARNING = "warning"     # Logged but may proceed with approval


# ============================================================================
# EXCEPTIONS
# ============================================================================

class RecoveryInvariantViolation(Exception):
    """
    Raised when a recovery invariant is violated.
    This is a CRITICAL safety event.
    """
    def __init__(
        self,
        invariant_name: str,
        violation_reason: str,
        context: 'RecoveryContext',
        severity: InvariantSeverity = InvariantSeverity.CRITICAL
    ):
        self.invariant_name = invariant_name
        self.violation_reason = violation_reason
        self.context = context
        self.severity = severity
        
        super().__init__(
            f"INVARIANT VIOLATION [{severity.value}]: {invariant_name} - {violation_reason}"
        )


# ============================================================================
# RECOVERY CONTEXT (IMMUTABLE)
# ============================================================================

@dataclass(frozen=True)
class RecoveryContext:
    """
    Immutable context for all recovery operations.
    All invariant checks operate on this context.
    """
    recovery_id: str
    phase: RecoveryPhase
    
    damage_report_id: str
    snapshot_id: Optional[str]
    
    replay_mode: bool
    emergency_clear: bool
    
    affected_subsystems: List[str]
    mutation_targets: List[str]
    
    started_at: int
    
    # Additional context for invariant checks
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """Validate required fields"""
        if not self.recovery_id:
            raise ValueError("recovery_id cannot be empty")
        if not self.damage_report_id:
            raise ValueError("damage_report_id cannot be empty")
        if not self.affected_subsystems:
            raise ValueError("affected_subsystems cannot be empty")


# ============================================================================
# BASE INVARIANT
# ============================================================================

class RecoveryInvariant(ABC):
    """
    Abstract base for recovery invariants.
    
    Each invariant:
    - Has a unique name
    - Has a clear reason for existing
    - Can determine if it applies to a context
    - Can validate the context
    - Emits structured violations
    """
    
    def __init__(self, name: str, reason: str, severity: InvariantSeverity = InvariantSeverity.CRITICAL):
        self.name = name
        self.reason = reason
        self.severity = severity
    
    @abstractmethod
    def applies_to(self, ctx: RecoveryContext) -> bool:
        """Determine if this invariant applies to the given context"""
        pass
    
    @abstractmethod
    def check(self, ctx: RecoveryContext) -> None:
        """
        Validate the invariant.
        
        Raises:
            RecoveryInvariantViolation: If invariant is violated
        """
        pass
    
    def _raise_violation(self, ctx: RecoveryContext, reason: str) -> None:
        """Raise a structured violation"""
        raise RecoveryInvariantViolation(
            invariant_name=self.name,
            violation_reason=reason,
            context=ctx,
            severity=self.severity
        )


# ============================================================================
# GLOBAL RECOVERY INVARIANTS (ALWAYS ENFORCED)
# ============================================================================

class NoTrustIncreaseInvariant(RecoveryInvariant):
    """
    🚫 TRUST & LEGITIMACY
    Recovery may NEVER increase trust scores.
    """
    
    def __init__(self):
        super().__init__(
            name="no_trust_increase",
            reason="Recovery must not inflate trust - violates platform legitimacy",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        """Applies to all phases"""
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no trust-related mutations that increase scores"""
        forbidden_mutations = [
            "trust_score",
            "reputation_increase",
            "privilege_escalation",
            "trust_level_up"
        ]
        
        for target in ctx.mutation_targets:
            if any(forbidden in target.lower() for forbidden in forbidden_mutations):
                self._raise_violation(
                    ctx,
                    f"Attempted to mutate trust-related field: {target}"
                )


class NoEnforcementRemovalInvariant(RecoveryInvariant):
    """
    🚫 TRUST & LEGITIMACY
    Recovery may NEVER remove enforcement flags or clear suppression evidence.
    """
    
    def __init__(self):
        super().__init__(
            name="no_enforcement_removal",
            reason="Removing enforcement flags violates platform safety",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no enforcement flag removal"""
        forbidden_patterns = [
            "remove_flag",
            "clear_suppression",
            "unban",
            "remove_strike",
            "clear_violation"
        ]
        
        for target in ctx.mutation_targets:
            if any(pattern in target.lower() for pattern in forbidden_patterns):
                self._raise_violation(
                    ctx,
                    f"Attempted to remove enforcement: {target}"
                )


class NoHistoricalDeletionInvariant(RecoveryInvariant):
    """
    🚫 HISTORICAL INTEGRITY
    Recovery may NEVER delete audit logs or rewrite historical events.
    """
    
    def __init__(self):
        super().__init__(
            name="no_historical_deletion",
            reason="Historical integrity is immutable - required for auditability",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no historical data deletion"""
        forbidden_operations = [
            "delete_audit",
            "remove_log",
            "clear_history",
            "delete_event",
            "purge_archive"
        ]
        
        for target in ctx.mutation_targets:
            if any(op in target.lower() for op in forbidden_operations):
                self._raise_violation(
                    ctx,
                    f"Attempted to delete historical data: {target}"
                )


class NoExperimentResultModificationInvariant(RecoveryInvariant):
    """
    🚫 HISTORICAL INTEGRITY
    Recovery may NEVER alter experiment results or model outputs already emitted.
    """
    
    def __init__(self):
        super().__init__(
            name="no_experiment_modification",
            reason="Experiment results are immutable facts",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no experiment result modification"""
        for target in ctx.mutation_targets:
            if "experiment_result" in target.lower() or "model_output" in target.lower():
                if ctx.metadata.get("allow_experiment_archive", False):
                    # Archiving is OK, modification is not
                    if "modify" in target.lower() or "update" in target.lower():
                        self._raise_violation(
                            ctx,
                            f"Attempted to modify experiment results: {target}"
                        )
                else:
                    self._raise_violation(
                        ctx,
                        f"Attempted to touch experiment results: {target}"
                    )


class NoPlatformAPIInvariant(RecoveryInvariant):
    """
    🚫 PLATFORM RISK
    Recovery may NEVER post content or trigger platform APIs.
    """
    
    def __init__(self):
        super().__init__(
            name="no_platform_api_calls",
            reason="Recovery must not interact with external platforms - risk of bans",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no platform API calls"""
        forbidden_subsystems = [
            "platform_poster",
            "api_client",
            "content_publisher",
            "account_rotator"
        ]
        
        for subsystem in ctx.affected_subsystems:
            if any(forbidden in subsystem.lower() for forbidden in forbidden_subsystems):
                self._raise_violation(
                    ctx,
                    f"Attempted to interact with platform: {subsystem}"
                )


class NoAccountRotationInvariant(RecoveryInvariant):
    """
    🚫 PLATFORM RISK
    Recovery may NEVER rotate accounts or change IP/device graphs.
    """
    
    def __init__(self):
        super().__init__(
            name="no_account_rotation",
            reason="Account rotation during recovery risks platform detection",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no account rotation"""
        for target in ctx.mutation_targets:
            if any(pattern in target.lower() for pattern in ["rotate_account", "switch_proxy", "change_device"]):
                self._raise_violation(
                    ctx,
                    f"Attempted account rotation: {target}"
                )


class NoTimeManipulationInvariant(RecoveryInvariant):
    """
    🚫 TIME INTEGRITY
    Recovery may NEVER modify clocks, skip logical time, or replay events out of order.
    """
    
    def __init__(self):
        super().__init__(
            name="no_time_manipulation",
            reason="Time integrity required for causality and replay",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no time manipulation"""
        for target in ctx.mutation_targets:
            if any(pattern in target.lower() for pattern in ["modify_clock", "skip_time", "reorder_events"]):
                self._raise_violation(
                    ctx,
                    f"Attempted time manipulation: {target}"
                )
        
        # In replay mode, verify events are ordered
        if ctx.replay_mode and ctx.metadata.get("event_timestamps"):
            timestamps = ctx.metadata["event_timestamps"]
            if timestamps != sorted(timestamps):
                self._raise_violation(
                    ctx,
                    "Events not in chronological order during replay"
                )


class NoScopeEscalationInvariant(RecoveryInvariant):
    """
    🚫 SCOPE ESCALATION
    Recovery may NEVER add new resources or expand blast radius.
    """
    
    def __init__(self):
        super().__init__(
            name="no_scope_escalation",
            reason="Recovery scope must not exceed damage report",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return True
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify mutations stay within declared scope"""
        # Check if mutation targets are subset of affected subsystems
        declared_scope = set(ctx.affected_subsystems)
        
        for target in ctx.mutation_targets:
            # Extract subsystem from target (e.g., "queue_service.queue_123")
            subsystem = target.split(".")[0] if "." in target else target
            
            if subsystem not in declared_scope:
                self._raise_violation(
                    ctx,
                    f"Mutation target '{target}' outside declared scope {declared_scope}"
                )


# ============================================================================
# PHASE-SPECIFIC INVARIANTS
# ============================================================================

class PreRollbackNoMutationInvariant(RecoveryInvariant):
    """
    🔒 PRE_ROLLBACK
    No mutation allowed at all - snapshot resolution only.
    """
    
    def __init__(self):
        super().__init__(
            name="pre_rollback_no_mutation",
            reason="Pre-rollback phase is read-only",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.PRE_ROLLBACK
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no mutations in pre-rollback phase"""
        if ctx.mutation_targets:
            self._raise_violation(
                ctx,
                f"Mutation not allowed in PRE_ROLLBACK phase: {ctx.mutation_targets}"
            )


class PostRollbackReadOnlyInvariant(RecoveryInvariant):
    """
    🔄 POST_ROLLBACK
    Validation reads only - no repair execution yet.
    """
    
    def __init__(self):
        super().__init__(
            name="post_rollback_read_only",
            reason="Post-rollback phase is for validation only",
            severity=InvariantSeverity.BLOCKING
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.POST_ROLLBACK
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify only read operations"""
        forbidden_operations = ["repair", "requeue", "modify", "update", "delete"]
        
        for target in ctx.mutation_targets:
            if any(op in target.lower() for op in forbidden_operations):
                self._raise_violation(
                    ctx,
                    f"Write operation not allowed in POST_ROLLBACK: {target}"
                )


class RepairPhaseWhitelistInvariant(RecoveryInvariant):
    """
    🧠 REPAIR
    Only declared repair actions on whitelisted mutation paths.
    """
    
    def __init__(self):
        super().__init__(
            name="repair_phase_whitelist",
            reason="Repair must use declared strategies only",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.REPAIR
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify mutations are from declared repair strategies"""
        if not ctx.metadata.get("repair_strategy_declared"):
            self._raise_violation(
                ctx,
                "No repair strategy declared for REPAIR phase"
            )
        
        allowed_strategies = ctx.metadata.get("allowed_repair_strategies", [])
        if not allowed_strategies:
            self._raise_violation(
                ctx,
                "No allowed repair strategies defined"
            )


class RepairSnapshotRequiredInvariant(RecoveryInvariant):
    """
    🧠 REPAIR
    Snapshot required before any repair can execute.
    """
    
    def __init__(self):
        super().__init__(
            name="repair_snapshot_required",
            reason="Snapshot required for safe repair",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.REPAIR
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify snapshot exists"""
        if not ctx.snapshot_id:
            self._raise_violation(
                ctx,
                "No snapshot_id provided for REPAIR phase"
            )


class NoConfigVersionChangeInRepairInvariant(RecoveryInvariant):
    """
    🧠 REPAIR
    Config version changes forbidden during repair.
    """
    
    def __init__(self):
        super().__init__(
            name="no_config_version_change",
            reason="Config versions must remain stable during repair",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.REPAIR
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify no config version changes"""
        for target in ctx.mutation_targets:
            if "config_version" in target.lower():
                self._raise_violation(
                    ctx,
                    f"Config version change not allowed during repair: {target}"
                )


class FinalizationRecoveryMarkersRequiredInvariant(RecoveryInvariant):
    """
    🟢 FINALIZATION
    Recovery markers must be preserved during finalization.
    """
    
    def __init__(self):
        super().__init__(
            name="finalization_markers_required",
            reason="Recovery markers must be preserved for audit trail",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.FINALIZATION
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify recovery markers not removed"""
        for target in ctx.mutation_targets:
            if "remove_recovery_marker" in target.lower():
                self._raise_violation(
                    ctx,
                    "Recovery markers cannot be removed"
                )


class NoSilentResumptionInvariant(RecoveryInvariant):
    """
    🟢 FINALIZATION
    System resumption must be explicit and gated.
    """
    
    def __init__(self):
        super().__init__(
            name="no_silent_resumption",
            reason="System resumption must be explicit and audited",
            severity=InvariantSeverity.CRITICAL
        )
    
    def applies_to(self, ctx: RecoveryContext) -> bool:
        return ctx.phase == RecoveryPhase.FINALIZATION
    
    def check(self, ctx: RecoveryContext) -> None:
        """Verify resumption is explicit"""
        if ctx.metadata.get("resuming_subsystems"):
            if not ctx.metadata.get("resumption_approved"):
                self._raise_violation(
                    ctx,
                    "Subsystem resumption not approved"
                )


# ============================================================================
# RECOVERY INVARIANT ENGINE
# ============================================================================

class RecoveryInvariantEngine:
    """
    Validates all recovery operations against invariants.
    
    BEHAVIOR:
    - Evaluates all applicable invariants
    - Stops immediately on first violation
    - Emits audit + safety events
    - Triggers emergency stop if configured
    
    There is NO "continue anyway".
    """
    
    def __init__(self, event_bus, emergency_stop_trigger=None):
        """
        Args:
            event_bus: For emitting violation events
            emergency_stop_trigger: Optional callback to trigger emergency stop
        """
        self.event_bus = event_bus
        self.emergency_stop_trigger = emergency_stop_trigger
        self._invariants: List[RecoveryInvariant] = []
        self._initialize_invariants()
    
    def _initialize_invariants(self) -> None:
        """Initialize all invariants (IMMUTABLE WHITELIST)"""
        # Global invariants (always enforced)
        self._invariants.extend([
            NoTrustIncreaseInvariant(),
            NoEnforcementRemovalInvariant(),
            NoHistoricalDeletionInvariant(),
            NoExperimentResultModificationInvariant(),
            NoPlatformAPIInvariant(),
            NoAccountRotationInvariant(),
            NoTimeManipulationInvariant(),
            NoScopeEscalationInvariant(),
        ])
        
        # Phase-specific invariants
        self._invariants.extend([
            PreRollbackNoMutationInvariant(),
            PostRollbackReadOnlyInvariant(),
            RepairPhaseWhitelistInvariant(),
            RepairSnapshotRequiredInvariant(),
            NoConfigVersionChangeInRepairInvariant(),
            FinalizationRecoveryMarkersRequiredInvariant(),
            NoSilentResumptionInvariant(),
        ])
    
    def validate(self, ctx: RecoveryContext) -> None:
        """
        Validate context against all applicable invariants.
        
        Raises:
            RecoveryInvariantViolation: On first violation (FAIL FAST)
        """
        logger.info(f"Validating recovery context {ctx.recovery_id} in phase {ctx.phase.value}")
        
        applicable_invariants = [
            inv for inv in self._invariants
            if inv.applies_to(ctx)
        ]
        
        logger.debug(f"Checking {len(applicable_invariants)} invariants")
        
        try:
            for invariant in applicable_invariants:
                logger.debug(f"Checking invariant: {invariant.name}")
                invariant.check(ctx)
            
            logger.info(f"✓ All invariants passed for {ctx.recovery_id}")
        
        except RecoveryInvariantViolation as e:
            # Emit high-severity audit event
            self._emit_violation_event(e)
            
            # Trigger emergency stop for CRITICAL violations
            if e.severity == InvariantSeverity.CRITICAL and self.emergency_stop_trigger:
                logger.critical(f"CRITICAL VIOLATION - Triggering emergency stop")
                self.emergency_stop_trigger(e)
            
            # Re-raise to halt recovery
            raise
    
    def _emit_violation_event(self, violation: RecoveryInvariantViolation) -> None:
        """Emit structured violation event"""
        self.event_bus.emit({
            "event": "recovery_invariant_violation",
            "severity": violation.severity.value,
            "invariant": violation.invariant_name,
            "reason": violation.violation_reason,
            "recovery_id": violation.context.recovery_id,
            "phase": violation.context.phase.value,
            "damage_report_id": violation.context.damage_report_id,
            "affected_subsystems": violation.context.affected_subsystems,
            "mutation_targets": violation.context.mutation_targets,
            "timestamp": int(time.time() * 1000)
        })
    
    def list_invariants(self) -> List[str]:
        """List all registered invariants"""
        return [inv.name for inv in self._invariants]
    
    def get_invariant_reason(self, invariant_name: str) -> Optional[str]:
        """Get reason for an invariant"""
        for inv in self._invariants:
            if inv.name == invariant_name:
                return inv.reason
        return None


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Mock event bus
    class MockEventBus:
        def emit(self, event):
            print(f"📡 EVENT: {event['event']} - {event.get('reason', '')}")
    
    # Initialize engine
    event_bus = MockEventBus()
    engine = RecoveryInvariantEngine(event_bus)
    
    print(f"✅ Loaded {len(engine.list_invariants())} invariants\n")
    
    # Test: Valid repair context
    valid_ctx = RecoveryContext(
        recovery_id="rec_001",
        phase=RecoveryPhase.REPAIR,
        damage_report_id="dmg_001",
        snapshot_id="snap_123",
        replay_mode=False,
        emergency_clear=False,
        affected_subsystems=["queue_service"],
        mutation_targets=["queue_service.queue_123"],
        started_at=int(time.time() * 1000),
        metadata={
            "repair_strategy_declared": True,
            "allowed_repair_strategies": ["queue_repair"]
        }
    )
    
    try:
        engine.validate(valid_ctx)
        print("✅ Valid context passed all invariants\n")
    except RecoveryInvariantViolation as e:
        print(f"❌ Unexpected violation: {e}\n")
    
    # Test: Invalid - trust increase
    invalid_ctx = RecoveryContext(
        recovery_id="rec_002",
        phase=RecoveryPhase.REPAIR,
        damage_report_id="dmg_002",
        snapshot_id="snap_124",
        replay_mode=False,
        emergency_clear=False,
        affected_subsystems=["account_service"],
        mutation_targets=["account_service.trust_score"],  # FORBIDDEN
        started_at=int(time.time() * 1000),
        metadata={
            "repair_strategy_declared": True,
            "allowed_repair_strategies": ["account_repair"]
        }
    )
    
    try:
        engine.validate(invalid_ctx)
        print("❌ Should have failed - trust mutation not allowed\n")
    except RecoveryInvariantViolation as e:
        print(f"✅ Correctly blocked: {e}\n")
    
    # Test: Invalid - scope escalation
    scope_violation_ctx = RecoveryContext(
        recovery_id="rec_003",
        phase=RecoveryPhase.REPAIR,
        damage_report_id="dmg_003",
        snapshot_id="snap_125",
        replay_mode=False,
        emergency_clear=False,
        affected_subsystems=["queue_service"],
        mutation_targets=["database_service.table_123"],  # OUTSIDE SCOPE
        started_at=int(time.time() * 1000),
        metadata={
            "repair_strategy_declared": True,
            "allowed_repair_strategies": ["queue_repair"]
        }
    )
    
    try:
        engine.validate(scope_violation_ctx)
        print("❌ Should have failed - scope escalation not allowed\n")
    except RecoveryInvariantViolation as e:
        print(f"✅ Correctly blocked: {e}\n")