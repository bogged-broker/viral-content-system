"""
/infra/persistence/backend/logical_clock.py

Logical Clock for Deterministic Timestamps (Tier-0 Requirement)

This module provides a deterministic timestamp source based on logical
sequence numbers, not wall-clock time.

TIER-0 REQUIREMENT:
    Timestamps must be deterministic for replay guarantees.
    Same journal entries → same timestamps → same replay outcome.

This module answers:
    "What is the deterministic temporal ordering of events?"

Not:
    - "What time is it now?" (wall-clock)
    - "When did this happen in real time?" (external time)
    - "What's a good approximation?" (heuristic)

This is a LOGICAL CLOCK, not a physical clock.
"""

from typing import Optional
from dataclasses import dataclass


@dataclass(frozen=True)
class LogicalTimestamp:
    """
    Deterministic logical timestamp.
    
    TIER-0 CRITICAL: This timestamp is derived from:
    1. Global sequence number (monotonic, deterministic)
    2. Logical clock state (not wall-clock time)
    
    This ensures:
    - Same journal → same timestamps
    - Replay determinism
    - No external time dependence
    """
    sequence: int  # Global sequence number (monotonic)
    nanoseconds: int  # Derived nanoseconds (deterministic)
    
    def __post_init__(self):
        """Validate timestamp."""
        if self.sequence < 0:
            raise ValueError("sequence must be >= 0")
        if self.nanoseconds < 0:
            raise ValueError("nanoseconds must be >= 0")
    
    def __lt__(self, other: "LogicalTimestamp") -> bool:
        """Compare timestamps (monotonic ordering)."""
        if not isinstance(other, LogicalTimestamp):
            return NotImplemented
        return self.sequence < other.sequence
    
    def __le__(self, other: "LogicalTimestamp") -> bool:
        """Compare timestamps."""
        if not isinstance(other, LogicalTimestamp):
            return NotImplemented
        return self.sequence <= other.sequence
    
    def __gt__(self, other: "LogicalTimestamp") -> bool:
        """Compare timestamps."""
        if not isinstance(other, LogicalTimestamp):
            return NotImplemented
        return self.sequence > other.sequence
    
    def __ge__(self, other: "LogicalTimestamp") -> bool:
        """Compare timestamps."""
        if not isinstance(other, LogicalTimestamp):
            return NotImplemented
        return self.sequence >= other.sequence


class LogicalClock:
    """
    Deterministic logical clock for transaction timestamps.
    
    TIER-0 REQUIREMENT: This clock provides deterministic timestamps
    based on global sequence numbers, not wall-clock time.
    
    Design:
        - Timestamps are derived from global_sequence
        - Same sequence → same timestamp (deterministic)
        - Monotonic ordering guaranteed
        - No external time dependence
    
    This ensures replay determinism:
        - Same journal entries → same timestamps
        - Same timestamps → same replay outcome
    """
    
    # TIER-0: Deterministic timestamp generation
    # We use global_sequence * NANOSECONDS_PER_SEQUENCE to create
    # deterministic nanoseconds that maintain monotonicity
    NANOSECONDS_PER_SEQUENCE = 1_000_000_000  # 1 second per sequence
    
    def __init__(self, initial_sequence: int = 0):
        """
        Initialize logical clock.
        
        Args:
            initial_sequence: Starting global sequence number
        """
        self._sequence = initial_sequence
    
    def tick(self) -> LogicalTimestamp:
        """
        Advance clock and return timestamp.
        
        TIER-0 CRITICAL: This method provides deterministic timestamps.
        The timestamp is derived from the current sequence number,
        ensuring same sequence → same timestamp.
        
        Returns:
            LogicalTimestamp: Deterministic timestamp
        """
        timestamp = LogicalTimestamp(
            sequence=self._sequence,
            nanoseconds=self._sequence * self.NANOSECONDS_PER_SEQUENCE
        )
        self._sequence += 1
        return timestamp
    
    def now(self) -> LogicalTimestamp:
        """
        Get current timestamp without advancing.
        
        Returns:
            LogicalTimestamp: Current deterministic timestamp
        """
        return LogicalTimestamp(
            sequence=self._sequence,
            nanoseconds=self._sequence * self.NANOSECONDS_PER_SEQUENCE
        )
    
    def from_sequence(self, sequence: int) -> LogicalTimestamp:
        """
        Create timestamp from sequence number (deterministic).
        
        TIER-0 CRITICAL: This method provides deterministic timestamp
        reconstruction from sequence numbers. Same sequence → same timestamp.
        
        Args:
            sequence: Global sequence number
        
        Returns:
            LogicalTimestamp: Deterministic timestamp
        """
        return LogicalTimestamp(
            sequence=sequence,
            nanoseconds=sequence * self.NANOSECONDS_PER_SEQUENCE
        )
    
    def get_sequence(self) -> int:
        """Get current sequence number."""
        return self._sequence
    
    def set_sequence(self, sequence: int) -> None:
        """
        Set sequence number (for recovery).
        
        TIER-0: This allows deterministic clock reconstruction from journal.
        """
        if sequence < 0:
            raise ValueError("sequence must be >= 0")
        self._sequence = sequence
    
    def reset(self) -> None:
        """Reset clock to initial state."""
        self._sequence = 0
