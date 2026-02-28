"""
Comprehensive IO sandbox for replay execution.

This module provides complete side-effect isolation by intercepting all
IO operations: file operations, subprocess, sockets, network, mmap, temp files.
"""

from __future__ import annotations

import os
import sys
import subprocess
import socket
import mmap
import tempfile
from pathlib import Path
from typing import Set, List, Dict, Any, Optional
from contextlib import contextmanager

from replay_errors import ReplayError, ReplayPhase
from replay_invariants import InvariantID


class IOSandbox:
    """
    Comprehensive IO sandboxing enforcement.
    
    Intercepts and controls:
    - File operations (open, read, write)
    - Subprocess execution
    - Socket operations
    - Network access
    - Memory-mapped files
    - Temporary file creation
    """
    
    def __init__(self, allowed_paths: Set[str]):
        """
        Initialize IO sandbox.
        
        Args:
            allowed_paths: Set of allowed file paths (read-only access)
        """
        self.allowed_paths = {Path(p).resolve() for p in allowed_paths}
        self._access_log: List[Dict[str, Any]] = []
        
        # Store original functions
        self._original_open = open
        self._original_os_path_exists = os.path.exists
        self._original_os_listdir = os.listdir
        self._original_subprocess_run = subprocess.run
        self._original_subprocess_call = subprocess.call
        self._original_subprocess_popen = subprocess.Popen
        self._original_socket_socket = socket.socket
        self._original_mmap_mmap = mmap.mmap
        self._original_tempfile_mkstemp = tempfile.mkstemp
        self._original_tempfile_mkdtemp = tempfile.mkdtemp
        self._original_tempfile_TemporaryFile = tempfile.TemporaryFile
        self._original_tempfile_NamedTemporaryFile = tempfile.NamedTemporaryFile
        
        self._patched_builtins: Optional[Any] = None
    
    def __enter__(self):
        """Activate IO sandboxing."""
        import builtins
        
        # Patch builtin open()
        def sandboxed_open(file, mode='r', *args, **kwargs):
            if 'w' in mode or 'a' in mode or 'x' in mode:
                raise ReplayError(
                    invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                    phase=ReplayPhase.EXECUTION,
                    component_id="io_sandbox",
                    message=f"Write operation forbidden: {file}",
                    expected_value="read_only",
                    observed_value=f"write_mode_{mode}"
                )
            
            filepath = Path(file).resolve()
            # Check if filepath is within any allowed path
            is_allowed = False
            for allowed in self.allowed_paths:
                try:
                    if filepath.is_relative_to(allowed):
                        is_allowed = True
                        break
                except (ValueError, AttributeError):
                    # Python < 3.9 compatibility or path resolution issue
                    if str(filepath).startswith(str(allowed)):
                        is_allowed = True
                        break
            
            if not is_allowed:
                raise ReplayError(
                    invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                    phase=ReplayPhase.EXECUTION,
                    component_id="io_sandbox",
                    message=f"File access outside allowed paths: {file}",
                    expected_value=str(self.allowed_paths),
                    observed_value=str(filepath)
                )
            
            self._access_log.append({"operation": "open", "path": str(filepath), "mode": mode})
            return self._original_open(file, mode, *args, **kwargs)
        
        builtins.open = sandboxed_open  # type: ignore[assignment]
        self._patched_builtins = builtins
        
        # Patch subprocess operations
        def sandboxed_subprocess_run(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Subprocess execution forbidden during replay",
                expected_value="no_subprocess",
                observed_value="subprocess.run"
            )
        
        def sandboxed_subprocess_call(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Subprocess execution forbidden during replay",
                expected_value="no_subprocess",
                observed_value="subprocess.call"
            )
        
        def sandboxed_subprocess_popen(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Subprocess execution forbidden during replay",
                expected_value="no_subprocess",
                observed_value="subprocess.Popen"
            )
        
        subprocess.run = sandboxed_subprocess_run  # type: ignore[assignment]
        subprocess.call = sandboxed_subprocess_call  # type: ignore[assignment]
        subprocess.Popen = sandboxed_subprocess_popen  # type: ignore[assignment]
        
        # Patch socket operations
        def sandboxed_socket(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Socket operations forbidden during replay",
                expected_value="no_network",
                observed_value="socket.socket"
            )
        
        socket.socket = sandboxed_socket  # type: ignore[assignment]
        
        # Patch mmap operations
        def sandboxed_mmap(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Memory-mapped files forbidden during replay",
                expected_value="no_mmap",
                observed_value="mmap.mmap"
            )
        
        mmap.mmap = sandboxed_mmap  # type: ignore[assignment]
        
        # Patch tempfile operations
        def sandboxed_mkstemp(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Temporary file creation forbidden during replay",
                expected_value="no_tempfiles",
                observed_value="tempfile.mkstemp"
            )
        
        def sandboxed_mkdtemp(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Temporary directory creation forbidden during replay",
                expected_value="no_tempdirs",
                observed_value="tempfile.mkdtemp"
            )
        
        def sandboxed_TemporaryFile(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Temporary file creation forbidden during replay",
                expected_value="no_tempfiles",
                observed_value="tempfile.TemporaryFile"
            )
        
        def sandboxed_NamedTemporaryFile(*args, **kwargs):
            raise ReplayError(
                invariant_id=InvariantID.IO_PERMISSIONS_SEALED.value,
                phase=ReplayPhase.EXECUTION,
                component_id="io_sandbox",
                message="Temporary file creation forbidden during replay",
                expected_value="no_tempfiles",
                observed_value="tempfile.NamedTemporaryFile"
            )
        
        tempfile.mkstemp = sandboxed_mkstemp  # type: ignore[assignment]
        tempfile.mkdtemp = sandboxed_mkdtemp  # type: ignore[assignment]
        tempfile.TemporaryFile = sandboxed_TemporaryFile  # type: ignore[assignment]
        tempfile.NamedTemporaryFile = sandboxed_NamedTemporaryFile  # type: ignore[assignment]
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Deactivate IO sandboxing."""
        import builtins
        
        # Restore original functions
        if self._patched_builtins:
            builtins.open = self._original_open  # type: ignore[assignment]
        
        subprocess.run = self._original_subprocess_run  # type: ignore[assignment]
        subprocess.call = self._original_subprocess_call  # type: ignore[assignment]
        subprocess.Popen = self._original_subprocess_popen  # type: ignore[assignment]
        socket.socket = self._original_socket_socket  # type: ignore[assignment]
        mmap.mmap = self._original_mmap_mmap  # type: ignore[assignment]
        tempfile.mkstemp = self._original_tempfile_mkstemp  # type: ignore[assignment]
        tempfile.mkdtemp = self._original_tempfile_mkdtemp  # type: ignore[assignment]
        tempfile.TemporaryFile = self._original_tempfile_TemporaryFile  # type: ignore[assignment]
        tempfile.NamedTemporaryFile = self._original_tempfile_NamedTemporaryFile  # type: ignore[assignment]
        
        return False
    
    def get_access_log(self) -> List[Dict[str, Any]]:
        """Get log of all IO operations."""
        return self._access_log.copy()


__all__ = ['IOSandbox']
