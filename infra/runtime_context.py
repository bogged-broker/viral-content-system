
"""
/infra/runtime_context.py

Immutable Execution Context & Run Identity Authority

This module defines the single source of truth for "what execution does this process belong to?"

CRITICAL INVARIANTS:
- RuntimeContext is created ONCE and frozen FOREVER
- No mutation after creation
- No lazy evaluation
- No optional fields
- Deterministic hashing for reproducibility

Thread-safe, process-safe, audit-safe.
"""

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# =============================================================================
# ENUMS - EXPLICIT, NOT STRINGS
# =============================================================================

class RuntimeEnvironment(Enum):
    """
    Defines where this process is executing.
    
    PRODUCTION: Live production environment
    STAGING: Pre-production staging environment
    SANDBOX: Development/testing sandbox
    REPLAY: Historical replay environment
    """
    PRODUCTION = "production"
    STAGING = "staging"
    SANDBOX = "sandbox"
    REPLAY = "replay"

    def is_production(self) -> bool:
        return self == RuntimeEnvironment.PRODUCTION

    def is_replay(self) -> bool:
        return self == RuntimeEnvironment.REPLAY


class ExecutionMode(Enum):
    """
    Defines how this process is executing.
    
    LIVE: Real-time execution with side effects
    BACKTEST: Historical simulation, no side effects
    REPLAY: Deterministic replay of past execution
    DRY_RUN: Test mode, no mutations
    """
    LIVE = "live"
    BACKTEST = "backtest"
    REPLAY = "replay"
    DRY_RUN = "dry_run"

    def allows_side_effects(self) -> bool:
        return self == ExecutionMode.LIVE

    def requires_determinism(self) -> bool:
        return self in (ExecutionMode.REPLAY, ExecutionMode.BACKTEST)


# =============================================================================
# IMMUTABLE IDENTITY
# =============================================================================

@dataclass(frozen=True)
class RuntimeIdentity:
    """
    Immutable identity of this execution run.
    
    Created once at boot, never changes.
    """
    run_id: str  # Deterministic, boot-time generated
    boot_hash: str  # From bootstrap validation
    deploy_id: str  # Infrastructure deployment identifier
    created_at: datetime  # Monotonic UTC timestamp

    def __post_init__(self):
        """Validate identity fields."""
        if not self.run_id or len(self.run_id) < 16:
            raise ValueError(f"Invalid run_id: {self.run_id}")
        
        if not self.boot_hash or len(self.boot_hash) != 64:
            raise ValueError(f"Invalid boot_hash: {self.boot_hash}")
        
        if not self.deploy_id:
            raise ValueError("deploy_id cannot be empty")
        
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware UTC")


# =============================================================================
# RUNTIME CONTEXT - THE CORE OBJECT
# =============================================================================

@dataclass(frozen=True)
class RuntimeContext:
    """
    Immutable execution context for this process.
    
    NO OPTIONAL FIELDS. NO DEFAULTS. NO LAZY EVALUATION.
    
    This is the single source of truth for:
    - Who this run is
    - Where it's running
    - What mode it's in
    - What guarantees apply
    """
    identity: RuntimeIdentity
    
    environment: RuntimeEnvironment
    mode: ExecutionMode
    
    region: str
    platform: str  # e.g., "linux_x86_64", "gpu_a100"
    
    config_version: str  # Global config snapshot ID
    feature_flag_version: str  # Feature flag snapshot ID
    
    replay_enabled: bool
    audit_strict: bool
    
    invariants_hash: str  # Infrastructure invariants digest
    
    # Computed on creation
    context_hash: str = field(init=False)

    def __post_init__(self):
        """
        Compute deterministic context hash.
        
        This hash is used for:
        - Audit correlation
        - Replay matching
        - Experiment traceability
        - Bug reproduction
        """
        # Must use object.__setattr__ because dataclass is frozen
        hash_input = (
            f"{self.identity.run_id}|"
            f"{self.identity.boot_hash}|"
            f"{self.identity.deploy_id}|"
            f"{self.environment.value}|"
            f"{self.mode.value}|"
            f"{self.region}|"
            f"{self.platform}|"
            f"{self.config_version}|"
            f"{self.feature_flag_version}|"
            f"{self.replay_enabled}|"
            f"{self.audit_strict}|"
            f"{self.invariants_hash}"
        )
        
        computed_hash = hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
        object.__setattr__(self, 'context_hash', computed_hash)
        
        # Validate after hash computation
        self._validate()

    def _validate(self) -> None:
        """Validate context consistency."""
        if not self.region:
            raise ValueError("region cannot be empty")
        
        if not self.platform:
            raise ValueError("platform cannot be empty")
        
        if not self.config_version:
            raise ValueError("config_version cannot be empty")
        
        if not self.feature_flag_version:
            raise ValueError("feature_flag_version cannot be empty")
        
        if not self.invariants_hash or len(self.invariants_hash) != 64:
            raise ValueError(f"Invalid invariants_hash: {self.invariants_hash}")

    def is_production(self) -> bool:
        return self.environment.is_production()

    def is_live(self) -> bool:
        return self.mode == ExecutionMode.LIVE

    def requires_audit(self) -> bool:
        return self.audit_strict or self.is_production()

    def allows_mutations(self) -> bool:
        return self.mode.allows_side_effects() and not self.replay_enabled


# =============================================================================
# CONTEXT VALIDATOR - STRICT CONSISTENCY CHECKS
# =============================================================================

class ContextValidator:
    """
    Validates RuntimeContext for logical consistency.
    
    NO WARNINGS. ONLY PASS/FAIL.
    """
    
    @staticmethod
    def validate(
        environment: RuntimeEnvironment,
        mode: ExecutionMode,
        replay_enabled: bool,
        audit_strict: bool,
        config_version: str,
        feature_flag_version: str,
    ) -> None:
        """
        Validate context consistency before creation.
        
        Raises ValueError on any violation.
        """
        # Production environment rules
        if environment == RuntimeEnvironment.PRODUCTION:
            if mode == ExecutionMode.DRY_RUN:
                raise ValueError(
                    "PRODUCTION environment cannot run in DRY_RUN mode"
                )
            
            if not audit_strict:
                raise ValueError(
                    "PRODUCTION environment REQUIRES audit_strict=True"
                )
        
        # Replay environment rules
        if environment == RuntimeEnvironment.REPLAY:
            if mode != ExecutionMode.REPLAY:
                raise ValueError(
                    "REPLAY environment REQUIRES ExecutionMode.REPLAY"
                )
            
            if not replay_enabled:
                raise ValueError(
                    "REPLAY environment REQUIRES replay_enabled=True"
                )
        
        # Replay mode rules
        if mode == ExecutionMode.REPLAY:
            if not replay_enabled:
                raise ValueError(
                    "ExecutionMode.REPLAY REQUIRES replay_enabled=True"
                )
            
            if not config_version or config_version == "latest":
                raise ValueError(
                    "REPLAY mode REQUIRES pinned config_version (not 'latest')"
                )
            
            if not feature_flag_version or feature_flag_version == "latest":
                raise ValueError(
                    "REPLAY mode REQUIRES pinned feature_flag_version (not 'latest')"
                )
        
        # Live mode with replay is contradictory
        if mode == ExecutionMode.LIVE and replay_enabled:
            raise ValueError(
                "ExecutionMode.LIVE cannot have replay_enabled=True"
            )
        
        # Backtest mode rules
        if mode == ExecutionMode.BACKTEST:
            if environment == RuntimeEnvironment.PRODUCTION:
                raise ValueError(
                    "BACKTEST mode cannot run in PRODUCTION environment"
                )


# =============================================================================
# CONTEXT BUILDER - ONE-TIME CREATION
# =============================================================================

class ContextBuilder:
    """
    Builds RuntimeContext exactly once.
    
    If called twice → HARD FAIL.
    """
    
    def __init__(self):
        self._built = False
        self._lock = threading.Lock()

    def build(
        self,
        run_id: str,
        boot_hash: str,
        deploy_id: str,
        environment: RuntimeEnvironment,
        mode: ExecutionMode,
        region: str,
        platform: str,
        config_version: str,
        feature_flag_version: str,
        replay_enabled: bool,
        audit_strict: bool,
        invariants_hash: str,
    ) -> RuntimeContext:
        """
        Build and freeze RuntimeContext.
        
        Can only be called ONCE per builder instance.
        
        Raises:
            RuntimeError: If called more than once
            ValueError: If validation fails
        """
        with self._lock:
            if self._built:
                raise RuntimeError(
                    "ContextBuilder.build() called multiple times - "
                    "RuntimeContext can only be created ONCE"
                )
            
            # Validate BEFORE creating
            ContextValidator.validate(
                environment=environment,
                mode=mode,
                replay_enabled=replay_enabled,
                audit_strict=audit_strict,
                config_version=config_version,
                feature_flag_version=feature_flag_version,
            )
            
            # Create identity
            identity = RuntimeIdentity(
                run_id=run_id,
                boot_hash=boot_hash,
                deploy_id=deploy_id,
                created_at=datetime.now(timezone.utc),
            )
            
            # Create context (will compute hash in __post_init__)
            context = RuntimeContext(
                identity=identity,
                environment=environment,
                mode=mode,
                region=region,
                platform=platform,
                config_version=config_version,
                feature_flag_version=feature_flag_version,
                replay_enabled=replay_enabled,
                audit_strict=audit_strict,
                invariants_hash=invariants_hash,
            )
            
            self._built = True
            return context


# =============================================================================
# CONTEXT ACCESSOR - READ-ONLY GLOBAL ACCESS
# =============================================================================

class ContextAccessor:
    """
    Thread-safe, process-safe read-only accessor for RuntimeContext.
    
    Returns the same frozen object always.
    """
    
    _context: Optional[RuntimeContext] = None
    _lock = threading.Lock()

    @classmethod
    def initialize(cls, context: RuntimeContext) -> None:
        """
        Initialize the global context exactly once.
        
        Raises:
            RuntimeError: If called more than once
        """
        with cls._lock:
            if cls._context is not None:
                raise RuntimeError(
                    "ContextAccessor already initialized - "
                    "cannot re-initialize RuntimeContext"
                )
            
            if not isinstance(context, RuntimeContext):
                raise TypeError(
                    f"Expected RuntimeContext, got {type(context)}"
                )
            
            cls._context = context

    @classmethod
    def get(cls) -> RuntimeContext:
        """
        Get the global RuntimeContext.
        
        Raises:
            RuntimeError: If accessed before initialization
        """
        if cls._context is None:
            raise RuntimeError(
                "RuntimeContext accessed before initialization - "
                "must call ContextAccessor.initialize() first"
            )
        
        return cls._context

    @classmethod
    def is_initialized(cls) -> bool:
        """Check if context has been initialized."""
        return cls._context is not None

    @classmethod
    def _reset_for_testing(cls) -> None:
        """
        TESTING ONLY: Reset the global context.
        
        DO NOT USE IN PRODUCTION CODE.
        """
        with cls._lock:
            cls._context = None


# =============================================================================
# PUBLIC API - READ-ONLY ACCESS
# =============================================================================

def get_runtime_context() -> RuntimeContext:
    """
    Get the global RuntimeContext.
    
    This is the ONLY public API for accessing runtime context.
    
    Returns:
        Frozen RuntimeContext instance
        
    Raises:
        RuntimeError: If accessed before initialization
    """
    return ContextAccessor.get()


def is_runtime_context_initialized() -> bool:
    """
    Check if RuntimeContext has been initialized.
    
    Useful for conditional logic during startup.
    """
    return ContextAccessor.is_initialized()


# =============================================================================
# DETERMINISM VERIFICATION
# =============================================================================

def verify_context_determinism(
    context1: RuntimeContext,
    context2: RuntimeContext,
) -> bool:
    """
    Verify that two contexts are byte-identical.
    
    Used for replay validation and determinism testing.
    
    Given identical:
    - bootstrap inputs
    - config versions
    - infra invariants
    
    The RuntimeContext MUST be byte-identical.
    """
    return context1.context_hash == context2.context_hash


# =============================================================================
# CONTEXT SERIALIZATION (for audit/replay)
# =============================================================================

def serialize_context(context: RuntimeContext) -> dict:
    """
    Serialize RuntimeContext for audit logs or replay storage.
    
    Returns a dictionary representation suitable for JSON serialization.
    """
    return {
        "identity": {
            "run_id": context.identity.run_id,
            "boot_hash": context.identity.boot_hash,
            "deploy_id": context.identity.deploy_id,
            "created_at": context.identity.created_at.isoformat(),
        },
        "environment": context.environment.value,
        "mode": context.mode.value,
        "region": context.region,
        "platform": context.platform,
        "config_version": context.config_version,
        "feature_flag_version": context.feature_flag_version,
        "replay_enabled": context.replay_enabled,
        "audit_strict": context.audit_strict,
        "invariants_hash": context.invariants_hash,
        "context_hash": context.context_hash,
    }


def deserialize_context(data: dict) -> RuntimeContext:
    """
    Deserialize RuntimeContext from audit logs or replay storage.
    
    Note: This creates a new RuntimeContext but does NOT initialize
    the global accessor. Use for validation only.
    """
    identity = RuntimeIdentity(
        run_id=data["identity"]["run_id"],
        boot_hash=data["identity"]["boot_hash"],
        deploy_id=data["identity"]["deploy_id"],
        created_at=datetime.fromisoformat(data["identity"]["created_at"]),
    )
    
    return RuntimeContext(
        identity=identity,
        environment=RuntimeEnvironment(data["environment"]),
        mode=ExecutionMode(data["mode"]),
        region=data["region"],
        platform=data["platform"],
        config_version=data["config_version"],
        feature_flag_version=data["feature_flag_version"],
        replay_enabled=data["replay_enabled"],
        audit_strict=data["audit_strict"],
        invariants_hash=data["invariants_hash"],
    )


# =============================================================================
# USAGE EXAMPLE (for documentation)
# =============================================================================

"""
USAGE PATTERN:

# During startup (once, after bootstrap):
from infra.runtime_context import (
    ContextBuilder,
    ContextAccessor,
    RuntimeEnvironment,
    ExecutionMode,
)

builder = ContextBuilder()
context = builder.build(
    run_id="run_20260124_abc123",
    boot_hash="<64-char-hash-from-bootstrap>",
    deploy_id="deploy_prod_v123",
    environment=RuntimeEnvironment.PRODUCTION,
    mode=ExecutionMode.LIVE,
    region="us-west-2",
    platform="linux_x86_64",
    config_version="v2.3.1",
    feature_flag_version="v1.5.0",
    replay_enabled=False,
    audit_strict=True,
    invariants_hash="<64-char-invariants-hash>",
)

ContextAccessor.initialize(context)


# Everywhere else in the codebase:
from infra.runtime_context import get_runtime_context

ctx = get_runtime_context()

if ctx.is_production():
    # Production-only logic
    pass

if ctx.allows_mutations():
    # Safe to mutate external state
    pass

# Stamp all logs/metrics/events with:
log_event(
    run_id=ctx.identity.run_id,
    context_hash=ctx.context_hash,
    ...
)
"""




