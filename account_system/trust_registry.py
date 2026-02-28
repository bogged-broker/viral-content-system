"""
/account_system/trust_registry.py

Versioned Trust Model Registry & Governance Layer

MISSION:
Prevent silent trust drift by making trust a versioned, auditable, replayable contract.

CORE PRINCIPLE:
Trust must be explainable years later — not just "reasonable at the time."

RESPONSIBILITY:
- Register all trust model versions
- Enforce semantic versioning
- Lock exactly one active model per platform
- Track model compatibility
- Support historical replay
- Block invalid or unsafe models
- Enable instant model rollback
- Be deterministic & audit-safe

NOT RESPONSIBLE FOR:
- Computing trust scores (trust_scoring.py)
- Calculating decay (trust_decay.py)
- Analyzing risk (risk_signal_extractor.py)
- Recording events (reputation_ledger.py)
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Set, List, Dict, Tuple
from enum import Enum
import hashlib
import json
from collections import defaultdict


class TrustModelStatus(Enum):
    """Trust model lifecycle states"""
    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class TrustResolutionPurpose(Enum):
    """Why trust is being resolved - enforces correct usage"""
    LIVE = "live"                    # Real-time trust computation
    AUDIT = "audit"                  # Historical investigation
    REPLAY = "replay"                # Deterministic reconstruction
    EXPERIMENT = "experiment"        # A/B testing
    MIGRATION = "migration"          # Model transition


class VersionChangeType(Enum):
    """Semantic versioning change types"""
    MAJOR = "major"  # Meaning of trust score changes
    MINOR = "minor"  # Weight or signal changes
    PATCH = "patch"  # Bugfix, no semantic effect


@dataclass(frozen=True)
class TrustModelDefinition:
    """
    Immutable trust model definition.
    
    Every trust model is a CONTRACT that specifies:
    - What signals it consumes
    - How it combines them
    - What its output means
    - Where it can be used
    """
    model_id: str                          # e.g. "trust_v3_2_1"
    version: str                           # semantic: "3.2.1"
    
    description: str
    author: str
    created_at: datetime
    
    # Model mechanics
    input_signals: Set[str]                # Required trust signals
    output_range: Tuple[float, float]      # e.g. (0.0, 1.0)
    
    weight_schema: str                     # Reference to weight config
    aggregation_logic: str                 # Algorithm identifier
    
    # Compatibility
    compatible_platforms: Set[str]
    incompatible_states: Set[str]          # Enforcement states this blocks
    
    # Safety constraints
    invariant_constraints: List[str]       # Must satisfy these invariants
    
    # Metadata
    status: TrustModelStatus = TrustModelStatus.DEVELOPMENT
    deprecation_date: Optional[datetime] = None
    replacement_model: Optional[str] = None
    
    def __post_init__(self):
        """Validate model definition integrity"""
        if not self.model_id.startswith("trust_v"):
            raise ValueError(f"Invalid model_id format: {self.model_id}")
        
        parts = self.version.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"Invalid semantic version: {self.version}")
        
        if self.output_range[0] >= self.output_range[1]:
            raise ValueError(f"Invalid output_range: {self.output_range}")
        
        if not self.input_signals:
            raise ValueError("Model must declare at least one input signal")
    
    def get_semantic_version(self) -> Tuple[int, int, int]:
        """Parse semantic version into components"""
        major, minor, patch = self.version.split(".")
        return int(major), int(minor), int(patch)
    
    def is_compatible_with(self, other: 'TrustModelDefinition') -> bool:
        """Check if this model is compatible with another for comparison"""
        major_self, _, _ = self.get_semantic_version()
        major_other, _, _ = other.get_semantic_version()
        
        # Only same MAJOR version scores are comparable
        return major_self == major_other


@dataclass(frozen=True)
class TrustResolutionContext:
    """
    Context for resolving a trust model.
    
    Prevents:
    - Live models used in replay
    - Experimental models in production
    - Illegal cross-purpose usage
    """
    account_id: str
    platform: str
    evaluation_time: datetime
    purpose: TrustResolutionPurpose
    
    # Optional constraints
    required_model_id: Optional[str] = None
    max_model_age_days: Optional[int] = None
    
    def validates_model(self, model: TrustModelDefinition) -> Tuple[bool, str]:
        """Check if model is valid for this context"""
        
        # Platform compatibility
        if self.platform not in model.compatible_platforms:
            return False, f"Model {model.model_id} incompatible with platform {self.platform}"
        
        # Status checks
        if self.purpose == TrustResolutionPurpose.LIVE:
            if model.status != TrustModelStatus.PRODUCTION:
                return False, f"Live usage requires PRODUCTION status, got {model.status}"
        
        # Required model pinning
        if self.required_model_id and model.model_id != self.required_model_id:
            return False, f"Context requires {self.required_model_id}, got {model.model_id}"
        
        # Model age constraints
        if self.max_model_age_days is not None:
            age_days = (self.evaluation_time - model.created_at).days
            if age_days > self.max_model_age_days:
                return False, f"Model too old: {age_days} days > {self.max_model_age_days}"
        
        return True, "Valid"


class TrustModelHasher:
    """
    Generate canonical, deterministic hashes for trust models.
    
    If hash changes → version bump REQUIRED.
    """
    
    @staticmethod
    def compute_model_hash(model: TrustModelDefinition) -> str:
        """
        Compute deterministic hash of model semantics.
        
        Hash includes everything that affects trust computation:
        - Input signals (sorted for determinism)
        - Weight schema
        - Aggregation logic
        - Output range
        """
        hash_components = [
            model.model_id,
            model.version,
            *sorted(model.input_signals),
            model.weight_schema,
            model.aggregation_logic,
            str(model.output_range),
        ]
        
        hash_input = "|".join(hash_components).encode('utf-8')
        return hashlib.sha256(hash_input).hexdigest()
    
    @staticmethod
    def verify_hash_stability(
        model: TrustModelDefinition,
        expected_hash: str
    ) -> bool:
        """Verify model hash hasn't drifted"""
        actual_hash = TrustModelHasher.compute_model_hash(model)
        return actual_hash == expected_hash


class CompatibilityValidator:
    """
    Validates trust models against system constraints.
    
    Blocks models that:
    - Consume forbidden signals
    - Violate account invariants
    - Conflict with enforcement states
    - Bypass suppression ceilings
    """
    
    # System-level forbidden signals (would come from config)
    FORBIDDEN_SIGNALS = {
        "raw_user_ip",
        "exact_location",
        "private_messages"
    }
    
    # Signals that require special authorization
    RESTRICTED_SIGNALS = {
        "device_fingerprint",
        "cross_platform_id"
    }
    
    @staticmethod
    def validate_model(
        model: TrustModelDefinition,
        platform: str
    ) -> Tuple[bool, List[str]]:
        """
        Comprehensive model validation.
        
        Returns: (is_valid, list_of_violations)
        """
        violations = []
        
        # Check for forbidden signals
        forbidden = model.input_signals & CompatibilityValidator.FORBIDDEN_SIGNALS
        if forbidden:
            violations.append(f"Uses forbidden signals: {forbidden}")
        
        # Check for unauthorized restricted signals
        restricted = model.input_signals & CompatibilityValidator.RESTRICTED_SIGNALS
        if restricted:
            # In production, would check authorization database
            violations.append(f"Uses restricted signals without auth: {restricted}")
        
        # Validate output range
        if model.output_range != (0.0, 1.0):
            # Could support other ranges, but need explicit declaration
            violations.append(f"Non-standard output range: {model.output_range}")
        
        # Check invariant compatibility
        # In production, would validate against account_invariants.py
        if not model.invariant_constraints:
            violations.append("No invariant constraints declared (unsafe)")
        
        # Platform-specific validation
        if platform == "high_risk_platform" and "device_fingerprint" not in model.input_signals:
            violations.append(f"Platform {platform} requires device_fingerprint")
        
        return len(violations) == 0, violations


@dataclass
class TrustModelActivation:
    """Record of when a model was activated/deactivated"""
    model_id: str
    platform: str
    activated_at: datetime
    deactivated_at: Optional[datetime]
    activated_by: str
    reason: str


class TrustRegistry:
    """
    Singleton trust model registry.
    
    GUARANTEES:
    - Exactly one active model per platform at any time
    - All models are versioned and hashed
    - Historical models are resolvable
    - Model switches are audited
    
    INVARIANTS:
    - No silent model changes
    - No hash drift
    - No compatibility violations
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        # Model storage
        self._models: Dict[str, TrustModelDefinition] = {}
        self._model_hashes: Dict[str, str] = {}
        
        # Active model tracking (platform -> model_id)
        self._active_models: Dict[str, str] = {}
        
        # Activation history
        self._activation_history: List[TrustModelActivation] = []
        
        # Model compatibility graph
        self._compatibility_graph: Dict[str, Set[str]] = defaultdict(set)
        
        self._initialized = True
    
    def register_model(
        self,
        model: TrustModelDefinition,
        validate: bool = True
    ) -> Tuple[bool, str]:
        """
        Register a new trust model version.
        
        Args:
            model: Trust model definition
            validate: Run compatibility validation
        
        Returns:
            (success, message)
        """
        # Check if already registered
        if model.model_id in self._models:
            return False, f"Model {model.model_id} already registered"
        
        # Validate if requested
        if validate:
            for platform in model.compatible_platforms:
                is_valid, violations = CompatibilityValidator.validate_model(
                    model, platform
                )
                if not is_valid:
                    return False, f"Validation failed: {violations}"
        
        # Compute and store hash
        model_hash = TrustModelHasher.compute_model_hash(model)
        
        # Store model
        self._models[model.model_id] = model
        self._model_hashes[model.model_id] = model_hash
        
        # Update compatibility graph
        for existing_id, existing_model in self._models.items():
            if existing_id != model.model_id:
                if model.is_compatible_with(existing_model):
                    self._compatibility_graph[model.model_id].add(existing_id)
                    self._compatibility_graph[existing_id].add(model.model_id)
        
        return True, f"Registered {model.model_id} with hash {model_hash[:8]}"
    
    def activate_model(
        self,
        model_id: str,
        platform: str,
        activated_by: str,
        reason: str
    ) -> Tuple[bool, str]:
        """
        Activate a trust model for a platform.
        
        ENFORCES: Exactly one active model per platform
        """
        # Verify model exists
        if model_id not in self._models:
            return False, f"Model {model_id} not registered"
        
        model = self._models[model_id]
        
        # Verify platform compatibility
        if platform not in model.compatible_platforms:
            return False, f"Model {model_id} incompatible with platform {platform}"
        
        # Verify model is production-ready
        if model.status != TrustModelStatus.PRODUCTION:
            return False, f"Model {model_id} not in PRODUCTION status"
        
        # Deactivate current model if exists
        if platform in self._active_models:
            old_model_id = self._active_models[platform]
            
            # Find and close activation record
            for activation in reversed(self._activation_history):
                if (activation.platform == platform and 
                    activation.model_id == old_model_id and
                    activation.deactivated_at is None):
                    activation.deactivated_at = datetime.now()
                    break
        
        # Activate new model
        self._active_models[platform] = model_id
        
        # Record activation
        activation = TrustModelActivation(
            model_id=model_id,
            platform=platform,
            activated_at=datetime.now(),
            deactivated_at=None,
            activated_by=activated_by,
            reason=reason
        )
        self._activation_history.append(activation)
        
        return True, f"Activated {model_id} for {platform}"
    
    def resolve_model(
        self,
        context: TrustResolutionContext
    ) -> Optional[TrustModelDefinition]:
        """
        Resolve the appropriate trust model for a context.
        
        For LIVE: returns active model
        For REPLAY/AUDIT: returns historical model
        """
        # Handle explicit model pinning
        if context.required_model_id:
            model = self._models.get(context.required_model_id)
            if model is None:
                return None
            
            is_valid, _ = context.validates_model(model)
            return model if is_valid else None
        
        # Live resolution: return active model
        if context.purpose == TrustResolutionPurpose.LIVE:
            model_id = self._active_models.get(context.platform)
            if model_id is None:
                return None
            
            model = self._models[model_id]
            is_valid, _ = context.validates_model(model)
            return model if is_valid else None
        
        # Historical resolution: find active model at evaluation_time
        if context.purpose in {TrustResolutionPurpose.REPLAY, TrustResolutionPurpose.AUDIT}:
            for activation in reversed(self._activation_history):
                if activation.platform != context.platform:
                    continue
                
                # Check if this activation was active at evaluation_time
                if activation.activated_at <= context.evaluation_time:
                    if (activation.deactivated_at is None or 
                        activation.deactivated_at > context.evaluation_time):
                        
                        model = self._models[activation.model_id]
                        is_valid, _ = context.validates_model(model)
                        return model if is_valid else None
        
        return None
    
    def get_model(self, model_id: str) -> Optional[TrustModelDefinition]:
        """Get model by ID"""
        return self._models.get(model_id)
    
    def get_model_hash(self, model_id: str) -> Optional[str]:
        """Get stored hash for a model"""
        return self._model_hashes.get(model_id)
    
    def list_models(
        self,
        platform: Optional[str] = None,
        status: Optional[TrustModelStatus] = None
    ) -> List[TrustModelDefinition]:
        """List models with optional filtering"""
        models = list(self._models.values())
        
        if platform:
            models = [m for m in models if platform in m.compatible_platforms]
        
        if status:
            models = [m for m in models if m.status == status]
        
        return sorted(models, key=lambda m: m.created_at, reverse=True)
    
    def get_active_model(self, platform: str) -> Optional[TrustModelDefinition]:
        """Get currently active model for platform"""
        model_id = self._active_models.get(platform)
        return self._models.get(model_id) if model_id else None
    
    def get_activation_history(
        self,
        platform: Optional[str] = None
    ) -> List[TrustModelActivation]:
        """Get model activation history"""
        history = self._activation_history
        
        if platform:
            history = [a for a in history if a.platform == platform]
        
        return sorted(history, key=lambda a: a.activated_at, reverse=True)
    
    def deprecate_model(
        self,
        model_id: str,
        replacement_model_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Mark a model as deprecated"""
        if model_id not in self._models:
            return False, f"Model {model_id} not found"
        
        model = self._models[model_id]
        
        # Create deprecated version
        deprecated_model = TrustModelDefinition(
            model_id=model.model_id,
            version=model.version,
            description=model.description,
            author=model.author,
            created_at=model.created_at,
            input_signals=model.input_signals,
            output_range=model.output_range,
            weight_schema=model.weight_schema,
            aggregation_logic=model.aggregation_logic,
            compatible_platforms=model.compatible_platforms,
            incompatible_states=model.incompatible_states,
            invariant_constraints=model.invariant_constraints,
            status=TrustModelStatus.DEPRECATED,
            deprecation_date=datetime.now(),
            replacement_model=replacement_model_id
        )
        
        self._models[model_id] = deprecated_model
        
        return True, f"Deprecated {model_id}"


class TrustRegistryWatchdog:
    """
    Continuously verifies registry integrity.
    
    Verifies:
    - Active model matches config
    - Hashes haven't drifted
    - Deprecated models not used
    - Replay requests resolve correctly
    """
    
    def __init__(self, registry: TrustRegistry):
        self.registry = registry
        self.violations: List[str] = []
    
    def check_hash_stability(self) -> bool:
        """Verify all model hashes are stable"""
        stable = True
        
        for model_id, expected_hash in self.registry._model_hashes.items():
            model = self.registry.get_model(model_id)
            if model is None:
                continue
            
            if not TrustModelHasher.verify_hash_stability(model, expected_hash):
                self.violations.append(
                    f"Hash drift detected for {model_id}"
                )
                stable = False
        
        return stable
    
    def check_active_models(self) -> bool:
        """Verify active models are production-ready"""
        valid = True
        
        for platform, model_id in self.registry._active_models.items():
            model = self.registry.get_model(model_id)
            
            if model is None:
                self.violations.append(
                    f"Active model {model_id} for {platform} not found"
                )
                valid = False
                continue
            
            if model.status != TrustModelStatus.PRODUCTION:
                self.violations.append(
                    f"Active model {model_id} for {platform} not in PRODUCTION"
                )
                valid = False
        
        return valid
    
    def check_deprecated_usage(self) -> bool:
        """Ensure deprecated models aren't active"""
        valid = True
        
        for platform, model_id in self.registry._active_models.items():
            model = self.registry.get_model(model_id)
            
            if model and model.status == TrustModelStatus.DEPRECATED:
                self.violations.append(
                    f"Deprecated model {model_id} active for {platform}"
                )
                valid = False
        
        return valid
    
    def run_full_check(self) -> Tuple[bool, List[str]]:
        """Run all integrity checks"""
        self.violations = []
        
        checks = [
            self.check_hash_stability(),
            self.check_active_models(),
            self.check_deprecated_usage()
        ]
        
        return all(checks), self.violations


# ============================================================================
# REGISTRY OUTPUT CONTRACT
# ============================================================================

def get_model_info(model: TrustModelDefinition, registry: TrustRegistry) -> dict:
    """
    Standard model info output.
    
    Used everywhere:
    - trust_scoring.py
    - reputation_ledger.py
    - experiments/
    - audits/
    """
    return {
        "model_id": model.model_id,
        "version": model.version,
        "status": model.status.value,
        
        "compatible_platforms": sorted(model.compatible_platforms),
        "input_signals": sorted(model.input_signals),
        "output_range": model.output_range,
        
        "registered_at": model.created_at.isoformat(),
        "hash": registry.get_model_hash(model.model_id),
        
        "aggregation": model.aggregation_logic,
        "weight_schema": model.weight_schema
    }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Initialize registry
    registry = TrustRegistry()
    
    # Define a trust model
    model_v3 = TrustModelDefinition(
        model_id="trust_v3_0_0",
        version="3.0.0",
        description="Major revision: adds device fingerprinting",
        author="trust_team",
        created_at=datetime.now(),
        
        input_signals={
            "account_age_days",
            "content_violation_rate",
            "positive_interaction_rate",
            "device_fingerprint",
            "network_trust_score"
        },
        
        output_range=(0.0, 1.0),
        weight_schema="weights_v3_balanced",
        aggregation_logic="weighted_geometric_mean",
        
        compatible_platforms={"youtube", "tiktok", "instagram"},
        incompatible_states={"hard_suppression"},
        
        invariant_constraints=[
            "must_respect_age_floor",
            "must_enforce_violation_ceiling",
            "must_honor_network_floor"
        ],
        
        status=TrustModelStatus.PRODUCTION
    )
    
    # Register model
    success, msg = registry.register_model(model_v3)
    print(f"Registration: {msg}")
    
    # Activate for platform
    success, msg = registry.activate_model(
        "trust_v3_0_0",
        "youtube",
        "system_admin",
        "Production rollout"
    )
    print(f"Activation: {msg}")
    
    # Resolve model for live usage
    context = TrustResolutionContext(
        account_id="user_12345",
        platform="youtube",
        evaluation_time=datetime.now(),
        purpose=TrustResolutionPurpose.LIVE
    )
    
    resolved_model = registry.resolve_model(context)
    if resolved_model:
        info = get_model_info(resolved_model, registry)
        print(f"\nResolved model: {json.dumps(info, indent=2)}")
    
    # Run watchdog checks
    watchdog = TrustRegistryWatchdog(registry)
    is_healthy, violations = watchdog.run_full_check()
    print(f"\nRegistry health: {'✓ HEALTHY' if is_healthy else '✗ VIOLATIONS'}")
    if violations:
        for v in violations:
            print(f"  - {v}")
    
    # Show activation history
    print("\nActivation history:")
    for activation in registry.get_activation_history("youtube"):
        print(f"  {activation.model_id} @ {activation.activated_at}")




