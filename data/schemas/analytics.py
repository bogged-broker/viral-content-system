#!/usr/bin/env python3
"""
/data/schemas/analytics.py

Canonical Derived Analytics Schemas (Explicitly Computed, Never Observed)

CRITICAL: This file defines derived metrics — values that did not occur
directly, but were computed from canonical facts. Analytics must confess
how they were computed.

Design Principle: Analytics must confess how they were computed.
Any metric without a declared source and formula is invalid.

Analytics are answers to math questions — not decisions.
If you can't replay it, you can't trust it.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple, Dict, Any, Union


# ============================================================================
# CONSTANTS & CONFIGURATION
# ============================================================================

SCHEMA_NAME = "analytics"
SUPPORTED_SCHEMA_VERSIONS = {1, 2}

# Maximum allowed value (prevent overflow)
MAX_METRIC_VALUE = 1e15

# Minimum timestamp (epoch start)
MIN_TIMESTAMP = 0


# ============================================================================
# ENUMS & TYPE DEFINITIONS
# ============================================================================

class AnalyticsScope(Enum):
    """
    Scope declares what is being summarized, not why.
    
    This is about aggregation target, not business meaning.
    """
    CONTENT = "content"
    ACCOUNT = "account"
    WORKFLOW = "workflow"
    SYSTEM = "system"
    
    def __str__(self) -> str:
        """String representation."""
        return self.value
    
    @classmethod
    def from_string(cls, value: str) -> 'AnalyticsScope':
        """
        Parse scope from string.
        
        Args:
            value: String representation
            
        Returns:
            AnalyticsScope enum
            
        Raises:
            ValueError: If value is not valid
        """
        try:
            return cls(value.lower())
        except (ValueError, AttributeError):
            valid_scopes = [s.value for s in cls]
            raise ValueError(
                f"Invalid analytics scope: {value}. Must be one of: {valid_scopes}"
            )


class AnalyticsMetricKind(Enum):
    """
    Kind describes math shape — not importance.
    
    This is about the type of aggregation, not what it means.
    """
    COUNT = "count"
    RATE = "rate"
    RATIO = "ratio"
    DURATION = "duration"
    AGGREGATE = "aggregate"
    
    def __str__(self) -> str:
        """String representation."""
        return self.value
    
    @classmethod
    def from_string(cls, value: str) -> 'AnalyticsMetricKind':
        """
        Parse metric kind from string.
        
        Args:
            value: String representation
            
        Returns:
            AnalyticsMetricKind enum
            
        Raises:
            ValueError: If value is not valid
        """
        try:
            return cls(value.lower())
        except (ValueError, AttributeError):
            valid_kinds = [k.value for k in cls]
            raise ValueError(
                f"Invalid metric kind: {value}. Must be one of: {valid_kinds}"
            )


# ============================================================================
# VALIDATION ERRORS
# ============================================================================

class AnalyticsValidationError(Exception):
    """
    Raised when analytics validation fails.
    
    All validation failures are HARD FAILURES.
    Bad math → reject, don't patch.
    """
    pass


class AnalyticsInvariantViolation(Exception):
    """
    Raised when analytics violate core invariants.
    
    This indicates corruption of reproducibility guarantees.
    """
    pass


# ============================================================================
# SOURCE DECLARATION (MANDATORY)
# ============================================================================

@dataclass(frozen=True)
class AnalyticsSource:
    """
    Explicit declaration of data source for analytics.
    
    Rules:
    - Queries must be hashable
    - Ad-hoc queries are forbidden
    - Same query → same hash forever
    
    Sources are immutable and versioned.
    """
    
    # Source schema identity
    schema_name: str  # engagement, moderation, content, etc.
    schema_version: int
    
    # Query identity (deterministic hash)
    source_query_hash: str
    
    # Optional: human-readable query description
    query_description: str = ""
    
    def __post_init__(self):
        """Post-initialization validation."""
        self.validate()
    
    def validate(self) -> None:
        """
        Validate source declaration.
        
        Raises:
            AnalyticsValidationError: If validation fails
        """
        # Schema name must be non-empty
        if not self.schema_name or not self.schema_name.strip():
            raise AnalyticsValidationError("Source schema_name must be non-empty")
        
        # Schema version must be positive
        if self.schema_version < 1:
            raise AnalyticsValidationError(
                f"Source schema_version must be >= 1, got {self.schema_version}"
            )
        
        # Query hash must be non-empty and look like a hash
        if not self.source_query_hash or not self.source_query_hash.strip():
            raise AnalyticsValidationError("Source query hash must be non-empty")
        
        # Query hash should be hex string (basic check)
        try:
            int(self.source_query_hash, 16)
        except ValueError:
            raise AnalyticsValidationError(
                f"Source query hash must be hex string: {self.source_query_hash}"
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'schema_name': self.schema_name,
            'schema_version': self.schema_version,
            'source_query_hash': self.source_query_hash,
            'query_description': self.query_description,
        }
    
    def compute_hash(self) -> str:
        """
        Compute deterministic hash of source.
        
        Returns:
            SHA-256 hash (hex)
        """
        canonical = {
            'schema_name': self.schema_name.strip().lower(),
            'schema_version': self.schema_version,
            'source_query_hash': self.source_query_hash.strip().lower(),
        }
        
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


# ============================================================================
# COMPUTATION DECLARATION (MANDATORY)
# ============================================================================

@dataclass(frozen=True)
class AnalyticsComputation:
    """
    Explicit declaration of computation used to derive analytics.
    
    Rules:
    - Algorithm must be versioned
    - Parameters are serialized, not inferred
    - No runtime tuning hidden here
    
    Computations are deterministic and replayable.
    """
    
    # Deterministic computation identity
    computation_id: str
    
    # Versioned algorithm reference (opaque)
    algorithm_ref: str
    
    # Computation parameters (sorted tuples for determinism)
    parameters: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    
    # Optional: human-readable description
    description: str = ""
    
    def __post_init__(self):
        """Post-initialization validation."""
        self.validate()
    
    def validate(self) -> None:
        """
        Validate computation declaration.
        
        Raises:
            AnalyticsValidationError: If validation fails
        """
        # Computation ID must be non-empty
        if not self.computation_id or not self.computation_id.strip():
            raise AnalyticsValidationError("Computation ID must be non-empty")
        
        # Algorithm ref must be non-empty and versioned
        if not self.algorithm_ref or not self.algorithm_ref.strip():
            raise AnalyticsValidationError("Algorithm ref must be non-empty")
        
        if '@' not in self.algorithm_ref and ':' not in self.algorithm_ref:
            raise AnalyticsValidationError(
                f"Algorithm ref must be versioned (use '@' or ':'): {self.algorithm_ref}"
            )
        
        # Parameters must be valid tuples
        if self.parameters:
            for param in self.parameters:
                if not isinstance(param, tuple) or len(param) != 2:
                    raise AnalyticsValidationError(
                        f"Invalid parameter format: {param}. Must be (key, value) tuple"
                    )
                if not all(isinstance(p, str) for p in param):
                    raise AnalyticsValidationError(
                        f"Parameter must contain strings: {param}"
                    )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'computation_id': self.computation_id,
            'algorithm_ref': self.algorithm_ref,
            'parameters': sorted([
                (k.strip(), v.strip()) for k, v in self.parameters
            ]) if self.parameters else [],
            'description': self.description,
        }
    
    def compute_hash(self) -> str:
        """
        Compute deterministic hash of computation.
        
        Returns:
            SHA-256 hash (hex)
        """
        canonical = {
            'computation_id': self.computation_id.strip(),
            'algorithm_ref': self.algorithm_ref.strip(),
            'parameters': sorted([
                (k.strip(), v.strip()) for k, v in self.parameters
            ]) if self.parameters else [],
        }
        
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


# ============================================================================
# TIME WINDOW DECLARATION
# ============================================================================

@dataclass(frozen=True)
class AnalyticsWindow:
    """
    Explicit time window for analytics computation.
    
    Rules:
    - Inclusive of start, exclusive of end
    - No "rolling" or implicit windows
    - Window must be explicit or NULL (lifetime)
    
    Time windows are deterministic and bounded.
    """
    
    # UTC epoch seconds
    start_timestamp: int
    end_timestamp: int
    
    # Optional: human-readable description
    description: str = ""
    
    def __post_init__(self):
        """Post-initialization validation."""
        self.validate()
    
    def validate(self) -> None:
        """
        Validate time window.
        
        Raises:
            AnalyticsValidationError: If validation fails
        """
        # Timestamps must be non-negative
        if self.start_timestamp < MIN_TIMESTAMP:
            raise AnalyticsValidationError(
                f"Start timestamp must be >= {MIN_TIMESTAMP}, got {self.start_timestamp}"
            )
        
        if self.end_timestamp < MIN_TIMESTAMP:
            raise AnalyticsValidationError(
                f"End timestamp must be >= {MIN_TIMESTAMP}, got {self.end_timestamp}"
            )
        
        # End must be after start
        if self.end_timestamp <= self.start_timestamp:
            raise AnalyticsValidationError(
                f"End timestamp ({self.end_timestamp}) must be after "
                f"start timestamp ({self.start_timestamp})"
            )
        
        # Window should not be unreasonably large (sanity check)
        window_duration = self.end_timestamp - self.start_timestamp
        max_duration = 86400 * 365 * 10  # 10 years
        if window_duration > max_duration:
            raise AnalyticsValidationError(
                f"Window duration ({window_duration}s) exceeds maximum ({max_duration}s)"
            )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            'start_timestamp': self.start_timestamp,
            'end_timestamp': self.end_timestamp,
            'description': self.description,
        }
    
    def compute_hash(self) -> str:
        """
        Compute deterministic hash of window.
        
        Returns:
            SHA-256 hash (hex)
        """
        canonical = {
            'start_timestamp': self.start_timestamp,
            'end_timestamp': self.end_timestamp,
        }
        
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
    
    def duration_seconds(self) -> int:
        """Get window duration in seconds."""
        return self.end_timestamp - self.start_timestamp


# ============================================================================
# CORE SCHEMA: ANALYTICS METRIC
# ============================================================================

@dataclass(frozen=True)
class AnalyticsMetric:
    """
    Canonical derived analytics metric.
    
    This is the atomic analytics artifact.
    
    CRITICAL RULES:
    - Analytics are downstream artifacts only
    - Never override raw data
    - No policy or meaning allowed
    - Reproducibility is sacred
    
    If a number changes behavior, it does not belong here.
    """
    
    # ========================================================================
    # IDENTITY
    # ========================================================================
    
    # Deterministic analytics identifier
    analytics_id: str
    
    # Schema metadata
    schema_name: str
    schema_version: int
    
    # ========================================================================
    # SCOPE (what is being summarized)
    # ========================================================================
    
    # Scope of aggregation
    scope: AnalyticsScope
    
    # Scope identifier (content_id, account_id, etc.)
    # None for system-wide metrics
    scope_id: Optional[str] = None
    
    # ========================================================================
    # METRIC (the actual number)
    # ========================================================================
    
    # Metric name (descriptive, but not semantic)
    metric_name: str
    
    # Kind of metric (describes math shape)
    kind: AnalyticsMetricKind
    
    # Metric value (finite float)
    value: float
    
    # ========================================================================
    # PROVENANCE (how it was computed)
    # ========================================================================
    
    # Data sources (non-empty)
    sources: Tuple[AnalyticsSource, ...]
    
    # Computation specification
    computation: AnalyticsComputation
    
    # Time window (None = lifetime/all-time)
    window: Optional[AnalyticsWindow] = None
    
    # ========================================================================
    # TIMING
    # ========================================================================
    
    # When this metric was computed (UTC epoch seconds)
    computed_at: int
    
    # Optional metadata (does not affect analytics_id)
    metadata: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    
    def __post_init__(self):
        """Post-initialization validation."""
        self.validate()
    
    def validate(self) -> None:
        """
        Validate analytics metric.
        
        Raises:
            AnalyticsValidationError: If validation fails
        """
        # ====================================================================
        # IDENTITY VALIDATION
        # ====================================================================
        
        # Analytics ID must be non-empty
        if not self.analytics_id or not self.analytics_id.strip():
            raise AnalyticsValidationError("Analytics ID must be non-empty")
        
        # Schema name must be "analytics"
        if self.schema_name != SCHEMA_NAME:
            raise AnalyticsValidationError(
                f"Schema name must be '{SCHEMA_NAME}', got '{self.schema_name}'"
            )
        
        # Schema version must be supported
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise AnalyticsValidationError(
                f"Unsupported schema version: {self.schema_version}. "
                f"Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        
        # ====================================================================
        # SCOPE VALIDATION
        # ====================================================================
        
        # Scope must be valid enum
        if not isinstance(self.scope, AnalyticsScope):
            raise AnalyticsValidationError(
                f"Invalid scope type: {type(self.scope)}"
            )
        
        # Scope ID presence must match scope
        scope_requires_id = self.scope in {
            AnalyticsScope.CONTENT,
            AnalyticsScope.ACCOUNT,
            AnalyticsScope.WORKFLOW,
        }
        
        if scope_requires_id and not self.scope_id:
            raise AnalyticsValidationError(
                f"Scope '{self.scope}' requires scope_id"
            )
        
        if self.scope == AnalyticsScope.SYSTEM and self.scope_id:
            raise AnalyticsValidationError(
                f"System scope must not have scope_id"
            )
        
        # Scope ID must be non-empty if present
        if self.scope_id is not None and not self.scope_id.strip():
            raise AnalyticsValidationError("Scope ID must be non-empty if present")
        
        # ====================================================================
        # METRIC VALIDATION
        # ====================================================================
        
        # Metric name must be non-empty
        if not self.metric_name or not self.metric_name.strip():
            raise AnalyticsValidationError("Metric name must be non-empty")
        
        # Kind must be valid enum
        if not isinstance(self.kind, AnalyticsMetricKind):
            raise AnalyticsValidationError(
                f"Invalid metric kind type: {type(self.kind)}"
            )
        
        # Value must be finite (no NaN or inf)
        if not math.isfinite(self.value):
            raise AnalyticsValidationError(
                f"Metric value must be finite, got {self.value}"
            )
        
        # Value must not exceed maximum
        if abs(self.value) > MAX_METRIC_VALUE:
            raise AnalyticsValidationError(
                f"Metric value {self.value} exceeds maximum {MAX_METRIC_VALUE}"
            )
        
        # ====================================================================
        # PROVENANCE VALIDATION
        # ====================================================================
        
        # Sources must be non-empty
        if not self.sources:
            raise AnalyticsValidationError("Analytics must have at least one source")
        
        # Validate all sources
        for i, source in enumerate(self.sources):
            if not isinstance(source, AnalyticsSource):
                raise AnalyticsValidationError(
                    f"Source {i} is not AnalyticsSource: {type(source)}"
                )
            try:
                source.validate()
            except AnalyticsValidationError as e:
                raise AnalyticsValidationError(f"Source {i} validation failed: {e}")
        
        # Computation must be valid
        if not isinstance(self.computation, AnalyticsComputation):
            raise AnalyticsValidationError(
                f"Invalid computation type: {type(self.computation)}"
            )
        try:
            self.computation.validate()
        except AnalyticsValidationError as e:
            raise AnalyticsValidationError(f"Computation validation failed: {e}")
        
        # Window must be valid if present
        if self.window is not None:
            if not isinstance(self.window, AnalyticsWindow):
                raise AnalyticsValidationError(
                    f"Invalid window type: {type(self.window)}"
                )
            try:
                self.window.validate()
            except AnalyticsValidationError as e:
                raise AnalyticsValidationError(f"Window validation failed: {e}")
        
        # ====================================================================
        # TIMING VALIDATION
        # ====================================================================
        
        # Computed timestamp must be non-negative
        if self.computed_at < MIN_TIMESTAMP:
            raise AnalyticsValidationError(
                f"Computed timestamp must be >= {MIN_TIMESTAMP}, got {self.computed_at}"
            )
        
        # If window exists, computed_at should be >= window.end_timestamp
        # (can't compute analytics before window closes)
        if self.window and self.computed_at < self.window.end_timestamp:
            # Allow some grace period for processing
            grace_period = 3600  # 1 hour
            if self.computed_at < self.window.end_timestamp - grace_period:
                raise AnalyticsValidationError(
                    f"Computed timestamp ({self.computed_at}) is before window end "
                    f"({self.window.end_timestamp})"
                )
    
    def compute_deterministic_id(self) -> str:
        """
        Compute deterministic analytics ID.
        
        ID is derived from:
        - scope + scope_id
        - metric_name
        - computation_id
        - window boundaries
        - source_query_hashes
        
        No UUIDs. No counters.
        
        Returns:
            SHA-256 hash (hex)
        """
        # Build canonical representation
        canonical = {
            'scope': self.scope.value,
            'scope_id': self.scope_id.strip() if self.scope_id else None,
            'metric_name': self.metric_name.strip().lower(),
            'computation_id': self.computation.computation_id.strip(),
            'sources': sorted([src.compute_hash() for src in self.sources]),
        }
        
        # Add window if present
        if self.window:
            canonical['window'] = self.window.compute_hash()
        else:
            canonical['window'] = None
        
        # Serialize deterministically
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
        
        # Hash
        analytics_id = hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()
        
        return analytics_id
    
    def verify_id(self) -> bool:
        """
        Verify that analytics_id matches computed ID.
        
        Returns:
            True if ID is correct, False otherwise
        """
        computed_id = self.compute_deterministic_id()
        return self.analytics_id == computed_id
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary with stable ordering.
        
        Ordering:
        1. identity
        2. scope
        3. metric
        4. provenance
        5. timing
        
        Returns:
            Ordered dictionary
        """
        result = {}
        
        # Identity
        result['analytics_id'] = self.analytics_id
        result['schema_name'] = self.schema_name
        result['schema_version'] = self.schema_version
        
        # Scope
        result['scope'] = self.scope.value
        result['scope_id'] = self.scope_id
        
        # Metric
        result['metric_name'] = self.metric_name
        result['kind'] = self.kind.value
        result['value'] = self.value
        
        # Provenance
        result['sources'] = [src.to_dict() for src in self.sources]
        result['computation'] = self.computation.to_dict()
        result['window'] = self.window.to_dict() if self.window else None
        
        # Timing
        result['computed_at'] = self.computed_at
        
        # Metadata (optional)
        if self.metadata:
            result['metadata'] = sorted([
                (k.strip(), v.strip()) for k, v in self.metadata
            ])
        
        return result
    
    def to_json(self) -> str:
        """
        Serialize to JSON with stable formatting.
        
        Returns:
            JSON string
        """
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)
    
    def to_canonical_json(self) -> str:
        """
        Serialize to canonical JSON (for hashing).
        
        Returns:
            Canonical JSON string
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=True
        )
    
    def check_invariants(self) -> None:
        """
        Check core invariants.
        
        Raises:
            AnalyticsInvariantViolation: If any invariant is violated
        """
        # Invariant: Analytics ID is deterministic
        if not self.verify_id():
            raise AnalyticsInvariantViolation(
                f"Analytics ID does not match computed ID. "
                f"Stored: {self.analytics_id}, "
                f"Computed: {self.compute_deterministic_id()}"
            )
        
        # Invariant: Same metric → same hash (reproducibility)
        hash_1 = self.compute_deterministic_id()
        hash_2 = self.compute_deterministic_id()
        if hash_1 != hash_2:
            raise AnalyticsInvariantViolation(
                f"Analytics ID is non-deterministic: {hash_1} != {hash_2}"
            )
        
        # Invariant: Value is finite
        if not math.isfinite(self.value):
            raise AnalyticsInvariantViolation(
                f"Metric value is not finite: {self.value}"
            )
        
        # Invariant: Sources are immutable
        # (frozen dataclass ensures this, but verify)
        try:
            # This should fail on frozen dataclass
            object.__setattr__(self, 'value', 999.9)
            raise AnalyticsInvariantViolation("Analytics is mutable!")
        except AttributeError:
            # Expected: cannot modify frozen dataclass
            pass


# ============================================================================
# ANALYTICS FACTORY
# ============================================================================

class AnalyticsFactory:
    """
    Factory for creating analytics metrics from configuration.
    
    Handles validation and ID computation.
    """
    
    @staticmethod
    def create_metric(
        scope: AnalyticsScope,
        scope_id: Optional[str],
        metric_name: str,
        kind: AnalyticsMetricKind,
        value: float,
        sources: Tuple[AnalyticsSource, ...],
        computation: AnalyticsComputation,
        window: Optional[AnalyticsWindow],
        computed_at: int,
        schema_version: int = 1,
        metadata: Tuple[Tuple[str, str], ...] = ()
    ) -> AnalyticsMetric:
        """
        Create analytics metric with computed ID.
        
        Args:
            scope: Analytics scope
            scope_id: Scope identifier (or None for system)
            metric_name: Metric name
            kind: Metric kind
            value: Metric value
            sources: Data sources
            computation: Computation specification
            window: Time window (or None for lifetime)
            computed_at: Computation timestamp
            schema_version: Schema version
            metadata: Optional metadata
            
        Returns:
            Validated AnalyticsMetric
            
        Raises:
            AnalyticsValidationError: If metric is invalid
        """
        # Create metric with placeholder ID
        temp_metric = AnalyticsMetric(
            analytics_id="temp",
            schema_name=SCHEMA_NAME,
            schema_version=schema_version,
            scope=scope,
            scope_id=scope_id,
            metric_name=metric_name,
            kind=kind,
            value=value,
            sources=sources,
            computation=computation,
            window=window,
            computed_at=computed_at,
            metadata=metadata,
        )
        
        # Compute deterministic ID
        analytics_id = temp_metric.compute_deterministic_id()
        
        # Create final metric with computed ID
        metric = AnalyticsMetric(
            analytics_id=analytics_id,
            schema_name=SCHEMA_NAME,
            schema_version=schema_version,
            scope=scope,
            scope_id=scope_id,
            metric_name=metric_name,
            kind=kind,
            value=value,
            sources=sources,
            computation=computation,
            window=window,
            computed_at=computed_at,
            metadata=metadata,
        )
        
        # Validate
        metric.validate()
        metric.check_invariants()
        
        return metric
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> AnalyticsMetric:
        """
        Create analytics metric from dictionary.
        
        Args:
            data: Dictionary representation
            
        Returns:
            AnalyticsMetric instance
            
        Raises:
            AnalyticsValidationError: If metric cannot be created
        """
        # Parse scope
        scope = AnalyticsScope.from_string(data['scope'])
        
        # Parse kind
        kind = AnalyticsMetricKind.from_string(data['kind'])
        
        # Parse sources
        sources_data = data.get('sources', [])
        sources = tuple(
            AnalyticsSource(**src_data) for src_data in sources_data
        )
        
        # Parse computation
        comp_data = data['computation']
        comp_params = tuple(
            tuple(p) for p in comp_data.get('parameters', [])
        )
        computation = AnalyticsComputation(
            computation_id=comp_data['computation_id'],
            algorithm_ref=comp_data['algorithm_ref'],
            parameters=comp_params,
            description=comp_data.get('description', ''),
        )
        
        # Parse window
        window = None
        if data.get('window'):
            window_data = data['window']
            window = AnalyticsWindow(
                start_timestamp=window_data['start_timestamp'],
                end_timestamp=window_data['end_timestamp'],
                description=window_data.get('description', ''),
            )
        
        # Parse metadata
        metadata_raw = data.get('metadata', [])
        metadata = tuple(tuple(m) for m in metadata_raw) if metadata_raw else ()
        
        # Create metric
        return AnalyticsMetric(
            analytics_id=data['analytics_id'],
            schema_name=data['schema_name'],
            schema_version=data['schema_version'],
            scope=scope,
            scope_id=data.get('scope_id'),
            metric_name=data['metric_name'],
            kind=kind,
            value=float(data['value']),
            sources=sources,
            computation=computation,
            window=window,
            computed_at=data['computed_at'],
            metadata=metadata,
        )
    
    @staticmethod
    def from_json(json_str: str) -> AnalyticsMetric:
        """
        Create analytics metric from JSON string.
        
        Args:
            json_str: JSON string
            
        Returns:
            AnalyticsMetric instance
            
        Raises:
            AnalyticsValidationError: If metric cannot be created
        """
        try:
            data = json.loads(json_str)
            return AnalyticsFactory.from_dict(data)
        except json.JSONDecodeError as e:
            raise AnalyticsValidationError(f"Invalid JSON: {e}")


# ============================================================================
# FORBIDDEN PATTERN DETECTOR
# ============================================================================

class ForbiddenPatternDetector:
    """
    Detects forbidden patterns in analytics metrics.
    
    ZERO TOLERANCE for:
    - "Score" fields
    - Trust weighting
    - Policy flags
    - Confidence modifiers
    - Predictive labels
    - Hidden smoothing
    - Rolling averages without window
    - Backfilled mutation
    
    Analytics may summarize — never judge.
    """
    
    # Forbidden keywords in metric names
    FORBIDDEN_KEYWORDS = {
        'score',
        'trust',
        'confidence',
        'prediction',
        'forecast',
        'recommended',
        'quality',
        'reputation',
        'credibility',
        'rank',
        'ranking',
        'weight',
        'penalty',
        'boost',
        'adjusted',
        'normalized',  # allowed for specific math, flagged for review
        'smoothed',
        'filtered',
        'curated',
    }
    
    # Allowed exceptions (when combined with other words)
    ALLOWED_EXCEPTIONS = {
        'count',
        'raw_count',
        'total_count',
    }
    
    @staticmethod
    def check_metric_name(metric_name: str) -> None:
        """
        Check metric name for forbidden patterns.
        
        Args:
            metric_name: Metric name to check
            
        Raises:
            AnalyticsValidationError: If forbidden pattern detected
        """
        metric_lower = metric_name.lower().strip()
        
        # Check for forbidden keywords
        for keyword in ForbiddenPatternDetector.FORBIDDEN_KEYWORDS:
            if keyword in metric_lower:
                # Check if it's an allowed exception
                is_allowed = any(
                    exception in metric_lower
                    for exception in ForbiddenPatternDetector.ALLOWED_EXCEPTIONS
                )
                
                if not is_allowed:
                    raise AnalyticsValidationError(
                        f"Forbidden keyword '{keyword}' in metric name: {metric_name}. "
                        f"Analytics may summarize — never judge."
                    )
    
    @staticmethod
    def check_computation_algorithm(algorithm_ref: str) -> None:
        """
        Check algorithm reference for forbidden patterns.
        
        Args:
            algorithm_ref: Algorithm reference to check
            
        Raises:
            AnalyticsValidationError: If forbidden pattern detected
        """
        algorithm_lower = algorithm_ref.lower()
        
        # Check for smoothing without explicit window
        if 'smooth' in algorithm_lower or 'rolling' in algorithm_lower:
            # These require explicit window declaration
            raise AnalyticsValidationError(
                f"Algorithm '{algorithm_ref}' appears to use smoothing/rolling. "
                f"This requires explicit window declaration."
            )
        
        # Check for ML/prediction keywords
        ml_keywords = ['predict', 'forecast', 'model', 'neural', 'learning']
        for keyword in ml_keywords:
            if keyword in algorithm_lower:
                raise AnalyticsValidationError(
                    f"Algorithm '{algorithm_ref}' appears to use ML/prediction "
                    f"(contains '{keyword}'). Analytics must be deterministic computations."
                )
    
    @staticmethod
    def check_all_patterns(metric: AnalyticsMetric) -> None:
        """
        Check all forbidden patterns for a metric.
        
        Args:
            metric: Metric to check
            
        Raises:
            AnalyticsValidationError: If any forbidden pattern detected
        """
        ForbiddenPatternDetector.check_metric_name(metric.metric_name)
        ForbiddenPatternDetector.check_computation_algorithm(
            metric.computation.algorithm_ref
        )


# ============================================================================
# ANALYTICS REGISTRY
# ============================================================================

class AnalyticsRegistry:
    """
    Registry of analytics metrics for tracking and validation.
    
    Ensures no duplicate metrics and provides lookup.
    """
    
    def __init__(self):
        """Initialize analytics registry."""
        self._metrics: Dict[str, AnalyticsMetric] = {}
    
    def register(self, metric: AnalyticsMetric) -> None:
        """
        Register an analytics metric.
        
        Args:
            metric: Analytics metric to register
            
        Raises:
            AnalyticsValidationError: If metric ID already exists
        """
        # Validate metric before registration
        metric.validate()
        metric.check_invariants()
        ForbiddenPatternDetector.check_all_patterns(metric)
        
        # Check for duplicate ID
        if metric.analytics_id in self._metrics:
            raise AnalyticsValidationError(
                f"Analytics ID already registered: {metric.analytics_id}"
            )
        
        # Register
        self._metrics[metric.analytics_id] = metric
    
    def get(self, analytics_id: str) -> Optional[AnalyticsMetric]:
        """
        Get metric by ID.
        
        Args:
            analytics_id: Analytics ID
            
        Returns:
            AnalyticsMetric if found, None otherwise
        """
        return self._metrics.get(analytics_id)
    
    def list_by_scope(self, scope: AnalyticsScope) -> list:
        """
        List metrics by scope.
        
        Args:
            scope: Analytics scope
            
        Returns:
            List of analytics IDs
        """
        return sorted([
            analytics_id for analytics_id, metric in self._metrics.items()
            if metric.scope == scope
        ])
    
    def list_by_kind(self, kind: AnalyticsMetricKind) -> list:
        """
        List metrics by kind.
        
        Args:
            kind: Metric kind
            
        Returns:
            List of analytics IDs
        """
        return sorted([
            analytics_id for analytics_id, metric in self._metrics.items()
            if metric.kind == kind
        ])
    
    def count_metrics(self) -> int:
        """Get total number of registered metrics."""
        return len(self._metrics)


# ============================================================================
# SERIALIZATION UTILITIES
# ============================================================================

class AnalyticsSerializer:
    """
    Utilities for analytics serialization with stable formatting.
    
    Bit-stable forever.
    """
    
    @staticmethod
    def to_json(metric: AnalyticsMetric, indent: int = 2) -> str:
        """
        Serialize metric to JSON.
        
        Args:
            metric: Metric to serialize
            indent: JSON indentation
            
        Returns:
            JSON string
        """
        return json.dumps(
            metric.to_dict(),
            indent=indent,
            sort_keys=True,
            ensure_ascii=True
        )
    
    @staticmethod
    def batch_to_json(metrics: list, indent: int = 2) -> str:
        """
        Serialize multiple metrics to JSON array.
        
        Args:
            metrics: List of metrics
            indent: JSON indentation
            
        Returns:
            JSON array string
        """
        metrics_list = [m.to_dict() for m in metrics]
        return json.dumps(
            metrics_list,
            indent=indent,
            sort_keys=True,
            ensure_ascii=True
        )
    
    @staticmethod
    def to_json_lines(metrics: list) -> str:
        """
        Serialize metrics to JSON Lines format.
        
        Args:
            metrics: List of metrics
            
        Returns:
            JSON Lines string (one metric per line)
        """
        lines = [m.to_canonical_json() for m in metrics]
        return '\n'.join(lines)


# ============================================================================
# COMMAND-LINE UTILITIES
# ============================================================================

def validate_metric_from_file(filepath: str) -> AnalyticsMetric:
    """
    Load and validate a metric from JSON file.
    
    Args:
        filepath: Path to metric JSON file
        
    Returns:
        Validated AnalyticsMetric
        
    Raises:
        AnalyticsValidationError: If metric is invalid
    """
    with open(filepath, 'r') as f:
        metric_json = f.read()
    
    metric = AnalyticsFactory.from_json(metric_json)
    metric.validate()
    metric.check_invariants()
    ForbiddenPatternDetector.check_all_patterns(metric)
    
    return metric


if __name__ == '__main__':
    import sys
    
    # Simple CLI for validation
    if len(sys.argv) < 2:
        print("Usage: python analytics.py <metric_file.json>")
        sys.exit(1)
    
    filepath = sys.argv[1]
    
    try:
        metric = validate_metric_from_file(filepath)
        print(f"✓ Metric '{metric.metric_name}' is valid")
        print(f"  Scope: {metric.scope.value}")
        print(f"  Kind: {metric.kind.value}")
        print(f"  Value: {metric.value}")
        print(f"  Analytics ID: {metric.analytics_id}")
        print(f"  ID verified: {metric.verify_id()}")
    except Exception as e:
        print(f"✗ Validation failed: {e}")
        sys.exit(1)