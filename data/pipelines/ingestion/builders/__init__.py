"""
/data/pipelines/ingestion/builders/

Result construction builders for ingestion pipeline.

This module contains execution logic for constructing IngestResult objects.
These are pipeline behaviors, not ledger definitions.
"""

from .result_factory import (
    create_success_result,
    create_partial_result,
    create_failed_result,
    create_skipped_result,
    create_accepted_result,
    create_rejected_result,
    create_deduped_result,
)

__all__ = [
    'create_success_result',
    'create_partial_result',
    'create_failed_result',
    'create_skipped_result',
    'create_accepted_result',
    'create_rejected_result',
    'create_deduped_result',
]
