"""
/data/pipelines/ingestion/builders/result_factory.py

Result Construction Factories

This module contains execution logic for constructing IngestResult objects.
These are pipeline behaviors, not ledger definitions.

The ledger contract (ingest_result.py) defines facts.
This module defines how to construct those facts from execution context.
"""

from __future__ import annotations

from typing import Optional, List

from ..base.ingest_context import IngestContext
from ..base.ingest_result import (
    IngestResult,
    IngestStatus,
    MutationRecord,
    RejectionRecord,
    RejectionReason,
    compute_result_hash,
)


# ============================================================================
# CORE FACTORY METHODS
# ============================================================================

def create_success_result(
    context_hash: str,
    inputs_received: int,
    mutations: list[MutationRecord],
    started_at_ms: int,
    completed_at_ms: int
) -> IngestResult:
    """
    Create a SUCCESS result.
    
    Args:
        context_hash: Context hash from IngestContext
        inputs_received: Number of inputs received
        mutations: List of committed mutations
        started_at_ms: Start timestamp (epoch milliseconds)
        completed_at_ms: Completion timestamp (epoch milliseconds)
    
    Returns:
        IngestResult with SUCCESS status
    """
    if not mutations:
        raise ValueError("SUCCESS result requires at least one mutation")
    
    result_hash = compute_result_hash(
        context_hash=context_hash,
        status=IngestStatus.SUCCESS,
        inputs_received=inputs_received,
        inputs_committed=inputs_received,
        inputs_rejected=0,
        mutations=tuple(mutations),
        rejections=tuple()
    )
    
    return IngestResult(
        context_hash=context_hash,
        status=IngestStatus.SUCCESS,
        inputs_received=inputs_received,
        inputs_committed=inputs_received,
        inputs_rejected=0,
        mutations=tuple(mutations),
        rejections=tuple(),
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        result_hash=result_hash
    )


def create_partial_result(
    context_hash: str,
    inputs_received: int,
    mutations: list[MutationRecord],
    rejections: list[RejectionRecord],
    started_at_ms: int,
    completed_at_ms: int
) -> IngestResult:
    """
    Create a PARTIAL result.
    
    Args:
        context_hash: Context hash from IngestContext
        inputs_received: Number of inputs received
        mutations: List of committed mutations
        rejections: List of rejection records
        started_at_ms: Start timestamp (epoch milliseconds)
        completed_at_ms: Completion timestamp (epoch milliseconds)
    
    Returns:
        IngestResult with PARTIAL status
    """
    if not mutations:
        raise ValueError("PARTIAL result requires at least one mutation")
    
    if not rejections:
        raise ValueError("PARTIAL result requires at least one rejection")
    
    inputs_committed = len(mutations)
    inputs_rejected = len(rejections)
    
    if inputs_committed + inputs_rejected != inputs_received:
        raise ValueError(
            f"PARTIAL result totals do not reconcile: "
            f"received={inputs_received}, "
            f"committed={inputs_committed}, "
            f"rejected={inputs_rejected}"
        )
    
    result_hash = compute_result_hash(
        context_hash=context_hash,
        status=IngestStatus.PARTIAL,
        inputs_received=inputs_received,
        inputs_committed=inputs_committed,
        inputs_rejected=inputs_rejected,
        mutations=tuple(mutations),
        rejections=tuple(rejections)
    )
    
    return IngestResult(
        context_hash=context_hash,
        status=IngestStatus.PARTIAL,
        inputs_received=inputs_received,
        inputs_committed=inputs_committed,
        inputs_rejected=inputs_rejected,
        mutations=tuple(mutations),
        rejections=tuple(rejections),
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        result_hash=result_hash
    )


def create_failed_result(
    context_hash: str,
    inputs_received: int,
    rejections: list[RejectionRecord],
    started_at_ms: int,
    completed_at_ms: int
) -> IngestResult:
    """
    Create a FAILED result.
    
    Args:
        context_hash: Context hash from IngestContext
        inputs_received: Number of inputs received
        rejections: List of rejection records
        started_at_ms: Start timestamp (epoch milliseconds)
        completed_at_ms: Completion timestamp (epoch milliseconds)
    
    Returns:
        IngestResult with FAILED status
    """
    if inputs_received != len(rejections):
        raise ValueError(
            f"FAILED result requires all inputs to be rejected: "
            f"received={inputs_received}, rejections={len(rejections)}"
        )
    
    result_hash = compute_result_hash(
        context_hash=context_hash,
        status=IngestStatus.FAILED,
        inputs_received=inputs_received,
        inputs_committed=0,
        inputs_rejected=inputs_received,
        mutations=tuple(),
        rejections=tuple(rejections)
    )
    
    return IngestResult(
        context_hash=context_hash,
        status=IngestStatus.FAILED,
        inputs_received=inputs_received,
        inputs_committed=0,
        inputs_rejected=inputs_received,
        mutations=tuple(),
        rejections=tuple(rejections),
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        result_hash=result_hash
    )


def create_skipped_result(
    context_hash: str,
    inputs_received: int,
    rejections: list[RejectionRecord],
    started_at_ms: int,
    completed_at_ms: int
) -> IngestResult:
    """
    Create a SKIPPED result.
    
    Args:
        context_hash: Context hash from IngestContext
        inputs_received: Number of inputs received
        rejections: List of rejection records (why skipped)
        started_at_ms: Start timestamp (epoch milliseconds)
        completed_at_ms: Completion timestamp (epoch milliseconds)
    
    Returns:
        IngestResult with SKIPPED status
    """
    if inputs_received != len(rejections):
        raise ValueError(
            f"SKIPPED result requires all inputs to be rejected: "
            f"received={inputs_received}, rejections={len(rejections)}"
        )
    
    result_hash = compute_result_hash(
        context_hash=context_hash,
        status=IngestStatus.SKIPPED,
        inputs_received=inputs_received,
        inputs_committed=0,
        inputs_rejected=inputs_received,
        mutations=tuple(),
        rejections=tuple(rejections)
    )
    
    return IngestResult(
        context_hash=context_hash,
        status=IngestStatus.SKIPPED,
        inputs_received=inputs_received,
        inputs_committed=0,
        inputs_rejected=inputs_received,
        mutations=tuple(),
        rejections=tuple(rejections),
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        result_hash=result_hash
    )


# ============================================================================
# CONVENIENCE FACTORIES (HIGH-LEVEL API)
# ============================================================================

def create_accepted_result(
    context: IngestContext,
    fact_ids: list[str],
    started_at_ms: Optional[int] = None,
    completed_at_ms: Optional[int] = None
) -> IngestResult:
    """
    Create an accepted result for successful ingestion.
    
    Args:
        context: Ingestion context
        fact_ids: List of canonical fact IDs that were created
        started_at_ms: Start timestamp (defaults to context.timestamp_ms)
        completed_at_ms: Completion timestamp (defaults to context.timestamp_ms)
    
    Returns:
        IngestResult with SUCCESS status
    """
    if not fact_ids:
        raise ValueError("Accepted result requires at least one fact_id")
    
    if started_at_ms is None:
        started_at_ms = context.timestamp_ms
    if completed_at_ms is None:
        completed_at_ms = context.timestamp_ms
    
    mutations = [
        MutationRecord(
            entity_type="account",
            entity_id=fact_id,
            operation="create",
            hash=fact_id  # Use fact_id as hash for simplicity
        )
        for fact_id in fact_ids
    ]
    
    return create_success_result(
        context_hash=context.context_hash,
        inputs_received=1,
        mutations=mutations,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms
    )


def create_rejected_result(
    context: IngestContext,
    reason: RejectionReason,
    detail: str,
    started_at_ms: Optional[int] = None,
    completed_at_ms: Optional[int] = None
) -> IngestResult:
    """
    Create a rejected result for failed ingestion.
    
    Args:
        context: Ingestion context
        reason: Rejection reason
        detail: Rejection detail message
        started_at_ms: Start timestamp (defaults to context.timestamp_ms)
        completed_at_ms: Completion timestamp (defaults to context.timestamp_ms)
    
    Returns:
        IngestResult with FAILED status
    """
    if started_at_ms is None:
        started_at_ms = context.timestamp_ms
    if completed_at_ms is None:
        completed_at_ms = context.timestamp_ms
    
    rejection = RejectionRecord(
        input_id=context.run_id,
        reason=reason,
        details=detail
    )
    
    return create_failed_result(
        context_hash=context.context_hash,
        inputs_received=1,
        rejections=[rejection],
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms
    )


def create_deduped_result(
    context: IngestContext,
    existing_fact_ids: list[str],
    started_at_ms: Optional[int] = None,
    completed_at_ms: Optional[int] = None
) -> IngestResult:
    """
    Create a deduped result for duplicate ingestion.
    
    Args:
        context: Ingestion context
        existing_fact_ids: List of existing fact IDs that matched
        started_at_ms: Start timestamp (defaults to context.timestamp_ms)
        completed_at_ms: Completion timestamp (defaults to context.timestamp_ms)
    
    Returns:
        IngestResult with SUCCESS status (idempotent reingest)
    """
    if started_at_ms is None:
        started_at_ms = context.timestamp_ms
    if completed_at_ms is None:
        completed_at_ms = context.timestamp_ms
    
    # Idempotent reingest - no mutations, but success status
    mutations = [
        MutationRecord(
            entity_type="account",
            entity_id=fact_id,
            operation="update",  # Idempotent update
            hash=fact_id
        )
        for fact_id in existing_fact_ids
    ] if existing_fact_ids else []
    
    return create_success_result(
        context_hash=context.context_hash,
        inputs_received=1,
        mutations=mutations,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms
    )
