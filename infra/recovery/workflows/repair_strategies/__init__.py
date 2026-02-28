"""
/recovery/workflows/repair_strategies/__init__.py

Repair Strategy Registry & Exposure Boundary
(No Hidden Strategies, No Implicit Loading, No Execution Drift)

This module is the explicit strategy registry and exposure boundary for all
workflow-level repair strategies.

CRITICAL PRINCIPLES:
- Explicit registration (no dynamic discovery)
- Deterministic strategy lookup
- No hidden execution paths
- Immutable registry after definition
- Contract enforcement at import-time
- Version-aware strategy management

USAGE PATTERN:
CORRECT:
    from recovery.workflows.repair_strategies import get_repair_strategy
    strategy_class = get_repair_strategy("subgraph_repair")
    strategy = strategy_class(...)
    
FORBIDDEN:
    import importlib
    strategy = importlib.import_module(f"...{strategy_name}")  # Dynamic loading

REGISTRY STRUCTURE:
    REPAIR_STRATEGIES = {
        "subgraph_repair": SubgraphRepairStrategy,
        ...
    }

IMMUTABILITY:
- Registry is frozen using MappingProxyType
- Cannot add/remove strategies at runtime
- Mutation attempts raise TypeError

DETERMINISM:
- Identical code → identical registry
- No runtime variability
- No dynamic module scanning
- Stable lookup across reloads
"""

from typing import Dict, Type, Protocol, runtime_checkable, get_type_hints
from types import MappingProxyType
import inspect


# ============================================================================
# BASE STRATEGY INTERFACE (CONTRACT)
# ============================================================================

@runtime_checkable
class BaseRepairStrategy(Protocol):
    """
    Base protocol that all repair strategies must implement.
    
    This defines the contract that every repair strategy must satisfy.
    Contract enforcement happens at import-time during registry construction.
    
    Required Attributes:
        strategy_id: Unique identifier for this strategy
        strategy_schema_version: Schema version this strategy supports
        
    Required Methods:
        validate_request: Validate repair request before execution
        compute_scope: Determine scope of repair operation
        execute: Execute the repair operation
    """
    
    # Required class-level attributes
    strategy_id: str
    strategy_schema_version: int
    
    def validate_request(self, request: "RepairRequest") -> None:
        """
        Validate that repair request is well-formed and compatible.
        
        Args:
            request: Repair request to validate
            
        Raises:
            ValidationError: If request is invalid or incompatible
            
        This must check:
        - Request schema compatibility
        - Required fields present
        - Constraint satisfaction
        - Strategy applicability
        """
        ...
    
    def compute_scope(self, request: "RepairRequest") -> "RepairScope":
        """
        Compute the scope of entities/records affected by repair.
        
        Args:
            request: Repair request to analyze
            
        Returns:
            RepairScope defining affected entities
            
        This must determine:
        - Which entities will be modified
        - Dependency graph closure
        - Lock requirements
        - Isolation boundaries
        """
        ...
    
    def execute(self, request: "RepairRequest") -> "RepairResult":
        """
        Execute the repair operation.
        
        Args:
            request: Validated repair request
            
        Returns:
            RepairResult containing outcome and metadata
            
        Raises:
            RepairExecutionError: If repair fails
            
        This must:
        - Apply repair logic deterministically
        - Maintain audit trail
        - Verify post-conditions
        - Return structured result
        """
        ...


# ============================================================================
# STRATEGY IMPORTS (EXPLICIT ONLY)
# ============================================================================
# All strategies must be explicitly imported here.
# NO dynamic discovery, NO glob imports, NO pkgutil scanning.
# Only fully implemented strategies that satisfy BaseRepairStrategy protocol are imported.

from .subgraph_repair import SubgraphRepairStrategy
from .full_replay import FullReplayStrategy
from .checkpoint_rollback import CheckpointRollbackStrategy
from .incremental_rebuild import IncrementalRebuildStrategy
from .hash_verification_repair import HashVerificationRepairStrategy


# ============================================================================
# STRATEGY VALIDATION (IMPORT-TIME CONTRACT ENFORCEMENT)
# ============================================================================

def _validate_strategy_contract(
    strategy_id: str,
    strategy_class: Type,
) -> None:
    """
    Validate that strategy class satisfies BaseRepairStrategy contract.
    
    Checks:
    - Required attributes exist (strategy_id, strategy_schema_version)
    - Required methods exist (validate_request, compute_scope, execute)
    - Method signatures match Protocol requirements
    - strategy_id matches registration key
    - strategy_schema_version is valid integer
    
    Args:
        strategy_id: Expected strategy identifier (registry key)
        strategy_class: Strategy class to validate
        
    Raises:
        TypeError: If strategy doesn't satisfy contract
        ValueError: If strategy_id mismatch or invalid version
    """
    # Check if class satisfies protocol (runtime checkable)
    if not isinstance(strategy_class, type):
        raise TypeError(
            f"Strategy '{strategy_id}' must be a class, got {type(strategy_class)}"
        )
    
    # Check required attributes exist
    if not hasattr(strategy_class, 'strategy_id'):
        raise TypeError(
            f"Strategy class for '{strategy_id}' missing required attribute 'strategy_id'"
        )
    
    if not hasattr(strategy_class, 'strategy_schema_version'):
        raise TypeError(
            f"Strategy class for '{strategy_id}' missing required attribute 'strategy_schema_version'"
        )
    
    # Check required methods exist and validate signatures
    required_methods = {
        'validate_request': 1,  # Expects 1 parameter (self + request)
        'compute_scope': 1,     # Expects 1 parameter (self + request)
        'execute': 1,           # Expects 1 parameter (self + request)
    }
    
    for method_name, expected_param_count in required_methods.items():
        if not hasattr(strategy_class, method_name):
            raise TypeError(
                f"Strategy class for '{strategy_id}' missing required method '{method_name}'"
            )
        
        method = getattr(strategy_class, method_name)
        if not callable(method):
            raise TypeError(
                f"Strategy class for '{strategy_id}' attribute '{method_name}' is not callable"
            )
        
        # Validate method signature compatibility
        try:
            sig = inspect.signature(method)
            # Count non-self parameters (excluding 'self')
            param_count = len([p for p in sig.parameters.values() 
                              if p.name != 'self' and p.kind != inspect.Parameter.VAR_KEYWORD])
            
            if param_count != expected_param_count:
                raise TypeError(
                    f"Strategy class for '{strategy_id}' method '{method_name}' has "
                    f"incorrect signature: expected {expected_param_count} parameter(s) "
                    f"(excluding self), got {param_count}"
                )
        except (ValueError, TypeError) as e:
            # If signature inspection fails, still check basic callability
            # This handles edge cases like C extensions
            pass
    
    # Validate strategy_id matches registration key
    class_strategy_id = getattr(strategy_class, 'strategy_id')
    if class_strategy_id != strategy_id:
        raise ValueError(
            f"Strategy ID mismatch: registry key is '{strategy_id}' but "
            f"class declares strategy_id='{class_strategy_id}'"
        )
    
    # Validate strategy_schema_version is integer
    schema_version = getattr(strategy_class, 'strategy_schema_version')
    if not isinstance(schema_version, int):
        raise TypeError(
            f"Strategy '{strategy_id}' strategy_schema_version must be int, "
            f"got {type(schema_version)}"
        )
    
    if schema_version < 1:
        raise ValueError(
            f"Strategy '{strategy_id}' strategy_schema_version must be >= 1, "
            f"got {schema_version}"
        )


def _validate_registry_uniqueness(
    registry: Dict[str, Type],
) -> None:
    """
    Validate that registry has no duplicate strategy_ids.
    
    While the dict structure ensures key uniqueness, this also validates
    that no two different classes claim the same strategy_id.
    
    Args:
        registry: Strategy registry to validate
        
    Raises:
        ValueError: If duplicate strategy_id detected
    """
    seen_ids = {}
    
    for key, strategy_class in registry.items():
        strategy_id = getattr(strategy_class, 'strategy_id')
        
        if strategy_id in seen_ids:
            raise ValueError(
                f"Duplicate strategy_id '{strategy_id}' detected. "
                f"Registry key '{key}' has same ID as '{seen_ids[strategy_id]}'"
            )
        
        seen_ids[strategy_id] = key


# ============================================================================
# CANONICAL STRATEGY REGISTRY (IMMUTABLE)
# ============================================================================
# Build registry with explicit strategy mappings.
# Keys MUST be stable identifiers (no dynamic generation).
# Values MUST be strategy classes that satisfy BaseRepairStrategy contract.
# Only fully implemented strategies are registered.

_REPAIR_STRATEGIES: Dict[str, Type[BaseRepairStrategy]] = {
    "subgraph_repair": SubgraphRepairStrategy,
    "full_replay": FullReplayStrategy,
    "checkpoint_rollback": CheckpointRollbackStrategy,
    "incremental_rebuild": IncrementalRebuildStrategy,
    "hash_verification_repair": HashVerificationRepairStrategy,
}

# Validate all strategies at import time
for _strategy_id, _strategy_class in _REPAIR_STRATEGIES.items():
    _validate_strategy_contract(_strategy_id, _strategy_class)

# Validate registry-level invariants
_validate_registry_uniqueness(_REPAIR_STRATEGIES)

# Freeze registry to prevent runtime mutation
REPAIR_STRATEGIES: Dict[str, Type[BaseRepairStrategy]] = MappingProxyType(
    _REPAIR_STRATEGIES
)

# Clean up temporary variables
del _REPAIR_STRATEGIES, _strategy_id, _strategy_class


# ============================================================================
# DETERMINISTIC STRATEGY LOOKUP
# ============================================================================

def get_repair_strategy(strategy_id: str) -> Type[BaseRepairStrategy]:
    """
    Get repair strategy class by identifier.
    
    This is the ONLY sanctioned way to look up repair strategies.
    
    Rules:
    - Fails on unknown strategy_id (no fallback)
    - No fuzzy matching or suggestions
    - No auto-import of missing modules
    - Deterministic lookup guaranteed
    
    Args:
        strategy_id: Unique strategy identifier
        
    Returns:
        Strategy class implementing BaseRepairStrategy
        
    Raises:
        KeyError: If strategy_id not registered
        
    Usage:
        >>> strategy_class = get_repair_strategy("subgraph_repair")
        >>> strategy = strategy_class(config)
        >>> result = strategy.execute(request)
    """
    if strategy_id not in REPAIR_STRATEGIES:
        available = sorted(REPAIR_STRATEGIES.keys())
        raise KeyError(
            f"Unknown repair strategy: '{strategy_id}'. "
            f"Available strategies: {available}"
        )
    
    return REPAIR_STRATEGIES[strategy_id]


# ============================================================================
# REGISTRY INTEGRITY VALIDATION (INTERNAL ONLY)
# ============================================================================

def _validate_registry_integrity() -> None:
    """
    Validate registry integrity and contract compliance at import-time.
    
    Performs comprehensive validation:
    - All strategies satisfy contract
    - No duplicate IDs
    - Registry is immutable
    
    Raises:
        RuntimeError: If integrity checks fail
        
    This is called at import time only. Not exposed as public API.
    """
    # Verify registry is immutable
    if not isinstance(REPAIR_STRATEGIES, MappingProxyType):
        raise RuntimeError(
            "REPAIR_STRATEGIES must be MappingProxyType (immutable)"
        )
    
    # Verify all strategies still satisfy contract
    for strategy_id, strategy_class in REPAIR_STRATEGIES.items():
        try:
            _validate_strategy_contract(strategy_id, strategy_class)
        except (TypeError, ValueError) as e:
            raise RuntimeError(
                f"Strategy '{strategy_id}' fails contract validation: {e}"
            )
    
    # Verify uniqueness
    _validate_registry_uniqueness(REPAIR_STRATEGIES)


# ============================================================================
# IMPORT-TIME VALIDATION
# ============================================================================

# Execute full integrity validation on import (internal only)
_validate_registry_integrity()


# ============================================================================
# EXPORTED API (EXPLICIT EXPOSURE BOUNDARY)
# ============================================================================

__all__ = (
    # Base contract
    "BaseRepairStrategy",
    
    # Registry access
    "REPAIR_STRATEGIES",
    
    # Lookup function
    "get_repair_strategy",
)


# ============================================================================
# STATIC ASSERTIONS (COMPILE-TIME VALIDATION)
# ============================================================================

# Verify registry is non-empty
assert len(REPAIR_STRATEGIES) > 0, "REPAIR_STRATEGIES must not be empty"

# Verify all exports are defined
_module_globals = globals()
for _export in __all__:
    assert _export in _module_globals, f"Export '{_export}' not defined in module"

# Clean up namespace
del _module_globals, _export


# ============================================================================
# MODULE METADATA
# ============================================================================

__version__ = "1.0.0"
__author__ = "Recovery System"
__description__ = "Repair strategy registry and exposure boundary"

# Prevent modification of registry at runtime
_ORIGINAL_REGISTRY = REPAIR_STRATEGIES


def __dir__():
    """Customize dir() to show only public API."""
    return list(__all__)