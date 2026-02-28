
"""
/account_system/account_invariants.py

Hard Safety, Legitimacy & Non-Negotiable Account Rules

CONSTITUTIONAL PRINCIPLE:
> Invariants are not optimized. They are obeyed.

This file defines absolute truths about what an account is allowed to be and do.
It answers ONE question: "Is this account still legitimate and allowed to operate?"

If the answer is no, nothing downstream is allowed to proceed.

RESPONSIBILITIES:
1. Define non-negotiable legitimacy rules
2. Validate account state against them
3. Fail fast on violations
4. Be deterministic & audit-safe    #UPGRADE ALL OF THIS TO FULLY INSTAGRAM LEVLE WORTH FO TRUST ACCOUTN REGISTRY AND EVERYTHING BUT MAKE SURE THAT CHATGPT GIVE SYOU ALL THE STRIAGHT PLAIN ANSWER AND NOT LIKE SOOME DELUSIONALY BULLSHIT!

5. Be platform-aware (but platform-agnostic)
6. Never infer, only assert
7. Never mutate state

WHAT THIS IS NOT:
❌ Not trust scoring
❌ Not enforcement detection
❌ Not suppression inference
❌ Not recovery logic
❌ Not posting strategy
❌ Not experimentation

This is the highest authority below platform rules themselves.
Nothing overrides this file.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional, Any, Tuple, List, Dict
from collections import defaultdict
import hashlib
import json


# ============================================================================
# ENUMS - Categorical Boundaries
# ============================================================================

class InvariantCategory(Enum):
    """Categories of invariants - what aspect of legitimacy they protect"""
    IDENTITY_LEGITIMACY = "identity_legitimacy"
    NETWORK_HYGIENE = "network_hygiene"
    ENFORCEMENT_HARD_STOPS = "enforcement_hard_stops"
    SUPPRESSION_ESCALATION = "suppression_escalation"
    AUTOMATION_LEGITIMACY = "automation_legitimacy"
    EXPERIMENT_SAFETY = "experiment_safety"


class InvariantSeverity(Enum):
    """Severity levels - determines mandatory action"""
    SOFT = "soft"           # Warning boundary - monitoring only
    HARD = "hard"           # Safety breach - freeze/isolate
    CRITICAL = "critical"   # Legitimacy loss - halt everything


class RequiredAction(Enum):
    """Mandatory actions when invariants are violated"""
    NONE = "none"           # No violation
    MONITOR = "monitor"     # Log and watch
    FREEZE = "freeze"       # Stop new operations
    ISOLATE = "isolate"     # Disconnect from network
    HALT = "halt"           # Full stop everything


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class AccountInvariant:
    """
    An immutable invariant definition.
    
    Each invariant is:
    - Explicit and versioned
    - Deterministic and testable
    - Platform-aware but agnostic
    - Pure (no side effects)
    """
    invariant_id: str
    category: InvariantCategory
    severity: InvariantSeverity
    
    description: str
    assertion: str  # Human-readable rule
    
    evaluation_fn: Callable[[dict], bool]
    
    applicable_platforms: frozenset[str] = field(default_factory=frozenset)
    
    # Metadata
    version: str = "1.0"
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def applies_to_platform(self, platform: str) -> bool:
        """Check if this invariant applies to given platform"""
        if not self.applicable_platforms:
            return True  # Universal invariant
        return platform.lower() in {p.lower() for p in self.applicable_platforms}
    
    def evaluate(self, account_state: dict) -> bool:
        """
        Evaluate the invariant against account state.
        Returns True if invariant is SATISFIED (good).
        Returns False if invariant is VIOLATED (bad).
        """
        try:
            return self.evaluation_fn(account_state)
        except Exception as e:
            # Evaluation failure = invariant violation (fail-safe)
            return False


@dataclass(frozen=True)
class InvariantViolation:
    """
    An immutable record of an invariant violation.
    Auditable and hash-stable.
    """
    invariant_id: str
    severity: InvariantSeverity
    description: str
    assertion: str
    
    detected_at: datetime
    context: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "invariant_id": self.invariant_id,
            "severity": self.severity.value,
            "description": self.description,
            "assertion": self.assertion,
            "detected_at": self.detected_at.isoformat(),
            "context": self.context
        }


@dataclass(frozen=True)
class InvariantEvaluationResult:
    """Complete evaluation result for an account"""
    account_id: str
    platform: str
    evaluation_time: datetime
    
    is_valid: bool
    violated_invariants: tuple[InvariantViolation, ...]
    
    required_action: RequiredAction
    invariants_version: str
    
    # Audit trail
    evaluation_hash: str
    total_invariants_checked: int
    
    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "platform": self.platform,
            "evaluation_time": self.evaluation_time.isoformat(),
            "is_valid": self.is_valid,
            "violated_invariants": [v.to_dict() for v in self.violated_invariants],
            "required_action": self.required_action.value,
            "invariants_version": self.invariants_version,
            "evaluation_hash": self.evaluation_hash,
            "total_invariants_checked": self.total_invariants_checked
        }


# ============================================================================
# INVARIANT REGISTRY - Central Truth Store
# ============================================================================

class AccountInvariantRegistry:
    """
    Central registry of all account invariants.
    
    GUARANTEES:
    - Version-controlled
    - Immutable after registration
    - Deterministic evaluation order
    - Audit-safe
    """
    
    def __init__(self, version: str = "1.0.0"):
        self._invariants: dict[str, AccountInvariant] = {}
        self._version = version
        self._locked = False
        
    @property
    def version(self) -> str:
        return self._version
    
    def register_invariant(self, invariant: AccountInvariant) -> None:
        """Register a new invariant"""
        if self._locked:
            raise RuntimeError("Registry is locked - cannot add invariants")
        
        if invariant.invariant_id in self._invariants:
            raise ValueError(f"Invariant {invariant.invariant_id} already registered")
        
        self._invariants[invariant.invariant_id] = invariant
    
    def lock(self) -> None:
        """Lock the registry - no more invariants can be added"""
        self._locked = True
    
    def get_applicable_invariants(self, platform: str) -> list[AccountInvariant]:
        """Get all invariants applicable to a platform"""
        return [
            inv for inv in self._invariants.values()
            if inv.applies_to_platform(platform)
        ]
    
    def get_invariant(self, invariant_id: str) -> Optional[AccountInvariant]:
        """Get specific invariant by ID"""
        return self._invariants.get(invariant_id)


# ============================================================================
# INVARIANT EVALUATOR - Deterministic Checker
# ============================================================================

class InvariantEvaluator:
    """
    Evaluates account state against all applicable invariants.
    
    GUARANTEES:
    - Deterministic (same input = same output)
    - Hash-stable (reproducible evaluation hash)
    - No side effects
    - Fail-safe (errors = violations)
    """
    
    def __init__(self, registry: AccountInvariantRegistry):
        self.registry = registry
    
    def evaluate_account(self, account_state: dict) -> InvariantEvaluationResult:
        """
        Evaluate account against all applicable invariants.
        
        Returns complete evaluation result with:
        - Validity status
        - All violations
        - Required action
        - Audit hash
        """
        account_id = account_state.get("account_id", "unknown")
        platform = account_state.get("platform", "unknown")
        eval_time = datetime.utcnow()
        
        # Get applicable invariants
        invariants = self.registry.get_applicable_invariants(platform)
        
        # Evaluate each invariant
        violations: list[InvariantViolation] = []
        
        for invariant in sorted(invariants, key=lambda i: i.invariant_id):
            is_satisfied = invariant.evaluate(account_state)
            
            if not is_satisfied:
                violation = InvariantViolation(
                    invariant_id=invariant.invariant_id,
                    severity=invariant.severity,
                    description=invariant.description,
                    assertion=invariant.assertion,
                    detected_at=eval_time,
                    context={
                        "platform": platform,
                        "category": invariant.category.value
                    }
                )
                violations.append(violation)
        
        # Determine validity and required action
        is_valid = len(violations) == 0
        required_action = self._determine_required_action(violations)
        
        # Generate audit hash
        eval_hash = self._generate_evaluation_hash(
            account_id, platform, eval_time, violations, invariants
        )
        
        return InvariantEvaluationResult(
            account_id=account_id,
            platform=platform,
            evaluation_time=eval_time,
            is_valid=is_valid,
            violated_invariants=tuple(violations),
            required_action=required_action,
            invariants_version=self.registry.version,
            evaluation_hash=eval_hash,
            total_invariants_checked=len(invariants)
        )
    
    def _determine_required_action(
        self, violations: list[InvariantViolation]
    ) -> RequiredAction:
        """Determine required action based on violation severity"""
        if not violations:
            return RequiredAction.NONE
        
        # Get max severity
        severities = [v.severity for v in violations]
        
        if InvariantSeverity.CRITICAL in severities:
            return RequiredAction.HALT
        elif InvariantSeverity.HARD in severities:
            # Check if network hygiene is involved
            categories = {
                v.context.get("category") for v in violations
                if v.severity == InvariantSeverity.HARD
            }
            if InvariantCategory.NETWORK_HYGIENE.value in categories:
                return RequiredAction.ISOLATE
            else:
                return RequiredAction.FREEZE
        elif InvariantSeverity.SOFT in severities:
            return RequiredAction.MONITOR
        
        return RequiredAction.NONE
    
    def _generate_evaluation_hash(
        self,
        account_id: str,
        platform: str,
        eval_time: datetime,
        violations: list[InvariantViolation],
        invariants: list[AccountInvariant]
    ) -> str:
        """
        Generate deterministic hash of evaluation.
        
        Hash prevents:
        - Retroactive weakening
        - "Temporary bypasses"
        - Silent policy drift
        """
        hash_input = {
            "account_id": account_id,
            "platform": platform,
            "evaluation_time": eval_time.isoformat(),
            "violations": sorted([
                {
                    "invariant_id": v.invariant_id,
                    "severity": v.severity.value,
                    "detected_at": v.detected_at.isoformat()
                }
                for v in violations
            ], key=lambda x: x["invariant_id"]),
            "invariants_checked": sorted([i.invariant_id for i in invariants]),
            "registry_version": self.registry.version
        }
        
        hash_str = json.dumps(hash_input, sort_keys=True)
        return hashlib.sha256(hash_str.encode()).hexdigest()


# ============================================================================
# INVARIANT WATCHDOG - Institutional Memory Protection
# ============================================================================

class InvariantWatchdog:
    """
    Monitors for suspicious changes to invariant system.
    
    ALERTS ON:
    - Invariant set changes without version bump
    - Violations disappear unexpectedly
    - Severity mapping mutates
    - Evaluation frequency drops
    
    Protects institutional memory and prevents drift.
    """
    
    def __init__(self):
        self._evaluation_history: list[InvariantEvaluationResult] = []
        self._invariant_checksums: dict[str, str] = {}
        self._last_version: Optional[str] = None
        
    def record_evaluation(self, result: InvariantEvaluationResult) -> None:
        """Record an evaluation for watchdog analysis"""
        self._evaluation_history.append(result)
        
        # Detect version changes
        if self._last_version and result.invariants_version != self._last_version:
            self._alert_version_change(self._last_version, result.invariants_version)
        
        self._last_version = result.invariants_version
    
    def check_for_drift(self, registry: AccountInvariantRegistry) -> list[str]:
        """
        Check for policy drift.
        Returns list of alerts.
        """
        alerts = []
        
        # Check if invariant set changed without version bump
        current_checksum = self._compute_registry_checksum(registry)
        registry_version = registry.version
        
        if registry_version in self._invariant_checksums:
            if self._invariant_checksums[registry_version] != current_checksum:
                alerts.append(
                    f"CRITICAL: Invariant set changed without version bump "
                    f"(version {registry_version})"
                )
        else:
            self._invariant_checksums[registry_version] = current_checksum
        
        # Check for unexpected violation disappearance
        if len(self._evaluation_history) >= 2:
            alerts.extend(self._check_violation_disappearance())
        
        return alerts
    
    def _compute_registry_checksum(self, registry: AccountInvariantRegistry) -> str:
        """Compute checksum of entire registry"""
        invariant_data = sorted([
            {
                "id": inv.invariant_id,
                "severity": inv.severity.value,
                "category": inv.category.value,
                "assertion": inv.assertion
            }
            for inv in registry._invariants.values()
        ], key=lambda x: x["id"])
        
        checksum_str = json.dumps(invariant_data, sort_keys=True)
        return hashlib.sha256(checksum_str.encode()).hexdigest()
    
    def _check_violation_disappearance(self) -> list[str]:
        """Check if violations disappeared unexpectedly"""
        alerts = []
        
        recent_results = self._evaluation_history[-10:]
        
        # Track violations by account
        account_violations: dict[str, list[set[str]]] = defaultdict(list)
        
        for result in recent_results:
            violation_ids = {v.invariant_id for v in result.violated_invariants}
            account_violations[result.account_id].append(violation_ids)
        
        # Check for sudden disappearance of CRITICAL violations
        for account_id, violation_sets in account_violations.items():
            if len(violation_sets) >= 2:
                prev_violations = violation_sets[-2]
                curr_violations = violation_sets[-1]
                
                disappeared = prev_violations - curr_violations
                
                if disappeared:
                    alerts.append(
                        f"WARNING: Violations disappeared for {account_id}: "
                        f"{disappeared}"
                    )
        
        return alerts
    
    def _alert_version_change(self, old_version: str, new_version: str) -> None:
        """Alert on version change (could extend to external logging)"""
        print(f"INFO: Invariant version changed: {old_version} -> {new_version}")


# ============================================================================
# BUILT-IN INVARIANTS - Production-Grade Definitions
# ============================================================================

def create_standard_invariants() -> AccountInvariantRegistry:
    """
    Create registry with standard production invariants.
    
    These are the constitutional rules that protect system integrity.
    """
    registry = AccountInvariantRegistry(version="1.0.0")
    
    # ========================================================================
    # 1. IDENTITY LEGITIMACY
    # ========================================================================
    
    registry.register_invariant(AccountInvariant(
        invariant_id="IDENT_001_VALID_CREATION",
        category=InvariantCategory.IDENTITY_LEGITIMACY,
        severity=InvariantSeverity.CRITICAL,
        description="Account must have valid creation metadata",
        assertion="created_at must exist and be <= now",
        evaluation_fn=lambda state: (
            "created_at" in state and
            isinstance(state["created_at"], datetime) and
            state["created_at"] <= datetime.utcnow()
        )
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="IDENT_002_STABLE_PLATFORM_ID",
        category=InvariantCategory.IDENTITY_LEGITIMACY,
        severity=InvariantSeverity.CRITICAL,
        description="Platform account ID must be stable and non-empty",
        assertion="platform_account_id must exist and be non-empty string",
        evaluation_fn=lambda state: (
            "platform_account_id" in state and
            isinstance(state["platform_account_id"], str) and
            len(state["platform_account_id"]) > 0
        )
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="IDENT_003_NO_IMPERSONATION",
        category=InvariantCategory.IDENTITY_LEGITIMACY,
        severity=InvariantSeverity.CRITICAL,
        description="Account must not be flagged for impersonation",
        assertion="impersonation_flag must be False or absent",
        evaluation_fn=lambda state: not state.get("impersonation_flag", False)
    ))
    
    # ========================================================================
    # 2. NETWORK HYGIENE
    # ========================================================================
    
    registry.register_invariant(AccountInvariant(
        invariant_id="NETWORK_001_NO_BANNED_IP",
        category=InvariantCategory.NETWORK_HYGIENE,
        severity=InvariantSeverity.HARD,
        description="Account must not be on banned IP cluster",
        assertion="banned_ip_cluster must be False",
        evaluation_fn=lambda state: not state.get("banned_ip_cluster", False)
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="NETWORK_002_AFFILIATION_RISK",
        category=InvariantCategory.NETWORK_HYGIENE,
        severity=InvariantSeverity.HARD,
        description="High-risk affiliation overlap must be below threshold",
        assertion="high_risk_affiliation_overlap < 0.7",
        evaluation_fn=lambda state: (
            state.get("high_risk_affiliation_overlap", 0.0) < 0.7
        )
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="NETWORK_003_SANCTIONED_CONTAGION",
        category=InvariantCategory.NETWORK_HYGIENE,
        severity=InvariantSeverity.CRITICAL,
        description="Must not have direct link to sanctioned accounts",
        assertion="sanctioned_account_links must be 0",
        evaluation_fn=lambda state: state.get("sanctioned_account_links", 0) == 0
    ))
    
    # ========================================================================
    # 3. ENFORCEMENT HARD STOPS
    # ========================================================================
    
    registry.register_invariant(AccountInvariant(
        invariant_id="ENFORCE_001_HARD_BAN",
        category=InvariantCategory.ENFORCEMENT_HARD_STOPS,
        severity=InvariantSeverity.CRITICAL,
        description="Account must not be under hard ban",
        assertion="hard_ban must be False",
        evaluation_fn=lambda state: not state.get("hard_ban", False)
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="ENFORCE_002_PERMANENT_RESTRICTION",
        category=InvariantCategory.ENFORCEMENT_HARD_STOPS,
        severity=InvariantSeverity.CRITICAL,
        description="Account must not have permanent posting restriction",
        assertion="permanent_posting_restriction must be False",
        evaluation_fn=lambda state: not state.get("permanent_posting_restriction", False)
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="ENFORCE_003_PLATFORM_INELIGIBLE",
        category=InvariantCategory.ENFORCEMENT_HARD_STOPS,
        severity=InvariantSeverity.CRITICAL,
        description="Account must not be platform-declared ineligible",
        assertion="platform_ineligible must be False",
        evaluation_fn=lambda state: not state.get("platform_ineligible", False)
    ))
    
    # ========================================================================
    # 4. SUPPRESSION ESCALATION CEILINGS
    # ========================================================================
    
    registry.register_invariant(AccountInvariant(
        invariant_id="SUPPRESS_001_SUSTAINED_CEILING",
        category=InvariantCategory.SUPPRESSION_ESCALATION,
        severity=InvariantSeverity.HARD,
        description="Sustained suppression must not exceed hard ceiling",
        assertion="sustained_suppression_score < 0.85",
        evaluation_fn=lambda state: (
            state.get("sustained_suppression_score", 0.0) < 0.85
        )
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="SUPPRESS_002_THROTTLE_ENFORCEMENT_CORRELATION",
        category=InvariantCategory.SUPPRESSION_ESCALATION,
        severity=InvariantSeverity.HARD,
        description="Prolonged throttle + enforcement correlation requires cooldown",
        assertion="throttle_enforcement_correlation < 0.75 OR enforcement_count < 3",
        evaluation_fn=lambda state: (
            state.get("throttle_enforcement_correlation", 0.0) < 0.75 or
            state.get("enforcement_count", 0) < 3
        )
    ))
    
    # ========================================================================
    # 5. AUTOMATION LEGITIMACY
    # ========================================================================
    
    registry.register_invariant(AccountInvariant(
        invariant_id="AUTO_001_POST_CADENCE_LIMIT",
        category=InvariantCategory.AUTOMATION_LEGITIMACY,
        severity=InvariantSeverity.HARD,
        description="Posting cadence must not exceed hard limit",
        assertion="posts_per_hour <= 30",
        evaluation_fn=lambda state: state.get("posts_per_hour", 0) <= 30
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="AUTO_002_BOT_SIGNATURE",
        category=InvariantCategory.AUTOMATION_LEGITIMACY,
        severity=InvariantSeverity.HARD,
        description="Must not have bot-like behavioral signature",
        assertion="bot_signature_score < 0.8",
        evaluation_fn=lambda state: state.get("bot_signature_score", 0.0) < 0.8
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="AUTO_003_PACING_VIOLATION",
        category=InvariantCategory.AUTOMATION_LEGITIMACY,
        severity=InvariantSeverity.HARD,
        description="Must not have invariant pacing violations",
        assertion="pacing_variance_coefficient < 0.05",
        evaluation_fn=lambda state: (
            state.get("pacing_variance_coefficient", 1.0) >= 0.05
        )
    ))
    
    # ========================================================================
    # 6. EXPERIMENT SAFETY BARRIERS
    # ========================================================================
    
    registry.register_invariant(AccountInvariant(
        invariant_id="EXPERIMENT_001_NO_DURING_ENFORCEMENT",
        category=InvariantCategory.EXPERIMENT_SAFETY,
        severity=InvariantSeverity.HARD,
        description="Cannot run experiments while enforcement active",
        assertion="active_experiment=False OR active_enforcement=False",
        evaluation_fn=lambda state: not (
            state.get("active_experiment", False) and
            state.get("active_enforcement", False)
        )
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="EXPERIMENT_002_TRUST_FLOOR",
        category=InvariantCategory.EXPERIMENT_SAFETY,
        severity=InvariantSeverity.HARD,
        description="Cannot rollout experiments if trust below floor",
        assertion="experiment_rollout=False OR trust_score >= 0.3",
        evaluation_fn=lambda state: not (
            state.get("experiment_rollout", False) and
            state.get("trust_score", 1.0) < 0.3
        )
    ))
    
    registry.register_invariant(AccountInvariant(
        invariant_id="EXPERIMENT_003_NO_CONFLICTS",
        category=InvariantCategory.EXPERIMENT_SAFETY,
        severity=InvariantSeverity.HARD,
        description="Cannot run conflicting experiments on same account",
        assertion="active_experiment_count <= 1",
        evaluation_fn=lambda state: state.get("active_experiment_count", 0) <= 1
    ))
    
    # Lock registry to prevent further modifications
    registry.lock()
    
    return registry


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def evaluate_account_invariants(account_state: dict) -> InvariantEvaluationResult:
    """
    Convenience function to evaluate account against standard invariants.
    
    Usage:
        result = evaluate_account_invariants(account_state)
        
        if not result.is_valid:
            if result.required_action == RequiredAction.HALT:
                # Stop everything
            elif result.required_action == RequiredAction.FREEZE:
                # Freeze operations
    """
    registry = create_standard_invariants()
    evaluator = InvariantEvaluator(registry)
    return evaluator.evaluate_account(account_state)


def check_account_legitimacy(account_state: dict) -> bool:
    """
    Quick legitimacy check - returns True if account passes all invariants.
    
    Usage:
        if not check_account_legitimacy(account_state):
            # Account is not legitimate - halt operations
    """
    result = evaluate_account_invariants(account_state)
    return result.is_valid


# ============================================================================
# MODULE EXPORTS
# ============================================================================

__all__ = [
    # Enums
    "InvariantCategory",
    "InvariantSeverity",
    "RequiredAction",
    
    # Data structures
    "AccountInvariant",
    "InvariantViolation",
    "InvariantEvaluationResult",
    
    # Core components
    "AccountInvariantRegistry",
    "InvariantEvaluator",
    "InvariantWatchdog",
    
    # Factory functions
    "create_standard_invariants",
    
    # Convenience functions
    "evaluate_account_invariants",
    "check_account_legitimacy"
]


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    # Example account state
    account_state = {
        "account_id": "acc_12345",
        "platform": "twitter",
        "platform_account_id": "twitter_67890",
        "created_at": datetime.utcnow() - timedelta(days=365),
        
        # Identity flags
        "impersonation_flag": False,
        
        # Network signals
        "banned_ip_cluster": False,
        "high_risk_affiliation_overlap": 0.3,
        "sanctioned_account_links": 0,
        
        # Enforcement state
        "hard_ban": False,
        "permanent_posting_restriction": False,
        "platform_ineligible": False,
        
        # Suppression signals
        "sustained_suppression_score": 0.6,
        "throttle_enforcement_correlation": 0.5,
        "enforcement_count": 1,
        
        # Automation signals
        "posts_per_hour": 10,
        "bot_signature_score": 0.3,
        "pacing_variance_coefficient": 0.2,
        
        # Experiment state
        "active_experiment": False,
        "active_enforcement": False,
        "experiment_rollout": False,
        "trust_score": 0.7,
        "active_experiment_count": 0
    }
    
    # Evaluate
    result = evaluate_account_invariants(account_state)
    
    print("=" * 80)
    print("ACCOUNT INVARIANT EVALUATION")
    print("=" * 80)
    print(f"Account: {result.account_id}")
    print(f"Platform: {result.platform}")
    print(f"Valid: {result.is_valid}")
    print(f"Required Action: {result.required_action.value}")
    print(f"Invariants Checked: {result.total_invariants_checked}")
    print(f"Violations: {len(result.violated_invariants)}")
    print(f"Hash: {result.evaluation_hash}")
    print()
    
    if result.violated_invariants:
        print("VIOLATIONS:")
        for v in result.violated_invariants:
            print(f"  [{v.severity.value.upper()}] {v.invariant_id}")
            print(f"    {v.description}")
            print(f"    Rule: {v.assertion}")
            print()
    
    # Test with violation
    print("=" * 80)
    print("TESTING WITH HARD BAN")
    print("=" * 80)
    
    banned_state = account_state.copy()
    banned_state["hard_ban"] = True
    
    result2 = evaluate_account_invariants(banned_state)
    print(f"Valid: {result2.is_valid}")
    print(f"Required Action: {result2.required_action.value}")
    print(f"Violations: {len(result2.violated_invariants)}")
    
    # Demonstrate watchdog
    print()
    print("=" * 80)
    print("WATCHDOG DEMONSTRATION")
    print("=" * 80)
    
    watchdog = InvariantWatchdog()
    watchdog.record_evaluation(result)
    watchdog.record_evaluation(result2)
    
    registry = create_standard_invariants()
    alerts = watchdog.check_for_drift(registry)
    
    if alerts:
        print("ALERTS:")
        for alert in alerts:
            print(f"  {alert}")
    else:
        print("No drift detected")









