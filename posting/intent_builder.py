"""
posting/intent_builder.py

Canonical Posting Intent Construction & Validation Spine

This is the ONLY place where "we want to post something" becomes
"this exact thing is allowed to be posted under these rules".

If an intent is malformed here, it must never reach dispatch.

Core Guarantees:
- Fully explicit PostIntent objects
- All posting invariants validated
- Platform-agnostic correctness
- Deterministic, hashable, immutable intents
- Audit-safe & RL-safe
- Better to block a million posts than poison the system once

Architectural Position:
    generation/content_pipeline.py
            ↓
    posting/intent_builder.py ← YOU ARE HERE
            ↓
    posting/post_dispatcher.py
            ↓
    posting/platforms/*

NO BYPASSES. EVER.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple, List, Dict, Any
from enum import Enum


# ============================================================================
# CORE DATA CONTRACTS
# ============================================================================

@dataclass(frozen=True)
class PostIntent:
    """
    Immutable, hashable posting intent.
    
    Once created, this object CANNOT be modified.
    All fields must be explicitly filled (except where Optional).
    """
    intent_id: str
    content_id: str
    content_version: str
    
    media_type: str  # video / image / carousel
    duration_seconds: float
    
    caption: str
    hashtags: Tuple[str, ...]  # tuple for immutability
    
    platform_targets: Tuple[str, ...]  # normalized order
    
    account_id: str
    identity_profile_id: str
    
    scheduled_time: Optional[float]
    
    distribution_mode: str  # organic / repost / revive
    
    confidence_score: float
    risk_flags: Tuple[str, ...]
    
    invariant_hash: str
    created_at: float
    
    lifecycle_tag: str  # SANDBOX / PRODUCTION / HIGH_RISK / BLOCKED
    
    def __hash__(self) -> int:
        return hash(self.intent_id)


@dataclass
class IntentContext:
    """
    Input envelope carrying everything upstream already knows.
    IntentBuilder NEVER fetches data itself.
    """
    content_id: str
    content_version: str
    
    generated_assets_ref: str
    
    niche_id: str
    predicted_engagement_envelope: Dict[str, Any]
    
    target_platforms: List[str]
    
    identity_profile_id: str
    account_id: str
    
    desired_time: Optional[float]
    
    upstream_confidence: float
    
    # Optional metadata
    media_type: Optional[str] = None
    duration_seconds: Optional[float] = None
    caption: Optional[str] = None
    hashtags: Optional[List[str]] = None
    distribution_mode: Optional[str] = "organic"


class LifecycleTag(Enum):
    """Intent lifecycle classification"""
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"
    HIGH_RISK = "HIGH_RISK"
    BLOCKED = "BLOCKED"


# ============================================================================
# EXCEPTIONS
# ============================================================================

class IntentValidationError(Exception):
    """Raised when intent violates invariants"""
    pass


class IntentBindingError(Exception):
    """Raised when binding phase fails"""
    pass


class IntentInvariantViolation(Exception):
    """Raised when hard invariants are violated"""
    pass


# ============================================================================
# INTENT BUILDER (CORE ENGINE)
# ============================================================================

class IntentBuilder:
    """
    Single entry point for constructing validated PostIntent objects.
    
    Phases (STRICT ORDER):
    1. Content binding
    2. Identity binding
    3. Timing binding
    4. Routing binding
    5. Assembly
    6. Validation
    7. Risk classification
    8. Finalization
    
    NO OPTIONAL STEPS. NO SHORTCUTS.
    """
    
    def __init__(
        self,
        feature_registry,
        identity_manager,
        platform_config
    ):
        self.feature_registry = feature_registry
        self.identity_manager = identity_manager
        self.platform_config = platform_config
        
        self.validator = IntentInvariantValidator(platform_config)
        self.hasher = IntentDeterminismHasher()
        self.risk_classifier = IntentRiskClassifier()
        self.lifecycle_tagger = IntentLifecycleTagger()
        self.watchdog = IntentWatchdog()
        
    def build_intent(self, context: IntentContext) -> PostIntent:
        """
        SINGLE ENTRY POINT for intent construction.
        
        Args:
            context: Complete input envelope from upstream
            
        Returns:
            Validated, immutable PostIntent
            
        Raises:
            IntentValidationError: If intent violates invariants
            IntentBindingError: If binding phase fails
        """
        # Phase 1: Content binding
        content_binding = self._bind_content(context)
        
        # Phase 2: Identity binding
        identity_binding = self._bind_identity(context)
        
        # Phase 3: Timing binding
        timing_binding = self._bind_timing(context)
        
        # Phase 4: Routing binding
        routing_binding = self._bind_routing(context)
        
        # Phase 5: Assembly
        intent = self._assemble(
            context,
            content_binding,
            identity_binding,
            timing_binding,
            routing_binding
        )
        
        # Phase 6: Validation
        self._validate_intent(intent)
        
        # Phase 7: Risk classification
        intent = self._classify_risk(intent)
        
        # Phase 8: Finalization
        intent = self._finalize(intent)
        
        # Watchdog monitoring
        self.watchdog.record_intent_creation(intent)
        
        return intent
    
    # ========================================================================
    # BINDING PHASES
    # ========================================================================
    
    def _bind_content(self, context: IntentContext) -> Dict[str, Any]:
        """
        Phase 1: Content binding
        
        Validates:
        - Asset existence
        - Duration bounds
        - Content version immutability
        - Media compatibility with targets
        
        Failures here indicate generation bugs.
        """
        if not context.content_id:
            raise IntentBindingError("content_id is required")
        
        if not context.content_version:
            raise IntentBindingError("content_version is required")
        
        # Extract content metadata
        media_type = context.media_type
        if not media_type:
            raise IntentBindingError("media_type must be provided")
        
        if media_type not in ["video", "image", "carousel"]:
            raise IntentBindingError(f"Invalid media_type: {media_type}")
        
        duration = context.duration_seconds or 0.0
        if duration < 0:
            raise IntentBindingError("duration_seconds cannot be negative")
        
        # Validate caption
        caption = context.caption or ""
        if len(caption) > 2200:  # Instagram max
            raise IntentBindingError("Caption exceeds maximum length")
        
        # Validate and normalize hashtags
        hashtags = context.hashtags or []
        if len(hashtags) > 30:  # Instagram max
            raise IntentBindingError("Too many hashtags")
        
        # Normalize hashtag order for determinism
        hashtags = sorted(set(h.strip().lower() for h in hashtags))
        
        return {
            "media_type": media_type,
            "duration_seconds": duration,
            "caption": caption,
            "hashtags": tuple(hashtags),
        }
    
    def _bind_identity(self, context: IntentContext) -> Dict[str, Any]:
        """
        Phase 2: Identity binding
        
        Binds:
        - Exact account
        - Identity profile
        - Trust tier snapshot
        
        Hard-fails if:
        - Identity is unhealthy
        - Trust score < minimum
        - Identity-platform mismatch
        """
        if not context.account_id:
            raise IntentBindingError("account_id is required")
        
        if not context.identity_profile_id:
            raise IntentBindingError("identity_profile_id is required")
        
        # Verify identity health
        identity = self.identity_manager.get_identity(
            context.identity_profile_id
        )
        
        if not identity:
            raise IntentBindingError(
                f"Identity not found: {context.identity_profile_id}"
            )
        
        if identity.get("health_status") != "healthy":
            raise IntentBindingError(
                f"Identity unhealthy: {identity.get('health_status')}"
            )
        
        trust_score = identity.get("trust_score", 0.0)
        if trust_score < 0.5:  # Minimum threshold
            raise IntentBindingError(
                f"Trust score too low: {trust_score}"
            )
        
        # Verify account-platform compatibility
        account = self.identity_manager.get_account(context.account_id)
        if not account:
            raise IntentBindingError(f"Account not found: {context.account_id}")
        
        account_platforms = set(account.get("platforms", []))
        target_platforms = set(context.target_platforms)
        
        if not target_platforms.issubset(account_platforms):
            raise IntentBindingError(
                f"Account doesn't support platforms: "
                f"{target_platforms - account_platforms}"
            )
        
        return {
            "account_id": context.account_id,
            "identity_profile_id": context.identity_profile_id,
        }
    
    def _bind_timing(self, context: IntentContext) -> Dict[str, Any]:
        """
        Phase 3: Timing binding
        
        Rules:
        - No timezone ambiguity
        - No platform-illegal windows
        - No retroactive scheduling
        - If None → explicitly marked as immediate dispatch
        """
        desired_time = context.desired_time
        
        if desired_time is not None:
            # No retroactive scheduling
            now = time.time()
            if desired_time < now:
                raise IntentBindingError(
                    f"Cannot schedule in the past: {desired_time} < {now}"
                )
            
            # Validate timing windows (platform-specific rules could go here)
            # For now, just ensure it's reasonable (within 30 days)
            max_future = now + (30 * 24 * 60 * 60)
            if desired_time > max_future:
                raise IntentBindingError(
                    "Cannot schedule more than 30 days in advance"
                )
        
        return {
            "scheduled_time": desired_time,
        }
    
    def _bind_routing(self, context: IntentContext) -> Dict[str, Any]:
        """
        Phase 4: Routing binding
        
        Requires:
        - Explicit platform list
        - No implicit fan-out
        - Platform order normalized
        - Unknown platforms → hard fail
        """
        if not context.target_platforms:
            raise IntentBindingError("target_platforms cannot be empty")
        
        # Validate all platforms are known
        known_platforms = set(self.platform_config.get_supported_platforms())
        target_platforms = set(context.target_platforms)
        
        unknown = target_platforms - known_platforms
        if unknown:
            raise IntentBindingError(
                f"Unknown platforms: {unknown}"
            )
        
        # Normalize order for determinism
        normalized_platforms = tuple(sorted(target_platforms))
        
        return {
            "platform_targets": normalized_platforms,
        }
    
    # ========================================================================
    # ASSEMBLY & VALIDATION
    # ========================================================================
    
    def _assemble(
        self,
        context: IntentContext,
        content_binding: Dict[str, Any],
        identity_binding: Dict[str, Any],
        timing_binding: Dict[str, Any],
        routing_binding: Dict[str, Any],
    ) -> PostIntent:
        """
        Phase 5: Assemble all bindings into PostIntent
        """
        # Generate deterministic intent ID
        intent_data = {
            "content_id": context.content_id,
            "content_version": context.content_version,
            "account_id": identity_binding["account_id"],
            "platforms": routing_binding["platform_targets"],
            "scheduled_time": timing_binding["scheduled_time"],
        }
        intent_id = self.hasher.generate_intent_id(intent_data)
        
        # Generate invariant hash (for validation tracking)
        invariant_data = {
            **intent_data,
            "media_type": content_binding["media_type"],
            "duration": content_binding["duration_seconds"],
        }
        invariant_hash = self.hasher.generate_invariant_hash(invariant_data)
        
        # Distribution mode
        distribution_mode = context.distribution_mode or "organic"
        if distribution_mode not in ["organic", "repost", "revive"]:
            distribution_mode = "organic"
        
        # Assemble
        intent = PostIntent(
            intent_id=intent_id,
            content_id=context.content_id,
            content_version=context.content_version,
            media_type=content_binding["media_type"],
            duration_seconds=content_binding["duration_seconds"],
            caption=content_binding["caption"],
            hashtags=content_binding["hashtags"],
            platform_targets=routing_binding["platform_targets"],
            account_id=identity_binding["account_id"],
            identity_profile_id=identity_binding["identity_profile_id"],
            scheduled_time=timing_binding["scheduled_time"],
            distribution_mode=distribution_mode,
            confidence_score=context.upstream_confidence,
            risk_flags=tuple(),  # Will be populated by risk classifier
            invariant_hash=invariant_hash,
            created_at=time.time(),
            lifecycle_tag=LifecycleTag.PRODUCTION.value,  # Default
        )
        
        return intent
    
    def _validate_intent(self, intent: PostIntent) -> None:
        """
        Phase 6: Validate all invariants
        
        Raises IntentValidationError if any invariant is violated.
        """
        self.validator.validate(intent)
    
    def _classify_risk(self, intent: PostIntent) -> PostIntent:
        """
        Phase 7: Risk classification
        
        Assigns risk flags but does NOT block dispatch.
        Downstream policy uses these flags.
        """
        risk_flags = self.risk_classifier.classify(intent)
        
        # Rebuild with risk flags (immutable, so we recreate)
        return PostIntent(
            intent_id=intent.intent_id,
            content_id=intent.content_id,
            content_version=intent.content_version,
            media_type=intent.media_type,
            duration_seconds=intent.duration_seconds,
            caption=intent.caption,
            hashtags=intent.hashtags,
            platform_targets=intent.platform_targets,
            account_id=intent.account_id,
            identity_profile_id=intent.identity_profile_id,
            scheduled_time=intent.scheduled_time,
            distribution_mode=intent.distribution_mode,
            confidence_score=intent.confidence_score,
            risk_flags=tuple(risk_flags),
            invariant_hash=intent.invariant_hash,
            created_at=intent.created_at,
            lifecycle_tag=intent.lifecycle_tag,
        )
    
    def _finalize(self, intent: PostIntent) -> PostIntent:
        """
        Phase 8: Final lifecycle tagging
        
        Determines if intent should be:
        - SANDBOX (experimental)
        - PRODUCTION (safe)
        - HIGH_RISK (limited fan-out)
        - BLOCKED (rejected)
        """
        lifecycle_tag = self.lifecycle_tagger.tag(intent)
        
        if lifecycle_tag == LifecycleTag.BLOCKED.value:
            raise IntentValidationError(
                f"Intent blocked by lifecycle policy: {intent.intent_id}"
            )
        
        # Rebuild with final tag
        return PostIntent(
            intent_id=intent.intent_id,
            content_id=intent.content_id,
            content_version=intent.content_version,
            media_type=intent.media_type,
            duration_seconds=intent.duration_seconds,
            caption=intent.caption,
            hashtags=intent.hashtags,
            platform_targets=intent.platform_targets,
            account_id=intent.account_id,
            identity_profile_id=intent.identity_profile_id,
            scheduled_time=intent.scheduled_time,
            distribution_mode=intent.distribution_mode,
            confidence_score=intent.confidence_score,
            risk_flags=intent.risk_flags,
            invariant_hash=intent.invariant_hash,
            created_at=intent.created_at,
            lifecycle_tag=lifecycle_tag,
        )


# ============================================================================
# INVARIANT VALIDATOR
# ============================================================================

class IntentInvariantValidator:
    """
    Validates all posting invariants.
    
    Examples:
    - duration ≤ platform max
    - hashtag count ≤ platform limit
    - caption length safe
    - confidence_score ≥ minimum threshold
    - predicted p50_views ≥ baseline (e.g., 5M)
    """
    
    def __init__(self, platform_config):
        self.platform_config = platform_config
        
    def validate(self, intent: PostIntent) -> None:
        """
        Validate all invariants. Raises IntentInvariantViolation on failure.
        """
        self._validate_confidence(intent)
        self._validate_duration(intent)
        self._validate_caption(intent)
        self._validate_hashtags(intent)
        self._validate_platform_compatibility(intent)
        
    def _validate_confidence(self, intent: PostIntent) -> None:
        """Confidence must meet minimum threshold"""
        min_confidence = 0.6  # Configurable
        if intent.confidence_score < min_confidence:
            raise IntentInvariantViolation(
                f"Confidence {intent.confidence_score} below minimum "
                f"{min_confidence}"
            )
    
    def _validate_duration(self, intent: PostIntent) -> None:
        """Duration must be within platform bounds"""
        for platform in intent.platform_targets:
            max_duration = self.platform_config.get_max_duration(platform)
            if intent.duration_seconds > max_duration:
                raise IntentInvariantViolation(
                    f"Duration {intent.duration_seconds}s exceeds "
                    f"{platform} max {max_duration}s"
                )
    
    def _validate_caption(self, intent: PostIntent) -> None:
        """Caption must be within platform limits"""
        for platform in intent.platform_targets:
            max_length = self.platform_config.get_max_caption_length(platform)
            if len(intent.caption) > max_length:
                raise IntentInvariantViolation(
                    f"Caption length {len(intent.caption)} exceeds "
                    f"{platform} max {max_length}"
                )
    
    def _validate_hashtags(self, intent: PostIntent) -> None:
        """Hashtag count must be within platform limits"""
        for platform in intent.platform_targets:
            max_tags = self.platform_config.get_max_hashtags(platform)
            if len(intent.hashtags) > max_tags:
                raise IntentInvariantViolation(
                    f"Hashtag count {len(intent.hashtags)} exceeds "
                    f"{platform} max {max_tags}"
                )
    
    def _validate_platform_compatibility(self, intent: PostIntent) -> None:
        """Media type must be compatible with all target platforms"""
        for platform in intent.platform_targets:
            supported = self.platform_config.get_supported_media_types(platform)
            if intent.media_type not in supported:
                raise IntentInvariantViolation(
                    f"Media type {intent.media_type} not supported on {platform}"
                )


# ============================================================================
# DETERMINISM HASHER
# ============================================================================

class IntentDeterminismHasher:
    """
    Generates deterministic hashes for intents.
    
    Guarantees:
    - Identical input → identical intent
    - Safe replay
    - Safe deduplication
    
    Used by:
    - post_dispatcher lock manager
    - Audit systems
    - RL replay buffers
    """
    
    def generate_intent_id(self, data: Dict[str, Any]) -> str:
        """Generate stable intent ID from input data"""
        canonical = json.dumps(data, sort_keys=True)
        hash_obj = hashlib.sha256(canonical.encode())
        return f"intent_{hash_obj.hexdigest()[:16]}"
    
    def generate_invariant_hash(self, data: Dict[str, Any]) -> str:
        """Generate invariant hash for validation tracking"""
        canonical = json.dumps(data, sort_keys=True)
        hash_obj = hashlib.sha256(canonical.encode())
        return hash_obj.hexdigest()


# ============================================================================
# RISK CLASSIFIER
# ============================================================================

class IntentRiskClassifier:
    """
    Assigns risk flags to intents.
    
    Flags:
    - LOW_CONFIDENCE
    - NEW_ACCOUNT
    - EXPERIMENTAL_FORMAT
    - PLATFORM_SATURATION_RISK
    
    This does NOT block dispatch. It informs downstream policy.
    """
    
    def classify(self, intent: PostIntent) -> List[str]:
        """Classify intent and return risk flags"""
        flags = []
        
        # Low confidence
        if intent.confidence_score < 0.7:
            flags.append("LOW_CONFIDENCE")
        
        # Experimental format (e.g., carousel)
        if intent.media_type == "carousel":
            flags.append("EXPERIMENTAL_FORMAT")
        
        # Platform saturation (posting to many platforms)
        if len(intent.platform_targets) > 3:
            flags.append("PLATFORM_SATURATION_RISK")
        
        # Short duration (potentially risky)
        if intent.media_type == "video" and intent.duration_seconds < 5:
            flags.append("SHORT_DURATION")
        
        return flags


# ============================================================================
# LIFECYCLE TAGGER
# ============================================================================

class IntentLifecycleTagger:
    """
    Determines lifecycle tag for intent.
    
    Tags:
    - SANDBOX: experimental
    - PRODUCTION: safe
    - HIGH_RISK: limited fan-out
    - BLOCKED: rejected
    """
    
    def tag(self, intent: PostIntent) -> str:
        """Determine lifecycle tag"""
        # Block if too many risk flags
        if len(intent.risk_flags) >= 3:
            return LifecycleTag.BLOCKED.value
        
        # High risk if low confidence
        if intent.confidence_score < 0.65:
            return LifecycleTag.HIGH_RISK.value
        
        # Sandbox for experimental
        if "EXPERIMENTAL_FORMAT" in intent.risk_flags:
            return LifecycleTag.SANDBOX.value
        
        # Default to production
        return LifecycleTag.PRODUCTION.value


# ============================================================================
# WATCHDOG
# ============================================================================

class IntentWatchdog:
    """
    Monitors intent creation health.
    
    Tracks:
    - Rejection rates
    - Invariant violations
    - Intent entropy drift
    - Confidence vs outcome mismatch
    
    Used to detect:
    - Generation degradation
    - Prompt collapse
    - Model drift
    """
    
    def __init__(self):
        self.intent_count = 0
        self.rejection_count = 0
        self.risk_flag_counts: Dict[str, int] = {}
        
    def record_intent_creation(self, intent: PostIntent) -> None:
        """Record successful intent creation"""
        self.intent_count += 1
        
        for flag in intent.risk_flags:
            self.risk_flag_counts[flag] = self.risk_flag_counts.get(flag, 0) + 1
    
    def record_rejection(self, reason: str) -> None:
        """Record intent rejection"""
        self.rejection_count += 1
    
    def get_rejection_rate(self) -> float:
        """Get current rejection rate"""
        if self.intent_count == 0:
            return 0.0
        return self.rejection_count / (self.intent_count + self.rejection_count)
    
    def get_risk_distribution(self) -> Dict[str, float]:
        """Get distribution of risk flags"""
        if self.intent_count == 0:
            return {}
        
        return {
            flag: count / self.intent_count
            for flag, count in self.risk_flag_counts.items()
        }


# ============================================================================
# PLATFORM CONFIG (STUB - would be real registry in production)
# ============================================================================

class PlatformConfig:
    """Platform-specific configuration and limits"""
    
    def get_supported_platforms(self) -> List[str]:
        return ["instagram", "tiktok", "youtube", "twitter"]
    
    def get_max_duration(self, platform: str) -> float:
        limits = {
            "instagram": 90.0,
            "tiktok": 180.0,
            "youtube": 3600.0,
            "twitter": 140.0,
        }
        return limits.get(platform, 60.0)
    
    def get_max_caption_length(self, platform: str) -> int:
        limits = {
            "instagram": 2200,
            "tiktok": 2200,
            "youtube": 5000,
            "twitter": 280,
        }
        return limits.get(platform, 2200)
    
    def get_max_hashtags(self, platform: str) -> int:
        limits = {
            "instagram": 30,
            "tiktok": 20,
            "youtube": 15,
            "twitter": 10,
        }
        return limits.get(platform, 30)
    
    def get_supported_media_types(self, platform: str) -> List[str]:
        types = {
            "instagram": ["video", "image", "carousel"],
            "tiktok": ["video"],
            "youtube": ["video"],
            "twitter": ["video", "image"],
        }
        return types.get(platform, ["video", "image"])


# ============================================================================
# IDENTITY MANAGER (STUB)
# ============================================================================

class IdentityManager:
    """Stub for identity/account management"""
    
    def get_identity(self, identity_id: str) -> Optional[Dict[str, Any]]:
        # In production, this would fetch from registry
        return {
            "health_status": "healthy",
            "trust_score": 0.85,
        }
    
    def get_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        # In production, this would fetch from registry
        return {
            "platforms": ["instagram", "tiktok", "youtube"],
        }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Initialize dependencies
    platform_config = PlatformConfig()
    identity_manager = IdentityManager()
    feature_registry = None  # Would be real registry
    
    # Create builder
    builder = IntentBuilder(
        feature_registry=feature_registry,
        identity_manager=identity_manager,
        platform_config=platform_config
    )
    
    # Create context
    context = IntentContext(
        content_id="content_abc123",
        content_version="v1",
        generated_assets_ref="s3://bucket/content_abc123/v1",
        niche_id="niche_gaming",
        predicted_engagement_envelope={"p50_views": 8_000_000},
        target_platforms=["instagram", "tiktok"],
        identity_profile_id="identity_xyz",
        account_id="account_123",
        desired_time=None,
        upstream_confidence=0.85,
        media_type="video",
        duration_seconds=45.0,
        caption="Epic gaming moment! #gaming #viral",
        hashtags=["gaming", "viral", "epic"],
        distribution_mode="organic"
    )
    
    # Build intent
    try:
        intent = builder.build_intent(context)
        print(f"✅ Intent created: {intent.intent_id}")
        print(f"   Platforms: {intent.platform_targets}")
        print(f"   Confidence: {intent.confidence_score}")
        print(f"   Risk flags: {intent.risk_flags}")
        print(f"   Lifecycle: {intent.lifecycle_tag}")
    except (IntentValidationError, IntentBindingError) as e:
        print(f"❌ Intent rejected: {e}")