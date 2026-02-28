

"""
account_profile.py

Canonical Account Identity Snapshot

This file defines the single authoritative snapshot of an account's identity,
state, and legitimacy as of a moment in time.

Core Principle:
> There must exist exactly ONE canonical account snapshot at any moment.

This is NOT:
- A posting config
- A credentials vault
- A growth profile
- A platform rules model
- Mutable session state

This IS:
- The signed passport photo of the account at time T
- Immutable once constructed
- Fully deterministic & versioned
- Replay & audit capable

Every downstream system (trust scoring, suppression, experiments) depends on this.
"""

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any, Tuple, List, Dict
from copy import deepcopy


# ============================================================================
# LIFECYCLE STATE
# ============================================================================

class LifecycleStage(Enum):
    """
    Account lifecycle stages.
    
    Used by:
    - Trust scoring
    - Posting cadence limits
    - Experiment eligibility
    """
    NEW = "new"                    # 0-7 days
    WARMING = "warming"            # 7-30 days
    ACTIVE = "active"              # 30+ days, healthy
    SENSITIVE = "sensitive"        # Under scrutiny
    RESTRICTED = "restricted"      # Active limitations
    RECOVERY = "recovery"          # Post-enforcement recovery


# ============================================================================
# VERIFICATION STATE
# ============================================================================

@dataclass(frozen=True)
class VerificationState:
    """
    Legitimacy anchors (not immunity).
    
    NO CREDENTIALS STORED HERE. EVER.
    """
    email_verified: bool
    phone_verified: bool
    
    verification_age_days: int              # Days since first verification
    verification_consistency: float         # Stability score [0.0-1.0]
    
    last_verification_change: Optional[datetime]
    
    def __post_init__(self):
        if not 0.0 <= self.verification_consistency <= 1.0:
            raise ValueError(f"verification_consistency must be [0.0-1.0], got {self.verification_consistency}")


# ============================================================================
# ENFORCEMENT STATE
# ============================================================================

@dataclass(frozen=True)
class EnforcementState:
    """
    Historical and active penalties only.
    
    No speculation. Only observed outcomes.
    """
    active_restrictions: tuple[str, ...]    # Current active restrictions
    historical_actions: tuple[str, ...]     # Past enforcement actions
    
    last_enforcement_timestamp: Optional[datetime]
    severity_score: float                   # Cumulative severity [0.0-1.0]
    
    def __post_init__(self):
        if not 0.0 <= self.severity_score <= 1.0:
            raise ValueError(f"severity_score must be [0.0-1.0], got {self.severity_score}")


# ============================================================================
# PROFILE INTEGRITY
# ============================================================================

@dataclass(frozen=True)
class ProfileIntegrity:
    """
    Ensures snapshot reliability.
    
    Low integrity → downstream systems widen uncertainty.
    """
    completeness_score: float               # [0.0-1.0]
    consistency_score: float                # [0.0-1.0]
    
    missing_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    
    def __post_init__(self):
        if not 0.0 <= self.completeness_score <= 1.0:
            raise ValueError(f"completeness_score must be [0.0-1.0], got {self.completeness_score}")
        if not 0.0 <= self.consistency_score <= 1.0:
            raise ValueError(f"consistency_score must be [0.0-1.0], got {self.consistency_score}")
    
    @property
    def is_reliable(self) -> bool:
        """Profile is reliable if both scores > 0.7"""
        return self.completeness_score > 0.7 and self.consistency_score > 0.7


# ============================================================================
# ACCOUNT PROFILE (CANONICAL SNAPSHOT)
# ============================================================================

@dataclass(frozen=True)
class AccountProfile:
    """
    The canonical identity snapshot.
    
    Frozen = immutable.
    Mutations require new snapshot.
    
    This is the single source of truth at time T.
    """
    # Identity
    account_id: str
    entity_id: str                          # Parent business/entity
    platform: str                           # twitter, linkedin, etc.
    
    # Temporal
    created_at: datetime
    snapshot_timestamp: datetime
    
    # Lifecycle
    account_age_days: int
    lifecycle_stage: LifecycleStage
    
    # State Components
    verification_state: VerificationState
    enforcement_state: EnforcementState
    profile_integrity: ProfileIntegrity
    
    # Platform-Specific Metadata
    platform_metadata: dict[str, Any] = field(default_factory=dict)
    
    # Validation & Versioning
    invariants_passed: bool = True
    profile_version: str = "1.0.0"
    
    # Computed Hash (set by builder)
    profile_hash: str = ""
    
    def __post_init__(self):
        """Validate invariants on construction"""
        errors = []
        
        # No time travel
        if self.snapshot_timestamp < self.created_at:
            errors.append("snapshot_timestamp cannot be before created_at")
        
        # Account age must be non-negative
        if self.account_age_days < 0:
            errors.append(f"account_age_days cannot be negative: {self.account_age_days}")
        
        # Platform must be non-empty
        if not self.platform or not self.platform.strip():
            errors.append("platform cannot be empty")
        
        # Identity fields required
        if not self.account_id or not self.entity_id:
            errors.append("account_id and entity_id are required")
        
        if errors:
            raise ValueError(f"AccountProfile invariant violations: {errors}")
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (for hashing, serialization)"""
        data = asdict(self)
        
        # Convert enums
        data['lifecycle_stage'] = self.lifecycle_stage.value
        
        # Convert datetimes to ISO format
        data['created_at'] = self.created_at.isoformat()
        data['snapshot_timestamp'] = self.snapshot_timestamp.isoformat()
        
        if self.verification_state.last_verification_change:
            data['verification_state']['last_verification_change'] = \
                self.verification_state.last_verification_change.isoformat()
        
        if self.enforcement_state.last_enforcement_timestamp:
            data['enforcement_state']['last_enforcement_timestamp'] = \
                self.enforcement_state.last_enforcement_timestamp.isoformat()
        
        return data
    
    def canonical_json(self) -> str:
        """Deterministic JSON representation for hashing"""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))


# ============================================================================
# PROFILE HASHER
# ============================================================================

class ProfileHasher:
    """
    Generates deterministic hashes for account profiles.
    
    Used for:
    - Experiment isolation
    - Trust regression analysis
    - Drift detection
    """
    
    @staticmethod
    def hash_profile(profile: AccountProfile) -> str:
        """
        Generate SHA256 hash of canonical profile.
        
        Given same inputs + builder version → byte-identical hash.
        """
        canonical = profile.canonical_json()
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    
    @staticmethod
    def verify_hash(profile: AccountProfile) -> bool:
        """Verify profile hash matches current state"""
        expected = ProfileHasher.hash_profile(profile)
        return profile.profile_hash == expected


# ============================================================================
# PROFILE VALIDATOR
# ============================================================================

class AccountProfileValidator:
    """
    Validates account profiles against invariants.
    
    Fail-fast approach: better to reject than accept corrupt data.
    """
    
    KNOWN_PLATFORMS = {'twitter', 'linkedin', 'facebook', 'instagram', 'threads'}
    
    @classmethod
    def validate(cls, profile: AccountProfile) -> tuple[bool, list[str]]:
        """
        Validate profile against all invariants.
        
        Returns:
            (is_valid, error_messages)
        """
        errors = []
        
        # Platform validation
        if profile.platform.lower() not in cls.KNOWN_PLATFORMS:
            errors.append(f"Unknown platform: {profile.platform}")
        
        # Temporal consistency
        if profile.snapshot_timestamp.tzinfo is None:
            errors.append("snapshot_timestamp must be timezone-aware")
        
        if profile.created_at.tzinfo is None:
            errors.append("created_at must be timezone-aware")
        
        # Lifecycle consistency
        age_vs_stage = cls._validate_lifecycle_consistency(profile)
        if age_vs_stage:
            errors.append(age_vs_stage)
        
        # State consistency
        state_errors = cls._validate_state_consistency(profile)
        errors.extend(state_errors)
        
        # Hash verification
        if profile.profile_hash and not ProfileHasher.verify_hash(profile):
            errors.append("profile_hash does not match current state")
        
        return len(errors) == 0, errors
    
    @staticmethod
    def _validate_lifecycle_consistency(profile: AccountProfile) -> Optional[str]:
        """Validate lifecycle stage matches account age"""
        age = profile.account_age_days
        stage = profile.lifecycle_stage
        
        # NEW accounts should be < 7 days
        if stage == LifecycleStage.NEW and age >= 7:
            return f"NEW stage but account is {age} days old"
        
        # WARMING should be 7-30 days (unless overridden by enforcement)
        if stage == LifecycleStage.WARMING and age < 7:
            return f"WARMING stage but account is only {age} days old"
        
        return None
    
    @staticmethod
    def _validate_state_consistency(profile: AccountProfile) -> list[str]:
        """Validate internal state consistency"""
        errors = []
        
        # If RESTRICTED, should have active restrictions
        if profile.lifecycle_stage == LifecycleStage.RESTRICTED:
            if not profile.enforcement_state.active_restrictions:
                errors.append("RESTRICTED stage but no active_restrictions")
        
        # If enforcement exists, severity should be > 0
        if profile.enforcement_state.historical_actions:
            if profile.enforcement_state.severity_score == 0.0:
                errors.append("Has enforcement history but severity_score is 0.0")
        
        # Verification age cannot exceed account age
        if profile.verification_state.verification_age_days > profile.account_age_days:
            errors.append(
                f"verification_age_days ({profile.verification_state.verification_age_days}) "
                f"exceeds account_age_days ({profile.account_age_days})"
            )
        
        return errors


# ============================================================================
# ACCOUNT PROFILE BUILDER
# ============================================================================

class AccountProfileBuilder:
    """
    The ONLY legal way to create an AccountProfile.
    
    Responsibilities:
    - Merge upstream sources
    - Resolve conflicts deterministically
    - Validate invariants
    - Freeze snapshot
    
    No ad-hoc construction allowed.
    """
    
    def __init__(self):
        self._account_id: Optional[str] = None
        self._entity_id: Optional[str] = None
        self._platform: Optional[str] = None
        self._created_at: Optional[datetime] = None
        self._snapshot_timestamp: Optional[datetime] = None
        
        self._verification_state: Optional[VerificationState] = None
        self._enforcement_state: Optional[EnforcementState] = None
        self._profile_integrity: Optional[ProfileIntegrity] = None
        
        self._platform_metadata: dict[str, Any] = {}
        self._profile_version: str = "1.0.0"
    
    def set_identity(
        self,
        account_id: str,
        entity_id: str,
        platform: str
    ) -> 'AccountProfileBuilder':
        """Set core identity fields"""
        self._account_id = account_id
        self._entity_id = entity_id
        self._platform = platform.lower()
        return self
    
    def set_temporal(
        self,
        created_at: datetime,
        snapshot_timestamp: Optional[datetime] = None
    ) -> 'AccountProfileBuilder':
        """Set temporal fields"""
        self._created_at = created_at
        self._snapshot_timestamp = snapshot_timestamp or datetime.now(timezone.utc)
        return self
    
    def set_verification_state(
        self,
        email_verified: bool,
        phone_verified: bool,
        verification_age_days: int,
        verification_consistency: float,
        last_verification_change: Optional[datetime] = None
    ) -> 'AccountProfileBuilder':
        """Set verification state"""
        self._verification_state = VerificationState(
            email_verified=email_verified,
            phone_verified=phone_verified,
            verification_age_days=verification_age_days,
            verification_consistency=verification_consistency,
            last_verification_change=last_verification_change
        )
        return self
    
    def set_enforcement_state(
        self,
        active_restrictions: list[str],
        historical_actions: list[str],
        severity_score: float,
        last_enforcement_timestamp: Optional[datetime] = None
    ) -> 'AccountProfileBuilder':
        """Set enforcement state"""
        self._enforcement_state = EnforcementState(
            active_restrictions=tuple(active_restrictions),
            historical_actions=tuple(historical_actions),
            last_enforcement_timestamp=last_enforcement_timestamp,
            severity_score=severity_score
        )
        return self
    
    def set_profile_integrity(
        self,
        completeness_score: float,
        consistency_score: float,
        missing_fields: list[str],
        conflicting_fields: list[str]
    ) -> 'AccountProfileBuilder':
        """Set profile integrity"""
        self._profile_integrity = ProfileIntegrity(
            completeness_score=completeness_score,
            consistency_score=consistency_score,
            missing_fields=tuple(missing_fields),
            conflicting_fields=tuple(conflicting_fields)
        )
        return self
    
    def add_platform_metadata(self, key: str, value: Any) -> 'AccountProfileBuilder':
        """Add platform-specific metadata"""
        self._platform_metadata[key] = value
        return self
    
    def build(self) -> AccountProfile:
        """
        Build and freeze the AccountProfile.
        
        Validates all invariants and computes hash.
        FAIL-FAST on any validation error.
        """
        # Validate required fields
        if not all([
            self._account_id,
            self._entity_id,
            self._platform,
            self._created_at,
            self._snapshot_timestamp,
            self._verification_state,
            self._enforcement_state,
            self._profile_integrity
        ]):
            raise ValueError("Missing required fields for AccountProfile")
        
        # Calculate account age
        account_age_days = (self._snapshot_timestamp - self._created_at).days
        
        # Determine lifecycle stage
        lifecycle_stage = self._determine_lifecycle_stage(
            account_age_days,
            self._enforcement_state
        )
        
        # Build profile (without hash)
        profile_dict = {
            'account_id': self._account_id,
            'entity_id': self._entity_id,
            'platform': self._platform,
            'created_at': self._created_at,
            'snapshot_timestamp': self._snapshot_timestamp,
            'account_age_days': account_age_days,
            'lifecycle_stage': lifecycle_stage,
            'verification_state': self._verification_state,
            'enforcement_state': self._enforcement_state,
            'profile_integrity': self._profile_integrity,
            'platform_metadata': deepcopy(self._platform_metadata),
            'profile_version': self._profile_version,
            'invariants_passed': True,
            'profile_hash': ''
        }
        
        # Create temporary profile for hash calculation
        temp_profile = AccountProfile(**profile_dict)
        
        # Calculate hash
        profile_hash = ProfileHasher.hash_profile(temp_profile)
        
        # Create final profile with hash
        profile_dict['profile_hash'] = profile_hash
        profile = AccountProfile(**profile_dict)
        
        # Validate
        is_valid, errors = AccountProfileValidator.validate(profile)
        if not is_valid:
            raise ValueError(f"Profile validation failed: {errors}")
        
        return profile
    
    @staticmethod
    def _determine_lifecycle_stage(
        account_age_days: int,
        enforcement_state: EnforcementState
    ) -> LifecycleStage:
        """
        Determine lifecycle stage from age and enforcement.
        
        Deterministic rules:
        - Active restrictions → RESTRICTED
        - Recent enforcement (< 30 days) → RECOVERY or SENSITIVE
        - Otherwise based on age
        """
        # Active restrictions override everything
        if enforcement_state.active_restrictions:
            return LifecycleStage.RESTRICTED
        
        # Recent enforcement → recovery or sensitive
        if enforcement_state.last_enforcement_timestamp:
            # This is simplified; real implementation would check timestamp age
            if enforcement_state.severity_score > 0.5:
                return LifecycleStage.RECOVERY
            else:
                return LifecycleStage.SENSITIVE
        
        # Age-based for clean accounts
        if account_age_days < 7:
            return LifecycleStage.NEW
        elif account_age_days < 30:
            return LifecycleStage.WARMING
        else:
            return LifecycleStage.ACTIVE
    
    @classmethod
    def build_from_sources(
        cls,
        account_data: dict[str, Any],
        verification_data: dict[str, Any],
        enforcement_data: dict[str, Any],
        integrity_data: dict[str, Any],
        platform_metadata: Optional[dict[str, Any]] = None
    ) -> AccountProfile:
        """
        Build profile from multiple upstream sources.
        
        This is the primary factory method for production use.
        Merges and resolves conflicts deterministically.
        """
        builder = cls()
        
        # Set identity
        builder.set_identity(
            account_id=account_data['account_id'],
            entity_id=account_data['entity_id'],
            platform=account_data['platform']
        )
        
        # Set temporal
        builder.set_temporal(
            created_at=account_data['created_at'],
            snapshot_timestamp=account_data.get('snapshot_timestamp')
        )
        
        # Set verification
        builder.set_verification_state(
            email_verified=verification_data.get('email_verified', False),
            phone_verified=verification_data.get('phone_verified', False),
            verification_age_days=verification_data.get('verification_age_days', 0),
            verification_consistency=verification_data.get('verification_consistency', 1.0),
            last_verification_change=verification_data.get('last_verification_change')
        )
        
        # Set enforcement
        builder.set_enforcement_state(
            active_restrictions=enforcement_data.get('active_restrictions', []),
            historical_actions=enforcement_data.get('historical_actions', []),
            severity_score=enforcement_data.get('severity_score', 0.0),
            last_enforcement_timestamp=enforcement_data.get('last_enforcement_timestamp')
        )
        
        # Set integrity
        builder.set_profile_integrity(
            completeness_score=integrity_data.get('completeness_score', 1.0),
            consistency_score=integrity_data.get('consistency_score', 1.0),
            missing_fields=integrity_data.get('missing_fields', []),
            conflicting_fields=integrity_data.get('conflicting_fields', [])
        )
        
        # Add platform metadata
        if platform_metadata:
            for key, value in platform_metadata.items():
                builder.add_platform_metadata(key, value)
        
        return builder.build()


# ============================================================================
# PROFILE WATCHDOG
# ============================================================================

class ProfileWatchdog:
    """
    Monitors profile health and detects anomalies.
    
    Watches for:
    - Unexpected profile changes
    - Rapid lifecycle flips
    - Integrity degradation
    - Version mismatches
    
    Can trigger:
    - Trust recalculation
    - Posting slowdown
    - Experiment freeze
    """
    
    def __init__(self):
        self._profile_history: dict[str, list[AccountProfile]] = {}
    
    def record_snapshot(self, profile: AccountProfile) -> None:
        """Record a profile snapshot for monitoring"""
        account_id = profile.account_id
        
        if account_id not in self._profile_history:
            self._profile_history[account_id] = []
        
        self._profile_history[account_id].append(profile)
    
    def detect_anomalies(self, profile: AccountProfile) -> list[str]:
        """
        Detect anomalies in profile evolution.
        
        Returns list of detected anomaly descriptions.
        """
        anomalies = []
        
        account_id = profile.account_id
        if account_id not in self._profile_history:
            return anomalies
        
        history = self._profile_history[account_id]
        if len(history) < 2:
            return anomalies
        
        previous = history[-2]
        current = profile
        
        # Check for rapid lifecycle changes
        if previous.lifecycle_stage != current.lifecycle_stage:
            time_delta = (current.snapshot_timestamp - previous.snapshot_timestamp).total_seconds()
            if time_delta < 3600:  # Less than 1 hour
                anomalies.append(
                    f"Rapid lifecycle change: {previous.lifecycle_stage.value} → "
                    f"{current.lifecycle_stage.value} in {time_delta}s"
                )
        
        # Check for integrity degradation
        if current.profile_integrity.completeness_score < previous.profile_integrity.completeness_score - 0.2:
            anomalies.append(
                f"Integrity degradation: completeness dropped from "
                f"{previous.profile_integrity.completeness_score:.2f} to "
                f"{current.profile_integrity.completeness_score:.2f}"
            )
        
        # Check for enforcement escalation
        prev_severity = previous.enforcement_state.severity_score
        curr_severity = current.enforcement_state.severity_score
        if curr_severity > prev_severity + 0.3:
            anomalies.append(
                f"Enforcement escalation: severity jumped from "
                f"{prev_severity:.2f} to {curr_severity:.2f}"
            )
        
        # Check for time travel
        if current.snapshot_timestamp < previous.snapshot_timestamp:
            anomalies.append(
                f"Time travel detected: current snapshot ({current.snapshot_timestamp}) "
                f"is before previous ({previous.snapshot_timestamp})"
            )
        
        return anomalies
    
    def get_profile_history(self, account_id: str) -> list[AccountProfile]:
        """Get full profile history for an account"""
        return self._profile_history.get(account_id, []).copy()
    
    def clear_history(self, account_id: str) -> None:
        """Clear profile history for an account"""
        if account_id in self._profile_history:
            del self._profile_history[account_id]


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Building a canonical account profile
    
    now = datetime.now(timezone.utc)
    created = now.replace(day=1)  # Account created at start of month
    
    # Build profile using builder
    profile = AccountProfileBuilder.build_from_sources(
        account_data={
            'account_id': 'acct_12345',
            'entity_id': 'entity_abc',
            'platform': 'twitter',
            'created_at': created,
            'snapshot_timestamp': now
        },
        verification_data={
            'email_verified': True,
            'phone_verified': True,
            'verification_age_days': 15,
            'verification_consistency': 0.95
        },
        enforcement_data={
            'active_restrictions': [],
            'historical_actions': ['warning_spam'],
            'severity_score': 0.2,
            'last_enforcement_timestamp': now.replace(day=10)
        },
        integrity_data={
            'completeness_score': 0.9,
            'consistency_score': 0.95,
            'missing_fields': ['bio'],
            'conflicting_fields': []
        },
        platform_metadata={
            'follower_count': 1500,
            'following_count': 300,
            'tweet_count': 450
        }
    )
    
    print("=" * 80)
    print("ACCOUNT PROFILE SNAPSHOT")
    print("=" * 80)
    print(f"Account ID: {profile.account_id}")
    print(f"Platform: {profile.platform}")
    print(f"Lifecycle: {profile.lifecycle_stage.value}")
    print(f"Account Age: {profile.account_age_days} days")
    print(f"Verified: Email={profile.verification_state.email_verified}, "
          f"Phone={profile.verification_state.phone_verified}")
    print(f"Enforcement Severity: {profile.enforcement_state.severity_score}")
    print(f"Profile Hash: {profile.profile_hash[:16]}...")
    print(f"Integrity: Reliable={profile.profile_integrity.is_reliable}")
    print("=" * 80)
    
    # Validate
    is_valid, errors = AccountProfileValidator.validate(profile)
    print(f"\nValidation: {'PASS' if is_valid else 'FAIL'}")
    if errors:
        for error in errors:
            print(f"  - {error}")
    
    # Watchdog example
    watchdog = ProfileWatchdog()
    watchdog.record_snapshot(profile)
    
    # Simulate later snapshot with lifecycle change
    profile2 = AccountProfileBuilder.build_from_sources(
        account_data={
            'account_id': 'acct_12345',
            'entity_id': 'entity_abc',
            'platform': 'twitter',
            'created_at': created,
            'snapshot_timestamp': now.replace(minute=30)  # 30 mins later
        },
        verification_data={
            'email_verified': True,
            'phone_verified': True,
            'verification_age_days': 15,
            'verification_consistency': 0.95
        },
        enforcement_data={
            'active_restrictions': ['rate_limited'],
            'historical_actions': ['warning_spam', 'temp_restriction'],
            'severity_score': 0.6,
            'last_enforcement_timestamp': now.replace(minute=25)
        },
        integrity_data={
            'completeness_score': 0.9,
            'consistency_score': 0.95,
            'missing_fields': ['bio'],
            'conflicting_fields': []
        }
    )
    
    watchdog.record_snapshot(profile2)
    anomalies = watchdog.detect_anomalies(profile2)
    
    print("\n" + "=" * 80)
    print("WATCHDOG ANOMALY DETECTION")
    print("=" * 80)
    if anomalies:
        for anomaly in anomalies:
            print(f"  ⚠️  {anomaly}")
    else:
        print("  ✓ No anomalies detected")
    print("=" * 80)


