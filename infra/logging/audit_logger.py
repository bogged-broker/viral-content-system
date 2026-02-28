"""
/infra/logging/audit_logger.py

Tamper-Evident Audit Logging Authority

This is NOT general logging. This is cryptographic evidence for:
- Platform disputes
- Shadowban claims
- Enforcement investigations
- Regulator audits
- Legal defense

If a fact is not in the audit log, it cannot be trusted.

Core Principles (NON-NEGOTIABLE):
1. Append-only
2. Cryptographically chained
3. Deterministically generated
4. Externally verifiable
5. Replay-provable
6. Immutable once flushed
"""

import hashlib
import json
import threading
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional, Tuple, List, Dict
from collections import defaultdict

# Assumed imports from infra layer
# from infra.clock import TimePoint, MonotonicClock
# from infra.id_generator import GeneratedID, IDGenerator
# from infra.runtime_context import EventScope


# ============================================================================
# ENUMS (STRICT, NO STRINGS)
# ============================================================================

class AuditEventType(Enum):
    """Only meaningful, defensible events are allowed."""
    CONTENT_ACTION = "content_action"
    ACCOUNT_ACTION = "account_action"
    POLICY_DECISION = "policy_decision"
    ENFORCEMENT_EVENT = "enforcement_event"
    SYSTEM_OVERRIDE = "system_override"


class AuditSeverity(Enum):
    """
    Severity affects:
    - retention
    - anchoring
    - alerting
    - legal exposure
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class AuditSchema:
    """
    Schemas are frozen forever.
    Changes require version bumps.
    """
    name: str
    version: int
    
    event_type: AuditEventType
    allowed_scopes: frozenset[str]  # EventScope enum values as strings
    
    required_fields: frozenset[str]
    optional_fields: frozenset[str]
    
    description: str
    
    def validate_payload(self, payload: dict) -> bool:
        """Validate payload contains all required fields."""
        payload_keys = set(payload.keys())
        
        # Check required fields
        if not self.required_fields.issubset(payload_keys):
            missing = self.required_fields - payload_keys
            raise ValueError(f"Missing required fields: {missing}")
        
        # Check no unexpected fields
        allowed = self.required_fields | self.optional_fields
        unexpected = payload_keys - allowed
        if unexpected:
            raise ValueError(f"Unexpected fields: {unexpected}")
        
        return True


@dataclass(frozen=True)
class AuditRecord:
    """
    Each record links to the previous record.
    Any modification breaks the chain.
    """
    audit_id: str  # GeneratedID
    
    timestamp: float  # TimePoint as float for simplicity
    severity: AuditSeverity
    event_type: AuditEventType
    
    scope: str  # EventScope as string
    subject_id: str
    
    schema_name: str
    schema_version: int
    
    payload: dict
    
    previous_hash: str
    record_hash: str
    
    def to_hashable_dict(self) -> dict:
        """Convert to deterministic dict for hashing."""
        return {
            'audit_id': self.audit_id,
            'timestamp': self.timestamp,
            'severity': self.severity.value,
            'event_type': self.event_type.value,
            'scope': self.scope,
            'subject_id': self.subject_id,
            'schema_name': self.schema_name,
            'schema_version': self.schema_version,
            'payload': self.payload,
            'previous_hash': self.previous_hash,
        }


# ============================================================================
# CRYPTOGRAPHIC PRIMITIVES
# ============================================================================

class HashEngine:
    """
    Deterministic hashing for audit records.
    Uses SHA-256 for cryptographic strength.
    """
    
    @staticmethod
    def hash_record(record_data: dict) -> str:
        """
        Compute deterministic hash of record.
        
        H_n = hash(H_{n-1} || record_payload || metadata)
        """
        # Serialize deterministically (sorted keys)
        canonical = json.dumps(record_data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    @staticmethod
    def hash_bytes(data: bytes) -> str:
        """Hash raw bytes."""
        return hashlib.sha256(data).hexdigest()


class MerkleAccumulator:
    """
    Groups records into Merkle trees for O(log n) verification.
    
    This makes verification fast and scalable.
    """
    
    def __init__(self):
        self.leaves: list[str] = []
    
    def add_leaf(self, record_hash: str):
        """Add a record hash as a leaf."""
        self.leaves.append(record_hash)
    
    def compute_root(self) -> str:
        """
        Compute Merkle root from current leaves.
        Returns empty hash if no leaves.
        """
        if not self.leaves:
            return HashEngine.hash_bytes(b'')
        
        current_level = self.leaves[:]
        
        while len(current_level) > 1:
            next_level = []
            
            for i in range(0, len(current_level), 2):
                left = current_level[i]
                right = current_level[i + 1] if i + 1 < len(current_level) else left
                
                combined = left + right
                parent = HashEngine.hash_bytes(combined.encode('utf-8'))
                next_level.append(parent)
            
            current_level = next_level
        
        return current_level[0]
    
    def reset(self):
        """Clear accumulated leaves."""
        self.leaves.clear()


# ============================================================================
# AUDIT CHAIN
# ============================================================================

class AuditChain:
    """
    Maintains cryptographic chain of audit records.
    
    Properties:
    - Any modification breaks the chain
    - Any deletion breaks verification
    - Insertion is detectable
    - Reordering is detectable
    """
    
    def __init__(self):
        self.records: list[AuditRecord] = []
        self.last_hash: str = HashEngine.hash_bytes(b'AUDIT_CHAIN_GENESIS')
        self._sealed_batches: list[tuple[int, str]] = []  # (end_index, merkle_root)
    
    def append(self, record: AuditRecord) -> None:
        """
        Append record to chain.
        Updates last_hash for next record.
        """
        self.records.append(record)
        self.last_hash = record.record_hash
    
    def get_last_hash(self) -> str:
        """Get hash for chaining next record."""
        return self.last_hash
    
    def seal_batch(self, end_index: int, merkle_root: str) -> None:
        """Record sealed batch for immutability."""
        self._sealed_batches.append((end_index, merkle_root))
    
    def verify_chain(self, start_index: int = 0, end_index: Optional[int] = None) -> bool:
        """
        Verify chain integrity.
        
        Returns:
            True if chain is valid, False if tampered.
        """
        if end_index is None:
            end_index = len(self.records)
        
        expected_hash = HashEngine.hash_bytes(b'AUDIT_CHAIN_GENESIS')
        if start_index > 0:
            expected_hash = self.records[start_index - 1].record_hash
        
        for i in range(start_index, end_index):
            record = self.records[i]
            
            # Verify previous hash matches
            if record.previous_hash != expected_hash:
                return False
            
            # Recompute record hash
            computed = HashEngine.hash_record(record.to_hashable_dict())
            if computed != record.record_hash:
                return False
            
            expected_hash = record.record_hash
        
        return True
    
    def get_sealed_batches(self) -> list[tuple[int, str]]:
        """Get list of sealed batch checkpoints."""
        return self._sealed_batches.copy()


# ============================================================================
# EXTERNAL ANCHORING (OPTIONAL BUT STRONGLY ADVISED)
# ============================================================================

class ExternalAnchor:
    """
    Anchor audit hashes to external, immutable systems.
    
    May write hash roots to:
    - Public blockchains
    - Third-party notarization
    - Immutable object storage
    
    This makes after-the-fact tampering provably impossible.
    """
    
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.anchored_roots: list[dict] = []
    
    def anchor_root(self, merkle_root: str, timestamp: float, batch_size: int) -> None:
        """
        Anchor a Merkle root externally.
        
        In production, this would:
        - Submit to blockchain
        - Store in immutable storage
        - Register with notary service
        """
        if not self.enabled:
            return
        
        anchor_record = {
            'merkle_root': merkle_root,
            'timestamp': timestamp,
            'batch_size': batch_size,
            'anchor_hash': HashEngine.hash_bytes(f"{merkle_root}:{timestamp}".encode('utf-8'))
        }
        
        self.anchored_roots.append(anchor_record)
        
        # TODO: Actual external anchoring
        # - blockchain_client.submit(merkle_root)
        # - s3_client.put_object(immutable=True, ...)
        # - notary_service.register(...)
    
    def get_anchors(self) -> list[dict]:
        """Get all anchored roots."""
        return self.anchored_roots.copy()


# ============================================================================
# AUDIT WATCHDOG (PARANOID)
# ============================================================================

class AuditWatchdog:
    """
    Monitors audit system integrity.
    
    Monitors:
    - Missing audit records
    - Incorrect chaining
    - Skipped severity escalation
    - Unsealed batches
    - Forbidden schema use
    
    Can:
    - Halt execution
    - Freeze accounts
    - Invalidate runs
    - Trigger global kill-switch
    """
    
    def __init__(self):
        self.violations: list[dict] = []
        self.enabled = True
        self.kill_switch_triggered = False
    
    def check_chain_integrity(self, chain: AuditChain) -> bool:
        """Verify chain has not been tampered with."""
        if not chain.verify_chain():
            self._record_violation('CHAIN_INTEGRITY_FAILURE', 'Audit chain verification failed')
            return False
        return True
    
    def check_unsealed_batch_size(self, unsealed_count: int, max_allowed: int) -> bool:
        """Ensure batches are sealed regularly."""
        if unsealed_count > max_allowed:
            self._record_violation(
                'UNSEALED_BATCH_OVERFLOW',
                f'Unsealed records ({unsealed_count}) exceed threshold ({max_allowed})'
            )
            return False
        return True
    
    def check_severity_escalation(self, severity: AuditSeverity, subject_id: str) -> bool:
        """Monitor for suspicious severity patterns."""
        # TODO: Implement escalation tracking
        return True
    
    def _record_violation(self, violation_type: str, message: str) -> None:
        """Record watchdog violation."""
        violation = {
            'type': violation_type,
            'message': message,
            'timestamp': 0.0,  # Would use MonotonicClock
        }
        self.violations.append(violation)
        
        # In production: alert, log, potentially halt
        if violation_type in ('CHAIN_INTEGRITY_FAILURE', 'DELIBERATE_TAMPERING'):
            self.trigger_kill_switch()
    
    def trigger_kill_switch(self) -> None:
        """
        Trigger global kill-switch.
        Halts all operations until manual override.
        """
        self.kill_switch_triggered = True
        # TODO: Actual kill-switch implementation
        # - halt all posting
        # - freeze account operations
        # - alert security team
        # - dump emergency state
    
    def get_violations(self) -> list[dict]:
        """Get all recorded violations."""
        return self.violations.copy()


# ============================================================================
# AUDIT LOGGER (SINGLE AUTHORITY)
# ============================================================================

class AuditLogger:
    """
    Single source of truth for audit logging.
    Only one exists per runtime.
    
    Responsibilities:
    1. record() - append audit events
    2. seal_batch() - finalize hash chain segments
    3. verify_chain() - prove integrity
    """
    
    _instance: Optional['AuditLogger'] = None
    _lock = threading.Lock()
    
    def __init__(self):
        if AuditLogger._instance is not None:
            raise RuntimeError("AuditLogger is a singleton. Use get_instance().")
        
        self._chain = AuditChain()
        self._schemas: dict[str, AuditSchema] = {}
        self._unsealed_buffer: list[AuditRecord] = []
        self._merkle = MerkleAccumulator()
        self._anchor = ExternalAnchor(enabled=False)
        self._watchdog = AuditWatchdog()
        
        self._record_count = 0
        self._last_seal_count = 0
        
        # Configuration
        self.max_unsealed_records = 1000
        self.auto_seal_enabled = True
        
        self._register_builtin_schemas()
    
    @classmethod
    def get_instance(cls) -> 'AuditLogger':
        """Get singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = AuditLogger()
        return cls._instance
    
    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing only)."""
        with cls._lock:
            cls._instance = None
    
    def _register_builtin_schemas(self) -> None:
        """Register built-in audit schemas."""
        
        # Content action schema
        self.register_schema(AuditSchema(
            name="content_action",
            version=1,
            event_type=AuditEventType.CONTENT_ACTION,
            allowed_scopes=frozenset(["post", "reply", "quote"]),
            required_fields=frozenset(["action", "content_id", "actor_id"]),
            optional_fields=frozenset(["reason", "metadata"]),
            description="Actions taken on content"
        ))
        
        # Account action schema
        self.register_schema(AuditSchema(
            name="account_action",
            version=1,
            event_type=AuditEventType.ACCOUNT_ACTION,
            allowed_scopes=frozenset(["account"]),
            required_fields=frozenset(["action", "account_id", "actor_id"]),
            optional_fields=frozenset(["reason", "duration", "metadata"]),
            description="Actions taken on accounts"
        ))
        
        # Policy decision schema
        self.register_schema(AuditSchema(
            name="policy_decision",
            version=1,
            event_type=AuditEventType.POLICY_DECISION,
            allowed_scopes=frozenset(["system", "manual"]),
            required_fields=frozenset(["decision", "subject_id", "policy_id"]),
            optional_fields=frozenset(["confidence", "explanation", "overridden"]),
            description="Automated or manual policy decisions"
        ))
        
        # Enforcement event schema
        self.register_schema(AuditSchema(
            name="enforcement_event",
            version=1,
            event_type=AuditEventType.ENFORCEMENT_EVENT,
            allowed_scopes=frozenset(["enforcement"]),
            required_fields=frozenset(["event", "target_id", "enforcer_id", "severity"]),
            optional_fields=frozenset(["evidence", "appeal_deadline"]),
            description="Enforcement actions and their evidence"
        ))
        
        # System override schema
        self.register_schema(AuditSchema(
            name="system_override",
            version=1,
            event_type=AuditEventType.SYSTEM_OVERRIDE,
            allowed_scopes=frozenset(["system"]),
            required_fields=frozenset(["override_type", "original_value", "new_value", "operator_id"]),
            optional_fields=frozenset(["justification", "approval_ticket"]),
            description="Manual overrides of system behavior (HIGH RISK)"
        ))
    
    def register_schema(self, schema: AuditSchema) -> None:
        """
        Register an audit schema.
        Schemas are immutable once registered.
        """
        schema_key = f"{schema.name}:v{schema.version}"
        
        if schema_key in self._schemas:
            raise ValueError(f"Schema {schema_key} already registered")
        
        self._schemas[schema_key] = schema
    
    def record(
        self,
        schema_name: str,
        payload: dict,
        severity: AuditSeverity,
        scope: str,
        subject_id: str,
        schema_version: int = 1,
        audit_id: Optional[str] = None
    ) -> AuditRecord:
        """
        Record an audit event.
        
        Steps:
        1. Validate schema
        2. Validate payload fields
        3. Generate audit_id (deterministic)
        4. Stamp monotonic timestamp
        5. Compute chain hash
        6. Append record
        7. Buffer until sealed
        
        Failures are fatal.
        """
        
        # Watchdog: check if kill-switch triggered
        if self._watchdog.kill_switch_triggered:
            raise RuntimeError("Audit system kill-switch triggered. All operations halted.")
        
        # 1. Validate schema
        schema_key = f"{schema_name}:v{schema_version}"
        schema = self._schemas.get(schema_key)
        if schema is None:
            raise ValueError(f"Unknown audit schema: {schema_key}")
        
        # 2. Validate payload
        schema.validate_payload(payload)
        
        # Validate scope
        if scope not in schema.allowed_scopes:
            raise ValueError(f"Invalid scope '{scope}' for schema {schema_name}")
        
        # 3. Generate audit_id (deterministic)
        if audit_id is None:
            # Would use IDGenerator.generate() in production
            audit_id = f"audit_{self._record_count:010d}"
        
        # 4. Stamp monotonic timestamp
        # Would use MonotonicClock.now() in production
        timestamp = float(self._record_count)
        
        # 5. Get previous hash
        previous_hash = self._chain.get_last_hash()
        
        # 6. Create record (without hash)
        record_data = {
            'audit_id': audit_id,
            'timestamp': timestamp,
            'severity': severity.value,
            'event_type': schema.event_type.value,
            'scope': scope,
            'subject_id': subject_id,
            'schema_name': schema_name,
            'schema_version': schema_version,
            'payload': payload,
            'previous_hash': previous_hash,
        }
        
        # 7. Compute record hash
        record_hash = HashEngine.hash_record(record_data)
        
        # Create final record
        record = AuditRecord(
            audit_id=audit_id,
            timestamp=timestamp,
            severity=severity,
            event_type=schema.event_type,
            scope=scope,
            subject_id=subject_id,
            schema_name=schema_name,
            schema_version=schema_version,
            payload=payload,
            previous_hash=previous_hash,
            record_hash=record_hash
        )
        
        # 8. Append to chain
        self._chain.append(record)
        self._unsealed_buffer.append(record)
        self._merkle.add_leaf(record_hash)
        self._record_count += 1
        
        # 9. Auto-seal if threshold reached
        if self.auto_seal_enabled and len(self._unsealed_buffer) >= self.max_unsealed_records:
            self.seal_batch()
        
        # Watchdog: check unsealed buffer size
        self._watchdog.check_unsealed_batch_size(
            len(self._unsealed_buffer),
            self.max_unsealed_records * 2
        )
        
        return record
    
    def seal_batch(self) -> Optional[str]:
        """
        Seal current batch of records.
        
        Sealing:
        - Finalizes hash chain segment
        - Emits Merkle root
        - Prepares external anchoring
        
        Sealed records are immutable forever.
        
        Returns:
            Merkle root of sealed batch, or None if no unsealed records
        """
        if not self._unsealed_buffer:
            return None
        
        # Compute Merkle root
        merkle_root = self._merkle.compute_root()
        
        # Record seal in chain
        end_index = len(self._chain.records)
        self._chain.seal_batch(end_index, merkle_root)
        
        # External anchoring
        self._anchor.anchor_root(
            merkle_root=merkle_root,
            timestamp=float(self._record_count),
            batch_size=len(self._unsealed_buffer)
        )
        
        # Clear unsealed buffer and reset accumulator
        batch_size = len(self._unsealed_buffer)
        self._unsealed_buffer.clear()
        self._merkle.reset()
        self._last_seal_count = self._record_count
        
        return merkle_root
    
    def verify_chain(self, start_index: int = 0, end_index: Optional[int] = None) -> bool:
        """
        Verify audit chain integrity.
        
        Any mismatch = evidence tampering.
        
        Returns:
            True if chain is valid, False if tampered
        """
        is_valid = self._chain.verify_chain(start_index, end_index)
        
        if not is_valid:
            self._watchdog._record_violation(
                'CHAIN_VERIFICATION_FAILURE',
                f'Chain verification failed for range [{start_index}:{end_index}]'
            )
        
        return is_valid
    
    def get_records(self, start_index: int = 0, end_index: Optional[int] = None) -> list[AuditRecord]:
        """Get audit records in range."""
        if end_index is None:
            end_index = len(self._chain.records)
        return self._chain.records[start_index:end_index]
    
    def get_sealed_batches(self) -> list[tuple[int, str]]:
        """Get all sealed batch checkpoints."""
        return self._chain.get_sealed_batches()
    
    def get_stats(self) -> dict:
        """Get audit logger statistics."""
        return {
            'total_records': self._record_count,
            'unsealed_records': len(self._unsealed_buffer),
            'sealed_batches': len(self._chain.get_sealed_batches()),
            'chain_valid': self.verify_chain(),
            'watchdog_violations': len(self._watchdog.get_violations()),
            'kill_switch_triggered': self._watchdog.kill_switch_triggered,
        }
    
    def enable_external_anchoring(self) -> None:
        """Enable external anchoring of Merkle roots."""
        self._anchor.enabled = True
    
    def get_anchored_roots(self) -> list[dict]:
        """Get all externally anchored roots."""
        return self._anchor.get_anchors()


# ============================================================================
# MODULE-LEVEL CONVENIENCE
# ============================================================================

def get_audit_logger() -> AuditLogger:
    """Get singleton audit logger instance."""
    return AuditLogger.get_instance()


def audit_record(
    schema_name: str,
    payload: dict,
    severity: AuditSeverity,
    scope: str,
    subject_id: str,
    schema_version: int = 1
) -> AuditRecord:
    """
    Convenience function to record audit event.
    
    Usage:
        audit_record(
            schema_name="content_action",
            payload={"action": "delete", "content_id": "post_123", "actor_id": "admin_1"},
            severity=AuditSeverity.HIGH,
            scope="post",
            subject_id="post_123"
        )
    """
    logger = get_audit_logger()
    return logger.record(schema_name, payload, severity, scope, subject_id, schema_version)


def seal_audit_batch() -> Optional[str]:
    """Seal current batch of audit records."""
    logger = get_audit_logger()
    return logger.seal_batch()


def verify_audit_chain() -> bool:
    """Verify entire audit chain integrity."""
    logger = get_audit_logger()
    return logger.verify_chain()


# ============================================================================
# EXAMPLE USAGE (FOR TESTING)
# ============================================================================

if __name__ == "__main__":
    # Initialize audit logger
    logger = get_audit_logger()
    
    print("=== Audit Logger Test ===\n")
    
    # Record some audit events
    print("Recording audit events...")
    
    # Content action
    audit_record(
        schema_name="content_action",
        payload={
            "action": "delete",
            "content_id": "post_12345",
            "actor_id": "moderator_001",
            "reason": "spam"
        },
        severity=AuditSeverity.MEDIUM,
        scope="post",
        subject_id="post_12345"
    )
    
    # Account action
    audit_record(
        schema_name="account_action",
        payload={
            "action": "suspend",
            "account_id": "user_67890",
            "actor_id": "admin_002",
            "duration": "7d",
            "reason": "repeated violations"
        },
        severity=AuditSeverity.HIGH,
        scope="account",
        subject_id="user_67890"
    )
    
    # Policy decision
    audit_record(
        schema_name="policy_decision",
        payload={
            "decision": "flag_for_review",
            "subject_id": "post_99999",
            "policy_id": "hate_speech_v2",
            "confidence": 0.87,
            "explanation": "Potential hate speech detected"
        },
        severity=AuditSeverity.MEDIUM,
        scope="system",
        subject_id="post_99999"
    )
    
    # System override (CRITICAL)
    audit_record(
        schema_name="system_override",
        payload={
            "override_type": "rate_limit_bypass",
            "original_value": "100/hour",
            "new_value": "unlimited",
            "operator_id": "engineer_003",
            "justification": "Emergency hotfix deployment",
            "approval_ticket": "TICK-12345"
        },
        severity=AuditSeverity.CRITICAL,
        scope="system",
        subject_id="rate_limiter"
    )
    
    print(f"Recorded {logger._record_count} events\n")
    
    # Verify chain
    print("Verifying chain integrity...")
    is_valid = verify_audit_chain()
    print(f"Chain valid: {is_valid}\n")
    
    # Seal batch
    print("Sealing batch...")
    merkle_root = seal_audit_batch()
    print(f"Merkle root: {merkle_root}\n")
    
    # Get stats
    stats = logger.get_stats()
    print("=== Audit Logger Stats ===")
    for key, value in stats.items():
        print(f"{key}: {value}")
    
    print("\n=== Sealed Batches ===")
    for idx, (end_idx, root) in enumerate(logger.get_sealed_batches()):
        print(f"Batch {idx}: records[0:{end_idx}] -> {root[:16]}...")
    
    # Simulate chain verification after modification (would fail)
    print("\n=== Testing Tamper Detection ===")
    print("Chain is cryptographically sealed.")
    print("Any modification would break verification.")
    print("This provides evidence in disputes and audits.")


