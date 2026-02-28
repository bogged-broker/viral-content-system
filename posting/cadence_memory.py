"""
cadence_memory.py - Human-Like Posting Rhythm Memory Engine

Purpose: Long-term behavioral memory for HOW an account posts, not WHAT it posts.
Tracks tempo, consistency, entropy, and rest patterns to avoid platform suppression.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from enum import Enum
import statistics
import math
from collections import defaultdict


class TimeOfDayBucket(Enum):
    """Human posting time classifications"""
    NIGHT = "night"          # 12am - 6am
    MORNING = "morning"      # 6am - 12pm
    AFTERNOON = "afternoon"  # 12pm - 6pm
    EVENING = "evening"      # 6pm - 12am


class ViolationType(Enum):
    """Cadence violation types"""
    ACCELERATION = "acceleration"
    ZERO_REST = "zero_rest"
    PERFECT_PERIODICITY = "perfect_periodicity"
    SUDDEN_SHIFT = "sudden_shift"
    BURST_OVERLOAD = "burst_overload"
    OVERNIGHT_POSTING = "overnight_posting"


class Decision(Enum):
    """Posting decision states"""
    ALLOW = "allow"
    DELAY = "delay"
    BLOCK = "block"


@dataclass
class PostingEvent:
    """Single posting event record"""
    content_id: str
    posted_at: datetime
    interval_since_last: Optional[float]  # seconds
    local_time_bucket: TimeOfDayBucket
    day_of_week: int
    burst_group_id: Optional[str] = None


@dataclass
class BurstProfile:
    """Burst behavior characterization"""
    burst_frequency: float  # bursts per week
    avg_burst_length: float  # posts per burst
    inter_burst_rest_time: float  # seconds between bursts
    cooldown_compliance_score: float  # 0-1, how well they rest after bursts


@dataclass
class CadenceVariance:
    """Statistical variance in posting rhythm"""
    mean_interval: float  # seconds
    interval_stddev: float  # seconds
    entropy_score: float  # 0-1, higher = more random (good)
    regularity_penalty: float  # 0-1, higher = too mechanical


@dataclass
class CadenceMatchScore:
    """Platform-native rhythm alignment"""
    alignment_pct: float  # 0-100
    deviation_risk: float  # 0-1
    acceleration_flag: bool


@dataclass
class RestProfile:
    """Sleep/silence behavior tracking"""
    avg_daily_silence: float  # hours
    longest_rest_window: float  # hours
    rest_regularity_score: float  # 0-1


@dataclass
class CadenceSnapshot:
    """Serializable cadence state snapshot"""
    last_post_at: Optional[datetime]
    rolling_intervals: List[float]  # last N intervals in seconds
    burst_state: Dict
    rest_state: Dict
    variance_metrics: Dict
    
    def to_dict(self) -> Dict:
        """Serialize for persistence"""
        return {
            'last_post_at': self.last_post_at.isoformat() if self.last_post_at else None,
            'rolling_intervals': self.rolling_intervals,
            'burst_state': self.burst_state,
            'rest_state': self.rest_state,
            'variance_metrics': self.variance_metrics
        }


@dataclass
class CadenceRiskSignal:
    """Risk assessment output"""
    risk_score: float  # 0-1
    violation_type: Optional[ViolationType]
    confidence: float  # 0-1
    details: str = ""


@dataclass
class PostingDecision:
    """Decision with reasoning"""
    decision: Decision
    reason: str
    suggested_wait_seconds: Optional[float] = None


class CadenceMemory:
    """Main cadence memory facade"""
    
    # Safety thresholds
    MAX_ACCELERATION_FACTOR = 3.0  # max speedup ratio
    MIN_DAILY_SILENCE_HOURS = 4.0  # minimum rest per 24h
    MAX_PERIODICITY_SCORE = 0.85  # max regularity (too mechanical)
    MAX_BURST_LENGTH = 5  # posts per burst
    MIN_INTER_BURST_REST_SECONDS = 1800  # 30 min
    OVERNIGHT_RISK_HOURS = (0, 5)  # 12am-5am
    
    def __init__(self, window_size: int = 50):
        """
        Args:
            window_size: Number of recent posts to analyze
        """
        self.window_size = window_size
        self._events: Dict[str, List[PostingEvent]] = defaultdict(list)
        self._burst_groups: Dict[str, List[str]] = defaultdict(list)
        
    def record_post(
        self, 
        account_id: str,
        content_id: str,
        posted_at: datetime,
        burst_group_id: Optional[str] = None
    ) -> None:
        """Record a posting event"""
        events = self._events[account_id]
        
        # Calculate interval
        interval = None
        if events:
            interval = (posted_at - events[-1].posted_at).total_seconds()
        
        # Determine time bucket
        hour = posted_at.hour
        if 0 <= hour < 6:
            bucket = TimeOfDayBucket.NIGHT
        elif 6 <= hour < 12:
            bucket = TimeOfDayBucket.MORNING
        elif 12 <= hour < 18:
            bucket = TimeOfDayBucket.AFTERNOON
        else:
            bucket = TimeOfDayBucket.EVENING
        
        event = PostingEvent(
            content_id=content_id,
            posted_at=posted_at,
            interval_since_last=interval,
            local_time_bucket=bucket,
            day_of_week=posted_at.weekday(),
            burst_group_id=burst_group_id
        )
        
        events.append(event)
        
        # Keep window bounded
        if len(events) > self.window_size:
            events.pop(0)
        
        # Track burst groups
        if burst_group_id:
            self._burst_groups[account_id].append(content_id)
    
    def compute_current_cadence(self, account_id: str) -> CadenceVariance:
        """Compute current posting cadence statistics"""
        events = self._events[account_id]
        
        if len(events) < 2:
            return CadenceVariance(
                mean_interval=0,
                interval_stddev=0,
                entropy_score=1.0,
                regularity_penalty=0
            )
        
        intervals = [e.interval_since_last for e in events if e.interval_since_last]
        
        if not intervals:
            return CadenceVariance(0, 0, 1.0, 0)
        
        mean_interval = statistics.mean(intervals)
        stddev = statistics.stdev(intervals) if len(intervals) > 1 else 0
        
        # Calculate entropy (normalized)
        entropy = self._calculate_entropy(intervals)
        
        # Calculate regularity penalty (coefficient of variation inverse)
        cv = stddev / mean_interval if mean_interval > 0 else 0
        regularity_penalty = 1.0 - min(cv, 1.0)  # lower CV = more regular = higher penalty
        
        return CadenceVariance(
            mean_interval=mean_interval,
            interval_stddev=stddev,
            entropy_score=entropy,
            regularity_penalty=regularity_penalty
        )
    
    def get_cadence_risk(self, account_id: str) -> CadenceRiskSignal:
        """Assess current cadence risk"""
        events = self._events[account_id]
        
        if len(events) < 3:
            return CadenceRiskSignal(0, None, 1.0, "Insufficient history")
        
        # Check for acceleration
        acceleration_risk = self._check_acceleration(events)
        if acceleration_risk:
            return acceleration_risk
        
        # Check for zero rest
        rest_risk = self._check_rest_violations(events)
        if rest_risk:
            return rest_risk
        
        # Check for perfect periodicity
        periodicity_risk = self._check_periodicity(events)
        if periodicity_risk:
            return periodicity_risk
        
        # Check for sudden shifts
        shift_risk = self._check_sudden_shifts(events)
        if shift_risk:
            return shift_risk
        
        # Check burst overload
        burst_risk = self._check_burst_overload(account_id, events)
        if burst_risk:
            return burst_risk
        
        # Check overnight posting
        overnight_risk = self._check_overnight_posting(events)
        if overnight_risk:
            return overnight_risk
        
        return CadenceRiskSignal(0.0, None, 1.0, "No violations detected")
    
    def get_rest_profile(self, account_id: str) -> RestProfile:
        """Analyze rest/silence patterns"""
        events = self._events[account_id]
        
        if len(events) < 2:
            return RestProfile(0, 0, 0)
        
        # Calculate daily silence periods
        daily_silences = []
        current_day_events = []
        last_date = None
        
        for event in events:
            event_date = event.posted_at.date()
            if last_date and event_date != last_date:
                # New day - calculate previous day's silence
                if current_day_events:
                    silence = self._calculate_daily_silence(current_day_events)
                    daily_silences.append(silence)
                current_day_events = [event]
            else:
                current_day_events.append(event)
            last_date = event_date
        
        # Add last day
        if current_day_events:
            silence = self._calculate_daily_silence(current_day_events)
            daily_silences.append(silence)
        
        avg_daily_silence = statistics.mean(daily_silences) if daily_silences else 0
        
        # Find longest rest window
        intervals = [e.interval_since_last for e in events if e.interval_since_last]
        longest_rest = max(intervals) / 3600 if intervals else 0  # convert to hours
        
        # Calculate rest regularity (consistency of sleep patterns)
        rest_regularity = 1.0 - (statistics.stdev(daily_silences) / 24.0 if len(daily_silences) > 1 else 0)
        rest_regularity = max(0, min(1.0, rest_regularity))
        
        return RestProfile(
            avg_daily_silence=avg_daily_silence,
            longest_rest_window=longest_rest,
            rest_regularity_score=rest_regularity
        )
    
    def should_post_now(self, account_id: str, timestamp: datetime) -> PostingDecision:
        """Determine if posting is safe right now"""
        events = self._events[account_id]
        
        if not events:
            return PostingDecision(Decision.ALLOW, "First post")
        
        last_event = events[-1]
        time_since_last = (timestamp - last_event.posted_at).total_seconds()
        
        # Check minimum cooldown after burst
        if last_event.burst_group_id:
            if time_since_last < self.MIN_INTER_BURST_REST_SECONDS:
                wait = self.MIN_INTER_BURST_REST_SECONDS - time_since_last
                return PostingDecision(
                    Decision.DELAY,
                    "Cooling down after burst",
                    wait
                )
        
        # Check overnight posting risk
        hour = timestamp.hour
        if self.OVERNIGHT_RISK_HOURS[0] <= hour < self.OVERNIGHT_RISK_HOURS[1]:
            recent_night_posts = sum(
                1 for e in events[-10:] 
                if e.local_time_bucket == TimeOfDayBucket.NIGHT
            )
            if recent_night_posts >= 2:
                return PostingDecision(
                    Decision.BLOCK,
                    "Too many overnight posts - unnatural pattern"
                )
        
        # Check acceleration
        cadence = self.compute_current_cadence(account_id)
        if cadence.mean_interval > 0:
            acceleration = cadence.mean_interval / time_since_last
            if acceleration > self.MAX_ACCELERATION_FACTOR:
                wait = cadence.mean_interval - time_since_last
                return PostingDecision(
                    Decision.DELAY,
                    f"Posting too fast (acceleration: {acceleration:.2f}x)",
                    max(0, wait)
                )
        
        # Check daily rest
        rest_profile = self.get_rest_profile(account_id)
        if rest_profile.avg_daily_silence < self.MIN_DAILY_SILENCE_HOURS:
            # Check if we're in potential rest window
            if hour < 6:  # Early morning
                return PostingDecision(
                    Decision.DELAY,
                    "Need more daily rest time",
                    3600  # Wait 1 hour
                )
        
        # Check general risk
        risk = self.get_cadence_risk(account_id)
        if risk.risk_score > 0.7:
            return PostingDecision(
                Decision.BLOCK,
                f"High cadence risk: {risk.details}"
            )
        elif risk.risk_score > 0.4:
            return PostingDecision(
                Decision.DELAY,
                f"Moderate cadence risk: {risk.details}",
                1800  # 30 min
            )
        
        return PostingDecision(Decision.ALLOW, "Cadence within safe bounds")
    
    def get_burst_profile(self, account_id: str) -> BurstProfile:
        """Analyze burst posting behavior"""
        events = self._events[account_id]
        
        if len(events) < 5:
            return BurstProfile(0, 0, 0, 1.0)
        
        # Detect bursts (posts within 10 minutes)
        bursts = []
        current_burst = []
        
        for i, event in enumerate(events):
            if i == 0:
                current_burst.append(event)
                continue
            
            interval = event.interval_since_last
            if interval and interval < 600:  # 10 minutes
                current_burst.append(event)
            else:
                if len(current_burst) > 1:
                    bursts.append(current_burst)
                current_burst = [event]
        
        if len(current_burst) > 1:
            bursts.append(current_burst)
        
        if not bursts:
            return BurstProfile(0, 0, 0, 1.0)
        
        # Calculate metrics
        total_days = (events[-1].posted_at - events[0].posted_at).days + 1
        burst_frequency = len(bursts) / max(total_days / 7, 1)  # per week
        avg_burst_length = statistics.mean(len(b) for b in bursts)
        
        # Calculate inter-burst rest
        rest_times = []
        for i in range(len(bursts) - 1):
            end_of_burst = bursts[i][-1].posted_at
            start_of_next = bursts[i + 1][0].posted_at
            rest_times.append((start_of_next - end_of_burst).total_seconds())
        
        inter_burst_rest = statistics.mean(rest_times) if rest_times else 0
        
        # Cooldown compliance (are they resting enough between bursts?)
        compliant_rests = sum(1 for r in rest_times if r >= self.MIN_INTER_BURST_REST_SECONDS)
        cooldown_compliance = compliant_rests / len(rest_times) if rest_times else 1.0
        
        return BurstProfile(
            burst_frequency=burst_frequency,
            avg_burst_length=avg_burst_length,
            inter_burst_rest_time=inter_burst_rest,
            cooldown_compliance_score=cooldown_compliance
        )
    
    def get_snapshot(self, account_id: str) -> CadenceSnapshot:
        """Get serializable snapshot of current state"""
        events = self._events[account_id]
        
        intervals = [e.interval_since_last for e in events if e.interval_since_last]
        cadence = self.compute_current_cadence(account_id)
        rest = self.get_rest_profile(account_id)
        burst = self.get_burst_profile(account_id)
        
        return CadenceSnapshot(
            last_post_at=events[-1].posted_at if events else None,
            rolling_intervals=intervals[-20:],  # last 20 intervals
            burst_state={
                'frequency': burst.burst_frequency,
                'avg_length': burst.avg_burst_length,
                'compliance': burst.cooldown_compliance_score
            },
            rest_state={
                'avg_silence': rest.avg_daily_silence,
                'longest_rest': rest.longest_rest_window,
                'regularity': rest.rest_regularity_score
            },
            variance_metrics={
                'mean_interval': cadence.mean_interval,
                'stddev': cadence.interval_stddev,
                'entropy': cadence.entropy_score,
                'regularity_penalty': cadence.regularity_penalty
            }
        )
    
    # Private helper methods
    
    def _calculate_entropy(self, intervals: List[float]) -> float:
        """Calculate normalized entropy of intervals"""
        if len(intervals) < 2:
            return 1.0
        
        # Bin intervals into buckets
        min_int = min(intervals)
        max_int = max(intervals)
        
        if max_int == min_int:
            return 0.0  # Perfect regularity
        
        num_bins = min(10, len(intervals) // 2)
        bin_width = (max_int - min_int) / num_bins
        
        bins = [0] * num_bins
        for interval in intervals:
            bin_idx = min(int((interval - min_int) / bin_width), num_bins - 1)
            bins[bin_idx] += 1
        
        # Calculate Shannon entropy
        total = len(intervals)
        entropy = 0
        for count in bins:
            if count > 0:
                p = count / total
                entropy -= p * math.log2(p)
        
        # Normalize to 0-1
        max_entropy = math.log2(num_bins)
        return entropy / max_entropy if max_entropy > 0 else 0
    
    def _calculate_daily_silence(self, day_events: List[PostingEvent]) -> float:
        """Calculate hours of silence in a day"""
        if not day_events:
            return 24.0
        
        # Sort by time
        sorted_events = sorted(day_events, key=lambda e: e.posted_at)
        
        # Find largest gap
        max_gap = 0
        for i in range(len(sorted_events) - 1):
            gap = (sorted_events[i + 1].posted_at - sorted_events[i].posted_at).total_seconds()
            max_gap = max(max_gap, gap)
        
        # Also check overnight gap (end of day to start of next)
        overnight_gap = 24 * 3600 - (sorted_events[-1].posted_at.hour * 3600 + 
                                      sorted_events[-1].posted_at.minute * 60)
        overnight_gap += (sorted_events[0].posted_at.hour * 3600 + 
                          sorted_events[0].posted_at.minute * 60)
        
        max_gap = max(max_gap, overnight_gap)
        
        return max_gap / 3600  # Convert to hours
    
    def _check_acceleration(self, events: List[PostingEvent]) -> Optional[CadenceRiskSignal]:
        """Check for unnatural acceleration"""
        if len(events) < 10:
            return None
        
        # Compare recent average to historical average
        recent = [e.interval_since_last for e in events[-5:] if e.interval_since_last]
        historical = [e.interval_since_last for e in events[-20:-5] if e.interval_since_last]
        
        if not recent or not historical:
            return None
        
        recent_avg = statistics.mean(recent)
        historical_avg = statistics.mean(historical)
        
        if historical_avg == 0:
            return None
        
        acceleration = historical_avg / recent_avg
        
        if acceleration > self.MAX_ACCELERATION_FACTOR:
            return CadenceRiskSignal(
                risk_score=min(1.0, acceleration / 5.0),
                violation_type=ViolationType.ACCELERATION,
                confidence=0.9,
                details=f"Acceleration factor: {acceleration:.2f}x"
            )
        
        return None
    
    def _check_rest_violations(self, events: List[PostingEvent]) -> Optional[CadenceRiskSignal]:
        """Check for insufficient rest periods"""
        if len(events) < 5:
            return None
        
        # Check last 24 hours
        now = events[-1].posted_at
        day_ago = now - timedelta(hours=24)
        recent = [e for e in events if e.posted_at >= day_ago]
        
        if len(recent) < 2:
            return None
        
        # Find longest gap in last 24h
        max_gap = 0
        for i in range(len(recent) - 1):
            gap = (recent[i + 1].posted_at - recent[i].posted_at).total_seconds() / 3600
            max_gap = max(max_gap, gap)
        
        if max_gap < self.MIN_DAILY_SILENCE_HOURS:
            return CadenceRiskSignal(
                risk_score=0.8,
                violation_type=ViolationType.ZERO_REST,
                confidence=0.95,
                details=f"Only {max_gap:.1f}h rest in 24h (need {self.MIN_DAILY_SILENCE_HOURS}h)"
            )
        
        return None
    
    def _check_periodicity(self, events: List[PostingEvent]) -> Optional[CadenceRiskSignal]:
        """Check for overly regular posting (bot-like)"""
        intervals = [e.interval_since_last for e in events if e.interval_since_last]
        
        if len(intervals) < 5:
            return None
        
        mean = statistics.mean(intervals)
        stddev = statistics.stdev(intervals)
        
        if mean == 0:
            return None
        
        cv = stddev / mean  # Coefficient of variation
        periodicity_score = 1.0 - min(cv, 1.0)
        
        if periodicity_score > self.MAX_PERIODICITY_SCORE:
            return CadenceRiskSignal(
                risk_score=periodicity_score,
                violation_type=ViolationType.PERFECT_PERIODICITY,
                confidence=0.85,
                details=f"Too regular: CV={cv:.3f}"
            )
        
        return None
    
    def _check_sudden_shifts(self, events: List[PostingEvent]) -> Optional[CadenceRiskSignal]:
        """Check for sudden cadence changes"""
        if len(events) < 15:
            return None
        
        # Split into three periods
        third = len(events) // 3
        period1 = [e.interval_since_last for e in events[:third] if e.interval_since_last]
        period3 = [e.interval_since_last for e in events[-third:] if e.interval_since_last]
        
        if not period1 or not period3:
            return None
        
        avg1 = statistics.mean(period1)
        avg3 = statistics.mean(period3)
        
        if avg1 == 0:
            return None
        
        shift_ratio = abs(avg3 - avg1) / avg1
        
        if shift_ratio > 2.0:  # More than 2x change
            return CadenceRiskSignal(
                risk_score=min(1.0, shift_ratio / 3.0),
                violation_type=ViolationType.SUDDEN_SHIFT,
                confidence=0.75,
                details=f"Cadence shifted {shift_ratio:.2f}x"
            )
        
        return None
    
    def _check_burst_overload(self, account_id: str, events: List[PostingEvent]) -> Optional[CadenceRiskSignal]:
        """Check for excessive burst posting"""
        burst_profile = self.get_burst_profile(account_id)
        
        if burst_profile.avg_burst_length > self.MAX_BURST_LENGTH:
            return CadenceRiskSignal(
                risk_score=0.7,
                violation_type=ViolationType.BURST_OVERLOAD,
                confidence=0.8,
                details=f"Average burst length: {burst_profile.avg_burst_length:.1f} posts"
            )
        
        if burst_profile.cooldown_compliance_score < 0.5:
            return CadenceRiskSignal(
                risk_score=0.6,
                violation_type=ViolationType.BURST_OVERLOAD,
                confidence=0.75,
                details=f"Poor burst cooldown compliance: {burst_profile.cooldown_compliance_score:.2f}"
            )
        
        return None
    
    def _check_overnight_posting(self, events: List[PostingEvent]) -> Optional[CadenceRiskSignal]:
        """Check for suspicious overnight activity"""
        recent = events[-10:]
        night_posts = [e for e in recent if e.local_time_bucket == TimeOfDayBucket.NIGHT]
        
        night_ratio = len(night_posts) / len(recent)
        
        if night_ratio > 0.3:  # More than 30% overnight
            return CadenceRiskSignal(
                risk_score=0.65,
                violation_type=ViolationType.OVERNIGHT_POSTING,
                confidence=0.7,
                details=f"{len(night_posts)}/{len(recent)} recent posts overnight"
            )
        
        return None


# Example usage
if __name__ == "__main__":
    memory = CadenceMemory()
    
    # Simulate posting events
    base_time = datetime(2026, 1, 28, 9, 0)
    
    # Healthy pattern: morning posts with natural variance
    for i in range(10):
        offset = i * 3600 + (i % 3) * 600  # ~1h intervals with variance
        post_time = base_time + timedelta(seconds=offset)
        memory.record_post(
            account_id="user123",
            content_id=f"post_{i}",
            posted_at=post_time
        )
    
    # Check if we can post now
    decision = memory.should_post_now("user123", datetime(2026, 1, 28, 20, 0))
    print(f"Decision: {decision.decision.value}")
    print(f"Reason: {decision.reason}")
    
    # Get risk assessment
    risk = memory.get_cadence_risk("user123")
    print(f"\nRisk Score: {risk.risk_score:.2f}")
    print(f"Details: {risk.details}")
    
    # Get profiles
    cadence = memory.compute_current_cadence("user123")
    print(f"\nMean Interval: {cadence.mean_interval / 60:.1f} minutes")
    print(f"Entropy Score: {cadence.entropy_score:.2f}")
    
    rest = memory.get_rest_profile("user123")
    print(f"\nAvg Daily Silence: {rest.avg_daily_silence:.1f} hours")
    print(f"Rest Regularity: {rest.rest_regularity_score:.2f}")