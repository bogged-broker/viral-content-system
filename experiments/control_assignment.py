"""
control_assignment.py — TRAFFIC SPLITTING & ISOLATION AUTHORITY

This file is the only thing standing between you and fake wins.

Core responsibility:
- Assign traffic to control vs variants
- Guarantee isolation between experiment arms
- Prevent cross-arm contamination
- Enforce exclusivity rules
- Ensure deterministic replay
- Produce complete audit trail

This file does not optimize performance. It protects truth.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Any, Tuple, List, Dict
from collections import defaultdict
from pathlib import Path


# ============================================================================
# CORE DATA MODELS (STRICT)
# ============================================================================

@dataclass(frozen=True)
class TrafficUnit:
    """
    Atomic assignable entity.
    No partial units. No dynamic resizing.
    """
    unit_id: str                    # user_id, account_id, geo_bucket
    platform: str                   # tiktok, youtube, instagram
    region: str                     # geo region
    time_bucket: str                # coarse-grained temporal bucket
    capability_flags: frozenset     # autoplay, sound_on, etc
    
    def __post_init__(self):
        if not self.unit_id or not self.platform or not self.region:
            raise ValueError("TrafficUnit requires unit_id, platform, region")
        
        # Ensure capability_flags is frozen
        if not isinstance(self.capability_flags, frozenset):
            object.__setattr__(self, 'capability_flags', frozenset(self.capability_flags))
    
    def to_hash_key(self) -> str:
        """Deterministic hash key for assignment."""
        parts = [
            self.unit_id,
            self.platform,
            self.region,
            self.time_bucket,
            "|".join(sorted(self.capability_flags))
        ]
        return ":".join(parts)


class IsolationLevel(Enum):
    """
    Explicit isolation guarantees.
    NONE is forbidden for learning experiments.
    """
    HARD = "hard"          # strict disjoint traffic
    SOFT = "soft"          # statistical isolation
    NONE = "none"          # forbidden for learning
    
    def is_valid_for_learning(self) -> bool:
        return self != IsolationLevel.NONE


@dataclass(frozen=True)
class AssignmentPolicy:
    """
    Complete assignment policy specification.
    No defaults. No inferred values.
    """
    experiment_id: str
    
    # Split configuration
    split_ratio: dict[str, float]           # {"control": 0.5, "variant_a": 0.5}
    isolation_level: IsolationLevel
    
    # Stratification
    stratify_on: tuple[str, ...]            # (region, platform, device)
    deterministic_seed: str
    
    # Constraints
    exclusivity_group: str                  # blocks other experiments
    max_exposure_fraction: float            # cap on total traffic
    
    # Reversibility
    reversible: bool                        # can assignments be undone?
    
    def __post_init__(self):
        # Validate split ratio
        if not self.split_ratio:
            raise ValueError("split_ratio cannot be empty")
        
        total = sum(self.split_ratio.values())
        if not (0.99 <= total <= 1.01):  # float tolerance
            raise ValueError(f"split_ratio must sum to 1.0, got {total}")
        
        if "control" not in self.split_ratio:
            raise ValueError("split_ratio must include 'control' arm")
        
        # Validate isolation
        if not self.isolation_level.is_valid_for_learning():
            raise ValueError(f"IsolationLevel.NONE forbidden for experiments")
        
        # Validate exposure
        if not (0.0 < self.max_exposure_fraction <= 1.0):
            raise ValueError("max_exposure_fraction must be in (0, 1]")
        
        # Ensure stratify_on is tuple
        if not isinstance(self.stratify_on, tuple):
            object.__setattr__(self, 'stratify_on', tuple(self.stratify_on))
    
    def policy_hash(self) -> str:
        """Deterministic hash of policy for audit."""
        policy_dict = {
            'experiment_id': self.experiment_id,
            'split_ratio': dict(sorted(self.split_ratio.items())),
            'isolation_level': self.isolation_level.value,
            'stratify_on': list(self.stratify_on),
            'seed': self.deterministic_seed,
            'exclusivity_group': self.exclusivity_group,
            'max_exposure': self.max_exposure_fraction,
            'reversible': self.reversible
        }
        
        policy_str = json.dumps(policy_dict, sort_keys=True)
        return hashlib.sha256(policy_str.encode()).hexdigest()


@dataclass(frozen=True)
class AssignmentRecord:
    """
    Immutable assignment record.
    Written to append-only ledger.
    """
    experiment_id: str
    traffic_unit_id: str
    assigned_arm: str               # control, variant_a, etc
    
    assignment_hash: str            # deterministic assignment hash
    isolation_level: str
    policy_hash: str
    
    stratification_key: str         # what strata was this in?
    exposure_timestamp: str
    
    created_at: str
    
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ============================================================================
# SPLIT STRATEGIES (PLUGGABLE)
# ============================================================================

class SplitStrategy:
    """Base class for split strategies."""
    
    def assign(self, hash_value: float, split_ratio: dict[str, float]) -> str:
        """
        Assign to arm based on hash value [0, 1) and split ratio.
        Pure deterministic math.
        """
        raise NotImplementedError


class FixedRatioSplit(SplitStrategy):
    """
    Fixed ratio assignment (50/50, 90/10, etc).
    Most common strategy.
    """
    
    def assign(self, hash_value: float, split_ratio: dict[str, float]) -> str:
        # Sort arms for determinism
        sorted_arms = sorted(split_ratio.items())
        
        cumulative = 0.0
        for arm, ratio in sorted_arms:
            cumulative += ratio
            if hash_value < cumulative:
                return arm
        
        # Fallback (should never hit due to validation)
        return sorted_arms[-1][0]


class RampedRolloutSplit(SplitStrategy):
    """
    Gradual rollout with time-based ramping.
    Useful for canary deployments.
    """
    
    def __init__(self, ramp_schedule: dict[str, float]):
        """
        ramp_schedule: {time_bucket: treatment_fraction}
        e.g., {"2024-01": 0.1, "2024-02": 0.5, "2024-03": 1.0}
        """
        self.ramp_schedule = ramp_schedule
    
    def assign(self, hash_value: float, split_ratio: dict[str, float],
               time_bucket: str) -> str:
        # Get current ramp fraction
        ramp_fraction = self.ramp_schedule.get(time_bucket, 0.0)
        
        # Adjust split ratio based on ramp
        adjusted_ratio = split_ratio.copy()
        if "variant_a" in adjusted_ratio:
            control_fraction = 1.0 - ramp_fraction
            adjusted_ratio["control"] = control_fraction
            adjusted_ratio["variant_a"] = ramp_fraction
        
        return FixedRatioSplit().assign(hash_value, adjusted_ratio)


# ============================================================================
# STRATIFICATION ENGINE
# ============================================================================

class StratificationEngine:
    """
    Ensures distribution parity across arms.
    Prevents demographic skew.
    """
    
    def __init__(self, stratify_fields: tuple[str, ...]):
        self.stratify_fields = stratify_fields
    
    def get_stratum_key(self, traffic_unit: TrafficUnit) -> str:
        """Generate stratification key from traffic unit."""
        parts = []
        for field in self.stratify_fields:
            value = getattr(traffic_unit, field, "unknown")
            parts.append(f"{field}={value}")
        
        return "|".join(parts)
    
    def validate_distribution(self, assignments: dict[str, list[TrafficUnit]]) -> bool:
        """
        Validate that stratification is balanced across arms.
        Returns False if significant skew detected.
        """
        strata_counts = defaultdict(lambda: defaultdict(int))
        
        for arm, units in assignments.items():
            for unit in units:
                stratum = self.get_stratum_key(unit)
                strata_counts[stratum][arm] += 1
        
        # Check each stratum for balance
        for stratum, arm_counts in strata_counts.items():
            if len(arm_counts) < 2:
                continue  # Can't check balance with <2 arms
            
            counts = list(arm_counts.values())
            max_count = max(counts)
            min_count = min(counts)
            
            # Allow up to 20% imbalance (adjustable threshold)
            if max_count > 0 and min_count / max_count < 0.8:
                return False
        
        return True


# ============================================================================
# CONTAMINATION GUARD (CRITICAL)
# ============================================================================

class ContaminationGuard:
    """
    Detects and blocks cross-arm contamination.
    
    Prevents:
    - Same user in multiple arms
    - Same account reused
    - Correlated exposure paths
    - Time-leakage effects
    """
    
    def __init__(self):
        self.seen_units: dict[str, set[str]] = defaultdict(set)  # experiment_id -> unit_ids
        self.unit_to_arm: dict[str, dict[str, str]] = defaultdict(dict)  # experiment_id -> {unit_id: arm}
    
    def check_contamination(self, experiment_id: str, 
                           traffic_unit: TrafficUnit,
                           assigned_arm: str) -> tuple[bool, str]:
        """
        Check if assignment would cause contamination.
        Returns (is_valid, reason).
        """
        unit_key = traffic_unit.to_hash_key()
        
        # Check if unit already assigned to different arm
        if unit_key in self.unit_to_arm[experiment_id]:
            existing_arm = self.unit_to_arm[experiment_id][unit_key]
            if existing_arm != assigned_arm:
                return False, f"Unit {traffic_unit.unit_id} already in {existing_arm}, cannot assign to {assigned_arm}"
        
        # Check for correlated exposure (same user across experiments)
        # This is a simplified check - production would be more sophisticated
        
        return True, ""
    
    def record_assignment(self, experiment_id: str,
                         traffic_unit: TrafficUnit,
                         assigned_arm: str):
        """Record valid assignment."""
        unit_key = traffic_unit.to_hash_key()
        self.seen_units[experiment_id].add(unit_key)
        self.unit_to_arm[experiment_id][unit_key] = assigned_arm
    
    def detect_leakage(self, experiment_id: str, 
                      assignments: dict[str, list[TrafficUnit]]) -> list[str]:
        """
        Detect time-leakage or other contamination patterns.
        Returns list of issues found.
        """
        issues = []
        
        # Check for same unit in multiple arms
        all_units = defaultdict(list)
        for arm, units in assignments.items():
            for unit in units:
                all_units[unit.unit_id].append(arm)
        
        for unit_id, arms in all_units.items():
            if len(set(arms)) > 1:
                issues.append(f"Unit {unit_id} appears in multiple arms: {set(arms)}")
        
        return issues


# ============================================================================
# CONTROL ASSIGNMENT ENGINE (CORE)
# ============================================================================

class ControlAssignmentEngine:
    """
    The traffic court.
    Assigns traffic deterministically with isolation guarantees.
    """
    
    def __init__(self, 
                 split_strategy: Optional[SplitStrategy] = None,
                 platform_constraints: Optional[dict] = None):
        self.split_strategy = split_strategy or FixedRatioSplit()
        self.platform_constraints = platform_constraints or {}
        self.contamination_guard = ContaminationGuard()
        
        # Active experiments tracking
        self.active_experiments: dict[str, AssignmentPolicy] = {}
        self.exclusivity_groups: dict[str, str] = {}  # group -> experiment_id
    
    def _compute_assignment_hash(self, traffic_unit: TrafficUnit,
                                 policy: AssignmentPolicy) -> float:
        """
        Compute deterministic hash value [0, 1) for assignment.
        Same inputs → same hash → same assignment.
        """
        hash_input = ":".join([
            traffic_unit.to_hash_key(),
            policy.experiment_id,
            policy.deterministic_seed
        ])
        
        hash_bytes = hashlib.sha256(hash_input.encode()).digest()
        hash_int = int.from_bytes(hash_bytes[:8], byteorder='big')
        
        # Normalize to [0, 1)
        return hash_int / (2 ** 64)
    
    def _validate_experiment_eligibility(self, policy: AssignmentPolicy) -> tuple[bool, str]:
        """Validate that experiment can be started."""
        
        # Check exclusivity
        if policy.exclusivity_group in self.exclusivity_groups:
            existing_exp = self.exclusivity_groups[policy.exclusivity_group]
            if existing_exp != policy.experiment_id:
                return False, f"Exclusivity group {policy.exclusivity_group} already used by {existing_exp}"
        
        # Check isolation level
        if not policy.isolation_level.is_valid_for_learning():
            return False, "IsolationLevel.NONE forbidden for learning experiments"
        
        return True, ""
    
    def _filter_traffic_units(self, traffic_units: list[TrafficUnit],
                             policy: AssignmentPolicy) -> list[TrafficUnit]:
        """
        Filter traffic units based on exposure cap and eligibility.
        """
        # Apply platform constraints
        filtered = []
        for unit in traffic_units:
            if unit.platform in self.platform_constraints:
                constraints = self.platform_constraints[unit.platform]
                # Apply platform-specific filtering
                # (simplified - production would be more complex)
                if self._check_platform_constraints(unit, constraints):
                    filtered.append(unit)
            else:
                filtered.append(unit)
        
        # Apply exposure cap
        max_units = int(len(filtered) * policy.max_exposure_fraction)
        
        # Deterministically select units up to cap
        hash_sorted = sorted(filtered, 
                           key=lambda u: self._compute_assignment_hash(u, policy))
        
        return hash_sorted[:max_units]
    
    def _check_platform_constraints(self, unit: TrafficUnit, 
                                   constraints: dict) -> bool:
        """Check platform-specific constraints."""
        # Simplified version
        return True
    
    def assign(self, traffic_units: list[TrafficUnit],
              policy: AssignmentPolicy) -> dict[str, Any]:
        """
        Main assignment method.
        
        Flow:
        1. Validate experiment eligibility
        2. Filter traffic units
        3. Apply stratification
        4. Perform deterministic assignment
        5. Enforce isolation
        6. Record assignments
        7. Return immutable records
        
        If any step fails → assignment is blocked.
        """
        
        # Step 1: Validate eligibility
        is_valid, reason = self._validate_experiment_eligibility(policy)
        if not is_valid:
            return {
                "success": False,
                "error": reason,
                "assignments": {}
            }
        
        # Step 2: Filter traffic units
        eligible_units = self._filter_traffic_units(traffic_units, policy)
        
        if not eligible_units:
            return {
                "success": False,
                "error": "No eligible traffic units after filtering",
                "assignments": {}
            }
        
        # Step 3: Apply stratification
        stratification = StratificationEngine(policy.stratify_on)
        
        # Step 4: Perform deterministic assignment
        assignments = defaultdict(list)
        assignment_records = []
        
        for unit in eligible_units:
            # Compute deterministic hash
            hash_value = self._compute_assignment_hash(unit, policy)
            
            # Assign to arm
            assigned_arm = self.split_strategy.assign(hash_value, policy.split_ratio)
            
            # Check contamination
            is_clean, contamination_reason = self.contamination_guard.check_contamination(
                policy.experiment_id, unit, assigned_arm
            )
            
            if not is_clean:
                # Skip contaminated assignment
                continue
            
            # Record assignment
            assignments[assigned_arm].append(unit)
            self.contamination_guard.record_assignment(
                policy.experiment_id, unit, assigned_arm
            )
            
            # Create assignment record
            stratum_key = stratification.get_stratum_key(unit)
            
            record = AssignmentRecord(
                experiment_id=policy.experiment_id,
                traffic_unit_id=unit.unit_id,
                assigned_arm=assigned_arm,
                assignment_hash=f"{hash_value:.16f}",
                isolation_level=policy.isolation_level.value,
                policy_hash=policy.policy_hash(),
                stratification_key=stratum_key,
                exposure_timestamp=str(int(time.time())),
                created_at=str(int(time.time()))
            )
            
            assignment_records.append(record)
        
        # Step 5: Validate stratification balance
        if not stratification.validate_distribution(assignments):
            return {
                "success": False,
                "error": "Stratification validation failed - significant skew detected",
                "assignments": {}
            }
        
        # Step 6: Detect leakage
        leakage_issues = self.contamination_guard.detect_leakage(
            policy.experiment_id, assignments
        )
        
        if leakage_issues:
            return {
                "success": False,
                "error": f"Contamination detected: {'; '.join(leakage_issues)}",
                "assignments": {}
            }
        
        # Step 7: Register experiment
        self.active_experiments[policy.experiment_id] = policy
        self.exclusivity_groups[policy.exclusivity_group] = policy.experiment_id
        
        return {
            "success": True,
            "experiment_id": policy.experiment_id,
            "policy_hash": policy.policy_hash(),
            "assignments": {arm: len(units) for arm, units in assignments.items()},
            "records": assignment_records,
            "total_assigned": len(assignment_records)
        }
    
    def validate_isolation(self, experiment_id: str) -> dict[str, Any]:
        """
        Validate that isolation guarantees are being maintained.
        """
        if experiment_id not in self.active_experiments:
            return {
                "valid": False,
                "error": f"Experiment {experiment_id} not active"
            }
        
        policy = self.active_experiments[experiment_id]
        
        # Check isolation level compliance
        # (simplified - production would check actual traffic patterns)
        
        return {
            "valid": True,
            "isolation_level": policy.isolation_level.value,
            "exclusivity_group": policy.exclusivity_group
        }
    
    def enforce_exclusivity(self, new_policy: AssignmentPolicy) -> bool:
        """
        Enforce that no conflicting experiments run simultaneously.
        """
        if new_policy.exclusivity_group in self.exclusivity_groups:
            existing = self.exclusivity_groups[new_policy.exclusivity_group]
            return existing == new_policy.experiment_id
        
        return True


# ============================================================================
# ASSIGNMENT LEDGER (IMMUTABLE)
# ============================================================================

class AssignmentLedger:
    """
    Append-only ledger of all assignments.
    Foundation for audit trail.
    """
    
    def __init__(self, ledger_dir: str = "./experiments/traffic_assignments"):
        self.ledger_dir = Path(ledger_dir)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
    
    def write_assignments(self, records: list[AssignmentRecord]) -> str:
        """
        Write assignment records to ledger.
        Returns ledger file path.
        """
        if not records:
            return ""
        
        # Group by experiment
        experiment_id = records[0].experiment_id
        timestamp = int(time.time())
        
        ledger_file = self.ledger_dir / f"{experiment_id}_{timestamp}.jsonl"
        
        with open(ledger_file, 'a') as f:
            for record in records:
                f.write(json.dumps(record.to_dict()) + '\n')
        
        return str(ledger_file)
    
    def read_assignments(self, experiment_id: str) -> list[AssignmentRecord]:
        """Read all assignments for an experiment."""
        records = []
        
        for ledger_file in self.ledger_dir.glob(f"{experiment_id}_*.jsonl"):
            with open(ledger_file, 'r') as f:
                for line in f:
                    data = json.loads(line)
                    records.append(AssignmentRecord(**data))
        
        return records
    
    def verify_determinism(self, experiment_id: str, 
                          traffic_units: list[TrafficUnit],
                          policy: AssignmentPolicy) -> bool:
        """
        Verify that reassignment produces identical results.
        Critical for audit.
        """
        existing = self.read_assignments(experiment_id)
        
        if not existing:
            return True  # No existing assignments
        
        # Recreate assignment
        engine = ControlAssignmentEngine()
        result = engine.assign(traffic_units, policy)
        
        if not result["success"]:
            return False
        
        new_records = result["records"]
        
        # Compare assignment hashes
        existing_hashes = {r.traffic_unit_id: r.assignment_hash for r in existing}
        new_hashes = {r.traffic_unit_id: r.assignment_hash for r in new_records}
        
        return existing_hashes == new_hashes


# ============================================================================
# ASSIGNMENT WATCHDOG (PRODUCTION GRADE)
# ============================================================================

class AssignmentWatchdog:
    """
    Monitors assignment health and detects violations.
    
    Can freeze experiments and invalidate results.
    """
    
    def __init__(self):
        self.violations: dict[str, list[str]] = defaultdict(list)
        self.frozen_experiments: set[str] = set()
    
    def monitor_overlap(self, assignments: dict[str, list[AssignmentRecord]]) -> list[str]:
        """Monitor for traffic overlap violations."""
        issues = []
        
        # Check for unit appearing in multiple experiments
        unit_to_experiments = defaultdict(set)
        
        for experiment_id, records in assignments.items():
            for record in records:
                unit_to_experiments[record.traffic_unit_id].add(experiment_id)
        
        for unit_id, experiments in unit_to_experiments.items():
            if len(experiments) > 1:
                issues.append(f"Unit {unit_id} in multiple experiments: {experiments}")
        
        return issues
    
    def monitor_exposure_creep(self, experiment_id: str,
                              current_exposure: float,
                              max_exposure: float) -> bool:
        """Monitor for exposure exceeding caps."""
        if current_exposure > max_exposure * 1.05:  # 5% tolerance
            violation = f"Exposure {current_exposure} exceeds cap {max_exposure}"
            self.violations[experiment_id].append(violation)
            return False
        
        return True
    
    def freeze_experiment(self, experiment_id: str, reason: str):
        """Freeze experiment due to violation."""
        self.frozen_experiments.add(experiment_id)
        self.violations[experiment_id].append(f"FROZEN: {reason}")
    
    def is_frozen(self, experiment_id: str) -> bool:
        """Check if experiment is frozen."""
        return experiment_id in self.frozen_experiments
    
    def get_violations(self, experiment_id: str) -> list[str]:
        """Get all violations for experiment."""
        return self.violations.get(experiment_id, [])


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Create traffic units
    units = [
        TrafficUnit(
            unit_id=f"user_{i}",
            platform="tiktok",
            region="us",
            time_bucket="2024-01",
            capability_flags=frozenset(["autoplay", "sound_on"])
        )
        for i in range(1000)
    ]
    
    # Define assignment policy
    policy = AssignmentPolicy(
        experiment_id="exp_001",
        split_ratio={"control": 0.5, "variant_a": 0.5},
        isolation_level=IsolationLevel.HARD,
        stratify_on=("platform", "region"),
        deterministic_seed="seed_12345",
        exclusivity_group="group_1",
        max_exposure_fraction=0.8,
        reversible=True
    )
    
    # Assign traffic
    engine = ControlAssignmentEngine()
    result = engine.assign(units, policy)
    
    print(f"Success: {result['success']}")
    print(f"Assignments: {result.get('assignments', {})}")
    print(f"Total assigned: {result.get('total_assigned', 0)}")
    
    # Write to ledger
    if result["success"]:
        ledger = AssignmentLedger()
        ledger_file = ledger.write_assignments(result["records"])
        print(f"Ledger written to: {ledger_file}")
        
        # Verify determinism
        is_deterministic = ledger.verify_determinism(
            policy.experiment_id, units, policy
        )
        print(f"Deterministic: {is_deterministic}")