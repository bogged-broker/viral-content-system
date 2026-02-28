"""
Idempotency infrastructure for distributed event deduplication.

This package provides the distributed idempotency store that ensures
global "first-seen wins" semantics across multiple workers, regions,
and process restarts.
"""

from .event_identity_store import (
    EventIdentityStore,
    EventIdentityRecord,
    IdempotencyResult,
    create_event_identity_store,
)

__all__ = [
    'EventIdentityStore',
    'EventIdentityRecord',
    'IdempotencyResult',
    'create_event_identity_store',
]
