"""
/data/versioning/__init__.py

Authoritative Versioning Control Plane Boundary.

This module defines the ONLY supported public interface for:
- Schema version modeling
- Migration planning
- Compatibility validation
- Runtime version resolution
- Governance enforcement

All internal modules are considered private implementation details.

---

ARCHITECTURAL ROLE

This file is the authority boundary for schema evolution at scale.

Think of it as:

/data/versioning/
  ├── model/
  ├── evolution/
  ├── compatibility/
  ├── governance/
  ├── runtime/
  └── __init__.py   ← Safe control plane facade

Other modules should NOT import:
- migration_executor directly
- compatibility_matrix directly
- version_graph directly

They import:
- from data.versioning import VersionEngine
- from data.versioning import SchemaVersion
- from data.versioning import VersionError

Everything else is internal.

---

DESIGN PRINCIPLES

1. Encapsulation: Internal modules are volatile. __init__.py must stay stable.
2. Deterministic Ordering: No migration runs without a planned path.
3. Governance Precedence: Lock check runs before compatibility or planning.
4. Explicit Failure: All failures produce type-specific exceptions.
5. Testability: Dependency injection enables deterministic testing.

---

PRODUCTION HARDENING

- Minimal exports via __all__
- CI-protected interface stability
- Governance-aware operations
- No accidental imports allowed
"""

from __future__ import annotations

from typing import Final, Protocol, List

# DO NOT import internal modules outside this package.
# All external code must use the public API defined here.

# ============================================================================
# PUBLIC MODEL EXPORTS
# ============================================================================

# Import from lineage as the canonical source, but re-export through our boundary
# Note: These are internal implementation details - external code should use SchemaVersion
from ..lineage.schema_versions import SchemaVersionDefinition
from ..lineage.lineage_types import SchemaVersionID

# Public type aliases
SchemaVersion = SchemaVersionID
"""Public type alias for schema version identifiers."""


# ============================================================================
# PUBLIC ERROR TYPES
# ============================================================================

class VersionError(Exception):
    """Base exception for versioning subsystem."""
    pass


class IncompatibleVersionError(VersionError):
    """Raised when an illegal compatibility transition is attempted."""
    pass


class MigrationPathError(VersionError):
    """Raised when no legal migration path exists."""
    pass


class GovernanceViolationError(VersionError):
    """Raised when version bump violates governance policy."""
    pass


# ============================================================================
# INTERNAL COMPONENT PROTOCOLS
# ============================================================================

class _MigrationPlanner(Protocol):
    """Protocol for migration planning components."""
    
    def compute_path(
        self,
        source: SchemaVersion,
        target: SchemaVersion,
    ) -> list[SchemaVersion] | None:
        """Compute deterministic legal upgrade path. Returns None if no path exists."""
        ...


class _MigrationExecutor(Protocol):
    """Protocol for migration execution components."""
    
    def execute(
        self,
        data: object,
        path: list[SchemaVersion],
    ) -> object:
        """Apply ordered, deterministic, idempotent migrations."""
        ...


class _ContractEnforcer(Protocol):
    """Protocol for compatibility enforcement components."""
    
    def is_compatible(
        self,
        source: SchemaVersion,
        target: SchemaVersion,
    ) -> bool:
        """Check if source version is compatible with target version."""
        ...


class _VersionResolver(Protocol):
    """Protocol for runtime version resolution components."""
    
    def resolve(
        self,
        requested: SchemaVersion | None,
    ) -> SchemaVersion:
        """Resolve the serving version based on policy + rollout state."""
        ...


class _VersionLock(Protocol):
    """Protocol for governance locking components."""
    
    def is_frozen(self) -> bool:
        """Check if schema evolution is currently frozen."""
        ...


# ============================================================================
# CENTRAL CONTROL PLANE OBJECT
# ============================================================================

class VersionEngine:
    """
    Deterministic control plane for all schema evolution operations.
    
    This is the ONLY supported entry point for versioning operations.
    All internal mechanics are encapsulated and accessed through this interface.
    
    Usage::
    
        engine = VersionEngine(
            planner=planner,
            executor=executor,
            enforcer=enforcer,
            resolver=resolver,
            lock=lock,
        )
        
        # Check compatibility
        engine.assert_compatible(source="v1", target="v2")
        
        # Plan migration
        path = engine.plan_migration(source="v1", target="v2")
        
        # Execute migration
        migrated_data = engine.migrate(data, source="v1", target="v2")
        
        # Resolve runtime version
        effective = engine.resolve_effective_version(requested="v2")
    """
    
    __slots__ = (
        "_planner",
        "_executor",
        "_enforcer",
        "_resolver",
        "_lock",
    )
    
    def __init__(
        self,
        planner: _MigrationPlanner,
        executor: _MigrationExecutor,
        enforcer: _ContractEnforcer,
        resolver: _VersionResolver,
        lock: _VersionLock,
    ) -> None:
        """
        Initialize the version engine with dependency injection.
        
        Args:
            planner: Migration planning component
            executor: Migration execution component
            enforcer: Compatibility enforcement component
            resolver: Runtime version resolution component
            lock: Governance locking component
            
        Raises:
            TypeError: If any component is None or invalid type
        """
        if planner is None:
            raise TypeError("planner cannot be None")
        if executor is None:
            raise TypeError("executor cannot be None")
        if enforcer is None:
            raise TypeError("enforcer cannot be None")
        if resolver is None:
            raise TypeError("resolver cannot be None")
        if lock is None:
            raise TypeError("lock cannot be None")
            
        object.__setattr__(self, "_planner", planner)
        object.__setattr__(self, "_executor", executor)
        object.__setattr__(self, "_enforcer", enforcer)
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_lock", lock)
    
    def __setattr__(self, *_: object) -> None:
        """Prevent mutation after construction."""
        raise TypeError("VersionEngine is immutable after construction.")
    
    # ========================================================================
    # SAFE PUBLIC OPERATIONS
    # ========================================================================
    
    def assert_compatible(
        self,
        source: SchemaVersion,
        target: SchemaVersion,
    ) -> None:
        """
        Enforce compatibility constraints.
        
        Raises IncompatibleVersionError if the transition from source to target
        is not legally compatible according to the compatibility matrix.
        
        Args:
            source: Source schema version
            target: Target schema version
            
        Raises:
            IncompatibleVersionError: If source is not compatible with target
            TypeError: If source or target are invalid types
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be SchemaVersion (str), got {type(source)!r}")
        if not isinstance(target, str):
            raise TypeError(f"target must be SchemaVersion (str), got {type(target)!r}")
        
        if not self._enforcer.is_compatible(source, target):
            raise IncompatibleVersionError(
                f"{source} is not compatible with {target}"
            )
    
    def plan_migration(
        self,
        source: SchemaVersion,
        target: SchemaVersion,
    ) -> list[SchemaVersion]:
        """
        Compute deterministic legal upgrade path.
        
        Returns an ordered list of schema versions representing the migration path
        from source to target. The path includes both source and target versions.
        
        Args:
            source: Source schema version
            target: Target schema version
            
        Returns:
            Ordered list of schema versions from source to target (inclusive)
            
        Raises:
            MigrationPathError: If no legal migration path exists
            TypeError: If source or target are invalid types
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be SchemaVersion (str), got {type(source)!r}")
        if not isinstance(target, str):
            raise TypeError(f"target must be SchemaVersion (str), got {type(target)!r}")
        
        path = self._planner.compute_path(source, target)
        if not path:
            raise MigrationPathError(
                f"No legal migration path from {source} to {target}"
            )
        return path
    
    def migrate(
        self,
        data: object,
        source: SchemaVersion,
        target: SchemaVersion,
    ) -> object:
        """
        Apply ordered, deterministic, idempotent migrations.
        
        This method enforces the correct sequence:
        1. Governance check (lock validation)
        2. Compatibility check
        3. Deterministic planning
        4. Execution
        
        Args:
            data: Data object to migrate
            source: Current schema version of the data
            target: Target schema version
            
        Returns:
            Migrated data object at target version
            
        Raises:
            GovernanceViolationError: If schema evolution is frozen
            IncompatibleVersionError: If source is not compatible with target
            MigrationPathError: If no legal migration path exists
            TypeError: If arguments are invalid types
        """
        if not isinstance(source, str):
            raise TypeError(f"source must be SchemaVersion (str), got {type(source)!r}")
        if not isinstance(target, str):
            raise TypeError(f"target must be SchemaVersion (str), got {type(target)!r}")
        
        # 1. Governance check (precedence)
        if self._lock.is_frozen():
            raise GovernanceViolationError("Schema evolution frozen.")
        
        # 2. Compatibility check
        self.assert_compatible(source, target)
        
        # 3. Deterministic planning
        path = self.plan_migration(source, target)
        
        # 4. Execution
        return self._executor.execute(data, path)
    
    def resolve_effective_version(
        self,
        requested: SchemaVersion | None = None,
    ) -> SchemaVersion:
        """
        Resolve the serving version based on policy + rollout state.
        
        This is used by:
        - API layer for request routing
        - Storage deserialization
        - Background processors
        
        Args:
            requested: Optional requested version. If None, resolves to default.
            
        Returns:
            Effective schema version to use for serving
            
        Raises:
            TypeError: If requested is invalid type
        """
        if requested is not None and not isinstance(requested, str):
            raise TypeError(
                f"requested must be SchemaVersion (str) or None, got {type(requested)!r}"
            )
        
        return self._resolver.resolve(requested)


# ============================================================================
# SINGLETON ENGINE (OPTIONAL BUT COMMON)
# ============================================================================

_engine: VersionEngine | None = None
"""Global version engine instance. Protected by module-level initialization."""


def initialize(engine: VersionEngine) -> None:
    """
    Initialize the global version engine singleton.
    
    This prevents accidental multi-engine divergence and ensures node-level
    consistency across the system.
    
    Args:
        engine: VersionEngine instance to use globally
        
    Raises:
        RuntimeError: If engine is already initialized
        TypeError: If engine is not a VersionEngine instance
    """
    global _engine
    
    if _engine is not None:
        raise RuntimeError("VersionEngine already initialized.")
    
    if not isinstance(engine, VersionEngine):
        raise TypeError(f"engine must be VersionEngine, got {type(engine)!r}")
    
    _engine = engine


def engine() -> VersionEngine:
    """
    Get the global version engine instance.
    
    Returns:
        The initialized VersionEngine instance
        
    Raises:
        RuntimeError: If engine has not been initialized
    """
    if _engine is None:
        raise RuntimeError(
            "VersionEngine not initialized. Call initialize() first."
        )
    return _engine


# ============================================================================
# PUBLIC SURFACE (SEALED)
# ============================================================================

__all__: Final = [
    # Public model types
    "SchemaVersion",
    "SchemaVersionDefinition",
    "SchemaVersionID",
    
    # Public error types
    "VersionError",
    "IncompatibleVersionError",
    "MigrationPathError",
    "GovernanceViolationError",
    
    # Public control plane
    "VersionEngine",
    
    # Singleton management
    "initialize",
    "engine",
]
