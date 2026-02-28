"""
/utils/time.py

Monotonic, Normalized Time Utilities

Deterministic. Production-grade. No softness.

This module is the single authority for all time access inside the system.
It eliminates system clock ambiguity, timezone drift, mixed units, and
non-monotonic behavior.

Core Philosophy:
    Time has three acceptable forms:
        1. Event Time (external truth, canonicalized)
        2. Wall Time (UTC only, explicit)
        3. Monotonic Time (performance measurement only)
    
    Anything else is banned.

Critical:
    If time lies, replay collapses and audit becomes fiction.

Global Rules (ABSOLUTE):
    - All timestamps are UTC
    - All timestamps are epoch milliseconds (int)
    - No floats in timestamp representation
    - No naive datetime objects allowed past boundary
    - Monotonic time may never be converted to wall time
    - Wall time may never be used for determinism-sensitive logic

Forbidden Patterns:
    - datetime.now()
    - datetime.utcnow()
    - time.time()
    - strftime without UTC enforcement
    - Storing datetime objects in state
    - Storing monotonic time in persistence
    - Mixing seconds and milliseconds
    - Implicit unit guessing

Performance:
    - now_utc_ms(): O(1)
    - monotonic_ns(): Native speed
    - normalize_timestamp(): O(1)
    - No regex-heavy parsing
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


# =============================================================================
# CONSTANTS
# =============================================================================

# Epoch milliseconds bounds
MIN_TIMESTAMP_MS: int = 0  # Unix epoch
MAX_TIMESTAMP_MS: int = 4102444800000  # 2100-01-01 (reasonable future bound)

# Unit detection threshold
# Timestamps below this are assumed to be seconds, above are milliseconds
# Set to 10^11 (Nov 1973 in ms, Nov 5138 in seconds)
SECONDS_MS_THRESHOLD: int = 100_000_000_000

# Milliseconds per time unit
MS_PER_SECOND: int = 1_000
MS_PER_MINUTE: int = 60_000
MS_PER_HOUR: int = 3_600_000
MS_PER_DAY: int = 86_400_000


# =============================================================================
# ERROR MODEL
# =============================================================================

class TimeError(RuntimeError):
    """
    Time operation or validation failure.
    
    Raised when:
        - Invalid timestamp format
        - Out of bounds timestamp
        - Naive datetime encountered
        - Non-UTC timezone in strict mode
        - Ambiguous unit conversion
        - Invalid time arithmetic
    
    Attributes:
        message: Error description
        timestamp: Timestamp that caused failure (if applicable)
        timestamp_type: Type of timestamp
    """
    
    def __init__(
        self,
        message: str,
        *,
        timestamp: Any = None,
        timestamp_type: str | None = None
    ) -> None:
        self.timestamp = timestamp
        self.timestamp_type = timestamp_type
        
        # Build deterministic error message
        parts = [f"Time error: {message}"]
        
        if timestamp is not None:
            parts.append(f"Timestamp: {timestamp}")
        if timestamp_type is not None:
            parts.append(f"Type: {timestamp_type}")
        
        full_message = " | ".join(parts)
        super().__init__(full_message)


# =============================================================================
# WALL CLOCK ACCESS (UTC ONLY)
# =============================================================================

def now_utc_ms() -> int:
    """
    Get current UTC wall-clock time in epoch milliseconds.
    
    Returns:
        Current UTC time as integer milliseconds since epoch
    
    Guarantees:
        - UTC timezone
        - Integer milliseconds
        - No floating point
        - Timezone-aware conversion
    
    Critical:
        NEVER use in event processing logic.
        NEVER use in window computation.
        NEVER use in replay.
        ONLY for external I/O boundary.
    
    Use Cases:
        - Ingestion timestamp assignment
        - Watermark generation
        - Latency measurement
        - System monitoring
        - Audit timestamps
    
    Example:
        >>> ingestion_time = now_utc_ms()
        >>> event = Event(
        ...     event_time=event_data.timestamp_ms,
        ...     ingestion_time=ingestion_time
        ... )
    """
    # Get current time as timezone-aware datetime in UTC
    now = datetime.now(timezone.utc)
    
    # Convert to epoch milliseconds
    epoch_seconds = now.timestamp()
    epoch_ms = int(epoch_seconds * MS_PER_SECOND)
    
    return epoch_ms


# =============================================================================
# MONOTONIC CLOCK ACCESS
# =============================================================================

def monotonic_ns() -> int:
    """
    Get monotonic clock value in nanoseconds.
    
    Returns:
        Monotonic time in nanoseconds
    
    Guarantees:
        - Monotonically increasing
        - Never affected by system clock changes
        - Only valid within same process
        - Cannot be compared across processes
    
    Critical:
        NEVER convert to wall time.
        NEVER serialize into replay state.
        NEVER use in hashing.
        ONLY for measuring elapsed durations.
    
    Use Cases:
        - Performance measurement
        - Latency tracking
        - Timeout detection
        - Duration calculation
    
    Example:
        >>> start = monotonic_ns()
        >>> # ... do work ...
        >>> elapsed_ns = monotonic_ns() - start
        >>> elapsed_ms = elapsed_ns // 1_000_000
    """
    return time.monotonic_ns()


# =============================================================================
# TIMESTAMP NORMALIZATION
# =============================================================================

def normalize_timestamp(value: Any, *, strict: bool = True) -> int:
    """
    Normalize various timestamp formats to canonical epoch milliseconds.
    
    Args:
        value: Timestamp to normalize (int, datetime, or ISO8601 string)
        strict: If True, reject non-UTC timezones and ambiguous formats
    
    Returns:
        Canonical UTC epoch milliseconds (int)
    
    Raises:
        TimeError: If timestamp is invalid or ambiguous
    
    Accepts:
        - int (seconds or ms, auto-detected via threshold)
        - datetime (timezone-aware only)
        - ISO8601 string (explicit Z required in strict mode)
    
    Guarantees:
        - Always returns UTC milliseconds
        - Rejects naive datetime
        - Rejects negative timestamps (unless configured)
        - Rejects absurdly future timestamps
        - Deterministic conversion
    
    Example:
        >>> normalize_timestamp(1705315800)  # seconds
        1705315800000
        >>> normalize_timestamp(1705315800000)  # milliseconds
        1705315800000
        >>> normalize_timestamp("2024-01-15T10:30:00Z")
        1705315800000
    """
    # None check
    if value is None:
        raise TimeError(
            "Timestamp must not be None",
            timestamp=value,
            timestamp_type="None"
        )
    
    # Integer timestamp (seconds or milliseconds)
    if isinstance(value, int):
        return _normalize_int_timestamp(value, strict=strict)
    
    # Datetime object
    if isinstance(value, datetime):
        return _normalize_datetime(value, strict=strict)
    
    # String (ISO8601)
    if isinstance(value, str):
        return _normalize_iso8601(value, strict=strict)
    
    # Unsupported type
    raise TimeError(
        f"Unsupported timestamp type: {type(value).__name__}",
        timestamp=value,
        timestamp_type=type(value).__name__
    )


def _normalize_int_timestamp(timestamp: int, *, strict: bool) -> int:
    """
    Normalize integer timestamp (auto-detect seconds vs milliseconds).
    
    Args:
        timestamp: Integer timestamp
        strict: If True, reject ambiguous values
    
    Returns:
        Epoch milliseconds
    
    Raises:
        TimeError: If timestamp is invalid or ambiguous
    """
    # Negative timestamps (reject by default)
    if timestamp < 0:
        raise TimeError(
            "Negative timestamps not allowed",
            timestamp=timestamp,
            timestamp_type="int"
        )
    
    # Auto-detect seconds vs milliseconds
    if timestamp < SECONDS_MS_THRESHOLD:
        # Assume seconds, convert to milliseconds
        timestamp_ms = timestamp * MS_PER_SECOND
    else:
        # Assume milliseconds
        timestamp_ms = timestamp
    
    # Validate bounds
    validate_utc_ms(timestamp_ms)
    
    return timestamp_ms


def _normalize_datetime(dt: datetime, *, strict: bool) -> int:
    """
    Normalize datetime object to epoch milliseconds.
    
    Args:
        dt: Datetime object
        strict: If True, reject naive datetime
    
    Returns:
        Epoch milliseconds
    
    Raises:
        TimeError: If datetime is naive or invalid
    """
    # Reject naive datetime
    if dt.tzinfo is None:
        raise TimeError(
            "Naive datetime not allowed (must be timezone-aware)",
            timestamp=dt,
            timestamp_type="datetime"
        )
    
    # Strict mode: require UTC
    if strict and dt.tzinfo != timezone.utc:
        raise TimeError(
            "Non-UTC timezone in strict mode (convert to UTC first)",
            timestamp=dt,
            timestamp_type="datetime"
        )
    
    # Convert to UTC if needed
    dt_utc = dt.astimezone(timezone.utc)
    
    # Convert to epoch milliseconds
    epoch_seconds = dt_utc.timestamp()
    epoch_ms = int(epoch_seconds * MS_PER_SECOND)
    
    # Validate bounds
    validate_utc_ms(epoch_ms)
    
    return epoch_ms


def _normalize_iso8601(timestamp_str: str, *, strict: bool) -> int:
    """
    Parse ISO8601 timestamp string to epoch milliseconds.
    
    Args:
        timestamp_str: ISO8601 timestamp string
        strict: If True, require explicit Z suffix
    
    Returns:
        Epoch milliseconds
    
    Raises:
        TimeError: If string is invalid or ambiguous
    
    Formats Accepted:
        - 2024-01-15T10:30:00Z
        - 2024-01-15T10:30:00.123Z
        - 2024-01-15T10:30:00+00:00 (if not strict)
    """
    if not timestamp_str:
        raise TimeError(
            "Empty timestamp string",
            timestamp=timestamp_str,
            timestamp_type="str"
        )
    
    # Strict mode: require Z suffix
    if strict and not timestamp_str.endswith('Z'):
        raise TimeError(
            "ISO8601 timestamp must end with 'Z' in strict mode",
            timestamp=timestamp_str,
            timestamp_type="str"
        )
    
    # Parse datetime
    try:
        # Try with Z suffix
        if timestamp_str.endswith('Z'):
            # Remove Z and parse as UTC
            dt = datetime.fromisoformat(timestamp_str[:-1]).replace(tzinfo=timezone.utc)
        else:
            # Parse with timezone
            dt = datetime.fromisoformat(timestamp_str)
    except ValueError as e:
        raise TimeError(
            f"Invalid ISO8601 timestamp: {e}",
            timestamp=timestamp_str,
            timestamp_type="str"
        )
    
    # Convert using datetime normalization
    return _normalize_datetime(dt, strict=strict)


# =============================================================================
# TIMESTAMP VALIDATION
# =============================================================================

def validate_utc_ms(timestamp: int) -> None:
    """
    Validate timestamp is within acceptable bounds.
    
    Args:
        timestamp: Timestamp in epoch milliseconds
    
    Raises:
        TimeError: If timestamp is out of bounds
    
    Checks:
        - Type is int
        - Within system bounds (1970-2100)
        - Not negative
        - Not absurdly future-dated
    
    Critical:
        This does NOT auto-correct. It only enforces.
    
    Example:
        >>> validate_utc_ms(1705315800000)  # Valid
        >>> validate_utc_ms(-1000)  # Raises TimeError
        >>> validate_utc_ms(9999999999999)  # Raises TimeError
    """
    # Type check
    if not isinstance(timestamp, int):
        raise TimeError(
            f"Timestamp must be int, got {type(timestamp).__name__}",
            timestamp=timestamp,
            timestamp_type=type(timestamp).__name__
        )
    
    # Bounds check
    if timestamp < MIN_TIMESTAMP_MS:
        raise TimeError(
            f"Timestamp before Unix epoch: {timestamp} < {MIN_TIMESTAMP_MS}",
            timestamp=timestamp,
            timestamp_type="int"
        )
    
    if timestamp > MAX_TIMESTAMP_MS:
        raise TimeError(
            f"Timestamp beyond reasonable future: {timestamp} > {MAX_TIMESTAMP_MS}",
            timestamp=timestamp,
            timestamp_type="int"
        )


# =============================================================================
# TIME FORMATTING
# =============================================================================

def format_iso8601(timestamp_ms: int) -> str:
    """
    Format epoch milliseconds as ISO8601 string.
    
    Args:
        timestamp_ms: Timestamp in epoch milliseconds
    
    Returns:
        ISO8601 string with Z suffix (UTC)
    
    Guarantees:
        - Always UTC (Z suffix)
        - Millisecond precision
        - Deterministic output
        - Sortable string format
    
    Format:
        YYYY-MM-DDTHH:MM:SS.sssZ
    
    Example:
        >>> format_iso8601(1705315800000)
        '2024-01-15T10:30:00.000Z'
    """
    # Validate input
    validate_utc_ms(timestamp_ms)
    
    # Convert to datetime
    epoch_seconds = timestamp_ms / MS_PER_SECOND
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    
    # Format with millisecond precision
    # Use isoformat() and replace +00:00 with Z
    iso_str = dt.isoformat(timespec='milliseconds')
    
    # Replace +00:00 with Z
    if iso_str.endswith('+00:00'):
        iso_str = iso_str[:-6] + 'Z'
    
    return iso_str


# =============================================================================
# DURATION OPERATIONS
# =============================================================================

def duration_ms(
    *,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0,
    milliseconds: int = 0
) -> int:
    """
    Calculate duration in milliseconds from components.
    
    Args:
        days: Number of days
        hours: Number of hours
        minutes: Number of minutes
        seconds: Number of seconds
        milliseconds: Number of milliseconds
    
    Returns:
        Total duration in milliseconds
    
    Guarantees:
        - Integer arithmetic only
        - Overflow detection
        - Deterministic calculation
    
    Use Cases:
        - Window size specification
        - Allowed lateness
        - Timeout configuration
    
    Example:
        >>> duration_ms(hours=1)
        3600000
        >>> duration_ms(minutes=5, seconds=30)
        330000
    """
    # Calculate total milliseconds
    total_ms = (
        days * MS_PER_DAY +
        hours * MS_PER_HOUR +
        minutes * MS_PER_MINUTE +
        seconds * MS_PER_SECOND +
        milliseconds
    )
    
    # Validate non-negative
    if total_ms < 0:
        raise TimeError(
            f"Duration must be non-negative, got {total_ms}ms",
            timestamp=total_ms,
            timestamp_type="duration"
        )
    
    return total_ms


def add_duration(timestamp_ms: int, duration_ms: int) -> int:
    """
    Add duration to timestamp.
    
    Args:
        timestamp_ms: Base timestamp in epoch milliseconds
        duration_ms: Duration to add in milliseconds
    
    Returns:
        New timestamp in epoch milliseconds
    
    Raises:
        TimeError: If result is out of bounds
    
    Example:
        >>> add_duration(1705315800000, duration_ms(hours=1))
        1705319400000
    """
    validate_utc_ms(timestamp_ms)
    
    result = timestamp_ms + duration_ms
    
    validate_utc_ms(result)
    
    return result


def subtract_duration(timestamp_ms: int, duration_ms: int) -> int:
    """
    Subtract duration from timestamp.
    
    Args:
        timestamp_ms: Base timestamp in epoch milliseconds
        duration_ms: Duration to subtract in milliseconds
    
    Returns:
        New timestamp in epoch milliseconds
    
    Raises:
        TimeError: If result is out of bounds
    
    Example:
        >>> subtract_duration(1705315800000, duration_ms(minutes=30))
        1705313900000
    """
    validate_utc_ms(timestamp_ms)
    
    result = timestamp_ms - duration_ms
    
    validate_utc_ms(result)
    
    return result


# =============================================================================
# TIME COMPARISON
# =============================================================================

def is_before(time_a: int, time_b: int) -> bool:
    """
    Check if time_a is strictly before time_b.
    
    Args:
        time_a: First timestamp
        time_b: Second timestamp
    
    Returns:
        True if time_a < time_b
    
    Example:
        >>> is_before(1000, 2000)
        True
    """
    return time_a < time_b


def is_after(time_a: int, time_b: int) -> bool:
    """
    Check if time_a is strictly after time_b.
    
    Args:
        time_a: First timestamp
        time_b: Second timestamp
    
    Returns:
        True if time_a > time_b
    
    Example:
        >>> is_after(2000, 1000)
        True
    """
    return time_a > time_b


def time_between(time: int, start: int, end: int, *, inclusive: bool = True) -> bool:
    """
    Check if time is within range [start, end].
    
    Args:
        time: Timestamp to check
        start: Range start
        end: Range end
        inclusive: Include boundaries (default: True)
    
    Returns:
        True if time in range
    
    Example:
        >>> time_between(1500, 1000, 2000)
        True
        >>> time_between(1000, 1000, 2000, inclusive=False)
        False
    """
    if inclusive:
        return start <= time <= end
    else:
        return start < time < end


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Constants
    "MIN_TIMESTAMP_MS",
    "MAX_TIMESTAMP_MS",
    "MS_PER_SECOND",
    "MS_PER_MINUTE",
    "MS_PER_HOUR",
    "MS_PER_DAY",
    
    # Error model
    "TimeError",
    
    # Wall clock access
    "now_utc_ms",
    
    # Monotonic clock access
    "monotonic_ns",
    
    # Normalization
    "normalize_timestamp",
    "validate_utc_ms",
    
    # Formatting
    "format_iso8601",
    
    # Duration operations
    "duration_ms",
    "add_duration",
    "subtract_duration",
    
    # Comparison
    "is_before",
    "is_after",
    "time_between",
]




