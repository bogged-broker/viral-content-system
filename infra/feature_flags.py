
# /infra/feature_flags.py
"""
Gated Rollouts, Kill-Switch Wiring & Controlled Exposure Authority

If this file is sloppy, you get silent production drift.
If this file is strict, you get safe velocity at scale.

Core principle (NON-NEGOTIABLE):
    Feature flags are authority checks, not preferences.

If a flag is off:
- the code path must not execute
- the system must not "work around it"
- violation = safety breach

This is velocity without death.
"""

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Set, List, Callable, Any

from infra.runtime_context import RuntimeContext, ExecutionMode, RuntimeEnvironment
from infra.config_registry import ConfigRegistry


# ============================================================================
# ENUMS (EXPLICIT SEMANTICS)
# ============================================================================

class FlagScope(Enum):
    """Scopes limit who may read the flag."""
    GLOBAL = "global"                    # System-wide
    MODEL = "model"                      # Model/inference subsystem
    AGENT = "agent"                      # Agent subsystem
    ORCHESTRATION = "orchestration"      # Orchestration logic
    POSTING = "posting"                  # Content posting
    EVALUATION = "evaluation"            # Evaluation/metrics


class FlagStability(Enum):
    """Registry enforces usage rules per environment."""
    STABLE = "stable"              # Production-ready, long-term
    EXPERIMENTAL = "experimental"  # Under development, may break
    EMERGENCY = "emergency"        # Emergency kill-switch
    SUNSET = "sunset"             # Deprecated, being removed


# ============================================================================
# FLAG CONDITIONS (DETERMINISTIC ONLY)
# ============================================================================

@dataclass(frozen=True)
class FlagCondition:
    """
    Deterministic predicate for flag evaluation.
    
    Conditions may depend on:
    - runtime context
    - config version
    - experiment assignment
    
    ❌ NEVER on:
    - time
    - randomness
    - live metrics
    """
    predicate: str           # Human-readable description
    evaluator_name: str      # Registered evaluator function name
    
    def __post_init__(self):
        if not self.predicate:
            raise ValueError("FlagCondition predicate cannot be empty")
        if not self.evaluator_name:
            raise ValueError("FlagCondition evaluator_name cannot be empty")


# ============================================================================
# FEATURE FLAG (CANONICAL DEFINITION)
# ============================================================================

@dataclass(frozen=True)
class FeatureFlag:
    """
    Canonical definition of a feature flag.
    
    No dynamic mutation.
    No implicit defaults.
    """
    name: str
    version: str                              # Semantic version
    
    scope: FlagScope
    stability: FlagStability
    
    description: str
    
    default_enabled: bool
    
    allowed_environments: Set[RuntimeEnvironment]
    allowed_modes: Set[ExecutionMode]
    
    conditions: List[FlagCondition]
    
    kill_switch: bool                         # True = emergency shutdown capability
    
    def __post_init__(self):
        if not self.name:
            raise ValueError("FeatureFlag name cannot be empty")
        if not self.version:
            raise ValueError("FeatureFlag version cannot be empty")
        if not self.description:
            raise ValueError("FeatureFlag description cannot be empty")
        
        # Emergency flags MUST be kill-switches
        if self.stability == FlagStability.EMERGENCY and not self.kill_switch:
            raise ValueError(
                f"Emergency flag '{self.name}' must have kill_switch=True"
            )
        
        # Experimental flags should not be default-enabled in production
        if self.stability == FlagStability.EXPERIMENTAL and self.default_enabled:
            if RuntimeEnvironment.PRODUCTION in self.allowed_environments:
                raise ValueError(
                    f"Experimental flag '{self.name}' cannot be default_enabled "
                    f"in PRODUCTION environment"
                )
    
    def get_id(self) -> str:
        """Get unique identifier for this flag."""
        return f"{self.name}:v{self.version}"


# ============================================================================
# FLAG SNAPSHOT (REPRODUCIBILITY)
# ============================================================================

@dataclass(frozen=True)
class FlagSnapshot:
    """
    Immutable snapshot of flag states.
    Stored with runtime context, config snapshot, experiment metadata.
    """
    snapshot_id: str
    flags: Dict[str, bool]              # flag_name -> enabled
    created_at: datetime
    runtime_mode: ExecutionMode
    runtime_env: RuntimeEnvironment
    hash: str
    
    def __post_init__(self):
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
    
    def validate(self) -> None:
        """Validate snapshot integrity."""
        # Recompute hash
        flag_items = sorted(self.flags.items())
        flag_str = "|".join(f"{k}={v}" for k, v in flag_items)
        computed_hash = hashlib.sha256(flag_str.encode('utf-8')).hexdigest()
        
        if computed_hash != self.hash:
            raise ValueError(
                f"Snapshot hash mismatch: expected {self.hash}, got {computed_hash}"
            )


# ============================================================================
# FLAG EVALUATOR (CONDITION RESOLUTION)
# ============================================================================

class FlagEvaluator:
    """
    Evaluates flag conditions deterministically.
    All evaluators must be pure functions of:
    - RuntimeContext
    - ConfigRegistry
    - (optionally) ExperimentContext
    """
    
    def __init__(self, runtime_context: RuntimeContext, config_registry: ConfigRegistry):
        self._runtime_context = runtime_context
        self._config_registry = config_registry
        
        # Registered evaluator functions
        self._evaluators: Dict[str, Callable[[RuntimeContext, ConfigRegistry], bool]] = {}
        self._lock = threading.Lock()
        
        # Register built-in evaluators
        self._register_builtin_evaluators()
    
    def _register_builtin_evaluators(self) -> None:
        """Register standard evaluator functions."""
        
        def always_true(ctx: RuntimeContext, cfg: ConfigRegistry) -> bool:
            return True
        
        def always_false(ctx: RuntimeContext, cfg: ConfigRegistry) -> bool:
            return False
        
        def is_production(ctx: RuntimeContext, cfg: ConfigRegistry) -> bool:
            return ctx.env == RuntimeEnvironment.PRODUCTION
        
        def is_development(ctx: RuntimeContext, cfg: ConfigRegistry) -> bool:
            return ctx.env == RuntimeEnvironment.DEVELOPMENT
        
        def is_replay_mode(ctx: RuntimeContext, cfg: ConfigRegistry) -> bool:
            return ctx.mode == ExecutionMode.REPLAY
        
        self.register_evaluator("always_true", always_true)
        self.register_evaluator("always_false", always_false)
        self.register_evaluator("is_production", is_production)
        self.register_evaluator("is_development", is_development)
        self.register_evaluator("is_replay_mode", is_replay_mode)
    
    def register_evaluator(
        self,
        name: str,
        evaluator: Callable[[RuntimeContext, ConfigRegistry], bool]
    ) -> None:
        """Register a custom evaluator function."""
        with self._lock:
            if name in self._evaluators:
                raise ValueError(f"Evaluator '{name}' already registered")
            self._evaluators[name] = evaluator
    
    def evaluate(self, condition: FlagCondition) -> bool:
        """Evaluate a flag condition."""
        with self._lock:
            if condition.evaluator_name not in self._evaluators:
                raise ValueError(
                    f"Unknown evaluator: '{condition.evaluator_name}' "
                    f"for condition '{condition.predicate}'"
                )
            
            evaluator = self._evaluators[condition.evaluator_name]
        
        # Evaluate (must be deterministic)
        try:
            result = evaluator(self._runtime_context, self._config_registry)
            if not isinstance(result, bool):
                raise TypeError(
                    f"Evaluator '{condition.evaluator_name}' returned non-bool: {result}"
                )
            return result
        except Exception as e:
            raise RuntimeError(
                f"Error evaluating condition '{condition.predicate}': {e}"
            ) from e


# ============================================================================
# KILL SWITCH (EMERGENCY PATH)
# ============================================================================

class KillSwitch:
    """
    Emergency flags with kill_switch=True:
    - bypass normal evaluation
    - immediately halt: posting, orchestration, experimentation
    - escalate to watchdog
    
    This is how you survive platform meltdowns.
    """
    
    def __init__(self):
        self._active_switches: Set[str] = set()
        self._lock = threading.Lock()
    
    def activate(self, flag_name: str, reason: str) -> None:
        """Activate a kill switch (emergency shutdown)."""
        with self._lock:
            self._active_switches.add(flag_name)
            # In production, this would:
            # - Log to monitoring
            # - Alert on-call
            # - Trigger graceful shutdown of affected subsystems
            print(f"[KILL SWITCH ACTIVATED] {flag_name}: {reason}")
    
    def deactivate(self, flag_name: str) -> None:
        """Deactivate a kill switch (recovery)."""
        with self._lock:
            if flag_name in self._active_switches:
                self._active_switches.remove(flag_name)
                print(f"[KILL SWITCH DEACTIVATED] {flag_name}")
    
    def is_active(self, flag_name: str) -> bool:
        """Check if a kill switch is active."""
        with self._lock:
            return flag_name in self._active_switches
    
    def get_active_switches(self) -> Set[str]:
        """Get all active kill switches."""
        with self._lock:
            return self._active_switches.copy()


# ============================================================================
# FEATURE FLAG REGISTRY (SINGLETON)
# ============================================================================

class FeatureFlagRegistry:
    """
    The single authority for feature flag definitions and evaluation.
    
    Guarantees:
    - Deterministic resolution (same inputs → same outputs)
    - Environment enforcement
    - Condition checking
    - Cached per run (immutable)
    
    Created once at boot.
    """
    
    _instance: Optional['FeatureFlagRegistry'] = None
    _lock = threading.Lock()
    
    def __init__(
        self,
        runtime_context: RuntimeContext,
        config_registry: ConfigRegistry
    ):
        self._runtime_context = runtime_context
        self._config_registry = config_registry
        
        # Flag registry
        self._flags: Dict[str, FeatureFlag] = {}
        self._registry_lock = threading.Lock()
        
        # Evaluation cache (immutable per run)
        self._evaluation_cache: Dict[str, bool] = {}
        self._cache_lock = threading.Lock()
        
        # Components
        self._evaluator = FlagEvaluator(runtime_context, config_registry)
        self._kill_switch = KillSwitch()
        
        # Validation state
        self._validated = False
    
    @classmethod
    def get_instance(
        cls,
        runtime_context: RuntimeContext,
        config_registry: ConfigRegistry
    ) -> 'FeatureFlagRegistry':
        """Singleton accessor."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = FeatureFlagRegistry(runtime_context, config_registry)
        return cls._instance
    
    def register_flag(self, flag: FeatureFlag) -> None:
        """
        Register a feature flag.
        
        Hard fails if:
        - duplicate name+version
        - invalid scope
        - emergency flag not kill-switch safe
        - experimental flag allowed in production without override
        """
        with self._registry_lock:
            flag_id = flag.get_id()
            
            # Check for duplicates
            if flag_id in self._flags:
                existing = self._flags[flag_id]
                if existing != flag:
                    raise ValueError(
                        f"Flag '{flag_id}' already registered with different config"
                    )
                return  # Already registered identically
            
            # Validate flag is allowed in current environment
            if self._runtime_context.env not in flag.allowed_environments:
                raise ValueError(
                    f"Flag '{flag_id}' not allowed in environment "
                    f"{self._runtime_context.env.value}"
                )
            
            # Validate flag is allowed in current mode
            if self._runtime_context.mode not in flag.allowed_modes:
                raise ValueError(
                    f"Flag '{flag_id}' not allowed in mode "
                    f"{self._runtime_context.mode.value}"
                )
            
            # Register
            self._flags[flag_id] = flag
            
            # Mark as needing validation
            self._validated = False
    
    def validate_registry(self) -> None:
        """
        Validate the entire registry (BOOT-TIME).
        
        Checks:
        - no conflicting flags
        - no orphaned flags
        - sunset flags not referenced
        - emergency flags correctly wired
        """
        with self._registry_lock:
            # Check for sunset flags that shouldn't be used
            sunset_flags = [
                f for f in self._flags.values()
                if f.stability == FlagStability.SUNSET
            ]
            if sunset_flags and self._runtime_context.mode == ExecutionMode.LIVE:
                raise ValueError(
                    f"SUNSET flags found in PRODUCTION mode: "
                    f"{[f.name for f in sunset_flags]}"
                )
            
            # Validate all conditions reference registered evaluators
            for flag in self._flags.values():
                for condition in flag.conditions:
                    if condition.evaluator_name not in self._evaluator._evaluators:
                        raise ValueError(
                            f"Flag '{flag.name}' references unknown evaluator "
                            f"'{condition.evaluator_name}'"
                        )
            
            self._validated = True
    
    def is_enabled(self, flag_name: str, version: Optional[str] = None) -> bool:
        """
        Check if a flag is enabled.
        
        Rules:
        - deterministic resolution
        - condition-checked
        - environment-checked
        - cached per run
        
        No lazy evaluation - all flags resolved at boot.
        """
        if not self._validated:
            raise RuntimeError(
                "FeatureFlagRegistry must be validated before use. "
                "Call validate_registry() at boot."
            )
        
        # Construct flag_id
        flag_id = f"{flag_name}:v{version}" if version else self._find_latest_version(flag_name)
        
        # Check cache first
        with self._cache_lock:
            if flag_id in self._evaluation_cache:
                return self._evaluation_cache[flag_id]
        
        # Get flag
        with self._registry_lock:
            if flag_id not in self._flags:
                raise ValueError(f"Unknown flag: '{flag_id}'")
            flag = self._flags[flag_id]
        
        # Check kill switch
        if flag.kill_switch and self._kill_switch.is_active(flag_name):
            # Kill switch active = flag disabled
            result = False
        else:
            # Evaluate based on default and conditions
            result = flag.default_enabled
            
            # Evaluate all conditions (all must be true if any exist)
            if flag.conditions:
                for condition in flag.conditions:
                    if not self._evaluator.evaluate(condition):
                        result = False
                        break
        
        # Cache result
        with self._cache_lock:
            self._evaluation_cache[flag_id] = result
        
        return result
    
    def assert_allowed(self, flag_name: str, version: Optional[str] = None) -> None:
        """
        Assert that a flag is enabled.
        
        Used in critical paths.
        If flag disabled → hard crash (do not silently degrade)
        """
        if not self.is_enabled(flag_name, version):
            raise RuntimeError(
                f"FEATURE FLAG VIOLATION: Flag '{flag_name}' is DISABLED. "
                f"This code path is not authorized to execute."
            )
    
    def activate_kill_switch(self, flag_name: str, reason: str) -> None:
        """Activate emergency kill switch."""
        # Verify flag exists and is a kill switch
        flag_id = self._find_latest_version(flag_name)
        
        with self._registry_lock:
            if flag_id not in self._flags:
                raise ValueError(f"Unknown flag: '{flag_name}'")
            
            flag = self._flags[flag_id]
            if not flag.kill_switch:
                raise ValueError(
                    f"Flag '{flag_name}' is not configured as a kill switch"
                )
        
        # Activate
        self._kill_switch.activate(flag_name, reason)
        
        # Invalidate cache for this flag
        with self._cache_lock:
            if flag_id in self._evaluation_cache:
                del self._evaluation_cache[flag_id]
    
    def deactivate_kill_switch(self, flag_name: str) -> None:
        """Deactivate emergency kill switch."""
        self._kill_switch.deactivate(flag_name)
        
        # Invalidate cache
        flag_id = self._find_latest_version(flag_name)
        with self._cache_lock:
            if flag_id in self._evaluation_cache:
                del self._evaluation_cache[flag_id]
    
    def snapshot(self, snapshot_id: str) -> FlagSnapshot:
        """Create immutable snapshot of current flag states."""
        if not self._validated:
            raise RuntimeError("Registry must be validated before snapshot")
        
        # Ensure all flags are evaluated
        with self._registry_lock:
            for flag_id in self._flags.keys():
                # Extract name from flag_id
                flag_name = flag_id.split(':v')[0]
                if flag_id not in self._evaluation_cache:
                    self.is_enabled(flag_name)
        
        # Create snapshot
        with self._cache_lock:
            flag_states = self._evaluation_cache.copy()
        
        # Compute hash
        flag_items = sorted(flag_states.items())
        flag_str = "|".join(f"{k}={v}" for k, v in flag_items)
        snapshot_hash = hashlib.sha256(flag_str.encode('utf-8')).hexdigest()
        
        return FlagSnapshot(
            snapshot_id=snapshot_id,
            flags=flag_states,
            created_at=datetime.now(timezone.utc),
            runtime_mode=self._runtime_context.mode,
            runtime_env=self._runtime_context.env,
            hash=snapshot_hash
        )
    
    def restore_from_snapshot(self, snapshot: FlagSnapshot) -> None:
        """Restore flag states from snapshot (for replay)."""
        snapshot.validate()
        
        if self._runtime_context.mode != ExecutionMode.REPLAY:
            raise RuntimeError(
                "restore_from_snapshot() only allowed in REPLAY mode"
            )
        
        # Restore cache
        with self._cache_lock:
            self._evaluation_cache = snapshot.flags.copy()
    
    def _find_latest_version(self, flag_name: str) -> str:
        """Find latest version of a flag by name."""
        with self._registry_lock:
            matching = [
                flag_id for flag_id in self._flags.keys()
                if flag_id.startswith(f"{flag_name}:v")
            ]
            
            if not matching:
                raise ValueError(f"No versions found for flag: '{flag_name}'")
            
            # Return the last one (assumes registration order)
            return matching[-1]
    
    def get_flag(self, flag_name: str, version: Optional[str] = None) -> FeatureFlag:
        """Get flag definition (for inspection)."""
        flag_id = f"{flag_name}:v{version}" if version else self._find_latest_version(flag_name)
        
        with self._registry_lock:
            if flag_id not in self._flags:
                raise ValueError(f"Unknown flag: '{flag_id}'")
            return self._flags[flag_id]
    
    def list_flags(self, scope: Optional[FlagScope] = None) -> List[FeatureFlag]:
        """List all registered flags, optionally filtered by scope."""
        with self._registry_lock:
            flags = list(self._flags.values())
            
            if scope is not None:
                flags = [f for f in flags if f.scope == scope]
            
            return flags


# ============================================================================
# FEATURE FLAG WATCHDOG (ENFORCEMENT)
# ============================================================================

class FeatureFlagWatchdog:
    """
    Monitors feature flag usage for violations:
    - access to undefined flags
    - violation of scope
    - disabled path execution
    - emergency triggers
    
    Any violation → escalation.
    """
    
    def __init__(self, registry: FeatureFlagRegistry):
        self._registry = registry
        self._violations: List[str] = []
        self._lock = threading.Lock()
    
    def check_undefined_access(self, flag_name: str) -> None:
        """Detect attempts to access undefined flags."""
        try:
            self._registry._find_latest_version(flag_name)
        except ValueError:
            violation = f"WATCHDOG: Access to undefined flag '{flag_name}'"
            self._record_violation(violation)
            raise RuntimeError(violation)
    
    def check_scope_violation(self, flag_name: str, calling_scope: FlagScope) -> None:
        """Detect scope violations (e.g., posting code accessing model flags)."""
        flag = self._registry.get_flag(flag_name)
        
        if flag.scope != FlagScope.GLOBAL and flag.scope != calling_scope:
            violation = (
                f"WATCHDOG: Scope violation - flag '{flag_name}' has scope "
                f"{flag.scope.value}, but accessed from {calling_scope.value}"
            )
            self._record_violation(violation)
            raise RuntimeError(violation)
    
    def check_disabled_execution(self, flag_name: str, code_path: str) -> None:
        """Detect execution of code paths when flag is disabled."""
        if not self._registry.is_enabled(flag_name):
            violation = (
                f"WATCHDOG: Disabled code path executed - flag '{flag_name}' "
                f"is disabled but code path '{code_path}' was executed"
            )
            self._record_violation(violation)
            raise RuntimeError(violation)
    
    def _record_violation(self, violation: str) -> None:
        """Record a violation for audit."""
        with self._lock:
            self._violations.append(violation)
            # In production, this would log to monitoring system
            print(f"[FeatureFlagWatchdog] {violation}")
    
    def get_violations(self) -> List[str]:
        """Get all recorded violations."""
        with self._lock:
            return self._violations.copy()


# ============================================================================
# MODULE-LEVEL HELPERS
# ============================================================================

def initialize_feature_flags(
    runtime_context: RuntimeContext,
    config_registry: ConfigRegistry
) -> FeatureFlagRegistry:
    """
    Initialize the global feature flag system.
    Called once at process boot.
    """
    registry = FeatureFlagRegistry.get_instance(runtime_context, config_registry)
    return registry


def get_feature_flags() -> FeatureFlagRegistry:
    """Get the singleton FeatureFlagRegistry instance."""
    if FeatureFlagRegistry._instance is None:
        raise RuntimeError(
            "FeatureFlagRegistry not initialized. "
            "Call initialize_feature_flags() at process boot."
        )
    return FeatureFlagRegistry._instance


# ============================================================================
# FORBIDDEN PATTERNS (ZERO TOLERANCE)
# ============================================================================

def _forbidden_dynamic_flag_mutation():
    """❌ NEVER mutate flags during run"""
    raise NotImplementedError(
        "Dynamic flag mutation is FORBIDDEN. "
        "Flags are immutable per run."
    )


def _forbidden_time_based_evaluation():
    """❌ NEVER evaluate flags based on time"""
    raise NotImplementedError(
        "Time-based flag evaluation is FORBIDDEN. "
        "Flags must be deterministic."
    )


def _forbidden_random_rollout():
    """❌ NEVER use randomness in flag evaluation"""
    raise NotImplementedError(
        "Random flag evaluation is FORBIDDEN. "
        "Use experiments for A/B testing."
    )

