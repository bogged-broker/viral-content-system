"""
/infra/persistence/__init__.py
Persistence Authority Boundary & Contract Seal

This file defines the ONLY public persistence surface exposed to the rest of the system.
No side effects. No logic. No cleverness.
Pure authority declaration.

IMPORT SAFETY GUARANTEE:
Importing this module has zero runtime consequences.
No initialization. No configuration. No mutations.

EXPORT POLICY:
Only top-level persistence authorities are exposed.
Internal implementation details remain sealed.

BREAKING CHANGE NOTICE:
Any modification to exports is a breaking infrastructure change.
"""

from infra.persistence.lock_manager import LockManager
from infra.persistence.state_serializer import StateSerializer
from infra.persistence.snapshot_store import SnapshotStore
try:
    from infra.persistence.state_migrator import MigrationExecutor
    StateMigrator = MigrationExecutor  # Alias for backward compatibility
except ImportError:
    StateMigrator = None  # Optional if not available

# Explicit export declaration
# If it's not here, it doesn't exist outside this package
__all__ = [
    "LockManager",
    "StateSerializer",
    "SnapshotStore",
    "StateMigrator",
]

# Version contract
# Signals stability expectations to external systems
__version__ = "1.0.0"

# Architectural assertion
# Documents the persistence spine for auditors & tooling
__persistence_authorities__ = frozenset(__all__)