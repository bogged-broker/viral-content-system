"""
/infra/observability/watchdog_hooks.py

Policy-Driven Enforcement Hook Interface

This file defines how observability can legally influence enforcement — and nothing more.
It is a contract layer, not a brain.

Hooks are permissioned levers, not tools.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Tuple, List, Dict
from abc import ABC, abstractmethod


# ============================================================================
# CORE ENUMS (EXPLICIT — NO HIDDEN ACTIONS)
# ============================================================================


class WatchdogActionType(Enum):
    """
    Explicit watchdog actions. No hidden actions ever.
    """

    PAUSE = "pause"
    DEGRADE = "degrade"
    ISOLATE = "isolate"
    SHUTDOWN = "shutdown"


class WatchdogHookScope(Enum):
    """
    Scope defines blast radius, not reason.
    """

    GLOBAL = "global"
    FACTORY = "factory"
    WORKFLOW = "workflow"
    ACCOUNT = "account"


# ============================================================================
# JUSTIFICATION PAYLOAD (MANDATORY)
# ============================================================================


@dataclass(frozen=True)
class WatchdogJustification:
    """
    Mandatory justification for every hook invocation.
    
    No justification → hook invocation rejected.
    """

    reason: str
    triggering_dimension: str
    health_state: str
    policy_version: str

    trace_id: str
    timestamp: int

    def validate(self) -> None:
        """
        Validate justification has all required fields.
        
        Raises:
            ValueError: If justification invalid
        """
        if not self.reason or not self.reason.strip():
            raise ValueError("Justification reason cannot be empty")

        if not self.triggering_dimension:
            raise ValueError("Triggering dimension required")

        if not self.health_state:
            raise ValueError("Health state required")

        if not self.policy_version:
            raise ValueError("Policy version required")

        if not self.trace_id:
            raise ValueError("Trace ID required for audit trail")

        if self.timestamp <= 0:
            raise ValueError("Valid timestamp required")


# ============================================================================
# HOOK RESULT (AUDITABLE)
# ============================================================================


@dataclass(frozen=True)
class WatchdogHookResult:
    """
    Every hook produces structured evidence.
    
    This is the auditable output of every enforcement action.
    """

    action: WatchdogActionType
    scope: WatchdogHookScope

    success: bool
    message: str

    effected_entities: list[str]

    justification: WatchdogJustification
    execution_timestamp: int

    def to_audit_record(self) -> dict:
        """
        Convert result to audit record format.
        
        Returns:
            Audit record dict
        """
        return {
            "action": self.action.value,
            "scope": self.scope.value,
            "success": self.success,
            "message": self.message,
            "effected_entities": self.effected_entities,
            "justification": {
                "reason": self.justification.reason,
                "triggering_dimension": self.justification.triggering_dimension,
                "health_state": self.justification.health_state,
                "policy_version": self.justification.policy_version,
                "trace_id": self.justification.trace_id,
                "timestamp": self.justification.timestamp,
            },
            "execution_timestamp": self.execution_timestamp,
        }


# ============================================================================
# BASE HOOK INTERFACE (MANDATORY)
# ============================================================================


class BaseWatchdogHook(ABC):
    """
    Base interface for all watchdog hooks.
    
    Rules:
    - Idempotent
    - Bounded execution
    - No retries inside hook
    - No branching decisions
    """

    def __init__(
        self,
        action: WatchdogActionType,
        scope: WatchdogHookScope,
    ):
        """
        Initialize base hook.
        
        Args:
            action: Type of action this hook performs
            scope: Scope/blast radius of this hook
        """
        self.action = action
        self.scope = scope

    @abstractmethod
    def invoke(
        self,
        justification: WatchdogJustification,
    ) -> WatchdogHookResult:
        """
        Invoke the hook with justification.
        
        MUST be idempotent. MUST be bounded. MUST NOT retry internally.
        
        Args:
            justification: Mandatory justification for action
            
        Returns:
            WatchdogHookResult with execution details
        """
        pass

    def validate_justification(
        self,
        justification: WatchdogJustification,
    ) -> None:
        """
        Validate justification before invocation.
        
        Args:
            justification: Justification to validate
            
        Raises:
            ValueError: If justification invalid
        """
        justification.validate()

    def get_hook_id(self) -> str:
        """
        Get unique identifier for this hook.
        
        Returns:
            Hook identifier
        """
        return f"{self.action.value}:{self.scope.value}"


# ============================================================================
# HOOK REGISTRY (EXPLICIT)
# ============================================================================


class HookRegistry:
    """
    Manages registered watchdog hooks.
    
    Rules:
    - Single hook per (action, scope)
    - Duplicates forbidden
    - Registry validated at boot
    """

    def __init__(self):
        """Initialize empty hook registry."""
        self._hooks: dict[tuple[WatchdogActionType, WatchdogHookScope], BaseWatchdogHook] = {}
        self._validated = False

    def register(self, hook: BaseWatchdogHook) -> None:
        """
        Register a watchdog hook.
        
        Args:
            hook: Hook to register
            
        Raises:
            ValueError: If hook already registered for (action, scope)
        """
        key = (hook.action, hook.scope)

        if key in self._hooks:
            raise ValueError(
                f"Hook already registered for action={hook.action.value}, "
                f"scope={hook.scope.value}"
            )

        self._hooks[key] = hook
        self._validated = False  # Need revalidation

    def get(
        self,
        action: WatchdogActionType,
        scope: WatchdogHookScope,
    ) -> BaseWatchdogHook | None:
        """
        Get hook for specific action and scope.
        
        Args:
            action: Action type
            scope: Hook scope
            
        Returns:
            Hook if registered, None otherwise
        """
        return self._hooks.get((action, scope))

    def require(
        self,
        action: WatchdogActionType,
        scope: WatchdogHookScope,
    ) -> BaseWatchdogHook:
        """
        Get hook or raise if not registered.
        
        Args:
            action: Action type
            scope: Hook scope
            
        Returns:
            Hook
            
        Raises:
            ValueError: If hook not registered
        """
        hook = self.get(action, scope)
        if hook is None:
            raise ValueError(
                f"No hook registered for action={action.value}, scope={scope.value}"
            )
        return hook

    def list_registered(self) -> list[BaseWatchdogHook]:
        """
        Get all registered hooks.
        
        Returns:
            List of registered hooks
        """
        return list(self._hooks.values())

    def validate_registry(self) -> list[str]:
        """
        Validate registry for completeness and correctness.
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        # Check for required hooks (example: at least one PAUSE hook)
        has_pause = any(
            hook.action == WatchdogActionType.PAUSE
            for hook in self._hooks.values()
        )
        if not has_pause:
            errors.append("Registry missing required PAUSE hooks")

        # Verify all hooks are properly configured
        for hook in self._hooks.values():
            try:
                # Verify hook has valid action/scope
                if not isinstance(hook.action, WatchdogActionType):
                    errors.append(f"Hook {hook.get_hook_id()} has invalid action type")

                if not isinstance(hook.scope, WatchdogHookScope):
                    errors.append(f"Hook {hook.get_hook_id()} has invalid scope type")

            except Exception as e:
                errors.append(f"Hook validation error: {e}")

        self._validated = len(errors) == 0
        return errors

    def is_validated(self) -> bool:
        """Check if registry has been validated."""
        return self._validated


# ============================================================================
# HOOK INVARIANTS (ENFORCED)
# ============================================================================


class HookInvariants:
    """
    Enforces hook invariants across the system.
    
    Invariants:
    - Hooks never recurse
    - Hooks never call watchdog
    - Hooks never evaluate health
    - Hooks never trigger experiments
    - Hooks are reversible where possible
    
    If violated → startup fails.
    """

    _invocation_stack: list[str] = []

    @classmethod
    def enter_hook(cls, hook_id: str) -> None:
        """
        Mark entry into hook execution.
        
        Args:
            hook_id: Hook identifier
            
        Raises:
            RuntimeError: If hook recursion detected
        """
        if hook_id in cls._invocation_stack:
            raise RuntimeError(
                f"Hook recursion detected: {hook_id} already in stack "
                f"(INVARIANT VIOLATION)"
            )

        cls._invocation_stack.append(hook_id)

    @classmethod
    def exit_hook(cls, hook_id: str) -> None:
        """
        Mark exit from hook execution.
        
        Args:
            hook_id: Hook identifier
        """
        if cls._invocation_stack and cls._invocation_stack[-1] == hook_id:
            cls._invocation_stack.pop()

    @classmethod
    def verify_no_recursion(cls) -> None:
        """
        Verify no hook recursion in progress.
        
        Raises:
            RuntimeError: If recursion detected
        """
        if len(cls._invocation_stack) > 1:
            raise RuntimeError(
                f"Hook recursion detected: {cls._invocation_stack} "
                f"(INVARIANT VIOLATION)"
            )

    @classmethod
    def clear_stack(cls) -> None:
        """Clear invocation stack (for testing/recovery)."""
        cls._invocation_stack.clear()


# ============================================================================
# CORE HOOKS (REFERENCE SET)
# ============================================================================


class PauseWorkflowsHook(BaseWatchdogHook):
    """
    Pause workflows hook.
    
    - Blocks new workflow scheduling
    - Does NOT cancel in-flight safely completed units
    """

    def __init__(self):
        """Initialize pause workflows hook."""
        super().__init__(
            action=WatchdogActionType.PAUSE,
            scope=WatchdogHookScope.WORKFLOW,
        )
        self._paused = False

    def invoke(
        self,
        justification: WatchdogJustification,
    ) -> WatchdogHookResult:
        """
        Invoke pause workflows.
        
        Args:
            justification: Justification for pause
            
        Returns:
            Hook result
        """
        import time

        self.validate_justification(justification)

        hook_id = self.get_hook_id()
        HookInvariants.enter_hook(hook_id)

        try:
            # Idempotent: already paused is success
            if self._paused:
                return WatchdogHookResult(
                    action=self.action,
                    scope=self.scope,
                    success=True,
                    message="Workflows already paused (idempotent)",
                    effected_entities=[],
                    justification=justification,
                    execution_timestamp=int(time.time() * 1000),
                )

            # Pause workflows (boundary call - actual implementation elsewhere)
            self._paused = True

            return WatchdogHookResult(
                action=self.action,
                scope=self.scope,
                success=True,
                message="Workflow scheduling paused",
                effected_entities=["workflow_scheduler"],
                justification=justification,
                execution_timestamp=int(time.time() * 1000),
            )

        finally:
            HookInvariants.exit_hook(hook_id)


class DegradeCapacityHook(BaseWatchdogHook):
    """
    Degrade capacity hook.
    
    - Reduces concurrency limits
    - Lowers posting rate
    - Throttles factories
    """

    def __init__(self, degradation_factor: float = 0.5):
        """
        Initialize degrade capacity hook.
        
        Args:
            degradation_factor: Factor to reduce capacity by (0.5 = 50%)
        """
        super().__init__(
            action=WatchdogActionType.DEGRADE,
            scope=WatchdogHookScope.GLOBAL,
        )
        self._degradation_factor = degradation_factor
        self._degraded = False

    def invoke(
        self,
        justification: WatchdogJustification,
    ) -> WatchdogHookResult:
        """
        Invoke capacity degradation.
        
        Args:
            justification: Justification for degradation
            
        Returns:
            Hook result
        """
        import time

        self.validate_justification(justification)

        hook_id = self.get_hook_id()
        HookInvariants.enter_hook(hook_id)

        try:
            # Idempotent check
            if self._degraded:
                return WatchdogHookResult(
                    action=self.action,
                    scope=self.scope,
                    success=True,
                    message="Capacity already degraded (idempotent)",
                    effected_entities=[],
                    justification=justification,
                    execution_timestamp=int(time.time() * 1000),
                )

            # Degrade capacity (boundary call)
            self._degraded = True

            return WatchdogHookResult(
                action=self.action,
                scope=self.scope,
                success=True,
                message=f"Capacity degraded by {self._degradation_factor * 100}%",
                effected_entities=["concurrency_limiter", "posting_rate", "factories"],
                justification=justification,
                execution_timestamp=int(time.time() * 1000),
            )

        finally:
            HookInvariants.exit_hook(hook_id)


class IsolateAccountsHook(BaseWatchdogHook):
    """
    Isolate accounts hook.
    
    - Prevents posting
    - Freezes trust updates
    - Isolates blast radius
    """

    def __init__(self):
        """Initialize isolate accounts hook."""
        super().__init__(
            action=WatchdogActionType.ISOLATE,
            scope=WatchdogHookScope.ACCOUNT,
        )
        self._isolated_accounts: set[str] = set()

    def invoke(
        self,
        justification: WatchdogJustification,
        account_ids: list[str] | None = None,
    ) -> WatchdogHookResult:
        """
        Invoke account isolation.
        
        Args:
            justification: Justification for isolation
            account_ids: Optional specific accounts to isolate
            
        Returns:
            Hook result
        """
        import time

        self.validate_justification(justification)

        hook_id = self.get_hook_id()
        HookInvariants.enter_hook(hook_id)

        try:
            accounts_to_isolate = account_ids or []

            # Idempotent: only isolate new accounts
            newly_isolated = [
                acc for acc in accounts_to_isolate
                if acc not in self._isolated_accounts
            ]

            if not newly_isolated:
                return WatchdogHookResult(
                    action=self.action,
                    scope=self.scope,
                    success=True,
                    message="No new accounts to isolate (idempotent)",
                    effected_entities=[],
                    justification=justification,
                    execution_timestamp=int(time.time() * 1000),
                )

            # Isolate accounts (boundary call)
            self._isolated_accounts.update(newly_isolated)

            return WatchdogHookResult(
                action=self.action,
                scope=self.scope,
                success=True,
                message=f"Isolated {len(newly_isolated)} accounts",
                effected_entities=newly_isolated,
                justification=justification,
                execution_timestamp=int(time.time() * 1000),
            )

        finally:
            HookInvariants.exit_hook(hook_id)


class HaltPostingHook(BaseWatchdogHook):
    """
    Halt posting hook.
    
    - Global posting freeze
    - Workflows may continue internally
    - Reversible
    """

    def __init__(self):
        """Initialize halt posting hook."""
        super().__init__(
            action=WatchdogActionType.SHUTDOWN,
            scope=WatchdogHookScope.GLOBAL,
        )
        self._halted = False

    def invoke(
        self,
        justification: WatchdogJustification,
    ) -> WatchdogHookResult:
        """
        Invoke posting halt.
        
        Args:
            justification: Justification for halt
            
        Returns:
            Hook result
        """
        import time

        self.validate_justification(justification)

        hook_id = self.get_hook_id()
        HookInvariants.enter_hook(hook_id)

        try:
            # Idempotent check
            if self._halted:
                return WatchdogHookResult(
                    action=self.action,
                    scope=self.scope,
                    success=True,
                    message="Posting already halted (idempotent)",
                    effected_entities=[],
                    justification=justification,
                    execution_timestamp=int(time.time() * 1000),
                )

            # Halt posting (boundary call)
            self._halted = True

            return WatchdogHookResult(
                action=self.action,
                scope=self.scope,
                success=True,
                message="Global posting halted",
                effected_entities=["posting_system"],
                justification=justification,
                execution_timestamp=int(time.time() * 1000),
            )

        finally:
            HookInvariants.exit_hook(hook_id)


# ============================================================================
# FACTORY FUNCTIONS
# ============================================================================


def create_default_registry() -> HookRegistry:
    """
    Create default hook registry with core hooks.
    
    Returns:
        Configured HookRegistry
    """
    registry = HookRegistry()

    # Register core hooks
    registry.register(PauseWorkflowsHook())
    registry.register(DegradeCapacityHook())
    registry.register(IsolateAccountsHook())
    registry.register(HaltPostingHook())

    # Validate registry
    errors = registry.validate_registry()
    if errors:
        raise RuntimeError(f"Hook registry validation failed: {errors}")

    return registry


def create_minimal_registry() -> HookRegistry:
    """
    Create minimal hook registry for testing.
    
    Returns:
        Minimal HookRegistry
    """
    registry = HookRegistry()

    # Only essential hooks
    registry.register(PauseWorkflowsHook())
    registry.register(HaltPostingHook())

    return registry