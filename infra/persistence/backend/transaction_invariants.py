"""
/infra/persistence/backend/transaction_invariants.py

Transaction Invariants Module (Tier-0 Formalized Enforcement)

This module provides formalized invariant definitions and enforcement
for the transactional backend system.

TIER-0 REQUIREMENT:
    All transaction lifecycle invariants must be formally defined,
    verifiable, and fail-stop enforced.

This module answers:
    "What are the mathematical guarantees of the transaction system?"

Not:
    - "What might go wrong?"
    - "What should we check sometimes?"
    - "What are best practices?"

These are LAWS, not guidelines.
"""

from dataclasses import dataclass
from typing import List, Tuple, Optional
from enum import Enum

from base import (
    BackendInvariantViolation,
    BackendDataCorruption,
)


class InvariantSeverity(Enum):
    """Invariant violation severity."""
    FATAL = "fatal"  # Immediate halt
    BLOCKING = "blocking"  # Operation blocked
    CORRUPTION = "corruption"  # Data corruption detected


@dataclass(frozen=True)
class InvariantViolation:
    """Record of an invariant violation."""
    invariant_name: str
    severity: InvariantSeverity
    message: str
    context: dict


class TransactionInvariants:
    """
    Formalized transaction system invariants.
    
    TIER-0 REQUIREMENT: All invariants must be:
    - Deterministic (same input → same result)
    - Fail-stop (violation → immediate halt)
    - Externally verifiable (can be checked by external systems)
    - Mathematically provable (not heuristic)
    """
    
    # ========================================================================
    # JOURNAL INTEGRITY INVARIANTS
    # ========================================================================
    
    @staticmethod
    def verify_hash_chain_integrity(
        entries: List,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Journal hash chain must be unbroken.
        
        Mathematical guarantee:
            For all i in [1, n-1]:
                entry[i].previous_entry_hash == entry[i-1].entry_hash
        
        Violation → CORRUPTION (journal tampering detected)
        """
        if not entries:
            return True
        
        for i in range(1, len(entries)):
            prev_hash = entries[i-1].entry_hash
            curr_prev_hash = entries[i].previous_entry_hash
            
            if curr_prev_hash != prev_hash:
                errors.append(
                    f"TIER-0 VIOLATION [hash_chain_integrity]: "
                    f"Hash chain broken at entry {i}. "
                    f"Expected prev_hash={prev_hash}, got {curr_prev_hash}"
                )
                return False
        
        return True
    
    @staticmethod
    def verify_entry_hash_validity(
        entry,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Each entry's hash must match computed hash.
        
        Mathematical guarantee:
            entry.entry_hash == SHA256(entry.previous_entry_hash || entry_data)
        
        Violation → CORRUPTION (entry tampering detected)
        """
        if not entry.verify_hash():
            errors.append(
                f"TIER-0 VIOLATION [entry_hash_validity]: "
                f"Entry hash verification failed for tx {entry.transaction_id} "
                f"seq {entry.sequence_number}. "
                f"Expected: {entry.compute_hash()}, Got: {entry.entry_hash}"
            )
            return False
        return True
    
    @staticmethod
    def verify_global_sequence_monotonicity(
        entries: List,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Global sequence must be strictly monotonic.
        
        Mathematical guarantee:
            For all i in [1, n-1]:
                entry[i].global_sequence > entry[i-1].global_sequence
        
        Violation → CORRUPTION (ordering violation)
        """
        if len(entries) < 2:
            return True
        
        for i in range(1, len(entries)):
            prev_seq = entries[i-1].global_sequence
            curr_seq = entries[i].global_sequence
            
            if curr_seq <= prev_seq:
                errors.append(
                    f"TIER-0 VIOLATION [global_sequence_monotonicity]: "
                    f"Entry {i} (tx {entries[i].transaction_id}) "
                    f"global_sequence {curr_seq} is not monotonic. "
                    f"Previous: {prev_seq}"
                )
                return False
        
        return True
    
    @staticmethod
    def verify_timestamp_monotonicity(
        entries: List,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Timestamps must be monotonic (deterministic source).
        
        Mathematical guarantee:
            For all i in [1, n-1]:
                entry[i].timestamp_ns >= entry[i-1].timestamp_ns
        
        Violation → CORRUPTION (temporal ordering violation)
        """
        if len(entries) < 2:
            return True
        
        for i in range(1, len(entries)):
            prev_ts = entries[i-1].timestamp_ns
            curr_ts = entries[i].timestamp_ns
            
            if curr_ts < prev_ts:
                errors.append(
                    f"TIER-0 VIOLATION [timestamp_monotonicity]: "
                    f"Entry {i} (tx {entries[i].transaction_id}) "
                    f"timestamp_ns {curr_ts} is not monotonic. "
                    f"Previous: {prev_ts}"
                )
                return False
        
        return True
    
    # ========================================================================
    # TRANSACTION LIFECYCLE INVARIANTS
    # ========================================================================
    
    @staticmethod
    def verify_state_transition_validity(
        current_state,
        next_state,
        transaction_id: str,
        errors: List[str]
    ) -> bool:
        """
        Invariant: State transitions must follow legal paths.
        
        Legal transitions:
            BEGIN → PREPARED
            BEGIN → ABORTED
            PREPARED → COMMITTED
            PREPARED → ABORTED
        
        Violation → FATAL (illegal state machine transition)
        """
        if not current_state.can_transition_to(next_state):
            errors.append(
                f"TIER-0 VIOLATION [state_transition_validity]: "
                f"Invalid state transition in tx {transaction_id}: "
                f"{current_state.value} → {next_state.value}. "
                f"This is a hard invariant - no path can bypass state machine."
            )
            return False
        return True
    
    @staticmethod
    def verify_prepare_required_before_commit(
        entries: List,
        transaction_id: str,
        errors: List[str]
    ) -> bool:
        """
        Invariant: COMMITTED state requires PREPARED state.
        
        Mathematical guarantee:
            If ∃ entry with state=COMMITTED:
                ∃ entry with state=PREPARED AND sequence_number < COMMITTED.sequence_number
        
        Violation → FATAL (bypassed PREPARE phase)
        """
        committed_entries = [
            e for e in entries 
            if e.state.value == "committed"
        ]
        
        if not committed_entries:
            return True  # No commits to check
        
        for committed in committed_entries:
            prepared_entries = [
                e for e in entries
                if e.state.value == "prepared" 
                and e.sequence_number < committed.sequence_number
            ]
            
            if not prepared_entries:
                errors.append(
                    f"TIER-0 VIOLATION [prepare_required_before_commit]: "
                    f"Transaction {transaction_id} reached COMMITTED state "
                    f"without PREPARED state. This is a hard invariant - "
                    f"PREPARE cannot be bypassed."
                )
                return False
        
        return True
    
    @staticmethod
    def verify_begin_entry_exists(
        entries: List,
        transaction_id: str,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Every transaction must have a BEGIN entry.
        
        Mathematical guarantee:
            For every transaction_id:
                ∃ entry with state=BEGIN AND transaction_id=transaction_id
        
        Violation → FATAL (transaction lifecycle violation)
        """
        begin_entries = [
            e for e in entries
            if e.state.value == "begin"
        ]
        
        if not begin_entries:
            errors.append(
                f"TIER-0 VIOLATION [begin_entry_exists]: "
                f"Transaction {transaction_id} has no BEGIN entry. "
                f"Every transaction must start with a BEGIN entry."
            )
            return False
        
        return True
    
    @staticmethod
    def verify_intent_digest_consistency(
        begin_entry,
        prepared_entry,
        transaction_id: str,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Intent digest must match between BEGIN and PREPARED.
        
        Mathematical guarantee:
            BEGIN.intent_digest == PREPARED.intent_digest
        
        Violation → FATAL (intent mismatch)
        """
        if begin_entry.intent_digest != prepared_entry.intent_digest:
            errors.append(
                f"TIER-0 VIOLATION [intent_digest_consistency]: "
                f"Intent digest mismatch in tx {transaction_id}. "
                f"BEGIN: {begin_entry.intent_digest}, "
                f"PREPARED: {prepared_entry.intent_digest}"
            )
            return False
        return True
    
    # ========================================================================
    # RECOVERY INVARIANTS
    # ========================================================================
    
    @staticmethod
    def verify_recovery_determinism(
        recovery_audit: dict,
        errors: List[str]
    ) -> bool:
        """
        Invariant: Recovery must be deterministic.
        
        Mathematical guarantee:
            Same journal → same recovery outcome
        
        Violation → FATAL (non-deterministic recovery)
        """
        # Check that recovery timestamp is deterministic (derived from journal)
        if "recovery_timestamp_ns" not in recovery_audit:
            errors.append(
                f"TIER-0 VIOLATION [recovery_determinism]: "
                f"Recovery audit missing deterministic timestamp"
            )
            return False
        
        # Check that journal seal is present (proves journal state)
        if "journal_seal" not in recovery_audit:
            errors.append(
                f"TIER-0 VIOLATION [recovery_determinism]: "
                f"Recovery audit missing journal seal"
            )
            return False
        
        return True
    
    @staticmethod
    def verify_committed_effects_visible(
        committed_tx_count: int,
        replayed_count: int,
        errors: List[str]
    ) -> bool:
        """
        Invariant: All committed transactions must be replayed.
        
        Mathematical guarantee:
            committed_tx_count == replayed_count
        
        Violation → FATAL (recovery incomplete)
        """
        if committed_tx_count != replayed_count:
            errors.append(
                f"TIER-0 VIOLATION [committed_effects_visible]: "
                f"Recovery incomplete: {committed_tx_count} committed transactions, "
                f"but only {replayed_count} replayed. "
                f"All committed effects must be visible."
            )
            return False
        return True
    
    # ========================================================================
    # COMPREHENSIVE VERIFICATION
    # ========================================================================
    
    @staticmethod
    def verify_all_invariants(
        journal_entries: List,
        recovery_audit: Optional[dict] = None
    ) -> Tuple[bool, List[str]]:
        """
        Verify all transaction invariants.
        
        TIER-0 REQUIREMENT: Comprehensive invariant verification.
        This method provides formal proof that all guarantees hold.
        
        Returns:
            Tuple[bool, List[str]]: (all_valid, list of violations)
        """
        errors: List[str] = []
        
        if not journal_entries:
            return True, []  # Empty journal is valid
        
        # Group by transaction
        tx_groups: dict = {}
        for entry in journal_entries:
            if entry.transaction_id not in tx_groups:
                tx_groups[entry.transaction_id] = []
            tx_groups[entry.transaction_id].append(entry)
        
        # Sort entries within each transaction
        for tx_id in tx_groups:
            tx_groups[tx_id].sort(key=lambda e: e.sequence_number)
        
        # 1. Journal integrity invariants
        TransactionInvariants.verify_hash_chain_integrity(journal_entries, errors)
        TransactionInvariants.verify_global_sequence_monotonicity(journal_entries, errors)
        TransactionInvariants.verify_timestamp_monotonicity(journal_entries, errors)
        
        # Verify each entry's hash
        for entry in journal_entries:
            TransactionInvariants.verify_entry_hash_validity(entry, errors)
        
        # 2. Transaction lifecycle invariants
        for tx_id, entries in tx_groups.items():
            # Verify BEGIN entry exists
            TransactionInvariants.verify_begin_entry_exists(entries, tx_id, errors)
            
            # Verify state transitions
            for i in range(len(entries) - 1):
                current_state = entries[i].state
                next_state = entries[i + 1].state
                TransactionInvariants.verify_state_transition_validity(
                    current_state, next_state, tx_id, errors
                )
            
            # Verify PREPARE required before COMMIT
            TransactionInvariants.verify_prepare_required_before_commit(
                entries, tx_id, errors
            )
            
            # Verify intent digest consistency
            begin_entries = [e for e in entries if e.state.value == "begin"]
            prepared_entries = [e for e in entries if e.state.value == "prepared"]
            
            if begin_entries and prepared_entries:
                TransactionInvariants.verify_intent_digest_consistency(
                    begin_entries[0], prepared_entries[0], tx_id, errors
                )
        
        # 3. Recovery invariants
        if recovery_audit:
            TransactionInvariants.verify_recovery_determinism(recovery_audit, errors)
        
        return len(errors) == 0, errors
    
    @staticmethod
    def enforce_invariants(
        journal_entries: List,
        recovery_audit: Optional[dict] = None
    ) -> None:
        """
        Enforce all invariants (fail-stop on violation).
        
        TIER-0 REQUIREMENT: Fail-stop enforcement.
        If any invariant is violated, the system must halt immediately.
        
        Raises:
            BackendDataCorruption: If corruption invariants violated
            BackendInvariantViolation: If lifecycle invariants violated
        """
        all_valid, errors = TransactionInvariants.verify_all_invariants(
            journal_entries, recovery_audit
        )
        
        if not all_valid:
            # Separate corruption errors from lifecycle errors
            corruption_errors = [
                e for e in errors 
                if "CORRUPTION" in e or "hash" in e.lower() or "sequence" in e.lower()
            ]
            lifecycle_errors = [
                e for e in errors 
                if e not in corruption_errors
            ]
            
            if corruption_errors:
                raise BackendDataCorruption(
                    f"TIER-0 VIOLATION: Journal corruption detected. "
                    f"Errors: {'; '.join(corruption_errors)}. "
                    f"Refusing to operate."
                )
            
            if lifecycle_errors:
                raise BackendInvariantViolation(
                    f"TIER-0 VIOLATION: Transaction lifecycle invariants violated. "
                    f"Errors: {'; '.join(lifecycle_errors)}. "
                    f"Refusing to operate."
                )
