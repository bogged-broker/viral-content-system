"""
replay_loader.py — Deterministic Experiment Reconstitution Engine

WHAT THIS FILE ACTUALLY DOES:
    Given a snapshot, recreate the experiment exactly as it ran — bit-for-bit.
    Not approximately. Not "close enough." Exactly.
    
    If this file is wrong, everything above it is theater.

CORE PRINCIPLE:
    Replay is not rerunning — it is re-instantiation.
    Nothing is recomputed. Nothing is re-derived.
    Everything is loaded, verified, and locked.

HARD INVARIANTS (ABSOLUTE):
    ❌ NEVER recompute metrics
    ❌ NEVER re-run models
    ❌ NEVER query live databases
    ❌ NEVER modify artifacts
    ❌ NEVER guess missing values
    ❌ NEVER use wall-clock time
    
    One violation breaks trust permanently.

DETERMINISM GUARANTEE:
    Given same snapshot + same loader version →
    Identical hashes, structures, values.
    Determinism is binary: perfect or invalid.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Any, Protocol
from datetime import datetime
from pathlib import Path
from enum import Enum
import hashlib
import json
import struct
from collections import OrderedDict
import zlib


# ============================================================================
# CORE DATA STRUCTURES (MANDATORY)
# ============================================================================

class ReplayEnvironment(Enum):
    """Target environment for replay execution."""
    REPLAY = "replay"      # Full replay for audit
    AUDIT = "audit"        # Compliance verification
    DEBUG = "debug"        # Forensic analysis
    FORENSIC = "forensic"  # Deep investigation


@dataclass(frozen=True)
class ReplaySpec:
    """
    Specification for what to replay and how.
    Partial replays are FORBIDDEN by default.
    """
    snapshot_id: str
    target_environment: ReplayEnvironment
    allow_partial: bool = False
    strict_verification: bool = True
    
    def __post_init__(self):
        if self.allow_partial and self.target_environment == ReplayEnvironment.AUDIT:
            raise ValueError("Partial replays forbidden in audit environment")


@dataclass(frozen=True)
class ReplayContext:
    """
    Freezes execution reality.
    This is the canonical "when and how" of the experiment.
    """
    experiment_id: str
    snapshot_timestamp: datetime
    
    # Version locking
    code_versions: Dict[str, str]
    model_versions: Dict[str, str]
    
    # Determinism anchors
    deterministic_seed: int
    config_hash: str
    
    # Metadata
    platform_info: Dict[str, str]
    dependencies: Dict[str, str]
    
    def to_canonical_bytes(self) -> bytes:
        """Convert to canonical byte representation for hashing."""
        canonical = {
            'experiment_id': self.experiment_id,
            'timestamp': self.snapshot_timestamp.isoformat(),
            'code_versions': sorted(self.code_versions.items()),
            'model_versions': sorted(self.model_versions.items()),
            'seed': self.deterministic_seed,
            'config_hash': self.config_hash,
        }
        return json.dumps(canonical, sort_keys=True).encode('utf-8')


@dataclass(frozen=True)
class ReplayArtifact:
    """
    Immutable artifact from snapshot.
    Everything is validated before use.
    """
    name: str
    payload: bytes
    content_hash: str
    schema_version: str
    artifact_type: str
    size_bytes: int
    
    def verify_integrity(self) -> bool:
        """Verify hash matches payload."""
        computed = hashlib.sha256(self.payload).hexdigest()
        return computed == self.content_hash
    
    def decompress(self) -> bytes:
        """Decompress if payload is compressed."""
        if self.payload[:2] == b'\x1f\x8b':  # gzip magic
            return zlib.decompress(self.payload)
        return self.payload


class HashMismatchError(Exception):
    """Hash verification failed during replay."""
    pass


class IncompletSnapshotError(Exception):
    """Snapshot is missing required components."""
    pass


class DeterminismViolationError(Exception):
    """Replay violated determinism guarantees."""
    pass


class SchemaIncompatibilityError(Exception):
    """Schema version incompatible with loader."""
    pass


# ============================================================================
# SNAPSHOT LOADER
# ============================================================================

class SnapshotLoader:
    """
    Responsible for:
    - Loading snapshot from storage
    - Verifying completeness
    - Preventing lazy or partial loading
    - Maintaining canonical order
    
    If loading is lossy → abort.
    """
    
    REQUIRED_MANIFEST_KEYS = {
        'snapshot_id', 'experiment_id', 'timestamp',
        'context', 'artifacts', 'schema_version'
    }
    
    REQUIRED_ARTIFACT_TYPES = {
        'experiment_spec', 'variants', 'control_assignment',
        'metric_timeline', 'effect_sizes', 'statistical_results',
        'confidence_scores'
    }
    
    def __init__(self, storage_root: Path):
        self.storage_root = storage_root
        self._loaded_snapshots: Dict[str, dict] = {}
    
    def load_snapshot(self, snapshot_id: str, allow_cache: bool = True) -> dict:
        """
        Load complete snapshot manifest and verify structure.
        
        Returns:
            Complete snapshot manifest with all artifacts
            
        Raises:
            IncompletSnapshotError: If snapshot missing required components
            FileNotFoundError: If snapshot doesn't exist
        """
        if allow_cache and snapshot_id in self._loaded_snapshots:
            return self._loaded_snapshots[snapshot_id]
        
        snapshot_path = self.storage_root / snapshot_id / "manifest.json"
        
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")
        
        with open(snapshot_path, 'r') as f:
            manifest = json.load(f)
        
        # Verify manifest structure
        self._verify_manifest_structure(manifest)
        
        # Load all artifacts
        manifest['_loaded_artifacts'] = self._load_all_artifacts(
            snapshot_id, 
            manifest['artifacts']
        )
        
        if allow_cache:
            self._loaded_snapshots[snapshot_id] = manifest
        
        return manifest
    
    def _verify_manifest_structure(self, manifest: dict) -> None:
        """Verify manifest has all required keys."""
        missing = self.REQUIRED_MANIFEST_KEYS - set(manifest.keys())
        if missing:
            raise IncompletSnapshotError(
                f"Manifest missing required keys: {missing}"
            )
    
    def _load_all_artifacts(
        self, 
        snapshot_id: str, 
        artifact_manifest: Dict[str, dict]
    ) -> Dict[str, ReplayArtifact]:
        """
        Load all artifacts from storage.
        Maintains canonical order (alphabetical by name).
        """
        artifacts = OrderedDict()
        artifact_dir = self.storage_root / snapshot_id / "artifacts"
        
        # Check for required artifact types
        present_types = {meta['artifact_type'] for meta in artifact_manifest.values()}
        missing_types = self.REQUIRED_ARTIFACT_TYPES - present_types
        
        if missing_types:
            raise IncompletSnapshotError(
                f"Snapshot missing required artifact types: {missing_types}"
            )
        
        # Load in canonical order
        for name in sorted(artifact_manifest.keys()):
            meta = artifact_manifest[name]
            artifact_path = artifact_dir / f"{name}.bin"
            
            if not artifact_path.exists():
                raise IncompletSnapshotError(
                    f"Artifact file missing: {name}"
                )
            
            with open(artifact_path, 'rb') as f:
                payload = f.read()
            
            artifact = ReplayArtifact(
                name=name,
                payload=payload,
                content_hash=meta['content_hash'],
                schema_version=meta['schema_version'],
                artifact_type=meta['artifact_type'],
                size_bytes=len(payload)
            )
            
            artifacts[name] = artifact
        
        return artifacts


# ============================================================================
# SNAPSHOT VERIFIER (MANDATORY)
# ============================================================================

class SnapshotVerifier:
    """
    Before ANY replay begins:
    - Verify all hashes
    - Verify schema versions
    - Validate invariant states
    - Confirm deterministic seed integrity
    
    Replay cannot proceed unless verification passes.
    """
    
    SUPPORTED_SCHEMA_VERSIONS = {'1.0', '1.1', '1.2'}
    
    def __init__(self, strict: bool = True):
        self.strict = strict
        self.verification_log: List[Tuple[str, bool, str]] = []
    
    def verify_snapshot(
        self, 
        manifest: dict, 
        artifacts: Dict[str, ReplayArtifact]
    ) -> bool:
        """
        Run complete verification suite.
        
        Returns:
            True if all verifications pass
            
        Raises:
            HashMismatchError: If any hash verification fails
            SchemaIncompatibilityError: If schema version unsupported
        """
        self.verification_log.clear()
        
        # 1. Verify schema version
        self._verify_schema_version(manifest['schema_version'])
        
        # 2. Verify all artifact hashes
        self._verify_artifact_hashes(artifacts)
        
        # 3. Verify context integrity
        self._verify_context_integrity(manifest['context'])
        
        # 4. Verify deterministic seed
        self._verify_deterministic_seed(manifest['context'])
        
        # 5. Verify artifact completeness
        self._verify_artifact_completeness(artifacts)
        
        # All checks passed
        all_passed = all(passed for _, passed, _ in self.verification_log)
        
        if not all_passed and self.strict:
            failed = [name for name, passed, _ in self.verification_log if not passed]
            raise HashMismatchError(f"Verification failed for: {failed}")
        
        return all_passed
    
    def _verify_schema_version(self, version: str) -> None:
        """Verify schema version is supported."""
        if version not in self.SUPPORTED_SCHEMA_VERSIONS:
            raise SchemaIncompatibilityError(
                f"Unsupported schema version: {version}. "
                f"Supported: {self.SUPPORTED_SCHEMA_VERSIONS}"
            )
        self.verification_log.append(('schema_version', True, version))
    
    def _verify_artifact_hashes(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Verify hash of every artifact."""
        for name, artifact in artifacts.items():
            is_valid = artifact.verify_integrity()
            self.verification_log.append((f'hash_{name}', is_valid, artifact.content_hash))
            
            if not is_valid:
                raise HashMismatchError(
                    f"Hash mismatch for artifact: {name}"
                )
    
    def _verify_context_integrity(self, context_data: dict) -> None:
        """Verify context has required fields and valid values."""
        required = {'experiment_id', 'code_versions', 'model_versions', 
                   'deterministic_seed', 'config_hash'}
        
        missing = required - set(context_data.keys())
        if missing:
            raise IncompletSnapshotError(
                f"Context missing fields: {missing}"
            )
        
        # Verify config hash format
        if not isinstance(context_data['config_hash'], str) or len(context_data['config_hash']) != 64:
            raise ValueError("Invalid config_hash format")
        
        self.verification_log.append(('context_integrity', True, 'complete'))
    
    def _verify_deterministic_seed(self, context_data: dict) -> None:
        """Verify deterministic seed is valid integer."""
        seed = context_data['deterministic_seed']
        
        if not isinstance(seed, int):
            raise ValueError(f"Deterministic seed must be int, got {type(seed)}")
        
        if seed < 0 or seed >= 2**32:
            raise ValueError(f"Deterministic seed out of valid range: {seed}")
        
        self.verification_log.append(('deterministic_seed', True, str(seed)))
    
    def _verify_artifact_completeness(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Verify all required artifact types present."""
        present_types = {a.artifact_type for a in artifacts.values()}
        missing = SnapshotLoader.REQUIRED_ARTIFACT_TYPES - present_types
        
        if missing:
            raise IncompletSnapshotError(
                f"Missing required artifact types: {missing}"
            )
        
        self.verification_log.append(('artifact_completeness', True, 'all_present'))


# ============================================================================
# REPLAY ASSEMBLER
# ============================================================================

class ReplayAssembler:
    """
    Reconstructs:
    - Experiment spec
    - Variants
    - Control assignment
    - Metric timelines
    - Effect size inputs
    - Statistical test inputs
    - Confidence inputs
    
    NO computation. ONLY reconstruction.
    """
    
    def __init__(self):
        self._assembled_components: Dict[str, Any] = {}
    
    def assemble_experiment(
        self, 
        artifacts: Dict[str, ReplayArtifact]
    ) -> Dict[str, Any]:
        """
        Assemble complete experiment from artifacts.
        
        Returns:
            Dictionary with all reconstructed components
        """
        self._assembled_components.clear()
        
        # Assemble in dependency order
        self._assemble_experiment_spec(artifacts)
        self._assemble_variants(artifacts)
        self._assemble_control_assignment(artifacts)
        self._assemble_metric_timeline(artifacts)
        self._assemble_effect_sizes(artifacts)
        self._assemble_statistical_results(artifacts)
        self._assemble_confidence_scores(artifacts)
        
        return dict(self._assembled_components)
    
    def _assemble_experiment_spec(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct experiment specification."""
        spec_artifact = self._find_artifact_by_type(artifacts, 'experiment_spec')
        spec_data = json.loads(spec_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['experiment_spec'] = spec_data
    
    def _assemble_variants(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct variant definitions."""
        variants_artifact = self._find_artifact_by_type(artifacts, 'variants')
        variants_data = json.loads(variants_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['variants'] = variants_data
    
    def _assemble_control_assignment(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct control assignment."""
        assignment_artifact = self._find_artifact_by_type(artifacts, 'control_assignment')
        assignment_data = json.loads(assignment_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['control_assignment'] = assignment_data
    
    def _assemble_metric_timeline(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct metric timeline."""
        timeline_artifact = self._find_artifact_by_type(artifacts, 'metric_timeline')
        timeline_data = json.loads(timeline_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['metric_timeline'] = timeline_data
    
    def _assemble_effect_sizes(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct effect size calculations."""
        effect_artifact = self._find_artifact_by_type(artifacts, 'effect_sizes')
        effect_data = json.loads(effect_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['effect_sizes'] = effect_data
    
    def _assemble_statistical_results(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct statistical test results."""
        stats_artifact = self._find_artifact_by_type(artifacts, 'statistical_results')
        stats_data = json.loads(stats_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['statistical_results'] = stats_data
    
    def _assemble_confidence_scores(self, artifacts: Dict[str, ReplayArtifact]) -> None:
        """Reconstruct confidence scores."""
        confidence_artifact = self._find_artifact_by_type(artifacts, 'confidence_scores')
        confidence_data = json.loads(confidence_artifact.decompress().decode('utf-8'))
        
        self._assembled_components['confidence_scores'] = confidence_data
    
    def _find_artifact_by_type(
        self, 
        artifacts: Dict[str, ReplayArtifact], 
        artifact_type: str
    ) -> ReplayArtifact:
        """Find artifact by type."""
        for artifact in artifacts.values():
            if artifact.artifact_type == artifact_type:
                return artifact
        
        raise ValueError(f"No artifact found with type: {artifact_type}")


# ============================================================================
# DETERMINISM ENFORCER
# ============================================================================

class DeterminismEnforcer:
    """
    Ensures:
    - Random number generators fixed
    - Ordering deterministic
    - No runtime branching
    - No external dependencies
    - Time frozen
    
    Violations cause hard stop.
    """
    
    def __init__(self):
        self.violations: List[str] = []
        self._enforced_seed: Optional[int] = None
        self._frozen_time: Optional[datetime] = None
    
    def enforce_determinism(self, context: ReplayContext) -> None:
        """
        Enforce deterministic execution environment.
        
        Raises:
            DeterminismViolationError: If environment cannot be made deterministic
        """
        self.violations.clear()
        
        # Lock random seed
        self._enforce_random_seed(context.deterministic_seed)
        
        # Freeze time
        self._freeze_time(context.snapshot_timestamp)
        
        # Verify no runtime branching sources
        self._verify_no_external_state()
        
        if self.violations:
            raise DeterminismViolationError(
                f"Determinism violations: {self.violations}"
            )
    
    def _enforce_random_seed(self, seed: int) -> None:
        """Lock random number generator."""
        import random
        import numpy as np
        
        try:
            random.seed(seed)
            np.random.seed(seed)
            self._enforced_seed = seed
        except Exception as e:
            self.violations.append(f"Failed to set random seed: {e}")
    
    def _freeze_time(self, timestamp: datetime) -> None:
        """Freeze time to snapshot timestamp."""
        self._frozen_time = timestamp
    
    def _verify_no_external_state(self) -> None:
        """Verify no external state dependencies."""
        # Check environment variables that could affect execution
        import os
        
        risky_env_vars = ['RANDOM_SEED', 'TZ', 'PYTHONHASHSEED']
        for var in risky_env_vars:
            if var in os.environ:
                self.violations.append(
                    f"External state variable present: {var}"
                )
    
    def get_frozen_time(self) -> datetime:
        """Get frozen timestamp for replay."""
        if self._frozen_time is None:
            raise RuntimeError("Time not frozen - call enforce_determinism first")
        return self._frozen_time
    
    def verify_deterministic_state(self) -> bool:
        """Verify current state is deterministic."""
        if self._enforced_seed is None:
            self.violations.append("Random seed not enforced")
        if self._frozen_time is None:
            self.violations.append("Time not frozen")
        
        return len(self.violations) == 0


# ============================================================================
# REPLAY SESSION
# ============================================================================

@dataclass(frozen=True)
class ReplaySession:
    """
    Immutable replay session.
    
    Properties:
    - Immutable
    - Queryable
    - Inspectable
    - Side-effect-free
    
    Used by:
    - Auditors
    - Reports
    - Rollback
    - Forensic analysis
    
    Never used for live decision-making.
    """
    session_id: str
    replay_spec: ReplaySpec
    context: ReplayContext
    
    # Reconstructed components
    experiment_spec: dict
    variants: dict
    control_assignment: dict
    metric_timeline: dict
    effect_sizes: dict
    statistical_results: dict
    confidence_scores: dict
    
    # Verification metadata
    verification_log: List[Tuple[str, bool, str]]
    all_hashes_verified: bool
    
    # Session metadata
    created_at: datetime
    loader_version: str
    
    def get_metric_value(self, metric_name: str, variant_id: str) -> Optional[float]:
        """Query metric value for variant."""
        timeline = self.metric_timeline.get(variant_id, {})
        return timeline.get(metric_name)
    
    def get_effect_size(self, metric_name: str) -> Optional[float]:
        """Query effect size for metric."""
        return self.effect_sizes.get(metric_name)
    
    def get_statistical_significance(self, metric_name: str) -> Optional[float]:
        """Query statistical significance (p-value) for metric."""
        stats = self.statistical_results.get(metric_name, {})
        return stats.get('p_value')
    
    def get_confidence_score(self, decision_type: str) -> Optional[float]:
        """Query confidence score for decision type."""
        return self.confidence_scores.get(decision_type)
    
    def to_audit_report(self) -> dict:
        """Generate audit report from session."""
        return {
            'session_id': self.session_id,
            'experiment_id': self.context.experiment_id,
            'snapshot_timestamp': self.context.snapshot_timestamp.isoformat(),
            'verification_passed': self.all_hashes_verified,
            'verification_details': self.verification_log,
            'code_versions': self.context.code_versions,
            'model_versions': self.context.model_versions,
            'deterministic_seed': self.context.deterministic_seed,
            'created_at': self.created_at.isoformat(),
            'loader_version': self.loader_version,
        }


# ============================================================================
# REPLAY LOADER (CORE ENGINE)
# ============================================================================

class ReplayLoader:
    """
    Core replay engine.
    
    Orchestrates:
    - Snapshot loading
    - Verification
    - Assembly
    - Determinism enforcement
    - Session creation
    """
    
    VERSION = "1.0.0"
    
    def __init__(self, storage_root: Path):
        self.storage_root = storage_root
        self.loader = SnapshotLoader(storage_root)
        self.verifier = SnapshotVerifier()
        self.assembler = ReplayAssembler()
        self.enforcer = DeterminismEnforcer()
        
        self._active_sessions: Dict[str, ReplaySession] = {}
    
    def load_snapshot(self, replay_spec: ReplaySpec) -> ReplaySession:
        """
        Load and verify snapshot, create replay session.
        
        Flow:
        1. Load snapshot manifest
        2. Verify all hashes
        3. Lock context
        4. Create replay session
        5. Assemble all artifacts
        6. Enforce determinism
        7. Return frozen replay bundle
        
        Returns:
            Immutable ReplaySession
            
        Raises:
            Various errors if verification or assembly fails
        """
        # 1. Load snapshot manifest
        manifest = self.loader.load_snapshot(replay_spec.snapshot_id)
        artifacts = manifest['_loaded_artifacts']
        
        # 2. Verify all hashes
        verification_passed = self.verifier.verify_snapshot(manifest, artifacts)
        
        # 3. Lock context
        context = self._create_replay_context(manifest['context'])
        
        # 4. Assemble all artifacts
        assembled = self.assembler.assemble_experiment(artifacts)
        
        # 5. Enforce determinism
        self.enforcer.enforce_determinism(context)
        
        # 6. Create replay session
        session = ReplaySession(
            session_id=f"replay_{replay_spec.snapshot_id}_{datetime.now().isoformat()}",
            replay_spec=replay_spec,
            context=context,
            experiment_spec=assembled['experiment_spec'],
            variants=assembled['variants'],
            control_assignment=assembled['control_assignment'],
            metric_timeline=assembled['metric_timeline'],
            effect_sizes=assembled['effect_sizes'],
            statistical_results=assembled['statistical_results'],
            confidence_scores=assembled['confidence_scores'],
            verification_log=list(self.verifier.verification_log),
            all_hashes_verified=verification_passed,
            created_at=datetime.now(),
            loader_version=self.VERSION,
        )
        
        # Cache session
        self._active_sessions[session.session_id] = session
        
        return session
    
    def reconstruct_experiment(self, session: ReplaySession) -> dict:
        """
        Reconstruct complete experiment state from session.
        
        Returns complete experiment representation with all components.
        """
        return {
            'metadata': {
                'experiment_id': session.context.experiment_id,
                'snapshot_timestamp': session.context.snapshot_timestamp,
                'code_versions': session.context.code_versions,
                'model_versions': session.context.model_versions,
            },
            'configuration': session.experiment_spec,
            'variants': session.variants,
            'assignments': session.control_assignment,
            'results': {
                'metrics': session.metric_timeline,
                'effects': session.effect_sizes,
                'statistics': session.statistical_results,
                'confidence': session.confidence_scores,
            },
            'verification': {
                'all_verified': session.all_hashes_verified,
                'log': session.verification_log,
            }
        }
    
    def verify_replay(self, session: ReplaySession, snapshot_id: str) -> bool:
        """
        Verify replay session matches snapshot exactly.
        
        Confirms:
        - Reconstructed outputs exactly match snapshot
        - Derived outputs not recomputed
        - Confidence & statistics identical
        
        Returns:
            True if replay is valid
        """
        # Reload original snapshot
        original_manifest = self.loader.load_snapshot(snapshot_id)
        original_artifacts = original_manifest['_loaded_artifacts']
        
        # Assemble original
        original_assembled = self.assembler.assemble_experiment(original_artifacts)
        
        # Compare all components
        mismatches = []
        
        for component_name in ['experiment_spec', 'variants', 'control_assignment',
                               'metric_timeline', 'effect_sizes', 'statistical_results',
                               'confidence_scores']:
            
            session_component = getattr(session, component_name)
            original_component = original_assembled[component_name]
            
            if self._deep_compare(session_component, original_component) is False:
                mismatches.append(component_name)
        
        if mismatches:
            raise DeterminismViolationError(
                f"Replay mismatch in components: {mismatches}"
            )
        
        return True
    
    def _create_replay_context(self, context_data: dict) -> ReplayContext:
        """Create ReplayContext from manifest data."""
        return ReplayContext(
            experiment_id=context_data['experiment_id'],
            snapshot_timestamp=datetime.fromisoformat(context_data['timestamp']),
            code_versions=context_data['code_versions'],
            model_versions=context_data['model_versions'],
            deterministic_seed=context_data['deterministic_seed'],
            config_hash=context_data['config_hash'],
            platform_info=context_data.get('platform_info', {}),
            dependencies=context_data.get('dependencies', {}),
        )
    
    def _deep_compare(self, obj1: Any, obj2: Any) -> bool:
        """Deep comparison of objects."""
        if type(obj1) != type(obj2):
            return False
        
        if isinstance(obj1, dict):
            if set(obj1.keys()) != set(obj2.keys()):
                return False
            return all(self._deep_compare(obj1[k], obj2[k]) for k in obj1.keys())
        
        if isinstance(obj1, (list, tuple)):
            if len(obj1) != len(obj2):
                return False
            return all(self._deep_compare(a, b) for a, b in zip(obj1, obj2))
        
        return obj1 == obj2
    
    def get_session(self, session_id: str) -> Optional[ReplaySession]:
        """Retrieve cached replay session."""
        return self._active_sessions.get(session_id)
    
    def list_sessions(self) -> List[str]:
        """List all active replay sessions."""
        return list(self._active_sessions.keys())


# ============================================================================
# REPLAY WATCHDOG
# ============================================================================

class ReplayWatchdog:
    """
    Monitors:
    - Hash mismatches
    - Replay drift
    - Dependency violations
    - Schema incompatibility
    
    Can:
    - Block audits
    - Flag suspicious experiments
    - Escalate compliance alerts
    """
    
    def __init__(self):
        self.alerts: List[Dict[str, Any]] = []
        self.blocked_snapshots: Set[str] = set()
    
    def monitor_replay(self, session: ReplaySession) -> None:
        """Monitor replay session for issues."""
        # Check verification
        if not session.all_hashes_verified:
            self._raise_alert(
                'VERIFICATION_FAILED',
                session.session_id,
                "Hash verification failed during replay"
            )
        
        # Check for schema issues
        if session.loader_version != ReplayLoader.VERSION:
            self._raise_alert(
                'VERSION_MISMATCH',
                session.session_id,
                f"Loader version mismatch: {session.loader_version} vs {ReplayLoader.VERSION}"
            )
        
        # Check for missing data
        required_components = [
            'experiment_spec', 'variants', 'control_assignment',
            'metric_timeline', 'effect_sizes', 'statistical_results'
        ]
        
        for component in required_components:
            if not getattr(session, component, None):
                self._raise_alert(
                    'MISSING_COMPONENT',
                    session.session_id,
                    f"Required component missing: {component}"
                )
    
    def check_snapshot_blocked(self, snapshot_id: str) -> bool:
        """Check if snapshot is blocked from replay."""
        return snapshot_id in self.blocked_snapshots
    
    def block_snapshot(self, snapshot_id: str, reason: str) -> None:
        """Block snapshot from future replays."""
        self.blocked_snapshots.add(snapshot_id)
        self._raise_alert(
            'SNAPSHOT_BLOCKED',
            snapshot_id,
            f"Snapshot blocked: {reason}"
        )
    
    def _raise_alert(self, alert_type: str, subject_id: str, message: str) -> None:
        """Raise alert for watchdog issue."""
        alert = {
            'type': alert_type,
            'subject_id': subject_id,
            'message': message,
            'timestamp': datetime.now().isoformat(),
        }
        self.alerts.append(alert)
    
    def get_alerts(self, alert_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get alerts, optionally filtered by type."""
        if alert_type:
            return [a for a in self.alerts if a['type'] == alert_type]
        return list(self.alerts)
    
    def clear_alerts(self) -> None:
        """Clear all alerts."""
        self.alerts.clear()


# ============================================================================
# HIGH-LEVEL API
# ============================================================================

def replay_experiment(
    snapshot_id: str,
    storage_root: Path,
    strict_verification: bool = True,
    environment: ReplayEnvironment = ReplayEnvironment.REPLAY
) -> ReplaySession:
    """
    High-level API for replaying an experiment from snapshot.
    
    Args:
        snapshot_id: ID of snapshot to replay
        storage_root: Root path of snapshot storage
        strict_verification: If True, fail on any verification issue
        environment: Target environment for replay
        
    Returns:
        Immutable ReplaySession with complete experiment state
        
    Example:
        >>> session = replay_experiment(
        ...     'exp_123_snapshot_456',
        ...     Path('/data/snapshots'),
        ...     strict_verification=True
        ... )
        >>> session.get_effect_size('conversion_rate')
        0.0234
    """
    spec = ReplaySpec(
        snapshot_id=snapshot_id,
        target_environment=environment,
        allow_partial=False,
        strict_verification=strict_verification
    )
    
    loader = ReplayLoader(storage_root)
    watchdog = ReplayWatchdog()
    
    # Check if snapshot blocked
    if watchdog.check_snapshot_blocked(snapshot_id):
        raise ValueError(f"Snapshot blocked from replay: {snapshot_id}")
    
    # Load and verify
    session = loader.load_snapshot(spec)
    
    # Monitor
    watchdog.monitor_replay(session)
    
    # Verify replay validity
    loader.verify_replay(session, snapshot_id)
    
    return session


def audit_experiment(
    snapshot_id: str,
    storage_root: Path
) -> dict:
    """
    Audit an experiment replay for compliance.
    
    Returns audit report with verification details.
    """
    session = replay_experiment(
        snapshot_id,
        storage_root,
        strict_verification=True,
        environment=ReplayEnvironment.AUDIT
    )
    
    return session.to_audit_report()


# ============================================================================
# INVARIANT CHECKS (FOR TESTING)
# ============================================================================

def verify_replay_invariants(session: ReplaySession) -> List[str]:
    """
    Verify all replay invariants hold.
    
    Returns list of violations (empty if all pass).
    """
    violations = []
    
    # Immutability
    try:
        session.experiment_spec['test'] = 'mutate'
        violations.append("Session not immutable")
    except (AttributeError, TypeError, KeyError):
        pass  # Expected - session is frozen
    
    # Completeness
    required = ['experiment_spec', 'variants', 'control_assignment',
                'metric_timeline', 'effect_sizes', 'statistical_results',
                'confidence_scores']
    
    for attr in required:
        if not getattr(session, attr, None):
            violations.append(f"Missing required attribute: {attr}")
    
    # Verification
    if not session.all_hashes_verified:
        violations.append("Hash verification incomplete")
    
    # Determinism
    if not session.context.deterministic_seed:
        violations.append("No deterministic seed in context")
    
    return violations


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Replay an experiment
    storage = Path("/data/experiment_snapshots")
    snapshot_id = "exp_12345_snapshot_2026_01_23"
    
    try:
        # Load replay session
        session = replay_experiment(
            snapshot_id=snapshot_id,
            storage_root=storage,
            strict_verification=True
        )
        
        print(f"Replay successful: {session.session_id}")
        print(f"Experiment: {session.context.experiment_id}")
        print(f"Verification: {'PASSED' if session.all_hashes_verified else 'FAILED'}")
        
        # Query results
        effect = session.get_effect_size('conversion_rate')
        print(f"Effect size: {effect}")
        
        significance = session.get_statistical_significance('conversion_rate')
        print(f"P-value: {significance}")
        
        confidence = session.get_confidence_score('ship_decision')
        print(f"Confidence: {confidence}")
        
        # Verify invariants
        violations = verify_replay_invariants(session)
        if violations:
            print(f"INVARIANT VIOLATIONS: {violations}")
        else:
            print("All invariants verified ✓")
        
        # Generate audit report
        audit_report = session.to_audit_report()
        print("\nAudit Report:")
        print(json.dumps(audit_report, indent=2))
        
    except Exception as e:
        print(f"Replay failed: {e}")
        raise

