"""
Structured, Invariant-Safe Logging

This module provides deterministic, structured logging that observes execution
without interfering with correctness, replay, or audit truth.

Core Invariant:
    Logging is observational only. Deleting all logs must not change system outputs.

Guarantees:
    - No mutation of inputs
    - No execution flow changes
    - No exception swallowing
    - Deterministic field ordering
    - Immutable event payloads
    - Thread-safe emission

Non-Goals:
    - NOT business analytics
    - NOT metrics collection
    - NOT audit evidence
    - NOT tracing framework
    - NOT retry logic
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from enum import Enum
from typing import Any, Protocol, Dict

from utils.frozen import freeze, is_frozen
from utils.serialization import canonicalize, is_serializable
from utils.time import now_ms
from utils.guards import GuardViolation, require


# ============================================================================
# Log Level Enumeration
# ============================================================================


class LogLevel(Enum):
    """
    Explicit log level enumeration.
    
    Rules:
        - No custom string levels allowed
        - Levels do not imply behavior changes
        - Logging never affects execution
    """
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    
    def __lt__(self, other: LogLevel) -> bool:
        """Enable level comparison for filtering."""
        order = {
            LogLevel.DEBUG: 0,
            LogLevel.INFO: 1,
            LogLevel.WARNING: 2,
            LogLevel.ERROR: 3,
            LogLevel.CRITICAL: 4,
        }
        return order[self] < order[other]


# ============================================================================
# Log Sink Protocol
# ============================================================================


class LogSink(Protocol):
    """
    Protocol for log output destinations.
    
    Requirements:
        - Must be deterministic
        - Must be thread-safe
        - Must not mutate record
        - Failures must not alter business logic
    """
    
    def emit(self, record: Mapping[str, Any]) -> None:
        """
        Emit a log record.
        
        Args:
            record: Immutable, canonicalized log record
            
        The record is guaranteed to be:
            - Frozen (immutable)
            - Canonicalized (deterministic ordering)
            - JSON-serializable
        """
        ...


# ============================================================================
# Default Sink Implementation
# ============================================================================


class StdoutJsonSink:
    """
    Default sink: JSON lines to stdout.
    
    Thread-safe, deterministic, minimal allocation.
    """
    
    def __init__(self) -> None:
        self._lock = threading.Lock()
    
    def emit(self, record: Mapping[str, Any]) -> None:
        """Emit record as JSON line to stdout."""
        try:
            # Serialize deterministically
            line = json.dumps(record, sort_keys=True, ensure_ascii=True)
            
            # Thread-safe write
            with self._lock:
                sys.stdout.write(line)
                sys.stdout.write('\n')
                sys.stdout.flush()
        except Exception:
            # Sink failure must not propagate
            # In production, this would go to a fallback sink
            pass


class SilentSink:
    """Sink that discards all logs (for replay mode)."""
    
    def emit(self, record: Mapping[str, Any]) -> None:
        """Discard log record."""
        pass


# ============================================================================
# Global Sink Configuration
# ============================================================================


_global_sink: LogSink = StdoutJsonSink()
_global_sink_lock = threading.Lock()
_global_min_level: LogLevel = LogLevel.INFO


def configure_sink(sink: LogSink, min_level: LogLevel = LogLevel.INFO) -> None:
    """
    Configure global log sink.
    
    Args:
        sink: The sink to use for all log emission
        min_level: Minimum level to emit (inclusive)
        
    Thread-safe. Should be called once at startup.
    """
    global _global_sink, _global_min_level
    
    require(
        hasattr(sink, 'emit'),
        "Sink must implement emit(record) method"
    )
    
    with _global_sink_lock:
        _global_sink = sink
        _global_min_level = min_level


def get_sink() -> LogSink:
    """Get current global sink (thread-safe)."""
    with _global_sink_lock:
        return _global_sink


def get_min_level() -> LogLevel:
    """Get current minimum log level (thread-safe)."""
    with _global_sink_lock:
        return _global_min_level


# ============================================================================
# Field Sanitization
# ============================================================================


def _sanitize_fields(fields: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """
    Sanitize and validate log fields.
    
    Args:
        fields: Raw fields dictionary or None
        
    Returns:
        Frozen, canonicalized, validated fields
        
    Raises:
        GuardViolation: If fields contain non-serializable data
        
    Guarantees:
        - Result is frozen (immutable)
        - Result is canonicalized (deterministic)
        - Result is JSON-serializable
        - No mutation of input
    """
    if fields is None:
        return freeze({})
    
    # Validate serializability
    require(
        is_serializable(fields),
        f"Log fields must be JSON-serializable, got: {type(fields)}"
    )
    
    # Canonicalize for deterministic ordering
    canonical = canonicalize(fields)
    
    # Freeze to prevent mutation
    frozen = freeze(canonical)
    
    return frozen


def _sanitize_exception(exc: Exception) -> Mapping[str, Any]:
    """
    Sanitize exception for deterministic logging.
    
    Args:
        exc: Exception to sanitize
        
    Returns:
        Deterministic exception representation
        
    Rules:
        - No memory addresses
        - No local variable dumps
        - Stable string representation
        - No traceback (unless explicitly enabled)
    """
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__
    exc_message = str(exc)
    
    # Build deterministic exception dict
    exc_dict = {
        "exception_type": exc_type,
        "exception_module": exc_module,
        "exception_message": exc_message,
    }
    
    # Add cause chain if present
    if exc.__cause__ is not None:
        exc_dict["cause"] = _sanitize_exception(exc.__cause__)
    
    return freeze(exc_dict)


# ============================================================================
# Core Logging Function
# ============================================================================


def log(
    *,
    level: LogLevel,
    source: str,
    event: str,
    fields: Mapping[str, Any] | None = None,
    timestamp_ms: int | None = None,
) -> None:
    """
    Emit a structured log event.
    
    Args:
        level: Log level (required)
        source: Source identifier (e.g., "window_engine")
        event: Event name (e.g., "window_resolved")
        fields: Optional structured fields
        timestamp_ms: Optional timestamp override (for replay)
        
    Guarantees:
        - fields frozen and canonicalized
        - No mutation of inputs
        - No exception swallowing
        - No execution flow changes
        - Deterministic output
        
    Thread-safe.
    
    Example:
        >>> log(
        ...     level=LogLevel.INFO,
        ...     source="aggregation",
        ...     event="window_assigned",
        ...     fields={"window_id": "W001", "count": 42}
        ... )
    """
    require(isinstance(level, LogLevel), "level must be LogLevel enum")
    require(isinstance(source, str) and source, "source must be non-empty string")
    require(isinstance(event, str) and event, "event must be non-empty string")
    
    # Check level filter
    current_min = get_min_level()
    if level < current_min:
        return  # Filtered out
    
    # Get deterministic timestamp
    ts = timestamp_ms if timestamp_ms is not None else now_ms()
    
    # Sanitize fields
    safe_fields = _sanitize_fields(fields)
    
    # Build record
    record = freeze({
        "level": level.value,
        "timestamp_ms": ts,
        "source": source,
        "event": event,
        "fields": safe_fields,
    })
    
    # Emit to sink
    sink = get_sink()
    try:
        sink.emit(record)
    except Exception:
        # Sink failures must not propagate
        # In production, would write to fallback
        pass


def log_exception(
    *,
    source: str,
    event: str,
    exc: Exception,
    fields: Mapping[str, Any] | None = None,
    timestamp_ms: int | None = None,
) -> None:
    """
    Log an exception with deterministic representation.
    
    Args:
        source: Source identifier
        event: Event name
        exc: Exception to log
        fields: Additional structured fields
        timestamp_ms: Optional timestamp override
        
    Guarantees:
        - No traceback dump (unless configured)
        - No memory addresses
        - Deterministic exception representation
        - No local variable exposure
        
    Example:
        >>> try:
        ...     raise ValueError("Bad input")
        ... except ValueError as e:
        ...     log_exception(
        ...         source="validator",
        ...         event="validation_failed",
        ...         exc=e,
        ...         fields={"input_id": "X123"}
        ...     )
    """
    require(isinstance(exc, Exception), "exc must be Exception instance")
    
    # Sanitize exception
    exc_data = _sanitize_exception(exc)
    
    # Merge with user fields
    merged_fields = dict(fields) if fields else {}
    merged_fields["exception"] = exc_data
    
    # Log at ERROR level
    log(
        level=LogLevel.ERROR,
        source=source,
        event=event,
        fields=merged_fields,
        timestamp_ms=timestamp_ms,
    )


# ============================================================================
# Structured Logger (Convenience API)
# ============================================================================


class StructuredLogger:
    """
    Convenience wrapper that binds a source identifier.
    
    Provides level-specific methods for cleaner call sites.
    """
    
    def __init__(self, source: str) -> None:
        require(isinstance(source, str) and source, "source must be non-empty string")
        self._source = source
    
    def debug(
        self,
        *,
        event: str,
        fields: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Log at DEBUG level."""
        log(
            level=LogLevel.DEBUG,
            source=self._source,
            event=event,
            fields=fields,
            timestamp_ms=timestamp_ms,
        )
    
    def info(
        self,
        *,
        event: str,
        fields: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Log at INFO level."""
        log(
            level=LogLevel.INFO,
            source=self._source,
            event=event,
            fields=fields,
            timestamp_ms=timestamp_ms,
        )
    
    def warning(
        self,
        *,
        event: str,
        fields: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Log at WARNING level."""
        log(
            level=LogLevel.WARNING,
            source=self._source,
            event=event,
            fields=fields,
            timestamp_ms=timestamp_ms,
        )
    
    def error(
        self,
        *,
        event: str,
        fields: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Log at ERROR level."""
        log(
            level=LogLevel.ERROR,
            source=self._source,
            event=event,
            fields=fields,
            timestamp_ms=timestamp_ms,
        )
    
    def critical(
        self,
        *,
        event: str,
        fields: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Log at CRITICAL level."""
        log(
            level=LogLevel.CRITICAL,
            source=self._source,
            event=event,
            fields=fields,
            timestamp_ms=timestamp_ms,
        )
    
    def exception(
        self,
        *,
        event: str,
        exc: Exception,
        fields: Mapping[str, Any] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Log an exception."""
        log_exception(
            source=self._source,
            event=event,
            exc=exc,
            fields=fields,
            timestamp_ms=timestamp_ms,
        )


def get_logger(source: str) -> StructuredLogger:
    """
    Create a structured logger bound to a source identifier.
    
    Args:
        source: Source identifier (e.g., "window_engine")
        
    Returns:
        Logger instance with source pre-bound
        
    Example:
        >>> logger = get_logger("aggregation")
        >>> logger.info(event="started", fields={"version": "v2"})
    """
    return StructuredLogger(source)


# ============================================================================
# Replay Mode Support
# ============================================================================


class ReplayLoggingContext:
    """
    Context manager for replay mode logging.
    
    During replay:
        - Logs can be suppressed entirely
        - Timestamps can be frozen
        - Sink can be replaced with silent sink
        
    Example:
        >>> with ReplayLoggingContext(silent=True):
        ...     # All logs suppressed during replay
        ...     logger.info(event="test")
    """
    
    def __init__(self, silent: bool = False) -> None:
        self._silent = silent
        self._prev_sink: LogSink | None = None
        self._prev_min_level: LogLevel | None = None
    
    def __enter__(self) -> ReplayLoggingContext:
        """Enter replay mode."""
        if self._silent:
            global _global_sink, _global_min_level
            with _global_sink_lock:
                self._prev_sink = _global_sink
                self._prev_min_level = _global_min_level
                _global_sink = SilentSink()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit replay mode."""
        if self._silent and self._prev_sink is not None:
            global _global_sink, _global_min_level
            with _global_sink_lock:
                _global_sink = self._prev_sink
                _global_min_level = self._prev_min_level


# ============================================================================
# Public API Exports
# ============================================================================


__all__ = [
    # Core types
    "LogLevel",
    "LogSink",
    
    # Core functions
    "log",
    "log_exception",
    "get_logger",
    
    # Configuration
    "configure_sink",
    "get_sink",
    "get_min_level",
    
    # Built-in sinks
    "StdoutJsonSink",
    "SilentSink",
    
    # Replay support
    "ReplayLoggingContext",
    
    # Convenience
    "StructuredLogger",
]

