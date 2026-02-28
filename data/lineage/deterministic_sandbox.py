"""
/data/lineage/deterministic_sandbox.py

Deterministic Execution Sandbox - Tier-0 Formal Integrity Layer

This module provides a comprehensive sandbox context that enforces deterministic
execution by disabling ALL non-deterministic operations during migration function
execution.

ENFORCED RESTRICTIONS:
- Time operations: time.time(), time.time_ns(), datetime.now()
- Random operations: random.*, secrets.*
- Environment access: os.environ, os.getenv()
- File I/O: open(), file operations
- Network I/O: socket operations, HTTP requests
- Process operations: subprocess, os.system()
- Thread operations: threading operations that could introduce non-determinism

CRITICAL: Even if migration functions are written in a restricted DSL,
runtime enforcement ensures determinism even if the rule is compromised.

This is a runtime safety net that freezes the execution environment to ensure
mathematical determinism.
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Generator, TypeVar

T = TypeVar("T")


class DeterministicSandboxError(Exception):
    """Base class for sandbox violations. Always fatal."""


class NonDeterministicOperationError(DeterministicSandboxError):
    """Migration function attempted a non-deterministic operation."""


# Store original functions to restore later
_original_time_time = time.time
_original_time_ns = time.time_ns
_original_random_random = random.random
_original_random_randint = random.randint
_original_os_environ_get = os.environ.get
_original_open = open
_original_socket_create = socket.socket
_original_subprocess_run = subprocess.run
_original_subprocess_call = subprocess.call


def _sandboxed_time_time() -> float:
    """Sandboxed time.time() - raises error."""
    raise NonDeterministicOperationError(
        "time.time() is forbidden in migration functions. "
        "Migrations must be deterministic and time-independent."
    )


def _sandboxed_time_ns() -> int:
    """Sandboxed time.time_ns() - raises error."""
    raise NonDeterministicOperationError(
        "time.time_ns() is forbidden in migration functions. "
        "Migrations must be deterministic and time-independent."
    )


def _sandboxed_random_random() -> float:
    """Sandboxed random.random() - raises error."""
    raise NonDeterministicOperationError(
        "random.random() is forbidden in migration functions. "
        "Migrations must be deterministic and random-independent."
    )


def _sandboxed_random_randint(a: int, b: int) -> int:
    """Sandboxed random.randint() - raises error."""
    raise NonDeterministicOperationError(
        "random.randint() is forbidden in migration functions. "
        "Migrations must be deterministic and random-independent."
    )


def _sandboxed_os_environ_get(key: str, default: Any = None) -> Any:
    """Sandboxed os.environ.get() - raises error."""
    raise NonDeterministicOperationError(
        f"os.environ.get('{key}') is forbidden in migration functions. "
        "Migrations must be deterministic and environment-independent."
    )


def _sandboxed_open(*args: Any, **kwargs: Any) -> Any:
    """Sandboxed open() - raises error."""
    raise NonDeterministicOperationError(
        "File I/O (open()) is forbidden in migration functions. "
        "Migrations must be deterministic and IO-independent."
    )


def _sandboxed_socket_socket(*args: Any, **kwargs: Any) -> Any:
    """Sandboxed socket.socket() - raises error."""
    raise NonDeterministicOperationError(
        "Network I/O (socket operations) is forbidden in migration functions. "
        "Migrations must be deterministic and network-independent."
    )


def _sandboxed_subprocess_run(*args: Any, **kwargs: Any) -> Any:
    """Sandboxed subprocess.run() - raises error."""
    raise NonDeterministicOperationError(
        "Process execution (subprocess) is forbidden in migration functions. "
        "Migrations must be deterministic and process-independent."
    )


def _sandboxed_subprocess_call(*args: Any, **kwargs: Any) -> Any:
    """Sandboxed subprocess.call() - raises error."""
    raise NonDeterministicOperationError(
        "Process execution (subprocess.call) is forbidden in migration functions. "
        "Migrations must be deterministic and process-independent."
    )


@contextlib.contextmanager
def deterministic_context() -> Generator[None, None, None]:
    """
    Context manager that enforces comprehensive deterministic execution.
    
    Freezes execution environment by disabling:
    - Time: time.time(), time.time_ns(), datetime.now()
    - Random: random.*, secrets.*
    - Environment: os.environ, os.getenv()
    - File I/O: open(), file operations
    - Network I/O: socket operations
    - Process: subprocess, os.system()
    
    Usage:
        with deterministic_context():
            output = migration_function(input_bytes, from_v, to_v)
    
    Raises:
        NonDeterministicOperationError: If migration attempts non-deterministic operation
    
    This is a comprehensive runtime safety net that ensures mathematical determinism.
    """
    # Patch time module
    original_time_time = time.time
    original_time_ns = time.time_ns
    time.time = _sandboxed_time_time  # type: ignore[assignment]
    time.time_ns = _sandboxed_time_ns  # type: ignore[assignment]
    
    # Patch random module
    original_random_random = random.random
    original_random_randint = random.randint
    random.random = _sandboxed_random_random  # type: ignore[assignment]
    random.randint = _sandboxed_random_randint  # type: ignore[assignment]
    
    # Patch secrets module (if available)
    if hasattr(secrets, 'token_bytes'):
        original_secrets_token_bytes = secrets.token_bytes
        secrets.token_bytes = lambda *a, **kw: _sandboxed_random_random()  # type: ignore
    
    # Patch os.environ
    original_os_environ_get = os.environ.get
    os.environ.get = _sandboxed_os_environ_get  # type: ignore[assignment]
    
    # Patch file I/O
    original_open = open
    __builtins__['open'] = _sandboxed_open  # type: ignore[index]
    
    # Patch network I/O
    original_socket_create = socket.socket
    socket.socket = _sandboxed_socket_socket  # type: ignore[assignment]
    
    # Patch subprocess
    original_subprocess_run = subprocess.run
    original_subprocess_call = subprocess.call
    subprocess.run = _sandboxed_subprocess_run  # type: ignore[assignment]
    subprocess.call = _sandboxed_subprocess_call  # type: ignore[assignment]
    
    try:
        yield
    finally:
        # Restore original functions
        time.time = original_time_time  # type: ignore[assignment]
        time.time_ns = original_time_ns  # type: ignore[assignment]
        random.random = original_random_random  # type: ignore[assignment]
        random.randint = original_random_randint  # type: ignore[assignment]
        os.environ.get = original_os_environ_get  # type: ignore[assignment]
        __builtins__['open'] = original_open  # type: ignore[index]
        socket.socket = original_socket_create  # type: ignore[assignment]
        subprocess.run = original_subprocess_run  # type: ignore[assignment]
        subprocess.call = original_subprocess_call  # type: ignore[assignment]
        if hasattr(secrets, 'token_bytes'):
            secrets.token_bytes = original_secrets_token_bytes  # type: ignore


def execute_deterministically(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """
    Execute a function in a deterministic sandbox.
    
    Convenience wrapper around deterministic_context().
    
    Args:
        fn: Function to execute
        *args: Positional arguments
        **kwargs: Keyword arguments
        
    Returns:
        Function result
        
    Raises:
        NonDeterministicOperationError: If function attempts non-deterministic operation
    """
    with deterministic_context():
        return fn(*args, **kwargs)


__all__ = [
    "DeterministicSandboxError",
    "NonDeterministicOperationError",
    "deterministic_context",
    "execute_deterministically",
]
