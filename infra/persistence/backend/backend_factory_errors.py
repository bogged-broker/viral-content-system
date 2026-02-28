from typing import List
"""
Backend Factory Exceptions

Exception classes for backend factory operations.
Separated to avoid circular imports.
"""


class BackendFactoryError(Exception):
    """Base exception for backend factory failures."""
    pass


class UnknownBackendError(BackendFactoryError):
    """Backend type is not registered."""
    def __init__(self, backend_type: str, available: list[str]):
        super().__init__(
            f"Unknown backend type '{backend_type}'. "
            f"Available backends: {', '.join(available)}"
        )
        self.backend_type = backend_type
        self.available = available


class InvalidBackendConfigError(BackendFactoryError):
    """Backend configuration is invalid or incomplete."""
    def __init__(self, backend_type: str, reason: str):
        super().__init__(
            f"Invalid configuration for backend '{backend_type}': {reason}"
        )
        self.backend_type = backend_type
        self.reason = reason


class PolicyViolationError(BackendFactoryError):
    """Backend/environment combination violates policy."""
    def __init__(self, backend_type: str, environment: str, reason: str):
        super().__init__(
            f"Policy violation: backend '{backend_type}' in environment "
            f"'{environment}' - {reason}"
        )
        self.backend_type = backend_type
        self.environment = environment
        self.reason = reason


class BackendConstructionError(BackendFactoryError):
    """Backend construction failed."""
    def __init__(self, backend_type: str, reason: str):
        super().__init__(
            f"Failed to construct backend '{backend_type}': {reason}"
        )
        self.backend_type = backend_type
        self.reason = reason
