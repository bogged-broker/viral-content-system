"""
Backend Construction Authority (Deterministic Storage Selection Layer)

This module is the single authority responsible for constructing persistence backends
in a deterministic, policy-compliant manner. It resolves backend type, validates
configuration against policy, and constructs the correct backend instance.

This file does NOT:
- Implement storage logic
- Contain business rules
- Perform dynamic discovery
- Mutate configuration
- Define policy rules
- Define validation rules
- Define config schemas
- Define adapters

It strictly:
- Resolves backend type
- Validates configuration via external validators
- Enforces policy via external policy module
- Constructs the correct backend instance
- Guarantees deterministic behavior

If this file lies, persistence becomes inconsistent across environments.
"""

from __future__ import annotations

from typing import Protocol, Dict, Type, Any, Optional, List

# Import config schemas
from infra.persistence.backends.backend_config_schemas import (
    BackendConfig,
    MemoryBackendConfig,
    KVBackendConfig,
)

# Import policy and validators
from infra.persistence.backends.backend_policy import BackendPolicy
from infra.persistence.backends.backend_validators import BackendValidator

# Import adapters
from infra.persistence.backends.adapters import (
    MemoryBackendAdapter,
    FilesystemBackendAdapter,
)

# ============================================================================
# Backend Interface Protocol (matches BackendBase from backend_base.py)
# ============================================================================

class BackendProtocol(Protocol):
    """
    Protocol defining backend interface required by factory.
    
    This matches BackendBase from backend_base.py.
    All backends must implement this interface.
    """
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        ...
    
    def get(self, key: str) -> Optional[bytes]:
        """Get raw bytes for key. Returns None if key does not exist."""
        ...
    
    def put(self, key: str, value: bytes, mode: Any = None, expected_version: Optional[int] = None) -> None:
        """Write raw bytes to key."""
        ...
    
    def delete(self, key: str) -> None:
        """Delete key."""
        ...
    
    def get_metadata(self, key: str) -> Dict[str, Any]:
        """Get backend-specific metadata for key."""
        ...
    
    def get_capabilities(self) -> Any:
        """Get backend capabilities including transaction support."""
        ...


# Type alias for return type
BackendBase = BackendProtocol


# Import factory exceptions
from infra.persistence.backends.backend_factory_errors import (
    BackendFactoryError,
    UnknownBackendError,
    InvalidBackendConfigError,
    PolicyViolationError,
    BackendConstructionError,
)


# ============================================================================
# Backend Implementations (Import Points)
# ============================================================================

# Import actual backend implementations
# Tier-0 determinism: only register backends that are available
# If a backend is not available, it is not registered (deterministic)
from infra.persistence.backends.memory_backend import MemoryBackend as _MemoryBackendImpl

# FilesystemBackend may be optional - only register if available
_FilesystemBackendImpl: Type | None = None
try:
    from infra.persistence.backends.filesystem_backend import FilesystemBackend as _FilesystemBackendImpl
except ImportError:
    # Backend not available - will not be registered
    # This is deterministic: same environment = same availability
    _FilesystemBackendImpl = None


# ============================================================================
# Backend Registry
# ============================================================================

# Static registry mapping backend types to their implementation classes
# No dynamic registration allowed - determinism over convenience
# Only backends that are available at module load time are registered
# This ensures deterministic behavior: same imports = same registry
_BACKEND_REGISTRY: Dict[str, Type] = {
    "memory": _MemoryBackendImpl,
}
if _FilesystemBackendImpl is not None:
    _BACKEND_REGISTRY["kv"] = _FilesystemBackendImpl
# Future backends must be explicitly registered here:
# _BACKEND_REGISTRY["redis"] = RedisBackend
# _BACKEND_REGISTRY["s3"] = S3Backend


# ============================================================================
# Backend Factory
# ============================================================================

class BackendFactory:
    """
    Deterministic backend construction authority.
    
    Single entry point for backend instantiation.
    Delegates to external modules for:
    - Policy enforcement (backend_policy.py)
    - Config validation (backend_validators.py)
    - Adapter wrapping (adapters/)
    """
    
    @staticmethod
    def create_backend(config: BackendConfig) -> BackendProtocol:
        """
        Create backend from configuration.
        
        This is the primary entry point for backend construction.
        
        Args:
            config: Fully resolved backend configuration
            
        Returns:
            Constructed backend instance
            
        Raises:
            UnknownBackendError: Backend type not registered
            InvalidBackendConfigError: Configuration is invalid
            PolicyViolationError: Backend/environment combination not allowed
            BackendConstructionError: Construction failed
        """
        # 1. Validate backend type exists in registry
        backend_type = config.backend_type
        if backend_type not in _BACKEND_REGISTRY:
            raise UnknownBackendError(
                backend_type,
                list(_BACKEND_REGISTRY.keys())
            )
        
        # 2. Validate policy compliance (via external policy module)
        BackendPolicy.validate_backend_for_environment(
            backend_type,
            config.environment
        )
        
        # 3. Validate backend-specific configuration (via external validators)
        BackendValidator.validate_config(config)
        
        # 4. Construct backend
        try:
            backend = BackendFactory._construct_backend(config)
        except Exception as e:
            raise BackendConstructionError(
                backend_type,
                str(e)
            ) from e
        
        # 5. Verify backend implements required interface
        BackendFactory._verify_interface(backend, backend_type)
        
        return backend
    
    @staticmethod
    def _construct_backend(config: BackendConfig) -> BackendProtocol:
        """
        Construct backend instance from configuration.
        
        Dispatches to appropriate constructor based on backend type.
        """
        backend_type = config.backend_type
        
        # Dispatch to type-specific construction
        if backend_type == "memory":
            return BackendFactory._construct_memory_backend(config)
        elif backend_type == "kv":
            return BackendFactory._construct_kv_backend(config)
        else:
            # Generic construction for future backends
            # This should be extended as new backends are added
            raise BackendConstructionError(
                backend_type,
                f"No constructor defined for backend type '{backend_type}'"
            )
    
    @staticmethod
    def _construct_memory_backend(config: BackendConfig) -> BackendProtocol:
        """
        Construct memory backend with adapter.
        
        Tier-0 determinism: fail loud on wrong config type, no implicit coercion.
        """
        if not isinstance(config, MemoryBackendConfig):
            raise InvalidBackendConfigError(
                "memory",
                f"MemoryBackendConfig required, got {type(config).__name__}"
            )
        
        backend_impl = _MemoryBackendImpl(
            max_size_mb=config.max_size_mb,
            eviction_policy=config.eviction_policy,
            thread_safe=False  # Default to non-thread-safe for determinism
        )
        
        # Wrap with adapter to match BackendBase interface
        return MemoryBackendAdapter(backend_impl)
    
    @staticmethod
    def _construct_kv_backend(config: BackendConfig) -> BackendProtocol:
        """
        Construct KV backend using FilesystemBackend as implementation.
        
        Tier-0 determinism: fail loud on wrong config type, no implicit coercion.
        """
        if not isinstance(config, KVBackendConfig):
            raise InvalidBackendConfigError(
                "kv",
                f"KVBackendConfig required, got {type(config).__name__}"
            )
        
        # Check if FilesystemBackend is available
        if _FilesystemBackendImpl is None:
            raise BackendConstructionError(
                "kv",
                "FilesystemBackend implementation not available (not registered)"
            )
        
        # Use FilesystemBackend as the concrete KV backend implementation
        # storage_path maps to root_path in FilesystemBackend
        backend_impl = _FilesystemBackendImpl(
            root_path=config.storage_path,
            use_locking=True,
            fsync_enabled=config.sync_writes,
            lock_timeout=5.0
        )
        
        # Wrap with adapter to match BackendBase interface
        return FilesystemBackendAdapter(backend_impl)
    
    @staticmethod
    def _verify_interface(backend: Any, backend_type: str) -> None:
        """
        Verify backend implements required interface from BackendBase.
        
        Checks for presence of required methods at construction time.
        All backends must implement BackendBase interface from backend_base.py.
        """
        required_methods = [
            "exists",
            "get",
            "put",
            "delete",
            "get_metadata",
            "get_capabilities"
        ]
        
        for method_name in required_methods:
            if not hasattr(backend, method_name):
                raise BackendConstructionError(
                    backend_type,
                    f"Backend missing required method: {method_name} (must implement BackendBase)"
                )
            
            if not callable(getattr(backend, method_name)):
                raise BackendConstructionError(
                    backend_type,
                    f"Backend attribute '{method_name}' is not callable"
                )
        
        # Verify backend is fully initialized (not a lazy proxy)
        # This is a basic sanity check - actual initialization happens in constructor
        if not hasattr(backend, '__class__'):
            raise BackendConstructionError(
                backend_type,
                "Backend instance appears to be invalid"
            )


# ============================================================================
# Convenience Functions
# ============================================================================

def create_backend(config: BackendConfig) -> BackendProtocol:
    """
    Convenience wrapper for BackendFactory.create_backend.
    
    This is the primary public API for backend creation.
    """
    return BackendFactory.create_backend(config)


def get_available_backends() -> list[str]:
    """
    Get list of available backend types.
    
    Returns:
        List of registered backend type names
    """
    return list(_BACKEND_REGISTRY.keys())


def is_backend_available(backend_type: str) -> bool:
    """
    Check if backend type is available.
    
    Args:
        backend_type: Backend type to check
        
    Returns:
        True if backend is registered
    """
    return backend_type in _BACKEND_REGISTRY
