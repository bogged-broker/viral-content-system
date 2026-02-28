
"""
/infra/logging/log_schemas.py

Enforced Log Contract Authority

This file defines what logs are legally allowed to exist.

NOT:
- How they're written
- Where they go
- How they're stored

BUT:
- Which log events are valid
- What they must contain
- What invariants they must obey

If structured_logger is how truth is emitted,
and audit_logger is how truth is proven,
then log_schemas.py is what truth is allowed to say.

RULES:
- Every log event must declare a schema
- Schemas are immutable once registered
- Required fields are enforced
- Types are enforced
- Invariants are validated
- Invalid log = hard failure
- No best-effort behavior
- No dynamic fields
- No schema mutation

If schema enforcement breaks, nothing above infra can be trusted.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional, Tuple, List, Dict
import threading
import hashlib
import json
import re

# ============================================================================
# LOG EVENT TYPE (STRICT — NO STRINGS)
# ============================================================================

class LogEventType(Enum):
    """
    Exhaustive log event categories.
    
    New types must be registered explicitly — no ad-hoc categories.
    """
    SYSTEM = "system"                    # Infrastructure events
    CONTENT = "content"                  # Content lifecycle
    ACCOUNT = "account"                  # Account operations
    ORCHESTRATION = "orchestration"      # Workflow orchestration
    EXPERIMENT = "experiment"            # A/B test events
    EVALUATION = "evaluation"            # Evaluation/validation
    POSTING = "posting"                  # Social posting
    SAFETY = "safety"                    # Safety violations


# ============================================================================
# LOG SEVERITY (ALIGNED WITH SCHEMA INTENT)
# ============================================================================

class LogSeverity(Enum):
    """
    Log severity levels.
    
    Severity must align with schema intent.
    """
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"
    
    def __lt__(self, other):
        """Allow severity comparison."""
        if not isinstance(other, LogSeverity):
            return NotImplemented
        
        order = {
            LogSeverity.DEBUG: 0,
            LogSeverity.INFO: 1,
            LogSeverity.WARN: 2,
            LogSeverity.ERROR: 3,
            LogSeverity.CRITICAL: 4
        }
        return order[self] < order[other]


# ============================================================================
# FIELD INVARIANT VALIDATORS
# ============================================================================

class InvariantValidator:
    """
    Validates field invariants.
    
    Supports rules like:
    - non_empty
    - positive
    - monotonic
    - enum:<values>
    - id_format:content_id
    - hash:sha256
    - timestamp:monotonic
    
    Violations crash the emitter immediately.
    """
    
    @staticmethod
    def validate(invariant: str, value: Any, field_name: str) -> None:
        """
        Validate value against invariant rule.
        
        Raises ValueError if invariant violated.
        """
        if invariant == "non_empty":
            if not value:
                raise ValueError(
                    f"Field {field_name}: value must be non-empty"
                )
        
        elif invariant == "positive":
            if not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(
                    f"Field {field_name}: value must be positive, got {value}"
                )
        
        elif invariant == "non_negative":
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(
                    f"Field {field_name}: value must be non-negative, got {value}"
                )
        
        elif invariant.startswith("enum:"):
            allowed_values = invariant[5:].split(",")
            if str(value) not in allowed_values:
                raise ValueError(
                    f"Field {field_name}: value must be in {allowed_values}, "
                    f"got {value}"
                )
        
        elif invariant.startswith("id_format:"):
            format_type = invariant[10:]
            pattern_map = {
                "content_id": r"^content_[a-z0-9]{16}$",
                "account_id": r"^account_[a-z0-9]{16}$",
                "run_id": r"^run_[a-z0-9]{16}$",
                "job_id": r"^job_[a-z0-9]{16}$",
            }
            
            if format_type not in pattern_map:
                raise ValueError(f"Unknown id_format type: {format_type}")
            
            pattern = pattern_map[format_type]
            if not isinstance(value, str) or not re.match(pattern, value):
                raise ValueError(
                    f"Field {field_name}: value must match {format_type} format, "
                    f"got {value}"
                )
        
        elif invariant.startswith("hash:"):
            hash_type = invariant[5:]
            if hash_type == "sha256":
                if not isinstance(value, str) or len(value) != 64:
                    raise ValueError(
                        f"Field {field_name}: value must be 64-char SHA256 hex, "
                        f"got {value}"
                    )
                try:
                    int(value, 16)
                except ValueError:
                    raise ValueError(
                        f"Field {field_name}: value must be valid hex, got {value}"
                    )
        
        elif invariant.startswith("min_length:"):
            min_len = int(invariant[11:])
            if not isinstance(value, str) or len(value) < min_len:
                raise ValueError(
                    f"Field {field_name}: value must be at least {min_len} chars, "
                    f"got length {len(value) if isinstance(value, str) else 'N/A'}"
                )
        
        elif invariant.startswith("max_length:"):
            max_len = int(invariant[11:])
            if not isinstance(value, str) or len(value) > max_len:
                raise ValueError(
                    f"Field {field_name}: value must be at most {max_len} chars, "
                    f"got length {len(value) if isinstance(value, str) else 'N/A'}"
                )
        
        else:
            raise ValueError(f"Unknown invariant type: {invariant}")


# ============================================================================
# INVARIANT ENGINE (ORCHESTRATES VALIDATION)
# ============================================================================

class InvariantEngine:
    """
    Orchestrates invariant validation across all fields.
    
    Violations:
    - Crash the emitter
    - Surface immediately
    - Never silently degrade
    """
    
    @staticmethod
    def validate_field(
        field_def: 'LogFieldDefinition',
        value: Any
    ) -> None:
        """
        Validate field value against all invariants.
        
        Raises ValueError if any invariant violated.
        """
        for invariant in field_def.invariants:
            InvariantValidator.validate(invariant, value, field_def.name)


# ============================================================================
# LOG FIELD DEFINITION
# ============================================================================

@dataclass(frozen=True)
class LogFieldDefinition:
    """
    Definition of a single log field.
    
    Every field:
    - Explicitly typed
    - Explicitly required or optional
    - Invariant-checked
    
    No "freeform metadata".
    """
    name: str
    dtype: type
    required: bool
    nullable: bool
    description: str
    invariants: list[str] = field(default_factory=list)
    
    def validate(self, value: Any) -> None:
        """
        Validate value against field definition.
        
        Raises ValueError if validation fails.
        """
        # Check nullable
        if value is None:
            if not self.nullable:
                raise ValueError(
                    f"Field {self.name} cannot be null"
                )
            return  # Null is valid if nullable=True
        
        # Check type
        if not isinstance(value, self.dtype):
            raise ValueError(
                f"Field {self.name} must be {self.dtype.__name__}, "
                f"got {type(value).__name__}"
            )
        
        # Check invariants
        InvariantEngine.validate_field(self, value)


# ============================================================================
# LOG SCHEMA (IMMUTABLE CONTRACT)
# ============================================================================

@dataclass(frozen=True)
class LogSchema:
    """
    Immutable schema definition for a log event.
    
    Schemas are immutable once registered.
    Any meaning change → version bump.
    """
    name: str
    version: int
    event_type: LogEventType
    severity: LogSeverity
    fields: dict[str, LogFieldDefinition]
    description: str
    producer_module: str
    replay_safe: bool
    
    def validate_event(self, event_data: dict[str, Any]) -> None:
        """
        Validate event data against schema.
        
        Validation path:
        1. Check required fields present
        2. Enforce types
        3. Validate invariants
        4. Check for unknown fields
        
        Raises ValueError if validation fails.
        """
        # Check required fields
        for field_name, field_def in self.fields.items():
            if field_def.required and field_name not in event_data:
                raise ValueError(
                    f"Schema {self.name} v{self.version}: "
                    f"Required field {field_name} missing"
                )
        
        # Validate present fields
        for field_name, value in event_data.items():
            if field_name not in self.fields:
                raise ValueError(
                    f"Schema {self.name} v{self.version}: "
                    f"Unknown field {field_name}"
                )
            
            field_def = self.fields[field_name]
            field_def.validate(value)
    
    def get_required_fields(self) -> list[str]:
        """Return list of required field names."""
        return [
            name for name, field_def in self.fields.items()
            if field_def.required
        ]
    
    def get_optional_fields(self) -> list[str]:
        """Return list of optional field names."""
        return [
            name for name, field_def in self.fields.items()
            if not field_def.required
        ]


# ============================================================================
# SCHEMA VERSION RESOLVER
# ============================================================================

class SchemaVersionResolver:
    """
    Resolves schema versions for replay and evolution.
    
    RULES:
    - Fields may only be added in minor versions
    - Required field changes → major bump
    - Old versions remain valid for replay
    - No automatic migration
    """
    
    @staticmethod
    def is_compatible(
        old_version: int,
        new_version: int,
        old_schema: LogSchema,
        new_schema: LogSchema
    ) -> bool:
        """
        Check if schemas are forward-compatible.
        
        Compatible if:
        - Same name
        - New version only adds optional fields
        - No required fields removed
        - No type changes
        """
        if old_schema.name != new_schema.name:
            return False
        
        # Check that all old required fields still present
        old_required = set(old_schema.get_required_fields())
        new_required = set(new_schema.get_required_fields())
        
        if not old_required.issubset(new_required):
            return False
        
        # Check that common fields have same type
        for field_name in old_schema.fields:
            if field_name in new_schema.fields:
                old_field = old_schema.fields[field_name]
                new_field = new_schema.fields[field_name]
                
                if old_field.dtype != new_field.dtype:
                    return False
        
        return True


# ============================================================================
# SCHEMA REGISTRY (SINGLE SOURCE OF TRUTH)
# ============================================================================

class SchemaRegistry:
    """
    Singleton registry for all log schemas.
    
    Responsibilities:
    - Prevent duplicate names (at same version)
    - Enforce version monotonicity
    - Block schema replacement
    - Expose schemas to loggers
    
    This is the single source of truth for log contracts.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._schemas: dict[tuple[str, int], LogSchema] = {}
        self._latest_versions: dict[str, int] = {}
        self._registry_lock = threading.Lock()
        self._initialized = True
    
    def register(self, schema: LogSchema) -> None:
        """
        Register a schema.
        
        Raises ValueError if:
        - Schema already registered at this version
        - Version is not monotonically increasing
        - Schema is incompatible with previous version
        """
        with self._registry_lock:
            key = (schema.name, schema.version)
            
            # Check for duplicate
            if key in self._schemas:
                raise ValueError(
                    f"Schema {schema.name} v{schema.version} already registered"
                )
            
            # Check version monotonicity
            if schema.name in self._latest_versions:
                latest = self._latest_versions[schema.name]
                if schema.version <= latest:
                    raise ValueError(
                        f"Schema {schema.name} v{schema.version} is not greater "
                        f"than latest version {latest}"
                    )
                
                # Check compatibility
                old_schema = self._schemas[(schema.name, latest)]
                if not SchemaVersionResolver.is_compatible(
                    latest, schema.version, old_schema, schema
                ):
                    raise ValueError(
                        f"Schema {schema.name} v{schema.version} is not compatible "
                        f"with v{latest}"
                    )
            
            self._schemas[key] = schema
            self._latest_versions[schema.name] = schema.version
    
    def get(self, name: str, version: Optional[int] = None) -> LogSchema:
        """
        Get schema by name and optional version.
        
        If version not specified, returns latest.
        Raises KeyError if not found.
        """
        with self._registry_lock:
            if version is None:
                if name not in self._latest_versions:
                    raise KeyError(f"Schema {name} not registered")
                version = self._latest_versions[name]
            
            key = (name, version)
            if key not in self._schemas:
                raise KeyError(f"Schema {name} v{version} not found")
            
            return self._schemas[key]
    
    def exists(self, name: str, version: Optional[int] = None) -> bool:
        """Check if schema exists."""
        with self._registry_lock:
            if version is None:
                return name in self._latest_versions
            return (name, version) in self._schemas
    
    def get_latest_version(self, name: str) -> int:
        """Get latest version number for schema."""
        with self._registry_lock:
            if name not in self._latest_versions:
                raise KeyError(f"Schema {name} not registered")
            return self._latest_versions[name]
    
    def snapshot_hash(self) -> str:
        """
        Compute deterministic hash of all schemas.
        
        Used for replay validation and drift detection.
        """
        with self._registry_lock:
            schema_data = {}
            
            for (name, version), schema in sorted(self._schemas.items()):
                schema_data[f"{name}_v{version}"] = {
                    'event_type': schema.event_type.value,
                    'severity': schema.severity.value,
                    'fields': {
                        fname: {
                            'dtype': fdef.dtype.__name__,
                            'required': fdef.required,
                            'nullable': fdef.nullable
                        }
                        for fname, fdef in sorted(schema.fields.items())
                    }
                }
            
            serialized = json.dumps(schema_data, sort_keys=True)
            return hashlib.sha256(serialized.encode()).hexdigest()
    
    def validate_event(
        self,
        schema_name: str,
        event_data: dict[str, Any],
        schema_version: Optional[int] = None
    ) -> None:
        """
        Validate event against schema.
        
        This is the main validation entry point.
        Raises ValueError if validation fails.
        """
        schema = self.get(schema_name, schema_version)
        schema.validate_event(event_data)


# ============================================================================
# SCHEMA WATCHDOG (PARANOID ENFORCEMENT)
# ============================================================================

class SchemaWatchdog:
    """
    Paranoid watchdog for schema integrity.
    
    Monitors:
    - Unknown schemas
    - Unregistered emitters
    - Schema mismatch at runtime
    - Replay incompatibility
    
    Can:
    - Halt execution
    - Mark run invalid
    - Trigger global infra kill-switch
    """
    
    def __init__(self, registry: SchemaRegistry):
        self.registry = registry
        self._baseline_hash: Optional[str] = None
        self._violation_count = 0
        self._lock = threading.Lock()
    
    def set_baseline(self) -> None:
        """Set baseline schema hash for drift detection."""
        with self._lock:
            self._baseline_hash = self.registry.snapshot_hash()
    
    def check_drift(self) -> bool:
        """
        Check for schema drift.
        
        Returns True if drift detected.
        """
        with self._lock:
            if self._baseline_hash is None:
                return False
            
            current_hash = self.registry.snapshot_hash()
            return current_hash != self._baseline_hash
    
    def record_violation(self) -> None:
        """Record a schema validation violation."""
        with self._lock:
            self._violation_count += 1
    
    def get_violation_count(self) -> int:
        """Get total violation count."""
        with self._lock:
            return self._violation_count
    
    def enforce_integrity(self) -> None:
        """
        Enforce schema integrity.
        
        Raises SystemExit if critical violations detected.
        """
        if self.check_drift():
            raise SystemExit(
                "Schema drift detected. Registry hash changed during execution. "
                "Run invalidated."
            )
        
        if self.get_violation_count() > 0:
            raise SystemExit(
                f"Schema validation violations detected: {self._violation_count}. "
                f"Run invalidated."
            )


# ============================================================================
# PRODUCTION SCHEMA DEFINITIONS
# ============================================================================

def register_production_schemas(registry: SchemaRegistry) -> None:
    """
    Register all production log schemas.
    
    This is where ALL log events are defined.
    No ad-hoc logging allowed.
    """
    
    # ========================================================================
    # SYSTEM SCHEMAS
    # ========================================================================
    
    registry.register(LogSchema(
        name="system_startup",
        version=1,
        event_type=LogEventType.SYSTEM,
        severity=LogSeverity.INFO,
        fields={
            "run_id": LogFieldDefinition(
                name="run_id",
                dtype=str,
                required=True,
                nullable=False,
                description="Unique run identifier",
                invariants=["id_format:run_id"]
            ),
            "timestamp": LogFieldDefinition(
                name="timestamp",
                dtype=int,
                required=True,
                nullable=False,
                description="Startup timestamp in ms",
                invariants=["positive"]
            ),
            "version": LogFieldDefinition(
                name="version",
                dtype=str,
                required=True,
                nullable=False,
                description="System version",
                invariants=["non_empty"]
            ),
            "environment": LogFieldDefinition(
                name="environment",
                dtype=str,
                required=True,
                nullable=False,
                description="Execution environment",
                invariants=["enum:development,staging,production"]
            )
        },
        description="System startup event",
        producer_module="infra.runtime_context",
        replay_safe=True
    ))
    
    registry.register(LogSchema(
        name="system_error",
        version=1,
        event_type=LogEventType.SYSTEM,
        severity=LogSeverity.ERROR,
        fields={
            "run_id": LogFieldDefinition(
                name="run_id",
                dtype=str,
                required=True,
                nullable=False,
                description="Unique run identifier",
                invariants=["id_format:run_id"]
            ),
            "timestamp": LogFieldDefinition(
                name="timestamp",
                dtype=int,
                required=True,
                nullable=False,
                description="Error timestamp in ms",
                invariants=["positive"]
            ),
            "error_type": LogFieldDefinition(
                name="error_type",
                dtype=str,
                required=True,
                nullable=False,
                description="Error type/class",
                invariants=["non_empty"]
            ),
            "error_message": LogFieldDefinition(
                name="error_message",
                dtype=str,
                required=True,
                nullable=False,
                description="Error message",
                invariants=["non_empty"]
            ),
            "stack_trace": LogFieldDefinition(
                name="stack_trace",
                dtype=str,
                required=False,
                nullable=True,
                description="Stack trace if available",
                invariants=[]
            )
        },
        description="System error event",
        producer_module="infra.logging",
        replay_safe=True
    ))
    
    # ========================================================================
    # CONTENT SCHEMAS
    # ========================================================================
    
    registry.register(LogSchema(
        name="content_generated",
        version=1,
        event_type=LogEventType.CONTENT,
        severity=LogSeverity.INFO,
        fields={
            "content_id": LogFieldDefinition(
                name="content_id",
                dtype=str,
                required=True,
                nullable=False,
                description="Unique content identifier",
                invariants=["id_format:content_id"]
            ),
            "timestamp": LogFieldDefinition(
                name="timestamp",
                dtype=int,
                required=True,
                nullable=False,
                description="Generation timestamp in ms",
                invariants=["positive"]
            ),
            "strategy": LogFieldDefinition(
                name="strategy",
                dtype=str,
                required=True,
                nullable=False,
                description="Content strategy used",
                invariants=["non_empty"]
            ),
            "platform": LogFieldDefinition(
                name="platform",
                dtype=str,
                required=True,
                nullable=False,
                description="Target platform",
                invariants=["enum:twitter,reddit,youtube"]
            ),
            "length_chars": LogFieldDefinition(
                name="length_chars",
                dtype=int,
                required=True,
                nullable=False,
                description="Content length in characters",
                invariants=["non_negative"]
            )
        },
        description="Content generation event",
        producer_module="content.content_generator",
        replay_safe=True
    ))
    
    # ========================================================================
    # POSTING SCHEMAS
    # ========================================================================
    
    registry.register(LogSchema(
        name="posting_attempted",
        version=1,
        event_type=LogEventType.POSTING,
        severity=LogSeverity.INFO,
        fields={
            "content_id": LogFieldDefinition(
                name="content_id",
                dtype=str,
                required=True,
                nullable=False,
                description="Content being posted",
                invariants=["id_format:content_id"]
            ),
            "account_id": LogFieldDefinition(
                name="account_id",
                dtype=str,
                required=True,
                nullable=False,
                description="Account posting from",
                invariants=["id_format:account_id"]
            ),
            "timestamp": LogFieldDefinition(
                name="timestamp",
                dtype=int,
                required=True,
                nullable=False,
                description="Attempt timestamp in ms",
                invariants=["positive"]
            ),
            "platform": LogFieldDefinition(
                name="platform",
                dtype=str,
                required=True,
                nullable=False,
                description="Platform being posted to",
                invariants=["enum:twitter,reddit,youtube"]
            )
        },
        description="Posting attempt event",
        producer_module="posting.posting_engine",
        replay_safe=True
    ))
    
    # ========================================================================
    # SAFETY SCHEMAS
    # ========================================================================
    
    registry.register(LogSchema(
        name="safety_violation",
        version=1,
        event_type=LogEventType.SAFETY,
        severity=LogSeverity.CRITICAL,
        fields={
            "content_id": LogFieldDefinition(
                name="content_id",
                dtype=str,
                required=True,
                nullable=False,
                description="Content that violated safety",
                invariants=["id_format:content_id"]
            ),
            "timestamp": LogFieldDefinition(
                name="timestamp",
                dtype=int,
                required=True,
                nullable=False,
                description="Violation timestamp in ms",
                invariants=["positive"]
            ),
            "violation_type": LogFieldDefinition(
                name="violation_type",
                dtype=str,
                required=True,
                nullable=False,
                description="Type of safety violation",
                invariants=["non_empty"]
            ),
            "severity_score": LogFieldDefinition(
                name="severity_score",
                dtype=float,
                required=True,
                nullable=False,
                description="Violation severity (0.0-1.0)",
                invariants=["non_negative"]
            ),
            "blocked": LogFieldDefinition(
                name="blocked",
                dtype=bool,
                required=True,
                nullable=False,
                description="Whether content was blocked",
                invariants=[]
            )
        },
        description="Safety violation detected",
        producer_module="safety.safety_filter",
        replay_safe=True
    ))


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Initialize registry and watchdog
    registry = SchemaRegistry()
    register_production_schemas(registry)
    
    watchdog = SchemaWatchdog(registry)
    watchdog.set_baseline()
    
    print("Log Schema Registry Initialized")
    print(f"Registry Hash: {registry.snapshot_hash()[:16]}...")
    print(f"Total Schemas: {len(registry._schemas)}")
    
    # Example: Validate a valid event
    valid_event = {
        "run_id": "run_abc123def4567890",
        "timestamp": 1640000000000,
        "version": "1.0.0",
        "environment": "production"
    }
    
    try:
        registry.validate_event("system_startup", valid_event)
        print("\n✓ Valid event passed validation")
    except ValueError as e:
        print(f"\n✗ Validation failed: {e}")
    
    # Example: Validate an invalid event (missing required field)
    invalid_event = {
        "run_id": "run_abc123def4567890",
        "timestamp": 1640000000000,
        # Missing 'version' and 'environment'
    }
    
    try:
        registry.validate_event("system_startup", invalid_event)
        print("✓ Invalid event passed (THIS SHOULD NOT HAPPEN)")
    except ValueError as e:
        print(f"✓ Invalid event correctly rejected: {e}")
    
    # Example: Validate event with invariant violation
    invariant_violation = {
        "run_id": "invalid_id_format",  # Wrong format
        "timestamp": 1640000000000,
        "version": "1.0.0",
        "environment": "production"
    }
    
    try:
        registry.validate_event("system_startup", invariant_violation)
        print("✓ Invariant violation passed (THIS SHOULD NOT HAPPEN)")
    except ValueError as e:
        print(f"✓ Invariant violation correctly rejected: {e}")
    
    # Check integrity
    print(f"\nWatchdog Status: {'✓ CLEAN' if not watchdog.check_drift() else '✗ DRIFT DETECTED'}")
    print(f"Violations: {watchdog.get_violation_count()}")





