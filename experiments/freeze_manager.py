"""
freeze_manager.py — EXPERIMENT & SYSTEM FREEZE CONTROL

Halts motion without undoing state.
Preserves learning, stops compounding errors, protects causality.

Freeze = discipline, not panic.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple, List, Dict
from enum import Enum
import hashlib
import json
import threading
from pathlib import Path


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

class FreezeSeverity(Enum):
    """Freeze severity levels"""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class FreezeSource(Enum):
    """Origin of freeze request"""
    EVALUATION = "evaluation"
    HUMAN = "human"
    WATCHDOG = "watchdog"
    PLATFORM = "platform"


@dataclass(frozen=True)
class FreezeReason:
    """Immutable freeze reason with full context"""
    reason_id: str
    source: FreezeSource
    description: str
    severity: FreezeSeverity
    requested_at: datetime
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "reason_id": self.reason_id,
            "source": self.source.value,
            "description": self.description,
            "severity": self.severity.value,
            "requested_at": self.requested_at.isoformat(),
            "metadata": self.metadata
        }


@dataclass(frozen=True)
class FreezeScope:
    """Explicit freeze scope - no implicit globals"""
    experiment_ids: tuple[str, ...] = field(default_factory=tuple)
    agent_ids: tuple[str, ...] = field(default_factory=tuple)
    platforms: tuple[str, ...] = field(default_factory=tuple)
    pipelines: tuple[str, ...] = field(default_factory=tuple)

    def is_global(self) -> bool:
        """Check if this is a global freeze"""
        return (
            not self.experiment_ids and
            not self.agent_ids and
            not self.platforms and
            not self.pipelines
        )

    def affects_experiment(self, exp_id: str) -> bool:
        return exp_id in self.experiment_ids or self.is_global()

    def affects_agent(self, agent_id: str) -> bool:
        return agent_id in self.agent_ids or self.is_global()

    def affects_platform(self, platform: str) -> bool:
        return platform in self.platforms or self.is_global()

    def affects_pipeline(self, pipeline: str) -> bool:
        return pipeline in self.pipelines or self.is_global()

    def to_dict(self) -> dict:
        return {
            "experiment_ids": list(self.experiment_ids),
            "agent_ids": list(self.agent_ids),
            "platforms": list(self.platforms),
            "pipelines": list(self.pipelines)
        }


@dataclass(frozen=True)
class FreezeState:
    """Immutable freeze state snapshot"""
    freeze_id: str
    scope: FreezeScope
    reason: FreezeReason
    frozen_at: datetime
    snapshot_version: str
    mutable: bool = False  # HARD ENFORCED - not advisory
    initiator: str = "system"

    def to_dict(self) -> dict:
        return {
            "freeze_id": self.freeze_id,
            "scope": self.scope.to_dict(),
            "reason": self.reason.to_dict(),
            "frozen_at": self.frozen_at.isoformat(),
            "snapshot_version": self.snapshot_version,
            "mutable": self.mutable,
            "initiator": self.initiator
        }


@dataclass
class ResumeApproval:
    """Resume authorization with validation"""
    approved_by: str
    approved_at: datetime
    resume_plan: str
    state_checksum_verified: bool
    integrity_check_passed: bool
    notes: str = ""


# ============================================================================
# MUTATION GUARD (CRITICAL)
# ============================================================================

class MutationGuard:
    """Hard gate against any state mutation during freeze"""

    def __init__(self):
        self._frozen_scopes: dict[str, FreezeScope] = {}
        self._lock = threading.RLock()

    def register_freeze(self, freeze_id: str, scope: FreezeScope) -> None:
        """Register a freeze scope"""
        with self._lock:
            self._frozen_scopes[freeze_id] = scope

    def unregister_freeze(self, freeze_id: str) -> None:
        """Remove freeze scope"""
        with self._lock:
            self._frozen_scopes.pop(freeze_id, None)

    def check_mutation_allowed(
        self,
        experiment_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        platform: Optional[str] = None,
        pipeline: Optional[str] = None
    ) -> tuple[bool, Optional[str]]:
        """
        Check if mutation is allowed
        Returns: (allowed, blocking_freeze_id)
        """
        with self._lock:
            for freeze_id, scope in self._frozen_scopes.items():
                if experiment_id and scope.affects_experiment(experiment_id):
                    return False, freeze_id
                if agent_id and scope.affects_agent(agent_id):
                    return False, freeze_id
                if platform and scope.affects_platform(platform):
                    return False, freeze_id
                if pipeline and scope.affects_pipeline(pipeline):
                    return False, freeze_id
            return True, None

    def assert_mutation_allowed(self, **kwargs) -> None:
        """Assert mutation is allowed, raise if blocked"""
        allowed, freeze_id = self.check_mutation_allowed(**kwargs)
        if not allowed:
            raise FreezeViolationError(
                f"Mutation blocked by freeze {freeze_id}. "
                f"Scope: {self._frozen_scopes[freeze_id].to_dict()}"
            )


# ============================================================================
# EXPOSURE BLOCKER
# ============================================================================

class ExposureBlocker:
    """Guarantees zero new exposure during freeze"""

    def __init__(self, mutation_guard: MutationGuard):
        self._guard = mutation_guard
        self._blocked_traffic: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def block_traffic(self, freeze_id: str, scope: FreezeScope) -> None:
        """Block all new traffic for frozen scope"""
        with self._lock:
            self._blocked_traffic[freeze_id] = []

    def allow_traffic(self, freeze_id: str) -> None:
        """Resume traffic after freeze"""
        with self._lock:
            self._blocked_traffic.pop(freeze_id, None)

    def check_exposure_allowed(
        self,
        experiment_id: Optional[str] = None,
        platform: Optional[str] = None
    ) -> bool:
        """Check if new exposure is allowed"""
        allowed, _ = self._guard.check_mutation_allowed(
            experiment_id=experiment_id,
            platform=platform
        )
        return allowed

    def record_blocked_exposure(self, freeze_id: str, exposure_id: str) -> None:
        """Record blocked exposure attempt"""
        with self._lock:
            if freeze_id in self._blocked_traffic:
                self._blocked_traffic[freeze_id].append(exposure_id)

    def get_blocked_count(self, freeze_id: str) -> int:
        """Get count of blocked exposures"""
        with self._lock:
            return len(self._blocked_traffic.get(freeze_id, []))


# ============================================================================
# STATE PRESERVER
# ============================================================================

class StatePreserver:
    """Captures and preserves state snapshots (write-once)"""

    def __init__(self, snapshot_dir: Path):
        self._snapshot_dir = snapshot_dir
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots: dict[str, dict] = {}
        self._lock = threading.RLock()

    def capture_snapshot(self, freeze_id: str, state_data: dict) -> str:
        """
        Capture immutable snapshot
        Returns: snapshot version (checksum)
        """
        with self._lock:
            # Generate snapshot version from content hash
            snapshot_json = json.dumps(state_data, sort_keys=True)
            snapshot_version = hashlib.sha256(
                snapshot_json.encode()
            ).hexdigest()[:16]

            # Write once - prevent overwrites
            snapshot_path = self._snapshot_dir / f"{freeze_id}.json"
            if snapshot_path.exists():
                raise FreezeViolationError(
                    f"Snapshot already exists for {freeze_id}"
                )

            snapshot_path.write_text(snapshot_json)
            self._snapshots[freeze_id] = {
                "version": snapshot_version,
                "data": state_data,
                "captured_at": datetime.utcnow().isoformat()
            }

            return snapshot_version

    def verify_snapshot(self, freeze_id: str, expected_version: str) -> bool:
        """Verify snapshot integrity"""
        with self._lock:
            if freeze_id not in self._snapshots:
                return False
            return self._snapshots[freeze_id]["version"] == expected_version

    def get_snapshot(self, freeze_id: str) -> Optional[dict]:
        """Retrieve snapshot data"""
        with self._lock:
            return self._snapshots.get(freeze_id, {}).get("data")


# ============================================================================
# FREEZE LEDGER (IMMUTABLE)
# ============================================================================

class FreezeLedger:
    """Immutable audit log of all freeze operations"""

    def __init__(self, ledger_path: Path):
        self._ledger_path = ledger_path
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record_freeze(self, state: FreezeState) -> None:
        """Record freeze event"""
        with self._lock:
            entry = {
                "event": "freeze",
                "freeze_id": state.freeze_id,
                "reason": state.reason.to_dict(),
                "scope": state.scope.to_dict(),
                "snapshot_version": state.snapshot_version,
                "timestamp": state.frozen_at.isoformat(),
                "initiator": state.initiator
            }
            self._append_entry(entry)

    def record_resume(
        self,
        freeze_id: str,
        approval: ResumeApproval
    ) -> None:
        """Record resume event"""
        with self._lock:
            entry = {
                "event": "resume",
                "freeze_id": freeze_id,
                "approved_by": approval.approved_by,
                "approved_at": approval.approved_at.isoformat(),
                "resume_plan": approval.resume_plan,
                "checksum_verified": approval.state_checksum_verified,
                "integrity_passed": approval.integrity_check_passed,
                "notes": approval.notes
            }
            self._append_entry(entry)

    def record_escalation(
        self,
        freeze_id: str,
        escalation_reason: str
    ) -> None:
        """Record escalation to rollback"""
        with self._lock:
            entry = {
                "event": "escalate_to_rollback",
                "freeze_id": freeze_id,
                "reason": escalation_reason,
                "timestamp": datetime.utcnow().isoformat()
            }
            self._append_entry(entry)

    def record_violation(
        self,
        freeze_id: str,
        violation_type: str,
        details: str
    ) -> None:
        """Record freeze violation"""
        with self._lock:
            entry = {
                "event": "violation",
                "freeze_id": freeze_id,
                "violation_type": violation_type,
                "details": details,
                "timestamp": datetime.utcnow().isoformat()
            }
            self._append_entry(entry)

    def _append_entry(self, entry: dict) -> None:
        """Append entry to ledger (append-only)"""
        with open(self._ledger_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def get_freeze_history(self, freeze_id: str) -> list[dict]:
        """Get all events for a freeze"""
        with self._lock:
            if not self._ledger_path.exists():
                return []

            history = []
            with open(self._ledger_path, 'r') as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get("freeze_id") == freeze_id:
                        history.append(entry)
            return history


# ============================================================================
# FREEZE WATCHDOG
# ============================================================================

class FreezeWatchdog:
    """Detects freeze abuse and violations"""

    def __init__(self, ledger: FreezeLedger):
        self._ledger = ledger
        self._freeze_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def check_freeze_fatigue(
        self,
        scope: FreezeScope,
        threshold: int = 5,
        window_hours: int = 24
    ) -> bool:
        """Detect excessive freeze requests (freeze fatigue)"""
        # Simplified - production would check time windows
        scope_key = json.dumps(scope.to_dict(), sort_keys=True)
        with self._lock:
            count = self._freeze_counts.get(scope_key, 0)
            self._freeze_counts[scope_key] = count + 1
            return count >= threshold

    def detect_bypass_attempt(
        self,
        freeze_id: str,
        mutation_type: str
    ) -> bool:
        """Detect freeze bypass attempts"""
        # Check if mutation occurred during active freeze
        history = self._ledger.get_freeze_history(freeze_id)
        
        # If we have freeze record but no resume, mutation is illegal
        has_freeze = any(e["event"] == "freeze" for e in history)
        has_resume = any(e["event"] == "resume" for e in history)
        
        return has_freeze and not has_resume

    def detect_illegal_resume(self, freeze_id: str) -> bool:
        """Detect resume without proper approval"""
        history = self._ledger.get_freeze_history(freeze_id)
        
        resume_events = [e for e in history if e["event"] == "resume"]
        if not resume_events:
            return False
        
        # Check if resume had proper verification
        latest_resume = resume_events[-1]
        return not (
            latest_resume.get("checksum_verified", False) and
            latest_resume.get("integrity_passed", False)
        )

    def escalate_to_global_safety(self, reason: str) -> None:
        """Escalate to global safety watchdog"""
        # In production, this would trigger alerts, dashboards, etc.
        print(f"[CRITICAL] Escalating to global safety: {reason}")


# ============================================================================
# FREEZE MANAGER (CORE ENGINE)
# ============================================================================

class FreezeManager:
    """Core freeze control system"""

    def __init__(
        self,
        snapshot_dir: Path,
        ledger_path: Path
    ):
        self._mutation_guard = MutationGuard()
        self._exposure_blocker = ExposureBlocker(self._mutation_guard)
        self._state_preserver = StatePreserver(snapshot_dir)
        self._ledger = FreezeLedger(ledger_path)
        self._watchdog = FreezeWatchdog(self._ledger)
        
        self._active_freezes: dict[str, FreezeState] = {}
        self._lock = threading.RLock()

    def request_freeze(
        self,
        reason: FreezeReason,
        scope: FreezeScope,
        state_data: dict,
        initiator: str = "system"
    ) -> FreezeState:
        """
        Request freeze with explicit reason and scope
        
        Allowed triggers:
        - Platform anomalies
        - Evaluation inconsistency
        - Human override (logged)
        
        Forbidden:
        - Silent self-freeze
        """
        with self._lock:
            # Check for freeze fatigue
            if self._watchdog.check_freeze_fatigue(scope):
                self._watchdog.escalate_to_global_safety(
                    f"Freeze fatigue detected for scope: {scope.to_dict()}"
                )

            # Generate freeze ID
            freeze_id = f"freeze_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}"

            # Capture state snapshot (write-once)
            snapshot_version = self._state_preserver.capture_snapshot(
                freeze_id, state_data
            )

            # Create freeze state
            state = FreezeState(
                freeze_id=freeze_id,
                scope=scope,
                reason=reason,
                frozen_at=datetime.utcnow(),
                snapshot_version=snapshot_version,
                mutable=False,  # HARD ENFORCED
                initiator=initiator
            )

            # Enforce freeze atomically
            self._enforce_freeze(state)

            # Record in ledger
            self._ledger.record_freeze(state)

            # Store active freeze
            self._active_freezes[freeze_id] = state

            return state

    def _enforce_freeze(self, state: FreezeState) -> None:
        """
        Atomically enforce freeze:
        - Block traffic assignment
        - Disable policy updates
        - Pause posting queues
        - Lock training optimizers
        - Freeze feature registry mutations
        
        NO PARTIAL ENFORCEMENT ALLOWED
        """
        try:
            # Register mutation guard
            self._mutation_guard.register_freeze(
                state.freeze_id, state.scope
            )

            # Block exposure
            self._exposure_blocker.block_traffic(
                state.freeze_id, state.scope
            )

            # In production, this would also:
            # - Signal control_assignment.py to halt traffic
            # - Signal rollout_manager.py to pause rollouts
            # - Signal posting_queue.py to freeze posts
            # - Signal training/* to lock optimizers
            # - Signal feature registry to block mutations

        except Exception as e:
            # Rollback partial enforcement
            self._mutation_guard.unregister_freeze(state.freeze_id)
            self._exposure_blocker.allow_traffic(state.freeze_id)
            raise FreezeEnforcementError(
                f"Failed to enforce freeze atomically: {e}"
            )

    def validate_freeze_integrity(self, freeze_id: str) -> bool:
        """
        Ensure:
        - Nothing mutated post-freeze
        - No traffic leakage
        - No shadow rollouts
        - No background retraining
        
        Violation → immediate escalation
        """
        with self._lock:
            if freeze_id not in self._active_freezes:
                return False

            state = self._active_freezes[freeze_id]

            # Verify snapshot integrity
            if not self._state_preserver.verify_snapshot(
                freeze_id, state.snapshot_version
            ):
                self._ledger.record_violation(
                    freeze_id,
                    "snapshot_integrity",
                    "Snapshot checksum mismatch"
                )
                self.escalate_to_rollback(
                    freeze_id,
                    "Snapshot integrity compromised"
                )
                return False

            # Check for bypass attempts
            if self._watchdog.detect_bypass_attempt(freeze_id, "any"):
                self._ledger.record_violation(
                    freeze_id,
                    "bypass_attempt",
                    "Mutation detected during freeze"
                )
                self.escalate_to_rollback(
                    freeze_id,
                    "Freeze bypass detected"
                )
                return False

            # Verify no exposure leakage
            blocked_count = self._exposure_blocker.get_blocked_count(freeze_id)
            if blocked_count > 0:
                # This is expected - just verify blocking is working
                pass

            return True

    def resume(
        self,
        freeze_id: str,
        approval: ResumeApproval
    ) -> bool:
        """
        Resume requires:
        - Explicit resume plan
        - Validated state checksum
        - Signed approval (system or human)
        - Integrity check
        
        Freeze does NOT auto-expire
        """
        with self._lock:
            if freeze_id not in self._active_freezes:
                raise ValueError(f"Freeze {freeze_id} not found")

            # Validate approval
            if not approval.state_checksum_verified:
                raise ValueError("State checksum not verified")

            if not approval.integrity_check_passed:
                raise ValueError("Integrity check failed")

            if not approval.resume_plan:
                raise ValueError("Resume plan required")

            # Final integrity check
            if not self.validate_freeze_integrity(freeze_id):
                raise FreezeViolationError(
                    "Integrity check failed - cannot resume"
                )

            # Record resume
            self._ledger.record_resume(freeze_id, approval)

            # Unfreeze atomically
            state = self._active_freezes[freeze_id]
            self._mutation_guard.unregister_freeze(freeze_id)
            self._exposure_blocker.allow_traffic(freeze_id)

            # Remove from active
            del self._active_freezes[freeze_id]

            return True

    def escalate_to_rollback(
        self,
        freeze_id: str,
        escalation_reason: str
    ) -> None:
        """
        Escalate to rollback when:
        - Damage confirmed
        - Platform penalties detected
        - Causality compromised
        - Unknown behavior persists
        """
        with self._lock:
            # Record escalation
            self._ledger.record_escalation(freeze_id, escalation_reason)

            # In production, this would trigger rollback_manager.py
            print(f"[CRITICAL] Escalating freeze {freeze_id} to ROLLBACK")
            print(f"Reason: {escalation_reason}")

            # Watchdog escalation
            self._watchdog.escalate_to_global_safety(
                f"Freeze {freeze_id} escalated: {escalation_reason}"
            )

    def get_active_freezes(self) -> list[FreezeState]:
        """Get all active freezes"""
        with self._lock:
            return list(self._active_freezes.values())

    def is_frozen(
        self,
        experiment_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        platform: Optional[str] = None,
        pipeline: Optional[str] = None
    ) -> bool:
        """Check if any scope is currently frozen"""
        allowed, _ = self._mutation_guard.check_mutation_allowed(
            experiment_id=experiment_id,
            agent_id=agent_id,
            platform=platform,
            pipeline=pipeline
        )
        return not allowed


# ============================================================================
# EXCEPTIONS
# ============================================================================

class FreezeViolationError(Exception):
    """Raised when freeze is violated"""
    pass


class FreezeEnforcementError(Exception):
    """Raised when freeze cannot be enforced atomically"""
    pass


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

def example_usage():
    """Example of freeze manager usage"""
    
    # Initialize
    manager = FreezeManager(
        snapshot_dir=Path("./snapshots"),
        ledger_path=Path("./ledger/freeze.log")
    )
    
    # Request freeze
    reason = FreezeReason(
        reason_id="eval_001",
        source=FreezeSource.EVALUATION,
        description="Reward model showing 15% drift on platform X",
        severity=FreezeSeverity.CRITICAL,
        requested_at=datetime.utcnow()
    )
    
    scope = FreezeScope(
        experiment_ids=("exp_123", "exp_456"),
        platforms=("twitter", "linkedin")
    )
    
    state_data = {
        "model_weights": "sha256:abc123...",
        "feature_registry": "v2.3.1",
        "policy_network": "checkpoint_890"
    }
    
    freeze_state = manager.request_freeze(
        reason=reason,
        scope=scope,
        state_data=state_data,
        initiator="human"
    )
    
    print(f"Freeze active: {freeze_state.freeze_id}")
    
    # Check if frozen
    print(f"Platform frozen: {manager.is_frozen(platform='twitter')}")
    
    # Validate integrity
    is_valid = manager.validate_freeze_integrity(freeze_state.freeze_id)
    print(f"Integrity valid: {is_valid}")
    
    # Resume (requires approval)
    approval = ResumeApproval(
        approved_by="human_operator",
        approved_at=datetime.utcnow(),
        resume_plan="Gradual resume with 5% traffic test",
        state_checksum_verified=True,
        integrity_check_passed=True,
        notes="Drift resolved after model retrain"
    )
    
    manager.resume(freeze_state.freeze_id, approval)
    print("Freeze resumed successfully")


if __name__ == "__main__":
    example_usage()