"""
/account_system/network_affiliation.py

Shared Infrastructure, Graph Overlap & Contagion Control Engine

This file answers: "If this account gets hit, who else is at risk — and why?"

CRITICAL PRINCIPLES:
- Trust is partially inherited — risk is contagious
- Platforms evaluate clusters, not individual accounts
- Shared infra = shared control assumption
- Strong isolation dampens contagion
- Blast radius containment is everything

WHAT THIS IS:
✓ Passive observational modeling
✓ Cluster risk propagation
✓ Trust isolation scoring
✓ Contagion boundary detection

WHAT THIS IS NOT:
✗ IP scraping
✗ Fingerprinting users
✗ Evasion logic
✗ Identity spoofing
✗ Deanonymization

Consumed by: trust_scoring, posting governors, rollout managers, experiment isolation
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Set, FrozenSet
from enum import Enum
import hashlib
import json
import math
from collections import defaultdict, Counter


# ============================================================================
# CORE DATA STRUCTURES
# ============================================================================

class AffiliationType(Enum):
    """Types of account affiliations (abstracted, passive only)"""
    INFRASTRUCTURE = "infrastructure"      # hosting ASN, cloud region bucket
    DEVICE_CLASS = "device_class"          # OS family, browser family
    TIMING_PATTERN = "timing_pattern"      # synchronized posting patterns
    CONTENT_OPS = "content_ops"            # shared formats at scale
    NETWORK_GRAPH = "network_graph"        # mentions, repost paths
    OPS_ENVELOPE = "ops_envelope"          # rate similarity, cadence


@dataclass(frozen=True)
class AffiliationSignal:
    """
    Single passive signal of potential affiliation
    
    CRITICAL: Never contains raw identifiers
    Everything is bucketed/abstracted
    """
    signal_type: AffiliationType
    abstract_bucket: str          # e.g., "cloud_provider_A_region_2"
    confidence: float             # 0.0-1.0, signal quality
    timestamp: datetime
    metadata: Dict = field(default_factory=dict)
    
    def __post_init__(self):
        assert 0.0 <= self.confidence <= 1.0
        # Enforce no raw identifiers in bucket
        forbidden = ['ip=', 'device_id=', 'user_id=', 'mac=']
        assert not any(f in self.abstract_bucket.lower() for f in forbidden), \
            "Raw identifiers forbidden in affiliation signals"


@dataclass(frozen=True)
class AffiliationCluster:
    """
    Group of accounts sharing meaningful infrastructure/behavior patterns
    
    This is what downstream systems reason over
    """
    cluster_id: str
    relationship_type: AffiliationType
    member_accounts: FrozenSet[str]       # immutable set
    
    # Strength metrics
    cluster_strength: float               # 0.0-1.0, consistency over time
    shared_risk_score: float              # 0.0-1.0, contagion vulnerability
    trust_alignment: float                # 0.0-1.0, behavioral coherence
    
    # Temporal metadata
    formation_timestamp: datetime
    last_update: datetime
    signal_count: int
    
    def __post_init__(self):
        assert 0.0 <= self.cluster_strength <= 1.0
        assert 0.0 <= self.shared_risk_score <= 1.0
        assert 0.0 <= self.trust_alignment <= 1.0
        assert len(self.member_accounts) >= 2


@dataclass(frozen=True)
class NetworkAffiliationProfile:
    """Complete network affiliation assessment for an account"""
    account_id: str
    platform: str
    timestamp: datetime
    
    # Cluster memberships
    affiliation_clusters: Tuple[AffiliationCluster, ...]
    
    # Risk metrics
    contagion_risk: float                 # 0.0-1.0, blast radius exposure
    isolation_score: float                # 0.0-1.0, independence strength
    cluster_density: float                # avg connections per cluster
    
    # Graph position
    centrality_score: float               # how "central" in risk graphs
    boundary_distance: float              # proximity to high-risk clusters
    
    # Metadata
    network_model_version: str
    signal_hash: str
    
    def __post_init__(self):
        assert 0.0 <= self.contagion_risk <= 1.0
        assert 0.0 <= self.isolation_score <= 1.0
        assert 0.0 <= self.cluster_density <= 100.0
        assert 0.0 <= self.centrality_score <= 1.0
        assert 0.0 <= self.boundary_distance <= 1.0


# ============================================================================
# AFFILIATION GRAPH BUILDER
# ============================================================================

class AffiliationGraphBuilder:
    """
    Constructs account-to-account relationship graphs from passive signals
    
    CRITICAL: All signals are abstracted/bucketed
    No raw identifiers ever stored
    """
    
    def __init__(self):
        # account_id -> list of signals
        self.signals: Dict[str, List[AffiliationSignal]] = defaultdict(list)
        
        # (type, bucket) -> set of account_ids
        self.bucket_membership: Dict[Tuple[AffiliationType, str], Set[str]] = \
            defaultdict(set)
        
        # Temporal windows for pattern detection
        self.time_window_short = timedelta(hours=6)
        self.time_window_long = timedelta(days=7)
    
    def add_signal(self, account_id: str, signal: AffiliationSignal):
        """Register a passive affiliation signal"""
        self.signals[account_id].append(signal)
        self.bucket_membership[(signal.signal_type, signal.abstract_bucket)].add(account_id)
    
    def build_clusters(self, min_strength: float = 0.3) -> List[AffiliationCluster]:
        """
        Build affiliation clusters from accumulated signals
        
        Strength computed from:
        - Consistency over time
        - Signal diversity
        - Co-movement stability
        - Absence of noise
        """
        clusters = []
        now = datetime.utcnow()
        
        for (aff_type, bucket), members in self.bucket_membership.items():
            if len(members) < 2:
                continue
            
            # Compute cluster strength
            strength = self._compute_cluster_strength(members, aff_type, bucket, now)
            
            if strength < min_strength:
                continue
            
            # Compute shared risk
            shared_risk = self._compute_shared_risk(members, aff_type)
            
            # Compute trust alignment
            trust_alignment = self._compute_trust_alignment(members)
            
            # Count supporting signals
            signal_count = sum(
                1 for acc in members
                for sig in self.signals[acc]
                if sig.signal_type == aff_type and sig.abstract_bucket == bucket
            )
            
            # Find formation time (earliest signal)
            formation_ts = min(
                sig.timestamp
                for acc in members
                for sig in self.signals[acc]
                if sig.signal_type == aff_type and sig.abstract_bucket == bucket
            )
            
            cluster = AffiliationCluster(
                cluster_id=self._generate_cluster_id(aff_type, bucket),
                relationship_type=aff_type,
                member_accounts=frozenset(members),
                cluster_strength=strength,
                shared_risk_score=shared_risk,
                trust_alignment=trust_alignment,
                formation_timestamp=formation_ts,
                last_update=now,
                signal_count=signal_count
            )
            
            clusters.append(cluster)
        
        return clusters
    
    def _compute_cluster_strength(self, members: Set[str], 
                                  aff_type: AffiliationType,
                                  bucket: str, now: datetime) -> float:
        """
        Cluster strength from consistency, not just co-occurrence
        
        Strong clusters have:
        - Stable membership over time
        - Consistent signal quality
        - Low noise/churn
        """
        # Collect all signals for this cluster
        cluster_signals = [
            sig for acc in members
            for sig in self.signals[acc]
            if sig.signal_type == aff_type and sig.abstract_bucket == bucket
        ]
        
        if not cluster_signals:
            return 0.0
        
        # Temporal consistency (signals spread over time)
        timestamps = [sig.timestamp for sig in cluster_signals]
        time_span = (max(timestamps) - min(timestamps)).total_seconds()
        temporal_score = min(1.0, time_span / (7 * 24 * 3600))  # normalize to week
        
        # Signal quality (average confidence)
        quality_score = sum(sig.confidence for sig in cluster_signals) / len(cluster_signals)
        
        # Membership stability (all members actively signaling)
        active_members = len(set(
            acc for acc in members
            for sig in self.signals[acc]
            if sig.signal_type == aff_type 
            and sig.abstract_bucket == bucket
            and (now - sig.timestamp) < self.time_window_long
        ))
        stability_score = active_members / len(members)
        
        # Combined strength (geometric mean for harsh gating)
        strength = (temporal_score * quality_score * stability_score) ** (1/3)
        
        return min(1.0, strength)
    
    def _compute_shared_risk(self, members: Set[str], 
                            aff_type: AffiliationType) -> float:
        """
        Shared risk = contagion vulnerability
        
        Higher when:
        - Infrastructure type (more platform-visible)
        - Large member count (bigger blast radius)
        - Recent signals (active coupling)
        """
        # Type-based base risk
        type_risk = {
            AffiliationType.INFRASTRUCTURE: 0.8,
            AffiliationType.DEVICE_CLASS: 0.5,
            AffiliationType.TIMING_PATTERN: 0.7,
            AffiliationType.CONTENT_OPS: 0.6,
            AffiliationType.NETWORK_GRAPH: 0.4,
            AffiliationType.OPS_ENVELOPE: 0.5
        }
        
        base_risk = type_risk.get(aff_type, 0.5)
        
        # Size penalty (larger clusters = more contagion)
        size_penalty = min(1.0, math.log10(len(members)) / 2)  # log scale
        
        # Recency factor (recent activity = active coupling)
        now = datetime.utcnow()
        recent_signals = sum(
            1 for acc in members
            for sig in self.signals[acc]
            if (now - sig.timestamp) < self.time_window_short
        )
        recency_factor = min(1.0, recent_signals / (len(members) * 3))
        
        shared_risk = base_risk * (0.5 + 0.5 * size_penalty) * (0.7 + 0.3 * recency_factor)
        
        return min(1.0, shared_risk)
    
    def _compute_trust_alignment(self, members: Set[str]) -> float:
        """
        Trust alignment = behavioral coherence
        
        Higher when members exhibit similar operational patterns
        (but NOT identical, which would be suspicious)
        """
        if len(members) < 2:
            return 1.0
        
        # Simple heuristic: variance in signal counts
        # Too similar = suspicious, moderate variance = natural
        signal_counts = [len(self.signals[acc]) for acc in members]
        
        if not signal_counts:
            return 0.5
        
        mean_count = sum(signal_counts) / len(signal_counts)
        variance = sum((c - mean_count) ** 2 for c in signal_counts) / len(signal_counts)
        cv = math.sqrt(variance) / (mean_count + 1e-6)  # coefficient of variation
        
        # Optimal CV around 0.3-0.5 (moderate variance)
        if cv < 0.1:  # too similar
            alignment = 0.3
        elif 0.1 <= cv <= 0.6:  # healthy variance
            alignment = 1.0
        else:  # too scattered
            alignment = 0.5
        
        return alignment
    
    def _generate_cluster_id(self, aff_type: AffiliationType, bucket: str) -> str:
        """Deterministic cluster ID generation"""
        raw = f"{aff_type.value}:{bucket}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ============================================================================
# CONTAGION MODEL
# ============================================================================

class ContagionModel:
    """
    Models how risk propagates through affiliation graphs
    
    Key insight: Strong isolation dampens contagion
    """
    
    def __init__(self, decay_factor: float = 0.7):
        """
        Args:
            decay_factor: How much risk decays per graph hop (0-1)
        """
        self.decay_factor = decay_factor
        assert 0.0 < decay_factor < 1.0
    
    def compute_contagion_risk(self, account_id: str,
                               clusters: List[AffiliationCluster],
                               account_risks: Dict[str, float]) -> float:
        """
        Compute contagion risk for an account
        
        Risk propagates from:
        - Direct cluster membership
        - Shared neighbors with high-risk accounts
        - Graph centrality
        
        But dampened by:
        - Isolation score
        - Trust alignment
        - Cluster strength (strong = contained)
        """
        # Find clusters this account belongs to
        member_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        if not member_clusters:
            return 0.0
        
        total_risk = 0.0
        risk_weights = 0.0
        
        for cluster in member_clusters:
            # Direct cluster risk
            cluster_base_risk = cluster.shared_risk_score
            
            # Neighbor risk contribution
            neighbor_risk = 0.0
            neighbor_count = 0
            
            for member in cluster.member_accounts:
                if member != account_id:
                    member_risk = account_risks.get(member, 0.0)
                    neighbor_risk += member_risk
                    neighbor_count += 1
            
            if neighbor_count > 0:
                avg_neighbor_risk = neighbor_risk / neighbor_count
            else:
                avg_neighbor_risk = 0.0
            
            # Cluster contribution (weighted by strength and alignment)
            cluster_weight = cluster.cluster_strength * cluster.trust_alignment
            cluster_contribution = (
                0.5 * cluster_base_risk + 
                0.5 * avg_neighbor_risk * self.decay_factor
            )
            
            total_risk += cluster_contribution * cluster_weight
            risk_weights += cluster_weight
        
        if risk_weights > 0:
            contagion_risk = total_risk / risk_weights
        else:
            contagion_risk = 0.0
        
        return min(1.0, contagion_risk)
    
    def propagate_risk_event(self, affected_account: str,
                            event_severity: float,
                            clusters: List[AffiliationCluster]) -> Dict[str, float]:
        """
        Simulate risk propagation from a penalty event
        
        Returns: Dict mapping account_id -> propagated_risk_increase
        """
        risk_deltas = defaultdict(float)
        
        # Find clusters containing affected account
        affected_clusters = [c for c in clusters if affected_account in c.member_accounts]
        
        for cluster in affected_clusters:
            # Risk propagates based on cluster properties
            propagation_strength = (
                cluster.shared_risk_score * 
                cluster.cluster_strength *
                (1.0 - cluster.trust_alignment * 0.3)  # high alignment reduces propagation
            )
            
            for member in cluster.member_accounts:
                if member != affected_account:
                    # Risk decays with propagation
                    propagated_risk = event_severity * propagation_strength * self.decay_factor
                    risk_deltas[member] += propagated_risk
        
        return dict(risk_deltas)


# ============================================================================
# ISOLATION SCORER
# ============================================================================

class IsolationScorer:
    """
    Quantifies account independence from risk graphs
    
    High isolation = failures don't cascade
    This is THE most underused trust lever
    """
    
    def compute_isolation_score(self, account_id: str,
                                clusters: List[AffiliationCluster]) -> float:
        """
        Isolation score from:
        - Cluster diversity (spread across types)
        - Cluster size (smaller = more isolated)
        - Temporal independence (async patterns)
        - Boundary distance (far from high-risk)
        """
        member_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        if not member_clusters:
            return 1.0  # Completely isolated
        
        # 1. Type diversity score
        cluster_types = set(c.relationship_type for c in member_clusters)
        type_diversity = len(cluster_types) / len(AffiliationType)
        
        # 2. Size independence (smaller clusters = better)
        avg_cluster_size = sum(len(c.member_accounts) for c in member_clusters) / len(member_clusters)
        size_score = max(0.0, 1.0 - math.log10(avg_cluster_size) / 3)  # penalize log scale
        
        # 3. Strength independence (weaker clusters = more isolated)
        avg_strength = sum(c.cluster_strength for c in member_clusters) / len(member_clusters)
        strength_score = 1.0 - avg_strength * 0.5  # partial penalty
        
        # 4. Risk alignment (low shared risk = isolated)
        avg_shared_risk = sum(c.shared_risk_score for c in member_clusters) / len(member_clusters)
        risk_score = 1.0 - avg_shared_risk
        
        # Combined isolation (geometric mean)
        isolation = (type_diversity * size_score * strength_score * risk_score) ** 0.25
        
        return isolation
    
    def compute_cluster_density(self, account_id: str,
                               clusters: List[AffiliationCluster]) -> float:
        """Average connections per cluster"""
        member_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        if not member_clusters:
            return 0.0
        
        total_connections = sum(len(c.member_accounts) - 1 for c in member_clusters)
        return total_connections / len(member_clusters)


# ============================================================================
# GRAPH SIMILARITY ANALYZER
# ============================================================================

class GraphSimilarityAnalyzer:
    """
    Detects suspicious co-movement and synchronized behavior
    
    Used to identify coordinated automation vs. natural affiliation
    """
    
    def compute_timing_similarity(self, account_a: str, account_b: str,
                                  signals_a: List[AffiliationSignal],
                                  signals_b: List[AffiliationSignal],
                                  window: timedelta = timedelta(minutes=5)) -> float:
        """
        Measure temporal co-occurrence of signals
        
        High similarity + infrastructure sharing = suspicious
        """
        if not signals_a or not signals_b:
            return 0.0
        
        timestamps_a = [sig.timestamp for sig in signals_a]
        timestamps_b = [sig.timestamp for sig in signals_b]
        
        # Count near-simultaneous signals
        matches = 0
        for ts_a in timestamps_a:
            for ts_b in timestamps_b:
                if abs((ts_a - ts_b).total_seconds()) < window.total_seconds():
                    matches += 1
                    break
        
        # Normalize
        similarity = matches / min(len(timestamps_a), len(timestamps_b))
        
        return similarity
    
    def detect_clock_alignment(self, signals: List[AffiliationSignal],
                              precision: int = 60) -> float:
        """
        Detect if signals align to regular clock intervals
        
        Natural behavior drifts; automation aligns
        
        Args:
            precision: Alignment precision in seconds (60 = minute boundaries)
        
        Returns:
            Alignment score 0-1 (higher = more aligned = more suspicious)
        """
        if len(signals) < 5:
            return 0.0
        
        # Convert to seconds since epoch
        timestamps = [(sig.timestamp.timestamp() % precision) for sig in signals]
        
        # Measure clustering around boundaries
        boundaries = [0, precision * 0.25, precision * 0.5, precision * 0.75]
        boundary_tolerance = precision * 0.05  # 5% tolerance
        
        near_boundary = sum(
            1 for ts in timestamps
            if any(abs(ts - b) < boundary_tolerance for b in boundaries)
        )
        
        alignment = near_boundary / len(timestamps)
        
        return alignment


# ============================================================================
# NETWORK HASHER
# ============================================================================

class NetworkHasher:
    """
    Deterministic hashing for graph snapshots
    
    Used for:
    - Drift detection
    - Replay
    - Audit defense
    """
    
    @staticmethod
    def hash_affiliation_profile(profile: NetworkAffiliationProfile) -> str:
        """Generate deterministic hash of network state"""
        # Serialize clusters in sorted order
        cluster_data = sorted([
            (c.cluster_id, c.relationship_type.value, 
             sorted(c.member_accounts), c.cluster_strength)
            for c in profile.affiliation_clusters
        ])
        
        # Include key metrics
        data_str = json.dumps({
            'account_id': profile.account_id,
            'platform': profile.platform,
            'clusters': cluster_data,
            'contagion_risk': round(profile.contagion_risk, 4),
            'isolation_score': round(profile.isolation_score, 4),
            'model_version': profile.network_model_version
        }, sort_keys=True)
        
        return hashlib.sha256(data_str.encode()).hexdigest()
    
    @staticmethod
    def hash_signal_set(signals: List[AffiliationSignal]) -> str:
        """Hash a set of affiliation signals"""
        signal_data = sorted([
            (sig.signal_type.value, sig.abstract_bucket, 
             sig.confidence, sig.timestamp.isoformat())
            for sig in signals
        ])
        
        data_str = json.dumps(signal_data, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()


# ============================================================================
# NETWORK VALIDATOR
# ============================================================================

class NetworkValidator:
    """
    Enforces safety constraints on network modeling
    
    CRITICAL: Prevents accidental drift into forbidden territory
    """
    
    @staticmethod
    def validate_signal(signal: AffiliationSignal) -> Tuple[bool, Optional[str]]:
        """Ensure signal contains no raw identifiers"""
        bucket = signal.abstract_bucket.lower()
        
        # Forbidden patterns
        forbidden_patterns = [
            'ip=', 'ipv4=', 'ipv6=',
            'device_id=', 'udid=', 'imei=',
            'mac=', 'mac_address=',
            'user_id=', 'uid=',
            'email=', 'phone='
        ]
        
        for pattern in forbidden_patterns:
            if pattern in bucket:
                return False, f"Forbidden identifier pattern: {pattern}"
        
        # Must be abstracted/bucketed
        if any(c in bucket for c in ['@', ':', '//', '.']):
            # Check if it looks like a raw IP or URL
            parts = bucket.split('.')
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                return False, "Raw IP address detected"
        
        return True, None
    
    @staticmethod
    def validate_cluster(cluster: AffiliationCluster) -> Tuple[bool, Optional[str]]:
        """Ensure cluster is safe and meaningful"""
        # Size constraints
        if len(cluster.member_accounts) > 10000:
            return False, "Cluster too large (>10k accounts)"
        
        # Strength thresholds
        if cluster.cluster_strength < 0.1:
            return False, "Cluster strength too low (spurious)"
        
        return True, None


# ============================================================================
# NETWORK WATCHDOG
# ============================================================================

@dataclass
class NetworkAlert:
    """Alert from network monitoring"""
    alert_type: str
    severity: float  # 0.0-1.0
    account_id: str
    message: str
    timestamp: datetime
    metadata: Dict = field(default_factory=dict)


class NetworkWatchdog:
    """
    Real-time monitoring for dangerous network patterns
    
    Triggers alerts if:
    - Cluster density spikes
    - Contagion accelerates
    - Isolation drops suddenly
    - Unexpected co-movement appears
    """
    
    def __init__(self, 
                 density_threshold: float = 50.0,
                 contagion_threshold: float = 0.7,
                 isolation_threshold: float = 0.3):
        self.density_threshold = density_threshold
        self.contagion_threshold = contagion_threshold
        self.isolation_threshold = isolation_threshold
        
        # Historical baselines
        self.density_history: Dict[str, List[float]] = defaultdict(list)
        self.contagion_history: Dict[str, List[float]] = defaultdict(list)
        self.isolation_history: Dict[str, List[float]] = defaultdict(list)
    
    def check_profile(self, profile: NetworkAffiliationProfile) -> List[NetworkAlert]:
        """Check profile for dangerous patterns"""
        alerts = []
        now = datetime.utcnow()
        
        # 1. Density spike
        if profile.cluster_density > self.density_threshold:
            alerts.append(NetworkAlert(
                alert_type="DENSITY_SPIKE",
                severity=min(1.0, profile.cluster_density / self.density_threshold),
                account_id=profile.account_id,
                message=f"Cluster density {profile.cluster_density:.1f} exceeds threshold",
                timestamp=now,
                metadata={'density': profile.cluster_density}
            ))
        
        # 2. High contagion risk
        if profile.contagion_risk > self.contagion_threshold:
            alerts.append(NetworkAlert(
                alert_type="HIGH_CONTAGION",
                severity=profile.contagion_risk,
                account_id=profile.account_id,
                message=f"Contagion risk {profile.contagion_risk:.2f} exceeds threshold",
                timestamp=now,
                metadata={'contagion_risk': profile.contagion_risk}
            ))
        
        # 3. Low isolation
        if profile.isolation_score < self.isolation_threshold:
            alerts.append(NetworkAlert(
                alert_type="LOW_ISOLATION",
                severity=1.0 - profile.isolation_score,
                account_id=profile.account_id,
                message=f"Isolation score {profile.isolation_score:.2f} below threshold",
                timestamp=now,
                metadata={'isolation_score': profile.isolation_score}
            ))
        
        # 4. Sudden drops (need history)
        self._check_sudden_changes(profile, alerts, now)
        
        # Update history
        self.density_history[profile.account_id].append(profile.cluster_density)
        self.contagion_history[profile.account_id].append(profile.contagion_risk)
        self.isolation_history[profile.account_id].append(profile.isolation_score)
        
        # Trim history
        max_history = 100
        for hist in [self.density_history, self.contagion_history, self.isolation_history]:
            if len(hist[profile.account_id]) > max_history:
                hist[profile.account_id] = hist[profile.account_id][-max_history:]
        
        return alerts
    
    def _check_sudden_changes(self, profile: NetworkAffiliationProfile,
                             alerts: List[NetworkAlert], now: datetime):
        """Detect sudden metric changes"""
        account_id = profile.account_id
        
        # Need at least 3 historical points
        if len(self.isolation_history[account_id]) < 3:
            return
        
        # Check isolation drop
        recent_isolation = self.isolation_history[account_id][-3:]
        avg_isolation = sum(recent_isolation) / len(recent_isolation)
        
        if avg_isolation > 0.6 and profile.isolation_score < 0.4:
            alerts.append(NetworkAlert(
                alert_type="ISOLATION_DROP",
                severity=0.8,
                account_id=account_id,
                message=f"Sudden isolation drop: {avg_isolation:.2f} -> {profile.isolation_score:.2f}",
                timestamp=now,
                metadata={
                    'previous_avg': avg_isolation,
                    'current': profile.isolation_score
                }
            ))


# ============================================================================
# MAIN ENGINE
# ============================================================================

class NetworkAffiliationEngine:
    """
    Main engine for network affiliation analysis
    
    Orchestrates:
    - Graph construction
    - Cluster detection
    - Risk propagation
    - Isolation scoring
    """
    
    MODEL_VERSION = "v1.0.0"
    
    def __init__(self):
        self.graph_builder = AffiliationGraphBuilder()
        self.contagion_model = ContagionModel()
        self.isolation_scorer = IsolationScorer()
        self.similarity_analyzer = GraphSimilarityAnalyzer()
        self.watchdog = NetworkWatchdog()
        
        # Account risk cache (external input)
        self.account_risks: Dict[str, float] = {}
    
    def register_signals(self, account_id: str, signals: List[AffiliationSignal]):
        """Register affiliation signals for an account"""
        for signal in signals:
            # Validate before accepting
            valid, error = NetworkValidator.validate_signal(signal)
            if not valid:
                raise ValueError(f"Invalid signal: {error}")
            
            self.graph_builder.add_signal(account_id, signal)
    
    def build_profile(self, account_id: str, platform: str) -> NetworkAffiliationProfile:
        """
        Build complete network affiliation profile
        
        This is the main output consumed by trust_scoring
        """
        now = datetime.utcnow()
        
        # Build clusters
        clusters = self.graph_builder.build_clusters()
        
        # Validate clusters
        for cluster in clusters:
            valid, error = NetworkValidator.validate_cluster(cluster)
            if not valid:
                # Log but don't fail - just exclude invalid clusters
                clusters = [c for c in clusters if c != cluster]
        
        # Filter to account's clusters
        account_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        # Compute metrics
        contagion_risk = self.contagion_model.compute_contagion_risk(
            account_id, clusters, self.account_risks
        )
        
        isolation_score = self.isolation_scorer.compute_isolation_score(
            account_id, clusters
        )
        
        cluster_density = self.isolation_scorer.compute_cluster_density(
            account_id, clusters
        )
        
        # Compute graph position metrics
        centrality = self._compute_centrality(account_id, clusters)
        boundary_distance = self._compute_boundary_distance(account_id, clusters)
        
        # Generate signal hash
        account_signals = self.graph_builder.signals.get(account_id, [])
        signal_hash = NetworkHasher.hash_signal_set(account_signals)
        
        # Build profile
        profile = NetworkAffiliationProfile(
            account_id=account_id,
            platform=platform,
            timestamp=now,
            affiliation_clusters=tuple(account_clusters),
            contagion_risk=contagion_risk,
            isolation_score=isolation_score,
            cluster_density=cluster_density,
            centrality_score=centrality,
            boundary_distance=boundary_distance,
            network_model_version=self.MODEL_VERSION,
            signal_hash=signal_hash
        )
        
        # Check for alerts
        alerts = self.watchdog.check_profile(profile)
        if alerts:
            # In production, log to monitoring system
            for alert in alerts:
                print(f"[NETWORK ALERT] {alert.alert_type}: {alert.message}")
        
        return profile
    
    def _compute_centrality(self, account_id: str,
                           clusters: List[AffiliationCluster]) -> float:
        """
        Graph centrality = how connected/central this account is
        
        High centrality = high blast radius if compromised
        """
        member_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        if not member_clusters:
            return 0.0
        
        # Count unique neighbors across all clusters
        neighbors = set()
        for cluster in member_clusters:
            neighbors.update(cluster.member_accounts)
        neighbors.discard(account_id)
        
        # Normalize by cluster participation
        centrality = len(neighbors) / (len(member_clusters) * 10)  # assume max 10 per cluster
        
        return min(1.0, centrality)
    
    def _compute_boundary_distance(self, account_id: str,
                                   clusters: List[AffiliationCluster]) -> float:
        """
        Distance from high-risk cluster boundaries
        
        Lower = closer to dangerous territory
        """
        member_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        if not member_clusters:
            return 1.0  # Far from risk
        
        # Find minimum shared risk across clusters
        min_distance = min(1.0 - c.shared_risk_score for c in member_clusters)
        
        return min_distance
    
    def simulate_penalty_cascade(self, affected_account: str,
                                 severity: float,
                                 platform: str) -> Dict[str, float]:
        """
        Simulate what happens if an account gets penalized
        
        Returns: Map of account_id -> risk_increase
        
        Used for pre-deployment blast radius analysis
        """
        clusters = self.graph_builder.build_clusters()
        risk_deltas = self.contagion_model.propagate_risk_event(
            affected_account, severity, clusters
        )
        
        return risk_deltas
    
    def get_isolation_recommendations(self, account_id: str) -> List[str]:
        """
        Get actionable recommendations to improve isolation
        
        This is how you legitimately optimize
        """
        clusters = self.graph_builder.build_clusters()
        member_clusters = [c for c in clusters if account_id in c.member_accounts]
        
        recommendations = []
        
        # Check cluster sizes
        large_clusters = [c for c in member_clusters if len(c.member_accounts) > 20]
        if large_clusters:
            recommendations.append(
                f"Reduce membership in {len(large_clusters)} large clusters "
                f"(avg size: {sum(len(c.member_accounts) for c in large_clusters) / len(large_clusters):.0f}). "
                "Split infrastructure to smaller, independent groups."
            )
        
        # Check type diversity
        cluster_types = set(c.relationship_type for c in member_clusters)
        if len(cluster_types) < 3:
            recommendations.append(
                "Low cluster type diversity. Vary infrastructure, timing, and ops patterns "
                "to avoid single-point-of-failure coupling."
            )
        
        # Check shared risk
        high_risk_clusters = [c for c in member_clusters if c.shared_risk_score > 0.7]
        if high_risk_clusters:
            recommendations.append(
                f"Member of {len(high_risk_clusters)} high-risk clusters. "
                "Consider infrastructure changes to reduce contagion exposure."
            )
        
        # Check timing alignment
        account_signals = self.graph_builder.signals.get(account_id, [])
        if account_signals:
            alignment = self.similarity_analyzer.detect_clock_alignment(account_signals)
            if alignment > 0.7:
                recommendations.append(
                    f"High timing alignment detected ({alignment:.2f}). "
                    "Add randomized jitter to posting schedules to appear more organic."
                )
        
        if not recommendations:
            recommendations.append("Isolation posture is healthy. Maintain current patterns.")
        
        return recommendations


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

def example_usage():
    """
    Example: Building network profiles for a multi-account setup
    """
    engine = NetworkAffiliationEngine()
    
    # Example: Three accounts sharing some infrastructure
    accounts = ["account_A", "account_B", "account_C"]
    
    # Account A and B share cloud provider (legitimate)
    for acc in ["account_A", "account_B"]:
        engine.register_signals(acc, [
            AffiliationSignal(
                signal_type=AffiliationType.INFRASTRUCTURE,
                abstract_bucket="cloud_provider_aws_us_east_1",
                confidence=0.95,
                timestamp=datetime.utcnow() - timedelta(hours=i)
            ) for i in range(24)
        ])
    
    # All three show similar content ops (natural for same creator)
    for acc in accounts:
        engine.register_signals(acc, [
            AffiliationSignal(
                signal_type=AffiliationType.CONTENT_OPS,
                abstract_bucket="video_format_1080p_landscape",
                confidence=0.8,
                timestamp=datetime.utcnow() - timedelta(hours=i*2)
            ) for i in range(12)
        ])
    
    # Account C has suspicious timing alignment
    engine.register_signals("account_C", [
        AffiliationSignal(
            signal_type=AffiliationType.TIMING_PATTERN,
            abstract_bucket="posting_hour_14",
            confidence=0.9,
            timestamp=datetime.utcnow() - timedelta(days=i)
        ) for i in range(7)
    ])
    
    # Build profiles
    for acc in accounts:
        profile = engine.build_profile(acc, platform="example_platform")
        
        print(f"\n{'='*60}")
        print(f"Account: {acc}")
        print(f"{'='*60}")
        print(f"Cluster Memberships: {len(profile.affiliation_clusters)}")
        print(f"Contagion Risk: {profile.contagion_risk:.3f}")
        print(f"Isolation Score: {profile.isolation_score:.3f}")
        print(f"Cluster Density: {profile.cluster_density:.1f}")
        print(f"Centrality: {profile.centrality_score:.3f}")
        
        # Get recommendations
        recommendations = engine.get_isolation_recommendations(acc)
        print("\nIsolation Recommendations:")
        for rec in recommendations:
            print(f"  • {rec}")
    
    # Simulate penalty cascade
    print(f"\n{'='*60}")
    print("Simulating penalty on account_B (severity 0.8)")
    print(f"{'='*60}")
    
    risk_deltas = engine.simulate_penalty_cascade("account_B", 0.8, "example_platform")
    for acc, delta in risk_deltas.items():
        print(f"  {acc}: +{delta:.3f} contagion risk")


if __name__ == "__main__":
    example_usage()

